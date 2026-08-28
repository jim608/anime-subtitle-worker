from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Callable

from completed_delivery import (
    COMPLETED_DELIVERY_CONTRACT,
    COMPLETED_DELIVERY_SCHEMA_VERSION,
    CompletedDeliveryError,
    completed_delivery_destination,
    completed_delivery_marker_path,
    completed_delivery_receipt_path,
    completed_delivery_root,
    validate_completed_delivery,
)

from output_manifest import (
    ADOPTED_ZH_TW_PUBLICATION_KIND,
    CONVERTED_ZH_CN_PUBLICATION_KIND,
    TRANSLATED_PUBLICATION_KIND,
    delivery_identity,
    manifest_publication_semantics,
    output_manifest_path,
    publication_is_traditional_chinese_delivery,
    validate_output_manifest,
)
from processing_provenance import load_provenance, processing_config_signature, provenance_path_for_video
from safe_files import sha256_file
from source_decision import CONVERT_ZH_CN, TRANSLATE_JAPANESE, USE_ZH_TW
from subtitle_extract import classify_subtitle_content_file
from subtitle_quality import analyze_subtitle_file


ACCEPTANCE_CONTRACT = "anime-unattended-acceptance-plan-v1"
OBSERVATION_CONTRACT = "anime-unattended-acceptance-observations-v1"
REPORT_CONTRACT = "anime-unattended-acceptance-report-v1"
FAULT_EVIDENCE_CONTRACT = "anime-fault-injection-evidence-v1"
PLAN_SCHEMA_VERSION = 2
OBSERVATION_SCHEMA_VERSION = 2
SUPPORTED_PLAN_SCHEMA_VERSIONS = frozenset({1, PLAN_SCHEMA_VERSION})
SUPPORTED_OBSERVATION_SCHEMA_VERSIONS = frozenset({1, OBSERVATION_SCHEMA_VERSION})
REQUIRED_CASE_COUNT = 100
MINIMUM_UNATTENDED_SUCCESSES = 99
MAXIMUM_REVIEW_CASES = 1
MAXIMUM_MANUAL_INTERVENTIONS = 0
MINIMUM_DISTINCT_SERIES = 10
MINIMUM_DISTINCT_CONTAINERS = 2
MINIMUM_DURATION_BUCKETS = 3
MINIMUM_FAULTED_CASES = 10
MINIMUM_CASES_PER_ROUTE = 10
MINIMUM_CASES_PER_CONTAINER = 10
MINIMUM_CASES_PER_DURATION_BUCKET = 10

ROUTES = frozenset(
    {
        "existing_zh_tw",
        "zh_cn_opencc",
        "japanese_subtitle_translation",
        "japanese_audio_asr",
    }
)
DURATION_BUCKETS = frozenset({"short", "standard", "long"})
BASE_FAULT_SCENARIOS = frozenset(
    {
        "worker_kill",
        "translation_timeout",
        "asr_process_crash",
        "gpu_oom",
        "model_unavailable",
        "output_publish_interrupt",
        "temporary_io_error",
        "temporary_database_busy",
    }
)
COMPLETED_DELIVERY_FAULT_SCENARIOS = frozenset(
    {
        "mux_process_crash",
        "completed_publish_interrupt",
    }
)
FAULT_SCENARIOS = BASE_FAULT_SCENARIOS | COMPLETED_DELIVERY_FAULT_SCENARIOS
_COMPLETED_FAULT_STAGE = {
    "mux_process_crash": "mux",
    "completed_publish_interrupt": "completed_publish",
}
_COMPLETED_FAULT_LEDGER_STAGE = {
    "mux_process_crash": "completed_delivery",
    "completed_publish_interrupt": "completed_delivery",
}
_HEX64 = re.compile(r"[0-9a-f]{64}")
_OBLIGATION_ID = re.compile(r"aiobl_[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class AcceptanceInputError(ValueError):
    """Raised when an acceptance input cannot be parsed at all."""


def read_json_object(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(
            source.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceInputError(f"cannot read JSON object {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceInputError(f"JSON root must be an object: {source}")
    return payload


def probe_media(path: Path) -> dict[str, Any]:
    """Read-only ffprobe check used to prove corpus entries are real media."""

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,channels",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceInputError(f"ffprobe failed for {path}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown ffprobe error").strip()
        raise AcceptanceInputError(f"ffprobe rejected {path}: {detail[:500]}")
    try:
        payload = json.loads(completed.stdout or "{}")
        duration = float((payload.get("format") or {}).get("duration") or 0)
        formats = {
            item.strip().casefold()
            for item in str((payload.get("format") or {}).get("format_name") or "").split(",")
            if item.strip()
        }
        streams = payload.get("streams") or []
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AcceptanceInputError(f"ffprobe returned malformed evidence for {path}") from exc
    video_streams = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    if not math.isfinite(duration) or duration <= 0 or not formats or not video_streams or not audio_streams:
        raise AcceptanceInputError(
            f"media must have positive duration plus video and audio streams: {path}"
        )
    return {
        "duration_seconds": duration,
        "format_names": sorted(formats),
        "video_streams": len(video_streams),
        "audio_streams": len(audio_streams),
    }


def duration_bucket(duration_seconds: float) -> str:
    if duration_seconds < 600:
        return "short"
    if duration_seconds <= 2100:
        return "standard"
    return "long"


def validate_plan_structure(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    _reject_unknown_fields(
        plan,
        {"contract", "schema_version", "suite_id", "created_at", "cases"},
        "plan",
        errors,
    )
    if plan.get("contract") != ACCEPTANCE_CONTRACT:
        errors.append(f"contract must be {ACCEPTANCE_CONTRACT}")
    schema_version = plan.get("schema_version")
    if schema_version not in SUPPORTED_PLAN_SCHEMA_VERSIONS:
        errors.append(
            f"schema_version must be one of {sorted(SUPPORTED_PLAN_SCHEMA_VERSIONS)}"
        )
    suite_id = plan.get("suite_id")
    if not isinstance(suite_id, str) or not _SAFE_ID.fullmatch(suite_id):
        errors.append("suite_id must be a stable 1-128 character identifier")
    _require_positive_timestamp(plan.get("created_at"), "created_at", errors)
    cases = plan.get("cases")
    if not isinstance(cases, list):
        return [*errors, "cases must be a list"]
    if len(cases) != REQUIRED_CASE_COUNT:
        errors.append(f"cases must contain exactly {REQUIRED_CASE_COUNT} entries; found {len(cases)}")

    case_ids: list[str] = []
    canonical_paths: list[str] = []
    fingerprints: list[str] = []
    obligation_ids: list[str] = []
    fault_ids: list[str] = []
    routes: list[str] = []
    series_ids: list[str] = []
    containers: list[str] = []
    buckets: list[str] = []
    faulted_cases = 0
    fault_scenarios: list[str] = []
    completed_destinations: list[str] = []
    completed_receipts: list[str] = []
    required_fault_scenarios = (
        FAULT_SCENARIOS if schema_version == PLAN_SCHEMA_VERSION else BASE_FAULT_SCENARIOS
    )
    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _reject_unknown_fields(
            case,
            {
                "case_id",
                "media",
                "expected_route",
                "strata",
                "completed_delivery",
                "faults",
            },
            prefix,
            errors,
        )
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not _SAFE_ID.fullmatch(case_id):
            errors.append(f"{prefix}.case_id must be a stable identifier")
        else:
            case_ids.append(case_id)
        route = case.get("expected_route")
        if route not in ROUTES:
            errors.append(f"{prefix}.expected_route must be one of {sorted(ROUTES)}")
        else:
            routes.append(str(route))

        media = case.get("media")
        if not isinstance(media, dict):
            errors.append(f"{prefix}.media must be an object")
        else:
            _reject_unknown_fields(
                media,
                {
                    "canonical_path",
                    "media_size",
                    "media_mtime_ns",
                    "media_fingerprint",
                    "policy_revision",
                    "obligation_id",
                },
                f"{prefix}.media",
                errors,
            )
            canonical_path = media.get("canonical_path")
            if not isinstance(canonical_path, str) or not canonical_path.strip():
                errors.append(f"{prefix}.media.canonical_path is required")
            else:
                try:
                    canonical_paths.append(_normalized_path_key(canonical_path))
                except (OSError, ValueError):
                    errors.append(f"{prefix}.media.canonical_path is invalid")
            for field in ("media_size", "media_mtime_ns"):
                value = media.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    errors.append(f"{prefix}.media.{field} must be a positive integer")
            fingerprint = media.get("media_fingerprint")
            if not isinstance(fingerprint, str) or not _HEX64.fullmatch(fingerprint):
                errors.append(f"{prefix}.media.media_fingerprint must be lowercase SHA-256")
            else:
                fingerprints.append(fingerprint)
            policy = media.get("policy_revision")
            if not isinstance(policy, str) or not _HEX64.fullmatch(policy):
                errors.append(f"{prefix}.media.policy_revision must be lowercase SHA-256")
            obligation = media.get("obligation_id")
            if not isinstance(obligation, str) or not _OBLIGATION_ID.fullmatch(obligation):
                errors.append(f"{prefix}.media.obligation_id must be an aiobl_ identity")
            else:
                obligation_ids.append(obligation)

        strata = case.get("strata")
        if not isinstance(strata, dict):
            errors.append(f"{prefix}.strata must be an object")
        else:
            _reject_unknown_fields(
                strata,
                {
                    "series_id",
                    "container",
                    "duration_bucket",
                    "audio_layout",
                    "subtitle_layout",
                    "release_profile",
                },
                f"{prefix}.strata",
                errors,
            )
            for field in ("series_id", "container", "audio_layout", "subtitle_layout", "release_profile"):
                value = strata.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{prefix}.strata.{field} is required")
            if isinstance(strata.get("series_id"), str) and strata["series_id"].strip():
                series_ids.append(strata["series_id"].strip())
            if isinstance(strata.get("container"), str) and strata["container"].strip():
                containers.append(strata["container"].strip().casefold())
            bucket = strata.get("duration_bucket")
            if bucket not in DURATION_BUCKETS:
                errors.append(f"{prefix}.strata.duration_bucket must be one of {sorted(DURATION_BUCKETS)}")
            else:
                buckets.append(str(bucket))

        completed_delivery = case.get("completed_delivery")
        if schema_version == PLAN_SCHEMA_VERSION and not isinstance(completed_delivery, dict):
            errors.append(f"{prefix}.completed_delivery is required by schema_version 2")
        if completed_delivery is not None:
            if not isinstance(completed_delivery, dict):
                errors.append(f"{prefix}.completed_delivery must be an object")
            else:
                _reject_unknown_fields(
                    completed_delivery,
                    {"source_sha256", "receipt_path", "destination"},
                    f"{prefix}.completed_delivery",
                    errors,
                )
                source_digest = completed_delivery.get("source_sha256")
                if not isinstance(source_digest, str) or not _HEX64.fullmatch(source_digest):
                    errors.append(
                        f"{prefix}.completed_delivery.source_sha256 must be lowercase SHA-256"
                    )
                for field, values in (
                    ("receipt_path", completed_receipts),
                    ("destination", completed_destinations),
                ):
                    value = completed_delivery.get(field)
                    if not isinstance(value, str) or not value.strip():
                        errors.append(f"{prefix}.completed_delivery.{field} is required")
                        continue
                    path = Path(value)
                    if not path.is_absolute():
                        errors.append(f"{prefix}.completed_delivery.{field} must be absolute")
                        continue
                    if field == "destination" and path.suffix.casefold() != ".mkv":
                        errors.append(
                            f"{prefix}.completed_delivery.destination must be a .mkv path"
                        )
                    try:
                        values.append(_normalized_path_key(path))
                    except (OSError, ValueError):
                        errors.append(f"{prefix}.completed_delivery.{field} is invalid")

        faults = case.get("faults")
        if not isinstance(faults, list):
            errors.append(f"{prefix}.faults must be a list, including [] when none are planned")
            continue
        if faults:
            faulted_cases += 1
        for fault_index, fault in enumerate(faults):
            fault_prefix = f"{prefix}.faults[{fault_index}]"
            if not isinstance(fault, dict):
                errors.append(f"{fault_prefix} must be an object")
                continue
            _reject_unknown_fields(
                fault,
                {"fault_id", "scenario", "trigger"},
                fault_prefix,
                errors,
            )
            fault_id = fault.get("fault_id")
            if not isinstance(fault_id, str) or not _SAFE_ID.fullmatch(fault_id):
                errors.append(f"{fault_prefix}.fault_id must be a stable identifier")
            else:
                fault_ids.append(fault_id)
            scenario = fault.get("scenario")
            if scenario not in FAULT_SCENARIOS:
                errors.append(f"{fault_prefix}.scenario must be one of {sorted(FAULT_SCENARIOS)}")
            else:
                fault_scenarios.append(str(scenario))
            trigger = fault.get("trigger")
            if not isinstance(trigger, str) or not trigger.strip():
                errors.append(f"{fault_prefix}.trigger is required")

    _append_duplicate_errors(case_ids, "case_id", errors)
    _append_duplicate_errors(canonical_paths, "canonical media path", errors)
    _append_duplicate_errors(fingerprints, "media fingerprint", errors)
    _append_duplicate_errors(obligation_ids, "obligation id", errors)
    _append_duplicate_errors(fault_ids, "fault id", errors)
    _append_duplicate_errors(completed_destinations, "completed destination", errors)
    _append_duplicate_errors(completed_receipts, "completed receipt path", errors)
    missing_routes = sorted(ROUTES - set(routes))
    if missing_routes:
        errors.append(f"corpus must cover every source route; missing {missing_routes}")
    for route, count in sorted(Counter(routes).items()):
        if count < MINIMUM_CASES_PER_ROUTE:
            errors.append(f"source route {route} needs at least {MINIMUM_CASES_PER_ROUTE} cases; found {count}")
    if len(set(series_ids)) < MINIMUM_DISTINCT_SERIES:
        errors.append(f"corpus must cover at least {MINIMUM_DISTINCT_SERIES} distinct series")
    substantial_containers = {
        container
        for container, count in Counter(containers).items()
        if count >= MINIMUM_CASES_PER_CONTAINER
    }
    if len(substantial_containers) < MINIMUM_DISTINCT_CONTAINERS:
        errors.append(
            f"corpus needs at least {MINIMUM_DISTINCT_CONTAINERS} containers with "
            f"{MINIMUM_CASES_PER_CONTAINER} or more cases each"
        )
    if len(set(buckets)) < MINIMUM_DURATION_BUCKETS:
        errors.append(f"corpus must cover all {MINIMUM_DURATION_BUCKETS} duration buckets")
    for bucket, count in sorted(Counter(buckets).items()):
        if count < MINIMUM_CASES_PER_DURATION_BUCKET:
            errors.append(
                f"duration bucket {bucket} needs at least {MINIMUM_CASES_PER_DURATION_BUCKET} cases; found {count}"
            )
    if faulted_cases < MINIMUM_FAULTED_CASES:
        errors.append(f"at least {MINIMUM_FAULTED_CASES} cases must have a planned fault")
    missing_faults = sorted(required_fault_scenarios - set(fault_scenarios))
    if missing_faults:
        errors.append(f"corpus must exercise every fault scenario; missing {missing_faults}")
    return errors


def validate_plan(
    plan: dict[str, Any],
    config: Any,
    *,
    media_probe: Callable[[Path], dict[str, Any]] = probe_media,
) -> list[str]:
    errors = validate_plan_structure(plan)
    cases = plan.get("cases")
    if not isinstance(cases, list) or len(cases) != REQUIRED_CASE_COUNT:
        return errors
    current_policy = processing_config_signature(config)
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("media"), dict):
            continue
        prefix = f"cases[{index}]"
        media = case["media"]
        raw_path = media.get("canonical_path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        try:
            if not path.is_file():
                errors.append(f"{prefix}: media is not a real file: {path}")
                continue
            actual = delivery_identity(path, config)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"{prefix}: cannot derive current media identity: {exc}")
            continue
        expected_identity = {
            "canonical_path": media.get("canonical_path"),
            "media_fingerprint": media.get("media_fingerprint"),
            "media_size": media.get("media_size"),
            "media_mtime_ns": media.get("media_mtime_ns"),
        }
        if actual.get("media") != expected_identity:
            errors.append(f"{prefix}: media identity drifted from the pinned corpus manifest")
        if media.get("policy_revision") != current_policy or actual.get("policy_revision") != current_policy:
            errors.append(f"{prefix}: policy revision drifted from the pinned corpus manifest")
        if media.get("obligation_id") != actual.get("obligation_id"):
            errors.append(f"{prefix}: obligation id does not match current canonical identity")
        try:
            probe = media_probe(path)
            formats = {str(item).casefold() for item in probe.get("format_names", [])}
            expected_container = str((case.get("strata") or {}).get("container") or "").casefold()
            if expected_container not in formats:
                errors.append(
                    f"{prefix}: probed container {sorted(formats)} does not match {expected_container!r}"
                )
            actual_bucket = duration_bucket(float(probe.get("duration_seconds") or 0))
            if actual_bucket != (case.get("strata") or {}).get("duration_bucket"):
                errors.append(f"{prefix}: probed duration bucket is {actual_bucket}")
            if int(probe.get("video_streams") or 0) < 1 or int(probe.get("audio_streams") or 0) < 1:
                errors.append(f"{prefix}: real media proof requires video and audio streams")
        except (AcceptanceInputError, OSError, TypeError, ValueError) as exc:
            errors.append(f"{prefix}: real media probe failed: {exc}")
        completed_plan = case.get("completed_delivery")
        if isinstance(completed_plan, dict):
            if getattr(config, "completed_delivery_enabled", False) is not True:
                errors.append(f"{prefix}: completed delivery is required but disabled")
            try:
                source_digest = sha256_file(path)
                if completed_plan.get("source_sha256") != source_digest:
                    errors.append(f"{prefix}: completed delivery source SHA-256 drifted")
                expected_destination = completed_delivery_destination(path, config)
                expected_receipt = completed_delivery_receipt_path(path, config)
                if _normalized_path_key(completed_plan.get("destination", "")) != _normalized_path_key(
                    expected_destination
                ):
                    errors.append(f"{prefix}: completed delivery destination does not match config")
                if _normalized_path_key(completed_plan.get("receipt_path", "")) != _normalized_path_key(
                    expected_receipt
                ):
                    errors.append(f"{prefix}: completed delivery receipt path does not match config")
            except (CompletedDeliveryError, OSError, TypeError, ValueError) as exc:
                errors.append(f"{prefix}: completed delivery plan cannot be verified: {exc}")
    return errors


def validate_observations(
    observations: dict[str, Any],
    *,
    plan: dict[str, Any],
    plan_file_sha256: str,
) -> list[str]:
    errors: list[str] = []
    _reject_unknown_fields(
        observations,
        {
            "contract",
            "schema_version",
            "suite_id",
            "plan_sha256",
            "started_at",
            "finished_at",
            "manual_interventions",
            "cases",
        },
        "observations",
        errors,
    )
    if observations.get("contract") != OBSERVATION_CONTRACT:
        errors.append(f"observation contract must be {OBSERVATION_CONTRACT}")
    observation_schema_version = observations.get("schema_version")
    if observation_schema_version not in SUPPORTED_OBSERVATION_SCHEMA_VERSIONS:
        errors.append(
            "observation schema_version must be one of "
            f"{sorted(SUPPORTED_OBSERVATION_SCHEMA_VERSIONS)}"
        )
    if observation_schema_version != plan.get("schema_version"):
        errors.append("observation schema_version does not match the plan")
    if observations.get("suite_id") != plan.get("suite_id"):
        errors.append("observation suite_id does not match the plan")
    if observations.get("plan_sha256") != plan_file_sha256:
        errors.append("observation plan_sha256 does not match the exact plan file")
    started = _positive_float(observations.get("started_at"))
    finished = _positive_float(observations.get("finished_at"))
    if started is None:
        errors.append("observations.started_at must be a positive timestamp")
    if finished is None or (started is not None and finished < started):
        errors.append("observations.finished_at must be at or after started_at")
    manual = observations.get("manual_interventions")
    if not isinstance(manual, list):
        errors.append("observations.manual_interventions must be a list")
    cases = observations.get("cases")
    if not isinstance(cases, list):
        return [*errors, "observations.cases must be a list"]
    if len(cases) != REQUIRED_CASE_COUNT:
        errors.append(f"observations.cases must contain exactly {REQUIRED_CASE_COUNT} entries")
    expected_cases = {
        str(case.get("case_id")): case
        for case in plan.get("cases", [])
        if isinstance(case, dict) and isinstance(case.get("case_id"), str)
    }
    observed_ids: list[str] = []
    for index, item in enumerate(cases):
        prefix = f"observations.cases[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        _reject_unknown_fields(
            item,
            {
                "case_id",
                "canonical_path",
                "obligation_id",
                "route",
                "started_at",
                "finished_at",
                "outcome",
                "review_required",
                "manual_interventions",
                "errors",
                "evidence",
                "completed_delivery",
                "faults",
            },
            prefix,
            errors,
        )
        case_id = item.get("case_id")
        if not isinstance(case_id, str):
            errors.append(f"{prefix}.case_id is required")
            continue
        observed_ids.append(case_id)
        planned = expected_cases.get(case_id)
        if planned is None:
            errors.append(f"{prefix}: unknown case_id {case_id!r}")
            continue
        media = planned.get("media") or {}
        if item.get("canonical_path") != media.get("canonical_path"):
            errors.append(f"{prefix}: canonical_path does not match the plan")
        if item.get("obligation_id") != media.get("obligation_id"):
            errors.append(f"{prefix}: obligation_id does not match the plan")
        if item.get("route") not in ROUTES:
            errors.append(f"{prefix}.route must be one of {sorted(ROUTES)}")
        if item.get("route") != planned.get("expected_route"):
            errors.append(f"{prefix}: observed route does not match the predeclared route")
        item_started = _positive_float(item.get("started_at"))
        item_finished = _positive_float(item.get("finished_at"))
        if item_started is None or item_finished is None or item_finished < item_started:
            errors.append(f"{prefix}: invalid start/finish timestamps")
        elif started is not None and finished is not None and not (
            started <= item_started <= item_finished <= finished
        ):
            errors.append(f"{prefix}: timestamps are outside the suite window")
        if item.get("outcome") not in {"completed", "failed", "review_required"}:
            errors.append(f"{prefix}.outcome must be completed, failed, or review_required")
        if not isinstance(item.get("review_required"), bool):
            errors.append(f"{prefix}.review_required must be boolean")
        if not isinstance(item.get("manual_interventions"), list):
            errors.append(f"{prefix}.manual_interventions must be a list")
        if not isinstance(item.get("errors"), list) or not all(
            isinstance(value, str) for value in item.get("errors", [])
        ):
            errors.append(f"{prefix}.errors must be a list of strings")
        if not isinstance(item.get("evidence"), list) or not item.get("evidence"):
            errors.append(f"{prefix}.evidence must contain at least one hash-bound artifact")
        else:
            for evidence_index, reference in enumerate(item["evidence"]):
                if isinstance(reference, dict):
                    _reject_unknown_fields(
                        reference,
                        {"kind", "path", "sha256"},
                        f"{prefix}.evidence[{evidence_index}]",
                        errors,
                    )
        planned_completed = planned.get("completed_delivery")
        observed_completed = item.get("completed_delivery")
        if isinstance(planned_completed, dict) and not isinstance(observed_completed, dict):
            errors.append(f"{prefix}.completed_delivery is required by the plan")
        if observed_completed is not None:
            if not isinstance(planned_completed, dict):
                errors.append(f"{prefix}.completed_delivery was not declared by the plan")
            if not isinstance(observed_completed, dict):
                errors.append(f"{prefix}.completed_delivery must be an object")
            else:
                _reject_unknown_fields(
                    observed_completed,
                    {"receipt", "final_mkv"},
                    f"{prefix}.completed_delivery",
                    errors,
                )
                for field, expected_kind, planned_field in (
                    ("receipt", "completed_delivery_receipt", "receipt_path"),
                    ("final_mkv", "completed_mkv", "destination"),
                ):
                    reference = observed_completed.get(field)
                    reference_prefix = f"{prefix}.completed_delivery.{field}"
                    if not isinstance(reference, dict):
                        errors.append(f"{reference_prefix} must be an evidence object")
                        continue
                    _reject_unknown_fields(
                        reference,
                        {"kind", "path", "sha256"},
                        reference_prefix,
                        errors,
                    )
                    if reference.get("kind") != expected_kind:
                        errors.append(f"{reference_prefix}.kind must be {expected_kind}")
                    reference_path = reference.get("path")
                    expected_path = (
                        planned_completed.get(planned_field)
                        if isinstance(planned_completed, dict)
                        else None
                    )
                    try:
                        path_matches = (
                            isinstance(reference_path, str)
                            and isinstance(expected_path, str)
                            and _normalized_path_key(reference_path)
                            == _normalized_path_key(expected_path)
                        )
                    except (OSError, ValueError):
                        path_matches = False
                    if not path_matches:
                        errors.append(f"{reference_prefix}.path does not match the plan")
                    digest = reference.get("sha256")
                    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
                        errors.append(f"{reference_prefix}.sha256 must be lowercase hexadecimal")
        observed_faults = item.get("faults")
        if not isinstance(observed_faults, list):
            errors.append(f"{prefix}.faults must be a list")
            continue
        planned_faults = {
            str(fault.get("fault_id")): fault
            for fault in planned.get("faults", [])
            if isinstance(fault, dict)
        }
        actual_fault_ids = [
            str(fault.get("fault_id"))
            for fault in observed_faults
            if isinstance(fault, dict)
        ]
        if len(actual_fault_ids) != len(set(actual_fault_ids)):
            errors.append(f"{prefix}: duplicate observed fault ids")
        if set(actual_fault_ids) != set(planned_faults):
            errors.append(f"{prefix}: observed faults do not exactly match planned faults")
        for fault_index, fault in enumerate(observed_faults):
            fault_prefix = f"{prefix}.faults[{fault_index}]"
            if not isinstance(fault, dict):
                errors.append(f"{fault_prefix} must be an object")
                continue
            _reject_unknown_fields(
                fault,
                {"fault_id", "status", "injected_at", "recovered_at", "evidence"},
                fault_prefix,
                errors,
            )
            if fault.get("status") not in {"recovered", "not_recovered", "not_injected"}:
                errors.append(f"{fault_prefix}.status is invalid")
            injected = _positive_float(fault.get("injected_at"))
            recovered = _positive_float(fault.get("recovered_at"))
            if injected is None:
                errors.append(f"{fault_prefix}.injected_at is required")
            elif item_started is not None and item_finished is not None and not (
                item_started <= injected <= item_finished
            ):
                errors.append(f"{fault_prefix}.injected_at is outside the case window")
            if fault.get("status") == "recovered" and (
                recovered is None or (injected is not None and recovered < injected)
            ):
                errors.append(f"{fault_prefix}: recovered fault needs a valid recovered_at")
            elif (
                recovered is not None
                and item_started is not None
                and item_finished is not None
                and not (item_started <= recovered <= item_finished)
            ):
                errors.append(f"{fault_prefix}.recovered_at is outside the case window")
            if not isinstance(fault.get("evidence"), list) or not fault.get("evidence"):
                errors.append(f"{fault_prefix}.evidence must not be empty")
            else:
                for evidence_index, reference in enumerate(fault["evidence"]):
                    if isinstance(reference, dict):
                        _reject_unknown_fields(
                            reference,
                            {"kind", "path", "sha256"},
                            f"{fault_prefix}.evidence[{evidence_index}]",
                            errors,
                        )
    _append_duplicate_errors(observed_ids, "observed case id", errors)
    if set(observed_ids) != set(expected_cases):
        errors.append("observations do not cover exactly the planned case ids")
    return errors


def evaluate_acceptance(
    plan_path: str | Path,
    observations_path: str | Path,
    config: Any,
) -> dict[str, Any]:
    plan_file = Path(plan_path)
    observations_file = Path(observations_path)
    plan = read_json_object(plan_file)
    observations = read_json_object(observations_file)
    plan_digest = sha256_file(plan_file)
    plan_errors = validate_plan(plan, config)
    observation_errors = validate_observations(
        observations,
        plan=plan,
        plan_file_sha256=plan_digest,
    )
    suite_started = _positive_float(observations.get("started_at")) or 0.0
    suite_finished = _positive_float(observations.get("finished_at")) or 0.0

    control_snapshot, control_errors = _read_control_snapshot(
        config,
        started_at=suite_started,
        finished_at=suite_finished,
    )
    observed_by_id = {
        str(item.get("case_id")): item
        for item in observations.get("cases", [])
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
    }
    try:
        plan_schema_version = int(plan.get("schema_version") or 0)
    except (TypeError, ValueError):
        plan_schema_version = 0
    case_reports: list[dict[str, Any]] = []
    for planned in plan.get("cases", []):
        if not isinstance(planned, dict):
            continue
        case_id = str(planned.get("case_id") or "")
        observed = observed_by_id.get(case_id)
        case_reports.append(
            _evaluate_case(
                planned,
                observed,
                config,
                suite_id=str(plan.get("suite_id") or ""),
                control_snapshot=control_snapshot,
                plan_schema_version=plan_schema_version,
            )
        )

    top_manual = observations.get("manual_interventions")
    observation_manual_count = len(top_manual) if isinstance(top_manual, list) else 1
    case_manual_count = sum(int(item.get("manual_intervention_count") or 0) for item in case_reports)
    database_manual_count = len(control_snapshot.get("commands", []))
    manual_count = observation_manual_count + case_manual_count + database_manual_count
    successes = sum(1 for item in case_reports if item.get("success") is True)
    review_cases = sum(1 for item in case_reports if item.get("review_required") is True)
    planned_faults = sum(int(item.get("planned_faults") or 0) for item in case_reports)
    recovered_faults = sum(int(item.get("recovered_faults") or 0) for item in case_reports)
    fault_failures = planned_faults - recovered_faults
    completed_required = sum(
        1 for item in case_reports if item.get("completed_delivery_required") is True
    )
    completed_verified = sum(
        1
        for item in case_reports
        if item.get("completed_delivery_required") is True
        and isinstance(item.get("completed_delivery"), dict)
        and item["completed_delivery"].get("verified") is True
    )
    input_errors = [*plan_errors, *observation_errors, *control_errors]
    qualification_reasons: list[str] = []
    if input_errors:
        qualification_reasons.append("input_or_evidence_contract_failed")
    if len(case_reports) != REQUIRED_CASE_COUNT:
        qualification_reasons.append("case_count_not_100")
    if successes < MINIMUM_UNATTENDED_SUCCESSES:
        qualification_reasons.append("unattended_success_below_99_of_100")
    if review_cases > MAXIMUM_REVIEW_CASES:
        qualification_reasons.append("review_cases_exceed_1")
    if manual_count != MAXIMUM_MANUAL_INTERVENTIONS:
        qualification_reasons.append("manual_interventions_not_zero")
    if fault_failures != 0:
        qualification_reasons.append("planned_fault_not_recovered")
    if completed_required and completed_verified != completed_required:
        qualification_reasons.append("completed_delivery_not_100_percent")
    report = {
        "contract": REPORT_CONTRACT,
        "schema_version": 1,
        "suite_id": plan.get("suite_id"),
        "evaluated_at": time.time(),
        "plan_path": str(plan_file),
        "plan_sha256": plan_digest,
        "observations_path": str(observations_file),
        "readonly_evaluation": True,
        "qualified": not qualification_reasons,
        "qualification_reasons": qualification_reasons,
        "input_errors": input_errors,
        "thresholds": {
            "required_cases": REQUIRED_CASE_COUNT,
            "minimum_unattended_successes": MINIMUM_UNATTENDED_SUCCESSES,
            "maximum_review_cases": MAXIMUM_REVIEW_CASES,
            "maximum_manual_interventions": MAXIMUM_MANUAL_INTERVENTIONS,
            "all_planned_faults_must_recover": True,
            "all_required_completed_deliveries_must_verify": True,
        },
        "counts": {
            "cases": len(case_reports),
            "unattended_successes": successes,
            "failures": len(case_reports) - successes,
            "review_cases": review_cases,
            "manual_interventions": manual_count,
            "planned_faults": planned_faults,
            "recovered_faults": recovered_faults,
            "unrecovered_faults": fault_failures,
            "completed_deliveries_required": completed_required,
            "completed_deliveries_verified": completed_verified,
        },
        "rates": {
            "unattended_success_percent": round(successes / len(case_reports) * 100, 3)
            if case_reports
            else 0.0,
            "review_percent": round(review_cases / len(case_reports) * 100, 3)
            if case_reports
            else 0.0,
            "completed_delivery_percent": round(
                completed_verified / completed_required * 100, 3
            )
            if completed_required
            else None,
        },
        "coverage": {
            "expected_routes": dict(
                sorted(Counter(str(item.get("expected_route") or "") for item in case_reports).items())
            ),
            "observed_routes": dict(
                sorted(Counter(str(item.get("route") or "") for item in case_reports).items())
            ),
            "series": dict(
                sorted(
                    Counter(
                        str((item.get("strata") or {}).get("series_id") or "")
                        for item in case_reports
                    ).items()
                )
            ),
            "containers": dict(
                sorted(
                    Counter(
                        str((item.get("strata") or {}).get("container") or "")
                        for item in case_reports
                    ).items()
                )
            ),
            "duration_buckets": dict(
                sorted(
                    Counter(
                        str((item.get("strata") or {}).get("duration_bucket") or "")
                        for item in case_reports
                    ).items()
                )
            ),
            "fault_scenarios": dict(
                sorted(
                    Counter(
                        str(fault.get("scenario") or "")
                        for item in case_reports
                        for fault in item.get("faults", [])
                        if isinstance(fault, dict)
                    ).items()
                )
            ),
        },
        "control_state_evidence": control_snapshot,
        "cases": case_reports,
    }
    return report


def _evaluate_case(
    planned: dict[str, Any],
    observed: dict[str, Any] | None,
    config: Any,
    *,
    suite_id: str,
    control_snapshot: dict[str, Any],
    plan_schema_version: int,
) -> dict[str, Any]:
    case_id = str(planned.get("case_id") or "")
    media = planned.get("media") if isinstance(planned.get("media"), dict) else {}
    video = Path(str(media.get("canonical_path") or ""))
    expected_route = str(planned.get("expected_route") or "")
    reasons: list[str] = []
    evidence: list[dict[str, Any]] = []
    route = str((observed or {}).get("route") or "")
    observation_outcome = str((observed or {}).get("outcome") or "")
    observation_review = bool((observed or {}).get("review_required") is True)
    manual = (observed or {}).get("manual_interventions")
    manual_count = len(manual) if isinstance(manual, list) else 1
    if observed is None:
        reasons.append("missing_observation")
    else:
        for ref in observed.get("evidence", []) if isinstance(observed.get("evidence"), list) else []:
            checked, error = _verify_evidence_ref(ref)
            if checked is not None:
                evidence.append(checked)
            if error:
                reasons.append(f"observation_evidence:{error}")
    if observation_outcome != "completed":
        reasons.append(f"observation_outcome:{observation_outcome or 'missing'}")
    if route != expected_route:
        reasons.append("observed_route_mismatch")
    if manual_count:
        reasons.append("case_manual_intervention")

    current_identity: dict[str, Any] | None = None
    try:
        current_identity = delivery_identity(video, config)
        if current_identity.get("obligation_id") != media.get("obligation_id"):
            reasons.append("current_obligation_identity_mismatch")
        if current_identity.get("policy_revision") != media.get("policy_revision"):
            reasons.append("current_policy_revision_mismatch")
        expected_media = {
            "canonical_path": media.get("canonical_path"),
            "media_fingerprint": media.get("media_fingerprint"),
            "media_size": media.get("media_size"),
            "media_mtime_ns": media.get("media_mtime_ns"),
        }
        if current_identity.get("media") != expected_media:
            reasons.append("current_media_identity_mismatch")
    except (OSError, TypeError, ValueError):
        reasons.append("current_media_identity_unavailable")

    manifest_path = output_manifest_path(video, config)
    manifest: dict[str, Any] | None = None
    manifest_digest = ""
    try:
        manifest = read_json_object(manifest_path)
        manifest_digest = sha256_file(manifest_path)
    except (AcceptanceInputError, OSError):
        reasons.append("output_manifest_unreadable")
    if current_identity is not None:
        valid_manifest = validate_output_manifest(
            video,
            config,
            verify_hashes=True,
            require_delivery_evidence=True,
            expected_obligation_id=str(media.get("obligation_id") or ""),
            expected_policy_revision=str(media.get("policy_revision") or ""),
        )
        if not valid_manifest:
            reasons.append("strict_output_manifest_validation_failed")

    publication = manifest_publication_semantics(manifest) if isinstance(manifest, dict) else None
    zh_tw_output: Path | None = None
    if publication is None:
        reasons.append("strict_publication_semantics_missing")
    else:
        if not publication_is_traditional_chinese_delivery(publication):
            reasons.append("publication_is_not_traditional_chinese_delivery")
        languages = publication.get("output_languages") or []
        if languages.count("zh-TW") != 1:
            reasons.append("final_traditional_chinese_output_missing")
        else:
            language_index = languages.index("zh-TW")
            outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
            if isinstance(outputs, list) and language_index < len(outputs) and isinstance(outputs[language_index], dict):
                zh_tw_output = Path(str(outputs[language_index].get("path") or ""))
    quality_gate = manifest.get("quality_gate") if isinstance(manifest, dict) else None
    if not isinstance(quality_gate, dict) or quality_gate != {
        "passed": True,
        "contract": "worker-prepublication-v1",
    }:
        reasons.append("strict_worker_quality_gate_missing")
    qc: dict[str, Any] = {}
    if zh_tw_output is not None:
        try:
            report = analyze_subtitle_file(zh_tw_output, config, role="translated")
            classification = classify_subtitle_content_file(zh_tw_output)
            qc = {
                "path": str(zh_tw_output),
                "status": report.status,
                "score": report.score,
                "dialogues": report.dialogues,
                "has_failures": report.has_failures,
                "issue_codes": [issue.code for issue in report.issues],
                "content_language": classification.language,
                "content_language_reason": classification.reason,
                "traditional_score": classification.traditional_score,
                "simplified_score": classification.simplified_score,
            }
            if report.has_failures or report.status == "rerun" or report.dialogues <= 0:
                reasons.append("traditional_chinese_qc_failed")
            if classification.language != "zh-tw":
                reasons.append("traditional_chinese_content_not_verified")
        except (OSError, TypeError, ValueError):
            reasons.append("traditional_chinese_qc_unavailable")

    provenance = load_provenance(config, video)
    provenance_route = ""
    asr_used: bool | None = None
    if not isinstance(provenance, dict):
        reasons.append("processing_provenance_missing")
    else:
        if provenance.get("schema_version") != 1:
            reasons.append("processing_provenance_schema_invalid")
        if str(provenance.get("video_path") or "") != str(video):
            reasons.append("processing_provenance_video_mismatch")
        if str(provenance.get("config_signature") or "") != str(media.get("policy_revision") or ""):
            reasons.append("processing_provenance_policy_mismatch")
        if provenance.get("status") != "complete":
            reasons.append("processing_provenance_not_complete")
        provenance_route = _provenance_route(provenance, manifest)
        if provenance_route != expected_route:
            reasons.append("processing_provenance_route_mismatch")
        asr_used = _provenance_asr_used(provenance, manifest)
        if expected_route == "japanese_audio_asr" and asr_used is not True:
            reasons.append("audio_asr_route_missing_asr_evidence")
        if expected_route != "japanese_audio_asr" and asr_used is not False:
            reasons.append("non_asr_route_has_asr_evidence")
        observed_start = _positive_float((observed or {}).get("started_at"))
        observed_finish = _positive_float((observed or {}).get("finished_at"))
        run_start = _positive_float(provenance.get("run_started_at"))
        run_finish = _positive_float(provenance.get("finished_at"))
        if run_start is None or run_finish is None:
            reasons.append("processing_provenance_timestamps_missing")
        elif observed_start is not None and observed_finish is not None and not (
            observed_start <= run_start <= run_finish <= observed_finish
        ):
            reasons.append("processing_provenance_outside_observed_window")
        provenance_file = provenance_path_for_video(config, video)
        try:
            evidence.append({"kind": "processing_provenance", "path": str(provenance_file), "sha256": sha256_file(provenance_file)})
        except OSError:
            reasons.append("processing_provenance_hash_unavailable")

    ledger, attempts, ledger_error = _read_delivery_ledger(
        config,
        obligation_id=str(media.get("obligation_id") or ""),
    )
    if ledger_error:
        reasons.append(f"delivery_ledger:{ledger_error}")
    elif ledger is not None:
        exact_fields = (
            "obligation_id",
            "canonical_path",
            "media_fingerprint",
            "media_size",
            "media_mtime_ns",
            "policy_revision",
        )
        expected = {**media, "obligation_id": media.get("obligation_id")}
        if any(ledger.get(field) != expected.get(field) for field in exact_fields):
            reasons.append("delivery_ledger_identity_mismatch")
        if ledger.get("state") != "succeeded":
            reasons.append("delivery_ledger_not_succeeded")
        if not manifest_digest or ledger.get("manifest_sha256") != manifest_digest:
            reasons.append("delivery_ledger_manifest_hash_mismatch")
        try:
            if Path(str(ledger.get("manifest_path") or "")).resolve() != manifest_path.resolve():
                reasons.append("delivery_ledger_manifest_path_mismatch")
        except OSError:
            reasons.append("delivery_ledger_manifest_path_invalid")
        verification = ledger.get("verification")
        if not isinstance(verification, dict) or verification.get("publication_semantics_verified") is not True:
            reasons.append("delivery_ledger_publication_not_verified")
        else:
            expected_verification = {
                "publication_contract": "ai-publication-semantics-v1",
                "expected_policy_revision": media.get("policy_revision"),
                "manifest_policy_revision": media.get("policy_revision"),
                "policy_revision_matched": True,
                "publication_kind": (publication or {}).get("kind"),
                "output_languages": (publication or {}).get("output_languages"),
            }
            for field, expected_value in expected_verification.items():
                if verification.get(field) != expected_value:
                    reasons.append(f"delivery_ledger_verification_mismatch:{field}")
            if (verification.get("output_languages") or []).count("zh-TW") != 1:
                reasons.append("delivery_ledger_lacks_traditional_chinese")
        try:
            attempt_count_matches = int(ledger.get("attempt_count") or -1) == len(attempts)
        except (TypeError, ValueError):
            attempt_count_matches = False
        if not attempt_count_matches:
            reasons.append("delivery_ledger_attempt_count_mismatch")
        if not attempts or str(attempts[-1].get("status") or "") != "succeeded":
            reasons.append("delivery_ledger_has_no_terminal_success_attempt")

    planned_completed = planned.get("completed_delivery")
    completed_delivery_required = isinstance(planned_completed, dict)
    completed_delivery_report: dict[str, Any] = {}
    if completed_delivery_required:
        completed_delivery_report, completed_reasons, completed_evidence = (
            _evaluate_completed_delivery(
                planned_completed,
                (observed or {}).get("completed_delivery"),
                config,
                video=video,
                media=media,
                manifest_path=manifest_path,
                manifest_digest=manifest_digest,
                publication=publication,
                observed_started_at=_positive_float((observed or {}).get("started_at")),
                observed_finished_at=_positive_float((observed or {}).get("finished_at")),
            )
        )
        reasons.extend(completed_reasons)
        evidence.extend(completed_evidence)

    matched_reviews = _reviews_for_video(control_snapshot.get("reviews", []), video)
    review_required = observation_review or bool(matched_reviews)
    if review_required:
        reasons.append("review_required")
    matched_commands = _commands_for_video(control_snapshot.get("commands", []), video)
    if matched_commands:
        reasons.append("database_manual_command_for_case")

    planned_faults = planned.get("faults") if isinstance(planned.get("faults"), list) else []
    observed_faults = (observed or {}).get("faults")
    observed_faults = observed_faults if isinstance(observed_faults, list) else []
    observed_fault_by_id = {
        str(item.get("fault_id")): item for item in observed_faults if isinstance(item, dict)
    }
    fault_report: list[dict[str, Any]] = []
    recovered_faults = 0
    for fault in planned_faults:
        if not isinstance(fault, dict):
            continue
        fault_id = str(fault.get("fault_id") or "")
        actual = observed_fault_by_id.get(fault_id)
        fault_reasons: list[str] = []
        fault_evidence: list[dict[str, Any]] = []
        structured_evidence_verified = False
        structured_evidence_errors: list[str] = []
        if actual is None:
            fault_reasons.append("missing_fault_observation")
        else:
            if actual.get("status") != "recovered":
                fault_reasons.append(str(actual.get("status") or "missing_status"))
            for ref in actual.get("evidence", []) if isinstance(actual.get("evidence"), list) else []:
                checked, error = _verify_evidence_ref(ref)
                if checked is not None:
                    fault_evidence.append(checked)
                if error:
                    fault_reasons.append(f"evidence:{error}")
                elif checked is not None:
                    structured_error = _verify_structured_fault_evidence(
                        Path(checked["path"]),
                        suite_id=suite_id,
                        case_id=case_id,
                        obligation_id=str(media.get("obligation_id") or ""),
                        fault=fault,
                        observation=actual,
                        plan_schema_version=plan_schema_version,
                    )
                    if structured_error:
                        structured_evidence_errors.append(structured_error)
                    else:
                        structured_evidence_verified = True
            if not structured_evidence_verified:
                fault_reasons.append("no_verified_structured_fault_evidence")
                if structured_evidence_errors:
                    fault_reasons.append(
                        f"structured_evidence:{structured_evidence_errors[0]}"
                    )
            scenario = str(fault.get("scenario") or "")
            attempt_error = _fault_attempt_recovery_error(
                attempts,
                actual,
                expected_stage=_COMPLETED_FAULT_LEDGER_STAGE.get(scenario),
            )
            if attempt_error:
                fault_reasons.append(f"delivery_attempts:{attempt_error}")
            if scenario in COMPLETED_DELIVERY_FAULT_SCENARIOS:
                if completed_delivery_report.get("verified") is not True:
                    fault_reasons.append("completed_delivery_receipt_not_verified")
                else:
                    injected_at = _positive_float(actual.get("injected_at"))
                    recovered_at = _positive_float(actual.get("recovered_at"))
                    committed_at = _positive_float(
                        completed_delivery_report.get("committed_at")
                    )
                    if (
                        injected_at is None
                        or recovered_at is None
                        or committed_at is None
                        or not (injected_at <= committed_at <= recovered_at)
                    ):
                        fault_reasons.append(
                            "completed_delivery_receipt_outside_recovery_window"
                        )
        recovered = not fault_reasons
        if recovered:
            recovered_faults += 1
        else:
            reasons.append(f"fault_not_recovered:{fault_id}")
        fault_report.append(
            {
                "fault_id": fault_id,
                "scenario": fault.get("scenario"),
                "trigger": fault.get("trigger"),
                "status": (actual or {}).get("status", "missing"),
                "recovered": recovered,
                "injected_at": (actual or {}).get("injected_at"),
                "recovered_at": (actual or {}).get("recovered_at"),
                "errors": fault_reasons,
                "evidence": fault_evidence,
            }
        )

    if manifest_digest:
        evidence.append({"kind": "output_manifest", "path": str(manifest_path), "sha256": manifest_digest})
    started_at = _positive_float((observed or {}).get("started_at"))
    finished_at = _positive_float((observed or {}).get("finished_at"))
    return {
        "case_id": case_id,
        "canonical_path": str(video),
        "obligation_id": media.get("obligation_id"),
        "strata": planned.get("strata") if isinstance(planned.get("strata"), dict) else {},
        "expected_route": expected_route,
        "route": route,
        "provenance_route": provenance_route,
        "asr_used": asr_used,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(finished_at - started_at, 3)
        if started_at is not None and finished_at is not None
        else None,
        "outcome": observation_outcome,
        "success": not reasons,
        "review_required": review_required,
        "manual_intervention_count": manual_count,
        "errors": list((observed or {}).get("errors") or []),
        "oracle_failures": reasons,
        "quality": qc,
        "publication": publication,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_digest,
        "completed_delivery_required": completed_delivery_required,
        "completed_delivery": completed_delivery_report,
        "ledger": ledger,
        "attempts": attempts,
        "planned_faults": len(planned_faults),
        "recovered_faults": recovered_faults,
        "faults": fault_report,
        "evidence": evidence,
    }


def _evaluate_completed_delivery(
    planned: dict[str, Any],
    observed: Any,
    config: Any,
    *,
    video: Path,
    media: dict[str, Any],
    manifest_path: Path,
    manifest_digest: str,
    publication: dict[str, Any] | None,
    observed_started_at: float | None,
    observed_finished_at: float | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Independently verify a committed completed-MKV receipt and artifact.

    The production validator remains the strongest stream/content oracle, but
    acceptance also checks the durable receipt and filesystem invariants here
    so a generic hash-bound log or a relocated artifact cannot count as proof.
    """

    reasons: list[str] = []
    evidence: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "required": True,
        "verified": False,
        "receipt_path": str(planned.get("receipt_path") or ""),
        "destination": str(planned.get("destination") or ""),
        "source_sha256": str(planned.get("source_sha256") or ""),
    }
    try:
        planned_receipt = Path(str(planned.get("receipt_path") or "")).resolve()
        planned_destination = Path(str(planned.get("destination") or "")).resolve()
        expected_receipt = completed_delivery_receipt_path(video, config).resolve()
        expected_destination = completed_delivery_destination(video, config).resolve()
        completed_root = completed_delivery_root(config).resolve()
        marker_path = completed_delivery_marker_path(video, config).resolve()
    except (CompletedDeliveryError, OSError, TypeError, ValueError) as exc:
        reasons.append(f"completed_delivery_config_invalid:{exc}")
        report["errors"] = reasons
        return report, reasons, evidence

    if planned_receipt != expected_receipt:
        reasons.append("completed_delivery_plan_receipt_path_mismatch")
    if planned_destination != expected_destination:
        reasons.append("completed_delivery_plan_destination_mismatch")
    if not _path_is_within(planned_destination, completed_root):
        reasons.append("completed_delivery_destination_outside_root")
    if planned_destination.suffix.casefold() != ".mkv":
        reasons.append("completed_delivery_destination_not_mkv")

    observed_payload = observed if isinstance(observed, dict) else {}
    checked_refs: dict[str, dict[str, Any]] = {}
    for field, expected_kind, expected_path in (
        ("receipt", "completed_delivery_receipt", planned_receipt),
        ("final_mkv", "completed_mkv", planned_destination),
    ):
        reference = observed_payload.get(field)
        checked, error = _verify_evidence_ref(reference)
        if checked is not None:
            evidence.append(checked)
            checked_refs[field] = checked
            if checked.get("kind") != expected_kind:
                reasons.append(f"completed_delivery_{field}_kind_mismatch")
            try:
                if Path(str(checked.get("path") or "")).resolve() != expected_path:
                    reasons.append(f"completed_delivery_{field}_path_mismatch")
            except OSError:
                reasons.append(f"completed_delivery_{field}_path_invalid")
        if error:
            reasons.append(f"completed_delivery_{field}_evidence:{error}")

    if marker_path.exists():
        reasons.append("completed_delivery_marker_exists")
    partial_digest = hashlib.sha256(planned_destination.name.encode()).hexdigest()[:16]
    try:
        owned_partials = sorted(
            str(path)
            for path in planned_destination.parent.glob(f".muxing-{partial_digest}-*.mkv")
            if path.is_file()
        )
    except OSError as exc:
        owned_partials = []
        reasons.append(f"completed_delivery_partial_scan_failed:{exc}")
    if owned_partials:
        reasons.append("completed_delivery_partial_exists")
    report["marker_path"] = str(marker_path)
    report["owned_partials"] = owned_partials

    receipt: dict[str, Any] | None = None
    try:
        receipt = read_json_object(planned_receipt)
    except AcceptanceInputError as exc:
        reasons.append(f"completed_delivery_receipt_unreadable:{exc}")
    committed_at: float | None = None
    if receipt is not None:
        expected_top_fields = {
            "schema_version",
            "contract",
            "source",
            "delivery",
            "publication_manifest",
            "publication",
            "destination",
            "state",
            "attempt_id",
            "output",
            "source_retained",
            "committed_at",
        }
        if set(receipt) != expected_top_fields:
            reasons.append("completed_delivery_receipt_fields_invalid")
        if receipt.get("schema_version") != COMPLETED_DELIVERY_SCHEMA_VERSION:
            reasons.append("completed_delivery_receipt_schema_invalid")
        if receipt.get("contract") != COMPLETED_DELIVERY_CONTRACT:
            reasons.append("completed_delivery_receipt_contract_invalid")
        if receipt.get("state") != "committed":
            reasons.append("completed_delivery_receipt_not_committed")
        if receipt.get("source_retained") is not True or not video.is_file():
            reasons.append("completed_delivery_source_not_retained")
        if not isinstance(receipt.get("attempt_id"), str) or not receipt.get("attempt_id"):
            reasons.append("completed_delivery_attempt_id_missing")

        expected_source = {
            "canonical_path": str(video.resolve()),
            "media_size": media.get("media_size"),
            "media_mtime_ns": media.get("media_mtime_ns"),
            "media_fingerprint": media.get("media_fingerprint"),
            "sha256": planned.get("source_sha256"),
        }
        if receipt.get("source") != expected_source:
            reasons.append("completed_delivery_source_identity_mismatch")
        try:
            if sha256_file(video) != str(planned.get("source_sha256") or ""):
                reasons.append("completed_delivery_source_hash_mismatch")
        except OSError:
            reasons.append("completed_delivery_source_hash_unavailable")

        expected_delivery = {
            "obligation_id": media.get("obligation_id"),
            "policy_revision": media.get("policy_revision"),
        }
        if receipt.get("delivery") != expected_delivery:
            reasons.append("completed_delivery_obligation_or_policy_mismatch")
        expected_manifest = {
            "path": str(manifest_path.resolve()),
            "sha256": manifest_digest,
        }
        if receipt.get("publication_manifest") != expected_manifest:
            reasons.append("completed_delivery_publication_manifest_mismatch")
        if not isinstance(publication, dict) or receipt.get("publication") != publication:
            reasons.append("completed_delivery_publication_semantics_mismatch")
        if str(receipt.get("destination") or "") != str(planned_destination):
            reasons.append("completed_delivery_receipt_destination_mismatch")

        output = receipt.get("output")
        if not isinstance(output, dict) or set(output) != {"path", "size", "mtime_ns", "sha256"}:
            reasons.append("completed_delivery_output_evidence_invalid")
        else:
            if str(output.get("path") or "") != str(planned_destination):
                reasons.append("completed_delivery_output_path_mismatch")
            try:
                output_stat = planned_destination.stat()
                if int(output.get("size") or -1) != int(output_stat.st_size):
                    reasons.append("completed_delivery_output_size_mismatch")
                if int(output.get("mtime_ns") or -1) != int(output_stat.st_mtime_ns):
                    reasons.append("completed_delivery_output_mtime_mismatch")
                output_digest = sha256_file(planned_destination)
                if str(output.get("sha256") or "") != output_digest:
                    reasons.append("completed_delivery_output_hash_mismatch")
                final_reference = checked_refs.get("final_mkv")
                if isinstance(final_reference, dict) and final_reference.get("sha256") != output_digest:
                    reasons.append("completed_delivery_final_reference_hash_mismatch")
            except OSError:
                reasons.append("completed_delivery_output_unavailable")

        committed_at = _positive_float(receipt.get("committed_at"))
        if committed_at is None:
            reasons.append("completed_delivery_committed_at_invalid")
        elif (
            observed_started_at is not None
            and observed_finished_at is not None
            and not (observed_started_at <= committed_at <= observed_finished_at)
        ):
            reasons.append("completed_delivery_commit_outside_observed_window")

    stream_report, stream_errors = _verify_completed_media_streams(
        video,
        planned_destination,
    )
    reasons.extend(stream_errors)
    report["streams"] = stream_report
    try:
        production_valid = validate_completed_delivery(video, config, verify_streams=True)
    except (CompletedDeliveryError, OSError, TypeError, ValueError):
        production_valid = False
    if not production_valid:
        reasons.append("completed_delivery_production_validation_failed")

    report.update(
        {
            "receipt_sha256": checked_refs.get("receipt", {}).get("sha256", ""),
            "output_sha256": checked_refs.get("final_mkv", {}).get("sha256", ""),
            "committed_at": committed_at,
            "source_retained": bool(receipt and receipt.get("source_retained") is True),
            "production_validator_passed": production_valid,
            "verified": not reasons,
            "errors": list(reasons),
        }
    )
    return report, reasons, evidence


def _verify_completed_media_streams(source: Path, output: Path) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    report: dict[str, Any] = {}
    try:
        source_probe = _probe_stream_inventory(source)
        output_probe = _probe_stream_inventory(output)
    except AcceptanceInputError as exc:
        return report, [f"completed_delivery_ffprobe_failed:{exc}"]

    source_streams = [item for item in source_probe.get("streams", []) if isinstance(item, dict)]
    output_streams = [item for item in output_probe.get("streams", []) if isinstance(item, dict)]
    source_av = [
        _acceptance_stream_signature(item)
        for item in source_streams
        if item.get("codec_type") in {"video", "audio"}
    ]
    output_av = [
        _acceptance_stream_signature(item)
        for item in output_streams
        if item.get("codec_type") in {"video", "audio"}
    ]
    if not source_av or source_av != output_av:
        reasons.append("completed_delivery_av_streams_changed")

    formats = {
        value.strip().casefold()
        for value in str((output_probe.get("format") or {}).get("format_name") or "").split(",")
        if value.strip()
    }
    if "matroska" not in formats:
        reasons.append("completed_delivery_output_not_matroska")
    source_duration = _probe_duration(source_probe)
    output_duration = _probe_duration(output_probe)
    if source_duration is None or output_duration is None:
        reasons.append("completed_delivery_duration_unavailable")
    else:
        tolerance = max(1.0, source_duration * 0.002)
        if abs(source_duration - output_duration) > tolerance:
            reasons.append("completed_delivery_duration_changed")

    subtitle_streams = [item for item in output_streams if item.get("codec_type") == "subtitle"]
    default_subtitles = [
        item
        for item in subtitle_streams
        if int(((item.get("disposition") or {}).get("default") or 0)) == 1
    ]
    default_zh_tw = []
    for item in default_subtitles:
        tags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
        if str(tags.get("language") or "").casefold() == "zh-tw":
            default_zh_tw.append(item)
    if len(default_subtitles) != 1 or len(default_zh_tw) != 1:
        reasons.append("completed_delivery_unique_default_zh_tw_missing")
    elif str((default_zh_tw[0].get("tags") or {}).get("title") or "") != "AI 繁體中文":
        reasons.append("completed_delivery_default_zh_tw_title_mismatch")

    report.update(
        {
            "source_av": source_av,
            "output_av": output_av,
            "source_duration_seconds": source_duration,
            "output_duration_seconds": output_duration,
            "output_formats": sorted(formats),
            "subtitle_stream_count": len(subtitle_streams),
            "default_subtitle_count": len(default_subtitles),
            "default_zh_tw_count": len(default_zh_tw),
        }
    )
    return report, reasons


def _probe_stream_inventory(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceInputError(f"ffprobe failed for {path}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown ffprobe error").strip()
        raise AcceptanceInputError(f"ffprobe rejected {path}: {detail[:500]}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AcceptanceInputError(f"ffprobe returned malformed evidence for {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise AcceptanceInputError(f"ffprobe returned no stream inventory for {path}")
    return payload


def _acceptance_stream_signature(stream: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(stream.get("codec_type") or ""),
        str(stream.get("codec_name") or ""),
        str(stream.get("profile") or ""),
        int(stream.get("width") or 0),
        int(stream.get("height") or 0),
        int(stream.get("channels") or 0),
        str(stream.get("sample_rate") or ""),
    )


def _probe_duration(payload: dict[str, Any]) -> float | None:
    try:
        value = float((payload.get("format") or {}).get("duration"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_delivery_ledger(
    config: Any,
    *,
    obligation_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    path = _configured_path(config, "scanner_state_path", "scanner_state.sqlite3")
    if not path.is_file():
        return None, [], f"scanner state database is missing: {path}"
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_readonly(path)
        row = connection.execute(
            """
            SELECT obligation_id, canonical_path, media_fingerprint, media_size,
                   media_mtime_ns, policy_revision, state, outcome_code,
                   manifest_path, manifest_sha256, verification_json,
                   eligible_at, due_at, verified_at, attempt_count
            FROM ai_delivery_obligations WHERE obligation_id=?
            """,
            (obligation_id,),
        ).fetchone()
        if row is None:
            return None, [], "obligation is missing"
        ledger = dict(row)
        try:
            verification = json.loads(str(ledger.pop("verification_json") or "{}"))
        except json.JSONDecodeError:
            verification = None
        ledger["verification"] = verification
        attempts = [
            dict(item)
            for item in connection.execute(
                """
                SELECT attempt_id, attempt_number, status, stage, error_code,
                       detail, started_at, finished_at
                FROM ai_delivery_attempts
                WHERE obligation_id=? ORDER BY attempt_number
                """,
                (obligation_id,),
            ).fetchall()
        ]
        return ledger, attempts, ""
    except (OSError, sqlite3.Error) as exc:
        return None, [], str(exc)
    finally:
        if connection is not None:
            connection.close()


def _read_control_snapshot(
    config: Any,
    *,
    started_at: float,
    finished_at: float,
) -> tuple[dict[str, Any], list[str]]:
    path = _configured_path(config, "control_state_path", "control_state.sqlite3")
    empty = {"path": str(path), "readonly": True, "commands": [], "reviews": []}
    if not path.is_file():
        return empty, [f"control state database is missing: {path}"]
    if started_at <= 0 or finished_at < started_at:
        return empty, ["cannot query control state without a valid suite time window"]
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_readonly(path)
        commands = [
            dict(row)
            for row in connection.execute(
                """
                SELECT command_id, action, target, requested_at, status
                FROM control_commands
                WHERE requested_at>=? AND requested_at<=?
                ORDER BY requested_at, command_id
                """,
                (started_at, finished_at),
            ).fetchall()
        ]
        reviews: list[dict[str, Any]] = []
        for row in connection.execute(
            """
            SELECT review_id, kind, target_key, status, diagnosis_json,
                   created_at, updated_at, resolved_at
            FROM review_items
            WHERE created_at<=?
              AND (status='open' OR updated_at>=? OR resolved_at>=?)
            ORDER BY created_at, review_id
            """,
            (finished_at, started_at, started_at),
        ).fetchall():
            item = dict(row)
            try:
                item["diagnosis"] = json.loads(str(item.pop("diagnosis_json") or "{}"))
            except json.JSONDecodeError:
                item["diagnosis"] = None
            reviews.append(item)
        return {"path": str(path), "readonly": True, "commands": commands, "reviews": reviews}, []
    except (OSError, sqlite3.Error) as exc:
        return empty, [f"control state read failed: {exc}"]
    finally:
        if connection is not None:
            connection.close()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _configured_path(config: Any, field: str, default: str) -> Path:
    configured = Path(str(getattr(config, field, default) or default))
    return configured if configured.is_absolute() else Path(config.work_path) / configured


def _provenance_route(provenance: dict[str, Any], manifest: dict[str, Any] | None) -> str:
    manifest_provenance = (manifest or {}).get("provenance")
    manifest_provenance = manifest_provenance if isinstance(manifest_provenance, dict) else {}
    source = provenance.get("subtitle_source")
    if not isinstance(source, dict):
        source = manifest_provenance.get("subtitle_source")
    source = source if isinstance(source, dict) else None
    publication = manifest_publication_semantics(manifest) if isinstance(manifest, dict) else None
    publication_kind = str((publication or {}).get("kind") or "")
    strategy = str((source or {}).get("strategy") or "")
    strict_route = {
        (ADOPTED_ZH_TW_PUBLICATION_KIND, USE_ZH_TW): "existing_zh_tw",
        (CONVERTED_ZH_CN_PUBLICATION_KIND, CONVERT_ZH_CN): "zh_cn_opencc",
        (TRANSLATED_PUBLICATION_KIND, TRANSLATE_JAPANESE): "japanese_subtitle_translation",
    }.get((publication_kind, strategy))
    if strict_route:
        return strict_route
    if publication_kind == TRANSLATED_PUBLICATION_KIND and source is None:
        return "japanese_audio_asr"
    candidates: list[Any] = [
        provenance.get("delivery_route"),
        (provenance.get("source_decision") or {}).get("route")
        if isinstance(provenance.get("source_decision"), dict)
        else None,
        (provenance.get("outcome") or {}).get("route")
        if isinstance(provenance.get("outcome"), dict)
        else None,
        manifest_provenance.get("delivery_route"),
    ]
    for candidate in candidates:
        if candidate in ROUTES:
            return str(candidate)
    return ""


def _provenance_asr_used(provenance: dict[str, Any], manifest: dict[str, Any] | None) -> bool | None:
    manifest_provenance = (manifest or {}).get("provenance")
    manifest_provenance = manifest_provenance if isinstance(manifest_provenance, dict) else {}
    source = provenance.get("subtitle_source")
    if not isinstance(source, dict):
        source = manifest_provenance.get("subtitle_source")
    if isinstance(source, dict) and isinstance(source.get("asr_used"), bool):
        return bool(source["asr_used"])
    candidates = [
        provenance.get("asr_used"),
        (provenance.get("source_decision") or {}).get("asr_used")
        if isinstance(provenance.get("source_decision"), dict)
        else None,
        manifest_provenance.get("asr_used"),
    ]
    for candidate in candidates:
        if isinstance(candidate, bool):
            return candidate
    if isinstance(provenance.get("asr"), dict):
        return True
    stages = provenance.get("stages")
    if isinstance(stages, list):
        names = {
            str(stage.get("stage") or "")
            for stage in stages
            if isinstance(stage, dict)
        }
        if names & {"transcription", "asr", "selective_asr"}:
            return True
    return None


def _verify_evidence_ref(value: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(value, dict):
        return None, "reference must be an object"
    path_value = value.get("path")
    digest = value.get("sha256")
    if not isinstance(path_value, str) or not path_value:
        return None, "path is required"
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return None, "sha256 must be lowercase hexadecimal"
    path = Path(path_value)
    try:
        if not path.is_file():
            return None, f"artifact is missing: {path}"
        actual = sha256_file(path)
    except OSError as exc:
        return None, f"artifact cannot be read: {exc}"
    checked = {"kind": str(value.get("kind") or "artifact"), "path": str(path), "sha256": actual}
    if actual != digest:
        return checked, f"artifact hash mismatch: {path}"
    return checked, ""


def _verify_structured_fault_evidence(
    path: Path,
    *,
    suite_id: str,
    case_id: str,
    obligation_id: str,
    fault: dict[str, Any],
    observation: dict[str, Any],
    plan_schema_version: int,
) -> str:
    try:
        payload = read_json_object(path)
    except AcceptanceInputError as exc:
        return str(exc)
    allowed = {
        "contract",
        "schema_version",
        "suite_id",
        "case_id",
        "obligation_id",
        "fault_id",
        "scenario",
        "trigger",
        "injected_at",
        "observed_failure",
        "recovery",
        "manual_interventions",
    }
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        return f"unknown fields {unknown}"
    evidence_schema_version = payload.get("schema_version")
    if evidence_schema_version not in {1, 2}:
        return "schema_version is unsupported"
    if fault.get("scenario") in COMPLETED_DELIVERY_FAULT_SCENARIOS and evidence_schema_version != 2:
        return "completed-delivery fault evidence requires schema_version 2"
    if plan_schema_version == 1 and evidence_schema_version != 1:
        return "schema_version does not match the v1 plan"
    expected = {
        "contract": FAULT_EVIDENCE_CONTRACT,
        "suite_id": suite_id,
        "case_id": case_id,
        "obligation_id": obligation_id,
        "fault_id": fault.get("fault_id"),
        "scenario": fault.get("scenario"),
        "trigger": fault.get("trigger"),
    }
    for field, expected_value in expected.items():
        if payload.get(field) != expected_value:
            return f"{field} does not match the fixed plan"
    injected = _positive_float(payload.get("injected_at"))
    observed_injected = _positive_float(observation.get("injected_at"))
    if injected is None or observed_injected is None or abs(injected - observed_injected) > 0.001:
        return "injected_at does not match the observation"
    failure = payload.get("observed_failure")
    if not isinstance(failure, dict) or set(failure) != {"stage", "error_code", "observed_at"}:
        return "observed_failure must contain only stage, error_code, observed_at"
    if not str(failure.get("stage") or "").strip() or not str(failure.get("error_code") or "").strip():
        return "observed_failure stage and error_code are required"
    expected_stage = _COMPLETED_FAULT_STAGE.get(str(fault.get("scenario") or ""))
    if expected_stage is not None and str(failure.get("stage") or "") != expected_stage:
        return f"observed_failure stage must be {expected_stage}"
    failure_at = _positive_float(failure.get("observed_at"))
    if failure_at is None or failure_at < injected:
        return "observed failure must occur after injection"
    recovery = payload.get("recovery")
    if not isinstance(recovery, dict) or set(recovery) != {
        "automatic",
        "started_at",
        "completed_at",
        "checkpoint",
    }:
        return "recovery must contain only automatic, started_at, completed_at, checkpoint"
    if recovery.get("automatic") is not True:
        return "recovery must be automatic"
    recovery_started = _positive_float(recovery.get("started_at"))
    recovery_completed = _positive_float(recovery.get("completed_at"))
    observed_recovered = _positive_float(observation.get("recovered_at"))
    if (
        recovery_started is None
        or recovery_completed is None
        or observed_recovered is None
        or recovery_started < failure_at
        or recovery_completed < recovery_started
        or abs(recovery_completed - observed_recovered) > 0.001
    ):
        return "automatic recovery timestamps are invalid"
    if not str(recovery.get("checkpoint") or "").strip():
        return "automatic recovery checkpoint is required"
    if expected_stage is not None and recovery.get("checkpoint") != "completed_delivery_committed":
        return "completed-delivery recovery checkpoint must be completed_delivery_committed"
    if payload.get("manual_interventions") != []:
        return "fault evidence contains a manual intervention"
    return ""


def _fault_attempt_recovery_error(
    attempts: list[dict[str, Any]],
    observation: dict[str, Any],
    *,
    expected_stage: str | None = None,
) -> str:
    injected = _positive_float(observation.get("injected_at"))
    recovered = _positive_float(observation.get("recovered_at"))
    if injected is None or recovered is None:
        return "fault recovery timestamps are unavailable"
    terminal_failures = {
        "retryable_failure",
        "deferred",
        "failed",
    }
    failed_attempt = any(
        str(attempt.get("status") or "") in terminal_failures
        and (expected_stage is None or str(attempt.get("stage") or "") == expected_stage)
        and (finished := _positive_float(attempt.get("finished_at"))) is not None
        and injected <= finished <= recovered
        for attempt in attempts
    )
    if not failed_attempt:
        return "no terminal failed attempt is bound to the injection window"
    recovered_attempt = any(
        str(attempt.get("status") or "") == "succeeded"
        and (finished := _positive_float(attempt.get("finished_at"))) is not None
        and finished >= recovered
        for attempt in attempts
    )
    if not recovered_attempt:
        return "no later succeeded attempt proves automatic recovery"
    return ""


def _reviews_for_video(reviews: Any, video: Path) -> list[dict[str, Any]]:
    if not isinstance(reviews, list):
        return []
    needle = str(video)
    matched: list[dict[str, Any]] = []
    for item in reviews:
        if not isinstance(item, dict):
            continue
        haystack = json.dumps(
            {"target_key": item.get("target_key"), "diagnosis": item.get("diagnosis")},
            ensure_ascii=False,
            sort_keys=True,
        )
        if needle in haystack:
            matched.append(item)
    return matched


def _commands_for_video(commands: Any, video: Path) -> list[dict[str, Any]]:
    if not isinstance(commands, list):
        return []
    needle = str(video)
    return [
        item
        for item in commands
        if isinstance(item, dict) and needle in str(item.get("target") or "")
    ]


def _append_duplicate_errors(values: list[str], label: str, errors: list[str]) -> None:
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        errors.append(f"duplicate {label}: {duplicates[:5]}")


def _reject_unknown_fields(
    payload: dict[str, Any],
    allowed: set[str],
    prefix: str,
    errors: list[str],
) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        errors.append(f"{prefix} has unknown fields: {unknown}")


def _normalized_path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _require_positive_timestamp(value: Any, field: str, errors: list[str]) -> None:
    if _positive_float(value) is None:
        errors.append(f"{field} must be a positive Unix timestamp")


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
