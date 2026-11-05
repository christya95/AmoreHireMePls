"""The app is profession neutral: everything personal comes from candidate/profile.toml (or the Profile page), and a
sales account executive's resume goes through parsing, tailoring, grounding and the page limit like a developer's.
Everything here is fictional."""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from datetime import date
from pathlib import Path

import pytest
from docx import Document

from amorehiremepls.apply.applications import FormField, map_answers
from amorehiremepls.apply.tailoring import (
    PAGE_LIMIT,
    Tailor,
    build_docx,
    check_grounding,
    deterministic_tailor,
    find_soffice,
    fit_to_page_limit,
    page_limit_block_reason,
    parse_base_resume,
    render_pdf,
)
from amorehiremepls.candidate.candidate_profile import (
    CONTROL_KEY,
    ImportReport,
    ProfileTomlError,
    apply_profile_toml,
    export_profile_toml,
    load_profile_toml,
    preference_fingerprint,
    toml_status,
)
from amorehiremepls.candidate.profile import Fact, Profile, Provenance, default_preferences
from amorehiremepls.core.ai import AIRunner, FakeProvider, Pricing
from amorehiremepls.core.skills import catalog
from amorehiremepls.core.util import sha256_file
from amorehiremepls.jobs.discovery import qualify
from amorehiremepls.outreach.outreach import template_connect_note, validate_connect_note
from tests.fixtures.make_resume import make_base_resume, make_sales_resume
from tests.fixtures.profile_fixture import build_test_profile, sales_preferences
from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "candidate" / "profile.example.toml"


# -- profile.toml -----------------------------------------------------------------------------------------------


def test_example_toml_is_fictional_sales_profile_and_loads_every_section():
    data = load_profile_toml(EXAMPLE)
    p = Profile()
    report = apply_profile_toml(p, data, confirmed_on="2026-11-05")
    assert isinstance(report, ImportReport)
    expected = {
        "candidate",
        "facts",
        "eligibility",
        "employment",
        "education",
        "answers",
        "skills",
        "preferences",
        "compensation",
        "exclusions",
        "outreach",
        "resume",
        "fit",
        "style",
    }
    assert set(report.sections) >= expected
    assert p.first_name() == "Jo" and p.profession() == "enterprise account executive"
    assert p.fact_value("legal_last_name") == "Sample" and p.fact_value("website_url") is None  # blank = unknown, not ""
    assert p.fact("pronouns").question_pattern == "pronouns" and p.fact("pronouns").category == "identity"  # inline table form
    assert p.compensation.currency == "GBP" and p.compensation.floor_base == 70000 and p.compensation.target_base == 85000
    assert p.preferences.work_authorization.startswith("Right to work") and p.fact("requires_sponsorship").value == "No"
    assert [e.kind for e in p.employment] == ["full_time", "full_time", "internship"] and p.employment[0].end is None and p.employment[0].end_known
    assert {s.name for s in p.skills} >= {"MEDDIC", "Salesforce"} and report.skills == 6
    assert p.preferences.role_families[0] == "account executive" and p.preferences.region_label == "the UK"
    assert p.preferences.resume.section_aliases == {"skills": ["toolbox", "sales stack"], "projects": ["deals of note"]}
    assert p.preferences.resume.allowed_terms == ["London", "Account Executive"]
    assert [a.words for a in p.preferences.fit.term_aliases] == [["CRM"], ["sales methodology"]]
    assert p.preferences.outreach_rules["role_phrase"] == "permanent enterprise sales roles" and "ask_other_permanent_backend_roles" not in p.preferences.outreach_rules
    assert p.is_excluded_employer("Initech UK") is not None and p.preferences.recipient_exclusions[0].resolution == "resolved"
    answers = {a.key: a for a in p.answers}
    assert answers["quota_attainment"].question_pattern == "quota attainment|percent of quota"
    # every fact carries the file's provenance, not a skill or a chat
    assert {f.provenance.source for f in p.facts + p.answers + p.education} == {"candidate:profile.toml"}
    # nothing in the example is anybody real
    text = EXAMPLE.read_text().lower()
    assert "example" in text and "joss" not in text and "toronto" not in text


def test_toml_round_trip_through_export_keeps_every_preference_block():
    p = Profile()
    apply_profile_toml(p, load_profile_toml(EXAMPLE), confirmed_on="2026-11-05")
    text = export_profile_toml(p)
    again = Profile()
    apply_profile_toml(again, tomllib.loads(text), confirmed_on="2026-11-05")
    assert preference_fingerprint(p) == preference_fingerprint(again)
    assert again.fact_value("website_url") is None and "website_url" in text  # unknown facts survive as ""
    assert again.fact("pronouns").question_pattern == "pronouns" and "match = " in text  # patterns, categories and sensitivity survive
    p.facts.append(Fact(category="other", key="veteran_status", value="No", sensitivity="sensitive", provenance=Provenance(source="t")))
    p.facts.append(Fact(category="contact", key="signal_handle", value="@x", sensitivity="sensitive", provenance=Provenance(source="t")))
    third = Profile()
    apply_profile_toml(third, tomllib.loads(export_profile_toml(p)), confirmed_on="2026-11-05")
    assert third.fact("veteran_status").sensitivity == "sensitive" and third.fact("signal_handle").sensitivity == "sensitive" and third.fact("signal_handle").category == "contact"
    only_rules = export_profile_toml(p, include_facts=False)
    assert "[facts]" not in only_rules and "[[employment]]" not in only_rules and "[preferences]" in only_rules


def test_toml_sections_present_replace_and_absent_keep(tmp_path):
    p = build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY)
    before = preference_fingerprint(p)
    report = apply_profile_toml(p, {"compensation": {"currency": "USD", "floor_base": 90000}}, confirmed_on="2026-11-05")
    after = preference_fingerprint(p)
    assert report.sections == ["compensation"] and p.compensation.currency == "USD" and p.compensation.floor_base == 90000
    assert after["compensation"] != before["compensation"] and all(after[k] == before[k] for k in before if k != "compensation")
    assert p.preferences.role_families and p.fact_value("email") == "alex@example.com"  # untouched
    # [facts] upserts by key; a blank value makes the fact unknown again
    apply_profile_toml(p, {"facts": {"email": "new@example.com", "phone": ""}}, confirmed_on="2026-11-05")
    assert p.fact_value("email") == "new@example.com" and p.fact("phone").value is None
    # invalid values are refused with a plain message and nothing half applied
    with pytest.raises(ProfileTomlError, match="band_rule"):
        apply_profile_toml(p, {"compensation": {"band_rule": "lots"}}, confirmed_on="2026-11-05")
    with pytest.raises(ProfileTomlError, match="kind"):
        apply_profile_toml(p, {"employment": [{"employer": "X", "kind": "gig"}]}, confirmed_on="2026-11-05")
    bad = tmp_path / "bad.toml"
    bad.write_text("[candidate\nfirst_name = 'x'")
    with pytest.raises(ProfileTomlError):
        load_profile_toml(bad)


def test_toml_status_tracks_the_loaded_fingerprint(tmp_path):
    class Controls:
        def __init__(self):
            self.v = {}

        def get_control(self, key, default=None):
            return self.v.get(key, default)

        def set_control(self, key, value):
            self.v[key] = value

    store = Controls()
    assert toml_status(tmp_path, store).state == "missing"
    path = tmp_path / "profile.toml"
    path.write_text(EXAMPLE.read_text())
    st = toml_status(tmp_path, store)
    assert st.state == "new" and st.sha256 == sha256_file(path)
    store.set_control(CONTROL_KEY, {"sha256": st.sha256, "loaded_at": "2026-11-05T00:00:00"})
    assert toml_status(tmp_path, store).state == "loaded"
    path.write_text(path.read_text() + "\n# edited\n")
    assert toml_status(tmp_path, store).state == "changed"


# -- a non technical candidate through the pipeline --------------------------------------------------------------


def test_sales_resume_parses_with_its_own_headings_and_keeps_unknown_sections_verbatim(tmp_path):
    path = make_sales_resume(tmp_path / "sales.docx")
    base = parse_base_resume(path, rules=sales_preferences().resume)
    assert base.contact_lines[0] == "Jo Sample"
    assert base.summary.startswith("Enterprise account executive")
    assert "MEDDIC" in base.skills and "Salesforce" in base.skills  # Core Competencies -> skills
    assert len(base.experience) == 3 and base.experience[2].is_internship and not base.experience[0].is_internship  # Sales Experience
    assert base.projects and base.projects[0].title.startswith("President's Club")  # Key Achievements -> projects
    assert base.education == ["BA Business Studies, Example University"]
    assert base.certifications == ["MEDDPICC Certified Practitioner"]  # Licences & Certifications
    assert base.other == [("Languages", ["English (native), Spanish (professional)"]), ("Volunteer Work", ["Mentor, Sales Careers Network, 2023 to present"])]
    assert "other" not in base.as_sections()  # never offered to the model for rewriting
    # the developer fixture still parses exactly as before (a certificate name is content, not a heading)
    dev = parse_base_resume(make_base_resume(tmp_path / "dev.docx"))
    assert dev.certifications == ["AWS Certified Cloud Practitioner"] and dev.other == []


def test_sales_resume_tailors_grounds_and_rebuilds_with_other_sections_untouched(tmp_path):
    path = make_sales_resume(tmp_path / "sales.docx")
    rules = sales_preferences()
    base = parse_base_resume(path, rules=rules.resume)
    posting = "Enterprise Account Executive selling to CFOs with MEDDIC and Salesforce. Forecast accuracy matters."
    tailored = deterministic_tailor(base, posting, max_bullets_per_role=3)
    assert all(len(e["bullets"]) <= 3 for e in tailored["experience"])
    assert tailored["skills"][0] in ("MEDDIC", "Salesforce", "Enterprise sales")  # posting terms sort first
    assert check_grounding(tailored, base, allowed_terms=rules.resume.allowed_terms, no_dashes=rules.style.no_dashes).ok
    # a rewritten bullet naming a tool or client the resume never mentions is rejected; a sales word is not "technology"
    bad = {**tailored, "experience": [{"experience_id": "exp_1", "bullets": [{"source_id": "b_1", "text": "Closed Initrode using Gong call reviews."}]}]}
    report = check_grounding(bad, base)
    assert not report.ok and any("Gong" in v for v in report.violations) and any("Initrode" in v for v in report.violations)
    assert not any("Closed" in v for v in report.violations)
    # allowed_terms lets the candidate's own words through; the dash rule is the candidate's choice
    dashed = {**tailored, "summary": "Enterprise AE in London - finance platforms"}
    assert "hyphen" in " ".join(check_grounding(dashed, base).violations)
    assert check_grounding(dashed, base, allowed_terms=["London"], no_dashes=False).ok
    # the template build keeps the verbatim sections and the fresh build emits them after the known sections
    out = build_docx(base, tailored, tmp_path / "out.docx")
    texts = [p.text for p in Document(str(out)).paragraphs if p.text.strip()]
    assert "Languages" in texts and "Mentor, Sales Careers Network, 2023 to present" in texts
    fresh_base = parse_base_resume(path, rules=rules.resume)
    fresh_base.source_path = tmp_path / "gone.docx"
    fresh = build_docx(fresh_base, tailored, tmp_path / "fresh.docx")
    fresh_texts = [p.text for p in Document(str(fresh)).paragraphs if p.text.strip()]
    assert fresh_texts.index("LANGUAGES") > fresh_texts.index("CERTIFICATIONS") and "VOLUNTEER WORK" in fresh_texts and "SKILLS" in fresh_texts


def test_page_limit_comes_from_the_profile(tmp_path):
    path = make_sales_resume(tmp_path / "sales.docx")
    base = parse_base_resume(path)
    tailored = deterministic_tailor(base, "sales", max_bullets_per_role=10)

    def render_pages(n: int):
        def render(docx_path: Path) -> dict:
            pdf = docx_path.with_suffix(".pdf")
            pdf.write_bytes(b"%PDF-1.4\n%fixture\n")
            return {"pdf_path": str(pdf), "pages": n, "verified": n <= 1, "selectable_text": True, "reason": "" if n <= 1 else f"{n} pages exceeds one"}

        return render

    # one page allowed, two rendered: the trim loop runs; two pages allowed: nothing to do
    _fitted, pdf, trimmed = fit_to_page_limit(base, tailored, "sales", tmp_path / "a.docx", render=render_pages(2), max_pages=1)
    assert trimmed and pdf["pages"] == 2
    _fitted, pdf, trimmed = fit_to_page_limit(base, tailored, "sales", tmp_path / "b.docx", render=render_pages(2), max_pages=2)
    assert not trimmed
    assert page_limit_block_reason({"page_limit_verified": False}, 1) == "resume PDF is not verified at one page or fewer"
    assert page_limit_block_reason({"page_limit_verified": False}) == "resume PDF is not verified at two pages or fewer"
    assert page_limit_block_reason({"page_limit_verified": True, "pdf_path": "x.pdf"}, 3) is None
    assert PAGE_LIMIT == 2
    if find_soffice():
        one = render_pdf(build_docx(base, tailored, tmp_path / "real.docx"), max_pages=1)
        assert one["pages"] >= 1 and (one["verified"] or "exceeds one" in one["reason"])


@pytest.mark.mongo
def test_tailor_service_reads_page_limit_bullets_and_profession_from_preferences(store, tmp_path):
    base_path = make_sales_resume(tmp_path / "sales.docx")
    prefs = sales_preferences()
    prefs.resume.max_pages = 3
    prefs.resume.max_bullets_per_role = 2
    seen: list[dict] = []

    def respond(user: str) -> dict:
        seen.append({"user": user})
        base = parse_base_resume(base_path)
        return {
            "summary": base.summary,
            "skills": list(base.skills),
            "experience": [{"experience_id": e.id, "bullets": [{"source_id": b.id, "text": b.text} for b in e.bullets]} for e in base.experience],
        }

    provider = FakeProvider({"TailorOutput": respond})
    tailor = Tailor(store, tmp_path / "artifacts", AIRunner(store, provider, catalog, Pricing(None)), preferences=prefs, profession="enterprise account executive")
    assert tailor.max_pages == 3 and tailor.max_bullets == 2
    snapshot = {"text": "MEDDIC Salesforce", "posting_hash": "p"}
    job = {"_id": "job_s1", "employer": "Ledgerly", "title": "Senior Account Executive", "requisition_id": "S1", "latest_snapshot": snapshot}
    art = tailor.tailor_for_job(job, base_path, sha256_file(base_path), fact_version=1, display_name="Jo Sample")
    assert art["method"].startswith("model") and art["grounding"]["ok"]
    sent = seen[0]["user"]
    assert "3 pages" in sent and "max_bullets_per_role: 2" in sent and "enterprise account executive" in sent and "account executive" in sent
    assert provider.calls and "page_budget" in provider.calls[0]["system"] and "backend" not in provider.calls[0]["system"].lower()


def test_sales_candidate_rules_drive_qualification_answers_and_outreach():
    p = Profile()
    apply_profile_toml(p, load_profile_toml(EXAMPLE), confirmed_on="2026-11-05")
    job = {"title": "Enterprise Account Executive", "employer": "Example Corp", "location": "London, UK"}
    q = qualify(job, {"employment_type": "permanent"}, p, "Permanent enterprise account executive role in London. GBP 75,000 to 90,000 base.")
    assert q.decision in ("eligible", "needs_review") and q.location_ok
    remote = qualify({**job, "location": "Remote"}, {"remote_policy": "remote"}, p, "Fully remote. Open to candidates anywhere in Europe.")
    assert remote.decision == "needs_review" and any("the UK residents" in r for r in remote.reasons)
    away = qualify({**job, "location": "Austin, Texas"}, {}, p, "On site in Austin.")
    assert away.decision == "excluded" and "outside the UK" in away.reasons[0]
    sdr = qualify({**job, "title": "SDR Team Lead"}, {}, p, "London based.")
    assert sdr.decision == "excluded" and "sdr" in sdr.reasons[0]
    kept = qualify({**job, "title": "Strategic Account Director"}, {}, p, "London based.")
    assert kept.decision != "excluded"  # the exception phrase wins over the excluded word "director"
    tiny = qualify(job, {}, p, "You would be our first sales hire in London.")
    assert "small_startup" in " ".join(tiny.flags) or tiny.decision == "needs_review"
    # a field specific years question and the candidate's own recurring question both map without code changes
    fields = [
        FormField("y", "How many years of B2B sales experience do you have?", "text"),
        FormField("m", "Which sales methodology do you use?", "text"),
        FormField("q", "What was your quota attainment last year?", "text"),
        FormField("g", "Gender", "select", options=["Man", "Woman", "Prefer not to say"]),
    ]
    answers = {a.field_id: a for a in map_answers(fields, p, {}, None, as_of=date(2026, 1, 15))}
    assert answers["y"].value == "6" and answers["m"].value.startswith("MEDDIC") and answers["q"].value.startswith("112 percent")  # 2019-06 to 2026-01, intern months excluded
    assert answers["g"].value is None and answers["g"].reason == "sensitive disclosure: left blank unless you supply a preference"
    # the Connect note speaks in the candidate's words, in their field, signed with their name
    rules = p.preferences.outreach_rules
    kwargs = {"one_liner": rules["one_liner"], "role_phrase": rules["role_phrase"], "sign_off_name": p.first_name()}
    note = template_connect_note("Sam", "Example Corp", "Enterprise Account Executive", **kwargs)
    assert validate_connect_note(note, "Enterprise Account Executive", rules).ok
    assert "data platforms" in note and "enterprise sales roles" in note and note.endswith("Thanks, Jo") and "backend" not in note


def test_neutral_profile_never_drops_a_gap_and_asks_about_location_only_when_rules_exist():
    p = Profile()
    p.preferences = default_preferences()
    q = qualify({"title": "Clinical Psychologist", "employer": "Example Clinic", "location": "Lisbon"}, {}, p, "Permanent clinical psychologist role.")
    assert q.decision != "excluded" and q.location_ok is True  # no location rule saved: nothing to check
    assert not any("outside" in r for r in q.reasons)


# -- the tracker is optional ------------------------------------------------------------------------------------------


@pytest.mark.mongo
def test_tracker_off_records_the_application_without_a_github_queue(store, monkeypatch):
    from amorehiremepls.core.config import get_settings
    from amorehiremepls.records.sync import SyncCoordinator, SyncOutbox

    store.c("applications").insert_one({"_id": "app_t1", "job_id": "job_t1", "status": "submitted", "sync": {"state": "sync_pending"}})
    monkeypatch.setenv("AMORE_TRACKER_ENABLED", "false")
    get_settings.cache_clear()
    item = SyncOutbox(store).enqueue("app_t1")
    assert item["status"] == "not_applicable" and "AMORE_TRACKER_ENABLED" in item["note"]
    assert store.c("applications").find_one({"_id": "app_t1"})["sync"]["state"] == "not_applicable"
    assert SyncOutbox(store).pending() == []
    status = SyncCoordinator(store, Path("/nonexistent/clone")).status()
    assert status["enabled"] is False and status["clone_present"] is False and status["outbox"] == {"not_applicable": 1}
    monkeypatch.setenv("AMORE_TRACKER_ENABLED", "true")
    get_settings.cache_clear()
    store.c("applications").insert_one({"_id": "app_t2", "job_id": "job_t2", "status": "submitted", "sync": {"state": "sync_pending"}})
    assert SyncOutbox(store).enqueue("app_t2")["status"] == "sync_pending"


# -- no owner literals in the code --------------------------------------------------------------------------------


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_src_contains_no_profession_city_employer_or_owner_literals():
    """The owner's first name is checked as a hash (tests/fixtures/src_only_token_hashes.txt) so this file never spells it.
    Profession, city and employer words are checked as plain words: none of them may appear in src/ as a literal."""
    hashes = {ln.strip() for ln in (REPO / "tests" / "fixtures" / "src_only_token_hashes.txt").read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    assert hashes
    banned_words = {"toronto", "ontario", "mississauga", "milton", "faang", "christya95"}  # employer names stay allowed in the public logo map
    tracked = subprocess.run(["git", "ls-files", "src"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    assert tracked
    hits: list[tuple[str, str]] = []
    for rel in tracked:
        p = REPO / rel
        if p.suffix.lower() not in {".py", ".html", ".js", ".css", ".md", ".txt", ".json"}:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]+", text):
            low = word.lower()
            if _sha(low) in hashes or low in banned_words:
                hits.append((rel, low[:2] + "*" * (len(low) - 2)))
    # the alias filters keep an older zone name only as an alias key, never as copy (they live in chrome.py)
    hits = [h for h in hits if not (h[0].endswith(("web.py", "chrome.py")) and h[1].startswith("to"))]
    assert not hits, f"owner or profession literals in src/: {sorted(set(hits))}"
    # and the persona prompt is neutral
    persona = catalog.get("assess_fit").system_prompt.lower()
    assert "software" not in persona and "backend" not in persona and "candidate_profession" in persona


def test_profile_models_default_to_nothing_personal():
    p = Profile()
    assert p.preferences.locations == [] and p.preferences.excluded_employers == [] and p.compensation.currency == ""
    assert p.preferences.resume.max_pages == 2 and p.preferences.style.no_dashes and p.preferences.outreach_rule("ask_other_roles")
    p.preferences.outreach_rules = {"ask_other_permanent_backend_roles": False}
    assert p.preferences.outreach_rule("ask_other_roles") is False  # a legacy key saved by an older version still counts
    p.facts.append(Fact(category="identity", key="preferred_name", value="Sam Lee", provenance=Provenance(source="t")))
    assert p.first_name() == "Sam"


# -- the Profile page's Load button ----------------------------------------------------------------------------------


@pytest.mark.mongo
def test_profile_page_shows_the_file_state_and_the_load_button_applies_it(store, tmp_path):
    from fastapi.testclient import TestClient

    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.runtime import Runtime
    from amorehiremepls.web.web import create_app
    from tests.test_ui_settings import TEST_MONGO_URI

    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        candidate_dir=candidate_dir,
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    client = TestClient(create_app(Runtime(s, store=store)), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/profile")
    assert page.status_code == 200 and "No <code>profile.toml</code>" in page.text and 'action="/profile/load-toml"' not in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    # the route needs a session and the CSRF token; a missing file is reported, not crashed on
    assert client.post("/profile/load-toml", data={"csrf": "wrong"}).status_code in (400, 403)
    (candidate_dir / "profile.toml").write_text(EXAMPLE.read_text())
    page = client.get("/profile")
    assert "not loaded yet" in page.text and 'action="/profile/load-toml"' in page.text
    r = client.post("/profile/load-toml", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("ok=toml_loaded")
    page = client.get("/profile?ok=toml_loaded")
    assert "Your profile file was loaded" in page.text and "enterprise account executive" in page.text and "Last load: facts" in page.text
    rt = client.app.state.rt
    assert rt.profile.first_name() == "Jo" and rt.profile.compensation.currency == "GBP"
    assert store.get_control(CONTROL_KEY)["sha256"] == sha256_file(candidate_dir / "profile.toml")
    assert store.get_control("candidate_profile_toml_last_report")["ok"] is True
    assert store.get_control("discovery_sources") and store.get_control("discovery_sources")[0]["type"] == "greenhouse"
    # the greeting uses the name from the file
    assert "Jo" in client.get("/").text
    # an edit to the file shows as changed until it is loaded again; a broken file is refused with a plain message
    (candidate_dir / "profile.toml").write_text(EXAMPLE.read_text() + "\n[style]\nno_dashes = false\n")
    assert "changed since" in client.get("/profile").text.lower()
    (candidate_dir / "profile.toml").write_text("[compensation]\nband_rule = 'lots'\n")
    r = client.post("/profile/load-toml", data={"csrf": csrf})
    assert r.status_code == 303 and r.headers["location"].endswith("err=toml_invalid")
    assert rt.profile.compensation.currency == "GBP"  # nothing changed
    assert store.get_control("candidate_profile_toml_last_report")["ok"] is False
