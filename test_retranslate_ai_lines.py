from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from output_manifest import (
    output_manifest_path,
    output_publication_marker_path,
    validate_output_manifest,
    write_output_manifest,
)
from retranslate_ai_lines import retranslate_lines
from safe_files import sha256_file
from srt_utils import SrtBlock, read_srt, write_srt
from subtitle_paths import paths_for_video
from subtitle_quality import SubtitleQualityError
from transcriber import asr_diagnostics_path
from translation_quality import translation_quality_events_path
from worker import VideoWorker


class RetranslateAiLinesSafetyTest(unittest.TestCase):
    def test_known_source_hallucination_requires_full_retranscribe_before_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, paths = _fixture(root)
            source = read_srt(paths.ja_srt)
            write_srt(
                paths.ja_srt,
                [
                    SrtBlock(
                        source[0].index,
                        source[0].timing,
                        ["この動画の字幕は視聴者によって作成されました。"],
                    ),
                    *source[1:],
                ],
            )
            worker = _worker_that_publishes(paths)
            translator = Mock()

            with (
                patch("retranslate_ai_lines.VideoWorker", return_value=worker),
                patch("retranslate_ai_lines.SubtitleTranslator", return_value=translator),
                self.assertRaisesRegex(RuntimeError, "use full retranscribe instead"),
            ):
                retranslate_lines(config, video, {1}, logging.getLogger("test.retranslate.hallucination"))

            translator.translate_blocks.assert_not_called()
            self.assertEqual(read_srt(paths.ja_srt)[0].text, ["この動画の字幕は視聴者によって作成されました。"])

    def test_corrupt_quality_events_fail_closed_without_translation_or_clearing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, paths = _fixture(root)
            event_path = translation_quality_events_path(paths.zh_cn_srt)
            event_path.write_text("{corrupt-json", encoding="utf-8")
            old_translation = paths.zh_cn_srt.read_bytes()
            worker = _worker_that_publishes(paths)
            translator = Mock()

            with (
                patch("retranslate_ai_lines.VideoWorker", return_value=worker),
                patch(
                    "retranslate_ai_lines.SubtitleTranslator",
                    return_value=translator,
                ),
                patch(
                    "retranslate_ai_lines.build_series_metadata_context",
                    return_value=None,
                ),
                self.assertRaisesRegex(RuntimeError, "corrupt"),
            ):
                retranslate_lines(
                    config,
                    video,
                    {1},
                    logging.getLogger("test.retranslate.corrupt-events"),
                )

            translator.translate_blocks.assert_not_called()
            self.assertEqual(paths.zh_cn_srt.read_bytes(), old_translation)
            self.assertEqual(
                event_path.read_text(encoding="utf-8"),
                "{corrupt-json",
            )

    def test_success_uses_staged_publisher_and_writes_complete_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, paths = _fixture(root)
            worker = _worker_that_publishes(paths)
            translator = _translator_with_replacement("fixed-one")

            with (
                patch("retranslate_ai_lines.VideoWorker", return_value=worker),
                patch("retranslate_ai_lines.SubtitleTranslator", return_value=translator),
                patch("retranslate_ai_lines.build_series_metadata_context", return_value=None),
            ):
                result = retranslate_lines(config, video, {1}, logging.getLogger("test.retranslate"))

            worker._publish_ai_ass.assert_called_once_with(video.resolve(), paths)
            worker._export_ai_ass.assert_not_called()
            self.assertTrue(
                validate_output_manifest(
                    video.resolve(),
                    config,
                    verify_hashes=True,
                    required_outputs=[paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                )
            )
            self.assertFalse(output_publication_marker_path(video.resolve(), config).exists())
            self.assertEqual(read_srt(paths.zh_cn_srt)[0].text, ["fixed-one"])
            self.assertTrue((Path(str(result["archive"])) / "result.json").is_file())

    def test_success_keeps_japanese_srt_when_intermediates_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, paths = _fixture(root)
            config.keep_intermediate_files = False
            worker = _worker_that_publishes(paths)
            translator = _translator_with_replacement("fixed-one")

            with (
                patch("retranslate_ai_lines.VideoWorker", return_value=worker),
                patch("retranslate_ai_lines.SubtitleTranslator", return_value=translator),
                patch("retranslate_ai_lines.build_series_metadata_context", return_value=None),
            ):
                retranslate_lines(config, video, {1}, logging.getLogger("test.retranslate"))

            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertTrue(paths.ai_ja_ass.exists())
            self.assertTrue(paths.ai_zh_cn_ass.exists())
            self.assertTrue(paths.ai_zh_tw_ass.exists())

    def test_publish_failure_restores_outputs_caches_manifest_and_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, paths = _fixture(root)
            outputs = [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass]
            old_output_content = [path.read_text(encoding="utf-8") for path in outputs]
            old_translation = paths.zh_cn_srt.read_text(encoding="utf-8")
            old_manifest = output_manifest_path(video, config).read_text(encoding="utf-8")
            source_blocks = read_srt(paths.ja_srt)
            paths.ja_srt.unlink()
            worker = Mock()
            worker._restore_japanese_srt_cache_from_ass.side_effect = lambda _paths: write_srt(
                paths.ja_srt,
                source_blocks,
            )
            worker._convert_to_zh_tw.side_effect = lambda src, dst: write_srt(dst, read_srt(src))

            def fail_after_partial_publish(_video: Path, _paths: object) -> None:
                paths.ai_ja_ass.write_text("new-ja", encoding="utf-8")
                paths.ai_zh_cn_ass.write_text("new-zh-cn", encoding="utf-8")
                raise RuntimeError("injected staged publisher failure")

            worker._publish_ai_ass.side_effect = fail_after_partial_publish
            translator = _translator_with_replacement("fixed-one")

            with (
                patch("retranslate_ai_lines.VideoWorker", return_value=worker),
                patch("retranslate_ai_lines.SubtitleTranslator", return_value=translator),
                patch("retranslate_ai_lines.build_series_metadata_context", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected staged publisher failure"):
                    retranslate_lines(config, video, {1}, logging.getLogger("test.retranslate"))

            self.assertEqual(
                [path.read_text(encoding="utf-8") for path in outputs],
                old_output_content,
            )
            self.assertEqual(paths.zh_cn_srt.read_text(encoding="utf-8"), old_translation)
            self.assertFalse(paths.ja_srt.exists())
            self.assertEqual(output_manifest_path(video, config).read_text(encoding="utf-8"), old_manifest)
            self.assertFalse(output_publication_marker_path(video, config).exists())
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    required_outputs=outputs,
                )
            )
            archives = list((config.work_path / "manual_line_retranslate").glob("*"))
            self.assertEqual(len(archives), 1)
            archive_manifest = json.loads((archives[0] / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(all(entry.get("sha256") for entry in archive_manifest["copied"]))

    def test_direct_retranslate_cannot_publish_legacy_rejected_japanese_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, video, paths = _fixture(root)
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
            worker = VideoWorker(config, logging.getLogger("test.retranslate.gate"))
            translator = _translator_with_replacement("must-not-publish")

            with (
                patch("retranslate_ai_lines.VideoWorker", return_value=worker),
                patch(
                    "retranslate_ai_lines.SubtitleTranslator",
                    return_value=translator,
                ),
                patch(
                    "retranslate_ai_lines.build_series_metadata_context",
                    return_value=None,
                ),
                patch.object(
                    worker,
                    "_convert_to_zh_tw",
                    side_effect=lambda src, dst: write_srt(dst, read_srt(src)),
                ),
                patch.object(worker, "_export_ai_ass") as export,
                self.assertRaises(SubtitleQualityError),
            ):
                retranslate_lines(
                    config,
                    video,
                    {1},
                    logging.getLogger("test.retranslate.gate"),
                )

            export.assert_not_called()
            self.assertTrue(diagnostic.exists())
            self.assertEqual(
                json.loads(diagnostic.read_text(encoding="utf-8"))["status"],
                "selective_retry_required",
            )


def _fixture(root: Path) -> tuple[SimpleNamespace, Path, object]:
    work = root / "work"
    work.mkdir()
    config = SimpleNamespace(
        input_path=root,
        work_path=work,
        video_extensions={".mkv"},
        ai_japanese_ass_suffix=".AI.ja.ass",
        ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
        ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
        keep_intermediate_files=True,
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
    video = root / "Anime S01E01.mkv"
    video.write_bytes(b"video")
    paths = paths_for_video(video, config)
    source = [
        SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source-one"]),
        SrtBlock(2, "00:00:03,000 --> 00:00:04,000", ["source-two"]),
    ]
    translated = [
        SrtBlock(1, source[0].timing, ["old-one"]),
        SrtBlock(2, source[1].timing, ["old-two"]),
    ]
    write_srt(paths.ja_srt, source)
    write_srt(paths.zh_cn_srt, translated)
    write_srt(paths.zh_tw_srt, translated)
    for path, content in (
        (paths.ai_ja_ass, "old-ja"),
        (paths.ai_zh_cn_ass, "old-zh-cn"),
        (paths.ai_zh_tw_ass, "old-zh-tw"),
    ):
        path.write_text(content, encoding="utf-8")
    write_output_manifest(
        video,
        config,
        [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
        provenance={"operation": "fixture"},
    )
    return config, video, paths


def _translator_with_replacement(text: str) -> Mock:
    translator = Mock()

    def translate(blocks, _ja_srt, output, **_kwargs) -> None:
        write_srt(
            output,
            [SrtBlock(block.index, block.timing, [text]) for block in blocks],
        )

    translator.translate_blocks.side_effect = translate
    return translator


def _worker_that_publishes(paths: object) -> Mock:
    worker = Mock()
    worker._restore_japanese_srt_cache_from_ass.return_value = None
    worker._convert_to_zh_tw.side_effect = lambda src, dst: write_srt(dst, read_srt(src))

    def publish(_video: Path, _paths: object) -> None:
        for path, content in (
            (paths.ai_ja_ass, "new-ja"),
            (paths.ai_zh_cn_ass, "new-zh-cn"),
            (paths.ai_zh_tw_ass, "new-zh-tw"),
        ):
            path.write_text(content, encoding="utf-8")

    worker._publish_ai_ass.side_effect = publish
    return worker


if __name__ == "__main__":
    unittest.main()
