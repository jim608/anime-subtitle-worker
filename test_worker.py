from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from ass_utils import format_ass
from audio import AudioStreamInfo
from srt_utils import SrtBlock, read_srt, validate_translation, write_srt
from language_detector import AudioStreamSelection, LanguageDetectionResult
from metadata_context import MetadataContext
from output_manifest import delivery_identity, validate_output_manifest
from safe_files import atomic_write_text as real_atomic_write_text
from safe_files import sha256_file
from safe_files import verified_copy_replace as real_verified_copy_replace
from scan_state import ScanStateStore
from subtitle_quality import SubtitleQualityError, managed_quality_report_path
from subtitle_paths import (
    has_ai_finished_subtitle,
    paths_for_video,
    source_transcript_paths_for_video,
)
from transcriber import (
    AsrSelectiveRepairUnavailableError,
    LowConfidenceTranscriptionError,
    TranscriptionError,
    attach_asr_diagnostics_context,
    asr_diagnostics_path,
    asr_transcription_hold_path,
)
from translation_quality import (
    read_translation_quality_events_strict,
    translation_quality_events_path,
    translation_quality_hold_path,
    write_translation_quality_events,
    write_translation_quality_hold,
)
from translation_memory_bridge import (
    read_translation_memory_origin_strict,
    write_translation_memory_origin,
)
from translator import AsrReviewError
from worker import ProcessOutcome, VideoWorker


class VideoWorkerTest(unittest.TestCase):
    def test_resource_admission_preserves_explicit_lower_memory_asr_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                japanese_transcription_model="large-v3",
                whisper_model="large-v3",
                whisper_compute_type="float16",
            )
            worker = VideoWorker(config, _logger())
            worker._resource_launch_plan = {
                "selected_route": {
                    "model": "large-v3",
                    "compute_type": "float16",
                },
                "effective": {},
            }

            recovery = SimpleNamespace(
                **{
                    **vars(config),
                    "whisper_model": "large-v2",
                    "whisper_compute_type": "int8_float16",
                }
            )
            adjusted = worker._resource_adjusted_asr_config(recovery)

            self.assertEqual(adjusted.whisper_model, "large-v2")
            self.assertEqual(adjusted.whisper_compute_type, "int8_float16")

            worker._resource_launch_plan["selected_route"]["compute_type"] = "int8_float16"
            expensive = SimpleNamespace(
                **{
                    **vars(config),
                    "whisper_model": "large-v2",
                    "whisper_compute_type": "float16",
                }
            )
            downshifted = worker._resource_adjusted_asr_config(expensive)
            self.assertEqual(downshifted.whisper_compute_type, "int8_float16")

    def test_quality_line_previews_capture_bounded_japanese_and_chinese_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root)
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [
                    SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["正常"]),
                    SrtBlock(2, "00:00:03,000 --> 00:00:04,000", ["ありがとう"]),
                ],
            )
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["正常"]),
                    SrtBlock(2, "00:00:03,000 --> 00:00:04,000", ["ありがとう"]),
                ],
            )
            reports = [
                SimpleNamespace(
                    issues=[
                        SimpleNamespace(code="residual_japanese_kana", indexes=[2]),
                        SimpleNamespace(code="prompt_leak", indexes=[2]),
                    ]
                )
            ]

            previews = VideoWorker._quality_line_previews(paths, reports)

            self.assertEqual(len(previews), 1)
            self.assertEqual(previews[0]["index"], 2)
            self.assertEqual(previews[0]["source_ja"], "ありがとう")
            self.assertEqual(previews[0]["output_zh"], "ありがとう")
            self.assertEqual(previews[0]["issue_codes"], ["prompt_leak", "residual_japanese_kana"])

    def test_publish_ai_ass_uses_short_work_staging_for_long_media_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            media = root / "anime"
            work.mkdir()
            media.mkdir()
            video = media / ("Long Anime Title " + ("x" * 150) + ".mkv")
            video.write_bytes(b"video")
            config = _config(
                work,
                export_ai_ass=True,
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            )
            paths = paths_for_video(video, config)
            worker = VideoWorker(config, _logger())
            observed: list[Path] = []

            def export(staged) -> int:
                for path, content in (
                    (staged.ai_ja_ass, "ja"),
                    (staged.ai_zh_cn_ass, "zh-cn"),
                    (staged.ai_zh_tw_ass, "zh-tw"),
                ):
                    observed.append(path)
                    path.write_text(content, encoding="utf-8")
                return 3

            with (
                patch.object(worker, "_export_ai_ass", side_effect=export),
                patch.object(worker, "_quality_check_ai_outputs", return_value=[]),
            ):
                self.assertEqual(worker._publish_ai_ass(video, paths), 3)

            staging_base = work / "ai_publish_staging"
            self.assertEqual(
                [path.read_text(encoding="utf-8") for path in (paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass)],
                ["ja", "zh-cn", "zh-tw"],
            )
            self.assertTrue(all(path.is_relative_to(staging_base) for path in observed))
            self.assertTrue(all(len(path.name.encode("utf-8")) < 32 for path in observed))
            self.assertFalse(any(staging_base.rglob("*.ass")))

    def test_partial_ai_ass_publish_failure_restores_every_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = VideoWorker(_config(root), _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            staged_outputs = [root / f"staged-{index}.ass" for index in range(3)]
            destinations = [root / f"published-{index}.ass" for index in range(3)]
            for index, path in enumerate(staged_outputs):
                path.write_text(f"new-{index}", encoding="utf-8")
            for index, path in enumerate(destinations):
                path.write_text(f"old-{index}", encoding="utf-8")

            def fail_second_publish(source: Path, destination: Path) -> Path:
                if Path(source) == staged_outputs[1]:
                    real_verified_copy_replace(source, destination)
                    raise OSError("injected second-output failure")
                return real_verified_copy_replace(source, destination)

            with patch("worker.verified_copy_replace", side_effect=fail_second_publish):
                with self.assertRaisesRegex(OSError, "injected second-output failure"):
                    worker._replace_ai_outputs_with_rollback(video, staged_outputs, destinations)

            self.assertEqual(
                [path.read_text(encoding="utf-8") for path in destinations],
                ["old-0", "old-1", "old-2"],
            )

    def test_rollback_attempts_every_output_and_records_incomplete_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = VideoWorker(_config(root), _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            staged_outputs = [root / f"staged-{index}.ass" for index in range(3)]
            destinations = [root / f"published-{index}.ass" for index in range(3)]
            for index, path in enumerate(staged_outputs):
                path.write_text(f"new-{index}", encoding="utf-8")
            for index, path in enumerate(destinations):
                path.write_text(f"old-{index}", encoding="utf-8")

            def fail_publish_and_one_restore(source: Path, destination: Path) -> Path:
                source = Path(source)
                destination = Path(destination)
                if source == staged_outputs[1]:
                    real_verified_copy_replace(source, destination)
                    raise OSError("injected publish failure")
                if source.name.startswith("previous-1-") and destination == destinations[1]:
                    raise OSError("injected rollback failure")
                return real_verified_copy_replace(source, destination)

            with patch("worker.verified_copy_replace", side_effect=fail_publish_and_one_restore):
                with self.assertRaisesRegex(RuntimeError, "rollback was incomplete"):
                    worker._replace_ai_outputs_with_rollback(video, staged_outputs, destinations)

            self.assertEqual(destinations[0].read_text(encoding="utf-8"), "old-0")
            self.assertEqual(destinations[1].read_text(encoding="utf-8"), "new-1")
            self.assertEqual(destinations[2].read_text(encoding="utf-8"), "old-2")
            manifests = list((root / "ai_output_versions").rglob("manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rollback_failed")
            self.assertEqual(manifest["rollback_errors"][0]["path"], str(destinations[1]))

    def test_staged_quality_failure_preserves_every_previous_ai_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            destinations = [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass]
            for index, destination in enumerate(destinations):
                destination.write_text(f"old-{index}", encoding="utf-8")

            def export(staged) -> int:
                for index, output in enumerate((staged.ai_ja_ass, staged.ai_zh_cn_ass, staged.ai_zh_tw_ass)):
                    output.write_text(f"new-{index}", encoding="utf-8")
                return 3

            with (
                patch.object(worker, "_export_ai_ass", side_effect=export),
                patch.object(
                    worker,
                    "_quality_check_ai_outputs",
                    side_effect=SubtitleQualityError("injected staged quality failure"),
                ),
            ):
                with self.assertRaisesRegex(SubtitleQualityError, "injected staged quality failure"):
                    worker._publish_ai_ass(video, paths)

            self.assertEqual(
                [path.read_text(encoding="utf-8") for path in destinations],
                ["old-0", "old-1", "old-2"],
            )

    def test_publication_failure_rolls_back_remediated_srt_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            timing = "00:00:01,000 --> 00:00:03,000"
            write_srt(paths.ja_srt, [SrtBlock(1, timing, ["source"] )])
            write_srt(paths.zh_cn_srt, [SrtBlock(1, timing, ["软件在里面"] )])
            write_srt(paths.zh_tw_srt, [SrtBlock(1, timing, ["软件在里面"] )])
            before = {
                paths.ja_srt: paths.ja_srt.read_bytes(),
                paths.zh_cn_srt: paths.zh_cn_srt.read_bytes(),
                paths.zh_tw_srt: paths.zh_tw_srt.read_bytes(),
            }

            with (
                patch.object(worker, "_enforce_asr_publication_gate"),
                patch.object(
                    worker,
                    "_export_ai_ass",
                    side_effect=SubtitleQualityError("injected ASS export failure"),
                ),
            ):
                with self.assertRaisesRegex(SubtitleQualityError, "injected ASS export failure"):
                    worker._publish_ai_ass(video, paths)

            self.assertEqual(paths.ja_srt.read_bytes(), before[paths.ja_srt])
            self.assertEqual(paths.zh_cn_srt.read_bytes(), before[paths.zh_cn_srt])
            self.assertEqual(paths.zh_tw_srt.read_bytes(), before[paths.zh_tw_srt])

    def test_prepublication_qc_repairs_safe_srt_issue_then_rechecks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                subtitle_quality_max_primary_chars=8,
                subtitle_quality_hard_max_primary_chars=12,
            )
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            timing = "00:00:01,000 --> 00:00:04,000"
            write_srt(paths.zh_cn_srt, [SrtBlock(1, timing, ["短句"] )])
            write_srt(paths.zh_tw_srt, [SrtBlock(1, timing, ["第一段，第二段，第三段"] )])

            diagnostics = worker._remediate_prepublication_srts(video, paths)

            self.assertEqual(len(diagnostics), 1)
            self.assertTrue(diagnostics[0]["recheck"]["has_failures"] is False)
            self.assertGreater(len(read_srt(paths.zh_tw_srt)[0].text), 1)

    def test_prepublication_qc_never_auto_repairs_hallucination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            write_srt(
                paths.zh_tw_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["ご視聴ありがとうございました。"])],
            )

            with self.assertRaises(SubtitleQualityError):
                worker._remediate_prepublication_srts(video, paths)

    def test_prepublication_qc_keeps_timing_repair_fail_closed_for_trilingual_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            write_srt(
                paths.zh_tw_srt,
                [
                    SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["第一句"]),
                    SrtBlock(2, "00:00:02,850 --> 00:00:04,000", ["第二句"]),
                ],
            )

            original = paths.zh_tw_srt.read_bytes()
            with self.assertRaises(SubtitleQualityError):
                worker._remediate_prepublication_srts(video, paths)

            # A timing-only change to zh-CN/zh-TW would violate the shared
            # Japanese timeline required by the trilingual ASS exporter.
            # Keep the canonical cache unchanged until a bundle-wide repair
            # can update all three tracks and their evidence atomically.
            self.assertEqual(paths.zh_tw_srt.read_bytes(), original)

    def test_prepublication_qc_repairs_exactly_aligned_trilingual_timing_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            timings = [
                "00:00:01,000 --> 00:00:03,000",
                "00:00:45,560 --> 00:00:50,260",
                "00:00:48,460 --> 00:00:50,360",
                "00:00:50,320 --> 00:00:51,120",
                "00:00:50,420 --> 00:00:55,100",
            ]
            write_srt(
                paths.ja_srt,
                [SrtBlock(index, timing, [f"source {index}"]) for index, timing in enumerate(timings, 1)],
            )
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(index, timing, [f"简体 {index}"]) for index, timing in enumerate(timings, 1)],
            )
            write_srt(
                paths.zh_tw_srt,
                [SrtBlock(index, timing, [f"繁體 {index}"]) for index, timing in enumerate(timings, 1)],
            )

            diagnostics = worker._remediate_prepublication_srts(video, paths)

            self.assertEqual(len(diagnostics), 3)
            repaired_ja = read_srt(paths.ja_srt)
            repaired_cn = read_srt(paths.zh_cn_srt)
            repaired_tw = read_srt(paths.zh_tw_srt)
            validate_translation(repaired_ja, repaired_cn)
            validate_translation(repaired_ja, repaired_tw)
            self.assertEqual(repaired_ja[1].timing, "00:00:45,560 --> 00:00:48,460")
            self.assertEqual(repaired_ja[3].timing, "00:00:50,320 --> 00:00:51,120")
            self.assertEqual(repaired_ja[4].timing, "00:00:51,120 --> 00:00:55,100")

    def test_prepublication_qc_repairs_evidence_bound_aligned_cps_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                subtitle_quality_fail_cps=25.0,
                subtitle_quality_min_duration_seconds=0.35,
            )
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            timings = [
                "00:00:01,000 --> 00:00:01,220",
                "00:00:02,000 --> 00:00:04,000",
            ]
            write_srt(
                paths.ja_srt,
                [
                    SrtBlock(1, timings[0], ["abcdefghij"]),
                    SrtBlock(2, timings[1], ["next"]),
                ],
            )
            for target in (paths.zh_cn_srt, paths.zh_tw_srt):
                write_srt(
                    target,
                    [
                        SrtBlock(1, timings[0], ["ok"]),
                        SrtBlock(2, timings[1], ["next"]),
                    ],
                )

            diagnostics = worker._remediate_prepublication_srts(video, paths)

            self.assertEqual(len(diagnostics), 3)
            repaired_ja = read_srt(paths.ja_srt)
            repaired_cn = read_srt(paths.zh_cn_srt)
            repaired_tw = read_srt(paths.zh_tw_srt)
            validate_translation(repaired_ja, repaired_cn)
            validate_translation(repaired_ja, repaired_tw)
            self.assertEqual(
                repaired_ja[0].timing,
                "00:00:01,000 --> 00:00:01,401",
            )
            self.assertEqual(repaired_ja[1].timing, timings[1])

    def test_cps_repair_retranslates_only_over_speed_lines_and_preserves_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            source = [
                SrtBlock(
                    1,
                    "00:00:01,000 --> 00:00:02,000",
                    ["An English source line"],
                ),
                SrtBlock(
                    2,
                    "00:00:03,000 --> 00:00:05,000",
                    ["Another source line"],
                ),
            ]
            write_srt(paths.ja_srt, source)
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(
                        1,
                        source[0].timing,
                        ["這是一段明顯超過二十五個中文字而且無法在一秒內閱讀完成的字幕"],
                    ),
                    SrtBlock(2, source[1].timing, ["正常字幕"]),
                ],
            )
            write_srt(paths.zh_tw_srt, read_srt(paths.zh_cn_srt))
            translator = Mock()

            def repair(
                blocks,
                _source_path,
                output_path,
                *,
                series_glossary,
                source_language,
                max_display_chars_by_index,
            ) -> None:
                self.assertEqual([block.index for block in blocks], [1])
                self.assertEqual(series_glossary, {})
                self.assertEqual(source_language, "en")
                self.assertEqual(max_display_chars_by_index, {1: 25})
                write_srt(
                    output_path,
                    [SrtBlock(1, source[0].timing, ["精簡翻譯"])],
                )

            translator.retranslate_problem_blocks.side_effect = repair
            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(
                    worker,
                    "_build_series_metadata_context",
                    return_value=None,
                ),
            ):
                changed = worker._repair_translation_cps_violations(
                    video,
                    paths,
                    source_language="en",
                )

            self.assertTrue(changed)
            repaired = read_srt(paths.zh_cn_srt)
            self.assertEqual([block.timing for block in repaired], [block.timing for block in source])
            self.assertEqual([" ".join(block.text) for block in repaired], ["精簡翻譯", "正常字幕"])
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(translation_quality_hold_path(paths.zh_cn_srt).exists())

    def test_refresh_ass_refuses_partial_srt_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])],
            )
            paths.ai_ja_ass.write_text("old-good-output", encoding="utf-8")

            with (
                patch.object(worker, "_export_ai_ass") as export_mock,
                patch.object(worker, "_restyle_existing_ai_ass_safely", return_value=0),
            ):
                self.assertFalse(worker.refresh_ass(video))

            export_mock.assert_not_called()
            self.assertEqual(paths.ai_ja_ass.read_text(encoding="utf-8"), "old-good-output")

    def test_refresh_ass_keeps_verified_japanese_srt_when_intermediates_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, export_ai_ass=True, keep_intermediate_files=False)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            block = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["text"])]
            for srt in (paths.ja_srt, paths.zh_cn_srt, paths.zh_tw_srt):
                write_srt(srt, block)

            def publish(_video: Path, _paths) -> int:
                for output in (_paths.ai_ja_ass, _paths.ai_zh_cn_ass, _paths.ai_zh_tw_ass):
                    output.write_text("validated", encoding="utf-8")
                return 3

            with patch.object(worker, "_publish_ai_ass", side_effect=publish):
                self.assertTrue(worker.refresh_ass(video))

            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    required_outputs=[paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                )
            )

    def test_refresh_ass_blocks_pending_or_rejected_japanese_asr_cache(self) -> None:
        for case in ("pending", "rejected"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config = _config(
                    root,
                    export_ai_ass=True,
                    log_path=root / "failure.log",
                )
                worker = VideoWorker(config, _logger())
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                paths = paths_for_video(video, config)
                block = [
                    SrtBlock(
                        1,
                        "00:00:00,000 --> 00:00:01,000",
                        ["cache"],
                    )
                ]
                for srt in (paths.ja_srt, paths.zh_cn_srt, paths.zh_tw_srt):
                    write_srt(srt, block)
                if case == "pending":
                    worker._begin_asr_commit(
                        paths.ja_srt,
                        reason="simulated interrupted refresh input",
                    )
                else:
                    diagnostic = asr_diagnostics_path(paths.ja_srt, config)
                    diagnostic.parent.mkdir(parents=True, exist_ok=True)
                    diagnostic.write_text(
                        json.dumps(
                            {
                                "status": "selective_retry_required",
                                "srt_path": str(paths.ja_srt),
                                "srt_sha256": sha256_file(paths.ja_srt),
                            }
                        ),
                        encoding="utf-8",
                    )

                with (
                    patch.object(worker, "_export_ai_ass") as export,
                    patch("worker.log_failure"),
                ):
                    self.assertFalse(worker.refresh_ass(video))

                export.assert_not_called()
                self.assertFalse(paths.ai_ja_ass.exists())
                self.assertFalse(paths.ai_zh_cn_ass.exists())
                self.assertFalse(paths.ai_zh_tw_ass.exists())

    def test_existing_ass_restyle_quality_failure_preserves_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            paths.ai_ja_ass.write_text("old-good-output", encoding="utf-8")

            def restyle_staged(path: Path, _style) -> bool:
                path.write_text("new-unverified-output", encoding="utf-8")
                return True

            with (
                patch("worker.restyle_ass_file", side_effect=restyle_staged),
                patch.object(
                    worker,
                    "_quality_check_ai_outputs",
                    side_effect=SubtitleQualityError("injected refresh quality failure"),
                ),
            ):
                with self.assertRaisesRegex(SubtitleQualityError, "injected refresh quality failure"):
                    worker._restyle_existing_ai_ass_safely(video, paths)

            self.assertEqual(paths.ai_ja_ass.read_text(encoding="utf-8"), "old-good-output")
            self.assertFalse(any((root / "ai_publish_staging").rglob("*.ass")))

    def test_version_manifest_finalize_failure_rolls_back_every_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = VideoWorker(_config(root), _logger())
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            staged_outputs = [root / f"staged-{index}.ass" for index in range(3)]
            destinations = [root / f"published-{index}.ass" for index in range(3)]
            for index, path in enumerate(staged_outputs):
                path.write_text(f"new-{index}", encoding="utf-8")
            for index, path in enumerate(destinations):
                path.write_text(f"old-{index}", encoding="utf-8")
            manifest_writes = 0

            def fail_completed_manifest(path: Path, content: str) -> Path:
                nonlocal manifest_writes
                manifest_writes += 1
                if manifest_writes == 2:
                    raise OSError("injected completed-manifest failure")
                return real_atomic_write_text(path, content)

            with patch("worker.atomic_write_text", side_effect=fail_completed_manifest):
                with self.assertRaisesRegex(OSError, "injected completed-manifest failure"):
                    worker._replace_ai_outputs_with_rollback(video, staged_outputs, destinations)

            self.assertEqual(
                [path.read_text(encoding="utf-8") for path in destinations],
                ["old-0", "old-1", "old-2"],
            )
            manifests = list((root / "ai_output_versions").rglob("manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertIn(json.loads(manifests[0].read_text(encoding="utf-8"))["status"], {"prepared", "rolled_back"})

    def test_completed_output_version_retention_preserves_incomplete_restore_points(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, ai_output_versions_keep=2)
            worker = VideoWorker(config, _logger())
            digest = "a" * 16
            version_root = root / "ai_output_versions" / digest
            for stamp in range(1, 6):
                candidate = version_root / str(stamp)
                candidate.mkdir(parents=True)
                payload = (
                    {"video": "legacy.mkv", "created_at": 1.0, "backups": []}
                    if stamp == 1
                    else {"status": "completed", "stamp": stamp}
                )
                (candidate / "manifest.json").write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )
            prepared = version_root / "999"
            prepared.mkdir()
            (prepared / "manifest.json").write_text(
                json.dumps({"status": "prepared"}),
                encoding="utf-8",
            )

            worker._prune_completed_ai_output_versions(digest)

            self.assertEqual(
                sorted(path.name for path in version_root.iterdir() if path.is_dir()),
                ["4", "5", "999"],
            )

    def test_source_ass_quality_failure_preserves_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            video = root / "English Show S01E01.mkv"
            video.write_bytes(b"video")
            source = source_transcript_paths_for_video(video, config, "en")
            write_srt(source.srt, [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["hello"])])
            source.ass.write_text("previous-good-source", encoding="utf-8")

            with patch.object(
                worker,
                "_quality_check_ai_outputs",
                side_effect=SubtitleQualityError("injected source quality failure"),
            ):
                with self.assertRaisesRegex(SubtitleQualityError, "injected source quality failure"):
                    worker._publish_source_ass(video, source.srt, source.ass)

            self.assertEqual(source.ass.read_text(encoding="utf-8"), "previous-good-source")

    def test_stage_tracking_discards_locked_transaction_before_continuing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            worker = VideoWorker(_config(root), _logger())
            state = Mock()
            state.commit.side_effect = sqlite3.OperationalError("database is locked")
            worker._stage_state = state

            worker._set_stage(video, "translation", "running", "Translating")

            state.rollback.assert_called_once_with()
            state.close.assert_called_once_with()
            self.assertIsNone(worker._stage_state)

    def test_process_waits_for_short_official_subtitle_lock_instead_of_failing_ai_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root)
            config.video_lock_wait_seconds = 1
            worker = VideoWorker(config, _logger())
            fake_lock = Mock()
            fake_lock.acquired = False

            def acquire() -> bool:
                if fake_lock.acquire.call_count == 1:
                    return False
                fake_lock.acquired = True
                return True

            fake_lock.acquire.side_effect = acquire
            with (
                patch("worker.VideoLock", return_value=fake_lock),
                patch.object(worker, "_process_locked", return_value=ProcessOutcome("skipped", "ok", "Official subtitle exists")),
                patch("worker.time.sleep"),
            ):
                self.assertTrue(worker.process(video))

            self.assertEqual(fake_lock.acquire.call_count, 2)
            fake_lock.release.assert_called_once_with()

    def test_process_removes_temp_audio_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root)
            config.log_path = root
            worker = VideoWorker(config, _logger())
            audio_path = worker._audio_path(video)
            vocals_path = worker._separated_audio_path(video)
            audio_path.write_bytes(b"audio")
            vocals_path.write_bytes(b"vocals")

            with (
                patch.object(worker, "_process_locked", side_effect=RuntimeError("boom")),
                patch.object(worker, "_set_stage"),
                patch("worker.log_failure"),
            ):
                self.assertFalse(worker.process(video))

            self.assertFalse(audio_path.exists())
            self.assertFalse(vocals_path.exists())

    def test_restores_japanese_srt_cache_from_clean_ai_ass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root)
            paths = paths_for_video(video, config)
            paths.ai_ja_ass.write_text(
                format_ass(
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:03,000",
                            ["再利用できる日本語字幕"],
                        )
                    ]
                ),
                encoding="utf-8-sig",
            )
            diagnostic = asr_diagnostics_path(paths.ja_srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text('{"status":"accepted"}', encoding="utf-8")
            worker = VideoWorker(config, _logger())

            restored = worker._restore_japanese_srt_cache_from_ass(paths)

            self.assertTrue(restored)
            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(diagnostic.exists())
            self.assertEqual(read_srt(paths.ja_srt)[0].text, ["再利用できる日本語字幕"])

    def test_cached_japanese_srt_leading_gap_is_detected_for_bounded_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            srt_path = root / "cached.ja.srt"
            write_srt(
                srt_path,
                [SrtBlock(1, "00:00:45,000 --> 00:00:47,000", ["途中から始まる字幕"])],
            )
            worker = VideoWorker(
                _config(
                    root,
                    enable_leading_gap_rescue=True,
                    gap_rescue_leading_threshold_seconds=1.5,
                    gap_rescue_leading_max_seconds=30.0,
                ),
                _logger(),
            )

            self.assertEqual(worker._cached_japanese_leading_gap_range(srt_path), (0.0, 30.0))

    def test_cached_japanese_srt_opening_probe_recovers_lines_and_invalidates_translations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                enable_leading_gap_rescue=True,
                gap_rescue_leading_threshold_seconds=1.5,
                gap_rescue_leading_max_seconds=120.0,
                gap_rescue_no_speech_threshold=0.95,
                gap_rescue_log_prob_threshold=-1.5,
                gap_rescue_compression_ratio_threshold=2.4,
                asr_diagnostics_path="asr_diagnostics",
            )
            paths = paths_for_video(video, config)
            original = SrtBlock(1, "00:00:20,000 --> 00:00:22,000", ["二十秒才開始"])
            write_srt(paths.ja_srt, [original])
            write_srt(paths.zh_cn_srt, [SrtBlock(1, original.timing, ["舊翻譯"])])
            write_srt(paths.zh_tw_srt, [SrtBlock(1, original.timing, ["舊翻譯"])])
            diagnostic = asr_diagnostics_path(paths.ja_srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "srt_path": str(paths.ja_srt),
                        "srt_sha256": sha256_file(paths.ja_srt),
                    }
                ),
                encoding="utf-8",
            )
            worker = VideoWorker(config, _logger())
            observed: dict[str, object] = {}
            hold = asr_transcription_hold_path(paths.ja_srt, config)

            def repair(_audio, srt, ranges, repair_config, _logger) -> None:
                observed["ranges"] = ranges
                observed["prompt"] = repair_config.whisper_initial_prompt
                observed["no_speech_threshold"] = repair_config.whisper_no_speech_threshold
                write_srt(
                    srt,
                    [
                        SrtBlock(1, "00:00:00,500 --> 00:00:02,000", ["補回第一句"]),
                        SrtBlock(2, original.timing, original.text),
                    ],
                )

            original_invalidate = worker._invalidate_translation_intermediates

            def invalidate(observed_paths) -> None:
                self.assertTrue(hold.is_file())
                original_invalidate(observed_paths)

            with (
                patch(
                    "worker.repair_low_confidence_ranges",
                    side_effect=repair,
                ) as repair_mock,
                patch.object(
                    worker,
                    "_invalidate_translation_intermediates",
                    side_effect=invalidate,
                ),
            ):
                ready = worker._refresh_cached_japanese_leading_gap(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                )

            self.assertTrue(ready)
            repair_mock.assert_called_once()
            self.assertEqual(observed["ranges"], [(0.0, 20.0)])
            self.assertIsNone(observed["prompt"])
            self.assertEqual(observed["no_speech_threshold"], 0.95)
            self.assertEqual(read_srt(paths.ja_srt)[0].text, ["補回第一句"])
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertFalse(diagnostic.exists())
            self.assertFalse(hold.exists())
            self.assertTrue(worker._leading_gap_cache_probe_path(paths.ja_srt).exists())

    def test_cached_japanese_opening_artifact_rejection_runs_full_prompt_free_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                enable_leading_gap_rescue=True,
                gap_rescue_leading_threshold_seconds=1.5,
                gap_rescue_leading_max_seconds=120.0,
                whisper_initial_prompt="unsafe cached prompt",
                op_ed_initial_prompt="unsafe cached lyrics prompt",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                scanner_cache_enabled=False,
            )
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:20,000 --> 00:00:22,000",
                        ["rejected cached opening"],
                    )
                ],
            )
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:20,000 --> 00:00:22,000",
                        ["stale simplified translation"],
                    )
                ],
            )
            write_srt(
                paths.zh_tw_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:20,000 --> 00:00:22,000",
                        ["stale traditional translation"],
                    )
                ],
            )
            worker = VideoWorker(config, _logger())
            observed_configs: list[object] = []

            def full_transcribe(_audio, output, active_config) -> None:
                observed_configs.append(active_config)
                write_srt(
                    output,
                    [
                        SrtBlock(
                            1,
                            "00:00:05,000 --> 00:00:06,500",
                            ["clean prompt-free rebuild"],
                        )
                    ],
                )

            with (
                patch(
                    "worker.repair_low_confidence_ranges",
                    side_effect=TranscriptionError(
                        "ASR artifacts or low-confidence rescue candidates were rejected; "
                        "ranges=0.0-5.5s"
                    ),
                ),
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=full_transcribe,
                ) as full,
            ):
                ready = worker._refresh_cached_japanese_leading_gap(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                )
                ready_again = worker._refresh_cached_japanese_leading_gap(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                )

            self.assertTrue(ready)
            self.assertTrue(ready_again)
            full.assert_called_once()
            self.assertEqual(len(observed_configs), 1)
            fallback_config = observed_configs[0]
            self.assertIsNone(fallback_config.whisper_initial_prompt)
            self.assertIsNone(fallback_config.op_ed_initial_prompt)
            self.assertFalse(fallback_config.whisper_condition_on_previous_text)
            self.assertFalse(fallback_config.asr_optional_rescue_rejection_is_fatal)
            self.assertTrue(
                fallback_config.asr_prompt_free_allow_recovered_primary_artifacts
            )
            self.assertEqual(
                read_srt(paths.ja_srt)[0].text,
                ["clean prompt-free rebuild"],
            )
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(
                worker._leading_gap_cache_probe_path(paths.ja_srt).exists()
            )
            archived_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (root / "asr_rejected_cache").rglob("*")
                if path.is_file()
            )
            self.assertIn("rejected cached opening", archived_text)
            self.assertIn("stale simplified translation", archived_text)
            self.assertIn("stale traditional translation", archived_text)
            self.assertNotIn("clean prompt-free rebuild", archived_text)

    def test_cached_japanese_opening_fallback_failure_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                enable_leading_gap_rescue=True,
                gap_rescue_leading_threshold_seconds=1.5,
                gap_rescue_leading_max_seconds=120.0,
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                scanner_cache_enabled=False,
            )
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:20,000 --> 00:00:22,000",
                        ["rejected cache must not survive"],
                    )
                ],
            )
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:20,000 --> 00:00:22,000",
                        ["stale translation must not survive"],
                    )
                ],
            )
            worker = VideoWorker(config, _logger())

            with (
                patch(
                    "worker.repair_low_confidence_ranges",
                    side_effect=TranscriptionError(
                        "ASR artifacts or low-confidence rescue candidates were rejected; "
                        "ranges=0.0-5.5s"
                    ),
                ),
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=TranscriptionError("full prompt-free fallback failed"),
                ),
            ):
                with self.assertRaisesRegex(
                    TranscriptionError,
                    "Full prompt-free ASR fallback failed after rejected cache was archived",
                ):
                    worker._refresh_cached_japanese_leading_gap(
                        video,
                        audio,
                        paths,
                        audio_ready=True,
                    )

            self.assertFalse(paths.ja_srt.exists())
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertFalse(
                asr_transcription_hold_path(paths.ja_srt, config).exists()
            )
            archived_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (root / "asr_rejected_cache").rglob("*")
                if path.is_file()
            )
            self.assertIn("rejected cache must not survive", archived_text)
            self.assertIn("stale translation must not survive", archived_text)

    def test_full_prompt_free_fallback_cuda_oom_retries_once_with_lower_memory_compute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                whisper_device="cuda",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                japanese_transcription_fallback_compute_type="float16",
                asr_diagnostics_enabled=False,
                scanner_cache_enabled=False,
            )
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["rejected cache"])],
            )
            worker = VideoWorker(config, _logger())
            observed_compute_types: list[str] = []

            def transcribe(_audio, output, active_config) -> None:
                observed_compute_types.append(active_config.whisper_compute_type)
                if len(observed_compute_types) == 1:
                    write_srt(
                        output,
                        [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["unsafe partial"])],
                    )
                    raise TranscriptionError("CUDA failed with error out of memory")
                write_srt(
                    output,
                    [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["clean fallback"])],
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.clear_whisper_model_cache") as clear_cache,
            ):
                rebuilt = worker._run_full_prompt_free_asr_fallback(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                    reason="test rejected cache",
                )

            self.assertTrue(rebuilt)
            self.assertEqual(observed_compute_types, ["float16", "int8_float16"])
            clear_cache.assert_called_once()
            self.assertEqual(read_srt(paths.ja_srt)[0].text, ["clean fallback"])

    def test_full_prompt_free_fallback_second_oom_uses_smaller_final_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                whisper_device="cuda",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_compute_type="float16",
                japanese_transcription_final_fallback_backend="faster-whisper",
                japanese_transcription_final_fallback_model="medium",
                japanese_transcription_final_fallback_compute_type="int8_float16",
                asr_diagnostics_enabled=False,
                scanner_cache_enabled=False,
            )
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["rejected cache"])],
            )
            worker = VideoWorker(config, _logger())
            observed_routes: list[tuple[str, str]] = []

            def transcribe(_audio, output, active_config) -> None:
                observed_routes.append(
                    (
                        active_config.whisper_model,
                        active_config.whisper_compute_type,
                    )
                )
                if len(observed_routes) < 3:
                    raise TranscriptionError("CUDA failed with error out of memory")
                write_srt(
                    output,
                    [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["small model recovery"])],
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.clear_whisper_model_cache") as clear_cache,
            ):
                rebuilt = worker._run_full_prompt_free_asr_fallback(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                    reason="test repeated oom",
                )

            self.assertTrue(rebuilt)
            self.assertEqual(
                observed_routes,
                [
                    ("large-v2", "float16"),
                    ("large-v2", "int8_float16"),
                    ("medium", "int8_float16"),
                ],
            )
            self.assertEqual(clear_cache.call_count, 2)
            self.assertEqual(
                read_srt(paths.ja_srt)[0].text,
                ["small model recovery"],
            )
            self.assertEqual(worker._last_asr_route.model, "medium")

    def test_cached_japanese_srt_no_speech_probe_is_persisted_and_not_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                enable_leading_gap_rescue=True,
                gap_rescue_leading_threshold_seconds=1.5,
                gap_rescue_leading_max_seconds=120.0,
                asr_diagnostics_path="asr_diagnostics",
            )
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:20,000 --> 00:00:22,000", ["実際の最初の台詞"])],
            )
            worker = VideoWorker(config, _logger())

            with patch(
                "worker.repair_low_confidence_ranges",
                side_effect=TranscriptionError("Whisper returned no subtitle segments."),
            ) as repair_mock:
                worker._refresh_cached_japanese_leading_gap(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                )
                worker._refresh_cached_japanese_leading_gap(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                )

            repair_mock.assert_called_once()
            marker = worker._leading_gap_cache_probe_path(paths.ja_srt)
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8"))["status"],
                "no_speech_detected",
            )

    def test_translation_cache_without_japanese_source_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root)
            paths = paths_for_video(video, config)
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, "00:00:20,000 --> 00:00:22,000", ["無法驗證的舊翻譯"])],
            )
            write_srt(
                paths.zh_tw_srt,
                [SrtBlock(1, "00:00:20,000 --> 00:00:22,000", ["無法驗證的舊翻譯"])],
            )
            worker = VideoWorker(config, _logger())

            moved = worker._quarantine_unverifiable_translation_cache(video, paths)

            self.assertEqual(len(moved), 2)
            self.assertTrue(all(path.is_file() for path in moved))
            self.assertTrue(all("missing_japanese_source" in str(path) for path in moved))
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())

    def test_source_language_srt_is_wrapped_before_ass_quality_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            srt_path = root / "source.en.srt"
            write_srt(
                srt_path,
                [
                    SrtBlock(
                        1,
                        "00:00:01,000 --> 00:00:04,000",
                        ["This is a very long English subtitle line that should be wrapped before ASS export."],
                    )
                ],
            )
            worker = VideoWorker(_config(root, subtitle_quality_max_primary_chars=24), _logger())

            self.assertTrue(worker._normalize_source_language_srt_for_readability(srt_path))

            blocks = read_srt(srt_path)
            self.assertGreater(len(blocks[0].text), 1)
            self.assertTrue(all(len(line) <= 24 for line in blocks[0].text))

    def test_migrate_legacy_ai_srt_paths_renames_old_intermediate_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            old_ja = root / "Anime S01E01.AI.ja.srt"
            old_zh_cn = root / "Anime S01E01.AI.zh.srt"
            old_zh_tw = root / "Anime S01E01.AI.zh-TW.srt"
            human_zh_tw = root / "Anime S01E01.zh-TW.srt"
            for path in (old_ja, old_zh_cn, old_zh_tw):
                path.write_text("legacy", encoding="utf-8")
            human_zh_tw.write_text("human", encoding="utf-8")
            config = _config(root)
            worker = VideoWorker(config, _logger())

            paths = paths_for_video(video, config)
            worker._migrate_legacy_ai_srt_paths(video, paths)

            self.assertFalse(old_ja.exists())
            self.assertFalse(old_zh_cn.exists())
            self.assertFalse(old_zh_tw.exists())
            self.assertTrue(human_zh_tw.exists())
            self.assertTrue(paths.ja_srt.exists())
            self.assertTrue(paths.zh_cn_srt.exists())
            self.assertTrue(paths.zh_tw_srt.exists())

    def test_process_restores_japanese_srt_if_it_disappears_during_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root)
            worker = VideoWorker(config, _logger())
            source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]
            translator = Mock()

            def transcribe(_audio_path: Path, ja_srt: Path) -> None:
                write_srt(ja_srt, source_blocks)

            def postprocess(ja_srt: Path) -> list[SrtBlock]:
                ja_srt.unlink()
                return source_blocks

            def translate_blocks(blocks: list[SrtBlock], _ja_srt: Path, zh_cn_srt: Path) -> None:
                self.assertEqual(blocks, source_blocks)
                write_srt(zh_cn_srt, [SrtBlock(1, source_blocks[0].timing, ["translated"])])

            translator.translate_blocks.side_effect = translate_blocks
            with (
                patch("worker.extract_audio", return_value=None),
                patch.object(worker, "_transcribe", side_effect=transcribe),
                patch.object(worker, "_postprocess_ja_srt", side_effect=postprocess),
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_convert_to_zh_tw", side_effect=lambda _src, dst: write_srt(dst, source_blocks)),
            ):
                worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            paths = paths_for_video(video, config)
            self.assertTrue(paths.ja_srt.exists())
            self.assertTrue(paths.zh_cn_srt.exists())
            self.assertFalse((root / "Anime S01E01.AI.ja.srt").exists())
            self.assertFalse((root / "Anime S01E01.AI.zh.srt").exists())

    def test_process_repairs_fresh_safe_omission_in_the_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(
                root,
                enable_leading_gap_rescue=False,
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [
                SrtBlock(
                    1,
                    "00:00:00,000 --> 00:00:01,000",
                    ["fresh source"],
                )
            ]
            write_srt(paths.ja_srt, source)
            translator = Mock()

            def translate(_blocks, _source_path, output_path) -> None:
                write_srt(
                    output_path,
                    [SrtBlock(1, source[0].timing, ["safe omitted output"])],
                )
                write_translation_quality_events(
                    output_path,
                    [
                        {
                            "code": "translation_safe_omission",
                            "severity": "fail",
                            "index": 1,
                            "source": "fresh source",
                            "output": "safe omitted output",
                            "reason": "fresh deterministic omission",
                        }
                    ],
                )

            def repair(
                blocks,
                _source_path,
                output_path,
                *,
                series_glossary,
            ) -> None:
                self.assertEqual([block.index for block in blocks], [1])
                self.assertEqual(series_glossary, {})
                write_srt(
                    output_path,
                    [SrtBlock(1, source[0].timing, ["repaired in same job"])],
                )

            translator.translate_blocks.side_effect = translate
            translator.retranslate_problem_blocks.side_effect = repair

            def convert(src: Path, dst: Path) -> None:
                write_srt(dst, read_srt(src))

            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_convert_to_zh_tw", side_effect=convert),
                patch.object(
                    worker,
                    "_postprocess_ja_srt",
                    side_effect=lambda path: read_srt(path),
                ),
            ):
                self.assertTrue(worker.process(video))

            translator.translate_blocks.assert_called_once()
            translator.retranslate_problem_blocks.assert_called_once()
            self.assertEqual(
                read_srt(paths.zh_cn_srt)[0].text,
                ["repaired in same job"],
            )
            self.assertFalse(
                translation_quality_events_path(paths.zh_cn_srt).exists()
            )
            self.assertFalse(
                translation_quality_hold_path(paths.zh_cn_srt).exists()
            )

    def test_process_bounds_fresh_omission_asr_escalation_and_fails_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                enable_leading_gap_rescue=False,
                asr_selective_retry_enabled=True,
                japanese_transcription_fallback_backend="faster-whisper",
                scanner_cache_enabled=False,
                log_path=root / "failure.log",
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            timing = "00:00:10,000 --> 00:00:12,000"
            write_srt(paths.ja_srt, [SrtBlock(1, timing, ["original source"])])
            translator = Mock()

            def write_omission(_blocks, _source_path, output_path, **_kwargs) -> None:
                source_text = " ".join(read_srt(paths.ja_srt)[0].text)
                write_srt(
                    output_path,
                    [SrtBlock(1, timing, ["same unresolved output"])],
                )
                write_translation_quality_events(
                    output_path,
                    [
                        {
                            "code": "translation_safe_omission",
                            "severity": "fail",
                            "index": 1,
                            "source": source_text,
                            "output": "same unresolved output",
                            "reason": "deterministic omission",
                        }
                    ],
                )

            translator.translate_blocks.side_effect = write_omission
            translator.retranslate_problem_blocks.side_effect = write_omission

            def selective(
                _video,
                _audio,
                observed_paths,
                _ranges,
                _fallback_config,
                *,
                require_confidence,
                require_changed_transcript,
            ) -> bool:
                self.assertFalse(require_confidence)
                self.assertTrue(require_changed_transcript)
                write_srt(
                    observed_paths.ja_srt,
                    [SrtBlock(1, timing, ["prompt-free changed source"])],
                )
                worker._invalidate_translation_intermediates(observed_paths)
                return True

            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_audio_path", return_value=audio),
                patch("worker.validate_cached_audio", return_value=True),
                patch.object(
                    worker,
                    "_try_prompt_free_selective_asr_repair",
                    side_effect=selective,
                ) as selective_retry,
                patch.object(
                    worker,
                    "_postprocess_ja_srt",
                    side_effect=lambda path: read_srt(path),
                ),
                patch("worker.log_failure"),
                patch("worker.notify_event"),
            ):
                self.assertFalse(worker.process(video))

            self.assertEqual(translator.translate_blocks.call_count, 2)
            self.assertEqual(
                translator.retranslate_problem_blocks.call_count,
                2,
            )
            selective_retry.assert_called_once()
            self.assertTrue(
                translation_quality_events_path(paths.zh_cn_srt).exists()
            )

    def test_process_runs_final_targeted_repair_after_asr_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                enable_leading_gap_rescue=False,
                asr_selective_retry_enabled=True,
                japanese_transcription_fallback_backend="faster-whisper",
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            timing = "00:00:10,000 --> 00:00:12,000"
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, timing, ["original source"])],
            )
            translator = Mock()

            def write_omission(
                _blocks,
                _source_path,
                output_path,
                **_kwargs,
            ) -> None:
                source_text = " ".join(read_srt(paths.ja_srt)[0].text)
                write_srt(
                    output_path,
                    [SrtBlock(1, timing, ["same unresolved output"])],
                )
                write_translation_quality_events(
                    output_path,
                    [
                        {
                            "code": "translation_safe_omission",
                            "severity": "fail",
                            "index": 1,
                            "source": source_text,
                            "output": "same unresolved output",
                            "reason": "deterministic omission",
                        }
                    ],
                )

            repair_calls = 0

            def targeted_repair(
                blocks,
                _source_path,
                output_path,
                **_kwargs,
            ) -> None:
                nonlocal repair_calls
                repair_calls += 1
                if repair_calls == 1:
                    write_omission(blocks, _source_path, output_path)
                    return
                write_srt(
                    output_path,
                    [
                        SrtBlock(
                            1,
                            timing,
                            ["final targeted repair"],
                        )
                    ],
                )

            translator.translate_blocks.side_effect = write_omission
            translator.retranslate_problem_blocks.side_effect = targeted_repair

            def selective(
                _video,
                _audio,
                observed_paths,
                _ranges,
                _fallback_config,
                **_kwargs,
            ) -> bool:
                write_srt(
                    observed_paths.ja_srt,
                    [SrtBlock(1, timing, ["prompt-free changed source"])],
                )
                worker._invalidate_translation_intermediates(observed_paths)
                return True

            def convert(source: Path, destination: Path) -> None:
                write_srt(destination, read_srt(source))

            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_audio_path", return_value=audio),
                patch("worker.validate_cached_audio", return_value=True),
                patch.object(
                    worker,
                    "_try_prompt_free_selective_asr_repair",
                    side_effect=selective,
                ) as selective_retry,
                patch.object(
                    worker,
                    "_postprocess_ja_srt",
                    side_effect=lambda path: read_srt(path),
                ),
                patch.object(
                    worker,
                    "_convert_to_zh_tw",
                    side_effect=convert,
                ),
            ):
                self.assertTrue(worker.process(video))

            self.assertEqual(translator.translate_blocks.call_count, 2)
            self.assertEqual(
                translator.retranslate_problem_blocks.call_count,
                2,
            )
            selective_retry.assert_called_once()
            self.assertEqual(
                read_srt(paths.zh_cn_srt)[0].text,
                ["final targeted repair"],
            )
            self.assertFalse(
                translation_quality_events_path(paths.zh_cn_srt).exists()
            )
            self.assertFalse(
                translation_quality_hold_path(paths.zh_cn_srt).exists()
            )

    def test_process_keeps_japanese_srt_after_exporting_ass_when_intermediates_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, export_ai_ass=True, keep_intermediate_files=False)
            worker = VideoWorker(config, _logger())
            source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]
            translator = Mock()

            def transcribe(_audio_path: Path, ja_srt: Path) -> None:
                write_srt(ja_srt, source_blocks)

            def translate_blocks(_blocks: list[SrtBlock], _ja_srt: Path, zh_cn_srt: Path) -> None:
                write_srt(zh_cn_srt, [SrtBlock(1, source_blocks[0].timing, ["translated"])])

            translator.translate_blocks.side_effect = translate_blocks
            with (
                patch("worker.extract_audio", return_value=None),
                patch.object(worker, "_transcribe", side_effect=transcribe),
                patch.object(worker, "_postprocess_ja_srt", return_value=source_blocks),
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_convert_to_zh_tw", side_effect=lambda _src, dst: write_srt(dst, source_blocks)),
            ):
                worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            paths = paths_for_video(video, config)
            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(paths.ai_ja_ass.exists())
            self.assertTrue(paths.ai_zh_cn_ass.exists())
            self.assertTrue(paths.ai_zh_tw_ass.exists())
            self.assertFalse(Path(str(paths.ai_zh_tw_ass) + ".quality.json").exists())
            self.assertTrue(managed_quality_report_path(paths.ai_zh_tw_ass, config.work_path).exists())

    def test_quality_check_failure_marks_bad_ai_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, export_ai_ass=True, keep_intermediate_files=True, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            paths.ai_zh_tw_ass.write_text(
                "\n".join(
                    [
                        "[Events]",
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
                        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,字幕製作人 初音未來",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            write_srt(paths.zh_cn_srt, [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["bad cache"])])
            write_srt(paths.zh_tw_srt, [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["bad cache"])])

            with self.assertRaises(SubtitleQualityError):
                worker._quality_check_ai_outputs(video, [(paths.ai_zh_tw_ass, "translated")])

            self.assertFalse(paths.ai_zh_tw_ass.exists())
            self.assertFalse(Path(str(paths.ai_zh_tw_ass) + ".quality.json").exists())
            self.assertFalse(managed_quality_report_path(paths.ai_zh_tw_ass, config.work_path).exists())
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            archive = root / "failed_ai_quality"
            self.assertEqual(len(list(archive.glob("*.ass"))), 1)
            self.assertEqual(len(list(archive.glob("*.quality.json"))), 1)

    def test_quality_disabled_cannot_bypass_translation_failure_event_or_hold(self) -> None:
        for case in ("failure_event", "pending_hold"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                config = _config(
                    root,
                    subtitle_quality_check_enabled=False,
                    scanner_cache_enabled=False,
                )
                worker = VideoWorker(config, _logger())
                paths = paths_for_video(video, config)
                write_srt(
                    paths.zh_cn_srt,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,000 --> 00:00:01,000",
                            ["blocked output"],
                        )
                    ],
                )
                if case == "failure_event":
                    write_translation_quality_events(
                        paths.zh_cn_srt,
                        [
                            {
                                "code": "translation_safe_omission",
                                "severity": "fail",
                                "index": 1,
                                "reason": "must remain a hard gate",
                            }
                        ],
                    )
                else:
                    write_translation_quality_hold(
                        paths.zh_cn_srt,
                        srt_sha256=sha256_file(paths.zh_cn_srt),
                        reason="derived output commit is pending",
                    )

                with self.assertRaises(SubtitleQualityError):
                    worker._quality_check_ai_outputs(video, [])

    def test_staged_translated_roles_cannot_bypass_hard_translation_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, subtitle_quality_check_enabled=True)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, "00:00:00,000 --> 00:00:01,000", ["blocked output"])],
            )
            write_translation_quality_events(
                paths.zh_cn_srt,
                [
                    {
                        "code": "translation_safe_omission",
                        "severity": "fail",
                        "index": 1,
                        "reason": "must remain a hard gate",
                    }
                ],
            )
            staged = root / "staged.zh-CN.ass"
            staged.write_text("not inspected because durable event blocks first", encoding="utf-8")

            with self.assertRaisesRegex(SubtitleQualityError, r"indexes=\[1\]"):
                worker._quality_check_ai_outputs(
                    video,
                    [(staged, "translated_zh_cn")],
                    discard_on_failure=False,
                    persist_reports=False,
                )

    def test_translation_cache_chain_invalidates_partial_or_timing_mismatched_zh_cn(self) -> None:
        for case in ("partial", "timing_mismatch"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                config = _config(root, scanner_cache_enabled=False)
                worker = VideoWorker(config, _logger())
                paths = paths_for_video(video, config)
                source = [
                    SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["one"]),
                    SrtBlock(2, "00:00:03,000 --> 00:00:04,000", ["two"]),
                ]
                write_srt(paths.ja_srt, source)
                translated = [
                    SrtBlock(
                        1,
                        (
                            "00:00:01,100 --> 00:00:02,000"
                            if case == "timing_mismatch"
                            else source[0].timing
                        ),
                        ["translated one"],
                    )
                ]
                if case == "timing_mismatch":
                    translated.append(
                        SrtBlock(2, source[1].timing, ["translated two"])
                    )
                write_srt(paths.zh_cn_srt, translated)
                write_srt(paths.zh_tw_srt, translated)
                write_translation_quality_events(
                    paths.zh_cn_srt,
                    [
                        {
                            "code": "translation_safe_omission",
                            "severity": "fail",
                            "index": 1,
                        }
                    ],
                )

                worker._validate_translation_cache_chain(video, paths)

                self.assertTrue(paths.ja_srt.exists())
                self.assertFalse(paths.zh_cn_srt.exists())
                self.assertFalse(paths.zh_tw_srt.exists())
                self.assertFalse(
                    translation_quality_events_path(paths.zh_cn_srt).exists()
                )

    def test_translation_cache_chain_removes_orphan_zh_tw_before_new_zh_cn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])],
            )
            write_srt(
                paths.zh_tw_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["orphan"])],
            )

            worker._validate_translation_cache_chain(video, paths)

            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())

    def test_orphan_zh_tw_delete_failure_aborts_while_zh_cn_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])],
            )
            write_srt(
                paths.zh_tw_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["orphan"])],
            )
            real_unlink = Path.unlink

            def fail_only_zh_tw(path, *args, **kwargs):
                if Path(path) == paths.zh_tw_srt:
                    raise OSError("orphan zh-TW is locked")
                return real_unlink(path, *args, **kwargs)

            with (
                patch.object(Path, "unlink", new=fail_only_zh_tw),
                self.assertRaisesRegex(OSError, "orphan zh-TW is locked"),
            ):
                worker._validate_translation_cache_chain(video, paths)

            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertTrue(paths.zh_tw_srt.exists())

    def test_translation_cache_chain_rejects_mismatched_zh_tw(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [
                SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])
            ]
            write_srt(paths.ja_srt, source)
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, source[0].timing, ["simplified"])],
            )
            write_srt(
                paths.zh_tw_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:01,100 --> 00:00:02,000",
                        ["stale traditional"],
                    )
                ],
            )

            worker._validate_translation_cache_chain(video, paths)

            self.assertTrue(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())

    def test_cached_safe_omission_retry_retranslates_only_rejected_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [
                SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"]),
                SrtBlock(2, "00:00:03,000 --> 00:00:04,000", ["source two"]),
                SrtBlock(3, "00:00:05,000 --> 00:00:06,000", ["source three"]),
            ]
            write_srt(paths.ja_srt, source)
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(1, source[0].timing, ["translated one"]),
                    SrtBlock(2, source[1].timing, ["……"]),
                    SrtBlock(3, source[2].timing, ["translated three"]),
                ],
            )
            write_srt(paths.zh_tw_srt, [SrtBlock(1, source[0].timing, ["stale traditional"])])
            write_translation_quality_events(
                paths.zh_cn_srt,
                [
                    {
                        "code": "translation_safe_omission",
                        "severity": "fail",
                        "index": 2,
                        "source": "source two",
                        "output": "bad",
                        "reason": "malformed output",
                    }
                ],
            )
            scope = worker._translation_memory_scope(video)
            write_translation_memory_origin(
                root,
                paths.zh_cn_srt,
                source_srt_path=paths.ja_srt,
                source_srt_sha256=sha256_file(paths.ja_srt),
                target_srt_sha256=sha256_file(paths.zh_cn_srt),
                split_decision_digest="a" * 64,
                cached_indexes=(1,),
                translation_lineage_mode="tm_split",
                scope=scope,
            )
            translator = Mock()

            def repair(blocks, _source_path, output_path, *, series_glossary):
                self.assertEqual([block.index for block in blocks], [2])
                self.assertEqual(series_glossary, {})
                write_srt(
                    output_path,
                    [SrtBlock(2, source[1].timing, ["translated two"])],
                )

            translator.retranslate_problem_blocks.side_effect = repair
            metadata = MetadataContext(
                "Anime",
                "anilist",
                "Characters: Koyomi Araragi, 阿良々木暦",
                cached=True,
            )
            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(
                    worker,
                    "_build_series_metadata_context",
                    return_value=metadata,
                ),
            ):
                repaired = worker._repair_cached_translation_safe_omissions(video, paths)

            self.assertTrue(repaired)
            context_call = translator.set_targeted_repair_context.call_args
            self.assertEqual(context_call.args[0], source)
            self.assertEqual(
                [" ".join(block.text) for block in context_call.args[1]],
                ["translated one", "……", "translated three"],
            )
            self.assertEqual(context_call.args[2], {2})
            self.assertEqual(context_call.kwargs["series_context"], metadata.text)
            self.assertEqual(
                [" ".join(block.text) for block in read_srt(paths.zh_cn_srt)],
                ["translated one", "translated two", "translated three"],
            )
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(translation_quality_events_path(paths.zh_cn_srt).exists())
            self.assertTrue(translation_quality_hold_path(paths.zh_cn_srt).exists())
            rebound = read_translation_memory_origin_strict(root, paths.zh_cn_srt)
            self.assertEqual(rebound.cached_indexes, (1,))
            self.assertEqual(rebound.target_srt_sha256, sha256_file(paths.zh_cn_srt))
            write_srt(paths.zh_tw_srt, read_srt(paths.zh_cn_srt))
            worker._finalize_translation_cache_commit(paths)
            self.assertFalse(translation_quality_events_path(paths.zh_cn_srt).exists())
            self.assertFalse(translation_quality_hold_path(paths.zh_cn_srt).exists())

    def test_safe_omission_cache_rejects_matching_indexes_with_wrong_timings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])],
            )
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:01,100 --> 00:00:02,000",
                        ["bad timing"],
                    )
                ],
            )
            write_translation_quality_events(
                paths.zh_cn_srt,
                [
                    {
                        "code": "translation_safe_omission",
                        "severity": "fail",
                        "index": 1,
                    }
                ],
            )
            translator = Mock()

            with patch.object(worker, "_get_translator", return_value=translator):
                repaired = worker._repair_cached_translation_safe_omissions(
                    video,
                    paths,
                )

            self.assertFalse(repaired)
            translator.retranslate_problem_blocks.assert_not_called()
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(
                translation_quality_events_path(paths.zh_cn_srt).exists()
            )

    def test_cached_safe_omission_sidecar_is_removed_when_translation_cache_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["cached"])],
            )
            write_translation_quality_events(
                paths.zh_cn_srt,
                [{"code": "translation_safe_omission", "severity": "fail", "index": 1}],
            )
            paths.zh_cn_srt.unlink()

            self.assertFalse(worker._repair_cached_translation_safe_omissions(video, paths))
            self.assertFalse(translation_quality_events_path(paths.zh_cn_srt).exists())

    def test_corrupt_translation_event_sidecar_invalidates_cached_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])],
            )
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["cached"])],
            )
            write_srt(
                paths.zh_tw_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["cached traditional"])],
            )
            event_path = translation_quality_events_path(paths.zh_cn_srt)
            event_path.write_text("{not-json", encoding="utf-8")

            self.assertFalse(
                worker._repair_cached_translation_safe_omissions(video, paths)
            )
            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertFalse(event_path.exists())

    def test_unreadable_translation_event_sidecar_invalidates_cached_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])],
            )
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["cached"])],
            )
            event_path = translation_quality_events_path(paths.zh_cn_srt)
            event_path.write_text("{}", encoding="utf-8")

            with patch(
                "worker.read_translation_quality_events_strict",
                side_effect=OSError("permission denied"),
            ):
                self.assertFalse(
                    worker._repair_cached_translation_safe_omissions(video, paths)
                )

            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(event_path.exists())

    def test_cached_safe_omission_merge_sidecar_failure_invalidates_new_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [
                SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"]),
                SrtBlock(2, "00:00:03,000 --> 00:00:04,000", ["source two"]),
            ]
            write_srt(paths.ja_srt, source)
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(1, source[0].timing, ["translated one"]),
                    SrtBlock(2, source[1].timing, ["bad"]),
                ],
            )
            write_srt(
                paths.zh_tw_srt,
                [
                    SrtBlock(1, source[0].timing, ["traditional one"]),
                    SrtBlock(2, source[1].timing, ["bad traditional"]),
                ],
            )
            write_translation_quality_events(
                paths.zh_cn_srt,
                [
                    {
                        "code": "translation_safe_omission",
                        "severity": "fail",
                        "index": 2,
                        "source": "source two",
                        "output": "bad",
                        "reason": "malformed output",
                    }
                ],
            )
            translator = Mock()

            def repair(blocks, _source_path, output_path, *, series_glossary):
                self.assertEqual([block.index for block in blocks], [2])
                write_srt(
                    output_path,
                    [SrtBlock(2, source[1].timing, ["translated two"])],
                )

            translator.retranslate_problem_blocks.side_effect = repair
            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch(
                    "worker.write_translation_quality_hold",
                    side_effect=OSError("sidecar disk full"),
                ),
                self.assertRaisesRegex(OSError, "sidecar disk full"),
            ):
                worker._repair_cached_translation_safe_omissions(video, paths)

            self.assertTrue(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(translation_quality_events_path(paths.zh_cn_srt).exists())
            self.assertEqual(
                read_srt(paths.zh_cn_srt)[1].text,
                ["bad"],
            )
            worker._close_stage_state()

    def test_cached_safe_omission_keeps_rejection_when_stale_zh_tw_cannot_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, scanner_cache_enabled=False)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [
                SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"]),
                SrtBlock(2, "00:00:03,000 --> 00:00:04,000", ["source two"]),
            ]
            write_srt(paths.ja_srt, source)
            write_srt(
                paths.zh_cn_srt,
                [
                    SrtBlock(1, source[0].timing, ["translated one"]),
                    SrtBlock(2, source[1].timing, ["original rejected output"]),
                ],
            )
            write_srt(
                paths.zh_tw_srt,
                [
                    SrtBlock(1, source[0].timing, ["stale traditional one"]),
                    SrtBlock(2, source[1].timing, ["stale traditional two"]),
                ],
            )
            write_translation_quality_events(
                paths.zh_cn_srt,
                [
                    {
                        "code": "translation_safe_omission",
                        "severity": "fail",
                        "index": 2,
                        "source": "source two",
                        "output": "original rejected output",
                        "reason": "malformed output",
                    }
                ],
            )
            translator = Mock()

            def repair(_blocks, _source_path, output_path, *, series_glossary):
                write_srt(
                    output_path,
                    [SrtBlock(2, source[1].timing, ["translated two"])],
                )

            translator.retranslate_problem_blocks.side_effect = repair
            real_unlink = Path.unlink

            def fail_only_zh_tw_unlink(path, *args, **kwargs):
                if Path(path) == paths.zh_tw_srt:
                    raise OSError("traditional cache is locked")
                return real_unlink(path, *args, **kwargs)

            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(Path, "unlink", new=fail_only_zh_tw_unlink),
                self.assertRaisesRegex(OSError, "traditional cache is locked"),
            ):
                worker._repair_cached_translation_safe_omissions(video, paths)

            self.assertEqual(
                read_srt(paths.zh_cn_srt)[1].text,
                ["original rejected output"],
            )
            self.assertTrue(paths.zh_tw_srt.exists())
            retained_events = read_translation_quality_events_strict(
                paths.zh_cn_srt
            )
            self.assertEqual(
                [event["index"] for event in retained_events],
                [2],
            )

            with patch.object(worker, "_get_translator", return_value=translator):
                repaired = worker._repair_cached_translation_safe_omissions(
                    video,
                    paths,
                )

            self.assertTrue(repaired)
            self.assertEqual(translator.retranslate_problem_blocks.call_count, 2)
            self.assertEqual(read_srt(paths.zh_cn_srt)[1].text, ["translated two"])
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(translation_quality_events_path(paths.zh_cn_srt).exists())
            self.assertTrue(translation_quality_hold_path(paths.zh_cn_srt).exists())
            write_srt(paths.zh_tw_srt, read_srt(paths.zh_cn_srt))
            worker._finalize_translation_cache_commit(paths)
            self.assertFalse(translation_quality_events_path(paths.zh_cn_srt).exists())
            self.assertFalse(translation_quality_hold_path(paths.zh_cn_srt).exists())

    def test_repeated_safe_omission_escalates_same_line_to_prompt_free_asr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                translation_temperature=0.0,
                asr_selective_retry_enabled=True,
                asr_selective_retry_padding_seconds=1.5,
                whisper_initial_prompt="unsafe dialogue prompt",
                op_ed_initial_prompt="unsafe lyrics prompt",
                japanese_transcription_fallback_backend="faster-whisper",
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [
                SrtBlock(
                    1,
                    "00:00:10,000 --> 00:00:12,000",
                    ["same deterministic source"],
                )
            ]
            write_srt(paths.ja_srt, source)
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, source[0].timing, ["same malformed output"])],
            )
            write_translation_quality_events(
                paths.zh_cn_srt,
                [
                    {
                        "code": "translation_safe_omission",
                        "severity": "fail",
                        "index": 1,
                        "source": "same deterministic source",
                        "output": "same malformed output",
                        "reason": "identical deterministic malformed response",
                    }
                ],
            )
            translator = Mock()

            def repeat_same_omission(
                blocks,
                _source_path,
                output_path,
                *,
                series_glossary,
            ):
                self.assertEqual([block.index for block in blocks], [1])
                write_srt(
                    output_path,
                    [SrtBlock(1, source[0].timing, ["same malformed output"])],
                )
                write_translation_quality_events(
                    output_path,
                    [
                        {
                            "code": "translation_safe_omission",
                            "severity": "fail",
                            "index": 1,
                            "source": "same deterministic source",
                            "output": "same malformed output",
                            "reason": "identical deterministic malformed response",
                        }
                    ],
                )

            translator.retranslate_problem_blocks.side_effect = repeat_same_omission
            observed: dict[str, object] = {}

            def selective(
                _video,
                observed_audio,
                observed_paths,
                ranges,
                fallback_config,
                *,
                require_confidence,
                require_changed_transcript,
            ):
                observed["audio"] = observed_audio
                observed["ranges"] = ranges
                observed["prompt"] = fallback_config.whisper_initial_prompt
                observed["op_ed_prompt"] = fallback_config.op_ed_initial_prompt
                observed["require_confidence"] = require_confidence
                observed["require_changed"] = require_changed_transcript
                write_srt(
                    observed_paths.ja_srt,
                    [
                        SrtBlock(
                            1,
                            source[0].timing,
                            ["prompt-free ASR changed the source"],
                        )
                    ],
                )
                worker._invalidate_translation_intermediates(observed_paths)
                return True

            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_audio_path", return_value=audio),
                patch("worker.validate_cached_audio", return_value=True),
                patch.object(
                    worker,
                    "_try_prompt_free_selective_asr_repair",
                    side_effect=selective,
                ) as selective_retry,
                patch.object(worker, "_run_full_prompt_free_asr_fallback") as full,
            ):
                repaired = worker._repair_cached_translation_safe_omissions(
                    video,
                    paths,
                )

            self.assertTrue(repaired)
            selective_retry.assert_called_once()
            full.assert_not_called()
            self.assertEqual(observed["audio"], audio)
            self.assertEqual(observed["ranges"], [(8.5, 13.5)])
            self.assertIsNone(observed["prompt"])
            self.assertIsNone(observed["op_ed_prompt"])
            self.assertFalse(observed["require_confidence"])
            self.assertTrue(observed["require_changed"])
            self.assertEqual(
                read_srt(paths.ja_srt)[0].text,
                ["prompt-free ASR changed the source"],
            )
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(translation_quality_events_path(paths.zh_cn_srt).exists())

    def test_cached_asr_rejection_retries_recorded_ranges_without_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=True,
                whisper_initial_prompt="unsafe dialogue prompt",
                op_ed_initial_prompt="unsafe lyrics prompt",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                japanese_transcription_fallback_compute_type="float16",
                whisper_compute_type="float16",
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [SrtBlock(1, "00:00:10,000 --> 00:00:12,000", ["rejected line"])],
            )
            write_srt(
                paths.zh_cn_srt,
                [SrtBlock(1, "00:00:10,000 --> 00:00:12,000", ["stale translation"])],
            )
            write_srt(
                paths.zh_tw_srt,
                [SrtBlock(1, "00:00:10,000 --> 00:00:12,000", ["stale traditional"])],
            )
            diagnostic = asr_diagnostics_path(paths.ja_srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(
                json.dumps(
                    {
                        "status": "selective_retry_required",
                        "srt_path": str(paths.ja_srt),
                        "srt_sha256": sha256_file(paths.ja_srt),
                        "review_ranges": [[8.0, 14.0]],
                    }
                ),
                encoding="utf-8",
            )
            attach_asr_diagnostics_context(
                paths.ja_srt,
                config,
                media_path=video,
                audio_path=audio,
                audio_stream=None,
            )
            observed: dict[str, object] = {}

            def repair(_audio, srt_path, ranges, repair_config, _logger):
                observed["ranges"] = ranges
                observed["repair_prompt"] = repair_config.whisper_initial_prompt
                observed["repair_op_ed_prompt"] = repair_config.op_ed_initial_prompt
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:10,000 --> 00:00:12,000", ["repaired line"])],
                )

            def finalize(
                _audio,
                _srt,
                _ranges,
                final_config,
                _logger,
                *,
                segment_confidences,
                require_confidence,
            ):
                observed["final_prompt"] = final_config.whisper_initial_prompt
                observed["final_op_ed_prompt"] = final_config.op_ed_initial_prompt
                observed["segment_confidences"] = segment_confidences
                observed["require_confidence"] = require_confidence

            with (
                patch("worker.repair_low_confidence_ranges", side_effect=repair),
                patch("worker.finalize_repaired_transcription", side_effect=finalize),
                patch.object(worker, "_extract_preferred_audio") as extract,
            ):
                ready = worker._repair_cached_asr_rejection(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                )

            self.assertTrue(ready)
            self.assertEqual(observed["ranges"], [(8.0, 14.0)])
            self.assertIsNone(observed["repair_prompt"])
            self.assertIsNone(observed["repair_op_ed_prompt"])
            self.assertIsNone(observed["final_prompt"])
            self.assertIsNone(observed["final_op_ed_prompt"])
            self.assertEqual(observed["segment_confidences"], ())
            self.assertTrue(observed["require_confidence"])
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            extract.assert_not_called()

    def test_cached_asr_context_mismatch_or_spent_repair_uses_full_fallback(self) -> None:
        cases = (
            (
                "context_mismatch",
                (False, ["audio fingerprint mismatch"], {"audio_fingerprint": "current"}),
                True,
                "context no longer matches source",
            ),
            (
                "repair_already_attempted",
                (True, [], {"audio_fingerprint": "current"}),
                False,
                "already attempted",
            ),
        )
        for name, context_result, claim_result, expected_reason in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                audio = root / "audio.wav"
                audio.write_bytes(b"audio")
                config = _config(
                    root,
                    asr_diagnostics_enabled=True,
                    asr_selective_retry_enabled=True,
                    japanese_transcription_fallback_backend="faster-whisper",
                    japanese_transcription_fallback_model="fallback-model",
                    scanner_cache_enabled=False,
                )
                worker = VideoWorker(config, _logger())
                paths = paths_for_video(video, config)
                write_srt(
                    paths.ja_srt,
                    [SrtBlock(1, "00:00:10,000 --> 00:00:12,000", ["rejected cache"])],
                )
                diagnostic = asr_diagnostics_path(paths.ja_srt, config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "status": "selective_retry_required",
                            "srt_path": str(paths.ja_srt),
                            "srt_sha256": sha256_file(paths.ja_srt),
                            "review_ranges": [[8.0, 14.0]],
                            "reason_code": "asr_artifact",
                        }
                    ),
                    encoding="utf-8",
                )
                attach_asr_diagnostics_context(
                    paths.ja_srt,
                    config,
                    media_path=video,
                    audio_path=audio,
                    audio_stream=None,
                )

                with (
                    patch(
                        "worker.verify_asr_diagnostics_context",
                        return_value=context_result,
                    ),
                    patch(
                        "worker.claim_asr_repair_attempt",
                        return_value=claim_result,
                    ) as claim,
                    patch.object(
                        worker,
                        "_run_full_prompt_free_asr_fallback",
                        return_value=True,
                    ) as full,
                    patch("worker.repair_low_confidence_ranges") as selective,
                ):
                    ready = worker._repair_cached_asr_rejection(
                        video,
                        audio,
                        paths,
                        audio_ready=True,
                    )

                self.assertTrue(ready)
                full.assert_called_once()
                self.assertIn(expected_reason, full.call_args.kwargs["reason"])
                self.assertIsNone(
                    full.call_args.kwargs["fallback_config"].whisper_initial_prompt
                )
                selective.assert_not_called()
                if name == "context_mismatch":
                    claim.assert_not_called()
                else:
                    claim.assert_called_once()

    def test_accepted_asr_diagnostic_hash_mismatch_fails_closed_without_rerun(self) -> None:
        for case in ("missing_hash", "mismatched_hash"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                audio = root / "audio.wav"
                audio.write_bytes(b"audio")
                config = _config(
                    root,
                    asr_diagnostics_enabled=True,
                    japanese_transcription_fallback_backend="faster-whisper",
                    japanese_transcription_fallback_model="fallback-model",
                    scanner_cache_enabled=False,
                )
                worker = VideoWorker(config, _logger())
                paths = paths_for_video(video, config)
                write_srt(
                    paths.ja_srt,
                    [
                        SrtBlock(
                            1,
                            "00:00:10,000 --> 00:00:12,000",
                            ["stale accepted transcript"],
                        )
                    ],
                )
                payload = {
                    "status": "accepted",
                    "srt_path": str(paths.ja_srt),
                }
                if case == "mismatched_hash":
                    payload["srt_sha256"] = "0" * 64
                diagnostic = asr_diagnostics_path(paths.ja_srt, config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(json.dumps(payload), encoding="utf-8")

                with patch.object(
                    worker,
                    "_transcribe_with_config",
                ) as full:
                    with self.assertRaisesRegex(
                        AsrSelectiveRepairUnavailableError,
                        "refused fail-closed",
                    ):
                        worker._repair_cached_asr_rejection(
                            video,
                            audio,
                            paths,
                            audio_ready=True,
                        )

                full.assert_not_called()
                self.assertEqual(
                    read_srt(paths.ja_srt)[0].text,
                    ["stale accepted transcript"],
                )

    def test_cached_asr_rejection_untrusted_evidence_fails_closed_without_full_rerun(self) -> None:
        cases = (
            (
                "selective_disabled",
                {"asr_selective_retry_enabled": False},
                {"review_ranges": [[8.0, 14.0]], "include_hash": True},
            ),
            (
                "invalid_ranges",
                {"asr_selective_retry_enabled": True},
                {"review_ranges": [["bad"]], "include_hash": True},
            ),
            (
                "missing_hash",
                {"asr_selective_retry_enabled": True},
                {"review_ranges": [[8.0, 14.0]], "include_hash": False},
            ),
            (
                "unsupported_selective_backend",
                {
                    "asr_selective_retry_enabled": True,
                    "japanese_transcription_fallback_backend": "transformers-whisper",
                },
                {"review_ranges": [[8.0, 14.0]], "include_hash": True},
            ),
        )
        for name, config_overrides, diagnostic_case in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                audio = root / "audio.wav"
                audio.write_bytes(b"audio")
                settings = {
                    "asr_diagnostics_enabled": True,
                    "whisper_initial_prompt": "unsafe dialogue prompt",
                    "op_ed_initial_prompt": "unsafe lyrics prompt",
                    "japanese_transcription_fallback_backend": "faster-whisper",
                    "japanese_transcription_fallback_model": "fallback-model",
                    "scanner_cache_enabled": False,
                }
                settings.update(config_overrides)
                config = _config(root, **settings)
                worker = VideoWorker(config, _logger())
                paths = paths_for_video(video, config)
                write_srt(
                    paths.ja_srt,
                    [
                        SrtBlock(
                            1,
                            "00:00:10,000 --> 00:00:12,000",
                            ["rejected cached line"],
                        )
                    ],
                )
                payload = {
                    "status": "selective_retry_required",
                    "srt_path": str(paths.ja_srt),
                    "review_ranges": diagnostic_case["review_ranges"],
                    "reason_code": "asr_artifact",
                }
                if diagnostic_case["include_hash"]:
                    payload["srt_sha256"] = sha256_file(paths.ja_srt)
                diagnostic = asr_diagnostics_path(paths.ja_srt, config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(json.dumps(payload), encoding="utf-8")

                with (
                    patch.object(worker, "_transcribe_with_config") as full,
                    patch("worker.repair_low_confidence_ranges") as selective,
                ):
                    with self.assertRaises(AsrSelectiveRepairUnavailableError):
                        worker._repair_cached_asr_rejection(
                            video,
                            audio,
                            paths,
                            audio_ready=True,
                        )

                selective.assert_not_called()
                full.assert_not_called()
                self.assertEqual(read_srt(paths.ja_srt)[0].text, ["rejected cached line"])

    def test_cached_asr_selective_failure_runs_full_prompt_free_fallback(self) -> None:
        for failure_stage in ("repair", "final_validation"):
            with (
                self.subTest(failure_stage=failure_stage),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                audio = root / "audio.wav"
                audio.write_bytes(b"audio")
                config = _config(
                    root,
                    asr_diagnostics_enabled=True,
                    asr_selective_retry_enabled=True,
                    japanese_transcription_fallback_backend="faster-whisper",
                    japanese_transcription_fallback_model="fallback-model",
                    scanner_cache_enabled=False,
                )
                worker = VideoWorker(config, _logger())
                paths = paths_for_video(video, config)
                write_srt(
                    paths.ja_srt,
                    [
                        SrtBlock(
                            1,
                            "00:00:10,000 --> 00:00:12,000",
                            ["original rejected cache"],
                        )
                    ],
                )
                diagnostic = asr_diagnostics_path(paths.ja_srt, config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "status": "selective_retry_required",
                            "srt_path": str(paths.ja_srt),
                            "srt_sha256": sha256_file(paths.ja_srt),
                            "review_ranges": [[8.0, 14.0]],
                            "reason_code": "asr_artifact",
                        }
                    ),
                    encoding="utf-8",
                )
                attach_asr_diagnostics_context(
                    paths.ja_srt,
                    config,
                    media_path=video,
                    audio_path=audio,
                    audio_stream=None,
                )

                def repair(_audio, output, _ranges, _config, _logger):
                    if failure_stage == "repair":
                        raise TranscriptionError("selective repair failed")
                    write_srt(
                        output,
                        [
                            SrtBlock(
                                1,
                                "00:00:10,000 --> 00:00:12,000",
                                ["modified but rejected repair"],
                            )
                        ],
                    )
                    return SimpleNamespace(segment_confidences=())

                def finalize(*_args, **_kwargs):
                    if failure_stage == "final_validation":
                        raise TranscriptionError("final validation failed")

                def full_transcribe(_audio, output, _config):
                    write_srt(
                        output,
                        [
                            SrtBlock(
                                1,
                                "00:00:01,000 --> 00:00:02,000",
                                ["full fallback"],
                            )
                        ],
                    )

                with (
                    patch(
                        "worker.repair_low_confidence_ranges",
                        side_effect=repair,
                    ),
                    patch(
                        "worker.finalize_repaired_transcription",
                        side_effect=finalize,
                    ),
                    patch.object(
                        worker,
                        "_transcribe_with_config",
                        side_effect=full_transcribe,
                    ) as full,
                ):
                    ready = worker._repair_cached_asr_rejection(
                        video,
                        audio,
                        paths,
                        audio_ready=True,
                    )

                self.assertTrue(ready)
                full.assert_called_once()
                self.assertEqual(
                    read_srt(paths.ja_srt)[0].text,
                    ["full fallback"],
                )
                guarded = json.loads(diagnostic.read_text(encoding="utf-8"))
                self.assertEqual(guarded["status"], "accepted")

    def test_cached_low_confidence_repair_without_confidence_runs_full_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=True,
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            write_srt(
                paths.ja_srt,
                [
                    SrtBlock(
                        1,
                        "00:00:10,000 --> 00:00:12,000",
                        ["low confidence cache"],
                    )
                ],
            )
            diagnostic = asr_diagnostics_path(paths.ja_srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(
                json.dumps(
                    {
                        "status": "selective_retry_required",
                        "srt_path": str(paths.ja_srt),
                        "srt_sha256": sha256_file(paths.ja_srt),
                        "review_ranges": [[8.0, 14.0]],
                        "reason_code": "low_confidence",
                    }
                ),
                encoding="utf-8",
            )
            attach_asr_diagnostics_context(
                paths.ja_srt,
                config,
                media_path=video,
                audio_path=audio,
                audio_stream=None,
            )

            def repair(_audio, output, _ranges, _config, _logger):
                write_srt(
                    output,
                    [
                        SrtBlock(
                            1,
                            "00:00:10,000 --> 00:00:12,000",
                            ["selective without confidence"],
                        )
                    ],
                )
                return SimpleNamespace(segment_confidences=())

            def full_transcribe(_audio, output, _config):
                write_srt(
                    output,
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:02,000",
                            ["confidence-checked full fallback"],
                        )
                    ],
                )

            with (
                patch(
                    "worker.repair_low_confidence_ranges",
                    side_effect=repair,
                ),
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=full_transcribe,
                ) as full,
            ):
                ready = worker._repair_cached_asr_rejection(
                    video,
                    audio,
                    paths,
                    audio_ready=True,
                )

            self.assertTrue(ready)
            full.assert_called_once()
            self.assertEqual(
                read_srt(paths.ja_srt)[0].text,
                ["confidence-checked full fallback"],
            )

    def test_quality_reviews_resolve_only_after_verified_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            with (
                patch("worker.has_ai_finished_subtitle", return_value=True) as finished,
                patch(
                    "worker.resolve_ai_quality_reviews_for_target_if_idle",
                    return_value=["review_quality"],
                ) as resolve,
            ):
                self.assertEqual(
                    worker._resolve_completed_ai_quality_reviews(
                        video,
                        ProcessOutcome("quality_check", "failed", "not published"),
                    ),
                    [],
                )
                resolve.assert_not_called()
                self.assertEqual(
                    worker._resolve_completed_ai_quality_reviews(
                        video,
                        ProcessOutcome(),
                    ),
                    ["review_quality"],
                )
                self.assertEqual(
                    worker._resolve_completed_ai_quality_reviews(
                        video,
                        ProcessOutcome(
                            "source_translation",
                            "ok",
                            "Traditional-Chinese delivery completed from language=en",
                        ),
                    ),
                    ["review_quality"],
                )

            self.assertEqual(finished.call_count, 2)
            finished.assert_called_with(video, config)
            resolution = resolve.call_args.args[2]
            self.assertEqual(resolution["source"], "worker")
            self.assertEqual(
                resolution["reason"],
                "quality_gate_and_publication_succeeded",
            )

    def test_complete_outcome_does_not_resolve_reviews_without_verified_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            with (
                patch("worker.has_ai_finished_subtitle", return_value=False),
                patch("worker.resolve_ai_quality_reviews_for_target_if_idle") as resolve,
            ):
                self.assertEqual(
                    worker._resolve_completed_ai_quality_reviews(video, ProcessOutcome()),
                    [],
                )
            resolve.assert_not_called()

    def test_startup_review_reconciliation_requires_newer_strict_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            manifest = root / "manifest.json"
            config = _config(root, input_path=root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            targets = [{"target_key": str(video), "latest_review_at": 100.0, "review_count": 1}]

            manifest.write_text(json.dumps({"delivery": {"verified_at": 90.0}}), encoding="utf-8")
            with (
                patch("worker.list_open_ai_quality_review_targets", return_value=targets),
                patch("worker.output_manifest_path", return_value=manifest),
                patch("worker.has_ai_finished_subtitle") as finished,
                patch("worker.resolve_ai_quality_reviews_for_target_if_idle") as resolve,
            ):
                stale = worker.reconcile_published_ai_quality_reviews()
            self.assertEqual(stale["stale"], 1)
            finished.assert_not_called()
            resolve.assert_not_called()

            manifest.write_text(json.dumps({"delivery": {"verified_at": 110.0}}), encoding="utf-8")
            with (
                patch("worker.list_open_ai_quality_review_targets", return_value=targets),
                patch("worker.output_manifest_path", return_value=manifest),
                patch("worker.has_ai_finished_subtitle", return_value=True) as finished,
                patch(
                    "worker.resolve_ai_quality_reviews_for_target_if_idle",
                    return_value=["review_quality"],
                ) as resolve,
            ):
                reconciled = worker.reconcile_published_ai_quality_reviews()
            self.assertEqual(reconciled["resolved"], 1)
            finished.assert_called_once_with(video.resolve(), config)
            resolution = resolve.call_args.args[2]
            self.assertEqual(resolution["source"], "worker_startup")
            self.assertEqual(resolution["manifest_verified_at"], 110.0)

    def test_quality_error_is_not_classified_as_transcription_failure(self) -> None:
        self.assertEqual(
            VideoWorker._stage_for_exception(SubtitleQualityError("quality rejected")),
            "quality_check",
        )

    def test_exhausted_low_confidence_asr_is_classified_for_review_through_exception_chain(self) -> None:
        direct = LowConfidenceTranscriptionError(
            "prompt-free ASR remained unreliable",
            [(348.5, 353.8)],
        )
        caused = TranscriptionError("full ASR fallback failed")
        caused.__cause__ = LowConfidenceTranscriptionError(
            "fallback low confidence",
            [(10.0, 20.0)],
        )
        contextual = TranscriptionError("ASR recovery wrapper")
        contextual.__context__ = LowConfidenceTranscriptionError(
            "context low confidence",
            [(30.0, 40.0)],
        )

        for error in (direct, caused, contextual):
            with self.subTest(error=str(error)):
                self.assertTrue(VideoWorker._requires_asr_review(error))
                self.assertEqual(VideoWorker._stage_for_exception(error), "transcription_review")

        transient = TranscriptionError("CUDA failed with error out of memory")
        self.assertFalse(VideoWorker._requires_asr_review(transient))
        self.assertEqual(VideoWorker._stage_for_exception(transient), "transcription")

        transient_with_low_confidence_context = TranscriptionError(
            "CUDA failed with error out of memory"
        )
        transient_with_low_confidence_context.__context__ = LowConfidenceTranscriptionError(
            "primary low confidence",
            [(50.0, 60.0)],
        )
        self.assertFalse(
            VideoWorker._requires_asr_review(transient_with_low_confidence_context)
        )
        self.assertEqual(
            VideoWorker._stage_for_exception(transient_with_low_confidence_context),
            "transcription",
        )

    def test_process_creates_asr_review_for_wrapped_exhausted_low_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            worker = VideoWorker(_config(root, log_path=root), _logger())
            error = TranscriptionError("full prompt-free ASR fallback failed")
            error.__cause__ = LowConfidenceTranscriptionError(
                "prompt-free ASR remained unreliable",
                [(348.5, 353.8)],
            )
            provenance = Mock()

            with (
                patch("worker.VideoLock") as video_lock,
                patch("worker.ProvenanceRecorder", return_value=provenance),
                patch.object(worker, "_process_locked", side_effect=error),
                patch.object(worker, "_archive_asr_review_outputs") as archive,
                patch.object(worker, "_create_review_item") as create_review,
                patch.object(worker, "_set_stage") as set_stage,
                patch.object(worker, "_cleanup_audio_files"),
                patch.object(worker, "_close_stage_state"),
                patch("worker.mark_ai_failure") as mark_failure,
                patch("worker.log_failure"),
                patch("worker.notify_event"),
            ):
                video_lock.return_value.acquire.return_value = True
                self.assertFalse(worker.process(video))

            archive.assert_called_once_with(
                video,
                reason="deterministic_asr_quality_review",
            )
            create_review.assert_called_once()
            self.assertEqual(create_review.call_args.kwargs["kind"], "asr_quality")
            self.assertEqual(
                create_review.call_args.kwargs["diagnosis"]["stage"],
                "transcription_review",
            )
            self.assertEqual(
                create_review.call_args.kwargs["diagnosis"]["reason"],
                "deterministic_asr_quality_review",
            )
            diagnosis = create_review.call_args.kwargs["diagnosis"]
            self.assertEqual(diagnosis["reason_code"], "low_confidence")
            self.assertEqual(diagnosis["failure_code"], "low_confidence")
            self.assertEqual(diagnosis["review_ranges"], [[348.5, 353.8]])
            self.assertEqual(
                diagnosis["media_fingerprint"]["digest"],
                sha256_file(video),
            )
            candidate = create_review.call_args.kwargs["candidates"][0]
            self.assertEqual(candidate["strategy"], "full_transcription_rerun")
            self.assertFalse(candidate["selective"])
            self.assertIn("full re-transcription", candidate["label"])
            set_stage.assert_any_call(
                video,
                "transcription_review",
                "failed",
                "full prompt-free ASR fallback failed",
            )
            mark_failure.assert_not_called()

    def test_asr_review_archives_all_dependent_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            outputs = (
                paths.ja_srt,
                paths.zh_cn_srt,
                paths.zh_tw_srt,
                paths.ai_ja_ass,
                paths.ai_zh_cn_ass,
                paths.ai_zh_tw_ass,
            )
            for path in outputs:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("bad", encoding="utf-8")
            diagnostic = asr_diagnostics_path(paths.ja_srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text('{"status":"accepted"}', encoding="utf-8")

            worker._archive_asr_review_outputs(video)

            self.assertFalse(any(path.exists() for path in outputs))
            self.assertFalse(diagnostic.exists())
            manifests = list((root / "asr_review_archive").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["reason"], "translator_requested_fresh_asr")
            self.assertEqual(VideoWorker._stage_for_exception(AsrReviewError("bad ASR")), "transcription_review")

    def test_asr_review_archive_failure_retains_source_and_records_partial_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, export_ai_ass=True)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            paths.ja_srt.parent.mkdir(parents=True, exist_ok=True)
            paths.ja_srt.write_text("source transcript", encoding="utf-8")

            with patch("worker.verified_move", side_effect=OSError("archive unavailable")):
                with self.assertRaisesRegex(OSError, "archive is incomplete"):
                    worker._archive_asr_review_outputs(video)

            self.assertTrue(paths.ja_srt.is_file())
            manifests = list((root / "asr_review_archive").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "partial")
            self.assertEqual(len(manifest["failures"]), 1)

    def test_process_creates_ai_subtitles_when_official_chinese_exists_and_ai_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            (root / "Anime S01E01.official.zh-TW.ass").write_text("official", encoding="utf-8")
            config = _config(root, export_ai_ass=True, require_ai_subtitles=True)
            worker = VideoWorker(config, _logger())
            source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]
            translator = Mock()

            def transcribe(_audio_path: Path, ja_srt: Path) -> None:
                write_srt(ja_srt, source_blocks)

            def translate_blocks(_blocks: list[SrtBlock], _ja_srt: Path, zh_cn_srt: Path) -> None:
                write_srt(zh_cn_srt, [SrtBlock(1, source_blocks[0].timing, ["translated"])])

            translator.translate_blocks.side_effect = translate_blocks
            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch.object(worker, "_transcribe", side_effect=transcribe),
                patch.object(worker, "_postprocess_ja_srt", return_value=source_blocks),
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_convert_to_zh_tw", side_effect=lambda _src, dst: write_srt(dst, source_blocks)),
            ):
                worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            paths = paths_for_video(video, config)
            extract_audio_mock.assert_called_once()
            self.assertTrue(paths.ai_zh_tw_ass.exists())
            self.assertTrue((root / "Anime S01E01.official.zh-TW.ass").exists())

    def test_process_creates_ai_subtitles_when_official_chinese_exists_and_force_ai_is_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            (root / "Anime S01E01.official.zh-TW.ass").write_text("official", encoding="utf-8")
            config = _config(root, export_ai_ass=True, require_ai_subtitles=False, scanner_state_path=root / "scanner_state.sqlite3")
            state = ScanStateStore.from_config(config)
            try:
                state.force_ai_queue_candidate(video)
                state.commit()
            finally:
                state.close()

            worker = VideoWorker(config, _logger())
            source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]
            translator = Mock()

            def transcribe(_audio_path: Path, ja_srt: Path) -> None:
                write_srt(ja_srt, source_blocks)

            def translate_blocks(_blocks: list[SrtBlock], _ja_srt: Path, zh_cn_srt: Path) -> None:
                write_srt(zh_cn_srt, [SrtBlock(1, source_blocks[0].timing, ["translated"])])

            translator.translate_blocks.side_effect = translate_blocks
            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch.object(worker, "_transcribe", side_effect=transcribe),
                patch.object(worker, "_postprocess_ja_srt", return_value=source_blocks),
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_convert_to_zh_tw", side_effect=lambda _src, dst: write_srt(dst, source_blocks)),
            ):
                worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            paths = paths_for_video(video, config)
            extract_audio_mock.assert_called_once()
            self.assertTrue(paths.ai_zh_tw_ass.exists())
            self.assertTrue((root / "Anime S01E01.official.zh-TW.ass").exists())

    def test_restyle_existing_ai_ass_renames_spaced_legacy_ai_suffix_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                export_ai_ass=True,
                ass_primary_font_size=58,
                ass_secondary_font_size=32,
                ass_primary_outline=2.2,
                ass_secondary_outline=1.4,
                ass_margin_v=70,
            )
            paths = paths_for_video(video, config)
            legacy_ai = root / "Anime S01E01.AI繁 日 雙 語 .zh-TW.ass"
            human_ass = root / "Anime S01E01.繁 體 中 文 .zh-TW.ass"
            content = (
                "[Script Info]\n"
                "PlayResX: 1920\n"
                "PlayResY: 1080\n"
                "[V4+ Styles]\n"
                "Style: Default,Noto Sans CJK TC,44,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1.6,0,2,40,40,54,1\n"
                "[Events]\n"
                r"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,繁體中文\N{\fs25\c&HE6E6E6&\alpha&H18&\bord1\shad0}日本語"
                "\n"
            )
            legacy_ai.write_text(content, encoding="utf-8-sig")
            human_ass.write_text(content, encoding="utf-8-sig")
            worker = VideoWorker(config, _logger())

            refreshed = worker.refresh_ass(video)

            self.assertTrue(refreshed)
            self.assertFalse(legacy_ai.exists())
            self.assertTrue(paths.ai_zh_tw_ass.exists())
            self.assertIn("Style: Default,Noto Sans CJK TC,58", paths.ai_zh_tw_ass.read_text(encoding="utf-8-sig"))
            self.assertTrue(human_ass.exists())
            self.assertIn("Style: Default,Noto Sans CJK TC,44", human_ass.read_text(encoding="utf-8-sig"))

    def test_normalize_existing_ai_ass_names_prefers_role_label_over_trailing_language(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, export_ai_ass=True)
            paths = paths_for_video(video, config)
            legacy_ja = root / "Anime S01E01.AI日本語.日本語.ja.ass"
            legacy_zh_cn = root / "Anime S01E01.AI简日双语.日本語.ja.ass"
            legacy_zh_tw = root / "Anime S01E01.AI繁日雙語.日本語.ja.ass"
            for path in (legacy_ja, legacy_zh_cn, legacy_zh_tw):
                path.write_text("[Script Info]\n", encoding="utf-8-sig")
            worker = VideoWorker(config, _logger())

            normalized = worker._normalize_existing_ai_ass_names(paths)

            self.assertEqual(normalized, 3)
            self.assertFalse(legacy_ja.exists())
            self.assertFalse(legacy_zh_cn.exists())
            self.assertFalse(legacy_zh_tw.exists())
            self.assertTrue(paths.ai_ja_ass.exists())
            self.assertTrue(paths.ai_zh_cn_ass.exists())
            self.assertTrue(paths.ai_zh_tw_ass.exists())

    def test_normalize_conflicting_legacy_ai_ass_archives_instead_of_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(root, export_ai_ass=True)
            paths = paths_for_video(video, config)
            legacy = root / "Anime S01E01.AI.zh-TW.legacy.ass"
            legacy.write_text("legacy-content", encoding="utf-8")
            paths.ai_zh_tw_ass.write_text("canonical-content", encoding="utf-8")
            worker = VideoWorker(config, _logger())

            normalized = worker._normalize_existing_ai_ass_names(paths)

            self.assertEqual(normalized, 1)
            self.assertFalse(legacy.exists())
            self.assertEqual(paths.ai_zh_tw_ass.read_text(encoding="utf-8"), "canonical-content")
            archived = list((root / "ai_legacy_duplicates").rglob("*.ass"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_text(encoding="utf-8"), "legacy-content")
            metadata = json.loads(archived[0].with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["source"], str(legacy))
            self.assertEqual(metadata["canonical"], str(paths.ai_zh_tw_ass))

    def test_process_skips_non_allowed_language_before_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
            )
            worker = VideoWorker(config, _logger())

            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch.object(
                    worker,
                    "_detect_source_language",
                    return_value=LanguageDetectionResult("en", 0.95, False, True, "test"),
                ),
                patch.object(worker, "_transcribe") as transcribe_mock,
                patch.object(worker, "_get_translator") as translator_mock,
            ):
                outcome = worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            extract_audio_mock.assert_called_once()
            transcribe_mock.assert_not_called()
            translator_mock.assert_not_called()
            self.assertEqual(outcome.stage, "language_skip")
            self.assertEqual(outcome.status, "skipped")
            self.assertIn("language=en", outcome.message)

    def test_process_skips_uncertain_language_before_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Unknown Audio S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                language_uncertain_policy="skip",
            )
            worker = VideoWorker(config, _logger())

            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch.object(
                    worker,
                    "_detect_source_language",
                    return_value=LanguageDetectionResult("unknown", 0.52, False, False, "test", reason="language_uncertain"),
                ),
                patch.object(worker, "_transcribe") as transcribe_mock,
                patch.object(worker, "_get_translator") as translator_mock,
            ):
                outcome = worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            extract_audio_mock.assert_called_once()
            transcribe_mock.assert_not_called()
            translator_mock.assert_not_called()
            self.assertEqual(outcome.stage, "language_uncertain")
            self.assertEqual(outcome.status, "skipped")
            self.assertIn("reason=language_uncertain", outcome.message)

    def test_process_rejects_uncertain_unknown_without_japanese_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Unknown Audio S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                language_uncertain_policy="continue",
                enable_vocal_separation=False,
            )
            worker = VideoWorker(config, _logger())

            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch("worker.probe_audio_streams", return_value=[]),
                patch.object(
                    worker,
                    "_detect_source_language",
                    return_value=LanguageDetectionResult(
                        "unknown",
                        0.52,
                        False,
                        False,
                        "test",
                        reason="language_uncertain",
                    ),
                ),
                patch("worker.has_japanese_audio_stream", return_value=False),
                patch.object(worker, "_transcribe") as transcribe_mock,
                patch.object(worker, "_get_translator") as translator_mock,
            ):
                outcome = worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            extract_audio_mock.assert_called_once()
            transcribe_mock.assert_not_called()
            translator_mock.assert_not_called()
            self.assertEqual(outcome.stage, "language_uncertain")
            self.assertEqual(outcome.status, "skipped")
            self.assertIn("decision=insufficient_japanese_evidence", outcome.message)

    def test_process_continues_uncertain_detector_japanese_to_japanese_asr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Low Confidence Japanese S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                language_uncertain_policy="continue",
                enable_vocal_separation=False,
            )
            worker = VideoWorker(config, _logger())

            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch("worker.probe_audio_streams", return_value=[]),
                patch.object(
                    worker,
                    "_detect_source_language",
                    return_value=LanguageDetectionResult(
                        "ja",
                        0.52,
                        False,
                        False,
                        "test",
                        reason="language_uncertain",
                    ),
                ),
                patch("worker.has_japanese_audio_stream") as metadata_mock,
                patch.object(
                    worker,
                    "_transcribe",
                    side_effect=TranscriptionError("continued to Japanese ASR"),
                ) as transcribe_mock,
                patch.object(worker, "_get_translator") as translator_mock,
            ):
                with self.assertRaisesRegex(TranscriptionError, "continued to Japanese ASR"):
                    worker._process_locked(video, root / "audio.wav", root / "vocals.wav")
                worker._close_stage_state()

            extract_audio_mock.assert_called_once()
            transcribe_mock.assert_called_once()
            translator_mock.assert_not_called()
            metadata_mock.assert_not_called()

    def test_process_continues_uncertain_unknown_with_japanese_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Metadata Japanese S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                language_uncertain_policy="continue",
                enable_vocal_separation=False,
            )
            worker = VideoWorker(config, _logger())

            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch("worker.probe_audio_streams", return_value=[]),
                patch("worker.has_japanese_audio_stream", return_value=True) as metadata_mock,
                patch.object(
                    worker,
                    "_detect_source_language",
                    return_value=LanguageDetectionResult(
                        "unknown",
                        0.52,
                        False,
                        False,
                        "test",
                        reason="language_uncertain",
                    ),
                ),
                patch.object(
                    worker,
                    "_transcribe",
                    side_effect=TranscriptionError("continued to Japanese ASR"),
                ) as transcribe_mock,
                patch.object(worker, "_get_translator") as translator_mock,
            ):
                with self.assertRaisesRegex(TranscriptionError, "continued to Japanese ASR"):
                    worker._process_locked(video, root / "audio.wav", root / "vocals.wav")
                worker._close_stage_state()

            extract_audio_mock.assert_called_once()
            transcribe_mock.assert_called_once()
            translator_mock.assert_not_called()
            metadata_mock.assert_called_once_with(video)

    def test_cached_japanese_translation_skips_audio_language_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E02.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                scanner_cache_enabled=False,
            )
            paths = paths_for_video(video, config)
            source_blocks = [
                SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["今日は学校へ行きます。"])
            ]
            write_srt(paths.ja_srt, source_blocks)
            worker = VideoWorker(config, _logger())
            translator = Mock()
            translator.translate_blocks.side_effect = (
                lambda _blocks, _source, destination, **_kwargs: write_srt(
                    destination,
                    [SrtBlock(1, source_blocks[0].timing, ["今天去学校。"])],
                )
            )

            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch.object(worker, "_extract_preferred_audio") as preferred_audio_mock,
                patch.object(
                    worker,
                    "_detect_source_language",
                    return_value=LanguageDetectionResult("en", 0.95, False, True, "test"),
                ) as language_mock,
                patch.object(worker, "_postprocess_ja_srt", return_value=source_blocks) as postprocess_mock,
                patch.object(worker, "_get_translator", return_value=translator),
            ):
                outcome = worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            extract_audio_mock.assert_not_called()
            preferred_audio_mock.assert_not_called()
            language_mock.assert_not_called()
            postprocess_mock.assert_called_once()
            translator.translate_blocks.assert_called_once()
            self.assertEqual(outcome.stage, "complete")
            self.assertEqual(outcome.status, "ok")

    def test_process_translates_non_allowed_language_to_traditional_chinese_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Spanish Show S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                transcribe_non_allowed_languages=True,
                translate_non_japanese_sources=True,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-model",
                export_ai_ass=True,
                keep_intermediate_files=True,
            )
            worker = VideoWorker(config, _logger())
            source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["hola"])]
            seen = {}

            def transcribe(_audio_path: Path, srt_path: Path, transcribe_config) -> None:
                seen["backend"] = transcribe_config.transcription_backend
                seen["model"] = transcribe_config.whisper_model
                seen["language"] = transcribe_config.whisper_language
                write_srt(srt_path, source_blocks)

            translator = Mock()

            def translate(blocks, source_srt, target_srt, **kwargs) -> None:
                seen["translation_source_language"] = kwargs["source_language"]
                self.assertEqual(blocks, source_blocks)
                self.assertEqual(source_srt, source_transcript_paths_for_video(video, config, "es").srt)
                write_srt(
                    target_srt,
                    [SrtBlock(1, source_blocks[0].timing, ["这里很安全"])],
                )

            translator.translate_blocks.side_effect = translate

            def convert_to_zh_tw(_source: Path, target: Path) -> None:
                write_srt(
                    target,
                    [SrtBlock(1, source_blocks[0].timing, ["這裡很安全"])],
                )

            def publish(_video: Path, paths, *, source_language: str) -> int:
                seen["publication_source_language"] = source_language
                return worker._export_ai_ass(paths)

            with (
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch.object(
                    worker,
                    "_detect_source_language",
                    return_value=LanguageDetectionResult(
                        "es",
                        0.95,
                        False,
                        True,
                        "test",
                        reason="non_allowed_language_detected",
                    ),
                ),
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe) as transcribe_mock,
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_build_series_metadata_context", return_value=None),
                patch.object(worker, "_convert_to_zh_tw", side_effect=convert_to_zh_tw),
                patch.object(worker, "_publish_ai_ass", side_effect=publish),
            ):
                outcome = worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            source_paths = source_transcript_paths_for_video(video, config, "es")
            regular_paths = paths_for_video(video, config)
            extract_audio_mock.assert_called_once()
            transcribe_mock.assert_called_once()
            self.assertEqual(seen["backend"], "faster-whisper")
            self.assertEqual(seen["model"], "large-model")
            self.assertEqual(seen["language"], "es")
            self.assertEqual(seen["translation_source_language"], "es")
            self.assertEqual(seen["publication_source_language"], "es")
            self.assertTrue(source_paths.srt.exists())
            self.assertTrue(source_paths.ass.exists())
            self.assertTrue(regular_paths.zh_cn_srt.exists())
            self.assertTrue(regular_paths.zh_tw_srt.exists())
            self.assertTrue(regular_paths.ai_zh_tw_ass.exists())
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    required_outputs=[
                        source_paths.ass,
                        regular_paths.ai_zh_cn_ass,
                        regular_paths.ai_zh_tw_ass,
                    ],
                    require_delivery_evidence=True,
                    expected_obligation_id=delivery_identity(video, config)["obligation_id"],
                    expected_policy_revision=delivery_identity(video, config)["policy_revision"],
                    expected_publication_kind="translated_trilingual",
                    expected_output_languages=("es", "zh-CN", "zh-TW"),
                    require_publication_semantics=True,
                )
            )
            self.assertTrue(has_ai_finished_subtitle(video, config))
            self.assertEqual(outcome.stage, "source_translation")
            self.assertEqual(outcome.status, "ok")
            self.assertIn("language=es", outcome.message)

    def test_source_translation_repairs_quality_omissions_without_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E01.mkv"
            video.write_bytes(b"video")
            config = _config(
                root,
                translate_non_japanese_sources=True,
                export_ai_ass=False,
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "en")
            source_blocks = [
                SrtBlock(
                    1,
                    "00:00:01,000 --> 00:00:02,000",
                    ["This English source line was not translated."],
                )
            ]
            write_srt(source_paths.srt, source_blocks)
            seen: dict[str, object] = {}
            translator = Mock()

            def translate(_blocks, _source, target, **_kwargs) -> None:
                write_srt(target, source_blocks)
                write_translation_quality_events(
                    target,
                    [
                        {
                            "code": "translation_safe_omission",
                            "severity": "fail",
                            "index": 1,
                            "source": source_blocks[0].text[0],
                            "output": source_blocks[0].text[0],
                            "reason": "test omission",
                        }
                    ],
                    srt_sha256=sha256_file(target),
                )

            def repair(_blocks, _source, target, **kwargs) -> None:
                seen["source_language"] = kwargs.get("source_language")
                write_srt(
                    target,
                    [SrtBlock(1, source_blocks[0].timing, ["translated Chinese"])],
                )

            translator.translate_blocks.side_effect = translate
            translator.retranslate_problem_blocks.side_effect = repair

            def convert(source: Path, target: Path) -> None:
                write_srt(target, read_srt(source))

            with (
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_build_series_metadata_context", return_value=None),
                patch.object(worker, "_convert_to_zh_tw", side_effect=convert),
            ):
                outcome = worker._translate_source_transcription(video, source_paths)

            self.assertEqual(seen["source_language"], "en")
            self.assertEqual(
                read_srt(paths_for_video(video, config).zh_cn_srt)[0].text,
                ["translated Chinese"],
            )
            self.assertEqual(outcome.status, "ok")

    def test_source_asr_rejection_is_rebuilt_then_normalized_success_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Assassination Classroom S01E20.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                export_ai_ass=True,
                keep_intermediate_files=True,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-model",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                japanese_transcription_fallback_compute_type="float16",
                whisper_compute_type="float16",
                whisper_initial_prompt="unsafe source prompt",
                op_ed_initial_prompt="unsafe source lyrics prompt",
                whisper_condition_on_previous_text=True,
                asr_optional_rescue_rejection_is_fatal=True,
                asr_selective_retry_enabled=False,
                subtitle_quality_max_primary_chars=24,
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "es")
            observed_configs = []

            def transcribe(_audio, target, active_config) -> None:
                observed_configs.append(active_config)
                diagnostic = asr_diagnostics_path(target, active_config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                if len(observed_configs) == 1:
                    write_srt(
                        target,
                        [
                            SrtBlock(
                                1,
                                "00:01:39,000 --> 00:02:06,000",
                                ["rejected source transcript"],
                            )
                        ],
                    )
                    diagnostic.write_text(
                        json.dumps(
                            {
                                "status": "selective_retry_required",
                                "srt_path": str(target),
                                "srt_sha256": sha256_file(target),
                            }
                        ),
                        encoding="utf-8",
                    )
                    raise LowConfidenceTranscriptionError(
                        "source quality rejected",
                        [(99.0, 126.0), (261.5, 276.1)],
                        reason_code="low_confidence",
                    )
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,000 --> 00:00:02,000",
                            [
                                "This is a very long accepted source subtitle "
                                "that will be wrapped for readability."
                            ],
                        )
                    ],
                )
                diagnostic.write_text(
                    json.dumps(
                        {
                            "status": "accepted",
                            "srt_path": str(target),
                            "srt_sha256": sha256_file(target),
                        }
                    ),
                    encoding="utf-8",
                )

            def publish_source(_video, _source_srt, destination) -> None:
                destination.write_text("published", encoding="utf-8")

            with (
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=transcribe,
                ),
                patch.object(
                    worker,
                    "_publish_source_ass",
                    side_effect=publish_source,
                ) as publish,
            ):
                worker._process_source_transcription(video, audio, "es")
                worker._process_source_transcription(video, audio, "es")

            self.assertEqual(len(observed_configs), 2)
            self.assertEqual(publish.call_count, 2)
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    required_outputs=[source_paths.ass],
                )
            )
            primary_config, fallback_config = observed_configs
            self.assertEqual(primary_config.whisper_language, "es")
            self.assertEqual(primary_config.whisper_model, "large-model")
            self.assertEqual(primary_config.whisper_initial_prompt, "unsafe source prompt")
            self.assertEqual(fallback_config.whisper_language, "es")
            self.assertEqual(fallback_config.whisper_model, "fallback-model")
            self.assertEqual(fallback_config.transcription_backend, "faster-whisper")
            self.assertEqual(fallback_config.whisper_compute_type, "float16")
            self.assertIsNone(fallback_config.whisper_initial_prompt)
            self.assertIsNone(fallback_config.op_ed_initial_prompt)
            self.assertFalse(fallback_config.whisper_condition_on_previous_text)
            self.assertTrue(fallback_config.asr_optional_rescue_rejection_is_fatal)
            self.assertGreater(len(read_srt(source_paths.srt)[0].text), 1)
            self.assertFalse(
                asr_diagnostics_path(source_paths.srt, config).exists()
            )
            self.assertFalse(
                asr_transcription_hold_path(source_paths.srt, config).exists()
            )

    def test_source_low_confidence_exact_ranges_try_prompt_free_selective_repair_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Assassination Classroom S01E20.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=True,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-v3",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_compute_type="float16",
                whisper_compute_type="float16",
                scanner_cache_enabled=False,
                whisper_initial_prompt=None,
                op_ed_initial_prompt=None,
                whisper_condition_on_previous_text=False,
                asr_optional_rescue_rejection_is_fatal=True,
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "en")
            ranges = [(99.0, 126.0), (261.5, 276.1)]
            observed = {}

            def reject_primary(_audio, target, active_config) -> None:
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:01:39,000 --> 00:02:06,000",
                            ["prompt-shaped rejected English"],
                        )
                    ],
                )
                diagnostic = asr_diagnostics_path(target, active_config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "status": "selective_retry_required",
                            "srt_path": str(target),
                            "srt_sha256": sha256_file(target),
                            "review_ranges": [list(item) for item in ranges],
                        }
                    ),
                    encoding="utf-8",
                )
                raise LowConfidenceTranscriptionError(
                    "source quality rejected",
                    ranges,
                    reason_code="low_confidence",
                )

            def selective(_audio, target, requested_ranges, active_config, _logger):
                observed["selective_config"] = active_config
                observed["ranges"] = requested_ranges
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:01:39,000 --> 00:02:06,000",
                            ["recovered English source"],
                        )
                    ],
                )
                return SimpleNamespace(segment_confidences=())

            def finalize(_audio, target, requested_ranges, active_config, _logger, **kwargs):
                observed["final_config"] = active_config
                observed["require_confidence"] = kwargs["require_confidence"]
                self.assertEqual(requested_ranges, ranges)
                diagnostic = asr_diagnostics_path(target, active_config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "status": "accepted_after_selective_retry",
                            "srt_path": str(target),
                            "srt_sha256": sha256_file(target),
                        }
                    ),
                    encoding="utf-8",
                )

            with (
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=reject_primary,
                ) as transcribe,
                patch(
                    "worker.repair_low_confidence_ranges",
                    side_effect=selective,
                ) as selective_repair,
                patch(
                    "worker.finalize_repaired_transcription",
                    side_effect=finalize,
                ) as final_quality,
            ):
                outcome = worker._process_source_transcription(
                    video,
                    audio,
                    "en",
                )

            transcribe.assert_called_once()
            selective_repair.assert_called_once()
            final_quality.assert_called_once()
            self.assertEqual(observed["ranges"], ranges)
            self.assertEqual(observed["selective_config"].whisper_language, "en")
            self.assertIsNone(observed["selective_config"].whisper_initial_prompt)
            self.assertIsNone(observed["selective_config"].op_ed_initial_prompt)
            self.assertFalse(
                observed["selective_config"].whisper_condition_on_previous_text
            )
            self.assertTrue(
                observed["selective_config"].asr_optional_rescue_rejection_is_fatal
            )
            self.assertEqual(observed["final_config"].whisper_language, "en")
            self.assertTrue(observed["require_confidence"])
            self.assertEqual(
                read_srt(source_paths.srt)[0].text,
                ["recovered English source"],
            )
            self.assertEqual(outcome.stage, "source_transcription")
            self.assertEqual(outcome.status, "ok")

    def test_source_prompt_free_fallback_failure_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=False,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-v3",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_compute_type="float16",
                whisper_compute_type="float16",
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "en")
            configs = []

            def reject(_audio, target, active_config) -> None:
                configs.append(active_config)
                write_srt(
                    target,
                    [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["unsafe"])],
                )
                if len(configs) == 1:
                    raise LowConfidenceTranscriptionError(
                        "primary low confidence",
                        [(99.0, 126.0), (261.5, 276.1)],
                        reason_code="low_confidence",
                    )
                raise TranscriptionError("prompt-free fallback quality rejected")

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=reject),
                self.assertRaisesRegex(
                    TranscriptionError,
                    "prompt-free fallback quality rejected",
                ),
            ):
                worker._process_source_transcription(video, audio, "en")

            self.assertEqual(len(configs), 2)
            self.assertEqual(configs[1].whisper_language, "en")
            self.assertIsNone(configs[1].whisper_initial_prompt)
            self.assertFalse(configs[1].whisper_condition_on_previous_text)
            self.assertFalse(source_paths.srt.exists())
            self.assertFalse(
                asr_diagnostics_path(source_paths.srt, config).exists()
            )
            self.assertFalse(
                asr_transcription_hold_path(source_paths.srt, config).exists()
            )

    def test_source_prompt_free_fallback_uses_configured_final_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=False,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-v3",
                non_japanese_transcription_fallback_backend="faster-whisper",
                non_japanese_transcription_fallback_model="large-v2",
                non_japanese_transcription_fallback_compute_type="float16",
                non_japanese_transcription_final_fallback_backend="transformers-whisper",
                non_japanese_transcription_final_fallback_model="openai/whisper-large-v3-turbo",
                non_japanese_transcription_final_fallback_compute_type="float16",
                whisper_compute_type="float16",
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "en")
            configs = []

            def transcribe(_audio, target, active_config) -> None:
                configs.append(active_config)
                if len(configs) == 1:
                    raise LowConfidenceTranscriptionError(
                        "primary low confidence",
                        [(10.0, 20.0)],
                        reason_code="low_confidence",
                    )
                if len(configs) == 2:
                    raise TranscriptionError("prompt-free fallback quality rejected")
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:02,000",
                            ["independent final fallback output"],
                        )
                    ],
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.clear_whisper_model_cache") as clear_cache,
            ):
                outcome = worker._process_source_transcription(video, audio, "en")

            self.assertEqual(len(configs), 3)
            final_config = configs[2]
            self.assertEqual(final_config.transcription_backend, "transformers-whisper")
            self.assertEqual(final_config.whisper_model, "openai/whisper-large-v3-turbo")
            self.assertEqual(final_config.whisper_language, "en")
            self.assertIsNone(final_config.whisper_initial_prompt)
            self.assertFalse(final_config.whisper_condition_on_previous_text)
            self.assertGreaterEqual(clear_cache.call_count, 1)
            self.assertTrue(worker._asr_cache_diagnostics_are_trusted(source_paths.srt))
            self.assertEqual(outcome.status, "ok")

    def test_source_backend_runtime_error_is_wrapped_and_recovered_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=True,
                non_japanese_transcription_backend="vibevoice",
                non_japanese_transcription_model="primary-model",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                whisper_compute_type="float16",
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "en")
            configs = []

            def transcribe(_audio, target, active_config) -> None:
                configs.append(active_config)
                if len(configs) == 1:
                    raise RuntimeError("backend-specific primary failure")
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:02,000",
                            ["English fallback output"],
                        )
                    ],
                )
                diagnostic = asr_diagnostics_path(target, active_config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "status": "accepted",
                            "srt_path": str(target),
                            "srt_sha256": sha256_file(target),
                        }
                    ),
                    encoding="utf-8",
                )

            with patch.object(
                worker,
                "_transcribe_with_config",
                side_effect=transcribe,
            ):
                outcome = worker._process_source_transcription(video, audio, "en")

            self.assertEqual(len(configs), 2)
            self.assertEqual(configs[1].transcription_backend, "faster-whisper")
            self.assertEqual(configs[1].whisper_language, "en")
            self.assertIsNone(configs[1].whisper_initial_prompt)
            self.assertEqual(
                read_srt(source_paths.srt)[0].text,
                ["English fallback output"],
            )
            self.assertEqual(outcome.status, "ok")

    def test_source_fallback_cuda_oom_retries_once_with_lower_memory_compute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=False,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-v3",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_compute_type="float16",
                whisper_compute_type="float16",
                whisper_device="cuda",
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "en")
            configs = []

            def transcribe(_audio, target, active_config) -> None:
                configs.append(active_config)
                if len(configs) == 1:
                    raise TranscriptionError("primary quality rejected")
                if len(configs) == 2:
                    raise TranscriptionError("CUDA failed with error out of memory")
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:02,000",
                            ["lower-memory English output"],
                        )
                    ],
                )
                diagnostic = asr_diagnostics_path(target, active_config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "status": "accepted",
                            "srt_path": str(target),
                            "srt_sha256": sha256_file(target),
                        }
                    ),
                    encoding="utf-8",
                )

            with (
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=transcribe,
                ),
                patch("worker.clear_whisper_model_cache") as clear_cache,
            ):
                worker._process_source_transcription(video, audio, "en")

            self.assertEqual(len(configs), 3)
            self.assertEqual(configs[1].whisper_compute_type, "float16")
            self.assertEqual(configs[2].whisper_compute_type, "int8_float16")
            self.assertEqual(configs[2].whisper_language, "en")
            self.assertIsNone(configs[2].whisper_initial_prompt)
            self.assertGreaterEqual(clear_cache.call_count, 1)
            self.assertEqual(
                read_srt(source_paths.srt)[0].text,
                ["lower-memory English output"],
            )

    def test_source_fallback_without_required_diagnostic_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English Show S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=False,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-v3",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="large-v2",
                scanner_cache_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "en")
            calls = 0

            def transcribe(_audio, target, _active_config) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise TranscriptionError("primary failed")
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:02,000",
                            ["output without diagnostic"],
                        )
                    ],
                )

            with (
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=transcribe,
                ),
                self.assertRaisesRegex(
                    TranscriptionError,
                    "diagnostic is missing",
                ),
            ):
                worker._process_source_transcription(video, audio, "en")

            self.assertEqual(calls, 2)
            self.assertFalse(source_paths.srt.exists())
            self.assertFalse(
                asr_transcription_hold_path(source_paths.srt, config).exists()
            )

    def test_source_asr_pending_restart_discards_old_cache_before_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Spanish Show S01E01.mkv"
            video.write_bytes(b"video")
            audio = root / "audio.wav"
            audio.write_bytes(b"audio")
            config = _config(
                root,
                export_ai_ass=False,
                non_japanese_transcription_backend="faster-whisper",
                non_japanese_transcription_model="large-model",
            )
            worker = VideoWorker(config, _logger())
            source_paths = source_transcript_paths_for_video(video, config, "es")
            write_srt(
                source_paths.srt,
                [
                    SrtBlock(
                        1,
                        "00:00:00,000 --> 00:00:01,000",
                        ["interrupted old source"],
                    )
                ],
            )
            diagnostic = asr_diagnostics_path(source_paths.srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "srt_path": str(source_paths.srt),
                        "srt_sha256": sha256_file(source_paths.srt),
                    }
                ),
                encoding="utf-8",
            )
            hold = worker._begin_asr_commit(
                source_paths.srt,
                reason="simulated source ASR crash",
            )

            def rebuild(_audio, target, _active_config) -> None:
                self.assertFalse(target.exists())
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,000 --> 00:00:01,000",
                            ["rebuilt source"],
                        )
                    ],
                )

            with patch.object(
                worker,
                "_transcribe_with_config",
                side_effect=rebuild,
            ) as transcribe:
                worker._process_source_transcription(video, audio, "es")

            transcribe.assert_called_once()
            self.assertEqual(
                read_srt(source_paths.srt)[0].text,
                ["rebuilt source"],
            )
            self.assertFalse(diagnostic.exists())
            self.assertFalse(hold.exists())

    def test_process_reextracts_when_cached_audio_detects_non_japanese_but_video_has_japanese_track(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Dual Audio Show S01E01.mkv"
            video.write_text("", encoding="utf-8")
            audio_path = root / "audio.wav"
            audio_path.write_bytes(b"stale english audio")
            config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                transcribe_non_allowed_languages=True,
            )
            worker = VideoWorker(config, _logger())

            language_results = [
                LanguageDetectionResult("en", 0.99, False, True, "test", reason="non_allowed_language_detected"),
                LanguageDetectionResult("en", 0.99, False, True, "test", reason="non_allowed_language_detected"),
            ]

            with (
                patch.object(worker, "_detect_source_language", side_effect=language_results) as detect_mock,
                patch("worker.has_japanese_audio_stream", return_value=True),
                patch("worker.validate_cached_audio", return_value=True),
                patch("worker.extract_audio", return_value=None) as extract_audio_mock,
                patch.object(
                    worker,
                    "_process_source_transcription",
                    return_value=ProcessOutcome("source_transcription", "ok", "language=en"),
                ) as source_transcription_mock,
            ):
                outcome = worker._process_locked(video, audio_path, root / "vocals.wav")
                worker._close_stage_state()

            self.assertEqual(detect_mock.call_count, 2)
            extract_audio_mock.assert_called_once_with(video, audio_path)
            source_transcription_mock.assert_called_once()
            self.assertEqual(outcome.stage, "source_transcription")

    def test_language_recovery_content_probes_after_metadata_stream_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Dual Audio Show S01E01.mkv"
            video.write_bytes(b"video")
            audio_path = root / "audio.wav"
            audio_path.write_bytes(b"audio")
            config = _config(root, audio_content_probe_enabled=True)
            worker = VideoWorker(config, _logger())
            first = AudioStreamInfo(1, "eng", "Main", True, False)
            japanese = AudioStreamInfo(2, "und", "Dialogue", False, False)
            worker._selected_audio_stream = first
            blocked = LanguageDetectionResult("en", 0.98, False, True, "test", reason="non_allowed_language_detected")
            allowed = LanguageDetectionResult("ja", 0.96, True, True, "test", reason="allowed_language_detected")
            detector = Mock()
            detector.select_japanese_audio_stream.return_value = AudioStreamSelection(
                japanese,
                [],
                "content_detected_japanese",
            )

            with (
                patch("worker._should_retry_language_gate_with_japanese_audio", return_value=False),
                patch("worker.probe_audio_streams", return_value=[first, japanese]),
                patch("worker.LanguageDetector", return_value=detector),
                patch.object(worker, "_extract_preferred_audio", return_value=audio_path) as extract,
                patch.object(worker, "_detect_source_language", return_value=allowed) as detect,
                patch.object(worker, "_write_audio_selection_manifest"),
                patch.object(worker, "_set_stage"),
            ):
                result = worker._recover_japanese_audio_stream(video, audio_path, blocked, force_ai=False)

            self.assertTrue(result.allowed)
            extract.assert_called_once_with(video, audio_path, stream=japanese)
            detect.assert_called_once_with(video, audio_path, force_ai=False, force_refresh=True)

    def test_primary_rejection_with_diagnostics_disabled_cannot_become_cache_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                japanese_transcription_model="large-v3",
                japanese_transcription_backend="faster-whisper",
                japanese_transcription_fallback_model="large-v3",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_diagnostics_enabled=False,
                asr_selective_retry_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            output = root / "out.ja.srt"
            stale_diagnostic = asr_diagnostics_path(output, config)
            stale_diagnostic.parent.mkdir(parents=True, exist_ok=True)
            stale_diagnostic.write_text(
                '{"status":"accepted","srt_sha256":"stale"}',
                encoding="utf-8",
            )

            def reject(_audio, target, _config):
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:10,000 --> 00:00:12,000",
                            ["rejected transcript"],
                        )
                    ],
                )
                raise LowConfidenceTranscriptionError(
                    "quality rejected",
                    [(8.0, 14.0)],
                    reason_code="low_confidence",
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=reject),
                self.assertRaises(LowConfidenceTranscriptionError),
            ):
                worker._transcribe(root / "audio.wav", output)

            self.assertFalse(output.exists())
            self.assertFalse(stale_diagnostic.exists())
            self.assertFalse(asr_transcription_hold_path(output, config).exists())

    def test_successful_asr_validation_clears_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, asr_diagnostics_enabled=False)
            worker = VideoWorker(config, _logger())
            output = root / "out.ja.srt"

            def succeed(_audio: Path, target: Path) -> None:
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,000 --> 00:00:01,000",
                            ["accepted transcript"],
                        )
                    ],
                )

            with patch.object(
                worker,
                "_transcribe_with_fallback",
                side_effect=succeed,
            ):
                worker._transcribe(root / "audio.wav", output)

            self.assertTrue(output.exists())
            self.assertFalse(asr_transcription_hold_path(output, config).exists())

    def test_restart_invalidates_asr_pending_output_with_or_without_accepted_diagnostic(self) -> None:
        for diagnostic_status in (None, "accepted"):
            with (
                self.subTest(diagnostic_status=diagnostic_status),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"video")
                config = _config(root, scanner_cache_enabled=False)
                worker = VideoWorker(config, _logger())
                paths = paths_for_video(video, config)
                block = SrtBlock(
                    1,
                    "00:00:00,000 --> 00:00:01,000",
                    ["possibly interrupted transcript"],
                )
                write_srt(paths.ja_srt, [block])
                write_srt(paths.zh_cn_srt, [block])
                write_srt(paths.zh_tw_srt, [block])
                hold = worker._begin_asr_commit(
                    paths.ja_srt,
                    reason="simulated process termination",
                )
                diagnostic = asr_diagnostics_path(paths.ja_srt, config)
                if diagnostic_status is not None:
                    diagnostic.parent.mkdir(parents=True, exist_ok=True)
                    diagnostic.write_text(
                        json.dumps(
                            {
                                "status": diagnostic_status,
                                "srt_path": str(paths.ja_srt),
                                "srt_sha256": sha256_file(paths.ja_srt),
                            }
                        ),
                        encoding="utf-8",
                    )

                worker._recover_pending_asr_commit(video, paths)

                self.assertFalse(paths.ja_srt.exists())
                self.assertFalse(paths.zh_cn_srt.exists())
                self.assertFalse(paths.zh_tw_srt.exists())
                self.assertFalse(diagnostic.exists())
                self.assertFalse(hold.exists())

    def test_failed_asr_never_leaves_a_trusted_output_or_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, asr_diagnostics_enabled=False)
            worker = VideoWorker(config, _logger())
            output = root / "out.ja.srt"

            def fail_after_output(_audio: Path, target: Path) -> None:
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,000 --> 00:00:01,000",
                            ["uncommitted transcript"],
                        )
                    ],
                )
                raise TranscriptionError("injected ASR failure")

            with (
                patch.object(
                    worker,
                    "_transcribe_with_fallback",
                    side_effect=fail_after_output,
                ),
                self.assertRaises(TranscriptionError),
            ):
                worker._transcribe(root / "audio.wav", output)

            self.assertFalse(output.exists())
            self.assertFalse(asr_diagnostics_path(output, config).exists())
            self.assertFalse(asr_transcription_hold_path(output, config).exists())

    def test_primary_diagnostic_write_failure_cannot_leave_rejected_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                japanese_transcription_model="large-v3",
                japanese_transcription_backend="faster-whisper",
                japanese_transcription_fallback_model="large-v3",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_diagnostics_enabled=True,
                asr_selective_retry_enabled=False,
                enable_gap_rescue=False,
                enable_leading_gap_rescue=False,
                op_ed_transcription_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            output = root / "out.ja.srt"
            rejection = LowConfidenceTranscriptionError(
                "quality rejected",
                [(0.0, 3.0)],
                reason_code="low_confidence",
            )
            diagnostic_write = Mock(return_value=None)

            def reject_after_diagnostic_failure(_audio, target, _config):
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,500 --> 00:00:02,000",
                            ["rejected transcript"],
                        )
                    ],
                )
                diagnostic_write()
                raise rejection

            with (
                patch.object(
                    worker,
                    "_transcribe_with_config",
                    side_effect=reject_after_diagnostic_failure,
                ),
                self.assertRaises(LowConfidenceTranscriptionError),
            ):
                worker._transcribe(root / "audio.wav", output)

            diagnostic_write.assert_called_once_with()
            self.assertFalse(output.exists())
            self.assertFalse(asr_diagnostics_path(output, config).exists())

    def test_full_fallback_rejection_cannot_leave_rejected_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_diagnostics_enabled=False,
                asr_selective_retry_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            output = root / "out.ja.srt"
            calls = 0

            def reject(_audio, target, _config):
                nonlocal calls
                calls += 1
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:10,000 --> 00:00:12,000",
                            [f"rejected attempt {calls}"],
                        )
                    ],
                )
                if calls == 1:
                    raise TranscriptionError("primary failed")
                raise LowConfidenceTranscriptionError(
                    "fallback quality rejected",
                    [(8.0, 14.0)],
                    reason_code="low_confidence",
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=reject),
                patch("worker.clear_whisper_model_cache"),
                self.assertRaises(LowConfidenceTranscriptionError),
            ):
                worker._transcribe(root / "audio.wav", output)

            self.assertEqual(calls, 2)
            self.assertFalse(output.exists())
            self.assertFalse(asr_diagnostics_path(output, config).exists())

    def test_japanese_transcription_falls_back_to_base_model_when_primary_quality_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-model",
                transcription_backend="faster-whisper",
                japanese_transcription_backend="transformers-whisper",
                japanese_transcription_model="anime-model",
            )
            worker = VideoWorker(config, _logger())
            ja_srt = root / "out.ja.srt"
            seen_routes: list[tuple[str, str]] = []

            def transcribe(_audio_path: Path, srt_path: Path, transcribe_config) -> None:
                seen_routes.append((transcribe_config.transcription_backend, transcribe_config.whisper_model))
                if len(seen_routes) == 1:
                    raise TranscriptionError("ASR quality check failed")
                write_srt(srt_path, [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["fallback"])])

            with patch.object(worker, "_transcribe_with_config", side_effect=transcribe) as transcribe_mock:
                worker._transcribe(root / "audio.wav", ja_srt)

            self.assertEqual(seen_routes, [("transformers-whisper", "anime-model"), ("faster-whisper", "large-model")])
            self.assertEqual(transcribe_mock.call_count, 2)
            self.assertTrue(ja_srt.exists())
            self.assertIsNotNone(worker._last_asr_route)
            self.assertEqual(worker._last_asr_route.backend, "faster-whisper")
            self.assertEqual(worker._last_asr_route.model, "large-model")
            self.assertTrue(worker._last_asr_route.fallback_used)
            self.assertEqual(worker._last_asr_route.failed_reason, "ASR quality check failed")
            self.assertIn("fallback faster-whisper model=large-model", worker._japanese_srt_created_message())
            self.assertIn("failed: ASR quality check failed", worker._japanese_srt_created_message())

    def test_every_backend_runs_shared_final_srt_quality_gate(self) -> None:
        backend_targets = {
            "faster-whisper": "worker.transcribe_to_srt",
            "vibevoice": "transcriber_vibevoice.transcribe_to_srt_with_vibevoice",
            "whisperx": "transcriber_whisperx.transcribe_to_srt_with_whisperx",
            "transformers-whisper": (
                "transcriber_transformers_whisper."
                "transcribe_to_srt_with_transformers_whisper"
            ),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"

            for backend, target in backend_targets.items():
                with self.subTest(backend=backend):
                    output = root / f"{backend}.srt"
                    config = _config(root, transcription_backend=backend)
                    worker = VideoWorker(config, _logger())

                    def produce(_audio, srt_path, _config, _logger) -> None:
                        write_srt(
                            srt_path,
                            [
                                SrtBlock(
                                    1,
                                    "00:00:00,000 --> 00:00:01,000",
                                    ["backend output"],
                                )
                            ],
                        )

                    with (
                        patch(target, side_effect=produce) as backend_call,
                        patch(
                            "worker.validate_transcription_srt_quality",
                            side_effect=TranscriptionError("shared final gate rejected"),
                        ) as final_gate,
                        self.assertRaisesRegex(
                            TranscriptionError,
                            "shared final gate rejected",
                        ),
                    ):
                        worker._transcribe_with_config(audio, output, config)

                    backend_call.assert_called_once()
                    final_gate.assert_called_once_with(
                        audio,
                        output,
                        config,
                        worker.logger,
                    )

    def test_backend_runtime_error_is_normalized_with_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            output = root / "episode.srt"
            config = _config(root, transcription_backend="vibevoice")
            worker = VideoWorker(config, _logger())
            backend_error = RuntimeError("decoder exploded")

            with (
                patch(
                    "transcriber_vibevoice.transcribe_to_srt_with_vibevoice",
                    side_effect=backend_error,
                ),
                self.assertRaisesRegex(
                    TranscriptionError,
                    "ASR backend vibevoice failed.*decoder exploded",
                ) as caught,
            ):
                worker._transcribe_with_config(audio, output, config)

            self.assertIs(caught.exception.__cause__, backend_error)

    def test_vibevoice_runtime_failure_enters_configured_japanese_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            output = root / "episode.srt"
            config = _config(
                root,
                japanese_transcription_backend="vibevoice",
                japanese_transcription_model="vibe-model",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_model="fallback-model",
                japanese_transcription_fallback_compute_type="float16",
            )
            worker = VideoWorker(config, _logger())

            def fallback(_audio, srt_path, _config, _logger) -> None:
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:00,000 --> 00:00:01,000", ["fallback"])],
                )

            with (
                patch(
                    "transcriber_vibevoice.transcribe_to_srt_with_vibevoice",
                    side_effect=RuntimeError("vibe runtime failed"),
                ),
                patch("worker.transcribe_to_srt", side_effect=fallback) as fallback_call,
            ):
                worker._transcribe_with_fallback(audio, output)

            fallback_call.assert_called_once()
            self.assertEqual(read_srt(output)[0].text, ["fallback"])
            self.assertTrue(worker._last_asr_route and worker._last_asr_route.fallback_used)

    def test_faster_whisper_fallback_releases_primary_model_before_loading_second_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
            )
            worker = VideoWorker(config, _logger())
            calls = 0

            def transcribe(_audio_path: Path, srt_path: Path, _transcribe_config) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise TranscriptionError("primary failed")
                write_srt(srt_path, [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["fallback"])])

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.clear_whisper_model_cache") as clear_cache,
            ):
                worker._transcribe(root / "audio.wav", root / "out.srt")

            clear_cache.assert_called_once()
            self.assertEqual(calls, 2)

    def test_faster_whisper_fallback_cuda_oom_retries_once_with_lower_memory_compute(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_device="cuda",
                whisper_model="large-v3",
                whisper_compute_type="float16",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_selective_retry_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            observed_compute_types: list[str] = []

            def transcribe(_audio_path: Path, srt_path: Path, transcribe_config) -> None:
                observed_compute_types.append(transcribe_config.whisper_compute_type)
                if len(observed_compute_types) == 1:
                    raise TranscriptionError("primary quality failed")
                if len(observed_compute_types) == 2:
                    write_srt(
                        srt_path,
                        [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["unsafe partial"])],
                    )
                    raise TranscriptionError("CUDA out of memory")
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["fallback"])],
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.clear_whisper_model_cache") as clear_cache,
            ):
                worker._transcribe(root / "audio.wav", root / "out.srt")

            self.assertEqual(
                observed_compute_types,
                ["float16", "float16", "int8_float16"],
            )
            self.assertEqual(clear_cache.call_count, 2)
            self.assertEqual(read_srt(root / "out.srt")[0].text, ["fallback"])

    def test_japanese_fallback_second_oom_uses_smaller_final_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_device="cuda",
                whisper_model="large-v3",
                whisper_compute_type="float16",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                japanese_transcription_final_fallback_backend="faster-whisper",
                japanese_transcription_final_fallback_model="medium",
                japanese_transcription_final_fallback_compute_type="int8_float16",
                asr_selective_retry_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            observed_routes: list[tuple[str, str]] = []

            def transcribe(_audio_path: Path, srt_path: Path, active_config) -> None:
                observed_routes.append(
                    (
                        active_config.whisper_model,
                        active_config.whisper_compute_type,
                    )
                )
                if len(observed_routes) == 1:
                    raise TranscriptionError("primary quality failed")
                if len(observed_routes) < 4:
                    raise TranscriptionError("CUDA failed with error out of memory")
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["small model recovery"])],
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.clear_whisper_model_cache") as clear_cache,
            ):
                worker._transcribe(root / "audio.wav", root / "out.srt")

            self.assertEqual(
                observed_routes,
                [
                    ("large-v3", "float16"),
                    ("large-v2", "float16"),
                    ("large-v2", "int8_float16"),
                    ("medium", "int8_float16"),
                ],
            )
            self.assertEqual(clear_cache.call_count, 3)
            self.assertEqual(read_srt(root / "out.srt")[0].text, ["small model recovery"])
            self.assertEqual(worker._last_asr_route.model, "medium")

    def test_selective_asr_repair_must_pass_final_quality_before_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_selective_retry_enabled=True,
            )
            worker = VideoWorker(config, _logger())
            output = root / "out.ja.srt"
            calls = 0

            def transcribe(_audio_path: Path, srt_path: Path, _transcribe_config) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    write_srt(
                        srt_path,
                        [SrtBlock(1, "00:00:45,000 --> 00:00:47,000", ["primary starts late"])],
                    )
                    raise LowConfidenceTranscriptionError(
                        "opening gap requires repair",
                        [(0.0, 45.0)],
                        reason_code="leading_gap",
                    )
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:00,800 --> 00:00:02,000", ["full fallback restored opening"])],
                )

            def incomplete_repair(_audio, srt_path, _ranges, _config, _logger) -> None:
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:40,000 --> 00:00:42,000", ["still starts late"])],
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.repair_low_confidence_ranges", side_effect=incomplete_repair),
                patch("worker.clear_whisper_model_cache"),
            ):
                worker._transcribe(root / "audio.wav", output)

            self.assertEqual(calls, 2)
            self.assertEqual(read_srt(output)[0].text, ["full fallback restored opening"])
            self.assertTrue(worker._last_asr_route and worker._last_asr_route.fallback_used)

    def test_asr_artifact_selective_retry_clears_all_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                whisper_initial_prompt="unsafe primary prompt",
                op_ed_initial_prompt="unsafe lyrics prompt",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_selective_retry_enabled=True,
            )
            worker = VideoWorker(config, _logger())
            ja_srt = root / "out.ja.srt"

            def transcribe(_audio_path: Path, srt_path: Path, _transcribe_config) -> None:
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:20,000 --> 00:00:22,000", ["clean line"])],
                )
                raise LowConfidenceTranscriptionError(
                    "ASR artifact requires repair",
                    [(0.0, 20.0)],
                    reason_code="asr_artifact",
                )

            observed: dict[str, object] = {}

            def repair(_audio, _srt, ranges, repair_config, _logger) -> None:
                observed["ranges"] = ranges
                observed["whisper_initial_prompt"] = repair_config.whisper_initial_prompt
                observed["op_ed_initial_prompt"] = repair_config.op_ed_initial_prompt
                observed["enable_leading_gap_rescue"] = repair_config.enable_leading_gap_rescue
                observed["op_ed_transcription_enabled"] = repair_config.op_ed_transcription_enabled

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.repair_low_confidence_ranges", side_effect=repair) as repair_mock,
                patch("worker.clear_whisper_model_cache"),
            ):
                worker._transcribe(root / "audio.wav", ja_srt)

            repair_mock.assert_called_once()
            self.assertEqual(observed["ranges"], [(0.0, 20.0)])
            self.assertIsNone(observed["whisper_initial_prompt"])
            self.assertIsNone(observed["op_ed_initial_prompt"])
            self.assertFalse(observed["enable_leading_gap_rescue"])
            self.assertFalse(observed["op_ed_transcription_enabled"])

    def test_low_confidence_full_fallback_clears_prompts_when_selective_repair_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                whisper_initial_prompt="unsafe primary prompt",
                op_ed_initial_prompt="unsafe lyrics prompt",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v2",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_selective_retry_enabled=True,
            )
            worker = VideoWorker(config, _logger())
            observed: list[tuple[str, object, object, bool, bool, bool]] = []

            def transcribe(_audio_path: Path, srt_path: Path, transcribe_config) -> None:
                observed.append(
                    (
                        transcribe_config.whisper_model,
                        getattr(transcribe_config, "whisper_initial_prompt", None),
                        getattr(transcribe_config, "op_ed_initial_prompt", None),
                        bool(
                            getattr(
                                transcribe_config,
                                "whisper_condition_on_previous_text",
                                True,
                            )
                        ),
                        bool(
                            getattr(
                                transcribe_config,
                                "asr_optional_rescue_rejection_is_fatal",
                                True,
                            )
                        ),
                        bool(
                            getattr(
                                transcribe_config,
                                "asr_prompt_free_allow_recovered_primary_artifacts",
                                False,
                            )
                        ),
                    )
                )
                if len(observed) == 1:
                    raise LowConfidenceTranscriptionError(
                        "primary transcript confidence was rejected",
                        [(0.0, 30.0)],
                        reason_code="low_confidence",
                    )
                write_srt(
                    srt_path,
                    [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["fallback"])],
                )

            with (
                patch.object(worker, "_transcribe_with_config", side_effect=transcribe),
                patch("worker.clear_whisper_model_cache"),
            ):
                worker._transcribe(root / "audio.wav", root / "out.ja.srt")

            self.assertEqual(
                observed,
                [
                    (
                        "large-v3",
                        "unsafe primary prompt",
                        "unsafe lyrics prompt",
                        True,
                        True,
                        False,
                    ),
                    ("large-v2", None, None, False, False, True),
                ],
            )

    def test_same_model_low_confidence_fallback_retries_after_removing_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(
                root,
                whisper_model="large-v3",
                whisper_compute_type="float16",
                whisper_initial_prompt="unsafe primary prompt",
                op_ed_initial_prompt="unsafe lyrics prompt",
                japanese_transcription_model="large-v3",
                japanese_transcription_fallback_model="large-v3",
                japanese_transcription_fallback_backend="faster-whisper",
                japanese_transcription_fallback_compute_type="float16",
                asr_selective_retry_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            observed: list[tuple[str, str, object, object]] = []

            def transcribe(_audio_path, output, transcribe_config):
                observed.append(
                    (
                        transcribe_config.transcription_backend,
                        transcribe_config.whisper_model,
                        transcribe_config.whisper_initial_prompt,
                        transcribe_config.op_ed_initial_prompt,
                    )
                )
                if len(observed) == 1:
                    write_srt(
                        output,
                        [
                            SrtBlock(
                                1,
                                "00:00:10,000 --> 00:00:12,000",
                                ["prompt-shaped rejected line"],
                            )
                        ],
                    )
                    raise LowConfidenceTranscriptionError(
                        "prompt-shaped transcript rejected",
                        [(8.0, 14.0)],
                        reason_code="low_confidence",
                    )
                write_srt(
                    output,
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:02,000",
                            ["prompt-free retry"],
                        )
                    ],
                )

            output = root / "out.ja.srt"
            with patch.object(
                worker,
                "_transcribe_with_config",
                side_effect=transcribe,
            ):
                worker._transcribe(root / "audio.wav", output)

            self.assertEqual(
                observed,
                [
                    (
                        "faster-whisper",
                        "large-v3",
                        "unsafe primary prompt",
                        "unsafe lyrics prompt",
                    ),
                    ("faster-whisper", "large-v3", None, None),
                ],
            )
            self.assertEqual(read_srt(output)[0].text, ["prompt-free retry"])

    def test_process_passes_metadata_context_to_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, translation_metadata_context_enabled=True)
            worker = VideoWorker(config, _logger())
            source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]
            translator = Mock()

            def transcribe(_audio_path: Path, ja_srt: Path) -> None:
                write_srt(ja_srt, source_blocks)

            def translate_blocks(
                _blocks: list[SrtBlock],
                _ja_srt: Path,
                zh_cn_srt: Path,
                *,
                series_context: str = "",
            ) -> None:
                self.assertEqual(series_context, "Series metadata context")
                write_srt(zh_cn_srt, [SrtBlock(1, source_blocks[0].timing, ["translated"])])

            translator.translate_blocks.side_effect = translate_blocks
            with (
                patch("worker.extract_audio", return_value=None),
                patch.object(worker, "_transcribe", side_effect=transcribe),
                patch.object(worker, "_postprocess_ja_srt", return_value=source_blocks),
                patch.object(worker, "_get_translator", return_value=translator),
                patch.object(worker, "_convert_to_zh_tw", side_effect=lambda _src, dst: write_srt(dst, source_blocks)),
                patch(
                    "worker.build_series_metadata_context",
                    return_value=MetadataContext("Anime", "anilist", "Series metadata context", cached=False),
                ),
            ):
                worker._process_locked(video, root / "audio.wav", root / "vocals.wav")

            translator.translate_blocks.assert_called_once()


class CompletedDeliveryWorkerTests(unittest.TestCase):
    def test_strict_chinese_publication_runs_completed_delivery_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            worker = VideoWorker(
                _config(root, completed_delivery_enabled=True),
                _logger(),
            )
            result = SimpleNamespace(
                destination=str(root / "completed" / "episode.mkv"),
                to_dict=lambda: {"destination": "completed"},
            )
            with (
                patch.object(worker, "_has_strict_chinese_publication", return_value=True),
                patch.object(worker, "_set_stage") as set_stage,
                patch("completed_delivery.deliver_completed_mkv", return_value=result) as deliver,
            ):
                worker._deliver_completed_media_if_required(video)
            deliver.assert_called_once_with(video, worker.config, logger=worker.logger)
            self.assertEqual([call.args[1] for call in set_stage.call_args_list], ["mux", "move_completed"])


def _config(root: Path, **overrides: object) -> SimpleNamespace:
    config = dict(
        work_path=root,
        ai_japanese_ass_suffix=".AI日本語.ja.ass",
        ai_simplified_chinese_ass_suffix=".AI简日双语.zh-CN.ass",
        ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
        finished_subtitle_suffixes=[".official.zh-TW.ass"],
        export_ai_ass=False,
        require_ai_subtitles=False,
        keep_intermediate_files=True,
        enable_vocal_separation=False,
        vocal_separation_engine="none",
        vocal_separation_output="vocals",
        transcription_backend="faster-whisper",
        transcription_quality_check_enabled=True,
        transcription_quality_min_audio_seconds=600.0,
        transcription_quality_min_coverage_percent=8.0,
        transcription_quality_min_blocks_per_minute=1.5,
        transcription_quality_min_avg_logprob=-1.0,
        transcription_quality_max_low_confidence_percent=25.0,
        transcription_quality_min_confidence_segments=8,
        transcription_quality_max_leading_gap_seconds=30.0,
        enable_leading_gap_rescue=True,
        gap_rescue_leading_max_seconds=120.0,
        asr_diagnostics_enabled=False,
        asr_diagnostics_path="asr_diagnostics",
        write_gap_report=False,
        japanese_transcription_backend=None,
        whisper_model="fallback-model",
        whisper_language="ja",
        japanese_transcription_model=None,
        non_japanese_transcription_backend=None,
        non_japanese_transcription_model=None,
        translate_non_japanese_sources=False,
        language_detect_model=None,
        opencc_config="s2twp.json",
        ass_play_res_x=1920,
        ass_play_res_y=1080,
        ass_font_name="Noto Sans CJK TC",
        ass_primary_font_size=44,
        ass_secondary_font_size=25,
        ass_primary_color="&H00FFFFFF",
        ass_secondary_color="&HE6E6E6&",
        ass_outline_color="&H00000000",
        ass_back_color="&H80000000",
        ass_secondary_alpha="&H18&",
        ass_primary_outline=1.6,
        ass_secondary_outline=1.0,
        ass_shadow=0.0,
        ass_margin_l=40,
        ass_margin_r=40,
        ass_margin_v=54,
    )
    config.update(overrides)
    return SimpleNamespace(**config)


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.worker")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
