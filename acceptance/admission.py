from __future__ import annotations

import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable

from acceptance_queue_lane import (
    ACCEPTANCE_QUEUE_TARGET_COUNT,
    AcceptanceQueueLane,
    AcceptanceQueueTarget,
    load_acceptance_queue_lane,
    verify_acceptance_queue_target_source,
)
from safe_files import sha256_file
from scan_state import AI_DELIVERY_DEADLINE_SECONDS, scan_state_path

from .fresh import fresh_run_claim_path, fresh_run_claim_payload
from .harness import AcceptanceInputError, read_json_object, validate_plan_structure


ADMISSION_SOURCE = "acceptance_fresh_admission"
_ISOLATED_DB_GUIDANCE = (
    "initialize an empty scan-state database with the isolated acceptance config; "
    "never point fresh admission at a production database"
)
_REQUIRED_COLUMNS = {
    "ai_candidate_queue": {
        "path",
        "mtime_ns",
        "status",
        "source",
        "added_at",
        "updated_at",
        "next_retry_at",
    },
    "ai_delivery_obligations": {
        "obligation_id",
        "canonical_path",
        "media_fingerprint",
        "media_size",
        "media_mtime_ns",
        "policy_revision",
        "acceptance_run_id",
        "source",
        "state",
        "eligible_at",
        "due_at",
        "attempt_count",
        "created_at",
        "updated_at",
    },
    "ai_delivery_attempts": {
        "obligation_id",
        "acceptance_run_id",
    },
}


def admit_fresh_plan(
    plan_path: str | Path,
    config: Any,
    *,
    clock: Callable[[], float] = time.time,
    source_verifier: Callable[[AcceptanceQueueTarget, Any], None] = (
        verify_acceptance_queue_target_source
    ),
) -> dict[str, Any]:
    """Atomically admit exactly one immutable schema-v3 acceptance plan.

    This deliberately bypasses library scanning and queue backfill.  All plan,
    claim, source, and absence checks must pass before the fixed 100 queue rows
    and run-bound obligations can commit together.
    """

    if getattr(config, "acceptance_queue_lane_enabled", False) is not True:
        raise AcceptanceInputError(
            "acceptance_queue_lane_enabled=true is required for fresh admission"
        )
    requested_path = Path(plan_path).resolve()
    lane, plan = _validated_lane_contract(requested_path, config)
    admitted_at = max(_positive_clock(clock), float(plan["created_at"]))

    database = scan_state_path(config).resolve()
    if not database.is_file():
        raise AcceptanceInputError(
            "scan-state database must already exist before fresh admission; "
            f"{_ISOLATED_DB_GUIDANCE}: {database}"
        )
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=rw",
            uri=True,
            timeout=30,
        )
    except sqlite3.Error as exc:
        raise AcceptanceInputError(
            f"scan-state database cannot be opened without creation: {database}"
        ) from exc

    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        _require_schema(connection)
        _assert_all_absent(connection, lane)
        for target in lane.targets:
            source_verifier(target, config)
        connection.execute("BEGIN IMMEDIATE")
        _assert_all_absent(connection, lane)
        changes_before = connection.total_changes
        _insert_exact_targets(connection, lane, admitted_at=admitted_at)

        # Recheck immutable inputs after all tentative writes.  A plan, claim,
        # or source replacement during admission rolls the entire transaction back.
        current_lane, _current_plan = _validated_lane_contract(requested_path, config)
        if (
            current_lane.run_id != lane.run_id
            or current_lane.nonce != lane.nonce
            or current_lane.plan_sha256 != lane.plan_sha256
            or current_lane.targets != lane.targets
        ):
            raise AcceptanceInputError("fresh acceptance plan changed during admission")
        for target in current_lane.targets:
            source_verifier(target, config)

        _assert_exact_admission(connection, lane, admitted_at=admitted_at)
        expected_changes = ACCEPTANCE_QUEUE_TARGET_COUNT * 2
        actual_changes = connection.total_changes - changes_before
        if actual_changes != expected_changes:
            raise AcceptanceInputError(
                "fresh admission attempted unexpected database mutations: "
                f"expected={expected_changes} actual={actual_changes}"
            )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise AcceptanceInputError(f"fresh admission transaction failed: {exc}") from exc
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "mode": "admit-fresh-plan",
        "admitted": True,
        "database_mutated": True,
        "transactional": True,
        "run_id": lane.run_id,
        "plan_path": str(lane.plan_path),
        "plan_sha256": lane.plan_sha256,
        "one_use_claim_verified": True,
        "queue_rows_added": ACCEPTANCE_QUEUE_TARGET_COUNT,
        "obligations_added": ACCEPTANCE_QUEUE_TARGET_COUNT,
        "admitted_at": admitted_at,
    }


def _validated_lane_contract(
    requested_path: Path,
    config: Any,
) -> tuple[AcceptanceQueueLane, dict[str, Any]]:
    lane = load_acceptance_queue_lane(config)
    if lane is None:
        raise AcceptanceInputError("fresh acceptance lane is not enabled")
    if lane.plan_path != requested_path:
        raise AcceptanceInputError(
            "--admit-fresh-plan path must exactly match acceptance_queue_lane_plan_path"
        )
    if sha256_file(requested_path) != lane.plan_sha256:
        raise AcceptanceInputError("fresh acceptance plan changed after lane loading")
    plan = read_json_object(requested_path)
    if sha256_file(requested_path) != lane.plan_sha256:
        raise AcceptanceInputError("fresh acceptance plan changed while it was validated")
    structure_errors = validate_plan_structure(plan)
    if structure_errors:
        raise AcceptanceInputError(
            "fresh acceptance plan structure is invalid: " + structure_errors[0]
        )
    if plan.get("run_id") != lane.run_id or plan.get("suite_id") != lane.run_id:
        raise AcceptanceInputError("fresh acceptance plan run_id does not match its lane")
    if plan.get("nonce") != lane.nonce:
        raise AcceptanceInputError("fresh acceptance plan nonce does not match its lane")

    pre_admission = plan.get("pre_admission")
    if not isinstance(pre_admission, dict):
        raise AcceptanceInputError("fresh acceptance plan has no pre_admission contract")
    claim_path = Path(str(pre_admission.get("run_claim_path") or "")).resolve()
    expected_claim_path = fresh_run_claim_path(config, lane.nonce)
    if claim_path != expected_claim_path:
        raise AcceptanceInputError("fresh run claim path does not match its nonce registry")
    claim = read_json_object(claim_path)
    if claim != fresh_run_claim_payload(plan):
        raise AcceptanceInputError("fresh run claim does not exactly match the immutable plan")

    corpus_path = Path(str(pre_admission.get("corpus_manifest_path") or "")).resolve()
    corpus_sha256 = str(pre_admission.get("corpus_manifest_sha256") or "")
    if not corpus_path.is_file() or sha256_file(corpus_path) != corpus_sha256:
        raise AcceptanceInputError("pre-admission corpus manifest is missing or changed")
    return lane, plan


def _require_schema(connection: sqlite3.Connection) -> None:
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing = sorted(required - columns)
        if missing:
            raise AcceptanceInputError(
                "scan-state schema is not ready for fresh admission and will not be migrated; "
                f"{_ISOLATED_DB_GUIDANCE}: {table} missing {missing}"
            )


def _assert_all_absent(
    connection: sqlite3.Connection,
    lane: AcceptanceQueueLane,
) -> None:
    for table in (
        "ai_candidate_queue",
        "ai_delivery_obligations",
        "ai_delivery_attempts",
    ):
        count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count != 0:
            raise AcceptanceInputError(
                "fresh admission requires a globally empty isolated queue/ledger; "
                f"refusing database with {table} rows={count}; {_ISOLATED_DB_GUIDANCE}"
            )
    if connection.execute(
        "SELECT 1 FROM ai_delivery_obligations WHERE acceptance_run_id=? LIMIT 1",
        (lane.run_id,),
    ).fetchone() is not None:
        raise AcceptanceInputError("fresh acceptance run_id was already admitted")
    if connection.execute(
        "SELECT 1 FROM ai_delivery_attempts WHERE acceptance_run_id=? LIMIT 1",
        (lane.run_id,),
    ).fetchone() is not None:
        raise AcceptanceInputError("fresh acceptance run_id already has delivery attempts")
    for target in lane.targets:
        if connection.execute(
            "SELECT 1 FROM ai_candidate_queue WHERE path=? LIMIT 1",
            (target.canonical_path,),
        ).fetchone() is not None:
            raise AcceptanceInputError(
                f"fresh admission requires every queue row absent: {target.canonical_path}"
            )
        if connection.execute(
            """
            SELECT 1
            FROM ai_delivery_obligations
            WHERE obligation_id=? OR canonical_path=?
            LIMIT 1
            """,
            (target.obligation_id, target.canonical_path),
        ).fetchone() is not None:
            raise AcceptanceInputError(
                "fresh admission requires every obligation absent: "
                f"{target.obligation_id}"
            )
        if connection.execute(
            "SELECT 1 FROM ai_delivery_attempts WHERE obligation_id=? LIMIT 1",
            (target.obligation_id,),
        ).fetchone() is not None:
            raise AcceptanceInputError(
                f"fresh admission found an orphan attempt: {target.obligation_id}"
            )


def _insert_exact_targets(
    connection: sqlite3.Connection,
    lane: AcceptanceQueueLane,
    *,
    admitted_at: float,
) -> None:
    due_at = admitted_at + AI_DELIVERY_DEADLINE_SECONDS
    for target in lane.targets:
        connection.execute(
            """
            INSERT INTO ai_delivery_obligations(
                obligation_id, canonical_path, media_fingerprint, media_size,
                media_mtime_ns, policy_revision, acceptance_run_id, source,
                state, eligible_at, due_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                target.obligation_id,
                target.canonical_path,
                target.media_fingerprint,
                target.media_size,
                target.media_mtime_ns,
                target.policy_revision,
                lane.run_id,
                ADMISSION_SOURCE,
                admitted_at,
                due_at,
                admitted_at,
                admitted_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_candidate_queue(
                path, mtime_ns, status, source, added_at, updated_at, next_retry_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, 0)
            """,
            (
                target.canonical_path,
                target.media_mtime_ns,
                ADMISSION_SOURCE,
                admitted_at,
                admitted_at,
            ),
        )


def _assert_exact_admission(
    connection: sqlite3.Connection,
    lane: AcceptanceQueueLane,
    *,
    admitted_at: float,
) -> None:
    due_at = admitted_at + AI_DELIVERY_DEADLINE_SECONDS
    for target in lane.targets:
        queue_row = connection.execute(
            """
            SELECT mtime_ns, status, source, added_at, updated_at, next_retry_at
            FROM ai_candidate_queue
            WHERE path=?
            """,
            (target.canonical_path,),
        ).fetchone()
        if queue_row != (
            target.media_mtime_ns,
            "queued",
            ADMISSION_SOURCE,
            admitted_at,
            admitted_at,
            0.0,
        ):
            raise AcceptanceInputError(
                f"fresh admission queue identity mismatch: {target.canonical_path}"
            )
        obligation_row = connection.execute(
            """
            SELECT canonical_path, media_fingerprint, media_size, media_mtime_ns,
                   policy_revision, acceptance_run_id, source, state, eligible_at,
                   due_at, attempt_count
            FROM ai_delivery_obligations
            WHERE obligation_id=?
            """,
            (target.obligation_id,),
        ).fetchone()
        if obligation_row != (
            target.canonical_path,
            target.media_fingerprint,
            target.media_size,
            target.media_mtime_ns,
            target.policy_revision,
            lane.run_id,
            ADMISSION_SOURCE,
            "open",
            admitted_at,
            due_at,
            0,
        ):
            raise AcceptanceInputError(
                f"fresh admission obligation identity mismatch: {target.obligation_id}"
            )


def _positive_clock(clock: Callable[[], float]) -> float:
    try:
        value = float(clock())
    except (TypeError, ValueError, OverflowError) as exc:
        raise AcceptanceInputError("fresh admission clock must return a timestamp") from exc
    if not math.isfinite(value) or value <= 0:
        raise AcceptanceInputError("fresh admission clock must return a positive timestamp")
    return value
