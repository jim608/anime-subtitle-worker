"""Publication guards use the existing analyzer thresholds, never mocked eligibility."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from subtitle_extract import normalize_sidecar_subtitles_for_output


def dialogue_ass(text="這裡的學校讓我們一起選擇明天的課程", count=100):
    return "[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "".join(
        f"Dialogue: 0,0:{i // 60:02}:{i % 60:02}.00,0:{(i + 1) // 60:02}:{(i + 1) % 60:02}.00,Default,,0,0,0,,{text}\n"
        for i in range(count)
    )


def dialogue_srt(text):
    return "".join(f"{i + 1}\n00:{i // 60:02}:{i % 60:02},000 --> 00:{(i + 1) // 60:02}:{(i + 1) % 60:02},000\n{text}\n\n" for i in range(100))


class MikanImportValidationTest(unittest.TestCase):
    def test_invalid_and_signs_only_cannot_publish_or_retire_ai(self):
        for text in ("<html>這裡的學校讓我們一起選擇明天的課程</html>", dialogue_ass(count=1),
                     dialogue_ass().replace("0:00:01.00", "bad-time")):
            with self.subTest(text=text[:40]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "download" / "Show - 01.mkv"
                source.parent.mkdir()
                sidecar = source.with_suffix(".zh-TW.ass")
                sidecar.write_text(text, encoding="utf-8")
                target = root / "library" / "Show - S01E01.mkv"
                target.parent.mkdir()
                target.write_bytes(b"source untouched")
                official = target.with_suffix(".zh-TW.ass")
                official.write_text("previous official", encoding="utf-8")
                ai = target.with_suffix(".AI.zh-TW.ass")
                ai.write_text("previous AI", encoding="utf-8")
                diagnostics = []
                with patch("source_inventory._probe_media", return_value={"format": {"duration": "100"}}), patch(
                    "subtitle_extract.remove_ai_subtitle_outputs"
                ) as remove_ai:
                    result = normalize_sidecar_subtitles_for_output(
                        source, SimpleNamespace(work_path=root / "work", mikan_remove_ai_after_extract=True), output_video_path=target,
                        diagnostics=diagnostics, validate_for_import=True,
                    )
                self.assertEqual(result, [])
                remove_ai.assert_not_called()
                self.assertEqual(official.read_text(encoding="utf-8"), "previous official")
                self.assertEqual(ai.read_text(encoding="utf-8"), "previous AI")
                self.assertEqual(target.read_bytes(), b"source untouched")
                self.assertTrue(any(d.get("status") == "validation_failed" for d in diagnostics))

    def test_valid_complete_dialogue_publishes_and_restart_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "download" / "Show - 01.mkv"
            source.parent.mkdir()
            sidecar = source.with_suffix(".zh-TW.ass")
            sidecar.write_text(dialogue_ass(), encoding="utf-8")
            target = root / "library" / "Show - S01E01.mkv"
            target.parent.mkdir()
            target.write_bytes(b"source untouched")
            for _ in range(2):
                diagnostics = []
                with patch("source_inventory._probe_media", return_value={"format": {"duration": "100"}}):
                    result = normalize_sidecar_subtitles_for_output(
                        source, SimpleNamespace(work_path=root / "work", mikan_remove_ai_after_extract=False), output_video_path=target,
                        diagnostics=diagnostics, validate_for_import=True,
                    )
                self.assertEqual(len(result), 1)
                self.assertTrue(any(d.get("output_parse") == "PASS" for d in diagnostics))
            self.assertEqual(len(list((root / "work" / "official_subtitle_versions").rglob("manifest.json"))), 1)
            self.assertTrue(sidecar.exists())
            self.assertEqual(target.read_bytes(), b"source untouched")


if __name__ == "__main__":
    unittest.main()
