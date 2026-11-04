# Progress

Keep this file current. A later session should be able to continue from the "Exact next step".

## Decisions

- Python 3.12 (not the Mac's 3.15) so Playwright, pydantic-core and the official Playwright Docker image match. `uv` manages the lockfile.
- One image, two processes: `amore web` (FastAPI + dashboard) and `amore worker` (LangGraph runs, sync coordinator, scheduler). Compose runs them as two services from the same image.
- MongoDB is the only database. NAS target is the existing `mongo:8.0` standalone (no replica set, so no transactions or change streams are assumed). Dev profile `dev-mongo` runs the same image locally on 127.0.0.1:27027.
- LangGraph checkpointer: `langgraph-checkpoint-mongodb` `MongoDBSaver(client, db_name=...)` in the application database, collections `lg_checkpoints` / `lg_checkpoint_writes`.
- Side effects never happen inside a graph node directly on replay: nodes persist an `action` document with an idempotency key and reserve it atomically (`findOneAndUpdate` with state guard) before the adapter runs. Replay finds the reservation and reads the recorded outcome.
- Interactive browser access: **Mac companion session export**. `amore browser login <site> <url>` runs headed Chromium locally and saves Playwright `storage_state` under `runtime/private/sessions/`; the worker loads it headless. Login challenges become `handoff` interrupts. No remote-control endpoint exists.
- Secrets: `op run --env-file .env.references -- <cmd>` on the Mac; read-only service-account token in a protected file on the NAS. `.env.example` holds non-secret config only.
- Recipient exclusion "Jessica" is stored as an identity exclusion with `resolution: unresolved`; it never matches by first name; same-first-name recipients are held for reconciliation.
- Ruff: line length 180, `ruff format` applied to the whole tree (commit a258e66).

## Done (local commits)

- 99ad8b8 Increment 1: sources inspected, `docs/workflow-sources.md`, skeleton, lockfile, Compose, Dockerfile, `.env.example`.
- c08edce Increment 2: config, Mongo store, indexes, atomic reserve/release, logging redaction, util.
- a4616ca Increment 3: private profile with provenance, experience computation, salary rule, base resume selection, tracker import preview/import (idempotent; verified read-only against the real clone: 39 records).
- 15bf25d Increment 4a: authorization scopes, idempotent actions, emergency stop.
- a6ae9ba Increments 5–7, 10: discovery adapters + dedup + deterministic filters + fit ranking; grounded tailoring (DOCX/PDF); Playwright adapter + fixture site; application review packages and execution; tracker sync coordinator/outbox; skill catalog, cache, budgets, pricing, usage.
- cb8b468 / 2203dd4 Increments 4b, 8: LangGraph application graph with MongoDBSaver, interrupts, pause/cancel/restart; recruiter identities, dedup, drafts, channels.
- a258e66 Increments 9–11: runtime wiring, worker with America/Toronto schedule slots, backup/restore, dashboard (auth + CSRF, all pages, controls), CLI, formatting; 62 tests.
- (this commit) Increment 12: README, backup/restore, troubleshooting, NAS deployment, capability map, AGENTS commands. Docker image built locally (`amorehiremepls:local`, soffice present, uid 10001).

## Verification status

- `uv run pytest`: 62 passed (dev Mongo at 127.0.0.1:27027 and Chromium present). Without dev Mongo, mongo-marked tests skip.
- `uv run ruff check src tests` and `ruff format --check`: clean.
- `docker compose config`: valid. `docker compose build web`: ok.
- Live: nothing. No application submitted, no message sent, no tracker push, no NAS deployment.

## Remaining / not done

- NAS deployment (host unreachable from this Mac during the build): follow `docs/nas-deployment.md`.
- Real profile import (`amore profile import` with the private skill paths) and base resume selection: needs the approved DOCX.
- `config/pricing.json` prices for the chosen model(s); until filled every cost reads "unknown".
- Site-specific adapters beyond the generic form plan (Workday, LinkedIn Easy Apply) if wanted; currently handoffs.
- Optional: email channel (SMTP) configuration for recruiter email; LinkedIn remains manual.

## Open questions for Joss

1. Base resume: only `iCloud Drive/Downloads/Joss_Christyanton_Resume.pdf` (2026-07-28) was found; no DOCX. Where is the current approved base DOCX, or should a new base be built from the PDF text after your review?
2. NAS: `192.168.2.49` was unreachable (SSH 22 and 8787 closed, no tunnel). Is the NAS online, and is SSH enabled in UGOS for the deployment window?
3. Which provider/model should be the default (OpenAI or Anthropic), and which 1Password vault holds this project's keys?
4. Recipient exclusion "Jessica": which LinkedIn profile URL/employer is meant?

## Exact next step

Run the first real setup on the Mac against dev Mongo: `uv run amore profile import --facts-skill … --salary-skill …`, select the base resume once its DOCX location is known, `amore tracker clone && amore tracker import`, then start `amore web` + `amore worker` and import one real posting URL to review a package (no authorization granted). Deploy to the NAS when it is reachable.
