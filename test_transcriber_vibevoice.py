from __future__ import annotations

from types import SimpleNamespace
import unittest

from transcriber import TranscriptionError
from transcriber_vibevoice import VibeVoiceTranscriptionError, _result_to_blocks


CONFIG = SimpleNamespace(
    filter_repeated_vocalizations=True,
    repeated_vocalization_min_chars=6,
    subtitle_min_duration_seconds=0.8,
    vibevoice_prompt="Japanese anime dialogue.",
)


class VibeVoiceParserTest(unittest.TestCase):
    def test_backend_errors_participate_in_worker_transcription_fallback(self) -> None:
        self.assertTrue(issubclass(VibeVoiceTranscriptionError, TranscriptionError))

    def test_parses_structured_generated_text(self) -> None:
        result = {
            "generated_text": (
                'assistant\n[{"Start":0.0,"End":1.25,"Speaker":0,"Content":"こんにちは"},'
                '{"Start":1.5,"End":2.0,"Speaker":1,"Content":"テスト"}]'
            )
        }

        blocks = _result_to_blocks(result, CONFIG)

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].timing, "00:00:00,000 --> 00:00:01,250")
        self.assertEqual(blocks[0].text, ["こんにちは"])

    def test_parses_pipeline_chunks(self) -> None:
        result = {"chunks": [{"timestamp": (2.0, 3.5), "text": "字幕"}]}

        blocks = _result_to_blocks(result, CONFIG)

        self.assertEqual(blocks[0].timing, "00:00:02,000 --> 00:00:03,500")
        self.assertEqual(blocks[0].text, ["字幕"])


if __name__ == "__main__":
    unittest.main()
