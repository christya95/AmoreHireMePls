# Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `/health` returns 503 | MongoDB unreachable or auth failed | Check `AMORE_MONGODB_URI`/user; `uv run amore health`. The app never falls back to another database. |
| Overview shows "no recent heartbeat" | worker not running or crashed | `docker compose logs worker`; `amore worker --once` prints the tick. Heartbeat is `controls.worker_heartbeat`. |
| Login refused with the right password | `AMORE_APP_PASSWORD` shorter than 12 characters | Set a longer password; login is disabled with a weak one. |
| Sessions reset after restart | `AMORE_APP_SECRET` shorter than 32 characters | An ephemeral key is generated per process. Set a stable secret. |
| Form POST returns 403 | CSRF token missing/stale or cross-origin referer | Reload the page and resubmit. Only same-host requests are accepted. |
| Run stuck in `waiting` | Interrupt needs you | Open the run; the interrupt type is `needs_input`, `awaiting_authorization` or `handoff`. The review page resolves it; the worker resumes automatically. |
| Run `waiting` with `needs_input: approved base resume not selected` | No approved DOCX chosen | Profile page or `amore profile base-resume <docx> --approved-on YYYY-MM-DD`. Old tailored copies are never used as a base. |
| Application `needs_verification` | Timeout or ambiguous receipt during submission | Check the employer portal/email. Reconcile on the application page; nothing is retried automatically. |
| Application `handoff` | No adapter/form plan for this host, or a login challenge/CAPTCHA | Finish in your own browser, then "Record manual submission" with the exact confirmation text. For login challenges refresh the session with `amore browser login <site> <url>` on the Mac. |
| Authorization refused: "material content changed" | Answers or document hash changed after you authorized | Review the new package and authorize again. This is intentional. |
| Sync item `local_only` | Push failed (network/credentials) | Committed in the clone, not pushed. Fix credentials, click "Retry pending" or `amore tracker sync`. The application status is unaffected. |
| Sync item `conflict` | `records.py add` rejected the record (employer/requisition already recorded) | Inspect the tracker record and the local application; resolve by hand in the clone, then reconcile. |
| Sync status says coordinator lock held | Previous worker crashed mid-sync | "Reconcile after crash" (or `amore tracker sync --reconcile`) inspects the clone and repairs outbox states. |
| PDF page count "unverified" locally (`render_note: no LibreOffice renderer available`) | LibreOffice not installed on the Mac | `brew install --cask libreoffice`, then `soffice --headless --version`. No env var: `find_soffice()` uses `soffice` on `PATH` or `/Applications/LibreOffice.app/Contents/MacOS/soffice`. The Docker image includes `libreoffice-writer`. |
| Rendered PDF uses a different font than Word shows | Calibri missing on the Mac (no Microsoft Office) | Expected: LibreOffice substitutes bundled Carlito (identical metrics), the same font the container uses. Page counts are unaffected. |
| `uv run pytest`/`ruff`: command not found after `uv sync` | `pytest`/`ruff` live in the `dev` extra; plain `uv sync` removes extras | `uv sync --frozen --all-extras`. |
| Model calls fail with `ProviderUnavailable` | `AMORE_MODEL` or API key unset | Deterministic qualification/tailoring still runs; model-backed ranking, extraction and drafting are skipped and marked as such. `amore provider-check`. |
| `BudgetExhausted` | Per-run token/USD/step/time limit reached | Raise `AMORE_BUDGET_*` for the run or split the batch. Usage is visible per run. |
| Discovery did not run at the scheduled time | Worker down at that minute, or slot already claimed | Slots are claimed once per Toronto date; a worker catching up within 6 hours runs it once. Check `scheduler_slots`. |
| Duplicate job appears twice | Different identities (new requisition ID vs LinkedIn ID) | Jobs page shows `possible duplicates`; merge by reconciling the status manually. Exact identities are unique-indexed and cannot duplicate. |
| Recipient blocked: "unknown contact history" | No reconciliation recorded | Check LinkedIn/email for prior contact, record it, then "Mark reconciled". First contact needs this. |
| Browser test skipped | Chromium missing | `uv run playwright install chromium` (lands in `~/Library/Caches/ms-playwright`). `playwright install-deps` is Linux-only and does nothing on macOS. |
| Chromium works in one shell but "Executable doesn't exist" in another | `PLAYWRIGHT_BROWSERS_PATH` set in that shell (agent sandboxes point it at a temp cache) | `env -u PLAYWRIGHT_BROWSERS_PATH uv run ...`, or run `uv run playwright install chromium` once from a normal Terminal so the user cache is populated. |
| Mongo tests skipped | dev container down | `docker compose --profile dev-mongo up -d dev-mongo`. |
