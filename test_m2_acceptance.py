from __future__ import annotations

import ast
import builtins
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_support import configure_isolated_test_tempdir

configure_isolated_test_tempdir()

from pipeline_state import PipelineJobStore
from source_analysis_service import (
    CandidateInventory,
    SourceAnalysisContext,
    run_source_analysis,
)
from source_analyzer import (
    ANALYZER_VERSION,
    ASR_JA_AUDIO,
    CONVERT_ZH_CN,
    DECISION_SCHEMA_VERSION,
    DECISION_VERSION,
    NEEDS_REVIEW,
    TRANSLATE_JA_SUBTITLE,
    UNSUPPORTED,
    USE_EXISTING_ZH_TW,
    analyze_sources,
    decision_sha256,
    normalize_language_tag,
)
from source_inventory import build_source_input_identity
from test_source_analyzer import (
    JAPANESE_TEXT,
    MEDIA_DURATION,
    SIMPLIFIED_TEXT,
    TRADITIONAL_TEXT,
    audio,
    subtitle,
)


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class M2AcceptanceTest(unittest.TestCase):
    """Executable acceptance mapping for docs/PROJECT_GOAL.md requirements 1-22."""

    def decide(self, subtitles=(), audios=(), **kwargs):
        return analyze_sources(
            subtitles,
            audios,
            media_duration_seconds=MEDIA_DURATION,
            **kwargs,
        )

    @staticmethod
    def _new_job(root: Path) -> tuple[PipelineJobStore, Path, dict]:
        media = root / "episode.mkv"
        media.write_bytes(b"immutable-m2-acceptance-video")
        store = PipelineJobStore(root / "pipeline.sqlite3")
        stat = media.stat()
        store.observe_ingest(
            media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type="created",
            state="STABILIZING",
            evidence={"acceptance": "m2"},
            confidence=1.0,
        )
        observed = store.observe_ingest(
            media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type="closed",
            state="QUEUED",
            evidence={"acceptance": "m2"},
            confidence=1.0,
        )
        job = store.get_job(str(observed["job_id"]))
        if job is None:
            raise AssertionError("M2 acceptance fixture did not create a durable job")
        return store, media, job

    @staticmethod
    def _start_attempt(store: PipelineJobStore, job: dict) -> dict:
        return store.start_stage_attempt(
            str(job["job_id"]),
            "SUBTITLE_DETECTION",
            inputs={"contract": "m2-acceptance"},
            retry_limit=2,
            reason_code="m2_acceptance_started",
            evidence={"acceptance": "m2"},
            confidence=1.0,
        )

    @staticmethod
    def _context(
        media: Path,
        job: dict,
        attempt: dict,
        *,
        candidate_fingerprint: str | None = None,
        sidecar_paths: tuple[Path, ...] = (),
    ) -> SourceAnalysisContext:
        identity = build_source_input_identity(media, job, sidecar_paths=sidecar_paths)
        return SourceAnalysisContext(
            job_id=str(job["job_id"]),
            stage_attempt_id=str(attempt["stage_attempt_id"]),
            input_identity=identity.to_dict(),
            media_revision=str(job["media_revision"]),
            source_fingerprint=str(job["media_fingerprint"]),
            config_fingerprint=_sha256("m2-acceptance-config-v1"),
            cheap_candidate_fingerprint=(
                candidate_fingerprint or identity.candidate_fingerprint
            ),
            analyzer_version=ANALYZER_VERSION,
            decision_schema_version=DECISION_SCHEMA_VERSION,
            decision_version=DECISION_VERSION,
        )

    @staticmethod
    def _inventory() -> CandidateInventory:
        return CandidateInventory(
            subtitle_candidates=(
                subtitle(3, TRADITIONAL_TEXT, "zh-TW"),
                subtitle(4, JAPANESE_TEXT, "ja"),
            ),
            audio_candidates=(),
            media_duration_seconds=MEDIA_DURATION,
            subtitle_inventory_complete=True,
            audio_inventory_complete=True,
        )

    @staticmethod
    def _finish(store: PipelineJobStore, attempt: dict) -> None:
        store.finish_stage_attempt(
            str(attempt["stage_attempt_id"]),
            "SUCCEEDED",
            reason_code="m2_acceptance_succeeded",
            evidence={"acceptance": "m2"},
            confidence=1.0,
        )

    def _run_named_suite(self, names: tuple[str, ...], minimum: int) -> None:
        suite = unittest.TestSuite(
            unittest.defaultTestLoader.loadTestsFromName(name) for name in names
        )
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        self.assertGreaterEqual(result.testsRun, minimum)
        self.assertTrue(result.wasSuccessful(), stream.getvalue())

    def test_01_complete_zh_tw_selects_existing(self) -> None:
        decision = self.decide([subtitle(1, TRADITIONAL_TEXT, "zh-TW")])
        self.assertEqual(USE_EXISTING_ZH_TW, decision.strategy)
        self.assertEqual(1, decision.selected_subtitle_track)
        self.assertGreaterEqual(decision.confidence, 0.90)

    def test_02_complete_zh_cn_selects_conversion(self) -> None:
        decision = self.decide([subtitle(2, SIMPLIFIED_TEXT, "zh-CN")])
        self.assertEqual(CONVERT_ZH_CN, decision.strategy)
        self.assertEqual(2, decision.selected_subtitle_track)

    def test_03_complete_japanese_subtitle_selects_translation(self) -> None:
        decision = self.decide([subtitle(3, JAPANESE_TEXT, "jpn")])
        self.assertEqual(TRANSLATE_JA_SUBTITLE, decision.strategy)
        self.assertEqual(3, decision.selected_subtitle_track)

    def test_04_japanese_audio_without_subtitle_selects_asr(self) -> None:
        decision = self.decide([], [audio(4, "jpn")])
        self.assertEqual(ASR_JA_AUDIO, decision.strategy)
        self.assertEqual(4, decision.selected_audio_track)
        self.assertFalse(decision.evidence["asr_invoked"])

    def test_05_zh_tw_precedes_japanese_subtitle(self) -> None:
        decision = self.decide(
            [
                subtitle(5, JAPANESE_TEXT, "ja", default=True),
                subtitle(6, TRADITIONAL_TEXT, "zh-TW"),
            ]
        )
        self.assertEqual(USE_EXISTING_ZH_TW, decision.strategy)
        self.assertEqual(6, decision.selected_subtitle_track)

    def test_06_zh_cn_precedes_japanese_subtitle(self) -> None:
        decision = self.decide(
            [
                subtitle(7, JAPANESE_TEXT, "ja", default=True),
                subtitle(8, SIMPLIFIED_TEXT, "zh-CN"),
            ]
        )
        self.assertEqual(CONVERT_ZH_CN, decision.strategy)
        self.assertEqual(8, decision.selected_subtitle_track)

    def test_07_forced_chinese_cannot_override_complete_japanese(self) -> None:
        decision = self.decide(
            [
                subtitle(9, TRADITIONAL_TEXT, "zh-TW", forced=True, default=True),
                subtitle(10, JAPANESE_TEXT, "ja"),
            ]
        )
        self.assertEqual(TRANSLATE_JA_SUBTITLE, decision.strategy)
        forced = next(item for item in decision.candidates if item.index == 9)
        self.assertIn("forced_track_risk", forced.rejection_reasons)

    def test_08_signs_only_is_not_complete_chinese(self) -> None:
        signs = subtitle(
            11,
            "出口 入口 注意 危險",
            "zh-TW",
            title="Signs & Songs",
            event_count=7,
            valid_timing_count=7,
            first_timestamp_seconds=300.0,
            last_timestamp_seconds=900.0,
        )
        decision = self.decide([signs], [audio(11, "ja")])
        self.assertEqual(ASR_JA_AUDIO, decision.strategy)
        analyzed = next(item for item in decision.candidates if item.kind == "subtitle")
        self.assertIn("signs_only_risk", analyzed.rejection_reasons)

    def test_09_default_flag_cannot_beat_complete_subtitle(self) -> None:
        weak_default = subtitle(
            12,
            TRADITIONAL_TEXT,
            "zh-TW",
            default=True,
            event_count=25,
            valid_timing_count=25,
            first_timestamp_seconds=500.0,
            last_timestamp_seconds=1_400.0,
        )
        complete = subtitle(13, TRADITIONAL_TEXT, "zh-TW")
        decision = self.decide([weak_default, complete])
        self.assertEqual(13, decision.selected_subtitle_track)

    def test_10_metadata_content_language_conflict_is_detected(self) -> None:
        decision = self.decide([subtitle(14, JAPANESE_TEXT, "zh-CN")])
        self.assertEqual(TRANSLATE_JA_SUBTITLE, decision.strategy)
        self.assertEqual("ja", decision.candidates[0].detected_language)
        self.assertTrue(decision.candidates[0].evidence["metadata_content_conflict"])

    def test_11_missing_metadata_uses_content_language(self) -> None:
        decision = self.decide([subtitle(15, TRADITIONAL_TEXT, "und")])
        self.assertEqual("zh-hant", decision.candidates[0].detected_language)
        self.assertEqual("NORMALIZE_ZH_HANT", decision.strategy)

    def test_12_language_tags_and_chinese_variants_are_normalized(self) -> None:
        expected = {
            "zh-TW": "zh-tw",
            "zh_Hant": "zh-hant",
            "cht": "zh-hant",
            "zh-CN": "zh-cn",
            "zh_Hans": "zh-hans",
            "chs": "zh-hans",
            "chi": "zh",
            "zho": "zh",
            "ja": "ja",
            "jpn": "ja",
            "unknown": "und",
            "": "und",
        }
        self.assertEqual(
            expected,
            {tag: normalize_language_tag(tag) for tag in expected},
        )
        self.assertEqual(
            "NORMALIZE_ZH_HANT",
            self.decide([subtitle(16, TRADITIONAL_TEXT, "zho")]).strategy,
        )
        self.assertEqual(
            CONVERT_ZH_CN,
            self.decide([subtitle(17, SIMPLIFIED_TEXT, "chi")]).strategy,
        )

    def test_13_close_candidates_execute_additional_checks(self) -> None:
        decision = self.decide(
            [
                subtitle(18, TRADITIONAL_TEXT, "zh-TW"),
                subtitle(19, TRADITIONAL_TEXT, "zh-TW"),
            ]
        )
        self.assertEqual(NEEDS_REVIEW, decision.strategy)
        self.assertTrue(decision.evidence["additional_checks"]["required"])
        self.assertEqual("insufficient", decision.evidence["additional_checks"]["result"])
        self.assertEqual("subtitle_selection_ambiguous", decision.reason_code)
        self.assertIsNone(decision.selected_subtitle_track)

    def test_14_low_confidence_source_needs_review(self) -> None:
        decision = self.decide([], [audio(20, "und", title="")])
        self.assertEqual(NEEDS_REVIEW, decision.strategy)
        self.assertLess(decision.confidence, 0.60)
        self.assertEqual("candidate_analysis_inconclusive", decision.reason_code)

    def test_15_no_supported_source_is_unsupported(self) -> None:
        decision = self.decide(
            [subtitle(21, "Complete English dialogue subtitle", "eng")],
            [audio(21, "eng", title="English")],
        )
        self.assertEqual(UNSUPPORTED, decision.strategy)
        self.assertEqual("no_supported_subtitle_or_audio_source", decision.reason_code)

    def test_16_decision_record_persists_candidates_reasons_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store, media, job = self._new_job(root)
            try:
                attempt = self._start_attempt(store, job)
                context = self._context(media, job, attempt)
                result = run_source_analysis(store, context, self._inventory)
                expected_decision = result.to_dict()["decision"]
                store.commit()
                store.close()
                store = PipelineJobStore(root / "pipeline.sqlite3")
                records = store.list_source_decisions(context.job_id)
                self.assertEqual(1, len(records))
                record = records[0]
                payload = record["decision"]
                self.assertEqual(expected_decision, payload)
                for field in (
                    "job_id",
                    "decision_version",
                    "strategy",
                    "selected_subtitle_track",
                    "selected_audio_track",
                    "confidence",
                    "reason_code",
                    "evidence",
                    "candidates",
                    "unselected_reasons",
                    "created_at",
                    "analyzer_version",
                    "input_identity",
                    "source_fingerprint",
                ):
                    self.assertIn(field, payload)
                self.assertTrue(payload["evidence"])
                self.assertTrue(payload["unselected_reasons"])
                self.assertTrue(
                    all(
                        "score" in item
                        and "evidence" in item
                        and "rejection_reasons" in item
                        for item in payload["candidates"]
                    )
                )
                bound = store.list_stage_attempts(context.job_id, "SUBTITLE_DETECTION")[-1]
                self.assertEqual(result.decision_id, bound["checkpoint"]["decision_id"])
                self.assertTrue(bound["outputs_verified"])
            finally:
                store.close()

    def test_17_restart_reuses_valid_decision_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            store, media, job = self._new_job(root)
            try:
                first_attempt = self._start_attempt(store, job)
                first_context = self._context(media, job, first_attempt)
                first = run_source_analysis(store, first_context, self._inventory)
                self._finish(store, first_attempt)
                store.commit()
                store.close()

                store = PipelineJobStore(root / "pipeline.sqlite3")
                second_attempt = self._start_attempt(store, job)
                second_context = replace(
                    first_context,
                    stage_attempt_id=str(second_attempt["stage_attempt_id"]),
                )
                reused = run_source_analysis(
                    store,
                    second_context,
                    lambda: self.fail("restart reuse must not reload candidates"),
                )
                self.assertTrue(reused.reused)
                self.assertFalse(reused.candidate_loader_called)
                self.assertEqual(first.decision_id, reused.decision_id)
                self.assertEqual(1, len(store.list_source_decisions(first_context.job_id)))
                rebound = next(
                    item
                    for item in store.list_stage_attempts(
                        first_context.job_id, "SUBTITLE_DETECTION"
                    )
                    if item["stage_attempt_id"] == second_attempt["stage_attempt_id"]
                )
                self.assertEqual(first.decision_id, rebound["checkpoint"]["decision_id"])
                self.assertTrue(rebound["outputs_verified"])
            finally:
                store.close()

    def test_18_changed_real_sidecar_fingerprint_invalidates_old_decision(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            store, media, job = self._new_job(Path(raw_root))
            try:
                sidecar = media.with_name(f"{media.stem}.ja.srt")
                sidecar.write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n",
                    encoding="utf-8",
                )
                original_stat = sidecar.stat()
                first_attempt = self._start_attempt(store, job)
                first_context = self._context(
                    media,
                    job,
                    first_attempt,
                    sidecar_paths=(sidecar,),
                )
                first = run_source_analysis(store, first_context, self._inventory)
                self._finish(store, first_attempt)

                sidecar.write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nさようなら\n",
                    encoding="utf-8",
                )
                self.assertEqual(original_stat.st_size, sidecar.stat().st_size)
                os.utime(
                    sidecar,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                second_attempt = self._start_attempt(store, job)
                changed_context = self._context(
                    media,
                    job,
                    second_attempt,
                    sidecar_paths=(sidecar,),
                )
                self.assertNotEqual(
                    first_context.cheap_candidate_fingerprint,
                    changed_context.cheap_candidate_fingerprint,
                )
                calls: list[str] = []

                def reload_inventory() -> CandidateInventory:
                    calls.append("loaded")
                    return self._inventory()

                changed = run_source_analysis(store, changed_context, reload_inventory)
                self.assertFalse(changed.reused)
                self.assertEqual(
                    "source_decision_input_identity_changed",
                    changed.reuse_reason,
                )
                self.assertEqual(["loaded"], calls)
                self.assertNotEqual(first.decision_id, changed.decision_id)
                self.assertEqual(2, len(store.list_source_decisions(first_context.job_id)))
            finally:
                store.close()

    def test_19_same_input_is_deterministic(self) -> None:
        candidates = [
            subtitle(22, JAPANESE_TEXT, "ja"),
            subtitle(23, TRADITIONAL_TEXT, "zh-TW"),
            audio(22, "jpn"),
        ]
        first = self.decide(candidates[:2], candidates[2:])
        second = self.decide(list(reversed(candidates[:2])), candidates[2:])
        self.assertEqual(first.strategy, second.strategy)
        self.assertEqual(first.reason_code, second.reason_code)
        self.assertEqual(
            [(item.kind, item.index, item.score) for item in first.candidates],
            [(item.kind, item.index, item.score) for item in second.candidates],
        )
        self.assertEqual(decision_sha256(first), decision_sha256(second))

    def test_20_decision_stage_never_imports_or_invokes_whisper(self) -> None:
        original_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if str(name).split(".", 1)[0] in {
                "faster_whisper",
                "transcriber",
                "language_detector",
            }:
                self.fail(f"decision stage imported forbidden model module: {name}")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=guarded_import):
            decision = self.decide([], [audio(24, "ja")])
        self.assertEqual(ASR_JA_AUDIO, decision.strategy)
        self.assertFalse(decision.evidence["asr_invoked"])
        forbidden = {"faster_whisper", "transcriber", "language_detector"}
        imported: set[str] = set()
        for name in (
            "source_analyzer.py",
            "source_inventory.py",
            "source_analysis_service.py",
        ):
            tree = ast.parse(Path(__file__).with_name(name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".", 1)[0])
        self.assertTrue(forbidden.isdisjoint(imported), imported & forbidden)

    def test_21_m1_twelve_case_acceptance_remains_green(self) -> None:
        self._run_named_suite(("test_m1_acceptance",), minimum=12)

    def test_22_existing_m2_integration_regression_remains_green(self) -> None:
        self._run_named_suite(
            (
                "test_source_inventory",
                "test_source_decision_adapter",
                "test_source_analysis_service",
                "test_pipeline_source_decision",
                "test_m2_fixtures",
                "test_m2_worker_integration",
                "test_source_analyzer",
                "test_config_source_analyzer",
            ),
            minimum=106,
        )


if __name__ == "__main__":
    unittest.main()
