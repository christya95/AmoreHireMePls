"""Ashby form adapter: the public GraphQL job posting read, comboboxes, and the same submit rules as Greenhouse."""

from __future__ import annotations

import socket
from dataclasses import asdict

import pytest

from amorehiremepls.ats import registry
from amorehiremepls.ats.ashby import AshbyAdapter, parse_board_url
from amorehiremepls.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.browser import BrowserAdapter, BrowserConfig
from amorehiremepls.jobs import JobStore
from tests.fixtures import ashby_site
from tests.fixtures.ashby_site import POSTING_ID
from tests.fixtures.fixture_site import FixtureServer
from tests.test_applications_and_sync import _job, _profile, _service
from tests.test_greenhouse import _chromium_available

skip_no_browser = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_parse_board_url_accepts_ashby_and_rejects_other_sites():
    posting = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    assert parse_board_url(f"https://jobs.ashbyhq.com/acme/{posting}") == ("acme", posting)
    assert parse_board_url(f"https://jobs.ashbyhq.com/acme/{posting}/application") == ("acme", posting)
    assert parse_board_url(f"https://jobs.lever.co/acme/{posting}") is None


@pytest.fixture
def board():
    ashby_site.AB_OPERATIONS.clear()
    with FixtureServer(_free_port()) as srv:
        adapter = AshbyAdapter(graphql_url=f"{srv.base}/ab-api/non-user-graphql", board_hosts={f"127.0.0.1:{srv.port}"})
        registry.register(adapter)
        try:
            yield srv, adapter
        finally:
            registry._adapters.remove(adapter)


def test_questions_come_from_the_graphql_read_and_not_from_a_mutation(board):
    srv, adapter = board
    url = f"{srv.base}/ab/acme/{POSTING_ID}"
    qs = adapter.fetch_questions({"apply_url": url, "posting_url": url})
    assert qs.source == "ashby_graphql" and qs.external_job_id == POSTING_ID
    assert ashby_site.AB_OPERATIONS == ["ApiJobPosting"]
    fields = {f.id: f for f in qs.fields}
    assert fields["_systemfield_resume"].type == "file"
    assert fields["q_years"].type == "select" and "2-4 years" in fields["q_years"].options
    assert fields["q_hear"].type == "multiselect"
    assert fields["gender"].meta["section"] == "eeo" and "I don't wish to answer" in fields["gender"].options


def test_a_posting_without_a_form_is_read_from_the_page(board):
    srv, adapter = board
    url = f"{srv.base}/ab/acme/{POSTING_ID}?variant=extra"

    class _NoForm:
        def post(self, url_, json=None):
            class Response:
                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return {"data": {"jobPosting": {"id": POSTING_ID, "title": "Backend Developer"}}}

            return Response()

        def get(self, url_, params=None):
            return adapter_client.get(url_, params=params)

    adapter_client = adapter.client
    adapter.client = _NoForm()
    qs = adapter.fetch_questions({"apply_url": url, "posting_url": url})
    assert qs.source == "ashby_form_html" and any(f.id == "portfolio" and f.required for f in qs.fields)
    assert srv.base  # the page was served by this board


def test_personal_questions_decline_and_unknowns_need_an_answer(tmp_path, board):
    srv, adapter = board
    url = f"{srv.base}/ab/acme/{POSTING_ID}"
    fields = adapter.fetch_questions({"apply_url": url, "posting_url": url}).fields
    answers = {a.field_id: a for a in adapter.map_answers(fields, _profile(tmp_path), {}, "/tmp/Alex_Example_Resume.pdf", None, "fair")}
    assert answers["_systemfield_name"].value == "Alex Example"
    assert answers["location"].value == "Milton, Ontario"
    assert answers["q_years"].value == "2-4 years"
    assert answers["gender"].sensitive and answers["gender"].value == "I don't wish to answer" and not answers["gender"].needs_input
    assert answers["q_why"].needs_input and not answers["q_hear"].needs_input


def _browser(tmp_path) -> BrowserAdapter:
    artifact_dir = tmp_path / "artifacts"
    cfg = BrowserConfig(allowed_hosts={"127.0.0.1"}, upload_root=artifact_dir, artifact_dir=artifact_dir, session_dir=tmp_path / "sessions")
    return BrowserAdapter(cfg)


def _prepared(store, tmp_path, board, variant: str = "new", receipt_timeout_ms: int = 30000):
    srv, adapter = board
    url = f"{srv.base}/ab/acme/{POSTING_ID}" + (f"?variant={variant}" if variant != "new" else "")
    job = _job(store, req=f"AB-{variant}", url=url)
    qs = adapter.fetch_questions(job)
    JobStore(store).set_status(job["_id"], job["status"], form_plan={"fields": [asdict(f) for f in qs.fields], "source": qs.source}, apply_route="ashby")
    svc = _service(store, tmp_path, _profile(tmp_path))
    app = svc.prepare(job["_id"], qs.fields)
    assert app["status"] == "needs_input" and any("Why do you want" in o for o in app["review_package"]["outstanding"])
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
def test_comboboxes_file_upload_and_receipt_and_replay_does_not_resubmit(store, tmp_path, board):
    svc, _job_doc, app, _first = _prepared(store, tmp_path, board)
    before = len(ashby_site.AB_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="ab"))
    assert out.status == "succeeded", out.reason
    sub = ashby_site.AB_SUBMISSIONS[-1]
    assert len(ashby_site.AB_SUBMISSIONS) == before + 1
    assert sub["_systemfield_name"] == "Alex Example" and sub["q_why"] == "I like reliable backend systems."
    assert sub["q_years"] == "2-4 years" and sub["gender"] == "I don't wish to answer"
    assert sub["location"] == "Milton, Ontario" and sub["_systemfield_resume"]["filename"]
    saved = svc.apps.find_one({"_id": app["_id"]})
    assert saved["status"] == "submitted" and POSTING_ID in saved["evidence"]["final_url"]
    again = svc.execute(app["_id"], "w2", lambda plan: pytest.fail("adapter must not run again"))
    assert again.status == "succeeded" and len(ashby_site.AB_SUBMISSIONS) == before + 1


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_human_check_and_login_do_not_submit(store, tmp_path, board):
    svc, _j, app, _h = _prepared(store, tmp_path, board, variant="check")
    before = len(ashby_site.AB_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="ab"))
    assert out.status == "handoff" and len(ashby_site.AB_SUBMISSIONS) == before
    svc2, _j2, app2, _h2 = _prepared(store, tmp_path, board, variant="login")
    out2 = svc2.execute(app2["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="ab"))
    assert out2.status == "handoff" and len(ashby_site.AB_SUBMISSIONS) == before


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_validation_is_retryable_and_a_missing_confirmation_is_not_submitted(store, tmp_path, board):
    svc, job, app, _h = _prepared(store, tmp_path, board, variant="invalid")
    before = len(ashby_site.AB_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="ab"))
    assert out.status == "failed" and out.retryable and len(ashby_site.AB_SUBMISSIONS) == before
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready"
    svc2, _j, app2, _h2 = _prepared(store, tmp_path, board, variant="slow", receipt_timeout_ms=1500)
    out2 = svc2.execute(app2["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="ab"))
    assert out2.status == "needs_verification" and len(ashby_site.AB_SUBMISSIONS) == before + 1
    assert svc2.execute(app2["_id"], "w1", lambda plan: pytest.fail("must not run again")).status == "needs_verification"


@pytest.mark.mongo
def test_a_changed_answer_changes_the_approval_hash(store, tmp_path, board):
    svc, _j, app, first = _prepared(store, tmp_path, board)
    app = svc.supply_inputs(app["_id"], {"q_why": "A different answer."})
    assert app["material_hash"] != first
    out = svc.execute(app["_id"], "w1", lambda plan: {"verified": True})
    assert out.status == "denied" and "material content changed" in out.reason
