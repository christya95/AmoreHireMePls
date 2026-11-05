"""Rotating, redacted application logs. Secrets and obvious credentials never reach the log files."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACTIONS = [
    (re.compile(r"(mongodb(?:\+srv)?://)([^:@/\s]+):([^@/\s]+)@"), r"\1***:***@"),
    (re.compile(r"(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+"), r"\1***"),
    (re.compile(r"(?i)(api[_-]?key|password|secret|token|authorization)(\"?\s*[:=]\s*\"?)([^\s\"',;]+)"), r"\1\2***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}\b"), "gh*_***"),
    (re.compile(r"ops_[A-Za-z0-9]{10,}"), "ops_***"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(log_dir: Path | None, level: int = logging.INFO, name: str = "app") -> None:
    root = logging.getLogger()
    if getattr(root, "_amore_configured", False):
        return
    root.setLevel(level)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / f"{name}.log", maxBytes=5_000_000, backupCount=5)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root._amore_configured = True  # type: ignore[attr-defined]
