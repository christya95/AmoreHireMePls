from pathlib import Path

import pytest
from docx import Document
from docx.enum.section import WD_SECTION

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
from tests.fixtures.make_resume import FIXTURE_CONTACT, FIXTURE_NAME, make_base_resume, make_column_header_resume


def test_parse_base_resume_sections(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    assert base.contact_lines[0] == "Alex Example"
    assert "Java" in base.skills and "Kafka" in base.skills
    assert len(base.experience) == 2 and base.experience[1].is_internship
    assert len(base.experience[0].bullets) == 4
    assert base.projects and base.projects[0].bullets
    assert base.education and base.certifications
    assert base.source_path == tmp_path / "base.docx"


def test_parse_column_header_layout_keeps_name_and_contact_out_of_summary(tmp_path):
    # the "SUMMARY" label precedes the name in document order; the name and contact strip are still the header
    base = parse_base_resume(make_column_header_resume(tmp_path / "base.docx"))
    assert base.contact_lines == [FIXTURE_NAME, FIXTURE_CONTACT]
    assert base.summary.startswith("Backend developer") and FIXTURE_NAME not in base.summary and "alex@example.com" not in base.summary
    # category labels are not skills; items behind "Label:<tab>" are
    assert "Languages" not in base.skills and "Languages:" not in base.skills
    assert {"Java", "Python", "SQL", "Spring Boot", "Kafka", "Git"} <= set(base.skills)
    assert sum(len(e.bullets) for e in base.experience) == 5 and base.projects[0].bullets


def _paragraph_xml(docx_path: Path) -> list[str]:
    from lxml import etree

    return [etree.tostring(p._p) for p in Document(str(docx_path)).paragraphs]


def _texts(docx_path: Path) -> list[str]:
    return [p.text.strip() for p in Document(str(docx_path)).paragraphs if p.text.strip()]


MODEL_LIKE = {
    "summary": "Backend developer who builds Java and Spring Boot services for banks.",
    "skills": ["Spring Boot", "Java", "Kafka", "GitHub Actions"],
    "experience": [
        {
            "experience_id": "exp_1",
            "bullets": [
                {"source_id": "b_2", "text": "Tuned PostgreSQL queries and cut batch processing time by 40%."},
                {"source_id": "b_1", "text": "Built REST APIs in Java and Spring Boot serving 2 million daily requests."},
            ],
        },
        {"experience_id": "exp_2", "bullets": [{"source_id": "b_5", "text": "Wrote Python scripts to process Kafka events for analytics."}]},
    ],
}


def test_template_build_keeps_header_block_verbatim_before_first_heading(tmp_path):
    base_path = make_base_resume(tmp_path / "base.docx")
    base_sha = sha256_file(base_path)
    base = parse_base_resume(base_path)
    out = build_docx(base, MODEL_LIKE, tmp_path / "out.docx")
    texts = _texts(out)
    # the header block (name + contact strip) is first, untouched, and precedes the first section heading
    assert texts[:3] == [FIXTURE_NAME, FIXTURE_CONTACT, "Summary"]
    assert _paragraph_xml(out)[:2] == _paragraph_xml(base_path)[:2]
    # tailorable parts were replaced in place; everything else is the base verbatim
    assert texts[3] == MODEL_LIKE["summary"]
    assert texts[5] == "Java, Spring Boot, Kafka, GitHub Actions"  # base order kept, dropped items gone, new grounded skill appended
    assert texts[7:11] == [
        "Software Developer at Big Bank | May 2021 – March 2025",
        "Tuned PostgreSQL queries and cut batch processing time by 40%.",
        "Built REST APIs in Java and Spring Boot serving 2 million daily requests.",
        "Software Developer Intern at Startup Co | May 2022 – August 2022",
    ]
    assert "Led a hackathon health monitoring project that expanded into a working group." not in texts
    assert texts[-4:] == ["Education", "Honours Associates, Computer Programming, Example College", "Certifications", "AWS Certified Cloud Practitioner"]
    styles = {p.text.strip(): p.style.name for p in Document(str(out)).paragraphs if p.text.strip()}
    assert styles["Tuned PostgreSQL queries and cut batch processing time by 40%."] == "List Bullet"
    assert sha256_file(base_path) == base_sha  # the base file itself is never written
    assert verify_docx_structure(out)["first_line"] == FIXTURE_NAME


def test_template_build_preserves_two_column_header_layout(tmp_path):
    from docx.oxml.ns import qn

    base_path = make_column_header_resume(tmp_path / "base.docx")
    base = parse_base_resume(base_path)
    out = build_docx(base, {**MODEL_LIKE, "experience": [{"experience_id": "exp_2", "bullets": MODEL_LIKE["experience"][0]["bullets"]}]}, tmp_path / "out.docx")
    doc = Document(str(out))
    base_doc = Document(str(base_path))
    # section 1 keeps its two unequal columns; the header paragraphs are byte-identical to the base
    cols = doc.sections[0]._sectPr.find(qn("w:cols"))
    assert cols.get(qn("w:num")) == "2" and [c.get(qn("w:w")) for c in cols.findall(qn("w:col"))] == ["1368", "8515"]
    assert _paragraph_xml(out)[:6] == _paragraph_xml(base_path)[:6]
    name_p = next(p for p in doc.paragraphs if p.text.strip() == FIXTURE_NAME)
    assert name_p.style.name == "Heading 1" and name_p._p.find(".//" + qn("w:br")).get(qn("w:type")) == "column"
    contact_p = next(p for p in doc.paragraphs if p.text.strip() == FIXTURE_CONTACT)
    assert contact_p._p.find(qn("w:hyperlink")) is not None
    texts = _texts(out)
    assert texts[:4] == ["SUMMARY", FIXTURE_NAME, FIXTURE_CONTACT, MODEL_LIKE["summary"]]
    # skills keep their "Label:<tab>items" groups and bold labels; a group left empty disappears, extras join the last surviving group
    assert texts[5:8] == ["Languages:\tJava", "Frameworks:\tSpring Boot, Kafka, GitHub Actions", "PROFESSIONAL EXPERIENCE"]
    skills_p = next(p for p in doc.paragraphs if p.text.startswith("Frameworks:"))
    assert [r.bold for r in skills_p.runs][:2] == [True, True] and skills_p.runs[-1].bold is None
    # role header lines untouched, bullets rewritten in place with the list style, leftovers removed
    assert texts[8:12] == [
        "Software Developer\tMay 2021 – March 2025",
        "Big Bank, Toronto, Ontario",
        "Tuned PostgreSQL queries and cut batch processing time by 40%.",
        "Built REST APIs in Java and Spring Boot serving 2 million daily requests.",
    ]
    assert texts[12] == "Software Developer Intern\tMay 2022 – August 2022" and len(doc.paragraphs) == len(base_doc.paragraphs) - 3
    # the page-2 section now flows instead of forcing a page break; the two-column first section is unchanged
    assert [s.start_type for s in doc.sections][1:] == [WD_SECTION.CONTINUOUS, WD_SECTION.CONTINUOUS]
    assert verify_docx_structure(out)["has_summary"]


def test_template_build_refuses_when_base_file_changed_and_falls_back_when_missing(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    make_column_header_resume(tmp_path / "base.docx")  # same path, different document
    with pytest.raises(ValueError, match="no longer matches"):
        build_docx(base, deterministic_tailor(base, "Java"), tmp_path / "out.docx")
    base.source_path = tmp_path / "gone.docx"
    out = build_docx(base, deterministic_tailor(base, "Java"), tmp_path / "fresh.docx")
    assert _texts(out)[:2] == [FIXTURE_NAME, FIXTURE_CONTACT]


@pytest.mark.skipif(not find_soffice(), reason="LibreOffice not installed")
def test_rendered_pdf_starts_with_header_for_column_layout(tmp_path):
    from pypdf import PdfReader

    base = parse_base_resume(make_column_header_resume(tmp_path / "base.docx"))
    out = build_docx(base, MODEL_LIKE, tmp_path / "artifacts" / "out.docx")
    res = render_pdf(out)
    assert res["reason"] == "" and res["pages"] <= 2
    page1 = "".join((PdfReader(res["pdf_path"]).pages[0].extract_text() or "").split())
    head = page1[: len("".join((FIXTURE_NAME + FIXTURE_CONTACT + "SUMMARY").split())) + 40]
    assert "".join(FIXTURE_NAME.split()) in head and "".join(FIXTURE_CONTACT.split()) in head
    assert head.index("AlexExample") < page1.index("".join(MODEL_LIKE["summary"].split())[:30])


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
