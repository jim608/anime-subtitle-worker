from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterator

from file_times import file_time_metadata
from sqlite_safety import online_backup_before_migration, quick_check_connection


SCHEMA_VERSION = 6
BUSY_TIMEOUT_SECONDS = 60
CONTROL_HISTORY_MAX_ROWS = 25_000
CONTROL_HISTORY_RETENTION_DAYS = 30
_INITIALIZE_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[Path] = set()


@dataclass(frozen=True)
class ControlCommand:
    command_id: str
    action: str
    target: str
    parameters: dict[str, Any]
    requested_at: float


def control_state_path(config: Any) -> Path:
    configured = Path(str(getattr(config, "control_state_path", "control_state.sqlite3") or "control_state.sqlite3"))
    if configured.is_absolute():
        return configured
    return Path(config.work_path) / configured


def control_inbox_path(config: Any) -> Path:
    configured = Path(str(getattr(config, "control_inbox_path", "control_inbox") or "control_inbox"))
    if configured.is_absolute():
        return configured
    return Path(config.work_path) / configured


def ingest_command_inbox(config: Any, *, limit: int = 100) -> int:
    inbox = control_inbox_path(config)
    inbox.mkdir(parents=True, exist_ok=True)
    ingested = 0
    rejected = inbox.parent / f"{inbox.name}_rejected"
    for path in sorted(inbox.glob("*.json"), key=lambda item: item.name)[: max(1, int(limit))]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("command payload must be an object")
            action = str(payload.get("action") or "").strip()
            idempotency_key = str(payload.get("idempotency_key") or path.stem).strip()
            if not action or not idempotency_key:
                raise ValueError("command action and idempotency_key are required")
            parameters = payload.get("parameters")
            enqueue_command(
                config,
                action=action,
                target=str(payload.get("target") or ""),
                parameters=parameters if isinstance(parameters, dict) else {},
                idempotency_key=idempotency_key,
            )
            path.unlink()
            ingested += 1
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
            rejected.mkdir(parents=True, exist_ok=True)
            destination = rejected / path.name
            try:
                path.replace(destination)
            except OSError:
                pass
    return ingested


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS)
    else:
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_SECONDS * 1000}")
    if not readonly:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA wal_autocheckpoint=500")
    return connection


@contextmanager
def control_connection(config: Any, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    path = control_state_path(config)
    if readonly and not path.exists():
        initialize_control_state(config)
    connection = _connect(path, readonly=readonly)
    try:
        yield connection
        if not readonly and connection.in_transaction:
            connection.commit()
    except Exception:
        if not readonly and connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_control_state(config: Any) -> Path:
    path = control_state_path(config)
    with _INITIALIZE_LOCK:
        if path in _INITIALIZED_PATHS and path.is_file():
            return path
        existing_version = _existing_schema_version(path)
        if path.is_file() and path.stat().st_size > 0 and existing_version < SCHEMA_VERSION:
            online_backup_before_migration(
                path,
                backup_dir=path.parent / "sqlite_migration_backups",
                reason=f"control-v{existing_version}-to-v{SCHEMA_VERSION}",
            )
        # sqlite3.Connection.__exit__ commits or rolls back but deliberately
        # does not close the handle.  A leaked initializer connection keeps
        # temporary databases and migration backups locked on Windows and
        # unnecessarily retains WAL readers in production.
        with closing(_connect(path)) as connection:
            connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS control_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS control_commands (
                command_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                review_id TEXT NOT NULL DEFAULT '',
                parameters_json TEXT NOT NULL DEFAULT '{}',
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'queued',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                requested_at REAL NOT NULL,
                started_at REAL NOT NULL DEFAULT 0,
                finished_at REAL NOT NULL DEFAULT 0,
                worker_id TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_control_commands_status_requested
                ON control_commands(status, requested_at);
            CREATE TABLE IF NOT EXISTS review_items (
                review_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                target_key TEXT NOT NULL,
                canonical_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                severity TEXT NOT NULL DEFAULT 'warning',
                summary TEXT NOT NULL,
                diagnosis_json TEXT NOT NULL DEFAULT '{}',
                candidates_json TEXT NOT NULL DEFAULT '[]',
                resolution_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                resolved_at REAL NOT NULL DEFAULT 0,
                UNIQUE(kind, target_key)
            );
            CREATE INDEX IF NOT EXISTS idx_review_items_status_updated
                ON review_items(status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS operation_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_operation_audit_created
                ON operation_audit(created_at DESC);
            CREATE TABLE IF NOT EXISTS series_source_mappings (
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                series_path TEXT NOT NULL,
                series_id TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 1,
                locked INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(source, source_id, season)
            );
            CREATE INDEX IF NOT EXISTS idx_series_source_mappings_path
                ON series_source_mappings(series_path);
            CREATE TABLE IF NOT EXISTS daily_metrics (
                day TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY(day, metric)
            );
            CREATE TABLE IF NOT EXISTS auto_remediation_campaigns (
                campaign_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                parameters_json TEXT NOT NULL DEFAULT '{}',
                counters_json TEXT NOT NULL DEFAULT '{}',
                current_item_id TEXT NOT NULL DEFAULT '',
                next_run_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_auto_remediation_campaigns_state_next
                ON auto_remediation_campaigns(state, next_run_at, created_at);
            CREATE TABLE IF NOT EXISTS auto_remediation_items (
                item_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                path TEXT NOT NULL,
                failure_revision TEXT NOT NULL,
                strategy TEXT NOT NULL,
                status TEXT NOT NULL,
                before_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                claimed_at REAL NOT NULL DEFAULT 0,
                finished_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(path, failure_revision, strategy)
            );
            CREATE INDEX IF NOT EXISTS idx_auto_remediation_items_campaign_status
                ON auto_remediation_items(campaign_id, status, updated_at);
            CREATE TABLE IF NOT EXISTS control_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            """
            )
            now = time.time()
            _migrate_review_identity(connection, now=now)
            _migrate_command_review_identity(connection)
            if existing_version < 5:
                _migrate_open_review_file_metadata(connection)
            connection.execute(
            """
            INSERT INTO control_meta(key, value, updated_at) VALUES('schema_version', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (str(SCHEMA_VERSION), now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO control_schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, now),
            )
            _prune_control_history(connection, now=now)
            connection.commit()
            quick_check_connection(connection)
        _INITIALIZED_PATHS.add(path)
    return path


def _migrate_command_review_identity(connection: sqlite3.Connection) -> None:
    """Add a durable review-to-command link without changing command semantics."""

    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(control_commands)").fetchall()
    }
    if "review_id" not in columns:
        connection.execute(
            "ALTER TABLE control_commands ADD COLUMN review_id TEXT NOT NULL DEFAULT ''"
        )
    rows = connection.execute(
        "SELECT command_id, parameters_json FROM control_commands WHERE review_id=''"
    ).fetchall()
    for row in rows:
        review_id = str(_json_object(row["parameters_json"]).get("review_id") or "").strip()
        if re.fullmatch(r"review_[0-9a-f]{24}", review_id):
            connection.execute(
                "UPDATE control_commands SET review_id=? WHERE command_id=?",
                (review_id, str(row["command_id"])),
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_control_commands_review_requested "
        "ON control_commands(review_id, requested_at DESC)"
    )


def _existing_schema_version(path: Path) -> int:
    if not path.is_file() or path.stat().st_size <= 0:
        return 0
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='control_meta'"
        ).fetchone()
        if table is None:
            return 0
        row = connection.execute(
            "SELECT value FROM control_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return 0
    finally:
        if connection is not None:
            connection.close()


def canonical_review_key(
    kind: str,
    target_key: str,
    diagnosis: dict[str, Any] | None = None,
) -> str:
    """Return the stable identity shared by every review for one torrent.

    Only an exact BitTorrent v1 info hash is strong enough to merge target
    ambiguity reviews.  Names, episode numbers and paths are deliberately not
    used because each of them can refer to a different series or season.
    """

    if str(kind or "").strip().casefold() != "target_ambiguity":
        return ""
    values = [str((diagnosis or {}).get("torrent_hash") or ""), str(target_key or "")]
    for value in values:
        match = re.search(r"(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])", value.strip(), flags=re.IGNORECASE)
        if match:
            return f"torrent:{match.group(1).casefold()}"
    return ""


def _migrate_review_identity(connection: sqlite3.Connection, *, now: float) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(review_items)").fetchall()
    }
    if "canonical_key" not in columns:
        connection.execute(
            "ALTER TABLE review_items ADD COLUMN canonical_key TEXT NOT NULL DEFAULT ''"
        )

    rows = connection.execute(
        """
        SELECT review_id, kind, target_key, status, diagnosis_json,
               candidates_json, resolution_json, updated_at
        FROM review_items
        WHERE kind='target_ambiguity'
        ORDER BY updated_at DESC, review_id
        """
    ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        canonical_key = canonical_review_key(
            str(row["kind"]),
            str(row["target_key"]),
            _json_object(row["diagnosis_json"]),
        )
        if not canonical_key:
            continue
        connection.execute(
            "UPDATE review_items SET canonical_key=? WHERE review_id=?",
            (canonical_key, str(row["review_id"])),
        )
        if str(row["status"] or "") == "open":
            groups.setdefault(canonical_key, []).append(row)

    for canonical_key, duplicates in groups.items():
        if len(duplicates) < 2:
            continue
        keeper = max(duplicates, key=_review_row_quality)
        keeper_id = str(keeper["review_id"])
        merged_diagnosis = _json_object(keeper["diagnosis_json"])
        merged_candidates = _json_list(keeper["candidates_json"])
        for row in duplicates:
            review_id = str(row["review_id"])
            if review_id == keeper_id:
                continue
            merged_diagnosis = _merge_review_diagnosis(
                merged_diagnosis,
                _json_object(row["diagnosis_json"]),
            )
            merged_candidates = _merge_review_candidates(
                merged_candidates,
                _json_list(row["candidates_json"]),
            )
            resolution = {
                **_json_object(row["resolution_json"]),
                "duplicate_of": keeper_id,
                "reason": "canonical_identity_migration",
            }
            connection.execute(
                """
                UPDATE review_items
                SET status='resolved', resolution_json=?, resolved_at=?, updated_at=?
                WHERE review_id=? AND status='open'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM control_commands active
                      WHERE active.review_id=review_items.review_id
                        AND active.status IN ('queued', 'running')
                  )
                """,
                (json.dumps(resolution, ensure_ascii=False), now, now, review_id),
            )
        connection.execute(
            """
            UPDATE review_items
            SET canonical_key=?, diagnosis_json=?, candidates_json=?, updated_at=?
            WHERE review_id=?
            """,
            (
                canonical_key,
                json.dumps(merged_diagnosis, ensure_ascii=False),
                json.dumps(merged_candidates, ensure_ascii=False),
                now,
                keeper_id,
            ),
        )

    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_review_items_open_canonical
        ON review_items(kind, canonical_key)
        WHERE status='open' AND canonical_key<>''
        """
    )


def _review_row_quality(row: sqlite3.Row) -> tuple[int, int, float, str]:
    candidates = _json_list(row["candidates_json"])
    diagnosis = _json_object(row["diagnosis_json"])
    evidence = sum(1 for value in diagnosis.values() if value not in (None, "", [], {}))
    return (len(candidates), evidence, float(row["updated_at"] or 0), str(row["review_id"]))


def _review_file_metadata(
    diagnosis: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enriched_diagnosis = dict(diagnosis)
    video_path = str(enriched_diagnosis.get("video") or "").strip()
    if video_path:
        metadata = file_time_metadata(video_path)
        if metadata:
            enriched_diagnosis["media_file"] = metadata

    enriched_candidates: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, dict):
            continue
        candidate = dict(raw_candidate)
        candidate_path = str(candidate.get("path") or candidate.get("series_path") or "").strip()
        if candidate_path:
            metadata = file_time_metadata(candidate_path)
            if metadata:
                candidate["file_info"] = metadata
        enriched_candidates.append(candidate)
    return enriched_diagnosis, enriched_candidates


def _migrate_open_review_file_metadata(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT review_id, diagnosis_json, candidates_json
        FROM review_items
        WHERE status='open'
        """
    ).fetchall()
    for row in rows:
        diagnosis = _json_object(row["diagnosis_json"])
        candidates = [item for item in _json_list(row["candidates_json"]) if isinstance(item, dict)]
        enriched_diagnosis, enriched_candidates = _review_file_metadata(diagnosis, candidates)
        if enriched_diagnosis == diagnosis and enriched_candidates == candidates:
            continue
        connection.execute(
            "UPDATE review_items SET diagnosis_json=?, candidates_json=? WHERE review_id=?",
            (
                json.dumps(enriched_diagnosis, ensure_ascii=False),
                json.dumps(enriched_candidates, ensure_ascii=False),
                str(row["review_id"]),
            ),
        )


def _merge_review_diagnosis(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            combined: list[Any] = []
            seen: set[str] = set()
            for item in [*merged[key], *value]:
                marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if marker in seen:
                    continue
                seen.add(marker)
                combined.append(item)
            merged[key] = combined
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _merge_review_candidates(existing: list[Any], incoming: list[Any]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in [*existing, *incoming]:
        if not isinstance(raw, dict):
            continue
        identity = str(raw.get("path") or raw.get("series_path") or "").strip()
        if not identity:
            identity = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        previous = merged.get(identity)
        if previous is None or float(raw.get("score") or 0) >= float(previous.get("score") or 0):
            merged[identity] = dict(raw)
    return sorted(
        merged.values(),
        key=lambda item: (float(item.get("score") or 0), str(item.get("path") or "")),
        reverse=True,
    )[:20]


def _prune_control_history(connection: sqlite3.Connection, *, now: float) -> None:
    row = connection.execute(
        "SELECT value FROM control_meta WHERE key='history_pruned_at'"
    ).fetchone()
    if row is not None:
        try:
            if now - float(row[0]) < 6 * 3600:
                return
        except (TypeError, ValueError):
            pass
    connection.execute(
        "DELETE FROM operation_audit WHERE created_at < ?",
        (now - CONTROL_HISTORY_RETENTION_DAYS * 86400,),
    )
    cutoff = connection.execute(
        "SELECT id FROM operation_audit ORDER BY id DESC LIMIT 1 OFFSET ?",
        (CONTROL_HISTORY_MAX_ROWS,),
    ).fetchone()
    if cutoff is not None:
        connection.execute("DELETE FROM operation_audit WHERE id <= ?", (int(cutoff[0]),))
    connection.execute(
        """
        INSERT INTO control_meta(key, value, updated_at) VALUES('history_pruned_at', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (str(now), now),
    )


def _stable_id(prefix: str, *values: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(value) for value in values).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def enqueue_command(
    config: Any,
    *,
    action: str,
    target: str = "",
    parameters: dict[str, Any] | None = None,
    idempotency_key: str,
    retry_failed: bool = False,
) -> dict[str, Any]:
    initialize_control_state(config)
    now = time.time()
    command_id = _stable_id("cmd", idempotency_key)
    normalized_parameters = parameters or {}
    payload = json.dumps(normalized_parameters, ensure_ascii=False, sort_keys=True)
    review_id = str(normalized_parameters.get("review_id") or "").strip()
    if not re.fullmatch(r"review_[0-9a-f]{24}", review_id):
        review_id = ""
    with control_connection(config) as connection:
        connection.execute(
            """
            INSERT INTO control_commands(
                command_id, action, target, review_id, parameters_json, idempotency_key, requested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (command_id, str(action), str(target), review_id, payload, str(idempotency_key), now),
        )
        row = connection.execute(
            "SELECT * FROM control_commands WHERE idempotency_key = ?",
            (str(idempotency_key),),
        ).fetchone()
        if row is not None and (
            str(row["action"] or "") != str(action)
            or str(row["target"] or "") != str(target)
            or str(row["parameters_json"] or "{}") != payload
        ):
            raise ValueError("idempotency key was already used with a different command payload")
        if row is not None and retry_failed and str(row["status"] or "") == "failed":
            connection.execute(
                """
                UPDATE control_commands
                SET status='queued', result_json='{}', error='',
                    requested_at=?, started_at=0, finished_at=0, worker_id=''
                WHERE command_id=? AND status='failed'
                """,
                (now, str(row["command_id"])),
            )
            _audit(
                connection,
                "command_requeued",
                "command",
                str(row["command_id"]),
                {"reason": "retryable_pre_execution_failure"},
            )
            row = connection.execute(
                "SELECT * FROM control_commands WHERE idempotency_key = ?",
                (str(idempotency_key),),
            ).fetchone()
    return _public_command(row)


def claim_next_command(
    config: Any,
    *,
    worker_id: str,
    allowed_actions: set[str] | None = None,
) -> ControlCommand | None:
    initialize_control_state(config)
    with control_connection(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if allowed_actions is None:
            row = connection.execute(
                """
                SELECT * FROM control_commands
                WHERE status = 'queued'
                ORDER BY requested_at, command_id
                LIMIT 1
                """
            ).fetchone()
        else:
            normalized_actions = sorted({str(value) for value in allowed_actions if str(value)})
            if not normalized_actions:
                connection.commit()
                return None
            placeholders = ",".join("?" for _ in normalized_actions)
            row = connection.execute(
                f"""
                SELECT * FROM control_commands
                WHERE status = 'queued' AND action IN ({placeholders})
                ORDER BY requested_at, command_id
                LIMIT 1
                """,
                normalized_actions,
            ).fetchone()
        if row is None:
            connection.commit()
            return None
        now = time.time()
        changed = connection.execute(
            """
            UPDATE control_commands
            SET status='running', started_at=?, worker_id=?
            WHERE command_id=? AND status='queued'
            """,
            (now, worker_id, str(row["command_id"])),
        ).rowcount
        connection.commit()
        if changed != 1:
            return None
    return ControlCommand(
        command_id=str(row["command_id"]),
        action=str(row["action"]),
        target=str(row["target"] or ""),
        parameters=_json_object(row["parameters_json"]),
        requested_at=float(row["requested_at"] or 0),
    )


def finish_command(
    config: Any,
    command_id: str,
    *,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    status = "failed" if error else "completed"
    now = time.time()
    with control_connection(config) as connection:
        connection.execute(
            """
            UPDATE control_commands
            SET status=?, result_json=?, error=?, finished_at=?
            WHERE command_id=? AND status='running'
            """,
            (status, json.dumps(result or {}, ensure_ascii=False), str(error)[:4000], now, str(command_id)),
        )
        _audit(connection, f"command_{status}", "command", command_id, result or {"error": error})


def get_command(config: Any, command_id: str) -> dict[str, Any] | None:
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            "SELECT * FROM control_commands WHERE command_id=?", (str(command_id),)
        ).fetchone()
    return _public_command(row) if row is not None else None


def reconcile_stale_running_commands(
    config: Any,
    *,
    stale_after_seconds: int = 1800,
) -> int:
    """Fail closed for commands whose worker disappeared before recording a result.

    Replaying an arbitrary command is not exactly-once safe.  A durable
    remediation campaign reconciles its own item ledger separately; ordinary
    commands are therefore marked failed and require an explicit new request.
    """

    initialize_control_state(config)
    now = time.time()
    cutoff = now - max(60, int(stale_after_seconds or 0))
    with control_connection(config) as connection:
        rows = connection.execute(
            """
            SELECT command_id
            FROM control_commands
            WHERE status='running' AND started_at > 0 AND started_at <= ?
            """,
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        command_ids = [str(row["command_id"]) for row in rows]
        placeholders = ",".join("?" for _ in command_ids)
        changed = connection.execute(
            f"""
            UPDATE control_commands
            SET status='failed',
                error='stale running command requires reconciliation',
                finished_at=?
            WHERE status='running' AND command_id IN ({placeholders})
            """,
            (now, *command_ids),
        ).rowcount
        for command_id in command_ids:
            _audit(
                connection,
                "command_stale_failed",
                "command",
                command_id,
                {"stale_after_seconds": max(60, int(stale_after_seconds or 0))},
            )
    return int(changed or 0)


def create_auto_remediation_campaign(
    config: Any,
    *,
    campaign_key: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    initialize_control_state(config)
    now = time.time()
    campaign_id = _stable_id("sweep", str(campaign_key))
    payload = json.dumps(parameters, ensure_ascii=False, sort_keys=True)
    with control_connection(config) as connection:
        connection.execute(
            """
            INSERT INTO auto_remediation_campaigns(
                campaign_id, state, parameters_json, counters_json,
                next_run_at, created_at, updated_at
            ) VALUES (?, 'running', ?, '{}', ?, ?, ?)
            ON CONFLICT(campaign_id) DO NOTHING
            """,
            (campaign_id, payload, now, now, now),
        )
        row = connection.execute(
            "SELECT * FROM auto_remediation_campaigns WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("auto remediation campaign could not be created")
        if str(row["parameters_json"] or "{}") != payload:
            raise ValueError("campaign key was already used with different parameters")
        _audit(
            connection,
            "auto_remediation_campaign_started",
            "auto_remediation_campaign",
            campaign_id,
            parameters,
        )
    return _public_auto_remediation_campaign(row)


def get_auto_remediation_campaign(
    config: Any,
    campaign_id: str,
) -> dict[str, Any] | None:
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            "SELECT * FROM auto_remediation_campaigns WHERE campaign_id=?",
            (str(campaign_id),),
        ).fetchone()
    return _public_auto_remediation_campaign(row) if row is not None else None


def latest_auto_remediation_campaign(config: Any) -> dict[str, Any] | None:
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM auto_remediation_campaigns
            ORDER BY created_at DESC, campaign_id DESC
            LIMIT 1
            """
        ).fetchone()
    return _public_auto_remediation_campaign(row) if row is not None else None


def due_auto_remediation_campaign(config: Any, *, now: float | None = None) -> dict[str, Any] | None:
    initialize_control_state(config)
    effective_now = time.time() if now is None else float(now)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM auto_remediation_campaigns
            WHERE state='running' AND next_run_at <= ?
            ORDER BY next_run_at, created_at, campaign_id
            LIMIT 1
            """,
            (effective_now,),
        ).fetchone()
    return _public_auto_remediation_campaign(row) if row is not None else None


def processed_auto_remediation_keys(config: Any) -> set[tuple[str, str, str]]:
    """Return semantic retry keys that have already consumed their one safe attempt."""

    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        rows = connection.execute(
            """
            SELECT path, failure_revision, strategy
            FROM auto_remediation_items
            """
        ).fetchall()
    return {
        (
            str(row["path"] or ""),
            str(row["failure_revision"] or ""),
            str(row["strategy"] or ""),
        )
        for row in rows
    }


def update_auto_remediation_campaign(
    config: Any,
    campaign_id: str,
    *,
    state: str | None = None,
    counters: dict[str, Any] | None = None,
    current_item_id: str | None = None,
    next_run_at: float | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    initialize_control_state(config)
    normalized_id = str(campaign_id)
    now = time.time()
    with control_connection(config) as connection:
        row = connection.execute(
            "SELECT * FROM auto_remediation_campaigns WHERE campaign_id=?",
            (normalized_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"auto remediation campaign does not exist: {normalized_id}")
        next_state = str(state if state is not None else row["state"])
        if next_state not in {"running", "paused", "completed", "cancelled", "failed"}:
            raise ValueError(f"invalid auto remediation campaign state: {next_state}")
        next_counters = counters if counters is not None else _json_object(row["counters_json"])
        finished_at = (
            now
            if next_state in {"completed", "cancelled", "failed"}
            else 0.0
        )
        connection.execute(
            """
            UPDATE auto_remediation_campaigns
            SET state=?, counters_json=?, current_item_id=?, next_run_at=?,
                last_error=?, updated_at=?, finished_at=?
            WHERE campaign_id=?
            """,
            (
                next_state,
                json.dumps(next_counters, ensure_ascii=False, sort_keys=True),
                str(current_item_id if current_item_id is not None else row["current_item_id"] or ""),
                float(next_run_at if next_run_at is not None else row["next_run_at"] or 0),
                str(last_error if last_error is not None else row["last_error"] or "")[:4000],
                now,
                finished_at,
                normalized_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM auto_remediation_campaigns WHERE campaign_id=?",
            (normalized_id,),
        ).fetchone()
        _audit(
            connection,
            "auto_remediation_campaign_updated",
            "auto_remediation_campaign",
            normalized_id,
            {"state": next_state, "current_item_id": str(current_item_id or "")},
        )
    return _public_auto_remediation_campaign(updated)


def create_auto_remediation_item(
    config: Any,
    *,
    campaign_id: str,
    path: str,
    failure_revision: str,
    strategy: str,
    before: dict[str, Any],
) -> dict[str, Any]:
    initialize_control_state(config)
    now = time.time()
    item_id = _stable_id(
        "sweepitem",
        str(campaign_id),
        str(path),
        str(failure_revision),
        str(strategy),
    )
    before_json = json.dumps(before, ensure_ascii=False, sort_keys=True)
    with control_connection(config) as connection:
        connection.execute(
            """
            INSERT INTO auto_remediation_items(
                item_id, campaign_id, path, failure_revision, strategy,
                status, before_json, claimed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            ON CONFLICT(path, failure_revision, strategy) DO NOTHING
            """,
            (
                item_id,
                str(campaign_id),
                str(path),
                str(failure_revision),
                str(strategy),
                before_json,
                now,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM auto_remediation_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT * FROM auto_remediation_items
                WHERE path=? AND failure_revision=? AND strategy=?
                """,
                (str(path), str(failure_revision), str(strategy)),
            ).fetchone()
        if (
            row is None
            or str(row["before_json"] or "{}") != before_json
            or str(row["campaign_id"] or "") != str(campaign_id)
        ):
            raise ValueError("auto remediation item semantic key conflicts with existing state")
        _audit(
            connection,
            "auto_remediation_item_created",
            "auto_remediation_item",
            str(row["item_id"]),
            {"campaign_id": campaign_id, "path": path, "strategy": strategy},
        )
    return _public_auto_remediation_item(row)


def get_auto_remediation_item(config: Any, item_id: str) -> dict[str, Any] | None:
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            "SELECT * FROM auto_remediation_items WHERE item_id=?",
            (str(item_id),),
        ).fetchone()
    return _public_auto_remediation_item(row) if row is not None else None


def update_auto_remediation_item(
    config: Any,
    item_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    normalized_status = str(status)
    if normalized_status not in {"queued", "running", "succeeded", "failed", "blocked_review", "skipped"}:
        raise ValueError(f"invalid auto remediation item status: {normalized_status}")
    initialize_control_state(config)
    now = time.time()
    finished_at = now if normalized_status in {"succeeded", "failed", "blocked_review", "skipped"} else 0.0
    with control_connection(config) as connection:
        changed = connection.execute(
            """
            UPDATE auto_remediation_items
            SET status=?, result_json=?, error=?, updated_at=?, finished_at=?
            WHERE item_id=?
            """,
            (
                normalized_status,
                json.dumps(result or {}, ensure_ascii=False, sort_keys=True),
                str(error)[:4000],
                now,
                finished_at,
                str(item_id),
            ),
        ).rowcount
        if changed != 1:
            raise ValueError(f"auto remediation item does not exist: {item_id}")
        row = connection.execute(
            "SELECT * FROM auto_remediation_items WHERE item_id=?",
            (str(item_id),),
        ).fetchone()
        _audit(
            connection,
            f"auto_remediation_item_{normalized_status}",
            "auto_remediation_item",
            str(item_id),
            result or {"error": error},
        )
    return _public_auto_remediation_item(row)


def auto_remediation_status(config: Any) -> dict[str, Any]:
    campaign = latest_auto_remediation_campaign(config)
    if campaign is None:
        return {
            "available": True,
            "state": "idle",
            "campaign_id": "",
            "counters": {},
            "current_item": None,
        }
    current_item = None
    if campaign.get("current_item_id"):
        current_item = get_auto_remediation_item(config, str(campaign["current_item_id"]))
    return {
        "available": True,
        **campaign,
        "current_item": current_item,
    }


def _suppressed_review_id(
    connection: sqlite3.Connection,
    *,
    kind: str,
    target_key: str,
    canonical_key: str,
) -> str:
    """Return a user-dismissed source review that must not be reopened.

    A torrent info hash is the durable identity when it is available.  Older
    rows without a hash fall back to their exact target key.  This contract is
    deliberately limited to target ambiguity reviews: translation and ASR
    quality failures must never be hidden by a generic dismiss operation.
    """

    if str(kind or "").strip().casefold() != "target_ambiguity":
        return ""
    clauses = ["target_key=?"]
    parameters: list[Any] = [str(kind), str(target_key)]
    if canonical_key:
        clauses.append("canonical_key=?")
        parameters.append(str(canonical_key))
    rows = connection.execute(
        f"""
        SELECT review_id, resolution_json
        FROM review_items
        WHERE kind=? AND status='resolved' AND ({' OR '.join(clauses)})
        ORDER BY resolved_at DESC, updated_at DESC, review_id
        """,
        parameters,
    ).fetchall()
    for row in rows:
        resolution = _json_object(row["resolution_json"])
        if bool(resolution.get("suppress_reopen")) and bool(resolution.get("dismissed")):
            return str(row["review_id"])
    return ""


def upsert_review_item(
    config: Any,
    *,
    kind: str,
    target_key: str,
    summary: str,
    diagnosis: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    severity: str = "warning",
    canonical_key: str = "",
    replace_candidates: bool = False,
) -> str:
    initialize_control_state(config)
    normalized_kind = str(kind).strip()
    normalized_target = str(target_key).strip()
    stable_key = str(canonical_key or "").strip() or canonical_review_key(
        normalized_kind,
        normalized_target,
        diagnosis,
    )
    diagnosis_payload, candidates_payload = _review_file_metadata(
        dict(diagnosis or {}),
        list(candidates or []),
    )
    review_id = _stable_id("review", normalized_kind, stable_key or normalized_target)
    now = time.time()
    with control_connection(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        suppressed_id = _suppressed_review_id(
            connection,
            kind=normalized_kind,
            target_key=normalized_target,
            canonical_key=stable_key,
        )
        if suppressed_id:
            return suppressed_id
        existing = None
        if stable_key:
            existing = connection.execute(
                """
                SELECT * FROM review_items
                WHERE kind=? AND canonical_key=? AND status='open'
                ORDER BY updated_at DESC, review_id
                LIMIT 1
                """,
                (normalized_kind, stable_key),
            ).fetchone()
        if existing is not None:
            review_id = str(existing["review_id"])
            merged_diagnosis = _merge_review_diagnosis(
                _json_object(existing["diagnosis_json"]),
                diagnosis_payload,
            )
            merged_candidates = _merge_review_candidates(
                [] if replace_candidates else _json_list(existing["candidates_json"]),
                candidates_payload,
            )
            connection.execute(
                """
                UPDATE review_items
                SET severity=?, summary=?, diagnosis_json=?, candidates_json=?,
                    resolution_json='{}', resolved_at=0, updated_at=?
                WHERE review_id=?
                """,
                (
                    str(severity),
                    str(summary)[:1000],
                    json.dumps(merged_diagnosis, ensure_ascii=False),
                    json.dumps(merged_candidates, ensure_ascii=False),
                    now,
                    review_id,
                ),
            )
            _audit(
                connection,
                "review_upserted",
                "review",
                review_id,
                {
                    "kind": normalized_kind,
                    "target_key": normalized_target,
                    "canonical_key": stable_key,
                    "candidate_mode": "replace" if replace_candidates else "merge",
                },
            )
            return review_id
        connection.execute(
            """
            INSERT INTO review_items(
                review_id, kind, target_key, canonical_key, severity, summary,
                diagnosis_json, candidates_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, target_key) DO UPDATE SET
                status='open', severity=excluded.severity, summary=excluded.summary,
                diagnosis_json=excluded.diagnosis_json, candidates_json=excluded.candidates_json,
                canonical_key=excluded.canonical_key, resolution_json='{}',
                resolved_at=0, updated_at=excluded.updated_at
            """,
            (
                review_id,
                normalized_kind,
                normalized_target,
                stable_key,
                str(severity),
                str(summary)[:1000],
                json.dumps(diagnosis_payload, ensure_ascii=False),
                json.dumps(candidates_payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT review_id FROM review_items WHERE kind=? AND target_key=?",
            (normalized_kind, normalized_target),
        ).fetchone()
        review_id = str(row["review_id"] if row is not None else review_id)
        _audit(
            connection,
            "review_upserted",
            "review",
            review_id,
            {"kind": normalized_kind, "target_key": normalized_target, "canonical_key": stable_key},
        )
    return review_id


def list_review_items(
    config: Any,
    *,
    status: str = "open",
    kind: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    initialize_control_state(config)
    clauses = ["1=1"]
    parameters: list[Any] = []
    if status:
        clauses.append("status=?")
        parameters.append(status)
    if kind:
        clauses.append("kind=?")
        parameters.append(kind)
    parameters.extend([max(1, min(200, int(limit))), max(0, int(offset))])
    with control_connection(config, readonly=True) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM review_items WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, review_id
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
    return [_public_review(row) for row in rows]


def list_open_ai_quality_review_targets(
    config: Any,
    *,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return compact unique targets eligible for strict publication reconciliation.

    This deliberately excludes target ambiguity reviews.  Keeping the query
    compact also avoids loading large historical diagnosis payloads merely to
    discover whether a verified newer publication can close a stale AI review.
    """

    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        rows = connection.execute(
            """
            SELECT target_key,
                   MAX(updated_at) AS latest_review_at,
                   COUNT(*) AS review_count
            FROM review_items
            WHERE status='open'
              AND kind IN ('subtitle_quality', 'asr_quality')
              AND target_key<>''
            GROUP BY target_key
            ORDER BY latest_review_at DESC, target_key
            LIMIT ?
            """,
            (max(1, min(5000, int(limit))),),
        ).fetchall()
    return [
        {
            "target_key": str(row["target_key"]),
            "latest_review_at": float(row["latest_review_at"] or 0),
            "review_count": int(row["review_count"] or 0),
        }
        for row in rows
    ]


def list_open_review_autopilot_candidates(
    config: Any,
    *,
    kind: str,
    idempotency_prefix: str,
    required_completed_prefix: str = "",
    allow_revision_scoped_attempts: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return old-to-new open reviews not yet attempted by one autopilot policy.

    The durable command idempotency key is also the attempt ledger.  By
    default a review is considered once per policy; revision-scoped policies
    may opt into later filtering against their bounded revision ledger.
    Active operator commands always own their review and exclude it from
    automatic selection.
    """

    normalized_kind = str(kind or "").strip().casefold()
    if normalized_kind not in {"asr_quality", "subtitle_quality", "target_ambiguity"}:
        raise ValueError(f"unsupported review autopilot kind: {kind}")
    normalized_prefix = str(idempotency_prefix or "").strip()
    if not normalized_prefix or len(normalized_prefix) > 180:
        raise ValueError("review autopilot idempotency prefix is invalid")
    normalized_required_prefix = str(required_completed_prefix or "").strip()
    if normalized_required_prefix and len(normalized_required_prefix) > 180:
        raise ValueError("required review autopilot prefix is invalid")
    normalized_offset = int(offset)
    if normalized_offset < 0 or normalized_offset > 100_000:
        raise ValueError("review autopilot candidate offset is invalid")
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        rows = connection.execute(
            """
            SELECT r.*
            FROM review_items r
            WHERE r.status='open'
              AND r.kind=?
              AND r.target_key<>''
              AND (
                  ?=1
                  OR NOT EXISTS (
                      SELECT 1
                      FROM control_commands attempted
                      WHERE attempted.idempotency_key = ? || r.review_id
                  )
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM control_commands active
                  WHERE active.review_id=r.review_id
                    AND active.status IN ('queued', 'running')
              )
              AND (
                  ?=''
                  OR EXISTS (
                      SELECT 1
                      FROM control_commands prerequisite
                      WHERE (
                            prerequisite.idempotency_key = ? || r.review_id
                            OR substr(
                                   prerequisite.idempotency_key,
                                   1,
                                   length(? || r.review_id || ':')
                               ) = ? || r.review_id || ':'
                        )
                        AND prerequisite.status='completed'
                  )
              )
            ORDER BY r.created_at, r.updated_at, r.review_id
            LIMIT ? OFFSET ?
            """,
            (
                normalized_kind,
                1 if allow_revision_scoped_attempts else 0,
                normalized_prefix,
                normalized_required_prefix,
                normalized_required_prefix,
                normalized_required_prefix,
                normalized_required_prefix,
                max(1, min(500, int(limit))),
                normalized_offset,
            ),
        ).fetchall()
    return [_public_review(row) for row in rows]


def review_autopilot_revision_attempt_allowed(
    config: Any,
    *,
    idempotency_prefix: str,
    review_id: str,
    failure_revision: str,
    max_attempts: int,
) -> bool:
    """Return whether one policy may try a new failure revision.

    Both the legacy exact key and revision-scoped keys count toward the fixed
    attempt budget.  A revision already represented by either form is
    idempotently rejected, and any active operator command keeps ownership of
    the review.
    """

    normalized_prefix = str(idempotency_prefix or "").strip()
    normalized_review_id = str(review_id or "").strip()
    normalized_revision = str(failure_revision or "").strip()
    bounded_max_attempts = int(max_attempts)
    if not normalized_prefix or len(normalized_prefix) > 180:
        raise ValueError("review autopilot idempotency prefix is invalid")
    if not re.fullmatch(r"review_[0-9a-f]{24}", normalized_review_id):
        raise ValueError("review autopilot review id is invalid")
    if not normalized_revision or len(normalized_revision) > 200:
        raise ValueError("review autopilot failure revision is invalid")
    if bounded_max_attempts < 1 or bounded_max_attempts > 20:
        raise ValueError("review autopilot max attempts is invalid")

    initialize_control_state(config)
    legacy_key = f"{normalized_prefix}{normalized_review_id}"
    scoped_key = f"{legacy_key}:{normalized_revision}"
    with control_connection(config, readonly=True) as connection:
        active = connection.execute(
            """
            SELECT 1
            FROM control_commands
            WHERE review_id=? AND status IN ('queued', 'running')
            LIMIT 1
            """,
            (normalized_review_id,),
        ).fetchone()
        if active is not None:
            return False
        scoped_prefix = f"{legacy_key}:"
        rows = connection.execute(
            """
            SELECT idempotency_key, parameters_json
            FROM control_commands
            WHERE idempotency_key=?
               OR substr(idempotency_key, 1, length(?))=?
            ORDER BY requested_at, command_id
            """,
            (legacy_key, scoped_prefix, scoped_prefix),
        ).fetchall()

    if len(rows) >= bounded_max_attempts:
        return False
    for row in rows:
        key = str(row["idempotency_key"] or "")
        if key == scoped_key:
            return False
        try:
            parameters = json.loads(str(row["parameters_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parameters = None
        if not isinstance(parameters, dict):
            if key == legacy_key:
                return False
            continue
        recorded_revision = str(
            parameters.get("expected_failure_revision") or ""
        ).strip()
        if recorded_revision == normalized_revision:
            return False
        if key == legacy_key and not recorded_revision:
            # An unversioned legacy attempt cannot be proven distinct from the
            # current failure, so fail closed instead of duplicating work.
            return False
    return True


def next_review_autopilot_retry_attempt(
    config: Any,
    *,
    idempotency_prefix: str,
    review_id: str,
    max_attempts: int,
) -> int | None:
    """Return the next bounded retry number for one fail-closed review policy."""

    normalized_prefix = str(idempotency_prefix or "").strip()
    normalized_review_id = str(review_id or "").strip()
    bounded_max_attempts = int(max_attempts)
    if not normalized_prefix or len(normalized_prefix) > 180:
        raise ValueError("review autopilot idempotency prefix is invalid")
    if not re.fullmatch(r"review_[0-9a-f]{24}", normalized_review_id):
        raise ValueError("review autopilot review id is invalid")
    if bounded_max_attempts < 1 or bounded_max_attempts > 20:
        raise ValueError("review autopilot max attempts is invalid")

    initialize_control_state(config)
    base_key = f"{normalized_prefix}{normalized_review_id}"
    attempt_prefix = f"{base_key}:attempt-"
    with control_connection(config, readonly=True) as connection:
        rows = connection.execute(
            """
            SELECT status
            FROM control_commands
            WHERE review_id=?
              AND (
                    idempotency_key=?
                    OR substr(idempotency_key, 1, length(?))=?
              )
            ORDER BY requested_at, command_id
            """,
            (
                normalized_review_id,
                base_key,
                attempt_prefix,
                attempt_prefix,
            ),
        ).fetchall()
    statuses = [str(row["status"] or "").strip().casefold() for row in rows]
    if any(status in {"queued", "running", "completed"} for status in statuses):
        return None
    if any(status != "failed" for status in statuses):
        return None
    return len(statuses) + 1 if len(statuses) < bounded_max_attempts else None


def latest_review_autopilot_command(
    config: Any,
    *,
    idempotency_prefix: str,
) -> dict[str, Any] | None:
    """Return the latest command created by one review-autopilot policy."""

    normalized_prefix = str(idempotency_prefix or "").strip()
    if not normalized_prefix or len(normalized_prefix) > 180:
        raise ValueError("review autopilot idempotency prefix is invalid")
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM control_commands
            WHERE substr(idempotency_key, 1, length(?)) = ?
            ORDER BY requested_at DESC, command_id DESC
            LIMIT 1
            """,
            (normalized_prefix, normalized_prefix),
        ).fetchone()
    return _public_command(row) if row is not None else None


def active_review_autopilot_command(
    config: Any,
    *,
    idempotency_prefix: str,
) -> dict[str, Any] | None:
    """Return one queued/running command reserved by an autopilot policy."""

    normalized_prefix = str(idempotency_prefix or "").strip()
    if not normalized_prefix or len(normalized_prefix) > 180:
        raise ValueError("review autopilot idempotency prefix is invalid")
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM control_commands
            WHERE substr(idempotency_key, 1, length(?)) = ?
              AND status IN ('queued', 'running')
            ORDER BY requested_at, command_id
            LIMIT 1
            """,
            (normalized_prefix, normalized_prefix),
        ).fetchone()
    return _public_command(row) if row is not None else None


def get_review_item(config: Any, review_id: str) -> dict[str, Any] | None:
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            "SELECT * FROM review_items WHERE review_id=?",
            (str(review_id),),
        ).fetchone()
    return _public_review(row) if row is not None else None


def open_ai_quality_review_for_target(config: Any, target: str) -> dict[str, Any] | None:
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM review_items
            WHERE target_key=?
              AND kind IN ('subtitle_quality', 'asr_quality')
              AND status='open'
            ORDER BY updated_at DESC, review_id
            LIMIT 1
            """,
            (str(target),),
        ).fetchone()
    return _public_review(row) if row is not None else None


def active_review_command_ids(config: Any, review_ids: list[str] | set[str]) -> set[str]:
    """Return review ids that currently have a queued or running command.

    Source-lifecycle reconciliation runs independently from the command
    consumer.  Checking the durable command identity before auto-resolving a
    review prevents a qBittorrent poll from closing an item while the operator's
    confirmation is already waiting in the Worker mailbox.
    """

    normalized = sorted({
        str(review_id).strip()
        for review_id in review_ids
        if str(review_id).strip()
    })
    if not normalized:
        return set()
    initialize_control_state(config)
    active: set[str] = set()
    with control_connection(config, readonly=True) as connection:
        for start in range(0, len(normalized), 400):
            chunk = normalized[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT DISTINCT review_id
                FROM control_commands
                WHERE review_id IN ({placeholders})
                  AND status IN ('queued', 'running')
                """,
                chunk,
            ).fetchall()
            active.update(str(row[0]) for row in rows if str(row[0] or ""))
    return active


def update_review_source_lifecycle(
    config: Any,
    review_id: str,
    *,
    lifecycle: str,
    torrent_in_qbit: bool,
    source_files_present: bool,
    redownload_available: bool,
    processing: bool,
    source_path: str = "",
    missing_since: float = 0.0,
) -> bool:
    """Persist a target-review source state without changing inbox ordering.

    Poll timestamps alone must not make an old review appear new.  Therefore
    the row is written only when the meaningful lifecycle contract changes and
    ``updated_at`` is deliberately left untouched.
    """

    normalized_lifecycle = str(lifecycle or "").strip().casefold()
    if normalized_lifecycle not in {
        "qbit_present",
        "source_files_present",
        "redownload_available",
        "processing",
        "source_unavailable_pending",
        "source_gone",
        "unknown",
    }:
        raise ValueError(f"Unsupported review source lifecycle: {lifecycle}")
    normalized = {
        "source_lifecycle": normalized_lifecycle,
        "source_torrent_in_qbit": bool(torrent_in_qbit),
        "source_files_present": bool(source_files_present),
        "source_redownload_available": bool(redownload_available),
        "source_processing": bool(processing),
        "source_existing_path": str(source_path or ""),
        "source_missing_since": max(0.0, float(missing_since or 0.0)),
    }
    initialize_control_state(config)
    with control_connection(config) as connection:
        row = connection.execute(
            """
            SELECT status, diagnosis_json
            FROM review_items
            WHERE review_id=?
            """,
            (str(review_id),),
        ).fetchone()
        if row is None or str(row["status"] or "") != "open":
            return False
        diagnosis = _json_object(row["diagnosis_json"])
        previous = {key: diagnosis.get(key) for key in normalized}
        if previous == normalized:
            return False
        diagnosis.update(normalized)
        diagnosis["source_lifecycle_checked_at"] = time.time()
        changed = connection.execute(
            """
            UPDATE review_items
            SET diagnosis_json=?
            WHERE review_id=? AND status='open'
            """,
            (json.dumps(diagnosis, ensure_ascii=False), str(review_id)),
        ).rowcount
        if changed:
            _audit(
                connection,
                "review_source_lifecycle_changed",
                "review",
                str(review_id),
                normalized,
            )
    return changed == 1


def resolve_review_item(config: Any, review_id: str, resolution: dict[str, Any]) -> bool:
    now = time.time()
    with control_connection(config) as connection:
        changed = connection.execute(
            """
            UPDATE review_items
            SET status='resolved', resolution_json=?, resolved_at=?, updated_at=?
            WHERE review_id=? AND status='open'
            """,
            (json.dumps(resolution, ensure_ascii=False), now, now, str(review_id)),
        ).rowcount
        if changed:
            _audit(connection, "review_resolved", "review", review_id, resolution)
    return changed == 1


def dismiss_review_item(
    config: Any,
    review_id: str,
    *,
    reason: str = "dismissed_by_user",
) -> bool:
    """Resolve one source-pairing review and suppress the same source identity.

    The review row and audit history are retained.  No media, subtitle,
    torrent, download, or Mikan state is deleted here.
    """

    initialize_control_state(config)
    now = time.time()
    normalized_id = str(review_id).strip()
    resolution = {
        "action": "dismiss_review",
        "reason": str(reason or "dismissed_by_user")[:200],
        "dismissed": True,
        "suppress_reopen": True,
        "resolved_by": "operator",
    }
    with control_connection(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT kind, status FROM review_items WHERE review_id=?",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return False
        if str(row["kind"] or "") != "target_ambiguity":
            raise ValueError("only target ambiguity reviews can be dismissed")
        if str(row["status"] or "") != "open":
            return False
        changed = connection.execute(
            """
            UPDATE review_items
            SET status='resolved', resolution_json=?, resolved_at=?, updated_at=?
            WHERE review_id=? AND status='open'
            """,
            (json.dumps(resolution, ensure_ascii=False), now, now, normalized_id),
        ).rowcount
        if changed:
            _audit(connection, "review_dismissed", "review", normalized_id, resolution)
    return changed == 1


def resolve_review_item_if_idle(config: Any, review_id: str, resolution: dict[str, Any]) -> bool:
    """Resolve an open review only when no operator command is in flight."""

    now = time.time()
    with control_connection(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """
            UPDATE review_items
            SET status='resolved', resolution_json=?, resolved_at=?, updated_at=?
            WHERE review_id=? AND status='open'
              AND NOT EXISTS (
                  SELECT 1
                  FROM control_commands
                  WHERE review_id=?
                    AND status IN ('queued', 'running')
              )
            """,
            (
                json.dumps(resolution, ensure_ascii=False),
                now,
                now,
                str(review_id),
                str(review_id),
            ),
        ).rowcount
        if changed:
            _audit(connection, "review_resolved", "review", review_id, resolution)
    return changed == 1


def resolve_ai_quality_reviews_for_target_if_idle(
    config: Any,
    target_key: str,
    resolution: dict[str, Any],
) -> list[str]:
    """Resolve stale AI quality reviews after a verified Worker publication.

    Only AI subtitle/ASR quality kinds are eligible. A queued or running
    operator command owns its review and prevents automatic resolution, so the
    Worker cannot race a deliberate remediation. Target ambiguity reviews are
    outside this contract and are never selected.
    """

    initialize_control_state(config)
    normalized_target = str(target_key or "").strip()
    if not normalized_target:
        return []
    payload = {
        **dict(resolution or {}),
        "source": str((resolution or {}).get("source") or "worker"),
        "reason": str(
            (resolution or {}).get("reason")
            or "quality_gate_and_publication_succeeded"
        ),
    }
    now = time.time()
    resolved: list[str] = []
    with control_connection(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT review_id
            FROM review_items
            WHERE target_key=?
              AND kind IN ('subtitle_quality', 'asr_quality')
              AND status='open'
              AND NOT EXISTS (
                  SELECT 1
                  FROM control_commands
                  WHERE control_commands.review_id=review_items.review_id
                    AND control_commands.status IN ('queued', 'running')
              )
            ORDER BY updated_at, review_id
            """,
            (normalized_target,),
        ).fetchall()
        for row in rows:
            review_id = str(row["review_id"])
            changed = connection.execute(
                """
                UPDATE review_items
                SET status='resolved', resolution_json=?, resolved_at=?, updated_at=?
                WHERE review_id=? AND status='open'
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                    review_id,
                ),
            ).rowcount
            if changed:
                resolved.append(review_id)
                _audit(
                    connection,
                    "review_auto_resolved",
                    "review",
                    review_id,
                    payload,
                )
    return resolved


def resolve_sibling_target_reviews(
    config: Any,
    *,
    torrent_hash: str,
    exclude_review_id: str,
    resolution: dict[str, Any],
) -> list[str]:
    """Resolve duplicate open ambiguity reviews for the same exact torrent.

    A recovered qBittorrent mapping and a completed-download ambiguity can
    describe the same torrent under different target keys.  Once an operator
    has confirmed a validated series mapping, leaving the duplicate card open
    is misleading and can trigger a second, conflicting decision.
    """

    normalized_hash = str(torrent_hash or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized_hash):
        return []
    initialize_control_state(config)
    now = time.time()
    resolved: list[str] = []
    with control_connection(config) as connection:
        canonical_key = f"torrent:{normalized_hash}"
        rows = connection.execute(
            """
            SELECT r.review_id, r.diagnosis_json
            FROM review_items r
            WHERE r.kind='target_ambiguity' AND r.status='open' AND r.review_id<>?
              AND (r.canonical_key=? OR r.canonical_key='')
              AND NOT EXISTS (
                  SELECT 1
                  FROM control_commands active
                  WHERE active.review_id=r.review_id
                    AND active.status IN ('queued', 'running')
              )
            """,
            (str(exclude_review_id), canonical_key),
        ).fetchall()
        for row in rows:
            try:
                diagnosis = json.loads(str(row["diagnosis_json"] or "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            candidate_hash = str((diagnosis or {}).get("torrent_hash") or "").strip().casefold()
            if candidate_hash != normalized_hash:
                continue
            review_id = str(row["review_id"])
            sibling_resolution = {
                **resolution,
                "duplicate_of": str(exclude_review_id),
            }
            changed = connection.execute(
                """
                UPDATE review_items
                SET status='resolved', resolution_json=?, resolved_at=?, updated_at=?
                WHERE review_id=? AND status='open'
                """,
                (json.dumps(sibling_resolution, ensure_ascii=False), now, now, review_id),
            ).rowcount
            if changed:
                resolved.append(review_id)
                _audit(connection, "review_resolved", "review", review_id, sibling_resolution)
    return resolved


def upsert_series_source_mapping(
    config: Any,
    *,
    source: str,
    source_id: str | int,
    season: int,
    series_path: str,
    series_id: str = "",
    confidence: float = 1.0,
    locked: bool = True,
) -> None:
    initialize_control_state(config)
    now = time.time()
    with control_connection(config) as connection:
        connection.execute(
            """
            INSERT INTO series_source_mappings(
                source, source_id, season, series_path, series_id, confidence, locked, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id, season) DO UPDATE SET
                series_path=excluded.series_path, series_id=excluded.series_id,
                confidence=excluded.confidence, locked=excluded.locked, updated_at=excluded.updated_at
            """,
            (
                str(source).casefold(), str(source_id), int(season), str(series_path), str(series_id),
                max(0.0, min(1.0, float(confidence))), int(bool(locked)), now, now,
            ),
        )
        _audit(
            connection,
            "series_mapping_upserted",
            "series_mapping",
            f"{source}:{source_id}:{season}",
            {"series_path": series_path, "locked": bool(locked)},
        )


def locked_series_source_mappings(config: Any, *, source: str = "mikan") -> list[dict[str, Any]]:
    initialize_control_state(config)
    with control_connection(config, readonly=True) as connection:
        rows = connection.execute(
            """
            SELECT * FROM series_source_mappings
            WHERE source=? AND locked=1
            ORDER BY source_id, season
            """,
            (str(source).casefold(),),
        ).fetchall()
    return [dict(row) for row in rows]


def increment_daily_metric(config: Any, metric: str, value: float = 1.0) -> None:
    """Add one value to a UTC daily aggregate using a short transaction."""

    normalized = str(metric or "").strip()
    if not normalized:
        raise ValueError("metric name is required")
    initialize_control_state(config)
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    with control_connection(config) as connection:
        connection.execute(
            """
            INSERT INTO daily_metrics(day, metric, value, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(day, metric) DO UPDATE SET
                value=daily_metrics.value + excluded.value,
                updated_at=excluded.updated_at
            """,
            (day, normalized, float(value), now),
        )


def record_daily_sample(config: Any, metric: str, value: float) -> None:
    """Record count/sum/max aggregates for a numeric sample in one commit."""

    normalized = str(metric or "").strip()
    numeric = float(value)
    if not normalized:
        raise ValueError("metric name is required")
    if not (-1e308 < numeric < 1e308):
        raise ValueError("metric value must be finite")
    initialize_control_state(config)
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    with control_connection(config) as connection:
        for suffix, sample, mode in (
            ("count", 1.0, "sum"),
            ("sum", numeric, "sum"),
            ("max", numeric, "max"),
        ):
            metric_name = f"{normalized}.{suffix}"
            if mode == "max":
                connection.execute(
                    """
                    INSERT INTO daily_metrics(day, metric, value, updated_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(day, metric) DO UPDATE SET
                        value=MAX(daily_metrics.value, excluded.value),
                        updated_at=excluded.updated_at
                    """,
                    (day, metric_name, sample, now),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO daily_metrics(day, metric, value, updated_at)
                    VALUES(?, ?, ?, ?)
                    ON CONFLICT(day, metric) DO UPDATE SET
                        value=daily_metrics.value + excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    (day, metric_name, sample, now),
                )


def _audit(
    connection: sqlite3.Connection,
    event: str,
    entity_type: str,
    entity_id: str,
    detail: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO operation_audit(event, entity_type, entity_id, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(event), str(entity_type), str(entity_id), json.dumps(detail, ensure_ascii=False), time.time()),
    )


def _json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _public_command(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    payload = dict(row)
    payload["parameters"] = _json_object(payload.pop("parameters_json", "{}"))
    payload["result"] = _json_object(payload.pop("result_json", "{}"))
    payload.pop("idempotency_key", None)
    return payload


def _public_auto_remediation_campaign(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    payload = dict(row)
    payload["parameters"] = _json_object(payload.pop("parameters_json", "{}"))
    payload["counters"] = _json_object(payload.pop("counters_json", "{}"))
    return payload


def _public_auto_remediation_item(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    payload = dict(row)
    payload["before"] = _json_object(payload.pop("before_json", "{}"))
    payload["result"] = _json_object(payload.pop("result_json", "{}"))
    return payload


def _public_review(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["diagnosis"] = _json_object(payload.pop("diagnosis_json", "{}"))
    payload["candidates"] = _json_list(payload.pop("candidates_json", "[]"))
    payload["resolution"] = _json_object(payload.pop("resolution_json", "{}"))
    return payload
