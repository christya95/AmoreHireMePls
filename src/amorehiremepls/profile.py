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
        return SalaryDecision(outcome="needs_input" if comp.unknown_rule == "needs_input" else "leave_blank", reason="no published base band for this requisition")
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
        sha = sha256_file(target)
        profile.base_resume = BaseResume(path=str(target), sha256=sha, approved_on=approved_on, source=source, note=note)
        # Employment titles/internships come from the approved base itself (evidence), merged under the
        # user-confirmed skill dates. Parsing failures leave the profile unchanged rather than guessing.
        try:
            from .tailoring import parse_base_resume

            parsed = parse_base_resume(target)
            entries = [{"employer": e.employer, "title": e.title, "dates": e.dates, "header": e.header} for e in parsed.experience]
            profile.employment = merge_employment(profile.employment, employment_from_base_resume(entries, approved_on, sha))
            profile.unknowns = [u for u in profile.unknowns if "base resume" not in u]
        except Exception as exc:  # pragma: no cover - depends on the real document layout
            profile.unknowns.append(f"base resume experience not parsed ({type(exc).__name__}); employment unchanged")
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
    faang = [
        ExcludedEmployer(name=n, aliases=a, reason="FAANG exclusion")
        for n, a in (("Meta", ["Facebook", "Meta Platforms"]), ("Apple", []), ("Amazon", ["AWS", "Amazon Web Services"]), ("Netflix", []), ("Google", ["Alphabet"]))
    ]
    rbc = ExcludedEmployer(name="RBC", aliases=["Royal Bank of Canada", "RBC Capital Markets"], reason="saved rule: excluded for outreach and applications")
    return Preferences(
        employment_types=["permanent"],
        role_families=[
            "backend",
            "back end",
            "back-end",
            "software developer",
            "software engineer",
            "java developer",
            "python developer",
            "api developer",
            "platform engineer",
            "ai engineer",
            "senior ai engineer",
        ],
        excluded_role_families=[
            "frontend",
            "front end",
            "mobile",
            "ios",
            "android",
            "data scientist",
            "research scientist",
            "machine learning scientist",
            "qa",
            "test engineer",
            "sre",
            "devops",
            "site reliability",
            "manager",
            "director",
            "staff",
            "principal",
            "architect",
            "intern",
            "co-op",
        ],
        locations=["Toronto", "GTA", "Mississauga", "Milton", "Oakville", "Markham", "Vaughan", "Ontario", "Canada"],
        remote_rule="remote must be explicitly open to Ontario residents",
        excluded_employers=[rbc, *faang],
        # No named recipient exclusions by default (Joss, 2026-11-04): prior-contact checks are handled by the
        # first-contact gate, which fails closed until contact history is imported or confirmed per recipient.
        recipient_exclusions=[],
        small_startup_rule="needs_review",
        work_authorization="Authorized to work in Canada without sponsorship now or in the future",
        outreach_rules={
            "connect_note_max_chars": 300,
            "no_dashes": True,
            "no_layoff_mention": True,
            "mention_applied_role": True,
            "ask_other_permanent_backend_roles": True,
            "message_words": [45, 75],
            "draft_by_default": True,
        },
    )


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def _to_ym(text: str) -> str | None:
    """'May 2021' / 'May 2021' / '2021-05' / '05/2021' -> '2021-05'. Returns None when not a month/year."""
    t = text.strip().lower()
    m = re.match(r"([a-z]+)\.?\s+(\d{4})$", t)
    if m:
        mon = next((v for k, v in _MONTHS.items() if k.startswith(m.group(1)[:3])), None)
        return f"{m.group(2)}-{mon:02d}" if mon else None
    m = re.match(r"(\d{4})-(\d{2})$", t) or re.match(r"(\d{2})/(\d{4})$", t)
    if m:
        y, mo = (m.group(1), m.group(2)) if len(m.group(1)) == 4 else (m.group(2), m.group(1))
        return f"{y}-{int(mo):02d}"
    return None


def parse_apply_skill(text: str, confirmed_on: str | None = None) -> tuple[list[Employment], list[Fact]]:
    """Generic extraction from the private apply skill: employment dates, work authorization, degree,
    school and the confirmed AI-workflow answer. Patterns match sentence shapes, never specific values."""
    prov = Provenance(source="skill:apply-joss-jobs", confirmed_on=confirmed_on)
    employment: list[Employment] = []
    answers: list[Fact] = []
    employment_re = (
        r"(?P<employer>[A-Z][\w&.' ]{0,40}?) full[- ]time employment ended[^;.\n]*;\s*month/year dates are "
        r"(?P<start>[A-Za-z]+\.? \d{4})\s*[\u2013\u2014-]\s*(?P<end>[A-Za-z]+\.? \d{4}|present)"
    )
    m = re.search(employment_re, text)
    if m:
        start, end = _to_ym(m.group("start")), (None if m.group("end").lower() == "present" else _to_ym(m.group("end")))
        if start:
            employment.append(
                Employment(
                    employer=m.group("employer").strip(),
                    title="(title from the approved base resume)",
                    kind="full_time",
                    start=start,
                    end=end,
                    end_known=True,
                    provenance=Provenance(
                        source="skill:apply-joss-jobs", confirmed_on=confirmed_on, note="month/year dates from the skill; title/internships come from the base resume"
                    ),
                )
            )
    m = re.search(r"^[-*]?\s*(Authorized to work in [^.\n]+?(?:now or in the future)?)\.?$", text, re.M | re.I)
    if m:
        answers.append(Fact(category="answer", key="work_authorization", value=m.group(1).strip(), provenance=prov))
        answers.append(Fact(category="answer", key="requires_sponsorship", value="No" if "without sponsorship" in m.group(1).lower() else None, provenance=prov))
    m = re.search(r"Degree on forms is ([^.\n]+?)\.", text)
    if m:
        answers.append(Fact(category="education", key="degree_level", value=m.group(1).strip(), provenance=prov))
    m = re.search(r"cannot find ([A-Z][\w' ]+?(?:College|University|Institute|Polytechnic|School))", text)
    if m:
        answers.append(Fact(category="education", key="school", value=m.group(1).strip(), provenance=prov))
    m = re.search(r"asked about AI workflow, use the confirmed answer:\s*([^\n]+?)\.(?:\s|$)", text)
    if m:
        answers.append(Fact(category="answer", key="ai_workflow", value=m.group(1).strip(), provenance=prov))
    if re.search(r"^\s*[-*]\s*Permanent\b", text, re.M | re.I):
        answers.append(Fact(category="answer", key="employment_preference", value="Permanent", provenance=prov))
    return employment, answers


def apply_private_overrides(profile: Profile, data: dict, confirmed_on: str | None = None) -> Profile:
    """Merge a private JSON document (kept under runtime/, never in git) with keys `employment`, `education`,
    `answers`, `skills`. Each entry may carry its own provenance; otherwise the file is the source."""
    prov = Provenance(source="private:profile_overrides.json", confirmed_on=confirmed_on)
    for e in data.get("employment", []):
        emp = Employment.model_validate({"provenance": prov.model_dump(), **e})
        profile.employment = [x for x in profile.employment if not (x.employer == emp.employer and x.start == emp.start)] + [emp]
    for cat in ("education", "answers"):
        for f in data.get(cat, []):
            fact = Fact.model_validate({"category": "education" if cat == "education" else "answer", "provenance": prov.model_dump(), **f})
            target = profile.education if cat == "education" else profile.answers
            target[:] = [x for x in target if x.key != fact.key] + [fact]
    for sname in data.get("skills", []):
        if isinstance(sname, str):
            profile.skills = [x for x in profile.skills if x.name != sname] + [Skill(name=sname, provenance=prov)]
    return profile


def employment_from_base_resume(resume_text_entries: list[dict], confirmed_on: str | None, base_sha256: str) -> list[Employment]:
    """Build employment rows from the approved base resume's experience headers (employer/title/dates).
    Internships are detected from the title text. Unparseable dates stay unknown instead of being guessed."""
    prov = Provenance(source=f"base_resume:{base_sha256[:12]}", confirmed_on=confirmed_on)
    out: list[Employment] = []
    for e in resume_text_entries:
        dates = e.get("dates", "")
        parts = re.split(r"\s*[\u2013\u2014-]\s*|\s+to\s+", dates, maxsplit=1)
        start = _to_ym(parts[0]) if parts else None
        end_raw = parts[1].strip().lower() if len(parts) > 1 else ""
        present = end_raw in ("present", "current", "now")
        end = None if present or not end_raw else _to_ym(end_raw)
        kind = "internship" if re.search(r"intern|co-?op", (e.get("title") or "") + " " + (e.get("header") or ""), re.I) else "full_time"
        if not start:
            continue
        out.append(
            Employment(
                employer=e.get("employer", "").strip() or "(see base resume)",
                title=e.get("title", "").strip() or "(see base resume)",
                kind=kind,
                start=start,
                end=end,
                end_known=present or end is not None,
                provenance=prov,
            )
        )
    return out


def merge_employment(primary: list[Employment], secondary: list[Employment]) -> list[Employment]:
    """Primary rows (user-confirmed skill dates) win on employer+kind; secondary adds internships/titles."""
    merged = list(primary)
    for s in secondary:
        match = next((p for p in merged if normalize_employer(p.employer) == normalize_employer(s.employer) and p.kind == s.kind), None)
        if match:
            if match.title.startswith("("):
                match.title = s.title
            continue
        merged.append(s)
    return merged


def build_profile_from_skills(
    facts_text: str | None, salary_text: str | None, confirmed_on: str = "2026-10-15", apply_text: str | None = None, overrides: dict | None = None
) -> Profile:
    """Private sources -> Profile. Nothing personal is defaulted in code: employment, education and answers
    come from the apply skill text, the private overrides JSON, or the approved base resume."""
    profile = Profile()
    if facts_text:
        profile.facts = parse_facts_skill(facts_text, confirmed_on)
    if salary_text:
        profile.compensation = parse_salary_skill(salary_text, confirmed_on)
    profile.preferences = default_preferences(confirmed_on)
    if apply_text:
        employment, answers = parse_apply_skill(apply_text, confirmed_on)
        profile.employment = employment
        profile.answers = [a for a in answers if a.category == "answer"]
        profile.education = [a for a in answers if a.category == "education"]
    if overrides:
        apply_private_overrides(profile, overrides, confirmed_on)
    profile.unknowns = [
        u
        for u in [
            None if profile.employment else "employment history (import the apply skill, a private overrides JSON, or select the base resume)",
            "internship employers/dates (read from the approved base resume once selected)" if not any(e.kind == "internship" for e in profile.employment) else None,
            "education dates and GPA (never invented)",
            "base resume DOCX not selected",
        ]
        if u
    ]
    return profile
