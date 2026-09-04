from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile
import uuid


_TEST_TEMP_ROOT: Path | None = None


def configure_isolated_test_tempdir() -> None:
    """Move POSIX unittest fixtures outside directories rejected as temporary."""

    global _TEST_TEMP_ROOT
    if os.name != "posix" or _TEST_TEMP_ROOT is not None:
        return
    forbidden_markers = {"tmp", "part", "partial", "publishing", "staging"}
    for base in (Path("/dev/shm"), Path.home()):
        try:
            resolved_base = base.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved_base.is_dir() or not os.access(resolved_base, os.W_OK):
            continue
        if forbidden_markers.intersection(
            part.casefold().strip(".") for part in resolved_base.parts if part
        ):
            continue
        candidate = resolved_base / f"m2-unittest-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            candidate.mkdir(mode=0o700)
        except OSError:
            continue
        _TEST_TEMP_ROOT = candidate
        tempfile.tempdir = str(candidate)
        atexit.register(_cleanup_isolated_test_tempdir)
        return
    raise RuntimeError("no safe isolated POSIX unittest temporary root is available")


def _cleanup_isolated_test_tempdir() -> None:
    root = _TEST_TEMP_ROOT
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)
