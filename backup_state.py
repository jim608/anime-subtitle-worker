from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Any, Iterable

from config import AppConfig, load_config


MANIFEST_VERSION = 1


class StateBackupError(RuntimeError):
    pass


def create_state_backup(
    config: AppConfig,
    *,
    output_dir: str | Path | None = None,
    retention_count: int | None = None,
) -> dict[str, Any]:
    root = _resolve_work_path(config, output_dir or config.state_backup_path)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".state-backup.lock"
    _acquire_state_backup_lock(lock_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    partial = root / f".{stamp}.partial"
    final = root / stamp

    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "creating",
        "entries": [],
    }
    try:
        partial.mkdir(parents=False)
        for name, source in _sqlite_sources(config):
            if not source.exists():
                manifest["entries"].append(_missing_entry(name, "sqlite", source))
                continue
            target = partial / "databases" / f"{name}.sqlite3"
            target.parent.mkdir(parents=True, exist_ok=True)
            details = _backup_sqlite(source, target)
            manifest["entries"].append({
                "name": name,
                "kind": "sqlite",
                "source": str(source),
                "backup": str(target.relative_to(partial)),
                "restorable": True,
                **details,
            })

        for name, source, restorable in _file_sources(config):
            if not source.exists() or not source.is_file():
                manifest["entries"].append(_missing_entry(name, "file", source, restorable=restorable))
                continue
            suffix = source.suffix or ".data"
            target = partial / "files" / f"{name}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if suffix.lower() == ".json":
                try:
                    _validate_json_file(target)
                except StateBackupError as exc:
                    if name in {"mikan_pending", "mikan_seen"}:
                        raise
                    target.unlink(missing_ok=True)
                    manifest["entries"].append({
                        **_missing_entry(name, "file", source, restorable=restorable),
                        "invalid": True,
                        "error": str(exc),
                    })
                    continue
            manifest["entries"].append({
                "name": name,
                "kind": "file",
                "source": str(source),
                "backup": str(target.relative_to(partial)),
                "restorable": restorable,
                "size": target.stat().st_size,
                "sha256": _sha256(target),
            })

        manifest["status"] = "complete"
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(partial / "manifest.json", manifest)
        partial.replace(final)
        verified = verify_state_backup(final)
        keep = config.state_backup_retention_count if retention_count is None else max(1, int(retention_count))
        removed = prune_state_backups(root, keep=keep, exclude={final})
        return {
            "ok": True,
            "backup": str(final),
            "entries": len(manifest["entries"]),
            "available_entries": sum(1 for item in manifest["entries"] if not item.get("missing")),
            "verified": verified["ok"],
            "removed_old_backups": removed,
        }
    except Exception:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    finally:
        lock_path.unlink(missing_ok=True)


def verify_state_backup(backup_dir: str | Path) -> dict[str, Any]:
    root = Path(backup_dir).resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateBackupError(f"Invalid backup manifest: {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != MANIFEST_VERSION:
        raise StateBackupError("Unsupported or malformed state backup manifest")
    if manifest.get("status") != "complete":
        raise StateBackupError(f"Backup is not complete: status={manifest.get('status')!r}")

    checked = 0
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict) or entry.get("missing"):
            continue
        relative = Path(str(entry.get("backup") or ""))
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise StateBackupError(f"Backup entry escapes root: {relative}") from exc
        if not target.is_file():
            raise StateBackupError(f"Backup entry is missing: {target}")
        expected_hash = str(entry.get("sha256") or "")
        actual_hash = _sha256(target)
        if expected_hash and actual_hash != expected_hash:
            raise StateBackupError(f"Checksum mismatch: {target}")
        if entry.get("kind") == "sqlite":
            check = _sqlite_quick_check(target)
            if check != ["ok"]:
                raise StateBackupError(f"SQLite quick_check failed for {target}: {check}")
        elif target.suffix.lower() == ".json":
            _validate_json_file(target)
        checked += 1
    return {"ok": True, "backup": str(root), "checked_entries": checked, "manifest": manifest}


def restore_state_backup(
    config: AppConfig,
    backup_dir: str | Path,
    *,
    apply: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    verified = verify_state_backup(backup_dir)
    manifest = verified["manifest"]
    restore_targets = {name: path.resolve() for name, path in _sqlite_sources(config)}
    restore_targets.update({name: path.resolve() for name, path, restorable in _file_sources(config) if restorable})
    restorable = [
        entry for entry in manifest.get("entries", [])
        if (
            isinstance(entry, dict)
            and not entry.get("missing")
            and entry.get("restorable", True)
            and str(entry.get("name") or "") in restore_targets
        )
    ]
    plan = [
        {"name": item.get("name"), "source": item.get("backup"), "target": str(restore_targets[str(item["name"])])}
        for item in restorable
    ]
    if not apply:
        return {"ok": True, "apply": False, "backup": str(Path(backup_dir).resolve()), "restore_plan": plan}
    if not force:
        active_reasons = _active_runtime_reasons(config)
        if active_reasons:
            raise StateBackupError("Refusing live restore while work is active: " + "; ".join(active_reasons))

    state_backup_root = _resolve_work_path(config, config.state_backup_path)
    state_backup_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_backup_root / ".state-backup.lock"
    _acquire_state_backup_lock(lock_path)
    recovery_root = state_backup_root / (
        "pre-restore-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    )
    try:
        recovery_root.mkdir(parents=True, exist_ok=False)
        restored: list[str] = []
        backup_root = Path(backup_dir).resolve()
        for entry in restorable:
            source = (backup_root / str(entry["backup"])).resolve()
            target = restore_targets[str(entry["name"])]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                current_backup = recovery_root / f"{entry['name']}{target.suffix or '.data'}"
                shutil.copy2(target, current_backup)
            temp = target.with_name(f".{target.name}.restore-{time.time_ns()}")
            shutil.copy2(source, temp)
            temp.replace(target)
            restored.append(str(target))
        return {
            "ok": True,
            "apply": True,
            "backup": str(backup_root),
            "pre_restore_backup": str(recovery_root),
            "restored": restored,
        }
    finally:
        # Pre-restore snapshots are intentionally retained if an entry fails.
        lock_path.unlink(missing_ok=True)


def prune_state_backups(root: str | Path, *, keep: int, exclude: Iterable[Path] = ()) -> list[str]:
    backup_root = Path(root).resolve()
    excluded = {item.resolve() for item in exclude}
    candidates = sorted(
        (
            item for item in backup_root.iterdir()
            if item.is_dir() and item.resolve() not in excluded and (item / "manifest.json").is_file()
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for item in candidates[max(0, int(keep) - len(excluded)):]:
        resolved = item.resolve()
        try:
            resolved.relative_to(backup_root)
        except ValueError:
            continue
        shutil.rmtree(resolved)
        removed.append(str(resolved))
    return removed


def _sqlite_sources(config: AppConfig) -> list[tuple[str, Path]]:
    pending = _resolve_work_path(config, config.mikan_pending_path)
    return [
        ("scanner_state", _resolve_work_path(config, config.scanner_state_path)),
        ("mikan_state", pending.with_name("mikan_state.sqlite3")),
        ("control_state", _resolve_work_path(config, getattr(config, "control_state_path", "control_state.sqlite3"))),
        ("series_metadata", _resolve_work_path(config, config.series_metadata_db_path)),
    ]


def _file_sources(config: AppConfig) -> list[tuple[str, Path, bool]]:
    items: list[tuple[str, Path, bool]] = [
        ("mikan_pending", _resolve_work_path(config, config.mikan_pending_path), True),
        ("mikan_seen", _resolve_work_path(config, config.mikan_seen_path), True),
        ("language_detection_cache", _resolve_work_path(config, config.language_detect_cache_path), True),
        ("metadata_context_cache", _resolve_work_path(config, config.metadata_context_cache_path), True),
        ("mikan_auto_matches", _resolve_work_path(config, config.mikan_auto_match_cache_path), True),
    ]
    if config.config_path is not None:
        items.append(("config_snapshot", Path(config.config_path).resolve(), False))
    return items


def _backup_sqlite(source: Path, target: Path) -> dict[str, Any]:
    source_conn = sqlite3.connect(str(source), timeout=60)
    target_conn = sqlite3.connect(str(target), timeout=60)
    try:
        source_conn.execute("PRAGMA busy_timeout=60000")
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.backup(target_conn, pages=512, sleep=0.05)
        target_conn.commit()
    finally:
        target_conn.close()
        source_conn.close()
    check = _sqlite_quick_check(target)
    if check != ["ok"]:
        raise StateBackupError(f"SQLite backup verification failed for {source}: {check}")
    conn = sqlite3.connect(str(target), timeout=30)
    try:
        table_count = int(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    finally:
        conn.close()
    return {
        "size": target.stat().st_size,
        "sha256": _sha256(target),
        "quick_check": check[0],
        "table_count": table_count,
        "schema_version": schema_version,
    }


def _sqlite_quick_check(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        return [str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()]
    finally:
        conn.close()


def _active_runtime_reasons(config: AppConfig) -> list[str]:
    reasons: list[str] = []
    try:
        init_command = Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except OSError:
        init_command = ""
    if os.getpid() != 1 and "main.py" in init_command:
        reasons.append("Worker PID 1 is running; stop the container before restore")
    work = Path(config.work_path)
    for name in ("mikan_worker.lock", "mikan_enqueue.lock", "mikan_extract.lock", "mikan_redownload.lock"):
        if (work / name).exists():
            reasons.append(name)
    scanner = _resolve_work_path(config, config.scanner_state_path)
    if scanner.exists():
        try:
            conn = sqlite3.connect(str(scanner), timeout=5)
            try:
                running = int(conn.execute("SELECT COUNT(*) FROM ai_candidate_queue WHERE status='running'").fetchone()[0])
            finally:
                conn.close()
            if running:
                reasons.append(f"AI running jobs={running}")
        except sqlite3.Error as exc:
            reasons.append(f"scanner state unavailable: {exc}")
    return reasons


def _resolve_work_path(config: AppConfig, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (Path(config.work_path) / path).resolve()


def _missing_entry(name: str, kind: str, source: Path, *, restorable: bool = True) -> dict[str, Any]:
    return {"name": name, "kind": kind, "source": str(source), "missing": True, "restorable": restorable}


def _validate_json_file(path: Path) -> None:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateBackupError(f"Invalid JSON state file: {path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _acquire_state_backup_lock(path: Path) -> None:
    for attempt in range(2):
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "created_at": time.time()}))
            return
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > 3600
            except OSError:
                stale = False
            if attempt == 0 and stale:
                path.unlink(missing_ok=True)
                continue
            raise StateBackupError(f"Another state backup or restore is already running: {path}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consistent backup, verification and offline restore for Worker state")
    parser.add_argument("--config", default="/app/config.yaml")
    parser.add_argument("--output-dir")
    parser.add_argument("--retention", type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", metavar="BACKUP_DIR")
    mode.add_argument("--restore", metavar="BACKUP_DIR")
    parser.add_argument("--apply", action="store_true", help="Apply --restore; restore is a dry-run without this flag")
    parser.add_argument("--force", action="store_true", help="Allow restore while runtime activity is detected")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config(args.config)
    try:
        if args.verify:
            result = verify_state_backup(args.verify)
            result.pop("manifest", None)
        elif args.restore:
            result = restore_state_backup(config, args.restore, apply=bool(args.apply), force=bool(args.force))
        else:
            result = create_state_backup(config, output_dir=args.output_dir, retention_count=args.retention)
    except (OSError, sqlite3.Error, StateBackupError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
