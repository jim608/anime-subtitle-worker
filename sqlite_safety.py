from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import time

from safe_files import atomic_write_text, fsync_directory, sha256_file


class SQLiteSafetyError(RuntimeError):
    pass


def quick_check_connection(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA quick_check").fetchall()
    messages = [str(row[0]) for row in rows if row]
    if messages != ["ok"]:
        raise SQLiteSafetyError(f"SQLite quick_check failed: {'; '.join(messages) or 'no result'}")


def quick_check_path(path: str | Path) -> None:
    database = Path(path)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=60)
    try:
        connection.execute("PRAGMA busy_timeout=60000")
        quick_check_connection(connection)
    finally:
        connection.close()


def online_backup_before_migration(
    path: str | Path,
    *,
    backup_dir: str | Path,
    reason: str,
) -> Path:
    """Create and verify an online SQLite backup plus SHA-256 sidecar."""

    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_root = Path(backup_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe_reason = "".join(character if character.isalnum() or character in "-_" else "-" for character in reason)
    destination = destination_root / f"{source_path.name}.{safe_reason}.{timestamp}.{os.getpid()}.sqlite3"
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=60)
    target = sqlite3.connect(destination, timeout=60)
    try:
        source.execute("PRAGMA busy_timeout=60000")
        quick_check_connection(source)
        source.backup(target, pages=256, sleep=0.02)
        target.commit()
        quick_check_connection(target)
    except Exception:
        target.close()
        source.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
    digest = sha256_file(destination)
    atomic_write_text(
        destination.with_suffix(f"{destination.suffix}.sha256"),
        f"{digest}  {destination.name}\n",
    )
    fsync_directory(destination_root)
    return destination
