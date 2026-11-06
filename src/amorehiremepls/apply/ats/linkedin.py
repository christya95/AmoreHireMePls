"""LinkedIn job pages the owner is already signed in to.

LinkedIn does not publish an Easy Apply question list. This adapter never calls LinkedIn. A test board can
serve the questions; a live job uses the contact step that form always starts with (email, phone, resume).
Further questions are filled only when they were in that list and a saved answer exists. Anything else, a
signed-out page, or a verify-it's-you page stops before the application is sent.

The plan uses the shared browser steps: a button by its accessible name, fills and choices by label, then
Next, and a final Submit only when that button is already on the page. There is no message or connection step.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx

from amorehiremepls.apply.ats.base import QuestionSet
from amorehiremepls.apply.ats.lever import _decline_option
from amorehiremepls.apply.browser import Plan, Step

if TYPE_CHECKING:
    from amorehiremepls.apply.applications import Answer, FormField
    from amorehiremepls.candidate.profile import BandEvidence, Profile

# Scripts and images the job page loads. Never navigation targets.
RESOURCE_HOSTS = ["*.licdn.com", "licdn.com"]
RECEIPT_PATTERNS = [r"your application was sent", r"application was sent", r"application submitted"]
EASY_APPLY = 'button[aria-label="Easy Apply" i]:visible, button[aria-label*="Easy Apply" i]:visible'
NEXT_BUTTON = 'button[aria-label="Continue to next step" i]:visible'
SUBMIT_BUTTON = 'button[aria-label="Submit application" i]:visible'
_JOB_ID = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})")


def linkedin_host(host: str | None) -> bool:
    bare = (host or "").split(":")[0].lower()
    return bare == "linkedin.com" or bare.endswith(".linkedin.com")


def parse_job_url(url: str | None, hosts: set[str] | None = None) -> str | None:
    """The numeric job id for a LinkedIn job URL, or for a test board registered with `hosts`."""
    if not url or "://" not in url:
        return None
    parts = urlsplit(url)
    host = parts.netloc.lower()
    allowed = linkedin_host(host) or bool(hosts and (host in hosts or host.split(":")[0] in hosts))
    if not allowed:
        return None
    match = _JOB_ID.search(parts.path)
    if match:
        return match.group(1)
    current = (parse_qs(parts.query).get("currentJobId") or [""])[0]
    if re.fullmatch(r"\d{6,}", current):
        return current
    return None


def _standard_questions() -> list[dict[str, Any]]:
    """The contact step. No extra questions are invented for a live posting."""
    return [
        {"id": "email", "label": "Email address", "type": "text", "required": True, "step": 1},
        {"id": "phone", "label": "Mobile phone number", "type": "text", "required": True, "step": 1},
        {"id": "resume", "label": "Resume", "type": "file", "required": True, "step": 1},
    ]


def questions_to_fields(questions: list[dict[str, Any]]) -> list[FormField]:
    from amorehiremepls.apply.applications import FormField

    fields: list[FormField] = []
    for question in questions:
        name = str(question.get("id") or "")
        if not name:
            continue
        label = str(question.get("label") or name)
        kind = str(question.get("type") or "text")
        options = [str(option) for option in question.get("options") or []]
        if kind not in ("text", "textarea", "select", "multiselect", "file"):
            kind = "select" if options else "text"
        fields.append(
            FormField(
                id=name,
                label=label,
                type=kind,
                options=options,
                required=bool(question.get("required")),
                selector=f'[id="{name}"], [name="{name}"]',
                meta={"ats": "linkedin", "name": name, "section": str(question.get("section") or "questions"), "step": int(question.get("step") or 1)},
            )
        )
    return fields


class LinkedInAdapter:
    name = "linkedin"
    site_label = "LinkedIn"
    resource_hosts = RESOURCE_HOSTS

    def __init__(self, api_base: str | None = None, client: httpx.Client | None = None, timeout: float = 20.0, board_hosts: set[str] | None = None) -> None:
        self.api_base = api_base.rstrip("/") if api_base else None
        self.board_hosts = {h.lower() for h in (board_hosts or set())}
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "AmoreHireMePls/0.1 (application form reader)"})

    def parse(self, url: str | None) -> str | None:
        return parse_job_url(url, self.board_hosts or None)

    def matches(self, host: str | None, url: str | None) -> bool:
        return self.parse(url) is not None

    def fetch_questions(self, job: dict) -> QuestionSet:
        url = job.get("apply_url") or job.get("posting_url")
        job_id = self.parse(url)
        if job_id is None:
            raise ValueError("not a LinkedIn job URL")
        if self.api_base is None:
            return QuestionSet(fields=questions_to_fields(_standard_questions()), source="linkedin_easy_apply_standard", external_job_id=job_id)
        variant = (parse_qs(urlsplit(url).query).get("variant") or [""])[0]
        response = self.client.get(f"{self.api_base}/questions/{job_id}", params={"variant": variant} if variant else None)
        response.raise_for_status()
        payload = response.json()
        fields = questions_to_fields(list(payload.get("questions") or []))
        return QuestionSet(fields=fields, source="linkedin_fixture_questions", external_job_id=str(payload.get("id") or job_id), raw={"job_id": job_id})

    def map_answers(self, fields: list[FormField], profile: Profile, job: dict, resume_path: str | None, band: BandEvidence | None, fit: str, salary_case=None) -> list[Answer]:
        from amorehiremepls.apply.applications import SENSITIVE, Answer, map_answers, own_personal_answer

        out: list[Answer] = []
        generic: list[FormField] = []
        for field in fields:
            section = (field.meta or {}).get("section", "questions")
            label = field.label or field.id
            if section in ("compliance", "demographic") or SENSITIVE.search(label):
                if own := own_personal_answer(field, profile, _decline_option(field.options)):
                    out.append(own)
                elif field.required and field.options and (opt := _decline_option(field.options)):
                    out.append(Answer(field.id, label, opt, [], False, "personal question: the form requires an answer, so the decline option is chosen", sensitive=True))
                else:
                    out.append(Answer(field.id, label, None, [], field.required, "personal question: left blank unless you supply a preference", sensitive=True))
                continue
            generic.append(field)
        for answer in map_answers(generic, profile, job, resume_path, band, fit, salary_case=salary_case):
            field = next(item for item in generic if item.id == answer.field_id)
            if answer.value is None and not answer.sensitive and not field.required and answer.reason.startswith(("no confirmed fact", "fact ")):
                answer.needs_input, answer.reason = False, "optional question left blank; add an answer if you want one"
            if answer.value is None and field.required and not answer.sensitive:
                answer.needs_input = True
            out.append(answer)
        by_id = {field.id: field for field in fields}
        for answer in out:
            source = by_id.get(answer.field_id)
            if source is not None:
                answer.required = bool(source.required)
                answer.options = list(source.options)
        order = {field.id: index for index, field in enumerate(fields)}
        return sorted(out, key=lambda answer: order.get(answer.field_id, 0))

    def build_plan(self, job: dict, pkg: dict, answers: list[dict]) -> Plan:
        fields = {field["id"]: field for field in ((job.get("form_plan") or {}).get("fields") or [])}
        by_step: dict[int, list[Step]] = {}
        for answer in answers:
            if answer.get("value") in (None, ""):
                continue
            field = fields.get(answer["field_id"]) or {"id": answer["field_id"], "type": "text", "label": answer.get("label", ""), "meta": {}}
            meta = field.get("meta") or {}
            name = str(meta.get("name") or field["id"])
            label = field.get("label") or answer.get("label") or ""
            kind = field.get("type") or "text"
            selector = f'[id="{name}"], [name="{name}"]'
            step_no = int(meta.get("step") or 1)
            if kind == "file":
                step = Step("upload", f'input[type=file]{selector}, input[type=file][name="{name}"]', answer["value"], label=label)
            elif kind == "select":
                step = Step("choose", selector, str(answer["value"]), label=label, sensitive=bool(answer.get("sensitive")))
            elif kind == "multiselect":
                chosen = [part.strip() for part in str(answer["value"]).split("|") if part.strip()]
                step = Step("choose_multi", selector, label=label, options=chosen, sensitive=bool(answer.get("sensitive")))
            else:
                step = Step("fill", selector, str(answer["value"]), label=label, sensitive=bool(answer.get("sensitive")))
            by_step.setdefault(step_no, []).append(step)
        steps: list[Step] = [Step("click", EASY_APPLY, label="Easy Apply", missing_is_handoff=True)]
        for step_no in sorted(by_step):
            steps.extend(by_step[step_no])
            steps.append(Step("click", NEXT_BUTTON, label="Next"))
        if not by_step:
            steps.append(Step("click", NEXT_BUTTON, label="Next"))
        steps.append(Step("click", SUBMIT_BUTTON, label="Submit application", final=True, timeout_ms=30000))
        job_id = self.parse(pkg.get("apply_url")) or self.parse(job.get("posting_url")) or job.get("linkedin_job_id")
        upload_name = next(
            (Path(str(answer["value"])).name for answer in answers if answer.get("value") and (fields.get(answer["field_id"]) or {}).get("type") == "file"),
            None,
        )
        return Plan(
            start_url=pkg["apply_url"],
            steps=steps,
            receipt_text="Your application was sent",
            receipt_patterns=RECEIPT_PATTERNS,
            receipt_url_pattern=r"/confirmation",
            receipt_url_must_include=[str(job_id)] if job_id else [],
            upload_filename=upload_name,
            job_id=job.get("_id"),
            resource_hosts=list(self.resource_hosts),
            site=self.site_label,
            handoff_if_submit_missing=True,
        )
