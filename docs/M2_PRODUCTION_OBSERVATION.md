# M2 Production Observation

## Status

- Milestone state: `M2_GUARDRAILS_ARMED`
- Circuit-breaker runtime status before the repair deployment: `ARMED` (`runtime_baseline_match`)
- Former Gate final status: `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`
- Frozen-cohort repository candidate: **locally ready; Production deployment validation pending**
- Active replacement Gate: **not yet started by this document**
- Former Gate counter: **`0 / 20` at invalidation; it is not a valid cohort and cannot be resumed or backfilled**
- Production acceptance: **not accepted**
- Scope: M2 observation-automation repair and deployment closeout only. This work does not start M3.
- Evidence boundary: fixed values below describe the invalidated historical Gate and its bounded snapshot. New live Worker/image/Gate values are deployment-time evidence and are intentionally not invented here. The queue is not continuously polled or observed item by item.

This status must not be represented as `M2_PRODUCTION_ACCEPTED`, and it is not evidence that the platform has reached 99% or 99.9% autonomy.

The normative cohort contract is in
[`OBSERVATION_GATE_POLICY.md`](OBSERVATION_GATE_POLICY.md), focused local
evidence is in
[`OBSERVATION_GATE_TEST_RESULTS.md`](OBSERVATION_GATE_TEST_RESULTS.md), and the
additive database rollout is in
[`M2_OBSERVATION_SCHEMA_MIGRATION.md`](M2_OBSERVATION_SCHEMA_MIGRATION.md).

## Invalidated historical Gate baseline

| Historical field | Preserved value |
| --- | --- |
| Final status | `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY` |
| Worker commit SHA | `99d383f7ee72628875a223b3ff2c4f4f05845ec3` |
| WebUI commit SHA | `7bd36c30fb07e393eba71760a164246d267c5b16` |
| Worker container image ID | `sha256:995b6586dd5bd064a7eeba9d3287d2671cd26b042d1823380fb527acf00c99b7` |
| WebUI container image ID | `sha256:4b0e2458b770de20c9fda8af01926a97e8cc5104aff1458c18f79c403abc26a2` |
| Worker source revision | `b8986e794d3cb84bdcc831fbb53d19dfe8275358c37529fe1d9375ccd6e1fd3d` |
| WebUI source revision | `ea2f9e9f4341d5e76d1067fbc113cdcec4d06bb6a07228258b18ad2db300403c` |
| Configuration fingerprint | `sha256:ca8be249cc69fe265e7bc9668959f2bce916f20f6113b6f63b79b9c7dd99163f` |
| Decision schema/version | schema `1`; `m2-source-decision-v1`; `subtitle-source-priority-v1` |
| Gate baseline version | `m2-guardrail-v1:276fdef781528ba2059c114e` |
| Gate start | `2026-09-04T11:00:12.736033Z` |
| Pre-gate attempts | `8`; supplemental history only |

The historical arm operation verified both clean Git identities, running image
IDs and source revisions, the read-only runtime configuration mount, effective
configuration fingerprint, and loaded decision contract. The observation
implementation attached to that baseline did not satisfy the frozen-cohort
contract, so the complete identity and observation record are preserved under
the exact invalidation status above. None of its attempts may be selected into
the replacement Gate.

The pre-closeout Worker SHA `0d989e6fab861d6beef6a06afe0de3657dc8aaaa`
did not contain the runtime guardrail modules and could not report `ARMED`.
It was therefore superseded by the tested code-only runtime baseline above;
it must not be represented as the armed Worker revision.

## Pre-gate server snapshot

Historical bounded snapshot time: 2026-09-04 06:13 (Asia/Taipei). The recorded
queue count remains the supplied pre-gate snapshot; it was not refreshed while
arming the guardrails.

| Observation | Last confirmed status | Evidence | Confidence |
| --- | --- | --- | --- |
| Processing | `1` job | Anonymous job `checkpoint-resume-001` was processing. | High |
| Queued | `6346` jobs | Queue counter snapshot. | High |
| Failed, awaiting retry | `479` jobs | Retry counter snapshot; this is not counted as completed. | High |
| Completed canary | `canary-latest-added-001` completed | Canonical Traditional Chinese ASS was produced and the observed output passed the available parse and final QC checks. This is one canary, not production acceptance. | High |
| Resumed checkpoint | `checkpoint-resume-001` active | The persisted source decision was reused and the selected Japanese audio source was restored without repeating the completed decision stage. | High |
| Hallucination interception | `hallucination-intercept-001` intercepted | A suspected tail hallucination with unresolved prompt-free repair was held in `transcription_review` and was not published as completed. | High |

Media names, source paths, host addresses, ports, and full logs are intentionally excluded from this document.

At historical arm time the read-only exclusion snapshot found `8` running
delivery attempts and `0` running queue rows. Their identities are persisted
only as hashes. Their eventual results may be recorded in supplemental history,
but none can count toward either the invalidated Gate or its replacement.

## Safe concurrency

Worker concurrency remains at the current safe minimum:

```text
max_concurrent_videos = 1
```

The M2 observer must fail validation if canary mode is enabled with a higher
Worker concurrency. No closeout task may increase concurrency automatically.

## Required frozen twenty-job observation gate

The replacement formal Gate cohort must be the first 20 distinct, eligible jobs
claimed by the Worker after the new `gate_start_at`, in durable claim order.
The cohort closes as soon as its twentieth slot is assigned. A failed, review,
quarantined, hallucination-blocked, or otherwise non-strict member keeps its
slot; later successful jobs must never replace it.

- Gate size: `20`
- Gate ID: deployment-time evidence
- Gate start: deployment-time evidence
- Gate baseline: deployment-time evidence
- Worker SHA, image ID, full container ID, in-container identity and runtime-instance fingerprint: deployment-time evidence
- Initial persisted progress: must be verified as `0 / 20` after re-arm and before claim resume
- Claimed after gate start at initialization: must be `0`
- Completed strict-verified at initialization: must be `0`
- Required selection: first 20 eligible post-start claims, with no backfill
- Required completion trigger: all 20 frozen members have reached a terminal state
- Required output: exactly one atomic, machine-readable, sanitized summary for the frozen cohort

Enrollment is part of the Worker claim transaction and executes under SQLite
WAL with `BEGIN IMMEDIATE`. The stable delivery-obligation identity, not an
individual retry attempt, occupies the Gate slot. Database constraints enforce
one ordinal and one stable job per Gate, and the next ordinal can never exceed
20. Duplicate claims reuse the original slot. Jobs started before the new Gate,
claims after slot 20, and claims after settlement are supplemental only.

Observation schema version 3 is installed or migrated atomically. It binds a
new Gate to the host-attested full Worker container ID plus an in-container
identity/runtime-instance fingerprint. A restart of the same container keeps
the binding; recreation is runtime drift and requires revalidation. Historical
version-1/2 Gate rows are preserved without fabricating missing evidence. The
former Gate's canonical runtime manifest and digest preserve its exact hashed
pre-gate snapshot after the live manifest is replaced during re-arm.

Only a job newly claimed after the replacement `gate_start_at` and processed
completely with its immutable baseline may enter the formal cohort. Each member
retains claimed time, final state, strict-verification result, individual
parse/QC/hallucination/source-checksum results, duplicate job and publish
results, and checkpoint/retry/fallback result. A strict-verified output must
satisfy all of the following at the same terminal decision:

1. Final state is `COMPLETED`.
2. Output parse is `PASS`.
3. Hard QC is `PASS`.
4. Hallucination validation is `PASS`.
5. Source checksum is unchanged.
6. There is no duplicate job.
7. There is no duplicate publish.
8. The Decision Record is complete.
9. Stage and checkpoint history is complete.
10. Runtime commit SHA matches the gate baseline.
11. No unresolved retry, quarantine, or fallback state remains.

Each retry or terminal attempt also has an immutable result-event row keyed by
its claim identity. The row stores a canonical bounded payload and its SHA-256.
This preserves earlier OOM, parse-failure or breaker incidents when a later
attempt for the same frozen member succeeds. Before Gate settlement, every
included event payload/digest is revalidated; tampering blocks the twentieth
terminal transaction and summary creation. Only events joined to one of the 20
frozen stable-job identities are included. Pre-gate and slot-21-or-later
supplemental incidents remain recorded but cannot affect frozen counters. Once
`terminal_at` is set, the terminal trigger prevents later updates to every
terminal state, strict/projected result, breaker/reason/strategy/incident field
and evidence digest.

The required automatic summary contract contains `gate_progress`,
`claimed_after_gate_start`, `completed_strict_verified`, `needs_review`,
`failed`, `quarantined`, `hallucination_blocked`, `output_parse_failures`,
`source_mutation_incidents`, `duplicate_jobs`, `duplicate_publishes`,
`breaker_trips`, `checkpoint_resumes`, `oom_events`, and
`processing_strategy_counts`. Every counter and strategy count was initialized
to zero at Gate creation. Frozen-member outcome counters come from member rows;
retry/incident counters also include the immutable result-event journal.
Terminal evidence and the summary payload are written to the same durable
database before publication. The summary outbox publishes an atomic file and
records emission; concurrent or restarted publishers cannot logically emit a
second report. The frozen-cohort report may be emitted only after all 20 fixed
members are terminal. A breaker trip independently stops admission and persists
bounded reason/evidence.

### Repair status and fail-closed behavior

The historical observer failed the no-backfill and complete per-member record
requirements. That Gate therefore has the final status
`INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`; its preserved `0/20` is not a
candidate for repair, continuation, or retroactive enrollment.

The repository candidate implements the replacement frozen cohort in the
existing WAL scanner-state database. Focused local coverage contains 24 core
frozen-cohort/schema/event-journal tests and 11 recovery/replay tests. The seven
isolated breaker classes also pass locally. The repaired Worker image still requires bounded
server deployment, fresh 7/7 breaker execution, runtime identity validation,
new Gate creation at `0/20`, and claim resume. Until those deployment-time
steps pass, this document does not claim that Production automation is ready.

If Worker/WebUI/runtime image or instance identity, configuration fingerprint,
Decision schema, eligibility policy, Gate identity, or Gate start identity
changes during the replacement Gate, claim admission atomically records
expected and actual evidence and marks the Gate
`INVALIDATED_BY_RUNTIME_CHANGE`. New claims then stop; existing queue rows,
running work, checkpoints, and recorded members remain intact. A restart with
the same exact baseline reopens the same Gate and next ordinal without
invalidating it.

The summary may contain counters, stage/error/reason codes, breaker state, and the observation window. It must not contain media titles, source paths, server addresses, ports, credentials, prompts, transcripts, subtitles, or raw exception/log text.

Full operational logs remain on the server under the configured log retention policy. Successful work must emit only the bounded gate summary to the observation channel; full logs must not be copied into a conversation.

## Automatic circuit breaker

The pre-repair runtime state is `ARMED`, with no confirmed trip at historical
initialization. That running Worker image executed the isolated fault suite
after its container start and passed all seven required breaker classes. These
records remain valid historical breaker evidence; the repaired image must run
a fresh timestamped 7/7 suite before replacement Gate creation.

- Validation run: `m2-guardrail-fi-20260904T105817974649Z-b7176039`
- Validation window: `2026-09-04T10:58:17.974636Z` to `2026-09-04T10:58:18.758425Z`
- Result digest: `sha256:0b5f747c66eb514a1927daf33cec985c8235f7f27f73572a838ad838b8bb4c90`
- Full event-log digest: `sha256:371c300b231fdc42efa0203533d7473d07344eebd236d973839770abe10e674a`
- Breaker tests: `7 / 7 PASS`
- Production source/output affected by fault injection: **No**

The complete `events.jsonl` and `result.json` remain in the timestamped server
validation run; only this bounded evidence is recorded here.

| Trigger | Trip rule |
| --- | --- |
| Source mutation | Trip immediately when a source mutation is detected. |
| Duplicate publish | Trip immediately when a duplicate formal publication is detected. |
| Output parse failure | Trip immediately when a formal output cannot be parsed. |
| Incorrect completion | Trip immediately when an invalid result is marked completed. |
| Repeated OOM | Trip after the configured bounded repeat threshold; current closeout setting is `3`. |
| Repeated identical stage failure | Trip after the same stage and failure signature reaches the configured bounded threshold; current closeout setting is `3`. |
| Insufficient disk space | Trip before claiming a new job when configured free-space reserve is not met. |

Circuit-breaker semantics are fail-closed for **new claims only**:

1. Stop claiming new jobs once the breaker is open.
2. Allow the already-running job to finish or reach its normal recoverable checkpoint.
3. Preserve all completed stages, checkpoint state, retry evidence, source inputs, and existing outputs.
4. Do not delete, rewrite, or invalidate checkpoints as a breaker side effect.
5. Keep the breaker latched until an operator verifies the cause and performs an explicit recovery action.

## Items not yet verified

- Terminal results of the eight pre-gate running attempts; this goal did not wait for them and they remain excluded from the formal gate.
- New Worker runtime SHA and container identity after the repaired image is deployed.
- Fresh server-image results for the 24 core observation tests, 11 recovery/replay tests, 7 isolated breaker cases, and related M1/M2 regression.
- Replacement Gate ID, baseline, start timestamp, and bounded initial `0/20` status.
- Real Production execution of the replacement first-20 cohort; this work must not wait for it.
- Exactly-once Production summary publication after those same fixed 20 members are terminal.
- A real Production runtime-drift event; isolated drift invalidation and same-baseline restart are locally verified.
- Live production trip and controlled runtime reload/recovery after an actual breaker cause is removed. Isolated safe recovery is verified.
- Full M2 release-acceptance corpus of at least 100 eligible inputs.
- Separate restart, Docker restart, model crash/timeout, OOM, partial-stage, duplicate-event, and temporary-output fault-injection gates.
- Rolling 500-job production SLO evidence.
- Any measured 99% or 99.9% autonomy claim.
- Final disposition of the full queued backlog; this closeout intentionally does not wait for or monitor it job by job.
