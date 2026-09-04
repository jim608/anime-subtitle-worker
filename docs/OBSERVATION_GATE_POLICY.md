# M2 Frozen Observation Gate Policy

## Scope and authority

This policy governs only the M2 Production observation cohort. It does not
change translation, source decisions, QC, hallucination detection, model
routing, the Decision Schema, breaker thresholds, or WebUI behavior, and it
does not start M3.

The durable SQLite schema-v3 record is authoritative for Gate identity,
membership, immutable per-attempt result events, terminal evidence,
invalidation and summary publication. JSON runtime status is a bounded
operational view and cannot create, repair, reorder or replace a Gate member.

## Historical Gate disposition

The former baseline `m2-guardrail-v1:276fdef781528ba2059c114e`, started at
`2026-09-04T11:00:12.736033Z`, has the final status
`INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`.

- Its full baseline, start time, invalidation reason and legacy observation
  payload remain immutable history.
- Its canonical old runtime manifest and SHA-256 preserve the exact eight
  hashed pre-gate attempt identities and queue-job snapshot after re-arm.
- Its eight pre-gate attempts remain supplemental records only.
- No former attempt or result may be selected retroactively into a replacement
  Gate.
- The former `0/20` counter cannot be resumed, repaired or presented as a
  conforming Gate.

## Gate identity and frozen baseline

A replacement Gate is created only while new claims are durably paused and
after the deployed Worker image has passed bounded validation. Its immutable
identity includes:

- Gate ID and UTC `gate_start_at`;
- schema version and eligibility-policy version;
- Worker and WebUI commit SHAs and source revisions;
- Worker and WebUI image IDs;
- host-attested Worker container ID, in-container identity and
  runtime-instance fingerprint;
- configuration fingerprint;
- Decision schema version; and
- target size, fixed at 20.

The live values are deployment-time evidence. A document or configuration file
must not guess them before the runtime creates the Gate.

At arm time, host inspection supplies the full Docker container ID and hostname
while an in-container probe reads its own identity and derives the instance
fingerprint from the root overlay token when available, otherwise from the
container identity. The identities must agree. A normal restart of the same
container preserves the binding; a recreated container is treated as a new
runtime instance and must be revalidated rather than silently continuing an
old Gate.

## Claim-time enrollment

Enrollment occurs in the same SQLite transaction that changes the queue row to
running and creates the delivery attempt. The transaction uses WAL,
`busy_timeout`, and `BEGIN IMMEDIATE`, so concurrent claimers serialize before
reading or incrementing the frozen ordinal.

An eligible stable job receives the next ordinal from 1 through 20 only when:

1. the Gate is `ACTIVE`;
2. the claim time is after the Gate start;
3. no attempt for the stable delivery obligation started before the Gate;
4. the loaded runtime matches every immutable baseline field;
5. the stable job is not already a Gate member; and
6. fewer than 20 members have been enrolled.

The stable delivery-obligation identity owns the slot. Retry-attempt IDs are
recorded as claim/result identities but cannot consume another slot. A replayed
or duplicate claim returns the existing ordinal.

Database constraints enforce one `(gate_id, job_id)` membership and one
`(gate_id, ordinal)` membership. A trigger accepts only the next ordinal and
only while the Gate is active and below 20. Membership identity cannot be
updated or deleted.

## No-backfill rule

The first 20 eligible, distinct stable jobs are the complete cohort. Selection
does not depend on outcome.

- `FAILED`, `NEEDS_REVIEW`, `QUARANTINED`, hallucination-blocked, retry-exhausted
  and any other non-passing terminal member keeps its original slot.
- Claim 21 and all later claims are supplemental with a bounded exclusion
  reason. They can never replace a member.
- A claim for a job started before the Gate is supplemental even if its retry
  occurs after Gate start.
- Once all 20 slots are settled, later claims remain supplemental and normal
  Production processing may continue if guardrails allow it.

## Restart and recovery

Gate state, member ordinals, claim identities, evidence and summary journal are
stored in the existing scanner-state SQLite database, not process memory. A
Worker/module restart or database reopen with the exact same baseline reuses
the same Gate and resumes at the next unassigned ordinal.

Startup reconciliation must settle crash-window outcomes in the same database
transaction as queue and delivery-ledger recovery:

- a verified previously completed running attempt becomes `COMPLETED` with its
  strict evidence;
- an interrupted line-retranslation attempt becomes `NEEDS_REVIEW` rather than
  remaining indefinitely nonterminal, including the stale-running requeue
  path; and
- an acceptance-lane running attempt recovered at restart becomes a deferred
  queue item plus a durable `RETRYING` event in the same Gate transaction;
- an operator `ai.skip` on an enrolled running job becomes `NEEDS_REVIEW`.

If observation evidence cannot be written, the associated queue/delivery
recovery transaction rolls back. An acceptance-lane stale observer failure
leaves its attempt, queue, member and event journal untouched so the callback
can replay once safely. A scanner stale-running observer failure also trips the
breaker with `observation_pipeline_failure` evidence. Replay of a terminal
claim is idempotent and does not advance breaker failure streaks or rewrite
immutable evidence.

## Runtime drift and claim stop

Every claim and terminal observation revalidates the active Gate baseline.
Mismatch in any bound runtime identity atomically changes the Gate status to
`INVALIDATED_BY_RUNTIME_CHANGE`, records a bounded reason plus expected and
actual values, and rejects further Gate admission. A missing observation
database while runtime state claims an armed Gate also fails closed.

Runtime invalidation does not delete or rewrite queue rows, running work,
source media, outputs, checkpoints, attempt history, or existing Gate members.
The current job may finish or reach its normal recoverable checkpoint, but no
new job may be claimed until an operator resolves and explicitly re-arms the
runtime. Statistics from different baselines must never be combined.

A breaker `TRIPPED` state similarly stops new claims and preserves reason and
evidence, but does not relabel the Gate unless a baseline mismatch also exists.

## Per-attempt result-event journal

Schema version 2 and later record every result attempt under a unique claim-identity
hash. The event row contains the Gate/stable-job binding, observed state,
canonical bounded payload, SHA-256 of that exact payload and creation time.
Update/delete triggers make the journal append-only.

This journal preserves nonterminal retry incidents even when the same frozen
member later reaches strict `COMPLETED`. A result replay with the same attempt
identity is ignored and cannot increment OOM/identical-failure streaks or
incident counters twice. Summary construction joins events to the frozen member
table by Gate and stable-job identity; events for pre-gate, slot-21-or-later or
other supplemental jobs remain durable but never enter frozen counters. Before
settling the Gate, summary construction recomputes every included event digest,
parses the payload, and requires its canonical serialization. Payload/digest
tampering fails the transaction at 19 settled members; it cannot publish a
summary from altered incident history.

## Per-member terminal evidence

Every frozen member preserves at least:

- ordinal, stable job hash, claim identity hash and claimed time;
- final state and terminal time;
- strict-verification result and reason codes;
- output parse, hard QC and hallucination results;
- source-checksum result;
- duplicate-job and duplicate-publish results;
- Decision Record completeness and stage/checkpoint-history completeness;
- checkpoint resume and retry/fallback results;
- runtime-baseline match;
- breaker evidence, incident flags, processing strategy and bounded
  failure/review reason; and
- an immutable terminal-evidence digest.

Missing evidence is recorded as missing and can never be interpreted as pass.
A strict-verified result requires `COMPLETED` plus PASS/true evidence for every
required contract field and no unresolved retry, quarantine or fallback. Once
`terminal_at` is set, the write-once trigger protects the current/final state,
all strict and projected parse/QC/hallucination/source/duplicate/checkpoint
results, breaker evidence, reason, strategy, incident flags and terminal
digest from any later update.

## Exactly-once terminal summary

No Gate summary is created at 19/20 or before. The transaction that settles the
twentieth member:

1. verifies that exactly 20 members are enrolled and terminal;
2. validates every canonical result-event payload/digest and derives fixed
   terminal counters plus processing strategies from member rows and attempt
   incident counters from the append-only journal;
3. marks the Gate `SETTLED`; and
4. journals the canonical summary payload and SHA-256 in the database outbox.

The publisher writes one atomic, sanitized `<gate_id>.json` artifact and then
records its emitted timestamp/path. A concurrent publisher, process restart or
terminal replay observes the durable journal/emission state and does not emit
a second logical report. If the atomic file exists after a crash but the
emission marker does not, the publisher verifies the expected digest before
completing the marker.

The summary reports only the frozen 20 members and includes Gate identity,
baseline/start/end, enrolled/settled progress, strict-verified, review, failed,
quarantined, hallucination-blocked, parse/source/duplicate incidents, breaker
trips, checkpoint resumes, OOM events and processing-strategy counts. It must
not expose media titles, paths, subtitles, prompts, credentials, addresses or
raw log text.

## Deployment and observation boundary

The deployment closeout must:

1. durably pause claims;
2. deploy through the repository safe-update path without editing live SQLite
   over SMB or removing deployment locks;
3. preserve/import the historical Gate invalidation;
4. run all 24 observation/schema/event-journal tests, 11 recovery/replay tests,
   the related regression and fresh seven-breaker sandbox in the deployed
   image, retaining timestamped logs;
5. verify the live Worker/WebUI/image/configuration/Decision identities;
6. create and read once the replacement Gate at `0/20`; and
7. resume claims.

The closeout must not poll the queue, continuously read Docker logs, or wait for
20 jobs. Only the terminal 20/20 summary or a breaker/invalidation event should
produce an observation report. A started Gate is not evidence of
`M2_PRODUCTION_ACCEPTED`, 99%, or 99.9% autonomy.
