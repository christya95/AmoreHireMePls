"""Close a job when its posting is gone, delete tailored resumes that were never sent, and collapse repeated updates.
Fictional employers only. Nothing here submits or touches a live record."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.applications import ApplicationService, delete_tailored_files, remove_unused_resumes
from amorehiremepls.apply.tailoring import Tailor
from amorehiremepls.candidate.profile import BaseResume
from amorehiremepls.core.actions import ActionStore
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import sha256_file, utcnow
from amorehiremepls.jobs.discovery import POSTING_CHECK_BATCH, POSTING_CLOSED_REASON, Discovery, posting_is_gone
from amorehiremepls.jobs.jobs import JobStore
from amorehiremepls.web.chrome import application_timeline
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.fixtures.make_resume import make_base_resume
from tests.fixtures.profile_fixture import build_test_profile
from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY
from tests.test_remove_and_restart import _app, _job, _login

pytestmark = pytest.mark.mongo

NOW = datetime.now(UTC)
LONG = "Permanent role building services for a fictional employer. " * 8


def _profile():
    return build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY)


def _discovery(store):
    return Discovery(store, _profile())


def _tailored(store, tmp_path, job_id="job_demo"):
    root = tmp_path / "artifacts" / "jobs" / job_id
    root.mkdir(parents=True)
    docx = root / "tailored.docx"
    pdf = root / "tailored.pdf"
    docx.write_bytes(b"tailored")
    pdf.write_bytes(b"pdf")
    base = tmp_path / "base.docx"
    base.write_bytes(b"base resume")
    store.c("artifacts").insert_one(
        {
            "_id": "art_tailored",
            "owner_id": job_id,
            "kind": "resume_tailored",
            "path": str(docx),
            "pdf_path": str(pdf),
            "sha256": "docx-hash",
            "pdf_sha256": "pdf-hash",
            "created_at": NOW,
        }
    )
    store.c("artifacts").insert_one({"_id": "art_shot", "owner_id": job_id, "kind": "screenshot", "path": str(root / "shot.png"), "sha256": "shot", "created_at": NOW})
    return docx, pdf, base


def test_a_short_refusal_or_missing_page_counts_as_gone_and_a_long_page_does_not():
    assert posting_is_gone(403, "denied")
    assert posting_is_gone(404, LONG)
    assert posting_is_gone(200, "The page you are looking for does not exist. " + LONG)
    assert posting_is_gone(200, "too short")
    assert not posting_is_gone(200, LONG)
    assert not posting_is_gone(None, "")
    assert not posting_is_gone(500, "short")


def test_repeated_update_lines_collapse_to_one_line_with_a_count_and_the_events_stay():
    app = {"history": [{"at": NOW, "status": "handoff"}, {"at": NOW + timedelta(seconds=1), "status": "handoff"}]}
    runs = [
        {"events": [{"at": NOW + timedelta(seconds=2), "message": "running"}, {"at": NOW + timedelta(seconds=3), "message": "running"}], "interrupt": {"type": "handoff"}},
    ]
    rows = application_timeline(app, runs)
    started = next(row for row in rows if row["text"] == "Task started")
    assert started["count"] == 2
    waiting = next(row for row in rows if row["text"].startswith("Stopped before anything was sent"))
    assert waiting["count"] == 2
    assert len(app["history"]) == 2 and len(runs[0]["events"]) == 2


def test_a_missing_posting_closes_the_job_and_a_sent_application_stays_open(store, tmp_path):
    jobs = JobStore(store)
    job, _ = jobs.upsert(employer="Northwind", title="Account Executive", posting_url="https://jobs.example.test/role", source="url_import")
    _app(store, job["_id"], _id="app_open", status="handoff")
    docx, pdf, base = _tailored(store, tmp_path, job["_id"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash="hash-1"))
    sent, _ = jobs.upsert(employer="Contoso", title="Account Executive", posting_url="https://jobs.example.test/sent", source="url_import")
    _app(store, sent["_id"], _id="app_sent", status="submitted", evidence={"confirmation": "received"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sent"):
            return httpx.Response(404, text="missing")
        return httpx.Response(404, text="The page you are looking for does not exist")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    d = _discovery(store)
    closed = d.check_posting(job["_id"], force=True, client=client, artifact_dir=tmp_path / "artifacts")
    assert closed["outcome"] == "closed" and closed["reason"] == POSTING_CLOSED_REASON
    kept = jobs.get(job["_id"])
    assert kept["status"] == "closed" and kept["closed_reason"] == POSTING_CLOSED_REASON
    assert not docx.exists() and not pdf.exists() and base.exists()
    assert store.c("artifacts").find_one({"_id": "art_tailored"}) is None
    assert store.c("artifacts").find_one({"_id": "art_shot"}) is not None
    assert not AuthorizationStore(store).active("application_submit")
    note = store.c("applications").find_one({"_id": "app_open"})
    assert note["status"] == "handoff"
    assert any(entry.get("text") == POSTING_CLOSED_REASON for entry in note["history"])
    stayed = d.check_posting(sent["_id"], force=True, client=client, artifact_dir=tmp_path / "artifacts")
    assert stayed["outcome"] == "kept_sent"
    assert jobs.get(sent["_id"])["status"] != "closed"
    assert store.c("applications").find_one({"_id": "app_sent"})["status"] == "submitted"


def test_a_live_page_is_kept_a_network_error_does_not_close_and_a_recent_check_is_skipped(store):
    jobs = JobStore(store)
    job, _ = jobs.upsert(employer="Northwind", title="Account Executive", posting_url="https://jobs.example.test/open", source="url_import")
    body = "<html><body><h1>Account Executive</h1><p>" + LONG + "</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/down"):
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    d = _discovery(store)
    opened = d.check_posting(job["_id"], force=True, client=client)
    assert opened["outcome"] == "open"
    assert jobs.get(job["_id"])["status"] == "new"
    assert len((jobs.get(job["_id"]).get("latest_snapshot") or {}).get("text") or "") >= 200
    skipped = d.check_posting(job["_id"], force=False, client=client)
    assert skipped["outcome"] == "skipped"
    again = d.check_posting(job["_id"], force=True, client=client)
    assert again["outcome"] == "open"
    down, _ = jobs.upsert(employer="Fabrikam", title="Account Executive", posting_url="https://jobs.example.test/down", source="url_import")
    unread = d.check_posting(down["_id"], force=True, client=client)
    assert unread["outcome"] == "unreadable" and jobs.get(down["_id"])["status"] == "new"


def test_the_batch_checks_one_at_a_time_up_to_the_cap_and_skips_a_recent_job(store):
    jobs = JobStore(store)
    recent, _ = jobs.upsert(employer="Northwind", title="Recent", posting_url="https://jobs.example.test/recent", source="url_import")
    jobs.set_status(recent["_id"], "qualified", posting_checked_at=utcnow())
    for n in range(POSTING_CHECK_BATCH + 3):
        jobs.upsert(employer="Northwind", title=f"Role {n}", posting_url=f"https://jobs.example.test/n{n}", source="url_import")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<p>" + LONG + "</p>")

    summary = _discovery(store).check_open_postings(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert summary["checked"] == POSTING_CHECK_BATCH
    assert summary["skipped"] >= 1 and summary["open"] == POSTING_CHECK_BATCH
    assert len(seen) == POSTING_CHECK_BATCH
    assert jobs.get(recent["_id"])["status"] == "qualified"


def test_unused_resumes_are_refused_once_a_receipt_exists_and_a_restart_deletes_the_old_file(store, tmp_path):
    _job(store)
    _app(store, status="handoff")
    docx, pdf, base = _tailored(store, tmp_path)
    refused = remove_unused_resumes(store, tmp_path / "artifacts", "job_other")
    assert refused["removed"] == 0
    store.c("applications").update_one({"_id": "app_demo"}, {"$set": {"status": "submitted", "evidence": {"receipt_text": "received"}}})
    blocked = remove_unused_resumes(store, tmp_path / "artifacts", "job_demo")
    assert blocked["refused"] == "already_sent" and docx.exists()
    store.c("applications").update_one({"_id": "app_demo"}, {"$set": {"status": "handoff"}, "$unset": {"evidence": ""}})
    profile = _profile()
    resume = make_base_resume(tmp_path / "real-base.docx")
    profile.base_resume = BaseResume(path=str(resume), sha256=sha256_file(resume), approved_on="2026-11-04", source="test")
    service = ApplicationService(store, profile, Tailor(store, tmp_path / "artifacts"), ActionStore(store))
    service.restart_unsent("app_demo")
    assert not docx.exists() and not pdf.exists() and base.exists() and resume.exists()
    assert store.c("artifacts").find_one({"kind": "resume_tailored", "owner_id": "job_demo", "sha256": "docx-hash"}) is None


def test_a_failed_attempt_reuses_the_same_application(store, tmp_path):
    _job(store, status="needs_review")
    _app(store, status="failed", reason="stopped before anything was sent")
    profile = _profile()
    resume = make_base_resume(tmp_path / "real-base.docx")
    profile.base_resume = BaseResume(path=str(resume), sha256=sha256_file(resume), approved_on="2026-11-04", source="test")
    service = ApplicationService(store, profile, Tailor(store, tmp_path / "artifacts"), ActionStore(store))
    again = service.get_or_create("job_demo")
    assert again["_id"] == "app_demo"
    assert store.c("applications").count_documents({"job_id": "job_demo"}) == 1
    prepared = service.prepare("job_demo", [])
    assert prepared["_id"] == "app_demo"
    assert store.c("applications").count_documents({"job_id": "job_demo"}) == 1


def test_delete_removes_the_tailored_file_and_leaves_the_base_resume(store, tmp_path):
    _job(store)
    _app(store)
    docx, _, base = _tailored(store, tmp_path)
    profile = _profile()
    resume = make_base_resume(tmp_path / "real-base.docx")
    profile.base_resume = BaseResume(path=str(resume), sha256=sha256_file(resume), approved_on="2026-11-04", source="test")
    service = ApplicationService(store, profile, Tailor(store, tmp_path / "artifacts"), ActionStore(store))
    assert delete_tailored_files(store, tmp_path / "artifacts", "job_demo") == 1
    service.delete_unsent("app_demo", tmp_path / "backups")
    assert not docx.exists() and base.exists() and resume.exists()
    assert store.c("jobs").find_one({"_id": "job_demo"})["status"] == "qualified"


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


def test_pages_offer_the_check_and_hide_a_closed_job_from_the_open_list(client, store):
    _job(store, "job_open", status="qualified")
    _app(store, "job_open", _id="app_open")
    _job(store, "job_closed", status="closed")
    store.c("jobs").update_one({"_id": "job_closed"}, {"$set": {"closed_reason": POSTING_CLOSED_REASON}})
    csrf = _login(client)
    page = client.get("/jobs").text
    assert "Check which jobs are still posted" in page
    assert "Remove unused resumes" in page
    assert "job_open" in page and "job_closed" not in page
    closed = client.get("/jobs?status_filter=closed").text
    assert "This posting is no longer up" in closed
    job_page = client.get("/jobs/job_open").text
    assert "Check if this posting is still up" in job_page
    app_page = client.get("/applications/app_open").text
    assert "Remove unused resumes" in app_page and "Type remove to confirm" in app_page
    short = client.post("/applications/app_open/remove-resumes", data={"csrf": csrf, "confirm": "no"})
    assert short.status_code == 303 and short.headers["location"].endswith("err=confirm_needed")
    assert store.c("applications").find_one({"_id": "app_open"})["status"] == "handoff"
