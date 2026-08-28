from __future__ import annotations

from pathlib import Path
import hashlib
import json
import time
from typing import Any


MARKER_DIR_NAME = "ai_failures"


def mark_ai_failure(config: Any, video_path: str | Path, stage: str, error: BaseException | str) -> Path:
    marker = ai_failure_marker_path(config, video_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "video": str(Path(video_path)),
        "stage": stage,
        "error": str(error),
        "failed_at": time.time(),
        "ai_config_signature": _ai_config_signature(config),
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return marker


def clear_ai_failure_marker(config: Any, video_path: str | Path) -> None:
    try:
        ai_failure_marker_path(config, video_path).unlink(missing_ok=True)
    except OSError:
        pass


def recent_ai_failure(config: Any, video_path: str | Path, now: float | None = None) -> tuple[bool, int]:
    cooldown = int(getattr(config, "auto_ai_failure_cooldown_seconds", 0) or 0)
    if cooldown <= 0:
        return False, 0

    marker = ai_failure_marker_path(config, video_path)
    try:
        marker_mtime = marker.stat().st_mtime
    except FileNotFoundError:
        return False, 0

    payload = _load_marker_payload(marker)
    if payload.get("ai_config_signature") != _ai_config_signature(config):
        try:
            marker.unlink(missing_ok=True)
        except OSError:
            pass
        return False, 0

    current_time = time.time() if now is None else now
    age = max(0, current_time - marker_mtime)
    if age < cooldown:
        return True, int(cooldown - age)

    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    return False, 0


def ai_failure_marker_path(config: Any, video_path: str | Path) -> Path:
    digest = hashlib.sha1(str(Path(video_path).resolve()).encode("utf-8")).hexdigest()
    return Path(config.work_path) / MARKER_DIR_NAME / f"{digest}.json"


def _load_marker_payload(marker: Path) -> dict[str, Any]:
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _ai_config_signature(config: Any) -> str:
    payload = {
        "transcription_backend": getattr(config, "transcription_backend", None),
        "whisper_model": getattr(config, "whisper_model", None),
        "language_detect_model": getattr(config, "language_detect_model", None),
        "japanese_transcription_model": getattr(config, "japanese_transcription_model", None),
        "japanese_transcription_fallback_backend": getattr(
            config,
            "japanese_transcription_fallback_backend",
            None,
        ),
        "japanese_transcription_fallback_model": getattr(
            config,
            "japanese_transcription_fallback_model",
            None,
        ),
        "japanese_transcription_fallback_compute_type": getattr(
            config,
            "japanese_transcription_fallback_compute_type",
            None,
        ),
        "non_japanese_transcription_model": getattr(config, "non_japanese_transcription_model", None),
        "whisper_device": getattr(config, "whisper_device", None),
        "whisper_compute_type": getattr(config, "whisper_compute_type", None),
        "whisper_language": getattr(config, "whisper_language", None),
        "whisper_task": getattr(config, "whisper_task", None),
        "language_gate_enabled": getattr(config, "language_gate_enabled", None),
        "allowed_source_languages": list(getattr(config, "allowed_source_languages", []) or []),
        "skip_non_allowed_language": getattr(config, "skip_non_allowed_language", None),
        "transcribe_non_allowed_languages": getattr(config, "transcribe_non_allowed_languages", None),
        "language_detect_min_probability": getattr(config, "language_detect_min_probability", None),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()
