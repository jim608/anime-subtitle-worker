from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from config import ConfigError, load_config
from source_analyzer import (
    ANALYZER_VERSION,
    DECISION_SCHEMA_VERSION,
    DECISION_VERSION,
    AnalyzerThresholds,
)


class SourceAnalyzerConfigTests(unittest.TestCase):
    def _write_config(self, root: Path, **overrides: object) -> Path:
        raw: dict[str, object] = {
            "input_path": str(root / "input"),
            "work_path": str(root / "work"),
            "log_path": str(root / "logs"),
            "video_extensions": [".mkv", ".mp4"],
            "whisper_model": "large-v3",
            "whisper_device": "cuda",
            "whisper_compute_type": "float16",
            "whisper_language": "ja",
            "whisper_task": "transcribe",
            "whisper_vad_filter": True,
            "whisper_condition_on_previous_text": False,
            "whisper_temperature": 0.0,
            "enable_vocal_separation": False,
            "vocal_separation_engine": "none",
            "vocal_separation_output": "vocals",
            "translator_base_url": "https://example.invalid/v1",
            "translator_api_key": "TEST_PLACEHOLDER",
            "translator_model": "test-model",
            "translator_timeout_seconds": 120,
            "batch_size": 10,
            "max_retries": 3,
            "watch_interval_seconds": 300,
            "opencc_config": "s2twp.json",
            "keep_intermediate_files": False,
        }
        raw.update(overrides)
        path = root / "config.yaml"
        path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def test_old_config_without_m2_fields_loads_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(self._write_config(Path(temp_dir)))

        self.assertFalse(config.source_analyzer_enabled)
        self.assertEqual(config.source_analyzer_high_confidence, 0.90)
        self.assertEqual(config.source_analyzer_low_confidence, 0.60)
        self.assertEqual(config.source_analyzer_min_dialogue_completeness_score, 0.68)
        self.assertEqual(config.source_analyzer_min_subtitle_coverage_ratio, 0.60)
        self.assertEqual(config.source_analyzer_tie_margin, 0.025)
        self.assertEqual(config.source_analyzer_version, ANALYZER_VERSION)
        self.assertEqual(config.source_decision_schema_version, DECISION_SCHEMA_VERSION)
        self.assertEqual(config.source_decision_version, DECISION_VERSION)

    def test_explicit_m2_settings_load_and_convert_to_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = load_config(
                self._write_config(
                    root,
                    source_analyzer_enabled=True,
                    pipeline_job_store_required=True,
                    source_analyzer_high_confidence=0.96,
                    source_analyzer_low_confidence=0.70,
                    source_analyzer_min_dialogue_completeness_score=0.77,
                    source_analyzer_min_subtitle_coverage_ratio=0.72,
                    source_analyzer_tie_margin=0.015,
                    source_analyzer_version="fixture-analyzer-v2",
                    source_decision_schema_version=2,
                    source_decision_version="fixture-decision-v2",
                )
            )

        self.assertTrue(config.source_analyzer_enabled)
        self.assertEqual(config.source_analyzer_version, "fixture-analyzer-v2")
        self.assertEqual(config.source_decision_schema_version, 2)
        self.assertEqual(config.source_decision_version, "fixture-decision-v2")
        thresholds = config.source_analyzer_thresholds()
        self.assertIsInstance(thresholds, AnalyzerThresholds)
        self.assertEqual(thresholds.auto_accept_confidence, 0.96)
        self.assertEqual(thresholds.review_confidence, 0.70)
        self.assertEqual(thresholds.min_dialogue_completeness_score, 0.77)
        self.assertEqual(thresholds.min_subtitle_coverage_ratio, 0.72)
        self.assertEqual(thresholds.close_candidate_score_margin, 0.015)

    def test_zero_tie_margin_still_builds_valid_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_config(
                self._write_config(
                    Path(temp_dir),
                    source_analyzer_tie_margin=0.0,
                )
            )

        thresholds = config.source_analyzer_thresholds()
        self.assertEqual(thresholds.close_candidate_score_margin, 0.0)
        self.assertEqual(thresholds.exact_tie_score_epsilon, 0.0)

    def test_confidence_order_must_be_strict(self) -> None:
        cases = (
            {"source_analyzer_low_confidence": 0.90, "source_analyzer_high_confidence": 0.90},
            {"source_analyzer_low_confidence": 0.91, "source_analyzer_high_confidence": 0.90},
        )
        for index, values in enumerate(cases):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temp_dir:
                path = self._write_config(Path(temp_dir), **values)
                with self.assertRaisesRegex(ConfigError, "0 <= source_analyzer_low_confidence"):
                    load_config(path)

    def test_probability_fields_reject_out_of_range_nonfinite_and_boolean(self) -> None:
        cases = (
            ("source_analyzer_high_confidence", 1.01),
            ("source_analyzer_low_confidence", -0.01),
            ("source_analyzer_min_dialogue_completeness_score", float("inf")),
            ("source_analyzer_min_subtitle_coverage_ratio", -0.1),
            ("source_analyzer_tie_margin", True),
        )
        for field_name, value in cases:
            with self.subTest(field=field_name), tempfile.TemporaryDirectory() as temp_dir:
                path = self._write_config(Path(temp_dir), **{field_name: value})
                with self.assertRaisesRegex(ConfigError, "finite number between 0 and 1"):
                    load_config(path)

    def test_enabled_analyzer_requires_durable_job_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_config(
                Path(temp_dir),
                source_analyzer_enabled=True,
                pipeline_job_store_required=False,
            )
            with self.assertRaisesRegex(ConfigError, "requires pipeline_job_store_required"):
                load_config(path)

    def test_version_identity_must_be_nonempty_and_schema_positive_integer(self) -> None:
        cases = (
            {"source_analyzer_version": ""},
            {"source_decision_version": ""},
            {"source_decision_schema_version": 0},
            {"source_decision_schema_version": True},
        )
        for values in cases:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temp_dir:
                path = self._write_config(Path(temp_dir), **values)
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_example_is_a_sanitized_m2_fragment(self) -> None:
        path = Path("config.example.yaml")
        raw_text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(raw_text)

        self.assertIsInstance(raw, dict)
        self.assertFalse(raw["source_analyzer_enabled"])
        self.assertEqual(raw["source_analyzer_version"], ANALYZER_VERSION)
        self.assertEqual(raw["source_decision_schema_version"], DECISION_SCHEMA_VERSION)
        self.assertEqual(raw["source_decision_version"], DECISION_VERSION)
        self.assertNotIn("input_path", raw)
        self.assertNotIn("translator_api_key", raw)
        self.assertNotIn("http://", raw_text)
        self.assertNotIn("https://", raw_text)


if __name__ == "__main__":
    unittest.main()
