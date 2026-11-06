# Handoff

Written 2026-11-06 17:38 Toronto at commit `1d60045`. Read this first in every new session. No private values: field names and counts only. Keep under 120 lines; finished items move to `docs/PROGRESS.md`.

## State now

- Deployed 17:37: NAS stack `amorehiremepls`, image `lepia/amorehiremepls:1d60045c6f80` (commit `1d60045`). Web and worker healthy; NAS `/health` 200. Local dashboard python pid 74353 (op pid 73705), started 17:37, `/health` 200, same commit. NAS runs the worker; local web is display only.
- Eight one-character boxes under "Security code" are found and filled one character each (`browser.py`: `CODE_MARK`, `_CODE_BOXES_JS`, `_type_code`). The name-based selector missed them, so the 16:13 attempt never showed "Code from your email". Fixture tested. The live prompt is the next check.
- Also in this image: Remove on a prepared row, Cancel before the final click, 90 s receipt wait, one resubmit after the emailed code, place-list host allowed, phone country rows ignored, typed answers win, exact city row, years buckets.
- The 16:13 Faire attempt reached the code prompt and was not sent. No employer receipt exists. Nothing has been submitted by the app.
- Live and used for real: profile store, base resume, tracker preview/import (nothing pushed), discovery by URL, model check through `op run`, deterministic tailoring, review and approval gates, application status, fit check, `candidate/` drop folder, self identification, salary case, timezone and tracker settings, `profile.toml`. `amore migrate-users` ran once (222 stamped).
- Fixture tested only: Greenhouse, Lever and Ashby form filling, including the eight-box code field; the salary skill; Workday refresh; notifications; the human check and the one later try.
- Data (counts only, live database): contact and address facts, links, availability, notice, work arrangements, compensation floor, five self identification facts, 62 answers, 11 skills, 11 education rows. Profile city and province are still the defaults; `location_fallbacks` is empty. Faire application `app_6a78e38192928c99` (job `job_d0f65161045baf54`).

## Decisions

- Updates feed is built from stored events with no model call (owner).
- Captcha solving stays opt in, default off (owner). The original operator accepted the terms risk on 2026-11-05.
- `candidate/` is the drop surface. Legal first name on forms; preferred name in greetings and outreach. Compensation floor applies to every job.
- Demographic questions use saved self identification facts matched to the form's options; no match keeps the decline option.
- A typed value on the application beats a profile fact on every rebuild (owner, 2026-11-06).
- The watch browser panel and the Network Tab stay off the application page. Grok Bot is out of scope (owner).

## Open questions for the owner

1. Start date: ASAP (saved) or two weeks' notice?
2. Exact wording of the "highest education" dropdown value to save for `degree_level`?
3. Internship employer names and titles differ between the resume DOCX and the LinkedIn handoff notes; which is right?
4. BMO job `job_37efeb56f094fb95`: the Workday posting is gone (403). Mark it closed or keep it?
5. Copy `candidate/profile.toml` to the NAS `/candidate` so the Profile card finds it? Preferences, not secrets.
6. The handoff notes file in `candidate/` is truncated at the 40,000 character cap; trim or split it so the important part sorts first.
7. Should saved `pronouns` and `citizenship_status` be typed on forms? If yes, add a `question_pattern` on the Profile page.
8. Save Mississauga and Toronto as later places (`location_fallbacks`)? Requested earlier, not saved.

## Queue

1. Owner, Faire `app_6a78e38192928c99`: record "It was not sent", press Retry, paste the new email code into "Code from your email". An agent must not retry it or mark it sent.
2. Not done: a higher score resume rebuild through the tailored resume skill. Size M. Needs the owner's go.
3. Not done: the two saved later places (question 8). Profile page or `profile.toml`, not code.
4. After the first confirmed employer receipt: recruiter Connect note drafts (in house, skip RBC and FAANG, under 300 characters, no dashes, no layoff mention, name the role, draft only). Waits for a confirmed receipt.

Next step: the owner records "It was not sent", presses Retry, and pastes the new email code into "Code from your email".

## Operating rules

1. Test: `env -u PLAYWRIGHT_BROWSERS_PATH -u UV_CACHE_DIR -u TMPDIR -u __CURSOR_SANDBOX_ENV_RESTORE uv run pytest -q -p no:cacheprovider` (outside the sandbox). Dev Mongo `127.0.0.1:27027`; Chromium via `uv run playwright install chromium`; mongo and browser tests skip without them.
2. Lint: `uv run ruff check src tests && uv run ruff format --check src tests` (line length 180).
3. Deploy: `git fetch origin && git worktree remove --force /tmp/amore_deploy_wt 2>/dev/null || true && git worktree add --detach /tmp/amore_deploy_wt origin/main && cd /tmp/amore_deploy_wt && PORTAINER_URL=http://192.168.2.49:9000 op run --env-file <repo>/.env.references -- ./scripts/deploy_nas.sh` (outside the sandbox), then check `/health` is 200. Restart the Mac dashboard detached the same way, with those four env vars stripped.
4. If auto review blocks a command, retry it once unchanged with the smart mode approval request and the exact block reason. Push `main` only after tests and lint pass.
5. Secrets come from 1Password through `op run --env-file .env.references`. Web starts detached and outside the sandbox. Only one worker against the NAS database.
6. Never: retry a live application, send a message, or grant an approval; print secrets or profile values; force push; commit `candidate/` (except README, `.gitignore`, `profile.example.toml`), `runtime/`, `.env`, or `.env.references`.

## Where to look

- `docs/PROGRESS.md`, `docs/architecture.md`, `docs/ui-copy.md`.
- `apply/browser.py` runs the plans (`_CODE_BOXES_JS`, `_type_code`). `apply/applications.py` and `apply/ats/rules.py` hold the field rules.
- `web/chrome.py`, `web/routes/applications.py`, `web/templates/_macros.html`.
- `candidate/candidate_profile.py` and `candidate/profile.example.toml`.
- `scripts/deploy_nas.sh`, `deploy/nas/compose.yaml`, `docs/nas-deployment.md`.
