# M1 Design — Durable Ingestion and Pipeline State

## Scope

M1 extends the existing SQLite WAL store and existing Worker. It does not add Redis, Celery, Kafka, change translation models, redesign QC, or alter WebUI behavior.

## Ingestion

All Watchdog events and reconciliation discoveries call one idempotent discovery path. A durable observation records canonical location, file identity, size, nanosecond mtime, first/last seen time, stable sample count and close-write evidence. Temporary/incomplete names are rejected before stat/probe work.

Admission requires stable size and mtime over the configured quiet window plus a read-open/stat-consistency check. A native close-write event can use its durable close evidence; a no-close quiet event must pass a full `ffprobe -count_packets` parse check outside the callback thread. Failed or timed-out probes remain pending for bounded retry. Restart reloads pending observations instead of losing stabilization progress.

## Identity and compatibility

A UUID `job_id` identifies one media revision. Media revision uses canonical path plus size/mtime and filesystem identity when available; path alone is never the uniqueness authority. The new Job Store dual-writes/links to the legacy AI queue so existing scanner, WebUI and Worker behavior remain operational during the migration.

## Persistence

The existing database gains durable Jobs, ingest observations, immutable transition history and per-stage attempts. A stage attempt stores start/end, input/output evidence, model, attempt number, timeout/deadline, retry/error classification and checkpoint evidence. Completion of a Stage is committed only after its output/checkpoint validates.

## Recovery and publication

On startup, a running attempt is recorded as interrupted. Interrupted attempts consume the same bounded Stage retry budget; a recoverable Job transitions through `RETRYING` to `QUEUED`, while repeated interruption eventually enters `NEEDS_REVIEW`. Completed valid stages remain reusable. The legacy Worker caches remain the execution mechanism in M1, while the unified store is the audit and resume authority.

Immediately before publication, the source identity is compared with the captured read-only snapshot. Formal `COMPLETED` additionally requires strict delivery evidence: manifest-v2 and every required final artifact are rehashed, media identity is matched, and temporary/publication markers are absent. A temporary artifact or stale manifest can never satisfy completion.

## Low-I/O policy

Watchdog is primary. Reconciliation runs once after startup and thereafter only at a configurable low frequency, with bounded roots/time/items and a durable cursor/cooldown. An unfinished pass must not hot-loop. Watcher failure uses the same bounded low-frequency policy. Mikan uses persistent incremental index coverage, dirty/due metadata and cooldown; each pass selects only the configured bounded number of roots (default eight), including when episode-index lookup is unavailable. Direct scanning is a bounded fallback when persistent catalog access fails, not an all-root per-cycle path.
