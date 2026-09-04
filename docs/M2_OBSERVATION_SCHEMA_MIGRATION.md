# M2 Observation Schema Migration

## Purpose

The repaired observer adds a frozen-cohort ledger to the existing
scanner-state SQLite database. The migration is additive: it does not rewrite
Production source media, output artifacts, queue history, delivery attempts,
stage checkpoints or Decision Records.

Observation schema version: `3`

Eligibility policy version: `m2-frozen-first-20-v1`

## Storage location and database mode

The observer uses the same database selected by `scan_state_path(config)`. It
does not create an independent in-memory or JSON-only Gate counter. Database
connections enable foreign keys, a bounded busy timeout and SQLite WAL. Claim
enrollment and terminal recording use `BEGIN IMMEDIATE` transactions.

The live database must never be edited manually over SMB. Deployment must not
remove a deployment lock or bypass the repository safe-update path.

## Additive objects

| Object | Purpose | Key invariants |
| --- | --- | --- |
| `m2_observation_meta` | Schema marker and durable failure streaks | one value per key |
| `m2_observation_gates` | Immutable historical and active Gate identities, including canonical legacy runtime evidence | one `ACTIVE` Gate; target exactly 20; counts bounded |
| `m2_observation_gate_jobs` | Frozen members and complete terminal evidence | unique stable job and ordinal per Gate; ordinals 1-20 |
| `m2_observation_supplemental` | Pre-gate, post-20, settled-Gate and other excluded claims | never contributes to cohort counters |
| `m2_observation_result_events` | Per-attempt retry/terminal incident journal | immutable unique claim identity, canonical payload and payload digest |

Triggers prevent deletion of Gate/member/result history, mutation of Gate or
member identity, a non-next/non-active member insert, and replacement of
write-once terminal evidence or the summary journal. Once a Gate leaves
`ACTIVE`, its final status cannot be changed.

After a member has `terminal_at`, the terminal trigger covers every mutable
terminal projection: current/final state, all strict/projected result columns,
reason/strategy, breaker evidence, incident flags and terminal digest. Result
event update/delete triggers make the attempt journal append-only.

## Version 1 to version 2

`ensure_observation_schema` owns one `BEGIN IMMEDIATE` transaction when called
outside a transaction and uses a savepoint when the caller already owns one.
It executes individual static DDL statements without
`sqlite3.executescript`'s implicit commit. Any exception rolls back all schema
and data changes.

For a supported version-1 database, version 2:

- adds `worker_container_id` and
  `worker_runtime_instance_fingerprint` to the Gate table;
- adds canonical `event_payload_json` to the result-event journal;
- installs/replaces the complete version-2 triggers and indexes;
- creates any missing additive observation objects;
- validates the required table columns, indexes and triggers, including every
  field protected by the terminal write-once trigger; and
- updates the global `m2_observation_meta.schema_version` to `2` only after
  validation succeeds.

Historical Gate rows retain their original per-Gate `schema_version`; version-1
Gate/member/supplemental data and counts are not rewritten. The new container
fields on those historical rows remain empty rather than inventing runtime
evidence. Newly created version-2 Gates must carry the verified full Docker
container ID and runtime-instance fingerprint.

## Version 2 to version 3

Version 3 adds `legacy_runtime_manifest_json` and
`legacy_runtime_manifest_sha256` to each Gate row. The former Gate import stores
the complete canonical pre-invalidation runtime manifest, including the hashed
`pre_gate_running.attempt_keys`, `queue_job_keys` and their counts, plus the
SHA-256 of those exact canonical bytes. Both fields are protected by the Gate
identity trigger and survive replacement-Gate re-arm even though the live
runtime JSON path is overwritten with the new Gate manifest.

Existing version-1 or version-2 rows receive empty defaults rather than
invented historical evidence. A replacement Gate cannot be created while the
legacy observation JSON exists unless the exact explicit legacy invalidation
has already persisted and verified both legacy payloads. The initializer never
silently imports the former Gate with a zero pre-gate count.

A malformed/negative/future schema version fails before mutation. An invalid
version-1/version-2 or current version-3 shape fails closed. The atomic migration test
verifies that added columns, dropped/recreated triggers, new tables and the
global version marker all roll back together when shape validation fails.

## Preserving the former Gate

The former JSON observation state is imported as immutable Gate history with:

- final status `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`;
- baseline `m2-guardrail-v1:276fdef781528ba2059c114e`;
- start `2026-09-04T11:00:12.736033Z`;
- Worker SHA `99d383f7ee72628875a223b3ff2c4f4f05845ec3`;
- WebUI SHA `7bd36c30fb07e393eba71760a164246d267c5b16`;
- the complete canonical legacy JSON payload and its hash;
- the complete canonical old runtime manifest and its SHA-256; and
- the eight hashed pre-gate attempt identities (plus queue-job snapshot) and
  pre-gate attempt count `8`.

Import requires the exact expected legacy identity and invalidation reason. A
mismatch aborts rather than importing ambiguous history. Repeating the same
import is idempotent. The eight pre-gate attempts remain supplemental only;
the migration performs no retroactive membership selection. Re-arm fails
closed until this exact import is present and internally verifiable.

## Deployment sequence

1. Durably pause new AI claims through the existing control-command path and
   verify the pause record once.
2. Preserve the current database through the normal safe-update backup path.
3. Deploy the tested repository revision. Do not manually move, replace or
   modify the SQLite database.
4. Start/open the database so the additive version-3 objects and triggers are
   installed.
5. Import/invalidate the former observation state with the exact historical
   expectations above while claims remain paused.
6. Run the 24 core frozen-cohort/schema/event-journal cases, 11 recovery/replay
   cases, related M1/M2 regression and fresh seven-breaker sandbox in the
   deployed Worker image.
7. Verify the live Worker/WebUI/image/container/configuration/Decision
   identities and confirm the breaker runtime reports `ARMED`.
8. Create one replacement Gate. Its Gate ID, start timestamp and baseline are
   runtime-generated deployment evidence.
9. Read the bounded Gate status once and require `ACTIVE`, enrolled `0`,
   settled `0`, target `20`.
10. Resume claims. Do not poll the queue, continuously tail Docker logs or wait
    for the Gate to finish.

If a partial or unexpected observation schema cannot pass the supported
version-1/version-2 migration and final shape validation, deployment must stop for
bounded inspection. The failed migration leaves no partial columns, tables,
triggers or version bump. No destructive schema downgrade is defined.

## Transaction boundaries

For an ordinary claim, queue transition, delivery-attempt creation and Gate
enrollment share one `BEGIN IMMEDIATE` transaction. A failed enrollment or
runtime validation rolls back the claim-side state.

For each retry/terminal result, the unique claim identity, canonical bounded
event payload and its SHA-256 are reserved with the associated side effects.
For a terminal result, queue/delivery terminal state and frozen-member evidence
share one transaction. Startup and stale-running recovery follow the same
transaction rule. In the scanner stale-running path, observer failure rolls
the unit back and trips the observation breaker. Summary publication occurs
only after the database commit; the durable summary payload and digest act as
its outbox.

Acceptance-lane restart recovery records its deferred queue/attempt transition
and one nonterminal `RETRYING` result event in the same transaction. If the
acceptance stale observer fails, queue, attempt, member and result-event changes
all roll back; a later callback can reserve the event and replay exactly once.

Runtime mismatch validation and the change to
`INVALIDATED_BY_RUNTIME_CHANGE` occur in the admission transaction before a
new queue row can be claimed. Expected/actual evidence is persisted with the
invalidation. Existing queue rows, members and checkpoints are not deleted.

## Restart behavior

On an exact-baseline restart, the runtime derives the same Gate identity,
reopens the same database row, preserves all ordinals/evidence, and continues
from the next free ordinal. Restart does not reset counts or create another
Gate.

If the process stops after a terminal summary is journaled but before its file
is marked emitted, the publisher resumes from the durable outbox. It verifies
an already-created file by digest or atomically creates it, then stores the
emission marker. Terminal replay and concurrent publishers cannot create a
second logical report.

Before the twentieth terminal transaction can settle the Gate, every stored
result event must have a valid matching digest, parse as an object and be in
canonical form. Payload/digest tampering rolls that transaction back, leaving
the Gate at 19 settled with no summary. Summary incident counters are derived
only from attempt events joined to frozen Gate members, so an earlier OOM,
parse failure or breaker trip remains counted if the member later completes
strictly while pre-gate and post-slot-20 supplemental incidents remain excluded.

## Failure and recovery policy

- Schema installation/import failure: keep claims paused, keep the previous
  database/backup intact, emit only a bounded log tail, and do not arm a new
  Gate.
- Test or runtime identity failure: do not create/resume the Gate.
- Breaker trip: stop new claims and preserve the breaker reason/evidence,
  queue, running job and checkpoints.
- Runtime drift after Gate creation: atomically mark the Gate
  `INVALIDATED_BY_RUNTIME_CHANGE`, stop new claims and do not mix versions.
- Same-version Worker restart: reopen the existing Gate; do not invalidate or
  reset it.

No recovery step may delete a Gate/member row, renumber a slot, replace a
failed member, or copy a later success into the frozen cohort.

## Evidence boundary

This document defines the migration contract. It does not assert that the
repaired revision is already deployed. The new runtime SHA, container identity,
Gate ID, Gate start, Gate baseline, fresh server test log and initial `0/20`
status must come from the deployment run. The deployment closeout must finish
after claims resume and must not wait for 20 terminal jobs.
