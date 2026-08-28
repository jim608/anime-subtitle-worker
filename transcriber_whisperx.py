from __future__ import annotations

from pathlib import Path
import logging

from config import AppConfig
from gpu_memory import (
    adaptive_whisper_batch_size,
    cuda_free_memory_mib,
    is_cuda_oom,
    release_cuda_cache,
)
from srt_utils import SrtBlock, write_srt
from transcriber import (
    TranscriptionError,
    _add_nvidia_dll_directories,
    _format_timestamp,
    _is_hallucination_text,
    _normalize_timing,
    _split_word_chunks,
    _write_gap_report,
)


def transcribe_to_srt_with_whisperx(
    audio_path: str | Path,
    srt_path: str | Path,
    config: AppConfig,
    logger: logging.Logger,
) -> Path:
    _add_nvidia_dll_directories(logger)

    try:
        import whisperx
    except ImportError as exc:
        raise TranscriptionError(
            "whisperx is not installed. Build a custom image or add whisperx to requirements.txt before using transcription_backend=whisperx."
        ) from exc

    try:
        device = config.whisper_device
        model = whisperx.load_model(
            config.whisper_model,
            device,
            compute_type=config.whisper_compute_type,
            language=config.whisper_language,
            task=config.whisper_task,
            asr_options=_build_asr_options(config),
            vad_method=config.whisperx_vad_method,
            vad_options={
                "chunk_size": config.whisperx_chunk_size,
                "vad_onset": config.whisperx_vad_onset,
                "vad_offset": config.whisperx_vad_offset,
            },
        )
        audio = whisperx.load_audio(str(audio_path))
        torch_module = _optional_torch()
        configured_batch_size = int(config.whisperx_batch_size)
        effective_batch_size = configured_batch_size
        if bool(getattr(config, "whisper_dynamic_batch_enabled", True)) and str(device).casefold().startswith("cuda"):
            effective_batch_size = adaptive_whisper_batch_size(
                configured_batch_size,
                free_memory_mib=cuda_free_memory_mib(torch_module) if torch_module is not None else None,
                model_name=str(config.whisper_model),
                reserve_memory_mib=int(getattr(config, "whisper_gpu_memory_reserve_mib", 2048) or 2048),
            )
        logger.info(
            "Running WhisperX ASR: model=%s device=%s configured_batch=%s effective_batch=%s",
            config.whisper_model,
            device,
            configured_batch_size,
            effective_batch_size,
        )
        result = _transcribe_with_oom_backoff(
            model,
            audio,
            batch_size=effective_batch_size,
            language=config.whisper_language,
            task=config.whisper_task,
            torch_module=torch_module,
            logger=logger,
        )

        align_model, metadata = whisperx.load_align_model(
            language_code=config.whisper_language,
            device=device,
            model_name=config.whisperx_align_model,
        )
        aligned = whisperx.align(
            result["segments"],
            align_model,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )

        raw_segments = _extract_aligned_chunks(aligned.get("segments", []), config)
        raw_segments = _normalize_timing(raw_segments, config)

        blocks = [
            SrtBlock(
                index=index,
                timing=f"{_format_timestamp(start)} --> {_format_timestamp(end)}",
                text=[text],
            )
            for index, (start, end, text) in enumerate(raw_segments, start=1)
        ]
        if not blocks:
            raise TranscriptionError("WhisperX returned no subtitle segments.")

        output = Path(srt_path)
        write_srt(output, blocks)
        if config.write_gap_report:
            _write_gap_report(output, raw_segments, config)
        logger.info("Created Japanese SRT with WhisperX: %s", output)
        return output
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(f"WhisperX transcription failed for {audio_path}: {exc}") from exc


def _extract_aligned_chunks(
    segments: list[dict],
    config: AppConfig,
) -> list[tuple[float, float, str]]:
    chunks: list[tuple[float, float, str]] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text or _is_hallucination_text(text, config):
            continue

        words = _extract_words(segment)
        if words:
            chunks.extend(_split_word_chunks(words, config))
            continue

        start = segment.get("start")
        end = segment.get("end")
        if start is None or end is None:
            continue
        chunks.append((float(start), float(end), text))

    return chunks


def _optional_torch():
    try:
        import torch

        return torch
    except ImportError:
        return None


def _transcribe_with_oom_backoff(
    model,
    audio,
    *,
    batch_size: int,
    language: str,
    task: str,
    torch_module,
    logger: logging.Logger,
):
    current_batch_size = max(1, int(batch_size or 1))
    while True:
        try:
            return model.transcribe(
                audio,
                batch_size=current_batch_size,
                language=language,
                task=task,
            )
        except Exception as exc:
            if not is_cuda_oom(exc) or current_batch_size <= 1:
                raise
            next_batch_size = max(1, current_batch_size // 2)
            logger.warning(
                "WhisperX CUDA OOM; retrying with a smaller batch. old=%s new=%s",
                current_batch_size,
                next_batch_size,
            )
            if torch_module is not None:
                release_cuda_cache(torch_module)
            current_batch_size = next_batch_size


def _build_asr_options(config: AppConfig) -> dict[str, object]:
    options: dict[str, object] = {
        "beam_size": config.whisper_beam_size,
        "best_of": config.whisper_best_of,
        "patience": config.whisper_patience,
        "length_penalty": config.whisper_length_penalty,
        "repetition_penalty": config.whisper_repetition_penalty,
        "no_repeat_ngram_size": config.whisper_no_repeat_ngram_size,
        "temperatures": [float(config.whisper_temperature)],
        "condition_on_previous_text": config.whisper_condition_on_previous_text,
        "initial_prompt": config.whisper_initial_prompt,
        "compression_ratio_threshold": config.whisper_compression_ratio_threshold,
        "log_prob_threshold": config.whisper_log_prob_threshold,
        "no_speech_threshold": config.whisper_no_speech_threshold,
        "hallucination_silence_threshold": config.whisper_hallucination_silence_threshold,
    }
    return {key: value for key, value in options.items() if value is not None}


def _extract_words(segment: dict) -> list[tuple[float, float, str]]:
    extracted: list[tuple[float, float, str]] = []
    for word in segment.get("words") or []:
        text = str(word.get("word") or "").strip()
        start = word.get("start")
        end = word.get("end")
        if not text or start is None or end is None:
            continue
        start_float = float(start)
        end_float = float(end)
        if end_float <= start_float:
            continue
        extracted.append((start_float, end_float, text))
    return extracted
