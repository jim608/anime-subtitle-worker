from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import m2_production_observation as observation
import m2_production_recovery as recovery
import m2_guardrail_runtime as runtime
import test_m2_production_observation as observation_tests
import test_m2_guardrail_runtime as runtime_tests
import test_m2_production_recovery as recovery_tests
from scan_state import ScanStateStore


class GenericFailureSignatureTests(unittest.TestCase):
    def test_legacy_materialized_language_review_is_not_a_global_failure(self):
        state = {}
        detail = "materialized subtitle language conflicts with persisted strategy: strategy=TRANSLATE_JA_SUBTITLE detected=unknown"
        self.assertEqual(recovery.classify_failure("worker", "worker_unknown", detail), "QUALITY_BLOCKED")
        for index in range(3):
            outcome = {"job_key": str(index), "failed": True, "terminal_status": "RETRYING", "stage": "worker", "error_code": "worker_unknown", "_classification_detail": detail}
            observation._update_failure_streaks(state, outcome)
            self.assertEqual(state["identical_failure_streak"], 0)
        self.assertNotEqual(recovery.classify_failure("worker", "worker_unknown", "unexpected adapter internal error"), "QUALITY_BLOCKED")

    def test_three_quality_jobs_stay_armed_then_same_unknown_across_paths_trips(self):
        fixture = observation_tests.M2ProductionObservationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        config = fixture._config("quality-and-unknown-integration")
        detail = "materialized subtitle language conflicts with persisted strategy: strategy=TRANSLATE_JA_SUBTITLE detected=unknown"
        for index in range(3):
            outcome = fixture._failed(stage="worker", error_code="worker_unknown")
            outcome["detail"] = detail
            result = observation.record_job_result(config, job_identity=f"quality-attempt-{index}", gate_job_identity=f"quality-obligation-{index}", outcome=outcome)
            self.assertFalse(result["circuit_breaker_tripped"])
            self.assertEqual(runtime.runtime_guardrail_status(config)["status"], "ARMED")
        for index, title in enumerate(("First Show", "Second Show", "Third Show")):
            outcome["detail"] = f"unexpected adapter internal error: /anime/{title}/Season 1/Episode {index}.mkv attempt=aiatt_{str(index) * 64}"
            result = observation.record_job_result(config, job_identity=f"unknown-attempt-{index}", gate_job_identity=f"unknown-obligation-{index}", outcome=outcome)
            self.assertEqual(result["circuit_breaker_tripped"], index == 2)
        self.assertEqual(runtime.runtime_guardrail_status(config)["status"], "TRIPPED")

    def test_source_mutation_remains_a_direct_breaker(self):
        fixture = observation_tests.M2ProductionObservationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        config = fixture._config("mutation-integration")
        outcome = fixture._failed(stage="worker", error_code="source_mutation")
        result = observation.record_job_result(config, job_identity="mutation-attempt", gate_job_identity="mutation-obligation", outcome=outcome)
        self.assertTrue(result["circuit_breaker_tripped"])
        self.assertEqual(runtime.runtime_guardrail_status(config)["status"], "TRIPPED")
    def test_same_cause_ignores_media_paths_and_attempt_ids(self):
        details = [
            f"database is locked: /anime/{title}/Season 1/{title} - S01E{index}.mkv "
            f"attempt=aiatt_{str(index) * 64}"
            for index, title in enumerate(("Maid Sama!", "Another Show", "Third Title"), 1)
        ]
        self.assertEqual(len({recovery.normalize_failure_signature("worker", "worker_unknown", value) for value in details}), 1)
        self.assertEqual(
            recovery.normalize_failure_signature("worker", "worker_unknown", "database is locked: 'C:\\anime\\First Title\\one.mkv'"),
            recovery.normalize_failure_signature("worker", "worker_unknown", "database is locked: 'D:\\anime\\Other Title\\two.mkv'"),
        )

    def test_different_generic_causes_do_not_trip_but_three_same_causes_do(self):
        fixture = observation_tests.M2ProductionObservationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        config = fixture._config("generic-collision")
        checkpoint = fixture._checkpoint_sentinel(config)
        messages = [
            "materialized subtitle language conflicts with persisted strategy: strategy=TRANSLATE_JA_SUBTITLE detected=unknown",
            "materialized subtitle language conflicts with persisted strategy: strategy=TRANSLATE_JA_SUBTITLE detected=unknown",
            "database is locked",
        ]
        for index, detail in enumerate(messages):
            outcome = fixture._failed(stage="worker", error_code="worker_unknown")
            outcome["detail"] = detail
            result = observation.record_job_result(config, job_identity=f"attempt-{index}", gate_job_identity=f"obligation-{index}", outcome=outcome)
            self.assertFalse(result["circuit_breaker_tripped"])
        for index in (3, 4):
            outcome["detail"] = "database is locked"
            result = observation.record_job_result(config, job_identity=f"attempt-{index}", gate_job_identity=f"obligation-{index}", outcome=outcome)
        self.assertTrue(result["circuit_breaker_tripped"])
        fixture._assert_tripped(config, "repeated_identical_stage_failure", checkpoint)
        breaker = json.loads(observation.circuit_breaker_state_path(config).read_text())
        evidence = breaker["reasons"][-1]["evidence"]
        self.assertEqual(len(evidence["identical_failure_job_ids"]), 3)
        self.assertEqual(evidence["normalized_failure_signature"], recovery.normalize_failure_signature("worker", "worker_unknown", "database is locked"))
        self.assertEqual(evidence["gate_id"], runtime.load_runtime_state(config)["gate"]["gate_id"])

    def test_retrip_keeps_old_reason_and_current_event_evidence(self):
        fixture = observation_tests.M2ProductionObservationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        config = fixture._config("retripped")
        with mock.patch.object(observation.time, "time", return_value=100.0):
            observation.trip_circuit_breaker(config, "repeated_identical_stage_failure", evidence={"job_key": "first"})
        with mock.patch.object(observation.time, "time", return_value=200.0):
            result = observation.trip_circuit_breaker(config, "repeated_identical_stage_failure", evidence={"job_key": "second"})
        self.assertEqual([item["observed_at"] for item in result["reasons"]], [100.0, 200.0])
        self.assertEqual(result["latest_trip"]["observed_at"], 200.0)


class CollisionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_tests.M2ControlledRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.state = ScanStateStore(Path(self.config.scanner_state_path))
        self.addCleanup(self.state.close)
        gate_time = time.time() - 3
        gate = runtime.initialize_gate(self.config, self.fixture._evidence("1" * 40, gate_time), source_revision_file=self.fixture.revision, now=gate_time)
        self.members = []
        self.source_digests = {}
        messages = ["materialized subtitle language conflicts with persisted strategy: strategy=TRANSLATE_JA_SUBTITLE detected=unknown"] * 2 + ["database is locked"]
        for index, detail in enumerate(messages):
            media = self.fixture.root / f"incident-{index}.mkv"
            media.write_bytes(f"immutable-source-{index}".encode())
            self.source_digests[media] = hashlib.sha256(media.read_bytes()).hexdigest()
            stat = media.stat()
            self.state.upsert_ai_queue_candidate(media, stat.st_mtime_ns)
            self.state.mark_ai_queue_running(media)
            self.state.mark_ai_queue_failed(media, detail, max_attempts=3, error_code="worker_unknown", retry_strategy="bounded_retry")
            obligation = self.state.ensure_ai_delivery_obligation(media, media_size=stat.st_size, media_mtime_ns=stat.st_mtime_ns, policy_revision="collision-test")
            attempt = self.state.begin_ai_delivery_attempt(obligation["obligation_id"])
            self.state.finish_ai_delivery_attempt(attempt["attempt_id"], status="retryable_failure", stage="worker", error_code="worker_unknown", detail=detail)
            recovery_tests.M2ProductionRecoveryTests._pipeline(
                SimpleNamespace(connection=self.state.observation_connection), media,
                state="RETRYING", stage="SUBTITLE_DETECTION", error_code="worker_unknown",
                resume_state="SUBTITLE_DETECTION", checkpoint={"completed_chunks": [1, 2]},
            )
            self.state.commit()
            # Reproduce only the old signature/classifier, not the new safety checks.
            with mock.patch.object(observation, "_outcome_failure_signature", return_value="worker:worker_unknown"), mock.patch.object(recovery, "breaker_streak_eligible", return_value=True):
                observation.record_job_result(self.config, job_identity=attempt["attempt_id"], gate_job_identity=obligation["obligation_id"], outcome={"terminal_status": "RETRYING", "stage": "worker", "error_code": "worker_unknown", "detail": detail})
            event = self.state.observation_connection.execute("SELECT event_sha256 FROM m2_observation_result_events WHERE claim_identity_hash=?", (hashlib.sha256(attempt["attempt_id"].encode()).hexdigest(),)).fetchone()
            self.members.append({"attempt_id": attempt["attempt_id"], "event_sha256": event[0], "detail_sha256": hashlib.sha256(detail.encode()).hexdigest()})
        self.breaker_path = observation.circuit_breaker_state_path(self.config)
        breaker = json.loads(self.breaker_path.read_text())
        # Model the deployed old latch: historical tripped_at/reasons retained,
        # while updated_at is the new collision. Recovery must ignore the old time.
        breaker["tripped_at"] = gate_time - 3600
        breaker["reasons"] = [{"reason_code": "repeated_identical_stage_failure", "observed_at": gate_time - 3600, "evidence": {"error_code": "source_selection_needs_review"}}]
        breaker.pop("latest_trip", None)
        self.breaker_path.write_text(json.dumps(breaker))
        self.now = time.time() + 10
        self.evidence = self.fixture._evidence("2" * 40, self.now)
        log = self.fixture.logs / "isolated-regression.log"
        log.write_text("server isolated regressions passed\n")
        self.evidence["root_cause"] = {
            "mode": "generic_failure_signature_collision",
            "breaker_reason": "repeated_identical_stage_failure",
            "affected_stage": "worker", "failure_code": "worker_unknown",
            "expected_old_gate_id": gate["gate"]["gate_id"],
            "expected_breaker_updated_at": breaker["updated_at"],
            "expected_counter_updated_at": self.state.observation_connection.execute("SELECT updated_at FROM m2_observation_meta WHERE key='identical_failure_job_ids'").fetchone()[0],
            "members": self.members,
            "regression_results": {
                "contract": "m2-generic-failure-collision-regression-v1",
                "worker_source_revision": "a" * 64,
                "production_resources_affected": False,
                "mixed_causes_separated": True, "same_cause_distinct_jobs_trips": True,
                "scanner_second_writer_during_inventory_io": True,
                "full_log_path": str(log), "full_log_sha256": "sha256:" + hashlib.sha256(log.read_bytes()).hexdigest(),
            },
        }
        self.before = runtime._durable_recovery_snapshot(self.state.observation_connection)
        self.queue_before = self.state.observation_connection.execute("SELECT * FROM ai_candidate_queue ORDER BY path").fetchall()
        self.checkpoints_before = self.state.observation_connection.execute("SELECT * FROM pipeline_stage_attempts ORDER BY stage_attempt_id").fetchall()

    def recover(self, evidence=None):
        return runtime.recover_runtime_local(self.config, evidence or self.evidence, source_revision_file=self.fixture.revision, now=self.now)

    def assert_refused(self, code):
        with self.assertRaisesRegex(runtime.RuntimeContractError, code):
            self.recover()
        self.assertTrue(json.loads(self.breaker_path.read_text())["tripped"])
        self.assertEqual(runtime._durable_recovery_snapshot(self.state.observation_connection), self.before)

    def test_exact_collision_recovers_without_historical_reconcile_and_preserves_checkpoints(self):
        with mock.patch.object(recovery, "reconcile_historical_jobs", side_effect=AssertionError("must not scan history")):
            result = self.recover()
        self.assertEqual(result["status"], "DISARMED")
        self.assertEqual(result["reconciliation"]["job_states_changed"], 0)
        self.assertEqual(self.state.observation_connection.execute("SELECT * FROM ai_candidate_queue ORDER BY path").fetchall(), self.queue_before)
        self.assertEqual(self.state.observation_connection.execute("SELECT * FROM pipeline_stage_attempts ORDER BY stage_attempt_id").fetchall(), self.checkpoints_before)
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.source_digests}, self.source_digests)
        report = json.loads(Path(result["log_path"]).read_text())
        self.assertEqual(sorted(report["incident"]["normalized_failure_clusters"].values()), [1, 2])
        self.assertTrue(report["remaining_language_failures_unresolved"])
        breaker = json.loads(self.breaker_path.read_text())
        self.assertEqual(len(breaker["reasons"]), 2)
        self.assertEqual(breaker["latest_trip"]["observed_at"], self.evidence["root_cause"]["expected_breaker_updated_at"])
        resumed = self.recover()
        self.assertEqual(resumed["recovery_record_id"], result["recovery_record_id"])

    def test_missing_member_refused(self):
        self.members.pop()
        self.assert_refused("collision_member_evidence_incomplete")

    def test_changed_counter_refused(self):
        self.state.observation_connection.execute("UPDATE m2_observation_meta SET value='2' WHERE key='identical_failure_streak'")
        self.state.commit()
        self.assert_refused("collision_current_counters_changed")

    def test_stale_counter_timestamp_refused(self):
        self.evidence["root_cause"]["expected_counter_updated_at"] -= 1
        self.assert_refused("collision_current_counters_changed")

    def test_old_runtime_refused(self):
        self.evidence["worker_commit_sha"] = "1" * 40
        self.assert_refused("recovery_requires_new_worker_runtime")

    def test_unloaded_signature_fix_refused(self):
        with mock.patch.object(observation, "_outcome_failure_signature", return_value="worker:worker_unknown"):
            self.assert_refused("collision_signature_fix_not_loaded")

    def test_false_same_cause_collision_refused(self):
        for member in self.members:
            self.state.observation_connection.execute("UPDATE ai_delivery_attempts SET detail='database is locked' WHERE attempt_id=?", (member["attempt_id"],))
            member["detail_sha256"] = hashlib.sha256(b"database is locked").hexdigest()
        self.state.commit()
        self.assert_refused("collision_not_proven")

    def test_tampered_event_reference_refused(self):
        self.members[0]["event_sha256"] = "0" * 64
        self.assert_refused("collision_member_binding_mismatch")

    def test_old_trip_timestamp_refused(self):
        self.evidence["root_cause"]["expected_breaker_updated_at"] -= 3600
        self.assert_refused("collision_latest_trip_mismatch")

    def test_missing_second_writer_regression_refused(self):
        self.evidence["root_cause"]["regression_results"]["scanner_second_writer_during_inventory_io"] = False
        self.assert_refused("collision_regression_evidence_invalid")

    def test_stale_fault_injection_refused(self):
        self.evidence["fault_results"]["container_started_at_epoch"] = self.evidence["root_cause"]["expected_breaker_updated_at"] - 1
        self.assert_refused("collision_fault_evidence_not_fresh")

    def test_fresh_work_refused(self):
        self.state.observation_connection.execute("UPDATE pipeline_stage_attempts SET status='RUNNING',heartbeat_at=? WHERE stage_attempt_id=(SELECT stage_attempt_id FROM pipeline_stage_attempts LIMIT 1)", (self.now,))
        self.state.commit()
        self.assert_refused("running_work_has_not_reached_safe_boundary")


if __name__ == "__main__":
    unittest.main()
