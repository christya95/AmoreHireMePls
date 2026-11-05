"""Jargon lint: every page is rendered with fixture data and must not show raw technical terms outside the places
reserved for them (muted `.chip`/`.raw` chips, `<code>`, `<details>`, `<pre>`). The plain -> technical map lives in
docs/ui-copy.md and the wording itself in ui_settings.py."""

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

pytestmark = pytest.mark.mongo

# Words a non-developer should never meet in running text. "ATS" is matched as a whole word, "scope" as a whole word
# so that words like "microscope" or "telescope" (not that we use them) do not trip it.
DENYLIST = [
    r"requisition",
    r"idempoten",
    r"checkpoint",
    r"outbox",
    r"artifact",
    r"\bscopes?\b",
    r"material hash",
    r"handoff",
    r"hand-off",
    r"qualification",
    r"\bATS\b",
    r"\bCSRF\b",
    r"provenance",
    r"adapter",
    r"heartbeat",
    r"deterministic",
    r"reconcil",
    r"\bSMTP\b",
    r"\bpayload\b",
    r"\bworker\b",
    r"\b(?:emp|fact)_[0-9a-f]{8,}\b",
]

# Elements whose content is allowed to carry technical terms: raw chips, code, collapsed details, preformatted dumps,
# scripts/styles and SVG symbol definitions.
STRIP = [
    r"<script\b.*?</script>",
    r"<style\b.*?</style>",
    r"<svg\b.*?</svg>",
    r"<pre\b.*?</pre>",
    r"<code\b.*?</code>",
    r"<details\b.*?</details>",
    r"<[^>]*\bclass=\"[^\"]*\b(?:chip|raw)\b[^\"]*\"[^>]*>.*?</(?:span|code|small)>",
]


def visible_text(html: str) -> str:
    out = html
    for pat in STRIP:
        out = re.sub(pat, " ", out, flags=re.S | re.I)
    # attribute values are not shown either, except title/placeholder/aria-label/alt which users do read
    kept = re.findall(r'(?:title|placeholder|aria-label|alt)="([^"]*)"', out)
    out = re.sub(r"<[^>]+>", " ", out)
    return out + " " + " ".join(kept)


@pytest.fixture
def client(store, tmp_path):
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
    rt = Runtime(s, store=store)
    return TestClient(create_app(rt), follow_redirects=False)


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)


def _seed(client, tmp_path) -> dict[str, str]:
    """Real flow where it is cheap (import, run, worker tick, approval, recipient, draft) plus direct inserts for the
    rarer states so every page has at least one row of each kind."""
    from amorehiremepls.core.worker import Worker
    from tests.fixtures.make_resume import make_base_resume
    from tests.fixtures.profile_fixture import build_test_profile
    from tests.test_profile import FAKE_APPLY, FAKE_FACTS, FAKE_SALARY

    csrf = _login(client)
    rt = client.app.state.rt
    rt.profiles.save(build_test_profile(FAKE_FACTS, FAKE_SALARY, apply_text=FAKE_APPLY), actor="test")
    rt.profiles.select_base_resume(make_base_resume(tmp_path / "base.docx"), "2026-10-01", "test", rt.settings.private_dir)
    rt.reload_profile()
    data = {
        "csrf": csrf,
        "url": "https://careers.acme.example/jobs/R91",
        "employer": "Acme",
        "title": "Backend Developer",
        "requisition_id": "R91",
        "location": "Toronto, Ontario",
        "posting_text": "Permanent backend developer, Java, Toronto. CAD 110,000 to 130,000.",
    }
    job_id = client.post("/jobs/import", data=data).headers["location"].rsplit("/", 1)[1]
    run_id = client.post(f"/jobs/{job_id}/run", data={"csrf": csrf}).headers["location"].rsplit("/", 1)[1]
    Worker(rt).tick()
    run = rt.store.c("runs").find_one({"_id": run_id})
    app_id = run["interrupt"]["application_id"]
    a = rt.store.c("applications").find_one({"_id": app_id})
    assert client.post(f"/applications/{app_id}/authorize", data={"csrf": csrf, "material_hash": a["material_hash"], "hours": 1}).status_code == 303
    Worker(rt).tick()  # -> handoff (no adapter for this host)
    client.post("/authorizations/batch", data={"csrf": csrf, "kind": "application_submit", "employers": "Acme", "max_fit_rank": 2, "max_count": 2, "hours": 2})
    client.post("/outreach/recipients", data={"csrf": csrf, "name": "Sam Recruiter", "employer": "Acme", "profile_url": "https://linkedin.com/in/sam-r", "remit": "TA"})
    rid = rt.store.c("recipients").find_one({})["_id"]
    client.post(f"/outreach/recipients/{rid}/draft", data={"csrf": csrf, "kind": "connection_request", "applied_role": ""})
    now = datetime.now(UTC)
    # a second, submitted application with a sync item, an unclear one, and an imported record
    job2 = {"_id": "job_lint2", "employer": "Globex", "title": "Platform Engineer", "status": "applied", "posting_url": "https://x.example/2", "sources": ["manual"]}
    rt.store.c("jobs").insert_one({**job2, "created_at": now, "updated_at": now})
    for i, (status, extra) in enumerate((("submitted", {"application_date": "2026-11-01"}), ("needs_verification", {}))):
        rt.store.c("applications").insert_one(
            {
                "_id": f"app_lint{i}",
                "job_id": "job_lint2",
                "status": status,
                "provenance": {"origin": "local"},
                "material_hash": "m",
                "review_package": {"apply_url": "https://x.example/apply", "posting_url": "https://x.example/2", "answers": [], "gaps": []},
                "history": [],
                "created_at": now,
                "updated_at": now,
                **extra,
            }
        )
    rt.sync.outbox.enqueue("app_lint0")
    rt.store.c("applications").insert_one(
        {
            "_id": "app_lint_import",
            "job_id": "job_lint2",
            "status": "submitted",
            "provenance": {"origin": "tracker_import"},
            "tracker_record_id": "rec-1",
            "record": {"employer": "Globex", "role": "Platform Engineer", "application_date": "2025-01-02", "date_basis": "recorded", "evidence_basis": "email receipt"},
            "artifact_status": [{"kind": "resume", "hash_ok": True}],
            "sync": {"state": "synced"},
            "history": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    usage = {"run_id": run_id, "skill": "rank_fit@1", "model": "fake", "input_tokens": 10, "output_tokens": 5, "cached_tokens": 0}
    rt.store.c("usage").insert_one({**usage, "cost_usd": None, "cost_known": False, "latency_ms": 12, "created_at": now})
    # Stored fit text cites internal ids. The pages must show the sentence without the id.
    job = rt.jobs.get(job_id)
    rt.jobs.set_status(
        job_id,
        job["status"],
        fit={
            "rank": 3,
            "fit_points": ["Has current Software Developer experience at Big Bank (emp_0f1e2d3c4b5a6978)."],
            "genuine_gaps": ["No evidence of a fictional tool (fact_00aa11bb22cc33dd)."],
            "missing_evidence": ["on-call rotation"],
        },
    )
    rt.store.c("applications").update_one({"_id": app_id}, {"$set": {"review_package.gaps": ["No evidence of a fictional tool (fact_00aa11bb22cc33dd)."]}})
    return {"csrf": csrf, "job": job_id, "app": app_id, "run": run_id, "unclear": "app_lint1"}


def test_every_page_is_free_of_jargon(client, tmp_path):
    ids = _seed(client, tmp_path)
    pages = [
        "/",
        "/jobs",
        f"/jobs/{ids['job']}",
        "/jobs?status_filter=needs_review",
        "/applications",
        "/applications?status_filter=submitted",
        f"/applications/{ids['app']}",
        f"/applications/{ids['unclear']}",
        "/runs",
        f"/runs/{ids['run']}",
        "/documents",
        "/outreach",
        "/history",
        "/sync",
        "/usage",
        "/authorizations",
        "/profile",
        "/settings",
        "/help",
    ]
    problems = []
    for path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        text = visible_text(r.text)
        for pat in DENYLIST:
            for m in re.finditer(pat, text, flags=re.I):
                snippet = text[max(0, m.start() - 40) : m.end() + 40].replace("\n", " ")
                problems.append(f"{path}: /{pat}/ in '…{' '.join(snippet.split())}…'")
    # the signed-out page too
    client.post("/logout", data={"csrf": ids["csrf"]})
    login = visible_text(client.get("/login").text)
    for pat in DENYLIST:
        if re.search(pat, login, flags=re.I):
            problems.append(f"/login: /{pat}/")
    assert not problems, "\n".join(problems)


def test_step_names_and_glossary_are_plain():
    from amorehiremepls.web.ui_settings import GLOSSARY, NEXT_ACTIONS, PAGE_INTROS, STATUS_LABELS, STEP_HELP

    assert list(STEP_HELP) == ["Import", "Check fit", "Tailor resume", "Read package", "Approve", "Apply", "Save to tracker", "Contact recruiters"]
    for name, (tip, tech) in STEP_HELP.items():
        assert tip.endswith(".") and tech.islower() and "-" not in name
    corpus = " ".join([*STATUS_LABELS.values(), *NEXT_ACTIONS.values(), *(t for g in GLOSSARY.values() for t in g[:2]), *(x for v in PAGE_INTROS.values() for x in v)])
    for pat in DENYLIST:
        hits = [m.group(0) for m in re.finditer(pat, corpus, flags=re.I)]
        assert not hits, (pat, hits)


def test_failure_reasons_become_plain_sentences():
    """The technical stop reason stays in the evidence; the page shows one plain sentence that says nothing was sent."""
    from amorehiremepls.web.ui_settings import plain_failure

    raw = 'timeout before final action: Locator.evaluate: Timeout 15000ms exceeded.\nCall log:\n  - waiting for locator("#resume, input[type=file][name=resume]").first\n'
    assert plain_failure(raw) == "The job site did not respond in time. Nothing was sent."
    assert plain_failure("the form did not respond in time at step 6 (Resume/CV); nothing was sent") == "The job site did not respond in time. Nothing was sent."
    assert plain_failure("resume field not found: Resume/CV. Nothing was filled in or sent") == "The form had no place to attach the resume. Nothing was sent."
    missing = "the form is missing fields the app expected: Phone, Resume/CV. Nothing was filled in or sent"
    assert plain_failure(missing) == "The form did not have: Phone, Resume/CV. Nothing was sent."
    unconfirmed = "could not confirm the resume was attached: 'A_Resume.pdf' is not shown on the form. Nothing was sent"
    assert plain_failure(unconfirmed) == "The resume did not show as attached on the form. Nothing was sent."
    problem = "the form reported a problem: ['Phone must include an area code']"
    assert plain_failure(problem) == "The job site reported a problem: Phone must include an area code. Nothing was sent."
    assert plain_failure("") == "" and plain_failure(None) == ""
    for sentence in (plain_failure(raw), plain_failure("resume field not found: Resume/CV")):
        for pat in DENYLIST:
            assert not re.search(pat, sentence, flags=re.I), (pat, sentence)
        assert "Locator" not in sentence and "#" not in sentence
