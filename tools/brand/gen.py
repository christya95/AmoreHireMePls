#!/usr/bin/env python3
"""Generator for the AmoreHireMePls logo mark (paper plane + briefcase handle + heart trail).

Writes mark.svg, mark-tile.svg, favicon.svg and the preview/lockup source SVGs into the output directory
(first argument, default /tmp/amore_logo). All geometry lives in a 64x64 viewBox; everything is hand-tuned
here and emitted as compact paths.

Tuning workflow: edit the geometry below, run `python tools/brand/gen.py /tmp/amore_logo`, render PNGs with
`tools/brand/render.sh /tmp/amore_logo` (needs rsvg-convert), inspect, then copy mark.svg, mark-tile.svg,
favicon.svg, favicon-32.png and favicon-180.png into src/amorehiremepls/static/brand/. The app reads the
paths from static/brand/mark.svg at startup, so the sidebar, sign-in page, favicon and avatar follow.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/amore_logo")
SAGE = "#6f8f73"
DARK = "#1f2421"
K = 0.5523  # cubic kappa for quarter circles

# ---------------------------------------------------------------- helpers

def f(v: float) -> str:
    s = f"{v:.1f}"
    if s.endswith(".0"):
        s = s[:-2]
    if s == "-0":
        s = "0"
    return s


OFFSET = (1.0, -1.5)  # optical centring of the whole mark inside the 64 box


def pt(p) -> str:
    return f"{f(p[0] + OFFSET[0])} {f(p[1] + OFFSET[1])}"


def add(a, b, s=1.0):
    return (a[0] + b[0] * s, a[1] + b[1] * s)


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def norm(v):
    l = math.hypot(*v)
    return (v[0] / l, v[1] / l)


def quarter(center, r, a0_deg, a1_deg):
    """Quarter circle as cubic: returns (p1, p2, p3) in the same frame as center."""
    a0, a1 = math.radians(a0_deg), math.radians(a1_deg)
    sgn = 1.0 if a1 > a0 else -1.0
    p0 = (center[0] + r * math.cos(a0), center[1] + r * math.sin(a0))
    p3 = (center[0] + r * math.cos(a1), center[1] + r * math.sin(a1))
    t0 = (-math.sin(a0) * sgn, math.cos(a0) * sgn)
    t1 = (-math.sin(a1) * sgn, math.cos(a1) * sgn)
    p1 = add(p0, t0, K * r)
    p2 = sub(p3, (t1[0] * K * r, t1[1] * K * r))
    return p1, p2, p3


# ---------------------------------------------------------------- plane geometry (64 box)
# Global points. Plane flies up to the right. y grows downward.
N = (59.0, 9.0)   # nose
A = (12.5, 26.0)  # top wing back corner
F = (29.0, 32.5)  # where the centre fold meets the back
T = (28.5, 47.0)  # tail bottom (left tip of the body)
V = (36.5, 40.0)  # notch between tail and lower wing
B = (44.5, 48.5)  # lower wing back tip

GAP = 1.15  # half of the crease gap at the back (tapers to 0 at the nose)

fold_dir = norm(sub(N, F))
perp = (fold_dir[1], -fold_dir[0])  # rotate; pick the side where A lies
if (A[0] - F[0]) * perp[0] + (A[1] - F[1]) * perp[1] < 0:
    perp = (-perp[0], -perp[1])
F1 = add(F, perp, GAP)   # top-wing side
F2 = add(F, perp, -GAP)  # body side

# Handle in a frame local to the top edge: x along edge (A->N), y "up" away from the wing.
u = norm(sub(N, A))
n = (u[1], -u[0])
if n[1] > 0:  # ensure it points up on screen
    n = (-n[0], -n[1])
EDGE_LEN = math.hypot(*sub(N, A))
HANDLE_T = 0.53 * EDGE_LEN  # handle centre along the edge
C = add(A, u, HANDLE_T)

HW, HH, HR = 8.6, 7.0, 4.6       # outer half-width, height above edge, outer corner radius
IW, IH, IR, ID = 5.6, 4.0, 1.8, 2.4  # inner half-width, inner top, inner radius, notch depth into wing


def L(p_local):
    """Local handle frame -> global."""
    return add(add(C, u, p_local[0]), n, p_local[1])


def rounded_top(hw, top, r, bottom):
    """Local path pieces for a rounded-top rectangle from y=bottom to y=top, open at the bottom.
    Returns list of ('L'|'C', points...) in local coords, starting at (-hw,bottom), ending at (hw,bottom)."""
    segs = []
    segs.append(("M", (-hw, bottom)))
    segs.append(("L", (-hw, top - r)))
    segs.append(("C", *quarter((-hw + r, top - r), r, 180, 90)))
    segs.append(("L", (hw - r, top)))
    segs.append(("C", *quarter((hw - r, top - r), r, 90, 0)))
    segs.append(("L", (hw, bottom)))
    return segs


def emit(segs, transform=L, close=False, first_as_line=False):
    out = []
    for i, s in enumerate(segs):
        cmd, pts = s[0], s[1:]
        if i == 0 and first_as_line:
            cmd = "L"
        out.append(cmd + " " + " ".join(pt(transform(p)) for p in pts))
    if close:
        out.append("Z")
    return " ".join(out)


def ident(p):
    return p


def top_wing_path() -> str:
    outer = rounded_top(HW, HH, HR, 0.0)
    hole = rounded_top(IW, IH, IR, -ID)
    d = f"M {pt(A)} " + emit(outer, first_as_line=True) + f" L {pt(N)} L {pt(F1)} Z "
    d += emit(hole, close=True)
    return d


def body_path() -> str:
    return f"M {pt(N)} L {pt(F2)} L {pt(T)} L {pt(V)} L {pt(B)} Z"


# ---------------------------------------------------------------- hearts
# Unit heart: width 2 (x -1..1), y -0.8..0.95, upright. Cubic curves only.
HEART = [
    ("M", (0.0, 0.95)),
    ("C", (-0.42, 0.62), (-1.0, 0.22), (-1.0, -0.25)),
    ("C", (-1.0, -0.55), (-0.78, -0.8), (-0.5, -0.8)),
    ("C", (-0.22, -0.8), (0.0, -0.62), (0.0, -0.38)),
    ("C", (0.0, -0.62), (0.22, -0.8), (0.5, -0.8)),
    ("C", (0.78, -0.8), (1.0, -0.55), (1.0, -0.25)),
    ("C", (1.0, 0.22), (0.42, 0.62), (0.0, 0.95)),
]


def heart_path(cx, cy, width, rot_deg=0.0) -> str:
    s = width / 2.0
    a = math.radians(rot_deg)
    ca, sa = math.cos(a), math.sin(a)

    def tr(p):
        x, y = p[0] * s, p[1] * s
        return (cx + x * ca - y * sa, cy + x * sa + y * ca)

    return emit(HEART, transform=tr, close=True)


# centre, width, tilt. Trail runs from behind the tail down to the lower left on a gentle curve.
HEARTS = [
    ((20.0, 41.5), 11.0, 0),
    ((11.5, 49.5), 8.2, 0),
    ((5.2, 56.5), 5.6, 0),
]


# ---------------------------------------------------------------- SVG assembly
XMLNS = 'xmlns="http://www.w3.org/2000/svg"'


def mark_inner(include_detail=True, fill=None) -> str:
    fa = f' fill="{fill}"' if fill else ""
    parts = [
        f'<path fill-rule="evenodd"{fa} d="{top_wing_path()}"/>',
        f'<path{fa} d="{body_path()}"/>',
        f'<path{fa} d="{heart_path(*HEARTS[0][0], HEARTS[0][1], HEARTS[0][2])}"/>',
        f'<path{fa} d="{heart_path(*HEARTS[1][0], HEARTS[1][1], HEARTS[1][2])}"/>',
    ]
    if include_detail:
        parts.append(f'<g class="detail"><path{fa} d="{heart_path(*HEARTS[2][0], HEARTS[2][1], HEARTS[2][2])}"/></g>')
    return "".join(parts)


def mark_svg() -> str:
    return (
        f'<svg {XMLNS} viewBox="0 0 64 64" fill="currentColor" aria-hidden="true">'
        "<!-- AmoreHireMePls mark. Hide .detail below 24px. -->"
        + mark_inner(True)
        + "</svg>\n"
    )


TILE_SCALE = 0.8
TILE_PAD = (64 - 64 * TILE_SCALE) / 2


def tile_svg(rx=14) -> str:
    return (
        f'<svg {XMLNS} viewBox="0 0 64 64" fill="currentColor" aria-hidden="true">'
        f'<rect width="64" height="64" rx="{rx}"/>'
        f'<g fill="#fff" transform="translate({f(TILE_PAD)} {f(TILE_PAD)}) scale({TILE_SCALE})">'
        + mark_inner(True)
        + "</g></svg>\n"
    )


def favicon_svg() -> str:
    # 32 box, fixed sage (browser favicons do not inherit currentColor), no smallest heart.
    s = TILE_SCALE / 2
    p = TILE_PAD / 2
    return (
        f'<svg {XMLNS} viewBox="0 0 32 32">'
        f'<rect width="32" height="32" rx="7" fill="{SAGE}"/>'
        f'<g fill="#fff" transform="translate({f(p)} {f(p)}) scale({f(s)})">'
        + mark_inner(False)
        + "</g></svg>\n"
    )


def square_tile_svg() -> str:
    """Full-bleed square (rx 0) for apple-touch-icon; iOS applies its own mask."""
    return tile_svg(rx=0)


# ---------------------------------------------------------------- preview sheet
SIZES = [16, 24, 32, 64, 128]


def preview_svg() -> str:
    pad = 24
    gap = 28
    label_h = 18
    row_h = 128 + label_h + 48
    width = pad * 2 + sum(SIZES) + gap * (len(SIZES) - 1) + 150
    height = pad * 2 + row_h * 3
    sym = (
        f'<symbol id="full" viewBox="0 0 64 64"><g fill="currentColor">{mark_inner(True)}</g></symbol>'
        f'<symbol id="sm" viewBox="0 0 64 64"><g fill="currentColor">{mark_inner(False)}</g></symbol>'
        f'<symbol id="tile" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="currentColor"/>'
        f'<g fill="#fff" transform="translate({f(TILE_PAD)} {f(TILE_PAD)}) scale({TILE_SCALE})">{mark_inner(True)}</g></symbol>'
        f'<symbol id="tilesm" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="currentColor"/>'
        f'<g fill="#fff" transform="translate({f(TILE_PAD)} {f(TILE_PAD)}) scale({TILE_SCALE})">{mark_inner(False)}</g></symbol>'
    )
    font = "font-family=\"Helvetica Neue, Helvetica, Arial, sans-serif\""
    rows = []
    specs = [
        ("sage on white", "#fff", SAGE, "full", "sm", "#333"),
        ("white on sage tile", "#fff", SAGE, "tile", "tilesm", "#333"),
        ("sage on dark", DARK, SAGE, "full", "sm", "#ddd"),
    ]
    for ri, (title, bg, color, big, small, label_col) in enumerate(specs):
        y0 = pad + ri * row_h
        rows.append(f'<rect x="0" y="{y0 - 8}" width="{width}" height="{row_h}" fill="{bg}"/>')
        rows.append(f'<text x="{pad}" y="{y0 + 12}" font-size="13" font-weight="700" fill="{label_col}" {font}>{title}</text>')
        x = pad + 150
        for sz in SIZES:
            sid = small if sz < 24 else big
            y = y0 + label_h + (128 - sz)
            rows.append(f'<use href="#{sid}" x="{x}" y="{y}" width="{sz}" height="{sz}" color="{color}"/>')
            rows.append(f'<text x="{x + sz / 2}" y="{y0 + label_h + 128 + 16}" font-size="11" text-anchor="middle" fill="{label_col}" {font}>{sz}px</text>')
            x += sz + gap
    return (
        f'<svg {XMLNS} xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="#fff"/>' + sym + "".join(rows) + "</svg>\n"
    )


def lockup_svg() -> str:
    # Sidebar-style lockup: 2 variants stacked (light sidebar, dark sidebar).
    w, h = 420, 176
    font = "font-family=\"-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica Neue, Helvetica, Arial, sans-serif\""
    out = [f'<svg {XMLNS} width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           f'<symbol id="full" viewBox="0 0 64 64"><g fill="currentColor">{mark_inner(True)}</g></symbol>']
    for i, (bg, fg, text_col) in enumerate([("#f6f7f4", SAGE, "#1f2421"), (DARK, SAGE, "#f2f4ef")]):
        y0 = i * (h / 2)
        out.append(f'<rect x="0" y="{y0}" width="{w}" height="{h / 2}" fill="{bg}"/>')
        out.append(f'<use href="#full" x="24" y="{y0 + 28}" width="32" height="32" color="{fg}"/>')
        out.append(f'<text x="66" y="{y0 + 50}" font-size="20" font-weight="700" letter-spacing="-0.2" fill="{text_col}" {font}>AmoreHireMePls</text>')
        out.append(f'<rect x="0" y="{y0 + 72}" width="{w}" height="1" fill="{fg}" opacity="0.25"/>')
    out.append("</svg>\n")
    return "".join(out)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mark.svg").write_text(mark_svg())
    (OUT / "mark-tile.svg").write_text(tile_svg())
    (OUT / "favicon.svg").write_text(favicon_svg())
    (OUT / "_apple-square.svg").write_text(square_tile_svg())
    (OUT / "_preview.svg").write_text(preview_svg())
    (OUT / "_lockup.svg").write_text(lockup_svg())
    for name in ["mark.svg", "mark-tile.svg", "favicon.svg"]:
        print(name, len((OUT / name).read_bytes()), "bytes")
