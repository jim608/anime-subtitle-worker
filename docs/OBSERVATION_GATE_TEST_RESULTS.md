# M2 Frozen Observation Gate Test Results

## Result boundary

- Repository candidate date: `2026-09-04`
- Core frozen-cohort/schema/event-journal suite: **24/24 PASS**
- Recovery/replay suite: **11/11 PASS**
- Isolated breaker harness: **7/7 breaker cases PASS**
- Production source/output used or modified: **No**
- Repaired Worker image deployed: **not established by these local results**
- Fresh server-image regression and Gate creation: **deployment-time validation pending**

These focused results validate the repository candidate against isolated
SQLite databases, synthetic inputs, sentinel checkpoints and sandbox output
directories. They do not claim a new Production Gate, a completed 20-job Gate,
`M2_PRODUCTION_ACCEPTED`, or a 99%/99.9% rate.

## Core frozen-cohort/schema/event-journal suite: 24/24 PASS

Command:

```text
python -m unittest -v test_m2_frozen_observation.py
```

| Case | Required behavior | Result |
| --- | --- | --- |
| 1 | First 20 eligible stable jobs receive frozen ordinals 1–20 | PASS |
| 2 | Claim 21 is supplemental and never a member | PASS |
| 3 | Failed member keeps its slot; no success backfill | PASS |
| 4 | Needs-review member keeps its slot | PASS |
| 5 | Quarantined member keeps its slot | PASS |
| 6 | Hallucination-blocked member keeps its slot | PASS |
| 7 | Pre-gate attempt is preserved as supplemental only; historical count remains 8 | PASS |
| 8 | Duplicate claim for the same stable job reuses the original slot | PASS |
| 9 | 32 concurrent claimers create exactly 20 unique members and 12 supplemental records | PASS |
| 10 | Module restart preserves membership and next ordinal | PASS |
| 11 | Database reopen preserves Gate/member terminal evidence | PASS |
| 12 | Same-baseline restart is idempotent and does not invalidate | PASS |
| 13 | Worker SHA drift atomically invalidates with expected/actual evidence | PASS |
| 14 | Configuration drift atomically invalidates | PASS |
| 15 | Invalidated Gate rejects admission without changing queue/checkpoint sentinels | PASS |
| 16 | Missing terminal evidence can never become strict pass | PASS |
| 17 | Summary is journaled only at 20 terminal members and published once | PASS |
| 18 | Restart and terminal replay do not duplicate the summary | PASS |
| 19 | Version-1 to version-2 migration is atomic and preserves Gate/member/supplemental history | PASS |
| 20 | Future or invalid schema fails without leaving a partial migration | PASS |
| 21 | Result-event payload tampering blocks final settlement and summary creation | PASS |
| 22 | Result-event digest tampering blocks final settlement and summary creation | PASS |
| 23 | Retry incidents remain counted after the same frozen member later succeeds strictly | PASS |
| 24 | Pre-gate and post-slot-20 supplemental result incidents are excluded from the frozen summary | PASS |

The concurrency case verifies the database invariants directly: unique
`(gate_id, job_id)`, unique `(gate_id, ordinal)`, ordinals exactly 1–20, and no
enrolled count above 20 under concurrent `BEGIN IMMEDIATE` transactions. The
schema cases verify atomic version-1 migration, complete rollback for
unsupported/invalid shapes, and version-2 shape validation. Event-journal
cases verify that summary construction accepts only immutable canonical payloads
whose SHA-256 matches, preserves retry incidents even after strict success, and
excludes durable result events that belong only to pre-gate or post-slot-20
supplemental jobs.

The successful migration case retains the historical Gate row's original
per-Gate schema version and evidence while advancing only the database-wide
schema marker to version 2. It adds the Worker container ID/runtime-instance
fingerprint fields, canonical result-event payload, immutable event triggers,
and a terminal write-once trigger that names every terminal projection column.
The failure case verifies the complete DDL/version rollback rather than merely
checking for an exception.

## Recovery and replay suite: 11/11 PASS

Command:

```text
python -m unittest -v test_m2_observation_recovery.py
```

| Case | Required behavior | Result |
| --- | --- | --- |
| 1 | Restart-recovered verified success settles queue, delivery ledger and Gate member in one transaction | PASS |
| 2 | Restart-recovered interrupted line retranslation settles as `NEEDS_REVIEW` | PASS |
| 3 | Acceptance-lane restart records the deferred attempt as one nonterminal `RETRYING` Gate event in the recovery transaction | PASS |
| 4 | Acceptance-lane stale observer failure atomically rolls back queue/attempt/member/event state and then replays once | PASS |
| 5 | Stale-running requeue also settles interrupted line retranslation as `NEEDS_REVIEW` | PASS |
| 6 | Stale-running observer failure rolls back queue/attempt/member changes and trips the breaker | PASS |
| 7 | Operator `ai.skip` terminalizes an enrolled running member as `NEEDS_REVIEW` | PASS |
| 8 | Two concurrent summary publishers report exactly one emitter | PASS |
| 9 | Failed terminal replay does not advance OOM or identical-failure streaks twice | PASS |
| 10 | Terminal replay ignores a later unrelated ambient breaker state and preserves original evidence | PASS |
| 11 | Retry followed by strict success retains the earlier OOM incident in the final summary | PASS |

The first recovery case deliberately makes observation persistence fail and
verifies that queue/delivery success rolls back with it. The stale-running path
separately verifies rollback plus breaker trip when strict observation fails.
The acceptance-lane cases verify restart records deferred work without settling
its slot and that a stale callback failure leaves no partial event before its
single successful replay. These cases prevent crash-window or scanner recovery
from settling queue and delivery state outside the frozen-member transaction.

## Isolated breaker harness: 7/7 PASS

Command:

```text
python -m unittest -v test_m2_guardrail_fault_injection.py
```

The six harness tests execute and validate all seven required breaker classes:

| Breaker class | Result |
| --- | --- |
| Source mutation | PASS |
| Duplicate publish | PASS |
| Output parse failure | PASS |
| Incorrect completion | PASS |
| Repeated OOM | PASS |
| Repeated identical stage failure | PASS |
| Insufficient disk space | PASS |

For each breaker, the sandbox checks production admission is called, no new job
is accepted after trip, queue/checkpoint sentinels remain intact, no false
`COMPLETED` is produced, bounded reason/evidence persists, and isolated recovery
is safe. Repeated runs use distinct timestamped artifact directories; successful
CLI output is one bounded summary line and failed output is a bounded tail.

The earlier deployed runtime also recorded a historical server-image 7/7
result. That record is retained in `docs/CIRCUIT_BREAKER_TEST_RESULTS.md`, but it
does not substitute for a fresh 7/7 execution in the repaired Worker image.

## Required deployment-time validation

Before a replacement Gate may be described as ready, the deployment closeout
must preserve a timestamped server log and verify:

1. the repaired Worker runtime SHA and image/container identity;
2. unchanged intended WebUI runtime SHA;
3. all 24 focused observation/schema/event-journal and 11 recovery/replay tests
   in the deployed image;
4. fresh 7/7 isolated breaker execution in that same image;
5. related M1/M2 regression result;
6. runtime guardrail status `ARMED`; and
7. a newly created Gate ID/start/baseline with a one-time initial `0/20` read.

Claims may then resume. Validation must stop without waiting for, refreshing or
polling the first 20 jobs.
