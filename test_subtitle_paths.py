from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from subtitle_paths import finished_subtitle_paths, has_ai_finished_subtitle, has_finished_subtitle, paths_for_video


CONFIG = SimpleNamespace(
    work_path=Path("work"),
    export_ai_ass=True,
    ai_japanese_ass_suffix=".AI\u65e5\u672c\u8a9e.ja.ass",
    ai_simplified_chinese_ass_suffix=".AI\u7b80\u65e5\u53cc\u8bed.zh-CN.ass",
    ai_traditional_chinese_ass_suffix=".AI\u7e41\u65e5\u96d9\u8a9e.zh-TW.ass",
    ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
    finished_subtitle_suffixes=[
        ".AI\u7e41\u65e5\u96d9\u8a9e.zh-TW.ass",
        ".AI\u7b80\u65e5\u53cc\u8bed.zh-CN.ass",
        ".\u7e41\u9ad4\u4e2d\u6587.zh-TW.ass",
        ".zh-TW.ass",
        ".zh-CN.ass",
    ],
)


class SubtitlePathsTest(unittest.TestCase):
    def test_ai_ass_paths_use_requested_names(self) -> None:
        paths = paths_for_video(Path("Anime S01E01.mkv"), CONFIG)

        self.assertEqual(paths.ai_ja_ass, Path("Anime S01E01.AI\u65e5\u672c\u8a9e.ja.ass"))
        self.assertEqual(paths.ai_zh_cn_ass, Path("Anime S01E01.AI\u7b80\u65e5\u53cc\u8bed.zh-CN.ass"))
        self.assertEqual(paths.ai_zh_tw_ass, Path("Anime S01E01.AI\u7e41\u65e5\u96d9\u8a9e.zh-TW.ass"))
        self.assertEqual(paths.ja_srt.parent, Path("work") / "ai_srt_cache")
        self.assertEqual(paths.zh_cn_srt.parent, Path("work") / "ai_srt_cache")
        self.assertEqual(paths.zh_tw_srt.parent, Path("work") / "ai_srt_cache")
        self.assertTrue(paths.ja_srt.name.endswith(".AI\u65e5\u672c\u8a9e.ja.srt"))
        self.assertTrue(paths.zh_cn_srt.name.endswith(".AI\u7b80\u65e5\u53cc\u8bed.zh-CN.srt"))
        self.assertTrue(paths.zh_tw_srt.name.endswith(".AI\u7e41\u65e5\u96d9\u8a9e.zh-TW.srt"))

    def test_finished_paths_do_not_treat_internal_zh_tw_srt_as_done_when_exporting_ass(self) -> None:
        paths = finished_subtitle_paths(Path("Anime S01E01.mkv"), CONFIG)

        self.assertNotIn(Path("Anime S01E01.zh-TW.srt"), paths)
        self.assertNotIn(Path("Anime S01E01.AI\u7e41\u65e5\u96d9\u8a9e.zh-TW.srt"), paths)
        self.assertIn(Path("Anime S01E01.\u7e41\u9ad4\u4e2d\u6587.zh-TW.ass"), paths)

    def test_finished_paths_accept_ai_zh_tw_srt_when_ass_export_disabled(self) -> None:
        config = SimpleNamespace(
            **{
                **CONFIG.__dict__,
                "export_ai_ass": False,
            }
        )

        paths = finished_subtitle_paths(Path("Anime S01E01.mkv"), config)

        self.assertIn(paths_for_video(Path("Anime S01E01.mkv"), config).zh_tw_srt, paths)
        self.assertIn(Path("Anime S01E01.zh-TW.srt"), paths)

    def test_finished_detection_accepts_named_traditional_chinese_sidecar_srt_when_exporting_ass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            sidecar = Path(temp_dir) / "Anime S01E01.\u7e41\u9ad4\u4e2d\u6587.zh-TW.srt"
            sidecar.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n\u660e\u5929\u9078\u73ed\u9577\n",
                encoding="utf-8",
            )

            self.assertTrue(has_finished_subtitle(video, CONFIG))

    def test_finished_detection_accepts_named_traditional_chinese_sidecar_srt_when_ass_export_disabled(self) -> None:
        config = SimpleNamespace(
            **{
                **CONFIG.__dict__,
                "export_ai_ass": False,
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            sidecar = Path(temp_dir) / "Anime S01E01.\u7e41\u9ad4\u4e2d\u6587.zh-TW.srt"
            sidecar.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n\u660e\u5929\u9078\u73ed\u9577\n",
                encoding="utf-8",
            )

            self.assertTrue(has_finished_subtitle(video, config))

    def test_ai_finished_detection_requires_canonical_ai_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            official = Path(temp_dir) / "Anime S01E01.zh-TW.ass"
            official.write_text(
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,明天選班長\n",
                encoding="utf-8",
            )

            self.assertTrue(has_finished_subtitle(video, CONFIG))
            self.assertFalse(has_ai_finished_subtitle(video, CONFIG))

            paths_for_video(video, CONFIG).ai_zh_tw_ass.write_text(
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,這裡會選擇開啟網路連線\n",
                encoding="utf-8",
            )

            self.assertTrue(has_ai_finished_subtitle(video, CONFIG))

    def test_source_language_ai_transcript_does_not_count_as_zh_tw_finished(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            source_ass = Path(temp_dir) / "Anime S01E01.AIEnglish.en.ass"
            source_ass.write_text("source transcript", encoding="utf-8")

            self.assertFalse(has_finished_subtitle(video, CONFIG))
            self.assertFalse(has_ai_finished_subtitle(video, CONFIG))

    def test_source_language_ai_transcript_uses_language_label(self) -> None:
        from subtitle_paths import source_transcript_paths_for_video

        paths = source_transcript_paths_for_video(Path("Anime S01E01.mkv"), CONFIG, "en")

        self.assertEqual(paths.ass, Path("Anime S01E01.AIEnglish.en.ass"))
        self.assertTrue(paths.srt.name.endswith(".AIEnglish.en.srt"))


if __name__ == "__main__":
    unittest.main()
