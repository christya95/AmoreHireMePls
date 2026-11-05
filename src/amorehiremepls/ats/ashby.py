"""Ashby hosted boards (`jobs.ashbyhq.com`).

The public job-board feed lists jobs and does not include the application form. The board itself loads the
form with an unauthenticated GraphQL read, `ApiJobPosting`, at
`https://jobs.ashbyhq.com/api/non-user-graphql`: `applicationForm.sections[].fieldEntries[]` and
`surveyForms` (the diversity questions). This adapter only sends that read. It never submits through
GraphQL. If the form is missing, the apply page HTML is scanned. A failed read is left for the browser scan.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from ..browser import Plan, Step
from .base import QuestionSet
from .lever import _decline_option, _pick_bucket, parse_form_questions
from .rules import YEARS_RULE

if TYPE_CHECKING:
    from ..applications import Answer, FormField
    from ..profile import BandEvidence, Profile

log = logging.getLogger(__name__)

BOARD_HOSTS = {"jobs.ashbyhq.com"}
GRAPHQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
RECEIPT_PATTERNS = [r"application submitted", r"thank you for applying", r"your application has been received"]
QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id
    title
    applicationForm { sections { fieldEntries { isRequired field { path title type selectableValues { label value } } } } }
    surveyForms { surveyType formDefinition { sections { fields { isRequired field { path title type selectableValues { label value } } } } } }
  }
}"""
SYSTEM = {
    "_systemfield_name": "name",
    "name": "name",
    "_systemfield_email": "email",
    "email": "email",
    "_systemfield_phone": "phone",
    "phone": "phone",
    "_systemfield_resume": "resume",
    "resume": "resume",
}
TYPE_KIND = {
    "String": "text",
    "Email": "text",
    "Phone": "text",
    "LongText": "textarea",
    "File": "file",
    "ValueSelect": "select",
    "MultiValueSelect": "multiselect",
    "Boolean": "checkbox",
    "Location": "text",
}


def parse_board_url(url: str | None, hosts: set[str] | None = None) -> tuple[str, str] | None:
    """(board name, posting id) for an Ashby URL, including the fixture's `/ab/` prefix."""
    if not url or "://" not in url:
        return None
    parts = urlsplit(url)
    host = parts.netloc.lower()
    allowed = hosts or BOARD_HOSTS
    if host not in allowed and host.split(":")[0] not in allowed:
        return None
    m = re.match(rf"^/(?:ab/)?([^/]+)/({UUID})(?:/application)?/?$", parts.path)
    if not m:
        return None
    return m.group(1), m.group(2)


def _entries(posting: dict) -> list[dict]:
    rows: list[dict] = []
    form = posting.get("applicationForm") or {}
    for section in form.get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            rows.append({"entry": entry, "section": "application"})
    for survey in posting.get("surveyForms") or []:
        kind = "eeo" if str(survey.get("surveyType") or "").lower() in ("diversity", "eeo", "eeoc") else "survey"
        definition = survey.get("formDefinition") or {}
        for section in definition.get("sections") or []:
            for entry in section.get("fields") or []:
                rows.append({"entry": entry, "section": kind})
    return rows


def questions_to_fields(posting: dict) -> list[FormField]:
    from ..applications import FormField

    fields: list[FormField] = []
    seen: set[str] = set()
    for row in _entries(posting):
        entry, section = row["entry"], row["section"]
        field = entry.get("field") or {}
        path = str(field.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        options = [str(o.get("label") or "") for o in field.get("selectableValues") or [] if o.get("label")]
        kind = TYPE_KIND.get(str(field.get("type") or "String"), "text")
        required = bool(entry.get("isRequired") or field.get("isRequired"))
        fields.append(
            FormField(
                id=path,
                label=str(field.get("title") or path),
                type=kind,
                options=options,
                required=required,
                selector=f'[id="{path}"], [name="{path}"]',
                meta={"ats": "ashby", "name": path, "section": section, "ashby_type": field.get("type"), "values": {o: o for o in options}},
            )
        )
    return fields


class AshbyAdapter:
    name = "ashby"
    site_label = "Ashby"
    resource_hosts: list[str] = []

    def __init__(self, graphql_url: str = GRAPHQL_URL, client: httpx.Client | None = None, timeout: float = 20.0, board_hosts: set[str] | None = None) -> None:
        self.graphql_url = graphql_url
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
            raise ValueError("not an Ashby job URL")
        org, posting_id = parsed
        response = self.client.post(
            self.graphql_url,
            json={"operationName": "ApiJobPosting", "variables": {"organizationHostedJobsPageName": org, "jobPostingId": posting_id}, "query": QUERY},
        )
        response.raise_for_status()
        body = response.json()
        posting = ((body.get("data") or {}).get("jobPosting")) or {}
        if not posting:
            raise ValueError("Ashby returned no posting")
        source = "ashby_graphql"
        fields = questions_to_fields(posting)
        if not fields:
            html = self._form_html(job)
            extra = parse_form_questions(html) if html else []
            if extra:
                from .lever import questions_to_fields as lever_fields

                fields = [f for f in lever_fields(extra) if (f.meta or {}).get("section") != "standard" or f.id in ("name", "email", "phone", "resume")]
                for f in fields:
                    f.meta = {**(f.meta or {}), "ats": "ashby"}
                source = "ashby_form_html"
            else:
                raise ValueError("Ashby form was not in the posting or the page")
        return QuestionSet(fields=fields, source=source, external_job_id=str(posting.get("id") or posting_id), raw={"org": org, "posting_id": posting_id})

    def _form_html(self, job: dict) -> str:
        for url in (job.get("apply_url"), job.get("posting_url")):
            if not url:
                continue
            try:
                page = self.client.get(str(url))
            except Exception as exc:
                log.info("Ashby form page %s could not be read (%s)", url, type(exc).__name__)
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
            name, section, label = SYSTEM.get(meta.get("name", f.id), meta.get("name", f.id)), meta.get("section", "application"), f.label or f.id
            if section == "eeo" or SENSITIVE.search(label):
                if own := own_personal_answer(f, profile, _decline_option(f.options)):
                    out.append(own)
                elif f.required and f.options and (opt := _decline_option(f.options)):
                    out.append(Answer(f.id, label, opt, [], False, "personal question: the form requires an answer, so the decline option is chosen", sensitive=True))
                else:
                    out.append(Answer(f.id, label, None, [], f.required, "personal question: left blank unless you supply a preference", sensitive=True))
                continue
            if name == "resume" or f.type == "file":
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
            if name == "location" or str(meta.get("ashby_type")) == "Location":
                city, prov = profile.fact("city"), profile.fact("province")
                value = ", ".join(p.value for p in (city, prov) if p and p.value) or None
                out.append(Answer(f.id, label, value, [p.id for p in (city, prov) if p], value is None and f.required, "" if value else "city unknown"))
                continue
            if YEARS_RULE.search(label) and f.options:
                out.append(years_bucket_answer(f, profile, exp.total_years_completed, _pick_bucket))
                continue
            generic.append(f)
        for a in map_answers(generic, profile, job, resume_path, band, fit, salary_case=salary_case):
            field = next(x for x in generic if x.id == a.field_id)
            if a.value is None and not a.sensitive and not field.required and a.reason.startswith("no confirmed fact"):
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
        stored = {f["id"]: f for f in ((job.get("form_plan") or {}).get("fields") or [])}
        steps: list[Step] = []
        for a in answers:
            if a.get("value") in (None, ""):
                continue
            f = stored.get(a["field_id"]) or {"id": a["field_id"], "type": "text", "label": a.get("label", ""), "meta": {}}
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
            upload_filename=next((Path(str(a["value"])).name for a in answers if a.get("value") and (stored.get(a["field_id"]) or {}).get("type") == "file"), None),
            job_id=job.get("_id"),
            resource_hosts=list(self.resource_hosts),
            site=self.site_label,
        )
