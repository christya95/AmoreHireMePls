"""Shared fixtures. Mongo-backed tests use a throwaway database on the local dev container."""

from __future__ import annotations

import os
import socket
import subprocess
import uuid
import webbrowser
from urllib.parse import urlsplit

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from amorehiremepls.core.db import Store

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


# -- nothing in a test reaches the world outside this computer ----------------------------------------------------
# Three guards, set once for the whole run: no browser window, no request to a real host, no real 1Password tool.
# A test that trips one fails, also when the code under test swallowed the refusal.
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "", urlsplit(TEST_MONGO_URI).hostname or ""}
_REFUSED: list[str] = []


class RefusedInTests(OSError):
    """What a guarded call raises. An OSError, so code that handles a network failure handles it the same way."""


def _local_address(address) -> bool:
    if not isinstance(address, tuple):  # a unix socket path
        return True
    host = str(address[0]).lower()
    return host in _LOCAL_HOSTS or host.startswith("127.")


def _refuse(what: str) -> RefusedInTests:
    _REFUSED.append(what)
    return RefusedInTests(f"a test tried to {what}")


def _guard_sockets() -> None:
    real_connect, real_connect_ex, real_lookup = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def connect(self, address):
        if not _local_address(address):
            raise _refuse(f"connect to {address[0]}")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _local_address(address):
            raise _refuse(f"connect to {address[0]}")
        return real_connect_ex(self, address)

    def lookup(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        if not _local_address((name, 0)) and name != socket.gethostname():
            raise _refuse(f"look up {name}")
        return real_lookup(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, lookup


def _guard_tools() -> None:
    """The real 1Password tool is never started, and nothing opens the person's own browser."""
    real_popen = subprocess.Popen

    class Popen(real_popen):
        def __init__(self, args, *rest, **kwargs):
            first = args if isinstance(args, (str, bytes)) else (list(args) or [""])[0]
            first = first.decode() if isinstance(first, bytes) else str(first)
            if os.path.basename(first.split()[0] if first.split() else "") in ("op", "open", "xdg-open"):
                raise _refuse(f"run {os.path.basename(first.split()[0])}")
            super().__init__(args, *rest, **kwargs)

    subprocess.Popen = Popen

    def no_window(url, *args, **kwargs):
        raise _refuse("open a browser window")

    webbrowser.open = webbrowser.open_new = webbrowser.open_new_tab = no_window


def _local_url(url: str) -> bool:
    if url.startswith(("about:", "data:", "blob:", "chrome:")):
        return True
    return _local_address(((urlsplit(url).hostname or ""), 0))


def _fence_context(context) -> None:
    """Abort a request that leaves this computer. The abort is immediate, so the browser can still exit."""

    def fence(route):
        if _local_url(route.request.url):
            route.continue_()
        else:
            route.abort()

    context.route("**/*", fence)


def _guard_chromium() -> None:
    """Chromium in tests is always headless. Other hosts fail at once instead of waiting out a resolver.

    `MAP * ~NOTFOUND` made Chromium's network service wait out its shutdown, so the suite sat for minutes
    after the tests had passed. Mapping those names to an address that refuses the connection ends the wait.
    """
    try:
        from playwright.sync_api import BrowserType
    except ImportError:
        return
    real_launch = BrowserType.launch

    def launch(self, *args, **kwargs):
        kwargs["headless"] = True
        kwargs["args"] = [
            *(kwargs.get("args") or []),
            "--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost, EXCLUDE 127.0.0.1, EXCLUDE ::1",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--no-first-run",
        ]
        browser = real_launch(self, *args, **kwargs)
        real_new_context, real_new_page = browser.new_context, browser.new_page

        def new_context(*ctx_args, **ctx_kwargs):
            context = real_new_context(*ctx_args, **ctx_kwargs)
            _fence_context(context)
            return context

        def new_page(*page_args, **page_kwargs):
            page = real_new_page(*page_args, **page_kwargs)
            _fence_context(page.context)
            return page

        browser.new_context, browser.new_page = new_context, new_page
        return browser

    BrowserType.launch = launch


_guard_sockets()
_guard_tools()
_guard_chromium()


@pytest.fixture(autouse=True)
def _nothing_left_this_computer():
    del _REFUSED[:]
    yield
    refused = list(dict.fromkeys(_REFUSED))
    del _REFUSED[:]
    assert not refused, f"this test tried to: {'; '.join(refused)}"


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
    from amorehiremepls.core.config import get_settings

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
