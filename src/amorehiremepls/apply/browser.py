"""Playwright execution adapter: deterministic plan executor with compact observations.

A `plan` is a list of steps (fill, select, check, upload, click, expect_text, wait_url, assert_value, choose,
choose_multi, autocomplete). Site adapters produce plans; this module executes them inside an isolated
browser context:
  * top-level navigation restricted to allowed hosts; sub-resources (scripts, styles, fonts, frames) may
    additionally come from the plan's / config's `resource_hosts`; everything else is aborted
  * no downloads, dedicated storage state per site, no access to the host filesystem beyond the
    specific files passed for upload
  * login challenges raise HandoffRequired (never bypassed)
  * a CAPTCHA / human check raises HandoffRequired too, unless an opt-in `CaptchaSolver` is attached and
    the challenge is one of the simple kinds it supports (text image, image grid); every attempt is
    recorded on the evidence and the attempt count is capped
  * a timeout after the final submit raises AmbiguousOutcome with evidence (screenshot, URL, text)

Steps run inside `plan.frame_selector` when that iframe is present (embedded job boards), otherwise on the page.
`choose` / `choose_multi` / `autocomplete` resolve the control kind at run time (native select, React style
combobox, radio / checkbox group), so one plan covers several layouts of the same form.

Form check first: before anything is typed, every required step's control is resolved on the loaded page
(one page load). A missing control stops the run as a retryable failure that names the missing fields;
nothing was filled in or sent.

`upload` resolves the file control in this order: the step's selector; a file input whose label, group
heading or nearest section heading mentions the step label (or resume / CV); a file input whose id, name or
data attributes mention it; a visible Attach / Upload button inside such a section, driven through the file
chooser. Hidden inputs receive the file directly. The upload is then verified (the input still reports the
file, or the filename appears on the form, which is what React boards show after they swap the input for a
filename row) before the run moves on; an unverified upload stops the run before the submit click.

Screenshots: `summary` is the fully filled final review step captured immediately before the final submit
click (after any `assert_value` review checks passed); `receipt` is the verified confirmation page; an
ambiguous outcome after the final click keeps the distinct `ambiguous` label; a human check keeps `challenge`.
Each entry in `evidence["screenshots"]` is `{label, path, sha256, captured_at, url}`. Files are written under the
job's artifact directory (`<artifact_dir>/jobs/<job_id>/screenshots/`) when a job id is known, otherwise under
`screenshot_dir`.

Authenticated sessions: `login_interactive(site)` opens a headed browser on the desktop for the owner to sign
in, then saves Playwright storage_state under the private session dir. Workers load that state.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from amorehiremepls.apply.network_log import sensitive_values, watch_network
from amorehiremepls.core.actions import ActionFailed, ActionHandoff, AmbiguousOutcome
from amorehiremepls.core.util import redact_free, sha256_file, utcnow

log = logging.getLogger(__name__)

LOGIN_PATTERNS = re.compile(r"(sign ?in|log ?in|password|verify you are human|captcha|one-time code|two-factor)", re.I)
SCREENSHOT_LABELS = ("summary", "receipt", "ambiguous", "receipt_mismatch", "failed", "handoff", "validation", "challenge")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
HUMAN_CHECK_REASON = "the job site asked for a human check"

# Visible challenge widgets. Kind -> (selector that is visible only while a challenge is open, supported by the solver?)
CHALLENGE_SELECTORS: list[tuple[str, str]] = [
    ("text_captcha", '[data-captcha-challenge="text"]'),
    ("image_grid", '[data-captcha-challenge="grid"]'),
    ("recaptcha", 'iframe[src*="recaptcha"][src*="bframe"]'),
    ("hcaptcha", 'iframe[src*="hcaptcha.com"][src*="frame=challenge"]'),
    ("turnstile", 'iframe[src*="challenges.cloudflare.com"]'),
    ("human_check", "[data-captcha-challenge]"),
]
SOLVABLE_CHALLENGES = ("text_captcha", "image_grid")
VALIDATION_SELECTOR = "[role=alert], .error, .field-error, .helper-text--error, .input-wrapper--error [aria-invalid=true], [aria-invalid=true]"
FILE_WORDS = re.compile(r"resume|résumé|\bcv\b|curriculum", re.I)
ATTACH_WORDS = re.compile(r"attach|upload|choose file|browse|select file|add file", re.I)
PREFLIGHT_TYPES = ("fill", "select", "check", "upload", "click", "choose", "choose_multi", "autocomplete")
# Rows in the field's own menu and in a menu drawn later on the same frame or the top page.
SUGGESTION_SELECTOR = '[role="option"], .select__option, [id*="-option-"], .pac-item, [role="listbox"] li'
_ROW_JS = (
    "els => els.map(el => {"
    " const style = window.getComputedStyle(el);"
    " if (el.hidden || style.display === 'none' || style.visibility === 'hidden') return '';"
    " return (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();"
    " })"
)
_FOCUS_JS = (
    "els => els.findIndex(el => el.getAttribute('aria-selected') === 'true' "
    "|| (el.className && el.className.toString().includes('focused')) "
    "|| el.classList.contains('pac-item-selected'))"
)
# A menu drawn over the field (react-select portals this). Read and press the row in the page so a covered
# input click cannot time out before the place is chosen.
_MENU_ROWS_JS = """() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (el.hidden || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const labels = [];
  for (const el of document.querySelectorAll('.select__option, [role="option"], .pac-item')) {
    if (!visible(el)) continue;
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (text && !labels.includes(text)) labels.push(text);
  }
  return labels;
}"""
_MENU_CLICK_JS = """(label) => {
  const want = (label || '').replace(/\\s+/g, ' ').trim();
  for (const el of document.querySelectorAll('.select__option, [role="option"], .pac-item')) {
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (text !== want) continue;
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    el.click();
    return true;
  }
  return false;
}"""

# Runs in the page: context text for every file input (own label, aria-labelledby of its group, nearest heading-like
# text in the enclosing sections) plus its identifying attributes. Index = position in `input[type=file]`.
_FILE_INPUTS_JS = """() => {
  const heading = (el) => {
    let hop = el;
    for (let i = 0; i < 8 && hop; i++) {
      const ids = hop.getAttribute && hop.getAttribute('aria-labelledby');
      if (ids) { const t = ids.split(/\\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ').trim(); if (t) return t; }
      const h = hop.querySelector && hop.querySelector('legend, h1, h2, h3, h4, h5, [class*="label"], [class*="heading"], [class*="title"]');
      if (h && h.innerText && h.innerText.trim()) return h.innerText.trim();
      hop = hop.parentElement;
    }
    return '';
  };
  return Array.from(document.querySelectorAll('input[type=file]')).map(el => {
    const own = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label');
    const attrs = [el.id, el.name, el.getAttribute('aria-label')].concat(Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name + '=' + a.value));
    return {label: own ? (own.innerText || '').trim() : '', heading: heading(el), attrs: attrs.filter(Boolean).join(' ')};
  });
}"""
# Visible buttons that open a file picker, with the same context text.
_ATTACH_BUTTONS_JS = """() => {
  const heading = (el) => {
    let hop = el;
    for (let i = 0; i < 8 && hop; i++) {
      const ids = hop.getAttribute && hop.getAttribute('aria-labelledby');
      if (ids) { const t = ids.split(/\\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ').trim(); if (t) return t; }
      const h = hop.querySelector && hop.querySelector('legend, h1, h2, h3, h4, h5, [class*="label"], [class*="heading"], [class*="title"]');
      if (h && h.innerText && h.innerText.trim()) return h.innerText.trim();
      hop = hop.parentElement;
    }
    return '';
  };
  return Array.from(document.querySelectorAll('button, [role=button], label[for]'))
    .map(el => ({text: (el.innerText || '').trim(), heading: heading(el), hidden: el.getClientRects().length === 0}));
}"""


def host_allowed(host: str, hosts: set[str]) -> bool:
    """Exact host, or a glob pattern such as `grnhse-*.s3.amazonaws.com` (used for vendor upload buckets)."""
    return host in hosts or any("*" in h and fnmatchcase(host, h) for h in hosts)


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


def location_queries(value: str, fallbacks: list[str] | None = None) -> list[str]:
    """Places to type into a location box, in order.

    The saved value comes first. When it names a city and a region, the city alone is next. Each saved
    alternate place follows. Blank and repeated queries are dropped.
    """
    queries: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        cleaned = " ".join((text or "").split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            queries.append(cleaned)

    add(value)
    head = (value or "").split(",", 1)[0]
    if head.strip().casefold() != (value or "").strip().casefold():
        add(head)
    for item in fallbacks or []:
        add(item)
    return queries


def _place_parts(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


_DIAL_CODE = re.compile(r"\s*\(?\+\d[\d\s-]*\)?$")  # a country row in a phone picker ends with its calling code: "Canada +1"


def _city_part(text: str) -> str:
    parts = _place_parts(text)
    first = parts[0] if parts else text.strip()
    return _DIAL_CODE.sub("", first).strip()


def _longer_city_name(suggestion_city: str, typed_city: str) -> bool:
    """True when the row's first part only begins with the typed city, as a beach, valley, or island does."""
    return bool(typed_city) and suggestion_city.startswith(typed_city) and len(suggestion_city) > len(typed_city)


def _parts_agree(parts: list[str], suggestion: str) -> bool:
    words = re.findall(r"[a-z0-9]+", suggestion)

    def part_ok(part: str) -> bool:
        if part in suggestion:
            return True
        head = part.split()[0]
        return len(head) >= 4 and any(len(word) == 2 and head.startswith(word) for word in words)

    return all(part_ok(part) for part in parts)


def _part_agrees(want: str, got: str) -> bool:
    if want == got or want in got or got in want:
        return True
    return _parts_agree([want], got)


def _match_rank(want: str, got: str) -> int | None:
    if got == want:
        return 3
    longer = _longer_city_name(_city_part(got), _city_part(want))
    if want in got and not longer:
        return 2
    parts = _place_parts(want)
    if longer:
        return 1 if len(parts) < 2 or _parts_agree(parts, got) else None
    if len(parts) < 2:
        return 1 if want in got else None
    return 1 if _parts_agree(parts, got) else None


def _region_bonus(query: str, suggestion: str, preferred: str) -> int:
    """An exact city row ranks higher when its later parts agree with the saved region and country."""
    if _city_part(suggestion) != _city_part(query):
        return 0
    saved = _place_parts(preferred)
    found = _place_parts(suggestion)
    bonus = 0
    if len(saved) >= 2 and len(found) >= 2 and _part_agrees(saved[1], found[1]):
        bonus += 1
    if len(saved) >= 3 and len(found) >= 3 and _part_agrees(saved[2], found[2]):
        bonus += 1
    return bonus


def suggestion_rank(query: str, suggestion: str, preferred: str | None = None) -> int | None:
    """How well a suggestion matches a typed place. None means it does not. Higher is a closer match.

    A two letter word in the suggestion counts as a region when the typed region starts with those letters,
    so a short region code still matches the saved region name. A longer name that only starts with the
    city ranks below the row whose first part is that city. When `preferred` names a region or country,
    an exact city row that agrees with those parts ranks above one that does not.
    """
    want = " ".join(query.casefold().split())
    got = " ".join(suggestion.casefold().split())
    if not want or not got:
        return None
    base = _match_rank(want, got)
    if base is None or not preferred:
        return base
    place = " ".join(preferred.casefold().split())
    return base + _region_bonus(want, got, place)


def exact_city_row(query: str, suggestion: str) -> bool:
    """The row's first comma-separated part is the typed city, not a longer name that only starts with it."""
    want = " ".join(query.casefold().split())
    got = " ".join(suggestion.casefold().split())
    return bool(want and got) and _city_part(got) == _city_part(want)


def _label_from_path(path: str) -> str:
    stem = Path(path).stem
    for label in sorted(SCREENSHOT_LABELS, key=len, reverse=True):
        if stem.endswith("_" + label):
            return label
    return stem.rsplit("_", 1)[-1] if "_" in stem else "unknown"


class HandoffRequired(ActionHandoff):
    """Login wall or human check: the browser stopped before the submit went through."""


@dataclass
class Step:
    type: str  # fill | select | check | upload | click | expect_text | wait_url | assert_value | choose | choose_multi | autocomplete
    selector: str = ""  # CSS; a comma list of alternatives is allowed (first match wins)
    value: str = ""
    text: str = ""
    timeout_ms: int = 10000
    final: bool = False  # the irreversible submit click
    label: str = ""  # fallback: find the control by its visible label when no selector matches (other layouts)
    options: list[str] = field(default_factory=list)  # choose_multi: option labels to tick
    option_selectors: list[str] = field(default_factory=list)  # choose/choose_multi: per-option radio/checkbox selectors (same order as options)
    optional: bool = False  # skip silently when the control does not exist on this layout
    sensitive: bool = False  # self-identification answer: error messages and evidence never quote the value
    fallbacks: list[str] = field(default_factory=list)  # autocomplete: further queries when the value is not offered


@dataclass
class Plan:
    start_url: str
    steps: list[Step]
    receipt_text: str  # text that must appear after the final step (one of `receipt_patterns` when those are set)
    receipt_must_include: list[str] = field(default_factory=list)  # e.g. requisition id in the receipt text
    upload_filename: str | None = None
    job_id: str | None = None  # names screenshot files and selects the job's artifact directory
    receipt_patterns: list[str] = field(default_factory=list)  # regex alternatives for the confirmation text
    receipt_url_pattern: str | None = None  # regex the final URL may match instead of / in addition to the text
    receipt_url_must_include: list[str] = field(default_factory=list)  # identifiers that must appear in the final URL
    frame_selector: str | None = None  # run the steps inside this iframe when it exists (embedded boards)
    resource_hosts: list[str] = field(default_factory=list)  # extra hosts for scripts/styles/frames (never navigation)
    site: str = ""  # plain site name for messages ("Greenhouse")


def _note_text(message: str) -> str:
    """One saved step line. Drops the browser call log and anything the existing secret filter covers."""
    text = " ".join((message or "").split())
    cut = text.lower().find("call log:")
    if cut >= 0:
        text = text[:cut].strip()
    return redact_free(text)[:300]


def _timeout_text(exc: BaseException) -> str:
    return _note_text(str(exc))[:180]


def timeout_failure_note(plan: Plan, evidence: dict | None) -> str:
    """Why a step stopped waiting. Names the field. Does not include the typed value."""
    step_no = int((evidence or {}).get("steps_done") or 0) + 1
    steps = plan.steps if plan is not None else []
    step = steps[step_no - 1] if 0 < step_no <= len(steps) else None
    label = ((step.label or step.type) if step else "").strip()
    what = f" at step {step_no} ({label})" if label else ""
    return f"the form did not respond in time{what}; nothing was sent"


def step_note(step: Step) -> str:
    """One line for What to do now, taken from the step itself. A personal answer is never included."""
    name = (step.label or "").strip()
    if step.sensitive:
        return f"Answering {name}" if name else "Answering a personal question"
    if step.type == "upload":
        return f"Attaching {name}" if name else "Attaching a file"
    if step.type == "click" and step.final:
        return "Sending the application"
    if step.type == "click":
        return f"Continuing ({name})" if name else "Continuing"
    if step.type == "check":
        return f"Checking {name}" if name else "Checking a box"
    if name:
        return f"Filling {name}"
    return step.type or "Working"


def _option_list(options: list[str] | None, limit: int = 4) -> str:
    """The first few choices a list actually showed, for a difficulty line."""
    cleaned: list[str] = []
    for item in options or []:
        text = " ".join(str(item).split())[:40]
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return ", ".join(cleaned[:-1]) + ", and " + cleaned[-1]


def location_attempt_note(label: str, query: str, chosen: str | None = None, missed: bool = False, offered: list[str] | None = None) -> str:
    """What the location box just did, using the place that was typed."""
    name = (label or "").strip() or "Location"
    if chosen:
        return f"{name}: typed {query}. A list appeared. Chose {chosen}."
    if missed:
        options = _option_list(offered)
        if options:
            return f"{name}: a list appeared for {query} but none matched. The site showed {options}"
        return f"{name}: no list appeared for {query}"
    return f"{name}: typing {query}"


def location_failure_reason(queries: list[str], offered: list[str] | None = None) -> str:
    """Places that were typed, and whether any list appeared. Unmatched choices are named when a list did."""
    places = ", then ".join(query for query in queries if query)
    options = _option_list(offered)
    if not places:
        return "The location box did not show a place. No list appeared." if not options else f"The location box showed {options}, and none matched."
    if options:
        return f"The location box typed {places}. A list appeared but none matched. The site showed {options}."
    return f"The location box typed {places}. No list appeared."


ANSWERABLE_FIELDS = {"fill", "select", "choose", "choose_multi", "autocomplete"}
PAGE_NOT_CONTINUED = "The page could not stay open while waiting for an answer. Starting again begins at the top of the form and does not continue this form. Nothing was sent."


def apply_location_fallbacks(plan: Plan, fallbacks: list[str]) -> None:
    """Attach alternate places to location suggestion steps. The approved answer stays the first query."""
    extras = [item.strip() for item in fallbacks if item and item.strip()]
    for step in plan.steps:
        if step.type == "autocomplete" and (step.label or "").strip().casefold() == "location":
            step.fallbacks = extras


@dataclass
class BrowserConfig:
    allowed_hosts: set[str]
    headless: bool = True
    session_dir: Path | None = None
    screenshot_dir: Path | None = None  # fallback when no job id / artifact dir is known
    default_timeout_ms: int = 15000
    upload_root: Path | None = None  # uploads must live under this directory (tailored artifacts only)
    artifact_dir: Path | None = None  # screenshots go to <artifact_dir>/jobs/<job_id>/screenshots/
    resource_hosts: set[str] = field(default_factory=set)  # sub-resource hosts allowed on every plan
    captcha_max_attempts: int = 3
    cdp_port: int = 0  # localhost debug port, used only while a human-check hold is active


@dataclass
class _ShotTarget:
    directory: Path | None
    job_id: str | None = None


class _Scope:
    """Page or iframe: everything the steps touch goes through here."""

    def __init__(self, page, frame_selector: str | None = None) -> None:
        self.page = page
        self.frame_selector = frame_selector
        self.root = page.frame_locator(frame_selector) if frame_selector else page

    def locator(self, selector: str):
        return self.root.locator(selector)

    def get_by_label(self, text: str, exact: bool = False):
        return self.root.get_by_label(text, exact=exact)

    def get_by_text(self, text, exact: bool = False):
        return self.root.get_by_text(text, exact=exact)

    def current_url(self) -> str:
        """URL of the frame the form lives in (the iframe's own URL when one is used)."""
        if self.frame_selector:
            try:
                return self.root.locator("body").evaluate("() => location.href", timeout=1000)
            except Exception:
                pass
        return self.page.url

    def body_text(self) -> str:
        """Visible text of the page (and the iframe when one is used). Empty while the page is navigating."""
        text = ""
        for root in [self.page, self.root] if self.frame_selector else [self.page]:
            try:
                text += "\n" + root.locator("body").inner_text(timeout=1000)
            except Exception:
                continue
        return text


class BrowserAdapter:
    def __init__(self, config: BrowserConfig, captcha_solver: Any | None = None, network: Any | None = None) -> None:
        self.config = config
        self.captcha_solver = captcha_solver  # opt-in; only ever used from the application-submit path
        self.network = network  # per-job request log; each row is saved as Chromium sees it
        self._progress = None

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

    def run_plan(
        self,
        plan: Plan,
        site_key: str = "default",
        observe: bool = True,
        captcha_solver: Any | None = None,
        hold: Any | None = None,
        confirm_only: bool = False,
        progress: Any | None = None,
    ) -> dict[str, Any]:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        from amorehiremepls.core.human_check import launch_args

        self._check_host(plan.start_url)
        self._progress = progress
        solver = captcha_solver if captcha_solver is not None else self.captcha_solver
        evidence: dict[str, Any] = {"started_at": utcnow().isoformat(), "steps_done": 0, "final_clicked": False}
        shots = self._shot_target(plan)
        state = self._state_path(site_key)
        # Debug port only while a hold may run, and only on localhost. A failed launch drops the port and continues.
        # A confirm-only later try never holds and never clicks.
        cdp_port = int(self.config.cdp_port or 0) if hold is not None and not confirm_only else 0
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=self.config.headless, args=launch_args(cdp_port))
            except Exception:
                if not cdp_port:
                    raise
                cdp_port = 0
                browser = pw.chromium.launch(headless=self.config.headless, args=launch_args(0))
            evidence["cdp_port"] = cdp_port or None
            ctx_kwargs: dict[str, Any] = {"accept_downloads": False, "viewport": {"width": 1280, "height": 900}}
            if state and state.exists():
                ctx_kwargs["storage_state"] = str(state)
            context = browser.new_context(**ctx_kwargs)
            # Record every request from this moment, before the first page load and before any form step.
            if self.network is not None and plan.job_id:
                watch_network(context, self.network, plan.job_id, sensitive_values(plan))
            context.set_default_timeout(self.config.default_timeout_ms)
            allowed = self.config.allowed_hosts
            resources = {h.lower() for h in [*self.config.resource_hosts, *plan.resource_hosts]}

            def gate(route, request):
                host = urlsplit(request.url).netloc.split(":")[0].lower()
                if request.url.startswith("data:") or host in allowed:
                    route.continue_()
                    return
                if host_allowed(host, resources):
                    try:  # resource hosts may serve scripts, styles, fonts and child frames, never the top-level page
                        top_level = request.is_navigation_request() and request.frame.parent_frame is None
                    except Exception:
                        top_level = True
                    if not top_level:
                        route.continue_()
                        return
                route.abort()

            context.route("**/*", gate)
            page = context.new_page()
            scope = _Scope(page)
            try:
                page.goto(plan.start_url, wait_until="domcontentloaded")
                self._detect_login(page, evidence, shots)
                scope = self._scope_for(page, plan)
                if confirm_only:
                    # The first try already clicked. Look for a confirmation and do not click again.
                    evidence["confirm_only"] = True
                    evidence["final_clicked"] = False
                    if not self._receipt_ready(scope, plan):
                        challenge = self._detect_challenge(scope)
                        self._snapshot(page, evidence, "challenge" if challenge else "ambiguous", shots)
                        site = plan.site or "the job site"
                        extra = {"url": page.url}
                        if challenge:
                            evidence["challenge"] = challenge["kind"]
                            extra["challenge"] = challenge["kind"]
                        raise HandoffRequired(f"{site} showed no confirmation on the later try", {**evidence, **extra})
                else:
                    self._run_steps(scope, plan, evidence, shots, solver, hold)
                body = scope.body_text()[:4000]
                final_url = scope.current_url()
                evidence.update(receipt_text=body, final_url=final_url, observed_at=utcnow().isoformat())
                expected = [m for m in plan.receipt_must_include if m]
                matched = [m for m in expected if m.lower() in body.lower()]
                url_expected = [m for m in plan.receipt_url_must_include if m]
                url_matched = [m for m in url_expected if m.lower() in final_url.lower()]
                evidence["matched_identifiers"] = matched + [m for m in url_matched if m not in matched]  # only what was really seen
                missing = [m for m in expected if m not in matched] + [m for m in url_expected if m not in url_matched]
                if missing:
                    self._snapshot(page, evidence, "receipt_mismatch", shots)
                    raise AmbiguousOutcome(f"receipt does not reference expected identifiers: {missing}", evidence)
                evidence["verified"] = True
                self._snapshot(page, evidence, "receipt", shots)
                if state:
                    state.parent.mkdir(parents=True, exist_ok=True)
                    context.storage_state(path=str(state))
                return evidence
            except (AmbiguousOutcome, HandoffRequired):
                raise
            except ActionFailed as exc:
                self._say(str(exc))
                if "steps_done" not in exc.evidence:
                    # a step-level stop (file not confirmed, option not offered, ...): keep the run evidence and the page as it looked
                    if not evidence.get("final_clicked") and not any(s.get("label") in ("failed", "validation") for s in evidence.get("screenshots") or []):
                        self._snapshot(page, evidence, "failed", shots)
                    exc.evidence = {**evidence, **exc.evidence}
                raise
            except PWTimeout as exc:
                if evidence.get("final_clicked"):
                    short = _timeout_text(exc)
                    self._say(f"timeout after the form was sent: {short}" if short else "timeout after the form was sent")
                    self._snapshot(page, evidence, "ambiguous", shots)
                    raise AmbiguousOutcome(f"timeout after final action: {exc}", evidence) from exc
                evidence["error"] = _timeout_text(exc)[:400]
                reason = timeout_failure_note(plan, evidence)
                self._say(reason)
                self._snapshot(page, evidence, "failed", shots)
                raise ActionFailed(reason, evidence, retryable=True) from exc
            finally:
                context.close()
                browser.close()
                self._progress = None

    def _say(self, message: str) -> None:
        """Record one real step so the application page can show it. A note that fails does not stop the form."""
        note = getattr(self, "_progress", None)
        text = _note_text(message)
        if not note or not text:
            return
        try:
            note(text)
        except Exception:
            log.exception("could not record the current step")

    def _run_steps(self, scope: _Scope, plan: Plan, evidence: dict, shots: _ShotTarget, solver: Any | None, hold: Any | None) -> None:
        from playwright.sync_api import TimeoutError as PWTimeout

        page = scope.page
        self._preflight(scope, plan, evidence, shots)
        for i, step in enumerate(plan.steps):
            if evidence.get("user_finished"):
                return
            if step.final:
                # the filled-in review state exactly as it looked before the irreversible click
                self._snapshot(page, evidence, "summary", shots)
                evidence["final_clicked"] = True
                evidence["final_url_before"] = page.url
            if step.type != "autocomplete":
                self._say(step_note(step))
            try:
                self._do_step(scope, step, plan, evidence)
            except (ActionFailed, PWTimeout) as exc:
                if not self._should_ask(step, evidence, hold):
                    raise
                reason = self._difficulty_line(plan, evidence, step, exc)
                answer = self._ask_for_answer(scope, plan, step, evidence, hold, reason)
                if evidence.get("user_finished"):
                    return
                if not answer:
                    self._say(PAGE_NOT_CONTINUED)
                    raise ActionFailed(PAGE_NOT_CONTINUED, evidence, retryable=False) from exc
                self._type_supplied(scope, step, answer, evidence)
            evidence["steps_done"] = i + 1
            if step.type in ("click",) and not step.final:
                self._detect_login(page, evidence, shots)
                self._detect_validation_errors(scope, evidence, shots)
        self._await_receipt(scope, plan, evidence, shots, solver, hold)

    def _should_ask(self, step: Step, evidence: dict, hold: Any | None) -> bool:
        """A required field that did not complete can wait for one dashboard answer. Optional fields are unchanged."""
        if hold is None or step.optional or step.final or evidence.get("final_clicked"):
            return False
        return step.type in ANSWERABLE_FIELDS

    def _difficulty_line(self, plan: Plan, evidence: dict, step: Step, exc: BaseException) -> str:
        """Why this field stopped. A sensitive value is never included."""
        if step.sensitive:
            name = (step.label or "This field").strip()
            return f"{name} needs an answer."
        if isinstance(exc, ActionFailed):
            saved = str((exc.evidence or {}).get("difficulty") or "").strip()
            if saved:
                return saved
            text = _note_text(str(exc))
            if text:
                return text
        return timeout_failure_note(plan, evidence)

    def _ask_for_answer(self, scope: _Scope, plan: Plan, step: Step, evidence: dict, hold: Any, reason: str) -> str:
        """Keep this same page open until the dashboard sends one answer. Does not navigate or open another browser."""
        self._say(reason)
        label = (step.label or "This field").strip()
        field = {"purpose": "field", "field_label": label, "field_reason": reason, "sensitive": bool(step.sensitive)}
        outcome = self._hold_page(scope, plan, hold, evidence.get("cdp_port"), field=field)
        if outcome == "receipt":
            evidence["user_finished"] = True
            hold.finish("continued")
            return ""
        if outcome != "continue":
            hold.finish("timeout")
            return ""
        hold.finish("continued")
        reader = getattr(hold, "supplied_answer", None)
        text = " ".join(str(reader() or "").split()) if reader else ""
        return text[:200]

    def _type_supplied(self, scope: _Scope, step: Step, answer: str, evidence: dict) -> None:
        """Type the dashboard answer into the same control and continue. A sensitive value is not recorded."""
        label = (step.label or "This field").strip()
        if step.sensitive:
            self._say(f"Answer received for {label}")
        else:
            self._say(f"{label}: typing {answer}")
        loc = self._require(scope, step)
        if loc is None:
            raise ActionFailed(f"{label} is not on the page, so the answer could not be typed. Nothing was sent", retryable=False)
        if step.type == "autocomplete":
            if self._kind(loc) == "select":
                loc.select_option(label=answer, timeout=step.timeout_ms)
                chosen = answer
            else:
                chosen = self._type_and_pick(scope, loc, answer, step.timeout_ms, step.sensitive, preferred=answer)
            self._note_place(evidence, step, "" if step.sensitive else answer, "" if step.sensitive else chosen)
            if not step.sensitive:
                self._say(location_attempt_note(label, answer, chosen=chosen))
            return
        if step.type == "fill":
            loc.fill(answer, timeout=step.timeout_ms)
            return
        if step.type == "select":
            loc.select_option(label=answer, timeout=step.timeout_ms)
            return
        if step.type == "choose":
            self._pick_option(scope, loc, answer, step.timeout_ms, exact=False, sensitive=step.sensitive)
            return
        if step.type == "choose_multi":
            box = self._in_group(scope, step.label, answer) or scope.get_by_label(answer, exact=True).first
            box.check(timeout=step.timeout_ms)
            return
        raise ActionFailed(f"{label} could not take the answer. Nothing was sent", retryable=False)

    # -- form check before anything is typed ------------------------------------------------------------
    def _preflight(self, scope: _Scope, plan: Plan, evidence: dict, shots: _ShotTarget) -> None:
        """Resolve every required control on the loaded page before the first fill. One page load, no typing.

        The page may still be rendering (React boards): the first required control is awaited with the step's
        timeout, then the rest are checked without waiting. Multi-page forms are checked up to and including the
        first click (the button that leads to the next page); later pages are not loaded here. Missing controls
        stop the run as a retryable failure whose reason lists the fields in plain words; the action returns to
        `ready` and nothing was sent."""
        first_page: list[Step] = []
        for s in plan.steps:
            first_page.append(s)
            if s.type in ("click", "wait_url", "expect_text"):
                break
        required = [s for s in first_page if s.type in PREFLIGHT_TYPES and not s.optional and (s.selector or s.label)]
        if not required:
            return
        first = required[0]
        try:
            scope.locator(first.selector).first.wait_for(state="attached", timeout=first.timeout_ms) if first.selector else None
        except Exception:
            pass  # resolved again below through the label / heading fallbacks; missing controls are reported together
        missing: list[dict[str, str]] = []
        for step in required:
            if not self._present(scope, step):
                missing.append({"label": step.label or step.selector[:80], "type": step.type, "selector": step.selector})
        evidence["preflight"] = {"checked": len(required), "missing": missing}
        if missing:
            self._snapshot(scope.page, evidence, "failed", shots)
            names = ", ".join(m["label"] for m in missing)
            file_missing = any(m["type"] == "upload" for m in missing)
            head = "resume field not found" if file_missing and FILE_WORDS.search(names) else "the form is missing fields the app expected"
            raise ActionFailed(f"{head}: {names}. Nothing was filled in or sent", evidence, retryable=True)

    def _present(self, scope: _Scope, step: Step) -> bool:
        """Can this step's control be found right now (no waiting)?"""
        try:
            if step.type == "upload":
                return self._find_upload(scope, step) is not None or self._attach_button(scope, step) is not None
            if step.type in ("choose", "choose_multi"):
                if any(scope.locator(sel).first.count() for sel in step.option_selectors if sel):
                    return True
                if self._find(scope, step) is not None:
                    return True
                options = step.options or [step.value]
                return any(self._in_group(scope, step.label, o) is not None for o in options if o)
            return self._find(scope, step) is not None
        except Exception:
            return False

    # -- receipt / challenge loop -----------------------------------------------------------------------
    def _await_receipt(self, scope: _Scope, plan: Plan, evidence: dict, shots: _ShotTarget, solver: Any | None, hold: Any | None = None) -> None:
        """After the final click: wait for the confirmation, but stop early on a human check or a validation error."""
        page = scope.page
        timeout_ms = plan.steps[-1].timeout_ms if plan.steps else 15000
        deadline = time.monotonic() + timeout_ms / 1000
        attempts = 0
        while True:
            if self._receipt_ready(scope, plan):
                return
            challenge = self._detect_challenge(scope)
            if challenge:
                self._snapshot(page, evidence, "challenge", shots)
                evidence["challenge"] = challenge["kind"]
                if solver is not None and challenge["kind"] in SOLVABLE_CHALLENGES and attempts < self.config.captcha_max_attempts:
                    attempts += 1
                    solved = self._try_solve(scope, challenge, solver, evidence, attempts)
                    if solved:
                        deadline = time.monotonic() + timeout_ms / 1000  # the submit continues after the check
                        continue
                    if attempts < self.config.captcha_max_attempts and self._detect_challenge(scope):
                        continue
                if hold is not None and not evidence.get("human_hold_done"):
                    # One hold on this page. Continue re-checks the receipt and never clicks submit again.
                    evidence["human_hold_done"] = True
                    outcome = self._hold_page(scope, plan, hold, evidence.get("cdp_port"))
                    if outcome == "timeout":
                        hold.finish("timeout")
                        site = plan.site or "the job site"
                        raise HandoffRequired(f"{site} asked for a human check", {**evidence, "url": page.url, "challenge": challenge["kind"], "hold": "timeout"})
                    hold.finish("continued")
                    deadline = time.monotonic() + timeout_ms / 1000
                    continue
                site = plan.site or "the job site"
                raise HandoffRequired(f"{site} asked for a human check", {**evidence, "url": page.url, "challenge": challenge["kind"]})
            errs = self._validation_errors(scope)
            if errs:
                self._snapshot(page, evidence, "validation", shots)
                # client-side validation kept the form on the page: nothing was sent, so this may be fixed and retried
                raise ActionFailed(f"the form reported a problem: {errs[:3]}", {**evidence, "validation_errors": errs}, retryable=True)
            if time.monotonic() > deadline:
                self._snapshot(page, evidence, "ambiguous", shots)
                raise AmbiguousOutcome("receipt not observed before timeout after final action", evidence)
            page.wait_for_timeout(250)

    def _receipt_ready(self, scope: _Scope, plan: Plan) -> bool:
        patterns = [re.compile(p, re.I) for p in (plan.receipt_patterns or [re.escape(plan.receipt_text)]) if p]
        url_re = re.compile(plan.receipt_url_pattern, re.I) if plan.receipt_url_pattern else None
        if url_re and url_re.search(scope.current_url()):
            return True
        return any(p.search(scope.body_text()) for p in patterns)

    def _hold_page(self, scope: _Scope, plan: Plan, hold: Any, cdp_port: int | None, field: dict | None = None) -> str:
        """Keep this same page open until Continue, a confirmation, or the hold deadline. Does not navigate or click."""
        from amorehiremepls.core.human_check import run_hold

        extra = field or {}
        return run_hold(
            minutes=hold.minutes,
            poll_seconds=hold.poll_seconds,
            sleep=lambda seconds: scope.page.wait_for_timeout(int(seconds * 1000)),
            heartbeat=lambda: hold.heartbeat({"cdp_port": cdp_port, **extra}),
            continue_requested=hold.continue_requested,
            receipt_visible=lambda: self._receipt_ready(scope, plan),
        )

    def _try_solve(self, scope: _Scope, challenge: dict, solver: Any, evidence: dict, attempt: int) -> bool:
        """One bounded solver attempt against a simple challenge widget. Records the attempt either way."""
        from amorehiremepls.apply.captcha import CaptchaAnswer, read_challenge

        record: dict[str, Any] = {"attempt": attempt, "kind": challenge["kind"], "solved": False}
        evidence.setdefault("captcha_attempts", []).append(record)
        try:
            widget = read_challenge(scope, challenge)
            answer: CaptchaAnswer = solver.solve(widget)
            record.update(answer=answer.kind, usage=answer.usage)
            if answer.kind == "text" and answer.text:
                scope.locator(widget.input_selector).first.fill(answer.text)
            elif answer.kind == "cells" and answer.cells:
                for idx in answer.cells:
                    scope.locator(f'{widget.selector} [data-cell="{idx}"]').first.click()
            else:
                return False
            scope.locator(widget.submit_selector).first.click()
            scope.page.wait_for_timeout(500)
            record["solved"] = self._detect_challenge(scope) is None
        except Exception as exc:  # a solver problem is never a submission problem; fall back to the human check
            record["error"] = type(exc).__name__
            log.warning("captcha solve attempt %s failed: %s", attempt, type(exc).__name__)
        return bool(record["solved"])

    def _detect_challenge(self, scope: _Scope) -> dict | None:
        for kind, selector in CHALLENGE_SELECTORS:
            try:
                loc = scope.locator(selector).first
                if loc.count() and loc.is_visible():
                    return {"kind": kind, "selector": selector}
            except Exception:
                continue
        return None

    # -- steps ------------------------------------------------------------------------------------------
    def _scope_for(self, page, plan: Plan) -> _Scope:
        if not plan.frame_selector:
            return _Scope(page)
        try:
            page.locator(plan.frame_selector).first.wait_for(state="attached", timeout=3000)
            return _Scope(page, plan.frame_selector)
        except Exception:
            return _Scope(page)

    def _find(self, scope: _Scope, step: Step):
        """Locator for a step: first matching selector alternative, else the visible label, else None."""
        if step.selector:
            loc = scope.locator(step.selector).first
            if loc.count():
                return loc
        if step.label:
            loc = scope.get_by_label(step.label, exact=False).first
            if loc.count():
                return loc
        return None

    def _require(self, scope: _Scope, step: Step):
        loc = self._find(scope, step)
        if loc is None:
            if step.optional:
                return None
            loc = scope.locator(step.selector).first  # let Playwright raise its timeout with the selector in the message
            loc.wait_for(state="attached", timeout=step.timeout_ms)
        return loc

    def _do_step(self, scope: _Scope, step: Step, plan: Plan, evidence: dict | None = None) -> None:
        if step.type == "fill":
            loc = self._require(scope, step)
            if loc is not None:
                loc.fill(step.value, timeout=step.timeout_ms)
        elif step.type == "select":
            loc = self._require(scope, step)
            if loc is not None:
                loc.select_option(step.value, timeout=step.timeout_ms)
        elif step.type == "check":
            loc = self._require(scope, step)
            if loc is not None:
                loc.check(timeout=step.timeout_ms)
        elif step.type == "upload":
            self._upload(scope, step, evidence if evidence is not None else {})
        elif step.type == "click":
            loc = self._require(scope, step)
            if loc is not None:
                loc.click(timeout=step.timeout_ms)
        elif step.type == "expect_text":
            scope.get_by_text(step.text, exact=False).first.wait_for(timeout=step.timeout_ms)
        elif step.type == "wait_url":
            scope.page.wait_for_url(re.compile(step.value), timeout=step.timeout_ms)
        elif step.type == "assert_value":
            loc = self._require(scope, step)
            actual = loc.input_value(timeout=step.timeout_ms) if loc is not None else None
            if actual != step.value:
                raise ActionFailed(f"review state mismatch for {step.selector}: '{actual}' != '{step.value}'")
        elif step.type == "choose":
            self._choose(scope, step)
        elif step.type == "choose_multi":
            self._choose_multi(scope, step)
        elif step.type == "autocomplete":
            self._autocomplete(scope, step, evidence)
        else:
            raise ActionFailed(f"unknown step type {step.type}")

    def _kind(self, loc) -> str:
        js = (
            "el => el.tagName === 'SELECT' ? 'select' : (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') "
            "? 'combobox' : (el.type || el.tagName.toLowerCase())"
        )
        return loc.evaluate(js)

    def _in_group(self, scope: _Scope, label: str, option: str):
        """A radio/checkbox inside the fieldset (or group) whose legend is the question label."""
        if not label:
            return None
        group = scope.locator("fieldset, [role=group], [role=radiogroup]").filter(has_text=label[:60])
        if group.count():
            loc = group.first.get_by_label(option, exact=True).first
            if loc.count():
                return loc
        return None

    def _choose(self, scope: _Scope, step: Step) -> None:
        """Single choice: radio by option selector, native <select> by label, or a combobox (type, then click the exact option)."""
        for sel in step.option_selectors:
            loc = scope.locator(sel).first
            if loc.count():
                loc.check(timeout=step.timeout_ms)
                return
        loc = self._find(scope, step)
        if loc is None:
            grouped = self._in_group(scope, step.label, step.value)
            if grouped is not None:
                grouped.check(timeout=step.timeout_ms)
                return
            loc = self._require(scope, step)
            if loc is None:
                return
        kind = self._kind(loc)
        if kind == "select":
            loc.select_option(label=step.value, timeout=step.timeout_ms)
        elif kind == "combobox":
            self._pick_option(scope, loc, step.value, step.timeout_ms, exact=True, sensitive=step.sensitive)
        elif kind in ("radio", "checkbox"):
            scope.get_by_label(step.value, exact=True).first.check(timeout=step.timeout_ms)
        else:
            loc.fill(step.value, timeout=step.timeout_ms)

    def _choose_multi(self, scope: _Scope, step: Step) -> None:
        """Several choices: checkbox per option selector, else by option label, else a multi-select combobox."""
        picked: set[int] = set()
        for i, sel in enumerate(step.option_selectors):
            if i >= len(step.options):
                break
            loc = scope.locator(sel).first
            if loc.count():
                loc.check(timeout=step.timeout_ms)
                picked.add(i)
        remaining = [o for i, o in enumerate(step.options) if i not in picked]
        if not remaining:
            return
        loc = self._find(scope, step)
        kind = self._kind(loc) if loc is not None else ""
        if kind == "select":
            loc.select_option(label=remaining, timeout=step.timeout_ms)
        elif kind == "combobox":
            for opt in remaining:
                self._pick_option(scope, loc, opt, step.timeout_ms, exact=True, sensitive=step.sensitive)
        else:
            for opt in remaining:
                box = self._in_group(scope, step.label, opt) or scope.get_by_label(opt, exact=True).first
                box.check(timeout=step.timeout_ms)

    def _autocomplete(self, scope: _Scope, step: Step, evidence: dict | None = None) -> None:
        """Type into a suggestion box and pick a suggestion for the value, then each later place."""
        loc = self._require(scope, step)
        if loc is None:
            return
        queries = location_queries(step.value, step.fallbacks)
        shown: list[str] = []
        if self._kind(loc) == "select":
            last: Exception | None = None
            for query in queries:
                if not step.sensitive:
                    self._say(location_attempt_note(step.label, query))
                try:
                    loc.select_option(label=query, timeout=step.timeout_ms)
                except Exception as exc:
                    last = exc
                    got = [] if step.sensitive else self._choice_texts(loc)
                    self._remember_choices(shown, got)
                    if not step.sensitive:
                        self._say(location_attempt_note(step.label, query, missed=True, offered=got))
                    continue
                self._note_place(evidence, step, query, query)
                if not step.sensitive:
                    self._say(location_attempt_note(step.label, query, chosen=query))
                return
            if step.optional:
                self._skip_optional(step)
                return
            self._fail_autocomplete(step, queries, shown, last)
        for query in queries:
            if not step.sensitive:
                self._say(location_attempt_note(step.label, query))
            try:
                chosen = self._type_and_pick(scope, loc, query, step.timeout_ms, step.sensitive, preferred=step.value)
            except ActionFailed as exc:
                got = [] if step.sensitive else [str(item).strip() for item in (exc.evidence.get("offered") or []) if str(item).strip()]
                self._remember_choices(shown, got)
                if not step.sensitive:
                    self._say(location_attempt_note(step.label, query, missed=True, offered=got))
                continue
            self._note_place(evidence, step, query, chosen)
            if not step.sensitive:
                self._say(location_attempt_note(step.label, query, chosen=chosen))
            return
        if step.optional:
            self._leave_as_was(loc)
            self._skip_optional(step)
            return
        self._fail_autocomplete(step, queries, shown, None)

    def _skip_optional(self, step: Step) -> None:
        """An optional suggestion box that offers no matching row is left alone; the form decides whether it minds."""
        name = (step.label or "This field").strip()
        self._say(f"{name}: no row matched, so it was left as it was. The field is optional.")

    def _leave_as_was(self, loc) -> None:
        """Close the list and drop what was typed so an unmatched query is not left in the box."""
        for action in (lambda: loc.press("Escape", timeout=1000), lambda: loc.fill("", timeout=1000)):
            try:
                action()
            except Exception:
                pass

    def _remember_choices(self, shown: list[str], got: list[str]) -> None:
        for item in got:
            if item and item not in shown:
                shown.append(item)

    def _choice_texts(self, loc) -> list[str]:
        try:
            return [text.strip() for text in loc.locator("option").all_inner_texts() if text and text.strip()][:8]
        except Exception:
            return []

    def _fail_autocomplete(self, step: Step, queries: list[str], shown: list[str], last: Exception | None) -> None:
        """Every saved place was tried. The reason names those places, or hides a sensitive value."""
        if step.sensitive:
            name = (step.label or "This field").strip()
            reason = f"{name} needs an answer."
            raise ActionFailed(reason, {"difficulty": reason}, retryable=True) from last
        reason = location_failure_reason(queries, shown)
        detail = {"value": step.value, "tried": queries, "offered": shown[:8], "difficulty": reason}
        raise ActionFailed(reason, detail, retryable=True) from last

    def _note_place(self, evidence: dict | None, step: Step, query: str, chosen: str) -> None:
        if evidence is None or (step.label or "").strip().casefold() != "location" or step.sensitive:
            return
        evidence["location_typed"] = query
        evidence["location_chosen"] = chosen

    def _pause(self, scope: _Scope, ms: int) -> None:
        page = getattr(scope, "page", scope)
        waiter = getattr(page, "wait_for_timeout", None)
        if not callable(waiter):
            waiter = getattr(scope, "wait_for_timeout", None)
        if not callable(waiter):
            return
        try:
            waiter(ms)
        except Exception:
            return

    def _frame_of(self, loc):
        """The frame that contains this input. A menu drawn beside the field lives in that same frame."""
        marker = getattr(loc, "frame", None)
        if marker is not None and hasattr(marker, "locator"):
            return marker
        try:
            handle = loc.element_handle(timeout=1000)
        except Exception:
            return None
        if handle is None:
            return None
        try:
            return handle.owner_frame()
        except Exception:
            return None

    def _menu_roots(self, scope: _Scope, frame) -> list:
        roots = []
        for root in (frame, scope, getattr(scope, "page", None)):
            if root is not None and root not in roots:
                roots.append(root)
        return roots

    def _nearby_menu(self, loc):
        try:
            shell = loc.locator("xpath=ancestor::*[contains(@class,'select')][1]")
            return shell.locator(SUGGESTION_SELECTOR)
        except Exception:
            return None

    def _owned_menu(self, root, loc):
        try:
            menu_id = (loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns") or "").strip()
        except Exception:
            return None
        if not menu_id:
            return None
        safe = menu_id.replace("\\", "").replace('"', "")
        try:
            return root.locator(f'[id="{safe}"]').locator(SUGGESTION_SELECTOR)
        except Exception:
            return None

    def _broad_menu(self, root):
        try:
            return root.locator(SUGGESTION_SELECTOR)
        except Exception:
            return None

    def _row_entries(self, menu) -> list[tuple[int, str]]:
        if menu is None:
            return []
        try:
            if menu.count() == 0:
                return []
        except Exception:
            return []
        raw: list | None = None
        try:
            got = menu.evaluate_all(_ROW_JS)
            if isinstance(got, list):
                raw = got
        except Exception:
            raw = None
        if raw is None:
            try:
                raw = list(menu.all_inner_texts())
            except Exception:
                return []
        entries: list[tuple[int, str]] = []
        for index, text in enumerate(raw):
            cleaned = " ".join(str(text or "").split())
            if cleaned:
                entries.append((index, cleaned))
        return entries

    def _best_entry(self, query: str, entries: list[tuple[int, str]], preferred: str | None) -> tuple[int, str, int] | None:
        best: tuple[int, str, int] | None = None
        for index, text in entries:
            if not exact_city_row(query, text):
                continue
            rank = suggestion_rank(query, text, preferred=preferred)
            if rank is None:
                continue
            if best is None or rank > best[2]:
                best = (index, text, rank)
        return best

    def _best_in_menus(self, menus: list, query: str, preferred: str | None, seen: list[str]) -> tuple[Any, int, str, int] | None:
        best: tuple[Any, int, str, int] | None = None
        for menu in menus:
            entries = self._row_entries(menu)
            for _index, text in entries:
                if text not in seen:
                    seen.append(text)
            picked = self._best_entry(query, entries, preferred)
            if picked is not None and (best is None or picked[2] > best[3]):
                best = (menu, picked[0], picked[1], picked[2])
        return best

    def _focused_index(self, menu) -> int | None:
        try:
            found = menu.evaluate_all(_FOCUS_JS)
        except Exception:
            return None
        return found if isinstance(found, int) and found >= 0 else None

    def _arrow_to(self, loc, focused: int, index: int) -> None:
        delta = index - focused
        key = "ArrowDown" if delta > 0 else "ArrowUp"
        for _ in range(abs(delta)):
            loc.press(key)
        loc.press("Enter")

    def _still_unpicked(self, loc, query: str, chosen: str) -> bool:
        reader = getattr(loc, "input_value", None)
        if not callable(reader):
            return False
        try:
            current = " ".join(str(reader(timeout=400) or "").split())
        except Exception:
            return False
        if not current or chosen.casefold() in current.casefold():
            return False
        if len(_place_parts(chosen)) == 1 and _city_part(chosen.casefold()) == current.casefold():
            return False  # a one part row such as "Canada +1" may show as "Canada" once chosen
        return current.casefold() == " ".join(query.casefold().split())

    def _dom_rows(self, scope: _Scope) -> list[tuple[int, str]]:
        page = getattr(scope, "page", None)
        evaler = getattr(page, "evaluate", None)
        if not callable(evaler):
            return []
        try:
            raw = evaler(_MENU_ROWS_JS)
        except Exception:
            return []
        if not isinstance(raw, list):
            return []
        return [(index, text) for index, text in enumerate(raw) if isinstance(text, str) and text.strip()]

    def _dom_click_label(self, loc, label: str) -> bool:
        page = getattr(loc, "page", None)
        evaler = getattr(page, "evaluate", None)
        if not callable(evaler):
            return False
        try:
            return bool(evaler(_MENU_CLICK_JS, label))
        except Exception:
            return False

    def _select_listed_row(self, loc, menu, index: int, query: str, chosen: str, timeout_ms: int) -> None:
        """Click the row, or move to it with the keyboard when the field still shows only the typed text."""
        if menu is None:
            if self._dom_click_label(loc, chosen) and not self._still_unpicked(loc, query, chosen):
                return
            raise ActionFailed(f"could not choose '{chosen}'", {"value": query, "offered": [chosen]})
        focused = self._focused_index(menu)
        moved = False
        if focused is not None:
            try:
                self._arrow_to(loc, focused, index)
                moved = True
            except Exception:
                moved = False
            if moved and not self._still_unpicked(loc, query, chosen):
                return
        short = min(int(timeout_ms or 2000), 2000)
        try:
            menu.nth(index).click(timeout=short)
        except Exception:
            if self._dom_click_label(loc, chosen) and not self._still_unpicked(loc, query, chosen):
                return
            if not moved:
                self._arrow_to(loc, focused or 0, index)
        if self._still_unpicked(loc, query, chosen):
            if self._dom_click_label(loc, chosen) and not self._still_unpicked(loc, query, chosen):
                return
            if not moved:
                self._arrow_to(loc, focused or 0, index)
            if self._still_unpicked(loc, query, chosen):
                raise ActionFailed(f"could not choose '{chosen}'", {"value": query, "offered": [chosen]})

    def _read_suggestion(self, scope: _Scope, loc, frame, query: str, preferred: str | None) -> tuple[Any, int, str, list[str]] | None:
        """The closest exact city row in the field's menu, its frame, or a menu portaled on the top page."""
        seen: list[str] = []
        field_menus = []
        later_menus = []
        near = self._nearby_menu(loc)
        if near is not None:
            field_menus.append(near)
        for root in self._menu_roots(scope, frame):
            bucket = field_menus if root is frame else later_menus
            owned = self._owned_menu(root, loc)
            broad = self._broad_menu(root)
            if owned is not None:
                bucket.append(owned)
            if broad is not None:
                bucket.append(broad)
        found = self._best_in_menus(field_menus, query, preferred, seen)
        if found is None:
            found = self._best_in_menus(later_menus, query, preferred, seen)
        if found is None:
            dom = self._dom_rows(scope)
            for _index, text in dom:
                if text not in seen:
                    seen.append(text)
            picked = self._best_entry(query, dom, preferred)
            if picked is not None:
                return None, -1, picked[1], seen
        if found is not None:
            return found[0], found[1], found[2], seen
        if seen:
            return None, -1, "", seen
        return None

    def _wait_for_suggestion(self, scope: _Scope, loc, query: str, preferred: str | None, timeout_ms: int, sensitive: bool) -> tuple[Any, int, str]:
        frame = self._frame_of(loc)
        budget = min(max(int(timeout_ms or 0), 1000), 8000)
        ticks = max(1, budget // 200)
        shown = "the chosen personal answer" if sensitive else f"'{query}'"
        last_seen: list[str] = []
        for _tick in range(ticks):
            self._pause(scope, 200)
            found = self._read_suggestion(scope, loc, frame, query, preferred)
            if found is None:
                continue
            menu, index, text, seen = found
            last_seen = seen
            if text and ((menu is not None and index >= 0) or (menu is None and index < 0)):
                return menu, index, text
        if last_seen and not sensitive:
            raise ActionFailed(f"option '{query}' is not offered; the site offers {last_seen[:8]}", {"value": query, "offered": last_seen[:20]})
        if last_seen and sensitive:
            raise ActionFailed(f"option {shown} is not offered by the site", {"offered": last_seen[:20]})
        raise ActionFailed(f"no suggestions appeared for {shown}", {} if sensitive else {"value": query, "offered": []})

    def _take_open(self, scope: _Scope, loc, preferred: str, query: str, timeout_ms: int) -> str | None:
        """Choose a matching row that is already open. Returns the row text when the field accepts it."""
        frame = self._frame_of(loc)
        found = self._read_suggestion(scope, loc, frame, preferred, preferred)
        if not found:
            return None
        menu, index, text, _seen = found
        if not text:
            return None
        try:
            self._select_listed_row(loc, menu, index, query, text, timeout_ms)
        except Exception:
            return None
        if self._still_unpicked(loc, query, text):
            return None
        return text

    def _focus_field(self, loc, timeout_ms: int) -> bool:
        """Click the field. A short timeout: an open list covers the input and must be read, not waited out."""
        short = min(max(int(timeout_ms or 2000), 500), 2000)
        try:
            loc.click(timeout=short)
            return True
        except Exception:
            return False

    def _type_and_pick(self, scope: _Scope, loc, query: str, timeout_ms: int, sensitive: bool, preferred: str | None = None) -> str:
        shown = "the chosen personal answer" if sensitive else f"'{query}'"
        place = preferred or query
        taken = self._take_open(scope, loc, place, query, timeout_ms)
        if taken:
            return taken
        if not self._focus_field(loc, timeout_ms):
            # The click opened the list and then the list covered the field. Choose that row.
            taken = self._take_open(scope, loc, place, query, timeout_ms)
            if taken:
                return taken
            try:
                loc.press("Escape")
            except Exception:
                pass
            try:
                loc.click(timeout=1500, force=True)
            except Exception:
                pass
        # Keystrokes, not a pasted value: the location box only searches as someone types.
        loc.press("Control+A")
        loc.press("Backspace")
        loc.press_sequentially(query, delay=40, timeout=timeout_ms)
        try:
            menu, index, text = self._wait_for_suggestion(scope, loc, query, preferred or query, timeout_ms, sensitive)
        except ActionFailed:
            raise
        except Exception as exc:
            raise ActionFailed(f"no suggestions appeared for {shown}", {} if sensitive else {"value": query, "offered": []}) from exc
        self._select_listed_row(loc, menu, index, query, text, timeout_ms)
        return text

    def _pick_option(self, scope: _Scope, loc, value: str, timeout_ms: int, exact: bool, sensitive: bool = False) -> None:
        shown = "the chosen personal answer" if sensitive else f"'{value}'"  # a self-identification value never lands in evidence
        loc.click(timeout=timeout_ms)
        loc.fill(value if exact else value.split(",")[0].strip(), timeout=timeout_ms)
        options = scope.locator('[role="option"]:visible, .select__option:visible')
        try:
            options.first.wait_for(state="visible", timeout=min(timeout_ms, 5000))
        except Exception as exc:
            raise ActionFailed(f"no suggestions appeared for {shown}", {} if sensitive else {"value": value}) from exc
        texts = [t.strip() for t in options.all_inner_texts()]
        want = value.strip().lower()
        idx = next((i for i, t in enumerate(texts) if t.lower() == want), None)
        if idx is None and not exact:
            idx = next((i for i, t in enumerate(texts) if want in t.lower()), None)
            if idx is None:
                head = want.split(",")[0].strip()
                idx = next((i for i, t in enumerate(texts) if head and head in t.lower()), None)
        if idx is None:
            if sensitive:
                raise ActionFailed(f"option {shown} is not offered by the site", {"offered": texts[:20]})
            raise ActionFailed(f"option '{value}' is not offered; the site offers {texts[:8]}", {"value": value, "offered": texts[:20]})
        options.nth(idx).click(timeout=timeout_ms)

    # -- file upload --------------------------------------------------------------------------------------
    def _file_words(self, step: Step) -> re.Pattern:
        """Words that identify this file control: the step label's words plus resume / CV."""
        words = [re.escape(w) for w in re.split(r"[^A-Za-z0-9é]+", step.label or "") if len(w) > 2]
        return re.compile("|".join([FILE_WORDS.pattern, *words]), re.I) if words else FILE_WORDS

    def _find_upload(self, scope: _Scope, step: Step):
        """Locator for the file input, by selector, then by label / group heading / section heading, then by attributes.
        Returns None when the page has no file input for this control (an Attach button may still open a chooser)."""
        if step.selector:
            loc = scope.locator(step.selector).first
            if loc.count():
                return loc
        inputs = scope.locator("input[type=file]")
        if not inputs.count():
            return None
        try:
            infos = scope.locator("body").evaluate(_FILE_INPUTS_JS)
        except Exception:
            infos = []
        words = self._file_words(step)
        for key in ("label", "heading", "attrs"):
            for idx, info in enumerate(infos):
                if words.search(info.get(key) or ""):
                    return inputs.nth(idx)
        return None

    def _attach_button(self, scope: _Scope, step: Step):
        """A visible Attach / Upload button that sits under a heading naming this file control."""
        try:
            infos = scope.locator("body").evaluate(_ATTACH_BUTTONS_JS)
        except Exception:
            return None
        words = self._file_words(step)
        buttons = scope.locator("button, [role=button], label[for]")
        for idx, info in enumerate(infos):
            if info.get("hidden"):
                continue
            if ATTACH_WORDS.search(info.get("text") or "") and (words.search(info.get("heading") or "") or words.search(info.get("text") or "")):
                return buttons.nth(idx)
        return None

    def _upload(self, scope: _Scope, step: Step, evidence: dict) -> None:
        path = self.check_upload_path(step.value)
        record: dict[str, Any] = {"label": step.label, "filename": path.name, "how": None, "verified": None}
        evidence.setdefault("uploads", []).append(record)
        loc = self._find_upload(scope, step)
        if loc is not None:
            try:
                loc.set_input_files(str(path), timeout=step.timeout_ms)  # works on visually hidden inputs; no click needed
                record["how"] = "file_input"
                record["verified"] = self._verify_upload(scope, loc, path, step)
                return
            except ActionFailed:
                raise
            except Exception as exc:
                log.info("direct file set failed (%s); trying the Attach button", type(exc).__name__)
        button = self._attach_button(scope, step)
        if button is None:
            if loc is None and step.optional:
                evidence["uploads"].remove(record)
                return
            raise ActionFailed(
                f"resume field not found: the form has no file box or Attach button for '{step.label or step.selector}'. Nothing was sent",
                {**evidence, "selector": step.selector, "label": step.label},
                retryable=True,
            )
        with scope.page.expect_file_chooser(timeout=step.timeout_ms) as chooser:
            button.click(timeout=step.timeout_ms)
        chooser.value.set_files(str(path))
        record["how"] = "attach_button"
        record["verified"] = self._verify_upload(scope, None, path, step)

    def _verify_upload(self, scope: _Scope, loc, path: Path, step: Step) -> str:
        """The input still reports the file (`input_files`), or the filename is shown on the page (`filename_shown`).
        React boards replace the input with a filename row (sometimes after an upload progress bar), so the input
        going away is not a failure. Neither within the step's timeout stops the run before the submit click."""
        deadline = time.monotonic() + min(step.timeout_ms, 20000) / 1000
        last = None
        while True:
            if loc is not None:
                try:
                    if loc.count():
                        last = loc.evaluate("el => el.files && el.files[0] ? el.files[0].name : null", timeout=1000)
                        if last == path.name:
                            return "input_files"
                except Exception:
                    pass
            if path.name in scope.body_text():
                return "filename_shown"
            if time.monotonic() > deadline:
                seen = f" (the file box reports '{last}')" if last else ""
                raise ActionFailed(
                    f"could not confirm the resume was attached: '{path.name}' is not shown on the form{seen}. Nothing was sent",
                    {"filename": path.name, "seen": last},
                    retryable=True,
                )
            scope.page.wait_for_timeout(250)

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

    def _validation_errors(self, scope: _Scope) -> list[str]:
        try:
            errs = [e.strip() for e in scope.locator(VALIDATION_SELECTOR).all_inner_texts() if e.strip()]
        except Exception:
            return []
        return list(dict.fromkeys(errs))

    def _detect_validation_errors(self, scope: _Scope, evidence: dict, shots: _ShotTarget | None = None) -> None:
        errs = self._validation_errors(scope)
        if errs:
            self._snapshot(scope.page, evidence, "validation", shots)
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
                        if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
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
