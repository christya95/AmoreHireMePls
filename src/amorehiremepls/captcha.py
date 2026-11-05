"""Opt-in automatic CAPTCHA step for the application-submit path.

Off by default (`AMORE_CAPTCHA_AUTOSOLVE=false`, Settings toggle "Try to solve captchas automatically"). When off, a
human check pauses the application as a handoff. When on, the browser adapter hands a simple challenge widget
(a text image or an image grid with an instruction) to a `CaptchaSolver`; the model based solver sends the picture
and the instruction to the configured vision capable model through `AIRunner` (skill `solve_captcha`), so the usual
token / spend budgets, exact-input cache and usage records apply. Attempts are capped per application
(`AMORE_CAPTCHA_MAX_ATTEMPTS`, default 3); every attempt is recorded on the application evidence. Score based or
unsupported checks (reCAPTCHA v3 / Enterprise invisible, hCaptcha, Turnstile, audio) always fall back to the human
check. Nothing here runs for logins, discovery or outreach.

Approach credit: the picture-plus-instruction prompting pattern follows aydinnyunus/ai-captcha-bypass
(https://github.com/aydinnyunus/ai-captcha-bypass). Its code is not vendored or imported: it is a Selenium CLI
under a non-commercial, research-only licence, so this module is an independent implementation.
Joss accepted the platform-terms risk of this feature on 2026-11-05 (see AGENTS.md).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

SUPPORTED_KINDS = ("text_captcha", "image_grid")


@dataclass
class ChallengeWidget:
    kind: str  # text_captcha | image_grid
    selector: str  # the widget's container
    image_png: bytes
    instruction: str  # untrusted text shown next to the picture
    input_selector: str
    submit_selector: str
    grid: tuple[int, int] | None = None  # rows, cols for image grids


@dataclass
class CaptchaAnswer:
    kind: str  # text | cells | cannot
    text: str = ""
    cells: list[int] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class CaptchaSolver(Protocol):
    def solve(self, widget: ChallengeWidget) -> CaptchaAnswer: ...


def read_challenge(scope: Any, challenge: dict) -> ChallengeWidget:
    """Read one visible challenge widget: its picture, instruction, answer box and confirm button."""
    sel = challenge["selector"]
    container = scope.locator(sel).first
    kind = challenge["kind"]
    instr = container.locator("[data-captcha-instruction]").first
    instruction = (instr.inner_text() if instr.count() else container.inner_text())[:300]
    image = container.locator("[data-captcha-image], [data-captcha-grid], img").first
    png = image.screenshot() if image.count() else container.screenshot()
    grid = None
    if kind == "image_grid":
        cells = container.locator("[data-cell]").count()
        cols = container.get_attribute("data-grid-cols")
        c = int(cols) if cols and cols.isdigit() else (3 if cells in (9, 6, 3) else 4)
        grid = (max(1, cells // c), c)
    return ChallengeWidget(
        kind=kind,
        selector=sel,
        image_png=png,
        instruction=instruction,
        input_selector=f"{sel} input[type=text], {sel} input:not([type=hidden]):not([type=checkbox])",
        submit_selector=f"{sel} [data-captcha-submit], {sel} button[type=submit], {sel} button",
        grid=grid,
    )


class ModelCaptchaSolver:
    """Sends the challenge picture and instruction to the configured model through `AIRunner`."""

    def __init__(self, ai: Any) -> None:
        self.ai = ai

    def solve(self, widget: ChallengeWidget) -> CaptchaAnswer:
        from .skills import CaptchaInput

        inputs = CaptchaInput(
            kind=widget.kind,
            instruction=widget.instruction,
            grid_rows=widget.grid[0] if widget.grid else 0,
            grid_cols=widget.grid[1] if widget.grid else 0,
            image_sha256=hashlib.sha256(widget.image_png).hexdigest(),
        )
        res = self.ai.run("solve_captcha", inputs, images=[widget.image_png], untrusted_fields=("instruction",))
        out = res.output
        kind = out.get("kind", "cannot")
        cells = [int(c) for c in out.get("cells") or [] if isinstance(c, int) or str(c).isdigit()]
        if widget.grid:
            cells = [c for c in cells if 0 <= c < widget.grid[0] * widget.grid[1]]
        return CaptchaAnswer(kind=kind, text=(out.get("text") or "").strip(), cells=cells, usage=res.usage.model_dump(exclude_none=True))


class NeverSolver:
    """Test double / safety default: refuses every challenge and counts the calls."""

    def __init__(self) -> None:
        self.calls = 0

    def solve(self, widget: ChallengeWidget) -> CaptchaAnswer:
        self.calls += 1
        return CaptchaAnswer(kind="cannot")
