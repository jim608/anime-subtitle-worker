from __future__ import annotations

import hashlib
import time
import unittest
from unittest import mock

import m2_guardrail_runtime as runtime
import m2_production_recovery as recovery
import test_m2_production_recovery as fixtures
import test_m2_production_observation as observation_fixtures
from scan_state import ScanStateStore


class RecoveryDispatchDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.M2ProductionRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.connection = self.fixture.connection
        self.now = time.time() + 1000

    def prepare(self, *names):
        media = [self.fixture._media(name + ".mkv") for name in names]
        for path in media:
            self.fixture._queue(path)
        self.fixture._reconcile()
        for priority, path in enumerate(media):
            self.connection.execute("UPDATE m2_recovery_jobs SET priority=? WHERE canonical_path=?", (priority, str(path)))
        return media

    def test_queue_retry_deadline_does_not_block_other_due_item_or_get_cleared(self):
        first, second = self.prepare("first-waits", "second-due")
        deadline = self.now + 3600
        self.connection.execute("UPDATE ai_candidate_queue SET next_retry_at=? WHERE path=?", (deadline, str(first)))
        before = self.connection.execute("SELECT * FROM ai_candidate_queue WHERE path=?", (str(first),)).fetchone()
        ledger_before = self.fixture._recovery_row(first)
        result = recovery.dispatch_next_recovery(self.connection, runtime_status="ARMED", now=self.now)
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["recovery_id"], self.fixture._recovery_row(second)["recovery_id"])
        self.assertEqual(self.connection.execute("SELECT * FROM ai_candidate_queue WHERE path=?", (str(first),)).fetchone(), before)
        self.assertEqual(self.fixture._recovery_row(first), ledger_before)

    def test_both_deadlines_survive_restart_and_empty_is_not_latched(self):
        (media,) = self.prepare("two-deadlines")
        self.connection.execute("UPDATE m2_recovery_jobs SET not_before=? WHERE canonical_path=?", (self.now + 300, str(media)))
        self.connection.execute("UPDATE ai_candidate_queue SET next_retry_at=? WHERE path=?", (self.now + 600, str(media)))
        before = self.fixture._recovery_row(media)
        for at in (self.now, self.now + 300, self.now + 599):
            result = recovery.dispatch_next_recovery(self.connection, runtime_status="ARMED", now=at)
            self.assertFalse(result["dispatched"])
            self.assertEqual(result["reason_code"], "recovery_backoff")
            self.assertEqual(result["eligible_at"], self.now + 600)
            self.assertEqual(recovery._meta(self.connection, "lane_state", ""), "CANARY_READY")
        self.assertEqual(self.fixture._recovery_row(media), before)
        self.connection.commit()
        reopened = ScanStateStore(self.fixture.root / "scanner.sqlite3")
        try:
            result = recovery.dispatch_next_recovery(reopened.observation_connection, runtime_status="ARMED", now=self.now + 600)
            self.assertTrue(result["dispatched"])
            self.assertTrue(result["canary"])
        finally:
            reopened.close()

    def test_exhausted_budget_never_dispatches_or_blocks_another_due_item(self):
        first, second = self.prepare("exhausted", "due")
        self.connection.execute("UPDATE m2_recovery_jobs SET recovery_attempt_count=retry_budget WHERE canonical_path=?", (str(first),))
        before = self.fixture._recovery_row(first)
        result = recovery.dispatch_next_recovery(self.connection, runtime_status="ARMED", now=self.now)
        self.assertEqual(result["recovery_id"], self.fixture._recovery_row(second)["recovery_id"])
        self.assertEqual(self.fixture._recovery_row(first), before)


class VerifiedQualityPauseTests(unittest.TestCase):
    def setUp(self):
        observer = observation_fixtures.M2ProductionObservationTests()
        observer.setUp()
        self.addCleanup(observer.doCleanups)
        self.addCleanup(observer.tearDown)
        self.config = observer._config("quality-pause")
        self.fixture = fixtures.M2ProductionRecoveryTests()
        self.fixture.root = self.config.input_path
        self.fixture.state = ScanStateStore(self.config.scanner_state_path)
        self.addCleanup(self.fixture.state.close)
        self.connection = self.fixture.connection = self.fixture.state.observation_connection
        first = self.fixture._media("quality.mkv")
        self.second = self.fixture._media("waiting.mkv")
        for path in (first, self.second):
            self.fixture._queue(path)
        self.fixture._reconcile(current_worker_version="1" * 40)
        self.connection.execute("UPDATE m2_recovery_jobs SET priority=0 WHERE canonical_path=?", (str(first),))
        self.now = time.time()
        dispatched = recovery.dispatch_next_recovery(self.connection, runtime_status="ARMED", now=self.now)
        attempt = self.fixture._claim_dispatched(first)
        detail = "Targeted subtitle readability repair exceeded its hard display limit at index 10: allowed=1"
        self.fixture.state.mark_ai_queue_failed(first, detail, max_attempts=1, error_code="translation_unknown", retry_strategy="bounded_retry")
        self.fixture._pipeline(first, state="NEEDS_REVIEW", stage="TRANSLATING", error_code="translation_unknown", resume_state="TRANSLATING", checkpoint={"schema_version": 2, "translated_chunks": [1, 2]})
        self.fixture.state.finish_ai_delivery_attempt(attempt, status="review_required", stage="translation", error_code="translation_unknown", detail=detail)
        with mock.patch.object(recovery, "classify_failure", return_value="PERMANENT_SYSTEM_ERROR"):
            recovery.settle_recovery_attempt(self.connection, first, attempt, now=self.now + 2)
        row = self.fixture._recovery_row(first)
        events = self.connection.execute("SELECT event_id,event_type,payload_json FROM m2_recovery_events WHERE recovery_id=?", (dispatched["recovery_id"],)).fetchall()
        dispatch = next(event for event in events if event[1] == "RECOVERY_DISPATCHED")
        settled = next(event for event in events if event[1] == "RECOVERY_FAILED")
        self.root_cause = {
            "expected_old_gate_id": runtime.load_runtime_state(self.config)["gate"]["gate_id"],
            "expected_breaker_updated_at": self.now + 5,
            "lane_quality_pause": {
                "recovery_id": row["recovery_id"], "claim_attempt_id": attempt,
                "dispatch_event_id": dispatch[0], "dispatch_payload_sha256": hashlib.sha256(dispatch[2].encode()).hexdigest(),
                "settlement_event_id": settled[0], "settlement_payload_sha256": hashlib.sha256(settled[2].encode()).hexdigest(),
                "checkpoint_sha256": row["checkpoint_sha256"],
            },
        }
        self.connection.execute("UPDATE ai_candidate_queue SET next_retry_at=? WHERE path=?", (self.now + 1000, str(self.second)))
        self.before = self.snapshot()

    def snapshot(self):
        return {table: self.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("m2_recovery_jobs", "ai_candidate_queue", "ai_delivery_attempts", "pipeline_stage_attempts")}

    def release(self, **kwargs):
        return runtime._recover_verified_quality_pause(self.connection, root_cause=self.root_cause,
            old_worker_sha="1" * 40, recovery_record_id="m2breakerrec_test", now=self.now + 10, **kwargs)

    def test_bound_quality_pause_release_preserves_failed_canary_and_all_deadlines(self):
        self.assertTrue(self.release(validate_only=True)["changed"])
        self.assertEqual(recovery._meta(self.connection, "lane_state", ""), "PAUSED")
        result = self.release()
        self.assertEqual(result["original_canary_status"], "FAILED")
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(recovery._meta(self.connection, "lane_state", ""), "CANARY_READY")
        waiting = recovery.dispatch_next_recovery(self.connection, runtime_status="ARMED", now=self.now + 400)
        self.assertEqual(waiting["reason_code"], "recovery_backoff")
        self.assertEqual(self.snapshot(), self.before)
        next_item = recovery.dispatch_next_recovery(self.connection, runtime_status="ARMED", now=self.now + 1000)
        self.assertTrue(next_item["dispatched"])
        self.assertNotEqual(next_item["recovery_id"], self.root_cause["lane_quality_pause"]["recovery_id"])

    def test_corrupted_settlement_evidence_refused(self):
        self.root_cause["lane_quality_pause"]["settlement_payload_sha256"] = "0" * 64
        with self.assertRaisesRegex(runtime.RuntimeContractError, "quality_pause_event_binding_mismatch"):
            self.release()
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(recovery._meta(self.connection, "lane_state", ""), "PAUSED")

    def test_later_canary_or_changed_checkpoint_refused(self):
        recovery._set_meta(self.connection, "last_dispatch_at", repr(self.now + 1), self.now + 1)
        with self.assertRaisesRegex(runtime.RuntimeContractError, "quality_pause_cause_not_proven"):
            self.release()
        recovery._set_meta(self.connection, "last_dispatch_at", repr(self.now), self.now + 1)
        self.root_cause["lane_quality_pause"]["checkpoint_sha256"] = "0" * 64
        with self.assertRaisesRegex(runtime.RuntimeContractError, "quality_pause_checkpoint_changed"):
            self.release()
        self.assertEqual(self.snapshot(), self.before)

    def test_later_lane_event_cannot_be_hidden_by_old_receipt(self):
        recovery._record_event(self.connection, event_key="newer-review-evidence",
            recovery_id=self.root_cause["lane_quality_pause"]["recovery_id"],
            run_id=recovery._meta(self.connection, "last_run_id", ""),
            event_type="RECOVERY_REVIEW_UPDATED", payload={"reason": "new_evidence"},
            now=self.now + 3)
        with self.assertRaisesRegex(runtime.RuntimeContractError, "quality_pause_cause_not_proven"):
            self.release()
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(recovery._meta(self.connection, "lane_state", ""), "PAUSED")

    def test_unfixed_taxonomy_or_unproven_pause_cannot_be_released(self):
        with mock.patch.object(recovery, "classify_failure", return_value="PERMANENT_SYSTEM_ERROR"):
            with self.assertRaisesRegex(runtime.RuntimeContractError, "quality_pause_cause_not_proven"):
                self.release()
        self.root_cause.pop("lane_quality_pause")
        with self.assertRaisesRegex(runtime.RuntimeContractError, "quality_pause_evidence_missing"):
            self.release()
        self.assertEqual(self.snapshot(), self.before)

    def test_future_readability_refusal_is_quality_not_systemic(self):
        detail = "Targeted subtitle readability repair exceeded its hard display limit at index 10: allowed=1"
        self.assertEqual(recovery.classify_failure("translation", "translation_unknown", detail), "QUALITY_BLOCKED")
        self.assertNotEqual(recovery.classify_failure("translation", "translation_unknown", "unexpected adapter exception"), "QUALITY_BLOCKED")


if __name__ == "__main__":
    unittest.main()
