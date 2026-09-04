# M2 Test Results

Status: **local M2 acceptance and the complete repository regression passed; external release and Production SLO gates remain open**. This document records local results only and does not claim production deployment or a measured autonomy rate.

## Required M2 acceptance matrix

- Command: `rtk python -m unittest test_m2_acceptance`
- Latest observed result: **PASS — 22 tests, 4.117 seconds**.
- Acceptance case 21 ran the existing M1 acceptance suite as a nested gate: **PASS — 12 tests**.
- Acceptance case 22 ran the inventory, adapter, orchestration, persistence, fixture, Worker-integration, analyzer and configuration suites as a nested gate: **PASS — 106 tests**.

| # | Acceptance case | Direct coverage | Status |
|---:|---|---|---|
| 1 | Complete zh-TW selects `USE_EXISTING_ZH_TW` | `test_01_complete_zh_tw_selects_existing` | Pass |
| 2 | Complete zh-CN selects `CONVERT_ZH_CN` | `test_02_complete_zh_cn_selects_conversion` | Pass |
| 3 | Complete Japanese subtitle selects `TRANSLATE_JA_SUBTITLE` | `test_03_complete_japanese_subtitle_selects_translation` | Pass |
| 4 | Japanese audio without usable subtitle selects `ASR_JA_AUDIO` | `test_04_japanese_audio_without_subtitle_selects_asr` | Pass |
| 5 | zh-TW takes priority over Japanese subtitle | `test_05_zh_tw_precedes_japanese_subtitle` | Pass |
| 6 | zh-CN takes priority over Japanese subtitle | `test_06_zh_cn_precedes_japanese_subtitle` | Pass |
| 7 | Forced Chinese cannot override complete Japanese dialogue | `test_07_forced_chinese_cannot_override_complete_japanese` | Pass |
| 8 | Signs-only subtitle is not accepted as complete Chinese | `test_08_signs_only_is_not_complete_chinese` | Pass |
| 9 | Default flag cannot outrank materially better quality | `test_09_default_flag_cannot_beat_complete_subtitle` | Pass |
| 10 | Metadata/content language conflict is detected | `test_10_metadata_content_language_conflict_is_detected` | Pass |
| 11 | Missing metadata falls back to content evidence | `test_11_missing_metadata_uses_content_language` | Pass |
| 12 | Language aliases and Chinese variants are normalized | `test_12_language_tags_and_chinese_variants_are_normalized` | Pass |
| 13 | Close candidates execute additional checks | `test_13_close_candidates_execute_additional_checks` | Pass |
| 14 | Confidence below 0.60 enters `NEEDS_REVIEW` | `test_14_low_confidence_source_needs_review` | Pass — asserts `< 0.60` |
| 15 | No supported source enters `UNSUPPORTED` | `test_15_no_supported_source_is_unsupported` | Pass |
| 16 | Decision Record persists candidates, scores, reason and evidence | `test_16_decision_record_persists_candidates_reasons_and_evidence` | Pass — full payload verified after reopen |
| 17 | Restart reuses and rebinds the valid immutable Decision checkpoint | `test_17_restart_reuses_valid_decision_checkpoint` | Pass |
| 18 | Real same-size/same-mtime sidecar mutation invalidates the old Decision | `test_18_changed_real_sidecar_fingerprint_invalidates_old_decision` | Pass |
| 19 | Identical inputs produce deterministic strategy and ordering | `test_19_same_input_is_deterministic` | Pass |
| 20 | The M2 decision stage neither imports nor invokes Whisper | `test_20_decision_stage_never_imports_or_invokes_whisper` | Pass — static and runtime import guard |
| 21 | Existing M1 twelve-case acceptance remains green | `test_21_m1_twelve_case_acceptance_remains_green` | Pass — nested 12/12 |
| 22 | Existing focused M2 integration remains green | `test_22_existing_m2_integration_regression_remains_green` | Pass — nested 106/106 |

## M2 integration gate

- Command: `rtk python -m unittest test_source_inventory test_source_decision_adapter test_source_analysis_service test_pipeline_source_decision test_m2_fixtures test_m2_worker_integration test_source_analyzer test_config_source_analyzer`
- Latest observed result through acceptance case 22: **PASS — 106 tests**.
- Composition:
  - source inventory and materialization: 15 tests;
  - persisted-decision adapter and executable-source revalidation: 6 tests;
  - source-analysis orchestration and restart reuse: 8 tests;
  - schema migration, immutable persistence and checkpoint integrity: 22 tests.
  - representative fixture matrix: 1 test;
  - Worker integration: 5 tests;
  - deterministic analyzer: 41 tests;
  - analyzer configuration: 8 tests.

The committed fixture matrix covers all ten representative classes required by `docs/PROJECT_GOAL.md`: complete Traditional Chinese, complete Simplified Chinese, complete Japanese, mixed Chinese/Japanese subtitles, forced/signs-only, incorrect metadata, missing metadata, multiple Japanese audio tracks, no usable source, and damaged timeline/content.

## Complete repository regression

- Command: `rtk python -m unittest discover -v`
- Latest observed result: **PASS — 1,587 tests, 98.093 seconds; OK with 1 skipped test**.
- The skipped test remains governed by its existing condition and is not counted as evidence for that unavailable condition.

## Static and diff checks

- Changed-file `rtk python -m py_compile ...`: **PASS after the final code and test edits**.
- `rtk git diff --check`: **PASS after the final code, test and documentation edits**.
- The release review found no private deployment address, path, port, credential or media content in the M2 additions.

## Additional adversarial evidence

A temporary two-connection SQLite concurrency probe was also executed against the real persistence API. Concurrent `persist_source_decision` and `finish_stage_attempt` calls produced one successful writer and one transient SQLite lock. After rollback and bounded replay, the losing caller reused the same Decision and final Stage result. The database retained one Decision, one persistence event and one finish event; the Decision checkpoint was not overwritten. This probe did not modify a source file, but it is not yet a committed regression test.

The same review confirmed that opening the store with SQLite foreign keys disabled inside an active caller transaction fails closed. The caller transaction remained under caller control, rollback removed its uncommitted row, and no pipeline schema object was created.

## Gates still open

- Docker restart, server restart and live deployment have not been demonstrated by these local tests.
- No untouched 100-input Eligible corpus or independent fault-injection release gate has been completed for M2.
- No mature rolling window of 500 Eligible Jobs has been measured.

## Evidence boundary

These results demonstrate deterministic local source selection, durable Decision persistence, restart/reuse behavior, the representative fixture matrix, preservation of the M1 acceptance contract and a green complete local regression run. They do **not** establish:

- 99% or 99.9% autonomy;
- zero false completion, source data loss or duplicate publication in production;
- production performance on an UNRAID host or the target GPU; or
- completion of the release and Production SLO gates in `docs/PROJECT_VISION.md`.
