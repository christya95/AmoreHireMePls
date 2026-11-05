import socket
import subprocess
from pathlib import Path

import pytest

from amorehiremepls.actions import ActionFailed, AmbiguousOutcome
from amorehiremepls.browser import BrowserAdapter, BrowserConfig, HandoffRequired, Plan, Step
from amorehiremepls.util import sha256_file
from tests.fixtures import fixture_site
from tests.fixtures.fixture_site import FixtureServer

pytestmark = pytest.mark.browser


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


skip_no_browser = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def site():
    with FixtureServer(_free_port()) as srv:
        yield srv


def _plan(base: str, req: str, resume: Path, phone="555 000 0000", slow: int = 0, submit_timeout=8000, job_id: str | None = None) -> Plan:
    steps = [
        Step("fill", "#first_name", "Alex"),
        Step("fill", "#last_name", "Example"),
        Step("fill", "#email", "alex@example.com"),
        Step("fill", "#phone", phone),
        Step("click", "#next1"),
        Step("upload", "#resume", str(resume)),
        Step("select", "#work_auth", "yes"),
        Step("fill", "#salary", "120000"),
        Step("click", "#next2"),
        Step("assert_value", "#rv_resume", resume.name),
        Step("assert_value", "#rv_email", "alex@example.com"),
    ]
    if slow:
        steps.append(Step("wait_url", r"/review/"))
    steps.append(Step("click", "#submit", final=True, timeout_ms=submit_timeout))
    url = f"{base}/apply/{req}"
    return Plan(start_url=url, steps=steps, receipt_text="Application received", receipt_must_include=[req], upload_filename=resume.name, job_id=job_id)


def _by_label(ev: dict) -> dict[str, dict]:
    return {s["label"]: s for s in ev.get("screenshots", [])}


@skip_no_browser
def test_resource_host_patterns_match_vendor_upload_buckets():
    from amorehiremepls.browser import host_allowed

    hosts = {"job-boards.cdn.greenhouse.io", "grnhse-*.s3.amazonaws.com", "grnhse-*.s3.*.amazonaws.com"}
    assert host_allowed("job-boards.cdn.greenhouse.io", hosts)
    assert host_allowed("grnhse-prod-jben-us-east-1.s3.amazonaws.com", hosts)  # observed upload target of the React board
    assert host_allowed("grnhse-prod-jben-eu-west-1.s3.eu-west-1.amazonaws.com", hosts)
    assert not host_allowed("evil-grnhse-prod.s3.amazonaws.com", hosts) and not host_allowed("s3.amazonaws.com", hosts)
    assert not host_allowed("job-boards.greenhouse.io", hosts)


def test_multi_step_form_upload_and_receipt(site, tmp_path):
    resume = tmp_path / "Alex_Example_Resume_Acme_R42.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    artifact_dir = tmp_path / "artifacts"
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}, screenshot_dir=tmp_path / "shots", session_dir=tmp_path / "sessions", artifact_dir=artifact_dir))
    ev = adapter.run_plan(_plan(site.base, "R42", resume, job_id="job_fx42"), site_key="fixture")
    assert ev["verified"] and "R42" in ev["receipt_text"] and ev["final_clicked"]
    assert fixture_site.SUBMISSIONS[-1]["resume_name"] == resume.name and fixture_site.SUBMISSIONS[-1]["req"] == "R42"
    assert (tmp_path / "sessions" / "fixture.storage.json").exists()
    # summary = filled review page right before the final click; receipt = verified confirmation page
    shots = _by_label(ev)
    assert [s["label"] for s in ev["screenshots"]] == ["summary", "receipt"]
    assert "/review/" in shots["summary"]["url"] and "/submit/" in shots["receipt"]["url"]
    assert shots["summary"]["captured_at"] <= shots["receipt"]["captured_at"]
    for label, shot in shots.items():
        p = Path(shot["path"])
        assert p.parent == artifact_dir / "jobs" / "job_fx42" / "screenshots" and p.is_file() and p.stat().st_size > 0
        assert p.name.endswith(f"_job_fx42_{label}.png") and shot["sha256"] == sha256_file(p)
    assert not (tmp_path / "shots").exists()  # the global fallback dir is not used when the job dir is known


@skip_no_browser
def test_screenshot_dir_fallback_without_job_id(site, tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF")
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}, screenshot_dir=tmp_path / "shots", artifact_dir=tmp_path / "artifacts"))
    ev = adapter.run_plan(_plan(site.base, "R43", resume))
    paths = [Path(s["path"]) for s in ev["screenshots"]]
    assert paths and all(p.parent == tmp_path / "shots" and p.is_file() for p in paths)
    assert [p.name.split("_", 1)[1] for p in paths] == ["summary.png", "receipt.png"]


@skip_no_browser
def test_validation_error_fails_before_final_click(site, tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF")
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}))
    before = len(fixture_site.SUBMISSIONS)
    with pytest.raises(ActionFailed) as exc:
        adapter.run_plan(_plan(site.base, "R1", resume, phone=""))
    assert "Phone is required" in str(exc.value) and not exc.value.evidence.get("final_clicked")
    assert len(fixture_site.SUBMISSIONS) == before


@skip_no_browser
def test_slow_submit_is_ambiguous_not_failed(site, tmp_path):
    resume = tmp_path / "r.pdf"
    resume.write_bytes(b"%PDF")
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}, screenshot_dir=tmp_path / "shots"))
    plan = _plan(site.base, "R7", resume, submit_timeout=1500)
    plan.start_url = f"{site.base}/apply/R7?slow=3"  # server sleeps after recording the submission
    before = len(fixture_site.SUBMISSIONS)
    with pytest.raises(AmbiguousOutcome) as exc:
        adapter.run_plan(plan)
    assert exc.value.evidence["final_clicked"] is True
    shots = _by_label(exc.value.evidence)
    # the review page was still captured before the click; the post-click state keeps its distinct label; no receipt claimed
    assert set(shots) == {"summary", "ambiguous"} and "/review/" in shots["summary"]["url"]
    assert Path(shots["ambiguous"]["path"]).name.endswith("_ambiguous.png") and Path(shots["ambiguous"]["path"]).is_file()
    # The employer did receive it: exactly the situation where a blind retry would double-apply.
    assert len(fixture_site.SUBMISSIONS) == before + 1


@skip_no_browser
@pytest.mark.mongo
def test_fixture_submission_records_summary_and_receipt_screenshots_and_sync_archives_both(site, store, tmp_path):
    """Full path: ApplicationService -> Playwright adapter on the fixture ATS -> application record -> tracker clone."""
    import json

    from amorehiremepls.authorization import AuthorizationStore, ScopeBinding
    from tests.test_applications_and_sync import FIELDS, _coord, _job, _profile, _service

    req = "R77"
    profile = _profile(tmp_path)
    artifact_dir = tmp_path / "artifacts"  # same root the Tailor writes under (see _service)

    def builder(job, pkg, answers):
        resume = Path(pkg["document"]["path"])  # the tailored file from the review package, under artifact_dir/jobs/<job_id>/
        plan = _plan(site.base, req, resume)
        plan.job_id = None  # ApplicationService must fill the job id itself
        return plan

    svc = _service(store, tmp_path, profile, {f"127.0.0.1:{site.port}": builder})  # builders are keyed by apply-url host
    job = _job(store, req=req, url=f"{site.base}/apply/{req}")
    app = svc.prepare(job["_id"], [f for f in FIELDS if f.id != "why"])
    assert app["status"] == "ready_for_review"
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}, upload_root=artifact_dir, artifact_dir=artifact_dir, session_dir=tmp_path / "sessions"))
    out = svc.execute(app["_id"], "w1", lambda plan: adapter.run_plan(plan, site_key="fixture"))
    assert out.status == "succeeded", out.reason

    app = store.c("applications").find_one({"_id": app["_id"]})
    assert app["status"] == "submitted" and app["evidence_basis"] == "receipt_screenshot_reviewed"
    shots = {s["label"]: s for s in app["evidence"]["screenshots"]}
    assert set(shots) == {"summary", "receipt"}
    for label, shot in shots.items():
        p = Path(shot["path"])
        assert p.parent == artifact_dir / "jobs" / job["_id"] / "screenshots" and p.is_file()
        assert p.name.endswith(f"_{job['_id']}_{label}.png") and shot["sha256"] == sha256_file(p) and shot["captured_at"] and shot["url"]
        art = store.c("artifacts").find_one({"_id": shot["artifact_id"]})
        assert art["kind"] == "screenshot" and art["job_id"] == job["_id"] == art["owner_id"] and art["application_id"] == app["_id"]
        assert art["label"] == label and art["sha256"] == shot["sha256"] and art["path"] == str(p) and art["filename"] == p.name
    assert "/review/" in shots["summary"]["url"] and fixture_site.SUBMISSIONS[-1]["req"] == req

    coord, clone, bare = _coord(store, tmp_path)
    res = coord.process_pending()
    assert res and res[0]["status"] == "synced", res
    rid = store.c("applications").find_one({"_id": app["_id"]})["tracker_record_id"]
    rec = next(r for r in json.loads((clone / "data/applications.json").read_text()) if r["id"] == rid)
    archived = {a["kind"]: a for a in rec["artifacts"]}
    assert set(archived) == {"resume_submitted", "receipt", "summary_screenshot"}
    for kind, label in (("receipt", "receipt"), ("summary_screenshot", "summary")):
        a = archived[kind]
        assert a["path"].startswith(f"artifacts/{rid}/") and a["path"].endswith(f"-{rid}_{label}.png") and a["original_filename"] == f"{rid}_{label}.png"
        assert (clone / a["path"]).is_file() and a["sha256"] == sha256_file(clone / a["path"]) == shots[label]["sha256"]
    assert f"{rid}_summary.png" in (clone / "records" / f"{rid}.md").read_text()
    assert not subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True).stdout  # everything archived was committed


@skip_no_browser
def test_login_wall_becomes_handoff_and_disallowed_host_is_refused(site, tmp_path):
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}))
    plan = Plan(start_url=f"{site.base}/gated/R9", steps=[Step("click", "#submit", final=True)], receipt_text="x")
    with pytest.raises(HandoffRequired):
        adapter.run_plan(plan)
    with pytest.raises(ActionFailed):
        adapter.run_plan(Plan(start_url="https://example.com/apply", steps=[], receipt_text="x"))
