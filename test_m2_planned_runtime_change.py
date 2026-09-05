from __future__ import annotations

import json
from pathlib import Path
import time
import unittest
from unittest import mock

import m2_guardrail_runtime as runtime
import m2_production_observation as observation
from m2_observation_store import active_gate, enroll_claim, gate_by_id
from scan_state import ScanStateStore
import test_m2_guardrail_runtime as runtime_tests


class PlannedRuntimeChangeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_tests.M2ControlledRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        self.state = ScanStateStore(Path(self.config.scanner_state_path))
        self.addCleanup(self.state.close)
        self.connection = self.state.observation_connection
        self.now = time.time()
        self.old = runtime.initialize_gate(
            self.config, self.fixture._evidence("1" * 40, self.now - 20),
            source_revision_file=self.fixture.revision, now=self.now - 20,
        )
        self.old_gate = self.old["gate"]["gate_id"]
        enroll_claim(self.connection, self.old, claim_identity="frozen-attempt",
                     gate_job_identity="frozen-job", input_fingerprint="source",
                     claimed_at=self.now - 10, processing_strategy="ASR_JA_AUDIO",
                     eligible=True, eligibility_reason="eligible", now=self.now - 10)
        self.state.commit()
        self.media = self.fixture.root / "preserved.mkv"
        self.media.write_bytes(b"immutable-video")
        self.output = self.fixture.output / "preserved.ass"
        self.output.write_bytes(b"immutable-formal-output")
        self.state.upsert_ai_queue_candidate(self.media, self.media.stat().st_mtime_ns)
        self.state.commit()
        self.before_files = {p: p.read_bytes() for p in (self.media, self.output)}

    def prepare(self, **overrides):
        args = dict(expected_old_gate_id=self.old_gate, expected_new_worker_sha="2" * 40,
                    receipt_id="parser-change-test", source_revision_file=self.fixture.revision,
                    now=self.now)
        args.update(overrides)
        return runtime.prepare_runtime_change(self.config, **args)

    def deploy(self):
        self.prepared = self.prepare()
        self.fixture.identity.write_text("f" * 12 + "\n")
        self.fixture.revision.write_text("b" * 64 + "\n")
        (self.fixture.runtime_app / "worker.py").write_text("VALUE = 3\n")
        with mock.patch.object(observation.time, "time", return_value=self.now + 4):
            self.assertFalse(observation.admit_new_job(self.config))
        self.evidence = self.fixture._evidence("2" * 40, self.now + 5)
        self.evidence.update({
            "worker_source_revision": "b" * 64,
            "worker_container_id": "f" * 64, "worker_container_identity": "f" * 12,
            "worker_image_id": "sha256:" + "e" * 64,
        })
        self.evidence["fault_results"]["worker_source_revision"] = "b" * 64
        self.evidence["root_cause"] = {
            "mode": "planned_runtime_change", "breaker_reason": "runtime_change",
            "affected_stage": "runtime_validation",
            "failure_code": "live_worker_container_identity_mismatch",
            "expected_old_gate_id": self.old_gate,
            "planned_change_receipt": self.prepared["receipt_path"],
            "planned_change_receipt_sha256": self.prepared["receipt_sha256"],
        }

    def recover(self):
        return runtime.recover_runtime_local(
            self.config, self.evidence, source_revision_file=self.fixture.revision,
            now=self.now + 10,
        )

    def assert_refused(self, reason):
        before = observation.circuit_breaker_state_path(self.config).read_bytes()
        with self.assertRaisesRegex(runtime.RuntimeContractError, reason):
            self.recover()
        self.assertEqual(before, observation.circuit_breaker_state_path(self.config).read_bytes())
        self.assertEqual(self.before_files, {p: p.read_bytes() for p in self.before_files})

    def test_prepare_is_immutable_and_idempotent(self):
        first = self.prepare()
        before = Path(first["receipt_path"]).read_bytes()
        self.assertEqual(first, self.prepare(now=self.now + 1))
        self.assertEqual(before, Path(first["receipt_path"]).read_bytes())
        with self.assertRaisesRegex(runtime.RuntimeContractError, "receipt_conflict"):
            self.prepare(expected_new_worker_sha="3" * 40)
        self.assertEqual(before, Path(first["receipt_path"]).read_bytes())

    def test_prepare_refuses_wrong_gate_and_same_sha(self):
        for overrides in ({"expected_old_gate_id": "wrong"}, {"expected_new_worker_sha": "1" * 40}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(runtime.RuntimeContractError, "gate_or_new_sha_invalid"):
                self.prepare(**overrides)

    def test_prepare_requires_existing_durable_pause(self):
        (self.fixture.work / "ai_control.json").unlink()
        with self.assertRaisesRegex(observation.ObservationStateError, "ai_claim_pause_missing"):
            self.prepare()

    def test_prepare_refuses_running_work_without_stopping_it(self):
        self.state.mark_ai_queue_running(self.media)
        self.state.commit()
        with self.assertRaisesRegex(runtime.RuntimeContractError, "work_not_idle"):
            self.prepare()
        self.assertEqual("running", self.connection.execute(
            "SELECT status FROM ai_candidate_queue WHERE path=?", (str(self.media),)
        ).fetchone()[0])

    def test_prepare_refuses_tripped_runtime(self):
        observation.trip_circuit_breaker(self.config, "source_mutation", evidence={"test": True})
        with self.assertRaisesRegex(runtime.RuntimeContractError, "runtime_not_armed"):
            self.prepare()

    def old_delivery_attempt(self, updated_at):
        obligation = self.state.ensure_ai_delivery_obligation(
            self.media, media_size=self.media.stat().st_size,
            media_mtime_ns=self.media.stat().st_mtime_ns, policy_revision="planned-change-test",
        )
        attempt = self.state.begin_ai_delivery_attempt(obligation["obligation_id"])
        self.connection.execute(
            "UPDATE ai_delivery_attempts SET started_at=?,created_at=?,updated_at=? WHERE attempt_id=?",
            (updated_at, updated_at, updated_at, attempt["attempt_id"]),
        )
        self.state.commit()
        return attempt["attempt_id"]

    def test_known_stale_attempt_is_preserved_and_excluded_from_new_gate(self):
        attempt_id = self.old_delivery_attempt(self.now - 86400)
        before = tuple(self.connection.execute(
            "SELECT * FROM ai_delivery_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone())
        self.deploy()
        self.recover()
        new = runtime.initialize_gate(self.config, self.evidence,
                                      source_revision_file=self.fixture.revision, now=self.now + 11)
        self.assertEqual(1, new["pre_gate_running"]["attempt_count"])
        self.assertEqual(before, tuple(self.connection.execute(
            "SELECT * FROM ai_delivery_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()))

    def test_fresh_or_unknown_delivery_heartbeat_refuses_prepare(self):
        attempt_id = self.old_delivery_attempt(self.now - 86400)
        for timestamp in (self.now, float("inf"), 0, float("-inf")):
            with self.subTest(timestamp=timestamp):
                self.connection.execute("UPDATE ai_delivery_attempts SET updated_at=? WHERE attempt_id=?", (timestamp, attempt_id))
                self.state.commit()
                with self.assertRaisesRegex(runtime.RuntimeContractError, "work_not_idle"):
                    self.prepare()

    def test_planned_recovery_preserves_cohort_and_retry_does_not_rebuild_gate(self):
        self.deploy()
        result = self.recover()
        self.assertEqual("DISARMED", result["status"])
        self.assertEqual("INVALIDATED_BY_RUNTIME_CHANGE", gate_by_id(self.connection, self.old_gate)["status"])
        self.assertEqual(1, self.connection.execute(
            "SELECT COUNT(*) FROM m2_observation_gate_jobs WHERE gate_id=?", (self.old_gate,)
        ).fetchone()[0])
        self.assertEqual(0, result["reconciliation"]["requeued"])
        log = json.loads(Path(result["log_path"]).read_text())
        self.assertEqual("planned_runtime_change", log["recovery_mode"])
        self.assertIsNone(log["source_identity_preserved"])
        self.assertFalse(observation.circuit_breaker_active(self.config))
        # A retry after clearing the latch but before arm preserves its record.
        self.assertEqual(result["recovery_record_id"], self.recover()["recovery_record_id"])
        new = runtime.initialize_gate(self.config, self.evidence,
                                      source_revision_file=self.fixture.revision, now=self.now + 11)
        new_gate = new["gate"]["gate_id"]
        self.assertNotEqual(self.old_gate, new_gate)
        before_new_gate = active_gate(self.connection)
        self.assertEqual(0, self.connection.execute(
            "SELECT COUNT(*) FROM m2_observation_gate_jobs WHERE gate_id=?", (new_gate,)
        ).fetchone()[0])
        runtime.resume_claims_local(self.config, source_revision_file=self.fixture.revision)
        self.assertEqual(result["recovery_record_id"], self.recover()["recovery_record_id"])
        repeated = runtime.initialize_gate(self.config, self.evidence,
                                           source_revision_file=self.fixture.revision, now=self.now + 12)
        self.assertEqual(new_gate, repeated["gate"]["gate_id"])
        self.assertEqual(new["gate_start_at"], repeated["gate_start_at"])
        self.assertEqual(before_new_gate, active_gate(self.connection))
        receipt = json.loads(Path(self.prepared["receipt_path"]).read_text())
        self.assertEqual(receipt["snapshot"]["members_sha256"], runtime._planned_change_snapshot(
            self.connection, self.old_gate)["members_sha256"])
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM m2_observation_gates").fetchone()[0])
        self.assertEqual(self.before_files, {p: p.read_bytes() for p in self.before_files})

    def test_completed_change_cannot_be_replayed_with_another_receipt(self):
        other = self.prepare(receipt_id="another-prepared-receipt")
        self.deploy()
        self.recover()
        runtime.initialize_gate(self.config, self.evidence,
                                source_revision_file=self.fixture.revision, now=self.now + 11)
        gate_before = active_gate(self.connection)
        self.evidence["root_cause"].update({
            "planned_change_receipt": other["receipt_path"],
            "planned_change_receipt_sha256": other["receipt_sha256"],
        })
        self.assert_refused("pending_planned_change_evidence_mismatch")
        self.assertEqual(gate_before, active_gate(self.connection))

    def test_refuses_missing_or_changed_receipt(self):
        self.deploy()
        self.evidence["root_cause"]["planned_change_receipt_sha256"] = "sha256:" + "0" * 64
        self.assert_refused("planned_change_receipt_invalid")

    def test_refuses_wrong_gate(self):
        self.deploy()
        self.evidence["root_cause"]["expected_old_gate_id"] = "wrong"
        self.assert_refused("prior_gate_identity_mismatch")

    def test_refuses_same_sha_or_unplanned_sha(self):
        self.deploy()
        self.evidence["worker_commit_sha"] = "3" * 40
        self.assert_refused("planned_change_new_sha_mismatch")

    def test_refuses_configuration_drift(self):
        self.deploy()
        self.config.m2_recovery_retry_budget += 1
        self.evidence["configuration_fingerprint"] = runtime.configuration_fingerprint(self.config)
        self.assert_refused("planned_change_frozen_policy_changed")

    def test_refuses_old_fault_evidence(self):
        self.deploy()
        self.evidence["fault_results"]["container_started_at_epoch"] = self.now - 1
        self.assert_refused("planned_change_fault_evidence_not_fresh")

    def test_refuses_other_breaker(self):
        self.deploy()
        with mock.patch.object(observation.time, "time", return_value=self.now + 6):
            observation.trip_circuit_breaker(self.config, "source_mutation", evidence={"test": True})
        self.assert_refused("planned_change_unexpected_breaker")

    def test_refuses_new_claim(self):
        self.deploy()
        self.state.mark_ai_queue_running(self.media)
        self.state.commit()
        self.assert_refused("work_not_idle")

    def test_refuses_new_stage_event(self):
        self.deploy()
        self.connection.execute(
            "INSERT INTO ai_stage_events(path,stage,status,message,created_at) VALUES(?,?,?,?,?)",
            (str(self.media), "transcription", "failed", "new result", self.now + 6),
        )
        self.state.commit()
        self.assert_refused("new_work_or_evidence_changed")

    def test_new_trip_during_archive_is_never_cleared(self):
        self.assert_overlap_trip_preserved(".tripped-", "breaker_changed_before_clear")

    def test_new_trip_before_archive_is_never_cleared(self):
        self.assert_overlap_trip_preserved("runtime.json", "breaker_changed_before_archive")

    def assert_overlap_trip_preserved(self, trigger, reason):
        self.deploy()
        original_write = runtime.atomic_write_text

        def interleaved_write(path, value):
            original_write(path, value)
            if trigger in Path(path).name:
                with mock.patch.object(observation.time, "time", return_value=self.now + 9):
                    observation.trip_circuit_breaker(self.config, "source_mutation", evidence={"test": True})

        with mock.patch.object(runtime, "atomic_write_text", side_effect=interleaved_write):
            with self.assertRaisesRegex(runtime.RuntimeContractError, reason):
                self.recover()
        breaker = json.loads(observation.circuit_breaker_state_path(self.config).read_text())
        self.assertTrue(breaker["tripped"])
        self.assertEqual("source_mutation", breaker["latest_trip"]["reason_code"])

    def test_stale_attempt_evidence_change_without_new_timestamp_is_rejected(self):
        attempt_id = self.old_delivery_attempt(self.now - 86400)
        self.deploy()
        self.connection.execute("UPDATE ai_delivery_attempts SET detail='changed evidence' WHERE attempt_id=?", (attempt_id,))
        self.state.commit()
        self.assert_refused("new_work_or_evidence_changed")

    def test_refuses_update_to_existing_noncohort_result(self):
        self.connection.execute(
            "INSERT INTO m2_observation_supplemental(gate_id,job_id,claim_identity_hash,exclusion_reason,claimed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (self.old_gate, "supplemental-job", "supplemental-claim", "pre_gate", self.now - 5, self.now - 5, self.now - 5),
        )
        self.state.commit()
        self.deploy()
        self.connection.execute(
            "UPDATE m2_observation_supplemental SET last_state='FAILED',updated_at=? WHERE job_id=?",
            (self.now + 6, "supplemental-job"),
        )
        self.state.commit()
        self.assert_refused("new_work_or_evidence_changed")

    def test_parser_supports_exact_planned_contract(self):
        args = runtime._parser().parse_args([
            "prepare-runtime-change", "--config", "config.yaml", "--expected-old-gate-id", "gate",
            "--expected-new-worker-sha", "2" * 40, "--receipt-id", "change-test",
        ])
        self.assertEqual("prepare-runtime-change", args.command)


if __name__ == "__main__":
    unittest.main()
