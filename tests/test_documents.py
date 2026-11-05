"""Serving tailored documents by artifact id: path confinement, hash re-check, headers, auth."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.config import Settings
from amorehiremepls.runtime import Runtime
from amorehiremepls.util import sha256_bytes
from amorehiremepls.web import DOCX_MEDIA_TYPE, create_app
from tests.conftest import TEST_MONGO_URI

pytestmark = pytest.mark.mongo

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
DOCX = b"PK\x03\x04" + b"fake docx body " * 8


@pytest.fixture
def client(store, tmp_path):
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "artifacts",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(s, store=store)
    return TestClient(create_app(rt), follow_redirects=False)


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def _artifact(rt, art_id: str, docx_path, pdf_path=None, **over) -> dict:
    doc = {
        "_id": art_id,
        "owner_id": "job_doc1",
        "kind": "resume_tailored",
        "path": str(docx_path),
        "filename": "Fictional_Person_Resume_Acme_R1.docx",
        "sha256": sha256_bytes(docx_path.read_bytes()),
        "pdf_path": str(pdf_path) if pdf_path else None,
        "pdf_sha256": sha256_bytes(pdf_path.read_bytes()) if pdf_path else None,
        "pages": 2 if pdf_path else None,
        "page_limit_verified": bool(pdf_path),
        "render_note": "" if pdf_path else "no LibreOffice renderer available",
        "method": "deterministic",
        "grounding": {"ok": True, "violations": [], "checked_bullets": 3},
        "created_at": datetime.now(UTC),
        **over,
    }
    rt.store.c("artifacts").insert_one(doc)
    return doc


def test_documents_are_served_by_id_with_hash_check_and_confinement(client, tmp_path):
    rt = client.app.state.rt
    jobdir = rt.settings.artifact_dir / "jobs" / "job_doc1"
    jobdir.mkdir(parents=True)
    docx, pdf = jobdir / "resume.docx", jobdir / "resume.pdf"
    docx.write_bytes(DOCX)
    pdf.write_bytes(PDF)
    _artifact(rt, "art_ok", docx, pdf)
    # login required before anything is served
    r = client.get("/documents/art_ok/download?kind=pdf&inline=1")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    _login(client)
    # DOCX attachment with the stored filename and the right media type
    r = client.get("/documents/art_ok/download")
    assert r.status_code == 200 and r.content == DOCX
    assert r.headers["content-type"].startswith(DOCX_MEDIA_TYPE)
    assert r.headers["content-disposition"] == 'attachment; filename="Fictional_Person_Resume_Acme_R1.docx"'
    assert r.headers["cache-control"] == "private, no-store" and r.headers["x-content-type-options"] == "nosniff"
    # PDF inline vs attachment
    r = client.get("/documents/art_ok/download?kind=pdf&inline=1")
    assert r.status_code == 200 and r.content == PDF and r.headers["content-type"].startswith("application/pdf")
    assert r.headers["content-disposition"] == 'inline; filename="resume.pdf"'
    r = client.get("/documents/art_ok/download?kind=pdf")
    assert r.headers["content-disposition"] == 'attachment; filename="resume.pdf"'
    assert client.get("/documents/art_ok/download?kind=docx&inline=1").headers["content-disposition"].startswith("attachment")  # docx never inline
    assert client.get("/documents/art_ok/download?kind=exe").status_code == 400
    # unknown id
    assert client.get("/documents/art_nope/download").status_code == 404
    # no PDF recorded
    docx2 = jobdir / "resume2.docx"
    docx2.write_bytes(DOCX + b"2")
    _artifact(rt, "art_nopdf", docx2)
    assert client.get("/documents/art_nopdf/download?kind=pdf").status_code == 404
    assert client.get("/documents/art_nopdf/download").status_code == 200
    # path outside the artifact dir is refused even though the file exists and the hash matches
    outside = tmp_path / "elsewhere" / "secret.docx"
    outside.parent.mkdir()
    outside.write_bytes(DOCX)
    _artifact(rt, "art_outside", outside)
    assert client.get("/documents/art_outside/download").status_code == 403
    sneaky = jobdir / ".." / ".." / ".." / "elsewhere" / "secret.docx"
    _artifact(rt, "art_sneaky", sneaky)
    assert client.get("/documents/art_sneaky/download").status_code == 403
    # tampered or stale file: hash mismatch -> 409, nothing served
    docx.write_bytes(DOCX + b" edited after review")
    r = client.get("/documents/art_ok/download")
    assert r.status_code == 409 and b"edited" not in r.content
    assert client.get("/documents/art_ok/download?kind=pdf&inline=1").status_code == 200  # the PDF is still intact
    # missing file
    pdf.unlink()
    assert client.get("/documents/art_ok/download?kind=pdf").status_code == 404
    audit = {e["event"] for e in rt.store.c("audit").find({"subject_id": {"$in": ["art_ok", "art_outside", "art_sneaky"]}})}
    assert {"document_hash_mismatch", "document_path_refused"} <= audit
    # the client never chooses a path: only the id and kind are query inputs
    assert client.get("/documents/art_ok/download?kind=docx&path=/etc/passwd").status_code == 409  # path ignored; still the (now tampered) docx


def test_document_buttons_appear_on_documents_job_and_review_pages(client):
    rt = client.app.state.rt
    _login(client)
    jobdir = rt.settings.artifact_dir / "jobs" / "job_doc1"
    jobdir.mkdir(parents=True)
    docx, pdf = jobdir / "resume.docx", jobdir / "resume.pdf"
    docx.write_bytes(DOCX)
    pdf.write_bytes(PDF)
    art = _artifact(rt, "art_btn", docx, pdf)
    now = datetime.now(UTC)
    job = {"_id": "job_doc1", "employer": "Acme", "title": "Backend Developer", "status": "qualified", "posting_url": "https://careers.acme.example/r1", "sources": ["manual"]}
    rt.store.c("jobs").insert_one({**job, "created_at": now, "updated_at": now})
    document = {"filename": art["filename"], "sha256": art["sha256"], "pdf_sha256": art["pdf_sha256"], "pages": 2}
    rt.store.c("applications").insert_one(
        {
            "_id": "app_doc1",
            "job_id": "job_doc1",
            "status": "ready_for_review",
            "provenance": {"origin": "local"},
            "material_hash": "mh",
            "review_package": {"apply_url": "https://careers.acme.example/apply", "posting_url": "https://careers.acme.example/r1", "answers": [], "document": document},
            "history": [],
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    for path in ("/documents", "/jobs/job_doc1", "/applications/app_doc1"):
        page = client.get(path).text
        assert "/documents/art_btn/download?kind=pdf&amp;inline=1" in page, path
        assert "/documents/art_btn/download?kind=docx" in page and "Open PDF" in page and "Download DOCX" in page, path
        assert str(docx) in page and 'class="path"' in page, path
    assert "Read what would be uploaded" in client.get("/applications/app_doc1").text
