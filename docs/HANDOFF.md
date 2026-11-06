# Handoff

Written 2026-11-06 12:30 Toronto at commit `a7692be` with an uncommitted local batch (34 files). Read this first in every new
session. No private values: field names and counts only. Keep under 120 lines; finished items move to `docs/PROGRESS.md`.

## State now

- Deployed 14:56: NAS stack `amorehiremepls`, image `lepia/amorehiremepls:719082c5e83f` (commit `719082c`). Local dashboard
  python pid 21704 (op pid 21691) on the same commit. The 14:45 Faire attempt bound Location ("Chose Milton, Ontario,
  Canada") then failed on the synthetic Country box, which resolved to the phone code picker ("Canada +1"); an optional
  suggestion box with no matching row is now cleared and skipped. Owner retries. `8fad9ea` was the page polish.
- Deployed 13:56: `1b7083e`. Root cause of every Faire Location failure: the place list host
  `api-geocode-earth-proxy.greenhouse.io` was not a Greenhouse resource host, so the gate aborted it and no city ever
  appeared; the DOM fallback then read the phone country code rows. Both fixed (host added, `data-dial-code` rows
  ignored). Owner retries the Faire application; check its Location field answer first (one attempt typed "Yes" from it).
- Committed in `c3d1ec0` and deployed (full suite passed, ruff clean); the working tree is clean:
  - A value the person types on the application wins for every field; profile facts fill only untouched fields; typed values
    survive a rebuild (`applications.py: keep_typed_answers`).
  - A replay that did not open the form says "The form was not opened. Showing the result already saved."
  - A dropped Word resume is chosen as base when none is chosen (`runtime.reload_profile` adopts from the candidate folder;
    the Profile upload redirects with `ok=candidate_resume_chosen`).
  - Location picker waits for the place list (field frame or portaled menu), picks the exact city row whose region and country
    agree, prefers an exact row over a longer name, asks only when no rows; step saved as "Location: typed X. A list appeared.
    Chose X, Region, Country." (`browser.py: exact_city_row`, `_region_bonus`).
  - A years question with range options picks the range holding the computed years (3 picks 2-4 years;
    `ats/rules.py: pick_years_bucket`). Rebuild keeps each question's `meta.ats` so the site adapter maps it.
  - Application and job pages redesigned: one status block at top (`status_block` macro; single ask and single control only
    when needed, otherwise "Nothing is needed from you right now."). Folds closed by default except Still needed, Proof after
    an attempt, What would be sent when answers are needed, What is happening while live. Network Tab and history dump removed
    from the page (recording still runs server side). Spacing/motion tokens, `rise`/`row-in`/`chip-pulse` keyframes, reduced motion snaps.
- Live and used for real: profile store, base resume registration, tracker preview/import (nothing pushed), discovery by URL, model check
  through `op run`, deterministic tailoring with a 2 page PDF, review package and approval gates, application status and Updates list from
  stored events, fit check, `candidate/` drop folder (NAS bind mount `/candidate`, `AMORE_CANDIDATE_SUPPLEMENTAL_MAX_CHARS=40000`), self
  identification matching, `salary_case`, `AMORE_TIMEZONE` and tracker settings, `profile.toml` as the customization surface. `amore migrate-users` ran once (222 stamped).
- Fixture tested only: Greenhouse, Lever and Ashby form filling (upload, pre fill check, location picker, years buckets); the
  salary skill; Workday posting refresh; owner notifications, the human check hold and the one later try.
- Not verified: no live employer receipt exists for any ATS; nothing has ever been submitted by the app. Only one real browser
  visit ever happened (2026-11-06 11:46Z); every later pass was a replay.
- Data (live database through `ProfileStore`, values never in the repo): the profile doc had no `user_id` and was invisible
  after sign in; it was stamped to the admin user so answers fill again. Profile city and province are still the defaults;
  `location_fallbacks` is empty. Contact and address facts, links, availability, notice period, work arrangements,
  compensation floor, five self identification facts (sensitive), 62 answers, 11 skills, 11 education rows. The Faire
  application `app_6a78e38192928c99` (job `job_d0f65161045baf54`) was prepared before the fixes: its years answer still shows
  the bare number and its location the typed city; a rebuild with the new code is needed.

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
- Tracker sync keeps running for an expired member (owner, 2026-11-05).
- AI usage, token counts, pricing and spend are admin only; the admin People page shows per person usage and cost (agent).
- Profile version 7 saved through `ProfileStore` with provenance `user:chat` and `user:chatgpt-handoff` (agent).
- A typed value on the application beats a profile fact on every rebuild (owner, 2026-11-06).
- The watch browser panel and the Network Tab left the application page on purpose; do not re-add. Grok Bot is out of scope (owner).

## Open questions for the owner

1. Start date: ASAP (saved) or two weeks' notice?
2. Exact wording of the "highest education" dropdown value to save for `degree_level`?
3. Internship employer names and titles differ between the resume DOCX and the LinkedIn handoff notes; which is right? If the
   DOCX is wrong, fix it and re-register it; otherwise edit the fact on the Profile page.
4. BMO job `job_37efeb56f094fb95`: the Workday posting is gone (403). Mark it closed or keep it?
5. Faire application `app_6a78e38192928c99` (the earlier handoff named `app_1c0408f4d9f27059`): rebuild the answers with the
   new code, then approve? Never retried by an agent.
6. Deploy the local uncommitted batch? Not asked for yet.
7. Copy `candidate/profile.toml` to the NAS `/candidate` so the Profile card finds it? It holds preferences, not secrets.
8. The handoff notes file in `candidate/` is truncated at the 40,000 character cap; trim or split it so the important part sorts first.
9. Should the saved `pronouns` and `citizenship_status` answers be typed on forms? If yes, add a `question_pattern`
   (`pronouns?` and `citizenship`) to each on the Profile page; without one they stay blank by design.
10. Save Mississauga and Toronto as later places (`location_fallbacks`)? Requested earlier, not saved.

## Queue

1. Done locally, not deployed: one Start over button on the application page and the job page. It clears the unsent
   attempt, approval, and tailored files, then returns to the job. No typed word. A receipt hides it and says so.
2. Not done: a higher score resume rebuild through the tailored resume skill (never started). Size: M. Needs the owner's go.
3. Not done: deploy of the local batch (owner has not asked; question 6). Sequence: tests + lint, commit, push `main`,
   `scripts/deploy_nas.sh` via `op run`, then restart the Mac dashboard.
4. Not done: the two saved later places (question 10). Profile page or `profile.toml`, not code.
5. After the first confirmed employer receipt: recruiter Connect note drafts per the owner's rule (in house recruiters at that
   employer, skip RBC and FAANG, under 300 characters, no dashes, no layoff mention, name the role applied to, draft only,
   send only when the owner asks). Plan: `outreach.py`. Waits for: a confirmed receipt.

Next step for a new session: ask the owner whether to deploy the local batch (queue 3, now including Start over), then rebuild
the Faire answers so years and location use the new code, then let the owner approve. Do not retry Faire or mark BMO closed.

## Operating rules

1. Test: `env -u PLAYWRIGHT_BROWSERS_PATH -u UV_CACHE_DIR -u TMPDIR -u __CURSOR_SANDBOX_ENV_RESTORE uv run pytest -q -p no:cacheprovider`
   (outside the sandbox, `required_permissions: ["all"]`). Dev Mongo `127.0.0.1:27027` (`docker compose --profile dev-mongo up -d dev-mongo`);
   Chromium via `uv run playwright install chromium`; mongo and browser tests skip without them.
2. Lint: `uv run ruff check src tests && uv run ruff format --check src tests` (line length 180).
3. Deploy: `git fetch origin && git worktree remove --force /tmp/amore_deploy_wt 2>/dev/null || true && git worktree add --detach /tmp/amore_deploy_wt origin/main
   && cd /tmp/amore_deploy_wt && PORTAINER_URL=http://192.168.2.49:9000 op run --env-file <repo>/.env.references -- ./scripts/deploy_nas.sh`
   (outside the sandbox, allow 7 minutes), then check `/health` is 200 and the Overview greeting shows the display name; never as part of a handoff.
4. If auto review blocks a command, retry it once unchanged with the smart mode approval request and the exact block reason.
   Commit after each step with a plain sentence; push `main` only after tests and lint pass.
5. Secrets come from 1Password through `op run --env-file .env.references` (`op signin` outside the sandbox if it fails).
   Web and worker start detached and outside the sandbox; only one worker against the NAS database. `/chat-handoff`
   (personal agent store) when the conversation is compacted or passes about 25 turns.
6. Never: retry a live application, send a message or grant an approval (those are the owner's clicks in the dashboard);
   print or log secrets or profile values (compare profiles by fingerprint, report counts); force push; commit `candidate/`
   (except README, `.gitignore`, `profile.example.toml`), `runtime/`, `.env` or `.env.references`.

## Where to look

- `docs/PROGRESS.md` (history, deploy tags, next step), `docs/architecture.md` (package map), `docs/ui-copy.md` (copy rules).
- `apply/applications.py` and `apply/ats/rules.py`: `FIELD_RULES`, `SELF_ID_RULES`, `YEARS_RULE`, `pick_years_bucket`,
  `keep_typed_answers`. `apply/ats/`: Greenhouse, Lever, Ashby adapters; `apply/browser.py` runs their plans.
- `web/chrome.py` (`needed_now`, headline status), `web/routes/applications.py: application_view`,
  `web/templates/_macros.html` (`status_block`, `fold`, `tech`, `confirm_action`).
- `candidate/candidate_profile.py` and `candidate/profile.example.toml`: the customization surface.
- `scripts/deploy_nas.sh`, `deploy/nas/compose.yaml`, `docs/nas-deployment.md`: deploy variables and sequence.
- `tests/test_regressions.py`, `tests/test_customization.py`: private token and neutrality guards; `docs/capability-map.md`.
