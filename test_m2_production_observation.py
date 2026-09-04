from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import main as main_module
import m2_production_observation as observation


class M2ProductionObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False

    def tearDown(self) -> None:
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False

    def _config(self, name: str = "case", **overrides: object) -> SimpleNamespace:
        root = self.root / name
        input_path = root / "input"
        work_path = root / "work"
        log_path = root / "logs"
        completed_path = root / "completed"
        output_path = log_path / "m2-observations"
        for path in (input_path, work_path, log_path, completed_path, output_path):
            path.mkdir(parents=True, exist_ok=True)
        values: dict[str, object] = {
            "m2_server_canary_observer_enabled": True,
            "m2_server_canary_observation_gate_size": 20,
            "m2_server_canary_observation_state_path": (
                work_path / "m2-observation-state.json"
            ),
            "m2_server_canary_observation_output_dir": output_path,
            "m2_server_canary_circuit_breaker_enabled": True,
            "m2_server_canary_circuit_breaker_state_path": (
                work_path / "m2-circuit-breaker.json"
            ),
            "m2_server_canary_repeated_oom_threshold": 3,
            "m2_server_canary_identical_failure_threshold": 3,
            "disk_min_free_gb": 0.0,
            "input_path": input_path,
            "work_path": work_path,
            "log_path": log_path,
            "completed_delivery_enabled": False,
            "completed_delivery_path": completed_path,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def _verified() -> dict[str, object]:
        return {
            "verified_completed": True,
            "failed": False,
            "stage": "delivery_verification",
            "error_code": "",
            "reason_code": "strict_delivery_verified",
        }

    @staticmethod
    def _failed(
        *,
        stage: str = "worker",
        error_code: str = "model_timeout",
        reason_code: str = "",
        detail: str = "",
    ) -> dict[str, object]:
        return {
            "verified_completed": False,
            "failed": True,
            "stage": stage,
            "error_code": error_code,
            "reason_code": reason_code,
            "detail": detail,
        }

    @staticmethod
    def _read(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _checkpoint_sentinel(self, config: SimpleNamespace) -> Path:
        sentinel = Path(config.work_path) / "checkpoint-preserve.sentinel"
        sentinel.write_text("checkpoint-must-survive", encoding="utf-8")
        return sentinel

    def _assert_tripped(
        self,
        config: SimpleNamespace,
        reason_code: str,
        sentinel: Path,
    ) -> None:
        self.assertTrue(observation.circuit_breaker_active(config))
        breaker = self._read(Path(config.m2_server_canary_circuit_breaker_state_path))
        self.assertTrue(breaker["tripped"])
        self.assertEqual(breaker["action"], "stop_claiming_new_jobs")
        self.assertEqual(
            breaker["running_job_policy"], "finish_without_interruption"
        )
        self.assertEqual(breaker["checkpoint_policy"], "preserve")
        self.assertIn(
            reason_code,
            [item["reason_code"] for item in breaker["reasons"]],
        )
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "checkpoint-must-survive"
        )

    def test_gate_persists_across_restart_and_duplicate_jobs_do_not_advance(self) -> None:
        config = self._config("gate")
        for index in range(19):
            result = observation.record_job_result(
                config,
                job_identity=f"attempt-{index:03d}",
                outcome=self._verified(),
            )
            self.assertEqual(result["emitted"], [])

        self.assertEqual(result["verified_since_gate"], 19)
        self.assertEqual(result["next_gate_after_verified_completed"], 20)
        self.assertEqual(
            list(Path(config.m2_server_canary_observation_output_dir).glob("gate-*.json")),
            [],
        )

        importlib.reload(observation)
        duplicate = observation.record_job_result(
            config,
            job_identity="attempt-018",
            outcome=self._verified(),
        )
        self.assertEqual(duplicate["verified_since_gate"], 19)
        self.assertEqual(duplicate["emitted"], [])

        twentieth = observation.record_job_result(
            config,
            job_identity="attempt-019",
            outcome=self._verified(),
        )
        self.assertEqual(twentieth["emitted"], ["gate-000001.json"])
        gate_path = (
            Path(config.m2_server_canary_observation_output_dir)
            / "gate-000001.json"
        )
        gate = self._read(gate_path)
        self.assertEqual(gate["status"], "M2_SERVER_CANARY_ACTIVE")
        self.assertEqual(gate["gate_size"], 20)
        self.assertEqual(gate["window"]["verified_completed"], 20)

        importlib.reload(observation)
        replay = observation.record_job_result(
            config,
            job_identity="attempt-019",
            outcome=self._verified(),
        )
        self.assertEqual(replay["emitted"], [])
        self.assertEqual(replay["verified_since_gate"], 0)
        state = self._read(Path(config.m2_server_canary_observation_state_path))
        self.assertEqual(state["total_verified_completed"], 20)
        self.assertEqual(state["total_attempts_observed"], 20)
        self.assertEqual(state["gate_index"], 1)
        self.assertEqual(replay["next_gate_after_verified_completed"], 40)

    def test_persisted_state_and_gate_exclude_sensitive_job_content(self) -> None:
        config = self._config("redaction")
        raw_identity = "PRIVATE_MEDIA_PATH_MARKER"
        sensitive_outcome = {
            **self._verified(),
            "title": "PRIVATE_MEDIA_TITLE_MARKER",
            "source_path": raw_identity,
            "detail": (
                "PRIVATE_TRACE_MARKER PRIVATE_HOST_MARKER PRIVATE_PORT_MARKER"
            ),
            "full_log": "PRIVATE_FULL_LOG_MARKER",
        }
        observation.record_job_result(
            config,
            job_identity=raw_identity,
            outcome=sensitive_outcome,
        )
        for index in range(1, 20):
            observation.record_job_result(
                config,
                job_identity=f"opaque-attempt-{index}",
                outcome=self._verified(),
            )

        persisted = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                Path(config.m2_server_canary_observation_state_path),
                Path(config.m2_server_canary_observation_output_dir)
                / "gate-000001.json",
            )
        )
        for secret in (
            "PRIVATE_MEDIA_PATH_MARKER",
            "PRIVATE_MEDIA_TITLE_MARKER",
            "PRIVATE_TRACE_MARKER",
            "PRIVATE_HOST_MARKER",
            "PRIVATE_PORT_MARKER",
            "PRIVATE_FULL_LOG_MARKER",
        ):
            self.assertNotIn(secret, persisted)
    def test_four_immediate_breaker_categories_stop_only_new_claims(self) -> None:
        cases = {
            "source_mutation": self._failed(
                stage="source_analysis",
                error_code="media_revision_changed",
            ),
            "duplicate_publish": self._failed(
                stage="publish",
                error_code="duplicate_publish",
            ),
            "output_parse_failure": self._failed(
                stage="delivery_verification",
                error_code="output_parse_failure",
            ),
            "incorrect_completion": self._failed(
                stage="delivery_verification",
                error_code="delivery_evidence_missing",
            ),
        }
        for reason_code, outcome in cases.items():
            with self.subTest(reason_code=reason_code):
                observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
                config = self._config(f"immediate-{reason_code}")
                sentinel = self._checkpoint_sentinel(config)
                result = observation.record_job_result(
                    config,
                    job_identity=f"job-{reason_code}",
                    outcome=outcome,
                )
                self.assertTrue(result["circuit_breaker_tripped"])
                self.assertFalse(observation.admit_new_job(config))
                self._assert_tripped(config, reason_code, sentinel)

    def test_repeated_oom_trips_only_at_configured_threshold(self) -> None:
        config = self._config("oom")
        sentinel = self._checkpoint_sentinel(config)
        outcome = self._failed(stage="transcription", error_code="transient_oom")

        for index in range(2):
            result = observation.record_job_result(
                config,
                job_identity=f"oom-{index}",
                outcome=outcome,
            )
            self.assertFalse(result["circuit_breaker_tripped"])
            self.assertTrue(observation.admit_new_job(config))

        third = observation.record_job_result(
            config,
            job_identity="oom-2",
            outcome=outcome,
        )
        self.assertTrue(third["circuit_breaker_tripped"])
        self._assert_tripped(config, "repeated_oom", sentinel)

    def test_identical_stage_failure_streak_resets_and_uses_threshold(self) -> None:
        config = self._config("identical")
        sentinel = self._checkpoint_sentinel(config)
        repeated = self._failed(stage="translation", error_code="model_timeout")

        for index in range(2):
            result = observation.record_job_result(
                config,
                job_identity=f"repeat-before-reset-{index}",
                outcome=repeated,
            )
            self.assertFalse(result["circuit_breaker_tripped"])
        different = observation.record_job_result(
            config,
            job_identity="different-failure",
            outcome=self._failed(stage="mux", error_code="temporary_io_error"),
        )
        self.assertFalse(different["circuit_breaker_tripped"])

        for index in range(2):
            result = observation.record_job_result(
                config,
                job_identity=f"repeat-after-reset-{index}",
                outcome=repeated,
            )
            self.assertFalse(result["circuit_breaker_tripped"])
        threshold = observation.record_job_result(
            config,
            job_identity="repeat-after-reset-2",
            outcome=repeated,
        )
        self.assertTrue(threshold["circuit_breaker_tripped"])
        self._assert_tripped(
            config,
            "repeated_identical_stage_failure",
            sentinel,
        )

    def test_insufficient_disk_trips_before_new_job_claim(self) -> None:
        config = self._config("disk", disk_min_free_gb=2.0)
        sentinel = self._checkpoint_sentinel(config)
        disk_usage = SimpleNamespace(total=10_000, used=9_999, free=1)
        with mock.patch.object(observation.shutil, "disk_usage", return_value=disk_usage):
            self.assertFalse(observation.admit_new_job(config))
        self._assert_tripped(config, "insufficient_disk_space", sentinel)

    def test_malformed_breaker_state_fails_closed_without_touching_checkpoint(self) -> None:
        config = self._config("malformed")
        sentinel = self._checkpoint_sentinel(config)
        breaker_path = Path(config.m2_server_canary_circuit_breaker_state_path)
        breaker_path.write_text("{not valid json", encoding="utf-8")
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False

        self.assertTrue(observation.circuit_breaker_active(config))
        self.assertFalse(observation.admit_new_job(config))
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "checkpoint-must-survive"
        )
        self.assertFalse((Path(config.work_path) / "ai_control.json").exists())

    def test_main_queue_pause_observes_active_m2_breaker(self) -> None:
        config = self._config("main-queue-pause")
        sentinel = self._checkpoint_sentinel(config)
        observation.trip_circuit_breaker(
            config,
            "source_mutation",
            evidence={"stage": "source_analysis"},
        )

        self.assertTrue(main_module._ai_queue_paused(config))
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "checkpoint-must-survive"
        )

    def test_main_admission_refuses_breaker_and_low_disk(self) -> None:
        breaker_config = self._config("main-admission-breaker")
        observation.trip_circuit_breaker(
            breaker_config,
            "duplicate_publish",
            evidence={"stage": "publish"},
        )
        self.assertFalse(
            main_module._m2_server_canary_admit_new_job(breaker_config)
        )

        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
        disk_config = self._config("main-admission-disk", disk_min_free_gb=2.0)
        disk_usage = SimpleNamespace(total=10_000, used=9_999, free=1)
        with mock.patch.object(observation.shutil, "disk_usage", return_value=disk_usage):
            self.assertFalse(
                main_module._m2_server_canary_admit_new_job(disk_config)
            )
        breaker = self._read(
            Path(disk_config.m2_server_canary_circuit_breaker_state_path)
        )
        self.assertIn(
            "insufficient_disk_space",
            [item["reason_code"] for item in breaker["reasons"]],
        )

    def test_terminal_queue_commit_feeds_observer_without_raw_video_identity(self) -> None:
        config = self._config("main-terminal-observation")
        state = mock.Mock()
        state.get_ai_delivery_attempt.return_value = {
            "status": "succeeded",
            "stage": "delivery_verification",
            "error_code": "",
            "detail": "",
        }
        private_video = Path("PRIVATE_MEDIA_PATH_MARKER")
        with (
            mock.patch.object(main_module, "_mark_queue_result") as commit,
            mock.patch.object(observation, "record_job_result") as record,
        ):
            main_module._mark_queue_result_and_observe(
                state,
                private_video,
                True,
                config,
                delivery_attempt_id="opaque-attempt-id",
            )

        commit.assert_called_once()
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["job_identity"], "opaque-attempt-id")
        self.assertNotIn(str(private_video), repr(record.call_args))


if __name__ == "__main__":
    unittest.main()
