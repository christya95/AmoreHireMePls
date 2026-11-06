"""Human-check hold: the same page stays open, Continue does not start a new submit, a timeout becomes a handoff."""

from __future__ import annotations

import re
import socket
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.actions import ActionStore
from amorehiremepls.core.config import Settings
from amorehiremepls.core.human_check import HumanHold, active_hold, launch_args, request_continue, run_hold
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import utcnow
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.test_applications_and_sync import _job


def test_run_hold_stops_on_continue_receipt_or_the_deadline():
    beats = {"n": 0}

    def heartbeat():
        beats["n"] += 1

    def cont():
        return beats["n"] >= 2

    slept: list[float] = []
    assert run_hold(minutes=10, poll_seconds=5, sleep=slept.append, heartbeat=heartbeat, continue_requested=cont, receipt_visible=lambda: False) == "continue"
    assert slept == [5] and beats["n"] == 2
    assert run_hold(minutes=10, poll_seconds=5, sleep=slept.append, heartbeat=heartbeat, continue_requested=lambda: True, receipt_visible=lambda: True) == "receipt"
    assert run_hold(minutes=0, poll_seconds=1, sleep=slept.append, heartbeat=heartbeat, continue_requested=lambda: True, receipt_visible=lambda: True) == "timeout"

    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    assert (
        run_hold(
            minutes=1,
            poll_seconds=30,
            sleep=sleep,
            heartbeat=lambda: None,
            continue_requested=lambda: False,
            receipt_visible=lambda: False,
            monotonic=lambda: clock["t"],
        )
        == "timeout"
    )
    assert clock["t"] == 60


def test_launch_args_bind_the_debug_port_to_localhost_only():
    assert launch_args(0) == ["--disable-dev-shm-usage"]
    args = launch_args(9222)
    assert "--remote-debugging-port=9222" in args and "--remote-debugging-address=127.0.0.1" in args


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.browser
@pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")
def test_debug_port_answers_on_localhost():
    import urllib.request

    from playwright.sync_api import sync_playwright

    port = _free_port()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=launch_args(port))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as resp:
                body = resp.read()
            assert b"127.0.0.1" in body and b"webSocketDebuggerUrl" in body
        finally:
            browser.close()


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


def _login(client: TestClient) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def _holding(store, job_id: str, app_id: str, material_hash: str = "hash_hold") -> HumanHold:
    store.c("applications").insert_one({"_id": app_id, "job_id": job_id, "status": "authorized", "material_hash": material_hash, "updated_at": utcnow()})
    store.c("actions").insert_one(
        {
            "_id": "act_hold",
            "kind": "application_submit",
            "target_id": job_id,
            "idempotency_key": f"submit:{job_id}",
            "status": "executing",
            "reserved_by": "w1",
            "reserved_until": utcnow() + timedelta(seconds=30),
            "material_hash": material_hash,
        }
    )
    store.c("jobs").update_one({"_id": job_id}, {"$set": {"status": "executing", "reserved_until": utcnow() + timedelta(seconds=30)}})
    hold = HumanHold(store, ActionStore(store), Settings(_env_file=None), app_id, job_id, "w1")
    hold.heartbeat({"cdp_port": 9222})
    return hold


@pytest.mark.mongo
def test_heartbeat_extends_the_same_action_and_continue_does_not_create_another(store):
    job = _job(store)
    before_until = utcnow()
    hold = _holding(store, job["_id"], "app_hold")
    action = store.c("actions").find_one({"_id": "act_hold"})
    assert action["reserved_until"] > before_until + timedelta(seconds=120)
    assert store.c("human_holds").count_documents({"application_id": "app_hold"}) == 1
    hold.heartbeat({"cdp_port": 9222})
    assert store.c("human_holds").count_documents({"application_id": "app_hold"}) == 1  # the same hold, not a second one
    app = store.c("applications").find_one({"_id": "app_hold"})
    assert request_continue(store, app) == "ok"
    assert store.c("actions").count_documents({}) == 1
    assert store.c("human_holds").find_one({"_id": hold.hold_id})["continue_requested"] is True
    store.c("human_holds").update_one({"_id": hold.hold_id}, {"$set": {"material_hash": "other"}})
    assert request_continue(store, app) == "changed"
    assert store.c("actions").count_documents({}) == 1
    store.c("human_holds").update_one({"_id": hold.hold_id}, {"$set": {"heartbeat_at": utcnow() - timedelta(seconds=60), "status": "holding"}})
    assert active_hold(store, "app_hold") is None
    assert request_continue(store, app) == "missing"


@pytest.mark.mongo
def test_application_page_shows_human_check_and_continue_is_csrf_bound(store, client):
    job = _job(store)
    hold = _holding(store, job["_id"], "app_hold")
    csrf = _login(client)
    page = client.get("/applications/app_hold").text
    assert "Human check needed" in page and "127.0.0.1:9222" in page and "does not send the form again" in page
    assert "Human check needed" in client.get("/").text
    assert client.post("/applications/app_hold/continue", data={"material_hash": "hash_hold"}).status_code == 422
    assert client.post("/applications/app_hold/continue", data={"csrf": "bogus", "material_hash": "hash_hold"}).status_code == 403
    assert client.post("/applications/app_hold/continue", data={"csrf": csrf, "material_hash": "stale"}).status_code == 409
    ok = client.post("/applications/app_hold/continue", data={"csrf": csrf, "material_hash": "hash_hold"})
    assert ok.status_code == 303 and "ok=continue_ok" in ok.headers["location"]
    assert store.c("actions").count_documents({}) == 1
    assert store.c("human_holds").find_one({"_id": hold.hold_id})["continue_requested"] is True
    again = client.post("/applications/app_hold/continue", data={"csrf": csrf, "material_hash": "hash_hold"})
    assert again.status_code == 303 and "ok=continue_ok" in again.headers["location"]
    store.c("human_holds").update_one({"_id": hold.hold_id}, {"$set": {"material_hash": "other"}})
    changed = client.post("/applications/app_hold/continue", data={"csrf": csrf, "material_hash": "hash_hold"})
    assert changed.status_code == 303 and "err=continue_changed" in changed.headers["location"]
    assert "The answers or the resume changed" in client.get(changed.headers["location"]).text
    store.c("human_holds").update_one({"_id": hold.hold_id}, {"$set": {"heartbeat_at": utcnow() - timedelta(minutes=5)}})
    stale = client.get("/applications/app_hold").text
    assert "Human check needed" not in stale
