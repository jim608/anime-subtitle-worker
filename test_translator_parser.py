from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from srt_utils import SrtBlock, read_srt
from translation_quality import (
    is_repetitive_kana_source,
    read_translation_quality_events,
    read_translation_quality_events_strict,
    translation_quality_events_path,
)
from translator import (
    ASR_REVIEW_TOKEN,
    AsrReviewError,
    SubtitleTranslator,
    TranslationError,
    TranslationTimeoutError,
    _apply_glossary_to_blocks,
    _build_line_translation_prompt,
    _build_line_translation_system_prompt,
    _contextual_non_japanese_fragment_fallback,
    _initial_translation_model_index,
    _model_ids_from_response,
    _parse_translated_lines,
    _sanitize_residual_kana_candidate,
    _select_available_translator_model,
    _strip_known_prompt_echo,
    _validate_translation_output_size,
    _validate_translated_text,
)


class TranslatorParserTest(unittest.TestCase):
    def test_non_japanese_source_starts_on_multilingual_fallback(self) -> None:
        models = ("sakura", "qwen")

        self.assertEqual(_initial_translation_model_index(models, "en"), 1)
        self.assertEqual(_initial_translation_model_index(models, "en-US"), 1)
        self.assertEqual(_initial_translation_model_index(models, "ja"), 0)

    def test_contextual_non_japanese_article_fallback_is_bounded(self) -> None:
        self.assertEqual(
            _contextual_non_japanese_fragment_fallback("The", "en"),
            "該",
        )
        self.assertEqual(
            _contextual_non_japanese_fragment_fallback("an", "en-US"),
            "一個",
        )
        self.assertIsNone(
            _contextual_non_japanese_fragment_fallback("research", "en")
        )
        self.assertIsNone(_contextual_non_japanese_fragment_fallback("The", "ja"))

    def test_repetitive_kana_source_classifier_is_narrow(self) -> None:
        self.assertTrue(
            is_repetitive_kana_source(
                "ゆるりゆるりゆるりゆゆゆるりゆゆるゆりゆゆ"
            )
        )
        self.assertFalse(is_repetitive_kana_source("今日はいい天気です"))
        self.assertFalse(is_repetitive_kana_source("ありがとう"))
        self.assertFalse(is_repetitive_kana_source("の"))

    def test_safe_omission_event_is_durable_before_srt_replace(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            batch_size=10,
            translation_context_enabled=False,
            translation_glossary={},
        )
        translator.logger = logging.getLogger("test.translator.event-first")
        translator._progress_callback = None
        source = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"])
        ]

        def unsafe_batch(batch, _index, _context):
            translator._quality_events.append(
                {
                    "code": "translation_safe_omission",
                    "severity": "fail",
                    "index": 1,
                    "source": "source one",
                    "output": "unsafe output",
                    "reason": "malformed output",
                }
            )
            return [
                SrtBlock(batch[0].index, batch[0].timing, ["unsafe output"])
            ]

        translator._translate_batch = unsafe_batch
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "translated.srt"

            def interrupt_srt_replace(_output, _blocks):
                payload = json.loads(
                    translation_quality_events_path(output).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    payload["events"][0]["code"],
                    "translation_safe_omission",
                )
                raise OSError("interrupted SRT replace")

            with (
                patch("translator.write_srt", side_effect=interrupt_srt_replace),
                self.assertRaisesRegex(OSError, "interrupted SRT replace"),
            ):
                translator.translate_blocks(
                    source,
                    Path(temp_dir) / "source.srt",
                    output,
                )

            self.assertFalse(output.exists())
            self.assertTrue(translation_quality_events_path(output).exists())

    def test_sidecar_failure_with_locked_output_leaves_invalid_cache_marker(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            batch_size=10,
            translation_context_enabled=False,
            translation_glossary={},
        )
        translator.logger = logging.getLogger("test.translator.locked-output")
        translator._progress_callback = None
        source = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"])
        ]
        def unsafe_batch(batch, _index, _context):
            translator._quality_events.append(
                {
                    "code": "translation_safe_omission",
                    "severity": "fail",
                    "index": 1,
                    "source": "source one",
                    "output": "unsafe cached output",
                    "reason": "malformed output",
                }
            )
            return [
                SrtBlock(
                    batch[0].index,
                    batch[0].timing,
                    ["unsafe cached output"],
                )
            ]

        translator._translate_batch = unsafe_batch

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "translated.srt"
            output.write_text("preexisting cache", encoding="utf-8")
            real_unlink = Path.unlink

            def fail_only_output_unlink(path, *args, **kwargs):
                if Path(path) == output:
                    raise OSError("output is locked")
                return real_unlink(path, *args, **kwargs)

            with (
                patch(
                    "translator.write_translation_quality_events",
                    side_effect=OSError("original sidecar failure"),
                ),
                patch.object(Path, "unlink", new=fail_only_output_unlink),
                self.assertRaisesRegex(OSError, "original sidecar failure"),
            ):
                translator.translate_blocks(
                    source,
                    Path(temp_dir) / "source.srt",
                    output,
                )

            self.assertTrue(output.exists())
            events = read_translation_quality_events_strict(output)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["code"], "translation_safe_omission")
            self.assertIn("commit guard write failed", str(events[0]["reason"]))

    def test_translate_safe_omission_sidecar_failure_removes_output_cache(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            batch_size=10,
            translation_context_enabled=False,
            translation_glossary={},
        )
        translator.logger = logging.getLogger("test.translator.sidecar-write")
        translator._progress_callback = None
        source = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"])
        ]

        def translate_batch(batch, _batch_index, _context):
            translator._quality_events.append(
                {
                    "code": "translation_safe_omission",
                    "severity": "fail",
                    "index": 1,
                    "source": "source one",
                    "output": "bad",
                    "reason": "malformed output",
                }
            )
            return [SrtBlock(batch[0].index, batch[0].timing, ["bad"])]

        translator._translate_batch = translate_batch
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "translated.srt"
            with (
                patch(
                    "translator.write_translation_quality_events",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                translator.translate_blocks(
                    source,
                    Path(temp_dir) / "source.srt",
                    output,
                )

            self.assertFalse(output.exists())
            self.assertFalse(translation_quality_events_path(output).exists())

    def test_targeted_safe_omission_sidecar_failure_removes_output_cache(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(translation_glossary={})
        translator.logger = logging.getLogger("test.translator.targeted-sidecar-write")
        translator._progress_callback = None
        source = [
            SrtBlock(7, "00:00:01,000 --> 00:00:02,000", ["source seven"])
        ]

        def translate_batch(batch, _batch_index, _context):
            translator._quality_events.append(
                {
                    "code": "translation_safe_omission",
                    "severity": "fail",
                    "index": 7,
                    "source": "source seven",
                    "output": "bad",
                    "reason": "malformed output",
                }
            )
            return [SrtBlock(batch[0].index, batch[0].timing, ["bad"])]

        translator._translate_batch = translate_batch
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "repair.srt"
            with (
                patch(
                    "translator.write_translation_quality_events",
                    side_effect=OSError("read only"),
                ),
                self.assertRaisesRegex(OSError, "read only"),
            ):
                translator.retranslate_problem_blocks(
                    source,
                    Path(temp_dir) / "source.srt",
                    output,
                    series_glossary={},
                )

            self.assertFalse(output.exists())
            self.assertFalse(translation_quality_events_path(output).exists())

    def test_problem_line_retry_uses_independent_context_free_batches(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(translation_glossary={})
        translator.logger = logging.getLogger("test.translator.problem-lines")
        translator._progress_callback = None
        calls: list[tuple[list[int], str, str]] = []

        def translate_batch(batch: list[SrtBlock], batch_index: str, context: str):
            calls.append(([block.index for block in batch], batch_index, context))
            block = batch[0]
            return [SrtBlock(block.index, block.timing, [f"fixed {block.index}"])]

        translator._translate_batch = translate_batch
        source = [
            SrtBlock(7, "00:00:01,000 --> 00:00:02,000", ["source seven"]),
            SrtBlock(11, "00:00:03,000 --> 00:00:04,000", ["source eleven"]),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "repair.srt"
            translator.retranslate_problem_blocks(
                source,
                Path(temp_dir) / "source.srt",
                output,
                series_glossary={},
            )

            self.assertEqual(
                calls,
                [([7], "repair-7", ""), ([11], "repair-11", "")],
            )
            self.assertEqual(
                [" ".join(block.text) for block in read_srt(output)],
                ["fixed 7", "fixed 11"],
            )

    def test_problem_line_retry_threads_non_japanese_source_language(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(translation_glossary={})
        translator.logger = logging.getLogger("test.translator.problem-lines.en")
        translator._progress_callback = None
        seen: list[str] = []

        def translate_batch(
            batch: list[SrtBlock],
            _batch_index: str,
            _context: str,
            *,
            source_language: str,
        ) -> list[SrtBlock]:
            seen.append(source_language)
            block = batch[0]
            return [SrtBlock(block.index, block.timing, ["translated Chinese"])]

        translator._translate_batch = translate_batch
        source = [SrtBlock(7, "00:00:01,000 --> 00:00:02,000", ["English source"])]
        with tempfile.TemporaryDirectory() as temp_dir:
            translator.retranslate_problem_blocks(
                source,
                Path(temp_dir) / "source.en.srt",
                Path(temp_dir) / "repair.srt",
                series_glossary={},
                source_language="en",
            )

        self.assertEqual(seen, ["en"])

    def test_problem_line_retry_enforces_readability_display_limit(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(translation_glossary={})
        translator.logger = logging.getLogger(
            "test.translator.problem-lines.readability"
        )
        translator._progress_callback = None
        contexts: list[str] = []

        def translate_batch(
            batch: list[SrtBlock],
            _batch_index: str,
            context: str,
            *,
            source_language: str,
        ) -> list[SrtBlock]:
            self.assertEqual(source_language, "en")
            contexts.append(context)
            output = "這個翻譯明顯超過限制" if len(contexts) == 1 else "短譯"
            return [SrtBlock(batch[0].index, batch[0].timing, [output])]

        translator._translate_batch = translate_batch
        source = [
            SrtBlock(7, "00:00:01,000 --> 00:00:02,000", ["English source"])
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "repair.srt"
            translator.retranslate_problem_blocks(
                source,
                Path(temp_dir) / "source.en.srt",
                output,
                series_glossary={},
                source_language="en",
                max_display_chars_by_index={7: 4},
            )

            self.assertEqual(read_srt(output)[0].text, ["短譯"])

        self.assertEqual(len(contexts), 2)
        self.assertIn("不得超過 4 個非空白字元", contexts[0])

    def test_problem_line_retry_receives_bounded_neighbor_context(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(translation_glossary={})
        translator.logger = logging.getLogger(
            "test.translator.problem-line-neighbors"
        )
        translator._progress_callback = None
        calls: list[str] = []
        source = [
            SrtBlock(209, "00:10:20,000 --> 00:10:22,000", ["前の台詞"]),
            SrtBlock(210, "00:10:22,000 --> 00:10:24,000", ["悩みがある"]),
            SrtBlock(211, "00:10:24,000 --> 00:10:26,000", ["の"]),
            SrtBlock(212, "00:10:26,000 --> 00:10:28,000", ["次の台詞"]),
        ]
        translated = [
            SrtBlock(209, source[0].timing, ["前一句"]),
            SrtBlock(210, source[1].timing, ["是不是有煩惱"]),
            SrtBlock(211, source[2].timing, ["……"]),
            SrtBlock(212, source[3].timing, ["下一句"]),
        ]

        def translate_batch(
            batch: list[SrtBlock],
            _batch_index: str,
            context: str,
        ) -> list[SrtBlock]:
            calls.append(context)
            return [SrtBlock(batch[0].index, batch[0].timing, ["嗎？"])]

        translator._translate_batch = translate_batch
        translator.set_targeted_repair_context(
            source,
            translated,
            {211},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "repair.srt"
            translator.retranslate_problem_blocks(
                [source[2]],
                Path(temp_dir) / "source.srt",
                output,
                series_glossary={},
            )

        self.assertEqual(len(calls), 1)
        self.assertIn("悩みがある", calls[0])
        self.assertIn("是不是有煩惱", calls[0])
        self.assertIn("次の台詞", calls[0])
        self.assertNotIn("……", calls[0])

    def test_problem_line_retry_keeps_only_series_names_for_meaningful_line(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(translation_glossary={})
        translator.logger = logging.getLogger(
            "test.translator.problem-line-series-names"
        )
        translator._progress_callback = None
        calls: list[str] = []
        source = [
            SrtBlock(168, "00:10:28,000 --> 00:10:31,000", ["戦国なでこ"]),
            SrtBlock(169, "00:10:35,000 --> 00:10:37,000", ["あららぎは知っているのか?"]),
            SrtBlock(170, "00:10:38,000 --> 00:10:40,000", ["お前が会っていることを"]),
        ]
        translated = [
            SrtBlock(168, source[0].timing, ["戰國撫子"]),
            SrtBlock(169, source[1].timing, ["……"]),
            # An unsafe neighboring translation must not reinforce residual kana.
            SrtBlock(170, source[2].timing, ["あららぎ知道了"]),
        ]

        def translate_batch(
            batch: list[SrtBlock],
            _batch_index: str,
            context: str,
        ) -> list[SrtBlock]:
            calls.append(context)
            return [SrtBlock(batch[0].index, batch[0].timing, ["阿良良木知道嗎？"])]

        translator._translate_batch = translate_batch
        translator.set_targeted_repair_context(
            source,
            translated,
            {169},
            series_context=(
                "Series metadata context:\n"
                "Titles: Monogatari Series: Second Season / 物語系列\n"
                "Characters: Koyomi Araragi, 阿良々木暦, Nadeko Sengoku, 千石撫子\n"
                "Synopsis: this long plot summary must not enter a targeted repair prompt"
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            translator.retranslate_problem_blocks(
                [source[1]],
                Path(temp_dir) / "source.srt",
                Path(temp_dir) / "repair.srt",
                series_glossary={},
            )

        self.assertEqual(len(calls), 1)
        self.assertIn("Koyomi Araragi, 阿良々木暦", calls[0])
        self.assertNotIn("plot summary", calls[0])
        self.assertNotIn("あららぎ知道了", calls[0])

    def test_standalone_kana_repair_uses_neighbor_grammar_prompt(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.standalone-kana-context"
        )
        calls: list[tuple[str, str]] = []

        def request(source: str, system_prompt: str) -> str:
            calls.append((source, system_prompt))
            return "211\t吗？"

        translator._request_translation = request
        block = SrtBlock(
            211,
            "00:10:24,420 --> 00:10:26,040",
            ["の"],
        )
        context = (
            "前一句日文「さくらこ何か悩みでもあるんです」；"
            "下一句日文「おめーだよ」"
        )

        repaired = translator._repair_single_kana_residual(
            block,
            "211\tの",
            "repair-211.211",
            context,
        )

        self.assertEqual(repaired[0].text, ["吗？"])
        self.assertEqual(len(calls), 1)
        self.assertIn("助詞、語尾或疑問語氣", calls[0][1])
        self.assertIn("さくらこ何か悩みでもあるんです", calls[0][1])

    def test_standalone_no_uses_context_safe_fallback_when_model_echoes_source(
        self,
    ) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.standalone-no-context-fallback"
        )
        translator._progress_callback = None
        translator._quality_events = []
        translator._request_translation = lambda _source, _prompt: "211\tの"
        block = SrtBlock(
            211,
            "00:10:24,420 --> 00:10:26,040",
            ["の"],
        )
        context = (
            "前一句日文「さくらこ何か悩みでもあるんです」；"
            "前一句中文「櫻子是不是有什麼煩惱」；"
            "下一句日文「おめーだよ」"
        )

        repaired = translator._repair_single_kana_residual(
            block,
            "211\tの",
            "repair-211.211",
            context,
        )

        self.assertEqual(repaired[0].text, ["吗？"])
        self.assertEqual(translator.translation_quality_events, [])

    def test_symbol_only_prolongation_uses_punctuation_fallback(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.symbol-only-prolongation"
        )
        translator._progress_callback = None
        translator._quality_events = []
        translator._request_translation = lambda _source, _prompt: "385\tー！"
        block = SrtBlock(
            385,
            "00:20:44,000 --> 00:20:45,000",
            ["ー!"],
        )

        repaired = translator._repair_single_kana_residual(
            block,
            "385\tー！",
            "repair-385.385",
            "",
        )

        self.assertEqual(repaired[0].text, ["！"])
        self.assertEqual(translator.translation_quality_events, [])

    def test_standalone_no_without_deterministic_context_remains_fail_closed(
        self,
    ) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.standalone-no-ambiguous-context"
        )
        translator._progress_callback = None
        translator._quality_events = []
        translator._request_translation = lambda _source, _prompt: "211\tの"
        block = SrtBlock(
            211,
            "00:10:24,420 --> 00:10:26,040",
            ["の"],
        )

        repaired = translator._repair_single_kana_residual(
            block,
            "211\tの",
            "repair-211.211",
            "前一句日文「これは私」；下一句日文「本です」",
        )

        self.assertEqual(repaired[0].text, ["……"])
        self.assertEqual(
            [event["index"] for event in translator.translation_quality_events],
            [211],
        )

    def test_residual_kana_repair_uses_neighbor_wordplay_context(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.kana-wordplay-context"
        )
        calls: list[tuple[str, str]] = []

        def request(source: str, system_prompt: str) -> str:
            calls.append((source, system_prompt))
            return "363\t柴色？"

        translator._request_translation = request
        source = [
            SrtBlock(362, "00:19:28,000 --> 00:19:29,000", ["シャン!"]),
            SrtBlock(363, "00:19:29,000 --> 00:19:32,000", ["しばいろ?"]),
            SrtBlock(
                364,
                "00:19:32,000 --> 00:19:35,000",
                ["紫だよ!む!ら!さ!き!"],
            ),
        ]
        malformed = [
            SrtBlock(362, source[0].timing, ["鏘！"]),
            SrtBlock(363, source[1].timing, ["しばいろ?"]),
            SrtBlock(364, source[2].timing, ["是紫色！紫！色！"]),
        ]

        repaired = translator._repair_residual_kana_blocks(
            malformed,
            source,
            "61",
        )

        self.assertEqual(repaired[1].text, ["柴色？"])
        self.assertEqual(len(calls), 1)
        self.assertIn("紫だよ", calls[0][1])
        self.assertIn("是紫色", calls[0][1])
        self.assertIn("只供理解", calls[0][1])

    def test_ignores_extra_model_preamble_when_all_indexes_present(self) -> None:
        original = [
            SrtBlock(10, "00:00:01,000 --> 00:00:02,000", ["source 1"]),
            SrtBlock(11, "00:00:02,000 --> 00:00:03,000", ["source 2"]),
        ]

        translated = _parse_translated_lines(
            "translation follows\n10\ttranslated 1\n11\ttranslated 2",
            original,
        )

        self.assertEqual([block.text[0] for block in translated], ["translated 1", "translated 2"])

    def test_remaps_complete_model_local_indexes_to_original_srt_indexes(self) -> None:
        original = [
            SrtBlock(259, "00:00:01,000 --> 00:00:02,000", ["source 1"]),
            SrtBlock(260, "00:00:02,000 --> 00:00:03,000", ["source 2"]),
            SrtBlock(261, "00:00:03,000 --> 00:00:04,000", ["source 3"]),
        ]

        translated = _parse_translated_lines(
            "translation follows\n1\ttranslated 1\n2\ttranslated 2\n3\ttranslated 3",
            original,
        )

        self.assertEqual([block.index for block in translated], [259, 260, 261])
        self.assertEqual([block.text[0] for block in translated], ["translated 1", "translated 2", "translated 3"])

    def test_accepts_period_numbering_and_remaps_model_local_indexes(self) -> None:
        original = [
            SrtBlock(19, "00:00:01,000 --> 00:00:02,000", ["source 1"]),
            SrtBlock(20, "00:00:02,000 --> 00:00:03,000", ["source 2"]),
            SrtBlock(21, "00:00:03,000 --> 00:00:04,000", ["source 3"]),
        ]

        global_numbered = _parse_translated_lines(
            "19. translated 1\n20．translated 2\n21、translated 3",
            original,
        )
        local_numbered = _parse_translated_lines(
            "1. translated 1\n2．translated 2\n3、translated 3",
            original,
        )

        self.assertEqual([block.text[0] for block in global_numbered], ["translated 1", "translated 2", "translated 3"])
        self.assertEqual(local_numbered, global_numbered)

    def test_does_not_remap_incomplete_model_local_indexes(self) -> None:
        original = [
            SrtBlock(259, "00:00:01,000 --> 00:00:02,000", ["source 1"]),
            SrtBlock(260, "00:00:02,000 --> 00:00:03,000", ["source 2"]),
            SrtBlock(261, "00:00:03,000 --> 00:00:04,000", ["source 3"]),
        ]

        with self.assertRaisesRegex(ValueError, "Unexpected translated index"):
            _parse_translated_lines("1\ttranslated 1\n2\ttranslated 2", original)

    def test_accepts_expected_indexes_when_model_omits_separator(self) -> None:
        original = [
            SrtBlock(55, "00:00:01,000 --> 00:00:02,000", ["source 1"]),
            SrtBlock(56, "00:00:02,000 --> 00:00:03,000", ["source 2"]),
        ]

        translated = _parse_translated_lines("55第一句\n56第二句", original)

        self.assertEqual([block.text[0] for block in translated], ["第一句", "第二句"])

    def test_accepts_complete_ordered_unnumbered_batch(self) -> None:
        original = [
            SrtBlock(94, "00:00:01,000 --> 00:00:02,000", ["source 1"]),
            SrtBlock(95, "00:00:02,000 --> 00:00:03,000", ["source 2"]),
            SrtBlock(96, "00:00:03,000 --> 00:00:04,000", ["source 3"]),
        ]

        translated = _parse_translated_lines(
            "請逐行翻譯下列字幕。\n每一行都必須輸出。\n第一句\n第二句\n第三句",
            original,
        )

        self.assertEqual([block.text[0] for block in translated], ["第一句", "第二句", "第三句"])

    def test_accepts_single_line_plain_translation_for_single_block(self) -> None:
        original = [SrtBlock(5, "00:00:01,000 --> 00:00:02,000", ["source"])]

        translated = _parse_translated_lines("translated text", original)

        self.assertEqual(translated, [SrtBlock(5, "00:00:01,000 --> 00:00:02,000", ["translated text"])])

    def test_corrects_single_block_off_by_one_model_index(self) -> None:
        original = [SrtBlock(367, "00:26:06,040 --> 00:26:07,380", ["楽しかった"])]

        translated = _parse_translated_lines("368\t開心極了", original)

        self.assertEqual(
            translated,
            [SrtBlock(367, "00:26:06,040 --> 00:26:07,380", ["開心極了"])],
        )

    def test_does_not_remap_large_single_block_index_error(self) -> None:
        original = [SrtBlock(367, "00:26:06,040 --> 00:26:07,380", ["楽しかった"])]

        with self.assertRaisesRegex(ValueError, "Unexpected translated index: 999"):
            _parse_translated_lines("999\t開心極了", original)

    def test_does_not_remap_single_index_when_extra_output_is_present(self) -> None:
        original = [SrtBlock(367, "00:26:06,040 --> 00:26:07,380", ["楽しかった"])]

        with self.assertRaisesRegex(ValueError, "Unexpected translated index: 368"):
            _parse_translated_lines("translation follows\n368\t開心極了", original)

    def test_accepts_multi_line_plain_translation_for_single_block_repair(self) -> None:
        original = [SrtBlock(5, "00:00:01,000 --> 00:00:02,000", ["source"])]

        translated = _parse_translated_lines("translated text\nsecond line", original)

        self.assertEqual(
            translated,
            [SrtBlock(5, "00:00:01,000 --> 00:00:02,000", ["translated text second line"])],
        )

    def test_rejects_missing_indexes(self) -> None:
        original = [
            SrtBlock(10, "00:00:01,000 --> 00:00:02,000", ["source 1"]),
            SrtBlock(11, "00:00:02,000 --> 00:00:03,000", ["source 2"]),
        ]

        with self.assertRaises(ValueError):
            _parse_translated_lines("10\ttranslated 1", original)

    def test_rejects_residual_kana_when_enabled(self) -> None:
        config = type("Config", (), {"translation_reject_residual_kana": True})()

        with self.assertRaises(ValueError):
            _validate_translated_text(
                [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["\u3053\u308c\u306f\u672a\u7ffb\u8b6f\u3067\u3059"])],
                config,
            )

    def test_rejects_prompt_leak_even_when_kana_check_is_disabled(self) -> None:
        config = type("Config", (), {"translation_reject_residual_kana": False})()

        with self.assertRaisesRegex(ValueError, "model-output pollution"):
            _validate_translated_text(
                [
                    SrtBlock(
                        49,
                        "00:00:01,000 --> 00:00:02,000",
                        ["請逐行翻譯下列字幕。每一行都必須輸出，格式必須是：原編號<TAB>中文字幕。"],
                    )
                ],
                config,
            )

    def test_rejects_neighbor_repair_context_echo(self) -> None:
        config = type("Config", (), {"translation_reject_residual_kana": False})()

        with self.assertRaisesRegex(ValueError, "model-output pollution"):
            _validate_translated_text(
                [
                    SrtBlock(
                        366,
                        "00:22:23,160 --> 00:22:25,120",
                        [
                            "問題行前後字幕參考（只供理解，禁止輸出參考內容）："
                            "前一句日文「ホドル」 仍然只翻譯使用者訊息中的單一字幕行。"
                        ],
                    )
                ],
                config,
            )

    def test_rejects_structured_neighbor_context_echo_variant(self) -> None:
        config = type("Config", (), {"translation_reject_residual_kana": False})()

        with self.assertRaisesRegex(ValueError, "model-output pollution"):
            _validate_translated_text(
                [
                    SrtBlock(
                        14,
                        "00:00:01,000 --> 00:00:02,000",
                        ["问题行前一句日文「前文」；问题行中文「錯誤」；下一句日文「後文」"],
                    )
                ],
                config,
            )

    def test_asr_review_token_is_never_accepted_as_translation(self) -> None:
        config = type("Config", (), {"translation_reject_residual_kana": True})()

        with self.assertRaises(AsrReviewError):
            _validate_translated_text(
                [SrtBlock(7, "00:00:01,000 --> 00:00:02,000", [ASR_REVIEW_TOKEN])],
                config,
            )

    def test_strips_complete_leading_prompt_echo_when_translation_remains(self) -> None:
        leaked = (
            "请逐行翻译下列字幕。 每一行都必须输出，格式必须是："
            "原编号<TAB>中文字幕。 49.新生早瀨優子"
        )

        self.assertEqual(_strip_known_prompt_echo(leaked, 49), "新生早瀨優子")
        translated = _parse_translated_lines(
            f"49\t{leaked}",
            [SrtBlock(49, "00:00:01,000 --> 00:00:02,000", ["早瀬優子"])],
        )
        self.assertEqual(translated[0].text, ["新生早瀨優子"])

    def test_does_not_strip_prompt_echo_without_a_translation_remainder(self) -> None:
        leaked = "請逐行翻譯下列字幕。每一行都必須輸出，格式必須是：原編號<TAB>中文字幕。"

        self.assertEqual(_strip_known_prompt_echo(leaked, 49), leaked)

    def test_allows_short_kana_name_inside_chinese_translation(self) -> None:
        config = type("Config", (), {"translation_reject_residual_kana": True})()

        _validate_translated_text(
            [
                SrtBlock(
                    99,
                    "00:00:01,000 --> 00:00:02,000",
                    ["\u6211\u53eb\u4e94\u8272\u3057\u304a\u308a\uff0c\u662f\u5e0c\u671b\u64d4\u4efb\u52a9\u624b\u7684\u4eba"],
                )
            ],
            config,
        )

    def test_applies_glossary_before_residual_kana_validation(self) -> None:
        blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source name"])]

        translated = _apply_glossary_to_blocks(blocks, {"source": "target"})

        self.assertEqual(translated[0].text, ["target name"])

    def test_sanitizes_residual_kana_when_chinese_translation_remains(self) -> None:
        self.assertEqual(_sanitize_residual_kana_candidate("254\t\u30fc\u30ad\u304c\u622a\u6b62\u65e5\u671f?"), "\u622a\u6b62\u65e5\u671f")

    def test_residual_kana_sanitizer_rejects_echoed_repair_prompt(self) -> None:
        leaked = (
            "82\t原始字幕：我已心死 上次輸出：我已心死 "
            "請只輸出修正後的一行中文字幕，不要包含日文假名或片假名。"
        )

        self.assertIsNone(_sanitize_residual_kana_candidate(leaked))

    def test_residual_kana_sanitizer_rejects_incomplete_fragment(self) -> None:
        self.assertIsNone(_sanitize_residual_kana_candidate("158\t首先是メジロパーナー"))

    def test_residual_kana_repair_retranslates_original_japanese_line(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.kana-repair")
        calls: list[tuple[str, str]] = []

        def request(source: str, system_prompt: str) -> str:
            calls.append((source, system_prompt))
            return "43\t何時開始？"

        translator._request_translation = request
        source = [SrtBlock(43, "00:01:00,000 --> 00:01:02,000", ["いつから？"])]
        malformed = [SrtBlock(43, "00:01:00,000 --> 00:01:02,000", ["いつはら："])]

        repaired = translator._repair_residual_kana_blocks(malformed, source, "7")

        self.assertEqual(repaired[0].text, ["何時開始？"])
        self.assertEqual(calls[0][0], "43\tいつから？")

    def test_residual_name_repair_switches_to_strict_prompt_variant(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.strict-name-repair")
        translator._progress_callback = None
        translator._quality_events = []
        calls: list[str] = []

        def request(_source: str, system_prompt: str) -> str:
            calls.append(system_prompt)
            if len(calls) == 1:
                return "169\tあららぎ知道嗎？"
            return "169\t阿良良木知道嗎？"

        translator._request_translation = request
        block = SrtBlock(
            169,
            "00:10:35,420 --> 00:10:37,540",
            ["あららぎは知っているのか?"],
        )

        repaired = translator._repair_single_kana_residual(
            block,
            "169\tあららぎ知道嗎？",
            "repair-169.169",
            "Characters: Koyomi Araragi, 阿良々木暦",
        )

        self.assertEqual(repaired[0].text, ["阿良良木知道嗎？"])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertIn("角色名、姓氏與稱呼", calls[1])
        self.assertEqual(translator.translation_quality_events, [])

    def test_malformed_singleton_residual_uses_kana_repair_before_omission(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.malformed-singleton-kana-repair"
        )
        translator._progress_callback = None
        translator._quality_events = []
        calls: list[str] = []

        def request(_source: str, system_prompt: str) -> str:
            calls.append(system_prompt)
            if len(calls) == 1:
                return "170\tあららぎ知道嗎？"
            return "169\t阿良良木知道嗎？"

        translator._request_translation = request
        block = SrtBlock(
            169,
            "00:10:35,420 --> 00:10:37,540",
            ["あららぎは知っているのか?"],
        )

        repaired = translator._repair_single_malformed_output(
            block,
            "wrong index",
            "repair-169",
            reason="Unexpected translated index: 170",
            translation_context="Characters: Koyomi Araragi, 阿良々木暦",
        )

        self.assertEqual(repaired[0].text, ["阿良良木知道嗎？"])
        self.assertEqual(len(calls), 2)
        self.assertIn("只需要翻譯一行", calls[0])
        self.assertEqual(translator.translation_quality_events, [])

    def test_residual_kana_context_echo_retries_once_without_context(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
            subtitle_quality_hard_max_primary_chars=64,
        )
        translator.logger = logging.getLogger("test.translator.context-echo-retry")
        calls: list[str] = []

        def request(_source: str, system_prompt: str) -> str:
            calls.append(system_prompt)
            if len(calls) == 1:
                return (
                    "43\t问题行前一句日文「前文」；"
                    "问题行中文「錯誤」；下一句日文「後文」"
                )
            return "43\t何時開始？"

        translator._request_translation = request
        block = SrtBlock(
            43,
            "00:00:01,000 --> 00:00:02,000",
            ["いつから？"],
        )
        context = "前一句日文「前文」；下一句日文「後文」"

        repaired = translator._repair_single_kana_residual(
            block,
            "43\tいつから？",
            "repair-43",
            context,
        )

        self.assertEqual(repaired[0].text, ["何時開始？"])
        self.assertEqual(len(calls), 2)
        self.assertIn("前文", calls[0])
        self.assertNotIn("前文", calls[1])

    def test_residual_kana_repair_does_not_retry_ambiguous_short_fragment(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.kana-fragment")
        calls: list[tuple[str, str]] = []

        def request(source: str, system_prompt: str) -> str:
            calls.append((source, system_prompt))
            return "213\tツ"

        translator._request_translation = request
        source = [SrtBlock(213, "00:10:00,000 --> 00:10:00,400", ["ツ"])]
        malformed = [SrtBlock(213, "00:10:00,000 --> 00:10:00,400", ["ツ"])]

        repaired = translator._repair_residual_kana_blocks(malformed, source, "36")

        self.assertEqual(repaired[0].text, ["……"])
        self.assertEqual(len(calls), 1)

    def test_residual_kana_repair_safely_omits_unresolved_model_output(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.kana-safe-fallback")
        calls: list[tuple[str, str]] = []

        def request(source: str, system_prompt: str) -> str:
            calls.append((source, system_prompt))
            return "1\tおにぎり"

        translator._request_translation = request
        source = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["おにぎり"])]
        malformed = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["おにぎり"])]

        repaired = translator._repair_residual_kana_blocks(malformed, source, "1")

        self.assertEqual(repaired[0].text, ["……"])
        # Stop after the same deterministic invalid response is seen twice.
        self.assertEqual(len(calls), 2)

    def test_residual_kana_repair_does_not_hide_transport_failure(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.kana-transport-failure")

        def request(_source: str, _system_prompt: str) -> str:
            raise TranslationTimeoutError("translator unavailable")

        translator._request_translation = request
        source = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["おにぎり"])]
        malformed = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["おにぎり"])]

        with self.assertRaisesRegex(TranslationError, "translator unavailable"):
            translator._repair_residual_kana_blocks(malformed, source, "1")

    def test_residual_kana_repair_preserves_explicit_asr_review_request(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.kana-asr-review")
        translator._request_translation = lambda _source, _system_prompt: f"1\t{ASR_REVIEW_TOKEN}"
        source = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["おにぎり"])]
        malformed = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["おにぎり"])]

        with self.assertRaises(AsrReviewError):
            translator._repair_residual_kana_blocks(malformed, source, "1")

    def test_singleton_malformed_index_uses_dedicated_repair_prompt(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.singleton-repair")
        translator._progress_callback = None
        calls: list[tuple[str, str]] = []

        def request(source: str, system_prompt: str) -> str:
            calls.append((source, system_prompt))
            if len(calls) == 1:
                return (
                    "SRT編號<TAB>字幕文字。\n"
                    "1\t我睜開眼睛，發現眼前是一片陌生的天花板。\n"
                    "這裡不是我的房間。"
                )
            return "337\t向明天奔跑"

        translator._request_translation = request
        block = SrtBlock(337, "00:23:27,000 --> 00:23:31,440", ["明日まで駆け出す"])

        translated = translator._translate_batch([block], "57", "")

        self.assertEqual(translated[0].text, ["向明天奔跑"])
        self.assertEqual(len(calls), 2)
        self.assertIn("只需要翻譯一行", calls[1][1])

    def test_non_japanese_singleton_repair_forbids_source_echo(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.non-ja-singleton-repair")
        translator._progress_callback = None
        translator._translator_models = ("sakura-primary", "qwen-multilingual")
        translator._translator_model_index = 0
        translator._translator_model = "sakura-primary"
        calls: list[tuple[str, str]] = []
        repair_calls: list[tuple[str, str, str]] = []
        source = "Is it? Well, in any case, the feedback architecture is way too sensitive."

        def request(prompt: str, system_prompt: str) -> str:
            calls.append((prompt, system_prompt))
            return f"256\t{source}"

        def request_with_model_timeout(
            prompt: str,
            system_prompt: str,
            model: str,
        ) -> str:
            repair_calls.append((prompt, system_prompt, model))
            return "256\t是嗎？總之，回饋架構實在太敏感了。"

        translator._request_translation = request
        translator._request_translation_with_model_timeout = request_with_model_timeout
        block = SrtBlock(256, "00:10:00,000 --> 00:10:03,000", [source])

        translated = translator._translate_batch(
            [block],
            "43.2.1",
            "",
            source_language="en",
        )

        self.assertEqual(translated[0].text, ["是嗎？總之，回饋架構實在太敏感了。"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(repair_calls), 1)
        self.assertEqual(repair_calls[0][2], "qwen-multilingual")
        self.assertIn("把一行外語動漫字幕翻譯", repair_calls[0][1])
        self.assertIn("禁止照抄或保留來源外語", repair_calls[0][1])

    def test_repetitive_kana_runaway_uses_concise_lyric_repair_prompt(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.repetitive-lyric-repair"
        )
        translator._progress_callback = None
        calls: list[tuple[str, str]] = []

        def request(source: str, system_prompt: str) -> str:
            calls.append((source, system_prompt))
            if len(calls) == 1:
                return "1\t" + ("慢慢" * 60)
            return "1\t悠然悠然，百合百合"

        translator._request_translation = request
        block = SrtBlock(
            1,
            "00:00:02,200 --> 00:00:06,200",
            ["ゆるりゆるりゆるりゆゆゆるりゆゆるゆりゆゆ"],
        )

        translated = translator._translate_batch([block], "1.1.1", "")

        self.assertEqual(translated[0].text, ["悠然悠然，百合百合"])
        self.assertEqual(len(calls), 2)
        self.assertIn("反覆吟唱", calls[1][1])
        self.assertIn("最多24個中文字", calls[1][1])
        self.assertEqual(translator.translation_quality_events, [])

    def test_repetitive_kana_repair_collapses_exact_runaway_model_output(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger(
            "test.translator.repetitive-lyric-collapse"
        )
        translator._progress_callback = None
        calls = 0

        def request(_source: str, _system_prompt: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "1\t" + ("慢慢地" * 60)
            return "1\t" + ("悠" * 62)

        translator._request_translation = request
        block = SrtBlock(
            1,
            "00:00:02,200 --> 00:00:06,200",
            ["ゆるりゆるりゆるりゆゆゆるりゆゆるゆりゆゆ"],
        )

        translated = translator._translate_batch([block], "1.1.1", "")

        self.assertEqual(translated[0].text, ["悠悠"])
        self.assertEqual(calls, 2)
        self.assertEqual(translator.translation_quality_events, [])

    def test_singleton_invalid_repair_safely_omits_and_records_quality_event(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=3,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.singleton-safe-omit")
        translator._progress_callback = None
        calls: list[str] = []

        def request(_source: str, _system_prompt: str) -> str:
            calls.append("call")
            return "1\t輸出格式：原編號<TAB>中文字幕"

        translator._request_translation = request
        block = SrtBlock(337, "00:23:27,000 --> 00:23:31,440", ["明日まで駆け出す"])

        translated = translator._translate_batch([block], "57", "")

        self.assertEqual(translated[0].text, ["……"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(translator.translation_quality_events[0]["index"], 337)
        self.assertEqual(translator.translation_quality_events[0]["code"], "translation_safe_omission")

    def test_singleton_asr_review_enters_bounded_recovery_event_path(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.asr-review-recovery")
        translator._progress_callback = None
        translator._request_translation = (
            lambda _source, _system_prompt: f"119\t{ASR_REVIEW_TOKEN}"
        )
        block = SrtBlock(119, "00:01:26,000 --> 00:01:28,000", ["聞き取れない"])

        translated = translator._translate_batch([block], "repair-119", "")

        self.assertEqual(translated[0].text, ["……"])
        self.assertEqual(translator.translation_quality_events[0]["index"], 119)
        self.assertEqual(
            translator.translation_quality_events[0]["code"],
            "translation_safe_omission",
        )
        self.assertIn("ASR review requested", translator.translation_quality_events[0]["reason"])

    def test_non_japanese_article_asr_review_uses_context_safe_fallback(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=1,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.non-ja-article-fallback")
        translator._progress_callback = None
        translator._request_translation = (
            lambda _source, _system_prompt: f"496\t{ASR_REVIEW_TOKEN}"
        )
        block = SrtBlock(496, "00:16:00,000 --> 00:16:00,800", ["The"])

        translated = translator._translate_batch(
            [block],
            "repair-496",
            "",
            source_language="en",
        )

        self.assertEqual(translated[0].text, ["該"])
        self.assertEqual(translator.translation_quality_events, [])

    def test_singleton_repair_transport_failure_is_retried_and_not_hidden(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            max_retries=2,
            translation_glossary={},
            translation_context_retry_without_context=False,
            translation_reject_residual_kana=True,
            translation_allow_source_fallback=False,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.singleton-transport")
        translator._progress_callback = None
        calls = 0

        def request(_source: str, _system_prompt: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return "1\t錯誤編號"
            raise TranslationTimeoutError("translator unavailable")

        translator._request_translation = request
        block = SrtBlock(337, "00:23:27,000 --> 00:23:31,440", ["明日まで駆け出す"])

        with self.assertRaisesRegex(TranslationError, "without a model response"):
            translator._translate_batch([block], "57", "")

        self.assertEqual(calls, 3)
        self.assertEqual(translator.translation_quality_events, [])

    def test_translate_blocks_persists_safe_omission_events(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            batch_size=1,
            translation_context_enabled=False,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        translator.logger = logging.getLogger("test.translator.event-sidecar")
        translator._progress_callback = None
        translator._build_translation_context = lambda _blocks, _path: ""

        def translate_batch(batch: list[SrtBlock], batch_index: str, _context: str = "") -> list[SrtBlock]:
            return translator._safe_omit_translation_line(
                batch[0],
                source="壊れた音声",
                rejected_output="model preamble",
                reason="invalid output",
                batch_index=batch_index,
                attempts=1,
            )

        translator._translate_batch = translate_batch
        source = [SrtBlock(8, "00:00:01,000 --> 00:00:02,000", ["壊れた音声"])]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "episode.zh-CN.srt"
            translator.translate_blocks(source, Path(temp_dir) / "episode.ja.srt", output)
            events = read_translation_quality_events(output)

        self.assertEqual([event["index"] for event in events], [8])

    def test_rejects_pathologically_long_translation_line(self) -> None:
        config = SimpleNamespace(
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
        )
        source = [SrtBlock(85, "00:00:01,000 --> 00:00:02,000", ["\u30b7\u30a8\u30ca"])]
        translated = [
            SrtBlock(
                85,
                "00:00:01,000 --> 00:00:02,000",
                ["\u30b7\u30a8\u30ca" * 100],
            )
        ]

        with self.assertRaisesRegex(ValueError, "unreasonably long"):
            _validate_translation_output_size(source, translated, config)

    def test_translation_size_gate_matches_publication_hard_limit(self) -> None:
        config = SimpleNamespace(
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
            subtitle_quality_hard_max_primary_chars=64,
        )
        source = [SrtBlock(6, "00:00:01,000 --> 00:00:02,000", ["短句"])]
        translated = [
            SrtBlock(6, source[0].timing, ["中" * 65])
        ]

        with self.assertRaisesRegex(ValueError, "allowed=32"):
            _validate_translation_output_size(source, translated, config)

    def test_translate_blocks_does_not_reopen_source_srt(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(batch_size=10, translation_context_enabled=False)
        translator.logger = logging.getLogger("test.translator")
        translator._translate_batch = lambda batch, _batch_index, _context="": [
            SrtBlock(block.index, block.timing, ["translated"]) for block in batch
        ]
        source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]

        with tempfile.TemporaryDirectory() as temp_dir:
            missing_source = Path(temp_dir) / "missing.ja.srt"
            output = Path(temp_dir) / "out.zh-CN.srt"

            translator.translate_blocks(source_blocks, missing_source, output)

            self.assertTrue(output.exists())
            self.assertIn("translated", output.read_text(encoding="utf-8-sig"))

    def test_line_translation_keeps_only_subtitle_data_in_user_prompt(self) -> None:
        prompt = _build_line_translation_prompt(
            [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["太郎さん"])],
        )
        system_prompt = _build_line_translation_system_prompt(
            {"太郎": "太郎"},
            "角色：太郎，主角，語氣較輕鬆。",
        )

        self.assertEqual(prompt, "1\t太郎さん")
        self.assertNotIn("術語表", prompt)
        self.assertNotIn("角色：太郎", prompt)
        self.assertIn("術語參考", system_prompt)
        self.assertIn("角色：太郎", system_prompt)
        self.assertNotIn("\n1.", system_prompt)

    def test_non_japanese_source_uses_language_aware_chinese_prompt(self) -> None:
        system_prompt = _build_line_translation_system_prompt(
            {},
            source_language="en",
        )

        self.assertIn("英文字幕", system_prompt)
        self.assertIn("簡體中文", system_prompt)
        self.assertNotIn("你是日文動漫字幕翻譯模型", system_prompt)

    def test_translate_blocks_generates_context_once_and_passes_to_batches(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            batch_size=10,
            translation_context_enabled=True,
            translation_context_max_blocks=20,
            translation_context_max_chars=1000,
            translation_context_max_output_chars=500,
            translation_glossary={},
        )
        translator.logger = logging.getLogger("test.translator")
        translator._build_translation_context = lambda _blocks, _path: "episode context"
        seen_contexts: list[str] = []

        def translate_batch(batch: list[SrtBlock], _batch_index: str, context: str = "") -> list[SrtBlock]:
            seen_contexts.append(context)
            return [SrtBlock(block.index, block.timing, ["translated"]) for block in batch]

        translator._translate_batch = translate_batch
        source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "out.zh-CN.srt"
            translator.translate_blocks(source_blocks, Path(temp_dir) / "source.ja.srt", output)

        self.assertEqual(seen_contexts, ["episode context"])

    def test_translate_batch_retries_without_context_after_indexed_output_failure(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translation_glossary={},
            translation_context_retry_without_context=True,
            translation_context_auto_disable=True,
            translation_context_fast_retry_without_context_on_format_error=True,
            translation_reject_residual_kana=False,
            translation_allow_source_fallback=False,
            max_retries=1,
        )
        translator.logger = logging.getLogger("test.translator")
        calls: list[tuple[str, str]] = []

        def request(prompt: str, system_prompt: str) -> str:
            calls.append((prompt, system_prompt))
            if "series context" in system_prompt:
                return "model preamble only"
            return "1\ttranslated one\n2\ttranslated two"

        translator._request_translation = request
        batch = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"]),
            SrtBlock(2, "00:00:02,000 --> 00:00:03,000", ["source two"]),
        ]

        translated = translator._translate_batch(batch, "1", "series context")

        self.assertEqual([block.text[0] for block in translated], ["translated one", "translated two"])
        self.assertEqual(len(calls), 2)

    def test_translate_batch_retries_without_context_when_context_is_echoed_as_indexes(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translation_glossary={},
            translation_context_retry_without_context=True,
            translation_context_auto_disable=True,
            translation_context_fast_retry_without_context_on_format_error=True,
            translation_reject_residual_kana=False,
            translation_allow_source_fallback=False,
            translation_split_batch_on_format_error=True,
            max_retries=1,
        )
        translator.logger = logging.getLogger("test.translator")
        translator._progress_callback = None
        calls: list[tuple[str, str]] = []

        def request(prompt: str, system_prompt: str) -> str:
            calls.append((prompt, system_prompt))
            if "series context" in system_prompt:
                return "1. 角色：測試角色\n7\ttranslated seven\n8\ttranslated eight"
            return "7\ttranslated seven\n8\ttranslated eight"

        translator._request_translation = request
        batch = [
            SrtBlock(7, "00:00:01,000 --> 00:00:02,000", ["source seven"]),
            SrtBlock(8, "00:00:02,000 --> 00:00:03,000", ["source eight"]),
        ]

        translated = translator._translate_batch(batch, "2", "series context")

        self.assertEqual([block.text[0] for block in translated], ["translated seven", "translated eight"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0] == "7\tsource seven\n8\tsource eight" for call in calls))

    def test_translate_blocks_disables_context_for_remaining_batches_after_pollution(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            batch_size=1,
            translation_context_enabled=True,
            translation_context_max_blocks=20,
            translation_context_max_chars=1000,
            translation_context_max_output_chars=500,
            translation_glossary={},
            translation_context_retry_without_context=True,
            translation_context_auto_disable=True,
            translation_context_fast_retry_without_context_on_format_error=True,
            translation_context_retry_without_context_on_timeout=True,
            translation_reject_residual_kana=False,
            translation_allow_source_fallback=False,
            max_retries=3,
        )
        translator.logger = logging.getLogger("test.translator")
        translator._build_translation_context = lambda _blocks, _path: "episode context"
        calls: list[tuple[str, str]] = []

        def request(prompt: str, system_prompt: str) -> str:
            calls.append((prompt, system_prompt))
            if "episode context" in system_prompt:
                return "assistant preamble only"
            if "1\t" in prompt:
                return "1\ttranslated one"
            return "2\ttranslated two"

        translator._request_translation = request
        source_blocks = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"]),
            SrtBlock(2, "00:00:02,000 --> 00:00:03,000", ["source two"]),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "out.zh-CN.srt"
            translator.translate_blocks(source_blocks, Path(temp_dir) / "source.ja.srt", output)

        self.assertEqual(len(calls), 3)
        self.assertEqual(sum("episode context" in system for _prompt, system in calls), 1)

    def test_translate_batch_retries_without_context_after_context_timeout(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translation_glossary={},
            translation_context_retry_without_context=True,
            translation_context_auto_disable=True,
            translation_context_retry_without_context_on_timeout=True,
            translation_context_fast_retry_without_context_on_format_error=True,
            translation_reject_residual_kana=False,
            translation_allow_source_fallback=False,
            translation_split_batch_on_timeout=True,
            max_retries=3,
        )
        translator.logger = logging.getLogger("test.translator")
        calls: list[tuple[str, str]] = []

        def request(prompt: str, system_prompt: str) -> str:
            calls.append((prompt, system_prompt))
            if "series context" in system_prompt:
                raise TranslationTimeoutError("Translation API request timed out after 120s")
            return "1\ttranslated one\n2\ttranslated two"

        translator._request_translation = request
        batch = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"]),
            SrtBlock(2, "00:00:02,000 --> 00:00:03,000", ["source two"]),
        ]

        translated = translator._translate_batch(batch, "1", "series context")

        self.assertEqual([block.text[0] for block in translated], ["translated one", "translated two"])
        self.assertEqual(len(calls), 2)

    def test_translate_batch_splits_immediately_after_timeout(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translation_glossary={},
            translation_context_retry_without_context=True,
            translation_reject_residual_kana=False,
            translation_allow_source_fallback=False,
            translation_split_batch_on_timeout=True,
            max_retries=3,
        )
        translator.logger = logging.getLogger("test.translator")
        calls: list[str] = []

        def request(prompt: str, _system_prompt: str) -> str:
            calls.append(prompt)
            if all(f"{index}\t" in prompt for index in (1, 2, 3, 4)):
                raise TranslationTimeoutError("Translation API request timed out after 180s")
            if "1\t" in prompt:
                return "1\ttranslated one\n2\ttranslated two"
            return "3\ttranslated three\n4\ttranslated four"

        translator._request_translation = request
        batch = [
            SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source one"]),
            SrtBlock(2, "00:00:02,000 --> 00:00:03,000", ["source two"]),
            SrtBlock(3, "00:00:03,000 --> 00:00:04,000", ["source three"]),
            SrtBlock(4, "00:00:04,000 --> 00:00:05,000", ["source four"]),
        ]

        translated = translator._translate_batch(batch, "1", "")

        self.assertEqual(
            [block.text[0] for block in translated],
            ["translated one", "translated two", "translated three", "translated four"],
        )
        self.assertEqual(len(calls), 3)

    def test_translate_batch_splits_immediately_after_index_format_error(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translation_glossary={},
            translation_context_retry_without_context=True,
            translation_reject_residual_kana=False,
            translation_allow_source_fallback=False,
            translation_split_batch_on_format_error=True,
            max_retries=3,
        )
        translator.logger = logging.getLogger("test.translator")
        translator._progress_callback = None
        calls: list[str] = []

        def request(prompt: str, _system_prompt: str) -> str:
            calls.append(prompt)
            if all(f"{index}\t" in prompt for index in (1, 2, 3, 4)):
                return "1\ttranslated one\n2\ttranslated two\n3\ttranslated three"
            if "1\t" in prompt:
                return "1\ttranslated one\n2\ttranslated two"
            return "3\ttranslated three\n4\ttranslated four"

        translator._request_translation = request
        batch = [
            SrtBlock(index, f"00:00:0{index},000 --> 00:00:0{index + 1},000", [f"source {index}"])
            for index in range(1, 5)
        ]

        translated = translator._translate_batch(batch, "1", "")

        self.assertEqual(
            [block.text[0] for block in translated],
            ["translated one", "translated two", "translated three", "translated four"],
        )
        self.assertEqual(len(calls), 3)

    def test_translate_batch_splits_immediately_after_runaway_output(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translation_glossary={},
            translation_context_retry_without_context=True,
            translation_reject_residual_kana=False,
            translation_allow_source_fallback=False,
            translation_split_batch_on_format_error=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
            max_retries=3,
        )
        translator.logger = logging.getLogger("test.translator")
        translator._progress_callback = None
        calls: list[str] = []

        def request(prompt: str, _system_prompt: str) -> str:
            calls.append(prompt)
            if all(f"{index}\t" in prompt for index in (1, 2, 3, 4)):
                return "\n".join(
                    [
                        f"1\t{'runaway' * 100}",
                        "2\ttranslated two",
                        "3\ttranslated three",
                        "4\ttranslated four",
                    ]
                )
            if "1\t" in prompt:
                return "1\ttranslated one\n2\ttranslated two"
            return "3\ttranslated three\n4\ttranslated four"

        translator._request_translation = request
        batch = [
            SrtBlock(index, f"00:00:0{index},000 --> 00:00:0{index + 1},000", [f"source {index}"])
            for index in range(1, 5)
        ]

        translated = translator._translate_batch(batch, "1", "")

        self.assertEqual(
            [block.text[0] for block in translated],
            ["translated one", "translated two", "translated three", "translated four"],
        )
        self.assertEqual(len(calls), 3)

    def test_translate_blocks_combines_series_and_episode_contexts(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            batch_size=10,
            translation_context_enabled=True,
            translation_context_max_blocks=20,
            translation_context_max_chars=1000,
            translation_context_max_output_chars=500,
            translation_glossary={},
        )
        translator.logger = logging.getLogger("test.translator")
        translator._build_translation_context = lambda _blocks, _path: "episode context"
        seen_contexts: list[str] = []

        def translate_batch(batch: list[SrtBlock], _batch_index: str, context: str = "") -> list[SrtBlock]:
            seen_contexts.append(context)
            return [SrtBlock(block.index, block.timing, ["translated"]) for block in batch]

        translator._translate_batch = translate_batch
        source_blocks = [SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["source"])]

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "out.zh-CN.srt"
            translator.translate_blocks(
                source_blocks,
                Path(temp_dir) / "source.ja.srt",
                output,
                series_context="series context",
            )

        self.assertEqual(seen_contexts, ["series context\n\nepisode context"])

    def test_selects_only_available_translator_model_when_configured_alias_is_missing(self) -> None:
        available = ["hf.co/SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF:latest"]

        selected = _select_available_translator_model("SakuraLLM:latest", available)

        self.assertEqual(selected, available[0])

    def test_selects_unique_alias_match_from_multiple_translator_models(self) -> None:
        available = [
            "other-model:latest",
            "hf.co/SakuraLLM/Sakura-7B-Qwen2.5-v1.0-GGUF:latest",
        ]

        selected = _select_available_translator_model("SakuraLLM:latest", available)

        self.assertEqual(selected, available[1])

    def test_keeps_configured_translator_model_when_multiple_models_do_not_match(self) -> None:
        selected = _select_available_translator_model("SakuraLLM:latest", ["a:latest", "b:latest"])

        self.assertEqual(selected, "SakuraLLM:latest")

    def test_translation_request_uses_ordered_runtime_model_fallback(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translation_request_hard_timeout_seconds=1,
            translator_timeout_seconds=1,
            translation_request_max_tokens=64,
        )
        translator.logger = logging.getLogger("test.translator.model-fallback")
        translator._progress_callback = None
        translator._translator_models = ("primary-model", "secondary-model")
        translator._translator_model_index = 0
        translator._translator_model = "primary-model"
        calls: list[str] = []

        def create(**kwargs: object) -> object:
            model = str(kwargs["model"])
            calls.append(model)
            if model == "primary-model":
                raise RuntimeError("primary unavailable")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="1\t翻譯"))]
            )

        translator.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        content = translator._request_translation("1\t原文", "system")

        self.assertEqual(content, "1\t翻譯")
        self.assertEqual(calls, ["primary-model", "secondary-model"])
        self.assertEqual(translator._translator_model, "secondary-model")
        self.assertEqual(translator._translator_model_index, 1)

    def test_invalid_model_output_advances_without_wrapping_to_primary(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.logger = logging.getLogger("test.translator.model-advance")
        translator._progress_callback = None
        translator._translator_models = ("primary", "secondary", "small")
        translator._translator_model_index = 0
        translator._translator_model = "primary"

        self.assertTrue(translator._advance_translator_model("malformed output"))
        self.assertEqual(translator._translator_model, "secondary")
        self.assertTrue(translator._advance_translator_model("residual kana"))
        self.assertEqual(translator._translator_model, "small")
        self.assertFalse(translator._advance_translator_model("still malformed"))
        self.assertEqual(translator._translator_model, "small")

    def test_translation_timeout_uses_next_model_and_all_failures_stay_closed(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.logger = logging.getLogger("test.translator.timeout-fallback")
        translator._progress_callback = None
        translator._translator_models = ("primary", "small")
        translator._translator_model_index = 0
        translator._translator_model = "primary"
        attempts: list[str] = []

        def request(
            _source_text: str,
            _system_prompt: str,
            model: str,
        ) -> str:
            attempts.append(model)
            if model == "primary":
                raise TranslationTimeoutError("primary timeout")
            return "1\t完成"

        translator._request_translation_with_model_timeout = request
        self.assertEqual(translator._request_translation("1\t原文"), "1\t完成")
        self.assertEqual(attempts, ["primary", "small"])

        translator._translator_model_index = 0
        translator._translator_model = "primary"
        translator._request_translation_with_model_timeout = (
            lambda _source, _system, model: (_ for _ in ()).throw(
                TranslationError(f"{model} unavailable")
            )
        )
        with self.assertRaisesRegex(
            TranslationError,
            r"Translation models failed \(primary, small\)",
        ):
            translator._request_translation("1\t原文")

    def test_resolves_configured_model_chain_in_order_and_deduplicates(self) -> None:
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(
            translator_model="primary",
            translator_fallback_models=["secondary", "PRIMARY", "small"],
            translator_base_url="http://translator.invalid/v1",
        )
        translator.logger = logging.getLogger("test.translator.model-resolve")
        translator.client = SimpleNamespace(
            models=SimpleNamespace(
                list=lambda: SimpleNamespace(
                    data=[
                        SimpleNamespace(id="primary"),
                        SimpleNamespace(id="secondary"),
                        SimpleNamespace(id="small"),
                    ]
                )
            )
        )

        self.assertEqual(
            translator._resolve_translator_models(),
            ("primary", "secondary", "small"),
        )

    def test_extracts_model_ids_from_openai_compatible_response(self) -> None:
        response = SimpleNamespace(data=[SimpleNamespace(id="model-a"), {"id": "model-b"}, {"name": "skip"}])

        self.assertEqual(_model_ids_from_response(response), ["model-a", "model-b"])


if __name__ == "__main__":
    unittest.main()
