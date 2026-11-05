from datetime import date

from amorehiremepls.candidate.profile import (
    BandEvidence,
    Compensation,
    Employment,
    Provenance,
    build_profile_from_skills,
    compute_experience,
    default_preferences,
    desired_salary,
    parse_facts_skill,
)

FAKE_FACTS = """
# facts
- Legal first name: Alex
- Legal last name: Example
- Preferred name is always Al if there is a section for it
- Address line 1 is always: 1 Test Street
- City: Milton
- Province: Ontario
- Country: Canada
- Postal code is always L0L 0L0 (keep the space)
- Email: alex@example.com
- Phone: 555 000 0000 (format as required by the form; Canadian +1)
"""

FAKE_SALARY = "Personal floor remains CAD 123,000 to decide whether to apply. Personal target CAD 130,000 is a preference."

FAKE_APPLY = """# Apply skill (fictional)

Saved preferences:

- Permanent backend software development.
- Authorized to work in Canada without sponsorship now or in the future.
- Globex full-time employment ended March 3, 2025; month/year dates are May 2021\u2013March 2025. Calculate experience from dates.
- Preserve the actual education credential. Degree on forms is Diploma of Example Studies. If the school typeahead cannot find Example Technical College, pick Other.

When asked about AI workflow, use the confirmed answer: Example Assistant for drafting; every change reviewed before merge. This does not establish production LLM experience.
"""


def test_parse_facts_with_provenance():
    facts = {f.key: f for f in parse_facts_skill(FAKE_FACTS, "2026-10-15")}
    assert facts["legal_first_name"].value == "Alex"
    assert facts["preferred_name"].value == "Al"
    assert facts["postal_code"].value == "L0L 0L0"
    assert facts["phone"].value == "555 000 0000"
    assert facts["email"].provenance.source == "skill:application-facts"
    assert facts["email"].provenance.confirmed_on == "2026-10-15"


def test_experience_separates_internships_and_full_time():
    prov = Provenance(source="test")
    emp = [
        Employment(employer="Bank", title="Dev", kind="full_time", start="2021-05", end="2024-08", provenance=prov),
        Employment(employer="Startup", title="Intern", kind="internship", start="2022-05", end="2022-08", provenance=prov),
        Employment(employer="Unknown", title="Dev", kind="full_time", start=None, end=None, provenance=prov),
    ]
    s = compute_experience(emp, as_of=date(2024, 9, 4))
    assert s.full_time_years_completed == 3  # 39 months -> 3 completed, never rounded up
    assert s.full_time_months == 39
    assert s.internship_months == 3
    assert s.total_years_completed == 3
    assert s.unknown_ranges == ["Unknown: start unknown"]


def test_salary_rule_requires_matching_base_band():
    comp = Compensation(currency="CAD", floor_base=110000, target_base=120000, provenance=Provenance(source="t"))
    assert desired_salary(comp, None).outcome == "needs_input"
    band = BandEvidence(min_base=115000, max_base=125000, currency="CAD", title_matches=True, location_matches=True)
    no_currency = Compensation(floor_base=110000, provenance=Provenance(source="t"))
    assert desired_salary(no_currency, band).outcome == "needs_input"  # no currency saved: ask
    d = desired_salary(comp, band, "fair")
    assert d.outcome == "answer" and d.amount == 120000
    assert desired_salary(comp, band, "stretch").amount == 115000
    senior = band.model_copy(update={"title_matches": False})
    assert desired_salary(comp, senior).outcome == "needs_input"
    ote = band.model_copy(update={"is_total_comp_or_ote": True})
    assert desired_salary(comp, ote).outcome == "needs_input"
    low = BandEvidence(min_base=70000, max_base=90000, currency="CAD", title_matches=True, location_matches=True)
    assert desired_salary(comp, low).outcome == "below_floor"
    usd = band.model_copy(update={"currency": "USD"})
    assert desired_salary(comp, usd).outcome == "needs_input"


def test_default_preferences_are_neutral_and_exclusions_come_from_the_candidate():
    from tests.fixtures.profile_fixture import build_test_profile

    prefs = default_preferences()
    assert prefs.recipient_exclusions == []  # no named recipient exclusions by default; first-contact gate handles history
    assert prefs.excluded_employers == [] and prefs.locations == [] and prefs.role_families == [] and prefs.excluded_role_families == []
    assert prefs.outreach_rules["role_phrase"] == "" and prefs.outreach_rules["one_liner"] == ""
    bare = build_profile_from_skills(FAKE_FACTS, FAKE_SALARY)
    assert bare.is_excluded_employer("Royal Bank of Canada") is None  # nothing is excluded until the candidate says so
    assert bare.compensation.currency == "CAD" and bare.compensation.floor_base == 123000  # from the salary text, not a default
    profile = build_test_profile(FAKE_FACTS, FAKE_SALARY)
    assert profile.is_excluded_employer("Royal Bank of Canada") is not None
    assert profile.is_excluded_employer("Amazon Web Services") is not None
    assert profile.is_excluded_employer("Shopify") is None
    assert "base_resume" in profile.missing_essentials()
    assert profile.first_name() == "Al" and profile.profession() == "backend software developer"
    assert bare.profession() == "professional" and build_profile_from_skills(None, None).first_name() == ""


def test_apply_skill_parsing_is_generic_and_private_defaults_are_gone():
    import inspect

    from amorehiremepls.candidate import profile as profile_mod
    from amorehiremepls.candidate.profile import apply_private_overrides, parse_apply_skill

    employment, answers = parse_apply_skill(FAKE_APPLY, "2026-01-01")
    assert len(employment) == 1 and employment[0].employer == "Globex" and employment[0].start == "2021-05" and employment[0].end == "2025-03"
    keys = {a.key: a.value for a in answers}
    assert keys["work_authorization"].startswith("Authorized to work in Canada")
    assert keys["requires_sponsorship"] == "No"
    assert keys["degree_level"] == "Diploma of Example Studies"
    assert keys["school"] == "Example Technical College"
    assert keys["ai_workflow"].startswith("Example Assistant")
    assert keys["employment_preference"] == "Permanent"
    # without any private source the profile has no employment/answers at all
    empty = build_profile_from_skills(None, None)
    assert empty.employment == [] and empty.answers == [] and empty.education == []
    assert any("employment history" in u for u in empty.unknowns)
    full = build_profile_from_skills(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY)
    assert full.employment[0].provenance.source == "skill:apply-jobs"
    assert {a.key: a for a in full.answers}["ai_workflow"].question_pattern  # the legacy answer still knows which question it answers
    apply_private_overrides(full, {"employment": [{"employer": "Initech", "title": "Intern", "kind": "internship", "start": "2020-05", "end": "2020-08"}], "skills": ["Java"]})
    assert any(e.kind == "internship" for e in full.employment) and full.skills[0].name == "Java"
    # the module source must not carry employment rows, degrees or schools as literals
    src = inspect.getsource(profile_mod)
    assert "default_employment" not in src and "default_answers" not in src
    assert 'Employment(employer="' not in src.replace("employer=e.get", "")


def test_merge_employment_does_not_double_count_the_same_job_written_differently():
    from datetime import date

    from amorehiremepls.candidate.profile import Employment, Provenance, compute_experience, merge_employment

    prov = Provenance(source="test", confirmed_on="2026-01-01")
    confirmed = [Employment(employer="Globex", title="(title from the approved base resume)", kind="full_time", start="2021-05", end="2025-03", end_known=True, provenance=prov)]
    resume = [
        Employment(employer="Globex Corporation Ltd.", title="Backend Developer", kind="full_time", start="2021-05", end=None, end_known=True, provenance=prov),
        Employment(employer="Initech", title="Intern", kind="internship", start="2020-05", end="2020-08", end_known=True, provenance=prov),
    ]
    merged = merge_employment(confirmed, resume)
    assert len(merged) == 2
    assert merged[0].end == "2025-03" and merged[0].title == "Backend Developer"  # confirmed dates win, resume title fills the placeholder
    assert compute_experience(merged, as_of=date(2026, 1, 1)).full_time_months == 46
