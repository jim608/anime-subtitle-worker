from __future__ import annotations

import gc
import logging
import threading
from typing import Any


_LOCK = threading.RLock()
_MODEL: Any | None = None
_MODEL_KEY: tuple[int, str, str, str] | None = None


def get_whisper_model(
    model_name: str,
    *,
    device: str,
    compute_type: str,
    cache_enabled: bool = True,
    logger: logging.Logger | None = None,
) -> Any:
    """Return a process-local faster-whisper model.

    The worker normally performs language detection and transcription in the
    same isolated subprocess. Reusing the model removes a second large model
    load while still keeping GPU ownership isolated from other videos.
    """

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed. Run: pip install -r requirements.txt") from exc

    key = (id(WhisperModel), str(model_name), str(device), str(compute_type))
    if not cache_enabled:
        return WhisperModel(model_name, device=device, compute_type=compute_type)

    global _MODEL, _MODEL_KEY
    with _LOCK:
        if _MODEL is not None and _MODEL_KEY == key:
            if logger is not None:
                logger.info(
                    "Reusing loaded Whisper model model=%s device=%s compute_type=%s",
                    model_name,
                    device,
                    compute_type,
                )
            return _MODEL

        clear_whisper_model_cache(logger=logger)
        if logger is not None:
            logger.info(
                "Loading Whisper model model=%s device=%s compute_type=%s",
                model_name,
                device,
                compute_type,
            )
        _MODEL = WhisperModel(model_name, device=device, compute_type=compute_type)
        _MODEL_KEY = key
        return _MODEL


def clear_whisper_model_cache(*, logger: logging.Logger | None = None) -> None:
    global _MODEL, _MODEL_KEY
    with _LOCK:
        had_model = _MODEL is not None
        _MODEL = None
        _MODEL_KEY = None
        # A failed WhisperModel constructor can leave temporary CUDA owners
        # even though the cache was never assigned. Always collect before a
        # bounded lower-memory retry, not only when _MODEL was populated.
        gc.collect()
        _release_torch_cuda_cache()
        if had_model and logger is not None:
            logger.info("Released cached Whisper model")


def whisper_model_cache_info() -> dict[str, str | bool | None]:
    with _LOCK:
        if _MODEL_KEY is None:
            return {"loaded": False, "model": None, "device": None, "compute_type": None}
        return {
            "loaded": True,
            "model": _MODEL_KEY[1],
            "device": _MODEL_KEY[2],
            "compute_type": _MODEL_KEY[3],
        }


def _release_torch_cuda_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, RuntimeError):
        return
