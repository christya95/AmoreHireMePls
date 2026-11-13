"""Submission screenshots shown on the job, application and documents pages."""

from __future__ import annotations

from amorehiremepls.apply.stop_record import DIAGNOSTIC_SHOTS
from amorehiremepls.web.chrome import SCREENSHOT_LABELS, humanize, normalize_screenshots


def split_screenshots(arts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Tailored documents and submission screenshots are shown apart: screenshots get no DOCX/PDF buttons."""
    shots = [describe_shot(a) for a in arts if a.get("kind") == "screenshot" and a.get("label") not in DIAGNOSTIC_SHOTS]
    docs = [a for a in arts if a.get("kind") not in ("screenshot", "stop_record")]
    return docs, shots


def describe_shot(a: dict) -> dict:
    label = str(a.get("label") or "screenshot")
    title, meaning = SCREENSHOT_LABELS.get(label, (humanize(label).capitalize(), ""))
    return {**a, "label": label, "title": title, "meaning": meaning, "artifact_id": a.get("artifact_id") or a.get("_id")}


def evidence_shots(rt, a: dict) -> list[dict]:
    """Submission captures for one application: the structured evidence list (legacy path lists are widened), each
    joined to its `screenshot` artifact so the hash-verified route can show it. Captures whose file never landed stay
    listed without a link."""
    raw = (a.get("evidence") or {}).get("screenshots") or []
    shots = normalize_screenshots(raw)
    given = {r.get("sha256"): r.get("artifact_id") for r in raw if isinstance(r, dict) and r.get("artifact_id")}
    arts = {x["sha256"]: x for x in rt.store.c("artifacts").find({"kind": "screenshot", "application_id": a["_id"]}, {"content": 0})}
    out = []
    seen = set()
    for s in shots:
        art = arts.get(s.get("sha256"))
        aid = given.get(s.get("sha256")) or (art or {}).get("_id")
        seen.add(aid)
        out.append(describe_shot({**(art or {}), **s, "_id": aid, "artifact_id": aid}))
    for art in arts.values():  # registered captures missing from the evidence list (should not happen; shown anyway)
        if art["_id"] not in seen:
            out.append(describe_shot({**art, "artifact_id": art["_id"]}))
    order = {"summary": 0, "receipt": 1, "ambiguous": 2}
    out.sort(key=lambda s: (order.get(s["label"], 9), str(s.get("captured_at") or "")))
    return out
