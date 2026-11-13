"""The stop record as the application page shows it.

Everything read here came from a web page. It is passed to the templates as plain values and shown as escaped
text: never as markup, never as a link that could be followed by accident.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from amorehiremepls.apply.stop_record import BODY_CAP, DIAGNOSTIC_SHOTS, FILE_CAP, FILES, KIND
from amorehiremepls.core.util import sha256_file

SHOW_ROWS = 300  # rows drawn on the page; the request list file has all of them
SHOW_BODY = 4096  # bytes of one body drawn on the page
STOPPED = ("handoff", "failed", "needs_verification")
NOTICE = "This record is complete. It can include passwords, verification codes and session cookies."
WHERE = "It is kept in private storage on the computer that ran the attempt, and it is deleted when a new attempt starts."


def human_size(n: Any) -> str:
    if not isinstance(n, (int, float)) or n < 0:
        return "size unknown"
    for unit, step in (("bytes", 1), ("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if n < step * 1024 or unit == "GB":
            return f"{int(n)} bytes" if unit == "bytes" else f"{n / step:.1f} {unit}"
    return f"{n} bytes"


LIMITS = (
    f"A text body over {human_size(BODY_CAP)} is cut in the request list and whole in the network archive. "
    "A picture or other binary body is listed by size and type and kept in the archive. "
    f"An archive or trace over {human_size(FILE_CAP)} is not kept."
)


def _local_path(rt, art: dict | None) -> Path | None:
    """The file, when it is on this computer and inside the artifact folder."""
    if not art or not art.get("path"):
        return None
    path = Path(str(art["path"])).resolve()
    if not path.is_relative_to(rt.settings.artifact_dir.resolve()) or not path.is_file():
        return None
    return path


def _read_json(rt, art: dict | None) -> Any | None:
    """Parse one of the record's JSON files, only while it still matches the hash saved with it."""
    path = _local_path(rt, art)
    if path is None or sha256_file(path) != art.get("sha256"):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _records(rt, a: dict) -> list[dict]:
    query = {"kind": KIND, "job_id": a.get("job_id"), "application_id": {"$in": [a.get("_id"), None]}}
    return list(rt.store.c("artifacts").find(query).sort("created_at", -1))


def stop_record_view(rt, a: dict) -> dict:
    """What the application page needs: the newest record's pictures, counts and downloads. No request rows."""
    arts = _records(rt, a)
    if not arts:
        return {"state": "none", "expected": a.get("status") in STOPPED}
    attempt = arts[0].get("attempt")
    mine = {x.get("label"): x for x in arts if x.get("attempt") == attempt}
    summary = (mine.get("record") or {}).get("summary") or {}
    manifest = _read_json(rt, mine.get("record")) or {}
    page = manifest.get("page") or {}

    def href(label: str, kind: str, inline: bool = False) -> str | None:
        art = mine.get(label)
        return f"/documents/{art['_id']}/download?kind={kind}{'&inline=1' if inline else ''}" if art else None

    files = []
    for name, (label, title, _media) in FILES.items():
        art = mine.get(label)
        if not art or label in ("view", "page"):
            continue
        files.append({"title": title, "name": name, "href": href(label, "file"), "size": human_size(art.get("size")), "here": _local_path(rt, art) is not None})
    counts = summary.get("counts") or {}
    return {
        "state": "saved",
        "attempt": attempt,
        "captured_at": arts[0].get("captured_at") or arts[0].get("created_at"),
        "here": bool(manifest),
        "counts": counts,
        "problems": int(counts.get("failed") or 0) + int(counts.get("blocked") or 0),
        "notes": list(summary.get("notes") or []),
        "errors": list(summary.get("errors") or []),
        "view": href("view", "png", True) if _local_path(rt, mine.get("view")) else None,
        "full": href("page", "png", True) if _local_path(rt, mine.get("page")) else None,
        "has_picture": bool(mine.get("view") or mine.get("page")),
        "files": files,
        "page_url": str(page.get("url") or ""),
        "page_title": str(page.get("title") or ""),
        "older": len({x.get("attempt") for x in arts}) - 1,
        "limits": LIMITS,
    }


def _body_view(body: dict | None) -> dict:
    body = body or {}
    text = str(body.get("text") or "")
    raw = text.encode("utf-8", "replace")
    shown = raw[:SHOW_BODY].decode("utf-8", "ignore") if len(raw) > SHOW_BODY else text
    state = body.get("state") or "none"
    size = human_size(body.get("size"))
    if state == "binary":
        note = f"{size}, {body.get('mime') or 'no type given'}. Not text, so it is not shown. The bytes are in the network archive."
    elif state == "unreadable":
        note = "This body could not be read from the archive."
    elif state == "none":
        note = "" if not body.get("size") else f"{size}, {body.get('mime') or 'no type given'}. The site sent no readable body."
    elif len(raw) > SHOW_BODY or state == "cut":
        where = "the network archive" if state == "cut" else "the request list file"
        note = f"Showing the first {human_size(len(shown.encode('utf-8')))} of {size}. The whole body is in {where}."
    else:
        note = f"{size}, {body.get('mime') or 'no type given'}."
    return {"text": shown, "note": note, "mime": str(body.get("mime") or "")}


def _row_view(row: dict) -> dict:
    status = row.get("status")
    if row.get("blocked"):
        tone, mark = "warn", "refused"
    elif row.get("failed"):
        tone, mark = "bad", "failed"
    elif isinstance(status, int) and status >= 400:
        tone, mark = "bad", str(status)
    else:
        tone, mark = "", str(status if status is not None else "")
    took = row.get("time_ms")
    return {
        "n": row.get("n"),
        "tone": tone,
        "mark": mark,
        "method": str(row.get("method") or ""),
        "url": str(row.get("url") or ""),
        "type": str(row.get("type") or ""),
        "time": f"{took:g} ms" if isinstance(took, (int, float)) else "",
        "started": str(row.get("started") or ""),
        "size": human_size((row.get("response_body") or {}).get("size")) if (row.get("response_body") or {}).get("size") else "",
        "status_text": str(row.get("status_text") or ""),
        "failure": str(row.get("failure") or ""),
        "redirect_to": str(row.get("redirect_to") or ""),
        "redirected_from": row.get("redirected_from"),
        "server": str(row.get("server") or ""),
        "http": str(row.get("http") or ""),
        "request_headers": [(str(a), str(b)) for a, b in row.get("request_headers") or []],
        "response_headers": [(str(a), str(b)) for a, b in row.get("response_headers") or []],
        "cookies_sent": [(str(a), str(b)) for a, b in row.get("cookies_sent") or []],
        "cookies_set": [str(x) for x in row.get("cookies_set") or []],
        "form_fields": [(str(a), str(b)) for a, b in row.get("form_fields") or []],
        "request_body": _body_view(row.get("request_body")),
        "response_body": _body_view(row.get("response_body")),
        "problem": bool(row.get("blocked") or row.get("failed")),
    }


def stop_network_view(rt, a: dict) -> dict:
    """The request list and the console lines of the newest record, for the panel."""
    arts = _records(rt, a)
    if not arts:
        return {"state": "none"}
    attempt = arts[0].get("attempt")
    mine = {x.get("label"): x for x in arts if x.get("attempt") == attempt}
    network = _read_json(rt, mine.get("network"))
    console = _read_json(rt, mine.get("console")) or {}
    if network is None:
        return {"state": "elsewhere"}
    rows = [_row_view(r) for r in network.get("rows") or []]
    problems = [r for r in rows if r["problem"]]
    return {
        "state": "saved",
        "total": len(rows),
        "rows": rows[:SHOW_ROWS],
        "left_out": max(0, len(rows) - SHOW_ROWS),
        "problems": problems[:SHOW_ROWS],
        "failed": sum(1 for r in rows if r["mark"] == "failed"),
        "refused": sum(1 for r in rows if r["mark"] == "refused"),
        "errors": sum(1 for r in rows if r["tone"] == "bad" and r["mark"] != "failed"),
        "console": [
            {"type": str(c.get("type") or ""), "text": str(c.get("text") or ""), "at": str(c.get("at") or ""), "url": str(c.get("url") or "")}
            for c in (console.get("lines") or [])[:500]
        ],
    }


def split_proof(shots: list[dict]) -> tuple[list[dict], list[dict]]:
    """Proof of applying apart from pictures of where an unsent attempt stopped."""
    proof = [s for s in shots if s.get("label") not in DIAGNOSTIC_SHOTS]
    stopped = [s for s in shots if s.get("label") in DIAGNOSTIC_SHOTS]
    return proof, stopped
