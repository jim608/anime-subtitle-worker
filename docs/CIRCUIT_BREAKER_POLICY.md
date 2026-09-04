# M2 Circuit Breaker Policy

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
| `repeated_oom` | Consecutive failed observations are classified as GPU/process out-of-memory failures. | 3 consecutive failures |
| `repeated_identical_stage_failure` | The same stage and normalized failure signature recur consecutively. | 3 consecutive failures |
| `insufficient_disk_space` | Any required runtime volume is below the configured free-space floor or its capacity cannot be read. | Immediate at admission |

A successful outcome resets both repeated-failure streaks. A non-OOM failure
resets the OOM streak, and a different stage/failure signature starts a new
identical-failure streak. The two repeated-failure thresholds are configured
as `3` for this milestone.

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
running work must reach a safe boundary; checkpoints and queue state must be
preserved; and the runtime must be probed again before admission resumes.

The isolated harness proves this sequence by requiring persisted reason
evidence, moving only the sandbox latch to a recovery archive, writing a
recovery record, clearing only the sandbox process latch, re-running the real
admission wrapper, and rechecking all fixture hashes. This sandbox proof does
not by itself validate a live-container restart or production recovery.
