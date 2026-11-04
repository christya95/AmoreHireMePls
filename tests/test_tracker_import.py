import json

import pytest

from amorehiremepls.jobs import JobStore, dedup_keys
from amorehiremepls.tracker import TrackerClone, TrackerImporter
from tests.fixtures.tracker_seed import build_tracker_clone

pytestmark = pytest.mark.mongo


def test_dedup_keys_cover_linkedin_requisition_and_url():
    keys = dedup_keys("Northwind Traders Inc.", "https://careers.northwind.example/jobs/REQ-77?utm_source=x", "REQ-77", "1000000001")
    assert "li:1000000001" in keys
    assert "req:northwind traders:req-77" in keys
    assert "url:https://careers.northwind.example/jobs/REQ-77" in keys


def test_import_twice_preserves_counts_provenance_missing_artifacts_and_duplicates(store, tmp_path):
    clone = TrackerClone(build_tracker_clone(tmp_path / "tracker"))
    importer = TrackerImporter(store, clone)

    preview = importer.preview()
    assert preview.total == 4 and preview.new == 4
    assert preview.by_status == {"submitted": 3, "blocked": 1}
    assert [d["record_ids"] for d in preview.duplicates] == [["li-1000000001", "northwind-REQ-77"]]
    assert len(preview.missing_artifacts) == 1 and preview.missing_artifacts[0]["record_id"] == "fabrikam-9"
    assert preview.source_commit != "unknown" and len(preview.source_sha256) == 64

    first = importer.import_all()
    assert first["counts"]["inserted"] == 4 and first["counts"]["jobs_created"] == 3  # duplicate pair -> one job

    second = importer.import_all()
    assert second["counts"] == {"inserted": 0, "updated": 0, "unchanged": 4, "conflicts": 0, "jobs_created": 0}
    assert store.c("applications").count_documents({}) == 4
    assert store.c("jobs").count_documents({}) == 3
    app = store.c("applications").find_one({"tracker_record_id": "li-1000000001"})
    assert app["provenance"]["origin"] == "tracker_import"
    assert app["evidence_basis"] == "contemporaneous_log_not_rechecked"  # never relabelled as ATS-verified
    assert app["artifact_status"][0]["hash_ok"] is True
    bad = store.c("applications").find_one({"tracker_record_id": "fabrikam-9"})
    assert bad["artifact_status"][0]["exists"] is False
    assert store.c("imports").count_documents({}) == 2


def test_reimport_detects_conflict_with_local_edits_and_updates_clean_changes(store, tmp_path):
    root = build_tracker_clone(tmp_path / "tracker")
    importer = TrackerImporter(store, TrackerClone(root))
    importer.import_all()
    # Local edit on one record; tracker edit on two records.
    store.c("applications").update_one({"tracker_record_id": "contoso-5551"}, {"$set": {"local_changes": {"notes": "local note"}}})
    data = json.loads((root / "data/applications.json").read_text())
    for r in data:
        if r["id"] in ("contoso-5551", "fabrikam-9"):
            r["notes"] = "tracker changed"
    (root / "data/applications.json").write_text(json.dumps(data, indent=2) + "\n")
    pv = importer.preview()
    assert pv.changed == 2 and len(pv.conflicts) == 1 and pv.conflicts[0]["record_id"] == "contoso-5551"
    res = importer.import_all()
    assert res["counts"]["updated"] == 1 and res["counts"]["conflicts"] == 1
    c = store.c("applications").find_one({"tracker_record_id": "contoso-5551"})
    assert c["sync"]["state"] == "conflict" and c["local_changes"]["notes"] == "local note"
    assert c["conflict"]["tracker_record"]["notes"] == "tracker changed"


def test_job_merge_by_changed_url_and_city(store):
    jobs = JobStore(store)
    a, created = jobs.upsert(employer="Acme", title="Backend Developer", posting_url="https://acme.wd3.myworkdayjobs.com/en-US/Acme/job/Toronto/Backend_R123?source=li",
                             requisition_id="R123", location="Toronto")
    b, created2 = jobs.upsert(employer="ACME Inc", title="Backend Developer", posting_url="https://acme.wd3.myworkdayjobs.com/en-US/Acme/job/Vancouver/Backend_R123",
                              requisition_id="R123", location="Vancouver")
    assert created and not created2 and a["_id"] == b["_id"]
    assert store.c("jobs").count_documents({}) == 1
