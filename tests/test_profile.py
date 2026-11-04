from datetime import date

from amorehiremepls.profile import (
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

FAKE_SALARY = "Personal floor remains CAD 95,000 to decide whether to apply. Personal target CAD 100,000 is a preference."


def test_parse_facts_with_provenance():
    facts = {f.key: f for f in parse_facts_skill(FAKE_FACTS, "2026-10-15")}
    assert facts["legal_first_name"].value == "Alex"
    assert facts["preferred_name"].value == "Al"
    assert facts["postal_code"].value == "L0L 0L0"
    assert facts["phone"].value == "555 000 0000"
    assert facts["email"].provenance.source == "skill:joss-application-facts"
    assert facts["email"].provenance.confirmed_on == "2026-10-15"


def test_experience_separates_internships_and_full_time():
    prov = Provenance(source="test")
    emp = [
        Employment(employer="Bank", title="Dev", kind="full_time", start="2023-07", end="2026-10", provenance=prov),
        Employment(employer="Startup", title="Intern", kind="internship", start="2022-05", end="2022-08", provenance=prov),
        Employment(employer="Unknown", title="Dev", kind="full_time", start=None, end=None, provenance=prov),
    ]
    s = compute_experience(emp, as_of=date(2026, 11, 4))
    assert s.full_time_years_completed == 3  # 39 months -> 3 completed, never rounded up
    assert s.full_time_months == 39
    assert s.internship_months == 3
    assert s.total_years_completed == 3
    assert s.unknown_ranges == ["Unknown: start unknown"]


def test_salary_rule_requires_matching_base_band():
    comp = Compensation(floor_base=95000, target_base=100000, provenance=Provenance(source="t"))
    assert desired_salary(comp, None).outcome == "needs_input"
    band = BandEvidence(min_base=115000, max_base=125000, currency="CAD", title_matches=True, location_matches=True)
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


def test_exclusions_are_rules_not_first_names():
    prefs = default_preferences()
    jess = prefs.recipient_exclusions[0]
    assert jess.resolution == "unresolved" and jess.profile_url is None
    profile = build_profile_from_skills(FAKE_FACTS, FAKE_SALARY)
    assert profile.is_excluded_employer("Royal Bank of Canada") is not None
    assert profile.is_excluded_employer("Amazon Web Services") is not None
    assert profile.is_excluded_employer("Shopify") is None
    assert profile.compensation.floor_base == 95000
    assert "base_resume" in profile.missing_essentials()
