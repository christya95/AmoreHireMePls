# Backup and restore

## What is backed up

`amore backup <out.tar.gz>` writes one archive containing:

- `db/<collection>.json`: every application collection as MongoDB Extended JSON (profile, jobs, applications, actions, authorizations, recipients, outreach, sync_outbox, runs, audit, usage, cache, artifacts, controls, scheduler_slots, imports) plus the LangGraph checkpoint collections (`lg_checkpoints`, `lg_checkpoint_writes`) unless `include_checkpoints=False`.
- `manifest.json`: time, database name, document counts, app version.
- `artifacts/`: tailored DOCX/PDF outputs and screenshots (`AMORE_ARTIFACT_DIR`).
- `private/`: base resume copies, browser session states, profile exports (`AMORE_PRIVATE_DIR`).
- `config/`: `.env.example`, `.env.references.example`, `config/pricing.json` (never `.env`).

The archive contains private data. Store it encrypted (1Password document, encrypted NAS share). It never belongs in git.

## Restore

```bash
uv run amore restore /path/archive.tar.gz            # target database must be empty
uv run amore restore /path/archive.tar.gz --force    # drops and replaces the target collections and directories
```

Restore recreates indexes afterwards. Runs that were `running`/`claimed` at backup time are reconciled on the next worker tick (lease expiry), the sync outbox is replayed by state, and actions that were mid-flight are `needs_verification`, never silently re-executed.

## Demonstrated restore (non-production data)

Verified in `tests/test_backup_worker.py::test_backup_and_restore_round_trip_with_artifacts` against the dev Mongo container: a throwaway database with a profile, job, application and artifact file is backed up, dropped, restored into a fresh database, and document counts, the artifact bytes and the recreated unique indexes are asserted equal. Restore into a non-empty database without `--force` is asserted to refuse.

## Suggested schedule

On the NAS, run nightly from the host (not from inside the container) so the archive lands on a different disk than the Mongo volume:

```bash
docker compose -f /volume1/docker/amorehiremepls/docker-compose.yml run --rm -v /volume1/backups/amore:/backup web amore backup /backup/amore-$(date +%F).tar.gz
```

Keep 14 daily archives; test a restore into an empty database monthly.
