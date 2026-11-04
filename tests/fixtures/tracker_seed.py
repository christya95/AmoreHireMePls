"""Builds a synthetic tracker clone (same schema as job-application-records) in a temp dir."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECORDS_PY = HERE / "records_py_snapshot.py"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def seed_records() -> list[dict]:
    return [
        {
            "id": "li-1000000001", "employer": "Northwind Traders", "role": "Java Developer", "linkedin_job_id": "1000000001",
            "requisition_id": "", "posting_url": "https://www.linkedin.com/jobs/view/1000000001/", "status": "submitted",
            "application_date": "2026-10-13", "date_basis": "inferred from dated batch folder; exact submission timestamp unavailable",
            "confirmation": "Application submitted", "evidence_basis": "contemporaneous_log_not_rechecked",
            "source_log": "JobApplications_2026-10-13/application-log.md", "notes": "Reconstructed from local log.",
            "artifacts": [{"kind": "resume_submitted", "path": "artifacts/li-1000000001/resume.pdf", "sha256": "", "original_filename": "resume.pdf"}],
            "resume_match_basis": "filename explicitly named in source log",
        },
        {
            "id": "northwind-REQ-77", "employer": "Northwind Traders", "role": "Java Developer", "linkedin_job_id": "1000000001",
            "requisition_id": "REQ-77", "posting_url": "https://careers.northwind.example/jobs/REQ-77", "status": "submitted",
            "application_date": "2026-10-15", "date_basis": "employer receipt observed in America/Toronto", "confirmation": "Thank you for applying",
            "evidence_basis": "receipt_screenshot_reviewed", "source_log": "JobApplications_2026-10-15/application-log.md",
            "notes": "Same LinkedIn job as li-1000000001 (duplicate identity).", "artifacts": [],
            "resume_match_basis": "exact file selected and verified at submission", "resume_unavailable_reason": "file not archived",
        },
        {
            "id": "contoso-5551", "employer": "Contoso", "role": "Backend Developer", "linkedin_job_id": "", "requisition_id": "5551",
            "posting_url": "https://contoso.example/careers/5551", "status": "blocked", "application_date": "", "date_basis": "not submitted",
            "confirmation": "", "source_log": "JobApplications_2026-10-15/application-log.md", "notes": "Workday asked for a login.", "artifacts": [],
        },
        {
            "id": "fabrikam-9", "employer": "Fabrikam", "role": "Software Engineer", "linkedin_job_id": "", "requisition_id": "9",
            "posting_url": "https://jobs.lever.co/fabrikam/9", "status": "submitted", "application_date": "2026-10-14",
            "date_basis": "explicit date in contemporaneous log", "confirmation": "Application submitted!", "evidence_basis": "contemporaneous_log_not_rechecked",
            "source_log": "JobApplications_2026-10-13_Batch2/application-log.md", "notes": "",
            "artifacts": [{"kind": "resume_submitted", "path": "artifacts/fabrikam-9/missing.pdf", "sha256": "deadbeef", "original_filename": "missing.pdf"}],
            "resume_match_basis": "inferred from unique employer/job-ID filename",
        },
    ]


def build_tracker_clone(root: Path, with_git: bool = True, include_missing_artifact: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "templates").mkdir(exist_ok=True)
    records = seed_records()
    if not include_missing_artifact:
        records[3]["artifacts"] = []
        records[3]["resume_unavailable_reason"] = "fixture"
    # real artifact for the first record
    art = root / "artifacts/li-1000000001/resume.pdf"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_bytes(b"%PDF-1.4 fixture resume\n")
    records[0]["artifacts"][0]["sha256"] = _sha(art.read_bytes())
    (root / "data/applications.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    (root / "data/sources.json").write_text("[]\n", encoding="utf-8")
    shutil.copy(RECORDS_PY, root / "scripts/records.py")
    (root / "README.md").write_text("# fixture tracker\n", encoding="utf-8")
    (root / "docs/recording-guide.md").write_text("fixture\n", encoding="utf-8")
    (root / "templates/application.json").write_text("{}\n", encoding="utf-8")
    if not include_missing_artifact:
        subprocess.run([sys.executable, str(root / "scripts/records.py"), "render"], check=True, capture_output=True, cwd=root)
    if with_git:
        env = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "f@example.com", "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "f@example.com"}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=f@example.com", "commit", "-q", "-m", "seed"], cwd=root, check=True, env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    return root


def build_bare_remote(bare: Path, clone: Path) -> Path:
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)
    return bare
