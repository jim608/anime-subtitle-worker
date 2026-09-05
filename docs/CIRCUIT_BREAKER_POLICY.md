# M2 Circuit Breaker Policy

## Recovery unblock candidate safety boundary (2026-09-05)

Generic Worker failures are grouped by normalized underlying detail, not only
the generic error code; media paths and attempt IDs cannot split one recurring
cause. Typed materialized-subtitle content refusals use existing NEEDS_REVIEW
semantics. Three distinct jobs with a genuinely identical system failure still
trip. Each trip occurrence retains its current event/member evidence.

The narrowly supported `generic_failure_signature_collision` recovery requires
the exact current Gate, immutable three-attempt/event/digest evidence, matching
current counter and trip epochs, distinct normalized cause groups below the
threshold, unchanged sources/Queue/checkpoints, a safe claim boundary, changed
verified runtime, fresh 7/7 fault injection and hashed running-image regression
evidence. It cannot clear a genuinely identical streak or run blanket historical
reconciliation. Normal retry/backoff and existing recovery-lane limits remain.

A local hard-display-limit QC refusal cannot pause unrelated recovery as a
permanent system failure. Repair of the already-paused lane requires exact last
canary dispatch/settlement hashes, current run/version, review-required result,
unchanged paused Queue entry and checkpoint, and no inflight recovery. Preserve
the rejected canary; only the proven lane-level pause can become CANARY_READY.
Dispatch checks both recovery and Queue deadlines plus remaining budget again
at the atomic update; normal backoff cannot be cleared to manufacture a claim.

Normal discovery and due recovery share bounded, restartable source scheduling.
Elapsed-budget preemption preserves pending requests and retry budgets; incomplete
matching evidence cannot become a confirmed miss or a selected winner. Source
network calls must not hold SQLite write transactions. This is cooperative time
bounding with existing HTTP timeouts, not a hard kill of arbitrary filesystem or
trickling network operations.

Official import must pass complete parsing, existing source validation and the
unchanged hard QC. Actual staged bytes are checked again before any formal write,
backup or receipt. Bad downloaded ASS files are not repaired by byte-copy
normalization. Failed content cannot overwrite valid output or become COMPLETED.
Verified season-scoped metadata does not authorize unseasoned legacy batches.

The candidate does not change breaker thresholds, Decision Schema, models,
frozen-cohort membership or runtime configuration. Actual deployment must preserve
the old Gate as INVALIDATED_BY_RUNTIME_CHANGE and attest the new running image
with fresh isolated 7/7 evidence before claims resume. Read-only verification and
document synchronization never trigger that transition.

## Acceptance-only follow-up, 2026-09-05

Read-only target validation, bounded source lookup, immutable-event inspection
and documentation synchronization did not change runtime baseline or recreate
the Gate. The b911 runtime was still `ARMED / runtime_baseline_match` and Gate
`m2-gate-20260905T045640981079Z-08147de925` remained ACTIVE. Historical download
dispositions and two observed recovery claims do not replace cohort members.
An isolated `database is locked` retry and a source-selection NEEDS_REVIEW
retained their existing budgets/checkpoints; neither was relabeled COMPLETED.
The four missing-valid-subtitle targets remained source-blocked under current
QC/dedup policy. No breaker thresholds, source decisions or claim protections
were changed. Detailed acceptance boundaries are in M2_PRODUCTION_OBSERVATION.

## M2 download recovery boundary (2026-09-05)

The final M2 runtime is Worker
`b911794ed0ec872cb475f714e1385e20e8ac4388`. Runtime `ARMED` was re-attested
after safe deployment and fresh 7/7 fault injection; the frozen baseline is
`m2-guardrail-v1:5b4d2a88f2d5c0c5749f6747`, initially `0/20`.

A controlled recovery publishes the SQLite Gate and runtime manifest under a
durable claim pause. During the narrowly identified DISARMED handoff, valid
recovery contract/record and pause evidence deny admission without invalidating
the replacement Gate. A delayed sample is ignored only after transactional
revalidation of the current ARMED runtime against the exact active Gate.
Ordinary DISARMED states, damaged evidence and genuine runtime changes remain
fail-closed. This exception never permits a claim or relaxes a breaker.

An orphan DISPATCHED recovery item with no claim, no Queue item and no running
job/delivery is recorded as `EXCLUDED / KEEP_NEEDS_REVIEW`, never succeeded.
Its immutable pre-claim event preserves media identity evidence and advances
only the existing single-canary lane. No Queue item is blindly recreated.
Quality/bad-input exclusions and budgeted retries leave the lane CANARY_READY
with existing backoff; a success is still required for ACTIVE. Permanent
system faults remain paused and TRIPPED runtime always blocks dispatch.
These records and all download recovery metrics cannot backfill a frozen Gate.

Download repair leaves breaker thresholds, Decision Schema, AI QC, hallucination
validation and model routing unchanged. External subtitle import applies the
existing Source Analyzer policy plus full timestamp parsing before atomic
publication or AI subtitle retirement. Rejected candidates are
`subtitle_validation_failed`, never COMPLETED, and use existing failed-hash,
seen-source and retry/backoff policies.

qB queued/checking/moving/paused states remain normal after partial download.
Stalled partial torrents and pieces remain available for qB resume while a
different allowed source may be considered. Timeout cleanup never deletes
downloaded files. Untrusted project torrents are not reassigned. Only an actual
runtime change invalidates the frozen Gate; history cannot fill cohort slots.

## Purpose and scope

The M2 circuit breaker is a fail-closed admission guard. It protects source
media, published outputs, queue state, and completed stage checkpoints while
the M2 strict observation gate is active.

The breaker controls only the admission of a new AI job. Opening the breaker
must not cancel an already-running job, remove or rewrite a queue item, delete
a checkpoint, modify source media, or publish a replacement output.

This policy does not establish production acceptance, an observation-gate
result, or an autonomy-rate result.

## Runtime states

| Status | Meaning | New job admission |
| --- | --- | --- |
| `DISARMED` | The observer or circuit-breaker feature is disabled. No M2 guardrail claim is made. | Allowed only when the observer itself is intentionally disabled; otherwise denied until the runtime is armed. |
| `ARMED` | The effective runtime settings, immutable runtime baseline, container/source evidence, decision contract, and required isolated breaker evidence all match. | Allowed while the latch remains closed and the disk check passes. |
| `TRIPPED` | A breaker reason is durably latched. | Denied. Running work may finish and valid checkpoints remain available. |
| `DEGRADED` | Guardrails are intended to be active, but runtime state is missing, unreadable, inconsistent, unsafe, or no longer matches the armed baseline. | Denied fail-closed. |

`ARMED` describes the guardrail runtime only. It is not a claim that a later
quality or production observation gate has passed.

## Immutable runtime baseline

Arming creates one immutable baseline containing the Worker and WebUI commit
SHAs, their running container image IDs and source revisions, the effective
configuration fingerprint, Decision schema/version/contract, gate start time,
and derived gate baseline version. The runtime must verify this evidence from
the running containers and loaded configuration, not merely from files present
in Git.

A missing, unreadable, or mismatched runtime baseline is `DEGRADED` and denies
new claims. A different source revision, configuration fingerprint, decision
contract, or formal baseline requires a new explicit validation and arm; it
must never inherit the prior `ARMED` assertion silently.

For a formal observation cohort, any result-affecting runtime, program, image,
configuration, or Decision-contract change must durably mark the active gate
`INVALIDATED_BY_RUNTIME_CHANGE`. Collection for that gate stops immediately,
and results from different baselines must never be combined. Re-arming requires
a new baseline and gate start.

## Admission boundary

Every production claim path calls the fail-closed Worker admission wrapper
immediately before the queue can be mutated or work can start. The wrapper
delegates to the circuit-breaker admission decision and returns false on a
runtime validation error.

The same boundary applies to initial dispatch, serial dispatch, concurrent
dispatch, and guarded remediation dispatch. A trip does not interrupt an
already-running job. The persisted policy is:

- action: `stop_claiming_new_jobs`
- running job policy: `finish_without_interruption`
- checkpoint policy: `preserve`

## Breaker conditions

| Reason code | Trigger | Threshold |
| --- | --- | --- |
| `source_mutation` | Source identity, revision, or checksum changes during processing. | Immediate |
| `duplicate_publish` | A destination collision or second publication cannot be matched to the same verified delivery evidence. | Immediate |
| `output_parse_failure` | A prospective final output cannot be parsed or fails final publication revalidation. | Immediate |
| `incorrect_completion` | Work reports success without the evidence required for a valid completed delivery. | Immediate |
| `repeated_oom` | Distinct jobs consecutively report an eligible GPU/process out-of-memory failure. | 3 distinct jobs |
| `repeated_identical_stage_failure` | Distinct jobs consecutively report the same eligible stage and normalized failure signature. | 3 distinct jobs |
| `insufficient_disk_space` | Any required runtime volume is below the configured free-space floor or its capacity cannot be read. | Immediate at admission |

A successful or non-system outcome resets both repeated-failure streaks. A
non-OOM system failure resets the OOM streak, and a different eligible
stage/failure signature starts a new identical-failure streak. Each streak
retains bounded stable-job identities. Retry replay, process-restart replay,
terminal-observation replay, duplicate delivery-attempt evidence, and a new
attempt for the same delivery obligation cannot increment it. The two
distinct-job thresholds are configured as `3` for this milestone.

## Failure classification and historical reconciliation

| Category | Automatic disposition |
| --- | --- |
| `TRANSIENT` | Bounded retry/backoff from a compatible checkpoint or the failed stage. |
| `RESOURCE` | Existing lower-memory/resource fallback, then bounded checkpoint/stage retry. |
| `CODE_VERSION_FIXED` | Re-evaluate on the attested new runtime and resume the nearest compatible safe stage. |
| `BAD_INPUT` | `UNSUPPORTED`, `QUARANTINED`, or `NEEDS_REVIEW`; never an unbounded retry. |
| `QUALITY_BLOCKED` | `NEEDS_REVIEW` with preserved evidence; never false `COMPLETED`. |
| `PERMANENT_SYSTEM_ERROR` | `FAILED` with evidence after recovery is not proven safe. |

The durable recovery decisions are `RECOVER_FROM_CHECKPOINT`, `RETRY_STAGE`,
`REPROCESS_FROM_SAFE_STAGE`, `REQUEUE_WITH_NEW_RUNTIME`,
`KEEP_NEEDS_REVIEW`, `KEEP_QUARANTINED`, `MARK_UNSUPPORTED`, and
`KEEP_FAILED`. Checkpoint JSON must be canonical and match its stored SHA-256.
Stage-specific schema compatibility is checked before claiming checkpoint
resume; an incompatible checkpoint is preserved as evidence while only its
minimum safe stage is rerun.

The recovery lane is a separate indexed SQLite ledger. It dispatches one exact
item into the normal queue, marks the first item as the mandatory canary, and
never enumerates the media tree. A repeated same-stage/signature failure on the
same runtime with no new checkpoint is no progress. The job is not requeued
after its bounded budget and the rest of the normal queue remains eligible.

## Persistence and evidence

Trips are atomically persisted with a normalized reason code, timestamp,
bounded evidence, action, running-job policy, and checkpoint policy. A
malformed or unreadable latch fails closed instead of silently clearing.

The full isolated validation stream is retained on the server as
`events.jsonl` together with `result.json` in a newly created timestamped run
directory. A successful command writes only a bounded summary to stdout. A
failure writes only a bounded log tail to stdout; the complete traceback and
per-assertion evidence remain in the server log.

The armed state retains the sanitized validation window, pass/required counts,
Worker source revision, result digest, and full event-log digest. This makes
the accepted breaker proof independently checkable without copying private
paths or raw logs into tracked documents.

Tracked documentation and bounded summaries must not contain media names,
source/output paths, server addresses, ports, credentials, or raw logs.

## Frozen observation cohort contract

The formal cohort is the first 20 distinct eligible jobs claimed after the
immutable gate start, ordered and durably assigned at claim time. Once all 20
slots are assigned, later claims are outside that cohort. Failed,
`NEEDS_REVIEW`, and `QUARANTINED` members retain their slots and cannot be
replaced by later successful jobs. The eight recorded pre-gate attempts remain
available as historical observations but are never cohort members.

Every cohort member must durably retain claimed time, final state,
strict-verification result, output-parse result, hard-QC result, hallucination
result, source-checksum result, duplicate-job and duplicate-publish results,
and checkpoint/retry/fallback result. Exactly one cohort summary is produced
only after all 20 fixed members have reached terminal states.

The persisted, sanitized observation summary includes `gate_progress`,
`claimed_after_gate_start`, `completed_strict_verified`, `needs_review`,
`failed`, `quarantined`, `hallucination_blocked`, `output_parse_failures`,
`source_mutation_incidents`, `duplicate_jobs`, `duplicate_publishes`,
`breaker_trips`, `checkpoint_resumes`, `oom_events`, and
`processing_strategy_counts`.

Producing the summary must not scan or poll the full queue. A breaker trip is a
separate durable event: it immediately denies new claims and persists bounded
reason/evidence without requiring a full-queue scan.

This section is the required contract. The 2026-09-04 closeout audit invalidated
the historical success-count observer as
`INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`. The replacement implementation
uses a durable frozen first-20 cohort, immutable per-job evidence, result-event
journaling, and `INVALIDATED_BY_RUNTIME_CHANGE`; repository tests do not make it
Production-ready until the repaired Worker image passes fresh server validation
and initializes a new deployment-bound Gate. Breaker 7/7 evidence alone never
proves the cohort contract or accepts the Production Gate.

## Isolation contract

The fault-injection harness never loads the production configuration. Each
case creates fresh temporary input, output, queue, running-job, checkpoint,
work, and log fixtures. The only caller-provided location is the parent for a
new timestamped validation artifact directory.

Every one of the seven cases must prove all ten invariants:

1. The production admission wrapper was called.
2. A new claim was stopped.
3. The queued job remained unchanged.
4. The valid checkpoint remained unchanged.
5. The running job was not interrupted.
6. No false `COMPLETED` state was produced.
7. The expected reason and non-empty evidence were persisted.
8. The isolated latch could be safely recovered.
9. The synthetic source remained byte-identical.
10. The output fixture remained untouched.

Disk exhaustion is injected through an isolated capacity hook rather than by
filling a real volume. OOM and repeated-stage failures are injected as
synthetic terminal observations rather than by crashing production models.

## Recovery policy

The breaker is latched and must not auto-reset. Recovery requires the cause to
be understood and remediated first. Evidence must be archived, not discarded;
fresh running work must reach a safe boundary; cutoff-proven stale work is
terminalized as interrupted without deleting its checkpoint; and the runtime
must be probed again before admission resumes.

Production recovery uses `m2_guardrail_runtime.py recover`. It refuses an
unsupported cause, unchanged Worker runtime, missing fresh 7/7 evidence,
mismatched runtime/config/Decision identity, active recent work, changed source
identity, changed queue identity, changed checkpoint content, or changed
formal-output evidence. It writes and validates a durable operator claim pause,
appends a recovery event and timestamped full log, invalidates the old Gate,
and writes `DISARMED` before closing the latch. A second attested arm creates a
new immutable `0/20` Gate. The durable pause is cleared only after the runtime
is `ARMED`, the new Gate is active, and observation reports a matching baseline;
no deletion of the breaker file or observation rows is part of recovery.

If deployment or process restart occurs after the latch is cleared but before
the replacement Gate is armed, recovery remains fail-closed. A pending-resume
operation must revalidate the original recovery record and log, old-Gate
invalidation, current runtime/source/config/Decision identity, and fresh fault
evidence before re-establishing the durable pause and continuing. It must not
repeat historical reconciliation or mix Worker versions in one Gate.

The isolated harness proves this sequence by requiring persisted reason
evidence, moving only the sandbox latch to a recovery archive, writing a
recovery record, clearing only the sandbox process latch, re-running the real
admission wrapper, and rechecking all fixture hashes. This sandbox proof does
not by itself validate a live-container restart or production recovery.

## 2026-09-05 live controlled recovery

The Production incident was caused by three distinct
`source_selection_needs_review` outcomes at `source_selection_review`. Durable
pipeline state correctly held them as `NEEDS_REVIEW`, while the legacy adapter
misclassified them as retryable system failures and the breaker counted that
signature. The repaired classification excludes `QUALITY_BLOCKED` review
outcomes, and streak membership is now keyed to distinct stable jobs.

The final running Worker
`d9dfcd01aa9ebeffe65c8367f4e1bbace56d5bcc` passed the fresh server-image 7/7
suite, completed the restart-safe pending recovery, transitioned the breaker
from `TRIPPED` to `ARMED`, invalidated the prior Gate as
`INVALIDATED_BY_RUNTIME_CHANGE`, and created
`m2-gate-20260905T020845085531Z-7d0c5c7333` at `0/20`. Normal claims resumed
only after the active baseline matched. Exactly one recovery canary was
dispatched. The bounded checks reported no Production source-media or formal-
output change.
