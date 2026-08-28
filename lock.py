from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import ctypes
import errno
import os
import time


@dataclass
class VideoLock:
    video_path: Path
    stale_seconds: float = 43_200.0

    def __post_init__(self) -> None:
        self.lock_path = self.video_path.with_name(f"{self.video_path.name}.lock")
        self._fd: int | None = None
        self.acquired = False

    def acquire(self) -> bool:
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._is_stale_lock():
                return False
            self.lock_path.unlink(missing_ok=True)
            return self.acquire()

        try:
            payload = (
                f"pid={os.getpid()}\n"
                f"process_start={_process_start_epoch(os.getpid()) or ''}\n"
                f"created={datetime.now().isoformat(timespec='seconds')}\n"
            ).encode("utf-8")
            written = 0
            while written < len(payload):
                write_count = os.write(fd, payload[written:])
                if write_count <= 0:
                    raise OSError(errno.EIO, "Could not write lock payload")
                written += write_count
        except BaseException:
            # Until the payload is complete, this instance is not the owner.
            # Best-effort cleanup must not replace the original failure.
            try:
                os.close(fd)
            except BaseException:
                pass
            try:
                self.lock_path.unlink(missing_ok=True)
            except BaseException:
                pass
            raise

        self._fd = fd
        self.acquired = True
        return True

    def release(self) -> None:
        close_error: OSError | None = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError as exc:
                close_error = exc
            finally:
                self._fd = None
        if self.acquired:
            try:
                self.lock_path.unlink(missing_ok=True)
            finally:
                self.acquired = False
        # A descriptor may already have been closed during process-wide file
        # descriptor exhaustion.  The owned lock file must still be removed;
        # otherwise a live PID makes the orphan look active forever.
        if close_error is not None and close_error.errno != errno.EBADF:
            raise close_error

    def __enter__(self) -> "VideoLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def _is_stale_lock(self) -> bool:
        try:
            lock_stat = self.lock_path.stat()
            lock_info = _read_lock_info(self.lock_path)
        except OSError:
            return True

        pid = lock_info.get("pid")
        if pid is not None:
            if not _is_process_running(pid):
                return True

            process_start = _process_start_epoch(pid)
            lock_process_start = lock_info.get("process_start")
            if process_start is None:
                return False
            if lock_process_start is not None:
                return abs(process_start - lock_process_start) > 1.0
            return lock_stat.st_mtime + 1.0 < process_start

        # Age is only a fallback for legacy/corrupt lock files with no owner
        # identity. A valid live PID must remain authoritative even when a
        # legitimate long-running operation exceeds stale_seconds.
        return time.time() - lock_stat.st_mtime > self.stale_seconds


def _read_lock_pid(lock_path: Path) -> int | None:
    return _read_lock_info(lock_path).get("pid")


def _read_lock_info(lock_path: Path) -> dict[str, float | int]:
    info: dict[str, float | int] = {}
    try:
        for line in lock_path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key == "pid":
                info["pid"] = int(value.strip())
            elif key == "process_start" and value.strip():
                info["process_start"] = float(value.strip())
    except (OSError, ValueError):
        return {}
    return info


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_start_epoch(pid: int) -> float | None:
    if pid <= 0 or os.name == "nt":
        return None

    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        start_ticks = int(stat_fields[21])
        ticks_per_second = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return _system_boot_epoch() + (start_ticks / ticks_per_second)
    except (OSError, IndexError, KeyError, ValueError):
        return None


def _system_boot_epoch() -> float:
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    try:
        return time.time() - float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, IndexError, ValueError):
        return time.time()
