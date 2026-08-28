from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from safe_files import fsync_directory, sha256_file, verified_move


class MikanCacheStoreError(RuntimeError):
    pass


def sqlite_cache_enabled(config: Any) -> bool:
    return bool(getattr(config, "mikan_sqlite_authoritative_state", False))


def mikan_state_db_path(config: Any) -> Path:
    work_path = Path(getattr(config, "work_path", "/work"))
    pending = Path(str(getattr(config, "mikan_pending_path", "mikan_pending.json") or "mikan_pending.json"))
    if not pending.is_absolute():
        pending = work_path / pending
    return pending.with_name("mikan_state.sqlite3")


def ensure_mikan_cache_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS mikan_cache_entries (
            namespace TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(namespace, cache_key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_mikan_cache_entries_updated_at "
        "ON mikan_cache_entries(namespace, updated_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS mikan_cache_namespaces (
            namespace TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            source_path TEXT NOT NULL DEFAULT '',
            imported_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


class MikanIndexedCache:
    """Small indexed cache stored in the authoritative Mikan SQLite database.

    Each logical cache entry is a separate row.  This avoids repeatedly parsing
    and rewriting the large auto-match and fallback JSON files on hot paths.
    """

    def __init__(
        self,
        config: Any,
        *,
        namespace: str,
        legacy_path: Path,
        schema_version: int,
    ) -> None:
        normalized_namespace = str(namespace or "").strip()
        if not normalized_namespace:
            raise ValueError("cache namespace is required")
        self.db_path = mikan_state_db_path(config)
        self.namespace = normalized_namespace
        self.legacy_path = Path(legacy_path)
        self.schema_version = max(1, int(schema_version))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def initialized(self) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT schema_version FROM mikan_cache_namespaces WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
            return row is not None and int(row[0] or 0) == self.schema_version
        finally:
            connection.close()

    def initialize_if_needed(self, entries: Mapping[str, Any]) -> bool:
        """Atomically import legacy entries once.

        Returns True only for the process that performed the import.  Multiple
        startup workers may race here; BEGIN IMMEDIATE plus the namespace row
        makes the operation idempotent.
        """

        serialized = _serialize_entries(entries)
        connection = self._connect()
        imported = False
        now = time.time()
        try:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT schema_version FROM mikan_cache_namespaces WHERE namespace = ?",
                (self.namespace,),
            ).fetchone()
            if row is None:
                if serialized:
                    connection.executemany(
                        """
                        INSERT INTO mikan_cache_entries(namespace, cache_key, value_json, updated_at)
                        VALUES(?, ?, ?, ?)
                        """,
                        ((self.namespace, key, value, now) for key, value in serialized.items()),
                    )
                connection.execute(
                    """
                    INSERT INTO mikan_cache_namespaces(
                        namespace, schema_version, source_path, imported_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?)
                    """,
                    (self.namespace, self.schema_version, str(self.legacy_path), now, now),
                )
                imported = True
            elif int(row[0] or 0) != self.schema_version:
                connection.execute(
                    "DELETE FROM mikan_cache_entries WHERE namespace = ?",
                    (self.namespace,),
                )
                if serialized:
                    connection.executemany(
                        """
                        INSERT INTO mikan_cache_entries(namespace, cache_key, value_json, updated_at)
                        VALUES(?, ?, ?, ?)
                        """,
                        ((self.namespace, key, value, now) for key, value in serialized.items()),
                    )
                connection.execute(
                    "UPDATE mikan_cache_namespaces SET schema_version = ?, updated_at = ? WHERE namespace = ?",
                    (self.schema_version, now, self.namespace),
                )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

        if imported:
            archive_legacy_cache(self.legacy_path)
        return imported

    def load_all(self) -> dict[str, Any]:
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT cache_key, value_json
                FROM mikan_cache_entries
                WHERE namespace = ?
                ORDER BY cache_key
                """,
                (self.namespace,),
            ).fetchall()
        finally:
            connection.close()
        result: dict[str, Any] = {}
        for key, value_json in rows:
            try:
                result[str(key)] = json.loads(str(value_json))
            except json.JSONDecodeError as exc:
                raise MikanCacheStoreError(
                    f"Invalid cached JSON namespace={self.namespace} key={key}"
                ) from exc
        return result

    def replace_all(self, entries: Mapping[str, Any]) -> None:
        serialized = _serialize_entries(entries)
        connection = self._connect()
        now = time.time()
        try:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM mikan_cache_entries WHERE namespace = ?",
                (self.namespace,),
            )
            if serialized:
                connection.executemany(
                    """
                    INSERT INTO mikan_cache_entries(namespace, cache_key, value_json, updated_at)
                    VALUES(?, ?, ?, ?)
                    """,
                    ((self.namespace, key, value, now) for key, value in serialized.items()),
                )
            connection.execute(
                """
                UPDATE mikan_cache_namespaces
                SET schema_version = ?, updated_at = ?
                WHERE namespace = ?
                """,
                (self.schema_version, now, self.namespace),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=60,
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=60000")
            return connection
        connection = sqlite3.connect(self.db_path, timeout=60)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=60000")
        ensure_mikan_cache_tables(connection)
        return connection


def archive_legacy_cache(path: Path) -> Path | None:
    source = Path(path)
    if not source.is_file():
        return None
    destination = source.with_name(f"{source.stem}.legacy-readonly{source.suffix}")
    if destination.exists():
        if sha256_file(source) != sha256_file(destination):
            raise MikanCacheStoreError(f"Legacy cache backup already exists with different content: {destination}")
        source.unlink()
        fsync_directory(source.parent)
        return destination
    source_digest = sha256_file(source)
    verified_move(source, destination)
    if sha256_file(destination) != source_digest:
        raise MikanCacheStoreError(f"Legacy cache backup verification failed: {destination}")
    try:
        destination.chmod(0o444)
    except OSError:
        pass
    return destination


def _serialize_entries(entries: Mapping[str, Any]) -> dict[str, str]:
    serialized: dict[str, str] = {}
    for key, value in entries.items():
        serialized[str(key)] = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return serialized
