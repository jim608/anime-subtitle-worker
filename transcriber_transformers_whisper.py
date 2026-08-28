from __future__ import annotations

import gc
from pathlib import Path
import logging
import os
from typing import Any

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
    _format_timestamp,
    _normalize_timing,
    _split_word_chunks,
    _validate_transcription_quality,
    _write_gap_report,
)


class TransformersWhisperTranscriptionError(TranscriptionError):
    pass


def transcribe_to_srt_with_transformers_whisper(
    audio_path: str | Path,
    srt_path: str | Path,
    config: AppConfig,
    logger: logging.Logger,
) -> Path:
    _disable_progress_bars()

    try:
        import torch
        from transformers import pipeline
    except ImportError as exc:
        raise TransformersWhisperTranscriptionError(
            "transformers-whisper backend requires torch and transformers. Run: pip install -r requirements.txt"
        ) from exc

    pipe: Any | None = None
    try:
        pipe_kwargs = _build_pipeline_kwargs(config, torch)
        dtype_name = str(getattr(config, "transformers_whisper_torch_dtype", "float16") or "auto")
        task_name = str(pipe_kwargs.get("task") or "")
        punctuator_enabled = bool(pipe_kwargs.get("punctuator", False))
        stable_ts_enabled = bool(pipe_kwargs.get("stable_ts", False))
        model_kwargs = pipe_kwargs.get("model_kwargs") if isinstance(pipe_kwargs.get("model_kwargs"), dict) else {}
        attn_implementation = str(model_kwargs.get("attn_implementation") or "").strip()

        logger.info(
            "Loading transformers Whisper ASR model: task=%s model=%s device=%s dtype=%s batch_size=%s trust_remote_code=%s punctuator=%s stable_ts=%s attn=%s",
            task_name,
            config.whisper_model,
            config.whisper_device,
            dtype_name,
            int(pipe_kwargs.get("batch_size") or 1),
            bool(getattr(config, "transformers_whisper_trust_remote_code", False)),
            punctuator_enabled,
            stable_ts_enabled,
            attn_implementation or "-",
        )
        pipe = pipeline(**pipe_kwargs)

        generate_kwargs = _build_generate_kwargs(config)
        logger.info(
            "Running transformers Whisper ASR: task=%s model=%s language=%s audio=%s",
            task_name,
            config.whisper_model,
            config.whisper_language,
            audio_path,
        )
        word_timestamps = (
            str(getattr(config, "subtitle_timing_mode", "segment") or "segment")
            .strip()
            .casefold()
            == "word"
        )
        result = _run_pipeline_with_oom_backoff(
            pipe,
            str(audio_path),
            batch_size=int(pipe_kwargs.get("batch_size") or 1),
            torch_module=torch,
            logger=logger,
            call_kwargs={
                "return_timestamps": "word" if word_timestamps else True,
                "chunk_length_s": float(getattr(config, "transformers_whisper_chunk_length_s", 30.0)),
                "generate_kwargs": generate_kwargs,
            },
        )
        if _result_has_plain_text_without_timestamp_chunks(result):
            raise TransformersWhisperTranscriptionError(
                f"Transformers Whisper task={task_name} model={config.whisper_model} returned text "
                "but no timestamp chunks; cannot build subtitle timings."
            )
        raw_segments = _result_to_segments(
            result,
            config=config,
            word_timestamps=word_timestamps,
        )
        raw_segments = _normalize_timing(raw_segments, config)
        if not raw_segments:
            raise TransformersWhisperTranscriptionError("Transformers Whisper returned no subtitle segments.")

        _validate_transcription_quality(audio_path, raw_segments, config, logger)

        blocks = [
            SrtBlock(
                index=index,
                timing=f"{_format_timestamp(start)} --> {_format_timestamp(end)}",
                text=[text],
            )
            for index, (start, end, text) in enumerate(raw_segments, start=1)
        ]

        output = Path(srt_path)
        write_srt(output, blocks)
        if config.write_gap_report:
            _write_gap_report(output, raw_segments, config)
        logger.info("Created Japanese SRT with transformers Whisper: %s", output)
        return output
    except TransformersWhisperTranscriptionError:
        raise
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TransformersWhisperTranscriptionError(
            f"Transformers Whisper transcription failed for {audio_path}: {exc}"
        ) from exc
    finally:
        _release_pipeline_memory(pipe, torch)
        pipe = None


def _release_pipeline_memory(pipe: Any | None, torch_module: Any) -> None:
    if pipe is not None:
        try:
            model = getattr(pipe, "model", None)
            if model is not None and callable(getattr(model, "to", None)):
                model.to("cpu")
        except Exception:
            # Cleanup must never replace the ASR result or its original error.
            pass
    gc.collect()
    release_cuda_cache(torch_module)


def _disable_progress_bars() -> None:
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    try:
        from huggingface_hub.utils import disable_progress_bars as disable_hf_progress_bars

        disable_hf_progress_bars()
    except Exception:
        pass
    try:
        from transformers.utils import logging as transformers_logging

        transformers_logging.disable_progress_bar()
    except Exception:
        pass


def _pipeline_device(device: str) -> int | str:
    normalized = str(device or "").strip().lower()
    if normalized.startswith("cuda"):
        return 0
    if normalized in {"cpu", "-1"}:
        return -1
    return device


def _torch_dtype(torch_module: Any, dtype_name: str) -> Any:
    normalized = dtype_name.strip().lower()
    mapping = {
        "float16": torch_module.float16,
        "fp16": torch_module.float16,
        "bfloat16": torch_module.bfloat16,
        "bf16": torch_module.bfloat16,
        "float32": torch_module.float32,
        "fp32": torch_module.float32,
    }
    if normalized not in mapping:
        raise TransformersWhisperTranscriptionError(f"Unsupported transformers_whisper_torch_dtype: {dtype_name}")
    return mapping[normalized]


def _build_pipeline_kwargs(config: AppConfig, torch_module: Any) -> dict[str, Any]:
    model_name = str(getattr(config, "whisper_model", "") or "")
    task_name = _pipeline_task(config)
    configured_batch_size = int(getattr(config, "transformers_whisper_batch_size", 8))
    effective_batch_size = configured_batch_size
    if (
        bool(getattr(config, "whisper_dynamic_batch_enabled", True))
        and str(getattr(config, "whisper_device", "")).casefold().startswith("cuda")
    ):
        effective_batch_size = adaptive_whisper_batch_size(
            configured_batch_size,
            free_memory_mib=cuda_free_memory_mib(torch_module),
            model_name=model_name,
            reserve_memory_mib=int(getattr(config, "whisper_gpu_memory_reserve_mib", 2048) or 2048),
        )
    pipe_kwargs: dict[str, Any] = {
        "task": task_name,
        "model": model_name,
        "device": _pipeline_device(config.whisper_device),
        "batch_size": effective_batch_size,
    }

    dtype_name = str(getattr(config, "transformers_whisper_torch_dtype", "float16") or "auto")
    if dtype_name != "auto":
        pipe_kwargs["torch_dtype"] = _torch_dtype(torch_module, dtype_name)

    if bool(getattr(config, "transformers_whisper_trust_remote_code", False)):
        pipe_kwargs["trust_remote_code"] = True

    if _supports_kotoba_pipeline_options(task_name):
        if bool(getattr(config, "transformers_whisper_punctuator", False)):
            pipe_kwargs["punctuator"] = True
        if bool(getattr(config, "transformers_whisper_stable_ts", False)):
            pipe_kwargs["stable_ts"] = True

    attn_implementation = str(getattr(config, "transformers_whisper_attn_implementation", "") or "").strip()
    if attn_implementation:
        pipe_kwargs["model_kwargs"] = {"attn_implementation": attn_implementation}

    return pipe_kwargs


def _run_pipeline_with_oom_backoff(
    pipe: Any,
    audio_path: str,
    *,
    batch_size: int,
    torch_module: Any,
    logger: logging.Logger,
    call_kwargs: dict[str, Any],
) -> Any:
    current_batch_size = max(1, int(batch_size or 1))
    while True:
        try:
            return pipe(audio_path, batch_size=current_batch_size, **call_kwargs)
        except Exception as exc:
            if not is_cuda_oom(exc) or current_batch_size <= 1:
                raise
            next_batch_size = max(1, current_batch_size // 2)
            logger.warning(
                "Transformers Whisper CUDA OOM; retrying with a smaller batch. old=%s new=%s",
                current_batch_size,
                next_batch_size,
            )
            release_cuda_cache(torch_module)
            current_batch_size = next_batch_size


def _pipeline_task(config: AppConfig) -> str:
    configured_task = str(getattr(config, "transformers_whisper_task", "") or "").strip()
    if configured_task and configured_task.lower() != "auto":
        return configured_task
    return "automatic-speech-recognition"


def _supports_kotoba_pipeline_options(task_name: str) -> bool:
    return task_name.strip().lower() == "kotoba-whisper"


def _build_generate_kwargs(config: AppConfig) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    language = _transformers_language(
        config.whisper_language,
        model_name=str(getattr(config, "whisper_model", "") or ""),
    )
    if language:
        kwargs["language"] = language
    if config.whisper_task:
        kwargs["task"] = config.whisper_task
    if config.whisper_no_repeat_ngram_size > 0:
        kwargs["no_repeat_ngram_size"] = config.whisper_no_repeat_ngram_size
    if config.whisper_repetition_penalty and config.whisper_repetition_penalty != 1.0:
        kwargs["repetition_penalty"] = config.whisper_repetition_penalty
    return kwargs


def _transformers_language(language: str, *, model_name: str = "") -> str:
    normalized = str(language or "").strip().lower()
    if _is_kotoba_whisper_model(model_name):
        return {
            "ja": "ja",
            "jp": "ja",
            "japanese": "ja",
            "en": "en",
            "english": "en",
            "zh": "zh",
            "zh-cn": "zh",
            "zh-tw": "zh",
        }.get(normalized, normalized)
    return {
        "ja": "Japanese",
        "jp": "Japanese",
        "japanese": "Japanese",
        "en": "English",
        "english": "English",
        "zh": "Chinese",
        "zh-cn": "Chinese",
        "zh-tw": "Chinese",
    }.get(normalized, normalized)


def _is_kotoba_whisper_model(model_name: str) -> bool:
    return str(model_name or "").strip().lower().startswith("kotoba-tech/kotoba-whisper")


def _result_to_segments(
    result: Any,
    *,
    config: AppConfig | Any | None = None,
    word_timestamps: bool = False,
) -> list[tuple[float, float, str]]:
    segments: list[tuple[float, float, str]] = []
    chunks = _extract_chunks(result)
    for chunk in chunks:
        raw_text = str(chunk.get("text") or "")
        text = raw_text if word_timestamps else raw_text.strip()
        timestamp = chunk.get("timestamp")
        if not text.strip() or not isinstance(timestamp, (list, tuple)) or len(timestamp) != 2:
            continue
        start = _safe_float(timestamp[0])
        end = _safe_float(timestamp[1])
        if start is None:
            continue
        if end is None or end <= start:
            end = start + 1.0
        segments.append((start, end, text))

    if segments:
        if word_timestamps:
            if config is None:
                raise TransformersWhisperTranscriptionError(
                    "Word timestamps require subtitle chunking configuration."
                )
            return _split_word_chunks(segments, config)
        return segments

    return []


def _extract_chunks(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        chunks = result.get("chunks")
        return [chunk for chunk in chunks if isinstance(chunk, dict)] if isinstance(chunks, list) else []
    if isinstance(result, list):
        chunks: list[dict[str, Any]] = []
        for item in result:
            chunks.extend(_extract_chunks(item))
        return chunks
    return []


def _result_has_plain_text_without_timestamp_chunks(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    text = str(result.get("text") or "").strip()
    if not text:
        return False
    return not _extract_chunks(result)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
