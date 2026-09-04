# M2 Production Observation

## Status

- Canary state: `M2_SERVER_CANARY_ACTIVE`
- Production acceptance: **not accepted**
- Scope: M2 server canary closeout only. This observation does not start M3.
- Evidence boundary: values below are the last confirmed snapshot. The queue is not being continuously polled or observed item by item.

This status must not be represented as `M2_PRODUCTION_ACCEPTED`, and it is not evidence that the platform has reached 99% or 99.9% autonomy.

## Deployed revisions

| Component | Deployed commit SHA |
| --- | --- |
| Worker | `0d989e6fab861d6beef6a06afe0de3657dc8aaaa` |
| WebUI | `7bd36c30fb07e393eba71760a164246d267c5b16` |

These are deployment identities, not release-acceptance results.

## Last confirmed server snapshot

Snapshot time: 2026-09-04 06:13 (Asia/Taipei).

| Observation | Last confirmed status | Evidence | Confidence |
| --- | --- | --- | --- |
| Processing | `1` job | Anonymous job `checkpoint-resume-001` was processing. | High |
| Queued | `6346` jobs | Queue counter snapshot. | High |
| Failed, awaiting retry | `479` jobs | Retry counter snapshot; this is not counted as completed. | High |
| Completed canary | `canary-latest-added-001` completed | Canonical Traditional Chinese ASS was produced and the observed output passed the available parse and final QC checks. This is one canary, not production acceptance. | High |
| Resumed checkpoint | `checkpoint-resume-001` active | The persisted source decision was reused and the selected Japanese audio source was restored without repeating the completed decision stage. | High |
| Hallucination interception | `hallucination-intercept-001` intercepted | A suspected tail hallucination with unresolved prompt-free repair was held in `transcription_review` and was not published as completed. | High |

Media names, source paths, host addresses, ports, and full logs are intentionally excluded from this document.

## Safe concurrency

Worker concurrency remains at the current safe minimum:

```text
max_concurrent_videos = 1
```

The M2 canary observer must fail validation if canary mode is enabled with a higher Worker concurrency. No closeout task may increase concurrency automatically.

## Twenty-output observation gate

The machine-generated observation gate is based on **strictly verified completed outputs**, not queue claims, retries, skipped jobs, or Worker success without verified publication evidence.

- Gate size: `20`
- Current persisted gate baseline: `0 / 20` newly observed strict completions
- Next gate: `20` strict verified completed outputs
- Later gates: `40`, `60`, `80`, and so on
- Output: one atomic, machine-readable, sanitized aggregate summary per completed gate

The summary may contain counters, stage/error/reason codes, breaker state, and the observation window. It must not contain media titles, source paths, server addresses, ports, credentials, prompts, transcripts, subtitles, or raw exception/log text.

Full operational logs remain on the server under the configured log retention policy. Successful work must emit only the bounded gate summary to the observation channel; full logs must not be copied into a conversation.

## Automatic circuit breaker

Canary target state: `ARMED`, with no confirmed trip in this snapshot. Runtime activation and every trigger path remain subject to the verification items below.

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

- Terminal result and output validity of the currently processing anonymous job.
- The first machine-generated 20-output observation gate.
- Live fault-injection proof for each of the seven circuit-breaker trigger classes.
- Automatic recovery after the breaker cause is removed and claims are explicitly resumed.
- Full M2 release-acceptance corpus of at least 100 eligible inputs.
- Separate restart, Docker restart, model crash/timeout, OOM, partial-stage, duplicate-event, and temporary-output fault-injection gates.
- Rolling 500-job production SLO evidence.
- Any measured 99% or 99.9% autonomy claim.
- Final disposition of the full queued backlog; this closeout intentionally does not wait for or monitor it job by job.
