"""Playwright execution adapter: deterministic plan executor with compact observations.

A `plan` is a list of steps (fill, select, check, upload, click, expect_text, wait_url). Site adapters
produce plans; this module executes them inside an isolated browser context:
  * navigation and sub-requests restricted to allowed hosts (everything else aborted)
  * no downloads, dedicated storage state per site, no access to the host filesystem beyond the
    specific files passed for upload
  * login challenges / CAPTCHAs raise HandoffRequired (never bypassed)
  * a timeout after the final submit raises AmbiguousOutcome with evidence (screenshot, URL, text)

Screenshots: `summary` is the fully filled final review step captured immediately before the final submit
click (after any `assert_value` review checks passed); `receipt` is the verified confirmation page; an
ambiguous outcome after the final click keeps the distinct `ambiguous` label. Each entry in
`evidence["screenshots"]` is `{label, path, sha256, captured_at, url}`. Files are written under the job's
artifact directory (`<artifact_dir>/jobs/<job_id>/screenshots/`) when a job id is known, otherwise under
`screenshot_dir`.

Authenticated sessions: `login_interactive(site)` opens a headed browser on the Mac for Joss to sign
in, then saves Playwright storage_state under the private session dir. Workers load that state.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .actions import ActionFailed, AmbiguousOutcome
from .util import sha256_file, utcnow

log = logging.getLogger(__name__)

LOGIN_PATTERNS = re.compile(r"(sign ?in|log ?in|password|verify you are human|captcha|one-time code|two-factor)", re.I)
SCREENSHOT_LABELS = ("summary", "receipt", "ambiguous", "receipt_mismatch", "failed", "handoff", "validation")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_screenshots(raw: Any) -> list[dict[str, Any]]:
    """Return screenshot evidence as a list of `{label, path, sha256, captured_at, url}`.

    Older records stored a plain list of paths; those are widened on read (label inferred from the
    `_<label>.png` suffix, hash/time/url unknown) so readers never need to branch on the shape."""
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, dict):
            if not item.get("path"):
                continue
            out.append(
                {
                    "label": item.get("label") or _label_from_path(item["path"]),
                    "path": str(item["path"]),
                    "sha256": item.get("sha256"),
                    "captured_at": item.get("captured_at"),
                    "url": item.get("url"),
                }
            )
        elif isinstance(item, str) and item:
            out.append({"label": _label_from_path(item), "path": item, "sha256": None, "captured_at": None, "url": None})
    return out


def _label_from_path(path: str) -> str:
    stem = Path(path).stem
    for label in sorted(SCREENSHOT_LABELS, key=len, reverse=True):
        if stem.endswith("_" + label):
            return label
    return stem.rsplit("_", 1)[-1] if "_" in stem else "unknown"


class HandoffRequired(Exception):
    def __init__(self, reason: str, evidence: dict | None = None) -> None:
        super().__init__(reason)
        self.evidence = evidence or {}


@dataclass
class Step:
    type: str  # fill | select | check | upload | click | expect_text | wait_url | assert_value
    selector: str = ""
    value: str = ""
    text: str = ""
    timeout_ms: int = 10000
    final: bool = False  # the irreversible submit click


@dataclass
class Plan:
    start_url: str
    steps: list[Step]
    receipt_text: str  # text that must appear after the final step
    receipt_must_include: list[str] = field(default_factory=list)  # e.g. requisition id
    upload_filename: str | None = None
    job_id: str | None = None  # names screenshot files and selects the job's artifact directory


@dataclass
class BrowserConfig:
    allowed_hosts: set[str]
    headless: bool = True
    session_dir: Path | None = None
    screenshot_dir: Path | None = None  # fallback when no job id / artifact dir is known
    default_timeout_ms: int = 15000
    upload_root: Path | None = None  # uploads must live under this directory (tailored artifacts only)
    artifact_dir: Path | None = None  # screenshots go to <artifact_dir>/jobs/<job_id>/screenshots/


@dataclass
class _ShotTarget:
    directory: Path | None
    job_id: str | None = None


class BrowserAdapter:
    def __init__(self, config: BrowserConfig) -> None:
        self.config = config

    def _shot_target(self, plan: Plan | None = None) -> _ShotTarget:
        job_id = plan.job_id if plan else None
        if job_id and self.config.artifact_dir is not None:
            return _ShotTarget(self.config.artifact_dir / "jobs" / job_id / "screenshots", job_id)
        return _ShotTarget(self.config.screenshot_dir, job_id)

    def _check_host(self, url: str) -> str:
        host = urlsplit(url).netloc.split("@")[-1].lower()
        bare = host.split(":")[0]
        if bare not in self.config.allowed_hosts and host not in self.config.allowed_hosts:
            raise ActionFailed(f"host {bare} not in allowed hosts", {"url": url})
        return bare

    def _state_path(self, site_key: str) -> Path | None:
        if not self.config.session_dir:
            return None
        return self.config.session_dir / f"{site_key}.storage.json"

    def run_plan(self, plan: Plan, site_key: str = "default", observe: bool = True) -> dict[str, Any]:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        self._check_host(plan.start_url)
        evidence: dict[str, Any] = {"started_at": utcnow().isoformat(), "steps_done": 0, "final_clicked": False}
        shots = self._shot_target(plan)
        state = self._state_path(site_key)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.config.headless, args=["--disable-dev-shm-usage"])
            ctx_kwargs: dict[str, Any] = {"accept_downloads": False, "viewport": {"width": 1280, "height": 900}}
            if state and state.exists():
                ctx_kwargs["storage_state"] = str(state)
            context = browser.new_context(**ctx_kwargs)
            context.set_default_timeout(self.config.default_timeout_ms)
            allowed = self.config.allowed_hosts

            def gate(route, request):
                host = urlsplit(request.url).netloc.split(":")[0].lower()
                if host in allowed or request.url.startswith("data:"):
                    route.continue_()
                else:
                    route.abort()

            context.route("**/*", gate)
            page = context.new_page()
            try:
                page.goto(plan.start_url, wait_until="domcontentloaded")
                self._detect_login(page, evidence, shots)
                for i, step in enumerate(plan.steps):
                    if step.final:
                        # the filled-in review state exactly as it looked before the irreversible click
                        self._snapshot(page, evidence, "summary", shots)
                        evidence["final_clicked"] = True
                        evidence["final_url_before"] = page.url
                    self._do_step(page, step, plan)
                    evidence["steps_done"] = i + 1
                    if step.type in ("click",) and not step.final:
                        self._detect_login(page, evidence, shots)
                        self._detect_validation_errors(page, evidence, shots)
                try:
                    page.get_by_text(plan.receipt_text, exact=False).first.wait_for(timeout=plan.steps[-1].timeout_ms if plan.steps else 15000)
                except PWTimeout as exc:
                    self._snapshot(page, evidence, "ambiguous", shots)
                    raise AmbiguousOutcome("receipt not observed before timeout after final action", evidence) from exc
                body = page.inner_text("body")[:4000]
                evidence.update(receipt_text=body, final_url=page.url, observed_at=utcnow().isoformat())
                expected = [m for m in plan.receipt_must_include if m]
                matched = [m for m in expected if m.lower() in body.lower()]
                evidence["matched_identifiers"] = matched  # only what was really seen on the receipt
                missing = [m for m in expected if m not in matched]
                if missing:
                    self._snapshot(page, evidence, "receipt_mismatch", shots)
                    raise AmbiguousOutcome(f"receipt does not reference expected identifiers: {missing}", evidence)
                evidence["verified"] = True
                self._snapshot(page, evidence, "receipt", shots)
                if state:
                    state.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(state))
                return evidence
            except (AmbiguousOutcome, ActionFailed, HandoffRequired):
                raise
            except PWTimeout as exc:
                if evidence.get("final_clicked"):
                    self._snapshot(page, evidence, "ambiguous", shots)
                    raise AmbiguousOutcome(f"timeout after final action: {exc}", evidence) from exc
                self._snapshot(page, evidence, "failed", shots)
                raise ActionFailed(f"timeout before final action: {str(exc)[:200]}", evidence, retryable=True) from exc
            finally:
                context.close()
                browser.close()

    def _do_step(self, page, step: Step, plan: Plan) -> None:
        if step.type == "fill":
            page.fill(step.selector, step.value, timeout=step.timeout_ms)
        elif step.type == "select":
            page.select_option(step.selector, step.value, timeout=step.timeout_ms)
        elif step.type == "check":
            page.check(step.selector, timeout=step.timeout_ms)
        elif step.type == "upload":
            path = self.check_upload_path(step.value)
            page.set_input_files(step.selector, str(path), timeout=step.timeout_ms)
            shown = page.evaluate("sel => { const el = document.querySelector(sel); return el && el.files && el.files[0] ? el.files[0].name : null }", step.selector)
            if shown != path.name:
                raise ActionFailed(f"uploaded filename mismatch: expected {path.name}, saw {shown}")
        elif step.type == "click":
            page.click(step.selector, timeout=step.timeout_ms)
        elif step.type == "expect_text":
            page.get_by_text(step.text, exact=False).first.wait_for(timeout=step.timeout_ms)
        elif step.type == "wait_url":
            page.wait_for_url(re.compile(step.value), timeout=step.timeout_ms)
        elif step.type == "assert_value":
            actual = page.input_value(step.selector, timeout=step.timeout_ms)
            if actual != step.value:
                raise ActionFailed(f"review state mismatch for {step.selector}: '{actual}' != '{step.value}'")
        else:
            raise ActionFailed(f"unknown step type {step.type}")

    def check_upload_path(self, value: str) -> Path:
        """Only files under the configured artifact directory may be uploaded (never arbitrary host paths)."""
        path = Path(value).expanduser().resolve()
        if self.config.upload_root is not None:
            root = self.config.upload_root.resolve()
            if root != path and root not in path.parents:
                raise ActionFailed(f"upload path outside artifact directory: {path.name}", {"path": str(path)})
        if not path.is_file():
            raise ActionFailed(f"upload file missing: {path.name}")
        return path

    def _detect_login(self, page, evidence: dict, shots: _ShotTarget | None = None) -> None:
        try:
            has_pw = page.locator("input[type=password]").count() > 0
            title = page.title() or ""
        except Exception:
            return
        if has_pw or LOGIN_PATTERNS.search(title) or LOGIN_PATTERNS.search(page.url):
            self._snapshot(page, evidence, "handoff", shots)
            raise HandoffRequired("login or human verification challenge detected", {**evidence, "url": page.url})

    def _detect_validation_errors(self, page, evidence: dict, shots: _ShotTarget | None = None) -> None:
        try:
            errs = page.locator("[role=alert], .error, .field-error, [aria-invalid=true]").all_inner_texts()
        except Exception:
            return
        errs = [e.strip() for e in errs if e.strip()]
        if errs:
            self._snapshot(page, evidence, "validation", shots)
            raise ActionFailed(f"form validation errors: {errs[:3]}", {**evidence, "validation_errors": errs})

    def _snapshot(self, page, evidence: dict, label: str, shots: _ShotTarget | None = None) -> None:
        """Full-page PNG named `{timestamp}_{job_id}_{label}.png` (or `{timestamp}_{label}.png` without a job id),
        recorded on the evidence as `{label, path, sha256, captured_at, url}`. Best effort: never fails the run."""
        target = shots or self._shot_target()
        if not target.directory:
            return
        try:
            target.directory.mkdir(parents=True, exist_ok=True)
            captured = utcnow()
            parts = [captured.strftime("%Y%m%dT%H%M%S"), _SAFE_ID.sub("_", target.job_id) if target.job_id else "", label]
            path = target.directory / ("_".join(p for p in parts if p) + ".png")
            page.screenshot(path=str(path), full_page=True)
            evidence.setdefault("screenshots", []).append({"label": label, "path": str(path), "sha256": sha256_file(path), "captured_at": captured.isoformat(), "url": page.url})
            evidence["last_url"] = page.url
        except Exception as exc:  # screenshots are best effort
            evidence["screenshot_error"] = type(exc).__name__

    def scan_form(self, url: str, site_key: str = "default") -> list[dict[str, Any]]:
        """Read-only: list visible form controls with labels, types, options and stable selectors."""
        from playwright.sync_api import sync_playwright

        self._check_host(url)
        state = self._state_path(site_key)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.config.headless)
            context = browser.new_context(accept_downloads=False, storage_state=str(state) if state and state.exists() else None)
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded")
                self._detect_login(page, {})
                fields = page.evaluate(
                    """() => {
                      const out = [];
                      const labelFor = (el) => {
                        if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) return l.innerText.trim(); }
                        const p = el.closest('label'); if (p) return p.innerText.trim();
                        return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.id || '';
                      };
                      document.querySelectorAll('input, select, textarea').forEach((el, i) => {
                        if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button') return;
                        const sel = el.id ? `#${CSS.escape(el.id)}` : (el.name ? `${el.tagName.toLowerCase()}[name="${el.name}"]` : null);
                        if (!sel) return;
                        const opts = el.tagName === 'SELECT' ? Array.from(el.options).map(o => o.text.trim()) : [];
                        const type = el.tagName === 'SELECT' ? 'select' : (el.type || el.tagName.toLowerCase());
                        out.push({id: el.id || el.name || `f${i}`, label: labelFor(el), type: type, options: opts, selector: sel, required: el.required});
                      });
                      return out; }"""
                )
                return fields
            finally:
                context.close()
                browser.close()

    # -- sessions -----------------------------------------------------------------------
    def login_interactive(self, site_key: str, start_url: str, done_when_url_matches: str | None = None, timeout_s: int = 600) -> Path:
        """Headed browser for the user to complete login on the Mac; saves storage state privately."""
        from playwright.sync_api import sync_playwright

        state = self._state_path(site_key)
        if state is None:
            raise ValueError("session_dir not configured")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(storage_state=str(state) if state.exists() else None)
            page = context.new_page()
            page.goto(start_url)
            if done_when_url_matches:
                page.wait_for_url(re.compile(done_when_url_matches), timeout=timeout_s * 1000)
            else:
                page.wait_for_event("close", timeout=timeout_s * 1000)
            state.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(state))
            state.chmod(0o600)
            context.close()
            browser.close()
        return state

    def session_status(self, site_key: str) -> dict[str, Any]:
        state = self._state_path(site_key)
        if not state or not state.exists():
            return {"site": site_key, "present": False}
        return {"site": site_key, "present": True, "saved_at": utcnow().fromtimestamp(state.stat().st_mtime).isoformat()}

    def logout(self, site_key: str) -> bool:
        state = self._state_path(site_key)
        if state and state.exists():
            state.unlink()
            return True
        return False
