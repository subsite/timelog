# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Timelog: a time tracker where each user's data is completely separate (Customer → Project; each entry's free-text description is what was worked on, like Clockify — there is no Task level) with start/pause/resume/stop timers, continuing earlier entries, manual entries, and reports with grouping, filters and CSV export. Python (FastAPI) + SQLite + plain JavaScript with no build step. The whole backend is `app.py`; the whole frontend is `static/app.js` (a hash-routed SPA served from `static/index.html`). `templates/login.html` is the only server-rendered page.

## Commands

```sh
# Setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx2

# Run locally (session cookies are HTTPS-only unless TIMELOG_INSECURE_COOKIES=1)
set -a; . ./.env; set +a
export TIMELOG_INSECURE_COOKIES=1 TIMELOG_BASE_URL=http://localhost:8000
.venv/bin/python app.py add-user you@example.com
.venv/bin/uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --no-proxy-headers

# Tests
.venv/bin/pytest
.venv/bin/pytest tests/test_app.py::test_start_pause_resume_stop

# CLI
python app.py add-user <email>        # invite; also list-users, set-email, disable-user, enable-user,
                                      # logout-user, unlink-github (see USAGE in app.py)
python app.py backup <destination>    # online SQLite backup, safe while running

# Deploy (Docker; listens on 127.0.0.1:8000 behind an HTTPS reverse proxy)
./deploy.sh                           # production: clean tree + tests + DB backup, then rebuild
./deploy.sh staging                   # recreate staging (needed only after .env.staging changes)
docker compose up -d --build          # reads .env (GitHub client ID/secret, TIMELOG_BASE_URL)
docker compose exec timelog python app.py add-user you@example.com
```

**Staging** (https://timelog-staging.subsite.fi, 127.0.0.1:8001): `docker-compose -p timelog-staging -f compose.staging.yml up -d --build` runs this checkout mounted read-only with uvicorn `--reload`, so edits are live there at once (static files and templates immediately, `app.py` on save) — try changes on staging, and only rebuild production (`docker-compose up -d --build`) from committed, tested code. Staging has its own database volume, its own GitHub OAuth app and settings in `.env.staging` (gitignored), and is invite-only: `docker-compose -p timelog-staging -f compose.staging.yml exec timelog python app.py add-user <email>`. On this server only docker-compose v1 is installed: use `docker-compose`, and if a rebuild fails with `KeyError: 'ContainerConfig'`, run `docker-compose rm -sf timelog` first. Production is https://timelog.subsite.fi (Apache proxies to 127.0.0.1:8000).

No linter or formatter is configured.

Environment (`.env`, never committed; template in `.env.example`): `TIMELOG_DB` (default `data/timelog.db`), `TIMELOG_GITHUB_CLIENT_ID`/`TIMELOG_GITHUB_CLIENT_SECRET`, `TIMELOG_BASE_URL` (the GitHub callback is `<base>/auth/github/callback`), `TIMELOG_OPEN_SIGNUP=1` (anyone with GitHub can sign up), `TIMELOG_ENVIRONMENT` (e.g. `Staging`: a red label, a red top line and the name in the tab title; returned by `/api/me` only when set), `TIMELOG_INSECURE_COOKIES=1` (local HTTP only). The SMTP settings in `.env.example` are for planned email sign-in codes and are not used yet.

## Architecture

**App factory.** `create_app(db_path)` initializes the DB and defines all routes as closures over `db_path`, the session secret and the GitHub settings. Pure helpers (segment validation, report tree building, CSV) are module-level functions below it. The app keeps no state in memory between requests.

**Database access.** No ORM; raw `sqlite3` with a fresh connection per request. Use the `reading()` context manager for reads and `writing()` for writes — `writing()` runs `BEGIN IMMEDIATE` and rolls back on any exception, so raising `ApiError` mid-handler undoes partial writes. The schema is the `SCHEMA` string (`CREATE ... IF NOT EXISTS`, applied on every start). There is no migration framework: one-off migrations in `init_db()` detect the old shape and fix it before `SCHEMA` runs (`migrate_tasks_into_descriptions` folds the old `tasks` table into entry descriptions; `add_customer_rate_column` adds `customers.hourly_rate`; `migrate_to_users` gives a single-user database's data and preferences to the user `FIRST_USER_EMAIL` and drops the old password). Table rebuilds use the `*_TABLE` templates with foreign keys off. The `settings` table holds only the session secret. Each user's preferences are JSON in `users.preferences`, through `GET/PUT /api/preferences` (`Preferences` model: theme, date format, billing rounding, default hourly rate, currency).

**Users.** Users share nothing. `customers.user_id` and `entries.user_id` say who owns what; projects belong to a user through their customer, and `entries.user_id` always equals its project's owner. Every route takes `uid: UserId` (a dependency reading `request.state.user_id`, set by the middleware) and every query filters on it; `require_row(conn, table, id, uid)` (via `OWNED_ROW`) treats other users' rows as nonexistent (404 for entries, 400 for customers/projects). New routes must do the same — `test_users_cannot_touch_each_others_data` covers the existing ones.

**Data model.** An entry has a state (`running`/`paused`/`stopped`) and one or more **segments** (`start_ts`, `end_ts` as Unix seconds); duration is the sum of its segments. Pausing closes the open segment, resuming opens a new one. Invariants:
- A partial unique index allows only one non-stopped entry per user; starting a timer stops that user's current one.
- `clean_segments()` enforces per-state rules: running entries have exactly one open segment (the latest), others have none; no overlaps within an entry; active entries cannot have future times. Overlaps between entries are allowed.
- Segments shorter than one second are dropped, and an entry left with no segments is deleted (`close_open_segment`, `stop_entry`).
- Deleting a customer cascades to its projects, but `entries.project_id` is `ON DELETE RESTRICT`, so items with logged time must be archived instead. `sqlite3.IntegrityError` is turned into a 409 by a global handler.

**Errors.** Raise `ApiError(message, status)` for user-facing errors; responses are always `{"error": "..."}`. Pydantic validation errors are flattened to the same shape with status 400. Request bodies use `strict=True` models.

**Auth/security.** Sign-in is GitHub OAuth only, invite-only unless `TIMELOG_OPEN_SIGNUP=1` (then `user_for_github()` creates a user under the first verified email no user has yet): `/auth/github` stores a random `state` in the session and redirects to GitHub; `/auth/github/callback` checks and consumes the state, then `github_identity()` exchanges the code (stdlib `urllib` via `github_request()`, which tests replace with `FakeGitHub`) for the account id and verified emails, and `user_for_github()` matches `users.github_id`, or on first sign-in a verified email of a user not yet linked. The session holds `uid` and the user's `session_epoch`; `disable-user`/`logout-user` bump the epoch to end sessions. An HTTP middleware (`protect`) gates everything except `/login`, `/auth/` and `/static/`: `/api/*` gets 401 JSON, pages redirect to `/login`. `GET /api/me` returns the user's email (shown in the header). `GET /api/export.sqlite` downloads the user's data as a new database (`export_user_database()`: fresh `init_db`, rows copied through `ATTACH` in one transaction, its own session secret, journal mode DELETE); new user-owned tables must be added there too. `POST /api/import/clockify` (`ClockifyImport`: API key used once and never stored, region, `dry_run`) fetches the user's Clockify entries (`fetch_clockify()` via `clockify_request()`, which tests replace with `FakeClockify`), maps them with `clockify_rows()` and inserts them with `import_clockify_rows()`; `entries.source_id` (`clockify:<id>`, unique per user) makes re-imports skip what is already there. API keys: `POST /api/api-key` makes a key (`tl_` + random, shown once; only `users.api_key_hash`, its SHA-256, is stored, and it is not exported), `DELETE` removes it. Paths under `/api/v1/` are the public API: the middleware lets them through without a session and each route checks `Authorization: Bearer <key>` with `user_for_api_key()` (disabled users get 401). `GET /api/v1/timer` returns `public_timer_payload()`. **Live timer:** `/api/timer/stream` (session) and `/api/v1/timer/stream` (API key) are server-sent event streams (`timer_events()`, sent through `event_stream()`): the current timer at once, then again whenever it changes, with a keep-alive comment every `STREAM_KEEPALIVE` seconds; a stream ends when its `render()` returns None (logged out, disabled, key removed). The `protect` middleware calls `TimerHub.notify(uid)` after every successful non-GET request, which wakes that user's streams to re-read the timer — so new write routes need nothing extra. `TimerHub` lives in memory, so the app must stay a single process (one uvicorn worker); `--timeout-graceful-shutdown 3` keeps open streams from blocking restarts. The frontend subscribes in `listenForTimer()`. The middleware also sets a strict CSP (relaxed only for `/docs`, which loads Swagger UI from jsDelivr) — the app itself must not load third-party resources or use inline scripts.

**Reports.** `report_rows()` selects segments joined up to customer, filtered by date range in the client's time zone (`tz` query param). Each segment counts entirely on the day it starts. `include_archived=false` (the page's default, unless "Show archived" is ticked) leaves out time on archived customers and projects. `add_to_tree()` builds nested groups for up to four levels (`MAX_GROUP_LEVELS`) from `GROUPS`; `group_key()` prefixes project labels with the customer unless the customer level is already grouped, so same-named projects are not merged. Running segments count up to `now_ts()`. Billable time is rounded up per billed unit by `billable_amounts()` (unit = entry; day + project + description in the report's time zone; or project over the whole report), and each unit is also split by the report's group keys so every group bills whole steps; the rounding is added to the unit's first row so groups add up to the total. The total can therefore depend on the grouping. Amounts are billable seconds × the customer's `hourly_rate`, or the default rate when that is NULL (`amount_for()`). Tracked seconds are never rounded. CSV export has `detail`/`summary` modes and a `standard`/`excel` dialect (`;` separator, decimal comma, UTF-8 BOM).

**Frontend.** `static/app.js` builds the DOM with a small `h()` helper, calls the JSON API through `api()`, and routes on `#timer`, `#reports`, `#manage`. It corrects for clock skew using the `now` value returned by `/api/timer`. Description inputs share a `<datalist>` filled from `/api/recent` (distinct description + project pairs, newest first); choosing a suggestion fills in its project and billable flag. Preferences are loaded at startup into `S.prefs`; `useLocale()` switches every date/time display and input between ISO text fields (`isoInput`, with a calendar button that opens a hidden native input's `showPicker()`; place `el.field`, not `el`) and the browser's native locale pickers. The theme is mirrored to `localStorage` and applied by `static/theme.js` in `<head>` (also on the login page) to avoid a flash. The currency is the `currency` preference (EUR by default; the allowed codes are `CURRENCIES` in both `app.py` and `app.js`). Within a day, the Timer list groups entries with the same description, project and billable flag (`groupSameWork`). Report form state and last-used project are kept in `localStorage`.

## Testing

All tests are in `tests/test_app.py` and use FastAPI's `TestClient` against a temporary SQLite file. Fixtures: `github` (a `FakeGitHub` patched in; set `account_id`/`emails`/`down`), `clockify` (a `FakeClockify`; add entries with `.entry()`), `anon` (app with `me@example.com` invited, not signed in), `client` (signed in), `other` (a second signed-in user, `other@example.com`), `clock` (monkeypatches `timelog.now_ts` with a settable fake — set `clock.value` to move time). Code must call the module-level `now_ts()` rather than `time.time()` for this to work. Helpers `make_client()`, `sign_in()`, `add_project()`, `add_entry()` and `ts()` (Helsinki-time timestamps) build test data.
