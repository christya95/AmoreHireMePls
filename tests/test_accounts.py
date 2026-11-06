"""Per-person accounts. Values here are fictional. Tracker sync keeps running after access ends."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.accounts import (
    AccountError,
    access_summary,
    account_is_active,
    active_user_ids,
    create_member,
    delete_user,
    hash_password,
    migrate_users,
    set_user_status,
    user_scope,
    verify_password,
)
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import utcnow
from amorehiremepls.core.worker import Worker
from amorehiremepls.records.sync import SyncCoordinator
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI


def test_passwords_are_argon2id_and_access_badges_skip_admins():
    stored = hash_password("correct-horse-battery")
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, "correct-horse-battery")
    assert not verify_password(stored, "wrong-horse-battery")
    now = utcnow()
    assert access_summary({"role": "admin", "status": "active"}, now)["badge"] is None
    member = {"role": "member", "status": "active", "access_until": now + timedelta(days=10, hours=2)}
    summary = access_summary(member, now)
    assert summary["badge"] == "10 days of access left" and not summary["expired"]
    ended = {"role": "member", "status": "active", "access_until": now - timedelta(days=1)}
    assert access_summary(ended, now)["badge"] == "Access ended" and access_summary(ended, now)["expired"]
    assert not account_is_active(ended, now)
    assert account_is_active({"role": "admin", "status": "active", "access_until": None}, now)


@pytest.fixture
def client(store, tmp_path):
    settings = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(settings, store=store)
    return TestClient(create_app(rt), follow_redirects=False)


@pytest.mark.mongo
def test_migrate_stamps_records_and_a_second_run_does_not_add_another_admin(store):
    store.db["jobs"].insert_one({"_id": "job_old", "employer": "Acme", "title": "Role"})
    first = migrate_users(store, "correct-horse-battery", "Alex")
    assert first["admin_created"] is True and first["stamped_documents"] >= 1
    admin = store.db["users"].find_one({"role": "admin"})
    assert admin["access_until"] is None and admin["status"] == "active"
    assert admin["password_hash"].startswith("$argon2id$")
    assert store.db["jobs"].find_one({"_id": "job_old"})["user_id"] == admin["_id"]
    second = migrate_users(store, "correct-horse-battery", "Alex")
    assert second["admin_created"] is False
    assert store.db["users"].count_documents({"role": "admin", "status": "active"}) == 1


@pytest.mark.mongo
def test_scope_hides_another_persons_jobs(store):
    migrate_users(store, "correct-horse-battery", "Alex")
    member = create_member(store, "casey", "another-good-password")
    with user_scope(member):
        store.c("jobs").insert_one({"_id": "job_member", "employer": "Northwind", "title": "Role"})
    with user_scope(store.db["users"].find_one({"role": "admin"})["_id"]):
        assert store.c("jobs").find_one({"_id": "job_member"}) is None
    assert store.db["jobs"].find_one({"_id": "job_member"})["user_id"] == member


@pytest.mark.mongo
def test_expired_member_can_sign_in_only_to_the_ended_page_and_sync_still_runs(client, store, tmp_path):
    migrate_users(store, "correct-horse-battery", "Alex")
    member = create_member(store, "casey", "another-good-password")
    store.db["users"].update_one({"_id": member}, {"$set": {"access_until": utcnow() - timedelta(days=2)}})
    signed = client.post("/login", data={"password": "another-good-password", "login_name": "casey"})
    assert signed.status_code == 303 and signed.headers["location"] == "/access-ended"
    assert "GitHub tracker continues" in client.get("/access-ended").text
    assert client.get("/jobs").status_code == 303 and client.get("/jobs").headers["location"] == "/access-ended"
    assert client.get("/usage").status_code == 303
    job_id, app_id = "job_sync", "app_sync"
    store.db["jobs"].insert_one({"_id": job_id, "user_id": member, "employer": "Acme", "title": "Role"})
    store.db["applications"].insert_one({"_id": app_id, "job_id": job_id, "user_id": member, "status": "submitted"})
    store.db["sync_outbox"].insert_one(
        {
            "_id": "out_sync",
            "application_id": app_id,
            "kind": "record_submission",
            "status": "sync_pending",
            "attempts": 0,
            "next_attempt_at": utcnow(),
            "created_at": utcnow(),
            "user_id": member,
        }
    )
    sync = SyncCoordinator(store, tmp_path / "no-clone")
    with user_scope("user_someone_else"):
        sync.process_pending(owner="test")
    item = store.db["sync_outbox"].find_one({"_id": "out_sync"})
    assert item["status"] != "skipped"
    assert "clone" in (item.get("error") or "").lower()


@pytest.mark.mongo
def test_worker_skips_expired_and_off_people_but_still_runs_sync(client, store):
    migrate_users(store, "correct-horse-battery", "Alex")
    admin = store.db["users"].find_one({"role": "admin"})["_id"]
    expired = create_member(store, "expired", "another-good-password")
    off = create_member(store, "paused", "another-good-password")
    store.db["users"].update_one({"_id": expired}, {"$set": {"access_until": utcnow() - timedelta(days=1)}})
    set_user_status(store, off, "off")
    now = utcnow()

    def queued(run_id: str, user_id: str) -> dict:
        return {"_id": run_id, "kind": "application", "status": "queued", "thread_id": run_id, "user_id": user_id, "params": {"job_id": "job_x"}, "created_at": now}

    store.db["runs"].insert_many([queued("run_admin", admin), queued("run_expired", expired), queued("run_off", off)])
    worker = Worker(client.app.state.rt)
    started: list[str] = []
    worker._start = lambda run: started.append(run["_id"])
    worker.tick()
    assert started == ["run_admin"]
    assert store.db["runs"].find_one({"_id": "run_expired"})["status"] == "queued"
    assert store.db["runs"].find_one({"_id": "run_off"})["status"] == "queued"
    assert active_user_ids(store) == [admin]


@pytest.mark.mongo
def test_worker_claims_a_queued_application_that_has_no_owner(client, store):
    migrate_users(store, "correct-horse-battery", "Alex")
    admin = store.db["users"].find_one({"role": "admin"})["_id"]
    now = utcnow()
    store.db["jobs"].insert_one({"_id": "job_x", "employer": "Acme", "title": "Role"})
    store.db["runs"].insert_one(
        {"_id": "run_loose", "kind": "application", "status": "queued", "thread_id": "thread_x", "params": {"job_id": "job_x"}, "created_at": now, "events": []}
    )
    worker = Worker(client.app.state.rt)
    started: list[str] = []
    worker._start = lambda run: started.append(run["_id"])
    worker.tick()
    assert started == ["run_loose"]
    assert store.db["runs"].find_one({"_id": "run_loose"})["user_id"] == admin
    assert store.db["jobs"].find_one({"_id": "job_x"})["user_id"] == admin


@pytest.mark.mongo
def test_unowned_run_page_says_the_worker_cannot_pick_it_up(client, store):
    migrate_users(store, "correct-horse-battery", "Alex")
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    now = utcnow()
    store.db["runs"].insert_one(
        {
            "_id": "run_loose",
            "kind": "application",
            "status": "queued",
            "thread_id": "thread_x",
            "params": {},
            "created_at": now,
            "events": [],
            "usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "tool_steps": 0, "cost_usd": 0, "cost_known": True},
        }
    )
    store.set_control("worker_heartbeat", {"at": now})
    page = client.get("/runs/run_loose")
    assert page.status_code == 200 and "This task has no owner" in page.text and "just checked in" in page.text


@pytest.mark.mongo
def test_people_page_extends_hides_cost_from_members_and_refuses_to_delete_the_last_admin(client, store, tmp_path):
    migrate_users(store, "correct-horse-battery", "Alex")
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/admin")
    assert page.status_code == 200 and "Add a person" in page.text and "Known cost" in page.text
    csrf = page.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    added = client.post("/admin/people", data={"csrf": csrf, "login": "casey", "password": "another-good-password", "display_name": "Casey"})
    assert added.status_code == 303 and "person_added" in added.headers["location"]
    member = store.db["users"].find_one({"login": "casey"})
    store.db["usage"].insert_one(
        {"user_id": member["_id"], "skill": "rank_fit@1", "model": "fake", "input_tokens": 3, "output_tokens": 1, "cost_usd": 0.25, "cost_known": True, "created_at": utcnow()}
    )
    detail = client.get(f"/admin/people/{member['_id']}")
    assert detail.status_code == 200 and "0.25000" in detail.text
    extended = client.post(f"/admin/people/{member['_id']}/extend", data={"csrf": csrf, "days": "10"})
    assert extended.status_code == 303
    until = store.db["users"].find_one({"_id": member["_id"]})["access_until"]
    assert until > utcnow() + timedelta(days=360)
    client.post("/logout", data={"csrf": csrf})
    client.post("/login", data={"login_name": "casey", "password": "another-good-password"})
    home = client.get("/")
    assert home.status_code == 200 and "AI cost this month" not in home.text and "AI use (all time)" not in home.text
    assert "days of access left" in home.text
    assert client.get("/usage").headers["location"] == "/"
    assert client.get("/admin").headers["location"] == "/"
    client.post("/logout", data={"csrf": home.text.split('name="csrf" value="', 1)[1].split('"', 1)[0]})
    client.post("/login", data={"password": "correct-horse-battery"})
    csrf = client.get("/admin").text.split('name="csrf" value="', 1)[1].split('"', 1)[0]
    admin = store.db["users"].find_one({"role": "admin"})
    with pytest.raises(AccountError):
        delete_user(store, admin["_id"], tmp_path / "backups")
    removed = client.post(f"/admin/people/{member['_id']}/delete", data={"csrf": csrf, "confirm": "delete"})
    assert removed.status_code == 303 and "person_deleted" in removed.headers["location"]
    tombstone = store.db["users"].find_one({"_id": member["_id"]})
    assert tombstone["status"] == "deleted" and tombstone["tombstone"] is True and "password_hash" not in tombstone
    assert store.db["usage"].count_documents({"user_id": member["_id"]}) == 0
