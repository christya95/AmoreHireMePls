"""Lever hosted postings (`jobs.lever.co` and `jobs.eu.lever.co`).

The public postings API is `GET https://api.lever.co/v0/postings/{org}/{id}`. That document describes the
job. It does not normally include the application form. When a response does carry `customQuestions` or
`questions` (the fixture does; a future authenticated payload may), those are the question list. Otherwise
the apply page HTML is read and its controls are used. A failed API call is left for the caller, which
scans the live page in the browser. Nothing here submits an application.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ..browser import Plan, Step
from .base import QuestionSet
from .rules import YEARS_RULE

if TYPE_CHECKING:
    from ..applications import Answer, FormField
    from ..profile import BandEvidence, Profile

log = logging.getLogger(__name__)

BOARD_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}
API_BASE = "https://api.lever.co/v0"
RECEIPT_PATTERNS = [r"application submitted", r"thanks for applying", r"thank you for applying", r"application has been received"]
DECLINE_WORDS = ("don't wish to answer", "do not wish to answer", "decline to self", "prefer not to", "rather not say", "decline to answer")
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_RANGE = re.compile(r"(\d+)\s*(?:-|–|to)\s*(\d+)")
_LESS = re.compile(r"(?:less than|under|fewer than|<)\s*(\d+)")
_MORE = re.compile(r"(\d+)\s*\+|(?:more than|over|at least|>)\s*(\d+)")
SENSITIVE_HINT = re.compile(r"(gender|race|ethnic|veteran|disab|sexual orientation|pronoun)", re.I)

# Controls Lever puts on every application. Custom questions are added beside them.
STANDARD: list[tuple[str, str, str, bool]] = [
    ("name", "Full name", "text", True),
    ("email", "Email", "text", True),
    ("phone", "Phone", "text", True),
    ("org", "Current company", "text", False),
    ("urls", "LinkedIn URL", "text", False),
    ("resume", "Resume", "file", True),
    ("comments", "Additional information", "textarea", False),
]


def parse_board_url(url: str | None, hosts: set[str] | None = None) -> tuple[str, str] | None:
    """(org, posting id) for a Lever hosted URL, including the fixture's `/lv/` prefix."""
    if not url or "://" not in url:
        return None
    parts = urlsplit(url)
    host = parts.netloc.lower()
    allowed = hosts or BOARD_HOSTS
    if host not in allowed and host.split(":")[0] not in allowed:
        return None
    m = re.match(rf"^/(?:lv/)?([^/]+)/({UUID})(?:/apply)?/?$", parts.path)
    if not m:
        return None
    return m.group(1), m.group(2)


def _pick_bucket(options: list[str], years: int) -> str | None:
    for o in options:
        m = _RANGE.search(o.lower())
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


class _FormParser(HTMLParser):
    """Pull labelled controls out of an apply page. Labels that precede a control name it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.questions: list[dict] = []
        self._label = ""
        self._in_label = False
        self._label_buf: list[str] = []
        self._select: dict | None = None
        self._options: list[str] = []
        self._in_option = False
        self._option_buf: list[str] = []
        self._checks: dict[str, dict] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "label":
            self._in_label = True
            self._label_buf = []
        elif tag == "option" and self._select is not None:
            self._in_option = True
            self._option_buf = []
        elif tag == "select":
            self._select = a
            self._options = []
        elif tag in ("input", "textarea"):
            self._control(tag, a)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._in_label:
            self._label = re.sub(r"\s+", " ", "".join(self._label_buf)).strip(" :*")
            self._in_label = False
        elif tag == "option" and self._in_option:
            text = re.sub(r"\s+", " ", "".join(self._option_buf)).strip()
            if text and text.lower() not in ("select", "please select", "choose"):
                self._options.append(text)
            self._in_option = False
        elif tag == "select" and self._select is not None:
            self._control("select", self._select, self._options)
            self._select = None

    def handle_data(self, data: str) -> None:
        if self._in_option:
            self._option_buf.append(data)
        elif self._in_label:
            self._label_buf.append(data)

    def _control(self, tag: str, attrs: dict, options: list[str] | None = None) -> None:
        kind = (attrs.get("type") or "").lower()
        if kind in ("hidden", "submit", "button", "reset"):
            return
        name = attrs.get("name") or attrs.get("id")
        if not name:
            return
        label = self._label or attrs.get("aria-label") or name
        required = "required" in attrs
        if kind == "checkbox":
            bucket = self._checks.setdefault(name, {"id": name, "text": label, "type": "multiple-select", "required": required, "options": []})
            value = attrs.get("value") or label
            bucket["options"].append({"text": value})
            bucket["required"] = bucket["required"] or required
            return
        qtype = {"textarea": "textarea", "select": "dropdown", "file": "file"}.get(tag if tag != "input" else kind, "text")
        if tag == "select":
            qtype = "dropdown"
        opts = [{"text": o} for o in (options or [])]
        self.questions.append({"id": name, "text": label, "type": qtype, "required": required, "options": opts})


def parse_form_questions(html: str) -> list[dict]:
    parser = _FormParser()
    parser.feed(html)
    parser.questions.extend(parser._checks.values())
    return parser.questions


def questions_to_fields(questions: list[dict]) -> list[FormField]:
    from ..applications import FormField

    fields: list[FormField] = []
    seen: set[str] = set()

    def add(qid: str, label: str, qtype: str, options: list[str], required: bool, section: str) -> None:
        if not qid or qid in seen:
            return
        seen.add(qid)
        kind = {"textarea": "textarea", "dropdown": "select", "multiple-select": "multiselect", "multiple-choice": "select", "file": "file"}.get(qtype, "text")
        fields.append(
            FormField(
                id=qid,
                label=label,
                type=kind,
                options=options,
                required=required,
                selector=f'[id="{qid}"], [name="{qid}"]',
                meta={"ats": "lever", "name": qid, "section": section, "values": {o: o for o in options}},
            )
        )

    for name, label, qtype, required in STANDARD:
        add(name, label, qtype, [], required, "standard")
    for q in questions:
        options = [str(o.get("text") or o.get("label") or "") for o in q.get("options") or []]
        label = str(q.get("text") or q.get("label") or q.get("id") or "")
        section = str(q.get("section") or "")
        if not section and SENSITIVE_HINT.search(label):
            section = "eeo"
        add(str(q.get("id") or ""), label, str(q.get("type") or "text"), options, bool(q.get("required")), section or "custom")
    return fields


class LeverAdapter:
    name = "lever"
    site_label = "Lever"
    resource_hosts: list[str] = []

    def __init__(self, api_base: str = API_BASE, client: httpx.Client | None = None, timeout: float = 20.0, board_hosts: set[str] | None = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.board_hosts = {h.lower() for h in (board_hosts or BOARD_HOSTS)}
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "AmoreHireMePls/0.1 (application form reader)"})

    def parse(self, url: str | None) -> tuple[str, str] | None:
        return parse_board_url(url, self.board_hosts)

    def matches(self, host: str | None, url: str | None) -> bool:
        host = (host or "").lower()
        return self.parse(url) is not None or host in self.board_hosts or host.split(":")[0] in self.board_hosts

    def fetch_questions(self, job: dict) -> QuestionSet:
        parsed = self.parse(job.get("apply_url")) or self.parse(job.get("posting_url"))
        if parsed is None:
            raise ValueError("not a Lever job URL")
        org, posting_id = parsed
        response = self.client.get(f"{self.api_base}/postings/{org}/{posting_id}")
        response.raise_for_status()
        schema = response.json()
        questions = list(schema.get("customQuestions") or schema.get("questions") or [])
        source = "lever_postings_api"
        if not questions:
            html = self._form_html(job, schema)
            if html:
                questions = parse_form_questions(html)
                source = "lever_form_html"
        fields = questions_to_fields(questions)
        return QuestionSet(fields=fields, source=source, external_job_id=str(schema.get("id") or posting_id), raw={"org": org, "posting_id": posting_id})

    def _form_html(self, job: dict, schema: dict) -> str:
        for url in (schema.get("applyUrl"), schema.get("hostedUrl"), job.get("apply_url"), job.get("posting_url")):
            if not url:
                continue
            try:
                page = self.client.get(str(url))
            except Exception as exc:
                log.info("Lever form page %s could not be read (%s)", url, type(exc).__name__)
                continue
            if page.status_code == 200 and "<form" in page.text.lower():
                return page.text
        return ""

    def map_answers(self, fields: list[FormField], profile: Profile, job: dict, resume_path: str | None, band: BandEvidence | None, fit: str, salary_case=None) -> list[Answer]:
        from ..applications import SENSITIVE, Answer, map_answers, own_personal_answer, years_bucket_answer
        from ..profile import compute_experience

        out: list[Answer] = []
        generic: list[FormField] = []
        exp = compute_experience(profile.employment)
        for f in fields:
            meta = f.meta or {}
            name, section, label = meta.get("name", f.id), meta.get("section", "custom"), f.label or f.id
            personal = section == "eeo" or bool(SENSITIVE.search(label))
            if personal:
                if own := own_personal_answer(f, profile, _decline_option(f.options)):
                    out.append(own)
                elif f.required and f.options and (opt := _decline_option(f.options)):
                    out.append(Answer(f.id, label, opt, [], False, "personal question: the form requires an answer, so the decline option is chosen", sensitive=True))
                else:
                    out.append(Answer(f.id, label, None, [], f.required, "personal question: left blank unless you supply a preference", sensitive=True))
                continue
            if name == "resume":
                out.append(Answer(f.id, label, resume_path, ["artifact"], resume_path is None, "" if resume_path else "no tailored resume yet"))
                continue
            if name == "name":
                first, last = profile.fact("legal_first_name"), profile.fact("legal_last_name")
                value = " ".join(p.value for p in (first, last) if p and p.value) or None
                out.append(Answer(f.id, label, value, [p.id for p in (first, last) if p], value is None and f.required, "" if value else "name unknown"))
                continue
            if name == "email":
                fact = profile.fact("email")
                out.append(Answer(f.id, label, fact.value if fact else None, [fact.id] if fact else [], fact is None and f.required, "" if fact else "email unknown"))
                continue
            if name == "phone":
                fact = profile.fact("phone")
                out.append(Answer(f.id, label, fact.value if fact else None, [fact.id] if fact else [], fact is None and f.required, "" if fact else "phone unknown"))
                continue
            if name == "urls":
                fact = profile.fact("linkedin_url")
                out.append(Answer(f.id, label, fact.value if fact else None, [fact.id] if fact else [], False, "" if fact else "no saved link; optional"))
                continue
            if name == "org":
                current = next((e for e in profile.employment if not e.end), None)
                out.append(Answer(f.id, label, current.employer if current else None, [current.id] if current else [], False, "" if current else "no current employer; optional"))
                continue
            if name == "comments":
                out.append(Answer(f.id, label, None, [], False, "optional note left blank"))
                continue
            if YEARS_RULE.search(label) and f.options:
                out.append(years_bucket_answer(f, profile, exp.total_years_completed, _pick_bucket))
                continue
            generic.append(f)
        for a in map_answers(generic, profile, job, resume_path, band, fit, salary_case=salary_case):
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

    def build_plan(self, job: dict, pkg: dict, answers: list[dict]) -> Plan:
        fields = {f["id"]: f for f in ((job.get("form_plan") or {}).get("fields") or [])}
        steps: list[Step] = []
        for a in answers:
            if a.get("value") in (None, ""):
                continue
            f = fields.get(a["field_id"]) or {"id": a["field_id"], "type": "text", "label": a.get("label", ""), "meta": {}}
            name = (f.get("meta") or {}).get("name", f["id"])
            label = f.get("label") or a.get("label") or ""
            kind = f.get("type", "text")
            sel = f'[id="{name}"], [name="{name}"]'
            if kind == "file":
                steps.append(Step("upload", f'{sel}, input[type=file][name="{name}"]', a["value"], label=label))
            elif kind == "select":
                steps.append(Step("choose", sel, a["value"], label=label, sensitive=bool(a.get("sensitive"))))
            elif kind == "multiselect":
                chosen = [v.strip() for v in str(a["value"]).split("|") if v.strip()]
                steps.append(Step("choose_multi", sel, label=label, options=chosen, sensitive=bool(a.get("sensitive"))))
            else:
                steps.append(Step("fill", sel, str(a["value"]), label=label, sensitive=bool(a.get("sensitive"))))
        steps.append(Step("click", "form#application-form button[type=submit], form button[type=submit]", final=True, timeout_ms=30000))
        parsed = self.parse(pkg.get("apply_url")) or self.parse(job.get("posting_url"))
        posting_id = parsed[1] if parsed else None
        return Plan(
            start_url=pkg["apply_url"],
            steps=steps,
            receipt_text="Application submitted",
            receipt_patterns=RECEIPT_PATTERNS,
            receipt_url_pattern=r"/thanks",
            receipt_url_must_include=[posting_id] if posting_id else [],
            upload_filename=next((Path(str(a["value"])).name for a in answers if a.get("value") and (fields.get(a["field_id"]) or {}).get("type") == "file"), None),
            job_id=job.get("_id"),
            resource_hosts=list(self.resource_hosts),
            site=self.site_label,
        )
