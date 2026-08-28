from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import subprocess
from typing import Any


def file_time_metadata(path: str | Path) -> dict[str, Any]:
    """Return a truthful timestamp for one existing regular file.

    ``created`` is reported only when the filesystem exposes birth time.
    Otherwise mtime is returned as ``modified``; inode ctime is deliberately
    not presented as creation time.
    """

    candidate = Path(path)
    try:
        stat_result = candidate.stat()
    except (OSError, ValueError):
        return {}
    if not candidate.is_file():
        return {}

    created_at = float(getattr(stat_result, "st_birthtime", 0.0) or 0.0)
    if created_at <= 0 and os.name == "posix":
        created_at = _posix_birth_time(
            str(candidate),
            int(stat_result.st_mtime_ns),
            int(stat_result.st_size),
        )
    if created_at > 0:
        timestamp = created_at
        kind = "created"
    else:
        timestamp = float(stat_result.st_mtime or 0.0)
        kind = "modified"
    if timestamp <= 0:
        return {}
    return {
        "path": str(candidate),
        "timestamp": timestamp,
        "kind": kind,
        "size": int(stat_result.st_size),
    }


@lru_cache(maxsize=4096)
def _posix_birth_time(path: str, _mtime_ns: int, _size: int) -> float:
    try:
        completed = subprocess.run(
            ["stat", "-c", "%W", "--", path],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
        if completed.returncode != 0:
            return 0.0
        value = float(completed.stdout.strip() or 0)
        return value if value > 0 else 0.0
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return 0.0
