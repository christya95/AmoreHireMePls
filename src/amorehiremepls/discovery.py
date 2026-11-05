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
from pathlib import Path
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

CONTRACT_TERMS = ("contract", "contractor", "temporary", "temp ", "fixed term", "fixed-term", "internship", "intern ", "co-op", "coop", "freelance", "part-time", "part time")
PERMANENT_TERMS = ("permanent", "full-time", "full time", "fulltime", "regular")
ONTARIO_TERMS = (
    "ontario",
    "toronto",
    "gta",
    "mississauga",
    "milton",
    "oakville",
    "markham",
    "vaughan",
    "brampton",
    "burlington",
    "hamilton",
    "waterloo",
    "kitchener",
    "ottawa",
    "canada",
)
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

    # work arrangement: only a review reason when the posting states a kind Joss has not accepted
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
            reasons.append(f"salary in {cur}; confirm CAD base")
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

    if prefs.small_startup_rule == "needs_review" and any(s in text_low for s in STARTUP_SIGNALS):
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
    "backend",
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
# Words that make a gap line about a technology. A line that names none of these is never dropped.
_TECH_WORD = re.compile(
    r"\b(?:java|kotlin|scala|python|golang|typescript|javascript|node(?:\.js)?|spring|hibernate|rest(?:ful)?|apis?|graphql|grpc|microservices?|"
    r"databases?|sql|nosql|postgres(?:ql)?|mysql|oracle|mongodb|redis|dynamodb|kafka|rabbitmq|docker|kubernetes|k8s|containers?|"
    r"aws|azure|gcp|cloud|ci/?cd|jenkins|junit|pytest|test(?:s|ing)?|git|linux|terraform|openshift)\b",
    re.I,
)
# Category words on a gap line. Any member of the category in the evidence answers the category word; a member named
# on the line (Azure, MongoDB, JUnit) must appear in the evidence itself.
_CATEGORIES: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = (
    (re.compile(r"APIs?|REST(?:ful)?", re.I), re.compile(r"\bAPIs?\b|\bREST(?:ful)?\b", re.I)),
    (re.compile(r"databases?", re.I), re.compile(r"\bdatabases?\b|\bSQL\b|\bPostgreSQL\b|\bPostgres\b|\bMySQL\b|\bOracle\b|\bMongoDB\b|\bRedis\b|\bDynamoDB\b", re.I)),
    (re.compile(r"cloud", re.I), re.compile(r"\bcloud\b|\bAWS\b|\bAzure\b|\bGCP\b", re.I)),
    (re.compile(r"containers?|containeri[sz]ation", re.I), re.compile(r"\bcontainers?\b|\bdocker\b|\bkubernetes\b|\bk8s\b", re.I)),
    (re.compile(r"CI|CD|CI/?CD", re.I), re.compile(r"\bCI/?CD\b|\bCI\b|\bGitHub Actions\b|\bJenkins\b|\bGitLab\b|\bpipelines?\b", re.I)),
    (re.compile(r"test(?:s|ing)?", re.I), re.compile(r"\btest(?:s|ing)?\b|\bJUnit\b|\bpytest\b|\bTDD\b", re.I)),
)
# A line about any of these is never dropped by a technology match, whatever the evidence says.
_ALWAYS_KEEP = re.compile(
    r"\bsenior(?:ity)?\b|\bstaff\b|\bprincipal\b|\blead(?:s|ing|er|ership)?\b|\bled\b|\bmentor\w*|\bmanag\w*|\bownership\b|\bown(?:ed|s)?\b|"
    r"\barchitect\w*|\bdesign authority\b|\bon[- ]?call\b|\bproduction support\b|\bincident\w*|\bcertif\w*|\bdegree\b|\bbachelor\w*|\bmaster'?s?\b|"
    r"\byears?\b|\byrs\b|\bmonths?\b|\bBMO\b",
    re.I,
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.]*")


def _clip(text: str, limit: int) -> str:
    text = _EMAIL_RE.sub("", text or "")
    text = _PHONE_RE.sub("", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def _carry_name(title: str, employer: str) -> str:
    from .tailoring import DATE_RE

    parts: list[str] = []
    for part in (title, employer):
        cleaned = DATE_RE.sub("", part or "")
        cleaned = " ".join(cleaned.split()).strip(" |,-–—")
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    return ", ".join(parts)


def _stitch_roles(experiences: list) -> list:
    """Bullets that the parser hung on the employer line belong to the dated role above them."""
    from .tailoring import DATE_RE

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
    from .tailoring import DATE_RE

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
        from .tailoring import parse_base_resume

        parsed = parse_base_resume(path)
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


def line_already_supported(line: str, evidence: str) -> bool:
    """True when a gap line is only about technologies and every word that carries meaning is already in the evidence.

    Conservative by design: a doubtful line stays a gap. Lines about seniority, leadership, architecture, on-call,
    certifications, education, durations, or BMO are never dropped. A line without a known technology word is never
    dropped. Every other word on the line, including numbers ("Java 21") and short names ("C#"), must itself appear in
    the evidence; a capitalised word must match case ("Batch" in "Spring Batch"). Only a category word (databases,
    APIs, cloud, containers, CI/CD, testing) is answered by a member of that category (PostgreSQL, REST, AWS, Docker).
    """
    if _ALWAYS_KEEP.search(line) or not _TECH_WORD.search(line):
        return False
    checked = False
    for index, raw in enumerate(_TOKEN_RE.findall(line)):
        word = raw.strip(".")
        if index == 0 and word[:1].isupper() and word[1:].islower():
            word = word.lower()  # sentence-start capitalisation ("No", "Lacks"), not a name or an acronym
        if not word:
            continue
        category = next((proof for member, proof in _CATEGORIES if member.fullmatch(word)), None)
        if category is not None:
            checked = True
            if category.search(evidence):
                continue
            return False
        if word.lower() in _EVIDENCE_STOP or (len(word) < 3 and word.isalpha() and word.islower()):
            continue
        checked = True
        flags = 0 if any(ch.isupper() for ch in word) else re.I
        if not _word_in(word, evidence, flags):
            return False
    return checked


def _drop_supported(lines: list, evidence: str) -> tuple[list, list]:
    kept, dropped = [], []
    for line in lines:
        if isinstance(line, str) and line_already_supported(line, evidence):
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
            self.jobs.set_status(job["_id"], job.get("status", "new"), apply_route="linkedin_or_employer_site_handoff")
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
                ),
                fact_version=self.profile.version,
                posting_hash=snapshot.get("posting_hash"),
                untrusted_fields=("posting_text",),
            )
        except ProviderUnavailable:
            return job
        fit = res.output
        grounded = [p for p in fit.get("fit_points", []) if _cites(p, known)]
        dropped = [p for p in fit.get("fit_points", []) if p not in grounded]
        gaps, gaps_dropped = _drop_supported(fit.get("genuine_gaps") or [], evidence)
        missing, missing_dropped = _drop_supported(fit.get("missing_evidence") or [], evidence)
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
