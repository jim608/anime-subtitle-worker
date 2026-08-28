from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from processing_provenance import (
    ProvenanceRecorder,
    load_provenance,
    processing_config_signature,
    prompt_signature,
    provenance_path_for_video,
)
from translator import TRANSLATION_PROMPT_VERSION


class ProcessingProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.video = self.root / "episode.mkv"
        self.video.write_bytes(b"video")
        self.config = SimpleNamespace(
            work_path=self.root,
            processing_provenance_path="provenance",
            whisper_model="large-v3",
            japanese_transcription_model="large-v3",
            japanese_transcription_fallback_model="large-v2",
            japanese_transcription_fallback_compute_type="float16",
            japanese_transcription_final_fallback_backend="faster-whisper",
            japanese_transcription_final_fallback_model="medium",
            japanese_transcription_final_fallback_compute_type="int8_float16",
            whisper_compute_type="float16",
            whisper_beam_size=5,
            whisper_best_of=5,
            whisper_initial_prompt="Japanese anime dialogue",
            translator_model="sakura",
            batch_size=6,
            translation_metadata_context_enabled=True,
            metadata_context_max_chars=2000,
            translation_glossary={"先輩": "學長"},
            opencc_config="s2twp.json",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_recorder_persists_stage_sections_and_outcome_atomically(self) -> None:
        recorder = ProvenanceRecorder(self.config, self.video)
        recorder.record_stage("language_detect", "running", "sampling")
        recorder.record_stage("language_detect", "ok", "ja 98%")
        recorder.update("asr", {"model": "large-v3"})
        recorder.finish(ok=True, outcome={"created": 3})

        payload = load_provenance(self.config, self.video)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["asr"]["model"], "large-v3")
        self.assertEqual(payload["outcome"]["created"], 3)
        self.assertEqual(payload["stages"][-1]["status"], "ok")
        self.assertNotIn("acceptance_run_id", payload)
        self.assertFalse(provenance_path_for_video(self.config, self.video).with_suffix(".json.tmp").exists())
        json.loads(provenance_path_for_video(self.config, self.video).read_text(encoding="utf-8"))

    def test_acceptance_run_id_is_persisted_and_changes_resume_identity(self) -> None:
        first_run_id = "accrun_" + "3" * 48
        second_run_id = "accrun_" + "4" * 48
        with patch(
            "processing_provenance.acceptance_run_id_for_video",
            return_value=first_run_id,
        ):
            original = ProvenanceRecorder(self.config, self.video)
            original.record_stage("translation", "running", "old acceptance run")

        with patch(
            "processing_provenance.acceptance_run_id_for_video",
            return_value=second_run_id,
        ):
            restarted = ProvenanceRecorder(self.config, self.video)

        self.assertEqual(restarted.payload["acceptance_run_id"], second_run_id)
        self.assertEqual(restarted.payload["stages"], [])
        self.assertNotEqual(restarted.payload["created_at"], original.payload["created_at"])

    def test_recorder_restarts_when_processing_policy_identity_changes(self) -> None:
        original = ProvenanceRecorder(self.config, self.video)
        original.record_stage("translation", "failed", "old policy")
        original.finish(ok=False, error=RuntimeError("old failure"))

        changed = SimpleNamespace(**{**vars(self.config), "batch_size": 7})
        restarted = ProvenanceRecorder(changed, self.video)

        self.assertEqual(
            restarted.payload["config_signature"],
            processing_config_signature(changed),
        )
        self.assertNotEqual(
            restarted.payload["config_signature"],
            processing_config_signature(self.config),
        )
        self.assertEqual(restarted.payload["status"], "running")
        self.assertEqual(restarted.payload["stages"], [])
        self.assertNotIn("error", restarted.payload)
        self.assertNotIn("finished_at", restarted.payload)

    def test_recorder_restarts_when_media_identity_changes(self) -> None:
        original = ProvenanceRecorder(self.config, self.video)
        original.record_stage("transcription", "ok", "old media")

        self.video.write_bytes(b"replacement-video")
        restarted = ProvenanceRecorder(self.config, self.video)

        self.assertEqual(restarted.payload["status"], "running")
        self.assertEqual(restarted.payload["stages"], [])
        self.assertEqual(restarted.payload["video"]["size"], self.video.stat().st_size)
        self.assertNotEqual(restarted.payload["video"], original.payload["video"])

    def test_recorder_resumes_checkpoints_for_same_current_identity(self) -> None:
        original = ProvenanceRecorder(self.config, self.video)
        original.record_stage("transcription", "running", "checkpoint")
        original.update("asr", {"checkpoint": "segment-42"})
        created_at = original.payload["created_at"]

        resumed = ProvenanceRecorder(self.config, self.video)

        self.assertEqual(resumed.payload["created_at"], created_at)
        self.assertEqual(resumed.payload["status"], "running")
        self.assertEqual(resumed.payload["asr"]["checkpoint"], "segment-42")
        self.assertEqual(resumed.payload["stages"][-1]["message"], "checkpoint")

    def test_signatures_change_when_processing_contract_changes(self) -> None:
        first = processing_config_signature(self.config)
        changed = SimpleNamespace(**{**vars(self.config), "batch_size": 7})
        self.assertNotEqual(first, processing_config_signature(changed))
        remediation_changed = SimpleNamespace(
            **{
                **vars(self.config),
                "subtitle_remediation_max_timing_shift_seconds": 0.10,
            }
        )
        self.assertNotEqual(first, processing_config_signature(remediation_changed))
        memory_changed = SimpleNamespace(
            **{
                **vars(self.config),
                "translation_memory_enabled": False,
                "translation_memory_auto_apply_enabled": False,
            }
        )
        self.assertNotEqual(first, processing_config_signature(memory_changed))
        self.assertEqual(prompt_signature("a", "b"), prompt_signature("a", "b"))
        self.assertNotEqual(prompt_signature("a", "b"), prompt_signature("a", "c"))

    def test_language_routing_policy_changes_processing_signature(self) -> None:
        baseline_values = {
            **vars(self.config),
            "language_gate_enabled": True,
            "allowed_source_languages": ["ja"],
            "skip_non_allowed_language": True,
            "transcribe_non_allowed_languages": True,
            "translate_non_japanese_sources": True,
            "language_detect_model": "large-v3",
            "language_detect_min_probability": 0.60,
            "language_detect_sample_count": 3,
            "language_detect_sample_seconds": 15,
            "language_uncertain_policy": "continue",
            "audio_content_probe_enabled": True,
            "audio_content_probe_max_streams": 8,
            "audio_content_probe_sample_count": 3,
            "audio_content_probe_sample_seconds": 12,
            "force_ai_bypass_language_gate": False,
        }
        baseline = SimpleNamespace(**baseline_values)
        first = processing_config_signature(baseline)
        changes = {
            "language_gate_enabled": False,
            "allowed_source_languages": ["ja", "en"],
            "skip_non_allowed_language": False,
            "transcribe_non_allowed_languages": False,
            "translate_non_japanese_sources": False,
            "language_detect_model": "alternate-detector",
            "language_detect_min_probability": 0.75,
            "language_detect_sample_count": 5,
            "language_detect_sample_seconds": 30,
            "language_uncertain_policy": "skip",
            "audio_content_probe_enabled": False,
            "audio_content_probe_max_streams": 4,
            "audio_content_probe_sample_count": 5,
            "audio_content_probe_sample_seconds": 20,
            "force_ai_bypass_language_gate": True,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = SimpleNamespace(**{**baseline_values, field: value})
                self.assertNotEqual(first, processing_config_signature(changed))

    def test_asr_fallback_chain_changes_processing_signature(self) -> None:
        baseline = processing_config_signature(self.config)
        changes = {
            "japanese_transcription_fallback_compute_type": "int8_float16",
            "japanese_transcription_final_fallback_backend": "transformers-whisper",
            "japanese_transcription_final_fallback_model": "alternate-medium",
            "japanese_transcription_final_fallback_compute_type": "float16",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = SimpleNamespace(**{**vars(self.config), field: value})
                self.assertNotEqual(
                    baseline,
                    processing_config_signature(changed),
                )

    def test_translation_prompt_has_human_readable_version(self) -> None:
        self.assertRegex(TRANSLATION_PROMPT_VERSION, r"^(?:ja|multi)-zh-indexed-v\d+$")


if __name__ == "__main__":
    unittest.main()
