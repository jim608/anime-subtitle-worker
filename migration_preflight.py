from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

from control_state import initialize_control_state
from mikan_worker import _ensure_mikan_state_tables
from scan_state import ScanStateStore
from series_metadata import SeriesMetadataStore
from sqlite_safety import quick_check_path


DATABASE_NAMES = (
    "scanner_state.sqlite3",
    "mikan_state.sqlite3",
    "control_state.sqlite3",
    "series_metadata.sqlite3",
)


def rehearse_database_backups(backup_dir: Path) -> dict[str, object]:
    backup_root = Path(backup_dir).resolve()
    database_root = backup_root / "databases"
    rehearsal_root = backup_root / "migration_rehearsal"
    rehearsal_root.mkdir(parents=True, exist_ok=False)
    results: dict[str, dict[str, object]] = {}
    for name in DATABASE_NAMES:
        source = database_root / name
        if not source.is_file():
            results[name] = {"status": "missing"}
            continue
        target = rehearsal_root / name
        shutil.copy2(source, target)
        quick_check_path(target)
        if name == "scanner_state.sqlite3":
            store = ScanStateStore(target)
            store.close()
        elif name == "control_state.sqlite3":
            initialize_control_state(
                SimpleNamespace(work_path=rehearsal_root, control_state_path=target)
            )
        elif name == "series_metadata.sqlite3":
            store = SeriesMetadataStore(target)
            store.close()
        else:
            connection = sqlite3.connect(target, timeout=60)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                _ensure_mikan_state_tables(connection)
                connection.commit()
            finally:
                connection.close()
        quick_check_path(target)
        results[name] = {
            "status": "ok",
            "source": str(source),
            "rehearsal": str(target),
            "bytes": target.stat().st_size,
        }
    return {
        "status": "ok",
        "backup_dir": str(backup_root),
        "rehearsal_dir": str(rehearsal_root),
        "databases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rehearse additive SQLite migrations on deployment backups")
    parser.add_argument("--backup-dir", required=True)
    args = parser.parse_args()
    try:
        result = rehearse_database_backups(Path(args.backup_dir))
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
