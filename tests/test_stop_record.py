"""The stop record: what the page showed and sent when an attempt stopped. Fixture site only; every value is fictional."""

from __future__ import annotations

import json
import logging
import re
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.apply import stop_record
from amorehiremepls.apply.applications import ApplicationService
from amorehiremepls.apply.browser import ActionFailed, BrowserAdapter, BrowserConfig, HandoffRequired, Plan, Step
from amorehiremepls.apply.signin import SiteSignIn
from amorehiremepls.apply.stop_record import BODY_CAP, KIND, SAVED_LINE, StopRecorder, StopRecords, clear_stop_records
from amorehiremepls.core.backup import backup
from amorehiremepls.core.config import Settings
from amorehiremepls.core.orchestration import queue_approved_send
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.site_signin import SignInMemory
from amorehiremepls.core.util import new_id, sha256_file, utcnow
from amorehiremepls.web.stop_view import NOTICE
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.fixtures import signin_site as fx
from tests.fixtures.signin_site import PASSWORD, SignInSite
from tests.test_applications_and_sync import _job
from tests.test_browser_adapter import skip_no_browser
from tests.test_signin import FAST, HOST, SECRET, FakeHold, Owner, _database_text, _free_port

MARKERS = (fx.QUERY_TOKEN, fx.COOKIE_VALUE, fx.BODY_MARKER, fx.REPLY_MARKER, fx.CONSOLE_MARKER, fx.PAGE_ERROR_MARKER, PASSWORD)
pytestmark = [pytest.mark.mongo, pytest.mark.browser, skip_no_browser]


@pytest.fixture
def site():
    with SignInSite(_free_port()) as running:
        yield running


@pytest.fixture
def world(store, tmp_path):
    """One job with an approved, unsent application; a browser that records stops; and the signed-in dashboard."""
    settings = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret=SECRET,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(settings, store=store)
    job = _job(store)
    store.c("applications").insert_one(
        {"_id": "app_s", "job_id": job["_id"], "status": "authorized", "material_hash": "hash_s", "updated_at": utcnow(), "provenance": {"origin": "local"}}
    )
    run = store.create_run("application", {"job_id": job["_id"]})
    store.update_run(run["_id"], status="running")
    records = StopRecords(store, settings.artifact_dir)
    browser = BrowserAdapter(
        BrowserConfig(allowed_hosts={HOST}, session_dir=tmp_path / "sessions", artifact_dir=settings.artifact_dir, default_timeout_ms=8000), stop_records=records
    )
    client = TestClient(create_app(rt), follow_redirects=False)
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return SimpleNamespace(rt=rt, store=store, job=job, run_id=run["_id"], records=records, browser=browser, client=client, artifacts=settings.artifact_dir, tmp=tmp_path)


def _plan(site, world, sc: str = "noisy", path: str = "stop") -> Plan:
    return Plan(
        start_url=f"{site.base}/{path}/{sc}/job",
        steps=[Step("fill", "#ready_note", "ready"), Step("fill", "#full_name", "Casey Fixture"), Step("click", "#submit", final=True)],
        receipt_text="Application received",
        receipt_must_include=[f"R-{sc}"],
        job_id=world.job["_id"],
        handoff_if_submit_missing=True,
    )


def _stop(site, world, sc: str = "noisy", **kwargs):
    """Run one attempt that stops before anything is sent: the form never offers its send button. Returns the stop."""
    notes: list[str] = []
    with pytest.raises((HandoffRequired, ActionFailed)) as caught:
        world.browser.run_plan(_plan(site, world, sc), site_key=HOST, progress=notes.append, **kwargs)
    world.store.c("applications").update_one({"_id": "app_s"}, {"$set": {"status": "handoff", "evidence": caught.value.evidence, "reason": str(caught.value)}})
    return caught.value, notes


def _arts(world) -> dict[str, dict]:
    return {a["label"]: a for a in world.store.c("artifacts").find({"kind": KIND}).sort("created_at", 1)}


def _rows(world) -> list[dict]:
    return json.loads(Path(_arts(world)["network"]["path"]).read_text())["rows"]


def _row(rows: list[dict], part: str) -> dict:
    return next(r for r in rows if part in r["url"])


def _outside_record(world, caplog, *more) -> str:
    """Everything the app keeps apart from the record's own files: the database, the logs, and what a caller passes."""
    return "\n".join([_database_text(world.store), caplog.text, *[json.dumps(m, default=str) if not isinstance(m, str) else m for m in more]])


def test_a_stop_keeps_the_page_and_every_request_with_headers_bodies_and_cookies(site, world, caplog):
    caplog.set_level(logging.DEBUG)
    stop, notes = _stop(site, world)
    arts = _arts(world)
    assert set(arts) == {"view", "page", "html", "network", "console", "har", "trace", "record"}
    for art in arts.values():  # hashed, registered for this application, inside this job's private folder
        path = Path(art["path"])
        assert path.is_file() and sha256_file(path) == art["sha256"] and art["application_id"] == "app_s"
        assert path.is_relative_to(world.artifacts / "jobs" / world.job["_id"] / "stop") and oct(path.stat().st_mode)[-3:] == "600"
    rows = _rows(world)
    data = _row(rows, "/data?")
    assert data["url"].endswith(f"/stop/noisy/data?token={fx.QUERY_TOKEN}&next=%2Fa%3Fb")  # the whole address, with its query
    assert data["status"] == 200 and data["method"] == "GET" and data["type"] == "fetch" and data["time_ms"] is not None
    assert any(n.lower() == "user-agent" for n, _ in data["request_headers"]) and any(n.lower() == "content-type" for n, _ in data["response_headers"])
    assert fx.REPLY_MARKER in data["response_body"]["text"] and data["response_body"]["state"] == "whole"
    assert any(fx.COOKIE_VALUE in c for c in data["cookies_set"])
    posted = _row(rows, "/post")
    assert posted["method"] == "POST" and fx.BODY_MARKER in posted["request_body"]["text"]
    assert ["track", fx.COOKIE_VALUE] in posted["cookies_sent"]  # the cookie the page sent is in the record
    big = _row(rows, "/big")
    assert big["response_body"]["state"] == "cut" and big["response_body"]["size"] > fx.BIG_BYTES
    assert len(big["response_body"]["text"].encode()) == BODY_CAP and big["response_body"]["text"].startswith("START-OF-BIG")
    blob = _row(rows, "/blob")
    assert blob["response_body"]["state"] == "binary" and blob["response_body"]["size"] == 2048 and blob["response_body"]["text"] == ""
    refused = _row(rows, "blocked.invalid")
    assert refused["blocked"] and refused["failed"] and refused["status"] is None and "refused by the app" in refused["failure"]
    assert refused["url"] == "http://blocked.invalid/pixel?id=9"
    gone = _row(rows, ":9/gone")
    assert gone["failed"] and not gone["blocked"] and gone["failure"]
    assert _row(rows, "/missing")["status"] == 404
    record = json.loads(Path(arts["record"]["path"]).read_text())
    assert record["page"]["url"].endswith("/stop/noisy/job") and record["page"]["title"] == "Apply <now> R-noisy"
    assert record["counts"]["blocked"] == 1 and record["counts"]["failed"] == 1 and record["counts"]["errors"] == 1 and record["counts"]["bodies_cut"] == 1
    assert record["counts"]["requests"] == len(rows) and record["errors"] == []
    console = json.loads(Path(arts["console"]["path"]).read_text())["lines"]
    assert any(fx.CONSOLE_MARKER in c["text"] for c in console) and any(c["type"] == "pageerror" and fx.PAGE_ERROR_MARKER in c["text"] for c in console)
    assert "shown as text" in Path(arts["html"]["path"]).read_text()
    with zipfile.ZipFile(arts["har"]["path"]) as archive:  # the archive keeps the body the list had to cut
        har = json.loads(archive.read("har.har"))
        whole = next(e for e in har["log"]["entries"] if e["request"]["url"].endswith("/big"))["response"]["content"]["_file"]
        assert archive.read(whole).endswith(b"END-OF-BIG")
    assert zipfile.ZipFile(arts["trace"]["path"]).namelist()
    # The database holds counts, sizes and hashes. The events hold one plain line. Nothing captured is anywhere else.
    assert arts["record"]["summary"]["counts"]["requests"] == len(rows)
    events = [e["message"] for e in world.store.c("runs").find_one({"_id": world.run_id})["events"]]
    assert SAVED_LINE in events
    outside = _outside_record(world, caplog, stop.evidence, str(stop), notes)
    for marker in MARKERS[:-1]:
        assert marker not in outside
    assert "stop_record" not in json.dumps(stop.evidence, default=str)


def test_a_part_that_cannot_be_recorded_never_changes_why_the_attempt_stopped(site, world, monkeypatch):
    plain, _ = _stop(site, world)

    def broken(*args, **kwargs):
        raise RuntimeError("capture broke")

    for name in ("keep", "stop", "_network", "blocked", "attach"):
        monkeypatch.setattr(StopRecorder, name, broken)
    again, _ = _stop(site, world)
    assert type(again) is type(plain) and str(again) == str(plain) and again.evidence["final_clicked"] is False
    monkeypatch.setattr(StopRecorder, "finish", broken)
    assert str(_stop(site, world)[0]) == str(plain)
    monkeypatch.setattr(StopRecorder, "context_options", broken)
    assert str(_stop(site, world)[0]) == str(plain)
    monkeypatch.setattr(StopRecords, "begin", broken)
    assert str(_stop(site, world)[0]) == str(plain)


def test_a_part_that_failed_is_named_in_the_record(site, world, monkeypatch):
    monkeypatch.setattr(StopRecorder, "_body", lambda self, *a, **k: (_ for _ in ()).throw(ValueError("no body")))
    _stop(site, world)
    summary = _arts(world)["record"]["summary"]
    assert summary["errors"] == ["network: ValueError"] and summary["counts"]["requests"] == 0
    assert "could not be recorded: network: ValueError" in world.client.get("/applications/app_s").text


def test_a_confirmed_success_keeps_no_stop_record(site, world):
    evidence = world.browser.run_plan(_plan(site, world, "ok"), site_key=HOST)
    assert evidence["verified"] is True
    assert _arts(world) == {} and not list((world.artifacts / "jobs" / world.job["_id"] / "stop").glob("*"))


def test_a_sign_in_stop_is_recorded_whole_and_only_there(site, world, caplog):
    caplog.set_level(logging.DEBUG)
    memory = SignInMemory(world.store)
    owner = Owner(memory=memory, fixes=["wait"])
    plan = _plan(site, world, "wrong", path="acct")
    with pytest.raises(HandoffRequired) as caught:
        world.browser.run_plan(plan, site_key=HOST, hold=FakeHold(owner, minutes=0.05), signin=SiteSignIn(SECRET, memory, FAST, job_id=plan.job_id))
    sent = _row(_rows(world), "/acct/wrong/login")
    posts = [r for r in _rows(world) if r["method"] == "POST" and "/acct/wrong/login" in r["url"]]
    assert posts and any(PASSWORD in json.dumps(r.get("form_fields") or r["request_body"]) for r in posts) and sent
    assert PASSWORD not in _outside_record(world, caplog, caught.value.evidence, str(caught.value))  # the one place it is kept


def test_a_new_attempt_clears_the_record_and_proof_is_never_cleared(site, world):
    _stop(site, world)
    first = {a["path"] for a in _arts(world).values()}
    shots = world.artifacts / "jobs" / world.job["_id"] / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    kept, gone = [], []
    for label in ("summary", "receipt", "ambiguous", "handoff", "failed"):
        path = shots / f"20260101T000000_{label}.png"
        path.write_bytes(label.encode())
        art = {"_id": new_id("art"), "owner_id": world.job["_id"], "job_id": world.job["_id"], "application_id": "app_s", "kind": "screenshot", "label": label}
        world.store.c("artifacts").insert_one({**art, "path": str(path), "filename": path.name, "sha256": sha256_file(path), "created_at": utcnow()})
        (gone if label in ("handoff", "failed") else kept).append(path)
    resume = world.artifacts / "jobs" / world.job["_id"] / "resume.docx"
    resume.write_bytes(b"resume")
    world.store.c("artifacts").insert_one({"_id": "art_resume", "owner_id": world.job["_id"], "kind": "resume_tailored", "path": str(resume), "sha256": sha256_file(resume)})
    world.store.c("applications").update_one(
        {"_id": "app_s"}, {"$set": {"evidence.screenshots": [{"label": "handoff", "path": str(gone[0])}, {"label": "summary", "path": str(kept[0])}]}}
    )
    assert world.records.begin(world.job["_id"]) is not None  # what the browser calls before it opens the page
    assert _arts(world) == {} and not any(Path(p).exists() for p in first)
    assert [s["label"] for s in world.store.c("applications").find_one({"_id": "app_s"})["evidence"]["screenshots"]] == ["summary"]
    _stop(site, world)
    second = {a["path"] for a in _arts(world).values()}
    assert first.isdisjoint(second) and not any(Path(p).exists() for p in first) and all(Path(p).is_file() for p in second)
    assert len(list((world.artifacts / "jobs" / world.job["_id"] / "stop").iterdir())) == 1
    assert all(p.is_file() for p in kept) and not any(p.exists() for p in gone) and resume.is_file()
    labels = sorted(a["label"] for a in world.store.c("artifacts").find({"kind": "screenshot"}))
    assert labels == ["ambiguous", "receipt", "summary"] and world.store.c("artifacts").find_one({"_id": "art_resume"})


def test_queuing_a_retry_start_over_and_remove_each_clear_the_record(site, world):
    _stop(site, world)
    world.store.c("runs").update_many({}, {"$set": {"status": "done"}})
    world.store.c("applications").update_one({"_id": "app_s"}, {"$set": {"status": "authorized"}})
    assert queue_approved_send(world.store, world.store.c("applications").find_one({"_id": "app_s"}), True, replace_repeat=True)
    assert _arts(world) == {} and not list((world.artifacts / "jobs" / world.job["_id"] / "stop").glob("*/*"))
    world.store.c("runs").update_many({}, {"$set": {"status": "done"}})
    _stop(site, world)
    paths = [Path(a["path"]) for a in _arts(world).values()]
    svc: ApplicationService = world.rt.applications()
    svc.delete_unsent("app_s", world.tmp / "backups")
    assert _arts(world) == {} and not any(p.exists() for p in paths)
    assert fx.QUERY_TOKEN not in "".join(p.read_text() for p in (world.tmp / "backups").glob("*.json"))


def test_an_unclear_result_and_a_sent_application_keep_their_record(site, world):
    _stop(site, world)
    paths = [Path(a["path"]) for a in _arts(world).values()]
    for status in ("needs_verification", "submitted"):
        world.store.c("applications").update_one({"_id": "app_s"}, {"$set": {"status": status}})
        app = world.store.c("applications").find_one({"_id": "app_s"})
        assert clear_stop_records(world.store, app) == 0 and all(p.is_file() for p in paths)
    world.store.c("applications").update_one({"_id": "app_s"}, {"$set": {"status": "handoff"}})
    world.store.c("actions").insert_one({"_id": "act_s", "idempotency_key": f"submit:{world.job['_id']}", "status": "needs_verification"})
    assert clear_stop_records(world.store, world.store.c("applications").find_one({"_id": "app_s"})) == 0 and all(p.is_file() for p in paths)
    _stop(site, world)  # a later look at an unclear result adds a record and keeps the first
    assert all(p.is_file() for p in paths) and world.store.c("artifacts").count_documents({"kind": KIND, "label": "record"}) == 2


def test_files_whose_record_was_cleared_on_another_computer_are_removed_at_the_next_attempt(site, world, monkeypatch):
    _stop(site, world)
    folder = Path(_arts(world)["record"]["path"]).parent
    world.store.c("artifacts").delete_many({"kind": KIND})  # the dashboard on another computer cleared the records
    assert world.records.sweep() == 0 and folder.is_dir()  # too recent: it may be an attempt that is still being saved
    monkeypatch.setattr(stop_record, "ORPHAN_MINUTES", -1)
    assert world.records.sweep() == 1 and not folder.exists()


def test_a_backup_leaves_the_stop_record_out(site, world):
    _stop(site, world)
    resume = world.artifacts / "jobs" / world.job["_id"] / "resume.docx"
    resume.write_bytes(b"resume")
    out = world.tmp / "backup.tar.gz"
    manifest = backup(world.store, world.artifacts, world.tmp / "p", out)
    with tarfile.open(out) as tar:
        names = tar.getnames()
        dumped = tar.extractfile("db/artifacts.json").read().decode()
    assert f"artifacts/jobs/{world.job['_id']}/resume.docx" in names and not any("/stop/" in n or n.endswith("/stop") for n in names)
    kept = [p for p in world.artifacts.rglob("*") if p.is_file() and "stop" not in p.relative_to(world.artifacts).parts]
    assert KIND not in dumped and manifest["artifacts"] == len(kept) and resume in kept


def test_the_page_shows_the_record_as_text_with_a_notice_and_serves_files_by_hash(site, world):
    assert "stop-record" not in world.client.get("/applications/app_s").text  # nothing stopped yet, nothing shown
    world.store.c("applications").update_one({"_id": "app_s"}, {"$set": {"status": "handoff"}})
    empty = world.client.get("/applications/app_s").text
    assert 'id="stop-none"' in empty and "No record was kept for this stop." in empty  # state one: no record yet
    _stop(site, world)
    page = world.client.get("/applications/app_s").text
    assert "What the app saw when it stopped" in page and NOTICE in page and "passwords, verification codes and session cookies" in page
    assert "deleted when a new attempt starts" in page and "Apply &lt;now&gt; R-noisy" in page and "<now>" not in page
    assert "1 failed" in page and "1 refused by the app" in page and 'data-load="/applications/app_s/stop-record?part=1"' in page
    assert re.search(r'<details class="disclosure stop-net" id="stop-network" data-load="[^"]+">', page)  # closed until opened
    assert fx.QUERY_TOKEN not in page and fx.REPLY_MARKER not in page  # the rows are fetched only when the panel is opened
    arts = _arts(world)
    picture = world.client.get(f"/documents/{arts['view']['_id']}/download?kind=png&inline=1")
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/png" and picture.content[:4] == b"\x89PNG"
    panel = world.client.get("/applications/app_s/stop-record?part=1")
    assert panel.status_code == 200 and "no-store" in panel.headers["cache-control"] and "<html" not in panel.text
    text = panel.text
    assert f"token={fx.QUERY_TOKEN}&amp;next=%2Fa%3Fb" in text and fx.COOKIE_VALUE in text and fx.BODY_MARKER in text and fx.REPLY_MARKER in text
    assert "&lt;script&gt;window.hacked = 1&lt;/script&gt;" in text and "<script>window.hacked" not in text and "<img src=x" not in text
    assert f"{fx.CONSOLE_MARKER} &lt;b&gt;bold&lt;/b&gt;" in text and fx.PAGE_ERROR_MARKER in text
    assert 'id="net-problems"' in text and text.index("blocked.invalid") < text.index('id="net-all"')  # failed and refused rows come first
    assert "Showing the first 4.0 KB of" in text and "The whole body is in the network archive." in text
    assert "Not text, so it is not shown." in text and '<a href="http' not in text  # a captured address is never a link
    full = world.client.get("/applications/app_s/stop-record").text
    assert "<html" in full and NOTICE in full and fx.QUERY_TOKEN in full
    # Files are downloads, checked against their hash, and only for a signed-in person.
    got = world.client.get(f"/documents/{arts['html']['_id']}/download?kind=file")
    assert got.status_code == 200 and got.headers["content-disposition"].startswith("attachment") and got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-type"] == "application/octet-stream" and "no-store" in got.headers["cache-control"]
    assert world.client.get(f"/documents/{arts['html']['_id']}/download?kind=docx").status_code == 400
    assert world.client.get(f"/documents/{arts['html']['_id']}/download?kind=pdf").status_code == 400
    Path(arts["network"]["path"]).write_text('{"rows": []}')
    assert world.client.get(f"/documents/{arts['network']['_id']}/download?kind=file").status_code == 409
    assert 'id="net-elsewhere"' in world.client.get("/applications/app_s/stop-record?part=1").text  # a changed file is not shown either
    stranger = TestClient(create_app(world.rt), follow_redirects=False)
    assert stranger.get(f"/documents/{arts['html']['_id']}/download?kind=file").status_code in (303, 401)
    assert stranger.get("/applications/app_s/stop-record?part=1").status_code in (303, 401)
    listing = world.client.get("/documents").text + world.client.get(f"/jobs/{world.job['_id']}").text
    assert "page.html" not in listing and "attempt.har.zip" not in listing


def test_the_page_says_when_the_files_are_on_another_computer_and_when_nothing_was_requested(site, world):
    _stop(site, world, "quiet")
    panel = world.client.get("/applications/app_s/stop-record?part=1").text
    assert 'id="net-all"' in panel  # the page itself is one request
    arts = _arts(world)
    Path(arts["network"]["path"]).write_text(json.dumps({"rows": []}))
    world.store.c("artifacts").update_one({"_id": arts["network"]["_id"]}, {"$set": {"sha256": sha256_file(Path(arts["network"]["path"]))}})
    assert 'id="net-empty"' in world.client.get("/applications/app_s/stop-record?part=1").text  # state three: an empty list
    for art in arts.values():  # state two: the record is known, its files are on the computer that ran the attempt
        Path(art["path"]).unlink()
    page = world.client.get("/applications/app_s").text
    assert 'id="stop-elsewhere"' in page and "shot-missing" in page and "not on this one" in page and "data-load" not in page
    assert "not on this computer" in page and 'id="net-elsewhere"' in world.client.get("/applications/app_s/stop-record?part=1").text
    assert world.client.get(f"/documents/{arts['html']['_id']}/download?kind=file").status_code == 404


def test_nothing_captured_can_reach_a_model_or_the_tracker():
    src = Path(stop_record.__file__).resolve().parents[1]
    for name in ("core/ai.py", "core/skills.py", "records/sync.py", "records/tracker.py", "outreach/__init__.py"):
        path = src / name
        assert not path.exists() or "stop_record" not in path.read_text()
