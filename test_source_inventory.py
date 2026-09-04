from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from source_analyzer import analyze_sources
from source_inventory import (
    SourceChangedError,
    SourceInventoryError,
    SourceProbeError,
    build_source_input_identity,
    discover_source_sidecars,
    inventory_sources,
    materialize_selected_subtitle,
)


class SourceInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "Episode 01.mkv"
        self.video.write_bytes(b"immutable-video-source")
        bootstrap = build_source_input_identity(self.video, "job-test-1")
        self.job = {
            **bootstrap.media_job_identity,
            "state": "SUBTITLE_DETECTION",
            "updated_at": 99999,
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _sidecar(self, suffix: str, text: str) -> Path:
        path = self.video.with_name(f"{self.video.stem}{suffix}")
        path.write_text(text, encoding="utf-8")
        return path

    def test_fingerprint_is_insensitive_to_sidecar_input_order(self) -> None:
        first = self._sidecar(".ja.srt", "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n")
        second = self._sidecar(".zh-TW.ass", "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\n")

        one = build_source_input_identity(self.video, self.job, sidecar_paths=[first, second])
        two = build_source_input_identity(self.video, dict(reversed(tuple(self.job.items()))), sidecar_paths=[second, first])

        self.assertEqual(one.fingerprint, two.fingerprint)
        self.assertEqual(
            ["Episode 01.ja.srt", "Episode 01.zh-TW.ass"],
            [item.relative_path for item in one.sidecars],
        )

    def test_sidecar_content_change_invalidates_even_with_same_size_and_mtime(self) -> None:
        sidecar = self._sidecar(".ja.srt", "1\n00:00:01,000 --> 00:00:02,000\nAAAA\n")
        before_stat = sidecar.stat()
        before = build_source_input_identity(self.video, self.job, sidecar_paths=[sidecar])

        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nBBBB\n", encoding="utf-8")
        os.utime(sidecar, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
        after = build_source_input_identity(self.video, self.job, sidecar_paths=[sidecar])

        self.assertNotEqual(before.fingerprint, after.fingerprint)
        self.assertEqual(before.sidecars[0].size, after.sidecars[0].size)
        self.assertEqual(before.sidecars[0].mtime_ns, after.sidecars[0].mtime_ns)

    def test_pipeline_job_identity_cannot_be_rebound_to_another_same_stat_file(self) -> None:
        other = self.root / "Episode 02.mkv"
        other.write_bytes(self.video.read_bytes())
        source_stat = self.video.stat()
        os.utime(other, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))

        with self.assertRaises(SourceChangedError):
            build_source_input_identity(other, self.job)

    def test_pipeline_job_identity_allows_a_verified_hardlink_alias(self) -> None:
        alias = self.root / "Episode alias.mkv"
        try:
            os.link(self.video, alias)
        except OSError:
            self.skipTest("hard links unavailable")

        identity = build_source_input_identity(alias, self.job)

        self.assertEqual(self.job["media_fingerprint"], identity.media_job_identity["media_fingerprint"])
        self.assertEqual(str(alias.resolve()).casefold(), str(identity.media_job_identity["canonical_path"]).casefold())

    def test_explicit_nested_sidecar_is_rejected_by_inventory_contract(self) -> None:
        nested = self.root / "subs"
        nested.mkdir()
        sidecar = nested / f"{self.video.stem}.ja.srt"
        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")

        with self.assertRaises(SourceInventoryError):
            discover_source_sidecars(self.video, sidecar_paths=[sidecar])

    def test_generated_sidecars_are_excluded_from_identity(self) -> None:
        official = self._sidecar(".ja.ass", "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,こんにちは\n")
        generated = self._sidecar(".AI.zh-TW.ass", "generated-one")
        config = SimpleNamespace(
            ai_japanese_ass_suffix=".machine-ja.ass",
            ai_simplified_chinese_ass_suffix=".machine-zh.ass",
            ai_traditional_chinese_ass_suffix=".machine-zh-TW.ass",
            ai_source_transcript_ass_suffix_template=".Machine{label}.{language}.ass",
        )
        configured = self._sidecar(".machine-ja.ass", "configured-one")
        first = build_source_input_identity(self.video, self.job, config=config)

        generated.write_text("generated-two", encoding="utf-8")
        configured.write_text("configured-two", encoding="utf-8")
        second = build_source_input_identity(self.video, self.job, config=config)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual((official,), discover_source_sidecars(self.video, config=config))
        self.assertEqual([official.name], [item.relative_path for item in first.sidecars])

    def test_verified_converted_output_is_excluded_without_hiding_plain_user_sidecar(self) -> None:
        sidecar = self._sidecar(
            ".zh-TW.ass",
            "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,繁體字幕\n",
        )
        manifest = self.root / "manifest.json"
        config = SimpleNamespace()

        self.assertEqual((sidecar,), discover_source_sidecars(self.video, config=config))

        manifest.write_text(
            '{"publication":{"kind":"converted_zh_cn"},"provenance":{}}',
            encoding="utf-8",
        )
        with patch("output_manifest.validate_output_manifest", return_value=True), patch(
            "output_manifest.output_manifest_path", return_value=manifest
        ):
            self.assertEqual((), discover_source_sidecars(self.video, config=config))

    def test_verified_normalization_output_requires_explicit_m2_strategy(self) -> None:
        sidecar = self._sidecar(
            ".zh-TW.ass",
            "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,繁體字幕\n",
        )
        manifest = self.root / "manifest.json"
        config = SimpleNamespace()

        for strategy, expected in (
            ("USE_EXISTING_ZH_TW", (sidecar,)),
            ("NORMALIZE_ZH_HANT", ()),
        ):
            manifest.write_text(
                json.dumps(
                    {
                        "publication": {"kind": "adopted_zh_tw"},
                        "provenance": {"source_analysis": {"strategy": strategy}},
                    }
                ),
                encoding="utf-8",
            )
            with patch("output_manifest.validate_output_manifest", return_value=True), patch(
                "output_manifest.output_manifest_path", return_value=manifest
            ):
                self.assertEqual(expected, discover_source_sidecars(self.video, config=config))

    def test_ffprobe_error_is_fail_closed_without_candidates(self) -> None:
        before = self.video.stat()
        with patch("source_inventory._probe_media", side_effect=SourceProbeError("ffprobe_failed:test")):
            result = inventory_sources(self.video, self.job)
        after = self.video.stat()

        self.assertFalse(result.subtitle_inventory_complete)
        self.assertFalse(result.audio_inventory_complete)
        self.assertEqual((), result.subtitle_candidates)
        self.assertEqual((), result.audio_candidates)
        self.assertIn("ffprobe_failed:test", result.probing_errors)
        self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_inventory_extracts_only_to_temp_and_preserves_all_sources(self) -> None:
        sidecar = self._sidecar(
            ".zh-TW.srt",
            "1\n00:00:01,000 --> 00:00:03,000\n這是繁體字幕\n\n",
        )
        video_before = self.video.stat()
        sidecar_before = sidecar.stat()
        temp_root = self.root / "inventory-temp"
        probe = {
            "format": {"duration": "60.0"},
            "streams": [
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "ass",
                    "tags": {"language": "jpn", "title": "Japanese Dialogue"},
                    "disposition": {"default": 1, "forced": 0, "hearing_impaired": 0},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": "48000",
                    "duration": "60.0",
                    "tags": {"language": "jpn", "title": "Japanese"},
                    "disposition": {"default": 1, "comment": 0},
                },
            ],
        }

        def fake_extract(_video, _index, output, **_kwargs):
            lines = ["[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
            for index in range(25):
                lines.append(
                    f"Dialogue: 0,0:00:{index:02d}.00,0:00:{index + 1:02d}.00,Default,,0,0,0,,こんにちは{index}"
                )
            output.write_text("\n".join(lines), encoding="utf-8")

        with patch("source_inventory._probe_media", return_value=probe), patch(
            "source_inventory._extract_embedded_subtitle", side_effect=fake_extract
        ):
            result = inventory_sources(self.video, self.job, temp_root=temp_root)

        video_after = self.video.stat()
        sidecar_after = sidecar.stat()
        self.assertTrue(result.subtitle_inventory_complete)
        self.assertTrue(result.audio_inventory_complete)
        self.assertEqual(2, len(result.subtitle_candidates))
        embedded = next(item for item in result.subtitle_candidates if item.source_kind == "embedded")
        self.assertEqual(25, embedded.event_count)
        self.assertEqual(25, embedded.valid_timing_count)
        self.assertIn("こんにちは", embedded.sample_text)
        self.assertRegex(embedded.content_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(48_000, result.audio_candidates[0].sample_rate)
        self.assertEqual("container_metadata_only", result.audio_candidates[0].detection_source)
        self.assertEqual([], list(temp_root.iterdir()))
        self.assertEqual((video_before.st_size, video_before.st_mtime_ns), (video_after.st_size, video_after.st_mtime_ns))
        self.assertEqual((sidecar_before.st_size, sidecar_before.st_mtime_ns), (sidecar_after.st_size, sidecar_after.st_mtime_ns))
        analyzer = result.analyzer_arguments()
        self.assertEqual(result.media_duration_seconds, analyzer["media_duration_seconds"])
        self.assertEqual(2, len(analyzer["subtitle_candidates"]))
        self.assertEqual(analyzer, result.to_analyzer_arguments())

    def test_semantic_content_hash_matches_across_srt_and_ass(self) -> None:
        self._sidecar(
            ".ja.srt",
            "1\n00:00:01,000 --> 00:00:02,500\nこんにちは\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nまたね\n",
        )
        probe = {
            "format": {"duration": "25.0"},
            "streams": [
                {
                    "index": 4,
                    "codec_type": "subtitle",
                    "codec_name": "ass",
                    "tags": {"language": "jpn"},
                    "disposition": {},
                }
            ],
        }

        def fake_extract(_video, _index, output, **_kwargs):
            output.write_text(
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,こんにちは\n"
                "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,またね\n",
                encoding="utf-8",
            )

        with patch("source_inventory._probe_media", return_value=probe), patch(
            "source_inventory._extract_embedded_subtitle", side_effect=fake_extract
        ):
            result = inventory_sources(self.video, self.job)

        hashes = {candidate.content_sha256 for candidate in result.subtitle_candidates}
        self.assertEqual(1, len(hashes))
        self.assertNotEqual({""}, hashes)

    def test_materialize_sidecar_revalidates_and_returns_read_only_source(self) -> None:
        sidecar = self._sidecar(
            ".ja.srt",
            "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n",
        )
        probe = {"format": {"duration": "10.0"}, "streams": []}
        with patch("source_inventory._probe_media", return_value=probe):
            inventory = inventory_sources(self.video, self.job)
        candidate = inventory.subtitle_candidates[0].to_analyzer_dict()
        video_before = (self.video.read_bytes(), self.video.stat().st_size, self.video.stat().st_mtime_ns)
        sidecar_before = (sidecar.read_bytes(), sidecar.stat().st_size, sidecar.stat().st_mtime_ns)

        result = materialize_selected_subtitle(
            self.video,
            candidate,
            self.job,
            None,
            expected_candidate_fingerprint=inventory.candidate_fingerprint,
        )

        self.assertEqual(sidecar.resolve(), result.resolve())
        self.assertEqual(video_before, (self.video.read_bytes(), self.video.stat().st_size, self.video.stat().st_mtime_ns))
        self.assertEqual(sidecar_before, (sidecar.read_bytes(), sidecar.stat().st_size, sidecar.stat().st_mtime_ns))

    def test_materialize_sidecar_rejects_changed_content_and_path_traversal(self) -> None:
        sidecar = self._sidecar(
            ".ja.srt",
            "1\n00:00:01,000 --> 00:00:02,000\nAAAA\n\n",
        )
        probe = {"format": {"duration": "10.0"}, "streams": []}
        with patch("source_inventory._probe_media", return_value=probe):
            inventory = inventory_sources(self.video, self.job)
        candidate = inventory.subtitle_candidates[0].to_analyzer_dict()
        original_stat = sidecar.stat()
        sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nBBBB\n\n", encoding="utf-8")
        os.utime(sidecar, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        with self.assertRaises(SourceChangedError):
            materialize_selected_subtitle(self.video, candidate, self.job, None)

        traversal = dict(candidate)
        traversal["source_reference"] = "../escape.srt"
        with self.assertRaises(SourceInventoryError):
            materialize_selected_subtitle(self.video, traversal, self.job, None)

    def test_materialize_embedded_publishes_stable_cache_and_reuses_it(self) -> None:
        probe = {
            "format": {"duration": "25.0"},
            "streams": [
                {
                    "index": 7,
                    "codec_type": "subtitle",
                    "codec_name": "ass",
                    "tags": {"language": "jpn"},
                    "disposition": {"default": 1},
                }
            ],
        }

        def fake_extract(_video, _index, output, **_kwargs):
            lines = [
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            ]
            for index in range(25):
                lines.append(
                    f"Dialogue: 0,0:00:{index:02d}.00,0:00:{index + 1:02d}.00,Default,,0,0,0,,こんにちは{index}"
                )
            output.write_text("\n".join(lines), encoding="utf-8")

        with patch("source_inventory._probe_media", return_value=probe), patch(
            "source_inventory._extract_embedded_subtitle", side_effect=fake_extract
        ):
            inventory = inventory_sources(self.video, self.job)
        decision = analyze_sources(**inventory.analyzer_arguments()).to_dict()
        candidate = decision["selected_subtitle_track"]
        self.assertIsNotNone(candidate)
        work_path = self.root / "work"
        config = SimpleNamespace(work_path=work_path)
        source_before = (self.video.read_bytes(), self.video.stat().st_size, self.video.stat().st_mtime_ns)

        with patch("source_inventory._extract_embedded_subtitle", side_effect=fake_extract) as extract:
            first = materialize_selected_subtitle(
                self.video,
                candidate,
                self.job,
                config,
                expected_candidate_fingerprint=inventory.candidate_fingerprint,
            )
        self.assertEqual(1, extract.call_count)
        self.assertTrue(first.is_file())
        self.assertTrue(first.resolve().is_relative_to(work_path.resolve()))
        self.assertFalse(first.name.startswith(".source-inventory-"))

        with patch(
            "source_inventory._extract_embedded_subtitle",
            side_effect=AssertionError("valid cache must be reused"),
        ) as extract_again:
            second = materialize_selected_subtitle(
                self.video,
                candidate,
                self.job,
                config,
                expected_candidate_fingerprint=inventory.candidate_fingerprint,
            )
        self.assertEqual(first, second)
        self.assertEqual(0, extract_again.call_count)
        self.assertEqual([], list(work_path.rglob("*.partial.ass")))
        self.assertEqual(source_before, (self.video.read_bytes(), self.video.stat().st_size, self.video.stat().st_mtime_ns))

    def test_module_has_no_speech_model_import(self) -> None:
        module_path = Path(__file__).with_name("source_inventory.py")
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertTrue({"language_detector", "transcriber", "faster_whisper"}.isdisjoint(imported))


if __name__ == "__main__":
    unittest.main()
