"""Dashboard presentation settings: pure helpers (no database) and the settings/avatar endpoints (Mongo)."""

from __future__ import annotations

import re
import struct
import zlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from amorehiremepls.config import Settings
from amorehiremepls.runtime import Runtime
from amorehiremepls.ui_settings import (
    ACCENT_PRESETS,
    AVATAR_MAX_BYTES,
    AvatarError,
    UISettings,
    donut,
    greeting,
    initials,
    line_chart,
    month_buckets,
    normalize_accent,
    resolve_display_name,
    sniff_image,
    status_tone,
    validate_avatar,
)
from amorehiremepls.web import create_app
from tests.conftest import TEST_MONGO_URI

# -- fixtures: tiny valid images built in memory ------------------------------------------------------------


def png_bytes(width: int = 32, height: int = 32) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80\x90\x80" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def jpeg_bytes(width: int = 40, height: int = 24) -> bytes:
    # SOI, APP0, SOF0 (with dimensions), then junk; enough for header sniffing.
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + b"\x00" * 64 + b"\xff\xd9"


def webp_bytes(width: int = 50, height: int = 20) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"VP8L" + struct.pack("<I", 5) + b"\x2f" + struct.pack("<I", bits) + b"\x00" * 20
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WEBP" + payload


# -- helpers ------------------------------------------------------------------------------------------------


def test_sniff_and_validate_accept_real_headers_only():
    assert sniff_image(png_bytes(32, 20)) == ("image/png", "png", 32, 20)
    assert sniff_image(jpeg_bytes(40, 24)) == ("image/jpeg", "jpg", 40, 24)
    assert sniff_image(webp_bytes(50, 20)) == ("image/webp", "webp", 50, 20)
    assert sniff_image(b"GIF89a" + b"\x00" * 40) is None
    assert sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>" + b" " * 40) is None
    assert validate_avatar(png_bytes(), "image/png")[0] == "image/png"
    with pytest.raises(AvatarError, match="does not match"):
        validate_avatar(png_bytes(), "image/jpeg")  # declared type must agree with the bytes
    with pytest.raises(AvatarError, match="only PNG"):
        validate_avatar(png_bytes(), "text/plain")
    with pytest.raises(AvatarError, match="larger than"):
        validate_avatar(png_bytes() + b"\x00" * AVATAR_MAX_BYTES, "image/png")
    with pytest.raises(AvatarError, match="between"):
        validate_avatar(png_bytes(8, 8), "image/png")
    with pytest.raises(AvatarError, match="between"):
        validate_avatar(png_bytes(5000, 10), "image/png")
    with pytest.raises(AvatarError, match="empty"):
        validate_avatar(b"", "image/png")


def test_accent_normalisation_and_presets():
    assert normalize_accent("sage") == ACCENT_PRESETS["sage"]
    assert normalize_accent("#ABCDEF") == "#abcdef"
    assert normalize_accent("  #123456 ") == "#123456"
    for bad in ("", "red", "#12345", "#1234567", "#ggg000", "javascript:alert(1)", "#123456;x"):
        assert normalize_accent(bad) is None, bad
    assert UISettings(accent="#3b6fd6").accent_name == "blue"
    assert UISettings(accent="#010203").accent_name is None


def test_greeting_initials_and_display_name_resolution():
    class P:
        def __init__(self, **facts):
            self.facts = facts

        def fact_value(self, key):
            return self.facts.get(key)

    assert greeting(datetime(2026, 11, 5, 13, 0, tzinfo=UTC)) == "Good morning"  # 08:00 Toronto
    assert greeting(datetime(2026, 11, 5, 19, 0, tzinfo=UTC)) == "Good afternoon"  # 14:00
    assert greeting(datetime(2026, 11, 6, 0, 0, tzinfo=UTC)) == "Good evening"  # 19:00
    assert greeting(datetime(2026, 11, 6, 6, 30, tzinfo=UTC)) == "Working late"  # 01:30
    assert initials("Ada Lovelace") == "AL" and initials("ada") == "AD" and initials("") == "?" and initials("Acme-Corp Inc") == "AI"
    assert resolve_display_name(UISettings(display_name="Captain"), P(preferred_name="Sam")) == "Captain"
    assert resolve_display_name(UISettings(), P(preferred_name="Sam Smith")) == "Sam"
    assert resolve_display_name(UISettings(), P(legal_first_name="Samuel")) == "Samuel"
    assert resolve_display_name(UISettings(), P(display_name="S. Smith")) == "S."
    assert resolve_display_name(UISettings(), P(), fallback="Fallback") == "Fallback"
    assert resolve_display_name(UISettings(), None, fallback="F") == "F"


def test_chart_and_donut_geometry_are_well_formed():
    buckets = month_buckets(12, datetime(2026, 11, 5, 12, 0, tzinfo=UTC))
    assert len(buckets) == 12 and buckets[-1] == ("2026-11", "Nov") and buckets[0] == ("2025-12", "Dec")
    c = line_chart([(label, i % 4) for i, (_, label) in enumerate(buckets)])
    assert c["line"].startswith("M") and "C" in c["line"] and c["area"].endswith("Z") and len(c["points"]) == 12
    assert c["ticks"][0]["v"] == 0 and c["ticks"][-1]["v"] >= max(i % 4 for i in range(12))
    assert all(c["x0"] <= p["x"] <= c["x1"] for p in c["points"])
    empty = line_chart([])
    assert empty["line"] == "" and empty["area"] == "" and empty["points"] == []
    single = line_chart([("Nov", 3)])
    assert single["line"] == "" and len(single["points"]) == 1
    d = donut([("new", 3, "c1"), ("applied", 1, "c2")])
    assert d["total"] == 4 and d["segments"][0]["pct"] == 75.0 and d["segments"][1]["offset"] == pytest.approx(25 - 75)
    assert donut([])["total"] == 0 and donut([("x", 0, "c1")])["segments"][0]["pct"] == 0
    assert status_tone("submitted") == "ok" and status_tone("needs_verification") == "bad" and status_tone("handoff") == "warn"
    assert status_tone("whatever") == "neutral" and status_tone(None) == "neutral"


# -- endpoints ----------------------------------------------------------------------------------------------


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
    page = client.get("/")
    assert page.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


@pytest.mark.mongo
def test_settings_routes_require_login_and_csrf(client):
    for path in ("/settings", "/settings/avatar/image", "/applications", "/documents"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert client.get("/static/app.css").status_code == 200  # static assets are the only public addition
    csrf = _login(client)
    for path, data in (
        ("/settings/theme", {"theme": "dark"}),
        ("/settings/appearance", {"theme": "dark", "accent": "blue"}),
        ("/settings/identity", {"display_name": "X"}),
        ("/settings/avatar", {"style": "solid"}),
        ("/settings/avatar/remove", {}),
    ):
        assert client.post(path, data=data).status_code == 422, path  # token missing
        assert client.post(path, data={**data, "csrf": "bogus"}).status_code == 403, path
    # a cross-site origin is refused even with the right token
    assert client.post("/settings/theme", data={"csrf": csrf, "theme": "dark"}, headers={"origin": "https://evil.example", "host": "testserver"}).status_code == 403
    assert client.app.state.rt.store.get_control("ui_settings") is None  # nothing persisted by the refused requests


@pytest.mark.mongo
def test_theme_accent_and_name_persist_and_render(client):
    csrf = _login(client)
    home = client.get("/").text
    assert 'data-theme="' not in home and "Good" in home or "Working late" in home  # system theme by default, greeting present
    assert f'data-accent="{ACCENT_PRESETS["sage"]}"' in home
    # header toggle (fetch path) returns JSON and sets the mirror cookie
    r = client.post("/settings/theme", data={"csrf": csrf, "theme": "dark", "next": "/jobs"}, headers={"x-requested-with": "fetch"})
    assert r.status_code == 200 and r.json() == {"ok": True, "theme": "dark"} and "amore_ui=" in r.headers.get("set-cookie", "")
    assert 'data-theme="dark"' in client.get("/").text
    # plain form path redirects to a validated local `next`
    r = client.post("/settings/theme", data={"csrf": csrf, "theme": "light", "next": "//evil.example/x"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.post("/settings/theme", data={"csrf": csrf, "theme": "light", "next": "/usage"})
    assert r.headers["location"] == "/usage" and 'data-theme="light"' in client.get("/usage").text
    assert client.post("/settings/theme", data={"csrf": csrf, "theme": "sepia"}).status_code == 400
    # appearance: preset, custom hex, invalid hex
    r = client.post("/settings/appearance", data={"csrf": csrf, "theme": "dark", "accent": "violet"})
    assert r.status_code == 303 and r.headers["location"] == "/settings?ok=saved"
    page = client.get("/settings?ok=saved").text
    assert f'data-accent="{ACCENT_PRESETS["violet"]}"' in page and 'data-theme="dark"' in page and "Saved." in page
    r = client.post("/settings/appearance", data={"csrf": csrf, "theme": "system", "accent": "custom", "accent_hex": "#A1B2C3"})
    assert r.status_code == 303
    page = client.get("/settings").text
    assert 'data-accent="#a1b2c3"' in page and 'data-theme="' not in page
    r = client.post("/settings/appearance", data={"csrf": csrf, "theme": "system", "accent": "custom", "accent_hex": "url(javascript:1)"})
    assert r.status_code == 303 and r.headers["location"].endswith("err=bad_accent")
    assert 'data-accent="#a1b2c3"' in client.get("/settings").text  # unchanged
    # the sign-in page honours the mirrored cookie before any session exists
    anon = TestClient(client.app, follow_redirects=False)
    anon.cookies.set("amore_ui", "dark|#a1b2c3")
    login = anon.get("/login").text
    assert 'data-theme="dark"' in login and 'data-accent="#a1b2c3"' in login
    anon.cookies.set("amore_ui", "weird|not-a-colour")
    login = anon.get("/login").text
    assert 'data-theme="' not in login and f'data-accent="{ACCENT_PRESETS["sage"]}"' in login
    # display name
    r = client.post("/settings/identity", data={"csrf": csrf, "display_name": "  Captain   Nemo  "})
    assert r.status_code == 303
    home = client.get("/").text
    assert ", Captain Nemo" in home and ">CN<" in home
    stored = client.app.state.rt.store.get_control("ui_settings")
    assert stored["display_name"] == "Captain Nemo" and stored["theme"] == "system" and stored["accent"] == "#a1b2c3"
    client.post("/settings/identity", data={"csrf": csrf, "display_name": ""})
    assert client.app.state.rt.store.get_control("ui_settings")["display_name"] is None


@pytest.mark.mongo
def test_avatar_upload_validation_storage_and_removal(client, tmp_path):
    csrf = _login(client)
    rt = client.app.state.rt
    # wrong declared type, disguised bytes, oversize and non-image uploads are refused and nothing is written
    for name, content, ctype in (
        ("a.txt", b"hello " * 10, "text/plain"),
        ("a.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>" + b" " * 40, "image/png"),
        ("a.png", jpeg_bytes(), "image/png"),
        ("big.png", png_bytes() + b"\x00" * AVATAR_MAX_BYTES, "image/png"),
        ("a.html", b"<html>" * 20, "text/html"),
    ):
        r = client.post("/settings/avatar", data={"csrf": csrf}, files={"file": (name, content, ctype)})
        assert r.status_code == 400, name
    assert not (rt.settings.private_dir / "avatar").exists()
    assert client.get("/settings/avatar/image").status_code == 404
    # a valid PNG is stored under the private dir and served only to the session
    r = client.post("/settings/avatar", data={"csrf": csrf}, files={"file": ("me.png", png_bytes(64, 64), "image/png")})
    assert r.status_code == 303 and r.headers["location"] == "/settings?ok=avatar_saved"
    stored = rt.store.get_control("ui_settings")["avatar"]
    assert stored["kind"] == "upload" and stored["content_type"] == "image/png" and stored["width"] == 64
    files = list((rt.settings.private_dir / "avatar").iterdir())
    assert [f.name for f in files] == ["avatar.png"]
    img = client.get("/settings/avatar/image")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/png") and img.content == png_bytes(64, 64)
    assert "private" in img.headers["cache-control"] and img.headers["x-content-type-options"] == "nosniff"
    home = client.get("/").text
    assert f"/settings/avatar/image?v={stored['sha256'][:16]}" in home
    assert TestClient(client.app, follow_redirects=False).get("/settings/avatar/image").status_code == 303  # no session -> login
    # replacing with a WebP removes the old file; initials styles switch kind back without deleting unexpectedly
    r = client.post("/settings/avatar", data={"csrf": csrf}, files={"file": ("me.webp", webp_bytes(), "image/webp")})
    assert r.status_code == 303
    assert [f.name for f in (rt.settings.private_dir / "avatar").iterdir()] == ["avatar.webp"]
    assert client.post("/settings/avatar", data={"csrf": csrf, "style": "gradient"}).status_code == 303
    assert rt.store.get_control("ui_settings")["avatar"]["style"] == "gradient"
    assert client.post("/settings/avatar", data={"csrf": csrf, "style": "sparkly"}).status_code == 400
    assert client.post("/settings/avatar", data={"csrf": csrf}).status_code == 400  # neither file nor style
    r = client.post("/settings/avatar/remove", data={"csrf": csrf})
    assert r.status_code == 303 and not list((rt.settings.private_dir / "avatar").iterdir())
    assert rt.store.get_control("ui_settings")["avatar"]["kind"] == "initials"
    assert client.get("/settings/avatar/image").status_code == 404
    assert 'class="avatar  style-gradient"' in client.get("/").text or "style-gradient" in client.get("/").text


@pytest.mark.mongo
def test_overview_lists_and_new_pages_render_with_data(client):
    csrf = _login(client)
    rt = client.app.state.rt
    r = client.post(
        "/jobs/import",
        data={
            "csrf": csrf,
            "url": "https://careers.acme.example/jobs/R90",
            "employer": "Acme",
            "title": "Backend Developer",
            "requisition_id": "R90",
            "posting_text": "Permanent backend developer, Java, Toronto.",
        },
    )
    job_id = r.headers["location"].rsplit("/", 1)[1]
    rt.store.c("applications").insert_one(
        {
            "_id": "app_test1",
            "job_id": job_id,
            "status": "needs_verification",
            "provenance": {"origin": "local"},
            "material_hash": "m",
            "review_package": {"apply_url": "https://careers.acme.example/apply", "posting_url": "https://careers.acme.example/jobs/R90", "answers": [], "gaps": []},
            "history": [],
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )
    home = client.get("/").text
    assert "Needs your attention" in home and "Outcome unknown" in home and "/applications/app_test1" in home
    assert 'class="chart"' in home and "Job pipeline" in home and "Spend this month" in home
    apps = client.get("/applications").text
    assert "app_test1" in apps and "needs verification" in apps
    assert "app_test1" in client.get("/applications?status_filter=needs_verification").text
    assert "app_test1" not in client.get("/applications?status_filter=submitted").text
    assert client.get("/documents").status_code == 200
    assert 'aria-current="page"' in client.get("/settings").text
    # the emergency stop still needs its confirm form and the clear button appears once active
    assert "Confirm emergency stop" in home
    assert client.post("/controls/emergency-stop", data={"csrf": csrf, "active": "on", "reason": "test"}).status_code == 303
    page = client.get("/").text
    assert "Emergency stop active" in page and "Clear emergency stop" in page
    client.post("/controls/emergency-stop", data={"csrf": csrf, "active": "off"})
