from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from cleanup_generated import cleanup_generated_artifacts
from subtitle_paths import paths_for_video


class CleanupGeneratedTest(unittest.TestCase):
    def test_cleanup_removes_ai_srt_and_qa_without_touching_human_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            human_srt = root / "Anime S01E01.zh-TW.srt"
            ai_srt = root / "Anime S01E01.AI.zh-TW.srt"
            legacy_ai_srt = root / "Anime S01E01.AIlegacy.zh.srt"
            qa_report = root / "Anime S01E01.qa.txt"
            orphan_qa_report = root / "Orphan.qa.txt"
            human_srt.write_text("human", encoding="utf-8")
            ai_srt.write_text("ai", encoding="utf-8")
            legacy_ai_srt.write_text("ai", encoding="utf-8")
            qa_report.write_text("qa", encoding="utf-8")
            orphan_qa_report.write_text("qa", encoding="utf-8")
            config = SimpleNamespace(
                input_path=root,
                work_path=root / "work",
                video_extensions=[".mkv"],
                keep_intermediate_files=True,
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            )
            paths = paths_for_video(video, config)
            paths.zh_tw_srt.parent.mkdir(parents=True, exist_ok=True)
            paths.zh_tw_srt.write_text("cache", encoding="utf-8")

            result = cleanup_generated_artifacts(config, _logger())

            self.assertEqual(result.removed_ai_srt, 3)
            self.assertEqual(result.removed_qa, 2)
            self.assertEqual(result.removed_cache_dirs, 1)
            self.assertTrue(human_srt.exists())
            self.assertFalse(ai_srt.exists())
            self.assertFalse(legacy_ai_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertFalse(qa_report.exists())
            self.assertFalse(orphan_qa_report.exists())
            self.assertFalse((root / "work" / "ai_srt_cache").exists())


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.cleanup_generated")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
