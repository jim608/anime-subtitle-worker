from __future__ import annotations

import hashlib
import io
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from event_watcher import _FilesystemEventQueue
from pipeline_event_log import append_pipeline_event
from pipeline_state import (
    EXECUTION_PIPELINE_STATES,
    PipelineJobStore,
    PipelineStateError,
)
from source_integrity import capture_source_snapshot, verify_source_snapshot


_FORMAL_TEST_STAGES = (
    "ANALYZING",
    "SUBTITLE_DETECTION",
    "ASR",
    "TRANSLATING",
    "POST_PROCESSING",
    "QC",
    "MUXING",
)


class M1AcceptanceTest(unittest.TestCase):
    """Fast, explicit acceptance coverage for the twelve M0/M1 gates."""

    def _event_config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            video_extensions=[".mkv"],
            work_path=root,
            scanner_state_path=str(root / "scanner_state.sqlite3"),
            scanner_skip_standalone_op_ed=True,
        )

    def _event_queue(
        self,
        root: Path,
        *,
        probe,
    ) -> _FilesystemEventQueue:
        return _FilesystemEventQueue(
            self._event_config(root),
            logging.getLogger("test_m1_acceptance.watcher"),
            quiet_window_seconds=0,
            stable_observations_required=2,
            file_complete_probe=probe,
        )

    def _observe_job(
        self,
        store: PipelineJobStore,
        media: Path,
        *,
        state: str = "QUEUED",
    ) -> dict:
        stat = media.stat()
        observation = store.observe_ingest(
            media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type="closed",
            state=state,
            evidence={"acceptance": True},
            confidence=1.0,
        )
        job = store.get_job(str(observation["job_id"]))
        self.assertIsNotNone(job)
        return dict(job or {})

    def _run_existing_case(self, case_type: type[unittest.TestCase], name: str) -> None:
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(
            unittest.TestSuite([case_type(name)])
        )
        self.assertFalse(result.skipped, stream.getvalue())
        self.assertTrue(result.wasSuccessful(), stream.getvalue())

    def test_01_one_complete_watch_video_creates_one_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E01.mkv"
            media.write_bytes(b"complete-video")
            queue = self._event_queue(root, probe=lambda _path: True)
            queue.submit(media, event_type="closed")

            queue._write_batch([media])
            queue._write_batch([media])

            connection = sqlite3.connect(root / "scanner_state.sqlite3")
            try:
                jobs = connection.execute(
                    "SELECT job_id, state FROM pipeline_jobs"
                ).fetchall()
                queued = connection.execute(
                    "SELECT COUNT(*) FROM ai_candidate_queue WHERE path=? AND status='queued'",
                    (str(media.resolve()),),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(1, len(jobs))
            self.assertEqual("QUEUED", jobs[0][1])
            self.assertEqual(1, queued)

    def test_02_duplicate_filesystem_events_keep_one_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E02.mkv"
            media.write_bytes(b"same-media-revision")
            database = root / "scanner_state.sqlite3"
            store = PipelineJobStore(database)
            try:
                job_ids: set[str] = set()
                for event_type in ("created", "modified", "closed", "modified", "moved"):
                    stat = media.stat()
                    observation = store.observe_ingest(
                        media,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                        event_type=event_type,
                        state="QUEUED",
                        evidence={"event_type": event_type},
                        confidence=1.0,
                    )
                    job_ids.add(str(observation["job_id"]))
                count = store._conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0]
                observation_count = store._conn.execute(
                    "SELECT observation_count FROM pipeline_ingest_observations"
                ).fetchone()[0]
            finally:
                store.close()
            self.assertEqual(1, len(job_ids))
            self.assertEqual(1, count)
            self.assertEqual(5, observation_count)

    def test_03_incomplete_write_is_not_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E03.mkv"
            media.write_bytes(b"still-being-written")
            queue = self._event_queue(root, probe=lambda _path: False)
            queue.submit(media, event_type="modified")

            queue._write_batch([media])
            queue._write_batch([media])

            connection = sqlite3.connect(root / "scanner_state.sqlite3")
            try:
                state = connection.execute("SELECT state FROM pipeline_jobs").fetchone()[0]
                analyzed = connection.execute(
                    "SELECT COUNT(*) FROM pipeline_job_transitions WHERE to_state='ANALYZING'"
                ).fetchone()[0]
                queued = connection.execute(
                    "SELECT COUNT(*) FROM ai_candidate_queue WHERE path=?",
                    (str(media.resolve()),),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual("STABILIZING", state)
            self.assertEqual(0, analyzed)
            self.assertEqual(0, queued)

    def test_04_stable_file_starts_automatically(self) -> None:
        from test_event_watcher import FilesystemEventQueueTest

        self._run_existing_case(
            FilesystemEventQueueTest,
            "test_debounces_duplicate_video_events_into_one_queue_row",
        )

    def test_05_crash_at_every_formal_stage_resumes(self) -> None:
        self.assertEqual(set(_FORMAL_TEST_STAGES), set(EXECUTION_PIPELINE_STATES))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E05.mkv"
            media.write_bytes(b"formal-stage-recovery")
            database = root / "scanner_state.sqlite3"
            store = PipelineJobStore(database)
            job = self._observe_job(store, media, state="STABILIZING")
            try:
                for index, stage in enumerate(_FORMAL_TEST_STAGES, start=1):
                    inputs = {"stage": stage, "media_revision": job["media_revision"]}
                    attempt = store.start_stage_attempt(
                        job["job_id"],
                        stage,
                        inputs=inputs,
                        retry_limit=1,
                        timeout_seconds=30,
                        checkpoint={"unit": index},
                        reason_code="acceptance_stage_started",
                        evidence={"crash_injection": stage},
                        confidence=1.0,
                    )
                    store.checkpoint_stage(
                        attempt["stage_attempt_id"],
                        {"unit": index, "durable": True},
                        reason_code="acceptance_checkpoint",
                        evidence={"stage": stage},
                        confidence=1.0,
                    )
                    store.commit()
                    store.close()

                    store = PipelineJobStore(database)
                    recovered = store.recover_interrupted_stages(recover_all_running=True)
                    self.assertEqual(1, len(recovered), stage)
                    self.assertEqual(stage, recovered[0]["stage"])
                    self.assertEqual(
                        {"unit": index, "durable": True},
                        recovered[0]["checkpoint"],
                    )
                    current = store.get_job(job["job_id"])
                    self.assertEqual("RETRYING", current["state"])
                    self.assertEqual(stage, current["resume_state"])

                    resumed = store.start_stage_attempt(
                        job["job_id"],
                        stage,
                        inputs=inputs,
                        retry_limit=1,
                        checkpoint=recovered[0]["checkpoint"],
                        reason_code="acceptance_stage_resumed",
                        evidence={"recovered_attempt": attempt["stage_attempt_id"]},
                        confidence=1.0,
                    )
                    store.finish_stage_attempt(
                        resumed["stage_attempt_id"],
                        "SUCCEEDED",
                        outputs={
                            "no_artifact_required": True,
                            "checkpoint_evidence": {"stage": stage},
                        },
                        outputs_verified=True,
                        reason_code="acceptance_stage_finished",
                        evidence={"resumed": True},
                        confidence=1.0,
                    )
                    store.commit()

                for stage in _FORMAL_TEST_STAGES:
                    attempts = store.list_stage_attempts(job["job_id"], stage)
                    self.assertEqual(
                        ["INTERRUPTED", "SUCCEEDED"],
                        [attempt["status"] for attempt in attempts],
                        stage,
                    )
            finally:
                store.close()

    def test_06_durable_volume_reopen_resumes_unfinished_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E06.mkv"
            media.write_bytes(b"docker-volume-semantics")
            database = root / "scanner_state.sqlite3"
            store = PipelineJobStore(database)
            job = self._observe_job(store, media)
            inputs = {"media_revision": job["media_revision"]}
            attempt = store.start_stage_attempt(
                job["job_id"],
                "ASR",
                inputs=inputs,
                model={"adapter": "acceptance", "name": "whisper"},
                retry_limit=1,
                checkpoint={"segment": 17},
                reason_code="asr_started",
                evidence={"restart_test": True},
                confidence=1.0,
            )
            store.commit()
            store.close()

            restarted = PipelineJobStore(database)
            recovered = restarted.recover_interrupted_stages(recover_all_running=True)
            restarted.commit()
            restarted.close()

            after_second_open = PipelineJobStore(database)
            try:
                current = after_second_open.get_job(job["job_id"])
                interrupted = after_second_open._get_attempt(attempt["stage_attempt_id"])
                journal_mode = after_second_open._conn.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
                self.assertEqual(1, len(recovered))
                self.assertEqual({"segment": 17}, recovered[0]["checkpoint"])
                self.assertEqual("RETRYING", current["state"])
                self.assertEqual("ASR", current["resume_state"])
                self.assertEqual("INTERRUPTED", interrupted["status"])
                self.assertEqual("wal", str(journal_mode).casefold())

                resumed = after_second_open.start_stage_attempt(
                    job["job_id"],
                    "ASR",
                    inputs=inputs,
                    model={"adapter": "acceptance", "name": "whisper"},
                    retry_limit=1,
                    checkpoint=recovered[0]["checkpoint"],
                    reason_code="asr_resumed_after_volume_reopen",
                    evidence={"previous_attempt": attempt["stage_attempt_id"]},
                    confidence=1.0,
                )
                after_second_open.finish_stage_attempt(
                    resumed["stage_attempt_id"],
                    "SUCCEEDED",
                    outputs={
                        "no_artifact_required": True,
                        "checkpoint_evidence": {"segment": 17},
                    },
                    outputs_verified=True,
                    reason_code="asr_resume_completed",
                    evidence={"resumed": True},
                    confidence=1.0,
                )
                self.assertEqual(2, resumed["attempt_number"])
                self.assertEqual("ASR", after_second_open.get_job(job["job_id"])["state"])
            finally:
                after_second_open.close()

    def test_07_valid_completed_stage_is_reused_not_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E07.mkv"
            media.write_bytes(b"reuse-stage")
            artifact = root / "Anime - S01E07.zh-TW.srt"
            artifact.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
            database = root / "scanner_state.sqlite3"
            inputs = {"source": "stable-input"}
            store = PipelineJobStore(database)
            job = self._observe_job(store, media)
            attempt = store.start_stage_attempt(
                job["job_id"],
                "TRANSLATING",
                inputs=inputs,
                reason_code="translation_started",
                evidence={},
                confidence=1.0,
            )
            store.finish_stage_attempt(
                attempt["stage_attempt_id"],
                "SUCCEEDED",
                outputs={
                    "artifacts": [
                        {
                            "path": str(artifact),
                            "size": artifact.stat().st_size,
                            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        }
                    ]
                },
                outputs_verified=True,
                reason_code="translation_validated",
                evidence={"parser": "ok"},
                confidence=1.0,
            )
            store.commit()
            store.close()

            restarted = PipelineJobStore(database)
            try:
                reusable = restarted.reusable_stage_attempt(
                    job["job_id"], "TRANSLATING", inputs=inputs
                )
                self.assertIsNotNone(reusable)
                self.assertEqual(attempt["stage_attempt_id"], reusable["stage_attempt_id"])
                self.assertEqual(
                    1,
                    len(restarted.list_stage_attempts(job["job_id"], "TRANSLATING")),
                )
            finally:
                restarted.close()

    def test_08_temporary_output_cannot_complete_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E08.mkv"
            media.write_bytes(b"temporary-publication")
            store = PipelineJobStore(root / "scanner_state.sqlite3")
            try:
                job = self._observe_job(store, media)
                store.transition_legacy_stage(job["job_id"], "complete", "complete")
                temporary_manifest = root / "delivery.manifest.json.tmp"
                temporary_manifest.write_text("{}", encoding="utf-8")
                with self.assertRaises(PipelineStateError):
                    store.complete_job(
                        job["job_id"],
                        delivery_evidence={
                            "manifest_path": str(temporary_manifest),
                            "manifest_sha256": hashlib.sha256(
                                temporary_manifest.read_bytes()
                            ).hexdigest(),
                            "verification": {
                                "required_outputs_complete": True,
                                "hashes_verified": True,
                                "publication_marker_absent": True,
                                "media_identity_matched": True,
                            },
                        },
                    )
                self.assertNotEqual("COMPLETED", store.get_job(job["job_id"])["state"])
            finally:
                store.close()

    def test_09_source_checksum_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E09.mkv"
            media.write_bytes(b"source-must-remain-read-only")
            before = capture_source_snapshot(media, hash_content=True)
            store = PipelineJobStore(root / "scanner_state.sqlite3")
            try:
                job = self._observe_job(store, media)
                attempt = store.start_stage_attempt(
                    job["job_id"],
                    "ASR",
                    inputs={"media_revision": job["media_revision"]},
                    checkpoint={"segment": 1},
                    reason_code="read_only_stage",
                    evidence={},
                    confidence=1.0,
                )
                store.checkpoint_stage(
                    attempt["stage_attempt_id"],
                    {"segment": 2},
                    reason_code="read_only_checkpoint",
                    evidence={},
                    confidence=1.0,
                )
                store.commit()
            finally:
                store.close()
            evidence = verify_source_snapshot(before, hash_content=True)
            self.assertTrue(evidence["verified"])
            self.assertEqual(before.sha256, evidence["sha256"])

    def test_10_idle_watchers_do_not_repeat_full_recursive_walks(self) -> None:
        from test_main_queue import MainQueueResultTest
        from test_mikan_worker import MikanWorkerPendingTest

        self._run_existing_case(
            MainQueueResultTest,
            "test_background_ai_scan_uses_delayed_low_frequency_reconciliation_with_event_watcher",
        )
        self._run_existing_case(
            MainQueueResultTest,
            "test_background_ai_scan_incomplete_batch_uses_bounded_cooldown_not_hot_loop",
        )
        self._run_existing_case(
            MikanWorkerPendingTest,
            "test_library_scan_plan_bounds_and_cools_index_unavailable_fallback",
        )
        self._run_existing_case(
            MikanWorkerPendingTest,
            "test_indexed_missing_episode_scan_never_walks_series_roots",
        )

    def test_11_existing_normal_subtitle_pipeline_completes(self) -> None:
        from test_source_priority import SubtitleSourcePriorityTest

        self._run_existing_case(
            SubtitleSourcePriorityTest,
            "test_english_sidecar_translation_uses_no_audio_or_asr_and_has_strict_manifest",
        )

    def test_12_transitions_and_retries_are_auditable_in_db_and_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "Anime - S01E12.mkv"
            media.write_bytes(b"audit-evidence")
            store = PipelineJobStore(root / "scanner_state.sqlite3")
            try:
                job = self._observe_job(store, media)
                attempt = store.start_stage_attempt(
                    job["job_id"],
                    "ASR",
                    inputs={"media_revision": job["media_revision"]},
                    model={"adapter": "acceptance", "name": "whisper"},
                    retry_limit=1,
                    timeout_seconds=30,
                    checkpoint={"segment": 0},
                    reason_code="asr_started",
                    evidence={"device": "test"},
                    confidence=0.99,
                )
                store.checkpoint_stage(
                    attempt["stage_attempt_id"],
                    {"segment": 3},
                    reason_code="asr_checkpoint",
                    evidence={"segments_saved": 3},
                    confidence=0.99,
                )
                store.finish_stage_attempt(
                    attempt["stage_attempt_id"],
                    "RETRYABLE_FAILURE",
                    error_class="transient",
                    error_code="model_timeout",
                    error={"timeout_seconds": 30},
                    retry_after_seconds=5,
                    reason_code="asr_timeout_retry",
                    evidence={"fallback": "bounded_retry"},
                    confidence=1.0,
                )
                store.commit()

                transitions = store.list_transitions(job["job_id"])
                retry_transition = next(
                    row for row in transitions if row["to_state"] == "RETRYING"
                )
                persisted_attempt = store._get_attempt(attempt["stage_attempt_id"])
                stage_events = store._conn.execute(
                    "SELECT event_type FROM pipeline_stage_events "
                    "WHERE stage_attempt_id=? ORDER BY created_at",
                    (attempt["stage_attempt_id"],),
                ).fetchall()
                self.assertEqual("asr_timeout_retry", retry_transition["reason_code"])
                self.assertEqual("model_timeout", persisted_attempt["error_code"])
                self.assertEqual(
                    ["STARTED", "CHECKPOINTED", "FINISHED"],
                    [row[0] for row in stage_events],
                )

                log_path = append_pipeline_event(
                    root / "logs",
                    "state_transition",
                    job_id=job["job_id"],
                    media_revision=job["media_revision"],
                    state=retry_transition["to_state"],
                    stage="ASR",
                    attempt=persisted_attempt["attempt_number"],
                    reason_code=retry_transition["reason_code"],
                    confidence=retry_transition["confidence"],
                    evidence=retry_transition["evidence"],
                )
            finally:
                store.close()

            rows = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(1, len(rows))
            self.assertEqual("RETRYING", rows[0]["state"])
            self.assertEqual("asr_timeout_retry", rows[0]["reason_code"])
            self.assertEqual("transient", rows[0]["evidence"]["error_class"])
            self.assertEqual("model_timeout", rows[0]["evidence"]["error_code"])


if __name__ == "__main__":
    unittest.main()
