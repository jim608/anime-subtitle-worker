from __future__ import annotations

from pathlib import Path
import logging
import os
import secrets
import shutil
from typing import Any

from qbit_client import map_remote_path


class RuntimeSafetyError(RuntimeError):
    pass


def ensure_runtime_safety(config: Any, logger: logging.Logger) -> None:
    if not bool(getattr(config, "safety_check_enabled", True)):
        return

    input_path = Path(config.input_path)
    if not input_path.exists() or not input_path.is_dir():
        raise RuntimeSafetyError(f"input_path is not a readable directory: {input_path}")

    work_path = Path(config.work_path)
    log_path = Path(config.log_path)
    _ensure_writable_dir(work_path)
    _ensure_writable_dir(log_path)

    min_free_bytes = int(float(getattr(config, "disk_min_free_gb", 2.0) or 0.0) * 1024 * 1024 * 1024)
    if min_free_bytes > 0:
        _check_free_space(input_path, min_free_bytes, "input_path")
        _check_free_space(work_path, min_free_bytes, "work_path")

    completed_path: Path | None = None
    if bool(getattr(config, "completed_delivery_enabled", False)):
        completed_path = Path(str(getattr(config, "completed_delivery_path", "") or ""))
        _ensure_writable_dir(completed_path)
        _ensure_completed_atomic_publish_capability(completed_path)
        if min_free_bytes > 0:
            _check_free_space(completed_path, min_free_bytes, "completed_delivery_path")

    qbit_save_path = getattr(config, "qbit_save_path", None)
    if qbit_save_path:
        mapped = map_remote_path(qbit_save_path, getattr(config, "qbit_path_mappings", []))
        if mapped is None or not mapped.exists():
            raise RuntimeSafetyError(
                f"qBittorrent save path does not map to a mounted path visible from this container: {qbit_save_path}"
            )
        if min_free_bytes > 0:
            _check_free_space(mapped, min_free_bytes, "qbit_save_path")

    logger.info(
        "Runtime safety checks passed. input_path=%s work_path=%s completed_path=%s min_free_gb=%s",
        input_path,
        work_path,
        completed_path,
        getattr(config, "disk_min_free_gb", 2.0),
    )


def _ensure_writable_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise RuntimeSafetyError(f"not a directory: {path}")
    probe = path / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeSafetyError(f"path is not writable: {path}") from exc


def _check_free_space(path: Path, min_free_bytes: int, label: str) -> None:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        raise RuntimeSafetyError(f"cannot read disk usage for {label}: {path}") from exc
    if usage.free < min_free_bytes:
        free_gb = usage.free / 1024 / 1024 / 1024
        min_free_gb = min_free_bytes / 1024 / 1024 / 1024
        raise RuntimeSafetyError(f"{label} free space too low: {free_gb:.2f}GB < {min_free_gb:.2f}GB path={path}")


def _ensure_completed_atomic_publish_capability(path: Path) -> None:
    """Fail at startup if the completed volume cannot perform no-clobber publish."""

    token = secrets.token_hex(12)
    source = path / f".completed-delivery-preflight-{token}.tmp"
    linked = path / f".completed-delivery-preflight-{token}.link"
    descriptor: int | None = None
    try:
        descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, b"completed-delivery-preflight")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(source, linked)
        if linked.read_bytes() != b"completed-delivery-preflight":
            raise OSError("hard-link content verification failed")
    except OSError as exc:
        raise RuntimeSafetyError(
            "completed_delivery_path does not support the required atomic no-clobber hard-link publish: "
            f"{path}"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        linked.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
