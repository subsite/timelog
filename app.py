"""Timelog: a time tracker (Customer > Project, entries described in free text); each user's data is separate."""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).parent

SCHEMA = """
-- Only the session secret; everything else belongs to a user.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
{users};
{customers}
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    archived INTEGER NOT NULL DEFAULT 0,
    UNIQUE (customer_id, name)
);
{entries}
-- Each user can have only one entry running or paused at a time.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_entry_per_user ON entries (user_id)
    WHERE state != 'stopped';
CREATE INDEX IF NOT EXISTS entries_user ON entries (user_id);
CREATE UNIQUE INDEX IF NOT EXISTS entries_source ON entries (user_id, source_id)
    WHERE source_id IS NOT NULL;
-- An entry's duration is the sum of its segments; pausing closes a segment.
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    CHECK (end_ts IS NULL OR end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS segments_entry ON segments (entry_id);
CREATE INDEX IF NOT EXISTS segments_start ON segments (start_ts);
"""

# Users share nothing: customers and entries belong to one user, projects to a customer.
USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    -- Users are invited by email; signing in with GitHub matches a verified address.
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    -- Set on the first GitHub sign-in, so later sign-ins survive email changes on GitHub.
    github_id INTEGER UNIQUE,
    preferences TEXT NOT NULL DEFAULT '{}',
    disabled INTEGER NOT NULL DEFAULT 0,
    -- Stored in sessions; raising it logs the user out everywhere.
    session_epoch INTEGER NOT NULL DEFAULT 0,
    -- SHA-256 of the user's API key (the key itself is shown once and never stored).
    api_key_hash TEXT
)
"""

CUSTOMERS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    archived INTEGER NOT NULL DEFAULT 0,
    -- NULL means the default hourly rate from the user's preferences.
    hourly_rate REAL CHECK (hourly_rate IS NULL OR hourly_rate >= 0),
    UNIQUE (user_id, name)
);
"""

# The description is what was worked on; there is no task level below projects. user_id is
# always the owner of the entry's project; it is stored for the one-active-entry index.
ENTRIES_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    description TEXT NOT NULL DEFAULT '',
    billable INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'stopped' CHECK (state IN ('running', 'paused', 'stopped')),
    -- Where an imported entry came from, e.g. "clockify:<id>", so importing again skips it.
    source_id TEXT
);
"""
SCHEMA += "CREATE UNIQUE INDEX IF NOT EXISTS users_api_key ON users (api_key_hash);\n"
SCHEMA = SCHEMA.replace("{users}", USERS_TABLE)
SCHEMA = SCHEMA.replace("{customers}", CUSTOMERS_TABLE.format(name="customers"))
SCHEMA = SCHEMA.replace("{entries}", ENTRIES_TABLE.format(name="entries"))

# The entries table before users existed; migrate_tasks_into_descriptions still builds it.
SINGLE_USER_ENTRIES_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    description TEXT NOT NULL DEFAULT '',
    billable INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'stopped' CHECK (state IN ('running', 'paused', 'stopped'))
);
"""
# Owner of a single-user database's data after migration; rename with `app.py set-email`.
FIRST_USER_EMAIL = "owner@localhost"
EMAIL_PATTERN = r"[^@\s]+@[^@\s]+"

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"

# Clockify keeps each workspace in one region, and its API answers only at that region's address.
CLOCKIFY_REGIONS = {
    "global": "https://api.clockify.me/api/v1",
    "euc1": "https://euc1.clockify.me/api/v1",
    "use2": "https://use2.clockify.me/api/v1",
    "euw2": "https://euw2.clockify.me/api/v1",
    "apse2": "https://apse2.clockify.me/api/v1",
}
CLOCKIFY_PAGE_SIZE = 5000  # The most Clockify returns per request; the free plan allows 30 an hour.
CLOCKIFY_MAX_PAGES = 100
NO_CLIENT = "(no client)"
NO_PROJECT = "(no project)"

TIME_GROUPS = {"day", "week", "month"}
GROUPS = TIME_GROUPS | {"customer", "project", "description"}
MAX_GROUP_LEVELS = 4
MIN_TS = 946684800  # 2000-01-01
MAX_TS = 4102444800  # 2100-01-01
SESSION_LIFETIME = int(timedelta(days=90).total_seconds())

CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
# FastAPI's /docs page loads Swagger UI from a CDN and starts it with an inline script.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; frame-ancestors 'none'; base-uri 'none'"
)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- Request bodies and queries ---

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Timestamp = Annotated[int, Field(ge=MIN_TS, le=MAX_TS)]
Rate = Annotated[float, Field(ge=0, le=1_000_000)]


class Body(BaseModel):
    model_config = ConfigDict(strict=True)


class CustomerCreate(Body):
    name: Name


class ProjectCreate(Body):
    name: Name
    customer_id: int


class ItemUpdate(Body):
    name: Name | None = None
    archived: bool | None = None
    # Customers only; null goes back to the default rate.
    hourly_rate: Rate | None = None


class TimerStart(Body):
    project_id: int
    description: Description = ""
    billable: bool = True


class Segment(Body):
    start: Timestamp
    end: Timestamp | None = None


class ClockifyImport(Body):
    # Used for this request only, never stored.
    api_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    region: Literal["global", "euc1", "use2", "euw2", "apse2"] = "global"
    # Only count what would be imported.
    dry_run: bool = True


class EntryWrite(Body):
    project_id: int
    description: Description = ""
    billable: bool = True
    segments: list[Segment]


CURRENCIES = (
    "EUR", "USD", "GBP", "SEK", "NOK", "DKK", "ISK", "CHF", "PLN", "CZK", "HUF", "RON", "BGN",
    "CAD", "AUD", "NZD", "JPY", "CNY", "INR", "BRL", "MXN", "ZAR", "SGD", "HKD", "KRW", "TRY",
    "UAH", "ILS",
)


class Preferences(Body):
    theme: Literal["system", "light", "dark"] = "system"
    date_format: Literal["iso", "locale"] = "iso"
    # Billable time is rounded up to the next started step; 0 turns rounding off.
    round_minutes: Literal[0, 5, 6, 10, 15, 30, 60] = 0
    # What one billed unit is: each entry, each day's work on one project and description,
    # or each project's whole time in the report.
    round_scope: Literal["entry", "day", "project"] = "entry"
    # Used for customers without their own rate.
    default_rate: Rate = 0
    # Only shown in the interface; amounts are plain numbers.
    currency: Literal[CURRENCIES] = "EUR"


def current_user(request: Request) -> int:
    """The logged-in user's id; the protect middleware has already checked the session."""
    return request.state.user_id


UserId = Annotated[int, Depends(current_user)]


class ReportQuery(BaseModel):
    tz: str = "UTC"
    start_date: date | None = Field(default=None, alias="from")
    to: date | None = None
    customer_id: int | None = None
    project_id: int | None = None
    billable: Literal["", "0", "1"] = ""
    # False leaves out time on archived customers and projects.
    include_archived: bool = True
    q: str = ""
    group: str = ""


class CsvQuery(ReportQuery):
    mode: Literal["detail", "summary"] = "detail"
    dialect: Literal["standard", "excel"] = "standard"


# --- Database ---


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    # Migrations of older shapes run first; SCHEMA then only adds what is missing.
    migrate_tasks_into_descriptions(conn)
    add_customer_rate_column(conn)
    migrate_to_users(conn)
    add_entry_source_column(conn)
    add_user_api_key_column(conn)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('secret_key', ?)",
        (secrets.token_hex(32),),
    )
    return conn


def migrate_tasks_into_descriptions(conn):
    """Older databases had Customer > Project > Task; a task's name moves into its entries' descriptions."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone():
        return
    # Rebuilding a table referenced by segments needs foreign keys off, outside a transaction.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS entries_new")
        conn.execute(SINGLE_USER_ENTRIES_TABLE.format(name="entries_new"))
        conn.execute(
            """
            INSERT INTO entries_new (id, project_id, description, billable, state)
            SELECT e.id, t.project_id,
                   CASE WHEN e.description = '' THEN t.name ELSE t.name || ': ' || e.description END,
                   e.billable, e.state
            FROM entries e JOIN tasks t ON t.id = e.task_id
            """
        )
        conn.execute("DROP TABLE entries")
        conn.execute("ALTER TABLE entries_new RENAME TO entries")
        conn.execute("DROP TABLE tasks")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Migration left broken references; nothing was changed.")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def table_columns(conn, table):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def add_entry_source_column(conn):
    columns = table_columns(conn, "entries")
    if columns and "source_id" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN source_id TEXT")


def add_user_api_key_column(conn):
    columns = table_columns(conn, "users")
    if columns and "api_key_hash" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN api_key_hash TEXT")


def add_customer_rate_column(conn):
    columns = table_columns(conn, "customers")
    if columns and "hourly_rate" not in columns:
        conn.execute(
            "ALTER TABLE customers ADD COLUMN hourly_rate REAL "
            "CHECK (hourly_rate IS NULL OR hourly_rate >= 0)"
        )


def migrate_to_users(conn):
    """Single-user databases get one user, FIRST_USER_EMAIL, who owns all data and the
    preferences; the old password is dropped. Until then customers were global and entries
    had no owner."""
    customers = table_columns(conn, "customers")
    if not customers or "user_id" in customers:
        return
    # Rebuilding tables that others reference needs foreign keys off, outside a transaction.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(USERS_TABLE)
        password_hash = get_setting(conn, "password_hash")
        preferences = get_setting(conn, "preferences") or "{}"
        has_data = conn.execute("SELECT 1 FROM customers").fetchone()
        user_id = None
        if password_hash or has_data:
            user_id = conn.execute(
                "INSERT INTO users (email, preferences) VALUES (?, ?)", (FIRST_USER_EMAIL, preferences)
            ).lastrowid
        conn.execute("DELETE FROM settings WHERE key IN ('password_hash', 'preferences')")

        for table, template, columns in (
            ("customers", CUSTOMERS_TABLE, "id, name, archived, hourly_rate"),
            ("entries", ENTRIES_TABLE, "id, project_id, description, billable, state"),
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}_new")
            conn.execute(template.format(name=f"{table}_new"))
            conn.execute(
                f"INSERT INTO {table}_new (user_id, {columns}) SELECT ?, {columns} FROM {table}",
                (user_id,),
            )
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Migration left broken references; nothing was changed.")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def read_preferences(conn, user_id):
    row = conn.execute("SELECT preferences FROM users WHERE id = ?", (user_id,)).fetchone()
    try:
        return Preferences.model_validate(json.loads(row["preferences"]))
    except (TypeError, ValueError):
        return Preferences()


def export_user_database(source_path, user_id, destination):
    """Writes a new database at destination holding only this user's data, ready to run as
    another server's TIMELOG_DB. It gets its own session secret, never this server's."""
    init_db(destination).close()
    conn = connect(source_path)
    try:
        conn.execute("ATTACH DATABASE ? AS export", (str(destination),))
        # One transaction, so the copy is consistent even while the user keeps working.
        conn.execute("BEGIN")
        conn.execute(
            """
            INSERT INTO export.users (id, email, github_id, preferences)
            SELECT id, email, github_id, preferences FROM users WHERE id = ?
            """,
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO export.customers (id, user_id, name, archived, hourly_rate)
            SELECT id, user_id, name, archived, hourly_rate FROM customers WHERE user_id = ?
            """,
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO export.projects (id, customer_id, name, archived)
            SELECT p.id, p.customer_id, p.name, p.archived
            FROM projects p JOIN customers c ON c.id = p.customer_id WHERE c.user_id = ?
            """,
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO export.entries (id, user_id, project_id, description, billable, state, source_id)
            SELECT id, user_id, project_id, description, billable, state, source_id
            FROM entries WHERE user_id = ?
            """,
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO export.segments (id, entry_id, start_ts, end_ts)
            SELECT s.id, s.entry_id, s.start_ts, s.end_ts
            FROM segments s JOIN entries e ON e.id = s.entry_id WHERE e.user_id = ?
            """,
            (user_id,),
        )
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE export")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    # A single self-contained file, without a -wal file beside it.
    out = connect(destination)
    out.execute("PRAGMA journal_mode = DELETE")
    out.close()


def db_path_from_env():
    return os.environ.get("TIMELOG_DB", str(BASE_DIR / "data" / "timelog.db"))


# --- Users ---


def normalize_email(email):
    email = email.strip()
    if len(email) > 254 or not re.fullmatch(EMAIL_PATTERN, email):
        raise ValueError(f"Not an email address: {email!r}")
    return email


def find_user(conn, email):
    return conn.execute("SELECT * FROM users WHERE email = ?", (email.strip(),)).fetchone()


def require_user(conn, email):
    user = find_user(conn, email)
    if user is None:
        raise ValueError(f"No user with the email {email}.")
    return user


def add_user(conn, email):
    email = normalize_email(email)
    if find_user(conn, email):
        raise ValueError(f"A user with the email {email} already exists.")
    return conn.execute("INSERT INTO users (email) VALUES (?)", (email,)).lastrowid


API_KEY_PREFIX = "tl_"


def hash_api_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def user_for_api_key(conn, key):
    """The enabled user owning this API key, or None."""
    if not key.startswith(API_KEY_PREFIX):
        return None
    user = conn.execute(
        "SELECT id, disabled FROM users WHERE api_key_hash = ?", (hash_api_key(key),)
    ).fetchone()
    return user["id"] if user and not user["disabled"] else None


# --- Sign in with GitHub ---


class ClockifyError(Exception):
    pass


def clockify_request(base_url, path, api_key, params=None):
    """One GET to Clockify's API, returning the decoded JSON."""
    url = f"{base_url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"X-Api-Key": api_key, "Accept": "application/json", "User-Agent": "Timelog"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise ClockifyError(
                "Clockify did not accept the API key. Check the key, and the region if your workspace has one."
            ) from None
        if err.code == 429:
            raise ClockifyError("Clockify's request limit was reached. Try again in an hour.") from None
        raise ClockifyError(f"Clockify answered with an error ({err.code}).") from None
    except (OSError, ValueError):
        raise ClockifyError("Could not reach Clockify. Try again in a moment.") from None


def fetch_clockify(api_key, region):
    """The workspace name and every time entry of the key's user in their active workspace."""
    base = CLOCKIFY_REGIONS[region]
    user = clockify_request(base, "/user", api_key)
    workspace_id = user.get("activeWorkspace") or user.get("defaultWorkspace")
    if not workspace_id:
        raise ClockifyError("The Clockify account has no workspace.")
    workspaces = clockify_request(base, "/workspaces", api_key)
    name = next((w.get("name") for w in workspaces if w.get("id") == workspace_id), "")
    clients = {}
    for archived in ("false", "true"):
        for client in clockify_request(
            base, f"/workspaces/{workspace_id}/clients", api_key,
            {"archived": archived, "page-size": CLOCKIFY_PAGE_SIZE},
        ):
            clients[client.get("id")] = client.get("name") or ""
    entries = []
    for page in range(1, CLOCKIFY_MAX_PAGES + 1):
        batch = clockify_request(
            base, f"/workspaces/{workspace_id}/user/{user['id']}/time-entries", api_key,
            {"hydrated": "true", "page": page, "page-size": CLOCKIFY_PAGE_SIZE},
        )
        entries.extend(batch)
        if len(batch) < CLOCKIFY_PAGE_SIZE:
            break
    return name, clients, entries


def parse_clockify_time(value):
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except (TypeError, ValueError):
        return None


def clockify_rows(entries, clients):
    """Clockify time entries as Timelog rows; returns (rows, running, invalid)."""
    rows, running, invalid = [], 0, 0
    for item in entries:
        interval = item.get("timeInterval") or {}
        start = parse_clockify_time(interval.get("start"))
        end = parse_clockify_time(interval.get("end"))
        if start is not None and interval.get("end") is None:
            running += 1
            continue
        if start is None or end is None or end <= start or start < MIN_TS or end > MAX_TS:
            invalid += 1
            continue
        project = item.get("project") or {}
        customer = (
            project.get("clientName") or clients.get(project.get("clientId")) or NO_CLIENT
        ) if project else NO_CLIENT
        task = (item.get("task") or {}).get("name") or ""
        text = (item.get("description") or "").strip()
        description = f"{task}: {text}" if task and text else task or text
        rows.append(
            {
                "source_id": f"clockify:{item['id']}",
                "customer": customer.strip()[:200] or NO_CLIENT,
                "project": (project.get("name") or NO_PROJECT).strip()[:200] or NO_PROJECT,
                "description": description[:2000],
                "billable": bool(item.get("billable")),
                "start": start,
                "end": end,
            }
        )
    return rows, running, invalid


def import_clockify_rows(conn, user_id, rows, dry_run):
    """Adds rows not imported before, creating customers and projects by name as needed."""
    imported = {
        row["source_id"]
        for row in conn.execute(
            "SELECT source_id FROM entries WHERE user_id = ? AND source_id IS NOT NULL", (user_id,)
        )
    }
    customers = {
        row["name"].casefold(): row["id"]
        for row in conn.execute("SELECT id, name FROM customers WHERE user_id = ?", (user_id,))
    }
    projects = {
        (row["customer_id"], row["name"].casefold()): row["id"]
        for row in conn.execute(
            """
            SELECT p.id, p.customer_id, p.name FROM projects p
            JOIN customers c ON c.id = p.customer_id WHERE c.user_id = ?
            """,
            (user_id,),
        )
    }
    new_customers, new_projects, added, skipped = [], [], 0, 0
    seen = set()
    for row in rows:
        if row["source_id"] in imported or row["source_id"] in seen:
            skipped += 1
            continue
        seen.add(row["source_id"])
        customer_key = row["customer"].casefold()
        if customer_key not in customers:
            new_customers.append(row["customer"])
            customers[customer_key] = None if dry_run else conn.execute(
                "INSERT INTO customers (user_id, name) VALUES (?, ?)", (user_id, row["customer"])
            ).lastrowid
        customer_id = customers[customer_key]
        project_key = (customer_id if customer_id is not None else customer_key, row["project"].casefold())
        if project_key not in projects:
            new_projects.append(f"{row['customer']} / {row['project']}")
            projects[project_key] = None if dry_run else conn.execute(
                "INSERT INTO projects (customer_id, name) VALUES (?, ?)", (customer_id, row["project"])
            ).lastrowid
        if not dry_run:
            entry_id = conn.execute(
                """
                INSERT INTO entries (user_id, project_id, description, billable, source_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, projects[project_key], row["description"], int(row["billable"]), row["source_id"]),
            ).lastrowid
            conn.execute(
                "INSERT INTO segments (entry_id, start_ts, end_ts) VALUES (?, ?, ?)",
                (entry_id, row["start"], row["end"]),
            )
        added += 1
    return {
        "entries": added,
        "already_imported": skipped,
        "new_customers": sorted(new_customers, key=str.casefold),
        "new_projects": sorted(new_projects, key=str.casefold),
    }


def github_request(url, token=None, data=None):
    """One JSON request to GitHub: a POST when data is given, otherwise a GET."""
    headers = {"Accept": "application/json", "User-Agent": "Timelog"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def github_identity(client_id, client_secret, code, redirect_uri):
    """Exchanges a callback code for the GitHub account's id and verified email addresses."""
    token = github_request(
        GITHUB_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    ).get("access_token")
    if not token:
        raise ValueError("GitHub did not accept the sign-in code.")
    account = github_request(f"{GITHUB_API}/user", token=token)
    emails = github_request(f"{GITHUB_API}/user/emails", token=token)
    verified = [item["email"] for item in emails if item.get("verified")]
    return int(account["id"]), verified


def user_for_github(conn, github_id, verified_emails, open_signup=False):
    """The enabled user this GitHub account signs in as, linking it on the first sign-in.

    With open sign-up, an account that matches no user becomes a new user under its first
    verified email that no other user has."""
    user = conn.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
    if user is None:
        for email in verified_emails:
            found = find_user(conn, email)
            if found and found["github_id"] is None:
                conn.execute("UPDATE users SET github_id = ? WHERE id = ?", (github_id, found["id"]))
                user = find_user(conn, email)
                break
    if user is None and open_signup:
        free = [email for email in verified_emails if not find_user(conn, email)]
        if free:
            uid = add_user(conn, free[0])
            conn.execute("UPDATE users SET github_id = ? WHERE id = ?", (github_id, uid))
            user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if user is None or user["disabled"]:
        return None
    return user


# --- Application ---


def create_app(db_path=None):
    db_path = db_path or db_path_from_env()
    conn = init_db(db_path)
    secret_key = get_setting(conn, "secret_key")
    conn.close()

    github_client_id = os.environ.get("TIMELOG_GITHUB_CLIENT_ID", "")
    github_client_secret = os.environ.get("TIMELOG_GITHUB_CLIENT_SECRET", "")
    base_url = os.environ.get("TIMELOG_BASE_URL", "http://localhost:8000").rstrip("/")
    open_signup = os.environ.get("TIMELOG_OPEN_SIGNUP") == "1"
    github_callback = f"{base_url}/auth/github/callback"
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @contextmanager
    def reading():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def writing():
        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def session_user(session):
        """The logged-in user's id, if that user still exists, is enabled and has not been
        logged out everywhere since this session signed in."""
        user_id = session.get("uid")
        if not isinstance(user_id, int):
            return None
        with reading() as conn:
            user = conn.execute(
                "SELECT disabled, session_epoch FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if not user or user["disabled"] or session.get("epoch") != user["session_epoch"]:
            return None
        return user_id

    app = FastAPI(title="Timelog", docs_url="/docs", redoc_url=None)

    @app.middleware("http")
    async def protect(request, call_next):
        path = request.url.path
        # /api/v1/ is the public API: its routes check an API key instead of the session.
        if path == "/login" or path.startswith(("/static/", "/auth/", "/api/v1/")):
            response = await call_next(request)
        else:
            user_id = await run_in_threadpool(session_user, request.session)
            if user_id is not None:
                request.state.user_id = user_id
                response = await call_next(request)
            elif path.startswith("/api/"):
                response = JSONResponse({"error": "Not logged in"}, status_code=401)
            else:
                response = RedirectResponse("/login", status_code=303)
        response.headers["Content-Security-Policy"] = DOCS_CSP if path == "/docs" else CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # Revalidate pages and assets so an upgrade takes effect on the next load.
        response.headers["Cache-Control"] = "no-cache"
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="timelog_session",
        max_age=SESSION_LIFETIME,
        same_site="lax",
        https_only=os.environ.get("TIMELOG_INSECURE_COOKIES") != "1",
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.exception_handler(ApiError)
    def on_api_error(_request, err):
        return JSONResponse({"error": err.message}, status_code=err.status)

    @app.exception_handler(RequestValidationError)
    def on_validation_error(_request, err):
        first = err.errors()[0]
        field = ".".join(str(part) for part in first["loc"] if part not in ("body", "query"))
        message = f"{field}: {first['msg']}" if field else first["msg"]
        return JSONResponse({"error": message}, status_code=400)

    @app.exception_handler(sqlite3.IntegrityError)
    def on_integrity_error(_request, err):
        if "UNIQUE" in str(err):
            return JSONResponse({"error": "That name already exists here."}, status_code=409)
        return JSONResponse({"error": "It still has logged time. Archive it instead."}, status_code=409)

    # --- Pages and login ---

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(BASE_DIR / "static" / "index.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request, error: str | None = None):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"configured": bool(github_client_id and github_client_secret), "error": error},
        )

    @app.get("/auth/github", include_in_schema=False)
    def github_start(request: Request):
        if not (github_client_id and github_client_secret):
            return RedirectResponse("/login", status_code=303)
        # The state ties GitHub's answer to this browser, so nobody can sign it in as someone else.
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state
        query = urllib.parse.urlencode(
            {
                "client_id": github_client_id,
                "redirect_uri": github_callback,
                "scope": "user:email",
                "state": state,
                "allow_signup": "true" if open_signup else "false",
            }
        )
        return RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{query}", status_code=303)

    @app.get("/auth/github/callback", include_in_schema=False)
    def github_callback_route(request: Request, code: str = "", state: str = "", error: str = ""):
        expected = request.session.pop("oauth_state", None)
        if error:
            return RedirectResponse("/login?error=denied", status_code=303)
        if not expected or not state or not hmac.compare_digest(expected, state) or not code:
            return RedirectResponse("/login?error=expired", status_code=303)
        try:
            github_id, emails = github_identity(github_client_id, github_client_secret, code, github_callback)
        except Exception as err:  # Network trouble or an unexpected answer from GitHub.
            print(f"GitHub sign-in failed: {err!r}", file=sys.stderr)
            return RedirectResponse("/login?error=failed", status_code=303)
        with writing() as conn:
            user = user_for_github(conn, github_id, emails, open_signup)
        if user is None:
            return RedirectResponse("/login?error=noaccess", status_code=303)
        request.session.clear()
        request.session["uid"] = user["id"]
        request.session["epoch"] = user["session_epoch"]
        return RedirectResponse("/", status_code=303)

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- Preferences ---

    @app.get("/api/preferences", tags=["preferences"])
    def get_preferences(uid: UserId) -> Preferences:
        with reading() as conn:
            return read_preferences(conn, uid)

    @app.put("/api/preferences", tags=["preferences"])
    def put_preferences(body: Preferences, uid: UserId) -> Preferences:
        with writing() as conn:
            conn.execute("UPDATE users SET preferences = ? WHERE id = ?", (body.model_dump_json(), uid))
            return read_preferences(conn, uid)

    @app.get("/api/me", tags=["preferences"])
    def me(uid: UserId):
        with reading() as conn:
            return {"email": conn.execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()["email"]}

    # --- Customers and projects ---

    @app.get("/api/tree", tags=["hierarchy"])
    def tree(uid: UserId):
        with reading() as conn:
            return {
                # has_time: logged time blocks deleting, so the page offers archiving instead.
                "customers": rows(
                    conn.execute(
                        """
                        SELECT c.id, c.name, c.archived, c.hourly_rate,
                               EXISTS (SELECT 1 FROM projects p JOIN entries e ON e.project_id = p.id
                                       WHERE p.customer_id = c.id) AS has_time
                        FROM customers c WHERE c.user_id = ? ORDER BY c.name
                        """,
                        (uid,),
                    )
                ),
                "projects": rows(
                    conn.execute(
                        """
                        SELECT p.*, EXISTS (SELECT 1 FROM entries e WHERE e.project_id = p.id) AS has_time
                        FROM projects p JOIN customers c ON c.id = p.customer_id
                        WHERE c.user_id = ? ORDER BY p.name
                        """,
                        (uid,),
                    )
                ),
            }

    @app.post("/api/customers", status_code=201, tags=["hierarchy"])
    def create_customer(body: CustomerCreate, uid: UserId):
        with writing() as conn:
            cur = conn.execute("INSERT INTO customers (user_id, name) VALUES (?, ?)", (uid, body.name))
            return fetch_item(conn, "customers", cur.lastrowid)

    @app.post("/api/projects", status_code=201, tags=["hierarchy"])
    def create_project(body: ProjectCreate, uid: UserId):
        with writing() as conn:
            require_row(conn, "customers", body.customer_id, uid)
            cur = conn.execute(
                "INSERT INTO projects (customer_id, name) VALUES (?, ?)", (body.customer_id, body.name)
            )
            return fetch_item(conn, "projects", cur.lastrowid)

    def register_item_routes(kind):
        def update_item(item_id: int, body: ItemUpdate, uid: UserId):
            if "hourly_rate" in body.model_fields_set and kind != "customers":
                raise ApiError("Only customers have an hourly rate.")
            with writing() as conn:
                require_row(conn, kind, item_id, uid)
                if "hourly_rate" in body.model_fields_set:
                    conn.execute(
                        "UPDATE customers SET hourly_rate = ? WHERE id = ?", (body.hourly_rate, item_id)
                    )
                if body.name is not None:
                    conn.execute(f"UPDATE {kind} SET name = ? WHERE id = ?", (body.name, item_id))
                if body.archived is not None:
                    conn.execute(
                        f"UPDATE {kind} SET archived = ? WHERE id = ?", (int(body.archived), item_id)
                    )
                return fetch_item(conn, kind, item_id)

        def delete_item(item_id: int, uid: UserId):
            with writing() as conn:
                require_row(conn, kind, item_id, uid)
                conn.execute(f"DELETE FROM {kind} WHERE id = ?", (item_id,))

        app.add_api_route(
            f"/api/{kind}/{{item_id}}", update_item, methods=["PATCH"],
            name=f"update_{kind}", tags=["hierarchy"],
        )
        app.add_api_route(
            f"/api/{kind}/{{item_id}}", delete_item, methods=["DELETE"], status_code=204,
            name=f"delete_{kind}", tags=["hierarchy"],
        )

    for kind in ("customers", "projects"):
        register_item_routes(kind)

    # --- Timer ---

    # --- API key and the public API ---

    @app.get("/api/api-key", tags=["api key"])
    def api_key_status(uid: UserId):
        with reading() as conn:
            row = conn.execute("SELECT api_key_hash FROM users WHERE id = ?", (uid,)).fetchone()
        return {"exists": row["api_key_hash"] is not None}

    @app.post("/api/api-key", tags=["api key"])
    def api_key_create(uid: UserId):
        """Makes a new API key, replacing the old one. The key is only ever shown in this answer."""
        key = API_KEY_PREFIX + secrets.token_urlsafe(32)
        with writing() as conn:
            conn.execute("UPDATE users SET api_key_hash = ? WHERE id = ?", (hash_api_key(key), uid))
        return {"key": key}

    @app.delete("/api/api-key", tags=["api key"])
    def api_key_delete(uid: UserId):
        with writing() as conn:
            conn.execute("UPDATE users SET api_key_hash = NULL WHERE id = ?", (uid,))
        return {"exists": False}

    @app.get("/api/v1/timer", tags=["public api"])
    def public_timer(request: Request):
        """The current timer, for scripts and widgets. Send `Authorization: Bearer <API key>`."""
        scheme, _, key = request.headers.get("authorization", "").partition(" ")
        with reading() as conn:
            uid = user_for_api_key(conn, key.strip()) if scheme.lower() == "bearer" else None
            if uid is None:
                return JSONResponse(
                    {"error": "Missing or wrong API key."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return public_timer_payload(conn, uid)

    @app.get("/api/timer", tags=["timer"])
    def timer(uid: UserId):
        with reading() as conn:
            return timer_payload(conn, uid)

    @app.post("/api/timer/start", tags=["timer"])
    def timer_start(body: TimerStart, uid: UserId):
        with writing() as conn:
            require_row(conn, "projects", body.project_id, uid)
            active = conn.execute(
                "SELECT id FROM entries WHERE user_id = ? AND state != 'stopped'", (uid,)
            ).fetchone()
            if active:
                stop_entry(conn, active["id"])
            cur = conn.execute(
                """
                INSERT INTO entries (user_id, project_id, description, billable, state)
                VALUES (?, ?, ?, ?, 'running')
                """,
                (uid, body.project_id, body.description, int(body.billable)),
            )
            conn.execute(
                "INSERT INTO segments (entry_id, start_ts) VALUES (?, ?)", (cur.lastrowid, now_ts())
            )
            return timer_payload(conn, uid)

    @app.post("/api/timer/pause", tags=["timer"])
    def timer_pause(uid: UserId):
        with writing() as conn:
            entry = active_entry(conn, uid, "running")
            close_open_segment(conn, entry["id"])
            conn.execute("UPDATE entries SET state = 'paused' WHERE id = ?", (entry["id"],))
            return timer_payload(conn, uid)

    @app.post("/api/timer/resume", tags=["timer"])
    def timer_resume(uid: UserId):
        with writing() as conn:
            entry = active_entry(conn, uid, "paused")
            conn.execute("UPDATE entries SET state = 'running' WHERE id = ?", (entry["id"],))
            conn.execute(
                "INSERT INTO segments (entry_id, start_ts) VALUES (?, ?)", (entry["id"], now_ts())
            )
            return timer_payload(conn, uid)

    @app.post("/api/timer/stop", tags=["timer"])
    def timer_stop(uid: UserId):
        with writing() as conn:
            entry = active_entry(conn, uid, "running", "paused")
            stop_entry(conn, entry["id"])
            return timer_payload(conn, uid)

    # --- Entries ---

    @app.get("/api/entries", tags=["entries"])
    def list_entries(
        uid: UserId,
        limit: Annotated[int, Query(ge=0, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        with reading() as conn:
            found = conn.execute(
                """
                SELECT e.* FROM entries e
                WHERE e.user_id = ? AND e.state = 'stopped'
                ORDER BY (SELECT MIN(start_ts) FROM segments s WHERE s.entry_id = e.id) DESC, e.id DESC
                LIMIT ? OFFSET ?
                """,
                (uid, limit + 1, offset),
            ).fetchall()
            return {
                "entries": serialize_entries(conn, found[:limit]),
                "has_more": len(found) > limit,
            }

    @app.get("/api/recent", tags=["entries"])
    def recent(uid: UserId, limit: Annotated[int, Query(ge=1, le=500)] = 200):
        """Distinct description and project pairs, most recently used first, for continuing work."""
        with reading() as conn:
            return rows(
                conn.execute(
                    """
                    SELECT e.description, e.project_id, e.billable, MAX(s.start_ts) AS last_used
                    FROM entries e JOIN segments s ON s.entry_id = e.id
                    WHERE e.user_id = ? AND e.description != ''
                    GROUP BY e.description, e.project_id
                    ORDER BY last_used DESC
                    LIMIT ?
                    """,
                    (uid, limit),
                )
            )

    @app.post("/api/entries", status_code=201, tags=["entries"])
    def create_entry(body: EntryWrite, uid: UserId):
        with writing() as conn:
            require_row(conn, "projects", body.project_id, uid)
            segments = clean_segments(body.segments, "stopped")
            cur = conn.execute(
                "INSERT INTO entries (user_id, project_id, description, billable) VALUES (?, ?, ?, ?)",
                (uid, body.project_id, body.description, int(body.billable)),
            )
            replace_segments(conn, cur.lastrowid, segments)
            return fetch_entry(conn, cur.lastrowid)

    @app.put("/api/entries/{entry_id}", tags=["entries"])
    def update_entry(entry_id: int, body: EntryWrite, uid: UserId):
        with writing() as conn:
            require_row(conn, "entries", entry_id, uid)
            state = conn.execute("SELECT state FROM entries WHERE id = ?", (entry_id,)).fetchone()["state"]
            require_row(conn, "projects", body.project_id, uid)
            segments = clean_segments(body.segments, state)
            conn.execute(
                "UPDATE entries SET project_id = ?, description = ?, billable = ? WHERE id = ?",
                (body.project_id, body.description, int(body.billable), entry_id),
            )
            replace_segments(conn, entry_id, segments)
            return fetch_entry(conn, entry_id)

    @app.delete("/api/entries/{entry_id}", status_code=204, tags=["entries"])
    def delete_entry(entry_id: int, uid: UserId):
        with writing() as conn:
            require_row(conn, "entries", entry_id, uid)
            conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    # --- Reports ---

    @app.get("/api/report", tags=["reports"])
    def report(query: Annotated[ReportQuery, Query()], uid: UserId):
        tz = parse_tz(query.tz)
        levels = parse_groups(query.group)
        now = now_ts()
        total = new_node("")
        with reading() as conn:
            prefs = read_preferences(conn, uid)
            found = report_rows(conn, uid, query, tz)
        for row, billable in zip(found, billable_amounts(found, prefs, levels, tz, now)):
            add_to_tree(total, row, levels, tz, now, billable, amount_for(row, billable, prefs))
        return {
            "total": {
                "seconds": total["seconds"],
                "billable_seconds": total["billable_seconds"],
                "amount": round(total["amount"], 2),
            },
            "groups": finish_children(total),
        }

    @app.post("/api/import/clockify", tags=["preferences"])
    def import_clockify(body: ClockifyImport, uid: UserId):
        """Imports the signed-in user's Clockify time entries; with dry_run, only counts them."""
        try:
            workspace, clients, entries = fetch_clockify(body.api_key, body.region)
        except ClockifyError as err:
            raise ApiError(str(err), 502) from None
        rows, running, invalid = clockify_rows(entries, clients)
        # A dry run writes nothing, so it only needs to read.
        with (reading() if body.dry_run else writing()) as conn:
            result = import_clockify_rows(conn, uid, rows, body.dry_run)
        starts = [row["start"] for row in rows]
        return {
            "workspace": workspace,
            "found": len(entries),
            "running": running,
            "invalid": invalid,
            "first": min(starts) if starts else None,
            "last": max(starts) if starts else None,
            "dry_run": body.dry_run,
            **result,
        }

    @app.get("/api/export.sqlite", tags=["preferences"])
    def export_database(uid: UserId):
        """The signed-in user's data as a SQLite database for running Timelog on another server."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "export.db"
            export_user_database(db_path, uid, path)
            content = path.read_bytes()
        filename = f"timelog-{date.today().isoformat()}.db"
        return Response(
            content,
            media_type="application/vnd.sqlite3",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/report.csv", tags=["reports"])
    def report_csv(query: Annotated[CsvQuery, Query()], uid: UserId):
        tz = parse_tz(query.tz)
        levels = parse_groups(query.group)
        with reading() as conn:
            prefs = read_preferences(conn, uid)
            found = report_rows(conn, uid, query, tz)
        text = render_csv(found, query.mode, query.dialect, levels, tz, prefs)
        return Response(
            "﻿" + text,  # BOM so Excel reads the file as UTF-8.
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="timelog-{query.mode}.csv"'},
        )

    return app


# --- Data helpers ---


def rows(cursor):
    return [dict(row) for row in cursor.fetchall()]


def now_ts():
    return int(time.time())


def fetch_item(conn, kind, item_id):
    item = dict(conn.execute(f"SELECT * FROM {kind} WHERE id = ?", (item_id,)).fetchone())
    item.pop("user_id", None)
    return item


def fetch_entry(conn, entry_id):
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return serialize_entries(conn, [row])[0]


# Finds a row only if it belongs to the user; other users' rows look like they do not exist.
OWNED_ROW = {
    "customers": "SELECT id FROM customers WHERE id = ? AND user_id = ?",
    "projects": """
        SELECT p.id FROM projects p JOIN customers c ON c.id = p.customer_id
        WHERE p.id = ? AND c.user_id = ?
    """,
    "entries": "SELECT id FROM entries WHERE id = ? AND user_id = ?",
}


def require_row(conn, table, row_id, user_id):
    if conn.execute(OWNED_ROW[table], (row_id, user_id)).fetchone() is None:
        raise ApiError(f"Unknown {table[:-1]}.", 404 if table == "entries" else 400)


def timer_payload(conn, user_id):
    row = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? AND state != 'stopped'", (user_id,)
    ).fetchone()
    return {"active": serialize_entries(conn, [row])[0] if row else None, "now": now_ts()}


def iso_utc(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def public_timer_payload(conn, user_id):
    """The user's running or paused entry with its project and customer names."""
    now = now_ts()
    row = conn.execute(
        """
        SELECT e.id, e.description, e.billable, e.state, p.name AS project, c.name AS customer
        FROM entries e JOIN projects p ON p.id = e.project_id JOIN customers c ON c.id = p.customer_id
        WHERE e.user_id = ? AND e.state != 'stopped'
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return {"running": False, "entry": None, "now": iso_utc(now)}
    segments = conn.execute(
        "SELECT start_ts, end_ts FROM segments WHERE entry_id = ? ORDER BY start_ts", (row["id"],)
    ).fetchall()
    elapsed = sum((seg["end_ts"] or now) - seg["start_ts"] for seg in segments)
    open_segment = next((seg for seg in segments if seg["end_ts"] is None), None)
    hours, rest = divmod(max(elapsed, 0), 3600)
    return {
        "running": row["state"] == "running",
        "entry": {
            "state": row["state"],
            "description": row["description"],
            "project": row["project"],
            "customer": row["customer"],
            "billable": bool(row["billable"]),
            "started_at": iso_utc(segments[0]["start_ts"]) if segments else None,
            "running_since": iso_utc(open_segment["start_ts"]) if open_segment else None,
            "elapsed_seconds": elapsed,
            "elapsed": f"{hours}:{rest // 60:02d}:{rest % 60:02d}",
        },
        "now": iso_utc(now),
    }


def active_entry(conn, user_id, *states):
    row = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? AND state != 'stopped'", (user_id,)
    ).fetchone()
    if row is None or row["state"] not in states:
        raise ApiError("The timer has changed. Reloaded the latest state.", 409)
    return row


def close_open_segment(conn, entry_id):
    now = now_ts()
    # A segment shorter than a second has no duration, so it is dropped.
    conn.execute(
        "DELETE FROM segments WHERE entry_id = ? AND end_ts IS NULL AND start_ts >= ?",
        (entry_id, now),
    )
    conn.execute(
        "UPDATE segments SET end_ts = ? WHERE entry_id = ? AND end_ts IS NULL", (now, entry_id)
    )


def stop_entry(conn, entry_id):
    close_open_segment(conn, entry_id)
    count = conn.execute("SELECT COUNT(*) FROM segments WHERE entry_id = ?", (entry_id,)).fetchone()[0]
    if count == 0:
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    else:
        conn.execute("UPDATE entries SET state = 'stopped' WHERE id = ?", (entry_id,))


def replace_segments(conn, entry_id, segments):
    conn.execute("DELETE FROM segments WHERE entry_id = ?", (entry_id,))
    conn.executemany(
        "INSERT INTO segments (entry_id, start_ts, end_ts) VALUES (?, ?, ?)",
        [(entry_id, start, end) for start, end in segments],
    )


def clean_segments(value, state):
    """Applies the rules for an entry in the given state; returns sorted tuples."""
    if not value and state != "paused":
        raise ApiError("Add at least one time period.")
    for item in value:
        if item.end is not None and item.end <= item.start:
            raise ApiError("A time period must end after it starts.")
    segments = sorted(
        ((item.start, item.end) for item in value),
        key=lambda seg: (seg[0], seg[1] is None),
    )

    open_count = sum(1 for _, end in segments if end is None)
    if state == "running":
        if open_count != 1 or segments[-1][1] is not None:
            raise ApiError("A running entry's latest period must have no end.")
    elif open_count:
        raise ApiError("Every time period needs an end.")
    for (_, prev_end), (next_start, _) in zip(segments, segments[1:]):
        if prev_end is None or prev_end > next_start:
            raise ApiError("Time periods in an entry cannot overlap.")
    if state != "stopped":
        now = now_ts()
        if any(start > now or (end is not None and end > now) for start, end in segments):
            raise ApiError("A running or paused entry cannot have times in the future.")
    return segments


def serialize_entries(conn, entry_rows):
    if not entry_rows:
        return []
    ids = [row["id"] for row in entry_rows]
    marks = ",".join("?" * len(ids))
    by_entry = {entry_id: [] for entry_id in ids}
    for seg in conn.execute(
        f"SELECT entry_id, start_ts, end_ts FROM segments WHERE entry_id IN ({marks}) ORDER BY start_ts",
        ids,
    ):
        by_entry[seg["entry_id"]].append({"start": seg["start_ts"], "end": seg["end_ts"]})
    return [
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "description": row["description"],
            "billable": bool(row["billable"]),
            "state": row["state"],
            "segments": by_entry[row["id"]],
        }
        for row in entry_rows
    ]


# --- Reports ---


def parse_tz(name):
    try:
        return ZoneInfo(name)
    except Exception:
        raise ApiError("Unknown time zone.") from None


def parse_groups(raw):
    levels = [level for level in raw.split(",") if level]
    if len(levels) > MAX_GROUP_LEVELS or len(set(levels)) != len(levels) or not set(levels) <= GROUPS:
        raise ApiError("Invalid grouping.")
    return levels


def day_start(day, tz):
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())


def report_rows(conn, user_id, query, tz):
    where, params = ["e.user_id = ?"], [user_id]
    if query.start_date:
        where.append("s.start_ts >= ?")
        params.append(day_start(query.start_date, tz))
    if query.to:
        where.append("s.start_ts < ?")
        params.append(day_start(query.to + timedelta(days=1), tz))
    for value, column in (
        (query.customer_id, "c.id"),
        (query.project_id, "p.id"),
    ):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    if query.billable:
        where.append("e.billable = ?")
        params.append(int(query.billable))
    if not query.include_archived:
        where.append("c.archived = 0 AND p.archived = 0")
    search = query.q.strip()
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("e.description LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    clause = f"WHERE {' AND '.join(where)}"
    return conn.execute(
        f"""
        SELECT s.start_ts, s.end_ts, e.id AS entry_id, e.description, e.billable,
               p.id AS project_id, p.name AS project_name,
               c.id AS customer_id, c.name AS customer_name, c.hourly_rate AS customer_rate
        FROM segments s
        JOIN entries e ON e.id = s.entry_id
        JOIN projects p ON p.id = e.project_id
        JOIN customers c ON c.id = p.customer_id
        {clause}
        ORDER BY s.start_ts
        """,
        params,
    ).fetchall()


def render_csv(found, mode, dialect, levels, tz, prefs):
    def number(value):
        text = f"{value:.2f}"
        return text.replace(".", ",") if dialect == "excel" else text

    def hours(seconds):
        return number(seconds / 3600)

    now = now_ts()
    billables = billable_amounts(found, prefs, levels, tz, now)
    buffer = io.StringIO()
    out = csv.writer(buffer, delimiter=";" if dialect == "excel" else ",")
    if mode == "detail":
        out.writerow(
            ["Date", "Start", "End", "Customer", "Project", "Description", "Billable", "Hours", "Billable hours", "Amount"]
        )
        for row, billable in zip(found, billables):
            start = datetime.fromtimestamp(row["start_ts"], tz)
            end = datetime.fromtimestamp(row["end_ts"], tz) if row["end_ts"] else None
            out.writerow(
                [
                    start.date().isoformat(),
                    start.strftime("%H:%M:%S"),
                    end.strftime("%H:%M:%S") if end else "",
                    row["customer_name"],
                    row["project_name"],
                    row["description"],
                    "Yes" if row["billable"] else "No",
                    hours(row_seconds(row, now)),
                    hours(billable),
                    number(amount_for(row, billable, prefs)),
                ]
            )
        return buffer.getvalue()

    total = new_node("")
    for row, billable in zip(found, billables):
        add_to_tree(total, row, levels, tz, now, billable, amount_for(row, billable, prefs))
    out.writerow([level.capitalize() for level in levels] + ["Hours", "Billable hours", "Amount"])

    def figures(node):
        return [hours(node["seconds"]), hours(node["billable_seconds"]), number(node["amount"])]

    def walk(nodes, path):
        for node in nodes:
            if node["children"]:
                walk(node["children"], path + [node["label"]])
            else:
                out.writerow(path + [node["label"]] + figures(node))

    walk(finish_children(total), [])
    out.writerow(["Total"] + [""] * max(len(levels) - 1, 0) + figures(total))
    return buffer.getvalue()


def row_seconds(row, now):
    end = row["end_ts"] if row["end_ts"] is not None else now
    return max(end - row["start_ts"], 0)


def billable_amounts(found, prefs, levels, tz, now):
    """Billable seconds per report row, after rounding each billed unit up to a started step.

    A unit is an entry, one day's work on a project with one description, or a project's whole
    time in the report, and it is also split by the report's groups: a project grouped by month
    is rounded once per month. So every group is a whole number of steps. A unit's rounding is
    added to its first row, so groups and totals add up to the rounded amounts."""
    amounts = [row_seconds(row, now) if row["billable"] else 0 for row in found]
    step = prefs.round_minutes * 60
    if not step:
        return amounts
    units = {}
    for index, row in enumerate(found):
        if not row["billable"]:
            continue
        if prefs.round_scope == "entry":
            key = row["entry_id"]
        elif prefs.round_scope == "project":
            key = row["project_id"]
        else:
            day = datetime.fromtimestamp(row["start_ts"], tz).date()
            key = (day, row["project_id"], row["description"])
        groups = tuple(group_key(level, levels[:i], row, tz)[0] for i, level in enumerate(levels))
        units.setdefault((key, groups), []).append(index)
    for indexes in units.values():
        raw = sum(amounts[i] for i in indexes)
        amounts[indexes[0]] += -(-raw // step) * step - raw
    return amounts


def amount_for(row, billable_seconds, prefs):
    rate = row["customer_rate"] if row["customer_rate"] is not None else prefs.default_rate
    return billable_seconds * rate / 3600


def new_node(label, sort_key=""):
    return {
        "label": label,
        "sort_key": sort_key,
        "seconds": 0,
        "billable_seconds": 0,
        "amount": 0.0,
        "children": {},
    }


def group_key(level, previous, row, tz):
    """Returns (identity, label, sort key) for a segment at one grouping level."""
    if level in TIME_GROUPS:
        day = datetime.fromtimestamp(row["start_ts"], tz).date()
        if level == "day":
            key = day.isoformat()
        elif level == "week":
            iso = day.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        else:
            key = day.strftime("%Y-%m")
        return key, key, key
    if level == "description":
        label = row["description"] or "(no description)"
        return row["description"], label, label.casefold()
    # Same-named projects are told apart unless the customer level already does it.
    if level == "customer":
        path = [row["customer_name"]]
    else:
        path = [row["project_name"]]
        if "customer" not in previous:
            path.insert(0, row["customer_name"])
    label = " / ".join(path)
    return row[f"{level}_id"], label, label.casefold()


def add_to_tree(root, row, levels, tz, now, billable, amount):
    seconds = row_seconds(row, now)
    root["seconds"] += seconds
    root["billable_seconds"] += billable
    root["amount"] += amount
    node = root
    for index, level in enumerate(levels):
        key, label, sort_key = group_key(level, levels[:index], row, tz)
        node = node["children"].setdefault(key, new_node(label, sort_key))
        node["seconds"] += seconds
        node["billable_seconds"] += billable
        node["amount"] += amount


def finish_children(node):
    children = sorted(node["children"].values(), key=lambda child: child["sort_key"])
    return [
        {
            "label": child["label"],
            "seconds": child["seconds"],
            "billable_seconds": child["billable_seconds"],
            "amount": round(child["amount"], 2),
            "children": finish_children(child),
        }
        for child in children
    ]


# --- Command line ---


USAGE = """Usage: python app.py <command>
  add-user <email>          let this email address sign in (with a GitHub account that has it verified)
  list-users
  set-email <email> <new>   change a user's email; their GitHub link stays
  disable-user <email>      block signing in and end the user's sessions
  enable-user <email>
  logout-user <email>       end the user's sessions everywhere
  unlink-github <email>     let the user sign in with a different GitHub account
  backup <destination-file> online SQLite backup, safe while the app runs"""


def cli(argv):
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    arity = {
        "add-user": 1, "list-users": 0, "set-email": 2, "disable-user": 1, "enable-user": 1,
        "logout-user": 1, "unlink-github": 1, "backup": 1,
    }
    if arity.get(command) != len(args):
        sys.exit(USAGE)
    conn = init_db(db_path_from_env())
    try:
        if command == "backup":
            destination = sqlite3.connect(args[0])
            with destination:
                conn.backup(destination)
            destination.close()
            print(f"Backed up to {args[0]}")
        elif command == "list-users":
            for user in conn.execute("SELECT * FROM users ORDER BY email"):
                flags = [] if user["github_id"] is None else [f"GitHub {user['github_id']}"]
                if user["disabled"]:
                    flags.append("disabled")
                print(user["email"] + (f" ({', '.join(flags)})" if flags else ""))
        elif command == "add-user":
            add_user(conn, args[0])
            print(f"Added {args[0].strip()}. They can sign in with a GitHub account that has this email verified.")
        else:
            user = require_user(conn, args[0])
            if command == "set-email":
                new = normalize_email(args[1])
                conn.execute("UPDATE users SET email = ? WHERE id = ?", (new, user["id"]))
                print(f"{user['email']} is now {new}.")
            elif command == "unlink-github":
                conn.execute("UPDATE users SET github_id = NULL WHERE id = ?", (user["id"],))
                print(f"{user['email']} can now sign in with any GitHub account that has it verified.")
            else:
                disabled = {"disable-user": 1, "enable-user": 0}.get(command, user["disabled"])
                conn.execute(
                    "UPDATE users SET disabled = ?, session_epoch = session_epoch + ? WHERE id = ?",
                    (disabled, int(command != "enable-user"), user["id"]),
                )
                done = {"disable-user": "disabled and logged out", "enable-user": "enabled", "logout-user": "logged out everywhere"}
                print(f"{user['email']} is {done[command]}.")
    except (ValueError, sqlite3.IntegrityError) as err:
        sys.exit(str(err))
    finally:
        conn.close()


if __name__ == "__main__":
    cli(sys.argv[1:])
