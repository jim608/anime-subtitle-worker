from __future__ import annotations

from types import SimpleNamespace
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from asr_quality import (
    KNOWN_ASR_HALLUCINATION_TEXTS,
    asr_artifact_line_indexes,
    asr_prompt_echo_line_indexes,
    asr_prompt_echo_reason,
)
from safe_files import sha256_file
from srt_utils import SrtBlock, read_srt, write_srt
from transcriber import (
    LowConfidenceTranscriptionError,
    SegmentConfidence,
    TranscriptionError,
    attach_asr_diagnostics_context,
    asr_diagnostics_path,
    claim_asr_repair_attempt,
    _artifact_review_ranges,
    _clean_transcribed_text,
    _expand_review_ranges_to_primary_blocks,
    _filter_asr_prompt_echo_chunks,
    _gap_rescue_clips,
    _gap_rescue_segment_rejection_reason,
    _is_hallucination_text,
    _is_repeated_vocalization_text,
    _low_confidence_review_ranges,
    _op_ed_segment_rejection_reason,
    _prompt_free_artifact_silence_evidence,
    _prompt_free_tail_artifact_consensus,
    _selective_window_silence_evidence,
    _tail_adjacent_speech_coverage_evidence,
    finalize_repaired_transcription,
    repair_low_confidence_ranges,
    _rescue_op_ed_lyrics,
    _select_op_ed_rescue_ranges,
    _select_rescue_gaps,
    transcribe_to_srt,
    validate_transcription_srt_quality,
    verify_asr_diagnostics_context,
    _validate_transcription_quality,
)


CONFIG = SimpleNamespace(
    filter_repeated_vocalizations=True,
    repeated_vocalization_min_chars=6,
    whisper_hallucination_phrases=[],
)


def _final_quality_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        work_path=root,
        transcription_quality_check_enabled=True,
        transcription_quality_min_audio_seconds=600.0,
        transcription_quality_min_coverage_percent=8.0,
        transcription_quality_min_blocks_per_minute=1.5,
        transcription_quality_min_avg_logprob=-1.0,
        transcription_quality_max_low_confidence_percent=25.0,
        transcription_quality_min_confidence_segments=8,
        transcription_quality_max_leading_gap_seconds=30.0,
        enable_leading_gap_rescue=True,
        gap_rescue_leading_max_seconds=120.0,
        asr_diagnostics_enabled=True,
        asr_diagnostics_path="diagnostics",
        whisper_model="large-v2",
        whisper_compute_type="float16",
        whisper_hallucination_phrases=[],
        write_gap_report=False,
    )


def _selective_repair_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        work_path=root,
        gap_rescue_clip_seconds=30.0,
        gap_rescue_clip_overlap_seconds=2.0,
        whisper_initial_prompt="must not be reused",
        op_ed_initial_prompt="must not be reused either",
        whisper_hallucination_phrases=[],
        whisper_model="large-v2",
        whisper_compute_type="float16",
        transcription_quality_min_avg_logprob=-1.0,
        asr_diagnostics_enabled=False,
        asr_diagnostics_path="diagnostics",
    )


def _prompt_free_tail_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        work_path=root,
        whisper_model="large-v2",
        whisper_device="cpu",
        whisper_compute_type="int8",
        whisper_language="ja",
        whisper_task="transcribe",
        whisper_vad_filter=False,
        whisper_vad_threshold=0.5,
        whisper_vad_min_silence_duration_ms=500,
        whisper_vad_speech_pad_ms=0,
        whisper_condition_on_previous_text=False,
        whisper_temperature=0.0,
        whisper_beam_size=5,
        whisper_best_of=5,
        whisper_patience=1.0,
        whisper_length_penalty=1.0,
        whisper_repetition_penalty=1.0,
        whisper_no_repeat_ngram_size=0,
        whisper_word_timestamps=True,
        whisper_no_speech_threshold=0.6,
        whisper_log_prob_threshold=-1.0,
        whisper_compression_ratio_threshold=2.4,
        whisper_hallucination_silence_threshold=None,
        whisper_initial_prompt=None,
        whisper_model_cache_enabled=False,
        whisper_hallucination_phrases=[],
        filter_repeated_vocalizations=True,
        repeated_vocalization_min_chars=6,
        subtitle_timing_mode="segment",
        subtitle_max_duration_seconds=4.8,
        subtitle_min_duration_seconds=0.8,
        subtitle_max_chars=24,
        subtitle_end_padding_seconds=0.12,
        subtitle_min_gap_seconds=0.06,
        enable_gap_rescue=False,
        enable_leading_gap_rescue=False,
        op_ed_transcription_enabled=True,
        op_ed_initial_prompt=None,
        op_ed_min_audio_seconds=0.0,
        op_ed_opening_window_seconds=0.0,
        op_ed_ending_window_seconds=10.0,
        op_ed_gap_threshold_seconds=0.5,
        op_ed_max_gap_seconds=20.0,
        op_ed_max_rescue_ranges=2,
        op_ed_padding_seconds=0.0,
        op_ed_no_speech_threshold=0.95,
        op_ed_log_prob_threshold=-1.5,
        op_ed_compression_ratio_threshold=3.0,
        gap_rescue_clip_seconds=30.0,
        gap_rescue_clip_overlap_seconds=2.0,
        asr_selective_retry_padding_seconds=1.5,
        asr_selective_retry_merge_gap_seconds=3.0,
        asr_optional_rescue_rejection_is_fatal=False,
        asr_prompt_free_allow_recovered_primary_artifacts=True,
        transcription_quality_check_enabled=False,
        transcription_quality_min_avg_logprob=-1.0,
        asr_diagnostics_enabled=True,
        asr_diagnostics_path="diagnostics",
        write_gap_report=False,
    )


class TranscriberFilterTest(unittest.TestCase):
    def test_asr_diagnostics_context_is_hash_bound_and_claims_each_repair_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "episode.mkv"
            media.write_bytes(b"media")
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.ja.srt"
            write_srt(
                output,
                [SrtBlock(1, "00:00:08,000 --> 00:00:10,000", ["rejected"])],
            )
            config = _final_quality_config(root)
            diagnostic = asr_diagnostics_path(output, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "selective_retry_required",
                        "srt_path": str(output),
                        "srt_sha256": sha256_file(output),
                        "reason_code": "low_confidence",
                        "review_ranges": [[8.0, 10.0]],
                        "model": "large-v2",
                        "compute_type": "float16",
                    }
                ),
                encoding="utf-8",
            )

            context = attach_asr_diagnostics_context(
                output,
                config,
                media_path=media,
                audio_path=audio,
                audio_stream={"index": 1, "language": "ja"},
            )

            self.assertEqual(context["review_ranges"], [[8.0, 10.0]])
            for key in (
                "media_fingerprint",
                "audio_fingerprint",
                "audio_stream_fingerprint",
                "cache_fingerprint",
            ):
                self.assertEqual(len(context[key]["fingerprint"]), 64)
            repair_fingerprint = context["repair_fingerprint"]
            self.assertEqual(len(repair_fingerprint), 64)
            verified, reasons, observed = verify_asr_diagnostics_context(
                output,
                config,
                media_path=media,
                audio_path=audio,
                audio_stream={"index": 1, "language": "ja"},
            )
            self.assertTrue(verified)
            self.assertEqual(reasons, [])
            self.assertEqual(observed["repair_fingerprint"], repair_fingerprint)
            self.assertTrue(
                claim_asr_repair_attempt(output, config, repair_fingerprint)
            )
            self.assertFalse(
                claim_asr_repair_attempt(output, config, repair_fingerprint)
            )

            audio.write_bytes(b"changed-audio")
            verified, reasons, _observed = verify_asr_diagnostics_context(
                output,
                config,
                media_path=media,
                audio_path=audio,
                audio_stream={"index": 1, "language": "ja"},
            )
            self.assertFalse(verified)
            self.assertIn("extracted audio fingerprint mismatch", reasons)

    def test_prompt_free_recovery_accepts_only_evidence_backed_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.srt"
            segment = SimpleNamespace(
                start=0.0,
                end=2.0,
                text="正常台詞",
                words=[],
                avg_logprob=-0.2,
                no_speech_prob=0.1,
                compression_ratio=1.1,
            )
            model = SimpleNamespace(
                transcribe=lambda *_args, **_kwargs: ([segment], SimpleNamespace())
            )
            config = SimpleNamespace(
                work_path=root,
                whisper_model="large-v2",
                whisper_device="cpu",
                whisper_compute_type="int8",
                whisper_language="ja",
                whisper_task="transcribe",
                whisper_vad_filter=False,
                whisper_vad_threshold=0.5,
                whisper_vad_min_silence_duration_ms=2000,
                whisper_vad_speech_pad_ms=400,
                whisper_condition_on_previous_text=False,
                whisper_temperature=0.0,
                whisper_beam_size=5,
                whisper_best_of=5,
                whisper_patience=1.0,
                whisper_length_penalty=1.0,
                whisper_repetition_penalty=1.0,
                whisper_no_repeat_ngram_size=0,
                whisper_word_timestamps=True,
                whisper_no_speech_threshold=0.6,
                whisper_log_prob_threshold=-1.0,
                whisper_compression_ratio_threshold=2.4,
                whisper_hallucination_silence_threshold=None,
                whisper_initial_prompt=None,
                whisper_model_cache_enabled=False,
                whisper_hallucination_phrases=[],
                filter_repeated_vocalizations=True,
                repeated_vocalization_min_chars=6,
                subtitle_timing_mode="segment",
                subtitle_max_duration_seconds=4.8,
                subtitle_min_duration_seconds=0.8,
                subtitle_max_chars=24,
                subtitle_end_padding_seconds=0.12,
                subtitle_min_gap_seconds=0.06,
                enable_gap_rescue=True,
                enable_leading_gap_rescue=False,
                op_ed_transcription_enabled=False,
                asr_selective_retry_padding_seconds=1.5,
                asr_selective_retry_merge_gap_seconds=3.0,
                asr_optional_rescue_rejection_is_fatal=False,
                transcription_quality_check_enabled=False,
                transcription_quality_min_avg_logprob=-1.0,
                asr_diagnostics_enabled=True,
                asr_diagnostics_path="diagnostics",
                write_gap_report=False,
            )

            def reject_optional_rescue(
                _model,
                _audio_path,
                _chunks,
                _config,
                _logger,
                *,
                removed_artifact_ranges=None,
                rejected_quality_ranges=None,
            ):
                self.assertEqual(removed_artifact_ranges, [])
                rejected_quality_ranges.append((10.0, 12.0))
                return []

            with (
                patch("transcriber.get_whisper_model", return_value=model),
                patch("transcriber._rescue_gaps", side_effect=reject_optional_rescue),
            ):
                result = transcribe_to_srt(
                    audio,
                    output,
                    config,
                    logging.getLogger("test.transcriber.optional-rescue"),
                )

            self.assertEqual(result, output)
            self.assertEqual(len(read_srt(output)), 1)
            diagnostic = json.loads(
                asr_diagnostics_path(output, config).read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["status"], "accepted")
            self.assertEqual(
                diagnostic["reason_code"],
                "optional_rescue_rejections_ignored",
            )
            self.assertEqual(diagnostic["review_ranges"], [[2.12, 13.5]])

            strict_output = root / "strict-episode.srt"
            config.asr_optional_rescue_rejection_is_fatal = True
            with (
                patch("transcriber.get_whisper_model", return_value=model),
                patch("transcriber._rescue_gaps", side_effect=reject_optional_rescue),
                self.assertRaises(LowConfidenceTranscriptionError),
            ):
                transcribe_to_srt(
                    audio,
                    strict_output,
                    config,
                    logging.getLogger("test.transcriber.strict-optional-rescue"),
                )
            strict_diagnostic = json.loads(
                asr_diagnostics_path(strict_output, config).read_text(encoding="utf-8")
            )
            self.assertEqual(strict_diagnostic["status"], "selective_retry_required")

            artifact_output = root / "recovered-primary.srt"
            artifact_segments = [
                SimpleNamespace(
                    start=0.0,
                    end=2.0,
                    text=KNOWN_ASR_HALLUCINATION_TEXTS[0],
                    words=[],
                    avg_logprob=-0.2,
                    no_speech_prob=0.1,
                    compression_ratio=1.1,
                ),
            ]
            artifact_model = SimpleNamespace(
                transcribe=lambda *_args, **_kwargs: (
                    artifact_segments,
                    SimpleNamespace(),
                )
            )
            clean_rescue = [(0.5, 1.5, "clean prompt-free rescue")]
            config.asr_optional_rescue_rejection_is_fatal = False
            config.asr_prompt_free_allow_recovered_primary_artifacts = True
            with (
                patch("transcriber.get_whisper_model", return_value=artifact_model),
                patch("transcriber._rescue_gaps", return_value=clean_rescue),
            ):
                transcribe_to_srt(
                    audio,
                    artifact_output,
                    config,
                    logging.getLogger("test.transcriber.recovered-primary"),
                )

            recovered_diagnostic = json.loads(
                asr_diagnostics_path(artifact_output, config).read_text(encoding="utf-8")
            )
            self.assertEqual(recovered_diagnostic["status"], "accepted")
            self.assertEqual(
                recovered_diagnostic["reason_code"],
                "prompt_free_recovered_primary_artifacts",
            )
            self.assertEqual(read_srt(artifact_output)[0].text, ["clean prompt-free rescue"])

            unresolved_output = root / "unresolved-primary.srt"
            with (
                patch("transcriber.get_whisper_model", return_value=artifact_model),
                patch("transcriber._rescue_gaps", return_value=[]),
                self.assertRaises(LowConfidenceTranscriptionError),
            ):
                transcribe_to_srt(
                    audio,
                    unresolved_output,
                    config,
                    logging.getLogger("test.transcriber.unresolved-primary"),
                )

    def test_selective_repair_final_gate_rejects_an_unresolved_opening_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"not-a-real-wave")
            output = root / "episode.srt"
            write_srt(
                output,
                [SrtBlock(1, "00:00:45,000 --> 00:00:47,000", ["まだ途中から始まる"])],
            )

            with self.assertRaises(LowConfidenceTranscriptionError) as caught:
                finalize_repaired_transcription(
                    audio,
                    output,
                    [(0.0, 45.0)],
                    _final_quality_config(root),
                    logging.getLogger("test.transcriber.final-reject"),
                )

            self.assertEqual(caught.exception.reason_code, "leading_gap")
            diagnostic = next((root / "diagnostics").glob("*.json"))
            payload = json.loads(diagnostic.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "selective_repair_rejected")
            self.assertEqual(payload["review_ranges"], [[0.0, 45.0]])

    def test_selective_repair_final_gate_records_an_accepted_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"not-a-real-wave")
            output = root / "episode.srt"
            write_srt(
                output,
                [SrtBlock(1, "00:00:00,600 --> 00:00:02,000", ["補回した最初の台詞"])],
            )

            finalized = finalize_repaired_transcription(
                audio,
                output,
                [(0.0, 45.0)],
                _final_quality_config(root),
                logging.getLogger("test.transcriber.final-accept"),
            )

            self.assertEqual(finalized, output)
            diagnostic = next((root / "diagnostics").glob("*.json"))
            payload = json.loads(diagnostic.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "accepted_after_selective_retry")
            self.assertEqual(payload["repaired_ranges"], [[0.0, 45.0]])

    def test_selective_repair_splits_long_opening_and_skips_silent_clips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.srt"
            write_srt(
                output,
                [SrtBlock(1, "00:02:00,000 --> 00:02:02,000", ["既存の台詞"])],
            )
            config = SimpleNamespace(
                gap_rescue_clip_seconds=30.0,
                gap_rescue_clip_overlap_seconds=2.0,
                whisper_initial_prompt="must not be reused",
                op_ed_initial_prompt="must not be reused either",
                whisper_hallucination_phrases=[],
            )
            transcribe_calls: list[Path] = []
            observed_prompts: list[tuple[object, object]] = []

            def run_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"clip")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            def transcribe_clip(_sample, sample_srt, repair_config, _logger):
                transcribe_calls.append(Path(sample_srt))
                observed_prompts.append(
                    (
                        repair_config.whisper_initial_prompt,
                        repair_config.op_ed_initial_prompt,
                    )
                )
                if len(transcribe_calls) != 2:
                    raise TranscriptionError("Whisper returned no subtitle segments.")
                write_srt(
                    sample_srt,
                    [SrtBlock(1, "00:00:00,500 --> 00:00:02,000", ["補回的開場台詞"])],
                )

            with (
                patch("transcriber.subprocess.run", side_effect=run_ffmpeg),
                patch("transcriber.transcribe_to_srt", side_effect=transcribe_clip),
            ):
                repair_low_confidence_ranges(
                    audio,
                    output,
                    [(0.0, 120.0)],
                    config,
                    logging.getLogger("test.transcriber.selective-long-opening"),
                )

            blocks = read_srt(output)
            self.assertEqual(len(transcribe_calls), 5)
            self.assertEqual(observed_prompts, [(None, None)] * 5)
            self.assertEqual(blocks[0].timing, "00:00:28,500 --> 00:00:30,000")
            self.assertEqual(blocks[0].text, ["補回的開場台詞"])
            self.assertEqual(blocks[1].text, ["既存の台詞"])

    def test_selective_repair_accepts_artifact_omission_only_with_dual_silence_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.srt"
            original = [
                SrtBlock(1, "00:00:00,000 --> 00:00:02,000", ["suspect opening"]),
                SrtBlock(2, "00:00:10,000 --> 00:00:12,000", ["suspect dialogue"]),
            ]
            write_srt(output, original)
            config = _selective_repair_config(root)
            transcribe_calls: list[Path] = []

            def run_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"clip")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            def transcribe_clip(_sample, sample_srt, _clip_config, _logger):
                transcribe_calls.append(Path(sample_srt))
                if len(transcribe_calls) == 1:
                    raise LowConfidenceTranscriptionError(
                        "known prompt-free artifact",
                        [(0.0, 2.0)],
                        reason_code="asr_artifact",
                    )
                write_srt(
                    sample_srt,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,000 --> 00:00:02,000",
                            ["clean replacement"],
                        )
                    ],
                )

            def confirmed_silence(_sample, _exc, clip_start, clip_end, _config):
                return [
                    {
                        "confirmed": True,
                        "vad_no_speech": True,
                        "energy_no_speech": True,
                        "rms_dbfs": -80.0,
                        "maximum_rms_dbfs": -50.0,
                        "clip_range": [clip_start, clip_end],
                        "local_range": [0.0, clip_end - clip_start],
                        "absolute_range": [clip_start, clip_end],
                        "reason_code": "prompt_free_asr_artifact",
                    }
                ]

            with (
                patch("transcriber.subprocess.run", side_effect=run_ffmpeg),
                patch("transcriber.transcribe_to_srt", side_effect=transcribe_clip),
                patch(
                    "transcriber._prompt_free_artifact_silence_evidence",
                    side_effect=confirmed_silence,
                ),
            ):
                result = repair_low_confidence_ranges(
                    audio,
                    output,
                    [(0.0, 2.0), (10.0, 12.0)],
                    config,
                    logging.getLogger("test.transcriber.selective-dual-silence"),
                )

            self.assertEqual(len(transcribe_calls), 2)
            self.assertEqual(result.confirmed_silent_ranges, ((0.0, 2.0),))
            self.assertEqual(
                [(block.timing, block.text) for block in read_srt(output)],
                [("00:00:10,000 --> 00:00:12,000", ["clean replacement"])],
            )
            pending = json.loads(
                asr_diagnostics_path(output, config).read_text(encoding="utf-8")
            )
            self.assertEqual(pending["status"], "selective_repair_completed")
            self.assertEqual(pending["confirmed_silent_ranges"], [[0.0, 2.0]])
            self.assertTrue(pending["selective_silence_evidence"][0]["vad_no_speech"])
            self.assertTrue(pending["selective_silence_evidence"][0]["energy_no_speech"])

            final_config = _final_quality_config(root)
            finalize_repaired_transcription(
                audio,
                output,
                [(0.0, 2.0), (10.0, 12.0)],
                final_config,
                logging.getLogger("test.transcriber.selective-dual-silence-final"),
            )
            accepted = json.loads(
                asr_diagnostics_path(output, final_config).read_text(encoding="utf-8")
            )
            self.assertEqual(accepted["status"], "accepted_after_selective_retry")
            self.assertEqual(
                accepted["reason_code"],
                "prompt_free_artifact_confirmed_silent",
            )
            self.assertEqual(accepted["confirmed_silent_ranges"], [[0.0, 2.0]])

    def test_selective_artifact_omission_restores_primary_if_diagnostic_write_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.srt"
            original = [
                SrtBlock(1, "00:00:00,000 --> 00:00:02,000", ["suspect opening"]),
                SrtBlock(2, "00:00:10,000 --> 00:00:12,000", ["trusted dialogue"]),
            ]
            write_srt(output, original)
            config = _selective_repair_config(root)

            def run_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"clip")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            artifact = LowConfidenceTranscriptionError(
                "known prompt-free artifact",
                [(0.0, 2.0)],
                reason_code="asr_artifact",
            )
            evidence = [
                {
                    "confirmed": True,
                    "vad_no_speech": True,
                    "energy_no_speech": True,
                    "absolute_range": [0.0, 2.0],
                }
            ]
            with (
                patch("transcriber.subprocess.run", side_effect=run_ffmpeg),
                patch("transcriber.transcribe_to_srt", side_effect=artifact),
                patch(
                    "transcriber._prompt_free_artifact_silence_evidence",
                    return_value=evidence,
                ),
                patch("transcriber._write_asr_diagnostics", return_value=None),
            ):
                with self.assertRaisesRegex(
                    TranscriptionError,
                    "durable diagnostic could not be written",
                ):
                    repair_low_confidence_ranges(
                        audio,
                        output,
                        [(0.0, 2.0)],
                        config,
                        logging.getLogger("test.transcriber.selective-diagnostic-failure"),
                    )

            self.assertEqual(
                [(block.timing, block.text) for block in read_srt(output)],
                [(block.timing, block.text) for block in original],
            )

    def test_selective_repair_checks_later_ranges_but_fails_closed_without_dual_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.srt"
            original = [
                SrtBlock(1, "00:00:00,000 --> 00:00:02,000", ["suspect opening"]),
                SrtBlock(2, "00:00:10,000 --> 00:00:12,000", ["suspect dialogue"]),
            ]
            write_srt(output, original)
            config = _selective_repair_config(root)
            transcribe_calls: list[Path] = []

            def run_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"clip")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            def transcribe_clip(_sample, sample_srt, _clip_config, _logger):
                transcribe_calls.append(Path(sample_srt))
                if len(transcribe_calls) == 1:
                    raise LowConfidenceTranscriptionError(
                        "known prompt-free artifact",
                        [(0.0, 2.0)],
                        reason_code="asr_artifact",
                    )
                write_srt(
                    sample_srt,
                    [SrtBlock(1, "00:00:00,000 --> 00:00:02,000", ["later repair"])],
                )

            insufficient_evidence = [
                {
                    "confirmed": False,
                    "vad_no_speech": True,
                    "energy_no_speech": False,
                    "rms_dbfs": -20.0,
                    "maximum_rms_dbfs": -50.0,
                    "absolute_range": [0.0, 2.0],
                }
            ]
            with (
                patch("transcriber.subprocess.run", side_effect=run_ffmpeg),
                patch("transcriber.transcribe_to_srt", side_effect=transcribe_clip),
                patch(
                    "transcriber._prompt_free_artifact_silence_evidence",
                    return_value=insufficient_evidence,
                ),
            ):
                with self.assertRaisesRegex(
                    TranscriptionError,
                    "after checking all requested ranges",
                ):
                    repair_low_confidence_ranges(
                        audio,
                        output,
                        [(0.0, 2.0), (10.0, 12.0)],
                        config,
                        logging.getLogger("test.transcriber.selective-fail-closed"),
                    )

            self.assertEqual(len(transcribe_calls), 2)
            self.assertEqual(
                [(block.timing, block.text) for block in read_srt(output)],
                [(block.timing, block.text) for block in original],
            )

    def test_selective_silence_evidence_requires_vad_and_energy_on_same_window(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            silent = root / "silent.wav"
            loud = root / "loud.wav"
            for path, frames in (
                (silent, b"\x00\x00" * 16000),
                (loud, int(12000).to_bytes(2, "little", signed=True) * 16000),
            ):
                with wave.open(str(path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(16000)
                    wav_file.writeframes(frames)
            config = SimpleNamespace(
                whisper_vad_threshold=0.35,
                whisper_vad_min_silence_duration_ms=500,
                whisper_vad_speech_pad_ms=800,
            )

            with patch("transcriber._silero_speech_timestamps", return_value=[]):
                both_silent = _selective_window_silence_evidence(
                    silent, 0.1, 0.9, config
                )
                energetic = _selective_window_silence_evidence(
                    loud, 0.1, 0.9, config
                )
            with patch(
                "transcriber._silero_speech_timestamps",
                return_value=[{"start": 0, "end": 512}],
            ):
                vad_speech = _selective_window_silence_evidence(
                    silent, 0.1, 0.9, config
                )

            self.assertTrue(both_silent["confirmed"])
            self.assertTrue(both_silent["vad_no_speech"])
            self.assertTrue(both_silent["energy_no_speech"])
            self.assertFalse(energetic["confirmed"])
            self.assertTrue(energetic["vad_no_speech"])
            self.assertFalse(energetic["energy_no_speech"])
            self.assertFalse(vad_speech["confirmed"])
            self.assertFalse(vad_speech["vad_no_speech"])
            self.assertTrue(vad_speech["energy_no_speech"])

            artifact = LowConfidenceTranscriptionError(
                "known artifact",
                [(0.1, 0.9)],
                reason_code="asr_artifact",
            )
            generic_low_confidence = LowConfidenceTranscriptionError(
                "low confidence speech",
                [(0.1, 0.9)],
                reason_code="low_confidence",
            )
            with patch("transcriber._silero_speech_timestamps", return_value=[]):
                artifact_evidence = _prompt_free_artifact_silence_evidence(
                    silent, artifact, 10.0, 11.0, config
                )
            self.assertTrue(artifact_evidence[0]["confirmed"])
            self.assertEqual(artifact_evidence[0]["absolute_range"], [10.1, 10.9])
            self.assertEqual(
                _prompt_free_artifact_silence_evidence(
                    silent, generic_low_confidence, 10.0, 11.0, config
                ),
                [],
            )

    def test_prompt_free_tail_consensus_covers_nine_live_eof_artifacts(self) -> None:
        live_cases = (
            ("mekakucity-s01e01", 1414.080, (1412.66, 1414.06)),
            ("monogatari-s01e21", 1493.347, (1492.36, 1493.30)),
            ("monogatari-s01e15", 1563.247, (1561.80, 1563.20)),
            ("monogatari-s01e13", 1594.127, (1593.40, 1593.88)),
            ("assassination-classroom-s01e21", 1383.381, (1381.96, 1383.36)),
            ("maoyu-s01e11", 1420.992, (1419.24, 1420.96)),
            ("maoyu-s01e10", 1420.992, (1419.26, 1420.96)),
            ("maoyu-s01e02", 1421.088, (1419.90, 1421.06)),
            ("polar-opposites-s02e06", 1446.998, (1445.56, 1446.96)),
        )
        config = SimpleNamespace(
            asr_prompt_free_allow_recovered_primary_artifacts=True,
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
        )
        silence = {
            "confirmed": True,
            "vad_no_speech": True,
            "energy_no_speech": True,
            "rms_dbfs": -70.0,
            "maximum_rms_dbfs": -50.0,
        }
        adjacent = {
            "complete": True,
            "vad_speech_ranges": [],
            "uncovered_speech_ranges": [],
        }

        for name, duration, artifact_range in live_cases:
            with self.subTest(name=name):
                start, end = artifact_range
                probe = {
                    "range": [max(0.0, start - 10.0), duration],
                    "completed_clip_ranges": [[max(0.0, start - 10.0), duration]],
                    "prompt_free": True,
                    "completed": True,
                    "observed_ranges": [],
                    "known_artifact_ranges": [],
                    "rejected_quality_ranges": [],
                    "clean_ranges": [],
                    "unbounded_observation": False,
                }
                with (
                    patch("transcriber._wav_duration_seconds", return_value=duration),
                    patch(
                        "transcriber._selective_window_silence_evidence",
                        return_value=dict(silence),
                    ),
                    patch(
                        "transcriber._tail_adjacent_speech_coverage_evidence",
                        return_value=dict(adjacent),
                    ),
                ):
                    confirmed, evidence = _prompt_free_tail_artifact_consensus(
                        "episode.wav",
                        [artifact_range],
                        [(start - 2.0, start - 0.5, "covered adjacent speech")],
                        [],
                        [probe],
                        config,
                    )

                self.assertEqual(confirmed, [artifact_range])
                self.assertTrue(evidence[0]["confirmed"])
                self.assertEqual(evidence[0]["second_pass"], "no_dialogue_segment")

    def test_prompt_free_tail_consensus_fails_closed_on_dialogue_or_missing_evidence(
        self,
    ) -> None:
        duration = 100.0
        artifact_range = (98.4, 99.9)
        base_probe = {
            "range": [96.0, duration],
            "completed_clip_ranges": [[96.0, duration]],
            "prompt_free": True,
            "completed": True,
            "observed_ranges": [],
            "known_artifact_ranges": [],
            "rejected_quality_ranges": [],
            "clean_ranges": [],
            "unbounded_observation": False,
        }
        cases = (
            ("missing_second_pass", [], [], base_probe, True, True),
            (
                "clean_recovery_dialogue",
                [(98.5, 99.5, "real tail dialogue")],
                [base_probe],
                base_probe,
                True,
                True,
            ),
            (
                "second_pass_dialogue",
                [],
                [
                    {
                        **base_probe,
                        "observed_ranges": [[98.5, 99.5]],
                        "clean_ranges": [[98.5, 99.5]],
                    }
                ],
                base_probe,
                True,
                True,
            ),
            (
                "second_pass_ambiguous_quality",
                [],
                [
                    {
                        **base_probe,
                        "observed_ranges": [[98.5, 99.5]],
                        "rejected_quality_ranges": [[98.5, 99.5]],
                    }
                ],
                base_probe,
                True,
                True,
            ),
            ("artifact_not_silent", [], [base_probe], base_probe, False, True),
            ("adjacent_speech_missing", [], [base_probe], base_probe, True, False),
        )

        for name, recovery, probes, _unused, silent, adjacent_complete in cases:
            with self.subTest(name=name):
                config = SimpleNamespace(
                    asr_prompt_free_allow_recovered_primary_artifacts=True,
                    whisper_initial_prompt=None,
                    op_ed_initial_prompt=None,
                    whisper_condition_on_previous_text=False,
                )
                with (
                    patch("transcriber._wav_duration_seconds", return_value=duration),
                    patch(
                        "transcriber._selective_window_silence_evidence",
                        return_value={"confirmed": silent},
                    ),
                    patch(
                        "transcriber._tail_adjacent_speech_coverage_evidence",
                        return_value={"complete": adjacent_complete},
                    ),
                ):
                    confirmed, evidence = _prompt_free_tail_artifact_consensus(
                        "episode.wav",
                        [artifact_range],
                        [(96.0, 97.5, "covered adjacent speech")],
                        recovery,
                        probes,
                        config,
                    )

                self.assertEqual(confirmed, [])
                self.assertFalse(evidence[0]["confirmed"])
                self.assertIn("failure", evidence[0])

        prompted = SimpleNamespace(
            asr_prompt_free_allow_recovered_primary_artifacts=True,
            whisper_initial_prompt="unsafe prompt",
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
        )
        with patch("transcriber._wav_duration_seconds", return_value=duration):
            confirmed, evidence = _prompt_free_tail_artifact_consensus(
                "episode.wav",
                [artifact_range],
                [],
                [],
                [base_probe],
                prompted,
            )
        self.assertEqual((confirmed, evidence), ([], []))

    def test_tail_adjacent_speech_requires_every_vad_region_to_be_covered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "episode.wav"
            with wave.open(str(audio), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000 * 20)
            config = SimpleNamespace(
                whisper_vad_threshold=0.35,
                whisper_vad_min_silence_duration_ms=500,
                whisper_vad_speech_pad_ms=800,
            )
            # Tail context is 6-20s.  This VAD interval maps to 16-17s and is
            # fully covered by the retained dialogue block.
            with patch(
                "transcriber._silero_speech_timestamps",
                return_value=[{"start": 10 * 16000, "end": 11 * 16000}],
            ):
                covered = _tail_adjacent_speech_coverage_evidence(
                    audio,
                    18.0,
                    20.0,
                    20.0,
                    [(15.8, 17.2, "real adjacent dialogue")],
                    config,
                )
            self.assertTrue(covered["complete"])

            # A second speech interval at 17.5-18.0s has no subtitle coverage,
            # so the exact same tail artifact must remain in review.
            with patch(
                "transcriber._silero_speech_timestamps",
                return_value=[{"start": 11.5 * 16000, "end": 12 * 16000}],
            ):
                uncovered = _tail_adjacent_speech_coverage_evidence(
                    audio,
                    18.0,
                    20.0,
                    20.0,
                    [(15.8, 17.2, "real adjacent dialogue")],
                    config,
                )
            self.assertFalse(uncovered["complete"])
            self.assertEqual(uncovered["uncovered_speech_ranges"], [[17.5, 18.0]])

            # Exact live regression: the retained cue ends at 1427.18s.  The
            # existing 0.15s tolerance extends coverage to 1427.33s, covering
            # 93.20% of the 1420.56-1427.824s VAD interval.  The remaining
            # 0.494s is only the trailing edge of that same interval.
            with patch(
                "transcriber._silero_speech_timestamps",
                return_value=[
                    {
                        "start": int(0.16 * 16000),
                        "end": int(7.424 * 16000),
                    }
                ],
            ):
                trailing_edge = _tail_adjacent_speech_coverage_evidence(
                    audio,
                    18.0,
                    20.0,
                    20.0,
                    [(6.0, 12.78, "covered dialogue")],
                    config,
                )
            self.assertTrue(trailing_edge["complete"])
            self.assertEqual(trailing_edge["uncovered_speech_ranges"], [])
            self.assertEqual(
                trailing_edge["accepted_trailing_edge_ranges"][0][
                    "trailing_gap_seconds"
                ],
                0.494,
            )
            self.assertEqual(
                trailing_edge["accepted_trailing_edge_ranges"][0][
                    "coverage_ratio"
                ],
                0.932,
            )
            self.assertEqual(
                trailing_edge["accepted_trailing_edge_ranges"][0][
                    "uncovered_range"
                ],
                [12.93, 13.424],
            )

            # Partial-edge exceptions cannot accumulate across VAD regions.
            # Even though each gap independently satisfies both numeric caps,
            # two separate partial regions must fail closed.
            with patch(
                "transcriber._silero_speech_timestamps",
                return_value=[
                    {"start": 2 * 16000, "end": 6 * 16000},
                    {"start": 8 * 16000, "end": 12 * 16000},
                ],
            ):
                accumulated_edges = _tail_adjacent_speech_coverage_evidence(
                    audio,
                    18.0,
                    20.0,
                    20.0,
                    [
                        (7.8, 11.64, "first partial dialogue"),
                        (13.8, 17.64, "second partial dialogue"),
                    ],
                    config,
                )
            self.assertFalse(accumulated_edges["complete"])
            self.assertEqual(accumulated_edges["accepted_trailing_edge_ranges"], [])
            self.assertEqual(
                accumulated_edges["uncovered_speech_ranges"],
                [[8.0, 12.0], [14.0, 18.0]],
            )
            self.assertEqual(len(accumulated_edges["partial_edge_rejections"]), 2)
            self.assertTrue(
                all(
                    edge["reason"]
                    == "multiple partial trailing-edge exceptions are not allowed"
                    for edge in accumulated_edges["partial_edge_rejections"]
                )
            )

            boundary_cases = (
                # 92.75% coverage with an otherwise small 0.29s trailing gap.
                (4.0, 8.0, (9.8, 13.56, "below ratio")),
                # 96.36% coverage but a 0.51s trailing gap.
                (0.0, 14.0, (5.8, 19.34, "gap too large")),
                # Coverage starts after the VAD speech, leaving a leading gap.
                (4.0, 8.0, (10.2, 14.2, "leading gap")),
            )
            for vad_start, vad_end, retained in boundary_cases:
                with patch(
                    "transcriber._silero_speech_timestamps",
                    return_value=[
                        {
                            "start": int(vad_start * 16000),
                            "end": int(vad_end * 16000),
                        }
                    ],
                ):
                    rejected_edge = _tail_adjacent_speech_coverage_evidence(
                        audio,
                        18.0,
                        20.0,
                        20.0,
                        [retained],
                        config,
                    )
                self.assertFalse(rejected_edge["complete"])

            # Two separated subtitle ranges leave an internal gap, even when
            # their combined duration would exceed 95%.  They must never be
            # treated as one continuous coverage interval.
            with patch(
                "transcriber._silero_speech_timestamps",
                return_value=[
                    {"start": 4 * 16000, "end": 10 * 16000},
                ],
            ):
                internal_gap = _tail_adjacent_speech_coverage_evidence(
                    audio,
                    18.0,
                    20.0,
                    20.0,
                    [
                        (9.8, 13.0, "first block"),
                        (13.4, 15.7, "second block"),
                    ],
                    config,
                )
            self.assertFalse(internal_gap["complete"])
            self.assertEqual(
                internal_gap["uncovered_speech_ranges"],
                [[10.0, 16.0]],
            )

    def test_prompt_free_tail_consensus_independently_covers_partial_vad_edge(
        self,
    ) -> None:
        duration = 1435.225
        artifact_range = (1432.4, 1435.2)
        config = SimpleNamespace(
            asr_prompt_free_allow_recovered_primary_artifacts=True,
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
            whisper_condition_on_previous_text=False,
        )
        adjacent = {
            "complete": True,
            "accepted_trailing_edge_ranges": [
                {
                    "speech_range": [1420.56, 1427.824],
                    "coverage_range": [1420.4, 1427.33],
                    "uncovered_range": [1427.33, 1427.824],
                    "coverage_ratio": 0.932,
                    "trailing_gap_seconds": 0.494,
                }
            ],
            "uncovered_speech_ranges": [],
        }
        base_probe = {
            "range": [1427.18, duration],
            "completed_clip_ranges": [[1426.18, duration]],
            "prompt_free": True,
            "completed": True,
            "observed_ranges": [],
            "known_artifact_ranges": [],
            "rejected_quality_ranges": [],
            "clean_ranges": [],
            "unbounded_observation": False,
        }

        with (
            patch("transcriber._wav_duration_seconds", return_value=duration),
            patch(
                "transcriber._selective_window_silence_evidence",
                return_value={"confirmed": True},
            ),
            patch(
                "transcriber._tail_adjacent_speech_coverage_evidence",
                return_value=adjacent,
            ),
        ):
            confirmed, evidence = _prompt_free_tail_artifact_consensus(
                "episode.wav",
                [artifact_range],
                [(1420.4, 1427.18, "covered final dialogue")],
                [],
                [base_probe],
                config,
            )
        self.assertEqual(confirmed, [artifact_range])
        self.assertTrue(evidence[0]["confirmed"])
        self.assertTrue(
            evidence[0]["trailing_edge_probe_evidence"][0][
                "confirmed_no_dialogue"
            ]
        )

        observed_probe = {
            **base_probe,
            "observed_ranges": [[1427.5, 1427.7]],
            "clean_ranges": [[1427.5, 1427.7]],
        }
        with (
            patch("transcriber._wav_duration_seconds", return_value=duration),
            patch(
                "transcriber._selective_window_silence_evidence",
                return_value={"confirmed": True},
            ),
            patch(
                "transcriber._tail_adjacent_speech_coverage_evidence",
                return_value=adjacent,
            ),
        ):
            rejected, rejected_evidence = _prompt_free_tail_artifact_consensus(
                "episode.wav",
                [artifact_range],
                [(1420.4, 1427.18, "covered final dialogue")],
                [],
                [observed_probe],
                config,
            )
        self.assertEqual(rejected, [])
        self.assertFalse(rejected_evidence[0]["confirmed"])
        self.assertEqual(
            rejected_evidence[0]["failure"],
            "prompt-free probe observed content in accepted VAD trailing edge",
        )
        self.assertEqual(
            rejected_evidence[0]["trailing_edge_probe_evidence"][0][
                "overlapping_observed_ranges"
            ],
            [[1427.5, 1427.7]],
        )

    def test_full_prompt_free_tail_consensus_is_hash_bound_and_dialogue_stays_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            with wave.open(str(audio), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000 * 20)
            config = _prompt_free_tail_config(root)
            clean = SimpleNamespace(
                start=10.0,
                end=15.0,
                text="real adjacent dialogue",
                words=[],
                avg_logprob=-0.2,
                no_speech_prob=0.1,
                compression_ratio=1.1,
            )
            artifact = SimpleNamespace(
                start=19.0,
                end=20.0,
                text=KNOWN_ASR_HALLUCINATION_TEXTS[0],
                words=[],
                avg_logprob=-0.2,
                no_speech_prob=0.1,
                compression_ratio=1.1,
            )

            calls = 0

            def consensus_transcribe(*_args, **options):
                nonlocal calls
                calls += 1
                if "clip_timestamps" not in options:
                    return [clean, artifact], SimpleNamespace()
                return [], SimpleNamespace()

            output = root / "accepted-tail.srt"
            with (
                patch(
                    "transcriber.get_whisper_model",
                    return_value=SimpleNamespace(transcribe=consensus_transcribe),
                ),
                patch("transcriber._silero_speech_timestamps", return_value=[]),
            ):
                transcribe_to_srt(
                    audio,
                    output,
                    config,
                    logging.getLogger("test.transcriber.tail-consensus"),
                )

            self.assertEqual(calls, 2)
            self.assertEqual(read_srt(output)[0].text, ["real adjacent dialogue"])
            diagnostic = json.loads(
                asr_diagnostics_path(output, config).read_text(encoding="utf-8")
            )
            self.assertEqual(
                diagnostic["reason_code"],
                "prompt_free_tail_artifact_consensus",
            )
            self.assertEqual(
                diagnostic["confirmed_tail_artifact_ranges"],
                [[19.0, 20.0]],
            )
            self.assertTrue(diagnostic["tail_consensus_evidence"][0]["confirmed"])
            self.assertEqual(diagnostic["srt_sha256"], sha256_file(output))

            tail_dialogue = SimpleNamespace(
                start=18.5,
                end=19.5,
                text="actual spoken epilogue",
                words=[],
                avg_logprob=-0.2,
                no_speech_prob=0.1,
                compression_ratio=1.1,
            )

            def dialogue_transcribe(*_args, **options):
                if "clip_timestamps" not in options:
                    return [clean, artifact], SimpleNamespace()
                return [tail_dialogue], SimpleNamespace()

            review_output = root / "tail-dialogue-review.srt"
            with (
                patch(
                    "transcriber.get_whisper_model",
                    return_value=SimpleNamespace(transcribe=dialogue_transcribe),
                ),
                patch("transcriber._silero_speech_timestamps", return_value=[]),
                self.assertRaises(LowConfidenceTranscriptionError),
            ):
                transcribe_to_srt(
                    audio,
                    review_output,
                    config,
                    logging.getLogger("test.transcriber.tail-dialogue-review"),
                )
            review = json.loads(
                asr_diagnostics_path(review_output, config).read_text(encoding="utf-8")
            )
            self.assertEqual(review["status"], "selective_retry_required")
            self.assertFalse(review["tail_consensus_evidence"][0]["confirmed"])

    def test_tail_consensus_diagnostic_write_failure_removes_unpublishable_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            with wave.open(str(audio), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000 * 20)
            config = _prompt_free_tail_config(root)
            segments = [
                SimpleNamespace(
                    start=10.0,
                    end=15.0,
                    text="real adjacent dialogue",
                    words=[],
                    avg_logprob=-0.2,
                    no_speech_prob=0.1,
                    compression_ratio=1.1,
                ),
                SimpleNamespace(
                    start=19.0,
                    end=20.0,
                    text=KNOWN_ASR_HALLUCINATION_TEXTS[0],
                    words=[],
                    avg_logprob=-0.2,
                    no_speech_prob=0.1,
                    compression_ratio=1.1,
                ),
            ]

            def transcribe(*_args, **options):
                return (
                    ([], SimpleNamespace())
                    if "clip_timestamps" in options
                    else (segments, SimpleNamespace())
                )

            output = root / "diagnostic-failure.srt"
            with (
                patch(
                    "transcriber.get_whisper_model",
                    return_value=SimpleNamespace(transcribe=transcribe),
                ),
                patch("transcriber._silero_speech_timestamps", return_value=[]),
                patch("transcriber._write_asr_diagnostics", return_value=None),
                self.assertRaisesRegex(
                    TranscriptionError,
                    "durable diagnostic could not be written",
                ),
            ):
                transcribe_to_srt(
                    audio,
                    output,
                    config,
                    logging.getLogger("test.transcriber.tail-diagnostic-failure"),
                )
            self.assertFalse(output.exists())

    def test_selective_repair_expands_s03e02_range_without_losing_primary_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.srt"
            write_srt(
                output,
                [
                    SrtBlock(
                        210,
                        "00:10:19,780 --> 00:10:24,360",
                        ["さくらこ何か悩みでもあるんです"],
                    ),
                    SrtBlock(
                        211,
                        "00:10:24,420 --> 00:10:26,040",
                        ["の"],
                    ),
                    SrtBlock(
                        212,
                        "00:10:26,900 --> 00:10:28,520",
                        ["おめーだよ"],
                    ),
                    SrtBlock(
                        213,
                        "00:10:28,980 --> 00:10:30,900",
                        ["いや別にないけど"],
                    ),
                ],
            )
            before_blocks = read_srt(output)
            before_content = [
                (block.timing, list(block.text))
                for block in before_blocks
            ]
            config = SimpleNamespace(
                gap_rescue_clip_seconds=30.0,
                gap_rescue_clip_overlap_seconds=2.0,
                whisper_initial_prompt=None,
                op_ed_initial_prompt=None,
                whisper_hallucination_phrases=[],
            )
            decode_windows: list[tuple[str, str]] = []

            def run_ffmpeg(command, **_kwargs):
                start = command[command.index("-ss") + 1]
                duration = command[command.index("-t") + 1]
                decode_windows.append(
                    (start, f"{float(start) + float(duration):.3f}")
                )
                Path(command[-1]).write_bytes(b"clip")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            def transcribe_clip(_sample, sample_srt, _repair_config, _logger):
                write_srt(
                    sample_srt,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,000 --> 00:00:04,580",
                            list(before_blocks[0].text),
                        ),
                        SrtBlock(
                            2,
                            "00:00:04,640 --> 00:00:06,260",
                            list(before_blocks[1].text),
                        ),
                        SrtBlock(
                            3,
                            "00:00:07,120 --> 00:00:08,740",
                            list(before_blocks[2].text),
                        ),
                    ],
                )

            with (
                patch("transcriber.subprocess.run", side_effect=run_ffmpeg),
                patch("transcriber.transcribe_to_srt", side_effect=transcribe_clip),
            ):
                repair_low_confidence_ranges(
                    audio,
                    output,
                    [(622.920, 627.540)],
                    config,
                    logging.getLogger(
                        "test.transcriber.selective-partial-intersection"
                    ),
                )

            self.assertEqual(decode_windows, [("619.780", "628.520")])
            repaired_content = [
                (block.timing, list(block.text))
                for block in read_srt(output)
            ]
            self.assertEqual(repaired_content, before_content)

    def test_review_ranges_expand_to_primary_block_fixed_point_and_merge(self) -> None:
        primary_blocks = [
            SrtBlock(1, "00:00:10,000 --> 00:00:20,000", ["one"]),
            SrtBlock(2, "00:00:19,000 --> 00:00:30,000", ["two"]),
            SrtBlock(3, "00:00:29,000 --> 00:00:40,000", ["three"]),
        ]

        expanded = _expand_review_ranges_to_primary_blocks(
            primary_blocks,
            [(11.0, 12.0), (38.0, 39.0)],
        )

        self.assertEqual(expanded, [(10.0, 40.0)])

    def test_selective_repair_returns_prompt_free_segment_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"audio")
            output = root / "episode.srt"
            write_srt(
                output,
                [
                    SrtBlock(
                        1,
                        "00:00:10,000 --> 00:00:12,000",
                        ["low confidence primary"],
                    )
                ],
            )
            config = SimpleNamespace(
                work_path=root,
                gap_rescue_clip_seconds=30.0,
                gap_rescue_clip_overlap_seconds=2.0,
                whisper_initial_prompt="must be removed",
                op_ed_initial_prompt="must also be removed",
                whisper_hallucination_phrases=[],
            )
            observed_prompts: list[tuple[object, object]] = []

            def run_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"clip")
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            def transcribe_clip(_sample, sample_srt, clip_config, _logger):
                observed_prompts.append(
                    (
                        clip_config.whisper_initial_prompt,
                        clip_config.op_ed_initial_prompt,
                    )
                )
                write_srt(
                    sample_srt,
                    [
                        SrtBlock(
                            1,
                            "00:00:00,500 --> 00:00:02,000",
                            ["prompt-free replacement"],
                        )
                    ],
                )
                diagnostic = asr_diagnostics_path(sample_srt, clip_config)
                diagnostic.parent.mkdir(parents=True, exist_ok=True)
                diagnostic.write_text(
                    json.dumps(
                        {
                            "srt_path": str(sample_srt),
                            "confidence_segments": [
                                {
                                    "start": 0.5,
                                    "end": 2.0,
                                    "avg_logprob": -0.2,
                                    "no_speech_prob": 0.05,
                                    "compression_ratio": 1.1,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            with (
                patch("transcriber.subprocess.run", side_effect=run_ffmpeg),
                patch("transcriber.transcribe_to_srt", side_effect=transcribe_clip),
            ):
                result = repair_low_confidence_ranges(
                    audio,
                    output,
                    [(10.0, 20.0)],
                    config,
                    logging.getLogger("test.transcriber.selective-confidence"),
                )

            self.assertEqual(observed_prompts, [(None, None)])
            self.assertEqual(len(result.segment_confidences), 1)
            confidence = result.segment_confidences[0]
            self.assertEqual((confidence.start, confidence.end), (10.5, 12.0))
            self.assertEqual(confidence.avg_logprob, -0.2)
            self.assertEqual(confidence.no_speech_prob, 0.05)
            self.assertEqual(confidence.compression_ratio, 1.1)

    def test_confidence_triggered_selective_finalization_rejects_missing_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "episode.wav"
            audio.write_bytes(b"not-a-real-wave")
            output = root / "episode.srt"
            write_srt(
                output,
                [
                    SrtBlock(
                        1,
                        "00:00:00,600 --> 00:00:02,000",
                        ["selective repair without diagnostics"],
                    )
                ],
            )
            config = _final_quality_config(root)
            config.transcription_quality_min_confidence_segments = 1

            with self.assertRaisesRegex(
                TranscriptionError,
                "confidence validation is unavailable",
            ):
                finalize_repaired_transcription(
                    audio,
                    output,
                    [(0.0, 4.0)],
                    config,
                    logging.getLogger("test.transcriber.missing-confidence"),
                    segment_confidences=(),
                    require_confidence=True,
                )

            diagnostic = next((root / "diagnostics").glob("*.json"))
            payload = json.loads(diagnostic.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "selective_repair_rejected")

    def test_repeated_vocalization_is_removed(self) -> None:
        self.assertTrue(_is_repeated_vocalization_text("うおおおおおおおおお", CONFIG))
        self.assertEqual(_clean_transcribed_text("うおおおおおおおおお", CONFIG), "")

    def test_vocalization_prefix_is_trimmed_when_dialogue_remains(self) -> None:
        self.assertEqual(_clean_transcribed_text("おおおおおおおこれでどうだ!", CONFIG), "これでどうだ!")

    def test_short_reaction_is_kept(self) -> None:
        self.assertEqual(_clean_transcribed_text("うおっ", CONFIG), "うおっ")

    def test_regular_dialogue_is_kept(self) -> None:
        text = "傲慢なファウストの猟犬が"
        self.assertEqual(_clean_transcribed_text(text, CONFIG), text)

    def test_common_whisper_credit_hallucinations_are_removed(self) -> None:
        self.assertTrue(_is_hallucination_text("字幕製作人 初音未來", CONFIG))
        self.assertTrue(_is_hallucination_text("字幕作成者 初音ミク", CONFIG))
        self.assertTrue(_is_hallucination_text("作詞・作曲・編曲 初音ミク", CONFIG))
        self.assertTrue(_is_hallucination_text("この動画の字幕は視聴者によって作成されました。", CONFIG))
        self.assertEqual(
            asr_artifact_line_indexes(["この動画の字幕は視聴者によって作成されました。"], CONFIG),
            {0},
        )
        self.assertEqual(_clean_transcribed_text("字幕製作人 初音未來", CONFIG), "")
        self.assertEqual(_clean_transcribed_text("この動画の字幕は視聴者によって作成されました。", CONFIG), "")

    def test_real_dialogue_with_hatsune_miku_name_is_kept(self) -> None:
        text = "初音ミクの曲が好きです"
        self.assertFalse(_is_hallucination_text(text, CONFIG))
        self.assertEqual(_clean_transcribed_text(text, CONFIG), text)

    def test_fragmented_initial_prompt_echo_is_removed_as_one_window(self) -> None:
        chunks = [
            (-1.0, -0.1, "はぁ……"),
            (0.0, 1.0, "日本ニメ"),
            (1.1, 2.0, "ング"),
            (2.1, 3.0, "挿入"),
            (3.1, 4.0, "歌。"),
            (4.1, 6.0, "今日はいい天気だ。"),
        ]

        indexes = asr_prompt_echo_line_indexes([item[2] for item in chunks], CONFIG)
        filtered = _filter_asr_prompt_echo_chunks(chunks, CONFIG)

        self.assertEqual(indexes, {1, 2, 3, 4})
        self.assertEqual(filtered, [chunks[0], chunks[-1]])

    def test_normal_dialogue_about_a_song_is_not_prompt_echo(self) -> None:
        text = "このアニメの歌が大好きです"
        self.assertIsNone(asr_prompt_echo_reason(text, CONFIG))

    def test_normal_opening_dialogue_is_not_removed_as_prompt_or_artifact(self) -> None:
        samples = (
            "この物語は、ある少女の出会いから始まる。",
            "字幕が見えないの？",
            "日本語で話してください。",
            "オープニングからずっと見ていたよ。",
            "エンディングはまだ先だ。",
            "その台詞、省略しないでください。",
            "歌詞を正確に覚えています。",
        )

        for text in samples:
            with self.subTest(text=text):
                self.assertIsNone(asr_prompt_echo_reason(text, CONFIG))
                self.assertEqual(asr_artifact_line_indexes([text], CONFIG), set())
                self.assertEqual(_clean_transcribed_text(text, CONFIG), text)

    def test_fragmented_normal_opening_dialogue_is_not_removed(self) -> None:
        samples = (
            ["日本語で", "話してください。", "今日は大切な話があります。"],
            ["このアニメの", "オープニングが", "大好きです。"],
            ["その台詞は", "省略しないで", "正確に伝えてください。"],
            ["字幕が", "見えないの？", "近くに来て。"],
        )

        for texts in samples:
            with self.subTest(texts=texts):
                self.assertEqual(asr_artifact_line_indexes(texts, CONFIG), set())

    def test_fragmented_viewing_and_subscription_hallucination_is_removed(self) -> None:
        texts = [
            "字幕オンしてご視",
            "聴ますし",
            "ければチャンネル登録をお願いいた",
            "します。",
            "本当の台詞です。",
        ]

        self.assertEqual(asr_artifact_line_indexes(texts), {0, 1, 2, 3})

    def test_fragmented_thanks_for_watching_hallucination_is_removed(self) -> None:
        texts = ["ご視", "聴有", "う", "御", "座いました。", "本当の台詞です。"]

        self.assertEqual(asr_artifact_line_indexes(texts), {0, 1, 2, 3, 4})

    def test_filter_reports_removed_artifact_timestamps(self) -> None:
        chunks = [
            (2.0, 4.0, "字幕オンしてご視"),
            (4.1, 6.0, "聴ください。"),
            (12.0, 14.0, "本当の台詞です。"),
        ]
        removed: list[tuple[float, float]] = []

        filtered = _filter_asr_prompt_echo_chunks(
            chunks,
            CONFIG,
            removed_ranges=removed,
        )

        self.assertEqual(filtered, [chunks[-1]])
        self.assertEqual(removed, [(2.0, 6.0)])

    def test_artifact_repair_range_expands_to_clean_neighbor_boundaries(self) -> None:
        config = SimpleNamespace(
            asr_selective_retry_padding_seconds=1.5,
            asr_selective_retry_merge_gap_seconds=3.0,
        )
        retained = [
            (0.0, 2.0, "前の台詞"),
            (20.0, 22.0, "次の台詞"),
        ]

        self.assertEqual(
            _artifact_review_ranges([(3.0, 8.0)], retained, config),
            [(2.0, 20.0)],
        )

    def test_leading_gap_uses_stricter_dedicated_threshold(self) -> None:
        config = SimpleNamespace(
            enable_gap_rescue=False,
            enable_leading_gap_rescue=True,
            gap_rescue_threshold_seconds=4.0,
            gap_rescue_leading_threshold_seconds=1.5,
            gap_rescue_leading_max_seconds=35.0,
            gap_rescue_max_gap_seconds=12.0,
        )
        chunks = [
            (2.5, 4.0, "first detected line"),
            (7.0, 8.0, "three second internal pause"),
        ]

        self.assertEqual(_select_rescue_gaps(chunks, config), [(0.0, 2.5)])

    def test_long_leading_gap_is_split_into_overlapping_rescue_clips(self) -> None:
        config = SimpleNamespace(
            gap_rescue_clip_seconds=30.0,
            gap_rescue_clip_overlap_seconds=2.0,
        )

        self.assertEqual(
            _gap_rescue_clips(0.0, 80.0, config),
            [(0.0, 30.0), (28.0, 58.0), (56.0, 80.0)],
        )

    def test_gap_rescue_uses_strict_post_decode_quality_gate(self) -> None:
        config = SimpleNamespace(
            gap_rescue_accept_min_avg_logprob=-1.15,
            gap_rescue_accept_max_no_speech_prob=0.90,
            gap_rescue_accept_max_compression_ratio=2.4,
        )

        self.assertIsNone(
            _gap_rescue_segment_rejection_reason(
                SimpleNamespace(avg_logprob=-0.42, no_speech_prob=0.12, compression_ratio=1.4),
                config,
            )
        )
        self.assertIn(
            "avg_logprob",
            _gap_rescue_segment_rejection_reason(
                SimpleNamespace(avg_logprob=-1.31, no_speech_prob=0.22, compression_ratio=1.4),
                config,
            )
            or "",
        )
        self.assertIn(
            "compression_ratio",
            _gap_rescue_segment_rejection_reason(
                SimpleNamespace(avg_logprob=-0.42, no_speech_prob=0.12, compression_ratio=2.8),
                config,
            )
            or "",
        )
        self.assertIn(
            "no_speech_prob",
            _gap_rescue_segment_rejection_reason(
                SimpleNamespace(avg_logprob=-0.72, no_speech_prob=0.96, compression_ratio=1.4),
                config,
            )
            or "",
        )

    def test_excessive_leading_gap_requests_selective_retranscription(self) -> None:
        config = SimpleNamespace(
            transcription_quality_check_enabled=True,
            transcription_quality_min_audio_seconds=600.0,
            transcription_quality_min_coverage_percent=8.0,
            transcription_quality_min_blocks_per_minute=1.5,
            transcription_quality_min_avg_logprob=-1.0,
            transcription_quality_max_low_confidence_percent=25.0,
            transcription_quality_min_confidence_segments=3,
            transcription_quality_max_leading_gap_seconds=30.0,
            enable_leading_gap_rescue=True,
            gap_rescue_leading_max_seconds=120.0,
        )

        with self.assertRaises(LowConfidenceTranscriptionError) as caught:
            _validate_transcription_quality(
                "missing.wav",
                [(45.0, 47.0, "first detected line")],
                config,
                logging.getLogger("test.transcriber.leading-gap"),
            )

        self.assertEqual(caught.exception.reason_code, "leading_gap")
        self.assertEqual(caught.exception.review_ranges, [(0.0, 45.0)])

    def test_shared_final_srt_gate_rejects_sparse_backend_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "sparse.srt"
            write_srt(
                output,
                [SrtBlock(1, "00:00:00,000 --> 00:00:01,000", ["one line"])],
            )

            with (
                patch("transcriber._wav_duration_seconds", return_value=1200.0),
                self.assertRaisesRegex(
                    TranscriptionError,
                    "coverage .*blocks_per_minute",
                ),
            ):
                validate_transcription_srt_quality(
                    root / "episode.wav",
                    output,
                    _final_quality_config(root),
                    logging.getLogger("test.transcriber.final-srt-quality"),
                )

    def test_low_confidence_segments_fail_asr_quality_check(self) -> None:
        config = SimpleNamespace(
            transcription_quality_check_enabled=True,
            transcription_quality_min_audio_seconds=600.0,
            transcription_quality_min_coverage_percent=8.0,
            transcription_quality_min_blocks_per_minute=1.5,
            transcription_quality_min_avg_logprob=-1.0,
            transcription_quality_max_low_confidence_percent=25.0,
            transcription_quality_min_confidence_segments=3,
        )
        confidences = [
            SegmentConfidence(0, 1, -1.4, 0.1, 1.0),
            SegmentConfidence(1, 2, -1.2, 0.1, 1.0),
            SegmentConfidence(2, 3, -0.4, 0.1, 1.0),
        ]

        with self.assertRaisesRegex(TranscriptionError, "low_confidence_segments"):
            _validate_transcription_quality(
                "missing.wav",
                [(0.0, 1.0, "test")],
                config,
                logging.getLogger("test.transcriber.quality"),
                segment_confidences=confidences,
            )

    def test_low_confidence_ranges_are_padded_and_nearby_segments_are_merged(self) -> None:
        config = SimpleNamespace(
            asr_selective_retry_padding_seconds=1.5,
            asr_selective_retry_merge_gap_seconds=3.0,
        )
        ranges = _low_confidence_review_ranges(
            [
                SegmentConfidence(0, 1, -1.4, 0.1, 1.0),
                SegmentConfidence(2, 3, -1.2, 0.1, 1.0),
                SegmentConfidence(10, 11, -1.3, 0.1, 1.0),
            ],
            config,
        )
        self.assertEqual(ranges, [(0.0, 4.5), (8.5, 12.5)])

    def test_quality_failure_exposes_selective_review_ranges(self) -> None:
        config = SimpleNamespace(
            transcription_quality_check_enabled=True,
            transcription_quality_min_audio_seconds=600.0,
            transcription_quality_min_coverage_percent=8.0,
            transcription_quality_min_blocks_per_minute=1.5,
            transcription_quality_min_avg_logprob=-1.0,
            transcription_quality_max_low_confidence_percent=25.0,
            transcription_quality_min_confidence_segments=3,
            asr_selective_retry_padding_seconds=1.0,
            asr_selective_retry_merge_gap_seconds=1.0,
        )
        confidences = [
            SegmentConfidence(0, 1, -1.4, 0.1, 1.0),
            SegmentConfidence(5, 6, -1.2, 0.1, 1.0),
            SegmentConfidence(8, 9, -0.4, 0.1, 1.0),
        ]
        with self.assertRaises(LowConfidenceTranscriptionError) as caught:
            _validate_transcription_quality(
                "missing.wav",
                [(0.0, 1.0, "test")],
                config,
                logging.getLogger("test.transcriber.selective"),
                segment_confidences=confidences,
            )
        self.assertEqual(caught.exception.review_ranges, [(0.0, 2.0), (4.0, 7.0)])

    def test_op_ed_rescue_ranges_only_cover_opening_and_ending_gaps(self) -> None:
        config = SimpleNamespace(
            op_ed_min_audio_seconds=600.0,
            op_ed_opening_window_seconds=360.0,
            op_ed_ending_window_seconds=300.0,
            op_ed_gap_threshold_seconds=6.0,
            op_ed_max_gap_seconds=210.0,
            op_ed_max_rescue_ranges=6,
        )
        chunks = [
            (0.0, 30.0, "cold open"),
            (120.0, 500.0, "episode dialogue"),
            (1000.0, 1260.0, "episode dialogue"),
        ]

        ranges = _select_op_ed_rescue_ranges(chunks, 1440.0, config)

        self.assertEqual(ranges, [(30.0, 120.0), (1260.0, 1440.0)])

    def test_op_ed_rescue_is_not_used_for_short_video(self) -> None:
        config = SimpleNamespace(
            op_ed_min_audio_seconds=600.0,
            op_ed_opening_window_seconds=360.0,
            op_ed_ending_window_seconds=300.0,
            op_ed_gap_threshold_seconds=6.0,
            op_ed_max_gap_seconds=210.0,
            op_ed_max_rescue_ranges=6,
        )

        self.assertEqual(_select_op_ed_rescue_ranges([], 120.0, config), [])

    def test_op_ed_range_limit_never_drops_opening_or_ending_boundary(self) -> None:
        config = SimpleNamespace(
            op_ed_min_audio_seconds=600.0,
            op_ed_opening_window_seconds=400.0,
            op_ed_ending_window_seconds=300.0,
            op_ed_gap_threshold_seconds=6.0,
            op_ed_max_gap_seconds=210.0,
            op_ed_max_rescue_ranges=2,
        )
        chunks = [
            (10.0, 20.0, "opening line"),
            (100.0, 110.0, "opening line"),
            (200.0, 210.0, "opening line"),
            (300.0, 400.0, "dialogue"),
            (700.0, 900.0, "dialogue"),
        ]

        self.assertEqual(
            _select_op_ed_rescue_ranges(chunks, 1000.0, config),
            [(0.0, 10.0), (900.0, 1000.0)],
        )

    def test_op_ed_rescue_uses_lyrics_prompt_and_fills_only_uncovered_time(self) -> None:
        config = SimpleNamespace(
            op_ed_min_audio_seconds=600.0,
            op_ed_opening_window_seconds=360.0,
            op_ed_ending_window_seconds=300.0,
            op_ed_gap_threshold_seconds=6.0,
            op_ed_max_gap_seconds=210.0,
            op_ed_padding_seconds=1.0,
            op_ed_max_rescue_ranges=6,
            op_ed_no_speech_threshold=0.95,
            op_ed_log_prob_threshold=-1.5,
            op_ed_compression_ratio_threshold=3.0,
            op_ed_initial_prompt="Japanese anime song lyrics",
            gap_rescue_clip_seconds=30.0,
            gap_rescue_clip_overlap_seconds=2.0,
            whisper_language="ja",
            whisper_task="transcribe",
            whisper_beam_size=5,
            whisper_best_of=5,
            whisper_patience=1.0,
            whisper_length_penalty=1.0,
            whisper_repetition_penalty=1.0,
            whisper_no_repeat_ngram_size=5,
            whisper_hallucination_silence_threshold=None,
            whisper_hallucination_phrases=[],
            subtitle_timing_mode="segment",
        )
        chunks = [
            (0.0, 30.0, "cold open"),
            (120.0, 1140.0, "episode dialogue"),
            (1140.0, 1440.0, "ending already covered"),
        ]
        segment = SimpleNamespace(start=40.0, end=45.0, text="歌詞です", words=[])
        model = SimpleNamespace()
        calls: list[tuple[str, dict]] = []

        def transcribe(audio_path: str, **kwargs):
            calls.append((audio_path, kwargs))
            return [segment], SimpleNamespace()

        model.transcribe = transcribe
        with patch("transcriber._wav_duration_seconds", return_value=1440.0):
            rescued = _rescue_op_ed_lyrics(
                model,
                "episode.wav",
                chunks,
                config,
                logging.getLogger("test.transcriber.op-ed"),
            )

        self.assertEqual(rescued, [(40.0, 45.0, "歌詞です")])
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            [call[1]["clip_timestamps"] for call in calls],
            ["29.000,59.000", "57.000,87.000", "85.000,115.000", "113.000,121.000"],
        )
        self.assertTrue(all(call[1]["initial_prompt"] == "Japanese anime song lyrics" for call in calls))
        self.assertTrue(all(not call[1]["vad_filter"] for call in calls))
        self.assertTrue(all(call[1]["no_speech_threshold"] == 0.95 for call in calls))

    def test_op_ed_rescue_rejects_one_character_but_keeps_two_character_chunk(
        self,
    ) -> None:
        config = SimpleNamespace(
            op_ed_min_audio_seconds=600.0,
            op_ed_opening_window_seconds=360.0,
            op_ed_ending_window_seconds=300.0,
            op_ed_gap_threshold_seconds=6.0,
            op_ed_max_gap_seconds=210.0,
            op_ed_padding_seconds=1.0,
            op_ed_max_rescue_ranges=6,
            op_ed_no_speech_threshold=0.95,
            op_ed_log_prob_threshold=-1.5,
            op_ed_compression_ratio_threshold=3.0,
            op_ed_initial_prompt=None,
            gap_rescue_clip_seconds=30.0,
            gap_rescue_clip_overlap_seconds=2.0,
            gap_rescue_min_chars=2,
            whisper_language="ja",
            whisper_task="transcribe",
            whisper_beam_size=5,
            whisper_best_of=5,
            whisper_patience=1.0,
            whisper_length_penalty=1.0,
            whisper_repetition_penalty=1.0,
            whisper_no_repeat_ngram_size=5,
            whisper_hallucination_silence_threshold=None,
            whisper_hallucination_phrases=[],
            subtitle_timing_mode="segment",
        )
        segments = [
            SimpleNamespace(start=40.0, end=42.0, text="づ", words=[]),
            SimpleNamespace(start=44.0, end=46.0, text="歌詞", words=[]),
        ]
        model = SimpleNamespace(
            transcribe=lambda *_args, **_kwargs: (segments, SimpleNamespace())
        )

        with patch("transcriber._wav_duration_seconds", return_value=1440.0):
            rescued = _rescue_op_ed_lyrics(
                model,
                "episode.wav",
                [(0.0, 30.0, "cold open"), (120.0, 1440.0, "dialogue")],
                config,
                logging.getLogger("test.transcriber.op-ed-min-chars"),
            )

        self.assertEqual(rescued, [(44.0, 46.0, "歌詞")])

    def test_op_ed_rescue_does_not_send_empty_initial_prompt(self) -> None:
        config = SimpleNamespace(
            op_ed_min_audio_seconds=600.0,
            op_ed_opening_window_seconds=360.0,
            op_ed_ending_window_seconds=300.0,
            op_ed_gap_threshold_seconds=6.0,
            op_ed_max_gap_seconds=210.0,
            op_ed_padding_seconds=1.0,
            op_ed_max_rescue_ranges=6,
            op_ed_no_speech_threshold=0.95,
            op_ed_log_prob_threshold=-1.5,
            op_ed_compression_ratio_threshold=3.0,
            op_ed_initial_prompt=None,
            gap_rescue_clip_seconds=30.0,
            gap_rescue_clip_overlap_seconds=2.0,
            whisper_language="ja",
            whisper_task="transcribe",
            whisper_beam_size=5,
            whisper_best_of=5,
            whisper_patience=1.0,
            whisper_length_penalty=1.0,
            whisper_repetition_penalty=1.0,
            whisper_no_repeat_ngram_size=5,
            whisper_hallucination_silence_threshold=None,
            whisper_hallucination_phrases=[],
            subtitle_timing_mode="segment",
        )
        segment = SimpleNamespace(start=40.0, end=45.0, text="本当の歌詞です", words=[])
        calls: list[dict] = []

        def transcribe(_audio_path: str, **kwargs):
            calls.append(kwargs)
            return [segment], SimpleNamespace()

        model = SimpleNamespace(transcribe=transcribe)
        with patch("transcriber._wav_duration_seconds", return_value=1440.0):
            rescued = _rescue_op_ed_lyrics(
                model,
                "episode.wav",
                [(0.0, 30.0, "cold open"), (120.0, 1440.0, "dialogue")],
                config,
                logging.getLogger("test.transcriber.op-ed-no-prompt"),
            )

        self.assertEqual(rescued, [(40.0, 45.0, "本当の歌詞です")])
        self.assertEqual(len(calls), 4)
        self.assertTrue(all("initial_prompt" not in call for call in calls))

    def test_op_ed_low_confidence_segment_is_rejected(self) -> None:
        config = SimpleNamespace(
            op_ed_accept_min_avg_logprob=-1.15,
            op_ed_accept_max_no_speech_prob=0.90,
            op_ed_accept_max_compression_ratio=2.4,
            whisper_initial_prompt=None,
            op_ed_initial_prompt=None,
        )
        segment = SimpleNamespace(
            start=10.0,
            end=12.0,
            text="聞き取れない歌詞",
            avg_logprob=-1.40,
            no_speech_prob=0.10,
            compression_ratio=1.20,
        )

        self.assertIn("avg_logprob", _op_ed_segment_rejection_reason(segment, config) or "")


if __name__ == "__main__":
    unittest.main()
