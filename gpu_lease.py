from __future__ import annotations

"""Crash-safe, cross-process single-GPU kernel lease.

The open kernel lock is the sole source of authority.  The adjacent JSON file
is deliberately observational: it is never consulted when deciding whether a
lease may be acquired, and stale state can therefore never steal a live lock.
The lock file is persistent and is never unlinked.
"""

from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import BinaryIO
import uuid
import weakref

from safe_files import atomic_write_text

try:  # Linux and other POSIX production hosts.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - expected on Windows.
    fcntl = None

try:  # Windows CRT byte-range locking, backed by the OS file-lock primitive.
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - expected on POSIX.
    msvcrt = None


GPU_LEASE_CONTRACT = "gpu-kernel-lease-v1"
GPU_LEASE_SCHEMA_VERSION = 1
DEFAULT_POLL_INTERVAL_SECONDS = 0.05
_MAX_GPU_IDENTITY_CHARS = 512
_MAX_PHASE_CHARS = 128


class GpuLeaseError(RuntimeError):
    """Base class for GPU lease errors."""


class GpuLeaseBusy(GpuLeaseError):
    """A context-manager acquisition could not obtain the kernel lease."""


class GpuLeasePrimitiveUnavailable(GpuLeaseError):
    """The current platform has no supported kernel file-lock primitive."""


class GpuLeasePrimitiveError(GpuLeaseError):
    """The kernel lock primitive failed for a reason other than contention."""


class GpuLeaseOwnershipError(GpuLeaseError):
    """An operation was attempted by an instance that does not own the lease."""


@dataclass
class _RegistryEntry:
    token: str
    handle: BinaryIO | None
    owner: weakref.ReferenceType[GpuKernelLease]


_REGISTRY_LOCK = threading.Lock()
_ACTIVE_LEASES: dict[str, _RegistryEntry] = {}


class GpuKernelLease:
    """One non-reentrant, process- and kernel-scoped GPU lease.

    ``timeout_seconds=0`` is a nonblocking attempt.  Positive values bound the
    wait using a monotonic clock.  Different instances in the same process are
    rejected by a process-local registry because some OS locking APIs permit
    process-reentrant byte-range locks.
    """

    def __init__(
        self,
        lock_root: str | Path,
        gpu_identity: str,
        *,
        timeout_seconds: float = 0.0,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.lock_root = Path(lock_root).resolve()
        self.gpu_identity = _normalize_gpu_identity(gpu_identity)
        identity_hash = hashlib.sha256(self.gpu_identity.encode("utf-8")).hexdigest()
        self.lock_path = self.lock_root / f"gpu-{identity_hash}.lease.lock"
        self.state_path = self.lock_root / f"gpu-{identity_hash}.lease.state.json"
        self.timeout_seconds = _finite_nonnegative(timeout_seconds, "timeout_seconds")
        self.poll_interval_seconds = _finite_positive(
            poll_interval_seconds,
            "poll_interval_seconds",
        )

        self._registry_key = os.path.normcase(os.path.abspath(str(self.lock_path)))
        self._instance_lock = threading.RLock()
        self._handle: BinaryIO | None = None
        self._token: str | None = None
        self._owner_pid: int | None = None
        self._acquired_at: float | None = None
        self._phase = ""
        self._state_error: str | None = None

    @property
    def acquired(self) -> bool:
        return (
            self._handle is not None
            and self._token is not None
            and self._owner_pid == os.getpid()
        )

    @property
    def token(self) -> str | None:
        return self._token if self.acquired else None

    @property
    def state_error(self) -> str | None:
        """Last observational-state write error, if any.

        State publication is not lock authority and cannot weaken an acquired
        kernel lease.  Callers may surface this value as a diagnostic warning.
        """

        return self._state_error

    def acquire(self, timeout_seconds: float | None = None) -> bool:
        """Acquire the lease, returning ``False`` on bounded contention."""

        timeout = self.timeout_seconds if timeout_seconds is None else _finite_nonnegative(
            timeout_seconds,
            "timeout_seconds",
        )
        with self._instance_lock:
            if self.acquired:
                return True
            if self._handle is not None or self._token is not None:
                raise GpuLeaseOwnershipError("lease instance has inconsistent ownership state")

            token = uuid.uuid4().hex
            if not _reserve_process_local(self._registry_key, token, self):
                return False

            handle: BinaryIO | None = None
            try:
                self.lock_root.mkdir(parents=True, exist_ok=True)
                handle = _open_persistent_lock_file(self.lock_path)
                _set_registry_handle(self._registry_key, token, handle)
                deadline = time.monotonic() + timeout
                while True:
                    if _try_kernel_lock(handle):
                        self._handle = handle
                        self._token = token
                        self._owner_pid = os.getpid()
                        self._acquired_at = time.time()
                        self._phase = "acquired"
                        self._publish_state("held")
                        return True

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    time.sleep(min(self.poll_interval_seconds, remaining))
            finally:
                if self._handle is None:
                    if handle is not None:
                        handle.close()
                    _drop_process_local_reservation(self._registry_key, token)

    def heartbeat(self, *, phase: str | None = None) -> bool:
        """Publish owner heartbeat metadata without affecting lock authority."""

        with self._instance_lock:
            self._require_owner()
            if phase is not None:
                self._phase = _normalize_phase(phase)
            return self._publish_state("held")

    def release(self) -> bool:
        """Release only this instance's token; nonowners are harmless no-ops."""

        with self._instance_lock:
            if self._handle is None and self._token is None:
                return False
            self._require_owner()
            handle = self._handle
            token = self._token
            if handle is None or token is None:  # Guarded by _require_owner.
                raise GpuLeaseOwnershipError("lease instance has no releasable handle")

            # Publish while the kernel lock is still held.  Publishing after
            # unlock could race and overwrite a successor owner's held state.
            self._phase = "released"
            self._publish_state("released")
            unlock_error: BaseException | None = None
            try:
                _kernel_unlock(handle)
            except BaseException as exc:  # Close still releases on supported OSes.
                unlock_error = exc
            finally:
                try:
                    handle.close()
                finally:
                    _drop_process_local_reservation(self._registry_key, token)
                    self._handle = None
                    self._token = None
                    self._owner_pid = None
                    self._acquired_at = None
            if unlock_error is not None:
                if isinstance(unlock_error, GpuLeaseError):
                    raise unlock_error
                raise GpuLeasePrimitiveError(f"could not unlock {self.lock_path}: {unlock_error}") from unlock_error
            return True

    def close(self) -> bool:
        return self.release()

    def __enter__(self) -> GpuKernelLease:
        if not self.acquire():
            raise GpuLeaseBusy(
                f"GPU lease is busy after {self.timeout_seconds:.3f}s: {self.gpu_identity}"
            )
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def _require_owner(self) -> None:
        token = self._token
        if (
            self._handle is None
            or token is None
            or self._owner_pid != os.getpid()
            or not _registry_token_matches(self._registry_key, token, self)
        ):
            raise GpuLeaseOwnershipError("GPU lease operation requires the current token owner")

    def _publish_state(self, status: str) -> bool:
        token = self._token
        if token is None:
            return False
        now = time.time()
        payload = {
            "schema_version": GPU_LEASE_SCHEMA_VERSION,
            "contract": GPU_LEASE_CONTRACT,
            "gpu_identity": self.gpu_identity,
            "lock_path": str(self.lock_path),
            "lease_token": token,
            "owner_pid": os.getpid(),
            "status": status,
            "phase": self._phase,
            "acquired_at": self._acquired_at,
            "updated_at": now,
        }
        try:
            atomic_write_text(
                self.state_path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
            )
        # The sidecar is diagnostics only.  Any ordinary publication failure
        # must be observable through state_error, never revoke or leak the
        # already-acquired kernel lease.
        except Exception as exc:
            self._state_error = f"{type(exc).__name__}: {exc}"
            return False
        self._state_error = None
        return True

    def _reset_after_fork_child(self) -> None:
        """Forget a copied lease object without unlocking the parent's lease."""

        self._handle = None
        self._token = None
        self._owner_pid = None
        self._acquired_at = None
        self._phase = ""


def read_gpu_lease_state(path: str | Path) -> dict[str, object] | None:
    """Read strict observational state; malformed/stale state is never authority."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != GPU_LEASE_SCHEMA_VERSION:
        return None
    if payload.get("contract") != GPU_LEASE_CONTRACT:
        return None
    return payload


def _reserve_process_local(key: str, token: str, owner: GpuKernelLease) -> bool:
    with _REGISTRY_LOCK:
        if key in _ACTIVE_LEASES:
            return False
        _ACTIVE_LEASES[key] = _RegistryEntry(token, None, weakref.ref(owner))
        return True


def _set_registry_handle(key: str, token: str, handle: BinaryIO) -> None:
    with _REGISTRY_LOCK:
        entry = _ACTIVE_LEASES.get(key)
        if entry is None or entry.token != token:
            raise GpuLeaseOwnershipError("process-local GPU lease reservation was lost")
        entry.handle = handle


def _drop_process_local_reservation(key: str, token: str) -> None:
    with _REGISTRY_LOCK:
        entry = _ACTIVE_LEASES.get(key)
        if entry is not None and entry.token == token:
            del _ACTIVE_LEASES[key]


def _registry_token_matches(key: str, token: str, owner: GpuKernelLease) -> bool:
    with _REGISTRY_LOCK:
        entry = _ACTIVE_LEASES.get(key)
        return entry is not None and entry.token == token and entry.owner() is owner


def _open_persistent_lock_file(path: Path) -> BinaryIO:
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.set_inheritable(descriptor, False)
        # Windows byte-range locks require a real byte.  Never truncate an
        # existing persistent lock file; concurrent initializers may safely
        # write the same sentinel at offset zero before either locks it.
        if os.fstat(descriptor).st_size < 1:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        return os.fdopen(descriptor, "r+b", buffering=0)
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise GpuLeasePrimitiveError(f"could not open persistent GPU lock {path}: {exc}") from exc


def _try_kernel_lock(handle: BinaryIO) -> bool:
    descriptor = handle.fileno()
    if os.name == "posix":
        if fcntl is None:
            raise GpuLeasePrimitiveUnavailable("fcntl.flock is unavailable on this POSIX host")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise GpuLeasePrimitiveError(f"fcntl.flock failed: {exc}") from exc

    if os.name == "nt":
        if msvcrt is None:
            raise GpuLeasePrimitiveUnavailable("msvcrt byte-range locking is unavailable on Windows")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            # Windows commonly reports lock contention as EACCES.  Unknown
            # failures must remain errors; never fall back to PID deletion.
            raise GpuLeasePrimitiveError(f"Windows byte-range lock failed: {exc}") from exc

    raise GpuLeasePrimitiveUnavailable(f"unsupported OS for GPU kernel lease: {os.name}")


def _kernel_unlock(handle: BinaryIO) -> None:
    descriptor = handle.fileno()
    if os.name == "posix":
        if fcntl is None:
            raise GpuLeasePrimitiveUnavailable("fcntl.flock is unavailable on this POSIX host")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return
        except OSError as exc:
            raise GpuLeasePrimitiveError(f"fcntl unlock failed: {exc}") from exc

    if os.name == "nt":
        if msvcrt is None:
            raise GpuLeasePrimitiveUnavailable("msvcrt byte-range locking is unavailable on Windows")
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return
        except OSError as exc:
            raise GpuLeasePrimitiveError(f"Windows byte-range unlock failed: {exc}") from exc

    raise GpuLeasePrimitiveUnavailable(f"unsupported OS for GPU kernel lease: {os.name}")


def _normalize_gpu_identity(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("gpu_identity must be a string")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > _MAX_GPU_IDENTITY_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError("gpu_identity must be nonempty, canonical, and free of control characters")
    return normalized


def _normalize_phase(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("phase must be a string")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > _MAX_PHASE_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError("phase must be nonempty, canonical, and free of control characters")
    return normalized


def _finite_nonnegative(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field} must be a finite number")
    normalized = float(value)
    if normalized < 0:
        raise ValueError(f"{field} must be nonnegative")
    return normalized


def _finite_positive(value: float, field: str) -> float:
    normalized = _finite_nonnegative(value, field)
    if normalized <= 0:
        raise ValueError(f"{field} must be positive")
    return normalized


def _after_fork_child() -> None:
    """Close copied descriptors so a grandchild cannot keep a dead owner alive."""

    global _REGISTRY_LOCK
    entries = tuple(_ACTIVE_LEASES.values())
    _ACTIVE_LEASES.clear()
    _REGISTRY_LOCK = threading.Lock()
    for entry in entries:
        if entry.handle is not None:
            try:
                # Do not call LOCK_UN: the copied descriptor shares the
                # parent's open-file description on POSIX.
                entry.handle.close()
            except OSError:
                pass
        owner = entry.owner()
        if owner is not None:
            owner._reset_after_fork_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "GPU_LEASE_CONTRACT",
    "GPU_LEASE_SCHEMA_VERSION",
    "GpuKernelLease",
    "GpuLeaseBusy",
    "GpuLeaseError",
    "GpuLeaseOwnershipError",
    "GpuLeasePrimitiveError",
    "GpuLeasePrimitiveUnavailable",
    "read_gpu_lease_state",
]
