from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main as main_module
from output_manifest import (
    ADOPTED_ZH_TW_PUBLICATION_KIND,
    CONVERTED_ZH_CN_PUBLICATION_KIND,
    manifest_publication_semantics,
    output_manifest_path,
    validate_output_manifest,
    write_output_manifest,
)
from safe_files import sha256_file
from scanner import VideoScanner
from source_decision import (
    CONVERT_ZH_CN,
    TRANSLATE_JAPANESE,
    USE_ZH_TW,
    select_subtitle_source,
)
from srt_utils import SrtBlock, read_srt, write_srt
from subtitle_extract import ExtractedSubtitle
from subtitle_paths import paths_for_video
from test_scanner import _config as _scanner_config
from test_scanner import _logger as _scanner_logger
from test_worker import _config as _worker_config
from test_worker import _logger as _worker_logger
from worker import VideoWorker


class SubtitleSourcePriorityTest(unittest.TestCase):
    def test_deterministic_multisubtitle_priority_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _worker_config(root, mikan_remove_ai_after_extract=False)
            zh_tw = root / "episode.zh-TW.ass"
            zh_cn = root / "episode.zh.ass"
            japanese = root / "episode.ja.ass"
            _write_ass(zh_tw, [("0:00:01.00", "0:00:03.00", "這裡會選擇開啟網路連線並顯示資訊")])
            _write_ass(zh_cn, [("0:00:01.00", "0:00:03.00", "这里会选择开启网络连接并显示信息")])
            _write_ass(japanese, [("0:00:01.00", "0:00:03.00", "今日は友達と学校へ行きます。")])

            candidates = [
                ExtractedSubtitle(japanese, "ja", 7),
                ExtractedSubtitle(zh_cn, "zh-cn", 5),
                ExtractedSubtitle(zh_tw, "zh-tw", 9),
            ]
            decision = select_subtitle_source(candidates, config, source_kind="embedded")
            self.assertIsNotNone(decision)
            self.assertEqual(decision.strategy, USE_ZH_TW)
            self.assertEqual(decision.stream_index, 9)

            _write_ass(zh_tw, [("0:00:01.00", "0:00:03.00", "English text only")])
            decision = select_subtitle_source(candidates, config, source_kind="embedded")
            self.assertIsNotNone(decision)
            self.assertEqual(decision.strategy, CONVERT_ZH_CN)
            self.assertEqual(decision.stream_index, 5)

            _write_ass(zh_cn, [("0:00:01.00", "0:00:03.00", "Ambiguous text only")])
            decision = select_subtitle_source(candidates, config, source_kind="embedded")
            self.assertIsNotNone(decision)
            self.assertEqual(decision.strategy, TRANSLATE_JAPANESE)
            self.assertEqual(decision.stream_index, 7)

            _write_ass(japanese, [("0:00:01.00", "0:00:03.00", "Ambiguous text only")])
            self.assertIsNone(
                select_subtitle_source(candidates, config, source_kind="embedded")
            )

    def test_zh_cn_conversion_uses_no_audio_asr_or_llm_and_preserves_ass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"media")
            source = root / "Anime S01E01.zh.ass"
            _write_ass(
                source,
                [
                    (
                        "0:00:01.23",
                        "0:00:03.45",
                        r"{\an8}这里会选择开启网络连接并显示信息",
                    )
                ],
            )
            config = _worker_config(
                root,
                export_ai_ass=True,
                scanner_cache_enabled=False,
                ai_output_manifest_path=root / "manifests",
                mikan_remove_ai_after_extract=False,
            )
            worker = VideoWorker(config, _worker_logger())

            with (
                patch.object(worker, "_extract_preferred_audio") as preferred_audio,
                patch("worker.extract_audio") as raw_audio,
                patch("worker.preferred_audio_stream_info") as audio_stream_probe,
                patch("worker.validate_cached_audio") as audio_cache_probe,
                patch.object(worker, "_detect_source_language") as detect_language,
                patch.object(worker, "_transcribe") as transcribe,
                patch.object(worker, "_get_translator") as translator,
            ):
                outcome = worker._process_locked(
                    video,
                    root / "audio.wav",
                    root / "vocals.wav",
                )

            self.assertEqual((outcome.stage, outcome.status), ("complete", "ok"))
            preferred_audio.assert_not_called()
            raw_audio.assert_not_called()
            audio_stream_probe.assert_not_called()
            audio_cache_probe.assert_not_called()
            detect_language.assert_not_called()
            transcribe.assert_not_called()
            translator.assert_not_called()

            output = root / "Anime S01E01.zh-TW.ass"
            source_event = _dialogue_fields(source)[0]
            output_event = _dialogue_fields(output)[0]
            self.assertEqual(output_event[:9], source_event[:9])
            self.assertIn(r"{\an8}", output_event[9])
            self.assertIn("這裡會選擇開啟網路連線並顯示資訊", output_event[9])

            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    require_publication_semantics=True,
                )
            )
            payload = _manifest(video, config)
            self.assertEqual(
                manifest_publication_semantics(payload),
                {
                    "contract": "ai-publication-semantics-v2",
                    "kind": CONVERTED_ZH_CN_PUBLICATION_KIND,
                    "output_languages": ["zh-TW"],
                },
            )
            evidence = payload["provenance"]["subtitle_source"]
            self.assertEqual(evidence["strategy"], CONVERT_ZH_CN)
            self.assertEqual(evidence["source_language"], "zh-cn")
            self.assertIs(evidence["asr_used"], False)
            self.assertEqual(evidence["source_sha256"], sha256_file(source))
            self.assertEqual(payload["outputs"][0]["sha256"], sha256_file(output))

            tampered = copy.deepcopy(payload)
            tampered["provenance"]["subtitle_source"]["asr_used"] = True
            output_manifest_path(video, config).write_text(
                json.dumps(tampered, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.assertFalse(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    require_publication_semantics=True,
                )
            )

    def test_zh_tw_adoption_uses_no_audio_or_ai_and_has_strict_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E02.mkv"
            video.write_bytes(b"media")
            source = root / "Anime S01E02.zh-TW.ass"
            _write_ass(
                source,
                [("0:00:01.00", "0:00:03.00", "這裡會選擇開啟網路連線並顯示資訊")],
            )
            config = _worker_config(
                root,
                export_ai_ass=True,
                scanner_cache_enabled=False,
                ai_output_manifest_path=root / "manifests",
                mikan_remove_ai_after_extract=False,
            )
            worker = VideoWorker(config, _worker_logger())

            with (
                patch.object(worker, "_extract_preferred_audio") as preferred_audio,
                patch("worker.extract_audio") as raw_audio,
                patch.object(worker, "_detect_source_language") as detect_language,
                patch.object(worker, "_transcribe") as transcribe,
                patch.object(worker, "_get_translator") as translator,
            ):
                outcome = worker._process_locked(
                    video,
                    root / "audio.wav",
                    root / "vocals.wav",
                )

            self.assertEqual((outcome.stage, outcome.status), ("complete", "ok"))
            preferred_audio.assert_not_called()
            raw_audio.assert_not_called()
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
            payload = _manifest(video, config)
            self.assertEqual(payload["publication_kind"], ADOPTED_ZH_TW_PUBLICATION_KIND)
            self.assertEqual(payload["publication"]["output_languages"], ["zh-TW"])
            self.assertEqual(
                payload["provenance"]["subtitle_source"]["strategy"],
                USE_ZH_TW,
            )
            self.assertIs(
                payload["provenance"]["subtitle_source"]["asr_used"],
                False,
            )

    def test_japanese_subtitle_translation_uses_no_audio_or_asr_and_preserves_times(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E03.mkv"
            video.write_bytes(b"media")
            source = root / "Anime S01E03.ja.ass"
            _write_ass(
                source,
                [
                    ("0:00:01.23", "0:00:03.45", "今日は友達と学校へ行きます。"),
                    ("0:00:04.56", "0:00:06.78", "明日は一緒に勉強します。"),
                ],
            )
            config = _worker_config(
                root,
                export_ai_ass=True,
                scanner_cache_enabled=False,
                keep_intermediate_files=True,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                ai_output_manifest_path=root / "manifests",
                mikan_remove_ai_after_extract=False,
            )
            worker = VideoWorker(config, _worker_logger())
            translator = Mock()

            def translate(blocks, _source_path, destination, **_kwargs) -> None:
                texts = ["今天和朋友一起去学校。", "明天一起学习。"]
                write_srt(
                    destination,
                    [
                        SrtBlock(block.index, block.timing, [texts[index]])
                        for index, block in enumerate(blocks)
                    ],
                )

            translator.translate_blocks.side_effect = translate

            with (
                patch.object(worker, "_extract_preferred_audio") as preferred_audio,
                patch("worker.extract_audio") as raw_audio,
                patch("worker.preferred_audio_stream_info") as audio_stream_probe,
                patch("worker.validate_cached_audio") as audio_cache_probe,
                patch.object(worker, "_detect_source_language") as detect_language,
                patch.object(worker, "_transcribe") as transcribe,
                patch.object(worker, "_postprocess_ja_srt") as postprocess,
                patch.object(worker, "_build_series_metadata_context", return_value=None),
                patch.object(worker, "_get_translator", return_value=translator),
            ):
                outcome = worker._process_locked(
                    video,
                    root / "audio.wav",
                    root / "vocals.wav",
                )

            self.assertEqual((outcome.stage, outcome.status), ("complete", "ok"))
            preferred_audio.assert_not_called()
            raw_audio.assert_not_called()
            audio_stream_probe.assert_not_called()
            audio_cache_probe.assert_not_called()
            detect_language.assert_not_called()
            transcribe.assert_not_called()
            postprocess.assert_not_called()
            translator.translate_blocks.assert_called_once()

            paths = paths_for_video(video, config)
            expected_timings = [
                "00:00:01,230 --> 00:00:03,450",
                "00:00:04,560 --> 00:00:06,780",
            ]
            self.assertEqual(
                [block.timing for block in read_srt(paths.ja_srt)],
                expected_timings,
            )
            self.assertEqual(
                [block.timing for block in read_srt(paths.zh_cn_srt)],
                expected_timings,
            )
            self.assertEqual(
                [block.timing for block in read_srt(paths.zh_tw_srt)],
                expected_timings,
            )
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    require_publication_semantics=True,
                )
            )
            payload = _manifest(video, config)
            self.assertEqual(payload["publication_kind"], "translated_trilingual")
            evidence = payload["provenance"]["subtitle_source"]
            self.assertEqual(evidence["strategy"], TRANSLATE_JAPANESE)
            self.assertEqual(evidence["source_language"], "ja")
            self.assertIs(evidence["asr_used"], False)

    def test_scanner_keeps_invalid_named_zh_tw_and_skips_audio_probe_for_valid_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            invalid_video = root / "Invalid S01E01.mkv"
            invalid_video.write_bytes(b"media")
            _write_ass(
                root / "Invalid S01E01.official.zh-TW.ass",
                [("0:00:01.00", "0:00:03.00", "English text only")],
            )
            config = _scanner_config(root)
            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(
                    VideoScanner(config, _scanner_logger()).scan(),
                    [invalid_video.resolve()],
                )

        for language, text, suffix in (
            ("zh-cn", "这里会选择开启网络连接并显示信息", ".zh.ass"),
            ("ja", "今日は友達と学校へ行きます。", ".ja.ass"),
        ):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "Source S01E01.mkv"
                video.write_bytes(b"media")
                _write_ass(
                    root / f"Source S01E01{suffix}",
                    [("0:00:01.00", "0:00:03.00", text)],
                )
                config = _scanner_config(
                    root,
                    language_gate_enabled=True,
                    allowed_source_languages=["ja"],
                    skip_non_allowed_language=True,
                    transcribe_non_allowed_languages=False,
                )
                with (
                    patch("scanner.extract_available_subtitles", return_value=[]),
                    patch("scanner.probe_audio_stream_manifest") as audio_probe,
                ):
                    self.assertEqual(
                        VideoScanner(config, _scanner_logger()).scan(),
                        [video.resolve()],
                    )
                audio_probe.assert_not_called()

    def test_source_language_manifest_is_not_traditional_chinese_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "English S01E01.mkv"
            output = root / "English S01E01.AIEnglish.en.ass"
            video.write_bytes(b"media")
            output.write_text("English source transcript", encoding="utf-8")
            config = _worker_config(
                root,
                export_ai_ass=True,
                scanner_cache_enabled=False,
                ai_output_manifest_path=root / "manifests",
            )
            write_output_manifest(
                video,
                config,
                [output],
                publication_kind="source_language",
                output_languages=("en",),
            )
            payload = _manifest(video, config)
            self.assertIsNone(
                main_module._verified_ai_delivery_evidence(
                    video,
                    config,
                    obligation_id=payload["delivery"]["obligation_id"],
                    expected_policy_revision=payload["delivery"]["policy_revision"],
                    attempt_started_at=0.0,
                )
            )


def _write_ass(path: Path, events: list[tuple[str, str, str]]) -> None:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines.extend(
        f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}"
        for start, end, text in events
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def _dialogue_fields(path: Path) -> list[list[str]]:
    return [
        line.split(":", 1)[1].lstrip().split(",", 9)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.startswith("Dialogue:")
    ]


def _manifest(video: Path, config) -> dict[str, object]:
    return json.loads(output_manifest_path(video, config).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
