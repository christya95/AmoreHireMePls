"""Per-person accounts in the one application database.

Admin access does not expire. A member gets 365 days, which can be extended. Ending or turning off access
stops new work for that person and does not pause tracker sync: queued GitHub ledger items still flush.
That sync rule is an owner decision (2026-11-05).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from bson import json_util

from amorehiremepls.core.db import OWNED_COLLECTIONS, Store, current_user_id
from amorehiremepls.core.util import new_id, sha256_file, utcnow

MEMBER_ACCESS_DAYS = 365
MIN_PASSWORD = 12
_LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_hasher = PasswordHasher()  # argon2id is the library default


class AccountError(ValueError):
    pass


@contextmanager
def user_scope(user_id: str | None):
    """Bind later store reads and writes to one person. None clears the bind."""
    token = current_user_id.set(user_id)
    try:
        yield
    finally:
        current_user_id.reset(token)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError:
        return False


def normalize_login(value: str) -> str:
    return value.strip().lower()


def valid_login(value: str) -> bool:
    return bool(_LOGIN.match(normalize_login(value)))


def account_is_active(user: dict | None, now: datetime | None = None) -> bool:
    """True when this person may be worked: status active, and a member whose access has not ended."""
    if not user or user.get("status") != "active":
        return False
    if user.get("role") == "admin":
        return True
    until = user.get("access_until")
    return bool(until) and until > (now or utcnow())


def access_summary(user: dict | None, now: datetime | None = None) -> dict[str, Any]:
    """What the Overview badge needs. Admin has no expiry badge. Days are calendar days left."""
    now = now or utcnow()
    role = (user or {}).get("role") or "member"
    status = (user or {}).get("status") or "off"
    if role == "admin":
        return {"role": "admin", "status": status, "expired": status != "active", "days_left": None, "badge": None}
    until = (user or {}).get("access_until")
    if status == "off":
        return {"role": role, "status": status, "expired": True, "days_left": 0, "badge": "Access turned off"}
    if status == "deleted" or until is None or until <= now:
        return {"role": role, "status": status, "expired": True, "days_left": 0, "badge": "Access ended"}
    days = (until.date() - now.date()).days
    if days < 1:
        return {"role": role, "status": status, "expired": False, "days_left": 0, "badge": "Less than a day of access left"}
    label = "1 day of access left" if days == 1 else f"{days} days of access left"
    return {"role": role, "status": status, "expired": False, "days_left": days, "badge": label}


def active_user_ids(store: Store, now: datetime | None = None) -> list[str] | None:
    """None when nobody has been migrated yet (the single-operator behaviour). Otherwise the people who may be worked."""
    if store.db["users"].count_documents({}) == 0:
        return None
    now = now or utcnow()
    return [u["_id"] for u in store.db["users"].find({"status": "active"}) if account_is_active(u, now)]


def _require(store: Store, user_id: str) -> dict:
    user = store.db["users"].find_one({"_id": user_id, "status": {"$ne": "deleted"}})
    if not user:
        raise AccountError("that person was not found")
    return user


def _last_active_admin(store: Store, user: dict) -> bool:
    if user.get("role") != "admin" or user.get("status") != "active":
        return False
    return store.db["users"].count_documents({"role": "admin", "status": "active", "_id": {"$ne": user["_id"]}}) == 0


@dataclass
class AuthResult:
    user: dict | None
    reason: str  # ok | invalid | name_required | no_accounts


def authenticate(store: Store, password: str, login: str = "") -> AuthResult:
    """Match a password to one living account. Two matches without a sign-in name ask for the name."""
    users = list(store.db["users"].find({"status": {"$ne": "deleted"}}))
    if not users:
        return AuthResult(None, "no_accounts")
    name = normalize_login(login)
    if name:
        user = next((u for u in users if u.get("login") == name), None)
        if user and verify_password(user.get("password_hash") or "", password):
            return AuthResult(user, "ok")
        return AuthResult(None, "invalid")
    matches = [u for u in users if verify_password(u.get("password_hash") or "", password)]
    if len(matches) == 1:
        return AuthResult(matches[0], "ok")
    if len(matches) > 1:
        return AuthResult(None, "name_required")
    return AuthResult(None, "invalid")


def migrate_users(store: Store, password: str, display_name: str) -> dict[str, Any]:
    """Create the admin with no expiry, if missing, and stamp ownerless records with that person. Idempotent.

    The report is counts only. The display name is stored on the account and is not returned."""
    if len(password) < MIN_PASSWORD:
        raise AccountError("password must be at least 12 characters")
    admin = store.db["users"].find_one({"role": "admin", "status": {"$ne": "deleted"}})
    created = admin is None
    if admin is None:
        now = utcnow()
        admin = {
            "_id": new_id("user"),
            "login": "admin",
            "display_name": (display_name or "").strip()[:80],
            "role": "admin",
            "status": "active",
            "access_until": None,
            "password_hash": hash_password(password),
            "created_at": now,
            "updated_at": now,
        }
        store.db["users"].insert_one(admin)
    stamped: dict[str, int] = {}
    for name in sorted(OWNED_COLLECTIONS):
        result = store.db[name].update_many({"user_id": {"$exists": False}}, {"$set": {"user_id": admin["_id"]}})
        if result.modified_count:
            stamped[name] = result.modified_count
    return {"admin_created": created, "stamped_documents": sum(stamped.values()), "stamped": stamped}


def create_member(store: Store, login: str, password: str, display_name: str = "", now: datetime | None = None) -> str:
    """Add a member with 365 days of access. Returns the new id. Raises AccountError on a bad name or a short password."""
    login = normalize_login(login)
    if not valid_login(login):
        raise AccountError("use 2 to 32 letters or numbers for the sign-in name")
    if len(password) < MIN_PASSWORD:
        raise AccountError("password must be at least 12 characters")
    now = now or utcnow()
    user_id = new_id("user")
    doc = {
        "_id": user_id,
        "login": login,
        "display_name": display_name.strip()[:80],
        "role": "member",
        "status": "active",
        "access_until": now + timedelta(days=MEMBER_ACCESS_DAYS),
        "password_hash": hash_password(password),
        "created_at": now,
        "updated_at": now,
    }
    if not store.insert_unique("users", doc):
        raise AccountError("that sign-in name is already in use")
    return user_id


def set_password(store: Store, user_id: str, password: str) -> None:
    _require(store, user_id)
    if len(password) < MIN_PASSWORD:
        raise AccountError("password must be at least 12 characters")
    store.db["users"].update_one({"_id": user_id}, {"$set": {"password_hash": hash_password(password), "updated_at": utcnow()}})


def extend_access(store: Store, user_id: str, days: int = MEMBER_ACCESS_DAYS, now: datetime | None = None) -> datetime | None:
    """Add days on top of the later of now and the current end. Admin access stays open and returns None."""
    user = _require(store, user_id)
    if days < 1:
        raise AccountError("extend by at least one day")
    if user.get("role") == "admin":
        return None
    now = now or utcnow()
    base = user.get("access_until") or now
    if base < now:
        base = now
    until = base + timedelta(days=days)
    store.db["users"].update_one({"_id": user_id}, {"$set": {"access_until": until, "status": "active", "updated_at": now}})
    return until


def set_user_status(store: Store, user_id: str, status: str) -> None:
    if status not in ("active", "off"):
        raise AccountError("status must be active or off")
    user = _require(store, user_id)
    if status == "off" and _last_active_admin(store, user):
        raise AccountError("the last admin cannot be turned off")
    store.db["users"].update_one({"_id": user_id}, {"$set": {"status": status, "updated_at": utcnow()}})


def backup_user(store: Store, user_id: str, dest_dir: Path) -> dict[str, Any]:
    """Write this person's records to a private JSON file. The password hash stays in the file and out of the report."""
    user = store.db["users"].find_one({"_id": user_id})
    if not user:
        raise AccountError("that person was not found")
    dest_dir.mkdir(parents=True, exist_ok=True)
    collections: dict[str, list] = {}
    counts: dict[str, int] = {}
    for name in sorted(OWNED_COLLECTIONS):
        docs = list(store.db[name].find({"user_id": user_id}))
        collections[name] = docs
        counts[name] = len(docs)
    path = dest_dir / f"{user_id}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json_util.dumps({"user": user, "collections": collections}), encoding="utf-8")
    return {"sha256": sha256_file(path), "counts": counts, "documents": sum(counts.values())}


def delete_user(store: Store, user_id: str, dest_dir: Path) -> dict[str, Any]:
    """Back up, remove the person's records, and leave a tombstone with no password. The last admin cannot be deleted."""
    user = _require(store, user_id)
    if _last_active_admin(store, user):
        raise AccountError("the last admin cannot be deleted")
    backup = backup_user(store, user_id, dest_dir)
    for name in OWNED_COLLECTIONS:
        store.db[name].delete_many({"user_id": user_id})
    store.db["users"].update_one(
        {"_id": user_id},
        {
            "$set": {"status": "deleted", "deleted_at": utcnow(), "tombstone": True, "backup_sha256": backup["sha256"], "updated_at": utcnow()},
            "$unset": {"password_hash": "", "display_name": ""},
        },
    )
    return {"backup_sha256": backup["sha256"], "documents": backup["documents"]}


def usage_totals(store: Store) -> dict[str | None, dict[str, Any]]:
    """Calls and known cost per person. Missing user_id (records from before migration) groups under None."""
    out: dict[str | None, dict[str, Any]] = {}
    pipeline = [
        {
            "$group": {
                "_id": "$user_id",
                "calls": {"$sum": 1},
                "cost": {"$sum": {"$ifNull": ["$cost_usd", 0]}},
                "unknown": {"$sum": {"$cond": [{"$eq": ["$cost_known", False]}, 1, 0]}},
            }
        }
    ]
    for row in store.db["usage"].aggregate(pipeline):
        out[row["_id"]] = {"calls": row["calls"], "cost": row["cost"], "unknown": row["unknown"]}
    return out
