"""Fit score ring: a 0–100 view derived from a stored assessment, with no model call."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.ui_settings import fit_score
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI


def test_rank_anchors_land_in_the_named_bands():
    assert fit_score(None) is None
    assert fit_score({}) is None
    assert fit_score({"rank": True}) is None
    assert fit_score({"rank": "2"}) is None
    bands = {1: ("excellent", "info", 94), 2: ("strong", "ok", 78), 3: ("partial", "warn", 55), 4: ("weak", "bad", 32), 5: ("weak", "bad", 14)}
    for rank, (band, tone, score) in bands.items():
        view = fit_score({"rank": rank})
        assert view["band"] == band and view["tone"] == tone and view["score"] == score
        assert view["offset"] == round(view["circ"] * (1 - score / 100), 2)
    assert fit_score({"rank": 9})["score"] == 14  # clamped to the worst rank
    assert fit_score({"rank": 0})["score"] == 94


def test_grounded_matches_nudge_and_blockers_pull_down():
    bare = fit_score({"rank": 3})
    sunny = fit_score({"rank": 3, "fit_points": ["a", "b", "c", "d"], "genuine_gaps": ["g"], "missing_evidence": ["u"]})
    cloudy = fit_score({"rank": 3, "fit_points": ["a"], "genuine_gaps": ["g", "h", "i", "j"], "missing_evidence": ["u"]})
    blocked = fit_score({"rank": 2, "fit_points": ["a"], "mandatory_qualification_blockers": ["needs a clearance", "needs a second clearance"]})
    assert bare["score"] == 55
    assert sunny["score"] > bare["score"] and cloudy["score"] < bare["score"]
    assert 40 <= cloudy["score"] <= 69 and 40 <= sunny["score"] <= 69
    assert blocked["score"] == 63  # base 78, one match of three is a -3 nudge, two blockers cap at -12
    assert blocked["gaps_n"] == 2 and blocked["blockers"] == ["needs a clearance", "needs a second clearance"]
    assert fit_score({"rank": 1, "fit_points": ["  spaced   line  "], "genuine_gaps": [""], "missing_evidence": "nope"})["matches"] == ["spaced line"]
    cited = fit_score(
        {
            "rank": 3,
            "fit_points": [
                "Has current Software Developer experience at Big Bank from May 2021 to Mar 2025 (emp_0f1e2d3c4b5a6978).",
                "emp_1: shipped a billing API",
                "b_1: Software Developer: Built REST APIs.",
            ],
            "genuine_gaps": ["No evidence of a fictional tool (fact_00aa11bb22cc33dd, fact_44ee55ff66778899)."],
            "missing_evidence": ["Spring (skill_3)."],
        }
    )
    assert cited["matches"] == [
        "Has current Software Developer experience at Big Bank from May 2021 to Mar 2025.",
        "shipped a billing API",
        "Software Developer: Built REST APIs.",
    ]
    assert cited["gaps"] == ["No evidence of a fictional tool."]
    assert cited["unknown"] == ["Spring."]
    assert "emp_" not in " ".join(cited["matches"]) and "fact_" not in " ".join(cited["gaps"])


def test_band_edges():
    assert fit_score({"rank": 4})["band"] == "weak"  # 32
    lifted = fit_score({"rank": 4, "fit_points": ["a"] * 8})  # every point is a match: +8, into the partial band
    assert lifted["score"] == 40 and lifted["band"] == "partial"
    assert fit_score({"rank": 1})["band"] == "excellent"


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
    return TestClient(create_app(Runtime(s, store=store)), follow_redirects=False)


@pytest.mark.mongo
def test_job_page_shows_the_ring_and_hides_raw_json(client):
    rt = client.app.state.rt
    job, _ = rt.jobs.upsert(employer="Globex", title="Backend Engineer", posting_url="https://jobs.globex.example/1")
    rt.jobs.set_status(
        job["_id"],
        "needs_review",
        fit={
            "rank": 2,
            "fit_points": ["emp_1: shipped a billing API"],
            "genuine_gaps": ["no Kafka in the approved facts"],
            "missing_evidence": ["on-call rotation not stated"],
            "mandatory_qualification_blockers": [],
            "notes": "raw note that stays in the details",
        },
    )
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get(f"/jobs/{job['_id']}").text
    assert 'aria-label="Fit score 75 out of 100, Strong match"' in page
    assert 'class="fit-ring band-strong"' in page
    assert "Where you line up" in page and "shipped a billing API" in page
    assert "emp_1" not in re.sub(r"<details\b.*?</details>", " ", page, flags=re.S)
    assert "Gaps" in page and "no Kafka in the approved facts" in page
    assert "Not enough to say" in page and "on-call rotation not stated" in page
    assert 'class="fit-bar"' in page
    # the raw document is only inside Technical details
    visible = re.sub(r"<details\b.*?</details>", "", page, flags=re.S)
    assert "raw note that stays in the details" not in visible
    assert "<pre>" not in visible
    jobs = client.get("/jobs").text
    assert 'class="pill ok"' in jobs and ">75<" in jobs
    # a job with no assessment still says so, and does not invent a score
    empty, _ = rt.jobs.upsert(employer="Initech", title="Backend Engineer", posting_url="https://jobs.initech.example/1")
    quiet = client.get(f"/jobs/{empty['_id']}").text
    assert "No fit score yet" in quiet and "fit-ring" not in quiet
    assert Path("src/amorehiremepls/web/static/app.css").read_text().count("cdn") == 0
