from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock

import main as main_module
import m2_guardrail_runtime as runtime
import m2_production_observation as observation
from m2_strict_observation import strict_evidence_template
from safe_files import atomic_write_text
from source_decision import SOURCE_DECISION_CONTRACT


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
            "m2_guardrail_runtime_state_path": work_path / "m2-guardrail-runtime.json",
            "m2_guardrail_source_revision_file": work_path / "source-revision",
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
            "max_concurrent_videos": 1,
            "source_integrity_sha256_enabled": True,
            "source_decision_schema_version": 1,
            "source_decision_version": "m2-source-decision-v1",
        }
        values.update(overrides)
        config = SimpleNamespace(**values)
        self._arm(config)
        return config

    @staticmethod
    def _arm(config: SimpleNamespace) -> None:
        marker = Path(config.m2_guardrail_source_revision_file)
        marker.write_text("a" * 64 + "\n", encoding="utf-8")
        baseline = {
            "worker_commit_sha": "1" * 40,
            "webui_commit_sha": "2" * 40,
            "worker_source_revision": "a" * 64,
            "webui_source_revision": "b" * 64,
            "worker_image_id": "sha256:" + "3" * 64,
            "webui_image_id": "sha256:" + "4" * 64,
            "configuration_fingerprint": runtime.configuration_fingerprint(config),
            "decision_schema_version": 1,
            "decision_version": "m2-source-decision-v1",
            "decision_contract": SOURCE_DECISION_CONTRACT,
        }
        now = time.time() - 1
        state = {
            "contract": runtime.RUNTIME_CONTRACT,
            "schema_version": runtime.RUNTIME_SCHEMA_VERSION,
            "status": "ARMED",
            "armed_at": "2026-09-04T00:00:00Z",
            "gate_start_at": "2026-09-04T00:00:00Z",
            "gate_start_epoch": now,
            "gate_baseline_version": runtime._baseline_version(baseline),
            "baseline": baseline,
            "gate": {
                "target": 20,
                "progress": 0,
                "claimed_after_gate_start": 0,
                "completed_strict_verified": 0,
            },
            "pre_gate_running": {
                "attempt_keys": [],
                "queue_job_keys": [],
                "attempt_count": 0,
                "queue_job_count": 0,
                "policy": "record_result_but_exclude_from_formal_gate",
            },
            "breaker_tests": {
                "contract": runtime.FAULT_RESULT_CONTRACT,
                "passed_count": 7,
                "required_count": 7,
                "production_resources_affected": False,
                "worker_source_revision": "a" * 64,
                "summary_sha256": "sha256:" + "5" * 64,
                "full_log_sha256": "sha256:" + "6" * 64,
                "container_started_at_epoch": now - 3,
                "started_at_epoch": now - 2,
                "finished_at_epoch": now - 1,
            },
            "runtime_checks": {
                "worker_container_running": True,
                "webui_container_running": True,
                "worker_config_mount_readonly": True,
                "worker_command_uses_config": True,
                "worker_config_unchanged_since_start": True,
            },
            "production_resources_affected": False,
        }
        observation.initialize_observation_gate(config, state, now=now)
        atomic_write_text(
            runtime.runtime_state_path(config),
            json.dumps(state, sort_keys=True, indent=2) + "\n",
        )

    @staticmethod
    def _verified() -> dict[str, object]:
        return {
            "verified_completed": True,
            "failed": False,
            "terminal_status": "COMPLETED",
            "processing_strategy": "USE_EXISTING_ZH_TW",
            "stage": "delivery_verification",
            "error_code": "",
            "reason_code": "strict_delivery_verified",
        }

    def _record_verified(
        self,
        config: SimpleNamespace,
        identity: str,
        *,
        outcome: dict[str, object] | None = None,
    ) -> dict[str, object]:
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        return observation.record_job_result(
            config,
            job_identity=identity,
            outcome=outcome or self._verified(),
            strict_evidence=strict_evidence_template(passed=True),
        )

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
            result = self._record_verified(
                config, f"attempt-{index:03d}"
            )
            self.assertEqual(result["emitted"], [])

        self.assertEqual(result["verified_since_gate"], 19)
        self.assertEqual(result["next_gate_after_verified_completed"], 20)
        self.assertEqual(
            list(Path(config.m2_server_canary_observation_output_dir).glob("gate-*.json")),
            [],
        )

        importlib.reload(observation)
        duplicate = self._record_verified(
            config, "attempt-018"
        )
        self.assertEqual(duplicate["verified_since_gate"], 19)
        self.assertEqual(duplicate["emitted"], [])

        twentieth = self._record_verified(
            config, "attempt-019"
        )
        self.assertEqual(twentieth["emitted"], ["gate-000001.json"])
        gate_path = (
            Path(config.m2_server_canary_observation_output_dir)
            / "gate-000001.json"
        )
        gate = self._read(gate_path)
        self.assertEqual(gate["status"], "M2_GUARDRAILS_ARMED")
        self.assertEqual(gate["gate_size"], 20)
        self.assertEqual(
            gate["window"]["counters"]["completed_strict_verified"], 20
        )

        importlib.reload(observation)
        replay = self._record_verified(
            config, "attempt-019"
        )
        self.assertEqual(replay["emitted"], [])
        self.assertEqual(replay["verified_since_gate"], 0)
        state = self._read(Path(config.m2_server_canary_observation_state_path))
        self.assertEqual(state["totals"]["completed_strict_verified"], 20)
        self.assertEqual(state["total_attempts_observed"], 20)
        self.assertEqual(state["gate_index"], 1)
        self.assertEqual(replay["next_gate_after_verified_completed"], 40)

    def test_pre_gate_terminal_result_is_recorded_but_excluded(self) -> None:
        config = self._config("pre-gate")
        identity = "attempt-running-before-arm"
        runtime_path = runtime.runtime_state_path(config)
        runtime_state = self._read(runtime_path)
        runtime_state["pre_gate_running"]["attempt_keys"] = [
            observation._job_key(identity)
        ]
        atomic_write_text(
            runtime_path,
            json.dumps(runtime_state, sort_keys=True, indent=2) + "\n",
        )

        result = observation.record_job_result(
            config,
            job_identity=identity,
            outcome=self._verified(),
            strict_evidence=strict_evidence_template(passed=True),
        )

        self.assertFalse(result["gate_eligible"])
        self.assertEqual(result["verified_since_gate"], 0)
        state = self._read(Path(config.m2_server_canary_observation_state_path))
        self.assertEqual(state["excluded_terminal_results"], 1)
        self.assertEqual(
            state["excluded_reason_counts"]["running_before_gate_start"], 1
        )

    def test_missing_runtime_manifest_blocks_new_claim_without_tripping(self) -> None:
        config = self._config("runtime-missing")
        runtime.runtime_state_path(config).unlink()

        self.assertFalse(observation.admit_new_job(config))
        self.assertFalse(observation.circuit_breaker_active(config))

    def test_disabled_observer_cannot_bypass_an_existing_m2_guardrail(self) -> None:
        config = self._config("observer-disabled")
        config.m2_server_canary_observer_enabled = False

        self.assertFalse(observation.admit_new_job(config))

    def test_admission_validates_observer_contract_baseline_and_counter_shape(self) -> None:
        mutations = {
            "contract": lambda state: state.__setitem__("contract", "wrong"),
            "baseline": lambda state: state.__setitem__(
                "gate_baseline_version", "wrong"
            ),
            "counters": lambda state: state["totals"].pop("gate_progress"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
                config = self._config(f"invalid-observer-{name}")
                state_path = Path(config.m2_server_canary_observation_state_path)
                state = self._read(state_path)
                mutate(state)
                atomic_write_text(
                    state_path,
                    json.dumps(state, sort_keys=True, indent=2) + "\n",
                )

                self.assertFalse(observation.admit_new_job(config))
                breaker = self._read(
                    Path(config.m2_server_canary_circuit_breaker_state_path)
                )
                self.assertIn(
                    "observation_state_degraded",
                    [item["reason_code"] for item in breaker["reasons"]],
                )

    def test_terminal_result_trips_when_observer_state_or_baseline_is_missing(self) -> None:
        for name, mutate in (
            ("missing", lambda path: path.unlink()),
            (
                "baseline",
                lambda path: atomic_write_text(
                    path,
                    json.dumps(
                        {
                            **self._read(path),
                            "gate_baseline_version": "wrong",
                        },
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n",
                ),
            ),
        ):
            with self.subTest(name=name):
                observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
                config = self._config(f"terminal-state-{name}")
                mutate(Path(config.m2_server_canary_observation_state_path))

                result = observation.record_job_result(
                    config,
                    job_identity=f"attempt-{name}",
                    outcome=self._verified(),
                    strict_evidence=strict_evidence_template(passed=True),
                )

                self.assertFalse(result["recorded"])
                self.assertTrue(result["circuit_breaker_tripped"])
                self.assertIn(
                    result["reason_code"],
                    {"observation_state_missing", "observation_baseline_mismatch"},
                )

    def test_durable_claim_binding_is_public_and_fails_closed_on_bad_state(self) -> None:
        config = self._config("durable-claim")
        identity = "durably-bound-attempt"
        self.assertFalse(
            observation.has_durable_gate_claim_binding(
                config,
                job_identity=identity,
            )
        )
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        self.assertTrue(
            observation.has_durable_gate_claim_binding(
                config,
                job_identity=identity,
            )
        )
        Path(config.m2_server_canary_observation_state_path).unlink()
        with self.assertRaises(observation.ObservationStateError):
            observation.has_durable_gate_claim_binding(
                config,
                job_identity=identity,
            )

    def test_completed_without_all_strict_evidence_trips_incorrect_completion(self) -> None:
        config = self._config("strict-fail-closed")
        identity = "strict-missing-attempt"
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )

        result = observation.record_job_result(
            config,
            job_identity=identity,
            outcome=self._verified(),
            strict_evidence={},
        )

        self.assertTrue(result["circuit_breaker_tripped"])
        self.assertFalse(result["strictly_qualified"])
        breaker = self._read(Path(config.m2_server_canary_circuit_breaker_state_path))
        self.assertIn(
            "incorrect_completion",
            [item["reason_code"] for item in breaker["reasons"]],
        )

    def test_completed_safety_incidents_keep_specific_breaker_reason(self) -> None:
        cases = {
            "source_mutation": {"source_mutation_incident": True},
            "duplicate_publish": {"duplicate_publish": True},
            "output_parse_failure": {"output_parse_failure": True},
        }
        for reason_code, flags in cases.items():
            with self.subTest(reason_code=reason_code):
                observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
                config = self._config(f"completed-priority-{reason_code}")
                identity = f"completed-priority-{reason_code}"
                observation.record_job_claim(
                    config,
                    job_identity=identity,
                    claimed_at=time.time(),
                )
                result = observation.record_job_result(
                    config,
                    job_identity=identity,
                    outcome={**self._verified(), **flags},
                    strict_evidence=strict_evidence_template(passed=True),
                )

                self.assertTrue(result["circuit_breaker_tripped"])
                breaker = self._read(
                    Path(config.m2_server_canary_circuit_breaker_state_path)
                )
                self.assertEqual(breaker["reasons"][0]["reason_code"], reason_code)

    def test_explicit_oom_flag_drives_streak_without_message_matching(self) -> None:
        config = self._config("explicit-oom")
        outcome = {
            **self._failed(stage="transcription", error_code="gpu_resource"),
            "oom_event": True,
        }

        for index in range(2):
            result = observation.record_job_result(
                config,
                job_identity=f"explicit-oom-{index}",
                outcome=outcome,
            )
            self.assertFalse(result["circuit_breaker_tripped"])
        result = observation.record_job_result(
            config,
            job_identity="explicit-oom-2",
            outcome=outcome,
        )

        self.assertTrue(result["circuit_breaker_tripped"])
        breaker = self._read(Path(config.m2_server_canary_circuit_breaker_state_path))
        self.assertEqual(breaker["reasons"][0]["reason_code"], "repeated_oom")

    def test_pending_gate_recovers_after_summary_publish_before_state_commit(self) -> None:
        config = self._config("pending-gate-recovery")
        for index in range(19):
            self._record_verified(config, f"pending-{index:03d}")

        identity = "pending-019"
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        real_write = observation._write_observation_state

        def crash_after_publish(write_config, state):
            gate_path = (
                Path(config.m2_server_canary_observation_output_dir)
                / "gate-000001.json"
            )
            if (
                gate_path.exists()
                and state.get("gate_index") == 1
                and state.get("pending_gate") is None
            ):
                raise OSError("simulated crash after gate publication")
            return real_write(write_config, state)

        with mock.patch.object(
            observation,
            "_write_observation_state",
            side_effect=crash_after_publish,
        ):
            with self.assertRaises(OSError):
                observation.record_job_result(
                    config,
                    job_identity=identity,
                    outcome=self._verified(),
                    strict_evidence=strict_evidence_template(passed=True),
                )

        journal = self._read(Path(config.m2_server_canary_observation_state_path))
        self.assertIsNotNone(journal["pending_gate"])
        self.assertTrue(
            (
                Path(config.m2_server_canary_observation_output_dir)
                / "gate-000001.json"
            ).is_file()
        )

        importlib.reload(observation)
        self.assertTrue(observation.admit_new_job(config))
        recovered = self._read(Path(config.m2_server_canary_observation_state_path))
        self.assertEqual(recovered["gate_index"], 1)
        self.assertIsNone(recovered["pending_gate"])
        self.assertEqual(recovered["window"]["counters"]["gate_progress"], 0)
        replay = observation.record_job_result(
            config,
            job_identity=identity,
            outcome=self._verified(),
            strict_evidence=strict_evidence_template(passed=True),
        )
        self.assertTrue(replay["duplicate_observation_ignored"])

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
        self._record_verified(
            config,
            raw_identity,
            outcome=sensitive_outcome,
        )
        for index in range(1, 20):
            self._record_verified(
                config, f"opaque-attempt-{index}"
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
        strict_result = {
            "outcome": {
                "verified_completed": True,
                "failed": False,
                "reason_code": "succeeded",
            },
            "evidence": strict_evidence_template(passed=True),
            "processing_strategy": "ASR_JA_AUDIO",
        }
        with (
            mock.patch.object(main_module, "_mark_queue_result") as commit,
            mock.patch.object(observation, "record_job_result") as record,
            mock.patch(
                "m2_guardrail_runtime.load_runtime_state",
                return_value={"status": "ARMED"},
            ),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                return_value=strict_result,
            ) as build_evidence,
        ):
            main_module._mark_queue_result_and_observe(
                state,
                private_video,
                True,
                config,
                delivery_attempt_id="opaque-attempt-id",
            )

        commit.assert_called_once()
        build_evidence.assert_called_once_with(
            state,
            private_video,
            config,
            "opaque-attempt-id",
            {"status": "ARMED"},
        )
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["job_identity"], "opaque-attempt-id")
        self.assertEqual(record.call_args.kwargs["outcome"], strict_result["outcome"])
        self.assertEqual(
            record.call_args.kwargs["strict_evidence"], strict_result["evidence"]
        )
        self.assertNotIn(str(private_video), repr(record.call_args))


if __name__ == "__main__":
    unittest.main()
