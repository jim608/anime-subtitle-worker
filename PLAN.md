# Incremental Delivery Plan

## M0 — Baseline and low-I/O ingestion

- [x] Audit current Watcher, task creation, stages, temporary/final outputs, error paths, Docker and configuration.
- [x] Record focused regression baseline.
- [x] Identify recursive disk-I/O sources.
- [x] Make filesystem events the normal ingestion authority.
- [x] Persist discovery/stability observations across restart.
- [x] Reject temporary/incomplete files and require a write-complete gate.
- [x] Keep startup reconciliation and low-frequency bounded fallback only.
- [x] Stop idle AI and Mikan loops from repeatedly walking the whole library.

## M1 — Durable resumable pipeline state

- [x] Add stable Job/media revision identity to the existing WAL SQLite store.
- [x] Enforce the documented states and auditable transitions.
- [x] Persist per-stage attempts, inputs, outputs, model, timeout, retry and checkpoint.
- [x] Recover interrupted work from the last valid checkpoint.
- [x] Preserve existing queue/WebUI and subtitle pipeline compatibility.
- [x] Keep source video immutable and publish verified staged outputs atomically.
- [x] Pass all M1 acceptance and focused regression tests.
- [x] Complete architecture, design, state-machine, recovery and test-result documents.

M0/M1 implementation and acceptance are complete locally. These checkboxes do not claim an UNRAID deployment, the 100-input release gate, the rolling-500 Production SLO, or a measured 99%/99.9% autonomy rate.

M2 translation quality, Translation Memory, full QC redesign and WebUI work are explicitly out of scope until every M0/M1 checkbox is complete.

## M2 — Deterministic source analysis and durable decisions

- [x] Inventory supported subtitle sidecars, embedded subtitle streams and audio streams without modifying source media.
- [x] Normalize language metadata and combine it with subtitle content, script and quality evidence.
- [x] Score completeness, timing, empty-content, forced, signs-only, songs-only and commentary risks.
- [x] Apply deterministic candidate ordering and all seven formal M2 strategies.
- [x] Persist immutable Decision Records with candidates, scores, reason codes, evidence, versions and source context.
- [x] Bind Decisions to verified Stage checkpoints with idempotent restart reuse and exact-context invalidation.
- [x] Revalidate selected subtitle/audio sources before routing them into the existing worker.
- [x] Materialize embedded subtitles through validated temporary files and atomic cache publication.
- [x] Keep Whisper and other speech models outside the M2 decision stage.
- [x] Pass the dedicated 22-case M2 acceptance suite locally, including the nested M1 twelve-case gate and 106-case M2 integration gate.
- [x] Cover all ten required representative source-selection fixture classes.
- [x] Record focused local results and evidence boundaries in `docs/TEST_RESULTS_M2.md`.
- [x] Run the complete existing repository regression suite against the combined M2 diff: 1,649 tests run, OK with one conditional skip.
- [x] Complete the M2 design, source-selection, Decision-schema and confidence-policy documents.
- [x] Run final changed-file compile, diff checks and release review with no private deployment details in tracked files.
- [x] Verify the deployed Worker/WebUI runtime baseline and loaded guardrail configuration, pass all seven isolated breaker cases in the server image, and arm `M2_GUARDRAILS_ARMED`.
- [x] Implement exact preservation/import for the former observation baseline `m2-guardrail-v1:276fdef781528ba2059c114e`, started at `2026-09-04T11:00:12.736033Z`, with required final status `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`; keep its eight pre-gate attempts as supplemental history only.
- [x] Implement the frozen first-20 claim cohort with claim-time `BEGIN IMMEDIATE`, no success backfill, immutable ordinals, stable-job uniqueness, WAL persistence, and restart-safe continuation.
- [x] Persist the complete required terminal record for every frozen member, retain immutable per-attempt incident events, and journal one summary/outbox record only after all 20 members are terminal.
- [x] Durably invalidate an active gate as `INVALIDATED_BY_RUNTIME_CHANGE`, with expected/actual evidence and no mixed-baseline admission.
- [x] Add the atomic observation schema-v2 migration, including container-instance identity and canonical result-event payload/digest verification, while preserving version-1 Gate history.
- [x] Pass the focused repository candidate suites: 24 frozen-cohort/schema/event-journal cases, 11 recovery/replay cases, and the isolated 7-breaker harness.
- [x] Isolate the live `repeated_identical_stage_failure` cause to three distinct `source_selection_needs_review` outcomes that the pipeline correctly held in `NEEDS_REVIEW` but the legacy queue adapter mislabeled as retryable failures.
- [x] Add the six-way failure taxonomy, version-aware historical reconciliation, compatible-checkpoint/minimum-safe-stage decisions, bounded no-progress budget, and a restart-persistent single-canary recovery lane using indexed Job Store state only.
- [x] Make repeated OOM/identical-stage streaks distinct-job aware and replay-idempotent, while keeping three distinct systemic failures fail-closed.
- [x] Add the evidence-bound `m2_guardrail_runtime.py recover` flow: preserve trip evidence, verify runtime/queue/checkpoint/source/output identities, invalidate the old Gate, journal recovery, and require a new attested `0/20` Gate before claims resume.
- [x] Pass the 2026-09-05 recovery candidate regression: 1,720 tests run, OK with one conditional skip.
- [ ] While claims are paused, deploy this repository candidate, apply the exact former-Gate invalidation, rerun the focused regression and seven-breaker suite in the new server image, and verify the live runtime remains `ARMED`.
- [ ] Create a new deployment-bound Gate ID/baseline/start timestamp at `0/20`, then resume claims without waiting for the cohort to finish.

The former `0/20` observation baseline is not a valid Gate and has the final
status `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`. Its eight pre-gate
attempts remain supplemental records and cannot be enrolled retroactively.
The replacement frozen-cohort implementation is locally tested, but its live
Worker SHA, image identity, Gate ID, baseline and start time remain
deployment-time evidence. No new Production Gate is represented as started by
this plan. M2 is not `M2_PRODUCTION_ACCEPTED`; the 20-job Gate, 100-input
release gate, rolling-500 Production SLO, and any measured 99%/99.9% autonomy
claim remain unverified.

Translation-quality improvements, Translation Memory, full QC redesign, WebUI redesign and M3 model-fallback work remain out of scope for this milestone.
