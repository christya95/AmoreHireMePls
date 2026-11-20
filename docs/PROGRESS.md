# Progress

> Start here: [docs/HANDOFF.md](HANDOFF.md) points to the current state, the open items for Joss and the queue in order. This file is the history from 2026-11-12 on, with the decisions and the open lists; earlier sections are in [PROGRESS-archive.md](PROGRESS-archive.md). Search either file; do not read one whole.

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

## Remaining / not done

- Optionally set `AMORE_PUBLISH_HOST=0.0.0.0` for Tailscale access; copy browser sessions to the NAS `private/sessions` when created (command in `docs/nas-deployment.md`).
- Tailscale ACL limiting `tcp:27017` on the NAS to Joss's devices (observation: port is open to the whole tailnet, auth required).
- Greenhouse adapter is fixture tested only; first live run pending (see the exact next step). Lever and Ashby form adapters not started (Greenhouse pattern in `ats/`); Workday and LinkedIn Easy Apply stay handoffs. Optional SMTP channel.

## Open questions for Joss

1. Should `Product Engineer` style titles count as a preferred role family, or stay `needs_review`? (Affects qualification of the Faire posting and similar ones.)

## Jobs list: "Check for new jobs" shows what happened (2026-11-12)

- **The button did nothing before.** `POST /jobs/discover` saved a `discovery_request` task that no code read, and `AMORE_DISCOVERY_SCHEDULE` is empty on the Mac and absent from the NAS stack, so no board was read by button or by schedule. `Worker.run_requested_discovery()` now claims the oldest request, runs one pass and closes the other waiting requests against it. Takes effect on the NAS with the next image.
- `run_discovery` records one row per board (`found`, `new`, `error` = `unsupported` / `not_found` / `unreachable`), the ids of new jobs (first 200) and live `progress`. A crash mid-pass ends the task as `failed` with the boards read so far.
- `web/jobs_refresh.py` turns those tasks into the words above the list (pure, no database). Routes: `GET /jobs/refresh` (small JSON state), `GET /jobs/list` (the table only), `POST /jobs/discover` (one request at a time; JSON when asked, otherwise redirect).
- Page: a status line above the list (what was found, from which boards, last checked as a relative time), a thin sweep and dimmed rows while checking, rows fade and rise in on reload (50 ms apart, capped), new jobs get a "New" mark and a tint that fades, a banner states the outcome (warning when a board was skipped, critical when all failed). `static/jobs.js` is loaded by the jobs page only; without JS the form posts and the page reloads. Reduced motion removes all of it.
- Tests: `tests/test_jobs_refresh.py` (wording, routes, CSRF, one pass for several presses, failed board, stylesheet block). Seen in the browser against fictional data on the dev database only; not pressed against the real database.
- Open: decide whether to set `AMORE_DISCOVERY_SCHEDULE` so boards are read without the button. LinkedIn as a source of jobs was investigated, nothing built.

## Jobs page: several links per paste, and checks twice a day (2026-11-13)

Two owner decisions: "Build pasting several links at once now, and reading LinkedIn job alert emails once Gmail reading works" and "Yes, twice a day, morning and late afternoon."

- **Several links per paste.** "Add a job" takes one link or up to 50, one per line or separated by spaces or commas (`web/jobs_paste.py: split_links`). Blank lines, repeated links (same LinkedIn job id, or the same address without tracking parts) and plain words are ignored; an entry that is not an http(s) link is named in the banner and not added. Each link goes through `Discovery.import_link`, the body `import_url` always had, so identity, duplicates and the LinkedIn job id are unchanged and nothing is fetched (the tests fail on any outgoing request). One clean link behaves as before: employer, title, job ID, location and pasted ad text apply, and the job opens. Anything else returns to the list with one banner (`banner`): added, already in the list, could not be read (three names, cut to 48 characters), left out over the limit. Green, amber or red by outcome.
- **Why inline.** Adding a link reads no board and no page: it is a handful of small writes, and a link without ad text makes no model call. Fifty links are added in the request itself, so no background task was added for it.
- **New mark.** One paste leaves one activity entry (`job_links_added`, "Job links pasted") with the ids it added. The list marks the latest paste that added jobs with the same `is-new` / "New" mark a check uses (`pasted_job_ids`), until the next check ends or a day passes. A job known only by its link shows the link, shortened, after "Unknown title".
- **Schedule.** `AMORE_DISCOVERY_SCHEDULE` is a comma separated list of 24 hour `HH:MM` times of day in `AMORE_TIMEZONE` (`core/schedule.py`). The code default stays empty. `deploy/nas/compose.yaml` now passes it to web and worker, `scripts/deploy_nas.sh --set AMORE_DISCOVERY_SCHEDULE=…` refuses a value that is not `HH:MM`, and `docs/nas-deployment.md` section 4a documents it. This operator's stack: `08:00,16:30`, `America/Toronto`. The Mac `.env` was not touched; the worker runs on the NAS.
- **Safe to repeat.** A due time of day only queues a check (`Worker.run_schedule`); `run_requested_discovery` reads the boards once for it and for any button press waiting beside it. Nothing is queued while a check is waiting or running (that check answers the slot), or while no board is watched. Each slot is claimed once in `scheduler_slots`, so a restart or a redeploy does not repeat it, and only the latest time of day that passed is made up for (within 6 hours): a worker that was off through several times runs one check. A posting already scored is not sent to the model again; `tests/test_jobs_schedule.py` counts the model calls of a second pass (zero) with a fixed clock.
- **Why every check moved every job to the top, and the fix.** `JobStore.upsert` wrote `updated_at = now` and a fresh `latest_snapshot` (new `fetched_at`) for every posting it saw again, changed or not. All jobs is sorted by `updated_at` and cut at 200 rows, so jobs pasted by hand sank after each check. Now `updated_at` moves only when something about the job changed: a new identity key, a first value for an empty field, a posting whose text or source details differ (`posting_changed`), or a new duplicate flag. A posting seen again unchanged gets `last_seen_at` only and keeps its place; a new or changed posting still rises. A changed posting is not scored again by a check; "Check fit" on the job does that.
- **Strip.** Under the check line: "Next automatic check today at 16:30. The last one ran today at 08:00." The worker saves the times it loaded in `controls.discovery_schedule` and logs `discovery schedule: 08:00, 16:30 (America/Toronto)` at its first pass; the page reads that record, so the Mac dashboard, whose own setting is empty, shows it too. With no time set the line is absent.
- Changed behaviour to know: a pasted entry without `http://` or `https://` is no longer accepted, also when it is the only one.
- **Next source of jobs, agreed with the owner:** LinkedIn job alert emails, read from the mailbox once mail reading works. Not started. Searching LinkedIn by script is ruled out; the app still does not contact LinkedIn to find jobs.
- Tests: `tests/test_jobs_paste.py`, `tests/test_jobs_schedule.py`. Fixture tested and looked at in a headless browser on fictional data.
- Deployed 2026-11-13 15:25 as `lepia/amorehiremepls:1c735bcc39fd` with `--set AMORE_DISCOVERY_SCHEDULE=08:00,16:30`; every other stack variable was carried over. Web and worker healthy; both containers carry the variable and the worker logged `discovery schedule: 08:00, 16:30 (America/Toronto)`. The first automatic check is the 16:30 one that day; no scheduled check had run when this was written, so the schedule itself is fixture tested, not yet seen live.

## Sign in on a job site with a saved 1Password login (2026-11-12)

Opt-in, default off (`AMORE_SITE_SIGNIN`, Settings switch "Sign in with saved logins", control `site_signin`; on for this operator's stack and local `.env`). One generic step, `apply/signin.py`, finds a sign-in form by structure and is called from `apply/browser.py: _pass_wall` at every place a plan can meet a wall. No site is named in `src/`.

How the login reaches the browser. The worker on the NAS has no 1Password tool and gets no token. At a sign-in page it opens a hold with a one-time public key signed with the app secret. The application page shows which site asks and the button Allow 1Password. The dashboard process that receives the press (the Mac dashboard, where `op` and the 1Password app are) reads the saved login, which is when 1Password shows its own Touch ID or password window, seals it to that key (`core/sealed.py`) and writes the box on the hold. The worker takes the box out as it reads it and types the password. Nothing is stored at rest, and no vault, item or token was created or moved. The dashboard in the container answers that it cannot open 1Password.

Rules as built. Host match: exact host, else the same registrable domain, never another domain; several matches prefer the profile email, else stop; none stops. The password is typed only into a password box on an https page in the page or a same-site frame. One-time code: asked on the application page, typed into one box or split boxes; the saved one-time password is read only with its own press and the timeline says so. Never: creating an account, a captcha on a sign-in page, a checkpoint with no code box, a block page, the final click or anything after it. The intent row (`site_signins`) is saved before the password is sent, so a replay or restart finds it.

Approval rules. Nothing is read without a button press, and no loop triggers a prompt. A missed, dismissed or timed-out prompt keeps the run waiting with the same button (six misses or the hold time end it with a retry-safe stop). The one-time code is a separate approval. Approved but unusable lookups say which case it was.

Retries added, each bounded and written in the timeline: a form that renders late (10 s), the page after the password or a code (30 s, a quiet page for 1.5 s counts as signed in), passing 1Password failures (3 tries, 1 s then 3 s), refused codes (3 codes), the site's resend control only at the owner's request (2), a stale saved session (sign in once, replace the session, continue the same run), page work for one sign-in capped at 240 s. Retries left out on purpose: a refused password without the owner, anything through a block page, captcha or checkpoint, a second sign-in on one site in one attempt, the final submit click, any unbounded loop.

A refused password (owner, 2026-11-12). Detected only as an inline error with the form still showing after the settle wait; a lock or throttle is told apart. The attempt keeps the page and holds: the status block names the site, quotes the site's words as escaped text, links the site's own reset page (https and same registrable domain only; never opened by the app), links the address the run opened for the sign-in page, and names the 1Password item by title. "I changed the password, try again" is the 1Password approval for one more try. If the saved login is unchanged (fingerprint of item id, version, updated time) nothing is read and the page asks a second question; only "Send the same login once more" sends it. Three refusals per site in 24 hours stop further tries until the window passes. A lock waits the time the site named, or 30 minutes. A changed saved login or a sign-in that worked clears the remembered refusal. If the hold runs out the application stays stopped with the same button as its action.

Secrets. The password, the code and the username are not in Mongo, run events, evidence, logs or exceptions (asserted in tests); network rows drop them; password boxes are emptied before the stopped page is photographed.

Fixture tested (`tests/test_signin.py`, 53 tests on `tests/fixtures/signin_site.py` with a deterministic credential source): every case above. Not live verified: no real 1Password read, no real job site, no live application. Also fixed: two tests in `tests/test_cancel_attempt.py` used a step double with an older signature and failed at the previous commit.

Open questions: none blocking. Owner-only steps are in `docs/HANDOFF.md`.

## The stop record: what the app saw when it stopped (2026-11-12)

Owner: "for whatever reason the app stops. Please take a screenshot of the screen with network tab open and post the screenshot in the app", then "don't leave anything out, record it all." `apply/stop_record.py`, on by default (`AMORE_STOP_RECORD`, `AMORE_STOP_RECORD_TRACE`).

What is recorded, for every browser attempt that ends without a confirmed success (handoff, failure, unclear after the final click, cancel, a later look that finds no confirmation, an unexpected error): the screen and the whole page as pictures, unmasked; the page source, address, title and frames; every request from the first page load, with the full address and query, method, type, timing, status, request and response headers, cookies sent and set, form fields, request body, response body, redirect links, failure text, and whether the app's own list of allowed sites refused it; console lines and page errors; Playwright's network archive (zipped HAR with every body, binary ones too) and a Playwright trace.

Limits, shown on the page: a text body over 512 KB is cut in `network.json` and whole in the archive; a binary body is listed by size and type and kept in the archive; the list keeps 5000 requests and 2000 console lines; the page source keeps 8 MB; an archive or trace over 200 MB is not kept and the record says so. The page draws 300 rows and the first 4 KB of a body.

Where it lives: `artifacts/jobs/<job_id>/stop/<attempt>/` on the computer that ran the attempt (the NAS for the worker), files mode 600, each hashed and registered in `artifacts` as kind `stop_record`, served only through the signed-in, hash-verified download route as downloads. The database holds names, sizes, hashes and counts. Nothing captured is in the application's evidence, a run event (one plain line is written), a log, the tracker or its outbox, a model input, or a backup (`core/backup.py` leaves the folders and the rows out).

A dashboard that does not have the files brings them from the dashboard that ran the attempt (`AMORE_STOP_RECORD_ORIGIN`). It signs in with the dashboard password, downloads each file through that same hash-verified route, checks the hash, and stores the bytes under its own artifact folder. The page then shows the picture, the request list and the downloads. Nothing is written into MongoDB, the tracker, a log, a backup or a model input. If the fetch fails, the page says "The files could not be brought over from the computer that ran this attempt." and offers Try again. The NAS stack leaves the variable empty because the files are already on its disk.

What deletes it: the next attempt for the job (the browser clears it before it opens the page, and queuing a retry clears it at once), Start over, and Remove. The copy on the dashboard that brought the files is removed with the record, and again when that page is opened after the record is gone. A sent application, a saved receipt and an unclear result keep theirs, and proof pictures (filled review page, confirmation, unclear result) are never cleared; the pictures of where an unsent attempt stopped are cleared with the record and now show in this card instead of under Proof of applying. When the dashboard that cleared a record is not the computer that holds the files (the Mac dashboard and the NAS worker), the files are removed when that computer next starts an attempt.

This is the one exception to the rule that a sign-in secret is never stored: a stop during or after a sign-in can hold the site password and session cookies. The page says so first.

Capture never changes why an attempt stopped: each part is wrapped, a failed part is named in the record. Fixture tested (`tests/test_stop_record.py`, 13 tests). Not yet seen on the deployed worker with a real stop.

## The application page as one flow (2026-11-13)

Owner: "Do we need to design this page or functionality a little bit to make things easier and smoother for the application process and sign in? If so, I will leave the designing to you." `web/app_flow.py` finishes what `needed_now` decides; nothing in it grants an approval or queues work.

Done:
- One state, one sentence, one main action. `STATES` lists 38 states with the control and label each shows; `docs/ui-copy.md` carries the same table and `tests/test_app_flow.py` walks every row. A stopped application never reads "Nothing is needed from you right now."
- Step tracker in the status block: Prepared, Approved, Opening the job site, Signing in, Filling the form, Verification code, Sending, Confirmed. Signing in and Verification code appear only when they came up. One step is marked now, waiting on you, or stopped here.
- Live: `/applications/{id}/activity` also returns the tracker, the attempt line and the ask. The page repaints the tracker in place and loads again only when the ask changed, never while an answer is being typed. An idle page looks every 12 seconds, so an attempt the app starts by itself is not missed. Without JavaScript the page is the same after a reload.
- A new attempt shows "Attempt N · started <time>". Earlier attempts are one line each under Earlier attempts; the full stop record is kept only for the latest stop.
- Waits in the block (1Password, a code, an answer, a human check) have a large button, focus in the box, and the time the wait ends. A background tab's title starts with "Needs you · "; "Tell me when this needs me" asks the browser for a notice where the browser allows it.
- "I applied on the site" is never the main button for an unsent application. The main button opens the job site; the record form is under More with a tick box, and `POST /applications/{id}/manual` answers 400 without the tick. After the final click with no confirmation seen it is the main step, labelled Record the confirmation. Start over and Remove unused resumes sit under the same More.
- Before approval the header says what is known about signing in where the form lives, in amber when an earlier application stopped there because the site only offered sign up or no login was saved. Settings lists the job sites the app has signed in on.
- Timeline rows wrap beside the time. Sections are ordered by what matters; technical details are last.

Not done, with the reason:
- Sign in now ahead of an attempt. It needs its own worker task and hold, which means `core/worker.py` and `apply/browser.py`; both were off limits to keep the other agents' rebases clean. Retry with the app on an application for that site runs the same sign-in step.
- A count in the side navigation for pages that wait on the owner (`base.html` belongs to the jobs-page work). The tab title carries the notice.
- The browser notice shows only on a secure address (127.0.0.1 or https), so not on the NAS dashboard over plain http.

Fixture tested (`tests/test_app_flow.py`; `tests/test_web.py`). Looked at in headless Chromium on a throwaway dashboard with fictional data, light and dark and at 390 px: the tracker, a hold appearing on an open page without a manual reload, a half-typed code surviving a refresh, the background tab title, no sideways scroll. Commits `064f342` and `2a9d292`, image `2a9d292723b6`, deployed 2026-11-13 15:03, web and worker healthy, Mac dashboard restarted on the same commit. Not yet watched on a live attempt.

## Groundwork for creating a job site account (2026-11-13)

Owner: "I want to be able to create an account if there is none found using 1Password", with Gmail for verification codes, 1Password-suggested passwords, one fixed email, and an item name that says what the login is for. Decisions: new logins go to the vault named `Personal`; mail reading is built from the start. This entry is the groundwork only. Nothing calls it yet: no page offers it, no run creates an account, and no real 1Password item or mailbox was touched.

Done, each fixture tested:

- Approval kind `site_account_create` (`ScopeKind`, `ACTION_SCOPE`), keyed by `site_account_key(user, site, email)` = `account:<user>:<registrable domain>:<first 16 of sha256(email)>`. An approval to apply does not allow it, and a replay does not run it twice.
- Settings, all off or empty by default (`.env.example`): `AMORE_SITE_ACCOUNT_CREATE`, `AMORE_SITE_ACCOUNT_VAULT`, `AMORE_SITE_ACCOUNT_EMAIL`, `AMORE_SITE_ACCOUNT_PASSWORD_TRIES`, `AMORE_MAIL_VERIFICATION`, `AMORE_MAIL_WAIT_SECONDS`, `AMORE_MAIL_GRANT_STORE`. `Runtime`: `site_account_create_enabled()` (needs sign-in on), `mail_verification_enabled()`, `account_email()`, `item_writer()`, `mail_access()`.
- `core/account_items.py`: the item is created with `op item create - --generate-password=<recipe>` and a template on standard input, tagged unconfirmed, before a form is sent. Title `<Employer> job application account (<host>)` or `<Site> job site account (<host>)`, website the sign-in page, username the account email, tags, section "Created by AmoreHireMePls" with an App record. Refused when any login exists for the registrable domain. 3 passwords in all on one item. Rename, retag and archive only with both tags and a matching App record. `answer_account_ask` hands the new login to the task sealed. `SavedLogin.tags`; the sign-in lookup skips `account-not-created`.
- `core/mail_access.py`, `core/mail_codes.py`, `amore mail connect|status|disconnect`: read-only scope only, desktop consent on a loopback address with PKCE, calls limited to the profile, the message list and one message. A withdrawn or expired grant becomes "Mail access needs to be connected again". The lookup takes at most 5 messages sent after the form, to the account email, from the site's registrable domain (or a sender the site's page named), with a passed signature; it returns one code or one link on the site's own domain, the sender domain and the time. Two candidates mean ask the owner. Looks at 5, 10, 20, 30 seconds then every 30 until `AMORE_MAIL_WAIT_SECONDS`; no look reads wider. The grant is left out of backups.
- `apply/signup.py`: `PAGE_JS` describes a page; `read_page` tells a create-account form from a sign-in form, an emailed code prompt, an emailed link, an existing account, a refused password (with the rules it adds), a refused form, a human check, a phone code and a block page; `plan_signup` returns what would be typed and ticked. Consent is ticked, marketing never, anything else required is a question (5 at most).
- Agent skill `.cursor/skills/amore-site-account/SKILL.md`.

Checked against the installed `op` 2.41.1 with `--dry-run` and help only (nothing created, no secret printed): the template on standard input, tags, two websites, a custom section, the recipe forms and their length limits (1 to 64), the unknown-vault error, an API credential template. Not checked live: a real create and what it prints, `op item edit` (field assignments under a section name with spaces, `--tags`, `--generate-password`), `op item delete --archive`, and the approval window for a detached dashboard. Not run: `amore mail connect`; no mailbox was read.

Open questions for the owner:

1. Where the mail grant is kept. The design said "in 1Password, injected like other secrets". Injection through `.env.references` was not built: a missing item would stop every `op run` start, the same file feeds the server, and a weekly re-consent would need a restart. Built instead: one item in `AMORE_SITE_ACCOUNT_VAULT`, read at the owner's press (default), or an owner-only file (`AMORE_MAIL_GRANT_STORE=file`).
2. Symbols. The 1Password generator uses a fixed set (`!@.-_*`) and cannot be limited. When a site names the only symbols it accepts and requires one, the app stops and the owner creates that account by hand.
3. Google marks a consent screen in testing as expiring after 7 days. Publishing the screen, or connecting again weekly, is the owner's choice.

Wired 2026-11-14. `SiteSignUp` runs from `_pass_wall` when sign-in stops with `no_login` or `account_needed` and the switch is on. The application page shows the card; the press grants `site_account_create` for that offer, saves the login first, and the form is sent once (`AccountStore.claim_send`). Settings has both switches, off until the owner turns them on. A security question stops (a concealed 1Password field was not verified). The confirmed tag waits for the next press. Fixture tested in `tests/test_account_run.py` with the local sign-up site and the in-memory `op`. The live 1Password check of one throwaway item was not approved: `op item list` returned an authorization timeout twice, so create, section edit, regenerate, read-back and archive were not verified and no item was created by this check. No mailbox was read.

Exact next step for the owner: turn the two switches on, on the Mac dashboard, and run `amore mail connect` before relying on Check my email. See `docs/HANDOFF.md`.

## Exact next step

Queue item 5 waits for a confirmed employer receipt. Do not draft recruiter Connect notes until one exists. Do not retry the Faire application or mark the BMO job closed; those are owner decisions (handoff questions 4 and 5). The employment-type rule is deployed; the owner uses Check fit again on the skipped job. An already approved package with no task is picked up by the worker; do not start that send by hand. The plain-language fill page is deployed as `329d1c022adf`. The live profile still has no later places; save those before relying on them. Do not confirm a destructive action on the live Workday job unless the owner asks. Open owner questions are 1, 2, 3, 4, 5, 7, 8, and 9. Question 6 is answered: tracker sync keeps running for an expired member.

Greenhouse, Lever, and Ashby form filling, owner notifications, the human-check hold, and one later try are fixture-tested only. None of those paths has been live-submitted. Do not retry the Faire application or mark the BMO job closed; those are owner decisions (handoff questions 4 and 5).

Email stays off until vault `AmoreHireMePls` has item `SMTP` with fields `username` and `password`, `.env` has `AMORE_NOTIFY_EMAIL_TO`, `AMORE_SMTP_HOST`, `AMORE_SMTP_PORT`, and `AMORE_SMTP_USER`, and the two `op://AmoreHireMePls/SMTP/…` lines in `.env.references` are uncommented. Optional: `AMORE_NOTIFY_WEBHOOK_URL`, `AMORE_PUBLISH_URL` or `AMORE_PUBLISH_HOST`.
