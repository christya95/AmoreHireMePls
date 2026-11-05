# Architecture

The installable package is still `amorehiremepls`. Imports are absolute (`from amorehiremepls.jobs.discovery import Discovery`). The command is `amore` (`amorehiremepls.web.cli:app`).

## Packages

| Package | What lives here |
| --- | --- |
| `core` | Configuration, MongoDB, logging, ids and time, authorization, the action store, the model runner and skill catalog, LangGraph orchestration, process wiring, the worker, notifications, the human-check hold, backups. |
| `candidate` | Profile facts and preferences, `profile.toml` load and export, extra documents dropped in `candidate/`. |
| `jobs` | Job records and deduplication, discovery and fit, cached employer logos. |
| `apply` | Tailoring, form answers, the browser, captcha, and `apply/ats/` (Greenhouse, Lever, Ashby). |
| `records` | The tracker clone and the sync outbox. One writer. |
| `outreach` | Recipient identity and drafted messages. Sending stays behind an authorization scope. |
| `web` | FastAPI dashboard, Jinja templates, local CSS and JS, UI copy, and the CLI. `web/chrome.py` holds templates and page helpers. `web/routes/` is one module per area of the site. `create_app` owns the session, CSRF and the render closure, then registers those modules. |

A new site adapter goes in `apply/ats/`, is registered from `apply/ats/__init__.py`, and returns a browser plan. Extend `apply/browser.py` for a new kind of step. Do not click a specific site's layout from a route.

LangGraph checkpoints on the NAS were checked before the move (2026-11-05): `lg_checkpoints` (11 documents) and `lg_checkpoint_writes` (35 documents) store no `amorehiremepls.*` module path, so the package move does not invalidate a saved run.

## Docstrings

A package and a module start with one or two sentences that say what it is for. A function gets a docstring when the name does not already state the rule it enforces (a limit, a value that must stay blank, an idempotency or receipt check). Do not restate the code.

The queue asked for 22 high-value comments. No list of those 22 exists in this repository, in `CURSOR_PROMPT.md`, or in `docs/PROGRESS.md`. No comments were added to fill a count. Comments already in the code, including the form rules, the sensitive-question blanks and the receipt checks, stay as they are.
