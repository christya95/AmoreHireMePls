"""A dashboard brings a stop record onto the computer that is showing it. Every byte in here is fictional."""

from __future__ import annotations

import hashlib
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply.stop_fetch import FAILED, reset_brought_sessions
from amorehiremepls.apply.stop_record import KIND, clear_stop_records
from amorehiremepls.core.backup import backup
from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.util import sha256_file, utcnow
from amorehiremepls.web.stop_view import NOTICE
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

pytestmark = pytest.mark.mongo

PASSWORD = "correct-horse-battery"
PNG = b"\x89PNG\r\n\x1a\nFIXTURE-PICTURE"
NETWORK = b'{"rows":[{"n":1,"method":"GET","url":"http://fixture.invalid/apply","status":204}]}'
RECORD = json.dumps({"page": {"url": "http://fixture.invalid/apply", "title": "Fixture"}, "counts": {"requests": 1}}).encode()
BODIES = {"art_view": PNG, "art_net": NETWORK, "art_rec": RECORD}
ATTEMPT = "20260101T000000_fix"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        self.server.hits += 1  # type: ignore[attr-defined]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path != "/login" or raw != b"password=" + PASSWORD.encode():
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", "amore_session=ok; Path=/; HttpOnly")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self.server.hits += 1  # type: ignore[attr-defined]
        if "amore_session=ok" not in (self.headers.get("Cookie") or ""):
            self.send_response(303)
            self.send_header("Location", "/login")
            self.end_headers()
            return
        path = urlsplit(self.path).path
        if path == "/":
            self.send_response(200)
            self.end_headers()
            return
        art_id = path.split("/")[2] if path.startswith("/documents/") else ""
        body = self.server.bodies.get(art_id)  # type: ignore[attr-defined]
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies
        self.hits = 0
        super().__init__(("127.0.0.1", 0), _Handler)

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
        self.server_close()


@pytest.fixture
def world(store, tmp_path):
    reset_brought_sessions()
    artifacts = tmp_path / "a"
    settings = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=artifacts,
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password=PASSWORD,
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        stop_record_origin="http://127.0.0.1:9",
        _env_file=None,
    )
    store.c("jobs").insert_one({"_id": "job_fix", "employer": "Northwind", "title": "Analyst", "status": "qualified"})
    store.c("applications").insert_one({"_id": "app_fix", "job_id": "job_fix", "status": "handoff", "provenance": {"origin": "local"}, "updated_at": utcnow()})
    rt = Runtime(settings, store=store)
    client = TestClient(create_app(rt), follow_redirects=False)
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303
    return rt, client, artifacts


def _register(store, tmp_path, bodies: dict[str, bytes]) -> None:
    remote = tmp_path / "other-computer"
    specs = {
        "art_view": ("view", "view.png", "image/png"),
        "art_net": ("network", "network.json", "application/json"),
        "art_rec": ("record", "record.json", "application/json"),
    }
    for art_id, (label, name, media) in specs.items():
        raw = bodies[art_id]
        doc = {
            "_id": art_id,
            "owner_id": "job_fix",
            "job_id": "job_fix",
            "application_id": "app_fix",
            "kind": KIND,
            "label": label,
            "attempt": ATTEMPT,
            "path": str(remote / name),
            "filename": name,
            "sha256": hashlib.sha256(BODIES[art_id]).hexdigest(),
            "size": len(BODIES[art_id]),
            "media_type": media,
            "captured_at": utcnow(),
            "created_at": utcnow(),
        }
        if label == "record":
            doc["summary"] = {"counts": {"requests": 1}, "notes": [], "errors": []}
        store.c("artifacts").insert_one(doc)
        assert raw  # the registered hash is of BODIES, even when the server sends something else
    assert not remote.exists()


def _folder(artifacts: Path) -> Path:
    return artifacts / "jobs" / "job_fix" / "stop" / ATTEMPT


def test_a_dashboard_brings_the_files_and_serves_the_picture_by_hash(store, tmp_path, world, caplog):
    caplog.set_level("DEBUG")
    rt, client, artifacts = world
    _register(store, tmp_path, BODIES)
    with _Server(BODIES) as server:
        rt.settings.stop_record_origin = f"http://127.0.0.1:{server.server_address[1]}"
        page = client.get("/applications/app_fix").text
        assert server.hits >= 1
    assert "not on this computer" not in page and "not on this one" not in page and 'id="stop-fetch"' not in page
    assert NOTICE in page and "passwords, verification codes and session cookies" in page
    assert 'data-load="/applications/app_fix/stop-record?part=1"' in page
    folder = _folder(artifacts)
    picture = folder / "view.png"
    assert picture.is_file() and sha256_file(picture) == hashlib.sha256(PNG).hexdigest() and oct(picture.stat().st_mode)[-3:] == "600"
    assert (folder / "brought").is_file() and not (tmp_path / "other-computer").exists()
    got = client.get("/documents/art_view/download?kind=png&inline=1")
    assert got.status_code == 200 and got.headers["content-type"].startswith("image/png")
    assert hashlib.sha256(got.content).hexdigest() == hashlib.sha256(PNG).hexdigest()
    panel = client.get("/applications/app_fix/stop-record?part=1").text
    assert "fixture.invalid" in panel and 'id="stop-fetch"' not in panel
    again = client.get("/applications/app_fix").text  # the copy is local, so this does not ask the other computer again
    assert 'id="stop-fetch"' not in again
    assert PASSWORD not in caplog.text and "FIXTURE-PICTURE" not in caplog.text
    dumped = json.dumps(list(store.c("artifacts").find()), default=str)
    assert "FIXTURE-PICTURE" not in dumped
    resume = artifacts / "jobs" / "job_fix" / "resume.docx"
    resume.write_bytes(b"resume-fixture")
    archive = tmp_path / "backup.tar.gz"
    backup(store, artifacts, tmp_path / "p", archive)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert any(n.endswith("resume.docx") for n in names) and not any("/stop/" in n or n.endswith("/stop") for n in names)


def test_bytes_that_do_not_match_the_hash_are_not_kept(store, tmp_path, world):
    rt, client, artifacts = world
    _register(store, tmp_path, BODIES)
    with _Server({key: b"nope" for key in BODIES}) as server:
        rt.settings.stop_record_origin = f"http://127.0.0.1:{server.server_address[1]}"
        page = client.get("/applications/app_fix").text
    assert FAILED in page and "Try again" in page and 'id="stop-fetch"' in page
    assert "not on this computer" not in page and "not on this one" not in page and NOTICE in page
    assert not _folder(artifacts).exists()
    assert client.get("/documents/art_view/download?kind=png").status_code == 404


def test_clearing_the_record_removes_the_copy_on_this_computer(store, tmp_path, world):
    rt, client, artifacts = world
    _register(store, tmp_path, BODIES)
    with _Server(BODIES) as server:
        rt.settings.stop_record_origin = f"http://127.0.0.1:{server.server_address[1]}"
        assert client.get("/applications/app_fix").status_code == 200
    folder = _folder(artifacts)
    assert folder.is_dir()
    app = store.c("applications").find_one({"_id": "app_fix"})
    store.c("applications").update_one({"_id": "app_fix"}, {"$set": {"status": "submitted"}})
    kept = store.c("applications").find_one({"_id": "app_fix"})
    assert clear_stop_records(store, kept, artifacts) == 0 and folder.is_dir()
    store.c("applications").update_one({"_id": "app_fix"}, {"$set": {"status": "handoff"}})
    assert clear_stop_records(store, app, artifacts) > 0 and not folder.exists()


def test_a_copy_whose_record_was_cleared_elsewhere_is_removed_when_the_page_opens(store, tmp_path, world):
    rt, client, artifacts = world
    _register(store, tmp_path, BODIES)
    with _Server(BODIES) as server:
        rt.settings.stop_record_origin = f"http://127.0.0.1:{server.server_address[1]}"
        assert 'id="stop-fetch"' not in client.get("/applications/app_fix").text
    store.c("artifacts").delete_many({"kind": KIND})  # the computer that ran the attempt started a new one
    page = client.get("/applications/app_fix").text
    assert "No record was kept for this stop." in page and not _folder(artifacts).exists()


def test_this_dashboard_does_not_ask_itself_for_the_files(store, tmp_path, world):
    rt, client, artifacts = world
    _register(store, tmp_path, BODIES)
    with _Server(BODIES) as server:
        port = server.server_address[1]
        rt.settings.bind_host = "127.0.0.1"
        rt.settings.port = port
        rt.settings.stop_record_origin = f"http://127.0.0.1:{port}"
        page = client.get("/applications/app_fix").text
        assert server.hits == 0
    assert FAILED in page and "Try again" in page and not _folder(artifacts).exists()
