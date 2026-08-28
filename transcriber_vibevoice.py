from __future__ import annotations

from pathlib import Path
import ast
import json
import logging
import os
import re
from typing import Any

from config import AppConfig
from srt_utils import SrtBlock, write_srt
from transcriber import TranscriptionError, _clean_transcribed_text, _format_timestamp


class VibeVoiceTranscriptionError(TranscriptionError):
    pass


def transcribe_to_srt_with_vibevoice(
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
        raise VibeVoiceTranscriptionError(
            "VibeVoice backend requires torch and transformers. Run: pip install -r requirements.txt"
        ) from exc

    try:
        pipe_kwargs: dict[str, Any] = {
            "task": "any-to-any",
            "model": config.vibevoice_model,
            "trust_remote_code": config.vibevoice_trust_remote_code,
        }
        if config.vibevoice_device_map:
            pipe_kwargs["device_map"] = config.vibevoice_device_map
        if config.vibevoice_torch_dtype != "auto":
            pipe_kwargs["torch_dtype"] = _torch_dtype(torch, config.vibevoice_torch_dtype)

        logger.info("Loading VibeVoice ASR model: %s", config.vibevoice_model)
        pipe = pipeline(**pipe_kwargs)

        generate_kwargs: dict[str, Any] = {}
        if config.vibevoice_max_new_tokens > 0:
            generate_kwargs["max_new_tokens"] = config.vibevoice_max_new_tokens
        if config.vibevoice_tokenizer_chunk_size > 0:
            generate_kwargs["acoustic_tokenizer_chunk_size"] = config.vibevoice_tokenizer_chunk_size

        logger.info("Running VibeVoice ASR: %s", audio_path)
        call_kwargs = {"generate_kwargs": generate_kwargs} if generate_kwargs else {}
        result = pipe(
            text=_build_chat_template(audio_path, config),
            return_full_text=False,
            **call_kwargs,
        )
        blocks = _result_to_blocks(result, config)
        if not blocks:
            raise VibeVoiceTranscriptionError("VibeVoice returned no subtitle blocks.")

        output = Path(srt_path)
        write_srt(output, blocks)
        logger.info("Created Japanese SRT with VibeVoice: %s", output)
        return output
    except VibeVoiceTranscriptionError:
        raise
    except Exception as exc:
        raise VibeVoiceTranscriptionError(f"VibeVoice transcription failed for {audio_path}: {exc}") from exc


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
        raise VibeVoiceTranscriptionError(f"Unsupported vibevoice_torch_dtype: {dtype_name}")
    return mapping[normalized]


def _build_chat_template(audio_path: str | Path, config: AppConfig) -> list[dict[str, Any]]:
    content: list[dict[str, str]] = []
    if config.vibevoice_prompt:
        content.append({"type": "text", "text": config.vibevoice_prompt})
    content.append({"type": "audio", "path": str(audio_path)})
    return [{"role": "user", "content": content}]


def _result_to_blocks(result: Any, config: AppConfig) -> list[SrtBlock]:
    chunks = _extract_chunks(result)
    if chunks:
        return _chunks_to_blocks(chunks, config)

    text = _extract_text(result)
    if not text:
        return []
    cleaned = _clean_transcribed_text(text, config)
    if not cleaned:
        return []
    return [SrtBlock(index=1, timing="00:00:00,000 --> 00:00:05,000", text=[cleaned])]


def _extract_chunks(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        if isinstance(result.get("chunks"), list):
            return result["chunks"]
        text = _extract_text(result)
        return _parse_structured_text(text)

    if isinstance(result, list):
        chunks: list[dict[str, Any]] = []
        for item in result:
            chunks.extend(_extract_chunks(item))
        return chunks

    if isinstance(result, str):
        return _parse_structured_text(result)

    return []


def _extract_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        for key in ("text", "generated_text", "transcription"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _parse_structured_text(text: str) -> list[dict[str, Any]]:
    if not text:
        return []

    payload = _strip_assistant_prefix(text)
    candidates = [payload]
    bracket_match = re.search(r"\[[\s\S]*\]", payload)
    if bracket_match:
        candidates.append(bracket_match.group(0))

    for candidate in candidates:
        parsed = _loads_structured(candidate)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]

    return []


def _strip_assistant_prefix(text: str) -> str:
    stripped = text.strip()
    if stripped.lower().startswith("assistant"):
        return stripped.split("\n", 1)[1].strip() if "\n" in stripped else ""
    return stripped


def _loads_structured(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return ast.literal_eval(text)
    except Exception:
        return None


def _chunks_to_blocks(chunks: list[dict[str, Any]], config: AppConfig) -> list[SrtBlock]:
    blocks: list[SrtBlock] = []
    for chunk in chunks:
        timing = _chunk_timing(chunk)
        text = _chunk_text(chunk)
        text = _clean_transcribed_text(text, config)
        if timing is None or not text:
            continue
        start, end = timing
        if end <= start:
            end = start + max(0.8, config.subtitle_min_duration_seconds)
        blocks.append(
            SrtBlock(
                index=len(blocks) + 1,
                timing=f"{_format_timestamp(start)} --> {_format_timestamp(end)}",
                text=[text],
            )
        )
    return blocks


def _chunk_timing(chunk: dict[str, Any]) -> tuple[float, float] | None:
    if "timestamp" in chunk:
        timestamp = chunk["timestamp"]
        if isinstance(timestamp, (list, tuple)) and len(timestamp) == 2:
            return _float_or_none(timestamp[0]), _float_or_none(timestamp[1])

    start = _first_number(chunk, ("Start", "start", "begin", "Begin"))
    end = _first_number(chunk, ("End", "end", "stop", "Stop"))
    if start is None or end is None:
        return None
    return start, end


def _chunk_text(chunk: dict[str, Any]) -> str:
    for key in ("Content", "content", "text", "Text", "transcription"):
        value = chunk.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_number(chunk: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = chunk.get(key)
        number = _float_or_none(value)
        if number is not None:
            return number
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
