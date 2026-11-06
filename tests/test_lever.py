"""Lever form adapter: questions from the fixture postings API or the apply page, then the same submit rules as Greenhouse."""

from __future__ import annotations

import socket
from dataclasses import asdict

import pytest

from amorehiremepls.apply.ats import registry
from amorehiremepls.apply.ats.lever import LeverAdapter, parse_board_url, parse_form_questions
from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.jobs.jobs import JobStore
from tests.fixtures import lever_site
from tests.fixtures.fixture_site import FixtureServer
from tests.fixtures.lever_site import POSTING_ID
from tests.test_applications_and_sync import _job, _profile, _service
from tests.test_greenhouse import _chromium_available

skip_no_browser = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_parse_board_url_accepts_hosted_and_eu_and_rejects_other_sites():
    posting = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert parse_board_url(f"https://jobs.lever.co/acme/{posting}") == ("acme", posting)
    assert parse_board_url(f"https://jobs.eu.lever.co/acme/{posting}/apply") == ("acme", posting)
    assert parse_board_url(f"https://jobs.lever.co/acme/{posting}?lever-source=linked") == ("acme", posting)
    assert parse_board_url("https://boards.greenhouse.io/acme/jobs/123") is None
    assert parse_board_url("https://jobs.ashbyhq.com/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") is None


def test_html_form_scan_reads_a_labelled_control():
    html = '<form><label for="portfolio">Portfolio URL</label><input id="portfolio" name="portfolio" required></form>'
    found = {q["id"]: q for q in parse_form_questions(html)}
    assert found["portfolio"]["text"] == "Portfolio URL" and found["portfolio"]["required"] is True


@pytest.fixture
def board():
    with FixtureServer(_free_port()) as srv:
        adapter = LeverAdapter(api_base=f"{srv.base}/lv-api/v0", board_hosts={f"127.0.0.1:{srv.port}"})
        registry.register(adapter)
        try:
            yield srv, adapter
        finally:
            registry._adapters.remove(adapter)


def test_questions_come_from_the_postings_payload_when_it_has_them(board):
    srv, adapter = board
    url = f"{srv.base}/lv/acme/{POSTING_ID}"
    qs = adapter.fetch_questions({"apply_url": url, "posting_url": url})
    assert qs.source == "lever_postings_api" and qs.external_job_id == POSTING_ID
    fields = {f.id: f for f in qs.fields}
    assert fields["name"].required and fields["resume"].type == "file"
    assert fields["q_why"].required and fields["q_gender"].meta["section"] == "eeo"
    assert "I don't wish to answer" in fields["q_gender"].options


def test_missing_api_questions_are_read_from_the_apply_page(board):
    srv, adapter = board
    url = f"{srv.base}/lv/acme/{POSTING_ID}?variant=extra"
    # The bare payload points at the apply page. The page, not the API, carries the extra question.
    response = adapter.client.get(f"{srv.base}/lv-api/v0/postings/acme/{POSTING_ID}", params={"bare": 1})
    assert "customQuestions" not in response.json()
    qs = adapter.fetch_questions({"apply_url": url, "posting_url": url})
    # fetch uses the URL's posting id and the API default, which includes questions. Force the bare document.
    adapter.client = _BareClient(adapter.client, srv.base)
    qs = adapter.fetch_questions({"apply_url": url, "posting_url": url})
    assert qs.source == "lever_form_html"
    assert any(f.id == "portfolio" and f.required for f in qs.fields)


class _BareClient:
    """Returns the bare postings document, and otherwise behaves like the real client."""

    def __init__(self, inner, base: str) -> None:
        self.inner = inner
        self.base = base

    def get(self, url, params=None):
        if "/postings/" in url:
            return self.inner.get(url, params={"bare": 1})
        return self.inner.get(url, params=params)


def test_personal_questions_decline_or_stay_blank_and_unknowns_need_an_answer(tmp_path, board):
    srv, adapter = board
    url = f"{srv.base}/lv/acme/{POSTING_ID}"
    profile = _profile(tmp_path)
    fields = adapter.fetch_questions({"apply_url": url, "posting_url": url}).fields
    answers = {a.field_id: a for a in adapter.map_answers(fields, profile, {}, "/tmp/Alex_Example_Resume.pdf", None, "fair")}
    assert answers["name"].value == "Alex Example" and answers["email"].value == "alex@example.com"
    assert answers["q_years"].value == "2-4 years"
    assert answers["q_gender"].sensitive and answers["q_gender"].value == "I don't wish to answer" and not answers["q_gender"].needs_input
    assert answers["q_why"].needs_input and "q_hear" in answers and not answers["q_hear"].needs_input
    assert answers["resume"].value.endswith("Alex_Example_Resume.pdf")


def _browser(tmp_path) -> BrowserAdapter:
    artifact_dir = tmp_path / "artifacts"
    cfg = BrowserConfig(allowed_hosts={"127.0.0.1"}, upload_root=artifact_dir, artifact_dir=artifact_dir, session_dir=tmp_path / "sessions", receipt_wait_ms=1500)
    return BrowserAdapter(cfg)


def _prepared(store, tmp_path, board, variant: str = "new", receipt_timeout_ms: int = 30000):
    srv, adapter = board
    url = f"{srv.base}/lv/acme/{POSTING_ID}" + (f"?variant={variant}" if variant != "new" else "")
    job = _job(store, req=f"LV-{variant}", url=url)
    qs = adapter.fetch_questions(job)
    JobStore(store).set_status(job["_id"], job["status"], form_plan={"fields": [asdict(f) for f in qs.fields], "source": qs.source}, apply_route="lever")
    svc = _service(store, tmp_path, _profile(tmp_path))
    app = svc.prepare(job["_id"], qs.fields)
    assert app["status"] == "needs_input"
    assert any("Why do you want" in o for o in app["review_package"]["outstanding"])
    assert not any("gender" in o.lower() for o in app["review_package"]["outstanding"])
    app = svc.supply_inputs(app["_id"], {"q_why": "I like reliable backend systems."})
    assert app["status"] == "ready_for_review", app["review_package"]["outstanding"]
    first = app["material_hash"]
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=first))
    if receipt_timeout_ms != 30000:
        original = adapter.build_plan

        def shorter(job_, pkg, answers):
            plan = original(job_, pkg, answers)
            plan.steps[-1].timeout_ms = receipt_timeout_ms
            return plan

        adapter.build_plan = shorter  # type: ignore[method-assign]
    return svc, job, app, first


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_submit_records_a_receipt_tied_to_the_posting_and_replay_does_not_resubmit(store, tmp_path, board):
    svc, job, app, _first = _prepared(store, tmp_path, board)
    before = len(lever_site.LV_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="lv"))
    assert out.status == "succeeded", out.reason
    sub = lever_site.LV_SUBMISSIONS[-1]
    assert len(lever_site.LV_SUBMISSIONS) == before + 1
    assert sub["posting_id"] == POSTING_ID and sub["name"] == "Alex Example" and sub["email"] == "alex@example.com"
    assert sub["q_why"] == "I like reliable backend systems." and sub["q_years"] == "2-4 years"
    assert sub["q_gender"] == "I don't wish to answer" and sub["resume"]["filename"]
    saved = svc.apps.find_one({"_id": app["_id"]})
    assert saved["status"] == "submitted" and POSTING_ID in saved["evidence"]["final_url"]
    again = svc.execute(app["_id"], "w2", lambda plan: pytest.fail("adapter must not run again"))
    assert again.status == "succeeded" and len(lever_site.LV_SUBMISSIONS) == before + 1


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_human_check_and_login_become_handoffs_without_a_submission(store, tmp_path, board):
    svc, _job_doc, app, _first = _prepared(store, tmp_path, board, variant="check")
    before = len(lever_site.LV_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="lv"))
    assert out.status == "handoff" and "human check" in out.reason
    assert len(lever_site.LV_SUBMISSIONS) == before
    svc2, _j, app2, _h = _prepared(store, tmp_path, board, variant="login")
    out2 = svc2.execute(app2["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="lv"))
    assert out2.status == "handoff" and len(lever_site.LV_SUBMISSIONS) == before


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_validation_error_is_retryable_and_a_missing_confirmation_is_not_a_submission(store, tmp_path, board):
    svc, job, app, _first = _prepared(store, tmp_path, board, variant="invalid")
    before = len(lever_site.LV_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="lv"))
    assert out.status == "failed" and out.retryable and len(lever_site.LV_SUBMISSIONS) == before
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready"
    svc2, _j, app2, _h = _prepared(store, tmp_path, board, variant="slow", receipt_timeout_ms=1500)
    out2 = svc2.execute(app2["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="lv"))
    assert out2.status == "needs_verification"
    assert len(lever_site.LV_SUBMISSIONS) == before + 1
    assert svc2.execute(app2["_id"], "w1", lambda plan: pytest.fail("must not run again")).status == "needs_verification"


@pytest.mark.mongo
def test_a_changed_answer_changes_the_approval_hash(store, tmp_path, board):
    svc, _job_doc, app, first = _prepared(store, tmp_path, board)
    app = svc.supply_inputs(app["_id"], {"q_why": "A different answer."})
    assert app["material_hash"] != first
    out = svc.execute(app["_id"], "w1", lambda plan: {"verified": True})
    assert out.status == "denied" and "material content changed" in out.reason
