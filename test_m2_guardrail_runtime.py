from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest

import m2_guardrail_runtime as runtime
from m2_observation_store import ELIGIBILITY_POLICY_VERSION
from source_analyzer import DECISION_SCHEMA_VERSION, DECISION_VERSION


WORKER_SHA = "a" * 40
WEBUI_SHA = "b" * 40
WORKER_SOURCE_REVISION = "1" * 64
WEBUI_SOURCE_REVISION = "2" * 64
WORKER_IMAGE = "sha256:" + "c" * 64
WEBUI_IMAGE = "sha256:" + "d" * 64
WORKER_CONTAINER_ID = "e" * 64
WORKER_CONTAINER_IDENTITY = "unit-test-worker"


class M2GuardrailRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.work = root / "work"
        self.work.mkdir()
        self.container_identity = root / "container-identity"
        self.container_identity.write_text(
            WORKER_CONTAINER_IDENTITY + "\n",
            encoding="utf-8",
        )
        (self.work / "ai_control.json").write_text(
            json.dumps(
                {
                    "paused": True,
                    "updated_at": time.time(),
                    "requested_by": "unit-test",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.config = SimpleNamespace(
            work_path=self.work,
            m2_guardrail_container_identity_file=self.container_identity,
            input_path=root / "production-input",
            completed_delivery_path=str(root / "production-output"),
            scanner_state_path="scanner.sqlite3",
            m2_server_canary_observer_enabled=True,
            m2_server_canary_circuit_breaker_enabled=True,
            m2_server_canary_circuit_breaker_state_path="breaker.json",
            m2_server_canary_observation_state_path="observation.json",
            m2_server_canary_observation_output_dir="observation-summaries",
            m2_server_canary_observation_gate_size=20,
            max_concurrent_videos=1,
            source_integrity_sha256_enabled=True,
            source_decision_schema_version=DECISION_SCHEMA_VERSION,
            source_decision_version=DECISION_VERSION,
            translator_api_key="must-never-be-published",
        )
        self.revision = root / ".source-revision"
        self.revision.write_text(WORKER_SOURCE_REVISION + "\n", encoding="utf-8")
        self.mounted_config = root / "runtime-config.yaml"
        self.mounted_config.write_text("enabled: true\n", encoding="utf-8")
        self.worker_repo = root / "worker-repo"
        self.webui_repo = root / "webui-repo"
        self._create_source_revision_repositories()
        self.config.m2_guardrail_runtime_app_root = self.worker_repo
        self._create_queue_database()
        self.fault_summary = self._write_fault_results()
        import m2_production_observation

        self.previous_process_latch = m2_production_observation._PROCESS_LOCAL_CIRCUIT_OPEN
        m2_production_observation._PROCESS_LOCAL_CIRCUIT_OPEN = False

    def tearDown(self) -> None:
        import m2_production_observation

        m2_production_observation._PROCESS_LOCAL_CIRCUIT_OPEN = self.previous_process_latch
        self.temp.cleanup()

    def _create_queue_database(self) -> None:
        database = self.work / "scanner.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE ai_delivery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE ai_candidate_queue (
                    path TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );
                INSERT INTO ai_delivery_attempts(attempt_id, status)
                VALUES('attempt-before-gate', 'running');
                INSERT INTO ai_candidate_queue(path, status)
                VALUES('/production/media/private-title.mkv', 'running');
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _create_source_revision_repositories(self) -> None:
        self.worker_repo.mkdir()
        for name, content in (
            ("Dockerfile", "FROM scratch\n"),
            ("requirements.txt", "example==1\n"),
            ("config.yaml", "enabled: true\n"),
            ("worker.py", "VALUE = 1\n"),
        ):
            (self.worker_repo / name).write_text(content, encoding="utf-8")
        self.webui_repo.mkdir()
        (self.webui_repo / "src").mkdir()
        (self.webui_repo / "tests").mkdir()
        for name, content in (
            ("Dockerfile", "FROM scratch\n"),
            ("requirements.txt", "example==1\n"),
            ("package.json", "{}\n"),
            ("package-lock.json", "{}\n"),
            ("index.html", "<main></main>\n"),
            ("vite.config.js", "export default {}\n"),
            ("app.py", "VALUE = 1\n"),
            ("control_api.py", "VALUE = 2\n"),
            ("src/app.js", "export const x = 1\n"),
            ("tests/app.test.js", "export const ok = true\n"),
        ):
            path = self.webui_repo / name
            path.write_text(content, encoding="utf-8")

    def _write_fault_results(self) -> Path:
        run_id = "m2-guardrail-fi-20260904T010203123456Z-abcdef12"
        directory = Path(self.temp.name) / run_id
        directory.mkdir()
        log_path = directory / "events.jsonl"
        case_results = []
        log_events = []
        for breaker in runtime.REQUIRED_BREAKERS:
            checks = {
                "production_admission_path_called": True,
                "new_job_claim_stopped": True,
                "queue_preserved": True,
                "checkpoint_preserved": True,
                "running_job_not_interrupted": True,
                "no_false_completed": True,
                "reason_evidence_persisted": True,
                "safe_recovery": True,
                "source_unchanged": True,
                "production_output_untouched": True,
            }
            case_results.append({"fault": breaker, "passed": True, "checks": checks})
            log_events.append(
                {
                    "event": "case_verified",
                    "fault": breaker,
                    "checks": checks,
                    "breaker_reason": breaker,
                    "breaker_evidence": {"stage": "isolated"},
                }
            )
        log_path.write_text(
            "".join(json.dumps(event) + "\n" for event in log_events),
            encoding="utf-8",
        )
        summary = directory / "result.json"
        payload = {
            "contract": runtime.FAULT_RESULT_CONTRACT,
            "schema_version": runtime.RUNTIME_SCHEMA_VERSION,
            "run_id": run_id,
            "worker_source_revision": WORKER_SOURCE_REVISION,
            "started_at": "1970-01-01T00:00:10Z",
            "started_at_epoch": 10.0,
            "finished_at": "1970-01-01T00:00:20Z",
            "finished_at_epoch": 20.0,
            "status": "PASS",
            "breaker_tests_passed": 7,
            "breaker_tests_total": 7,
            "production_resources_affected": False,
            "production_config_loaded": False,
            "case_results": case_results,
            "log_path": str(log_path),
            "result_path": str(summary),
        }
        summary.write_text(json.dumps(payload), encoding="utf-8")
        return summary

    def _evidence(self) -> dict[str, object]:
        runtime_instance = runtime.worker_runtime_instance_fingerprint(self.config)
        return {
            "worker_commit_sha": WORKER_SHA,
            "webui_commit_sha": WEBUI_SHA,
            "worker_source_revision": WORKER_SOURCE_REVISION,
            "worker_runtime_code_revision": (
                runtime.compute_worker_runtime_code_revision(self.worker_repo)
            ),
            "webui_source_revision": WEBUI_SOURCE_REVISION,
            "worker_image_id": WORKER_IMAGE,
            "webui_image_id": WEBUI_IMAGE,
            "worker_container_id": WORKER_CONTAINER_ID,
            "worker_container_identity": WORKER_CONTAINER_IDENTITY,
            "worker_runtime_instance_fingerprint": runtime_instance[
                "runtime_instance_fingerprint"
            ],
            "configuration_fingerprint": runtime.configuration_fingerprint(self.config),
            "decision": runtime._decision_descriptor(self.config),
            "fault_results": runtime.validate_fault_results(
                self.config,
                self.fault_summary,
                expected_worker_source_revision=WORKER_SOURCE_REVISION,
                not_before_epoch=1.0,
            ),
            "runtime_checks": {
                "worker_container_running": True,
                "webui_container_running": True,
                "worker_config_mount_readonly": True,
                "worker_command_uses_config": True,
                "worker_config_unchanged_since_start": True,
            },
        }

    def test_validates_all_seven_isolated_breaker_results_and_log(self) -> None:
        result = runtime.validate_fault_results(
            self.config,
            self.fault_summary,
            expected_worker_source_revision=WORKER_SOURCE_REVISION,
            not_before_epoch=1.0,
        )

        self.assertEqual(result["passed_count"], 7)
        self.assertEqual(result["required_count"], 7)
        self.assertFalse(result["production_resources_affected"])
        self.assertRegex(result["summary_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(result["full_log_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn(str(self.fault_summary.parent), json.dumps(result))

    def test_accepts_actual_isolated_fault_harness_artifacts(self) -> None:
        from m2_guardrail_fault_injection import run_fault_suite

        with tempfile.TemporaryDirectory() as raw:
            not_before = time.time() - 1.0
            generated = run_fault_suite(
                raw,
                worker_source_revision_file=self.revision,
            )
            result = runtime.validate_fault_results(
                self.config,
                generated["result_path"],
                expected_worker_source_revision=WORKER_SOURCE_REVISION,
                not_before_epoch=not_before,
            )

        self.assertEqual(result["passed_count"], 7)
        self.assertFalse(result["production_resources_affected"])

    def test_rejects_breaker_result_missing_required_safety_assertion(self) -> None:
        payload = json.loads(self.fault_summary.read_text(encoding="utf-8"))
        payload["case_results"][0]["checks"]["queue_preserved"] = False
        self.fault_summary.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "fault_assertion_failed_source_mutation_queue_preserved",
        ):
            runtime.validate_fault_results(
                self.config,
                self.fault_summary,
                expected_worker_source_revision=WORKER_SOURCE_REVISION,
                not_before_epoch=1.0,
            )

    def test_rejects_stale_or_wrong_revision_fault_results(self) -> None:
        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "fault_summary_not_fresh",
        ):
            runtime.validate_fault_results(
                self.config,
                self.fault_summary,
                expected_worker_source_revision=WORKER_SOURCE_REVISION,
                not_before_epoch=11.0,
            )

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "fault_worker_source_revision_mismatch",
        ):
            runtime.validate_fault_results(
                self.config,
                self.fault_summary,
                expected_worker_source_revision="9" * 64,
                not_before_epoch=1.0,
            )

    def test_initializes_immutable_armed_gate_and_excludes_pre_gate_running_jobs(self) -> None:
        state = runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=1_788_454_923.0,
        )

        self.assertEqual(state["status"], "ARMED")
        self.assertEqual(state["gate"]["target"], 20)
        self.assertEqual(state["gate"]["progress"], 0)
        self.assertEqual(state["gate"]["claimed_after_gate_start"], 0)
        self.assertEqual(state["gate"]["completed_strict_verified"], 0)
        self.assertEqual(
            state["gate"]["eligibility_policy_version"],
            ELIGIBILITY_POLICY_VERSION,
        )
        self.assertRegex(
            state["gate"]["gate_id"],
            r"^m2-gate-[0-9]{8}T[0-9]{12}Z-[0-9a-f]{10}$",
        )
        self.assertEqual(
            state["baseline"]["eligibility_policy_version"],
            ELIGIBILITY_POLICY_VERSION,
        )
        self.assertEqual(state["gate_start_at"], "2026-09-03T17:02:03Z")
        self.assertEqual(state["pre_gate_running"]["attempt_count"], 1)
        self.assertEqual(state["pre_gate_running"]["queue_job_count"], 1)
        persisted = (self.work / "m2_guardrail_runtime.json").read_text(encoding="utf-8")
        self.assertNotIn("private-title", persisted)
        self.assertNotIn("/production/", persisted)
        self.assertNotIn("must-never-be-published", persisted)

        repeated = runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=1_788_455_000.0,
        )
        self.assertEqual(repeated["gate_start_at"], "2026-09-03T17:02:03Z")
        self.assertEqual(repeated["gate"]["gate_id"], state["gate"]["gate_id"])

    def test_gate_claim_requires_post_start_matching_baseline_and_not_preexisting(self) -> None:
        state = runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=100.0,
        )
        baseline = state["gate_baseline_version"]

        self.assertEqual(
            runtime.gate_claim_eligible(
                state,
                job_identity="attempt-before-gate",
                claimed_at=101.0,
                gate_baseline_version=baseline,
            ),
            (False, "running_before_gate_start"),
        )
        self.assertEqual(
            runtime.gate_claim_eligible(
                state,
                job_identity="new-attempt",
                claimed_at=100.0,
                gate_baseline_version=baseline,
            ),
            (False, "claimed_before_gate_start"),
        )
        self.assertEqual(
            runtime.gate_claim_eligible(
                state,
                job_identity="new-attempt",
                claimed_at=101.0,
                gate_baseline_version="wrong",
            ),
            (False, "runtime_baseline_mismatch"),
        )
        self.assertEqual(
            runtime.gate_claim_eligible(
                state,
                job_identity="new-attempt",
                claimed_at=101.0,
                gate_baseline_version=baseline,
            ),
            (True, "eligible"),
        )

        missing_policy = json.loads(json.dumps(state))
        missing_policy["gate"].pop("eligibility_policy_version")
        self.assertEqual(
            runtime.gate_claim_eligible(
                missing_policy,
                job_identity="new-attempt",
                claimed_at=101.0,
                gate_baseline_version=baseline,
            ),
            (False, "eligibility_policy_mismatch"),
        )

    def test_initialize_requires_durable_pause_and_runtime_requires_gate_id(self) -> None:
        pause = self.work / "ai_control.json"
        pause.unlink()
        with self.assertRaisesRegex(ValueError, "ai_claim_pause_missing"):
            runtime.initialize_gate(
                self.config,
                self._evidence(),
                source_revision_file=self.revision,
                now=100.0,
            )

        pause.write_text(
            json.dumps(
                {
                    "paused": True,
                    "updated_at": time.time(),
                    "requested_by": "unit-test",
                }
            ),
            encoding="utf-8",
        )
        state = runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=100.0,
        )
        state["gate"].pop("gate_id")
        runtime.runtime_state_path(self.config).write_text(
            json.dumps(state),
            encoding="utf-8",
        )
        status = runtime.runtime_guardrail_status(
            self.config,
            source_revision_file=self.revision,
        )
        self.assertEqual(status["status"], "DEGRADED")
        self.assertEqual(status["reason_code"], "live_eligibility_policy_mismatch")

    def test_runtime_status_is_limited_to_explicit_contract_states(self) -> None:
        decision = runtime._decision_descriptor(self.config)
        self.assertEqual(runtime._local_guardrail_status(self.config, decision)[0], "ARMED")
        missing = runtime.runtime_guardrail_status(
            self.config,
            source_revision_file=self.revision,
        )
        self.assertEqual(missing["status"], "DEGRADED")
        self.assertEqual(missing["reason_code"], "runtime_state_missing")

        self.config.m2_server_canary_observer_enabled = False
        self.assertEqual(runtime._local_guardrail_status(self.config, decision)[0], "DISARMED")
        self.assertEqual(
            runtime.runtime_guardrail_status(
                self.config,
                source_revision_file=self.revision,
            )["status"],
            "DISARMED",
        )
        self.config.m2_server_canary_observer_enabled = True

        runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=100.0,
        )

        (self.work / "breaker.json").write_text(
            json.dumps({"schema_version": 1, "tripped": True}),
            encoding="utf-8",
        )
        self.assertEqual(runtime._local_guardrail_status(self.config, decision)[0], "TRIPPED")
        self.assertEqual(
            runtime.runtime_guardrail_status(
                self.config,
                source_revision_file=self.revision,
            )["status"],
            "TRIPPED",
        )
        (self.work / "breaker.json").unlink()

        self.config.max_concurrent_videos = 2
        self.assertEqual(runtime._local_guardrail_status(self.config, decision)[0], "DEGRADED")

    def test_admission_status_requires_exact_live_runtime_baseline(self) -> None:
        state = runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=100.0,
        )

        status = runtime.runtime_guardrail_status(
            self.config,
            source_revision_file=self.revision,
        )
        self.assertEqual(status["status"], "ARMED")
        self.assertEqual(status["state"], state)

        self.revision.write_text("3" * 64 + "\n", encoding="utf-8")
        changed = runtime.runtime_guardrail_status(
            self.config,
            source_revision_file=self.revision,
        )
        self.assertEqual(changed["status"], "DEGRADED")
        self.assertEqual(changed["reason_code"], "live_worker_source_revision_mismatch")
        self.assertIsNone(changed["state"])

    def test_same_container_in_place_python_mutation_degrades_runtime(self) -> None:
        runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=100.0,
        )
        original_identity = runtime.worker_runtime_instance_fingerprint(self.config)

        (self.worker_repo / "worker.py").write_text("VALUE = 2\n", encoding="utf-8")
        changed = runtime.runtime_guardrail_status(
            self.config,
            source_revision_file=self.revision,
        )

        self.assertEqual(
            runtime.worker_runtime_instance_fingerprint(self.config),
            original_identity,
        )
        self.assertEqual(changed["status"], "DEGRADED")
        self.assertEqual(
            changed["reason_code"],
            "live_worker_runtime_code_revision_mismatch",
        )
        self.assertIsNone(changed["state"])

    def test_initialize_rejects_live_code_that_differs_from_host_evidence(self) -> None:
        evidence = self._evidence()
        (self.worker_repo / "worker.py").write_text("VALUE = 2\n", encoding="utf-8")

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "worker_runtime_code_revision_changed_during_arm",
        ):
            runtime.initialize_gate(
                self.config,
                evidence,
                source_revision_file=self.revision,
                now=100.0,
            )

    def test_container_recreation_is_runtime_drift_but_same_identity_restart_is_not(self) -> None:
        state = runtime.initialize_gate(
            self.config,
            self._evidence(),
            source_revision_file=self.revision,
            now=100.0,
        )
        same_instance = runtime.runtime_guardrail_status(
            self.config,
            source_revision_file=self.revision,
        )
        self.assertEqual(same_instance["status"], "ARMED")
        self.assertEqual(same_instance["state"]["gate"]["gate_id"], state["gate"]["gate_id"])

        self.container_identity.write_text("recreated-worker\n", encoding="utf-8")
        recreated = runtime.runtime_guardrail_status(
            self.config,
            source_revision_file=self.revision,
        )
        self.assertEqual(recreated["status"], "DEGRADED")
        self.assertEqual(
            recreated["reason_code"],
            "live_worker_container_identity_mismatch",
        )
        self.assertIsNone(recreated["state"])

    def test_host_arm_checks_live_containers_and_returns_initialized_summary(self) -> None:
        probe = {
            "status": "ARMED",
            "reason_code": "runtime_guardrails_loaded",
            "worker_source_revision": runtime.compute_worker_source_revision(
                self.worker_repo
            ),
            "worker_runtime_code_revision": (
                runtime.compute_worker_runtime_code_revision(self.worker_repo)
            ),
            "configuration_fingerprint": "sha256:" + "e" * 64,
            "worker_container_identity": WORKER_CONTAINER_IDENTITY,
            "worker_runtime_instance_fingerprint": "sha256:" + "6" * 64,
            "decision": {"schema_version": 1, "version": DECISION_VERSION, "contract": "subtitle-source-priority-v1"},
            "fault_results": {
                "passed_count": 7,
                "required_count": 7,
                "summary_sha256": "sha256:" + "f" * 64,
                "full_log_sha256": "sha256:" + "0" * 64,
                "production_resources_affected": False,
            },
        }
        initialized = {
            "status": "ARMED",
            "worker_runtime_sha": WORKER_SHA,
            "webui_runtime_sha": WEBUI_SHA,
            "worker_source_revision": runtime.compute_worker_source_revision(
                self.worker_repo
            ),
            "worker_runtime_code_revision": (
                runtime.compute_worker_runtime_code_revision(self.worker_repo)
            ),
            "webui_source_revision": runtime.compute_webui_source_revision(
                self.webui_repo
            ),
            "worker_image_id": WORKER_IMAGE,
            "webui_image_id": WEBUI_IMAGE,
            "configuration_fingerprint": probe["configuration_fingerprint"],
            "decision_schema_version": 1,
            "gate_start_at": "2026-09-04T01:02:03Z",
            "gate_baseline_version": "m2-guardrail-v1:test",
            "initial_gate_progress": "0/20",
            "breaker_tests_passed": 7,
            "production_resources_affected": False,
        }
        commands: list[list[str]] = []

        def runner(command: list[str], stdin: str | None, timeout: float) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[0] == "git" and "status" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "git" and "ls-files" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "git" and "rev-parse" in command:
                revision = WORKER_SHA if str(self.worker_repo) in command else WEBUI_SHA
                return subprocess.CompletedProcess(command, 0, revision + "\n", "")
            if command[1:3] == ["inspect", "anime-subtitle-worker"]:
                payload = [{
                    "Id": WORKER_CONTAINER_ID,
                    "State": {"Running": True, "StartedAt": "2099-01-01T00:00:00Z"},
                    "Image": WORKER_IMAGE,
                    "Args": ["main.py", "--config", "config.yaml", "--auto-watch"],
                    "Config": {"Hostname": WORKER_CONTAINER_IDENTITY, "WorkingDir": "/app", "Cmd": ["python", "main.py", "--config", "config.yaml", "--auto-watch"]},
                    "Mounts": [{"Destination": "/app/config.yaml", "Source": str(self.mounted_config), "RW": False}],
                }]
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            if command[1:3] == ["inspect", "anime-subtitle-worker-webui"]:
                payload = [{"Id": "f" * 64, "State": {"Running": True}, "Image": WEBUI_IMAGE, "Config": {}, "Mounts": []}]
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            if "probe" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(probe), "")
            if command[-2:] == ["cat", "/app/.source-revision"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    runtime.compute_webui_source_revision(self.webui_repo) + "\n",
                    "",
                )
            if "initialize" in command:
                self.assertIsNotNone(stdin)
                evidence = json.loads(str(stdin))
                self.assertEqual(evidence["worker_image_id"], WORKER_IMAGE)
                self.assertEqual(evidence["worker_container_id"], WORKER_CONTAINER_ID)
                self.assertEqual(
                    evidence["worker_container_identity"],
                    WORKER_CONTAINER_IDENTITY,
                )
                self.assertEqual(
                    evidence["worker_runtime_code_revision"],
                    runtime.compute_worker_runtime_code_revision(self.worker_repo),
                )
                return subprocess.CompletedProcess(command, 0, json.dumps(initialized), "")
            raise AssertionError(f"unexpected command: {command}")

        result = runtime.arm_runtime_on_host(
            docker_binary="docker",
            worker_container="anime-subtitle-worker",
            webui_container="anime-subtitle-worker-webui",
            expected_worker_commit_sha=WORKER_SHA,
            expected_webui_commit_sha=WEBUI_SHA,
            fault_summary_path="/logs/fault/summary.json",
            worker_repo=self.worker_repo,
            webui_repo=self.webui_repo,
            runner=runner,
        )

        self.assertEqual(result, initialized)
        self.assertEqual(len(commands), 11)
        self.assertIn("status", commands[0])
        self.assertIn("inspect", commands[6])
        self.assertIn("probe", commands[8])
        self.assertIn("--fault-not-before-epoch", commands[8])
        self.assertIn("initialize", commands[10])

    def test_host_arm_fails_closed_on_source_revision_mismatch(self) -> None:
        def runner(command: list[str], stdin: str | None, timeout: float) -> subprocess.CompletedProcess[str]:
            if command[0] == "git" and "status" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "git" and "ls-files" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "git" and "rev-parse" in command:
                revision = WORKER_SHA if str(self.worker_repo) in command else WEBUI_SHA
                return subprocess.CompletedProcess(command, 0, revision + "\n", "")
            if command[1] == "inspect":
                is_worker = command[2] == "anime-subtitle-worker"
                payload = [{
                    "Id": WORKER_CONTAINER_ID if is_worker else "f" * 64,
                    "State": {"Running": True, "StartedAt": "2099-01-01T00:00:00Z"},
                    "Image": WORKER_IMAGE if is_worker else WEBUI_IMAGE,
                    "Args": ["main.py", "--config", "config.yaml"],
                    "Config": {"Hostname": WORKER_CONTAINER_IDENTITY, "WorkingDir": "/app", "Cmd": ["python", "main.py", "--config", "config.yaml"]},
                    "Mounts": [{"Destination": "/app/config.yaml", "Source": str(self.mounted_config), "RW": False}],
                }]
                return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
            if "probe" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"status": "ARMED", "worker_source_revision": "9" * 64}),
                    "",
                )
            raise AssertionError("arm must stop before any mutation")

        with self.assertRaisesRegex(runtime.RuntimeContractError, "worker_source_revision_mismatch"):
            runtime.arm_runtime_on_host(
                docker_binary="docker",
                worker_container="anime-subtitle-worker",
                webui_container="anime-subtitle-worker-webui",
                expected_worker_commit_sha=WORKER_SHA,
                expected_webui_commit_sha=WEBUI_SHA,
                fault_summary_path="/logs/fault/summary.json",
                worker_repo=self.worker_repo,
                webui_repo=self.webui_repo,
                runner=runner,
            )

    def test_git_identity_allows_non_source_backup_but_rejects_untracked_python(self) -> None:
        clean_commands: list[list[str]] = []

        def clean_runner(command: list[str], stdin: str | None, timeout: float) -> subprocess.CompletedProcess[str]:
            clean_commands.append(command)
            if "status" in command or "ls-files" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, WORKER_SHA + "\n", "")

        self.assertEqual(
            runtime._clean_git_commit(self.worker_repo, clean_runner, "worker"),
            WORKER_SHA,
        )
        expected_safe_directory = f"safe.directory={self.worker_repo.resolve()}"
        self.assertTrue(clean_commands)
        for command in clean_commands:
            self.assertEqual(command[:3], ["git", "-c", expected_safe_directory])
            self.assertEqual(command[3:5], ["-C", str(self.worker_repo.resolve())])

        def untracked_runner(command: list[str], stdin: str | None, timeout: float) -> subprocess.CompletedProcess[str]:
            if "status" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if "ls-files" in command:
                return subprocess.CompletedProcess(command, 0, "new_runtime.py\n", "")
            raise AssertionError("revision must not be accepted")

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "worker_source_inputs_untracked",
        ):
            runtime._clean_git_commit(self.worker_repo, untracked_runner, "worker")

    def test_runtime_state_override_must_remain_under_work_path(self) -> None:
        allowed = runtime.runtime_state_path(
            self.config,
            self.work / "guardrails" / "state.json",
        )
        self.assertEqual(allowed, (self.work / "guardrails" / "state.json").resolve())

        with self.assertRaisesRegex(
            runtime.RuntimeContractError,
            "runtime_state_path_outside_work",
        ):
            runtime.runtime_state_path(
                self.config,
                Path(self.temp.name) / "outside.json",
            )


if __name__ == "__main__":
    unittest.main()
