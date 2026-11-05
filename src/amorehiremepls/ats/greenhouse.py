"""Greenhouse hosted job boards.

Verified 2026-11-05 against the public Job Board API docs (developers.greenhouse.io/job-board.html) and a read-only
Playwright inspection of the Faire posting (nothing was filled or sent):

* Every hosted URL now lands on the React board `job-boards.greenhouse.io/<org>/jobs/<id>`: `boards.greenhouse.io/...`
  and the embed URL `boards.greenhouse.io/embed/job_app?for=<org>&token=<id>` both redirect there. The form is
  `form#application-form`; controls carry the API field name as their id (`#first_name`, `#last_name`,
  `#preferred_name`, `#email`, `#phone` (tel input), `#resume` / `#cover_letter` (hidden file inputs),
  `#question_<id>` for text answers, `input#question_<id>[role=combobox]` (React select) for single selects, checkbox
  groups `input[name="question_<id>[]"]` with id `question_<id>[]_<value id>` for multi selects, `#country` and
  `#candidate-location` (suggestion boxes, not in the API), demographic questions under their numeric id). The submit
  button is `button[type=submit]` ("Submit application"). A successful submit navigates to `/<org>/jobs/<id>/confirmation`
  which reads "Thank you for applying. Your application has been received." An invisible reCAPTCHA Enterprise badge is
  loaded from www.recaptcha.net / www.gstatic.com; the board's own scripts come from job-boards.cdn.greenhouse.io.
* The legacy layout (`form#application_form`, `#submit_app`, `#job_application_answers_attributes_N_*`, native selects,
  `#grnhse_iframe` on company sites) is no longer served for hosted boards; the plan still copes with it through label
  based fallbacks, native select / radio handling and the optional iframe scope, and the fixture site exercises that path.
* The Job Board API (`GET https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{id}?questions=true`) is the primary
  source of the question list: `questions`, `location_questions`, `compliance` (EEOC) and `demographic_questions`,
  field types `input_text`, `textarea`, `input_file`, `input_hidden`, `multi_value_single_select`,
  `multi_value_multi_select`. There is no separate EU API host.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..browser import Plan, Step
from .base import QuestionSet

if TYPE_CHECKING:
    from ..applications import Answer, FormField
    from ..profile import BandEvidence, Profile

log = logging.getLogger(__name__)

BOARD_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
API_BASE = "https://boards-api.greenhouse.io/v1/boards"
# Sub-resources the React board needs. Never navigation targets; the request gate still blocks every other host.
RESOURCE_HOSTS = [
    "job-boards.cdn.greenhouse.io",
    "s2-recruiting.cdn.greenhouse.io",
    "c.spl.greenhouse.io",
    "my.greenhouse.io",
    "boards-api.greenhouse.io",
    "www.recaptcha.net",
    "www.google.com",
    "www.gstatic.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
]
RECEIPT_PATTERNS = [r"thank you for applying", r"application has been received", r"application has been submitted", r"application was submitted"]
DECLINE_WORDS = ("don't wish to answer", "do not wish to answer", "decline to self", "prefer not to", "rather not say", "decline to answer")
STANDARD_KEYS = {
    "first_name": "legal_first_name",
    "last_name": "legal_last_name",
    "preferred_name": "preferred_name",
    "email": "email",
    "phone": "phone",
    "country": "country",
}
LINK_RULES: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(r"linkedin", re.I), ("linkedin_url",)),
    (re.compile(r"github", re.I), ("github_url",)),
    (re.compile(r"website|portfolio|personal site", re.I), ("website_url", "portfolio_url", "github_url")),
]
YEARS_RULE = re.compile(r"years? of (full[- ]time |professional |software |relevant |work )?experience", re.I)
FULL_TIME_RULE = re.compile(r"full[- ]time|excluding intern", re.I)
_RANGE = re.compile(r"(\d+)\s*(?:-|–|to)\s*(\d+)")
_LESS = re.compile(r"(?:less than|under|fewer than|<)\s*(\d+)")
_MORE = re.compile(r"(\d+)\s*\+|(?:more than|over|at least|>)\s*(\d+)")


def parse_board_url(url: str | None, hosts: set[str] | None = None) -> tuple[str, str] | None:
    """(org, job id) for any Greenhouse hosted / embedded job URL, else None."""
    if not url or "://" not in url:
        return None
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host not in (hosts or BOARD_HOSTS) and host.split(":")[0] not in (hosts or BOARD_HOSTS):
        return None
    qs = parse_qs(parts.query)
    m = re.match(r"^/([^/]+)/jobs/(\d+)", parts.path)
    if m:
        return m.group(1), m.group(2)
    if parts.path.startswith("/embed/job_app") and qs.get("for") and qs.get("token"):
        return qs["for"][0], qs["token"][0]
    if qs.get("gh_jid"):
        org = parts.path.strip("/").split("/")[0] if parts.path.strip("/") else None
        if org:
            return org, qs["gh_jid"][0]
    return None


def _css_id(value: str) -> str:
    return f'[id="{value}"]' if re.search(r"[^A-Za-z0-9_-]|^\d", value) else f"#{value}"


def questions_to_fields(schema: dict[str, Any]) -> list[FormField]:
    """Flatten the API schema into the generic FormField list used by the review package (meta keeps the API detail)."""
    from ..applications import FormField

    fields: list[FormField] = []
    file_names: set[str] = set()

    def add(name: str, label: str, gh_type: str, values: list[dict], required: bool, section: str, extra: dict | None = None) -> None:
        if gh_type == "input_hidden":
            return
        kind = {"input_text": "text", "textarea": "textarea", "input_file": "file", "multi_value_single_select": "select", "multi_value_multi_select": "multiselect"}.get(
            gh_type, "text"
        )
        options = [str(v.get("label", "")) for v in values]
        meta = {
            "ats": "greenhouse",
            "name": name,
            "gh_type": gh_type,
            "section": section,
            "values": {str(v.get("label", "")): str(v.get("value", v.get("id", ""))) for v in values},
        }
        if extra:
            meta.update(extra)
        fields.append(FormField(id=name, label=label, type=kind, options=options, selector=_css_id(name), required=bool(required), meta=meta))

    for q in schema.get("questions") or []:
        for f in q.get("fields") or []:
            name = str(f.get("name", ""))
            if f.get("type") == "input_file":
                file_names.add(name)
            if name.endswith("_text") and name[:-5] in file_names:
                continue  # "paste your resume" twin of a file upload; we upload the file
            add(name, q.get("label", name), f.get("type", "input_text"), f.get("values") or [], q.get("required", False), "questions")
    for q in schema.get("location_questions") or []:
        for f in q.get("fields") or []:
            if f.get("type") == "input_hidden":
                continue
            add(str(f.get("name", "location")), "Location (City)", f.get("type", "input_text"), [], q.get("required", False), "location")
    if schema.get("location_questions"):
        # the React board also asks for the country in a suggestion box that the API does not list
        add("country", "Country", "input_text", [], False, "location", {"synthetic": True})
    for c in schema.get("compliance") or []:
        for q in c.get("questions") or []:
            for f in q.get("fields") or []:
                add(str(f.get("name", "")), q.get("label", ""), f.get("type", "input_text"), f.get("values") or [], q.get("required", False), "compliance")
    demo = schema.get("demographic_questions") or {}
    for q in demo.get("questions") or []:
        values = [{"label": o.get("label", ""), "value": o.get("id", ""), "free_form": o.get("free_form", False)} for o in q.get("answer_options") or []]
        name = str(q.get("id", ""))
        kind = q.get("type", "multi_value_single_select")
        add(name, q.get("label", ""), kind, values, q.get("required", False), "demographic", {"demographic_id": name})
    return fields


def _pick_bucket(options: list[str], years: int) -> str | None:
    """Match completed years to a range option such as 'Less than 2 years', '2-4 years', '4+ years'."""
    for o in options:
        low = o.lower()
        m = _RANGE.search(low)
        if m and int(m.group(1)) <= years <= int(m.group(2)):
            return o
    for o in options:
        low = o.lower()
        m = _LESS.search(low)
        if m and years < int(m.group(1)):
            return o
        m = _MORE.search(low)
        if m and years >= int(m.group(1) or m.group(2)):
            return o
    return None


def _decline_option(options: list[str]) -> str | None:
    return next((o for o in options if any(w in o.lower() for w in DECLINE_WORDS)), None)


class GreenhouseAdapter:
    name = "greenhouse"
    site_label = "Greenhouse"
    resource_hosts = RESOURCE_HOSTS

    def __init__(self, api_base: str = API_BASE, client: httpx.Client | None = None, timeout: float = 20.0, board_hosts: set[str] | None = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.board_hosts = {h.lower() for h in (board_hosts or BOARD_HOSTS)}  # tests point this at the fixture board
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "AmoreHireMePls/0.1 (application form reader)"})

    def parse(self, url: str | None) -> tuple[str, str] | None:
        return parse_board_url(url, self.board_hosts)

    # -- routing ----------------------------------------------------------------------------------------
    def matches(self, host: str | None, url: str | None) -> bool:
        host = (host or "").lower()
        return self.parse(url) is not None or host in self.board_hosts or host.split(":")[0] in self.board_hosts

    # -- preparation (no side effects) --------------------------------------------------------------------
    def fetch_questions(self, job: dict) -> QuestionSet:
        parsed = self.parse(job.get("apply_url")) or self.parse(job.get("posting_url"))
        if parsed is None:
            raise ValueError("not a Greenhouse job URL")
        org, job_id = parsed
        r = self.client.get(f"{self.api_base}/{org}/jobs/{job_id}", params={"questions": "true"})
        r.raise_for_status()
        schema = r.json()
        fields = questions_to_fields(schema)
        return QuestionSet(fields=fields, source="greenhouse_job_board_api", external_job_id=str(schema.get("id") or job_id), raw={"org": org, "job_id": job_id})

    def map_answers(self, fields: list[FormField], profile: Profile, job: dict, resume_path: str | None, band: BandEvidence | None, fit: str) -> list[Answer]:
        """Deterministic mapping by Greenhouse field name first, then the generic label rules. Nothing is inferred."""
        from ..applications import SENSITIVE, Answer, map_answers
        from ..profile import compute_experience

        out: list[Answer] = []
        generic: list[FormField] = []
        exp = compute_experience(profile.employment)
        for f in fields:
            meta = f.meta or {}
            name, section, label = meta.get("name", f.id), meta.get("section", "questions"), f.label or f.id
            if section in ("compliance", "demographic") or SENSITIVE.search(label):
                if f.required and f.options and (opt := _decline_option(f.options)):
                    out.append(
                        Answer(f.id, label, opt, [], needs_input=False, reason="personal question: the form requires an answer, so the decline option is chosen", sensitive=True)
                    )
                else:
                    out.append(Answer(f.id, label, None, [], needs_input=f.required, reason="personal question: left blank unless Joss supplies a preference", sensitive=True))
                continue
            if name == "resume":
                out.append(Answer(f.id, label, resume_path, ["artifact"], needs_input=resume_path is None, reason="" if resume_path else "no tailored resume yet"))
                continue
            if name == "cover_letter":
                out.append(Answer(f.id, label, None, [], needs_input=f.required, reason="optional: no cover letter is attached unless you add one"))
                continue
            if name in STANDARD_KEYS:
                fact = profile.fact(STANDARD_KEYS[name])
                value = fact.value if fact else None
                out.append(
                    Answer(f.id, label, value, [fact.id] if fact else [], needs_input=value is None and f.required, reason="" if value else f"fact '{STANDARD_KEYS[name]}' unknown")
                )
                continue
            if name == "location":
                city, prov = profile.fact("city"), profile.fact("province")
                value = ", ".join(v.value for v in (city, prov) if v and v.value) or None
                out.append(Answer(f.id, label, value, [v.id for v in (city, prov) if v], needs_input=value is None and f.required, reason="" if value else "city unknown"))
                continue
            link = next((keys for pat, keys in LINK_RULES if pat.search(label)), None)
            if link:
                fact = next((profile.fact(k) for k in link if profile.fact(k) and profile.fact(k).value), None)
                out.append(
                    Answer(
                        f.id,
                        label,
                        fact.value if fact else None,
                        [fact.id] if fact else [],
                        needs_input=f.required and fact is None,
                        reason="" if fact else "no saved link; optional",
                    )
                )
                continue
            if YEARS_RULE.search(label) and f.options:
                years = exp.full_time_years_completed if FULL_TIME_RULE.search(label) else exp.total_years_completed
                opt = _pick_bucket(f.options, years)
                out.append(Answer(f.id, label, opt, [e.id for e in profile.employment], needs_input=opt is None, reason=f"{years} completed years computed from dates"))
                continue
            generic.append(f)
        for a in map_answers(generic, profile, job, resume_path, band, fit):
            f = next(x for x in generic if x.id == a.field_id)
            if a.value is None and not a.sensitive and not f.required and a.reason.startswith("no confirmed fact"):
                a.needs_input, a.reason = False, "optional question left blank; add an answer if you want one"
            out.append(a)
        by_id = {f.id: f for f in fields}
        for a in out:
            src = by_id.get(a.field_id)
            if src is not None:
                a.required = bool(src.required)
                a.options = list(src.options)
        order = {f.id: i for i, f in enumerate(fields)}
        return sorted(out, key=lambda a: order.get(a.field_id, 0))

    # -- execution plan (run by the browser adapter behind the action store) ------------------------------
    def build_plan(self, job: dict, pkg: dict, answers: list[dict]) -> Plan:
        fields = {f["id"]: f for f in ((job.get("form_plan") or {}).get("fields") or [])}
        steps: list[Step] = []
        for a in answers:
            if a.get("value") in (None, ""):
                continue
            f = fields.get(a["field_id"]) or {"id": a["field_id"], "type": "text", "label": a.get("label", ""), "meta": {}}
            meta = f.get("meta") or {}
            name = meta.get("name", f["id"])
            label = f.get("label") or a.get("label") or ""
            values = meta.get("values") or {}
            sel = f'{_css_id(name)}, [name="{name}"]'
            kind = f.get("type", "text")
            if kind == "file":
                steps.append(Step("upload", f"{_css_id(name)}, input[type=file][name={name}], [data-field={name}] input[type=file]", a["value"], label=label))
            elif name == "location":
                steps.append(Step("autocomplete", "#candidate-location, #auto_complete_input, #job_application_location, [name=location]", a["value"], label="Location"))
            elif name == "country":
                steps.append(Step("autocomplete", "#country, [name=country]", a["value"], label="Country", optional=True))
            elif kind == "select":
                vid = values.get(a["value"])
                steps.append(Step("choose", sel, a["value"], label=label, option_selectors=[_css_id(f"{name}_{vid}")] if vid else []))
            elif kind == "multiselect":
                chosen = [v.strip() for v in str(a["value"]).split("|") if v.strip()] if isinstance(a["value"], str) else list(a["value"])
                base = name[:-2] if name.endswith("[]") else name
                steps.append(Step("choose_multi", sel, label=label, options=chosen, option_selectors=[_css_id(f"{base}[]_{values[c]}") if c in values else "" for c in chosen]))
            else:
                steps.append(Step("fill", sel, str(a["value"]), label=label))
        steps.append(
            Step("click", "#application-form button[type=submit], #submit_app, #application_form input[type=submit], form button[type=submit]", final=True, timeout_ms=30000)
        )
        parsed = self.parse(pkg.get("apply_url")) or self.parse(job.get("posting_url"))
        gh_job_id = parsed[1] if parsed else None
        return Plan(
            start_url=pkg["apply_url"],
            steps=steps,
            receipt_text="Thank you for applying",
            receipt_patterns=RECEIPT_PATTERNS,
            receipt_url_pattern=r"/confirmation",
            # the confirmation page names no requisition, but its URL carries the Greenhouse job id
            receipt_url_must_include=[gh_job_id] if gh_job_id else [],
            upload_filename=next((Path(str(a["value"])).name for a in answers if a.get("value") and (fields.get(a["field_id"]) or {}).get("type") == "file"), None),
            job_id=job.get("_id"),
            frame_selector="#grnhse_iframe" if self.parse(pkg.get("apply_url")) is None else None,
            resource_hosts=list(self.resource_hosts),
            site=self.site_label,
        )
