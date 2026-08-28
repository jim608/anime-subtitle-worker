from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from language_detector import (
    LanguageDetectionResult,
    LanguageDetectionSample,
    LanguageDetector,
    _aggregate_samples,
    _sample_centers,
    format_language_skip,
    should_fail_for_language,
    should_skip_for_language,
)


class LanguageDetectorTests(unittest.TestCase):
    def config(self, **overrides: object) -> SimpleNamespace:
        values = {
            "allowed_source_languages": ["ja"],
            "language_detect_min_probability": 0.70,
            "language_uncertain_policy": "skip",
            "skip_non_allowed_language": True,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_aggregate_allows_japanese_only_when_allowed_samples_win(self) -> None:
        result = _aggregate_samples(
            [
                LanguageDetectionSample("ja", 0.82, 0, 30),
                LanguageDetectionSample("unknown", 0.20, 120, 30),
                LanguageDetectionSample("zh", 0.42, 240, 30),
            ],
            self.config(),
        )

        self.assertTrue(result.allowed)
        self.assertTrue(result.confident)
        self.assertEqual(result.language, "ja")
        self.assertEqual(result.reason, "allowed_language_detected")

    def test_aggregate_blocks_when_non_allowed_confident_samples_win(self) -> None:
        result = _aggregate_samples(
            [
                LanguageDetectionSample("ja", 0.75, 0, 30),
                LanguageDetectionSample("en", 0.91, 120, 30),
                LanguageDetectionSample("en", 0.88, 240, 30),
            ],
            self.config(),
        )

        self.assertFalse(result.allowed)
        self.assertTrue(result.confident)
        self.assertEqual(result.language, "en")
        self.assertEqual(result.reason, "non_allowed_language_detected")

    def test_aggregate_marks_low_or_tied_confidence_uncertain(self) -> None:
        low = _aggregate_samples(
            [
                LanguageDetectionSample("ja", 0.62, 0, 30),
                LanguageDetectionSample("en", 0.65, 120, 30),
            ],
            self.config(),
        )
        tied = _aggregate_samples(
            [
                LanguageDetectionSample("ja", 0.78, 0, 30),
                LanguageDetectionSample("en", 0.81, 120, 30),
            ],
            self.config(),
        )

        self.assertEqual(low.reason, "language_uncertain")
        self.assertFalse(low.confident)
        self.assertEqual(tied.reason, "language_uncertain")
        self.assertFalse(tied.confident)

    def test_uncertain_policy_controls_skip_or_fail(self) -> None:
        result = LanguageDetectionResult("la", 0.62, False, False, "test", reason="language_uncertain")

        self.assertTrue(should_skip_for_language(result, self.config(language_uncertain_policy="skip")))
        self.assertFalse(should_skip_for_language(result, self.config(language_uncertain_policy="continue")))
        self.assertTrue(should_fail_for_language(result, self.config(language_uncertain_policy="fail")))
        self.assertFalse(should_fail_for_language(result, self.config(language_uncertain_policy="fail", skip_non_allowed_language=False)))

    def test_format_skip_includes_samples_for_webui_diagnostics(self) -> None:
        result = LanguageDetectionResult(
            "la",
            0.62,
            False,
            False,
            "test",
            reason="language_uncertain",
            samples=[LanguageDetectionSample("la", 0.62, 42, 30)],
        )

        message = format_language_skip(result, self.config())

        self.assertIn("reason=language_uncertain", message)
        self.assertIn("samples=la:0.62@42s", message)

    def test_sample_centers_scale_with_requested_count(self) -> None:
        centers = _sample_centers(100.0, 4)

        self.assertEqual(len(centers), 4)
        self.assertEqual(centers, [20.0, 40.0, 60.0, 80.0])

    def test_cache_variant_keeps_audio_stream_results_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            config = self.config(
                work_path=root,
                language_detect_cache_enabled=True,
                language_detect_cache_path="language.json",
                language_detect_model="large-v3",
                whisper_model="large-v3",
                whisper_device="cuda",
                language_detect_sample_count=3,
                language_detect_sample_seconds=30,
            )
            detector = LanguageDetector(config, __import__("logging").getLogger("test.language.cache"))
            result = LanguageDetectionResult("ja", 0.95, True, True, "test", reason="allowed_language_detected")
            detector._write_cached(video, result, "stream:1")

            self.assertEqual(detector._read_cached(video, "stream:1"), result)
            self.assertIsNone(detector._read_cached(video, "stream:2"))


if __name__ == "__main__":
    unittest.main()
