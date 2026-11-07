"""LinkedIn application form: fixture only. Nothing is sent to LinkedIn, and no message or connection is made."""

from __future__ import annotations

import json
import socket
from dataclasses import asdict
from pathlib import Path

import pytest

from amorehiremepls.apply.ats import registry
from amorehiremepls.apply.ats.linkedin import LinkedInAdapter, parse_job_url
from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig
from amorehiremepls.candidate.candidate_profile import apply_profile_toml, load_profile_toml
from amorehiremepls.candidate.profile import Profile
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.jobs.jobs import JobStore
from tests.fixtures import linkedin_site
from tests.fixtures.fixture_site import FixtureServer
from tests.fixtures.linkedin_site import JOB_ID
from tests.test_applications_and_sync import _job, _profile, _service
from tests.test_greenhouse import _chromium_available

skip_no_browser = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")
EXAMPLE = Path(__file__).resolve().parents[1] / "candidate" / "profile.example.toml"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("a live LinkedIn page must not be fetched")


def test_job_url_is_a_posting_and_a_profile_link_is_not():
    assert parse_job_url("https://www.linkedin.com/jobs/view/1000000001/") == "1000000001"
    assert parse_job_url("https://www.linkedin.com/jobs/view/example-role-1000000001/?trk=x") == "1000000001"
    assert parse_job_url("https://www.linkedin.com/jobs/search/?currentJobId=1000000001") == "1000000001"
    assert parse_job_url("https://www.linkedin.com/in/someone/") is None
    assert parse_job_url("https://www.linkedin.com/messaging/") is None
    assert parse_job_url("https://jobs.lever.co/acme/abc") is None
    adapter = LinkedInAdapter()
    assert adapter.matches("www.linkedin.com", "https://www.linkedin.com/jobs/view/1000000001/")
    assert not adapter.matches("www.linkedin.com", "https://www.linkedin.com/in/someone/")


def test_live_question_list_is_the_contact_step_and_does_not_call_linkedin():
    adapter = LinkedInAdapter(client=_NoNetwork())  # type: ignore[arg-type]
    qs = adapter.fetch_questions({"apply_url": "https://www.linkedin.com/jobs/view/1000000001/"})
    assert qs.source == "linkedin_easy_apply_standard" and qs.external_job_id == "1000000001"
    assert [field.id for field in qs.fields] == ["email", "phone", "resume"]


def test_plan_puts_submit_last_and_ties_the_receipt_to_the_job():
    adapter = LinkedInAdapter()
    fields = adapter.fetch_questions({"apply_url": "https://www.linkedin.com/jobs/view/1000000001/"}).fields
    job = {"_id": "job_x", "linkedin_job_id": "1000000001", "form_plan": {"fields": [asdict(field) for field in fields]}}
    pkg = {"apply_url": "https://www.linkedin.com/jobs/view/1000000001/"}
    answers = [{"field_id": "email", "value": "a@example.com", "label": "Email address"}, {"field_id": "resume", "value": "/tmp/resume.pdf", "label": "Resume"}]
    plan = adapter.build_plan(job, pkg, answers)
    assert plan.steps[0].type == "open_apply" and plan.steps[0].label == "Easy Apply" and not plan.steps[0].final and plan.steps[0].missing_is_handoff
    assert plan.steps[-1].final and plan.steps[-1].label == "Submit application"
    assert all(not step.final for step in plan.steps[:-1])
    assert plan.receipt_url_must_include == ["1000000001"] and plan.handoff_if_submit_missing
    assert not any(step.type == "click" and "connect" in step.label.lower() for step in plan.steps)


def _sales_profile() -> Profile:
    profile = Profile()
    apply_profile_toml(profile, load_profile_toml(EXAMPLE), confirmed_on="2026-11-05")
    return profile


@pytest.fixture
def board():
    with FixtureServer(_free_port()) as srv:
        adapter = LinkedInAdapter(api_base=f"{srv.base}/li-api", board_hosts={f"127.0.0.1:{srv.port}"})
        registry.register(adapter)
        try:
            yield srv, adapter
        finally:
            registry._adapters.remove(adapter)


def test_missing_required_answer_is_a_question_for_both_candidates(tmp_path, board):
    srv, adapter = board
    url = f"{srv.base}/jobs/view/{JOB_ID}?variant=missing"
    fields = adapter.fetch_questions({"apply_url": url}).fields
    years = next(field.options for field in fields if field.id == "years")
    for profile in (_profile(tmp_path), _sales_profile()):
        answers = {answer.field_id: answer for answer in adapter.map_answers(fields, profile, {}, "/tmp/resume.pdf", None, "fair")}
        assert answers["referral"].value is None and answers["referral"].needs_input
        assert answers["email"].value == profile.fact_value("email")
        assert answers["phone"].value == profile.fact_value("phone")
        if answers["years"].value is not None:
            assert answers["years"].value in years
        assert answers["work_auth"].value in ("Yes", "No") or answers["work_auth"].needs_input


def _browser(tmp_path) -> BrowserAdapter:
    artifact_dir = tmp_path / "artifacts"
    cfg = BrowserConfig(allowed_hosts={"127.0.0.1"}, upload_root=artifact_dir, artifact_dir=artifact_dir, session_dir=tmp_path / "sessions", receipt_wait_ms=1500)
    return BrowserAdapter(cfg)


def _prepared(store, tmp_path, board, variant: str = "", authorize: bool = True):
    srv, adapter = board
    url = f"{srv.base}/jobs/view/{JOB_ID}" + (f"?variant={variant}" if variant else "")
    job = _job(store, req=f"LI-{variant or 'form'}", url=url)
    qs = adapter.fetch_questions(job)
    JobStore(store).set_status(job["_id"], job["status"], form_plan={"fields": [asdict(field) for field in qs.fields], "source": qs.source}, apply_route="linkedin")
    svc = _service(store, tmp_path, _profile(tmp_path))
    app = svc.prepare(job["_id"], qs.fields)
    return svc, job, app, adapter


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_signed_in_form_is_filled_and_submit_waits_for_a_saved_approval(store, tmp_path, board):
    svc, job, app, _adapter = _prepared(store, tmp_path, board, authorize=False)
    assert app["status"] == "ready_for_review", app["review_package"]["outstanding"]
    before = len(linkedin_site.LI_SUBMISSIONS)
    denied = svc.execute(app["_id"], "w1", lambda plan: pytest.fail("must not open the form without approval"))
    assert denied.status == "denied" and len(linkedin_site.LI_SUBMISSIONS) == before and not denied.adapter_ran
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out.status == "succeeded", out.reason
    sub = linkedin_site.LI_SUBMISSIONS[-1]
    assert len(linkedin_site.LI_SUBMISSIONS) == before + 1
    assert sub["job_id"] == JOB_ID and sub["email"] == "alex@example.com" and sub["phone"] == "555 000 0000"
    assert sub["years"] == "2-4 years" and sub["work_auth"] == "Yes" and sub["resume"]["filename"]
    assert sub["resume"]["bytes"] > 0
    saved = svc.apps.find_one({"_id": app["_id"]})
    assert saved["status"] == "submitted" and JOB_ID in saved["evidence"]["final_url"] and saved["evidence"]["verified_identifiers"] == [JOB_ID]
    assert out.evidence["final_clicked"] is True
    again = svc.execute(app["_id"], "w2", lambda plan: pytest.fail("must not send a second time"))
    assert again.status == "succeeded" and len(linkedin_site.LI_SUBMISSIONS) == before + 1


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_logged_out_and_verify_pages_hand_back_without_a_submission(store, tmp_path, board):
    before = len(linkedin_site.LI_SUBMISSIONS)
    svc, job, app, _adapter = _prepared(store, tmp_path, board, variant="login")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out.status == "handoff", out.reason
    assert out.evidence.get("final_clicked") is False and len(linkedin_site.LI_SUBMISSIONS) == before
    saved = svc.apps.find_one({"_id": app["_id"]})
    assert saved["status"] == "handoff" and saved["handoff_kind"] == "login" and "Sign in" in saved["next_action"]
    svc2, job2, app2, _adapter = _prepared(store, tmp_path, board, variant="checkpoint")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job2["_id"], material_hash=app2["material_hash"]))
    out2 = svc2.execute(app2["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out2.status == "handoff" and out2.evidence.get("final_clicked") is False
    assert len(linkedin_site.LI_SUBMISSIONS) == before
    saved2 = svc2.apps.find_one({"_id": app2["_id"]})
    assert saved2["handoff_kind"] == "checkpoint" and "verify it is you" in saved2["next_action"]


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_external_apply_is_followed_and_a_page_with_neither_control_is_a_handoff(store, tmp_path, board):
    before = len(linkedin_site.LI_SUBMISSIONS)
    company_before = len(linkedin_site.COMPANY_SUBMISSIONS)
    svc, job, app, _adapter = _prepared(store, tmp_path, board, variant="external")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out.status == "succeeded", out.reason
    followed = (out.evidence.get("external_apply") or {}).get("followed") or ""
    assert "/company/" in followed and (out.evidence.get("external_apply") or {}).get("control") == "apply_with_linkedin"
    sub = linkedin_site.LI_SUBMISSIONS[-1]
    assert len(linkedin_site.LI_SUBMISSIONS) == before + 1 and sub["opened_by"] == "apply-with-linkedin" and sub["job_id"] == JOB_ID
    assert sub["email"] == "alex@example.com" and JOB_ID in out.evidence["final_url"] and out.evidence["final_clicked"] is True
    assert len(linkedin_site.COMPANY_SUBMISSIONS) == company_before

    svc2, job2, app2, _adapter = _prepared(store, tmp_path, board, variant="neither")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job2["_id"], material_hash=app2["material_hash"]))
    out2 = svc2.execute(app2["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out2.status == "handoff", out2.reason
    assert out2.evidence.get("final_clicked") is False and "/company/" in ((out2.evidence.get("external_apply") or {}).get("followed") or "")
    assert len(linkedin_site.LI_SUBMISSIONS) == before + 1 and len(linkedin_site.COMPANY_SUBMISSIONS) == company_before
    saved = svc2.apps.find_one({"_id": app2["_id"]})
    assert saved["handoff_kind"] == "unsupported" and "Nothing was sent" in saved["next_action"]

    svc3, job3, app3, _adapter = _prepared(store, tmp_path, board, variant="own")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job3["_id"], material_hash=app3["material_hash"]))
    out3 = svc3.execute(app3["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out3.status == "handoff" and out3.evidence.get("final_clicked") is False
    assert (out3.evidence.get("external_apply") or {}).get("control") == "site_apply"
    assert len(linkedin_site.LI_SUBMISSIONS) == before + 1 and len(linkedin_site.COMPANY_SUBMISSIONS) == company_before
    assert "Nothing was sent" in (svc3.apps.find_one({"_id": app3["_id"]})["next_action"])


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_external_apply_redirect_loads_a_script_from_another_host(store, tmp_path, board):
    """The company page is a redirect off the allowed host. Its form is opened by a script on a third host.

    Both names resolve to loopback, and the gate sees the names. The script is not a saved host and not the page
    host. If the request gate aborts it, the form stays hidden and the run hands back before anything is sent."""
    before = len(linkedin_site.LI_SUBMISSIONS)
    svc, job, app, _adapter = _prepared(store, tmp_path, board, variant="assets")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out.status == "succeeded", out.reason
    external = out.evidence.get("external_apply") or {}
    assert "/safety/go/" in (external.get("followed") or "")
    assert external.get("landed_host") == "site.127.0.0.1.nip.io"
    assert external.get("control") == "apply_with_linkedin"
    assert len(linkedin_site.LI_SUBMISSIONS) == before + 1
    sub = linkedin_site.LI_SUBMISSIONS[-1]
    assert sub["opened_by"] == "apply-with-linkedin" and sub["job_id"] == JOB_ID
    assert sub["email"] == "alex@example.com" and sub["resume"]["bytes"] > 0
    assert out.evidence.get("final_clicked") is True


def _saved_session(tmp_path: Path, host: str) -> None:
    """A session cookie for the fixture host. The value is a marker, not a credential."""
    directory = tmp_path / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    state = {
        "cookies": [
            {
                "name": "awli",
                "value": "1",
                "domain": "127.0.0.1",
                "path": "/",
                "expires": 1893456000,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    (directory / f"{host}.storage.json").write_text(json.dumps(state))


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_apply_with_linkedin_popup_uses_the_saved_session(store, tmp_path, board):
    """A cross-site Apply with LinkedIn popup is signed out. The saved session is on the context, so the app opens that URL itself."""
    linkedin_site.OAUTH_HITS.clear()
    before = len(linkedin_site.LI_SUBMISSIONS)
    srv, _adapter = board
    host = f"127.0.0.1:{srv.port}"
    _saved_session(tmp_path, host)
    svc, job, app, _adapter = _prepared(store, tmp_path, board, variant="popup")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key=host))
    assert out.status == "succeeded", out.reason
    assert any(hit["site"] == "cross-site" and hit["ok"] is False for hit in linkedin_site.OAUTH_HITS)
    assert any(hit["ok"] is True for hit in linkedin_site.OAUTH_HITS)
    assert len(linkedin_site.LI_SUBMISSIONS) == before + 1
    sub = linkedin_site.LI_SUBMISSIONS[-1]
    assert sub["job_id"] == JOB_ID and sub["email"] == "alex@example.com" and sub["resume"]["bytes"] > 0
    assert out.evidence.get("final_clicked") is True
    assert (out.evidence.get("external_apply") or {}).get("control") == "apply_with_linkedin"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_block_page_on_the_login_path_is_not_a_sign_in(store, tmp_path, board):
    before = len(linkedin_site.LI_SUBMISSIONS)
    svc, job, app, _adapter = _prepared(store, tmp_path, board, variant="blocked")
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="li"))
    assert out.status == "handoff", out.reason
    assert out.evidence.get("wall") == "blocked" and out.evidence.get("final_clicked") is False
    assert len(linkedin_site.LI_SUBMISSIONS) == before
    saved = svc.apps.find_one({"_id": app["_id"]})
    assert saved["handoff_kind"] == "blocked"
    assert "Sign in" not in saved["next_action"]
    assert "blocked this browser" in saved["next_action"]
