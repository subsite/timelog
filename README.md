# Timelog

A single-user time tracker: Customer → Project, with each entry described in free text. Start/pause/stop, continue earlier work, manual entries, editing, and reports with grouping, filters and CSV export.

Python (FastAPI), SQLite and plain JavaScript. All data is in one database file.

## Deploy on a VPS

```sh
docker compose up -d --build
docker compose exec timelog python app.py set-password
```

The app listens on `127.0.0.1:8000`. Login cookies are HTTPS-only, so it needs an HTTPS reverse proxy. With Caddy:

```
time.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

`TIMELOG_BEHIND_PROXY=1` (set in `compose.yml`) makes the login rate limit use the client address sent by the proxy. Only set it when the app is reachable solely through a proxy.

## Backups

```sh
docker compose exec timelog python app.py backup /data/backup.db
docker compose cp timelog:/data/backup.db ./timelog-$(date +%F).db
```

This is safe while the app is running. To restore, stop the app and replace `timelog.db` in the `timelog-data` volume, deleting any `timelog.db-wal` and `timelog.db-shm` files beside it.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx2
export TIMELOG_INSECURE_COOKIES=1
.venv/bin/python app.py set-password
.venv/bin/uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --no-proxy-headers
```

Run the tests with `.venv/bin/pytest`.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `TIMELOG_DB` | `data/timelog.db` | Database file |
| `TIMELOG_BEHIND_PROXY` | off | Trust one proxy's `X-Forwarded-*` headers |
| `TIMELOG_INSECURE_COOKIES` | off | Allow login over plain HTTP, for local use only |

## API docs

Interactive docs are at `/docs` once you are logged in, and the schema is at `/openapi.json`. The `/docs` page loads Swagger UI from the jsDelivr CDN; the rest of the app loads nothing from third parties.

## Settings in the app

Under **Settings**: theme (system, day, night), date format (ISO or the browser's locale), billing rounding and a default hourly rate. A customer's own rate is set under **Manage → Edit**. The currency follows the browser's locale for now.

Billing rounding rounds billable time up to the next started step (for example 30 minutes), either per entry or per day's work on one project and description. It changes billable hours and amounts in reports and CSV exports, never the tracked time.

## How time is counted

- An entry has one or more time periods. Pausing closes a period and resuming opens a new one.
- Reports count each period on the day it starts, in the browser's time zone. A paused entry resumed the next day appears on both days.
- A period that runs past midnight counts entirely on its start day.
- Starting a new timer stops the current one.
- Overlaps are rejected within an entry, but not between entries.
- Grouping a report by description adds up all entries with the same text, so continuing earlier work keeps its time together.
