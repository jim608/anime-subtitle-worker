from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Mapping

from safe_files import atomic_write_text


STATUS = "M2_SERVER_CANARY_ACTIVE"
SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_PROCESS_LOCAL_CIRCUIT_OPEN = False


def observation_state_path(config: Any) -> Path:
    return _path_under_work(
        config,
        str(config.m2_server_canary_observation_state_path),
    )


def observation_output_dir(config: Any) -> Path:
    return _path_under_work(
        config,
        str(config.m2_server_canary_observation_output_dir),
    )


def circuit_breaker_state_path(config: Any) -> Path:
    return _path_under_work(
        config,
        str(config.m2_server_canary_circuit_breaker_state_path),
    )


def _path_under_work(config: Any, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(str(config.work_path)) / path


def circuit_breaker_active(config: Any) -> bool:
    if not bool(getattr(config, "m2_server_canary_circuit_breaker_enabled", False)):
        return False
    if _PROCESS_LOCAL_CIRCUIT_OPEN:
        return True
    path = circuit_breaker_state_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # A malformed latch must fail closed. Operators can inspect or archive
        # it after the cause is understood; restart must not silently clear it.
        return path.exists()
    return bool(payload.get("tripped")) if isinstance(payload, dict) else True


def admit_new_job(config: Any, *, logger: Any | None = None) -> bool:
    """Check the latched breaker and disk immediately before a new claim."""

    if not bool(getattr(config, "m2_server_canary_observer_enabled", False)):
        return True
    if not bool(
        getattr(config, "m2_server_canary_circuit_breaker_enabled", False)
    ):
        return True
    if circuit_breaker_active(config):
        return False
    disk_evidence = _insufficient_disk_evidence(config)
    if disk_evidence is None:
        return True
    trip_circuit_breaker(
        config,
        "insufficient_disk_space",
        evidence=disk_evidence,
        logger=logger,
    )
    return False


def record_job_result(
    config: Any,
    *,
    job_identity: str,
    outcome: Mapping[str, Any],
    logger: Any | None = None,
) -> dict[str, Any]:
    """Persist one sanitized terminal observation and emit each 20-job gate.

    The gate denominator is strictly verified completed outputs. Failed
    attempts are retained as aggregate counts but cannot advance the gate.
    Raw paths, titles, errors, and logs are deliberately excluded.
    """

    if not bool(getattr(config, "m2_server_canary_observer_enabled", False)):
        return {}
    now = time.time()
    sanitized = _sanitize_outcome(job_identity, outcome)
    with _LOCK:
        state = _load_observation_state(config, now=now)
        observed_job_keys = state["observed_job_keys"]
        if sanitized["job_key"] in observed_job_keys:
            return {
                "status": STATUS,
                "verified_since_gate": int(state["verified_since_gate"]),
                "next_gate_after_verified_completed": int(
                    state["next_gate_after_verified_completed"]
                ),
                "emitted": [],
                "duplicate_observation_ignored": True,
                "circuit_breaker_tripped": circuit_breaker_active(config),
            }
        observed_job_keys.append(sanitized["job_key"])
        state["total_attempts_observed"] += 1
        window = state["window"]
        window["attempts"] += 1
        window["last_observed_at"] = now
        if not window.get("started_at"):
            window["started_at"] = now

        if sanitized["verified_completed"]:
            state["total_verified_completed"] += 1
            state["verified_since_gate"] += 1
            window["verified_completed"] += 1
        else:
            window["failed_or_unverified"] += 1
            error_code = sanitized["error_code"] or "unclassified_failure"
            errors = window["error_codes"]
            errors[error_code] = int(errors.get(error_code) or 0) + 1

        _update_failure_streaks(state, sanitized)
        state["last_event"] = {
            key: value for key, value in sanitized.items() if not key.startswith("_")
        }
        state["updated_at"] = now

        breaker_reason = (
            _breaker_reason(sanitized, state, config)
            if bool(
                getattr(
                    config,
                    "m2_server_canary_circuit_breaker_enabled",
                    False,
                )
            )
            else ""
        )
        if breaker_reason:
            trip_circuit_breaker(
                config,
                breaker_reason,
                evidence={
                    "job_key": sanitized["job_key"],
                    "stage": sanitized["stage"],
                    "error_code": sanitized["error_code"],
                    "identical_failure_streak": state["identical_failure_streak"],
                    "oom_streak": state["oom_streak"],
                },
                logger=logger,
            )

        gate_size = max(
            1,
            int(getattr(config, "m2_server_canary_observation_gate_size", 20) or 20),
        )
        emitted: list[str] = []
        if int(state["verified_since_gate"]) >= gate_size:
            gate_index = int(state["gate_index"]) + 1
            summary = {
                "schema_version": SCHEMA_VERSION,
                "status": STATUS,
                "gate_index": gate_index,
                "gate_size": gate_size,
                "generated_at": now,
                "window": {
                    "started_at": window.get("started_at"),
                    "ended_at": now,
                    "attempts": int(window["attempts"]),
                    "verified_completed": int(window["verified_completed"]),
                    "failed_or_unverified": int(window["failed_or_unverified"]),
                    "error_codes": dict(sorted(window["error_codes"].items())),
                },
                "totals": {
                    "attempts_observed": int(state["total_attempts_observed"]),
                    "verified_completed": int(state["total_verified_completed"]),
                },
                "circuit_breaker": {
                    "enabled": bool(
                        getattr(
                            config,
                            "m2_server_canary_circuit_breaker_enabled",
                            False,
                        )
                    ),
                    "tripped": circuit_breaker_active(config),
                },
                "evidence_boundary": (
                    "aggregate machine summary only; full logs remain on the server"
                ),
            }
            output_dir = observation_output_dir(config)
            output_dir.mkdir(parents=True, exist_ok=True)
            target = output_dir / f"gate-{gate_index:06d}.json"
            if target.exists():
                trip_circuit_breaker(
                    config,
                    "observation_gate_collision",
                    evidence={"stage": "gate_publish"},
                    logger=logger,
                )
                raise FileExistsError(
                    "M2 observation gate already exists; refusing to overwrite it"
                )
            atomic_write_text(
                target,
                json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            emitted.append(target.name)
            state["gate_index"] = gate_index
            state["verified_since_gate"] = 0
            state["window"] = _empty_window()

        state["next_gate_after_verified_completed"] = (
            int(state["total_verified_completed"])
            + gate_size
            - int(state["verified_since_gate"])
        )
        _write_observation_state(config, state)
        return {
            "status": STATUS,
            "verified_since_gate": int(state["verified_since_gate"]),
            "next_gate_after_verified_completed": int(
                state["next_gate_after_verified_completed"]
            ),
            "emitted": emitted,
            "circuit_breaker_tripped": circuit_breaker_active(config),
        }


def trip_circuit_breaker(
    config: Any,
    reason_code: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Latch stop-new-work state without touching jobs, attempts, or checkpoints."""

    global _PROCESS_LOCAL_CIRCUIT_OPEN
    _PROCESS_LOCAL_CIRCUIT_OPEN = True
    now = time.time()
    reason = _safe_code(reason_code, default="unknown_safety_event", limit=120)
    safe_evidence = _sanitize_evidence(evidence or {})
    path = circuit_breaker_state_path(config)
    existing = _read_json_object(path) or {}
    reasons = list(existing.get("reasons") or []) if isinstance(existing, dict) else []
    item = {
        "reason_code": reason,
        "observed_at": now,
        "evidence": safe_evidence,
    }
    if not any(
        isinstance(previous, dict)
        and str(previous.get("reason_code") or "") == reason
        for previous in reasons
    ):
        reasons.append(item)
    try:
        tripped_at = float(existing.get("tripped_at") or now)
    except (TypeError, ValueError):
        tripped_at = now
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        "tripped": True,
        "tripped_at": tripped_at,
        "updated_at": now,
        "reasons": reasons,
        "action": "stop_claiming_new_jobs",
        "running_job_policy": "finish_without_interruption",
        "checkpoint_policy": "preserve",
    }
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    if logger is not None:
        logger.critical(
            "M2 server canary circuit breaker tripped. reason=%s action=stop_new_jobs",
            reason,
        )
    return payload


def public_status(config: Any) -> dict[str, Any]:
    """Return a bounded, path-free status payload suitable for operator output."""

    state = _read_json_object(observation_state_path(config)) or {}
    breaker = _read_json_object(circuit_breaker_state_path(config)) or {}
    return {
        "status": STATUS,
        "observer_enabled": bool(
            getattr(config, "m2_server_canary_observer_enabled", False)
        ),
        "gate_size": int(
            getattr(config, "m2_server_canary_observation_gate_size", 20) or 20
        ),
        "verified_since_gate": int(state.get("verified_since_gate") or 0),
        "next_gate_after_verified_completed": int(
            state.get("next_gate_after_verified_completed") or 20
        ),
        "circuit_breaker": {
            "enabled": bool(
                getattr(config, "m2_server_canary_circuit_breaker_enabled", False)
            ),
            "tripped": circuit_breaker_active(config),
            "reason_codes": [
                str(item.get("reason_code") or "")
                for item in breaker.get("reasons", [])
                if isinstance(item, dict)
            ],
        },
    }


def _load_observation_state(config: Any, *, now: float) -> dict[str, Any]:
    path = observation_state_path(config)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "status": STATUS,
            "created_at": now,
            "updated_at": now,
            "total_attempts_observed": 0,
            "total_verified_completed": 0,
            "verified_since_gate": 0,
            "gate_index": 0,
            "next_gate_after_verified_completed": int(
                getattr(config, "m2_server_canary_observation_gate_size", 20) or 20
            ),
            "oom_streak": 0,
            "identical_failure_signature": "",
            "identical_failure_streak": 0,
            "observed_job_keys": [],
            "window": _empty_window(),
            "last_event": None,
        }
    payload = _read_json_object(path)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        trip_circuit_breaker(
            config,
            "observation_state_invalid",
            evidence={"state_present": True},
        )
        raise ValueError("M2 server canary observation state is invalid")
    payload.setdefault("window", _empty_window())
    payload.setdefault("oom_streak", 0)
    payload.setdefault("identical_failure_signature", "")
    payload.setdefault("identical_failure_streak", 0)
    payload.setdefault("observed_job_keys", [])
    if not isinstance(payload["observed_job_keys"], list):
        trip_circuit_breaker(
            config,
            "observation_state_invalid",
            evidence={"state_present": True},
        )
        raise ValueError("M2 server canary observation job cursor is invalid")
    return payload


def _write_observation_state(config: Any, state: Mapping[str, Any]) -> None:
    atomic_write_text(
        observation_state_path(config),
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _empty_window() -> dict[str, Any]:
    return {
        "started_at": None,
        "last_observed_at": None,
        "attempts": 0,
        "verified_completed": 0,
        "failed_or_unverified": 0,
        "error_codes": {},
    }


def _sanitize_outcome(job_identity: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    stage = _safe_code(outcome.get("stage"), default="worker", limit=80)
    error_code = _safe_code(outcome.get("error_code"), default="", limit=120)
    reason_code = _safe_code(outcome.get("reason_code"), default="", limit=120)
    detail = str(outcome.get("detail") or outcome.get("error") or "")
    verified = bool(outcome.get("verified_completed"))
    failed = bool(outcome.get("failed")) or not verified
    return {
        "job_key": hashlib.sha256(str(job_identity).encode("utf-8")).hexdigest()[:16],
        "verified_completed": verified,
        "failed": failed,
        "stage": stage,
        "error_code": error_code,
        "reason_code": reason_code,
        # The raw message is used transiently for classification only. It is
        # removed before any observation state or summary is persisted.
        "_classification_detail": detail.casefold(),
    }


def _update_failure_streaks(state: dict[str, Any], outcome: Mapping[str, Any]) -> None:
    if not bool(outcome.get("failed")):
        state["oom_streak"] = 0
        state["identical_failure_signature"] = ""
        state["identical_failure_streak"] = 0
        return
    text = " ".join(
        str(outcome.get(key) or "")
        for key in ("error_code", "reason_code", "_classification_detail")
    )
    state["oom_streak"] = int(state.get("oom_streak") or 0) + 1 if _is_oom(text) else 0
    signature = f"{outcome.get('stage') or 'worker'}:{outcome.get('error_code') or outcome.get('reason_code') or 'unknown'}"
    if signature == state.get("identical_failure_signature"):
        state["identical_failure_streak"] = int(
            state.get("identical_failure_streak") or 0
        ) + 1
    else:
        state["identical_failure_signature"] = signature
        state["identical_failure_streak"] = 1


def _breaker_reason(
    outcome: Mapping[str, Any],
    state: Mapping[str, Any],
    config: Any,
) -> str:
    if not bool(outcome.get("failed")):
        return ""
    stage = str(outcome.get("stage") or "")
    error_code = str(outcome.get("error_code") or "")
    detail = str(outcome.get("_classification_detail") or "")
    text = " ".join((stage, error_code, str(outcome.get("reason_code") or ""), detail))
    if any(
        marker in text
        for marker in (
            "source_mutation",
            "media_revision_changed",
            "media_changed_during_pipeline",
            "source media changed",
            "source identity changed",
            "source checksum changed",
            "source candidate fingerprint changed",
            "source sidecar identity changed",
            "source path changed before completion",
        )
    ):
        return "source_mutation"
    if any(
        marker in text
        for marker in (
            "duplicate_publish",
            "completed destination exists without a matching receipt",
            "completed destination belongs to different delivery evidence",
            "completed-path collision",
            "completed destination appeared during",
            "completeddeliverycollisionerror",
        )
    ):
        return "duplicate_publish"
    if error_code == "delivery_evidence_missing" or (
        "worker returned success without" in text
        and "verified" in text
    ):
        return "incorrect_completion"
    if stage in {"completed_delivery", "delivery_verification", "mux", "publish"} and any(
        marker in text
        for marker in (
            "output_parse_failure",
            "output parse",
            "returned invalid json",
            "publication manifest is unreadable",
            "failed revalidation",
            "ffprobe returned",
        )
    ):
        return "output_parse_failure"
    oom_threshold = max(
        1,
        int(
            getattr(
                config,
                "m2_server_canary_repeated_oom_threshold",
                3,
            )
            or 3
        ),
    )
    if int(state.get("oom_streak") or 0) >= oom_threshold:
        return "repeated_oom"
    identical_threshold = max(
        1,
        int(
            getattr(
                config,
                "m2_server_canary_identical_failure_threshold",
                3,
            )
            or 3
        ),
    )
    if int(state.get("identical_failure_streak") or 0) >= identical_threshold:
        return "repeated_identical_stage_failure"
    return ""


def _is_oom(text: str) -> bool:
    normalized = str(text or "").casefold()
    return any(
        marker in normalized
        for marker in (
            "transient_oom",
            "out of memory",
            "cuda oom",
            "returncode=137",
            "sigkill/oom",
        )
    )


def _insufficient_disk_evidence(config: Any) -> dict[str, Any] | None:
    minimum_gb = float(getattr(config, "disk_min_free_gb", 2.0) or 0.0)
    minimum_bytes = int(max(0.0, minimum_gb) * 1024 * 1024 * 1024)
    if minimum_bytes <= 0:
        return None
    candidates: list[tuple[str, Path]] = [
        ("input", Path(str(config.input_path))),
        ("work", Path(str(config.work_path))),
        ("log", Path(str(config.log_path))),
    ]
    if bool(getattr(config, "completed_delivery_enabled", False)):
        candidates.append(
            ("completed_delivery", Path(str(config.completed_delivery_path)))
        )
    seen: set[str] = set()
    for label, path in candidates:
        try:
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            free = int(shutil.disk_usage(path).free)
        except OSError:
            return {
                "volume_role": label,
                "free_bytes": -1,
                "minimum_free_bytes": minimum_bytes,
            }
        if free < minimum_bytes:
            return {
                "volume_role": label,
                "free_bytes": free,
                "minimum_free_bytes": minimum_bytes,
            }
    return None


def _sanitize_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "job_key",
        "stage",
        "error_code",
        "identical_failure_streak",
        "oom_streak",
        "volume_role",
        "free_bytes",
        "minimum_free_bytes",
        "state_present",
    }
    sanitized: dict[str, Any] = {}
    for key, value in evidence.items():
        normalized_key = str(key)
        if normalized_key not in allowed or not isinstance(
            value,
            (str, int, float, bool, type(None)),
        ):
            continue
        sanitized[normalized_key] = (
            _safe_code(value, default="", limit=120)
            if isinstance(value, str)
            else value
        )
    return sanitized


def _safe_code(value: Any, *, default: str, limit: int) -> str:
    normalized = str(value or default).strip().casefold()
    return re.sub(r"[^a-z0-9_.-]+", "_", normalized).strip("_.-")[:limit]


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print bounded M2 server-canary observation status."
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    from config import load_config

    config = load_config(args.config)
    print(json.dumps(public_status(config), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
