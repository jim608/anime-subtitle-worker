from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from transcriber_transformers_whisper import (
    _build_generate_kwargs,
    _build_pipeline_kwargs,
    _pipeline_task,
    _release_pipeline_memory,
    _result_has_plain_text_without_timestamp_chunks,
    _result_to_segments,
)


class _FakeTorch:
    float16 = "torch.float16"
    bfloat16 = "torch.bfloat16"
    float32 = "torch.float32"


class TransformersWhisperTranscriberTests(unittest.TestCase):
    def test_release_pipeline_memory_moves_model_to_cpu_and_clears_cuda_cache(self) -> None:
        model = SimpleNamespace(to=lambda device: moved_to.append(device))
        moved_to: list[str] = []
        pipe = SimpleNamespace(model=model)

        with (
            patch("transcriber_transformers_whisper.gc.collect") as collect_mock,
            patch("transcriber_transformers_whisper.release_cuda_cache") as release_mock,
        ):
            _release_pipeline_memory(pipe, _FakeTorch)

        self.assertEqual(moved_to, ["cpu"])
        collect_mock.assert_called_once_with()
        release_mock.assert_called_once_with(_FakeTorch)

    def test_result_to_segments_reads_pipeline_chunks(self) -> None:
        result = {
            "chunks": [
                {"timestamp": (1.0, 2.5), "text": "  こんにちは  "},
                {"timestamp": (3.0, None), "text": "次です"},
                {"timestamp": None, "text": "skip"},
            ]
        }

        self.assertEqual(
            _result_to_segments(result),
            [(1.0, 2.5, "こんにちは"), (3.0, 4.0, "次です")],
        )

    def test_result_to_segments_does_not_synthesize_fake_timing_from_plain_text(self) -> None:
        self.assertEqual(_result_to_segments("plain transcript without timestamps"), [])
        self.assertEqual(_result_to_segments({"text": "plain transcript without timestamps"}), [])

    def test_result_to_segments_groups_word_timestamps_into_readable_cues(self) -> None:
        result = {
            "chunks": [
                {"timestamp": (1.0, 1.4), "text": "We"},
                {"timestamp": (1.4, 1.8), "text": " are"},
                {"timestamp": (1.8, 2.3), "text": " ready."},
            ]
        }
        config = SimpleNamespace(
            subtitle_max_duration_seconds=4.8,
            subtitle_max_chars=24,
        )

        self.assertEqual(
            _result_to_segments(
                result,
                config=config,
                word_timestamps=True,
            ),
            [(1.0, 2.3, "We are ready.")],
        )

    def test_build_generate_kwargs_normalizes_japanese_language(self) -> None:
        config = SimpleNamespace(
            whisper_model="litagin/anime-whisper",
            whisper_language="ja",
            whisper_task="transcribe",
            whisper_no_repeat_ngram_size=3,
            whisper_repetition_penalty=1.15,
        )

        self.assertEqual(
            _build_generate_kwargs(config),
            {
                "language": "Japanese",
                "task": "transcribe",
                "no_repeat_ngram_size": 3,
                "repetition_penalty": 1.15,
            },
        )

    def test_build_generate_kwargs_uses_kotoba_language_code(self) -> None:
        config = SimpleNamespace(
            whisper_model="kotoba-tech/kotoba-whisper-v2.1",
            whisper_language="ja",
            whisper_task="transcribe",
            whisper_no_repeat_ngram_size=0,
            whisper_repetition_penalty=1.0,
        )

        self.assertEqual(
            _build_generate_kwargs(config),
            {
                "language": "ja",
                "task": "transcribe",
            },
        )

    def test_build_pipeline_kwargs_defaults_kotoba_to_standard_asr(self) -> None:
        config = _pipeline_config(
            whisper_model="kotoba-tech/kotoba-whisper-v2.1",
            transformers_whisper_punctuator=True,
            transformers_whisper_stable_ts=True,
        )

        kwargs = _build_pipeline_kwargs(config, _FakeTorch)

        self.assertEqual(kwargs["task"], "automatic-speech-recognition")
        self.assertEqual(kwargs["model"], "kotoba-tech/kotoba-whisper-v2.1")
        self.assertEqual(kwargs["device"], 0)
        self.assertEqual(kwargs["torch_dtype"], "torch.float16")
        self.assertTrue(kwargs["trust_remote_code"])
        self.assertNotIn("punctuator", kwargs)
        self.assertNotIn("stable_ts", kwargs)
        self.assertEqual(kwargs["model_kwargs"], {"attn_implementation": "sdpa"})

    def test_build_pipeline_kwargs_keeps_standard_asr_for_non_kotoba_models(self) -> None:
        config = _pipeline_config(
            whisper_model="litagin/anime-whisper",
            transformers_whisper_punctuator=True,
            transformers_whisper_stable_ts=True,
        )

        kwargs = _build_pipeline_kwargs(config, _FakeTorch)

        self.assertEqual(kwargs["task"], "automatic-speech-recognition")
        self.assertNotIn("punctuator", kwargs)
        self.assertNotIn("stable_ts", kwargs)

    def test_build_pipeline_kwargs_respects_explicit_kotoba_custom_task_override(self) -> None:
        config = _pipeline_config(
            whisper_model="kotoba-tech/kotoba-whisper-v2.1",
            transformers_whisper_task="kotoba-whisper",
            transformers_whisper_punctuator=True,
        )

        kwargs = _build_pipeline_kwargs(config, _FakeTorch)

        self.assertEqual(kwargs["task"], "kotoba-whisper")
        self.assertTrue(kwargs["punctuator"])

    def test_pipeline_task_auto_uses_standard_asr_for_kotoba(self) -> None:
        self.assertEqual(
            _pipeline_task(_pipeline_config(whisper_model="kotoba-tech/kotoba-whisper-v2.1")),
            "automatic-speech-recognition",
        )
        self.assertEqual(
            _pipeline_task(_pipeline_config(whisper_model="Systran/faster-whisper-large-v3")),
            "automatic-speech-recognition",
        )

    def test_plain_text_without_timestamp_chunks_is_rejected(self) -> None:
        self.assertTrue(
            _result_has_plain_text_without_timestamp_chunks(
                {"text": "full transcript but no usable subtitle timing"}
            )
        )
        self.assertFalse(
            _result_has_plain_text_without_timestamp_chunks(
                {"text": "timed", "chunks": [{"timestamp": (0.0, 1.0), "text": "timed"}]}
            )
        )


def _pipeline_config(**overrides: object) -> SimpleNamespace:
    config = dict(
        whisper_model="kotoba-tech/kotoba-whisper-v2.1",
        whisper_device="cuda",
        whisper_language="ja",
        whisper_task="transcribe",
        whisper_no_repeat_ngram_size=0,
        whisper_repetition_penalty=1.0,
        transformers_whisper_batch_size=16,
        transformers_whisper_torch_dtype="float16",
        transformers_whisper_trust_remote_code=True,
        transformers_whisper_punctuator=False,
        transformers_whisper_attn_implementation="sdpa",
        transformers_whisper_task=None,
        transformers_whisper_stable_ts=False,
    )
    config.update(overrides)
    return SimpleNamespace(**config)


if __name__ == "__main__":
    unittest.main()
