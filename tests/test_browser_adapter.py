import socket
from pathlib import Path

import pytest

from amorehiremepls.actions import ActionFailed, AmbiguousOutcome
from amorehiremepls.browser import BrowserAdapter, BrowserConfig, HandoffRequired, Plan, Step
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


def _plan(base: str, req: str, resume: Path, phone="555 000 0000", slow: int = 0, submit_timeout=8000) -> Plan:
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
    return Plan(start_url=url, steps=steps, receipt_text="Application received", receipt_must_include=[req], upload_filename=resume.name)


@skip_no_browser
def test_multi_step_form_upload_and_receipt(site, tmp_path):
    resume = tmp_path / "Alex_Example_Resume_Acme_R42.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}, screenshot_dir=tmp_path / "shots", session_dir=tmp_path / "sessions"))
    ev = adapter.run_plan(_plan(site.base, "R42", resume), site_key="fixture")
    assert ev["verified"] and "R42" in ev["receipt_text"] and ev["final_clicked"]
    assert fixture_site.SUBMISSIONS[-1]["resume_name"] == resume.name and fixture_site.SUBMISSIONS[-1]["req"] == "R42"
    assert any(p.endswith("_receipt.png") for p in ev["screenshots"])
    assert (tmp_path / "sessions" / "fixture.storage.json").exists()


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
    assert any("ambiguous" in p for p in exc.value.evidence.get("screenshots", []))
    # The employer did receive it: exactly the situation where a blind retry would double-apply.
    assert len(fixture_site.SUBMISSIONS) == before + 1


@skip_no_browser
def test_login_wall_becomes_handoff_and_disallowed_host_is_refused(site, tmp_path):
    adapter = BrowserAdapter(BrowserConfig(allowed_hosts={"127.0.0.1"}))
    plan = Plan(start_url=f"{site.base}/gated/R9", steps=[Step("click", "#submit", final=True)], receipt_text="x")
    with pytest.raises(HandoffRequired):
        adapter.run_plan(plan)
    with pytest.raises(ActionFailed):
        adapter.run_plan(Plan(start_url="https://example.com/apply", steps=[], receipt_text="x"))
