from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from pipeline_state import PipelineJobStore
from source_analyzer import (
    ANALYZER_VERSION,
    DECISION_SCHEMA_VERSION,
    DECISION_VERSION,
    USE_EXISTING_ZH_TW,
)
from source_analysis_service import (
    CandidateInventory,
    SourceAnalysisAttemptError,
    SourceAnalysisContext,
    run_source_analysis,
    source_decision_idempotency_key,
)


TRADITIONAL_TEXT = (
    "這裡是臺灣繁體中文字幕，歡迎大家一起觀看動畫。"
    "我們會繼續努力，讓每個人都能學習並選擇優質內容。"
)


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class _InventoryFacade:
    def __init__(self, arguments: dict) -> None:
        self.arguments = arguments
        self.method_calls = 0

    def analyzer_arguments(self) -> dict:
        self.method_calls += 1
        return self.arguments


class SourceAnalysisServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "scanner_state.sqlite3"
        self.media = self.root / "episode.mkv"
        self.media.write_bytes(b"immutable-video")
        self.store = PipelineJobStore(self.database)
        stat = self.media.stat()
        self.store.observe_ingest(
            self.media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type="created",
            state="STABILIZING",
            evidence={"test": True},
            confidence=1.0,
        )
        observed = self.store.observe_ingest(
            self.media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type="closed",
            state="QUEUED",
            evidence={"test": True},
            confidence=1.0,
        )
        job = self.store.get_job(str(observed["job_id"]))
        assert job is not None
        self.job = job

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _attempt(self) -> dict:
        return self.store.start_stage_attempt(
            str(self.job["job_id"]),
            "SUBTITLE_DETECTION",
            inputs={"contract": "m2-source-analysis-service"},
            retry_limit=2,
            reason_code="source_analysis_started",
            evidence={"test": True},
            confidence=1.0,
        )

    def _finish(self, attempt: dict) -> None:
        self.store.finish_stage_attempt(
            str(attempt["stage_attempt_id"]),
            "SUCCEEDED",
            reason_code="source_analysis_succeeded",
            evidence={"test": True},
            confidence=1.0,
        )

    def _context(self, attempt: dict, **overrides: object) -> SourceAnalysisContext:
        values: dict[str, object] = {
            "job_id": str(self.job["job_id"]),
            "stage_attempt_id": str(attempt["stage_attempt_id"]),
            "input_identity": {
                "canonical_path": str(self.job["canonical_path"]),
                "media_revision": str(self.job["media_revision"]),
                "inventory_revision": "inventory-v1",
            },
            "media_revision": str(self.job["media_revision"]),
            "source_fingerprint": str(self.job["media_fingerprint"]),
            "config_fingerprint": _sha256("source-analysis-config-v1"),
            "cheap_candidate_fingerprint": _sha256("cheap-candidate-inventory-v1"),
            "analyzer_version": ANALYZER_VERSION,
            "decision_schema_version": DECISION_SCHEMA_VERSION,
            "decision_version": DECISION_VERSION,
        }
        values.update(overrides)
        return SourceAnalysisContext(**values)  # type: ignore[arg-type]

    def _inventory_arguments(self) -> dict:
        return {
            "subtitle_candidates": [
                {
                    "track_index": -1,
                    "codec": "ass",
                    "source_kind": "sidecar",
                    "source_reference": "episode.zh-TW.ass",
                    "source_size": 12_345,
                    "source_mtime_ns": 1_725_000_000_123_456_789,
                    "source_sha256": _sha256("sidecar-source-bytes"),
                    "content_sha256": _sha256("semantic-subtitle-events"),
                    "container_language_tag": "zh-TW",
                    "title": "臺灣繁體",
                    "default": False,
                    "forced": False,
                    "hearing_impaired": False,
                    "event_count": 240,
                    "first_timestamp_seconds": 12.0,
                    "last_timestamp_seconds": 1_425.0,
                    "valid_timing_count": 240,
                    "empty_event_count": 0,
                    "sample_text": TRADITIONAL_TEXT,
                    "extraction_error": "",
                }
            ],
            "audio_candidates": [],
            "media_duration_seconds": 1_440.0,
            "subtitle_inventory_complete": True,
            "audio_inventory_complete": True,
        }

    def _loader(self, calls: list[str]):
        def load() -> CandidateInventory:
            calls.append("loaded")
            return CandidateInventory.from_value(self._inventory_arguments())

        return load

    @staticmethod
    def _analysis_core(decision: dict) -> dict:
        keys = (
            "strategy",
            "confidence",
            "reason_code",
            "evidence",
            "selected_subtitle_track",
            "selected_audio_track",
            "candidates",
            "unselected_reasons",
        )
        return {key: decision[key] for key in keys}

    def test_miss_loads_analyzes_and_persists_one_bound_checkpoint(self) -> None:
        attempt = self._attempt()
        context = self._context(attempt)
        calls: list[str] = []

        result = run_source_analysis(self.store, context, self._loader(calls))

        self.assertFalse(result.reused)
        self.assertEqual(result.reuse_reason, "source_decision_missing")
        self.assertTrue(result.candidate_loader_called)
        structured = result.to_dict()
        self.assertFalse(structured["reused"])
        self.assertEqual(structured["reuse_reason"], "source_decision_missing")
        self.assertEqual(calls, ["loaded"])
        self.assertEqual(result.strategy, USE_EXISTING_ZH_TW)
        self.assertEqual(result.idempotency_key, source_decision_idempotency_key(context))
        self.assertEqual(len(self.store.list_source_decisions(context.job_id)), 1)
        current = self.store.list_stage_attempts(context.job_id, "SUBTITLE_DETECTION")[0]
        self.assertEqual(current["status"], "RUNNING")
        self.assertTrue(current["outputs_verified"])
        self.assertEqual(current["checkpoint"]["decision_id"], result.decision_id)
        selected = result.decision["selected_subtitle_track"]
        self.assertEqual(selected["source_kind"], "sidecar")
        self.assertEqual(selected["source_reference"], "episode.zh-TW.ass")
        self.assertEqual(selected["source_sha256"], _sha256("sidecar-source-bytes"))

    def test_exact_hit_skips_loader_and_rebinds_same_immutable_decision(self) -> None:
        first_attempt = self._attempt()
        first_context = self._context(first_attempt)
        first = run_source_analysis(self.store, first_context, self._loader([]))
        self._finish(first_attempt)
        second_attempt = self._attempt()
        second_context = replace(
            first_context,
            stage_attempt_id=str(second_attempt["stage_attempt_id"]),
        )
        calls: list[str] = []

        def must_not_load():
            calls.append("unexpected")
            raise AssertionError("candidate loader was called on a reuse hit")

        reused = run_source_analysis(self.store, second_context, must_not_load)

        self.assertTrue(reused.reused)
        self.assertEqual(reused.reuse_reason, "source_decision_reusable")
        self.assertFalse(reused.candidate_loader_called)
        self.assertEqual(calls, [])
        self.assertEqual(reused.decision_id, first.decision_id)
        self.assertEqual(reused.decision_sha256, first.decision_sha256)
        self.assertEqual(len(self.store.list_source_decisions(first_context.job_id)), 1)
        attempts = self.store.list_stage_attempts(first_context.job_id, "SUBTITLE_DETECTION")
        self.assertEqual(attempts[-1]["checkpoint"]["decision_id"], first.decision_id)
        self.assertTrue(attempts[-1]["outputs_verified"])

    def test_restart_reuses_committed_decision_without_loading_candidates(self) -> None:
        first_attempt = self._attempt()
        first_context = self._context(first_attempt)
        first = run_source_analysis(self.store, first_context, self._loader([]))
        self._finish(first_attempt)
        self.store.commit()
        self.store.close()
        self.store = PipelineJobStore(self.database)
        second_attempt = self._attempt()
        second_context = replace(
            first_context,
            stage_attempt_id=str(second_attempt["stage_attempt_id"]),
        )

        reused = run_source_analysis(
            self.store,
            second_context,
            lambda: self.fail("restart hit must not reload detailed candidates"),
        )

        self.assertTrue(reused.reused)
        self.assertEqual(reused.decision_id, first.decision_id)
        self.assertEqual(len(self.store.list_source_decisions(first_context.job_id)), 1)

    def test_all_versions_and_input_identity_invalidate_reuse(self) -> None:
        attempt = self._attempt()
        context = self._context(attempt)
        run_source_analysis(self.store, context, self._loader([]))
        self._finish(attempt)

        changes = (
            ("analyzer_version", f"{ANALYZER_VERSION}-next", "source_decision_analyzer_version_changed"),
            ("decision_schema_version", DECISION_SCHEMA_VERSION + 1, "source_decision_schema_version_changed"),
            ("decision_version", f"{DECISION_VERSION}-next", "source_decision_version_changed"),
        )
        for field_name, value, expected_reason in changes:
            attempt = self._attempt()
            context = replace(
                context,
                stage_attempt_id=str(attempt["stage_attempt_id"]),
                **{field_name: value},
            )
            calls: list[str] = []
            result = run_source_analysis(self.store, context, self._loader(calls))
            self.assertFalse(result.reused)
            self.assertEqual(result.reuse_reason, expected_reason)
            self.assertEqual(calls, ["loaded"])
            self._finish(attempt)

        attempt = self._attempt()
        changed_identity = dict(context.input_identity)
        changed_identity["inventory_revision"] = "inventory-v2"
        context = replace(
            context,
            stage_attempt_id=str(attempt["stage_attempt_id"]),
            input_identity=changed_identity,
        )
        result = run_source_analysis(self.store, context, self._loader([]))
        self.assertFalse(result.reused)
        self.assertEqual(result.reuse_reason, "source_decision_input_identity_changed")
        self.assertEqual(len(self.store.list_source_decisions(context.job_id)), 5)

    def test_loader_exception_leaves_no_decision_or_verified_checkpoint(self) -> None:
        attempt = self._attempt()
        context = self._context(attempt)

        def fail_load():
            raise RuntimeError("candidate extraction failed")

        with self.assertRaisesRegex(RuntimeError, "candidate extraction failed"):
            run_source_analysis(self.store, context, fail_load)

        self.assertEqual(self.store.list_source_decisions(context.job_id), [])
        current = self.store.list_stage_attempts(context.job_id, "SUBTITLE_DETECTION")[0]
        self.assertEqual(current["status"], "RUNNING")
        self.assertFalse(current["outputs_verified"])
        self.assertEqual(current["checkpoint"], {})

    def test_same_loaded_input_is_deterministic_after_candidate_invalidation(self) -> None:
        first_attempt = self._attempt()
        first_context = self._context(first_attempt)
        first = run_source_analysis(self.store, first_context, self._loader([]))
        self._finish(first_attempt)
        second_attempt = self._attempt()
        second_context = replace(
            first_context,
            stage_attempt_id=str(second_attempt["stage_attempt_id"]),
            cheap_candidate_fingerprint=_sha256("cheap-candidate-inventory-v2"),
        )
        second = run_source_analysis(self.store, second_context, self._loader([]))

        self.assertFalse(second.reused)
        self.assertEqual(
            second.reuse_reason,
            "source_decision_candidate_fingerprint_changed",
        )
        self.assertEqual(
            self._analysis_core(dict(first.decision)),
            self._analysis_core(dict(second.decision)),
        )
        self.assertNotEqual(first.decision_id, second.decision_id)
        self.assertEqual(
            source_decision_idempotency_key(first_context),
            source_decision_idempotency_key(
                replace(first_context, stage_attempt_id="another-running-attempt")
            ),
        )

    def test_inventory_adapter_method_is_consumed_only_on_a_miss(self) -> None:
        attempt = self._attempt()
        context = self._context(attempt)
        facade = _InventoryFacade(self._inventory_arguments())

        result = run_source_analysis(self.store, context, lambda: facade)

        self.assertEqual(result.strategy, USE_EXISTING_ZH_TW)
        self.assertEqual(facade.method_calls, 1)

    def test_non_running_attempt_fails_before_loading(self) -> None:
        attempt = self._attempt()
        self._finish(attempt)
        context = self._context(attempt)
        calls: list[str] = []

        with self.assertRaisesRegex(SourceAnalysisAttemptError, "RUNNING"):
            run_source_analysis(self.store, context, self._loader(calls))

        self.assertEqual(calls, [])
        self.assertEqual(self.store.list_source_decisions(context.job_id), [])


if __name__ == "__main__":
    unittest.main()
