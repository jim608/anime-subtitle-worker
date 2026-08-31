# M1 Test Results

Status: **completed locally**. This file records only commands actually executed and observed results; it does not claim deployment.

## Pre-change baseline

- Command: `python -m unittest test_event_watcher test_scanner_state_recovery test_scanner_state_auto_recovery test_scan_state test_scanner`
- Result: **PASS — 140 tests, 5.211 seconds**.

## Required M1 acceptance matrix

- Command: `rtk python -m unittest test_m1_acceptance`
- Observed result: **PASS — 12 tests, 1.380 seconds**.

| # | Acceptance case | Direct coverage | Status |
|---:|---|---|---|
| 1 | One complete Watch Folder video creates one Job | `test_01_one_complete_watch_video_creates_one_job` | Pass |
| 2 | Duplicate filesystem events do not create another Job | `test_02_duplicate_filesystem_events_keep_one_job` | Pass |
| 3 | Incomplete write is not analyzed | `test_03_incomplete_write_is_not_analyzed` | Pass |
| 4 | Stable file starts automatically | `test_04_stable_file_starts_automatically` | Pass |
| 5 | Crash at each test Stage resumes | `test_05_crash_at_every_formal_stage_resumes` | Pass |
| 6 | Durable-volume reopen (Docker restart semantics) resumes unfinished Job | `test_06_durable_volume_reopen_resumes_unfinished_job` | Pass |
| 7 | Completed valid Stage is not rerun | `test_07_valid_completed_stage_is_reused_not_rerun` | Pass |
| 8 | Temporary output cannot mark Job completed | `test_08_temporary_output_cannot_complete_job` | Pass |
| 9 | Source checksum is unchanged | `test_09_source_checksum_is_unchanged` | Pass |
| 10 | Idle Watcher does not repeatedly recurse the full media tree | `test_10_idle_watchers_do_not_repeat_full_recursive_walks` | Pass |
| 11 | Existing normal subtitle Pipeline still completes | `test_11_existing_normal_subtitle_pipeline_completes` | Pass |
| 12 | Transitions and retries are auditable in logs and Job Store | `test_12_transitions_and_retries_are_auditable_in_db_and_jsonl` | Pass |

## Focused combined regression

- Command: `rtk python -m unittest test_m1_acceptance test_pipeline_state test_event_watcher test_config test_scanner test_mikan_worker test_main_queue test_worker test_source_integrity test_pipeline_event_log test_scan_state test_scanner_state_recovery test_scanner_state_auto_recovery test_source_priority test_completed_delivery`
- Observed result: **PASS — 680 tests, 27.798 seconds**.

## Restart and static checks

- Command: `rtk python verify_m1_docker_restart.py --no-pull`
- Observed result: **PASS**. This exercised the local Docker restart/resume harness with the existing image and persistent state; it is not an UNRAID deployment result.
- Changed-file `rtk python -m py_compile ...` check: **PASS**.
- Scoped `rtk git ... diff --check` check: **PASS**.

## Evidence boundary

These results establish local M0/M1 implementation and focused regression coverage only. They do **not** provide:

- the required 100 previously untouched Eligible Inputs;
- a mature rolling window of 500 Eligible Jobs;
- measured 99% or 99.9% autonomy;
- production proof of zero false completion, source data loss or duplicate publication; or
- an UNRAID deployment or production soak result.

The release and Production SLO gates in `docs/PROJECT_VISION.md` remain separate work.
