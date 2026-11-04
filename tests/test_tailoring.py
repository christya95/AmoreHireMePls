import pytest

from amorehiremepls.ai import AIRunner, FakeProvider, Pricing
from amorehiremepls.skills import catalog
from amorehiremepls.tailoring import Tailor, build_docx, check_grounding, deterministic_tailor, find_soffice, parse_base_resume, verify_docx_structure
from amorehiremepls.util import sha256_file
from tests.fixtures.make_resume import make_base_resume


def test_parse_base_resume_sections(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    assert base.contact_lines[0] == "Alex Example"
    assert "Java" in base.skills and "Kafka" in base.skills
    assert len(base.experience) == 2 and base.experience[1].is_internship
    assert len(base.experience[0].bullets) == 4
    assert base.projects and base.projects[0].bullets
    assert base.education and base.certifications


def test_grounding_rejects_invented_tech_years_and_unknown_sources(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    good = deterministic_tailor(base, "Java Spring Boot PostgreSQL")
    assert check_grounding(good, base).ok
    bad = {"summary": "Backend developer", "skills": ["Java", "Kubernetes"],
           "experience": [{"experience_id": "exp_1", "bullets": [{"source_id": "b_1", "text": "Built REST APIs in Java with Kubernetes and Terraform."},
                                                                 {"source_id": "b_99", "text": "Led a team."},
                                                                 {"source_id": "b_2", "text": "Over 7 years tuning PostgreSQL."}]}]}
    report = check_grounding(bad, base)
    assert not report.ok
    joined = " ".join(report.violations)
    assert "Kubernetes" in joined and "Terraform" in joined and "b_99" not in joined and "without base source" in joined and "7 years" in joined


def test_docx_build_is_single_column_with_standard_headings(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    out = build_docx(base, deterministic_tailor(base, "Java"), tmp_path / "out.docx")
    s = verify_docx_structure(out)
    assert s["single_column"] and s["has_summary"] and s["has_experience"] and s["first_line"] == "Alex Example"


@pytest.mark.mongo
def test_tailor_service_falls_back_when_model_output_is_ungrounded_and_records_hashes(store, tmp_path):
    base_path = make_base_resume(tmp_path / "base.docx")
    job = {"_id": "job_1", "employer": "Acme Inc", "title": "Backend Developer", "requisition_id": "R1",
           "latest_snapshot": {"text": "Java Spring Boot APIs Kubernetes", "posting_hash": "abcd"}}
    provider = FakeProvider({"TailorOutput": lambda u: {"summary": "Backend developer with Kubernetes experience", "skills": ["Kubernetes"],
                                                         "experience": [{"experience_id": "exp_1", "bullets": [{"source_id": "b_1", "text": "Ran Kubernetes clusters."}]}]}})
    t = Tailor(store, tmp_path / "artifacts", AIRunner(store, provider, catalog, Pricing(None)))
    art = t.tailor_for_job(job, base_path, sha256_file(base_path), fact_version=3, display_name="Alex Example")
    assert art["method"] == "deterministic_after_rejection" and art["grounding"]["ok"]
    assert art["filename"].startswith("Alex_Example_Resume_Acme_Inc_Backend_Developer_R1") and art["sha256"]
    assert art["structure"]["single_column"]
    if find_soffice():
        assert art["page_limit_verified"] and art["pages"] <= 2
    else:
        assert art["page_limit_verified"] is False and "renderer" in art["render_note"]
    assert store.c("artifacts").count_documents({"owner_id": "job_1"}) == 1
    # base untouched
    assert sha256_file(base_path) == art["base_sha256"]
