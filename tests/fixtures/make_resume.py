"""Synthetic base resume DOCX for tests. Fictional person; no real personal data."""

from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

FIXTURE_NAME = "Alex Example"
FIXTURE_CONTACT = "Milton, Ontario | alex@example.com | 555 000 0000 | linkedin.com/in/alexexample"


def make_base_resume(path: Path) -> Path:
    """Plain single-column layout: name and contact line are the first two body paragraphs."""
    doc = Document()
    doc.add_paragraph(FIXTURE_NAME)
    doc.add_paragraph(FIXTURE_CONTACT)
    doc.add_paragraph("Summary")
    doc.add_paragraph("Backend developer with three years of full time experience building Java and Spring Boot services for a large bank.")
    doc.add_paragraph("Technical Skills")
    doc.add_paragraph("Java, Spring Boot, Python, REST APIs, PostgreSQL, Docker, Kafka, Git")
    doc.add_paragraph("Professional Experience")
    doc.add_paragraph("Software Developer at Big Bank | May 2021 – March 2025")
    doc.add_paragraph("Built REST APIs in Java and Spring Boot serving 2 million daily requests.", style="List Bullet")
    doc.add_paragraph("Reduced batch processing time by 40% by tuning PostgreSQL queries.", style="List Bullet")
    doc.add_paragraph("Led a hackathon health monitoring project that expanded into a working group.", style="List Bullet")
    doc.add_paragraph("Automated deployments with Docker and GitHub Actions.", style="List Bullet")
    doc.add_paragraph("Software Developer Intern at Startup Co | May 2022 – August 2022")
    doc.add_paragraph("Wrote Python scripts to process Kafka events for analytics.", style="List Bullet")
    doc.add_paragraph("Projects")
    doc.add_paragraph("Instrument CLI | 2026")
    doc.add_paragraph("Command line tool in Python for AI assisted development workflows.", style="List Bullet")
    doc.add_paragraph("Education")
    doc.add_paragraph("Honours Associates, Computer Programming, Example College")
    doc.add_paragraph("Certifications")
    doc.add_paragraph("AWS Certified Cloud Practitioner")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _hyperlink_run(paragraph, text: str, url: str) -> None:
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _labelled_skills(doc, label: str, items: str, style: str) -> None:
    p = doc.add_paragraph(style=style)
    p.add_run(label + ":").bold = True
    p.add_run("\t").bold = True
    for i, item in enumerate(items.split(", ")):
        p.add_run(item + ("," if i < len(items.split(", ")) - 1 else ""))
        if i < len(items.split(", ")) - 1:
            p.add_run(" ")


def make_column_header_resume(path: Path) -> Path:
    """Mirror of the structure of a PDF-converted two-column header (fictional content):

    * section 1 has two unequal columns; column 1 holds three empty paragraphs and the bold "SUMMARY" label,
      a column break moves the Heading 1 name and the contact line (with a hyperlink) into column 2, so in
      document order the SUMMARY label comes *before* the name;
    * section 2 (continuous) is the single-column body with Heading 2 section titles, "Label:<tab>items" skills
      paragraphs with bold labels and multi-run bullets;
    * section 3 starts on a new page (education and certifications).
    """
    doc = Document()
    for _ in range(3):
        doc.add_paragraph("")
    doc.add_paragraph().add_run("SUMMARY").bold = True
    name = doc.add_paragraph(style="Heading 1")
    name.add_run().add_break(WD_BREAK.COLUMN)
    name.add_run(FIXTURE_NAME)
    contact = doc.add_paragraph("Milton, Ontario | ")
    _hyperlink_run(contact, "alex@example.com", "mailto:alex@example.com")
    contact.add_run(" | 555 000 0000 | linkedin.com/in/alexexample")
    doc.add_section(WD_SECTION.CONTINUOUS)
    sect_pr = doc.sections[0]._sectPr
    cols = sect_pr.find(qn("w:cols"))
    if cols is None:
        cols = OxmlElement("w:cols")
        sect_pr.append(cols)
    cols.set(qn("w:num"), "2")
    cols.set(qn("w:equalWidth"), "false")
    for width, space in (("1368", "916"), ("8515", None)):
        col = OxmlElement("w:col")
        col.set(qn("w:w"), width)
        if space:
            col.set(qn("w:space"), space)
        cols.append(col)

    summary = doc.add_paragraph()
    for word in "Backend developer with three years of full time experience building Java and Spring Boot services for a large bank.".split(" "):
        summary.add_run(word)
        summary.add_run(" ")
    doc.add_paragraph("SKILLS", style="Heading 2")
    _labelled_skills(doc, "Languages", "Java, Python, SQL", "Normal")
    _labelled_skills(doc, "Frameworks", "Spring Boot, REST APIs, Kafka", "Body Text")
    _labelled_skills(doc, "Tools", "PostgreSQL, Docker, Git", "Normal")
    doc.add_paragraph("PROFESSIONAL EXPERIENCE", style="Heading 2")
    role = doc.add_paragraph()
    role.add_run("Software Developer").bold = True
    role.add_run("\tMay 2021 – March 2025")
    doc.add_paragraph("Big Bank, Toronto, Ontario")
    for text in (
        "Built REST APIs in Java and Spring Boot serving 2 million daily requests.",
        "Reduced batch processing time by 40% by tuning PostgreSQL queries.",
        "Led a hackathon health monitoring project that expanded into a working group.",
        "Automated deployments with Docker and GitHub Actions.",
    ):
        bp = doc.add_paragraph(style="List Bullet")
        for word in text.split(" "):
            bp.add_run(word)
            bp.add_run(" ")
    role = doc.add_paragraph()
    role.add_run("Software Developer Intern").bold = True
    role.add_run("\tMay 2022 – August 2022")
    doc.add_paragraph("Startup Co, Toronto, Ontario")
    doc.add_paragraph("Wrote Python scripts to process Kafka events for analytics.", style="List Bullet")
    doc.add_paragraph("PROJECTS", style="Heading 2")
    doc.add_paragraph("Instrument CLI | 2026", style="Heading 3")
    doc.add_paragraph("Command line tool in Python for AI assisted development workflows.", style="List Bullet")
    doc.add_section(WD_SECTION.NEW_PAGE)
    doc.add_paragraph("EDUCATION", style="Heading 2")
    doc.add_paragraph("Honours Associates, Computer Programming, Example College", style="Heading 3")
    doc.add_paragraph("CERTIFICATIONS", style="Heading 2")
    doc.add_paragraph("AWS Certified Cloud Practitioner", style="Body Text")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
