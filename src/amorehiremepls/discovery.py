"""Job discovery and qualification.

Sources: Greenhouse / Lever / Ashby public job-board JSON APIs (read-only), job URL/data import, and
pasted posting text. Every posting gets a dated snapshot with hashes. Deterministic filters and
duplicate checks run before any model call; the model only ranks what survives.

LinkedIn: no authorised API for this account, so LinkedIn jobs enter via URL/data import only.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel

from .ai import AIRunner, ProviderUnavailable
from .db import Store
from .jobs import JobStore
from .profile import Profile, compute_experience
from .skills import ExtractInput, FitInput
from .util import extract_linkedin_job_id, normalize_text, sha256_bytes, utcnow

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_to_text(content: str | None) -> str:
    if not content:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", html.unescape(str(content))))
    return _WS_RE.sub(" ", text).strip()


@dataclass
class RawJob:
    source: str
    employer: str
    title: str
    url: str
    requisition_id: str | None = None
    location: str | None = None
    posted_at: str | None = None
    content_text: str = ""
    employment_type: str | None = None
    remote_policy: str | None = None
    salary: dict | None = None
    extra: dict = field(default_factory=dict)


class SourceError(Exception):
    pass


class HttpSource:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 20.0) -> None:
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "AmoreHireMePls/0.1 (job discovery)"})

    def _get_json(self, url: str) -> Any:
        try:
            r = self.client.get(url)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError(f"{type(exc).__name__}: {url}") from exc


class GreenhouseSource(HttpSource):
    name = "greenhouse"

    def fetch(self, board_token: str, employer: str | None = None) -> list[RawJob]:
        data = self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true")
        jobs = []
        for j in data.get("jobs", []):
            jobs.append(RawJob(self.name, employer or data.get("meta", {}).get("company", board_token), j.get("title", ""), j.get("absolute_url", ""),
                               requisition_id=str(j.get("id") or j.get("requisition_id") or ""), location=(j.get("location") or {}).get("name"),
                               posted_at=j.get("updated_at"), content_text=html_to_text(j.get("content")),
                               extra={"departments": [d.get("name") for d in j.get("departments", [])]}))
        return jobs


class LeverSource(HttpSource):
    name = "lever"

    def fetch(self, site: str, employer: str | None = None) -> list[RawJob]:
        data = self._get_json(f"https://api.lever.co/v0/postings/{site}?mode=json")
        jobs = []
        for p in data if isinstance(data, list) else []:
            cats = p.get("categories", {})
            text = " ".join(html_to_text(x) for x in [p.get("descriptionPlain") or p.get("description"), *(l.get("text", "") + " " + html_to_text(l.get("content")) for l in p.get("lists", []))])
            jobs.append(RawJob(self.name, employer or site, p.get("text", ""), p.get("hostedUrl") or p.get("applyUrl", ""), requisition_id=p.get("id"),
                               location=cats.get("location"), posted_at=str(p.get("createdAt")), content_text=text,
                               employment_type=(cats.get("commitment") or "").lower() or None, remote_policy=(p.get("workplaceType") or "").lower() or None,
                               salary=(p.get("salaryRange") or None), extra={"team": cats.get("team"), "apply_url": p.get("applyUrl")}))
        return jobs


class AshbySource(HttpSource):
    name = "ashby"

    def fetch(self, board_name: str, employer: str | None = None) -> list[RawJob]:
        data = self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true")
        jobs = []
        for j in data.get("jobs", []):
            comp = j.get("compensation") or {}
            jobs.append(RawJob(self.name, employer or board_name, j.get("title", ""), j.get("jobUrl", ""), requisition_id=j.get("id"),
                               location=j.get("location"), posted_at=j.get("publishedAt"), content_text=html_to_text(j.get("descriptionHtml") or j.get("descriptionPlain")),
                               employment_type=(j.get("employmentType") or "").lower() or None, remote_policy="remote" if j.get("isRemote") else None,
                               salary=comp or None, extra={"apply_url": j.get("applyUrl"), "department": j.get("department")}))
        return jobs


SOURCES = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource}


# -- snapshots -------------------------------------------------------------------------


def make_snapshot(url: str, text: str, raw_html: str | None = None, extracted: dict | None = None, source: str = "import") -> dict:
    text = text.strip()
    return {"url": url, "source": source, "fetched_at": utcnow(), "text": text[:60000], "text_sha256": sha256_bytes(text.encode()),
            "html_sha256": sha256_bytes(raw_html.encode()) if raw_html else None, "extracted": extracted or {}, "posting_hash": sha256_bytes(text.encode())[:16]}


# -- deterministic qualification -------------------------------------------------------


INJECTION_PATTERNS = [
    r"ignore (all|any|previous|prior) instructions", r"you are (now )?an? (ai|assistant|agent)", r"system prompt", r"disregard (the|your) (rules|instructions)",
    r"mark (this|the) (job|posting|candidate) as", r"submit (the )?application (now|immediately|automatically)", r"send (a |the )?message to", r"reveal|print (your|the) (secret|key|token|password)",
    r"<\s*/?\s*(system|assistant|tool|instruction)", r"run (the )?(command|tool|script)", r"api[_ -]?key",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)

CONTRACT_TERMS = ("contract", "contractor", "temporary", "temp ", "fixed term", "fixed-term", "internship", "intern ", "co-op", "coop", "freelance", "part-time", "part time")
PERMANENT_TERMS = ("permanent", "full-time", "full time", "fulltime", "regular")
ONTARIO_TERMS = ("ontario", "toronto", "gta", "mississauga", "milton", "oakville", "markham", "vaughan", "brampton", "burlington", "hamilton", "waterloo", "kitchener", "ottawa", "canada")
STARTUP_SIGNALS = ("early stage", "early-stage", "seed stage", "pre-seed", "series a", "founding engineer", "small team of", "first engineer")


class Qualification(BaseModel):
    decision: str  # eligible | excluded | needs_review
    reasons: list[str]
    flags: list[str] = []
    employment_type: str | None = None
    location_ok: bool | None = None
    salary_outcome: str | None = None
    injection_suspected: bool = False


def detect_injection(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _INJECTION_RE.finditer(text or "")})


def qualify(job: dict, extracted: dict, profile: Profile, posting_text: str = "") -> Qualification:
    reasons: list[str] = []
    flags: list[str] = []
    decision = "eligible"
    text_low = normalize_text(" ".join([job.get("title", ""), extracted.get("title") or "", posting_text or ""]))
    title_low = normalize_text(job.get("title", ""))
    prefs = profile.preferences

    ex = profile.is_excluded_employer(job.get("employer", ""))
    if ex and "applications" in ex.applies_to:
        return Qualification(decision="excluded", reasons=[f"employer excluded: {ex.name} ({ex.reason})"])

    inj = detect_injection(posting_text)
    if inj:
        flags.append("injection_suspected")
        reasons.append(f"posting contains instruction-like text: {inj[:3]}")

    if (extracted.get("closing_status") or "").lower() in ("closed", "filled", "expired"):
        return Qualification(decision="excluded", reasons=["posting closed"], flags=flags, injection_suspected=bool(inj))

    # employment type
    etype = (extracted.get("employment_type") or job.get("employment_type") or "").lower()
    if not etype:
        if any(t in text_low for t in ("contract", "temporary", "internship", "co op", "coop", "freelance", "part time")):
            etype = "contract_or_temporary"
        elif any(t in text_low for t in ("permanent", "full time", "fulltime", "regular")):
            etype = "permanent"
        else:
            etype = "unknown"
    if "permanent" in prefs.employment_types:
        if etype in ("contract", "contractor", "temporary", "internship", "intern", "co-op", "part-time", "contract_or_temporary", "fixed-term"):
            if "permanent" in text_low and "contract" in text_low:
                flags.append("permanent_or_contract")
                decision = "needs_review"
                reasons.append("posting offers permanent or contract; confirm permanent")
            else:
                return Qualification(decision="excluded", reasons=[f"employment type {etype} is not permanent"], employment_type=etype, flags=flags)
        elif etype == "unknown":
            flags.append("employment_type_unknown")
            decision = "needs_review"
            reasons.append("employment type not stated")

    # role family
    if any(x in title_low for x in prefs.excluded_role_families):
        hit = next(x for x in prefs.excluded_role_families if x in title_low)
        if "ai engineer" in title_low and hit in ("staff", "principal"):
            pass
        else:
            return Qualification(decision="excluded", reasons=[f"title family excluded: '{hit}'"], employment_type=etype, flags=flags)
    if prefs.role_families and not any(x in title_low for x in prefs.role_families):
        decision = "needs_review"
        reasons.append("title is outside the preferred role families")
        flags.append("role_family_unclear")

    # location
    loc = normalize_text(" ".join(filter(None, [job.get("location") or "", extracted.get("location") or "", extracted.get("remote_policy") or ""])))
    remote = "remote" in loc or (extracted.get("remote_policy") or "").lower() == "remote" or "remote" in text_low
    location_ok: bool | None
    if any(t in loc for t in ONTARIO_TERMS):
        location_ok = True
    elif remote:
        if any(t in text_low for t in ("ontario", "canada", "canadian residents")):
            location_ok = True
        else:
            location_ok = None
            decision = "needs_review"
            reasons.append("remote role; eligibility for Ontario residents not stated")
    elif not loc:
        location_ok = None
        decision = "needs_review"
        reasons.append("location not stated")
    else:
        location_ok = False
        return Qualification(decision="excluded", reasons=[f"location '{loc}' is outside Ontario/Canada"], employment_type=etype, flags=flags, location_ok=False)

    # salary
    comp = profile.compensation
    smin, smax, cur = extracted.get("salary_min"), extracted.get("salary_max"), (extracted.get("salary_currency") or "").upper()
    salary_outcome = "unknown"
    if smin or smax:
        if extracted.get("salary_is_total_comp"):
            salary_outcome = "total_comp_only"
            decision = "needs_review"
            reasons.append("only total compensation/OTE stated; base unknown")
        elif cur and cur != comp.currency:
            salary_outcome = "foreign_currency"
            decision = "needs_review"
            reasons.append(f"salary in {cur}; confirm CAD base")
        elif (extracted.get("salary_period") or "annual") != "annual":
            salary_outcome = "non_annual"
            decision = "needs_review"
            reasons.append("salary period is not annual")
        elif comp.floor_base and smax and smax < comp.floor_base:
            return Qualification(decision="excluded", reasons=[f"published base max {smax} below floor {comp.floor_base}"], employment_type=etype, flags=flags, location_ok=location_ok, salary_outcome="below_floor")
        elif comp.floor_base and smin and smin < comp.floor_base <= (smax or smin):
            salary_outcome = "overlaps_floor"
            flags.append("salary_overlaps_floor")
            reasons.append("published band overlaps the floor; confirm")
        else:
            salary_outcome = "meets_floor"
    else:
        flags.append("salary_unknown")
        reasons.append("no published salary; stays unknown")

    if prefs.small_startup_rule == "needs_review" and any(s in text_low for s in STARTUP_SIGNALS):
        flags.append("possible_small_startup")
        decision = "needs_review"
        reasons.append("posting signals an early-stage company; confirm employer size preference")

    if inj:
        decision = "needs_review" if decision == "eligible" else decision
    return Qualification(decision=decision, reasons=reasons, flags=flags, employment_type=etype, location_ok=location_ok,
                         salary_outcome=salary_outcome, injection_suspected=bool(inj))


# -- discovery service -----------------------------------------------------------------------


def compact_facts(profile: Profile) -> list[str]:
    facts = [f"{e.id}: {e.employer} {e.title} {e.kind} {e.start or '?'}..{e.end or 'present' if e.end_known else '?'}" for e in profile.employment]
    facts += [f"{s.name}" for s in profile.skills][:40]
    facts += [f"{f.id}: {f.key}={f.value}" for f in profile.education if f.value]
    return facts


class Discovery:
    def __init__(self, store: Store, profile: Profile, ai: AIRunner | None = None) -> None:
        self.store = store
        self.profile = profile
        self.jobs = JobStore(store)
        self.ai = ai

    def ingest_raw(self, raw: RawJob) -> tuple[dict, bool]:
        extracted = {"employment_type": raw.employment_type, "remote_policy": raw.remote_policy, "location": raw.location}
        if raw.salary:
            extracted.update(_salary_from_source(raw.salary))
        snapshot = make_snapshot(raw.url, raw.content_text, extracted=extracted, source=raw.source)
        job, created = self.jobs.upsert(employer=raw.employer, title=raw.title, posting_url=raw.url, requisition_id=raw.requisition_id or None,
                                        location=raw.location, source=raw.source, snapshot=snapshot,
                                        extra={"apply_url": raw.extra.get("apply_url") or raw.url, "posted_at": raw.posted_at})
        return job, created

    def import_url(self, url: str, employer: str | None = None, title: str | None = None, posting_text: str | None = None, requisition_id: str | None = None,
                   location: str | None = None) -> dict:
        li = extract_linkedin_job_id(url)
        employer = employer or ("Unknown employer" if not li else "Unknown employer (LinkedIn)")
        title = title or "Unknown title"
        snapshot = make_snapshot(url, posting_text or "", source="linkedin_import" if li else "url_import") if posting_text else None
        job, _ = self.jobs.upsert(employer=employer, title=title, posting_url=url, requisition_id=requisition_id, linkedin_job_id=li, location=location,
                                  source="linkedin_import" if li else "url_import", snapshot=snapshot)
        if li:
            self.jobs.set_status(job["_id"], job.get("status", "new"), apply_route="linkedin_or_employer_site_handoff")
        return self.jobs.get(job["_id"])

    def qualify_job(self, job_id: str, use_model_extraction: bool = True) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        snapshot = job.get("latest_snapshot") or {}
        text = snapshot.get("text", "")
        extracted = dict(snapshot.get("extracted") or {})
        if text and use_model_extraction and self.ai is not None and not extracted.get("_model_extracted"):
            try:
                res = self.ai.run("extract_posting", ExtractInput(posting_text=text[:12000], posting_url=job.get("posting_url") or ""), posting_hash=snapshot.get("posting_hash"), untrusted_fields=("posting_text",))
                for k, v in res.output.items():
                    if v not in (None, "", [], False) and not extracted.get(k):
                        extracted[k] = v
                extracted["_model_extracted"] = True
            except ProviderUnavailable:
                pass
        q = qualify(job, extracted, self.profile, text)
        dupes = [d["_id"] for d in self.jobs.possible_duplicates(job)]
        prior = self.store.c("applications").find_one({"job_id": job_id, "status": "submitted"})
        if prior:
            q = Qualification(decision="excluded", reasons=[f"already applied: tracker record {prior.get('tracker_record_id')}"], flags=q.flags + ["already_applied"])
        status = {"eligible": "qualified", "excluded": "excluded", "needs_review": "needs_review"}[q.decision]
        self.jobs.set_status(job_id, status, qualification=q.model_dump(), extracted=extracted, possible_duplicates=dupes, qualified_at=utcnow())
        return self.jobs.get(job_id)

    def rank_fit(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if self.ai is None:
            return job
        snapshot = job.get("latest_snapshot") or {}
        facts = compact_facts(self.profile)
        known_ids = {f.split(":")[0] for f in facts if ":" in f}
        try:
            res = self.ai.run("assess_fit", FitInput(job_title=job["title"], employer=job["employer"], posting_text=snapshot.get("text", "")[:12000], candidate_facts=facts,
                                                     experience_summary=compute_experience(self.profile.employment).as_dict(), role_rules={"families": self.profile.preferences.role_families}),
                              fact_version=self.profile.version, posting_hash=snapshot.get("posting_hash"), untrusted_fields=("posting_text",))
        except ProviderUnavailable:
            return job
        fit = res.output
        grounded = [p for p in fit.get("fit_points", []) if any(k in p for k in known_ids)]
        dropped = [p for p in fit.get("fit_points", []) if p not in grounded]
        fit["fit_points"], fit["ungrounded_points_dropped"] = grounded, dropped
        self.jobs.set_status(job_id, job["status"], fit=fit, fit_cache_key=res.cache_key)
        return self.jobs.get(job_id)


def _salary_from_source(s: dict) -> dict:
    out: dict[str, Any] = {}
    if "min" in s or "max" in s:
        out["salary_min"], out["salary_max"], out["salary_currency"] = s.get("min"), s.get("max"), s.get("currency")
        out["salary_period"] = "annual" if (s.get("interval") or "per-year-salary").startswith("per-year") else s.get("interval")
    elif "summaryComponents" in s:
        for c in s.get("summaryComponents", []):
            if c.get("compensationType") == "Salary":
                out.update(salary_min=c.get("minValue"), salary_max=c.get("maxValue"), salary_currency=c.get("currencyCode"), salary_period="annual" if c.get("interval") == "1 YEAR" else c.get("interval"))
    return out
