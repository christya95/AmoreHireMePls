"""The application page as one flow: every state names one next action, and the page says it the same way.

`STATES` in `web/app_flow.py` is the state → action table. The first tests walk every row without a database;
the rest render the page for a handful of saved applications. All data here is fictional."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amorehiremepls.web.app_flow import KIND_LABEL, NOTHING_NEEDED, OPEN_SITE, STATES, STEPS, attempts, finish_need, flow_key, flow_steps, site_status
from amorehiremepls.web.chrome import needed_now
from tests.test_web import _login, client  # noqa: F401  (the fixture)

ROOT = Path(__file__).resolve().parents[1]
NEED_FLAGS = ("can_fill", "can_retry", "can_clear", "working", "can_cancel", "stale", "can_run", "can_retry_same")


def _walk(state):
    flags = state.flags
    shown = str(state.app["status"])
    need = needed_now(state.app, shown, state.hold, signin_fix=state.fix, **{name: bool(flags.get(name)) for name in NEED_FLAGS})
    need = finish_need(
        need,
        state.app,
        {},
        show_status=shown,
        working=bool(flags.get("working")),
        live=bool(flags.get("live")),
        allowed=flags.get("allowed", True),
        can_clear=bool(flags.get("can_clear")),
        hold=state.hold,
    )
    steps = flow_steps(state.app, shown, need, state.hold, working=bool(flags.get("working")), final_noted=bool(flags.get("final_noted")), live=bool(flags.get("live")))
    return need, steps


@pytest.mark.parametrize("state", STATES, ids=[s.name for s in STATES])
def test_every_state_names_one_next_action(state):
    need, steps = _walk(state)
    assert (need["kind"], need["label"]) == (state.kind, state.label)
    assert need["waits_on_you"] is state.waits
    if state.waits:
        # Stopped and waiting on the person: the page says what to do, with a control, never "nothing is needed".
        assert need["title"] != NOTHING_NEEDED and need["label"]
    if state.app["status"] == "handoff":
        # The headline label never tells the person to finish on the site while the button offers to try again.
        assert bool(need["status_label"]) == (need["kind"] not in ("site", "manual")), state.name
    if state.app["status"] in ("handoff", "failed", "needs_verification"):
        assert need["title"] != NOTHING_NEEDED and need["text"], "a stop always says what happened and what is next"
    # One step carries the marker, and it says whose turn it is.
    marked = [row for row in steps if row["state"] in ("current", "you", "stopped")]
    done_all = all(row["state"] == "done" for row in steps)
    assert len(marked) == 1 or done_all or (state.app["status"] == "authorized" and not marked)
    if state.waits and marked:
        assert marked[0]["state"] in ("you", "stopped")
    if state.app["status"] in ("handoff", "failed"):
        assert marked and marked[0]["state"] == "stopped" and marked[0]["note"] == "stopped here"


def test_recording_a_manual_application_is_never_the_main_button_for_an_unsent_one():
    for state in STATES:
        need, _ = _walk(state)
        clicked = bool((state.app.get("evidence") or {}).get("final_clicked"))
        if need["kind"] == "manual":
            assert clicked, state.name
        if state.app["status"] == "handoff" and not clicked:
            assert need["manual_offer"] and need["kind"] != "manual", state.name
        if need["kind"] == "site":
            assert need["label"] == OPEN_SITE and "under More" in need["text"]


def test_every_stop_has_its_own_sentence():
    titles = {}
    for state in STATES:
        if state.app["status"] not in ("handoff", "failed"):
            continue
        need, _ = _walk(state)
        titles.setdefault(need["title"], []).append(state.name)
    shared = {title: names for title, names in titles.items() if len(names) > 1}
    # Two pairs share a headline on purpose: the same thing to say, a different control state behind it.
    assert set(shared) <= {"The app can try again.", "jobs.example.test did not accept the saved login."}, shared


def test_tracker_leaves_out_steps_that_did_not_come_up():
    plain = flow_steps({"status": "executing"}, "executing", {"kind": ""}, None, working=True, live=True)
    assert [row["key"] for row in plain] == ["prepared", "approved", "opening", "filling", "sending", "confirmed"]
    hold = {"purpose": "field", "field_label": "Verification code", "signin": {"host": "jobs.example.test", "step": "code"}}
    code = flow_steps({"status": "executing"}, "executing", {"kind": "field"}, hold, live=True)
    assert [row["key"] for row in code] == ["prepared", "approved", "opening", "signin", "code", "filling", "sending", "confirmed"]
    assert [row["state"] for row in code] == ["done", "done", "done", "done", "you", "todo", "todo", "todo"]
    sent = flow_steps({"status": "submitted"}, "submitted", {"kind": ""})
    assert all(row["state"] == "done" for row in sent) and sent[-1]["key"] == "confirmed"
    assert {key for key, _ in STEPS} >= {row["key"] for row in code}
    assert flow_key(code, {"kind": "field"}) != flow_key(plain, {"kind": ""})


def test_attempts_are_numbered_oldest_first_and_listed_newest_first():
    now = datetime.now(UTC)
    runs = [{"_id": f"r{i}", "created_at": now - timedelta(hours=3 - i), "status": "failed" if i < 2 else "running"} for i in range(3)]
    rows = attempts(runs, lambda run: f"line {run['_id']}")
    assert [(row["n"], row["how"], row["live"]) for row in rows] == [(3, "line r2", True), (2, "line r1", False), (1, "line r0", False)]


class _Memory:
    def __init__(self, last=None, asked=False):
        self._last, self._asked = last, asked

    def last_success(self, host):
        return self._last

    def asked(self, host):
        return self._asked


def test_site_status_says_where_the_form_is_and_what_is_known_about_signing_in():
    def local(value, fmt):
        return value.strftime(fmt)

    same = {"review_package": {"apply_url": "https://jobs.example.test/apply/1", "posting_url": "https://jobs.example.test/1"}}
    assert site_status(_Memory(), same, {}, True, local) is None  # nothing known, nothing said
    away = {"review_package": {"apply_url": "https://careers.other.test/apply/1", "posting_url": "https://jobs.example.test/1"}}
    seen = site_status(_Memory(), away, {}, True, local)
    assert seen["elsewhere"] and "careers.other.test, not on the site where the job is posted" in seen["text"]
    known = site_status(_Memory(last=datetime(2026, 11, 12, 9, 30, tzinfo=UTC)), same, {}, True, local)
    assert "last signed in to jobs.example.test on 2026-11-12 09:30" in known["text"] and "asks you to allow 1Password first" in known["text"]
    # A site that stopped an earlier application for want of an account is named before approval.
    need = site_status(_Memory(), same, {}, True, local, "account_needed")
    assert need["warn"] and "only offered sign up. The app does not create accounts." in need["text"]
    none = site_status(_Memory(), same, {}, True, local, "no_login")
    assert none["warn"] and "no saved login for it was found in 1Password" in none["text"]
    assert not site_status(_Memory(last=datetime(2026, 11, 12, 9, 30, tzinfo=UTC)), same, {}, True, local, "no_login")["warn"]  # a later sign-in worked
    off = site_status(_Memory(asked=True), same, {}, False, local)
    assert "no sign-in there has worked yet" in off["text"] and "is off under Settings" in off["text"]


def test_the_table_in_the_docs_lists_every_state():
    text = (ROOT / "docs" / "ui-copy.md").read_text()
    for state in STATES:
        label = state.label or "none"
        assert f"| {state.name} | {label} |" in text, state.name
    assert set(KIND_LABEL) >= {state.kind for state in STATES if state.kind and state.kind not in ("signin", "field", "manual")}


# -- the page ---------------------------------------------------------------------------------------------
pytestmark_page = pytest.mark.mongo


def _seed(rt, app_id="app_flow", job_id="job_flow", **app):
    now = datetime.now(UTC)
    url = "https://boards.greenhouse.io/northwind/jobs/7"
    rt.store.c("jobs").insert_one(
        {
            "_id": job_id,
            "employer": "Northwind",
            "employer_norm": "northwind",
            "title": "Account Executive",
            "status": "qualified",
            "posting_url": url,
            "apply_url": url,
            "sources": ["manual"],
            "created_at": now,
            "updated_at": now,
        }
    )
    doc = {
        "_id": app_id,
        "job_id": job_id,
        "status": "handoff",
        "provenance": {"origin": "local"},
        "material_hash": "pkg-flow",
        "evidence": {"steps_done": 0, "final_clicked": False},
        "review_package": {"apply_url": url, "posting_url": url, "answers": [], "outstanding": [], "gaps": []},
        "history": [{"at": now - timedelta(hours=2), "status": "authorized"}],
        "created_at": now,
        "updated_at": now,
    }
    doc.update(app)
    rt.store.c("applications").insert_one(doc)
    return app_id


def _block(text: str) -> str:
    return text.split('id="activity-banner"', 1)[1].split("</section>", 1)[0]


@pytest.mark.mongo
def test_a_blocked_stop_opens_the_job_site_and_keeps_the_manual_record_apart(client):  # noqa: F811
    csrf = _login(client)
    rt = client.app.state.rt
    app_id = _seed(rt, handoff_kind="blocked", reason="The job site blocked this browser.")
    page = client.get(f"/applications/{app_id}").text
    block = _block(page)
    assert "The job site blocked this browser." in block and "Nothing was sent." in block and NOTHING_NEEDED not in block
    assert 'id="open-site"' in block and 'href="https://boards.greenhouse.io/northwind/jobs/7"' in block and 'rel="noopener noreferrer"' in block
    assert "I applied on the site</button>" not in block and f'action="/applications/{app_id}/manual"' not in block
    assert 'class="is-stopped" data-step="opening"' in block and "stopped here" in block and "Finish on the site yourself" in block
    # The record form is under More, behind a tick, and the server refuses a request without it.
    more = page.split('id="app-more"', 1)[1].split("</nav>", 1)[0]
    assert "I applied on the site" in more and 'name="sent_myself" value="yes" required' in more and "It marks this application as sent." in more
    form = {"csrf": csrf, "confirmation": "Thanks for applying", "application_date": "2026-11-12"}
    assert client.post(f"/applications/{app_id}/manual", data=form).status_code == 400
    assert rt.store.c("applications").find_one({"_id": app_id})["status"] == "handoff"
    # Order: the status, then the job, then what would be sent, and the technical record last.
    marks = ['id="activity-banner"', 'id="app-more"', 'id="sent"', 'id="authorize"', 'id="updates"', 'id="gaps"', 'id="record"']
    places = [page.index(mark) for mark in marks]
    assert places == sorted(places)


@pytest.mark.mongo
def test_a_new_attempt_shows_its_number_and_the_tracker_follows_it(client):  # noqa: F811
    _login(client)
    rt = client.app.state.rt
    app_id = _seed(rt, status="authorized", evidence={})
    old = rt.store.create_run("application", {"job_id": "job_flow", "application_id": app_id})
    rt.store.run_event(old["_id"], "The job site asked you to sign in.")
    rt.store.update_run(old["_id"], status="failed", finished_at=datetime.now(UTC))
    rt.store.c("runs").update_one({"_id": old["_id"]}, {"$set": {"created_at": datetime.now(UTC) - timedelta(hours=1)}})
    rt.store.create_run("application", {"job_id": "job_flow", "application_id": app_id})
    page = client.get(f"/applications/{app_id}").text
    block = _block(page)
    assert 'id="flow-attempt">Attempt 2 · started ' in block
    assert 'class="is-current" data-step="opening"' in block
    assert 'id="attempts"' in page and "<strong>Attempt 1</strong>" in page and "The job site asked you to sign in." in page
    data = client.get(f"/applications/{app_id}/activity").json()
    assert data["attempt"].startswith("Attempt 2 · started ") and data["flow"].endswith("#") and data["ask"] == {"kind": "", "title": NOTHING_NEEDED, "waits": False}
    assert [row["key"] for row in data["steps"] if row["state"] == "current"] == ["opening"]


@pytest.mark.mongo
def test_a_code_hold_sits_in_the_status_block_with_its_time_limit(client):  # noqa: F811
    _login(client)
    rt = client.app.state.rt
    app_id = _seed(rt, status="executing", evidence={})
    run = rt.store.create_run("application", {"job_id": "job_flow", "application_id": app_id})
    rt.store.update_run(run["_id"], status="running")
    now = datetime.now(UTC)
    ask = {"ask_id": "ask_flow", "host": "boards.greenhouse.io", "step": "code"}
    rt.store.c("human_holds").insert_one(
        {
            "_id": "hold_flow",
            "application_id": app_id,
            "job_id": "job_flow",
            "status": "holding",
            "purpose": "field",
            "field_label": "Verification code",
            "field_reason": "<b>boards.greenhouse.io</b> sent a code to your email.",
            "signin": ask,
            "started_at": now,
            "heartbeat_at": now,
            "expires_at": now + timedelta(minutes=10),
        }
    )
    page = client.get(f"/applications/{app_id}").text
    block = _block(page)
    assert 'data-waits="1"' in page.split('id="activity-banner"', 1)[0][-400:] + block[:200]
    assert 'class="is-you" data-step="code"' in block and "waiting on you" in block
    assert 'class="code-input"' in block and "autofocus" in block and '<button class="lg">Use this code</button>' in block
    assert 'id="hold-limit">The app waits for you until ' in block
    assert "<b>boards.greenhouse.io</b>" not in block and "&lt;b&gt;boards.greenhouse.io&lt;/b&gt;" in block  # a site's words are text
    data = client.get(f"/applications/{app_id}/activity").json()
    assert data["ask"]["kind"] == "field" and data["ask"]["waits"] is True


@pytest.mark.mongo
def test_the_page_says_before_approval_that_the_site_has_asked_to_sign_in(client):  # noqa: F811
    _login(client)
    rt = client.app.state.rt
    app_id = _seed(rt, status="ready_for_review", evidence={})
    assert 'id="site-signin"' not in client.get(f"/applications/{app_id}").text
    now = datetime.now(UTC)
    rt.store.c("site_signins").insert_one(
        {"_id": "signin_flow", "host": "boards.greenhouse.io", "domain": "greenhouse.io", "fingerprint": "f", "status": "signed_in", "started_at": now, "finished_at": now}
    )
    page = client.get(f"/applications/{app_id}").text
    note = page.split('id="site-signin"', 1)[1].split("</p>", 1)[0]
    assert "The app last signed in to boards.greenhouse.io on " in note
    assert 'class="is-you" data-step="approved"' in _block(page) and "Approve this exact application" in _block(page)
    # Another application stopped on this site because no login was saved: said here, before approval, until a sign-in works.
    rt.store.c("site_signins").delete_many({})
    _seed(rt, app_id="app_other", job_id="job_other", handoff_kind="login", evidence={"signin": {"host": "boards.greenhouse.io", "outcome": "no_login"}})
    warned = client.get(f"/applications/{app_id}").text.split('id="site-signin"', 1)
    assert "site-note warn" in warned[0][-60:] and "no saved login for it was found in 1Password" in warned[1].split("</p>", 1)[0]
    rt.store.c("site_signins").insert_one(
        {"_id": "signin_flow2", "host": "boards.greenhouse.io", "domain": "greenhouse.io", "fingerprint": "f", "status": "signed_in", "started_at": now, "finished_at": now}
    )
    settings = client.get("/settings").text
    sites = settings.split('id="signin-sites"', 1)[1].split("</div>\n  </div>", 1)[0]
    assert "greenhouse.io" in sites and "Signed in" in sites
