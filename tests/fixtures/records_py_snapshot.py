"""Maintain application records and generate portable, lawyer-readable indexes."""

import argparse
import csv
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/applications.json"


def load():
    return json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else []


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(items):
    seen = set()
    for item in items:
        for key in ("id", "employer", "role", "status", "posting_url"):
            if not item.get(key):
                raise ValueError("Missing " + key)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,160}", item["id"]):
            raise ValueError("Unsafe record ID")
        if item["id"] in seen:
            raise ValueError("Duplicate record " + item["id"])
        seen.add(item["id"])
        if item["status"] not in ("submitted", "prepared", "blocked", "researched", "skipped", "withdrawn"):
            raise ValueError("Unknown status")
        if item["status"] == "submitted" and not item.get("confirmation"):
            raise ValueError("Submitted record requires confirmation evidence")
        for artifact in item.get("artifacts", []):
            p = (ROOT / artifact["path"]).resolve()
            if not p.is_relative_to(ROOT.resolve()):
                raise ValueError("Artifact outside repository")
            if not p.is_file() or digest(p) != artifact["sha256"]:
                raise ValueError("Missing or changed artifact " + str(p))
    return items


def render(items):
    validate(items)
    submitted = sorted((i for i in items if i["status"] == "submitted"), key=lambda i: (i.get("application_date") or "", i["employer"], i["id"]))
    pending = [i for i in items if i["status"] != "submitted"]
    headers = [
        "id",
        "application_date",
        "date_basis",
        "employer",
        "role",
        "requisition_id",
        "linkedin_job_id",
        "status",
        "posting_url",
        "resume_path",
        "resume_match_basis",
        "confirmation",
        "source_log",
        "notes",
    ]
    with (ROOT / "applications.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for i in submitted + pending:
            row = {key: i.get(key, "") for key in headers}
            row["resume_path"] = next((a["path"] for a in i.get("artifacts", []) if a["kind"] == "resume_submitted" or a["kind"] == "resume_prepared"), "")
            # Prevent spreadsheet formula execution when opening exported CSVs.
            for k, v in row.items():
                if isinstance(v, str) and v.startswith(("=", "+", "-", "@")):
                    row[k] = "'" + v
            w.writerow(row)
    lines = [
        "# Application record",
        "",
        f"{len(submitted)} recorded submissions; {len(pending)} other tracked opportunities.",
        "",
        "Historical submissions were reconstructed from contemporaneous local logs. Date basis and evidence strength are retained per record. A screenshot stored here has not necessarily been independently reviewed. The current files were captured when this repository was created; their hashes identify these captured copies, not an employer-held attachment.",
        "",
        "See [CSV](applications.csv) for filtering, [JSON](data/applications.json) for complete provenance, and [recording guide](docs/recording-guide.md) for updates.",
        "",
        "## Recorded submissions",
        "",
        "| Date | Employer / role | IDs | Resume | Evidence |",
        "|---|---|---|---|---|",
    ]

    def safe(s):
        return str(s).replace("|", "/").replace("\n", " ")

    for i in submitted:
        resume = next((a for a in i.get("artifacts", []) if a["kind"] == "resume_submitted"), None)
        res = f"[PDF]({resume['path']})" if resume else "Not recovered"
        ids = i.get("requisition_id") or i.get("linkedin_job_id", "")
        lines.append(
            f"| {safe(i.get('application_date', 'Unknown'))} | [{safe(i['employer'])}: {safe(i['role'])}]({i['posting_url']}) | {safe(ids)} | {res} | [Record](records/{i['id']}.md) |"
        )
    lines.extend(["", "## Other opportunities (not counted as submitted)", "", "| Employer / role | Status | Record |", "|---|---|---|"])
    for i in pending:
        lines.append(f"| {safe(i['employer'])}: {safe(i['role'])} | {i['status']} | [Details](records/{i['id']}.md) |")
    (ROOT / "APPLICATIONS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for i in items:
        body = [
            f"# {i['employer']} — {i['role']}",
            "",
            f"- Status: {i['status']}",
            f"- Application date: {i.get('application_date') or 'Unknown'}",
            f"- Date basis: {i.get('date_basis', 'unknown')}",
            f"- Requisition: {i.get('requisition_id') or 'Not recorded'}",
            f"- LinkedIn job ID: {i.get('linkedin_job_id') or 'Not recorded'}",
            f"- Posting: {i['posting_url']}",
            f"- Confirmation: {i.get('confirmation') or 'No submission confirmed'}",
            f"- Evidence basis: {i.get('evidence_basis', 'not submitted')}",
            f"- Resume mapping: {i.get('resume_match_basis', 'not recovered')}",
            f"- Source: {i.get('source_log', 'New record')}",
            "",
            i.get("notes", ""),
            "",
            "## Preserved artifacts",
            "",
        ]
        for a in i.get("artifacts", []):
            body.extend([f"- [{a['kind']}: {Path(a['path']).name}](../{a['path']})", f"  SHA-256: `{a['sha256']}`"])
        (ROOT / "records").mkdir(exist_ok=True)
        (ROOT / "records" / f"{i['id']}.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"Validated and rendered {len(submitted)} submissions and {len(pending)} other records.")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("render")
    sub.add_parser("validate")
    add = sub.add_parser("add")
    add.add_argument("record_json")
    add.add_argument("--resume", type=Path)
    add.add_argument("--receipt", type=Path)
    export = sub.add_parser("export")
    export.add_argument("output", type=Path)
    a = p.parse_args()
    items = load()
    if a.command == "validate":
        validate(items)
        print("Record and artifact validation passed.")
        return
    if a.command == "add":
        item = json.loads(Path(a.record_json).read_text(encoding="utf-8-sig"))
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,160}", item["id"]):
            p.error("Unsafe record ID")
        # One record per employer requisition. Same-day updates must preserve artifact history.
        old = next((i for i in items if i["id"] == item["id"]), None)
        if old:
            item["artifacts"] = old.get("artifacts", []) + [x for x in item.get("artifacts", []) if x not in old.get("artifacts", [])]
            item["history"] = old.get("history", []) + [{k: v for k, v in old.items() if k != "history"}]
        for kind, source in [("resume_submitted" if item["status"] == "submitted" else "resume_prepared", a.resume), ("receipt", a.receipt)]:
            if source:
                sha = digest(source)
                target = ROOT / "artifacts" / item["id"] / (sha[:12] + "-" + source.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                artifact = {"kind": kind, "path": target.relative_to(ROOT).as_posix(), "sha256": sha, "original_filename": source.name}
                if artifact not in item.setdefault("artifacts", []):
                    item["artifacts"].append(artifact)
        if item["status"] == "submitted" and not any(x["kind"] == "resume_submitted" for x in item.get("artifacts", [])):
            if not item.get("resume_unavailable_reason"):
                p.error("Provide --resume or an explicit resume_unavailable_reason")
        if item.get("requisition_id"):
            for i in items:
                if i["id"] != item["id"] and i["employer"].casefold() == item["employer"].casefold() and i.get("requisition_id") == item["requisition_id"]:
                    p.error("Employer/requisition already recorded as " + i["id"])
        items = [i for i in items if i["id"] != item["id"]] + [item]
        validate(items)
        DATA.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")
        render(items)
    elif a.command == "render":
        render(items)
    elif a.command == "export":
        render(items)
        if a.output.resolve().is_relative_to(ROOT.resolve()):
            p.error("Export outside the repository to avoid committing the bundle")
        with zipfile.ZipFile(a.output, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ("README.md", "APPLICATIONS.md", "applications.csv", "docs/recording-guide.md", "data/applications.json", "data/sources.json"):
                z.write(ROOT / name, name)
            for i in items:
                z.write(ROOT / "records" / f"{i['id']}.md", f"records/{i['id']}.md")
                for artifact in i.get("artifacts", []):
                    z.write(ROOT / artifact["path"], artifact["path"])
        print("Exported", a.output)


if __name__ == "__main__":
    main()
