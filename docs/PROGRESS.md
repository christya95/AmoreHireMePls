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

- `uv run pytest`: 86 passed on the Mac (dev Mongo + Chromium present), after the dashboard redesign.
- Mongo-marked tests against the NAS (`AMORE_TEST_MONGODB_URI` + `AMORE_TEST_MONGODB_DATABASE=amorehiremepls_test`, app user): 55 passed, exit 0.
- `uv run ruff check src tests` and `ruff format --check`: clean. `docker compose config`: valid. Chromium + LibreOffice verified inside the hardened container.
- Live: nothing. No application submitted, no message sent, no tracker push, no NAS deployment of the app containers.

## Running on the Mac against the NAS database (2026-11-05)

- Vault `AmoreHireMePls` now holds `OpenAI API Key`, `NAS MongoDB App User`, `Dashboard Password`, `App Session Secret`. `.env` (non-secret settings) and `.env.references` (`op://` references only) exist locally, gitignored.
- NAS database `amorehiremepls` set up and populated: profile (11 facts, 4 answers, 2 education, 4 employment incl. 3 internships, 1 unknown), base resume sha `44b2edba…`, tracker import 39 records / 38 jobs / 1 duplicate identity / 0 conflicts (no push), plus the Faire posting (`needs_review`). 39 jobs total.
- `amore web` (http://127.0.0.1:8790) and `amore worker` run detached through `op run`; PID files and logs under `runtime/logs/`. Start/stop commands in `README.md`. Dev Mongo container kept as a local copy only.
- Page latency over Tailscale: 20 to 80 ms, `/history` about 400 ms.

## Dashboard redesign (2026-11-05)

- New design system in `src/amorehiremepls/static/app.css` (CSS custom properties, `--accent` with `color-mix()` derivations, light/dark tokens, rounded cards, pill nav, KPI tiles, status pills, tables, forms) and `app.js` (progressive enhancement only: instant theme toggle, accent live preview, popover close, double-submit guard). No CDN, fonts or third-party scripts; inline SVG icon sprite; dependency-free SVG line chart and donut computed in `ui_settings.py`.
- Information architecture: left sidebar (Work: Overview, Jobs, Applications, Runs, Documents, Outreach · Records: History, Sync, Usage · Account: Authorizations, Candidate profile, Settings), pill top bar with greeting/avatar menu, theme toggle and a two-step emergency stop. New pages `/applications` (local packages by status) and `/documents` (artifacts). Overview: greeting in America/Toronto, KPI tiles (jobs new/needs review, submitted/prepared, needs verification, sync pending, spend this month with unknown pricing shown as unknown), submissions-per-month chart from local + imported history, job pipeline donut, "needs your attention" list (awaiting authorization, needs input, handoff, needs verification, waiting/failed runs, needs_review jobs, held recipients) with direct links, system card, recent runs/activity/jobs. All existing routes and forms kept.
- Settings (`/settings`): theme (system/light/dark), accent presets + free hex, display name, avatar upload (PNG/JPEG/WebP ≤ 1 MB, header-sniffed, dimension-checked, stored under `AMORE_PRIVATE_DIR/avatar/`, served by the authenticated `/settings/avatar/image`) or initials style. Persisted in `controls.ui_settings`; theme+accent mirrored in the `amore_ui` cookie for first paint and the login page. All new POSTs require the CSRF token and same-origin.
- Tests: `tests/test_ui_settings.py` (helpers without Mongo; endpoint tests for persistence, cookie mirror, `next` validation, avatar rejection of wrong type / disguised bytes / oversize / tiny / non-image, serving and removal, CSRF/login enforcement, overview attention list and new pages). Existing web tests unchanged and passing.
- Screenshots (light + dark, 1280 and 390 wide) were taken from a dev instance on 8791 against the dev Mongo; the live 8790 process was restarted afterwards.
- Document access: `GET /documents/{artifact_id}/download?kind=docx|pdf[&inline=1]` serves a tailored document by id (path from the artifact record only, confined to `artifact_dir` after `resolve()`, SHA-256 re-checked against the reviewed hash: 403 / 409 / 404, `private, no-store`, `nosniff`, stored filename in `Content-Disposition`). Buttons + hash prefixes + copyable path on `/documents`, the job page and the review package page (`tests/test_documents.py`). Imported historical artifacts (tracker clone `artifact_status`) are not served; they are listed on `/history` only.

## Guidance pass: easier to use (2026-11-05)

- Plain-language layer in `ui_settings.py`: `STATUS_LABELS` (applications, jobs, runs, interrupt types, sync, scopes, outreach), `NEXT_ACTIONS` (one verb per application state), `GLOSSARY` (authorization, material hash, batch, needs_verification, handoff, emergency stop, sync states, outreach scope, fit rank, grounding, regenerate) and `PAGE_INTROS` (one strip per page). Jinja macros `status()` (label pill + raw monospace chip) and `hint()` (native `<details>`, no JS) in `templates/_macros.html`; per-page intro strip `_intro.html` with Hide/Show remembered in `ui_settings.collapsed_intros` (`POST /settings/intro`, unknown page → 400).
- Overview: getting-started checklist computed from real state (profile, base resume, history import, provider key + last check, `AMORE_BROWSER_ALLOWED_HOSTS` with the exact `.env` line, pricing, first job, first review, first authorization) with one link per item and a progress bar; dismissible (`POST /settings/checklist`, restore from Settings → Guidance). Provider check result persisted in control `provider_last_check` (`POST /provider/check`). Attention items carry exactly one verb button (Review package, Answer questions, Finish on site, Check outcome, Approve submission, Record result, Open run, Resume run, See what failed, Review jobs, Check history).
- Job page: eight-step stepper (Import → Qualify → Tailor → Review → Authorize → Submit → Record → Outreach) with the next action under it; "More actions": Re-qualify, Start a new run, Regenerate documents. `POST /jobs/{id}/regenerate` marks earlier `resume_tailored` artifacts `superseded`, re-tailors, revokes single `application_submit` scopes bound to the job, re-prepares a live package (`needs_input`/`ready_for_review`/`authorized`), audits `documents_regenerated`; refuses without a base resume, while a run is queued/running, or after submission/executing. `ApplicationService.prepare` now ignores superseded artifacts and takes the newest. Review page shows a "What to do now" box per status; empty states everywhere say what to do with a button.
- Menu: Runs demoted to a tab under Applications (`_app_tabs.html`, `NAV_ALIASES`; `/runs` unchanged). New `/help` (workflow in eight steps, every status with raw state, glossary with anchors, safety model, `#hosts` environment table, `#files` paths). Help also in the avatar menu and linked from every intro strip.
- Appearance: separate background tint (`bg_tint`: `accent` | `neutral` | `warm` | `cool` | `#rrggbb`) driving `--tint` → `--bg` / soft surfaces independently of `--accent`; live preview; cookie `amore_ui` is now `theme|accent|tint` (two-part cookies still parse). Brand mark is one macro (`_brand.html`) used by sidebar, login, `GET /brand.svg` favicon (public, cached 1 h) and the "logo" avatar style.
- Tests: 98 (`test_ui_settings.py`: tint normalisation, persistence, `bad_tint`, three-part cookie on the login page, favicon, checklist and intro state incl. 400/CSRF/login, help anchors, runs tab; `test_web.py`: regenerate flow incl. refusals, superseded flag, new artifact on disk, new material hash, scope revoked, audit, submitted refusal). Ruff clean (`ui_settings.py` copy tables are exempt from E501).

## Submission screenshots: summary + receipt (2026-11-05)

- Playwright plan executor captures `summary` (the fully filled final review step, after the `assert_value` checks, immediately before the final submit click) and `receipt` (verified confirmation page); an ambiguous outcome after the click keeps the distinct `ambiguous` capture (plus the earlier `summary`). Files: `<artifact_dir>/jobs/<job_id>/screenshots/<UTC timestamp>_<job_id>_<label>.png` (`Plan.job_id`, filled by `ApplicationService` for every builder; `BrowserConfig.artifact_dir`); `screenshot_dir` stays the fallback without a job id.
- Evidence shape: `application.evidence.screenshots = [{label, path, sha256, captured_at, url, artifact_id}]`. Older string lists are widened on read by `browser.normalize_screenshots` (label from the `_<label>.png` suffix). Each capture is also registered in `artifacts` as `{kind: "screenshot", owner_id/job_id, application_id, label, path, filename, sha256, media_type: "image/png", captured_at, url, created_at}` so the hash-verified download route can serve it by id. `evidence_basis` is `receipt_screenshot_reviewed` only when a `receipt` capture exists.
- Tracker sync archives both PNGs with the record under the tracker's `artifacts/<record id>/<sha12>-<record id>_<label>.png` convention: receipt through `records.py add --receipt` (kind `receipt`), summary staged by the coordinator and listed on the record as kind `summary_screenshot` (validated by the tracker's own hash/path check; ledger schema unchanged).
- Tests: fixture review page now shows every answer; browser tests assert both captures, placement, naming, hashes, fallback dir and the ambiguous case; an end-to-end test runs `ApplicationService` → adapter → fixture ATS → application record → tracker clone; Mongo tests cover structured/legacy evidence, artifact registration idempotency, ambiguous outcomes and the sync archive. Dashboard display of these captures is pending (UI work tracked separately); until then `/documents` and the job page list `screenshot` artifacts with the generic document actions.

## Remaining / not done

- `config/pricing.json` prices for `gpt-5.4-mini` (and any other model used); until filled every cost reads "unknown".
- Deploy the app containers to the NAS (needs SSH enabled in UGOS for the rsync/build window) or run the app on the Mac against the NAS database (works today).
- Tailscale ACL limiting `tcp:27017` on the NAS to Joss's devices (observation: port is open to the whole tailnet, auth required).
- `AMORE_BROWSER_ALLOWED_HOSTS` for the ATS hosts Joss wants scanned (e.g. `boards.greenhouse.io`).
- Site-specific adapters beyond the generic form plan (Workday, LinkedIn Easy Apply) if wanted; currently handoffs. Optional SMTP channel.

## Open questions for Joss

1. Should `Product Engineer` style titles count as a preferred role family, or stay `needs_review`? (Affects qualification of the Faire posting and similar ones.)
2. Which ATS hosts may the worker open for form scanning (`AMORE_BROWSER_ALLOWED_HOSTS`)?

## Exact next step

Joss logs in to http://127.0.0.1:8790, works through the getting-started checklist on Overview, opens the Faire job and uses More actions → Regenerate documents to get the PDF (LibreOffice is now installed), reviews the rebuilt package, sets `AMORE_BROWSER_ALLOWED_HOSTS` for the ATS hosts he wants scanned, answers the role-family question, fills `config/pricing.json` for `gpt-5.4-mini`, and grants the first authorization scope in the dashboard when ready. Deploy containers to the NAS once SSH is enabled.
