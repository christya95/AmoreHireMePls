"""Synthetic base resume DOCX for tests. Fictional person; no real personal data."""

from pathlib import Path

from docx import Document


def make_base_resume(path: Path) -> Path:
    doc = Document()
    doc.add_paragraph("Alex Example")
    doc.add_paragraph("Milton, Ontario | alex@example.com | 555 000 0000 | linkedin.com/in/alexexample")
    doc.add_paragraph("Summary")
    doc.add_paragraph("Backend developer with three years of full time experience building Java and Spring Boot services for a large bank.")
    doc.add_paragraph("Technical Skills")
    doc.add_paragraph("Java, Spring Boot, Python, REST APIs, PostgreSQL, Docker, Kafka, Git")
    doc.add_paragraph("Professional Experience")
    doc.add_paragraph("Software Developer at Big Bank | July 2023 – October 2026")
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
