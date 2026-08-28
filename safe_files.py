from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import shutil
import threading
import time
import uuid


class VerifiedMoveError(OSError):
    """Raised when a cross-filesystem move cannot be verified safely."""


_ATOMIC_WRITE_LOCKS = tuple(threading.Lock() for _index in range(64))


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: str | Path) -> None:
    """Best-effort directory fsync used after publishing or removing a file."""

    directory = Path(path)
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def verified_move(source: str | Path, destination: str | Path) -> Path:
    """Move a file without ever deleting an unverified cross-device source.

    A normal atomic replace is used on one filesystem.  EXDEV falls back to a
    copy into the destination directory, fsync, SHA-256 verification, atomic
    publish, and only then source removal.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_path.replace(destination_path)
        fsync_directory(destination_path.parent)
        return destination_path
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise

    verified_copy_replace(source_path, destination_path)
    try:
        source_path.unlink()
        fsync_directory(source_path.parent)
        return destination_path
    except Exception as exc:
        # The destination is already a verified complete copy.  Never remove it
        # merely because source cleanup failed; surface the incomplete move so
        # the caller can reconcile the duplicate safely.
        if isinstance(exc, VerifiedMoveError):
            raise
        raise VerifiedMoveError(
            f"Verified copy completed but source removal failed: {source_path} -> {destination_path}: {exc}"
        ) from exc


def verified_copy_replace(source: str | Path, destination: str | Path) -> Path:
    """Copy, fsync and hash-verify before atomically replacing destination.

    The temporary component is intentionally independent of the destination
    basename.  Appending tokens to a near-255-byte subtitle name recreates the
    exact ENAMETOOLONG failure this helper is meant to prevent.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _short_temporary_sibling(destination_path, kind="copying", suffix=".copying")
    temporary.unlink(missing_ok=True)
    try:
        source_digest = sha256_file(source_path)
        with source_path.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        try:
            shutil.copystat(source_path, temporary, follow_symlinks=False)
        except OSError:
            pass
        copied_digest = sha256_file(temporary)
        if copied_digest != source_digest:
            raise VerifiedMoveError(
                f"SHA-256 verification failed while copying {source_path} to {destination_path}"
            )
        temporary.replace(destination_path)
        fsync_directory(destination_path.parent)
        return destination_path
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_bytes(path: str | Path, content: bytes) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_key = os.path.normcase(os.path.abspath(str(destination)))
    lock = _ATOMIC_WRITE_LOCKS[hash(lock_key) % len(_ATOMIC_WRITE_LOCKS)]
    with lock:
        temporary = _short_temporary_sibling(destination, kind="write", suffix=".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_permission_retry(temporary, destination)
            fsync_directory(destination.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return destination


def _replace_with_permission_retry(source: Path, destination: Path, *, attempts: int = 6) -> None:
    """Handle short Windows sharing violations without weakening atomic replace."""

    for attempt in range(max(1, attempts)):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if attempt + 1 >= max(1, attempts):
                raise
            time.sleep(0.01 * (2**attempt))


def _short_temporary_sibling(destination: Path, *, kind: str, suffix: str) -> Path:
    digest = hashlib.sha256(destination.name.encode("utf-8", errors="replace")).hexdigest()[:16]
    return destination.parent / f".{kind}-{digest}-{os.getpid()}-{uuid.uuid4().hex}{suffix}"


def atomic_write_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, content.encode(encoding))
