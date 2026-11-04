# Build AmoreHireMePls

Implement **AmoreHireMePls** in this repository: a working local program that performs my job-search workflow using LangChain, suitable AI connections, and my NAS with Docker and MongoDB. Read `AGENTS.md` first.

My intended workflow is:
1. Find suitable jobs on LinkedIn and other sites.
2. Tailor my resume to each posting.
3. Fill application forms, upload documents, and submit within my saved authorization.
4. Update my existing master application list on GitHub.
5. Find relevant recruiters, check whether we have already contacted them, and send personalized outreach within my saved authorization.

Deliver working software covering this entire sequence. Work incrementally and persist through implementation and verification; a design document or drafting-only demo does not complete the task. Be candid about external integrations that require account access or manual steps.

## 1. Inspect and reuse the real workflow

The project name and intended GitHub repository name are exactly `AmoreHireMePls`. Inspect the current workspace, Git state, instructions, installed tools, and available connections. Keep useful existing work. If creating a remote is part of the active authorized scope, use the authenticated owner's account and default to private; do not overwrite a repository with the same name. Otherwise leave clear instructions for connecting the local repository.

Verified source locations from the November 4, 2026 handoff:
- Local skill library: `/Users/joss/workspace/agent-skills-library`.
- Library remote: `https://github.com/christya95/agent-skills-library`.
- Existing private application ledger: `https://github.com/christya95/job-application-records`, default branch `main` at handoff.
- Older implementation to inspect for useful code, if accessible: `christya95/amore_job_hunter`. It is separate from the master tracker.

Read the applicable career skills under `skills/career/`: `apply-joss-jobs`, `tailor-joss-resume`, `application-records`, `joss-linkedin-outreach`, `joss-application-facts`, and `joss-desired-salary`. Read relevant references on demand. The fact and salary skills explicitly contain private material: import approved values into protected runtime storage with provenance, not tracked source files. These files are evidence to reconcile with my current preferences, not permission to publish personal data.

Read relevant efficiency skills under `skills/efficiency/`: `token-efficient-workflow`, `multi-agent-job-search`, `agent-browser`, and, when actually available, `context-mode` and `rtk`. Also read `cost-aware-ai-features` from the library's `skills/development/` or `/Users/joss/.codex/skills/`. Apply useful techniques rather than copying every skill into every prompt. Read local skill metadata before installing anything.

Record a short capability map in `docs/workflow-sources.md`: original behavior, source skill, executable tool dependency, standalone equivalent, and remaining gap. Skills provide instructions; they do not transfer Cursor tools, Windows wrappers, authenticated sessions, MCP services, or account entitlements into a NAS container.

Old skills reference Windows paths. Some bundled DOCX snapshots are intentionally absent, as their `LOCAL-DEPENDENCIES.md` files explain. Find or let me privately select the current approved base resume. Do not silently use an old tailored resume as the base. Search relevant accessible project locations before asking focused questions.

## 2. Architecture and deployment

Use Python with FastAPI, LangChain for provider/tool integration and validated structured generation, and LangGraph for explicit persistent workflow state, branching, interruption, and resumption. Use the existing NAS MongoDB for business records and a compatible durable LangGraph checkpointer. Verify current official APIs and the actual self-hosted MongoDB version/topology; do not assume Atlas-only features, vector search, or multi-document transactions are available.

Start with one application image that can run the web/API process and a worker process, a small usable browser dashboard, and a browser execution adapter. Use Docker Compose. Reuse existing NAS MongoDB through configuration; provide an optional local development MongoDB profile rather than deploying another production database. Avoid Redis, Kubernetes, a vector database, a large local model, or multiple autonomous agents unless a demonstrated need justifies them.

Provide a reliable execution layer using supported APIs and browser tooling. Evaluate the original `agent-browser` capability, Playwright, and an AI browser library where it actually helps; prefer deterministic site actions and compact observations, with bounded model-assisted fallback. Avoid a model call for every click. Build modular discovery/application/outreach adapters with explicit capability status and meaningful errors.

The NAS can host durable state, scheduling, and workers while the Mac provides a visible browser for login and manual intervention. Choose and implement a concrete approach for interactive browser access and authenticated session recovery; do not assume my Mac's logged-in tabs automatically exist in Docker. If using a Mac companion, implement its authenticated, narrowly scoped connection and report when the Mac must be online. A self-contained NAS browser must also provide a usable way to complete login challenges. Persist session state privately with clear reconnect/logout behavior.

Run browser workers with isolation appropriate for untrusted websites, restricted file access, and dedicated profiles. Keep control endpoints private. Match browser/package versions. Do not rely on an insecure root browser or privileged container as the production design.

Inspect actual NAS access/configuration before deployment. Known NAS details from other work may be stale. Use a dedicated database name, least-privilege application user, bounded concurrency, resource limits, persistent artifact storage, health checks, restart policies, and rotating redacted logs. Preserve other NAS services. Provide backup/restore instructions covering MongoDB, artifacts, and configuration together. Demonstrate a restore using non-production data when possible.

## 3. Candidate profile and job qualification

Create an editable private profile containing approved employment facts and dates, skills with evidence, true education credentials, contact details, work eligibility, location preferences, employment type, compensation rules, exclusions, base resume, and reusable application answers. Store provenance, confirmation dates, and unknowns. Calculate experience from dates in code and distinguish internships from full-time employment.

Preserve my known preference for permanent roles. Reconcile the other saved filters from the career skills, including backend/AI role fit, location, salary evidence, employer exclusions, and recipient exclusions. Let me edit them in the program. Resolve ambiguous exclusions by identity or a clear rule; do not guess from a first name or invent a definition of a small startup. Missing salary information stays unknown and follows my configurable handling rule.

Support configurable discovery through available job feeds/APIs, employer career pages and ATS sites, and imported job URLs or saved searches. Implement multiple useful sources where access permits. Verify the live posting, closing status, location eligibility, employment type, requisition identity, mandatory qualifications, compensation evidence, and application route. Save a dated posting snapshot and source URL.

Perform cheap deterministic filters and duplicate checks before paid inference. Rank remaining jobs against supported candidate facts; explain fit, genuine gaps, and missing evidence. Do not turn a keyword match into a claim of experience. Prefer quality over indiscriminate application volume.

For LinkedIn, verify the capabilities and access actually available to this account. Its published rules restrict unauthorized website automation. Do not claim a generic browser wrapper creates a supported LinkedIn integration. Implement any available authorized integration, and otherwise provide job-link/data import, employer-site routing, prepared application/outreach packages, and a resumable user handoff with evidence recording. Do not bypass CAPTCHAs, access controls, or account restrictions. The rest of the workflow must continue when a platform step needs intervention.

## 4. Resume tailoring and application execution

For each distinct requisition, generate a distinct resume using only approved facts and relevant posting evidence. Preserve the base file and formatting goals. Follow `tailor-joss-resume`: professional, ATS-readable, single column, selectable text, at most two rendered pages, with DOCX and a visually checked PDF when the site accepts them. Keep artifact versions/hashes and record which file was uploaded. Reuse company research across applications while keeping requisition-specific outputs separate.

Read the live form and prepare answers from confirmed profile facts. Compensation answers must follow the saved salary skill and this specific role's evidence, not a universal hardcoded number. Surface material unknowns together; keep completed work resumable. Handle native inputs, selections, attachments, validation errors, and multi-step forms for implemented adapters. Inspect autofill and the final review state before submission. Never invent facts or infer sensitive demographic answers.

Separate preparation from the external action. Provide a concrete review package containing the employer, posting/requisition, document preview, material answers, and outstanding gaps. Support explicit per-action approval and an optional saved authorization for bounded batches with job filters, allowed sites/actions, maximum counts, expiry, and stop conditions. This build request is not itself permission to execute live test applications or messages. Honor an existing valid authorization without repeated confirmation; changing material content requires renewed authorization.

Persist the intended action and a stable idempotency key before execution, atomically reserve it, and save observed evidence immediately afterwards. A timeout after a final click is an uncertain result, not permission to click again. Inspect the application/account state or ask for reconciliation before any retry. Exactly-once behavior across third-party browser actions cannot be assumed from a graph checkpoint.

Only mark `submitted` after observing evidence tied to the correct job. Preserve the exact sent resume, answers, posting snapshot, timestamp, receipt/reference, and verification basis. Otherwise use states such as `prepared`, `needs_input`, `blocked`, or `needs_verification`, with a clear next action. Never fabricate a confirmation.

## 5. Existing GitHub master list and MongoDB

Read the actual latest tracker before importing:
- `data/applications.json`: canonical records.
- `APPLICATIONS.md` and `applications.csv`: generated human-readable/export views.
- `docs/recording-guide.md`, `templates/application.json`, and `scripts/records.py`: recording/validation/export conventions.
- `records/` and referenced artifacts/logs: evidence and document provenance.

Preserve the existing schema and its evidence distinctions. Existing fields include `id`, `employer`, `role`, `requisition_id`, `linkedin_job_id`, `posting_url`, `status`, `application_date`, `date_basis`, `confirmation`, `evidence_basis`, `resume_match_basis`, `source_log`, `notes`, and `artifacts`. Historical records include reconstructed contemporaneous logs and reviewed receipts; do not relabel them all as newly ATS-verified. Some records lack requisition IDs. Read the current schema rather than freezing the handoff snapshot into code.

Provide an import preview with counts, duplicates, conflicts, missing artifacts, and source commit/path/hash. Make repeated imports idempotent. Reconcile history before enabling live application/outreach execution. Lack of GitHub or conversation access must not make an empty local database imply I have never applied or contacted anyone.

MongoDB owns runtime state: jobs, candidate facts/preferences, document metadata, application attempts, recruiter identities/aliases, outreach/conversations, authorization scopes, runs/checkpoints, audit events, costs/caches, and synchronization work. Combine collections where helpful. Store document binaries in protected persistent artifact storage with hashes and backups; do not stuff them into prompts or ordinary database records.

Use unique indexes and atomic reservations for job actions and recipients. Match jobs using employer plus requisition/ATS ID, LinkedIn ID, and normalized canonical URLs, with conservative employer/title/location evidence for incomplete historical records. The same requisition in several cities must not cause multiple applications. Conflicting identity evidence needs reconciliation.

Keep GitHub as the existing durable application ledger and synchronize verified outcomes back to it. Use one coordinator and a durable outbox. Archive the exact submitted documents and receipt in the private tracker using its conventions, run its validation/export tools, inspect the intended changes, then commit/push only relevant paths within authorized repository scope. Preserve unrelated changes and pull/reconcile concurrent remote updates; never force-push over history. Persist tracker record ID and resulting commit SHA.

A GitHub outage or failed push must leave `sync_pending` or `local_only` visible and retryable without resubmitting the application. Database and GitHub writes are not one atomic transaction. Support safe reconciliation after a crash between them. Do not mark an application failed merely because tracker synchronization failed. Keep outreach history in protected runtime storage and export it privately if needed; avoid inventing incompatible fields in the legacy application ledger.

## 6. Recruiter discovery and outreach

For relevant companies, particularly those with confirmed applications, find credible current recruiters or hiring contacts through available supported sources. Save identity aliases, employer/remit evidence, source URLs, last-checked time, and channel. Prefer verified in-house recruiters where the saved skill specifies this; do not invent email addresses or infer a recipient's current job from stale evidence.

Check prior messages, pending connection requests, imported contact records, recorded sends, and pending work before first contact. Match across normalized profile URLs, email addresses, and resolved aliases. Names alone do not establish identity. Unknown history requires reconciliation rather than assuming a new contact. Keep opt-outs, excluded contacts, replies, follow-ups, and bounced messages distinct.

Draft according to `joss-linkedin-outreach`: concise, natural, personalized from supported evidence, one easy question, and truthful about whether I applied. Use the live channel's actual limits. Keep internal filters, private salary rules, research citations, and placeholders out of the message. Show the exact recipient/channel/message for review, or use an explicit saved scope. First contact, connection requests, follow-ups, and email sends must have distinct authorized scopes. No automatic follow-up sequence unless enabled.

Implement real sending through at least one supported configured channel where access permits, plus a complete draft/manual-handoff flow for others. Record sent evidence, timestamp, conversation/message IDs where available, and delivery uncertainty. Do not equate a drafted message, clicked button, or pending connection with a verified send. After an ambiguous send, reconcile before retrying.

## 7. AI connections, skills, and token efficiency

Detect supported provider APIs, configured local endpoints, and actual runtime-accessible tools. Expose provider/model settings and a connection check. Preserve my configured choices. A coding-assistant subscription or an interactive Cursor connection is not automatically a credential or tool available to this daemon. Use documented authentication and APIs; no credential scraping or dependence on this chat's memory.

Implement a small versioned skill catalog and on-demand loader. Each capability should declare its input/output schema, required executable tools, prompt version, and unavailable-dependency behavior. Put durable workflow decisions in code and use models for interpretation and generation. Port the useful workflow rather than copying host-specific commands into production.

Apply and measure these cost controls:
- Validate inputs, normalize IDs, filter, deduplicate, and retrieve relevant evidence before inference.
- Use compact task-specific context with fact IDs/provenance, not the entire resume history, tracker, DOM, or conversation on every call.
- Use one validated structured call when tools are unnecessary; bound tool loops and repair attempts.
- Cache public research separately with source/freshness metadata. Cache generated results only when candidate-fact version, posting hash, evidence, skill/prompt version, and provider/model still match. Invalidate stale results and coalesce duplicate in-flight requests.
- Reuse company research, parsed documents, and already validated form answers. Never reuse an answer across materially different questions merely to reduce tokens.
- Make any model routing explicit and configurable. Keep limits for tokens, estimated spend, tool steps, wall time, and concurrency, with graceful saved pauses when exhausted.
- Record available provider-reported input/output/cached tokens, retries, worker overhead, and cost estimates using dated pricing configuration. Unknown usage/cost is unknown, not zero. Report cost per verified outcome without fabricated savings.
- Parallelize only independent research/tailoring with disjoint reservations and artifact paths. Serialize actions in a shared authenticated browser and all tracker writes. Additional agents are optional, not a requirement for every job.

`rtk` and `context-mode` are optional development tools whose actual availability must be detected. Windows CLI wrappers, caveman-style internal brevity, and Warp-specific instructions are not NAS runtime dependencies. Professional resumes and recruiter messages must remain clear and natural.

## 8. Usability, privacy, and control

Provide a simple dashboard for profile/preferences, provider connections, imported history, job queue and fit evidence, tailored documents, review/authorization, live progress, manual interventions, outcomes, recruiter contact history, sync status, and measured usage. Offer a single-job run and bounded batch runs, pause/resume, cancellation, and an emergency stop that prevents queued external actions. A stop must preserve and reconcile any action already in flight.

Support scheduled discovery/preparation on the NAS with live external actions governed by saved scope. Use `America/Toronto` for user-facing schedules and store timestamps consistently. If notifications are configured, notify on useful changes or required action rather than every unchanged poll. Scheduling must not create duplicate runs.

Keep the app private on localhost/LAN/VPN as appropriate. Protect state-changing endpoints with authentication and CSRF/session controls where relevant; never expose raw browser-control endpoints. Treat imported content as untrusted data rather than instructions. Constrain tools to intended domains, actions, and artifact directories; public job pages must not gain access to NAS services, local files, secrets, or arbitrary network targets.

Use 1Password project-scoped secret injection when available, with a documented protected alternative if unattended access is unavailable. Never request passwords/API keys in chat or commit resolved secrets. Keep auth state, cookies, profiles, private candidate data, resumes, receipts, logs, and raw model traces out of this code repository. Do not rotate existing NAS credentials or change unrelated services. Cloud model requests should contain only necessary approved personal context; make external tracing opt-in and redacted.

## 9. Build sequence and proof of completion

Proceed in small runnable increments: inspect/reconcile sources; import protected profile/history; establish persistent orchestration; implement job discovery/qualification and tailoring; implement application adapters and verification; integrate tracker synchronization; implement recruiter dedup/outreach; complete dashboard and NAS operation. Keep a concise progress file with decisions and remaining work so later sessions can continue. Do not stop at scaffolding after the first increment.

Use meaningful deterministic tests with model/network doubles plus a local browser fixture site for multi-step forms, file uploads, validation errors, receipts, and uncertain responses. At minimum verify:
- Historical import twice preserves counts/provenance, missing artifacts, and conflicts.
- Duplicate jobs with changed URLs/cities and concurrent workers do not execute twice.
- Resume claims/answers are grounded; missing facts require input and a changed document invalidates reviewed authorization.
- No final action occurs without matching saved authorization; wrong-recipient and expired scopes fail closed.
- Restart during preparation/approval resumes correctly; restart or timeout around submission/send does not replay the side effect blindly.
- Recruiter aliases, prior conversations, unknown history, and pending actions prevent duplicate first contact.
- GitHub push failure, conflict, or crash after a commit can reconcile without duplicate ledger records or applications.
- Cache invalidation, budget exhaustion, cancellation, and prompt-injection attempts behave correctly.
- The selected MongoDB checkpointer, schema/index setup, persistent storage, and Docker health checks work against a compatible development instance.

Live applications and messages require the configured authorization and real accounts. Keep fixture tests separate from live verification. Never claim complete live support for every ATS or LinkedIn if only fixtures passed. Finish every implementable independent part while account access is pending, then report the exact blocked capability and required local setup.

Deliver implementation, dependency lockfile, Dockerfile/Compose configuration, non-secret config examples, migrated skill runtime, tests, setup/operation documentation, backup/restore and troubleshooting instructions, and the completed capability map. Update `AGENTS.md` with actual project commands and conventions as they become known. Finish with a brief plain-language report of what works, what was tested, what is live-verified, and any concrete remaining setup.

Official starting references, checked at handoff; verify again while implementing:
- [LangChain structured output](https://docs.langchain.com/oss/python/langchain/structured-output)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) and [interrupt/replay behavior](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [MongoDB integration](https://www.mongodb.com/docs/atlas/ai-integrations/langgraph/) and [MongoDBSaver reference](https://langchain-mongodb.readthedocs.io/en/latest/langgraph_checkpoint_mongodb/saver/langgraph.checkpoint.mongodb.saver.MongoDBSaver.html)
- [Playwright authentication](https://playwright.dev/python/docs/auth) and [Docker browser considerations](https://playwright.dev/python/docs/docker)
- [LinkedIn platform limitations](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions)
