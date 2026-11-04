from datetime import datetime

import pytest
from pymongo import MongoClient

from amorehiremepls.backup import backup, restore
from amorehiremepls.config import Settings
from amorehiremepls.db import Store
from amorehiremepls.runtime import Runtime
from amorehiremepls.util import TORONTO
from amorehiremepls.worker import Worker, worker_healthy
from tests.conftest import TEST_MONGO_URI

pytestmark = pytest.mark.mongo


def test_backup_and_restore_round_trip_with_artifacts(store, tmp_path):
    store.c("jobs").insert_one({"_id": "job_1", "employer": "Acme", "dedup_keys": ["req:acme:1"], "created_at": datetime(2026, 11, 4, tzinfo=TORONTO)})
    store.c("profile").insert_one({"key": "candidate", "version": 2})
    art = tmp_path / "artifacts" / "jobs" / "job_1" / "resume.docx"
    art.parent.mkdir(parents=True)
    art.write_bytes(b"docx-bytes")
    priv = tmp_path / "private" / "sessions" / "site.storage.json"
    priv.parent.mkdir(parents=True)
    priv.write_text("{}")
    manifest = backup(store, tmp_path / "artifacts", tmp_path / "private", tmp_path / "backup.tgz")
    assert manifest["collections"]["jobs"] == 1 and manifest["artifacts"] == 1 and manifest["private"] == 1 and len(manifest["sha256"]) == 64
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    target = Store(client, store.db.name + "_restored")
    try:
        with pytest.raises(RuntimeError):
            target.c("x").insert_one({"a": 1})
            restore(target, tmp_path / "backup.tgz", tmp_path / "r_art", tmp_path / "r_priv")
        res = restore(target, tmp_path / "backup.tgz", tmp_path / "r_art", tmp_path / "r_priv", force=True)
        assert res["collections"]["jobs"] == 1 and res["artifacts"] == 1
        doc = target.c("jobs").find_one({"_id": "job_1"})
        assert doc["employer"] == "Acme" and doc["created_at"].year == 2026
        assert (tmp_path / "r_art" / "jobs" / "job_1" / "resume.docx").read_bytes() == b"docx-bytes"
        assert any(v.get("unique") for k, v in target.c("jobs").index_information().items() if "dedup_keys" in k)
    finally:
        client.drop_database(target.db.name)
        client.close()


def _runtime(store, tmp_path, **over):
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        browser_session_dir=tmp_path / "s",
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
        **over,
    )
    return Runtime(s, store=store)


def test_schedule_slots_claimed_once_and_heartbeat(store, tmp_path):
    rt = _runtime(store, tmp_path, discovery_schedule="09:00,18:30")
    w1, w2 = Worker(rt), Worker(rt)
    now = datetime(2026, 11, 4, 9, 5, tzinfo=TORONTO)
    assert w1.due_slots(now) == ["discovery:2026-11-04T09:00"]
    assert w1.due_slots(datetime(2026, 11, 4, 8, 0, tzinfo=TORONTO)) == []
    assert w1.due_slots(datetime(2026, 11, 4, 19, 0, tzinfo=TORONTO)) == ["discovery:2026-11-04T18:30"]
    assert store.claim_schedule_slot("discovery:2026-11-04T09:00", w1.name)
    assert not store.claim_schedule_slot("discovery:2026-11-04T09:00", w2.name)
    assert not worker_healthy(rt)
    w1.heartbeat()
    assert worker_healthy(rt)
    assert w1.run_discovery()["ingested"] == 0  # no sources configured, no network


def test_worker_tick_starts_queued_run_once_and_respects_emergency_stop(store, tmp_path):
    rt = _runtime(store, tmp_path)
    store.c("jobs").insert_one(
        {
            "_id": "job_q",
            "employer": "Acme",
            "employer_norm": "acme",
            "title": "Backend Developer",
            "title_norm": "backend developer",
            "dedup_keys": ["req:acme:q"],
            "status": "qualified",
            "latest_snapshot": {"text": "", "posting_hash": "x"},
        }
    )
    run = store.create_run("application", {"job_id": "job_q", "form_fields": []})
    store.set_emergency_stop(True, "test")
    w = Worker(rt)
    assert w.tick()["runs"] == 0 and store.c("runs").find_one({"_id": run["_id"]})["status"] == "queued"
    store.set_emergency_stop(False)
    assert w.tick()["runs"] == 1
    r = store.c("runs").find_one({"_id": run["_id"]})
    assert r["status"] == "waiting" and r["interrupt"]["type"] == "needs_input"  # no base resume -> asks instead of using an old one
    assert "approved base resume not selected" in r["interrupt"]["outstanding"]
    assert w.tick()["runs"] == 0  # not started twice
