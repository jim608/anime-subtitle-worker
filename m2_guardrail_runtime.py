from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from safe_files import atomic_write_text, sha256_file


RUNTIME_SCHEMA_VERSION = 1
RUNTIME_CONTRACT = "m2-guardrail-runtime-v1"
FAULT_RESULT_CONTRACT = "m2-isolated-circuit-breaker-test-v1"
RUNTIME_STATUSES = frozenset({"ARMED", "DISARMED", "TRIPPED", "DEGRADED"})
REQUIRED_BREAKERS = (
    "source_mutation",
    "duplicate_publish",
    "output_parse_failure",
    "incorrect_completion",
    "repeated_oom",
    "repeated_identical_stage_failure",
    "insufficient_disk_space",
)
REQUIRED_BREAKER_ASSERTIONS = (
    "new_job_claims_stopped",
    "queue_preserved",
    "checkpoint_preserved",
    "reason_evidence_persisted",
    "safe_recovery_verified",
)
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SOURCE_REVISION_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_FAULT_RUN_RE = re.compile(r"m2-guardrail-fi-\d{8}T\d{12}Z-[0-9a-f]{8}")


class RuntimeContractError(RuntimeError):
    def __init__(self, reason_code: str, *, status: str = "DEGRADED") -> None:
        normalized_status = status if status in RUNTIME_STATUSES else "DEGRADED"
        self.reason_code = _safe_code(reason_code, "runtime_validation_failed")
        self.status = normalized_status
        super().__init__(self.reason_code)


CommandRunner = Callable[[Sequence[str], str | None, float], subprocess.CompletedProcess[str]]


def runtime_state_path(config: Any, override: str | Path | None = None) -> Path:
    value = override or getattr(
        config,
        "m2_guardrail_runtime_state_path",
        "m2_guardrail_runtime.json",
    )
    work_root = Path(config.work_path).resolve()
    path = Path(value)
    target = (path if path.is_absolute() else work_root / path).resolve()
    try:
        target.relative_to(work_root)
    except ValueError as exc:
        raise RuntimeContractError("runtime_state_path_outside_work") from exc
    return target


def configuration_fingerprint(config: Any) -> str:
    """Hash the complete effective config without publishing any config value."""

    if is_dataclass(config):
        raw = asdict(config)
    elif hasattr(config, "__dict__"):
        raw = dict(vars(config))
    elif isinstance(config, Mapping):
        raw = dict(config)
    else:
        raise RuntimeContractError("effective_config_unavailable")
    # The filename used to load an otherwise identical config is not runtime
    # policy. Excluding it keeps the fingerprint stable across host/container.
    raw.pop("config_path", None)
    encoded = json.dumps(
        _json_value(raw),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def compute_worker_source_revision(repo: str | Path) -> str:
    """Reproduce safe-update-stack.sh's Worker content revision exactly."""

    root = Path(repo).resolve()
    entries: list[tuple[Path, str]] = [
        (root / "Dockerfile", "Dockerfile"),
        (root / "requirements.txt", "requirements.txt"),
        (root / "config.yaml", "config.yaml"),
    ]
    entries.extend(
        (path, f"./{path.name}")
        for path in sorted(root.glob("*.py"), key=lambda item: item.name)
        if path.is_file()
    )
    return _manifest_revision(entries)


def compute_webui_source_revision(repo: str | Path) -> str:
    """Reproduce safe-update-stack.sh's WebUI content revision exactly."""

    root = Path(repo).resolve()
    selected = (
        "Dockerfile",
        "requirements.txt",
        "package.json",
        "package-lock.json",
        "index.html",
        "vite.config.js",
        "app.py",
        "control_api.py",
        "src",
        "tests",
    )
    entries: list[tuple[Path, str]] = []
    for relative in selected:
        candidate = root / relative
        if candidate.is_file():
            entries.append((candidate, relative))
        elif candidate.is_dir():
            entries.extend(
                (path, path.relative_to(root).as_posix())
                for path in candidate.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
    entries.sort(key=lambda item: item[1])
    return _manifest_revision(entries)


def runtime_guardrail_status(
    config: Any,
    *,
    source_revision_file: str | Path | None = None,
    state_path_override: str | Path | None = None,
) -> dict[str, Any]:
    """Return the fail-closed status used immediately before each new claim."""

    marker_path = source_revision_file or getattr(
        config,
        "m2_guardrail_source_revision_file",
        "/app/.source-revision",
    )
    decision = _decision_descriptor(config)
    try:
        local_status, reason_code = _local_guardrail_status(config, decision)
    except RuntimeContractError as exc:
        return {"status": exc.status, "reason_code": exc.reason_code, "state": None}
    if local_status != "ARMED":
        return {"status": local_status, "reason_code": reason_code, "state": None}
    try:
        state = load_runtime_state(config, state_path_override)
        if state is None:
            raise RuntimeContractError("runtime_state_missing")
        if state.get("status") != "ARMED":
            status = str(state.get("status") or "DEGRADED")
            if status not in RUNTIME_STATUSES:
                status = "DEGRADED"
            return {
                "status": status,
                "reason_code": "runtime_state_not_armed",
                "state": state,
            }
        baseline = state.get("baseline")
        gate = state.get("gate")
        if not isinstance(baseline, dict) or not isinstance(gate, dict):
            raise RuntimeContractError("runtime_baseline_invalid")
        _require_sha(baseline.get("worker_commit_sha"), "worker_commit")
        _require_sha(baseline.get("webui_commit_sha"), "webui_commit")
        _require_image_id(baseline.get("worker_image_id"), "worker")
        _require_image_id(baseline.get("webui_image_id"), "webui")
        expected_source_revision = _require_source_revision(
            baseline.get("worker_source_revision"),
            "worker",
        )
        _require_source_revision(baseline.get("webui_source_revision"), "webui")
        if _read_source_revision(marker_path) != expected_source_revision:
            raise RuntimeContractError("live_worker_source_revision_mismatch")
        if baseline.get("configuration_fingerprint") != configuration_fingerprint(config):
            raise RuntimeContractError("live_configuration_fingerprint_mismatch")
        if (
            baseline.get("decision_schema_version") != decision["schema_version"]
            or baseline.get("decision_version") != decision["version"]
            or baseline.get("decision_contract") != decision["contract"]
        ):
            raise RuntimeContractError("live_decision_schema_mismatch")
        if int(gate.get("target") or 0) != 20:
            raise RuntimeContractError("runtime_gate_target_invalid")
        progress = gate.get("progress")
        if type(progress) is not int or not 0 <= progress <= 20:
            raise RuntimeContractError("runtime_gate_progress_invalid")
        if float(state.get("gate_start_epoch") or 0) <= 0 or not state.get("gate_start_at"):
            raise RuntimeContractError("runtime_gate_start_invalid")
        if state.get("production_resources_affected") is not False:
            raise RuntimeContractError("runtime_production_safety_invalid")
        breaker_tests = state.get("breaker_tests")
        if not isinstance(breaker_tests, dict) or (
            breaker_tests.get("contract") != FAULT_RESULT_CONTRACT
            or breaker_tests.get("passed_count") != len(REQUIRED_BREAKERS)
            or breaker_tests.get("required_count") != len(REQUIRED_BREAKERS)
            or breaker_tests.get("production_resources_affected") is not False
            or breaker_tests.get("worker_source_revision") != expected_source_revision
        ):
            raise RuntimeContractError("runtime_breaker_evidence_invalid")
        if any(
            not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                str(breaker_tests.get(key) or ""),
            )
            for key in ("summary_sha256", "full_log_sha256")
        ):
            raise RuntimeContractError("runtime_breaker_evidence_invalid")
        try:
            breaker_container_started = float(
                breaker_tests.get("container_started_at_epoch")
            )
            breaker_started = float(breaker_tests.get("started_at_epoch"))
            breaker_finished = float(breaker_tests.get("finished_at_epoch"))
        except (TypeError, ValueError) as exc:
            raise RuntimeContractError("runtime_breaker_evidence_invalid") from exc
        if (
            not math.isfinite(breaker_container_started)
            or not math.isfinite(breaker_started)
            or not math.isfinite(breaker_finished)
            or breaker_container_started <= 0
            or breaker_started < breaker_container_started
            or breaker_finished < breaker_started
            or breaker_finished > float(state.get("gate_start_epoch") or 0)
        ):
            raise RuntimeContractError("runtime_breaker_evidence_invalid")
        runtime_checks = state.get("runtime_checks")
        if not isinstance(runtime_checks, dict) or any(
            runtime_checks.get(key) is not True
            for key in (
                "worker_container_running",
                "webui_container_running",
                "worker_config_mount_readonly",
                "worker_command_uses_config",
                "worker_config_unchanged_since_start",
            )
        ):
            raise RuntimeContractError("runtime_container_evidence_invalid")
        pre_gate = state.get("pre_gate_running")
        if not isinstance(pre_gate, dict) or not isinstance(
            pre_gate.get("attempt_keys"),
            list,
        ):
            raise RuntimeContractError("runtime_pre_gate_snapshot_invalid")
        expected_baseline_version = _baseline_version(baseline)
        if state.get("gate_baseline_version") != expected_baseline_version:
            raise RuntimeContractError("runtime_baseline_version_mismatch")
    except RuntimeContractError as exc:
        return {"status": exc.status, "reason_code": exc.reason_code, "state": None}
    except (TypeError, ValueError):
        return {"status": "DEGRADED", "reason_code": "runtime_state_invalid", "state": None}
    return {"status": "ARMED", "reason_code": "runtime_baseline_match", "state": state}


def probe_local_runtime(
    config_path: str | Path,
    *,
    source_revision_file: str | Path = "/app/.source-revision",
    fault_summary_path: str | Path | None = None,
    fault_summary_not_before_epoch: float | None = None,
) -> dict[str, Any]:
    """Probe evidence from inside the live Worker container.

    This intentionally returns hashes, counts and contract versions only. Raw
    configuration, media paths and full fault logs stay inside the container.
    """

    from config import load_config

    config = load_config(config_path)
    decision = _decision_descriptor(config)
    local_status, reason_code = _local_guardrail_status(config, decision)
    running = _snapshot_running_jobs(config)
    worker_source_revision = _read_source_revision(source_revision_file)
    fault_results = (
        validate_fault_results(
            config,
            fault_summary_path,
            expected_worker_source_revision=worker_source_revision,
            not_before_epoch=fault_summary_not_before_epoch,
        )
        if fault_summary_path is not None
        else None
    )
    return {
        "contract": RUNTIME_CONTRACT,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "status": local_status,
        "reason_code": reason_code,
        "worker_source_revision": worker_source_revision,
        "configuration_fingerprint": configuration_fingerprint(config),
        "decision": decision,
        "guardrails": {
            "observer_enabled": bool(config.m2_server_canary_observer_enabled),
            "circuit_breaker_enabled": bool(
                config.m2_server_canary_circuit_breaker_enabled
            ),
            "gate_size": int(config.m2_server_canary_observation_gate_size),
            "max_concurrent_videos": int(config.max_concurrent_videos),
        },
        "running_snapshot": {
            "attempt_count": len(running["attempt_keys"]),
            "queue_job_count": len(running["queue_job_keys"]),
        },
        "fault_results": fault_results,
    }


def validate_fault_results(
    config: Any,
    summary_path: str | Path,
    *,
    expected_worker_source_revision: str,
    not_before_epoch: float | None,
) -> dict[str, Any]:
    """Validate the bounded summary and timestamped full-log evidence for 7 tests."""

    path = Path(summary_path)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise RuntimeContractError("fault_summary_too_large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeContractError("fault_summary_missing") from exc
    except RuntimeContractError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeContractError("fault_summary_invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeContractError("fault_summary_invalid")
    if payload.get("contract") != FAULT_RESULT_CONTRACT:
        raise RuntimeContractError("fault_summary_contract_mismatch")
    if payload.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        raise RuntimeContractError("fault_summary_contract_mismatch")
    run_id = str(payload.get("run_id") or "")
    if not _FAULT_RUN_RE.fullmatch(run_id) or path.parent.name != run_id:
        raise RuntimeContractError("fault_run_identity_invalid")
    if str(payload.get("status") or "") != "PASS":
        raise RuntimeContractError("fault_suite_not_passed")
    if payload.get("production_config_loaded") is not False:
        raise RuntimeContractError("fault_tests_not_isolated")
    if payload.get("production_resources_affected") is not False:
        raise RuntimeContractError("production_resources_affected")
    expected_revision = _require_source_revision(
        expected_worker_source_revision,
        "worker",
    )
    recorded_revision = _require_source_revision(
        payload.get("worker_source_revision"),
        "fault_worker",
    )
    if recorded_revision != expected_revision:
        raise RuntimeContractError("fault_worker_source_revision_mismatch")
    try:
        freshness_boundary = float(not_before_epoch)
        started_epoch = float(payload.get("started_at_epoch"))
        finished_epoch = float(payload.get("finished_at_epoch"))
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError("fault_summary_timestamp_invalid") from exc
    if (
        not math.isfinite(freshness_boundary)
        or not math.isfinite(started_epoch)
        or not math.isfinite(finished_epoch)
        or freshness_boundary <= 0
        or started_epoch < freshness_boundary
        or finished_epoch < started_epoch
    ):
        raise RuntimeContractError("fault_summary_not_fresh")
    if not payload.get("started_at") or not payload.get("finished_at"):
        raise RuntimeContractError("fault_summary_timestamp_invalid")

    try:
        run_directory = path.parent.resolve(strict=True)
        recorded_result = Path(str(payload.get("result_path") or "")).resolve(strict=True)
        log_path = Path(str(payload.get("log_path") or "")).resolve(strict=True)
        if recorded_result != path.resolve(strict=True):
            raise RuntimeContractError("fault_result_path_mismatch")
        if log_path.parent != run_directory or log_path.name != "events.jsonl":
            raise RuntimeContractError("fault_log_scope_invalid")
        if not log_path.is_file() or not 0 < log_path.stat().st_size <= 16 * 1024 * 1024:
            raise RuntimeContractError("fault_log_missing")
        log_digest = "sha256:" + sha256_file(log_path)
    except RuntimeContractError:
        raise
    except OSError as exc:
        raise RuntimeContractError("fault_log_unreadable") from exc

    tests = payload.get("case_results")
    if not isinstance(tests, list):
        raise RuntimeContractError("fault_test_results_invalid")
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in tests:
        if not isinstance(item, dict):
            raise RuntimeContractError("fault_test_results_invalid")
        name = str(item.get("fault") or "")
        if name in by_name:
            raise RuntimeContractError("fault_test_duplicate_result")
        by_name[name] = item
    if set(by_name) != set(REQUIRED_BREAKERS):
        raise RuntimeContractError("fault_test_coverage_incomplete")
    if (
        payload.get("breaker_tests_passed") != len(REQUIRED_BREAKERS)
        or payload.get("breaker_tests_total") != len(REQUIRED_BREAKERS)
    ):
        raise RuntimeContractError("fault_test_count_mismatch")
    verified_events = _verified_fault_log_events(log_path)
    for name in REQUIRED_BREAKERS:
        item = by_name[name]
        if item.get("passed") is not True:
            raise RuntimeContractError(f"fault_test_failed_{name}")
        checks = item.get("checks")
        if not isinstance(checks, dict):
            raise RuntimeContractError(f"fault_checks_invalid_{name}")
        assertion_map = {
            "new_job_claims_stopped": "new_job_claim_stopped",
            "queue_preserved": "queue_preserved",
            "checkpoint_preserved": "checkpoint_preserved",
            "reason_evidence_persisted": "reason_evidence_persisted",
            "safe_recovery_verified": "safe_recovery",
        }
        for assertion in REQUIRED_BREAKER_ASSERTIONS:
            if checks.get(assertion_map[assertion]) is not True:
                raise RuntimeContractError(f"fault_assertion_failed_{name}_{assertion}")
        for assertion in (
            "production_admission_path_called",
            "running_job_not_interrupted",
            "no_false_completed",
            "source_unchanged",
            "production_output_untouched",
        ):
            if checks.get(assertion) is not True:
                raise RuntimeContractError(f"fault_assertion_failed_{name}_{assertion}")
        if name not in verified_events:
            raise RuntimeContractError(f"fault_reason_evidence_missing_{name}")
    return {
        "contract": FAULT_RESULT_CONTRACT,
        "passed_count": len(REQUIRED_BREAKERS),
        "required_count": len(REQUIRED_BREAKERS),
        "worker_source_revision": recorded_revision,
        "container_started_at": _utc_timestamp(freshness_boundary),
        "container_started_at_epoch": freshness_boundary,
        "started_at": _utc_timestamp(started_epoch),
        "finished_at": _utc_timestamp(finished_epoch),
        "started_at_epoch": started_epoch,
        "finished_at_epoch": finished_epoch,
        "summary_sha256": "sha256:" + sha256_file(path),
        "full_log_sha256": log_digest,
        "production_resources_affected": False,
    }


def _verified_fault_log_events(path: Path) -> set[str]:
    verified: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get("event") != "case_verified":
                    continue
                fault = str(event.get("fault") or "")
                if fault not in REQUIRED_BREAKERS:
                    continue
                if event.get("breaker_reason") != fault:
                    continue
                if not isinstance(event.get("breaker_evidence"), dict) or not event["breaker_evidence"]:
                    continue
                verified.add(fault)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeContractError("fault_log_invalid") from exc
    return verified


def initialize_gate(
    config: Any,
    evidence: Mapping[str, Any],
    *,
    source_revision_file: str | Path = "/app/.source-revision",
    state_path_override: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Atomically arm one immutable 0/20 baseline from verified runtime evidence."""

    timestamp = time.time() if now is None else float(now)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise RuntimeContractError("gate_start_timestamp_invalid")
    worker_source_revision = _read_source_revision(source_revision_file)
    decision = _decision_descriptor(config)
    local_status, reason_code = _local_guardrail_status(config, decision)
    if local_status != "ARMED":
        raise RuntimeContractError(reason_code, status=local_status)

    worker_commit_sha = _require_sha(evidence.get("worker_commit_sha"), "worker_commit")
    webui_commit_sha = _require_sha(evidence.get("webui_commit_sha"), "webui_commit")
    expected_worker_source_revision = _require_source_revision(
        evidence.get("worker_source_revision"),
        "worker",
    )
    webui_source_revision = _require_source_revision(
        evidence.get("webui_source_revision"),
        "webui",
    )
    if worker_source_revision != expected_worker_source_revision:
        raise RuntimeContractError("worker_source_revision_changed_during_arm")
    worker_image_id = _require_image_id(evidence.get("worker_image_id"), "worker")
    webui_image_id = _require_image_id(evidence.get("webui_image_id"), "webui")
    fingerprint = configuration_fingerprint(config)
    if str(evidence.get("configuration_fingerprint") or "") != fingerprint:
        raise RuntimeContractError("configuration_changed_during_arm")
    if evidence.get("decision") != decision:
        raise RuntimeContractError("decision_schema_changed_during_arm")
    fault_results = evidence.get("fault_results")
    if not isinstance(fault_results, dict):
        raise RuntimeContractError("fault_test_evidence_missing")
    if (
        fault_results.get("contract") != FAULT_RESULT_CONTRACT
        or fault_results.get("passed_count") != len(REQUIRED_BREAKERS)
        or fault_results.get("required_count") != len(REQUIRED_BREAKERS)
        or fault_results.get("production_resources_affected") is not False
        or fault_results.get("worker_source_revision")
        != expected_worker_source_revision
    ):
        raise RuntimeContractError("fault_test_coverage_incomplete")
    for digest_key in ("summary_sha256", "full_log_sha256"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(fault_results.get(digest_key) or "")):
            raise RuntimeContractError("fault_test_digest_invalid")
    try:
        fault_container_started_epoch = float(
            fault_results.get("container_started_at_epoch")
        )
        fault_started_epoch = float(fault_results.get("started_at_epoch"))
        fault_finished_epoch = float(fault_results.get("finished_at_epoch"))
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError("fault_test_timestamp_invalid") from exc
    if (
        not math.isfinite(fault_container_started_epoch)
        or not math.isfinite(fault_started_epoch)
        or not math.isfinite(fault_finished_epoch)
        or fault_container_started_epoch <= 0
        or fault_started_epoch < fault_container_started_epoch
        or fault_finished_epoch < fault_started_epoch
        or fault_finished_epoch > timestamp
    ):
        raise RuntimeContractError("fault_test_timestamp_invalid")
    runtime_checks = evidence.get("runtime_checks")
    if not isinstance(runtime_checks, dict) or any(
        runtime_checks.get(key) is not True
        for key in (
            "worker_container_running",
            "webui_container_running",
            "worker_config_mount_readonly",
            "worker_command_uses_config",
            "worker_config_unchanged_since_start",
        )
    ):
        raise RuntimeContractError("runtime_container_contract_unproven")

    baseline = {
        "worker_commit_sha": worker_commit_sha,
        "webui_commit_sha": webui_commit_sha,
        "worker_source_revision": expected_worker_source_revision,
        "webui_source_revision": webui_source_revision,
        "worker_image_id": worker_image_id,
        "webui_image_id": webui_image_id,
        "configuration_fingerprint": fingerprint,
        "decision_schema_version": decision["schema_version"],
        "decision_version": decision["version"],
        "decision_contract": decision["contract"],
    }
    baseline_version = _baseline_version(baseline)
    target = runtime_state_path(config, state_path_override)
    existing = _read_json(target)
    if isinstance(existing, dict) and existing.get("status") == "ARMED":
        existing_gate = existing.get("gate")
        if (
            isinstance(existing_gate, dict)
            and existing.get("gate_baseline_version") == baseline_version
            and existing.get("baseline") == baseline
            and int(existing_gate.get("target", 0)) == 20
        ):
            from m2_production_observation import initialize_observation_gate

            initialize_observation_gate(config, existing, now=timestamp)
            return existing
        raise RuntimeContractError("armed_gate_already_exists")

    running = _snapshot_running_jobs(config)
    state = {
        "contract": RUNTIME_CONTRACT,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "status": "ARMED",
        "armed_at": _utc_timestamp(timestamp),
        "gate_start_at": _utc_timestamp(timestamp),
        "gate_start_epoch": timestamp,
        "gate_baseline_version": baseline_version,
        "baseline": baseline,
        "gate": {
            "target": 20,
            "progress": 0,
            "claimed_after_gate_start": 0,
            "completed_strict_verified": 0,
        },
        "pre_gate_running": {
            "attempt_keys": running["attempt_keys"],
            "queue_job_keys": running["queue_job_keys"],
            "attempt_count": len(running["attempt_keys"]),
            "queue_job_count": len(running["queue_job_keys"]),
            "policy": "record_result_but_exclude_from_formal_gate",
        },
        "breaker_tests": fault_results,
        "runtime_checks": {
            key: True
            for key in (
                "worker_container_running",
                "webui_container_running",
                "worker_config_mount_readonly",
                "worker_command_uses_config",
                "worker_config_unchanged_since_start",
            )
        },
        "production_resources_affected": False,
    }
    # Publish the empty strict-observation state first. If the second atomic
    # write fails, claim admission remains DEGRADED because no ARMED runtime
    # manifest exists; it can never count jobs against a half-created gate.
    from m2_production_observation import initialize_observation_gate

    initialize_observation_gate(config, state, now=timestamp)
    atomic_write_text(
        target,
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return state


def load_runtime_state(config: Any, override: str | Path | None = None) -> dict[str, Any] | None:
    payload = _read_json(runtime_state_path(config, override))
    if payload is None:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("contract") != RUNTIME_CONTRACT
        or payload.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or payload.get("status") not in RUNTIME_STATUSES
    ):
        raise RuntimeContractError("runtime_state_invalid")
    return payload


def gate_claim_eligible(
    state: Mapping[str, Any],
    *,
    job_identity: str,
    claimed_at: float,
    gate_baseline_version: str,
) -> tuple[bool, str]:
    """Return whether a claim belongs to the formal post-arm 20-job gate."""

    if state.get("status") != "ARMED":
        return False, "guardrails_not_armed"
    if not str(job_identity or "").strip():
        return False, "job_identity_invalid"
    if str(gate_baseline_version or "") != str(state.get("gate_baseline_version") or ""):
        return False, "runtime_baseline_mismatch"
    try:
        start = float(state.get("gate_start_epoch"))
        claim = float(claimed_at)
    except (TypeError, ValueError):
        return False, "claim_timestamp_invalid"
    if not math.isfinite(start) or not math.isfinite(claim):
        return False, "claim_timestamp_invalid"
    if claim <= start:
        return False, "claimed_before_gate_start"
    key = _job_key(job_identity)
    pre_gate = state.get("pre_gate_running")
    excluded = pre_gate.get("attempt_keys", []) if isinstance(pre_gate, dict) else []
    if key in excluded:
        return False, "running_before_gate_start"
    return True, "eligible"


def arm_runtime_on_host(
    *,
    docker_binary: str,
    worker_container: str,
    webui_container: str,
    expected_worker_commit_sha: str,
    expected_webui_commit_sha: str,
    fault_summary_path: str,
    worker_repo: str | Path = ".",
    webui_repo: str | Path = "../anime-subtitle-worker-webui",
    worker_config_path: str = "/app/config.yaml",
    worker_source_revision_file: str = "/app/.source-revision",
    webui_source_revision_file: str = "/app/.source-revision",
    runtime_state_path_override: str = "",
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Verify and arm the live stack with one bounded host-side invocation."""

    worker_name = _require_container_name(worker_container)
    webui_name = _require_container_name(webui_container)
    wanted_worker_commit = _require_sha(expected_worker_commit_sha, "worker_commit")
    wanted_webui_commit = _require_sha(expected_webui_commit_sha, "webui_commit")
    run = runner or _default_runner
    worker_commit = _clean_git_commit(worker_repo, run, "worker")
    webui_commit = _clean_git_commit(webui_repo, run, "webui")
    if worker_commit != wanted_worker_commit:
        raise RuntimeContractError("worker_commit_sha_mismatch")
    if webui_commit != wanted_webui_commit:
        raise RuntimeContractError("webui_commit_sha_mismatch")
    worker_source_revision = compute_worker_source_revision(worker_repo)
    webui_source_revision = compute_webui_source_revision(webui_repo)
    worker_inspect = _inspect_container(docker_binary, worker_name, run)
    webui_inspect = _inspect_container(docker_binary, webui_name, run)
    worker_image_id = _require_image_id(worker_inspect.get("Image"), "worker")
    webui_image_id = _require_image_id(webui_inspect.get("Image"), "webui")
    if not _container_running(worker_inspect):
        raise RuntimeContractError("worker_container_not_running")
    if not _container_running(webui_inspect):
        raise RuntimeContractError("webui_container_not_running")
    worker_started_epoch = _container_started_epoch(worker_inspect, "worker")
    if not _readonly_mount(worker_inspect, worker_config_path):
        raise RuntimeContractError("worker_config_mount_not_readonly")
    if not _worker_command_uses_config(worker_inspect, worker_config_path):
        raise RuntimeContractError("worker_command_config_mismatch")
    if not _worker_config_unchanged_since_start(worker_inspect, worker_config_path):
        raise RuntimeContractError("worker_config_changed_after_start")

    probe_command = [
        docker_binary,
        "exec",
        worker_name,
        "python",
        "/app/m2_guardrail_runtime.py",
        "probe",
        "--config",
        worker_config_path,
        "--source-revision-file",
        worker_source_revision_file,
        "--fault-summary",
        fault_summary_path,
        "--fault-not-before-epoch",
        repr(worker_started_epoch),
    ]
    probe_result = _run_json(probe_command, run, "worker_runtime_probe_failed")
    probe_status = str(probe_result.get("status") or "DEGRADED")
    if probe_status not in RUNTIME_STATUSES:
        raise RuntimeContractError("worker_runtime_status_invalid")
    if probe_status != "ARMED":
        raise RuntimeContractError(
            str(probe_result.get("reason_code") or "worker_guardrails_not_armed"),
            status=probe_status,
        )
    if probe_result.get("worker_source_revision") != worker_source_revision:
        raise RuntimeContractError("worker_source_revision_mismatch")

    webui_revision_result = _run_command(
        [docker_binary, "exec", webui_name, "cat", webui_source_revision_file],
        run,
        reason_code="webui_runtime_probe_failed",
    )
    live_webui_source_revision = _require_source_revision(
        webui_revision_result.stdout.strip(),
        "webui",
    )
    if live_webui_source_revision != webui_source_revision:
        raise RuntimeContractError("webui_source_revision_mismatch")

    evidence = {
        "worker_commit_sha": worker_commit,
        "webui_commit_sha": webui_commit,
        "worker_source_revision": worker_source_revision,
        "webui_source_revision": webui_source_revision,
        "worker_image_id": worker_image_id,
        "webui_image_id": webui_image_id,
        "configuration_fingerprint": probe_result.get("configuration_fingerprint"),
        "decision": probe_result.get("decision"),
        "fault_results": probe_result.get("fault_results"),
        "runtime_checks": {
            "worker_container_running": True,
            "webui_container_running": True,
            "worker_config_mount_readonly": True,
            "worker_command_uses_config": True,
            "worker_config_unchanged_since_start": True,
        },
    }
    initialize_command = [
        docker_binary,
        "exec",
        "-i",
        worker_name,
        "python",
        "/app/m2_guardrail_runtime.py",
        "initialize",
        "--config",
        worker_config_path,
        "--source-revision-file",
        worker_source_revision_file,
    ]
    if runtime_state_path_override:
        initialize_command.extend(["--state-path", runtime_state_path_override])
    initialized = _run_json(
        initialize_command,
        run,
        "runtime_gate_initialization_failed",
        stdin=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    )
    if initialized.get("status") != "ARMED":
        raise RuntimeContractError("runtime_gate_not_armed")
    if initialized.get("worker_runtime_sha") != worker_commit:
        raise RuntimeContractError("initialized_worker_baseline_mismatch")
    if initialized.get("webui_runtime_sha") != webui_commit:
        raise RuntimeContractError("initialized_webui_baseline_mismatch")
    return initialized


def _local_guardrail_status(
    config: Any,
    decision: Mapping[str, Any],
) -> tuple[str, str]:
    if not bool(getattr(config, "m2_server_canary_observer_enabled", False)):
        return "DISARMED", "observer_disabled"
    if not bool(getattr(config, "m2_server_canary_circuit_breaker_enabled", False)):
        return "DISARMED", "circuit_breaker_disabled"
    try:
        from m2_production_observation import circuit_breaker_active

        if circuit_breaker_active(config):
            return "TRIPPED", "circuit_breaker_tripped"
    except Exception as exc:  # noqa: BLE001 - status probing must fail closed.
        raise RuntimeContractError("circuit_breaker_status_unreadable") from exc
    if int(getattr(config, "m2_server_canary_observation_gate_size", 0) or 0) != 20:
        return "DEGRADED", "observation_gate_size_mismatch"
    if int(getattr(config, "max_concurrent_videos", 0) or 0) != 1:
        return "DEGRADED", "unsafe_worker_concurrency"
    if not bool(getattr(config, "source_integrity_sha256_enabled", False)):
        return "DEGRADED", "source_integrity_sha256_disabled"
    from source_analyzer import DECISION_SCHEMA_VERSION, DECISION_VERSION
    from source_decision import SOURCE_DECISION_CONTRACT

    if (
        decision.get("schema_version") != DECISION_SCHEMA_VERSION
        or decision.get("version") != DECISION_VERSION
        or decision.get("contract") != SOURCE_DECISION_CONTRACT
    ):
        return "DEGRADED", "decision_schema_mismatch"
    return "ARMED", "runtime_guardrails_loaded"


def _decision_descriptor(config: Any) -> dict[str, Any]:
    from source_decision import SOURCE_DECISION_CONTRACT

    return {
        "schema_version": int(getattr(config, "source_decision_schema_version", 0)),
        "version": str(getattr(config, "source_decision_version", "")),
        "contract": SOURCE_DECISION_CONTRACT,
    }


def _snapshot_running_jobs(config: Any) -> dict[str, list[str]]:
    from scan_state import scan_state_path

    database = scan_state_path(config)
    if not database.is_file():
        raise RuntimeContractError("queue_state_database_missing")
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            attempts = connection.execute(
                "SELECT attempt_id FROM ai_delivery_attempts WHERE status='running' ORDER BY attempt_id"
            ).fetchall()
            queue_jobs = connection.execute(
                "SELECT path FROM ai_candidate_queue WHERE status='running' ORDER BY path"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeContractError("queue_state_snapshot_failed") from exc
    return {
        "attempt_keys": [_job_key(row[0]) for row in attempts],
        "queue_job_keys": [_job_key(row[0]) for row in queue_jobs],
    }


def _manifest_revision(entries: Sequence[tuple[Path, str]]) -> str:
    if not entries:
        raise RuntimeContractError("source_revision_inputs_missing")
    manifest: list[str] = []
    for path, label in entries:
        if not path.is_file():
            raise RuntimeContractError("source_revision_inputs_missing")
        manifest.append(f"{sha256_file(path)}  {label}\n")
    return hashlib.sha256("".join(manifest).encode("utf-8")).hexdigest()


def _clean_git_commit(
    repo: str | Path,
    runner: CommandRunner,
    component: str,
) -> str:
    repository = str(Path(repo).resolve())
    git = ["git", "-c", f"safe.directory={repository}", "-C", repository]
    status = _run_command(
        [*git, "status", "--porcelain", "--untracked-files=no"],
        runner,
        reason_code=f"{component}_git_status_failed",
    )
    if status.stdout.strip():
        raise RuntimeContractError(f"{component}_git_worktree_not_clean")
    source_scope = (
        ["*.py"]
        if component == "worker"
        else [
            "Dockerfile",
            "requirements.txt",
            "package.json",
            "package-lock.json",
            "index.html",
            "vite.config.js",
            "app.py",
            "control_api.py",
            "src",
            "tests",
        ]
    )
    untracked = _run_command(
        [
            *git,
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            *source_scope,
        ],
        runner,
        reason_code=f"{component}_git_untracked_check_failed",
    )
    untracked_inputs = [line.strip() for line in untracked.stdout.splitlines() if line.strip()]
    if component == "worker":
        untracked_inputs = [
            value
            for value in untracked_inputs
            if "/" not in value.replace("\\", "/") and value.endswith(".py")
        ]
    if untracked_inputs:
        raise RuntimeContractError(f"{component}_source_inputs_untracked")
    revision = _run_command(
        [*git, "rev-parse", "HEAD"],
        runner,
        reason_code=f"{component}_git_revision_failed",
    )
    return _require_sha(revision.stdout.strip(), f"{component}_commit")


def _inspect_container(
    docker_binary: str,
    container: str,
    runner: CommandRunner,
) -> Mapping[str, Any]:
    result = _run_command(
        [docker_binary, "inspect", container],
        runner,
        reason_code="container_inspect_failed",
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeContractError("container_inspect_invalid") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeContractError("container_inspect_invalid")
    return payload[0]


def _container_running(payload: Mapping[str, Any]) -> bool:
    state = payload.get("State")
    return isinstance(state, dict) and state.get("Running") is True


def _container_started_epoch(payload: Mapping[str, Any], component: str) -> float:
    state = payload.get("State")
    if not isinstance(state, dict):
        raise RuntimeContractError(f"{_safe_code(component, 'container')}_started_at_invalid")
    started_raw = str(state.get("StartedAt") or "")
    try:
        started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        if started.tzinfo is None:
            raise ValueError("container timestamp has no timezone")
        epoch = started.timestamp()
    except (OverflowError, ValueError) as exc:
        raise RuntimeContractError(
            f"{_safe_code(component, 'container')}_started_at_invalid"
        ) from exc
    if not math.isfinite(epoch) or epoch <= 0:
        raise RuntimeContractError(f"{_safe_code(component, 'container')}_started_at_invalid")
    return epoch


def _readonly_mount(payload: Mapping[str, Any], destination: str) -> bool:
    mounts = payload.get("Mounts")
    if not isinstance(mounts, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("Destination") or "") == destination
        and item.get("RW") is False
        for item in mounts
    )


def _worker_command_uses_config(payload: Mapping[str, Any], config_path: str) -> bool:
    config = payload.get("Config")
    if not isinstance(config, dict):
        return False
    command = payload.get("Args")
    if not isinstance(command, list):
        command = config.get("Cmd")
        if not isinstance(command, list):
            return False
    tokens = [str(value) for value in command]
    try:
        index = tokens.index("--config")
    except ValueError:
        return False
    if index + 1 >= len(tokens):
        return False
    supplied = tokens[index + 1]
    if supplied == config_path:
        return True
    working_directory = str(config.get("WorkingDir") or "/")
    return str(PurePosixPath(working_directory) / supplied) == config_path


def _worker_config_unchanged_since_start(
    payload: Mapping[str, Any],
    destination: str,
) -> bool:
    state = payload.get("State")
    mounts = payload.get("Mounts")
    if not isinstance(state, dict) or not isinstance(mounts, list):
        return False
    started_raw = str(state.get("StartedAt") or "")
    try:
        started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    source = next(
        (
            str(item.get("Source") or "")
            for item in mounts
            if isinstance(item, dict)
            and str(item.get("Destination") or "") == destination
            and item.get("RW") is False
        ),
        "",
    )
    if not source:
        return False
    try:
        modified_at = Path(source).stat().st_mtime
    except OSError:
        return False
    # Filesystem timestamp precision differs across UNRAID filesystems. A
    # two-second allowance accepts the same deployment write/start boundary,
    # while still rejecting any later operator edit not loaded by the process.
    return modified_at <= started_at + 2.0


def _run_json(
    command: Sequence[str],
    runner: CommandRunner,
    reason_code: str,
    *,
    stdin: str | None = None,
) -> dict[str, Any]:
    result = _run_command(command, runner, reason_code=reason_code, stdin=stdin)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeContractError(reason_code) from exc
    if not isinstance(payload, dict):
        raise RuntimeContractError(reason_code)
    return payload


def _run_command(
    command: Sequence[str],
    runner: CommandRunner,
    *,
    reason_code: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(list(command), stdin, 30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeContractError(reason_code) from exc
    if result.returncode != 0:
        raise RuntimeContractError(reason_code)
    return result


def _default_runner(
    command: Sequence[str],
    stdin: str | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _public_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    baseline = state.get("baseline") if isinstance(state.get("baseline"), dict) else {}
    gate = state.get("gate") if isinstance(state.get("gate"), dict) else {}
    breaker_tests = (
        state.get("breaker_tests") if isinstance(state.get("breaker_tests"), dict) else {}
    )
    return {
        "status": str(state.get("status") or "DEGRADED"),
        "worker_runtime_sha": str(baseline.get("worker_commit_sha") or ""),
        "webui_runtime_sha": str(baseline.get("webui_commit_sha") or ""),
        "worker_source_revision": str(baseline.get("worker_source_revision") or ""),
        "webui_source_revision": str(baseline.get("webui_source_revision") or ""),
        "worker_image_id": str(baseline.get("worker_image_id") or ""),
        "webui_image_id": str(baseline.get("webui_image_id") or ""),
        "configuration_fingerprint": str(
            baseline.get("configuration_fingerprint") or ""
        ),
        "decision_schema_version": baseline.get("decision_schema_version"),
        "gate_start_at": str(state.get("gate_start_at") or ""),
        "gate_baseline_version": str(state.get("gate_baseline_version") or ""),
        "initial_gate_progress": f"{int(gate.get('progress') or 0)}/{int(gate.get('target') or 20)}",
        "breaker_tests_passed": int(breaker_tests.get("passed_count") or 0),
        "production_resources_affected": bool(
            state.get("production_resources_affected", True)
        ),
    }


def _read_source_revision(path: str | Path) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip().casefold()
    except OSError as exc:
        raise RuntimeContractError("runtime_source_revision_unreadable") from exc
    return _require_source_revision(value, "runtime")


def _require_sha(value: Any, component: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _SHA_RE.fullmatch(normalized):
        raise RuntimeContractError(f"{_safe_code(component, 'component')}_sha_invalid")
    return normalized


def _require_source_revision(value: Any, component: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _SOURCE_REVISION_RE.fullmatch(normalized):
        raise RuntimeContractError(
            f"{_safe_code(component, 'component')}_source_revision_invalid"
        )
    return normalized


def _require_image_id(value: Any, component: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _IMAGE_ID_RE.fullmatch(normalized):
        raise RuntimeContractError(f"{_safe_code(component, 'component')}_image_id_invalid")
    return normalized


def _require_container_name(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _CONTAINER_RE.fullmatch(normalized):
        raise RuntimeContractError("container_name_invalid")
    return normalized


def _baseline_version(baseline: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_value(dict(baseline)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "m2-guardrail-v1:" + hashlib.sha256(encoded).hexdigest()[:24]


def _job_key(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _utc_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=lambda item: str(item))
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeContractError("runtime_state_invalid") from exc
    return payload if isinstance(payload, dict) else None


def _safe_code(value: Any, default: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().casefold()).strip("_")
    return (normalized or default)[:120]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and arm the M2 runtime guardrails")
    subparsers = parser.add_subparsers(dest="command", required=True)

    arm = subparsers.add_parser("arm", help="Verify the running Docker stack and initialize 0/20")
    arm.add_argument("--docker", default="docker")
    arm.add_argument("--worker-container", default="anime-subtitle-worker")
    arm.add_argument("--webui-container", default="anime-subtitle-worker-webui")
    arm.add_argument("--expected-worker-commit-sha", required=True)
    arm.add_argument("--expected-webui-commit-sha", required=True)
    arm.add_argument("--worker-repo", default=".")
    arm.add_argument("--webui-repo", default="../anime-subtitle-worker-webui")
    arm.add_argument("--fault-summary", required=True)
    arm.add_argument("--worker-config", default="/app/config.yaml")
    arm.add_argument("--worker-source-revision-file", default="/app/.source-revision")
    arm.add_argument("--webui-source-revision-file", default="/app/.source-revision")
    arm.add_argument("--state-path", default="")

    probe = subparsers.add_parser("probe", help=argparse.SUPPRESS)
    probe.add_argument("--config", required=True)
    probe.add_argument("--source-revision-file", required=True)
    probe.add_argument("--fault-summary", required=True)
    probe.add_argument("--fault-not-before-epoch", required=True, type=float)

    initialize = subparsers.add_parser("initialize", help=argparse.SUPPRESS)
    initialize.add_argument("--config", required=True)
    initialize.add_argument("--source-revision-file", required=True)
    initialize.add_argument("--state-path", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "arm":
            result = arm_runtime_on_host(
                docker_binary=args.docker,
                worker_container=args.worker_container,
                webui_container=args.webui_container,
                expected_worker_commit_sha=args.expected_worker_commit_sha,
                expected_webui_commit_sha=args.expected_webui_commit_sha,
                fault_summary_path=args.fault_summary,
                worker_repo=args.worker_repo,
                webui_repo=args.webui_repo,
                worker_config_path=args.worker_config,
                worker_source_revision_file=args.worker_source_revision_file,
                webui_source_revision_file=args.webui_source_revision_file,
                runtime_state_path_override=args.state_path,
            )
        elif args.command == "probe":
            result = probe_local_runtime(
                args.config,
                source_revision_file=args.source_revision_file,
                fault_summary_path=args.fault_summary,
                fault_summary_not_before_epoch=args.fault_not_before_epoch,
            )
        else:
            from config import load_config

            config = load_config(args.config)
            raw = sys.stdin.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise RuntimeContractError("runtime_evidence_too_large")
            try:
                evidence = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeContractError("runtime_evidence_invalid") from exc
            if not isinstance(evidence, dict):
                raise RuntimeContractError("runtime_evidence_invalid")
            state = initialize_gate(
                config,
                evidence,
                source_revision_file=args.source_revision_file,
                state_path_override=args.state_path or None,
            )
            result = _public_summary(state)
    except RuntimeContractError as exc:
        print(
            json.dumps(
                {"status": exc.status, "reason_code": exc.reason_code},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
