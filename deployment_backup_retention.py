from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Iterable


BACKUP_NAME_RE = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)-\d+$")
SUCCESS_RE = re.compile(r"Stack update complete\. deployment_id=(?P<id>\d{8}T\d{6}Z-\d+)\b")
STATUS_FILE = "RETENTION_STATUS.json"
COMPLETED_STATE = "deployment_completed"
PROTECTED_STATES = {"backup_verified", "deployment_failed"}


class DeploymentBackupRetentionError(RuntimeError):
    pass


def _remove_verified_backup(path: Path, *, attempts: int = 12) -> None:
    """Remove a verified backup, retrying only the transient ENOTEMPTY race."""

    for attempt in range(1, max(1, attempts) + 1):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY or attempt >= attempts:
                raise
            # Unraid user-share directory entries can remain visible for more
            # than the sub-second window used by local filesystems. Keep this
            # bounded, but allow the verified root to settle before retrying.
            time.sleep(min(1.0, 0.1 * (2 ** (attempt - 1))))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_timestamp(path: Path) -> datetime | None:
    match = BACKUP_NAME_RE.fullmatch(path.name)
    if not match:
        return None
    return datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _safe_relative_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise DeploymentBackupRetentionError(f"Manifest path escapes backup: {relative}") from exc
    if not candidate.is_file():
        raise DeploymentBackupRetentionError(f"Manifest file is missing: {relative}")
    return candidate


def verify_sha256_manifest(backup: str | Path) -> dict[str, Any]:
    root = Path(backup).resolve()
    if _backup_timestamp(root) is None:
        raise DeploymentBackupRetentionError(f"Invalid deployment backup directory: {root}")
    manifest = root / "SHA256SUMS"
    if not manifest.is_file():
        raise DeploymentBackupRetentionError(f"SHA256SUMS is missing: {root}")
    checked = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise DeploymentBackupRetentionError(f"Invalid SHA256SUMS entry in {root}: {line[:120]}")
        expected, relative = match.groups()
        path = _safe_relative_file(root, relative)
        actual = _sha256_file(path)
        if actual != expected:
            raise DeploymentBackupRetentionError(f"Checksum mismatch: {root.name}/{relative}")
        checked += 1
    if checked == 0:
        raise DeploymentBackupRetentionError(f"SHA256SUMS is empty: {root}")
    return {"ok": True, "backup": str(root), "checked_files": checked, "manifest_sha256": _sha256_file(manifest)}


def create_sha256_manifest(backup: str | Path) -> dict[str, Any]:
    """Create the deployment checksum manifest without shell heredocs.

    Files are hashed as streams so enabling the optional AI-cache backup does
    not load a potentially large cache file into memory.  Symlinks are
    rejected because a deployment backup must never checksum data outside its
    own verified directory.
    """

    root = Path(backup).resolve()
    if _backup_timestamp(root) is None or not root.is_dir():
        raise DeploymentBackupRetentionError(f"Invalid deployment backup directory: {root}")
    manifest = root / "SHA256SUMS"
    files: list[Path] = []
    for path in root.rglob("*"):
        if path == manifest or not path.is_file():
            continue
        if path.is_symlink():
            raise DeploymentBackupRetentionError(f"Backup contains a symbolic link: {path}")
        files.append(path)
    if not files:
        raise DeploymentBackupRetentionError(f"Deployment backup contains no files: {root}")

    lines = [
        f"{_sha256_file(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix())
    ]
    temporary = manifest.with_name(f".{manifest.name}.{time.time_ns()}.tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "ok": True,
        "backup": str(root),
        "files": len(files),
        "manifest": str(manifest),
        "manifest_sha256": _sha256_file(manifest),
    }


def _read_status(backup: Path) -> dict[str, Any]:
    path = backup / STATUS_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def mark_backup_status(
    backup: str | Path,
    *,
    state: str,
    verified_by: str,
    external_sha256_verified: bool = False,
) -> dict[str, Any]:
    if state not in {COMPLETED_STATE, *PROTECTED_STATES}:
        raise DeploymentBackupRetentionError(f"Unsupported deployment backup state: {state}")
    root = Path(backup).resolve()
    timestamp = _backup_timestamp(root)
    if timestamp is None or not root.is_dir():
        raise DeploymentBackupRetentionError(f"Invalid deployment backup directory: {root}")
    manifest = root / "SHA256SUMS"
    if not manifest.is_file():
        raise DeploymentBackupRetentionError(f"SHA256SUMS is missing: {root}")
    manifest_sha = _sha256_file(manifest)
    previous = _read_status(root)
    previously_verified = previous.get("manifest_sha256") == manifest_sha
    if not external_sha256_verified and not previously_verified:
        verify_sha256_manifest(root)
    payload = {
        "schema_version": 1,
        "deployment_id": root.name,
        "state": state,
        "manifest_sha256": manifest_sha,
        "verified_by": str(verified_by),
        "updated_at": time.time(),
    }
    destination = root / STATUS_FILE
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=root, prefix=f".{STATUS_FILE}.", suffix=".tmp", delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            temporary = Path(handle.name)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return payload


def successful_deployment_ids(log_root: str | Path | None) -> set[str]:
    if not log_root:
        return set()
    root = Path(log_root)
    if not root.is_dir():
        return set()
    successful: set[str] = set()
    for path in root.glob("safe-update-stack*.log"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        successful.update(match.group("id") for match in SUCCESS_RE.finditer(text))
    return successful


def _verified_completed_backup(path: Path) -> bool:
    status = _read_status(path)
    manifest = path / "SHA256SUMS"
    return bool(
        status.get("state") == COMPLETED_STATE
        and manifest.is_file()
        and status.get("manifest_sha256") == _sha256_file(manifest)
    )


def _select_tiered_keep(
    backups: list[Path],
    *,
    newest: int,
    daily: int,
    weekly: int,
) -> set[Path]:
    ordered = sorted(backups, key=lambda path: _backup_timestamp(path) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    keep = set(ordered[: max(0, newest)])
    daily_keys: set[str] = set()
    weekly_keys: set[str] = set()
    for path in ordered:
        timestamp = _backup_timestamp(path)
        if timestamp is None:
            continue
        day_key = timestamp.strftime("%Y-%m-%d")
        if len(daily_keys) < max(0, daily) and day_key not in daily_keys:
            daily_keys.add(day_key)
            keep.add(path)
        iso_year, iso_week, _ = timestamp.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        if len(weekly_keys) < max(0, weekly) and week_key not in weekly_keys:
            weekly_keys.add(week_key)
            keep.add(path)
    return keep


def prune_deployment_backups(
    root: str | Path,
    *,
    apply: bool = False,
    exclude: Iterable[str] = (),
    success_log_root: str | Path | None = None,
    newest: int = 3,
    daily: int = 7,
    weekly: int = 4,
) -> dict[str, Any]:
    backup_root = Path(root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    excluded = {str(value) for value in exclude if str(value)}
    successful = successful_deployment_ids(success_log_root)
    candidates = [
        path
        for path in backup_root.iterdir()
        if path.is_dir() and _backup_timestamp(path) is not None
    ]
    adopted: list[str] = []
    protected: dict[str, str] = {}
    completed: list[Path] = []
    for path in candidates:
        if path.name in excluded:
            protected[path.name] = "explicitly_excluded"
            continue
        if not _verified_completed_backup(path) and path.name in successful:
            try:
                verify_sha256_manifest(path)
                mark_backup_status(
                    path,
                    state=COMPLETED_STATE,
                    verified_by="successful-deployment-log+sha256",
                    external_sha256_verified=True,
                )
                adopted.append(path.name)
            except DeploymentBackupRetentionError as exc:
                protected[path.name] = f"legacy_verification_failed:{exc}"
                continue
        if _verified_completed_backup(path):
            completed.append(path)
        else:
            state = str(_read_status(path).get("state") or "legacy_or_incomplete")
            protected[path.name] = state

    keep = _select_tiered_keep(completed, newest=newest, daily=daily, weekly=weekly)
    proposed_removals = sorted((path for path in completed if path not in keep), key=lambda path: path.name)
    removable: list[Path] = []
    for path in proposed_removals:
        try:
            # A completion marker proves that this backup was verified at
            # deployment time.  Re-check every byte immediately before a
            # destructive retention action so later damage or manual edits
            # turn the backup into a protected item instead of deleting it.
            verify_sha256_manifest(path)
        except DeploymentBackupRetentionError as exc:
            protected[path.name] = f"pre_delete_verification_failed:{exc}"
            continue
        removable.append(path)
    removed: list[str] = []
    if apply:
        for path in removable:
            resolved = path.resolve()
            try:
                resolved.relative_to(backup_root)
            except ValueError as exc:
                raise DeploymentBackupRetentionError(f"Refusing to remove path outside backup root: {resolved}") from exc
            if resolved.parent != backup_root or _backup_timestamp(resolved) is None:
                raise DeploymentBackupRetentionError(f"Refusing to remove unsafe backup path: {resolved}")
            _remove_verified_backup(resolved)
            removed.append(resolved.name)
    return {
        "ok": True,
        "apply": bool(apply),
        "root": str(backup_root),
        "policy": {"newest": newest, "daily": daily, "weekly": weekly},
        "eligible_completed": len(completed),
        "kept": sorted(path.name for path in keep),
        "planned_removals": [path.name for path in removable],
        "removed": removed,
        "protected": protected,
        "adopted_legacy": sorted(adopted),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely mark and prune verified deployment backups.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--backup", required=True)
    mark = subparsers.add_parser("mark")
    mark.add_argument("--backup", required=True)
    mark.add_argument("--state", required=True, choices=["backup_verified", COMPLETED_STATE, "deployment_failed"])
    mark.add_argument("--verified-by", default="safe-update-stack")
    mark.add_argument("--external-sha256-verified", action="store_true")
    prune = subparsers.add_parser("prune")
    prune.add_argument("--root", required=True)
    prune.add_argument("--success-log-root")
    prune.add_argument("--exclude", action="append", default=[])
    prune.add_argument("--newest", type=int, default=3)
    prune.add_argument("--daily", type=int, default=7)
    prune.add_argument("--weekly", type=int, default=4)
    prune.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "create":
        result = create_sha256_manifest(args.backup)
    elif args.command == "mark":
        result = mark_backup_status(
            args.backup,
            state=args.state,
            verified_by=args.verified_by,
            external_sha256_verified=bool(args.external_sha256_verified),
        )
    else:
        result = prune_deployment_backups(
            args.root,
            apply=bool(args.apply),
            exclude=args.exclude,
            success_log_root=args.success_log_root,
            newest=max(0, int(args.newest)),
            daily=max(0, int(args.daily)),
            weekly=max(0, int(args.weekly)),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
