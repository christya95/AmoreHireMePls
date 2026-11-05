from pathlib import Path

import pytest

from amorehiremepls.ai import AIRunner, FakeProvider, Pricing
from amorehiremepls.skills import catalog
from amorehiremepls.tailoring import (
    Tailor,
    _render_env,
    build_docx,
    check_grounding,
    deterministic_tailor,
    find_soffice,
    parse_base_resume,
    render_pdf,
    verify_docx_structure,
)
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
    bad = {
        "summary": "Backend developer",
        "skills": ["Java", "Kubernetes"],
        "experience": [
            {
                "experience_id": "exp_1",
                "bullets": [
                    {"source_id": "b_1", "text": "Built REST APIs in Java with Kubernetes and Terraform."},
                    {"source_id": "b_99", "text": "Led a team."},
                    {"source_id": "b_2", "text": "Over 7 years tuning PostgreSQL."},
                ],
            }
        ],
    }
    report = check_grounding(bad, base)
    assert not report.ok
    joined = " ".join(report.violations)
    assert "Kubernetes" in joined and "Terraform" in joined and "b_99" not in joined and "without base source" in joined and "7 years" in joined
    # the approved base summary passes verbatim even when it contains a dash; a rewritten summary with a dash does not
    base.summary = "Backend developer - APIs and data pipelines"
    assert check_grounding({**good, "summary": base.summary}, base).ok
    assert "hyphen or dash" in " ".join(check_grounding({**good, "summary": "Backend developer - rewritten"}, base).violations)


def test_docx_build_is_single_column_with_standard_headings(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    out = build_docx(base, deterministic_tailor(base, "Java"), tmp_path / "out.docx")
    s = verify_docx_structure(out)
    assert s["single_column"] and s["has_summary"] and s["has_experience"] and s["first_line"] == "Alex Example"


def _fixture_docx(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    return build_docx(base, deterministic_tailor(base, "Java"), tmp_path / "artifacts" / "out.docx")


@pytest.mark.skipif(not find_soffice(), reason="LibreOffice not installed")
def test_render_pdf_with_real_libreoffice_keeps_profile_out_of_artifact_dir(tmp_path):
    docx = _fixture_docx(tmp_path)
    res = render_pdf(docx)
    assert res["reason"] == "", res["reason"]
    assert res["pdf_path"] == str(docx.with_suffix(".pdf")) and Path(res["pdf_path"]).exists()
    assert res["verified"] and res["pages"] <= 2 and res["selectable_text"]
    assert sorted(p.name for p in docx.parent.iterdir()) == ["out.docx", "out.pdf"]
    # re-render overwrites the previous PDF and is still reported as fresh output
    assert render_pdf(docx)["verified"]


def _fake_soffice(tmp_path, body: str) -> str:
    script = tmp_path / "fake-soffice"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


def test_render_pdf_failure_reason_includes_exit_code_and_redacted_stderr_tail(tmp_path):
    docx = _fixture_docx(tmp_path)
    exe = _fake_soffice(tmp_path, f'echo "{"x" * 400}" >&2; echo "javaldx failed in {Path.home()}/Library/Application Support" >&2; exit 77')
    res = render_pdf(docx, exe=exe)
    assert res["pdf_path"] is None and res["pages"] is None and res["verified"] is False
    assert res["reason"].startswith("render failed: exit code 77; stderr: ")
    tail = res["reason"].split("stderr: ", 1)[1]
    assert len(tail) <= 300 and tail.endswith("javaldx failed in ~/Library/Application Support")
    assert str(Path.home()) not in res["reason"]


def test_render_pdf_failure_reason_without_stderr_and_on_timeout(tmp_path):
    docx = _fixture_docx(tmp_path)
    assert render_pdf(docx, exe=_fake_soffice(tmp_path, "exit 1"))["reason"] == "render failed: exit code 1; no stderr"
    assert render_pdf(docx, timeout=1, exe=_fake_soffice(tmp_path, "sleep 5"))["reason"].startswith("render failed: timeout after 1s")


def test_render_pdf_reports_missing_output_and_stale_pdf(tmp_path):
    docx = _fixture_docx(tmp_path)
    exe = _fake_soffice(tmp_path, 'echo "convert skipped" >&2; exit 0')
    assert render_pdf(docx, exe=exe)["reason"] == "renderer produced no file; output: convert skipped"
    docx.with_suffix(".pdf").write_bytes(b"%PDF-stale")
    assert render_pdf(docx, exe=exe)["reason"].startswith("renderer produced no file")


def test_render_env_adds_renderer_dir_and_keeps_real_home(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    exe = _fake_soffice(tmp_path / "bin", "exit 0")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SAL_LOG", "+WARN")
    env = _render_env(exe)
    assert env["PATH"].split(":")[0] == str((tmp_path / "bin").resolve()) and "/opt/homebrew/bin" in env["PATH"].split(":")
    assert env["HOME"] == str(Path.home()) and "SAL_LOG" not in env


@pytest.mark.mongo
def test_tailor_service_falls_back_when_model_output_is_ungrounded_and_records_hashes(store, tmp_path):
    base_path = make_base_resume(tmp_path / "base.docx")
    job = {
        "_id": "job_1",
        "employer": "Acme Inc",
        "title": "Backend Developer",
        "requisition_id": "R1",
        "latest_snapshot": {"text": "Java Spring Boot APIs Kubernetes", "posting_hash": "abcd"},
    }
    provider = FakeProvider(
        {
            "TailorOutput": lambda u: {
                "summary": "Backend developer with Kubernetes experience",
                "skills": ["Kubernetes"],
                "experience": [{"experience_id": "exp_1", "bullets": [{"source_id": "b_1", "text": "Ran Kubernetes clusters."}]}],
            }
        }
    )
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
