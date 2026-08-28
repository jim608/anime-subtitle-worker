from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import ANY, Mock, call, patch

import main as main_module
from scan_state import ScanStateStore


class MainQueueResultTest(unittest.TestCase):
    def test_scan_and_process_reports_scanner_database_failure_instead_of_idle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir) / "work"
            work.mkdir()
            scanner = Mock()
            scanner.queued_candidates.return_value = []
            scanner.last_database_error = "disk I/O error"
            scanner.last_database_error_code = "scanner_database_disk_io"
            worker = Mock()
            worker.config = SimpleNamespace(
                work_path=work,
                max_concurrent_videos=1,
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
            )

            processed = main_module._scan_and_process(
                scanner,
                worker,
                Mock(),
                queue_only=True,
            )

            self.assertEqual(processed, 0)
            worker.process.assert_not_called()
            state = json.loads((work / "ai_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "error")
            self.assertEqual(state["reason_code"], "scanner_database_disk_io")
            self.assertGreater(state["next_retry_at"], time.time())
            self.assertEqual(state["consecutive_errors"], 1)

    def test_successful_empty_scan_clears_scheduler_database_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir) / "work"
            work.mkdir()
            worker = Mock()
            worker.config = SimpleNamespace(
                work_path=work,
                max_concurrent_videos=1,
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
            )
            failed_scanner = Mock()
            failed_scanner.queued_candidates.return_value = []
            failed_scanner.last_database_error = "database is locked"
            failed_scanner.last_database_error_code = "scanner_database_busy"
            main_module._scan_and_process(failed_scanner, worker, Mock(), queue_only=True)

            healthy_scanner = Mock()
            healthy_scanner.queued_candidates.return_value = []
            healthy_scanner.last_database_error = ""
            healthy_scanner.last_database_error_code = ""
            main_module._scan_and_process(healthy_scanner, worker, Mock(), queue_only=True)

            state = json.loads((work / "ai_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["state"], "idle")
            self.assertEqual(state["reason_code"], "")
            self.assertEqual(state["consecutive_errors"], 0)
            self.assertEqual(state["next_retry_at"], 0.0)
            self.assertGreater(state["last_success_at"], 0)

    def test_scheduler_retry_command_wakes_waiting_auto_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(work_path=Path(temp_dir))
            main_module.AI_SCHEDULER_WAKE_EVENT.clear()

            result = main_module._execute_control_command(
                config,
                Mock(),
                "system.ai_scheduler_retry",
                "",
                {},
            )

            self.assertTrue(result["applied"])
            self.assertTrue(main_module.AI_SCHEDULER_WAKE_EVENT.is_set())
            state = json.loads((Path(temp_dir) / "ai_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertGreater(state["retry_requested_at"], 0)
            main_module.AI_SCHEDULER_WAKE_EVENT.clear()

    def test_expired_scheduler_retry_does_not_spin_the_next_cycle(self) -> None:
        shutdown_event = Mock()
        shutdown_event.wait.return_value = True
        main_module.AI_SCHEDULER_WAKE_EVENT.clear()

        with patch.object(
            main_module,
            "ai_scheduler_next_retry_at",
            return_value=time.time() - 1,
        ):
            stopped = main_module._wait_for_next_cycle_or_ai_resume(
                shutdown_event,
                300,
                SimpleNamespace(),
            )

        self.assertTrue(stopped)
        shutdown_event.wait.assert_called_once_with(2.0)

    def test_future_scheduler_retry_still_shortens_the_wait(self) -> None:
        shutdown_event = Mock()
        shutdown_event.wait.return_value = False
        main_module.AI_SCHEDULER_WAKE_EVENT.clear()

        with (
            patch.object(main_module, "ai_scheduler_next_retry_at", return_value=110.0),
            patch.object(main_module.time, "time", return_value=100.0),
            patch.object(main_module.time, "monotonic", side_effect=[200.0, 200.0, 210.0]),
            patch.object(main_module, "_ai_queue_paused", return_value=False),
            patch.object(main_module, "_deployment_hold_active", return_value=False),
        ):
            stopped = main_module._wait_for_next_cycle_or_ai_resume(
                shutdown_event,
                300,
                SimpleNamespace(),
            )

        self.assertFalse(stopped)
        shutdown_event.wait.assert_called_once_with(2.0)

    def test_control_command_loop_rechecks_stale_rows_after_fast_restart(self) -> None:
        config = SimpleNamespace()
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.side_effect = [False, False, True]
        shutdown_event.wait.return_value = False

        with (
            patch("control_state.reconcile_stale_running_commands") as reconcile,
            patch("control_state.ingest_command_inbox", return_value=0),
            patch("control_state.claim_next_command", return_value=None),
            patch.object(main_module, "_deployment_hold_active", return_value=True),
            patch.object(main_module.time, "monotonic", side_effect=[100.0, 120.0, 161.0]),
        ):
            main_module._background_control_command_loop(config, logger, shutdown_event)

        self.assertEqual(reconcile.call_count, 2)

    def test_control_command_loop_retries_reconciliation_after_transient_error(self) -> None:
        config = SimpleNamespace()
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.side_effect = [False, False, True]
        shutdown_event.wait.return_value = False

        with (
            patch(
                "control_state.reconcile_stale_running_commands",
                side_effect=[sqlite3.OperationalError("database is locked"), 0],
            ) as reconcile,
            patch("control_state.ingest_command_inbox", return_value=0),
            patch("control_state.claim_next_command", return_value=None),
            patch.object(main_module, "_deployment_hold_active", return_value=True),
            patch.object(main_module.time, "monotonic", side_effect=[100.0, 120.0, 161.0]),
        ):
            main_module._background_control_command_loop(config, logger, shutdown_event)

        self.assertEqual(reconcile.call_count, 2)
        logger.error.assert_called_once()

    def test_mark_queue_running_retries_transient_database_lock(self) -> None:
        state = Mock()
        state.mark_ai_queue_running.side_effect = [sqlite3.OperationalError("database is locked"), None]

        with patch("main.time.sleep") as sleep:
            main_module._mark_queue_running(state, Path("/anime/Series S01E01.mkv"))

        self.assertEqual(state.mark_ai_queue_running.call_count, 2)
        state.rollback.assert_called_once_with()
        state.commit.assert_called_once_with()
        sleep.assert_called_once_with(0.1)

    def test_mark_queue_running_retries_transient_disk_io_error(self) -> None:
        state = Mock()
        state.mark_ai_queue_running.side_effect = [sqlite3.OperationalError("disk I/O error"), None]

        with patch("main.time.sleep") as sleep:
            main_module._mark_queue_running(state, Path("/anime/Series S01E01.mkv"))

        self.assertEqual(state.mark_ai_queue_running.call_count, 2)
        state.rollback.assert_called_once_with()
        state.commit.assert_called_once_with()
        sleep.assert_called_once_with(0.1)

    def test_delivery_attempt_succeeds_only_with_strict_current_manifest(self) -> None:
        from output_manifest import write_output_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode 01.mkv"
            outputs = [
                root / "Episode 01.AI.ja.ass",
                root / "Episode 01.AI.zh-CN.ass",
                root / "Episode 01.AI.zh-TW.ass",
            ]
            video.write_bytes(b"media")
            for output in outputs:
                output.write_text("verified subtitle", encoding="utf-8")
            config = SimpleNamespace(
                work_path=root,
                scanner_state_path=root / "scanner_state.sqlite3",
                ai_output_manifest_path="manifests",
                control_state_path="control.sqlite3",
                auto_ai_failure_cooldown_seconds=0,
                auto_ai_max_attempts=3,
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
                attempt_id = main_module._mark_queue_running(state, video, config)
                write_output_manifest(
                    video,
                    config,
                    outputs,
                    publication_kind="translated_trilingual",
                    output_languages=("ja", "zh-CN", "zh-TW"),
                )

                main_module._mark_queue_result(
                    state,
                    video,
                    True,
                    config,
                    delivery_attempt_id=attempt_id,
                )

                attempt = state.get_ai_delivery_attempt(attempt_id)
                obligation = state.get_ai_delivery_obligation(attempt["obligation_id"])
                queue_status = state._conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
                self.assertEqual(attempt["status"], "succeeded")
                self.assertEqual(obligation["state"], "succeeded")
                self.assertEqual(obligation["outcome_code"], "verified_on_time")
                self.assertEqual(
                    obligation["verification"]["publication_kind"],
                    "translated_trilingual",
                )
                self.assertEqual(
                    obligation["verification"]["output_languages"],
                    ["ja", "zh-CN", "zh-TW"],
                )
                self.assertEqual(queue_status, "done")
            finally:
                state.close()

    def test_worker_ok_without_delivery_evidence_is_retryable_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode 01.mkv"
            video.write_bytes(b"media")
            config = SimpleNamespace(
                work_path=root,
                scanner_state_path=root / "scanner_state.sqlite3",
                ai_output_manifest_path="manifests",
                control_state_path="control.sqlite3",
                auto_ai_failure_cooldown_seconds=0,
                auto_ai_max_attempts=3,
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
                attempt_id = main_module._mark_queue_running(state, video, config)

                main_module._mark_queue_result(
                    state,
                    video,
                    True,
                    config,
                    delivery_attempt_id=attempt_id,
                )

                attempt = state.get_ai_delivery_attempt(attempt_id)
                obligation = state.get_ai_delivery_obligation(attempt["obligation_id"])
                queue_status, error_code = state._conn.execute(
                    "SELECT status, last_error_code FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(attempt["status"], "retryable_failure")
                self.assertEqual(obligation["state"], "open")
                self.assertEqual(queue_status, "failed_retry")
                self.assertEqual(error_code, "delivery_evidence_missing")
                self.assertEqual(
                    state.iter_ai_queue_candidates(now=time.time() + 1),
                    [video.resolve()],
                )
            finally:
                state.close()

    def test_close_queue_state_closes_connection_when_commit_fails(self) -> None:
        state = Mock()
        state.commit.side_effect = sqlite3.OperationalError("database is locked")

        with self.assertRaises(sqlite3.OperationalError):
            main_module._close_ai_queue_state(state)

        state.rollback.assert_called_once_with()
        state.close.assert_called_once_with()

    def test_scan_and_process_respects_persistent_ai_pause_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            (work / "ai_control.json").write_text('{"paused": true}', encoding="utf-8")
            scanner = Mock()
            worker = Mock()
            worker.config = SimpleNamespace(
                work_path=work,
                max_concurrent_videos=1,
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
            )

            processed = main_module._scan_and_process(scanner, worker, Mock())

            self.assertEqual(processed, 0)
            scanner.scan.assert_not_called()
            worker.process.assert_not_called()

    def test_scan_and_process_respects_deployment_hold_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir) / "work"
            work.mkdir()
            (work / "deployment_hold.json").write_text(
                '{"active": true, "deployment_id": "test"}',
                encoding="utf-8",
            )
            scanner = Mock()
            worker = Mock()
            worker.config = SimpleNamespace(
                work_path=work,
                max_concurrent_videos=1,
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
            )

            processed = main_module._scan_and_process(scanner, worker, Mock())

            self.assertEqual(processed, 0)
            scanner.scan.assert_not_called()
            worker.process.assert_not_called()

    def test_scan_and_process_pause_waits_for_current_video_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            videos = [root / "Anime S01E01.mkv", root / "Anime S01E02.mkv"]
            for video in videos:
                video.write_text("", encoding="utf-8")

            class FakeScanner:
                def scan(self, max_candidates=None):
                    return videos

            class FakeWorker:
                def __init__(self) -> None:
                    self.config = SimpleNamespace(
                        work_path=work,
                        max_concurrent_videos=1,
                        scanner_cache_enabled=False,
                        scanner_queue_enabled=False,
                    )
                    self.processed = []

                def process(self, video):
                    self.processed.append(video)
                    (work / "ai_control.json").write_text('{"paused": true}', encoding="utf-8")
                    return True

            worker = FakeWorker()
            processed = main_module._scan_and_process(FakeScanner(), worker, Mock())

            self.assertEqual(processed, 1)
            self.assertEqual(worker.processed, [videos[0]])

    def test_concurrent_queue_path_imports_worker_and_processes_all_videos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            videos = [root / f"Anime S01E{episode:02d}.mkv" for episode in range(1, 4)]
            for video in videos:
                video.write_text("", encoding="utf-8")
            scanner = Mock()
            scanner.scan.return_value = videos
            config = SimpleNamespace(
                work_path=work,
                max_concurrent_videos=2,
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
                ai_process_isolation_enabled=False,
            )
            worker = Mock()
            worker.config = config
            clones = []

            def make_worker(_config, _logger):
                clone = Mock()
                clone.config = config
                clone.process.return_value = True
                clones.append(clone)
                return clone

            with patch("worker.VideoWorker", side_effect=make_worker):
                processed = main_module._scan_and_process(scanner, worker, Mock())

            self.assertEqual(processed, 3)
            self.assertEqual(sum(clone.process.call_count for clone in clones), 3)

    def test_success_does_not_clear_force_ai_until_ai_output_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = SimpleNamespace(
                work_path=root / "work",
                export_ai_ass=True,
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
                finished_subtitle_suffixes=[".official.zh-TW.ass"],
                ass_style_versioning_enabled=False,
            )
            state = ScanStateStore(root / "state.sqlite3")
            try:
                state.force_ai_queue_candidate(video)
                state.commit()

                main_module._mark_queue_result(state, video, True, config)
                self.assertTrue(state.is_force_ai_queue_candidate(video))
                self.assertEqual(state.iter_ai_queue_candidates(), [video.resolve()])

                (root / "Anime S01E01.AI.zh-TW.ass").write_text(
                    "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
                    "這裡會選擇開啟網路連線並顯示資訊\n",
                    encoding="utf-8",
                )
                main_module._mark_queue_result(state, video, True, config)
                self.assertFalse(state.is_force_ai_queue_candidate(video))
                self.assertEqual(state.iter_ai_queue_candidates(), [])
            finally:
                state.close()

    def test_asr_review_failure_pauses_until_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            state = ScanStateStore(root / "state.sqlite3")
            config = SimpleNamespace(
                auto_ai_failure_cooldown_seconds=86400,
                auto_ai_asr_review_cooldown_seconds=900,
            )
            try:
                state.upsert_ai_queue_candidate(video, 1)
                state.mark_ai_queue_running(video)
                state.update_ai_job_stage(video, "transcription_review", "failed", "fresh ASR required")
                main_module._mark_queue_result(state, video, False, config)

                row = state._conn.execute(
                    "SELECT status, source, last_error, next_retry_at FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(row, ("paused", "asr_review", "fresh ASR required", 0.0))
            finally:
                state.close()

    def test_quality_review_autopilot_queues_one_exact_revision_without_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime" / "Season 1" / "Anime - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"media")
            review_id = "review_" + "a" * 24
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "failure-revision",
                "attempts": 3,
                "last_error_code": "deterministic_asr_quality",
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=900,
                work_path=root / "work",
            )
            review = {
                "review_id": review_id,
                "kind": "asr_quality",
                "target_key": str(video),
                "diagnosis": {"video": str(video)},
                "candidates": [{"action": "ai.retranscribe"}],
            }
            enqueue = Mock(return_value={"command_id": "cmd_test", "status": "queued"})
            revision_allowed = Mock(return_value=True)
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
                patch("control_state.list_open_review_autopilot_candidates", return_value=[review]),
                patch(
                    "control_state.review_autopilot_revision_attempt_allowed",
                    revision_allowed,
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertTrue(queued)
            parameters = enqueue.call_args.kwargs["parameters"]
            self.assertEqual(parameters["review_id"], review_id)
            self.assertEqual(parameters["expected_failure_revision"], "failure-revision")
            self.assertEqual(
                parameters["policy_revision"],
                main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
            )
            self.assertEqual(
                enqueue.call_args.kwargs["idempotency_key"],
                main_module._review_autopilot_prefix(
                    main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
                    "review.resolve_ai",
                )
                + review_id
                + ":failure-revision",
            )
            self.assertNotIn("retry_failed", enqueue.call_args.kwargs)
            self.assertEqual(
                revision_allowed.call_args.kwargs["max_attempts"],
                main_module._AI_QUALITY_REVIEW_AUTOPILOT_MAX_ATTEMPTS,
            )
            queue_state.close.assert_called_once()

    def test_quality_review_autopilot_pages_past_100_exhausted_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime" / "Anime - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"media")
            exhausted = [
                {
                    "review_id": f"review_{index:024x}",
                    "target_key": str(video),
                    "diagnosis": {"video": str(video)},
                    "candidates": [{"action": "ai.retranscribe"}],
                }
                for index in range(100)
            ]
            eligible = {
                "review_id": "review_" + "f" * 24,
                "target_key": str(video),
                "diagnosis": {"video": str(video)},
                "candidates": [{"action": "ai.retranscribe"}],
            }
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "current-revision",
                "last_error_code": "deterministic_asr_quality",
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=900,
            )

            def candidate_page(*_args, **kwargs):
                return exhausted if kwargs.get("offset", 0) == 0 else [eligible]

            revision_allowed = Mock(
                side_effect=lambda *_args, **kwargs: kwargs["review_id"]
                == eligible["review_id"]
            )
            enqueue = Mock(return_value={"command_id": "cmd_eligible", "status": "queued"})
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(
                    main_module,
                    "_review_autopilot_interval_elapsed",
                    side_effect=[True, False, False, False, False],
                ),
                patch(
                    "control_state.list_open_review_autopilot_candidates",
                    side_effect=candidate_page,
                ) as candidates,
                patch(
                    "control_state.review_autopilot_revision_attempt_allowed",
                    revision_allowed,
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertTrue(queued)
            self.assertEqual(enqueue.call_args.kwargs["parameters"]["review_id"], eligible["review_id"])
            self.assertEqual(
                [item.kwargs["offset"] for item in candidates.call_args_list],
                [0, 100],
            )

    def test_review_autopilot_paging_preserves_completed_prerequisite(self) -> None:
        reviews = [
            {"review_id": f"review_{index:024x}"}
            for index in range(101)
        ]

        def candidate_page(*_args, **kwargs):
            offset = int(kwargs.get("offset") or 0)
            limit = int(kwargs.get("limit") or 0)
            return reviews[offset : offset + limit]

        with patch(
            "control_state.list_open_review_autopilot_candidates",
            side_effect=candidate_page,
        ) as candidates:
            found = list(
                main_module._iter_review_autopilot_candidates(
                    SimpleNamespace(),
                    kind="asr_quality",
                    idempotency_prefix="omission-prefix:",
                    required_completed_prefix="asr-prefix:",
                )
            )

        self.assertEqual(found, reviews)
        self.assertEqual(
            [item.kwargs["offset"] for item in candidates.call_args_list],
            [0, 100],
        )
        self.assertTrue(
            all(
                item.kwargs["required_completed_prefix"] == "asr-prefix:"
                for item in candidates.call_args_list
            )
        )

    def test_quality_review_autopilot_skips_consumed_selective_payload_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            videos = [root / f"Anime - S01E0{index}.mkv" for index in (1, 2)]
            for video in videos:
                video.write_bytes(b"media")
            reviews = [
                {
                    "review_id": f"review_{index:024x}",
                    "target_key": str(video),
                    "diagnosis": {"video": str(video)},
                    "candidates": [
                        {"action": "ai.retry_selective_asr"},
                        {"action": "ai.retranscribe"},
                    ],
                }
                for index, video in enumerate(videos, start=1)
            ]
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "current-failure-revision",
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=60,
            )
            enqueue = Mock(
                side_effect=[
                    ValueError(
                        "idempotency key was already used with a different command payload"
                    ),
                    {"command_id": "cmd_next", "status": "queued"},
                ]
            )
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(
                    main_module,
                    "_review_autopilot_interval_elapsed",
                    return_value=True,
                ),
                patch.object(
                    main_module,
                    "_selective_asr_review_evidence",
                    return_value={
                        "checkpoint_id": "checkpoint",
                        "evidence_revision": "evidence-revision",
                    },
                ),
                patch(
                    "control_state.list_open_review_autopilot_candidates",
                    return_value=reviews,
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertTrue(queued)
            self.assertEqual(enqueue.call_count, 2)
            self.assertEqual(
                enqueue.call_args.kwargs["parameters"]["review_id"],
                reviews[1]["review_id"],
            )
            self.assertEqual(
                enqueue.call_args_list[0].kwargs["idempotency_key"],
                main_module._review_autopilot_prefix(
                    main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                    "review.resolve_ai",
                )
                + reviews[0]["review_id"]
                + ":current-failure-revision",
            )
            queue_state.close.assert_called_once()

    def test_quality_review_autopilot_never_full_retranscribes_translation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime.mkv"
            video.write_bytes(b"media")
            review = {
                "review_id": "review_" + "a" * 24,
                "target_key": str(video),
                "diagnosis": {"video": str(video)},
                "candidates": [{"action": "ai.retranscribe"}],
            }
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "translation-revision",
                "last_error_code": "translation_safe_omission",
                "job_stage": "quality_check",
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=900,
            )
            enqueue = Mock()
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(
                    main_module,
                    "_review_autopilot_interval_elapsed",
                    side_effect=[True, False, False, False, False],
                ),
                patch(
                    "control_state.list_open_review_autopilot_candidates",
                    return_value=[review],
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertFalse(queued)
            enqueue.assert_not_called()

    def test_full_asr_snapshot_accepts_exact_legacy_transcription_evidence(self) -> None:
        snapshot = {
            "status": "paused",
            "failure_revision": "legacy-revision",
            "last_error_code": "legacy_transcription",
            "job_stage": "transcription",
            "last_error": (
                "ASR artifacts or low-confidence rescue candidates were rejected; "
                "the affected audio must be re-transcribed without a prompt: 12.0-18.5s"
            ),
        }

        self.assertTrue(
            main_module._asr_review_snapshot_requires_full_retranscription(snapshot)
        )

    def test_full_asr_snapshot_rejects_malformed_legacy_transcription_evidence(self) -> None:
        snapshot = {
            "status": "paused",
            "failure_revision": "legacy-revision",
            "last_error_code": "legacy_transcription",
            "job_stage": "transcription",
            "last_error": (
                "ASR artifacts or low-confidence rescue candidates were rejected; "
                "the affected audio must be re-transcribed without a prompt: no ranges"
            ),
        }

        self.assertFalse(
            main_module._asr_review_snapshot_requires_full_retranscription(snapshot)
        )

    def test_full_asr_snapshot_accepts_only_rebuildable_cached_context_mismatch(self) -> None:
        snapshot = {
            "status": "paused",
            "failure_revision": "cached-revision",
            "last_error_code": "",
            "job_stage": "transcription_review",
            "last_error": (
                "Cached ASR selective repair refused fail-closed: "
                "extracted audio fingerprint mismatch; "
                "Japanese SRT cache fingerprint mismatch; repair fingerprint mismatch"
            ),
        }

        self.assertTrue(
            main_module._asr_review_snapshot_requires_full_retranscription(snapshot)
        )
        snapshot["last_error"] = (
            "Cached ASR selective repair refused fail-closed: "
            "diagnostic transcript hash mismatch"
        )
        self.assertFalse(
            main_module._asr_review_snapshot_requires_full_retranscription(snapshot)
        )

    def test_full_asr_snapshot_rejects_translation_code_even_with_asr_marker(self) -> None:
        snapshot = {
            "status": "paused",
            "failure_revision": "translation-revision",
            "last_error_code": "translation_safe_omission",
            "job_stage": "transcription",
            "last_error": (
                "ASR artifacts or low-confidence rescue candidates were rejected; "
                "the affected audio must be re-transcribed without a prompt: 12.0-18.5s"
            ),
        }

        self.assertFalse(
            main_module._asr_review_snapshot_requires_full_retranscription(snapshot)
        )

    def test_quality_review_autopilot_waits_for_existing_remediation(self) -> None:
        queue_state = Mock()
        queue_state.active_review_remediation_count.return_value = 1
        config = SimpleNamespace(
            auto_ai_quality_review_autopilot_enabled=True,
            auto_ai_quality_review_autopilot_interval_seconds=900,
        )
        enqueue = Mock()
        with (
            patch.object(main_module, "_ai_queue_paused", return_value=False),
            patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
            patch(
                "control_state.list_open_review_autopilot_candidates",
                return_value=[{"review_id": "review_" + "c" * 24}],
            ),
            patch("control_state.enqueue_command", enqueue),
            patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
        ):
            queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

        self.assertFalse(queued)
        enqueue.assert_not_called()
        queue_state.ai_queue_candidate_snapshot.assert_not_called()
        queue_state.close.assert_called_once()

    def test_quality_review_autopilot_prioritizes_exact_translation_omission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime" / "Season 1" / "Anime - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"media")
            review_id = "review_" + "d" * 24
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 2,
                "failure_revision": "translation-revision",
                "job_stage": "quality_check",
                "last_error": "Translation safe-omission remained after bounded same-job recovery: indexes=[9, 12]",
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=900,
            )
            review = {
                "review_id": review_id,
                "kind": "asr_quality",
                "target_key": str(video),
                "diagnosis": {"video": str(video)},
                "candidates": [{"action": "ai.retranscribe"}],
            }
            enqueue = Mock(return_value={"command_id": "cmd_translation"})
            candidates = Mock(return_value=[review])
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
                patch("control_state.list_open_review_autopilot_candidates", candidates),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertTrue(queued)
            parameters = enqueue.call_args.kwargs["parameters"]
            self.assertEqual(parameters["remediation"], "ai.retranslate")
            self.assertEqual(parameters["expected_failure_revision"], "translation-revision")
            self.assertEqual(
                parameters["policy_revision"],
                main_module._AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY,
            )
            prerequisite_prefixes = {
                item.kwargs.get("required_completed_prefix")
                for item in candidates.call_args_list
            }
            self.assertIn(
                main_module._review_autopilot_prefix(
                    main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
                    "review.resolve_ai",
                ),
                prerequisite_prefixes,
            )
            self.assertIn(
                main_module._review_autopilot_prefix(
                    main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                    "review.resolve_ai",
                ),
                prerequisite_prefixes,
            )

    def test_quality_review_autopilot_queues_revision_bound_omission_line_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime" / "Season 1" / "Anime - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"media")
            review_id = "review_" + "1" * 24
            review = {
                "review_id": review_id,
                "kind": "asr_quality",
                "target_key": str(video),
                "diagnosis": {"video": str(video)},
                "candidates": [{"action": "ai.retranscribe"}],
            }
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.running_ai_queue_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 4,
                "failure_revision": "line-repair-revision",
                "job_stage": "quality_check",
                "last_error": (
                    "Translation safe-omission remained after bounded same-job recovery: "
                    "indexes=[12, 9, 12]"
                ),
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=60,
            )
            action = "review.resolve_ai"
            line_prefix = main_module._review_autopilot_prefix(
                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
                action,
            )
            omission_prefix = main_module._review_autopilot_prefix(
                main_module._AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY,
                action,
            )

            def candidates(*_args, **kwargs):
                if kwargs.get("idempotency_prefix") == line_prefix:
                    return iter([review])
                return iter([])

            enqueue = Mock(return_value={"command_id": "cmd_lines", "status": "queued"})
            revision_allowed = Mock(return_value=True)
            candidate_iterator = Mock(side_effect=candidates)
            interval_ready = Mock(
                side_effect=lambda _config, **kwargs: (
                    kwargs.get("idempotency_prefix") == line_prefix
                )
            )
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(
                    main_module,
                    "_review_autopilot_interval_elapsed",
                    interval_ready,
                ),
                patch.object(
                    main_module,
                    "_iter_review_autopilot_candidates",
                    candidate_iterator,
                ),
                patch(
                    "control_state.review_autopilot_revision_attempt_allowed",
                    revision_allowed,
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertTrue(queued)
            parameters = enqueue.call_args.kwargs["parameters"]
            self.assertEqual(parameters["remediation"], "ai.retranslate_lines")
            self.assertEqual(parameters["lines"], "9,12")
            self.assertEqual(parameters["expected_failure_revision"], "line-repair-revision")
            self.assertEqual(
                parameters["policy_revision"],
                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
            )
            self.assertEqual(
                enqueue.call_args.kwargs["idempotency_key"],
                f"{line_prefix}{review_id}:line-repair-revision",
            )
            self.assertEqual(revision_allowed.call_args.kwargs["max_attempts"], 1)
            line_calls = [
                item.kwargs
                for item in candidate_iterator.call_args_list
                if item.kwargs.get("idempotency_prefix") == line_prefix
            ]
            self.assertEqual(len(line_calls), 1)
            self.assertEqual(line_calls[0]["required_completed_prefix"], omission_prefix)
            self.assertTrue(line_calls[0]["allow_revision_scoped_attempts"])
            checked_interval_prefixes = {
                item.kwargs.get("idempotency_prefix")
                for item in interval_ready.call_args_list
            }
            self.assertIn(omission_prefix, checked_interval_prefixes)
            self.assertIn(line_prefix, checked_interval_prefixes)

    def test_quality_review_autopilot_omission_line_repair_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime.mkv"
            video.write_bytes(b"media")
            review = {
                "review_id": "review_" + "2" * 24,
                "kind": "asr_quality",
                "target_key": str(video),
                "diagnosis": {"video": str(video)},
            }
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 4,
                "failure_revision": "already-attempted-revision",
                "job_stage": "quality_check",
                "last_error": (
                    "Translation safe-omission remained after bounded same-job recovery: "
                    "indexes=[98]"
                ),
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=60,
            )
            line_prefix = main_module._review_autopilot_prefix(
                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
                "review.resolve_ai",
            )

            def candidates(*_args, **kwargs):
                return iter([review]) if kwargs.get("idempotency_prefix") == line_prefix else iter([])

            enqueue = Mock()
            revision_allowed = Mock(return_value=False)
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
                patch.object(main_module, "_iter_review_autopilot_candidates", side_effect=candidates),
                patch(
                    "control_state.review_autopilot_revision_attempt_allowed",
                    revision_allowed,
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertFalse(queued)
            enqueue.assert_not_called()
            self.assertEqual(revision_allowed.call_args.kwargs["max_attempts"], 1)

    def test_omission_line_autopilot_waits_for_active_review_remediation(self) -> None:
        queue_state = Mock()
        queue_state.active_review_remediation_count.return_value = 1
        config = SimpleNamespace(
            auto_ai_quality_review_autopilot_enabled=True,
            auto_ai_quality_review_autopilot_interval_seconds=60,
        )
        candidates = Mock(return_value=iter([]))
        enqueue = Mock()
        revision_allowed = Mock()
        with (
            patch.object(main_module, "_ai_queue_paused", return_value=False),
            patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
            patch.object(main_module, "_iter_review_autopilot_candidates", candidates),
            patch(
                "control_state.review_autopilot_revision_attempt_allowed",
                revision_allowed,
            ),
            patch("control_state.enqueue_command", enqueue),
            patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
        ):
            queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

        self.assertFalse(queued)
        candidates.assert_not_called()
        revision_allowed.assert_not_called()
        enqueue.assert_not_called()

    def test_omission_line_autopilot_defers_running_or_locked_video_without_consuming(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime.mkv"
            video.write_bytes(b"media")
            review = {
                "review_id": "review_" + "3" * 24,
                "kind": "asr_quality",
                "target_key": str(video),
                "diagnosis": {"video": str(video)},
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=60,
            )
            line_prefix = main_module._review_autopilot_prefix(
                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
                "review.resolve_ai",
            )

            def candidates(*_args, **kwargs):
                return iter([review]) if kwargs.get("idempotency_prefix") == line_prefix else iter([])

            for running_jobs, lock_available in ((1, True), (0, False)):
                with self.subTest(running_jobs=running_jobs, lock_available=lock_available):
                    queue_state = Mock()
                    queue_state.active_review_remediation_count.return_value = 0
                    queue_state.running_ai_queue_count.return_value = running_jobs
                    queue_state.ai_queue_candidate_snapshot.return_value = {
                        "status": "paused",
                        "attempts": 4,
                        "failure_revision": "blocked-line-revision",
                        "job_stage": "quality_check",
                        "last_error": (
                            "Translation safe-omission remained after bounded same-job recovery: "
                            "indexes=[98]"
                        ),
                    }
                    video_lock = Mock()
                    video_lock.acquire.return_value = lock_available
                    enqueue = Mock()
                    with (
                        patch.object(main_module, "_ai_queue_paused", return_value=False),
                        patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
                        patch.object(
                            main_module,
                            "_iter_review_autopilot_candidates",
                            side_effect=candidates,
                        ),
                        patch(
                            "control_state.review_autopilot_revision_attempt_allowed",
                            return_value=True,
                        ),
                        patch("control_state.enqueue_command", enqueue),
                        patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                        patch("lock.VideoLock", return_value=video_lock) as lock_factory,
                    ):
                        queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

                    self.assertFalse(queued)
                    enqueue.assert_not_called()
                    if running_jobs:
                        lock_factory.assert_not_called()
                    else:
                        lock_factory.assert_called_once_with(video.resolve())
                        video_lock.acquire.assert_called_once()
                        video_lock.release.assert_not_called()

    def test_quality_review_autopilot_queues_exact_aligned_timing_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime" / "Season 1" / "Anime - S01E03.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"media")
            review_id = "review_" + "e" * 24
            review = {
                "review_id": review_id,
                "kind": "subtitle_quality",
                "target_key": str(video),
                "updated_at": 123.0,
                "diagnosis": {
                    "video": str(video),
                    "stage": "quality_check",
                    "reports": [
                        {
                            "role": "japanese",
                            "status": "rerun",
                            "issues": [
                                {"code": "short_duration", "severity": "warn", "indexes": [75]},
                                {"code": "cps_too_high", "severity": "fail", "indexes": [75]},
                            ],
                        }
                    ],
                },
                "candidates": [{"action": "ai.retranslate"}],
            }
            unsafe = {
                **review,
                "review_id": "review_" + "f" * 24,
                "diagnosis": {
                    **review["diagnosis"],
                    "reports": [
                        {
                            "role": "japanese",
                            "status": "rerun",
                            "issues": [
                                {"code": "cps_too_high", "severity": "fail", "indexes": [1]},
                            ],
                        }
                    ],
                },
            }
            self.assertIsNone(main_module._exact_aligned_timing_review_evidence(unsafe))
            self.assertFalse(
                main_module._timing_review_snapshot_is_recoverable(
                    {
                        "status": "paused",
                        "failure_revision": "deterministic-revision",
                        "job_stage": "transcription",
                        "last_error": "ASR artifacts remained after bounded repair",
                    }
                )
            )
            queue_state = Mock()
            queue_state.active_review_remediation_count.return_value = 0
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 2,
                "failure_revision": "timing-revision",
                "job_stage": "transcription",
                "last_error": "Whisper failed: CUDA failed with error out of memory",
            }
            config = SimpleNamespace(
                input_path=root,
                video_extensions=[".mkv"],
                auto_ai_quality_review_autopilot_enabled=True,
                auto_ai_quality_review_autopilot_interval_seconds=60,
            )
            enqueue = Mock(return_value={"command_id": "cmd_timing"})

            def candidates(*_args, **kwargs):
                return [review] if kwargs.get("kind") == "subtitle_quality" else []

            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
                patch("control_state.list_open_review_autopilot_candidates", side_effect=candidates),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(config, Mock())

            self.assertTrue(queued)
            parameters = enqueue.call_args.kwargs["parameters"]
            self.assertEqual(parameters["remediation"], "ai.retranslate")
            self.assertEqual(parameters["expected_failure_revision"], "timing-revision")
            self.assertEqual(
                parameters["policy_revision"],
                main_module._AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY,
            )
            self.assertRegex(parameters["expected_review_evidence_revision"], r"^[0-9a-f]{64}$")

    def test_target_review_autopilot_only_queues_fail_closed_rebuild(self) -> None:
        review_id = "review_" + "b" * 24
        config = SimpleNamespace(
            auto_target_review_autopilot_enabled=True,
            auto_target_review_autopilot_interval_seconds=300,
        )
        enqueue = Mock(return_value={"command_id": "cmd_target"})
        with (
            patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
            patch.object(
                main_module,
                "_iter_review_autopilot_candidates",
                return_value=iter([{"review_id": review_id}]),
            ),
            patch("control_state.next_review_autopilot_retry_attempt", return_value=2),
            patch("control_state.enqueue_command", enqueue),
        ):
            queued = main_module._advance_target_review_autopilot(config, Mock())

        self.assertTrue(queued)
        self.assertEqual(
            enqueue.call_args.kwargs["action"],
            "review.auto_rebuild_target_candidates",
        )
        self.assertEqual(enqueue.call_args.kwargs["target"], review_id)
        self.assertEqual(enqueue.call_args.kwargs["parameters"]["attempt"], 2)
        self.assertTrue(enqueue.call_args.kwargs["idempotency_key"].endswith(":attempt-2"))

    def test_target_review_autopilot_skips_non_unique_without_spending_attempt(self) -> None:
        ambiguous_id = "review_" + "a" * 24
        exact_id = "review_" + "b" * 24
        reviews = [
            {
                "review_id": ambiguous_id,
                "candidates": [
                    {"path": "/anime/A/Season 1/A - S01E01.mkv"},
                    {"path": "/anime/B/Season 1/B - S01E01.mkv"},
                ],
            },
            {
                "review_id": exact_id,
                "candidates": [
                    {"path": "/anime/C/Season 1/C - S01E01.mkv"},
                ],
            },
        ]
        config = SimpleNamespace(
            auto_target_review_autopilot_enabled=True,
            auto_target_review_autopilot_interval_seconds=300,
        )
        next_attempt = Mock(return_value=1)
        enqueue = Mock(return_value={"command_id": "cmd_target"})
        with (
            patch.object(main_module, "_review_autopilot_interval_elapsed", return_value=True),
            patch.object(
                main_module,
                "_iter_review_autopilot_candidates",
                return_value=iter(reviews),
            ),
            patch(
                "control_state.next_review_autopilot_retry_attempt",
                next_attempt,
            ),
            patch("control_state.enqueue_command", enqueue),
        ):
            queued = main_module._advance_target_review_autopilot(config, Mock())

        self.assertTrue(queued)
        next_attempt.assert_called_once()
        self.assertEqual(next_attempt.call_args.kwargs["review_id"], exact_id)
        self.assertEqual(enqueue.call_args.kwargs["target"], exact_id)

    def test_automatic_target_resolution_reuses_strict_resolver(self) -> None:
        expected = {"resolved": True, "requeued": 1}
        with patch.object(
            main_module,
            "_execute_control_command",
            return_value=expected,
        ) as execute:
            result = main_module._resolve_automatic_target_review(
                SimpleNamespace(),
                Mock(),
                review_id="review_" + "c" * 24,
                result={
                    "auto_selected": True,
                    "source_id": "2911",
                    "candidate_path": "/anime/Show/Season 2/Show - S02E01.mkv",
                    "series_id": "series_" + "d" * 24,
                    "season": 2,
                },
                original_source_ids={2911},
                policy_revision="target-policy-v2",
            )

        self.assertEqual(result, expected)
        args = execute.call_args.args
        self.assertEqual(args[2], "review.resolve_target")
        self.assertEqual(args[3], "review_" + "c" * 24)
        parameters = args[4]
        self.assertTrue(parameters["automatic_review"])
        self.assertEqual(parameters["source_id"], "2911")
        self.assertEqual(parameters["policy_revision"], "target-policy-v2")

    def test_automatic_target_resolution_rejects_new_source_identity(self) -> None:
        with patch.object(main_module, "_execute_control_command") as execute:
            with self.assertRaisesRegex(ValueError, "one verified rebuilt candidate"):
                main_module._resolve_automatic_target_review(
                    SimpleNamespace(),
                    Mock(),
                    review_id="review_" + "c" * 24,
                    result={
                        "auto_selected": True,
                        "source_id": "3518",
                        "candidate_path": "/anime/Show/Season 1/Show - S01E05.mkv",
                        "series_id": "series_" + "d" * 24,
                        "season": 1,
                    },
                    original_source_ids={2218},
                    policy_revision="target-policy-v2",
                )

        execute.assert_not_called()

    def test_historical_failure_classifier_requires_exact_parseable_evidence(self) -> None:
        asr = main_module._classify_historical_failed_retry(
            "transcription",
            "ASR artifacts or low-confidence rescue candidates were rejected; "
            "the affected audio must be re-transcribed without a prompt: "
            "ranges=79.1-89.4s,105.3-125.2s",
        )
        self.assertEqual(asr["failure_code"], "deterministic_asr_quality")
        self.assertEqual(asr["evidence"]["review_ranges"], [[79.1, 89.4], [105.3, 125.2]])
        self.assertEqual(asr["remediation_candidates"][0]["strategy"], "full_transcription_rerun")

        translation = main_module._classify_historical_failed_retry(
            "quality_check",
            "Translation safe-omission remained after bounded same-job recovery: indexes=[19, 3, 19]",
        )
        self.assertEqual(translation["failure_code"], "translation_safe_omission")
        self.assertEqual(translation["evidence"]["indexes"], [3, 19])
        self.assertEqual(translation["remediation_candidates"][0]["lines"], "3,19")

        self.assertEqual(
            main_module._classify_historical_failed_retry(
                "failed",
                "ASR artifacts or low-confidence rescue candidates were rejected; "
                "the affected audio must be re-transcribed without a prompt: ranges=79.1-89.4s",
            )["failure_code"],
            "deterministic_asr_quality",
        )
        self.assertEqual(
            main_module._classify_historical_failed_retry(
                "failed",
                "Translation safe-omission remained after bounded same-job recovery: indexes=[127]",
            )["failure_code"],
            "translation_safe_omission",
        )
        self.assertEqual(
            main_module._classify_historical_failed_retry(
                "transcription_review",
                "ASR artifacts or low-confidence rescue candidates were rejected; "
                "the affected audio must be re-transcribed without a prompt: ranges=1.0-2.0s",
            )["failure_code"],
            "deterministic_asr_quality",
        )

        self.assertIsNone(
            main_module._classify_historical_failed_retry(
                "transcription",
                "ASR artifacts or low-confidence rescue candidates were rejected; "
                "the affected audio must be re-transcribed without a prompt: ranges=unknown",
            )
        )
        self.assertIsNone(
            main_module._classify_historical_failed_retry(
                "quality_check",
                "Translation safe-omission remained after bounded same-job recovery: indexes=[]",
            )
        )

    def test_translation_omission_line_evidence_rejects_malformed_and_too_many_indexes(self) -> None:
        snapshot = {
            "status": "paused",
            "failure_revision": "translation-revision",
            "job_stage": "quality_check",
        }
        malformed_messages = [
            "Translation safe-omission remained after bounded same-job recovery: indexes=[]",
            "Translation safe-omission remained after bounded same-job recovery: indexes=[1, nope]",
            "Translation safe-omission remained after bounded same-job recovery: indexes=[1] trailing",
        ]
        self.assertEqual(
            main_module._classify_historical_failed_retry(
                "quality_check",
                malformed_messages[-1],
            )["failure_code"],
            "translation_safe_omission",
        )
        for message in malformed_messages:
            with self.subTest(message=message):
                self.assertIsNone(
                    main_module._translation_omission_line_repair_evidence(
                        {**snapshot, "last_error": message}
                    )
                )

        too_many = ",".join(
            str(index)
            for index in range(
                1,
                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_MAX_INDEXES + 2,
            )
        )
        self.assertIsNone(
            main_module._translation_omission_line_repair_evidence(
                {
                    **snapshot,
                    "last_error": (
                        "Translation safe-omission remained after bounded same-job recovery: "
                        f"indexes=[{too_many}]"
                    ),
                }
            )
        )

    def test_historical_review_preview_only_requeues_exact_translation_omission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            asr_video = root / "ASR.mkv"
            translation_video = root / "Translation.mkv"
            malformed_video = root / "Malformed.mkv"
            for video in (asr_video, translation_video, malformed_video):
                video.write_bytes(b"media")
            candidates = [
                {
                    "path": str(asr_video), "attempts": 1, "failure_revision": "asr-rev",
                    "last_error_code": "legacy_transcription", "job_stage": "transcription",
                    "last_error": "ASR artifacts or low-confidence rescue candidates were rejected; "
                    "the affected audio must be re-transcribed without a prompt: ranges=10.0-20.0s",
                    "next_retry_at": 0, "updated_at": 1,
                },
                {
                    "path": str(translation_video), "attempts": 1, "failure_revision": "tr-rev",
                    "last_error_code": "legacy_quality_check", "job_stage": "quality_check",
                    "last_error": "Translation safe-omission remained after bounded same-job recovery: indexes=[127]",
                    "next_retry_at": 0, "updated_at": 1,
                },
                {
                    "path": str(malformed_video), "attempts": 1, "failure_revision": "bad-rev",
                    "last_error_code": "legacy_transcription", "job_stage": "transcription",
                    "last_error": "ASR failed without bounded evidence", "next_retry_at": 0, "updated_at": 1,
                },
            ]
            state = Mock()
            state.failed_retry_candidates.return_value = candidates
            parameters = {
                "max_attempts": 3,
                "min_age_seconds": 0,
                "allowed_failure_codes": sorted(main_module._AI_SWEEP_ALLOWED_FAILURE_CODES),
            }
            selective = {
                "action": "ai.retry_selective_asr", "strategy": "selective_asr_repair",
                "selective": True, "repair_fingerprint": "a" * 64,
                "requires_runtime_fingerprint_verification": True,
            }
            with (
                patch("scan_state.ScanStateStore.from_config", return_value=state),
                patch("control_state.open_ai_quality_review_for_target", return_value=None),
                patch("control_state.processed_auto_remediation_keys", return_value=set()),
                patch.object(main_module, "_historical_asr_selective_preview_candidate", return_value=selective),
                patch.object(main_module.time, "time", return_value=1000.0),
            ):
                first = main_module._preview_ai_failed_retry_sweep(SimpleNamespace(), parameters)
                second = main_module._preview_ai_failed_retry_sweep(SimpleNamespace(), parameters)

            self.assertEqual(first, second)
            self.assertEqual(first["counters"]["review_required"], 2)
            self.assertEqual(first["counters"]["unsupported"], 1)
            self.assertEqual(
                first["eligible_items"],
                [
                    {
                        "path": str(translation_video),
                        "failure_revision": "tr-rev",
                        "failure_code": "translation_safe_omission",
                        "attempts": 1,
                        "strategy": "retry_preserve_budget",
                        "review_id": "",
                    }
                ],
            )
            self.assertEqual(
                [item["failure_code"] for item in first["review_required_items"]],
                ["deterministic_asr_quality", "translation_safe_omission"],
            )
            self.assertEqual(
                [candidate["strategy"] for candidate in first["review_required_items"][0]["remediation_candidates"]],
                ["selective_asr_repair", "full_transcription_rerun"],
            )
            state.queue_failed_retry_preserving_budget.assert_not_called()
            state.pause_failed_retry_for_review.assert_not_called()
            state.commit.assert_not_called()

    def test_selective_asr_preview_requires_complete_unspent_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime.mkv"
            ja_srt = root / "Anime.ja.srt"
            video.write_bytes(b"media")
            ja_srt.write_bytes(b"subtitle")
            digest = "a" * 64
            diagnostics = {
                "status": "selective_retry_required",
                "srt_sha256": digest,
                "repair_fingerprint": "b" * 64,
                "repair_attempts": [],
                **{key: {"fingerprint": digest} for key in (
                    "media_fingerprint", "audio_fingerprint", "audio_stream_fingerprint", "cache_fingerprint"
                )},
            }
            classification = {"evidence": {"review_ranges": [[10.0, 20.0]]}}
            with (
                patch("subtitle_paths.paths_for_video", return_value=SimpleNamespace(ja_srt=ja_srt)),
                patch("transcriber.read_asr_diagnostics", return_value=diagnostics),
                patch("safe_files.sha256_file", return_value=digest),
            ):
                candidate = main_module._historical_asr_selective_preview_candidate(
                    SimpleNamespace(asr_selective_retry_enabled=True), str(video), classification
                )
                self.assertEqual(candidate["strategy"], "selective_asr_repair")
                diagnostics["audio_fingerprint"] = {}
                self.assertIsNone(
                    main_module._historical_asr_selective_preview_candidate(
                        SimpleNamespace(asr_selective_retry_enabled=True), str(video), classification
                    )
                )

    def test_repeated_worker_failure_pauses_after_configured_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            state = ScanStateStore(root / "state.sqlite3")
            config = SimpleNamespace(
                work_path=root,
                control_state_path="control_state.sqlite3",
                auto_ai_failure_cooldown_seconds=60,
                auto_ai_max_attempts=2,
            )
            try:
                state.upsert_ai_queue_candidate(video, 1)
                state.mark_ai_queue_running(video)
                main_module._mark_queue_result(state, video, False, config)
                state.mark_ai_queue_running(video)
                main_module._mark_queue_result(state, video, False, config)

                row = state._conn.execute(
                    "SELECT status, source, attempts, next_retry_at FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(row, ("paused", "failure_review", 2, 0.0))
            finally:
                state.close()

    def test_open_quality_review_oom_uses_bounded_lower_memory_retry(self) -> None:
        video = Path("/anime/Example.mkv")
        state = Mock()
        state.ai_job_failure.return_value = (
            "transcription",
            "Whisper transcription failed: CUDA failed with error out of memory",
        )
        config = SimpleNamespace(
            auto_ai_failure_cooldown_seconds=60,
            auto_ai_max_attempts=3,
        )
        with patch(
            "control_state.open_ai_quality_review_for_target",
            return_value={"kind": "subtitle_quality", "status": "open"},
        ):
            main_module._mark_queue_result(state, video, False, config)

        state.mark_ai_queue_failed.assert_called_once_with(
            video,
            "Whisper transcription failed: CUDA failed with error out of memory",
            retry_after_seconds=60,
            max_attempts=3,
            error_code="transient_oom",
            retry_strategy="lower_memory_same_pipeline",
        )
        state.mark_ai_queue_review_required.assert_not_called()

    def test_scan_and_process_drains_current_video_on_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            videos = [root / "Anime S01E01.mkv", root / "Anime S01E02.mkv"]
            for video in videos:
                video.write_text("", encoding="utf-8")
            shutdown_event = threading.Event()

            class FakeScanner:
                def scan(self, max_candidates=None):
                    return videos

            class FakeWorker:
                def __init__(self) -> None:
                    self.config = SimpleNamespace(
                        max_concurrent_videos=1,
                        scanner_cache_enabled=False,
                        scanner_queue_enabled=False,
                    )
                    self.processed = []

                def process(self, video):
                    self.processed.append(video)
                    shutdown_event.set()
                    return True

            class FakeLogger:
                def info(self, *args, **kwargs):
                    pass

            worker = FakeWorker()
            main_module._scan_and_process(FakeScanner(), worker, FakeLogger(), shutdown_event=shutdown_event)

            self.assertEqual(worker.processed, [videos[0]])

    def test_scan_and_process_can_drain_queue_without_library_scan(self) -> None:
        video = Path("queued.mkv")
        scanner = Mock()
        scanner.queued_candidates.return_value = [video]
        worker = Mock()
        worker.config = SimpleNamespace(
            max_concurrent_videos=1,
            scanner_cache_enabled=False,
            scanner_queue_enabled=False,
        )
        worker.process.return_value = True

        main_module._scan_and_process(scanner, worker, Mock(), queue_only=True)

        scanner.queued_candidates.assert_called_once_with(max_candidates=None)
        scanner.scan.assert_not_called()
        worker.process.assert_called_once_with(video)

    def test_resource_admission_defers_before_queue_claim_or_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "queued.mkv"
            video.write_bytes(b"media")
            scanner = Mock()
            scanner.queued_candidates.return_value = [video]
            scanner.last_database_error = ""
            scanner.last_database_error_code = ""
            worker = Mock()
            worker.config = SimpleNamespace(
                work_path=Path(temp_dir),
                max_concurrent_videos=1,
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
                resource_admission_enabled=True,
            )
            plan = {
                "admitted": False,
                "retry_at": time.time() + 30,
                "reason_codes": ["ram_pressure"],
            }
            with patch.object(main_module, "_resource_launch_plan_for_video", return_value=plan):
                processed = main_module._scan_and_process(
                    scanner,
                    worker,
                    Mock(),
                    queue_only=True,
                )

            self.assertEqual(processed, 0)
            worker.process.assert_not_called()
            state = json.loads(
                (Path(temp_dir) / "ai_scheduler_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["state"], "resource_deferred")
            self.assertEqual(state["reason_code"], "resource_admission_deferred")
            self.assertGreater(state["next_retry_at"], time.time())

    def test_process_video_with_policy_uses_isolated_subprocess_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            video = root / "Anime S01E01.mkv"
            config_path.write_text("config", encoding="utf-8")
            video.write_text("", encoding="utf-8")
            worker = Mock()
            worker.config = SimpleNamespace(
                ai_process_isolation_enabled=True,
                ai_subprocess_timeout_seconds=123,
                config_path=config_path,
            )
            logger = Mock()

            with patch.object(main_module.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                ok = main_module._process_video_with_policy(worker, video, logger)

            self.assertTrue(ok)
            worker.process.assert_not_called()
            command = run.call_args.args[0]
            self.assertIn("--process-video", command)
            self.assertIn("--video-path", command)
            self.assertIn(str(video), command)
            self.assertIn(str(config_path), command)
            self.assertEqual(run.call_args.kwargs["timeout"], 123)

    def test_process_video_with_policy_treats_killed_subprocess_as_job_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            video = root / "Anime S01E01.mkv"
            config_path.write_text("config", encoding="utf-8")
            video.write_text("", encoding="utf-8")
            worker = Mock()
            worker.config = SimpleNamespace(
                ai_process_isolation_enabled=True,
                ai_subprocess_timeout_seconds=123,
                config_path=config_path,
            )
            logger = Mock()

            with patch.object(main_module.subprocess, "run", return_value=SimpleNamespace(returncode=-9)):
                ok = main_module._process_video_with_policy(worker, video, logger)

            self.assertFalse(ok)
            worker.process.assert_not_called()
            logger.error.assert_called()

    def test_isolated_subprocess_receives_only_serialized_resource_launch_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            video = root / "episode.mkv"
            config_path.write_text("config", encoding="utf-8")
            video.write_bytes(b"media")
            config = SimpleNamespace(
                ai_subprocess_timeout_seconds=123,
                config_path=config_path,
            )
            plan = {"contract": "resource-launch-plan-v1"}
            with patch.object(
                main_module.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as run, patch(
                "resource_runtime.serialize_launch_plan",
                return_value="signed-plan",
            ):
                self.assertTrue(
                    main_module._process_video_subprocess(
                        config,
                        video,
                        Mock(),
                        resource_launch_plan=plan,
                    )
                )
            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["ANIME_RESOURCE_LAUNCH_PLAN"], "signed-plan")

    def test_auto_run_drains_existing_ai_queue_between_watch_cycles(self) -> None:
        videos = [Path("queued-1.mkv"), Path("queued-2.mkv")]
        scanner = Mock()
        scanner.queued_candidates.side_effect = [[videos[0]], [videos[1]], []]
        worker = Mock()
        worker.config = SimpleNamespace(
            max_concurrent_videos=1,
            scanner_cache_enabled=False,
            scanner_queue_enabled=False,
        )
        worker.process.return_value = True
        config = SimpleNamespace(
            auto_enable_ai_fallback=True,
            auto_ai_max_videos_per_cycle=1,
            auto_ai_drain_queue_between_cycles=True,
        )

        with patch.object(main_module, "_mikan_redownload_pending_or_active", return_value=False):
            main_module._auto_run_once(
                scanner,
                worker,
                config,
                Mock(),
                mikan_managed_externally=True,
                ai_scan_managed_externally=True,
            )

        self.assertEqual(worker.process.call_count, 2)
        self.assertEqual([call.args[0] for call in worker.process.call_args_list], videos)
        self.assertEqual(scanner.queued_candidates.call_count, 3)
        scanner.scan.assert_not_called()

    def test_parallel_mikan_redownload_does_not_block_ai_queue(self) -> None:
        video = Path("queued-while-redownload.mkv")
        scanner = Mock()
        scanner.queued_candidates.side_effect = [[video], []]
        worker = Mock()
        worker.config = SimpleNamespace(
            max_concurrent_videos=1,
            scanner_cache_enabled=False,
            scanner_queue_enabled=False,
        )
        worker.process.return_value = True
        config = SimpleNamespace(
            auto_enable_ai_fallback=True,
            auto_ai_max_videos_per_cycle=1,
            auto_ai_drain_queue_between_cycles=True,
            auto_mikan_parallel_with_ai=True,
        )

        with patch.object(main_module, "_mikan_redownload_pending_or_active", return_value=True):
            main_module._auto_run_once(
                scanner,
                worker,
                config,
                Mock(),
                mikan_managed_externally=True,
                ai_scan_managed_externally=True,
            )

        worker.process.assert_called_once_with(video)

    def test_background_ai_scan_refreshes_queue_then_waits(self) -> None:
        config = SimpleNamespace(watch_interval_seconds=15)
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        scanner = Mock()

        with patch("scanner.VideoScanner", return_value=scanner):
            main_module._background_ai_scan_loop(config, logger, shutdown_event)

        scanner.refresh_queue.assert_called_once_with(reconcile_batch=True)
        shutdown_event.wait.assert_called_once_with(15)

    def test_background_ai_scan_uses_delayed_low_frequency_reconciliation_with_event_watcher(self) -> None:
        config = SimpleNamespace(
            watch_interval_seconds=15,
            scanner_event_watch_enabled=True,
            scanner_background_scan_interval_seconds=21600,
            scanner_background_scan_startup_delay_seconds=600,
            scanner_event_watch_health_interval_seconds=30,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.side_effect = [False, True]
        scanner = Mock()
        watcher = Mock()
        watcher.is_alive.return_value = True

        with patch("scanner.VideoScanner", return_value=scanner):
            main_module._background_ai_scan_loop(
                config,
                logger,
                shutdown_event,
                watcher,
            )

        scanner.refresh_queue.assert_called_once_with(reconcile_batch=True)
        self.assertEqual(shutdown_event.wait.call_args_list, [call(600), call(30.0)])

    def test_background_ai_scan_falls_back_immediately_when_watcher_failed_to_start(self) -> None:
        config = SimpleNamespace(
            watch_interval_seconds=15,
            scanner_event_watch_enabled=True,
            scanner_background_scan_interval_seconds=21600,
            scanner_background_scan_startup_delay_seconds=600,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        scanner = Mock()

        with patch("scanner.VideoScanner", return_value=scanner):
            main_module._background_ai_scan_loop(
                config,
                logger,
                shutdown_event,
                None,
            )

        scanner.refresh_queue.assert_called_once_with(reconcile_batch=True)
        shutdown_event.wait.assert_called_once_with(15)

    def test_background_ai_ledger_backfill_drains_one_bounded_batch_while_worker_is_busy(self) -> None:
        config = SimpleNamespace(
            scanner_active_queue_ledger_backfill_interval_seconds=11,
            scanner_active_queue_ledger_backfill_batch_size=7,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        scanner = Mock()
        scanner.backfill_active_queue_obligations.return_value = {"repaired": 1}

        with (
            patch("scanner.VideoScanner", return_value=scanner),
            patch.object(main_module, "_deployment_hold_active", return_value=False),
        ):
            main_module._background_ai_ledger_backfill_loop(
                config,
                logger,
                shutdown_event,
            )

        scanner.backfill_active_queue_obligations.assert_called_once_with(
            limit=7,
            cancel_event=shutdown_event,
        )
        shutdown_event.wait.assert_called_once_with(11)

    def test_background_ai_ledger_backfill_backs_off_when_only_stable_blockers_remain(self) -> None:
        config = SimpleNamespace(
            scanner_active_queue_ledger_backfill_interval_seconds=10,
            scanner_active_queue_ledger_backfill_no_progress_seconds=300,
            scanner_active_queue_ledger_backfill_batch_size=250,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        scanner = Mock()
        scanner.backfill_active_queue_obligations.return_value = {
            "selected": 49,
            "missing_or_unreadable": 42,
            "media_identity_changed_unproven": 7,
        }

        with (
            patch("scanner.VideoScanner", return_value=scanner),
            patch.object(main_module, "_deployment_hold_active", return_value=False),
        ):
            main_module._background_ai_ledger_backfill_loop(
                config,
                logger,
                shutdown_event,
            )

        shutdown_event.wait.assert_called_once_with(300)

    def test_background_ai_ledger_backfill_retries_database_busy_at_normal_interval(self) -> None:
        config = SimpleNamespace(
            scanner_active_queue_ledger_backfill_interval_seconds=10,
            scanner_active_queue_ledger_backfill_no_progress_seconds=300,
            scanner_active_queue_ledger_backfill_batch_size=250,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        scanner = Mock()
        scanner.backfill_active_queue_obligations.return_value = {"database_busy": 1}

        with (
            patch("scanner.VideoScanner", return_value=scanner),
            patch.object(main_module, "_deployment_hold_active", return_value=False),
        ):
            main_module._background_ai_ledger_backfill_loop(
                config,
                logger,
                shutdown_event,
            )

        shutdown_event.wait.assert_called_once_with(10)

    def test_background_ai_ledger_backfill_retries_unexpected_error_at_normal_interval(self) -> None:
        config = SimpleNamespace(
            scanner_active_queue_ledger_backfill_interval_seconds=10,
            scanner_active_queue_ledger_backfill_no_progress_seconds=300,
            scanner_active_queue_ledger_backfill_batch_size=250,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        scanner = Mock()
        scanner.backfill_active_queue_obligations.side_effect = RuntimeError("injected")

        with (
            patch("scanner.VideoScanner", return_value=scanner),
            patch.object(main_module, "_deployment_hold_active", return_value=False),
        ):
            main_module._background_ai_ledger_backfill_loop(
                config,
                logger,
                shutdown_event,
            )

        shutdown_event.wait.assert_called_once_with(10)
        logger.exception.assert_called_once()

    def test_background_ai_ledger_backfill_pauses_during_deployment_hold(self) -> None:
        config = SimpleNamespace(
            scanner_active_queue_ledger_backfill_interval_seconds=10,
            scanner_active_queue_ledger_backfill_batch_size=250,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        scanner = Mock()

        with (
            patch("scanner.VideoScanner", return_value=scanner),
            patch.object(main_module, "_deployment_hold_active", return_value=True),
        ):
            main_module._background_ai_ledger_backfill_loop(
                config,
                logger,
                shutdown_event,
            )

        scanner.backfill_active_queue_obligations.assert_not_called()
        shutdown_event.wait.assert_called_once_with(1.0)

    def test_background_ai_scan_long_wait_ends_when_watcher_dies(self) -> None:
        config = SimpleNamespace(
            watch_interval_seconds=15,
            scanner_event_watch_enabled=True,
            scanner_event_watch_health_interval_seconds=30,
        )
        shutdown_event = Mock()
        shutdown_event.wait.return_value = False
        watcher = Mock()
        watcher.is_alive.side_effect = [True, False]

        stopped = main_module._wait_for_background_ai_scan(
            shutdown_event,
            21600,
            config,
            event_watcher=watcher,
        )

        self.assertFalse(stopped)
        shutdown_event.wait.assert_called_once_with(30.0)

    def test_background_series_metadata_sync_waits_then_uses_index_only_sync(self) -> None:
        config = SimpleNamespace(
            series_metadata_sync_startup_delay_seconds=30,
            series_metadata_sync_interval_seconds=21600,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.side_effect = [False, True]

        with patch("series_metadata_sync.sync_series_metadata") as sync:
            main_module._background_series_metadata_sync_loop(config, logger, shutdown_event)

        sync.assert_called_once_with(config, logger)
        self.assertEqual(shutdown_event.wait.call_args_list, [call(30), call(21600)])

    def test_background_database_maintenance_runs_once_then_records_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                database_maintenance_startup_delay_seconds=0,
                database_maintenance_interval_hours=168,
                database_maintenance_min_reclaim_mib=64.0,
                database_maintenance_min_freelist_ratio=0.25,
            )
            shutdown_event = Mock()
            shutdown_event.is_set.return_value = False
            shutdown_event.wait.return_value = True
            logger = Mock()
            result = {"status": "not_needed", "optimized": [], "busy_reasons": []}

            with patch("database_maintenance.optimize_databases", return_value=result) as optimize:
                main_module._background_database_maintenance_loop(config, logger, shutdown_event)

            optimize.assert_called_once_with(
                config,
                apply=True,
                wait_seconds=0,
                online_only=True,
                min_reclaim_mib=64.0,
                min_freelist_ratio=0.25,
            )
            state = root / "database_maintenance_state.json"
            self.assertTrue(state.exists())
            self.assertIn('"status": "not_needed"', state.read_text(encoding="utf-8"))

    def test_paused_watch_wakes_promptly_when_ai_is_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            control_path = work / "ai_control.json"
            control_path.write_text('{"paused": true}', encoding="utf-8")
            config = SimpleNamespace(work_path=work)
            shutdown_event = Mock()

            def resume_during_wait(_seconds):
                control_path.write_text('{"paused": false}', encoding="utf-8")
                return False

            shutdown_event.wait.side_effect = resume_during_wait
            with patch.object(main_module.time, "monotonic", side_effect=[100.0, 100.0]):
                stopped = main_module._wait_for_next_cycle_or_ai_resume(shutdown_event, 300, config)

            self.assertFalse(stopped)
            shutdown_event.wait.assert_called_once_with(2.0)

    def test_deployment_hold_release_wakes_watch_without_full_interval_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            hold_path = work / "deployment_hold.json"
            hold_path.write_text('{"active": true}', encoding="utf-8")
            config = SimpleNamespace(work_path=work)
            shutdown_event = Mock()

            def release_hold_during_wait(_seconds):
                hold_path.unlink()
                return False

            shutdown_event.wait.side_effect = release_hold_during_wait
            with patch.object(main_module.time, "monotonic", side_effect=[100.0, 100.0]):
                stopped = main_module._wait_for_next_cycle_or_ai_resume(shutdown_event, 300, config)

            self.assertFalse(stopped)
            shutdown_event.wait.assert_called_once_with(2.0)

    def test_requeue_previous_worker_running_uses_queue_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Interrupted Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = SimpleNamespace(
                work_path=root / "work",
                scanner_state_path="scanner_state.sqlite3",
                scanner_cache_enabled=True,
                scanner_queue_enabled=True,
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, 10)
                state.mark_ai_queue_running(video)
                state.commit()
            finally:
                state.close()

            class FakeLogger:
                warnings = []

                def warning(self, *args, **kwargs):
                    self.warnings.append(args)

            logger = FakeLogger()
            count = main_module._requeue_previous_worker_running(config, logger)

            self.assertEqual(count, 1)
            self.assertTrue(logger.warnings)
            state = ScanStateStore.from_config(config)
            try:
                self.assertEqual(state.iter_ai_queue_candidates(), [video.resolve()])
            finally:
                state.close()

    def test_requeue_stale_ai_running_requeues_only_expired_rows_without_scanner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stale_video = root / "Stale Anime S01E01.mkv"
            fresh_video = root / "Fresh Anime S01E02.mkv"
            stale_video.write_bytes(b"stale")
            fresh_video.write_bytes(b"fresh")
            config = SimpleNamespace(
                work_path=root / "work",
                scanner_state_path="scanner_state.sqlite3",
                scanner_cache_enabled=True,
                scanner_queue_enabled=True,
                ai_queue_stage_stale_seconds=900,
            )
            state = ScanStateStore.from_config(config)
            try:
                with patch("scan_state.time.time", return_value=1000.0):
                    state.upsert_ai_queue_candidate(stale_video, stale_video.stat().st_mtime_ns)
                    obligation = state.ensure_ai_delivery_obligation(
                        stale_video,
                        media_size=stale_video.stat().st_size,
                        media_mtime_ns=stale_video.stat().st_mtime_ns,
                        policy_revision="stale-recovery-test-v1",
                    )
                    attempt = state.begin_ai_delivery_attempt(obligation["obligation_id"])
                    state.mark_ai_queue_running(stale_video)
                with patch("scan_state.time.time", return_value=1900.0):
                    state.upsert_ai_queue_candidate(fresh_video, fresh_video.stat().st_mtime_ns)
                    state.mark_ai_queue_running(fresh_video)
                state.commit()
            finally:
                state.close()

            with patch("scan_state.time.time", return_value=2000.0):
                count = main_module._requeue_stale_ai_running(config, Mock())

            self.assertEqual(count, 1)
            state = ScanStateStore.from_config(config)
            try:
                rows = dict(
                    state._conn.execute(
                        "SELECT path, status FROM ai_candidate_queue ORDER BY path"
                    ).fetchall()
                )
                self.assertEqual(rows[str(stale_video.resolve())], "queued")
                self.assertEqual(rows[str(fresh_video.resolve())], "running")
                stale_attempt = state.get_ai_delivery_attempt(attempt["attempt_id"])
                self.assertEqual(stale_attempt["status"], "deferred")
                self.assertEqual(stale_attempt["stage"], "stale_recovery")
                self.assertEqual(stale_attempt["error_code"], "stale_running_requeued")
            finally:
                state.close()

    def test_requeue_stale_ai_running_cli_returns_before_media_cleanup(self) -> None:
        config = SimpleNamespace(log_path="worker.log")
        logger = Mock()
        with (
            patch.object(
                sys,
                "argv",
                ["main.py", "--config", "config.yaml", "--requeue-stale-ai-running"],
            ),
            patch("config.load_config", return_value=config),
            patch("logger.setup_logging", return_value=logger),
            patch("backup_cleanup.cleanup_backup_files") as cleanup_backups,
            patch("work_cleanup.cleanup_work_artifacts") as cleanup_work,
            patch.object(main_module, "_install_shutdown_handler", return_value=Mock()),
            patch.object(main_module, "_run_safety_checks", return_value=True),
            patch.object(main_module, "_requeue_stale_ai_running", return_value=1) as requeue,
        ):
            result = main_module.main()

        self.assertEqual(result, 0)
        requeue.assert_called_once_with(config, logger)
        cleanup_backups.assert_not_called()
        cleanup_work.assert_not_called()

    def test_restart_requeues_source_only_manifest_without_zh_tw_delivery(self) -> None:
        from output_manifest import write_output_manifest

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Completed Anime S01E01.mkv"
            output = root / "Completed Anime S01E01.AIEnglish.en.ass"
            video.write_bytes(b"media")
            output.write_text("verified subtitle", encoding="utf-8")
            config = SimpleNamespace(
                work_path=root / "work",
                scanner_state_path="scanner_state.sqlite3",
                scanner_cache_enabled=True,
                scanner_queue_enabled=True,
                ai_output_manifest_path="manifests",
                control_state_path="control.sqlite3",
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
                attempt_id = main_module._mark_queue_running(state, video, config)
                write_output_manifest(
                    video,
                    config,
                    [output],
                    publication_kind="source_language",
                    output_languages=("en",),
                )
                state.update_ai_job_stage(video, "complete", "ok", "Finished video")
                state.commit()
            finally:
                state.close()

            self.assertEqual(main_module._requeue_previous_worker_running(config, Mock()), 1)

            state = ScanStateStore.from_config(config)
            try:
                attempt = state.get_ai_delivery_attempt(attempt_id)
                obligation = state.get_ai_delivery_obligation(attempt["obligation_id"])
                queue_status = state._conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
                self.assertEqual(queue_status, "queued")
                self.assertNotEqual(attempt["status"], "succeeded")
                self.assertNotEqual(obligation["state"], "succeeded")
            finally:
                state.close()

    def test_mikan_process_completed_once_applies_deferred_state_update_first(self) -> None:
        config = SimpleNamespace(mikan_extract_completed=True)
        logger = Mock()
        worker = Mock()

        with patch("mikan_worker.MikanWorker", return_value=worker):
            main_module._mikan_process_completed_once(config, logger)

        worker.consume_completed_state_update_request.assert_called_once_with()
        worker.process_completed_downloads.assert_called_once_with(required=False)

    def test_mikan_process_completed_once_skips_when_extract_disabled(self) -> None:
        config = SimpleNamespace(mikan_extract_completed=False)
        logger = Mock()

        with patch("mikan_worker.MikanWorker") as worker_class:
            main_module._mikan_process_completed_once(config, logger)

        worker_class.assert_not_called()

    def test_background_mikan_completed_loop_extracts_as_soon_as_completed_is_detected(self) -> None:
        config = SimpleNamespace(
            mikan_completed_poll_interval_seconds=300,
            mikan_active_poll_interval_seconds=1,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.side_effect = [False, True]
        worker = Mock()
        worker.poll_download_progress.side_effect = [
            SimpleNamespace(
                synced_progress_count=0,
                completed_pending_count=0,
                active_download_count=1,
            ),
            SimpleNamespace(
                synced_progress_count=0,
                completed_pending_count=1,
                active_download_count=0,
            ),
        ]
        worker.extract_dispatch_counts.return_value = (0, 0)

        with (
            patch("mikan_worker.MikanWorker", return_value=worker),
            patch.object(main_module.time, "monotonic", side_effect=[0.0, 0.0, 1.1, 1.1, 1.1, 1.1, 1.1]),
        ):
            main_module._background_mikan_completed_loop(config, logger, shutdown_event)

        self.assertEqual(worker.poll_download_progress.call_count, 2)
        self.assertEqual(
            worker.poll_download_progress.call_args_list[0],
            call(state_required=False, cached_mappings_only=True),
        )
        worker.process_queued_extract_jobs.assert_called_once_with(limit=1)
        worker.process_completed_downloads.assert_not_called()
        worker.consume_completed_state_update_request.assert_called_once_with()
        self.assertEqual(shutdown_event.wait.call_args_list[0].args[0], 1)
        logger.info.assert_any_call(
            "Completed Mikan/qB download detected and queued for immediate extraction. torrents=%s",
            1,
        )

    def test_background_mikan_completed_loop_fills_independent_extract_slots(self) -> None:
        config = SimpleNamespace(mikan_completed_poll_interval_seconds=300, mikan_extract_workers=2)
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        worker = Mock()
        worker.poll_download_progress.return_value = SimpleNamespace(
            synced_progress_count=0,
            completed_pending_count=2,
            claimable_extract_count=2,
            running_extract_count=0,
        )
        worker.extract_dispatch_counts.return_value = (0, 0)

        with patch("mikan_worker.MikanWorker", return_value=worker):
            main_module._background_mikan_completed_loop(config, logger, shutdown_event)

        self.assertEqual(worker.process_queued_extract_jobs.call_count, 2)
        self.assertEqual(
            worker.process_queued_extract_jobs.call_args_list,
            [call(limit=1), call(limit=1)],
        )
        worker.process_completed_downloads.assert_not_called()

    def test_background_mikan_completed_loop_reduces_disk_readers_during_ai(self) -> None:
        config = SimpleNamespace(
            mikan_completed_poll_interval_seconds=300,
            mikan_extract_workers=3,
            mikan_extract_workers_during_ai=1,
        )
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        worker = Mock()
        worker.poll_download_progress.return_value = SimpleNamespace(
            synced_progress_count=0,
            completed_pending_count=3,
            claimable_extract_count=3,
            running_extract_count=0,
        )
        worker.extract_dispatch_counts.return_value = (0, 0)

        with (
            patch("mikan_worker.MikanWorker", return_value=worker),
            patch.object(main_module, "_ai_processing_active", return_value=True),
        ):
            main_module._background_mikan_completed_loop(config, logger, shutdown_event)

        worker.process_queued_extract_jobs.assert_called_once_with(limit=1)

    def test_background_mikan_completed_loop_dispatches_ready_job_while_poll_is_blocked(self) -> None:
        config = SimpleNamespace(mikan_completed_poll_interval_seconds=300, mikan_extract_workers=1)
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.return_value = True
        worker = Mock()
        worker.extract_dispatch_counts.return_value = (1, 0)
        poll_release = threading.Event()

        def blocked_poll(**_kwargs):
            poll_release.wait(timeout=5)
            return SimpleNamespace(
                synced_progress_count=0,
                completed_pending_count=0,
                claimable_extract_count=0,
                running_extract_count=0,
            )

        worker.poll_download_progress.side_effect = blocked_poll
        try:
            with patch("mikan_worker.MikanWorker", return_value=worker):
                main_module._background_mikan_completed_loop(config, logger, shutdown_event)
        finally:
            poll_release.set()

        worker.process_queued_extract_jobs.assert_called_once_with(limit=1)

    def test_background_mikan_enqueue_loop_consumes_deferred_requests_every_second(self) -> None:
        config = SimpleNamespace(mikan_watch_interval_seconds=300)
        logger = Mock()
        shutdown_event = Mock()
        shutdown_event.is_set.return_value = False
        shutdown_event.wait.side_effect = [False, True]
        worker = Mock()

        with (
            patch("mikan_worker.MikanWorker", return_value=worker),
            patch.object(main_module.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
        ):
            main_module._background_mikan_enqueue_loop(config, logger, shutdown_event)

        worker.run_once.assert_called_once_with(process_completed=False)
        worker.consume_deferred_requests.assert_called_once_with()
        self.assertEqual(shutdown_event.wait.call_args_list[0].args[0], 1)

    def test_series_mutations_are_executed_by_worker_control_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            series = anime / "Example Series"
            work = root / "work"
            series.mkdir(parents=True)
            work.mkdir()
            config = SimpleNamespace(
                input_path=anime,
                work_path=work,
                series_metadata_path="series_metadata.sqlite3",
                video_extensions=[".mkv"],
            )

            matched = main_module._execute_control_command(
                config,
                Mock(),
                "series.match",
                str(series),
                {"provider": "anilist", "provider_id": "123", "title": "Example Anime"},
            )
            added = main_module._execute_control_command(
                config,
                Mock(),
                "series.glossary_upsert",
                str(series),
                {"source_text": "先輩", "target_text": "學長", "term_type": "name"},
            )

            from series_metadata import SeriesMetadataStore

            with SeriesMetadataStore.from_config(config) as store:
                profile = store.get_by_local_path(series)
                glossary = store.glossary(series)
            self.assertEqual(matched["provider_id"], "123")
            self.assertEqual(added["source_text"], "先輩")
            self.assertIsNotNone(profile)
            self.assertTrue(profile.locked)
            self.assertEqual(glossary, {"先輩": "學長"})

    def test_review_line_repair_closes_after_verified_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Season 1" / "Example - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "subtitle_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.resolve_review_item", return_value=True) as resolve_review,
                patch("subtitle_paths.has_ai_finished_subtitle", return_value=True) as finished,
                patch.object(main_module, "_run_ai_retranslate_lines_command", return_value="repaired") as repair,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.resolve_ai",
                    str(video),
                    {
                        "review_id": "review_test",
                        "remediation": "ai.retranslate_lines",
                        "lines": "3,7-9",
                    },
                )

            repair.assert_called_once_with(config, video.resolve(), lines="3,7-9")
            finished.assert_called_once_with(video.resolve(), config)
            resolve_review.assert_called_once()
            self.assertTrue(result["resolved"])
            self.assertFalse(result["queued"])
            self.assertEqual(result["output"], "repaired")

    def test_line_repair_asr_review_parser_accepts_only_exact_structured_error(self) -> None:
        direct = (
            "Traceback (most recent call last):\n"
            "translator.AsrReviewError: ASR review requested for subtitle index 98: "
            "source transcription is unreliable\n"
        )
        self.assertEqual(
            main_module._line_repair_asr_review_index(RuntimeError(direct)),
            98,
        )
        staged = (
            "2026-08-28 07:38:32,123 [WARNING] Translation batch 98.1 staged unresolved "
            "line at index 98 after 3 repair attempt(s); publication will be blocked: "
            "source='る' output='__ASR_REVIEW__' reason=ASR review requested for subtitle "
            "index 98: source transcription is unreliable\n"
        )
        chained = (
            staged
            + "Traceback (most recent call last):\n"
            "subtitle_quality.SubtitleQualityError: Translation quality event blocks "
            "publication: indexes=[98]\n"
        )
        self.assertEqual(
            main_module._line_repair_asr_review_index(RuntimeError(chained)),
            98,
        )
        self.assertEqual(
            main_module._line_repair_asr_review_index(
                RuntimeError(
                    "RuntimeError: ASR review requested for subtitle index 98: "
                    "source transcription is unreliable"
                )
            ),
            98,
        )
        self.assertIsNone(
            main_module._line_repair_asr_review_index(
                RuntimeError(direct.rstrip() + " automatic fallback suggested")
            )
        )
        self.assertIsNone(
            main_module._line_repair_asr_review_index(
                RuntimeError(
                    "translator.AsrReviewError: ASR review requested for subtitle index 98: "
                    "source transcription is unreliable\n"
                    "subtitle_quality.SubtitleQualityError: Translation quality event blocks "
                    "publication: indexes=[98]"
                )
            )
        )
        self.assertIsNone(
            main_module._line_repair_asr_review_index(
                RuntimeError(
                    "2026-08-28 07:38:32,123 [WARNING] Translation batch 98.1 staged unresolved "
                    "line at index 98 after 3 repair attempt(s); publication will be blocked: "
                    "source='る' output='__ASR_REVIEW__' reason=ASR review requested for subtitle "
                    "index 99: source transcription is unreliable\n"
                    "subtitle_quality.SubtitleQualityError: Translation quality event blocks "
                    "publication: indexes=[98]"
                )
            )
        )
        self.assertIsNone(
            main_module._line_repair_asr_review_index(
                RuntimeError(
                    chained.replace(
                        "Traceback (most recent call last):\n",
                        staged.replace("98", "99")
                        + "Traceback (most recent call last):\n",
                        1,
                    )
                )
            )
        )
        self.assertIsNone(
            main_module._line_repair_asr_review_index(
                RuntimeError(
                    chained.rstrip()
                    + "\nunrelated terminal failure"
                )
            )
        )
        self.assertIsNone(
            main_module._line_repair_asr_review_index(
                RuntimeError(
                    chained.replace(
                        "publication: indexes=[98]",
                        "publication: indexes=[98, 99]",
                    )
                )
            )
        )
        self.assertIsNone(
            main_module._line_repair_asr_review_index(
                RuntimeError(
                    "ASR review requested for subtitle index 98: "
                    "source transcription may be unreliable"
                )
            )
        )

    def test_automatic_translation_omission_review_requeues_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Season 1" / "Example - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 2,
                "failure_revision": "translation-revision",
                "job_stage": "quality_check",
                "last_error": "Translation safe-omission remained after bounded same-job recovery: indexes=[4, 8]",
            }
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch.object(main_module, "_run_ai_reprocess_command", return_value="queued") as reprocess,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.resolve_ai",
                    str(video),
                    {
                        "review_id": "review_test",
                        "remediation": "ai.retranslate",
                        "automatic_review": True,
                        "policy_revision": main_module._AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY,
                        "expected_failure_revision": "translation-revision",
                    },
                )

            reprocess.assert_called_once_with(
                config,
                video.resolve(),
                mode="retranslate",
                queue_mode="auto_review",
                expected_failure_revision="translation-revision",
                policy_revision=main_module._AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY,
            )
            queue_state.close.assert_called_once()
            self.assertTrue(result["queued"])
            self.assertFalse(result["resolved"])

    def test_automatic_omission_line_repair_revalidates_exact_indexes_and_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Season 1" / "Example - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 4,
                "failure_revision": "current-line-revision",
                "job_stage": "quality_check",
                "last_error": (
                    "Translation safe-omission remained after bounded same-job recovery: "
                    "indexes=[8, 4, 8]"
                ),
            }
            queue_state.running_ai_queue_count.return_value = 0
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.resolve_review_item", return_value=True) as resolve_review,
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch("subtitle_paths.has_ai_finished_subtitle", return_value=True) as finished,
                patch.object(
                    main_module,
                    "_run_ai_retranslate_lines_command",
                    return_value="repaired",
                ) as repair,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.resolve_ai",
                    str(video),
                    {
                        "review_id": "review_test",
                        "remediation": "ai.retranslate_lines",
                        "lines": "4,8",
                        "automatic_review": True,
                        "policy_revision": (
                            main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY
                        ),
                        "expected_failure_revision": "current-line-revision",
                    },
                )

            repair.assert_called_once_with(config, video.resolve(), lines="4,8")
            finished.assert_called_once_with(video.resolve(), config)
            resolve_review.assert_called_once()
            queue_state.close.assert_called_once()
            self.assertTrue(result["resolved"])
            self.assertFalse(result["queued"])

    def test_automatic_omission_line_repair_escalates_exact_asr_review_to_full_retranscribe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Season 1" / "Example - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            snapshot = {
                "status": "paused",
                "attempts": 4,
                "failure_revision": "current-line-revision",
                "job_stage": "quality_check",
                "last_error": (
                    "Translation safe-omission remained after bounded same-job recovery: "
                    "indexes=[98]"
                ),
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.side_effect = [snapshot, snapshot]
            queue_state.running_ai_queue_count.side_effect = [0, 0]
            resolve_review = Mock()
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.resolve_review_item", resolve_review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch("subtitle_paths.has_ai_finished_subtitle") as finished,
                patch.object(
                    main_module,
                    "_run_ai_retranslate_lines_command",
                    side_effect=RuntimeError(
                        "2026-08-28 07:38:32,123 [WARNING] Translation batch 98.1 staged "
                        "unresolved line at index 98 after 3 repair attempt(s); publication "
                        "will be blocked: source='る' output='__ASR_REVIEW__' reason=ASR review "
                        "requested for subtitle index 98: source transcription is unreliable\n"
                        "Traceback (most recent call last):\n"
                        "subtitle_quality.SubtitleQualityError: Translation quality event blocks "
                        "publication: indexes=[98]\n"
                    ),
                ) as repair,
                patch.object(
                    main_module,
                    "_run_ai_reprocess_command",
                    return_value="full ASR queued",
                ) as reprocess,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.resolve_ai",
                    str(video),
                    {
                        "review_id": "review_test",
                        "remediation": "ai.retranslate_lines",
                        "lines": "98",
                        "automatic_review": True,
                        "policy_revision": (
                            main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY
                        ),
                        "expected_failure_revision": "current-line-revision",
                    },
                )

            repair.assert_called_once_with(config, video.resolve(), lines="98")
            reprocess.assert_called_once_with(
                config,
                video.resolve(),
                mode="retranscribe",
                queue_mode="auto_review",
                expected_failure_revision="current-line-revision",
                policy_revision=main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
            )
            finished.assert_not_called()
            resolve_review.assert_not_called()
            self.assertFalse(result["resolved"])
            self.assertTrue(result["queued"])
            self.assertEqual(result["output"], "full ASR queued")

    def test_automatic_omission_line_repair_rejects_stale_failure_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Season 1" / "Example - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 4,
                "failure_revision": "current-line-revision",
                "job_stage": "quality_check",
                "last_error": (
                    "Translation safe-omission remained after bounded same-job recovery: "
                    "indexes=[98]"
                ),
            }
            queue_state.running_ai_queue_count.return_value = 0
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch.object(main_module, "_run_ai_retranslate_lines_command") as repair,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "current revision-bound omission evidence",
                ):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.resolve_ai",
                        str(video),
                        {
                            "review_id": "review_test",
                            "remediation": "ai.retranslate_lines",
                            "lines": "98",
                            "automatic_review": True,
                            "policy_revision": (
                                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY
                            ),
                            "expected_failure_revision": "stale-line-revision",
                        },
                    )

            repair.assert_not_called()
            queue_state.close.assert_called_once()

    def test_automatic_omission_line_repair_refuses_running_ai_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Season 1" / "Example - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 4,
                "failure_revision": "current-line-revision",
                "job_stage": "quality_check",
                "last_error": (
                    "Translation safe-omission remained after bounded same-job recovery: "
                    "indexes=[98]"
                ),
            }
            queue_state.running_ai_queue_count.return_value = 1
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch.object(main_module, "_run_ai_retranslate_lines_command") as repair,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "another AI queue job is running",
                ):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.resolve_ai",
                        str(video),
                        {
                            "review_id": "review_test",
                            "remediation": "ai.retranslate_lines",
                            "lines": "98",
                            "automatic_review": True,
                            "policy_revision": (
                                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY
                            ),
                            "expected_failure_revision": "current-line-revision",
                        },
                    )

            repair.assert_not_called()
            queue_state.close.assert_called_once()

    def test_automatic_omission_line_repair_refuses_asr_escalation_for_other_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 4,
                "failure_revision": "current-line-revision",
                "job_stage": "quality_check",
                "last_error": (
                    "Translation safe-omission remained after bounded same-job recovery: "
                    "indexes=[98]"
                ),
            }
            queue_state.running_ai_queue_count.return_value = 0
            resolve_review = Mock()
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.resolve_review_item", resolve_review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch.object(main_module, "_run_ai_reprocess_command") as reprocess,
                patch.object(
                    main_module,
                    "_run_ai_retranslate_lines_command",
                    side_effect=RuntimeError(
                        "translator.AsrReviewError: ASR review requested for subtitle index 99: "
                        "source transcription is unreliable"
                    ),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "subtitle index 99"):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.resolve_ai",
                        str(video),
                        {
                            "review_id": "review_test",
                            "remediation": "ai.retranslate_lines",
                            "lines": "98",
                            "automatic_review": True,
                            "policy_revision": (
                                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY
                            ),
                            "expected_failure_revision": "current-line-revision",
                        },
                    )

            resolve_review.assert_not_called()
            reprocess.assert_not_called()
            self.assertEqual(
                queue_state.method_calls,
                [
                    call.ai_queue_candidate_snapshot(video.resolve()),
                    call.running_ai_queue_count(),
                    call.close(),
                ],
            )

    def test_line_remediation_v4_reopens_consumed_v3_revision_budget_once(self) -> None:
        from control_state import (
            claim_next_command,
            enqueue_command,
            finish_command,
            review_autopilot_revision_attempt_allowed,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(work_path=Path(temp_dir))
            review_id = "review_" + "9" * 24
            failure_revision = "same-failure-revision"
            v1_prefix = main_module._review_autopilot_prefix(
                "translation-omission-lines-v1",
                "review.resolve_ai",
            )
            enqueue_command(
                config,
                action="review.resolve_ai",
                target="/anime/Example.mkv",
                parameters={
                    "review_id": review_id,
                    "remediation": "ai.retranslate_lines",
                },
                idempotency_key=f"{v1_prefix}{review_id}:{failure_revision}",
            )
            command = claim_next_command(config, worker_id="test-worker")
            self.assertIsNotNone(command)
            finish_command(
                config,
                command.command_id,
                error="automatic line remediation refused while another AI queue job is running",
            )

            self.assertFalse(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=v1_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=1,
                )
            )
            v2_prefix = main_module._review_autopilot_prefix(
                "translation-omission-lines-v2",
                "review.resolve_ai",
            )
            self.assertTrue(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=v2_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=1,
                )
            )
            enqueue_command(
                config,
                action="review.resolve_ai",
                target="/anime/Example.mkv",
                parameters={
                    "review_id": review_id,
                    "remediation": "ai.retranslate_lines",
                },
                idempotency_key=f"{v2_prefix}{review_id}:{failure_revision}",
            )
            v2_command = claim_next_command(config, worker_id="test-worker")
            self.assertIsNotNone(v2_command)
            finish_command(config, v2_command.command_id, error="targeted repair failed")
            self.assertFalse(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=v2_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=1,
                )
            )
            v3_prefix = main_module._review_autopilot_prefix(
                "translation-omission-lines-v3",
                "review.resolve_ai",
            )
            self.assertTrue(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=v3_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=1,
                )
            )
            enqueue_command(
                config,
                action="review.resolve_ai",
                target="/anime/Example.mkv",
                parameters={
                    "review_id": review_id,
                    "remediation": "ai.retranslate_lines",
                },
                idempotency_key=f"{v3_prefix}{review_id}:{failure_revision}",
            )
            v3_command = claim_next_command(config, worker_id="test-worker")
            self.assertIsNotNone(v3_command)
            finish_command(config, v3_command.command_id, error="ASR escalation parser missed")
            self.assertFalse(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=v3_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=1,
                )
            )
            self.assertEqual(
                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
                "translation-omission-lines-v4",
            )
            v4_prefix = main_module._review_autopilot_prefix(
                main_module._AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
                "review.resolve_ai",
            )
            self.assertTrue(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=v4_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=1,
                )
            )
            v4 = enqueue_command(
                config,
                action="review.resolve_ai",
                target="/anime/Example.mkv",
                parameters={
                    "review_id": review_id,
                    "remediation": "ai.retranslate_lines",
                },
                idempotency_key=f"{v4_prefix}{review_id}:{failure_revision}",
            )
            self.assertEqual(
                main_module._active_translation_omission_line_command(config)["command_id"],
                v4["command_id"],
            )
            v4_command = claim_next_command(config, worker_id="test-worker")
            self.assertIsNotNone(v4_command)
            finish_command(config, v4_command.command_id, error="targeted repair failed")
            self.assertFalse(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=v4_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=1,
                )
            )

    def test_normal_queue_claim_yields_to_reserved_line_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime - S01E01.mkv"
            video.write_bytes(b"media")
            scanner = Mock()
            scanner.queued_candidates.return_value = [video]
            scanner.last_database_error = ""
            scanner.last_database_error_code = ""
            worker = Mock()
            worker.config = SimpleNamespace(
                work_path=root / "work",
                max_concurrent_videos=1,
                resource_admission_enabled=False,
            )
            queue_state = Mock()
            reserved = {
                "command_id": "cmd_reserved",
                "target": str(root / "Paused - S01E02.mkv"),
                "status": "queued",
            }

            with (
                patch.object(main_module, "_deployment_hold_active", return_value=False),
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(main_module, "_open_ai_queue_state", return_value=queue_state),
                patch.object(
                    main_module,
                    "_active_translation_omission_line_command",
                    return_value=reserved,
                ),
                patch.object(main_module, "_process_video_with_policy") as process,
            ):
                processed = main_module._scan_and_process(
                    scanner,
                    worker,
                    Mock(),
                    queue_only=True,
                )

            self.assertEqual(processed, 0)
            queue_state.mark_ai_queue_running.assert_not_called()
            process.assert_not_called()
            queue_state.close.assert_called_once()

    def test_review_handoff_logs_one_yield_for_repeated_active_reservation(self) -> None:
        reserved = {
            "command_id": "cmd_reserved",
            "target": "/anime/Paused - S01E02.mkv",
            "status": "queued",
        }
        logger = Mock()

        with (
            patch.object(
                main_module,
                "_AI_REVIEW_REMEDIATION_LOGGED_RESERVATION",
                None,
            ),
            patch.object(
                main_module,
                "_active_translation_omission_line_command",
                return_value=reserved,
            ) as active,
            patch.object(main_module, "_advance_ai_quality_review_autopilot") as advance,
        ):
            first_yield = main_module._yield_normal_ai_queue_to_review_remediation(
                SimpleNamespace(),
                logger,
            )
            second_yield = main_module._yield_normal_ai_queue_to_review_remediation(
                SimpleNamespace(),
                logger,
            )

        self.assertTrue(first_yield)
        self.assertTrue(second_yield)
        self.assertEqual(active.call_count, 2)
        advance.assert_not_called()
        logger.info.assert_called_once_with(
            "Normal AI queue yielded to reserved automatic line remediation. "
            "command=%s path=%s",
            "cmd_reserved",
            "/anime/Paused - S01E02.mkv",
        )

    def test_continuous_drain_runs_review_handoff_before_next_scan(self) -> None:
        config = SimpleNamespace(
            auto_ai_drain_queue_between_cycles=True,
            auto_enable_ai_fallback=True,
        )
        with (
            patch.object(main_module, "_mikan_redownload_blocks_ai", return_value=False),
            patch.object(
                main_module,
                "_yield_normal_ai_queue_to_review_remediation",
                return_value=True,
            ) as handoff,
            patch.object(main_module, "_scan_and_process") as scan,
        ):
            main_module._drain_ai_queue_between_cycles(
                Mock(),
                Mock(),
                Mock(),
                config,
            )

        handoff.assert_called_once()
        scan.assert_not_called()

    def test_automatic_full_asr_review_revalidates_failure_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Example - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "asr-revision",
                "last_error_code": "deterministic_asr_quality",
            }
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch.object(
                    main_module,
                    "_run_ai_reprocess_command",
                    return_value="queued",
                ) as reprocess,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.resolve_ai",
                    str(video),
                    {
                        "review_id": "review_test",
                        "remediation": "ai.retranscribe",
                        "automatic_review": True,
                        "policy_revision": main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
                        "expected_failure_revision": "asr-revision",
                    },
                )

            self.assertTrue(result["queued"])
            reprocess.assert_called_once_with(
                config,
                video.resolve(),
                mode="retranscribe",
                queue_mode="auto_review",
                expected_failure_revision="asr-revision",
                policy_revision=main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
            )
            queue_state.close.assert_called_once()

    def test_automatic_full_asr_review_rejects_translation_failure_at_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Example - S01E02.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "asr_quality",
                "status": "open",
                "diagnosis": {"video": str(video.resolve())},
            }
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "translation-revision",
                "last_error_code": "translation_safe_omission",
                "job_stage": "quality_check",
            }
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch.object(main_module, "_run_ai_reprocess_command") as reprocess,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "current revision-bound evidence",
                ):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.resolve_ai",
                        str(video),
                        {
                            "review_id": "review_test",
                            "remediation": "ai.retranscribe",
                            "automatic_review": True,
                            "policy_revision": main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
                            "expected_failure_revision": "translation-revision",
                        },
                    )

            reprocess.assert_not_called()
            queue_state.close.assert_called_once()

    def test_automatic_aligned_timing_review_requires_exact_review_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            video = anime / "Example" / "Season 1" / "Example - S01E03.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "review_id": "review_test",
                "target_key": str(video.resolve()),
                "updated_at": 456.0,
                "kind": "subtitle_quality",
                "status": "open",
                "diagnosis": {
                    "video": str(video.resolve()),
                    "stage": "quality_check",
                    "reports": [
                        {
                            "role": "japanese",
                            "status": "rerun",
                            "issues": [
                                {"code": "short_duration", "severity": "warn", "indexes": [75]},
                                {"code": "cps_too_high", "severity": "fail", "indexes": [75]},
                            ],
                        }
                    ],
                },
            }
            evidence = main_module._exact_aligned_timing_review_evidence(review)
            self.assertIsNotNone(evidence)
            queue_state = Mock()
            queue_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "attempts": 2,
                "failure_revision": "timing-revision",
                "job_stage": "quality_check",
            }
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("scan_state.ScanStateStore.from_config", return_value=queue_state),
                patch.object(main_module, "_run_ai_reprocess_command", return_value="queued") as reprocess,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.resolve_ai",
                    str(video),
                    {
                        "review_id": "review_test",
                        "remediation": "ai.retranslate",
                        "automatic_review": True,
                        "policy_revision": main_module._AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY,
                        "expected_failure_revision": "timing-revision",
                        "expected_review_evidence_revision": evidence["evidence_revision"],
                    },
                )

            reprocess.assert_called_once_with(
                config,
                video.resolve(),
                mode="retranslate",
                queue_mode="auto_review",
                expected_failure_revision="timing-revision",
                policy_revision=main_module._AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY,
            )
            queue_state.close.assert_called_once()
            self.assertTrue(result["queued"])

    def test_review_dismiss_command_only_closes_source_review(self) -> None:
        review = {
            "kind": "target_ambiguity",
            "status": "open",
            "diagnosis": {"torrent_hash": "a" * 40},
        }
        with (
            patch("control_state.get_review_item", return_value=review),
            patch("control_state.dismiss_review_item", return_value=True) as dismiss_review,
        ):
            result = main_module._execute_control_command(
                SimpleNamespace(),
                Mock(),
                "review.dismiss",
                "review_test",
                {"review_id": "review_test"},
            )

        dismiss_review.assert_called_once_with(ANY, "review_test")
        self.assertTrue(result["dismissed"])
        self.assertFalse(result["media_deleted"])
        self.assertFalse(result["subtitle_deleted"])
        self.assertFalse(result["torrent_deleted"])

    def test_review_dismiss_command_rejects_quality_review(self) -> None:
        with patch(
            "control_state.get_review_item",
            return_value={"kind": "subtitle_quality", "status": "open"},
        ):
            with self.assertRaisesRegex(ValueError, "only target ambiguity"):
                main_module._execute_control_command(
                    SimpleNamespace(),
                    Mock(),
                    "review.dismiss",
                    "review_quality",
                    {"review_id": "review_quality"},
                )

    def test_review_target_command_derives_mapping_from_exact_stored_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            series = anime / "Non Non Biyori"
            candidate = series / "Season 3" / "Non Non Biyori - S03E01.mkv"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "diagnosis": {"bangumi_ids": [2402], "torrent_hash": "a" * 40},
                "candidates": [{
                    "path": str(candidate),
                    "season": 3,
                    "score": 1661,
                    "reasons": ["episode", "title_contains"],
                }],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_series_source_mapping") as upsert_mapping,
                patch("control_state.resolve_review_item", return_value=True) as resolve_review,
                patch("control_state.resolve_sibling_target_reviews", return_value=[]) as resolve_siblings,
                patch(
                    "mikan_worker.resume_target_ambiguity_source",
                    return_value={
                        "mode": "qbit_present",
                        "restored_pending": 1,
                        "requeued": 1,
                        "waiting_download": 0,
                    },
                ) as resume_source,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.resolve_target",
                    "review_test",
                    {
                        "review_id": "review_test",
                        "candidate_path": str(candidate),
                        "series_path": str(series),
                        "source_id": "2402",
                        "season": 3,
                    },
                )

            self.assertTrue(result["resolved"])
            upsert_mapping.assert_called_once_with(
                config,
                source="mikan",
                source_id="2402",
                season=3,
                series_path=str(series.resolve()),
                series_id="",
                confidence=1.0,
                locked=True,
            )
            resume_source.assert_called_once_with(
                config,
                bangumi_id=2402,
                torrent_hash="a" * 40,
                diagnosis=review["diagnosis"],
            )
            self.assertEqual(result["source_resume"]["mode"], "qbit_present")
            self.assertEqual(result["restored_pending"], 1)
            self.assertEqual(result["requeued"], 1)
            resolution = resolve_review.call_args.args[2]
            self.assertEqual(resolution["candidate_path"], str(candidate.resolve()))
            self.assertEqual(resolution["season"], 3)
            resolve_siblings.assert_called_once_with(
                config,
                torrent_hash="a" * 40,
                exclude_review_id="review_test",
                resolution=resolution,
            )

    def test_review_target_command_does_not_resolve_when_source_resume_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            series = anime / "Non Non Biyori"
            candidate = series / "Season 3" / "Non Non Biyori - S03E01.mkv"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "diagnosis": {"bangumi_ids": [2402], "torrent_hash": "a" * 40},
                "candidates": [{
                    "path": str(candidate),
                    "season": 3,
                    "score": 1661,
                    "reasons": ["episode", "title_contains"],
                }],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_series_source_mapping") as upsert_mapping,
                patch("control_state.resolve_review_item") as resolve_review,
                patch(
                    "mikan_worker.resume_target_ambiguity_source",
                    side_effect=RuntimeError("qB add rejected"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "qB add rejected"):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.resolve_target",
                        "review_test",
                        {
                            "review_id": "review_test",
                            "candidate_path": str(candidate),
                            "source_id": "2402",
                        },
                    )

            resolve_review.assert_not_called()
            upsert_mapping.assert_not_called()

    def test_review_target_command_keeps_review_open_without_durable_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            candidate = anime / "Show" / "Season 1" / "Show - S01E01.mkv"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "diagnosis": {"bangumi_ids": [1234], "torrent_hash": "a" * 40},
                "candidates": [{
                    "path": str(candidate),
                    "season": 1,
                    "reasons": ["title_verified", "episode_exact"],
                }],
            }
            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_series_source_mapping") as upsert_mapping,
                patch("control_state.resolve_review_item") as resolve_review,
                patch(
                    "mikan_worker.resume_target_ambiguity_source",
                    return_value={
                        "mode": "qbit_present",
                        "restored_pending": 0,
                        "requeued": 0,
                        "waiting_download": 0,
                    },
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "no durable pending"):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.resolve_target",
                        "review_test",
                        {
                            "review_id": "review_test",
                            "candidate_path": str(candidate),
                            "source_id": "1234",
                        },
                    )

            upsert_mapping.assert_not_called()
            resolve_review.assert_not_called()

    def test_review_candidate_rebuild_uses_selected_profile_and_real_episode(self) -> None:
        from series_metadata import SeriesMetadataStore, SeriesProfile, canonical_local_path, stable_series_id

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            series = anime / "BOFURI - I Don't Want to Get Hurt, so I'll Max Out My Defense"
            episode = series / "Season 2" / "BOFURI - S02E01.mkv"
            episode.parent.mkdir(parents=True)
            episode.write_bytes(b"")
            config = SimpleNamespace(
                input_path=anime,
                work_path=root / "work",
                series_metadata_db_path="series_metadata.sqlite3",
                video_extensions=[".mkv"],
            )
            with SeriesMetadataStore.from_config(config) as store:
                store.upsert_profile(SeriesProfile(
                    local_path=str(series),
                    canonical_title="BOFURI",
                    provider="anilist",
                    provider_id="106479",
                    mikan_bangumi_id=2911,
                    aliases=["Bofuri 2"],
                ))
            series_id = stable_series_id(canonical_local_path(series))
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "target_key": "torrent:bofuri",
                "summary": "Choose Bofuri target",
                "severity": "error",
                "diagnosis": {
                    "bangumi_ids": [260],
                    "torrent_hash": "b" * 40,
                    "torrent_name": "[LoliHouse] Bofuri 2 [01-12][WebRip 1080p]",
                },
                "candidates": [],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_review_item", return_value="review_bofuri") as upsert_review,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.rebuild_target_candidates",
                    "review_bofuri",
                    {
                        "review_id": "review_bofuri",
                        "series_id": series_id,
                        "season": 2,
                    },
                )

            self.assertEqual(result["source_id"], "2911")
            self.assertEqual(result["episode"], 1)
            self.assertEqual(result["candidate_count"], 1)
            persisted = upsert_review.call_args.kwargs
            self.assertEqual(persisted["diagnosis"]["bangumi_ids"][:2], [2911, 260])
            self.assertEqual(persisted["candidates"][0]["path"], str(episode.resolve()))
            self.assertEqual(persisted["candidates"][0]["series_id"], series_id)
            self.assertIn("manual_mapping", persisted["candidates"][0]["reasons"])

    def test_review_candidate_rebuild_rejects_empty_selected_season(self) -> None:
        from series_metadata import SeriesMetadataStore, SeriesProfile, canonical_local_path, stable_series_id

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            series = anime / "Bofuri"
            (series / "Season 2").mkdir(parents=True)
            config = SimpleNamespace(
                input_path=anime,
                work_path=root / "work",
                series_metadata_db_path="series_metadata.sqlite3",
                video_extensions=[".mkv"],
            )
            with SeriesMetadataStore.from_config(config) as store:
                store.upsert_profile(SeriesProfile(
                    local_path=str(series),
                    canonical_title="Bofuri",
                    provider="mikan",
                    provider_id="2911",
                    mikan_bangumi_id=2911,
                ))
            series_id = stable_series_id(canonical_local_path(series))
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "target_key": "torrent:bofuri-empty",
                "summary": "Choose Bofuri target",
                "diagnosis": {"episode": 1, "torrent_hash": "c" * 40},
                "candidates": [],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_review_item") as upsert_review,
            ):
                with self.assertRaisesRegex(ValueError, "no video for episode 1"):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.rebuild_target_candidates",
                        "review_empty",
                        {"review_id": "review_empty", "series_id": series_id, "season": 2},
                    )

            upsert_review.assert_not_called()

    def test_review_candidate_auto_rebuild_finds_one_title_matched_real_episode(self) -> None:
        from series_metadata import SeriesMetadataStore, SeriesProfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            real_series = anime / "BOFURI - I Don't Want to Get Hurt, so I'll Max Out My Defense"
            stale_series = anime / "Bofuri - stale"
            wrong_series = anime / "Different Show"
            episode = real_series / "Season 2" / "BOFURI - S02E01.mkv"
            episode.parent.mkdir(parents=True)
            episode.write_bytes(b"")
            (stale_series / "Season 2").mkdir(parents=True)
            wrong_episode = wrong_series / "Season 2" / "Different Show - S02E01.mkv"
            wrong_episode.parent.mkdir(parents=True)
            wrong_episode.write_bytes(b"")
            config = SimpleNamespace(
                input_path=anime,
                work_path=root / "work",
                series_metadata_db_path="series_metadata.sqlite3",
                video_extensions=[".mkv"],
            )
            with SeriesMetadataStore.from_config(config) as store:
                store.upsert_profile(SeriesProfile(
                    local_path=str(real_series),
                    canonical_title="BOFURI",
                    provider="mikan",
                    provider_id="2911",
                    mikan_bangumi_id=2911,
                ))
                store.upsert_profile(SeriesProfile(
                    local_path=str(stale_series),
                    canonical_title="Bofuri",
                    provider="mikan",
                    provider_id="2911",
                    mikan_bangumi_id=2911,
                ))
                store.upsert_profile(SeriesProfile(
                    local_path=str(wrong_series),
                    canonical_title="Different Show",
                    provider="mikan",
                    provider_id="260",
                    mikan_bangumi_id=260,
                ))
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "target_key": "torrent:bofuri-auto",
                "summary": "Bofuri mapping needs review",
                "diagnosis": {
                    "bangumi_ids": [260, 2911],
                    "torrent_hash": "d" * 40,
                    "torrent_name": "[LoliHouse] Bofuri 2 [01-12][WebRip 1080p]",
                },
                "candidates": [],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_review_item", return_value="review_bofuri_auto") as upsert_review,
                patch.object(
                    main_module,
                    "_resolve_automatic_target_review",
                    return_value={"resolved": True, "requeued": 1},
                ) as resolve_auto,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.auto_rebuild_target_candidates",
                    "review_bofuri_auto",
                    {"review_id": "review_bofuri_auto"},
                )

            self.assertTrue(result["auto_selected"])
            self.assertEqual(result["source_id"], "2911")
            self.assertEqual(result["season"], 2)
            self.assertEqual(result["candidate_path"], str(episode.resolve()))
            persisted = upsert_review.call_args.kwargs
            self.assertEqual(len(persisted["candidates"]), 1)
            self.assertEqual(persisted["candidates"][0]["path"], str(episode.resolve()))
            self.assertIn("series_mapping:auto_review_recovery", persisted["candidates"][0]["reasons"])
            self.assertTrue(persisted["replace_candidates"])
            self.assertNotIn("manual_mapping", persisted["candidates"][0]["reasons"])
            self.assertEqual(persisted["diagnosis"]["recovery"]["method"], "automatic")
            self.assertTrue(result["auto_resolved"])
            self.assertTrue(result["resolution"]["resolved"])
            self.assertEqual(resolve_auto.call_args.kwargs["original_source_ids"], {260, 2911})

    def test_review_candidate_auto_rebuild_can_suggest_unique_profile_outside_wrong_source_id(self) -> None:
        from series_metadata import SeriesMetadataStore, SeriesProfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            wrong_series = anime / "BanG Dream!"
            correct_series = anime / "BanG Dream! Ave Mujica"
            wrong_episode = wrong_series / "Season 1" / "BanG Dream! - S01E05.mkv"
            correct_episode = correct_series / "Season 1" / "BanG Dream! Ave Mujica - S01E05.mkv"
            wrong_episode.parent.mkdir(parents=True)
            correct_episode.parent.mkdir(parents=True)
            wrong_episode.write_bytes(b"")
            correct_episode.write_bytes(b"")
            config = SimpleNamespace(
                input_path=anime,
                work_path=root / "work",
                series_metadata_db_path="series_metadata.sqlite3",
                video_extensions=[".mkv"],
            )
            with SeriesMetadataStore.from_config(config) as store:
                store.upsert_profile(SeriesProfile(
                    local_path=str(wrong_series),
                    canonical_title="BanG Dream!",
                    provider="mikan",
                    provider_id="2218",
                    mikan_bangumi_id=2218,
                ))
                store.upsert_profile(SeriesProfile(
                    local_path=str(correct_series),
                    canonical_title="BanG Dream! Ave Mujica",
                    provider="mikan",
                    provider_id="3518",
                    mikan_bangumi_id=3518,
                ))
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "target_key": "torrent:ave-mujica-auto",
                "summary": "Ave Mujica mapping needs review",
                "diagnosis": {
                    "bangumi_ids": [2218],
                    "torrent_hash": "e" * 40,
                    "torrent_name": "[Group] BanG Dream! Ave Mujica - 05 [WebRip 1080p]",
                },
                "candidates": [],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_review_item", return_value="review_ave_auto") as upsert_review,
            ):
                result = main_module._execute_control_command(
                    config,
                    Mock(),
                    "review.auto_rebuild_target_candidates",
                    "review_ave_auto",
                    {"review_id": "review_ave_auto"},
                )

            self.assertTrue(result["auto_selected"])
            self.assertEqual(result["source_id"], "3518")
            self.assertEqual(result["candidate_path"], str(correct_episode.resolve()))
            persisted = upsert_review.call_args.kwargs
            self.assertEqual(persisted["candidates"][0]["path"], str(correct_episode.resolve()))
            self.assertEqual(persisted["diagnosis"]["bangumi_ids"], [3518, 2218])
            self.assertEqual(persisted["diagnosis"]["recovery"]["method"], "automatic")
            self.assertTrue(persisted["replace_candidates"])
            self.assertFalse(result["auto_resolved"])
            self.assertEqual(result["resolution_skipped"], "source_not_in_original_diagnosis")

    def test_review_profile_title_score_does_not_treat_semantic_suffix_as_exact(self) -> None:
        profile = SimpleNamespace(
            canonical_title="The Quintessential Quintuplets",
            local_path="/anime/The Quintessential Quintuplets",
            titles=[],
            aliases=["5Hanayome"],
        )

        score = main_module._review_profile_title_score(profile, "5Hanayome SP")

        self.assertLess(score, main_module._REVIEW_PROFILE_EXACT_TITLE_SCORE)

    def test_review_candidate_auto_rebuild_refuses_multiple_real_targets(self) -> None:
        from series_metadata import SeriesMetadataStore, SeriesProfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            series = anime / "Example Anime"
            first = series / "Season 1" / "Example Anime - S01E01.mkv"
            second = series / "Season 2" / "Example Anime - S02E01.mkv"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"")
            second.write_bytes(b"")
            config = SimpleNamespace(
                input_path=anime,
                work_path=root / "work",
                series_metadata_db_path="series_metadata.sqlite3",
                video_extensions=[".mkv"],
            )
            with SeriesMetadataStore.from_config(config) as store:
                store.upsert_profile(SeriesProfile(
                    local_path=str(series),
                    canonical_title="Example Anime",
                    provider="mikan",
                    provider_id="1234",
                    mikan_bangumi_id=1234,
                ))
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "summary": "Example Anime target needs review",
                "diagnosis": {
                    "bangumi_ids": [1234],
                    "episode": 1,
                    "torrent_name": "[Group] Example Anime [01]",
                },
                "candidates": [],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_review_item") as upsert_review,
            ):
                with self.assertRaisesRegex(ValueError, "found 2 and manual selection is required"):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.auto_rebuild_target_candidates",
                        "review_ambiguous",
                        {"review_id": "review_ambiguous"},
                    )

            upsert_review.assert_not_called()

    def test_review_target_command_rejects_candidate_not_stored_in_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            stored = anime / "Non Non Biyori" / "Season 3" / "Non Non Biyori - S03E01.mkv"
            forged = anime / "Other Series" / "Season 3" / "Other - S03E01.mkv"
            stored.parent.mkdir(parents=True)
            forged.parent.mkdir(parents=True)
            stored.write_bytes(b"")
            forged.write_bytes(b"")
            config = SimpleNamespace(input_path=anime, video_extensions=[".mkv"])
            review = {
                "kind": "target_ambiguity",
                "status": "open",
                "diagnosis": {"bangumi_ids": [2402], "torrent_hash": "a" * 40},
                "candidates": [{
                    "path": str(stored),
                    "season": 3,
                    "reasons": ["title_contains"],
                }],
            }

            with (
                patch("control_state.get_review_item", return_value=review),
                patch("control_state.upsert_series_source_mapping") as upsert_mapping,
                patch("mikan_worker.resume_target_ambiguity_source") as resume_source,
            ):
                with self.assertRaisesRegex(ValueError, "not one of the stored review candidates"):
                    main_module._execute_control_command(
                        config,
                        Mock(),
                        "review.resolve_target",
                        "review_test",
                        {
                            "review_id": "review_test",
                            "candidate_path": str(forged),
                            "source_id": "2402",
                        },
                    )

            upsert_mapping.assert_not_called()
            resume_source.assert_not_called()

    def test_redownload_command_never_accepts_media_deletion(self) -> None:
        with self.assertRaisesRegex(ValueError, "forbidden"):
            main_module._execute_control_command(
                SimpleNamespace(),
                Mock(),
                "mikan.request_redownload_all",
                "",
                {"delete_files": True},
            )

    def test_maintenance_command_runs_inside_worker_control_plane(self) -> None:
        with patch.object(main_module, "_run_control_subprocess", return_value="ok") as run:
            result = main_module._execute_control_command(
                SimpleNamespace(), Mock(), "system.backup_state", "", {}
            )

        self.assertEqual(result["output"], "ok")
        self.assertIn("/app/backup_state.py", run.call_args.args[0])

    def test_retry_all_failures_is_serialized_by_worker_control_plane(self) -> None:
        config = SimpleNamespace()
        state = Mock()
        state.retry_all_failed_ai_queue_candidates.return_value = 3

        def commit(_state, operation, *, attempts):
            self.assertIs(_state, state)
            self.assertEqual(attempts, 5)
            operation()

        with (
            patch("scan_state.ScanStateStore.from_config", return_value=state),
            patch("mikan_worker.requeue_failed_mikan_extract_jobs", return_value=2) as requeue_extracts,
            patch.object(main_module, "_commit_ai_queue_state_write", side_effect=commit),
        ):
            result = main_module._execute_control_command(
                config, Mock(), "system.retry_all_failures", "", {}
            )

        self.assertEqual(result["ai_requeued"], 3)
        self.assertEqual(result["extraction_requeued"], 2)
        state.close.assert_called_once_with()
        requeue_extracts.assert_called_once_with(config, include_terminal=False)

    def test_bulk_cli_requeue_failed_extracts_excludes_terminal_jobs(self) -> None:
        config = SimpleNamespace()
        logger = Mock()
        with patch(
            "mikan_worker.requeue_failed_mikan_extract_jobs",
            return_value=4,
        ) as requeue:
            main_module._mikan_requeue_failed_extracts(config, logger)

        requeue.assert_called_once_with(config, include_terminal=False)
        logger.warning.assert_called_once()

    def test_control_requeue_failed_extracts_excludes_terminal_jobs(self) -> None:
        config = SimpleNamespace()
        with patch(
            "mikan_worker.requeue_failed_mikan_extract_jobs",
            return_value=4,
        ) as requeue:
            result = main_module._execute_control_command(
                config,
                Mock(),
                "mikan.requeue_failed_extracts",
                "",
                {},
            )

        self.assertEqual(result["requeued"], 4)
        requeue.assert_called_once_with(config, include_terminal=False)

    def test_cancel_extract_command_is_routed_to_cooperative_worker_request(self) -> None:
        config = SimpleNamespace()
        with patch(
            "mikan_worker.request_mikan_extract_cancel",
            return_value={"cancel_requested": True, "job_key": "hash:test"},
        ) as cancel_extract:
            result = main_module._execute_control_command(
                config,
                Mock(),
                "mikan.cancel_extract",
                "",
                {"job_key": "hash:test"},
            )

        self.assertTrue(result["cancel_requested"])
        cancel_extract.assert_called_once_with(config, job_key="hash:test")

    def test_requeue_extract_command_targets_only_requested_job(self) -> None:
        config = SimpleNamespace()
        with patch("mikan_worker.requeue_mikan_extract_job", return_value=True) as requeue_extract:
            result = main_module._execute_control_command(
                config,
                Mock(),
                "mikan.requeue_extract",
                "",
                {"job_key": "hash:test"},
            )

        self.assertTrue(result["requeued"])
        requeue_extract.assert_called_once_with(config, job_key="hash:test")


if __name__ == "__main__":
    unittest.main()
