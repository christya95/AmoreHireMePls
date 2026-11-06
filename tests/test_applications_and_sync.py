import json
import subprocess
from pathlib import Path

import pytest

from amorehiremepls.apply.applications import ApplicationService, FormField, map_answers
from amorehiremepls.apply.browser import Plan, Step
from amorehiremepls.apply.tailoring import Tailor
from amorehiremepls.candidate.profile import BandEvidence, BaseResume
from amorehiremepls.core.actions import ActionFailed, ActionStore, AmbiguousOutcome
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.core.util import sha256_file
from amorehiremepls.jobs.jobs import JobStore
from amorehiremepls.records.sync import SyncCoordinator, SyncOutbox, build_record, git
from tests.fixtures.make_resume import make_base_resume
from tests.fixtures.profile_fixture import build_test_profile
from tests.fixtures.tracker_seed import build_bare_remote, build_tracker_clone
from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

pytestmark = pytest.mark.mongo

FIELDS = [
    FormField("fn", "First name", required=True),
    FormField("ln", "Last name"),
    FormField("em", "Email"),
    FormField("ph", "Phone"),
    FormField("wa", "Are you legally authorized to work in Canada?", "select", ["Select", "Yes, I am authorized", "No"]),
    FormField("sp", "Will you require sponsorship?", "select", ["Yes", "No"]),
    FormField("sal", "Desired salary"),
    FormField("res", "Resume/CV", "file"),
    FormField("gen", "Gender identity (optional)", "select", ["Man", "Woman", "Decline"]),
    FormField("why", "Why do you want to work here?", "textarea", required=True),
    FormField("yrs", "Years of professional experience"),
]


def _profile(tmp_path, with_base=True):
    p = build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY)
    p.version = 3
    if with_base:
        base = make_base_resume(tmp_path / "base.docx")
        p.base_resume = BaseResume(path=str(base), sha256=sha256_file(base), approved_on="2026-11-04", source="test")
    return p


def test_answers_are_grounded_and_unknowns_flagged(tmp_path):
    p = _profile(tmp_path)
    band = BandEvidence(min_base=110000, max_base=130000, currency="CAD", title_matches=True, location_matches=True)
    answers = {a.field_id: a for a in map_answers(FIELDS, p, {}, "/x/resume.pdf", band)}
    assert answers["fn"].value == "Alex" and answers["em"].value == "alex@example.com"
    assert answers["wa"].value == "Yes, I am authorized" and answers["sp"].value == "No"
    # the band midpoint (120000) sits under the fictional floor (123000), so the figure is raised to the floor, never below it
    assert answers["sal"].value == "123000" and "raised to your minimum" in answers["sal"].reason
    assert answers["gen"].value is None and answers["gen"].sensitive and not answers["gen"].needs_input
    assert answers["why"].needs_input
    assert answers["yrs"].value == "3"
    no_band = {a.field_id: a for a in map_answers(FIELDS, p, {}, None, None)}
    assert no_band["sal"].needs_input and no_band["res"].needs_input


def _service(store, tmp_path, profile, builders=None):
    return ApplicationService(store, profile, Tailor(store, tmp_path / "artifacts"), ActionStore(store), builders)


def _job(store, **over):
    jobs = JobStore(store)
    req = over.get("req", "R42")
    url = over.get("url", f"https://ats.example/jobs/{req}")
    job, _ = jobs.upsert(
        employer=over.get("employer", "Acme"),
        title="Backend Developer",
        posting_url=url,
        requisition_id=req,
        location="Toronto",
        snapshot={"text": "Java Spring Boot permanent Toronto", "posting_hash": "ph1", "extracted": {"salary_min": 110000, "salary_max": 130000, "salary_currency": "CAD"}},
    )
    jobs.set_status(job["_id"], "qualified", apply_url=url, fit={"rank": 2, "genuine_gaps": ["Kubernetes"]})
    return jobs.get(job["_id"])


def test_supply_inputs_replaces_an_existing_answer_and_keeps_unchanged_ones(store, tmp_path):
    svc = _service(store, tmp_path, _profile(tmp_path))
    app = svc.prepare(_job(store)["_id"], FIELDS)
    app = svc.supply_inputs(app["_id"], {"why": "I enjoy building reliable backend services."})
    before = app["material_hash"]
    answers = {a["field_id"]: a for a in app["review_package"]["answers"]}
    assert "Yes, I am authorized" in answers["wa"]["options"] and answers["why"]["required"]
    app = svc.supply_inputs(app["_id"], {"why": "I enjoy building reliable backend services.", "fn": "Jordan"})
    answers = {a["field_id"]: a for a in app["review_package"]["answers"]}
    assert answers["fn"]["value"] == "Jordan" and answers["fn"]["fact_ids"] == ["user:input"] and answers["fn"]["reason"] == "edited by you"
    assert answers["why"]["reason"] == "supplied by you" and app["material_hash"] != before
    store.c("applications").update_one({"_id": app["_id"]}, {"$set": {"status": "authorized"}})
    same = svc.supply_inputs(app["_id"], {"why": "I enjoy building reliable backend services.", "fn": "Jordan"})
    assert same["status"] == "authorized"
    app = svc.supply_inputs(app["_id"], {"why": ""})
    answers = {a["field_id"]: a for a in app["review_package"]["answers"]}
    assert answers["why"]["value"] is None and answers["why"]["needs_input"] and app["status"] == "needs_input"


def test_prepare_requires_inputs_then_changed_document_invalidates_authorization(store, tmp_path):
    profile = _profile(tmp_path)
    svc = _service(store, tmp_path, profile)
    job = _job(store)
    app = svc.prepare(job["_id"], FIELDS)
    assert app["status"] == "needs_input" and any("Why do you want" in o for o in app["review_package"]["outstanding"])
    assert app["review_package"]["document"]["sha256"]
    app = svc.supply_inputs(app["_id"], {"why": "I enjoy building reliable backend services."})
    assert app["status"] == "ready_for_review"
    mat1 = app["material_hash"]
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=mat1))
    # Joss approves an updated base resume -> new document bytes -> new material hash -> old scope no longer matches
    from docx import Document

    d = Document(profile.base_resume.path)
    d.add_paragraph("Mentored two junior developers on code review practices.", style="List Bullet")
    new_base = tmp_path / "base_v2.docx"
    d.save(str(new_base))
    profile.base_resume = BaseResume(path=str(new_base), sha256=sha256_file(new_base), approved_on="2026-11-05", source="test")
    profile.version = 4
    svc2 = _service(store, tmp_path, profile)
    app2 = svc2.prepare(job["_id"], FIELDS)
    app2 = svc2.supply_inputs(app2["_id"], {"why": "I enjoy building reliable backend services."})
    assert app2["material_hash"] != mat1
    out = svc2.execute(app2["_id"], "w1", run_adapter=lambda plan: {"verified": True})
    assert out.status == "handoff"  # no builder for ats.example -> manual handoff regardless
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="x")}
    svc3 = _service(store, tmp_path, profile, builders)
    svc3.apps.update_one({"_id": app2["_id"]}, {"$set": {"status": "ready_for_review"}})
    out = svc3.execute(app2["_id"], "w1", run_adapter=lambda plan: {"verified": True})
    assert out.status == "denied" and "material content changed" in out.reason


def test_replaying_a_failed_submit_keeps_the_saved_reason(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="received")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store, req="R-replay")
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    saved = "The location box did not show a place for Lakeside, Northland, then Lakeside"
    calls = []

    def fail(_plan):
        calls.append(1)
        raise ActionFailed(saved, {"value": "Lakeside, Northland"}, retryable=False)

    first = svc.execute(app["_id"], "w1", fail)
    assert first.status == "failed" and first.reason == saved and len(calls) == 1
    again = svc.execute(app["_id"], "w2", fail)
    assert again.status == "failed" and again.reason == saved and not again.adapter_ran and len(calls) == 1
    stored = svc.apps.find_one({"_id": app["_id"]})
    assert stored["reason"] == saved
    failed = [entry.get("reason") for entry in stored["history"] if entry.get("status") == "failed"]
    assert failed == [saved]
    assert JobStore(store).get(job["_id"])["status"] == "qualified"


def test_a_replayed_failure_is_no_longer_left_approved(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="received")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store, req="R-approved-fail")
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    saved = "The location box did not show a place for Lakeside, Northland, then Lakeside"
    calls = []

    def fail(_plan):
        calls.append(1)
        raise ActionFailed(saved, {"value": "Lakeside, Northland"}, retryable=False)

    first = svc.execute(app["_id"], "w1", fail)
    assert first.status == "failed" and len(calls) == 1
    svc.apps.update_one({"_id": app["_id"]}, {"$set": {"status": "authorized"}})
    again = svc.execute(app["_id"], "w2", fail)
    stored = svc.apps.find_one({"_id": app["_id"]})
    assert again.status == "failed" and not again.adapter_ran and len(calls) == 1
    assert stored["status"] == "failed" and stored["reason"] == saved
    assert all("recorded outcome" not in str(entry.get("reason") or "") for entry in stored["history"])


def test_typed_answers_survive_a_rebuild_and_a_new_attempt_uses_them(store, tmp_path):
    """Profile city and province are only the default. A value typed on the application wins for every field."""
    from amorehiremepls.candidate.candidate_profile import apply_profile_toml, load_profile_toml
    from amorehiremepls.candidate.profile import Fact, Profile, Provenance

    def place(profile, city, province):
        found = set()
        prov = Provenance(source="test", confirmed_on="2026-11-06")
        for fact in profile.facts:
            if fact.key in ("city", "province"):
                fact.value = city if fact.key == "city" else province
                found.add(fact.key)
        for key, value in (("city", city), ("province", province)):
            if key not in found:
                profile.facts.append(Fact(category="address", key=key, value=value, provenance=prov))
        return profile

    example = Path(__file__).resolve().parents[1] / "candidate" / "profile.example.toml"
    sales = Profile()
    apply_profile_toml(sales, load_profile_toml(example), confirmed_on="2026-11-06")
    generic = [
        FormField("city", "City", required=True),
        FormField("region", "Province", required=True),
        FormField("note", "Note for the team", required=True),
    ]
    for name, profile in (("tech", place(_profile(tmp_path, with_base=False), "Lakeside", "Northland")), ("sales", place(sales, "Lakeside", "Northland"))):
        svc = _service(store, tmp_path, profile)
        job = _job(store, req=f"R-{name}", url=f"https://ats.example/jobs/{name}")
        app = svc.prepare(job["_id"], generic)
        got = {a["field_id"]: a["value"] for a in app["review_package"]["answers"]}
        assert got["city"] == "Lakeside" and got["region"] == "Northland", name
        svc.supply_inputs(app["_id"], {"city": "Harbor", "note": "Ready to start"})
        rebuilt = svc.prepare(job["_id"], generic)
        got = {a["field_id"]: a for a in rebuilt["review_package"]["answers"]}
        assert got["city"]["value"] == "Harbor" and got["city"]["fact_ids"] == ["user:input"], name
        assert got["note"]["value"] == "Ready to start", name
        assert got["region"]["value"] == "Northland", name
        assert rebuilt["material_hash"] != app["material_hash"]

    profile = place(_profile(tmp_path, with_base=False), "Lakeside", "Northland")
    fields = [
        FormField("first_name", "First Name", required=True, meta={"ats": "greenhouse", "name": "first_name", "section": "questions"}),
        FormField("email", "Email", required=True, meta={"ats": "greenhouse", "name": "email", "section": "questions"}),
        FormField("phone", "Phone", required=True, meta={"ats": "greenhouse", "name": "phone", "section": "questions"}),
        FormField("location", "Location (City)", required=True, meta={"ats": "greenhouse", "name": "location", "section": "location"}),
        FormField("note", "Note for the team", required=True, meta={"ats": "greenhouse", "name": "note", "section": "questions"}),
    ]
    svc = _service(store, tmp_path, profile)
    job = _job(store, req="R-gh", url="https://job-boards.greenhouse.io/acme/jobs/424242")
    app = svc.prepare(job["_id"], fields)
    first_answers = {a["field_id"]: a["value"] for a in app["review_package"]["answers"]}
    assert first_answers["location"] == "Lakeside, Northland"
    assert first_answers["first_name"] == "Alex" and first_answers["email"] == "alex@example.com"
    svc.apps.update_one({"_id": app["_id"]}, {"$set": {"status": "failed"}})
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    calls = []

    def run(plan):
        loc = next(step.value for step in plan.steps if step.type == "autocomplete")
        calls.append(loc)
        raise ActionFailed("no list appeared", {"location_typed": loc, "value": loc}, retryable=False)

    first = svc.execute(app["_id"], "w1", run)
    assert first.adapter_ran and calls == ["Lakeside, Northland"]
    svc.supply_inputs(app["_id"], {"location": "Harbor", "first_name": "River", "note": "Ready to start", "phone": ""})
    rebuilt = svc.prepare(job["_id"], fields)
    got = {a["field_id"]: a for a in rebuilt["review_package"]["answers"]}
    assert got["location"]["value"] == "Harbor" and got["location"]["fact_ids"] == ["user:input"]
    assert got["first_name"]["value"] == "River" and got["note"]["value"] == "Ready to start"
    assert got["phone"]["value"] is None and got["email"]["value"] == "alex@example.com"
    assert "Lakeside, Northland" not in str(got["location"]["value"])
    svc.apps.update_one({"_id": rebuilt["_id"]}, {"$set": {"status": "failed"}})
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=rebuilt["material_hash"]))
    second = svc.execute(rebuilt["_id"], "w2", run)
    assert second.adapter_ran and calls == ["Lakeside, Northland", "Harbor"]
    assert second.evidence.get("location_typed") == "Harbor"
    kept = svc.actions.get(first.action_id)
    assert kept["status"] == "failed" and kept["evidence"]["location_typed"] == "Lakeside, Northland"
    assert ":prior:" in kept["idempotency_key"]


def test_a_changed_answer_starts_a_new_attempt_and_keeps_the_old_failure(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="received")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store, req="R-changed")
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    calls = []

    def fail(_plan):
        calls.append("old")
        raise ActionFailed("no suggestions appeared for Lakeside", {"value": "Lakeside", "location_typed": "Lakeside"}, retryable=False)

    first = svc.execute(app["_id"], "w1", fail)
    assert first.status == "failed" and first.adapter_ran and calls == ["old"]
    changed = svc.supply_inputs(app["_id"], {"fn": "River"})
    assert changed["material_hash"] != app["material_hash"]
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=changed["material_hash"]))

    def succeed(_plan):
        calls.append("new")
        return {"verified": True, "receipt_text": "received", "final_clicked": True}

    second = svc.execute(changed["_id"], "w2", succeed)
    assert second.status == "succeeded" and second.adapter_ran and calls == ["old", "new"]
    kept = svc.actions.get(first.action_id)
    assert kept["status"] == "failed" and f":prior:{first.action_id}" in kept["idempotency_key"]
    current = svc.actions.by_key(f"submit:{job['_id']}")
    assert current["_id"] != first.action_id and current["material_hash"] == changed["material_hash"]
    again = svc.execute(changed["_id"], "w3", succeed)
    assert again.status == "succeeded" and not again.adapter_ran and calls == ["old", "new"]


def test_no_base_resume_surfaces_question_not_old_resume(store, tmp_path):
    svc = _service(store, tmp_path, _profile(tmp_path, with_base=False))
    app = svc.prepare(_job(store)["_id"], FIELDS)
    assert app["status"] == "needs_input" and "approved base resume not selected" in app["review_package"]["outstanding"]
    assert app["review_package"]["document"] is None


def test_execute_success_records_evidence_and_enqueues_sync_and_replay_is_safe(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {
        "ats.example": lambda job, pkg, answers: Plan(
            start_url=pkg["apply_url"], steps=[Step("click", "#submit", final=True)], receipt_text="received", receipt_must_include=["R42"]
        )
    }
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store)
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    assert app["status"] == "ready_for_review"
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    runs = []

    def fake_browser(plan: Plan) -> dict:
        runs.append(plan.start_url)
        return {
            "verified": True,
            "receipt_text": "Application received for requisition R42. Reference REF-1",
            "final_url": plan.start_url + "/done",
            "screenshots": [],
            "final_clicked": True,
        }

    out = svc.execute(app["_id"], "w1", fake_browser)
    assert out.status == "succeeded"
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "submitted" and app["evidence_basis"] == "employer_status_reviewed" and app["sync"]["state"] == "sync_pending"
    assert store.c("sync_outbox").count_documents({"application_id": app["_id"]}) == 1
    assert JobStore(store).get(job["_id"])["status"] == "applied"
    # replay (restart after submission) does not run the browser again
    again = svc.execute(app["_id"], "w2", fake_browser)
    assert again.status == "succeeded" and len(runs) == 1
    # preparing the same job again is a duplicate, not a second application
    assert svc.prepare(job["_id"], FIELDS)["status"] == "duplicate"


def _png(path: Path, payload: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)
    label = path.stem.rsplit("_", 1)[-1]
    return {"label": label, "path": str(path), "sha256": sha256_file(path), "captured_at": "2026-11-05T06:00:00+00:00", "url": f"https://ats.example/{label}"}


def test_structured_screenshots_are_recorded_registered_as_artifacts_and_archived_by_sync(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[Step("click", "#submit", final=True)], receipt_text="received")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store)
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    shot_dir = tmp_path / "artifacts" / "jobs" / job["_id"] / "screenshots"
    seen_job_ids = []

    def fake_browser(plan: Plan) -> dict:
        seen_job_ids.append(plan.job_id)
        shots = [_png(shot_dir / f"20261105T060000_{job['_id']}_summary.png", b"review page"), _png(shot_dir / f"20261105T060002_{job['_id']}_receipt.png", b"thank you")]
        return {"verified": True, "receipt_text": "Application received for requisition R42.", "final_url": "https://ats.example/done", "screenshots": shots, "final_clicked": True}

    assert svc.execute(app["_id"], "w1", fake_browser).status == "succeeded"
    assert seen_job_ids == [job["_id"]]  # the service fills the plan's job id so the adapter can name/place the files
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["evidence_basis"] == "receipt_screenshot_reviewed"
    shots = app["evidence"]["screenshots"]
    assert [s["label"] for s in shots] == ["summary", "receipt"]
    for s in shots:
        assert set(s) == {"label", "path", "sha256", "captured_at", "url", "artifact_id"}
        assert Path(s["path"]).is_file() and sha256_file(Path(s["path"])) == s["sha256"] and s["url"].endswith(s["label"])
        art = store.c("artifacts").find_one({"_id": s["artifact_id"]})
        assert art["kind"] == "screenshot" and art["owner_id"] == job["_id"] == art["job_id"] and art["application_id"] == app["_id"]
        assert art["label"] == s["label"] and art["sha256"] == s["sha256"] and art["path"] == s["path"] and art["filename"] == Path(s["path"]).name
        assert art["media_type"] == "image/png" and art["captured_at"] == s["captured_at"] and art["created_at"]
    assert store.c("artifacts").count_documents({"kind": "screenshot", "application_id": app["_id"]}) == 2
    # registering again (replay / re-read) does not duplicate artifact rows
    assert [s["artifact_id"] for s in svc.register_screenshots(job["_id"], app["_id"], shots)] == [s["artifact_id"] for s in shots]
    assert store.c("artifacts").count_documents({"kind": "screenshot", "application_id": app["_id"]}) == 2

    coord, clone, bare = _coord(store, tmp_path)
    res = coord.process_pending()
    assert res and res[0]["status"] == "synced", res
    rec = next(r for r in json.loads((clone / "data/applications.json").read_text()) if r["id"] == "acme-R42")
    kinds = {a["kind"]: a for a in rec["artifacts"]}
    assert set(kinds) == {"resume_submitted", "receipt", "summary_screenshot"}
    by_label = {s["label"]: s for s in shots}
    for kind, label in (("receipt", "receipt"), ("summary_screenshot", "summary")):
        a = kinds[kind]
        assert a["path"].startswith("artifacts/acme-R42/") and a["path"].endswith(f"-acme-R42_{label}.png") and a["original_filename"] == f"acme-R42_{label}.png"
        assert a["sha256"] == by_label[label]["sha256"] == sha256_file(clone / a["path"])
    assert git(bare, "log", "-1", "--format=%s") == "Record confirmed application acme-R42" and not git(clone, "status", "--porcelain")
    assert "summary_screenshot" in (clone / "records" / "acme-R42.md").read_text()


def test_legacy_string_screenshots_are_normalised_and_hashed_on_record(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="x")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store)
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    old = tmp_path / "artifacts" / "screenshots" / "20261101T120000_receipt.png"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"\x89PNG old-style")
    missing = str(tmp_path / "artifacts" / "screenshots" / "20261101T115959_summary.png")  # never written
    legacy = {"verified": True, "receipt_text": "Application received for requisition R42.", "final_url": "u", "screenshots": [missing, str(old)]}
    assert svc.execute(app["_id"], "w1", lambda p: legacy).status == "succeeded"
    app = svc.apps.find_one({"_id": app["_id"]})
    shots = app["evidence"]["screenshots"]
    assert [(s["label"], s["path"], s["sha256"], s["captured_at"], s["url"]) for s in shots] == [
        ("summary", missing, None, None, None),
        ("receipt", str(old), sha256_file(old), None, None),
    ]
    assert "artifact_id" not in shots[0] and store.c("artifacts").count_documents({"kind": "screenshot", "application_id": app["_id"]}) == 1
    assert app["evidence_basis"] == "receipt_screenshot_reviewed"


def test_ambiguous_outcome_keeps_summary_and_ambiguous_screenshots(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="x")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store)
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    shot_dir = tmp_path / "artifacts" / "jobs" / job["_id"] / "screenshots"
    shots = [_png(shot_dir / f"20261105T060000_{job['_id']}_summary.png", b"review"), _png(shot_dir / f"20261105T060010_{job['_id']}_ambiguous.png", b"spinner")]

    def timeout(plan):
        raise AmbiguousOutcome("timeout after final click", {"final_clicked": True, "screenshots": shots})

    assert svc.execute(app["_id"], "w1", timeout).status == "needs_verification"
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "needs_verification" and [s["label"] for s in app["evidence"]["screenshots"]] == ["summary", "ambiguous"]
    assert all(s.get("artifact_id") for s in app["evidence"]["screenshots"])
    labels = sorted(a["label"] for a in store.c("artifacts").find({"kind": "screenshot", "application_id": app["_id"]}))
    assert labels == ["ambiguous", "summary"]


def test_ambiguous_submission_needs_verification_then_reconcile(store, tmp_path):
    profile = _profile(tmp_path)
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="x")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store)
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))

    def timeout(plan):
        raise AmbiguousOutcome("timeout after final click", {"final_clicked": True})

    assert svc.execute(app["_id"], "w1", timeout).status == "needs_verification"
    calls = []
    assert svc.execute(app["_id"], "w1", lambda p: calls.append(1) or {"verified": True}).status == "needs_verification" and not calls
    app = svc.reconcile(app["_id"], "succeeded", {"confirmation": "Candidate Home shows Submitted", "application_date": "2026-11-04"})
    assert app["status"] == "submitted" and app["evidence_basis"] == "explicit_user_confirmation"


# ---------------- tracker sync ----------------


def _submitted_app(store, tmp_path, profile, employer="Acme", req="R42"):
    builders = {"ats.example": lambda job, pkg, answers: Plan(start_url=pkg["apply_url"], steps=[], receipt_text="x")}
    svc = _service(store, tmp_path, profile, builders)
    job = _job(store, employer=employer, req=req)
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    svc.execute(app["_id"], "w1", lambda p: {"verified": True, "receipt_text": f"Application received for requisition {req}.", "final_url": "u", "screenshots": []})
    return svc.apps.find_one({"_id": app["_id"]})


def _coord(store, tmp_path, push=True):
    clone = build_tracker_clone(tmp_path / "clone", include_missing_artifact=False)
    bare = build_bare_remote(tmp_path / "remote.git", clone)
    return SyncCoordinator(store, clone, push_enabled=push), clone, bare


def test_sync_writes_record_runs_validation_commits_only_tracker_paths_and_pushes(store, tmp_path):
    app = _submitted_app(store, tmp_path, _profile(tmp_path))
    coord, clone, bare = _coord(store, tmp_path)
    (clone / "unrelated.txt").write_text("keep me unstaged\n")  # unrelated change must survive untouched
    res = coord.process_pending()
    assert res and res[0]["status"] == "synced", res
    data = json.loads((clone / "data/applications.json").read_text())
    rec = next(r for r in data if r["id"] == "acme-R42")
    assert rec["status"] == "submitted" and rec["evidence_basis"] == "employer_status_reviewed" and rec["artifacts"][0]["kind"] == "resume_submitted"
    assert "AmoreHireMePls" in rec["source_log"]
    assert (clone / "unrelated.txt").exists() and "unrelated.txt" in git(clone, "status", "--porcelain")
    assert git(bare, "log", "-1", "--format=%s") == "Record confirmed application acme-R42"
    app = store.c("applications").find_one({"_id": app["_id"]})
    assert app["sync"]["state"] == "synced" and app["tracker_record_id"] == "acme-R42" and len(app["sync"]["commit_sha"]) == 40
    # second pass: nothing pending, no duplicate records
    assert coord.process_pending() == []
    assert sum(1 for r in json.loads((clone / "data/applications.json").read_text()) if r["id"] == "acme-R42") == 1


def test_push_failure_leaves_local_only_and_retry_succeeds_without_duplicate(store, tmp_path):
    app = _submitted_app(store, tmp_path, _profile(tmp_path))
    coord, clone, bare = _coord(store, tmp_path)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    res = coord.process_pending()
    assert res[0]["status"] == "local_only"
    assert store.c("applications").find_one({"_id": app["_id"]})["sync"]["state"] == "local_only"
    assert store.c("applications").find_one({"_id": app["_id"]})["status"] == "submitted"  # sync failure is not application failure
    git(clone, "remote", "set-url", "origin", str(bare))
    store.c("sync_outbox").update_many({}, {"$set": {"next_attempt_at": store.c("sync_outbox").find_one({})["created_at"]}})
    res = coord.process_pending()
    assert res[0]["status"] == "synced"
    assert git(bare, "rev-list", "--count", "main") == "2"


def test_remote_moved_ahead_is_rebased_not_force_pushed(store, tmp_path):
    _submitted_app(store, tmp_path, _profile(tmp_path))
    coord, clone, bare = _coord(store, tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "README.md").write_text("# fixture tracker\nremote edit\n")
    git(other, "add", "README.md")
    git(other, "commit", "-q", "-m", "remote edit")
    git(other, "push", "-q", "origin", "main")
    res = coord.process_pending()
    assert res[0]["status"] == "synced"
    assert git(bare, "rev-list", "--count", "main") == "3"
    assert "remote edit" in git(clone, "log", "--format=%s")


def test_crash_between_commit_and_db_update_reconciles_without_duplicate(store, tmp_path):
    app = _submitted_app(store, tmp_path, _profile(tmp_path))
    coord, clone, bare = _coord(store, tmp_path)
    item = store.c("sync_outbox").find_one({"application_id": app["_id"]})
    # Simulate: record written + committed in the clone, then crash before the DB learned about it.
    job = store.c("jobs").find_one({"_id": app["job_id"]})
    record = build_record(app, job)
    SyncOutbox(store).set(item["_id"], "committing", record_id=record["id"])
    coord._write_and_commit(app, record)
    assert git(clone, "rev-list", "--count", "HEAD") == "2"
    res = coord.reconcile_all()
    assert res and res[0]["status"] == "synced"
    assert git(bare, "rev-list", "--count", "main") == "2"
    data = json.loads((clone / "data/applications.json").read_text())
    assert sum(1 for r in data if r["id"] == record["id"]) == 1
    # Crash with files written (records.py add ran) but nothing committed yet.
    app2 = _submitted_app(store, tmp_path / "p2", _profile(tmp_path / "p2"), employer="Globex", req="G7")
    item2 = store.c("sync_outbox").find_one({"application_id": app2["_id"]})
    job2 = store.c("jobs").find_one({"_id": app2["job_id"]})
    record2 = build_record(app2, job2)
    SyncOutbox(store).set(item2["_id"], "committing", record_id=record2["id"])
    rec_file = tmp_path / "rec2.json"
    rec_file.write_text(json.dumps(record2))
    subprocess.run(
        [coord.python, str(clone / "scripts/records.py"), "add", str(rec_file), "--resume", app2["review_package"]["document"]["path"]], check=True, cwd=clone, capture_output=True
    )
    assert git(clone, "status", "--porcelain", "--", "data")  # dirty, uncommitted
    res2 = coord.reconcile_all()
    assert [r["status"] for r in res2] == ["synced"]
    assert git(bare, "rev-list", "--count", "main") == "3"
    data = json.loads((clone / "data/applications.json").read_text())
    assert sum(1 for r in data if r["id"] == record2["id"]) == 1 and not git(clone, "status", "--porcelain", "--", "data")


def test_requisition_already_recorded_under_other_id_is_conflict_not_duplicate(store, tmp_path):
    app = _submitted_app(store, tmp_path, _profile(tmp_path))
    coord, clone, bare = _coord(store, tmp_path)
    data = json.loads((clone / "data/applications.json").read_text())
    data.append(
        {
            "id": "li-999",
            "employer": "Acme",
            "role": "Backend Developer",
            "requisition_id": "R42",
            "linkedin_job_id": "999",
            "posting_url": "https://x",
            "status": "prepared",
            "application_date": "",
            "date_basis": "not submitted",
            "confirmation": "",
            "source_log": "",
            "notes": "",
            "artifacts": [],
        }
    )
    (clone / "data/applications.json").write_text(json.dumps(data, indent=2) + "\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "other id")
    git(clone, "push", "-q", "origin", "main")
    res = coord.process_pending()
    assert res[0]["status"] == "conflict" and "li-999" in res[0]["error"]
    assert store.c("applications").find_one({"_id": app["_id"]})["sync"]["state"] == "conflict"
    assert Path(clone / "data/applications.json").exists()
