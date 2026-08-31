from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Iterator, Mapping, Sequence
import uuid


PIPELINE_SCHEMA_VERSION = 1
PIPELINE_STATES = (
    "DISCOVERED",
    "STABILIZING",
    "ANALYZING",
    "QUEUED",
    "SUBTITLE_DETECTION",
    "ASR",
    "TRANSLATING",
    "POST_PROCESSING",
    "QC",
    "MUXING",
    "RETRYING",
    "NEEDS_REVIEW",
    "FAILED",
    "COMPLETED",
)
TERMINAL_PIPELINE_STATES = frozenset({"FAILED", "COMPLETED"})
INGEST_PIPELINE_STATES = frozenset(
    {"DISCOVERED", "STABILIZING", "ANALYZING", "QUEUED"}
)
EXECUTION_PIPELINE_STATES = frozenset(
    {
        "ANALYZING",
        "SUBTITLE_DETECTION",
        "ASR",
        "TRANSLATING",
        "POST_PROCESSING",
        "QC",
        "MUXING",
    }
)
STAGE_ATTEMPT_STATUSES = frozenset(
    {
        "RUNNING",
        "SUCCEEDED",
        "RETRYABLE_FAILURE",
        "PERMANENT_FAILURE",
        "INTERRUPTED",
        "NEEDS_REVIEW",
    }
)
STAGE_ERROR_CLASSES = frozenset(
    {"", "transient", "resource", "quality", "permanent", "interrupted"}
)

ALLOWED_PIPELINE_TRANSITIONS: dict[str, frozenset[str]] = {
    "DISCOVERED": frozenset({"STABILIZING", "FAILED"}),
    "STABILIZING": frozenset({"ANALYZING", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "ANALYZING": frozenset({"STABILIZING", "QUEUED", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "QUEUED": frozenset({"SUBTITLE_DETECTION", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "SUBTITLE_DETECTION": frozenset(
        {"ASR", "TRANSLATING", "POST_PROCESSING", "QC", "RETRYING", "NEEDS_REVIEW", "FAILED"}
    ),
    "ASR": frozenset({"TRANSLATING", "POST_PROCESSING", "QC", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "TRANSLATING": frozenset({"POST_PROCESSING", "QC", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "POST_PROCESSING": frozenset({"QC", "MUXING", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "QC": frozenset({"MUXING", "COMPLETED", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "MUXING": frozenset({"COMPLETED", "RETRYING", "NEEDS_REVIEW", "FAILED"}),
    "RETRYING": frozenset(
        {
            "STABILIZING",
            "ANALYZING",
            "QUEUED",
            "SUBTITLE_DETECTION",
            "ASR",
            "TRANSLATING",
            "POST_PROCESSING",
            "QC",
            "MUXING",
            "NEEDS_REVIEW",
            "FAILED",
        }
    ),
    "NEEDS_REVIEW": frozenset({"RETRYING", "FAILED"}),
    "FAILED": frozenset(),
    "COMPLETED": frozenset(),
}


class PipelineStateError(RuntimeError):
    pass


class InvalidPipelineTransition(PipelineStateError):
    pass


class TerminalPipelineStateError(PipelineStateError):
    pass


class PipelineStateConflict(PipelineStateError):
    pass


class StageAttemptError(PipelineStateError):
    pass


def ensure_pipeline_state_schema(connection: sqlite3.Connection) -> None:
    """Install the M1 job/state schema in the existing scanner SQLite DB."""

    connection.execute("PRAGMA foreign_keys=ON")
    states_sql = ", ".join(f"'{state}'" for state in PIPELINE_STATES)
    attempts_sql = ", ".join(f"'{status}'" for status in sorted(STAGE_ATTEMPT_STATUSES))
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS pipeline_schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_jobs (
            job_id TEXT PRIMARY KEY,
            canonical_path TEXT NOT NULL,
            media_revision TEXT NOT NULL UNIQUE,
            media_fingerprint TEXT NOT NULL UNIQUE,
            identity_kind TEXT NOT NULL,
            media_size INTEGER NOT NULL,
            media_mtime_ns INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ({states_sql})),
            state_version INTEGER NOT NULL DEFAULT 1,
            active_stage_attempt_id TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL NOT NULL DEFAULT 0,
            resume_state TEXT NOT NULL DEFAULT '',
            terminal_reason_code TEXT NOT NULL DEFAULT '',
            terminal_error_json TEXT NOT NULL DEFAULT '{{}}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_path_updated
            ON pipeline_jobs(canonical_path, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_state_updated
            ON pipeline_jobs(state, updated_at);

        CREATE TABLE IF NOT EXISTS pipeline_job_paths (
            job_id TEXT NOT NULL,
            canonical_path TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            PRIMARY KEY(job_id, canonical_path),
            FOREIGN KEY(job_id) REFERENCES pipeline_jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_job_paths_path
            ON pipeline_job_paths(canonical_path, last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS pipeline_ingest_observations (
            canonical_path TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            media_revision TEXT NOT NULL,
            media_fingerprint TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            state TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            stable_since_at REAL NOT NULL,
            observation_count INTEGER NOT NULL DEFAULT 1,
            last_event_type TEXT NOT NULL DEFAULT '',
            close_observed INTEGER NOT NULL DEFAULT 0,
            evidence_json TEXT NOT NULL DEFAULT '{{}}',
            FOREIGN KEY(job_id) REFERENCES pipeline_jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_ingest_state_seen
            ON pipeline_ingest_observations(state, last_seen_at);

        CREATE TABLE IF NOT EXISTS pipeline_job_transitions (
            transition_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL CHECK(to_state IN ({states_sql})),
            reason_code TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            actor TEXT NOT NULL DEFAULT 'system',
            stage_attempt_id TEXT,
            idempotency_key TEXT,
            created_at REAL NOT NULL,
            UNIQUE(job_id, sequence),
            UNIQUE(job_id, idempotency_key),
            FOREIGN KEY(job_id) REFERENCES pipeline_jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_transitions_job_created
            ON pipeline_job_transitions(job_id, created_at);

        CREATE TABLE IF NOT EXISTS pipeline_stage_attempts (
            stage_attempt_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ({states_sql})),
            attempt_number INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ({attempts_sql})),
            input_json TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            output_json TEXT NOT NULL DEFAULT '{{}}',
            outputs_verified INTEGER NOT NULL DEFAULT 0,
            model_json TEXT NOT NULL DEFAULT '{{}}',
            retry_count INTEGER NOT NULL DEFAULT 0,
            retry_limit INTEGER NOT NULL DEFAULT 0,
            timeout_seconds REAL NOT NULL DEFAULT 0,
            checkpoint_json TEXT NOT NULL DEFAULT '{{}}',
            checkpoint_sha256 TEXT NOT NULL DEFAULT '',
            error_class TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_json TEXT NOT NULL DEFAULT '{{}}',
            idempotency_key TEXT,
            started_at REAL NOT NULL,
            heartbeat_at REAL NOT NULL,
            finished_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            UNIQUE(job_id, stage, attempt_number),
            UNIQUE(job_id, idempotency_key),
            FOREIGN KEY(job_id) REFERENCES pipeline_jobs(job_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_attempts_job_stage
            ON pipeline_stage_attempts(job_id, stage, attempt_number DESC);
        CREATE INDEX IF NOT EXISTS idx_pipeline_attempts_running
            ON pipeline_stage_attempts(status, heartbeat_at);

        CREATE TABLE IF NOT EXISTS pipeline_stage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            stage_attempt_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at REAL NOT NULL,
            FOREIGN KEY(job_id) REFERENCES pipeline_jobs(job_id) ON DELETE CASCADE,
            FOREIGN KEY(stage_attempt_id) REFERENCES pipeline_stage_attempts(stage_attempt_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_stage_events_attempt
            ON pipeline_stage_events(stage_attempt_id, id);

        CREATE TABLE IF NOT EXISTS pipeline_operation_idempotency (
            job_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            operation_kind TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            stage_attempt_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY(job_id, idempotency_key),
            FOREIGN KEY(job_id) REFERENCES pipeline_jobs(job_id) ON DELETE CASCADE,
            FOREIGN KEY(stage_attempt_id) REFERENCES pipeline_stage_attempts(stage_attempt_id) ON DELETE CASCADE
        );
        """
    )
    now = time.time()
    connection.execute(
        """
        INSERT INTO pipeline_schema_meta(key, value, updated_at)
        VALUES('schema_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(PIPELINE_SCHEMA_VERSION), now),
    )


class PipelineJobStore:
    """Durable M1 ingest, state-transition, attempt, and recovery store."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        owns_connection: bool | None = None,
    ) -> None:
        if connection is None and path is None:
            raise ValueError("pipeline job store requires a path or SQLite connection")
        if connection is None:
            database = Path(path or "")
            database.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(database, timeout=60)
            connection.execute("PRAGMA busy_timeout=60000")
            current = connection.execute("PRAGMA journal_mode").fetchone()
            if str(current[0] if current else "").casefold() != "wal":
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA wal_autocheckpoint=1000")
            connection.execute("PRAGMA temp_store=MEMORY")
            inferred_ownership = True
        else:
            inferred_ownership = False
        self._conn = connection
        self._owns_connection = inferred_ownership if owns_connection is None else bool(owns_connection)
        schema_ready = self._conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='pipeline_operation_idempotency'
            """
        ).fetchone()
        if schema_ready is None:
            ensure_pipeline_state_schema(self._conn)
            if self._owns_connection:
                self._conn.commit()

    @classmethod
    def from_config(cls, config: Any) -> "PipelineJobStore":
        configured = Path(str(getattr(config, "scanner_state_path", "scanner_state.sqlite3")))
        database = configured if configured.is_absolute() else Path(config.work_path) / configured
        return cls(database)

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> "PipelineJobStore":
        return cls(connection=connection, owns_connection=False)

    @property
    def in_transaction(self) -> bool:
        return bool(self._conn.in_transaction)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    def __enter__(self) -> "PipelineJobStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()

    @contextmanager
    def _savepoint(self) -> Iterator[None]:
        started_transaction = not self._conn.in_transaction
        if started_transaction:
            self._conn.execute("BEGIN")
        name = f"pipeline_{uuid.uuid4().hex}"
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._conn.execute(f"RELEASE SAVEPOINT {name}")
            if started_transaction:
                self._conn.rollback()
            raise
        else:
            self._conn.execute(f"RELEASE SAVEPOINT {name}")

    @staticmethod
    def _canonical_path(path: str | Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _structured(cls, value: Mapping[str, Any] | None, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"{field} must be a mapping")
        result = dict(value)
        cls._json(result)
        return result

    @staticmethod
    def _confidence(value: float) -> float:
        result = float(value)
        if not 0.0 <= result <= 1.0:
            raise ValueError("confidence must be between zero and one")
        return result

    @staticmethod
    def _reason(reason_code: str) -> str:
        result = str(reason_code).strip()
        if not result:
            raise ValueError("reason_code is required")
        return result

    @staticmethod
    def _row(cursor: sqlite3.Cursor, row: Sequence[Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {column[0]: value for column, value in zip(cursor.description or (), row)}

    def _fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        cursor = self._conn.execute(sql, parameters)
        return self._row(cursor, cursor.fetchone())

    def _fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = self._conn.execute(sql, parameters)
        return [self._row(cursor, row) or {} for row in cursor.fetchall()]

    @classmethod
    def _decode_row(cls, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for key in tuple(result):
            if key.endswith("_json"):
                decoded_key = key[:-5]
                try:
                    result[decoded_key] = json.loads(str(result[key] or "{}"))
                except (TypeError, ValueError):
                    result[decoded_key] = {}
        for key in ("outputs_verified", "close_observed"):
            if key in result:
                result[key] = bool(result[key])
        if "canonical_path" in result and "path" not in result:
            result["path"] = result["canonical_path"]
        if "last_event_type" in result and "event_type" not in result:
            result["event_type"] = result["last_event_type"]
        return result

    @classmethod
    def _media_identity(
        cls,
        path: str | Path,
        *,
        size: int | None,
        mtime_ns: int | None,
    ) -> tuple[str, int, int, str, str, str]:
        canonical = cls._canonical_path(path)
        stat_result = None
        try:
            stat_result = os.stat(canonical, follow_symlinks=True)
        except OSError:
            pass
        resolved_size = int(size if size is not None else getattr(stat_result, "st_size", 0))
        resolved_mtime = int(
            mtime_ns if mtime_ns is not None else getattr(stat_result, "st_mtime_ns", 0)
        )
        if resolved_size < 0 or resolved_mtime < 0:
            raise ValueError("size and mtime_ns must be non-negative")
        device = int(getattr(stat_result, "st_dev", 0) or 0)
        inode = int(getattr(stat_result, "st_ino", 0) or 0)
        if device and inode:
            identity_kind = "filesystem_object"
            identity = {"dev": device, "ino": inode, "size": resolved_size, "mtime_ns": resolved_mtime}
        else:
            identity_kind = "path_stat_fallback"
            identity = {"path": canonical, "size": resolved_size, "mtime_ns": resolved_mtime}
        fingerprint = hashlib.sha256(cls._json(identity).encode("utf-8")).hexdigest()
        revision_payload = {
            "fingerprint": fingerprint,
            "size": resolved_size,
            "mtime_ns": resolved_mtime,
        }
        revision = hashlib.sha256(cls._json(revision_payload).encode("utf-8")).hexdigest()
        return canonical, resolved_size, resolved_mtime, identity_kind, fingerprint, revision

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._decode_row(
            self._fetchone("SELECT * FROM pipeline_jobs WHERE job_id = ?", (str(job_id),))
        )

    def list_transitions(self, job_id: str) -> list[dict[str, Any]]:
        return [
            self._decode_row(row) or {}
            for row in self._fetchall(
                "SELECT * FROM pipeline_job_transitions WHERE job_id = ? ORDER BY sequence",
                (str(job_id),),
            )
        ]

    def list_stage_attempts(self, job_id: str, stage: str | None = None) -> list[dict[str, Any]]:
        if stage is None:
            rows = self._fetchall(
                "SELECT * FROM pipeline_stage_attempts WHERE job_id = ? ORDER BY started_at, attempt_number",
                (str(job_id),),
            )
        else:
            rows = self._fetchall(
                "SELECT * FROM pipeline_stage_attempts WHERE job_id = ? AND stage = ? ORDER BY attempt_number",
                (str(job_id), str(stage).upper()),
            )
        return [self._decode_row(row) or {} for row in rows]

    def _record_transition(
        self,
        job: Mapping[str, Any],
        to_state: str,
        *,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
        actor: str = "system",
        stage_attempt_id: str | None = None,
        idempotency_key: str | None = None,
        allow_completed: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        target = str(to_state).upper()
        if target not in PIPELINE_STATES:
            raise InvalidPipelineTransition(f"unknown pipeline state: {target}")
        if target == "COMPLETED" and not allow_completed:
            raise InvalidPipelineTransition("COMPLETED requires complete_job verified delivery evidence")
        current = str(job["state"])
        if idempotency_key:
            existing = self._decode_row(
                self._fetchone(
                    "SELECT * FROM pipeline_job_transitions WHERE job_id = ? AND idempotency_key = ?",
                    (str(job["job_id"]), str(idempotency_key)),
                )
            )
            if existing is not None:
                if (
                    existing["to_state"] != target
                    or existing["reason_code"] != self._reason(reason_code)
                    or existing.get("evidence") != self._structured(evidence, "evidence")
                    or float(existing["confidence"]) != self._confidence(confidence)
                ):
                    raise PipelineStateConflict("idempotency key was already used for a different transition")
                current_job = self.get_job(str(job["job_id"]))
                if current_job is None:
                    raise PipelineStateConflict("job disappeared during idempotent transition")
                return current_job
        if current in TERMINAL_PIPELINE_STATES:
            raise TerminalPipelineStateError(f"job {job['job_id']} is terminal in {current}")
        if target == current:
            current_job = self.get_job(str(job["job_id"]))
            if current_job is None:
                raise PipelineStateConflict("job disappeared during state transition")
            return current_job
        if target not in ALLOWED_PIPELINE_TRANSITIONS[current]:
            raise InvalidPipelineTransition(f"illegal pipeline transition {current} -> {target}")
        timestamp = float(time.time() if now is None else now)
        evidence_payload = self._structured(evidence, "evidence")
        reason = self._reason(reason_code)
        score = self._confidence(confidence)
        sequence = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM pipeline_job_transitions WHERE job_id = ?",
                (str(job["job_id"]),),
            ).fetchone()[0]
        )
        updated = self._conn.execute(
            """
            UPDATE pipeline_jobs
            SET state = ?, state_version = state_version + 1, updated_at = ?,
                completed_at = CASE WHEN ? IN ('FAILED', 'COMPLETED') THEN ? ELSE completed_at END,
                terminal_reason_code = CASE WHEN ? IN ('FAILED', 'COMPLETED') THEN ? ELSE terminal_reason_code END
            WHERE job_id = ? AND state = ? AND state_version = ?
            """,
            (
                target,
                timestamp,
                target,
                timestamp,
                target,
                reason,
                str(job["job_id"]),
                current,
                int(job["state_version"]),
            ),
        ).rowcount
        if updated != 1:
            raise PipelineStateConflict("job state/version changed concurrently")
        self._conn.execute(
            """
            INSERT INTO pipeline_job_transitions(
                transition_id, job_id, sequence, from_state, to_state, reason_code,
                evidence_json, confidence, actor, stage_attempt_id, idempotency_key, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                str(job["job_id"]),
                sequence,
                current,
                target,
                reason,
                self._json(evidence_payload),
                score,
                str(actor or "system"),
                stage_attempt_id,
                str(idempotency_key) if idempotency_key else None,
                timestamp,
            ),
        )
        result = self.get_job(str(job["job_id"]))
        if result is None:
            raise PipelineStateConflict("job disappeared after state transition")
        return result

    def transition_job(
        self,
        job_id: str,
        to_state: str,
        *,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
        expected_state: str | None = None,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        stage_attempt_id: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        with self._savepoint():
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(f"unknown pipeline job: {job_id}")
            if expected_state is not None and job["state"] != str(expected_state).upper():
                raise PipelineStateConflict(
                    f"expected state {expected_state}, found {job['state']}"
                )
            if expected_version is not None and int(job["state_version"]) != int(expected_version):
                raise PipelineStateConflict(
                    f"expected version {expected_version}, found {job['state_version']}"
                )
            return self._record_transition(
                job,
                to_state,
                reason_code=reason_code,
                evidence=evidence,
                confidence=confidence,
                actor=actor,
                stage_attempt_id=stage_attempt_id,
                idempotency_key=idempotency_key,
            )

    def _insert_job(
        self,
        canonical: str,
        size: int,
        mtime_ns: int,
        identity_kind: str,
        fingerprint: str,
        revision: str,
        *,
        now: float,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        self._conn.execute(
            """
            INSERT INTO pipeline_jobs(
                job_id, canonical_path, media_revision, media_fingerprint, identity_kind,
                media_size, media_mtime_ns, state, state_version, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', 1, ?, ?)
            """,
            (job_id, canonical, revision, fingerprint, identity_kind, size, mtime_ns, now, now),
        )
        self._conn.execute(
            """
            INSERT INTO pipeline_job_paths(job_id, canonical_path, first_seen_at, last_seen_at)
            VALUES(?, ?, ?, ?)
            """,
            (job_id, canonical, now, now),
        )
        self._conn.execute(
            """
            INSERT INTO pipeline_job_transitions(
                transition_id, job_id, sequence, from_state, to_state, reason_code,
                evidence_json, confidence, actor, created_at
            ) VALUES(?, ?, 1, '', 'DISCOVERED', ?, ?, ?, 'ingest', ?)
            """,
            (
                uuid.uuid4().hex,
                job_id,
                self._reason(reason_code),
                self._json(self._structured(evidence, "evidence")),
                self._confidence(confidence),
                now,
            ),
        )
        job = self.get_job(job_id)
        if job is None:
            raise PipelineStateConflict("failed to create pipeline job")
        return job

    def observe_ingest(
        self,
        path: str | Path,
        *,
        size: int | None = None,
        mtime_ns: int | None = None,
        event_type: str = "",
        observed_at: float | None = None,
        state: str = "DISCOVERED",
        evidence: Mapping[str, Any] | None = None,
        confidence: float = 1.0,
        reason_code: str = "media_discovered",
    ) -> dict[str, Any]:
        requested_state = str(state or "DISCOVERED").upper()
        if requested_state not in INGEST_PIPELINE_STATES:
            raise ValueError(f"ingest observation state is invalid: {requested_state}")
        canonical, resolved_size, resolved_mtime, identity_kind, fingerprint, revision = (
            self._media_identity(path, size=size, mtime_ns=mtime_ns)
        )
        timestamp = float(time.time() if observed_at is None else observed_at)
        event = str(event_type or "")
        payload = self._structured(evidence, "evidence")
        payload.update(
            {
                "canonical_path": canonical,
                "size": resolved_size,
                "mtime_ns": resolved_mtime,
                "event_type": event,
                "media_revision": revision,
            }
        )
        close_observed = int(
            event.casefold() in {"closed", "close", "close_write", "closed_write", "moved_to"}
        )
        with self._savepoint():
            observation = self._decode_row(
                self._fetchone(
                    "SELECT * FROM pipeline_ingest_observations WHERE canonical_path = ?",
                    (canonical,),
                )
            )
            job = self._decode_row(
                self._fetchone("SELECT * FROM pipeline_jobs WHERE media_fingerprint = ?", (fingerprint,))
            )
            stable_since = timestamp
            observation_count = 1
            first_seen = timestamp
            if observation is not None:
                observation_count = int(observation["observation_count"]) + 1
                first_seen = float(observation["first_seen_at"])
                if (
                    int(observation["size"]) == resolved_size
                    and int(observation["mtime_ns"]) == resolved_mtime
                ):
                    stable_since = float(observation["stable_since_at"])
                close_observed = max(close_observed, int(bool(observation["close_observed"])))
                previous_job = self.get_job(str(observation["job_id"]))
                if job is None and previous_job is not None:
                    if previous_job["state"] in {"DISCOVERED", "STABILIZING", "ANALYZING"}:
                        self._conn.execute(
                            """
                            UPDATE pipeline_jobs
                            SET media_revision = ?, media_fingerprint = ?, identity_kind = ?,
                                media_size = ?, media_mtime_ns = ?, canonical_path = ?, updated_at = ?
                            WHERE job_id = ?
                            """,
                            (
                                revision,
                                fingerprint,
                                identity_kind,
                                resolved_size,
                                resolved_mtime,
                                canonical,
                                timestamp,
                                previous_job["job_id"],
                            ),
                        )
                        job = self.get_job(str(previous_job["job_id"]))
                    elif previous_job["state"] not in TERMINAL_PIPELINE_STATES:
                        failed = self._record_transition(
                            previous_job,
                            "FAILED",
                            reason_code="media_changed_during_pipeline",
                            evidence=payload,
                            confidence=1.0,
                            actor="ingest",
                            now=timestamp,
                        )
                        self._conn.execute(
                            "UPDATE pipeline_jobs SET terminal_error_json = ? WHERE job_id = ?",
                            (
                                self._json(
                                    {
                                        "error_class": "permanent",
                                        "error_code": "media_revision_changed",
                                        "message": "source identity changed after the job was queued",
                                    }
                                ),
                                failed["job_id"],
                            ),
                        )
            if job is None:
                job = self._insert_job(
                    canonical,
                    resolved_size,
                    resolved_mtime,
                    identity_kind,
                    fingerprint,
                    revision,
                    now=timestamp,
                    reason_code=reason_code,
                    evidence=payload,
                    confidence=confidence,
                )
            self._conn.execute(
                """
                INSERT INTO pipeline_job_paths(job_id, canonical_path, first_seen_at, last_seen_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(job_id, canonical_path) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                (job["job_id"], canonical, timestamp, timestamp),
            )
            ingest_order = ("DISCOVERED", "STABILIZING", "ANALYZING", "QUEUED")
            effective_state = requested_state
            if job["state"] in ingest_order:
                current_index = ingest_order.index(str(job["state"]))
                requested_index = ingest_order.index(requested_state)
                if requested_index > current_index:
                    path_states = self._shortest_path(str(job["state"]), requested_state)
                    if not path_states:
                        raise InvalidPipelineTransition(
                            f"cannot apply ingest state {requested_state} from {job['state']}"
                        )
                    for index, target in enumerate(path_states):
                        transition_reason = {
                            "STABILIZING": "ingest_stabilization_started",
                            "ANALYZING": "ingest_analysis_started",
                            "QUEUED": "ingest_validation_passed",
                        }.get(target, "ingest_observation_state")
                        job = self._record_transition(
                            job,
                            target,
                            reason_code=transition_reason,
                            evidence=payload,
                            confidence=confidence,
                            actor="ingest",
                            idempotency_key=None,
                            now=timestamp + (index * 0.000001),
                        )
                else:
                    # Duplicate/late filesystem events never regress an accepted revision.
                    effective_state = str(job["state"])
            else:
                effective_state = "QUEUED"
            self._conn.execute(
                """
                INSERT INTO pipeline_ingest_observations(
                    canonical_path, job_id, media_revision, media_fingerprint, size, mtime_ns,
                    state, first_seen_at, last_seen_at, stable_since_at, observation_count,
                    last_event_type, close_observed, evidence_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    job_id=excluded.job_id, media_revision=excluded.media_revision,
                    media_fingerprint=excluded.media_fingerprint, size=excluded.size,
                    mtime_ns=excluded.mtime_ns, state=excluded.state,
                    first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at,
                    stable_since_at=excluded.stable_since_at,
                    observation_count=excluded.observation_count,
                    last_event_type=excluded.last_event_type,
                    close_observed=MAX(pipeline_ingest_observations.close_observed, excluded.close_observed),
                    evidence_json=excluded.evidence_json
                """,
                (
                    canonical,
                    job["job_id"],
                    revision,
                    fingerprint,
                    resolved_size,
                    resolved_mtime,
                    effective_state,
                    first_seen,
                    timestamp,
                    stable_since,
                    observation_count,
                    event,
                    close_observed,
                    self._json(payload),
                ),
            )
            result = self._decode_row(
                self._fetchone(
                    "SELECT * FROM pipeline_ingest_observations WHERE canonical_path = ?",
                    (canonical,),
                )
            )
            if result is None:
                raise PipelineStateConflict("ingest observation disappeared")
            return result

    def upsert_ingest_observation(
        self,
        path: str | Path,
        size: int,
        mtime_ns: int,
        observed_at: float | None = None,
        event_type: str = "",
        state: str = "stabilizing",
    ) -> dict[str, Any]:
        return self.observe_ingest(
            path,
            size=size,
            mtime_ns=mtime_ns,
            observed_at=observed_at,
            event_type=event_type,
            state=state,
            evidence={
                "source": "filesystem_event",
                "write_complete_and_probe_verified": str(state).casefold() == "queued",
            },
            confidence=1.0,
        )

    def iter_pending_ingest_observations(self) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT * FROM pipeline_ingest_observations
            WHERE state IN ('DISCOVERED', 'STABILIZING', 'ANALYZING')
            ORDER BY first_seen_at, canonical_path
            """
        )
        return [self._decode_row(row) or {} for row in rows]

    def clear_ingest_observation(self, path: str | Path) -> bool:
        canonical = self._canonical_path(path)
        return bool(
            self._conn.execute(
                "DELETE FROM pipeline_ingest_observations WHERE canonical_path = ?", (canonical,)
            ).rowcount
        )

    def job_for_path(
        self,
        path: str | Path,
        *,
        size: int | None = None,
        mtime_ns: int | None = None,
        create: bool = False,
        initial_state: str = "DISCOVERED",
        reason_code: str = "legacy_job_adopted",
        evidence: Mapping[str, Any] | None = None,
        confidence: float = 1.0,
    ) -> dict[str, Any] | None:
        canonical, resolved_size, resolved_mtime, _kind, fingerprint, _revision = self._media_identity(
            path, size=size, mtime_ns=mtime_ns
        )
        job = self._decode_row(
            self._fetchone("SELECT * FROM pipeline_jobs WHERE media_fingerprint = ?", (fingerprint,))
        )
        if job is None and size is None and mtime_ns is None:
            job = self._decode_row(
                self._fetchone(
                    """
                    SELECT j.* FROM pipeline_job_paths p
                    JOIN pipeline_jobs j ON j.job_id=p.job_id
                    WHERE p.canonical_path=? ORDER BY p.last_seen_at DESC LIMIT 1
                    """,
                    (canonical,),
                )
            )
        if job is not None or not create:
            return job
        observation = self.observe_ingest(
            canonical,
            size=resolved_size,
            mtime_ns=resolved_mtime,
            state=initial_state,
            evidence=dict(evidence or {}),
            confidence=confidence,
            reason_code=reason_code,
        )
        return self.get_job(str(observation["job_id"]))

    @staticmethod
    def _shortest_path(start: str, target: str) -> list[str]:
        source = str(start).upper()
        destination = str(target).upper()
        if source == destination:
            return []
        queue: deque[tuple[str, list[str]]] = deque([(source, [])])
        visited = {source}
        while queue:
            current, path = queue.popleft()
            for next_state in ALLOWED_PIPELINE_TRANSITIONS.get(current, ()):
                if next_state in TERMINAL_PIPELINE_STATES and next_state != destination:
                    continue
                if next_state in {"RETRYING", "NEEDS_REVIEW"} and next_state != destination:
                    continue
                if next_state == destination:
                    return [*path, next_state]
                if next_state not in visited:
                    visited.add(next_state)
                    queue.append((next_state, [*path, next_state]))
        return []

    def _advance_job(
        self,
        job: dict[str, Any],
        target: str,
        *,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
        actor: str,
        stage_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        target = str(target).upper()
        if job["state"] == target:
            return job
        path = self._shortest_path(str(job["state"]), target)
        if not path and "RETRYING" in ALLOWED_PIPELINE_TRANSITIONS.get(str(job["state"]), ()):
            path = ["RETRYING", target]
        if not path:
            raise InvalidPipelineTransition(f"cannot advance {job['state']} to {target}")
        for index, next_state in enumerate(path):
            job = self._record_transition(
                job,
                next_state,
                reason_code=reason_code if index == len(path) - 1 else "legacy_forward_skip",
                evidence={**dict(evidence), "requested_state": target},
                confidence=confidence,
                actor=actor,
                stage_attempt_id=stage_attempt_id,
            )
        return job

    def _stage_event(
        self,
        attempt: Mapping[str, Any],
        event_type: str,
        *,
        status: str,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
        payload: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO pipeline_stage_events(
                job_id, stage_attempt_id, event_type, stage, status, reason_code,
                evidence_json, confidence, payload_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(attempt["job_id"]),
                str(attempt["stage_attempt_id"]),
                str(event_type),
                str(attempt["stage"]),
                str(status),
                self._reason(reason_code),
                self._json(self._structured(evidence, "evidence")),
                self._confidence(confidence),
                self._json(self._structured(payload, "payload")),
                float(time.time() if now is None else now),
            ),
        )

    def _get_attempt(self, stage_attempt_id: str) -> dict[str, Any] | None:
        return self._decode_row(
            self._fetchone(
                "SELECT * FROM pipeline_stage_attempts WHERE stage_attempt_id = ?",
                (str(stage_attempt_id),),
            )
        )

    def start_stage_attempt(
        self,
        job_id: str,
        stage: str,
        *,
        inputs: Mapping[str, Any],
        model: Mapping[str, Any] | str | None = None,
        retry_limit: int = 0,
        timeout_seconds: float = 0,
        checkpoint: Mapping[str, Any] | None = None,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        target = str(stage).upper()
        if target not in EXECUTION_PIPELINE_STATES:
            raise StageAttemptError(f"invalid executable stage: {target}")
        input_payload = self._structured(inputs, "inputs")
        model_payload = {"name": model} if isinstance(model, str) else self._structured(model, "model")
        checkpoint_payload = self._structured(checkpoint, "checkpoint")
        retry_cap = max(0, int(retry_limit))
        timeout = max(0.0, float(timeout_seconds))
        input_json = self._json(input_payload)
        input_sha256 = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
        model_json = self._json(model_payload)
        now = time.time()
        with self._savepoint():
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(f"unknown pipeline job: {job_id}")
            if job["state"] in TERMINAL_PIPELINE_STATES:
                raise TerminalPipelineStateError(
                    f"cannot start {target}; job {job_id} is {job['state']}"
                )
            if idempotency_key:
                existing = self._get_attempt_by_idempotency(job_id, idempotency_key)
                if existing is not None:
                    if (
                        existing["stage"] != target
                        or existing["input_sha256"] != input_sha256
                        or self._json(existing.get("model", {})) != model_json
                        or int(existing["retry_limit"]) != retry_cap
                        or float(existing["timeout_seconds"]) != timeout
                    ):
                        raise PipelineStateConflict(
                            "stage idempotency key was already used with different inputs"
                        )
                    return existing
            active_id = str(job.get("active_stage_attempt_id") or "")
            if active_id:
                active = self._get_attempt(active_id)
                if active is not None and active["status"] == "RUNNING":
                    if active["stage"] == target and active["input_sha256"] == input_sha256:
                        return active
                    raise StageAttemptError(
                        f"job {job_id} already has running attempt {active_id}"
                    )
            job = self._advance_job(
                job,
                target,
                reason_code="stage_started",
                evidence={**dict(evidence), "stage": target},
                confidence=confidence,
                actor="worker",
            )
            attempt_number = int(
                self._conn.execute(
                    """
                    SELECT COALESCE(MAX(attempt_number), 0) + 1
                    FROM pipeline_stage_attempts WHERE job_id = ? AND stage = ?
                    """,
                    (job_id, target),
                ).fetchone()[0]
            )
            retry_count = max(0, attempt_number - 1)
            attempt_id = uuid.uuid4().hex
            checkpoint_json = self._json(checkpoint_payload)
            checkpoint_sha256 = (
                hashlib.sha256(checkpoint_json.encode("utf-8")).hexdigest()
                if checkpoint_payload
                else ""
            )
            self._conn.execute(
                """
                INSERT INTO pipeline_stage_attempts(
                    stage_attempt_id, job_id, stage, attempt_number, status,
                    input_json, input_sha256, model_json, retry_count, retry_limit,
                    timeout_seconds, checkpoint_json, checkpoint_sha256, idempotency_key,
                    started_at, heartbeat_at, updated_at
                ) VALUES(?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    target,
                    attempt_number,
                    input_json,
                    input_sha256,
                    model_json,
                    retry_count,
                    retry_cap,
                    timeout,
                    checkpoint_json,
                    checkpoint_sha256,
                    str(idempotency_key) if idempotency_key else None,
                    now,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                UPDATE pipeline_jobs
                SET active_stage_attempt_id=?, resume_state=?, updated_at=?
                WHERE job_id=?
                """,
                (attempt_id, target, now, job_id),
            )
            attempt = self._get_attempt(attempt_id)
            if attempt is None:
                raise StageAttemptError("stage attempt disappeared after creation")
            self._stage_event(
                attempt,
                "STARTED",
                status="RUNNING",
                reason_code=reason_code,
                evidence=evidence,
                confidence=confidence,
                payload={
                    "retry_count": retry_count,
                    "retry_limit": retry_cap,
                    "timeout_seconds": timeout,
                },
                now=now,
            )
            return attempt

    def _get_attempt_by_idempotency(
        self, job_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        return self._decode_row(
            self._fetchone(
                """
                SELECT * FROM pipeline_stage_attempts
                WHERE job_id=? AND idempotency_key=?
                """,
                (str(job_id), str(idempotency_key)),
            )
        )

    def checkpoint_stage(
        self,
        stage_attempt_id: str,
        checkpoint: Mapping[str, Any],
        *,
        outputs: Mapping[str, Any] | None = None,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
    ) -> dict[str, Any]:
        checkpoint_payload = self._structured(checkpoint, "checkpoint")
        output_payload = self._structured(outputs, "outputs")
        checkpoint_json = self._json(checkpoint_payload)
        now = time.time()
        with self._savepoint():
            attempt = self._get_attempt(stage_attempt_id)
            if attempt is None:
                raise KeyError(f"unknown stage attempt: {stage_attempt_id}")
            if attempt["status"] != "RUNNING":
                raise StageAttemptError(
                    f"cannot checkpoint attempt in {attempt['status']} status"
                )
            self._conn.execute(
                """
                UPDATE pipeline_stage_attempts
                SET checkpoint_json=?, checkpoint_sha256=?,
                    output_json=CASE WHEN ?='{}' THEN output_json ELSE ? END,
                    heartbeat_at=?, updated_at=?
                WHERE stage_attempt_id=? AND status='RUNNING'
                """,
                (
                    checkpoint_json,
                    hashlib.sha256(checkpoint_json.encode("utf-8")).hexdigest(),
                    self._json(output_payload),
                    self._json(output_payload),
                    now,
                    now,
                    stage_attempt_id,
                ),
            )
            updated = self._get_attempt(stage_attempt_id)
            if updated is None:
                raise StageAttemptError("stage attempt disappeared during checkpoint")
            self._stage_event(
                updated,
                "CHECKPOINTED",
                status="RUNNING",
                reason_code=reason_code,
                evidence=evidence,
                confidence=confidence,
                payload={"checkpoint_sha256": updated["checkpoint_sha256"]},
                now=now,
            )
            return updated

    @staticmethod
    def _failure_defaults(status: str) -> tuple[str, str]:
        return {
            "RETRYABLE_FAILURE": ("transient", "stage_retryable_failure"),
            "PERMANENT_FAILURE": ("permanent", "stage_permanent_failure"),
            "INTERRUPTED": ("interrupted", "stage_interrupted"),
            "NEEDS_REVIEW": ("quality", "stage_needs_review"),
        }.get(status, ("", ""))

    @staticmethod
    def _is_final_artifact_path(path: Path) -> bool:
        forbidden_markers = {"tmp", "part", "partial", "publishing", "staging"}
        filename_markers = set(path.name.casefold().split(".")[1:])
        directory_markers = {
            part.casefold().strip(".") for part in path.parts[:-1] if part
        }
        return (
            path.is_file()
            and not bool(forbidden_markers.intersection(filename_markers))
            and not bool(forbidden_markers.intersection(directory_markers))
        )

    @staticmethod
    def _valid_sha256(value: object) -> bool:
        candidate = str(value or "")
        return len(candidate) == 64 and all(
            character in "0123456789abcdefABCDEF" for character in candidate
        )

    @staticmethod
    def _stat_signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
        return (
            int(getattr(stat_result, "st_dev", 0) or 0),
            int(getattr(stat_result, "st_ino", 0) or 0),
            int(stat_result.st_size),
            int(stat_result.st_mtime_ns),
        )

    @classmethod
    def _hash_stable_final_file(
        cls,
        path: Path,
    ) -> tuple[str, tuple[int, int, int, int]]:
        """Hash one published file while rejecting replacement during the read."""

        if not cls._is_final_artifact_path(path):
            raise PipelineStateError(f"delivery artifact is missing or temporary: {path}")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                before = cls._stat_signature(os.fstat(handle.fileno()))
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = cls._stat_signature(os.fstat(handle.fileno()))
            current = cls._stat_signature(path.stat())
        except OSError as exc:
            raise PipelineStateError(f"cannot verify delivery artifact: {path}") from exc
        if before != after or after != current:
            raise PipelineStateError(f"delivery artifact changed during verification: {path}")
        return digest.hexdigest(), current

    @classmethod
    def _current_source_identity(
        cls,
        job: Mapping[str, Any],
    ) -> tuple[Path, tuple[int, int, int, int]]:
        source_path = Path(str(job.get("canonical_path") or ""))
        try:
            signature = cls._stat_signature(source_path.stat())
        except OSError as exc:
            raise PipelineStateError("pipeline source media is unavailable at completion") from exc
        try:
            expected_size = int(job.get("media_size"))
            expected_mtime_ns = int(job.get("media_mtime_ns"))
        except (TypeError, ValueError) as exc:
            raise PipelineStateError("pipeline source media identity is malformed") from exc
        if signature[2:] != (expected_size, expected_mtime_ns):
            raise PipelineStateError("pipeline source media changed before completion")
        identity_kind = str(job.get("identity_kind") or "")
        if identity_kind == "filesystem_object":
            identity_payload = {
                "dev": signature[0],
                "ino": signature[1],
                "size": signature[2],
                "mtime_ns": signature[3],
            }
        elif identity_kind == "path_stat_fallback":
            identity_payload = {
                "path": cls._canonical_path(source_path),
                "size": signature[2],
                "mtime_ns": signature[3],
            }
        else:
            raise PipelineStateError("pipeline source media identity kind is invalid")
        fingerprint = hashlib.sha256(
            cls._json(identity_payload).encode("utf-8")
        ).hexdigest()
        revision = hashlib.sha256(
            cls._json(
                {
                    "fingerprint": fingerprint,
                    "size": signature[2],
                    "mtime_ns": signature[3],
                }
            ).encode("utf-8")
        ).hexdigest()
        if (
            fingerprint != str(job.get("media_fingerprint") or "")
            or revision != str(job.get("media_revision") or "")
        ):
            raise PipelineStateError("pipeline source media object identity changed")
        return source_path, signature

    @classmethod
    def _completion_manifest_gate(
        cls,
        job: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> list[tuple[Path, tuple[int, int, int, int], str | None]]:
        """Independently prove manifest-v2, source identity, and every output.

        This intentionally does not import ``output_manifest`` because that module
        reaches ``scan_state`` and would create an import cycle here.  The checks
        below mirror the immutable portion of its v2 delivery contract.
        """

        manifest_value = str(evidence.get("manifest_path") or "").strip()
        expected_manifest_hash = str(evidence.get("manifest_sha256") or "").strip()
        if not manifest_value:
            raise PipelineStateError("formal completion requires manifest_path")
        if not cls._valid_sha256(expected_manifest_hash):
            raise PipelineStateError("formal completion requires a valid manifest_sha256")
        manifest_path = Path(manifest_value)
        manifest_hash, manifest_signature = cls._hash_stable_final_file(manifest_path)
        if manifest_hash.casefold() != expected_manifest_hash.casefold():
            raise PipelineStateError("delivery manifest hash does not match verified evidence")
        if manifest_path.with_suffix(".publishing").exists():
            raise PipelineStateError("delivery publication marker is still present")
        try:
            manifest_bytes = manifest_path.read_bytes()
            if hashlib.sha256(manifest_bytes).hexdigest().casefold() != manifest_hash.casefold():
                raise PipelineStateError("delivery manifest changed after verification")
            payload = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineStateError("delivery manifest is not valid UTF-8 JSON") from exc
        if (
            not isinstance(payload, Mapping)
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 2
        ):
            raise PipelineStateError("formal completion requires manifest schema version 2")

        media = payload.get("media")
        delivery = payload.get("delivery")
        quality_gate = payload.get("quality_gate")
        publication = payload.get("publication")
        outputs = payload.get("outputs")
        if not isinstance(media, Mapping):
            raise PipelineStateError("delivery manifest is missing media identity")
        if not isinstance(delivery, Mapping) or delivery.get("contract") != "ai-delivery-v1":
            raise PipelineStateError("delivery manifest has an invalid delivery contract")
        if not isinstance(quality_gate, Mapping) or quality_gate.get("passed") is not True:
            raise PipelineStateError("delivery manifest quality gate did not pass")
        if not isinstance(outputs, list) or not outputs:
            raise PipelineStateError("delivery manifest has no required outputs")

        job_canonical = cls._canonical_path(str(job.get("canonical_path") or ""))
        manifest_canonical = cls._canonical_path(str(media.get("canonical_path") or ""))
        if (
            type(media.get("media_size")) is not int
            or type(media.get("media_mtime_ns")) is not int
        ):
            raise PipelineStateError("delivery manifest media identity is malformed")
        try:
            manifest_size = int(media.get("media_size"))
            manifest_mtime_ns = int(media.get("media_mtime_ns"))
        except (TypeError, ValueError) as exc:
            raise PipelineStateError("delivery manifest media identity is malformed") from exc
        if (
            not job_canonical
            or manifest_canonical != job_canonical
            or manifest_size != int(job.get("media_size"))
            or manifest_mtime_ns != int(job.get("media_mtime_ns"))
        ):
            raise PipelineStateError("delivery manifest does not match the pipeline media revision")
        if cls._canonical_path(str(payload.get("video") or "")) != job_canonical:
            raise PipelineStateError("delivery manifest video path does not match the pipeline job")

        media_identity_payload = {
            "canonical_path": str(media.get("canonical_path") or ""),
            "media_mtime_ns": manifest_mtime_ns,
            "media_size": manifest_size,
        }
        expected_media_fingerprint = hashlib.sha256(
            cls._json(media_identity_payload).encode("utf-8", errors="replace")
        ).hexdigest()
        if str(media.get("media_fingerprint") or "").casefold() != expected_media_fingerprint:
            raise PipelineStateError("delivery manifest media fingerprint is invalid")
        policy_revision = str(delivery.get("policy_revision") or "").strip()
        obligation_payload = {**media_identity_payload, "policy_revision": policy_revision}
        expected_obligation_id = "aiobl_" + hashlib.sha256(
            cls._json(obligation_payload).encode("utf-8", errors="replace")
        ).hexdigest()
        try:
            verified_at = float(delivery.get("verified_at") or 0)
        except (TypeError, ValueError) as exc:
            raise PipelineStateError("delivery manifest verified_at is invalid") from exc
        if (
            not policy_revision
            or str(delivery.get("obligation_id") or "") != expected_obligation_id
            or verified_at <= 0
        ):
            raise PipelineStateError("delivery manifest obligation identity is invalid")

        if (
            not isinstance(publication, Mapping)
            or publication.get("contract") != "ai-publication-semantics-v2"
            or str(publication.get("kind") or "") != str(payload.get("publication_kind") or "")
        ):
            raise PipelineStateError("delivery manifest publication semantics are invalid")
        output_languages = publication.get("output_languages")
        publication_kind = str(publication.get("kind") or "")
        if (
            not isinstance(output_languages, list)
            or len(output_languages) != len(outputs)
            or any(not isinstance(language, str) for language in output_languages)
            or publication_kind
            not in {"translated_trilingual", "adopted_zh_tw", "converted_zh_cn"}
            or (
                publication_kind == "translated_trilingual"
                and (
                    len(output_languages) != 3
                    or output_languages[1:] != ["zh-CN", "zh-TW"]
                )
            )
            or (
                publication_kind in {"adopted_zh_tw", "converted_zh_cn"}
                and output_languages != ["zh-TW"]
            )
        ):
            raise PipelineStateError("delivery manifest is not a Traditional-Chinese publication")

        source_path, source_signature = cls._current_source_identity(job)

        verified_files: list[
            tuple[Path, tuple[int, int, int, int], str | None]
        ] = [
            (manifest_path, manifest_signature, manifest_hash),
            (source_path, source_signature, None),
        ]
        manifested_artifacts: dict[str, Mapping[str, Any]] = {}
        for index, entry in enumerate(outputs):
            if not isinstance(entry, Mapping):
                raise PipelineStateError("delivery manifest output entry is malformed")
            artifact_value = str(entry.get("path") or "").strip()
            if not artifact_value or not cls._valid_sha256(entry.get("sha256")):
                raise PipelineStateError("delivery manifest output evidence is incomplete")
            artifact_path = Path(artifact_value)
            canonical_artifact = cls._canonical_path(artifact_path)
            if canonical_artifact in manifested_artifacts:
                raise PipelineStateError("delivery manifest contains duplicate output paths")
            if type(entry.get("size")) is not int or type(entry.get("mtime_ns")) is not int:
                raise PipelineStateError("delivery manifest output metadata is malformed")
            try:
                expected_size = int(entry.get("size"))
                expected_mtime_ns = int(entry.get("mtime_ns"))
            except (TypeError, ValueError) as exc:
                raise PipelineStateError("delivery manifest output metadata is malformed") from exc
            actual_hash, artifact_signature = cls._hash_stable_final_file(artifact_path)
            if (
                artifact_signature[2] != expected_size
                or artifact_signature[3] != expected_mtime_ns
                or actual_hash.casefold() != str(entry.get("sha256") or "").casefold()
                or str(entry.get("language") or "") != str(output_languages[index])
            ):
                raise PipelineStateError("delivery output no longer matches its manifest evidence")
            manifested_artifacts[canonical_artifact] = entry
            verified_files.append((artifact_path, artifact_signature, actual_hash))

        supplied_artifacts = evidence.get("artifacts")
        if supplied_artifacts is not None:
            if not isinstance(supplied_artifacts, Sequence) or isinstance(
                supplied_artifacts, (str, bytes)
            ):
                raise PipelineStateError("delivery evidence artifacts must be a list")
            for supplied in supplied_artifacts:
                if not isinstance(supplied, Mapping) or not supplied.get("path"):
                    raise PipelineStateError("delivery evidence artifact is malformed")
                manifested = manifested_artifacts.get(
                    cls._canonical_path(str(supplied.get("path") or ""))
                )
                if manifested is None:
                    raise PipelineStateError("delivery evidence names an unmanifested artifact")
                for field in ("size", "mtime_ns", "sha256"):
                    if field in supplied and str(supplied[field]) != str(manifested.get(field)):
                        raise PipelineStateError(
                            "delivery evidence conflicts with the authoritative manifest"
                        )
        return verified_files

    def finish_stage_attempt(
        self,
        stage_attempt_id: str,
        status: str,
        *,
        outputs: Mapping[str, Any] | None = None,
        outputs_verified: bool = False,
        error_class: str = "",
        error_code: str = "",
        error: Mapping[str, Any] | None = None,
        next_state: str | None = None,
        retry_after_seconds: float = 0,
        reason_code: str,
        evidence: Mapping[str, Any],
        confidence: float,
    ) -> dict[str, Any]:
        final_status = str(status).upper()
        if final_status not in STAGE_ATTEMPT_STATUSES - {"RUNNING"}:
            raise StageAttemptError(f"invalid final stage attempt status: {final_status}")
        output_payload = self._structured(outputs, "outputs")
        error_payload = self._structured(error, "error")
        default_class, default_code = self._failure_defaults(final_status)
        resolved_error_class = str(error_class or default_class).casefold()
        resolved_error_code = str(error_code or default_code).strip()
        if resolved_error_class not in STAGE_ERROR_CLASSES:
            raise StageAttemptError(f"invalid stage error class: {resolved_error_class}")
        if final_status != "SUCCEEDED" and (not resolved_error_class or not resolved_error_code):
            raise StageAttemptError("failed stage attempts require error_class and error_code")
        now = time.time()
        with self._savepoint():
            attempt = self._get_attempt(stage_attempt_id)
            if attempt is None:
                raise KeyError(f"unknown stage attempt: {stage_attempt_id}")
            if attempt["status"] != "RUNNING":
                if attempt["status"] == final_status:
                    return attempt
                raise StageAttemptError(
                    f"attempt already finished as {attempt['status']}, not {final_status}"
                )
            self._conn.execute(
                """
                UPDATE pipeline_stage_attempts
                SET status=?, output_json=?, outputs_verified=?, error_class=?, error_code=?,
                    error_json=?, heartbeat_at=?, finished_at=?, updated_at=?
                WHERE stage_attempt_id=? AND status='RUNNING'
                """,
                (
                    final_status,
                    self._json(output_payload),
                    int(bool(outputs_verified)),
                    resolved_error_class,
                    resolved_error_code,
                    self._json(error_payload),
                    now,
                    now,
                    now,
                    stage_attempt_id,
                ),
            )
            job = self.get_job(str(attempt["job_id"]))
            if job is None:
                raise StageAttemptError("stage attempt has no job")
            self._conn.execute(
                "UPDATE pipeline_jobs SET active_stage_attempt_id=NULL, updated_at=? WHERE job_id=?",
                (now, job["job_id"]),
            )
            if final_status == "SUCCEEDED":
                if next_state is not None:
                    job = self._advance_job(
                        job,
                        str(next_state).upper(),
                        reason_code=reason_code,
                        evidence=evidence,
                        confidence=confidence,
                        actor="worker",
                        stage_attempt_id=stage_attempt_id,
                    )
            elif final_status in {"RETRYABLE_FAILURE", "INTERRUPTED"}:
                # A process/container restart consumes the same bounded retry
                # budget as every other recoverable failure.  Otherwise a crash
                # loop could remain RETRYING forever without durable progress.
                retry_count = int(attempt["retry_count"]) + 1
                can_retry = retry_count <= int(attempt["retry_limit"])
                target = "RETRYING" if can_retry else "NEEDS_REVIEW"
                job = self._advance_job(
                    job,
                    target,
                    reason_code=reason_code,
                    evidence={**dict(evidence), "error_class": resolved_error_class, "error_code": resolved_error_code},
                    confidence=confidence,
                    actor="recovery" if final_status == "INTERRUPTED" else "worker",
                    stage_attempt_id=stage_attempt_id,
                )
                self._conn.execute(
                    """
                    UPDATE pipeline_jobs
                    SET retry_count=retry_count+?, resume_state=?, next_retry_at=?, updated_at=?
                    WHERE job_id=?
                    """,
                    (
                        1,
                        attempt["stage"],
                        now + max(0.0, float(retry_after_seconds)) if can_retry else 0,
                        now,
                        job["job_id"],
                    ),
                )
            elif final_status == "NEEDS_REVIEW":
                job = self._advance_job(
                    job,
                    "NEEDS_REVIEW",
                    reason_code=reason_code,
                    evidence=evidence,
                    confidence=confidence,
                    actor="worker",
                    stage_attempt_id=stage_attempt_id,
                )
            else:
                job = self._advance_job(
                    job,
                    "FAILED",
                    reason_code=reason_code,
                    evidence=evidence,
                    confidence=confidence,
                    actor="worker",
                    stage_attempt_id=stage_attempt_id,
                )
                self._conn.execute(
                    "UPDATE pipeline_jobs SET terminal_error_json=? WHERE job_id=?",
                    (
                        self._json(
                            {
                                "error_class": resolved_error_class,
                                "error_code": resolved_error_code,
                                **error_payload,
                            }
                        ),
                        job["job_id"],
                    ),
                )
            updated = self._get_attempt(stage_attempt_id)
            if updated is None:
                raise StageAttemptError("stage attempt disappeared during finish")
            self._stage_event(
                updated,
                "FINISHED",
                status=final_status,
                reason_code=reason_code,
                evidence=evidence,
                confidence=confidence,
                payload={
                    "outputs_verified": bool(outputs_verified),
                    "error_class": resolved_error_class,
                    "error_code": resolved_error_code,
                },
                now=now,
            )
            return updated

    @classmethod
    def _outputs_are_valid(
        cls,
        outputs: Mapping[str, Any],
        checkpoint: Mapping[str, Any] | None = None,
    ) -> bool:
        artifacts: list[Mapping[str, Any]] = []
        explicit = outputs.get("artifacts")
        if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
            if any(not isinstance(item, Mapping) for item in explicit):
                return False
            artifacts.extend(item for item in explicit if isinstance(item, Mapping))
        if isinstance(outputs.get("artifact"), Mapping):
            artifacts.append(outputs["artifact"])
        if not artifacts:
            checkpoint_evidence = outputs.get("checkpoint_evidence")
            return bool(
                outputs.get("no_artifact_required") is True
                and isinstance(checkpoint_evidence, Mapping)
                and checkpoint_evidence
                and isinstance(checkpoint, Mapping)
                and checkpoint
            )
        for artifact in artifacts:
            artifact_path = artifact.get("path")
            if not artifact_path:
                return False
            path = Path(str(artifact_path))
            if not cls._is_final_artifact_path(path):
                return False
            expected_size = artifact.get("size")
            if expected_size is not None and path.stat().st_size != int(expected_size):
                return False
            expected_hash = str(artifact.get("sha256") or "")
            if expected_hash:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest().casefold() != expected_hash.casefold():
                    return False
        return True

    def reusable_stage_attempt(
        self,
        job_id: str,
        stage: str,
        *,
        inputs: Mapping[str, Any],
        verify_outputs: bool = True,
    ) -> dict[str, Any] | None:
        input_json = self._json(self._structured(inputs, "inputs"))
        input_hash = hashlib.sha256(input_json.encode("utf-8")).hexdigest()
        attempt = self._decode_row(
            self._fetchone(
                """
                SELECT * FROM pipeline_stage_attempts
                WHERE job_id=? AND stage=? AND status='SUCCEEDED'
                  AND outputs_verified=1 AND input_sha256=?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (str(job_id), str(stage).upper(), input_hash),
            )
        )
        if attempt is None:
            return None
        if verify_outputs and not self._outputs_are_valid(
            attempt.get("output", {}), attempt.get("checkpoint", {})
        ):
            return None
        return attempt

    def recover_interrupted_stages(
        self,
        *,
        now: float | None = None,
        stale_before: float | None = None,
        recover_all_running: bool = True,
    ) -> list[dict[str, Any]]:
        timestamp = float(time.time() if now is None else now)
        recovered: list[dict[str, Any]] = []
        attempts = [
            self._decode_row(row) or {}
            for row in self._fetchall(
                "SELECT * FROM pipeline_stage_attempts WHERE status='RUNNING' ORDER BY started_at"
            )
        ]
        for attempt in attempts:
            timed_out = bool(
                float(attempt["timeout_seconds"]) > 0
                and float(attempt["heartbeat_at"]) + float(attempt["timeout_seconds"]) <= timestamp
            )
            stale = stale_before is not None and float(attempt["heartbeat_at"]) <= float(stale_before)
            if not (recover_all_running or timed_out or stale):
                continue
            finished = self.finish_stage_attempt(
                str(attempt["stage_attempt_id"]),
                "INTERRUPTED",
                outputs=attempt.get("output", {}),
                outputs_verified=False,
                error_class="interrupted",
                error_code="worker_restart_interrupted_stage",
                error={
                    "message": "running stage was recovered after process interruption",
                    "timed_out": timed_out,
                    "stale": stale,
                },
                retry_after_seconds=0,
                reason_code="interrupted_stage_recovered",
                evidence={
                    "last_heartbeat_at": attempt["heartbeat_at"],
                    "checkpoint_sha256": attempt["checkpoint_sha256"],
                },
                confidence=1.0,
            )
            recovered.append(
                {
                    "job_id": attempt["job_id"],
                    "stage_attempt_id": attempt["stage_attempt_id"],
                    "stage": attempt["stage"],
                    "input": attempt.get("input", {}),
                    "output": attempt.get("output", {}),
                    "model": attempt.get("model", {}),
                    "checkpoint": attempt.get("checkpoint", {}),
                    "recovered_status": finished["status"],
                }
            )
        return recovered

    def complete_job(
        self,
        job_id: str,
        *,
        delivery_evidence: Mapping[str, Any],
        reason_code: str = "verified_delivery_completed",
        confidence: float = 1.0,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        evidence = self._structured(delivery_evidence, "delivery_evidence")
        verification = evidence.get("verification")
        required_verification = (
            "required_outputs_complete",
            "hashes_verified",
            "publication_marker_absent",
            "media_identity_matched",
        )
        if not isinstance(verification, Mapping) or not all(
            verification.get(field) is True for field in required_verification
        ):
            raise PipelineStateError(
                "formal completion requires strict delivery verification evidence"
            )
        snapshot = self.get_job(job_id)
        if snapshot is None:
            raise KeyError(f"unknown pipeline job: {job_id}")
        if snapshot["state"] == "COMPLETED":
            return snapshot
        if snapshot["state"] not in {"QC", "MUXING"}:
            raise InvalidPipelineTransition(
                f"formal completion requires QC or MUXING, found {snapshot['state']}"
            )
        verified_files = self._completion_manifest_gate(snapshot, evidence)
        independently_verified = {
            **evidence,
            "pipeline_completion_gate": {
                "manifest_schema_version": 2,
                "source_identity_verified": True,
                "required_artifact_count": max(0, len(verified_files) - 2),
                "required_artifacts_rehashed": True,
            },
        }
        with self._savepoint():
            job = self.get_job(job_id)
            if job is None:
                raise KeyError(f"unknown pipeline job: {job_id}")
            if job["state"] == "COMPLETED":
                return job
            if str(job["media_revision"]) != str(snapshot["media_revision"]):
                raise PipelineStateConflict("pipeline media revision changed during completion")
            if job["state"] not in {"QC", "MUXING"}:
                raise InvalidPipelineTransition(
                    f"formal completion requires QC or MUXING, found {job['state']}"
                )
            for verified_path, expected_signature, expected_hash in verified_files:
                if expected_hash is None:
                    current_source_path, current_signature = self._current_source_identity(job)
                    if self._canonical_path(current_source_path) != self._canonical_path(
                        verified_path
                    ):
                        raise PipelineStateError(
                            "pipeline source path changed before completion commit"
                        )
                else:
                    current_hash, current_signature = self._hash_stable_final_file(
                        verified_path
                    )
                    if current_hash.casefold() != expected_hash.casefold():
                        raise PipelineStateError(
                            f"verified delivery file content changed before commit: {verified_path}"
                        )
                if current_signature != expected_signature:
                    raise PipelineStateError(
                        f"verified delivery file changed before commit: {verified_path}"
                    )
            return self._record_transition(
                job,
                "COMPLETED",
                reason_code=reason_code,
                evidence=independently_verified,
                confidence=confidence,
                actor="publisher",
                idempotency_key=idempotency_key,
                allow_completed=True,
            )

    def transition_legacy_stage(
        self,
        job_id: str,
        stage: str,
        status: str = "running",
        *,
        inputs: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        model: Mapping[str, Any] | str | None = None,
        retry_limit: int = 1,
        timeout_seconds: float = 0,
        checkpoint: Mapping[str, Any] | None = None,
        error_class: str = "",
        error_code: str = "",
        error: Mapping[str, Any] | None = None,
        reason_code: str = "legacy_stage_event",
        evidence: Mapping[str, Any] | None = None,
        confidence: float = 1.0,
        idempotency_key: str | None = None,
        outputs_verified: bool = False,
    ) -> dict[str, Any]:
        request_payload = {
            "stage": str(stage),
            "status": str(status),
            "inputs": dict(inputs or {}),
            "outputs": dict(outputs or {}),
            "model": dict(model) if isinstance(model, Mapping) else model,
            "retry_limit": int(retry_limit),
            "timeout_seconds": float(timeout_seconds),
            "checkpoint": dict(checkpoint or {}),
            "error_class": str(error_class),
            "error_code": str(error_code),
            "error": dict(error or {}),
            "reason_code": str(reason_code),
            "evidence": dict(evidence or {}),
            "confidence": float(confidence),
            "outputs_verified": bool(outputs_verified),
        }
        request_sha256 = hashlib.sha256(
            self._json(request_payload).encode("utf-8")
        ).hexdigest()
        with self._savepoint():
            if idempotency_key:
                existing = self._fetchone(
                    """
                    SELECT * FROM pipeline_operation_idempotency
                    WHERE job_id=? AND idempotency_key=?
                    """,
                    (str(job_id), str(idempotency_key)),
                )
                if existing is not None:
                    if (
                        existing["operation_kind"] != "legacy_stage"
                        or existing["request_sha256"] != request_sha256
                    ):
                        raise PipelineStateConflict(
                            "legacy idempotency key was reused with a different request"
                        )
                    attempt = self._get_attempt(str(existing["stage_attempt_id"]))
                    if attempt is None:
                        raise PipelineStateConflict(
                            "legacy idempotency record references a missing attempt"
                        )
                    return attempt
            result = self._transition_legacy_stage_once(
                job_id,
                stage,
                status,
                inputs=inputs,
                outputs=outputs,
                model=model,
                retry_limit=retry_limit,
                timeout_seconds=timeout_seconds,
                checkpoint=checkpoint,
                error_class=error_class,
                error_code=error_code,
                error=error,
                reason_code=reason_code,
                evidence=evidence,
                confidence=confidence,
                idempotency_key=idempotency_key,
                outputs_verified=outputs_verified,
            )
            if idempotency_key:
                self._conn.execute(
                    """
                    INSERT INTO pipeline_operation_idempotency(
                        job_id, idempotency_key, operation_kind, request_sha256,
                        stage_attempt_id, created_at
                    ) VALUES(?, ?, 'legacy_stage', ?, ?, ?)
                    """,
                    (
                        str(job_id),
                        str(idempotency_key),
                        request_sha256,
                        str(result["stage_attempt_id"]),
                        time.time(),
                    ),
                )
            return result

    def _transition_legacy_stage_once(
        self,
        job_id: str,
        stage: str,
        status: str = "running",
        *,
        inputs: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        model: Mapping[str, Any] | str | None = None,
        retry_limit: int = 1,
        timeout_seconds: float = 0,
        checkpoint: Mapping[str, Any] | None = None,
        error_class: str = "",
        error_code: str = "",
        error: Mapping[str, Any] | None = None,
        reason_code: str = "legacy_stage_event",
        evidence: Mapping[str, Any] | None = None,
        confidence: float = 1.0,
        idempotency_key: str | None = None,
        outputs_verified: bool = False,
    ) -> dict[str, Any]:
        pipeline_stage = legacy_stage_to_pipeline_state(stage, status)
        normalized_status = str(status).casefold()
        normalized_stage_name = str(stage or "").strip().casefold().replace("-", "_").replace(" ", "_")
        evidence_payload = {"legacy_stage": str(stage), "legacy_status": str(status), **dict(evidence or {})}
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown pipeline job: {job_id}")
        if normalized_stage_name == "delivery_verification" and job["state"] == "MUXING":
            pipeline_stage = "MUXING"
        running_statuses = {"running", "started", "start"}
        success_statuses = {"ok", "succeeded", "success", "complete", "completed", "skipped"}
        stage_order = {
            state: index
            for index, state in enumerate(
                (
                    "ANALYZING",
                    "SUBTITLE_DETECTION",
                    "ASR",
                    "TRANSLATING",
                    "POST_PROCESSING",
                    "QC",
                    "MUXING",
                )
            )
        }
        effective_state = str(job["state"])
        if effective_state in {"RETRYING", "NEEDS_REVIEW"}:
            resume_state = str(job.get("resume_state") or "")
            if resume_state in stage_order:
                effective_state = resume_state
        active_id = str(job.get("active_stage_attempt_id") or "")
        active = self._get_attempt(active_id) if active_id else None
        recent = self._decode_row(
            self._fetchone(
                """
                SELECT * FROM pipeline_stage_attempts
                WHERE job_id=? AND stage=?
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (str(job_id), pipeline_stage),
            )
        )
        is_regression = (
            effective_state in stage_order
            and pipeline_stage in stage_order
            and stage_order[pipeline_stage] < stage_order[effective_state]
        )
        if is_regression:
            anchor = recent or active or self._decode_row(
                self._fetchone(
                    """
                    SELECT * FROM pipeline_stage_attempts
                    WHERE job_id=? ORDER BY started_at DESC, attempt_number DESC LIMIT 1
                    """,
                    (str(job_id),),
                )
            )
            if anchor is None:
                raise StageAttemptError("late legacy telemetry has no durable attempt anchor")
            self._stage_event(
                anchor,
                "LATE_LEGACY_TELEMETRY",
                status=str(anchor["status"]),
                reason_code="legacy_stage_late_no_regression",
                evidence={
                    **evidence_payload,
                    "incoming_pipeline_stage": pipeline_stage,
                    "formal_state_preserved": job["state"],
                    "effective_formal_stage": effective_state,
                },
                confidence=confidence,
            )
            return anchor
        if active is not None and active["status"] == "RUNNING":
            if active["stage"] == pipeline_stage:
                # Several legacy labels describe one formal stage (audio/transcription,
                # worker/preflight). One durable attempt owns that formal stage.
                if normalized_status in running_statuses:
                    return active
                running = active
            elif normalized_status not in running_statuses | success_statuses:
                # A failure can only finish the attempt that actually owns this
                # formal stage.  Delayed labels must never consume another stage's
                # retry budget or move RETRYING back to an earlier stage.
                self._stage_event(
                    active,
                    "LATE_LEGACY_TELEMETRY",
                    status=str(active["status"]),
                    reason_code="legacy_failure_non_active_stage_ignored",
                    evidence={
                        **evidence_payload,
                        "incoming_pipeline_stage": pipeline_stage,
                        "active_pipeline_stage": active["stage"],
                        "formal_state_preserved": job["state"],
                    },
                    confidence=confidence,
                )
                return active
            elif self._shortest_path(str(job["state"]), pipeline_stage):
                self.finish_stage_attempt(
                    str(active["stage_attempt_id"]),
                    "SUCCEEDED",
                    outputs={},
                    outputs_verified=False,
                    reason_code="legacy_stage_superseded",
                    evidence={
                        **evidence_payload,
                        "superseded_stage": active["stage"],
                        "next_stage": pipeline_stage,
                    },
                    confidence=confidence,
                )
                active = None
            elif normalized_status in running_statuses:
                # Ignore a late legacy start rather than moving a formal job backward.
                self._stage_event(
                    active,
                    "LATE_LEGACY_TELEMETRY",
                    status=str(active["status"]),
                    reason_code="legacy_stage_late_no_regression",
                    evidence={**evidence_payload, "formal_state_preserved": job["state"]},
                    confidence=confidence,
                )
                return active
        if normalized_status in running_statuses:
            return self.start_stage_attempt(
                job_id,
                pipeline_stage,
                inputs=dict(inputs or {"legacy_stage": str(stage)}),
                model=model,
                retry_limit=retry_limit,
                timeout_seconds=timeout_seconds,
                checkpoint=checkpoint,
                reason_code=reason_code,
                evidence=evidence_payload,
                confidence=confidence,
                idempotency_key=idempotency_key,
            )
        running = active if active is not None and active["stage"] == pipeline_stage else self._decode_row(
            self._fetchone(
                """
                SELECT * FROM pipeline_stage_attempts
                WHERE job_id=? AND stage=? AND status='RUNNING'
                ORDER BY attempt_number DESC LIMIT 1
                """,
                (str(job_id), pipeline_stage),
            )
        )
        if running is None and normalized_status in success_statuses:
            if recent is not None and recent["status"] == "SUCCEEDED":
                self._stage_event(
                    recent,
                    "LATE_LEGACY_TELEMETRY",
                    status=str(recent["status"]),
                    reason_code="legacy_stage_late_no_regression",
                    evidence={**evidence_payload, "formal_state_preserved": job["state"]},
                    confidence=confidence,
                )
                return recent
        current_job = self.get_job(job_id)
        if (
            running is None
            and current_job is not None
            and current_job["state"] != pipeline_stage
            and not self._shortest_path(str(current_job["state"]), pipeline_stage)
        ):
            if recent is not None:
                # Late legacy events are common; preserve the formal forward state.
                self._stage_event(
                    recent,
                    "LATE_LEGACY_TELEMETRY",
                    status=str(recent["status"]),
                    reason_code="legacy_stage_late_no_regression",
                    evidence={**evidence_payload, "formal_state_preserved": current_job["state"]},
                    confidence=confidence,
                )
                return recent
        if running is None and normalized_status not in success_statuses:
            anchor = recent or active or self._decode_row(
                self._fetchone(
                    """
                    SELECT * FROM pipeline_stage_attempts
                    WHERE job_id=? ORDER BY started_at DESC, attempt_number DESC LIMIT 1
                    """,
                    (str(job_id),),
                )
            )
            if anchor is None:
                raise StageAttemptError(
                    "legacy failure cannot create an implicit durable stage attempt"
                )
            self._stage_event(
                anchor,
                "LATE_LEGACY_TELEMETRY",
                status=str(anchor["status"]),
                reason_code="legacy_failure_without_active_stage_ignored",
                evidence={
                    **evidence_payload,
                    "incoming_pipeline_stage": pipeline_stage,
                    "formal_state_preserved": (
                        current_job["state"] if current_job is not None else job["state"]
                    ),
                },
                confidence=confidence,
            )
            return anchor
        if running is None:
            running = self.start_stage_attempt(
                job_id,
                pipeline_stage,
                inputs=dict(inputs or {"legacy_stage": str(stage)}),
                model=model,
                retry_limit=retry_limit,
                timeout_seconds=timeout_seconds,
                checkpoint=checkpoint,
                reason_code="legacy_stage_implicit_start",
                evidence=evidence_payload,
                confidence=confidence,
                idempotency_key=idempotency_key,
            )
        if normalized_status in success_statuses:
            # Legacy completion is telemetry only. Formal COMPLETED remains gated by complete_job().
            return self.finish_stage_attempt(
                str(running["stage_attempt_id"]),
                "SUCCEEDED",
                outputs=outputs,
                outputs_verified=outputs_verified,
                reason_code=reason_code,
                evidence=evidence_payload,
                confidence=confidence,
            )
        classified_error = str(error_class or "").casefold()
        if normalized_status in {"retry", "retryable_failure", "deferred"}:
            final_status = "RETRYABLE_FAILURE"
        elif normalized_status in {"review", "review_required", "needs_review"}:
            final_status = "NEEDS_REVIEW"
        elif classified_error == "quality":
            final_status = "NEEDS_REVIEW"
        elif classified_error == "permanent":
            final_status = "PERMANENT_FAILURE"
        else:
            # A legacy 'failed' label is not authoritative enough to terminalize
            # a durable job. Unknown/transient/resource/interrupted failures retry.
            final_status = "RETRYABLE_FAILURE"
        normalized_error_class = (
            classified_error if classified_error in STAGE_ERROR_CLASSES else "transient"
        )
        return self.finish_stage_attempt(
            str(running["stage_attempt_id"]),
            final_status,
            outputs=outputs,
            outputs_verified=False,
            error_class=normalized_error_class,
            error_code=error_code,
            error=error,
            reason_code=reason_code,
            evidence=evidence_payload,
            confidence=confidence,
        )


LEGACY_STAGE_STATE_MAP: dict[str, str] = {
    "worker": "SUBTITLE_DETECTION",
    "preflight": "SUBTITLE_DETECTION",
    "metadata_context": "SUBTITLE_DETECTION",
    "language_detect": "SUBTITLE_DETECTION",
    "audio_selection": "ASR",
    "subtitle_source": "SUBTITLE_DETECTION",
    "subtitle_detection": "SUBTITLE_DETECTION",
    "audio": "ASR",
    "source_transcription": "ASR",
    "resource_runtime": "ASR",
    "vocal_separation": "ASR",
    "transcription": "ASR",
    "transcription_review": "ASR",
    "translation": "TRANSLATING",
    "source_translation": "TRANSLATING",
    "line_retranslation": "TRANSLATING",
    "opencc": "POST_PROCESSING",
    "postprocess": "ASR",
    "post_processing": "POST_PROCESSING",
    "ass_export": "POST_PROCESSING",
    "source_ass_export": "POST_PROCESSING",
    "cleanup": "POST_PROCESSING",
    "quality_check": "QC",
    "quality_review": "QC",
    "delivery_verification": "QC",
    "qc": "QC",
    "mux": "MUXING",
    "move_completed": "MUXING",
    "completed_delivery": "MUXING",
    "complete": "QC",
    "completed": "QC",
    "skipped": "QC",
}


def legacy_stage_to_pipeline_state(stage: str, status: str = "running") -> str:
    normalized = str(stage or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in LEGACY_STAGE_STATE_MAP:
        return LEGACY_STAGE_STATE_MAP[normalized]
    candidate = normalized.upper()
    if candidate in EXECUTION_PIPELINE_STATES:
        return candidate
    if str(status).casefold() in {"complete", "completed", "skipped"}:
        return "QC"
    return "ANALYZING"
