"""Deterministic, fail-closed resource admission for the single-GPU Worker.

This module intentionally has no process, CUDA, or telemetry dependencies.  A
caller supplies one telemetry snapshot and carries the returned hysteresis
state into the next call.  The result is therefore reproducible and safe to
serialize in job diagnostics.

The policy does not claim multi-lane GPU capacity: ``concurrency_limit`` is
always one.  Red or unavailable decisions still permit at most one job that is
already running to drain to its next safe checkpoint; they never admit a new
GPU job.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Sequence


TIERS = ("green", "yellow", "red", "unavailable")
_TIER_SEVERITY = {tier: index for index, tier in enumerate(TIERS)}

GPU_JOB_STAGES = frozenset(
    {
        "language_detect",
        "vocal_separation",
        "transcription",
        "source_transcription",
        "asr",
    }
)
CPU_JOB_STAGES = frozenset(
    {
        "preflight",
        "audio_selection",
        "audio",
        "subtitle_extract",
        "translation",
        "quality_control",
        "publication",
        "mux",
        "move_completed",
    }
)
KNOWN_JOB_STAGES = GPU_JOB_STAGES | CPU_JOB_STAGES


@dataclass(frozen=True)
class ResourceTelemetry:
    """One caller-timestamped resource sample.

    MiB is used for memory values so the policy never guesses whether an input
    was bytes, MB, or GiB.  If ``available`` is false, numeric fields may be
    ``None`` and are never treated as zero.
    """

    cpu_percent: float | None
    ram_available_mib: float | None
    ram_total_mib: float | None
    gpu_util_percent: float | None
    vram_free_mib: float | None
    vram_total_mib: float | None
    age_seconds: float | None
    available: bool = True


@dataclass(frozen=True)
class ModelMemoryProfile:
    """An ASR route and its measured worst-case VRAM requirement."""

    model: str
    compute_type: str
    required_vram_mib: float


@dataclass(frozen=True)
class ResourceAdmissionConfig:
    """Validated thresholds for one single-GPU host."""

    telemetry_stale_after_seconds: float = 15.0
    cpu_yellow_percent: float = 80.0
    cpu_red_percent: float = 95.0
    ram_yellow_available_ratio: float = 0.20
    ram_red_available_ratio: float = 0.08
    gpu_yellow_percent: float = 85.0
    gpu_red_percent: float = 98.0
    vram_reserve_mib: float = 1024.0
    recovery_samples: int = 3
    yellow_retry_after_seconds: float = 30.0
    red_retry_after_seconds: float = 120.0
    unavailable_retry_after_seconds: float = 60.0
    gpu_concurrency_limit: int = 1


@dataclass(frozen=True)
class AdmissionHysteresisState:
    """Small persistent state carried by the caller between samples."""

    effective_tier: str
    candidate_tier: str | None = None
    consecutive_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResourceAdmissionDecision:
    tier: str
    allow_new_job: bool
    allow_running_job: bool
    asr_compute_type: str | None
    asr_model: str | None
    concurrency_limit: int
    retry_after: float
    reason_codes: tuple[str, ...]
    hysteresis_state: AdmissionHysteresisState
    diagnostics: dict[str, Any]

    @property
    def retry_after_seconds(self) -> float:
        """Compatibility/readability alias for callers that name the unit."""

        return self.retry_after

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe diagnostic payload without enum/custom objects."""

        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def validate_resource_admission_config(config: ResourceAdmissionConfig) -> None:
    """Raise ``ValueError`` for unsafe or internally inconsistent policy."""

    errors: list[str] = []

    def finite_positive(name: str, value: Any, *, allow_zero: bool = False) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{name} must be numeric")
            return
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0 or (numeric == 0 and not allow_zero):
            comparator = "non-negative" if allow_zero else "positive"
            errors.append(f"{name} must be finite and {comparator}")

    finite_positive("telemetry_stale_after_seconds", config.telemetry_stale_after_seconds)
    for name in (
        "cpu_yellow_percent",
        "cpu_red_percent",
        "gpu_yellow_percent",
        "gpu_red_percent",
    ):
        finite_positive(name, getattr(config, name))
        value = getattr(config, name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            if float(value) > 100:
                errors.append(f"{name} must be <= 100")
    if _is_finite_number(config.cpu_yellow_percent) and _is_finite_number(config.cpu_red_percent) and (
        float(config.cpu_yellow_percent) >= float(config.cpu_red_percent)
    ):
        errors.append("cpu_yellow_percent must be lower than cpu_red_percent")
    if _is_finite_number(config.gpu_yellow_percent) and _is_finite_number(config.gpu_red_percent) and (
        float(config.gpu_yellow_percent) >= float(config.gpu_red_percent)
    ):
        errors.append("gpu_yellow_percent must be lower than gpu_red_percent")

    for name in ("ram_yellow_available_ratio", "ram_red_available_ratio"):
        value = getattr(config, name)
        finite_positive(name, value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            if float(value) > 1:
                errors.append(f"{name} must be <= 1")
    if _is_finite_number(config.ram_red_available_ratio) and _is_finite_number(
        config.ram_yellow_available_ratio
    ) and float(config.ram_red_available_ratio) >= float(config.ram_yellow_available_ratio):
        errors.append("ram_red_available_ratio must be lower than ram_yellow_available_ratio")

    finite_positive("vram_reserve_mib", config.vram_reserve_mib, allow_zero=True)
    for name in (
        "yellow_retry_after_seconds",
        "red_retry_after_seconds",
        "unavailable_retry_after_seconds",
    ):
        finite_positive(name, getattr(config, name), allow_zero=True)

    if isinstance(config.recovery_samples, bool) or not isinstance(config.recovery_samples, int):
        errors.append("recovery_samples must be an integer")
    elif config.recovery_samples < 1:
        errors.append("recovery_samples must be >= 1")
    if config.gpu_concurrency_limit != 1:
        errors.append("gpu_concurrency_limit must be exactly 1 on this single-GPU Worker")

    if errors:
        raise ValueError("invalid resource admission config: " + "; ".join(errors))


def resource_admission_config_from_mapping(
    values: Mapping[str, Any],
) -> ResourceAdmissionConfig:
    """Build and validate a config from a plain mapping.

    Unknown keys are rejected by the dataclass constructor instead of being
    silently ignored.
    """

    try:
        config = ResourceAdmissionConfig(**dict(values))
    except TypeError as exc:
        raise ValueError(f"invalid resource admission config: {exc}") from exc
    validate_resource_admission_config(config)
    return config


def decide_resource_admission(
    telemetry: ResourceTelemetry,
    *,
    job_stage: str,
    primary_model: ModelMemoryProfile | None = None,
    fallback_models: Sequence[ModelMemoryProfile] = (),
    running_gpu_jobs: int = 0,
    recent_oom: bool = False,
    previous_state: AdmissionHysteresisState | None = None,
    config: ResourceAdmissionConfig | None = None,
) -> ResourceAdmissionDecision:
    """Return one deterministic, fail-closed admission and route decision.

    ``previous_state`` is optional for the first sample.  Resource deterioration
    applies immediately.  Recovery to a less restrictive tier requires
    ``recovery_samples`` consecutive observations of the same raw tier.
    """

    policy = config or ResourceAdmissionConfig()
    validate_resource_admission_config(policy)
    stage = str(job_stage or "").strip().casefold()
    reasons: list[str] = []
    telemetry_is_object = isinstance(telemetry, ResourceTelemetry)
    sample = telemetry if telemetry_is_object else ResourceTelemetry(
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        available=True,
    )
    metric_errors = _telemetry_error_codes(telemetry)
    profile_errors = _profile_error_codes(primary_model, fallback_models, stage)
    input_errors = metric_errors + profile_errors

    if stage not in KNOWN_JOB_STAGES:
        input_errors.append("invalid_job_stage")
    if isinstance(running_gpu_jobs, bool) or not isinstance(running_gpu_jobs, int) or running_gpu_jobs < 0:
        input_errors.append("invalid_running_gpu_jobs")
        normalized_running = 0
    else:
        normalized_running = running_gpu_jobs
    if not isinstance(recent_oom, bool):
        input_errors.append("invalid_recent_oom")
        recent_oom_flag = False
    else:
        recent_oom_flag = recent_oom

    telemetry_unavailable = telemetry_is_object and sample.available is False
    stale = False
    if not telemetry_unavailable and not metric_errors:
        stale = float(sample.age_seconds) > policy.telemetry_stale_after_seconds

    selected: ModelMemoryProfile | None = None
    fallback_selected = False
    pressure_blocks_new = False

    if telemetry_unavailable:
        raw_tier = "unavailable"
        reasons.append("telemetry_unavailable")
    elif input_errors:
        raw_tier = "unavailable"
        reasons.extend(input_errors)
    elif stale:
        raw_tier = "unavailable"
        reasons.append("telemetry_stale")
    else:
        cpu = float(sample.cpu_percent)
        ram_ratio = float(sample.ram_available_mib) / float(sample.ram_total_mib)
        gpu_util = float(sample.gpu_util_percent)
        critical = False
        warning = False

        if cpu >= policy.cpu_red_percent:
            critical = True
            pressure_blocks_new = True
            reasons.append("cpu_critical")
        elif cpu >= policy.cpu_yellow_percent:
            warning = True
            pressure_blocks_new = True
            reasons.append("cpu_pressure")

        if ram_ratio <= policy.ram_red_available_ratio:
            critical = True
            pressure_blocks_new = True
            reasons.append("ram_critical")
        elif ram_ratio <= policy.ram_yellow_available_ratio:
            warning = True
            pressure_blocks_new = True
            reasons.append("ram_pressure")

        if stage in GPU_JOB_STAGES:
            if gpu_util >= policy.gpu_red_percent:
                critical = True
                pressure_blocks_new = True
                reasons.append("gpu_saturated")
            elif gpu_util >= policy.gpu_yellow_percent:
                warning = True
                pressure_blocks_new = True
                reasons.append("gpu_busy")

            selected, fallback_selected, route_reasons = _select_model_route(
                primary_model,
                fallback_models,
                free_vram_mib=float(sample.vram_free_mib),
                reserve_mib=policy.vram_reserve_mib,
                recent_oom=recent_oom_flag,
            )
            reasons.extend(route_reasons)
            if selected is None:
                critical = True
                pressure_blocks_new = True
            elif fallback_selected:
                warning = True

        if critical:
            raw_tier = "red"
        elif warning:
            raw_tier = "yellow"
        else:
            raw_tier = "green"

    effective_tier, next_state, recovery_pending = _apply_hysteresis(
        raw_tier,
        previous_state,
        policy.recovery_samples,
    )
    if recovery_pending:
        reasons.append("hysteresis_recovery_pending")

    if normalized_running > policy.gpu_concurrency_limit:
        reasons.append("gpu_concurrency_policy_violation")
    elif normalized_running == policy.gpu_concurrency_limit:
        reasons.append("gpu_single_lane_busy")

    route_available = stage not in GPU_JOB_STAGES or selected is not None
    tier_permits_new = effective_tier in {"green", "yellow"}
    lane_available = normalized_running < policy.gpu_concurrency_limit
    allow_new = bool(
        route_available
        and tier_permits_new
        and not pressure_blocks_new
        and lane_available
        and not telemetry_unavailable
        and not stale
        and not input_errors
    )

    # Existing work is never killed merely because telemetry became stale or
    # the host crossed a pressure threshold.  Exactly one running GPU job may
    # drain; a policy-violating count is reported rather than normalized away.
    allow_running = normalized_running == 1
    if allow_running and not allow_new:
        reasons.append("running_job_drain_only")

    retry_after = _retry_after_seconds(effective_tier, policy)
    if allow_new:
        retry_after = 0.0
        reasons.append("admission_allowed")
    else:
        reasons.append("new_job_deferred")

    diagnostics = {
        "policy": "single_gpu_fail_closed_v1",
        "job_stage": stage,
        "gpu_stage": stage in GPU_JOB_STAGES,
        "raw_tier": raw_tier,
        "effective_tier": effective_tier,
        "telemetry": {
            "available": sample.available if isinstance(sample.available, bool) else None,
            "cpu_percent": _json_number(sample.cpu_percent),
            "ram_available_mib": _json_number(sample.ram_available_mib),
            "ram_total_mib": _json_number(sample.ram_total_mib),
            "gpu_util_percent": _json_number(sample.gpu_util_percent),
            "vram_free_mib": _json_number(sample.vram_free_mib),
            "vram_total_mib": _json_number(sample.vram_total_mib),
        },
        "telemetry_age_seconds": _json_number(sample.age_seconds),
        "telemetry_stale_after_seconds": policy.telemetry_stale_after_seconds,
        "ram_available_ratio": _safe_ratio(
            sample.ram_available_mib,
            sample.ram_total_mib,
        ),
        "vram_headroom_mib": _safe_difference(
            sample.vram_free_mib,
            policy.vram_reserve_mib,
        ),
        "primary_required_vram_mib": (
            float(primary_model.required_vram_mib) if primary_model is not None and not profile_errors else None
        ),
        "selected_required_vram_mib": (
            float(selected.required_vram_mib) if selected is not None else None
        ),
        "fallback_selected": fallback_selected,
        "running_gpu_jobs": normalized_running,
        "running_jobs_allowed_to_drain": min(normalized_running, 1),
        "recovery_samples_required": policy.recovery_samples,
    }

    return ResourceAdmissionDecision(
        tier=effective_tier,
        allow_new_job=allow_new,
        allow_running_job=allow_running,
        asr_compute_type=selected.compute_type if selected is not None else None,
        asr_model=selected.model if selected is not None else None,
        concurrency_limit=policy.gpu_concurrency_limit,
        retry_after=retry_after,
        reason_codes=tuple(_dedupe(reasons)),
        hysteresis_state=next_state,
        diagnostics=diagnostics,
    )


def _telemetry_error_codes(telemetry: Any) -> list[str]:
    if not isinstance(telemetry, ResourceTelemetry):
        return ["invalid_telemetry_object"]
    if not isinstance(telemetry.available, bool):
        return ["invalid_telemetry_available"]
    if telemetry.available is False:
        return []
    errors: list[str] = []
    _validate_percentage(telemetry.cpu_percent, "cpu_percent", errors)
    _validate_percentage(telemetry.gpu_util_percent, "gpu_util_percent", errors)
    _validate_finite_nonnegative(telemetry.age_seconds, "telemetry_age", errors)
    _validate_finite_positive(telemetry.ram_total_mib, "ram_total_mib", errors)
    _validate_finite_nonnegative(telemetry.ram_available_mib, "ram_available_mib", errors)
    _validate_finite_positive(telemetry.vram_total_mib, "vram_total_mib", errors)
    _validate_finite_nonnegative(telemetry.vram_free_mib, "vram_free_mib", errors)
    if (
        _is_finite_number(telemetry.ram_available_mib)
        and _is_finite_number(telemetry.ram_total_mib)
        and float(telemetry.ram_available_mib) > float(telemetry.ram_total_mib)
    ):
        errors.append("invalid_ram_available_mib")
    if (
        _is_finite_number(telemetry.vram_free_mib)
        and _is_finite_number(telemetry.vram_total_mib)
        and float(telemetry.vram_free_mib) > float(telemetry.vram_total_mib)
    ):
        errors.append("invalid_vram_free_mib")
    return errors


def _profile_error_codes(
    primary: ModelMemoryProfile | None,
    fallbacks: Sequence[ModelMemoryProfile],
    stage: str,
) -> list[str]:
    if stage not in GPU_JOB_STAGES:
        return []
    errors: list[str] = []
    if not isinstance(primary, ModelMemoryProfile):
        return ["invalid_primary_model_profile"]
    profiles: list[Any] = [primary]
    if isinstance(fallbacks, (str, bytes)) or not isinstance(fallbacks, Sequence):
        return ["invalid_fallback_model_profiles"]
    profiles.extend(fallbacks)
    seen: set[tuple[str, str]] = set()
    for index, profile in enumerate(profiles):
        label = "primary" if index == 0 else f"fallback_{index}"
        if not isinstance(profile, ModelMemoryProfile):
            errors.append(f"invalid_{label}_model_profile")
            continue
        if not isinstance(profile.model, str) or not profile.model.strip():
            errors.append(f"invalid_{label}_model")
        if not isinstance(profile.compute_type, str) or not profile.compute_type.strip():
            errors.append(f"invalid_{label}_compute_type")
        if not _is_finite_number(profile.required_vram_mib) or float(profile.required_vram_mib) <= 0:
            errors.append(f"invalid_{label}_required_vram_mib")
        if isinstance(profile.model, str) and isinstance(profile.compute_type, str):
            key = (profile.model.casefold(), profile.compute_type.casefold())
            if key in seen:
                errors.append(f"duplicate_{label}_model_route")
            seen.add(key)
    return errors


def _select_model_route(
    primary: ModelMemoryProfile | None,
    fallbacks: Sequence[ModelMemoryProfile],
    *,
    free_vram_mib: float,
    reserve_mib: float,
    recent_oom: bool,
) -> tuple[ModelMemoryProfile | None, bool, list[str]]:
    assert primary is not None  # guarded by profile validation
    usable_vram = max(0.0, free_vram_mib - reserve_mib)
    reasons: list[str] = []
    candidates: list[tuple[ModelMemoryProfile, bool]] = []
    if recent_oom:
        reasons.append("recent_oom")
        reasons.append("primary_route_bypassed_after_oom")
    else:
        candidates.append((primary, False))
    for fallback in fallbacks:
        if float(fallback.required_vram_mib) < float(primary.required_vram_mib):
            candidates.append((fallback, True))
        else:
            reasons.append("non_lower_memory_fallback_ignored")

    if not recent_oom and float(primary.required_vram_mib) > usable_vram:
        reasons.append("vram_primary_insufficient")
    for profile, is_fallback in candidates:
        if float(profile.required_vram_mib) <= usable_vram:
            if is_fallback:
                reasons.append("lower_memory_fallback_selected")
            return profile, is_fallback, reasons

    reasons.append("vram_no_model_route_fits")
    return None, False, reasons


def _apply_hysteresis(
    raw_tier: str,
    previous: AdmissionHysteresisState | None,
    recovery_samples: int,
) -> tuple[str, AdmissionHysteresisState, bool]:
    if previous is None or previous.effective_tier not in _TIER_SEVERITY:
        state = AdmissionHysteresisState(effective_tier=raw_tier)
        return raw_tier, state, False
    if previous.candidate_tier is not None and previous.candidate_tier not in _TIER_SEVERITY:
        state = AdmissionHysteresisState(effective_tier=raw_tier)
        return raw_tier, state, False

    current = previous.effective_tier
    if _TIER_SEVERITY[raw_tier] >= _TIER_SEVERITY[current]:
        state = AdmissionHysteresisState(effective_tier=raw_tier)
        return raw_tier, state, False

    count = previous.consecutive_samples + 1 if previous.candidate_tier == raw_tier else 1
    if count >= recovery_samples:
        state = AdmissionHysteresisState(effective_tier=raw_tier)
        return raw_tier, state, False
    state = AdmissionHysteresisState(
        effective_tier=current,
        candidate_tier=raw_tier,
        consecutive_samples=count,
    )
    return current, state, True


def _retry_after_seconds(tier: str, config: ResourceAdmissionConfig) -> float:
    if tier == "yellow":
        return float(config.yellow_retry_after_seconds)
    if tier == "red":
        return float(config.red_retry_after_seconds)
    if tier == "unavailable":
        return float(config.unavailable_retry_after_seconds)
    return 0.0


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_percentage(value: Any, name: str, errors: list[str]) -> None:
    if not _is_finite_number(value) or not 0 <= float(value) <= 100:
        errors.append(f"invalid_{name}")


def _validate_finite_positive(value: Any, name: str, errors: list[str]) -> None:
    if not _is_finite_number(value) or float(value) <= 0:
        errors.append(f"invalid_{name}")


def _validate_finite_nonnegative(value: Any, name: str, errors: list[str]) -> None:
    if not _is_finite_number(value) or float(value) < 0:
        errors.append(f"invalid_{name}")


def _safe_ratio(numerator: Any, denominator: Any) -> float | None:
    if not _is_finite_number(numerator) or not _is_finite_number(denominator):
        return None
    if float(denominator) <= 0:
        return None
    return float(numerator) / float(denominator)


def _safe_difference(value: Any, subtract: float) -> float | None:
    if not _is_finite_number(value):
        return None
    return float(value) - float(subtract)


def _json_number(value: Any) -> float | None:
    return float(value) if _is_finite_number(value) else None


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


__all__ = [
    "AdmissionHysteresisState",
    "CPU_JOB_STAGES",
    "GPU_JOB_STAGES",
    "KNOWN_JOB_STAGES",
    "ModelMemoryProfile",
    "ResourceAdmissionConfig",
    "ResourceAdmissionDecision",
    "ResourceTelemetry",
    "decide_resource_admission",
    "resource_admission_config_from_mapping",
    "validate_resource_admission_config",
]
