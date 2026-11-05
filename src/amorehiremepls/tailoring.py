"""Resume tailoring: parse the approved base DOCX, produce a grounded per-requisition DOCX (+ PDF when a
renderer exists), tighten until the PDF is within the profile's page limit, and record artifacts with hashes.

Grounding: every tailored bullet cites a base bullet id; skills must exist in the base; new text may
not introduce technology tokens absent from the base or from the candidate's own extra notes (the
``candidate/`` folder, passed in as supplemental evidence). Violations are rejected, not silently kept.
Without a model provider, a deterministic tailoring (reorder by posting relevance, no rewriting) is used.

Output: the approved base DOCX is the template. It is opened as-is (header block, styles, fonts, margins,
section layout, drawings, headers/footers all kept verbatim) and only the text of the tailorable parts
(summary paragraph, skill lists, experience/project bullets) is replaced in place. Contact details are never
model output and are never rewritten. A fresh document is only generated when the base file is unavailable.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from docx.text.paragraph import Paragraph

from .ai import AIRunner, ProviderUnavailable
from .db import Store
from .skills import TailorInput
from .util import new_id, normalize_text, sha256_file, slugify, utcnow

log = logging.getLogger(__name__)

# Section headings the parser understands, across professions. A candidate adds their own wording under
# [resume].section_aliases ("skills" = ["toolbox"]). Any other short heading starts an "other" section that is kept
# word for word and never tailored (publications, languages, volunteer work, licences in a different wording).
HEADINGS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "professional summary", "profile", "professional profile", "career summary", "about me", "objective", "career objective"),
    "skills": (
        "technical skills",
        "skills",
        "core skills",
        "key skills",
        "technologies",
        "core competencies",
        "competencies",
        "areas of expertise",
        "expertise",
        "skills & tools",
        "skills and tools",
        "tools",
    ),
    "experience": (
        "professional experience",
        "experience",
        "work experience",
        "employment",
        "employment history",
        "career history",
        "work history",
        "relevant experience",
        "clinical experience",
        "sales experience",
        "teaching experience",
        "research experience",
    ),
    "projects": ("projects", "selected projects", "personal projects", "key projects", "key achievements", "selected achievements", "achievements", "accomplishments"),
    "education": ("education", "education & training", "education and training", "academic background", "qualifications"),
    "certifications": (
        "certifications",
        "certificates",
        "awards",
        "certifications & awards",
        "licenses",
        "licences",
        "licenses & certifications",
        "licences & certifications",
        "credentials",
        "professional affiliations",
        "affiliations",
        "memberships",
        "publications",
        "professional development",
    ),
}
# Headings of sections the app keeps word for word and never tailors. A plain paragraph with one of these names (or a
# Heading-styled / ALL CAPS short line) after the education or certifications section opens such a section. A
# candidate adds their own under [resume].section_aliases with the key "other".
OTHER_HEADINGS: tuple[str, ...] = (
    "languages",
    "language skills",
    "volunteer",
    "volunteering",
    "volunteer work",
    "volunteer experience",
    "community involvement",
    "community service",
    "interests",
    "hobbies",
    "hobbies & interests",
    "interests & hobbies",
    "references",
    "presentations",
    "talks",
    "patents",
    "additional information",
    "additional",
    "extracurricular activities",
    "activities",
    "leadership",
    "leadership & activities",
    "honors",
    "honours",
    "honors & awards",
    "honours & awards",
    "professional memberships",
    "board positions",
    "teaching",
    "media",
    "training",
    "courses",
    "continuing education",
)
OTHER_HEADING_RE = re.compile(r"^[A-Z][A-Za-z&/' ]{1,38}[A-Za-z]$")


def headings_with(rules: Any | None) -> dict[str, tuple[str, ...]]:
    """HEADINGS merged with the candidate's own aliases (``ResumeRules.section_aliases``); the ``other`` key lists
    extra verbatim-section names."""
    extra = getattr(rules, "section_aliases", None) or {}
    merged = {k: tuple(dict.fromkeys(normalize_text(n) for n in v)) for k, v in HEADINGS.items()}
    merged["other"] = tuple(dict.fromkeys(normalize_text(n) for n in OTHER_HEADINGS))
    for key, names in extra.items():
        if key in merged:
            merged[key] = tuple(dict.fromkeys([*merged[key], *(normalize_text(n) for n in names if n.strip())]))
    return merged


HEADINGS = headings_with(None)  # normalized once so lookups match ``normalize_text`` output ("licences & certifications")
DATE_RE = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}|\d{4})\s*[-–—to]+\s*((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}|\d{4}|present|current)",
    re.I,
)
# A line that is contact information rather than prose: email, phone, URL, LinkedIn/GitHub handle or a "a | b | c" strip.
CONTACT_RE = re.compile(r"@|https?://|www\.|linkedin\.com|github\.com|\(?\+?\d[\d\s().-]{7,}\d|\|.*\|", re.I)
BULLET_GLYPH_RE = re.compile(r"^[•\-–·▪●]\s*")
SKILL_SPLIT_RE = re.compile(r"[,|•;]|\s{2,}|\t")
SKILL_LABEL_RE = re.compile(r"^[^:,|•;]{1,40}:\s*")
TEXT_CHILD_TAGS = ("w:r", "w:hyperlink", "w:fldSimple", "w:smartTag", "w:ins", "w:del", "w:sdt")


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
    source_path: Path | None = None
    # Sections the parser does not know (heading -> its lines), kept word for word and never offered for tailoring.
    other: list[tuple[str, list[str]]] = field(default_factory=list)

    def bullet_ids(self) -> set[str]:
        return {b.id for e in self.experience + self.projects for b in e.bullets}

    def as_sections(self) -> dict[str, Any]:
        """What the model may rewrite. ``other`` sections are deliberately absent."""
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


def _heading_key(text: str, headings: dict[str, tuple[str, ...]] | None = None) -> str | None:
    t = normalize_text(text)
    if len(t) > 40:
        return None
    for key, names in (headings or HEADINGS).items():
        if key != "other" and t in names:
            return key
    return None


@dataclass
class SkillGroup:
    """One "Label: a, b, c" run of a skills paragraph: the label runs (bold) and the runs holding the items."""

    label_runs: list[Any]
    item_runs: list[Any]

    @property
    def items_text(self) -> str:
        return "".join(r.text for r in self.item_runs)


def _split_skill_items(text: str) -> list[str]:
    out: list[str] = []
    for tok in SKILL_SPLIT_RE.split(text):
        tok = SKILL_LABEL_RE.sub("", tok).strip(" •-")
        if tok:
            out.append(tok)
    return out


def _skill_groups(p: Paragraph) -> list[SkillGroup]:
    """Group a skills paragraph's runs into (label, items) pairs by explicit bold formatting; a paragraph without
    bold labels is a single unlabeled group."""
    groups: list[SkillGroup] = []
    runs = list(p.runs)
    if not any(r.bold for r in runs):
        return [SkillGroup([], runs)] if runs else []
    prev_label = False
    for r in runs:
        if r.bold:
            if not groups or not prev_label:
                groups.append(SkillGroup([], []))
            groups[-1].label_runs.append(r)
            prev_label = True
        else:
            if not groups:
                groups.append(SkillGroup([], []))
            groups[-1].item_runs.append(r)
            prev_label = False
    return groups


@dataclass
class _Layout:
    """Where the tailorable parts live in the base document (paragraph elements), for in-place replacement."""

    contact: list[Paragraph] = field(default_factory=list)
    summary: list[Paragraph] = field(default_factory=list)
    skills: list[Paragraph] = field(default_factory=list)
    bullets: dict[str, list[Paragraph]] = field(default_factory=dict)  # experience id -> bullet paragraphs


def _is_contact_line(p: Paragraph, text: str) -> bool:
    style = (p.style.name or "").lower()
    return style.startswith(("title", "heading 1")) or bool(CONTACT_RE.search(text)) or p._p.find(qn("w:hyperlink")) is not None


def _is_other_heading(p: Paragraph, text: str, section: str, other_names: tuple[str, ...]) -> bool:
    """A heading of a verbatim section, once the structured sections are behind us: a Heading-styled paragraph, a
    short ALL CAPS line, or a known extra-section name (``OTHER_HEADINGS`` plus the candidate's own). A title case
    line that is none of these is content, not a heading, so a certificate name never opens a section."""
    if section == "contact" or len(text) > 40 or DATE_RE.search(text) or CONTACT_RE.search(text) or text[:1] in "•-–·▪●":
        return False
    style = (p.style.name or "").lower()
    if style.startswith("heading"):
        return True
    if normalize_text(text) in other_names:
        return True
    bold = bool(p.runs) and all(r.bold for r in p.runs if r.text.strip())
    return bold and bool(OTHER_HEADING_RE.match(text)) and text.isupper() and len(text.split()) <= 4


def _parse_document(doc, rules: Any | None = None) -> tuple[BaseResume, _Layout]:
    from .profile import DEFAULT_TRAINING_ROLE_TERMS, training_role_pattern

    headings = headings_with(rules)
    training = training_role_pattern(getattr(rules, "training_role_terms", None) or DEFAULT_TRAINING_ROLE_TERMS)
    layout = _Layout()
    section = "contact"
    contact: list[str] = []
    summary: list[str] = []
    skills: list[str] = []
    exp: list[Experience] = []
    projects: list[Experience] = []
    education: list[str] = []
    certs: list[str] = []
    other: list[tuple[str, list[str]]] = []
    current: Experience | None = None
    bucket: list[Experience] = exp
    all_text: list[str] = []
    counters = {"exp": 0, "b": 0}
    for p in doc.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        all_text.append(text)
        key = _heading_key(text, headings)
        if key:
            section = key
            current = None
            bucket = projects if key == "projects" else exp
            continue
        is_bullet = (p.style.name or "").lower().startswith("list") or text[:1] in "•-–·▪●" or (p.paragraph_format.left_indent or 0) > 0
        other_names = headings["other"]
        if section == "other" or (section in ("education", "certifications") and _is_other_heading(p, text, section, other_names)):
            # An unknown heading after the structured sections opens a verbatim section; its lines follow it.
            if _is_other_heading(p, text, section, other_names):
                other.append((text, []))
                section = "other"
                current = None
                continue
            other[-1][1].append(text)
        elif section == "contact" or (section == "summary" and not summary and _is_contact_line(p, text)):
            # Name and contact strip. Some layouts (two-column first section with a column break) place the
            # "Summary" label before the name in document order; those lines are still the header, not the summary.
            contact.append(text)
            layout.contact.append(p)
        elif section == "summary":
            summary.append(text)
            layout.summary.append(p)
        elif section == "skills":
            groups = _skill_groups(p)
            skills.extend(_split_skill_items(", ".join(g.items_text for g in groups) if groups else text))
            layout.skills.append(p)
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
                    is_internship=training.search(text) is not None,
                )
                bucket.append(current)
            else:
                counters["b"] += 1
                if current is None:
                    current = Experience(id=f"exp_{counters['exp'] + 1}", header="", employer="", title="", dates="")
                    counters["exp"] += 1
                    bucket.append(current)
                current.bullets.append(Bullet(id=f"b_{counters['b']}", text=text.lstrip("•-–·▪● ").strip()))
                layout.bullets.setdefault(current.id, []).append(p)
        elif section == "education":
            education.append(text)
        elif section == "certifications":
            certs.append(text)
    return BaseResume(contact, " ".join(summary), skills, exp, projects, education, certs, "\n".join(all_text), other=other), layout


def parse_base_resume(path: Path, rules: Any | None = None) -> BaseResume:
    """Read the approved base resume. ``rules`` is the profile's ``ResumeRules`` (section aliases, training role words)."""
    base, _ = _parse_document(Document(str(path)), rules)
    base.source_path = Path(path)
    return base


# -- grounding -------------------------------------------------------------------------


# A capitalised or product-looking token in rewritten text must already appear in the evidence. The allow-list holds
# only sentence-start grammar words and common verbs; a candidate's own domain words (a city, a product, a title) go
# under [resume].allowed_terms when they are not in the base resume text.
TECH_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9+#.]{1,}|[a-z]+(?:\.js|\.NET)|SQL|AWS|GCP|CI/CD)\b")
COMMON_WORDS = {
    "I",
    "The",
    "A",
    "An",
    "And",
    "As",
    "At",
    "By",
    "For",
    "In",
    "Of",
    "On",
    "To",
    "With",
    "Built",
    "Led",
    "Designed",
    "Developed",
    "Delivered",
    "Improved",
    "Reduced",
    "Increased",
    "Grew",
    "Implemented",
    "Worked",
    "Collaborated",
    "Owned",
    "Created",
    "Maintained",
    "Supported",
    "Managed",
    "Coordinated",
    "Launched",
    "Migrated",
    "Automated",
    "Wrote",
    "Team",
}


@dataclass
class GroundingReport:
    ok: bool
    violations: list[str]
    checked_bullets: int


def check_grounding(tailored: dict, base: BaseResume, supplemental_text: str = "", allowed_terms: Any = (), no_dashes: bool = True) -> GroundingReport:
    """Every bullet cites a base bullet; terms, years and skills must appear in the base or in the candidate's own
    supplemental notes (``supplemental_text``, from the candidate folder). The notes never ground a bullet id.
    ``allowed_terms`` ([resume].allowed_terms) are extra words that never count as unsupported claims; ``no_dashes``
    ([style].no_dashes) applies the no-hyphen rule to a rewritten summary."""
    violations: list[str] = []
    base_low = base.all_text.lower()
    if supplemental_text:
        base_low = base_low + "\n" + supplemental_text.lower()
    allowed = COMMON_WORDS | {str(t).strip() for t in (allowed_terms or ()) if str(t).strip()}
    allowed_low = {t.lower() for t in allowed}
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
                if tok in allowed or tok.lower() in allowed_low or tok.lower() in base_low or tok.rstrip("s").lower() in base_low:
                    continue
                violations.append(f"term not in evidence: '{tok}'")
            for m in re.findall(r"\b(\d+)\+?\s+years?", b.get("text", ""), re.I):
                if f"{m} years" not in base_low and f"{m}+ years" not in base_low:
                    violations.append(f"years claim not in evidence: {m} years")
    base_skills_low = {s.lower() for s in base.skills}
    for s in tailored.get("skills", []):
        if s.lower() not in base_skills_low and s.lower() not in base_low:
            violations.append(f"skill not in evidence: {s}")
    summary = tailored.get("summary", "")
    # The approved base summary is the candidate's own wording and passes as-is; the no-dash style rule applies to rewritten text.
    if no_dashes and summary.strip() != (base.summary or "").strip() and re.search(r"[-–—]", summary):
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


# -- page budget ---------------------------------------------------------------------------

# Default page limit when a profile has no [resume].max_pages. Every rendering/tightening step takes ``max_pages``.
PAGE_LIMIT = 2
_TRIM_CAPS = (4, 2, 1)
_PAGE_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def page_word(max_pages: int) -> str:
    return _PAGE_WORDS.get(int(max_pages), str(max_pages))


def page_budget_text(max_pages: int) -> str:
    """The budget string handed to the model and shown in copy: '2 pages', '1 page'."""
    return f"{int(max_pages)} page" + ("" if int(max_pages) == 1 else "s")


def page_limit_block(max_pages: int = PAGE_LIMIT) -> str:
    return f"resume PDF is not verified at {page_word(max_pages)} page{'' if int(max_pages) == 1 else 's'} or fewer"


PAGE_LIMIT_BLOCK = page_limit_block(PAGE_LIMIT)


def _posting_terms(posting_text: str) -> set[str]:
    return {t for t in normalize_text(posting_text).split() if len(t) > 2}


def _relevance(text: str, terms: set[str]) -> int:
    return len(set(normalize_text(text).split()) & terms)


def _tailor_signature(tailored: dict) -> tuple:
    return (
        tailored.get("summary") or "",
        tuple((e.get("experience_id"), tuple((b.get("source_id"), b.get("text")) for b in e.get("bullets") or [])) for e in tailored.get("experience") or []),
    )


def _cap_bullets(tailored: dict, cap: int, terms: set[str]) -> dict:
    """Drop the lowest-relevance bullets until each role has at most ``cap``, keeping the original order of the rest."""
    out = deepcopy(tailored)
    dropped: list[str] = []
    for exp in out.get("experience") or []:
        bullets = list(exp.get("bullets") or [])
        if len(bullets) <= cap:
            continue
        ranked = sorted(range(len(bullets)), key=lambda i: (_relevance(bullets[i].get("text", ""), terms), -i), reverse=True)
        keep = set(ranked[:cap])
        exp["bullets"] = [b for i, b in enumerate(bullets) if i in keep]
        dropped.extend(b.get("source_id") for i, b in enumerate(bullets) if i not in keep and b.get("source_id"))
    if dropped:
        out["dropped_bullet_ids"] = list(dict.fromkeys([*(out.get("dropped_bullet_ids") or []), *dropped]))
    return out


def _shorten_summary(summary: str, base: BaseResume) -> str | None:
    """A shorter prefix of ``summary`` that still passes grounding. None when nothing shorter is allowed."""
    text = " ".join((summary or "").split())
    if not text:
        return None
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    candidates: list[str] = []
    if len(sentences) > 2:
        candidates.append(" ".join(sentences[:2]))
    if len(sentences) > 1:
        candidates.append(sentences[0])
    words = text.split()
    for n in (24, 12):
        if len(words) > n:
            candidates.append(" ".join(words[:n]).rstrip(",;:") + ".")
    seen: set[str] = set()
    for cand in candidates:
        cand = " ".join(cand.split())
        if not cand or cand == text or cand in seen:
            continue
        seen.add(cand)
        if check_grounding({"summary": cand, "skills": [], "experience": []}, base).ok:
            return cand
    return None


def tighten_once(tailored: dict, base: BaseResume, posting_text: str) -> dict | None:
    """One deterministic reduction: cap bullets (lowest relevance first), then shorten the summary."""
    terms = _posting_terms(posting_text)
    widest = max((len(e.get("bullets") or []) for e in tailored.get("experience") or []), default=0)
    for cap in _TRIM_CAPS:
        if widest > cap:
            return _cap_bullets(tailored, cap, terms)
    shortened = _shorten_summary(tailored.get("summary") or "", base)
    if not shortened:
        return None
    out = deepcopy(tailored)
    out["summary"] = shortened
    return out


def page_limit_block_reason(record: dict | None, max_pages: int = PAGE_LIMIT) -> str | None:
    """Plain reason a package must not be approved or submitted. None when a verified PDF within the page limit is present."""
    if record and record.get("page_limit_verified"):
        pdf = record.get("pdf_path") or record.get("path")
        if pdf and str(pdf).lower().endswith(".pdf"):
            return None
    note = ((record or {}).get("render_note") or "").strip()
    return note or page_limit_block(max_pages)


# -- DOCX build: base document as template -------------------------------------------------


def _set_paragraph_text(p: Paragraph, text: str) -> None:
    """Replace the paragraph's text while keeping its paragraph properties and the formatting of its first text run."""
    runs = p.runs
    keep = next((r for r in runs if r.text.strip()), runs[0] if runs else None)
    if keep is None:
        keep = p.add_run()
    for child in list(p._p):
        if child is keep._r or child.tag == qn("w:pPr"):
            continue
        if child.tag in {qn(t) for t in TEXT_CHILD_TAGS}:
            p._p.remove(child)
    rpr = keep._r.find(qn("w:rPr"))
    if rpr is not None:
        keep._r.replace(rpr, _clean_rpr(rpr))
    keep.text = text


def _clone_paragraph_after(src: Paragraph, anchor: Paragraph) -> Paragraph:
    """Copy of ``src`` inserted after ``anchor``; a section break in the source is not duplicated."""
    el = deepcopy(src._p)
    if el.pPr is not None:
        for sect in el.pPr.findall(qn("w:sectPr")):
            el.pPr.remove(sect)
    anchor._p.addnext(el)
    return Paragraph(el, src._parent)


def _remove_paragraph(p: Paragraph) -> None:
    """Drop a paragraph; a paragraph that carries a section break is blanked instead so the layout survives."""
    if p._p.pPr is not None and p._p.pPr.find(qn("w:sectPr")) is not None:
        _set_paragraph_text(p, "")
        return
    p._p.getparent().remove(p._p)


def _rewrite_summary(paras: list[Paragraph], summary: str) -> None:
    _set_paragraph_text(paras[0], summary)
    for p in paras[1:]:
        _remove_paragraph(p)


def _skill_matches(item: str, wanted_norm: list[str], consumed: set[int]) -> bool:
    n = normalize_text(item)
    for i, w in enumerate(wanted_norm):
        if w == n or (len(w) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", n)):
            consumed.add(i)
            return True
    return False


@dataclass
class _SkillLine:
    """One output line of the skills block: ``Label:<tab>items`` built from a base group."""

    paragraph: Paragraph  # base paragraph the group came from (formatting source)
    label: str  # label text without trailing whitespace, "" when the base line has no label
    label_rpr: Any  # rPr element of the base label run (None when unlabeled)
    item_rpr: Any  # rPr element of the first base item run
    sep: str
    items: list[str]


def _clean_rpr(rpr: Any) -> Any:
    """Copy of a run's properties without per-word character spacing/scale tweaks (PDF conversion artifacts)."""
    if rpr is None:
        return None
    out = deepcopy(rpr)
    for tag in ("w:spacing", "w:w", "w:kern", "w:position"):
        for el in out.findall(qn(tag)):
            out.remove(el)
    return out


def _add_run(p: Paragraph, text: str, rpr: Any) -> None:
    run = p.add_run()
    if rpr is not None:
        run._r.insert(0, deepcopy(rpr))
    run.text = text


def _write_skill_line(p: Paragraph, line: _SkillLine) -> None:
    """Fill ``p`` with ``Label:<tab>items`` (or just the items), one group per paragraph, no right-indent wrap."""
    for child in list(p._p):
        if child.tag != qn("w:pPr") and child.tag in {qn(t) for t in TEXT_CHILD_TAGS}:
            p._p.remove(child)
    ind = p._p.pPr.find(qn("w:ind")) if p._p.pPr is not None else None
    if ind is not None:
        for attr in ("w:end", "w:right"):
            if ind.get(qn(attr)) not in (None, "0"):
                ind.set(qn(attr), "0")
    spacing = p._p.pPr.find(qn("w:spacing")) if p._p.pPr is not None else None
    if spacing is not None:  # uniform single line spacing across the block (conversions tune it per wrapped paragraph)
        for attr in ("w:line", "w:lineRule"):
            if spacing.get(qn(attr)) is not None:
                del spacing.attrib[qn(attr)]
    if line.label:
        _add_run(p, line.label, line.label_rpr)
        _add_run(p, "\t", line.label_rpr)
    _add_run(p, line.sep.join(line.items), line.item_rpr)


def _rewrite_skills(paras: list[Paragraph], skills: list[str]) -> None:
    """Rebuild the skills block one group per line. A base paragraph that holds several ``Label:<tab>items``
    groups (PDF conversions wrap them with a tuned right indent) is split into one paragraph per group, so a
    label can never land mid-line when items change. Items the tailoring left out are dropped (an emptied group
    disappears with its line); tailored skills without a base item go to the "Other"-style group, to the only
    group when the base has a single unlabeled list, or to a new "Other" line formatted like the last one."""
    wanted_norm = [normalize_text(s) for s in skills]
    consumed: set[int] = set()
    lines: list[_SkillLine] = []
    for p in paras:
        for g in _skill_groups(p):
            if not g.item_runs:
                continue
            raw = g.items_text
            sep = " | " if "|" in raw else "; " if ";" in raw else " • " if "•" in raw else ", "
            label_rpr = g.label_runs[0]._r.find(qn("w:rPr")) if g.label_runs else None
            label = "".join(r.text for r in g.label_runs).strip()
            if not g.label_runs:
                m = SKILL_LABEL_RE.match(raw.strip())
                label = m.group(0).strip() if m else ""
                label_rpr = g.item_runs[0]._r.find(qn("w:rPr"))
            items = [it for it in _split_skill_items(raw) if _skill_matches(it, wanted_norm, consumed)]
            lines.append(_SkillLine(p, label, _clean_rpr(label_rpr), _clean_rpr(g.item_runs[0]._r.find(qn("w:rPr"))), sep, items))
    extra = [s for i, s in enumerate(skills) if i not in consumed]
    if extra and lines:
        target = next((ln for ln in lines if re.match(r"^(other|others|additional|misc|miscellaneous)\b", ln.label, re.I)), None)
        if target is None and len(lines) == 1 and not lines[0].label:
            target = lines[0]
        if target is None:
            last = lines[-1]
            target = _SkillLine(last.paragraph, "Other:" if last.label else "", last.label_rpr, last.item_rpr, last.sep, [])
            lines.append(target)
        target.items.extend(extra)
    # emit: the first surviving line of each base paragraph reuses it; further lines are new paragraphs with the
    # same paragraph properties, inserted in order; base paragraphs with nothing left are removed
    used: set[int] = set()
    anchor: Paragraph | None = None
    for line in lines:
        if not line.items:
            continue
        if id(line.paragraph._p) not in used:
            used.add(id(line.paragraph._p))
            target = line.paragraph
        else:
            target = _clone_paragraph_after(line.paragraph, anchor or line.paragraph)
        _write_skill_line(target, line)
        anchor = target
    for p in paras:
        if id(p._p) not in used:
            _remove_paragraph(p)


def _rewrite_bullets(paras: list[Paragraph], texts: list[str]) -> None:
    """Rewrite one role's bullet paragraphs in place: first paragraph is the formatting template, extra bullets are
    clones of it, leftover base bullets are removed."""
    glyph = BULLET_GLYPH_RE.match(paras[0].text)
    prefix = glyph.group(0) if glyph else ""
    anchor = paras[0]
    for i, text in enumerate(texts):
        p = paras[i] if i < len(paras) else _clone_paragraph_after(paras[0], anchor)
        _set_paragraph_text(p, prefix + text)
        anchor = p
    for p in paras[len(texts) :]:
        _remove_paragraph(p)


def _set_document_properties(doc, title: str, author: str) -> None:
    """Plain document properties for ATS parsers and reviewers: the person's name and a readable title, no ids."""
    props = doc.core_properties
    if author:
        props.author = author
        props.last_modified_by = author
    if title:
        props.title = title
    props.modified = utcnow()


def build_docx(base: BaseResume, tailored: dict, out_path: Path, font: str = "Calibri", body_pt: float = 11, title: str = "", author: str = "") -> Path:
    """Write the tailored resume. The base DOCX is the template whenever it is available: everything outside the
    summary, skills and bullets stays byte-for-byte as approved (name/contact block included)."""
    if base.source_path and Path(base.source_path).exists():
        return _build_from_template(base, tailored, out_path, title, author)
    log.warning("base resume file unavailable; generating a fresh document instead of using it as the template")
    return _build_fresh(base, tailored, out_path, font, body_pt, title, author)


def _build_from_template(base: BaseResume, tailored: dict, out_path: Path, title: str = "", author: str = "") -> Path:
    doc = Document(str(base.source_path))
    parsed, layout = _parse_document(doc)
    if parsed.all_text != base.all_text or parsed.bullet_ids() != base.bullet_ids():
        raise ValueError("base resume file no longer matches the parsed base resume; refusing to tailor against a changed template")
    if tailored.get("summary") and layout.summary:
        _rewrite_summary(layout.summary, tailored["summary"])
    if tailored.get("skills") and layout.skills:
        _rewrite_skills(layout.skills, list(tailored["skills"]))
    for item in tailored.get("experience", []):
        paras = layout.bullets.get(item["experience_id"])
        if paras:
            _rewrite_bullets(paras, [b["text"] for b in item.get("bullets", [])])
    # A later section that starts on a new page pins the base's page boundary; with shorter or longer tailored text
    # that leaves a gap or pushes onto a third page, so let the body flow (the first section's layout is untouched).
    for section in list(doc.sections)[1:]:
        if section.start_type == WD_SECTION.NEW_PAGE:
            section.start_type = WD_SECTION.CONTINUOUS
    _set_document_properties(doc, title, author)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def _build_fresh(base: BaseResume, tailored: dict, out_path: Path, font: str, body_pt: float, title: str = "", author: str = "") -> Path:
    doc = Document()
    _set_document_properties(doc, title, author)
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
        heading("Skills")
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
    for label, lines in base.other:  # unknown sections: the candidate's wording, untouched
        heading(label)
        for line in lines:
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


SOFFICE_FALLBACK_PATHS = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
)
_BASE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name) or shutil.which(name, path=_BASE_PATH)
        if p:
            return p
    for candidate in SOFFICE_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def _render_env(exe: str) -> dict[str, str]:
    """Environment for the renderer: real HOME (macOS ``~/Library`` lookups), full PATH plus the executable's own
    directory (Homebrew's ``soffice`` is a wrapper that execs the app bundle), no inherited LibreOffice overrides."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SAL_", "OOO_", "URE_"))}
    parts = [str(Path(exe).resolve().parent), *(env.get("PATH") or "").split(os.pathsep), *_BASE_PATH.split(os.pathsep)]
    seen: set[str] = set()
    env["PATH"] = os.pathsep.join(p for p in parts if p and not (p in seen or seen.add(p)))
    env.setdefault("HOME", str(Path.home()))
    return env


def _redact_paths(text: str) -> str:
    text = text.replace(str(Path.home()), "~")
    return re.sub(r"/(?:Users|home)/[^/\s]+", "~", text)


def _failure_reason(exc: BaseException, stderr_tail: int = 300) -> str:
    raw = getattr(exc, "stderr", None) or getattr(exc, "output", None) or b""
    stderr = (raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)).strip()
    stderr = _redact_paths(" ".join(stderr.split()))[-stderr_tail:]
    if isinstance(exc, subprocess.TimeoutExpired):
        head = f"render failed: timeout after {int(exc.timeout)}s"
    elif isinstance(exc, subprocess.CalledProcessError):
        head = f"render failed: exit code {exc.returncode}"
    else:
        head = f"render failed: {type(exc).__name__}: {_redact_paths(str(exc))}"
    return f"{head}; stderr: {stderr}" if stderr else f"{head}; no stderr"


def render_pdf(docx_path: Path, timeout: int = 120, exe: str | None = None, max_pages: int = PAGE_LIMIT) -> dict[str, Any]:
    exe = exe or find_soffice()
    if not exe:
        return {"pdf_path": None, "pages": None, "verified": False, "reason": "no LibreOffice renderer available"}
    outdir = docx_path.parent
    pdf = docx_path.with_suffix(".pdf")
    stale_mtime = pdf.stat().st_mtime_ns if pdf.exists() else None
    # Per-call user profile: a concurrent soffice (GUI or another worker) sharing a profile would hijack or block
    # the conversion, and a profile must never be created inside the artifact directory.
    with tempfile.TemporaryDirectory(prefix="amore-soffice-") as profile_dir:
        cmd = [
            exe,
            f"-env:UserInstallation={Path(profile_dir).resolve().as_uri()}",
            "--headless",
            "--norestore",
            "--nologo",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(docx_path),
        ]
        try:
            proc = subprocess.run(cmd, check=True, capture_output=True, timeout=timeout, env=_render_env(exe))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            reason = _failure_reason(exc)
            log.warning("PDF render failed for %s: %s", docx_path.name, reason)
            return {"pdf_path": None, "pages": None, "verified": False, "reason": reason}
    if not pdf.exists() or (stale_mtime is not None and pdf.stat().st_mtime_ns == stale_mtime):
        tail = _redact_paths(" ".join((proc.stderr or proc.stdout or b"").decode(errors="replace").split()))[-300:]
        return {"pdf_path": None, "pages": None, "verified": False, "reason": f"renderer produced no file; output: {tail or 'none'}"}
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    pages = len(reader.pages)
    selectable = bool((reader.pages[0].extract_text() or "").strip())
    return {
        "pdf_path": str(pdf),
        "pages": pages,
        "verified": pages <= max_pages and selectable,
        "selectable_text": selectable,
        "reason": "" if pages <= max_pages else f"{pages} pages exceeds {page_word(max_pages)}",
    }


def _pages_over(pdf: dict, max_pages: int = PAGE_LIMIT) -> bool:
    pages = pdf.get("pages")
    return isinstance(pages, int) and pages > max_pages


def _mark_over(pdf: dict, max_pages: int = PAGE_LIMIT) -> dict:
    return {**pdf, "verified": False, "reason": f"{pdf.get('pages')} pages exceeds {page_word(max_pages)} after tightening"}


def _write_and_render(base: BaseResume, tailored: dict, docx_path: Path, render: Callable[[Path], dict], title: str, author: str) -> dict:
    build_docx(base, tailored, docx_path, title=title, author=author)
    return render(docx_path)


def fit_to_page_limit(
    base: BaseResume,
    tailored: dict,
    posting_text: str,
    docx_path: Path,
    *,
    render: Callable[[Path], dict] = render_pdf,
    title: str = "",
    author: str = "",
    max_steps: int = 6,
    max_pages: int = PAGE_LIMIT,
) -> tuple[dict, dict, bool]:
    """Write and render, then drop the lowest-relevance extra bullets, cap bullets per role, and shorten the summary
    until the PDF is within ``max_pages`` or nothing grounded is left to remove. The header block is never rewritten.
    Returns ``(tailored, pdf, trimmed)``. A caller that is still over may try one model re-tailor."""
    current = tailored
    pdf = _write_and_render(base, current, docx_path, render, title, author)
    trimmed = False
    seen: set[tuple] = set()
    for _ in range(max_steps):
        if not _pages_over(pdf, max_pages):
            break
        sig = _tailor_signature(current)
        if sig in seen:
            break
        seen.add(sig)
        nxt = tighten_once(current, base, posting_text)
        if nxt is None or _tailor_signature(nxt) == sig:
            break
        current = nxt
        trimmed = True
        pdf = _write_and_render(base, current, docx_path, render, title, author)
        log.info("page tighten step left %s pages", pdf.get("pages"))
    return current, pdf, trimmed


# -- service ------------------------------------------------------------------------------------


def page_budget_retry(max_pages: int) -> str:
    """The tighter budget string for the one model re-tailor after the PDF rendered over the limit."""
    return (
        f"{page_budget_text(max_pages)}. The previous draft rendered over {page_word(max_pages)} page{'' if int(max_pages) == 1 else 's'}. "
        "Shorten it: summary at most two sentences, at most two bullets per role, drop the lowest relevance bullets."
    )


_PAGE_BUDGET_RETRY = page_budget_retry(PAGE_LIMIT)


class Tailor:
    def __init__(
        self,
        store: Store,
        artifact_dir: Path,
        ai: AIRunner | None = None,
        render: Callable[[Path], dict] | None = None,
        supplemental_text: str = "",
        supplemental_sha256: str = "",
        preferences: Any | None = None,
        profession: str = "",
    ) -> None:
        self.store = store
        self.artifact_dir = artifact_dir
        self.ai = ai
        self.profession = profession or ""
        # Extra career notes from the candidate folder: evidence for both model calls and for grounding; the base stays the template.
        self.supplemental_text = supplemental_text or ""
        self.supplemental_sha256 = supplemental_sha256 or ""
        # The profile's resume/style/fit rules: page limit, bullet cap, allowed terms, dash rule, role families, profession.
        self.preferences = preferences
        resume_rules = getattr(preferences, "resume", None)
        self.max_pages = int(getattr(resume_rules, "max_pages", PAGE_LIMIT) or PAGE_LIMIT)
        self.max_bullets = int(getattr(resume_rules, "max_bullets_per_role", 4) or 4)
        self.allowed_terms = list(getattr(resume_rules, "allowed_terms", None) or [])
        self.no_dashes = bool(getattr(getattr(preferences, "style", None), "no_dashes", True))
        self.resume_rules = resume_rules
        self.render = render or (lambda path: render_pdf(path, max_pages=self.max_pages))

    def _tailor_input(self, base: BaseResume, posting_text: str, job: dict, page_budget: str | None = None) -> TailorInput:
        return TailorInput(
            posting_summary=(posting_text or "")[:8000],
            base_resume_sections=base.as_sections(),
            allowed_fact_ids=sorted(base.bullet_ids()),
            target_role_terms=[job.get("title", "")],
            supplemental_evidence=self.supplemental_text,
            page_budget=page_budget or page_budget_text(self.max_pages),
            max_bullets_per_role=self.max_bullets,
            role_families=list(getattr(self.preferences, "role_families", None) or []),
            candidate_profession=self.profession,
        )

    def _grounding(self, tailored: dict, base: BaseResume) -> GroundingReport:
        return check_grounding(tailored, base, self.supplemental_text, allowed_terms=self.allowed_terms, no_dashes=self.no_dashes)

    def tailor_for_job(self, job: dict, base_path: Path, base_sha256: str, fact_version: int, display_name: str = "Resume") -> dict:
        base = parse_base_resume(base_path, rules=self.resume_rules)
        snapshot = job.get("latest_snapshot") or {}
        posting_text = snapshot.get("text", "")
        tailored: dict
        method = "deterministic"
        if self.ai is not None:
            try:
                res = self.ai.run(
                    "tailor_resume",
                    self._tailor_input(base, posting_text, job),
                    fact_version=fact_version,
                    posting_hash=snapshot.get("posting_hash"),
                    untrusted_fields=("posting_summary", "supplemental_evidence"),
                )
                tailored, method = res.output, "model"
            except ProviderUnavailable:
                tailored = deterministic_tailor(base, posting_text, max_bullets_per_role=max(self.max_bullets, 1) + 1)
        else:
            tailored = deterministic_tailor(base, posting_text, max_bullets_per_role=max(self.max_bullets, 1) + 1)
        report = self._grounding(tailored, base)
        if not report.ok and method == "model":
            log.warning("model tailoring rejected (%d violations); falling back to deterministic", len(report.violations))
            tailored = deterministic_tailor(base, posting_text, max_bullets_per_role=max(self.max_bullets, 1) + 1)
            method = "deterministic_after_rejection"
            report = self._grounding(tailored, base)
        req = job.get("requisition_id") or job.get("linkedin_job_id") or job["_id"][-6:]
        fname = f"{slugify(display_name.replace(' ', '_'))}_Resume_{slugify(job['employer'])}_{slugify(job['title'], 40)}_{slugify(str(req), 24)}.docx"
        out_dir = self.artifact_dir / "jobs" / job["_id"]
        person = "" if display_name.strip().lower() == "resume" else display_name.strip()
        title = f"{person} Resume".strip()
        docx_path = out_dir / fname
        tailored, pdf, method = self._enforce_page_limit(base, tailored, posting_text, docx_path, job, fact_version, snapshot, method, title, person)
        report = self._grounding(tailored, base)
        structure = verify_docx_structure(docx_path)
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
            "supplemental_sha256": self.supplemental_sha256 or None,  # which candidate-folder notes were evidence, if any
            "fact_version": fact_version,
            "posting_hash": snapshot.get("posting_hash"),
            "created_at": utcnow(),
            "unsupported_requirements_note": tailored.get("unsupported_requirements_note", ""),
        }
        self.store.c("artifacts").insert_one(artifact)
        self.store.audit("resume_tailored", job["_id"], sha256=docx_hash, method=method, verified_pages=pdf.get("verified", False))
        return artifact

    def _enforce_page_limit(
        self,
        base: BaseResume,
        tailored: dict,
        posting_text: str,
        docx_path: Path,
        job: dict,
        fact_version: int,
        snapshot: dict,
        method: str,
        title: str,
        author: str,
    ) -> tuple[dict, dict, str]:
        """Deterministic trim first. If the PDF is still over the page limit, one model re-tailor with an explicit budget."""
        current, pdf, trimmed = fit_to_page_limit(base, tailored, posting_text, docx_path, render=self.render, title=title, author=author, max_pages=self.max_pages)
        if not _pages_over(pdf, self.max_pages):
            return current, pdf, f"{method}_trimmed" if trimmed else method
        revised = self._page_budget_retaylor(base, job, fact_version, snapshot, posting_text)
        if revised is not None:
            current = revised
            pdf = _write_and_render(base, current, docx_path, self.render, title, author)
            method = "model_page_budget"
        elif trimmed:
            method = f"{method}_trimmed"
        if _pages_over(pdf, self.max_pages):
            pdf = _mark_over(pdf, self.max_pages)
        return current, pdf, method

    def _page_budget_retaylor(self, base: BaseResume, job: dict, fact_version: int, snapshot: dict, posting_text: str) -> dict | None:
        """One bounded call. Ungrounded or unavailable output is discarded; the caller keeps the trimmed draft."""
        if self.ai is None:
            return None
        try:
            res = self.ai.run(
                "tailor_resume",
                self._tailor_input(base, posting_text, job, page_budget=page_budget_retry(self.max_pages)),
                fact_version=fact_version,
                posting_hash=snapshot.get("posting_hash"),
                untrusted_fields=("posting_summary", "supplemental_evidence"),
            )
        except (ProviderUnavailable, ValueError):
            log.warning("page-budget re-tailor unavailable; keeping the tightened draft")
            return None
        if not self._grounding(res.output, base).ok:
            log.warning("page-budget re-tailor rejected by grounding; keeping the tightened draft")
            return None
        return res.output
