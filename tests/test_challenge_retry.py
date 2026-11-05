"""One fresh-session try after a human check, before the owner is notified. Never a second click."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.applications import ApplicationService
from amorehiremepls.apply.browser import HandoffRequired, Plan
from amorehiremepls.candidate.profile import Profile
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import utcnow
from amorehiremepls.core.worker import Worker
from amorehiremepls.web.web import create_app
from tests.test_notify import _job_app, _notifier, _settings

pytestmark = pytest.mark.mongo


def _svc(store, sent, minutes=180):
    job, app = _job_app(store)
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    settings = _settings(store, challenge_retry_minutes=minutes)
    notifier = _notifier(store, settings, sent)
    plan = lambda _job, pkg, _answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="Thank you", site="Example")  # noqa: E731
    svc = ApplicationService(store, Profile(), None, notifier.actions, {"ats.example": plan}, notifier=notifier, challenge_retry_minutes=minutes)  # type: ignore[arg-type]
    return settings, svc, job, app


def _challenge(plan, clicked=True):
    raise HandoffRequired("the job site asked for a human check", {"challenge": "text_captcha", "final_clicked": clicked, "url": plan.start_url})


@pytest.mark.mongo
def test_a_check_waits_for_one_later_try_before_the_note(store, tmp_path):
    sent: list = []
    settings, svc, job, app = _svc(store, sent)
    out = svc.execute(app["_id"], "w1", lambda plan: _challenge(plan, clicked=True))
    assert out.status == "retry_scheduled" and sent == []
    saved = store.c("applications").find_one({"_id": app["_id"]})
    assert saved["status"] == "challenge_retry" and "once more later" in saved["reason"]
    doc = store.c("challenge_retries").find_one({"idempotency_key": f"challenge_retry:{job['_id']}"})
    assert doc["status"] == "scheduled" and doc["final_clicked"] is True and doc["material_hash"] == app["material_hash"]
    assert store.c("challenge_retries").count_documents({}) == 1

    called = {"n": 0}

    def boom(_plan):
        called["n"] += 1
        raise AssertionError("the browser must not run before the later try is due")

    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "authorized", "challenge_retry_active": False}})
    assert svc.execute(app["_id"], "w1", boom).status == "retry_scheduled"
    assert called["n"] == 0 and sent == []

    rt = Runtime(settings, store=store)
    rt.applications = lambda run_id=None, builders=None: svc  # type: ignore[method-assign]
    seen: dict = {}

    def run_plan(plan, site_key="default", observe=True, captcha_solver=None, hold=None, confirm_only=False):
        seen.update(confirm_only=confirm_only, hold=hold)
        raise HandoffRequired("the job site asked for a human check", {"challenge": "text_captcha", "url": plan.start_url})

    rt.browser.run_plan = run_plan
    worker = Worker(rt)
    assert worker.run_due_challenge_retries() == 0 and seen == {}
    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "challenge_retry", "challenge_retry_active": False}})
    store.c("challenge_retries").update_one({"_id": doc["_id"]}, {"$set": {"due_at": utcnow() - timedelta(seconds=1)}})
    assert worker.run_due_challenge_retries() == 1
    assert seen == {"confirm_only": True, "hold": None}
    assert len(sent) == 1 and "human check" in sent[0].get_content().lower()
    assert svc.apps.find_one({"_id": app["_id"]})["status"] == "handoff"
    assert store.c("challenge_retries").find_one({"_id": doc["_id"]})["status"] == "done"
    assert worker.run_due_challenge_retries() == 0 and len(sent) == 1  # the one try is spent


@pytest.mark.mongo
def test_retry_off_and_an_open_hold_do_not_queue_a_later_try(store):
    sent: list = []
    _settings_off, svc, job, app = _svc(store, sent, minutes=0)
    out = svc.execute(app["_id"], "w1", lambda plan: _challenge(plan))
    assert out.status == "handoff" and len(sent) == 1
    assert store.c("challenge_retries").count_documents({}) == 0

    sent.clear()
    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "authorized", "challenge_retry_active": False}})
    store.c("actions").delete_many({})
    store.c("notifications").delete_many({})
    store.c("authorizations").delete_many({})
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    svc.challenge_retry_minutes = 180
    store.c("human_holds").insert_one(
        {"_id": "hold_open", "application_id": app["_id"], "job_id": job["_id"], "status": "holding", "heartbeat_at": utcnow(), "material_hash": app["material_hash"]}
    )
    out = svc.execute(app["_id"], "w1", lambda plan: _challenge(plan, clicked=False))
    assert out.status == "retry_scheduled" and sent == []
    assert store.c("challenge_retries").count_documents({}) == 0
    assert svc.apps.find_one({"_id": app["_id"]})["status"] == "authorized"


@pytest.mark.mongo
def test_a_crashed_later_try_is_not_run_again(store):
    sent: list = []
    settings, svc, job, app = _svc(store, sent)
    svc.execute(app["_id"], "w1", lambda plan: _challenge(plan, clicked=True))
    doc = store.c("challenge_retries").find_one({"job_id": job["_id"]})
    store.c("challenge_retries").update_one({"_id": doc["_id"]}, {"$set": {"status": "running", "reserved_until": utcnow() - timedelta(minutes=5)}})
    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"challenge_retry_active": True}})
    rt = Runtime(settings, store=store)
    rt.applications = lambda run_id=None, builders=None: svc  # type: ignore[method-assign]
    rt.browser.run_plan = lambda *a, **k: pytest.fail("a crashed later try must not open the browser again")
    assert Worker(rt).run_due_challenge_retries() == 0
    assert svc.apps.find_one({"_id": app["_id"]})["status"] == "handoff"
    assert len(sent) == 1
    assert store.c("challenge_retries").find_one({"_id": doc["_id"]})["result"] == "lease_expired"


@pytest.mark.mongo
def test_waiting_page_uses_plain_language(store, tmp_path):
    sent: list = []
    settings, svc, _job, app = _svc(store, sent)
    svc.execute(app["_id"], "w1", lambda plan: _challenge(plan))
    store.c("applications").update_one(
        {"_id": app["_id"]},
        {"$set": {"review_package.document.sha256": "a" * 64, "review_package.document.pdf_sha256": "b" * 64}},
    )
    settings = _settings(
        store,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        tracker_clone_dir=tmp_path / "clone",
        app_password="correct-horse-battery",
    )
    client = TestClient(create_app(Runtime(settings, store=store)), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get(f"/applications/{app['_id']}").text
    assert "Trying once more later" in page and "once more later" in page
    assert "Finish on the site yourself" not in page
