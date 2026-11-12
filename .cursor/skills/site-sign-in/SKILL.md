---
name: site-sign-in
description: Get this job application app signed in to a job site with a login saved in 1Password, and diagnose a sign-in that stopped. Use when a run stops at a sign-in page, a verification code, a refused password, a locked account, or a 1Password approval that timed out, or when adding a job site that needs an account.
---

# Site sign-in with saved logins

The app signs in on a job site only when all of these hold: the owner turned on Sign in with saved logins (Settings, or `AMORE_SITE_SIGNIN`; default off), an approved application is running, and the owner pressed the button on the application page. Signing in is preparation. It never submits anything and it never grants an approval.

## Where each part runs

| Part | Where | Code |
|---|---|---|
| Finds the sign-in form, types, reads the result | Background task (worker) browser | `apply/signin.py` (`SiteSignIn`), called from `apply/browser.py: _pass_wall` |
| Asks the owner, waits | Hold record `human_holds`, shown in the application page status block | `core/human_check.py`, `web/chrome.py: needed_now` |
| Reads 1Password | The dashboard process that received the button press. It must run where the 1Password app and `op` are installed | `web/routes/signin.py`, `core/site_signin.py: answer_ask`, `core/credentials.py: OpCliSource` |
| Carries the login to the browser | Sealed to a one-time key the task made for that ask; removed from the database when read | `core/sealed.py` |
| Remembers how each sign-in ended | `site_signins` (site, fingerprint of the saved login, result, fix-it links; no secret) | `core/site_signin.py: SignInMemory` |

The worker has no 1Password tool and no token. A dashboard without `op` (the one in the container) answers "This dashboard cannot open 1Password" and leaves the ask open.

## Get the app signed in to a new site

1. The owner saves a Login item in 1Password whose website is the job site's address (the careers host or its sign-in host). One item per site.
2. Turn on Sign in with saved logins under Settings.
3. The site's hosts must be in `AMORE_BROWSER_ALLOWED_HOSTS`. During a sign-in the app may pass through other hosts of the same registrable domain; it never opens another site for it.
4. Run the approved application. At the sign-in page the application page shows which site asks, and the button Allow 1Password. Press it on the dashboard that runs where 1Password is installed. 1Password shows its own Touch ID or password window. The app never sees that password and never asks for it.
5. After a sign-in that worked, the session is saved the same way every session is (`<session dir>/<start host>.storage.json`), and later runs skip the form.

Never create an account, never type a password into chat or the dashboard, never copy items between vaults, and never run a live application to test this. Use the fixture: `tests/fixtures/signin_site.py` and `tests/test_signin.py`.

## How a saved login is matched

- The page's exact host wins. Otherwise a login saved for another host of the same registrable domain is used (last two labels; three when a two-letter country code follows a generic label such as `co`). An IP address matches only itself.
- A login for a different registrable domain is never used.
- Several matches: the one whose username is the profile email. Still several: stop and say so. None: stop and say so.
- The password is typed only into a password box on an https page (loopback http in tests), in the page itself or a frame of the same site.

## Approval rules

- Nothing is read from 1Password without a button press. No background loop ever triggers a prompt.
- Approval can be needed more than once: the session timed out, a second site, the one-time code (always its own press), a later run. Each time the page says so with the same button.
- A missed, dismissed or timed-out prompt is not a failure. The ask stays open, the page says "1Password was not approved in time", and the owner presses again. After `APPROVAL_ASKS` (6) misses, or when the hold time (`AMORE_HUMAN_CHECK_HOLD_MINUTES`) runs out, the attempt stops with Retry with the app, which is safe.
- Approved but nothing usable: the page says which (no saved login, more than one, 1Password not signed in, the app integration is off, the tool is missing) and what to do.

## Verification codes

If the site asks for a code after the password, the application page shows a code box. The typed code goes into one box or split boxes. A refused code is asked for again, 3 codes in all. "Send a new code" presses the site's own resend control, only when the owner asks. When the saved login has a one-time password, "Use the code from 1Password" reads it with its own approval, and the timeline says so. No code prompt: the run just continues.

## A refused password

The app never sends a refused password again by itself, in the same run or a later one.

1. The attempt keeps the page and holds. The status block shows the site, the site's own words (as untrusted text), the site's reset link (only https and the same registrable domain, never opened by the app), the sign-in link the run used, and which 1Password item to edit by its title.
2. The owner changes the password on the site, saves it in the same item, and presses "I changed the password, try again". That press is the 1Password approval.
3. If the saved login has not changed since the refusal (same fingerprint of item id, version and updated time), nothing is read. The page asks a second question, and only "Send the same login once more" sends it.
4. Three refusals for one site in 24 hours stop further tries until the window passes. The links stay.
5. A lock or a throttle is not offered an immediate retry. The app waits the time the site named, or 30 minutes when it named none.
6. A changed saved login, or a sign-in that worked, clears the remembered refusal by itself.

If the hold runs out, the application stays stopped with the same button as its one action.

## Retries

Safe and built in: waiting for a form that renders late, a redirect chain after the password (30 s), a passing 1Password failure (3 tries with backoff), a refused code (3 codes), a stale saved session (sign in once, replace the session, continue the same run).

Never: a refused password without the owner, anything through a block page, a captcha on a sign-in page or a verify-it's-you page with no code box, a second sign-in on the same site in one attempt, the final submit click or anything after it.

## What each stop sentence means

| Timeline sentence | Meaning | Next |
|---|---|---|
| "… asked you to sign in." | The switch is off, or the run could not wait | Turn the switch on, or sign in to the saved session yourself, then retry |
| "No saved login for … was found in 1Password." | No item's website matches the site | Save one (create the account on the site yourself first if needed), retry |
| "1Password has more than one saved login for …" | Not narrowed by the profile email | Keep one, retry |
| "… did not accept the saved password." | Inline error with the form still showing | See A refused password |
| "… says the account is locked or has had too many attempts." | Lock or throttle | Wait or unlock on the site |
| "… refused the saved login several times, so the app stopped trying for now." | The ceiling | Wait for the time shown |
| "… asked for a verification code and none was typed in time." / "… did not accept the verification codes that were typed." | Code step | Retry with the app; the password is asked for again by the site |
| "… asked for a human check on its sign-in page." | Captcha at sign-in | Sign in yourself |
| "… asked you to verify it is you, with no code box to fill." | Checkpoint | Finish it yourself |
| "… blocked the app from opening the page." | Block page | Signing in will not change it |
| "… only offers a passkey or a security key." / "… needs an account, and the page only offers sign up." | Not something the app does | Do it on the site yourself |
| "… did not finish signing in before the wait ended." | No clear result | Retry once; after two in a row the app asks before sending the same login again |
| "1Password was not approved in time." | No press, or the prompt was missed | Retry with the app |

## Troubleshooting

- Authorization timeout: 1Password's prompt was not approved within its window (the dashboard waits up to 120 s for it). Nothing failed. Press the button again and watch for the 1Password window; it can open behind other windows. If it never appears, open the 1Password app, unlock it, and check Settings, Developer, Integrate with 1Password CLI.
- "This dashboard cannot open 1Password": the page is served by a dashboard without `op`. Open the same application on the dashboard that runs on the computer with 1Password.
- "The sign-in request could not be checked": the dashboard and the worker were started with different `AMORE_APP_SECRET` values.
- The button does nothing visible for a while: the request is waiting for the 1Password window. The button reads "Waiting for 1Password".
- To see what the app remembers about a site: `site_signins` rows for the registrable domain, newest first. `status` is one of started, signed_in, rejected, locked, unclear, code_failed, stopped.
- To check a saved session: the file under the private session directory named for the start host. A stale one is replaced after the next sign-in that works.

## Secrets

The password, the one-time code and the 1Password account password are never written to Mongo, run events, evidence, logs, model input or exceptions. Network rows drop them. Password boxes are emptied before a stopped page is photographed.
