from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main as main_module
from asr_review_checkpoint import (
    AsrReviewCheckpoint,
    create_asr_review_checkpoint,
    load_asr_review_checkpoint,
)
from safe_files import sha256_file, verified_copy_replace
from srt_utils import SrtBlock, read_srt, write_srt
from subtitle_paths import paths_for_video
from test_worker import _config, _logger
from transcriber import (
    LowConfidenceTranscriptionError,
    asr_diagnostics_path,
    asr_transcription_hold_path,
)
from worker import VideoWorker


class AsrReviewIntegrationTest(unittest.TestCase):
    def test_worker_preserves_ja_checkpoint_before_fail_closed_and_orders_candidates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "anime"
            video = input_root / "Example" / "Season 1" / "Example - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                input_path=input_root,
                video_extensions=(".mkv",),
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=True,
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)

            def reject_with_trusted_evidence(_audio: Path, target: Path) -> None:
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:08,000 --> 00:00:14,000",
                            ["rejected Japanese transcript"],
                        )
                    ],
                )
                diagnostics = asr_diagnostics_path(target, config)
                diagnostics.parent.mkdir(parents=True, exist_ok=True)
                diagnostics.write_text(
                    json.dumps(
                        {
                            "status": "selective_retry_required",
                            "reason_code": "low_confidence",
                            "srt_path": str(target),
                            "srt_sha256": sha256_file(target),
                            "review_ranges": [[8.0, 14.0]],
                        }
                    ),
                    encoding="utf-8",
                )
                raise LowConfidenceTranscriptionError(
                    "bounded Japanese ASR rejection",
                    [(8.0, 14.0)],
                    reason_code="low_confidence",
                )

            worker._active_transcription_video = video
            try:
                with (
                    patch.object(
                        worker,
                        "_transcribe_with_fallback",
                        side_effect=reject_with_trusted_evidence,
                    ),
                    self.assertRaises(LowConfidenceTranscriptionError) as caught,
                ):
                    worker._transcribe(audio, paths.ja_srt)
            finally:
                worker._active_transcription_video = None

            self.assertFalse(paths.ja_srt.exists())
            self.assertFalse(asr_diagnostics_path(paths.ja_srt, config).exists())
            self.assertFalse(asr_transcription_hold_path(paths.ja_srt, config).exists())

            context = worker._asr_review_context(video, caught.exception)
            checkpoint_evidence = context["asr_review_checkpoint"]
            checkpoint = load_asr_review_checkpoint(
                checkpoint_evidence["manifest_path"],
                expected_manifest_sha256=checkpoint_evidence["manifest_sha256"],
                expected_checkpoint_id=checkpoint_evidence["checkpoint_id"],
                expected_target_path=paths.ja_srt,
                expected_language="ja",
            )
            self.assertEqual(checkpoint.review_ranges, ((8.0, 14.0),))
            self.assertEqual(
                read_srt(checkpoint.rejected_srt_path),
                [
                    SrtBlock(
                        1,
                        "00:00:08,000 --> 00:00:14,000",
                        ["rejected Japanese transcript"],
                    )
                ],
            )

            candidates = worker._asr_review_candidates(context)
            self.assertEqual(
                [candidate["action"] for candidate in candidates],
                ["ai.retry_selective_asr", "ai.retranscribe"],
            )
            self.assertTrue(candidates[0]["selective"])
            self.assertFalse(candidates[1]["selective"])

    def test_incomplete_or_tampered_checkpoint_degrades_to_full_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, checkpoint, review = self._checkpoint_review(root)
            worker = VideoWorker(config, _logger())

            incomplete_context = {
                "reason_code": "low_confidence",
                "asr_review_checkpoint": {
                    "selective_retry_supported": True,
                    "repair_attempted": False,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "manifest_sha256": "missing",
                    "repair_fingerprint": checkpoint.repair_fingerprint,
                },
            }
            self.assertEqual(
                [
                    candidate["action"]
                    for candidate in worker._asr_review_candidates(incomplete_context)
                ],
                ["ai.retranscribe"],
            )

            checkpoint.rejected_srt_path.write_text("tampered", encoding="utf-8")
            self.assertIsNone(
                main_module._selective_asr_review_evidence(config, review, video)
            )

            state = Mock()
            state.active_review_remediation_count.return_value = 0
            state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "failure-current",
                "attempts": 1,
                "last_error_code": "deterministic_asr_quality",
            }
            enqueue = Mock(
                return_value={"command_id": "cmd_full", "status": "queued"}
            )
            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(
                    main_module,
                    "_review_autopilot_interval_elapsed",
                    return_value=True,
                ),
                patch(
                    "control_state.list_open_review_autopilot_candidates",
                    return_value=[review],
                ),
                patch(
                    "control_state.review_autopilot_revision_attempt_allowed",
                    return_value=True,
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(
                    config,
                    _logger(),
                )

            self.assertTrue(queued)
            self.assertEqual(
                enqueue.call_args.kwargs["parameters"]["remediation"],
                "ai.retranscribe",
            )
            self.assertEqual(
                enqueue.call_args.kwargs["parameters"]["policy_revision"],
                main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
            )

    def test_main_loader_and_queue_executor_roll_back_stale_then_restore_and_queue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, checkpoint, review = self._checkpoint_review(root)
            evidence = main_module._selective_asr_review_evidence(
                config,
                review,
                video,
            )
            self.assertIsNotNone(evidence)
            evidence_revision = str(evidence["evidence_revision"])
            target_srt = Path(checkpoint.target_path)
            target_diagnostics = asr_diagnostics_path(target_srt, config)
            self.assertFalse(target_srt.exists())
            self.assertFalse(target_diagnostics.exists())

            before = self._file_snapshot(root)
            stale_state = Mock()
            stale_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "failure-current",
            }
            with (
                patch(
                    "scan_state.ScanStateStore.from_config",
                    return_value=stale_state,
                ),
                self.assertRaisesRegex(ValueError, "paused failure revision"),
            ):
                main_module._queue_selective_asr_review_command(
                    config,
                    video,
                    review=review,
                    expected_failure_revision="failure-stale",
                    expected_review_evidence_revision=evidence_revision,
                    policy_revision=main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                )

            self.assertEqual(self._file_snapshot(root), before)
            self.assertFalse(target_srt.exists())
            self.assertFalse(target_diagnostics.exists())
            stale_state.queue_paused_review_remediation.assert_not_called()
            stale_state.commit.assert_not_called()
            stale_state.rollback.assert_called_once()
            stale_state.close.assert_called_once()

            success_state = Mock()
            success_state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": "failure-current",
            }
            success_state.queue_paused_review_remediation.return_value = True
            with patch(
                "scan_state.ScanStateStore.from_config",
                return_value=success_state,
            ):
                output = main_module._queue_selective_asr_review_command(
                    config,
                    video,
                    review=review,
                    expected_failure_revision="failure-current",
                    expected_review_evidence_revision=evidence_revision,
                    policy_revision=main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                )

            self.assertIn(checkpoint.checkpoint_id, output)
            self.assertEqual(
                target_srt.read_bytes(),
                checkpoint.rejected_srt_path.read_bytes(),
            )
            self.assertEqual(
                target_diagnostics.read_bytes(),
                checkpoint.diagnostics_path.read_bytes(),
            )
            success_state.queue_paused_review_remediation.assert_called_once_with(
                video,
                expected_failure_revision="failure-current",
                policy_revision=main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
            )
            success_state.commit.assert_called_once()
            success_state.rollback.assert_not_called()
            success_state.close.assert_called_once()
            self.assertFalse(video.with_name(f"{video.name}.lock").exists())

    def test_executor_copies_diagnostics_first_and_second_copy_failure_leaves_no_srt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, checkpoint, review = self._checkpoint_review(root)
            evidence = main_module._selective_asr_review_evidence(
                config,
                review,
                video,
            )
            self.assertIsNotNone(evidence)
            target_srt = Path(checkpoint.target_path)
            target_diagnostics = asr_diagnostics_path(target_srt, config)
            copies: list[tuple[Path, Path]] = []

            def fail_second_copy(source: Path, destination: Path) -> Path:
                copies.append((Path(source), Path(destination)))
                if len(copies) == 1:
                    return verified_copy_replace(source, destination)
                raise OSError("injected rejected-SRT restore failure")

            state_factory = Mock()
            with (
                patch(
                    "safe_files.verified_copy_replace",
                    side_effect=fail_second_copy,
                ),
                patch(
                    "scan_state.ScanStateStore.from_config",
                    state_factory,
                ),
                self.assertRaisesRegex(OSError, "rejected-SRT restore failure"),
            ):
                main_module._queue_selective_asr_review_command(
                    config,
                    video,
                    review=review,
                    expected_failure_revision="failure-current",
                    expected_review_evidence_revision=str(
                        evidence["evidence_revision"]
                    ),
                    policy_revision=main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                )

            self.assertEqual(
                [destination for _source, destination in copies],
                [target_diagnostics, target_srt],
            )
            self.assertFalse(target_srt.exists())
            self.assertFalse(target_diagnostics.exists())
            state_factory.assert_not_called()

    def test_selective_autopilot_key_is_failure_revision_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, _checkpoint, review = self._checkpoint_review(root)
            failure_revision = "failure-revision-42"
            state = Mock()
            state.active_review_remediation_count.return_value = 0
            state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": failure_revision,
                "attempts": 1,
            }
            enqueue = Mock(
                return_value={
                    "command_id": "cmd_selective",
                    "status": "queued",
                }
            )
            selective_prefix = main_module._review_autopilot_prefix(
                main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                "review.resolve_ai",
            )

            def interval_elapsed(_config, **kwargs) -> bool:
                return kwargs["idempotency_prefix"] == selective_prefix

            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(
                    main_module,
                    "_review_autopilot_interval_elapsed",
                    side_effect=interval_elapsed,
                ),
                patch(
                    "control_state.list_open_review_autopilot_candidates",
                    return_value=[review],
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(
                    config,
                    _logger(),
                )

            self.assertTrue(queued)
            enqueue_kwargs = enqueue.call_args.kwargs
            self.assertEqual(
                enqueue_kwargs["idempotency_key"],
                f"{selective_prefix}{review['review_id']}:{failure_revision}",
            )
            self.assertNotIn("retry_failed", enqueue_kwargs)
            self.assertEqual(
                enqueue_kwargs["parameters"]["expected_failure_revision"],
                failure_revision,
            )
            self.assertEqual(
                enqueue_kwargs["parameters"]["remediation"],
                "ai.retry_selective_asr",
            )
            state.close.assert_called_once()

    def test_failed_selective_command_falls_through_to_bounded_full_asr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, _checkpoint, review = self._checkpoint_review(root)
            failure_revision = "failure-revision-43"
            state = Mock()
            state.active_review_remediation_count.return_value = 0
            state.ai_queue_candidate_snapshot.return_value = {
                "status": "paused",
                "failure_revision": failure_revision,
                "attempts": 1,
                "last_error_code": "deterministic_asr_quality",
            }
            enqueue = Mock(
                side_effect=[
                    {"command_id": "cmd_selective", "status": "failed"},
                    {"command_id": "cmd_full", "status": "queued"},
                ]
            )
            selective_prefix = main_module._review_autopilot_prefix(
                main_module._AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                "review.resolve_ai",
            )
            full_prefix = main_module._review_autopilot_prefix(
                main_module._AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
                "review.resolve_ai",
            )

            def interval_elapsed(_config, **kwargs) -> bool:
                return kwargs["idempotency_prefix"] in {
                    selective_prefix,
                    full_prefix,
                }

            with (
                patch.object(main_module, "_ai_queue_paused", return_value=False),
                patch.object(
                    main_module,
                    "_review_autopilot_interval_elapsed",
                    side_effect=interval_elapsed,
                ),
                patch(
                    "control_state.list_open_review_autopilot_candidates",
                    return_value=[review],
                ),
                patch(
                    "control_state.review_autopilot_revision_attempt_allowed",
                    return_value=True,
                ),
                patch("control_state.enqueue_command", enqueue),
                patch("scan_state.ScanStateStore.from_config", return_value=state),
            ):
                queued = main_module._advance_ai_quality_review_autopilot(
                    config,
                    _logger(),
                )

            self.assertTrue(queued)
            self.assertEqual(enqueue.call_count, 2)
            selective_kwargs = enqueue.call_args_list[0].kwargs
            full_kwargs = enqueue.call_args_list[1].kwargs
            self.assertEqual(
                selective_kwargs["parameters"]["remediation"],
                "ai.retry_selective_asr",
            )
            self.assertNotIn("retry_failed", selective_kwargs)
            self.assertEqual(
                full_kwargs["parameters"]["remediation"],
                "ai.retranscribe",
            )
            self.assertEqual(
                full_kwargs["idempotency_key"],
                f"{full_prefix}{review['review_id']}:{failure_revision}",
            )
            state.close.assert_called_once()

    @staticmethod
    def _checkpoint_review(
        root: Path,
    ) -> tuple[object, Path, AsrReviewCheckpoint, dict[str, object]]:
        input_root = root / "anime"
        video = input_root / "Example" / "Season 1" / "Example - S01E01.mkv"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")
        config = _config(
            root,
            input_path=input_root,
            video_extensions=(".mkv",),
            asr_diagnostics_enabled=True,
            asr_selective_retry_enabled=True,
            auto_ai_quality_review_autopilot_enabled=True,
            auto_ai_quality_review_autopilot_interval_seconds=60,
        )
        target_srt = paths_for_video(video, config).ja_srt
        rejected = root / "checkpoint-input" / "rejected.srt"
        rejected.parent.mkdir(parents=True)
        write_srt(
            rejected,
            [
                SrtBlock(
                    1,
                    "00:00:08,000 --> 00:00:14,000",
                    ["trusted rejected transcript"],
                )
            ],
        )
        fingerprints = {
            "media_fingerprint": {"fingerprint": "1" * 64, "size": 5},
            "audio_fingerprint": {"fingerprint": "2" * 64, "size": 10},
            "audio_stream_fingerprint": {"fingerprint": "3" * 64, "index": 1},
            "cache_fingerprint": {
                "fingerprint": "4" * 64,
                "size": rejected.stat().st_size,
            },
        }
        repair_fingerprint = "9" * 64
        source_diagnostics = root / "checkpoint-input" / "diagnostics.json"
        source_diagnostics.write_text(
            json.dumps(
                {
                    "status": "selective_retry_required",
                    "reason_code": "low_confidence",
                    "srt_sha256": sha256_file(rejected),
                    "review_ranges": [[8.0, 14.0]],
                    "repair_fingerprint": repair_fingerprint,
                    **fingerprints,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        checkpoint = create_asr_review_checkpoint(
            config.work_path,
            target_path=target_srt,
            language="ja",
            rejected_srt_path=rejected,
            diagnostics_path=source_diagnostics,
        )
        checkpoint_evidence = {
            "schema_version": checkpoint.schema_version,
            "checkpoint_id": checkpoint.checkpoint_id,
            "manifest_path": str(checkpoint.manifest_path),
            "manifest_sha256": checkpoint.manifest_sha256,
            "target_path": str(checkpoint.target_path),
            "language": checkpoint.language,
            "review_ranges": [list(item) for item in checkpoint.review_ranges],
            "repair_fingerprint": checkpoint.repair_fingerprint,
            "fingerprints": checkpoint.fingerprints,
            "rejected_srt_sha256": checkpoint.rejected_srt_sha256,
            "diagnostics_sha256": checkpoint.diagnostics_sha256,
            "repair_attempted": False,
            "selective_retry_supported": True,
        }
        selective = {
            "action": "ai.retry_selective_asr",
            "strategy": "selective_asr_repair",
            "selective": True,
            "checkpoint_id": checkpoint.checkpoint_id,
            "manifest_sha256": checkpoint.manifest_sha256,
            "repair_fingerprint": checkpoint.repair_fingerprint,
            "requires_runtime_fingerprint_verification": True,
        }
        full = {
            "action": "ai.retranscribe",
            "strategy": "full_transcription_rerun",
            "selective": False,
        }
        review: dict[str, object] = {
            "review_id": "review_" + "a" * 24,
            "kind": "asr_quality",
            "status": "open",
            "target_key": str(video),
            "diagnosis": {
                "video": str(video),
                "reason_code": "low_confidence",
                "review_ranges": [[8.0, 14.0]],
                "repair_attempted": False,
                "asr_review_checkpoint": checkpoint_evidence,
            },
            "candidates": [selective, full],
        }
        return config, video, checkpoint, review

    @staticmethod
    def _file_snapshot(root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): sha256_file(path)
            for path in root.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()
