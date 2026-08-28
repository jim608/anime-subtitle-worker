from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any


DISK_HEAVY_AI_STAGES = {
    "worker",
    "preflight",
    "audio_selection",
    "audio",
    "language_detect",
    "vocal_separation",
    "transcription",
    "source_transcription",
}


@dataclass(frozen=True)
class ResourceDecision:
    extract_workers: int
    ai_stage: str
    ai_disk_active: bool
    io_some_avg10: float
    io_full_avg10: float
    pause_existing_extracts: bool
    reason: str


_EXTRACTION_PRESSURE_PAUSE = threading.Event()


def decide_extraction_resources(
    config: Any,
    *,
    ai_stage: str = "",
    io_pressure: dict[str, dict[str, float]] | None = None,
) -> ResourceDecision:
    """Return one deterministic disk scheduling decision for the Worker.

    Translation is GPU/network bound and may overlap with two subtitle
    extractors. Audio extraction, Demucs and Whisper keep the configured
    single-reader limit. Linux full I/O pressure blocks new claims and asks
    running extractors to pause at their next safe stream boundary.
    """

    stage = str(ai_stage or "").casefold()
    idle_workers = max(1, int(getattr(config, "mikan_extract_workers", 2) or 2))
    disk_workers = min(
        idle_workers,
        max(1, int(getattr(config, "mikan_extract_workers_during_ai", 1) or 1)),
    )
    ai_disk_active = stage in DISK_HEAVY_AI_STAGES
    workers = disk_workers if ai_disk_active else idle_workers
    pressure = io_pressure or {}
    some = float(pressure.get("some", {}).get("avg10", 0.0) or 0.0)
    full = float(pressure.get("full", {}).get("avg10", 0.0) or 0.0)
    enabled = bool(getattr(config, "storage_io_pressure_enabled", True))
    some_threshold = max(
        0.0,
        float(getattr(config, "storage_io_pressure_some_avg10_threshold", 35.0) or 0.0),
    )
    full_threshold = max(
        0.0,
        float(getattr(config, "storage_io_pressure_full_avg10_threshold", 10.0) or 0.0),
    )
    pause = bool(enabled and full_threshold > 0 and full >= full_threshold)
    if pause:
        workers = 0
        reason = "io_full_pressure"
    elif enabled and some_threshold > 0 and some >= some_threshold:
        workers = min(workers, 1)
        reason = "io_some_pressure"
    elif ai_disk_active:
        reason = "ai_disk_stage"
    elif stage:
        reason = "ai_translation_stage"
    else:
        reason = "idle"
    return ResourceDecision(
        extract_workers=workers,
        ai_stage=stage,
        ai_disk_active=ai_disk_active,
        io_some_avg10=some,
        io_full_avg10=full,
        pause_existing_extracts=pause,
        reason=reason,
    )


def set_extraction_pressure_pause(paused: bool) -> None:
    if paused:
        _EXTRACTION_PRESSURE_PAUSE.set()
    else:
        _EXTRACTION_PRESSURE_PAUSE.clear()


def wait_for_extraction_pressure(
    *,
    cancel_event: Any | None = None,
    deadline_monotonic: float | None = None,
    sleep_seconds: float = 0.2,
) -> None:
    """Cooperatively wait at a safe extraction boundary while I/O is full."""

    while _EXTRACTION_PRESSURE_PAUSE.is_set():
        cancelled = bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())
        expired = deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic)
        if cancelled or expired:
            return
        time.sleep(max(0.02, float(sleep_seconds)))


def extraction_pressure_paused() -> bool:
    return _EXTRACTION_PRESSURE_PAUSE.is_set()
