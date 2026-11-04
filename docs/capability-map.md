# Capability map (as of 2026-11-04)

Status meanings: **implemented** = code exists; **fixture-tested** = covered by deterministic tests (doubles, local fixture site, local bare git remote, dev Mongo); **live-verified** = exercised against the real external system. Nothing in this project is live-verified for submissions or messages; this build was not authorization to do so.

| Stage | Capability | Status | Evidence / limits |
|---|---|---|---|
| Profile | Import approved facts/compensation/preferences with provenance into protected Mongo store | implemented, fixture-tested | `tests/test_profile.py` with fictional skill text; real skills importable via `amore profile import` (not run; private). |
| Profile | Experience computed from dates, internships separated, never rounded up | fixture-tested | `compute_experience` |
| Profile | Base resume selection (DOCX only, hashed, copied privately; old tailored resumes never used) | fixture-tested | Real base DOCX not found on this Mac (open question). |
| History | Tracker preview/import, idempotent re-import, provenance, duplicates and conflicts preserved | fixture-tested + verified against the real tracker clone read-only (39 records, 1 duplicate identity) | `tests/test_tracker_import.py`; `amore tracker preview` smoke run. No writes to the tracker. |
| Discovery | URL/data import, Greenhouse/Lever/Ashby feed adapters, snapshots with posting hash | implemented; adapters fixture-tested with canned JSON | Not live-verified against vendor APIs. |
| Discovery | Dedup by requisition/LinkedIn ID/normalized URL; soft duplicate flags | fixture-tested, including concurrent upserts | `tests/test_discovery.py`, `tests/test_db.py` |
| Discovery | Deterministic filters (employer exclusions incl. RBC/FAANG, employment type, role family, Ontario/remote, salary floor/currency, startup signals, prompt injection) before inference | fixture-tested | |
| Discovery | Structured fit ranking with evidence, ungrounded points dropped | fixture-tested with `FakeProvider` | Live model calls need `AMORE_MODEL` + key (not verified here). |
| Tailoring | Grounded DOCX from base with cited bullets, hashes, filename convention, ≤2 pages | fixture-tested (DOCX); PDF page verification requires LibreOffice | Mac lacks LibreOffice: reports `unverified`; the built Docker image contains `/usr/bin/soffice` (verified with `docker run`). |
| Applications | Review package: answers with fact IDs, sensitive fields blank, salary by rule, gaps, outstanding unknowns, material hash | fixture-tested | |
| Applications | Playwright adapter: multi-step form, upload with filename verification, validation errors, receipt verification, timeout → `needs_verification`, login → handoff, host allowlist | fixture-tested on local fixture site | Real ATS sites: generic form plan per job (dashboard) or handoff. No Workday/LinkedIn Easy Apply site-specific adapter. |
| Applications | Session export from Mac (`amore browser login`) loaded by headless worker | implemented; status/logout fixture-tested | Live login not performed. |
| Authorization | Single scope bound to job + material hash + host with expiry; bounded batch with filters and stop conditions; revoke; fail-closed checks | fixture-tested | |
| Actions | Idempotency key persisted before side effects; atomic reservation; replay returns recorded outcome; ambiguous → `needs_verification`; emergency stop | fixture-tested, including concurrent workers and restart | |
| Orchestration | LangGraph graph with MongoDBSaver, interrupts (`needs_input`, `awaiting_authorization`, `handoff`), pause/resume/cancel, restart recovery | fixture-tested on dev Mongo | |
| Sync | Outbox, single coordinator lock, `records.py add/validate`, scoped commits, rebase, push failure → `local_only`, crash reconcile | fixture-tested with local bare remote and a snapshot of `records.py` | Never pushed to the real tracker. |
| Outreach | Identity aliases, dedup across channels/imports/pending, unknown history blocks first contact, identity-based exclusions ("Jessica" unresolved), Connect-note rules (<300 chars, no hyphens, no layoff, mentions applied role) | fixture-tested | LinkedIn sending is a manual handoff (no supported API); email via SMTP only with verified alias + scope (SMTP not configured). |
| Dashboard | Password login, signed cookie, CSRF, all pages, single/batch runs, controls, emergency stop, no browser-control endpoints | fixture-tested with TestClient | |
| Worker | Claims runs atomically, resumes on dashboard actions, heartbeat health, America/Toronto discovery slots claimed once | fixture-tested | |
| Cost | Versioned skill catalog, exact-input cache keyed on fact version + posting hash + prompt version + model, coalescing, budgets, dated pricing, unknown cost reported as unknown | fixture-tested | `config/pricing.json` has no prices yet, so all costs show unknown until filled. |
| Ops | Backup/restore round trip, Compose with health checks/limits/volumes, redacted rotating logs, 1Password env references | fixture-tested (backup); `docker compose config` valid; image builds and runs `amore` as uid 10001 | NAS unreachable from this Mac; not deployed. |
