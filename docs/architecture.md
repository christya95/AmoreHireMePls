# Architecture

The installable package is still `amorehiremepls`. Imports are absolute (`from amorehiremepls.jobs.discovery import Discovery`). The command is `amore` (`amorehiremepls.web.cli:app`).

## Packages

| Package | What lives here |
| --- | --- |
| `core` | Configuration, MongoDB, logging, ids and time, authorization, the action store, the model runner and skill catalog, LangGraph orchestration, process wiring, the worker, notifications, the human-check hold, backups. |
| `candidate` | Profile facts and preferences, `profile.toml` load and export, extra documents dropped in `candidate/`. |
| `jobs` | Job records and deduplication, discovery and fit, cached employer logos. |
| `apply` | Tailoring, form answers, the browser, captcha, the generic site sign-in step, and `apply/ats/` (Greenhouse, Lever, Ashby). |
| `records` | The tracker clone and the sync outbox. One writer. |
| `outreach` | Recipient identity and drafted messages. Sending stays behind an authorization scope. |
| `web` | FastAPI dashboard, Jinja templates, local CSS and JS, UI copy, and the CLI. `web/chrome.py` holds templates and page helpers. `web/routes/` is one module per area of the site. `create_app` owns the session, CSRF and the render closure, then registers those modules. A signed-in person is bound with `user_scope` for that request. `core/accounts.py` stores people in the same database (`users`: role, access_until, status, argon2id password). Owned records carry `user_id`. Admin access does not expire; a member has 365 days. An expired or turned-off member is not worked, and tracker sync still flushes their queued ledger items. |

A new site adapter goes in `apply/ats/`, is registered from `apply/ats/__init__.py`, and returns a browser plan. Extend `apply/browser.py` for a new kind of step. Do not click a specific site's layout from a route.

## The stop record

`apply/stop_record.py`. `BrowserAdapter.run_plan` asks `StopRecords.begin(job_id)` for a recorder before it opens the page; that call first clears the earlier record for the job. The recorder turns on Playwright's own network archive (`record_har_path`, bodies attached) and trace, listens for console lines and page errors, and is told about each request the app's host gate refuses. In `finally`, when the attempt is not a confirmed success, it photographs the page, saves its source, closes the archive and trace, builds `network.json` from the archive, hashes every file and hands the list to `StopRecords.landed`, which registers one `artifacts` row per file (kind `stop_record`). A confirmed success deletes the folder. Every recorder call is wrapped so that a failure is named in the record and never changes the stop. `web/stop_view.py` and `web/routes/stop_record.py` show it; the request rows are read from the file only when the panel is opened. Nothing captured is put in evidence, events, logs or the database.

## Signing in on a job site (extension point)

One generic step signs in on every site: `apply/signin.py` (`SiteSignIn`). It finds the form by structure (a password box, the username box before it, a button), so a new site needs no code. `apply/browser.py: _pass_wall` calls it wherever a plan can meet a wall, and stops with the sentence for that kind of page (`core/site_signin.py: STOPS`, `WALLS`) when it cannot. The pieces, and where to extend each:

| To change | Edit |
|---|---|
| Where saved logins come from | Implement `core/credentials.py: CredentialSource` (`available`, `logins`, `secret`, `one_time_code`) and return it from `Runtime.credential_source()`. `OpCliSource` is the 1Password command line tool. Tests use `FakeSource` in `tests/test_signin.py`. |
| How a page host matches a saved login | `core/credentials.py: choose_login`, `registrable_domain`. Never across registrable domains. |
| What a sign-in page, a refusal, a lock or a code prompt looks like | The regexes and `_FORM_JS` in `apply/signin.py`, and `SIGNIN_CODE_WORDS` in `apply/browser.py`. Add a fixture page to `tests/fixtures/signin_site.py` first. |
| A new reason to stop | A `Stop` in `core/site_signin.py: STOPS` (timeline sentence, what to do, whether a plain retry is safe). The status block reads it in `web/chrome.py: needed_now`. |
| Ceilings | `SignInLimits`, `APPROVAL_ASKS`, `UNCLEAR_LIMIT`, `REJECT_CYCLES`, `REJECT_WINDOW_HOURS`, `LOCK_WAIT_MINUTES`, and the `AMORE_SITE_SIGNIN_*` settings. |

The lookup runs in the dashboard process that received the owner's button press (`web/routes/signin.py` → `core/site_signin.py: answer_ask`), because that is where the password manager can show its own approval window. The browser task never reads the password manager. It publishes a one-time public key on the hold record, signed with the app secret; the dashboard seals the login to that key (`core/sealed.py`, X25519 + ChaCha20-Poly1305, bound to the ask, the site and the step) and the task takes the box out of the database as it reads it. `SignInMemory` (`site_signins`) saves the intent before a password is sent and how each attempt ended, without any secret; it is what stops a refused password from being sent again and what a replay checks first. The operating guide for agents is `.cursor/skills/site-sign-in/SKILL.md`.

## Creating a job site account (groundwork, not wired)

Four site-neutral parts exist with fixture tests and no caller yet. `core/account_items.py` saves a new login in the password manager with a generated password before any form is sent (`ItemWriter`, `OpCliWriter`, `answer_account_ask` as the twin of `answer_ask`), names it so the owner can tell it apart, and touches only items carrying its own marks. `apply/signup.py` reads a description of a page (`PAGE_JS`) and returns what the page is (`read_page`) and what would be typed and ticked (`plan_signup`); it never acts. `core/mail_access.py` holds read-only mail access (`MailAccess` over a `GrantStore`; the consent runs on the owner's computer) and `core/mail_codes.py` looks up one verification code or link by fixed rules (`find_verification`, `wait_for_verification`, `answer_mail_ask`). The approval kind is `site_account_create` with `core/authorization.py: site_account_key`. To change where logins are saved implement `ItemWriter`; to change the mail service implement `MailReader`; to change where the grant is kept implement `GrantStore`. The operating guide for agents is `.cursor/skills/amore-site-account/SKILL.md`.

LangGraph checkpoints on the NAS were checked before the move (2026-11-05): `lg_checkpoints` (11 documents) and `lg_checkpoint_writes` (35 documents) store no `amorehiremepls.*` module path, so the package move does not invalidate a saved run.

## Docstrings

A package and a module start with one or two sentences that say what it is for. A function gets a docstring when the name does not already state the rule it enforces (a limit, a value that must stay blank, an idempotency or receipt check). Do not restate the code.

The queue asked for 22 high-value comments. No list of those 22 exists in this repository, in `CURSOR_PROMPT.md`, or in `docs/PROGRESS.md`. No comments were added to fill a count. Comments already in the code, including the form rules, the sensitive-question blanks and the receipt checks, stay as they are.
