"""Page motion is local CSS only, and reduced motion turns movement off."""

from pathlib import Path

STATIC = Path("src/amorehiremepls/web/static")


def test_motion_is_first_party_and_reduced_motion_snaps_movement():
    css = (STATIC / "app.css").read_text()
    js = (STATIC / "app.js").read_text()
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation-duration: 0.01ms" in css
    assert "draw-ring" in css and "flash-in" in css and "rise" in css
    assert "--ease-in:" in css
    assert "animation: rise var(--enter) var(--ease-in) backwards" in css
    assert ".next-action.working .working-line .dots i" in css
    assert "animation: none !important" in css
    assert "replayEntrance" in js
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in js
    assert "data-copy" in js and "activity-lines" in js
    assert "network-lines" not in js
    entrance = js[js.index("var entrance = ") : js.index(";", js.index("var entrance = "))]
    assert "working-dot" not in entrance and ".login-card" in entrance
    blob = css + js
    assert "lottie" not in blob.lower()
    assert "cdn." not in blob.lower()
    assert "http://" not in blob and "https://" not in blob
    for path in STATIC.rglob("*"):
        if path.suffix.lower() in {".js", ".css", ".json", ".map"} and path.is_file():
            text = path.read_text(errors="ignore").lower()
            assert "lottie" not in text
            assert "gsap" not in text
            assert "framer-motion" not in text
