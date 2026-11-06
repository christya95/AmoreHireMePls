"""Background tasks page: still waiting is separate from finished work. Values here are fictional."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import utcnow
from amorehiremepls.web.task_list import next_application, partition, queue_summary, queued_reason, worker_status
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

pytestmark = pytest.mark.mongo


def test_queued_reason_names_a_missing_owner_a_hold_and_a_quiet_worker():
    now = datetime(2026, 11, 5, 22, 40, tzinfo=UTC)
    run = {"status": "queued", "kind": "application", "created_at": now - timedelta(minutes=3)}
    missing = queued_reason(run, now=now, worker_at=now, owner_state="missing", emergency=False)
    assert "no owner" in missing and "just checked in" in missing and "3 minutes" in missing
    held = {**run, "reserved_until": now + timedelta(minutes=5)}
    assert "holding" in queued_reason(held, now=now, worker_at=now - timedelta(minutes=4), owner_state="active", emergency=False)
    assert "has not checked in" in queued_reason(run, now=now, worker_at=None, owner_state="active", emergency=False)
    assert queued_reason({**run, "status": "running"}, now=now, worker_at=now, owner_state="active", emergency=False) is None
    queued = {"status": "queued", "kind": "application", "params": {"job_id": "j1"}}
    assert worker_status(True, queued, {}) == ("idle", "The app is idle")
    running = {"status": "running", "kind": "application", "params": {"job_id": "j1"}}
    assert worker_status(True, running, {"j1": {"employer": "Acme", "title": "Role"}}) == ("", "The app is on Acme, Role")
    assert worker_status(False, None, {}) == ("warn", "Background tasks are not running")


def test_summary_names_the_oldest_application_and_counts_the_three_states():
    early = {"_id": "run_a", "status": "queued", "kind": "application", "created_at": datetime(2026, 1, 1, tzinfo=UTC), "params": {"job_id": "j1"}}
    later = {"_id": "run_b", "status": "queued", "kind": "application", "created_at": datetime(2026, 1, 2, tzinfo=UTC), "params": {"job_id": "j2"}}
    search = {"_id": "run_c", "status": "queued", "kind": "discovery_request", "created_at": datetime(2026, 1, 1, tzinfo=UTC), "params": {}}
    paused = {"_id": "run_d", "status": "waiting", "kind": "application", "created_at": datetime(2026, 1, 3, tzinfo=UTC), "params": {}}
    done = {"_id": "run_e", "status": "cancelled", "kind": "application", "created_at": datetime(2026, 1, 4, tzinfo=UTC), "params": {}}
    hidden = {"_id": "run_f", "status": "failed", "dismissed": True, "kind": "application", "params": {}}
    waiting, earlier = partition([done, early, later, search, paused, hidden])
    assert {r["_id"] for r in waiting} == {"run_a", "run_b", "run_c", "run_d"}
    assert [r["_id"] for r in earlier] == ["run_e"]
    assert next_application(waiting)["_id"] == "run_a"
    text = queue_summary(waiting, "Acme, Example role")
    assert text.startswith("3 tasks waiting to start, none running, 1 paused for you.")
    assert "Next: Acme, Example role." in text
    assert queue_summary([], None) == "Nothing is waiting."
    only_search = queue_summary([search], None)
    assert "1 task waiting to start" in only_search and "does not apply to a job" in only_search


@pytest.fixture
def client(store, tmp_path):
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    return TestClient(create_app(Runtime(s, store=store)), follow_redirects=False)


def _login(client):
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def _seed(client):
    store = client.app.state.rt.store
    store.c("jobs").insert_one({"_id": "job_acme", "employer": "Acme", "title": "Example role"})
    store.c("jobs").insert_one({"_id": "job_north", "employer": "Northwind", "title": "Later role"})
    store.c("applications").insert_one({"_id": "app_acme", "job_id": "job_acme", "status": "handoff", "provenance": {"origin": "local"}})
    store.c("applications").insert_one({"_id": "app_north", "job_id": "job_north", "status": "queued", "provenance": {"origin": "local"}})
    first = store.create_run("application", {"job_id": "job_acme"})
    second = store.create_run("application", {"job_id": "job_north"})
    search = store.create_run("discovery_request", {"trigger": "dashboard"})
    done = store.create_run("application", {"job_id": "job_acme"})
    store.update_run(first["_id"], created_at=datetime(2026, 1, 1, tzinfo=UTC))
    store.update_run(second["_id"], created_at=datetime(2026, 1, 2, tzinfo=UTC))
    store.update_run(
        done["_id"],
        status="cancelled",
        interrupt={"type": "handoff", "application_id": "app_acme"},
        finished_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    return store, first, second, search, done


def test_queued_run_page_says_it_has_not_started_and_when_the_worker_checked_in(client):
    _login(client)
    store = client.app.state.rt.store
    store.c("jobs").insert_one({"_id": "job_acme", "employer": "Acme", "title": "Example role"})
    run = store.create_run("application", {"job_id": "job_acme"})
    store.set_control("worker_heartbeat", {"at": utcnow()})
    page = client.get(f"/runs/{run['_id']}").text
    assert "<h1>Acme, Example role</h1>" in page and f'<p class="note mono">{run["_id"]}</p>' in page
    assert "It has not started." in page and "The worker just checked in." in page
    assert ">Pause</button>" not in page and ">Resume</button>" not in page and ">Cancel</button>" in page
    assert "AI use" not in page and "<h3>Result</h3>" not in page
    assert "The app is idle" in page and "Background tasks running" not in page
    assert "no owner" not in page


def test_page_splits_waiting_from_finished_and_offers_cancel_only_before_start(client):
    csrf = _login(client)
    store, first, second, search, done = _seed(client)
    page = client.get("/runs").text
    assert "Still waiting" in page and "Already finished" in page
    assert "Next: Acme, Example role." in page
    assert "3 tasks waiting to start" in page
    assert page.count(">Cancel</button>") == 3  # both applications and the job search
    assert "Hide finished and cancelled" in page
    # the finished row links to the application and does not look like it is still waiting
    finished = page.split("Already finished", 1)[1]
    assert "Finish on the site yourself" not in finished
    assert "/applications/app_acme" in finished
    assert f"/runs/{done['_id']}/hide" in finished
    waiting = page.split("Already finished", 1)[0]
    assert "/hide" not in waiting
    assert store.c("jobs").count_documents({}) == 2
    assert csrf


def test_cancel_stops_a_queued_task_and_leaves_the_job_and_application(client):
    csrf = _login(client)
    store, first, *_ = _seed(client)
    assert client.post(f"/runs/{first['_id']}/cancel", data={"csrf": csrf, "next": "/runs"}).status_code == 303
    doc = store.c("runs").find_one({"_id": first["_id"]})
    assert doc["status"] == "cancelled" and doc.get("note") == "cancelled before it started"
    assert store.c("jobs").find_one({"_id": "job_acme"})["title"] == "Example role"
    assert store.c("applications").find_one({"_id": "app_acme"})["status"] == "handoff"
    page = client.get("/runs").text
    assert "Next: Northwind, Later role." in page
    assert client.post(f"/runs/{first['_id']}/cancel", data={"next": "/runs"}).status_code == 422


def test_cancel_is_refused_once_a_submission_is_underway(client):
    csrf = _login(client)
    store, first, *_ = _seed(client)
    store.c("applications").update_one({"_id": "app_acme"}, {"$set": {"status": "executing"}})
    page = client.get("/runs").text
    assert f'action="/runs/{first["_id"]}/cancel"' not in page
    assert client.post(f"/runs/{first['_id']}/cancel", data={"csrf": csrf, "next": "/runs"}).status_code == 409
    assert store.c("runs").find_one({"_id": first["_id"]})["status"] == "queued"


def test_hide_removes_a_finished_row_from_the_list_and_keeps_the_record(client):
    csrf = _login(client)
    store, first, _, _, done = _seed(client)
    assert client.post(f"/runs/{first['_id']}/hide", data={"csrf": csrf, "next": "/runs"}).status_code == 409
    assert client.post(f"/runs/{done['_id']}/hide", data={"csrf": csrf, "next": "/runs"}).status_code == 303
    hidden = store.c("runs").find_one({"_id": done["_id"]})
    assert hidden["dismissed"] is True and hidden["status"] == "cancelled"
    assert store.c("applications").find_one({"_id": "app_acme"})
    page = client.get("/runs").text
    assert "Already finished" not in page and done["_id"] not in page
    # a second finished row, then the page action hides every earlier row
    other = store.create_run("application", {"job_id": "job_north"})
    store.update_run(other["_id"], status="failed")
    assert "Hide finished and cancelled" in client.get("/runs").text
    assert client.post("/runs/hide-finished", data={"csrf": csrf}).status_code == 303
    assert store.c("runs").find_one({"_id": other["_id"]})["dismissed"] is True
    assert store.c("runs").find_one({"_id": first["_id"]})["status"] == "queued"
    assert "Already finished" not in client.get("/runs").text


def test_clear_leaves_nothing_waiting_and_a_link_back_to_jobs(client):
    csrf = _login(client)
    store, first, *_ = _seed(client)
    assert client.post("/runs/clear", data={"csrf": csrf, "confirm": "clear"}).status_code == 303
    page = client.get("/runs").text
    assert "Nothing is waiting." in page and 'href="/jobs">Back to Jobs</a>' in page
    assert "<h3>Still waiting</h3>" not in page and "<h3>Already finished</h3>" not in page
    assert store.c("runs").find_one({"_id": first["_id"]})["status"] == "cancelled"
    assert store.c("jobs").find_one({"_id": "job_acme"})["title"] == "Example role"
    assert store.c("applications").find_one({"_id": "app_acme"})["status"] == "handoff"
