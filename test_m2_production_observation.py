from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock

import main as main_module
import m2_guardrail_runtime as runtime
import m2_production_observation as observation
from m2_observation_store import (
    ELIGIBILITY_POLICY_VERSION,
    observation_database_path,
)
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
        runtime_app_path = root / "runtime-app"
        for path in (
            input_path,
            work_path,
            log_path,
            completed_path,
            output_path,
            runtime_app_path,
        ):
            path.mkdir(parents=True, exist_ok=True)
        (runtime_app_path / "requirements.txt").write_text(
            "example==1\n",
            encoding="utf-8",
        )
        (runtime_app_path / "worker.py").write_text(
            "RUNTIME_VALUE = 1\n",
            encoding="utf-8",
        )
        values: dict[str, object] = {
            "m2_server_canary_observer_enabled": True,
            "m2_server_canary_observation_gate_size": 20,
            "scanner_state_path": work_path / "scanner_state.sqlite3",
            "m2_server_canary_observation_state_path": (
                work_path / "m2-observation-state.json"
            ),
            "m2_server_canary_observation_output_dir": output_path,
            "m2_guardrail_runtime_state_path": work_path / "m2-guardrail-runtime.json",
            "m2_guardrail_source_revision_file": work_path / "source-revision",
            "m2_guardrail_container_identity_file": work_path / "container-identity",
            "m2_guardrail_runtime_app_root": runtime_app_path,
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
        Path(config.m2_guardrail_container_identity_file).write_text(
            "unit-test-worker\n",
            encoding="utf-8",
        )
        atomic_write_text(
            Path(config.work_path) / "ai_control.json",
            json.dumps(
                {
                    "paused": True,
                    "updated_at": time.time(),
                    "requested_by": "unit-test",
                },
                sort_keys=True,
            )
            + "\n",
        )
        baseline = {
            "worker_commit_sha": "1" * 40,
            "webui_commit_sha": "2" * 40,
            "worker_source_revision": "a" * 64,
            "worker_runtime_code_revision": runtime.worker_runtime_code_revision(
                config
            ),
            "webui_source_revision": "b" * 64,
            "worker_image_id": "sha256:" + "3" * 64,
            "webui_image_id": "sha256:" + "4" * 64,
            "worker_container_id": "7" * 64,
            "worker_container_identity": "unit-test-worker",
            "worker_runtime_instance_fingerprint": runtime.worker_runtime_instance_fingerprint(
                config
            )["runtime_instance_fingerprint"],
            "configuration_fingerprint": runtime.configuration_fingerprint(config),
            "decision_schema_version": 1,
            "decision_version": "m2-source-decision-v1",
            "decision_contract": SOURCE_DECISION_CONTRACT,
            "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
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
                "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
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
        gate = observation.initialize_observation_gate(config, state, now=now)
        state["gate"]["gate_id"] = gate["gate_id"]
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

    def _runtime_manifest_with_different_gate(
        self,
        config: SimpleNamespace,
    ) -> dict[str, object]:
        state = self._read(runtime.runtime_state_path(config))
        baseline = dict(state["baseline"])
        baseline["worker_commit_sha"] = "9" * 40
        state["baseline"] = baseline
        state["gate_baseline_version"] = runtime._baseline_version(baseline)
        atomic_write_text(
            runtime.runtime_state_path(config),
            json.dumps(state, sort_keys=True, indent=2) + "\n",
        )
        self.assertEqual(runtime.runtime_guardrail_status(config)["status"], "ARMED")
        return state

    @staticmethod
    def _database_rows(
        config: SimpleNamespace,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(observation_database_path(config))
        try:
            return list(connection.execute(statement, parameters).fetchall())
        finally:
            connection.close()

    @staticmethod
    def _remove_database(config: SimpleNamespace) -> None:
        database = observation_database_path(config)
        for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            candidate.unlink(missing_ok=True)

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
            result = self._record_verified(config, f"attempt-{index:03d}")
            self.assertEqual(result["emitted"], [])

        self.assertEqual(result["verified_since_gate"], 19)
        self.assertEqual(result["frozen_cohort_progress"], "19/20")
        self.assertEqual(result["settled_progress"], "19/20")
        self.assertEqual(
            list(Path(config.m2_server_canary_observation_output_dir).glob("*.json")),
            [],
        )

        importlib.reload(observation)
        duplicate = self._record_verified(config, "attempt-018")
        self.assertEqual(duplicate["verified_since_gate"], 19)
        self.assertEqual(duplicate["frozen_cohort_progress"], "19/20")
        self.assertTrue(duplicate["duplicate_observation_ignored"])
        self.assertEqual(duplicate["emitted"], [])

        twentieth = self._record_verified(config, "attempt-019")
        gate_id = twentieth["gate_id"]
        self.assertEqual(twentieth["emitted"], [f"{gate_id}.json"])
        gate_path = (
            Path(config.m2_server_canary_observation_output_dir)
            / f"{gate_id}.json"
        )
        gate = self._read(gate_path)
        self.assertEqual(gate["status"], "SETTLED")
        self.assertEqual(gate["gate_id"], gate_id)
        self.assertEqual(gate["enrolled"], "20/20")
        self.assertEqual(gate["settled"], "20/20")
        self.assertEqual(gate["strict_verified_count"], 20)

        importlib.reload(observation)
        self.assertEqual(observation.publish_pending_summaries(config), [])
        rows = self._database_rows(
            config,
            "SELECT ordinal, job_id FROM m2_observation_gate_jobs "
            "WHERE gate_id=? ORDER BY ordinal",
            (gate_id,),
        )
        self.assertEqual([row[0] for row in rows], list(range(1, 21)))
        self.assertEqual(len({row[1] for row in rows}), 20)
        gate_row = self._database_rows(
            config,
            "SELECT status, enrolled_count, settled_count, summary_emitted_at "
            "FROM m2_observation_gates WHERE gate_id=?",
            (gate_id,),
        )[0]
        self.assertEqual(gate_row[:3], ("SETTLED", 20, 20))
        self.assertIsNotNone(gate_row[3])

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

        claim = observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        self.assertFalse(claim["gate_eligible"])
        self.assertEqual(claim["eligibility_reason"], "running_before_gate_start")

        result = observation.record_job_result(
            config,
            job_identity=identity,
            outcome=self._verified(),
            strict_evidence=strict_evidence_template(passed=True),
        )

        self.assertFalse(result["gate_eligible"])
        self.assertEqual(result["verified_since_gate"], 0)
        self.assertEqual(result["frozen_cohort_progress"], "0/20")
        supplemental = self._database_rows(
            config,
            "SELECT exclusion_reason, last_state FROM m2_observation_supplemental",
        )
        self.assertEqual(supplemental, [("running_before_gate_start", "COMPLETED")])
        self.assertEqual(
            self._database_rows(
                config,
                "SELECT COUNT(*) FROM m2_observation_gate_jobs",
            )[0][0],
            0,
        )

    def test_legacy_gate_invalidation_preserves_history_and_is_idempotent(self) -> None:
        config = self._config("legacy-invalidation")
        runtime_path = runtime.runtime_state_path(config)
        runtime_state = self._read(runtime_path)
        runtime_state["pre_gate_running"]["attempt_count"] = 8
        runtime_state["pre_gate_running"]["attempt_keys"] = [
            f"pre-gate-{number}" for number in range(8)
        ]
        atomic_write_text(
            runtime_path,
            json.dumps(runtime_state, sort_keys=True, indent=2) + "\n",
        )
        legacy = {
            "gate_baseline_version": runtime_state["gate_baseline_version"],
            "gate_start_at": runtime_state["gate_start_at"],
            "gate_start_epoch": runtime_state["gate_start_epoch"],
            "runtime_baseline": runtime_state["baseline"],
            "created_at": runtime_state["gate_start_epoch"],
            "claims": {
                "pre-gate-attempt": {
                    "gate_eligible": False,
                    "terminal_observed": True,
                }
            },
        }
        legacy_path = Path(config.m2_server_canary_observation_state_path)
        legacy_text = json.dumps(legacy, sort_keys=True, indent=2) + "\n"
        atomic_write_text(legacy_path, legacy_text)
        self._remove_database(config)

        expected = {
            "expected_gate_baseline_version": str(
                runtime_state["gate_baseline_version"]
            ),
            "expected_gate_start_at": str(runtime_state["gate_start_at"]),
            "expected_worker_sha": "1" * 40,
            "expected_webui_sha": "2" * 40,
            "expected_pre_gate_attempts": 8,
        }
        first = observation.invalidate_observation_gate(
            config,
            reason=observation.INVALIDATED_AUTOMATION,
            **expected,
        )
        second = observation.invalidate_observation_gate(
            config,
            reason=observation.INVALIDATED_AUTOMATION,
            **expected,
        )

        self.assertEqual(first["status"], observation.INVALIDATED_AUTOMATION)
        self.assertFalse(first["already_invalidated"])
        self.assertTrue(second["already_invalidated"])
        self.assertEqual(second["gate_id"], first["gate_id"])
        self.assertEqual(first["supplemental_pre_gate_attempts"], 8)
        self.assertTrue(first["history_preserved"])
        self.assertFalse(first["production_resources_affected"])
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), legacy_text)
        retired_runtime = self._read(runtime_path)
        self.assertEqual(retired_runtime["status"], "DEGRADED")
        self.assertEqual(
            retired_runtime["gate_final_status"],
            observation.INVALIDATED_AUTOMATION,
        )
        stored = self._database_rows(
            config,
            """
            SELECT status, baseline_version, pre_gate_attempt_count,
                   legacy_observation_json, legacy_runtime_manifest_json,
                   legacy_runtime_manifest_sha256
            FROM m2_observation_gates
            """,
        )
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0][0], observation.INVALIDATED_AUTOMATION)
        self.assertEqual(stored[0][1], runtime_state["gate_baseline_version"])
        self.assertEqual(stored[0][2], 8)
        self.assertEqual(json.loads(str(stored[0][3])), legacy)
        expected_runtime_manifest = json.dumps(
            runtime_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(stored[0][4], expected_runtime_manifest)
        self.assertEqual(
            stored[0][5],
            hashlib.sha256(expected_runtime_manifest.encode("utf-8")).hexdigest(),
        )

        replacement = json.loads(json.dumps(runtime_state))
        replacement_start = float(runtime_state["gate_start_epoch"]) + 60.0
        replacement["gate_start_epoch"] = replacement_start
        replacement["gate_start_at"] = runtime._utc_timestamp(replacement_start)
        replacement["gate"]["gate_id"] = ""
        replacement["pre_gate_running"] = {
            "attempt_keys": [],
            "queue_job_keys": [],
            "attempt_count": 0,
            "queue_job_count": 0,
            "policy": "record_result_but_exclude_from_formal_gate",
        }
        replacement_gate = observation.initialize_observation_gate(
            config,
            replacement,
            now=replacement_start,
        )
        replacement["gate"]["gate_id"] = replacement_gate["gate_id"]
        atomic_write_text(
            runtime_path,
            json.dumps(replacement, sort_keys=True, indent=2) + "\n",
        )
        historical_after_rearm = self._database_rows(
            config,
            """
            SELECT legacy_runtime_manifest_json, legacy_runtime_manifest_sha256,
                   pre_gate_attempt_count
            FROM m2_observation_gates WHERE gate_id=?
            """,
            (first["gate_id"],),
        )[0]
        self.assertEqual(historical_after_rearm[0], expected_runtime_manifest)
        self.assertEqual(historical_after_rearm[1], stored[0][5])
        self.assertEqual(historical_after_rearm[2], 8)

    def test_rearm_requires_exact_explicit_legacy_import(self) -> None:
        config = self._config("legacy-explicit-import-required")
        runtime_state = self._read(runtime.runtime_state_path(config))
        runtime_state["pre_gate_running"]["attempt_keys"] = [
            f"pre-gate-{number}" for number in range(8)
        ]
        runtime_state["pre_gate_running"]["attempt_count"] = 8
        legacy = {
            "gate_baseline_version": runtime_state["gate_baseline_version"],
            "gate_start_at": runtime_state["gate_start_at"],
            "gate_start_epoch": runtime_state["gate_start_epoch"],
            "runtime_baseline": runtime_state["baseline"],
            "created_at": runtime_state["gate_start_epoch"],
            "claims": {},
        }
        atomic_write_text(
            Path(config.m2_server_canary_observation_state_path),
            json.dumps(legacy, sort_keys=True, indent=2) + "\n",
        )
        self._remove_database(config)

        candidate = json.loads(json.dumps(runtime_state))
        candidate_start = float(runtime_state["gate_start_epoch"]) + 60.0
        candidate["gate_start_epoch"] = candidate_start
        candidate["gate_start_at"] = runtime._utc_timestamp(candidate_start)
        candidate["gate"]["gate_id"] = ""
        candidate["pre_gate_running"] = {
            "attempt_keys": [],
            "queue_job_keys": [],
            "attempt_count": 0,
            "queue_job_count": 0,
            "policy": "record_result_but_exclude_from_formal_gate",
        }
        with self.assertRaisesRegex(
            observation.ObservationStateError,
            "legacy_gate_requires_exact_invalidation",
        ):
            observation.initialize_observation_gate(
                config,
                candidate,
                now=candidate_start,
            )
        self.assertEqual(
            self._database_rows(
                config,
                "SELECT COUNT(*) FROM m2_observation_gates",
            )[0][0],
            0,
        )

    def test_v2_legacy_gate_is_enriched_once_during_v3_migration(self) -> None:
        config = self._config("legacy-v2-to-v3")
        runtime_path = runtime.runtime_state_path(config)
        runtime_state = self._read(runtime_path)
        runtime_state["pre_gate_running"]["attempt_keys"] = [
            f"pre-gate-{number}" for number in range(8)
        ]
        runtime_state["pre_gate_running"]["attempt_count"] = 8
        atomic_write_text(
            runtime_path,
            json.dumps(runtime_state, sort_keys=True, indent=2) + "\n",
        )
        legacy = {
            "gate_baseline_version": runtime_state["gate_baseline_version"],
            "gate_start_at": runtime_state["gate_start_at"],
            "gate_start_epoch": runtime_state["gate_start_epoch"],
            "runtime_baseline": runtime_state["baseline"],
            "created_at": runtime_state["gate_start_epoch"],
            "claims": {},
        }
        atomic_write_text(
            Path(config.m2_server_canary_observation_state_path),
            json.dumps(legacy, sort_keys=True, indent=2) + "\n",
        )
        self._remove_database(config)
        expected = {
            "expected_gate_baseline_version": str(
                runtime_state["gate_baseline_version"]
            ),
            "expected_gate_start_at": str(runtime_state["gate_start_at"]),
            "expected_worker_sha": "1" * 40,
            "expected_webui_sha": "2" * 40,
            "expected_pre_gate_attempts": 8,
        }
        first = observation.invalidate_observation_gate(
            config,
            reason=observation.INVALIDATED_AUTOMATION,
            **expected,
        )

        database = observation_database_path(config)
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "DROP TRIGGER trg_m2_observation_legacy_runtime_write_once"
            )
            connection.execute(
                "UPDATE m2_observation_gates SET legacy_runtime_manifest_json='', "
                "legacy_runtime_manifest_sha256='' WHERE gate_id=?",
                (first["gate_id"],),
            )
            connection.execute(
                "UPDATE m2_observation_meta SET value='2' WHERE key='schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        replay = observation.invalidate_observation_gate(
            config,
            reason=observation.INVALIDATED_AUTOMATION,
            **expected,
        )
        self.assertTrue(replay["already_invalidated"])
        stored = self._database_rows(
            config,
            "SELECT legacy_runtime_manifest_json, legacy_runtime_manifest_sha256 "
            "FROM m2_observation_gates WHERE gate_id=?",
            (first["gate_id"],),
        )[0]
        self.assertEqual(json.loads(str(stored[0])), runtime_state)
        self.assertEqual(
            stored[1], hashlib.sha256(str(stored[0]).encode("utf-8")).hexdigest()
        )
        connection = sqlite3.connect(database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE m2_observation_gates "
                    "SET legacy_runtime_manifest_json='{}' WHERE gate_id=?",
                    (first["gate_id"],),
                )
        finally:
            connection.rollback()
            connection.close()

    def test_legacy_gate_expectation_mismatch_is_zero_write(self) -> None:
        config = self._config("legacy-expectation-mismatch")
        runtime_state = self._read(runtime.runtime_state_path(config))
        atomic_write_text(
            Path(config.m2_server_canary_observation_state_path),
            json.dumps(
                {
                    "gate_baseline_version": runtime_state["gate_baseline_version"],
                    "gate_start_at": runtime_state["gate_start_at"],
                    "gate_start_epoch": runtime_state["gate_start_epoch"],
                    "runtime_baseline": runtime_state["baseline"],
                    "claims": {},
                },
                sort_keys=True,
            )
            + "\n",
        )
        self._remove_database(config)

        with self.assertRaisesRegex(
            observation.ObservationStateError,
            "runtime_legacy_gate_expectation_mismatch",
        ):
            observation.invalidate_observation_gate(
                config,
                reason=observation.INVALIDATED_AUTOMATION,
                expected_gate_baseline_version=str(
                    runtime_state["gate_baseline_version"]
                ),
                expected_gate_start_at=str(runtime_state["gate_start_at"]),
                expected_worker_sha="f" * 40,
                expected_webui_sha="2" * 40,
                expected_pre_gate_attempts=0,
            )
        self.assertFalse(observation_database_path(config).exists())

    def test_missing_runtime_manifest_blocks_new_claim_and_trips(self) -> None:
        config = self._config("runtime-missing")
        runtime.runtime_state_path(config).unlink()

        self.assertFalse(observation.admit_new_job(config))
        self.assertTrue(observation.circuit_breaker_active(config))
        breaker = self._read(Path(config.m2_server_canary_circuit_breaker_state_path))
        self.assertIn(
            "runtime_change",
            [item["reason_code"] for item in breaker["reasons"]],
        )

    def test_tripped_breaker_does_not_hide_runtime_drift_invalidation(self) -> None:
        config = self._config("tripped-runtime-drift")
        runtime_state = self._read(runtime.runtime_state_path(config))
        expected_source_revision = runtime_state["baseline"][
            "worker_source_revision"
        ]
        observation.trip_circuit_breaker(
            config,
            "manual_test_trip",
            evidence={"stage": "test", "error_code": "manual_test_trip"},
        )
        Path(config.m2_guardrail_source_revision_file).write_text(
            "f" * 64 + "\n",
            encoding="utf-8",
        )

        self.assertFalse(observation.admit_new_job(config))
        gate_status, reason, evidence_text = self._database_rows(
            config,
            """
            SELECT status, invalidation_reason, invalidation_evidence_json
            FROM m2_observation_gates
            """,
        )[0]
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(reason, "INVALIDATED_BY_RUNTIME_CHANGE")
        evidence = json.loads(evidence_text)
        self.assertEqual(
            evidence["reason_code"],
            "live_worker_source_revision_mismatch",
        )
        self.assertEqual(
            evidence["expected"]["worker_source_revision"],
            expected_source_revision,
        )
        self.assertEqual(evidence["actual"]["worker_source_revision"], "f" * 64)

    def test_same_container_python_mutation_invalidates_on_admission(self) -> None:
        config = self._config("same-container-code-admission")
        runtime_state = self._read(runtime.runtime_state_path(config))
        expected_code_revision = runtime_state["baseline"][
            "worker_runtime_code_revision"
        ]
        app_root = Path(config.m2_guardrail_runtime_app_root)

        (app_root / "worker.py").write_text(
            "RUNTIME_VALUE = 2\n",
            encoding="utf-8",
        )

        self.assertFalse(observation.admit_new_job(config))
        gate_status, evidence_text = self._database_rows(
            config,
            "SELECT status, invalidation_evidence_json FROM m2_observation_gates",
        )[0]
        evidence = json.loads(str(evidence_text))
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(
            evidence["reason_code"],
            "live_worker_runtime_code_revision_mismatch",
        )
        self.assertEqual(
            evidence["expected"]["worker_runtime_code_revision"],
            expected_code_revision,
        )
        self.assertNotEqual(
            evidence["actual"]["worker_runtime_code_revision"],
            expected_code_revision,
        )
        self.assertTrue(observation.circuit_breaker_active(config))

    def test_disabled_observer_cannot_bypass_an_existing_m2_guardrail(self) -> None:
        config = self._config("observer-disabled")
        config.m2_server_canary_observer_enabled = False

        self.assertFalse(observation.admit_new_job(config))

    def test_admission_requires_durable_sqlite_gate_not_legacy_json(self) -> None:
        config = self._config("invalid-observer-database")
        legacy_path = Path(config.m2_server_canary_observation_state_path)
        legacy_path.write_text("{not valid legacy json", encoding="utf-8")
        self.assertTrue(observation.admit_new_job(config))

        self._remove_database(config)
        self.assertFalse(observation.admit_new_job(config))
        breaker = self._read(Path(config.m2_server_canary_circuit_breaker_state_path))
        self.assertIn(
            "observation_state_degraded",
            [item["reason_code"] for item in breaker["reasons"]],
        )

    def test_terminal_result_trips_when_durable_database_is_missing(self) -> None:
        config = self._config("terminal-state-missing")
        identity = "attempt-missing"
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        self._remove_database(config)

        with self.assertRaises(observation.ObservationStateError):
            observation.record_job_result(
                config,
                job_identity=identity,
                outcome=self._verified(),
                strict_evidence=strict_evidence_template(passed=True),
            )
        self.assertTrue(observation.circuit_breaker_active(config))

    def test_terminal_result_invalidates_on_runtime_baseline_drift(self) -> None:
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
        config = self._config("terminal-state-baseline")
        identity = "attempt-baseline"
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        runtime_path = runtime.runtime_state_path(config)
        runtime_state = self._read(runtime_path)
        runtime_state["gate_baseline_version"] = "wrong"
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
        self.assertTrue(result["circuit_breaker_tripped"])
        self.assertFalse(result["strictly_qualified"])
        gate_status, reason = self._database_rows(
            config,
            "SELECT status, invalidation_reason FROM m2_observation_gates",
        )[0]
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertTrue(str(reason))

    def test_terminal_result_detects_code_mutation_with_spoofed_marker(self) -> None:
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
        config = self._config("terminal-code-and-marker-drift")
        identity = "attempt-code-and-marker-drift"
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        runtime_state = self._read(runtime.runtime_state_path(config))
        expected_source_revision = runtime_state["baseline"]["worker_source_revision"]
        expected_code_revision = runtime_state["baseline"][
            "worker_runtime_code_revision"
        ]

        app_root = Path(config.m2_guardrail_runtime_app_root)
        (app_root / "worker.py").write_text(
            "RUNTIME_VALUE = 3\n",
            encoding="utf-8",
        )
        # Rewriting the marker with the frozen value must not hide the live
        # code change; terminal observation independently hashes app files.
        Path(config.m2_guardrail_source_revision_file).write_text(
            str(expected_source_revision) + "\n",
            encoding="utf-8",
        )

        result = observation.record_job_result(
            config,
            job_identity=identity,
            outcome=self._verified(),
            strict_evidence=strict_evidence_template(passed=True),
        )

        self.assertTrue(result["circuit_breaker_tripped"])
        self.assertFalse(result["strictly_qualified"])
        gate_status, evidence_text = self._database_rows(
            config,
            "SELECT status, invalidation_evidence_json FROM m2_observation_gates",
        )[0]
        evidence = json.loads(str(evidence_text))
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(
            evidence["reason_code"],
            "live_worker_runtime_code_revision_mismatch",
        )
        self.assertEqual(
            evidence["expected"]["worker_source_revision"],
            expected_source_revision,
        )
        self.assertEqual(
            evidence["actual"]["worker_source_revision"],
            expected_source_revision,
        )
        self.assertNotEqual(
            evidence["actual"]["worker_runtime_code_revision"],
            expected_code_revision,
        )

    def test_admission_persists_runtime_invalidation_after_context_rollback(
        self,
    ) -> None:
        config = self._config("admission-runtime-rollback")
        self._runtime_manifest_with_different_gate(config)

        self.assertFalse(observation.admit_new_job(config))

        gate_status, reason, evidence_json = self._database_rows(
            config,
            """
            SELECT status, invalidation_reason, invalidation_evidence_json
            FROM m2_observation_gates
            """,
        )[0]
        evidence = json.loads(str(evidence_json))
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(reason, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(evidence["reason_code"], "runtime_gate_identity_mismatch")
        self.assertNotEqual(
            evidence["expected"]["baseline_version"],
            evidence["actual"]["baseline_version"],
        )
        self.assertTrue(observation.circuit_breaker_active(config))

    def test_standalone_claim_persists_runtime_invalidation_after_rollback(
        self,
    ) -> None:
        config = self._config("claim-runtime-rollback")
        self._runtime_manifest_with_different_gate(config)

        with self.assertRaisesRegex(
            observation.ObservationStateError,
            "INVALIDATED_BY_RUNTIME_CHANGE",
        ):
            observation.record_job_claim(
                config,
                job_identity="drifted-standalone-claim",
                claimed_at=time.time(),
            )

        gate_status, enrolled_count = self._database_rows(
            config,
            "SELECT status, enrolled_count FROM m2_observation_gates",
        )[0]
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(enrolled_count, 0)
        self.assertTrue(observation.circuit_breaker_active(config))

    def test_standalone_binding_persists_runtime_invalidation_after_rollback(
        self,
    ) -> None:
        config = self._config("binding-runtime-rollback")
        identity = "bound-before-drift"
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        self._runtime_manifest_with_different_gate(config)

        with self.assertRaisesRegex(
            observation.ObservationStateError,
            "INVALIDATED_BY_RUNTIME_CHANGE",
        ):
            observation.has_durable_gate_claim_binding(
                config,
                job_identity=identity,
            )

        gate_status, enrolled_count = self._database_rows(
            config,
            "SELECT status, enrolled_count FROM m2_observation_gates",
        )[0]
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(enrolled_count, 1)
        self.assertTrue(observation.circuit_breaker_active(config))

    def test_initialize_recovery_persists_mismatched_active_gate_invalidation(
        self,
    ) -> None:
        config = self._config("initialize-runtime-rollback")
        candidate = self._runtime_manifest_with_different_gate(config)
        candidate["gate"] = dict(candidate["gate"])
        candidate["gate"]["gate_id"] = ""

        with self.assertRaisesRegex(
            observation.ObservationStoreError,
            "INVALIDATED_BY_RUNTIME_CHANGE",
        ):
            observation.initialize_observation_gate(
                config,
                candidate,
                now=time.time(),
            )

        gate_status, enrolled_count = self._database_rows(
            config,
            "SELECT status, enrolled_count FROM m2_observation_gates",
        )[0]
        self.assertEqual(gate_status, "INVALIDATED_BY_RUNTIME_CHANGE")
        self.assertEqual(enrolled_count, 0)
        self.assertTrue(observation.circuit_breaker_active(config))

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
        self._remove_database(config)
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

    def test_pending_summary_recovers_after_publish_failure_exactly_once(self) -> None:
        config = self._config("pending-gate-recovery")
        for index in range(19):
            self._record_verified(config, f"pending-{index:03d}")

        identity = "pending-019"
        observation.record_job_claim(
            config,
            job_identity=identity,
            claimed_at=time.time(),
        )
        with mock.patch.object(
            observation,
            "publish_pending_summaries",
            side_effect=OSError("simulated summary publication failure"),
        ):
            with self.assertRaises(OSError):
                observation.record_job_result(
                    config,
                    job_identity=identity,
                    outcome=self._verified(),
                    strict_evidence=strict_evidence_template(passed=True),
                )

        journal = self._database_rows(
            config,
            "SELECT gate_id, status, summary_ready_at, summary_emitted_at "
            "FROM m2_observation_gates",
        )[0]
        gate_id = str(journal[0])
        self.assertEqual(journal[1], "SETTLED")
        self.assertIsNotNone(journal[2])
        self.assertIsNone(journal[3])
        gate_path = (
            Path(config.m2_server_canary_observation_output_dir) / f"{gate_id}.json"
        )
        self.assertFalse(gate_path.exists())

        importlib.reload(observation)
        self.assertEqual(
            observation.publish_pending_summaries(config),
            [f"{gate_id}.json"],
        )
        self.assertTrue(gate_path.is_file())
        self.assertEqual(observation.publish_pending_summaries(config), [])
        emitted_at = self._database_rows(
            config,
            "SELECT summary_emitted_at FROM m2_observation_gates WHERE gate_id=?",
            (gate_id,),
        )[0][0]
        self.assertIsNotNone(emitted_at)

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
            final = self._record_verified(config, f"opaque-attempt-{index}")

        database_bytes = observation_database_path(config).read_bytes()
        summary_text = (
            Path(config.m2_server_canary_observation_output_dir)
            / f"{final['gate_id']}.json"
        ).read_text(encoding="utf-8")
        persisted = database_bytes + summary_text.encode("utf-8")
        for secret in (
            raw_identity,
            "PRIVATE_MEDIA_TITLE_MARKER",
            "PRIVATE_TRACE_MARKER",
            "PRIVATE_HOST_MARKER",
            "PRIVATE_PORT_MARKER",
            "PRIVATE_FULL_LOG_MARKER",
        ):
            self.assertNotIn(secret.encode("utf-8"), persisted)
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

    def test_same_job_retries_do_not_advance_identical_failure_streak(self) -> None:
        config = self._config("identical-one-job")
        repeated = self._failed(stage="translation", error_code="model_timeout")

        for index in range(5):
            result = observation.record_job_result(
                config,
                job_identity=f"attempt-{index}",
                gate_job_identity="one-durable-obligation",
                outcome=repeated,
            )
            self.assertFalse(result["circuit_breaker_tripped"])
            self.assertTrue(observation.admit_new_job(config))

        connection = sqlite3.connect(observation_database_path(config))
        try:
            values = dict(
                connection.execute(
                    "SELECT key,value FROM m2_observation_meta WHERE key IN "
                    "('identical_failure_streak','identical_failure_job_ids')"
                ).fetchall()
            )
        finally:
            connection.close()
        self.assertEqual(values["identical_failure_streak"], "1")
        self.assertEqual(len(json.loads(values["identical_failure_job_ids"])), 1)

    def test_quality_review_outcomes_do_not_participate_in_failure_streak(self) -> None:
        config = self._config("quality-not-system-failure")
        outcome = self._failed(
            stage="source_selection_review",
            error_code="source_selection_needs_review",
        )
        outcome["terminal_status"] = "RETRYING"
        for index in range(5):
            result = observation.record_job_result(
                config,
                job_identity=f"quality-attempt-{index}",
                gate_job_identity=f"quality-job-{index}",
                outcome=outcome,
            )
            self.assertFalse(result["circuit_breaker_tripped"])
        self.assertTrue(observation.admit_new_job(config))

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
        pause_path = Path(config.work_path) / "ai_control.json"
        durable_pause = pause_path.read_text(encoding="utf-8")
        breaker_path = Path(config.m2_server_canary_circuit_breaker_state_path)
        breaker_path.write_text("{not valid json", encoding="utf-8")
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False

        self.assertTrue(observation.circuit_breaker_active(config))
        self.assertFalse(observation.admit_new_job(config))
        self.assertEqual(
            sentinel.read_text(encoding="utf-8"), "checkpoint-must-survive"
        )
        self.assertEqual(pause_path.read_text(encoding="utf-8"), durable_pause)

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

    def test_terminal_observer_uses_durable_attempt_and_obligation_identity(self) -> None:
        config = self._config("main-terminal-observation")
        state = mock.Mock()
        state.get_ai_delivery_attempt.return_value = {
            "obligation_id": "opaque-obligation-id",
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
            main_module._record_terminal_m2_observation(
                state,
                private_video,
                config,
                "opaque-attempt-id",
            )

        build_evidence.assert_called_once_with(
            state,
            private_video,
            config,
            "opaque-attempt-id",
            {"status": "ARMED"},
        )
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["job_identity"], "opaque-attempt-id")
        self.assertEqual(
            record.call_args.kwargs["gate_job_identity"],
            "opaque-obligation-id",
        )
        self.assertEqual(record.call_args.kwargs["outcome"], strict_result["outcome"])
        self.assertEqual(
            record.call_args.kwargs["strict_evidence"], strict_result["evidence"]
        )
        self.assertNotIn(str(private_video), repr(record.call_args))

    def test_terminal_queue_wrapper_requests_atomic_observation(self) -> None:
        config = self._config("main-terminal-wrapper")
        state = mock.Mock()
        video = Path("opaque-video")
        with mock.patch.object(main_module, "_mark_queue_result") as commit:
            main_module._mark_queue_result_and_observe(
                state,
                video,
                True,
                config,
                delivery_attempt_id="opaque-attempt-id",
            )
        commit.assert_called_once_with(
            state,
            video,
            True,
            config,
            delivery_attempt_id="opaque-attempt-id",
            observe_terminal=True,
            logger=None,
        )


if __name__ == "__main__":
    unittest.main()
