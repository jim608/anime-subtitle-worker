from __future__ import annotations

from typing import Any


MIB = 1024 * 1024


def cuda_free_memory_mib(torch_module: Any) -> int | None:
    """Return currently free CUDA memory without reserving any GPU memory."""

    try:
        cuda = torch_module.cuda
        if not bool(cuda.is_available()):
            return None
        free_bytes, _total_bytes = cuda.mem_get_info()
        return max(0, int(free_bytes) // MIB)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def adaptive_whisper_batch_size(
    configured_batch_size: int,
    *,
    free_memory_mib: int | None,
    model_name: str,
    reserve_memory_mib: int = 2048,
) -> int:
    """Choose a conservative ASR batch size from live free VRAM.

    The model itself is already loaded when WhisperX calls this helper, while
    Transformers may call it immediately before loading.  Keeping a fixed
    reserve protects the RTX 3060 desktop session and the translation runtime;
    CUDA OOM backoff remains the final guardrail.
    """

    configured = max(1, int(configured_batch_size or 1))
    if free_memory_mib is None:
        return configured
    usable = max(0, int(free_memory_mib) - max(0, int(reserve_memory_mib or 0)))
    normalized_model = str(model_name or "").casefold()
    is_large = "large" in normalized_model
    if is_large:
        if usable >= 8192:
            cap = 8
        elif usable >= 5632:
            cap = 6
        elif usable >= 3584:
            cap = 4
        elif usable >= 2048:
            cap = 2
        else:
            cap = 1
    else:
        if usable >= 8192:
            cap = 16
        elif usable >= 5632:
            cap = 12
        elif usable >= 3584:
            cap = 8
        elif usable >= 2048:
            cap = 4
        else:
            cap = 1
    return max(1, min(configured, cap))


def is_cuda_oom(error: BaseException) -> bool:
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    return (
        "outofmemory" in name
        or "cuda out of memory" in message
        or "cuda error: out of memory" in message
        or "cuda failed with error out of memory" in message
        or "cublas_status_alloc_failed" in message
    )


def release_cuda_cache(torch_module: Any) -> None:
    try:
        if bool(torch_module.cuda.is_available()):
            torch_module.cuda.empty_cache()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return
