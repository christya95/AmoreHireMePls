"""Shared fixtures. Mongo-backed tests use a throwaway database on the local dev container."""

from __future__ import annotations

import os
import uuid

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from amorehiremepls.db import Store

TEST_MONGO_URI = os.environ.get("AMORE_TEST_MONGODB_URI", "mongodb://127.0.0.1:27027")
# A fixed throwaway database name lets the suite run with a least-privilege user (readWrite on that one
# database, no dropDatabase); collections are dropped after each test instead of the database.
TEST_MONGO_DATABASE = os.environ.get("AMORE_TEST_MONGODB_DATABASE", "")


def _mongo_available() -> bool:
    try:
        MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=1500).admin.command("ping")
        return True
    except PyMongoError:
        return False


MONGO_AVAILABLE = _mongo_available()


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason=f"dev MongoDB not reachable at {TEST_MONGO_URI}")
    for item in items:
        if "mongo" in item.keywords and not MONGO_AVAILABLE:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _test_environment(tmp_path, monkeypatch):
    """Tests never read the real drop folder (candidate/): point every Settings() at an empty one. The suite's
    fictional candidate lives in Toronto with a GitHub tracker, so the process-wide settings say so explicitly
    (the code's own defaults are UTC and no tracker)."""
    from amorehiremepls.config import get_settings

    monkeypatch.setenv("AMORE_CANDIDATE_DIR", str(tmp_path / "candidate-empty"))
    monkeypatch.setenv("AMORE_TIMEZONE", "America/Toronto")
    monkeypatch.setenv("AMORE_TRACKER_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def store():
    client = MongoClient(TEST_MONGO_URI, serverSelectionTimeoutMS=3000)
    name = TEST_MONGO_DATABASE or f"amore_test_{uuid.uuid4().hex[:10]}"
    s = Store(client, name)
    if TEST_MONGO_DATABASE:
        for coll in client[name].list_collection_names():
            client[name].drop_collection(coll)
    s.ensure_indexes()
    try:
        yield s
    finally:
        if TEST_MONGO_DATABASE:
            for coll in client[name].list_collection_names():
                client[name].drop_collection(coll)
        else:
            client.drop_database(name)
        client.close()


@pytest.fixture
def tmp_dirs(tmp_path):
    d = {
        "artifacts": tmp_path / "artifacts",
        "private": tmp_path / "private",
        "logs": tmp_path / "logs",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d
