from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from safe_files import atomic_write_text


AI_SCHEDULER_STATE_NAME = "ai_scheduler_state.json"
AI_SCHEDULER_HEARTBEAT_SECONDS = 30.0
AI_SCHEDULER_RETRY_SECONDS = 15.0

_UNSET = object()
_STATE_LOCK = threading.RLock()
_STATE_CACHE: dict[str, dict[str, Any]] = {}
_LAST_WRITE_WARNING_AT: dict[str, float] = {}


def ai_scheduler_state_path(config: Any) -> Path | None:
    work_path = getattr(config, "work_path", None)
    if not work_path:
        return None
    return Path(work_path) / AI_SCHEDULER_STATE_NAME


def classify_ai_scheduler_error(error: BaseException | str) -> str:
    message = str(error or "").casefold()
    if "disk i/o error" in message:
        return "scanner_database_disk_io"
    if any(marker in message for marker in ("database is locked", "database table is locked", "database is busy")):
        return "scanner_database_busy"
    if "database" in message or "sqlite" in message:
        return "scanner_database_error"
    return "scheduler_error"


def update_ai_scheduler_state(
    config: Any,
    *,
    state: str | None = None,
    reason_code: str | None = None,
    message: str | None = None,
    error: str | None = None,
    current_video: str | Path | None | object = _UNSET,
    queue_only: bool | None = None,
    processed_last_cycle: int | None = None,
    next_retry_at: float | None = None,
    cycle_started_at: float | None = None,
    mark_success: bool = False,
    mark_claim: bool = False,
    mark_completed: bool = False,
    increment_error: bool = False,
    reset_errors: bool = False,
    logger: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = ai_scheduler_state_path(config)
    if path is None:
        return {}
    key = str(path)
    now = time.time()
    with _STATE_LOCK:
        payload = dict(_state_payload(path))
        previous_state = str(payload.get("state") or "")
        payload.update(
            {
                "schema_version": 1,
                "worker_pid": os.getpid(),
                "updated_at": now,
            }
        )
        if state is not None:
            normalized_state = str(state or "unknown").strip().casefold() or "unknown"
            payload["state"] = normalized_state
            if normalized_state != previous_state:
                payload["state_changed_at"] = now
        if reason_code is not None:
            payload["reason_code"] = str(reason_code or "")
        if message is not None:
            payload["message"] = str(message or "")
        if error is not None:
            payload["error"] = str(error or "")
        if current_video is not _UNSET:
            payload["current_video"] = str(current_video or "")
        if queue_only is not None:
            payload["queue_only"] = bool(queue_only)
        if processed_last_cycle is not None:
            payload["processed_last_cycle"] = max(0, int(processed_last_cycle))
        if next_retry_at is not None:
            payload["next_retry_at"] = max(0.0, float(next_retry_at))
        if cycle_started_at is not None:
            payload["cycle_started_at"] = max(0.0, float(cycle_started_at))
        if mark_success:
            payload["last_success_at"] = now
        if mark_claim:
            payload["last_claim_at"] = now
        if mark_completed:
            payload["last_completed_at"] = now
        if reset_errors:
            payload["consecutive_errors"] = 0
            payload["error"] = ""
            payload["reason_code"] = ""
            payload["next_retry_at"] = 0.0
        elif increment_error:
            payload["consecutive_errors"] = max(0, int(payload.get("consecutive_errors") or 0)) + 1
        if extra:
            payload.update(extra)
        payload.setdefault("state", "starting")
        payload.setdefault("state_changed_at", now)
        payload.setdefault("consecutive_errors", 0)
        payload.setdefault("next_retry_at", 0.0)
        _STATE_CACHE[key] = dict(payload)
        try:
            atomic_write_text(
                path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            )
        except OSError as exc:
            last_warning_at = float(_LAST_WRITE_WARNING_AT.get(key) or 0)
            if logger is not None and now - last_warning_at >= 60:
                logger.warning("Unable to persist AI scheduler heartbeat. path=%s error=%s", path, exc)
                _LAST_WRITE_WARNING_AT[key] = now
        else:
            _LAST_WRITE_WARNING_AT.pop(key, None)
        return dict(payload)


def report_ai_scheduler_error(
    config: Any,
    error: BaseException | str,
    *,
    reason_code: str | None = None,
    message: str = "",
    retry_seconds: float = AI_SCHEDULER_RETRY_SECONDS,
    logger: Any | None = None,
) -> dict[str, Any]:
    now = time.time()
    return update_ai_scheduler_state(
        config,
        state="error",
        reason_code=reason_code or classify_ai_scheduler_error(error),
        message=message or "AI scheduler could not read its queue and will retry automatically.",
        error=str(error or ""),
        current_video=None,
        next_retry_at=now + max(1.0, float(retry_seconds)),
        increment_error=True,
        logger=logger,
    )


def request_ai_scheduler_retry(config: Any, *, logger: Any | None = None) -> dict[str, Any]:
    now = time.time()
    return update_ai_scheduler_state(
        config,
        next_retry_at=now,
        logger=logger,
        extra={"retry_requested_at": now},
    )


def ai_scheduler_next_retry_at(config: Any) -> float:
    path = ai_scheduler_state_path(config)
    if path is None:
        return 0.0
    with _STATE_LOCK:
        payload = _state_payload(path)
        try:
            return max(0.0, float(payload.get("next_retry_at") or 0))
        except (TypeError, ValueError):
            return 0.0


def start_ai_scheduler_heartbeat(
    config: Any,
    logger: Any,
    shutdown_event: threading.Event,
    *,
    interval_seconds: float = AI_SCHEDULER_HEARTBEAT_SECONDS,
) -> threading.Thread | None:
    if ai_scheduler_state_path(config) is None:
        return None
    update_ai_scheduler_state(
        config,
        state="starting",
        reason_code="",
        message="AI scheduler is starting.",
        current_video=None,
        next_retry_at=0.0,
        reset_errors=True,
        logger=logger,
    )

    def heartbeat_loop() -> None:
        interval = max(5.0, float(interval_seconds))
        while not shutdown_event.wait(interval):
            update_ai_scheduler_state(config, logger=logger)

    thread = threading.Thread(
        target=heartbeat_loop,
        name="ai-scheduler-heartbeat",
        daemon=True,
    )
    thread.start()
    return thread


def _state_payload(path: Path) -> dict[str, Any]:
    key = str(path)
    cached = _STATE_CACHE.get(key)
    if cached is not None and int(cached.get("worker_pid") or 0) == os.getpid():
        return cached
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    _STATE_CACHE[key] = dict(payload)
    return _STATE_CACHE[key]
