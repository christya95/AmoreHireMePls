# Handoff: current state and the open queue

Read this first in every new session. Nothing private is in this file: no values from the profile, no secrets. Dates are
Toronto time. Keep it under 120 lines; move finished items to `docs/PROGRESS.md`.

## 1. Current state (2026-11-05, 12:30)

- **NAS**: Portainer stack `amorehiremepls`, dashboard `http://192.168.2.49:8790`, image `lepia/amorehiremepls:<tag>` where
  the tag is the 12-character sha of the deployed commit. Deployed tag: see the last line of this section.
- **Live and used for real**: profile store, base resume registration, tracker preview/import (nothing pushed), discovery
  by URL, model check through `op run`, deterministic tailoring with a 2 page PDF, review package and approval gates,
  dashboard on the NAS, Updates list on the Overview.
- **Fit check** shows resume evidence by line text, never raw ids (`docs/PROGRESS.md` "Fit card evidence").
- **Candidate folder**: `candidate/` on the Mac, bind mounted at `/candidate` on the NAS; Profile page upload form writes it
  there. `AMORE_CANDIDATE_SUPPLEMENTAL_MAX_CHARS=40000` on the stack and in the local `.env` (code default 12000).
- **Greenhouse adapter**: upload fix with a form check before the first fill. Fixture tested only; **no live Greenhouse,
  Lever or Ashby submission has ever been made**.
- **Profile** (live database, through `ProfileStore`; values never in the repo): contact and address facts under the mapper
  keys, links, availability, notice period, work arrangements, compensation floor, five self identification facts
  (sensitive), 62 answers, 11 skills, 11 education rows. Version 8 after the profile.toml import below (was 7).
- **Self identification on forms**: a saved answer is matched to the form's own option text; no match keeps the decline.
- **Salary**: posted band → figure inside it (floor enforced); no band → one bounded `salary_case` proposal, shown as
  "Why this figure", approve or edit.
- **Profession neutral refactor (this session, commits `55d4a8c`..`3426b51`)**: no role, place, currency, employer,
  technology word, name or timezone remains in `src/` (`tests/test_regressions.py::test_src_contains_no_owner_name`,
  `tests/test_customization.py`). The one customization surface is `candidate/profile.toml` (stdlib `tomllib`,
  `candidate_profile.py`): `amore profile import --from … [--dry-run]`, `amore profile export --to …`, `amore profile
  fingerprint`, and the **Load profile file** button on the Profile page. Timezone is `AMORE_TIMEZONE` (default UTC; the
  Mac `.env` and the NAS stack keep `America/Toronto`); the GitHub tracker is optional (`AMORE_TRACKER_ENABLED`,
  `AMORE_TRACKER_REMOTE_URL`; on for this operator). Deploy values are variables (`OP_VAULT`, `IMAGE_REPO`,
  `AMORE_DATA_ROOT`, `AMORE_PUBLISH_HOST`, `AMORE_TIMEZONE`, tracker). Joss's live preferences were exported to the
  gitignored `candidate/profile.toml`, the rules that used to be code defaults were added to it, and the import
  reproduced the live fingerprints block for block. Suite: 300 passed, ruff clean.
- **Deployed tag**: see "Deploy record" at the end of this file.

## 2. Open items for Joss

- **Faire** application `app_1c0408f4d9f27059`: failed, retryable. Retry **only** from the dashboard (Retry with the app);
  it rebuilds the package and waits for a new approval. Never retried by an agent.
- **BMO** job `job_37efeb56f094fb95`: the posting is gone from Workday (403 / page does not exist). Joss decides whether to
  mark it closed.
- **Three profile conflicts left as saved**: availability (ASAP vs two weeks notice), `degree_level` wording, internship
  employer names and titles differ between the resume and the LinkedIn handoff notes. If the resume is wrong, fix the
  DOCX and re-register it; otherwise edit the fact on the Profile page.
- **Handoff notes file** in `candidate/` is truncated at the 40,000 character cap; the knowledge base file is read in full.
  Trim or split the notes file so the important part sorts first.

## 3. Queue, in order

a. **Form rules for the newly saved answer keys.** Add `FIELD_RULES` entries (`applications.py`) for: `legal_middle_name`,
   `pronouns`, `time_zone`, `citizenship_status`, `currently_employed`, `willing_to_relocate`, `willing_to_travel`,
   `travel_percentage`, `overtime_willing`, `on_call_willing`, `languages`, `drivers_licence`, `security_clearance`,
   `background_check_consent`, `former_employer_contact`, `references_policy`, `relatives_at_employer`,
   `restrictive_agreements`, `outside_business_interests`, `reason_for_leaving_most_recent_role`,
   `cover_letter_preference`, `career_goals`, `years_*`, `gpa`, `graduation_year`, `program`, `field_of_study`,
   `certifications`; add `veteran_status` and `indigenous_identity` to `SELF_ID_RULES`. Note: `pronouns` and
   `citizenship status` are matched by the `SENSITIVE` regex today and stay blank; decide deliberately whether a saved
   answer may be typed (the neutral way is a `Fact.question_pattern` on the saved fact, which already works for any key).
   Keep every new rule field neutral; the years rule is `ats/rules.py: YEARS_RULE`.
b. **Directory reorganization** into `core/ candidate/ jobs/ apply/ records/ outreach/ web/` (7 groups) with `git mv` and
   absolute imports; first commit is move only. Before moving, check `lg_checkpoint_writes` for stored class paths
   (LangGraph checkpoints pickle module paths). Then: split web routes, docstring standard plus the 22 high value
   comments, `docs/architecture.md`, hard coded test paths for `static/` and `templates/`, move ruff per-file-ignores.
c. **Per-user accounts** with an admin role and 365 day access: single database plus `user_id` through a `ScopedStore`,
   argon2id passwords, `users` collection (`role`, `access_until`, `status`), `require_session` gate → `/access-ended`,
   People page under `/admin` (extend, set password, disable, delete with backup and tombstone), Overview badge for
   members only, worker filters to active users, `amore migrate-users` makes the original operator admin with no expiry.
   Order of the plan steps: 1, 2, 4, 7, 5, 8, 9.
d. **Deploy after each** of a, b, c and record the tag here and in `docs/PROGRESS.md`.

## 4. Operating rules

1. Run tests and lint outside the sandbox: `required_permissions: ["all"]`, and strip the sandbox variables:
   `env -u PLAYWRIGHT_BROWSERS_PATH -u UV_CACHE_DIR -u TMPDIR -u __CURSOR_SANDBOX_ENV_RESTORE uv run pytest -q -p no:cacheprovider`
2. Lint: `uv run ruff check src tests && uv run ruff format --check src tests` (line length 180). Dev Mongo on
   `127.0.0.1:27027` (`docker compose --profile dev-mongo up -d dev-mongo`); Chromium via `uv run playwright install chromium`.
3. Commit after each step with a plain sentence; push `main` only after tests and lint pass; never force push.
4. Deploy: `git fetch origin && git worktree remove --force /tmp/amore_deploy_wt 2>/dev/null || true && git worktree add
   --detach /tmp/amore_deploy_wt origin/main && cd /tmp/amore_deploy_wt && PORTAINER_URL=http://192.168.2.49:9000 op run
   --env-file <repo>/.env.references -- ./scripts/deploy_nas.sh` (outside the sandbox, allow up to 7 minutes), then check
   `/health` is 200 and the Overview greeting still shows the display name.
5. If auto review blocks the push or the deploy, retry the identical command once with the smart mode approval request
   and the exact block reason; do not change the command.
6. Never retry a live application, send a message or grant an approval; those are Joss's clicks in the dashboard.
7. Never print or log secrets or profile values; compare profiles by fingerprint (`amore profile fingerprint`), report counts.
8. Secrets come from 1Password through `op run --env-file .env.references`; the CLI may need the 1Password desktop app
   unlocked. Never ask for secret values in chat.
9. Long lived processes (web, worker) start detached and outside the sandbox; only one worker against the NAS database.
10. Private data stays out of git: `candidate/` (except README, `.gitignore`, `profile.example.toml`), `runtime/`, `.env`,
    `.env.references`. `tests/test_regressions.py` fails the build on known private tokens.

## Deploy record

- 2026-11-05 earlier: `e81e95931bf8` (self identification, salary research, candidate folder).
- 2026-11-05 after the profession neutral refactor: tag recorded in the commit that follows the deploy (see `git log -1 -- docs/HANDOFF.md`).
