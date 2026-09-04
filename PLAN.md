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
- [x] Run the complete existing repository regression suite against the combined M2 diff: 1,587 tests passed with one existing conditional skip.
- [x] Complete the M2 design, source-selection, Decision-schema and confidence-policy documents.
- [x] Run final changed-file compile, diff checks and release review with no private deployment details in tracked files.

M2 implementation is deployed under the `M2_SERVER_CANARY_ACTIVE` observation
state documented in [`docs/M2_PRODUCTION_OBSERVATION.md`](docs/M2_PRODUCTION_OBSERVATION.md).
It is not `M2_PRODUCTION_ACCEPTED`; the 20-output observation gate, 100-input
release gate, rolling-500 Production SLO, and any measured 99%/99.9% autonomy
claim remain unverified.

Translation-quality improvements, Translation Memory, full QC redesign, WebUI redesign and M3 model-fallback work remain out of scope for this milestone.
