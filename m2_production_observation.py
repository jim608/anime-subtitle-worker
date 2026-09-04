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
from m2_observation_store import (
    ACTIVE as GATE_ACTIVE,
    ELIGIBILITY_POLICY_VERSION,
    INVALIDATED_AUTOMATION,
    INVALIDATED_RUNTIME,
    ObservationStoreError,
    active_gate as sqlite_active_gate,
    connect_observation_database,
    create_gate as sqlite_create_gate,
    enroll_claim as sqlite_enroll_claim,
    gate_by_id as sqlite_gate_by_id,
    gate_identifier as sqlite_gate_identifier,
    import_legacy_gate,
    immediate_transaction,
    invalidate_active_gate as sqlite_invalidate_active_gate,
    latest_gate as sqlite_latest_gate,
    member_for_job,
    meta_state as sqlite_meta_state,
    publish_pending_summaries,
    record_terminal_evidence,
    reserve_result_event,
    status_summary as sqlite_status_summary,
    update_failure_streaks as sqlite_update_failure_streaks,
    validate_active_runtime,
)
from safe_files import atomic_write_text


STATUS = "M2_GUARDRAILS_ARMED"
SCHEMA_VERSION = 2
OBSERVATION_CONTRACT = "m2-production-observation-v2"
_LOCK = threading.RLock()
_PROCESS_LOCAL_CIRCUIT_OPEN = False
_PROCESS_LOCAL_CIRCUIT_OPEN_AT = 0.0


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


def require_durable_claim_pause(config: Any) -> dict[str, Any]:
    """Prove the operator pause latch is durable before changing gate lifecycle.

    Circuit-breaker state is intentionally not accepted as a substitute.  A
    lifecycle transition must survive a Worker restart and must not depend on
    a process-local latch.
    """

    path = Path(str(config.work_path)) / "ai_control.json"
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size <= 0 or stat.st_size > 64 * 1024:
            raise ObservationStateError("ai_claim_pause_invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ObservationStateError("ai_claim_pause_missing") from exc
    except ObservationStateError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ObservationStateError("ai_claim_pause_unreadable") from exc
    if not isinstance(payload, Mapping) or payload.get("paused") is not True:
        raise ObservationStateError("ai_claims_not_paused")
    try:
        updated_at = float(payload.get("updated_at"))
    except (TypeError, ValueError) as exc:
        raise ObservationStateError("ai_claim_pause_timestamp_invalid") from exc
    if not math.isfinite(updated_at) or updated_at <= 0:
        raise ObservationStateError("ai_claim_pause_timestamp_invalid")
    return {
        "paused": True,
        "updated_at": updated_at,
        "requested_by": str(payload.get("requested_by") or "unknown")[:80],
    }


def circuit_breaker_active(config: Any) -> bool:
    global _PROCESS_LOCAL_CIRCUIT_OPEN, _PROCESS_LOCAL_CIRCUIT_OPEN_AT
    if not bool(getattr(config, "m2_server_canary_circuit_breaker_enabled", False)):
        return False
    path = circuit_breaker_state_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return bool(_PROCESS_LOCAL_CIRCUIT_OPEN)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # A malformed latch must fail closed. Operators can inspect or archive
        # it after the cause is understood; restart must not silently clear it.
        return path.exists()
    if not isinstance(payload, dict):
        return True
    if bool(payload.get("tripped")):
        return True
    if _PROCESS_LOCAL_CIRCUIT_OPEN:
        recovery = payload.get("recovery_record")
        try:
            recovered_at = float(
                recovery.get("recovered_at_epoch")
                if isinstance(recovery, Mapping)
                else 0
            )
        except (TypeError, ValueError):
            recovered_at = 0.0
        if (
            not isinstance(recovery, Mapping)
            or recovery.get("contract") != "m2-controlled-breaker-recovery-v1"
            or not str(recovery.get("recovery_record_id") or "")
            or recovered_at < float(_PROCESS_LOCAL_CIRCUIT_OPEN_AT or 0)
        ):
            return True
        _PROCESS_LOCAL_CIRCUIT_OPEN = False
        _PROCESS_LOCAL_CIRCUIT_OPEN_AT = 0.0
    return False


def admit_new_job(config: Any, *, logger: Any | None = None) -> bool:
    """Admit only an exact armed runtime immediately before a new claim."""

    if not _m2_guardrail_config_exists(config):
        return True
    breaker_latched = circuit_breaker_active(config)
    try:
        from m2_guardrail_runtime import runtime_guardrail_status

        runtime = runtime_guardrail_status(config)
    except Exception as exc:  # noqa: BLE001 - a missing runtime contract fails closed.
        if logger is not None:
            logger.exception("M2 runtime guardrail status failed: %s", exc)
        return False
    runtime_status = str(runtime.get("status") or "DEGRADED")
    if runtime_status not in {"ARMED", "TRIPPED"}:
        _persist_runtime_drift_invalidation(
            config,
            reason_code=str(runtime.get("reason_code") or "runtime_not_armed"),
            runtime_state=runtime.get("state"),
            runtime_result=runtime,
            logger=logger,
        )
        return False
    runtime_state = runtime.get("state")
    if not isinstance(runtime_state, Mapping):
        return False
    try:
        connection = connect_observation_database(config)
        try:
            with immediate_transaction(connection):
                validate_active_runtime(connection, runtime_state)
        finally:
            connection.close()
        publish_pending_summaries(config)
    except Exception as exc:  # noqa: BLE001 - reload-safe reason contract.
        reason_code = str(getattr(exc, "reason_code", "") or "")
        if not reason_code:
            if logger is not None:
                logger.exception("M2 observation admission validation failed: %s", exc)
            reason_code = "state_recovery_failed"
        if _is_runtime_invalidation_error(exc):
            _persist_runtime_invalidation_after_rollback(
                config,
                error=exc,
                runtime_state=runtime_state,
                runtime_result=runtime,
                logger=logger,
            )
            return False
        trip_circuit_breaker(
            config,
            "observation_state_degraded",
            evidence={"stage": "admission", "error_code": reason_code},
            logger=logger,
        )
        return False
    if breaker_latched or runtime_status == "TRIPPED":
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
    """Atomically create one empty frozen cohort in the scanner WAL database.

    The pre-repair JSON is imported as immutable invalidated history and is
    never deleted or overwritten.
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

    baseline = runtime_state.get("baseline")
    if (
        not isinstance(baseline, Mapping)
        or baseline.get("eligibility_policy_version") != ELIGIBILITY_POLICY_VERSION
    ):
        raise ValueError("M2 runtime eligibility policy is incomplete")

    supplied_gate_id = str(gate.get("gate_id") or "")
    pause_evidence: dict[str, Any] | None = None
    if not supplied_gate_id:
        pause_evidence = require_durable_claim_pause(config)

    connection = connect_observation_database(config)
    try:
        with immediate_transaction(connection):
            computed_gate_id = sqlite_gate_identifier(runtime_state)
            if supplied_gate_id and supplied_gate_id != computed_gate_id:
                raise ObservationStateError("runtime_gate_id_mismatch")
            existing = sqlite_gate_by_id(connection, computed_gate_id)
            if existing is None:
                pause_evidence = pause_evidence or require_durable_claim_pause(config)
                recoverable = sqlite_active_gate(connection)
                if recoverable is not None:
                    # Recover the narrow crash window where the SQLite gate
                    # committed but the ARMED runtime manifest did not.  The
                    # baseline must match exactly; the original start/ordinal
                    # identity is retained rather than creating a second gate.
                    recovery_state = dict(runtime_state)
                    recovery_gate = dict(gate)
                    recovery_gate["gate_id"] = str(recoverable["gate_id"])
                    recovery_state["gate"] = recovery_gate
                    recovery_state["gate_start_at"] = str(
                        recoverable["gate_start_at"]
                    )
                    recovery_state["gate_start_epoch"] = float(
                        recoverable["gate_start_epoch"]
                    )
                    validate_active_runtime(connection, recovery_state)
                    return recoverable
                legacy = _read_json_object(observation_state_path(config))
                if isinstance(legacy, Mapping):
                    _require_exact_legacy_import(connection, legacy)
            return sqlite_create_gate(connection, runtime_state, now=timestamp)
    except Exception as exc:  # noqa: BLE001 - preserve invalidation across reload.
        if _is_runtime_invalidation_error(exc):
            _persist_runtime_invalidation_after_rollback(
                config,
                error=exc,
                runtime_state=runtime_state,
                runtime_result={
                    "status": "ARMED",
                    "reason_code": _runtime_invalidation_cause(exc),
                    "state": runtime_state,
                },
                logger=None,
            )
        raise
    finally:
        connection.close()


def record_job_claim(
    config: Any,
    *,
    job_identity: str,
    claimed_at: float,
    gate_job_identity: str = "",
    input_fingerprint: str = "",
    processing_strategy: str = "",
    transaction_connection: Any | None = None,
    prepared_runtime_state: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Enroll a claim in the frozen cohort, optionally in the queue transaction."""

    if not _m2_guardrail_config_exists(config):
        return {}
    from m2_guardrail_runtime import gate_claim_eligible, runtime_guardrail_status

    # Never trust a prepared manifest as live evidence.  It is a hand-off from
    # an earlier admission check, while the code digest must be recomputed at
    # the actual enrollment boundary even in a caller-owned transaction.
    runtime = runtime_guardrail_status(config)
    runtime_state = runtime.get("state")
    if str(runtime.get("status") or "") != "ARMED" or not isinstance(
        runtime_state, Mapping
    ):
        if str(runtime.get("status") or "") == "TRIPPED":
            raise ObservationStateError("circuit_breaker_tripped")
        if transaction_connection is not None and transaction_connection.in_transaction:
            transaction_connection.rollback()
        _persist_runtime_drift_invalidation(
            config,
            reason_code=str(runtime.get("reason_code") or "runtime_contract_degraded"),
            runtime_state=runtime_state,
            runtime_result=runtime,
            logger=logger,
        )
        raise ObservationStateError("runtime_contract_degraded")
    baseline_version = str(runtime_state.get("gate_baseline_version") or "")
    eligible, reason = gate_claim_eligible(
        runtime_state,
        job_identity=job_identity,
        claimed_at=float(claimed_at),
        gate_baseline_version=baseline_version,
    )
    strategy = normalize_processing_strategy(processing_strategy)
    owns_connection = transaction_connection is None
    connection = transaction_connection or connect_observation_database(config)
    try:
        with immediate_transaction(connection):
            validate_active_runtime(connection, runtime_state)
            result = sqlite_enroll_claim(
                connection,
                runtime_state,
                claim_identity=job_identity,
                gate_job_identity=gate_job_identity or job_identity,
                input_fingerprint=input_fingerprint or gate_job_identity or job_identity,
                claimed_at=float(claimed_at),
                processing_strategy=strategy,
                eligible=bool(eligible),
                eligibility_reason=reason,
            )
    except Exception as exc:  # noqa: BLE001 - reload-safe durable reason contract.
        reason_code = str(getattr(exc, "reason_code", "") or "")
        if not reason_code:
            raise
        if _is_runtime_invalidation_error(exc):
            if connection.in_transaction:
                connection.rollback()
            _persist_runtime_invalidation_after_rollback(
                config,
                error=exc,
                runtime_state=runtime_state,
                runtime_result={
                    "status": "ARMED",
                    "reason_code": _runtime_invalidation_cause(exc),
                    "state": runtime_state,
                },
                logger=logger,
            )
            raise ObservationStateError(reason_code) from exc
        trip_circuit_breaker(
            config,
            "observation_state_degraded",
            evidence={"stage": "queue_claim", "error_code": reason_code},
            logger=logger,
        )
        raise ObservationStateError(reason_code) from exc
    finally:
        if owns_connection:
            connection.close()
    result["status"] = STATUS
    result["gate_eligible"] = bool(result.get("enrolled"))
    return result


def validate_claim_transaction(
    config: Any,
    *,
    transaction_connection: Any,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Validate or invalidate the active gate before queue mutation begins."""

    from m2_guardrail_runtime import runtime_guardrail_status

    runtime = runtime_guardrail_status(config)
    runtime_state = runtime.get("state")
    runtime_status = str(runtime.get("status") or "")
    if runtime_status not in {"ARMED", "TRIPPED"} or not isinstance(
        runtime_state, Mapping
    ):
        gate = sqlite_active_gate(transaction_connection)
        if gate is not None:
            sqlite_invalidate_active_gate(
                transaction_connection,
                INVALIDATED_RUNTIME,
                evidence={
                    "reason_code": str(runtime.get("reason_code") or "runtime_not_armed"),
                    "expected": {
                        "baseline_version": gate.get("baseline_version"),
                        "worker_sha": gate.get("worker_sha"),
                        "webui_sha": gate.get("webui_sha"),
                        "container_image_id": gate.get("container_image_id"),
                        "worker_container_id": gate.get("worker_container_id"),
                        "worker_source_revision": _gate_worker_source_revision(
                            gate
                        ),
                        "worker_runtime_code_revision": _gate_runtime_code_revision(
                            gate
                        ),
                        "runtime_instance_fingerprint": gate.get(
                            "worker_runtime_instance_fingerprint"
                        ),
                        "configuration_fingerprint": gate.get(
                            "configuration_fingerprint"
                        ),
                        "decision_schema_version": gate.get(
                            "decision_schema_version"
                        ),
                        "eligibility_policy_version": gate.get(
                            "eligibility_policy_version"
                        ),
                    },
                    "actual": _actual_runtime_fingerprint(config, runtime),
                },
            )
        trip_circuit_breaker(
            config,
            "runtime_change",
            evidence={
                "stage": "claim_transaction",
                "error_code": str(runtime.get("reason_code") or "runtime_not_armed"),
            },
            logger=logger,
        )
        return {
            "admitted": False,
            "reason_code": str(runtime.get("reason_code") or "runtime_not_armed"),
        }
    try:
        validate_active_runtime(transaction_connection, runtime_state)
    except ObservationStoreError as exc:
        trip_circuit_breaker(
            config,
            "runtime_change",
            evidence={"stage": "claim_transaction", "error_code": exc.reason_code},
            logger=logger,
        )
        return {"admitted": False, "reason_code": exc.reason_code}
    if runtime_status == "TRIPPED":
        return {
            "admitted": False,
            "reason_code": "circuit_breaker_tripped",
        }
    return {"admitted": True, "runtime_state": dict(runtime_state)}


def record_job_result(
    config: Any,
    *,
    job_identity: str,
    gate_job_identity: str = "",
    outcome: Mapping[str, Any],
    strict_evidence: Mapping[str, Any] | None = None,
    transaction_connection: Any | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Atomically persist one member outcome and journal an all-terminal summary."""

    if not _m2_guardrail_config_exists(config):
        return {}
    sanitized = _sanitize_outcome(job_identity, outcome)
    strict_outcome = _strict_outcome_from_sanitized(
        sanitized,
        claimed_after_gate_start=True,
    )
    qualification = qualify_strict_output(
        strict_outcome,
        {} if strict_evidence is None else strict_evidence,
    )
    owns_connection = transaction_connection is None
    connection = transaction_connection or connect_observation_database(config)
    emitted: list[str] = []
    try:
        with immediate_transaction(connection):
            try:
                from m2_guardrail_runtime import runtime_guardrail_status

                terminal_runtime = runtime_guardrail_status(config)
            except Exception:  # noqa: BLE001 - terminal evidence persists fail closed.
                terminal_runtime = {
                    "status": "DEGRADED",
                    "reason_code": "runtime_status_unavailable",
                    "state": None,
                }
            runtime_drift_reason = ""
            active = sqlite_active_gate(connection)
            if active is None and sqlite_latest_gate(connection) is None:
                raise ObservationStoreError("observation_gate_missing")
            runtime_state = terminal_runtime.get("state")
            if active is not None:
                if (
                    str(terminal_runtime.get("status") or "") in {"ARMED", "TRIPPED"}
                    and isinstance(runtime_state, Mapping)
                ):
                    try:
                        validate_active_runtime(connection, runtime_state)
                    except ObservationStoreError as exc:
                        runtime_drift_reason = exc.reason_code
                else:
                    runtime_drift_reason = str(
                        terminal_runtime.get("reason_code") or "runtime_not_armed"
                    )
                    sqlite_invalidate_active_gate(
                        connection,
                        INVALIDATED_RUNTIME,
                        evidence={
                            "reason_code": runtime_drift_reason,
                            "expected": {
                                "baseline_version": active.get("baseline_version"),
                                "worker_sha": active.get("worker_sha"),
                                "webui_sha": active.get("webui_sha"),
                                "container_image_id": active.get(
                                    "container_image_id"
                                ),
                                "worker_container_id": active.get(
                                    "worker_container_id"
                                ),
                                "worker_source_revision": (
                                    _gate_worker_source_revision(active)
                                ),
                                "worker_runtime_code_revision": (
                                    _gate_runtime_code_revision(active)
                                ),
                                "runtime_instance_fingerprint": active.get(
                                    "worker_runtime_instance_fingerprint"
                                ),
                                "configuration_fingerprint": active.get(
                                    "configuration_fingerprint"
                                ),
                                "decision_schema_version": active.get(
                                    "decision_schema_version"
                                ),
                                "eligibility_policy_version": active.get(
                                    "eligibility_policy_version"
                                ),
                            },
                            "actual": _actual_runtime_fingerprint(
                                config, terminal_runtime
                            ),
                        },
                    )
            text = " ".join(
                str(sanitized.get(key) or "")
                for key in (
                    "stage",
                    "error_code",
                    "reason_code",
                    "_classification_detail",
                )
            )
            signature = (
                f"{sanitized.get('stage') or 'worker'}:"
                f"{sanitized.get('error_code') or sanitized.get('reason_code') or 'unknown'}"
            )
            is_oom = bool(sanitized.get("oom_event")) or _is_oom(text)
            from m2_production_recovery import breaker_streak_eligible

            streak_eligible = breaker_streak_eligible(sanitized)
            breaker_job_key = _job_key(gate_job_identity or job_identity)
            projected_streaks = _preview_failure_streaks(
                sqlite_meta_state(connection),
                failed=streak_eligible,
                is_oom=is_oom,
                signature=signature,
                job_key=breaker_job_key,
            )
            breaker_reason = (
                _breaker_reason(sanitized, projected_streaks, config)
                if bool(
                    getattr(
                        config,
                        "m2_server_canary_circuit_breaker_enabled",
                        False,
                    )
                )
                else ""
            )
            if runtime_drift_reason:
                breaker_reason = "runtime_change"
                strict_outcome["breaker_tripped"] = True
                sanitized["breaker_tripped"] = True
                drift_evidence = dict(strict_evidence or {})
                drift_evidence["runtime_commit_matches_gate_baseline"] = False
                qualification = qualify_strict_output(
                    strict_outcome,
                    drift_evidence,
                )
            if (
                sanitized["terminal_status"] == "COMPLETED"
                and qualification.get("qualified") is not True
            ):
                strict_outcome["incorrect_completion"] = True
                sanitized["incorrect_completion"] = True
                breaker_reason = breaker_reason or "incorrect_completion"
                qualification = qualify_strict_output(
                    strict_outcome,
                    {} if strict_evidence is None else strict_evidence,
                )
            if breaker_reason:
                strict_outcome["breaker_tripped"] = True
                sanitized["breaker_tripped"] = True
            persisted_outcome = {
                key: value
                for key, value in sanitized.items()
                if not str(key).startswith("_")
            }
            event_reservation = reserve_result_event(
                connection,
                gate_job_identity=gate_job_identity or job_identity,
                claim_identity=job_identity,
                observed_state=str(sanitized.get("terminal_status") or "UNKNOWN"),
                event_payload={
                    "outcome": persisted_outcome,
                    "normalized_outcome": strict_outcome,
                    "qualification": qualification,
                    "breaker": {
                        "tripped": bool(breaker_reason),
                        "reason_code": breaker_reason,
                        "identical_failure_streak": projected_streaks[
                            "identical_failure_streak"
                        ],
                        "oom_streak": projected_streaks["oom_streak"],
                    },
                },
            )
            if not event_reservation["reserved"]:
                if runtime_drift_reason:
                    trip_circuit_breaker(
                        config,
                        "runtime_change",
                        evidence={
                            "job_key": sanitized["job_key"],
                            "stage": "terminal_observation_replay",
                            "error_code": runtime_drift_reason,
                        },
                        logger=logger,
                    )
                result = {
                    "recorded": False,
                    "enrolled": bool(event_reservation.get("enrolled")),
                    "settled": bool(event_reservation.get("settled")),
                    "duplicate_observation_ignored": True,
                    "duplicate_result_ignored": True,
                    "gate_id": str(event_reservation.get("gate_id") or ""),
                    "ordinal": event_reservation.get("ordinal"),
                    "emission_pending": bool(
                        event_reservation.get("emission_pending")
                    ),
                }
            else:
                streaks = sqlite_update_failure_streaks(
                    connection,
                    sanitized,
                    is_oom=is_oom,
                    failed=streak_eligible,
                    signature=signature,
                    job_key=breaker_job_key,
                )
                if streaks != projected_streaks:
                    raise ObservationStoreError("failure_streak_projection_mismatch")
                breaker_payload: dict[str, Any] = {}
                if breaker_reason:
                    breaker_payload = trip_circuit_breaker(
                        config,
                        breaker_reason,
                        evidence={
                            "job_key": breaker_job_key,
                            "stage": sanitized["stage"],
                            "error_code": sanitized["error_code"],
                            "identical_failure_streak": streaks[
                                "identical_failure_streak"
                            ],
                            "oom_streak": streaks["oom_streak"],
                            "strict_failure_count": len(
                                qualification.get("failed_evidence") or []
                            ),
                            "strict_failure_code": str(
                                (qualification.get("reason_codes") or [""])[0]
                            ),
                        },
                        logger=logger,
                    )
                result = record_terminal_evidence(
                    connection,
                    gate_job_identity=gate_job_identity or job_identity,
                    claim_identity=job_identity,
                    outcome=persisted_outcome,
                    qualification=qualification,
                    breaker_evidence={
                        "tripped": bool(breaker_reason),
                        "reason_code": breaker_reason,
                        "tripped_at": breaker_payload.get("tripped_at"),
                    },
                )
            summary = sqlite_status_summary(connection)
    except ObservationStoreError as exc:
        trip_circuit_breaker(
            config,
            "observation_state_degraded",
            evidence={"stage": "terminal_observation", "error_code": exc.reason_code},
            logger=logger,
        )
        raise ObservationStateError(exc.reason_code) from exc
    finally:
        if owns_connection:
            connection.close()
    if owns_connection:
        emitted = publish_pending_summaries(config)
    return {
        "status": STATUS,
        "gate_id": result.get("gate_id", summary.get("gate_id", "")),
        "frozen_cohort_progress": f"{summary.get('enrolled_count', 0)}/{summary.get('target_size', 20)}",
        "settled_progress": f"{summary.get('settled_count', 0)}/{summary.get('target_size', 20)}",
        "verified_since_gate": int(summary.get("strict_verified_count") or 0),
        "emitted": emitted,
        "circuit_breaker_tripped": circuit_breaker_active(config),
        "gate_eligible": bool(result.get("enrolled")),
        "strictly_qualified": bool(result.get("strictly_qualified")),
        **{key: value for key, value in result.items() if key not in {"status"}},
    }


def record_state_attempt_result(
    config: Any,
    state: Any,
    video: str | Path,
    delivery_attempt_id: str,
    *,
    transaction_connection: Any,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Build and persist one queue attempt result on the caller's transaction."""

    if not bool(getattr(config, "m2_server_canary_observer_enabled", False)):
        return {}
    try:
        from m2_guardrail_runtime import load_runtime_state
        from m2_strict_runtime_evidence import build_m2_strict_runtime_evidence

        strict_result = build_m2_strict_runtime_evidence(
            state,
            video,
            config,
            delivery_attempt_id,
            load_runtime_state(config),
        )
        attempt = state.get_ai_delivery_attempt(delivery_attempt_id)
        gate_job_identity = (
            str(attempt.get("obligation_id") or delivery_attempt_id)
            if isinstance(attempt, Mapping)
            else delivery_attempt_id
        )
        return record_job_result(
            config,
            job_identity=delivery_attempt_id,
            gate_job_identity=gate_job_identity,
            outcome=strict_result["outcome"],
            strict_evidence=strict_result["evidence"],
            transaction_connection=transaction_connection,
            logger=logger,
        )
    except Exception:
        try:
            trip_circuit_breaker(
                config,
                "observation_pipeline_failure",
                evidence={"stage": "terminal_observation"},
                logger=logger,
            )
        except Exception:
            # The process-local latch is set before breaker evidence is written.
            pass
        raise


def has_durable_gate_claim_binding(
    config: Any,
    *,
    job_identity: str,
    gate_job_identity: str = "",
    transaction_connection: Any | None = None,
) -> bool:
    """Return whether an attempt has an intact, gate-eligible durable binding.

    Missing or malformed runtime/observer state raises ``ObservationStateError``
    so a completion caller cannot mistake unavailable evidence for a negative
    lookup and proceed fail-open.
    """

    if not str(job_identity or "").strip():
        raise ObservationStateError("job_identity_invalid")
    from m2_guardrail_runtime import runtime_guardrail_status

    runtime = runtime_guardrail_status(config)
    runtime_state = runtime.get("state")
    if str(runtime.get("status") or "") != "ARMED" or not isinstance(
        runtime_state, Mapping
    ):
        raise ObservationStateError("runtime_state_not_armed")
    owns_connection = transaction_connection is None
    connection = transaction_connection or connect_observation_database(config)
    try:
        with immediate_transaction(connection):
            gate = validate_active_runtime(connection, runtime_state)
            job_id = hashlib.sha256(
                str(gate_job_identity or job_identity).encode("utf-8")
            ).hexdigest()
            return member_for_job(connection, str(gate["gate_id"]), job_id) is not None
    except Exception as exc:
        # The frozen-store restart test deliberately reloads its module; use
        # the durable reason contract rather than relying on class identity
        # surviving that in-process reload.
        reason_code = getattr(exc, "reason_code", "")
        if not reason_code:
            raise
        if _is_runtime_invalidation_error(exc):
            if connection.in_transaction:
                connection.rollback()
            _persist_runtime_invalidation_after_rollback(
                config,
                error=exc,
                runtime_state=runtime_state,
                runtime_result=runtime,
                logger=None,
            )
        raise ObservationStateError(str(reason_code)) from exc
    finally:
        if owns_connection:
            connection.close()


def invalidate_observation_gate(
    config: Any,
    *,
    reason: str,
    expected_gate_baseline_version: str,
    expected_gate_start_at: str,
    expected_worker_sha: str,
    expected_webui_sha: str,
    expected_pre_gate_attempts: int,
    now: float | None = None,
) -> dict[str, Any]:
    """Import and retire the exact pre-repair gate without deleting evidence."""

    from m2_guardrail_runtime import runtime_state_path

    timestamp = time.time() if now is None else float(now)
    if reason != INVALIDATED_AUTOMATION:
        raise ObservationStateError("legacy_gate_invalidation_reason_mismatch")
    pause_evidence = require_durable_claim_pause(config)
    runtime_path = runtime_state_path(config)
    runtime_state = _read_json_object(runtime_path)
    if not isinstance(runtime_state, Mapping):
        raise ObservationStateError("runtime_state_missing")
    legacy = _read_json_object(observation_state_path(config))
    if not isinstance(legacy, Mapping):
        raise ObservationStateError("legacy_observation_state_missing")

    # All caller-supplied expectations are checked before the scanner database
    # is opened.  This makes a typo or stale operator snapshot a zero-write
    # failure instead of a partially imported/inactivated lifecycle.
    _assert_expected_legacy_runtime(
        runtime_state,
        expected_gate_baseline_version=expected_gate_baseline_version,
        expected_gate_start_at=expected_gate_start_at,
        expected_worker_sha=expected_worker_sha,
        expected_webui_sha=expected_webui_sha,
        expected_pre_gate_attempts=expected_pre_gate_attempts,
    )
    legacy_baseline_version = str(legacy.get("gate_baseline_version") or "")
    if legacy_baseline_version and legacy_baseline_version != expected_gate_baseline_version:
        raise ObservationStateError("legacy_observation_baseline_mismatch")
    legacy_start_at = str(legacy.get("gate_start_at") or "")
    if legacy_start_at and legacy_start_at != expected_gate_start_at:
        raise ObservationStateError("legacy_observation_start_mismatch")
    historical_text = json.dumps(
        dict(legacy), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    historical_digest = "sha256:" + hashlib.sha256(
        historical_text.encode("utf-8")
    ).hexdigest()
    expected_gate_id = sqlite_gate_identifier(
        {
            "gate_baseline_version": expected_gate_baseline_version,
            "gate_start_epoch": float(runtime_state.get("gate_start_epoch") or 0),
        }
    )

    connection = connect_observation_database(config)
    try:
        with immediate_transaction(connection):
            require_durable_claim_pause(config)
            existing_before = sqlite_gate_by_id(connection, expected_gate_id)
            unrelated_active = sqlite_active_gate(connection)
            if (
                unrelated_active is not None
                and str(unrelated_active.get("gate_id") or "") != expected_gate_id
            ):
                raise ObservationStateError("different_active_gate_exists")
            import_legacy_gate(
                connection,
                legacy,
                runtime_state,
                now=timestamp,
            )
            gate = sqlite_gate_by_id(connection, expected_gate_id)
            if gate is None:
                raise ObservationStateError("legacy_gate_missing")
            _assert_expected_legacy_gate(
                gate,
                expected_gate_baseline_version=expected_gate_baseline_version,
                expected_gate_start_at=expected_gate_start_at,
                expected_worker_sha=expected_worker_sha,
                expected_webui_sha=expected_webui_sha,
                expected_pre_gate_attempts=expected_pre_gate_attempts,
            )
            _assert_exact_legacy_payloads(gate, legacy, runtime_state)
            already_invalidated = (
                existing_before is not None
                and str(existing_before.get("status") or "") == INVALIDATED_AUTOMATION
            )
            if str(gate.get("status") or "") == GATE_ACTIVE:
                gate = sqlite_invalidate_active_gate(
                    connection,
                    INVALIDATED_AUTOMATION,
                    evidence={
                        "reason": "legacy_success_count_observer_not_ready",
                        "expected_pre_gate_attempts": int(expected_pre_gate_attempts),
                    },
                    now=timestamp,
                )
                already_invalidated = False
            if gate is None or str(gate.get("status") or "") != INVALIDATED_AUTOMATION:
                raise ObservationStateError("legacy_gate_invalidation_failed")
    finally:
        connection.close()

    if str(runtime_state.get("status") or "") == "ARMED":
        retired = dict(runtime_state)
        retired["status"] = "DEGRADED"
        retired["reason_code"] = INVALIDATED_AUTOMATION
        retired["gate_final_status"] = INVALIDATED_AUTOMATION
        retired["invalidated_at"] = timestamp
        atomic_write_text(
            runtime_path,
            json.dumps(retired, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
    return {
        "status": INVALIDATED_AUTOMATION,
        "gate_id": str(gate["gate_id"]),
        "baseline_version": str(gate["baseline_version"]),
        "gate_start_at": str(gate["gate_start_at"]),
        "invalidation_reason": str(gate["invalidation_reason"]),
        "invalidated_at": float(gate["invalidated_at"] or timestamp),
        "historical_evidence_sha256": historical_digest,
        "supplemental_pre_gate_attempts": int(gate["pre_gate_attempt_count"]),
        "enrolled_count": int(gate["enrolled_count"]),
        "settled_count": int(gate["settled_count"]),
        "already_invalidated": already_invalidated,
        "history_preserved": True,
        "claims_paused": pause_evidence["paused"],
        "production_resources_affected": False,
    }


def _latest_sqlite_gate(connection: Any) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT * FROM m2_observation_gates ORDER BY gate_start_epoch DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in cursor.description or []]
    return dict(zip(columns, row, strict=True))


def _assert_expected_legacy_runtime(
    runtime_state: Mapping[str, Any],
    *,
    expected_gate_baseline_version: str,
    expected_gate_start_at: str,
    expected_worker_sha: str,
    expected_webui_sha: str,
    expected_pre_gate_attempts: int,
) -> None:
    baseline = runtime_state.get("baseline")
    pre_gate = runtime_state.get("pre_gate_running")
    attempt_keys = pre_gate.get("attempt_keys") if isinstance(pre_gate, Mapping) else None
    queue_job_keys = (
        pre_gate.get("queue_job_keys") if isinstance(pre_gate, Mapping) else None
    )
    runtime_status = str(runtime_state.get("status") or "")
    already_retired = (
        runtime_status == "DEGRADED"
        and str(runtime_state.get("reason_code") or "") == INVALIDATED_AUTOMATION
        and str(runtime_state.get("gate_final_status") or "")
        == INVALIDATED_AUTOMATION
    )
    if (
        runtime_status != "ARMED"
        and not already_retired
        or str(runtime_state.get("gate_baseline_version") or "")
        != expected_gate_baseline_version
        or str(runtime_state.get("gate_start_at") or "") != expected_gate_start_at
        or not isinstance(baseline, Mapping)
        or str(baseline.get("worker_commit_sha") or "") != expected_worker_sha
        or str(baseline.get("webui_commit_sha") or "") != expected_webui_sha
        or not isinstance(pre_gate, Mapping)
        or not isinstance(attempt_keys, list)
        or not isinstance(queue_job_keys, list)
        or int(pre_gate.get("attempt_count") or 0) != int(expected_pre_gate_attempts)
        or int(pre_gate.get("attempt_count") or 0) != len(attempt_keys)
        or int(pre_gate.get("queue_job_count") or 0) != len(queue_job_keys)
    ):
        raise ObservationStateError("runtime_legacy_gate_expectation_mismatch")


def _assert_expected_legacy_gate(
    gate: Mapping[str, Any],
    *,
    expected_gate_baseline_version: str,
    expected_gate_start_at: str,
    expected_worker_sha: str,
    expected_webui_sha: str,
    expected_pre_gate_attempts: int,
) -> None:
    if (
        str(gate.get("baseline_version") or "") != expected_gate_baseline_version
        or str(gate.get("gate_start_at") or "") != expected_gate_start_at
        or str(gate.get("worker_sha") or "") != expected_worker_sha
        or str(gate.get("webui_sha") or "") != expected_webui_sha
        or int(gate.get("pre_gate_attempt_count") or 0)
        != int(expected_pre_gate_attempts)
    ):
        raise ObservationStateError("legacy_gate_expectation_mismatch")


def _canonical_mapping_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _legacy_runtime_manifest_payload(
    runtime_state: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(runtime_state)
    if (
        str(payload.get("status") or "") == "DEGRADED"
        and str(payload.get("reason_code") or "") == INVALIDATED_AUTOMATION
        and str(payload.get("gate_final_status") or "") == INVALIDATED_AUTOMATION
    ):
        payload["status"] = "ARMED"
        payload.pop("reason_code", None)
        payload.pop("gate_final_status", None)
        payload.pop("invalidated_at", None)
    return payload


def _validated_legacy_runtime_manifest(gate: Mapping[str, Any]) -> dict[str, Any]:
    manifest_text = str(gate.get("legacy_runtime_manifest_json") or "")
    manifest_digest = str(gate.get("legacy_runtime_manifest_sha256") or "")
    if not manifest_text or not re.fullmatch(r"[0-9a-f]{64}", manifest_digest):
        raise ObservationStateError("legacy_runtime_manifest_missing")
    if hashlib.sha256(manifest_text.encode("utf-8")).hexdigest() != manifest_digest:
        raise ObservationStateError("legacy_runtime_manifest_digest_mismatch")
    try:
        payload = json.loads(manifest_text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ObservationStateError("legacy_runtime_manifest_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ObservationStateError("legacy_runtime_manifest_invalid")
    if _canonical_mapping_json(payload) != manifest_text:
        raise ObservationStateError("legacy_runtime_manifest_not_canonical")
    return dict(payload)


def _assert_exact_legacy_payloads(
    gate: Mapping[str, Any],
    legacy_state: Mapping[str, Any],
    runtime_state: Mapping[str, Any],
) -> None:
    if str(gate.get("legacy_observation_json") or "") != _canonical_mapping_json(
        legacy_state
    ):
        raise ObservationStateError("legacy_observation_payload_mismatch")
    stored_runtime = _validated_legacy_runtime_manifest(gate)
    if stored_runtime != _legacy_runtime_manifest_payload(runtime_state):
        raise ObservationStateError("legacy_runtime_manifest_mismatch")
    baseline = stored_runtime.get("baseline")
    pre_gate = stored_runtime.get("pre_gate_running")
    attempt_keys = pre_gate.get("attempt_keys") if isinstance(pre_gate, Mapping) else None
    queue_job_keys = (
        pre_gate.get("queue_job_keys") if isinstance(pre_gate, Mapping) else None
    )
    if (
        not isinstance(baseline, Mapping)
        or not isinstance(attempt_keys, list)
        or not isinstance(queue_job_keys, list)
        or int(pre_gate.get("attempt_count") or 0) != len(attempt_keys)
        or int(pre_gate.get("queue_job_count") or 0) != len(queue_job_keys)
        or int(gate.get("pre_gate_attempt_count") or 0) != len(attempt_keys)
        or str(stored_runtime.get("gate_baseline_version") or "")
        != str(gate.get("baseline_version") or "")
        or str(stored_runtime.get("gate_start_at") or "")
        != str(gate.get("gate_start_at") or "")
        or str(baseline.get("worker_commit_sha") or "")
        != str(gate.get("worker_sha") or "")
        or str(baseline.get("webui_commit_sha") or "")
        != str(gate.get("webui_sha") or "")
    ):
        raise ObservationStateError("legacy_runtime_manifest_identity_mismatch")


def _require_exact_legacy_import(
    connection: Any,
    legacy_state: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        gate_id = sqlite_gate_identifier(
            {
                "gate_baseline_version": str(
                    legacy_state.get("gate_baseline_version") or ""
                ),
                "gate_start_epoch": float(
                    legacy_state.get("gate_start_epoch") or 0
                ),
            }
        )
        gate = sqlite_gate_by_id(connection, gate_id)
        if gate is None or str(gate.get("status") or "") != INVALIDATED_AUTOMATION:
            raise ObservationStateError("legacy_gate_not_explicitly_invalidated")
        runtime_state = _validated_legacy_runtime_manifest(gate)
        _assert_exact_legacy_payloads(gate, legacy_state, runtime_state)
        return gate
    except (ObservationStateError, ObservationStoreError, TypeError, ValueError) as exc:
        raise ObservationStateError("legacy_gate_requires_exact_invalidation") from exc


def _gate_runtime_code_revision(gate: Mapping[str, Any]) -> str:
    try:
        baseline = json.loads(str(gate.get("baseline_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "UNAVAILABLE"
    if not isinstance(baseline, Mapping):
        return "UNAVAILABLE"
    return str(baseline.get("worker_runtime_code_revision") or "UNAVAILABLE")


def _gate_worker_source_revision(gate: Mapping[str, Any]) -> str:
    try:
        baseline = json.loads(str(gate.get("baseline_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "UNAVAILABLE"
    if not isinstance(baseline, Mapping):
        return "UNAVAILABLE"
    return str(baseline.get("worker_source_revision") or "UNAVAILABLE")


def _actual_runtime_fingerprint(config: Any, runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Capture bounded, path-free actual values for drift evidence."""

    actual: dict[str, Any] = {
        "runtime_status": str(runtime.get("status") or "DEGRADED"),
        "reason_code": str(runtime.get("reason_code") or "runtime_status_unavailable"),
        "decision_schema_version": int(
            getattr(config, "source_decision_schema_version", 0) or 0
        ),
        "decision_version": str(getattr(config, "source_decision_version", "") or ""),
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
    }
    try:
        from m2_guardrail_runtime import (
            configuration_fingerprint,
            worker_runtime_instance_fingerprint,
        )

        actual["configuration_fingerprint"] = configuration_fingerprint(config)
        actual.update(worker_runtime_instance_fingerprint(config))
    except Exception:  # noqa: BLE001 - retain the reason code and other evidence.
        actual["runtime_instance_evidence"] = "UNAVAILABLE"
    marker = Path(
        str(
            getattr(
                config,
                "m2_guardrail_source_revision_file",
                "/app/.source-revision",
            )
        )
    )
    try:
        value = marker.read_text(encoding="utf-8").strip().casefold()
        actual["worker_source_revision"] = (
            value if re.fullmatch(r"[0-9a-f]{64}", value) else "INVALID"
        )
    except OSError:
        actual["worker_source_revision"] = "UNAVAILABLE"
    try:
        from m2_guardrail_runtime import worker_runtime_code_revision

        actual["worker_runtime_code_revision"] = worker_runtime_code_revision(config)
    except Exception:  # noqa: BLE001 - bounded drift evidence must remain available.
        actual["worker_runtime_code_revision"] = "UNAVAILABLE"
    runtime_state = runtime.get("state")
    if isinstance(runtime_state, Mapping):
        baseline = runtime_state.get("baseline")
        if isinstance(baseline, Mapping):
            actual.update(
                {
                    "baseline_version": str(
                        runtime_state.get("gate_baseline_version") or ""
                    ),
                    "worker_sha": str(baseline.get("worker_commit_sha") or ""),
                    "webui_sha": str(baseline.get("webui_commit_sha") or ""),
                    "container_image_id": str(
                        baseline.get("worker_image_id") or ""
                    ),
                    "worker_container_id": str(
                        baseline.get("worker_container_id") or ""
                    ),
                    "worker_runtime_instance_fingerprint": str(
                        actual.get("runtime_instance_fingerprint") or "UNAVAILABLE"
                    ),
                }
            )
            actual["declared_worker_sha"] = str(
                baseline.get("worker_commit_sha") or ""
            )
            actual["declared_container_image_id"] = str(
                baseline.get("worker_image_id") or ""
            )
    return actual


def _is_runtime_invalidation_error(error: BaseException) -> bool:
    return str(getattr(error, "reason_code", "") or "") == INVALIDATED_RUNTIME


def _runtime_invalidation_cause(error: BaseException) -> str:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        reason = str(getattr(current, "reason_code", "") or "")
        if reason and reason != INVALIDATED_RUNTIME:
            return reason
        current = current.__cause__
    return INVALIDATED_RUNTIME


def _persist_runtime_invalidation_after_rollback(
    config: Any,
    *,
    error: BaseException,
    runtime_state: Any,
    runtime_result: Mapping[str, Any] | None,
    logger: Any | None,
) -> None:
    """Redo a validator's rolled-back invalidation in a fresh transaction."""

    cause = _runtime_invalidation_cause(error)
    _persist_runtime_drift_invalidation(
        config,
        reason_code=cause,
        runtime_state=runtime_state,
        runtime_result=runtime_result,
        logger=logger,
    )
    try:
        trip_circuit_breaker(
            config,
            "runtime_change",
            evidence={
                "stage": "runtime_validation_after_rollback",
                "error_code": cause,
            },
            logger=logger,
        )
    except Exception:
        # trip_circuit_breaker sets the process-local latch before persistence.
        pass


def _persist_runtime_drift_invalidation(
    config: Any,
    *,
    reason_code: str,
    runtime_state: Any,
    runtime_result: Mapping[str, Any] | None = None,
    logger: Any | None,
) -> None:
    try:
        connection = connect_observation_database(config)
        try:
            with immediate_transaction(connection):
                gate = sqlite_active_gate(connection)
                if gate is None:
                    return
                sqlite_invalidate_active_gate(
                    connection,
                    INVALIDATED_RUNTIME,
                    evidence={
                        "reason_code": reason_code,
                        "expected": {
                            "baseline_version": gate.get("baseline_version"),
                            "worker_sha": gate.get("worker_sha"),
                            "webui_sha": gate.get("webui_sha"),
                            "container_image_id": gate.get("container_image_id"),
                            "worker_container_id": gate.get("worker_container_id"),
                            "worker_source_revision": (
                                _gate_worker_source_revision(gate)
                            ),
                            "worker_runtime_code_revision": (
                                _gate_runtime_code_revision(gate)
                            ),
                            "runtime_instance_fingerprint": gate.get(
                                "worker_runtime_instance_fingerprint"
                            ),
                            "configuration_fingerprint": gate.get(
                                "configuration_fingerprint"
                            ),
                            "decision_schema_version": gate.get(
                                "decision_schema_version"
                            ),
                            "eligibility_policy_version": gate.get(
                                "eligibility_policy_version"
                            ),
                        },
                        "actual": _actual_runtime_fingerprint(
                            config,
                            runtime_result
                            or {
                                "status": "DEGRADED",
                                "reason_code": reason_code,
                                "state": runtime_state,
                            },
                        ),
                    },
                )
        finally:
            connection.close()
        trip_circuit_breaker(
            config,
            "runtime_change",
            evidence={"stage": "runtime_validation", "error_code": reason_code},
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001 - admission is already fail-closed.
        if logger is not None:
            logger.exception("M2 runtime drift persistence failed: %s", exc)


def trip_circuit_breaker(
    config: Any,
    reason_code: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Latch stop-new-work state without touching jobs, attempts, or checkpoints."""

    global _PROCESS_LOCAL_CIRCUIT_OPEN, _PROCESS_LOCAL_CIRCUIT_OPEN_AT
    _PROCESS_LOCAL_CIRCUIT_OPEN = True
    now = time.time()
    _PROCESS_LOCAL_CIRCUIT_OPEN_AT = now
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

    breaker = _read_json_object(circuit_breaker_state_path(config)) or {}
    try:
        connection = connect_observation_database(config)
        try:
            gate_status = sqlite_status_summary(connection)
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - status reports missing/degraded without paths.
        gate_status = {
            "gate_id": "",
            "gate_status": "MISSING",
            "gate_baseline_version": "",
            "target_size": 20,
            "enrolled_count": 0,
            "settled_count": 0,
            "strict_verified_count": 0,
        }
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
        "gate_id": str(gate_status.get("gate_id") or ""),
        "gate_status": str(gate_status.get("gate_status") or "MISSING"),
        "gate_progress": int(gate_status.get("enrolled_count") or 0),
        "frozen_cohort_progress": (
            f"{int(gate_status.get('enrolled_count') or 0)}/"
            f"{int(gate_status.get('target_size') or 20)}"
        ),
        "settled_progress": (
            f"{int(gate_status.get('settled_count') or 0)}/"
            f"{int(gate_status.get('target_size') or 20)}"
        ),
        "completed_strict_verified": int(
            gate_status.get("strict_verified_count") or 0
        ),
        "gate_baseline_version": str(
            gate_status.get("gate_baseline_version") or ""
        ),
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
    from m2_production_recovery import breaker_streak_eligible

    if not breaker_streak_eligible(outcome):
        state["oom_streak"] = 0
        state["oom_job_ids"] = []
        state["identical_failure_signature"] = ""
        state["identical_failure_streak"] = 0
        state["identical_failure_job_ids"] = []
        return
    text = " ".join(
        str(outcome.get(key) or "")
        for key in ("error_code", "reason_code", "_classification_detail")
    )
    is_oom = bool(outcome.get("oom_event")) or _is_oom(text)
    job_key = str(outcome.get("job_key") or "unknown_job")
    if is_oom:
        oom_jobs = list(state.get("oom_job_ids") or [])
        if job_key not in oom_jobs:
            oom_jobs.append(job_key)
        state["oom_job_ids"] = oom_jobs[-50:]
        state["oom_streak"] = len(state["oom_job_ids"])
    else:
        state["oom_streak"] = 0
        state["oom_job_ids"] = []
    signature = f"{outcome.get('stage') or 'worker'}:{outcome.get('error_code') or outcome.get('reason_code') or 'unknown'}"
    if signature == state.get("identical_failure_signature"):
        identical_jobs = list(state.get("identical_failure_job_ids") or [])
        if job_key not in identical_jobs:
            identical_jobs.append(job_key)
        state["identical_failure_job_ids"] = identical_jobs[-50:]
        state["identical_failure_streak"] = len(state["identical_failure_job_ids"])
    else:
        state["identical_failure_signature"] = signature
        state["identical_failure_job_ids"] = [job_key]
        state["identical_failure_streak"] = 1


def _preview_failure_streaks(
    current: Mapping[str, Any],
    *,
    failed: bool,
    is_oom: bool,
    signature: str,
    job_key: str,
) -> dict[str, Any]:
    """Project the idempotent breaker counters before reserving an event."""

    projected = {
        "oom_streak": int(current.get("oom_streak") or 0),
        "oom_job_ids": list(current.get("oom_job_ids") or []),
        "identical_failure_signature": str(
            current.get("identical_failure_signature") or ""
        ),
        "identical_failure_streak": int(
            current.get("identical_failure_streak") or 0
        ),
        "identical_failure_job_ids": list(
            current.get("identical_failure_job_ids") or []
        ),
    }
    if not failed:
        projected.update(
            {
                "oom_streak": 0,
                "oom_job_ids": [],
                "identical_failure_signature": "",
                "identical_failure_streak": 0,
                "identical_failure_job_ids": [],
            }
        )
        return projected
    normalized_job = str(job_key or "unknown_job")
    if is_oom:
        if normalized_job not in projected["oom_job_ids"]:
            projected["oom_job_ids"].append(normalized_job)
        projected["oom_job_ids"] = projected["oom_job_ids"][-50:]
        projected["oom_streak"] = len(projected["oom_job_ids"])
    else:
        projected["oom_streak"] = 0
        projected["oom_job_ids"] = []
    if signature == projected["identical_failure_signature"]:
        if normalized_job not in projected["identical_failure_job_ids"]:
            projected["identical_failure_job_ids"].append(normalized_job)
        projected["identical_failure_job_ids"] = projected[
            "identical_failure_job_ids"
        ][-50:]
        projected["identical_failure_streak"] = len(
            projected["identical_failure_job_ids"]
        )
    else:
        projected["identical_failure_signature"] = str(signature)
        projected["identical_failure_job_ids"] = [normalized_job]
        projected["identical_failure_streak"] = 1
    return projected


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
        description="Manage the bounded M2 frozen observation gate."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status", "invalidate-legacy"),
        default="status",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--reason", default="")
    parser.add_argument("--expected-gate-baseline-version", default="")
    parser.add_argument("--expected-gate-start-at", default="")
    parser.add_argument("--expected-worker-sha", default="")
    parser.add_argument("--expected-webui-sha", default="")
    parser.add_argument("--expected-pre-gate-attempts", type=int, default=-1)
    args = parser.parse_args(argv)
    from config import load_config

    config = load_config(args.config)
    try:
        if args.command == "invalidate-legacy":
            if args.reason != INVALIDATED_AUTOMATION:
                parser.error(f"--reason must be {INVALIDATED_AUTOMATION}")
            if any(
                not str(value or "").strip()
                for value in (
                    args.expected_gate_baseline_version,
                    args.expected_gate_start_at,
                    args.expected_worker_sha,
                    args.expected_webui_sha,
                )
            ) or args.expected_pre_gate_attempts < 0:
                parser.error("invalidate-legacy requires every exact expected value")
            payload = invalidate_observation_gate(
                config,
                reason=args.reason,
                expected_gate_baseline_version=args.expected_gate_baseline_version,
                expected_gate_start_at=args.expected_gate_start_at,
                expected_worker_sha=args.expected_worker_sha,
                expected_webui_sha=args.expected_webui_sha,
                expected_pre_gate_attempts=args.expected_pre_gate_attempts,
            )
        else:
            payload = public_status(config)
    except (ObservationStateError, ObservationStoreError) as exc:
        print(
            json.dumps(
                {"status": "DEGRADED", "reason_code": exc.reason_code},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
