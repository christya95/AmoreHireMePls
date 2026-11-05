"""Application form adapters: one per applicant tracking system.

An adapter knows (1) which apply URLs it owns, (2) how to read the question list without a browser (the
vendor's public job board API, falling back to a DOM scan done by the caller), (3) how to map questions to
answers from the private profile, and (4) how to turn the approved answers into a deterministic browser `Plan`
whose receipt check ties the confirmation to the right requisition.

Preparation (`fetch_questions`, `map_answers`) has no side effects. Execution only happens through
`ApplicationService.execute` -> `ActionStore` with the `application_submit` scope; adapters never open a browser.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..browser import Plan

if TYPE_CHECKING:
    from ..applications import Answer, FormField
    from ..profile import BandEvidence, Profile


@dataclass
class QuestionSet:
    """Questions read from the vendor API, plus what the receipt must reference."""

    fields: list[FormField]
    source: str  # e.g. "greenhouse_job_board_api"
    external_job_id: str | None = None
    raw: dict = field(default_factory=dict)


class AtsAdapter(Protocol):
    name: str  # also used as `apply_route`
    site_label: str  # plain words for the dashboard ("Greenhouse")
    resource_hosts: list[str]  # extra hosts the page needs for scripts/styles/frames (never navigation)

    def matches(self, host: str | None, url: str | None) -> bool: ...

    def fetch_questions(self, job: dict) -> QuestionSet: ...

    def map_answers(self, fields: list[FormField], profile: Profile, job: dict, resume_path: str | None, band: BandEvidence | None, fit: str, salary_case=None) -> list[Answer]: ...

    def build_plan(self, job: dict, pkg: dict, answers: list[dict]) -> Plan: ...


class Registry:
    def __init__(self) -> None:
        self._adapters: list[AtsAdapter] = []

    def register(self, adapter: AtsAdapter) -> AtsAdapter:
        self._adapters.append(adapter)
        return adapter

    def all(self) -> list[AtsAdapter]:
        return list(self._adapters)

    def for_url(self, url: str | None, route: str | None = None) -> AtsAdapter | None:
        host = url.split("/")[2].lower() if url and "://" in url else None  # keeps the port so a test board can be told apart
        for a in self._adapters:  # the adapter that owns the URL wins (a test board may register a second instance)
            if a.matches(host, url):
                return a
        for a in self._adapters:
            if route and route == a.name:
                return a
        return None

    def for_fields(self, fields: list) -> AtsAdapter | None:
        """The adapter whose API produced these questions (fields carry `meta.ats`)."""
        names = {getattr(f, "meta", {}).get("ats") if not isinstance(f, dict) else (f.get("meta") or {}).get("ats") for f in fields}
        names.discard(None)
        if len(names) != 1:
            return None
        name = names.pop()
        return next((a for a in self._adapters if a.name == name), None)


registry = Registry()

PlanBuilder = Callable[[dict, dict, list[dict]], Plan]
