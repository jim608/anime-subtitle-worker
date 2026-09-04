from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Mapping

from m2_strict_observation import (
    STRICT_EVIDENCE_KEYS,
    empty_summary_counters,
    normalize_processing_strategy,
    qualify_strict_output,
    update_summary_counters,
    validate_summary_counters,
)
from safe_files import atomic_write_text


STATUS = "M2_GUARDRAILS_ARMED"
SCHEMA_VERSION = 2
OBSERVATION_CONTRACT = "m2-production-observation-v2"
_LOCK = threading.RLock()
_PROCESS_LOCAL_CIRCUIT_OPEN = False


class ObservationStateError(ValueError):
    """The durable observation state cannot safely admit or count work."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


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


def _m2_guardrail_config_exists(config: Any) -> bool:
    if bool(getattr(config, "m2_server_canary_observer_enabled", False)) or bool(
        getattr(config, "m2_server_canary_circuit_breaker_enabled", False)
    ):
        return True
    configured = str(getattr(config, "m2_guardrail_runtime_state_path", "") or "")
    if not configured:
        return False
    try:
        return _path_under_work(config, configured).exists()
    except (OSError, TypeError, ValueError):
        return True


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
    """Admit only an exact armed runtime immediately before a new claim."""

    if not _m2_guardrail_config_exists(config):
        return True
    if circuit_breaker_active(config):
        return False
    try:
        from m2_guardrail_runtime import runtime_guardrail_status

        runtime = runtime_guardrail_status(config)
    except Exception as exc:  # noqa: BLE001 - a missing runtime contract fails closed.
        if logger is not None:
            logger.exception("M2 runtime guardrail status failed: %s", exc)
        return False
    if str(runtime.get("status") or "DEGRADED") != "ARMED":
        return False
    runtime_state = runtime.get("state")
    if not isinstance(runtime_state, Mapping):
        return False
    try:
        with _LOCK:
            state = _load_armed_observation_state(config, runtime_state)
            if state.get("pending_gate") is not None:
                _recover_pending_gate(config, state, logger=logger)
                _validate_armed_observation_state(config, state, runtime_state)
    except ObservationStateError as exc:
        trip_circuit_breaker(
            config,
            "observation_state_degraded",
            evidence={"stage": "admission", "error_code": exc.reason_code},
            logger=logger,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - persistence faults fail closed.
        if logger is not None:
            logger.exception("M2 observation admission validation failed: %s", exc)
        trip_circuit_breaker(
            config,
            "observation_state_degraded",
            evidence={"stage": "admission", "error_code": "state_recovery_failed"},
            logger=logger,
        )
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


def initialize_observation_gate(
    config: Any,
    runtime_state: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Initialize the strict 0/20 state before the runtime manifest is published.

    Any prior canary observer state is retained as a hashed history file. The
    new state is written first so a crash can only leave admission fail-closed.
    """

    timestamp = time.time() if now is None else float(now)
    if str(runtime_state.get("status") or "") != "ARMED":
        raise ValueError("M2 observation gate requires an ARMED runtime candidate")
    baseline_version = str(runtime_state.get("gate_baseline_version") or "")
    gate_start_epoch = float(runtime_state.get("gate_start_epoch") or 0)
    gate = runtime_state.get("gate")
    if (
        not baseline_version
        or gate_start_epoch <= 0
        or not isinstance(gate, Mapping)
        or int(gate.get("target") or 0) != 20
    ):
        raise ValueError("M2 runtime gate baseline is incomplete")

    with _LOCK:
        target = observation_state_path(config)
        existing = _read_json_object(target)
        if (
            isinstance(existing, dict)
            and existing.get("schema_version") == SCHEMA_VERSION
            and existing.get("gate_baseline_version") == baseline_version
        ):
            _validate_armed_observation_state(config, existing, runtime_state)
            if existing.get("pending_gate") is not None:
                _recover_pending_gate(config, existing)
                _validate_armed_observation_state(config, existing, runtime_state)
            return existing
        if target.is_file():
            raw = target.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()[:16]
            history_dir = observation_output_dir(config) / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            history = history_dir / f"pre-arm-{digest}.json"
            if not history.exists():
                atomic_write_text(history, raw.decode("utf-8", errors="replace"))
        state = _new_observation_state(
            config,
            now=timestamp,
            gate_baseline_version=baseline_version,
            gate_start_epoch=gate_start_epoch,
            runtime_baseline=runtime_state.get("baseline"),
        )
        _write_observation_state(config, state)
        return state


def record_job_claim(
    config: Any,
    *,
    job_identity: str,
    claimed_at: float,
    processing_strategy: str = "",
    logger: Any | None = None,
) -> dict[str, Any]:
    """Bind a durable queue attempt to the exact armed gate baseline."""

    if not _m2_guardrail_config_exists(config):
        return {}
    from m2_guardrail_runtime import gate_claim_eligible, runtime_guardrail_status

    runtime = runtime_guardrail_status(config)
    runtime_state = runtime.get("state")
    if str(runtime.get("status") or "") != "ARMED" or not isinstance(
        runtime_state, Mapping
    ):
        trip_circuit_breaker(
            config,
            "runtime_contract_degraded",
            evidence={"stage": "queue_claim"},
            logger=logger,
        )
        return {
            "status": str(runtime.get("status") or "DEGRADED"),
            "recorded": False,
        }
    baseline_version = str(runtime_state.get("gate_baseline_version") or "")
    eligible, reason = gate_claim_eligible(
        runtime_state,
        job_identity=job_identity,
        claimed_at=float(claimed_at),
        gate_baseline_version=baseline_version,
    )
    job_key = _job_key(job_identity)
    strategy = normalize_processing_strategy(processing_strategy)
    now = time.time()
    with _LOCK:
        try:
            state = _load_armed_observation_state(config, runtime_state)
            if state.get("pending_gate") is not None:
                _recover_pending_gate(config, state, logger=logger)
                _validate_armed_observation_state(config, state, runtime_state)
        except ObservationStateError as exc:
            trip_circuit_breaker(
                config,
                "observation_state_degraded",
                evidence={"stage": "queue_claim", "error_code": exc.reason_code},
                logger=logger,
            )
            return {"status": "DEGRADED", "recorded": False}
        claims = state["claims"]
        if job_key in claims:
            return {
                "status": STATUS,
                "recorded": False,
                "duplicate_claim_ignored": True,
                "gate_eligible": bool(claims[job_key].get("gate_eligible")),
            }
        claims[job_key] = {
            "claimed_at": float(claimed_at),
            "gate_eligible": bool(eligible),
            "eligibility_reason": _safe_code(reason, default="unknown", limit=120),
            "gate_baseline_version": baseline_version,
            "processing_strategy": strategy,
            "terminal_observed": False,
        }
        if eligible:
            event = _strict_outcome(
                terminal_status="QUEUED",
                processing_strategy=strategy,
                event_kind="claim",
                claimed_after_gate_start=True,
            )
            state["totals"], _ = update_summary_counters(
                state["totals"], outcome=event
            )
            state["window"]["counters"], _ = update_summary_counters(
                state["window"]["counters"], outcome=event
            )
        else:
            state["excluded_claims"] = int(state.get("excluded_claims") or 0) + 1
        state["updated_at"] = now
        _write_observation_state(config, state)
        return {
            "status": STATUS,
            "recorded": True,
            "gate_eligible": bool(eligible),
            "eligibility_reason": reason,
        }


def record_job_result(
    config: Any,
    *,
    job_identity: str,
    outcome: Mapping[str, Any],
    strict_evidence: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Persist one bounded terminal result and emit only strict 20-job gates."""

    if not _m2_guardrail_config_exists(config):
        return {}
    now = time.time()
    sanitized = _sanitize_outcome(job_identity, outcome)
    with _LOCK:
        try:
            from m2_guardrail_runtime import load_runtime_state

            runtime_state = load_runtime_state(config)
            if not isinstance(runtime_state, Mapping):
                raise ObservationStateError("runtime_state_missing")
            state = _load_armed_observation_state(config, runtime_state)
            if state.get("pending_gate") is not None:
                _recover_pending_gate(config, state, logger=logger)
                _validate_armed_observation_state(config, state, runtime_state)
        except ObservationStateError as exc:
            trip_circuit_breaker(
                config,
                "observation_state_degraded",
                evidence={"stage": "terminal_observation", "error_code": exc.reason_code},
                logger=logger,
            )
            return {
                "status": "DEGRADED",
                "recorded": False,
                "circuit_breaker_tripped": True,
                "reason_code": exc.reason_code,
            }
        observed_job_keys = state["observed_job_keys"]
        if sanitized["job_key"] in observed_job_keys:
            return {
                "status": STATUS,
                "verified_since_gate": _gate_progress(state),
                "next_gate_after_verified_completed": _next_gate(state, config),
                "emitted": [],
                "duplicate_observation_ignored": True,
                "circuit_breaker_tripped": circuit_breaker_active(config),
            }
        observed_job_keys.append(sanitized["job_key"])
        state["total_attempts_observed"] += 1
        window = state["window"]
        window["last_observed_at"] = now
        claim = state["claims"].get(sanitized["job_key"])
        gate_eligible = bool(
            isinstance(claim, dict) and claim.get("gate_eligible") is True
        )
        exclusion_reason = (
            str(claim.get("eligibility_reason") or "")
            if isinstance(claim, dict)
            else _pre_gate_exclusion_reason(config, sanitized["job_key"])
        )

        _update_failure_streaks(state, sanitized)
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
        strict_outcome = _strict_outcome_from_sanitized(
            sanitized,
            claimed_after_gate_start=gate_eligible,
        )
        qualification = qualify_strict_output(
            strict_outcome,
            {} if strict_evidence is None else strict_evidence,
        )
        if (
            gate_eligible
            and sanitized["terminal_status"] == "COMPLETED"
            and qualification.get("qualified") is not True
        ):
            strict_outcome["incorrect_completion"] = True
            breaker_reason = breaker_reason or "incorrect_completion"
            qualification = qualify_strict_output(
                strict_outcome,
                {} if strict_evidence is None else strict_evidence,
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
                    "strict_failure_count": len(
                        qualification.get("failed_evidence") or []
                    ),
                    "strict_failure_code": str(
                        (qualification.get("reason_codes") or [""])[0]
                    ),
                },
                logger=logger,
            )
            strict_outcome["breaker_tripped"] = True

        if gate_eligible:
            window["terminal_attempts"] += 1
            if not window.get("started_at"):
                window["started_at"] = float(claim.get("claimed_at") or now)
            _replace_unreported_strategy(state, claim, sanitized["processing_strategy"])
            state["totals"], qualification = update_summary_counters(
                state["totals"],
                outcome=strict_outcome,
                evidence={} if strict_evidence is None else strict_evidence,
            )
            window["counters"], _ = update_summary_counters(
                window["counters"],
                outcome=strict_outcome,
                evidence={} if strict_evidence is None else strict_evidence,
            )
            if sanitized["terminal_status"] != "COMPLETED":
                error_code = sanitized["error_code"] or "unclassified_failure"
                errors = window["error_codes"]
                errors[error_code] = int(errors.get(error_code) or 0) + 1
        else:
            state["excluded_terminal_results"] = int(
                state.get("excluded_terminal_results") or 0
            ) + 1
            reasons = state["excluded_reason_counts"]
            safe_reason = _safe_code(
                exclusion_reason or "claim_not_bound_to_gate",
                default="claim_not_bound_to_gate",
                limit=120,
            )
            reasons[safe_reason] = int(reasons.get(safe_reason) or 0) + 1

        if isinstance(claim, dict):
            claim["terminal_observed"] = True
            claim["processing_strategy"] = sanitized["processing_strategy"]
        state["last_event"] = {
            "job_key": sanitized["job_key"],
            "terminal_status": sanitized["terminal_status"],
            "processing_strategy": sanitized["processing_strategy"],
            "stage": sanitized["stage"],
            "error_code": sanitized["error_code"],
            "reason_code": sanitized["reason_code"],
            "gate_eligible": gate_eligible,
            "gate_qualified": bool(qualification.get("qualified")),
            "qualification_reason_codes": list(
                qualification.get("reason_codes") or []
            ),
        }
        state["updated_at"] = now

        gate_size = max(
            1,
            int(getattr(config, "m2_server_canary_observation_gate_size", 20) or 20),
        )
        emitted = _emit_gate_if_ready(
            config,
            state,
            gate_size=gate_size,
            now=now,
            logger=logger,
        )
        _write_observation_state(config, state)
        return {
            "status": STATUS,
            "verified_since_gate": _gate_progress(state),
            "next_gate_after_verified_completed": _next_gate(state, config),
            "emitted": emitted,
            "circuit_breaker_tripped": circuit_breaker_active(config),
            "gate_eligible": gate_eligible,
            "strictly_qualified": bool(qualification.get("qualified")),
        }


def has_durable_gate_claim_binding(config: Any, *, job_identity: str) -> bool:
    """Return whether an attempt has an intact, gate-eligible durable binding.

    Missing or malformed runtime/observer state raises ``ObservationStateError``
    so a completion caller cannot mistake unavailable evidence for a negative
    lookup and proceed fail-open.
    """

    if not str(job_identity or "").strip():
        raise ObservationStateError("job_identity_invalid")
    from m2_guardrail_runtime import load_runtime_state

    with _LOCK:
        runtime_state = load_runtime_state(config)
        if not isinstance(runtime_state, Mapping):
            raise ObservationStateError("runtime_state_missing")
        state = _load_armed_observation_state(config, runtime_state)
        claim = state["claims"].get(_job_key(job_identity))
        return bool(
            isinstance(claim, Mapping)
            and claim.get("gate_eligible") is True
            and str(claim.get("gate_baseline_version") or "")
            == str(runtime_state.get("gate_baseline_version") or "")
        )


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
        "status": "TRIPPED",
        "milestone_status": STATUS,
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
            "M2 guardrail circuit breaker tripped. reason=%s action=stop_new_jobs",
            reason,
        )
    return payload


def public_status(config: Any) -> dict[str, Any]:
    """Return a bounded, path-free status payload suitable for operator output."""

    state = _read_json_object(observation_state_path(config)) or {}
    breaker = _read_json_object(circuit_breaker_state_path(config)) or {}
    try:
        from m2_guardrail_runtime import runtime_guardrail_status

        runtime = runtime_guardrail_status(config)
    except Exception:  # noqa: BLE001 - public status must report degraded, not raise.
        runtime = {"status": "DEGRADED", "reason_code": "runtime_status_unavailable"}
    runtime_status = str(runtime.get("status") or "DEGRADED")
    return {
        "milestone_status": (
            STATUS if runtime_status in {"ARMED", "TRIPPED"} else "M2_SERVER_CANARY_ACTIVE"
        ),
        "status": runtime_status,
        "reason_code": str(runtime.get("reason_code") or ""),
        "observer_enabled": bool(
            getattr(config, "m2_server_canary_observer_enabled", False)
        ),
        "gate_size": int(
            getattr(config, "m2_server_canary_observation_gate_size", 20) or 20
        ),
        "gate_progress": _gate_progress(state),
        "next_gate_after_verified_completed": _next_gate(state, config),
        "gate_baseline_version": str(state.get("gate_baseline_version") or ""),
        "counters": dict(state.get("totals") or empty_summary_counters()),
        "circuit_breaker": {
            "enabled": bool(
                getattr(config, "m2_server_canary_circuit_breaker_enabled", False)
            ),
            "status": "TRIPPED" if circuit_breaker_active(config) else runtime_status,
            "tripped": circuit_breaker_active(config),
            "reason_codes": [
                str(item.get("reason_code") or "")
                for item in breaker.get("reasons", [])
                if isinstance(item, dict)
            ],
        },
    }


def _load_armed_observation_state(
    config: Any,
    runtime_state: Mapping[str, Any],
) -> dict[str, Any]:
    path = observation_state_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ObservationStateError("observation_state_missing") from exc
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ObservationStateError("observation_state_unreadable") from exc
    if not isinstance(payload, dict):
        raise ObservationStateError("observation_state_not_object")
    _validate_armed_observation_state(config, payload, runtime_state)
    return payload


def _validate_armed_observation_state(
    config: Any,
    state: Mapping[str, Any],
    runtime_state: Mapping[str, Any],
) -> None:
    if (
        state.get("contract") != OBSERVATION_CONTRACT
        or state.get("schema_version") != SCHEMA_VERSION
        or state.get("status") != STATUS
    ):
        raise ObservationStateError("observation_contract_mismatch")
    if runtime_state.get("status") != "ARMED":
        raise ObservationStateError("runtime_state_not_armed")
    runtime_baseline = runtime_state.get("baseline")
    baseline_version = str(runtime_state.get("gate_baseline_version") or "")
    if not isinstance(runtime_baseline, Mapping) or not baseline_version:
        raise ObservationStateError("runtime_baseline_incomplete")
    if (
        str(state.get("gate_baseline_version") or "") != baseline_version
        or not isinstance(state.get("runtime_baseline"), Mapping)
        or dict(state.get("runtime_baseline") or {}) != dict(runtime_baseline)
    ):
        raise ObservationStateError("observation_baseline_mismatch")
    try:
        gate_start = float(state.get("gate_start_epoch"))
        runtime_gate_start = float(runtime_state.get("gate_start_epoch"))
    except (TypeError, ValueError) as exc:
        raise ObservationStateError("observation_gate_start_invalid") from exc
    if (
        not math.isfinite(gate_start)
        or not math.isfinite(runtime_gate_start)
        or gate_start <= 0
        or not math.isclose(gate_start, runtime_gate_start, rel_tol=0.0, abs_tol=1e-6)
    ):
        raise ObservationStateError("observation_gate_start_mismatch")

    gate = runtime_state.get("gate")
    gate_size = int(
        getattr(config, "m2_server_canary_observation_gate_size", 20) or 20
    )
    if (
        gate_size != 20
        or not isinstance(gate, Mapping)
        or int(gate.get("target") or 0) != gate_size
    ):
        raise ObservationStateError("observation_gate_target_mismatch")
    for key in (
        "gate_index",
        "total_attempts_observed",
        "excluded_claims",
        "excluded_terminal_results",
        "oom_streak",
        "identical_failure_streak",
    ):
        value = state.get(key)
        if type(value) is not int or value < 0:
            raise ObservationStateError("observation_counter_shape_invalid")
    _validate_counter_shape(state.get("totals"))
    window = state.get("window")
    if not isinstance(window, Mapping):
        raise ObservationStateError("observation_window_invalid")
    terminal_attempts = window.get("terminal_attempts")
    if type(terminal_attempts) is not int or terminal_attempts < 0:
        raise ObservationStateError("observation_window_invalid")
    _validate_counter_shape(window.get("counters"))
    if not isinstance(window.get("error_codes"), Mapping):
        raise ObservationStateError("observation_window_invalid")
    for value in window.get("error_codes", {}).values():
        if type(value) is not int or value < 0:
            raise ObservationStateError("observation_window_invalid")
    for timestamp_key in ("started_at", "last_observed_at"):
        value = window.get(timestamp_key)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ObservationStateError("observation_window_invalid") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ObservationStateError("observation_window_invalid")

    claims = state.get("claims")
    observed = state.get("observed_job_keys")
    excluded_reasons = state.get("excluded_reason_counts")
    if (
        not isinstance(claims, Mapping)
        or not isinstance(observed, list)
        or len(observed) != len(set(observed))
        or not isinstance(excluded_reasons, Mapping)
    ):
        raise ObservationStateError("observation_cursor_invalid")
    for value in excluded_reasons.values():
        if type(value) is not int or value < 0:
            raise ObservationStateError("observation_counter_shape_invalid")
    for job_key, claim in claims.items():
        if (
            not re.fullmatch(r"[0-9a-f]{16}", str(job_key))
            or not isinstance(claim, Mapping)
            or type(claim.get("gate_eligible")) is not bool
            or type(claim.get("terminal_observed")) is not bool
            or str(claim.get("gate_baseline_version") or "") != baseline_version
        ):
            raise ObservationStateError("observation_claim_invalid")
        try:
            claimed_at = float(claim.get("claimed_at"))
        except (TypeError, ValueError) as exc:
            raise ObservationStateError("observation_claim_invalid") from exc
        if not math.isfinite(claimed_at) or claimed_at <= 0:
            raise ObservationStateError("observation_claim_invalid")
    if any(not re.fullmatch(r"[0-9a-f]{16}", str(item)) for item in observed):
        raise ObservationStateError("observation_cursor_invalid")

    if "pending_gate" not in state:
        raise ObservationStateError("observation_pending_journal_missing")
    pending = state.get("pending_gate")
    progress = _gate_progress(state)
    if pending is None:
        if progress >= gate_size:
            raise ObservationStateError("observation_gate_publish_incomplete")
    else:
        _validate_pending_gate(config, state, pending)
        if progress < gate_size:
            raise ObservationStateError("observation_pending_gate_premature")


def _validate_counter_shape(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ObservationStateError("observation_counter_shape_invalid")
    expected = empty_summary_counters()
    if set(value) != set(expected):
        raise ObservationStateError("observation_counter_shape_invalid")
    strategies = value.get("processing_strategy_counts")
    if not isinstance(strategies, Mapping) or set(strategies) != set(
        expected["processing_strategy_counts"]
    ):
        raise ObservationStateError("observation_counter_shape_invalid")
    try:
        validate_summary_counters(value)
    except (TypeError, ValueError) as exc:
        raise ObservationStateError("observation_counter_shape_invalid") from exc


def _load_observation_state(config: Any, *, now: float) -> dict[str, Any]:
    path = observation_state_path(config)
    if not path.exists():
        return _new_observation_state(config, now=now)
    payload = _read_json_object(path)
    if not isinstance(payload, dict):
        raise ValueError("M2 guardrail observation state is unreadable")
    if payload.get("schema_version") != SCHEMA_VERSION:
        # The runtime arm step archives and replaces prior canary state. Before
        # arming, retain only failure classification state so legacy evidence
        # can never advance the formal gate.
        return _new_observation_state(config, now=now)
    payload.setdefault("totals", empty_summary_counters())
    payload.setdefault("claims", {})
    payload.setdefault("excluded_claims", 0)
    payload.setdefault("excluded_terminal_results", 0)
    payload.setdefault("excluded_reason_counts", {})
    payload.setdefault("window", _empty_window())
    payload["window"].setdefault("counters", empty_summary_counters())
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
    if not isinstance(payload["claims"], dict):
        trip_circuit_breaker(
            config,
            "observation_state_invalid",
            evidence={"state_present": True},
        )
        raise ValueError("M2 guardrail observation claim map is invalid")
    return payload


def _write_observation_state(config: Any, state: Mapping[str, Any]) -> None:
    atomic_write_text(
        observation_state_path(config),
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _new_observation_state(
    config: Any,
    *,
    now: float,
    gate_baseline_version: str = "",
    gate_start_epoch: float = 0.0,
    runtime_baseline: Any = None,
) -> dict[str, Any]:
    baseline = dict(runtime_baseline) if isinstance(runtime_baseline, Mapping) else {}
    return {
        "contract": OBSERVATION_CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "status": STATUS if gate_baseline_version else "M2_SERVER_CANARY_ACTIVE",
        "created_at": now,
        "updated_at": now,
        "gate_start_epoch": float(gate_start_epoch or 0),
        "gate_baseline_version": str(gate_baseline_version or ""),
        "runtime_baseline": baseline,
        "gate_index": 0,
        "total_attempts_observed": 0,
        "totals": empty_summary_counters(),
        "oom_streak": 0,
        "identical_failure_signature": "",
        "identical_failure_streak": 0,
        "claims": {},
        "observed_job_keys": [],
        "excluded_claims": 0,
        "excluded_terminal_results": 0,
        "excluded_reason_counts": {},
        "pending_gate": None,
        "window": _empty_window(),
        "last_event": None,
    }


def _empty_window() -> dict[str, Any]:
    return {
        "started_at": None,
        "last_observed_at": None,
        "terminal_attempts": 0,
        "counters": empty_summary_counters(),
        "error_codes": {},
    }


def _gate_progress(state: Mapping[str, Any]) -> int:
    window = state.get("window")
    counters = window.get("counters") if isinstance(window, Mapping) else None
    return int(counters.get("gate_progress") or 0) if isinstance(counters, Mapping) else 0


def _next_gate(state: Mapping[str, Any], config: Any) -> int:
    gate_size = max(
        1,
        int(getattr(config, "m2_server_canary_observation_gate_size", 20) or 20),
    )
    return (int(state.get("gate_index") or 0) + 1) * gate_size


def _job_key(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _pre_gate_exclusion_reason(config: Any, job_key: str) -> str:
    try:
        from m2_guardrail_runtime import load_runtime_state

        runtime = load_runtime_state(config)
    except Exception:  # noqa: BLE001 - absence remains excluded, never counted.
        runtime = None
    pre_gate = runtime.get("pre_gate_running") if isinstance(runtime, Mapping) else None
    if isinstance(pre_gate, Mapping) and job_key in set(pre_gate.get("attempt_keys") or []):
        return "running_before_gate_start"
    return "claim_not_bound_to_gate"


def _strict_outcome(
    *,
    terminal_status: str,
    processing_strategy: str,
    event_kind: str,
    claimed_after_gate_start: bool,
    **flags: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_kind": event_kind,
        "terminal_status": terminal_status,
        "processing_strategy": processing_strategy,
        "claimed_after_gate_start": bool(claimed_after_gate_start),
    }
    payload.update({key: bool(value) for key, value in flags.items()})
    return payload


def _strict_outcome_from_sanitized(
    outcome: Mapping[str, Any],
    *,
    claimed_after_gate_start: bool,
) -> dict[str, Any]:
    text = " ".join(
        str(outcome.get(key) or "")
        for key in ("stage", "error_code", "reason_code", "_classification_detail")
    ).casefold()
    return _strict_outcome(
        terminal_status=str(outcome.get("terminal_status") or "UNKNOWN"),
        processing_strategy=str(outcome.get("processing_strategy") or "UNREPORTED"),
        event_kind="terminal",
        claimed_after_gate_start=claimed_after_gate_start,
        quarantined=bool(outcome.get("quarantined")),
        hallucination_blocked=(
            bool(outcome.get("hallucination_blocked"))
            or "hallucination" in text
            or "prompt_echo" in text
        ),
        output_parse_failure=(
            bool(outcome.get("output_parse_failure"))
            or "output_parse" in text
            or "ffprobe" in text
        ),
        source_mutation_incident=(
            bool(outcome.get("source_mutation_incident"))
            or "source_mutation" in text
            or "source checksum changed" in text
            or "source identity changed" in text
        ),
        duplicate_job=bool(outcome.get("duplicate_job")),
        duplicate_publish=(
            bool(outcome.get("duplicate_publish")) or "duplicate_publish" in text
        ),
        incorrect_completion=bool(outcome.get("incorrect_completion")),
        breaker_tripped=False,
        checkpoint_resumed=bool(outcome.get("checkpoint_resumed")),
        oom_event=bool(outcome.get("oom_event")) or _is_oom(text),
        unresolved_retry=bool(outcome.get("unresolved_retry")),
        unresolved_fallback=bool(outcome.get("unresolved_fallback")),
    )


def _replace_unreported_strategy(
    state: dict[str, Any],
    claim: Mapping[str, Any] | None,
    strategy: str,
) -> None:
    if not isinstance(claim, Mapping):
        return
    previous = str(claim.get("processing_strategy") or "UNREPORTED")
    current = normalize_processing_strategy(strategy)
    if previous != "UNREPORTED" or current == "UNREPORTED":
        return
    for counters in (state.get("totals"), state.get("window", {}).get("counters")):
        if not isinstance(counters, dict):
            continue
        strategies = counters.get("processing_strategy_counts")
        if not isinstance(strategies, dict):
            continue
        if int(strategies.get("UNREPORTED") or 0) > 0:
            strategies["UNREPORTED"] = int(strategies["UNREPORTED"]) - 1
            strategies[current] = int(strategies.get(current) or 0) + 1


def _breaker_summary(config: Any) -> dict[str, Any]:
    payload = _read_json_object(circuit_breaker_state_path(config)) or {}
    reasons = [
        str(item.get("reason_code") or "")
        for item in payload.get("reasons", [])
        if isinstance(item, dict) and item.get("reason_code")
    ]
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = int(counts.get(reason) or 0) + 1
    active = circuit_breaker_active(config)
    return {
        "status": "TRIPPED" if active else "ARMED",
        "tripped": active,
        "trip_count": len(reasons),
        "reason_counts": dict(sorted(counts.items())),
        "action": "stop_claiming_new_jobs" if active else "allow_new_claims",
        "running_job_policy": "finish_without_interruption",
        "checkpoint_policy": "preserve",
    }


def _emit_gate_if_ready(
    config: Any,
    state: dict[str, Any],
    *,
    gate_size: int,
    now: float,
    logger: Any | None,
) -> list[str]:
    if state.get("pending_gate") is not None:
        return _recover_pending_gate(config, state, logger=logger)
    if _gate_progress(state) < gate_size:
        return []
    gate_index = int(state.get("gate_index") or 0) + 1
    window = state["window"]
    summary = {
        "contract": OBSERVATION_CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        "runtime_status": "TRIPPED" if circuit_breaker_active(config) else "ARMED",
        "gate_index": gate_index,
        "gate_size": gate_size,
        "gate_baseline_version": str(state.get("gate_baseline_version") or ""),
        "gate_start_epoch": float(state.get("gate_start_epoch") or 0),
        "generated_at": now,
        "baseline": dict(state.get("runtime_baseline") or {}),
        "window": {
            "started_at": window.get("started_at"),
            "ended_at": now,
            "terminal_attempts": int(window.get("terminal_attempts") or 0),
            "counters": dict(window.get("counters") or empty_summary_counters()),
            "error_codes": dict(sorted((window.get("error_codes") or {}).items())),
        },
        "totals": {
            "terminal_attempts_observed": int(state.get("total_attempts_observed") or 0),
            "excluded_claims": int(state.get("excluded_claims") or 0),
            "excluded_terminal_results": int(
                state.get("excluded_terminal_results") or 0
            ),
            "counters": dict(state.get("totals") or empty_summary_counters()),
        },
        "circuit_breaker": _breaker_summary(config),
        "strict_evidence_keys": list(STRICT_EVIDENCE_KEYS),
        "evidence_boundary": (
            "sanitized machine summary only; timestamped full logs remain on the server"
        ),
    }
    summary_text = _gate_summary_text(summary)
    target_name = f"gate-{gate_index:06d}.json"
    state["pending_gate"] = {
        "contract": "m2-gate-publish-journal-v1",
        "gate_index": gate_index,
        "target_name": target_name,
        "generated_at": now,
        "summary_sha256": hashlib.sha256(summary_text.encode("utf-8")).hexdigest(),
        "summary": summary,
    }
    state["updated_at"] = now
    # Persist intent before publishing the summary. Recovery can then either
    # publish the exact stored bytes or accept an already matching file.
    _write_observation_state(config, state)
    return _recover_pending_gate(config, state, logger=logger)


def _validate_pending_gate(
    config: Any,
    state: Mapping[str, Any],
    pending: Any,
) -> None:
    if not isinstance(pending, Mapping):
        raise ObservationStateError("observation_pending_gate_invalid")
    gate_index = state.get("gate_index")
    next_index = int(gate_index) + 1 if type(gate_index) is int else -1
    target_name = f"gate-{next_index:06d}.json"
    summary = pending.get("summary")
    if (
        pending.get("contract") != "m2-gate-publish-journal-v1"
        or pending.get("gate_index") != next_index
        or pending.get("target_name") != target_name
        or not isinstance(summary, Mapping)
    ):
        raise ObservationStateError("observation_pending_gate_invalid")
    try:
        generated_at = float(pending.get("generated_at"))
    except (TypeError, ValueError) as exc:
        raise ObservationStateError("observation_pending_gate_invalid") from exc
    gate_size = int(
        getattr(config, "m2_server_canary_observation_gate_size", 20) or 20
    )
    window = state.get("window")
    totals = summary.get("totals")
    summary_window = summary.get("window")
    if (
        not math.isfinite(generated_at)
        or generated_at <= 0
        or summary.get("contract") != OBSERVATION_CONTRACT
        or summary.get("schema_version") != SCHEMA_VERSION
        or summary.get("status") != STATUS
        or summary.get("gate_index") != next_index
        or summary.get("gate_size") != gate_size
        or str(summary.get("gate_baseline_version") or "")
        != str(state.get("gate_baseline_version") or "")
        or float(summary.get("gate_start_epoch") or 0)
        != float(state.get("gate_start_epoch") or 0)
        or dict(summary.get("baseline") or {})
        != dict(state.get("runtime_baseline") or {})
        or float(summary.get("generated_at") or 0) != generated_at
        or not isinstance(window, Mapping)
        or not isinstance(summary_window, Mapping)
        or summary_window.get("started_at") != window.get("started_at")
        or float(summary_window.get("ended_at") or 0) != generated_at
        or summary_window.get("terminal_attempts") != window.get("terminal_attempts")
        or dict(summary_window.get("counters") or {})
        != dict(window.get("counters") or {})
        or dict(summary_window.get("error_codes") or {})
        != dict(sorted((window.get("error_codes") or {}).items()))
        or not isinstance(totals, Mapping)
        or totals.get("terminal_attempts_observed")
        != state.get("total_attempts_observed")
        or totals.get("excluded_claims") != state.get("excluded_claims")
        or totals.get("excluded_terminal_results")
        != state.get("excluded_terminal_results")
        or dict(totals.get("counters") or {}) != dict(state.get("totals") or {})
        or list(summary.get("strict_evidence_keys") or [])
        != list(STRICT_EVIDENCE_KEYS)
    ):
        raise ObservationStateError("observation_pending_gate_mismatch")
    summary_text = _gate_summary_text(summary)
    expected_sha = hashlib.sha256(summary_text.encode("utf-8")).hexdigest()
    if str(pending.get("summary_sha256") or "").casefold() != expected_sha:
        raise ObservationStateError("observation_pending_gate_hash_mismatch")


def _recover_pending_gate(
    config: Any,
    state: dict[str, Any],
    *,
    logger: Any | None = None,
) -> list[str]:
    pending = state.get("pending_gate")
    if pending is None:
        return []
    _validate_pending_gate(config, state, pending)
    summary = dict(pending["summary"])
    summary_text = _gate_summary_text(summary)
    output_dir = observation_output_dir(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / str(pending["target_name"])
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ObservationStateError("observation_gate_unreadable") from exc
        if existing != summary_text:
            trip_circuit_breaker(
                config,
                "observation_gate_collision",
                evidence={"stage": "gate_publish"},
                logger=logger,
            )
            raise ObservationStateError("observation_gate_collision")
    else:
        atomic_write_text(target, summary_text)
    state["gate_index"] = int(pending["gate_index"])
    state["window"] = _empty_window()
    state["pending_gate"] = None
    state["updated_at"] = time.time()
    _write_observation_state(config, state)
    return [target.name]


def _gate_summary_text(summary: Mapping[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _sanitize_outcome(job_identity: str, outcome: Mapping[str, Any]) -> dict[str, Any]:
    stage = _safe_code(outcome.get("stage"), default="worker", limit=80)
    error_code = _safe_code(outcome.get("error_code"), default="", limit=120)
    reason_code = _safe_code(outcome.get("reason_code"), default="", limit=120)
    detail = str(outcome.get("detail") or outcome.get("error") or "")
    verified = bool(outcome.get("verified_completed"))
    supplied_status = str(outcome.get("terminal_status") or "").strip().upper()
    if not supplied_status:
        supplied_status = "COMPLETED" if verified else "FAILED"
    terminal_status = {
        "SUCCEEDED": "COMPLETED",
        "SUCCESS": "COMPLETED",
        "REVIEW_REQUIRED": "NEEDS_REVIEW",
        "RETRYABLE_FAILURE": "RETRYING",
        "PERMANENT_FAILURE": "FAILED",
    }.get(supplied_status, supplied_status)
    if terminal_status not in {
        "COMPLETED",
        "NEEDS_REVIEW",
        "FAILED",
        "QUARANTINED",
        "RETRYING",
        "RUNNING",
        "QUEUED",
        "UNKNOWN",
    }:
        terminal_status = "UNKNOWN"
    failed = bool(outcome.get("failed")) or terminal_status != "COMPLETED"
    return {
        "job_key": _job_key(job_identity),
        "verified_completed": verified,
        "failed": failed,
        "terminal_status": terminal_status,
        "processing_strategy": normalize_processing_strategy(
            outcome.get("processing_strategy")
        ),
        "stage": stage,
        "error_code": error_code,
        "reason_code": reason_code,
        "quarantined": bool(outcome.get("quarantined")),
        "hallucination_blocked": bool(outcome.get("hallucination_blocked")),
        "output_parse_failure": bool(outcome.get("output_parse_failure")),
        "source_mutation_incident": bool(outcome.get("source_mutation_incident")),
        "duplicate_job": bool(outcome.get("duplicate_job")),
        "duplicate_publish": bool(outcome.get("duplicate_publish")),
        "incorrect_completion": bool(outcome.get("incorrect_completion")),
        "checkpoint_resumed": bool(outcome.get("checkpoint_resumed")),
        "oom_event": bool(outcome.get("oom_event")),
        "unresolved_retry": bool(outcome.get("unresolved_retry")),
        "unresolved_fallback": bool(outcome.get("unresolved_fallback")),
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
    is_oom = bool(outcome.get("oom_event")) or _is_oom(text)
    state["oom_streak"] = int(state.get("oom_streak") or 0) + 1 if is_oom else 0
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
    stage = str(outcome.get("stage") or "")
    error_code = str(outcome.get("error_code") or "")
    detail = str(outcome.get("_classification_detail") or "")
    text = " ".join((stage, error_code, str(outcome.get("reason_code") or ""), detail))
    if bool(outcome.get("source_mutation_incident")) or any(
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
    if bool(outcome.get("duplicate_publish")) or any(
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
    if bool(outcome.get("output_parse_failure")) or (
        stage in {"completed_delivery", "delivery_verification", "mux", "publish"} and any(
        marker in text
        for marker in (
            "output_parse_failure",
            "output parse",
            "returned invalid json",
            "publication manifest is unreadable",
            "failed revalidation",
            "ffprobe returned",
        )
    )):
        return "output_parse_failure"
    if bool(outcome.get("incorrect_completion")) or error_code == "delivery_evidence_missing" or (
        "worker returned success without" in text
        and "verified" in text
    ):
        return "incorrect_completion"
    if not bool(outcome.get("failed")):
        return ""
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
        "strict_failure_count",
        "strict_failure_code",
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
        description="Print bounded M2 guardrail observation status."
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    from config import load_config

    config = load_config(args.config)
    print(json.dumps(public_status(config), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
