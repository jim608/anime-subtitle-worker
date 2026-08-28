from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from srt_utils import SrtBlock, read_srt, write_srt
from translation_checkpoint import (
    TranslationCheckpointError,
    load_translation_checkpoint,
    translation_checkpoint_path,
    translation_checkpoint_signature,
    write_translation_checkpoint,
)
from translator import SubtitleTranslator


class TranslationCheckpointTest(unittest.TestCase):
    @staticmethod
    def _blocks() -> list[SrtBlock]:
        return [
            SrtBlock(
                index,
                f"00:00:0{index},000 --> 00:00:0{index + 1},000",
                [f"source {index}"],
            )
            for index in range(1, 4)
        ]

    @staticmethod
    def _quality_event(index: int, **overrides: object) -> dict[str, object]:
        event: dict[str, object] = {
            "code": "translation_safe_omission",
            "severity": "fail",
            "index": index,
            "message": "Translation omitted an unresolved line.",
            "source": f"source {index}",
            "output": "unresolved output",
            "reason": "malformed model output",
            "batch_index": str(index),
        }
        event.update(overrides)
        return event

    def test_checkpoint_rejects_signature_and_timing_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            batches = [[block] for block in self._blocks()]
            signature = translation_checkpoint_signature(
                self._blocks(),
                output_path=output,
                batch_size=1,
                translation_context="context",
                glossary={"name": "姓名"},
                model_chain=["primary", "small"],
            )
            path = translation_checkpoint_path(root, output)
            translated = [SrtBlock(1, batches[0][0].timing, ["完成一"])]
            write_translation_checkpoint(
                path,
                signature=signature,
                output_path=output,
                completed_batches=[translated],
                last_model="primary",
                quality_events=[],
            )

            restored, count, model, events = load_translation_checkpoint(
                path,
                signature=signature,
                batches=batches,
            )
            self.assertEqual(count, 1)
            self.assertEqual(restored[0].text, ["完成一"])
            self.assertEqual(model, "primary")
            self.assertEqual(events, [])
            self.assertEqual(
                load_translation_checkpoint(
                    path,
                    signature="0" * 64,
                    batches=batches,
                ),
                ([], 0, None, []),
            )

            payload = path.read_text(encoding="utf-8").replace(
                batches[0][0].timing,
                "00:01:00,000 --> 00:01:01,000",
            )
            path.write_text(payload, encoding="utf-8")
            self.assertEqual(
                load_translation_checkpoint(
                    path,
                    signature=signature,
                    batches=batches,
                ),
                ([], 0, None, []),
            )

    def test_checkpoint_round_trips_normalized_quality_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            source = self._blocks()
            batches = [[block] for block in source]
            signature = translation_checkpoint_signature(
                source,
                output_path=output,
                batch_size=1,
                translation_context="",
                glossary={},
                model_chain=["primary"],
            )
            path = translation_checkpoint_path(root, output)
            completed = [
                [SrtBlock(1, source[0].timing, ["target one"])],
                [SrtBlock(2, source[1].timing, ["target two"])],
            ]
            write_translation_checkpoint(
                path,
                signature=signature,
                output_path=output,
                completed_batches=completed,
                last_model="primary",
                quality_events=[
                    self._quality_event(
                        1,
                        code=" TRANSLATION_SAFE_OMISSION ",
                        severity=" FAIL ",
                        message="  Translation\n omitted   one line.  ",
                    ),
                    self._quality_event(2, severity="WARN", batch_index="repair-2.1"),
                ],
            )

            restored, count, model, events = load_translation_checkpoint(
                path,
                signature=signature,
                batches=batches,
            )

            self.assertEqual([block.index for block in restored], [1, 2])
            self.assertEqual(count, 2)
            self.assertEqual(model, "primary")
            self.assertEqual([event["index"] for event in events], [1, 2])
            self.assertEqual(events[0]["code"], "translation_safe_omission")
            self.assertEqual(events[0]["severity"], "fail")
            self.assertEqual(events[0]["message"], "Translation omitted one line.")
            self.assertEqual(events[1]["severity"], "warn")

    def test_checkpoint_write_rejects_event_outside_completed_blocks_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            source = self._blocks()
            signature = translation_checkpoint_signature(
                source,
                output_path=output,
                batch_size=1,
                translation_context="",
                glossary={},
                model_chain=["primary"],
            )
            path = translation_checkpoint_path(root, output)
            completed = [[SrtBlock(1, source[0].timing, ["target one"])]]
            write_translation_checkpoint(
                path,
                signature=signature,
                output_path=output,
                completed_batches=completed,
                last_model="primary",
                quality_events=[self._quality_event(1)],
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(TranslationCheckpointError, "is not restored"):
                write_translation_checkpoint(
                    path,
                    signature=signature,
                    output_path=output,
                    completed_batches=completed,
                    last_model="primary",
                    quality_events=[self._quality_event(2)],
                )

            self.assertEqual(path.read_bytes(), before)

    def test_checkpoint_rejects_malformed_quality_events_on_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            source = self._blocks()
            signature = translation_checkpoint_signature(
                source,
                output_path=output,
                batch_size=1,
                translation_context="",
                glossary={},
                model_chain=["primary"],
            )
            completed = [[SrtBlock(1, source[0].timing, ["target one"])]]
            malformed = [
                None,
                {**self._quality_event(1), "unexpected": "field"},
                {
                    key: value
                    for key, value in self._quality_event(1).items()
                    if key != "reason"
                },
                self._quality_event(True),
                self._quality_event(1, severity="info"),
                self._quality_event(1, message="   "),
                self._quality_event(1, batch_index="bad batch"),
            ]
            for event in malformed:
                with self.subTest(event=event):
                    with self.assertRaises(TranslationCheckpointError):
                        write_translation_checkpoint(
                            translation_checkpoint_path(root, output),
                            signature=signature,
                            output_path=output,
                            completed_batches=completed,
                            last_model="primary",
                            quality_events=[event],  # type: ignore[list-item]
                        )

    def test_checkpoint_load_rejects_entire_payload_for_bad_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            source = self._blocks()
            batches = [[block] for block in source]
            signature = translation_checkpoint_signature(
                source,
                output_path=output,
                batch_size=1,
                translation_context="",
                glossary={},
                model_chain=["primary"],
            )
            path = translation_checkpoint_path(root, output)
            write_translation_checkpoint(
                path,
                signature=signature,
                output_path=output,
                completed_batches=[
                    [SrtBlock(1, source[0].timing, ["target one"])]
                ],
                last_model="primary",
                quality_events=[self._quality_event(1)],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

            for mutation in (
                lambda event: event.update(index=2),
                lambda event: event.pop("code"),
                lambda event: event.update(severity="ignored"),
                lambda event: event.update(index=True),
            ):
                with self.subTest(mutation=mutation):
                    tampered = json.loads(json.dumps(payload))
                    mutation(tampered["quality_events"][0])
                    path.write_text(json.dumps(tampered), encoding="utf-8")
                    self.assertEqual(
                        load_translation_checkpoint(
                            path,
                            signature=signature,
                            batches=batches,
                        ),
                        ([], 0, None, []),
                    )

    def test_checkpoint_load_rejects_duplicate_json_event_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            source = self._blocks()
            batches = [[block] for block in source]
            signature = translation_checkpoint_signature(
                source,
                output_path=output,
                batch_size=1,
                translation_context="",
                glossary={},
                model_chain=["primary"],
            )
            path = translation_checkpoint_path(root, output)
            write_translation_checkpoint(
                path,
                signature=signature,
                output_path=output,
                completed_batches=[
                    [SrtBlock(1, source[0].timing, ["target one"])]
                ],
                last_model="primary",
                quality_events=[self._quality_event(1)],
            )
            raw = path.read_text(encoding="utf-8").replace(
                '"severity":"fail"',
                '"severity":"warn","severity":"fail"',
                1,
            )
            path.write_text(raw, encoding="utf-8")

            self.assertEqual(
                load_translation_checkpoint(path, signature=signature, batches=batches),
                ([], 0, None, []),
            )

    def test_checkpoint_signature_binds_translation_memory_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "episode.zh-CN.srt"
            arguments = dict(
                output_path=output,
                batch_size=1,
                translation_context="context",
                glossary={},
                model_chain=["primary"],
            )
            first = translation_checkpoint_signature(
                self._blocks(),
                translation_memory_decision_digest="1" * 64,
                **arguments,
            )
            second = translation_checkpoint_signature(
                self._blocks(),
                translation_memory_decision_digest="2" * 64,
                **arguments,
            )
            self.assertNotEqual(first, second)
            with self.assertRaises(TranslationCheckpointError):
                translation_checkpoint_signature(
                    self._blocks(),
                    translation_memory_decision_digest="not-a-hash",
                    **arguments,
                )

    def test_checkpoint_signature_binds_source_language_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "episode.zh-CN.srt"
            arguments = dict(
                output_path=output,
                batch_size=1,
                translation_context="context",
                glossary={},
                model_chain=["primary"],
            )
            english = translation_checkpoint_signature(
                self._blocks(),
                source_language="en",
                **arguments,
            )
            french = translation_checkpoint_signature(
                self._blocks(),
                source_language="fr",
                **arguments,
            )
            changed_source = self._blocks()
            changed_source[0] = SrtBlock(
                changed_source[0].index,
                changed_source[0].timing,
                ["changed source text"],
            )
            changed = translation_checkpoint_signature(
                changed_source,
                source_language="en",
                **arguments,
            )

            self.assertNotEqual(english, french)
            self.assertNotEqual(english, changed)

    def test_translate_blocks_resumes_after_crash_at_next_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            source = self._blocks()
            config = SimpleNamespace(
                work_path=root / "work",
                batch_size=1,
                translation_context_enabled=False,
                translation_context_max_blocks=10,
                translation_context_max_chars=1000,
                translation_context_max_output_chars=500,
                translation_glossary={},
                translation_reject_residual_kana=True,
                translation_max_line_chars=320,
                translation_max_line_expansion_ratio=8.0,
                subtitle_quality_hard_max_primary_chars=64,
            )

            def make_translator() -> SubtitleTranslator:
                translator = object.__new__(SubtitleTranslator)
                translator.config = config
                translator.logger = logging.getLogger("test.translation.checkpoint")
                translator._progress_callback = None
                translator._translator_models = ("primary", "small")
                translator._translator_model_index = 0
                translator._translator_model = "primary"
                translator._build_translation_context = lambda _blocks, _path: ""
                translator._commit_translation_output = (
                    lambda destination, blocks, reason: write_srt(destination, blocks)
                )
                return translator

            first = make_translator()
            first_calls: list[int] = []

            def crash_on_second(batch: list[SrtBlock], _index: str, _context: str) -> list[SrtBlock]:
                first_calls.append(batch[0].index)
                if batch[0].index == 2:
                    raise RuntimeError("simulated process crash")
                first._quality_events.append(self._quality_event(batch[0].index))
                return [SrtBlock(batch[0].index, batch[0].timing, ["完成一"])]

            first._translate_batch = crash_on_second
            with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
                first.translate_blocks(source, root / "episode.ja.srt", output)
            self.assertEqual(first_calls, [1, 2])
            self.assertTrue(translation_checkpoint_path(config.work_path, output).is_file())
            self.assertFalse(output.exists())

            resumed = make_translator()
            resumed_calls: list[int] = []

            def finish(batch: list[SrtBlock], _index: str, _context: str) -> list[SrtBlock]:
                resumed_calls.append(batch[0].index)
                return [
                    SrtBlock(
                        batch[0].index,
                        batch[0].timing,
                        [f"完成{batch[0].index}"],
                    )
                ]

            resumed._translate_batch = finish
            resumed.translate_blocks(source, root / "episode.ja.srt", output)

            self.assertEqual(resumed_calls, [2, 3])
            self.assertEqual(
                [block.text[0] for block in read_srt(output)],
                ["完成一", "完成2", "完成3"],
            )
            self.assertEqual(
                [event["index"] for event in resumed.translation_quality_events],
                [1],
            )
            self.assertFalse(translation_checkpoint_path(config.work_path, output).exists())

    def test_non_japanese_completed_checkpoint_is_retained_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "episode.zh-CN.srt"
            source = self._blocks()
            config = SimpleNamespace(
                work_path=root / "work",
                batch_size=1,
                translation_context_enabled=False,
                translation_context_max_blocks=10,
                translation_context_max_chars=1000,
                translation_context_max_output_chars=500,
                translation_glossary={},
                translation_reject_residual_kana=True,
                translation_max_line_chars=320,
                translation_max_line_expansion_ratio=8.0,
                subtitle_quality_hard_max_primary_chars=64,
            )

            def make_translator() -> SubtitleTranslator:
                translator = object.__new__(SubtitleTranslator)
                translator.config = config
                translator.logger = logging.getLogger("test.translation.source-checkpoint")
                translator._progress_callback = None
                translator._translator_models = ("primary", "small")
                translator._translator_model_index = 0
                translator._translator_model = "primary"
                translator._build_translation_context = lambda _blocks, _path: ""
                translator._commit_translation_output = (
                    lambda destination, blocks, reason: write_srt(destination, blocks)
                )
                return translator

            first = make_translator()
            first_calls: list[int] = []

            def translate(
                batch: list[SrtBlock],
                _index: str,
                _context: str,
                **_kwargs: str,
            ) -> list[SrtBlock]:
                first_calls.append(batch[0].index)
                return [
                    SrtBlock(
                        batch[0].index,
                        batch[0].timing,
                        [f"translated {batch[0].index}"],
                    )
                ]

            first._translate_batch = translate
            first.translate_blocks(
                source,
                root / "episode.en.srt",
                output,
                source_language="en",
            )

            checkpoint = translation_checkpoint_path(config.work_path, output)
            self.assertEqual(first_calls, [1, 2, 3])
            self.assertTrue(checkpoint.is_file())

            output.unlink()
            resumed = make_translator()

            def unexpected_model_call(*_args: object, **_kwargs: object) -> list[SrtBlock]:
                raise AssertionError("completed source-language checkpoint must avoid model calls")

            resumed._translate_batch = unexpected_model_call
            resumed.translate_blocks(
                source,
                root / "episode.en.srt",
                output,
                source_language="en",
            )

            self.assertEqual(
                [block.text[0] for block in read_srt(output)],
                ["translated 1", "translated 2", "translated 3"],
            )
            self.assertTrue(checkpoint.is_file())


if __name__ == "__main__":
    unittest.main()
