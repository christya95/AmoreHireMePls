"""Form label rules for the saved answer keys: every rule is field neutral (it names what a form asks, never a
profession or a place) and every value here is fictional."""

from __future__ import annotations

import pytest

from amorehiremepls.apply.applications import (
    FIELD_RULES,
    SAVED_ANSWER_REASON,
    SELF_ID_REASON,
    SENSITIVE,
    FormField,
    map_answers,
    own_personal_answer,
    saved_years,
    self_identification_answer,
)
from amorehiremepls.apply.ats.greenhouse import GreenhouseAdapter, questions_to_fields
from amorehiremepls.apply.ats.lever import LeverAdapter
from amorehiremepls.candidate.candidate_profile import SENSITIVE_KEYS
from amorehiremepls.candidate.profile import Fact, Provenance
from tests.fixtures.greenhouse_site import board_schema
from tests.fixtures.profile_fixture import build_test_profile
from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

PROV = Provenance(source="user:chat", confirmed_on="2026-11-05")
DECLINE = "I don't wish to answer"

# Saved answers a candidate in any profession might keep, keyed the way the Profile page stores them.
SAVED = {
    "legal_middle_name": "Quinn",
    "time_zone": "Eastern Time",
    "currently_employed": "Yes",
    "willing_to_relocate": "No",
    "willing_to_travel": "Yes",
    "travel_percentage": "Up to 10 percent",
    "overtime_willing": "Yes",
    "on_call_willing": "Yes",
    "languages": "English, French",
    "drivers_licence": "Yes",
    "security_clearance": "None",
    "background_check_consent": "Yes",
    "former_employer_contact": "Not my current employer",
    "references_policy": "Available on request",
    "relatives_at_employer": "No",
    "restrictive_agreements": "No",
    "outside_business_interests": "No",
    "reason_for_leaving_most_recent_role": "Looking for a permanent role with more scope",
    "cover_letter_preference": "Not required",
    "career_goals": "Grow into a lead role",
    "gpa": "3.8",
    "graduation_year": "2021",
    "program": "Example Diploma Program",
    "field_of_study": "Example Studies",
    "certifications": "Example Certificate",
}

# (form label, saved key it must reach). One line per rule, phrased as forms phrase them.
LABELS = [
    ("Middle name", "legal_middle_name"),
    ("Middle initial", "legal_middle_name"),
    ("What time zone are you in?", "time_zone"),
    ("Are you currently employed?", "currently_employed"),
    ("Are you willing to relocate for this role?", "willing_to_relocate"),
    ("Are you willing to travel?", "willing_to_travel"),
    ("What percentage of time are you able to travel?", "travel_percentage"),
    ("Are you willing to work overtime when needed?", "overtime_willing"),
    ("This role has an on-call rotation. Are you able to participate?", "on_call_willing"),
    ("Languages spoken", "languages"),
    ("Which languages are you fluent in?", "languages"),
    ("Do you hold a valid driver's licence?", "drivers_licence"),
    ("Do you currently hold a security clearance?", "security_clearance"),
    ("Do you consent to a background check?", "background_check_consent"),
    ("May we contact your current employer?", "former_employer_contact"),
    ("References", "references_policy"),
    ("Do you have any relatives employed here?", "relatives_at_employer"),
    ("Are you bound by a non-compete or non-solicitation agreement?", "restrictive_agreements"),
    ("Do you have any outside business interests to declare?", "outside_business_interests"),
    ("Reason for leaving your most recent role", "reason_for_leaving_most_recent_role"),
    ("Cover letter", "cover_letter_preference"),
    ("What are your career goals?", "career_goals"),
    ("GPA", "gpa"),
    ("Expected graduation date", "graduation_year"),
    ("Program", "program"),
    ("Field of study", "field_of_study"),
    ("Major", "field_of_study"),
    ("Certifications", "certifications"),
]


def _profile(**sensitive: str):
    p = build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY)
    p.answers += [Fact(category="answer", key=k, value=v, provenance=PROV) for k, v in SAVED.items()]
    p.facts += [Fact(category="self_identification", key=k, value=v, sensitivity="sensitive", provenance=PROV) for k, v in sensitive.items()]
    return p


def _rule_key(label: str) -> str | None:
    return next((k for pat, k in FIELD_RULES if pat.search(label)), None)


@pytest.mark.parametrize(("label", "key"), LABELS)
def test_each_saved_answer_key_has_a_label_rule(label, key):
    assert _rule_key(label) == key
    assert not SENSITIVE.search(label), label
    [a] = map_answers([FormField("q", label)], _profile(), {}, None)
    assert a.value == SAVED[key] and not a.needs_input and not a.sensitive


def test_new_rules_do_not_steal_the_old_questions():
    for label, key in [
        ("First name", "legal_first_name"),
        ("Last name", "legal_last_name"),
        ("Highest level of education completed", "degree_level"),
        ("School", "school"),
        ("What are your salary expectations?", "__salary__"),
        ("Resume/CV", "__resume__"),
        ("How many years of full-time experience do you have?", "__years_full_time__"),
        ("Notice period", "notice_period"),
        ("When could you start?", "availability"),
        ("Are you legally authorized to work in this country?", "work_authorization"),
        ("Will you now or in the future require sponsorship?", "requires_sponsorship"),
    ]:
        assert _rule_key(label) == key, label
    # "age" inside another word is not an age question
    assert not SENSITIVE.search("Language") and not SENSITIVE.search("Percentage of travel")
    assert SENSITIVE.search("Age") and SENSITIVE.search("What is your age?")


def test_yes_no_answers_pick_whole_word_options_never_a_word_that_merely_contains_them():
    p = _profile()
    f = FormField("rel", "Are you willing to relocate?", "select", ["Yes, I know the area", "No, I would rather not", "Maybe"])
    [a] = map_answers([f], p, {}, None)
    assert a.value == "No, I would rather not"  # "No" never matches the "no" inside "know"
    f2 = FormField("rel2", "Are you willing to relocate?", "select", ["Yes", "Maybe"])
    [a2] = map_answers([f2], p, {}, None)
    assert a2.value is None and a2.needs_input and "lacks" in a2.reason


def test_cover_letter_upload_stays_optional_and_the_text_preference_is_typed_elsewhere():
    p = _profile()
    upload, text = FormField("cl", "Cover letter", "file"), FormField("cl2", "Cover letter (optional text)", "textarea")
    by_id = {a.field_id: a for a in map_answers([upload, text], p, {}, None)}
    assert by_id["cl"].value is None and not by_id["cl"].needs_input and "optional" in by_id["cl"].reason
    assert by_id["cl2"].value == "Not required"


# ------------------------------------------------------------------ personal questions


def test_pronouns_and_citizenship_stay_blank_without_a_question_pattern_and_are_typed_with_one():
    p = _profile()
    p.answers += [
        Fact(category="answer", key="pronouns", value="they/them", provenance=PROV),
        Fact(category="answer", key="citizenship_status", value="Citizen", provenance=PROV),
    ]
    fields = [FormField("pr", "Pronouns"), FormField("cit", "Citizenship status", "select", ["Citizen", "Permanent resident", "Other"])]
    blank = {a.field_id: a for a in map_answers(fields, p, {}, None)}
    assert blank["pr"].value is None and blank["pr"].sensitive and blank["cit"].value is None and blank["cit"].sensitive
    for f in p.answers:
        if f.key == "pronouns":
            f.question_pattern = "pronouns?"
        if f.key == "citizenship_status":
            f.question_pattern = "citizenship"
    typed = {a.field_id: a for a in map_answers(fields, p, {}, None)}
    assert typed["pr"].value == "they/them" and typed["pr"].sensitive and typed["pr"].reason == SAVED_ANSWER_REASON
    assert typed["cit"].value == "Citizen" and typed["cit"].sensitive and typed["cit"].fact_ids
    # the pattern answer never overrides the form's decline option, and a free-text self-identification still stays blank
    assert own_personal_answer(FormField("cit2", "Citizenship status", "select", ["Citizen", DECLINE]), p, "Citizen") is None
    assert own_personal_answer(FormField("g", "Gender", "text"), p) is None


def test_veteran_and_indigenous_identity_answers_match_the_form_options_only_when_saved():
    vet = FormField("vet", "Veteran status", "select", ["I am a veteran", "I am not a veteran", DECLINE], required=True)
    assert self_identification_answer(vet, _profile(), DECLINE) is None
    assert self_identification_answer(vet, _profile(veteran_status="No"), DECLINE).value == "I am not a veteran"
    assert self_identification_answer(vet, _profile(veteran_status="Yes"), DECLINE).value == "I am a veteran"
    plain = FormField("vet2", "Are you a veteran?", "select", ["Yes", "No", DECLINE])
    a = self_identification_answer(plain, _profile(veteran_status="No"), DECLINE)
    assert a.value == "No" and a.sensitive and a.reason == SELF_ID_REASON
    ind = FormField("ind", "Do you identify as Indigenous (First Nations, Métis or Inuit)?", "select", ["Yes", "No", DECLINE])
    assert self_identification_answer(ind, _profile(), DECLINE) is None
    assert self_identification_answer(ind, _profile(indigenous_identity="No"), DECLINE).value == "No"
    groups = FormField("ind2", "Indigenous identity", "select", ["First Nations", "Métis", "Inuit", "Not Indigenous", DECLINE])
    assert self_identification_answer(groups, _profile(indigenous_identity="Métis"), DECLINE).value == "Métis"
    assert self_identification_answer(groups, _profile(indigenous_identity="No"), DECLINE).value == "Not Indigenous"
    assert SENSITIVE.search("Indigenous identity") and SENSITIVE.search("Military service")
    assert "indigenous_identity" in SENSITIVE_KEYS and "veteran_status" in SENSITIVE_KEYS


def test_adapters_use_the_same_personal_answer_path():
    p = _profile(veteran_status="No")
    p.answers.append(Fact(category="answer", key="pronouns", value="they/them", question_pattern="pronouns?", provenance=PROV))
    fields = [
        FormField("pr", "Pronouns", "text", meta={"name": "question_1", "section": "questions"}),
        FormField("vet", "Veteran status", "select", ["Veteran", "Not a veteran", DECLINE], required=True, meta={"name": "question_2", "section": "demographic"}),
    ]
    for adapter in (GreenhouseAdapter(), LeverAdapter()):
        by_id = {a.field_id: a for a in adapter.map_answers(fields, p, {}, None, None, "fair")}
        assert by_id["pr"].value == "they/them" and by_id["pr"].sensitive, adapter.name
        assert by_id["vet"].value == "Not a veteran" and by_id["vet"].sensitive, adapter.name


# ------------------------------------------------------------------ years of experience


def test_a_saved_years_answer_wins_for_its_own_subject_and_dates_decide_the_rest():
    p = _profile()
    p.answers += [
        Fact(category="answer", key="years_example", value="5", provenance=PROV),
        Fact(category="answer", key="years_example_tooling", value="2 years", provenance=PROV),
    ]
    fact, n = saved_years(p, "How many years of Example experience do you have?")
    assert fact is not None and fact.key == "years_example" and n == 5
    fact, n = saved_years(p, "Years of Example Tooling experience")
    assert fact.key == "years_example_tooling" and n == 2  # the longer, more specific key wins
    assert saved_years(p, "Years of professional experience") == (None, None)
    fields = [
        FormField("ex", "How many years of Example experience do you have?"),
        FormField("exb", "Years of Example experience", "select", ["0-2 years", "3-5 years", "6+ years"]),
        FormField("exn", "Years of Example experience", "select", ["1", "3", "5", "15"]),
        FormField("gen", "How many years of professional experience do you have?"),
    ]
    by_id = {a.field_id: a for a in map_answers(fields, p, {}, None)}
    assert by_id["ex"].value == "5" and by_id["ex"].reason == SAVED_ANSWER_REASON and by_id["ex"].fact_ids == [p.fact("years_example").id]
    assert by_id["exb"].value == "3-5 years"
    assert by_id["exn"].value == "5"  # digit bounded: never "15"
    assert "completed years computed" in by_id["gen"].reason and by_id["gen"].fact_ids == [e.id for e in p.employment]


def test_computed_years_land_in_the_range_option_when_the_question_offers_ranges():
    """A question with ranges takes the range holding the computed number; a bare number is never left beside a list."""
    from amorehiremepls.candidate.profile import compute_experience

    p = _profile()
    years = compute_experience(p.employment).full_time_years_completed
    assert years >= 1
    low, high = max(0, years - 1), years + 1
    fields = [
        FormField("r", "How many years of full-time experience do you have?", "select", [f"Less than {low} years", f"{low}-{high} years", f"{high + 1}+ years"]),
        FormField("d", "How many years of full-time experience do you have?", "select", [f"Less than {low} years", f"{low}–{high} years", f"more than {high} years"]),
        FormField("n", "How many years of full-time experience do you have?", "select", ["0", str(years), "20"]),
        FormField("x", "How many years of full-time experience do you have?", "select", [f"{years + 5}-{years + 8} years"]),
        FormField("t", "How many years of full-time experience do you have?"),
    ]
    by_id = {a.field_id: a for a in map_answers(fields, p, {}, None)}
    assert by_id["r"].value == f"{low}-{high} years" and not by_id["r"].needs_input and by_id["r"].reason == f"{years} completed years computed from dates"
    assert by_id["d"].value == f"{low}–{high} years"
    assert by_id["n"].value == str(years)  # a list of plain numbers still takes the exact number
    assert by_id["x"].value is None and by_id["x"].needs_input and "no range" in by_id["x"].reason
    assert by_id["t"].value == str(years)  # a free text box gets the number


def test_greenhouse_buckets_honour_a_saved_years_answer_for_the_matching_question():
    fields = questions_to_fields(board_schema("acme", "1"))
    years_q = next(f for f in fields if "years of full-time experience" in f.label.lower())
    computed = {a.field_id: a for a in GreenhouseAdapter().map_answers(fields, _profile(), {}, None, None, "fair")}[years_q.id]
    assert "computed from dates" in computed.reason
    p = _profile()
    p.answers.append(Fact(category="answer", key="years_full_time", value="3", provenance=PROV))
    saved = {a.field_id: a for a in GreenhouseAdapter().map_answers(fields, p, {}, None, None, "fair")}[years_q.id]
    assert saved.value == "2-4 years" and saved.reason == SAVED_ANSWER_REASON and saved.fact_ids == [p.fact("years_full_time").id]
