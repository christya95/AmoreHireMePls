# Dashboard wording: plain words → technical terms

The dashboard is written for someone who is not a developer. Every internal name has one plain replacement, decided
here and applied from one place: the tables in `src/amorehiremepls/ui_settings.py` (`STATUS_LABELS`, `NEXT_ACTIONS`,
`STEP_HELP`, `GLOSSARY`, `PAGE_INTROS`, `SCREENSHOT_LABELS`) plus `PAGE_TITLES`, `NAV` and `FLASHES` in `web.py`.
Templates use the `status()` macro (plain label + raw state chip), the `hint()` macro (a "?" that opens the glossary
entry) and the `label` filter; they never spell a status out by hand.

Where the technical term still appears: the small grey `<code class="raw">` chip next to a status or glossary title,
the "Technical details" `<details>` blocks, `<pre>` dumps, and the Help page, which lists plain → technical for every
status, step and concept. `tests/test_copy.py` renders every page with fixture data and fails if a denylisted term
appears anywhere else.

## Workflow steps (job page stepper, Help)

| Plain (shown) | Technical (grey chip) | Tooltip on the step |
|---|---|---|
| Import | `import` | The job ad is saved here from a link you pasted or a job board the app watches. Nothing is sent. |
| Check fit | `qualify` | The app checks the job against your rules and explains how well you match. Nothing is sent. |
| Tailor resume | `tailor` | The app writes a version of your resume for this job, using only facts from your approved resume. |
| Read application | `review` | You read the resume and the form answers exactly as they would be sent. |
| Approve | `authorize` | You give the go ahead for this exact application. Change anything and you will be asked again. |
| Apply | `submit` | The app applies on the job site, or hands it to you when the site needs a person. |
| Save to tracker | `record` | The application is saved; with a tracker set up it is also written to your private GitHub tracker. |
| Contact recruiters | `outreach` | You add recruiters at that employer and the app drafts short notes. You send them yourself. |

## Pages and navigation

| Plain | Technical / previous |
|---|---|
| Overview | index |
| Jobs | jobs |
| Applications → tabs "Prepared applications" and "Background tasks" | applications / review packages, worker runs |
| This application (one application) | review package |
| Resumes | Documents / artifacts |
| Recruiters | Outreach |
| Past applications | History / tracker import |
| GitHub tracker | Sync / outbox |
| AI usage | Usage / tokens / skills |
| Approvals | Authorizations / scopes |
| Your profile | Candidate profile |
| Background task | run (LangGraph thread) |

## Concepts ("?" hints and Help → Words)

| Plain title | Technical term | Key |
|---|---|---|
| Approval | authorization scope | `authorization` |
| Fingerprint | material hash | `material_hash` |
| Prepare several at once | bounded batch | `batch` |
| Approval for several jobs | batch scope | `batch_scope` |
| Result unclear | needs_verification | `needs_verification` |
| Finish on the site yourself | handoff | `handoff` |
| Sign in on the job site first | a signed-in LinkedIn form can be filled after approval; a verify-it's-you page and a step the app cannot fill stay manual | — |
| Human check | captcha / challenge | `human_check` |
| Human check needed | attention card and notification while the page is held | — |
| Continue | same approval, same package; the form is not sent again | — |
| Trying once more later | `challenge_retry` | `challenge_retry` |
| Notifications | Settings card: Email and Webhook are "Set up" or "Not set up"; button "Send test notification" | — |
| Alert to you | `owner_notification` | `owner_notification` |
| Emergency stop (stops all sending and applying immediately) | emergency stop | `emergency_stop` |
| Saving to GitHub | sync outbox states | `sync_states` |
| Sending messages | outreach scope | `outreach_scope` |
| Fit score | fit rank | `fit_rank` |
| Facts check (every claim comes from your approved facts) | grounding | `grounding` |
| Proof of applying | submission evidence | `evidence` |
| Copy (application page: Technical details, and Technical details of the application) | copies the exact text in that block; the button then says Copied, or Copy failed | — |
| Log (Overview, closed until opened, only when recent steps exist) | saved step lines: time and message, newest first | — |
| The form is being filled in now. | the whole status line while a fill is actually in progress | — |
| Nothing is being filled in right now. | shown when no form is open and no saved step says more | — |
| [Field] was typed as [value]. No list of places appeared, so the form is waiting. | the latest saved event says a place was typed and no list appeared, and a fill is not in progress. The field and the value come from that event. | — |
| [Field] was typed as [value]. A list of places appeared, but none matched, so the form is waiting. | the latest saved event says a list appeared and none of the places matched | — |
| The form was not opened. Showing the result already saved. | nothing is running and the latest pass did not open the form. This is the status line, not only a log row. | — |
| Answer received. It will be typed into the same open form. | the green note only while that answer is not yet in the saved events. Once a saved event says it was typed, or that no list appeared, the note is removed and the status line is the only sentence. | — |
| Last time, [question] was filled in as [typed]. This page now says [current]. That new answer has not been typed yet. | the answer on the page and the value from the last fill differ; shown under What to do now and What happened | — |
| Failed (red) | the headline after an attempt failed, including when an approval is still on file. Approved stays the headline only when that attempt has not failed. Applying now stays while a form is actually being filled. Applied stays after a success. | `failed` |
| A value typed on this application | kept when the form is prepared again, for every field. A profile fact, including city and province, fills a field only when nothing was typed for it. | — |
| Fill in the form | after the answers change, approves them if needed and opens the form with those answers | — |
| Things the job asks for that you do not have | one coloured list row per skill gap from the saved fit. Missing answers are not in this list. The resume note under it is its own short note | — |
| Still needed | plain sentences for missing answers and blockers. Examples: "A resume still needs to be selected." "Email is still needed." A form label becomes "[label] is still needed." An unknown key becomes "Something else is still needed." Raw field keys are never shown. | — |
| Approved for this job · used 0 of 1 · ends [local time] | the approval line. No raw id. | — |
| Proof of applying | a list that scrolls inside its card (16rem): one status chip, the picture, the time, and the page address per picture. The status word is not repeated as text; the short fingerprint is a muted code line. | — |
| What happened last time | the previous attempt: its status and reason, Technical details with Copy, the application id and the code of what would be sent (both in grey chips), Technical details of the application | — |
| Code of the file you uploaded (optional) | the optional hash box on the "I applied on the site" form | — |
| Use this answer (application page, while a required field is waiting) | the same open form receives the typed answer and continues; a sensitive value is not written into the log | — |
| Try to solve captchas automatically (Settings → Applying; hint: "Uses the AI model; some job sites forbid this and may discard flagged applications.") | `AMORE_CAPTCHA_AUTOSOLVE` / control `captcha_autosolve` | — |
| Retry with the app (application page, after a human check or failure) | re-plan: cancel waiting run, clear `form_plan.fields`, new run | — |
| Answers that will be sent | review package answers. Intro: "A blank field marked needed still has to be answered. Change anything below before you approve. Saving asks for approval again." | — |
| Where it comes from (your profile, your work history, typed on this page, your resume file) | answer `fact_ids` | — |
| Rebuild the resume | regenerate documents | `regenerate` |
| Background task | run | `task` |
| Copy of the job ad | posting snapshot | `snapshot` |
| Your GitHub tracker | tracker / sync | `tracker` |

## Statuses (coloured label; raw state in the grey chip)

Applications: `queued` Queued · `preparing` Preparing · `ready_for_review` Waiting for your approval · `needs_input`
Needs your answers · `authorized` Approved · `executing` Applying now · `handoff` Finish on the site
yourself · `challenge_retry` Trying once more later · `needs_verification` Result unclear, check it · `submitted` Applied ·
`failed` Failed (red; this is the headline once an attempt has failed, even if an approval is still saved) · `duplicate` Already applied · `blocked` Blocked by your rules · `denied` Not approved.

Jobs: `new` New · `needs_review` Needs your decision · `qualified` Good fit · `applied` Applied · `applied_historical`
Applied (from your tracker) · `rejected` Rejected · `excluded` Skipped by your rules · `closed` Closed.

Background tasks: the page is "Still waiting" (in the queue) and "Already finished" (not in the queue). Hide removes a finished row from the list only. Cancel stops a task that has not started and leaves the job and the application. `claimed` Starting · `running` Running · `waiting` Waiting for you · `paused` Paused · `cancelled`
Cancelled · `succeeded`/`completed` Finished. Why it waits: `awaiting_authorization` Waiting for your approval ·
`awaiting_input`/`needs_input` Needs your answers · `handoff` Finish on the site yourself · `emergency_stop` Stopped by
the emergency stop.

Saving to GitHub: `sync_pending` Waiting to save to GitHub · `committing` Saving to GitHub now · `committed_local` Saved
here, not on GitHub yet · `local_only` Saved here only · `synced` Saved to GitHub · `conflict` GitHub save needs you ·
`not_applicable` No GitHub save needed.

Approvals: `active` Active · `revoked` Withdrawn · `expired` Expired · `exhausted` Used up · `single` One job · `batch`
Several jobs · `application_submit` Applying to a job · `outreach_send` Sending a message.

Recruiter notes: `draft` Draft · `sent` Sent · `recorded` Recorded as sent · `connection_request` Connection note ·
`outreach_first_contact` First message · `follow_up` Follow up.

People never to contact: `unresolved` Not yet identified · `resolved` Identified. How a resume was written:
`deterministic` Rules only, no AI · `model` AI, checked against your facts. Where a record came from: `local` This app ·
`tracker_import` Your GitHub tracker · `manual` Added by you · `url_import` Pasted link.

## Proof of applying pictures

`summary` Filled review page · `receipt` Confirmation page · `ambiguous` Unclear result · `challenge` Human check ·
`validation` Form complaint · `handoff` Needs you · `receipt_mismatch` Confirmation for another job? · `failed` Stopped early.

## Stop reasons (`plain_failure`, filter `failure`)

A failed attempt keeps its technical reason in the evidence; the page shows one sentence that ends with "Nothing was sent."

| Shown | Stored reason starts with |
|---|---|
| The form had no place to attach the resume. | `resume field not found: …` |
| The resume did not show as attached on the form. | `could not confirm the resume was attached: …` |
| The form did not have: Phone, Resume/CV. | `the form is missing fields the app expected: …` |
| The job site did not respond in time. | `the form did not respond in time …` / `timeout before final action: …` |
| The job site reported a problem: … | `the form reported a problem: […]` / `form validation errors: […]` |
| The resume file was not where the app expected it. | `upload path outside artifact directory` / `upload file missing` |
| The job site is not on the list of sites the app may open. | `host … not in allowed hosts` |

## Answers on the application page (2026-11-05)

| Shown | Meaning |
|---|---|
| personal (pill) · from your self-identification answers | The form's own self-identification question offered a choice that matches an answer Joss saved; it is sent only when the package is approved. |
| personal (pill) · the decline option is chosen / left blank | No saved answer matches the choices; the explicit decline is used when the form insists, otherwise the question stays blank. |
| salary estimate (proposed) · proposed from salary research; approve or edit | No pay range was posted. One bounded estimate (title, location, seniority hints, your floor) produced the figure; "Why this figure" shows the rationale, confidence and source kinds. Never below the saved minimum. |
| your pay rules and the posted range · raised to your minimum | The posted range straddles the minimum, so the figure inside the range is at least the minimum. |
| posted range is below your minimum | The whole posted range is under the floor; the answer is left for Joss. |

## Single words

| Shown | Instead of |
|---|---|
| job ID | requisition / req |
| job site | ATS |
| human check | captcha / challenge |
| form complaint | validation error |
| Greenhouse board, Lever board, Ashby board | apply route / ATS adapter name |
| file, resume | artifact, document |
| approval | authorization scope |
| approval for several jobs | batch scope |
| waiting to save to GitHub / saved here only | outbox, sync_pending, local_only |
| finish on the site yourself | handoff |
| result unclear, check it | needs_verification |
| background task | run, worker run, thread |
| every claim comes from your approved facts | grounding |
| copy of the job ad | posting snapshot |
| fit score | fit rank |
| person | recipient |
| company logo (Settings → Appearance, on by default; off shows initials; downloaded once by the app, never an AI call) | favicon, employer icon |
| other profile or email for the same person | alias |
| check / record the result | reconcile |
| the app is idle, the app is on a task, background tasks are not running | the worker, heartbeat, background tasks running while only queued |
| AI model, AI connection | model provider |
| words in / out (unit shown as a `tokens` chip) | tokens |
| rules only | deterministic |
| downloaded / latest version / not on GitHub yet | clone / HEAD / ahead |
| never shown | idempotency, checkpoint, CSRF, material hash (as a phrase), SHA-256 (only inside `<code>`) |

## The status block and the folds (application and job pages, 2026-11-06)

One block sits at the top of both pages (`status_block` macro in `_macros.html`, data from `chrome.needed_now` through
`routes.applications.application_view`). It has the status chip, one or two sentences from the latest saved event, and,
only when the app needs the person, one ask in bold with its one control beside it. Nothing else on either page asks for
anything. The reason sentence is added only when it says something the headline does not (`reason_adds`); the replay
label and bookkeeping words never appear.

| State | Sentences | Control |
|---|---|---|
| nothing needed | "Nothing is needed from you right now." | none |
| the form is being filled, final click not yet noted | "Nothing is needed from you right now. If you want, stop this attempt. Nothing is sent, and you can try again later." | Cancel this attempt (secondary) |
| failed, clearable | headline from the saved event, e.g. "The form was not opened. Showing the result already saved." · the stop reason · "Nothing is needed from you right now. Nothing was sent. To try again, use Start over." | none (Start over is in the header) |
| applied | "Nothing is needed from you right now. Applied and recorded. Next: short notes to recruiters at this employer." | Contact recruiters (secondary) |
| a check appeared, one more try queued | "Nothing is needed from you right now. A check appeared. The app tries once more later, then tells you if it still needs you." | none |
| ready for you to read | "Read the resume and the answers, then approve this exact application. Your approval covers this job, these answers and these exact files only. If anything changes, you will be asked again." | Approve this exact application · Approval options (hours, note) |
| earlier approval does not cover this version | "Your earlier approval does not cover this version. Approve it again." The saved reason stays in a muted chip. | Approve this exact application (the same approval form) |
| one answer missing | "[Label] is still needed. Type it here and save. The application is then ready for your approval." | one text box or list, Save this answer |
| several answers missing | "Some answers are still needed." | link to Answers that will be sent |
| no resume | "A resume still needs to be selected." | Select a resume (profile page) |
| a field is waiting on the open form | the field's label · "The same page is still open. Type the answer here and it is entered on that form. The form is not started over." | text box, Use this answer |
| human check | "The page is being held on this computer at 127.0.0.1:[port]. Finish the check there. Continue uses the same approval and does not send the form again." | Continue |
| answers changed since the last fill | "Fill in the form with the answers on this page. The job site is opened with these answers. Nothing else changes." | Fill in the form |
| approved, and nothing is in progress | "This application is approved. The app opens the job site with these answers. Your approval already covers them." | Apply with the app |
| failed or human check, retry possible | "The site asked for a human check last time." or "The app can try again. It reads the form again and prepares the answers afresh. If your approval still covers these exact answers and files, it applies right away; if anything changed, you are asked again before anything is sent." | Retry with the app |
| stopped before sending, and the approval still covers this package | "The app can try again. Nothing was sent. It opens the job site with these same answers. Your approval already covers them." The record form stays under What happened last time. Hidden while a task is queued, claimed, running, or paused, or waiting for anything other than this handoff. An approval for an older package asks you to approve again instead. | Retry with the app, and I applied on the site |
| finish on the site yourself | "Apply on the job site yourself, then record the confirmation here." | I applied on the site |
| result unclear | "Check the job site, then record whether it went through." | Save the result |

Below the block and the header (name, employer · location, Open the job, Apply page, Start over), every
section is a `<details>` fold (`fold` macro), closed unless it matters now: What would be sent (open while answers are
missing or the application waits for approval), Still needed (open when a line exists), Things the job asks for that you
do not have (closed, count in the meta), Your approval (closed), Proof of applying (open after a failed, applied or
unclear attempt), What is happening (open while a task is working or a form is being filled), What happened last time
(closed unless the app handed the form to you). The job page shows the same block with the same sentences; its control is
a link to the application page.

## Colour and the technical chip (design notes)

- One semantic system: good (`ok`, green), waiting (`warn`, amber), problem (`bad`, red), info (`info`, blue), neutral
  (grey). The `tone` filter maps every state to one of these; the fit ring bands use the same tones. Each tone has a
  text colour that passes AA on its tinted pill in both themes (`tests/test_design.py` checks the ratios).
- The grey technical chip next to a status is always rendered by the `status()` macro but, inside tables, is only
  shown when the row is hovered or focused. Detail pages (job, application, task) and Help show it all the time.
- IDs and hashes live in muted sub-lines (`<span class="sub mono">`) or Technical details, never in a first column.

## Profile page: the profile file and the rules form

| Plain (shown) | Technical | Where |
|---|---|---|
| Your profile file | `candidate/profile.toml` | card title; the path stays in `<code>` |
| not found / ready to load / changed since last load / loaded | `TomlStatus.state` = `missing` / `new` / `changed` / `loaded` | pill on the card |
| Load profile file / Load again | `POST /profile/load-toml` | the only way the file is applied |
| Your profile file was loaded. The counts are on the profile file card. | flash `toml_loaded` | after a load |
| The profile file could not be read. Nothing was changed; the reason is on the profile file card. | flash `toml_invalid` | `ProfileTomlError` |
| Last load: facts N, answers N, jobs N, education N, skills N; sections … | `ImportReport` (counts only, never values) | note under the pill |
| Your profession | `candidate.profession` | rules form; fed to the AI as `candidate_profession` |
| Kinds of role you want / Kinds of role you do not want / Exceptions to that | `role_families` / `excluded_role_families` / `excluded_role_family_exceptions` | rules form |
| Places you want / If your city is not offered, try these places / Remote jobs must say they are open to / How to name your area in messages | `locations` / `location_fallbacks` / `remote_eligible_terms` / `region_label` | rules form |
| Currency / Lowest base salary you accept / Salary you aim for | `compensation.currency` / `floor_base` / `target_base` | rules form |
| Extra phrases that mean a very small company in your field | `startup_signals_extra` | rules form |
| Roles to ask about / One line about what you do | `outreach_rules.role_phrase` / `one_liner` | rules form; used by `template_connect_note` and `draft_outreach` |
| Page limit / Bullets per role, at most / Words the AI may use even if your resume lacks them | `resume.max_pages` / `max_bullets_per_role` / `allowed_terms` | rules form |
| No hyphens or dashes in a rewritten summary | `style.no_dashes` | rules form |
| Your rules were saved. | flash `preferences_saved` | after the form |

The profession word is never hard coded anywhere in copy: where a sentence needs it, templates use `profile.profession()`
and read well when it is "professional"; where a name is needed they use `display_name`/`first_name()` and read well when empty.
Dates use the `local` and `local_day` filters (`AMORE_TIMEZONE`) and `tz_label()` for the zone name; the old `toronto`
filter names remain only as aliases.

## Signing in on a job site (2026-11-12)

The old combined sentence "login or human verification challenge detected" is gone. Each kind of page has its own timeline sentence, and each stop has a title (what to do) and a second sentence that ends or starts with "Nothing was sent." The table of record is `core/site_signin.py: STOPS` and `WALLS`; `{site}` is the page's host.

| Page or result | Timeline sentence | Status block title | Control |
|---|---|---|---|
| sign-in page, switch off | "{site} asked you to sign in." | "Sign in on the job site first." Says the switch is off under Settings. | Retry with the app |
| sign-in page, switch on | "{site} asks you to sign in. Waiting for you to allow 1Password." | "{site} asks you to sign in." | Allow 1Password, and a quiet "Do not sign in this time" |
| verification code | "{site} asked for a verification code. Waiting for you to type it." | "Verification code" with the reason under it | code box and Use this code; "Use the code from 1Password" when the saved login has one; "Send a new code" when the page has a resend control |
| refused code | "The last code was not accepted. Asking again (2 of 3)." | same box, the reason says which try this is | same |
| verify-it's-you page with no code box | "{site} asked you to verify it is you, with no code box to fill." | "The job site asked you to verify it is you." | I applied on the site (existing) |
| block page | "{site} blocked the app from opening the page." | "The job site blocked this browser." | none |
| no saved login | "No saved login for {site} was found in 1Password." | "Save a login for this site in 1Password, then retry." Says the app never creates accounts. | Retry with the app |
| several saved logins | "1Password has more than one saved login for {site}." | "Keep one saved login for this site, then retry." | Retry with the app |
| passkey only / sign up only / captcha at sign-in / not a secure page | one sentence each | "Sign in on the job site yourself." or "Create the account on the job site yourself." | none from the app |
| no clear result | "{site} did not finish signing in before the wait ended." | "Retry with the app." | Retry with the app |
| signed in | "Signed in to {site}. The session is saved for next time." | the run continues | none |

Approval rules, as the owner sees them:

- The app asks before 1Password does. The status block names the site and says a 1Password window will ask for Touch ID or the account password on the computer where this dashboard runs, and that the app never sees that password. Only the button opens that window.
- While the request waits for 1Password the button reads "Waiting for 1Password".
- Approval may be needed again: after 1Password locks, for the one-time code (its own button), on a second site, on a later run. The page asks each time with the same button.
- A missed prompt: "1Password was not approved in time. Nothing failed. Press Allow 1Password again when you are ready, and watch for the 1Password window." The run keeps waiting until the hold time runs out or six prompts were missed; then the stop reads "1Password was not approved in time." with Retry with the app.
- Approved but unusable: "1Password was approved, but it has no saved login for this site." · "1Password has more than one saved login for this site, so none was used." · "1Password is not signed in on this computer." · "The 1Password app did not answer. Turn on Integrate with 1Password CLI in its Developer settings, then retry." · "This dashboard cannot open 1Password. Open this page from the dashboard on the computer where 1Password is installed, then press Allow there."

A refused password (owner, 2026-11-12). The app never sends it again by itself.

| State | Status block | Control |
|---|---|---|
| refused, the same attempt is still holding | "{site} did not accept the saved login." "Nothing was sent. Change the password on the job site and save it in the same 1Password item. Then press the button, and 1Password asks you to approve again." Under it, muted: "The job site said" and the site's own words; "Reset the password on {site}" (new tab; "The app never changes a password."); "Open the sign-in page for this job" ("The same address the app opened."); "Save the new password in the same 1Password item, named "…", so the app can read it." | I changed the password, try again; quiet Not now |
| reset link missing, not https, or on another site | the link line reads "The reset link could not be found on the sign-in page. Use the site's own forgot password option." | same |
| pressed, but the saved login has not changed | "The saved login has not changed." "{site} already refused this password, and the login saved in 1Password is still the same. Sending it again can lock the account. Change the password first. Nothing was sent." | I changed the password, try again; secondary "Send the same login once more" |
| the hold ran out | "{site} did not accept the saved login." "Nothing was sent. The app will not send that password again by itself. Change it on the job site, save it in the same 1Password item, then press the button." Links stay. | I changed the password, try again (starts a new attempt, which asks to allow 1Password) |
| three refusals in 24 hours | "{site} refused the saved login 3 times, so the app stopped for now." "Nothing was sent. More tries could lock the account. Another try is offered after <time>. The links stay here." | none until then |
| locked or throttled | "{site} says the account is locked or has had too many attempts." "Nothing was sent. The app waits until <time>, as the site said, before it offers another try. You can unlock the account on the site." ("because the site did not say how long" when it named none; the wait is then 30 minutes.) | none until then, then "The account is unlocked, try again" |

The site's words, the item title and both links are untrusted. They are shown as escaped text; a link is shown only when it is https and, for the reset link, on the same registrable domain as the sign-in page or the run's site. Links open in a new tab with `rel="noopener noreferrer"`.

## What the app saw when it stopped (2026-11-12)

A card under the status block whenever a stop record exists for the application. Title "What the app saw when it stopped", with the capture time.

- Notice, always first: "This record is complete. It can include passwords, verification codes and session cookies. It is kept in private storage on the computer that ran the attempt, and it is deleted when a new attempt starts."
- The screen as a picture; a click opens the whole page full size in a new tab. Beside it: "Page address", "Page title", "Requests" ("42 in all · 1 failed · 2 refused by the app · 1 error replies"), "Console" ("7 lines · 1 page errors").
- Downloads, each with its size: "Page source", "Request list", "Console and page errors", "Network archive (HAR, zipped)", "Playwright trace", "Record summary".
- Limits, stated on the card: "A text body over 512.0 KB is cut in the request list and whole in the network archive. A picture or other binary body is listed by size and type and kept in the archive. An archive or trace over 200.0 MB is not kept."
- "Requests the page made", closed until opened, with the failed and refused counts in its heading. Inside: "Failed or refused" first, then "Every request, in order", then "Console and page errors". A row opens to request headers, cookies sent, form fields sent, request body, response headers, cookies set, response body. A body shows its first 4 KB with "Showing the first 4.0 KB of 700.0 KB. The whole body is in the network archive." Without scripts the same list opens on its own page.
- "Pictures taken during this attempt": the Needs you, Stopped early, Form complaint and Human check pictures. They are no longer under Proof of applying, which keeps the filled review page, the confirmation, an unclear result and a confirmation for another job.
- Three other states: no record ("No record was kept for this stop. The next time an attempt stops, the page, its requests and its console are saved here."), files on another computer ("The files of this record are on the computer that ran the attempt. Open this page on that dashboard to see the picture, the requests and the downloads." and "The picture is on the computer that ran this attempt, not on this one."), and an empty list ("No requests were recorded. The page stopped before it asked for anything, or the recording could not start.").
- A part that failed: "Parts that could not be recorded: network: ValueError. The attempt stopped for the reason shown above, not because of this."
- Timeline line: "Saved what the page showed and sent when this attempt stopped."

Everything in the record came from a web page. Addresses, titles, headers and bodies are shown as escaped text and are never links. Files are downloads; the saved page source is never opened as a page.

## The application page as one flow (2026-11-12)

One status block at the top carries everything the page asks for. It holds, in this order: the step tracker, the
status label with the attempt number and start time, what is happening, the one ask, and the one control.

Step tracker (`web/app_flow.py: flow_steps`). Steps: Prepared, Approved, Opening the job site, Signing in, Filling the
form, Verification code, Sending, Confirmed. Signing in appears only when the site asked for it, Verification code
only when a code was asked for. Exactly one step is marked: "now" (the app is working), "waiting on you", or
"stopped here". On a narrow screen finished and future steps are dots; the marked step keeps its words.

Rules:
- A stopped application never reads "Nothing is needed from you right now." It says what stopped and what to do.
- "I applied on the site" marks an application as sent, so it is never the main button for one that was not sent.
  It sits under More in the header, with a tick box "I sent this application on the job site myself"; a request
  without the tick records nothing. Only after the final click, when no confirmation was seen, is recording it the
  main step, labelled "Record the confirmation".
- Start over and Remove unused resumes sit under the same More, each closed until opened.
- The label of a `handoff` reads "Stopped before sending" on the application and job pages while the app can still
  act (retry, a wait that has to pass). It reads "Finish on the site yourself" only when the main button opens the
  job site. The list pages keep the status label.
- A wait in the block (1Password, a code, a human check) shows its time limit: "The app waits for you until HH:MM.
  After that this attempt stops with nothing sent, and you can start it again here."
- A new attempt shows "Attempt N · started YYYY-MM-DD HH:MM" beside the status, so its lines are not read as the
  last attempt's stop. Earlier attempts are one line each in the fold "Earlier attempts"; the full stop record is
  kept only for the latest stop.
- While the tab is in the background and the page waits on the person, the tab title starts with "Needs you · ".
  "Tell me when this needs me" (in "What is happening") asks the browser for a notice; it shows only where the
  browser allows notices (a secure address such as 127.0.0.1).
- Before approval the header says what is known about signing in where the form lives: "The form for this job is on
  <site>, not on the site where the job is posted.", "The app last signed in to <site> on <time>. It tries that
  saved session first.", "<site> has asked the app to sign in before, and no sign-in there has worked yet.", then
  "If the site asks to sign in, this page asks you to allow 1Password first." or "Signing in with saved logins is off
  under Settings, so a sign-in page stops the application for you." Nothing is said when nothing is known.
  When an earlier application stopped on the same site for want of an account, and no sign-in there has worked
  since, the note is amber and says which: "Last time <site> only offered sign up. The app does not create accounts.
  Create one on the job site yourself and save its login in 1Password first." or "Last time <site> asked to sign in
  and no saved login for it was found in 1Password. Save one there first, or the application stops at the sign-in
  page."
- Settings lists the job sites the app has signed in on: site, last signed in, last try.

Section order: status block, what the app saw when it stopped, the job header, Still needed, What would be sent,
Your approval, What is happening, Proof of applying, Things the job asks for that you do not have, Earlier attempts,
What happened last time (technical details).

State → action table (`web/app_flow.py: STATES`; `tests/test_app_flow.py` walks every row and fails when a row here
is missing). "none" means the page shows no button because the app is working or a wait has to pass.

| State | Button | What the block says |
| --- | --- | --- |
| A resume is not selected yet | Select a resume | A resume still needs to be selected. |
| One answer is missing | Save this answer | Notice period is still needed. |
| Several answers are missing | Answer the questions | Some answers are still needed. |
| Being prepared | none | (the app is working or waiting; no ask) |
| Ready to read and approve | Approve this exact application | Read the resume and the answers, then approve this exact application. |
| Answers changed after approval | Approve this exact application | Read the resume and the answers, then approve this exact application. |
| The approval ended or was used up | Approve this exact application | This application needs your approval again. |
| Approved, different answers on the page | Fill in the form | Fill in the form with the answers on this page. |
| Approved, nothing running | Apply with the app | This application is approved. |
| Queued or opening the job site | none | (the app is working or waiting; no ask) |
| Filling the form | Cancel this attempt | (the app is working or waiting; no ask) |
| Sending, after the final click | none | (the app is working or waiting; no ask) |
| Sign-in page, waiting for 1Password | Allow 1Password | <site> asks you to sign in. |
| 1Password prompt was missed | Allow 1Password | <site> asks you to sign in. |
| Saved password refused, attempt still open | I changed the password, try again | <site> did not accept the saved login. |
| Saved login unchanged since the refusal | I changed the password, try again | The saved login has not changed. |
| Verification code while signing in | Use this code | Verification code |
| The form asks for one more answer | Use this answer | Start date |
| Human check on the page | Continue | Human check needed |
| A later try is scheduled after a human check | none | (the app is working or waiting; no ask) |
| Stopped: sign-in page, saved logins off | Retry with the app | Sign in on the job site first. |
| Stopped: no saved login for the site | Retry with the app | Save a login for this site in 1Password, then retry. |
| Stopped: 1Password not approved in time | Retry with the app | Retry when you are at your computer. |
| Stopped: saved password refused | I changed the password, try again | <site> did not accept the saved login. |
| Stopped: account locked, wait running | none | <site> says the account is locked or has had too many attempts. |
| Stopped: refused too many times today | none | <site> refused the saved login 3 times, so the app stopped for now. |
| Stopped: verification code not typed or refused | Retry with the app | Retry when you can read the code. |
| Stopped: verify it is you, no code box | Open the job site | The job site asked you to verify it is you. |
| Stopped: the site blocked the browser | Open the job site | The job site blocked this browser. |
| Stopped: human check was not finished | Retry with the app | The site asked for a human check last time. |
| Stopped: a step the app cannot do | Open the job site | Finish the remaining step on the job site. |
| Stopped before sending, same approval still covers it | Retry with the app | The app can try again. |
| Stopped on a site the app cannot fill | Open the job site | Apply on the job site yourself. |
| Failed before anything was sent | Retry with the app | The app can try again. |
| Failed on a site the app cannot fill | Open the job site | This attempt stopped. |
| Stopped after the final click, no confirmation seen | Record the confirmation | The form was sent, but the app saw no confirmation. |
| Unclear after the final click | Save the result | Check the job site, then record whether it went through. |
| Sent and confirmed | none | (the app is working or waiting; no ask) |

## Rules applied everywhere

- Short everyday sentences, one idea per sentence. Verbs on buttons ("Check fit", "Read and approve", "I applied on
  the site", "Withdraw"). No hyphens in glossary titles.
- Flash messages and confirmation dialogs say what happened and what it means ("The resume was rebuilt. The files
  changed, so any earlier approval for this job was withdrawn.").
- Start over (`POST /applications/{id}/start-over` and `POST /jobs/{id}/start-over`) is one button. The sentence
  beside it says the unsent attempt, its approval, and the resume files made for it are cleared, the job stays,
  nothing is sent, and the tracker history is left as it is. It is refused when the application was sent or an
  employer confirmation is already saved; the button is hidden and that page says so in one sentence. The job
  stays. A GitHub tracker row is marked withdrawn instead of being erased. After it succeeds the person lands on
  the job, ready to prepare it again. Rebuilding the resume is separate: a new resume file is made, the old
  approval no longer applies, and nothing is sent. The base resume stays.
- Remove (`POST /applications/{id}/remove`, a quiet button in each row of Prepared applications) is the same
  clearing as Start over from the list. Its pop says it takes the prepared application off the list with its approval
  and resume files, nothing is sent, the job stays, and the tracker history is left as it is. The person lands back on
  the list with the `app_removed` flash. Rows where something was sent or a receipt is saved show no Remove.
- Cancel this attempt (`POST /applications/{id}/cancel`) appears in the status block only while the form is being
  filled and before the final click's step line ("Sending the application") is saved. Flashes: "Cancel received. The
  app stops before the final click and closes the page. Nothing is sent, and you can try again later." ·
  "Nothing is filling in this form right now, so there was nothing to stop." · "The final click already happened, so
  this attempt cannot be stopped. The app is checking the result. Record what the site shows if it stays unclear."
  The stopped attempt reads "Cancelled from the dashboard. Nothing was sent" and offers Retry with the app.
- "Check which jobs are still posted" (`POST /jobs/check-postings`) re-reads open jobs one at a time, up to 25, and
  skips a job checked in the last 6 hours. "Check if this posting is still up" (`POST /jobs/{id}/check-posting`)
  checks that one job even if it was checked recently. A missing page (403 or 404), a page that says the job does
  not exist, or a page too short to be a posting sets the job to Closed with "This posting is no longer up" and
  takes it off the open list. The record stays. A sent application is never closed. The same check writes one
  update line. Repeated identical update lines stay stored and show as one line with a count.
- "Remove unused resumes" (`POST /jobs/remove-unused-resumes` and `POST /applications/{id}/remove-resumes`) asks
  for the word `remove`. It deletes tailored files for applications that were never sent, and withdraws an approval
  that pointed at a deleted file. It runs on its own when a posting is closed or an unsent attempt is cleared. The base
  resume, a sent application, and GitHub tracker history are not deleted.
- DOCX and PDF stay as they are. Site names, file paths and IDs stay in `<code>`.
- Each stepper step has a one line `title` tooltip from `STEP_HELP`; `job_steps()` compares on the technical key, so a
  renamed label never changes behaviour.

## Jobs page: several links at once, and checks at set times (2026-11-13)

Appended block. The sentences live in `web/jobs_paste.py` (`banner`) and `web/jobs_refresh.py` (`automatic`).

- Add a job. Label "Links to job ads", help "One link per line, or separated by spaces or commas. A link you already added is not added twice." The other fields carry "For one link only: fill in what you know, and paste the text of the ad if the site blocks the app." Button "Add and check fit", "Adding…" while it posts.
- One clean link opens that job, as before. Anything else returns to the list with one banner, built from these sentences:
  - "3 jobs were added and checked against your rules. They are marked New in the list." (one: "1 job was added … It is marked New in the list.")
  - "2 were already in your list." Alone: "That job was already in your list. Nothing was added." or "All 4 jobs were already in your list. Nothing was added."
  - "1 link could not be read: files.example/role.pdf. A link starts with https://." Alone: "That link could not be read: …" or "None of the 5 links could be read: a, b, c and 2 more. … Nothing was added." Names are cut to 48 characters; three are shown.
  - Nothing pasted: "No link was found in what you pasted. Paste one or more links that start with https://. Nothing was added."
  - Over the limit: "Only the first 50 links were read. 12 more were left out, so paste those again." Too much text: "That was too much text to read as links. Paste up to 50 links at a time. Nothing was added."
  - Several links with the one-link fields filled: "The employer, title, job ID, location and ad text were not used, because they fit one link only."
- Tone: green when everything pasted was added or already known, amber when some were added or known and something needs a look, red when nothing could be used.
- New mark: the jobs the latest paste added carry the same "New" mark as jobs a check found. It goes when the next check for new jobs ends, or after a day. A job known only by its link shows the link, shortened, after "Unknown title".
- The check strip, second line, only when times of day are set: "Next automatic check today at 16:30. The last one ran today at 08:00." Before the first one: "… None has run yet." After one that stopped: "… The last one, yesterday at 16:32, stopped before it finished." With background tasks off: "Automatic checks are on hold because background tasks are not running." Days read today, tomorrow, yesterday, else "on Nov 9". No times set: the line is absent.
- Activity entry: "Job links pasted".
