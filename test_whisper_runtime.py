from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import whisper_runtime


class WhisperRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        whisper_runtime.clear_whisper_model_cache()

    def test_reuses_matching_model_and_replaces_it_for_fallback(self) -> None:
        created: list[tuple[str, str, str]] = []

        class FakeWhisperModel:
            def __init__(self, model: str, *, device: str, compute_type: str) -> None:
                created.append((model, device, compute_type))

        fake_module = types.SimpleNamespace(WhisperModel=FakeWhisperModel)
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            first = whisper_runtime.get_whisper_model("large-v3", device="cuda", compute_type="float16")
            reused = whisper_runtime.get_whisper_model("large-v3", device="cuda", compute_type="float16")
            fallback = whisper_runtime.get_whisper_model("large-v2", device="cuda", compute_type="float16")

        self.assertIs(first, reused)
        self.assertIsNot(first, fallback)
        self.assertEqual(created, [("large-v3", "cuda", "float16"), ("large-v2", "cuda", "float16")])
        self.assertEqual(whisper_runtime.whisper_model_cache_info()["model"], "large-v2")

    def test_cache_can_be_disabled(self) -> None:
        class FakeWhisperModel:
            def __init__(self, model: str, *, device: str, compute_type: str) -> None:
                self.model = model

        fake_module = types.SimpleNamespace(WhisperModel=FakeWhisperModel)
        with patch.dict(sys.modules, {"faster_whisper": fake_module}):
            first = whisper_runtime.get_whisper_model(
                "large-v3", device="cuda", compute_type="float16", cache_enabled=False
            )
            second = whisper_runtime.get_whisper_model(
                "large-v3", device="cuda", compute_type="float16", cache_enabled=False
            )
        self.assertIsNot(first, second)
        self.assertFalse(whisper_runtime.whisper_model_cache_info()["loaded"])


if __name__ == "__main__":
    unittest.main()
