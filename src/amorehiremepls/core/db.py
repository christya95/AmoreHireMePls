"""MongoDB access: collections, unique indexes, atomic reservations, audit, emergency stop.

Works against a standalone mongod (the NAS runs mongo:8.0 without a replica set), so nothing here
relies on multi-document transactions or change streams. Atomicity comes from single-document
`find_one_and_update` calls with state guards.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from bson.codec_options import CodecOptions
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from amorehiremepls.core.config import Settings
from amorehiremepls.core.util import new_id, utcnow

log = logging.getLogger(__name__)

# Set for the current request or the worker's current person. Unset means the original single-operator behaviour.
current_user_id: ContextVar[str | None] = ContextVar("amore_user_id", default=None)

# Documents one person owns. Global collections (users, controls, cache, scheduler slots, checkpoints) stay unscoped.
OWNED_COLLECTIONS = frozenset(
    {
        "profile",
        "jobs",
        "applications",
        "actions",
        "authorizations",
        "recipients",
        "outreach",
        "sync_outbox",
        "runs",
        "audit",
        "usage",
        "artifacts",
        "imports",
        "notifications",
        "human_holds",
        "challenge_retries",
    }
)

CHECKPOINT_COLLECTION = "lg_checkpoints"
CHECKPOINT_WRITES_COLLECTION = "lg_checkpoint_writes"

# collection -> list of (keys, options)
INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict[str, Any]]]] = {
    "profile": [([("user_id", ASCENDING), ("key", ASCENDING)], {"unique": True})],
    "users": [
        ([("login", ASCENDING)], {"unique": True, "partialFilterExpression": {"status": {"$in": ["active", "off"]}}}),
        ([("status", ASCENDING), ("role", ASCENDING)], {}),
    ],
    "jobs": [
        ([("user_id", ASCENDING), ("dedup_keys", ASCENDING)], {"unique": True, "partialFilterExpression": {"dedup_keys": {"$exists": True}}}),
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
        ([("user_id", ASCENDING), ("aliases", ASCENDING)], {"unique": True, "partialFilterExpression": {"aliases": {"$exists": True}}}),
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
    "notifications": [([("idempotency_key", ASCENDING)], {"unique": True})],
    "human_holds": [([("application_id", ASCENDING), ("status", ASCENDING)], {}), ([("started_at", ASCENDING)], {})],
    "challenge_retries": [([("idempotency_key", ASCENDING)], {"unique": True}), ([("status", ASCENDING), ("due_at", ASCENDING)], {})],
}

# Previous unique indexes, dropped once the per-person index exists. Dropping first would leave a window with no uniqueness.
LEGACY_INDEX_NAMES = {"profile": ["key_1"], "jobs": ["dedup_keys_1"], "recipients": ["aliases_1"]}


class ScopedCollection:
    """A collection view that reads and writes one person's documents. The raw collection is `coll`."""

    def __init__(self, coll: Collection, user_id: str) -> None:
        self.coll = coll
        self.user_id = user_id

    def _filter(self, filter_: dict | None) -> dict:
        return {**(filter_ or {}), "user_id": self.user_id}

    def _stamp(self, doc: dict) -> dict:
        doc["user_id"] = self.user_id
        return doc

    def find(self, filter: dict | None = None, *args: Any, **kwargs: Any):
        return self.coll.find(self._filter(filter), *args, **kwargs)

    def find_one(self, filter: dict | None = None, *args: Any, **kwargs: Any):
        return self.coll.find_one(self._filter(filter), *args, **kwargs)

    def insert_one(self, document: dict, *args: Any, **kwargs: Any):
        return self.coll.insert_one(self._stamp(document), *args, **kwargs)

    def insert_many(self, documents: list[dict], *args: Any, **kwargs: Any):
        return self.coll.insert_many([self._stamp(d) for d in documents], *args, **kwargs)

    def update_one(self, filter: dict, update: dict, *args: Any, upsert: bool = False, **kwargs: Any):
        return self.coll.update_one(self._filter(filter), self._upsert(update, upsert), *args, upsert=upsert, **kwargs)

    def update_many(self, filter: dict, update: dict, *args: Any, upsert: bool = False, **kwargs: Any):
        return self.coll.update_many(self._filter(filter), self._upsert(update, upsert), *args, upsert=upsert, **kwargs)

    def replace_one(self, filter: dict, replacement: dict, *args: Any, upsert: bool = False, **kwargs: Any):
        return self.coll.replace_one(self._filter(filter), self._stamp(replacement), *args, upsert=upsert, **kwargs)

    def delete_one(self, filter: dict, *args: Any, **kwargs: Any):
        return self.coll.delete_one(self._filter(filter), *args, **kwargs)

    def delete_many(self, filter: dict, *args: Any, **kwargs: Any):
        return self.coll.delete_many(self._filter(filter), *args, **kwargs)

    def count_documents(self, filter: dict | None = None, *args: Any, **kwargs: Any):
        return self.coll.count_documents(self._filter(filter), *args, **kwargs)

    def estimated_document_count(self, *args: Any, **kwargs: Any):
        return self.coll.count_documents({"user_id": self.user_id})

    def aggregate(self, pipeline: list, *args: Any, **kwargs: Any):
        return self.coll.aggregate([{"$match": {"user_id": self.user_id}}, *pipeline], *args, **kwargs)

    def find_one_and_update(self, filter: dict, update: dict, *args: Any, **kwargs: Any):
        update = dict(update)
        update.setdefault("$set", {})
        if isinstance(update["$set"], dict):
            update["$set"] = {**update["$set"], "user_id": self.user_id}
        return self.coll.find_one_and_update(self._filter(filter), update, *args, **kwargs)

    def _upsert(self, update: dict, upsert: bool) -> dict:
        if not upsert:
            return update
        update = dict(update)
        on_insert = dict(update.get("$setOnInsert") or {})
        on_insert["user_id"] = self.user_id
        update["$setOnInsert"] = on_insert
        return update


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
            info = coll.index_information()
            for old in LEGACY_INDEX_NAMES.get(name, []):
                if old in info:
                    coll.drop_index(old)
        for name in OWNED_COLLECTIONS:
            created.setdefault(name, []).append(self.db[name].create_index([("user_id", ASCENDING)]))
        return created

    def _uid(self) -> str | None:
        return current_user_id.get()

    def _owned_filter(self, collection: str, filter_: dict | None) -> dict:
        query = dict(filter_ or {})
        uid = self._uid()
        if uid and collection in OWNED_COLLECTIONS:
            query["user_id"] = uid
        return query

    def c(self, name: str) -> Collection | ScopedCollection:
        coll = self.db[name]
        uid = self._uid()
        if uid and name in OWNED_COLLECTIONS:
            return ScopedCollection(coll, uid)
        return coll

    # -- audit -------------------------------------------------------------
    def audit(self, event: str, subject_id: str | None = None, **data: Any) -> None:
        doc: dict[str, Any] = {"event": event, "subject_id": subject_id, "data": data, "created_at": utcnow()}
        if self._uid():
            doc["user_id"] = self._uid()
        self.db["audit"].insert_one(doc)

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
    def reserve(
        self,
        collection: str,
        filter_: dict,
        owner: str,
        from_states: Iterable[str],
        to_state: str,
        lease_seconds: int = 900,
        extra: dict | None = None,
        allow_takeover: bool = True,
    ) -> dict | None:
        """Move one document from one of `from_states` to `to_state` for `owner`, atomically.

        Returns the updated document, or None when another worker already holds it. With
        `allow_takeover` (default) an expired lease (reserved_until in the past) can be taken over so a
        crashed worker does not block forever. External actions pass `allow_takeover=False`: an expired
        lease there means the outcome is unknown and must become `needs_verification`, never a re-run.
        """
        now = utcnow()
        states = {"status": {"$in": list(from_states)}}
        query = {**filter_, "$or": [states, {"status": to_state, "reserved_until": {"$lt": now}}]} if allow_takeover else {**filter_, **states}
        query = self._owned_filter(collection, query)
        fields = {"status": to_state, "reserved_by": owner, "reserved_at": now, "reserved_until": now + timedelta(seconds=lease_seconds), "updated_at": now, **(extra or {})}
        if self._uid() and collection in OWNED_COLLECTIONS:
            fields["user_id"] = self._uid()
        update = {"$set": fields}
        return self.db[collection].find_one_and_update(query, update, return_document=ReturnDocument.AFTER)

    def release(self, collection: str, filter_: dict, owner: str, to_state: str, extra: dict | None = None) -> dict | None:
        now = utcnow()
        return self.db[collection].find_one_and_update(
            self._owned_filter(collection, {**filter_, "reserved_by": owner}),
            {"$set": {"status": to_state, "updated_at": now, **(extra or {})}, "$unset": {"reserved_by": "", "reserved_until": ""}},
            return_document=ReturnDocument.AFTER,
        )

    def insert_unique(self, collection: str, doc: dict) -> bool:
        """Insert; return False if a unique index rejected it."""
        try:
            uid = self._uid()
            if uid and collection in OWNED_COLLECTIONS:
                doc["user_id"] = uid
            self.db[collection].insert_one(doc)
            return True
        except DuplicateKeyError:
            return False

    # -- scheduler ---------------------------------------------------------
    def claim_schedule_slot(self, slot_key: str, owner: str) -> bool:
        uid = self._uid()
        if uid:
            slot_key = f"{uid}:{slot_key}"
        return self.insert_unique("scheduler_slots", {"slot_key": slot_key, "owner": owner, "created_at": utcnow()})

    # -- runs --------------------------------------------------------------
    def create_run(self, kind: str, params: dict, thread_id: str | None = None) -> dict:
        run = {
            "_id": new_id("run"),
            "kind": kind,
            "params": params,
            "status": "queued",
            "thread_id": thread_id or new_id("thread"),
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "events": [],
            "budget": {},
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0, "tool_steps": 0, "cost_usd": None, "cost_known": True},
        }
        if self._uid():
            run["user_id"] = self._uid()
        self.db["runs"].insert_one(run)
        return run

    def update_run(self, run_id: str, **fields: Any) -> None:
        self.db["runs"].update_one(self._owned_filter("runs", {"_id": run_id}), {"$set": {**fields, "updated_at": utcnow()}})

    def run_event(self, run_id: str, message: str, **data: Any) -> None:
        self.db["runs"].update_one(self._owned_filter("runs", {"_id": run_id}), {"$push": {"events": {"at": utcnow(), "message": message, **data}}})

    def run_requested_state(self, run_id: str) -> str | None:
        doc = self.db["runs"].find_one({"_id": run_id}, {"requested": 1})
        return (doc or {}).get("requested")


def utc_floor(dt: datetime, minutes: int) -> datetime:
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // minutes) * minutes)
