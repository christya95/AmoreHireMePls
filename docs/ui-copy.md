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
| one answer missing | "[Label] is still needed. Type it here and save. The application is then ready for your approval." | one text box or list, Save this answer |
| several answers missing | "Some answers are still needed." | link to Answers that will be sent |
| no resume | "A resume still needs to be selected." | Select a resume (profile page) |
| a field is waiting on the open form | the field's label · "The same page is still open. Type the answer here and it is entered on that form. The form is not started over." | text box, Use this answer |
| human check | "The page is being held on this computer at 127.0.0.1:[port]. Finish the check there. Continue uses the same approval and does not send the form again." | Continue |
| answers changed since the last fill | "Fill in the form with the answers on this page. The job site is opened with these answers. Nothing else changes." | Fill in the form |
| failed or human check, retry possible | "The site asked for a human check last time." or "The app can try again. It reads the form again and prepares the answers afresh. If your approval still covers these exact answers and files, it applies right away; if anything changed, you are asked again before anything is sent." | Retry with the app |
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
