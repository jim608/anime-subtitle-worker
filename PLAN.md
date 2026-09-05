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

- [ ] Finish the reproduced M2 recovery blockers: bounded cold/source scheduling,
  evidence-backed season scopes, full official-subtitle admission and staged hard
  QC. Complete combined server tests, safe deployment and guarded runtime handoff.
- [ ] Complete bounded real-case closeout with separate download/extraction/formal
  publication and AI route/completion evidence. Candidate mapping proves 83 local
  bindings; Amaburi E12 currently fails hard QC and is not a new subtitle success.

- [x] Perform bounded acceptance on the exact four unsuccessful and five old-completed download obligations without redeployment/Gate recreation: reverify four existing formal completions, retain one 6-versus-6.5 identity review, and preserve four failed-source/insufficient-subtitle cases without a false new-publication count.
- [x] Prove two subsequent M2 recovery claims, SUBTITLE_DETECTION execution/heartbeats, safe local disposition and continued automatic claim; preserve the actual database-contention retry and source-quality review evidence separately from success.
- [x] Freeze and document routing for the existing 1,227 blocked keys: 356 source-backoff and 871 unresolved identity/mapping/index records; zero newly proven ready-for-alternative or AI-fallback obligations in that blocked subset. Keep the separate 887 historical replacement targets and all recovery metrics outside Gate backfill.
- [ ] Prove a different subsequent download/extraction claim and genuinely new formal subtitle publication: current source-enqueue cycle is still processing, and the four bounded missing-valid-subtitle cases have no permitted untried source. Preserve normal waiting, failed hashes, QC and existing formal outputs.

- [x] Audit download/extraction/import history separately from AI recovery; verify qB authentication and actual mount parity with bounded server evidence.
- [x] Repair four-digit episode parsing, partial queuedDL timeout handling, piece preservation, pre-publication subtitle validation and durable bounded replacement/backoff; pass 283 server candidate tests.
- [x] Safely deploy the M2 download extension and recovery closeout as Worker `b911794ed0ec872cb475f714e1385e20e8ac4388`; pass 1,737 Worker and 229 WebUI tests, fresh 7/7 breakers, and attest ARMED without changing QC/models/configuration.
- [x] Apply all 2,120 deduplicated historical decisions using existing state/locks; enqueue 887 bounded replacement targets, preserve one existing download, and record real download/extraction/matching/isolated-import PASS with safe Production no-op, not a false new-import claim.
- [x] Repair the controlled DISARMED Gate-publication race and orphan pre-claim canary reconciliation; preserve strict invalidation, single-canary limits, retry budgets and local-failure isolation. Pass 74 focused integration tests.
- [x] Preserve superseded Gates and initialize `m2-gate-20260905T045640981079Z-08147de925` at `2026-09-05T04:56:40.981079Z`, baseline `m2-guardrail-v1:5b4d2a88f2d5c0c5749f6747`, initial `0/20`; resume claims without waiting for the Gate.
- [x] Record bounded server continuation evidence: 12 download targets consumed, eight existing outputs verified, and an independently dispatched next AI canary after one pre-claim exclusion; retain its queued/not-yet-claimed boundary.
- [ ] Verify a genuinely new Production subtitle import when an eligible external source is available; the representative target already had valid outputs, and missing-output sample 304:10 had no eligible untried source. Do not redownload failed hashes or relax validation to close this item.

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
- [x] Make controlled recovery restart-safe by durably pausing claims before Gate initialization, validating a pending recovery across deployment, and resuming claims only after an `ARMED` matching baseline; pass the complete 1,721-test regression with one conditional skip.
- [x] While claims were paused, safely deploy the repaired Worker, preserve the exact former-Gate invalidation, run a fresh seven-breaker suite in the new server image, and verify the live runtime is `ARMED`.
- [x] Create deployment-bound Gate `m2-gate-20260905T020845085531Z-7d0c5c7333` with baseline `m2-guardrail-v1:0180b8779ee97524bf0150d2` at `2026-09-05T02:08:45.085531Z` and `0/20`, then resume normal claims and dispatch only one recovery canary without waiting for either workflow to finish.

The former `0/20` observation baseline is not a valid Gate and has the final
status `INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY`. Its eight pre-gate
attempts remain supplemental records and cannot be enrolled retroactively.
The replacement frozen-cohort implementation is deployed at Worker runtime
`d9dfcd01aa9ebeffe65c8367f4e1bbace56d5bcc`; the fresh server-image breaker
suite passed 7/7 and the bounded Production recovery reported no source-media
or formal-output change. The new frozen Gate is active at `0/20`, but this plan
does not claim any member result or wait for it. M2 is not
`M2_PRODUCTION_ACCEPTED`; the 20-job Gate, 100-input release gate, rolling-500
Production SLO, recovery-canary result, and any measured 99%/99.9% autonomy
claim remain unverified.

Translation-quality improvements, Translation Memory, full QC redesign, WebUI redesign and M3 model-fallback work remain out of scope for this milestone.
