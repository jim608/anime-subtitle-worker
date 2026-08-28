from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import logging
import re

from config import AppConfig


BACKUP_RE = re.compile(r"^(?P<base>.+)\.bak-[^.]+$")


def cleanup_backup_files(config: AppConfig, logger: logging.Logger) -> int:
    if not config.cleanup_backup_files:
        return 0

    input_root = config.input_path.resolve()
    backups = [
        path
        for path in config.input_path.rglob("*")
        if path.is_file() and ".bak-" in path.name and _is_inside(path, input_root)
    ]
    if not backups:
        return 0

    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in backups:
        grouped[_backup_group_key(path)].append(path)

    deleted = 0
    for paths in grouped.values():
        paths.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for path in paths[config.backup_retention_count :]:
            path.unlink(missing_ok=True)
            deleted += 1
            logger.info("Removed backup file: %s", path)

    if deleted:
        logger.info("Removed %s backup file(s).", deleted)
    return deleted


def _backup_group_key(path: Path) -> str:
    match = BACKUP_RE.match(path.name)
    if not match:
        return str(path.with_name(path.name.split(".bak-", 1)[0]))
    return str(path.with_name(match.group("base")))


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True
