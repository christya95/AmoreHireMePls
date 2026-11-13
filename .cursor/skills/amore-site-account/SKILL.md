---
name: amore-site-account
description: Create an account on a job site for this job application app when 1Password has no login for it, save it in 1Password under a name the owner can tell apart, and read the site's verification email. Use for any work on account creation, the 1Password item writer, sign-up page recognition or verification mail, and to diagnose a mail lookup that found nothing, mail access that must be connected again, or a 1Password item left half made.
---

# Creating a job site account

The app creates an account only when all of these hold: the owner turned on both Sign in with saved logins and Create an account when none is saved (`AMORE_SITE_SIGNIN`, `AMORE_SITE_ACCOUNT_CREATE`; both default off), an approved application is running, 1Password has no login at all for the site, and the owner approved creating an account on that one site. An approval to apply never covers it: the scope kind is `site_account_create`, keyed on person, registrable domain and account email (`core/authorization.py: site_account_key`).

## The rules, in order

1. Never create while any login exists for the site's registrable domain (`account_items.existing_logins`). The owner signs in or resets the password instead.
2. The login is saved in 1Password, marked unconfirmed, before the form is sent. A run that dies after the save leaves an item and no account; it is found again by its App record and reused, never doubled.
3. The form is sent once. Replay, retry, restart and a second worker must not send it again (`ActionStore.prepare/execute` with `site_account_key`). An unclear result becomes `needs_verification`.
4. A password the site refuses for a rule gets a new one on the same item: 3 passwords in all (`AMORE_SITE_ACCOUNT_PASSWORD_TRIES`, 1 to 3).
5. A field the profile cannot answer is a question for the owner. More than 5 questions and the form is the owner's to fill.
6. No model sees a form or a mail. Both are untrusted and are read by fixed rules.
7. No secret in a command argument, a log, a note, evidence, a test or a commit. Passwords are made by 1Password, travel sealed (`core/sealed.py`), and exist in the app only in local variables.
8. Only items the app made are renamed, retagged or archived.

## Where each part is

| Part | Code | Runs |
|---|---|---|
| Scope kind and key | `core/authorization.py`, `core/actions.py: ACTION_SCOPE` | anywhere |
| Settings and switches | `core/config.py`, `core/runtime.py` (`site_account_create_enabled`, `mail_verification_enabled`, `account_email`, `item_writer`, `mail_access`) | anywhere |
| 1Password item writer | `core/account_items.py` (`OpCliWriter`, `item_spec`, `item_title`, `recipe_for`, `fits`, `answer_account_ask`) | the dashboard where the 1Password app and `op` are installed |
| Sign-up page recognition | `apply/signup.py` (`PAGE_JS`, `read_page`, `plan_signup`, `signup_control`, `rules_from_text`) | pure functions; the reader runs in the worker browser |
| Mail grant and read-only calls | `core/mail_access.py` (`MailAccess`, `run_consent`, `OpGrantStore`, `FileGrantStore`) | the computer the owner sits at, never the server |
| Narrow lookup and wait | `core/mail_codes.py` (`find_verification`, `wait_for_verification`, `answer_mail_ask`) | same |
| Commands | `amore mail connect`, `amore mail status`, `amore mail disconnect` (`web/cli.py`) | same |

The worker holds no 1Password tool and no mail grant. It asks through a hold; the dashboard answers with a sealed box that only the asking task can open.

## How it works on a new company's site

1. The sign-in step ends with no saved login. `signup_control` finds the site's own Create account control on the same site; a control that leaves the site is not followed.
2. `PAGE_JS` describes the page and `plan_signup` returns what would be typed and ticked: the account email (`rt.account_email()`: `AMORE_SITE_ACCOUNT_EMAIL`, else the profile email), the legal name from the profile, the password's place (never its value), the consent box ticked, marketing never ticked.
3. The owner sees the plan and presses Create. That press is the approval and the 1Password prompt. `answer_account_ask` checks again that no login exists, creates the item with a generated password that fits the page's rules, and seals it to the task.
4. The task types and sends the form once, then reads the page with `read_page(description, before=page_lines(earlier))`.
5. Verification: an emailed code or link is looked up by `find_verification` on the owner's computer and returned sealed. Without mail access, the owner types the code or opens the link.
6. Confirmed: the item's state tag becomes `account-confirmed` and its Status says so. Refused by the site: `account-not-created`, which the sign-in lookup skips.

Adding a site needs no code when its form is ordinary. For one that is not, add a hint to its adapter (whether one account serves many employers, documented mail sender domains, a sign-up path) and a fixture page to `tests/fixtures/signup_pages.py`. Vendor names stay in adapters, never in `apply/signup.py`.

## How the item is named

| Part | Value |
|---|---|
| Title | `<Employer> job application account (<host>)` when the sign-up host belongs to one employer. `<Site> job site account (<host>)` when one account serves many employers. The adapter says which; with no adapter the employer form is used only when the host contains the employer's name. |
| Website | The sign-in page address. A second website for the careers host when it differs. |
| Username | The account email. |
| Tags | `AmoreHireMePls`, `job-site-account`, and one state tag: `account-unconfirmed`, `account-confirmed` or `account-not-created`. |
| Section "Created by AmoreHireMePls" | Created for, Requisition, Job link, Created on, Status, Password rules used, Passwords made, App record. |
| Notes | One plain paragraph saying when and for which job the app made it. No secret. |
| Vault | `AMORE_SITE_ACCOUNT_VAULT`. Empty means nothing is saved and the press answers that the vault is not set; the tool is never left to pick one. |

An item is the app's own only when it carries both tags and its App record matches the record in hand (`OpCliWriter.owns`). Anything else raises `NotOurItem`.

## What stops it

| Stop | Meaning | What the owner does |
|---|---|---|
| `login_exists` | 1Password already has a login for this site | Sign in with it, or reset the password on the site |
| `email_exists` | The site says the email already has an account | Reset the password on the site and save it in 1Password |
| `captcha`, `phone_verification`, `blocked` | A human check, a code sent to a phone, a block page | Create the account by hand |
| `email_first`, `social_only`, `not_signup`, `no_submit` | The page is not an ordinary create-account form | Create the account by hand |
| `password_rules` | The page names the only symbols it accepts, or a length the generator cannot make | Create the account by hand |
| `too_many_questions`, `not_secure`, `no_email` | More than 5 questions, a page that is not https, no account email | Fill the form by hand, or set the email |
| Password limit | The site refused 3 generated passwords | Create the account by hand; archive the unused item |

A question (`plan.state == "ask"`) is not a stop: a security question, a date of birth, a username, a required box that is not plain consent, a name the profile lacks.

## Diagnose a mail lookup

Run `amore mail status` on the owner's computer. It prints the state and the address, never a token.

| State or result | Cause | Fix |
|---|---|---|
| `not_connected` | No grant saved | The owner runs `amore mail connect --client-file <path>` |
| `needs_reconnect` | The grant was withdrawn or expired (a consent screen in testing mode expires after 7 days) | The owner runs `amore mail connect` again; no file is needed the second time |
| `locked` | The grant is kept in 1Password and this process has not read it yet | The owner presses the button on the application page, or runs `amore mail status` |
| `wrong_mailbox` | The connected address is not the account email | Connect that mailbox, or set `AMORE_SITE_ACCOUNT_EMAIL` |
| `none`, then `timeout` | Nothing matched inside the window | Check the rules below; the owner types the code |
| `ambiguous` | Two different codes or links matched | The owner picks; the app never guesses |
| `elsewhere` | The verification link is on another site | The owner opens it; the app never follows it |
| `stale` | The form was sent too long ago | Ask the site for a new code |

A message counts only when all hold: sent after the form was (60 seconds of slack), to the account email, from the site's registrable domain or a sender the site's own page named, with a signature that passed for that domain, among at most 5 messages. A code is 4 to 8 characters next to a word like code. A link is https, on the site's own registrable domain, and says verify, confirm or activate. A retry never widens any of this. If a site's mail fails these rules, the answer is the owner typing the code, not a looser rule.

## Diagnose a half-made 1Password item

- An item tagged `account-unconfirmed` whose account does not exist: the save worked and the form was never sent, or the result was unclear. Start the application again; the same item is reused when its App record matches. Do not make a second one.
- An item with no account behind it that will not be used: mark it `account-not-created` (`mark_not_created`) so sign-in skips it, then the owner archives it from the page (`OpCliWriter.archive`).
- The site accepted a password the item does not hold: this cannot happen through the app (the password is saved before it is typed). If the owner changed it by hand, they save the new one in the same item.
- A save that failed says why by category: not approved in time (ask again from the page, it is not a failure), vault not found (`AMORE_SITE_ACCOUNT_VAULT`), 1Password not signed in, the tool missing.

## What to run

```bash
uv run pytest tests/test_account_items.py tests/test_mail_codes.py tests/test_signup.py tests/test_signin.py tests/test_actions_authorization.py
uv run ruff check src tests && uv run ruff format --check src tests
```

Doubles: `tests/fixtures/fake_op.py` (an in-memory `op`), `FakeGoogle` and `FakeMailbox` in `tests/test_mail_codes.py`, page descriptions and HTML in `tests/fixtures/signup_pages.py`. A change must pass for both fictional candidates.

## Never

- Create a live account, send a live sign-up form, or run a live application to test this.
- Run `amore mail connect` for the owner, read the owner's mailbox, or loosen the lookup to make a mail match.
- Create, edit, archive or delete an item in the owner's 1Password. `op` may be asked for its version, its help and a template; `--dry-run` only.
- Press an approval, or trigger a 1Password prompt from a background loop.
- Put a password, a code, a token or the client file's contents in a log, a note, an argument, a prompt, a test or a commit.
- Name a site, an employer or a person in `src/`.
