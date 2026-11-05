"""Greenhouse form adapter: question list from the (fixture) Job Board API, deterministic answer mapping, plan building,
and the full approve -> fill -> submit -> receipt path on the fixture board in both layouts, plus every stop condition
(validation error, human check, missing confirmation, replay, changed answer)."""

from __future__ import annotations

import socket
from dataclasses import asdict
from pathlib import Path

import pytest

from amorehiremepls.apply.ats import registry
from amorehiremepls.apply.ats.greenhouse import GreenhouseAdapter, parse_board_url, questions_to_fields
from amorehiremepls.apply.browser import BrowserAdapter, BrowserConfig, HandoffRequired
from amorehiremepls.apply.captcha import CaptchaAnswer, ModelCaptchaSolver, NeverSolver
from amorehiremepls.core.ai import AIRunner, Budget, FakeProvider, Pricing
from amorehiremepls.core.authorization import AuthorizationStore, ScopeBinding
from amorehiremepls.core.skills import catalog
from amorehiremepls.jobs.jobs import JobStore
from tests.fixtures import greenhouse_site
from tests.fixtures.fixture_site import FixtureServer
from tests.fixtures.greenhouse_site import GRID_CAPTCHA_CELLS, TEXT_CAPTCHA_ANSWER, board_schema
from tests.test_applications_and_sync import _job, _profile, _service

# ---------------------------------------------------------------- unit: URL parsing, schema, mapping, plan


def test_parse_board_url_handles_hosted_new_embed_and_eu_urls():
    assert parse_board_url("https://boards.greenhouse.io/faire/jobs/8831748002?gh_jid=8831748002") == ("faire", "8831748002")
    assert parse_board_url("https://job-boards.greenhouse.io/faire/jobs/8831748002") == ("faire", "8831748002")
    assert parse_board_url("https://job-boards.eu.greenhouse.io/acme/jobs/42") == ("acme", "42")
    assert parse_board_url("https://boards.greenhouse.io/embed/job_app?for=faire&token=8831748002") == ("faire", "8831748002")
    assert parse_board_url("https://careers.example.com/jobs?gh_jid=1") is None
    assert parse_board_url("https://jobs.lever.co/acme/abc") is None
    assert parse_board_url(None) is None


def test_questions_to_fields_flattens_every_section_and_skips_hidden_and_paste_twins():
    fields = {f.id: f for f in questions_to_fields(board_schema("acme", "123456"))}
    assert "resume_text" not in fields and "cover_letter_text" not in fields and "latitude" not in fields
    assert fields["resume"].type == "file" and fields["resume"].required and fields["resume"].meta["section"] == "questions"
    assert fields["question_1003"].type == "select" and fields["question_1003"].options == ["Alberta", "Ontario", "Quebec", "Not in Canada"]
    assert fields["question_1003"].meta["values"]["Ontario"] == "11" and fields["question_1003"].selector == "#question_1003"
    assert fields["question_1007[]"].type == "multiselect" and fields["question_1007[]"].selector == '[id="question_1007[]"]'
    assert fields["location"].label == "Location (City)" and fields["location"].meta["section"] == "location"
    assert fields["country"].meta.get("synthetic") is True
    assert fields["4000"].meta["section"] == "demographic" and fields["4000"].required and "I don't wish to answer" in fields["4000"].options
    assert fields["4001"].type == "multiselect" and not fields["4001"].required
    assert all(f.meta["ats"] == "greenhouse" for f in fields.values())


def test_map_answers_uses_facts_buckets_and_leaves_personal_questions_blank(tmp_path):
    profile = _profile(tmp_path, with_base=False)
    fields = questions_to_fields(board_schema("acme", "123456"))
    answers = {a.field_id: a for a in GreenhouseAdapter().map_answers(fields, profile, {}, "/art/jobs/j1/Alex_Example_Resume.pdf", None, "fair")}
    assert answers["first_name"].value == "Alex" and answers["last_name"].value == "Example" and answers["preferred_name"].value == "Al"
    assert answers["email"].value == "alex@example.com" and answers["phone"].value == "555 000 0000"
    assert answers["resume"].value.endswith("Alex_Example_Resume.pdf") and answers["resume"].fact_ids == ["artifact"]
    assert answers["cover_letter"].value is None and not answers["cover_letter"].needs_input
    assert answers["location"].value == "Milton, Ontario" and answers["country"].value == "Canada"
    assert answers["question_1003"].value == "Ontario"  # province fact matched to the option list
    assert answers["question_1005"].value == "2-4 years" and "3 completed years" in answers["question_1005"].reason  # dates, not a guess
    assert answers["question_1008"].value == "Yes"  # work authorization from the saved answer
    for unknown in ("question_1004", "question_1006", "question_1007[]"):  # hybrid commitment, free text, how did you hear
        assert answers[unknown].value is None and answers[unknown].needs_input, unknown
    assert answers["question_1001"].value is None and not answers["question_1001"].needs_input  # optional LinkedIn link, no saved fact
    # personal questions: optional ones stay blank; a required one takes the decline option and is flagged as sensitive
    assert answers["4001"].value is None and answers["4001"].sensitive and not answers["4001"].needs_input
    assert answers["4000"].value == "I don't wish to answer" and answers["4000"].sensitive and "decline" in answers["4000"].reason
    assert [a.field_id for a in GreenhouseAdapter().map_answers(fields, profile, {}, None, None, "fair")] == [f.id for f in fields]  # form order kept


def test_build_plan_targets_both_layouts_and_ties_the_receipt_to_the_job_id():
    fields = questions_to_fields(board_schema("acme", "123456"))
    job = {"_id": "job_x", "form_plan": {"fields": [asdict(f) for f in fields]}}
    pkg = {"apply_url": "https://job-boards.greenhouse.io/acme/jobs/123456", "document": {"filename": "Alex_Example_Resume.docx"}}
    answers = [
        {"field_id": "first_name", "value": "Alex"},
        {"field_id": "resume", "value": "/art/jobs/job_x/Alex_Example_Resume.pdf"},
        {"field_id": "question_1003", "value": "Ontario"},
        {"field_id": "question_1007[]", "value": "Job board|Other"},
        {"field_id": "location", "value": "Milton, Ontario"},
        {"field_id": "country", "value": "Canada"},
        {"field_id": "4000", "value": "I don't wish to answer"},
        {"field_id": "question_1006", "value": None},
    ]
    plan = GreenhouseAdapter().build_plan(job, pkg, answers)
    by_type = {}
    for s in plan.steps:
        by_type.setdefault(s.type, []).append(s)
    assert by_type["fill"][0].selector.startswith("#first_name") and by_type["fill"][0].label == "First Name"
    upload = by_type["upload"][0]
    assert upload.selector.startswith("input[type=file]#resume") and upload.label == "Resume/CV" and plan.upload_filename == "Alex_Example_Resume.pdf"
    assert "[aria-labelledby=upload-label-resume] input[type=file]" in upload.selector  # the React board's Resume/CV group
    assert "grnhse-*.s3.amazonaws.com" in plan.resource_hosts  # the board posts the chosen file to its bucket before the submit
    choose = {s.value: s for s in by_type["choose"]}
    assert choose["Ontario"].option_selectors == ["#question_1003_11"] and choose["I don't wish to answer"].selector.startswith('[id="4000"]')
    multi = by_type["choose_multi"][0]
    assert multi.options == ["Job board", "Other"] and multi.option_selectors == ['[id="question_1007[]_501"]', '[id="question_1007[]_503"]']
    auto = {s.value: s for s in by_type["autocomplete"]}
    assert auto["Milton, Ontario"].selector.startswith("#candidate-location") and auto["Canada"].optional
    assert plan.steps[-1].final and "#submit_app" in plan.steps[-1].selector and "#application-form button[type=submit]" in plan.steps[-1].selector
    assert plan.receipt_url_pattern == "/confirmation" and plan.receipt_url_must_include == ["123456"] and plan.frame_selector is None
    assert "job-boards.cdn.greenhouse.io" in plan.resource_hosts and "www.recaptcha.net" in plan.resource_hosts and plan.site == "Greenhouse"
    assert len([s for s in plan.steps if s.type == "fill"]) == 1  # blank answers produce no steps
    embedded = GreenhouseAdapter().build_plan({**job, "posting_url": pkg["apply_url"]}, {**pkg, "apply_url": "https://careers.acme.example/jobs/1"}, answers)
    assert embedded.frame_selector == "#grnhse_iframe" and embedded.receipt_url_must_include == ["123456"]


def test_registry_routes_greenhouse_hosts_and_fields():
    assert registry.for_url("https://boards.greenhouse.io/faire/jobs/8831748002?gh_jid=8831748002").name == "greenhouse"
    assert registry.for_url("https://job-boards.eu.greenhouse.io/x/jobs/1").name == "greenhouse"
    assert registry.for_url("https://jobs.lever.co/acme/1").name == "lever"
    assert registry.for_url("https://jobs.eu.lever.co/acme/1").name == "lever"
    assert registry.for_url("https://jobs.ashbyhq.com/acme/bbbbbbbb-cccc-dddd-eeee-ffffffffffff").name == "ashby"
    assert registry.for_url("https://careers.example/jobs/1") is None and registry.for_url(None) is None
    assert registry.for_fields(questions_to_fields(board_schema("acme", "123456"))).name == "greenhouse"
    assert registry.for_fields([]) is None


# ---------------------------------------------------------------- browser + mongo: the whole path on the fixture board


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


skip_no_browser = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def board():
    """Fixture board + a Greenhouse adapter pointed at it, registered for the duration of the test."""
    with FixtureServer(_free_port()) as srv:
        adapter = GreenhouseAdapter(api_base=f"{srv.base}/gh-api/v1/boards", board_hosts={f"127.0.0.1:{srv.port}"})
        registry.register(adapter)
        try:
            yield srv, adapter
        finally:
            registry._adapters.remove(adapter)


def _browser(tmp_path, solver=None, attempts: int = 3) -> BrowserAdapter:
    artifact_dir = tmp_path / "artifacts"
    cfg = BrowserConfig(allowed_hosts={"127.0.0.1"}, upload_root=artifact_dir, artifact_dir=artifact_dir, session_dir=tmp_path / "sessions", captcha_max_attempts=attempts)
    return BrowserAdapter(cfg, captcha_solver=solver)


def _prepared(store, tmp_path, board, variant: str = "new", apply_url: str | None = None, receipt_timeout_ms: int = 30000, upload_timeout_ms: int | None = None):
    """Job on the fixture board -> questions from the fixture API -> package with Joss's answers -> approval."""
    srv, adapter = board
    url = f"{srv.base}/acme/jobs/123456" + (f"?variant={variant}" if variant != "new" else "")
    job = _job(store, req="R-123456", url=url)
    if apply_url:
        JobStore(store).set_status(job["_id"], job["status"], apply_url=apply_url)
        job = JobStore(store).get(job["_id"])
    qs = adapter.fetch_questions(job)
    assert qs.source == "greenhouse_job_board_api" and qs.external_job_id == "123456"
    JobStore(store).set_status(job["_id"], job["status"], form_plan={"fields": [asdict(f) for f in qs.fields], "source": qs.source}, apply_route="greenhouse")
    svc = _service(store, tmp_path, _profile(tmp_path))
    app = svc.prepare(job["_id"], qs.fields)
    assert app["status"] == "needs_input"
    outstanding = app["review_package"]["outstanding"]
    assert "answer needed: Why do you want to work at Acme?" in outstanding and any("hybrid" in o for o in outstanding) and any("hear about" in o for o in outstanding)
    assert not any("gender" in o.lower() for o in outstanding)  # the personal question never becomes a question for Joss
    app = svc.supply_inputs(app["_id"], {"question_1006": "I like reliable backend systems.", "question_1004": "Yes", "question_1007[]": "Job board|Other"})
    assert app["status"] == "ready_for_review", app["review_package"]["outstanding"]
    AuthorizationStore(store).grant("application_submit", ScopeBinding(target_id=job["_id"], material_hash=app["material_hash"]))
    if receipt_timeout_ms != 30000 or upload_timeout_ms:
        original = adapter.build_plan

        def shorter(job_, pkg, answers):
            plan = original(job_, pkg, answers)
            plan.steps[-1].timeout_ms = receipt_timeout_ms
            for s in plan.steps:
                if s.type == "upload" and upload_timeout_ms:
                    s.timeout_ms = upload_timeout_ms
            return plan

        adapter.build_plan = shorter  # type: ignore[method-assign]
    return svc, job, app


def _labels(app: dict) -> list[str]:
    return [s["label"] for s in (app.get("evidence") or {}).get("screenshots", [])]


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_new_layout_submits_with_receipt_and_replay_never_resubmits(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board)
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "succeeded", out.reason
    sub = greenhouse_site.GH_SUBMISSIONS[-1]
    assert len(greenhouse_site.GH_SUBMISSIONS) == before + 1 and sub["variant"] == "new"
    doc_path = Path(app["review_package"]["answers"][5]["value"])  # the resume answer: the tailored file from the package
    assert sub["resume"]["filename"] == doc_path.name and sub["resume"]["bytes"] == doc_path.stat().st_size
    assert sub["first_name"] == "Alex" and sub["question_1003"] == "Ontario" and sub["question_1005"] == "2-4 years" and sub["question_1008"] == "Yes"
    assert sub["question_1007[]"] == ["501", "503"] and sub["location"] == "Milton, Ontario, Canada" and sub["country"] == "Canada"
    assert sub["demographic_4000"] == "I don't wish to answer" and "demographic_4001" not in sub or sub.get("demographic_4001") in ("", None)
    assert sub["question_1004"] == "Yes" and sub["question_1006"] == "I like reliable backend systems."
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "submitted" and app["evidence_basis"] == "receipt_screenshot_reviewed" and _labels(app) == ["summary", "receipt"]
    assert app["evidence"]["final_url"].endswith("/acme/jobs/123456/confirmation") and app["evidence"]["verified_identifiers"] == ["123456"]
    assert "Thank you for applying" in app["evidence"]["receipt_text"]
    for s in app["evidence"]["screenshots"]:
        assert Path(s["path"]).parent == tmp_path / "artifacts" / "jobs" / job["_id"] / "screenshots" and s["artifact_id"]
    # the board removed the hidden input once the file was chosen (as the real one does): verified through the filename row
    assert out.evidence["preflight"] == {"checked": out.evidence["preflight"]["checked"], "missing": []} and out.evidence["preflight"]["checked"] >= 15
    assert out.evidence["uploads"] == [{"label": "Resume/CV", "filename": doc_path.name, "how": "file_input", "verified": "filename_shown"}]
    # replay / restart: the recorded outcome comes back and the board sees no second submission
    again = svc.execute(app["_id"], "w2", lambda plan: pytest.fail("adapter must not run again"))
    assert again.status == "succeeded" and len(greenhouse_site.GH_SUBMISSIONS) == before + 1
    assert JobStore(store).get(job["_id"])["status"] == "applied"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_attach_button_without_a_file_input_goes_through_the_file_chooser(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="chooser")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "succeeded", out.reason
    sub = greenhouse_site.GH_SUBMISSIONS[-1]
    doc_path = Path(app["review_package"]["answers"][5]["value"])
    assert len(greenhouse_site.GH_SUBMISSIONS) == before + 1 and sub["resume"]["filename"] == doc_path.name and sub["resume"]["bytes"] == doc_path.stat().st_size
    assert out.evidence["uploads"] == [{"label": "Resume/CV", "filename": doc_path.name, "how": "attach_button", "verified": "filename_shown"}]
    assert out.evidence["preflight"]["missing"] == []  # the Attach button under the Resume/CV heading counts as the control


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_no_resume_control_stops_before_anything_is_typed(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="noresume")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "failed" and out.retryable, out.reason
    assert out.reason.startswith("resume field not found: Resume/CV") and "Nothing was filled in or sent" in out.reason
    assert len(greenhouse_site.GH_SUBMISSIONS) == before and out.evidence["steps_done"] == 0 and not out.evidence["final_clicked"]
    assert out.evidence["preflight"]["missing"] == [{"label": "Resume/CV", "type": "upload", "selector": out.evidence["preflight"]["missing"][0]["selector"]}]
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "failed" and _labels(app) == ["failed"]
    assert app["next_action"].startswith("Nothing was sent. The form did not have: Resume/CV.") and "Retry with the app" in app["next_action"]
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready" and JobStore(store).get(job["_id"])["status"] == "qualified"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_missing_ordinary_field_is_reported_by_the_form_check_before_typing(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="missing")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "failed" and out.retryable
    assert out.reason == "the form is missing fields the app expected: Phone. Nothing was filled in or sent"
    assert len(greenhouse_site.GH_SUBMISSIONS) == before and out.evidence["steps_done"] == 0
    assert [m["label"] for m in out.evidence["preflight"]["missing"]] == ["Phone"]
    assert svc.apps.find_one({"_id": app["_id"]})["next_action"].startswith("Nothing was sent. The form did not have: Phone.")


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_unconfirmed_upload_stops_before_the_submit_click(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="stuck", upload_timeout_ms=1500)
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "failed" and out.retryable, out.reason
    doc_path = Path(app["review_package"]["answers"][5]["value"])
    assert out.reason.startswith(f"could not confirm the resume was attached: '{doc_path.name}' is not shown on the form")
    assert len(greenhouse_site.GH_SUBMISSIONS) == before and not out.evidence["final_clicked"] and out.evidence["steps_done"] == 5  # the five fills before the upload
    assert out.evidence["uploads"][0]["how"] == "file_input" and out.evidence["uploads"][0]["verified"] is None
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "failed" and _labels(app) == ["failed"] and app["next_action"].startswith("Nothing was sent.")
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_legacy_layout_inside_company_iframe_uses_labels_native_selects_and_radios(store, tmp_path, board):
    srv, _ = board
    svc, job, app = _prepared(store, tmp_path, board, apply_url=f"{srv.base}/embed/acme/123456")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "succeeded", out.reason
    sub = greenhouse_site.GH_SUBMISSIONS[-1]
    assert len(greenhouse_site.GH_SUBMISSIONS) == before + 1 and sub["variant"] == "legacy"
    assert sub["first_name"] == "Alex" and sub["resume"]["filename"].endswith((".pdf", ".docx"))
    # custom questions carried no field-name ids: found through their labels / fieldset legends
    assert "11" in sub.values() and "Yes" not in sub.values()  # province option id via native select; radios post their value ids
    assert sub["location"] == "Milton, Ontario, Canada" and sub["demographic_4000"] == "43"
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "submitted" and _labels(app) == ["summary", "receipt"]
    assert app["evidence"]["final_url"].endswith("/acme/jobs/123456/confirmation") and app["evidence"]["verified_identifiers"] == ["123456"]


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_validation_error_after_submit_is_a_retryable_failure_not_a_submission(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="invalid")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "failed" and out.retryable and "area code" in out.reason
    assert len(greenhouse_site.GH_SUBMISSIONS) == before
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "failed" and set(_labels(app)) == {"summary", "validation"}
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready"  # nothing irreversible happened
    assert JobStore(store).get(job["_id"])["status"] == "qualified"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_human_check_without_the_opt_in_becomes_a_handoff_with_a_challenge_screenshot(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="check")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "handoff" and out.reason == "Greenhouse asked for a human check"
    assert len(greenhouse_site.GH_SUBMISSIONS) == before
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "handoff" and app["handoff_kind"] == "human_check" and "yourself" in app["next_action"]
    assert out.evidence["challenge"] == "text_captcha" and "captcha_attempts" not in out.evidence
    assert [s["label"] for s in out.evidence["screenshots"]] == ["summary", "challenge"]
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_missing_confirmation_is_needs_verification_never_submitted(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="slow", receipt_timeout_ms=1500)
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh"))
    assert out.status == "needs_verification" and "receipt not observed" in out.reason
    assert len(greenhouse_site.GH_SUBMISSIONS) == before + 1  # the board did record it: exactly when a blind retry would double-apply
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "needs_verification" and _labels(app) == ["summary", "ambiguous"]
    calls = []
    assert svc.execute(app["_id"], "w1", lambda plan: calls.append(1)).status == "needs_verification" and not calls


@pytest.mark.mongo
def test_changing_an_answer_after_approval_invalidates_it(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board)
    first_hash = app["material_hash"]
    app = svc.supply_inputs(app["_id"], {"question_1006": "A different answer."})
    assert app["material_hash"] != first_hash
    out = svc.execute(app["_id"], "w1", lambda plan: {"verified": True})
    assert out.status == "denied" and "material content changed" in out.reason
    assert len(greenhouse_site.GH_SUBMISSIONS) == 0 or greenhouse_site.GH_SUBMISSIONS[-1]["job_id"] != job["_id"]


# ---------------------------------------------------------------- opt-in automatic captcha step


def _fake_ai(store, answer: dict) -> tuple[AIRunner, FakeProvider]:
    provider = FakeProvider({"CaptchaOutput": lambda user: answer})
    return AIRunner(store, provider, catalog, Pricing(None), Budget(50_000, 1.0, 50, 300), run_id="run_captcha"), provider


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_opt_in_solver_reads_text_captcha_then_submits_and_records_every_attempt(store, tmp_path, board):
    ai, provider = _fake_ai(store, {"kind": "text", "text": TEXT_CAPTCHA_ANSWER, "confidence": 0.9})
    svc, job, app = _prepared(store, tmp_path, board, variant="check")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path, ModelCaptchaSolver(ai)).run_plan(plan, site_key="gh"))
    assert out.status == "succeeded", out.reason
    assert len(greenhouse_site.GH_SUBMISSIONS) == before + 1
    assert provider.calls and provider.calls[0]["model"] == "CaptchaOutput" and provider.calls[0]["images"] == 1
    assert "Type the characters" in provider.calls[0]["user"] and "<untrusted" in provider.calls[0]["user"]
    att = out.evidence["captcha_attempts"]
    assert len(att) == 1 and att[0]["solved"] and att[0]["kind"] == "text_captcha" and att[0]["answer"] == "text" and att[0]["usage"]["input_tokens"] == 120
    assert [s["label"] for s in out.evidence["screenshots"]] == ["summary", "challenge", "receipt"]
    assert store.c("usage").find_one({"skill": {"$regex": "^solve_captcha@"}, "run_id": "run_captcha"})
    app = svc.apps.find_one({"_id": app["_id"]})
    assert app["status"] == "submitted" and app["evidence"]["verified_identifiers"] == ["123456"]


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_opt_in_solver_picks_grid_cells(store, tmp_path, board):
    ai, provider = _fake_ai(store, {"kind": "cells", "cells": GRID_CAPTCHA_CELLS, "confidence": 0.8})
    svc, job, app = _prepared(store, tmp_path, board, variant="grid")
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path, ModelCaptchaSolver(ai)).run_plan(plan, site_key="gh"))
    assert out.status == "succeeded", out.reason
    assert "grid_rows: 3" in provider.calls[0]["user"] and "grid_cols: 3" in provider.calls[0]["user"]
    assert out.evidence["captcha_attempts"][0]["solved"] and out.evidence["captcha_attempts"][0]["kind"] == "image_grid"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_solver_attempts_are_capped_then_the_human_check_handoff_applies(store, tmp_path, board):
    ai, provider = _fake_ai(store, {"kind": "text", "text": "WRONG", "confidence": 0.2})
    svc, job, app = _prepared(store, tmp_path, board, variant="check")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path, ModelCaptchaSolver(ai), attempts=2).run_plan(plan, site_key="gh"))
    assert out.status == "handoff" and out.reason == "Greenhouse asked for a human check"
    assert len(greenhouse_site.GH_SUBMISSIONS) == before
    att = out.evidence["captcha_attempts"]
    assert len(att) == 2 and not any(a["solved"] for a in att)
    assert len(provider.calls) == 1  # same picture + instruction -> exact-input cache, one paid call
    assert max(e["attempts"] for e in greenhouse_site.CAPTCHA_ANSWERS.values()) == 2
    assert svc.apps.find_one({"_id": app["_id"]})["handoff_kind"] == "human_check"


@skip_no_browser
@pytest.mark.browser
def test_solver_that_cannot_answer_falls_back_without_touching_the_form(tmp_path, board):
    srv, adapter = board
    solver = NeverSolver()
    fields = questions_to_fields(board_schema("acme", "123456"))
    job = {"_id": "job_c", "form_plan": {"fields": [asdict(f) for f in fields]}}
    resume = tmp_path / "artifacts" / "jobs" / "job_c" / "r.pdf"
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF-1.4")
    answers = [
        {"field_id": k, "value": v}
        for k, v in {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": "1", "resume": str(resume), "question_1006": "x", "location": "Milton, Ontario"}.items()
    ]
    plan = adapter.build_plan(job, {"apply_url": f"{srv.base}/acme/jobs/123456?variant=check"}, answers)
    with pytest.raises(HandoffRequired) as exc:
        _browser(tmp_path, solver, attempts=3).run_plan(plan, site_key="gh")
    assert solver.calls == 3 and exc.value.evidence["challenge"] == "text_captcha"
    assert all(a["answer"] == "cannot" and not a["solved"] for a in exc.value.evidence["captcha_attempts"])


def test_captcha_answer_filters_cells_outside_the_grid():
    class _AI:
        def run(self, name, inputs, **kw):
            from amorehiremepls.core.ai import SkillResult, Usage

            assert name == "solve_captcha" and kw["images"] and kw["untrusted_fields"] == ("instruction",)
            return SkillResult(output={"kind": "cells", "cells": [0, 8, 9, -1, "4"], "text": ""}, usage=Usage(), cache_key="k", skill="solve_captcha@x")

    from amorehiremepls.apply.captcha import ChallengeWidget

    widget = ChallengeWidget("image_grid", "[x]", b"png", "pick", "[x] input", "[x] button", grid=(3, 3))
    ans = ModelCaptchaSolver(_AI()).solve(widget)
    assert isinstance(ans, CaptchaAnswer) and ans.kind == "cells" and ans.cells == [0, 8, 4]


@pytest.mark.mongo
def test_runtime_only_builds_a_solver_when_the_toggle_is_on(store, tmp_path):
    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.runtime import Runtime
    from tests.conftest import TEST_MONGO_URI

    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(s, store=store, provider=FakeProvider())
    assert s.captcha_autosolve is False and rt.captcha_solver() is None  # default: off
    store.set_control("captcha_autosolve", True)
    assert isinstance(rt.captcha_solver(), ModelCaptchaSolver)
    store.set_control("captcha_autosolve", False)
    assert rt.captcha_solver() is None
    rt_env = Runtime(Settings(**{**s.model_dump(), "captcha_autosolve": True}, _env_file=None), store=store, provider=FakeProvider())
    store.set_control("captcha_autosolve", None)
    assert isinstance(rt_env.captcha_solver(), ModelCaptchaSolver)  # environment default applies when the toggle was never set


@pytest.mark.mongo
def test_scan_fields_prefers_the_board_api_and_marks_the_route(store, tmp_path, board):
    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.runtime import RealServices, Runtime
    from tests.conftest import TEST_MONGO_URI

    srv, _ = board
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(s, store=store, provider=FakeProvider())
    job = _job(store, req="R-123456", url=f"{srv.base}/acme/jobs/123456")
    fields = RealServices(rt).scan_fields(job)
    assert [f["id"] for f in fields][:3] == ["first_name", "last_name", "preferred_name"] and fields[0]["meta"]["ats"] == "greenhouse"
    job = JobStore(store).get(job["_id"])
    assert job["apply_route"] == "greenhouse" and job["form_plan"]["source"] == "greenhouse_job_board_api" and job["form_plan"]["external_job_id"] == "123456"
    assert RealServices(rt).scan_fields(job) == fields  # cached on the job


def _runtime(store, tmp_path, **over):
    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.runtime import Runtime
    from tests.conftest import TEST_MONGO_URI

    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
        **over,
    )
    return Runtime(s, store=store, provider=FakeProvider())


@pytest.mark.mongo
def test_execute_passes_a_hold_only_when_the_wait_is_on(store, tmp_path, board):
    """The live path holds the same page. Turning the wait off leaves the immediate handoff."""
    from amorehiremepls.core.runtime import RealServices

    svc, _job_doc, app = _prepared(store, tmp_path, board, variant="check")
    rt = _runtime(store, tmp_path, challenge_retry_minutes=0)
    seen: dict = {}

    def run_plan(plan, site_key="default", observe=True, captcha_solver=None, hold=None):
        seen["hold"] = hold
        raise HandoffRequired("Greenhouse asked for a human check", {"challenge": "text_captcha", "url": plan.start_url})

    rt.browser.run_plan = run_plan
    out = RealServices(rt).execute(app["_id"], "w1")
    assert out["status"] == "handoff" and seen["hold"] is not None and seen["hold"].minutes == 30
    assert rt.browser.config.cdp_port == 9222
    assert store.c("human_holds").count_documents({}) == 0  # the fake adapter never reached a check, so nothing was held

    rt_off = _runtime(store, tmp_path, human_check_hold_minutes=0, challenge_retry_minutes=0)
    seen.clear()
    rt_off.browser.run_plan = run_plan
    app2 = svc.apps.find_one({"_id": app["_id"]})
    svc.apps.update_one({"_id": app["_id"]}, {"$set": {"status": "authorized"}})
    out_off = RealServices(rt_off).execute(app2["_id"], "w1")
    assert out_off["status"] == "handoff" and seen["hold"] is None


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_human_check_hold_times_out_into_a_handoff_without_a_second_submit(store, tmp_path, board):
    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.human_check import HumanHold

    svc, job, app = _prepared(store, tmp_path, board, variant="check")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    hold = HumanHold(store, svc.actions, Settings(_env_file=None, human_check_hold_minutes=1), app["_id"], job["_id"], "w1", poll_seconds=0.2)
    hold.minutes = 0.05  # three seconds; the fixture check never confirms on its own
    out = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh", hold=hold))
    assert out.status == "handoff" and out.evidence.get("hold") == "timeout"
    assert len(greenhouse_site.GH_SUBMISSIONS) == before
    doc = store.c("human_holds").find_one({"application_id": app["_id"]})
    assert doc["status"] == "timeout" and doc["idempotency_key"] == f"submit:{job['_id']}"
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_continue_rechecks_the_same_page_and_does_not_submit(store, tmp_path, board):
    import threading
    import time

    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.human_check import HumanHold, active_hold, request_continue

    svc, job, app = _prepared(store, tmp_path, board, variant="check")
    before = len(greenhouse_site.GH_SUBMISSIONS)
    hold = HumanHold(store, svc.actions, Settings(_env_file=None, human_check_hold_minutes=1), app["_id"], job["_id"], "w1", poll_seconds=0.2)
    box: dict = {}

    def run():
        try:
            box["out"] = svc.execute(app["_id"], "w1", lambda plan: _browser(tmp_path).run_plan(plan, site_key="gh", hold=hold))
        except Exception as exc:  # surfaced below; the hold must not be left running
            box["err"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    continued = False
    for _ in range(80):
        if active_hold(store, app["_id"]):
            assert request_continue(store, svc.apps.find_one({"_id": app["_id"]})) == "ok"
            continued = True
            break
        if "out" in box or "err" in box:
            break
        time.sleep(0.25)
    for _ in range(80):
        if "out" in box or "err" in box:
            break
        time.sleep(0.25)
    worker.join(70)
    assert continued, box
    assert "err" not in box, box.get("err")
    out = box["out"]
    assert out.status == "handoff" and out.evidence.get("hold") != "timeout"
    assert len(greenhouse_site.GH_SUBMISSIONS) == before
    assert store.c("actions").count_documents({"idempotency_key": f"submit:{job['_id']}"}) == 1
    assert svc.actions.by_key(f"submit:{job['_id']}")["status"] == "ready"


@skip_no_browser
@pytest.mark.browser
@pytest.mark.mongo
def test_confirm_only_later_try_opens_the_page_and_does_not_submit(store, tmp_path, board):
    svc, job, app = _prepared(store, tmp_path, board, variant="check")
    app = svc.apps.find_one({"_id": app["_id"]})
    _, adapter = board
    plan = adapter.build_plan(job, {"apply_url": job["apply_url"]}, app["review_package"]["answers"])
    before = len(greenhouse_site.GH_SUBMISSIONS)
    with pytest.raises(HandoffRequired, match="no confirmation"):
        _browser(tmp_path).run_plan(plan, site_key="gh", confirm_only=True)
    assert len(greenhouse_site.GH_SUBMISSIONS) == before
