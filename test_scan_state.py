from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from acceptance_queue_lane import AcceptanceQueueTarget
from scan_state import (
    AI_INVENTORY_MAX_AGE_SECONDS,
    AI_INVENTORY_RUNNING_STALE_SECONDS,
    ScanStateStore,
    _configure_scan_state_connection,
    ai_delivery_identity,
)


class ScanStateStoreQueueTest(unittest.TestCase):
    def test_acceptance_lane_lists_and_claims_only_exact_target_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            policy_revision = "a" * 64
            targets: list[AcceptanceQueueTarget] = []
            try:
                for ordinal in range(100):
                    path = root / f"target-{ordinal:03d}.mkv"
                    media_size = ordinal + 1
                    media_mtime_ns = 10_000 + ordinal
                    identity = ai_delivery_identity(
                        path,
                        media_size=media_size,
                        media_mtime_ns=media_mtime_ns,
                        policy_revision=policy_revision,
                    )
                    targets.append(
                        AcceptanceQueueTarget(
                            ordinal=ordinal,
                            canonical_path=str(identity["canonical_path"]),
                            media_size=media_size,
                            media_mtime_ns=media_mtime_ns,
                            media_fingerprint=str(identity["media_fingerprint"]),
                            policy_revision=policy_revision,
                            obligation_id=str(identity["obligation_id"]),
                            source_sha256=f"{ordinal:064x}",
                        )
                )

                allowed = targets[0]
                allowed_path = Path(allowed.canonical_path)
                alternate = targets[1]
                alternate_path = Path(alternate.canonical_path)
                store.upsert_ai_queue_candidate(
                    allowed_path,
                    allowed.media_mtime_ns,
                    source="fs_event",
                )
                store.ensure_ai_delivery_obligation(
                    allowed_path,
                    media_size=allowed.media_size,
                    media_mtime_ns=allowed.media_mtime_ns,
                    policy_revision=allowed.policy_revision,
                    obligation_id=allowed.obligation_id,
                )
                store.upsert_ai_queue_candidate(
                    alternate_path,
                    alternate.media_mtime_ns,
                    source="fs_event",
                )
                store.ensure_ai_delivery_obligation(
                    alternate_path,
                    media_size=alternate.media_size,
                    media_mtime_ns=alternate.media_mtime_ns,
                    policy_revision=alternate.policy_revision,
                    obligation_id=alternate.obligation_id,
                )
                backlog = root / "historical-backlog.mkv"
                store.upsert_ai_queue_candidate(backlog, 999, source="scan")
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(acceptance_targets=targets),
                    [allowed_path.resolve(), alternate_path.resolve()],
                )
                self.assertEqual(
                    store.iter_ai_queue_candidates(
                        acceptance_targets=targets,
                        exact_target=alternate_path,
                    ),
                    [alternate_path.resolve()],
                )
                self.assertEqual(
                    store.iter_ai_queue_candidates(
                        acceptance_targets=targets,
                        exact_target=backlog,
                    ),
                    [],
                )
                store.mark_ai_queue_running(
                    allowed_path,
                    acceptance_target=allowed,
                )
                store.commit()
                statuses = dict(
                    store._conn.execute(
                        "SELECT path, status FROM ai_candidate_queue"
                    ).fetchall()
                )
                self.assertEqual(statuses[str(allowed_path.resolve())], "running")
                self.assertEqual(statuses[str(alternate_path.resolve())], "queued")
                self.assertEqual(statuses[str(backlog.resolve())], "queued")

                with self.assertRaisesRegex(ValueError, "exact, open, claimable"):
                    store.mark_ai_queue_running(
                        backlog,
                        acceptance_target=allowed,
                    )

                store.mark_ai_queue_running(backlog)
                store.commit()
                self.assertEqual(
                    store.requeue_acceptance_running_targets(targets),
                    1,
                )
                store.commit()
                statuses = dict(
                    store._conn.execute(
                        "SELECT path, status FROM ai_candidate_queue"
                    ).fetchall()
                )
                self.assertEqual(statuses[str(allowed_path.resolve())], "queued")
                self.assertEqual(statuses[str(backlog.resolve())], "running")
            finally:
                store.close()

    def test_exact_target_queue_selection_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            neighbor = root / "neighbor.mkv"
            target = root / "target.mkv"
            missing = root / "missing.mkv"
            try:
                store.upsert_ai_queue_candidate(neighbor, 1, added_at=2000.0)
                store.upsert_ai_queue_candidate(target, 2, added_at=1000.0)
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(exact_target=target),
                    [target.resolve()],
                )
                self.assertEqual(store.iter_ai_queue_candidates(exact_target=missing), [])

                store.mark_ai_queue_done(target)
                store.commit()
                self.assertEqual(store.iter_ai_queue_candidates(exact_target=target), [])
                self.assertEqual(store.iter_ai_queue_candidates(), [neighbor.resolve()])
            finally:
                store.close()

    def test_connection_does_not_reapply_wal_when_already_enabled(self) -> None:
        conn = unittest.mock.Mock()
        conn.execute.side_effect = [None, unittest.mock.Mock(fetchone=lambda: ("wal",)), None, None, None]

        _configure_scan_state_connection(conn)

        statements = [call.args[0] for call in conn.execute.call_args_list]
        self.assertNotIn("PRAGMA journal_mode=WAL", statements)

    def test_scan_state_uses_wal_for_concurrent_worker_and_webui_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            try:
                journal_mode = str(store._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                synchronous = int(store._conn.execute("PRAGMA synchronous").fetchone()[0])
            finally:
                store.close()

            self.assertEqual(journal_mode, "wal")
            self.assertEqual(synchronous, 1)

    def test_running_stage_progress_updates_current_state_without_event_flood(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"

            store.update_ai_job_stage(video, "translation", "running", "Translating batch 1/50")
            store.update_ai_job_stage(video, "translation", "running", "Translating batch 2/50")
            store.update_ai_job_stage(video, "translation", "running", "Translating batch 3/50")
            store.commit()

            event_count = store._conn.execute(
                "SELECT COUNT(*) FROM ai_stage_events WHERE path = ?",
                (str(video.resolve()),),
            ).fetchone()[0]
            current_message = store._conn.execute(
                "SELECT message FROM ai_job_state WHERE path = ?",
                (str(video.resolve()),),
            ).fetchone()[0]
            store.close()

            self.assertEqual(event_count, 1)
            self.assertEqual(current_message, "Translating batch 3/50")

    def test_complete_stage_does_not_settle_queue_without_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            try:
                store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                store.mark_ai_queue_running(video)
                store.update_ai_job_stage(video, "complete", "ok", "Finished video")
                store.commit()

                row = store._conn.execute(
                    "SELECT status, running_at, force_ai FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(row[0], "running")
                self.assertGreater(row[1], 0)
                self.assertEqual(row[2], 0)
            finally:
                store.close()

    def test_stage_event_history_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch("scan_state.AI_STAGE_EVENT_MAX_ROWS", 3),
                patch("scan_state.AI_STAGE_EVENT_PRUNE_INTERVAL", 1),
            ):
                store = ScanStateStore(root / "state.sqlite3")
                video = root / "Anime S01E01.mkv"
                for index in range(8):
                    store.update_ai_job_stage(video, f"stage-{index}", "ok", f"message-{index}")
                store.commit()
                rows = store._conn.execute(
                    "SELECT stage FROM ai_stage_events ORDER BY id ASC"
                ).fetchall()
                store.close()

            self.assertEqual(rows, [("stage-5",), ("stage-6",), ("stage-7",)])

    def test_same_mtime_event_does_not_churn_existing_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"

            with patch("scan_state.time.time", return_value=1000.0):
                self.assertTrue(store.upsert_ai_queue_candidate(video, 123, source="scan"))
                store.commit()
            generation_before = int(
                dict(store._conn.execute("SELECT key, value FROM ai_delivery_meta"))[
                    "inventory_dirty_generation"
                ]
            )
            with patch("scan_state.time.time", return_value=2000.0):
                self.assertFalse(store.upsert_ai_queue_candidate(video, 123, source="fs_event"))
                store.commit()
            generation_after = int(
                dict(store._conn.execute("SELECT key, value FROM ai_delivery_meta"))[
                    "inventory_dirty_generation"
                ]
            )

            row = store._conn.execute(
                "SELECT status, source, added_at, updated_at FROM ai_candidate_queue WHERE path = ?",
                (str(video.resolve()),),
            ).fetchone()
            store.close()

            self.assertEqual(row, ("queued", "scan", 1000.0, 1000.0))
            self.assertEqual(generation_after, generation_before)

    def test_noop_queue_updates_do_not_wait_for_another_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "state.sqlite3"
            store = ScanStateStore(db_path)
            writer = ScanStateStore(db_path)
            video = root / "Anime S01E01.mkv"
            missing = root / "Missing S01E02.mkv"
            try:
                self.assertTrue(store.upsert_ai_queue_candidate(video, 123, source="scan"))
                store.commit()

                writer._conn.execute("BEGIN IMMEDIATE")
                store._conn.execute("PRAGMA busy_timeout=10")

                self.assertFalse(store.upsert_ai_queue_candidate(video, 123, source="scan"))
                self.assertFalse(store.remove_ai_queue_candidate(missing))
                self.assertFalse(store.remove_ai_queue_candidate(missing, clear_job_state=True))
                self.assertFalse(store.in_transaction)
            finally:
                writer._conn.rollback()
                writer.close()
                store.close()

    def test_ai_queue_tracks_running_done_and_retry_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            old_video = root / "Old S01E01.mkv"
            new_video = root / "New S01E01.mkv"

            with patch("scan_state.time.time", return_value=1000.0):
                store.upsert_ai_queue_candidate(old_video, 1)
                store.upsert_ai_queue_candidate(new_video, 2)
                store.commit()

            self.assertEqual(store.iter_ai_queue_candidates(), [new_video.resolve(), old_video.resolve()])

            store.mark_ai_queue_running(new_video)
            store.commit()
            self.assertEqual(store.iter_ai_queue_candidates(), [old_video.resolve()])

            with patch("scan_state.time.time", return_value=1000.0):
                store.mark_ai_queue_failed(new_video, "translator failed", retry_after_seconds=60)
                store.commit()
                self.assertEqual(store.iter_ai_queue_candidates(), [old_video.resolve()])

            with patch("scan_state.time.time", return_value=1061.0):
                # An unledgered historical failed_retry remains held for the
                # safety-gated remediation sweep even after its delay expires.
                self.assertEqual(store.iter_ai_queue_candidates(), [old_video.resolve()])

            store.mark_ai_queue_done(new_video)
            store.commit()
            self.assertEqual(store.iter_ai_queue_candidates(), [old_video.resolve()])
            store.close()

    def test_due_failed_retry_with_current_retryable_attempt_is_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Retry S01E01.mkv"
            video.write_bytes(b"media")
            try:
                with patch("scan_state.time.time", return_value=1000.0):
                    store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                    obligation = store.ensure_ai_delivery_obligation(
                        video,
                        media_size=video.stat().st_size,
                        media_mtime_ns=video.stat().st_mtime_ns,
                        policy_revision="policy-v1",
                        eligible_at=1000.0,
                    )
                    attempt = store.begin_ai_delivery_attempt(obligation["obligation_id"])
                    store.mark_ai_queue_running(video)
                    store.mark_ai_queue_failed(
                        video,
                        "temporary network failure",
                        retry_after_seconds=60,
                        error_code="transient_connection",
                        retry_strategy="same_pipeline",
                    )
                with patch("scan_state.time.time", return_value=1000.5):
                    store.finish_ai_delivery_attempt(
                        attempt["attempt_id"],
                        status="retryable_failure",
                        stage="translation",
                        error_code="transient_connection",
                        detail="temporary network failure",
                    )
                store.commit()

                self.assertEqual(store.iter_ai_queue_candidates(now=1059.0), [])
                self.assertEqual(
                    store.iter_ai_queue_candidates(now=1061.0),
                    [video.resolve()],
                )
            finally:
                store.close()

    def test_deadline_order_keeps_due_retry_behind_earlier_queued_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            urgent = root / "Urgent S01E01.mkv"
            retry = root / "Retry S01E02.mkv"
            later = root / "Later S01E03.mkv"
            for video in (urgent, retry, later):
                video.write_bytes(b"media")
                store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
            try:
                urgent_obligation = store.ensure_ai_delivery_obligation(
                    urgent,
                    media_size=urgent.stat().st_size,
                    media_mtime_ns=urgent.stat().st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=1000.0,
                )
                retry_obligation = store.ensure_ai_delivery_obligation(
                    retry,
                    media_size=retry.stat().st_size,
                    media_mtime_ns=retry.stat().st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=2000.0,
                )
                store.ensure_ai_delivery_obligation(
                    later,
                    media_size=later.stat().st_size,
                    media_mtime_ns=later.stat().st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=3000.0,
                )
                self.assertLess(urgent_obligation["due_at"], retry_obligation["due_at"])

                with patch("scan_state.time.time", return_value=4000.0):
                    attempt = store.begin_ai_delivery_attempt(retry_obligation["obligation_id"])
                    store.mark_ai_queue_running(retry)
                    store.mark_ai_queue_failed(
                        retry,
                        "temporary network failure",
                        error_code="transient_connection",
                        retry_strategy="same_pipeline",
                    )
                with patch("scan_state.time.time", return_value=4000.5):
                    store.finish_ai_delivery_attempt(
                        attempt["attempt_id"],
                        status="retryable_failure",
                        stage="translation",
                        error_code="transient_connection",
                        detail="temporary network failure",
                    )
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(now=4001.0),
                    [urgent.resolve(), retry.resolve(), later.resolve()],
                )
            finally:
                store.close()

    def test_due_manual_review_failure_is_not_automatically_dispatched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Review S01E01.mkv"
            video.write_bytes(b"media")
            try:
                with patch("scan_state.time.time", return_value=1000.0):
                    store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                    obligation = store.ensure_ai_delivery_obligation(
                        video,
                        media_size=video.stat().st_size,
                        media_mtime_ns=video.stat().st_mtime_ns,
                        policy_revision="policy-v1",
                        eligible_at=1000.0,
                    )
                    attempt = store.begin_ai_delivery_attempt(obligation["obligation_id"])
                    store.mark_ai_queue_running(video)
                    store.mark_ai_queue_failed(
                        video,
                        "translation safe-omission remained",
                        retry_after_seconds=0,
                        error_code="subtitle_quality_unknown",
                        retry_strategy="manual_review",
                    )
                with patch("scan_state.time.time", return_value=1000.5):
                    store.finish_ai_delivery_attempt(
                        attempt["attempt_id"],
                        status="retryable_failure",
                        stage="quality_check",
                        error_code="subtitle_quality_unknown",
                        detail="translation safe-omission remained",
                    )
                store.commit()

                self.assertEqual(store.iter_ai_queue_candidates(now=2000.0), [])
            finally:
                store.close()

    def test_queue_failure_preserves_detailed_worker_stage_and_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            try:
                store.upsert_ai_queue_candidate(video, 1)
                store.mark_ai_queue_running(video)
                store.update_ai_job_stage(
                    video,
                    "quality_check",
                    "failed",
                    "subtitle quality fail issues=translation_prompt_leak",
                )
                store.mark_ai_queue_failed(video, "worker returned false", retry_after_seconds=60)
                store.commit()

                queue_row = store._conn.execute(
                    "SELECT status, last_error FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()
                job_row = store._conn.execute(
                    "SELECT stage, status, message FROM ai_job_state WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(
                    queue_row,
                    ("failed_retry", "subtitle quality fail issues=translation_prompt_leak"),
                )
                self.assertEqual(
                    job_row,
                    (
                        "quality_check",
                        "failed",
                        "subtitle quality fail issues=translation_prompt_leak",
                    ),
                )
            finally:
                store.close()

    def test_failed_retry_candidates_maps_tuple_rows_to_named_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            try:
                store.upsert_ai_queue_candidate(video, 123, source="scan")
                store.mark_ai_queue_failed(
                    video,
                    "temporary timeout",
                    retry_after_seconds=60,
                    error_code="transient_timeout",
                    retry_strategy="same_pipeline",
                )
                store.commit()

                candidates = store.failed_retry_candidates(limit=1)

                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["path"], str(video.resolve()))
                self.assertEqual(candidates[0]["status"], "failed_retry")
                self.assertEqual(candidates[0]["last_error_code"], "transient_timeout")
                self.assertEqual(candidates[0]["retry_strategy"], "same_pipeline")
                self.assertTrue(candidates[0]["failure_revision"])
            finally:
                store.close()

    def test_ai_queue_candidate_snapshot_maps_tuple_row_to_named_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            try:
                store.upsert_ai_queue_candidate(video, 123, source="scan")
                store.mark_ai_queue_failed(
                    video,
                    "CUDA out of memory",
                    retry_after_seconds=60,
                    error_code="transient_oom",
                    retry_strategy="same_pipeline",
                )
                store.update_ai_job_stage(video, "transcription", "failed", "CUDA out of memory")
                store.commit()

                snapshot = store.ai_queue_candidate_snapshot(video)

                self.assertIsNotNone(snapshot)
                self.assertEqual(snapshot["path"], str(video.resolve()))
                self.assertEqual(snapshot["status"], "failed_retry")
                self.assertEqual(snapshot["attempts"], 1)
                self.assertEqual(snapshot["last_error_code"], "transient_oom")
                self.assertEqual(snapshot["job_stage"], "transcription")
                self.assertEqual(snapshot["job_status"], "failed")
                self.assertEqual(snapshot["job_message"], "CUDA out of memory")
                self.assertIsNone(store.ai_queue_candidate_snapshot(root / "Missing.mkv"))
            finally:
                store.close()

    def test_safe_sweep_queue_maps_tuple_and_preserves_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            media_mtime_ns = video.stat().st_mtime_ns
            try:
                store.upsert_ai_queue_candidate(video, media_mtime_ns, source="scan")
                store.mark_ai_queue_failed(
                    video,
                    "CUDA out of memory",
                    retry_after_seconds=60,
                    error_code="transient_oom",
                    retry_strategy="same_pipeline",
                )
                store.commit()
                candidate = store.failed_retry_candidates(limit=1)[0]

                self.assertFalse(
                    store.queue_failed_retry_preserving_budget(
                        video,
                        expected_failure_revision="stale-revision",
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                self.assertFalse(
                    store.queue_failed_retry_preserving_budget(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code="transient_timeout",
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                self.assertFalse(
                    store.queue_failed_retry_preserving_budget(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns + 1,
                    )
                )
                self.assertTrue(
                    store.queue_failed_retry_preserving_budget(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                store.commit()

                row = store._conn.execute(
                    """
                    SELECT status, source, attempts, last_error, last_error_code,
                           retry_strategy, failure_revision
                    FROM ai_candidate_queue WHERE path=?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(
                    row,
                    (
                        "queued",
                        "auto_retry_sweep",
                        1,
                        "CUDA out of memory",
                        "transient_oom",
                        "auto_same_pipeline",
                        candidate["failure_revision"],
                    ),
                )
            finally:
                store.close()

    def test_safe_sweep_queue_preempts_review_without_beating_manual_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            manual_priority = root / "Manual Priority S01E01.mkv"
            manual_force = root / "Manual Force S01E02.mkv"
            sweep = root / "Sweep S01E02.mkv"
            review = root / "Review S99E99.mkv"
            for video in (manual_priority, manual_force, sweep, review):
                video.write_bytes(b"media")
            try:
                store.upsert_ai_queue_candidate(
                    manual_priority,
                    manual_priority.stat().st_mtime_ns,
                )
                store.upsert_ai_queue_candidate(manual_force, manual_force.stat().st_mtime_ns)
                store.upsert_ai_queue_candidate(sweep, sweep.stat().st_mtime_ns)
                store.upsert_ai_queue_candidate(review, review.stat().st_mtime_ns)

                store.mark_ai_queue_failed(
                    sweep,
                    "CUDA out of memory",
                    error_code="transient_oom",
                    retry_strategy="lower_memory_same_pipeline",
                )
                sweep_failure = store.ai_queue_candidate_snapshot(sweep)
                self.assertIsNotNone(sweep_failure)
                assert sweep_failure is not None
                self.assertTrue(
                    store.queue_failed_retry_preserving_budget(
                        sweep,
                        expected_failure_revision=str(sweep_failure["failure_revision"]),
                        expected_failure_code="transient_oom",
                        expected_media_mtime_ns=sweep.stat().st_mtime_ns,
                    )
                )

                store.mark_ai_queue_failed(
                    review,
                    "ASR quality review",
                    max_attempts=1,
                    error_code="asr_quality",
                    retry_strategy="asr-full-retranscribe-v1",
                )
                review_failure = store.ai_queue_candidate_snapshot(review)
                self.assertIsNotNone(review_failure)
                assert review_failure is not None
                self.assertTrue(
                    store.queue_paused_review_remediation(
                        review,
                        expected_failure_revision=str(review_failure["failure_revision"]),
                        policy_revision="asr-full-retranscribe-v1",
                    )
                )
                store.prioritize_ai_queue_candidate(manual_priority)
                store.force_ai_queue_candidate(manual_force)
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(),
                    [
                        manual_priority.resolve(),
                        manual_force.resolve(),
                        sweep.resolve(),
                        review.resolve(),
                    ],
                )
            finally:
                store.close()

    def test_safe_sweep_queue_cas_does_not_overwrite_concurrent_failure_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            store = ScanStateStore(database)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            media_mtime_ns = video.stat().st_mtime_ns
            original_connection = store._conn
            interloper = sqlite3.connect(database)
            try:
                store.upsert_ai_queue_candidate(video, media_mtime_ns, source="scan")
                store.mark_ai_queue_failed(
                    video,
                    "CUDA out of memory",
                    retry_after_seconds=60,
                    error_code="transient_oom",
                    retry_strategy="same_pipeline",
                )
                store.commit()
                candidate = store.failed_retry_candidates(limit=1)[0]

                class InterleavingConnection:
                    def __init__(self) -> None:
                        self.mutated = False

                    def execute(self, sql, parameters=()):
                        if not self.mutated and "SET status='queued'" in str(sql):
                            self.mutated = True
                            interloper.execute(
                                """
                                UPDATE ai_candidate_queue
                                SET last_error='new timeout',
                                    last_error_code='transient_timeout',
                                    failure_revision=?
                                WHERE path=?
                                """,
                                ("f" * 24, str(video.resolve())),
                            )
                            interloper.commit()
                        return original_connection.execute(sql, parameters)

                    def __getattr__(self, name):
                        return getattr(original_connection, name)

                store._conn = InterleavingConnection()
                self.assertFalse(
                    store.queue_failed_retry_preserving_budget(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                store.rollback()
                store._conn = original_connection

                snapshot = store.ai_queue_candidate_snapshot(video)
                self.assertEqual(snapshot["status"], "failed_retry")
                self.assertEqual(snapshot["last_error_code"], "transient_timeout")
                self.assertEqual(snapshot["failure_revision"], "f" * 24)
            finally:
                store._conn = original_connection
                interloper.close()
                store.close()

    def test_exact_canary_claim_requires_queued_failure_and_current_media_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            media_mtime_ns = video.stat().st_mtime_ns
            try:
                store.upsert_ai_queue_candidate(video, media_mtime_ns, source="scan")
                store.mark_ai_queue_failed(
                    video,
                    "CUDA out of memory",
                    retry_after_seconds=60,
                    error_code="transient_oom",
                    retry_strategy="same_pipeline",
                )
                store.commit()
                candidate = store.failed_retry_candidates(limit=1)[0]
                self.assertTrue(
                    store.queue_failed_retry_preserving_budget(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                store.commit()

                stale_claims = (
                    {
                        "expected_failure_revision": "f" * 24,
                        "expected_failure_code": candidate["last_error_code"],
                        "expected_media_mtime_ns": media_mtime_ns,
                    },
                    {
                        "expected_failure_revision": candidate["failure_revision"],
                        "expected_failure_code": "transient_timeout",
                        "expected_media_mtime_ns": media_mtime_ns,
                    },
                    {
                        "expected_failure_revision": candidate["failure_revision"],
                        "expected_failure_code": candidate["last_error_code"],
                        "expected_media_mtime_ns": media_mtime_ns + 1,
                    },
                )
                for expected in stale_claims:
                    with self.subTest(expected=expected), self.assertRaises(ValueError):
                        store.mark_ai_queue_running(video, **expected)
                    store.rollback()
                    self.assertEqual(
                        store.ai_queue_candidate_snapshot(video)["status"],
                        "queued",
                    )

                changed_mtime_ns = media_mtime_ns + 2_000_000_000
                os.utime(video, ns=(changed_mtime_ns, changed_mtime_ns))
                with self.assertRaisesRegex(ValueError, "media identity changed"):
                    store.mark_ai_queue_running(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                os.utime(video, ns=(media_mtime_ns, media_mtime_ns))

                store.mark_ai_queue_running(
                    video,
                    expected_failure_revision=candidate["failure_revision"],
                    expected_failure_code=candidate["last_error_code"],
                    expected_media_mtime_ns=media_mtime_ns,
                )
                store.commit()
                snapshot = store.ai_queue_candidate_snapshot(video)
                self.assertEqual(snapshot["status"], "running")
                self.assertEqual(snapshot["attempts"], 1)
            finally:
                store.close()

    def test_exact_canary_containment_pauses_only_the_matching_queued_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            media_mtime_ns = video.stat().st_mtime_ns
            try:
                store.upsert_ai_queue_candidate(video, media_mtime_ns, source="scan")
                store.mark_ai_queue_failed(
                    video,
                    "CUDA out of memory",
                    retry_after_seconds=60,
                    error_code="transient_oom",
                    retry_strategy="same_pipeline",
                )
                store.commit()
                candidate = store.failed_retry_candidates(limit=1)[0]
                self.assertTrue(
                    store.queue_failed_retry_preserving_budget(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                store.commit()

                self.assertFalse(
                    store.pause_exact_queued_ai_queue_candidate(
                        video,
                        expected_failure_revision="f" * 24,
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                self.assertEqual(store.ai_queue_candidate_snapshot(video)["status"], "queued")
                self.assertTrue(
                    store.pause_exact_queued_ai_queue_candidate(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        expected_failure_code=candidate["last_error_code"],
                        expected_media_mtime_ns=media_mtime_ns,
                    )
                )
                store.commit()
                self.assertEqual(store.ai_queue_candidate_snapshot(video)["status"], "paused")
            finally:
                store.close()

    def test_safe_sweep_review_pause_maps_tuple_without_spending_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            try:
                store.upsert_ai_queue_candidate(video, 123, source="scan")
                store.mark_ai_queue_failed(
                    video,
                    "ASR quality review required",
                    retry_after_seconds=60,
                    error_code="deterministic_asr_quality",
                    retry_strategy="review_required",
                )
                store.commit()
                candidate = store.failed_retry_candidates(limit=1)[0]

                self.assertTrue(
                    store.pause_failed_retry_for_review(
                        video,
                        expected_failure_revision=candidate["failure_revision"],
                        message="Open quality review blocks automatic retry",
                    )
                )
                store.commit()

                row = store._conn.execute(
                    """
                    SELECT status, source, attempts, last_error, last_error_code,
                           retry_strategy, failure_revision
                    FROM ai_candidate_queue WHERE path=?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(
                    row,
                    (
                        "paused",
                        "existing_quality_review",
                        1,
                        "ASR quality review required",
                        "deterministic_asr_quality",
                        "manual_review",
                        candidate["failure_revision"],
                    ),
                )
            finally:
                store.close()

    def test_detected_existing_ai_done_uses_subtitle_completion_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")

            with patch("scan_state.time.time", return_value=2000.0):
                changed = store.mark_ai_queue_done(
                    video,
                    "Finished AI subtitle detected during scan",
                    completed_at=1234.0,
                    detected_existing=True,
                )
                store.commit()
            self.assertTrue(changed)

            with patch("scan_state.time.time", return_value=3000.0):
                changed = store.mark_ai_queue_done(
                    video,
                    "Finished AI subtitle detected during scan",
                    completed_at=1234.0,
                    detected_existing=True,
                )
                store.commit()
            self.assertFalse(changed)

            queue_updated_at = store._conn.execute(
                "SELECT updated_at FROM ai_candidate_queue WHERE path = ?",
                (str(video.resolve()),),
            ).fetchone()[0]
            stage, started_at, updated_at, finished_at = store._conn.execute(
                "SELECT stage, started_at, updated_at, finished_at FROM ai_job_state WHERE path = ?",
                (str(video.resolve()),),
            ).fetchone()

            self.assertEqual(queue_updated_at, 1234.0)
            self.assertEqual(stage, "detected_existing")
            self.assertEqual(started_at, 0.0)
            self.assertEqual(updated_at, 1234.0)
            self.assertEqual(finished_at, 1234.0)
            event_count = store._conn.execute(
                "SELECT COUNT(*) FROM ai_stage_events WHERE path = ?",
                (str(video.resolve()),),
            ).fetchone()[0]
            self.assertEqual(event_count, 1)
            store.close()

    def test_detected_existing_preserves_same_media_worker_completion_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            try:
                with patch("scan_state.time.time", return_value=1000.0):
                    store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                    store.mark_ai_queue_running(video)
                with patch("scan_state.time.time", return_value=1300.0):
                    store.mark_ai_queue_done(video)
                store.commit()

                before = store._conn.execute(
                    """
                    SELECT stage, status, message, started_at, updated_at, finished_at
                    FROM ai_job_state WHERE path = ?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
                event_count_before = store._conn.execute(
                    "SELECT COUNT(*) FROM ai_stage_events WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()[0]

                with patch("scan_state.time.time", return_value=2000.0):
                    changed = store.mark_ai_queue_done(
                        video,
                        "Finished AI subtitle detected during scan",
                        completed_at=1500.0,
                        detected_existing=True,
                    )
                store.commit()
                self.assertTrue(changed)
                self.assertEqual(
                    store._conn.execute(
                        """
                        SELECT stage, status, message, started_at, updated_at, finished_at
                        FROM ai_job_state WHERE path = ?
                        """,
                        (str(video.resolve()),),
                    ).fetchone(),
                    before,
                )
                self.assertEqual(before[0:2], ("complete", "ok"))
                self.assertEqual(before[3], 1000.0)
                self.assertEqual(before[5], 1300.0)
                self.assertEqual(
                    store._conn.execute(
                        "SELECT COUNT(*) FROM ai_stage_events WHERE path = ?",
                        (str(video.resolve()),),
                    ).fetchone()[0],
                    event_count_before,
                )

                with patch("scan_state.time.time", return_value=2100.0):
                    self.assertFalse(
                        store.mark_ai_queue_done(
                            video,
                            "Finished AI subtitle detected during scan",
                            completed_at=1500.0,
                            detected_existing=True,
                        )
                    )
            finally:
                store.close()

    def test_detected_existing_does_not_reuse_completion_for_changed_media_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            try:
                current_mtime_ns = video.stat().st_mtime_ns
                with patch("scan_state.time.time", return_value=1000.0):
                    store.upsert_ai_queue_candidate(video, current_mtime_ns)
                    store.mark_ai_queue_running(video)
                with patch("scan_state.time.time", return_value=1300.0):
                    store.mark_ai_queue_done(video)
                new_mtime_ns = current_mtime_ns + 1_000_000_000
                os.utime(video, ns=(new_mtime_ns, new_mtime_ns))
                with patch("scan_state.time.time", return_value=1400.0):
                    store.upsert_ai_queue_candidate(video, new_mtime_ns)
                store.commit()

                store.mark_ai_queue_done(
                    video,
                    "Finished AI subtitle detected after media changed",
                    completed_at=1500.0,
                    detected_existing=True,
                )
                store.commit()

                self.assertEqual(
                    store._conn.execute(
                        "SELECT stage, status, started_at, finished_at FROM ai_job_state WHERE path = ?",
                        (str(video.resolve()),),
                    ).fetchone(),
                    ("detected_existing", "ok", 0.0, 1500.0),
                )
            finally:
                store.close()

    def test_remove_ai_queue_candidate_reports_only_real_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")

            self.assertFalse(store.remove_ai_queue_candidate(video))

            store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
            store.commit()
            self.assertTrue(store.remove_ai_queue_candidate(video))
            store.commit()
            self.assertFalse(store.remove_ai_queue_candidate(video))
            store.close()

    def test_same_sequence_prefers_newer_file_mtime_before_queue_arrival(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            newer_file = root / "Newer File S01E01.mkv"
            newer_arrival = root / "Newer Arrival S01E01.mkv"
            newer_file.write_text("", encoding="utf-8")
            newer_arrival.write_text("", encoding="utf-8")

            with patch("scan_state.time.time", return_value=1000.0):
                store.upsert_ai_queue_candidate(newer_file, 9_999)
                store.commit()
            with patch("scan_state.time.time", return_value=2000.0):
                store.upsert_ai_queue_candidate(newer_arrival, 1)
                store.commit()

            self.assertEqual(
                store.iter_ai_queue_candidates(),
                [newer_file.resolve(), newer_arrival.resolve()],
            )
            self.assertEqual(
                store.iter_ai_queue_candidates(oldest_first=True),
                [newer_file.resolve(), newer_arrival.resolve()],
            )
            store.close()

    def test_recent_queue_orders_by_filename_sequence_then_file_dates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            season_1_episode_99 = root / "Series S01E99.mkv"
            season_2_episode_1 = root / "Series S02E01.mkv"
            episode_2_older = root / "Older Variant S02E02.mkv"
            episode_2_newer = root / "Newer Variant S02E02.mkv"
            episode_only_11 = root / "Episode Only - 11 [WebRip].mkv"
            episode_only_12 = root / "Episode Only - 12 [WebRip].mkv"
            unparsed_older = root / "Unparsed Older.mkv"
            unparsed_newer = root / "Unparsed Newer.mkv"
            rows = (
                (season_1_episode_99, 900, 900.0),
                (season_2_episode_1, 100, 100.0),
                (episode_2_older, 600, 900.0),
                (episode_2_newer, 700, 100.0),
                (episode_only_11, 999, 999.0),
                (episode_only_12, 1, 1.0),
                (unparsed_older, 10, 999.0),
                (unparsed_newer, 20, 1.0),
            )
            try:
                for video, mtime_ns, added_at in rows:
                    video.write_text("", encoding="utf-8")
                    store.upsert_ai_queue_candidate(
                        video,
                        mtime_ns,
                        added_at=added_at,
                    )
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(),
                    [
                        episode_2_newer.resolve(),
                        episode_2_older.resolve(),
                        season_2_episode_1.resolve(),
                        season_1_episode_99.resolve(),
                        episode_only_12.resolve(),
                        episode_only_11.resolve(),
                        unparsed_newer.resolve(),
                        unparsed_older.resolve(),
                    ],
                )
            finally:
                store.close()

    def test_oldest_fairness_cycle_ignores_filename_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            older_low_sequence = root / "Series S01E01.mkv"
            newer_high_sequence = root / "Series S99E999.mkv"
            try:
                store.upsert_ai_queue_candidate(
                    older_low_sequence,
                    1,
                    added_at=1000.0,
                )
                store.upsert_ai_queue_candidate(
                    newer_high_sequence,
                    2,
                    added_at=2000.0,
                )
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(),
                    [newer_high_sequence.resolve(), older_low_sequence.resolve()],
                )
                self.assertEqual(
                    store.iter_ai_queue_candidates(oldest_first=True),
                    [older_low_sequence.resolve(), newer_high_sequence.resolve()],
                )
            finally:
                store.close()

    def test_queue_sequence_columns_migrate_and_backfill_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            video = root / "Legacy Series S04E12.mkv"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO ai_candidate_queue(path, mtime_ns) VALUES (?, ?)",
                    (str(video.resolve()), 123),
                )
                connection.commit()
            finally:
                connection.close()

            for _reopen in range(2):
                store = ScanStateStore(database)
                try:
                    columns = {
                        str(row[1])
                        for row in store._conn.execute(
                            "PRAGMA table_info(ai_candidate_queue)"
                        ).fetchall()
                    }
                    self.assertIn("filename_season", columns)
                    self.assertIn("filename_episode", columns)
                    self.assertEqual(
                        store._conn.execute(
                            "SELECT filename_season, filename_episode "
                            "FROM ai_candidate_queue WHERE path=?",
                            (str(video.resolve()),),
                        ).fetchone(),
                        (4, 12),
                    )
                finally:
                    store.close()

    def test_tracked_queue_uses_earliest_deadline_even_on_recent_first_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            earlier = root / "Earlier Deadline S01E01.mkv"
            later = root / "Later Deadline S01E02.mkv"
            earlier.write_bytes(b"media")
            later.write_bytes(b"media")
            try:
                with patch("scan_state.time.time", return_value=1000.0):
                    store.upsert_ai_queue_candidate(earlier, earlier.stat().st_mtime_ns)
                with patch("scan_state.time.time", return_value=2000.0):
                    store.upsert_ai_queue_candidate(later, later.stat().st_mtime_ns)
                store.ensure_ai_delivery_obligation(
                    earlier,
                    media_size=earlier.stat().st_size,
                    media_mtime_ns=earlier.stat().st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=1000.0,
                )
                store.ensure_ai_delivery_obligation(
                    later,
                    media_size=later.stat().st_size,
                    media_mtime_ns=later.stat().st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=2000.0,
                )
                store.commit()

                expected = [earlier.resolve(), later.resolve()]
                self.assertEqual(store.iter_ai_queue_candidates(), expected)
                self.assertEqual(store.iter_ai_queue_candidates(oldest_first=True), expected)
            finally:
                store.close()

    def test_manual_and_force_priority_remain_ahead_of_tracked_deadlines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            urgent = root / "Urgent S01E01.mkv"
            forced = root / "Forced S01E02.mkv"
            manual = root / "Manual S01E03.mkv"
            for video in (urgent, forced, manual):
                video.write_bytes(b"media")
                store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
            try:
                for video, eligible_at in ((urgent, 1000.0), (forced, 2000.0), (manual, 3000.0)):
                    store.ensure_ai_delivery_obligation(
                        video,
                        media_size=video.stat().st_size,
                        media_mtime_ns=video.stat().st_mtime_ns,
                        policy_revision="policy-v1",
                        eligible_at=eligible_at,
                    )
                store.force_ai_queue_candidate(forced)
                store.prioritize_ai_queue_candidate(manual)
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(),
                    [manual.resolve(), forced.resolve(), urgent.resolve()],
                )
            finally:
                store.close()

    def test_stale_media_obligation_does_not_control_current_queue_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            stale_identity = root / "Changed S01E01.mkv"
            tracked = root / "Tracked S01E02.mkv"
            stale_identity.write_bytes(b"media")
            tracked.write_bytes(b"media")
            try:
                store.upsert_ai_queue_candidate(stale_identity, 200)
                store.upsert_ai_queue_candidate(tracked, 300)
                store.ensure_ai_delivery_obligation(
                    stale_identity,
                    media_size=stale_identity.stat().st_size,
                    media_mtime_ns=100,
                    policy_revision="policy-v1",
                    eligible_at=1000.0,
                )
                store.ensure_ai_delivery_obligation(
                    tracked,
                    media_size=tracked.stat().st_size,
                    media_mtime_ns=300,
                    policy_revision="policy-v1",
                    eligible_at=2000.0,
                )
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(),
                    [tracked.resolve(), stale_identity.resolve()],
                )
            finally:
                store.close()

    def test_equal_deadline_queue_order_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            alpha = root / "Alpha S01E02.mkv"
            zeta = root / "Zeta S01E02.mkv"
            alpha.write_bytes(b"media")
            zeta.write_bytes(b"media")
            try:
                with patch("scan_state.time.time", return_value=1000.0):
                    for video in (zeta, alpha):
                        store.upsert_ai_queue_candidate(video, 123)
                        store.ensure_ai_delivery_obligation(
                            video,
                            media_size=video.stat().st_size,
                            media_mtime_ns=123,
                            policy_revision="policy-v1",
                            eligible_at=1000.0,
                        )
                store.commit()

                self.assertEqual(
                    store.iter_ai_queue_candidates(),
                    [alpha.resolve(), zeta.resolve()],
                )
            finally:
                store.close()

    def test_failure_retry_budget_pauses_repeated_failure_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            store = ScanStateStore(root / "state.sqlite3")
            try:
                store.upsert_ai_queue_candidate(video, 1)
                self.assertFalse(store.mark_ai_queue_failed(video, "failure 1", max_attempts=3))
                self.assertFalse(store.mark_ai_queue_failed(video, "failure 2", max_attempts=3))
                self.assertTrue(store.mark_ai_queue_failed(video, "failure 3", max_attempts=3))
                store.commit()

                row = store._conn.execute(
                    "SELECT status, source, attempts, last_error, next_retry_at FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(row, ("paused", "failure_review", 3, "failure 3", 0.0))
                self.assertEqual(store.iter_ai_queue_candidates(), [])
            finally:
                store.close()

    def test_force_ai_queue_candidate_overrides_done_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")

            store.mark_ai_queue_done(video)
            store.commit()
            self.assertEqual(store.iter_ai_queue_candidates(), [])

            store.force_ai_queue_candidate(video)
            store.commit()
            self.assertTrue(store.is_force_ai_queue_candidate(video))
            self.assertEqual(store.iter_ai_queue_candidates(), [video.resolve()])

            store.mark_ai_queue_done(video)
            store.commit()
            self.assertFalse(store.is_force_ai_queue_candidate(video))
            self.assertEqual(store.iter_ai_queue_candidates(), [])
            store.close()

    def test_force_ai_queue_candidate_resets_retry_fields_with_canonical_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            store = ScanStateStore(root / "state.sqlite3")
            try:
                store.upsert_ai_queue_candidate(video, 1)
                store.mark_ai_queue_failed(video, "temporary failure")
                store.force_ai_queue_candidate(video)
                store.commit()

                row = store._conn.execute(
                    """
                    SELECT attempts, typeof(attempts), last_error, typeof(last_error)
                    FROM ai_candidate_queue
                    WHERE path = ?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(row, (0, "integer", "", "text"))
            finally:
                store.close()

    def test_manual_retry_bypasses_cooldown_without_forcing_ai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            try:
                store.upsert_ai_queue_candidate(video, 1, source="scan")
                store.mark_ai_queue_failed(video, "temporary failure", retry_after_seconds=60)
                store.retry_ai_queue_candidate(video)
                store.commit()

                self.assertEqual(store.ai_queue_candidate_policy(video), (False, True))
                self.assertFalse(store.is_force_ai_queue_candidate(video))
            finally:
                store.close()

    def test_bulk_retry_only_requeues_transient_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            retryable = root / "Retryable S01E01.mkv"
            review = root / "Review S01E02.mkv"
            store = ScanStateStore(root / "state.sqlite3")
            try:
                store.upsert_ai_queue_candidate(retryable, 1)
                store.mark_ai_queue_failed(retryable, "temporary failure", retry_after_seconds=60)
                store.upsert_ai_queue_candidate(review, 2)
                store.mark_ai_queue_failed(review, "quality failure", max_attempts=1)

                self.assertEqual(store.retry_all_failed_ai_queue_candidates(), 1)
                store.commit()

                rows = dict(
                    store._conn.execute(
                        "SELECT path, status FROM ai_candidate_queue ORDER BY path"
                    ).fetchall()
                )
                self.assertEqual(rows[str(retryable.resolve())], "queued")
                self.assertEqual(rows[str(review.resolve())], "paused")
            finally:
                store.close()

    def test_upsert_preserves_done_when_same_file_is_seen_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            try:
                video = root / "Anime S01E01.mkv"
                video.write_text("", encoding="utf-8")
                mtime_ns = video.stat().st_mtime_ns

                store.upsert_ai_queue_candidate(video, mtime_ns)
                store.mark_ai_queue_done(video)
                store.upsert_ai_queue_candidate(video, mtime_ns)
                store.commit()
                self.assertEqual(store.iter_ai_queue_candidates(), [])

                store.upsert_ai_queue_candidate(video, mtime_ns + 1)
                store.commit()
                self.assertEqual(store.iter_ai_queue_candidates(), [video.resolve()])
            finally:
                store.close()

    def test_force_ai_queue_candidate_moves_to_front(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            forced_video = root / "Old Anime S01E01.mkv"
            queued_video = root / "New Anime S01E01.mkv"
            forced_video.write_text("", encoding="utf-8")
            queued_video.write_text("", encoding="utf-8")

            with patch("scan_state.time.time", return_value=1000.0):
                store.upsert_ai_queue_candidate(forced_video, 1)
                store.upsert_ai_queue_candidate(queued_video, 999)
                store.commit()

            self.assertEqual(store.iter_ai_queue_candidates(), [queued_video.resolve(), forced_video.resolve()])

            with patch("scan_state.time.time_ns", return_value=1001):
                store.force_ai_queue_candidate(forced_video)
                store.commit()

            self.assertTrue(store.is_force_ai_queue_candidate(forced_video))
            self.assertEqual(store.iter_ai_queue_candidates(), [forced_video.resolve(), queued_video.resolve()])
            store.close()

    def test_prioritize_ai_queue_candidate_moves_older_item_to_front(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            older_video = root / "Old Anime S01E01.mkv"
            newer_video = root / "New Anime S01E01.mkv"
            older_video.write_text("", encoding="utf-8")
            newer_video.write_text("", encoding="utf-8")

            with patch("scan_state.time.time", return_value=1000.0):
                store.upsert_ai_queue_candidate(older_video, 1)
            with patch("scan_state.time.time", return_value=2000.0):
                store.upsert_ai_queue_candidate(newer_video, 2)
            store.commit()

            self.assertEqual(store.iter_ai_queue_candidates(), [newer_video.resolve(), older_video.resolve()])

            with patch("scan_state.time.time", return_value=3000.0):
                store.prioritize_ai_queue_candidate(older_video)
            store.commit()

            self.assertEqual(store.iter_ai_queue_candidates(), [older_video.resolve(), newer_video.resolve()])
            self.assertEqual(
                store.iter_ai_queue_candidates(oldest_first=True),
                [older_video.resolve(), newer_video.resolve()],
            )
            self.assertEqual(store.ai_queue_candidate_policy(older_video), (False, True))
            row = store._conn.execute(
                "SELECT mtime_ns, filename_season, filename_episode, "
                "added_at, force_ai, source FROM ai_candidate_queue WHERE path = ?",
                (str(older_video.resolve()),),
            ).fetchone()
            self.assertEqual(row, (1, 1, 1, 3000.0, 0, "manual_priority"))
            store.close()

    def test_requeue_running_from_previous_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Interrupted Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")

            store.upsert_ai_queue_candidate(video, 10)
            store.mark_ai_queue_running(video)
            store.commit()
            self.assertEqual(store.iter_ai_queue_candidates(), [])

            count = store.requeue_running_from_previous_worker()
            store.commit()

            self.assertEqual(count, 1)
            self.assertEqual(store.iter_ai_queue_candidates(), [video.resolve()])
            store.close()

    def test_restart_requeues_completed_running_without_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            completed = root / "Completed S01E01.mkv"
            interrupted = root / "Interrupted S01E02.mkv"
            completed.write_text("", encoding="utf-8")
            interrupted.write_text("", encoding="utf-8")
            try:
                for video in (completed, interrupted):
                    store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                    store.mark_ai_queue_running(video)
                store.update_ai_job_stage(completed, "complete", "ok", "Finished video")
                store.commit()

                self.assertEqual(store.requeue_running_from_previous_worker(), 2)
                store.commit()
                rows = dict(
                    store._conn.execute(
                        "SELECT path, status FROM ai_candidate_queue WHERE path IN (?, ?)",
                        (str(completed.resolve()), str(interrupted.resolve())),
                    ).fetchall()
                )
                self.assertEqual(rows[str(completed.resolve())], "queued")
                self.assertEqual(rows[str(interrupted.resolve())], "queued")
            finally:
                store.close()

    def test_latest_ai_delivery_attempt_returns_highest_attempt_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Attempted S01E01.mkv"
            video.write_bytes(b"media")
            try:
                stat = video.stat()
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat.st_size,
                    media_mtime_ns=stat.st_mtime_ns,
                    policy_revision="policy-v1",
                )
                first = store.begin_ai_delivery_attempt(
                    obligation["obligation_id"],
                    started_at=1000.0,
                )
                store.finish_ai_delivery_attempt(
                    first["attempt_id"],
                    status="retryable_failure",
                    finished_at=1100.0,
                )
                second = store.begin_ai_delivery_attempt(
                    obligation["obligation_id"],
                    started_at=1200.0,
                )

                latest = store.latest_ai_delivery_attempt(obligation["obligation_id"])
                self.assertEqual(latest["attempt_id"], second["attempt_id"])
                self.assertEqual(latest["attempt_number"], 2)
                self.assertEqual(latest["status"], "running")
                self.assertIsNone(store.latest_ai_delivery_attempt("missing-obligation"))
            finally:
                store.close()

    def test_restart_reconciles_source_translation_with_strict_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Completed S01E01.mkv"
            video.write_bytes(b"media")
            try:
                stat = video.stat()
                store.upsert_ai_queue_candidate(video, stat.st_mtime_ns)
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat.st_size,
                    media_mtime_ns=stat.st_mtime_ns,
                    policy_revision="policy-v1",
                )
                attempt = store.begin_ai_delivery_attempt(
                    obligation["obligation_id"],
                    started_at=1000.0,
                )
                store.mark_ai_queue_running(video)
                store.update_ai_job_stage(
                    video,
                    "source_translation",
                    "ok",
                    "Traditional-Chinese delivery completed from language=en",
                )
                store.commit()

                def strict_evidence(_video, observed_attempt, observed_obligation):
                    self.assertEqual(observed_attempt["attempt_id"], attempt["attempt_id"])
                    self.assertEqual(observed_obligation["obligation_id"], obligation["obligation_id"])
                    return {
                        "manifest_path": str(root / "manifest.json"),
                        "manifest_sha256": "a" * 64,
                        "verified_at": 1100.0,
                        "verification": {
                            "manifest_schema_version": 2,
                            "delivery_contract": "ai-delivery-v1",
                            "required_outputs_complete": True,
                            "hashes_verified": True,
                            "quality_gates_passed": True,
                            "publication_marker_absent": True,
                            "media_identity_matched": True,
                            "policy_revision_matched": True,
                            "expected_policy_revision": "policy-v1",
                            "manifest_policy_revision": "policy-v1",
                            "publication_semantics_verified": True,
                            "publication_contract": "ai-publication-semantics-v2",
                            "publication_kind": "translated_trilingual",
                            "output_languages": ["ja", "zh-CN", "zh-TW"],
                            "attempt_started_at": 1000.0,
                        },
                    }

                self.assertEqual(
                    store.reconcile_completed_running(
                        delivery_evidence_resolver=strict_evidence,
                    ),
                    1,
                )
                store.commit()

                queue_status = store._conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
                self.assertEqual(queue_status, "done")
                self.assertEqual(
                    store.get_ai_delivery_attempt(attempt["attempt_id"])["status"],
                    "succeeded",
                )
                self.assertEqual(
                    store.get_ai_delivery_obligation(obligation["obligation_id"])["state"],
                    "succeeded",
                )
            finally:
                store.close()

    def test_restart_rejects_incomplete_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Completed S01E01.mkv"
            video.write_bytes(b"media")
            try:
                stat = video.stat()
                store.upsert_ai_queue_candidate(video, stat.st_mtime_ns)
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat.st_size,
                    media_mtime_ns=stat.st_mtime_ns,
                    policy_revision="policy-v1",
                )
                attempt = store.begin_ai_delivery_attempt(obligation["obligation_id"])
                store.mark_ai_queue_running(video)
                store.update_ai_job_stage(video, "complete", "ok", "Finished video")
                store.commit()

                self.assertEqual(
                    store.reconcile_completed_running(
                        delivery_evidence_resolver=lambda *_args: {
                            "manifest_path": str(root / "manifest.json"),
                            "manifest_sha256": "a" * 64,
                            "verified_at": time.time(),
                            "verification": {"hashes_verified": True},
                        },
                    ),
                    0,
                )
                self.assertEqual(store.requeue_running_from_previous_worker(), 1)
                store.commit()

                queue_status = store._conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
                self.assertEqual(queue_status, "queued")
                self.assertEqual(
                    store.get_ai_delivery_attempt(attempt["attempt_id"])["status"],
                    "deferred",
                )
                self.assertEqual(
                    store.get_ai_delivery_obligation(obligation["obligation_id"])["state"],
                    "open",
                )
                self.assertEqual(
                    store.get_ai_delivery_obligation(obligation["obligation_id"])["outcome_code"],
                    "worker_restarted",
                )
            finally:
                store.close()

    def test_requeue_stale_running_uses_latest_job_stage_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            video = root / "Long Translation S01E01.mkv"
            video.write_text("", encoding="utf-8")

            with patch("scan_state.time.time", return_value=1000.0):
                store.upsert_ai_queue_candidate(video, 10)
                store.mark_ai_queue_running(video)
                store.commit()

            with patch("scan_state.time.time", return_value=2000.0):
                store.update_ai_job_stage(video, "translation", "running", "Translating batch 20/35")
                store.commit()

            with patch("scan_state.time.time", return_value=2500.0):
                self.assertEqual(store.requeue_stale_running(900), 0)
                store.commit()
                self.assertEqual(store.iter_ai_queue_candidates(), [])

            with patch("scan_state.time.time", return_value=3001.0):
                self.assertEqual(store.requeue_stale_running(900), 1)
                store.commit()
                self.assertEqual(store.iter_ai_queue_candidates(), [video.resolve()])

            store.close()


    def test_review_remediation_preserves_budget_and_requires_exact_paused_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            store = ScanStateStore(root / "state.sqlite3")
            try:
                store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                store.mark_ai_queue_running(video)
                self.assertTrue(
                    store.mark_ai_queue_failed(
                        video,
                        "deterministic ASR quality review",
                        max_attempts=1,
                        error_code="deterministic_asr_quality",
                        retry_strategy="manual_review",
                    )
                )
                before = store.ai_queue_candidate_snapshot(video)
                self.assertEqual(before["status"], "paused")
                self.assertEqual(before["attempts"], 1)

                self.assertFalse(
                    store.queue_paused_review_remediation(
                        video,
                        expected_failure_revision="stale-revision",
                        policy_revision="asr-full-retranscribe-v1",
                    )
                )
                self.assertTrue(
                    store.queue_paused_review_remediation(
                        video,
                        expected_failure_revision=before["failure_revision"],
                        policy_revision="asr-full-retranscribe-v1",
                    )
                )
                store.commit()
                after = store.ai_queue_candidate_snapshot(video)
                self.assertEqual(after["status"], "queued")
                self.assertEqual(after["source"], "auto_review_remediation")
                self.assertEqual(after["attempts"], 1)
                self.assertEqual(after["failure_revision"], before["failure_revision"])
                self.assertEqual(after["retry_strategy"], "asr-full-retranscribe-v1")
                self.assertEqual(after["force_ai"], 1)
                self.assertEqual(store.active_review_remediation_count(), 1)
                self.assertEqual(store.running_ai_queue_count(), 0)
                store.mark_ai_queue_running(video)
                self.assertEqual(store.active_review_remediation_count(), 1)
                self.assertEqual(store.running_ai_queue_count(), 1)
                store.mark_ai_queue_failed(
                    video,
                    "bounded automatic repair failed",
                    max_attempts=1,
                    error_code="quality_gate_failed",
                    retry_strategy="manual_review",
                )
                self.assertEqual(store.active_review_remediation_count(), 0)
                self.assertEqual(store.running_ai_queue_count(), 0)
            finally:
                store.close()

    def test_line_retranslation_claim_restores_exactly_and_restart_cannot_orphan_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            store = ScanStateStore(root / "state.sqlite3")
            try:
                store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                store.mark_ai_queue_running(video)
                store.update_ai_job_stage(
                    video,
                    "quality_check",
                    "failed",
                    "Translation safe-omission remained after bounded same-job recovery: indexes=[2]",
                )
                store.mark_ai_queue_failed(
                    video,
                    "worker returned false",
                    max_attempts=1,
                    error_code="translation_safe_omission",
                    retry_strategy="manual_review",
                )
                before = store.ai_queue_candidate_snapshot(video)
                claim = store.claim_ai_line_retranslation(
                    video,
                    expected_failure_revision=before["failure_revision"],
                    expected_media_mtime_ns=video.stat().st_mtime_ns,
                )
                self.assertEqual(store.ai_queue_candidate_snapshot(video)["status"], "running")
                self.assertTrue(
                    store.restore_ai_line_retranslation(
                        video,
                        original_status=claim["original_status"],
                        original_next_retry_at=claim["original_next_retry_at"],
                        original_source=claim["original_source"],
                        original_job_stage=claim["original_job_stage"],
                        original_job_status=claim["original_job_status"],
                        original_job_message=claim["original_job_message"],
                        expected_running_at=claim["running_at"],
                        expected_failure_revision=claim["failure_revision"],
                        expected_media_mtime_ns=claim["media_mtime_ns"],
                    )
                )
                restored = store.ai_queue_candidate_snapshot(video)
                self.assertEqual(restored["status"], before["status"])
                self.assertEqual(restored["attempts"], before["attempts"])
                self.assertEqual(restored["failure_revision"], before["failure_revision"])
                self.assertEqual(restored["last_error"], before["last_error"])

                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=video.stat().st_size,
                    media_mtime_ns=video.stat().st_mtime_ns,
                    policy_revision="line-repair-policy",
                )
                attempt = store.begin_ai_delivery_attempt(obligation["obligation_id"])
                store.claim_ai_line_retranslation(
                    video,
                    expected_failure_revision=before["failure_revision"],
                    expected_media_mtime_ns=video.stat().st_mtime_ns,
                )
                store.commit()

                self.assertFalse(
                    store.mark_ai_queue_done(
                        video,
                        "Scanner detected finished subtitle during line repair",
                        detected_existing=True,
                    )
                )
                self.assertEqual(store.ai_queue_candidate_snapshot(video)["status"], "running")

                self.assertEqual(store.requeue_running_from_previous_worker(), 1)
                store.commit()
                recovered = store.ai_queue_candidate_snapshot(video)
                self.assertEqual(recovered["status"], "paused")
                self.assertEqual(recovered["source"], "failure_review")
                self.assertEqual(recovered["attempts"], before["attempts"])
                self.assertEqual(
                    store.get_ai_delivery_attempt(attempt["attempt_id"])["status"],
                    "review_required",
                )
            finally:
                store.close()


class ScanStateInventoryEpochTest(unittest.TestCase):
    @staticmethod
    def _strict_verification(policy_revision: str) -> dict[str, object]:
        return {
            "publication_semantics_verified": True,
            "publication_contract": "ai-publication-semantics-v2",
            "publication_kind": "translated_trilingual",
            "output_languages": ["ja", "zh-CN", "zh-TW"],
            "expected_policy_revision": policy_revision,
            "manifest_policy_revision": policy_revision,
            "policy_revision_matched": True,
        }

    def test_restart_abandons_running_epoch_and_restarts_from_contract_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            try:
                first = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=1000.0,
                )
                store.commit()
                second = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=1100.0,
                )
                store.commit()

                prior = store._conn.execute(
                    "SELECT state, completed_at, failure_code FROM ai_inventory_epochs WHERE epoch_id=?",
                    (first["epoch_id"],),
                ).fetchone()
                self.assertEqual(prior, ("abandoned", 0.0, "worker_restarted"))
                self.assertNotEqual(first["epoch_id"], second["epoch_id"])
                self.assertEqual(second["eligibility_bound"], first["eligibility_bound"])
            finally:
                store.close()

    def test_matching_running_epoch_can_be_resumed_with_a_renewed_walk_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            try:
                first = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=1000.0,
                )
                store.commit()
                resumed = store.resume_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    resumed_at=1100.0,
                )
                store.commit()

                self.assertIsNotNone(resumed)
                assert resumed is not None
                self.assertTrue(resumed["resumed"])
                self.assertEqual(resumed["epoch_id"], first["epoch_id"])
                self.assertEqual(resumed["resumed_from_started_at"], 1000.0)
                self.assertEqual(resumed["started_at"], 1100.0)
                self.assertEqual(resumed["eligibility_bound"], first["eligibility_bound"])
                self.assertEqual(
                    store._conn.execute(
                        "SELECT state, started_at FROM ai_inventory_epochs"
                    ).fetchone(),
                    ("running", 1100.0),
                )
            finally:
                store.close()

    def test_resumed_epoch_prunes_observations_not_seen_in_the_renewed_walk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            removed = root / "Removed S01E01.mkv"
            current = root / "Current S01E02.mkv"
            removed.write_bytes(b"removed")
            current.write_bytes(b"current")
            removed_stat = removed.stat()
            current_stat = current.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=1000.0,
                )
                for video, stat_result in (
                    (removed, removed_stat),
                    (current, current_stat),
                ):
                    store.record_ai_inventory_observation(
                        epoch["epoch_id"],
                        video,
                        media_size=stat_result.st_size,
                        media_mtime_ns=stat_result.st_mtime_ns,
                        policy_revision="policy-v1",
                        classification="local_chinese",
                        disposition="policy_excluded",
                        observed_at=1001.0,
                    )
                store.commit()
                resumed = store.resume_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    resumed_at=1100.0,
                )
                assert resumed is not None
                store.record_ai_inventory_observation(
                    epoch["epoch_id"],
                    current,
                    media_size=current_stat.st_size,
                    media_mtime_ns=current_stat.st_mtime_ns,
                    policy_revision="policy-v1",
                    classification="local_chinese",
                    disposition="policy_excluded",
                    observed_at=1101.0,
                )
                result = store.finalize_ai_inventory_epoch(
                    epoch["epoch_id"], completed_at=1102.0
                )
                store.commit()

                self.assertEqual(result["observed_count"], 1)
                self.assertEqual(
                    store._conn.execute(
                        "SELECT canonical_path FROM ai_media_inventory"
                    ).fetchall(),
                    [(str(current.resolve()),)],
                )
            finally:
                store.close()

    def test_inventory_path_savepoint_rolls_back_queue_obligation_and_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            stat_result = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                )
                store.commit()
                store.begin_ai_inventory_path(epoch["epoch_id"])
                store.upsert_ai_queue_candidate(video, stat_result.st_mtime_ns)
                store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=epoch["eligibility_bound"],
                )
                store.record_ai_inventory_observation(
                    epoch["epoch_id"],
                    video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    classification="needs_ai",
                    disposition="delivery_required",
                )
                store.rollback_ai_inventory_path()
                store.commit()

                self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM ai_candidate_queue").fetchone()[0], 0)
                self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM ai_delivery_obligations").fetchone()[0], 0)
                self.assertEqual(store._conn.execute("SELECT COUNT(*) FROM ai_media_inventory").fetchone()[0], 0)
            finally:
                store.close()

    def test_finalize_empty_epoch_is_complete_but_has_no_synthetic_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=1000.0,
                )
                result = store.finalize_ai_inventory_epoch(epoch["epoch_id"], completed_at=1001.0)
                store.commit()
                self.assertTrue(result["coverage_complete"])
                self.assertEqual(result["observed_count"], 0)
                self.assertEqual(result["delivery_required_count"], 0)
                self.assertEqual(store.ai_delivery_slo_summary(now=1002.0)["denominator"], 0)
            finally:
                store.close()

    def test_finalize_marks_done_without_strict_succeeded_ledger_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            stat_result = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                )
                store.record_ai_inventory_observation(
                    epoch["epoch_id"], video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    classification="finished",
                    disposition="delivered",
                    ai_output_detected=True,
                    ai_output_mtime=time.time(),
                )
                result = store.finalize_ai_inventory_epoch(epoch["epoch_id"])
                store.commit()
                self.assertFalse(result["coverage_complete"])
                self.assertEqual(result["delivery_required_count"], 1)
                self.assertEqual(result["untracked_count"], 1)
            finally:
                store.close()

    def test_failed_epoch_cannot_be_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                )
                self.assertTrue(
                    store.fail_ai_inventory_epoch(
                        epoch["epoch_id"], failure_code="walk_error", detail="permission denied"
                    )
                )
                with self.assertRaises(RuntimeError):
                    store.finalize_ai_inventory_epoch(epoch["epoch_id"])
                store.commit()
                row = store._conn.execute(
                    "SELECT state, completed_at, coverage_complete FROM ai_inventory_epochs WHERE epoch_id=?",
                    (epoch["epoch_id"],),
                ).fetchone()
                self.assertEqual(row, ("failed", 0.0, 0))
            finally:
                store.close()

    def test_filesystem_delta_after_epoch_start_prevents_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=1000.0,
                )
                store.mark_ai_inventory_dirty(observed_at=1001.0)
                with self.assertRaisesRegex(RuntimeError, "changed during the proof walk"):
                    store.finalize_ai_inventory_epoch(epoch["epoch_id"], completed_at=1002.0)
                store.fail_ai_inventory_epoch(
                    epoch["epoch_id"], failure_code="inventory_changed_during_walk"
                )
                store.commit()
                self.assertEqual(
                    store._conn.execute(
                        "SELECT state, completed_at FROM ai_inventory_epochs WHERE epoch_id=?",
                        (epoch["epoch_id"],),
                    ).fetchone(),
                    ("failed", 0.0),
                )
            finally:
                store.close()

    def test_inventory_finalize_holds_wal_writer_before_recount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.sqlite3"
            store = ScanStateStore(state_path)
            competitor = ScanStateStore(state_path)
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=1000.0,
                )
                store.commit()
                competitor._conn.execute("PRAGMA busy_timeout=0")
                original_recount = store._recount_ai_inventory_tracked_ledgers
                competing_write_attempted = False

                def recount_with_competing_writer(epoch_id: str) -> int:
                    nonlocal competing_write_attempted
                    competing_write_attempted = True
                    with self.assertRaises(sqlite3.OperationalError):
                        competitor.mark_ai_inventory_dirty(observed_at=1000.5)
                        competitor.commit()
                    competitor.rollback()
                    return original_recount(epoch_id)

                with patch.object(
                    store,
                    "_recount_ai_inventory_tracked_ledgers",
                    side_effect=recount_with_competing_writer,
                ):
                    result = store.finalize_ai_inventory_epoch(
                        epoch["epoch_id"], completed_at=1001.0
                    )
                store.commit()

                self.assertTrue(competing_write_attempted)
                self.assertEqual(result["state"], "completed")
            finally:
                competitor.close()
                store.close()

    def test_dirty_generation_disambiguates_equal_wall_clock_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                started = time.time() + 1
                store.mark_ai_inventory_dirty(observed_at=started)
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=started,
                )
                store.finalize_ai_inventory_epoch(
                    epoch["epoch_id"], completed_at=started + 1
                )
                store.commit()
                self.assertTrue(
                    store.ai_inventory_coverage_summary(now=started + 2)["complete"]
                )

                store.mark_ai_inventory_dirty(observed_at=started + 1)
                store.commit()
                dirty = store.ai_inventory_coverage_summary(now=started + 2)
                self.assertEqual(dirty["state"], "inventory_dirty")
                self.assertFalse(dirty["complete"])
            finally:
                store.close()

    def test_read_time_coverage_rejects_deleted_exact_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            stat_result = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1"
                )
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=epoch["eligibility_bound"],
                )
                store.record_ai_inventory_observation(
                    epoch["epoch_id"], video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    classification="needs_ai",
                    disposition="delivery_required",
                )
                store.finalize_ai_inventory_epoch(epoch["epoch_id"])
                store.commit()
                self.assertTrue(store.ai_inventory_coverage_summary()["complete"])

                store._conn.execute(
                    "DELETE FROM ai_delivery_obligations WHERE obligation_id=?",
                    (obligation["obligation_id"],),
                )
                store.commit()
                coverage = store.ai_inventory_coverage_summary()
                self.assertEqual(coverage["inventory_tracked"], 0)
                self.assertEqual(coverage["inventory_untracked"], 1)
                self.assertFalse(coverage["complete"])
            finally:
                store.close()

    def test_read_time_coverage_rejects_mismatched_exact_obligation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            stat_result = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1"
                )
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=epoch["eligibility_bound"],
                )
                store.record_ai_inventory_observation(
                    epoch["epoch_id"], video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    classification="needs_ai",
                    disposition="delivery_required",
                )
                store.finalize_ai_inventory_epoch(epoch["epoch_id"])
                store._conn.execute(
                    "UPDATE ai_delivery_obligations SET media_size=media_size+1 "
                    "WHERE obligation_id=?",
                    (obligation["obligation_id"],),
                )
                store.commit()
                coverage = store.ai_inventory_coverage_summary()
                self.assertEqual(coverage["inventory_untracked"], 1)
                self.assertFalse(coverage["complete"])
            finally:
                store.close()

    def test_read_time_coverage_rejects_corrupted_strict_success_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            stat_result = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1"
                )
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=epoch["eligibility_bound"],
                )
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='succeeded', verified_at=?, terminal_at=?,
                        manifest_path='/work/manifest.json', manifest_sha256=?,
                        verification_json=?
                    WHERE obligation_id=?
                    """,
                    (
                        time.time(), time.time(), "a" * 64,
                        json.dumps(self._strict_verification("policy-v1")),
                        obligation["obligation_id"],
                    ),
                )
                store.record_ai_inventory_observation(
                    epoch["epoch_id"], video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    classification="finished",
                    disposition="delivered",
                    ai_output_detected=True,
                    ai_output_mtime=time.time(),
                )
                store.finalize_ai_inventory_epoch(epoch["epoch_id"])
                store.commit()
                self.assertTrue(store.ai_inventory_coverage_summary()["complete"])

                store._conn.execute(
                    "UPDATE ai_delivery_obligations SET verification_json='{}' "
                    "WHERE obligation_id=?",
                    (obligation["obligation_id"],),
                )
                store.commit()
                coverage = store.ai_inventory_coverage_summary()
                self.assertEqual(coverage["inventory_untracked"], 1)
                self.assertFalse(coverage["complete"])
            finally:
                store.close()

    def test_normal_strict_success_transition_does_not_dirty_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            stat_result = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1"
                )
                store.upsert_ai_queue_candidate(video, stat_result.st_mtime_ns)
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=epoch["eligibility_bound"],
                )
                store.record_ai_inventory_observation(
                    epoch["epoch_id"], video,
                    media_size=stat_result.st_size,
                    media_mtime_ns=stat_result.st_mtime_ns,
                    policy_revision="policy-v1",
                    classification="needs_ai",
                    disposition="delivery_required",
                )
                store.finalize_ai_inventory_epoch(epoch["epoch_id"])
                store.commit()
                generation_before = int(
                    dict(store._conn.execute("SELECT key, value FROM ai_delivery_meta"))["inventory_dirty_generation"]
                )

                store.mark_ai_delivery_verified(
                    obligation["obligation_id"],
                    manifest_path="/work/manifest.json",
                    manifest_sha256="a" * 64,
                    verification=self._strict_verification("policy-v1"),
                    evidence_verified=True,
                )
                store.mark_ai_queue_done(video)
                store.commit()

                generation_after = int(
                    dict(store._conn.execute("SELECT key, value FROM ai_delivery_meta"))["inventory_dirty_generation"]
                )
                self.assertEqual(generation_after, generation_before)
                self.assertTrue(store.ai_inventory_coverage_summary()["complete"])
            finally:
                store.close()

    def test_coverage_rejects_newer_failed_and_stale_running_epochs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                completed_at = time.time()
                epoch = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=completed_at - 1,
                )
                store.finalize_ai_inventory_epoch(
                    epoch["epoch_id"], completed_at=completed_at
                )
                failed = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=completed_at + 1,
                )
                store.fail_ai_inventory_epoch(
                    failed["epoch_id"], failure_code="walk_error",
                    failed_at=completed_at + 2,
                )
                store.commit()
                self.assertEqual(
                    store.ai_inventory_coverage_summary(now=completed_at + 3)["state"],
                    "inventory_failed",
                )

                store._conn.execute(
                    "DELETE FROM ai_inventory_epochs WHERE epoch_id=?",
                    (failed["epoch_id"],),
                )
                running = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=completed_at + 1,
                )
                store._conn.execute(
                    "UPDATE ai_inventory_epochs SET updated_at=? WHERE epoch_id=?",
                    (
                        completed_at - AI_INVENTORY_RUNNING_STALE_SECONDS - 1,
                        running["epoch_id"],
                    ),
                )
                store.commit()
                self.assertEqual(
                    store.ai_inventory_coverage_summary(now=completed_at + 3)["state"],
                    "inventory_running_stale",
                )
            finally:
                store.close()

    def test_continuous_coverage_stops_at_failed_epoch_and_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                base = time.time() + 10
                first = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=base,
                )
                store.finalize_ai_inventory_epoch(
                    first["epoch_id"], completed_at=base + 1
                )
                second = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=base + 100,
                )
                store.finalize_ai_inventory_epoch(
                    second["epoch_id"], completed_at=base + 101
                )
                store.commit()

                continuous = store.ai_inventory_continuous_coverage_summary(
                    now=base + 102
                )
                self.assertTrue(continuous["complete"])
                self.assertEqual(continuous["coverage_chain_epoch_count"], 2)
                self.assertEqual(continuous["continuous_coverage_since"], base + 1)
                self.assertEqual(continuous["coverage_complete_through"], base + 101)

                failed = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=base + 200,
                )
                store.fail_ai_inventory_epoch(
                    failed["epoch_id"], failure_code="walk_error",
                    failed_at=base + 201,
                )
                recovered = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=base + 300,
                )
                store.finalize_ai_inventory_epoch(
                    recovered["epoch_id"], completed_at=base + 301
                )
                store.commit()

                after_failure = store.ai_inventory_continuous_coverage_summary(
                    now=base + 302
                )
                self.assertTrue(after_failure["complete"])
                self.assertEqual(after_failure["coverage_chain_epoch_count"], 1)
                self.assertEqual(
                    after_failure["continuous_coverage_since"], base + 301
                )

                after_gap = base + 301 + AI_INVENTORY_MAX_AGE_SECONDS + 10
                newest = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1", root_signature="root-v1",
                    started_at=after_gap,
                )
                store.finalize_ai_inventory_epoch(
                    newest["epoch_id"], completed_at=after_gap + 1
                )
                store.commit()

                gapped = store.ai_inventory_continuous_coverage_summary(
                    now=after_gap + 2
                )
                self.assertTrue(gapped["complete"])
                self.assertEqual(gapped["coverage_chain_epoch_count"], 1)
                self.assertEqual(
                    gapped["continuous_coverage_since"], after_gap + 1
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
