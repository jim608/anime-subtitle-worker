from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time
from typing import Any

from backup_state import create_state_backup
from config import AppConfig, load_config
from scan_state import scan_state_path


def database_health(config: AppConfig) -> dict[str, Any]:
    return {
        "databases": [_database_stats(name, path) for name, path in _database_paths(config)],
        "busy_reasons": _runtime_busy_reasons(config),
    }


def optimize_databases(
    config: AppConfig,
    *,
    apply: bool,
    wait_seconds: int = 0,
    min_reclaim_mib: float = 16.0,
    min_freelist_ratio: float = 0.20,
    online_only: bool = False,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0, int(wait_seconds))
    reasons = _runtime_busy_reasons(config)
    while apply and reasons and time.monotonic() < deadline:
        print(f"database_maintenance_wait busy={','.join(reasons)}", flush=True)
        time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        reasons = _runtime_busy_reasons(config)

    result: dict[str, Any] = {
        "apply": bool(apply),
        "mode": "online" if online_only else "vacuum",
        "busy_reasons": reasons,
        "before": [],
        "optimized": [],
        "skipped": [],
    }
    if reasons:
        result["status"] = "busy"
        return result

    before = [_database_stats(name, path) for name, path in _database_paths(config)]
    result["before"] = before
    if not apply:
        return result

    if online_only:
        for item in before:
            if not item.get("exists") or item.get("error"):
                result["skipped"].append({
                    "name": item["name"],
                    "reason": "missing_or_unreadable",
                })
                continue
            name = str(item["name"])
            path = Path(str(item["path"]))
            print(f"database_online_optimize_start name={name}", flush=True)
            _optimize_database_online(path)
            after = _database_stats(name, path)
            result["optimized"].append({"before": item, "after": after})
            print(f"database_online_optimize_complete name={name}", flush=True)
        result["status"] = "complete"
        return result

    candidates = [
        item
        for item in before
        if item.get("exists")
        and float(item.get("reclaim_mib") or 0) >= float(min_reclaim_mib)
        and float(item.get("freelist_ratio") or 0) >= float(min_freelist_ratio)
    ]
    if not candidates:
        result["status"] = "not_needed"
        return result

    backup = create_state_backup(config)
    result["backup"] = {
        "path": backup.get("backup") or backup.get("path") or backup.get("backup_dir"),
        "status": "complete" if backup.get("ok") else backup.get("status"),
        "verified": bool(backup.get("verified")),
    }
    for item in before:
        if item not in candidates:
            result["skipped"].append({"name": item["name"], "reason": "below_threshold"})
            continue
        name = str(item["name"])
        path = Path(str(item["path"]))
        print(
            f"database_optimize_start name={name} size_mib={item['size_mib']} reclaim_mib={item['reclaim_mib']}",
            flush=True,
        )
        _vacuum_database(path)
        after = _database_stats(name, path)
        result["optimized"].append({"before": item, "after": after})
        print(
            f"database_optimize_complete name={name} size_mib={after['size_mib']} reclaimed_mib={round(float(item['size_mib']) - float(after['size_mib']), 2)}",
            flush=True,
        )
    result["status"] = "complete"
    return result


def _database_paths(config: AppConfig) -> list[tuple[str, Path]]:
    work = Path(config.work_path)
    pending = _resolve_work_path(work, config.mikan_pending_path)
    return [
        ("scanner_state", scan_state_path(config)),
        ("mikan_state", pending.with_name("mikan_state.sqlite3")),
        ("control_state", _resolve_work_path(work, getattr(config, "control_state_path", "control_state.sqlite3"))),
        ("series_metadata", _resolve_work_path(work, config.series_metadata_db_path)),
    ]


def _resolve_work_path(work: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else work / path


def _database_stats(name: str, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"name": name, "path": str(path), "exists": False}
    size_mib = path.stat().st_size / 1048576.0
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error as exc:
        return {
            "name": name,
            "path": str(path),
            "exists": True,
            "size_mib": round(size_mib, 2),
            "error": str(exc),
        }
    finally:
        if conn is not None:
            conn.close()
    reclaim_mib = freelist * page_size / 1048576.0
    return {
        "name": name,
        "path": str(path),
        "exists": True,
        "size_mib": round(size_mib, 2),
        "page_count": page_count,
        "freelist_pages": freelist,
        "freelist_ratio": round(freelist / page_count, 4) if page_count else 0.0,
        "reclaim_mib": round(reclaim_mib, 2),
        "quick_check": quick_check,
    }


def _runtime_busy_reasons(config: AppConfig) -> list[str]:
    reasons: list[str] = []
    scanner = scan_state_path(config)
    if scanner.exists():
        try:
            conn = sqlite3.connect(f"file:{scanner.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                if _table_exists(conn, "ai_candidate_queue"):
                    count = int(conn.execute("SELECT COUNT(*) FROM ai_candidate_queue WHERE status = 'running'").fetchone()[0])
                    if count:
                        reasons.append(f"ai_running:{count}")
            finally:
                conn.close()
        except sqlite3.Error:
            reasons.append("scanner_database_busy")

    mikan = _resolve_work_path(Path(config.work_path), config.mikan_pending_path).with_name("mikan_state.sqlite3")
    if mikan.exists():
        try:
            conn = sqlite3.connect(f"file:{mikan.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                if _table_exists(conn, "mikan_extract_jobs"):
                    count = int(conn.execute("SELECT COUNT(*) FROM mikan_extract_jobs WHERE status = 'running'").fetchone()[0])
                    if count:
                        reasons.append(f"subtitle_extract_running:{count}")
                if _table_exists(conn, "mikan_jobs"):
                    count = int(conn.execute("SELECT COUNT(*) FROM mikan_jobs WHERE status = 'running'").fetchone()[0])
                    if count:
                        reasons.append(f"mikan_job_running:{count}")
            finally:
                conn.close()
        except sqlite3.Error:
            reasons.append("mikan_database_busy")
    return reasons


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone() is not None


def _vacuum_database(path: Path) -> None:
    conn = sqlite3.connect(path, timeout=300)
    try:
        conn.execute("PRAGMA busy_timeout=300000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.execute("VACUUM")
        conn.execute("PRAGMA optimize").fetchall()
        check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if check != "ok":
            raise RuntimeError(f"Database quick_check failed after VACUUM: {path}: {check}")
    finally:
        conn.close()


def _optimize_database_online(path: Path) -> None:
    """Run only connection-safe maintenance while the Worker is live.

    A full VACUUM or truncating checkpoint may rotate SQLite sidecar files.
    On Unraid's FUSE-backed appdata path that can strand another process on a
    hidden WAL/SHM inode.  Scheduled maintenance therefore limits itself to
    SQLite's online query-planner optimization and a quick integrity check.
    Explicit offline maintenance can still use the full VACUUM path.
    """

    conn = sqlite3.connect(path, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA optimize").fetchall()
        check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if check != "ok":
            raise RuntimeError(f"Database quick_check failed after online optimize: {path}: {check}")
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or safely compact Worker SQLite state databases")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--min-reclaim-mib", type=float, default=16.0)
    parser.add_argument("--min-freelist-ratio", type=float, default=0.20)
    parser.add_argument(
        "--online-only",
        action="store_true",
        help="avoid VACUUM/checkpoint sidecar rotation; safe for a running Worker",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    result = optimize_databases(
        config,
        apply=bool(args.apply),
        wait_seconds=max(0, int(args.wait_seconds)),
        min_reclaim_mib=max(0.0, float(args.min_reclaim_mib)),
        min_freelist_ratio=max(0.0, min(1.0, float(args.min_freelist_ratio))),
        online_only=bool(args.online_only),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 2 if result.get("status") == "busy" else 0


if __name__ == "__main__":
    raise SystemExit(main())
