from __future__ import annotations

from datetime import datetime
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
import os
import sys

try:
    import fcntl
except ImportError:  # pragma: no cover - production containers are Linux.
    fcntl = None


LOGGER_NAME = "anime_subtitle_tool"
APP_LOG_MAX_BYTES = 50 * 1024 * 1024
APP_LOG_BACKUP_COUNT = 4
FAILURE_LOG_MAX_BYTES = 10 * 1024 * 1024
FAILURE_LOG_BACKUP_COUNT = 3


class MultiProcessRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that coordinates the parent and isolated AI process."""

    def __init__(self, filename: str | Path, **kwargs) -> None:
        super().__init__(filename, **kwargs)
        self._process_lock = None
        if fcntl is not None:
            self._process_lock = open(f"{self.baseFilename}.lock", "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        if self._process_lock is None:
            super().emit(record)
            return
        fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX)
        try:
            self._reopen_if_rotated_by_another_process()
            super().emit(record)
        finally:
            fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_UN)

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._process_lock is not None:
                self._process_lock.close()
                self._process_lock = None

    def _reopen_if_rotated_by_another_process(self) -> None:
        if self.stream is None:
            return
        try:
            current = os.stat(self.baseFilename)
            opened = os.fstat(self.stream.fileno())
        except OSError:
            current = None
            opened = None
        if current is not None and opened is not None and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
            return
        self.stream.close()
        self.stream = self._open()


def setup_logging(log_path: str | Path) -> logging.Logger:
    _configure_console_utf8()

    path = Path(log_path)
    path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    app_log_path = path / "app.log"
    app_log_max_bytes = _env_int("APP_LOG_MAX_BYTES", APP_LOG_MAX_BYTES, minimum=1024)
    _trim_legacy_oversized_log(app_log_path, max_bytes=app_log_max_bytes)

    file_handler = MultiProcessRotatingFileHandler(
        app_log_path,
        maxBytes=app_log_max_bytes,
        backupCount=_env_int("APP_LOG_BACKUP_COUNT", APP_LOG_BACKUP_COUNT, minimum=1),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def _configure_console_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def log_failure(log_path: str | Path, video_path: str | Path, stage: str, error: BaseException | str) -> None:
    path = Path(log_path)
    path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    message = f"{timestamp}\t{stage}\t{Path(video_path)}\t{error}\n"
    failure_path = path / "failed.log"
    _rotate_plain_log_if_needed(
        failure_path,
        incoming_bytes=len(message.encode("utf-8")),
        max_bytes=_env_int("FAILURE_LOG_MAX_BYTES", FAILURE_LOG_MAX_BYTES, minimum=1024),
        backup_count=_env_int("FAILURE_LOG_BACKUP_COUNT", FAILURE_LOG_BACKUP_COUNT, minimum=1),
    )
    with failure_path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(message)

    logging.getLogger(LOGGER_NAME).error("Failed at %s for %s: %s", stage, video_path, error)


def _rotate_plain_log_if_needed(path: Path, *, incoming_bytes: int, max_bytes: int, backup_count: int) -> None:
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        return
    except OSError:
        return
    if current_size + max(0, incoming_bytes) <= max_bytes:
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    oldest.unlink(missing_ok=True)
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def _trim_legacy_oversized_log(path: Path, *, max_bytes: int) -> None:
    """Bound a pre-rotation legacy log before opening the rotating handler."""

    try:
        size = path.stat().st_size
    except (FileNotFoundError, OSError):
        return
    if size <= max_bytes * 2:
        return

    keep_bytes = max(256, max_bytes // 2)
    marker = b"[older log content trimmed during startup]\n"
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.trim")
    try:
        with path.open("rb") as source:
            source.seek(max(0, size - keep_bytes))
            tail = source.read(keep_bytes)
        newline = tail.find(b"\n")
        if newline >= 0:
            tail = tail[newline + 1 :]
        with temp_path.open("wb") as target:
            target.write(marker)
            target.write(tail)
        os.replace(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)
