from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import main as main_module
from scan_state import ScanStateStore


class ResourceMainIntegrationTest(unittest.TestCase):
    def test_every_video_is_admitted_before_its_queue_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            videos = [root / "one.mkv", root / "two.mkv"]
            for video in videos:
                video.write_bytes(b"media")
            scanner = Mock()
            scanner.scan.return_value = videos
            scanner.last_database_error = ""
            scanner.last_database_error_code = ""
            worker = Mock()
            worker.config = SimpleNamespace(
                work_path=root,
                max_concurrent_videos=1,
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
                resource_admission_enabled=True,
                ai_process_isolation_enabled=True,
            )
            events: list[tuple[str, Path]] = []

            def decide(_config, video, _logger):
                path = Path(video)
                events.append(("admit", path))
                if path == videos[0]:
                    return {"admitted": True}
                return {
                    "admitted": False,
                    "retry_at": time.time() + 30,
                    "reason_codes": ["gpu_busy"],
                }

            def claim(
                _state,
                video,
                _config,
                *,
                canary_binding=None,
                logger=None,
            ):
                events.append(("claim", Path(video)))
                return "attempt"

            def process(_worker, video, _logger, **options):
                self.assertTrue(options["resource_launch_plan"]["admitted"])
                self.assertEqual(options["delivery_attempt_id"], "attempt")
                events.append(("process", Path(video)))
                return True

            with (
                patch.object(main_module, "_resource_launch_plan_for_video", side_effect=decide),
                patch.object(main_module, "_mark_queue_running", side_effect=claim),
                patch.object(main_module, "_mark_queue_result"),
                patch.object(main_module, "_process_video_with_policy", side_effect=process),
            ):
                processed = main_module._scan_and_process(scanner, worker, Mock())

            self.assertEqual(processed, 1)
            self.assertEqual(
                events,
                [
                    ("admit", videos[0]),
                    ("claim", videos[0]),
                    ("process", videos[0]),
                    ("admit", videos[1]),
                ],
            )
            scheduler = json.loads((root / "ai_scheduler_state.json").read_text(encoding="utf-8"))
            self.assertEqual(scheduler["state"], "resource_deferred")
            self.assertEqual(scheduler["processed_last_cycle"], 1)
            self.assertGreater(scheduler["next_retry_at"], time.time())

    def test_resource_policy_refuses_missing_plan_or_in_process_fallback(self) -> None:
        worker = Mock()
        worker.config = SimpleNamespace(
            resource_admission_enabled=True,
            ai_process_isolation_enabled=True,
        )
        logger = Mock()

        self.assertFalse(
            main_module._process_video_with_policy(
                worker,
                Path("episode.mkv"),
                logger,
                resource_launch_plan=None,
            )
        )
        worker.process.assert_not_called()

        worker.config.ai_process_isolation_enabled = False
        self.assertFalse(
            main_module._process_video_with_policy(
                worker,
                Path("episode.mkv"),
                logger,
                resource_launch_plan={"admitted": True},
            )
        )
        worker.process.assert_not_called()

    def test_sigkill_persists_lower_memory_retry_authority_and_queue_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text("config", encoding="utf-8")
            video = root / "episode.mkv"
            video.write_bytes(b"media")
            config = SimpleNamespace(
                work_path=root,
                config_path=config_path,
                ai_subprocess_timeout_seconds=10,
                resource_admission_enabled=True,
                resource_admission_state_path="resource.json",
                scanner_cache_enabled=True,
                scanner_queue_enabled=True,
                scanner_state_path="scanner.sqlite3",
                auto_ai_failure_cooldown_seconds=30,
                auto_ai_max_attempts=3,
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.mark_ai_queue_running(video)
                state.commit()
            finally:
                state.close()

            with (
                patch.object(
                    main_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=137),
                ),
                patch("resource_runtime.serialize_launch_plan", return_value="authorized-plan"),
            ):
                ok = main_module._process_video_subprocess(
                    config,
                    video,
                    Mock(),
                    resource_launch_plan={"admitted": True},
                )

            self.assertFalse(ok)
            resource = json.loads((root / "resource.json").read_text(encoding="utf-8"))
            self.assertEqual(resource["last_oom"]["reason_code"], "transient_oom")
            self.assertEqual(
                resource["last_oom"]["retry_strategy"],
                "lower_memory_same_pipeline",
            )
            state = ScanStateStore.from_config(config)
            try:
                stage, detail = state.ai_job_failure(video)
                main_module._mark_queue_result(state, video, False, config)
                queue_row = state._conn.execute(
                    "SELECT status, last_error_code, retry_strategy "
                    "FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()
            finally:
                state.close()
            self.assertEqual(stage, "resource_runtime")
            self.assertIn("SIGKILL/OOM", detail)
            self.assertEqual(
                main_module._ai_failure_policy(stage, detail),
                ("transient_resource_killed", "lower_memory_same_pipeline"),
            )
            self.assertEqual(
                queue_row,
                (
                    "failed_retry",
                    "transient_resource_killed",
                    "lower_memory_same_pipeline",
                ),
            )


if __name__ == "__main__":
    unittest.main()
