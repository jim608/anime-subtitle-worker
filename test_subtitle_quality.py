from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from subtitle_quality import (
    add_translation_quality_events,
    analyze_subtitle_file,
    managed_quality_report_path,
    quality_report_path,
    write_quality_report,
)
from translation_quality import (
    TranslationQualityEventsError,
    read_translation_quality_events,
    read_translation_quality_events_strict,
    translation_quality_events_path,
    write_translation_quality_events,
)


class SubtitleQualityTest(unittest.TestCase):
    def test_watchable_bilingual_ass_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(
                path,
                [
                    ("0:00:01.00", "0:00:03.00", r"早安\N{\fs50}おはよう"),
                    ("0:00:03.20", "0:00:05.00", r"今天也要努力\N{\fs50}今日も頑張ろう"),
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertEqual(report.status, "watchable")
            self.assertFalse(report.has_failures)
            self.assertEqual(report.dialogues, 2)

    def test_hallucination_line_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI日本語.ja.ass"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", "字幕製作人 初音未來")])

            report = analyze_subtitle_file(path, _config(), role="japanese")

            self.assertEqual(report.status, "rerun")
            self.assertTrue(report.has_failures)
            self.assertIn("hallucination_text", [issue.code for issue in report.issues])

    def test_translated_primary_residual_kana_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", r"これはテストです\N{\fs50}これはテストです")])

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertEqual(report.status, "rerun")
            self.assertIn("residual_japanese_kana", [issue.code for issue in report.issues])

    def test_translated_short_kana_name_uses_same_policy_as_translator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", "營養素サブライ很重要")])

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertNotIn("residual_japanese_kana", [issue.code for issue in report.issues])

    def test_translated_prompt_leak_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(
                path,
                [
                    (
                        "0:00:01.00",
                        "0:00:03.00",
                        "請逐行翻譯下列字幕。每一行都必須輸出，格式必須是：原編號<TAB>中文字幕。 49. 正常台詞",
                    )
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertEqual(report.status, "rerun")
            self.assertIn("translation_prompt_leak", [issue.code for issue in report.issues])

    def test_translated_neighbor_repair_context_echo_fails_as_prompt_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(
                path,
                [
                    (
                        "0:00:01.00",
                        "0:00:03.00",
                        "問題行前後字幕參考（只供理解，禁止輸出參考內容）："
                        "前一句日文「ホドル」 仍然只翻譯使用者訊息中的單一字幕行。",
                    )
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertEqual(report.status, "rerun")
            self.assertIn("translation_prompt_leak", [issue.code for issue in report.issues])

    def test_translated_runaway_repetition_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", "西耶娜" * 40)])

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertEqual(report.status, "rerun")
            self.assertIn("translation_runaway_repetition", [issue.code for issue in report.issues])

    def test_long_line_warns_but_does_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", "這是一段很長很長但是還不到硬性失敗門檻的中文字幕用來提醒可讀性")])

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertEqual(report.status, "check")
            self.assertFalse(report.has_failures)
            self.assertIn("long_line", [issue.code for issue in report.issues])

    def test_hard_cps_short_duration_and_overlap_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(
                path,
                [
                    ("0:00:01.00", "0:00:01.10", "這段字幕快得完全無法閱讀"),
                    ("0:00:00.90", "0:00:02.00", "下一句與前一句重疊"),
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="translated")
            codes = {issue.code for issue in report.issues}

            self.assertEqual(report.status, "rerun")
            self.assertIn("too_short", codes)
            self.assertIn("cps_too_high", codes)
            self.assertIn("timing_overlap", codes)

    def test_exact_hard_cps_boundary_is_not_failed_by_float_rounding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI原語言.en.ass"
            _write_ass(
                path,
                [("0:13:36.66", "0:13:37.46", "12345678901234567890")],
            )

            report = analyze_subtitle_file(path, _config(), role="unknown")

            self.assertFalse(report.has_failures)
            self.assertNotIn("cps_too_high", [issue.code for issue in report.issues])

    def test_simplified_remnant_and_glossary_inconsistency_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(
                path,
                [
                    (
                        "0:00:01.00",
                        "0:00:03.00",
                        r"这个名字譯錯了\N阿良々木正在說話",
                    )
                ],
            )
            config = _config()
            config.translation_glossary = {"阿良々木": "阿良良木"}

            report = analyze_subtitle_file(path, config, role="translated")
            codes = {issue.code for issue in report.issues}

            self.assertIn("simplified_chinese_remnant", codes)
            self.assertIn("glossary_term_inconsistent", codes)

    def test_simplified_chinese_role_keeps_translation_checks_but_not_tw_script_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI简日双语.zh-CN.ass"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", "这个学生正在说话")])

            report = analyze_subtitle_file(
                path,
                _config(),
                role="translated_zh_cn",
            )

            self.assertNotIn(
                "simplified_chinese_remnant",
                [issue.code for issue in report.issues],
            )

    def test_shared_character_he_does_not_trigger_simplified_remnant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.zh-TW.ass"
            _write_ass(
                path,
                [
                    (
                        "0:00:01.00",
                        "0:00:04.00",
                        "\u4ed6\u5011\u5408\u529b\u5b8c\u6210\u5408\u683c\u540d\u984d",
                    )
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="translated_zh_tw")

            self.assertNotIn(
                "simplified_chinese_remnant",
                [issue.code for issue in report.issues],
            )

    def test_five_identical_consecutive_cues_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            _write_ass(
                path,
                [
                    (f"0:00:{index:02d}.00", f"0:00:{index + 1:02d}.00", "完全相同")
                    for index in range(1, 6)
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="translated")

            self.assertIn(
                "repeated_consecutive_cues",
                [issue.code for issue in report.issues],
            )

    def test_late_first_japanese_line_warns_about_missing_opening(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI.ja.ass"
            _write_ass(path, [("0:00:45.00", "0:00:47.00", "first detected line")])

            report = analyze_subtitle_file(path, _config(), role="japanese")

            issue = next(issue for issue in report.issues if issue.code == "leading_gap")
            self.assertEqual(report.status, "check")
            self.assertEqual(issue.indexes, [1])
            self.assertEqual(report.largest_gap, 45.0)

    def test_write_quality_report_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI原語言.en.ass"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", "Hello there.")])
            report = analyze_subtitle_file(path, _config(), role="source")

            output = write_quality_report(report)

            self.assertEqual(output, quality_report_path(path))
            self.assertTrue(output.exists())
            self.assertIn('"status": "watchable"', output.read_text(encoding="utf-8"))

    def test_write_quality_report_to_managed_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "anime" / "Episode.AI繁日雙語.zh-TW.ass"
            path.parent.mkdir()
            _write_ass(path, [("0:00:01.00", "0:00:03.00", "中文字幕")])
            report = analyze_subtitle_file(path, _config(), role="translated")
            destination = managed_quality_report_path(path, root / "work")

            output = write_quality_report(report, destination)

            self.assertEqual(output, destination)
            self.assertTrue(output.is_file())
            self.assertFalse(quality_report_path(path).exists())

    def test_safe_omission_event_blocks_publication_with_target_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            srt = Path(temp_dir) / "Episode.zh-CN.srt"
            _write_ass(path, [("0:00:01.00", "0:00:03.00", r"……\N{\fs50}明日まで駆け出す")])
            srt.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n測試字幕\n",
                encoding="utf-8",
            )
            write_translation_quality_events(
                srt,
                [
                    {
                        "code": "translation_safe_omission",
                        "severity": "fail",
                        "index": 337,
                        "source": "明日まで駆け出す",
                        "output": "SRT編號<TAB>字幕文字",
                        "reason": "Unexpected translated index: 1",
                    }
                ],
            )

            report = add_translation_quality_events(
                analyze_subtitle_file(path, _config(), role="translated"),
                read_translation_quality_events(srt),
            )

            issue = next(issue for issue in report.issues if issue.code == "translation_safe_omission")
            self.assertEqual(report.status, "rerun")
            self.assertEqual(issue.severity, "fail")
            self.assertEqual(issue.indexes, [337])
            self.assertIn("#337", issue.samples[0])

    def test_fragmented_asr_prompt_echo_fails_japanese_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI日本語.ja.ass"
            _write_ass(
                path,
                [
                    ("0:00:01.00", "0:00:02.00", "日本ニメ"),
                    ("0:00:02.10", "0:00:03.00", "ング"),
                    ("0:00:03.10", "0:00:04.00", "挿入"),
                    ("0:00:04.10", "0:00:05.00", "歌。"),
                    ("0:00:05.10", "0:00:07.00", "今日はいい天気だ。"),
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="japanese")

            issue = next(issue for issue in report.issues if issue.code == "asr_prompt_echo")
            self.assertEqual(report.status, "rerun")
            self.assertEqual(issue.indexes, [1, 2, 3, 4])

    def test_fragmented_subscription_hallucination_fails_japanese_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI.ja.ass"
            _write_ass(
                path,
                [
                    ("0:00:01.00", "0:00:02.00", "\u5b57\u5e55\u30aa\u30f3\u306b\u3057\u3066"),
                    ("0:00:02.10", "0:00:03.00", "\u3054\u8996\u8074"),
                    ("0:00:03.10", "0:00:04.00", "\u3042\u308a\u304c\u3068\u3046"),
                    ("0:00:04.10", "0:00:05.00", "\u30c1\u30e3\u30f3\u30cd\u30eb\u767b\u9332\u3092"),
                    ("0:00:05.10", "0:00:07.00", "\u305d\u308c\u3067\u306f\u59cb\u3081\u307e\u3057\u3087\u3046"),
                ],
            )

            report = analyze_subtitle_file(path, _config(), role="japanese")

            issue = next(issue for issue in report.issues if issue.code == "hallucination_text")
            self.assertEqual(report.status, "rerun")
            self.assertEqual(issue.indexes, [1, 2, 3, 4])

    def test_strict_translation_event_reader_rejects_legacy_unbound_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            translated = Path(temp_dir) / "episode.zh-CN.srt"
            translated.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nblocked\n",
                encoding="utf-8",
            )
            translation_quality_events_path(translated).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "events": [
                            {
                                "code": "translation_safe_omission",
                                "severity": "fail",
                                "index": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(TranslationQualityEventsError):
                read_translation_quality_events_strict(translated)


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        subtitle_quality_max_duration_seconds=5.5,
        subtitle_quality_max_primary_chars=24,
        subtitle_quality_hard_max_primary_chars=64,
        subtitle_quality_max_gap_seconds=45.0,
        subtitle_quality_max_leading_gap_seconds=30.0,
        subtitle_quality_warn_cps=17.0,
        subtitle_quality_fail_cps=25.0,
        subtitle_quality_min_duration_seconds=0.35,
        subtitle_quality_hard_min_duration_seconds=0.12,
        subtitle_quality_max_overlap_seconds=0.10,
        translation_glossary={},
        whisper_hallucination_phrases=[],
    )


def _write_ass(path: Path, dialogues: list[tuple[str, str, str]]) -> None:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, text in dialogues:
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
