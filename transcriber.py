from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
import copy
from difflib import SequenceMatcher
import hashlib
import json
import logging
import math
import os
import re
import site
import subprocess
import tempfile
import time
from typing import Any
import wave

from asr_quality import asr_artifact_line_indexes, asr_artifact_reason
from config import AppConfig
from safe_files import atomic_write_text
from srt_utils import SrtBlock, read_srt, write_srt
from whisper_runtime import get_whisper_model


class TranscriptionError(RuntimeError):
    pass


class LowConfidenceTranscriptionError(TranscriptionError):
    def __init__(
        self,
        message: str,
        review_ranges: list[tuple[float, float]],
        *,
        reason_code: str = "low_confidence",
    ) -> None:
        super().__init__(message)
        self.review_ranges = review_ranges
        self.reason_code = reason_code
        self.asr_context: dict[str, Any] = {}


class AsrSelectiveRepairUnavailableError(TranscriptionError):
    """A cached ASR rejection cannot be repaired from trustworthy evidence."""

    def __init__(
        self,
        message: str,
        review_ranges: list[tuple[float, float]] | None = None,
        *,
        reason_code: str = "untrusted_cache",
        asr_context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.review_ranges = list(review_ranges or [])
        self.reason_code = str(reason_code or "untrusted_cache")
        self.asr_context = dict(asr_context or {})


@dataclass(frozen=True)
class SegmentConfidence:
    start: float
    end: float
    avg_logprob: float | None
    no_speech_prob: float | None
    compression_ratio: float | None


@dataclass(frozen=True)
class SelectiveRepairResult:
    path: Path
    segment_confidences: tuple[SegmentConfidence, ...]
    confirmed_silent_ranges: tuple[tuple[float, float], ...] = ()


VOCALIZATION_CHARS = set("うおあえぁぃぅぇぉオウアエォーっッんン!?！？…。、，,・ ")
MEANINGFUL_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9]")
DEFAULT_WHISPER_HALLUCINATION_PHRASES = (
    "字幕作成者 初音ミク",
    "字幕製作者 初音ミク",
    "字幕製作人 初音未來",
    "字幕制作人 初音未来",
    "作詞・作曲・編曲 初音ミク",
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "ご視聴ありがとうございます",
    "チャンネル登録よろしくお願いします",
)
DEFAULT_WHISPER_HALLUCINATION_CREDIT_NAMES = (
    "初音ミク",
    "初音未來",
    "初音未来",
    "hatsunemiku",
)
DEFAULT_WHISPER_HALLUCINATION_CREDIT_WORDS = (
    "字幕",
    "作成",
    "作成者",
    "製作",
    "制作",
    "作詞",
    "作曲",
    "編曲",
    "編集",
    "subtitle",
    "subtitles",
)

SELECTIVE_SILENCE_MAX_RMS_DBFS = -50.0
TAIL_ARTIFACT_MAX_END_GAP_SECONDS = 0.75
TAIL_ARTIFACT_CONTEXT_SECONDS = 12.0
TAIL_SPEECH_COVERAGE_TOLERANCE_SECONDS = 0.15


def transcribe_to_srt(
    audio_path: str | Path,
    srt_path: str | Path,
    config: AppConfig,
    logger: logging.Logger,
) -> Path:
    _add_nvidia_dll_directories(logger)

    try:
        model = get_whisper_model(
            config.whisper_model,
            device=config.whisper_device,
            compute_type=config.whisper_compute_type,
            cache_enabled=bool(getattr(config, "whisper_model_cache_enabled", True)),
            logger=logger,
        )
        transcribe_options = {
            "language": config.whisper_language,
            "task": config.whisper_task,
            "vad_filter": config.whisper_vad_filter,
            "vad_parameters": _build_vad_parameters(config),
            "condition_on_previous_text": config.whisper_condition_on_previous_text,
            "temperature": config.whisper_temperature,
            "beam_size": config.whisper_beam_size,
            "best_of": config.whisper_best_of,
            "patience": config.whisper_patience,
            "length_penalty": config.whisper_length_penalty,
            "repetition_penalty": config.whisper_repetition_penalty,
            "no_repeat_ngram_size": config.whisper_no_repeat_ngram_size,
            "word_timestamps": config.whisper_word_timestamps,
            "no_speech_threshold": config.whisper_no_speech_threshold,
            "log_prob_threshold": config.whisper_log_prob_threshold,
            "compression_ratio_threshold": config.whisper_compression_ratio_threshold,
            "hallucination_silence_threshold": config.whisper_hallucination_silence_threshold,
        }
        if config.whisper_initial_prompt:
            transcribe_options["initial_prompt"] = config.whisper_initial_prompt

        segments, _info = model.transcribe(str(audio_path), **transcribe_options)

        raw_segments: list[tuple[float, float, str]] = []
        primary_artifact_ranges: list[tuple[float, float]] = []
        optional_rescue_artifact_ranges: list[tuple[float, float]] = []
        rejected_rescue_ranges: list[tuple[float, float]] = []
        accepted_recovery_segments: list[tuple[float, float, str]] = []
        op_ed_probe_evidence: list[dict[str, Any]] = []
        segment_confidences: list[SegmentConfidence] = []
        for segment in segments:
            artifact = _segment_artifact(segment, config)
            if artifact is not None:
                reason, artifact_range, text = artifact
                primary_artifact_ranges.append(artifact_range)
                logger.error(
                    "Removed ASR artifact segment during primary transcription "
                    "range=%.2f-%.2fs reason=%s text=%r",
                    artifact_range[0],
                    artifact_range[1],
                    reason,
                    text[:160],
                )
                continue
            confidence = _segment_confidence(segment, config)
            if confidence is not None:
                segment_confidences.append(confidence)
            raw_segments.extend(_segment_to_chunks(segment, config))

        raw_segments = _normalize_timing(raw_segments, config)
        raw_segments = _filter_asr_prompt_echo_chunks(
            raw_segments,
            config,
            logger,
            stage="primary transcription",
            removed_ranges=primary_artifact_ranges,
        )
        if bool(getattr(config, "enable_gap_rescue", True)) or bool(
            getattr(config, "enable_leading_gap_rescue", True)
        ):
            rescue_segments = _rescue_gaps(
                model,
                audio_path,
                raw_segments,
                config,
                logger,
                removed_artifact_ranges=optional_rescue_artifact_ranges,
                rejected_quality_ranges=rejected_rescue_ranges,
            )
            if rescue_segments:
                logger.info("Gap rescue accepted %s subtitle chunk(s).", len(rescue_segments))
                accepted_recovery_segments.extend(
                    _filter_asr_prompt_echo_chunks(rescue_segments, config)
                )
                raw_segments = _normalize_timing([*raw_segments, *rescue_segments], config)
                raw_segments = _filter_asr_prompt_echo_chunks(
                    raw_segments,
                    config,
                    logger,
                    stage="gap rescue",
                    removed_ranges=optional_rescue_artifact_ranges,
                )

        if bool(getattr(config, "op_ed_transcription_enabled", True)):
            lyric_segments = _rescue_op_ed_lyrics(
                model,
                audio_path,
                raw_segments,
                config,
                logger,
                removed_artifact_ranges=optional_rescue_artifact_ranges,
                rejected_quality_ranges=rejected_rescue_ranges,
                probe_evidence=op_ed_probe_evidence,
            )
            if lyric_segments:
                logger.info("OP/ED lyrics rescue accepted %s subtitle chunk(s).", len(lyric_segments))
                accepted_recovery_segments.extend(
                    _filter_asr_prompt_echo_chunks(lyric_segments, config)
                )
                raw_segments = _normalize_timing([*raw_segments, *lyric_segments], config)
                raw_segments = _filter_asr_prompt_echo_chunks(
                    raw_segments,
                    config,
                    logger,
                    stage="OP/ED rescue",
                    removed_ranges=optional_rescue_artifact_ranges,
                )

        (
            confirmed_tail_artifact_ranges,
            tail_consensus_evidence,
        ) = _prompt_free_tail_artifact_consensus(
            audio_path,
            primary_artifact_ranges,
            raw_segments,
            accepted_recovery_segments,
            op_ed_probe_evidence,
            config,
        )
        recovered_primary_artifact_ranges: list[tuple[float, float]] = []
        unresolved_primary_artifact_ranges = list(primary_artifact_ranges)
        if bool(
            getattr(
                config,
                "asr_prompt_free_allow_recovered_primary_artifacts",
                False,
            )
        ):
            (
                recovered_primary_artifact_ranges,
                unresolved_primary_artifact_ranges,
            ) = _partition_artifact_ranges_by_rescue_evidence(
                primary_artifact_ranges,
                accepted_recovery_segments,
            )
        # A terminal artifact is never cleared merely because a rescue pass
        # emitted text over it.  Real tail dialogue, ambiguous lyrics, or a
        # missing second-pass proof must remain in review.  Only the stricter
        # prompt-free tail consensus below may omit the exact silent artifact.
        duration = _wav_duration_seconds(audio_path)
        terminal_primary_artifacts = set(
            _terminal_artifact_ranges(primary_artifact_ranges, duration)
        )
        recovered_terminal_artifacts = [
            item
            for item in recovered_primary_artifact_ranges
            if item in terminal_primary_artifacts
        ]
        if recovered_terminal_artifacts:
            recovered_primary_artifact_ranges = [
                item
                for item in recovered_primary_artifact_ranges
                if item not in terminal_primary_artifacts
            ]
            unresolved_primary_artifact_ranges.extend(recovered_terminal_artifacts)
        confirmed_tail_set = set(confirmed_tail_artifact_ranges)
        unresolved_primary_artifact_ranges = [
            item
            for item in _normalize_review_ranges(unresolved_primary_artifact_ranges)
            if item not in confirmed_tail_set
        ]
        primary_artifact_review_ranges = _artifact_review_ranges(
            unresolved_primary_artifact_ranges,
            raw_segments,
            config,
        )
        recovered_primary_review_ranges = _artifact_review_ranges(
            recovered_primary_artifact_ranges,
            raw_segments,
            config,
        )
        confirmed_tail_review_ranges = _artifact_review_ranges(
            confirmed_tail_artifact_ranges,
            raw_segments,
            config,
        )
        optional_rescue_review_ranges = _artifact_review_ranges(
            [*optional_rescue_artifact_ranges, *rejected_rescue_ranges],
            raw_segments,
            config,
        )
        strict_optional_rescue = bool(
            getattr(config, "asr_optional_rescue_rejection_is_fatal", True)
        )
        required_review_ranges = _normalize_review_ranges(
            [
                *primary_artifact_review_ranges,
                *(optional_rescue_review_ranges if strict_optional_rescue else []),
            ]
        )
        blocks: list[SrtBlock] = []
        for start, end, text in raw_segments:
            text_lines = [line.strip() for line in text.splitlines() if line.strip()]
            blocks.append(
                SrtBlock(
                    index=len(blocks) + 1,
                    timing=f"{_format_timestamp(start)} --> {_format_timestamp(end)}",
                    text=text_lines,
                )
            )

        review_error: LowConfidenceTranscriptionError | None = None
        if required_review_ranges:
            ranges_text = ",".join(
                f"{start:.1f}-{end:.1f}s"
                for start, end in required_review_ranges[:8]
            )
            reason_code = (
                "asr_artifact"
                if primary_artifact_review_ranges
                else "rescue_low_confidence"
            )
            review_error = LowConfidenceTranscriptionError(
                "ASR artifacts or low-confidence rescue candidates were rejected; "
                "the affected audio must be "
                f"re-transcribed without a prompt: ranges={ranges_text}",
                required_review_ranges,
                reason_code=reason_code,
            )
        if not blocks:
            if review_error is not None:
                raise review_error
            raise TranscriptionError("Whisper returned no subtitle segments.")

        output = Path(srt_path)
        try:
            if review_error is not None:
                raise review_error
            _validate_transcription_quality(
                audio_path,
                raw_segments,
                config,
                logger,
                segment_confidences=segment_confidences,
            )
        except LowConfidenceTranscriptionError as exc:
            # Keep the usable primary transcript so the worker can replace
            # only suspect windows with the alternate model.
            write_srt(output, blocks)
            _write_asr_diagnostics(
                output,
                audio_path,
                raw_segments,
                segment_confidences,
                config,
                status="selective_retry_required",
                review_ranges=exc.review_ranges,
                reason_code=exc.reason_code,
                confirmed_tail_artifact_ranges=confirmed_tail_artifact_ranges,
                tail_consensus_evidence=tail_consensus_evidence,
            )
            raise
        write_srt(output, blocks)
        ignored_review_ranges = _normalize_review_ranges(
            [
                *recovered_primary_review_ranges,
                *confirmed_tail_review_ranges,
                *(
                    optional_rescue_review_ranges
                    if optional_rescue_review_ranges and not strict_optional_rescue
                    else []
                ),
            ]
        )
        ignored_reason_code = ""
        if confirmed_tail_review_ranges:
            ignored_reason_code = "prompt_free_tail_artifact_consensus"
        elif recovered_primary_review_ranges and optional_rescue_review_ranges:
            ignored_reason_code = "prompt_free_recovered_primary_and_optional_rescue"
        elif recovered_primary_review_ranges:
            ignored_reason_code = "prompt_free_recovered_primary_artifacts"
        elif optional_rescue_review_ranges and not strict_optional_rescue:
            ignored_reason_code = "optional_rescue_rejections_ignored"
        accepted_diagnostic = _write_asr_diagnostics(
            output,
            audio_path,
            raw_segments,
            segment_confidences,
            config,
            status="accepted",
            review_ranges=ignored_review_ranges or None,
            reason_code=ignored_reason_code,
            confirmed_tail_artifact_ranges=confirmed_tail_artifact_ranges,
            tail_consensus_evidence=tail_consensus_evidence,
        )
        if confirmed_tail_artifact_ranges and accepted_diagnostic is None:
            output.unlink(missing_ok=True)
            raise TranscriptionError(
                "Prompt-free tail artifact consensus refused publication because "
                "its durable diagnostic could not be written"
            )
        if recovered_primary_review_ranges:
            logger.warning(
                "Accepted prompt-free ASR after clean rescue evidence repaired %s "
                "primary artifact range(s).",
                len(recovered_primary_review_ranges),
            )
        if optional_rescue_review_ranges and not strict_optional_rescue:
            logger.warning(
                "Accepted prompt-free ASR after safely discarding %s optional "
                "gap/OP-ED rescue range(s).",
                len(optional_rescue_review_ranges),
            )
        if confirmed_tail_review_ranges:
            logger.warning(
                "Accepted prompt-free ASR after two-pass tail consensus safely "
                "omitted %s terminal artifact range(s).",
                len(confirmed_tail_artifact_ranges),
            )
        if config.write_gap_report:
            _write_gap_report(output, raw_segments, config)
        logger.info("Created Japanese SRT: %s", output)
        return output
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(f"Whisper transcription failed for {audio_path}: {exc}") from exc


def _format_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000
    minutes = milliseconds // 60_000
    milliseconds %= 60_000
    secs = milliseconds // 1_000
    millis = milliseconds % 1_000
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _segment_to_chunks(segment: Any, config: AppConfig) -> list[tuple[float, float, str]]:
    text = (getattr(segment, "text", None) or "").strip()
    if not text or _is_hallucination_text(text, config):
        return []

    words = _extract_words(segment)
    if config.subtitle_timing_mode == "word" and words:
        return _split_word_chunks(words, config)

    return [(float(segment.start), float(segment.end), text)]


def _segment_confidence(segment: Any, config: AppConfig) -> SegmentConfidence | None:
    text = (getattr(segment, "text", None) or "").strip()
    if not text or _is_hallucination_text(text, config):
        return None
    try:
        start = float(segment.start)
        end = float(segment.end)
    except (AttributeError, TypeError, ValueError):
        return None
    return SegmentConfidence(
        start=start,
        end=end,
        avg_logprob=_optional_segment_float(segment, "avg_logprob"),
        no_speech_prob=_optional_segment_float(segment, "no_speech_prob"),
        compression_ratio=_optional_segment_float(segment, "compression_ratio"),
    )


def _optional_segment_float(segment: Any, name: str) -> float | None:
    value = getattr(segment, name, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _segment_time_range(segment: Any) -> tuple[float, float] | None:
    try:
        start = max(0.0, float(getattr(segment, "start")))
        end = float(getattr(segment, "end"))
    except (AttributeError, TypeError, ValueError):
        return None
    if end <= start:
        return None
    return start, end


def _segment_artifact(
    segment: Any,
    config: AppConfig,
) -> tuple[str, tuple[float, float], str] | None:
    text = str(getattr(segment, "text", "") or "").strip()
    if not text:
        return None
    reason = asr_artifact_reason(text, config)
    if reason is None:
        return None
    time_range = _segment_time_range(segment)
    if time_range is None:
        return None
    return reason, time_range, text


def _extract_words(segment: Any) -> list[tuple[float, float, str]]:
    words = getattr(segment, "words", None) or []
    extracted: list[tuple[float, float, str]] = []
    for word in words:
        text = (getattr(word, "word", "") or "").strip()
        start = getattr(word, "start", None)
        end = getattr(word, "end", None)
        if not text or start is None or end is None:
            continue
        start_float = float(start)
        end_float = float(end)
        if end_float <= start_float:
            continue
        extracted.append((start_float, end_float, text))
    return extracted


def _split_word_chunks(words: list[tuple[float, float, str]], config: AppConfig) -> list[tuple[float, float, str]]:
    chunks: list[tuple[float, float, str]] = []
    current: list[tuple[float, float, str]] = []

    for word in words:
        if not current:
            current.append(word)
            continue

        candidate = [*current, word]
        if _word_chunk_duration(candidate) > config.subtitle_max_duration_seconds or (
            _display_length(_join_word_text(candidate)) > config.subtitle_max_chars
        ):
            split_at = _find_preferred_split(current)
            if split_at is not None and split_at < len(current) - 1:
                chunks.append(_word_chunk_to_timed_text(current[: split_at + 1]))
                current = [*current[split_at + 1 :], word]
            else:
                chunks.append(_word_chunk_to_timed_text(current))
                current = [word]
        else:
            current.append(word)

    if current:
        chunks.append(_word_chunk_to_timed_text(current))

    return chunks


def _word_chunk_duration(words: list[tuple[float, float, str]]) -> float:
    return words[-1][1] - words[0][0]


def _word_chunk_to_timed_text(words: list[tuple[float, float, str]]) -> tuple[float, float, str]:
    return words[0][0], words[-1][1], _join_word_text(words)


def _join_word_text(words: list[tuple[float, float, str]]) -> str:
    return "".join(word[2] for word in words).strip()


def _display_length(text: str) -> int:
    return len("".join(text.split()))


def _find_preferred_split(words: list[tuple[float, float, str]]) -> int | None:
    punctuation = "。！？!?、，,；;：:"
    for index in range(len(words) - 1, -1, -1):
        if words[index][2].rstrip().endswith(tuple(punctuation)):
            return index
    return None


def _normalize_timing(
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
) -> list[tuple[float, float, str]]:
    clean_chunks: list[tuple[float, float, str]] = []
    for start, end, text in sorted(chunks, key=lambda item: item[0]):
        cleaned_text = _clean_transcribed_text(text, config)
        if not cleaned_text or _is_hallucination_text(cleaned_text, config):
            continue
        clean_chunks.append((max(0.0, start), max(start + 0.2, end), cleaned_text))
    clean_chunks = _merge_short_chunks(clean_chunks, config)

    normalized: list[tuple[float, float, str]] = []
    previous_end: float | None = None
    minimum_gap = max(0.0, float(config.subtitle_min_gap_seconds))
    fail_cps_limit = max(
        1.0,
        float(getattr(config, "subtitle_quality_fail_cps", 25.0) or 25.0),
    )
    for index, (raw_start, end, text) in enumerate(clean_chunks):
        start = raw_start
        if previous_end is not None:
            start = max(start, previous_end + minimum_gap)
        next_start = clean_chunks[index + 1][0] if index + 1 < len(clean_chunks) else None
        max_end = start + config.subtitle_max_duration_seconds
        target_end = min(end + config.subtitle_end_padding_seconds, max_end)

        if next_start is not None:
            target_end = min(target_end, next_start - minimum_gap)

        readable_duration = _display_length(text) / fail_cps_limit
        min_end = start + max(config.subtitle_min_duration_seconds, readable_duration)
        if target_end < min_end:
            # Word timestamps occasionally overlap or leave an impossibly short
            # cue. Preserve readability here and move the following cue forward
            # on its iteration instead of recreating an overlap.
            target_end = min(min_end, max_end)

        if target_end <= start:
            target_end = start + 0.2

        normalized.append((start, target_end, text))
        previous_end = target_end

    return normalized


def _merge_short_chunks(
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
) -> list[tuple[float, float, str]]:
    merged: list[tuple[float, float, str]] = []
    index = 0
    while index < len(chunks):
        start, end, text = chunks[index]
        duration = end - start
        if duration >= config.subtitle_min_duration_seconds:
            merged.append((start, end, text))
            index += 1
            continue

        if index + 1 < len(chunks):
            next_start, next_end, next_text = chunks[index + 1]
            next_candidate = (start, next_end, f"{text}{next_text}")
            if _can_merge_chunk(next_candidate, config):
                merged.append(next_candidate)
                index += 2
                continue

        if merged:
            prev_start, prev_end, prev_text = merged[-1]
            prev_candidate = (prev_start, end, f"{prev_text}{text}")
            if _can_merge_chunk(prev_candidate, config):
                merged[-1] = prev_candidate
                index += 1
                continue

        merged.append((start, end, text))
        index += 1

    return merged


def _can_merge_chunk(chunk: tuple[float, float, str], config: AppConfig) -> bool:
    start, end, text = chunk
    return (
        end > start
        and end - start <= config.subtitle_max_duration_seconds
        and _display_length(text) <= config.subtitle_max_chars
    )


def _rescue_gaps(
    model: Any,
    audio_path: str | Path,
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
    logger: logging.Logger,
    *,
    removed_artifact_ranges: list[tuple[float, float]] | None = None,
    rejected_quality_ranges: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float, str]]:
    gaps = _select_rescue_gaps(chunks, config)
    if config.gap_rescue_max_gaps:
        gaps = gaps[: config.gap_rescue_max_gaps]

    accepted: list[tuple[float, float, str]] = []
    existing_texts = [_normalize_text(text) for _, _, text in chunks]
    for gap_start, gap_end in gaps:
        padded_start = max(0.0, gap_start - config.gap_rescue_padding_seconds)
        padded_end = gap_end + config.gap_rescue_padding_seconds
        clips = _gap_rescue_clips(padded_start, padded_end, config)
        logger.info(
            "Running gap rescue: %.2fs -> %.2fs clips=%s",
            gap_start,
            gap_end,
            len(clips),
        )

        for clip_start, clip_end in clips:
            try:
                segments, _info = model.transcribe(
                    str(audio_path),
                    language=config.whisper_language,
                    task=config.whisper_task,
                    vad_filter=False,
                    condition_on_previous_text=False,
                    temperature=0,
                    beam_size=config.whisper_beam_size,
                    best_of=config.whisper_best_of,
                    patience=config.whisper_patience,
                    length_penalty=config.whisper_length_penalty,
                    repetition_penalty=config.whisper_repetition_penalty,
                    no_repeat_ngram_size=config.whisper_no_repeat_ngram_size,
                    word_timestamps=True,
                    no_speech_threshold=config.gap_rescue_no_speech_threshold,
                    log_prob_threshold=config.gap_rescue_log_prob_threshold,
                    compression_ratio_threshold=config.gap_rescue_compression_ratio_threshold,
                    hallucination_silence_threshold=config.whisper_hallucination_silence_threshold,
                    clip_timestamps=f"{clip_start:.3f},{clip_end:.3f}",
                )
            except Exception as exc:
                logger.warning(
                    "Gap rescue clip failed for %.2fs -> %.2fs inside gap %.2fs -> %.2fs: %s",
                    clip_start,
                    clip_end,
                    gap_start,
                    gap_end,
                    exc,
                )
                continue

            rescued: list[tuple[float, float, str]] = []
            for segment in segments:
                artifact = _segment_artifact(segment, config)
                if artifact is not None:
                    reason, artifact_range, text = artifact
                    if removed_artifact_ranges is not None:
                        removed_artifact_ranges.append(artifact_range)
                    logger.error(
                        "Removed ASR artifact segment during gap rescue "
                        "range=%.2f-%.2fs reason=%s text=%r",
                        artifact_range[0],
                        artifact_range[1],
                        reason,
                        text[:160],
                    )
                    continue
                rejection = _gap_rescue_segment_rejection_reason(segment, config)
                if rejection is not None:
                    rejected_range = _segment_time_range(segment)
                    if rejected_range is not None and rejected_quality_ranges is not None:
                        rejected_quality_ranges.append(rejected_range)
                    logger.warning(
                        "Rejected gap rescue ASR segment %.2f-%.2fs reason=%s text=%r",
                        float(getattr(segment, "start", 0.0) or 0.0),
                        float(getattr(segment, "end", 0.0) or 0.0),
                        rejection,
                        str(getattr(segment, "text", "") or "")[:120],
                    )
                    continue
                rescued.extend(_segment_to_chunks(segment, config))

            for start, end, text in rescued:
                center = (start + end) / 2
                if center < gap_start or center > gap_end:
                    continue
                if _display_length(text) < config.gap_rescue_min_chars:
                    continue
                if _is_duplicate_rescue(text, existing_texts):
                    continue
                accepted.append((start, end, text))
                existing_texts.append(_normalize_text(text))

    return accepted


def _gap_rescue_clips(
    start: float,
    end: float,
    config: AppConfig,
) -> list[tuple[float, float]]:
    """Split a long rescue range into overlapping clips.

    Whisper can skip speech near the beginning of a long ``clip_timestamps``
    request.  Small overlapping clips make the cold open and first spoken
    lines independent decoding boundaries while keeping internal short-gap
    rescue to one request.
    """

    if end <= start:
        return []
    clip_seconds = max(1.0, float(getattr(config, "gap_rescue_clip_seconds", 30.0)))
    overlap = max(0.0, float(getattr(config, "gap_rescue_clip_overlap_seconds", 2.0)))
    overlap = min(overlap, max(0.0, clip_seconds - 0.1))
    if end - start <= clip_seconds:
        return [(start, end)]

    clips: list[tuple[float, float]] = []
    cursor = start
    step = max(0.1, clip_seconds - overlap)
    while cursor < end:
        clip_end = min(end, cursor + clip_seconds)
        clips.append((cursor, clip_end))
        if clip_end >= end:
            break
        cursor = min(end, cursor + step)
    return clips


def _select_rescue_gaps(
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
) -> list[tuple[float, float]]:
    if not chunks:
        return []

    gaps: list[tuple[float, float]] = []
    first_start = chunks[0][0]
    leading_threshold = max(
        0.1,
        float(
            getattr(
                config,
                "gap_rescue_leading_threshold_seconds",
                config.gap_rescue_threshold_seconds,
            )
        ),
    )
    if (
        bool(getattr(config, "enable_leading_gap_rescue", True))
        and leading_threshold <= first_start <= config.gap_rescue_leading_max_seconds
    ):
        gaps.append((0.0, first_start))

    if not bool(getattr(config, "enable_gap_rescue", True)):
        return gaps

    previous_end = chunks[0][1]
    for start, end, _text in chunks[1:]:
        gap = start - previous_end
        if (
            gap >= config.gap_rescue_threshold_seconds
            and gap <= config.gap_rescue_max_gap_seconds
        ):
            gaps.append((previous_end, start))
        previous_end = max(previous_end, end)

    return gaps


def _is_duplicate_rescue(text: str, existing_texts: list[str]) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return True
    for existing in existing_texts:
        if normalized == existing:
            return True
        if len(normalized) >= 4 and SequenceMatcher(None, normalized, existing).ratio() >= 0.86:
            return True
    return False


def _rescue_op_ed_lyrics(
    model: Any,
    audio_path: str | Path,
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
    logger: logging.Logger,
    *,
    removed_artifact_ranges: list[tuple[float, float]] | None = None,
    rejected_quality_ranges: list[tuple[float, float]] | None = None,
    probe_evidence: list[dict[str, Any]] | None = None,
) -> list[tuple[float, float, str]]:
    """Fill likely opening/ending song gaps with a lyrics-tuned ASR pass.

    The normal full-episode pass remains authoritative.  This second pass only
    contributes text inside uncovered ranges near the beginning or end of a
    long-form episode, so it cannot duplicate normal dialogue that Whisper
    already transcribed.
    """

    duration = _wav_duration_seconds(audio_path)
    if duration is None:
        logger.warning("Skip OP/ED lyrics rescue because audio duration is unavailable: %s", audio_path)
        return []

    ranges = _select_op_ed_rescue_ranges(chunks, duration, config)
    if not ranges:
        return []

    logger.info(
        "OP/ED lyrics rescue scanning %s uncovered range(s): %s",
        len(ranges),
        ", ".join(f"{start:.1f}-{end:.1f}s" for start, end in ranges),
    )
    accepted: list[tuple[float, float, str]] = []
    existing_texts = [_normalize_text(text) for _, _, text in chunks]
    padding = max(0.0, float(getattr(config, "op_ed_padding_seconds", 1.0)))
    rejected_segments = 0
    initial_prompt = str(getattr(config, "op_ed_initial_prompt", "") or "").strip()

    for gap_start, gap_end in ranges:
        clip_start = max(0.0, gap_start - padding)
        clip_end = min(duration, gap_end + padding)
        clips = _gap_rescue_clips(clip_start, clip_end, config)
        logger.info(
            "OP/ED lyrics rescue range %.2fs -> %.2fs clips=%s",
            gap_start,
            gap_end,
            len(clips),
        )
        probe: dict[str, Any] = {
            "range": [round(gap_start, 3), round(gap_end, 3)],
            "clip_ranges": [],
            "completed_clip_ranges": [],
            "failed_clip_ranges": [],
            "prompt_free": not bool(initial_prompt),
            "observed_ranges": [],
            "known_artifact_ranges": [],
            "rejected_quality_ranges": [],
            "clean_ranges": [],
            "accepted_ranges": [],
            "unbounded_observation": False,
        }

        for subclip_start, subclip_end in clips:
            clip_range = [round(subclip_start, 3), round(subclip_end, 3)]
            probe["clip_ranges"].append(clip_range)
            try:
                transcribe_options = {
                    "language": config.whisper_language,
                    "task": config.whisper_task,
                    "vad_filter": False,
                    "condition_on_previous_text": False,
                    "temperature": 0,
                    "beam_size": config.whisper_beam_size,
                    "best_of": config.whisper_best_of,
                    "patience": config.whisper_patience,
                    "length_penalty": config.whisper_length_penalty,
                    "repetition_penalty": config.whisper_repetition_penalty,
                    "no_repeat_ngram_size": config.whisper_no_repeat_ngram_size,
                    "word_timestamps": True,
                    "no_speech_threshold": float(getattr(config, "op_ed_no_speech_threshold", 0.95)),
                    "log_prob_threshold": float(getattr(config, "op_ed_log_prob_threshold", -1.5)),
                    "compression_ratio_threshold": float(
                        getattr(config, "op_ed_compression_ratio_threshold", 3.0)
                    ),
                    "hallucination_silence_threshold": config.whisper_hallucination_silence_threshold,
                    "clip_timestamps": f"{subclip_start:.3f},{subclip_end:.3f}",
                }
                if initial_prompt:
                    transcribe_options["initial_prompt"] = initial_prompt
                segments, _info = model.transcribe(str(audio_path), **transcribe_options)
                materialized_segments = list(segments)
            except Exception as exc:
                probe["failed_clip_ranges"].append(clip_range)
                logger.warning(
                    "OP/ED lyrics rescue clip failed for %.2fs -> %.2fs inside range %.2fs -> %.2fs: %s",
                    subclip_start,
                    subclip_end,
                    gap_start,
                    gap_end,
                    exc,
                )
                continue
            probe["completed_clip_ranges"].append(clip_range)

            rescued: list[tuple[float, float, str]] = []
            for segment in materialized_segments:
                observed_range = _segment_time_range(segment)
                if observed_range is None:
                    probe["unbounded_observation"] = True
                else:
                    probe["observed_ranges"].append(
                        [round(observed_range[0], 3), round(observed_range[1], 3)]
                    )
                rejection = _op_ed_segment_rejection_reason(segment, config)
                if rejection is not None:
                    if rejection.startswith("asr_artifact:"):
                        artifact_range = observed_range
                        if artifact_range is not None and removed_artifact_ranges is not None:
                            removed_artifact_ranges.append(artifact_range)
                        if artifact_range is not None:
                            probe["known_artifact_ranges"].append(
                                [round(artifact_range[0], 3), round(artifact_range[1], 3)]
                            )
                    else:
                        rejected_range = observed_range
                        if rejected_range is not None and rejected_quality_ranges is not None:
                            rejected_quality_ranges.append(rejected_range)
                        if rejected_range is not None:
                            probe["rejected_quality_ranges"].append(
                                [round(rejected_range[0], 3), round(rejected_range[1], 3)]
                            )
                    rejected_segments += 1
                    logger.warning(
                        "Rejected OP/ED ASR segment %.2f-%.2fs reason=%s text=%r",
                        float(getattr(segment, "start", 0.0) or 0.0),
                        float(getattr(segment, "end", 0.0) or 0.0),
                        rejection,
                        str(getattr(segment, "text", "") or "")[:120],
                    )
                    continue
                if observed_range is not None:
                    probe["clean_ranges"].append(
                        [round(observed_range[0], 3), round(observed_range[1], 3)]
                    )
                rescued.extend(_segment_to_chunks(segment, config))

            for start, end, text in rescued:
                center = (start + end) / 2
                if center < gap_start or center > gap_end:
                    continue
                if _display_length(text) < 1 or _is_duplicate_rescue(text, existing_texts):
                    continue
                clamped_start = max(gap_start, start)
                clamped_end = min(gap_end, end)
                if clamped_end <= clamped_start:
                    continue
                accepted.append((clamped_start, clamped_end, text))
                probe["accepted_ranges"].append(
                    [round(clamped_start, 3), round(clamped_end, 3)]
                )
                existing_texts.append(_normalize_text(text))

        probe["completed"] = bool(clips) and not bool(probe["failed_clip_ranges"])
        if probe_evidence is not None:
            probe_evidence.append(probe)

    if rejected_segments:
        logger.warning("OP/ED lyrics rescue rejected %s low-quality segment(s).", rejected_segments)
    return accepted


def _select_op_ed_rescue_ranges(
    chunks: list[tuple[float, float, str]],
    duration: float,
    config: AppConfig,
) -> list[tuple[float, float]]:
    min_audio = max(0.0, float(getattr(config, "op_ed_min_audio_seconds", 600.0)))
    if duration < min_audio:
        return []

    opening_end = min(duration, float(getattr(config, "op_ed_opening_window_seconds", 360.0)))
    ending_start = max(0.0, duration - float(getattr(config, "op_ed_ending_window_seconds", 300.0)))
    windows = _merge_time_ranges([(0.0, opening_end), (ending_start, duration)])
    threshold = max(0.1, float(getattr(config, "op_ed_gap_threshold_seconds", 6.0)))
    max_gap = max(threshold, float(getattr(config, "op_ed_max_gap_seconds", 210.0)))

    candidates: list[tuple[float, float]] = []
    for window_start, window_end in windows:
        coverage = _merge_time_ranges(
            [
                (max(window_start, start), min(window_end, end))
                for start, end, _text in chunks
                if end > window_start and start < window_end
            ]
        )
        cursor = window_start
        for covered_start, covered_end in coverage:
            if covered_start - cursor >= threshold and covered_start - cursor <= max_gap:
                candidates.append((cursor, covered_start))
            cursor = max(cursor, covered_end)
        if window_end - cursor >= threshold and window_end - cursor <= max_gap:
            candidates.append((cursor, window_end))

    max_ranges = max(0, int(getattr(config, "op_ed_max_rescue_ranges", 6)))
    if max_ranges == 0:
        return []
    # Always preserve boundary gaps first.  Previously, six large internal
    # pauses could crowd out the opening and make the first spoken lines
    # permanently missing.
    boundary_candidates = [
        candidate
        for candidate in candidates
        if candidate[0] <= 0.001 or candidate[1] >= duration - 0.001
    ]
    selected: list[tuple[float, float]] = []
    for candidate in sorted(boundary_candidates, key=lambda item: item[0]):
        if candidate not in selected:
            selected.append(candidate)
        if len(selected) >= max_ranges:
            break
    for candidate in sorted(candidates, key=lambda item: (item[1] - item[0]), reverse=True):
        if candidate not in selected:
            selected.append(candidate)
        if len(selected) >= max_ranges:
            break
    return sorted(selected, key=lambda item: item[0])


def _merge_time_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges, key=lambda item: item[0]):
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _normalize_text(text: str) -> str:
    return "".join(text.split()).strip()


def _build_vad_parameters(config: AppConfig) -> dict[str, float | int]:
    return {
        "threshold": config.whisper_vad_threshold,
        "min_silence_duration_ms": config.whisper_vad_min_silence_duration_ms,
        "speech_pad_ms": config.whisper_vad_speech_pad_ms,
    }


def _is_hallucination_text(text: str, config: AppConfig) -> bool:
    normalized = _compact_hallucination_text(text)
    if not normalized:
        return True
    if asr_artifact_reason(text, config) is not None:
        return True

    phrases = (
        *DEFAULT_WHISPER_HALLUCINATION_PHRASES,
        *getattr(config, "whisper_hallucination_phrases", []),
    )
    for phrase in phrases:
        normalized_phrase = _compact_hallucination_text(str(phrase or ""))
        if normalized_phrase and normalized_phrase in normalized:
            return True

    has_credit_name = any(
        _compact_hallucination_text(name) in normalized
        for name in DEFAULT_WHISPER_HALLUCINATION_CREDIT_NAMES
    )
    if not has_credit_name:
        return False

    return any(
        _compact_hallucination_text(word) in normalized
        for word in DEFAULT_WHISPER_HALLUCINATION_CREDIT_WORDS
    )


def _chunk_index_ranges(
    chunks: list[tuple[float, float, str]],
    indexes: set[int],
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    active_start: float | None = None
    active_end: float | None = None
    previous_index: int | None = None
    for index in sorted(indexes):
        if index < 0 or index >= len(chunks):
            continue
        start, end, _text = chunks[index]
        if previous_index is None or index != previous_index + 1:
            if active_start is not None and active_end is not None:
                ranges.append((active_start, active_end))
            active_start = max(0.0, start)
            active_end = end
        else:
            active_end = max(active_end or end, end)
        previous_index = index
    if active_start is not None and active_end is not None:
        ranges.append((active_start, active_end))
    return ranges


def _artifact_review_ranges(
    ranges: list[tuple[float, float]],
    retained_chunks: list[tuple[float, float, str]],
    config: AppConfig,
) -> list[tuple[float, float]]:
    """Expand removed-artifact timestamps to the surrounding clean coverage.

    An echoed prompt often occupies only part of the real opening dialogue.
    Replacing only those exact timestamps can still lose the words immediately
    before or after the hallucination, so use the nearest clean subtitle
    boundaries when they are reasonably close.
    """

    if not ranges:
        return []
    padding = max(
        0.0,
        float(getattr(config, "asr_selective_retry_padding_seconds", 1.5) or 0.0),
    )
    neighbor_limit = max(
        12.0,
        float(getattr(config, "asr_selective_retry_merge_gap_seconds", 3.0) or 0.0)
        * 10.0,
    )
    clean = sorted(retained_chunks, key=lambda item: item[0])
    expanded: list[tuple[float, float]] = []
    for start, end in _normalize_review_ranges(ranges):
        previous_end = max(
            (chunk_end for _chunk_start, chunk_end, _text in clean if chunk_end <= start),
            default=None,
        )
        next_start = min(
            (chunk_start for chunk_start, _chunk_end, _text in clean if chunk_start >= end),
            default=None,
        )
        repair_start = max(0.0, start - padding)
        repair_end = end + padding
        if previous_end is not None and start - previous_end <= neighbor_limit:
            repair_start = previous_end
        elif previous_end is None and start <= neighbor_limit:
            repair_start = 0.0
        if next_start is not None and next_start - end <= neighbor_limit:
            repair_end = next_start
        expanded.append((repair_start, repair_end))
    return _normalize_review_ranges(expanded)


def _partition_artifact_ranges_by_rescue_evidence(
    ranges: list[tuple[float, float]],
    accepted_rescue_chunks: list[tuple[float, float, str]],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Separate primary artifacts that a clean prompt-free rescue replaced.

    Merely deleting a primary artifact is never enough evidence to publish.
    Recovery is accepted only when a separately decoded, already-filtered
    rescue chunk lands inside the removed interval (or contains its midpoint).
    """

    recovered: list[tuple[float, float]] = []
    unresolved: list[tuple[float, float]] = []
    clean_rescue = [
        (max(0.0, float(start)), float(end))
        for start, end, text in accepted_rescue_chunks
        if float(end) > float(start) and str(text or "").strip()
    ]
    for start, end in _normalize_review_ranges(ranges):
        artifact_midpoint = (start + end) / 2.0
        has_evidence = any(
            start <= (rescue_start + rescue_end) / 2.0 <= end
            or rescue_start <= artifact_midpoint <= rescue_end
            for rescue_start, rescue_end in clean_rescue
        )
        (recovered if has_evidence else unresolved).append((start, end))
    return recovered, unresolved


def _terminal_artifact_ranges(
    ranges: list[tuple[float, float]],
    duration: float | None,
) -> list[tuple[float, float]]:
    """Return exact artifact segments that terminate at the audio boundary."""

    if duration is None or not math.isfinite(duration) or duration <= 0:
        return []
    terminal: list[tuple[float, float]] = []
    for start, end in _normalize_review_ranges(ranges):
        if (
            start < duration
            and end <= duration + 0.25
            and duration - end <= TAIL_ARTIFACT_MAX_END_GAP_SECONDS
        ):
            terminal.append((start, end))
    return terminal


def _prompt_free_tail_artifact_consensus(
    audio_path: str | Path,
    primary_artifact_ranges: list[tuple[float, float]],
    retained_chunks: list[tuple[float, float, str]],
    accepted_recovery_chunks: list[tuple[float, float, str]],
    op_ed_probe_evidence: list[dict[str, Any]],
    config: AppConfig,
) -> tuple[list[tuple[float, float]], list[dict[str, Any]]]:
    """Confirm an EOF artifact with a second prompt-free pass and audio evidence.

    This is intentionally narrower than general artifact recovery.  The first
    prompt-free full pass must identify a known artifact ending at the audio
    boundary.  A completed prompt-free OP/ED pass must independently observe
    only the same known artifact, or no segment at all, over that exact window.
    VAD plus conservative PCM energy must find no speech in the artifact, and
    all adjacent VAD speech must already be covered by retained subtitle chunks.
    Any missing evidence, clean tail text, or low-quality speech fails closed.
    """

    if not bool(
        getattr(config, "asr_prompt_free_allow_recovered_primary_artifacts", False)
    ):
        return [], []
    if str(getattr(config, "whisper_initial_prompt", "") or "").strip():
        return [], []
    if str(getattr(config, "op_ed_initial_prompt", "") or "").strip():
        return [], []
    if bool(getattr(config, "whisper_condition_on_previous_text", True)):
        return [], []

    duration = _wav_duration_seconds(audio_path)
    terminal_ranges = _terminal_artifact_ranges(primary_artifact_ranges, duration)
    if duration is None or not terminal_ranges:
        return [], []

    confirmed: list[tuple[float, float]] = []
    evidence: list[dict[str, Any]] = []
    for artifact_start, artifact_end in terminal_ranges:
        item: dict[str, Any] = {
            "artifact_range": [round(artifact_start, 3), round(artifact_end, 3)],
            "audio_duration": round(duration, 3),
            "primary_pass": "known_artifact",
            "second_pass": None,
            "confirmed": False,
        }
        if any(
            _ranges_overlap(artifact_start, artifact_end, start, end)
            for start, end, text in accepted_recovery_chunks
            if str(text or "").strip()
        ):
            item["failure"] = "clean recovery text overlaps terminal artifact"
            evidence.append(item)
            continue

        probe = _matching_prompt_free_tail_probe(
            artifact_start,
            artifact_end,
            op_ed_probe_evidence,
        )
        if probe is None:
            item["failure"] = "completed prompt-free OP/ED evidence missing"
            evidence.append(item)
            continue
        item["probe_range"] = list(probe.get("range") or [])

        observed = _probe_ranges(probe.get("observed_ranges"))
        known_artifacts = _probe_ranges(probe.get("known_artifact_ranges"))
        rejected_quality = _probe_ranges(probe.get("rejected_quality_ranges"))
        clean = _probe_ranges(probe.get("clean_ranges"))
        if bool(probe.get("unbounded_observation")):
            item["failure"] = "second pass contains an observation without timing"
            evidence.append(item)
            continue
        if any(
            _ranges_overlap(artifact_start, artifact_end, start, end)
            for start, end in [*clean, *rejected_quality]
        ):
            item["failure"] = "second pass observed clean or ambiguous tail speech"
            evidence.append(item)
            continue
        overlapping_observed = [
            (start, end)
            for start, end in observed
            if _ranges_overlap(artifact_start, artifact_end, start, end)
        ]
        overlapping_known = [
            (start, end)
            for start, end in known_artifacts
            if _ranges_overlap(artifact_start, artifact_end, start, end)
        ]
        if overlapping_observed and len(overlapping_observed) != len(overlapping_known):
            item["failure"] = "second pass observations are not all known artifacts"
            evidence.append(item)
            continue
        item["second_pass"] = (
            "known_artifact" if overlapping_known else "no_dialogue_segment"
        )

        silence = _selective_window_silence_evidence(
            audio_path,
            artifact_start,
            min(duration, artifact_end),
            config,
        )
        item["artifact_silence_evidence"] = silence
        if not bool(silence.get("confirmed")):
            item["failure"] = "exact terminal artifact is not independently silent"
            evidence.append(item)
            continue

        adjacent = _tail_adjacent_speech_coverage_evidence(
            audio_path,
            artifact_start,
            artifact_end,
            duration,
            retained_chunks,
            config,
        )
        item["adjacent_speech_coverage"] = adjacent
        if not bool(adjacent.get("complete")):
            item["failure"] = "adjacent speech coverage is incomplete"
            evidence.append(item)
            continue

        item["confirmed"] = True
        evidence.append(item)
        confirmed.append((artifact_start, artifact_end))
    return _normalize_review_ranges(confirmed), evidence


def _matching_prompt_free_tail_probe(
    artifact_start: float,
    artifact_end: float,
    probes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for probe in probes:
        if not isinstance(probe, dict):
            continue
        if not bool(probe.get("prompt_free")) or not bool(probe.get("completed")):
            continue
        completed_clips = _probe_ranges(probe.get("completed_clip_ranges"))
        if any(
            clip_start <= artifact_start + 0.05
            and clip_end >= artifact_end - 0.05
            for clip_start, clip_end in completed_clips
        ):
            return probe
    return None


def _probe_ranges(value: object) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float]] = []
    if not isinstance(value, list):
        return parsed
    for raw in value:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            continue
        try:
            start = float(raw[0])
            end = float(raw[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            parsed.append((start, end))
    return parsed


def _ranges_overlap(
    first_start: float,
    first_end: float,
    second_start: float,
    second_end: float,
) -> bool:
    return first_end > second_start and first_start < second_end


def _tail_adjacent_speech_coverage_evidence(
    audio_path: str | Path,
    artifact_start: float,
    artifact_end: float,
    duration: float,
    retained_chunks: list[tuple[float, float, str]],
    config: AppConfig,
) -> dict[str, Any]:
    context_start = max(0.0, artifact_start - TAIL_ARTIFACT_CONTEXT_SECONDS)
    context_end = duration
    result: dict[str, Any] = {
        "complete": False,
        "context_range": [round(context_start, 3), round(context_end, 3)],
        "coverage_tolerance_seconds": TAIL_SPEECH_COVERAGE_TOLERANCE_SECONDS,
        "vad_speech_ranges": [],
        "covered_ranges": [],
        "uncovered_speech_ranges": [],
    }
    if context_end <= context_start:
        result["error"] = "empty adjacent coverage window"
        return result
    try:
        import numpy as np

        with wave.open(str(audio_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            total_frames = wav_file.getnframes()
            if channels != 1 or sample_width != 2 or sample_rate != 16000:
                raise ValueError(
                    "tail consensus requires mono 16 kHz signed 16-bit PCM"
                )
            first_frame = min(
                total_frames,
                max(0, int(math.floor(context_start * sample_rate))),
            )
            last_frame = min(
                total_frames,
                max(first_frame, int(math.ceil(context_end * sample_rate))),
            )
            wav_file.setpos(first_frame)
            raw = wav_file.readframes(last_frame - first_frame)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0:
            raise ValueError("adjacent coverage window contains no PCM frames")
        no_pad_config = _config_with_overrides(
            config,
            whisper_vad_speech_pad_ms=0,
        )
        timestamps = _silero_speech_timestamps(samples, no_pad_config)
        speech_ranges = [
            (
                context_start + max(0, int(item.get("start", 0))) / sample_rate,
                context_start + max(0, int(item.get("end", 0))) / sample_rate,
            )
            for item in timestamps
            if isinstance(item, dict)
            and int(item.get("end", 0)) > int(item.get("start", 0))
        ]
        tolerance = TAIL_SPEECH_COVERAGE_TOLERANCE_SECONDS
        coverage = _merge_time_ranges(
            [
                (max(context_start, start - tolerance), min(context_end, end + tolerance))
                for start, end, text in retained_chunks
                if str(text or "").strip()
                and end > context_start
                and start < context_end
            ]
        )
        uncovered = [
            (start, end)
            for start, end in speech_ranges
            if not any(
                start >= covered_start and end <= covered_end
                for covered_start, covered_end in coverage
            )
        ]
        result["vad_speech_ranges"] = [
            [round(start, 3), round(end, 3)] for start, end in speech_ranges
        ]
        result["covered_ranges"] = [
            [round(start, 3), round(end, 3)] for start, end in coverage
        ]
        result["uncovered_speech_ranges"] = [
            [round(start, 3), round(end, 3)] for start, end in uncovered
        ]
        result["complete"] = not uncovered
    except Exception as coverage_error:  # noqa: BLE001 - evidence must fail closed.
        result["error"] = str(coverage_error)[:240]
    return result


def _filter_asr_prompt_echo_chunks(
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
    logger: logging.Logger | None = None,
    *,
    stage: str = "ASR",
    removed_ranges: list[tuple[float, float]] | None = None,
) -> list[tuple[float, float, str]]:
    indexes = asr_artifact_line_indexes((text for _start, _end, text in chunks), config)
    if not indexes:
        return chunks
    if removed_ranges is not None:
        removed_ranges.extend(_chunk_index_ranges(chunks, indexes))
    if logger is not None:
        preview = " | ".join(chunks[index][2] for index in sorted(indexes)[:8])
        logger.error(
            "Removed %s ASR artifact chunk(s) during %s: %s",
            len(indexes),
            stage,
            preview[:240],
        )
    return [chunk for index, chunk in enumerate(chunks) if index not in indexes]


def _op_ed_segment_rejection_reason(segment: Any, config: AppConfig) -> str | None:
    text = str(getattr(segment, "text", "") or "").strip()
    artifact_reason = asr_artifact_reason(text, config)
    if artifact_reason is not None:
        return f"asr_artifact:{artifact_reason}"

    avg_logprob = _optional_segment_float(segment, "avg_logprob")
    minimum_logprob = float(getattr(config, "op_ed_accept_min_avg_logprob", -1.15))
    if avg_logprob is not None and avg_logprob < minimum_logprob:
        return f"avg_logprob={avg_logprob:.3f}<{minimum_logprob:.3f}"

    compression_ratio = _optional_segment_float(segment, "compression_ratio")
    maximum_compression = float(getattr(config, "op_ed_accept_max_compression_ratio", 2.4))
    if compression_ratio is not None and compression_ratio > maximum_compression:
        return f"compression_ratio={compression_ratio:.3f}>{maximum_compression:.3f}"

    no_speech_prob = _optional_segment_float(segment, "no_speech_prob")
    maximum_no_speech = float(getattr(config, "op_ed_accept_max_no_speech_prob", 0.90))
    if (
        no_speech_prob is not None
        and no_speech_prob > maximum_no_speech
        and (avg_logprob is None or avg_logprob < -0.60)
    ):
        return f"no_speech_prob={no_speech_prob:.3f}>{maximum_no_speech:.3f}"
    return None


def _gap_rescue_segment_rejection_reason(segment: Any, config: AppConfig) -> str | None:
    avg_logprob = _optional_segment_float(segment, "avg_logprob")
    minimum_logprob = float(getattr(config, "gap_rescue_accept_min_avg_logprob", -1.15))
    if avg_logprob is not None and avg_logprob < minimum_logprob:
        return f"avg_logprob={avg_logprob:.3f}<{minimum_logprob:.3f}"

    compression_ratio = _optional_segment_float(segment, "compression_ratio")
    maximum_compression = float(
        getattr(config, "gap_rescue_accept_max_compression_ratio", 2.4)
    )
    if compression_ratio is not None and compression_ratio > maximum_compression:
        return f"compression_ratio={compression_ratio:.3f}>{maximum_compression:.3f}"

    no_speech_prob = _optional_segment_float(segment, "no_speech_prob")
    maximum_no_speech = float(
        getattr(config, "gap_rescue_accept_max_no_speech_prob", 0.90)
    )
    if (
        no_speech_prob is not None
        and no_speech_prob > maximum_no_speech
        and (avg_logprob is None or avg_logprob < -0.60)
    ):
        return f"no_speech_prob={no_speech_prob:.3f}>{maximum_no_speech:.3f}"
    return None


def _compact_hallucination_text(text: str) -> str:
    return "".join(str(text or "").split()).casefold()


def _clean_transcribed_text(text: str, config: AppConfig) -> str:
    stripped = text.strip()
    if _is_hallucination_text(stripped, config):
        return ""

    if not config.filter_repeated_vocalizations:
        return stripped

    compact = "".join(stripped.split())
    if _is_repeated_vocalization_text(compact, config):
        return ""

    prefix_length = _vocalization_prefix_length(compact)
    if prefix_length >= config.repeated_vocalization_min_chars and prefix_length < len(compact):
        trimmed = compact[prefix_length:].lstrip("!?！？…。、，,・")
        if MEANINGFUL_TEXT_RE.search(trimmed) and not _is_hallucination_text(trimmed, config):
            return trimmed

    return stripped


def _is_repeated_vocalization_text(text: str, config: AppConfig) -> bool:
    compact = "".join(text.split())
    if len(compact) < config.repeated_vocalization_min_chars:
        return False

    core = compact.strip("!?！？…。、，,・ーっッ")
    if not core:
        return True

    return all(char in VOCALIZATION_CHARS for char in core)


def _vocalization_prefix_length(text: str) -> int:
    count = 0
    for char in text:
        if char not in VOCALIZATION_CHARS:
            break
        count += 1
    return count


def _write_gap_report(
    srt_path: Path,
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
) -> None:
    report_path = srt_path.with_name(f"{srt_path.stem}.gaps.txt")
    lines = ["# Subtitle gap report", f"threshold_seconds={config.gap_report_threshold_seconds}", ""]
    previous_end: float | None = None
    if chunks and chunks[0][0] >= config.gap_report_threshold_seconds:
        lines.append(
            f"{_format_timestamp(0.0)} --> {_format_timestamp(chunks[0][0])} "
            f"gap={chunks[0][0]:.2f}s before={chunks[0][2]} leading=true"
        )
    for start, end, text in chunks:
        if previous_end is not None and start - previous_end >= config.gap_report_threshold_seconds:
            lines.append(
                f"{_format_timestamp(previous_end)} --> {_format_timestamp(start)} "
                f"gap={start - previous_end:.2f}s before={text}"
            )
        previous_end = end

    if len(lines) == 3:
        lines.append("No large gaps detected.")

    report_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8-sig", newline="\n")


def _validate_transcription_quality(
    audio_path: str | Path,
    chunks: list[tuple[float, float, str]],
    config: AppConfig,
    logger: logging.Logger,
    *,
    segment_confidences: list[SegmentConfidence] | None = None,
) -> None:
    if not config.transcription_quality_check_enabled:
        return

    problems: list[str] = []
    low_confidence_review_ranges: list[tuple[float, float]] = []
    selective_review_ranges: list[tuple[float, float]] = []
    selective_problem_count = 0
    observed_confidences = [
        item
        for item in (segment_confidences or [])
        if item.avg_logprob is not None
    ]
    minimum_confidence_segments = max(
        1,
        int(getattr(config, "transcription_quality_min_confidence_segments", 8) or 8),
    )
    if len(observed_confidences) >= minimum_confidence_segments:
        min_avg_logprob = float(getattr(config, "transcription_quality_min_avg_logprob", -1.0))
        low_confidence = [
            item
            for item in observed_confidences
            if item.avg_logprob is not None and item.avg_logprob < min_avg_logprob
        ]
        low_confidence_percent = len(low_confidence) / len(observed_confidences) * 100.0
        max_low_confidence_percent = float(
            getattr(config, "transcription_quality_max_low_confidence_percent", 25.0)
        )
        logger.info(
            "ASR confidence summary: segments=%s low_confidence=%s percent=%.2f%% min_avg_logprob=%.2f audio=%s",
            len(observed_confidences),
            len(low_confidence),
            low_confidence_percent,
            min_avg_logprob,
            audio_path,
        )
        if low_confidence_percent > max_low_confidence_percent:
            samples = ",".join(
                f"{item.start:.1f}s:{item.avg_logprob:.2f}"
                for item in low_confidence[:5]
                if item.avg_logprob is not None
            )
            problems.append(
                "low_confidence_segments "
                f"{low_confidence_percent:.2f}% > {max_low_confidence_percent:.2f}%"
                + (f" samples={samples}" if samples else "")
            )
            low_confidence_review_ranges = _low_confidence_review_ranges(low_confidence, config)
            selective_review_ranges.extend(low_confidence_review_ranges)
            selective_problem_count += 1

    if chunks and bool(getattr(config, "enable_leading_gap_rescue", True)):
        first_start = max(0.0, min(start for start, _end, _text in chunks))
        max_leading_gap = max(
            0.1,
            float(getattr(config, "transcription_quality_max_leading_gap_seconds", 30.0)),
        )
        if first_start > max_leading_gap:
            problems.append(
                f"leading_gap {first_start:.2f}s > {max_leading_gap:.2f}s"
            )
            review_end = min(
                first_start,
                float(getattr(config, "gap_rescue_leading_max_seconds", 120.0)),
            )
            selective_review_ranges.append((0.0, review_end))
            selective_problem_count += 1

    duration = _wav_duration_seconds(audio_path)
    if duration is None:
        logger.warning("Skip ASR quality check because audio duration is unavailable: %s", audio_path)
    elif duration >= config.transcription_quality_min_audio_seconds:
        covered = sum(max(0.0, end - start) for start, end, _text in chunks)
        coverage_percent = covered / duration * 100 if duration else 0.0
        blocks_per_minute = len(chunks) / (duration / 60.0) if duration else 0.0
        logger.info(
            "ASR quality summary: chunks=%s coverage=%.2f%% blocks_per_minute=%.2f duration=%.1fs audio=%s",
            len(chunks),
            coverage_percent,
            blocks_per_minute,
            duration,
            audio_path,
        )
        if coverage_percent < config.transcription_quality_min_coverage_percent:
            problems.append(
                "coverage "
                f"{coverage_percent:.2f}% < {config.transcription_quality_min_coverage_percent:.2f}%"
            )
        if blocks_per_minute < config.transcription_quality_min_blocks_per_minute:
            problems.append(
                "blocks_per_minute "
                f"{blocks_per_minute:.2f} < {config.transcription_quality_min_blocks_per_minute:.2f}"
            )

    if problems:
        duration_text = f"{duration:.1f}s" if duration is not None else "unknown"
        message = (
            "ASR quality check failed for "
            f"{audio_path}: chunks={len(chunks)} duration={duration_text} "
            + "; ".join(problems)
        )
        if selective_review_ranges and len(problems) == selective_problem_count:
            normalized_ranges = _normalize_review_ranges(selective_review_ranges)
            reason_code = "leading_gap" if not low_confidence_review_ranges else "low_confidence"
            raise LowConfidenceTranscriptionError(
                message,
                normalized_ranges,
                reason_code=reason_code,
            )
        raise TranscriptionError(message)


def validate_transcription_srt_quality(
    audio_path: str | Path,
    srt_path: str | Path,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    """Apply the shared structural ASR gate to a backend's final SRT.

    Backends that expose decoder confidence metadata keep their native gate;
    this final pass deliberately validates only the evidence common to every
    backend: usable timings, leading coverage, total coverage, and subtitle
    density.
    """

    if not config.transcription_quality_check_enabled:
        return

    output = Path(srt_path)
    try:
        blocks = read_srt(output)
    except Exception as exc:
        raise TranscriptionError(
            f"ASR final quality gate could not read SRT {output}: {exc}"
        ) from exc
    if not blocks:
        raise TranscriptionError(f"ASR final quality gate found an empty SRT: {output}")

    chunks: list[tuple[float, float, str]] = []
    for block in blocks:
        text = " ".join(line.strip() for line in block.text if line.strip())
        if not text:
            raise TranscriptionError(
                "ASR final quality gate found an empty subtitle block "
                f"index={block.index} SRT={output}"
            )
        start, end = _srt_timing_seconds(block.timing)
        if end <= start:
            raise TranscriptionError(
                "ASR final quality gate found a non-positive subtitle timing "
                f"index={block.index} timing={block.timing!r} SRT={output}"
            )
        chunks.append((start, end, text))

    _validate_transcription_quality(audio_path, chunks, config, logger)


def repair_low_confidence_ranges(
    audio_path: str | Path,
    srt_path: str | Path,
    review_ranges: list[tuple[float, float]],
    config: AppConfig,
    logger: logging.Logger,
) -> SelectiveRepairResult:
    source = Path(audio_path)
    output = Path(srt_path)
    # A selective retry is recovery from evidence that the previous ASR pass
    # was unsafe. Reusing either the dialogue prompt or the OP/ED prompt can
    # deterministically reproduce the same prompt echo/hallucination in every
    # rejected window, even when a caller forgets to clear it. Enforce the
    # prompt-free contract at the narrowest boundary that actually invokes
    # Whisper for those ranges.
    repair_config = _prompt_free_selective_config(config)
    primary_blocks = read_srt(output)
    if not primary_blocks:
        raise TranscriptionError("Selective ASR repair requires a primary SRT")
    requested_ranges = _normalize_review_ranges(review_ranges)
    if not requested_ranges:
        raise TranscriptionError("Selective ASR repair received no valid time ranges")
    ranges = _expand_review_ranges_to_primary_blocks(primary_blocks, requested_ranges)
    if ranges != requested_ranges:
        logger.info(
            "Selective ASR repair expanded review ranges to preserve primary blocks: "
            "requested=%s expanded=%s",
            requested_ranges,
            ranges,
        )

    repaired_blocks: list[SrtBlock] = []
    repaired_confidences: list[SegmentConfidence] = []
    replaced_ranges: list[tuple[float, float]] = []
    unresolved_ranges: list[tuple[float, float]] = []
    unresolved_details: list[str] = []
    confirmed_silent_ranges: list[tuple[float, float]] = []
    silence_evidence: list[dict[str, Any]] = []
    from audio import _resolve_ffmpeg

    with tempfile.TemporaryDirectory(prefix="anime-subtitle-asr-repair-") as temp_dir:
        root = Path(temp_dir)
        clip_config = _config_with_overrides(
            repair_config,
            asr_diagnostics_enabled=True,
            asr_diagnostics_path=str(root / "diagnostics"),
        )
        for index, (start, end) in enumerate(ranges, start=1):
            range_blocks: list[SrtBlock] = []
            range_confidences: list[SegmentConfidence] = []
            range_failures: list[str] = []
            range_silence_evidence: list[dict[str, Any]] = []
            clips = _gap_rescue_clips(start, end, repair_config)
            logger.info(
                "Selective ASR repair range %.2fs -> %.2fs clips=%s",
                start,
                end,
                len(clips),
            )
            for clip_index, (clip_start, clip_end) in enumerate(clips, start=1):
                sample = root / f"range-{index}-clip-{clip_index}.wav"
                sample_srt = root / f"range-{index}-clip-{clip_index}.srt"
                duration = max(0.5, clip_end - clip_start)
                command = [
                    _resolve_ffmpeg(),
                    "-y",
                    "-ss",
                    f"{clip_start:.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    str(source),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-f",
                    "wav",
                    str(sample),
                ]
                try:
                    result = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                    )
                except OSError as extraction_error:
                    range_failures.append(
                        "Selective ASR audio extraction could not start "
                        f"range={clip_start:.1f}-{clip_end:.1f}: {extraction_error}"
                    )
                    logger.warning("%s", range_failures[-1])
                    continue
                if result.returncode != 0 or not sample.exists():
                    detail = (result.stderr or result.stdout or "ffmpeg produced no repair sample").strip()
                    range_failures.append(
                        "Selective ASR audio extraction failed "
                        f"range={clip_start:.1f}-{clip_end:.1f}: {detail}"
                    )
                    logger.warning("%s", range_failures[-1])
                    continue
                try:
                    transcribe_to_srt(sample, sample_srt, clip_config, logger)
                except TranscriptionError as exc:
                    if _is_empty_selective_asr_result(exc):
                        logger.info(
                            "Selective ASR repair clip contains no detected speech: %.2fs -> %.2fs",
                            clip_start,
                            clip_end,
                        )
                        continue
                    artifact_evidence = _prompt_free_artifact_silence_evidence(
                        sample,
                        exc,
                        clip_start,
                        clip_end,
                        repair_config,
                    )
                    if artifact_evidence and all(
                        bool(item.get("confirmed")) for item in artifact_evidence
                    ):
                        range_silence_evidence.extend(artifact_evidence)
                        logger.warning(
                            "Selective ASR omitted a prompt-free artifact only after "
                            "same-window VAD and energy both found no speech: "
                            "clip=%.2f-%.2fs windows=%s",
                            clip_start,
                            clip_end,
                            len(artifact_evidence),
                        )
                    else:
                        detail = (
                            "Selective ASR clip remained unresolved "
                            f"range={clip_start:.1f}-{clip_end:.1f}: {exc}"
                        )
                        range_failures.append(detail)
                        logger.warning("%s", detail)
                        continue
                range_confidences.extend(
                    _confidence_segments_from_diagnostics(
                        sample_srt,
                        clip_config,
                        offset_seconds=clip_start,
                        accepted_range=(start, end),
                    )
                )
                if not sample_srt.exists():
                    continue
                for block in read_srt(sample_srt):
                    block_start, block_end = _srt_timing_seconds(block.timing)
                    absolute_start = block_start + clip_start
                    absolute_end = block_end + clip_start
                    center = (absolute_start + absolute_end) / 2
                    if center < start or center > end:
                        continue
                    range_blocks.append(
                        SrtBlock(
                            index=0,
                            timing=(
                                f"{_format_timestamp(max(start, absolute_start))} --> "
                                f"{_format_timestamp(min(end, absolute_end))}"
                            ),
                            text=list(block.text),
                        )
                    )

            range_blocks = _deduplicate_repaired_blocks(range_blocks)
            if range_failures:
                unresolved_ranges.append((start, end))
                unresolved_details.extend(range_failures)
                continue
            if range_blocks:
                repaired_blocks.extend(range_blocks)
                repaired_confidences.extend(range_confidences)
                replaced_ranges.append((start, end))
                if range_silence_evidence:
                    silence_evidence.extend(range_silence_evidence)
                    confirmed_silent_ranges.extend(
                        _evidence_absolute_ranges(range_silence_evidence)
                    )
                continue
            evidence_ranges = _evidence_absolute_ranges(range_silence_evidence)
            overlapping_primary = [
                block
                for block in primary_blocks
                if _timing_overlaps_range(block.timing, start, end)
            ]
            unsafe_primary = [
                block
                for block in overlapping_primary
                if not _timing_contained_by_ranges(block.timing, evidence_ranges)
            ]
            if range_silence_evidence and not unsafe_primary:
                replaced_ranges.extend(evidence_ranges)
                confirmed_silent_ranges.extend(evidence_ranges)
                silence_evidence.extend(range_silence_evidence)
                continue
            if overlapping_primary:
                unresolved_ranges.append((start, end))
                unresolved_details.append(
                    "Selective ASR returned no trustworthy replacement for existing "
                    f"blocks range={start:.1f}-{end:.1f}"
                )

    if unresolved_ranges:
        formatted = ",".join(f"{start:.1f}-{end:.1f}s" for start, end in unresolved_ranges[:8])
        detail_text = " | ".join(unresolved_details[:4])
        raise TranscriptionError(
            "Selective ASR fallback left one or more ranges unresolved after checking "
            f"all requested ranges: ranges={formatted}"
            + (f" details={detail_text}" if detail_text else "")
        )

    confirmed_silent_ranges = _normalize_review_ranges(confirmed_silent_ranges)
    replaced_ranges = _normalize_review_ranges(replaced_ranges)
    if not repaired_blocks and not confirmed_silent_ranges:
        raise TranscriptionError("Selective ASR fallback returned no subtitle blocks")

    retained = [
        block
        for block in primary_blocks
        if not any(_timing_overlaps_range(block.timing, start, end) for start, end in replaced_ranges)
    ]
    merged = sorted([*retained, *repaired_blocks], key=lambda block: _srt_timing_seconds(block.timing)[0])
    normalized = [
        SrtBlock(index=index, timing=block.timing, text=list(block.text))
        for index, block in enumerate(merged, 1)
    ]
    artifact_indexes = asr_artifact_line_indexes(
        (" ".join(block.text) for block in normalized),
        config,
    )
    if artifact_indexes:
        sample_indexes = ",".join(str(index + 1) for index in sorted(artifact_indexes)[:12])
        raise TranscriptionError(
            "Selective ASR repair still contains prompt or hallucination artifacts "
            f"at subtitle indexes={sample_indexes}"
        )
    write_srt(output, normalized)
    if silence_evidence:
        diagnostic_config = _config_with_overrides(
            config,
            asr_diagnostics_enabled=True,
        )
        diagnostic = _write_asr_diagnostics(
            output,
            source,
            [
                (
                    _srt_timing_seconds(block.timing)[0],
                    _srt_timing_seconds(block.timing)[1],
                    " ".join(line.strip() for line in block.text if line.strip()),
                )
                for block in normalized
            ],
            repaired_confidences,
            diagnostic_config,
            status="selective_repair_completed",
            repaired_ranges=ranges,
            reason_code="prompt_free_artifact_confirmed_silent",
            confirmed_silent_ranges=confirmed_silent_ranges,
            silence_evidence=silence_evidence,
        )
        if diagnostic is None:
            write_srt(output, primary_blocks)
            raise TranscriptionError(
                "Selective ASR refused a silent-artifact omission because its "
                "durable diagnostic could not be written"
            )
    logger.info(
        "Selective ASR repair complete ranges=%s replaced_blocks=%s "
        "confirmed_silent_ranges=%s final_blocks=%s output=%s",
        len(ranges),
        len(repaired_blocks),
        len(confirmed_silent_ranges),
        len(normalized),
        output,
    )
    return SelectiveRepairResult(
        path=output,
        segment_confidences=tuple(
            _deduplicate_segment_confidences(repaired_confidences)
        ),
        confirmed_silent_ranges=tuple(confirmed_silent_ranges),
    )


def _prompt_free_selective_config(config: AppConfig) -> AppConfig:
    return _config_with_overrides(
        config,
        whisper_initial_prompt=None,
        op_ed_initial_prompt=None,
    )


def _config_with_overrides(config: AppConfig, **overrides: Any) -> AppConfig:
    if is_dataclass(config):
        return replace(config, **overrides)
    cloned = copy.copy(config)
    for key, value in overrides.items():
        setattr(cloned, key, value)
    return cloned


def finalize_repaired_transcription(
    audio_path: str | Path,
    srt_path: str | Path,
    review_ranges: list[tuple[float, float]],
    config: AppConfig,
    logger: logging.Logger,
    *,
    segment_confidences: list[SegmentConfidence] | tuple[SegmentConfidence, ...] | None = None,
    require_confidence: bool = False,
) -> Path:
    """Run publication quality gates on the merged selective-repair result."""

    output = Path(srt_path)
    blocks = read_srt(output)
    if not blocks:
        raise TranscriptionError("Selective ASR repair produced an empty SRT")
    chunks: list[tuple[float, float, str]] = []
    for block in blocks:
        text = " ".join(line.strip() for line in block.text if line.strip())
        if not text:
            raise TranscriptionError(
                f"Selective ASR repair produced empty text at subtitle index {block.index}"
            )
        start, end = _srt_timing_seconds(block.timing)
        chunks.append((start, end, text))

    artifact_indexes = asr_artifact_line_indexes(
        (text for _start, _end, text in chunks),
        config,
    )
    if artifact_indexes:
        sample_indexes = ",".join(str(index + 1) for index in sorted(artifact_indexes)[:12])
        raise TranscriptionError(
            "Selective ASR repair failed final artifact validation "
            f"at subtitle indexes={sample_indexes}"
        )

    normalized_ranges = _normalize_review_ranges(review_ranges)
    confidences = list(segment_confidences or [])
    confirmed_silent_ranges, silence_evidence = (
        _selective_silence_evidence_from_diagnostics(output, config)
    )
    try:
        if require_confidence:
            observed = [item for item in confidences if item.avg_logprob is not None]
            required = max(
                1,
                int(getattr(config, "transcription_quality_min_confidence_segments", 8) or 8),
            )
            if len(observed) < required:
                raise TranscriptionError(
                    "Selective ASR confidence validation is unavailable: "
                    f"observed={len(observed)} required={required}"
                )
        _validate_transcription_quality(
            audio_path,
            chunks,
            config,
            logger,
            segment_confidences=confidences,
        )
    except TranscriptionError as exc:
        _write_asr_diagnostics(
            output,
            audio_path,
            chunks,
            confidences,
            config,
            status="selective_repair_rejected",
            review_ranges=normalized_ranges,
            reason_code=str(getattr(exc, "reason_code", "") or "final_quality_rejected"),
            confirmed_silent_ranges=confirmed_silent_ranges,
            silence_evidence=silence_evidence,
        )
        raise

    accepted_diagnostic = _write_asr_diagnostics(
        output,
        audio_path,
        chunks,
        confidences,
        config,
        status="accepted_after_selective_retry",
        repaired_ranges=normalized_ranges,
        reason_code=(
            "prompt_free_artifact_confirmed_silent"
            if confirmed_silent_ranges
            else ""
        ),
        confirmed_silent_ranges=confirmed_silent_ranges,
        silence_evidence=silence_evidence,
    )
    if confirmed_silent_ranges and accepted_diagnostic is None:
        raise TranscriptionError(
            "Selective ASR refused publication because the accepted silent-artifact "
            "diagnostic could not be written"
        )
    if bool(getattr(config, "write_gap_report", True)):
        _write_gap_report(output, chunks, config)
    logger.info(
        "Selective ASR repair passed final quality validation ranges=%s blocks=%s output=%s",
        len(normalized_ranges),
        len(blocks),
        output,
    )
    return output


def _selective_silence_evidence_from_diagnostics(
    output: Path,
    config: AppConfig,
) -> tuple[list[tuple[float, float]], list[dict[str, Any]]]:
    payload = read_asr_diagnostics(output, config)
    if payload.get("status") != "selective_repair_completed":
        return [], []
    if str(payload.get("srt_sha256") or "") != str(_sha256_if_file(output) or ""):
        return [], []
    ranges = [
        (float(start), float(end))
        for start, end in _diagnostic_review_ranges(
            payload.get("confirmed_silent_ranges")
        )
    ]
    raw_evidence = payload.get("selective_silence_evidence")
    evidence = (
        [dict(item) for item in raw_evidence if isinstance(item, dict)]
        if isinstance(raw_evidence, list)
        else []
    )
    if not ranges or not evidence:
        return [], []
    return ranges, evidence


def _is_empty_selective_asr_result(exc: Exception) -> bool:
    message = str(exc)
    return (
        "Whisper returned no subtitle segments" in message
        or "returned no subtitle blocks" in message
        or "SRT is empty" in message
    )


def _prompt_free_artifact_silence_evidence(
    sample_path: str | Path,
    exc: Exception,
    clip_start: float,
    clip_end: float,
    config: AppConfig,
) -> list[dict[str, Any]]:
    """Return same-window evidence for a known prompt-free ASR artifact.

    A generic transcription failure or low-confidence segment is never eligible
    for omission.  Even a known artifact remains fail-closed unless Silero VAD
    and a conservative PCM energy check independently agree that its exact
    rejected window contains no speech.
    """

    if not isinstance(exc, LowConfidenceTranscriptionError):
        return []
    if str(getattr(exc, "reason_code", "")) != "asr_artifact":
        return []
    review_ranges = _normalize_review_ranges(list(getattr(exc, "review_ranges", []) or []))
    if not review_ranges:
        return []

    duration = _wav_duration_seconds(sample_path)
    evidence: list[dict[str, Any]] = []
    for local_start, local_end in review_ranges:
        bounded_start = max(0.0, local_start)
        bounded_end = min(float(duration), local_end) if duration is not None else local_end
        item = _selective_window_silence_evidence(
            sample_path,
            bounded_start,
            bounded_end,
            config,
        )
        item.update(
            {
                "clip_range": [round(clip_start, 3), round(clip_end, 3)],
                "local_range": [round(bounded_start, 3), round(bounded_end, 3)],
                "absolute_range": [
                    round(clip_start + bounded_start, 3),
                    round(min(clip_end, clip_start + bounded_end), 3),
                ],
                "reason_code": "prompt_free_asr_artifact",
            }
        )
        evidence.append(item)
    return evidence


def _selective_window_silence_evidence(
    audio_path: str | Path,
    start: float,
    end: float,
    config: AppConfig,
) -> dict[str, Any]:
    try:
        maximum_rms_dbfs = float(
            getattr(
                config,
                "asr_selective_silence_max_rms_dbfs",
                SELECTIVE_SILENCE_MAX_RMS_DBFS,
            )
        )
    except (TypeError, ValueError):
        maximum_rms_dbfs = SELECTIVE_SILENCE_MAX_RMS_DBFS
    if not math.isfinite(maximum_rms_dbfs):
        maximum_rms_dbfs = SELECTIVE_SILENCE_MAX_RMS_DBFS
    evidence: dict[str, Any] = {
        "confirmed": False,
        "vad_no_speech": False,
        "energy_no_speech": False,
        "rms_dbfs": None,
        "maximum_rms_dbfs": maximum_rms_dbfs,
    }
    if end <= start:
        evidence["error"] = "empty analysis window"
        return evidence

    try:
        import numpy as np

        with wave.open(str(audio_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            total_frames = wav_file.getnframes()
            if channels != 1 or sample_width != 2 or sample_rate != 16000:
                raise ValueError(
                    "selective silence evidence requires mono 16 kHz signed 16-bit PCM"
                )
            first_frame = min(total_frames, max(0, int(math.floor(start * sample_rate))))
            last_frame = min(total_frames, max(first_frame, int(math.ceil(end * sample_rate))))
            wav_file.setpos(first_frame)
            raw = wav_file.readframes(last_frame - first_frame)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size == 0:
            raise ValueError("analysis window contains no PCM frames")
        rms = float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))
        rms_dbfs = 20.0 * math.log10(max(rms, 1e-8))
        speech_timestamps = _silero_speech_timestamps(samples, config)
        evidence.update(
            {
                "vad_no_speech": not bool(speech_timestamps),
                "energy_no_speech": rms_dbfs <= maximum_rms_dbfs,
                "rms_dbfs": round(rms_dbfs, 2),
                "vad_speech_regions": len(speech_timestamps),
            }
        )
        evidence["confirmed"] = bool(
            evidence["vad_no_speech"] and evidence["energy_no_speech"]
        )
    except Exception as evidence_error:  # noqa: BLE001 - evidence failures must fail closed.
        evidence["error"] = str(evidence_error)[:240]
    return evidence


def _silero_speech_timestamps(samples: Any, config: AppConfig) -> list[dict[str, int]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(
        threshold=float(getattr(config, "whisper_vad_threshold", 0.5)),
        min_speech_duration_ms=0,
        min_silence_duration_ms=int(
            getattr(config, "whisper_vad_min_silence_duration_ms", 2000)
        ),
        speech_pad_ms=int(getattr(config, "whisper_vad_speech_pad_ms", 400)),
    )
    return get_speech_timestamps(samples, vad_options=options, sampling_rate=16000)


def _evidence_absolute_ranges(
    evidence: list[dict[str, Any]],
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for item in evidence:
        if not bool(item.get("confirmed")):
            continue
        raw_range = item.get("absolute_range")
        if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
            continue
        try:
            ranges.append((float(raw_range[0]), float(raw_range[1])))
        except (TypeError, ValueError):
            continue
    return _normalize_review_ranges(ranges)


def _timing_contained_by_ranges(
    timing: str,
    ranges: list[tuple[float, float]],
) -> bool:
    start, end = _srt_timing_seconds(timing)
    tolerance = 0.05
    return any(
        start >= range_start - tolerance and end <= range_end + tolerance
        for range_start, range_end in ranges
    )


def _deduplicate_repaired_blocks(blocks: list[SrtBlock]) -> list[SrtBlock]:
    deduplicated: list[SrtBlock] = []
    for candidate in sorted(blocks, key=lambda block: _srt_timing_seconds(block.timing)[0]):
        candidate_start, candidate_end = _srt_timing_seconds(candidate.timing)
        candidate_text = _normalize_text(" ".join(candidate.text))
        duplicate = False
        for existing in deduplicated:
            existing_start, existing_end = _srt_timing_seconds(existing.timing)
            existing_text = _normalize_text(" ".join(existing.text))
            if candidate_text != existing_text:
                continue
            overlap = min(candidate_end, existing_end) - max(candidate_start, existing_start)
            centers_close = abs(
                (candidate_start + candidate_end) / 2 - (existing_start + existing_end) / 2
            ) <= 2.0
            if overlap > 0 or centers_close:
                duplicate = True
                break
        if not duplicate:
            deduplicated.append(candidate)
    return deduplicated


def _low_confidence_review_ranges(
    confidences: list[SegmentConfidence],
    config: AppConfig,
) -> list[tuple[float, float]]:
    padding = max(0.0, float(getattr(config, "asr_selective_retry_padding_seconds", 1.5) or 0.0))
    merge_gap = max(0.0, float(getattr(config, "asr_selective_retry_merge_gap_seconds", 3.0) or 0.0))
    ranges = sorted((max(0.0, item.start - padding), max(item.start, item.end + padding)) for item in confidences)
    merged: list[list[float]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1] + merge_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(round(start, 3), round(end, 3)) for start, end in merged]


def _normalize_review_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    valid = sorted(
        (max(0.0, float(start)), max(0.0, float(end)))
        for start, end in ranges
        if float(end) > float(start)
    )
    merged: list[list[float]] = []
    for start, end in valid:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _expand_review_ranges_to_primary_blocks(
    primary_blocks: list[SrtBlock],
    ranges: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Expand repair ranges until every intersecting primary block is decoded.

    Selective replacement removes every primary block that overlaps a repair
    range.  Expanding the decode range to the complete timing of those blocks
    prevents content outside the original review window from being discarded.
    Expansion can expose another overlapping block, so repeat to a fixed point;
    normalization also merges ranges that meet during expansion.
    """

    expanded = _normalize_review_ranges(ranges)
    block_ranges = [
        _srt_timing_seconds(block.timing)
        for block in primary_blocks
    ]
    while expanded:
        previous = expanded
        adjusted: list[tuple[float, float]] = []
        for start, end in previous:
            overlapping = [
                (block_start, block_end)
                for block_start, block_end in block_ranges
                if block_end > start and block_start < end
            ]
            if overlapping:
                start = min(start, *(block_start for block_start, _block_end in overlapping))
                end = max(end, *(block_end for _block_start, block_end in overlapping))
            adjusted.append((start, end))
        expanded = _normalize_review_ranges(adjusted)
        if expanded == previous:
            break
    return expanded


def _srt_timing_seconds(timing: str) -> tuple[float, float]:
    start_text, end_text = [part.strip() for part in timing.split("-->", 1)]
    return _srt_timestamp_seconds(start_text), _srt_timestamp_seconds(end_text)


def _srt_timestamp_seconds(value: str) -> float:
    match = re.fullmatch(r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})", value.strip())
    if match is None:
        raise TranscriptionError(f"Invalid SRT timestamp during selective repair: {value!r}")
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(match.group("ms")) / 1000.0
    )


def _timing_overlaps_range(timing: str, start: float, end: float) -> bool:
    block_start, block_end = _srt_timing_seconds(timing)
    return block_end > start and block_start < end


def asr_file_fingerprint(
    path: str | Path,
    *,
    full_hash: bool = False,
) -> dict[str, Any]:
    """Return a bounded, structured fingerprint for ASR recovery guards."""

    source = Path(path).resolve()
    before = source.stat()
    size = int(before.st_size)
    if full_hash or size <= 8 * 1024 * 1024:
        digest = _sha256_if_file(source)
        method = "sha256-full"
    else:
        digest_builder = hashlib.sha256()
        digest_builder.update(f"size={size}\n".encode("ascii"))
        sample_size = 1024 * 1024
        offsets = sorted(
            {
                0,
                max(0, (size // 2) - (sample_size // 2)),
                max(0, size - sample_size),
            }
        )
        with source.open("rb") as handle:
            for offset in offsets:
                handle.seek(offset)
                sample = handle.read(min(sample_size, max(0, size - offset)))
                digest_builder.update(f"offset={offset};length={len(sample)}\n".encode("ascii"))
                digest_builder.update(sample)
        digest = digest_builder.hexdigest()
        method = "sha256-size-head-middle-tail-v1"
    after = source.stat()
    if (
        int(after.st_size) != size
        or int(after.st_mtime_ns) != int(before.st_mtime_ns)
        or not digest
    ):
        raise OSError(f"file changed while computing ASR fingerprint: {source}")
    identity = {
        "path": str(source),
        "size": size,
        "mtime_ns": int(after.st_mtime_ns),
        "method": method,
        "digest": digest,
    }
    identity["fingerprint"] = _canonical_sha256(identity)
    return identity


def asr_audio_stream_fingerprint(stream: object | None) -> dict[str, Any]:
    """Return a stable fingerprint for the selected source audio stream."""

    if stream is None:
        selection: object = {"selection": "ffmpeg-default"}
    elif hasattr(stream, "to_dict") and callable(getattr(stream, "to_dict")):
        selection = getattr(stream, "to_dict")()
    elif isinstance(stream, dict):
        selection = dict(stream)
    else:
        selection = {"selection": str(stream)}
    payload = {"selection": selection}
    payload["fingerprint"] = _canonical_sha256(payload)
    return payload


def attach_asr_diagnostics_context(
    srt_path: str | Path,
    config: AppConfig,
    *,
    media_path: str | Path,
    audio_path: str | Path,
    audio_stream: object | None,
) -> dict[str, Any]:
    """Hash-bind ASR diagnostics to media, audio stream, audio and cache."""

    output = Path(srt_path)
    payload = read_asr_diagnostics(output, config)
    if not payload or not output.is_file():
        return {}
    actual_srt_sha256 = _sha256_if_file(output)
    diagnosed_srt_sha256 = str(payload.get("srt_sha256") or "").strip().casefold()
    if not actual_srt_sha256 or diagnosed_srt_sha256 != actual_srt_sha256.casefold():
        return {}

    context = _build_asr_diagnostics_context(
        payload,
        output,
        media_path=media_path,
        audio_path=audio_path,
        audio_stream=audio_stream,
    )
    payload.update(context)
    payload["schema_version"] = max(2, int(payload.get("schema_version") or 0))
    payload["context_bound_at"] = time.time()
    path = asr_diagnostics_path(output, config)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return context


def verify_asr_diagnostics_context(
    srt_path: str | Path,
    config: AppConfig,
    *,
    media_path: str | Path,
    audio_path: str | Path,
    audio_stream: object | None,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Verify current ASR inputs against the context captured at rejection."""

    output = Path(srt_path)
    payload = read_asr_diagnostics(output, config)
    if not payload:
        return False, ["ASR diagnostics missing or corrupt"], {}
    try:
        current = _build_asr_diagnostics_context(
            payload,
            output,
            media_path=media_path,
            audio_path=audio_path,
            audio_stream=audio_stream,
        )
    except OSError as exc:
        return False, [f"ASR fingerprint unavailable: {exc}"], {}

    reasons: list[str] = []
    for key, label in (
        ("media_fingerprint", "source media"),
        ("audio_fingerprint", "extracted audio"),
        ("audio_stream_fingerprint", "audio stream"),
        ("cache_fingerprint", "Japanese SRT cache"),
    ):
        expected = payload.get(key)
        observed = current.get(key)
        if not isinstance(expected, dict) or not str(expected.get("fingerprint") or ""):
            reasons.append(f"{label} fingerprint missing")
            continue
        if not isinstance(observed, dict) or (
            str(expected.get("fingerprint")) != str(observed.get("fingerprint"))
        ):
            reasons.append(f"{label} fingerprint mismatch")

    expected_repair = str(payload.get("repair_fingerprint") or "").strip()
    observed_repair = str(current.get("repair_fingerprint") or "").strip()
    if not expected_repair:
        reasons.append("repair fingerprint missing")
    elif expected_repair != observed_repair:
        reasons.append("repair fingerprint mismatch")
    return not reasons, reasons, current


def claim_asr_repair_attempt(
    srt_path: str | Path,
    config: AppConfig,
    repair_fingerprint: str,
) -> bool:
    """Atomically record the one permitted selective attempt per fingerprint."""

    output = Path(srt_path)
    payload = read_asr_diagnostics(output, config)
    expected = str(repair_fingerprint or "").strip()
    if (
        not payload
        or not expected
        or str(payload.get("repair_fingerprint") or "").strip() != expected
        or str(payload.get("status") or "")
        not in {"selective_retry_required", "selective_repair_rejected"}
    ):
        return False
    raw_attempts = payload.get("repair_attempts")
    attempts = [item for item in raw_attempts if isinstance(item, dict)] if isinstance(raw_attempts, list) else []
    if any(str(item.get("fingerprint") or "") == expected for item in attempts):
        return False
    attempts.append({"fingerprint": expected, "attempted_at": time.time()})
    payload["repair_attempts"] = attempts[-16:]
    payload["repair_attempted"] = True
    payload["repair_attempted_fingerprints"] = [
        str(item.get("fingerprint") or "")
        for item in payload["repair_attempts"]
        if str(item.get("fingerprint") or "")
    ]
    atomic_write_text(
        asr_diagnostics_path(output, config),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return True


def _build_asr_diagnostics_context(
    payload: dict[str, Any],
    srt_path: Path,
    *,
    media_path: str | Path,
    audio_path: str | Path,
    audio_stream: object | None,
) -> dict[str, Any]:
    media_fingerprint = asr_file_fingerprint(media_path)
    audio_fingerprint = asr_file_fingerprint(audio_path)
    stream_fingerprint = asr_audio_stream_fingerprint(audio_stream)
    cache_fingerprint = asr_file_fingerprint(srt_path, full_hash=True)
    review_ranges = _diagnostic_review_ranges(payload.get("review_ranges"))
    repair_basis = {
        "schema_version": 1,
        "status": str(payload.get("status") or ""),
        "reason_code": str(payload.get("reason_code") or ""),
        "review_ranges": review_ranges,
        "model": str(payload.get("model") or ""),
        "compute_type": str(payload.get("compute_type") or ""),
        "media_fingerprint": str(media_fingerprint["fingerprint"]),
        "audio_fingerprint": str(audio_fingerprint["fingerprint"]),
        "audio_stream_fingerprint": str(stream_fingerprint["fingerprint"]),
        "cache_fingerprint": str(cache_fingerprint["fingerprint"]),
    }
    return {
        "review_ranges": review_ranges,
        "media_fingerprint": media_fingerprint,
        "audio_fingerprint": audio_fingerprint,
        "audio_stream_fingerprint": stream_fingerprint,
        "cache_fingerprint": cache_fingerprint,
        "repair_fingerprint": _canonical_sha256(repair_basis),
        "repair_basis": repair_basis,
    }


def _diagnostic_review_ranges(raw_ranges: object) -> list[list[float]]:
    parsed: list[tuple[float, float]] = []
    if isinstance(raw_ranges, list):
        for raw in raw_ranges[:64]:
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                continue
            try:
                parsed.append((float(raw[0]), float(raw[1])))
            except (TypeError, ValueError):
                continue
    return [[start, end] for start, end in _normalize_review_ranges(parsed)]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_asr_diagnostics(
    srt_path: Path,
    audio_path: str | Path,
    raw_segments: list[tuple[float, float, str]],
    confidences: list[SegmentConfidence],
    config: AppConfig,
    *,
    status: str,
    review_ranges: list[tuple[float, float]] | None = None,
    repaired_ranges: list[tuple[float, float]] | None = None,
    reason_code: str = "",
    confirmed_silent_ranges: list[tuple[float, float]] | None = None,
    silence_evidence: list[dict[str, Any]] | None = None,
    confirmed_tail_artifact_ranges: list[tuple[float, float]] | None = None,
    tail_consensus_evidence: list[dict[str, Any]] | None = None,
) -> Path | None:
    if not bool(getattr(config, "asr_diagnostics_enabled", True)):
        return None
    path = asr_diagnostics_path(srt_path, config)
    observed = [item.avg_logprob for item in confidences if item.avg_logprob is not None]
    threshold = float(getattr(config, "transcription_quality_min_avg_logprob", -1.0))
    payload = {
        "schema_version": 1,
        "status": status,
        "srt_path": str(srt_path),
        "audio_path": str(audio_path),
        "model": str(getattr(config, "whisper_model", "")),
        "compute_type": str(getattr(config, "whisper_compute_type", "")),
        "segments": len(raw_segments),
        "confidence_segments": [asdict(item) for item in confidences],
        "avg_logprob": round(sum(observed) / len(observed), 4) if observed else None,
        "low_confidence_segments": sum(1 for value in observed if value < threshold),
        "review_ranges": review_ranges or [],
        "repaired_ranges": repaired_ranges or [],
        "reason_code": str(reason_code or ""),
        "srt_sha256": _sha256_if_file(srt_path),
    }
    if confirmed_silent_ranges:
        payload["confirmed_silent_ranges"] = _diagnostic_review_ranges(
            confirmed_silent_ranges
        )
    if silence_evidence:
        payload["selective_silence_evidence"] = [
            dict(item) for item in silence_evidence if isinstance(item, dict)
        ]
    if confirmed_tail_artifact_ranges:
        payload["confirmed_tail_artifact_ranges"] = _diagnostic_review_ranges(
            confirmed_tail_artifact_ranges
        )
    if tail_consensus_evidence:
        payload["tail_consensus_evidence"] = [
            dict(item) for item in tail_consensus_evidence if isinstance(item, dict)
        ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    except OSError:
        return None
    return path


def asr_diagnostics_path(srt_path: str | Path, config: AppConfig) -> Path:
    configured = Path(str(getattr(config, "asr_diagnostics_path", "asr_diagnostics") or "asr_diagnostics"))
    root = configured if configured.is_absolute() else Path(config.work_path) / configured
    digest = hashlib.sha1(str(Path(srt_path).resolve()).encode("utf-8")).hexdigest()[:20]
    return root / f"{digest}.json"


def asr_transcription_hold_path(srt_path: str | Path, config: AppConfig) -> Path:
    """Return the durable pending marker for a live ASR SRT commit."""

    diagnostics = asr_diagnostics_path(srt_path, config)
    return diagnostics.with_name(f"{diagnostics.stem}.pending.json")


def read_asr_diagnostics(srt_path: str | Path, config: AppConfig) -> dict[str, Any]:
    path = asr_diagnostics_path(srt_path, config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    diagnosed_path = str(payload.get("srt_path") or "").strip()
    if diagnosed_path:
        try:
            if Path(diagnosed_path).resolve() != Path(srt_path).resolve():
                return {}
        except OSError:
            return {}
    return payload


def promote_asr_diagnostics(
    source_srt_path: str | Path,
    target_srt_path: str | Path,
    config: AppConfig,
) -> Path | None:
    """Relocate verified ASR diagnostics alongside a committed SRT cache."""

    source = Path(source_srt_path)
    target = Path(target_srt_path)
    payload = read_asr_diagnostics(source, config)
    if not payload or not target.is_file():
        return None
    source_sha256 = str(payload.get("srt_sha256") or "").strip()
    target_sha256 = _sha256_if_file(target)
    if not source_sha256 or source_sha256 != target_sha256:
        return None
    payload["srt_path"] = str(target)
    payload["srt_sha256"] = target_sha256
    destination = asr_diagnostics_path(target, config)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(destination)
    except OSError:
        return None
    return destination


def write_asr_acceptance_diagnostics(
    srt_path: str | Path,
    audio_path: str | Path,
    config: AppConfig,
    *,
    status: str = "accepted",
    repaired_ranges: list[tuple[float, float]] | None = None,
    segment_confidences: list[SegmentConfidence]
    | tuple[SegmentConfidence, ...]
    | None = None,
) -> Path | None:
    """Persist a hash-bound acceptance record for a committed ASR cache."""

    output = Path(srt_path)
    blocks = read_srt(output)
    chunks = [
        (
            _srt_timing_seconds(block.timing)[0],
            _srt_timing_seconds(block.timing)[1],
            " ".join(line.strip() for line in block.text if line.strip()),
        )
        for block in blocks
    ]
    if not chunks or any(not text for _start, _end, text in chunks):
        return None
    return _write_asr_diagnostics(
        output,
        audio_path,
        chunks,
        list(segment_confidences or []),
        config,
        status=str(status),
        repaired_ranges=list(repaired_ranges or []),
    )


def _confidence_segments_from_diagnostics(
    srt_path: str | Path,
    config: AppConfig,
    *,
    offset_seconds: float,
    accepted_range: tuple[float, float],
) -> list[SegmentConfidence]:
    payload = read_asr_diagnostics(srt_path, config)
    raw_segments = payload.get("confidence_segments")
    if not isinstance(raw_segments, list):
        return []
    accepted_start, accepted_end = accepted_range
    result: list[SegmentConfidence] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start")) + offset_seconds
            end = float(raw.get("end")) + offset_seconds
        except (TypeError, ValueError):
            continue
        center = (start + end) / 2
        if center < accepted_start or center > accepted_end:
            continue
        result.append(
            SegmentConfidence(
                start=start,
                end=end,
                avg_logprob=_optional_float(raw.get("avg_logprob")),
                no_speech_prob=_optional_float(raw.get("no_speech_prob")),
                compression_ratio=_optional_float(raw.get("compression_ratio")),
            )
        )
    return result


def _deduplicate_segment_confidences(
    confidences: list[SegmentConfidence],
) -> list[SegmentConfidence]:
    result: list[SegmentConfidence] = []
    seen: set[tuple[object, ...]] = set()
    for item in sorted(confidences, key=lambda value: (value.start, value.end)):
        key = (
            round(item.start, 3),
            round(item.end, 3),
            item.avg_logprob,
            item.no_speech_prob,
            item.compression_ratio,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sha256_if_file(path: str | Path) -> str:
    source = Path(path)
    try:
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _wav_duration_seconds(audio_path: str | Path) -> float | None:
    try:
        with wave.open(str(audio_path), "rb") as handle:
            frame_rate = handle.getframerate()
            if frame_rate <= 0:
                return None
            return handle.getnframes() / frame_rate
    except (OSError, EOFError, wave.Error):
        return None


def _add_nvidia_dll_directories(logger: logging.Logger) -> None:
    if os.name != "nt":
        return

    candidates: list[Path] = []
    for package_path in site.getsitepackages():
        nvidia_path = Path(package_path) / "nvidia"
        candidates.extend(nvidia_path.glob("*/bin"))

    added = 0
    for path in candidates:
        if not path.exists():
            continue
        try:
            os.add_dll_directory(str(path))
            os.environ["PATH"] = f"{path}{os.pathsep}{os.environ.get('PATH', '')}"
            added += 1
        except OSError as exc:
            logger.warning("Failed to add NVIDIA DLL directory %s: %s", path, exc)

    if added:
        logger.info("Added %s NVIDIA DLL directorie(s) for faster-whisper CUDA.", added)
