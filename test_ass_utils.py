from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ass_utils import (
    AssStyle,
    ass_dialogue_style_to_srt_blocks,
    ass_style_is_current,
    convert_ass_file_to_srt,
    dominant_ass_dialogue_style,
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

    def test_dominant_ass_dialogue_style_requires_count_share_and_unique_leader(self) -> None:
        def dialogue(style: str, text: str) -> str:
            return (
                "Dialogue: 0,0:00:01.00,0:00:02.00,"
                f"{style},,0,0,0,,{text}"
            )

        content = "\n".join(
            [dialogue("English", f"English dialogue {index}") for index in range(20)]
            + [dialogue("Signs", f"Sign {index}") for index in range(9)]
        )
        self.assertEqual(dominant_ass_dialogue_style(content), "English")
        merged = ass_dialogue_style_to_srt_blocks(content, "English")
        self.assertEqual(len(merged), 20)
        self.assertTrue(all(f"Sign {index}" in merged[0].text for index in range(9)))

        unique_secondary_timing = content + "\n" + (
            "Dialogue: 0,0:00:03.00,0:00:04.00,Signs,,0,0,0,,Unique sign"
        )
        self.assertIsNone(dominant_ass_dialogue_style(unique_secondary_timing))

        too_few = "\n".join(
            dialogue("English", f"English dialogue {index}") for index in range(19)
        )
        self.assertIsNone(dominant_ass_dialogue_style(too_few))

        below_share = "\n".join(
            [dialogue("English", f"English dialogue {index}") for index in range(20)]
            + [dialogue("Signs", f"Sign {index}") for index in range(11)]
            + [dialogue("Songs", f"Song {index}") for index in range(10)]
        )
        self.assertIsNone(dominant_ass_dialogue_style(below_share))

        tied = "\n".join(
            [dialogue("English", f"English dialogue {index}") for index in range(20)]
            + [dialogue("Alternate", f"Alternate dialogue {index}") for index in range(20)]
        )
        self.assertIsNone(dominant_ass_dialogue_style(tied))

    def test_ass_dialogue_style_to_srt_blocks_filters_style_and_converts_text(self) -> None:
        content = "\n".join(
            [
                r"Dialogue: 0,0:00:01.23,0:00:03.45,English,,0,0,0,,{\i1}This is the first line.\NYou are here.",
                "Dialogue: 0,0:00:01.23,0:00:03.45,Signs,,0,0,0,,SHOP",
                "Dialogue: 0,0:00:07.00,0:00:08.00,english,,0,0,0,,This is the second line.",
            ]
        )

        blocks = ass_dialogue_style_to_srt_blocks(content, "English")

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].index, 1)
        self.assertEqual(blocks[0].timing, "00:00:01,230 --> 00:00:03,450")
        self.assertEqual(
            blocks[0].text,
            ["This is the first line.", "You are here.", "SHOP"],
        )
        self.assertEqual(blocks[1].index, 2)
        self.assertEqual(blocks[1].text, ["This is the second line."])


if __name__ == "__main__":
    unittest.main()
