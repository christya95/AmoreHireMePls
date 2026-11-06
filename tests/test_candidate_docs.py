"""The candidate drop folder: loader, git hygiene, and how tailoring and fit ranking use the extra notes. Fictional data only."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from docx import Document

from amorehiremepls.apply.tailoring import Tailor, check_grounding, parse_base_resume
from amorehiremepls.candidate.candidate_docs import TRUNCATION_NOTE, CandidateUploadError, load_candidate_docs, pick_base_resume, safe_upload_name, save_upload
from amorehiremepls.candidate.profile import BaseResume, Profile
from amorehiremepls.core.ai import AIRunner, FakeProvider, Pricing
from amorehiremepls.core.skills import catalog
from amorehiremepls.core.util import sha256_file
from amorehiremepls.jobs.discovery import compact_facts
from tests.fixtures.make_resume import make_base_resume

REPO = Path(__file__).resolve().parents[1]
NOTES_MD = (
    "# Career notes 2023 to 2026\n\n"
    "## Big Bank, Software Developer\n\n"
    "- Wrote Terraform modules for the payment service's AWS accounts and reviewed them with the platform team.\n"
    "- Ran the Kafka consumer group for settlement events, 5 years of on call rotation across two teams.\n\n"
    "The batch tuning work moved nightly PostgreSQL jobs from four hours to under one.\n"
)


def _notes_docx(path: Path) -> Path:
    doc = Document()
    doc.add_paragraph("Mentored two interns on Spring Boot testing.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Tool", "Grafana"
    doc.save(str(path))
    return path


def _folder(tmp_path: Path) -> Path:
    d = tmp_path / "candidate"
    d.mkdir()
    make_base_resume(d / "Alex_Example_Resume.docx")
    _notes_docx(d / "mentoring_notes.docx")
    (d / "career_2023_2026.md").write_text(NOTES_MD, encoding="utf-8")
    (d / "01_focus.txt").write_text("Looking for permanent backend roles.\n", encoding="utf-8")
    (d / "README.md").write_text("instructions, never evidence", encoding="utf-8")
    (d / ".hidden.md").write_text("ignored", encoding="utf-8")
    return d


def test_loader_picks_resume_docx_and_concatenates_notes_in_name_order(tmp_path):
    d = _folder(tmp_path)
    docs = load_candidate_docs(d)
    assert docs.present and docs.base_resume == d / "Alex_Example_Resume.docx" and docs.base_resume_sha256 == sha256_file(d / "Alex_Example_Resume.docx")
    assert [p.name for p in docs.supplemental_files] == ["01_focus.txt", "career_2023_2026.md", "mentoring_notes.docx"]
    text = docs.supplemental_text
    assert text.index("## 01_focus.txt") < text.index("## career_2023_2026.md") < text.index("## mentoring_notes.docx")
    assert "Terraform modules" in text and "Mentored two interns" in text and "Tool | Grafana" in text
    assert "never evidence" not in text and "ignored" not in text
    assert docs.warnings == [] and not docs.truncated
    assert docs.supplemental_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert load_candidate_docs(d).supplemental_sha256 == docs.supplemental_sha256  # stable across reads
    assert docs.as_dict()["supplemental_chars"] == len(text)


def test_loader_cuts_text_at_the_character_budget(tmp_path):
    d = _folder(tmp_path)
    small = load_candidate_docs(d, max_chars=200)
    assert small.truncated and len(small.supplemental_text) <= 200 and small.supplemental_text.endswith(TRUNCATION_NOTE)
    assert [p.name for p in small.supplemental_files] == ["01_focus.txt", "career_2023_2026.md"]  # the cut file counts, later ones do not
    assert any("exceed 200" in w for w in small.warnings)
    assert load_candidate_docs(d, max_chars=0).truncated is False  # 0 = no cap


def test_loader_prefers_resume_named_docx_and_warns_about_several(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    for name in ("zeta_resume.docx", "Alpha_CV.docx", "notes.docx"):
        make_base_resume(d / name)
    base, extra, warnings = pick_base_resume(sorted(d.glob("*.docx")))
    assert base.name == "Alpha_CV.docx" and [p.name for p in extra] == ["notes.docx"]
    assert warnings and "zeta_resume.docx" in warnings[0] and "Alpha_CV.docx" in warnings[0]
    docs = load_candidate_docs(d)
    assert docs.base_resume.name == "Alpha_CV.docx" and [p.name for p in docs.supplemental_files] == ["notes.docx"]
    # no resume-looking name at all: the only DOCX is the base, nothing is supplemental
    only = tmp_path / "only"
    only.mkdir()
    make_base_resume(only / "alex.docx")
    assert load_candidate_docs(only).base_resume.name == "alex.docx" and load_candidate_docs(only).supplemental_text == ""


def test_loader_is_empty_for_missing_or_readme_only_folder(tmp_path):
    missing = load_candidate_docs(tmp_path / "nope")
    assert not missing.present and missing.base_resume is None and missing.supplemental_text == "" and missing.supplemental_sha256 == ""
    d = tmp_path / "c"
    d.mkdir()
    (d / "README.md").write_text("how to use this folder", encoding="utf-8")
    (d / "broken.docx").write_bytes(b"not a docx")
    docs = load_candidate_docs(d)
    assert docs.base_resume is None and docs.supplemental_text == "" and not docs.present
    assert any("broken.docx" in w for w in docs.warnings)


def _ignored(path: str) -> bool | None:
    try:
        proc = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 128:  # not a git checkout
        return None
    return proc.returncode == 0


@pytest.mark.skipif(not shutil.which("git") or not (REPO / ".git").exists(), reason="needs a git checkout")
def test_git_ignores_dropped_files_but_keeps_the_readme():
    assert _ignored("candidate/Someone_Resume.docx") is True
    assert _ignored("candidate/career_2023_2026.md") is True
    assert _ignored("candidate/README.md") is False
    assert _ignored("candidate/.gitignore") is False


def test_grounding_accepts_terms_that_appear_only_in_the_supplemental_notes(tmp_path):
    base = parse_base_resume(make_base_resume(tmp_path / "base.docx"))
    draft = {
        "summary": base.summary,
        "skills": ["Java", "Terraform"],
        "experience": [
            {
                "experience_id": "exp_1",
                "bullets": [
                    {"source_id": "b_4", "text": "Automated deployments with Docker and Terraform modules on AWS."},
                    {"source_id": "b_1", "text": "Built REST APIs in Java over 5 years of backend work."},
                ],
            }
        ],
    }
    without = check_grounding(draft, base)
    joined = " ".join(without.violations)
    assert not without.ok and "Terraform" in joined and "5 years" in joined
    assert check_grounding(draft, base, NOTES_MD).ok
    # the notes never ground a bullet id or a brand new technology
    bullets = [{"source_id": "doc_1", "text": "Wrote Terraform modules."}, {"source_id": "b_1", "text": "Built Rust services."}]
    report = check_grounding({**draft, "experience": [{"experience_id": "exp_1", "bullets": bullets}]}, base, NOTES_MD)
    assert not report.ok and "without base source" in " ".join(report.violations) and "Rust" in " ".join(report.violations)


class _Coll:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_one(self, doc: dict) -> None:
        self.rows.append(doc)


class _Store:
    def __init__(self) -> None:
        self.colls: dict[str, _Coll] = {}
        self.audits: list[tuple] = []

    def c(self, name: str) -> _Coll:
        return self.colls.setdefault(name, _Coll())

    def audit(self, *args, **kwargs) -> None:
        self.audits.append((args, kwargs))


def _render(pages: int):
    def render(docx_path: Path) -> dict:
        pdf = docx_path.with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-1.4\n%fixture\n")
        return {"pdf_path": str(pdf), "pages": pages, "verified": pages <= 2, "selectable_text": True, "reason": "" if pages <= 2 else f"{pages} pages exceeds two"}

    return render


def test_tailor_sends_the_notes_as_evidence_to_both_model_calls_and_records_their_hash(tmp_path):
    base_path = make_base_resume(tmp_path / "base.docx")
    base = parse_base_resume(base_path)
    job = {"_id": "job_1", "employer": "Acme Inc", "title": "Backend Developer", "requisition_id": "R1", "latest_snapshot": {"text": "Java Terraform AWS", "posting_hash": "abcd"}}
    # a model draft that is only grounded thanks to the notes (Terraform is not on the base resume)
    draft = {
        "summary": base.summary,
        "skills": ["Java", "Terraform"],
        "experience": [{"experience_id": "exp_1", "bullets": [{"source_id": "b_4", "text": "Automated deployments with Docker and Terraform modules."}]}],
    }
    provider = FakeProvider({"TailorOutput": lambda _u: draft})
    store = _Store()
    sha = hashlib.sha256(NOTES_MD.encode()).hexdigest()
    tailor = Tailor(store, tmp_path / "artifacts", AIRunner(None, provider, catalog, Pricing(None)), render=_render(3), supplemental_text=NOTES_MD, supplemental_sha256=sha)
    art = tailor.tailor_for_job(job, base_path, sha256_file(base_path), fact_version=1, display_name="Alex Example")
    assert art["method"] == "model_page_budget" and art["grounding"]["ok"] and art["supplemental_sha256"] == sha
    assert len(provider.calls) == 2  # first draft + the page budget re-tailor; the notes travel with both
    for call in provider.calls:
        assert '<untrusted field="supplemental_evidence">' in call["user"] and "Terraform modules for the payment service" in call["user"]
        assert "supplemental_evidence" in call["system"]
    assert sum("previous draft" in c["user"] for c in provider.calls) == 1
    # without the notes the same draft is rejected and the deterministic fallback is used
    plain = Tailor(_Store(), tmp_path / "artifacts2", AIRunner(None, FakeProvider({"TailorOutput": lambda _u: draft}), catalog, Pricing(None)), render=_render(2))
    art2 = plain.tailor_for_job(job, base_path, sha256_file(base_path), fact_version=1, display_name="Alex Example")
    assert art2["method"] == "deterministic_after_rejection" and art2["supplemental_sha256"] is None


def test_upload_names_are_sanitized_and_only_supported_files_are_written(tmp_path):
    assert safe_upload_name("../../etc/Alex Resume (final).docx") == "Alex_Resume_final_.docx"
    assert safe_upload_name("C:\\Users\\alex\\notes.MD") == "notes.MD"
    assert safe_upload_name(".hidden.md") == "hidden.md"  # a hidden file would be skipped by the loader, so it is made visible
    for name, code in (("tool.exe", "bad_type"), ("notes", "bad_type"), ("README.md", "bad_name"), (".gitignore", "bad_type"), ("", "bad_name"), ("x" * 130 + ".md", "bad_name")):
        with pytest.raises(CandidateUploadError) as exc:
            safe_upload_name(name)
        assert exc.value.code == code, name
    d = tmp_path / "drop"  # created on first write
    assert save_upload(d, "01_focus.txt", b"Looking for permanent backend roles.\n") == d / "01_focus.txt"
    with pytest.raises(CandidateUploadError) as exc:
        save_upload(d, "big.md", b"x" * 11, max_bytes=10)
    assert exc.value.code == "too_big"
    with pytest.raises(CandidateUploadError) as exc:
        save_upload(d, "fake.docx", b"not a word document")
    assert exc.value.code == "bad_type"
    with pytest.raises(CandidateUploadError):
        save_upload(d, "empty.md", b"")
    assert sorted(p.name for p in d.iterdir()) == ["01_focus.txt"]  # nothing partial left behind
    save_upload(d, "01_focus.txt", b"Replaced.\n")
    assert (d / "01_focus.txt").read_text() == "Replaced.\n"


def _dashboard(store, tmp_path: Path, folder: Path):
    from fastapi.testclient import TestClient

    from amorehiremepls.core.config import Settings
    from amorehiremepls.core.runtime import Runtime
    from amorehiremepls.web.web import create_app
    from tests.conftest import TEST_MONGO_URI

    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        candidate_dir=folder,
        _env_file=None,
    )
    rt = Runtime(s, store=store)
    client = TestClient(create_app(rt), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return rt, client


@pytest.mark.mongo
def test_profile_upload_puts_files_in_the_folder_and_refuses_the_wrong_ones(store, tmp_path):
    import re

    folder = tmp_path / "drop"  # does not exist yet; the first upload creates it
    rt, client = _dashboard(store, tmp_path, folder)
    page = client.get("/profile")
    assert "Add a resume or career notes" in page.text and 'action="/profile/candidate-upload"' in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert client.post("/profile/candidate-upload", files={"file": ("x.md", b"# notes", "text/markdown")}).status_code == 422  # no token
    r = client.post("/profile/candidate-upload", data={"csrf": csrf}, files={"file": ("tool.exe", b"MZ\x90\x00", "application/octet-stream")})
    assert r.status_code == 303 and r.headers["location"] == "/profile?err=candidate_bad_type"
    r = client.post("/profile/candidate-upload", data={"csrf": csrf}, files={"file": ("big.txt", b"x" * (5 * 1024 * 1024 + 1), "text/plain")})
    assert r.status_code == 303 and r.headers["location"] == "/profile?err=candidate_too_big"
    assert not folder.exists()
    r = client.post("/profile/candidate-upload", data={"csrf": csrf}, files={"file": ("../career 2023 to 2026.md", NOTES_MD.encode(), "text/markdown")})
    assert r.status_code == 303 and r.headers["location"] == "/profile?ok=candidate_saved"
    assert [p.name for p in folder.iterdir()] == ["career_2023_to_2026.md"] and (folder / "career_2023_to_2026.md").read_text() == NOTES_MD
    text = client.get("/profile?ok=candidate_saved").text
    assert "The file is in your candidate folder." in text and "career_2023_to_2026.md" in text and "Terraform modules" in rt.discovery().supplemental_text
    audit = rt.store.c("audit").find_one({"event": "candidate_file_uploaded"})
    assert audit["data"]["name"] == "career_2023_to_2026.md" and "Terraform" not in str(audit)  # the audit row never carries the content
    assert rt.reload_profile().base_resume is None  # notes are not a base resume
    resume = tmp_path / "Example_Resume.docx"
    make_base_resume(resume)
    r = client.post(
        "/profile/candidate-upload",
        data={"csrf": csrf},
        files={"file": (resume.name, resume.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert r.status_code == 303 and r.headers["location"] == "/profile?ok=candidate_resume_chosen"
    chosen_page = client.get("/profile?ok=candidate_resume_chosen").text
    assert "This resume is now the one every tailored version starts from." in chosen_page
    assert "Your approved base resume (DOCX) is not chosen yet." not in chosen_page
    assert rt.reload_profile().base_resume is not None


@pytest.mark.mongo
def test_profile_page_chooses_the_folder_resume_and_keeps_it_when_the_file_changes(store, tmp_path):
    folder = _folder(tmp_path)
    rt, client = _dashboard(store, tmp_path, folder)
    page = client.get("/profile")
    assert page.status_code == 200
    assert "Alex_Example_Resume.docx" in page.text and "This is your approved base resume." in page.text
    assert "Your approved base resume (DOCX) is not chosen yet." not in page.text
    assert "career_2023_2026.md" in page.text and "mentoring_notes.docx" in page.text
    chosen = rt.reload_profile().base_resume
    assert chosen and chosen.sha256 == sha256_file(folder / "Alex_Example_Resume.docx") and chosen.path.startswith(str(tmp_path / "p"))
    make_base_resume(folder / "Alex_Example_Resume.docx")  # replaced in the folder: the approved copy stays until asked
    (folder / "Alex_Example_Resume.docx").write_bytes((folder / "Alex_Example_Resume.docx").read_bytes() + b"\0")
    text = client.get("/profile").text
    assert "It differs from your approved copy" in text and rt.reload_profile().base_resume.sha256 == chosen.sha256
    # the tailor and fit services built by the runtime carry the notes
    assert "Terraform modules" in rt.tailor().supplemental_text and rt.discovery().supplemental_text == rt.tailor().supplemental_text


def test_compact_facts_adds_doc_lines_from_the_notes_within_the_cap(tmp_path):
    path = make_base_resume(tmp_path / "base.docx")
    profile = Profile(base_resume=BaseResume(path=str(path), sha256=sha256_file(path), approved_on="2026-01-01", source="test"))
    facts = compact_facts(profile, "Terraform AWS Kafka", NOTES_MD)
    doc_lines = [f for f in facts if f.startswith("doc_")]
    assert doc_lines and any("Terraform modules" in f for f in doc_lines) and any("nightly PostgreSQL jobs" in f for f in doc_lines)
    assert not any("career_2023_2026.md" in f or "#" in f for f in doc_lines)  # the loader's file heading is not a fact; the notes' own headings lose their hashes
    assert any(f.endswith(": Big Bank, Software Developer") for f in doc_lines)
    assert [f for f in facts if f.startswith("resume_summary")] and [f for f in facts if f.startswith("b_")]  # resume evidence still present
    resume_and_docs = [f for f in facts if f.startswith(("resume_summary", "skill_", "cert_", "b_", "doc_"))]
    assert sum(len(f) + 1 for f in resume_and_docs) <= 6000 + 1500
    assert compact_facts(profile, "Terraform AWS Kafka") == [f for f in facts if not f.startswith("doc_")]  # no notes, no doc lines
    # ids are positional in the notes, so the same passage keeps its id whatever the posting says
    first = next(f for f in compact_facts(profile, "Kafka", NOTES_MD) if "settlement events" in f)
    assert first.split(":", 1)[0] == next(f for f in doc_lines if "settlement events" in f).split(":", 1)[0]
