"""Append-only structured M1 pipeline event log."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any


_WRITE_LOCK = threading.Lock()


def append_pipeline_event(
    log_root: str | Path,
    event: str,
    *,
    job_id: str,
    state: str,
    reason_code: str,
    media_revision: str = "",
    stage: str = "",
    attempt: int | None = None,
    confidence: float = 1.0,
    evidence: dict[str, Any] | None = None,
) -> Path:
    """Durably append one JSON object without making the normal text log an API."""

    root = Path(log_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "pipeline-events.jsonl"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": str(event),
        "job_id": str(job_id),
        "media_revision": str(media_revision),
        "state": str(state),
        "stage": str(stage),
        "reason_code": str(reason_code),
        "confidence": max(0.0, min(1.0, float(confidence))),
        "evidence": dict(evidence or {}),
    }
    if attempt is not None:
        payload["attempt"] = int(attempt)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        with target.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return target

