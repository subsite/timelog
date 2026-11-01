"""Timelog: a single-user time tracker (Customer > Project, entries described in free text)."""
import csv
import getpass
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    archived INTEGER NOT NULL DEFAULT 0,
    -- NULL means the default hourly rate from preferences.
    hourly_rate REAL CHECK (hourly_rate IS NULL OR hourly_rate >= 0)
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    archived INTEGER NOT NULL DEFAULT 0,
    UNIQUE (customer_id, name)
);
{entries}
-- Only one entry can be running or paused at a time.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_entry ON entries (state != 'stopped')
    WHERE state != 'stopped';
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

# The description is what was worked on; there is no task level below projects.
ENTRIES_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    description TEXT NOT NULL DEFAULT '',
    billable INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'stopped' CHECK (state IN ('running', 'paused', 'stopped'))
);
"""
SCHEMA = SCHEMA.replace("{entries}", ENTRIES_TABLE.format(name="entries"))

TIME_GROUPS = {"day", "week", "month"}
GROUPS = TIME_GROUPS | {"customer", "project", "description"}
MIN_TS = 946684800  # 2000-01-01
MAX_TS = 4102444800  # 2100-01-01
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW = 15 * 60
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


class ReportQuery(BaseModel):
    tz: str = "UTC"
    start_date: date | None = Field(default=None, alias="from")
    to: date | None = None
    customer_id: int | None = None
    project_id: int | None = None
    billable: Literal["", "0", "1"] = ""
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
    migrate_tasks_into_descriptions(conn)
    conn.executescript(SCHEMA)
    add_customer_rate_column(conn)
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
        conn.execute(ENTRIES_TABLE.format(name="entries_new"))
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


def add_customer_rate_column(conn):
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(customers)")]
    if "hourly_rate" not in columns:
        conn.execute(
            "ALTER TABLE customers ADD COLUMN hourly_rate REAL "
            "CHECK (hourly_rate IS NULL OR hourly_rate >= 0)"
        )


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def read_preferences(conn):
    stored = get_setting(conn, "preferences")
    try:
        return Preferences.model_validate(json.loads(stored)) if stored else Preferences()
    except ValueError:
        return Preferences()


def db_path_from_env():
    return os.environ.get("TIMELOG_DB", str(BASE_DIR / "data" / "timelog.db"))


# --- Passwords ---


def scrypt(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


def set_password(conn, password):
    salt = secrets.token_bytes(16)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('password_hash', ?)",
        (f"{salt.hex()}${scrypt(password, salt).hex()}",),
    )


def check_password(password_hash, password):
    salt, digest = password_hash.split("$")
    return hmac.compare_digest(scrypt(password, bytes.fromhex(salt)).hex(), digest)


def password_fingerprint(password_hash):
    # Stored in the session, so changing the password logs out every session.
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


# --- Application ---


def create_app(db_path=None):
    db_path = db_path or db_path_from_env()
    conn = init_db(db_path)
    secret_key = get_setting(conn, "secret_key")
    conn.close()

    behind_proxy = os.environ.get("TIMELOG_BEHIND_PROXY") == "1"
    login_failures = {}
    login_lock = threading.Lock()
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

    def read_password_hash():
        with reading() as conn:
            return get_setting(conn, "password_hash")

    def client_ip(request):
        # The proxy appends the address it saw, so earlier values can be forged by the client.
        forwarded = request.headers.get("x-forwarded-for")
        if behind_proxy and forwarded:
            return forwarded.split(",")[-1].strip()
        return request.client.host if request.client else "unknown"

    app = FastAPI(title="Timelog", docs_url="/docs", redoc_url=None)

    @app.middleware("http")
    async def protect(request, call_next):
        path = request.url.path
        if path == "/login" or path.startswith("/static/"):
            response = await call_next(request)
        else:
            password_hash = await run_in_threadpool(read_password_hash)
            authed = password_hash and hmac.compare_digest(
                request.session.get("pw", ""), password_fingerprint(password_hash)
            )
            if authed:
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
            {"configured": read_password_hash() is not None, "error": error},
        )

    @app.post("/login", include_in_schema=False)
    def login(request: Request, password: Annotated[str, Form()] = ""):
        ip = client_ip(request)
        now = time.time()
        with login_lock:
            for key in [k for k, v in login_failures.items() if v[-1] < now - LOGIN_WINDOW]:
                del login_failures[key]
            failures = [t for t in login_failures.get(ip, []) if t >= now - LOGIN_WINDOW]
            if len(failures) >= MAX_LOGIN_FAILURES:
                return RedirectResponse("/login?error=locked", status_code=303)

        password_hash = read_password_hash()
        if not password_hash or not check_password(password_hash, password):
            with login_lock:
                login_failures[ip] = failures + [now]
            return RedirectResponse("/login?error=invalid", status_code=303)

        with login_lock:
            login_failures.pop(ip, None)
        request.session.clear()
        request.session["pw"] = password_fingerprint(password_hash)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout", include_in_schema=False)
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # --- Preferences ---

    @app.get("/api/preferences", tags=["preferences"])
    def get_preferences() -> Preferences:
        with reading() as conn:
            return read_preferences(conn)

    @app.put("/api/preferences", tags=["preferences"])
    def put_preferences(body: Preferences) -> Preferences:
        with writing() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('preferences', ?)",
                (body.model_dump_json(),),
            )
            return read_preferences(conn)

    # --- Customers and projects ---

    @app.get("/api/tree", tags=["hierarchy"])
    def tree():
        with reading() as conn:
            return {
                kind: rows(conn.execute(f"SELECT * FROM {kind} ORDER BY name"))
                for kind in ("customers", "projects")
            }

    def insert_item(kind, columns, values):
        with writing() as conn:
            marks = ", ".join("?" * len(values))
            cur = conn.execute(f"INSERT INTO {kind} ({columns}) VALUES ({marks})", values)
            return fetch_item(conn, kind, cur.lastrowid)

    @app.post("/api/customers", status_code=201, tags=["hierarchy"])
    def create_customer(body: CustomerCreate):
        return insert_item("customers", "name", (body.name,))

    @app.post("/api/projects", status_code=201, tags=["hierarchy"])
    def create_project(body: ProjectCreate):
        with reading() as conn:
            require_row(conn, "customers", body.customer_id)
        return insert_item("projects", "customer_id, name", (body.customer_id, body.name))

    def register_item_routes(kind):
        def update_item(item_id: int, body: ItemUpdate):
            if "hourly_rate" in body.model_fields_set and kind != "customers":
                raise ApiError("Only customers have an hourly rate.")
            with writing() as conn:
                require_row(conn, kind, item_id)
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

        def delete_item(item_id: int):
            with writing() as conn:
                require_row(conn, kind, item_id)
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

    @app.get("/api/timer", tags=["timer"])
    def timer():
        with reading() as conn:
            return timer_payload(conn)

    @app.post("/api/timer/start", tags=["timer"])
    def timer_start(body: TimerStart):
        with writing() as conn:
            require_row(conn, "projects", body.project_id)
            active = conn.execute("SELECT id FROM entries WHERE state != 'stopped'").fetchone()
            if active:
                stop_entry(conn, active["id"])
            cur = conn.execute(
                "INSERT INTO entries (project_id, description, billable, state) VALUES (?, ?, ?, 'running')",
                (body.project_id, body.description, int(body.billable)),
            )
            conn.execute(
                "INSERT INTO segments (entry_id, start_ts) VALUES (?, ?)", (cur.lastrowid, now_ts())
            )
            return timer_payload(conn)

    @app.post("/api/timer/pause", tags=["timer"])
    def timer_pause():
        with writing() as conn:
            entry = active_entry(conn, "running")
            close_open_segment(conn, entry["id"])
            conn.execute("UPDATE entries SET state = 'paused' WHERE id = ?", (entry["id"],))
            return timer_payload(conn)

    @app.post("/api/timer/resume", tags=["timer"])
    def timer_resume():
        with writing() as conn:
            entry = active_entry(conn, "paused")
            conn.execute("UPDATE entries SET state = 'running' WHERE id = ?", (entry["id"],))
            conn.execute(
                "INSERT INTO segments (entry_id, start_ts) VALUES (?, ?)", (entry["id"], now_ts())
            )
            return timer_payload(conn)

    @app.post("/api/timer/stop", tags=["timer"])
    def timer_stop():
        with writing() as conn:
            entry = active_entry(conn, "running", "paused")
            stop_entry(conn, entry["id"])
            return timer_payload(conn)

    # --- Entries ---

    @app.get("/api/entries", tags=["entries"])
    def list_entries(
        limit: Annotated[int, Query(ge=0, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        with reading() as conn:
            found = conn.execute(
                """
                SELECT e.* FROM entries e
                WHERE e.state = 'stopped'
                ORDER BY (SELECT MIN(start_ts) FROM segments s WHERE s.entry_id = e.id) DESC, e.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit + 1, offset),
            ).fetchall()
            return {
                "entries": serialize_entries(conn, found[:limit]),
                "has_more": len(found) > limit,
            }

    @app.get("/api/recent", tags=["entries"])
    def recent(limit: Annotated[int, Query(ge=1, le=500)] = 200):
        """Distinct description and project pairs, most recently used first, for continuing work."""
        with reading() as conn:
            return rows(
                conn.execute(
                    """
                    SELECT e.description, e.project_id, e.billable, MAX(s.start_ts) AS last_used
                    FROM entries e JOIN segments s ON s.entry_id = e.id
                    WHERE e.description != ''
                    GROUP BY e.description, e.project_id
                    ORDER BY last_used DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    @app.post("/api/entries", status_code=201, tags=["entries"])
    def create_entry(body: EntryWrite):
        with writing() as conn:
            require_row(conn, "projects", body.project_id)
            segments = clean_segments(body.segments, "stopped")
            cur = conn.execute(
                "INSERT INTO entries (project_id, description, billable) VALUES (?, ?, ?)",
                (body.project_id, body.description, int(body.billable)),
            )
            replace_segments(conn, cur.lastrowid, segments)
            return fetch_entry(conn, cur.lastrowid)

    @app.put("/api/entries/{entry_id}", tags=["entries"])
    def update_entry(entry_id: int, body: EntryWrite):
        with writing() as conn:
            require_row(conn, "entries", entry_id)
            state = conn.execute("SELECT state FROM entries WHERE id = ?", (entry_id,)).fetchone()["state"]
            require_row(conn, "projects", body.project_id)
            segments = clean_segments(body.segments, state)
            conn.execute(
                "UPDATE entries SET project_id = ?, description = ?, billable = ? WHERE id = ?",
                (body.project_id, body.description, int(body.billable), entry_id),
            )
            replace_segments(conn, entry_id, segments)
            return fetch_entry(conn, entry_id)

    @app.delete("/api/entries/{entry_id}", status_code=204, tags=["entries"])
    def delete_entry(entry_id: int):
        with writing() as conn:
            require_row(conn, "entries", entry_id)
            conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    # --- Reports ---

    @app.get("/api/report", tags=["reports"])
    def report(query: Annotated[ReportQuery, Query()]):
        tz = parse_tz(query.tz)
        levels = parse_groups(query.group)
        now = now_ts()
        total = new_node("")
        with reading() as conn:
            prefs = read_preferences(conn)
            found = report_rows(conn, query, tz)
        for row, billable in zip(found, billable_amounts(found, prefs, tz, now)):
            add_to_tree(total, row, levels, tz, now, billable, amount_for(row, billable, prefs))
        return {
            "total": {
                "seconds": total["seconds"],
                "billable_seconds": total["billable_seconds"],
                "amount": round(total["amount"], 2),
            },
            "groups": finish_children(total),
        }

    @app.get("/api/report.csv", tags=["reports"])
    def report_csv(query: Annotated[CsvQuery, Query()]):
        tz = parse_tz(query.tz)
        levels = parse_groups(query.group)
        with reading() as conn:
            prefs = read_preferences(conn)
            found = report_rows(conn, query, tz)
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
    return dict(conn.execute(f"SELECT * FROM {kind} WHERE id = ?", (item_id,)).fetchone())


def fetch_entry(conn, entry_id):
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return serialize_entries(conn, [row])[0]


def require_row(conn, table, row_id):
    if conn.execute(f"SELECT id FROM {table} WHERE id = ?", (row_id,)).fetchone() is None:
        raise ApiError(f"Unknown {table[:-1]}.", 404 if table == "entries" else 400)


def timer_payload(conn):
    row = conn.execute("SELECT * FROM entries WHERE state != 'stopped'").fetchone()
    return {"active": serialize_entries(conn, [row])[0] if row else None, "now": now_ts()}


def active_entry(conn, *states):
    row = conn.execute("SELECT * FROM entries WHERE state != 'stopped'").fetchone()
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
    if len(levels) > 3 or len(set(levels)) != len(levels) or not set(levels) <= GROUPS:
        raise ApiError("Invalid grouping.")
    return levels


def day_start(day, tz):
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())


def report_rows(conn, query, tz):
    where, params = [], []
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
    search = query.q.strip()
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("e.description LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
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
    billables = billable_amounts(found, prefs, tz, now)
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


def billable_amounts(found, prefs, tz, now):
    """Billable seconds per report row, after rounding each billed unit up to a started step.

    A unit is an entry, one day's work on a project with one description, or a project's whole
    time in the report. Its rounding is added to its first row, so groups and totals add up to
    the rounded amounts."""
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
        units.setdefault(key, []).append(index)
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


def cli(argv):
    usage = "Usage: python app.py set-password | python app.py backup <destination-file>"
    if len(argv) == 1 and argv[0] == "set-password":
        password = getpass.getpass("New password: ")
        if len(password) < 8:
            sys.exit("Use at least 8 characters.")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("The passwords do not match.")
        conn = init_db(db_path_from_env())
        set_password(conn, password)
        conn.close()
        print("Password set. Existing sessions are logged out.")
    elif len(argv) == 2 and argv[0] == "backup":
        source = init_db(db_path_from_env())
        destination = sqlite3.connect(argv[1])
        with destination:
            source.backup(destination)
        destination.close()
        source.close()
        print(f"Backed up to {argv[1]}")
    else:
        sys.exit(usage)


if __name__ == "__main__":
    cli(sys.argv[1:])
