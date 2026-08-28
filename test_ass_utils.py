from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ass_utils import (
    AssStyle,
    ass_style_is_current,
    convert_ass_file_to_srt,
    format_ass,
    format_bilingual_ass,
    restyle_ass_file,
)
from srt_utils import SrtBlock, read_srt


class AssUtilsTest(unittest.TestCase):
    def test_format_ass_converts_timing_and_text(self) -> None:
        content = format_ass(
            [
                SrtBlock(
                    1,
                    "00:01:02,340 --> 00:01:04,560",
                    ["第一行", "第二行{tag}"],
                )
            ]
        )

        self.assertIn("Format: Layer, Start, End", content)
        self.assertIn("PlayResX: 1920", content)
        self.assertIn("PlayResY: 1080", content)
        self.assertIn(r"Dialogue: 0,0:01:02.34,0:01:04.56,Default,,0,0,0,,第一行\N第二行｛tag｝", content)

    def test_convert_generated_ass_back_to_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ass_path = root / "Episode.AI日本語.ja.ass"
            srt_path = root / "cache" / "Episode.AI日本語.ja.srt"
            ass_path.write_text(
                format_ass(
                    [
                        SrtBlock(
                            7,
                            "00:01:02,340 --> 00:01:04,560",
                            ["第一行", "第二行,有逗號"],
                        )
                    ]
                ),
                encoding="utf-8-sig",
            )

            convert_ass_file_to_srt(ass_path, srt_path)

            blocks = read_srt(srt_path)
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0].index, 1)
            self.assertEqual(blocks[0].timing, "00:01:02,340 --> 00:01:04,560")
            self.assertEqual(blocks[0].text, ["第一行", "第二行,有逗號"])

    def test_format_bilingual_ass_puts_primary_chinese_above_small_japanese(self) -> None:
        content = format_bilingual_ass(
            [
                SrtBlock(
                    1,
                    "00:01:02,340 --> 00:01:04,560",
                    ["繁體中文"],
                )
            ],
            [
                SrtBlock(
                    1,
                    "00:01:02,340 --> 00:01:04,560",
                    ["日本語"],
                )
            ],
        )

        self.assertIn(
            r"Dialogue: 0,0:01:02.34,0:01:04.56,Default,,0,0,0,,繁體中文\N{\fs32",
            content,
        )
        self.assertIn("日本語", content)

    def test_restyle_ass_file_updates_default_and_inline_secondary_style(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Anime S01E01.AI繁日雙語.zh-TW.ass"
            path.write_text(
                "\n".join(
                    [
                        "[Script Info]",
                        "PlayResX: 1920",
                        "PlayResY: 1080",
                        "[V4+ Styles]",
                        "Style: Default,Noto Sans CJK TC,44,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1.6,0,2,40,40,54,1",
                        "[Events]",
                        r"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,繁體中文\N{\fs25\c&HE6E6E6&\alpha&H18&\bord1\shad0}日本語",
                    ]
                )
                + "\n",
                encoding="utf-8-sig",
            )

            changed = restyle_ass_file(path, AssStyle(primary_font_size=58, secondary_font_size=32, margin_v=70))

            content = path.read_text(encoding="utf-8-sig")
            self.assertTrue(changed)
            self.assertIn("Style: Default,Noto Sans CJK TC,58", content)
            self.assertIn("40,40,70,1", content)
            self.assertIn(r"\N{\fs32", content)
            self.assertTrue(ass_style_is_current(path, AssStyle(primary_font_size=58, secondary_font_size=32, margin_v=70)))

    def test_format_bilingual_ass_uses_custom_style(self) -> None:
        content = format_bilingual_ass(
            [
                SrtBlock(
                    1,
                    "00:01:02,340 --> 00:01:04,560",
                    ["繁體中文"],
                )
            ],
            [
                SrtBlock(
                    1,
                    "00:01:02,340 --> 00:01:04,560",
                    ["日本語"],
                )
            ],
            AssStyle(
                play_res_x=1280,
                play_res_y=720,
                primary_font_size=36,
                secondary_font_size=18,
                primary_outline=1.2,
                secondary_outline=0.7,
                margin_v=44,
            ),
        )

        self.assertIn("PlayResX: 1280", content)
        self.assertIn("PlayResY: 720", content)
        self.assertIn("Style: Default,Noto Sans CJK TC,36", content)
        self.assertIn(r"\N{\fs18", content)
        self.assertIn(r"\bord0.7", content)


if __name__ == "__main__":
    unittest.main()
