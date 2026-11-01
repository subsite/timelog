import csv
import io
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as timelog

HELSINKI = ZoneInfo("Europe/Helsinki")


def ts(year, month, day, hour=0, minute=0, tz=HELSINKI):
    return int(datetime(year, month, day, hour, minute, tzinfo=tz).timestamp())


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def clock(monkeypatch):
    fake = Clock(ts(2026, 10, 15, 9))
    monkeypatch.setattr(timelog, "now_ts", fake)
    return fake


@pytest.fixture
def anon(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMELOG_INSECURE_COOKIES", "1")
    path = tmp_path / "test.db"
    application = timelog.create_app(str(path))
    conn = timelog.connect(str(path))
    timelog.set_password(conn, "correct horse")
    conn.close()
    return TestClient(application, follow_redirects=False)


@pytest.fixture
def client(anon, clock):
    response = anon.post("/login", data={"password": "correct horse"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    return anon


def add_project(client, customer="Acme", project="Website"):
    tree = client.get("/api/tree").json()
    found = [c for c in tree["customers"] if c["name"] == customer]
    customer_id = found[0]["id"] if found else client.post("/api/customers", json={"name": customer}).json()["id"]
    response = client.post("/api/projects", json={"name": project, "customer_id": customer_id})
    assert response.status_code == 201, response.json()
    return response.json()["id"]


def add_entry(client, project_id, *segments, description="", billable=True):
    response = client.post(
        "/api/entries",
        json={
            "project_id": project_id,
            "description": description,
            "billable": billable,
            "segments": [{"start": s, "end": e} for s, e in segments],
        },
    )
    assert response.status_code == 201, response.json()
    return response.json()


# --- Auth ---


def test_requires_login(anon):
    assert anon.get("/api/tree").status_code == 401
    assert anon.post("/api/customers", json={"name": "X"}).status_code == 401
    response = anon.get("/")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_login_is_rate_limited(anon):
    for _ in range(timelog.MAX_LOGIN_FAILURES):
        assert anon.post("/login", data={"password": "nope"}).headers["location"] == "/login?error=invalid"
    # Even the right password is refused once locked.
    assert anon.post("/login", data={"password": "correct horse"}).headers["location"] == "/login?error=locked"


def test_forged_forwarded_address_cannot_dodge_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("TIMELOG_INSECURE_COOKIES", "1")
    monkeypatch.setenv("TIMELOG_BEHIND_PROXY", "1")
    path = tmp_path / "proxy.db"
    client = TestClient(timelog.create_app(str(path)), follow_redirects=False)
    conn = timelog.connect(str(path))
    timelog.set_password(conn, "correct horse")
    conn.close()

    def attempt(forwarded):
        response = client.post(
            "/login", data={"password": "nope"}, headers={"X-Forwarded-For": forwarded}
        )
        return response.headers["location"]

    for number in range(timelog.MAX_LOGIN_FAILURES):
        assert attempt(f"10.0.0.{number}, 203.0.113.9") == "/login?error=invalid"
    assert attempt("10.9.9.9, 203.0.113.9") == "/login?error=locked"
    # A different real client address is unaffected.
    assert attempt("203.0.113.10") == "/login?error=invalid"


def test_docs_and_schema_require_login(anon):
    assert anon.get("/docs").headers["location"] == "/login"
    assert anon.get("/openapi.json").headers["location"] == "/login"


def test_static_assets_and_login_page_are_public(anon):
    assert anon.get("/static/app.js").status_code == 200
    assert anon.get("/login").status_code == 200
    assert "Content-Security-Policy" in anon.get("/login").headers


def test_no_password_means_no_login(tmp_path):
    application = timelog.create_app(str(tmp_path / "fresh.db"))
    client = TestClient(application, follow_redirects=False)
    assert client.post("/login", data={"password": ""}).headers["location"] == "/login?error=invalid"
    assert client.get("/api/tree").status_code == 401


def test_changing_password_logs_out_sessions(client, tmp_path):
    conn = timelog.connect(str(tmp_path / "test.db"))
    timelog.set_password(conn, "another password")
    conn.close()
    assert client.get("/api/tree").status_code == 401


def test_session_cookie_is_secure_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TIMELOG_INSECURE_COOKIES", raising=False)
    client = TestClient(timelog.create_app(str(tmp_path / "secure.db")), follow_redirects=False)
    conn = timelog.connect(str(tmp_path / "secure.db"))
    timelog.set_password(conn, "correct horse")
    conn.close()
    cookie = client.post("/login", data={"password": "correct horse"}).headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


# --- Hierarchy ---


def test_duplicate_names_are_rejected_within_a_parent(client):
    add_project(client)
    customer = client.get("/api/tree").json()["customers"][0]
    assert client.post("/api/customers", json={"name": "acme"}).status_code == 409
    assert client.post("/api/projects", json={"name": "WEBSITE", "customer_id": customer["id"]}).status_code == 409
    other = client.post("/api/customers", json={"name": "Other"}).json()
    assert client.post("/api/projects", json={"name": "Website", "customer_id": other["id"]}).status_code == 201


def test_child_needs_existing_parent(client):
    assert client.post("/api/projects", json={"name": "P", "customer_id": 999}).status_code == 400
    assert client.post("/api/projects", json={"name": "P"}).status_code == 400


def test_unknown_kind_is_not_found(client):
    assert client.post("/api/settings", json={"name": "x"}).status_code == 404
    assert client.delete("/api/settings/1").status_code == 404
    assert client.post("/api/tasks", json={"name": "x", "project_id": 1}).status_code == 404


def test_rename_and_archive(client):
    project_id = add_project(client)
    assert client.patch(f"/api/projects/{project_id}", json={"name": "Build"}).json()["name"] == "Build"
    assert client.patch(f"/api/projects/{project_id}", json={"archived": True}).json()["archived"] == 1


def test_delete_blocked_while_time_is_logged(client, clock):
    project_id = add_project(client)
    entry = add_entry(client, project_id, (clock.value, clock.value + 60))
    customer_id = client.get("/api/tree").json()["customers"][0]["id"]
    for path in (f"/api/projects/{project_id}", f"/api/customers/{customer_id}"):
        assert client.delete(path).status_code == 409
    tree = client.get("/api/tree").json()
    assert len(tree["customers"]) == 1 and len(tree["projects"]) == 1

    assert client.delete(f"/api/entries/{entry['id']}").status_code == 204
    assert client.delete(f"/api/customers/{customer_id}").status_code == 204
    tree = client.get("/api/tree").json()
    assert tree == {"customers": [], "projects": []}


# --- Timer ---


def test_start_pause_resume_stop(client, clock):
    project_id = add_project(client)
    t0 = clock.value
    active = client.post("/api/timer/start", json={"project_id": project_id, "description": "Work"}).json()["active"]
    assert active["state"] == "running"
    assert active["segments"] == [{"start": t0, "end": None}]

    clock.value = t0 + 600
    active = client.post("/api/timer/pause").json()["active"]
    assert active["state"] == "paused"
    assert active["segments"] == [{"start": t0, "end": t0 + 600}]

    clock.value = t0 + 900
    active = client.post("/api/timer/resume").json()["active"]
    assert active["state"] == "running"
    assert active["segments"][-1] == {"start": t0 + 900, "end": None}

    clock.value = t0 + 1000
    assert client.post("/api/timer/stop").json()["active"] is None
    entries = client.get("/api/entries").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["segments"] == [
        {"start": t0, "end": t0 + 600},
        {"start": t0 + 900, "end": t0 + 1000},
    ]
    assert entries[0]["description"] == "Work"


def test_wrong_timer_transitions_conflict(client, clock):
    assert client.post("/api/timer/pause").status_code == 409
    assert client.post("/api/timer/resume").status_code == 409
    assert client.post("/api/timer/stop").status_code == 409
    project_id = add_project(client)
    client.post("/api/timer/start", json={"project_id": project_id})
    assert client.post("/api/timer/resume").status_code == 409
    clock.value += 10
    client.post("/api/timer/pause")
    assert client.post("/api/timer/pause").status_code == 409


def test_stop_from_paused(client, clock):
    project_id = add_project(client)
    t0 = clock.value
    client.post("/api/timer/start", json={"project_id": project_id})
    clock.value = t0 + 60
    client.post("/api/timer/pause")
    clock.value = t0 + 9000
    assert client.post("/api/timer/stop").json()["active"] is None
    # Time spent paused is not counted.
    assert client.get("/api/entries").json()["entries"][0]["segments"] == [{"start": t0, "end": t0 + 60}]


def test_starting_stops_the_current_timer(client, clock):
    first, second = add_project(client), add_project(client, project="Shop")
    t0 = clock.value
    client.post("/api/timer/start", json={"project_id": first})
    clock.value = t0 + 120
    active = client.post("/api/timer/start", json={"project_id": second}).json()["active"]
    assert active["project_id"] == second
    entries = client.get("/api/entries").json()["entries"]
    assert [e["project_id"] for e in entries] == [first]
    assert entries[0]["segments"] == [{"start": t0, "end": t0 + 120}]


def test_stop_within_same_second_leaves_nothing(client):
    project_id = add_project(client)
    client.post("/api/timer/start", json={"project_id": project_id})
    assert client.post("/api/timer/stop").json()["active"] is None
    assert client.get("/api/entries").json()["entries"] == []


def test_pause_and_stop_within_same_second_leaves_nothing(client):
    project_id = add_project(client)
    client.post("/api/timer/start", json={"project_id": project_id})
    active = client.post("/api/timer/pause").json()["active"]
    assert active["segments"] == []
    assert client.post("/api/timer/resume").status_code == 200
    client.post("/api/timer/stop")
    assert client.get("/api/entries").json()["entries"] == []


def test_only_one_active_entry_is_possible(tmp_path, client):
    conn = timelog.connect(str(tmp_path / "test.db"))
    project_id = add_project(client)
    conn.execute("INSERT INTO entries (project_id, state) VALUES (?, 'running')", (project_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entries (project_id, state) VALUES (?, 'paused')", (project_id,))
    conn.close()


def test_start_requires_known_project(client):
    assert client.post("/api/timer/start", json={"project_id": 999}).status_code == 400
    assert client.post("/api/timer/start", json={}).status_code == 400
    assert client.get("/api/timer").json()["active"] is None


def test_failed_request_rolls_back(client, clock):
    project_id = add_project(client)
    t = clock.value
    entry = add_entry(client, project_id, (t - 100, t - 50))
    response = client.put(
        f"/api/entries/{entry['id']}",
        json={"project_id": project_id, "description": "x", "segments": [{"start": t, "end": t - 5}]},
    )
    assert response.status_code == 400
    assert client.get("/api/entries").json()["entries"][0]["description"] == ""

    client.post("/api/timer/start", json={"project_id": project_id})
    clock.value += 60
    assert client.post("/api/timer/start", json={"project_id": 999}).status_code == 400
    assert client.get("/api/timer").json()["active"]["state"] == "running"


def test_non_boolean_billable_is_rejected(client):
    project_id = add_project(client)
    response = client.post("/api/timer/start", json={"project_id": project_id, "billable": "yes"})
    assert response.status_code == 400
    assert "error" in response.json()


def test_writes_are_visible_before_the_response_returns(client):
    created = client.post("/api/customers", json={"name": "Acme"})
    assert created.status_code == 201
    assert [c["name"] for c in client.get("/api/tree").json()["customers"]] == ["Acme"]


# --- Entries ---


def test_manual_entry_and_edit(client, clock):
    project_id = add_project(client)
    other = add_project(client, customer="Other", project="Ops")
    t = clock.value - 7200
    entry = add_entry(client, project_id, (t, t + 600), (t + 900, t + 1200), description="First")
    assert entry["state"] == "stopped"

    response = client.put(
        f"/api/entries/{entry['id']}",
        json={
            "project_id": other,
            "description": "Changed",
            "billable": False,
            "segments": [{"start": t, "end": t + 300}],
        },
    )
    assert response.status_code == 200
    saved = client.get("/api/entries").json()["entries"][0]
    assert saved["project_id"] == other
    assert saved["description"] == "Changed"
    assert saved["billable"] is False
    assert saved["segments"] == [{"start": t, "end": t + 300}]


@pytest.mark.parametrize(
    "segments",
    [
        [],
        [{"start": 1800000000}],
        [{"start": 1800000000, "end": None}],
        [{"start": 1800000000, "end": 1800000000}],
        [{"start": 1800000000, "end": 1799999999}],
        [{"start": "1800000000", "end": 1800000100}],
        [{"start": 18, "end": 1800000100}],
        [{"start": 1800000000, "end": 1800000100}, {"start": 1800000050, "end": 1800000200}],
        "nope",
    ],
)
def test_invalid_segments_are_rejected(client, segments):
    project_id = add_project(client)
    response = client.post("/api/entries", json={"project_id": project_id, "segments": segments})
    assert response.status_code == 400
    assert client.get("/api/entries").json()["entries"] == []


def test_edit_running_entry_keeps_timer_consistent(client, clock):
    project_id = add_project(client)
    t0 = clock.value
    active = client.post("/api/timer/start", json={"project_id": project_id}).json()["active"]
    clock.value = t0 + 600
    url = f"/api/entries/{active['id']}"

    def put(segments):
        return client.put(url, json={"project_id": project_id, "segments": segments}).status_code

    assert put([{"start": t0 - 300, "end": t0 - 100}]) == 400  # No open period.
    assert put([{"start": t0 + 9000, "end": None}]) == 400  # Starts in the future.
    assert put([{"start": t0 - 300, "end": None}, {"start": t0 - 200, "end": t0 - 100}]) == 400
    assert put([{"start": t0 - 900, "end": t0 - 800}, {"start": t0 - 300, "end": None}]) == 200

    active = client.get("/api/timer").json()["active"]
    assert active["state"] == "running"
    assert active["segments"][-1] == {"start": t0 - 300, "end": None}


def test_stopped_entry_cannot_gain_open_period(client, clock):
    project_id = add_project(client)
    entry = add_entry(client, project_id, (clock.value - 100, clock.value))
    response = client.put(
        f"/api/entries/{entry['id']}",
        json={"project_id": project_id, "segments": [{"start": clock.value - 100, "end": None}]},
    )
    assert response.status_code == 400


def test_entries_paginate_newest_first_and_exclude_active(client, clock):
    project_id = add_project(client)
    starts = [clock.value - 86400 * n for n in range(1, 4)]
    for start in starts:
        add_entry(client, project_id, (start, start + 60))
    client.post("/api/timer/start", json={"project_id": project_id})

    page = client.get("/api/entries?limit=2").json()
    assert [e["segments"][0]["start"] for e in page["entries"]] == starts[:2]
    assert page["has_more"] is True
    page = client.get("/api/entries?limit=2&offset=2").json()
    assert [e["segments"][0]["start"] for e in page["entries"]] == starts[2:]
    assert page["has_more"] is False


def test_recent_lists_distinct_descriptions_newest_first(client, clock):
    website = add_project(client)
    shop = add_project(client, project="Shop")
    t = clock.value - 86400
    add_entry(client, website, (t, t + 60), description="Mockups")
    add_entry(client, shop, (t + 100, t + 160), description="Mockups", billable=False)
    add_entry(client, website, (t + 200, t + 260), description="Review")
    add_entry(client, website, (t + 300, t + 360), description="Mockups")
    add_entry(client, website, (t + 400, t + 460))
    recent = client.get("/api/recent").json()
    assert [(r["description"], r["project_id"], r["billable"]) for r in recent] == [
        ("Mockups", website, 1),
        ("Review", website, 1),
        ("Mockups", shop, 0),
    ]


# --- Reports ---


@pytest.fixture
def report_data(client):
    website = add_project(client, "Acme", "Website")
    shop = add_project(client, "Acme", "Shop")
    support = add_project(client, "Globex", "Website")
    add_entry(client, website, (ts(2026, 10, 12, 9), ts(2026, 10, 12, 10)), description="Mockups")
    add_entry(client, shop, (ts(2026, 10, 12, 10), ts(2026, 10, 12, 12)), description="100% done")
    add_entry(client, support, (ts(2026, 10, 13, 9), ts(2026, 10, 13, 9, 30)), billable=False)
    add_entry(client, website, (ts(2026, 11, 2, 9), ts(2026, 11, 2, 10)), description="November")
    return {"website": website, "shop": shop, "support": support}


def report(client, **params):
    response = client.get("/api/report", params={"tz": "Europe/Helsinki", **params})
    assert response.status_code == 200, response.json()
    return response.json()


def test_report_nested_grouping(client, report_data):
    data = report(client, group="customer,project,description", **{"from": "2026-10-01", "to": "2026-10-31"})
    assert data["total"] == {"seconds": 3.5 * 3600, "billable_seconds": 3 * 3600, "amount": 0}
    acme, globex = data["groups"]
    assert (acme["label"], acme["seconds"]) == ("Acme", 3 * 3600)
    assert (globex["label"], globex["billable_seconds"]) == ("Globex", 0)
    assert [(p["label"], p["seconds"]) for p in acme["children"]] == [("Shop", 2 * 3600), ("Website", 3600)]
    assert [d["label"] for d in acme["children"][1]["children"]] == ["Mockups"]
    assert [d["label"] for d in globex["children"][0]["children"]] == ["(no description)"]


def test_same_named_projects_are_not_merged(client, report_data):
    data = report(client, group="project")
    assert [g["label"] for g in data["groups"]] == ["Acme / Shop", "Acme / Website", "Globex / Website"]
    data = report(client, group="customer,project")
    assert [p["label"] for p in data["groups"][1]["children"]] == ["Website"]


def test_report_time_grouping(client, report_data):
    assert [g["label"] for g in report(client, group="day")["groups"]] == [
        "2026-10-12",
        "2026-10-13",
        "2026-11-02",
    ]
    assert [g["label"] for g in report(client, group="week")["groups"]] == ["2026-W42", "2026-W45"]
    assert [(g["label"], g["seconds"]) for g in report(client, group="month")["groups"]] == [
        ("2026-10", 3.5 * 3600),
        ("2026-11", 3600),
    ]


def test_report_filters(client, report_data):
    assert report(client, project_id=report_data["shop"])["total"]["seconds"] == 2 * 3600
    customers = client.get("/api/tree").json()["customers"]
    globex = next(c for c in customers if c["name"] == "Globex")
    assert report(client, customer_id=globex["id"])["total"]["seconds"] == 1800
    assert report(client, billable="0")["total"]["seconds"] == 1800
    assert report(client, q="mock")["total"]["seconds"] == 3600
    # LIKE wildcards in the search are literal.
    assert report(client, q="100%")["total"]["seconds"] == 2 * 3600
    assert report(client, q="%")["total"]["seconds"] == 2 * 3600


def test_report_date_range_uses_requested_time_zone(client):
    project_id = add_project(client)
    # 00:30 on 1 Oct in Helsinki is still 30 Sep in UTC.
    start = ts(2026, 10, 1, 0, 30)
    add_entry(client, project_id, (start, start + 600))
    window = {"from": "2026-10-01", "to": "2026-10-01"}
    assert report(client, **window)["total"]["seconds"] == 600
    assert report(client, tz="UTC", **window)["total"]["seconds"] == 0


def test_report_counts_paused_entry_per_day(client):
    project_id = add_project(client)
    add_entry(
        client,
        project_id,
        (ts(2026, 10, 12, 16), ts(2026, 10, 12, 17)),
        (ts(2026, 10, 13, 9), ts(2026, 10, 13, 11)),
    )
    groups = report(client, group="day")["groups"]
    assert [(g["label"], g["seconds"]) for g in groups] == [("2026-10-12", 3600), ("2026-10-13", 7200)]


def test_report_includes_running_timer(client, clock):
    project_id = add_project(client)
    client.post("/api/timer/start", json={"project_id": project_id})
    clock.value += 90
    assert report(client)["total"]["seconds"] == 90


@pytest.mark.parametrize(
    "params",
    [
        {"tz": "Mars/Olympus"},
        {"tz": "../../etc/passwd"},
        {"from": "yesterday"},
        {"group": "nope"},
        {"group": "day,day"},
        {"group": "customer,project,description,day"},
        {"group": "task"},
        {"billable": "maybe"},
        {"customer_id": "abc"},
    ],
)
def test_report_rejects_bad_params(client, params):
    assert client.get("/api/report", params=params).status_code == 400


def read_csv(response, delimiter=","):
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    text = response.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def test_csv_detail(client, report_data):
    rows = read_csv(
        client.get(
            "/api/report.csv",
            params={"tz": "Europe/Helsinki", "mode": "detail", "from": "2026-10-13", "to": "2026-10-13"},
        )
    )
    assert rows[0][:3] == ["Date", "Start", "End"]
    assert rows[1] == ["2026-10-13", "09:00:00", "09:30:00", "Globex", "Website", "", "No", "0.50", "0.00", "0.00"]
    assert len(rows) == 2


def test_csv_summary_excel_dialect(client, report_data):
    rows = read_csv(
        client.get(
            "/api/report.csv",
            params={
                "tz": "Europe/Helsinki",
                "mode": "summary",
                "dialect": "excel",
                "group": "customer,project",
                "to": "2026-10-31",
            },
        ),
        delimiter=";",
    )
    assert rows[0] == ["Customer", "Project", "Hours", "Billable hours", "Amount"]
    assert ["Globex", "Website", "0,50", "0,00", "0,00"] in rows
    assert rows[-1] == ["Total", "", "3,50", "3,00", "0,00"]


def test_csv_summary_without_groups(client, report_data):
    rows = read_csv(client.get("/api/report.csv", params={"mode": "summary"}))
    assert rows == [["Hours", "Billable hours", "Amount"], ["Total", "4.50", "4.00", "0.00"]]


# --- Preferences and billing rounding ---


def set_prefs(client, **prefs):
    response = client.put("/api/preferences", json=prefs)
    assert response.status_code == 200, response.json()
    return response.json()


def test_preferences_default_save_and_validate(client):
    assert client.get("/api/preferences").json() == {
        "theme": "system", "date_format": "iso", "round_minutes": 0, "round_scope": "entry", "default_rate": 0,
    }
    saved = set_prefs(client, theme="dark", date_format="locale", round_minutes=30, round_scope="day")
    assert client.get("/api/preferences").json() == saved
    for bad in ({"theme": "pink"}, {"round_minutes": 7}, {"round_scope": "week"}, {"round_minutes": "30"}, {"default_rate": -1}):
        assert client.put("/api/preferences", json=bad).status_code == 400
    assert client.get("/api/preferences").json() == saved


def test_preferences_never_expose_secrets(client):
    text = client.get("/api/preferences").text
    assert "secret" not in text and "password" not in text


def test_billable_rounding_per_entry(client):
    project_id = add_project(client)
    # 10 min + 40 min in separate entries; one non-billable 5 min entry.
    add_entry(client, project_id, (ts(2026, 10, 12, 9), ts(2026, 10, 12, 9, 10)), description="Fix")
    add_entry(client, project_id, (ts(2026, 10, 12, 10), ts(2026, 10, 12, 10, 40)), description="Fix")
    add_entry(client, project_id, (ts(2026, 10, 12, 11), ts(2026, 10, 12, 11, 5)), billable=False)
    assert report(client)["total"] == {"seconds": 55 * 60, "billable_seconds": 50 * 60, "amount": 0}
    set_prefs(client, round_minutes=30)
    data = report(client, group="day")
    assert data["total"] == {"seconds": 55 * 60, "billable_seconds": 90 * 60, "amount": 0}
    assert data["groups"][0]["billable_seconds"] == 90 * 60
    set_prefs(client, round_minutes=30, round_scope="day")
    assert report(client)["total"]["billable_seconds"] == 60 * 60


def test_billable_rounding_counts_paused_entry_once(client):
    project_id = add_project(client)
    add_entry(
        client, project_id,
        (ts(2026, 10, 12, 9), ts(2026, 10, 12, 9, 10)),
        (ts(2026, 10, 12, 9, 20), ts(2026, 10, 12, 9, 35)),
    )
    set_prefs(client, round_minutes=15)
    assert report(client)["total"]["billable_seconds"] == 30 * 60
    rows = read_csv(client.get("/api/report.csv", params={"tz": "Europe/Helsinki", "mode": "detail"}))
    assert [r[-2] for r in rows[1:]] == ["0.25", "0.25"]  # Rounding goes on the first period.
    rows = read_csv(client.get("/api/report.csv", params={"tz": "Europe/Helsinki", "mode": "summary"}))
    assert rows[-1] == ["Total", "0.42", "0.50", "0.00"]


def test_rounding_day_scope_uses_report_time_zone(client):
    project_id = add_project(client)
    # 23:50 and 00:10 Helsinki time: two days there, but the same day in UTC.
    add_entry(client, project_id, (ts(2026, 10, 12, 23, 50), ts(2026, 10, 12, 23, 55)), description="On call")
    add_entry(client, project_id, (ts(2026, 10, 13, 0, 10), ts(2026, 10, 13, 0, 15)), description="On call")
    set_prefs(client, round_minutes=30, round_scope="day")
    assert report(client)["total"]["billable_seconds"] == 60 * 60
    assert report(client, tz="UTC")["total"]["billable_seconds"] == 30 * 60


def test_customer_rate_overrides_default(client):
    acme = add_project(client, "Acme", "Website")
    globex = add_project(client, "Globex", "Support")
    add_entry(client, acme, (ts(2026, 10, 12, 9), ts(2026, 10, 12, 10, 30)))
    add_entry(client, globex, (ts(2026, 10, 12, 11), ts(2026, 10, 12, 11, 10)))
    add_entry(client, globex, (ts(2026, 10, 12, 12), ts(2026, 10, 12, 13)), billable=False)
    customers = {c["name"]: c["id"] for c in client.get("/api/tree").json()["customers"]}
    response = client.patch(f"/api/customers/{customers['Globex']}", json={"hourly_rate": 120})
    assert response.json()["hourly_rate"] == 120
    set_prefs(client, default_rate=80, round_minutes=30)

    data = report(client, group="customer")
    # Acme: 1.5 h at the default 80; Globex: 10 min rounded up to 0.5 h at 120; non-billable is free.
    assert [(g["label"], g["amount"]) for g in data["groups"]] == [("Acme", 120), ("Globex", 60)]
    assert data["total"]["amount"] == 180
    rows = read_csv(client.get("/api/report.csv", params={"mode": "summary", "group": "customer"}))
    assert rows[-1] == ["Total", "2.67", "2.00", "180.00"]

    # Clearing the customer's rate goes back to the default.
    client.patch(f"/api/customers/{customers['Globex']}", json={"hourly_rate": None})
    assert report(client)["total"]["amount"] == 160


def test_hourly_rate_is_validated_and_customers_only(client):
    project_id = add_project(client)
    customer_id = client.get("/api/tree").json()["customers"][0]["id"]
    assert client.patch(f"/api/customers/{customer_id}", json={"hourly_rate": -5}).status_code == 400
    assert client.patch(f"/api/customers/{customer_id}", json={"hourly_rate": "90"}).status_code == 400
    assert client.patch(f"/api/projects/{project_id}", json={"hourly_rate": 90}).status_code == 400
    # Renaming leaves the rate alone.
    client.patch(f"/api/customers/{customer_id}", json={"hourly_rate": 95.5})
    renamed = client.patch(f"/api/customers/{customer_id}", json={"name": "Acme Oy"}).json()
    assert renamed["hourly_rate"] == 95.5


# --- Migration ---


def test_tasks_are_migrated_into_descriptions(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE projects (id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            name TEXT NOT NULL COLLATE NOCASE, archived INTEGER NOT NULL DEFAULT 0, UNIQUE (customer_id, name));
        CREATE TABLE tasks (id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL COLLATE NOCASE, archived INTEGER NOT NULL DEFAULT 0, UNIQUE (project_id, name));
        CREATE TABLE entries (id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
            description TEXT NOT NULL DEFAULT '', billable INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'stopped' CHECK (state IN ('running', 'paused', 'stopped')));
        CREATE UNIQUE INDEX one_active_entry ON entries (state != 'stopped') WHERE state != 'stopped';
        CREATE TABLE segments (id INTEGER PRIMARY KEY,
            entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            start_ts INTEGER NOT NULL, end_ts INTEGER, CHECK (end_ts IS NULL OR end_ts > start_ts));
        INSERT INTO customers VALUES (1, 'Acme', 0);
        INSERT INTO projects VALUES (2, 1, 'Website', 0);
        INSERT INTO tasks VALUES (3, 2, 'Design', 0);
        INSERT INTO entries VALUES (4, 3, '', 1, 'stopped'), (5, 3, 'Logo', 0, 'running');
        INSERT INTO segments VALUES (6, 4, 1800000000, 1800000600), (7, 5, 1800001000, NULL);
        """
    )
    conn.close()

    conn = timelog.init_db(str(path))
    assert [tuple(r) for r in conn.execute("SELECT * FROM entries ORDER BY id")] == [
        (4, 2, "Design", 1, "stopped"),
        (5, 2, "Design: Logo", 0, "running"),
    ]
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 2
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'tasks'").fetchone() is None
    assert conn.execute("SELECT hourly_rate FROM customers").fetchone()[0] is None
    # Constraints still hold on the rebuilt table.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO entries (project_id, state) VALUES (2, 'paused')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM projects WHERE id = 2")
    conn.execute("DELETE FROM entries WHERE id = 4")
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
    conn.close()
    # Running it again is harmless.
    timelog.init_db(str(path)).close()


# --- Command line ---


def test_backup_command(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.db"
    monkeypatch.setenv("TIMELOG_DB", str(source))
    conn = timelog.init_db(str(source))
    conn.execute("INSERT INTO customers (name) VALUES ('Acme')")
    conn.close()
    destination = tmp_path / "backup.db"
    timelog.cli(["backup", str(destination)])
    copy = sqlite3.connect(destination)
    assert copy.execute("SELECT name FROM customers").fetchall() == [("Acme",)]
    copy.close()


def test_set_password_command(tmp_path, monkeypatch):
    path = tmp_path / "pw.db"
    monkeypatch.setenv("TIMELOG_DB", str(path))
    monkeypatch.setattr(timelog.getpass, "getpass", lambda _prompt: "long enough")
    timelog.cli(["set-password"])
    client = TestClient(timelog.create_app(str(path)), follow_redirects=False)
    assert client.post("/login", data={"password": "long enough"}).headers["location"] == "/"
