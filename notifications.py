from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any

import requests

from config import AppConfig


_LOCK = threading.Lock()


def notify_event(
    config: AppConfig,
    event: str,
    title: str,
    message: str,
    *,
    severity: str = "warning",
    key: str = "",
    details: dict[str, Any] | None = None,
) -> bool:
    url = str(getattr(config, "notification_webhook_url", "") or "").strip()
    enabled_events = {
        str(item).strip().casefold()
        for item in getattr(config, "notification_events", [])
        if str(item).strip()
    }
    event_key = str(event or "").casefold()
    if not url or (enabled_events and event_key not in enabled_events):
        return False

    now = time.time()
    fingerprint = hashlib.sha256(
        f"{event_key}\0{key}\0{message}".encode("utf-8", errors="replace")
    ).hexdigest()
    state_path = _state_path(config)
    interval = max(0, int(getattr(config, "notification_min_interval_seconds", 300) or 0))
    with _LOCK:
        state = _read_state(state_path)
        if now - float(state.get(fingerprint) or 0) < interval:
            return False
        payload = {
            "event": event_key,
            "title": str(title or event),
            "message": str(message or "")[:4000],
            "severity": str(severity or "warning"),
            "key": str(key or ""),
            "created_at": now,
            "details": details or {},
        }
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
        except requests.RequestException:
            return False
        state[fingerprint] = now
        cutoff = now - max(86400, interval * 4)
        state = {item_key: value for item_key, value in state.items() if float(value or 0) >= cutoff}
        _write_state(state_path, state)
        return True


def _state_path(config: AppConfig) -> Path:
    configured = Path(str(getattr(config, "notification_state_path", "notification_state.json")))
    return configured if configured.is_absolute() else Path(config.work_path) / configured


def _read_state(path: Path) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp.replace(path)
