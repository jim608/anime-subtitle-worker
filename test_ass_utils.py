from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ass_utils import (
    AssExportError,
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
        self.assertEqual(len(merged), 1)
        self.assertTrue(all(f"Sign {index}" in merged[0].text for index in range(9)))

        unique_secondary_timing = content + "\n" + (
            "Dialogue: 0,0:00:03.00,0:00:04.00,Signs,,0,0,0,,Unique sign"
        )
        self.assertEqual(
            dominant_ass_dialogue_style(unique_secondary_timing),
            "English",
        )

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

    def test_dominant_style_does_not_count_exact_duplicate_events(self) -> None:
        duplicate = (
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
            "Repeated English dialogue"
        )

        self.assertIsNone(
            dominant_ass_dialogue_style("\n".join([duplicate] * 20))
        )

    def test_ass_dialogue_style_to_srt_blocks_filters_style_and_converts_text(self) -> None:
        content = "\n".join(
            [
                r"Dialogue: 0,0:00:01.23,0:00:03.45,English,,0,0,0,,{\i1}This is the first line.\NYou are here.",
                "Dialogue: 0,0:00:01.23,0:00:03.45,Signs,,0,0,0,,SHOP",
                "Dialogue: 0,0:00:07.00,0:00:08.00,english,,0,0,0,,This is the second line.",
            ]
            + [
                (
                    f"Dialogue: 0,0:01:{second:02d}.00,0:01:{second + 1:02d}.00,"
                    f"English,,0,0,0,,Additional English line {second}"
                )
                for second in range(1, 19)
            ]
        )

        blocks = ass_dialogue_style_to_srt_blocks(content, "English")

        self.assertEqual(len(blocks), 20)
        self.assertEqual(blocks[0].index, 1)
        self.assertEqual(blocks[0].timing, "00:00:01,230 --> 00:00:03,450")
        self.assertEqual(
            blocks[0].text,
            ["This is the first line.", "You are here.", "SHOP"],
        )
        self.assertEqual(blocks[1].index, 2)
        self.assertEqual(blocks[1].text, ["This is the second line."])

    def test_multistyle_dialogue_keeps_unique_timings_sorted_and_deduplicated(self) -> None:
        default_events = [
            (
                f"Dialogue: 0,0:00:{second:02d}.00,0:00:{second + 1:02d}.00,"
                f"Default,,0,0,0,,English dialogue {second}"
            )
            for second in range(1, 21)
        ]
        content = "\n".join(
            default_events
            + [
                "Dialogue: 0,0:00:00.50,0:00:00.90,italics,,0,0,0,,Whisper",
                "Dialogue: 0,0:00:05.00,0:00:06.00,sign_Arial,,0,0,0,,SHOP",
                "Dialogue: 0,0:00:05.00,0:00:06.00,overlap,,0,0,0,,English dialogue 5",
                "Comment: 0,0:00:30.00,0:00:31.00,Notes,,0,0,0,,Translator note",
            ]
        )

        style = dominant_ass_dialogue_style(content)
        self.assertEqual(style, "Default")

        blocks = ass_dialogue_style_to_srt_blocks(content, style)

        self.assertEqual(len(blocks), 21)
        self.assertEqual([block.index for block in blocks], list(range(1, 22)))
        self.assertEqual(
            blocks[0].timing,
            "00:00:00,500 --> 00:00:00,900",
        )
        self.assertEqual(blocks[0].text, ["Whisper"])
        shared = next(
            block
            for block in blocks
            if block.timing == "00:00:05,000 --> 00:00:06,000"
        )
        self.assertEqual(shared.text, ["English dialogue 5", "SHOP"])
        self.assertEqual(len({block.timing for block in blocks}), len(blocks))
        self.assertNotIn(
            "Translator note",
            [line for block in blocks for line in block.text],
        )

    def test_converter_rejects_requested_style_that_is_not_dominant(self) -> None:
        content = "\n".join(
            [
                (
                    f"Dialogue: 0,0:00:{second:02d}.00,0:00:{second + 1:02d}.00,"
                    f"Default,,0,0,0,,English dialogue {second}"
                )
                for second in range(1, 21)
            ]
            + [
                "Dialogue: 0,0:00:01.00,0:00:02.00,Signs,,0,0,0,,SHOP"
            ]
        )

        with self.assertRaisesRegex(
            AssExportError,
            "does not match its trusted dominant dialogue style",
        ):
            ass_dialogue_style_to_srt_blocks(content, "Signs")

    def test_converter_rejects_usable_dialogue_without_style(self) -> None:
        content = "\n".join(
            [
                (
                    f"Dialogue: 0,0:00:{second:02d}.00,0:00:{second + 1:02d}.00,"
                    f"Default,,0,0,0,,English dialogue {second}"
                )
                for second in range(1, 21)
            ]
            + [
                "Dialogue: 0,0:00:30.00,0:00:31.00,,,0,0,0,,Unstyled dialogue"
            ]
        )

        with self.assertRaisesRegex(
            AssExportError,
            "usable Dialogue text without a style",
        ):
            ass_dialogue_style_to_srt_blocks(content, "Default")

    def test_converter_excludes_vector_drawing_but_keeps_text_after_p0(self) -> None:
        content = "\n".join(
            [
                (
                    f"Dialogue: 0,0:00:{second:02d}.00,0:00:{second + 1:02d}.00,"
                    f"Default,,0,0,0,,English dialogue {second}"
                )
                for second in range(1, 21)
            ]
            + [
                r"Dialogue: 0,0:00:30.00,0:00:31.00,sign_shape,,0,0,0,,{\p1}m 0 0 l 100 0 100 100 0 100{\p0}",
                r"Dialogue: 0,0:00:31.00,0:00:32.00,sign_shape,,0,0,0,,{\p1}m 0 0 l 10 10{\p0}SHOP",
            ]
        )

        style = dominant_ass_dialogue_style(content)
        self.assertEqual(style, "Default")
        blocks = ass_dialogue_style_to_srt_blocks(content, style)

        self.assertEqual(len(blocks), 21)
        self.assertEqual(blocks[-1].text, ["SHOP"])
        flattened = [line for block in blocks for line in block.text]
        self.assertFalse(any("m 0 0 l" in line for line in flattened))

    def test_secondary_overlap_attaches_once_and_orphans_form_safe_cluster(self) -> None:
        content = "\n".join(
            [
                (
                    f"Dialogue: 0,0:00:{index * 2:02d}.00,"
                    f"0:00:{index * 2 + 1:02d}.00,Default,,0,0,0,,"
                    f"English dialogue {index}"
                )
                for index in range(1, 21)
            ]
            + [
                "Dialogue: 0,0:00:01.50,0:00:02.80,italics,,0,0,0,,Attach to first",
                "Dialogue: 0,0:00:03.80,0:00:06.90,overlap,,0,0,0,,Attach to maximum overlap",
                "Dialogue: 0,0:00:03.10,0:00:03.60,sign_Arial,,0,0,0,,Orphan one",
                "Dialogue: 0,0:00:03.50,0:00:03.90,DefaultTop,,0,0,0,,Orphan two",
            ]
        )

        blocks = ass_dialogue_style_to_srt_blocks(content, "Default")

        first = next(
            block
            for block in blocks
            if "Attach to first" in block.text
        )
        second = next(
            block
            for block in blocks
            if "Attach to maximum overlap" in block.text
        )
        orphan = next(
            block
            for block in blocks
            if block.timing == "00:00:03,100 --> 00:00:03,900"
        )
        self.assertEqual(first.text, ["English dialogue 1", "Attach to first"])
        self.assertEqual(
            second.text,
            ["English dialogue 2", "Attach to maximum overlap"],
        )
        self.assertEqual(orphan.text, ["Orphan one", "Orphan two"])

        def milliseconds(timestamp: str) -> int:
            hours, minutes, remainder = timestamp.split(":", 2)
            seconds, millis = remainder.split(",", 1)
            return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)

        timing_pairs = [block.timing.split(" --> ", 1) for block in blocks]
        self.assertTrue(
            all(
                milliseconds(current[0]) >= milliseconds(previous[1])
                for previous, current in zip(timing_pairs, timing_pairs[1:])
            )
        )
        flattened = [line for block in blocks for line in block.text]
        for text in (
            "Attach to first",
            "Attach to maximum overlap",
            "Orphan one",
            "Orphan two",
        ):
            self.assertEqual(flattened.count(text), 1)

    def test_converter_rejects_overlapping_dominant_skeleton(self) -> None:
        events = [
            (
                f"Dialogue: 0,0:00:{index * 2:02d}.00,"
                f"0:00:{index * 2 + 1:02d}.00,Default,,0,0,0,,"
                f"English dialogue {index}"
            )
            for index in range(1, 21)
        ]
        events[1] = (
            "Dialogue: 0,0:00:02.50,0:00:04.50,Default,,0,0,0,,"
            "Overlapping dominant dialogue"
        )

        with self.assertRaisesRegex(
            AssExportError,
            "dominant Dialogue cues overlap",
        ):
            ass_dialogue_style_to_srt_blocks("\n".join(events), "Default")

    def test_high_cps_orphan_expands_only_to_target_duration(self) -> None:
        content = "\n".join(
            [
                (
                    f"Dialogue: 0,0:00:{index * 2:02d}.00,"
                    f"0:00:{index * 2 + 1:02d}.00,Default,,0,0,0,,"
                    f"English dialogue {index}"
                )
                for index in range(1, 21)
            ]
            + [
                "Dialogue: 0,0:00:03.25,0:00:03.75,italics,,0,0,0,,"
                "ABC DEF GHI JKL MNO PQR"
            ]
        )

        blocks = ass_dialogue_style_to_srt_blocks(content, "Default")

        expanded = next(
            block
            for block in blocks
            if any("ABC DEF" in line for line in block.text)
        )
        self.assertEqual(
            expanded.timing,
            "00:00:03,125 --> 00:00:03,875",
        )

    def test_high_cps_orphan_fails_when_gap_cannot_reach_target(self) -> None:
        content = "\n".join(
            [
                (
                    f"Dialogue: 0,0:00:{index * 2:02d}.00,"
                    f"0:00:{index * 2 + 1:02d}.00,Default,,0,0,0,,"
                    f"English dialogue {index}"
                )
                for index in range(1, 21)
            ]
            + [
                "Dialogue: 0,0:00:03.40,0:00:03.60,italics,,0,0,0,,"
                + ("A" * 100)
            ]
        )

        with self.assertRaisesRegex(
            AssExportError,
            "cannot reach safe CPS within its gap",
        ):
            ass_dialogue_style_to_srt_blocks(content, "Default")

    def test_attached_secondary_expands_only_newly_high_cps_dominant_cue(self) -> None:
        events = [
            (
                f"Dialogue: 0,0:00:{index * 2:02d}.00,"
                f"0:00:{index * 2 + 1:02d}.00,Default,,0,0,0,,"
                f"English dialogue {index}"
            )
            for index in range(1, 21)
        ]
        events[0] = (
            "Dialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,"
            "AAAAAAAAAAAA"
        )
        events.append(
            "Dialogue: 0,0:00:02.00,0:00:03.00,italics,,0,0,0,,"
            "BBBBBBBBBBBBBBBBBBBBBBBB"
        )

        blocks = ass_dialogue_style_to_srt_blocks("\n".join(events), "Default")

        self.assertEqual(
            blocks[0].timing,
            "00:00:01,750 --> 00:00:03,250",
        )
        self.assertEqual(
            blocks[0].text,
            ["AAAAAAAAAAAA", "BBBBBBBBBBBBBBBBBBBBBBBB"],
        )
        self.assertEqual(
            blocks[1].timing,
            "00:00:04,000 --> 00:00:05,000",
        )


if __name__ == "__main__":
    unittest.main()
