# <img src="static/logo.svg" alt="" width="40" align="top"> Timelog

A time tracker for a few people, each with their own separate data: Customer → Project, with each entry described in free text. Start/pause/stop, continue earlier work, manual entries, editing, and reports with grouping, filters and CSV export.

Python (FastAPI), SQLite and plain JavaScript. All data is in one database file.

## Signing in

Users sign in with GitHub. By default access is by invitation: `python app.py add-user someone@example.com` lets in a GitHub account that has that address **verified**. With `TIMELOG_OPEN_SIGNUP=1` in `.env`, anyone with a GitHub account can sign in, and a new user is created under their first verified email address; `disable-user` still keeps someone out. The first sign-in links the GitHub account to the user, so later email changes on GitHub do not matter. Users share nothing; each has their own customers, projects, entries and settings.

Set up a GitHub OAuth app (GitHub → Settings → Developer settings → OAuth Apps) with the callback URL `https://<your-host>/auth/github/callback`, and put its client ID and secret in `.env` (copy `.env.example`).

```sh
python app.py add-user <email>          # invite
python app.py list-users
python app.py set-email <email> <new>   # change a user's email
python app.py disable-user <email>      # block and log out; enable-user undoes it
python app.py logout-user <email>       # end the user's sessions everywhere
python app.py unlink-github <email>     # allow a different GitHub account
```

A database from before users existed is migrated on start: all its data goes to the user `owner@localhost`. Give that user your real email with `set-email`.

## Deploy on a VPS

```sh
cp .env.example .env   # fill in the GitHub client ID and secret, and TIMELOG_BASE_URL
docker compose up -d --build
docker compose exec timelog python app.py add-user you@example.com
```

The app listens on `127.0.0.1:8000`. Session cookies are HTTPS-only, so it needs an HTTPS reverse proxy. With Caddy:

```
time.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

### Staging

`compose.staging.yml` runs a second copy on `127.0.0.1:8001` for trying changes first. It uses the code in your checkout directly (mounted read-only, reloading when `app.py` changes), so there is nothing to rebuild while you work; production keeps running its image until you rebuild it. Staging has its own database and its own settings file, `.env.staging`, which needs its own GitHub OAuth app with the staging host's callback URL.

```sh
cp .env.example .env.staging   # the staging OAuth app, and TIMELOG_BASE_URL=https://staging.example.com
docker compose -p timelog-staging -f compose.staging.yml up -d --build
docker compose -p timelog-staging -f compose.staging.yml exec timelog python app.py add-user you@example.com
```

## Backups

```sh
docker compose exec timelog python app.py backup /data/backup.db
docker compose cp timelog:/data/backup.db ./timelog-$(date +%F).db
```

This is safe while the app is running. To restore, stop the app and replace `timelog.db` in the `timelog-data` volume, deleting any `timelog.db-wal` and `timelog.db-shm` files beside it.

## Move your data to your own server

Any user can download their own data under **Settings → Your data → Download my data (SQLite)**. The file is a complete Timelog database holding only that user: their customers, projects, entries (a running timer keeps running) and settings, and nothing from other users. It has its own new session secret.

To run Timelog with it:

1. Set up Timelog on your server as described under [Deploy on a VPS](#deploy-on-a-vps), with your own GitHub OAuth app in `.env`.
2. Before the first start, copy the downloaded file into place as the database. With Docker, copy it from inside a one-off container so it belongs to the app's user:

   ```sh
   docker compose run --rm -v "$PWD/timelog-2026-11-01.db:/import.db:ro" timelog cp /import.db /data/timelog.db
   docker compose up -d
   ```

   If the app has already run, stop it first and delete any `timelog.db-wal` and `timelog.db-shm` files in the volume. Without Docker, point `TIMELOG_DB` at the file.
3. Sign in with the same GitHub account as before; your user is already in the file, so `add-user` is not needed. To use a different GitHub account, run `python app.py unlink-github <email>` first.

The download is a snapshot: later changes on the old server are not included.

## Import from Clockify

Under **Settings → Import from Clockify**, a user can copy all their Clockify time entries into Timelog. It uses Clockify's API, so the free plan's one-month limit on report exports does not apply.

1. In Clockify, create an API key: Preferences → Advanced → Manage API keys → Generate new.
2. Paste it in Timelog, choose your workspace's region if it has one, and click **Preview**. Nothing is saved yet; the preview shows how many entries, customers and projects would be added.
3. Click **Import**. The key is used for this request only and is never stored; you can delete it in Clockify afterwards.

It imports the entries of the key's user in their active Clockify workspace. Client → customer, project → project, and the task and description become the description ("Task: description"); entries without a client or project go under "(no client)" and "(no project)". Existing customers and projects with the same names are reused. A running Clockify timer and entries without valid times are skipped. Each imported entry remembers its Clockify id, so importing again only adds new entries. Hourly rates are not imported; set them under Manage.

Clockify's free plan allows 30 API requests an hour; one import uses about five.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx2
set -a; . ./.env; set +a                # GitHub client ID and secret
export TIMELOG_INSECURE_COOKIES=1 TIMELOG_BASE_URL=http://localhost:8000
.venv/bin/python app.py add-user you@example.com
.venv/bin/uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --no-proxy-headers
```

The GitHub OAuth app needs `http://localhost:8000/auth/github/callback` as a second callback URL for this. Run the tests with `.venv/bin/pytest`.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `TIMELOG_DB` | `data/timelog.db` | Database file |
| `TIMELOG_GITHUB_CLIENT_ID` | none | GitHub OAuth app's client ID |
| `TIMELOG_GITHUB_CLIENT_SECRET` | none | GitHub OAuth app's client secret |
| `TIMELOG_BASE_URL` | `http://localhost:8000` | Where users reach Timelog; the GitHub callback is built from it |
| `TIMELOG_ENVIRONMENT` | unset | A label such as `Staging`, shown in red on every page so a test copy is not mistaken for production |
| `TIMELOG_INSECURE_COOKIES` | off | Allow sessions over plain HTTP, for local use only |

## API docs

Interactive docs are at `/docs` once you are logged in, and the schema is at `/openapi.json`. The `/docs` page loads Swagger UI from the jsDelivr CDN; the rest of the app loads nothing from third parties.

## Timer API

Scripts, status bars and widgets can read your running timer. Make an API key under **Settings → API** (it is shown once; only a hash is stored, and making a new one replaces it), then:

```sh
curl -H "Authorization: Bearer tl_..." https://<your-host>/api/v1/timer
```

```json
{
  "running": true,
  "entry": {
    "state": "running",
    "description": "Fix login bug",
    "project": "Website",
    "customer": "Acme",
    "billable": true,
    "started_at": "2026-11-05T08:00:00Z",
    "running_since": "2026-11-05T09:30:00Z",
    "elapsed_seconds": 4500,
    "elapsed": "1:15:00"
  },
  "now": "2026-11-05T09:45:00Z"
}
```

`entry` is `null` when no timer is on. A paused timer has `"running": false`, `"state": "paused"` and `"running_since": null`. Times are UTC; `elapsed_seconds` counts only the time the timer has run. A missing or wrong key gets 401.

## Settings in the app

Under **Settings**: theme (system, day, night), date format (ISO text with a calendar button, or the browser's locale), billing rounding, currency (EUR by default) and a default hourly rate. A customer's own rate is set under **Manage → Edit**. The currency is only a display label: amounts are plain numbers, and CSV exports have no currency.

Billing rounding rounds billable time up to the next started step (for example 30 minutes), per entry, per day's work on one project and description, or per project (its whole time in the report's date range). Rounding is done within each row of a report, so every row bills whole steps: grouped by month, a project is rounded once per month, and the total is the sum of the rows. It changes billable hours and amounts in reports and CSV exports, never the tracked time.

## How time is counted

- An entry has one or more time periods. Pausing closes a period and resuming opens a new one.
- Reports count each period on the day it starts, in the browser's time zone. A paused entry resumed the next day appears on both days.
- A period that runs past midnight counts entirely on its start day.
- Starting a new timer stops the current one.
- Overlaps are rejected within an entry, but not between entries.
- Grouping a report by description adds up all entries with the same text, so continuing earlier work keeps its time together.
- Customers and projects with logged time can't be deleted, only archived. Reports leave out archived customers and projects unless **Show archived** is ticked.

## License

Made by [SubSite](https://subsite.fi). Copyright © 2026 Fredrik Welander.

Timelog is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full text.
