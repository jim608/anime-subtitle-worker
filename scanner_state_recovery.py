from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
from typing import Any


DEPLOYMENT_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+")
RESTORABLE_BACKUP_STATES = frozenset({"backup_verified", "deployment_completed"})
SCANNER_DATABASE_NAME = "scanner_state.sqlite3"
RECOVERY_AUDIT_NAME = "scanner_state_recovery.json"
RECOVERY_ANCHOR_NAME = "scanner_state_recovery_anchor.json"
RECOVERY_REQUEST_NAME = "scanner_state_recovery_required.json"
DEPLOYMENT_HOLD_NAME = "deployment_hold.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return payload


def _anchored_source_deployment_id(work_root: Path) -> str:
    for name in (RECOVERY_ANCHOR_NAME, RECOVERY_AUDIT_NAME):
        path = work_root / name
        try:
            payload = _read_json_object(path)
        except (FileNotFoundError, OSError, ValueError, RuntimeError, json.JSONDecodeError):
            continue
        if name == RECOVERY_AUDIT_NAME and str(payload.get("status") or "") != "restored":
            continue
        source_deployment_id = str(payload.get("source_deployment_id") or "")
        if DEPLOYMENT_ID_RE.fullmatch(source_deployment_id):
            return source_deployment_id
    return ""


def write_recovery_anchor(work_root: Path, source_deployment_id: str) -> dict[str, Any]:
    verified = verify_scanner_state_backup(work_root, source_deployment_id)
    anchor = {
        "schema_version": 1,
        "status": "verified",
        "source_deployment_id": source_deployment_id,
        "source_sha256": verified["sha256"],
        "queue": verified["queue"],
        "verified_at": time.time(),
    }
    _atomic_write_json(work_root.resolve() / RECOVERY_ANCHOR_NAME, anchor)
    return anchor


def request_scanner_state_recovery(
    work_root: Path,
    error: BaseException,
    *,
    operation: str,
) -> dict[str, Any]:
    """Fail closed and request an out-of-process stopped-service restore."""

    work_root = work_root.resolve()
    request_path = work_root / RECOVERY_REQUEST_NAME
    try:
        existing = _read_json_object(request_path)
    except (FileNotFoundError, OSError, ValueError, RuntimeError, json.JSONDecodeError):
        existing = {}
    hold_path = work_root / DEPLOYMENT_HOLD_NAME
    try:
        hold = _read_json_object(hold_path)
    except (FileNotFoundError, OSError, ValueError, RuntimeError, json.JSONDecodeError):
        hold = {}
    if (
        str(existing.get("status") or "")
        in {"pending", "helper_started", "backup_verified", "restoring"}
        and bool(hold.get("active"))
        and str(hold.get("deployment_id") or "")
        == str(existing.get("recovery_id") or "")
        and str(hold.get("reason") or "") == "scanner-state-corruption"
    ):
        return existing

    recovery_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
    source_deployment_id = _anchored_source_deployment_id(work_root)
    if bool(hold.get("active")):
        recovery_id = str(hold.get("deployment_id") or recovery_id)
    else:
        _atomic_write_json(
            hold_path,
            {
                "active": True,
                "deployment_id": recovery_id,
                "created_at": time.time(),
                "reason": "scanner-state-corruption",
            },
        )

    request = {
        "schema_version": 1,
        "status": "pending",
        "recovery_id": recovery_id,
        "source_deployment_id": source_deployment_id,
        "operation": str(operation or "unknown"),
        "error": str(error or "scanner state corruption"),
        "requested_at": time.time(),
        "attempts": int(existing.get("attempts") or 0),
    }
    _atomic_write_json(request_path, request)
    return request


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_sha256(backup_dir: Path, relative_path: str) -> str:
    matches: list[str] = []
    for raw_line in (backup_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        fields = raw_line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        digest, name = fields
        if name.lstrip("*") == relative_path:
            matches.append(digest.casefold())
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0]):
        raise RuntimeError(f"backup manifest has no unique checksum for {relative_path}")
    return matches[0]


def _quick_check(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=60)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or str(result[0]).casefold() != "ok":
            raise RuntimeError(f"scanner database quick_check failed: {result}")
        required = {"ai_candidate_queue", "ai_job_state", "ai_delivery_obligations"}
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError(f"scanner backup is missing required tables: {','.join(missing)}")
    finally:
        connection.close()


def verify_scanner_state_backup(work_root: Path, source_deployment_id: str) -> dict[str, Any]:
    if DEPLOYMENT_ID_RE.fullmatch(str(source_deployment_id or "")) is None:
        raise ValueError("source deployment id has an invalid format")
    work_root = work_root.resolve()
    backup_dir = (work_root / "deployment_backups" / source_deployment_id).resolve()
    if not backup_dir.is_relative_to(work_root / "deployment_backups"):
        raise ValueError("scanner backup escaped the deployment backup root")
    status_path = backup_dir / "RETENTION_STATUS.json"
    manifest_path = backup_dir / "SHA256SUMS"
    source = backup_dir / "databases" / SCANNER_DATABASE_NAME
    if not status_path.is_file() or not manifest_path.is_file() or not source.is_file():
        raise FileNotFoundError("scanner restore source is not a complete deployment backup")
    if source.is_symlink():
        raise RuntimeError("scanner restore source must be a regular backup file")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if str(status.get("deployment_id") or "") != source_deployment_id:
        raise RuntimeError("scanner backup deployment identity does not match its directory")
    backup_state = str(status.get("state") or "")
    if backup_state not in RESTORABLE_BACKUP_STATES:
        raise RuntimeError(f"scanner backup state is not restorable: {backup_state or 'missing'}")
    relative_path = f"databases/{SCANNER_DATABASE_NAME}"
    expected_sha256 = _manifest_sha256(backup_dir, relative_path)
    actual_sha256 = _sha256(source)
    if actual_sha256 != expected_sha256:
        raise RuntimeError("scanner backup checksum does not match the verified manifest")
    _quick_check(source)
    connection = sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True, timeout=60)
    try:
        queue = {
            str(row[0]): int(row[1] or 0)
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM ai_candidate_queue GROUP BY status"
            )
        }
    finally:
        connection.close()
    return {
        "source_deployment_id": source_deployment_id,
        "source": str(source),
        "backup_state": backup_state,
        "bytes": source.stat().st_size,
        "sha256": actual_sha256,
        "queue": queue,
    }


def restore_scanner_state_backup(
    work_root: Path,
    source_deployment_id: str,
    *,
    hold_deployment_id: str,
) -> dict[str, Any]:
    verified = verify_scanner_state_backup(work_root, source_deployment_id)
    work_root = work_root.resolve()
    hold_path = work_root / DEPLOYMENT_HOLD_NAME
    hold = json.loads(hold_path.read_text(encoding="utf-8"))
    if not bool(hold.get("active")) or str(hold.get("deployment_id") or "") != hold_deployment_id:
        raise RuntimeError("scanner restore requires the matching active deployment hold")
    source = Path(str(verified["source"]))
    target = work_root / SCANNER_DATABASE_NAME
    temporary = work_root / f".{SCANNER_DATABASE_NAME}.restore-{hold_deployment_id}.tmp"
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        if _sha256(temporary) != str(verified["sha256"]):
            raise RuntimeError("copied scanner restore file failed checksum verification")
        _quick_check(temporary)
        for suffix in ("-wal", "-shm", "-journal"):
            (work_root / f"{SCANNER_DATABASE_NAME}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, target)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is not None:
            directory_fd = os.open(work_root, os.O_RDONLY | directory_flag)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        _quick_check(target)
    finally:
        temporary.unlink(missing_ok=True)
    restored_at = time.time()
    audit = {
        **verified,
        "status": "restored",
        "target": str(target),
        "hold_deployment_id": hold_deployment_id,
        "restored_at": restored_at,
    }
    audit_path = work_root / RECOVERY_AUDIT_NAME
    _atomic_write_json(audit_path, audit)
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify or atomically restore scanner state")
    parser.add_argument("--work-root", type=Path, default=Path("/work"))
    parser.add_argument("--source-deployment-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--write-anchor", action="store_true")
    parser.add_argument("--hold-deployment-id", default="")
    args = parser.parse_args()
    if args.apply and args.write_anchor:
        parser.error("--apply and --write-anchor are mutually exclusive")
    if args.write_anchor:
        result = write_recovery_anchor(args.work_root, args.source_deployment_id)
    elif args.apply:
        if not args.hold_deployment_id:
            parser.error("--hold-deployment-id is required with --apply")
        result = restore_scanner_state_backup(
            args.work_root,
            args.source_deployment_id,
            hold_deployment_id=args.hold_deployment_id,
        )
    else:
        result = {"status": "verified", **verify_scanner_state_backup(
            args.work_root,
            args.source_deployment_id,
        )}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
