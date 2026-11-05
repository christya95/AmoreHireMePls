# Handoff

Written 2026-11-05 12:40 Toronto at commit `45be592`. Read this first in every new session.
No private values here: field names and counts only. Keep under 120 lines; finished items move to `docs/PROGRESS.md`.

## State now

- Deployed: NAS Portainer stack `amorehiremepls`, image `lepia/amorehiremepls:e57cd6de0ff4` (12 character sha of the deployed
  commit), web and worker healthy, dashboard `http://192.168.2.49:8790` (`/health` 200, Overview greeting shows the display
  name, tracker on). `origin/main` is docs only commits ahead of the deployed tag (`45be592` plus this handoff); no redeploy needed.
- Live and used for real: profile store, base resume registration, tracker preview/import (nothing pushed), discovery by URL,
  model check through `op run`, deterministic tailoring with a 2 page PDF, review package and approval gates, honest
  application status plus the Updates list on the Overview (built from stored events, no model call), fit check with resume
  evidence shown as plain lines (no raw ids), `candidate/` drop folder (Profile page upload, NAS bind mount `/candidate`,
  `AMORE_CANDIDATE_SUPPLEMENTAL_MAX_CHARS=40000` on the stack and in the local `.env`; code default 12000), self
  identification option matching against the form's own option text, `salary_case` proposal ("Why this figure", approve or
  edit), `AMORE_TIMEZONE` and tracker settings, profession neutral code with `candidate/profile.toml` as the one customization
  surface (`amore profile import --from … [--dry-run]`, `export --to …`, `fingerprint`, Profile page Load button). The
  owner's live preferences are exported to the gitignored Mac copy of `profile.toml`; the NAS Profile card says "not found"
  because the file is only on the Mac. Suite: 300 passed, ruff clean.
- Fixture tested only: Greenhouse, Lever and Ashby form filling including the hidden input upload fix and the pre fill form
  check; the salary skill; Workday posting refresh; owner notifications, the human check hold and the one later try.
- Not verified: no live employer receipt exists yet for any ATS; nothing has ever been submitted by the app.
- Data (live database through `ProfileStore`, values never in the repo): contact and address facts under the mapper keys,
  links, availability, notice period, work arrangements, compensation floor, five self identification facts (sensitive),
  62 answers, 11 skills, 11 education rows. Profile version 7 per the chat record (the previous handoff wrote 8; the Profile
  page shows the current number).

## Decisions made this session

- Updates feed is built from stored events with no model call (owner).
- Captcha solving stays opt in (`AMORE_CAPTCHA_AUTOSOLVE`, Settings switch), default off (owner).
- `candidate/` is the drop surface for any user: resume, notes, `profile.toml` (owner).
- Names: legal first name on forms, preferred name in greetings and outreach notes (owner).
- Compensation floor applies to every job; the salary expectation is researched first and never set below the floor (owner).
- Demographic questions are answered from the saved self identification facts, matched to the form's options; no match keeps
  the decline option (owner).
- Per user accounts: the admin role never expires, members get 365 days with top ups (agent design, owner approved the concept).
- "Remove" a user means Turn off (reversible) or Delete permanently with a backup and a tombstone (agent).
- Tracker sync keeps running for expired members (agent; owner to confirm, see open questions).
- AI usage, token counts, pricing and spend are admin only: members never see the AI usage page, cost chips or per run
  spend; the admin People page shows per person usage and cost; the owner views his own through the admin surface for
  consistency (agent design, owner approved).
- Profile version 7 saved through `ProfileStore` with provenance `user:chat` and `user:chatgpt-handoff`; values never in the
  repo (agent, from the owner's chat answers).

## Open questions for the owner

1. Start date: ASAP (saved) or two weeks' notice?
2. Exact wording of the "highest education" dropdown value to save for `degree_level`?
3. Internship employer names and titles differ between the resume DOCX and the LinkedIn handoff notes; which is right? If the
   DOCX is wrong, fix it and re-register it; otherwise edit the fact on the Profile page.
4. BMO job `job_37efeb56f094fb95`: the Workday posting is gone (403). Mark it closed or keep it?
5. Faire application `app_1c0408f4d9f27059`: retry from the dashboard (Retry with the app, then approve) now that the upload
   fix is deployed? Never retried by an agent.
6. Should tracker sync pause for expired members, or keep running as designed?
7. Copy `candidate/profile.toml` to the NAS `/candidate` so the Profile card finds it? It holds preferences, not secrets.
8. The handoff notes file in `candidate/` is truncated at the 40,000 character cap; trim or split it so the important part
   sorts first.

## Queue

1. Form rules for the newly saved answer keys. Size: M. Plan: `FIELD_RULES` in `applications.py` for `legal_middle_name`,
   `pronouns`, `time_zone`, `citizenship_status`, `currently_employed`, `willing_to_relocate`, `willing_to_travel`,
   `travel_percentage`, `overtime_willing`, `on_call_willing`, `languages`, `drivers_licence`, `security_clearance`,
   `background_check_consent`, `former_employer_contact`, `references_policy`, `relatives_at_employer`,
   `restrictive_agreements`, `outside_business_interests`, `reason_for_leaving_most_recent_role`,
   `cover_letter_preference`, `career_goals`, `years_*`, `gpa`, `graduation_year`, `program`, `field_of_study`,
   `certifications`; `veteran_status` and `indigenous_identity` into `SELF_ID_RULES`. `pronouns` and `citizenship_status`
   match the `SENSITIVE` regex today and stay blank; the neutral way to allow a saved answer is a `Fact.question_pattern`.
   Keep every rule field neutral; the years rule is `ats/rules.py: YEARS_RULE`. Waits for: an owner decision on the two
   sensitive keys (the rest can start now).
2. Directory reorganization into `core/ candidate/ jobs/ apply/ records/ outreach/ web/` with `git mv` and absolute imports.
   Size: M-L. Plan: first commit move only, from a clean tree; before moving, check `lg_checkpoint_writes` for stored class
   paths (LangGraph checkpoints pickle module paths). Then split web routes, docstring standard plus the 22 high value
   comments, `docs/architecture.md`, hard coded test paths for `static/` and `templates/`, move ruff per-file-ignores.
   Waits for: item 1 deployed.
3. Per user accounts with an admin role and 365 day access. Size: L. Plan: single database plus `user_id` through a
   `ScopedStore`, argon2id passwords, `users` collection (`role`, `access_until`, `status`), `require_session` gate to
   `/access-ended`, People page under `/admin` (extend, set password, turn off, delete with backup and tombstone), Overview
   badge for members only, worker filters to active users, `amore migrate-users` makes the original operator admin with no
   expiry. Step order 1, 2, 4, 7, 5, 8, 9; `backup_user()` and `access_summary` can be drafted first. Waits for: item 2 and
   the owner's answer to question 6.
   - Hide `/usage`, the usage KPIs on the Overview and the cost figures on run and application pages for `role=member`; add
     a per person usage column and a usage detail view under `/admin`; keep per user budget enforcement unchanged.
4. Deploy after each of items 1 to 3 and record the tag here and in `docs/PROGRESS.md`. Size: S. Plan: operating rule 3.
   Waits for: each item's tests and lint.
5. After the first confirmed employer receipt: recruiter Connect note drafts per the owner's rule (in house recruiters at
   that employer, skip RBC and FAANG, under 300 characters, no dashes, no layoff mention, mention the role applied to, draft
   only, send only when the owner asks). Size: S per batch. Plan: `outreach.py`. Waits for: a confirmed receipt.

## Operating rules

1. Test: `env -u PLAYWRIGHT_BROWSERS_PATH -u UV_CACHE_DIR -u TMPDIR -u __CURSOR_SANDBOX_ENV_RESTORE uv run pytest -q -p no:cacheprovider`
   (outside the sandbox, `required_permissions: ["all"]`). Dev Mongo `127.0.0.1:27027` (`docker compose --profile dev-mongo
   up -d dev-mongo`); Chromium via `uv run playwright install chromium`; mongo and browser tests skip without them.
2. Lint: `uv run ruff check src tests && uv run ruff format --check src tests` (line length 180).
3. Deploy: `git fetch origin && git worktree remove --force /tmp/amore_deploy_wt 2>/dev/null || true && git worktree add
   --detach /tmp/amore_deploy_wt origin/main && cd /tmp/amore_deploy_wt && PORTAINER_URL=http://192.168.2.49:9000 op run
   --env-file <repo>/.env.references -- ./scripts/deploy_nas.sh` (outside the sandbox, allow 7 minutes), then check `/health`
   is 200 and the Overview greeting shows the display name; never as part of a handoff.
4. If auto review blocks a command, retry the identical command once with the smart mode approval request and the exact
   block reason; do not change the command. Commit after each step with a plain sentence; push `main` only after tests and
   lint pass.
5. Secrets come from 1Password through `op run --env-file .env.references`; the CLI may block for minutes waiting on the
   desktop app; if `op run` fails, run `op signin` outside the sandbox first. Long lived processes (web, worker) start
   detached and outside the sandbox; only one worker against the NAS database. `/chat-handoff` (personal agent store) when
   the conversation is compacted or passes about 25 turns.
6. Never: retry a live application, send a message or grant an approval (those are the owner's clicks in the dashboard);
   print or log secrets or profile values (compare profiles by fingerprint, report counts); force push; commit `candidate/`
   (except README, `.gitignore`, `profile.example.toml`), `runtime/`, `.env` or `.env.references`.

## Where to look

- `docs/PROGRESS.md`: dated history of every increment, deploy tags, exact next step at the end.
- `src/amorehiremepls/applications.py` and `ats/rules.py`: form field rules (`FIELD_RULES`, `SELF_ID_RULES`, `YEARS_RULE`).
- `src/amorehiremepls/candidate_profile.py` and `candidate/profile.example.toml`: the customization surface.
- `src/amorehiremepls/ats/`: Greenhouse, Lever, Ashby adapters; `browser.py` runs their plans.
- `scripts/deploy_nas.sh`, `deploy/nas/compose.yaml`, `docs/nas-deployment.md`: deploy variables and sequence.
- `tests/test_regressions.py`, `tests/test_customization.py`: private token and neutrality guards; `docs/capability-map.md`.