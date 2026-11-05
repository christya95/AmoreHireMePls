"""Application preparation and execution.

prepare(job)  -> tailored artifact + answers from confirmed facts -> review package (+ material hash)
execute(app)  -> idempotent action, authorization check, site adapter plan, verified receipt or
                 needs_verification / failed / handoff.  Never marks `submitted` without evidence.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .actions import ActionHandoff, ActionStore, Outcome
from .ats.rules import SENSITIVE_BLANK_REASON, YEARS_RULE
from .authorization import material_hash
from .browser import Plan, normalize_screenshots
from .db import Store
from .jobs import JobStore
from .profile import BandEvidence, Profile, compute_experience, desired_salary
from .tailoring import Tailor, page_limit_block_reason
from .util import local_zone, new_id, sha256_file, to_local, utcnow

log = logging.getLogger(__name__)

SENSITIVE = re.compile(r"(gender|race|ethnic|disab|veteran|sexual orientation|religion|pronoun|date of birth|age\b|marital|citizenship status|criminal)", re.I)
FIELD_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bfirst\s*name|given name", re.I), "legal_first_name"),
    (re.compile(r"\blast\s*name|surname|family name", re.I), "legal_last_name"),
    (re.compile(r"preferred name|nickname", re.I), "preferred_name"),
    (re.compile(r"\be-?mail", re.I), "email"),
    (re.compile(r"country code|dial(ing)? code", re.I), "phone_country_code"),
    (re.compile(r"\bphone|mobile|telephone", re.I), "phone"),
    (re.compile(r"address line ?1|street|^address$", re.I), "address_line1"),
    (re.compile(r"\bcity\b|town", re.I), "city"),
    (re.compile(r"province|state|region", re.I), "province"),
    (re.compile(r"postal|zip", re.I), "postal_code"),
    (re.compile(r"\bcountry\b", re.I), "country"),
    (re.compile(r"linkedin", re.I), "linkedin_url"),
    (re.compile(r"github", re.I), "github_url"),
    (re.compile(r"portfolio|website|personal site|\burl\b", re.I), "website_url"),
    (re.compile(r"authoriz|eligible to work|legally (able|entitled) to work|work permit", re.I), "work_authorization"),
    (re.compile(r"sponsor", re.I), "requires_sponsorship"),
    (re.compile(r"salary|compensation|pay expectation|expected pay", re.I), "__salary__"),
    (re.compile(r"resume|cv\b|curriculum", re.I), "__resume__"),
    (re.compile(r"degree|education level|highest level", re.I), "degree_level"),
    (re.compile(r"school|university|college|institution", re.I), "school"),
    (YEARS_RULE, "__years_full_time__"),
    (re.compile(r"total years|years of experience", re.I), "__years_total__"),
    (re.compile(r"employment type|job type|looking for", re.I), "employment_preference"),
    (re.compile(r"notice period", re.I), "notice_period"),
    (re.compile(r"start date|available to start|availability|earliest start|when (can|could) you start", re.I), "availability"),
]
# A candidate's own recurring question ("Do you use AI tools?", "Are you licensed in this state?") is a fact with a
# `question_pattern`; those are matched after the built-in rules. See [[facts]] in candidate/profile.toml.
# A question for one kind of link may be answered by another saved link when the first is not on file.
LINK_FALLBACKS: dict[str, tuple[str, ...]] = {"website_url": ("website_url", "portfolio_url", "github_url"), "portfolio_url": ("portfolio_url", "website_url", "github_url")}
YES_WORDS = ("yes", "authorized", "authorised", "eligible", "i am", "no sponsorship", "do not require")
NO_WORDS = ("no", "not ", "require sponsorship", "need sponsorship")

# -- self-identification (EEOC / employment equity) ----------------------------------------------------------------
# Only the answers the candidate gave explicitly, matched to the option text of the form's own question. Deterministic: when no
# option clearly carries the saved answer, the question keeps the explicit decline (or stays blank). Veteran status,
# Indigenous identity, pronouns, religion, age and the like have no saved fact and are never inferred.
SELF_ID_REASON = "from your self-identification answers"
_NEGATED = re.compile(r"\b(no|not|don't|do not|never|without|non)\b", re.I)


def _opt_has(option: str, *phrases: str) -> bool:
    low = option.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", low) for p in phrases)


def _match_gender(options: list[str], value: str) -> str | None:
    v = value.strip().lower()
    wanted = {"male": ("male", "man"), "female": ("female", "woman")}.get(v, (v,))
    return next((o for o in options if _opt_has(o, *wanted) and not _opt_has(o, "non binary", "nonbinary", "transgender", "trans")), None)


def _match_yes_no(options: list[str], value: str, topic_words: tuple[str, ...]) -> str | None:
    """'Yes' -> an affirmative option about the topic ("Yes, I have a disability"); 'No' -> a plain negative one."""
    want_yes = value.strip().lower() in ("yes", "true", "y")
    for o in options:
        low = o.strip().lower()
        if want_yes and (low.startswith("yes") or (_opt_has(o, *topic_words) and not _NEGATED.search(low) and "wish" not in low and "prefer" not in low)):
            return o
        if not want_yes and (low.startswith("no") and "not wish" not in low and "prefer" not in low):
            return o
    return None


def _match_ethnicity(options: list[str], value: str) -> str | None:
    v = value.strip().lower()
    exact = next((o for o in options if _opt_has(o, v)), None)
    if exact:
        return exact
    if "asian" in v:  # South Asian on a form that only lists Asian
        return next((o for o in options if _opt_has(o, "asian") and not _opt_has(o, "south east", "southeast", "east asian", "west asian")), None)
    return None


def _match_orientation(options: list[str], value: str) -> str | None:
    v = value.strip().lower()
    if "hetero" in v or "straight" in v:
        return next((o for o in options if _opt_has(o, "heterosexual", "straight")), None)
    return next((o for o in options if _opt_has(o, v)), None)


# (question pattern, fact keys tried in order, matcher(options, fact value) -> option)
SELF_ID_RULES: list[tuple[re.Pattern, tuple[str, ...], Callable[[list[str], str], str | None]]] = [
    (re.compile(r"sexual orientation", re.I), ("sexual_orientation",), _match_orientation),
    (re.compile(r"disab", re.I), ("disability_status",), lambda opts, v: _match_yes_no(opts, v, ("disability", "disabled"))),
    # Canadian employment equity: a visible minority question may list groups (use the ethnicity fact) or ask yes/no
    (re.compile(r"visible minority|racialized|racialised", re.I), ("ethnicity",), _match_ethnicity),
    (re.compile(r"visible minority|racialized|racialised", re.I), ("visible_minority",), lambda opts, v: _match_yes_no(opts, v, ("visible minority", "racialized", "racialised"))),
    (re.compile(r"\brace\b|racial|ethnic", re.I), ("ethnicity",), _match_ethnicity),
    (re.compile(r"\bgender\b|\bsex\b", re.I), ("gender",), _match_gender),
]


def self_identification_answer(field: FormField, profile: Profile, decline: str | None = None) -> Answer | None:
    """The option on this self-identification question that carries a saved sensitive fact, or None.

    Returned answers are flagged sensitive with a fixed reason; they stay out of model inputs, logs and audit data and
    appear only in the review package. A question without options (free text) is never answered this way."""
    label = field.label or field.id
    if not field.options:
        return None
    for pattern, keys, matcher in SELF_ID_RULES:
        if not pattern.search(label):
            continue
        for key in keys:
            fact = profile.fact(key)
            if fact is None or not fact.value:
                continue
            opt = matcher(list(field.options), fact.value)
            if opt is None or opt == decline or _opt_has(opt, "wish", "prefer", "decline", "rather not"):
                continue
            return Answer(field.id, label, opt, [fact.id], needs_input=False, reason=SELF_ID_REASON, sensitive=True)
    return None


# -- salary research ----------------------------------------------------------------------------------------------
SALARY_RESEARCH_REASON = "proposed from salary research; approve or edit"
SALARY_RESEARCH_FACT = "research:salary"
_LEVEL_WORDS = re.compile(r"\b(junior|intermediate|senior|lead|staff|principal|associate|entry[- ]level|mid[- ]level|new grad|level \w+|II|III|IV)\b", re.I)
_YEARS_PHRASE = re.compile(r"\b\d{1,2}\s*(?:\+|-\s*\d{1,2})?\s*(?:or more\s+)?years?(?: of)?(?: \w+){0,3}? experience", re.I)


def seniority_hints(title: str, posting_text: str) -> list[str]:
    """Short, deterministic phrases about level: title level words plus 'N+ years ... experience' mentions."""
    hints: list[str] = []
    for m in _LEVEL_WORDS.finditer(title or ""):
        w = m.group(0).strip()
        if w and w.lower() not in [h.lower() for h in hints]:
            hints.append(w)
    for m in _YEARS_PHRASE.finditer(posting_text or ""):
        phrase = re.sub(r"\s+", " ", m.group(0)).strip()
        if phrase.lower() not in [h.lower() for h in hints]:
            hints.append(phrase[:60])
        if len(hints) >= 6:
            break
    return hints[:6]


@dataclass
class SalaryProposal:
    amount: int
    rationale: str
    confidence: str = "low"
    source_kinds: list[str] = field(default_factory=list)
    range_low: int | None = None
    range_high: int | None = None
    basis: str = "model_estimate"  # model_estimate | posted_range

    def detail(self) -> dict:
        return {
            "rationale": self.rationale,
            "confidence": self.confidence,
            "source_kinds": list(self.source_kinds),
            "range_low": self.range_low,
            "range_high": self.range_high,
            "basis": self.basis,
        }


class SalaryResearcher:
    """One bounded `salary_case` call per job (cached by the runner on fact version + posting hash). No web access.

    Deterministic rules run first in `map_answers`: a posted range decides without the model; a floor is always
    enforced on whatever the model proposes. Provider or budget problems return None and the question stays a
    needs_input item, exactly as before."""

    def __init__(self, ai: Any | None, profile: Profile) -> None:
        self.ai = ai
        self.profile = profile

    def propose(self, job: dict) -> SalaryProposal | None:
        comp = self.profile.compensation
        if self.ai is None or not comp.floor_base:
            return None
        from .ai import BudgetExhausted, ProviderUnavailable
        from .skills import SalaryCaseInput

        snapshot = job.get("latest_snapshot") or {}
        ext = job.get("extracted") or snapshot.get("extracted") or {}
        text = snapshot.get("text") or ""
        inputs = SalaryCaseInput(
            job_title=(job.get("title") or "")[:120],
            location=(ext.get("location") or job.get("location") or "")[:120],
            seniority_hints=seniority_hints(job.get("title") or "", text),
            currency=comp.currency,
            candidate_floor=comp.floor_base,
            candidate_target=comp.target_base,
            candidate_profession=self.profile.profession(),
        )
        try:
            res = self.ai.run(
                "salary_case", inputs, fact_version=self.profile.version, posting_hash=snapshot.get("posting_hash"), untrusted_fields=("job_title", "location", "seniority_hints")
            )
        except (ProviderUnavailable, BudgetExhausted, ValueError) as exc:
            log.info("salary case unavailable (%s)", type(exc).__name__)
            return None
        out = res.output
        proposed = int(out.get("proposed_base") or 0)
        amount = max(proposed, comp.floor_base)
        if comp.target_base and comp.target_base >= comp.floor_base and proposed < comp.floor_base:
            amount = comp.target_base  # the estimate fell under the minimum, so the floor-respecting target is the number stated
        amount = int(round(amount / 1000) * 1000)
        conf = str(out.get("confidence") or "low").lower()
        return SalaryProposal(
            amount=amount,
            rationale=str(out.get("rationale") or "")[:400],
            confidence=conf if conf in ("low", "medium", "high") else "low",
            source_kinds=[str(s)[:60] for s in (out.get("source_kinds") or [])][:5],
            range_low=out.get("range_low"),
            range_high=out.get("range_high"),
        )


SalaryCase = Callable[[], SalaryProposal | None]


@dataclass
class FormField:
    id: str
    label: str
    type: str = "text"  # text | select | checkbox | file | textarea | radio
    options: list[str] = field(default_factory=list)
    selector: str = ""
    required: bool = False
    meta: dict = field(default_factory=dict)  # ATS detail (field name, section, option ids) kept by the form adapters


@dataclass
class Answer:
    field_id: str
    label: str
    value: str | None
    fact_ids: list[str]
    needs_input: bool
    reason: str = ""
    sensitive: bool = False
    required: bool = False
    options: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)  # e.g. the salary rationale shown under the answer; never a fact value

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _pick_option(options: list[str], want_yes: bool) -> str | None:
    for o in options:
        low = o.lower()
        if want_yes and any(w in low for w in YES_WORDS) and not any(w in low for w in ("not ", "need", "require sponsorship")):
            return o
        if not want_yes and (low.strip() == "no" or any(w in low for w in ("not ", "do not"))):
            return o
    return None


def salary_answer(f: FormField, profile: Profile, band: BandEvidence | None, fit: str, salary_case: SalaryCase | None) -> Answer:
    """Expected pay: a posted base range decides in code (never under the floor); otherwise one researched proposal.

    Order: (1) a published annual base band in the profile currency -> a figure inside it, raised to the floor when the
    band straddles it; a band entirely under the floor is a needs_input item saying so. (2) No usable band -> the
    `salary_case` proposal (one bounded model estimate, floor enforced), stored as a proposed answer the owner approves or
    edits. (3) No floor or no model -> needs_input, as before."""
    label = f.label or f.id
    comp = profile.compensation
    d = desired_salary(comp, band, fit)  # type: ignore[arg-type]
    if d.outcome == "answer":
        amount = max(int(d.amount or 0), comp.floor_base or 0)
        reason = d.reason if amount == d.amount else f"{d.reason}; raised to your minimum"
        return Answer(f.id, label, str(amount), ["compensation_rule"], needs_input=False, reason=reason)
    if d.outcome == "below_floor":
        return Answer(f.id, label, None, [], needs_input=True, reason="posted range is below your minimum")
    proposal = salary_case() if (salary_case is not None and comp.floor_base) else None
    if proposal is None:
        return Answer(f.id, label, None, [], needs_input=True, reason=f"salary rule: {d.reason}")
    amount = max(int(proposal.amount), comp.floor_base)
    return Answer(f.id, label, str(amount), [SALARY_RESEARCH_FACT], needs_input=False, reason=SALARY_RESEARCH_REASON, detail=proposal.detail())


def _question_pattern_key(profile: Profile, label: str) -> str | None:
    """The key of the first candidate fact whose own `question_pattern` matches this form label. A broken pattern is
    ignored rather than failing the whole form."""
    for fact in [*profile.facts, *profile.answers, *profile.education]:
        pattern = (fact.question_pattern or "").strip()
        if not pattern or not fact.value:
            continue
        try:
            if re.search(pattern, label, re.I):
                return fact.key
        except re.error:
            log.warning("fact %s has an invalid question_pattern; ignored", fact.key)
    return None


def _link_value(profile: Profile, key: str):
    for k in LINK_FALLBACKS.get(key, (key,)):
        fact = profile.fact(k)
        if fact is not None and fact.value:
            return fact
    return profile.fact(key)


def map_answers(
    fields: list[FormField],
    profile: Profile,
    job: dict,
    resume_path: str | None,
    band: BandEvidence | None = None,
    fit: str = "fair",
    as_of: date | None = None,
    salary_case: SalaryCase | None = None,
) -> list[Answer]:
    answers: list[Answer] = []
    exp = compute_experience(profile.employment, as_of)
    for f in fields:
        label = f.label or f.id
        if SENSITIVE.search(label):
            own = self_identification_answer(f, profile)
            blank = Answer(f.id, label, None, [], needs_input=f.required, reason=SENSITIVE_BLANK_REASON, sensitive=True)
            answers.append(own or blank)
            continue
        key = next((k for pat, k in FIELD_RULES if pat.search(label)), None)
        if key is None:
            key = _question_pattern_key(profile, label)
        if key is None:
            answers.append(Answer(f.id, label, None, [], needs_input=True, reason="no confirmed fact maps to this question"))
            continue
        if key == "__resume__":
            answers.append(Answer(f.id, label, resume_path, ["artifact"], needs_input=resume_path is None, reason="" if resume_path else "no tailored resume yet"))
            continue
        if key == "__salary__":
            answers.append(salary_answer(f, profile, band, fit, salary_case))
            continue
        if key in ("__years_full_time__", "__years_total__"):
            years = exp.full_time_years_completed if key == "__years_full_time__" else exp.total_years_completed
            answers.append(
                Answer(
                    f.id,
                    label,
                    str(years),
                    [e.id for e in profile.employment],
                    needs_input=bool(exp.unknown_ranges and key == "__years_total__"),
                    reason="completed years computed from dates" + ("; some ranges unknown" if exp.unknown_ranges else ""),
                )
            )
            continue
        fact = _link_value(profile, key) if key in LINK_FALLBACKS else profile.fact(key)
        value = fact.value if fact else (profile.preferences.work_authorization if key == "work_authorization" else None)
        if key in ("work_authorization", "requires_sponsorship") and f.options:
            want_yes = key == "work_authorization"
            opt = _pick_option(f.options, want_yes)
            answers.append(Answer(f.id, label, opt, [fact.id] if fact else [], needs_input=opt is None, reason="" if opt else "no matching option"))
            continue
        if f.options and value and value not in f.options:
            match = next((o for o in f.options if o.lower() == value.lower() or value.lower() in o.lower()), None)
            if match is None:
                answers.append(Answer(f.id, label, None, [fact.id] if fact else [], needs_input=True, reason=f"option list lacks '{value}'"))
                continue
            value = match
        answers.append(Answer(f.id, label, value, [fact.id] if fact else [], needs_input=value is None, reason="" if value else f"fact '{key}' unknown"))
    by_id = {f.id: f for f in fields}
    for a in answers:
        src = by_id.get(a.field_id)
        if src is not None:
            a.required = bool(src.required)
            a.options = list(src.options)
    return answers


PlanBuilder = Callable[[dict, dict, list[dict]], Plan]


class ApplicationService:
    def __init__(
        self,
        store: Store,
        profile: Profile,
        tailor: Tailor,
        actions: ActionStore,
        plan_builders: dict[str, PlanBuilder] | None = None,
        notifier: Any | None = None,
        challenge_retry_minutes: int = 0,
        salary_research: SalaryResearcher | None = None,
    ) -> None:
        self.store = store
        self.profile = profile
        self.tailor = tailor
        self.actions = actions
        self.jobs = JobStore(store)
        self.apps = store.c("applications")
        self.plan_builders = plan_builders or {}
        self.notifier = notifier
        self.challenge_retry_minutes = challenge_retry_minutes
        self.salary_research = salary_research

    def _salary_case(self, job: dict) -> SalaryCase | None:
        """Zero-argument, run-at-most-once hook for `map_answers`; only invoked when the form actually asks about pay."""
        if self.salary_research is None:
            return None
        memo: dict[str, SalaryProposal | None] = {}

        def run() -> SalaryProposal | None:
            if "p" not in memo:
                memo["p"] = self.salary_research.propose(job)
            return memo["p"]

        return run

    def get_or_create(self, job_id: str) -> dict:
        app = self.apps.find_one({"job_id": job_id, "provenance.origin": "local"})
        if app:
            return app
        app = {
            "_id": new_id("app"),
            "job_id": job_id,
            "status": "queued",
            "provenance": {"origin": "local", "created_at": utcnow()},
            "sync": {"state": "not_applicable"},
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "history": [],
        }
        self.apps.insert_one(app)
        return app

    def _set(self, app_id: str, status: str | None = None, **fields: Any) -> dict:
        upd: dict[str, Any] = {"updated_at": utcnow(), **fields}
        if status:
            upd["status"] = status
        self.apps.update_one(
            {"_id": app_id}, {"$set": upd, "$push": {"history": {"at": utcnow(), "status": status, **{k: v for k, v in fields.items() if k in ("reason", "next_action")}}}}
        )
        return self.apps.find_one({"_id": app_id})

    def prepare(self, job_id: str, fields: list[FormField], base_resume_path: Path | None = None) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        app = self.get_or_create(job_id)
        prior = self.apps.find_one({"job_id": job_id, "status": "submitted"})
        if prior:
            return self._set(app["_id"], "duplicate", reason=f"already submitted ({prior.get('tracker_record_id') or prior['_id']})")
        if job.get("status") == "excluded":
            return self._set(app["_id"], "blocked", reason="job skipped by your rules", next_action="check the fit result")
        base = self.profile.base_resume
        base_path = base_resume_path or (Path(base.path) if base else None)
        unknowns: list[str] = []
        artifact = None
        if base_path is None or not base_path.exists():
            unknowns.append("approved base resume not selected")
        else:
            snapshot = job.get("latest_snapshot") or {}
            artifact = self.store.c("artifacts").find_one(
                {
                    "owner_id": job_id,
                    "kind": "resume_tailored",
                    "fact_version": self.profile.version,
                    "posting_hash": snapshot.get("posting_hash"),
                    "base_sha256": base.sha256 if base else None,
                    "grounding.ok": True,  # a cached ungrounded artifact is re-tailored, not reused
                    "superseded": {"$ne": True},  # regenerated documents replace older ones for new packages
                },
                sort=[("created_at", -1)],
            )
            if artifact is None:
                display = self.profile.fact_value("display_name") or "Resume"
                artifact = self.tailor.tailor_for_job(job, base_path, base.sha256 if base else "", self.profile.version, display_name=display)
            if not artifact.get("grounding", {}).get("ok"):
                unknowns.append("tailored resume failed grounding")
        ext = job.get("extracted") or (job.get("latest_snapshot") or {}).get("extracted") or {}
        band = None
        if ext.get("salary_min") and ext.get("salary_max"):
            band = BandEvidence(
                min_base=ext["salary_min"],
                max_base=ext["salary_max"],
                currency=ext.get("salary_currency"),
                period=ext.get("salary_period") or "annual",
                title_matches=True,
                location_matches=True,
                source_url=job.get("posting_url"),
                is_total_comp_or_ote=bool(ext.get("salary_is_total_comp")),
            )
        fit_rank = (job.get("fit") or {}).get("rank")
        fit = "strong" if fit_rank == 1 else "stretch" if (fit_rank or 3) >= 4 else "fair"
        # A verified PDF within the profile's page limit is the only file a package may upload. An unverified page
        # count does not fall back to the DOCX, and the package stays unapprovable until a verified PDF exists.
        block = page_limit_block_reason(artifact, self.profile.preferences.resume.max_pages) if artifact else None
        resume_file = artifact.get("pdf_path") if artifact and block is None else None
        from .ats import registry as ats_registry

        adapter = ats_registry.for_fields(fields)  # questions read through a site adapter's API are mapped by that adapter
        salary_case = self._salary_case(job)
        answers = (
            adapter.map_answers(fields, self.profile, job, resume_file, band, fit, salary_case=salary_case)
            if adapter
            else map_answers(fields, self.profile, job, resume_file, band, fit, salary_case=salary_case)
        )
        if block:
            for a in answers:
                if a.reason == "no tailored resume yet":
                    a.reason = block
        material_unknowns = [a.label for a in answers if a.needs_input and not a.sensitive]
        unknowns += [f"answer needed: {u}" for u in material_unknowns]
        unknowns += self.profile.missing_essentials()
        if block:
            unknowns.append(block)
        unknowns = sorted(set(unknowns))
        package = {
            "employer": job["employer"],
            "title": job["title"],
            "posting_url": job.get("posting_url"),
            "requisition_id": job.get("requisition_id"),
            "linkedin_job_id": job.get("linkedin_job_id"),
            "apply_url": job.get("apply_url") or job.get("posting_url"),
            "apply_route": job.get("apply_route"),
            "document": {
                "filename": artifact.get("filename"),
                "sha256": artifact.get("sha256"),
                "pdf_sha256": artifact.get("pdf_sha256"),
                "pages": artifact.get("pages"),
                "page_limit_verified": artifact.get("page_limit_verified"),
                "path": resume_file,
                "render_note": artifact.get("render_note"),
            }
            if artifact
            else None,
            "answers": [a.as_dict() for a in answers],
            "gaps": (job.get("fit") or {}).get("genuine_gaps", []),
            "qualification": job.get("qualification"),
            "outstanding": unknowns,
            "prepared_at": utcnow(),
            "unsupported_requirements_note": artifact.get("unsupported_requirements_note") if artifact else "",
        }
        mhash = material_hash(
            answers=[(a.field_id, a.value) for a in answers if not a.needs_input],
            document_hashes=[h for h in [artifact.get("sha256") if artifact else None, artifact.get("pdf_sha256") if artifact else None] if h],
            extra={"job_id": job_id, "requisition": job.get("requisition_id"), "apply_url": package["apply_url"]},
        )
        status = "needs_input" if unknowns else "ready_for_review"
        return self._set(
            app["_id"],
            status,
            review_package=package,
            material_hash=mhash,
            artifact_id=artifact["_id"] if artifact else None,
            next_action="supply missing inputs" if unknowns else "review and authorize",
        )

    def supply_inputs(self, app_id: str, values: dict[str, str]) -> dict:
        app = self.apps.find_one({"_id": app_id})
        pkg = app["review_package"]
        changed = False
        for a in pkg["answers"]:
            if a["field_id"] not in values or a.get("fact_ids") == ["artifact"]:
                continue
            new = str(values[a["field_id"]]).strip()
            old = a.get("value") or ""
            if new == old:
                continue
            changed = True
            a["detail"] = {}  # a proposal's rationale no longer describes an answer the owner typed
            if not new:
                a["value"] = None
                a["needs_input"] = bool(a.get("required"))
                a["fact_ids"] = ["user:input"]
                a["reason"] = "cleared by you" if a.get("required") else "left blank"
            else:
                a["value"], a["needs_input"], a["fact_ids"], a["reason"] = new, False, ["user:input"], ("edited by you" if old else "supplied by you")
        if not changed:
            return app  # the same answers again must not knock an approval back to "ready for review"
        kept = [u for u in pkg.get("outstanding") or [] if not str(u).startswith("answer needed:")]
        needed = [f"answer needed: {a['label']}" for a in pkg["answers"] if a.get("needs_input") and not a.get("sensitive")]
        pkg["outstanding"] = sorted(set(kept + needed))
        doc_hashes = [h for h in [(pkg.get("document") or {}).get("sha256"), (pkg.get("document") or {}).get("pdf_sha256")] if h]
        mhash = material_hash(
            answers=[(a["field_id"], a["value"]) for a in pkg["answers"] if not a["needs_input"]],
            document_hashes=doc_hashes,
            extra={"job_id": app["job_id"], "requisition": pkg.get("requisition_id"), "apply_url": pkg["apply_url"]},
        )
        status = "needs_input" if pkg["outstanding"] else "ready_for_review"
        self.store.audit("inputs_supplied", app_id, fields=list(values))
        return self._set(app_id, status, review_package=pkg, material_hash=mhash)

    def execute(self, app_id: str, worker: str, run_adapter: Callable[[Plan], dict] | None = None) -> Outcome:
        app = self.apps.find_one({"_id": app_id})
        if app is None:
            return Outcome("not_found", "unknown application")
        if app["status"] not in ("ready_for_review", "authorized", "executing", "needs_verification", "submitted", "failed"):
            return Outcome("denied", f"application is {app['status']}; not executable")
        job = self.jobs.get(app["job_id"])
        pending = self.store.c("challenge_retries").find_one({"idempotency_key": f"challenge_retry:{job['_id']}", "status": {"$in": ["scheduled", "running"]}})
        if pending and not app.get("challenge_retry_active"):
            return Outcome("retry_scheduled", "one later try is waiting")
        pkg = app["review_package"]
        if app["status"] in ("ready_for_review", "authorized"):
            block = page_limit_block_reason(pkg.get("document"), self.profile.preferences.resume.max_pages)
            if block:
                return Outcome("denied", block)
        prior = self.actions.prior_terminal_for_target("application_submit", job["_id"])
        if prior is not None and prior.get("material_hash") != app["material_hash"]:
            return Outcome(prior["status"], "an earlier submission for this job already exists; reconcile first", action_id=prior["_id"])
        host = (pkg.get("apply_url") or "").split("/")[2].lower() if pkg.get("apply_url") and "://" in pkg["apply_url"] else None
        builder = self._builder_for(host, pkg.get("apply_route"), pkg.get("apply_url"))
        if builder is None or run_adapter is None:
            self._set(
                app_id,
                "handoff",
                reason=f"the app cannot fill in forms on {host or pkg.get('apply_route')} yet",
                next_action="complete manually from the review package, then record the confirmation",
            )
            self.store.audit("application_handoff", app_id, host=host)
            self._notify(self.apps.find_one({"_id": app_id}), "handoff")
            return Outcome("handoff", "manual completion required", action_id=None)
        if job["status"] == "executing" and job.get("reserved_by") == worker:
            reserved = job  # re-entry by the same worker after an interrupt/resume
        else:
            # never treat an `executing` job held by someone else as free; only its expired lease can be taken
            reserved = self.store.reserve("jobs", {"_id": job["_id"]}, worker, [s for s in [job["status"]] if s != "executing"], "executing")
        if reserved is None:
            return Outcome("in_progress", "job reserved by another worker")
        action = self.actions.prepare(
            "application_submit", job["_id"], f"submit:{job['_id']}", {"application_id": app_id, "apply_url": pkg["apply_url"]}, app["material_hash"], host=host
        )
        if action.get("material_hash") != app["material_hash"] and action["status"] in ("succeeded", "needs_verification"):
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"] if job["status"] != "executing" else "qualified")
            return Outcome(action["status"], "an earlier submission for this job already exists; reconcile first", action_id=action["_id"])

        def adapter(action_doc: dict) -> dict:
            plan = builder(job, pkg, pkg["answers"])
            if not plan.job_id:
                plan.job_id = job["_id"]  # screenshots are named after and stored with the job's artifacts
            return run_adapter(plan)

        try:
            outcome = self.actions.execute(action["_id"], worker, adapter, job=job, has_material_unknowns=bool(pkg.get("outstanding")))
        except ActionHandoff as exc:  # the action store already put the action back to `ready` with the evidence
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"])
            ev = self._with_screenshots(job["_id"], app_id, exc.evidence)  # the challenge / login page capture becomes evidence too
            if self._schedule_challenge_retry(app, job, ev):
                return Outcome("retry_scheduled", "one later try is waiting", ev, action["_id"])
            nxt = (
                "finish the human check and the submit on the site yourself, then record the confirmation"
                if exc.evidence.get("challenge")
                else "refresh the site session from the Mac (amore browser login) or complete manually"
            )
            kind = "human_check" if exc.evidence.get("challenge") else "handoff"
            self._set(app_id, "handoff", reason=str(exc), next_action=nxt, evidence=ev, handoff_kind="human_check" if kind == "human_check" else "login")
            self.store.audit("application_handoff", app_id, host=host, challenge=exc.evidence.get("challenge"), captcha_attempts=len(exc.evidence.get("captcha_attempts") or []))
            self._notify(self.apps.find_one({"_id": app_id}), kind)
            return Outcome("handoff", str(exc), ev, action["_id"])
        self._apply_outcome(app, job, outcome, worker)
        return outcome

    def _apply_outcome(self, app: dict, job: dict, outcome: Outcome, worker: str) -> None:
        app_id = app["_id"]
        if outcome.status == "succeeded":
            ev = outcome.evidence
            shots = self.register_screenshots(job["_id"], app_id, ev.get("screenshots"))
            self._set(
                app_id,
                "submitted",
                submitted_at=utcnow(),
                application_date=to_local(utcnow()).date().isoformat(),
                evidence={
                    "basis": "receipt_observed",
                    "receipt_text": (ev.get("receipt_text") or "")[:1000],
                    "final_url": ev.get("final_url"),
                    "screenshots": shots,
                    # only identifiers the adapter actually observed in the receipt; never assumed from the job
                    "verified_identifiers": list(ev.get("matched_identifiers") or []),
                    "expected_identifiers": [x for x in [job.get("requisition_id")] if x],
                    "action_id": outcome.action_id,
                },
                evidence_basis="receipt_screenshot_reviewed" if any(s["label"] == "receipt" for s in shots) else "employer_status_reviewed",
                date_basis=f"employer receipt observed in {local_zone().key}",
                sync={"state": "sync_pending"},
                next_action="tracker sync + recruiter outreach prep",
            )
            self.store.release("jobs", {"_id": job["_id"]}, worker, "applied")
            from .sync import SyncOutbox

            SyncOutbox(self.store).enqueue(app_id, "record_submission")
            self._notify(self.apps.find_one({"_id": app_id}), "submitted")
        elif outcome.status == "needs_verification":
            ev = self._with_screenshots(job["_id"], app_id, outcome.evidence)
            self._set(app_id, "needs_verification", reason=outcome.reason, evidence=ev, next_action="check the employer account or email, then record the result")
            self.store.release("jobs", {"_id": job["_id"]}, worker, "needs_verification")
            self._notify(self.apps.find_one({"_id": app_id}), "needs_verification")
        elif outcome.status == "failed":
            ev = self._with_screenshots(job["_id"], app_id, outcome.evidence)
            missing = [m.get("label") for m in ((ev or {}).get("preflight") or {}).get("missing") or []] if isinstance(ev, dict) else []
            if missing:
                gone = ", ".join(str(m) for m in missing)
                nxt = f"Nothing was sent. The form did not have: {gone}. Retry with the app reads the form again; if it still fails, apply on the site yourself."
            elif outcome.retryable:
                nxt = "Nothing was sent. The app stopped before the final click, so Retry with the app is safe."
            else:
                nxt = "Nothing was sent. Fix the reported problem, then prepare the application again."
            self._set(app_id, "failed", reason=outcome.reason, evidence=ev, retryable=outcome.retryable, next_action=nxt)
            self.store.release("jobs", {"_id": job["_id"]}, worker, "qualified")
        elif outcome.status in ("denied", "blocked"):
            self._set(
                app_id,
                "ready_for_review" if outcome.status == "denied" else "blocked",
                reason=outcome.reason,
                next_action="grant authorization" if outcome.status == "denied" else "emergency stop active",
            )
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"] if job["status"] != "executing" else "qualified")
        else:
            self.store.release("jobs", {"_id": job["_id"]}, worker, job["status"] if job["status"] != "executing" else "qualified")

    def _with_screenshots(self, job_id: str, app_id: str, evidence: Any) -> Any:
        if not isinstance(evidence, dict) or not evidence.get("screenshots"):
            return evidence
        return {**evidence, "screenshots": self.register_screenshots(job_id, app_id, evidence.get("screenshots"))}

    def register_screenshots(self, job_id: str, app_id: str, raw: Any) -> list[dict]:
        """Normalise adapter screenshot evidence and register each capture in `artifacts` (kind `screenshot`).

        The artifact mirrors the tailored-document shape (`owner_id`, `path`, `filename`, `sha256`, `created_at`) so the
        hash-verified document download route can serve it by id; `job_id`, `application_id` and `label` let the
        application page list `summary` / `receipt` / `ambiguous` captures. Re-registering the same file is a no-op."""
        shots = normalize_screenshots(raw)
        arts = self.store.c("artifacts")
        for shot in shots:
            path = Path(shot["path"])
            if shot.get("sha256") is None and path.is_file():
                shot["sha256"] = sha256_file(path)
            if not shot.get("sha256"):
                continue  # file never landed; keep the evidence entry but there is nothing to serve
            existing = arts.find_one({"kind": "screenshot", "application_id": app_id, "sha256": shot["sha256"], "label": shot["label"]}, {"_id": 1})
            if existing:
                shot["artifact_id"] = existing["_id"]
                continue
            art = {
                "_id": new_id("art"),
                "owner_id": job_id,
                "job_id": job_id,
                "application_id": app_id,
                "kind": "screenshot",
                "label": shot["label"],
                "path": str(path),
                "filename": path.name,
                "sha256": shot["sha256"],
                "media_type": "image/png",
                "captured_at": shot.get("captured_at"),
                "url": shot.get("url"),
                "created_at": utcnow(),
            }
            arts.insert_one(art)
            shot["artifact_id"] = art["_id"]
        return shots

    def reconcile(self, app_id: str, resolved: str, evidence: dict, by: str = "user") -> dict:
        app = self.apps.find_one({"_id": app_id})
        action = self.actions.by_key(f"submit:{app['job_id']}")
        if action and action["status"] == "needs_verification":
            self.actions.reconcile(action["_id"], resolved, evidence, by)
        job = self.jobs.get(app["job_id"])
        if resolved == "succeeded":
            ev = {"basis": "explicit_user_confirmation", **evidence}
            self.jobs.set_status(job["_id"], "applied")
            from .sync import SyncOutbox

            SyncOutbox(self.store).enqueue(app_id, "record_submission")
            return self._set(
                app_id,
                "submitted",
                submitted_at=utcnow(),
                application_date=evidence.get("application_date") or to_local(utcnow()).date().isoformat(),
                evidence=ev,
                evidence_basis="explicit_user_confirmation",
                date_basis=evidence.get("date_basis", "user confirmed after reconciliation"),
                sync={"state": "sync_pending"},
            )
        self.jobs.set_status(job["_id"], "qualified")
        return self._set(app_id, "failed", reason="reconciled as not submitted", evidence=evidence)

    def record_manual_submission(self, app_id: str, confirmation: str, application_date: str, resume_sha256: str | None, receipt_path: str | None = None) -> dict:
        """The owner completed a handoff; record it with explicit_user_confirmation evidence."""
        app = self.apps.find_one({"_id": app_id})
        job = self.jobs.get(app["job_id"])
        ev = {"basis": "explicit_user_confirmation", "confirmation": confirmation, "resume_sha256": resume_sha256, "receipt_path": receipt_path}
        self.jobs.set_status(job["_id"], "applied")
        from .sync import SyncOutbox

        SyncOutbox(self.store).enqueue(app_id, "record_submission")
        return self._set(
            app_id,
            "submitted",
            submitted_at=utcnow(),
            application_date=application_date,
            evidence=ev,
            evidence_basis="explicit_user_confirmation",
            date_basis=f"user-reported date ({local_zone().key})",
            sync={"state": "sync_pending"},
        )

    def _schedule_challenge_retry(self, app: dict, job: dict, evidence: dict) -> bool:
        """Wait, then one fresh session, before telling the owner. Never a second schedule.

        A hold that is still open owns the page, so nothing is queued. A try that already clicked
        submit is recorded as confirm-only: the later session looks for a confirmation and does not click.
        Returns True when the owner should not be notified yet.
        """
        if not evidence.get("challenge") or self.challenge_retry_minutes <= 0 or app.get("challenge_retry_active"):
            return False
        from .human_check import active_hold

        if active_hold(self.store, app["_id"]):
            return True  # the open hold is the response; do not queue another try and do not notify yet
        key = f"challenge_retry:{job['_id']}"
        coll = self.store.c("challenge_retries")
        existing = coll.find_one({"idempotency_key": key})
        if existing:
            return existing.get("status") in ("scheduled", "running")
        due = utcnow() + timedelta(minutes=self.challenge_retry_minutes)
        doc = {
            "_id": new_id("chretry"),
            "idempotency_key": key,
            "application_id": app["_id"],
            "job_id": job["_id"],
            "material_hash": app.get("material_hash"),
            "final_clicked": bool(evidence.get("final_clicked")),
            "status": "scheduled",
            "due_at": due,
            "created_at": utcnow(),
        }
        if not self.store.insert_unique("challenge_retries", doc):
            existing = coll.find_one({"idempotency_key": key})
            return bool(existing and existing.get("status") in ("scheduled", "running"))
        self._set(
            app["_id"],
            "challenge_retry",
            reason="A check appeared. The app will try this once more later, then tell you if it still needs you.",
            next_action="Nothing to do yet. You will be told if the later try still cannot finish.",
            evidence=evidence,
            challenge_retry_due=due,
            challenge_retry_active=False,
        )
        self.store.audit("challenge_retry_scheduled", app["_id"], due_at=due.isoformat(), final_clicked=doc["final_clicked"])
        return True

    def _notify(self, app: dict | None, state: str) -> None:
        """Best-effort owner alert. A delivery problem never changes the application result."""
        if self.notifier is None or app is None:
            return
        try:
            job = self.jobs.get(app["job_id"]) or {"_id": app["job_id"], "employer": "", "title": ""}
            run = self.store.c("runs").find_one({"kind": "application", "params.job_id": app["job_id"]}, sort=[("created_at", -1)])
            self.notifier.notify(state=state, job=job, application=app, run_id=(run or {}).get("_id"))
        except Exception:
            log.exception("notification failed")

    def _builder_for(self, host: str | None, route: str | None, apply_url: str | None = None) -> PlanBuilder | None:
        if route and route in self.plan_builders:
            return self.plan_builders[route]
        if host:
            for key, b in self.plan_builders.items():
                if key == host or host.endswith("." + key):
                    return b
        from .ats import adapter_for

        adapter = adapter_for(apply_url, route)  # built-in site adapters (Greenhouse, ...)
        return adapter.build_plan if adapter else None
