"""Delete or restart an application that was never sent. Fictional employers only. Nothing here submits."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.applications import ApplicationRefused, ApplicationService, submission_lock_reason
from amorehiremepls.apply.tailoring import Tailor
from amorehiremepls.candidate.profile import BaseResume
from amorehiremepls.core.actions import ActionStore
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import sha256_file, utcnow
from amorehiremepls.records.sync import SyncCoordinator, SyncOutbox, mark_withdrawn
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.fixtures.make_resume import make_base_resume
from tests.fixtures.profile_fixture import build_test_profile
from tests.fixtures.tracker_seed import build_tracker_clone
from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

pytestmark = pytest.mark.mongo

NOW = datetime.now(UTC)


def test_mark_withdrawn_keeps_every_other_row_and_does_not_empty_the_list():
    rows = [
        {"id": "kept", "status": "submitted", "confirmation": "received", "history": [{"status": "submitted"}]},
        {"id": "gone", "status": "prepared", "confirmation": ""},
    ]
    updated = mark_withdrawn(rows, "gone")
    assert updated is not None and len(updated) == 2
    assert updated[0] == rows[0]
    assert updated[1]["status"] == "withdrawn" and updated[1]["history"][0]["status"] == "withdrawn"
    assert mark_withdrawn(updated, "gone")[1]["history"] == updated[1]["history"]
    assert mark_withdrawn(rows, "missing") is None
    with pytest.raises(ValueError):
        mark_withdrawn([], "gone")


def _job(store, job_id="job_demo", status="qualified"):
    store.c("jobs").insert_one(
        {
            "_id": job_id,
            "employer": "Northwind",
            "employer_norm": "northwind",
            "title": "Account Executive",
            "status": status,
            "posting_url": "https://example.test/jobs/1",
            "apply_url": "https://example.test/jobs/1",
            "requisition_id": "RQ1",
            "sources": ["manual"],
            "created_at": NOW,
            "updated_at": NOW,
        }
    )
    return store.c("jobs").find_one({"_id": job_id})


def _app(store, job_id="job_demo", **extra):
    doc = {
        "_id": extra.pop("_id", "app_demo"),
        "job_id": job_id,
        "status": "handoff",
        "reason": "the app cannot fill in forms on example.test yet",
        "provenance": {"origin": "local"},
        "sync": {"state": "not_applicable"},
        "review_package": {"apply_url": "https://example.test/jobs/1", "answers": [], "document": None},
        "material_hash": "hash-1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    doc.update(extra)
    store.c("applications").insert_one(doc)
    return doc


def _service(store, tmp_path):
    profile = build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY)
    base = make_base_resume(tmp_path / "base.docx")
    profile.base_resume = BaseResume(path=str(base), sha256=sha256_file(base), approved_on="2026-11-04", source="test")
    return ApplicationService(store, profile, Tailor(store, tmp_path / "artifacts"), ActionStore(store))


def test_delete_tombstones_an_unsent_application_and_leaves_the_job(store, tmp_path):
    _job(store)
    _app(store)
    kind = _service(store, tmp_path).delete_unsent("app_demo", tmp_path / "backups")
    assert kind == "removed"
    app = store.c("applications").find_one({"_id": "app_demo"})
    assert app["status"] == "removed" and app["tombstone"] and app["provenance"]["origin"] == "removed"
    assert "review_package" not in app
    assert store.c("jobs").find_one({"_id": "job_demo"})["status"] == "qualified"
    assert list(tmp_path.glob("backups/*.json"))
    assert store.db["sync_outbox"].count_documents({}) == 0


def test_delete_with_a_ledger_row_withdraws_locally_and_does_not_mark_the_outbox_pushed(store, tmp_path):
    _job(store)
    _app(store, tracker_record_id="kept-id")
    SyncOutbox(store).enqueue("app_demo", "record_submission")
    store.db["sync_outbox"].update_one({"application_id": "app_demo", "kind": "record_submission"}, {"$set": {"status": "local_only"}})
    kind = _service(store, tmp_path).delete_unsent("app_demo", tmp_path / "backups")
    assert kind == "withdrawn"
    app = store.c("applications").find_one({"_id": "app_demo"})
    assert app["status"] == "withdrawn" and app["provenance"]["origin"] == "withdrawn" and app["tracker_record_id"] == "kept-id"
    old = store.db["sync_outbox"].find_one({"kind": "record_submission"})
    assert old["status"] == "local_only"
    queued = store.db["sync_outbox"].find_one({"kind": "record_withdrawal"})
    assert queued["status"] == "sync_pending"
    assert store.c("jobs").count_documents({"_id": "job_demo"}) == 1


def test_withdrawal_updates_one_ledger_row_and_leaves_the_rest(store, tmp_path):
    clone = build_tracker_clone(tmp_path / "clone", include_missing_artifact=False)
    before = json.loads((clone / "data" / "applications.json").read_text())
    _job(store)
    _app(store, status="withdrawn", tracker_record_id="li-1000000001", provenance={"origin": "withdrawn"})
    item = SyncOutbox(store).enqueue("app_demo", "record_withdrawal")
    other = {
        "_id": "sync_other",
        "application_id": "app_demo",
        "kind": "record_submission",
        "status": "local_only",
        "attempts": 0,
        "created_at": utcnow(),
        "updated_at": utcnow(),
        "history": [],
    }
    store.db["sync_outbox"].insert_one(other)
    result = SyncCoordinator(store, clone, push_enabled=False).process_item(item)
    assert result["status"] == "local_only"
    after = json.loads((clone / "data" / "applications.json").read_text())
    assert len(after) == len(before) and [r["id"] for r in after] == [r["id"] for r in before]
    changed = next(r for r in after if r["id"] == "li-1000000001")
    assert changed["status"] == "withdrawn" and changed["confirmation"] == "Application submitted"
    assert store.db["sync_outbox"].find_one({"_id": "sync_other"})["status"] == "local_only"
    assert store.db["sync_outbox"].find_one({"kind": "record_withdrawal"})["status"] == "local_only"


def test_submitted_and_receipt_applications_are_refused(store, tmp_path):
    _job(store)
    _app(store, _id="app_sent", status="submitted", evidence={"confirmation": "received"})
    _app(store, _id="app_receipt", evidence={"receipt_text": "thank you"})
    svc = _service(store, tmp_path)
    with pytest.raises(ApplicationRefused) as sent:
        svc.delete_unsent("app_sent", tmp_path / "backups")
    with pytest.raises(ApplicationRefused) as receipt:
        svc.restart_unsent("app_receipt")
    assert sent.value.code == "already_sent" and receipt.value.code == "receipt_saved"
    assert store.c("applications").find_one({"_id": "app_sent"})["status"] == "submitted"
    assert store.c("applications").find_one({"_id": "app_receipt"})["evidence"]["receipt_text"] == "thank you"


def test_restart_clears_the_handoff_and_waits_for_approval_without_a_new_submission(store, tmp_path):
    _job(store, status="needs_review")
    _app(store)
    store.c("runs").insert_one({"_id": "run_wait", "kind": "application", "params": {"job_id": "job_demo"}, "status": "waiting", "created_at": NOW, "updated_at": NOW})
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id="job_demo", material_hash="hash-1"))
    app = _service(store, tmp_path).restart_unsent("app_demo")
    assert app["status"] in ("ready_for_review", "needs_input")
    assert app["status"] != "handoff" and "review_package" in app
    assert store.c("runs").find_one({"_id": "run_wait"})["status"] == "cancelled"
    assert store.c("runs").count_documents({"status": {"$in": ["queued", "running"]}}) == 0
    assert not AuthorizationStore(store).active("application_submit")
    assert store.c("actions").count_documents({"idempotency_key": "submit:job_demo"}) == 0
    assert app["next_action"] in ("review and authorize", "supply missing inputs", "read the package and approve again")


@pytest.fixture
def client(store, tmp_path):
    settings = Settings(
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
    return TestClient(create_app(Runtime(settings, store=store)), follow_redirects=False)


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def test_start_over_is_one_button_and_clears_the_unsent_attempt(client, store):
    _job(store, "job_page")
    _app(store, "job_page", _id="app_page")
    csrf = _login(client)
    app_page = client.get("/applications/app_page").text
    job_page = client.get("/jobs/job_page").text
    assert "Start over" in app_page and "Restart this job" not in app_page and "Delete this application" not in app_page
    assert "Type restart to confirm" not in app_page and "Type delete to confirm" not in app_page
    assert "Start over" in job_page and "Type restart to confirm" not in job_page and "Type delete to confirm" not in job_page
    untouched = client.post("/applications/app_page/start-over", data={})
    assert untouched.status_code == 422
    assert store.c("applications").find_one({"_id": "app_page"})["status"] == "handoff"
    sent = client.post("/jobs/job_page/start-over", data={"csrf": csrf})
    assert sent.status_code == 303 and "ok=app_removed" in sent.headers["location"] and sent.headers["location"].startswith("/jobs/job_page")
    assert store.c("jobs").find_one({"_id": "job_page"})
    assert store.c("applications").find_one({"_id": "app_page"})["provenance"]["origin"] == "removed"
    assert submission_lock_reason(store.c("applications").find_one({"_id": "app_page"})) == "already_removed"
    cleared = client.get("/jobs/job_page").text
    assert "Start over" not in cleared
    _job(store, "job_again", status="needs_review")
    _app(store, "job_again", _id="app_again", evidence={"receipt_text": "thank you"})
    kept = client.get("/applications/app_again").text
    assert "Start over" not in kept
    assert "A confirmation from the employer is already saved, so this attempt cannot be cleared." in kept
    refused = client.post("/applications/app_again/start-over", data={"csrf": csrf})
    assert refused.status_code == 303 and refused.headers["location"].endswith("err=receipt_saved")
    assert store.c("applications").find_one({"_id": "app_again"})["evidence"]["receipt_text"] == "thank you"
