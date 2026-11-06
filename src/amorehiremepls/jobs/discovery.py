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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from amorehiremepls.candidate.profile import Profile, compute_experience
from amorehiremepls.core.ai import AIRunner, ProviderUnavailable
from amorehiremepls.core.db import Store
from amorehiremepls.core.skills import ExtractInput, FitInput
from amorehiremepls.core.util import extract_linkedin_job_id, normalize_text, sha256_bytes, utcnow
from amorehiremepls.jobs.jobs import JobStore

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
            jobs.append(
                RawJob(
                    self.name,
                    employer or data.get("meta", {}).get("company", board_token),
                    j.get("title", ""),
                    j.get("absolute_url", ""),
                    requisition_id=str(j.get("id") or j.get("requisition_id") or ""),
                    location=(j.get("location") or {}).get("name"),
                    posted_at=j.get("updated_at"),
                    content_text=html_to_text(j.get("content")),
                    employment_type=greenhouse_employment(j.get("metadata")),
                    extra={"departments": [d.get("name") for d in j.get("departments", [])]},
                )
            )
        return jobs


class LeverSource(HttpSource):
    name = "lever"

    def fetch(self, site: str, employer: str | None = None) -> list[RawJob]:
        data = self._get_json(f"https://api.lever.co/v0/postings/{site}?mode=json")
        jobs = []
        for p in data if isinstance(data, list) else []:
            cats = p.get("categories", {})
            text = " ".join(
                html_to_text(x)
                for x in [p.get("descriptionPlain") or p.get("description"), *(lst.get("text", "") + " " + html_to_text(lst.get("content")) for lst in p.get("lists", []))]
            )
            jobs.append(
                RawJob(
                    self.name,
                    employer or site,
                    p.get("text", ""),
                    p.get("hostedUrl") or p.get("applyUrl", ""),
                    requisition_id=p.get("id"),
                    location=cats.get("location"),
                    posted_at=str(p.get("createdAt")),
                    content_text=text,
                    employment_type=(cats.get("commitment") or "").lower() or None,
                    remote_policy=(p.get("workplaceType") or "").lower() or None,
                    salary=(p.get("salaryRange") or None),
                    extra={"team": cats.get("team"), "apply_url": p.get("applyUrl")},
                )
            )
        return jobs


class AshbySource(HttpSource):
    name = "ashby"

    def fetch(self, board_name: str, employer: str | None = None) -> list[RawJob]:
        data = self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true")
        jobs = []
        for j in data.get("jobs", []):
            comp = j.get("compensation") or {}
            jobs.append(
                RawJob(
                    self.name,
                    employer or board_name,
                    j.get("title", ""),
                    j.get("jobUrl", ""),
                    requisition_id=j.get("id"),
                    location=j.get("location"),
                    posted_at=j.get("publishedAt"),
                    content_text=html_to_text(j.get("descriptionHtml") or j.get("descriptionPlain")),
                    employment_type=(j.get("employmentType") or "").lower() or None,
                    remote_policy="remote" if j.get("isRemote") else None,
                    salary=comp or None,
                    extra={"apply_url": j.get("applyUrl"), "department": j.get("department")},
                )
            )
        return jobs


SOURCES = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource}


# -- snapshots -------------------------------------------------------------------------


_WORKDAY_URL = re.compile(r"^https://(?P<tenant>[a-z0-9-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/]+)/job/(?P<path>.+)$")


def workday_json_url(url: str) -> str | None:
    """Public JSON endpoint behind a Workday posting page, or None when the URL is not a Workday job page."""
    m = _WORKDAY_URL.match(url.split("?")[0].rstrip("/"))
    if not m:
        return None
    return f"https://{m.group('tenant')}.{m.group('dc')}.myworkdayjobs.com/wday/cxs/{m.group('tenant')}/{m.group('site')}/job/{m.group('path')}"


# A posting check treats these as "no longer up". A short successful read is the same: the page came back, and it is not a job ad.
MIN_POSTING_CHARS = 200
POSTING_CHECK_HOURS = 6
POSTING_CHECK_BATCH = 25
POSTING_CLOSED_REASON = "This posting is no longer up"
_GONE_PAGE = re.compile(
    r"does not exist|doesn't exist|no longer available|no longer exists|page you are looking for|"
    r"job has expired|posting (?:has|is) expired|this job is closed|position has been filled|job is no longer",
    re.I,
)


def read_posting(url: str, client: httpx.Client | None = None, timeout: float = 20.0) -> dict:
    """One GET. HTTP 403 and 404 come back as data; other network failures set `error` and no status."""
    try:
        text, source, raw = fetch_posting_text(url, client, timeout)
        return {"text": text, "source": source, "html": raw, "status_code": 200, "error": None}
    except httpx.HTTPStatusError as exc:
        body = exc.response.text or ""
        return {"text": html_to_text(body)[:2000], "source": "", "html": body, "status_code": exc.response.status_code, "error": None}
    except httpx.HTTPError as exc:
        return {"text": "", "source": "", "html": None, "status_code": None, "error": type(exc).__name__}


def posting_is_gone(status_code: int | None, text: str) -> bool:
    """True when the employer page is a refusal, says the job is gone, or is too short to be a posting."""
    if status_code in (403, 404):
        return True
    if _GONE_PAGE.search(text or ""):
        return True
    return status_code == 200 and len((text or "").strip()) < MIN_POSTING_CHARS


def _checked_recently(when: datetime | None, now: datetime) -> bool:
    if not isinstance(when, datetime):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=now.tzinfo)
    return now - when < timedelta(hours=POSTING_CHECK_HOURS)


def fetch_posting_text(url: str, client: httpx.Client | None = None, timeout: float = 20.0) -> tuple[str, str, str | None]:
    """(text, source, raw_html) for one posting URL with a single GET. No browser, no login, no model."""
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "AmoreHireMePls/0.1 (posting reader)"})
    try:
        wd = workday_json_url(url)
        if wd:
            r = client.get(wd, headers={"Accept": "application/json"})
            r.raise_for_status()
            info = (r.json() or {}).get("jobPostingInfo") or {}
            parts = [info.get("title"), info.get("location"), info.get("timeType"), info.get("jobDescription")]
            return html_to_text(" ".join(str(p) for p in parts if p)), "workday_json", None
        r = client.get(url)
        r.raise_for_status()
        body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        return html_to_text(body), "url_refresh", r.text
    finally:
        if own:
            client.close()


def make_snapshot(url: str, text: str, raw_html: str | None = None, extracted: dict | None = None, source: str = "import") -> dict:
    text = text.strip()
    return {
        "url": url,
        "source": source,
        "fetched_at": utcnow(),
        "text": text[:60000],
        "text_sha256": sha256_bytes(text.encode()),
        "html_sha256": sha256_bytes(raw_html.encode()) if raw_html else None,
        "extracted": extracted or {},
        "posting_hash": sha256_bytes(text.encode())[:16],
    }


# -- deterministic qualification -------------------------------------------------------


INJECTION_PATTERNS = [
    r"ignore (all|any|previous|prior) instructions",
    r"you are (now )?an? (ai|assistant|agent)",
    r"system prompt",
    r"disregard (the|your) (rules|instructions)",
    r"mark (this|the) (job|posting|candidate) as",
    r"submit (the )?application (now|immediately|automatically)",
    r"send (a |the )?message to",
    r"reveal|print (your|the) (secret|key|token|password)",
    r"<\s*/?\s*(system|assistant|tool|instruction)",
    r"run (the )?(command|tool|script)",
    r"api[_ -]?key",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)

# A job board's own employment field. "Regular" is the usual full-time value. contract_or_temporary is not a board
# value; it was an old guess from a loose word scan and is ignored.
_BOARD_PERMANENT = {"permanent", "full-time", "full time", "fulltime", "regular"}
_BOARD_NON_PERMANENT = {
    "contract": "a contract role",
    "contractor": "a contract role",
    "temporary": "a temporary role",
    "internship": "an internship",
    "intern": "an internship",
    "co-op": "a co-op",
    "coop": "a co-op",
    "part-time": "a part-time role",
    "part time": "a part-time role",
    "fixed-term": "a fixed-term role",
    "fixed term": "a fixed-term role",
    "freelance": "a freelance role",
    "seasonal": "a seasonal role",
}
# An explicit statement that the role itself is not permanent. A bare "contract" inside another word
# ("contractor", "ContractAPI") does not match. "excluding internships / co-ops" is about experience, not the role.
_EXPERIENCE_ASIDE = re.compile(r"\b(?:excluding|except|not including|other than|beyond)\b(?:\W+\w+){0,6}\W*$", re.I)
_NON_PERMANENT_PHRASE = re.compile(
    r"\b("
    r"\d{1,2}\s*-\s*months?\s+contracts?"
    r"|\d{1,2}\s+months?\s+contracts?"
    r"|fixed[\s-]term"
    r"|temporary(?:\s+(?:position|role|contract|employment|assignment))"
    r"|contracts?(?:\s+(?:position|role|opening|basis))"
    r"|on a contract"
    r"|part[\s-]time"
    r"|freelance"
    r"|seasonal(?:\s+(?:position|role|work|employment))"
    r"|(?:internships?|interns?|co[\s-]?ops?)"
    r")\b",
    re.I,
)
_PERMANENT_ROLE = re.compile(r"\b(?:permanent|full[\s-]time)\s+(?:position|role|employment|opportunity)\b", re.I)
# Profession neutral signs of a very small or very new company. A candidate adds their own field's phrases
# ("founding engineer", "first sales hire") under [preferences].startup_signals_extra.
STARTUP_SIGNALS = ("early stage", "early-stage", "seed stage", "pre-seed", "series a", "small team of", "founding team", "first hire", "first employee")


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


def _phrase_in(term: str, text: str) -> bool:
    """Whole-word (or whole-phrase) match of a normalised term inside normalised text."""
    return bool(term) and re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def work_arrangement(policy: str | None) -> str | None:
    """Normalise a stated remote policy to remote | hybrid | onsite; None when it is not stated or not a known kind."""
    p = (policy or "").strip().lower().replace("_", " ").replace("-", " ")
    if not p:
        return None
    if "hybrid" in p:
        return "hybrid"
    if "remote" in p:
        return "remote"
    if p in ("onsite", "on site", "in office", "in person", "office") or "on site" in p or "in office" in p:
        return "onsite"
    return None


def greenhouse_employment(metadata: list | None) -> str | None:
    """The board's Employment Type value, lowercased, or None when the job does not state one."""
    for row in metadata or []:
        if str(row.get("name") or "").strip().lower() == "employment type":
            value = str(row.get("value") or "").strip().lower()
            return value or None
    return None


def explicit_non_permanent_phrase(text: str) -> str | None:
    """The posting's own words when it states that the role is not permanent. None when it says nothing, or the
    words are about past experience ("excluding internships") or sit inside another word ("contractor")."""
    for match in _NON_PERMANENT_PHRASE.finditer(text or ""):
        if _EXPERIENCE_ASIDE.search((text or "")[: match.start()]):
            continue
        return re.sub(r"\s+", " ", match.group(0)).strip()
    return None


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

    # employment type. Silence is permanent. Only the board's own field, or an explicit phrase in the posting, says otherwise.
    raw = (extracted.get("employment_type") or job.get("employment_type") or "").strip().lower()
    if raw == "contract_or_temporary":
        raw = ""
    phrase = None
    board_label = None
    if raw in _BOARD_NON_PERMANENT:
        etype = "non_permanent"
        board_label = _BOARD_NON_PERMANENT[raw]
    elif raw in _BOARD_PERMANENT:
        etype = "permanent"
    else:
        phrase = explicit_non_permanent_phrase(posting_text or "")
        if phrase and _PERMANENT_ROLE.search(posting_text or ""):
            etype = "either"
        elif phrase:
            etype = "non_permanent"
        else:
            etype = "permanent"
    if "permanent" in prefs.employment_types:
        if etype == "either":
            flags.append("permanent_or_contract")
            decision = "needs_review"
            reasons.append(f'The posting offers a permanent role and also says "{phrase}". Confirm it is permanent.')
        elif etype == "non_permanent":
            if board_label:
                reason = f"The job board lists this as {board_label}, so this is not a permanent role."
            else:
                reason = f'The posting says "{phrase}", so this is not a permanent role.'
            return Qualification(decision="excluded", reasons=[reason], employment_type="contract", flags=flags)

    # role family. An exception phrase in the title ("ai engineer") keeps it even when an excluded word also appears.
    excepted = any(x in title_low for x in prefs.excluded_role_family_exceptions)
    if not excepted and any(x in title_low for x in prefs.excluded_role_families):
        hit = next(x for x in prefs.excluded_role_families if x in title_low)
        return Qualification(decision="excluded", reasons=[f"title family excluded: '{hit}'"], employment_type=etype, flags=flags)
    if prefs.role_families and not any(x in title_low for x in prefs.role_families):
        decision = "needs_review"
        reasons.append("title is outside the preferred role families")
        flags.append("role_family_unclear")

    # location: the candidate's place words accept a posting; remote postings need one of the remote eligibility words
    loc = normalize_text(" ".join(filter(None, [job.get("location") or "", extracted.get("location") or "", extracted.get("remote_policy") or ""])))
    remote = "remote" in loc or (extracted.get("remote_policy") or "").lower() == "remote" or "remote" in text_low
    place_terms = [normalize_text(t) for t in prefs.locations if t.strip()]
    remote_terms = [normalize_text(t) for t in (prefs.remote_eligible_terms or prefs.locations) if t.strip()]
    region = prefs.region_label or (", ".join(prefs.locations[:3]) if prefs.locations else "your locations")
    location_ok: bool | None
    if not place_terms:
        location_ok = None if not loc else True  # no location rule saved: nothing to check
    elif any(_phrase_in(t, loc) for t in place_terms):
        location_ok = True
    elif remote:
        if any(_phrase_in(t, text_low) for t in remote_terms):
            location_ok = True
        else:
            location_ok = None
            decision = "needs_review"
            reasons.append(f"remote role; eligibility for {region} residents not stated")
    elif not loc:
        location_ok = None
        decision = "needs_review"
        reasons.append("location not stated")
    else:
        location_ok = False
        return Qualification(decision="excluded", reasons=[f"location '{loc}' is outside {region}"], employment_type=etype, flags=flags, location_ok=False)

    # work arrangement: only a review reason when the posting states a kind the candidate has not accepted
    arrangement = work_arrangement(extracted.get("remote_policy"))
    if arrangement and arrangement not in prefs.work_arrangements:
        decision = "needs_review"
        flags.append("work_arrangement_not_accepted")
        reasons.append(f"posting is {arrangement}; not in your accepted work arrangements")

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
            reasons.append(f"salary in {cur}; confirm the base in {comp.currency or 'your currency'}")
        elif (extracted.get("salary_period") or "annual") != "annual":
            salary_outcome = "non_annual"
            decision = "needs_review"
            reasons.append("salary period is not annual")
        elif comp.floor_base and smax and smax < comp.floor_base:
            return Qualification(
                decision="excluded",
                reasons=[f"published base max {smax} below floor {comp.floor_base}"],
                employment_type=etype,
                flags=flags,
                location_ok=location_ok,
                salary_outcome="below_floor",
            )
        elif comp.floor_base and smin and smin < comp.floor_base <= (smax or smin):
            salary_outcome = "overlaps_floor"
            flags.append("salary_overlaps_floor")
            reasons.append("published band overlaps the floor; confirm")
        else:
            salary_outcome = "meets_floor"
    else:
        flags.append("salary_unknown")
        reasons.append("no published salary; stays unknown")

    signals = [normalize_text(s) for s in (*STARTUP_SIGNALS, *prefs.startup_signals_extra) if s.strip()]
    if prefs.small_startup_rule == "needs_review" and any(s in text_low for s in signals):
        flags.append("possible_small_startup")
        decision = "needs_review"
        reasons.append("posting signals an early-stage company; confirm employer size preference")

    if inj:
        decision = "needs_review" if decision == "eligible" else decision
    return Qualification(
        decision=decision, reasons=reasons, flags=flags, employment_type=etype, location_ok=location_ok, salary_outcome=salary_outcome, injection_suspected=bool(inj)
    )


# -- discovery service -----------------------------------------------------------------------


# Resume body sent with the one fit call. Employment rows and education stay outside this cap.
_RESUME_EVIDENCE_CHARS = 6000
_SUMMARY_CHARS = 700
_BULLET_CHARS = 320
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\w)")
_EVIDENCE_STOP = {
    "evidence",
    "specific",
    "development",
    "experience",
    "other",
    "framework",
    "frameworks",
    "services",
    "service",
    "with",
    "from",
    "that",
    "this",
    "your",
    "candidate",
    "whether",
    "technologies",
    "technology",
    "tools",
    "tool",
    "knowledge",
    "domain",
    "missing",
    "required",
    "using",
    "including",
    "related",
    "approved",
    "facts",
    "fact",
    "listed",
    "shown",
    "stated",
    "have",
    "has",
    "and",
    "the",
    "for",
    "not",
    "lacks",
    "lacking",
    "absent",
    "mention",
    "mentions",
    "mentioned",
    "named",
    "names",
    "shows",
    "lists",
    "list",
    "does",
    "demonstrated",
    "explicit",
    "explicitly",
    "directly",
    "hands",
    "proficiency",
    "proficient",
    "familiarity",
    "familiar",
    "exposure",
    "skills",
    "skill",
    "any",
    "only",
    "also",
    "but",
    "nor",
    "none",
    "about",
    "into",
    "than",
    "there",
    "their",
    "they",
    "was",
    "were",
    "are",
    "been",
    "unclear",
    "resume",
    "profile",
    "posting",
    "role",
    "roles",
    "job",
    "position",
    "requirement",
    "requirements",
    "requires",
    "require",
    "working",
    "work",
}
# A gap line about any of these is never dropped by a skills match, whatever the evidence says: seniority, leadership,
# ownership, design authority, support duties, credentials and durations are not skills. A candidate adds names of
# their own (an employer, a domain) under [fit].hold_back_terms.
_ALWAYS_KEEP = re.compile(
    r"\bsenior(?:ity)?\b|\bstaff\b|\bprincipal\b|\blead(?:s|ing|er|ership)?\b|\bled\b|\bmentor\w*|\bmanag\w*|\bownership\b|\bown(?:ed|s)?\b|"
    r"\barchitect\w*|\bdesign authority\b|\bon[- ]?call\b|\bproduction support\b|\bincident\w*|\bcertif\w*|\bdegree\b|\bbachelor\w*|\bmaster'?s?\b|"
    r"\blicen[cs]e\w*|\byears?\b|\byrs\b|\bmonths?\b",
    re.I,
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.]*")


def _alt(words: list[str]) -> str:
    return "|".join(re.escape(w.strip()) for w in words if w.strip())


@dataclass
class FitTerms:
    """What makes a gap line "about a skill" for one candidate: skill words (from [fit].skill_terms, the term alias
    members and the evidence's own skill lines), category words answered by any member, and names held back."""

    skill_words: re.Pattern[str] | None
    categories: list[tuple[re.Pattern[str], re.Pattern[str]]]
    hold_back: re.Pattern[str] | None
    # Words of the candidate's own field ("backend", "clinical", "enterprise") that describe rather than claim.
    ignore: set[str] = field(default_factory=set)


def fit_terms(rules: Any | None, evidence_skills: list[str] | None = None, field_words: list[str] | None = None) -> FitTerms:
    skill_words: list[str] = list(getattr(rules, "skill_terms", None) or [])
    ignore = {w for phrase in (field_words or []) for w in re.findall(r"[a-z0-9]+", str(phrase).lower()) if len(w) > 2}
    categories: list[tuple[re.Pattern[str], re.Pattern[str]]] = []
    for alias in getattr(rules, "term_aliases", None) or []:
        words, members = list(alias.words), list(alias.evidence)
        if not words:
            continue
        skill_words.extend(words)
        skill_words.extend(members)
        proof = _alt(members or words)
        categories.append((re.compile(_alt(words), re.I), re.compile(rf"(?<![A-Za-z0-9+#])(?:{proof})(?![A-Za-z0-9+#])", re.I)))
    for s in evidence_skills or []:
        s = s.strip()
        if 2 <= len(s) <= 40:
            skill_words.append(s)
    alt = _alt(sorted(set(skill_words), key=len, reverse=True))
    hold = _alt(getattr(rules, "hold_back_terms", None) or [])
    return FitTerms(
        skill_words=re.compile(rf"(?<![A-Za-z0-9+#])(?:{alt})(?![A-Za-z0-9+#])", re.I) if alt else None,
        categories=categories,
        hold_back=re.compile(rf"(?<![A-Za-z0-9])(?:{hold})(?![A-Za-z0-9])", re.I) if hold else None,
        ignore=ignore,
    )


def _evidence_skills(facts: list[str]) -> list[str]:
    """Skill names from the compact facts (`skill_N: Java`, `pskill_N: Java`)."""
    out: list[str] = []
    for f in facts:
        head, _, body = f.partition(":")
        if head.startswith(("skill_", "pskill_")) and body.strip():
            out.append(body.strip())
    return out


def _clip(text: str, limit: int) -> str:
    text = _EMAIL_RE.sub("", text or "")
    text = _PHONE_RE.sub("", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def _carry_name(title: str, employer: str) -> str:
    from amorehiremepls.apply.tailoring import DATE_RE

    parts: list[str] = []
    for part in (title, employer):
        cleaned = DATE_RE.sub("", part or "")
        cleaned = " ".join(cleaned.split()).strip(" |,-–—")
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    return ", ".join(parts)


def _stitch_roles(experiences: list) -> list:
    """Bullets that the parser hung on the employer line belong to the dated role above them."""
    from amorehiremepls.apply.tailoring import DATE_RE

    roles: list = []
    for exp in experiences:
        if roles and not exp.dates and exp.bullets and not roles[-1].bullets:
            host = roles[-1]
            carried = _carry_name(exp.title, exp.employer)
            if carried and (not host.employer or DATE_RE.search(host.employer)):
                host.employer = carried
            host.bullets.extend(exp.bullets)
            continue
        roles.append(exp)
    return roles


def _role_label(exp) -> str:
    from amorehiremepls.apply.tailoring import DATE_RE

    employer = exp.employer or ""
    if DATE_RE.search(employer):
        employer = DATE_RE.sub("", employer)
    label = _carry_name(exp.title, employer)
    return label or "experience"


def _resume_lines(profile: Profile, posting_text: str) -> list[str]:
    """Approved resume summary, skills, bullets, and certifications. Empty when no file is selected."""
    base_resume = profile.base_resume
    if base_resume is None:
        return []
    path = Path(base_resume.path)
    if not path.is_file():
        log.info("base resume file missing; fit ranking uses profile rows only")
        return []
    try:
        from amorehiremepls.apply.tailoring import parse_base_resume

        parsed = parse_base_resume(path, rules=profile.preferences.resume)
    except Exception as exc:
        log.warning("base resume could not be read for fit ranking: %s", type(exc).__name__)
        return []
    head: list[str] = []
    summary = _clip(parsed.summary, _SUMMARY_CHARS)
    if summary:
        head.append(f"resume_summary: {summary}")
    for i, skill in enumerate(parsed.skills, 1):
        name = _clip(skill, 80)
        if name:
            head.append(f"skill_{i}: {name}")
    for i, cert in enumerate(parsed.certifications, 1):
        name = _clip(cert, 160)
        if name:
            head.append(f"cert_{i}: {name}")
    terms = {w for w in re.findall(r"[a-z0-9+#.]{3,}", (posting_text or "").lower()) if w not in _EVIDENCE_STOP}

    def _bullet_line(bullet, label: str, order: int) -> tuple[int, int, str] | None:
        text = _clip(bullet.text, _BULLET_CHARS)
        if not text:
            return None
        score = len(set(re.findall(r"[a-z0-9+#.]{3,}", text.lower())) & terms)
        return score, order, f"{bullet.id}: {label}: {text}"

    per_role: list[str] = []
    pool: list[tuple[int, int, str]] = []
    order = 0
    for exp in [*_stitch_roles(parsed.experience), *parsed.projects]:
        label = _role_label(exp)
        ranked: list[tuple[int, int, str]] = []
        for bullet in exp.bullets:
            item = _bullet_line(bullet, label, order)
            if item is None:
                continue
            ranked.append(item)
            order += 1
        if not ranked:
            continue
        ranked.sort(key=lambda item: (-item[0], item[1]))
        per_role.append(ranked[0][2])
        pool.extend(ranked[1:])
    pool.sort(key=lambda item: (-item[0], item[1]))
    kept: list[str] = []
    used = 0
    for line in [*head, *per_role, *[item[2] for item in pool]]:
        extra = len(line) + (1 if used else 0)
        if used and used + extra > _RESUME_EVIDENCE_CHARS:
            continue
        kept.append(line)
        used += extra
    return kept


_FILE_HEADING_RE = re.compile(r"^## \S.*\.(?:md|txt|docx)$", re.I)


def _supplemental_lines(supplemental_text: str, posting_text: str, budget: int) -> list[str]:
    """Paragraphs and bullet lines from the candidate's own extra notes (candidate/ folder), most posting-relevant
    first, as ``doc_<n>`` facts (n = position in the notes, so an id means the same passage for every posting).
    The loader's file headings (``## name.md``) are dropped; the candidate's own headings stay as short passages.
    Fits within ``budget`` characters."""
    if not supplemental_text or budget <= 0:
        return []
    terms = {w for w in re.findall(r"[a-z0-9+#.]{3,}", (posting_text or "").lower()) if w not in _EVIDENCE_STOP}
    passages: list[str] = []
    for block in re.split(r"\n\s*\n", supplemental_text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip() and not _FILE_HEADING_RE.match(ln.strip())]
        if not lines:
            continue
        if all(re.match(r"^[-*•]\s+", ln) for ln in lines):  # a bullet list: one fact per bullet
            passages.extend(re.sub(r"^[-*•]\s+", "", ln) for ln in lines)
        else:
            passages.append(" ".join(lines))
    ranked: list[tuple[int, int, str]] = []
    for n, passage in enumerate(passages, 1):
        text = _clip(re.sub(r"^#+\s*", "", passage), _BULLET_CHARS)
        if not text:
            continue
        score = len(set(re.findall(r"[a-z0-9+#.]{3,}", text.lower())) & terms)
        ranked.append((score, n, f"doc_{n}: {text}"))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    kept: list[tuple[int, str]] = []
    used = 0
    for _score, n, line in ranked:
        extra = len(line) + (1 if used else 0)
        if used and used + extra > budget:
            continue
        kept.append((n, line))
        used += extra
    return [line for _n, line in sorted(kept)]


# The extra notes get whatever the resume left under _RESUME_EVIDENCE_CHARS, and never less than this.
_SUPPLEMENTAL_MIN_CHARS = 1500


def compact_facts(profile: Profile, posting_text: str = "", supplemental_text: str = "") -> list[str]:
    """Bounded facts for the one fit call. Profile rows first, then the approved resume when it is on disk, then the
    candidate's extra notes (``supplemental_text``) in the room the resume left under the same cap."""
    facts = [f"{e.id}: {e.employer} {e.title} {e.kind} {e.start or '?'}..{(e.end or 'present') if e.end_known else '?'}" for e in profile.employment]
    facts += [f"pskill_{i}: {s.name}" for i, s in enumerate(profile.skills, 1) if s.name][:40]
    facts += [f"{f.id}: {f.key}={f.value}" for f in profile.education if f.value]
    resume = _resume_lines(profile, posting_text)
    facts += resume
    if supplemental_text:
        room = _RESUME_EVIDENCE_CHARS - sum(len(line) + 1 for line in resume)
        facts += _supplemental_lines(supplemental_text, posting_text, max(room, _SUPPLEMENTAL_MIN_CHARS))
    return [f for f in facts if f.split(":", 1)[-1].strip()]


def _known_ids(facts: list[str]) -> set[str]:
    return {f.split(":", 1)[0] for f in facts if ":" in f and f.split(":", 1)[0]}


def _cites(point: str, known_ids: set[str]) -> bool:
    return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])", point) for k in known_ids)


def _word_in(word: str, evidence: str, flags: int) -> bool:
    return re.search(rf"(?<![A-Za-z0-9+#]){re.escape(word)}(?![A-Za-z0-9+#])", evidence, flags) is not None


def line_already_supported(line: str, evidence: str, terms: FitTerms | None = None) -> bool:
    """True when a gap line is only about skills and every word that carries meaning is already in the evidence.

    Conservative by design: a doubtful line stays a gap. Lines about seniority, leadership, architecture, on-call,
    certifications, education, durations, or a held-back name are never dropped. A line without a known skill word
    (``terms``: the candidate's fit rules plus the evidence's own skill lines) is never dropped; with no terms at all
    nothing is ever dropped. Every other word on the line, including numbers ("Java 21") and short names ("C#"), must
    itself appear in the evidence; a capitalised word must match case ("Batch" in "Spring Batch"). Only a category
    word from the term aliases (databases, cloud, ...) is answered by a member of that category (PostgreSQL, AWS).
    """
    if terms is None or terms.skill_words is None:
        return False
    if _ALWAYS_KEEP.search(line) or (terms.hold_back and terms.hold_back.search(line)) or not terms.skill_words.search(line):
        return False
    checked = False
    for index, raw in enumerate(_TOKEN_RE.findall(line)):
        word = raw.strip(".")
        if index == 0 and word[:1].isupper() and word[1:].islower():
            word = word.lower()  # sentence-start capitalisation ("No", "Lacks"), not a name or an acronym
        if not word:
            continue
        category = next((proof for member, proof in terms.categories if member.fullmatch(word)), None)
        if category is not None:
            checked = True
            if category.search(evidence):
                continue
            return False
        is_skill = terms.skill_words.fullmatch(word) is not None  # a skill word is always checked, even if it is also a field word
        if not is_skill and (word.lower() in _EVIDENCE_STOP or word.lower() in terms.ignore or (len(word) < 3 and word.isalpha() and word.islower())):
            continue
        checked = True
        flags = 0 if any(ch.isupper() for ch in word) else re.I
        if not _word_in(word, evidence, flags):
            return False
    return checked


def _drop_supported(lines: list, evidence: str, terms: FitTerms | None) -> tuple[list, list]:
    kept, dropped = [], []
    for line in lines:
        if isinstance(line, str) and line_already_supported(line, evidence, terms):
            dropped.append(line)
        else:
            kept.append(line)
    return kept, dropped


class Discovery:
    def __init__(self, store: Store, profile: Profile, ai: AIRunner | None = None, supplemental_text: str = "") -> None:
        self.store = store
        self.profile = profile
        self.jobs = JobStore(store)
        self.ai = ai
        self.supplemental_text = supplemental_text or ""  # the candidate's extra notes; evidence for the fit call only

    def ingest_raw(self, raw: RawJob) -> tuple[dict, bool]:
        extracted = {"employment_type": raw.employment_type, "remote_policy": raw.remote_policy, "location": raw.location}
        if raw.salary:
            extracted.update(_salary_from_source(raw.salary))
        snapshot = make_snapshot(raw.url, raw.content_text, extracted=extracted, source=raw.source)
        job, created = self.jobs.upsert(
            employer=raw.employer,
            title=raw.title,
            posting_url=raw.url,
            requisition_id=raw.requisition_id or None,
            location=raw.location,
            source=raw.source,
            snapshot=snapshot,
            extra={"apply_url": raw.extra.get("apply_url") or raw.url, "posted_at": raw.posted_at},
        )
        return job, created

    def import_url(
        self, url: str, employer: str | None = None, title: str | None = None, posting_text: str | None = None, requisition_id: str | None = None, location: str | None = None
    ) -> dict:
        li = extract_linkedin_job_id(url)
        employer = employer or ("Unknown employer" if not li else "Unknown employer (LinkedIn)")
        title = title or "Unknown title"
        snapshot = make_snapshot(url, posting_text or "", source="linkedin_import" if li else "url_import") if posting_text else None
        job, _ = self.jobs.upsert(
            employer=employer,
            title=title,
            posting_url=url,
            requisition_id=requisition_id,
            linkedin_job_id=li,
            location=location,
            source="linkedin_import" if li else "url_import",
            snapshot=snapshot,
        )
        if li:
            self.jobs.set_status(job["_id"], job.get("status", "new"), apply_route="linkedin")
        return self.jobs.get(job["_id"])

    def refresh_posting(self, job_id: str, client: httpx.Client | None = None) -> dict:
        """Re-read the posting text behind a job's URL (GET only) and store a fresh snapshot. Nothing is re-ranked.

        Workday sites are read through their public JSON endpoint (the HTML page is an empty shell); every other URL is
        read as HTML and reduced to text. An empty read leaves the existing snapshot alone and says so."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        url = job.get("posting_url") or job.get("apply_url")
        if not url:
            return {"job_id": job_id, "chars": 0, "stored": False, "reason": "job has no posting URL"}
        try:
            text, source, html_raw = fetch_posting_text(url, client)
        except httpx.HTTPError as exc:
            return {"job_id": job_id, "chars": 0, "stored": False, "reason": f"fetch failed ({type(exc).__name__})"}
        if len(text) < 200:
            return {"job_id": job_id, "chars": len(text), "stored": False, "reason": "page returned no posting text"}
        old = job.get("latest_snapshot") or {}
        snapshot = make_snapshot(url, text, raw_html=html_raw, extracted=dict(old.get("extracted") or {}), source=source)
        self.jobs.set_status(job_id, job["status"], latest_snapshot=snapshot)
        return {"job_id": job_id, "chars": len(snapshot["text"]), "stored": True, "source": source, "posting_hash": snapshot["posting_hash"]}

    def _note_posting(self, job_id: str, text: str, now: datetime) -> None:
        """One line on each unsent application for this job. Stored events are not removed."""
        self.store.c("applications").update_many(
            {"job_id": job_id, "tombstone": {"$ne": True}, "status": {"$nin": ["removed", "withdrawn"]}},
            {"$push": {"history": {"at": now, "text": text}}},
        )

    def _sent_application(self, job_id: str) -> bool:
        """A sent application, a saved receipt, or a result that is still unclear. Those jobs stay open."""
        for app in self.store.c("applications").find({"job_id": job_id}):
            if app.get("status") in ("submitted", "executing", "needs_verification"):
                return True
            evidence = app.get("evidence") or {}
            if evidence.get("receipt_text") or evidence.get("confirmation") or evidence.get("receipt_path") or evidence.get("basis") == "receipt_observed":
                return True
        return False

    def check_posting(self, job_id: str, *, force: bool = False, client: httpx.Client | None = None, artifact_dir: Path | None = None) -> dict:
        """Re-read one posting. A gone page closes the job, keeps the record, and drops unsent tailored resumes.

        A job checked in the last few hours is skipped unless `force` is set (the single-job action). A sent
        application is never closed and its files are never deleted."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        now = utcnow()
        if not force and _checked_recently(job.get("posting_checked_at"), now):
            return {"job_id": job_id, "outcome": "skipped", "reason": "checked recently"}
        if self._sent_application(job_id):
            self.jobs.set_status(job_id, job["status"], posting_checked_at=now)
            self._note_posting(job_id, "This posting was left open because an application was already sent", now)
            return {"job_id": job_id, "outcome": "kept_sent", "reason": "an application was already sent"}
        url = job.get("posting_url") or job.get("apply_url")
        if not url:
            self.jobs.set_status(job_id, job["status"], posting_checked_at=now)
            return {"job_id": job_id, "outcome": "unreadable", "reason": "job has no posting URL"}
        read = read_posting(url, client)
        if read["error"] and not posting_is_gone(read["status_code"], read["text"]):
            self.jobs.set_status(job_id, job["status"], posting_checked_at=now)
            self._note_posting(job_id, "The posting could not be read", now)
            return {"job_id": job_id, "outcome": "unreadable", "reason": read["error"]}
        if posting_is_gone(read["status_code"], read["text"]):
            self.jobs.set_status(job_id, "closed", closed_reason=POSTING_CLOSED_REASON, posting_checked_at=now)
            self._note_posting(job_id, POSTING_CLOSED_REASON, now)
            removed = 0
            if artifact_dir is not None:
                from amorehiremepls.apply.applications import remove_unused_resumes

                removed = remove_unused_resumes(self.store, artifact_dir, job_id)["removed"]
            return {"job_id": job_id, "outcome": "closed", "reason": POSTING_CLOSED_REASON, "removed_resumes": removed}
        text = (read["text"] or "").strip()
        if len(text) >= MIN_POSTING_CHARS:
            old = job.get("latest_snapshot") or {}
            snapshot = make_snapshot(url, text, raw_html=read["html"], extracted=dict(old.get("extracted") or {}), source=read["source"] or "url_refresh")
            self.jobs.set_status(job_id, job["status"], latest_snapshot=snapshot, posting_checked_at=now)
        else:
            self.jobs.set_status(job_id, job["status"], posting_checked_at=now)
        self._note_posting(job_id, "This posting is still up", now)
        return {"job_id": job_id, "outcome": "open", "reason": "still posted", "chars": len(text)}

    def check_open_postings(self, *, client: httpx.Client | None = None, artifact_dir: Path | None = None) -> dict:
        """Check active, unsent jobs one at a time, up to a batch cap. Recently checked jobs are skipped."""
        summary = {"checked": 0, "closed": 0, "open": 0, "skipped": 0, "kept_sent": 0, "unreadable": 0}
        query = {"status": {"$nin": ["closed", "applied", "applied_historical", "excluded"]}}
        for job in self.store.c("jobs").find(query).sort("updated_at", 1):
            if summary["checked"] >= POSTING_CHECK_BATCH:
                break
            if _checked_recently(job.get("posting_checked_at"), utcnow()):
                summary["skipped"] += 1
                continue
            outcome = self.check_posting(job["_id"], force=False, client=client, artifact_dir=artifact_dir)["outcome"]
            summary["checked"] += 1
            summary[outcome] = summary.get(outcome, 0) + 1
        return summary

    def qualify_job(self, job_id: str, use_model_extraction: bool = True) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        snapshot = job.get("latest_snapshot") or {}
        text = snapshot.get("text", "")
        extracted = dict(snapshot.get("extracted") or {})
        if text and use_model_extraction and self.ai is not None and not extracted.get("_model_extracted"):
            try:
                res = self.ai.run(
                    "extract_posting",
                    ExtractInput(posting_text=text[:12000], posting_url=job.get("posting_url") or ""),
                    posting_hash=snapshot.get("posting_hash"),
                    untrusted_fields=("posting_text",),
                )
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
        posting = (snapshot.get("text") or "")[:12000]
        facts = compact_facts(self.profile, posting, self.supplemental_text)
        known = _known_ids(facts)
        evidence = "\n".join(facts)
        try:
            res = self.ai.run(
                "assess_fit",
                FitInput(
                    job_title=job["title"],
                    employer=job["employer"],
                    posting_text=posting,
                    candidate_facts=facts,
                    experience_summary=compute_experience(self.profile.employment).as_dict(),
                    role_rules={"families": self.profile.preferences.role_families},
                    candidate_profession=self.profile.profession(),
                ),
                fact_version=self.profile.version,
                posting_hash=snapshot.get("posting_hash"),
                untrusted_fields=("posting_text",),
            )
        except ProviderUnavailable:
            return job
        fit = res.output
        terms = fit_terms(self.profile.preferences.fit, _evidence_skills(facts), [*self.profile.preferences.role_families, self.profile.profession()])
        grounded = [p for p in fit.get("fit_points", []) if _cites(p, known)]
        dropped = [p for p in fit.get("fit_points", []) if p not in grounded]
        gaps, gaps_dropped = _drop_supported(fit.get("genuine_gaps") or [], evidence, terms)
        missing, missing_dropped = _drop_supported(fit.get("missing_evidence") or [], evidence, terms)
        fit["fit_points"], fit["ungrounded_points_dropped"] = grounded, dropped
        fit["genuine_gaps"], fit["missing_evidence"] = gaps, missing
        fit["contradicted_by_facts"] = gaps_dropped + missing_dropped
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
                out.update(
                    salary_min=c.get("minValue"),
                    salary_max=c.get("maxValue"),
                    salary_currency=c.get("currencyCode"),
                    salary_period="annual" if c.get("interval") == "1 YEAR" else c.get("interval"),
                )
    return out
