"""Design system checks: one semantic colour system with AA text on its tinted surfaces in both themes, a visible focus
ring, attention rows and tone classes in the rendered pages, and the Settings appearance panel."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI

CSS = Path("src/amorehiremepls/web/static/app.css").read_text()
TONES = ("ok", "warn", "bad", "info")


def _block(selector: str) -> dict[str, str]:
    body = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", CSS, flags=re.S).group(1)
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", body))


def _hex(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(a: tuple, b: tuple, pct_a: float) -> tuple:
    """color-mix(in srgb, a pct_a%, b): channel-wise interpolation of gamma-encoded sRGB."""
    return tuple(a[i] * pct_a + b[i] * (1 - pct_a) for i in range(3))


def _lum(rgb: tuple) -> float:
    def chan(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (chan(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: tuple, bg: tuple) -> float:
    lo, hi = sorted((_lum(fg), _lum(bg)))
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_tone_text_passes_aa_on_tinted_pills_and_cards(theme):
    tokens = _block(":root") if theme == "light" else _block('[data-theme="dark"]')
    base = _hex(tokens["mix-base"])
    soft_pct = float(tokens["soft-mix"].rstrip("%")) / 100
    surface = _hex(tokens["surface"])
    for tone in TONES:
        text = _hex(tokens[f"{tone}-text"])
        soft = _mix(_hex(tokens[tone]), base, soft_pct)
        assert contrast(text, soft) >= 4.5, (theme, tone, "text on pill", round(contrast(text, soft), 2))
        assert contrast(text, surface) >= 4.5, (theme, tone, "text on card", round(contrast(text, surface), 2))
        # the graphic colour (ring stroke, dot, icon) needs 3:1 against the card
        assert contrast(_hex(tokens[tone]), surface) >= 3.0, (theme, tone, "graphic on card")
    # neutral text on the neutral pill (surface-3) and body text on the page
    assert contrast(_hex(tokens["neutral-text"]), _hex(tokens["surface-3"])) >= 4.5
    assert contrast(_hex(tokens["text"]), _hex(tokens["bg-base"])) >= 7
    assert contrast(_hex(tokens["muted"]), surface) >= 4.5


def test_filled_controls_use_the_deepened_accent_and_presets_pass_aa():
    from amorehiremepls.web.ui_settings import ACCENT_PRESETS

    assert re.search(r"--accent-ui:\s*color-mix\(in srgb, var\(--accent\) (\d+)%, #000000\)", CSS)
    pct = int(re.search(r"--accent-ui:\s*color-mix\(in srgb, var\(--accent\) (\d+)%", CSS).group(1)) / 100
    for selector in (r"\.btn, button", r"\.nav a\.active", r"\.tabs a\.active"):
        assert re.search(selector + r" \{[^}]*background: var\(--accent-ui\)", CSS, flags=re.S), selector
    below = {name for name, hexv in ACCENT_PRESETS.items() if contrast(_mix(_hex(hexv), (0, 0, 0), pct), (255, 255, 255)) < 4.5}
    assert below <= {"amber"}, below  # amber is the documented exception; every other preset carries white text at AA


def test_job_fit_and_rules_are_grid_siblings_not_absolute():
    """The fit ring and the rules card share one grid. Neither card is taken out of flow."""
    job = Path("src/amorehiremepls/web/templates/job.html").read_text()
    start = job.index('<div class="grid-main top job-lower">')
    body = job[start : job.index('<div class="card flush">', start)]
    assert body.count('class="card ') == 2
    assert 'class="card rules-card order-2"' in body and 'class="card fit-card"' in body
    assert 'class="fit-ring' in body and "Does it fit your rules?" in body
    assert "position: absolute" not in body and "position:absolute" not in body
    fit_css = re.search(r"\.job-lower > \.fit-card \{([^}]*)\}", CSS).group(1)
    rules_css = re.search(r"\.job-lower > \.rules-card \{([^}]*)\}", CSS).group(1)
    assert "grid-column: 1" in fit_css and "position: static" in fit_css
    assert "grid-column: 2" in rules_css and "position: static" in rules_css
    assert "position: absolute" not in fit_css and "position: absolute" not in rules_css
    assert ".job-lower > .rules-card { grid-column: 1; grid-row: 2; }" in CSS
    menu = re.search(r"\.next-action details\.more \.pop-body \{([^}]*)\}", CSS).group(1)
    assert "top: calc(100% + 8px)" in menu and "right: 0" in menu and "left: auto" in menu
    assert "animation: rise var(--motion) var(--ease) backwards" in CSS


def test_semantic_pills_focus_ring_and_reduced_motion_are_defined():
    for tone in TONES:
        assert re.search(rf"\.pill\.{tone}, \.badge\.{tone} \{{[^}}]*color: var\(--{tone}-text\)", CSS), tone
    assert ".pill.neutral" in CSS and ".status .pill::before" in CSS
    assert re.search(r":focus-visible \{[^}]*outline: 2px solid var\(--accent-ui\)", CSS)
    assert ".choice input:focus-visible + span" in CSS and ".swatch input:focus-visible + span" in CSS and ".switch input:focus-visible" in CSS
    assert "tbody tr.hot td:first-child" in CSS and ".table-wrap .status .raw { opacity: 0;" in CSS
    for band, tone in (("weak", "bad"), ("partial", "warn"), ("strong", "ok"), ("excellent", "info")):
        assert f".fit-ring.band-{band} .value {{ stroke: var(--{tone}); }}" in CSS
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    # no stray web assets
    assert "@import" not in CSS and "url(http" not in CSS and "@font-face" not in CSS


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


@pytest.mark.mongo
def test_rendered_pages_carry_tone_and_attention_classes(client):
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)
    data = {"csrf": csrf, "url": "https://careers.acme.example/jobs/R77", "employer": "Acme", "title": "Backend Developer", "requisition_id": "R77"}
    r = client.post("/jobs/import", data={**data, "posting_text": "Permanent backend developer, Java, Toronto."})
    job_id = r.headers["location"].rsplit("/", 1)[1]
    rt = client.app.state.rt
    now = datetime.now(UTC)
    for i, status in enumerate(("needs_verification", "submitted")):
        rt.store.c("applications").insert_one(
            {
                "_id": f"app_design{i}",
                "job_id": job_id,
                "status": status,
                "provenance": {"origin": "local"},
                "material_hash": "m",
                "review_package": {"apply_url": "https://careers.acme.example/apply", "posting_url": "https://careers.acme.example/jobs/R77", "answers": [], "gaps": []},
                "history": [],
                "created_at": now,
                "updated_at": now,
            }
        )
    apps = client.get("/applications").text
    assert '<tr class="hot bad">' in apps and '<tr class="">' in apps  # unclear result flagged, applied row plain
    assert 'class="pill bad">Result unclear, check it</span><code class="raw"' in apps  # label + raw chip through the macro
    home = client.get("/").text
    assert 'class="card kpi tone-bad"' in home  # "Results unclear" tile turns red only while something is unclear
    assert 'class="pill neutral"' in client.get("/help").text  # unknown/neutral states get an explicit class
    job = client.get(f"/jobs/{job_id}").text
    assert '<hr class="soft">' in job and 'class="stepper"' in job and 'class="next-action"' in job  # one hero card: header, steps, next action
    assert 'class="grid-main top job-lower"' in job and 'class="card rules-card order-2"' in job and 'class="card fit-card"' in job
    settings = client.get("/settings").text
    assert 'class="appearance-preview"' in settings and 'class="theme-thumb system"' in settings
    assert re.search(r'<label class="switch">.*?name="company_logos" value="on" checked', settings, flags=re.S)
