from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio import AudioStreamInfo
from main import _ai_failure_policy
from output_manifest import validate_output_manifest
from scan_state import ScanStateStore
from source_analyzer import AnalyzerThresholds
from test_source_decision_adapter import _write_complete_ass
from test_worker import _config, _logger
from worker import (
    SourceSelectionReviewError,
    UnsupportedSourceError,
    VideoWorker,
)


class M2WorkerIntegrationTest(unittest.TestCase):
    def _config(self, root: Path):
        config = _config(
            root,
            source_analyzer_enabled=True,
            pipeline_job_store_required=True,
            scanner_cache_enabled=True,
            scanner_queue_enabled=True,
            scanner_state_path=root / "scanner-state.sqlite3",
            ai_output_manifest_path=root / "manifests",
            log_path=root / "logs",
            mikan_remove_ai_after_extract=False,
            source_analyzer_version="m2-source-analyzer-v1",
            source_decision_schema_version=1,
            source_decision_version="m2-source-decision-v1",
            subtitle_extract_timeout_seconds=30,
        )
        config.source_analyzer_thresholds = lambda: AnalyzerThresholds()
        return config

    def test_real_sidecar_is_analyzed_persisted_and_adopted_without_whisper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-video")
            source = root / "episode.zh-TW.ass"
            _write_complete_ass(source)
            source_before = (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns)
            config = self._config(root)
            worker = VideoWorker(config, _logger())
            probe = {"format": {"duration": "100"}, "streams": []}

            with (
                patch("source_inventory._probe_media", return_value=probe),
                patch.object(worker, "_extract_preferred_audio") as extract_audio,
                patch.object(worker, "_detect_source_language") as detect_language,
                patch.object(worker, "_transcribe") as transcribe,
                patch.object(worker, "_get_translator") as translator,
            ):
                outcome = worker._process_locked(
                    video,
                    root / "audio.wav",
                    root / "vocals.wav",
                )

            self.assertEqual(("complete", "ok"), (outcome.stage, outcome.status))
            extract_audio.assert_not_called()
            detect_language.assert_not_called()
            transcribe.assert_not_called()
            translator.assert_not_called()
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    require_publication_semantics=True,
                )
            )
            self.assertEqual(
                source_before,
                (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns),
            )

            state = ScanStateStore.from_config(config)
            try:
                pipeline = state.pipeline_jobs()
                job = pipeline.job_for_path(
                    video,
                    size=video.stat().st_size,
                    mtime_ns=video.stat().st_mtime_ns,
                    create=False,
                )
                assert isinstance(job, dict)
                decisions = pipeline.list_source_decisions(str(job["job_id"]))
                self.assertEqual(1, len(decisions))
                self.assertEqual("USE_EXISTING_ZH_TW", decisions[0]["strategy"])
                self.assertTrue(decisions[0]["integrity_valid"])
                subtitle_attempts = pipeline.list_stage_attempts(
                    str(job["job_id"]), "SUBTITLE_DETECTION"
                )
                qc_attempts = pipeline.list_stage_attempts(str(job["job_id"]), "QC")
                self.assertEqual(["SUCCEEDED"], [item["status"] for item in subtitle_attempts])
                self.assertEqual(["SUCCEEDED"], [item["status"] for item in qc_attempts])
                self.assertEqual("", str(job.get("active_stage_attempt_id") or ""))
            finally:
                state.close()

    def test_retry_from_asr_reenters_source_detection_before_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-video")
            source = root / "episode.zh-TW.ass"
            _write_complete_ass(source)
            source_before = (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns)
            config = self._config(root)

            state = ScanStateStore.from_config(config)
            try:
                pipeline = state.pipeline_jobs()
                stat = video.stat()
                job = pipeline.job_for_path(
                    video,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    create=True,
                    initial_state="QUEUED",
                    reason_code="m2_retry_regression_fixture",
                    evidence={"test": "m2_retry_from_asr"},
                    confidence=1.0,
                )
                assert isinstance(job, dict)
                asr_attempt = pipeline.start_stage_attempt(
                    str(job["job_id"]),
                    "ASR",
                    inputs={"fixture": "advanced_before_m2"},
                    retry_limit=2,
                    reason_code="m2_retry_regression_fixture",
                    evidence={"test": "m2_retry_from_asr"},
                    confidence=1.0,
                )
                pipeline.finish_stage_attempt(
                    str(asr_attempt["stage_attempt_id"]),
                    "NEEDS_REVIEW",
                    error_class="quality",
                    error_code="legacy_asr_review",
                    reason_code="m2_retry_regression_fixture",
                    evidence={"test": "m2_retry_from_asr"},
                    confidence=1.0,
                )
                state.commit()
            finally:
                state.close()

            worker = VideoWorker(config, _logger())
            probe = {"format": {"duration": "100"}, "streams": []}
            with (
                patch("source_inventory._probe_media", return_value=probe),
                patch.object(worker, "_extract_preferred_audio") as extract_audio,
                patch.object(worker, "_detect_source_language") as detect_language,
                patch.object(worker, "_transcribe") as transcribe,
            ):
                outcome = worker._process_locked(
                    video,
                    root / "audio.wav",
                    root / "vocals.wav",
                )

            self.assertEqual(("complete", "ok"), (outcome.stage, outcome.status))
            extract_audio.assert_not_called()
            detect_language.assert_not_called()
            transcribe.assert_not_called()
            self.assertEqual(
                source_before,
                (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns),
            )

            state = ScanStateStore.from_config(config)
            try:
                pipeline = state.pipeline_jobs()
                job = pipeline.job_for_path(
                    video,
                    size=video.stat().st_size,
                    mtime_ns=video.stat().st_mtime_ns,
                    create=False,
                )
                assert isinstance(job, dict)
                attempts = pipeline.list_stage_attempts(
                    str(job["job_id"]), "SUBTITLE_DETECTION"
                )
                self.assertEqual(["SUCCEEDED"], [item["status"] for item in attempts])
                self.assertEqual("QC", job["state"])
                self.assertEqual("", str(job.get("active_stage_attempt_id") or ""))
            finally:
                state.close()

    def test_preselected_m2_audio_is_used_without_metadata_reselection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            worker = VideoWorker(_config(root), _logger())
            selected = AudioStreamInfo(5, "ja", "Japanese Main", True, False, "aac", 2)
            worker._selected_audio_stream = selected
            destination = root / "audio.wav"
            with (
                patch("worker.preferred_audio_stream_info") as preferred,
                patch("worker.extract_audio", return_value=destination) as extract,
                patch("worker.probe_audio_streams", return_value=[selected]),
                patch.object(worker, "_set_stage"),
                patch.object(worker, "_write_audio_selection_manifest"),
            ):
                worker._extract_preferred_audio(video, destination)
            preferred.assert_not_called()
            extract.assert_called_once_with(video, destination, stream_index=5)

    def test_source_selection_failures_have_explicit_terminal_policy(self) -> None:
        self.assertEqual(
            "source_selection_review",
            VideoWorker._stage_for_exception(SourceSelectionReviewError("ambiguous")),
        )
        self.assertEqual(
            "source_selection_unsupported",
            VideoWorker._stage_for_exception(UnsupportedSourceError("unsupported")),
        )
        self.assertEqual(
            ("source_unsupported", "permanent"),
            _ai_failure_policy("source_selection_unsupported", "[source_unsupported] none"),
        )
        self.assertEqual(
            ("source_selection_needs_review", "manual_review"),
            _ai_failure_policy("source_selection_review", "ambiguous"),
        )

    def test_low_confidence_inventory_enters_durable_review_without_whisper(self) -> None:
        probe = {
            "format": {"duration": "100"},
            "streams": [
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "duration": "100",
                    "tags": {"language": "und", "title": ""},
                    "disposition": {"default": 1, "comment": 0},
                }
            ],
        }
        self._assert_failed_source_decision_state(
            probe,
            expected_job_state="NEEDS_REVIEW",
            expected_attempt_status="NEEDS_REVIEW",
        )

    def test_no_supported_inventory_enters_durable_permanent_failure(self) -> None:
        probe = {"format": {"duration": "100"}, "streams": []}
        self._assert_failed_source_decision_state(
            probe,
            expected_job_state="FAILED",
            expected_attempt_status="PERMANENT_FAILURE",
        )

    def _assert_failed_source_decision_state(
        self,
        probe: dict[str, object],
        *,
        expected_job_state: str,
        expected_attempt_status: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"immutable-video")
            source_before = (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns)
            config = self._config(root)
            worker = VideoWorker(config, _logger())
            with (
                patch("source_inventory._probe_media", return_value=probe),
                patch.object(worker, "_transcribe") as transcribe,
                patch("worker.notify_event"),
            ):
                self.assertFalse(worker.process(video))
            transcribe.assert_not_called()
            self.assertEqual(
                source_before,
                (video.read_bytes(), video.stat().st_size, video.stat().st_mtime_ns),
            )

            state = ScanStateStore.from_config(config)
            try:
                pipeline = state.pipeline_jobs()
                job = pipeline.job_for_path(
                    video,
                    size=video.stat().st_size,
                    mtime_ns=video.stat().st_mtime_ns,
                    create=False,
                )
                assert isinstance(job, dict)
                self.assertEqual(expected_job_state, job["state"])
                attempts = pipeline.list_stage_attempts(
                    str(job["job_id"]), "SUBTITLE_DETECTION"
                )
                self.assertEqual(expected_attempt_status, attempts[-1]["status"])
                self.assertEqual(1, len(pipeline.list_source_decisions(str(job["job_id"]))))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
