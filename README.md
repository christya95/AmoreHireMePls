# AmoreHireMePls

Local job-search assistant: discovers and qualifies postings, tailors a grounded resume from the approved base, prepares application packages for review, executes only within explicit saved authorization, records outcomes to the private GitHub tracker, and drafts recruiter outreach.

- Runtime state: MongoDB (NAS `mongo:8.0` standalone or the bundled `dev-mongo` profile).
- Orchestration: LangGraph with `langgraph-checkpoint-mongodb`; model calls through LangChain structured output.
- Deployment: one Docker image, two services (`web`, `worker`), Docker Compose.
- Ledger: `christya95/job-application-records` stays the durable record; this app writes to a local clone through a durable outbox.

See `docs/` for the capability map, backup/restore, troubleshooting and NAS deployment. `docs/PROGRESS.md` tracks what is done and what is next.

## Setup (Mac, development)

```bash
uv sync                                  # Python 3.12 virtualenv from uv.lock
uv run playwright install chromium       # browser for the adapter and fixture tests
docker compose --profile dev-mongo up -d dev-mongo
cp .env.example .env                     # edit AMORE_APP_PASSWORD (>= 12 chars) and AMORE_APP_SECRET (>= 32 chars)
uv run amore setup-db                    # indexes + checkpointer check
```

Secrets: do not put API keys in `.env`. Copy `.env.references.example` to `.env.references` with `op://` references and run commands through `op run --env-file .env.references -- uv run amore web`. See `docs/nas-deployment.md` for the unattended NAS variant.

## Run

```bash
uv run amore web                         # dashboard at http://127.0.0.1:8790 (password login, CSRF on every form)
uv run amore worker                      # runs, sync coordinator, America/Toronto discovery schedule
uv run amore worker --once               # one tick, prints what it did
```

Docker (same image for both processes):

```bash
docker compose build
docker compose up -d web worker          # locally: docker compose --profile dev-mongo up -d dev-mongo web worker
curl -s http://127.0.0.1:8790/health
```

## First-time data

```bash
# private profile: approved facts/salary skills -> protected Mongo profile store (never committed)
uv run amore profile import --facts-skill ~/workspace/agent-skills-library/skills/career/joss-application-facts/SKILL.md \
                            --salary-skill ~/workspace/agent-skills-library/skills/career/joss-desired-salary/SKILL.md
# approved base resume (DOCX only; copied into AMORE_PRIVATE_DIR/base_resume and hashed)
uv run amore profile base-resume /path/to/approved_base_resume.docx --approved-on 2026-10-15
# tracker history
uv run amore tracker clone               # into AMORE_TRACKER_CLONE_DIR (needs your git credentials)
uv run amore tracker preview             # counts, duplicates, conflicts, missing artifacts; nothing written
uv run amore tracker import              # idempotent; re-run after new tracker commits
```

Authenticated job sites (Workday etc.): `uv run amore browser login myworkdayjobs.com https://<tenant>.myworkdayjobs.com/...` opens a headed Chromium on the Mac; sign in yourself; the session state is saved privately for the worker. Add the host to `AMORE_BROWSER_ALLOWED_HOSTS` first.

## Operate

Dashboard pages (left sidebar): Overview (greeting, KPI tiles, applications-per-month chart, job pipeline, "needs your attention" list with direct actions, system health, recent runs and activity), Jobs (import URL, discovery sources, qualification, fit evidence, single-job run, bounded batch), Applications (local review packages by status), Runs (pause/resume/cancel, interrupts), Documents (tailored resumes with hashes, page verification and grounding), Outreach (recipients, history reconciliation, drafts, handoff/record send), History (tracker import preview/import), Sync (outbox states, retry, crash reconcile), Usage (measured tokens, known vs unknown cost, skill catalog), Authorizations (single and bounded batch scopes, revoke), Candidate profile (facts with provenance, computed experience, preferences, exclusions, base resume), Settings (appearance, name, avatar).

Emergency stop is in the header on every page (two steps: open, then confirm with an optional reason) and at `amore emergency-stop`. It blocks any new external action; in-flight actions finish into `needs_verification` or a recorded outcome.

### Settings (appearance, name, avatar)

- **Theme**: system / light / dark. The sun-moon button in the header flips it instantly; the Settings page sets it explicitly. The saved value is mirrored in the `amore_ui` cookie so the first paint (and the sign-in page) already use the right theme; `prefers-color-scheme` applies when the setting is `system`.
- **Accent colour**: six presets (sage, blue, violet, teal, amber, rose) or any `#rrggbb`. Everything else (hover, soft tints, page background, chart fill) derives from `--accent` through `color-mix()` in `src/amorehiremepls/static/app.css`.
- **Display name**: used in greetings and the header only. Empty means "use the profile's preferred or first name"; the code falls back to a generic name, never to data in templates.
- **Avatar**: upload a PNG, JPEG or WebP up to 1 MB (declared type, file header and dimensions are all checked) or pick one of four initials styles. The file is stored under `AMORE_PRIVATE_DIR/avatar/` and served only through the authenticated `/settings/avatar/image` route.
- Storage: everything except the image file lives in the MongoDB `controls` collection under the key `ui_settings`. Nothing here feeds resumes, answers or messages.
- Front end: no CDN, fonts or third-party scripts; one local stylesheet, one small progressive-enhancement script, inline SVG icons and a dependency-free SVG chart. Every control works as a plain form without JavaScript; all POSTs carry the CSRF token.

### Opening a tailored resume

Documents, the job page and the review package page show **Open PDF** (in a browser tab), **Download PDF** and **Download DOCX** buttons for each tailored document, with the hash prefixes, page count and the on-disk path in a copyable `code` element. The route is `GET /documents/{artifact_id}/download?kind=docx|pdf[&inline=1]` (login required). It never takes a path from the browser: the path comes from the artifact record, must resolve under `AMORE_ARTIFACT_DIR`, and the file's SHA-256 is recomputed and compared with the reviewed hash before anything is sent (403 outside the directory, 409 on mismatch, 404 when no PDF was rendered). A PDF exists only when LibreOffice was available at tailoring time (it is inside the container; on a plain Mac only the DOCX exists).

Nothing is submitted or sent without an authorization scope bound to the exact job, answers and document hashes (or a bounded batch scope with stop conditions). Sites without a form plan or adapter become resumable handoffs; you record the confirmation you saw.

## Test

```bash
uv run pytest                   # 86 tests; mongo-marked tests skip automatically if 127.0.0.1:27027 is down
TEST_MONGO_URI=mongodb://127.0.0.1:27027 uv run pytest -m mongo
uv run pytest -m browser        # Playwright adapter against the local fixture site (skips without Chromium)
uv run ruff check src tests && uv run ruff format --check src tests
```

Tests use deterministic doubles (`FakeProvider`, local fixture ATS site, local bare git remote). No live applications or messages are part of any test.

## Backup / restore

```bash
uv run amore backup /path/to/amore-$(date +%F).tar.gz
uv run amore restore /path/to/amore-2026-11-04.tar.gz        # refuses a non-empty database without --force
```

Details in `docs/backup-restore.md`.

## Creating the GitHub remote (not done automatically)

This repository has no remote. When you want one, run from the repo root:

```bash
gh repo create AmoreHireMePls --private --source=. --remote=origin --push
```

Do not point it at the tracker repository or the old job hunter repository. Private data lives under `runtime/` and is gitignored.
