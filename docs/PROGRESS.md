# Progress

Keep this file current. A later session should be able to continue from the "Exact next step".

## Decisions

- Python 3.12 (not the Mac's 3.15) so Playwright, pydantic-core and the official Playwright Docker image match. `uv` manages the lockfile.
- One image, two processes: `amore web` (FastAPI + dashboard) and `amore worker` (LangGraph runs, scheduler). Compose runs them as two services from the same image.
- MongoDB is the only database. NAS target is the existing `mongo:8.0` standalone (no replica set, so no transactions or change streams are assumed). Dev profile `dev-mongo` runs the same image locally.
- LangGraph checkpointer: `langgraph-checkpoint-mongodb` `MongoDBSaver(client, db_name=...)` in the application database (`amorehiremepls`), collections `lg_checkpoints` / `lg_checkpoint_writes`.
- Side effects never happen inside a graph node directly on replay: nodes persist an `action` document with an idempotency key and reserve it atomically (`findOneAndUpdate` with state guard) before the adapter runs. Replaying a checkpoint re-enters the node, finds the reservation and reads the recorded outcome instead of acting again.
- Interactive browser access: the chosen approach is a **Mac companion session export**. The authenticated browser is the Playwright Chromium launched locally by `amore browser login <site>` on the Mac; it saves a Playwright `storage_state` JSON under `runtime/private/sessions/` which the worker loads. On the NAS, the worker runs headless with that state; when a login challenge appears, the action becomes a `handoff` and the dashboard asks Joss to refresh the session from the Mac. No remote-control endpoint is exposed.
- Secrets: `op run --env-file .env.references -- <cmd>` on the Mac; a read-only service-account token in a protected file on the NAS (same pattern as AmoreGymBro). `.env.example` holds non-secret config only.
- Recipient exclusion "Jessica" is stored as an identity exclusion with `resolution: unresolved`; it blocks nothing until bound to a profile URL/employer, and it never matches by first name.

## Done

- Increment 1: sources inspected; `docs/workflow-sources.md` written; project skeleton, lockfile, Compose, Dockerfile, `.env.example`.

## In progress / remaining (build-sequence order)

- Increment 2: config, Mongo access, index setup, health endpoints, logging.
- Increment 3: private profile + tracker import preview/import.
- Increment 4: orchestration (LangGraph + MongoDBSaver), reservations, authorization scopes, idempotent actions, emergency stop.
- Increment 5: discovery adapters, dedup, filters, fit ranking, tailoring.
- Increment 6: Playwright adapter + fixture site, review package, handoffs.
- Increment 7: tracker sync coordinator/outbox against a local bare repo.
- Increment 8: outreach identities, dedup, drafts, channels.
- Increment 9: dashboard, auth/CSRF, scheduling, controls.
- Increment 10: cost controls (catalog, cache, budgets, usage).
- Increment 11: full test list; Mongo-backed tests.
- Increment 12: docs (README, backup/restore, troubleshooting, NAS deployment, capability map).

## Open questions for Joss

1. Base resume: only `iCloud Drive/Downloads/Joss_Christyanton_Resume.pdf` (2026-07-28) was found; no DOCX. Where is the current approved base DOCX, or should a new base be built from the PDF text after your review?
2. NAS: `192.168.2.49` was unreachable (SSH 22 and 8787 closed, no tunnel). Is the NAS online, and is SSH enabled in UGOS for the deployment window?
3. Which provider/model should be the default (OpenAI or Anthropic), and which 1Password vault holds this project's keys?
4. Recipient exclusion "Jessica": which LinkedIn profile URL/employer is meant?

## Exact next step

Implement increment 2 (`src/amorehiremepls/config.py`, `db.py`, `web/app.py` health, logging) and run `uv run pytest`.
