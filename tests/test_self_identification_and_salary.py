"""Self-identification answers from saved facts, the salary proposal path, link facts, work arrangements and the
posting refresh. Every value here is fictional."""

from __future__ import annotations

import httpx
import pytest

from amorehiremepls.ai import AIRunner, Budget, FakeProvider, Pricing
from amorehiremepls.applications import (
    SALARY_RESEARCH_FACT,
    SALARY_RESEARCH_REASON,
    SELF_ID_REASON,
    FormField,
    SalaryProposal,
    SalaryResearcher,
    map_answers,
    self_identification_answer,
    seniority_hints,
)
from amorehiremepls.ats.greenhouse import GreenhouseAdapter, questions_to_fields
from amorehiremepls.discovery import compact_facts, fetch_posting_text, qualify, work_arrangement, workday_json_url
from amorehiremepls.orchestration import _compact_package
from amorehiremepls.profile import BandEvidence, Compensation, Fact, Provenance
from amorehiremepls.skills import catalog
from amorehiremepls.web import saved_form_inputs
from tests.fixtures.greenhouse_site import board_schema
from tests.fixtures.profile_fixture import build_test_profile
from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

PROV = Provenance(source="user:chat", confirmed_on="2026-11-05")


def _sensitive(key: str, value: str) -> Fact:
    return Fact(category="self_identification", key=key, value=value, sensitivity="sensitive", provenance=PROV)


def _profile(**self_id: str):
    p = build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY)
    p.facts += [_sensitive(k, v) for k, v in self_id.items()]
    return p


DECLINE = "I don't wish to answer"


# ------------------------------------------------------------------ self-identification


def test_gender_matches_the_option_that_carries_the_saved_answer_and_never_the_other_one():
    p = _profile(gender="Female")
    f = FormField("g", "How do you currently describe your gender identity?", "select", ["Man, male or masculine", "Woman, female or feminine", DECLINE], required=True)
    a = self_identification_answer(f, p, DECLINE)
    assert a is not None and a.value == "Woman, female or feminine" and a.sensitive and a.reason == SELF_ID_REASON and not a.needs_input
    assert a.fact_ids == [p.fact("gender").id]
    binary = FormField("g2", "Gender", "select", ["Male", "Female", "Non-binary", "Prefer not to say"])
    assert self_identification_answer(binary, p).value == "Female"
    assert self_identification_answer(binary, _profile(gender="Male")).value == "Male"
    # a form whose options do not carry the answer keeps the explicit decline / blank
    assert self_identification_answer(FormField("g3", "Gender", "select", ["Non-binary", "Prefer not to say"]), p) is None


def test_disability_picks_the_affirmative_or_negative_statement_and_skips_the_decline():
    opts = ["Yes, I have a disability, or have had one in the past", "No, I do not have a disability", DECLINE]
    f = FormField("d", "Disability status", "select", opts, required=True)
    assert self_identification_answer(f, _profile(disability_status="Yes"), DECLINE).value == opts[0]
    assert self_identification_answer(f, _profile(disability_status="No"), DECLINE).value == opts[1]
    worded = FormField("d2", "Do you have a disability?", "select", ["I have a disability", "I don't have a disability", DECLINE])
    assert self_identification_answer(worded, _profile(disability_status="Yes")).value == "I have a disability"
    assert self_identification_answer(FormField("d3", "Please describe any disability", "text"), _profile(disability_status="Yes")) is None


def test_orientation_ethnicity_and_visible_minority_rules_are_deterministic():
    orient = FormField("o", "Sexual orientation", "select", ["Bisexual", "Gay or lesbian", "Heterosexual or straight", DECLINE])
    assert self_identification_answer(orient, _profile(sexual_orientation="Straight")).value == "Heterosexual or straight"
    assert self_identification_answer(orient, _profile(sexual_orientation="Bisexual")).value == "Bisexual"
    assert self_identification_answer(orient, _profile(sexual_orientation="Asexual")) is None
    race = FormField("r", "Race/Ethnicity", "select", ["Asian", "Black or African American", "Hispanic or Latino", "White", "Two or more races", DECLINE])
    assert self_identification_answer(race, _profile(ethnicity="East Asian")).value == "Asian"  # the broader option when the exact group is absent
    detailed = FormField("r2", "Race/Ethnicity", "select", ["East Asian", "Southeast Asian", "West Asian", "Black", DECLINE])
    assert self_identification_answer(detailed, _profile(ethnicity="East Asian")).value == "East Asian"
    assert self_identification_answer(race, _profile(ethnicity="Middle Eastern")) is None
    yes_no = FormField("v", "Are you a member of a visible minority?", "select", ["Yes", "No", DECLINE])
    assert self_identification_answer(yes_no, _profile(visible_minority="Yes")).value == "Yes"
    assert self_identification_answer(yes_no, _profile(visible_minority="No")).value == "No"
    groups = FormField("v2", "Visible minority group (if applicable)", "select", ["Black", "East Asian", "Filipino", DECLINE])
    assert self_identification_answer(groups, _profile(visible_minority="Yes", ethnicity="Filipino")).value == "Filipino"
    # no saved fact for veterans, pronouns or religion: nothing is picked
    vet = FormField("vet", "Veteran status", "select", ["I am a veteran", "I am not a veteran", DECLINE], required=True)
    assert self_identification_answer(vet, _profile(gender="Female", disability_status="Yes"), DECLINE) is None


def test_generic_and_greenhouse_mapping_use_the_saved_answers_and_keep_the_decline_otherwise(tmp_path):
    fields = [
        FormField("gen", "Gender", "select", ["Male", "Female", "Decline"]),
        FormField("vet", "Veteran status", "select", ["Veteran", "Not a veteran", "Decline"], required=True),
    ]
    answers = {a.field_id: a for a in map_answers(fields, _profile(gender="Female"), {}, None)}
    assert answers["gen"].value == "Female" and answers["gen"].sensitive and answers["gen"].reason == SELF_ID_REASON
    assert answers["vet"].value is None and answers["vet"].sensitive and answers["vet"].needs_input
    gh = {a.field_id: a for a in GreenhouseAdapter().map_answers(questions_to_fields(board_schema("acme", "1")), _profile(gender="Female"), {}, None, None, "fair")}
    assert gh["4000"].value == "Woman, female or feminine" and gh["4000"].sensitive and gh["4000"].reason == SELF_ID_REASON
    assert gh["4001"].value is None and gh["4001"].sensitive  # categories question: no saved fact, optional, stays blank
    without = {a.field_id: a for a in GreenhouseAdapter().map_answers(questions_to_fields(board_schema("acme", "1")), _profile(), {}, None, None, "fair")}
    assert without["4000"].value == DECLINE


def test_sensitive_answers_stay_out_of_run_records_and_replayed_inputs_and_model_facts():
    own = {"field_id": "gen", "label": "Gender", "value": "Female", "sensitive": True, "fact_ids": ["f1"]}
    app = {"review_package": {"answers": [own, {"field_id": "fn", "label": "First name", "value": "Alex"}]}}
    assert [a["label"] for a in _compact_package(app)["answers"]] == ["First name"]
    assert saved_form_inputs(app) == {"fn": "Alex"}
    p = _profile(gender="Female", ethnicity="East Asian", disability_status="Yes", sexual_orientation="Bisexual")
    joined = "\n".join(compact_facts(p, "posting text")).lower()
    for word in ("female", "east asian", "bisexual", "disability"):
        assert word not in joined


def test_build_plan_marks_self_identification_steps_sensitive():
    fields = questions_to_fields(board_schema("acme", "1"))
    from dataclasses import asdict

    job = {"_id": "job_x", "form_plan": {"fields": [asdict(f) for f in fields]}}
    pkg = {"apply_url": "https://job-boards.greenhouse.io/acme/jobs/1"}
    answers = [{"field_id": "4000", "value": "Woman, female or feminine", "sensitive": True}, {"field_id": "first_name", "value": "Alex"}]
    steps = {s.selector.split(",")[0]: s for s in GreenhouseAdapter().build_plan(job, pkg, answers).steps}
    assert steps['[id="4000"]'].sensitive and not steps["#first_name"].sensitive


# ------------------------------------------------------------------ salary


def _salary_field(required=True):
    return FormField("sal", "What are your salary expectations?", "text", required=required)


def test_posted_range_decides_in_code_and_never_goes_under_the_floor():
    p = _profile()
    p.compensation = Compensation(currency="CAD", floor_base=96000, target_base=110000, provenance=PROV)
    inside = BandEvidence(min_base=102000, max_base=128000, currency="CAD", title_matches=True, location_matches=True)
    calls = []

    def never():
        calls.append(1)
        return SalaryProposal(amount=1, rationale="x")

    a = map_answers([_salary_field()], p, {}, None, inside, salary_case=never)[0]
    assert a.value == "115000" and a.fact_ids == ["compensation_rule"] and not calls
    straddle = BandEvidence(min_base=80000, max_base=102000, currency="CAD", title_matches=True, location_matches=True)
    a = map_answers([_salary_field()], p, {}, None, straddle, salary_case=never)[0]
    assert a.value == "96000" and "raised to your minimum" in a.reason and not calls
    below = BandEvidence(min_base=70000, max_base=90000, currency="CAD", title_matches=True, location_matches=True)
    a = map_answers([_salary_field()], p, {}, None, below, salary_case=never)[0]
    assert a.value is None and a.needs_input and a.reason == "posted range is below your minimum" and not calls


def test_without_a_range_one_researched_proposal_is_stored_as_an_editable_answer():
    p = _profile()
    p.compensation = Compensation(currency="CAD", floor_base=96000, target_base=110000, provenance=PROV)
    proposal = SalaryProposal(105000, "Typical for this title in the city.", confidence="medium", source_kinds=["salary surveys"], range_low=96000, range_high=120000)
    calls = []

    def once():
        calls.append(1)
        return proposal

    fields = [_salary_field(), FormField("sal2", "Expected compensation", "text")]
    answers = {a.field_id: a for a in map_answers(fields, p, {}, None, None, salary_case=once)}
    assert answers["sal"].value == "105000" and answers["sal"].fact_ids == [SALARY_RESEARCH_FACT] and answers["sal"].reason == SALARY_RESEARCH_REASON
    assert not answers["sal"].needs_input and answers["sal"].detail["rationale"].startswith("Typical") and answers["sal"].detail["confidence"] == "medium"
    assert answers["sal2"].value == "105000" and len(calls) == 2  # map_answers calls the hook per field; the service memoises it
    # a proposal under the floor is raised; no proposal keeps the old needs_input outcome; no salary field never calls the hook
    low = map_answers([_salary_field()], p, {}, None, None, salary_case=lambda: SalaryProposal(amount=80000, rationale="r"))[0]
    assert low.value == "96000"
    none = map_answers([_salary_field()], p, {}, None, None, salary_case=lambda: None)[0]
    assert none.value is None and none.needs_input and none.reason.startswith("salary rule")
    calls.clear()
    map_answers([FormField("fn", "First name")], p, {}, None, None, salary_case=once)
    assert not calls


def test_salary_researcher_runs_the_skill_once_through_the_runner_and_enforces_the_floor():
    p = _profile()
    p.version = 7
    p.compensation = Compensation(currency="CAD", floor_base=96000, target_base=101000, provenance=PROV)
    seen: list[str] = []

    def reply(user: str) -> dict:
        seen.append(user)
        return {"proposed_base": 88000, "range_low": 85000, "range_high": 105000, "rationale": "Market pay.", "confidence": "low", "source_kinds": ["public salary surveys"]}

    provider = FakeProvider({"SalaryCaseOutput": reply})
    runner = AIRunner(None, provider, catalog, Pricing(None), Budget(10_000, 1.0, 10, 60))
    snapshot = {"text": "You bring 5+ years of experience building APIs.", "posting_hash": "ph"}
    job = {"title": "Senior Backend Developer II", "location": "Toronto, Ontario", "latest_snapshot": snapshot}
    prop = SalaryResearcher(runner, p).propose(job)
    assert prop is not None and prop.amount == 101000  # estimate fell under the floor: the floor-respecting target is used
    assert prop.confidence == "low" and prop.source_kinds == ["public salary surveys"] and prop.range_high == 105000
    assert "candidate_floor: 96000" in seen[0] and "<untrusted" in seen[0] and "5+ years of experience" in seen[0]
    assert "Alex" not in seen[0] and "alex@example.com" not in seen[0]  # nothing but title, location, hints and the pay rules
    from amorehiremepls.ai import NoProvider

    assert SalaryResearcher(AIRunner(None, NoProvider(), catalog, Pricing(None)), p).propose(job) is None
    assert SalaryResearcher(None, p).propose(job) is None


def test_seniority_hints_are_short_and_deterministic():
    hints = seniority_hints("Senior Software Developer II", "We need 5+ years of professional experience and 2-3 years of Java experience. Also 7 years.")
    assert hints[:2] == ["Senior", "II"] and any(h.startswith("5+ years") for h in hints) and any(h.startswith("2-3 years") for h in hints)
    assert len(hints) <= 6 and seniority_hints("", "") == []


# ------------------------------------------------------------------ links, availability, work arrangements


def test_link_and_availability_facts_fill_the_matching_questions():
    p = _profile()
    p.facts += [
        Fact(category="contact", key="github_url", value="https://github.com/example-dev", provenance=PROV),
        Fact(category="contact", key="phone_country_code", value="+1", provenance=PROV),
        Fact(category="answer", key="availability", value="Immediately", provenance=PROV),
        Fact(category="answer", key="notice_period", value="None", provenance=PROV),
    ]
    fields = [
        FormField("gh", "GitHub profile"),
        FormField("web", "Personal website or portfolio"),
        FormField("cc", "Phone country code"),
        FormField("ph", "Phone"),
        FormField("start", "When could you start?"),
        FormField("notice", "Notice period"),
    ]
    answers = {a.field_id: a for a in map_answers(fields, p, {}, None)}
    assert answers["gh"].value == "https://github.com/example-dev"
    assert answers["web"].value == "https://github.com/example-dev"  # falls back to the GitHub link when no website is saved
    assert answers["cc"].value == "+1" and answers["ph"].value == "555 000 0000"
    assert answers["start"].value == "Immediately" and answers["notice"].value == "None"


def test_work_arrangement_only_matters_when_a_kind_is_not_accepted():
    assert work_arrangement("Hybrid") == "hybrid" and work_arrangement("on-site") == "onsite" and work_arrangement("Remote (Canada)") == "remote"
    assert work_arrangement("flexible") is None and work_arrangement(None) is None
    p = _profile()
    job = {"employer": "Acme", "title": "Backend Developer", "location": "Toronto"}
    text = "Permanent full-time backend developer role in Toronto, on-site."
    assert p.preferences.work_arrangements == ["remote", "hybrid", "onsite"]
    q = qualify(job, {"employment_type": "permanent", "remote_policy": "onsite"}, p, text)
    assert q.decision == "eligible" and "work_arrangement_not_accepted" not in q.flags
    p.preferences.work_arrangements = ["remote", "hybrid"]
    q = qualify(job, {"employment_type": "permanent", "remote_policy": "onsite"}, p, text)
    assert q.decision == "needs_review" and "work_arrangement_not_accepted" in q.flags and any("onsite" in r for r in q.reasons)


# ------------------------------------------------------------------ posting refresh


def test_workday_postings_are_read_through_the_public_json_endpoint_and_others_as_html():
    url = "https://acme.wd3.myworkdayjobs.com/en-US/External/job/Backend-Developer_R1"
    assert workday_json_url(url) == "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External/job/Backend-Developer_R1"
    assert workday_json_url("https://careers.example.com/jobs/1") is None
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if "wday/cxs" in str(request.url):
            return httpx.Response(200, json={"jobPostingInfo": {"title": "Backend Developer", "location": "Toronto", "jobDescription": "<p>Build <b>APIs</b> in Java.</p>"}})
        return httpx.Response(200, text="<html><head><style>p{}</style><script>var x=1;</script></head><body><h1>Role</h1><p>Permanent role.</p></body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    text, source, raw = fetch_posting_text(url, client)
    assert text == "Backend Developer Toronto Build APIs in Java." and source == "workday_json" and raw is None
    text, source, raw = fetch_posting_text("https://careers.example.com/jobs/1", client)
    assert text == "Role Permanent role." and source == "url_refresh" and raw and "<script>" in raw
    assert all(m == "GET" for m, _ in seen)


@pytest.mark.mongo
def test_refresh_posting_stores_a_snapshot_only_when_text_came_back(store):
    from amorehiremepls.discovery import Discovery
    from amorehiremepls.jobs import JobStore

    jobs = JobStore(store)
    job, _ = jobs.upsert(employer="Acme", title="Backend Developer", posting_url="https://acme.wd3.myworkdayjobs.com/en-US/External/job/Backend-Developer_R1", source="url_import")
    body = "<p>" + "Build APIs in Java with a permanent team in Toronto. " * 8 + "</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobPostingInfo": {"title": "Backend Developer", "jobDescription": body}})

    d = Discovery(store, _profile())
    out = d.refresh_posting(job["_id"], client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["stored"] and out["chars"] > 200 and out["source"] == "workday_json"
    fresh = jobs.get(job["_id"])
    assert fresh["latest_snapshot"]["text"].startswith("Backend Developer Build APIs") and fresh["status"] == job["status"]
    empty = d.refresh_posting(job["_id"], client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    assert not empty["stored"] and empty["chars"] == 0 and jobs.get(job["_id"])["latest_snapshot"]["text"] == fresh["latest_snapshot"]["text"]
