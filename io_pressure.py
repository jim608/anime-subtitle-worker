from __future__ import annotations

from pathlib import Path
import re
from typing import Any


_VALUE_RE = re.compile(r"([a-zA-Z0-9]+)=([0-9.]+)")


def read_io_pressure(path: str | Path = "/proc/pressure/io") -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        kind, separator, values = line.partition(" ")
        if not separator or kind not in {"some", "full"}:
            continue
        result[kind] = {
            key: float(value)
            for key, value in _VALUE_RE.findall(values)
        }
    return result


def io_pressure_busy(config: Any, sample: dict[str, dict[str, float]] | None = None) -> bool:
    if not bool(getattr(config, "storage_io_pressure_enabled", True)):
        return False
    pressure = sample if sample is not None else read_io_pressure()
    if not pressure:
        return False
    try:
        some_threshold = max(0.0, float(getattr(config, "storage_io_pressure_some_avg10_threshold", 35.0)))
    except (TypeError, ValueError):
        some_threshold = 35.0
    try:
        full_threshold = max(0.0, float(getattr(config, "storage_io_pressure_full_avg10_threshold", 10.0)))
    except (TypeError, ValueError):
        full_threshold = 10.0
    some_value = float(pressure.get("some", {}).get("avg10", 0.0))
    full_value = float(pressure.get("full", {}).get("avg10", 0.0))
    return (some_threshold > 0 and some_value >= some_threshold) or (
        full_threshold > 0 and full_value >= full_threshold
    )
