"""Dashboard routes added with the Greenhouse adapter: the captcha switch under Settings and "Retry with the app"."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.config import Settings
from amorehiremepls.runtime import Runtime
from amorehiremepls.web import create_app
from tests.conftest import TEST_MONGO_URI

pytestmark = pytest.mark.mongo


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


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def test_settings_captcha_switch_is_off_by_default_and_persists(client):
    csrf = _login(client)
    rt = client.app.state.rt
    page = client.get("/settings").text
    assert "Try to solve captchas automatically" in page and 'name="autosolve" value="on" >' in page.replace("\n", " ") or 'name="autosolve" value="on"' in page
    assert "some job sites forbid this" in page and rt.captcha_autosolve_enabled() is False
    r = client.post("/settings/captcha", data={"csrf": csrf, "autosolve": "on"})
    assert r.status_code == 303 and r.headers["location"].endswith("ok=captcha_on")
    assert rt.store.get_control("captcha_autosolve") is True and rt.captcha_autosolve_enabled() is True
    assert "checked" in client.get("/settings").text.split("Try to solve captchas")[1][:600]
    r = client.post("/settings/captcha", data={"csrf": csrf})
    assert r.headers["location"].endswith("ok=captcha_off") and rt.captcha_autosolve_enabled() is False
    assert rt.store.c("audit").find_one({"event": "captcha_autosolve_changed", "data.enabled": True}) or rt.store.c("audit").find_one({"event": "captcha_autosolve_changed"})


def _handoff_app(rt) -> tuple[str, str, str]:
    now = datetime.now(UTC)
    job = {
        "_id": "job_gh1",
        "employer": "Acme",
        "employer_norm": "acme",
        "title": "Backend Developer",
        "status": "qualified",
        "posting_url": "https://boards.greenhouse.io/acme/jobs/123456",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/123456",
        "requisition_id": "123456",
        "sources": ["manual"],
        "form_plan": {
            "fields": [{"id": "first_name", "label": "First Name", "type": "text", "meta": {"ats": "greenhouse"}}],
            "source": "greenhouse_job_board_api",
            "receipt_text": "x",
        },
        "apply_route": "greenhouse",
        "created_at": now,
        "updated_at": now,
    }
    rt.store.c("jobs").insert_one(job)
    app = {
        "_id": "app_gh1",
        "job_id": "job_gh1",
        "status": "handoff",
        "handoff_kind": "human_check",
        "reason": "Greenhouse asked for a human check",
        "provenance": {"origin": "local"},
        "material_hash": "m1",
        "review_package": {"apply_url": job["apply_url"], "posting_url": job["posting_url"], "apply_route": "greenhouse", "answers": [], "gaps": [], "outstanding": []},
        "history": [],
        "created_at": now,
        "updated_at": now,
    }
    rt.store.c("applications").insert_one(app)
    run = rt.store.create_run("application", {"job_id": "job_gh1"})
    rt.store.update_run(run["_id"], status="waiting", interrupt={"type": "handoff", "application_id": "app_gh1"})
    return job["_id"], app["_id"], run["_id"]


def test_retry_with_the_app_closes_the_waiting_task_and_rereads_the_form(client):
    csrf = _login(client)
    rt = client.app.state.rt
    job_id, app_id, run_id = _handoff_app(rt)
    page = client.get(f"/applications/{app_id}").text
    assert "Retry with the app" in page and "human check" in page and "Greenhouse" in page
    assert '<button class="secondary"' not in page.split("Retry with the app", 1)[0][-120:]
    r = client.post(f"/applications/{app_id}/retry", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("ok=retry_started")
    job = rt.store.c("jobs").find_one({"_id": job_id})
    assert "fields" not in job["form_plan"] and job["form_plan"]["receipt_text"] == "x" and job["apply_route"] is None and job["status"] == "qualified"
    assert rt.store.c("runs").find_one({"_id": run_id})["status"] == "cancelled"
    new_run = rt.store.c("runs").find_one({"params.retry_of": app_id})
    assert new_run and new_run["status"] == "queued" and new_run["params"]["job_id"] == job_id
    app = rt.store.c("applications").find_one({"_id": app_id})
    assert app["status"] == "queued" and "reading the form again" in app["next_action"]
    # a second click while the new task is queued is refused politely
    r = client.post(f"/applications/{app_id}/retry", data={"csrf": csrf})
    assert r.status_code == 409


def test_retry_is_not_offered_for_sites_without_a_form_filler(client):
    csrf = _login(client)
    rt = client.app.state.rt
    _handoff_app(rt)
    rt.store.c("jobs").update_one({"_id": "job_gh1"}, {"$set": {"apply_url": "https://careers.example.com/x", "posting_url": "https://careers.example.com/x"}})
    rt.store.c("applications").update_one({"_id": "app_gh1"}, {"$set": {"review_package.apply_url": "https://careers.example.com/x", "handoff_kind": "login"}})
    assert "Retry with the app" not in client.get("/applications/app_gh1").text
    r = client.post("/applications/app_gh1/retry", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("err=retry_unsupported")


def test_approval_continues_a_task_still_waiting_for_answers_and_the_page_shows_updates(client):
    csrf = _login(client)
    rt = client.app.state.rt
    now = datetime.now(UTC)
    rt.store.c("jobs").insert_one(
        {
            "_id": "job_wait",
            "employer": "Acme",
            "employer_norm": "acme",
            "title": "Backend Developer",
            "status": "needs_review",
            "posting_url": "https://boards.greenhouse.io/acme/jobs/123456",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/123456",
            "requisition_id": "123456",
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )
    rt.store.c("applications").insert_one(
        {
            "_id": "app_wait",
            "job_id": "job_wait",
            "status": "ready_for_review",
            "provenance": {"origin": "local"},
            "material_hash": "m-wait",
            "review_package": {
                "apply_url": "https://boards.greenhouse.io/acme/jobs/123456",
                "posting_url": "https://boards.greenhouse.io/acme/jobs/123456",
                "answers": [{"field_id": "first_name", "label": "First name", "value": "Ada", "fact_ids": ["user:input"], "needs_input": False}],
                "outstanding": [],
                "gaps": [],
            },
            "history": [{"at": now, "status": "ready_for_review"}],
            "created_at": now,
            "updated_at": now,
        }
    )
    run = rt.store.create_run("application", {"job_id": "job_wait"})
    rt.store.update_run(run["_id"], status="waiting", interrupt={"type": "needs_input", "application_id": "app_wait"})
    rt.store.run_event(run["_id"], "waiting")
    r = client.post("/applications/app_wait/authorize", data={"csrf": csrf, "material_hash": "m-wait", "hours": "24"})
    assert r.status_code == 303
    saved = rt.store.c("runs").find_one({"_id": run["_id"]})
    assert saved["requested"] == "resume" and saved["resume_payload"]["inputs"]["first_name"] == "Ada"
    page = client.get("/applications/app_wait").text
    assert "Updates" in page and "You approved this package" in page and "Package ready for you to read" in page
    assert "The task will continue within a minute" in page
    assert "Nothing to do" not in page and "within a minute. Nothing" not in page
