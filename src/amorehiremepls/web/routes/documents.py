"""Routes registered by create_app."""

from __future__ import annotations

from amorehiremepls.web.chrome import (
    DOCX_MEDIA_TYPE,
    Any,
    Depends,
    FileResponse,
    HTMLResponse,
    HTTPException,
    Path,
    Request,
    re,
    sha256_file,
)
from amorehiremepls.web.shots import split_screenshots


def register(app, rt, require_session, render) -> None:
    # -- applications list / documents ---------------------------------------------------------
    @app.get("/applications", response_class=HTMLResponse)
    def applications_list(request: Request, session: dict = Depends(require_session), status_filter: str = ""):
        q: dict[str, Any] = {"provenance.origin": "local"}
        if status_filter:
            q["status"] = status_filter
        apps = list(rt.store.c("applications").find(q).sort("updated_at", -1).limit(200))
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [a["job_id"] for a in apps]}}, {"employer": 1, "title": 1, "location": 1})}
        counts = {r["_id"]: r["n"] for r in rt.store.c("applications").aggregate([{"$match": {"provenance.origin": "local"}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
        from amorehiremepls.web.routes.applications import _can_clear

        clearable = {a["_id"] for a in apps if _can_clear(a)}  # Remove is offered only when nothing was sent
        return render(request, "applications.html", session, apps=apps, jobs=jobs, counts=counts, status_filter=status_filter, clearable=clearable, tab="packages")

    @app.get("/documents", response_class=HTMLResponse)
    def documents(request: Request, session: dict = Depends(require_session)):
        arts = list(rt.store.c("artifacts").find({}, {"content": 0}).sort("created_at", -1).limit(200))
        jobs = {j["_id"]: j for j in rt.store.c("jobs").find({"_id": {"$in": [a.get("owner_id") for a in arts]}}, {"employer": 1, "title": 1})}
        docs, shots = split_screenshots(arts)
        for sh in shots:
            sh["job"] = jobs.get(sh.get("owner_id"))
        return render(request, "documents.html", session, artifacts=docs, screenshots=shots, jobs=jobs)

    @app.get("/documents/{artifact_id}/download")
    def document_download(artifact_id: str, session: dict = Depends(require_session), kind: str = "docx", inline: int = 0):
        """Serve a tailored document by id only. The path comes from the artifact record, must resolve under the
        artifact directory, and its hash must still match the reviewed hash; otherwise nothing is served."""
        art = rt.store.c("artifacts").find_one({"_id": artifact_id})
        if not art:
            raise HTTPException(404)
        is_shot = art.get("kind") == "screenshot" or str(art.get("media_type") or "").startswith("image/")
        if kind == "png":
            if not is_shot:
                raise HTTPException(400, "this artifact is a document, not a screenshot")
            raw, expected, media, name = art.get("path"), art.get("sha256"), "image/png", art.get("filename")
        elif is_shot:
            raise HTTPException(400, "screenshots are served with kind=png only")
        elif kind == "pdf":
            raw, expected, media, name = art.get("pdf_path"), art.get("pdf_sha256"), "application/pdf", None
        elif kind == "docx":
            raw, expected, media, name = art.get("path"), art.get("sha256"), DOCX_MEDIA_TYPE, art.get("filename")
        else:
            raise HTTPException(400, "kind must be docx, pdf or png")
        if not raw or not expected:
            raise HTTPException(404, f"no {kind} recorded for this document")
        path = Path(raw).resolve()
        root = rt.settings.artifact_dir.resolve()
        if not path.is_relative_to(root):
            rt.store.audit("document_path_refused", artifact_id, kind=kind)
            raise HTTPException(403, "document path is outside the artifact directory")
        if not path.is_file():
            raise HTTPException(404, "file missing on this host")
        if sha256_file(path) != expected:
            rt.store.audit("document_hash_mismatch", artifact_id, kind=kind)
            raise HTTPException(409, "file on disk no longer matches the reviewed hash; not serving it")
        filename = re.sub(r"[^A-Za-z0-9._-]+", "_", name or path.name) or f"document.{kind}"
        disposition = "inline" if inline and kind in ("pdf", "png") else "attachment"
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        return FileResponse(path, media_type=media, filename=filename, content_disposition_type=disposition, headers=headers)
