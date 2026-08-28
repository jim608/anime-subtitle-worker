"""Bounded local resource telemetry for the single-GPU Worker.

Only aggregate CPU, RAM, GPU utilization, and VRAM values are collected.  The
module never enumerates processes and never emits a PID or command line.  Both
host and GPU collection are time bounded; any missing, timed-out, malformed,
or partial source yields an unavailable sample instead of invented zeroes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import importlib
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from resource_admission import ResourceTelemetry


MIB = 1024 * 1024
NVIDIA_SMI_QUERY: tuple[str, ...] = (
    "nvidia-smi",
    "--id=0",
    "--query-gpu=utilization.gpu,memory.free,memory.total",
    "--format=csv,noheader,nounits",
)


class TelemetryUnavailable(RuntimeError):
    """Internal sentinel; exception details are not exported to diagnostics."""


class TelemetryTimeout(TelemetryUnavailable):
    pass


@dataclass(frozen=True)
class WorkerTelemetryConfig:
    gpu_timeout_seconds: float = 2.0
    host_timeout_seconds: float = 1.0
    cpu_sample_interval_seconds: float = 0.10


@dataclass(frozen=True)
class HostResourceReading:
    cpu_percent: float
    ram_available_mib: float
    ram_total_mib: float


@dataclass(frozen=True)
class GpuResourceReading:
    gpu_util_percent: float
    vram_free_mib: float
    vram_total_mib: float


@dataclass(frozen=True)
class WorkerResourceTelemetrySample:
    cpu_percent: float | None
    ram_available_mib: float | None
    ram_total_mib: float | None
    gpu_util_percent: float | None
    vram_free_mib: float | None
    vram_total_mib: float | None
    sampled_at_epoch_seconds: float
    available: bool
    error_codes: tuple[str, ...] = ()

    def to_resource_telemetry(
        self,
        *,
        now_epoch_seconds: float | None = None,
    ) -> ResourceTelemetry:
        """Convert directly to the admission module's immutable input type."""

        now = time.time() if now_epoch_seconds is None else now_epoch_seconds
        if not _is_finite_number(now) or float(now) < self.sampled_at_epoch_seconds:
            return _unavailable_admission_telemetry()
        age = float(now) - self.sampled_at_epoch_seconds
        if not self.available:
            return _unavailable_admission_telemetry(age_seconds=age)
        return ResourceTelemetry(
            cpu_percent=self.cpu_percent,
            ram_available_mib=self.ram_available_mib,
            ram_total_mib=self.ram_total_mib,
            gpu_util_percent=self.gpu_util_percent,
            vram_free_mib=self.vram_free_mib,
            vram_total_mib=self.vram_total_mib,
            age_seconds=age,
            available=True,
        )

    def to_dict(self, *, now_epoch_seconds: float | None = None) -> dict[str, Any]:
        """Return a JSON-safe payload containing aggregate metrics only."""

        payload = asdict(self)
        payload["error_codes"] = list(self.error_codes)
        converted = self.to_resource_telemetry(now_epoch_seconds=now_epoch_seconds)
        payload["age_seconds"] = converted.age_seconds
        return payload


def validate_worker_telemetry_config(config: WorkerTelemetryConfig) -> None:
    errors: list[str] = []
    for name in (
        "gpu_timeout_seconds",
        "host_timeout_seconds",
        "cpu_sample_interval_seconds",
    ):
        value = getattr(config, name, None)
        if not _is_finite_number(value) or float(value) <= 0:
            errors.append(f"{name} must be finite and positive")
    if (
        _is_finite_number(config.cpu_sample_interval_seconds)
        and _is_finite_number(config.host_timeout_seconds)
        and float(config.cpu_sample_interval_seconds) >= float(config.host_timeout_seconds)
    ):
        errors.append("cpu_sample_interval_seconds must be lower than host_timeout_seconds")
    if errors:
        raise ValueError("invalid worker telemetry config: " + "; ".join(errors))


def sample_worker_resource_telemetry(
    config: WorkerTelemetryConfig | None = None,
    *,
    clock: Callable[[], float] | None = None,
    host_sampler: Callable[[WorkerTelemetryConfig], HostResourceReading] | None = None,
    gpu_runner: Callable[[Sequence[str], float], Any] | None = None,
) -> WorkerResourceTelemetrySample:
    """Collect one bounded snapshot, returning unavailable on any source error.

    ``host_sampler`` and ``gpu_runner`` are dependency-injection seams for
    deterministic tests.  Production callers should omit them.
    """

    active_config = config or WorkerTelemetryConfig()
    validate_worker_telemetry_config(active_config)
    active_clock = clock or time.time
    sampled_at = active_clock()
    if not _is_finite_number(sampled_at):
        sampled_at = 0.0
        clock_error = ["invalid_sample_clock"]
    else:
        sampled_at = float(sampled_at)
        clock_error = []

    errors = list(clock_error)
    host: HostResourceReading | None = None
    gpu: GpuResourceReading | None = None

    try:
        sampler = host_sampler or _sample_host_resources
        host = _call_with_timeout(
            lambda: sampler(active_config),
            active_config.host_timeout_seconds,
        )
        _validate_host_reading(host)
    except TelemetryTimeout:
        errors.append("host_telemetry_timeout")
    except Exception:
        errors.append("host_telemetry_unavailable")

    try:
        runner = gpu_runner or _run_nvidia_smi
        completed = _call_with_timeout(
            lambda: runner(NVIDIA_SMI_QUERY, active_config.gpu_timeout_seconds),
            active_config.gpu_timeout_seconds,
        )
        gpu = _parse_nvidia_smi_result(completed)
    except (TelemetryTimeout, subprocess.TimeoutExpired):
        errors.append("gpu_telemetry_timeout")
    except FileNotFoundError:
        errors.append("gpu_tool_unavailable")
    except Exception:
        errors.append("gpu_telemetry_unavailable")

    if errors or host is None or gpu is None:
        return WorkerResourceTelemetrySample(
            cpu_percent=None,
            ram_available_mib=None,
            ram_total_mib=None,
            gpu_util_percent=None,
            vram_free_mib=None,
            vram_total_mib=None,
            sampled_at_epoch_seconds=sampled_at,
            available=False,
            error_codes=tuple(_dedupe(errors or ["telemetry_incomplete"])),
        )

    return WorkerResourceTelemetrySample(
        cpu_percent=float(host.cpu_percent),
        ram_available_mib=float(host.ram_available_mib),
        ram_total_mib=float(host.ram_total_mib),
        gpu_util_percent=float(gpu.gpu_util_percent),
        vram_free_mib=float(gpu.vram_free_mib),
        vram_total_mib=float(gpu.vram_total_mib),
        sampled_at_epoch_seconds=sampled_at,
        available=True,
        error_codes=(),
    )


def _run_nvidia_smi(command: Sequence[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    if tuple(command) != NVIDIA_SMI_QUERY:
        raise TelemetryUnavailable("non-fixed GPU query rejected")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(
        list(NVIDIA_SMI_QUERY),
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=float(timeout_seconds),
        creationflags=creation_flags,
    )


def _parse_nvidia_smi_result(completed: Any) -> GpuResourceReading:
    return_code = getattr(completed, "returncode", None)
    if isinstance(return_code, bool) or not isinstance(return_code, int) or return_code != 0:
        raise TelemetryUnavailable("GPU query failed")
    stdout = getattr(completed, "stdout", None)
    if not isinstance(stdout, str):
        raise TelemetryUnavailable("GPU query emitted no text")
    rows = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        raise TelemetryUnavailable("GPU query must return exactly GPU index 0")
    columns = [part.strip() for part in rows[0].split(",")]
    if len(columns) != 3:
        raise TelemetryUnavailable("GPU query column mismatch")
    try:
        utilization, free_mib, total_mib = (float(value) for value in columns)
    except ValueError as exc:
        raise TelemetryUnavailable("GPU query numeric parse failed") from exc
    reading = GpuResourceReading(utilization, free_mib, total_mib)
    _validate_gpu_reading(reading)
    return reading


def _sample_host_resources(config: WorkerTelemetryConfig) -> HostResourceReading:
    psutil_module = _load_optional_psutil()
    if psutil_module is not None:
        return _sample_host_with_psutil(
            psutil_module,
            interval_seconds=config.cpu_sample_interval_seconds,
        )
    if os.name == "nt":
        return _sample_windows_host_resources(
            interval_seconds=config.cpu_sample_interval_seconds,
        )
    if os.name == "posix" and Path("/proc/stat").is_file():
        return _sample_proc_host_resources(
            interval_seconds=config.cpu_sample_interval_seconds,
        )
    raise TelemetryUnavailable("no safe host telemetry provider")


def _load_optional_psutil() -> Any | None:
    try:
        return importlib.import_module("psutil")
    except (ImportError, ModuleNotFoundError):
        return None


def _sample_host_with_psutil(module: Any, *, interval_seconds: float) -> HostResourceReading:
    cpu = module.cpu_percent(interval=float(interval_seconds))
    memory = module.virtual_memory()
    reading = HostResourceReading(
        cpu_percent=float(cpu),
        ram_available_mib=float(memory.available) / MIB,
        ram_total_mib=float(memory.total) / MIB,
    )
    _validate_host_reading(reading)
    return reading


def _sample_windows_host_resources(
    *,
    interval_seconds: float,
    kernel32: Any | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> HostResourceReading:
    """Use Win32 APIs directly; no WMI, PowerShell, or process enumeration."""

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    api = kernel32 if kernel32 is not None else ctypes.windll.kernel32

    def system_times() -> tuple[int, int, int]:
        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not api.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise TelemetryUnavailable("GetSystemTimes failed")
        return (_filetime_value(idle), _filetime_value(kernel), _filetime_value(user))

    first_idle, first_kernel, first_user = system_times()
    sleeper(float(interval_seconds))
    second_idle, second_kernel, second_user = system_times()
    idle_delta = second_idle - first_idle
    total_delta = (second_kernel - first_kernel) + (second_user - first_user)
    if idle_delta < 0 or total_delta <= 0 or idle_delta > total_delta:
        raise TelemetryUnavailable("invalid GetSystemTimes deltas")
    cpu_percent = 100.0 * (total_delta - idle_delta) / total_delta

    memory = MEMORYSTATUSEX()
    memory.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not api.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise TelemetryUnavailable("GlobalMemoryStatusEx failed")
    reading = HostResourceReading(
        cpu_percent=cpu_percent,
        ram_available_mib=float(memory.ullAvailPhys) / MIB,
        ram_total_mib=float(memory.ullTotalPhys) / MIB,
    )
    _validate_host_reading(reading)
    return reading


def _sample_proc_host_resources(
    *,
    interval_seconds: float,
    stat_path: Path = Path("/proc/stat"),
    meminfo_path: Path = Path("/proc/meminfo"),
    sleeper: Callable[[float], None] = time.sleep,
) -> HostResourceReading:
    first = _read_proc_cpu_times(stat_path)
    sleeper(float(interval_seconds))
    second = _read_proc_cpu_times(stat_path)
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if idle_delta < 0 or total_delta <= 0 or idle_delta > total_delta:
        raise TelemetryUnavailable("invalid /proc/stat deltas")
    cpu_percent = 100.0 * (total_delta - idle_delta) / total_delta

    memory: dict[str, float] = {}
    for line in meminfo_path.read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        fields = value.strip().split()
        if not fields:
            continue
        try:
            memory[key] = float(fields[0]) / 1024.0
        except ValueError:
            continue
    reading = HostResourceReading(
        cpu_percent=cpu_percent,
        ram_available_mib=memory.get("MemAvailable", math.nan),
        ram_total_mib=memory.get("MemTotal", math.nan),
    )
    _validate_host_reading(reading)
    return reading


def _read_proc_cpu_times(path: Path) -> tuple[float, float]:
    first_line = path.read_text(encoding="ascii").splitlines()[0]
    fields = first_line.split()
    if not fields or fields[0] != "cpu" or len(fields) < 5:
        raise TelemetryUnavailable("invalid /proc/stat")
    try:
        values = [float(value) for value in fields[1:]]
    except ValueError as exc:
        raise TelemetryUnavailable("invalid /proc/stat numbers") from exc
    idle = values[3] + (values[4] if len(values) > 4 else 0.0)
    return idle, sum(values)


def _filetime_value(value: Any) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _call_with_timeout(function: Callable[[], Any], timeout_seconds: float) -> Any:
    """Run a potentially blocking provider in a daemon thread with a hard wait."""

    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put((True, function()), block=False)
        except BaseException as exc:
            try:
                result_queue.put((False, exc), block=False)
            except queue.Full:
                pass

    thread = threading.Thread(target=invoke, name="resource-telemetry-sample", daemon=True)
    thread.start()
    try:
        succeeded, value = result_queue.get(timeout=float(timeout_seconds))
    except queue.Empty as exc:
        raise TelemetryTimeout("telemetry provider timed out") from exc
    if not succeeded:
        raise value
    return value


def _validate_host_reading(reading: Any) -> None:
    if not isinstance(reading, HostResourceReading):
        raise TelemetryUnavailable("invalid host reading type")
    if not _is_percentage(reading.cpu_percent):
        raise TelemetryUnavailable("invalid CPU utilization")
    if not _is_finite_number(reading.ram_total_mib) or float(reading.ram_total_mib) <= 0:
        raise TelemetryUnavailable("invalid total RAM")
    if not _is_finite_number(reading.ram_available_mib) or not (
        0 <= float(reading.ram_available_mib) <= float(reading.ram_total_mib)
    ):
        raise TelemetryUnavailable("invalid available RAM")


def _validate_gpu_reading(reading: Any) -> None:
    if not isinstance(reading, GpuResourceReading):
        raise TelemetryUnavailable("invalid GPU reading type")
    if not _is_percentage(reading.gpu_util_percent):
        raise TelemetryUnavailable("invalid GPU utilization")
    if not _is_finite_number(reading.vram_total_mib) or float(reading.vram_total_mib) <= 0:
        raise TelemetryUnavailable("invalid total VRAM")
    if not _is_finite_number(reading.vram_free_mib) or not (
        0 <= float(reading.vram_free_mib) <= float(reading.vram_total_mib)
    ):
        raise TelemetryUnavailable("invalid free VRAM")


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_percentage(value: Any) -> bool:
    return _is_finite_number(value) and 0 <= float(value) <= 100


def _unavailable_admission_telemetry(*, age_seconds: float | None = None) -> ResourceTelemetry:
    return ResourceTelemetry(
        cpu_percent=None,
        ram_available_mib=None,
        ram_total_mib=None,
        gpu_util_percent=None,
        vram_free_mib=None,
        vram_total_mib=None,
        age_seconds=age_seconds,
        available=False,
    )


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


__all__ = [
    "GpuResourceReading",
    "HostResourceReading",
    "NVIDIA_SMI_QUERY",
    "WorkerResourceTelemetrySample",
    "WorkerTelemetryConfig",
    "sample_worker_resource_telemetry",
    "validate_worker_telemetry_config",
]
