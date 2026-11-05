"""The one personal configuration surface: ``candidate/profile.toml``.

A candidate describes themselves once in that file (profession, facts, eligibility, employment, education, saved
answers, skills, job preferences, pay, employers and people to skip, outreach wording, resume rules, fit rules and
job board seeds). The file is read only with stdlib ``tomllib``, never committed, and applied to the MongoDB profile
explicitly (`amore profile import --from candidate/profile.toml` or the Load button on the Profile page). MongoDB
remains the runtime truth; nothing reloads silently.

Sections present in the file replace the matching part of the profile; absent sections leave it unchanged. Facts,
answers, education rows, employment rows and skills are upserted by key (or employer + start month). Sensitive facts
already in the database stay unless the file names them. The approved base resume is never touched here.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from amorehiremepls.candidate.profile import (
    CandidateInfo,
    Compensation,
    Employment,
    ExcludedEmployer,
    Fact,
    FitRules,
    Preferences,
    Profile,
    Provenance,
    RecipientExclusion,
    ResumeRules,
    Skill,
    StyleRules,
    TermAlias,
    merge_employment,
    refresh_unknowns,
)
from amorehiremepls.core.util import new_id, sha256_file

PROFILE_TOML_NAME = "profile.toml"
EXAMPLE_TOML_NAME = "profile.example.toml"
SOURCE = "candidate:profile.toml"
CONTROL_KEY = "candidate_profile_toml"  # controls entry: {"sha256": ..., "loaded_at": ..., "path": ...}

# Fact keys the forms understand, with the group the dashboard shows them under.
FACT_CATEGORIES: dict[str, str] = {
    "legal_first_name": "identity",
    "legal_last_name": "identity",
    "preferred_name": "identity",
    "display_name": "identity",
    "email": "contact",
    "phone": "contact",
    "phone_country_code": "contact",
    "linkedin_url": "contact",
    "github_url": "contact",
    "website_url": "contact",
    "portfolio_url": "contact",
    "address_line1": "address",
    "city": "address",
    "province": "address",
    "postal_code": "address",
    "country": "address",
}
ELIGIBILITY_KEYS = ("work_authorization", "requires_sponsorship", "availability", "notice_period", "employment_preference")
SENSITIVE_KEYS = ("gender", "disability_status", "sexual_orientation", "visible_minority", "ethnicity", "veteran_status", "indigenous_identity", "race", "religion", "age")


class ProfileTomlError(ValueError):
    pass


@dataclass
class ImportReport:
    """What an import did (counts only; never values)."""

    facts: int = 0
    answers: int = 0
    education: int = 0
    employment: int = 0
    skills: int = 0
    sections: list[str] = field(default_factory=list)
    sources_added: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "answers": self.answers,
            "education": self.education,
            "employment": self.employment,
            "skills": self.skills,
            "sections": self.sections,
            "sources_added": self.sources_added,
            "warnings": self.warnings,
        }


def load_profile_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileTomlError(f"{path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileTomlError(f"{path.name}: top level must be a table")
    return data


def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _strs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def _upsert_fact(group: list[Fact], fact: Fact) -> None:
    group[:] = [x for x in group if x.key != fact.key] + [fact]


def _fact_rows(entries: Any, default_category: str, prov: Provenance, sensitive: bool = False) -> list[Fact]:
    """``[[answers]]``/``[[education]]`` rows: either ``{key, value[, match]}`` tables or plain ``name = value`` tables."""
    out: list[Fact] = []
    for row in entries or []:
        if not isinstance(row, dict):
            continue
        if "key" in row:
            out.append(
                Fact(
                    category=str(row.get("category") or default_category),
                    key=str(row["key"]).strip(),
                    value=_str(row.get("value")),
                    question_pattern=_str(row.get("match")),
                    sensitivity="sensitive" if sensitive or row.get("sensitive") else "personal",
                    provenance=prov,
                )
            )
            continue
        for k, v in row.items():
            if k in ("match", "category", "sensitive"):
                continue
            out.append(Fact(category=default_category, key=str(k).strip(), value=_str(v), sensitivity="sensitive" if sensitive else "personal", provenance=prov))
    return out


def _excluded_employers(entries: Any) -> list[ExcludedEmployer]:
    out: list[ExcludedEmployer] = []
    for row in entries or []:
        if isinstance(row, str):
            out.append(ExcludedEmployer(name=row.strip(), reason="listed in candidate/profile.toml"))
        elif isinstance(row, dict) and row.get("name"):
            out.append(
                ExcludedEmployer(
                    name=str(row["name"]).strip(),
                    aliases=_strs(row.get("aliases")),
                    reason=str(row.get("reason") or "listed in candidate/profile.toml"),
                    applies_to=_strs(row.get("applies_to")) or ["applications", "outreach"],
                )
            )
    return out


def _recipient_exclusions(entries: Any, prov: Provenance, existing: list[RecipientExclusion]) -> list[RecipientExclusion]:
    out: list[RecipientExclusion] = []
    by_label = {x.label.lower(): x for x in existing}
    for row in entries or []:
        if isinstance(row, str):
            row = {"label": row}
        if not isinstance(row, dict) or not row.get("label"):
            continue
        label = str(row["label"]).strip()
        url, email = _str(row.get("profile_url")), _str(row.get("email"))
        prior = by_label.get(label.lower())
        out.append(
            RecipientExclusion(
                id=prior.id if prior else new_id("rex"),
                label=label,
                resolution="resolved" if (url or email) else "unresolved",
                profile_url=url,
                email=email,
                employer=_str(row.get("employer")),
                reason=str(row.get("reason") or ""),
                provenance=prov,
            )
        )
    return out


def _preferences(data: dict[str, Any], prefs: Preferences) -> None:
    for key in (
        "employment_types",
        "role_families",
        "excluded_role_families",
        "excluded_role_family_exceptions",
        "locations",
        "remote_eligible_terms",
        "work_arrangements",
        "startup_signals_extra",
    ):
        if key in data:
            values = _strs(data[key])
            if key in ("role_families", "excluded_role_families", "excluded_role_family_exceptions", "remote_eligible_terms", "work_arrangements"):
                values = [v.lower() for v in values]
            setattr(prefs, key, values)
    for key in ("region_label", "remote_rule"):
        if key in data:
            setattr(prefs, key, str(data[key] or ""))
    if "small_startup_rule" in data:
        rule = str(data["small_startup_rule"]).strip().lower()
        if rule not in ("needs_review", "ignore"):
            raise ProfileTomlError("[preferences].small_startup_rule must be needs_review or ignore")
        prefs.small_startup_rule = rule  # type: ignore[assignment]


def _compensation(data: dict[str, Any], current: Compensation, prov: Provenance) -> Compensation:
    band = str(data.get("band_rule") or current.band_rule).strip().lower()
    unknown = str(data.get("unknown_rule") or current.unknown_rule).strip().lower()
    if band not in ("midpoint", "upper_half", "minimum"):
        raise ProfileTomlError("[compensation].band_rule must be midpoint, upper_half or minimum")
    if unknown not in ("needs_input", "leave_blank"):
        raise ProfileTomlError("[compensation].unknown_rule must be needs_input or leave_blank")
    target = data.get("target_base", current.target_base)
    return Compensation(
        currency=str(data.get("currency") or current.currency or "").upper(),
        floor_base=int(data.get("floor_base") or 0) if "floor_base" in data else current.floor_base,
        target_base=int(target) if target not in (None, "", 0) else None,
        band_rule=band,  # type: ignore[arg-type]
        unknown_rule=unknown,  # type: ignore[arg-type]
        provenance=prov,
    )


def _resume_rules(data: dict[str, Any], current: ResumeRules) -> ResumeRules:
    aliases = {}
    raw_aliases = data.get("section_aliases") or {}
    if not isinstance(raw_aliases, dict):
        raise ProfileTomlError("[resume].section_aliases must be a table of section = [headings]")
    for section, names in raw_aliases.items():
        aliases[str(section).strip().lower()] = [n.lower() for n in _strs(names)]
    return ResumeRules(
        max_pages=int(data.get("max_pages", current.max_pages)),
        training_role_terms=_strs(data["training_role_terms"]) if "training_role_terms" in data else current.training_role_terms,
        section_aliases=aliases if "section_aliases" in data else current.section_aliases,
        allowed_terms=_strs(data["allowed_terms"]) if "allowed_terms" in data else current.allowed_terms,
        max_bullets_per_role=int(data.get("max_bullets_per_role", current.max_bullets_per_role)),
    )


def _fit_rules(data: dict[str, Any], current: FitRules) -> FitRules:
    aliases: list[TermAlias] = []
    raw = data.get("term_aliases")
    if isinstance(raw, dict):  # {"databases": ["SQL", ...]} shorthand: the key is the one category word
        for word, members in raw.items():
            aliases.append(TermAlias(words=[str(word)], evidence=_strs(members)))
    elif isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict) and row.get("words"):
                aliases.append(TermAlias(words=_strs(row["words"]), evidence=_strs(row.get("evidence"))))
    return FitRules(
        skill_terms=_strs(data["skill_terms"]) if "skill_terms" in data else current.skill_terms,
        term_aliases=aliases if "term_aliases" in data else current.term_aliases,
        hold_back_terms=_strs(data["hold_back_terms"]) if "hold_back_terms" in data else current.hold_back_terms,
    )


def apply_profile_toml(profile: Profile, data: dict[str, Any], confirmed_on: str | None = None, store: Any | None = None) -> ImportReport:
    """Apply a parsed ``profile.toml`` to ``profile`` in place. Returns counts. ``store`` (optional) receives the
    ``[discovery]`` job board seeds as the ``discovery_sources`` control without duplicating existing entries."""
    prov = Provenance(source=SOURCE, confirmed_on=confirmed_on)
    report = ImportReport()
    cand = data.get("candidate") or {}
    if cand:
        report.sections.append("candidate")
        profile.candidate = CandidateInfo(profession=str(cand.get("profession") or "").strip(), headline=str(cand.get("headline") or "").strip())
        for src, key in (("first_name", "legal_first_name"), ("preferred_name", "preferred_name"), ("last_name", "legal_last_name")):
            if _str(cand.get(src)):
                _upsert_fact(profile.facts, Fact(category="identity", key=key, value=_str(cand[src]), provenance=prov))
                report.facts += 1
    facts = data.get("facts") or {}
    if facts:
        report.sections.append("facts")
        for k, v in facts.items():
            key = str(k).strip()
            # a plain value, or an inline table { value, match, category, sensitive } for a fact with a question pattern
            detail = v if isinstance(v, dict) else {"value": v}
            sensitive = key in SENSITIVE_KEYS or bool(detail.get("sensitive"))
            category = str(detail.get("category") or ("self_identification" if sensitive else FACT_CATEGORIES.get(key, "other")))
            fact = Fact(
                category=category,
                key=key,
                value=_str(detail.get("value")),
                question_pattern=_str(detail.get("match")),
                sensitivity="sensitive" if sensitive else "personal",
                provenance=prov,
            )
            _upsert_fact(profile.facts, fact)
            report.facts += 1
    elig = data.get("eligibility") or {}
    if elig:
        report.sections.append("eligibility")
        for k, v in elig.items():
            key = str(k).strip()
            _upsert_fact(profile.answers, Fact(category="answer", key=key, value=_str(v), provenance=prov))
            report.answers += 1
            if key == "work_authorization":
                profile.preferences.work_authorization = _str(v)
    if "employment" in data:
        report.sections.append("employment")
        rows: list[Employment] = []
        for row in data.get("employment") or []:
            if not isinstance(row, dict) or not row.get("employer"):
                continue
            kind = str(row.get("kind") or "full_time").strip().lower()
            if kind not in ("full_time", "internship", "contract", "unknown"):
                raise ProfileTomlError(f"[[employment]] kind must be full_time, internship, contract or unknown (got {kind})")
            end = _str(row.get("end"))
            present = (end or "").lower() in ("present", "current", "now")
            rows.append(
                Employment(
                    employer=str(row["employer"]).strip(),
                    title=str(row.get("title") or "(see base resume)").strip(),
                    kind=kind,  # type: ignore[arg-type]
                    start=_str(row.get("start")),
                    end=None if present else end,
                    end_known=bool(row.get("end_known", True)) if not present else True,
                    provenance=prov,
                )
            )
        for emp in rows:
            profile.employment = [x for x in profile.employment if not (x.employer == emp.employer and x.start == emp.start)] + [emp]
        report.employment = len(rows)
        base_rows = [e for e in profile.employment if e.provenance.source.startswith("base_resume:")]
        others = [e for e in profile.employment if not e.provenance.source.startswith("base_resume:")]
        profile.employment = merge_employment(others, base_rows)
    if "education" in data:
        report.sections.append("education")
        for fact in _fact_rows(data.get("education"), "education", prov):
            _upsert_fact(profile.education, fact)
            report.education += 1
    if "answers" in data:
        report.sections.append("answers")
        for fact in _fact_rows(data.get("answers"), "answer", prov):
            if fact.key in SENSITIVE_KEYS:
                fact.sensitivity, fact.category = "sensitive", "self_identification"
                _upsert_fact(profile.facts, fact)
                report.facts += 1
            else:
                _upsert_fact(profile.answers, fact)
                report.answers += 1
    if "skills" in data:
        report.sections.append("skills")
        names = _strs(data.get("skills"))
        for name in names:
            profile.skills = [x for x in profile.skills if x.name.lower() != name.lower()] + [Skill(name=name, provenance=prov)]
        report.skills = len(names)
    prefs = profile.preferences
    if "preferences" in data:
        report.sections.append("preferences")
        _preferences(data["preferences"] or {}, prefs)
    if "compensation" in data:
        report.sections.append("compensation")
        profile.compensation = _compensation(data["compensation"] or {}, profile.compensation, prov)
    excl = data.get("exclusions")
    if excl is not None:
        report.sections.append("exclusions")
        if "employers" in excl:
            prefs.excluded_employers = _excluded_employers(excl.get("employers"))
        if "recipients" in excl:
            prefs.recipient_exclusions = _recipient_exclusions(excl.get("recipients"), prov, prefs.recipient_exclusions)
    if "outreach" in data:
        report.sections.append("outreach")
        rules = dict(prefs.outreach_rules or {})
        for k, v in (data["outreach"] or {}).items():
            rules[str(k)] = v
        rules.pop("ask_other_permanent_backend_roles", None)  # legacy key, superseded by ask_other_roles + role_phrase
        prefs.outreach_rules = rules
    if "resume" in data:
        report.sections.append("resume")
        prefs.resume = _resume_rules(data["resume"] or {}, prefs.resume)
    if "fit" in data:
        report.sections.append("fit")
        prefs.fit = _fit_rules(data["fit"] or {}, prefs.fit)
    if "style" in data:
        report.sections.append("style")
        prefs.style = StyleRules(no_dashes=bool((data["style"] or {}).get("no_dashes", prefs.style.no_dashes)))
    disc = data.get("discovery") or {}
    if disc and store is not None:
        report.sections.append("discovery")
        existing = store.get_control("discovery_sources", []) or []
        seen = {(s.get("type"), s.get("key")) for s in existing}
        for row in disc.get("sources") or []:
            if not isinstance(row, dict) or not row.get("type") or not row.get("key"):
                continue
            item = {"type": str(row["type"]).strip().lower(), "key": str(row["key"]).strip(), "employer": _str(row.get("employer"))}
            if (item["type"], item["key"]) in seen:
                continue
            existing.append(item)
            seen.add((item["type"], item["key"]))
            report.sources_added += 1
        if report.sources_added:
            store.set_control("discovery_sources", existing)
    profile.unknowns = refresh_unknowns(profile)
    if profile.employment:
        profile.unknowns = [u for u in profile.unknowns if "employment history" not in u]
    return report


def preference_fingerprint(profile: Profile) -> dict[str, str]:
    """Stable hashes of each configurable part, for comparing a dry run against the live profile without values."""
    from amorehiremepls.core.util import sha256_json

    prefs = profile.preferences.model_dump()
    comp = profile.compensation.model_dump(exclude={"provenance"})
    return {
        "preferences": sha256_json({k: v for k, v in prefs.items() if k not in ("excluded_employers", "recipient_exclusions", "outreach_rules", "resume", "fit", "style")}),
        "excluded_employers": sha256_json([e.model_dump() for e in profile.preferences.excluded_employers]),
        "recipient_exclusions": sha256_json([x.model_dump(exclude={"id", "provenance"}) for x in profile.preferences.recipient_exclusions]),
        "outreach_rules": sha256_json(profile.preferences.outreach_rules),
        "resume": sha256_json(profile.preferences.resume.model_dump()),
        "fit": sha256_json(profile.preferences.fit.model_dump()),
        "style": sha256_json(profile.preferences.style.model_dump()),
        "compensation": sha256_json(comp),
        "candidate": sha256_json(profile.candidate.model_dump()),
        "facts": sha256_json(sorted((f.key, f.value or "", f.question_pattern or "") for f in profile.facts)),
        "answers": sha256_json(sorted((f.key, f.value or "", f.question_pattern or "") for f in profile.answers)),
        "education": sha256_json(sorted((f.key, f.value or "") for f in profile.education)),
        "employment": sha256_json(sorted((e.employer, e.title, e.kind, e.start or "", e.end or "", e.end_known) for e in profile.employment)),
        "skills": sha256_json(sorted(s.name for s in profile.skills)),
    }


# -- status for the dashboard ------------------------------------------------------------------------------------


@dataclass
class TomlStatus:
    path: Path
    present: bool
    sha256: str | None
    loaded_sha256: str | None
    loaded_at: Any | None

    @property
    def changed(self) -> bool:
        return self.present and self.sha256 != self.loaded_sha256

    @property
    def state(self) -> str:
        if not self.present:
            return "missing"
        if self.loaded_sha256 is None:
            return "new"
        return "changed" if self.changed else "loaded"


def toml_status(candidate_dir: Path, store: Any | None) -> TomlStatus:
    path = Path(candidate_dir) / PROFILE_TOML_NAME
    present = path.is_file()
    sha = sha256_file(path) if present else None
    rec = (store.get_control(CONTROL_KEY) if store is not None else None) or {}
    return TomlStatus(path=path, present=present, sha256=sha, loaded_sha256=rec.get("sha256"), loaded_at=rec.get("loaded_at"))


# -- export (writes the candidate's live settings back into the TOML shape) ------------------------------------------


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v)
    if "\n" in s:
        return '"""' + s.replace("\\", "\\\\").replace('"""', '\\"""') + '"""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else '"' + k.replace('"', '\\"') + '"'


class _Inline(str):
    """A pre-rendered TOML inline table; written as is by ``_table``."""


def _fact_entry(f: Fact) -> Any:
    """A plain string for an ordinary fact; an inline table when the fact carries a question pattern, a category the
    key does not imply, or a sensitivity the key does not imply, so a reload restores exactly what was saved."""
    implied_sensitive = f.key in SENSITIVE_KEYS
    implied_category = "self_identification" if implied_sensitive else FACT_CATEGORIES.get(f.key, "other")
    extras: dict[str, Any] = {}
    if f.question_pattern:
        extras["match"] = f.question_pattern
    if f.category != implied_category:
        extras["category"] = f.category
    if (f.sensitivity == "sensitive") != implied_sensitive:
        extras["sensitive"] = f.sensitivity == "sensitive"
    if not extras:
        return f.value if f.value else '""'
    items = {"value": f.value or "", **extras}
    return _Inline("{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in items.items()) + " }")


def _table(name: str, items: dict[str, Any], array: bool = False, keep_empty: bool = False) -> str:
    head = f"[[{name}]]" if array else f"[{name}]"
    lines = [head]
    for k, v in items.items():
        if isinstance(v, _Inline):
            lines.append(f"{_toml_key(k)} = {v}")
            continue
        if v == '""' and keep_empty:
            lines.append(f'{_toml_key(k)} = ""')
            continue
        if v is None or v == "" or v == [] or v == '""':
            continue
        lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


def export_profile_toml(profile: Profile, include_facts: bool = True) -> str:
    """The candidate's current profile in ``profile.toml`` form (personal values included; write it only to the
    gitignored candidate folder). Sensitive facts are included so the file is complete; keep it private."""
    p = profile.preferences
    out: list[str] = ["# Written by `amore profile export`. Private: this file is ignored by git.\n"]
    if include_facts and profile.skills:
        # a top level key must come before the first table header, or TOML attaches it to that table
        out.append(f"skills = {_toml_value([s.name for s in profile.skills])}\n")
    cand = {
        "profession": profile.candidate.profession,
        "headline": profile.candidate.headline,
        "first_name": profile.fact_value("legal_first_name"),
        "preferred_name": profile.fact_value("preferred_name"),
    }
    out.append(_table("candidate", cand))
    if include_facts:
        # unknown facts are written as "" so the file shows what is still missing and a reload keeps them unknown
        facts = {f.key: _fact_entry(f) for f in profile.facts if f.key not in ("legal_first_name", "preferred_name")}
        out.append(_table("facts", facts, keep_empty=True))
        elig = {f.key: f.value for f in profile.answers if f.key in ELIGIBILITY_KEYS and f.value}
        if elig:
            out.append(_table("eligibility", elig))
        for e in profile.employment:
            if e.provenance.source.startswith("base_resume:"):
                continue  # read from the approved resume on import; not a setting
            row = {"employer": e.employer, "title": e.title, "kind": e.kind, "start": e.start, "end": e.end or ("present" if e.end_known else None), "end_known": e.end_known}
            out.append(_table("employment", row, array=True))
        for f in profile.education:
            out.append(_table("education", {"key": f.key, "value": f.value}, array=True))
        for f in profile.answers:
            if f.key in ELIGIBILITY_KEYS:
                continue
            out.append(_table("answers", {"key": f.key, "value": f.value, "match": f.question_pattern}, array=True))
    out.append(
        _table(
            "preferences",
            {
                "employment_types": p.employment_types,
                "role_families": p.role_families,
                "excluded_role_families": p.excluded_role_families,
                "excluded_role_family_exceptions": p.excluded_role_family_exceptions,
                "locations": p.locations,
                "remote_eligible_terms": p.remote_eligible_terms,
                "region_label": p.region_label,
                "remote_rule": p.remote_rule,
                "work_arrangements": p.work_arrangements,
                "small_startup_rule": p.small_startup_rule,
                "startup_signals_extra": p.startup_signals_extra,
            },
        )
    )
    c = profile.compensation
    out.append(_table("compensation", {"currency": c.currency, "floor_base": c.floor_base, "target_base": c.target_base, "band_rule": c.band_rule, "unknown_rule": c.unknown_rule}))
    for e in p.excluded_employers:
        out.append(_table("exclusions.employers", {"name": e.name, "aliases": e.aliases, "reason": e.reason, "applies_to": e.applies_to}, array=True))
    for x in p.recipient_exclusions:
        out.append(_table("exclusions.recipients", {"label": x.label, "profile_url": x.profile_url, "email": x.email, "employer": x.employer, "reason": x.reason}, array=True))
    out.append(_table("outreach", {k: v for k, v in (p.outreach_rules or {}).items() if not k.startswith("ask_other_permanent")}))
    out.append(_table("resume", {k: v for k, v in p.resume.model_dump().items() if k != "section_aliases"}))
    if p.resume.section_aliases:
        out.append(_table("resume.section_aliases", p.resume.section_aliases))
    out.append(_table("fit", {"skill_terms": p.fit.skill_terms, "hold_back_terms": p.fit.hold_back_terms}))
    for alias in p.fit.term_aliases:
        out.append(_table("fit.term_aliases", {"words": alias.words, "evidence": alias.evidence}, array=True))
    out.append(_table("style", {"no_dashes": p.style.no_dashes}))
    return "\n".join(out)
