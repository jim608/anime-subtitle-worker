from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from acceptance_queue_lane import (
    AcceptanceQueueLane,
    AcceptanceQueueTarget,
    load_acceptance_queue_lane,
)
from scan_state import scan_state_path


ACCEPTANCE_ATTEMPT_CONTEXT_CONTRACT = "anime-acceptance-attempt-context-v1"
ACCEPTANCE_ATTEMPT_CONTEXT_SCHEMA_VERSION = 1
ACCEPTANCE_ATTEMPT_CONTEXT_ENV = "ANIME_ACCEPTANCE_ATTEMPT_CONTEXT"
_MAX_SERIALIZED_CONTEXT_BYTES = 8192
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"accrun_[0-9a-f]{48}")
_OBLIGATION_ID = re.compile(r"aiobl_[0-9a-f]{64}")
_ATTEMPT_ID = re.compile(r"aiatt_[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CONTEXT_FIELDS = {
    "contract",
    "schema_version",
    "run_id",
    "plan_sha256",
    "case_id",
    "fault_id",
    "fault_scenario",
    "canonical_path",
    "obligation_id",
    "delivery_attempt_id",
    "attempt_number",
    "started_at",
}


class AcceptanceRuntimeContextError(ValueError):
    """An isolated Worker cannot prove ownership of its acceptance attempt."""


@dataclass(frozen=True)
class AcceptanceAttemptContext:
    contract: str
    schema_version: int
    run_id: str
    plan_sha256: str
    case_id: str
    fault_id: str
    fault_scenario: str
    canonical_path: str
    obligation_id: str
    delivery_attempt_id: str
    attempt_number: int
    started_at: float


def build_acceptance_attempt_context(
    state: Any,
    config: Any,
    video: str | Path,
    delivery_attempt_id: str,
) -> AcceptanceAttemptContext | None:
    """Build parent-side immutable context after the queue claim commits."""

    lane = load_acceptance_queue_lane(config)
    if lane is None:
        return None
    target = lane.target_for_path(video)
    if target is None:
        raise AcceptanceRuntimeContextError(
            f"acceptance attempt path is not in the immutable plan: {video}"
        )
    if not target.case_id:
        raise AcceptanceRuntimeContextError("acceptance target has no immutable case identity")
    attempt = state.get_ai_delivery_attempt(str(delivery_attempt_id))
    if not isinstance(attempt, dict):
        raise AcceptanceRuntimeContextError("claimed delivery attempt is missing")
    obligation = state.get_ai_delivery_obligation(str(target.obligation_id))
    if not isinstance(obligation, dict):
        raise AcceptanceRuntimeContextError("claimed delivery obligation is missing")
    _verify_claim_rows(
        target,
        lane,
        attempt=attempt,
        obligation=obligation,
        delivery_attempt_id=str(delivery_attempt_id),
    )
    return AcceptanceAttemptContext(
        contract=ACCEPTANCE_ATTEMPT_CONTEXT_CONTRACT,
        schema_version=ACCEPTANCE_ATTEMPT_CONTEXT_SCHEMA_VERSION,
        run_id=lane.run_id,
        plan_sha256=lane.plan_sha256,
        case_id=target.case_id,
        fault_id=target.fault_id,
        fault_scenario=target.fault_scenario,
        canonical_path=target.canonical_path,
        obligation_id=target.obligation_id,
        delivery_attempt_id=str(delivery_attempt_id),
        attempt_number=int(attempt["attempt_number"]),
        started_at=float(attempt["started_at"]),
    )


def serialize_acceptance_attempt_context(context: AcceptanceAttemptContext) -> str:
    payload = asdict(context)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(serialized.encode("utf-8")) > _MAX_SERIALIZED_CONTEXT_BYTES:
        raise AcceptanceRuntimeContextError("acceptance attempt context exceeds safety limit")
    return serialized


def load_and_verify_acceptance_attempt_context(
    config: Any,
    video: str | Path,
    *,
    serialized: str | None = None,
) -> AcceptanceAttemptContext | None:
    """Child-side read-only verification before any Worker processing starts."""

    lane = load_acceptance_queue_lane(config)
    if lane is None:
        return None
    encoded = (
        os.environ.get(ACCEPTANCE_ATTEMPT_CONTEXT_ENV, "")
        if serialized is None
        else serialized
    )
    context = _parse_context(encoded)
    target = _target_for_context(lane, video, context)
    database = scan_state_path(config).resolve()
    if not database.is_file():
        raise AcceptanceRuntimeContextError(
            f"acceptance ledger is unavailable to isolated child: {database}"
        )
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
    except sqlite3.Error as exc:
        raise AcceptanceRuntimeContextError(
            f"acceptance ledger cannot be opened read-only: {database}"
        ) from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        _require_readonly_schema(connection)
        connection.execute("BEGIN")
        attempt = connection.execute(
            """
            SELECT attempt_id, obligation_id, acceptance_run_id, attempt_number,
                   status, started_at
            FROM ai_delivery_attempts
            WHERE attempt_id=?
            """,
            (context.delivery_attempt_id,),
        ).fetchone()
        obligation = connection.execute(
            """
            SELECT obligation_id, canonical_path, media_fingerprint, media_size,
                   media_mtime_ns, policy_revision, acceptance_run_id, state,
                   attempt_count
            FROM ai_delivery_obligations
            WHERE obligation_id=?
            """,
            (context.obligation_id,),
        ).fetchone()
        queue_row = connection.execute(
            "SELECT path, mtime_ns, status FROM ai_candidate_queue WHERE path=?",
            (context.canonical_path,),
        ).fetchone()
        if attempt is None or obligation is None or queue_row is None:
            raise AcceptanceRuntimeContextError(
                "acceptance attempt, obligation, or running queue row is missing"
            )
        _verify_claim_rows(
            target,
            lane,
            attempt=dict(attempt),
            obligation=dict(obligation),
            delivery_attempt_id=context.delivery_attempt_id,
        )
        if int(attempt["attempt_number"] or 0) != context.attempt_number:
            raise AcceptanceRuntimeContextError("acceptance attempt number changed")
        if float(attempt["started_at"] or 0) != context.started_at:
            raise AcceptanceRuntimeContextError("acceptance attempt started_at changed")
        if (
            str(queue_row["path"] or "") != target.canonical_path
            or int(queue_row["mtime_ns"] or 0) != target.media_mtime_ns
            or str(queue_row["status"] or "") != "running"
        ):
            raise AcceptanceRuntimeContextError(
                "acceptance queue row is not the exact running target"
            )
    except sqlite3.Error as exc:
        raise AcceptanceRuntimeContextError(
            f"acceptance ledger read-only verification failed: {exc}"
        ) from exc
    finally:
        connection.rollback()
        connection.close()

    current_lane = load_acceptance_queue_lane(config)
    if (
        current_lane is None
        or current_lane.run_id != lane.run_id
        or current_lane.plan_sha256 != lane.plan_sha256
        or current_lane.target_for_path(video) != target
    ):
        raise AcceptanceRuntimeContextError(
            "acceptance plan changed during isolated child verification"
        )
    return context


def _parse_context(serialized: str) -> AcceptanceAttemptContext:
    if not isinstance(serialized, str) or not serialized:
        raise AcceptanceRuntimeContextError(
            "isolated acceptance child requires an attempt context"
        )
    if len(serialized.encode("utf-8")) > _MAX_SERIALIZED_CONTEXT_BYTES:
        raise AcceptanceRuntimeContextError("acceptance attempt context exceeds safety limit")
    try:
        payload = json.loads(serialized, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AcceptanceRuntimeContextError(
            "acceptance attempt context is not strict JSON"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _CONTEXT_FIELDS:
        raise AcceptanceRuntimeContextError("acceptance attempt context fields are invalid")
    context = AcceptanceAttemptContext(
        contract=str(payload.get("contract") or ""),
        schema_version=(
            int(payload["schema_version"])
            if isinstance(payload.get("schema_version"), int)
            and not isinstance(payload.get("schema_version"), bool)
            else 0
        ),
        run_id=str(payload.get("run_id") or ""),
        plan_sha256=str(payload.get("plan_sha256") or ""),
        case_id=str(payload.get("case_id") or ""),
        fault_id=str(payload.get("fault_id") or ""),
        fault_scenario=str(payload.get("fault_scenario") or ""),
        canonical_path=str(payload.get("canonical_path") or ""),
        obligation_id=str(payload.get("obligation_id") or ""),
        delivery_attempt_id=str(payload.get("delivery_attempt_id") or ""),
        attempt_number=(
            int(payload["attempt_number"])
            if isinstance(payload.get("attempt_number"), int)
            and not isinstance(payload.get("attempt_number"), bool)
            else 0
        ),
        started_at=_strict_positive_float(payload.get("started_at")),
    )
    if (
        context.contract != ACCEPTANCE_ATTEMPT_CONTEXT_CONTRACT
        or context.schema_version != ACCEPTANCE_ATTEMPT_CONTEXT_SCHEMA_VERSION
        or not _RUN_ID.fullmatch(context.run_id)
        or not _HEX64.fullmatch(context.plan_sha256)
        or not _SAFE_ID.fullmatch(context.case_id)
        or not _OBLIGATION_ID.fullmatch(context.obligation_id)
        or not _ATTEMPT_ID.fullmatch(context.delivery_attempt_id)
        or context.attempt_number <= 0
        or not Path(context.canonical_path).is_absolute()
        or str(Path(context.canonical_path).resolve()) != context.canonical_path
    ):
        raise AcceptanceRuntimeContextError("acceptance attempt context identity is invalid")
    if bool(context.fault_id) != bool(context.fault_scenario):
        raise AcceptanceRuntimeContextError("acceptance fault identity is incomplete")
    if context.fault_id and (
        not _SAFE_ID.fullmatch(context.fault_id)
        or not _SAFE_ID.fullmatch(context.fault_scenario)
    ):
        raise AcceptanceRuntimeContextError("acceptance fault identity is invalid")
    return context


def _target_for_context(
    lane: AcceptanceQueueLane,
    video: str | Path,
    context: AcceptanceAttemptContext,
) -> AcceptanceQueueTarget:
    target = lane.target_for_path(video)
    if target is None:
        raise AcceptanceRuntimeContextError("isolated child video is not in acceptance plan")
    if (
        context.run_id != lane.run_id
        or context.plan_sha256 != lane.plan_sha256
        or context.canonical_path != target.canonical_path
        or context.obligation_id != target.obligation_id
        or context.case_id != target.case_id
        or context.fault_id != target.fault_id
        or context.fault_scenario != target.fault_scenario
    ):
        raise AcceptanceRuntimeContextError(
            "acceptance attempt context does not match immutable plan identity"
        )
    return target


def _verify_claim_rows(
    target: AcceptanceQueueTarget,
    lane: AcceptanceQueueLane,
    *,
    attempt: dict[str, Any],
    obligation: dict[str, Any],
    delivery_attempt_id: str,
) -> None:
    if (
        str(attempt.get("attempt_id") or "") != delivery_attempt_id
        or str(attempt.get("obligation_id") or "") != target.obligation_id
        or str(attempt.get("acceptance_run_id") or "") != lane.run_id
        or str(attempt.get("status") or "") != "running"
    ):
        raise AcceptanceRuntimeContextError(
            "delivery attempt is not the exact running acceptance attempt"
        )
    attempt_number = int(attempt.get("attempt_number") or 0)
    started_at = _strict_positive_float(attempt.get("started_at"))
    if (
        attempt_number <= 0
        or delivery_attempt_id
        != _stable_attempt_id(target.obligation_id, attempt_number)
        or str(obligation.get("obligation_id") or "") != target.obligation_id
        or str(obligation.get("canonical_path") or "") != target.canonical_path
        or str(obligation.get("media_fingerprint") or "") != target.media_fingerprint
        or int(obligation.get("media_size") or 0) != target.media_size
        or int(obligation.get("media_mtime_ns") or 0) != target.media_mtime_ns
        or str(obligation.get("policy_revision") or "") != target.policy_revision
        or str(obligation.get("acceptance_run_id") or "") != lane.run_id
        or str(obligation.get("state") or "") != "open"
        or int(obligation.get("attempt_count") or 0) != attempt_number
        or started_at <= 0
    ):
        raise AcceptanceRuntimeContextError(
            "delivery obligation does not own the exact running acceptance attempt"
        )


def _require_readonly_schema(connection: sqlite3.Connection) -> None:
    required = {
        "ai_candidate_queue": {"path", "mtime_ns", "status"},
        "ai_delivery_obligations": {
            "obligation_id",
            "canonical_path",
            "media_fingerprint",
            "media_size",
            "media_mtime_ns",
            "policy_revision",
            "acceptance_run_id",
            "state",
            "attempt_count",
        },
        "ai_delivery_attempts": {
            "attempt_id",
            "obligation_id",
            "acceptance_run_id",
            "attempt_number",
            "status",
            "started_at",
        },
    }
    for table, expected in required.items():
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing = sorted(expected - columns)
        if missing:
            raise AcceptanceRuntimeContextError(
                f"acceptance ledger schema is missing {table} columns: {missing}"
            )


def _strict_positive_float(value: Any) -> float:
    if isinstance(value, bool):
        raise AcceptanceRuntimeContextError("acceptance attempt timestamp is invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AcceptanceRuntimeContextError("acceptance attempt timestamp is invalid") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise AcceptanceRuntimeContextError("acceptance attempt timestamp is invalid")
    return parsed


def _stable_attempt_id(obligation_id: str, attempt_number: int) -> str:
    raw = json.dumps(
        {
            "attempt_number": int(attempt_number),
            "obligation_id": str(obligation_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"aiatt_{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")
