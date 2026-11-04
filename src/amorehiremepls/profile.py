"""Private candidate profile: facts with provenance, employment dates, preferences, exclusions,
compensation rules, base resume. Stored in MongoDB (`profile`, key=candidate); never in this repo.

Experience is computed from dates in code. Internships are never counted as full-time years.
Unknown values stay unknown. Exclusions are rules (employer aliases, resolved identities), not
first-name matches or invented thresholds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .db import Store
from .util import new_id, normalize_employer, sha256_file, utcnow

Kind = Literal["full_time", "internship", "contract", "unknown"]


class Provenance(BaseModel):
    source: str  # e.g. "skill:joss-application-facts", "user:dashboard", "tracker:import"
    confirmed_on: str | None = None  # YYYY-MM-DD
    note: str = ""


class Fact(BaseModel):
    id: str = Field(default_factory=lambda: new_id("fact"))
    category: str  # identity | contact | address | eligibility | education | answer | ai_workflow | other
    key: str
    value: str | None = None  # None = unknown
    sensitivity: str = "personal"  # personal | sensitive (never auto-filled)
    provenance: Provenance


class Employment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("emp"))
    employer: str
    title: str
    kind: Kind
    start: str | None  # YYYY-MM
    end: str | None  # YYYY-MM or None for present/unknown; see end_known
    end_known: bool = True
    provenance: Provenance


class Skill(BaseModel):
    name: str
    evidence: list[str] = Field(default_factory=list)  # employment/project ids or fact ids
    provenance: Provenance


class ExcludedEmployer(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    reason: str = ""
    applies_to: list[str] = Field(default_factory=lambda: ["applications", "outreach"])


class RecipientExclusion(BaseModel):
    """Identity-based exclusion. `unresolved` entries block nothing until bound to an identity."""

    id: str = Field(default_factory=lambda: new_id("rex"))
    label: str
    resolution: Literal["unresolved", "resolved"] = "unresolved"
    profile_url: str | None = None
    email: str | None = None
    employer: str | None = None
    reason: str = ""
    provenance: Provenance


class Compensation(BaseModel):
    currency: str = "CAD"
    floor_base: int = 0  # minimum annual base to apply; 0 = unknown
    target_base: int | None = None  # personal preference, never a form default
    band_rule: Literal["midpoint", "upper_half", "minimum"] = "midpoint"
    unknown_rule: Literal["needs_input", "leave_blank"] = "needs_input"
    provenance: Provenance


class Preferences(BaseModel):
    employment_types: list[str] = Field(default_factory=lambda: ["permanent"])
    role_families: list[str] = Field(default_factory=list)  # allowed title families (lowercase keywords)
    excluded_role_families: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)  # e.g. "Toronto", "GTA", "Ontario", "Canada remote"
    remote_rule: str = "remote must be open to Ontario residents"
    excluded_employers: list[ExcludedEmployer] = Field(default_factory=list)
    recipient_exclusions: list[RecipientExclusion] = Field(default_factory=list)
    small_startup_rule: Literal["needs_review", "ignore"] = "needs_review"
    work_authorization: str | None = None
    outreach_rules: dict[str, Any] = Field(default_factory=dict)


class BaseResume(BaseModel):
    path: str
    sha256: str
    approved_on: str
    source: str
    note: str = ""


class Profile(BaseModel):
    key: str = "candidate"
    version: int = 1
    facts: list[Fact] = Field(default_factory=list)
    employment: list[Employment] = Field(default_factory=list)
    education: list[Fact] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    answers: list[Fact] = Field(default_factory=list)
    compensation: Compensation = Field(default_factory=lambda: Compensation(provenance=Provenance(source="default")))
    preferences: Preferences = Field(default_factory=Preferences)
    base_resume: BaseResume | None = None
    unknowns: list[str] = Field(default_factory=list)

    # -- lookups -------------------------------------------------------------
    def fact(self, key: str) -> Fact | None:
        for group in (self.facts, self.answers, self.education):
            for f in group:
                if f.key == key:
                    return f
        return None

    def fact_value(self, key: str) -> str | None:
        f = self.fact(key)
        return None if f is None else f.value

    def is_excluded_employer(self, employer: str) -> ExcludedEmployer | None:
        n = normalize_employer(employer)
        for ex in self.preferences.excluded_employers:
            names = [ex.name, *ex.aliases]
            if any(normalize_employer(a) == n for a in names):
                return ex
        return None

    def missing_essentials(self) -> list[str]:
        missing = []
        for key in ("legal_first_name", "legal_last_name", "email", "phone", "city", "work_authorization"):
            if not self.fact_value(key) and not (key == "work_authorization" and self.preferences.work_authorization):
                missing.append(key)
        if self.base_resume is None:
            missing.append("base_resume")
        return missing


# -- experience ------------------------------------------------------------------


@dataclass
class ExperienceSummary:
    full_time_years_completed: int
    full_time_months: int
    internship_months: int
    total_months: int
    as_of: date
    unknown_ranges: list[str]

    @property
    def total_years_completed(self) -> int:
        return self.total_months // 12

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_time_years_completed": self.full_time_years_completed,
            "full_time_months": self.full_time_months,
            "internship_months": self.internship_months,
            "total_months": self.total_months,
            "total_years_completed": self.total_years_completed,
            "as_of": self.as_of.isoformat(),
            "unknown_ranges": self.unknown_ranges,
        }


def _ym(value: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not m:
        raise ValueError(f"bad YYYY-MM: {value}")
    return int(m.group(1)), int(m.group(2))


def months_between(start: str, end: str) -> int:
    (y1, m1), (y2, m2) = _ym(start), _ym(end)
    return max(0, (y2 - y1) * 12 + (m2 - m1))


def compute_experience(employment: list[Employment], as_of: date | None = None) -> ExperienceSummary:
    as_of = as_of or date.today()
    today_ym = f"{as_of.year:04d}-{as_of.month:02d}"
    ft = intern = total = 0
    unknown = []
    for e in employment:
        if not e.start:
            unknown.append(f"{e.employer}: start unknown")
            continue
        end = e.end if (e.end and e.end_known) else (today_ym if e.end is None and e.end_known else None)
        if end is None:
            unknown.append(f"{e.employer}: end unknown")
            continue
        months = months_between(e.start, min(end, today_ym))
        total += months
        if e.kind == "full_time":
            ft += months
        elif e.kind == "internship":
            intern += months
    return ExperienceSummary(ft // 12, ft, intern, total, as_of, unknown)


# -- compensation rule ----------------------------------------------------------


class BandEvidence(BaseModel):
    min_base: int | None = None
    max_base: int | None = None
    currency: str | None = None
    period: str = "annual"  # annual | hourly | unknown
    title_matches: bool = False
    location_matches: bool = False
    source_url: str | None = None
    is_total_comp_or_ote: bool = False


class SalaryDecision(BaseModel):
    outcome: Literal["answer", "needs_input", "below_floor", "leave_blank"]
    amount: int | None = None
    reason: str


def desired_salary(comp: Compensation, band: BandEvidence | None, fit: Literal["fair", "strong", "stretch", "low_end"] = "fair") -> SalaryDecision:
    """Pick one number inside this role's published base band, or report that input is needed."""
    if band is None or band.min_base is None or band.max_base is None:
        return SalaryDecision(outcome="needs_input" if comp.unknown_rule == "needs_input" else "leave_blank",
                              reason="no published base band for this requisition")
    if band.is_total_comp_or_ote:
        return SalaryDecision(outcome="needs_input", reason="band is total compensation or OTE, not base")
    if (band.currency or "").upper() != comp.currency or band.period != "annual":
        return SalaryDecision(outcome="needs_input", reason="band is not annual base in the profile currency")
    if not (band.title_matches and band.location_matches):
        return SalaryDecision(outcome="needs_input", reason="band is for a different title or location")
    if comp.floor_base and band.max_base < comp.floor_base:
        return SalaryDecision(outcome="below_floor", reason=f"band max {band.max_base} is below floor {comp.floor_base}")
    lo, hi = band.min_base, band.max_base
    if fit in ("stretch", "low_end"):
        amount = lo
    elif fit == "strong" or comp.band_rule == "upper_half":
        amount = lo + (hi - lo) * 3 // 4
    else:
        amount = (lo + hi) // 2
    amount = round(amount / 1000) * 1000 if hi - lo >= 5000 else amount
    amount = max(lo, min(hi, amount))
    return SalaryDecision(outcome="answer", amount=amount, reason=f"{fit} match; inside published band {lo}-{hi} {comp.currency}")


# -- persistence -------------------------------------------------------------------


class ProfileStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def load(self) -> Profile:
        doc = self.store.c("profile").find_one({"key": "candidate"}, {"_id": 0})
        return Profile.model_validate(doc) if doc else Profile()

    def save(self, profile: Profile, actor: str = "system") -> Profile:
        profile.version += 1
        self.store.c("profile").replace_one({"key": "candidate"}, {**profile.model_dump(), "updated_at": utcnow()}, upsert=True)
        self.store.audit("profile_saved", "candidate", actor=actor, version=profile.version)
        return profile

    def select_base_resume(self, path: Path, approved_on: str, source: str, private_dir: Path, note: str = "") -> Profile:
        """Copy the approved DOCX into protected storage and record its hash. The base is never modified."""
        if path.suffix.lower() != ".docx":
            raise ValueError("base resume must be a DOCX")
        private_dir.mkdir(parents=True, exist_ok=True)
        target = private_dir / "base_resume" / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        profile = self.load()
        profile.base_resume = BaseResume(path=str(target), sha256=sha256_file(target), approved_on=approved_on, source=source, note=note)
        return self.save(profile, actor="user")


# -- import from private skill files ----------------------------------------------------

_BULLET = re.compile(r"^-\s*(.+?):\s*(.+?)\s*$")


def parse_facts_skill(text: str, confirmed_on: str | None = None) -> list[Fact]:
    """Parse the private `joss-application-facts` SKILL.md bullets into facts with provenance."""
    prov = Provenance(source="skill:joss-application-facts", confirmed_on=confirmed_on)
    mapping = {
        "legal first name": ("identity", "legal_first_name"),
        "legal last name": ("identity", "legal_last_name"),
        "resume / professional display name": ("identity", "display_name"),
        "address line 1 is always": ("address", "address_line1"),
        "city": ("address", "city"),
        "province": ("address", "province"),
        "country": ("address", "country"),
        "email": ("contact", "email"),
        "phone": ("contact", "phone"),
    }
    facts: list[Fact] = []
    for line in text.splitlines():
        m = _BULLET.match(line.strip())
        if not m:
            low = line.lower()
            if "preferred name is always" in low:
                val = re.search(r"always\s+(\w+)", line)
                if val:
                    facts.append(Fact(category="identity", key="preferred_name", value=val.group(1), provenance=prov))
            elif "postal code is always" in low:
                val = re.search(r"always\s+([A-Z]\d[A-Z] ?\d[A-Z]\d)", line)
                if val:
                    facts.append(Fact(category="address", key="postal_code", value=val.group(1), provenance=prov))
            continue
        label, value = m.group(1).strip().lower(), m.group(2).strip()
        if label in mapping:
            cat, key = mapping[label]
            value = re.sub(r"\s*\(.*\)$", "", value)
            facts.append(Fact(category=cat, key=key, value=value, provenance=prov))
    return facts


def parse_salary_skill(text: str, confirmed_on: str | None = None) -> Compensation:
    prov = Provenance(source="skill:joss-desired-salary", confirmed_on=confirmed_on)
    floor = re.search(r"floor remains CAD\s*([\d,]+)", text)
    target = re.search(r"target CAD\s*([\d,]+)", text)
    return Compensation(
        currency="CAD",
        floor_base=int(floor.group(1).replace(",", "")) if floor else 0,
        target_base=int(target.group(1).replace(",", "")) if target else None,
        band_rule="midpoint",
        unknown_rule="needs_input",
        provenance=prov,
    )


def default_preferences(confirmed_on: str = "2026-10-15") -> Preferences:
    """Saved preferences reconciled from apply-joss-jobs / joss-linkedin-outreach and the user's rules."""
    prov = Provenance(source="skill:apply-joss-jobs;user-rules", confirmed_on=confirmed_on)
    faang = [
        ExcludedEmployer(name=n, aliases=a, reason="FAANG exclusion")
        for n, a in (("Meta", ["Facebook", "Meta Platforms"]), ("Apple", []), ("Amazon", ["AWS", "Amazon Web Services"]),
                     ("Netflix", []), ("Google", ["Alphabet"]))
    ]
    rbc = ExcludedEmployer(name="RBC", aliases=["Royal Bank of Canada", "RBC Capital Markets"], reason="former employer; excluded for outreach and applications per saved filters")
    return Preferences(
        employment_types=["permanent"],
        role_families=["backend", "back end", "back-end", "software developer", "software engineer", "java developer",
                       "python developer", "api developer", "platform engineer", "ai engineer", "senior ai engineer"],
        excluded_role_families=["frontend", "front end", "mobile", "ios", "android", "data scientist", "research scientist",
                                "machine learning scientist", "qa", "test engineer", "sre", "devops", "site reliability",
                                "manager", "director", "staff", "principal", "architect", "intern", "co-op"],
        locations=["Toronto", "GTA", "Mississauga", "Milton", "Oakville", "Markham", "Vaughan", "Ontario", "Canada"],
        remote_rule="remote must be explicitly open to Ontario residents",
        excluded_employers=[rbc, *faang],
        recipient_exclusions=[RecipientExclusion(label="Jessica", resolution="unresolved", reason="saved rule 'skip Jessica'; identity not yet bound", provenance=prov)],
        small_startup_rule="needs_review",
        work_authorization="Authorized to work in Canada without sponsorship now or in the future",
        outreach_rules={"connect_note_max_chars": 300, "no_dashes": True, "no_layoff_mention": True,
                        "mention_applied_role": True, "ask_other_permanent_backend_roles": True,
                        "message_words": [45, 75], "draft_by_default": True},
    )


def default_employment(confirmed_on: str = "2026-10-15") -> list[Employment]:
    prov = Provenance(source="skill:apply-joss-jobs", confirmed_on=confirmed_on, note="month/year dates from skill; internships need dates from the base resume")
    return [Employment(employer="RBC", title="Software Developer", kind="full_time", start="2023-07", end="2026-10", provenance=prov)]


def default_answers(confirmed_on: str = "2026-10-15") -> list[Fact]:
    prov = Provenance(source="skill:apply-joss-jobs", confirmed_on=confirmed_on)
    return [
        Fact(category="answer", key="work_authorization", value="Authorized to work in Canada without sponsorship now or in the future", provenance=prov),
        Fact(category="answer", key="requires_sponsorship", value="No", provenance=prov),
        Fact(category="answer", key="ai_workflow", value="Copilot and Devin; approximately 95% AI-generated code reviewed against acceptance criteria and functional requirements", provenance=prov),
        Fact(category="education", key="degree_level", value="Associate's Degree / Honours Associates", provenance=prov),
        Fact(category="education", key="school", value="Sheridan College", provenance=prov),
        Fact(category="answer", key="employment_preference", value="Permanent", provenance=prov),
    ]


def build_profile_from_skills(facts_text: str | None, salary_text: str | None, confirmed_on: str = "2026-10-15") -> Profile:
    profile = Profile()
    if facts_text:
        profile.facts = parse_facts_skill(facts_text, confirmed_on)
    if salary_text:
        profile.compensation = parse_salary_skill(salary_text, confirmed_on)
    profile.preferences = default_preferences(confirmed_on)
    profile.employment = default_employment(confirmed_on)
    profile.answers = default_answers(confirmed_on)
    profile.unknowns = ["internship employers/dates (read from the approved base resume once selected)",
                        "education dates and GPA (never invented)", "base resume DOCX not selected"]
    return profile
