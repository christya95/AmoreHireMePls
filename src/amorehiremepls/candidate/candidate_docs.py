"""The candidate drop folder (`candidate/` by default, `AMORE_CANDIDATE_DIR`).

One place a person can put their own files without touching code: the base resume as a DOCX and any extra
career notes (.md, .txt, .docx). The loader picks the base DOCX deterministically and concatenates the other
files' text under a character budget, in name order, with a content hash so callers can tell when it changed.

Everything here is private evidence the candidate wrote. It is never committed (the folder's .gitignore keeps
only the README) and it grants no instructions: callers pass it to the model as data, not as a prompt.
A missing or empty folder returns an empty result and changes nothing.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_SUFFIXES = (".md", ".txt")
UPLOAD_SUFFIXES = (".docx", *TEXT_SUFFIXES)
UPLOAD_MAX_BYTES = 5 * 1024 * 1024
RESUME_NAME_RE = re.compile(r"resume|résumé|\bcv\b|_cv|cv_", re.I)
_SKIP_NAMES = {"readme.md", ".gitignore"}
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
TRUNCATION_NOTE = "\n[extra documents cut at the character limit]"


class CandidateUploadError(ValueError):
    """An upload the folder will not take; ``code`` is one of ``bad_name``, ``bad_type``, ``too_big``."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def safe_upload_name(filename: str) -> str:
    """A plain basename the folder can hold: directory parts dropped, odd characters replaced by ``_``, must end in a
    supported suffix and must not be hidden or one of the files the loader skips. Raises ``CandidateUploadError``."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _UNSAFE_NAME_RE.sub("_", base).strip("._")
    if not base or len(base) > 120:
        raise CandidateUploadError("bad_name", "the file needs a short plain name")
    if Path(base).suffix.lower() not in UPLOAD_SUFFIXES:
        raise CandidateUploadError("bad_type", f"only {', '.join(UPLOAD_SUFFIXES)} files are read")
    if base.lower() in _SKIP_NAMES:
        raise CandidateUploadError("bad_name", f"{base} is not read as evidence")
    return base


def save_upload(directory: Path | str, filename: str, data: bytes, max_bytes: int = UPLOAD_MAX_BYTES) -> Path:
    """Write one uploaded file into the drop folder under a sanitized name; an existing file of that name is replaced.
    A DOCX that Word cannot open is refused before anything is written."""
    name = safe_upload_name(filename)
    if len(data) > max_bytes:
        raise CandidateUploadError("too_big", f"files are limited to {max_bytes // (1024 * 1024)} MB")
    if not data:
        raise CandidateUploadError("bad_type", "the file is empty")
    if name.lower().endswith(".docx"):
        import io

        from docx import Document

        try:
            Document(io.BytesIO(data))
        except Exception as exc:
            raise CandidateUploadError("bad_type", "that is not a Word document") from exc
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    tmp = directory / f".{name}.part"
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


@dataclass
class CandidateDocs:
    directory: Path
    base_resume: Path | None = None
    base_resume_sha256: str = ""
    supplemental_files: list[Path] = field(default_factory=list)
    supplemental_text: str = ""
    supplemental_sha256: str = ""  # sha256 of ``supplemental_text``; "" when there is none
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.base_resume is not None or bool(self.supplemental_text)

    def as_dict(self) -> dict:
        return {
            "directory": str(self.directory),
            "base_resume": self.base_resume.name if self.base_resume else None,
            "base_resume_sha256": self.base_resume_sha256,
            "supplemental_files": [p.name for p in self.supplemental_files],
            "supplemental_chars": len(self.supplemental_text),
            "supplemental_sha256": self.supplemental_sha256,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
        }


def _docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    lines = [p.text.rstrip() for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def read_document_text(path: Path) -> str:
    """Plain text of one supported file; whitespace tidied, never more than a double blank line in a row."""
    if path.suffix.lower() == ".docx":
        text = _docx_text(path)
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_base_resume(docx_files: list[Path]) -> tuple[Path | None, list[Path], list[str]]:
    """Choose the base DOCX. Files whose name says resume/CV are candidates; when none does, every DOCX is.
    The alphabetically first candidate wins; the others are left alone with a warning. Returns
    ``(base, supplemental_docx, warnings)`` where ``supplemental_docx`` are the DOCX files that are not resumes."""
    files = sorted(docx_files, key=lambda p: p.name.lower())
    if not files:
        return None, [], []
    named = [p for p in files if RESUME_NAME_RE.search(p.stem)]
    candidates = named or files
    base = candidates[0]
    warnings: list[str] = []
    if len(candidates) > 1:
        others = ", ".join(p.name for p in candidates[1:])
        warnings.append(f"several resume files found; using {base.name} (first by name) and ignoring {others}")
    supplemental = [p for p in files if p not in candidates]
    return base, supplemental, warnings


def load_candidate_docs(directory: Path | str, max_chars: int = 12_000) -> CandidateDocs:
    """Read the drop folder. Never raises for a missing folder or an unreadable file; problems become warnings."""
    directory = Path(directory)
    docs = CandidateDocs(directory=directory)
    if not directory.is_dir():
        return docs
    entries = sorted((p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".") and p.name.lower() not in _SKIP_NAMES), key=lambda p: p.name.lower())
    docx_files = [p for p in entries if p.suffix.lower() == ".docx"]
    text_files = [p for p in entries if p.suffix.lower() in TEXT_SUFFIXES]
    base, extra_docx, warnings = pick_base_resume(docx_files)
    docs.warnings.extend(warnings)
    if base is not None:
        try:
            from docx import Document

            Document(str(base))  # a file that Word cannot open must not become the template
            docs.base_resume, docs.base_resume_sha256 = base, _sha256_file(base)
        except Exception as exc:
            docs.warnings.append(f"could not read {base.name} as a Word document: {type(exc).__name__}")
    parts: list[str] = []
    used = 0
    for path in sorted([*text_files, *extra_docx], key=lambda p: p.name.lower()):
        try:
            body = read_document_text(path)
        except Exception as exc:  # a corrupt file must not stop tailoring; it is simply not evidence
            docs.warnings.append(f"could not read {path.name}: {type(exc).__name__}")
            continue
        if not body:
            continue
        chunk = f"## {path.name}\n{body}"
        sep = 2 if parts else 0
        if max_chars and used + sep + len(chunk) > max_chars:
            room = max_chars - used - sep - len(TRUNCATION_NOTE)
            if room > len(chunk.split("\n", 1)[0]) + 40:  # keep a cut file only when more than its heading fits
                parts.append(chunk[:room].rstrip() + TRUNCATION_NOTE)
                docs.supplemental_files.append(path)
            docs.truncated = True
            docs.warnings.append(f"extra documents exceed {max_chars} characters; text was cut at {path.name}")
            break
        parts.append(chunk)
        docs.supplemental_files.append(path)
        used += sep + len(chunk)
    docs.supplemental_text = "\n\n".join(parts)
    if docs.supplemental_text:
        docs.supplemental_sha256 = hashlib.sha256(docs.supplemental_text.encode("utf-8")).hexdigest()
    for w in docs.warnings:
        log.info("candidate folder: %s", w)
    return docs
