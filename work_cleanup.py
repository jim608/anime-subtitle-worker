from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any


TEMP_AUDIO_RETENTION_SECONDS = 24 * 60 * 60
SCANNER_CORRUPT_BACKUP_RETENTION_COUNT = 2
TEMP_AUDIO_RE = re.compile(r"\.[0-9a-f]{12}(?:\.[^/\\]+)?\.wav$", re.IGNORECASE)


@dataclass
class WorkCleanupSummary:
    scanned_audio: int = 0
    stale_audio: int = 0
    stale_corrupt_backups: int = 0
    stale_temp_files: int = 0
    removed_audio: int = 0
    removed_corrupt_backups: int = 0
    removed_temp_files: int = 0
    candidate_bytes: int = 0
    reclaimed_bytes: int = 0
    errors: int = 0


def cleanup_work_artifacts(
    config: Any,
    logger: logging.Logger,
    *,
    apply: bool = True,
    now: float | None = None,
) -> WorkCleanupSummary:
    """Remove bounded, unmistakably generated debris from the work directory."""

    root = Path(config.work_path)
    summary = WorkCleanupSummary()
    if not root.exists():
        return summary

    current_time = time.time() if now is None else float(now)
    retention_seconds = _env_int(
        "WORK_TEMP_AUDIO_RETENTION_SECONDS",
        TEMP_AUDIO_RETENTION_SECONDS,
        minimum=60 * 60,
    )
    for path in root.glob("*.wav"):
        if not path.is_file() or not TEMP_AUDIO_RE.search(path.name):
            continue
        summary.scanned_audio += 1
        try:
            age = current_time - path.stat().st_mtime
        except OSError:
            summary.errors += 1
            continue
        if age < retention_seconds:
            continue
        summary.stale_audio += 1
        _remove(path, summary, apply=apply, counter="removed_audio")

    backup_count = _env_int(
        "SCANNER_CORRUPT_BACKUP_RETENTION_COUNT",
        SCANNER_CORRUPT_BACKUP_RETENTION_COUNT,
        minimum=0,
    )
    backups = sorted(
        (path for path in root.glob("scanner_state.sqlite3.corrupt-*") if path.is_file()),
        key=_mtime_or_zero,
        reverse=True,
    )
    for path in backups[backup_count:]:
        summary.stale_corrupt_backups += 1
        _remove(path, summary, apply=apply, counter="removed_corrupt_backups")

    for path in root.glob(".mikan_fallback_sources.json.*.tmp"):
        if not path.is_file() or current_time - _mtime_or_zero(path) < retention_seconds:
            continue
        summary.stale_temp_files += 1
        _remove(path, summary, apply=apply, counter="removed_temp_files")

    if any(
        (
            summary.stale_audio,
            summary.stale_corrupt_backups,
            summary.stale_temp_files,
            summary.errors,
        )
    ):
        logger.info(
            "Work cleanup mode=%s stale_audio=%s removed_audio=%s removed_corrupt_backups=%s "
            "removed_temp_files=%s candidate_bytes=%s reclaimed_bytes=%s errors=%s",
            "apply" if apply else "dry-run",
            summary.stale_audio,
            summary.removed_audio,
            summary.removed_corrupt_backups,
            summary.removed_temp_files,
            summary.candidate_bytes,
            summary.reclaimed_bytes,
            summary.errors,
        )
    return summary


def _remove(path: Path, summary: WorkCleanupSummary, *, apply: bool, counter: str) -> None:
    try:
        size = path.stat().st_size
        summary.candidate_bytes += max(0, int(size))
        if apply:
            path.unlink(missing_ok=True)
            setattr(summary, counter, getattr(summary, counter) + 1)
            summary.reclaimed_bytes += max(0, int(size))
    except OSError:
        summary.errors += 1


def _mtime_or_zero(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean stale generated files from the worker work directory")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--apply", action="store_true", help="Delete candidates; default is dry-run")
    args = parser.parse_args()

    from config import load_config
    from logger import setup_logging

    config = load_config(args.config)
    summary = cleanup_work_artifacts(config, setup_logging(config.log_path), apply=bool(args.apply))
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **asdict(summary)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
