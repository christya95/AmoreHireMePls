"""Jobs page: pasting several job links into "Add a job".

Sites, employers and jobs here are fictional. Nothing is fetched: a pasted link is saved, not opened."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.jobs_paste import EVENT, MAX_LINKS, MAX_PASTE_CHARS, banner, pasted_job_ids, short, split_links
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.test_copy import DENYLIST, visible_text

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
A, B, C = "https://careers.example-corp.example/jobs/101", "https://jobs.northwind.example/postings/202", "https://boards.fabrikam.example/roles/303"


# -- reading what was pasted --------------------------------------------------------------------------------


def test_links_are_read_one_per_line_or_separated_by_spaces_or_commas_in_any_mix():
    pasted = split_links(f"{A}\n\n  {B} , {C},{A}/apply\r\n<{B}>\n")
    assert pasted == {"links": [A, B, C, f"{A}/apply"], "unreadable": [], "left_out": 0, "too_long": False}
    assert split_links(A)["links"] == [A] and split_links(f"  {A}  ")["links"] == [A]
    # a comma inside an address stays part of it; a full stop after it does not
    assert split_links("https://jobs.example/search?ids=1,2 .")["links"] == ["https://jobs.example/search?ids=1,2"]
    assert split_links(f"See {A}.")["links"] == [A]


def test_repeats_blank_lines_and_plain_words_are_ignored():
    same = f"{A}\n{A}/\n{A}?utm_source=mail\nHTTPS://careers.example-corp.example/jobs/101#top"
    assert split_links(same)["links"] == [A]
    linkedin = "https://www.linkedin.com/jobs/view/4123456789 https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4123456789"
    assert len(split_links(linkedin)["links"]) == 1  # the same LinkedIn job id is one job
    words = split_links(f"Two for today:\n1: {A}\nand {B}")
    assert words["links"] == [A, B] and words["unreadable"] == []
    for nothing in ("", "   \n\n ", None, "none today"):
        assert split_links(nothing) == {"links": [], "unreadable": [], "left_out": 0, "too_long": False}


def test_entries_that_are_not_web_links_are_named_not_added():
    pasted = split_links(f"{A}\nftp://files.example/role.pdf\njavascript:alert(1) careers.example/jobs/7\nwww.example.org/x mailto:hiring@example.org\nhttps://\nhttps://bad host")
    assert pasted["links"] == [A]
    assert pasted["unreadable"] == [
        "ftp://files.example/role.pdf",
        "javascript:alert(1",
        "careers.example/jobs/7",
        "www.example.org/x",
        "mailto:hiring@example.org",
        "https://",
        "https://bad",
    ]
    assert split_links("http://localhost:8080/jobs/1 http://127.0.0.1:8080/jobs/2")["unreadable"] == []
    assert split_links("https://" + "a" * 2100 + ".example/x")["links"] == []
    assert short("https://www.example.org/careers/1") == "example.org/careers/1" and short("ftp://files.example/a") == "ftp://files.example/a"
    long_name = short("https://careers.example/" + "x" * 80)
    assert len(long_name) == 48 and long_name.endswith("…")


def test_a_paste_is_capped_and_says_how_many_were_left():
    many = "\n".join(f"https://jobs.example/role/{n}" for n in range(1, MAX_LINKS + 13))
    pasted = split_links(many)
    assert len(pasted["links"]) == MAX_LINKS and pasted["left_out"] == 12 and pasted["links"][-1].endswith(f"/{MAX_LINKS}")
    assert split_links("x" * (MAX_PASTE_CHARS + 1)) == {"links": [], "unreadable": [], "left_out": 0, "too_long": True}


# -- the banner -----------------------------------------------------------------------------------------------


def test_each_outcome_has_its_own_sentence():
    assert banner({"added": 3}) == {"tone": "ok", "text": "3 jobs were added and checked against your rules. They are marked New in the list."}
    assert banner({"added": 1})["text"] == "1 job was added and checked against your rules. It is marked New in the list."
    mixed = banner({"added": 3, "known": 2, "failed": ["files.example/role.pdf"], "hint": True})
    assert mixed["tone"] == "warn"
    assert mixed["text"] == (
        "3 jobs were added and checked against your rules. They are marked New in the list. 2 were already in your list. "
        "1 link could not be read: files.example/role.pdf. A link starts with https://."
    )
    assert banner({"added": 1, "known": 1})["text"].endswith("1 was already in your list.") and banner({"added": 1, "known": 1})["tone"] == "ok"
    assert banner({"known": 4}) == {"tone": "ok", "text": "All 4 jobs were already in your list. Nothing was added."}
    assert banner({"known": 1})["text"] == "That job was already in your list. Nothing was added."
    none = banner({"failed": ["a.example/1", "b.example/2", "c.example/3", "d.example/4"], "failed_count": 5, "hint": True})
    assert none["tone"] == "bad"
    assert none["text"] == "None of the 5 links could be read: a.example/1, b.example/2, c.example/3 and 2 more. A link starts with https://. Nothing was added."
    assert (
        banner({"failed": ["ftp://files.example/a"], "hint": True})["text"] == "That link could not be read: ftp://files.example/a. A link starts with https://. Nothing was added."
    )
    assert banner({"known": 2, "failed": ["a.example/1", "b.example/2"]}) == {
        "tone": "warn",
        "text": "2 were already in your list. 2 links could not be read: a.example/1 and b.example/2. Nothing was added.",
    }
    empty = banner({})
    assert empty["tone"] == "bad" and empty["text"] == "No link was found in what you pasted. Paste one or more links that start with https://. Nothing was added."
    assert banner({"too_long": True}) == {"tone": "bad", "text": f"That was too much text to read as links. Paste up to {MAX_LINKS} links at a time. Nothing was added."}
    capped = banner({"added": MAX_LINKS, "left_out": 12})
    assert capped["tone"] == "warn" and capped["text"].endswith(f"Only the first {MAX_LINKS} links were read. 12 more were left out, so paste those again.")
    assert banner({"added": MAX_LINKS, "left_out": 1})["text"].endswith("1 more was left out, so paste it again.")
    extras = banner({"added": 2, "extras_ignored": True})
    assert extras["tone"] == "warn" and extras["text"].endswith("The employer, title, job ID, location and ad text were not used, because they fit one link only.")
    for said in (mixed, none, empty, capped, extras):
        assert not [pat for pat in DENYLIST if re.search(pat, said["text"], flags=re.I)] and "—" not in said["text"] and " - " not in said["text"]


def test_the_latest_paste_keeps_the_new_mark_until_the_next_check_ends_and_a_day_at_most():
    paste = {"created_at": NOW - timedelta(hours=2), "data": {"new_job_ids": ["job_a", "job_b"]}}
    later = {"created_at": NOW - timedelta(minutes=5), "data": {"new_job_ids": ["job_c"]}}
    nothing_added = {"created_at": NOW - timedelta(minutes=1), "data": {"new_job_ids": [], "known": 1}}
    assert pasted_job_ids([paste], None, NOW) == {"job_a", "job_b"}
    assert pasted_job_ids([nothing_added, later, paste], None, NOW) == {"job_c"}  # a paste that added nothing clears no mark
    assert pasted_job_ids([paste], {"status": "completed", "finished_at": NOW - timedelta(hours=3)}, NOW) == {"job_a", "job_b"}
    assert pasted_job_ids([later, paste], {"status": "completed", "finished_at": NOW}, NOW) == set()
    assert pasted_job_ids([{"created_at": NOW - timedelta(hours=25), "data": {"new_job_ids": ["job_old"]}}], None, NOW) == set()
    assert pasted_job_ids([{"created_at": NOW, "data": {}}], None, NOW) == set()


# -- the page and the route -----------------------------------------------------------------------------------


@pytest.fixture
def client(store, tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("adding a job link must not open any site")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    s = Settings(
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
    return TestClient(create_app(Runtime(s, store=store)), follow_redirects=False)


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def _paste(client, csrf, text, **extra):
    r = client.post("/jobs/import", data={"csrf": csrf, "url": text, **extra})
    assert r.status_code == 303
    return r.headers["location"]


def _flash(page: str) -> tuple[str, str]:
    m = re.search(r'<div class="flash ([a-z]*)">(.*?)</div>', page, flags=re.S)
    return (m.group(1), m.group(2)) if m else ("", "")


@pytest.mark.mongo
def test_the_form_takes_several_links_and_needs_the_form_token(client):
    csrf = _login(client)
    page = client.get("/jobs").text
    assert re.search(r'<textarea id="url" name="url"[^>]*required', page) and f"up to {MAX_LINKS} at once" in page
    assert "One link per line, or separated by spaces or commas." in page and 'data-busy="Adding…"' in page
    assert client.post("/jobs/import", data={"csrf": "wrong", "url": A}).status_code == 403
    assert client.post("/jobs/import", data={"url": A}).status_code == 422
    assert client.app.state.rt.store.c("jobs").count_documents({}) == 0
    signed_out = TestClient(client.app, follow_redirects=False).post("/jobs/import", data={"csrf": csrf, "url": A})
    assert signed_out.status_code == 303 and signed_out.headers["location"] == "/login"


@pytest.mark.mongo
def test_one_link_works_as_before_and_keeps_the_pasted_ad_text(client):
    csrf = _login(client)
    store = client.app.state.rt.store
    text = "Permanent role selling to mid sized companies. " * 8
    where = _paste(client, csrf, A, employer="Example Corp", title="Account Executive", requisition_id="R101", location="Lakeside", posting_text=text)
    job = store.c("jobs").find_one({})
    assert where == f"/jobs/{job['_id']}" and client.get(where).status_code == 200
    assert (job["employer"], job["title"], job["requisition_id"], job["location"]) == ("Example Corp", "Account Executive", "R101", "Lakeside")
    assert job["latest_snapshot"]["text"] == text.strip() and job["status"] != "new" and job.get("qualified_at")
    # the same link again opens the same job, and it is the row marked New on the list
    assert _paste(client, csrf, f"  {A}\n") == where and store.c("jobs").count_documents({}) == 1
    fragment = client.get("/jobs/list").text
    assert fragment.count(" is-new") == 1 and "Example Corp" in fragment


@pytest.mark.mongo
def test_several_links_are_added_in_one_go_and_the_banner_says_what_happened(client):
    csrf = _login(client)
    store = client.app.state.rt.store
    _paste(client, csrf, A, employer="Example Corp", title="Account Executive")
    before = store.c("jobs").find_one({})
    linkedin = "https://www.linkedin.com/jobs/view/4123456789"
    where = _paste(client, csrf, f"{A}\n{B}, {C}\n{linkedin} {B}\nftp://files.example/role.pdf\n", employer="Northwind", posting_text="Ignored for several links.")
    assert re.fullmatch(r"/jobs\?pasted=paste_[0-9a-f]{16}", where)
    assert store.c("jobs").count_documents({}) == 4
    page = client.get(where).text
    tone, text = _flash(page)
    assert tone == "warn" and text == (
        "3 jobs were added and checked against your rules. They are marked New in the list. 1 was already in your list. "
        "1 link could not be read: ftp://files.example/role.pdf. A link starts with https://. "
        "The employer, title, job ID, location and ad text were not used, because they fit one link only."
    )
    assert page.count(" is-new") == 3 and page.count('<span class="pill accent new-mark">New</span>') == 3
    # a job known only by its link shows the link, so three new rows are not three identical lines
    assert "Unknown title · jobs.northwind.example/postings/202" in page and "Unknown title · boards.fabrikam.example/roles/303" in page
    assert "Unknown title · 4123456789" in page and "Account Executive · careers" not in page
    assert not [pat for pat in DENYLIST if re.search(pat, visible_text(page), flags=re.I)]
    added = {j["posting_url"]: j for j in store.c("jobs").find({"_id": {"$ne": before["_id"]}})}
    assert set(added) == {B, C, linkedin}
    assert all(j["employer"].startswith("Unknown employer") and j["latest_snapshot"] is None and j.get("qualified_at") for j in added.values())
    # LinkedIn: the job id is kept from the link and the site is not contacted (the fixture fails on any request)
    assert added[linkedin]["linkedin_job_id"] == "4123456789" and added[linkedin]["apply_route"] == "linkedin" and added[linkedin]["dedup_keys"] == ["li:4123456789"]
    # the job that was already there was left exactly as it was: same place in the list, same status
    after = store.c("jobs").find_one({"_id": before["_id"]})
    assert after["updated_at"] == before["updated_at"] and after["status"] == before["status"] and after["employer"] == "Example Corp"
    event = store.c("audit").find_one({"event": EVENT, "subject_id": where.rsplit("=", 1)[1]})
    assert event["data"]["added"] == 3 and event["data"]["known"] == 1 and len(event["data"]["new_job_ids"]) == 3
    # a banner address that was not issued shows nothing, and an unknown one does not fail the page
    assert _flash(client.get("/jobs?pasted=paste_0000000000000000").text) == ("", "") and client.get("/jobs?pasted=x").status_code == 200


@pytest.mark.mongo
def test_empty_all_known_all_unreadable_and_too_many_each_say_so(client):
    csrf = _login(client)
    store = client.app.state.rt.store

    def said(text, **extra):
        return _flash(client.get(_paste(client, csrf, text, **extra)).text)

    assert said("  \n ") == ("bad", "No link was found in what you pasted. Paste one or more links that start with https://. Nothing was added.")
    assert client.post("/jobs/import", data={"csrf": csrf}).headers["location"].startswith("/jobs?pasted=")
    assert said("ftp://files.example/a careers.example/jobs/7") == (
        "bad",
        "None of the 2 links could be read: ftp://files.example/a and careers.example/jobs/7. A link starts with https://. Nothing was added.",
    )
    assert store.c("jobs").count_documents({}) == 0
    assert said(f"{A} {B}")[0] == "ok" and store.c("jobs").count_documents({}) == 2
    assert said(f"{B}\n{A}") == ("ok", "All 2 jobs were already in your list. Nothing was added.")
    # one good link next to one that cannot be read: the job is added, and the list says what was wrong
    tone, text = said(f"{C} ftp://files.example/a", employer="Fabrikam")
    assert tone == "warn" and text.startswith("1 job was added and checked against your rules. It is marked New in the list. 1 link could not be read")
    assert store.c("jobs").find_one({"posting_url": C})["employer"] == "Fabrikam"
    many = "\n".join(f"https://jobs.example/role/{n}" for n in range(1, MAX_LINKS + 3))
    tone, text = said(many)
    assert tone == "warn" and text.startswith(f"{MAX_LINKS} jobs were added") and "Only the first 50 links were read. 2 more were left out, so paste those again." in text
    assert store.c("jobs").count_documents({}) == 3 + MAX_LINKS
    assert said("x" * (MAX_PASTE_CHARS + 1))[0] == "bad"
    # the marks are the latest paste that added jobs, and they go when a check for new jobs ends
    assert client.get("/jobs/list").text.count(" is-new") == MAX_LINKS
    store.c("runs").insert_one(
        {"_id": "run_check", "kind": "discovery_request", "status": "completed", "created_at": datetime.now(UTC), "finished_at": datetime.now(UTC) + timedelta(seconds=1)}
    )
    assert " is-new" not in client.get("/jobs/list").text


@pytest.mark.mongo
def test_a_link_that_cannot_be_saved_is_named_and_the_others_are_still_added(client, monkeypatch):
    from amorehiremepls.jobs.discovery import Discovery

    csrf = _login(client)
    real = Discovery.import_link

    def flaky(self, url, *args, **kwargs):
        if "northwind" in url:
            raise RuntimeError("write failed")
        return real(self, url, *args, **kwargs)

    monkeypatch.setattr(Discovery, "import_link", flaky)
    tone, text = _flash(client.get(_paste(client, csrf, f"{A}\n{B}\n{C}")).text)
    assert tone == "warn" and "2 jobs were added" in text and "1 link could not be read: jobs.northwind.example/postings/202." in text and "https://." not in text
    assert client.app.state.rt.store.c("jobs").count_documents({}) == 2
    # one link alone that cannot be saved comes back to the list with the reason instead of an error page
    tone, text = _flash(client.get(_paste(client, csrf, B)).text)
    assert tone == "bad" and text == "That link could not be read: jobs.northwind.example/postings/202. Nothing was added."
