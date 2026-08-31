# M1 Migration

M1 uses the existing `scanner_state_path` SQLite database. On first open it creates `pipeline_jobs`, `pipeline_job_paths`, `pipeline_ingest_observations`, `pipeline_job_transitions`, `pipeline_stage_attempts`, `pipeline_stage_events` and `pipeline_schema_meta` with `CREATE TABLE IF NOT EXISTS`. Existing queue, delivery ledger and WebUI tables are not renamed or removed.

The implementation and migration path have been validated locally, including `rtk python verify_m1_docker_restart.py --no-pull`. This document is a deployment procedure, not evidence that an UNRAID host has been updated.

## Upgrade

1. Stop the Worker cleanly and back up the persistent `/work` volume/database using the existing backup procedure.
2. Merge the keys from `docs/M1_CONFIG_EXAMPLE.yaml` into the deployment configuration. Review the low-frequency AI reconciliation limits, unfinished-batch delay, Mikan incremental root budget/cooldown, retry limits and media-probe timeout for the host.
3. Start the updated container. Schema creation occurs in the existing WAL database before Watchdog reports ready.
4. Confirm the startup log reports one reconciliation and that `pipeline_schema_meta.schema_version` is `1`.
5. Add one bounded canary file. Confirm its Job advances from `DISCOVERED` through `QUEUED`, then reaches a verified terminal result. For files without native close-write evidence, confirm the admission log records the full ffprobe gate. Before accepting `COMPLETED`, confirm source identity, strict manifest rehash and required-artifact hashes are present.

No data copy, queue reset or new service is required. Rollback to the previous application version leaves the new tables unused; do not delete them because they contain the M1 audit/recovery history. Interrupted work remains subject to its persisted bounded retry budget after upgrade or restart.
