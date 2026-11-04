"""MongoDB access: collections, unique indexes, atomic reservations, audit, emergency stop.

Works against a standalone mongod (the NAS runs mongo:8.0 without a replica set), so nothing here
relies on multi-document transactions or change streams. Atomicity comes from single-document
`find_one_and_update` calls with state guards.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from bson.codec_options import CodecOptions
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from .config import Settings
from .util import new_id, utcnow

log = logging.getLogger(__name__)

CHECKPOINT_COLLECTION = "lg_checkpoints"
CHECKPOINT_WRITES_COLLECTION = "lg_checkpoint_writes"

# collection -> list of (keys, options)
INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict[str, Any]]]] = {
    "profile": [([("key", ASCENDING)], {"unique": True})],
    "jobs": [
        ([("dedup_keys", ASCENDING)], {"unique": True, "sparse": True}),
        ([("status", ASCENDING), ("updated_at", ASCENDING)], {}),
        ([("employer_norm", ASCENDING), ("title_norm", ASCENDING)], {}),
    ],
    "applications": [
        ([("job_id", ASCENDING)], {}),
        ([("status", ASCENDING)], {}),
        ([("tracker_record_id", ASCENDING)], {"unique": True, "sparse": True}),
    ],
    "actions": [
        ([("idempotency_key", ASCENDING)], {"unique": True}),
        ([("status", ASCENDING), ("created_at", ASCENDING)], {}),
        ([("target_id", ASCENDING), ("kind", ASCENDING)], {}),
    ],
    "authorizations": [([("status", ASCENDING), ("expires_at", ASCENDING)], {})],
    "recipients": [
        ([("aliases", ASCENDING)], {"unique": True, "sparse": True}),
        ([("employer_norm", ASCENDING)], {}),
    ],
    "outreach": [
        ([("recipient_id", ASCENDING), ("created_at", ASCENDING)], {}),
        ([("status", ASCENDING)], {}),
    ],
    "sync_outbox": [
        ([("application_id", ASCENDING), ("kind", ASCENDING)], {"unique": True}),
        ([("status", ASCENDING), ("next_attempt_at", ASCENDING)], {}),
    ],
    "runs": [([("status", ASCENDING), ("created_at", ASCENDING)], {}), ([("thread_id", ASCENDING)], {"unique": True})],
    "audit": [([("created_at", ASCENDING)], {}), ([("subject_id", ASCENDING)], {})],
    "usage": [([("run_id", ASCENDING)], {}), ([("created_at", ASCENDING)], {})],
    "cache": [([("cache_key", ASCENDING)], {"unique": True}), ([("created_at", ASCENDING)], {"expireAfterSeconds": 60 * 60 * 24 * 30})],
    "artifacts": [([("sha256", ASCENDING)], {}), ([("owner_id", ASCENDING)], {})],
    "controls": [([("key", ASCENDING)], {"unique": True})],
    "scheduler_slots": [([("slot_key", ASCENDING)], {"unique": True}), ([("created_at", ASCENDING)], {"expireAfterSeconds": 60 * 60 * 24 * 7})],
    "imports": [([("source_commit", ASCENDING), ("source_path", ASCENDING)], {})],
}


class Store:
    def __init__(self, client: MongoClient, db_name: str) -> None:
        self.client = client
        # Always read datetimes back as UTC-aware so comparisons with utcnow() are safe.
        self.db: Database = client.get_database(db_name, codec_options=CodecOptions(tz_aware=True, tzinfo=UTC))

    @classmethod
    def from_settings(cls, settings: Settings) -> Store:
        client: MongoClient = MongoClient(settings.mongodb_uri, **settings.mongo_client_kwargs())
        return cls(client, settings.mongodb_database)

    # -- lifecycle ---------------------------------------------------------
    def ping(self) -> dict[str, Any]:
        info = self.db.command("ping")
        build = self.client.server_info()
        return {"ok": info.get("ok") == 1, "version": build.get("version"), "database": self.db.name}

    def ensure_indexes(self) -> dict[str, list[str]]:
        created: dict[str, list[str]] = {}
        for name, specs in INDEXES.items():
            coll = self.db[name]
            for keys, options in specs:
                created.setdefault(name, []).append(coll.create_index(keys, **options))
        return created

    def c(self, name: str) -> Collection:
        return self.db[name]

    # -- audit -------------------------------------------------------------
    def audit(self, event: str, subject_id: str | None = None, **data: Any) -> None:
        self.db["audit"].insert_one({"event": event, "subject_id": subject_id, "data": data, "created_at": utcnow()})

    # -- controls ----------------------------------------------------------
    def set_control(self, key: str, value: Any) -> None:
        self.db["controls"].update_one({"key": key}, {"$set": {"value": value, "updated_at": utcnow()}}, upsert=True)

    def get_control(self, key: str, default: Any = None) -> Any:
        doc = self.db["controls"].find_one({"key": key})
        return default if doc is None else doc.get("value", default)

    def emergency_stop_active(self) -> bool:
        return bool(self.get_control("emergency_stop", False))

    def set_emergency_stop(self, active: bool, reason: str = "") -> None:
        self.set_control("emergency_stop", active)
        self.audit("emergency_stop", None, active=active, reason=reason)

    # -- atomic reservations ------------------------------------------------
    def reserve(self, collection: str, filter_: dict, owner: str, from_states: Iterable[str], to_state: str,
                lease_seconds: int = 900, extra: dict | None = None) -> dict | None:
        """Move one document from one of `from_states` to `to_state` for `owner`, atomically.

        Returns the updated document, or None when another worker already holds it. Expired leases
        (reserved_until in the past) can be taken over so a crashed worker does not block forever.
        """
        now = utcnow()
        query = {
            **filter_,
            "$or": [
                {"status": {"$in": list(from_states)}},
                {"status": to_state, "reserved_until": {"$lt": now}},
            ],
        }
        update = {"$set": {"status": to_state, "reserved_by": owner, "reserved_at": now,
                           "reserved_until": now + timedelta(seconds=lease_seconds), "updated_at": now, **(extra or {})}}
        return self.db[collection].find_one_and_update(query, update, return_document=ReturnDocument.AFTER)

    def release(self, collection: str, filter_: dict, owner: str, to_state: str, extra: dict | None = None) -> dict | None:
        now = utcnow()
        return self.db[collection].find_one_and_update(
            {**filter_, "reserved_by": owner},
            {"$set": {"status": to_state, "updated_at": now, **(extra or {})}, "$unset": {"reserved_by": "", "reserved_until": ""}},
            return_document=ReturnDocument.AFTER,
        )

    def insert_unique(self, collection: str, doc: dict) -> bool:
        """Insert; return False if a unique index rejected it."""
        try:
            self.db[collection].insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    # -- scheduler ---------------------------------------------------------
    def claim_schedule_slot(self, slot_key: str, owner: str) -> bool:
        return self.insert_unique("scheduler_slots", {"slot_key": slot_key, "owner": owner, "created_at": utcnow()})

    # -- runs --------------------------------------------------------------
    def create_run(self, kind: str, params: dict, thread_id: str | None = None) -> dict:
        run = {
            "_id": new_id("run"), "kind": kind, "params": params, "status": "queued",
            "thread_id": thread_id or new_id("thread"), "created_at": utcnow(), "updated_at": utcnow(),
            "events": [], "budget": {},
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0,
                      "tool_steps": 0, "cost_usd": None, "cost_known": True},
        }
        self.db["runs"].insert_one(run)
        return run

    def update_run(self, run_id: str, **fields: Any) -> None:
        self.db["runs"].update_one({"_id": run_id}, {"$set": {**fields, "updated_at": utcnow()}})

    def run_event(self, run_id: str, message: str, **data: Any) -> None:
        self.db["runs"].update_one({"_id": run_id}, {"$push": {"events": {"at": utcnow(), "message": message, **data}}})

    def run_requested_state(self, run_id: str) -> str | None:
        doc = self.db["runs"].find_one({"_id": run_id}, {"requested": 1})
        return (doc or {}).get("requested")


def utc_floor(dt: datetime, minutes: int) -> datetime:
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // minutes) * minutes)
