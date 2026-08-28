"""Production orchestration for bounded Worker resource admission."""

from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path
import time
import uuid
from typing import Any, Callable

from resource_admission import (
    AdmissionHysteresisState,
    GPU_JOB_STAGES,
    KNOWN_JOB_STAGES,
    ModelMemoryProfile,
    ResourceAdmissionConfig,
    decide_resource_admission,
)
from resource_telemetry_worker import (
    WorkerTelemetryConfig,
    sample_worker_resource_telemetry,
)
from safe_files import atomic_write_text


RESOURCE_STATE_CONTRACT = "resource-admission-state-v1"
RESOURCE_PLAN_CONTRACT = "resource-launch-plan-v1"
RESOURCE_SCHEMA_VERSION = 1
RESOURCE_PLAN_MAX_AGE_SECONDS = 60.0
_MAX_JSON_BYTES = 128 * 1024


def resource_admission_state_path(config: Any) -> Path:
    configured = Path(str(getattr(config, "resource_admission_state_path", "resource_admission_state.json")))
    return configured if configured.is_absolute() else Path(config.work_path) / configured


def build_resource_launch_plan(
    config: Any,
    video: str | Path,
    *,
    stage: str,
    running_gpu_jobs: int = 0,
    now: float | None = None,
    telemetry_sampler: Callable[..., Any] = sample_worker_resource_telemetry,
) -> dict[str, Any]:
    path = Path(video).resolve()
    stat = path.stat()
    previous = _read_state_unchecked(config)
    previous_hysteresis = _parse_hysteresis(previous.get("hysteresis_state") if previous else None)

    telemetry_config = WorkerTelemetryConfig(
        gpu_timeout_seconds=float(getattr(config, "resource_telemetry_gpu_timeout_seconds", 2.0)),
        host_timeout_seconds=float(getattr(config, "resource_telemetry_host_timeout_seconds", 1.0)),
        cpu_sample_interval_seconds=float(
            getattr(config, "resource_telemetry_cpu_sample_interval_seconds", 0.10)
        ),
    )
    sample = telemetry_sampler(telemetry_config)
    # The resource clock must be captured after the bounded collectors return.
    # Capturing it before sampling makes every real sample appear to come from
    # the future (the sampler timestamps itself at entry) and therefore causes
    # production admission to defer forever.
    current = _finite_now(now)
    last_oom_at = _safe_nonnegative(previous.get("last_oom_at") if previous else None)
    oom_cooldown = float(
        getattr(
            config,
            "resource_admission_recent_oom_cooldown_seconds",
            getattr(config, "resource_oom_cooldown_seconds", 21600),
        )
        or 0
    )
    recent_oom = bool(last_oom_at is not None and current - last_oom_at <= max(0.0, oom_cooldown))
    policy = _admission_policy(config)
    primary_model = str(
        getattr(config, "japanese_transcription_model", None)
        or getattr(config, "whisper_model", "")
    )
    primary_compute = str(getattr(config, "whisper_compute_type", "float16") or "float16")
    primary = ModelMemoryProfile(
        primary_model,
        primary_compute,
        float(getattr(config, "resource_admission_primary_vram_mib", 8500.0)),
    )
    lower = ModelMemoryProfile(
        primary_model,
        "int8_float16" if "int8" not in primary_compute.casefold() else "int8",
        float(getattr(config, "resource_admission_lower_memory_vram_mib", 6200.0)),
    )
    decision = decide_resource_admission(
        sample.to_resource_telemetry(now_epoch_seconds=current),
        job_stage=stage,
        primary_model=primary,
        fallback_models=(lower,),
        running_gpu_jobs=running_gpu_jobs,
        recent_oom=recent_oom,
        previous_state=previous_hysteresis,
        config=policy,
    )
    pressure = decision.tier != "green" or decision.asr_compute_type != primary_compute
    effective = {
        "concurrency": 1,
        "batch_size": max(1, int(getattr(config, "batch_size", 1)) // (2 if pressure else 1)),
        "translation_context_max_blocks": max(
            1,
            int(getattr(config, "translation_context_max_blocks", 1)) // (2 if pressure else 1),
        ),
        "translation_context_max_chars": max(
            1000,
            int(getattr(config, "translation_context_max_chars", 1000)) // (2 if pressure else 1),
        ),
        "whisperx_batch_size": max(
            1, int(getattr(config, "whisperx_batch_size", 1)) // (2 if pressure else 1)
        ),
        "transformers_whisper_batch_size": max(
            1,
            int(getattr(config, "transformers_whisper_batch_size", 1)) // (2 if pressure else 1),
        ),
    }
    sampled_at = float(sample.sampled_at_epoch_seconds)
    max_age = float(policy.telemetry_stale_after_seconds)
    retry_at = current + float(decision.retry_after_seconds) if not decision.allow_new_job else 0.0
    plan = {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "contract": RESOURCE_PLAN_CONTRACT,
        "decision_id": uuid.uuid4().hex,
        "video": {
            "canonical_path": str(path),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        },
        "sampled_at": sampled_at,
        # Admission happens before the queue attempt is created; allow bounded
        # process/bootstrap latency without pretending the telemetry itself is
        # fresh in the observability state.
        "expires_at": sampled_at + max(RESOURCE_PLAN_MAX_AGE_SECONDS, max_age),
        "stage": str(stage),
        "admitted": bool(decision.allow_new_job),
        "tier": decision.tier,
        "reason_codes": list(decision.reason_codes),
        "retry_at": retry_at,
        "selected_route": (
            {
                "model": decision.asr_model,
                "compute_type": decision.asr_compute_type,
                "required_vram_mib": decision.diagnostics.get("selected_required_vram_mib"),
            }
            if decision.asr_model and decision.asr_compute_type
            else None
        ),
        "effective": effective,
    }
    state = {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "contract": RESOURCE_STATE_CONTRACT,
        "updated_at": current,
        "sampled_at": sampled_at,
        "max_age_seconds": max_age,
        "telemetry": sample.to_dict(now_epoch_seconds=current),
        "decision": decision.to_dict(),
        "launch_plan": plan,
        "hysteresis_state": decision.hysteresis_state.to_dict(),
        "last_oom_at": last_oom_at,
        "last_oom": previous.get("last_oom") if previous else None,
    }
    _write_state(config, state)
    return plan


def serialize_launch_plan(plan: dict[str, Any]) -> str:
    parsed = _validate_plan(plan, expected_video=None, now=None, max_age=None)
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse_resource_launch_plan(
    payload: str,
    expected_video: str | Path,
    max_age: float | None = 60.0,
    now: float | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, str) or not payload or len(payload.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError("resource launch plan payload is empty or oversized")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("resource launch plan is not valid JSON") from exc
    return _validate_plan(value, expected_video=Path(expected_video), now=_finite_now(now), max_age=max_age)


def parse_authorized_resource_launch_plan(
    config: Any,
    payload: str,
    expected_video: str | Path,
    max_age: float | None = RESOURCE_PLAN_MAX_AGE_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """Parse a child plan and bind it to the atomically persisted decision.

    File identity and timestamps alone prevent accidental reuse, but they do
    not prove that an environment payload is the exact admission decision the
    parent persisted.  The decision id and full plan must match the current
    resource state before an isolated child may use it.
    """

    plan = parse_resource_launch_plan(
        payload,
        expected_video,
        max_age=max_age,
        now=now,
    )
    return validate_authorized_resource_launch_plan(
        config,
        plan,
        expected_video,
        max_age=max_age,
        now=now,
    )


def validate_authorized_resource_launch_plan(
    config: Any,
    plan: dict[str, Any],
    expected_video: str | Path,
    max_age: float | None = RESOURCE_PLAN_MAX_AGE_SECONDS,
    now: float | None = None,
) -> dict[str, Any]:
    """Revalidate plan freshness and persisted authority at actual launch."""

    parsed = _validate_plan(
        plan,
        expected_video=Path(expected_video),
        now=_finite_now(now),
        max_age=max_age,
    )
    state = _read_state_unchecked(config)
    authorized = state.get("launch_plan") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != RESOURCE_SCHEMA_VERSION
        or state.get("contract") != RESOURCE_STATE_CONTRACT
        or not isinstance(authorized, dict)
    ):
        raise ValueError("resource launch plan has no authoritative persisted decision")
    if str(authorized.get("decision_id") or "") != str(parsed.get("decision_id") or ""):
        raise ValueError("resource launch plan decision identity mismatch")
    if _canonical_json(authorized) != _canonical_json(parsed):
        raise ValueError("resource launch plan differs from the persisted decision")
    return parsed


def read_resource_admission_state(
    config: Any,
    now: float | None = None,
    max_age: float | None = None,
) -> dict[str, Any] | None:
    payload = _read_state_unchecked(config)
    if payload is None or payload.get("contract") != RESOURCE_STATE_CONTRACT:
        return None
    if payload.get("schema_version") != RESOURCE_SCHEMA_VERSION:
        return None
    sampled = _safe_nonnegative(payload.get("sampled_at"))
    allowed_age = float(max_age if max_age is not None else payload.get("max_age_seconds") or 0)
    current = _finite_now(now)
    if sampled is None or allowed_age <= 0 or current < sampled or current - sampled > allowed_age:
        return None
    return payload


def record_resource_oom(
    config: Any,
    video: str | Path,
    detail: str,
    now: float | None = None,
) -> dict[str, Any]:
    current = _finite_now(now)
    payload = _read_state_unchecked(config) or {
        "schema_version": RESOURCE_SCHEMA_VERSION,
        "contract": RESOURCE_STATE_CONTRACT,
    }
    payload["updated_at"] = current
    payload["last_oom_at"] = current
    payload["last_oom"] = {
        "video": str(Path(video).resolve()),
        "detail": str(detail)[:2000],
        "recorded_at": current,
        "reason_code": "transient_oom",
        "retry_strategy": "lower_memory_same_pipeline",
    }
    _write_state(config, payload)
    return payload


def _admission_policy(config: Any) -> ResourceAdmissionConfig:
    return ResourceAdmissionConfig(
        telemetry_stale_after_seconds=float(getattr(config, "resource_admission_telemetry_stale_seconds", 15.0)),
        cpu_yellow_percent=float(getattr(config, "resource_admission_cpu_yellow_percent", 80.0)),
        cpu_red_percent=float(getattr(config, "resource_admission_cpu_red_percent", 95.0)),
        ram_yellow_available_ratio=float(getattr(config, "resource_admission_ram_yellow_available_ratio", 0.20)),
        ram_red_available_ratio=float(getattr(config, "resource_admission_ram_red_available_ratio", 0.08)),
        gpu_yellow_percent=float(getattr(config, "resource_admission_gpu_yellow_percent", 85.0)),
        gpu_red_percent=float(getattr(config, "resource_admission_gpu_red_percent", 98.0)),
        vram_reserve_mib=float(getattr(config, "resource_admission_vram_reserve_mib", 2048.0)),
        recovery_samples=int(getattr(config, "resource_admission_recovery_samples", 3)),
        yellow_retry_after_seconds=float(getattr(config, "resource_admission_yellow_retry_seconds", 30.0)),
        red_retry_after_seconds=float(getattr(config, "resource_admission_red_retry_seconds", 120.0)),
        unavailable_retry_after_seconds=float(getattr(config, "resource_admission_unavailable_retry_seconds", 60.0)),
        gpu_concurrency_limit=1,
    )


def _validate_plan(value: Any, *, expected_video: Path | None, now: float | None, max_age: float | None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != RESOURCE_SCHEMA_VERSION:
        raise ValueError("resource launch plan schema mismatch")
    if value.get("contract") != RESOURCE_PLAN_CONTRACT or value.get("admitted") is not True:
        raise ValueError("resource launch plan is not an admitted v1 plan")
    decision_id = value.get("decision_id")
    if not isinstance(decision_id, str) or len(decision_id) != 32:
        raise ValueError("resource launch plan decision id is invalid")
    video = value.get("video")
    if not isinstance(video, dict):
        raise ValueError("resource launch plan video identity is missing")
    if expected_video is not None:
        path = expected_video.resolve()
        stat = path.stat()
        if (
            str(video.get("canonical_path")) != str(path)
            or video.get("size") != stat.st_size
            or video.get("mtime_ns") != stat.st_mtime_ns
        ):
            raise ValueError("resource launch plan video identity mismatch")
    sampled = _safe_nonnegative(value.get("sampled_at"))
    expires = _safe_nonnegative(value.get("expires_at"))
    if sampled is None or expires is None or expires < sampled:
        raise ValueError("resource launch plan timestamp is invalid")
    if now is not None:
        if now < sampled or now > expires:
            raise ValueError("resource launch plan is stale")
        if max_age is not None and now - sampled > float(max_age):
            raise ValueError("resource launch plan exceeds caller max age")
    stage = value.get("stage")
    if not isinstance(stage, str) or stage.strip().casefold() not in KNOWN_JOB_STAGES:
        raise ValueError("resource launch plan stage is invalid")
    route = value.get("selected_route")
    if stage.strip().casefold() in GPU_JOB_STAGES:
        if not isinstance(route, dict):
            raise ValueError("resource launch plan selected route is missing")
        if not isinstance(route.get("model"), str) or not str(route.get("model") or "").strip():
            raise ValueError("resource launch plan selected model is invalid")
        if not isinstance(route.get("compute_type"), str) or not str(route.get("compute_type") or "").strip():
            raise ValueError("resource launch plan selected compute type is invalid")
        required_vram = _safe_nonnegative(route.get("required_vram_mib"))
        if required_vram is None or required_vram <= 0:
            raise ValueError("resource launch plan selected VRAM requirement is invalid")
    elif route is not None:
        raise ValueError("resource launch plan has an unexpected GPU route")
    effective = value.get("effective")
    if not isinstance(effective, dict) or effective.get("concurrency") != 1:
        raise ValueError("resource launch plan effective concurrency must be one")
    for key in (
        "batch_size",
        "translation_context_max_blocks",
        "translation_context_max_chars",
        "whisperx_batch_size",
        "transformers_whisper_batch_size",
    ):
        limit = effective.get(key)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"resource launch plan effective {key} is invalid")
    json.dumps(value, allow_nan=False)
    return dict(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _read_state_unchecked(config: Any) -> dict[str, Any] | None:
    path = resource_admission_state_path(config)
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > _MAX_JSON_BYTES:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_state(config: Any, payload: dict[str, Any]) -> None:
    path = resource_admission_state_path(config)
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


def _parse_hysteresis(value: Any) -> AdmissionHysteresisState | None:
    if not isinstance(value, dict):
        return None
    try:
        return AdmissionHysteresisState(
            effective_tier=str(value["effective_tier"]),
            candidate_tier=(str(value["candidate_tier"]) if value.get("candidate_tier") is not None else None),
            consecutive_samples=int(value.get("consecutive_samples") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _safe_nonnegative(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _finite_now(value: float | None) -> float:
    current = time.time() if value is None else float(value)
    if not math.isfinite(current) or current < 0:
        raise ValueError("resource clock is invalid")
    return current


__all__ = [
    "RESOURCE_PLAN_CONTRACT",
    "RESOURCE_PLAN_MAX_AGE_SECONDS",
    "RESOURCE_SCHEMA_VERSION",
    "RESOURCE_STATE_CONTRACT",
    "build_resource_launch_plan",
    "parse_resource_launch_plan",
    "parse_authorized_resource_launch_plan",
    "read_resource_admission_state",
    "record_resource_oom",
    "resource_admission_state_path",
    "serialize_launch_plan",
    "validate_authorized_resource_launch_plan",
]
