from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time

from safe_files import fsync_directory, sha256_file, verified_copy_replace, verified_move
from subtitle_quality import QUALITY_REPORT_DIRECTORY, managed_quality_report_path


LEGACY_REPORT_SUFFIX = ".ass.quality.json"


@dataclass
class MigrationSummary:
    scanned_directories: int = 0
    matched_reports: int = 0
    migrated: int = 0
    duplicate_removed: int = 0
    quarantined: int = 0
    conflicts_archived: int = 0
    skipped_missing: int = 0
    errors: int = 0


def migrate_quality_sidecars(
    anime_root: str | Path,
    work_path: str | Path,
    *,
    container_anime_root: str | None = None,
    apply: bool = False,
    show_actions: bool = False,
    progress_interval_seconds: float = 5.0,
) -> MigrationSummary:
    root = Path(anime_root).resolve()
    work = Path(work_path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"anime root does not exist: {root}")
    if root == Path(root.anchor):
        raise ValueError(f"refusing to scan filesystem root: {root}")
    logical_root = _container_root(container_anime_root)

    summary = MigrationSummary()
    next_progress = time.monotonic() + max(0.0, progress_interval_seconds)
    scan_errors: list[OSError] = []

    def on_walk_error(error: OSError) -> None:
        scan_errors.append(error)
        summary.errors += 1
        if show_actions:
            print(f"scan_error: {error}", flush=True)

    for directory, dirnames, filenames in os.walk(root, topdown=True, onerror=on_walk_error, followlinks=False):
        dirnames.sort(key=str.casefold)
        filenames.sort(key=str.casefold)
        summary.scanned_directories += 1
        parent = Path(directory)
        for filename in filenames:
            if not filename.casefold().endswith(LEGACY_REPORT_SUFFIX):
                continue
            source = parent / filename
            summary.matched_reports += 1
            try:
                _migrate_one(
                    source,
                    work,
                    summary,
                    anime_root=root,
                    container_anime_root=logical_root,
                    apply=apply,
                    show_actions=show_actions,
                )
            except FileNotFoundError:
                # A newly deployed Worker may have migrated this exact report
                # after os.walk observed it. That race is already successful.
                summary.skipped_missing += 1
            except OSError as exc:
                summary.errors += 1
                print(f"migration_error: {source} error={exc}", flush=True)

        if progress_interval_seconds > 0 and time.monotonic() >= next_progress:
            print(
                "quality_sidecar_scan "
                f"directories={summary.scanned_directories} matched={summary.matched_reports} "
                f"migrated={summary.migrated} errors={summary.errors}",
                flush=True,
            )
            next_progress = time.monotonic() + progress_interval_seconds

    return summary


def _migrate_one(
    source: Path,
    work_path: Path,
    summary: MigrationSummary,
    *,
    anime_root: Path,
    container_anime_root: PurePosixPath | None,
    apply: bool,
    show_actions: bool,
) -> None:
    subtitle_name = source.name[: -len(".quality.json")]
    subtitle_path = source.with_name(subtitle_name)
    valid = _is_valid_quality_report(source)
    if valid:
        destination = _managed_destination(
            subtitle_path,
            work_path,
            anime_root=anime_root,
            container_anime_root=container_anime_root,
        )
        action = "migrate"
    else:
        destination = _archive_destination(work_path, source, "quarantine")
        action = "quarantine"

    if not apply:
        if show_actions:
            print(f"would_{action}: {source} -> {destination}", flush=True)
        return

    if valid:
        _install_valid_report(source, destination, work_path, summary, show_actions=show_actions)
        return

    destination = _unique_destination(destination, source)
    verified_move(source, destination)
    summary.quarantined += 1
    if show_actions:
        print(f"quarantine: {source} -> {destination}", flush=True)


def _install_valid_report(
    source: Path,
    destination: Path,
    work_path: Path,
    summary: MigrationSummary,
    *,
    show_actions: bool,
) -> None:
    if destination.is_file():
        if sha256_file(source) == sha256_file(destination):
            source.unlink()
            fsync_directory(source.parent)
            summary.duplicate_removed += 1
            if show_actions:
                print(f"remove_verified_duplicate: {source}", flush=True)
            return
        destination = _unique_archive_destination(work_path, source, "legacy_conflicts")
        verified_move(source, destination)
        summary.conflicts_archived += 1
        if show_actions:
            print(f"archive_conflict: {source} -> {destination}", flush=True)
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(str(source.absolute()).encode("utf-8", errors="replace")).hexdigest()[:16]
    staging = destination.parent / f".migrating-{token}-{os.getpid()}-{time.time_ns()}.json"
    try:
        verified_copy_replace(source, staging)
        try:
            # Hard-link publication is atomic and never replaces a report the
            # Worker may have created concurrently.
            os.link(staging, destination)
            fsync_directory(destination.parent)
        except FileExistsError:
            if sha256_file(source) == sha256_file(destination):
                source.unlink()
                fsync_directory(source.parent)
                summary.duplicate_removed += 1
                if show_actions:
                    print(f"remove_verified_duplicate: {source}", flush=True)
                return
            archive = _unique_archive_destination(work_path, source, "legacy_conflicts")
            verified_move(source, archive)
            summary.conflicts_archived += 1
            if show_actions:
                print(f"archive_conflict: {source} -> {archive}", flush=True)
            return
        source.unlink()
        fsync_directory(source.parent)
        summary.migrated += 1
        if show_actions:
            print(f"migrate: {source} -> {destination}", flush=True)
    finally:
        staging.unlink(missing_ok=True)


def _is_valid_quality_report(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict)


def _container_root(value: str | None) -> PurePosixPath | None:
    if not value:
        return None
    normalized = PurePosixPath(str(value).replace("\\", "/"))
    if not normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"container anime root must be an absolute lexical path: {value}")
    return normalized


def _managed_destination(
    subtitle_path: Path,
    work_path: Path,
    *,
    anime_root: Path,
    container_anime_root: PurePosixPath | None,
) -> Path:
    if container_anime_root is None:
        return managed_quality_report_path(subtitle_path, work_path)
    relative = subtitle_path.relative_to(anime_root)
    logical_path = container_anime_root.joinpath(*relative.parts)
    normalized = str(logical_path)
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:24]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", logical_path.name).strip("._-")[:96] or "subtitle"
    return work_path / QUALITY_REPORT_DIRECTORY / f"{safe_name}.{digest}.quality.json"


def _archive_destination(work_path: Path, source: Path, category: str) -> Path:
    digest = hashlib.sha256(str(source.absolute()).encode("utf-8", errors="replace")).hexdigest()[:24]
    return work_path / "subtitle_quality_reports" / category / f"{digest}.quality.json"


def _unique_archive_destination(work_path: Path, source: Path, category: str) -> Path:
    return _unique_destination(_archive_destination(work_path, source, category), source)


def _unique_destination(destination: Path, source: Path) -> Path:
    if not destination.exists():
        return destination
    try:
        if sha256_file(source) == sha256_file(destination):
            return destination
    except OSError:
        pass
    return destination.with_name(f"{destination.stem}-{time.time_ns()}{destination.suffix}")


def main() -> int:
    # Windows service shells commonly inherit CP950, which cannot represent
    # every simplified/traditional subtitle filename.  Logging must never
    # abort an already verified migration.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(
        description="Safely move legacy *.ass.quality.json files out of the media library.",
    )
    parser.add_argument("--root", required=True, help="Media library root, for example /anime")
    parser.add_argument("--work-path", required=True, help="Worker work directory, for example /work")
    parser.add_argument(
        "--container-anime-root",
        default=None,
        help="Logical Worker media root when --root is a host or SMB path, for example /anime",
    )
    parser.add_argument("--apply", action="store_true", help="Apply verified moves; default is dry-run")
    parser.add_argument("--show-actions", action="store_true")
    parser.add_argument("--progress-interval", type=float, default=5.0)
    args = parser.parse_args()

    summary = migrate_quality_sidecars(
        args.root,
        args.work_path,
        container_anime_root=args.container_anime_root,
        apply=bool(args.apply),
        show_actions=bool(args.show_actions),
        progress_interval_seconds=max(0.0, float(args.progress_interval)),
    )
    print(
        "quality_sidecar_migration "
        f"mode={'apply' if args.apply else 'dry-run'} "
        + " ".join(f"{key}={value}" for key, value in asdict(summary).items()),
        flush=True,
    )
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
