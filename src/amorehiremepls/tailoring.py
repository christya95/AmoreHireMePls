"""Resume tailoring: parse the approved base DOCX, produce a grounded per-requisition DOCX (+ PDF when a
renderer exists), verify the page limit, and record artifacts with hashes.

Grounding: every tailored bullet cites a base bullet id; skills must exist in the base; new text may
not introduce technology tokens absent from the base. Violations are rejected, not silently kept.
Without a model provider, a deterministic tailoring (reorder by posting relevance, no rewriting) is used.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from .ai import AIRunner, ProviderUnavailable
from .db import Store
from .skills import TailorInput
from .util import new_id, normalize_text, sha256_file, slugify, utcnow

log = logging.getLogger(__name__)

HEADINGS = {
    "summary": ("summary", "professional summary", "profile"),
    "skills": ("technical skills", "skills", "core skills", "technologies"),
    "experience": ("professional experience", "experience", "work experience", "employment"),
    "projects": ("projects", "selected projects", "personal projects"),
    "education": ("education",),
    "certifications": ("certifications", "certificates", "awards", "certifications & awards"),
}
DATE_RE = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}|\d{4})\s*[-–—to]+\s*((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}|\d{4}|present|current)",
    re.I,
)


@dataclass
class Bullet:
    id: str
    text: str


@dataclass
class Experience:
    id: str
    header: str
    employer: str
    title: str
    dates: str
    bullets: list[Bullet] = field(default_factory=list)
    is_internship: bool = False


@dataclass
class BaseResume:
    contact_lines: list[str]
    summary: str
    skills: list[str]
    experience: list[Experience]
    projects: list[Experience]
    education: list[str]
    certifications: list[str]
    all_text: str

    def bullet_ids(self) -> set[str]:
        return {b.id for e in self.experience + self.projects for b in e.bullets}

    def as_sections(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "skills": self.skills,
            "experience": [
                {"id": e.id, "employer": e.employer, "title": e.title, "dates": e.dates, "bullets": [{"id": b.id, "text": b.text} for b in e.bullets]} for e in self.experience
            ],
            "projects": [
                {"id": e.id, "employer": e.employer, "title": e.title, "dates": e.dates, "bullets": [{"id": b.id, "text": b.text} for b in e.bullets]} for e in self.projects
            ],
            "education": self.education,
            "certifications": self.certifications,
        }


def _heading_key(text: str) -> str | None:
    t = normalize_text(text)
    if len(t) > 40:
        return None
    for key, names in HEADINGS.items():
        if t in names:
            return key
    return None


def parse_base_resume(path: Path) -> BaseResume:
    doc = Document(str(path))
    paras = [p for p in doc.paragraphs]
    section = "contact"
    contact: list[str] = []
    summary: list[str] = []
    skills: list[str] = []
    exp: list[Experience] = []
    projects: list[Experience] = []
    education: list[str] = []
    certs: list[str] = []
    current: Experience | None = None
    bucket: list[Experience] = exp
    all_text: list[str] = []
    counters = {"exp": 0, "b": 0}
    for p in paras:
        text = p.text.strip()
        if not text:
            continue
        all_text.append(text)
        key = _heading_key(text)
        if key:
            section = key
            current = None
            bucket = projects if key == "projects" else exp
            continue
        is_bullet = (p.style.name or "").lower().startswith("list") or text[:1] in "•-–·▪●" or (p.paragraph_format.left_indent or 0) > 0
        if section == "contact":
            contact.append(text)
        elif section == "summary":
            summary.append(text)
        elif section == "skills":
            skills.extend([s.strip(" •-") for s in re.split(r"[,|•;]|\s{2,}", text.split(":", 1)[-1]) if s.strip(" •-")])
        elif section in ("experience", "projects"):
            if not is_bullet and (DATE_RE.search(text) or current is None or (len(text) < 90 and not text.endswith("."))):
                if not is_bullet and current is not None and not DATE_RE.search(text) and len(current.bullets) == 0 and not current.dates:
                    # second header line (employer / dates)
                    current.header += " | " + text
                    current.dates = (DATE_RE.search(text) or [None, ""])[0] or current.dates
                    current.employer = current.employer or text
                    continue
                counters["exp"] += 1
                dates = DATE_RE.search(text)
                head = text
                title, employer = head, ""
                for sep in (" at ", " | ", " – ", " - ", ", "):
                    if sep in head:
                        title, employer = head.split(sep, 1)
                        break
                current = Experience(
                    id=f"{'proj' if section == 'projects' else 'exp'}_{counters['exp']}",
                    header=head,
                    employer=employer.strip(),
                    title=title.strip(),
                    dates=dates.group(0) if dates else "",
                    is_internship="intern" in text.lower() or "co-op" in text.lower(),
                )
                bucket.append(current)
            else:
                counters["b"] += 1
                if current is None:
                    current = Experience(id=f"exp_{counters['exp'] + 1}", header="", employer="", title="", dates="")
                    counters["exp"] += 1
                    bucket.append(current)
                current.bullets.append(Bullet(id=f"b_{counters['b']}", text=text.lstrip("•-–·▪● ").strip()))
        elif section == "education":
            education.append(text)
        elif section == "certifications":
            certs.append(text)
    return BaseResume(contact, " ".join(summary), skills, exp, projects, education, certs, "\n".join(all_text))


# -- grounding -------------------------------------------------------------------------


TECH_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9+#.]{1,}|[a-z]+(?:\.js|\.NET)|SQL|AWS|GCP|CI/CD)\b")
COMMON_WORDS = {
    "I",
    "The",
    "A",
    "An",
    "Built",
    "Led",
    "Designed",
    "Developed",
    "Delivered",
    "Improved",
    "Reduced",
    "Implemented",
    "Worked",
    "Collaborated",
    "Owned",
    "Created",
    "Maintained",
    "Supported",
    "Migrated",
    "Automated",
    "Wrote",
    "Backend",
    "Summary",
    "Senior",
    "Software",
    "Developer",
    "Engineer",
    "Team",
    "Toronto",
    "Canada",
    "Ontario",
    "Java",
    "Python",
}


@dataclass
class GroundingReport:
    ok: bool
    violations: list[str]
    checked_bullets: int


def check_grounding(tailored: dict, base: BaseResume) -> GroundingReport:
    violations: list[str] = []
    base_low = base.all_text.lower()
    ids = base.bullet_ids() | {e.id for e in base.experience + base.projects}
    checked = 0
    for exp in tailored.get("experience", []):
        if exp["experience_id"] not in {e.id for e in base.experience + base.projects}:
            violations.append(f"unknown experience {exp['experience_id']}")
        for b in exp.get("bullets", []):
            checked += 1
            if b.get("source_id") not in ids:
                violations.append(f"bullet without base source: {b.get('text', '')[:60]}")
            for tok in TECH_TOKEN_RE.findall(b.get("text", "")):
                if tok in COMMON_WORDS or tok.lower() in base_low or tok.rstrip("s").lower() in base_low:
                    continue
                violations.append(f"technology/term not in base: '{tok}'")
            for m in re.findall(r"\b(\d+)\+?\s+years?", b.get("text", ""), re.I):
                if f"{m} years" not in base_low and f"{m}+ years" not in base_low:
                    violations.append(f"years claim not in base: {m} years")
    base_skills_low = {s.lower() for s in base.skills}
    for s in tailored.get("skills", []):
        if s.lower() not in base_skills_low and s.lower() not in base_low:
            violations.append(f"skill not in base: {s}")
    if re.search(r"[-–—]", tailored.get("summary", "")):
        violations.append("summary contains a hyphen or dash")
    return GroundingReport(not violations, violations, checked)


def deterministic_tailor(base: BaseResume, posting_text: str, max_bullets_per_role: int = 5) -> dict:
    """No model: keep original text, reorder bullets by overlap with the posting, trim weakest."""
    terms = set(normalize_text(posting_text).split())
    out_exp = []
    for e in base.experience + base.projects:
        scored = sorted(e.bullets, key=lambda b: -len(set(normalize_text(b.text).split()) & terms))
        keep = scored[:max_bullets_per_role] if len(scored) > max_bullets_per_role else scored
        keep_ids = {b.id for b in keep}
        ordered = [b for b in e.bullets if b.id in keep_ids]
        out_exp.append({"experience_id": e.id, "bullets": [{"source_id": b.id, "text": b.text} for b in ordered]})
    skills = sorted(base.skills, key=lambda s: (normalize_text(s) not in terms, base.skills.index(s)))
    return {"summary": base.summary, "skills": skills, "experience": out_exp, "dropped_bullet_ids": [], "unsupported_requirements_note": ""}


# -- DOCX build --------------------------------------------------------------------------


def build_docx(base: BaseResume, tailored: dict, out_path: Path, font: str = "Calibri", body_pt: float = 11) -> Path:
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Inches(0.7)
        s.top_margin = s.bottom_margin = Inches(0.6)
    style = doc.styles["Normal"]
    style.font.name = font
    style.font.size = Pt(body_pt)

    def heading(text: str):
        p = doc.add_paragraph()
        r = p.add_run(text.upper())
        r.bold = True
        r.font.size = Pt(body_pt + 1.5)
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after = Pt(2)

    def para(text: str, bold=False, size=None, align=None, after=2):
        p = doc.add_paragraph()
        r = p.add_run(text)
        r.bold = bold
        if size:
            r.font.size = Pt(size)
        if align:
            p.alignment = align
        p.paragraph_format.space_after = Pt(after)
        return p

    if base.contact_lines:
        para(base.contact_lines[0], bold=True, size=body_pt + 5, align=WD_ALIGN_PARAGRAPH.CENTER, after=0)
        for line in base.contact_lines[1:]:
            para(line, align=WD_ALIGN_PARAGRAPH.CENTER, after=0)
    if tailored.get("summary"):
        heading("Summary")
        para(tailored["summary"], after=4)
    if tailored.get("skills"):
        heading("Technical Skills")
        para(", ".join(tailored["skills"]), after=4)
    by_id = {e.id: e for e in base.experience + base.projects}
    exp_items = [t for t in tailored.get("experience", []) if t["experience_id"].startswith("exp_")]
    proj_items = [t for t in tailored.get("experience", []) if t["experience_id"].startswith("proj_")]
    for label, items in (("Professional Experience", exp_items), ("Projects", proj_items)):
        if not items:
            continue
        heading(label)
        for t in items:
            e = by_id.get(t["experience_id"])
            if e is None:
                continue
            p = doc.add_paragraph()
            p.paragraph_format.keep_with_next = True
            p.paragraph_format.space_after = Pt(0)
            r = p.add_run(e.title or e.header)
            r.bold = True
            if e.employer or e.dates:
                p.add_run(f"  |  {e.employer}" if e.employer else "")
                p.add_run(f"  |  {e.dates}" if e.dates else "")
            for b in t.get("bullets", []):
                bp = doc.add_paragraph(style="List Bullet")
                bp.add_run(b["text"])
                bp.paragraph_format.space_after = Pt(1)
    if base.education:
        heading("Education")
        for line in base.education:
            para(line, after=1)
    if base.certifications:
        heading("Certifications")
        for line in base.certifications:
            para(line, after=1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def verify_docx_structure(path: Path) -> dict[str, Any]:
    doc = Document(str(path))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    return {
        "tables": len(doc.tables),
        "paragraphs": len(texts),
        "has_summary": any(t.strip().upper() == "SUMMARY" for t in texts),
        "has_experience": any("EXPERIENCE" in t.upper() for t in texts),
        "single_column": len(doc.tables) == 0,
        "first_line": texts[0] if texts else "",
    }


# -- PDF render ----------------------------------------------------------------------------


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    return str(mac) if mac.exists() else None


def render_pdf(docx_path: Path, timeout: int = 120) -> dict[str, Any]:
    exe = find_soffice()
    if not exe:
        return {"pdf_path": None, "pages": None, "verified": False, "reason": "no LibreOffice renderer available"}
    outdir = docx_path.parent
    try:
        subprocess.run(
            [exe, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(docx_path)],
            check=True,
            capture_output=True,
            timeout=timeout,
            env={"HOME": str(outdir), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"pdf_path": None, "pages": None, "verified": False, "reason": f"render failed: {type(exc).__name__}"}
    pdf = docx_path.with_suffix(".pdf")
    if not pdf.exists():
        return {"pdf_path": None, "pages": None, "verified": False, "reason": "renderer produced no file"}
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    pages = len(reader.pages)
    selectable = bool((reader.pages[0].extract_text() or "").strip())
    return {
        "pdf_path": str(pdf),
        "pages": pages,
        "verified": pages <= 2 and selectable,
        "selectable_text": selectable,
        "reason": "" if pages <= 2 else f"{pages} pages exceeds two",
    }


# -- service ------------------------------------------------------------------------------------


class Tailor:
    def __init__(self, store: Store, artifact_dir: Path, ai: AIRunner | None = None) -> None:
        self.store = store
        self.artifact_dir = artifact_dir
        self.ai = ai

    def tailor_for_job(self, job: dict, base_path: Path, base_sha256: str, fact_version: int, display_name: str = "Resume") -> dict:
        base = parse_base_resume(base_path)
        snapshot = job.get("latest_snapshot") or {}
        posting_text = snapshot.get("text", "")
        tailored: dict
        method = "deterministic"
        if self.ai is not None:
            try:
                res = self.ai.run(
                    "tailor_resume",
                    TailorInput(
                        posting_summary=posting_text[:8000],
                        base_resume_sections=base.as_sections(),
                        allowed_fact_ids=sorted(base.bullet_ids()),
                        target_role_terms=[job.get("title", "")],
                    ),
                    fact_version=fact_version,
                    posting_hash=snapshot.get("posting_hash"),
                    untrusted_fields=("posting_summary",),
                )
                tailored, method = res.output, "model"
            except ProviderUnavailable:
                tailored = deterministic_tailor(base, posting_text)
        else:
            tailored = deterministic_tailor(base, posting_text)
        report = check_grounding(tailored, base)
        if not report.ok and method == "model":
            log.warning("model tailoring rejected (%d violations); falling back to deterministic", len(report.violations))
            tailored = deterministic_tailor(base, posting_text)
            method = "deterministic_after_rejection"
            report = check_grounding(tailored, base)
        req = job.get("requisition_id") or job.get("linkedin_job_id") or job["_id"][-6:]
        fname = f"{slugify(display_name.replace(' ', '_'))}_Resume_{slugify(job['employer'])}_{slugify(job['title'], 40)}_{slugify(str(req), 24)}.docx"
        out_dir = self.artifact_dir / "jobs" / job["_id"]
        docx_path = build_docx(base, tailored, out_dir / fname)
        structure = verify_docx_structure(docx_path)
        pdf = render_pdf(docx_path)
        docx_hash = sha256_file(docx_path)
        artifact = {
            "_id": new_id("art"),
            "owner_id": job["_id"],
            "kind": "resume_tailored",
            "path": str(docx_path),
            "filename": fname,
            "sha256": docx_hash,
            "pdf_path": pdf.get("pdf_path"),
            "pdf_sha256": sha256_file(Path(pdf["pdf_path"])) if pdf.get("pdf_path") else None,
            "pages": pdf.get("pages"),
            "page_limit_verified": pdf.get("verified", False),
            "render_note": pdf.get("reason", ""),
            "structure": structure,
            "method": method,
            "grounding": {"ok": report.ok, "violations": report.violations, "checked_bullets": report.checked_bullets},
            "base_sha256": base_sha256,
            "fact_version": fact_version,
            "posting_hash": snapshot.get("posting_hash"),
            "created_at": utcnow(),
            "unsupported_requirements_note": tailored.get("unsupported_requirements_note", ""),
        }
        self.store.c("artifacts").insert_one(artifact)
        self.store.audit("resume_tailored", job["_id"], sha256=docx_hash, method=method, verified_pages=pdf.get("verified", False))
        return artifact
