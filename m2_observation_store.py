from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator, Mapping

from safe_files import atomic_write_text


SCHEMA_VERSION = 4
ELIGIBILITY_POLICY_VERSION = "m2-frozen-first-20-v1"
ACTIVE = "ACTIVE"
SETTLED = "SETTLED"
INVALIDATED_AUTOMATION = "INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY"
INVALIDATED_RUNTIME = "INVALIDATED_BY_RUNTIME_CHANGE"
TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "NEEDS_REVIEW", "QUARANTINED"})
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class ObservationStoreError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = _safe_code(reason_code, "observation_store_error")
        super().__init__(self.reason_code)


def _execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute static DDL without sqlite3.executescript's implicit commit."""

    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        candidate = "\n".join(pending).strip()
        if candidate and sqlite3.complete_statement(candidate):
            connection.execute(candidate)
            pending.clear()
    if "\n".join(pending).strip():
        raise ObservationStoreError("observation_schema_script_incomplete")


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
        is not None
    )


def _observation_schema_issues(connection: sqlite3.Connection) -> list[str]:
    required_columns = {
        "m2_observation_meta": {"key", "value", "updated_at"},
        "m2_observation_gates": {
            "gate_id",
            "status",
            "baseline_version",
            "worker_sha",
            "webui_sha",
            "container_image_id",
            "worker_container_id",
            "worker_runtime_instance_fingerprint",
            "configuration_fingerprint",
            "decision_schema_version",
            "eligibility_policy_version",
            "gate_start_at",
            "gate_start_epoch",
            "target_size",
            "enrolled_count",
            "settled_count",
            "invalidation_reason",
            "summary_payload_json",
            "summary_emitted_at",
            "baseline_json",
            "legacy_observation_json",
            "legacy_runtime_manifest_json",
            "legacy_runtime_manifest_sha256",
            "pre_gate_attempt_count",
        },
        "m2_observation_gate_jobs": {
            "gate_id",
            "ordinal",
            "job_id",
            "claim_identity_hash",
            "input_fingerprint",
            "claimed_at",
            "eligibility_evidence_json",
            "runtime_baseline_json",
            "current_state",
            "final_state",
            "terminal_at",
            "strict_verified",
            "strict_evidence_json",
            "qualification_reason_json",
            "output_parse_result",
            "hard_qc_result",
            "hallucination_result",
            "source_checksum_result",
            "duplicate_job_result",
            "duplicate_publish_result",
            "checkpoint_resume_result",
            "retry_fallback_result",
            "breaker_evidence_json",
            "failure_review_reason",
            "processing_strategy",
            "incident_flags_json",
            "terminal_evidence_sha256",
        },
        "m2_observation_supplemental": {
            "gate_id",
            "job_id",
            "claim_identity_hash",
            "exclusion_reason",
            "evidence_json",
        },
        "m2_observation_result_events": {
            "claim_identity_hash",
            "gate_id",
            "job_id",
            "observed_state",
            "event_sha256",
            "event_payload_json",
            "created_at",
        },
    }
    issues: list[str] = []
    for table, required in required_columns.items():
        if not _table_exists(connection, table):
            issues.append(f"missing_table_{table}")
            continue
        missing = sorted(required - _table_columns(connection, table))
        if missing:
            issues.append(f"missing_columns_{table}_{'_'.join(missing)}")
    required_triggers = {
        "trg_m2_observation_job_insert_frozen_order",
        "trg_m2_observation_job_no_delete",
        "trg_m2_observation_job_identity_immutable",
        "trg_m2_observation_terminal_write_once",
        "trg_m2_observation_gate_no_delete",
        "trg_m2_observation_gate_identity_immutable",
        "trg_m2_observation_legacy_runtime_write_once",
        "trg_m2_observation_gate_terminal_status",
        "trg_m2_observation_summary_write_once",
        "trg_m2_observation_result_event_no_update",
        "trg_m2_observation_result_event_no_delete",
    }
    present_triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_m2_observation_%'"
        ).fetchall()
    }
    for trigger in sorted(required_triggers - present_triggers):
        issues.append(f"missing_trigger_{trigger}")
    terminal_trigger = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_m2_observation_terminal_write_once'"
    ).fetchone()
    terminal_columns = {
        "current_state",
        "final_state",
        "terminal_at",
        "strict_verified",
        "strict_evidence_json",
        "qualification_reason_json",
        "output_parse_result",
        "hard_qc_result",
        "hallucination_result",
        "source_checksum_result",
        "duplicate_job_result",
        "duplicate_publish_result",
        "checkpoint_resume_result",
        "retry_fallback_result",
        "breaker_evidence_json",
        "failure_review_reason",
        "processing_strategy",
        "incident_flags_json",
        "terminal_evidence_sha256",
    }
    trigger_sql = str(terminal_trigger[0] if terminal_trigger else "")
    if any(column not in trigger_sql for column in terminal_columns):
        issues.append("terminal_evidence_trigger_incomplete")
    required_indexes = {
        "idx_m2_observation_one_active_gate",
        "idx_m2_observation_gates_started",
        "idx_m2_observation_jobs_gate_terminal",
        "idx_m2_observation_result_events_gate_job",
    }
    present_indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_m2_observation_%'"
        ).fetchall()
    }
    for index in sorted(required_indexes - present_indexes):
        issues.append(f"missing_index_{index}")
    return issues


def ensure_observation_schema(connection: sqlite3.Connection) -> None:
    """Install or migrate the frozen-gate schema atomically."""

    owns_transaction = not connection.in_transaction
    savepoint = "m2_observation_schema_migration_v4"
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute(f"SAVEPOINT {savepoint}")
    try:
        _ensure_observation_schema_unprotected(connection)
    except BaseException:
        if owns_transaction:
            connection.rollback()
        else:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
        raise
    if owns_transaction:
        connection.commit()
    else:
        connection.execute(f"RELEASE {savepoint}")


def _ensure_observation_schema_unprotected(connection: sqlite3.Connection) -> None:
    """Install the additive frozen-gate schema inside the caller transaction."""

    stored_version = 0
    if _table_exists(connection, "m2_observation_meta"):
        version_row = connection.execute(
            "SELECT value FROM m2_observation_meta WHERE key='schema_version'"
        ).fetchone()
        if version_row is not None:
            try:
                stored_version = int(version_row[0])
            except (TypeError, ValueError) as exc:
                raise ObservationStoreError("observation_schema_version_malformed") from exc
            if stored_version < 0 or stored_version > SCHEMA_VERSION:
                raise ObservationStoreError("observation_schema_version_unsupported")
            if stored_version == SCHEMA_VERSION:
                issues = _observation_schema_issues(connection)
                if issues:
                    raise ObservationStoreError(
                        "observation_schema_current_shape_invalid_" + issues[0]
                    )
                return

    if _table_exists(connection, "m2_observation_gates"):
        gate_columns = _table_columns(connection, "m2_observation_gates")
        if "worker_container_id" not in gate_columns:
            connection.execute(
                "ALTER TABLE m2_observation_gates ADD COLUMN worker_container_id TEXT NOT NULL DEFAULT ''"
            )
        if "worker_runtime_instance_fingerprint" not in gate_columns:
            connection.execute(
                "ALTER TABLE m2_observation_gates ADD COLUMN worker_runtime_instance_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        if "legacy_runtime_manifest_json" not in gate_columns:
            connection.execute(
                "ALTER TABLE m2_observation_gates ADD COLUMN legacy_runtime_manifest_json TEXT NOT NULL DEFAULT ''"
            )
        if "legacy_runtime_manifest_sha256" not in gate_columns:
            connection.execute(
                "ALTER TABLE m2_observation_gates ADD COLUMN legacy_runtime_manifest_sha256 TEXT NOT NULL DEFAULT ''"
            )
    if _table_exists(connection, "m2_observation_result_events"):
        event_columns = _table_columns(connection, "m2_observation_result_events")
        if "event_payload_json" not in event_columns:
            connection.execute(
                "ALTER TABLE m2_observation_result_events ADD COLUMN event_payload_json TEXT NOT NULL DEFAULT '{}'"
            )
    for trigger in (
        "trg_m2_observation_job_insert_frozen_order",
        "trg_m2_observation_job_no_delete",
        "trg_m2_observation_job_identity_immutable",
        "trg_m2_observation_terminal_write_once",
        "trg_m2_observation_gate_no_delete",
        "trg_m2_observation_gate_identity_immutable",
        "trg_m2_observation_legacy_runtime_write_once",
        "trg_m2_observation_gate_terminal_status",
        "trg_m2_observation_summary_write_once",
        "trg_m2_observation_result_event_no_update",
        "trg_m2_observation_result_event_no_delete",
    ):
        connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')

    schema_sql = """
        CREATE TABLE IF NOT EXISTS m2_observation_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS m2_observation_gates (
            gate_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            status TEXT NOT NULL,
            baseline_version TEXT NOT NULL,
            worker_sha TEXT NOT NULL,
            webui_sha TEXT NOT NULL,
            container_image_id TEXT NOT NULL,
            worker_container_id TEXT NOT NULL,
            worker_runtime_instance_fingerprint TEXT NOT NULL,
            configuration_fingerprint TEXT NOT NULL,
            decision_schema_version INTEGER NOT NULL,
            eligibility_policy_version TEXT NOT NULL,
            gate_start_at TEXT NOT NULL,
            gate_start_epoch REAL NOT NULL,
            target_size INTEGER NOT NULL CHECK(target_size = 20),
            enrolled_count INTEGER NOT NULL DEFAULT 0
                CHECK(enrolled_count BETWEEN 0 AND target_size),
            settled_count INTEGER NOT NULL DEFAULT 0
                CHECK(settled_count BETWEEN 0 AND enrolled_count),
            invalidated_at REAL,
            invalidation_reason TEXT NOT NULL DEFAULT '',
            invalidation_evidence_json TEXT NOT NULL DEFAULT '{}',
            summary_ready_at REAL,
            summary_payload_json TEXT NOT NULL DEFAULT '',
            summary_sha256 TEXT NOT NULL DEFAULT '',
            summary_emitted_at REAL,
            summary_path TEXT NOT NULL DEFAULT '',
            baseline_json TEXT NOT NULL,
            legacy_observation_json TEXT NOT NULL DEFAULT '',
            legacy_runtime_manifest_json TEXT NOT NULL DEFAULT '',
            legacy_runtime_manifest_sha256 TEXT NOT NULL DEFAULT '',
            pre_gate_attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_m2_observation_one_active_gate
            ON m2_observation_gates(status) WHERE status = 'ACTIVE';
        CREATE INDEX IF NOT EXISTS idx_m2_observation_gates_started
            ON m2_observation_gates(gate_start_epoch DESC);

        CREATE TABLE IF NOT EXISTS m2_observation_gate_jobs (
            gate_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 20),
            job_id TEXT NOT NULL,
            claim_identity_hash TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            eligibility_evidence_json TEXT NOT NULL,
            runtime_baseline_json TEXT NOT NULL,
            current_state TEXT NOT NULL DEFAULT 'CLAIMED',
            final_state TEXT,
            terminal_at REAL,
            strict_verified INTEGER,
            strict_evidence_json TEXT NOT NULL DEFAULT '{}',
            qualification_reason_json TEXT NOT NULL DEFAULT '[]',
            output_parse_result TEXT NOT NULL DEFAULT 'PENDING',
            hard_qc_result TEXT NOT NULL DEFAULT 'PENDING',
            hallucination_result TEXT NOT NULL DEFAULT 'PENDING',
            source_checksum_result TEXT NOT NULL DEFAULT 'PENDING',
            duplicate_job_result TEXT NOT NULL DEFAULT 'PENDING',
            duplicate_publish_result TEXT NOT NULL DEFAULT 'PENDING',
            checkpoint_resume_result TEXT NOT NULL DEFAULT 'PENDING',
            retry_fallback_result TEXT NOT NULL DEFAULT 'PENDING',
            breaker_evidence_json TEXT NOT NULL DEFAULT '{}',
            failure_review_reason TEXT NOT NULL DEFAULT '',
            processing_strategy TEXT NOT NULL DEFAULT 'UNREPORTED',
            incident_flags_json TEXT NOT NULL DEFAULT '{}',
            terminal_evidence_sha256 TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(gate_id, job_id),
            UNIQUE(gate_id, ordinal),
            FOREIGN KEY(gate_id) REFERENCES m2_observation_gates(gate_id)
        );

        CREATE INDEX IF NOT EXISTS idx_m2_observation_jobs_gate_terminal
            ON m2_observation_gate_jobs(gate_id, terminal_at, ordinal);

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_job_insert_frozen_order
        BEFORE INSERT ON m2_observation_gate_jobs
        WHEN NOT EXISTS (
            SELECT 1 FROM m2_observation_gates g
            WHERE g.gate_id=NEW.gate_id
              AND g.status='ACTIVE'
              AND g.enrolled_count < g.target_size
              AND NEW.ordinal=g.enrolled_count + 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation enrollment is not the next frozen slot');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_job_no_delete
        BEFORE DELETE ON m2_observation_gate_jobs
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation membership is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_job_identity_immutable
        BEFORE UPDATE OF gate_id, ordinal, job_id, claim_identity_hash,
                         input_fingerprint, claimed_at,
                         eligibility_evidence_json, runtime_baseline_json
        ON m2_observation_gate_jobs
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation membership identity is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_terminal_write_once
        BEFORE UPDATE OF current_state, final_state, terminal_at,
                         strict_verified, strict_evidence_json,
                         qualification_reason_json, output_parse_result,
                         hard_qc_result, hallucination_result,
                         source_checksum_result, duplicate_job_result,
                         duplicate_publish_result, checkpoint_resume_result,
                         retry_fallback_result, breaker_evidence_json,
                         failure_review_reason, processing_strategy,
                         incident_flags_json, terminal_evidence_sha256
        ON m2_observation_gate_jobs
        WHEN OLD.terminal_at IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation terminal evidence is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_gate_no_delete
        BEFORE DELETE ON m2_observation_gates
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation gate history is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_gate_identity_immutable
        BEFORE UPDATE OF gate_id, schema_version, baseline_version, worker_sha,
                         webui_sha, container_image_id, worker_container_id,
                         worker_runtime_instance_fingerprint,
                         configuration_fingerprint, decision_schema_version,
                         eligibility_policy_version, gate_start_at,
                         gate_start_epoch, target_size, baseline_json,
                         legacy_observation_json, pre_gate_attempt_count,
                         created_at
        ON m2_observation_gates
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation gate identity is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_legacy_runtime_write_once
        BEFORE UPDATE OF legacy_runtime_manifest_json,
                         legacy_runtime_manifest_sha256
        ON m2_observation_gates
        WHEN OLD.legacy_runtime_manifest_json <> ''
          OR OLD.legacy_runtime_manifest_sha256 <> ''
          OR NEW.legacy_runtime_manifest_json = ''
          OR length(NEW.legacy_runtime_manifest_sha256) <> 64
          OR OLD.status <> 'INVALIDATED_OBSERVATION_AUTOMATION_NOT_READY'
          OR OLD.legacy_observation_json = ''
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation legacy runtime evidence is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_gate_terminal_status
        BEFORE UPDATE OF status ON m2_observation_gates
        WHEN OLD.status <> 'ACTIVE' AND NEW.status <> OLD.status
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation terminal gate status is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_summary_write_once
        BEFORE UPDATE OF summary_ready_at, summary_payload_json, summary_sha256
        ON m2_observation_gates
        WHEN OLD.summary_ready_at IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation summary journal is immutable');
        END;

        CREATE TABLE IF NOT EXISTS m2_observation_supplemental (
            gate_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            claim_identity_hash TEXT NOT NULL,
            exclusion_reason TEXT NOT NULL,
            claimed_at REAL,
            last_state TEXT NOT NULL DEFAULT '',
            last_observed_at REAL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(gate_id, job_id),
            FOREIGN KEY(gate_id) REFERENCES m2_observation_gates(gate_id)
        );

        CREATE TABLE IF NOT EXISTS m2_observation_result_events (
            claim_identity_hash TEXT PRIMARY KEY,
            gate_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            observed_state TEXT NOT NULL,
            event_sha256 TEXT NOT NULL,
            event_payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(gate_id) REFERENCES m2_observation_gates(gate_id)
        );

        CREATE INDEX IF NOT EXISTS idx_m2_observation_result_events_gate_job
            ON m2_observation_result_events(gate_id, job_id, created_at);

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_result_event_no_update
        BEFORE UPDATE ON m2_observation_result_events
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation result event is immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_m2_observation_result_event_no_delete
        BEFORE DELETE ON m2_observation_result_events
        BEGIN
            SELECT RAISE(ABORT, 'm2 observation result event is immutable');
        END;
        """
    _execute_schema_script(connection, schema_sql)
    now = time.time()
    for key, default in (
        ("oom_streak", "0"),
        ("oom_job_ids", "[]"),
        ("identical_failure_signature", ""),
        ("identical_failure_streak", "0"),
        ("identical_failure_job_ids", "[]"),
    ):
        connection.execute(
            "INSERT OR IGNORE INTO m2_observation_meta(key, value, updated_at) VALUES(?, ?, ?)",
            (key, default, now),
        )
    issues = _observation_schema_issues(connection)
    if issues:
        raise ObservationStoreError("observation_schema_migration_invalid_" + issues[0])
    connection.execute(
        """
        INSERT INTO m2_observation_meta(key, value, updated_at)
        VALUES('schema_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(SCHEMA_VERSION), now),
    )


def observation_database_path(config: Any) -> Path:
    from scan_state import scan_state_path

    return scan_state_path(config)


def connect_observation_database(config: Any) -> sqlite3.Connection:
    path = observation_database_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=60)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=60000")
    mode = connection.execute("PRAGMA journal_mode").fetchone()
    if str(mode[0] if mode else "").casefold() != "wal":
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    ensure_observation_schema(connection)
    connection.commit()
    return connection


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if owns_transaction:
            connection.commit()
    except Exception:
        if owns_transaction and connection.in_transaction:
            connection.rollback()
        raise


def gate_identifier(runtime_state: Mapping[str, Any]) -> str:
    start = float(runtime_state.get("gate_start_epoch") or 0)
    baseline = str(runtime_state.get("gate_baseline_version") or "")
    if not math.isfinite(start) or start <= 0 or not baseline:
        raise ObservationStoreError("gate_identity_incomplete")
    stamp = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(f"{baseline}\0{start:.6f}".encode("utf-8")).hexdigest()[:10]
    return f"m2-gate-{stamp}-{digest}"


def create_gate(
    connection: sqlite3.Connection,
    runtime_state: Mapping[str, Any],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    timestamp = time.time() if now is None else float(now)
    baseline = _validated_runtime_baseline(runtime_state)
    gate_id = gate_identifier(runtime_state)
    start = float(runtime_state["gate_start_epoch"])
    start_at = str(runtime_state.get("gate_start_at") or _utc(start))
    gate = runtime_state.get("gate")
    supplied_gate_id = str(gate.get("gate_id") or "") if isinstance(gate, Mapping) else ""
    if supplied_gate_id and supplied_gate_id != gate_id:
        raise ObservationStoreError("runtime_gate_id_mismatch")
    target = int(gate.get("target") or 0) if isinstance(gate, Mapping) else 0
    if target != 20:
        raise ObservationStoreError("gate_target_must_be_twenty")

    existing = gate_by_id(connection, gate_id)
    if existing is not None:
        _assert_gate_matches_runtime(existing, runtime_state)
        if str(existing.get("status") or "") not in {ACTIVE, SETTLED}:
            raise ObservationStoreError("gate_identity_already_invalidated")
        return existing
    active = active_gate(connection)
    if active is not None:
        raise ObservationStoreError("active_gate_already_exists")
    pre_gate = runtime_state.get("pre_gate_running")
    pre_gate_count = int(pre_gate.get("attempt_count") or 0) if isinstance(pre_gate, Mapping) else 0
    connection.execute(
        """
        INSERT INTO m2_observation_gates(
            gate_id, schema_version, status, baseline_version, worker_sha,
            webui_sha, container_image_id, worker_container_id,
            worker_runtime_instance_fingerprint, configuration_fingerprint,
            decision_schema_version, eligibility_policy_version, gate_start_at,
            gate_start_epoch, target_size, enrolled_count, settled_count,
            baseline_json, pre_gate_attempt_count, created_at, updated_at
        ) VALUES(?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 20, 0, 0, ?, ?, ?, ?)
        """,
        (
            gate_id,
            SCHEMA_VERSION,
            str(runtime_state["gate_baseline_version"]),
            str(baseline["worker_commit_sha"]),
            str(baseline["webui_commit_sha"]),
            str(baseline["worker_image_id"]),
            str(baseline["worker_container_id"]),
            str(baseline["worker_runtime_instance_fingerprint"]),
            str(baseline["configuration_fingerprint"]),
            int(baseline["decision_schema_version"]),
            str(baseline["eligibility_policy_version"]),
            start_at,
            start,
            _json(baseline),
            pre_gate_count,
            timestamp,
            timestamp,
        ),
    )
    created = gate_by_id(connection, gate_id)
    if created is None:
        raise ObservationStoreError("gate_create_failed")
    return created


def import_legacy_gate(
    connection: sqlite3.Connection,
    legacy_state: Mapping[str, Any],
    runtime_state: Mapping[str, Any] | None,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Preserve the pre-repair JSON gate as immutable invalidated history."""

    if not isinstance(runtime_state, Mapping):
        raise ObservationStoreError("legacy_runtime_manifest_missing")
    baseline = legacy_state.get("runtime_baseline")
    if not isinstance(baseline, Mapping):
        baseline = runtime_state.get("baseline")
    if not isinstance(baseline, Mapping):
        return None
    baseline_version = str(
        legacy_state.get("gate_baseline_version")
        or (runtime_state or {}).get("gate_baseline_version")
        or ""
    )
    try:
        start = float(
            legacy_state.get("gate_start_epoch")
            or (runtime_state or {}).get("gate_start_epoch")
            or 0
        )
    except (TypeError, ValueError):
        return None
    if not baseline_version or start <= 0:
        return None
    synthetic = {
        "gate_baseline_version": baseline_version,
        "gate_start_epoch": start,
    }
    gate_id = gate_identifier(synthetic)
    timestamp = time.time() if now is None else float(now)
    claims = legacy_state.get("claims")
    enrolled = min(20, sum(
        1
        for claim in (claims.values() if isinstance(claims, Mapping) else [])
        if isinstance(claim, Mapping) and claim.get("gate_eligible") is True
    ))
    settled = min(enrolled, sum(
        1
        for claim in (claims.values() if isinstance(claims, Mapping) else [])
        if isinstance(claim, Mapping)
        and claim.get("gate_eligible") is True
        and claim.get("terminal_observed") is True
    ))
    pre_gate = runtime_state.get("pre_gate_running")
    attempt_keys = pre_gate.get("attempt_keys") if isinstance(pre_gate, Mapping) else None
    queue_job_keys = pre_gate.get("queue_job_keys") if isinstance(pre_gate, Mapping) else None
    if (
        not isinstance(attempt_keys, list)
        or not isinstance(queue_job_keys, list)
        or int(pre_gate.get("attempt_count") or 0) != len(attempt_keys)
        or int(pre_gate.get("queue_job_count") or 0) != len(queue_job_keys)
    ):
        raise ObservationStoreError("legacy_runtime_pre_gate_snapshot_invalid")
    pre_gate_count = len(attempt_keys)
    runtime_manifest_json = _json(_legacy_runtime_manifest_payload(runtime_state))
    runtime_manifest_sha256 = hashlib.sha256(
        runtime_manifest_json.encode("utf-8")
    ).hexdigest()
    decision_schema = int(baseline.get("decision_schema_version") or 0)
    connection.execute(
        """
        INSERT OR IGNORE INTO m2_observation_gates(
            gate_id, schema_version, status, baseline_version, worker_sha,
            webui_sha, container_image_id, worker_container_id,
            worker_runtime_instance_fingerprint, configuration_fingerprint,
            decision_schema_version, eligibility_policy_version, gate_start_at,
            gate_start_epoch, target_size, enrolled_count, settled_count,
            invalidated_at, invalidation_reason, invalidation_evidence_json,
            baseline_json, legacy_observation_json, legacy_runtime_manifest_json,
            legacy_runtime_manifest_sha256, pre_gate_attempt_count,
            created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 20, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gate_id,
            SCHEMA_VERSION,
            INVALIDATED_AUTOMATION,
            baseline_version,
            str(baseline.get("worker_commit_sha") or "unknown"),
            str(baseline.get("webui_commit_sha") or "unknown"),
            str(baseline.get("worker_image_id") or "unknown"),
            str(baseline.get("worker_container_id") or "legacy-unavailable"),
            str(
                baseline.get("worker_runtime_instance_fingerprint")
                or "legacy-unavailable"
            ),
            str(baseline.get("configuration_fingerprint") or "unknown"),
            decision_schema,
            str(baseline.get("eligibility_policy_version") or "legacy-success-count-v2"),
            str((runtime_state or {}).get("gate_start_at") or _utc(start)),
            start,
            enrolled,
            settled,
            timestamp,
            INVALIDATED_AUTOMATION,
            _json({"reason": "legacy_observer_selected_strict_successes_instead_of_claims"}),
            _json(dict(baseline)),
            _json(dict(legacy_state)),
            runtime_manifest_json,
            runtime_manifest_sha256,
            pre_gate_count,
            float(legacy_state.get("created_at") or start),
            timestamp,
        ),
    )
    # Version-2 databases may already contain the immutable legacy Gate but not
    # its runtime snapshot.  Permit exactly one empty-to-complete enrichment;
    # the dedicated trigger rejects every later rewrite.
    connection.execute(
        """
        UPDATE m2_observation_gates
        SET legacy_runtime_manifest_json=?, legacy_runtime_manifest_sha256=?,
            updated_at=?
        WHERE gate_id=?
          AND status=?
          AND legacy_observation_json<>''
          AND legacy_runtime_manifest_json=''
          AND legacy_runtime_manifest_sha256=''
        """,
        (
            runtime_manifest_json,
            runtime_manifest_sha256,
            timestamp,
            gate_id,
            INVALIDATED_AUTOMATION,
        ),
    )
    return gate_by_id(connection, gate_id)


def invalidate_active_gate(
    connection: sqlite3.Connection,
    reason: str,
    *,
    evidence: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    normalized = _safe_code(reason, "gate_invalidated")
    if normalized not in {INVALIDATED_AUTOMATION, INVALIDATED_RUNTIME}:
        raise ObservationStoreError("invalidation_reason_not_allowed")
    gate = active_gate(connection)
    if gate is None:
        return None
    timestamp = time.time() if now is None else float(now)
    connection.execute(
        """
        UPDATE m2_observation_gates
        SET status=?, invalidated_at=?, invalidation_reason=?,
            invalidation_evidence_json=?, updated_at=?
        WHERE gate_id=? AND status='ACTIVE'
        """,
        (
            normalized,
            timestamp,
            normalized,
            _json(_bounded_mapping(evidence or {})),
            timestamp,
            gate["gate_id"],
        ),
    )
    return gate_by_id(connection, str(gate["gate_id"]))


def enroll_claim(
    connection: sqlite3.Connection,
    runtime_state: Mapping[str, Any],
    *,
    claim_identity: str,
    gate_job_identity: str,
    input_fingerprint: str,
    claimed_at: float,
    processing_strategy: str,
    eligible: bool,
    eligibility_reason: str,
    now: float | None = None,
) -> dict[str, Any]:
    gate = active_gate(connection) or latest_gate(connection)
    if gate is None:
        raise ObservationStoreError("observation_gate_missing")
    gate_status = str(gate.get("status") or "")
    if gate_status not in {ACTIVE, SETTLED}:
        raise ObservationStoreError("observation_gate_invalidated")
    _assert_gate_matches_runtime(gate, runtime_state)
    timestamp = time.time() if now is None else float(now)
    claim_time = float(claimed_at)
    if not math.isfinite(claim_time) or claim_time <= 0:
        raise ObservationStoreError("claim_timestamp_invalid")
    job_id = _identity_hash(gate_job_identity)
    claim_hash = _identity_hash(claim_identity)
    input_hash = _identity_hash(input_fingerprint)
    existing = member_for_job(connection, str(gate["gate_id"]), job_id)
    if existing is not None:
        if str(existing.get("input_fingerprint") or "") != input_hash:
            raise ObservationStoreError("duplicate_job_input_fingerprint_conflict")
        return {
            "status": gate_status,
            "recorded": False,
            "enrolled": True,
            "duplicate_claim_ignored": True,
            "ordinal": int(existing["ordinal"]),
            "gate_id": gate["gate_id"],
        }
    if gate_status == SETTLED:
        _upsert_supplemental(
            connection,
            gate_id=str(gate["gate_id"]),
            job_id=job_id,
            claim_identity_hash=claim_hash,
            exclusion_reason="frozen_cohort_settled",
            claimed_at=claim_time,
            now=timestamp,
        )
        return {
            "status": SETTLED,
            "recorded": True,
            "enrolled": False,
            "gate_eligible": False,
            "eligibility_reason": "frozen_cohort_settled",
            "gate_id": gate["gate_id"],
        }
    prior_attempt = _obligation_started_before_gate(
        connection,
        gate_job_identity,
        float(gate["gate_start_epoch"]),
        current_claim_identity=claim_identity,
    )
    if prior_attempt:
        eligible = False
        eligibility_reason = "job_started_before_gate"
    evidence = {
        "eligibility_policy_version": gate["eligibility_policy_version"],
        "reason": _safe_code(eligibility_reason, "unknown"),
        "claimed_after_gate_start": claim_time > float(gate["gate_start_epoch"]),
        "runtime_baseline_match": True,
        "pre_gate_excluded": not eligible and eligibility_reason == "running_before_gate_start",
        "first_attempt_after_gate_start": not prior_attempt,
    }
    if not eligible:
        _upsert_supplemental(
            connection,
            gate_id=str(gate["gate_id"]),
            job_id=job_id,
            claim_identity_hash=claim_hash,
            exclusion_reason=eligibility_reason,
            claimed_at=claim_time,
            now=timestamp,
        )
        return {
            "status": ACTIVE,
            "recorded": True,
            "enrolled": False,
            "gate_eligible": False,
            "eligibility_reason": eligibility_reason,
            "gate_id": gate["gate_id"],
        }
    enrolled = int(gate["enrolled_count"])
    target = int(gate["target_size"])
    if enrolled >= target:
        _upsert_supplemental(
            connection,
            gate_id=str(gate["gate_id"]),
            job_id=job_id,
            claim_identity_hash=claim_hash,
            exclusion_reason="frozen_cohort_full",
            claimed_at=claim_time,
            now=timestamp,
        )
        return {
            "status": ACTIVE,
            "recorded": True,
            "enrolled": False,
            "gate_eligible": False,
            "eligibility_reason": "frozen_cohort_full",
            "gate_id": gate["gate_id"],
        }
    ordinal = enrolled + 1
    connection.execute(
        """
        INSERT INTO m2_observation_gate_jobs(
            gate_id, ordinal, job_id, claim_identity_hash, input_fingerprint,
            claimed_at, eligibility_evidence_json, runtime_baseline_json,
            processing_strategy, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gate["gate_id"],
            ordinal,
            job_id,
            claim_hash,
            input_hash,
            claim_time,
            _json(evidence),
            str(gate["baseline_json"]),
            _safe_code(processing_strategy, "UNREPORTED"),
            timestamp,
            timestamp,
        ),
    )
    changed = connection.execute(
        """
        UPDATE m2_observation_gates
        SET enrolled_count=enrolled_count + 1, updated_at=?
        WHERE gate_id=? AND status='ACTIVE' AND enrolled_count=? AND enrolled_count < target_size
        """,
        (timestamp, gate["gate_id"], enrolled),
    ).rowcount
    if int(changed or 0) != 1:
        raise ObservationStoreError("gate_enrollment_counter_race")
    return {
        "status": ACTIVE,
        "recorded": True,
        "enrolled": True,
        "gate_eligible": True,
        "eligibility_reason": "eligible",
        "ordinal": ordinal,
        "gate_id": gate["gate_id"],
    }


def record_terminal_evidence(
    connection: sqlite3.Connection,
    *,
    gate_job_identity: str,
    claim_identity: str,
    outcome: Mapping[str, Any],
    qualification: Mapping[str, Any],
    breaker_evidence: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    timestamp = time.time() if now is None else float(now)
    job_id = _identity_hash(gate_job_identity)
    claim_hash = _identity_hash(claim_identity)
    member_row = connection.execute(
        """
        SELECT j.*, g.status AS gate_status, g.target_size
        FROM m2_observation_gate_jobs j
        JOIN m2_observation_gates g ON g.gate_id=j.gate_id
        WHERE j.job_id=?
        ORDER BY g.gate_start_epoch DESC LIMIT 1
        """,
        (job_id,),
    )
    member = _fetch_dict(member_row)
    if member is None:
        gate = latest_gate(connection)
        if gate is not None:
            _upsert_supplemental(
                connection,
                gate_id=str(gate["gate_id"]),
                job_id=job_id,
                claim_identity_hash=claim_hash,
                exclusion_reason="claim_not_in_frozen_cohort",
                claimed_at=None,
                now=timestamp,
                last_state=str(outcome.get("terminal_status") or "UNKNOWN"),
                evidence={
                    "qualification": _bounded_mapping(qualification),
                    "outcome": _bounded_mapping(outcome),
                },
            )
        return {"recorded": True, "enrolled": False, "settled": False, "emission_pending": False}

    gate_id = str(member["gate_id"])
    final_state = str(outcome.get("terminal_status") or "UNKNOWN").strip().upper()
    is_terminal = final_state in TERMINAL_STATES
    evidence = qualification.get("evidence")
    effective = dict(evidence) if isinstance(evidence, Mapping) else {}
    missing = set(str(item) for item in qualification.get("missing_evidence", []) or [])
    failed_evidence = set(
        str(item) for item in qualification.get("failed_evidence", []) or []
    )
    from m2_strict_observation import STRICT_EVIDENCE_KEYS

    strict_verified = (
        qualification.get("qualified") is True
        and is_terminal
        and final_state == "COMPLETED"
        and not missing
        and not failed_evidence
        and all(effective.get(key) is True for key in STRICT_EVIDENCE_KEYS)
    )
    incident_flags = {
        key: bool(outcome.get(key))
        for key in (
            "quarantined",
            "hallucination_blocked",
            "output_parse_failure",
            "source_mutation_incident",
            "duplicate_job",
            "duplicate_publish",
            "incorrect_completion",
            "breaker_tripped",
            "checkpoint_resumed",
            "oom_event",
            "unresolved_retry",
            "unresolved_fallback",
        )
    }
    terminal_payload = {
        "final_state": final_state,
        "strict_verified": strict_verified,
        "evidence": effective,
        "reason_codes": list(qualification.get("reason_codes") or []),
        "incidents": incident_flags,
        "breaker": _bounded_mapping(breaker_evidence or {}),
    }
    terminal_digest = hashlib.sha256(_json(terminal_payload).encode("utf-8")).hexdigest()
    if member.get("terminal_at") is not None:
        if str(member.get("terminal_evidence_sha256") or "") != terminal_digest:
            raise ObservationStoreError("terminal_evidence_conflict")
        return {
            "recorded": False,
            "enrolled": True,
            "settled": True,
            "duplicate_observation_ignored": True,
            "gate_id": gate_id,
            "ordinal": int(member["ordinal"]),
            "emission_pending": _summary_pending(connection, gate_id),
        }

    values = _terminal_result_values(effective, missing, outcome)
    connection.execute(
        """
        UPDATE m2_observation_gate_jobs
        SET current_state=?, final_state=?, terminal_at=?, strict_verified=?,
            strict_evidence_json=?, qualification_reason_json=?,
            output_parse_result=?, hard_qc_result=?, hallucination_result=?,
            source_checksum_result=?, duplicate_job_result=?,
            duplicate_publish_result=?, checkpoint_resume_result=?,
            retry_fallback_result=?, breaker_evidence_json=?,
            failure_review_reason=?, processing_strategy=?,
            incident_flags_json=?, terminal_evidence_sha256=?, updated_at=?
        WHERE gate_id=? AND job_id=?
        """,
        (
            final_state,
            final_state if is_terminal else None,
            timestamp if is_terminal else None,
            int(strict_verified) if is_terminal else None,
            _json(effective),
            _json(list(qualification.get("reason_codes") or [])),
            values["output_parse_result"],
            values["hard_qc_result"],
            values["hallucination_result"],
            values["source_checksum_result"],
            values["duplicate_job_result"],
            values["duplicate_publish_result"],
            values["checkpoint_resume_result"],
            values["retry_fallback_result"],
            _json(_bounded_mapping(breaker_evidence or {})),
            _safe_code(
                outcome.get("reason_code") or outcome.get("error_code") or "",
                "",
            ),
            _safe_code(outcome.get("processing_strategy"), "UNREPORTED"),
            _json(incident_flags),
            terminal_digest if is_terminal else "",
            timestamp,
            gate_id,
            job_id,
        ),
    )
    if is_terminal:
        connection.execute(
            "UPDATE m2_observation_gates SET settled_count=settled_count + 1, updated_at=? WHERE gate_id=?",
            (timestamp, gate_id),
        )
        _prepare_summary_if_ready(connection, gate_id, now=timestamp)
    return {
        "recorded": True,
        "enrolled": True,
        "settled": is_terminal,
        "strictly_qualified": strict_verified,
        "gate_id": gate_id,
        "ordinal": int(member["ordinal"]),
        "emission_pending": _summary_pending(connection, gate_id),
    }


def reserve_result_event(
    connection: sqlite3.Connection,
    *,
    gate_job_identity: str,
    claim_identity: str,
    observed_state: str,
    event_payload: Mapping[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    """Reserve one durable result side effect per delivery attempt.

    Queue/result callbacks can be replayed after a process crash.  The attempt
    identity is therefore the idempotency key for breaker streaks and terminal
    observation side effects.  Membership remains keyed by the stable delivery
    obligation so later attempts can settle the same frozen slot.
    """

    timestamp = time.time() if now is None else float(now)
    claim_hash = _identity_hash(claim_identity)
    job_id = _identity_hash(gate_job_identity)
    normalized_state = _safe_code(observed_state, "UNKNOWN")
    payload_text = _json(_bounded_mapping(event_payload))
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    existing = connection.execute(
        """
        SELECT gate_id, job_id, observed_state, event_sha256, event_payload_json
        FROM m2_observation_result_events
        WHERE claim_identity_hash=?
        """,
        (claim_hash,),
    ).fetchone()
    if existing is not None:
        if str(existing[1]) != job_id:
            raise ObservationStoreError("result_event_identity_conflict")
        stored_text = str(existing[4])
        if hashlib.sha256(stored_text.encode("utf-8")).hexdigest() != str(existing[3]):
            raise ObservationStoreError("observation_result_event_digest_mismatch")
        try:
            stored_payload = json.loads(stored_text)
            incoming_payload = json.loads(payload_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ObservationStoreError("observation_result_event_json_invalid") from exc
        if not isinstance(stored_payload, Mapping) or not isinstance(
            incoming_payload, Mapping
        ):
            raise ObservationStoreError("observation_result_event_payload_not_object")
        if (
            str(existing[2]) != normalized_state
            or _intrinsic_result_event_payload(stored_payload)
            != _intrinsic_result_event_payload(incoming_payload)
        ):
            raise ObservationStoreError("result_event_payload_conflict")
        member = member_for_job(connection, str(existing[0]), job_id)
        return {
            "reserved": False,
            "duplicate_result_ignored": True,
            "gate_id": str(existing[0]),
            "enrolled": member is not None,
            "settled": bool(member and member.get("terminal_at") is not None),
            "ordinal": int(member["ordinal"]) if member is not None else None,
            "emission_pending": _summary_pending(connection, str(existing[0])),
        }

    member_row = connection.execute(
        """
        SELECT j.*, g.gate_start_epoch
        FROM m2_observation_gate_jobs j
        JOIN m2_observation_gates g ON g.gate_id=j.gate_id
        WHERE j.job_id=?
        ORDER BY g.gate_start_epoch DESC LIMIT 1
        """,
        (job_id,),
    )
    member = _fetch_dict(member_row)
    gate = gate_by_id(connection, str(member["gate_id"])) if member is not None else latest_gate(connection)
    if gate is None:
        raise ObservationStoreError("observation_gate_missing")
    gate_id = str(gate["gate_id"])
    if member is not None and member.get("terminal_at") is not None:
        return {
            "reserved": False,
            "duplicate_result_ignored": True,
            "member_already_terminal": True,
            "gate_id": gate_id,
            "enrolled": True,
            "settled": True,
            "ordinal": int(member["ordinal"]),
            "emission_pending": _summary_pending(connection, gate_id),
        }

    connection.execute(
        """
        INSERT INTO m2_observation_result_events(
            claim_identity_hash, gate_id, job_id, observed_state,
            event_sha256, event_payload_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_hash,
            gate_id,
            job_id,
            normalized_state,
            digest,
            payload_text,
            timestamp,
        ),
    )
    return {
        "reserved": True,
        "duplicate_result_ignored": False,
        "gate_id": gate_id,
        "enrolled": member is not None,
        "settled": False,
        "ordinal": int(member["ordinal"]) if member is not None else None,
        "emission_pending": False,
    }


def publish_pending_summaries(config: Any) -> list[str]:
    connection = connect_observation_database(config)
    emitted: list[str] = []
    try:
        rows = connection.execute(
            """
            SELECT gate_id, summary_payload_json, summary_sha256
            FROM m2_observation_gates
            WHERE status='SETTLED' AND summary_ready_at IS NOT NULL
              AND summary_emitted_at IS NULL
            ORDER BY gate_start_epoch
            """
        ).fetchall()
        for gate_id, payload, digest in rows:
            text = str(payload)
            expected = str(digest)
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
                raise ObservationStoreError("summary_journal_hash_mismatch")
            output_dir = Path(str(config.m2_server_canary_observation_output_dir))
            if not output_dir.is_absolute():
                output_dir = Path(str(config.work_path)) / output_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            target = output_dir / f"{gate_id}.json"
            if target.exists():
                if target.read_text(encoding="utf-8") != text:
                    raise ObservationStoreError("summary_output_collision")
            else:
                atomic_write_text(target, text)
            with immediate_transaction(connection):
                changed = connection.execute(
                    """
                    UPDATE m2_observation_gates
                    SET summary_emitted_at=COALESCE(summary_emitted_at, ?),
                        summary_path=CASE WHEN summary_path='' THEN ? ELSE summary_path END,
                        updated_at=?
                    WHERE gate_id=? AND summary_emitted_at IS NULL
                    """,
                    (time.time(), target.name, time.time(), gate_id),
                ).rowcount
            if int(changed or 0) == 1:
                emitted.append(target.name)
    finally:
        connection.close()
    return emitted


def active_gate(connection: sqlite3.Connection) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT * FROM m2_observation_gates WHERE status='ACTIVE' ORDER BY gate_start_epoch DESC LIMIT 1"
    )
    return _fetch_dict(cursor)


def latest_gate(connection: sqlite3.Connection) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT * FROM m2_observation_gates ORDER BY gate_start_epoch DESC LIMIT 1"
    )
    return _fetch_dict(cursor)


def gate_by_id(connection: sqlite3.Connection, gate_id: str) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT * FROM m2_observation_gates WHERE gate_id=?",
        (str(gate_id),),
    )
    return _fetch_dict(cursor)


def member_for_job(
    connection: sqlite3.Connection,
    gate_id: str,
    job_id: str,
) -> dict[str, Any] | None:
    cursor = connection.execute(
        "SELECT * FROM m2_observation_gate_jobs WHERE gate_id=? AND job_id=?",
        (str(gate_id), str(job_id)),
    )
    return _fetch_dict(cursor)


def members_for_gate(connection: sqlite3.Connection, gate_id: str) -> list[dict[str, Any]]:
    cursor = connection.execute(
        "SELECT * FROM m2_observation_gate_jobs WHERE gate_id=? ORDER BY ordinal",
        (str(gate_id),),
    )
    columns = [str(item[0]) for item in cursor.description or []]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def validate_active_runtime(
    connection: sqlite3.Connection,
    runtime_state: Mapping[str, Any],
    *,
    actual_snapshot: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    gate = active_gate(connection) or latest_gate(connection)
    if gate is None:
        raise ObservationStoreError("observation_gate_missing")
    gate_status = str(gate.get("status") or "")
    if gate_status not in {ACTIVE, SETTLED}:
        raise ObservationStoreError("observation_gate_invalidated")
    try:
        _assert_gate_matches_runtime(gate, runtime_state, actual_snapshot=actual_snapshot)
    except ObservationStoreError as exc:
        if gate_status != ACTIVE:
            raise ObservationStoreError("runtime_drift_after_gate_settled") from exc
        invalidated = invalidate_active_gate(
            connection,
            INVALIDATED_RUNTIME,
            evidence={
                "reason_code": exc.reason_code,
                "expected": _gate_fingerprint(gate),
                "actual": _bounded_mapping(actual_snapshot or _runtime_fingerprint(runtime_state)),
            },
            now=now,
        )
        if invalidated is None:
            raise
        raise ObservationStoreError(INVALIDATED_RUNTIME) from exc
    return gate


def update_failure_streaks(
    connection: sqlite3.Connection,
    outcome: Mapping[str, Any],
    *,
    is_oom: bool,
    failed: bool,
    signature: str,
    job_key: str,
) -> dict[str, Any]:
    current = meta_state(connection)
    if not failed:
        current = {
            "oom_streak": 0,
            "oom_job_ids": [],
            "identical_failure_signature": "",
            "identical_failure_streak": 0,
            "identical_failure_job_ids": [],
        }
    else:
        normalized_job = _safe_code(job_key, "unknown_job")
        if is_oom:
            oom_jobs = list(current.get("oom_job_ids") or [])
            if normalized_job not in oom_jobs:
                oom_jobs.append(normalized_job)
            current["oom_job_ids"] = oom_jobs[-50:]
            current["oom_streak"] = len(current["oom_job_ids"])
        else:
            current["oom_streak"] = 0
            current["oom_job_ids"] = []
        normalized_signature = _safe_code(signature, "unknown_failure")
        if normalized_signature == current["identical_failure_signature"]:
            identical_jobs = list(current.get("identical_failure_job_ids") or [])
            if normalized_job not in identical_jobs:
                identical_jobs.append(normalized_job)
            current["identical_failure_job_ids"] = identical_jobs[-50:]
            current["identical_failure_streak"] = len(
                current["identical_failure_job_ids"]
            )
        else:
            current["identical_failure_signature"] = normalized_signature
            current["identical_failure_job_ids"] = [normalized_job]
            current["identical_failure_streak"] = 1
    now = time.time()
    for key, value in current.items():
        stored = json.dumps(value, sort_keys=True) if isinstance(value, list) else str(value)
        connection.execute(
            """
            INSERT INTO m2_observation_meta(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, stored, now),
        )
    return current


def meta_state(connection: sqlite3.Connection) -> dict[str, Any]:
    values = dict(
        connection.execute(
            "SELECT key, value FROM m2_observation_meta WHERE key IN (?, ?, ?, ?, ?)",
            (
                "oom_streak",
                "oom_job_ids",
                "identical_failure_signature",
                "identical_failure_streak",
                "identical_failure_job_ids",
            ),
        ).fetchall()
    )
    def job_ids(key: str) -> list[str]:
        try:
            decoded = json.loads(str(values.get(key) or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = []
        return [str(value) for value in decoded if str(value)] if isinstance(decoded, list) else []

    return {
        "oom_streak": int(values.get("oom_streak") or 0),
        "oom_job_ids": job_ids("oom_job_ids"),
        "identical_failure_signature": str(values.get("identical_failure_signature") or ""),
        "identical_failure_streak": int(values.get("identical_failure_streak") or 0),
        "identical_failure_job_ids": job_ids("identical_failure_job_ids"),
    }


def reset_failure_streaks(connection: sqlite3.Connection) -> None:
    """Reset breaker counters only after controlled recovery evidence is durable."""

    now = time.time()
    for key, value in (
        ("oom_streak", "0"),
        ("oom_job_ids", "[]"),
        ("identical_failure_signature", ""),
        ("identical_failure_streak", "0"),
        ("identical_failure_job_ids", "[]"),
    ):
        connection.execute(
            """
            INSERT INTO m2_observation_meta(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, value, now),
        )


def status_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    gate = active_gate(connection) or latest_gate(connection)
    if gate is None:
        return {
            "gate_id": "",
            "gate_status": "MISSING",
            "gate_baseline_version": "",
            "target_size": 20,
            "enrolled_count": 0,
            "settled_count": 0,
            "strict_verified_count": 0,
            "summary_emitted_at": None,
        }
    strict_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM m2_observation_gate_jobs WHERE gate_id=? AND strict_verified=1",
            (gate["gate_id"],),
        ).fetchone()[0]
    )
    return {
        "gate_id": str(gate["gate_id"]),
        "gate_status": str(gate["status"]),
        "gate_baseline_version": str(gate["baseline_version"]),
        "gate_start_at": str(gate["gate_start_at"]),
        "target_size": int(gate["target_size"]),
        "enrolled_count": int(gate["enrolled_count"]),
        "settled_count": int(gate["settled_count"]),
        "strict_verified_count": strict_count,
        "summary_emitted_at": gate["summary_emitted_at"],
        "invalidation_reason": str(gate["invalidation_reason"]),
    }


def _validate_terminal_member_evidence(member: Mapping[str, Any]) -> None:
    """Verify the immutable terminal row before it can influence a summary."""

    if member.get("terminal_at") is None or str(member.get("final_state") or "") not in TERMINAL_STATES:
        raise ObservationStoreError("terminal_evidence_incomplete")

    def canonical_object(column: str) -> dict[str, Any]:
        serialized = str(member.get(column) or "{}")
        try:
            parsed = json.loads(serialized)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ObservationStoreError("terminal_evidence_json_invalid") from exc
        if not isinstance(parsed, Mapping) or _json(parsed) != serialized:
            raise ObservationStoreError("terminal_evidence_json_invalid")
        return dict(parsed)

    reasons_text = str(member.get("qualification_reason_json") or "[]")
    try:
        reasons = json.loads(reasons_text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ObservationStoreError("terminal_evidence_json_invalid") from exc
    if (
        not isinstance(reasons, list)
        or _json(reasons) != reasons_text
        or any(not isinstance(item, str) for item in reasons)
    ):
        raise ObservationStoreError("terminal_evidence_json_invalid")

    terminal_payload = {
        "final_state": str(member["final_state"]),
        "strict_verified": member.get("strict_verified") == 1,
        "evidence": canonical_object("strict_evidence_json"),
        "reason_codes": reasons,
        "incidents": canonical_object("incident_flags_json"),
        "breaker": canonical_object("breaker_evidence_json"),
    }
    actual = hashlib.sha256(_json(terminal_payload).encode("utf-8")).hexdigest()
    expected = str(member.get("terminal_evidence_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
        raise ObservationStoreError("terminal_evidence_digest_mismatch")


def _prepare_summary_if_ready(
    connection: sqlite3.Connection,
    gate_id: str,
    *,
    now: float,
) -> None:
    gate = gate_by_id(connection, gate_id)
    if gate is None or str(gate["status"]) != ACTIVE:
        return
    if int(gate["enrolled_count"]) != int(gate["target_size"]):
        return
    if int(gate["settled_count"]) != int(gate["target_size"]):
        return
    members = members_for_gate(connection, gate_id)
    for member in members:
        _validate_terminal_member_evidence(member)
    result_events: list[dict[str, Any]] = []
    event_rows = connection.execute(
        """
        SELECT e.event_payload_json, e.event_sha256
        FROM m2_observation_result_events e
        JOIN m2_observation_gate_jobs j
          ON j.gate_id=e.gate_id AND j.job_id=e.job_id
        WHERE e.gate_id=?
        ORDER BY e.created_at, e.claim_identity_hash
        """,
        (gate_id,),
    ).fetchall()
    for payload_text, expected_sha256 in event_rows:
        serialized = str(payload_text)
        actual_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if actual_sha256 != str(expected_sha256):
            raise ObservationStoreError("observation_result_event_digest_mismatch")
        try:
            parsed = json.loads(serialized)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ObservationStoreError("observation_result_event_json_invalid") from exc
        if not isinstance(parsed, Mapping):
            raise ObservationStoreError("observation_result_event_payload_not_object")
        if _json(parsed) != serialized:
            raise ObservationStoreError("observation_result_event_payload_not_canonical")
        result_events.append(dict(parsed))
    payload = _summary_payload(gate, members, result_events=result_events, now=now)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    connection.execute(
        """
        UPDATE m2_observation_gates
        SET status='SETTLED', summary_ready_at=?, summary_payload_json=?,
            summary_sha256=?, updated_at=?
        WHERE gate_id=? AND status='ACTIVE' AND summary_ready_at IS NULL
        """,
        (now, text, digest, now, gate_id),
    )


def _summary_payload(
    gate: Mapping[str, Any],
    members: list[Mapping[str, Any]],
    *,
    result_events: list[Mapping[str, Any]],
    now: float,
) -> dict[str, Any]:
    def result_incident(name: str) -> int:
        count = 0
        for event in result_events:
            normalized = event.get("normalized_outcome")
            outcome = normalized if isinstance(normalized, Mapping) else event.get("outcome")
            if isinstance(outcome, Mapping) and outcome.get(name) is True:
                count += 1
        return count

    states = [str(member.get("final_state") or "") for member in members]
    strict_count = sum(1 for member in members if member.get("strict_verified") == 1)
    strategies: dict[str, int] = {}
    for member in members:
        strategy = str(member.get("processing_strategy") or "UNREPORTED")
        strategies[strategy] = strategies.get(strategy, 0) + 1
    terminal_times = [float(member["terminal_at"]) for member in members if member.get("terminal_at")]
    claimed_after_start = sum(
        1
        for member in members
        if _json_object(member.get("eligibility_evidence_json")).get(
            "claimed_after_gate_start"
        )
        is True
    )
    return {
        "contract": "m2-frozen-observation-summary-v1",
        "gate_id": str(gate["gate_id"]),
        "status": SETTLED,
        "baseline_version": str(gate["baseline_version"]),
        "baseline": _json_object(gate["baseline_json"]),
        "gate_start_at": str(gate["gate_start_at"]),
        "gate_end_at": _utc(max(terminal_times) if terminal_times else now),
        "generated_at": _utc(now),
        "gate_progress": f"{len(members)}/{int(gate['target_size'])}",
        "claimed_after_gate_start_count": claimed_after_start,
        "enrolled": f"{len(members)}/{int(gate['target_size'])}",
        "settled": f"{len(terminal_times)}/{int(gate['target_size'])}",
        "strict_verified_count": strict_count,
        "needs_review_count": states.count("NEEDS_REVIEW"),
        "failed_count": states.count("FAILED"),
        "quarantined_count": result_incident("quarantined"),
        "hallucination_blocked_count": result_incident("hallucination_blocked"),
        "breaker_trip_count": result_incident("breaker_tripped"),
        "checkpoint_resume_count": result_incident("checkpoint_resumed"),
        "oom_event_count": result_incident("oom_event"),
        "false_completed_count": result_incident("incorrect_completion"),
        "source_mutation_count": result_incident("source_mutation_incident"),
        "duplicate_job_count": result_incident("duplicate_job"),
        "duplicate_publish_count": result_incident("duplicate_publish"),
        "output_parse_failure_count": result_incident("output_parse_failure"),
        "checkpoint_loss_count": sum(
            1
            for member in members
            if _json_object(member.get("strict_evidence_json")).get("stage_checkpoint_history_complete") is not True
        ),
        "processing_strategy_counts": dict(sorted(strategies.items())),
        "safety_gate": "PASS" if strict_count == int(gate["target_size"]) else "FAIL",
        "autonomous_completion_rate": f"{strict_count}/{int(gate['target_size'])}",
    }


def _assert_gate_matches_runtime(
    gate: Mapping[str, Any],
    runtime_state: Mapping[str, Any],
    *,
    actual_snapshot: Mapping[str, Any] | None = None,
) -> None:
    if str(runtime_state.get("status") or "") != "ARMED":
        raise ObservationStoreError("runtime_state_not_armed")
    runtime_gate = runtime_state.get("gate")
    supplied_gate_id = (
        str(runtime_gate.get("gate_id") or "")
        if isinstance(runtime_gate, Mapping)
        else ""
    )
    if supplied_gate_id and supplied_gate_id != str(gate.get("gate_id") or ""):
        raise ObservationStoreError("runtime_gate_id_mismatch")
    if gate_identifier(runtime_state) != str(gate.get("gate_id") or ""):
        raise ObservationStoreError("runtime_gate_identity_mismatch")
    try:
        runtime_start = float(runtime_state.get("gate_start_epoch") or 0)
        stored_start = float(gate.get("gate_start_epoch") or 0)
    except (TypeError, ValueError) as exc:
        raise ObservationStoreError("runtime_gate_start_mismatch") from exc
    if (
        runtime_start != stored_start
        or str(runtime_state.get("gate_start_at") or "")
        != str(gate.get("gate_start_at") or "")
        or not isinstance(runtime_gate, Mapping)
        or int(runtime_gate.get("target") or 0) != int(gate.get("target_size") or 0)
    ):
        raise ObservationStoreError("runtime_gate_start_or_target_mismatch")
    expected = _gate_fingerprint(gate)
    actual = dict(actual_snapshot or _runtime_fingerprint(runtime_state))
    for key, value in expected.items():
        if actual.get(key) != value:
            raise ObservationStoreError(f"runtime_drift_{key}")


def _validated_runtime_baseline(runtime_state: Mapping[str, Any]) -> dict[str, Any]:
    if str(runtime_state.get("status") or "") != "ARMED":
        raise ObservationStoreError("runtime_state_not_armed")
    baseline = runtime_state.get("baseline")
    if not isinstance(baseline, Mapping):
        raise ObservationStoreError("runtime_baseline_missing")
    required = (
        "worker_commit_sha",
        "webui_commit_sha",
        "worker_image_id",
        "worker_container_id",
        "worker_runtime_instance_fingerprint",
        "configuration_fingerprint",
        "decision_schema_version",
        "eligibility_policy_version",
    )
    if any(baseline.get(key) in (None, "") for key in required):
        raise ObservationStoreError("runtime_baseline_incomplete")
    if str(baseline["eligibility_policy_version"]) != ELIGIBILITY_POLICY_VERSION:
        raise ObservationStoreError("eligibility_policy_version_mismatch")
    return dict(baseline)


def _gate_fingerprint(gate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "baseline_version": str(gate.get("baseline_version") or ""),
        "worker_sha": str(gate.get("worker_sha") or ""),
        "webui_sha": str(gate.get("webui_sha") or ""),
        "container_image_id": str(gate.get("container_image_id") or ""),
        "worker_container_id": str(gate.get("worker_container_id") or ""),
        "worker_runtime_instance_fingerprint": str(
            gate.get("worker_runtime_instance_fingerprint") or ""
        ),
        "configuration_fingerprint": str(gate.get("configuration_fingerprint") or ""),
        "decision_schema_version": int(gate.get("decision_schema_version") or 0),
        "eligibility_policy_version": str(gate.get("eligibility_policy_version") or ""),
    }


def _runtime_fingerprint(runtime_state: Mapping[str, Any]) -> dict[str, Any]:
    baseline = runtime_state.get("baseline")
    data = baseline if isinstance(baseline, Mapping) else {}
    return {
        "baseline_version": str(runtime_state.get("gate_baseline_version") or ""),
        "worker_sha": str(data.get("worker_commit_sha") or ""),
        "webui_sha": str(data.get("webui_commit_sha") or ""),
        "container_image_id": str(data.get("worker_image_id") or ""),
        "worker_container_id": str(data.get("worker_container_id") or ""),
        "worker_runtime_instance_fingerprint": str(
            data.get("worker_runtime_instance_fingerprint") or ""
        ),
        "configuration_fingerprint": str(data.get("configuration_fingerprint") or ""),
        "decision_schema_version": int(data.get("decision_schema_version") or 0),
        "eligibility_policy_version": str(data.get("eligibility_policy_version") or ""),
    }


def _terminal_result_values(
    evidence: Mapping[str, Any],
    missing: set[str],
    outcome: Mapping[str, Any],
) -> dict[str, str]:
    def result(key: str, pass_label: str = "PASS", fail_label: str = "FAIL") -> str:
        if key in missing:
            return "MISSING"
        return pass_label if evidence.get(key) is True else fail_label

    checkpoint = result("stage_checkpoint_history_complete", "COMPLETE", "INCOMPLETE")
    if bool(outcome.get("checkpoint_resumed")):
        checkpoint = "RESUMED_" + checkpoint
    retry_clear = evidence.get("no_unresolved_retry_quarantine_fallback") is True
    return {
        "output_parse_result": result("output_parse_pass"),
        "hard_qc_result": result("hard_qc_pass"),
        "hallucination_result": result("hallucination_validation_pass"),
        "source_checksum_result": result("source_checksum_unchanged", "UNCHANGED", "CHANGED_OR_UNPROVEN"),
        "duplicate_job_result": result("no_duplicate_job", "NONE", "DUPLICATE_OR_UNPROVEN"),
        "duplicate_publish_result": result("no_duplicate_publish", "NONE", "DUPLICATE_OR_UNPROVEN"),
        "checkpoint_resume_result": checkpoint,
        "retry_fallback_result": "CLEAR" if retry_clear else ("MISSING" if "no_unresolved_retry_quarantine_fallback" in missing else "UNRESOLVED"),
    }


def _upsert_supplemental(
    connection: sqlite3.Connection,
    *,
    gate_id: str,
    job_id: str,
    claim_identity_hash: str,
    exclusion_reason: str,
    claimed_at: float | None,
    now: float,
    last_state: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO m2_observation_supplemental(
            gate_id, job_id, claim_identity_hash, exclusion_reason, claimed_at,
            last_state, last_observed_at, evidence_json, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gate_id, job_id) DO UPDATE SET
            last_state=CASE WHEN excluded.last_state='' THEN last_state ELSE excluded.last_state END,
            last_observed_at=COALESCE(excluded.last_observed_at, last_observed_at),
            evidence_json=CASE WHEN excluded.evidence_json='{}' THEN evidence_json ELSE excluded.evidence_json END,
            updated_at=excluded.updated_at
        """,
        (
            gate_id,
            job_id,
            claim_identity_hash,
            _safe_code(exclusion_reason, "excluded"),
            claimed_at,
            _safe_code(last_state, "") if last_state else "",
            now if last_state else None,
            _json(_bounded_mapping(evidence or {})),
            now,
            now,
        ),
    )


def _summary_pending(connection: sqlite3.Connection, gate_id: str) -> bool:
    row = connection.execute(
        "SELECT summary_ready_at, summary_emitted_at FROM m2_observation_gates WHERE gate_id=?",
        (gate_id,),
    ).fetchone()
    return bool(row and row[0] is not None and row[1] is None)


def _obligation_started_before_gate(
    connection: sqlite3.Connection,
    obligation_id: str,
    gate_start_epoch: float,
    *,
    current_claim_identity: str,
) -> bool:
    """Exclude a pre-gate job retry while allowing standalone store tests."""

    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_delivery_attempts'"
    ).fetchone()
    if table is None:
        return False
    row = connection.execute(
        """
        SELECT MIN(started_at)
        FROM ai_delivery_attempts
        WHERE obligation_id=? AND attempt_id<>?
        """,
        (str(obligation_id), str(current_claim_identity)),
    ).fetchone()
    return bool(row and row[0] is not None and float(row[0]) <= float(gate_start_epoch))


def _fetch_dict(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [str(item[0]) for item in cursor.description or []]
    return dict(zip(columns, row, strict=True))


def _identity_hash(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ObservationStoreError("job_identity_invalid")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_code(value: Any, default: str) -> str:
    normalized = str(value or default).strip()
    if not normalized:
        return default
    if _SAFE_CODE_RE.fullmatch(normalized):
        return normalized[:160]
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", normalized).strip("_.:-")[:160] or default


def _bounded_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
        safe_key = _safe_code(key, "field")
        if isinstance(item, Mapping):
            result[safe_key] = _bounded_mapping(item)
        elif isinstance(item, (list, tuple)):
            result[safe_key] = [
                _safe_code(element, "") if isinstance(element, str) else element
                for element in list(item)[:32]
                if isinstance(element, (str, int, float, bool, type(None)))
            ]
        elif isinstance(item, str):
            result[safe_key] = _safe_code(item, "")
        elif isinstance(item, (int, float, bool, type(None))):
            result[safe_key] = item
    return result


def _intrinsic_result_event_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return replay-stable result facts, excluding projected streak counters."""

    intrinsic: dict[str, Any] = {}
    for key in ("outcome", "normalized_outcome", "qualification"):
        if key not in value:
            continue
        item = value.get(key)
        if isinstance(item, Mapping):
            normalized = _bounded_mapping(item)
            if key in {"outcome", "normalized_outcome"}:
                # The breaker flag can be projected from the durable global
                # streak, so a replay after the first reservation may observe
                # a different projection. The underlying failure facts and
                # qualification remain intrinsic and must match exactly.
                normalized.pop("breaker_tripped", None)
            intrinsic[key] = normalized
        else:
            intrinsic[key] = item
    return intrinsic


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_runtime_manifest_payload(runtime_state: Mapping[str, Any]) -> dict[str, Any]:
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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")
