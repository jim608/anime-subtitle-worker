from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from opencc_convert import OpenCCError, convert_srt_to_zh_tw
from srt_utils import (
    SrtBlock,
    format_srt,
    parse_srt,
    validate_translation,
    write_srt,
)


class SrtUtilsTest(unittest.TestCase):
    def test_parse_utf8_bom_srt(self) -> None:
        blocks = parse_srt(
            "\ufeff1\r\n"
            "00:00:01,000 --> 00:00:02,000\r\n"
            "こんにちは\r\n\r\n"
        )

        self.assertEqual(blocks, [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["こんにちは"])])

    def test_format_srt_uses_lf_and_trailing_newline(self) -> None:
        content = format_srt([SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["測試"])])

        self.assertEqual(content, "1\n00:00:01,000 --> 00:00:02,000\n測試\n")

    def test_validate_translation_rejects_timing_changes(self) -> None:
        original = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["a"])]
        translated = [SrtBlock(1, "00:00:01,100 --> 00:00:02,000", ["b"])]

        with self.assertRaises(ValueError):
            validate_translation(original, translated)

    def test_atomic_srt_write_failure_preserves_old_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "episode.srt"
            old_bytes = b"\xef\xbb\xbfold target\n"
            target.write_bytes(old_bytes)

            with (
                patch(
                    "safe_files._replace_with_permission_retry",
                    side_effect=OSError("interrupted replace"),
                ),
                self.assertRaisesRegex(OSError, "interrupted replace"),
            ):
                write_srt(
                    target,
                    [
                        SrtBlock(
                            1,
                            "00:00:01,000 --> 00:00:02,000",
                            ["new target"],
                        )
                    ],
                )

            self.assertEqual(target.read_bytes(), old_bytes)

    def test_atomic_opencc_write_failure_preserves_old_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.srt"
            target = root / "target.srt"
            source.write_text("simplified\n", encoding="utf-8-sig")
            old_bytes = b"\xef\xbb\xbfold traditional\n"
            target.write_bytes(old_bytes)
            converter = SimpleNamespace(convert=lambda content: "traditional\n")
            fake_opencc = SimpleNamespace(OpenCC=lambda _config: converter)

            with (
                patch.dict(sys.modules, {"opencc": fake_opencc}),
                patch(
                    "safe_files._replace_with_permission_retry",
                    side_effect=OSError("interrupted replace"),
                ),
                self.assertRaisesRegex(OpenCCError, "interrupted replace"),
            ):
                convert_srt_to_zh_tw(source, target, "s2twp")

            self.assertEqual(target.read_bytes(), old_bytes)


if __name__ == "__main__":
    unittest.main()
