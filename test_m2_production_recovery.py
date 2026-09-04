from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from m2_production_recovery import (
    breaker_streak_eligible,
    classify_failure,
    dispatch_next_recovery,
    mark_recovery_claimed,
    reconcile_historical_jobs,
    recovery_status,
    settle_recovery_attempt,
)
from scan_state import ScanStateStore


CURRENT_WORKER = "b" * 40


class M2ProductionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = ScanStateStore(self.root / "scanner.sqlite3")
        self.addCleanup(self.state.close)
        self.connection = self.state.observation_connection

    def _media(self, name: str) -> Path:
        path = self.root / name
        path.write_bytes((name + "\n").encode("utf-8"))
        return path.resolve()

    def _queue(
        self,
        path: Path,
        *,
        status: str = "failed_retry",
        stage: str = "worker",
        error_code: str = "transient_timeout",
        message: str = "model timed out",
        source: str = "scan",
        retry_strategy: str = "bounded_retry",
        updated_at: float | None = None,
    ) -> None:
        stat = path.stat()
        observed_at = time.time() if updated_at is None else float(updated_at)
        self.connection.execute(
            """
            INSERT INTO ai_candidate_queue(
                path,mtime_ns,status,source,attempts,running_at,last_error,
                last_error_at,last_error_code,retry_strategy,failure_revision,
                next_retry_at,force_ai,added_at,updated_at
            ) VALUES(?,?,?,?,2,?,?,?,?,?,?,0,0,?,?)
            """,
            (
                str(path),
                stat.st_mtime_ns,
                status,
                source,
                observed_at if status == "running" else 0,
                message,
                observed_at,
                error_code,
                retry_strategy,
                "old-policy",
                observed_at,
                observed_at,
            ),
        )
        self.connection.execute(
            "INSERT INTO ai_job_state(path,stage,status,message,started_at,updated_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                str(path),
                stage,
                "running" if status == "running" else "failed",
                message,
                observed_at,
                observed_at,
                None if status == "running" else observed_at,
            ),
        )

    def _pipeline(
        self,
        path: Path,
        *,
        state: str,
        stage: str,
        error_code: str,
        resume_state: str,
        checkpoint: dict[str, object] | None = None,
        updated_at: float | None = None,
    ) -> str:
        stat = path.stat()
        now = time.time() if updated_at is None else float(updated_at)
        seed = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        job_id = "job_" + seed[:24]
        self.connection.execute(
            """
            INSERT INTO pipeline_jobs(
                job_id,canonical_path,media_revision,media_fingerprint,identity_kind,
                media_size,media_mtime_ns,state,state_version,active_stage_attempt_id,
                retry_count,next_retry_at,resume_state,terminal_reason_code,
                terminal_error_json,created_at,updated_at,completed_at
            ) VALUES(?,?,?,?,?,?,?,?,1,NULL,1,0,?,?,?,?,?,0)
            """,
            (
                job_id,
                str(path),
                "rev_" + seed,
                "fingerprint_" + seed,
                "path_stat",
                stat.st_size,
                stat.st_mtime_ns,
                state,
                resume_state,
                error_code,
                json.dumps({"message": error_code}, sort_keys=True, separators=(",", ":")),
                now - 10,
                now,
            ),
        )
        checkpoint_json = json.dumps(
            checkpoint or {}, sort_keys=True, separators=(",", ":")
        )
        checkpoint_sha = (
            hashlib.sha256(checkpoint_json.encode("utf-8")).hexdigest()
            if checkpoint
            else ""
        )
        attempt_id = "attempt_" + seed[:24]
        self.connection.execute(
            """
            INSERT INTO pipeline_stage_attempts(
                stage_attempt_id,job_id,stage,attempt_number,status,input_json,
                input_sha256,output_json,outputs_verified,model_json,retry_count,
                retry_limit,timeout_seconds,checkpoint_json,checkpoint_sha256,
                error_class,error_code,error_json,idempotency_key,started_at,
                heartbeat_at,finished_at,updated_at
            ) VALUES(?,?,?,1,'RETRYABLE_FAILURE','{}',?,'{}',0,'{}',0,2,0,?,?,
                     'transient',?,'{}',NULL,?,?,?,?)
            """,
            (
                attempt_id,
                job_id,
                stage,
                hashlib.sha256(b"{}").hexdigest(),
                checkpoint_json,
                checkpoint_sha,
                error_code,
                now - 10,
                now,
                now,
                now,
            ),
        )
        return job_id

    def _reconcile(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "current_worker_version": CURRENT_WORKER,
            "current_analyzer_version": "m2-source-analyzer-v1",
            "current_decision_schema_version": 1,
            "current_checkpoint_schema_version": 2,
            "retry_budget": 2,
            "stale_after_seconds": 900,
        }
        values.update(overrides)
        result = reconcile_historical_jobs(self.connection, **values)
        self.connection.commit()
        return result

    def _recovery_row(self, path: Path) -> dict[str, object]:
        cursor = self.connection.execute(
            "SELECT * FROM m2_recovery_jobs WHERE canonical_path=?", (str(path),)
        )
        columns = [item[0] for item in cursor.description or ()]
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        return dict(zip(columns, row, strict=True))

    def test_failure_taxonomy_is_closed_and_quality_is_not_breaker_eligible(self) -> None:
        cases = {
            "TRANSIENT": ("translation", "transient_timeout", "timed out"),
            "RESOURCE": ("asr", "transient_oom", "out of memory"),
            "BAD_INPUT": ("probe", "unsupported_media", "unsupported format"),
            "QUALITY_BLOCKED": (
                "source_selection_review",
                "source_selection_needs_review",
                "low confidence",
            ),
            "PERMANENT_SYSTEM_ERROR": ("worker", "worker_unknown", "fatal"),
        }
        for expected, inputs in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(classify_failure(*inputs), expected)
        self.assertFalse(
            breaker_streak_eligible(
                {
                    "terminal_status": "RETRYING",
                    "stage": "source_selection_review",
                    "error_code": "source_selection_needs_review",
                }
            )
        )

    def test_old_failed_known_fix_is_version_aware_and_ready(self) -> None:
        media = self._media("fixed.mkv")
        self._pipeline(
            media,
            state="FAILED",
            stage="TRANSLATING",
            error_code="old_translation_bug",
            resume_state="TRANSLATING",
        )
        result = self._reconcile(fixed_failure_codes={"old_translation_bug"})
        row = self._recovery_row(media)
        self.assertEqual(result["historical_failed_total"], 1)
        self.assertEqual(row["failure_category"], "CODE_VERSION_FIXED")
        self.assertEqual(row["version_disposition"], "RECOVERABLE_BY_NEW_RUNTIME")
        self.assertEqual(row["recovery_decision"], "REQUEUE_WITH_NEW_RUNTIME")
        self.assertEqual(row["status"], "READY")

    def test_valid_translation_checkpoint_resumes_without_asr(self) -> None:
        media = self._media("translation.mkv")
        self._queue(media, stage="translation")
        self._pipeline(
            media,
            state="RETRYING",
            stage="TRANSLATING",
            error_code="transient_timeout",
            resume_state="TRANSLATING",
            checkpoint={"schema_version": 3, "completed_batches": 42, "next_batch": 43},
        )
        self._reconcile()
        row = self._recovery_row(media)
        self.assertEqual(row["recovery_decision"], "RECOVER_FROM_CHECKPOINT")
        self.assertEqual(row["resume_stage"], "TRANSLATING")
        checkpoint = json.loads(str(row["checkpoint_json"]))
        self.assertEqual(checkpoint["next_batch"], 43)
        self.assertNotEqual(row["resume_stage"], "ASR")

    def test_qc_failure_resumes_only_qc(self) -> None:
        media = self._media("qc.mkv")
        self._queue(media, stage="quality_check", error_code="transient_timeout")
        self._pipeline(
            media,
            state="RETRYING",
            stage="QC",
            error_code="transient_timeout",
            resume_state="QC",
            checkpoint={"schema_version": 2, "translation_verified": True},
        )
        self._reconcile()
        row = self._recovery_row(media)
        self.assertEqual(row["resume_stage"], "QC")
        self.assertEqual(row["recovery_decision"], "RECOVER_FROM_CHECKPOINT")

    def test_incompatible_checkpoint_reprocesses_only_its_safe_stage(self) -> None:
        media = self._media("old-translation-checkpoint.mkv")
        self._queue(media, stage="translation")
        self._pipeline(
            media,
            state="RETRYING",
            stage="TRANSLATING",
            error_code="transient_timeout",
            resume_state="TRANSLATING",
            checkpoint={"schema_version": 2, "completed_batches": 42},
        )
        self._reconcile()
        row = self._recovery_row(media)
        self.assertEqual(row["checkpoint_available"], 1)
        self.assertEqual(row["checkpoint_compatible"], 0)
        self.assertEqual(row["recovery_decision"], "RETRY_STAGE")
        self.assertEqual(row["resume_stage"], "TRANSLATING")

    def test_stale_running_is_recovered_before_retrying(self) -> None:
        stale = self._media("stale.mkv")
        retrying = self._media("retrying.mkv")
        old = time.time() - 7200
        self._queue(stale, status="running", updated_at=old)
        self._queue(retrying)
        result = self._reconcile(stale_after_seconds=900)
        self.assertEqual(result["stale_running_total"], 1)
        stale_row = self._recovery_row(stale)
        self.assertLess(int(stale_row["priority"]), int(self._recovery_row(retrying)["priority"]))
        queue_status = self.connection.execute(
            "SELECT status FROM ai_candidate_queue WHERE path=?", (str(stale),)
        ).fetchone()[0]
        self.assertEqual(queue_status, "failed_retry")
        dispatched = dispatch_next_recovery(
            self.connection, runtime_status="ARMED", now=time.time() + 1000
        )
        self.assertEqual(dispatched["recovery_id"], stale_row["recovery_id"])

    def test_quality_review_is_normalized_without_requeue(self) -> None:
        media = self._media("ambiguous.mkv")
        self._queue(
            media,
            stage="source_selection_review",
            error_code="source_selection_needs_review",
            message="ambiguous source confidence",
            retry_strategy="manual_review",
        )
        self._reconcile()
        row = self._recovery_row(media)
        queue = self.connection.execute(
            "SELECT status,source FROM ai_candidate_queue WHERE path=?", (str(media),)
        ).fetchone()
        self.assertEqual(row["recovery_decision"], "KEEP_NEEDS_REVIEW")
        self.assertEqual(row["status"], "EXCLUDED")
        self.assertEqual(tuple(queue), ("paused", "m2_recovery_review"))

    def test_unsupported_input_is_quarantined_and_never_dispatched(self) -> None:
        media = self._media("unsupported.mkv")
        self._queue(
            media,
            error_code="unsupported_media",
            message="unsupported format",
            retry_strategy="permanent",
        )
        self._reconcile()
        row = self._recovery_row(media)
        self.assertIn(row["recovery_decision"], {"KEEP_QUARANTINED", "MARK_UNSUPPORTED"})
        self.assertEqual(row["status"], "EXCLUDED")
        self.assertFalse(dispatch_next_recovery(self.connection, runtime_status="ARMED")["dispatched"])

    def test_first_recovery_dispatch_is_exactly_one_canary_and_survives_restart(self) -> None:
        first = self._media("first.mkv")
        second = self._media("second.mkv")
        self._queue(first)
        self._queue(second)
        self._reconcile()
        dispatched = dispatch_next_recovery(
            self.connection, runtime_status="ARMED", now=time.time() + 1000
        )
        self.connection.commit()
        self.assertTrue(dispatched["dispatched"])
        self.assertTrue(dispatched["canary"])
        counts = dict(
            self.connection.execute(
                "SELECT status,COUNT(1) FROM m2_recovery_jobs GROUP BY status"
            ).fetchall()
        )
        self.assertEqual(counts.get("DISPATCHED"), 1)
        self.assertEqual(counts.get("READY"), 1)
        self.state.close()
        reopened = ScanStateStore(self.root / "scanner.sqlite3")
        try:
            self.assertEqual(
                recovery_status(reopened.observation_connection)["lane_state"],
                "CANARY_IN_FLIGHT",
            )
        finally:
            reopened.close()

    def _claim_dispatched(self, path: Path) -> str:
        stat = path.stat()
        obligation = self.state.ensure_ai_delivery_obligation(
            path,
            media_size=stat.st_size,
            media_mtime_ns=stat.st_mtime_ns,
            policy_revision="recovery-test",
        )
        attempt = self.state.begin_ai_delivery_attempt(str(obligation["obligation_id"]))
        mark_recovery_claimed(self.connection, path, str(attempt["attempt_id"]))
        return str(attempt["attempt_id"])

    def test_successful_canary_activates_lane_and_records_checkpoint_resume(self) -> None:
        media = self._media("success.mkv")
        self._queue(media)
        self._pipeline(
            media,
            state="RETRYING",
            stage="TRANSLATING",
            error_code="transient_timeout",
            resume_state="TRANSLATING",
            checkpoint={"schema_version": 3, "next_batch": 2},
        )
        self._reconcile()
        dispatch_next_recovery(self.connection, runtime_status="ARMED", now=time.time() + 1000)
        attempt_id = self._claim_dispatched(media)
        self.state.finish_ai_delivery_attempt(attempt_id, status="succeeded", stage="delivery")
        settled = settle_recovery_attempt(self.connection, media, attempt_id)
        self.assertEqual(settled["status"], "SUCCEEDED")
        status = recovery_status(self.connection)
        self.assertEqual(status["lane_state"], "ACTIVE")
        self.assertEqual(status["recovered_from_checkpoint"], 1)

    def test_same_signature_same_runtime_without_checkpoint_is_blocked(self) -> None:
        media = self._media("loop.mkv")
        self._queue(media)
        self._reconcile()
        base = time.time() + 1000
        dispatch_next_recovery(self.connection, runtime_status="ARMED", now=base)
        first = self._claim_dispatched(media)
        self.state.finish_ai_delivery_attempt(
            first,
            status="retryable_failure",
            stage="translation",
            error_code="transient_timeout",
            detail="timed out",
        )
        first_result = settle_recovery_attempt(self.connection, media, first, now=base + 1)
        self.assertEqual(first_result["status"], "READY")
        self.connection.execute(
            "UPDATE ai_candidate_queue SET status='failed_retry' WHERE path=?", (str(media),)
        )
        self.connection.execute(
            "UPDATE m2_recovery_meta SET value='ACTIVE' WHERE key='lane_state'"
        )
        dispatch_next_recovery(self.connection, runtime_status="ARMED", now=base + 1000)
        second = self._claim_dispatched(media)
        self.state.finish_ai_delivery_attempt(
            second,
            status="retryable_failure",
            stage="translation",
            error_code="transient_timeout",
            detail="timed out again",
        )
        second_result = settle_recovery_attempt(
            self.connection, media, second, now=base + 1001
        )
        self.assertEqual(second_result["status"], "BLOCKED_NO_PROGRESS")
        self.assertTrue(second_result["no_progress"])

    def test_reconciliation_does_not_touch_source_or_formal_output(self) -> None:
        media = self._media("safe-source.mkv")
        output = self.root / "formal-output.ass"
        output.write_text("formal-output-sentinel", encoding="utf-8")
        source_before = hashlib.sha256(media.read_bytes()).hexdigest()
        output_before = hashlib.sha256(output.read_bytes()).hexdigest()
        self._queue(media)
        self._reconcile()
        self.assertEqual(hashlib.sha256(media.read_bytes()).hexdigest(), source_before)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), output_before)

    def test_status_exposes_required_recovery_metrics(self) -> None:
        media = self._media("metrics.mkv")
        self._queue(media)
        self._reconcile()
        status = recovery_status(self.connection)
        required = {
            "historical_failed_total",
            "historical_retrying_total",
            "stale_running_total",
            "historical_quarantined_total",
            "historical_needs_review_total",
            "recoverable_by_new_runtime",
            "recovered_from_checkpoint",
            "requeued_for_stage_retry",
            "permanently_failed",
            "kept_needs_review",
            "kept_quarantined",
            "unsupported",
            "recovery_success",
            "recovery_failed",
            "repeated_no_progress_blocked",
        }
        self.assertTrue(required.issubset(status))


if __name__ == "__main__":
    unittest.main()
