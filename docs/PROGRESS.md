# Progress

Keep this file current. A later session should be able to continue from the "Exact next step".

## Decisions

- Python 3.12 (not the Mac's 3.15) so Playwright, pydantic-core and the official Playwright Docker image match. `uv` manages the lockfile.
- One image, two processes: `amore web` (FastAPI + dashboard) and `amore worker` (LangGraph runs, sync coordinator, scheduler). Compose runs them as two services from the same image.
- MongoDB is the only database. NAS target is the existing `mongo:8.0` standalone (8.0.34 observed; no replica set, so no transactions or change streams are assumed). Dev profile `dev-mongo` runs the same image locally on 127.0.0.1:27027.
- LangGraph checkpointer: `langgraph-checkpoint-mongodb` `MongoDBSaver(client, db_name=...)` in the application database, collections `lg_checkpoints` / `lg_checkpoint_writes`.
- Side effects never happen inside a graph node directly on replay: nodes persist an `action` document with an idempotency key and reserve it atomically before the adapter runs. An `executing` action is never taken over by another worker; an expired lease becomes `needs_verification` (sweeper in the worker tick).
- Interactive browser access: **Mac companion session export**. `amore browser login <site> <url>` saves Playwright `storage_state` under `runtime/private/sessions/`; the worker loads it headless. Login challenges become `handoff` interrupts.
- Default model provider is **OpenAI** (`AMORE_MODEL_PROVIDER=openai`; `AMORE_MODEL` bare name or `provider:model`; Anthropic selectable per run). Verified 2026-11-04 with `gpt-5.4-mini`.
- Secrets: 1Password vault **`AmoreHireMePls`**; template `config/op.env.example`; `op run --env-file .env.references -- <cmd>` on the Mac; read-only service-account token in a protected file on the NAS. The Personal NAS root login is never used by the app (used once via `op run` to create the app database user).
- Recipient exclusions: no named exclusion by default. Unknown contact history fails closed until reconciled; RBC and FAANG stay as configurable employer rules.
- No private facts in source: employment, education, answers and the outreach candidate summary come from the private skills, overrides JSON and the approved base resume at runtime. `tests/test_regressions.py` scans tracked files against hashed private tokens.
- Ruff: line length 180, `ruff format` applied to the whole tree.

## Done (local commits)

- 99ad8b8 … 6957110 Increments 1–12: skeleton, store, profile, authorization/actions, discovery, tailoring, browser adapter, applications, sync, LangGraph graph, outreach, runtime/worker/backup/dashboard/CLI, docs.
- 2749ac2 Verifier fixes H1–H2, M1–M6, L1–L8 with regression tests (no private facts in source; no lease takeover; batch scopes only for `application_submit` with a job; retryable failures return to `ready`; receipt identifiers recorded only when matched; orphaned runs recovered at worker startup; rebase conflict aborts; scoped `git clean`; exact CSRF origin; minimal `/health` + authenticated `/status`; upload paths under the artifact dir; `Message-ID`; Playwright seccomp profile verified in the hardened container; doc drift).
- e42c419 Default provider OpenAI; `config/op.env.example`; `op run` injection verified with dummy values.
- 5578f2f Profile import merges the same job across skill and base resume by kind+start month (no double counted experience); unknowns refresh.
- 8b9ce0b Smoke-run fixes: approved base summary passes grounding verbatim; ungrounded cached artifacts are re-tailored; cancel on idle runs settles in the worker tick; provider check reports usage.
- (this commit) NAS: app database user created, Mongo-marked tests run against the NAS, `docs/nas-deployment.md` rewritten from observations, Compose `extra_hosts`; PROGRESS/capability map.

## First real setup on the Mac (2026-11-04, dev Mongo, nothing live)

- Profile import from the three private skills: 11 facts, 4 answers, 2 education rows, 4 employment rows (1 full time, 3 internships from the base resume), salary floor set, 1 unknown (education dates/GPA). Stored under `runtime/private`, never in the repo.
- Base resume registered: `runtime/private/base_resume/…docx`, sha256 `44b2edba…`, approved 2026-11-04, note "selected by Joss in chat". Experience computed from it: 3 full-time years completed, 9 internship months.
- Tracker: cloned read-only (6 commits, head 11cf8f6, 2026-10-15). Import: 39 records (22 submitted, 9 blocked, 4 researched, 2 prepared, 2 skipped), 38 jobs created (one LinkedIn id appears in two records), 0 conflicts, outbox empty, nothing pushed.
- Dashboard smoke: logged in; `/`, `/runs`, `/profile`, `/history`, `/jobs`, `/authorizations`, `/outreach`, `/sync`, `/usage` all HTTP 200 with their page content; `/health` minimal; `/status` reports Mongo 8.0.34 and worker heartbeat.
- One real public posting imported (Faire, Product Engineer - Ads, Greenhouse req 8831748002, Toronto): qualification `needs_review` (title outside preferred role families; salary unknown). Single-job run produced a review package with a tailored DOCX from the real base resume (grounding ok, 13 bullets checked) and halted at `awaiting_authorization`; 0 actions, 0 authorizations, nothing submitted. PDF rendered inside the hardened container: 2 pages (base resume also 2 pages), selectable text, within the two-page limit. Form scan against the live Greenhouse page failed as expected because `AMORE_BROWSER_ALLOWED_HOSTS` is empty.
- Provider check through `op run` with the vault key: `gpt-5.4-mini` answered "OK"; 13 input + 4 output tokens.
- Two defects surfaced by the smoke run and fixed (8b9ce0b): the real base summary contains a dash and was failing grounding; a cancel request on a waiting run was never processed.

## NAS (2026-11-04, over Tailscale, no SSH)

- MongoDB 8.0.34 standalone, auth enabled, `bindIp *`, reachable at 100.90.96.81:27017 on the tailnet (~5 ms per command). Databases before this project: admin/config/local only.
- Created database user `amorehiremepls_app` (readWrite on `amorehiremepls`; authSource `amorehiremepls`); password generated by 1Password into vault `AmoreHireMePls`, item `NAS MongoDB App User`. Boundaries verified (no admin read, no dropDatabase).
- Mongo-marked tests against the NAS with that user and throwaway database `amorehiremepls_test`: **55 passed** (about 10 minutes; one round trip per command). Throwaway databases dropped afterwards and the temporary test roles revoked; the NAS now holds only admin/config/local plus the empty `amorehiremepls` user definition.

## Verification status

- `uv run pytest`: 76 passed on the Mac (dev Mongo + Chromium present).
- Mongo-marked tests against the NAS (`AMORE_TEST_MONGODB_URI` + `AMORE_TEST_MONGODB_DATABASE=amorehiremepls_test`, app user): 55 passed, exit 0.
- `uv run ruff check src tests` and `ruff format --check`: clean. `docker compose config`: valid. Chromium + LibreOffice verified inside the hardened container.
- Live: nothing. No application submitted, no message sent, no tracker push, no NAS deployment of the app containers.

## Remaining / not done

- 1Password items still to create in `AmoreHireMePls`: `Dashboard Password`, `App Session Secret` (names/fields in `config/op.env.example`).
- `config/pricing.json` prices for `gpt-5.4-mini` (and any other model used); until filled every cost reads "unknown".
- Deploy the app containers to the NAS (needs SSH enabled in UGOS for the rsync/build window) or run the app on the Mac against the NAS database (works today).
- Tailscale ACL limiting `tcp:27017` on the NAS to Joss's devices (observation: port is open to the whole tailnet, auth required).
- `AMORE_BROWSER_ALLOWED_HOSTS` for the ATS hosts Joss wants scanned (e.g. `boards.greenhouse.io`).
- Site-specific adapters beyond the generic form plan (Workday, LinkedIn Easy Apply) if wanted; currently handoffs. Optional SMTP channel.

## Open questions for Joss

1. Should `Product Engineer` style titles count as a preferred role family, or stay `needs_review`? (Affects qualification of the Faire posting and similar ones.)
2. Which ATS hosts may the worker open for form scanning (`AMORE_BROWSER_ALLOWED_HOSTS`)?

## Exact next step

Create the two remaining vault items, fill `config/pricing.json` for `gpt-5.4-mini`, then run the app on the Mac against the NAS database: `.env` with `AMORE_MODEL=gpt-5.4-mini`, `AMORE_MONGODB_URI=mongodb://100.90.96.81:27017/?directConnection=true`, `AMORE_MONGODB_AUTH_SOURCE=amorehiremepls`; `op run --env-file .env.references -- uv run amore setup-db`, re-run `amore profile import` + `amore profile base-resume` + `amore tracker import` against it (the dev Mongo data is local only), start web + worker through `op run`, and review the Faire package in the dashboard. Deploy containers to the NAS once SSH is enabled.
