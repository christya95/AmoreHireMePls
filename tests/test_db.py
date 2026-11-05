import threading

import pytest

from amorehiremepls.core.util import extract_linkedin_job_id, normalize_employer, normalize_url, redact_free

pytestmark = pytest.mark.mongo


def test_indexes_and_ping(store):
    info = store.ping()
    assert info["ok"] and info["version"].startswith("8.")
    names = store.c("jobs").index_information()
    assert any(v.get("unique") for k, v in names.items() if "dedup_keys" in k)


def test_reservation_is_exclusive_across_threads(store):
    store.c("actions").insert_one({"_id": "a1", "status": "ready", "idempotency_key": "k1"})
    winners = []

    def worker(name):
        got = store.reserve("actions", {"_id": "a1"}, name, ["ready"], "reserved")
        if got:
            winners.append(name)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_schedule_slot_claimed_once(store):
    assert store.claim_schedule_slot("discovery:2026-11-04T09:00", "w1")
    assert not store.claim_schedule_slot("discovery:2026-11-04T09:00", "w2")


def test_emergency_stop_round_trip(store):
    assert not store.emergency_stop_active()
    store.set_emergency_stop(True, "test")
    assert store.emergency_stop_active()


def test_url_normalization_unit():
    a = normalize_url("https://www.linkedin.com/jobs/view/4467629125/?refId=abc&trackingId=x")
    b = normalize_url("http://linkedin.com/jobs/view/4467629125")
    assert a == b
    assert extract_linkedin_job_id("https://www.linkedin.com/jobs/view/java-dev-4467629125/?x=1") == "4467629125"
    assert normalize_employer("Electric Mind Inc.") == normalize_employer("electric mind")


def test_redaction_unit():
    assert "hunter2" not in redact_free("mongodb://user:hunter2@host/db password=hunter2 OPENAI_API_KEY=sk-abcdefghijklmnop")
