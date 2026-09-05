"""Durable, bounded M2 production recovery and reconciliation.

The recovery ledger lives in the existing scanner SQLite database.  It never
discovers media by walking the library: every candidate comes from indexed job,
queue, attempt, or checkpoint state that the Worker already owns.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
import uuid


RECOVERY_SCHEMA_VERSION = 1
RECOVERY_CONTRACT = "m2-production-recovery-v1"
RECOVERY_POLICY_VERSION = "m2-version-aware-recovery-v1"
RECOVERY_LANE_VERSION = "m2-single-canary-recovery-lane-v1"

FAILURE_CATEGORIES = frozenset(
    {
        "TRANSIENT",
        "RESOURCE",
        "CODE_VERSION_FIXED",
        "BAD_INPUT",
        "QUALITY_BLOCKED",
        "PERMANENT_SYSTEM_ERROR",
    }
)
RECOVERY_DECISIONS = frozenset(
    {
        "RECOVER_FROM_CHECKPOINT",
        "RETRY_STAGE",
        "REPROCESS_FROM_SAFE_STAGE",
        "REQUEUE_WITH_NEW_RUNTIME",
        "KEEP_NEEDS_REVIEW",
        "KEEP_QUARANTINED",
        "MARK_UNSUPPORTED",
        "KEEP_FAILED",
    }
)
RECOVERABLE_DECISIONS = frozenset(
    {
        "RECOVER_FROM_CHECKPOINT",
        "RETRY_STAGE",
        "REPROCESS_FROM_SAFE_STAGE",
        "REQUEUE_WITH_NEW_RUNTIME",
    }
)
RECOVERY_ACTIVE_STATUSES = frozenset({"READY", "DISPATCHED", "CLAIMED"})
RECOVERY_TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "EXCLUDED", "FAILED", "BLOCKED_NO_PROGRESS"}
)
LANE_STATES = frozenset(
    {"DISABLED", "EMPTY", "CANARY_READY", "CANARY_IN_FLIGHT", "ACTIVE", "PAUSED"}
)

_SAFE_CODE_RE = re.compile(r"[^a-z0-9_.:-]+")
_HEX_TOKEN_RE = re.compile(r"\b(?:[0-9a-f]{16,}|0x[0-9a-f]+)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_SPACE_RE = re.compile(r"\s+")


class RecoveryError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = _safe_code(reason_code, "recovery_error", 160)
        super().__init__(self.reason_code)


def _safe_code(value: Any, default: str, limit: int = 120) -> str:
    normalized = _SAFE_CODE_RE.sub("_", str(value or "").strip().casefold()).strip("_")
    return (normalized or default)[:limit]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode_json(value: Any, default: Any) -> Any:
    try:
        decoded = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return decoded


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_failure_signature(stage: Any, error_code: Any, detail: Any = "") -> str:
    """Return a stable stage/code signature without paths or volatile ids."""

    normalized_stage = _safe_code(stage, "worker", 80)
    normalized_code = _safe_code(error_code, "unknown_failure", 120)
    if normalized_code not in {"unknown_failure", "worker_unknown"}:
        return f"{normalized_stage}:{normalized_code}"
    text = str(detail or "").casefold()
    text = _HEX_TOKEN_RE.sub("<id>", text)
    text = _NUMBER_RE.sub("<n>", text)
    text = _SPACE_RE.sub(" ", text).strip()[:500]
    detail_digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{normalized_stage}:{normalized_code}:{detail_digest}"


def classify_failure(
    stage: Any,
    error_code: Any,
    detail: Any = "",
    *,
    retry_strategy: Any = "",
    fixed_failure_codes: Iterable[str] = (),
) -> str:
    """Classify one durable failure using a conservative, closed taxonomy."""

    stage_code = _safe_code(stage, "worker", 80)
    code = _safe_code(error_code, "unknown_failure", 120)
    strategy = _safe_code(retry_strategy, "", 80)
    text = " ".join((stage_code, code, strategy, str(detail or "").casefold()))
    fixed = {_safe_code(item, "", 120) for item in fixed_failure_codes}
    if code in fixed:
        return "CODE_VERSION_FIXED"
    if code in {
        "source_selection_needs_review",
        "deterministic_asr_quality",
        "asr_quality_review",
        "subtitle_quality_review",
        "subtitle_quality_unknown",
        "translation_safe_omission",
        "hallucination_blocked",
        "quality_blocked",
    } or strategy == "manual_review" or any(
        marker in text
        for marker in (
            "needs_review",
            "review_required",
            "quality repair exhausted",
            "low confidence",
            "hallucination",
            "non-positive duration",
            "structural qc",
        )
    ):
        return "QUALITY_BLOCKED"
    if code in {"source_unsupported", "unsupported_media", "bad_input"} or any(
        marker in text
        for marker in (
            "unsupported container",
            "unsupported format",
            "no usable audio",
            "no audio stream",
            "cannot decode",
            "corrupt container",
            "invalid data found when processing input",
        )
    ):
        return "BAD_INPUT"
    if code in {
        "transient_oom",
        "transient_resource_killed",
        "insufficient_disk_space",
        "resource_exhausted",
    } or any(
        marker in text
        for marker in (
            "out of memory",
            "cuda failed with error out of memory",
            "returncode=-9",
            "returncode=137",
            "sigkill",
            "no space left on device",
            "insufficient temporary resource",
        )
    ):
        return "RESOURCE"
    if code.startswith("transient_") or strategy in {
        "same_pipeline",
        "lower_memory_same_pipeline",
        "auto_same_pipeline",
        "bounded_retry",
    } or any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "temporary failure",
            "database is locked",
            "connection reset",
            "connection refused",
            "network is unreachable",
            "temporarily unavailable",
            "resource temporarily unavailable",
            "i/o error",
        )
    ):
        return "TRANSIENT"
    return "PERMANENT_SYSTEM_ERROR"


def breaker_streak_eligible(outcome: Mapping[str, Any]) -> bool:
    """Only real runtime failures participate in the multi-job streak."""

    terminal = str(outcome.get("terminal_status") or "").strip().upper()
    if terminal not in {"FAILED", "RETRYING"}:
        return False
    category = classify_failure(
        outcome.get("stage"),
        outcome.get("error_code") or outcome.get("reason_code"),
        outcome.get("_classification_detail") or outcome.get("detail"),
        retry_strategy=outcome.get("retry_strategy"),
    )
    return category not in {"QUALITY_BLOCKED", "BAD_INPUT", "CODE_VERSION_FIXED"}


def _execute_schema(connection: sqlite3.Connection, script: str) -> None:
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            connection.execute(statement)
            pending.clear()
    if "\n".join(pending).strip():
        raise RecoveryError("recovery_schema_script_incomplete")


def ensure_recovery_schema(connection: sqlite3.Connection) -> None:
    """Install the additive recovery ledger without committing caller work."""

    connection.execute("PRAGMA foreign_keys=ON")
    _execute_schema(
        connection,
        """
        CREATE TABLE IF NOT EXISTS m2_recovery_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS m2_recovery_runs (
            run_id TEXT PRIMARY KEY,
            contract TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            policy_version TEXT NOT NULL,
            state TEXT NOT NULL,
            current_worker_version TEXT NOT NULL,
            current_analyzer_version TEXT NOT NULL,
            current_decision_schema_version INTEGER NOT NULL,
            current_checkpoint_schema_version INTEGER NOT NULL,
            started_at REAL NOT NULL,
            completed_at REAL NOT NULL DEFAULT 0,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS m2_recovery_jobs (
            recovery_id TEXT PRIMARY KEY,
            canonical_path TEXT NOT NULL,
            media_mtime_ns INTEGER NOT NULL,
            source_store TEXT NOT NULL,
            previous_state TEXT NOT NULL,
            failure_category TEXT NOT NULL,
            failure_signature TEXT NOT NULL,
            failure_reason TEXT NOT NULL,
            original_worker_version TEXT NOT NULL,
            original_analyzer_version TEXT NOT NULL,
            original_decision_schema_version TEXT NOT NULL,
            original_processing_strategy TEXT NOT NULL,
            original_checkpoint_schema_version TEXT NOT NULL,
            current_worker_version TEXT NOT NULL,
            current_analyzer_version TEXT NOT NULL,
            current_decision_schema_version INTEGER NOT NULL,
            current_processing_strategy TEXT NOT NULL,
            current_checkpoint_schema_version INTEGER NOT NULL,
            version_disposition TEXT NOT NULL DEFAULT '',
            checkpoint_available INTEGER NOT NULL,
            checkpoint_compatible INTEGER NOT NULL DEFAULT 0,
            checkpoint_json TEXT NOT NULL,
            checkpoint_sha256 TEXT NOT NULL,
            recovery_decision TEXT NOT NULL,
            recovery_reason TEXT NOT NULL,
            resume_stage TEXT NOT NULL,
            retry_budget INTEGER NOT NULL,
            recovery_attempt_count INTEGER NOT NULL DEFAULT 0,
            failure_signature_history_json TEXT NOT NULL DEFAULT '[]',
            last_recovery_version TEXT NOT NULL DEFAULT '',
            no_progress_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL,
            claim_attempt_id TEXT NOT NULL DEFAULT '',
            claim_checkpoint_sha256 TEXT NOT NULL DEFAULT '',
            not_before REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(canonical_path, media_mtime_ns)
        );

        CREATE INDEX IF NOT EXISTS idx_m2_recovery_jobs_status_priority
            ON m2_recovery_jobs(status, priority, not_before, created_at, recovery_id);
        CREATE INDEX IF NOT EXISTS idx_m2_recovery_jobs_path
            ON m2_recovery_jobs(canonical_path, updated_at DESC);

        CREATE TABLE IF NOT EXISTS m2_recovery_events (
            event_id TEXT PRIMARY KEY,
            event_key TEXT NOT NULL UNIQUE,
            recovery_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_m2_recovery_events_job_created
            ON m2_recovery_events(recovery_id, created_at, event_id);

        CREATE TRIGGER IF NOT EXISTS trg_m2_recovery_event_no_update
        BEFORE UPDATE ON m2_recovery_events
        BEGIN
            SELECT RAISE(ABORT, 'm2 recovery evidence is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_recovery_event_no_delete
        BEFORE DELETE ON m2_recovery_events
        BEGIN
            SELECT RAISE(ABORT, 'm2 recovery evidence is immutable');
        END;
        """,
    )
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(m2_recovery_jobs)").fetchall()
    }
    if "version_disposition" not in columns:
        connection.execute(
            "ALTER TABLE m2_recovery_jobs ADD COLUMN version_disposition TEXT NOT NULL DEFAULT ''"
        )
    if "checkpoint_compatible" not in columns:
        connection.execute(
            "ALTER TABLE m2_recovery_jobs ADD COLUMN checkpoint_compatible INTEGER NOT NULL DEFAULT 0"
        )
    version_row = connection.execute(
        "SELECT value FROM m2_recovery_meta WHERE key='schema_version'"
    ).fetchone()
    if version_row is not None:
        try:
            stored_version = int(version_row[0])
        except (TypeError, ValueError) as exc:
            raise RecoveryError("recovery_schema_version_malformed") from exc
        if stored_version < 0 or stored_version > RECOVERY_SCHEMA_VERSION:
            raise RecoveryError("recovery_schema_version_unsupported")
    now = time.time()
    for key, value in (
        ("schema_version", str(RECOVERY_SCHEMA_VERSION)),
        ("policy_version", RECOVERY_POLICY_VERSION),
        ("lane_version", RECOVERY_LANE_VERSION),
        ("lane_state", "EMPTY"),
        ("current_worker_version", ""),
        ("last_dispatch_at", "0"),
        ("last_run_id", ""),
    ):
        connection.execute(
            """
            INSERT INTO m2_recovery_meta(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                value=CASE
                    WHEN m2_recovery_meta.key IN ('schema_version','policy_version','lane_version')
                    THEN excluded.value ELSE m2_recovery_meta.value END,
                updated_at=excluded.updated_at
            """,
            (key, value, now),
        )


def _meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute(
        "SELECT value FROM m2_recovery_meta WHERE key=?", (str(key),)
    ).fetchone()
    return str(row[0]) if row is not None else default


def _set_meta(connection: sqlite3.Connection, key: str, value: Any, now: float) -> None:
    connection.execute(
        """
        INSERT INTO m2_recovery_meta(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """,
        (str(key), str(value), float(now)),
    )


def _record_event(
    connection: sqlite3.Connection,
    *,
    event_key: str,
    recovery_id: str,
    run_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    now: float,
) -> None:
    event_id = "m2recevt_" + uuid.uuid4().hex
    connection.execute(
        """
        INSERT OR IGNORE INTO m2_recovery_events(
            event_id,event_key,recovery_id,run_id,event_type,payload_json,created_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            event_id,
            str(event_key)[:300],
            str(recovery_id),
            str(run_id),
            _safe_code(event_type, "event", 80).upper(),
            _json(dict(payload)),
            float(now),
        ),
    )


def _candidate_state(row: Mapping[str, Any], *, stale: bool = False) -> str:
    queue_status = str(row.get("queue_status") or row.get("status") or "").casefold()
    code = _safe_code(row.get("error_code") or row.get("last_error_code"), "", 120)
    source = str(row.get("source") or "").casefold()
    if stale:
        return "RUNNING"
    if queue_status == "failed_retry":
        return "RETRYING"
    if queue_status == "paused":
        if "quarantine" in source or "quarantine" in code:
            return "QUARANTINED"
        return "NEEDS_REVIEW"
    return str(row.get("pipeline_state") or row.get("previous_state") or "FAILED").upper()


def _latest_pipeline_evidence(
    connection: sqlite3.Connection,
    canonical_path: str,
    media_mtime_ns: int,
) -> dict[str, Any]:
    cursor = connection.execute(
        """
        SELECT p.job_id,p.state,p.retry_count,p.resume_state,p.terminal_reason_code,
               p.terminal_error_json,p.updated_at
        FROM pipeline_jobs p
        WHERE p.canonical_path=?
          AND (?<=0 OR p.media_mtime_ns=?)
        ORDER BY p.updated_at DESC LIMIT 1
        """,
        (str(canonical_path), int(media_mtime_ns), int(media_mtime_ns)),
    )
    columns = [str(item[0]) for item in cursor.description or ()]
    raw = cursor.fetchone()
    job = dict(zip(columns, raw, strict=True)) if raw is not None else {}
    if not job:
        return {}
    attempt_cursor = connection.execute(
        """
        SELECT * FROM pipeline_stage_attempts
        WHERE job_id=?
        ORDER BY updated_at DESC,attempt_number DESC LIMIT 1
        """,
        (str(job["job_id"]),),
    )
    attempt_columns = [str(item[0]) for item in attempt_cursor.description or ()]
    attempt_raw = attempt_cursor.fetchone()
    attempt = (
        dict(zip(attempt_columns, attempt_raw, strict=True))
        if attempt_raw is not None
        else {}
    )
    checkpoint_cursor = connection.execute(
        """
        SELECT * FROM pipeline_stage_attempts
        WHERE job_id=? AND checkpoint_sha256<>'' AND checkpoint_json<>'{}'
        ORDER BY updated_at DESC,attempt_number DESC LIMIT 1
        """,
        (str(job["job_id"]),),
    )
    checkpoint_columns = [str(item[0]) for item in checkpoint_cursor.description or ()]
    checkpoint_raw = checkpoint_cursor.fetchone()
    checkpoint_attempt = (
        dict(zip(checkpoint_columns, checkpoint_raw, strict=True))
        if checkpoint_raw is not None
        else {}
    )
    decision_cursor = connection.execute(
        """
        SELECT analyzer_version,decision_schema_version,decision_version,strategy,created_at
        FROM pipeline_source_decisions
        WHERE job_id=? ORDER BY created_at DESC,decision_id DESC LIMIT 1
        """,
        (str(job["job_id"]),),
    )
    decision_raw = decision_cursor.fetchone()
    decision = (
        {
            "analyzer_version": str(decision_raw[0] or ""),
            "decision_schema_version": str(decision_raw[1] or ""),
            "decision_version": str(decision_raw[2] or ""),
            "strategy": str(decision_raw[3] or ""),
            "created_at": float(decision_raw[4] or 0),
        }
        if decision_raw is not None
        else {}
    )
    return {
        "job": job,
        "attempt": attempt,
        "checkpoint_attempt": checkpoint_attempt,
        "decision": decision,
    }


def _original_worker_version(
    connection: sqlite3.Connection,
    canonical_path: str,
) -> str:
    row = connection.execute(
        """
        SELECT o.obligation_id
        FROM ai_delivery_obligations o
        WHERE o.canonical_path=? ORDER BY o.updated_at DESC LIMIT 1
        """,
        (str(canonical_path),),
    ).fetchone()
    if row is None:
        return "unknown"
    job_hash = hashlib.sha256(str(row[0]).encode("utf-8")).hexdigest()
    gate = connection.execute(
        """
        SELECT g.worker_sha
        FROM m2_observation_result_events e
        JOIN m2_observation_gates g ON g.gate_id=e.gate_id
        WHERE e.job_id=? ORDER BY e.created_at DESC LIMIT 1
        """,
        (job_hash,),
    ).fetchone()
    return str(gate[0]) if gate is not None and str(gate[0] or "") else "unknown"


def _resume_stage(previous_state: str, pipeline: Mapping[str, Any]) -> str:
    job = pipeline.get("job") if isinstance(pipeline.get("job"), Mapping) else {}
    attempt = pipeline.get("attempt") if isinstance(pipeline.get("attempt"), Mapping) else {}
    resume = str(job.get("resume_state") or "").upper()
    stage = str(attempt.get("stage") or "").upper()
    status = str(attempt.get("status") or "").upper()
    if resume:
        return resume
    if stage:
        if status == "SUCCEEDED":
            next_stage = {
                "SUBTITLE_DETECTION": "ASR",
                "ASR": "TRANSLATING",
                "TRANSLATING": "POST_PROCESSING",
                "POST_PROCESSING": "QC",
                "QC": "MUXING",
            }.get(stage)
            if next_stage:
                return next_stage
        return stage
    return {
        "RUNNING": "SUBTITLE_DETECTION",
        "RETRYING": "SUBTITLE_DETECTION",
        "NEEDS_REVIEW": "SUBTITLE_DETECTION",
        "QUARANTINED": "SUBTITLE_DETECTION",
    }.get(str(previous_state).upper(), "SUBTITLE_DETECTION")


def recovery_decision(
    category: str,
    previous_state: str,
    *,
    checkpoint_available: bool,
    resume_stage: str,
    deterministic_review_fix: bool = False,
) -> tuple[str, str, int]:
    normalized = str(category).upper()
    prior = str(previous_state).upper()
    if normalized == "QUALITY_BLOCKED" and not deterministic_review_fix:
        return "KEEP_NEEDS_REVIEW", "quality_evidence_requires_review", 60
    if normalized == "BAD_INPUT":
        if "UNSUPPORTED" in str(resume_stage).upper():
            return "MARK_UNSUPPORTED", "input_is_explicitly_unsupported", 70
        return "KEEP_QUARANTINED", "bad_input_is_not_retryable", 70
    if normalized == "PERMANENT_SYSTEM_ERROR":
        return "KEEP_FAILED", "no_safe_automatic_recovery_proven", 80
    if normalized == "CODE_VERSION_FIXED":
        if checkpoint_available:
            return "REPROCESS_FROM_SAFE_STAGE", "old_runtime_failure_fixed_checkpoint_reused", 30
        return "REQUEUE_WITH_NEW_RUNTIME", "old_runtime_failure_fixed", 30
    if normalized in {"TRANSIENT", "RESOURCE"}:
        if checkpoint_available:
            return "RECOVER_FROM_CHECKPOINT", "valid_durable_checkpoint_available", 10 if prior == "RUNNING" else 20
        return "RETRY_STAGE", "bounded_stage_retry", 10 if prior == "RUNNING" else 20
    raise RecoveryError("unknown_failure_category")


def _checkpoint_payload(pipeline: Mapping[str, Any]) -> tuple[bool, dict[str, Any], str]:
    attempt = (
        pipeline.get("checkpoint_attempt")
        if isinstance(pipeline.get("checkpoint_attempt"), Mapping)
        else pipeline.get("attempt")
        if isinstance(pipeline.get("attempt"), Mapping)
        else {}
    )
    raw = str(attempt.get("checkpoint_json") or "{}")
    decoded = _decode_json(raw, {})
    digest = str(attempt.get("checkpoint_sha256") or "")
    valid = (
        isinstance(decoded, dict)
        and bool(decoded)
        and raw == _json(decoded)
        and bool(re.fullmatch(r"[0-9a-f]{64}", digest))
        and hashlib.sha256(raw.encode("utf-8")).hexdigest() == digest
    )
    return valid, decoded if isinstance(decoded, dict) else {}, digest if valid else ""


def _checkpoint_compatible(
    checkpoint: Mapping[str, Any],
    *,
    resume_stage: str,
    current_decision_schema_version: int,
    current_checkpoint_schema_version: int,
) -> bool:
    try:
        schema = int(checkpoint.get("schema_version"))
    except (TypeError, ValueError):
        return False
    stage = str(resume_stage or "").upper()
    if stage == "TRANSLATING":
        from translation_checkpoint import TRANSLATION_CHECKPOINT_SCHEMA_VERSION

        return schema == TRANSLATION_CHECKPOINT_SCHEMA_VERSION
    if stage == "ASR":
        from asr_review_checkpoint import ASR_REVIEW_CHECKPOINT_SUPPORTED_SCHEMA_VERSIONS

        return schema in ASR_REVIEW_CHECKPOINT_SUPPORTED_SCHEMA_VERSIONS
    if stage == "SUBTITLE_DETECTION":
        return schema == int(current_decision_schema_version)
    return schema == int(current_checkpoint_schema_version)


def _recovery_id(path: str, media_mtime_ns: int) -> str:
    raw = _json({"path": str(path), "media_mtime_ns": int(media_mtime_ns)})
    return "m2rec_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _recover_stale_runtime_state(
    connection: sqlite3.Connection,
    *,
    stale_before: float,
    now: float,
) -> dict[str, int]:
    """Terminalize only indexed, cutoff-proven stale attempts and keep checkpoints."""

    from pipeline_state import PipelineJobStore

    pipeline_store = PipelineJobStore(connection=connection, owns_connection=False)
    recovered_pipeline = pipeline_store.recover_interrupted_stages(
        now=now,
        stale_before=stale_before,
        recover_all_running=False,
    )
    stale_paths: dict[str, int] = {}
    for item in recovered_pipeline:
        row = connection.execute(
            "SELECT canonical_path,media_mtime_ns FROM pipeline_jobs WHERE job_id=?",
            (str(item.get("job_id") or ""),),
        ).fetchone()
        if row is not None:
            stale_paths[str(row[0])] = int(row[1] or 0)
    delivery_rows = connection.execute(
        """
        SELECT a.attempt_id,o.canonical_path,o.media_mtime_ns
        FROM ai_delivery_attempts a
        JOIN ai_delivery_obligations o ON o.obligation_id=a.obligation_id
        LEFT JOIN ai_candidate_queue q ON q.path=o.canonical_path
        WHERE a.status='running' AND a.updated_at<=? AND o.state='open'
          AND COALESCE(q.status,'')<>'done'
        ORDER BY a.updated_at,a.attempt_id
        """,
        (float(stale_before),),
    ).fetchall()
    for attempt_id, path, mtime_ns in delivery_rows:
        connection.execute(
            """
            UPDATE ai_delivery_attempts
            SET status='retryable_failure',
                stage=CASE WHEN stage='' THEN 'worker_recovery' ELSE stage END,
                error_code='worker_interrupted',
                detail='Stale running attempt recovered from durable state',
                finished_at=?,updated_at=?
            WHERE attempt_id=? AND status='running' AND updated_at<=?
            """,
            (float(now), float(now), str(attempt_id), float(stale_before)),
        )
        stale_paths[str(path)] = int(mtime_ns or 0)
    return stale_paths


def _indexed_candidates(
    connection: sqlite3.Connection,
    stale_before: float,
    *,
    stale_paths: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        """
        SELECT q.path,q.mtime_ns,q.status AS queue_status,q.source,q.attempts,
               q.running_at,q.last_error,q.last_error_at,q.last_error_code,
               q.retry_strategy,q.failure_revision,q.next_retry_at,q.updated_at,
               j.stage AS job_stage,j.status AS job_status,j.message AS job_message,
               j.updated_at AS job_updated_at
        FROM ai_candidate_queue q
        LEFT JOIN ai_job_state j ON j.path=q.path
        WHERE q.status IN ('failed_retry','paused')
           OR (q.status='running' AND COALESCE(j.updated_at,q.updated_at,q.running_at,0)<=?)
        ORDER BY q.status,q.updated_at,q.path COLLATE NOCASE
        """,
        (float(stale_before),),
    )
    columns = [str(item[0]) for item in cursor.description or ()]
    candidates = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    stale_index = {str(path): int(mtime) for path, mtime in (stale_paths or {}).items()}
    for item in candidates:
        if str(item.get("path") or "") in stale_index:
            item["stale_recovered"] = 1
    seen = {str(item.get("path") or "") for item in candidates}
    pipeline_cursor = connection.execute(
        """
        SELECT canonical_path AS path,media_mtime_ns,state AS pipeline_state,
               retry_count,resume_state,terminal_reason_code,terminal_error_json,
               updated_at,1 AS stale_pipeline
        FROM pipeline_jobs
        WHERE state IN ('FAILED','RETRYING','QUARANTINED','NEEDS_REVIEW')
           OR (state IN ('ANALYZING','SUBTITLE_DETECTION','ASR','TRANSLATING',
                         'POST_PROCESSING','QC','MUXING') AND updated_at<=?)
        ORDER BY updated_at,canonical_path COLLATE NOCASE
        """,
        (float(stale_before),),
    )
    pipeline_columns = [str(item[0]) for item in pipeline_cursor.description or ()]
    for raw in pipeline_cursor.fetchall():
        item = dict(zip(pipeline_columns, raw, strict=True))
        path = str(item.get("path") or "")
        if path and path not in seen:
            item["queue_status"] = ""
            if path in stale_index:
                item["stale_recovered"] = 1
            candidates.append(item)
            seen.add(path)
    legacy_cursor = connection.execute(
        """
        SELECT j.path,COALESCE(v.mtime_ns,0) AS mtime_ns,'' AS queue_status,
               j.stage AS job_stage,j.status AS job_status,j.message AS job_message,
               j.updated_at AS job_updated_at,'FAILED' AS previous_state
        FROM ai_job_state j
        LEFT JOIN video_scan_cache v ON v.path=j.path
        WHERE j.status='failed'
          AND NOT EXISTS (SELECT 1 FROM ai_candidate_queue q WHERE q.path=j.path)
          AND NOT EXISTS (SELECT 1 FROM pipeline_jobs p WHERE p.canonical_path=j.path)
        ORDER BY j.updated_at,j.path COLLATE NOCASE
        """
    )
    legacy_columns = [str(item[0]) for item in legacy_cursor.description or ()]
    for raw in legacy_cursor.fetchall():
        item = dict(zip(legacy_columns, raw, strict=True))
        path = str(item.get("path") or "")
        if path and path not in seen:
            candidates.append(item)
            seen.add(path)
    for path, mtime_ns in stale_index.items():
        if path and path not in seen:
            candidates.append(
                {
                    "path": path,
                    "mtime_ns": int(mtime_ns),
                    "queue_status": "",
                    "previous_state": "RUNNING",
                    "stale_recovered": 1,
                    "last_error_code": "worker_interrupted",
                    "last_error": "Stale running attempt recovered from durable state",
                }
            )
            seen.add(path)
    return candidates


def _failure_fields(row: Mapping[str, Any], pipeline: Mapping[str, Any]) -> tuple[str, str, str]:
    attempt = pipeline.get("attempt") if isinstance(pipeline.get("attempt"), Mapping) else {}
    job = pipeline.get("job") if isinstance(pipeline.get("job"), Mapping) else {}
    stage = str(
        row.get("job_stage")
        or attempt.get("stage")
        or job.get("resume_state")
        or "worker"
    )
    code = str(
        row.get("last_error_code")
        or attempt.get("error_code")
        or row.get("terminal_reason_code")
        or job.get("terminal_reason_code")
        or "unknown_failure"
    )
    attempt_error = _decode_json(attempt.get("error_json"), {})
    terminal_error = _decode_json(
        row.get("terminal_error_json") or job.get("terminal_error_json"), {}
    )
    detail = str(
        row.get("last_error")
        or row.get("job_message")
        or (attempt_error.get("message") if isinstance(attempt_error, Mapping) else "")
        or (terminal_error.get("message") if isinstance(terminal_error, Mapping) else "")
        or code
    )[:1000]
    return stage, code, detail


def _apply_non_retry_decision(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    decision: str,
    *,
    failure_code: str,
    now: float,
) -> None:
    path = str(row.get("path") or "")
    queue_status = str(row.get("queue_status") or "")
    if decision == "KEEP_NEEDS_REVIEW" and queue_status == "failed_retry":
        connection.execute(
            """
            UPDATE ai_candidate_queue
            SET status='paused',source='m2_recovery_review',running_at=0,next_retry_at=0,
                retry_strategy='manual_review',updated_at=?
            WHERE path=? AND status='failed_retry' AND last_error_code=?
            """,
            (float(now), path, str(failure_code)),
        )
    elif decision in {"MARK_UNSUPPORTED", "KEEP_QUARANTINED"} and queue_status in {
        "failed_retry",
        "paused",
    }:
        connection.execute(
            """
            UPDATE ai_candidate_queue
            SET status='paused',source='m2_recovery_quarantine',running_at=0,next_retry_at=0,
                retry_strategy='permanent',updated_at=?
            WHERE path=? AND status IN ('failed_retry','paused')
            """,
            (float(now), path),
        )


def _prepare_stale_running(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    now: float,
) -> None:
    """Move one already-proven stale queue row to the bounded recovery lane."""

    path = str(row.get("path") or "")
    changed = connection.execute(
        """
        UPDATE ai_candidate_queue
        SET status='failed_retry',source='m2_recovery_interrupted',running_at=0,
            last_error='Worker interruption recovered from durable state',
            last_error_at=?,last_error_code='worker_interrupted',
            retry_strategy='bounded_retry',next_retry_at=0,updated_at=?
        WHERE path=? AND status='running'
        """,
        (float(now), float(now), path),
    ).rowcount
    if int(changed or 0) == 1:
        connection.execute(
            """
            UPDATE ai_job_state
            SET status='failed',message='Worker interruption queued for M2 recovery',
                updated_at=?,finished_at=?
            WHERE path=? AND status='running'
            """,
            (float(now), float(now), path),
        )


def reconcile_historical_jobs(
    connection: sqlite3.Connection,
    *,
    current_worker_version: str,
    current_analyzer_version: str,
    current_decision_schema_version: int,
    current_checkpoint_schema_version: int,
    retry_budget: int = 2,
    stale_after_seconds: int = 900,
    fixed_failure_codes: Iterable[str] = (),
    deterministic_review_codes: Iterable[str] = (),
    now: float | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify indexed historical state and persist one idempotent recovery run."""

    ensure_recovery_schema(connection)
    timestamp = float(time.time() if now is None else now)
    worker_version = str(current_worker_version or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{40}", worker_version):
        raise RecoveryError("current_worker_version_invalid")
    budget = max(1, min(10, int(retry_budget or 0)))
    fixed_codes = {_safe_code(code, "", 120) for code in fixed_failure_codes}
    deterministic_codes = {
        _safe_code(code, "", 120) for code in deterministic_review_codes
    }
    run_id = "m2recrun_" + uuid.uuid4().hex
    connection.execute(
        """
        INSERT INTO m2_recovery_runs(
            run_id,contract,schema_version,policy_version,state,current_worker_version,
            current_analyzer_version,current_decision_schema_version,
            current_checkpoint_schema_version,started_at,evidence_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            RECOVERY_CONTRACT,
            RECOVERY_SCHEMA_VERSION,
            RECOVERY_POLICY_VERSION,
            "RUNNING",
            worker_version,
            str(current_analyzer_version or "unknown"),
            int(current_decision_schema_version),
            int(current_checkpoint_schema_version),
            timestamp,
            _json(dict(evidence or {})),
        ),
    )
    stale_before = timestamp - max(60, int(stale_after_seconds or 0))
    stale_paths = _recover_stale_runtime_state(
        connection,
        stale_before=stale_before,
        now=timestamp,
    )
    candidates = _indexed_candidates(
        connection,
        stale_before,
        stale_paths=stale_paths,
    )
    metrics = {
        "historical_failed_total": 0,
        "historical_retrying_total": 0,
        "stale_running_total": 0,
        "historical_quarantined_total": 0,
        "historical_needs_review_total": 0,
        "recoverable_total": 0,
        "recoverable_by_new_runtime": 0,
        "recovered_from_checkpoint": 0,
        "requeued_for_stage_retry": 0,
        "permanently_failed": 0,
        "kept_needs_review": 0,
        "kept_quarantined": 0,
        "unsupported": 0,
        "recovery_success": 0,
        "recovery_failed": 0,
        "repeated_no_progress_blocked": 0,
    }
    for row in candidates:
        path = str(row.get("path") or "")
        mtime_ns = int(row.get("mtime_ns") or 0)
        pipeline = _latest_pipeline_evidence(connection, path, mtime_ns)
        pipeline_state = str(row.get("pipeline_state") or "").upper()
        stale = bool(row.get("stale_recovered")) or str(
            row.get("queue_status") or ""
        ).casefold() == "running" or (
            bool(row.get("stale_pipeline"))
            and pipeline_state
            in {
                "ANALYZING",
                "SUBTITLE_DETECTION",
                "ASR",
                "TRANSLATING",
                "POST_PROCESSING",
                "QC",
                "MUXING",
            }
        )
        previous_state = _candidate_state(row, stale=stale)
        metric_key = {
            "FAILED": "historical_failed_total",
            "RETRYING": "historical_retrying_total",
            "RUNNING": "stale_running_total",
            "QUARANTINED": "historical_quarantined_total",
            "NEEDS_REVIEW": "historical_needs_review_total",
        }.get(previous_state)
        if metric_key:
            metrics[metric_key] += 1
        stage, code, detail = _failure_fields(row, pipeline)
        category = (
            "TRANSIENT"
            if previous_state == "RUNNING"
            else classify_failure(
                stage,
                code,
                detail,
                retry_strategy=row.get("retry_strategy"),
                fixed_failure_codes=fixed_codes,
            )
        )
        checkpoint_available, checkpoint, checkpoint_sha = _checkpoint_payload(pipeline)
        resume_stage = _resume_stage(previous_state, pipeline)
        checkpoint_compatible = checkpoint_available and _checkpoint_compatible(
            checkpoint,
            resume_stage=resume_stage,
            current_decision_schema_version=int(current_decision_schema_version),
            current_checkpoint_schema_version=int(current_checkpoint_schema_version),
        )
        decision, reason, priority = recovery_decision(
            category,
            previous_state,
            checkpoint_available=checkpoint_compatible,
            resume_stage=resume_stage,
            deterministic_review_fix=_safe_code(code, "", 120) in deterministic_codes,
        )
        if decision in RECOVERABLE_DECISIONS:
            metrics["recoverable_total"] += 1
        if category == "CODE_VERSION_FIXED" and decision in RECOVERABLE_DECISIONS:
            metrics["recoverable_by_new_runtime"] += 1
        if decision == "KEEP_FAILED":
            metrics["permanently_failed"] += 1
        elif decision == "KEEP_NEEDS_REVIEW":
            metrics["kept_needs_review"] += 1
        elif decision == "KEEP_QUARANTINED":
            metrics["kept_quarantined"] += 1
        elif decision == "MARK_UNSUPPORTED":
            metrics["unsupported"] += 1
        recovery_id = _recovery_id(path, mtime_ns)
        attempt = pipeline.get("attempt") if isinstance(pipeline.get("attempt"), Mapping) else {}
        source_decision = (
            pipeline.get("decision")
            if isinstance(pipeline.get("decision"), Mapping)
            else {}
        )
        original_worker = _original_worker_version(connection, path)
        original_analyzer = str(
            source_decision.get("analyzer_version")
            or checkpoint.get("analyzer_version")
            or "unknown"
        )
        original_schema = str(
            source_decision.get("decision_schema_version")
            or checkpoint.get("decision_schema_version")
            or "unknown"
        )
        original_strategy = str(source_decision.get("strategy") or "UNREPORTED")
        original_checkpoint_schema = str(
            checkpoint.get("schema_version") or "unknown"
        )
        version_disposition = (
            "RECOVERABLE_BY_NEW_RUNTIME"
            if category == "CODE_VERSION_FIXED"
            and original_worker != worker_version
            else "CURRENT_RUNTIME_OR_UNPROVEN"
        )
        processing_strategy = (
            "lower_memory_same_pipeline"
            if category == "RESOURCE"
            else "checkpoint_resume"
            if checkpoint_compatible
            else "bounded_stage_retry"
            if decision in RECOVERABLE_DECISIONS
            else "no_automatic_reprocessing"
        )
        existing_cursor = connection.execute(
            "SELECT status,recovery_attempt_count,failure_signature_history_json,"
            "last_recovery_version,no_progress_count,created_at,current_worker_version "
            "FROM m2_recovery_jobs WHERE recovery_id=?",
            (recovery_id,),
        )
        existing_row = existing_cursor.fetchone()
        signature = normalize_failure_signature(stage, code, detail)
        if existing_row is None:
            status = "READY" if decision in RECOVERABLE_DECISIONS else "EXCLUDED"
            recovery_attempt_count = 0
            history = [
                {
                    "signature": signature,
                    "runtime_version": original_worker,
                    "observed_at": float(row.get("last_error_at") or row.get("updated_at") or timestamp),
                }
            ]
            last_recovery_version = ""
            no_progress_count = 0
            created_at = timestamp
        else:
            status = str(existing_row[0])
            if status not in RECOVERY_TERMINAL_STATUSES | {"DISPATCHED", "CLAIMED"}:
                status = "READY" if decision in RECOVERABLE_DECISIONS else "EXCLUDED"
            recovery_attempt_count = int(existing_row[1] or 0)
            history = _decode_json(existing_row[2], [])
            if not isinstance(history, list):
                history = []
            last_recovery_version = str(existing_row[3] or "")
            no_progress_count = int(existing_row[4] or 0)
            created_at = float(existing_row[5] or timestamp)
            prior_current_worker = str(existing_row[6] or "")
            if (
                decision in RECOVERABLE_DECISIONS
                and status in {"EXCLUDED", "FAILED", "BLOCKED_NO_PROGRESS"}
                and prior_current_worker
                and prior_current_worker != worker_version
            ):
                status = "READY"
                no_progress_count = 0
        connection.execute(
            """
            INSERT INTO m2_recovery_jobs(
                recovery_id,canonical_path,media_mtime_ns,source_store,previous_state,
                failure_category,failure_signature,failure_reason,
                original_worker_version,original_analyzer_version,
                original_decision_schema_version,original_processing_strategy,
                original_checkpoint_schema_version,current_worker_version,
                current_analyzer_version,current_decision_schema_version,
                current_processing_strategy,current_checkpoint_schema_version,
                version_disposition,
                checkpoint_available,checkpoint_compatible,checkpoint_json,checkpoint_sha256,
                recovery_decision,recovery_reason,resume_stage,retry_budget,
                recovery_attempt_count,failure_signature_history_json,
                last_recovery_version,no_progress_count,status,priority,
                claim_attempt_id,claim_checkpoint_sha256,not_before,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(recovery_id) DO UPDATE SET
                source_store=excluded.source_store,previous_state=excluded.previous_state,
                failure_category=excluded.failure_category,
                failure_signature=excluded.failure_signature,
                failure_reason=excluded.failure_reason,
                current_worker_version=excluded.current_worker_version,
                current_analyzer_version=excluded.current_analyzer_version,
                current_decision_schema_version=excluded.current_decision_schema_version,
                current_processing_strategy=excluded.current_processing_strategy,
                current_checkpoint_schema_version=excluded.current_checkpoint_schema_version,
                version_disposition=excluded.version_disposition,
                checkpoint_available=excluded.checkpoint_available,
                checkpoint_compatible=excluded.checkpoint_compatible,
                checkpoint_json=excluded.checkpoint_json,
                checkpoint_sha256=excluded.checkpoint_sha256,
                recovery_decision=excluded.recovery_decision,
                recovery_reason=excluded.recovery_reason,
                resume_stage=excluded.resume_stage,retry_budget=excluded.retry_budget,
                recovery_attempt_count=excluded.recovery_attempt_count,
                failure_signature_history_json=excluded.failure_signature_history_json,
                last_recovery_version=excluded.last_recovery_version,
                no_progress_count=excluded.no_progress_count,status=excluded.status,
                priority=excluded.priority,updated_at=excluded.updated_at
            """,
            (
                recovery_id,
                path,
                mtime_ns,
                "ai_queue" if row.get("queue_status") else "pipeline_jobs",
                previous_state,
                category,
                signature,
                detail,
                original_worker,
                original_analyzer,
                original_schema,
                original_strategy,
                original_checkpoint_schema,
                worker_version,
                str(current_analyzer_version or "unknown"),
                int(current_decision_schema_version),
                processing_strategy,
                int(current_checkpoint_schema_version),
                version_disposition,
                1 if checkpoint_available else 0,
                1 if checkpoint_compatible else 0,
                _json(checkpoint),
                checkpoint_sha,
                decision,
                reason,
                resume_stage,
                budget,
                recovery_attempt_count,
                _json(history[-20:]),
                last_recovery_version,
                no_progress_count,
                status,
                priority,
                "",
                "",
                0.0,
                created_at,
                timestamp,
            ),
        )
        if previous_state == "RUNNING" and decision in RECOVERABLE_DECISIONS:
            _prepare_stale_running(connection, row, now=timestamp)
        _apply_non_retry_decision(
            connection,
            row,
            decision,
            failure_code=code,
            now=timestamp,
        )
        _record_event(
            connection,
            event_key=f"decision:{recovery_id}:{worker_version}:{signature}:{decision}",
            recovery_id=recovery_id,
            run_id=run_id,
            event_type="RECOVERY_DECISION",
            payload={
                "previous_state": previous_state,
                "failure_category": category,
                "failure_signature": signature,
                "original_worker_version": original_worker,
                "current_worker_version": worker_version,
                "version_disposition": version_disposition,
                "checkpoint_available": checkpoint_available,
                "checkpoint_compatible": checkpoint_compatible,
                "recovery_decision": decision,
                "recovery_reason": reason,
                "resume_stage": resume_stage,
                "retry_budget": budget,
                "recovery_attempt_count": recovery_attempt_count,
            },
            now=timestamp,
        )
    ready_count = int(
        connection.execute(
            "SELECT COUNT(1) FROM m2_recovery_jobs WHERE status='READY'"
        ).fetchone()[0]
    )
    inflight_count = int(
        connection.execute(
            "SELECT COUNT(1) FROM m2_recovery_jobs WHERE status IN ('DISPATCHED','CLAIMED')"
        ).fetchone()[0]
    )
    lane_state = _meta(connection, "lane_state", "EMPTY")
    if inflight_count:
        lane_state = "CANARY_IN_FLIGHT" if lane_state.startswith("CANARY") else "ACTIVE"
    elif ready_count and lane_state in {"EMPTY", "DISABLED", "PAUSED"}:
        lane_state = "CANARY_READY"
    elif not ready_count:
        lane_state = "EMPTY"
    _set_meta(connection, "lane_state", lane_state, timestamp)
    _set_meta(connection, "current_worker_version", worker_version, timestamp)
    _set_meta(connection, "last_run_id", run_id, timestamp)
    metrics["ready_count"] = ready_count
    metrics["inflight_count"] = inflight_count
    connection.execute(
        """
        UPDATE m2_recovery_runs
        SET state='COMPLETED',completed_at=?,metrics_json=? WHERE run_id=?
        """,
        (timestamp, _json(metrics), run_id),
    )
    return {
        "contract": RECOVERY_CONTRACT,
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "run_id": run_id,
        "state": "COMPLETED",
        "lane_state": lane_state,
        **metrics,
    }


def _current_checkpoint_sha(connection: sqlite3.Connection, path: str, mtime_ns: int) -> str:
    pipeline = _latest_pipeline_evidence(connection, path, mtime_ns)
    _available, _payload, digest = _checkpoint_payload(pipeline)
    return digest


def dispatch_next_recovery(
    connection: sqlite3.Connection,
    *,
    runtime_status: str,
    dispatch_interval_seconds: int = 300,
    now: float | None = None,
) -> dict[str, Any]:
    """Materialize at most one exact Recovery Queue item into the worker queue."""

    ensure_recovery_schema(connection)
    timestamp = float(time.time() if now is None else now)
    if str(runtime_status).upper() != "ARMED":
        return {"dispatched": False, "reason_code": "runtime_not_armed"}
    lane_state = _meta(connection, "lane_state", "EMPTY")
    if lane_state in {"DISABLED", "EMPTY", "PAUSED"}:
        return {"dispatched": False, "reason_code": f"lane_{lane_state.casefold()}"}
    inflight_rows = connection.execute(
        "SELECT recovery_id,status,canonical_path,media_mtime_ns,claim_attempt_id "
        "FROM m2_recovery_jobs WHERE status IN ('DISPATCHED','CLAIMED') "
        "ORDER BY updated_at LIMIT 2"
    ).fetchall()
    if len(inflight_rows) > 1:
        return {"dispatched": False, "reason_code": "multiple_recovery_items_inflight"}
    if inflight_rows:
        inflight = inflight_rows[0]
        queue = connection.execute(
            "SELECT status,mtime_ns FROM ai_candidate_queue WHERE path=?",
            (str(inflight[2]),),
        ).fetchone()
        if str(inflight[1]) == "DISPATCHED" and not inflight[4] and queue is None:
            # The scanner may remove a not-yet-claimed item because existing
            # subtitles make it ineligible. Never recreate it blindly or
            # report AI completion; retain a reviewable pre-claim exclusion.
            changed = connection.execute(
                """
                UPDATE m2_recovery_jobs
                SET status='EXCLUDED',recovery_decision='KEEP_NEEDS_REVIEW',
                    recovery_reason='dispatched_queue_item_removed_before_claim',
                    updated_at=?
                WHERE recovery_id=? AND status='DISPATCHED' AND claim_attempt_id=''
                  AND NOT EXISTS (SELECT 1 FROM ai_candidate_queue WHERE path=?)
                  AND NOT EXISTS (
                    SELECT 1 FROM ai_job_state WHERE path=? AND status='running'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM ai_delivery_attempts a
                    JOIN ai_delivery_obligations o ON o.obligation_id=a.obligation_id
                    WHERE o.canonical_path=? AND a.status='running'
                  )
                """,
                (timestamp, str(inflight[0]), *(str(inflight[2]),) * 3),
            ).rowcount
            if int(changed or 0) == 1:
                try:
                    actual_mtime = int(Path(str(inflight[2])).stat().st_mtime_ns)
                except OSError:
                    actual_mtime = None
                if lane_state == "CANARY_IN_FLIGHT":
                    _set_meta(connection, "lane_state", "CANARY_READY", timestamp)
                _record_event(
                    connection,
                    event_key=f"preclaim-excluded:{inflight[0]}",
                    recovery_id=str(inflight[0]),
                    run_id=_meta(connection, "last_run_id", ""),
                    event_type="RECOVERY_PRECLAIM_EXCLUDED",
                    payload={
                        "reason_code": "dispatched_queue_item_removed_before_claim",
                        "previous_status": "DISPATCHED",
                        "status": "EXCLUDED",
                        "recovery_decision": "KEEP_NEEDS_REVIEW",
                        "queue_item_present": False,
                        "expected_media_mtime_ns": int(inflight[3]),
                        "observed_media_mtime_ns": actual_mtime,
                        "media_identity_matches": actual_mtime == int(inflight[3]),
                        "new_claims": 0,
                        "completion_verified": False,
                    },
                    now=timestamp,
                )
                return {
                    "dispatched": False,
                    "reason_code": "dispatched_queue_item_removed_before_claim",
                    "recovery_id": str(inflight[0]),
                    "status": "EXCLUDED",
                }
        return {
            "dispatched": False,
            "reason_code": (
                "recovery_queue_identity_mismatch"
                if queue is not None and int(queue[1]) != int(inflight[3])
                else "recovery_inflight"
            ),
            "recovery_id": str(inflight[0]),
            "status": str(inflight[1]),
        }
    if lane_state == "CANARY_IN_FLIGHT":
        return {"dispatched": False, "reason_code": "lane_canary_in_flight"}
    last_dispatch = float(_meta(connection, "last_dispatch_at", "0") or 0)
    if lane_state in {"ACTIVE", "CANARY_READY"} and timestamp < last_dispatch + max(
        60, int(dispatch_interval_seconds or 0)
    ):
        return {"dispatched": False, "reason_code": "recovery_lane_cooldown"}
    cursor = connection.execute(
        """
        SELECT recovery_id,canonical_path,media_mtime_ns,recovery_decision,
               failure_signature,checkpoint_sha256,retry_budget,recovery_attempt_count,
               source_store
        FROM m2_recovery_jobs
        WHERE status='READY' AND not_before<=?
        ORDER BY priority,created_at,recovery_id LIMIT 1
        """,
        (timestamp,),
    )
    columns = [str(item[0]) for item in cursor.description or ()]
    raw = cursor.fetchone()
    if raw is None:
        _set_meta(connection, "lane_state", "EMPTY", timestamp)
        return {"dispatched": False, "reason_code": "recovery_queue_empty"}
    item = dict(zip(columns, raw, strict=True))
    path = str(item["canonical_path"])
    try:
        media_stat = Path(path).stat()
        if int(media_stat.st_mtime_ns) != int(item["media_mtime_ns"] or 0):
            raise OSError("media identity changed")
    except OSError:
        connection.execute(
            "UPDATE m2_recovery_jobs SET status='EXCLUDED',recovery_reason='media_unavailable_or_changed',updated_at=? WHERE recovery_id=? AND status='READY'",
            (timestamp, item["recovery_id"]),
        )
        return {"dispatched": False, "reason_code": "media_unavailable_or_changed"}
    row = connection.execute(
        "SELECT status,mtime_ns,last_error_code,failure_revision FROM ai_candidate_queue WHERE path=?",
        (path,),
    ).fetchone()
    if row is None:
        connection.execute(
            """
            INSERT INTO ai_candidate_queue(
                path,mtime_ns,status,source,attempts,running_at,last_error,
                last_error_at,last_error_code,retry_strategy,failure_revision,
                next_retry_at,force_ai,added_at,updated_at
            ) VALUES(?,?,'paused','m2_recovery_staged',0,0,NULL,NULL,'','','',0,0,?,?)
            """,
            (path, int(item["media_mtime_ns"]), timestamp, timestamp),
        )
        row = ("paused", int(item["media_mtime_ns"]), "", "")
    if int(row[1] or 0) != int(item["media_mtime_ns"] or 0):
        connection.execute(
            "UPDATE m2_recovery_jobs SET status='EXCLUDED',recovery_reason='media_identity_changed',updated_at=? WHERE recovery_id=? AND status='READY'",
            (timestamp, item["recovery_id"]),
        )
        return {"dispatched": False, "reason_code": "media_identity_changed"}
    if str(row[0]) not in {"failed_retry", "paused"}:
        return {"dispatched": False, "reason_code": "queue_state_not_recoverable"}
    updated = connection.execute(
        """
        UPDATE ai_candidate_queue
        SET status='queued',source='m2_recovery',running_at=0,next_retry_at=0,updated_at=?
        WHERE path=? AND mtime_ns=? AND status IN ('failed_retry','paused')
        """,
        (timestamp, path, int(item["media_mtime_ns"])),
    ).rowcount
    if int(updated or 0) != 1:
        return {"dispatched": False, "reason_code": "queue_dispatch_race"}
    connection.execute(
        """
        UPDATE ai_job_state SET stage=?,status='queued',message=?,updated_at=?,finished_at=NULL
        WHERE path=?
        """,
        (
            str(item["recovery_decision"]).casefold(),
            "M2 recovery canary queued from durable decision",
            timestamp,
            path,
        ),
    )
    changed = connection.execute(
        """
        UPDATE m2_recovery_jobs
        SET status='DISPATCHED',not_before=0,updated_at=?
        WHERE recovery_id=? AND status='READY'
        """,
        (timestamp, item["recovery_id"]),
    ).rowcount
    if int(changed or 0) != 1:
        raise RecoveryError("recovery_dispatch_ledger_race")
    _set_meta(
        connection,
        "lane_state",
        "CANARY_IN_FLIGHT" if lane_state == "CANARY_READY" else "ACTIVE",
        timestamp,
    )
    _set_meta(connection, "last_dispatch_at", repr(timestamp), timestamp)
    run_id = _meta(connection, "last_run_id", "")
    _record_event(
        connection,
        event_key=f"dispatch:{item['recovery_id']}:{int(item['recovery_attempt_count']) + 1}",
        recovery_id=str(item["recovery_id"]),
        run_id=run_id,
        event_type="RECOVERY_DISPATCHED",
        payload={
            "recovery_decision": item["recovery_decision"],
            "failure_signature": item["failure_signature"],
            "checkpoint_sha256": item["checkpoint_sha256"],
            "attempt_number": int(item["recovery_attempt_count"]) + 1,
        },
        now=timestamp,
    )
    return {
        "dispatched": True,
        "recovery_id": str(item["recovery_id"]),
        "decision": str(item["recovery_decision"]),
        "canary": lane_state == "CANARY_READY",
    }


def mark_recovery_claimed(
    connection: sqlite3.Connection,
    canonical_path: str | Path,
    delivery_attempt_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    ensure_recovery_schema(connection)
    timestamp = float(time.time() if now is None else now)
    cursor = connection.execute(
        """
        SELECT recovery_id,media_mtime_ns,recovery_attempt_count,retry_budget
        FROM m2_recovery_jobs
        WHERE canonical_path=? AND status='DISPATCHED'
        ORDER BY updated_at DESC LIMIT 1
        """,
        (str(Path(canonical_path).resolve()),),
    )
    raw = cursor.fetchone()
    if raw is None:
        return {"claimed": False, "reason_code": "not_a_recovery_claim"}
    recovery_id, mtime_ns, attempt_count, retry_budget = raw
    worker_version = _meta(connection, "current_worker_version", "unknown")
    checkpoint_sha = _current_checkpoint_sha(
        connection, str(Path(canonical_path).resolve()), int(mtime_ns or 0)
    )
    changed = connection.execute(
        """
        UPDATE m2_recovery_jobs
        SET status='CLAIMED',claim_attempt_id=?,claim_checkpoint_sha256=?,
            recovery_attempt_count=recovery_attempt_count+1,
            last_recovery_version=?,updated_at=?
        WHERE recovery_id=? AND status='DISPATCHED'
        """,
        (
            str(delivery_attempt_id),
            checkpoint_sha,
            worker_version,
            timestamp,
            str(recovery_id),
        ),
    ).rowcount
    if int(changed or 0) != 1:
        raise RecoveryError("recovery_claim_race")
    run_id = _meta(connection, "last_run_id", "")
    _record_event(
        connection,
        event_key=f"claim:{recovery_id}:{delivery_attempt_id}",
        recovery_id=str(recovery_id),
        run_id=run_id,
        event_type="RECOVERY_CLAIMED",
        payload={
            "attempt_id": str(delivery_attempt_id),
            "recovery_attempt_count": int(attempt_count or 0) + 1,
            "retry_budget": int(retry_budget or 0),
            "checkpoint_sha256": checkpoint_sha,
            "runtime_version": worker_version,
        },
        now=timestamp,
    )
    return {"claimed": True, "recovery_id": str(recovery_id)}


def settle_recovery_attempt(
    connection: sqlite3.Connection,
    canonical_path: str | Path,
    delivery_attempt_id: str,
    *,
    retry_after_seconds: int = 300,
    now: float | None = None,
) -> dict[str, Any]:
    ensure_recovery_schema(connection)
    timestamp = float(time.time() if now is None else now)
    cursor = connection.execute(
        """
        SELECT * FROM m2_recovery_jobs
        WHERE canonical_path=? AND status='CLAIMED' AND claim_attempt_id=?
        ORDER BY updated_at DESC LIMIT 1
        """,
        (str(Path(canonical_path).resolve()), str(delivery_attempt_id)),
    )
    columns = [str(item[0]) for item in cursor.description or ()]
    raw = cursor.fetchone()
    if raw is None:
        return {"settled": False, "reason_code": "not_a_recovery_attempt"}
    item = dict(zip(columns, raw, strict=True))
    attempt = connection.execute(
        "SELECT status,stage,error_code,detail FROM ai_delivery_attempts WHERE attempt_id=?",
        (str(delivery_attempt_id),),
    ).fetchone()
    if attempt is None or str(attempt[0]) == "running":
        return {"settled": False, "reason_code": "attempt_not_terminal"}
    attempt_status, stage, code, detail = (str(value or "") for value in attempt)
    run_id = _meta(connection, "last_run_id", "")
    lane_state = _meta(connection, "lane_state", "EMPTY")
    if attempt_status == "succeeded":
        connection.execute(
            "UPDATE m2_recovery_jobs SET status='SUCCEEDED',updated_at=? WHERE recovery_id=? AND status='CLAIMED'",
            (timestamp, item["recovery_id"]),
        )
        if lane_state == "CANARY_IN_FLIGHT":
            _set_meta(connection, "lane_state", "ACTIVE", timestamp)
        _record_event(
            connection,
            event_key=f"settled:{item['recovery_id']}:{delivery_attempt_id}",
            recovery_id=str(item["recovery_id"]),
            run_id=run_id,
            event_type="RECOVERY_SUCCEEDED",
            payload={
                "attempt_id": delivery_attempt_id,
                "checkpoint_resume": bool(item["checkpoint_compatible"]),
            },
            now=timestamp,
        )
        return {"settled": True, "status": "SUCCEEDED", "recovery_id": item["recovery_id"]}
    category = classify_failure(stage, code, detail)
    signature = normalize_failure_signature(stage, code, detail)
    history = _decode_json(item["failure_signature_history_json"], [])
    if not isinstance(history, list):
        history = []
    worker_version = str(item["last_recovery_version"] or "")
    current_checkpoint = _current_checkpoint_sha(
        connection, str(item["canonical_path"]), int(item["media_mtime_ns"] or 0)
    )
    prior_same_runtime = any(
        isinstance(entry, Mapping)
        and str(entry.get("signature") or "") == signature
        and str(entry.get("runtime_version") or "") == worker_version
        for entry in history
    )
    no_new_checkpoint = current_checkpoint == str(item["claim_checkpoint_sha256"] or "")
    no_progress = bool(prior_same_runtime and no_new_checkpoint)
    history.append(
        {
            "signature": signature,
            "runtime_version": worker_version,
            "observed_at": timestamp,
            "checkpoint_sha256": current_checkpoint,
        }
    )
    attempt_count = int(item["recovery_attempt_count"] or 0)
    exhausted = attempt_count >= int(item["retry_budget"] or 0)
    if category in {"QUALITY_BLOCKED", "BAD_INPUT", "PERMANENT_SYSTEM_ERROR"}:
        next_status = "EXCLUDED" if category != "PERMANENT_SYSTEM_ERROR" else "FAILED"
        next_decision, next_reason, _priority = recovery_decision(
            category,
            str(item["previous_state"]),
            checkpoint_available=bool(item["checkpoint_compatible"]),
            resume_stage=str(item["resume_stage"]),
        )
    elif no_progress or exhausted:
        next_status = "BLOCKED_NO_PROGRESS" if no_progress else "EXCLUDED"
        next_decision = "KEEP_NEEDS_REVIEW"
        next_reason = "same_signature_same_runtime_without_new_checkpoint" if no_progress else "recovery_retry_budget_exhausted"
    else:
        next_status = "READY"
        next_decision = str(item["recovery_decision"])
        next_reason = "bounded_recovery_retry_scheduled"
    connection.execute(
        """
        UPDATE m2_recovery_jobs
        SET status=?,failure_category=?,failure_signature=?,failure_reason=?,
            recovery_decision=?,recovery_reason=?,checkpoint_sha256=?,
            failure_signature_history_json=?,no_progress_count=no_progress_count+?,
            not_before=?,updated_at=?
        WHERE recovery_id=? AND status='CLAIMED'
        """,
        (
            next_status,
            category,
            signature,
            str(detail)[:1000],
            next_decision,
            next_reason,
            current_checkpoint,
            _json(history[-20:]),
            1 if no_progress else 0,
            timestamp + max(60, int(retry_after_seconds or 0)) if next_status == "READY" else 0,
            timestamp,
            item["recovery_id"],
        ),
    )
    if lane_state == "CANARY_IN_FLIGHT" and next_status != "SUCCEEDED":
        # A local rejection or bounded retry must not permanently hold every
        # unrelated recovery job. Stay in single-canary mode; admission still
        # requires ARMED and the existing dispatch/retry interval.
        _set_meta(
            connection, "lane_state",
            "PAUSED" if category == "PERMANENT_SYSTEM_ERROR" else "CANARY_READY",
            timestamp,
        )
    _record_event(
        connection,
        event_key=f"settled:{item['recovery_id']}:{delivery_attempt_id}",
        recovery_id=str(item["recovery_id"]),
        run_id=run_id,
        event_type=("RECOVERY_NO_PROGRESS_BLOCKED" if no_progress else "RECOVERY_FAILED"),
        payload={
            "attempt_id": delivery_attempt_id,
            "failure_category": category,
            "failure_signature": signature,
            "runtime_version": worker_version,
            "checkpoint_before": item["claim_checkpoint_sha256"],
            "checkpoint_after": current_checkpoint,
            "retry_budget": int(item["retry_budget"] or 0),
            "recovery_attempt_count": attempt_count,
            "next_status": next_status,
            "recovery_reason": next_reason,
        },
        now=timestamp,
    )
    return {
        "settled": True,
        "status": next_status,
        "recovery_id": item["recovery_id"],
        "no_progress": no_progress,
        "retry_budget_exhausted": exhausted,
    }


def recovery_status(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_recovery_schema(connection)
    latest = connection.execute(
        "SELECT run_id,current_worker_version,started_at,completed_at,metrics_json FROM m2_recovery_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    base_metrics = _decode_json(latest[4], {}) if latest is not None else {}
    if not isinstance(base_metrics, dict):
        base_metrics = {}
    status_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(1) FROM m2_recovery_jobs GROUP BY status"
        ).fetchall()
    }
    decision_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT recovery_decision,COUNT(1) FROM m2_recovery_jobs GROUP BY recovery_decision"
        ).fetchall()
    }
    event_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT event_type,COUNT(1) FROM m2_recovery_events GROUP BY event_type"
        ).fetchall()
    }
    checkpoint_success = int(
        connection.execute(
            """
            SELECT COUNT(1)
            FROM m2_recovery_events e
            JOIN m2_recovery_jobs j ON j.recovery_id=e.recovery_id
            WHERE e.event_type='RECOVERY_SUCCEEDED' AND j.checkpoint_compatible=1
            """
        ).fetchone()[0]
    )
    return {
        "contract": RECOVERY_CONTRACT,
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "policy_version": RECOVERY_POLICY_VERSION,
        "lane_version": RECOVERY_LANE_VERSION,
        "lane_state": _meta(connection, "lane_state", "EMPTY"),
        "current_worker_version": _meta(connection, "current_worker_version", ""),
        "latest_run_id": str(latest[0]) if latest is not None else "",
        "latest_run_started_at": _utc(float(latest[2])) if latest is not None else "",
        "latest_run_completed_at": _utc(float(latest[3])) if latest is not None and float(latest[3] or 0) > 0 else "",
        **base_metrics,
        "recovered_from_checkpoint": checkpoint_success,
        "requeued_for_stage_retry": event_counts.get("RECOVERY_DISPATCHED", 0),
        "recovery_success": event_counts.get("RECOVERY_SUCCEEDED", 0),
        "preclaim_excluded_count": event_counts.get("RECOVERY_PRECLAIM_EXCLUDED", 0),
        "recovery_failed": event_counts.get("RECOVERY_FAILED", 0)
        + event_counts.get("RECOVERY_NO_PROGRESS_BLOCKED", 0),
        "repeated_no_progress_blocked": status_counts.get("BLOCKED_NO_PROGRESS", 0),
        "ready_count": status_counts.get("READY", 0),
        "dispatched_count": status_counts.get("DISPATCHED", 0),
        "claimed_count": status_counts.get("CLAIMED", 0),
        "permanently_excluded_count": sum(
            status_counts.get(key, 0) for key in ("EXCLUDED", "FAILED", "BLOCKED_NO_PROGRESS")
        ),
        "status_counts": status_counts,
        "decision_counts": decision_counts,
    }


def record_breaker_recovery(
    connection: sqlite3.Connection,
    *,
    recovery_record_id: str,
    evidence: Mapping[str, Any],
    now: float | None = None,
) -> None:
    ensure_recovery_schema(connection)
    timestamp = float(time.time() if now is None else now)
    _record_event(
        connection,
        event_key=f"breaker-recovery:{recovery_record_id}",
        recovery_id="__circuit_breaker__",
        run_id=_meta(connection, "last_run_id", ""),
        event_type="CONTROLLED_BREAKER_RECOVERY",
        payload=dict(evidence),
        now=timestamp,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M2 durable production recovery status")
    parser.add_argument("command", choices=("status", "dispatch"))
    parser.add_argument("--config", default="config.yaml")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from config import load_config
        from scan_state import ScanStateStore

        config = load_config(args.config)
        state = ScanStateStore.from_config(config)
        try:
            if args.command == "status":
                result = recovery_status(state.observation_connection)
            else:
                from m2_guardrail_runtime import runtime_guardrail_status

                result = dispatch_next_recovery(
                    state.observation_connection,
                    runtime_status=str(runtime_guardrail_status(config).get("status") or "DEGRADED"),
                    dispatch_interval_seconds=int(
                        getattr(config, "m2_recovery_dispatch_interval_seconds", 300) or 300
                    ),
                )
                state.commit()
        finally:
            state.close()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (RecoveryError, OSError, sqlite3.Error, ValueError) as exc:
        reason = exc.reason_code if isinstance(exc, RecoveryError) else _safe_code(type(exc).__name__, "recovery_failed")
        print(json.dumps({"status": "DEGRADED", "reason_code": reason}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
