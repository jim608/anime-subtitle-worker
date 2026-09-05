from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
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


class DeploymentHandoffRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = CollisionRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.root_cause = f.evidence["root_cause"]
        origin = self.root_cause["expected_breaker_updated_at"]
        self.first_receipt = {
            "worker_sha": "3" * 40, "created_at": origin + 3,
            "identity": {"container_id": "e" * 64, "image_id": "sha256:" + "f" * 64,
                         "source_revision": "b" * 64,
                         "started_at": datetime.fromtimestamp(origin + 1, timezone.utc).isoformat()},
            "root_cause_evidence": json.loads(json.dumps(self.root_cause)),
        }
        self.receipt_path = f.fixture.logs / "first-attestation-input.json"
        self.receipt_path.write_text(json.dumps(self.first_receipt))
        self.latest = {"reason_code": "runtime_change", "observed_at": origin + 2.02,
                       "evidence": {"stage": "runtime_validation", "error_code": "live_worker_container_identity_mismatch"}}
        breaker = json.loads(f.breaker_path.read_text())
        breaker.update({"latest_trip": self.latest, "updated_at": self.latest["observed_at"],
                        "reasons": [*breaker["reasons"], self.latest]})
        f.breaker_path.write_text(json.dumps(breaker))
        from m2_observation_store import invalidate_active_gate, latest_gate, INVALIDATED_RUNTIME
        gate = latest_gate(f.state.observation_connection)
        evidence = {
            "reason_code": "live_worker_container_identity_mismatch",
            "expected": {"baseline_version": gate["baseline_version"], "worker_sha": gate["worker_sha"],
                         "container_image_id": gate["container_image_id"], "worker_container_id": gate["worker_container_id"],
                         "configuration_fingerprint": gate["configuration_fingerprint"], "decision_schema_version": gate["decision_schema_version"]},
            "actual": {"reason_code": "live_worker_container_identity_mismatch", "runtime_status": "DEGRADED",
                       "container_identity": "e" * 12, "worker_source_revision": "b" * 64,
                       "configuration_fingerprint": f.evidence["configuration_fingerprint"],
                       "decision_schema_version": f.evidence["decision"]["schema_version"],
                       "decision_version": f.evidence["decision"]["version"]},
        }
        invalidate_active_gate(f.state.observation_connection, INVALIDATED_RUNTIME, evidence=evidence, now=origin + 2)
        f.state.commit()
        gate = latest_gate(f.state.observation_connection)
        self.root_cause["expected_deployment_handoff"] = {
            "first_attestation_path": str(self.receipt_path),
            "first_attestation_sha256": "sha256:" + hashlib.sha256(self.receipt_path.read_bytes()).hexdigest(),
            "expected_breaker_updated_at": self.latest["observed_at"],
            "latest_trip_sha256": "sha256:" + hashlib.sha256(json.dumps(self.latest, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "invalidation_evidence_sha256": "sha256:" + hashlib.sha256(gate["invalidation_evidence_json"].encode()).hexdigest(),
        }

    def test_attested_drift_before_final_container_start_preserves_original_incident(self):
        f = self.fixture
        self.assertLess(self.latest["observed_at"], f.evidence["fault_results"]["container_started_at_epoch"])
        result = f.recover()
        report = json.loads(Path(result["log_path"]).read_text())
        self.assertEqual(report["incident"]["trip_observed_at_epoch"], self.root_cause["expected_breaker_updated_at"])
        self.assertEqual(report["incident"]["deployment_handoff"]["latest_trip_observed_at_epoch"], self.latest["observed_at"])
        self.assertEqual(json.loads(f.breaker_path.read_text())["latest_trip"], self.latest)
        self.assertEqual(f.state.observation_connection.execute("SELECT * FROM ai_candidate_queue ORDER BY path").fetchall(), f.queue_before)
        self.assertEqual(f.state.observation_connection.execute("SELECT * FROM pipeline_stage_attempts ORDER BY stage_attempt_id").fetchall(), f.checkpoints_before)

    def test_missing_handoff_receipt_refuses_newer_trip(self):
        self.root_cause.pop("expected_deployment_handoff")
        self.fixture.assert_refused("collision_latest_trip_mismatch")

    def test_unknown_later_incident_cannot_be_hidden_behind_expected_deployment(self):
        f = self.fixture
        breaker = json.loads(f.breaker_path.read_text())
        breaker["reasons"].append({"reason_code": "source_mutation", "observed_at": self.latest["observed_at"] - 0.01, "evidence": {}})
        f.breaker_path.write_text(json.dumps(breaker))
        f.assert_refused("handoff_unexpected_later_incident")

    def test_wrong_recorded_first_container_refused_even_with_matching_file_hash(self):
        self.first_receipt["identity"]["container_id"] = "a" * 64
        self.receipt_path.write_text(json.dumps(self.first_receipt))
        self.root_cause["expected_deployment_handoff"]["first_attestation_sha256"] = "sha256:" + hashlib.sha256(self.receipt_path.read_bytes()).hexdigest()
        self.fixture.assert_refused("handoff_recorded_runtime_mismatch")

    def test_unexpected_runtime_change_reason_refused(self):
        f = self.fixture
        breaker = json.loads(f.breaker_path.read_text())
        breaker["latest_trip"]["evidence"]["error_code"] = "live_configuration_fingerprint_mismatch"
        f.breaker_path.write_text(json.dumps(breaker))
        self.root_cause["expected_deployment_handoff"]["latest_trip_sha256"] = "sha256:" + hashlib.sha256(json.dumps(breaker["latest_trip"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        f.assert_refused("handoff_unexpected_runtime_change")

    def test_changed_counters_still_refused_after_verified_handoff(self):
        f = self.fixture
        f.state.observation_connection.execute("UPDATE m2_observation_meta SET value='4' WHERE key='identical_failure_streak'")
        f.state.commit()
        f.assert_refused("collision_current_counters_changed")

    def _noncohort_terminal_attempt(self):
        f = self.fixture
        media = f.fixture.root / "not-enrolled-in-gate.mkv"
        media.write_bytes(b"new unrelated source")
        stat = media.stat()
        obligation = f.state.ensure_ai_delivery_obligation(media, media_size=stat.st_size,
            media_mtime_ns=stat.st_mtime_ns, policy_revision="noncohort-test")
        attempt = f.state.begin_ai_delivery_attempt(obligation["obligation_id"])
        # Terminal now: this must still be rejected even though the separate
        # existing fresh-running safeguard no longer sees it.
        f.state.finish_ai_delivery_attempt(attempt["attempt_id"], status="review_required",
            stage="translation", error_code="quality_blocked", detail="unrelated review")
        f.state.commit()
        return attempt["attempt_id"]

    def test_noncohort_new_claim_also_refuses_handoff(self):
        self._noncohort_terminal_attempt()
        self.fixture.assert_refused("handoff_new_work_observed")

    def test_standalone_noncohort_terminal_without_observation_event_refused(self):
        attempt_id = self._noncohort_terminal_attempt()
        f = self.fixture
        f.state.observation_connection.execute("UPDATE ai_delivery_attempts SET started_at=? WHERE attempt_id=?",
            (self.root_cause["expected_breaker_updated_at"] - 1, attempt_id))
        f.state.commit()
        f.assert_refused("handoff_new_work_observed")


if __name__ == "__main__":
    unittest.main()
