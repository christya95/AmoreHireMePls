"""Dashboard presentation settings (theme, accent, display name, avatar) and small view helpers.

These are per-user presentation preferences, not candidate facts: they live in the `controls`
collection under one key and never feed resumes, answers or messages. The avatar file lives under the
private directory and is only ever served through an authenticated route.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .db import Store
from .util import sha256_bytes, to_toronto, utcnow

UI_SETTINGS_KEY = "ui_settings"
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Accent presets (name -> hex). "sage" matches the reference design; everything else derives through color-mix().
ACCENT_PRESETS: dict[str, str] = {
    "sage": "#6f8f73",
    "blue": "#3b6fd6",
    "violet": "#7c5cd6",
    "teal": "#1f8a8a",
    "amber": "#c98a1b",
    "rose": "#d6517a",
}
DEFAULT_ACCENT = ACCENT_PRESETS["sage"]
AVATAR_STYLES = ("soft", "solid", "ring", "gradient")
AVATAR_MAX_BYTES = 1_048_576
AVATAR_MAX_DIMENSION = 4096
AVATAR_MIN_DIMENSION = 16
AVATAR_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
Theme = Literal["system", "light", "dark"]


class AvatarInfo(BaseModel):
    kind: Literal["initials", "upload"] = "initials"
    style: str = "soft"
    content_type: str | None = None
    ext: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    updated_at: datetime | None = None


class UISettings(BaseModel):
    theme: Theme = "system"
    accent: str = DEFAULT_ACCENT
    display_name: str | None = None
    avatar: AvatarInfo = Field(default_factory=AvatarInfo)
    updated_at: datetime | None = None

    @property
    def accent_name(self) -> str | None:
        for name, hexv in ACCENT_PRESETS.items():
            if hexv.lower() == self.accent.lower():
                return name
        return None


def normalize_accent(value: str | None) -> str | None:
    """Preset name or #rrggbb -> canonical lowercase hex; None when invalid."""
    v = (value or "").strip().lower()
    if v in ACCENT_PRESETS:
        return ACCENT_PRESETS[v]
    if HEX_RE.match(v):
        return v
    return None


class UISettingsStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    def load(self) -> UISettings:
        doc = self.store.get_control(UI_SETTINGS_KEY)
        if not doc:
            return UISettings()
        try:
            return UISettings.model_validate(doc)
        except Exception:  # a hand-edited document never breaks the dashboard
            return UISettings()

    def save(self, ui: UISettings) -> UISettings:
        ui.updated_at = utcnow()
        self.store.set_control(UI_SETTINGS_KEY, ui.model_dump(mode="python"))
        return ui


# -- avatar files -------------------------------------------------------------------------------------


def sniff_image(data: bytes) -> tuple[str, str, int, int] | None:
    """Return (content_type, ext, width, height) from the file header, or None when it is not a PNG/JPEG/WebP."""
    if len(data) < 30:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return "image/png", "png", w, h
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return "image/jpeg", "jpg", w, h
            i += 2 + seg_len
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 " and len(data) >= 30:
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return "image/webp", "webp", w, h
        if chunk == b"VP8L" and len(data) >= 25:
            bits = int.from_bytes(data[21:25], "little")
            return "image/webp", "webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X" and len(data) >= 30:
            return "image/webp", "webp", 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    return None


class AvatarError(ValueError):
    pass


def validate_avatar(data: bytes, declared_type: str | None) -> tuple[str, str, int, int]:
    """Size, declared type and sniffed header must all agree; anything else is refused."""
    if not data:
        raise AvatarError("empty file")
    if len(data) > AVATAR_MAX_BYTES:
        raise AvatarError(f"file is larger than {AVATAR_MAX_BYTES // 1024} KB")
    declared = (declared_type or "").split(";")[0].strip().lower()
    if declared == "image/jpg":
        declared = "image/jpeg"
    if declared not in AVATAR_TYPES:
        raise AvatarError("only PNG, JPEG or WebP images are accepted")
    sniffed = sniff_image(data)
    if sniffed is None or sniffed[0] != declared:
        raise AvatarError("file content does not match its image type")
    ctype, ext, w, h = sniffed
    if not (AVATAR_MIN_DIMENSION <= w <= AVATAR_MAX_DIMENSION and AVATAR_MIN_DIMENSION <= h <= AVATAR_MAX_DIMENSION):
        raise AvatarError(f"image must be between {AVATAR_MIN_DIMENSION} and {AVATAR_MAX_DIMENSION} pixels per side")
    return ctype, ext, w, h


def avatar_path(private_dir: Path, ext: str) -> Path:
    p = private_dir / "avatar" / f"avatar.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def store_avatar(private_dir: Path, data: bytes, declared_type: str | None) -> AvatarInfo:
    ctype, ext, w, h = validate_avatar(data, declared_type)
    remove_avatar_files(private_dir)
    avatar_path(private_dir, ext).write_bytes(data)
    return AvatarInfo(kind="upload", content_type=ctype, ext=ext, sha256=sha256_bytes(data), width=w, height=h, updated_at=utcnow())


def remove_avatar_files(private_dir: Path) -> None:
    for ext in set(AVATAR_TYPES.values()):
        p = private_dir / "avatar" / f"avatar.{ext}"
        if p.exists():
            p.unlink()


# -- greeting, initials ---------------------------------------------------------------------------------


def resolve_display_name(ui: UISettings, profile: Any, fallback: str = "Joss") -> str:
    """Saved dashboard name, else the profile's preferred/first name, else the fallback given by the caller."""
    if ui.display_name:
        return ui.display_name
    for key in ("preferred_name", "legal_first_name"):
        v = profile.fact_value(key) if profile is not None else None
        if v:
            return v.split()[0]
    v = profile.fact_value("display_name") if profile is not None else None
    if v:
        return v.split()[0]
    return fallback


def initials(name: str) -> str:
    parts = [p for p in re.split(r"[\s._-]+", name.strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def greeting(now: datetime | None = None) -> str:
    hour = to_toronto(now or utcnow()).hour
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 22:
        return "Good evening"
    return "Working late"


# -- status tones -----------------------------------------------------------------------------------------

_TONES: dict[str, str] = {
    "ok": "submitted applied applied_historical synced succeeded active qualified ok completed done resolved present allowed",
    "warn": "needs_review needs_input ready_for_review authorized handoff waiting paused sync_pending local_only committed_local prepared researched queued claimed pending unresolved",  # noqa: E501
    "bad": "needs_verification failed conflict blocked error cancelled canceled rejected expired revoked missing down skipped",
    "info": "new running executing draft imported reserved",
}
STATUS_TONE = {s: tone for tone, words in _TONES.items() for s in words.split()}


def status_tone(status: Any) -> str:
    return STATUS_TONE.get(str(status or "").lower(), "neutral")


def humanize(value: Any) -> str:
    return str(value or "").replace("_", " ")


# -- dependency-free SVG chart geometry ------------------------------------------------------------------


def _smooth_path(pts: list[tuple[float, float]]) -> str:
    """Catmull-Rom spline converted to cubic Beziers; monotone enough for small series."""
    if len(pts) < 2:
        return ""
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append(f"C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(d)


def line_chart(series: list[tuple[str, int | float]], width: int = 640, height: int = 220, pad: tuple[int, int, int, int] = (16, 12, 30, 36)) -> dict[str, Any]:
    """Geometry for a smooth area chart. Returns paths, point coordinates, y ticks and x labels."""
    top, right, bottom, left = pad
    n = len(series)
    values = [float(v) for _, v in series]
    vmax = max(values) if values else 0.0
    ymax = max(1.0, vmax)
    # a round-ish axis maximum (1, 2, 5 ladder)
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if ymax <= step * 4:
            ymax = step * 4
            tick_step = step
            break
    else:
        tick_step = max(1, int(ymax // 4))
        ymax = tick_step * 4
    inner_w = max(1, width - left - right)
    inner_h = max(1, height - top - bottom)

    def x(i: int) -> float:
        return left + (inner_w * i / (n - 1) if n > 1 else inner_w / 2)

    def y(v: float) -> float:
        return top + inner_h - inner_h * (v / ymax)

    pts = [(x(i), y(v)) for i, v in enumerate(values)]
    line = _smooth_path(pts)
    area = f"{line} L{pts[-1][0]:.1f},{top + inner_h:.1f} L{pts[0][0]:.1f},{top + inner_h:.1f} Z" if line else ""
    ticks = [{"v": int(tick_step * k), "y": round(y(tick_step * k), 1)} for k in range(0, 5)]
    return {
        "width": width,
        "height": height,
        "line": line,
        "area": area,
        "points": [{"x": round(px, 1), "y": round(py, 1), "label": series[i][0], "value": series[i][1]} for i, (px, py) in enumerate(pts)],
        "ticks": ticks,
        "x0": left,
        "x1": width - right,
        "baseline": round(top + inner_h, 1),
        "max": vmax,
        "total": sum(values),
    }


def donut(items: list[tuple[str, int | float, str]]) -> dict[str, Any]:
    """Segments for an SVG donut (circumference normalised to 100). items: (label, value, css class)."""
    total = float(sum(v for _, v, _ in items)) or 0.0
    segs = []
    offset = 25.0  # start at 12 o'clock
    for label, value, cls in items:
        pct = (float(value) / total * 100) if total else 0.0
        segs.append({"label": label, "value": value, "pct": round(pct, 1), "dash": f"{pct:.2f} {100 - pct:.2f}", "offset": round(offset, 2), "cls": cls})
        offset -= pct
    return {"total": int(total) if total.is_integer() else total, "segments": segs}


def month_buckets(count: int = 12, now: datetime | None = None) -> list[tuple[str, str]]:
    """Last `count` months in America/Toronto as (yyyy-mm key, short label), oldest first."""
    t = to_toronto(now or utcnow())
    y, m = t.year, t.month
    out: list[tuple[str, str]] = []
    for _ in range(count):
        out.append((f"{y:04d}-{m:02d}", datetime(y, m, 1).strftime("%b")))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))
