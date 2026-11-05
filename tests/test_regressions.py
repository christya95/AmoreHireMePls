"""Regression tests for defects found by the independent verification pass (2026-11-04)."""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig
from amorehiremepls.core.actions import ActionFailed, ActionStore
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding, material_hash
from amorehiremepls.core.runtime import generic_plan_builder
from amorehiremepls.core.util import utcnow

REPO = Path(__file__).resolve().parents[1]


# -- H1: no private facts in tracked source -----------------------------------------------------------------
# Known private tokens live as SHA-256 digests in tests/fixtures/private_token_hashes.txt so no tracked file
# contains the tokens themselves.


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_tracked_source_contains_no_known_private_tokens():
    hashes_path = REPO / "tests" / "fixtures" / "private_token_hashes.txt"
    hashes = {ln.strip() for ln in hashes_path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert hashes, "hash list missing"
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in tracked:
        p = REPO / rel
        if p.suffix.lower() not in {".py", ".md", ".html", ".toml", ".yml", ".yaml", ".json", ".txt", ".example"} or rel == "tests/fixtures/private_token_hashes.txt":
            continue
        text = p.read_text(encoding="utf-8", errors="ignore").lower().replace(",", "")
        toks = re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", text)
        grams = set(toks) | {f"{a} {b}" for a, b in zip(toks, toks[1:], strict=False)}
        for g in grams:
            if _h(g) in hashes:
                hits.append((rel, g[:3] + "***"))
    assert not hits, f"private tokens present in tracked files: {hits}"


def test_src_contains_no_owner_name():
    """The code must not know who runs it: the original owner's first name (hashed in tests/fixtures/src_only_token_hashes.txt,
    so this file never spells it) may appear in docs and history but never as a literal under src/."""
    hashes = {ln.strip() for ln in (REPO / "tests" / "fixtures" / "src_only_token_hashes.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert hashes, "hash list missing"
    tracked = subprocess.run(["git", "ls-files", "src"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    assert tracked
    hits = []
    for rel in tracked:
        p = REPO / rel
        if p.suffix.lower() not in {".py", ".md", ".html", ".js", ".css", ".txt", ".json", ".toml"}:
            continue
        for word in set(re.findall(r"[a-z0-9]+", p.read_text(encoding="utf-8", errors="ignore").lower())):
            if _h(word) in hashes:
                hits.append((rel, word[:1] + "***"))
    assert not hits, f"owner name present in src/: {hits}"


# -- H2: expired execution lease never re-runs the adapter ----------------------------------------------------
@pytest.mark.mongo
def test_expired_executing_lease_becomes_needs_verification_not_rerun(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"q": "a"}, document_hashes=["abc"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id="job1", material_hash=mat), max_count=5)
    a = acts.prepare("application_submit", "job1", "submit:job1", {}, mat)
    calls: list[str] = []
    second: dict = {}

    def adapter(_doc):
        calls.append("A")
        # lease_seconds=0 means worker A's lease is already expired while it is still working
        second["out"] = acts.execute(a["_id"], "B", lambda d: calls.append("B") or {"verified": True}, lease_seconds=0)
        return {"verified": True, "receipt_text": "thanks"}

    out_a = acts.execute(a["_id"], "A", adapter, lease_seconds=0)
    assert calls == ["A"], "adapter must run exactly once"
    assert second["out"].status == "needs_verification" and "lease expired" in second["out"].reason
    assert out_a.status == "needs_verification" and out_a.adapter_ran
    doc = acts.get(a["_id"])
    assert doc["status"] == "needs_verification" and doc["lease_expired"] and doc["late_outcome"]["status"] == "succeeded"
    # replay afterwards still never re-runs
    assert acts.execute(a["_id"], "C", lambda d: calls.append("C") or {"verified": True}).status == "needs_verification"
    assert calls == ["A"]


@pytest.mark.mongo
def test_sweeper_marks_expired_leases_and_two_workers_with_zero_lease_run_once(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"q": "b"}, document_hashes=["x"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id="job2", material_hash=mat), max_count=5)
    a = acts.prepare("application_submit", "job2", "submit:job2", {}, mat)
    gate = threading.Event()
    calls = []
    results = {}

    def slow_adapter(_doc):
        calls.append(1)
        gate.wait(5)
        return {"verified": True}

    def run(name, adapter):
        results[name] = acts.execute(a["_id"], name, adapter, lease_seconds=0)

    t1 = threading.Thread(target=run, args=("w1", slow_adapter))
    t1.start()
    while not calls:
        pass
    assert acts.sweep_expired_leases() == 1  # housekeeping sees the expired lease
    gate.set()
    t1.join()
    assert len(calls) == 1
    assert acts.get(a["_id"])["status"] == "needs_verification"
    assert results["w1"].status == "needs_verification"


# -- M1: batch scopes only for application_submit with a concrete matching job --------------------------------
@pytest.mark.mongo
def test_batch_scopes_are_not_blanket_authorizations(store):
    auth = AuthorizationStore(store)
    with pytest.raises(ValueError):
        auth.grant("outreach_first_contact", ScopeBinding(job_filters={"employers": ["Acme"]}), mode="batch", max_count=5)
    with pytest.raises(ValueError):
        auth.grant("application_submit", ScopeBinding(), mode="batch", max_count=5)  # no filters
    auth.grant("application_submit", ScopeBinding(job_filters={"employers": ["Acme"]}), mode="batch", max_count=5)
    # no job object -> denied even for the right kind
    assert not auth.check("application_submit", "job1", "m", job=None).allowed
    job = {"_id": "job1", "employer": "Acme", "employer_norm": "acme", "fit": {"rank": 1}}
    assert auth.check("application_submit", "job1", "m", job=job).allowed
    assert not auth.check("application_submit", "job1", "m", job={**job, "employer": "Globex", "employer_norm": "globex"}).allowed
    # a batch scope inserted directly for another kind (bypassing grant) still never authorizes
    store.c("authorizations").update_many({}, {"$set": {"kind": "connection_request"}})
    assert not auth.check("connection_request", "rec1", "m", job=job).allowed


# -- M2: retryable failure returns the action to ready; non-retryable stays failed ----------------------------
@pytest.mark.mongo
def test_retryable_failure_can_be_retried_but_terminal_outcomes_block(store):
    acts = ActionStore(store)
    mat = material_hash(answers={"q": "c"}, document_hashes=["y"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id="job3", material_hash=mat), max_count=5)
    a = acts.prepare("application_submit", "job3", "submit:job3", {}, mat)
    calls = []

    def flaky(_doc):
        calls.append(1)
        if len(calls) == 1:
            raise ActionFailed("timeout before final action", retryable=True)
        return {"verified": True}

    first = acts.execute(a["_id"], "w", flaky)
    assert first.status == "failed" and first.retryable
    doc = acts.get(a["_id"])
    assert doc["status"] == "ready" and len(doc["attempts"]) == 1 and doc["last_failure"]
    second = acts.execute(a["_id"], "w", flaky)
    assert second.status == "succeeded" and len(calls) == 2 and len(acts.get(a["_id"])["attempts"]) == 2
    assert acts.prior_terminal_for_target("application_submit", "job3")["_id"] == a["_id"]
    # non-retryable failure is terminal and replays without the adapter
    b = acts.prepare("application_submit", "job3b", "submit:job3b", {}, mat)
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id="job3b", material_hash=mat), max_count=5)
    calls2 = []

    def hard(_doc):
        calls2.append(1)
        raise ActionFailed("validation error on required field", retryable=False)

    assert acts.execute(b["_id"], "w", hard).status == "failed"
    assert acts.execute(b["_id"], "w", hard).status == "failed" and len(calls2) == 1


# -- M3: requisition is only "verified" when the receipt actually contained it ---------------------------------
def test_generic_plan_requires_requisition_in_receipt_by_default_and_evidence_reflects_matches():
    job = {"_id": "j", "requisition_id": "R-77", "form_plan": {"fields": [], "submit_selector": "#go"}}
    pkg = {"apply_url": "https://careers.example/apply"}
    plan = generic_plan_builder(job, pkg, [])
    assert plan.receipt_must_include == ["R-77"]
    job["form_plan"]["receipt_includes_requisition"] = False
    assert generic_plan_builder(job, pkg, []).receipt_must_include == []


def test_normalize_screenshots_widens_legacy_paths_and_keeps_structured_entries():
    from amorehiremepls.apply.browser import normalize_screenshots

    structured = {"label": "summary", "path": "/a/x_summary.png", "sha256": "ab", "captured_at": "t", "url": "u"}
    legacy = ["/old/20261101T120000_receipt.png", "/old/20261101T120001_receipt_mismatch.png"]
    out = normalize_screenshots([*legacy, structured, {"path": "/a/y_ambiguous.png"}, "", {"label": "no path"}])
    assert out == [
        {"label": "receipt", "path": "/old/20261101T120000_receipt.png", "sha256": None, "captured_at": None, "url": None},
        {"label": "receipt_mismatch", "path": "/old/20261101T120001_receipt_mismatch.png", "sha256": None, "captured_at": None, "url": None},
        structured,
        {"label": "ambiguous", "path": "/a/y_ambiguous.png", "sha256": None, "captured_at": None, "url": None},
    ]
    assert normalize_screenshots(None) == [] and normalize_screenshots([]) == []


@pytest.mark.mongo
def test_submission_evidence_lists_only_matched_identifiers(store, tmp_path):
    from amorehiremepls.apply.browser import Plan
    from tests.test_applications_and_sync import FIELDS, _job, _profile, _service

    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="x")}
    svc = _service(store, tmp_path, profile, builders)
    for employer, req, matched in (("Acme", "R-900", ["R-900"]), ("Globex", "R-901", None)):
        job = _job(store, employer=employer, req=req)
        app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
        AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
        ev_in = {"verified": True, "receipt_text": "Thank you", "final_url": "u", "screenshots": []}
        if matched is not None:
            ev_in["matched_identifiers"] = matched
        assert svc.execute(app["_id"], "w1", lambda plan, e=ev_in: e).status == "succeeded"
        ev = store.c("applications").find_one({"_id": app["_id"]})["evidence"]
        assert ev["verified_identifiers"] == (matched or []) and ev["expected_identifiers"] == [req]


# -- M4: hard crash while running is recoverable -----------------------------------------------------------------
@pytest.mark.mongo
def test_worker_startup_marks_orphaned_running_runs_interrupted_then_resumable(store, tmp_path):
    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.runtime import Runtime
    from amorehiremepls.core.worker import Worker
    from tests.conftest import TEST_MONGO_URI

    s = Settings(mongodb_uri=TEST_MONGO_URI, mongodb_database=store.db.name, artifact_dir=tmp_path / "a", private_dir=tmp_path / "p", log_dir=tmp_path / "l", _env_file=None)
    rt = Runtime(s, store=store)
    orphan = store.create_run("application", {"job_id": "job_x"})
    store.c("runs").update_one({"_id": orphan["_id"]}, {"$set": {"status": "running", "reserved_by": "dead-host:1", "reserved_until": utcnow() + timedelta(hours=1)}})
    live = store.create_run("application", {"job_id": "job_y"})
    store.c("runs").update_one({"_id": live["_id"]}, {"$set": {"status": "running", "reserved_by": "alive:2", "reserved_until": utcnow() + timedelta(hours=1)}})
    store.set_control("worker_heartbeat:alive:2", {"at": utcnow()})
    w = Worker(rt)
    assert w.recover_interrupted_runs() == 1
    o = store.c("runs").find_one({"_id": orphan["_id"]})
    assert o["status"] == "failed" and o["error"].startswith("interrupted") and "reserved_by" not in o
    assert store.c("runs").find_one({"_id": live["_id"]})["status"] == "running"
    # a failed run accepts the resume request path used by the dashboard
    store.update_run(orphan["_id"], requested="resume")
    assert store.c("runs").find_one({"_id": orphan["_id"], "requested": "resume", "status": "failed"}) is not None


# -- M5: rebase conflict aborts and surfaces as conflict ----------------------------------------------------------
@pytest.mark.mongo
def test_rebase_conflict_aborts_cleanly_and_marks_outbox_conflict(store, tmp_path):
    import json

    from amorehiremepls.records.sync import git
    from tests.test_applications_and_sync import _coord, _profile, _submitted_app

    _submitted_app(store, tmp_path, _profile(tmp_path))
    coord, clone, bare = _coord(store, tmp_path)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    assert coord.process_pending()[0]["status"] == "local_only"  # ledger commit exists locally only
    # remote moves ahead with a conflicting rewrite of the same ledger file
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    data = other / "data" / "applications.json"
    recs = json.loads(data.read_text())
    recs.append({"id": "remote-added", "employer": "Remote Co", "role": "Dev", "status": "researched"})
    data.write_text(json.dumps(recs, indent=4), encoding="utf-8")
    git(other, "add", "data/applications.json")
    git(other, "commit", "-q", "-m", "conflicting remote edit")
    git(other, "push", "-q", "origin", "main")
    git(clone, "remote", "set-url", "origin", str(bare))
    store.c("sync_outbox").update_many({}, {"$set": {"next_attempt_at": store.c("sync_outbox").find_one({})["created_at"]}})
    res = coord.process_pending()
    assert res[0]["status"] == "conflict" and "rebase conflict" in res[0]["error"], res
    assert not (clone / ".git" / "rebase-merge").exists() and not (clone / ".git" / "rebase-apply").exists()
    assert git(clone, "status", "--porcelain", "--", "data") == ""  # clean tree, nothing half-applied
    assert store.c("sync_outbox").find_one({})["status"] == "conflict"
    assert store.c("applications").find_one({})["sync"]["state"] == "conflict"
    assert git(bare, "rev-list", "--count", "main") == "2"  # nothing force-pushed; remote edit intact


# -- L6: uploads constrained to the artifact directory ---------------------------------------------------------------
def test_upload_paths_must_be_under_artifact_dir(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "ok.docx").write_bytes(b"x")
    outside = tmp_path / "secret.docx"
    outside.write_bytes(b"y")
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts=["127.0.0.1"], upload_root=root))
    assert adapter.check_upload_path(str(root / "ok.docx")) == (root / "ok.docx").resolve()
    with pytest.raises(ActionFailed, match="outside artifact directory"):
        adapter.check_upload_path(str(outside))
    with pytest.raises(ActionFailed, match="outside artifact directory"):
        adapter.check_upload_path(str(root / ".." / "secret.docx"))


# -- L3: concurrent upserts of the same job identity never create duplicates --------------------------------------
@pytest.mark.mongo
def test_concurrent_upserts_of_same_identity_create_one_job(store):
    from amorehiremepls.jobs.jobs import JobStore

    jobs = JobStore(store)
    results = []

    def worker(i):
        job, created = jobs.upsert(employer="Acme", title="Backend Developer", posting_url=f"https://ats.example/jobs/R1?utm_source={i}", requisition_id="R1", location="Toronto")
        results.append((job["_id"], created))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len({r[0] for r in results}) == 1 and sum(1 for r in results if r[1]) == 1
    assert store.c("jobs").count_documents({"requisition_id": "R1"}) == 1


# -- cancel requested on an idle (waiting) run is settled by the worker tick --------------------------------------
@pytest.mark.mongo
def test_cancel_request_on_waiting_run_is_settled_by_worker_tick(store, tmp_path):
    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.runtime import Runtime
    from amorehiremepls.core.worker import Worker
    from tests.conftest import TEST_MONGO_URI

    s = Settings(mongodb_uri=TEST_MONGO_URI, mongodb_database=store.db.name, artifact_dir=tmp_path / "a", private_dir=tmp_path / "p", log_dir=tmp_path / "l", _env_file=None)
    rt = Runtime(s, store=store)
    run = store.create_run("application", {"job_id": "job_idle"})
    store.update_run(run["_id"], status="waiting", interrupt={"type": "needs_input"}, requested="cancel")
    Worker(rt).tick()
    doc = store.c("runs").find_one({"_id": run["_id"]})
    assert doc["status"] == "cancelled" and doc.get("requested") is None and doc.get("interrupt") is None
