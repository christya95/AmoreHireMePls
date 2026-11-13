# Handoff

Written 2026-11-12 Toronto. Read this first in every new session. No private values: field names and counts only. Keep under 120 lines; finished items move to `docs/PROGRESS.md`.

## State now

- NAS stack `amorehiremepls` is image `lepia/amorehiremepls:addd8ce2abfd` (stop record, deployed 2026-11-12 20:41; before it `ff18f499c78c` sign-in, the same evening). The application page flow of 2026-11-13 deploys as the image named after its commit; the line below this list of images is updated once it is healthy. Both went through Portainer on the tailnet address (`PORTAINER_URL=http://100.90.96.81:9000`) because the Mac was off the home network. Web and worker report healthy in Portainer; `/health` on the LAN address was not reachable from the Mac that evening. The Mac dashboard was restarted on the same commit, `/health` 200. Earlier, on image `f26eb2344b66`: the 03:30 UTC run `run_5f0a1582aac580db` for `app_5cf993fc297f4b3e` loaded the shared session file (`www.linkedin.com.storage.json`, 18563 bytes, web and worker both mount `/volume1/docker/amorehiremepls/private`). The job page was signed in. Apply with LinkedIn then landed on `/uas/login`, which was a block page with no password field. Nothing was sent. A cross-site popup that lands on a login path is opened again by the app so the saved session is sent, and a block page is not called a sign-in. Fixture tested. Do not press I applied on the site.
- An approved package with nothing in progress shows Apply with the app in the status block. It posts the existing retry route, which queues that package and does not ask for a new approval. A task already running, and a stale approval, do not show it. The LinkedIn application ending 856733 is removed, so that page has no button. The current package for that job asks to approve again.
- A handoff that sent nothing, and whose approval still covers the package, shows Retry with the app as well as I applied on the site. Same retry route and CSRF. It queues that package and does not create an application or an approval. Hidden while a task is queued, claimed, running, paused, or waiting on something other than this handoff. A stale approval still asks to approve again. The button is on `app_5cf993fc297f4b3e`. No run was queued from this session.
- LinkedIn application forms: a signed-in Easy Apply style page is filled from saved answers and stops before submit unless an approval already matches. An external apply link is followed, including its redirect, and that page may load scripts from other hosts for the rest of the run. Apply with LinkedIn is clicked when that page has it. Sign-in, a verify-it's-you page, an unknown required question, and a page with neither control stay a handoff. Nothing was sent. No messages or connection requests. Fixture tested, including a redirect whose form is opened by a script on a third loopback name.
- Eight one-character boxes under "Security code" are found and filled one character each (`browser.py`: `CODE_MARK`, `_CODE_BOXES_JS`, `_type_code`). The name-based selector missed them, so the 16:13 attempt never showed "Code from your email". Fixture tested. The live prompt is the next check.
- Also in this image: Remove on a prepared row, Cancel before the final click, 90 s receipt wait, one resubmit after the emailed code, place-list host allowed, phone country rows ignored, typed answers win, exact city row, years buckets.
- The 16:13 Faire attempt reached the code prompt and was not sent. No employer receipt exists. Nothing has been submitted by the app.
- Live and used for real: profile store, base resume, tracker preview/import (nothing pushed), discovery by URL, model check through `op run`, deterministic tailoring, review and approval gates, application status, fit check, `candidate/` drop folder, self identification, salary case, timezone and tracker settings, `profile.toml`. `amore migrate-users` ran once (222 stamped).
- Fixture tested only: Greenhouse, Lever, Ashby and LinkedIn form filling, including an external apply link and the eight-box code field; the salary skill; Workday refresh; notifications; the human check and the one later try. LinkedIn was opened live on the previous image and stopped before send.
- Data (counts only, live database): contact and address facts, links, availability, notice, work arrangements, compensation floor, five self identification facts, 62 answers, 11 skills, 11 education rows. Profile city and province are still the defaults; `location_fallbacks` is empty. Faire application `app_6a78e38192928c99` (job `job_d0f65161045baf54`).

- Sign in with saved logins (2026-11-12, `AMORE_SITE_SIGNIN`, Settings switch, on for this stack): at a sign-in page an approved run holds and the application page shows Allow 1Password. The press must happen on the Mac dashboard (`op` and the 1Password app live there); the login reaches the NAS worker sealed to a one-time key and is not stored. Codes are typed on the application page. A refused password holds with the site's reset link and "I changed the password, try again"; the app never resends it by itself (3 refusals per site in 24 h, lock wait 30 min or what the site says). Fixture tested only; no real 1Password read and no real site yet. Guide for agents: `.cursor/skills/site-sign-in/SKILL.md`.
- Stop record (2026-11-12, `AMORE_STOP_RECORD`, on): every attempt that ends without a confirmed success keeps the page picture, page source, every request with headers, bodies and cookies, the console, a zipped HAR and a Playwright trace under `artifacts/jobs/<job_id>/stop/` on the computer that ran it. Unmasked by the owner's choice, so it can hold a site password. The application page shows it under the status block with that notice; the Mac dashboard shows counts only because the files are on the NAS (open the NAS dashboard for the picture and the request list). Cleared by the next attempt, Start over and Remove; never for a sent, receipted or unclear application; left out of backups. Fixture tested only.
- Application page flow (2026-11-13): one status block carries a step tracker (Prepared … Confirmed, one step marked now, waiting on you or stopped here), the attempt number and start time, the one ask and the one control, and it follows a new attempt without a reload. A stopped application always names a next action; the state to action table is in `docs/ui-copy.md` and `web/app_flow.py: STATES`, walked by `tests/test_app_flow.py`. I applied on the site, Start over and Remove unused resumes sit under More in the header; the manual record needs a tick and the server refuses it without one. The header warns before approval when the form is on another site, when the site has asked to sign in, or when an earlier application stopped there for want of an account. Settings lists the sites the app has signed in on. Not built: a Sign in now button ahead of an attempt (it needs a worker task of its own) and a count in the side navigation. Fixture tested and looked at in a headless browser on fictional data; not yet watched on a live run.

## Owner-only steps for site sign-in

1. 1Password app, Settings, Developer: Integrate with 1Password CLI is on (it already is if `op run` works without typing a password).
2. For each job site that needs an account: create the account on the site yourself, and save one Login item whose website is that site's address.
3. Use the Mac dashboard (`http://127.0.0.1:8790`) for the application page when a run asks to sign in. The NAS dashboard cannot open 1Password and says so.
4. Press Allow 1Password when the page asks, approve the 1Password window, and type the code if the site sends one.

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

Next step: the owner presses Retry with the app once on `app_5cf993fc297f4b3e`. The button still says to sign in; do not sign in again. Do not approve a new package, do not press I applied on the site, and do not record a confirmation unless the site shows one. Separately, the owner records "It was not sent" on the Faire attempt, presses Retry, and pastes the new email code into "Code from your email".

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
- Application page flow: `web/app_flow.py`, `web/templates/application.html`, `_macros.html: status_block`, `tests/test_app_flow.py`.
- Stop record: `apply/stop_record.py`, `web/stop_view.py`, `web/routes/stop_record.py`, `tests/test_stop_record.py`.
- Sign-in: `apply/signin.py`, `core/site_signin.py`, `core/credentials.py`, `core/sealed.py`, `web/routes/signin.py`, `tests/fixtures/signin_site.py`.
- `candidate/candidate_profile.py` and `candidate/profile.example.toml`.
- `scripts/deploy_nas.sh`, `deploy/nas/compose.yaml`, `docs/nas-deployment.md`.
