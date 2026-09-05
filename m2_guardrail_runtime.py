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
import uuid

from safe_files import atomic_write_text, sha256_file


RUNTIME_SCHEMA_VERSION = 1
RUNTIME_CONTRACT = "m2-guardrail-runtime-v1"
BREAKER_RECOVERY_CONTRACT = "m2-controlled-breaker-recovery-v1"
PLANNED_CHANGE_CONTRACT = "m2-planned-runtime-change-v1"
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
_CONTAINER_ID_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
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


def _set_durable_claim_control(
    config: Any,
    *,
    paused: bool,
    requested_by: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Atomically persist the operator claim latch and verify the written state."""

    timestamp = time.time() if now is None else float(now)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise RuntimeContractError("claim_control_timestamp_invalid")
    payload = {
        "paused": bool(paused),
        "requested_at": _utc_timestamp(timestamp),
        "updated_at": timestamp,
        "requested_by": _safe_code(requested_by, "m2_guardrail_runtime"),
    }
    path = Path(str(config.work_path)) / "ai_control.json"
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
    )
    written = _read_json(path)
    if (
        not isinstance(written, Mapping)
        or written.get("paused") is not bool(paused)
        or float(written.get("updated_at") or 0) != timestamp
    ):
        raise RuntimeContractError("claim_control_persistence_failed")
    if paused:
        from m2_production_observation import require_durable_claim_pause

        durable = require_durable_claim_pause(config)
        return {**durable, "requested_by": str(payload["requested_by"])}
    return {
        "paused": False,
        "updated_at": timestamp,
        "requested_by": str(payload["requested_by"]),
    }


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


def worker_runtime_instance_fingerprint(config: Any) -> dict[str, str]:
    """Return a path-free identity for the live Worker container instance.

    Docker keeps the writable overlay across ``docker restart`` but allocates a
    different upper layer when a container is recreated.  The hostname is
    retained as a second, independently host-attested identity.  Tests may
    point ``m2_guardrail_container_identity_file`` at an isolated fixture.
    """

    identity_path = Path(
        str(
            getattr(
                config,
                "m2_guardrail_container_identity_file",
                "/etc/hostname",
            )
        )
    )
    try:
        identity = identity_path.read_text(encoding="utf-8").strip()
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeContractError("worker_container_identity_unavailable") from exc
    if not _CONTAINER_RE.fullmatch(identity):
        raise RuntimeContractError("worker_container_identity_invalid")

    rootfs_token = ""
    mountinfo_path = Path("/proc/self/mountinfo")
    try:
        if mountinfo_path.is_file() and mountinfo_path.stat().st_size <= 2 * 1024 * 1024:
            for line in mountinfo_path.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) < 10 or fields[4] != "/" or " - " not in line:
                    continue
                match = re.search(r"(?:^|,)upperdir=([^,\s]+)", line.split(" - ", 1)[1])
                if match:
                    rootfs_token = "upperdir:" + match.group(1)
                break
    except (OSError, TypeError, ValueError):
        rootfs_token = ""
    canonical = rootfs_token or ("container-identity:" + identity)
    return {
        "container_identity": identity,
        "runtime_instance_fingerprint": "sha256:"
        + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


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


def compute_worker_runtime_code_revision(app_root: str | Path) -> str:
    """Hash the Worker source files actually present in the app image.

    The build marker describes the intended source tree, but it remains a
    writable file in a normal Docker overlay.  This independent digest is
    recomputed from deployed files and is intentionally never cached.
    """

    root = Path(app_root).resolve()
    entries: list[tuple[Path, str]] = []
    requirements = root / "requirements.txt"
    if requirements.is_file():
        entries.append((requirements, "requirements.txt"))
    entries.extend(
        (path, f"./{path.name}")
        for path in sorted(root.glob("*.py"), key=lambda item: item.name)
        if path.is_file()
    )
    acceptance = root / "acceptance"
    if acceptance.is_dir():
        acceptance_files = (
            path
            for path in acceptance.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.casefold() not in {".pyc", ".pyo", ".pyd"}
        )
        entries.extend(
            (path, path.relative_to(root).as_posix())
            for path in sorted(
                acceptance_files,
                key=lambda item: item.relative_to(root).as_posix(),
            )
        )
    return _manifest_revision(entries)


def worker_runtime_code_revision(config: Any) -> str:
    """Return a fresh digest of the live Worker application files."""

    app_root = getattr(config, "m2_guardrail_runtime_app_root", "/app")
    try:
        return compute_worker_runtime_code_revision(app_root)
    except RuntimeContractError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeContractError("worker_runtime_code_revision_unavailable") from exc


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
    # A tripped breaker stops claims, but terminal observation for work that
    # was already running must still validate the immutable runtime baseline.
    # Continue the read-only checks so a simultaneous code/config drift is not
    # hidden behind the breaker latch.
    if local_status not in {"ARMED", "TRIPPED"}:
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
        _require_container_id(baseline.get("worker_container_id"), "worker")
        expected_container_identity = _require_container_identity(
            baseline.get("worker_container_identity"), "worker"
        )
        live_instance = worker_runtime_instance_fingerprint(config)
        if live_instance["container_identity"] != expected_container_identity:
            raise RuntimeContractError("live_worker_container_identity_mismatch")
        if (
            live_instance["runtime_instance_fingerprint"]
            != str(baseline.get("worker_runtime_instance_fingerprint") or "")
        ):
            raise RuntimeContractError("live_worker_container_instance_mismatch")
        expected_source_revision = _require_source_revision(
            baseline.get("worker_source_revision"),
            "worker",
        )
        expected_runtime_code_revision = _require_source_revision(
            baseline.get("worker_runtime_code_revision"),
            "worker_runtime_code",
        )
        _require_source_revision(baseline.get("webui_source_revision"), "webui")
        if _read_source_revision(marker_path) != expected_source_revision:
            raise RuntimeContractError("live_worker_source_revision_mismatch")
        if worker_runtime_code_revision(config) != expected_runtime_code_revision:
            raise RuntimeContractError("live_worker_runtime_code_revision_mismatch")
        if baseline.get("configuration_fingerprint") != configuration_fingerprint(config):
            raise RuntimeContractError("live_configuration_fingerprint_mismatch")
        from m2_observation_store import ELIGIBILITY_POLICY_VERSION

        if (
            baseline.get("decision_schema_version") != decision["schema_version"]
            or baseline.get("decision_version") != decision["version"]
            or baseline.get("decision_contract") != decision["contract"]
        ):
            raise RuntimeContractError("live_decision_schema_mismatch")
        if (
            baseline.get("eligibility_policy_version")
            != ELIGIBILITY_POLICY_VERSION
            or gate.get("eligibility_policy_version")
            != ELIGIBILITY_POLICY_VERSION
            or not str(gate.get("gate_id") or "")
        ):
            raise RuntimeContractError("live_eligibility_policy_mismatch")
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
    if local_status == "TRIPPED":
        return {
            "status": "TRIPPED",
            "reason_code": "circuit_breaker_tripped",
            "state": state,
        }
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
    live_runtime_code_revision = worker_runtime_code_revision(config)
    runtime_instance = worker_runtime_instance_fingerprint(config)
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
        "worker_runtime_code_revision": live_runtime_code_revision,
        "worker_container_identity": runtime_instance["container_identity"],
        "worker_runtime_instance_fingerprint": runtime_instance[
            "runtime_instance_fingerprint"
        ],
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
    expected_runtime_code_revision = _require_source_revision(
        evidence.get("worker_runtime_code_revision"),
        "worker_runtime_code",
    )
    webui_source_revision = _require_source_revision(
        evidence.get("webui_source_revision"),
        "webui",
    )
    if worker_source_revision != expected_worker_source_revision:
        raise RuntimeContractError("worker_source_revision_changed_during_arm")
    live_runtime_code_revision = worker_runtime_code_revision(config)
    if live_runtime_code_revision != expected_runtime_code_revision:
        raise RuntimeContractError("worker_runtime_code_revision_changed_during_arm")
    worker_image_id = _require_image_id(evidence.get("worker_image_id"), "worker")
    webui_image_id = _require_image_id(evidence.get("webui_image_id"), "webui")
    worker_container_id = _require_container_id(
        evidence.get("worker_container_id"), "worker"
    )
    worker_container_identity = _require_container_identity(
        evidence.get("worker_container_identity"), "worker"
    )
    live_instance = worker_runtime_instance_fingerprint(config)
    if live_instance["container_identity"] != worker_container_identity:
        raise RuntimeContractError("worker_container_identity_changed_during_arm")
    if (
        str(evidence.get("worker_runtime_instance_fingerprint") or "")
        != live_instance["runtime_instance_fingerprint"]
    ):
        raise RuntimeContractError("worker_runtime_instance_changed_during_arm")
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

    from m2_observation_store import ELIGIBILITY_POLICY_VERSION

    baseline = {
        "worker_commit_sha": worker_commit_sha,
        "webui_commit_sha": webui_commit_sha,
        "worker_source_revision": expected_worker_source_revision,
        "worker_runtime_code_revision": expected_runtime_code_revision,
        "webui_source_revision": webui_source_revision,
        "worker_image_id": worker_image_id,
        "webui_image_id": webui_image_id,
        "worker_container_id": worker_container_id,
        "worker_container_identity": worker_container_identity,
        "worker_runtime_instance_fingerprint": live_instance[
            "runtime_instance_fingerprint"
        ],
        "configuration_fingerprint": fingerprint,
        "decision_schema_version": decision["schema_version"],
        "decision_version": decision["version"],
        "decision_contract": decision["contract"],
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
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

            observation_gate = initialize_observation_gate(config, existing, now=timestamp)
            existing_gate["gate_id"] = str(observation_gate["gate_id"])
            existing_gate["eligibility_policy_version"] = ELIGIBILITY_POLICY_VERSION
            existing["gate_start_at"] = str(observation_gate["gate_start_at"])
            existing["gate_start_epoch"] = float(observation_gate["gate_start_epoch"])
            atomic_write_text(
                target,
                json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
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
            "gate_id": "",
            "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
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

    observation_gate = initialize_observation_gate(config, state, now=timestamp)
    state["gate"]["gate_id"] = str(observation_gate["gate_id"])
    state["gate_start_at"] = str(observation_gate["gate_start_at"])
    state["gate_start_epoch"] = float(observation_gate["gate_start_epoch"])
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
    from m2_observation_store import ELIGIBILITY_POLICY_VERSION

    baseline = state.get("baseline")
    gate = state.get("gate")
    if (
        not isinstance(baseline, Mapping)
        or not isinstance(gate, Mapping)
        or baseline.get("eligibility_policy_version") != ELIGIBILITY_POLICY_VERSION
        or gate.get("eligibility_policy_version") != ELIGIBILITY_POLICY_VERSION
    ):
        return False, "eligibility_policy_mismatch"
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


def _durable_recovery_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    """Hash durable identities that recovery is forbidden to change."""

    def digest_rows(query: str) -> tuple[int, str]:
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(query):
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
            count += 1
        return count, "sha256:" + digest.hexdigest()

    queue_count, queue_identity = digest_rows(
        "SELECT path,mtime_ns FROM ai_candidate_queue ORDER BY path COLLATE NOCASE"
    )
    checkpoint_count, checkpoint_identity = digest_rows(
        """
        SELECT stage_attempt_id,job_id,stage,checkpoint_json,checkpoint_sha256
        FROM pipeline_stage_attempts
        WHERE checkpoint_sha256<>'' OR checkpoint_json<>'{}'
        ORDER BY stage_attempt_id
        """
    )
    output_count, output_identity = digest_rows(
        """
        SELECT obligation_id,state,manifest_path,manifest_sha256,verification_json
        FROM ai_delivery_obligations
        WHERE manifest_path<>'' OR manifest_sha256<>'' OR state='verified'
        ORDER BY obligation_id
        """
    )
    queue_states = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(1) FROM ai_candidate_queue GROUP BY status"
        ).fetchall()
    }
    return {
        "queue_count": queue_count,
        "queue_identity_sha256": queue_identity,
        "queue_status_counts": queue_states,
        "checkpoint_count": checkpoint_count,
        "checkpoint_identity_sha256": checkpoint_identity,
        "formal_output_record_count": output_count,
        "formal_output_identity_sha256": output_identity,
    }


def _planned_change_snapshot(connection: sqlite3.Connection, gate_id: str) -> dict[str, Any]:
    """Freeze durable work and append-only activity, never alter queue policy."""
    snapshot = _durable_recovery_snapshot(connection)
    activity = {}
    for table, timestamp in (
        ("ai_delivery_attempts", "updated_at"),
        ("pipeline_stage_attempts", "updated_at"),
        ("pipeline_stage_events", "created_at"),
        ("pipeline_job_transitions", "created_at"),
        ("ai_stage_events", "created_at"),
        ("m2_observation_result_events", "created_at"),
        ("m2_observation_supplemental", "updated_at"),
    ):
        activity[table] = list(connection.execute(
            f"SELECT COUNT(*),COALESCE(MAX({timestamp}),0) FROM {table}"
        ).fetchone())
    members = [list(row) for row in connection.execute(
        "SELECT * FROM m2_observation_gate_jobs WHERE gate_id=? ORDER BY job_id", (gate_id,)
    )]
    snapshot["activity"] = activity
    retained_running = {}
    for table, status, identity in (
        ("ai_delivery_attempts", "running", "attempt_id"),
        ("pipeline_stage_attempts", "RUNNING", "stage_attempt_id"),
        ("ai_job_state", "running", "path"),
    ):
        rows = [list(row) for row in connection.execute(
            f"SELECT * FROM {table} WHERE status=? ORDER BY {identity}", (status,)
        )]
        retained_running[table] = {"count": len(rows), "sha256": "sha256:" + hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    snapshot["retained_running"] = retained_running
    snapshot["members_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot["member_count"] = len(members)
    return snapshot


def _require_planned_change_idle(connection: sqlite3.Connection, config: Any, now: float) -> None:
    # Retained historical attempts use the same stale cutoff as recovery;
    # they are frozen in the receipt, not edited or treated as fresh claims.
    cutoff = now - max(60, int(getattr(config, "m2_recovery_stale_running_seconds", 21600) or 21600))
    if connection.execute("SELECT 1 FROM ai_candidate_queue WHERE status='running' LIMIT 1").fetchone():
        raise RuntimeContractError("planned_change_work_not_idle")
    for table, status, heartbeat in (
        ("ai_delivery_attempts", "running", "updated_at"),
        ("pipeline_stage_attempts", "RUNNING", "heartbeat_at"),
        ("ai_job_state", "running", "updated_at"),
    ):
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE status=? AND ({heartbeat}>? OR {heartbeat} IS NULL OR {heartbeat}<=0) LIMIT 1",
            (status, cutoff),
        ).fetchone():
            raise RuntimeContractError("planned_change_work_not_idle")


def prepare_runtime_change(
    config: Any, *, expected_old_gate_id: str, expected_new_worker_sha: str,
    receipt_id: str, source_revision_file: str | Path = "/app/.source-revision",
    state_path_override: str | Path | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Record one planned deployment only after the existing safe-idle pause."""
    from m2_observation_store import active_gate, validate_active_runtime
    from m2_production_observation import circuit_breaker_state_path, require_durable_claim_pause
    from scan_state import ScanStateStore

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", str(receipt_id)):
        raise RuntimeContractError("planned_change_receipt_id_invalid")
    timestamp = time.time() if now is None else float(now)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise RuntimeContractError("planned_change_timestamp_invalid")
    wanted = _require_sha(expected_new_worker_sha, "planned_worker")
    pause = require_durable_claim_pause(config)
    status = runtime_guardrail_status(config, source_revision_file=source_revision_file,
                                      state_path_override=state_path_override)
    if status.get("status") != "ARMED":
        raise RuntimeContractError("planned_change_runtime_not_armed")
    prior = status["state"]
    if (prior["gate"]["gate_id"] != expected_old_gate_id
        or prior["baseline"]["worker_commit_sha"] == wanted):
        raise RuntimeContractError("planned_change_gate_or_new_sha_invalid")
    breaker = _read_json(circuit_breaker_state_path(config)) or {"tripped": False, "status": "ARMED"}
    if breaker.get("tripped") is not False:
        raise RuntimeContractError("planned_change_breaker_not_clear")
    path = Path(str(getattr(config, "log_path", config.work_path))) / f"m2-planned-runtime-change-{receipt_id}.json"
    store = ScanStateStore.from_config(config)
    try:
        connection = store.observation_connection
        connection.execute("BEGIN IMMEDIATE")
        _require_planned_change_idle(connection, config, timestamp)
        validate_active_runtime(connection, prior)
        gate = active_gate(connection)
        if not gate or gate["gate_id"] != expected_old_gate_id:
            raise RuntimeContractError("planned_change_active_gate_mismatch")
        snapshot = _planned_change_snapshot(connection, expected_old_gate_id)
        existing = _read_json(path)
        if path.exists():
            if (not isinstance(existing, Mapping) or existing.get("contract") != PLANNED_CHANGE_CONTRACT
                or existing.get("runtime") != prior or existing.get("snapshot") != snapshot
                or existing.get("expected_new_worker_sha") != wanted or existing.get("breaker") != breaker):
                raise RuntimeContractError("planned_change_receipt_conflict")
        else:
            receipt = {"contract": PLANNED_CHANGE_CONTRACT, "receipt_id": receipt_id,
                       "prepared_at_epoch": timestamp, "expected_new_worker_sha": wanted,
                       "runtime": prior, "gate": gate, "breaker": breaker,
                       "snapshot": snapshot, "durable_claim_pause": pause}
            atomic_write_text(path, json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        connection.commit()
    except BaseException:
        store.rollback()
        raise
    finally:
        store.close()
    return {"status": "PREPARED", "old_gate_id": expected_old_gate_id,
            "expected_new_worker_sha": wanted, "receipt_path": str(path),
            "receipt_sha256": "sha256:" + sha256_file(path), "claims_paused": True}


def _planned_change_receipt(config: Any, root_cause: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(root_cause.get("planned_change_receipt") or "")).resolve()
    log_root = Path(str(getattr(config, "log_path", config.work_path))).resolve()
    if (not path.is_relative_to(log_root) or not path.is_file()
        or not path.name.startswith("m2-planned-runtime-change-")
        or not 0 < path.stat().st_size <= 256 * 1024
        or "sha256:" + sha256_file(path) != root_cause.get("planned_change_receipt_sha256")):
        raise RuntimeContractError("planned_change_receipt_invalid")
    receipt = _read_json(path)
    if (not isinstance(receipt, dict) or receipt.get("contract") != PLANNED_CHANGE_CONTRACT
        or not isinstance(receipt.get("runtime"), dict) or not isinstance(receipt.get("gate"), dict)
        or receipt["gate"].get("gate_id") != root_cause.get("expected_old_gate_id")
        or receipt["runtime"].get("status") != "ARMED" or receipt["gate"].get("status") != "ACTIVE"
        or not isinstance(receipt.get("breaker"), dict) or receipt["breaker"].get("tripped") is not False):
        raise RuntimeContractError("planned_change_receipt_gate_mismatch")
    return receipt


def _planned_change_incident(
    connection: sqlite3.Connection, *, config: Any, evidence: Mapping[str, Any],
    breaker: Mapping[str, Any], now: float,
) -> dict[str, Any]:
    from m2_observation_store import gate_by_id, active_gate, INVALIDATED_RUNTIME
    from m2_production_observation import require_durable_claim_pause

    root_cause = evidence["root_cause"]
    receipt = _planned_change_receipt(config, root_cause)
    require_durable_claim_pause(config)
    _require_planned_change_idle(connection, config, now)
    old = receipt["runtime"]["baseline"]
    if evidence["worker_commit_sha"] != receipt["expected_new_worker_sha"]:
        raise RuntimeContractError("planned_change_new_sha_mismatch")
    for key in ("worker_commit_sha", "worker_source_revision", "worker_runtime_code_revision",
                "worker_container_id", "worker_container_identity", "worker_image_id",
                "worker_runtime_instance_fingerprint"):
        if evidence.get(key) == old.get(key):
            raise RuntimeContractError("planned_change_new_runtime_not_proven")
    for key in ("webui_commit_sha", "webui_source_revision", "configuration_fingerprint"):
        if evidence.get(key) != old.get(key):
            raise RuntimeContractError("planned_change_frozen_policy_changed")
    if evidence.get("decision") != {
        "schema_version": old["decision_schema_version"], "version": old["decision_version"],
        "contract": old["decision_contract"],
    }:
        raise RuntimeContractError("planned_change_frozen_policy_changed")
    prepared = float(receipt["prepared_at_epoch"])
    fault = evidence["fault_results"]
    if not prepared < float(fault.get("container_started_at_epoch") or 0) <= float(fault.get("started_at_epoch") or 0) <= float(fault.get("finished_at_epoch") or 0) <= now:
        raise RuntimeContractError("planned_change_fault_evidence_not_fresh")
    if _planned_change_snapshot(connection, receipt["gate"]["gate_id"]) != receipt["snapshot"]:
        raise RuntimeContractError("planned_change_new_work_or_evidence_changed")
    gate = gate_by_id(connection, receipt["gate"]["gate_id"])
    if not gate or active_gate(connection) is not None or gate.get("status") != INVALIDATED_RUNTIME:
        raise RuntimeContractError("planned_change_old_gate_not_invalidated")
    mutable = {"status", "invalidated_at", "invalidation_reason", "invalidation_evidence_json", "updated_at"}
    if any(gate.get(key) != value for key, value in receipt["gate"].items() if key not in mutable):
        raise RuntimeContractError("planned_change_old_gate_evidence_changed")
    latest = breaker.get("latest_trip")
    previous_reasons = receipt["breaker"].get("reasons") or []
    if (not isinstance(latest, Mapping) or latest.get("reason_code") != "runtime_change"
        or latest.get("evidence") != {"stage": "runtime_validation", "error_code": "live_worker_container_identity_mismatch"}
        or breaker.get("reasons") != [*previous_reasons, latest]
        or latest.get("observed_at") != breaker.get("updated_at")):
        raise RuntimeContractError("planned_change_unexpected_breaker")
    try:
        invalidation = json.loads(gate["invalidation_evidence_json"])
        expected, actual = invalidation["expected"], invalidation["actual"]
        if (not prepared < float(fault["container_started_at_epoch"]) <= float(gate["invalidated_at"]) <= float(latest["observed_at"]) <= now
            or invalidation.get("reason_code") != "live_worker_container_identity_mismatch"
            or actual.get("reason_code") != "live_worker_container_identity_mismatch"
            or actual.get("container_identity") != evidence["worker_container_identity"]
            or actual.get("runtime_instance_fingerprint") != evidence["worker_runtime_instance_fingerprint"]
            or any(actual.get(key) != evidence.get(key) for key in ("worker_source_revision", "worker_runtime_code_revision", "configuration_fingerprint"))
            or any(expected.get(key) != receipt["gate"].get(key) for key in (
                "baseline_version", "worker_sha", "webui_sha", "container_image_id", "worker_container_id",
                "configuration_fingerprint", "decision_schema_version", "eligibility_policy_version"))):
            raise ValueError("mismatched deployment evidence")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeContractError("planned_change_runtime_evidence_mismatch") from exc
    return {"planned_snapshot_sha256": "sha256:" + hashlib.sha256(
                json.dumps(receipt["snapshot"], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "planned_change_receipt_sha256": root_cause["planned_change_receipt_sha256"],
            "prepared_at_epoch": prepared, "old_gate_id": gate["gate_id"],
            "new_work_observed": False, "member_count": receipt["snapshot"]["member_count"]}


def _breaker_incident_sources(
    connection: sqlite3.Connection,
    *,
    stage: str,
    error_code: str,
    tripped_at: float,
    threshold: int,
) -> dict[str, Any]:
    """Verify the distinct jobs behind a repeated-failure trip without a media walk."""

    rows = connection.execute(
        """
        SELECT a.attempt_id,a.obligation_id,a.attempt_number,a.finished_at,
               o.canonical_path,o.media_fingerprint,o.media_size,o.media_mtime_ns
        FROM ai_delivery_attempts a
        JOIN ai_delivery_obligations o ON o.obligation_id=a.obligation_id
        WHERE a.stage=? AND a.error_code=?
          AND a.status IN ('failed','retryable_failure','review_required')
          AND a.finished_at>0 AND a.finished_at<=?
          AND a.finished_at>=?
        ORDER BY a.finished_at DESC,a.attempt_id DESC
        LIMIT 200
        """,
        (str(stage), str(error_code), float(tripped_at) + 5.0, float(tripped_at) - 3600.0),
    ).fetchall()
    selected: list[tuple[Any, ...]] = []
    obligations: set[str] = set()
    for row in rows:
        obligation_id = str(row[1] or "")
        if obligation_id in obligations:
            continue
        selected.append(row)
        obligations.add(obligation_id)
        if len(selected) >= max(1, int(threshold)):
            break
    if len(selected) < max(1, int(threshold)):
        raise RuntimeContractError("breaker_distinct_job_evidence_incomplete")
    source_records: list[dict[str, Any]] = []
    for row in reversed(selected):
        path = Path(str(row[4]))
        try:
            stat = path.stat()
        except OSError as exc:
            raise RuntimeContractError("breaker_source_identity_unavailable") from exc
        if int(stat.st_size) != int(row[6] or 0) or int(stat.st_mtime_ns) != int(row[7] or 0):
            raise RuntimeContractError("breaker_source_identity_changed")
        source_records.append(
            {
                "attempt_key": _job_key(row[0]),
                "obligation_key": _job_key(row[1]),
                "attempt_number": int(row[2] or 0),
                "finished_at_epoch": float(row[3] or 0),
                "path_key": _job_key(str(path)),
                "media_fingerprint": str(row[5] or ""),
                "media_size": int(row[6] or 0),
                "media_mtime_ns": int(row[7] or 0),
            }
        )
    encoded = json.dumps(source_records, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "affected_stage": str(stage),
        "error_code": str(error_code),
        "distinct_job_count": len(source_records),
        "same_job_replay_count": max(0, len(rows) - len(obligations)),
        "first_failure_at_epoch": min(item["finished_at_epoch"] for item in source_records),
        "last_failure_at_epoch": max(item["finished_at_epoch"] for item in source_records),
        "source_identity_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "sources": source_records,
    }


def _verified_deployment_handoff(
    connection: sqlite3.Connection, *, config: Any, root_cause: Mapping[str, Any],
    breaker: Mapping[str, Any], gate: Mapping[str, Any], origin_epoch: float,
) -> dict[str, Any]:
    """Verify one recorded, attested deploy drift without hiding later faults."""
    proof = root_cause.get("expected_deployment_handoff")
    if not isinstance(proof, Mapping) or config is None:
        raise RuntimeContractError("collision_latest_trip_mismatch")
    latest = breaker.get("latest_trip")
    if (not isinstance(latest, Mapping) or latest.get("reason_code") != "runtime_change"
        or latest.get("evidence") != {"stage": "runtime_validation", "error_code": "live_worker_container_identity_mismatch"}):
        raise RuntimeContractError("handoff_unexpected_runtime_change")
    latest_text = json.dumps(dict(latest), sort_keys=True, separators=(",", ":"))
    invalidation_text = str(gate.get("invalidation_evidence_json") or "")
    if (gate.get("status") != "INVALIDATED_BY_RUNTIME_CHANGE"
        or gate.get("invalidation_reason") != "INVALIDATED_BY_RUNTIME_CHANGE"
        or breaker.get("updated_at") != proof.get("expected_breaker_updated_at")
        or latest.get("observed_at") != breaker.get("updated_at")
        or "sha256:" + hashlib.sha256(latest_text.encode()).hexdigest() != proof.get("latest_trip_sha256")
        or "sha256:" + hashlib.sha256(invalidation_text.encode()).hexdigest() != proof.get("invalidation_evidence_sha256")):
        raise RuntimeContractError("handoff_durable_evidence_mismatch")
    reasons = breaker.get("reasons")
    if not isinstance(reasons, list) or any(not isinstance(item, Mapping) for item in reasons):
        raise RuntimeContractError("handoff_unexpected_later_incident")
    try:
        later = [item for item in reasons if float(item.get("observed_at") or 0) > origin_epoch]
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError("handoff_unexpected_later_incident") from exc
    if later != [latest]:
        raise RuntimeContractError("handoff_unexpected_later_incident")
    if connection.execute(
        "SELECT 1 FROM m2_observation_result_events WHERE created_at>? LIMIT 1",
        (origin_epoch,),
    ).fetchone() or connection.execute(
        "SELECT 1 FROM ai_delivery_attempts WHERE started_at>? LIMIT 1",
        (origin_epoch,),
    ).fetchone() or connection.execute(
        "SELECT 1 FROM ai_delivery_attempts WHERE finished_at>? LIMIT 1",
        (origin_epoch,),
    ).fetchone() or connection.execute(
        "SELECT 1 FROM m2_observation_gate_jobs WHERE gate_id=? AND claimed_at>? LIMIT 1",
        (gate["gate_id"], origin_epoch),
    ).fetchone():
        raise RuntimeContractError("handoff_new_work_observed")
    log_root = Path(str(getattr(config, "log_path", config.work_path))).resolve()
    receipt_path = Path(str(proof.get("first_attestation_path") or "")).resolve()
    if (not receipt_path.is_relative_to(log_root) or not receipt_path.is_file()
        or not 0 < receipt_path.stat().st_size <= 64 * 1024
        or "sha256:" + sha256_file(receipt_path) != proof.get("first_attestation_sha256")):
        raise RuntimeContractError("handoff_attestation_receipt_invalid")
    receipt = _read_json(receipt_path)
    if not isinstance(receipt, Mapping) or not isinstance(receipt.get("identity"), Mapping):
        raise RuntimeContractError("handoff_attestation_receipt_invalid")
    original = receipt.get("root_cause_evidence")
    if not isinstance(original, Mapping) or any(original.get(key) != root_cause.get(key) for key in (
        "mode", "breaker_reason", "affected_stage", "failure_code", "expected_old_gate_id",
        "expected_breaker_updated_at", "expected_counter_updated_at", "members", "lane_quality_pause",
    )):
        raise RuntimeContractError("handoff_original_incident_mismatch")
    identity = receipt["identity"]
    container_id = _require_container_id(identity.get("container_id"), "handoff")
    _require_image_id(identity.get("image_id"), "handoff")
    source_revision = _require_source_revision(identity.get("source_revision"), "handoff")
    if _require_sha(receipt.get("worker_sha"), "handoff_worker") == gate.get("worker_sha"):
        raise RuntimeContractError("handoff_new_runtime_not_proven")
    try:
        invalidation = json.loads(invalidation_text)
        expected, actual = invalidation["expected"], invalidation["actual"]
        started_at = datetime.fromisoformat(str(identity["started_at"]).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            raise ValueError("deployment timestamp needs timezone")
        started = started_at.timestamp()
        created = float(receipt["created_at"])
        invalidated = float(gate["invalidated_at"])
        tripped = float(latest["observed_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeContractError("handoff_recorded_runtime_mismatch") from exc
    if (not all(math.isfinite(value) for value in (started, created, invalidated, tripped))
        or not origin_epoch < started <= invalidated <= tripped <= created
        or tripped - invalidated > 5
        or invalidation.get("reason_code") != "live_worker_container_identity_mismatch"
        or actual.get("reason_code") != "live_worker_container_identity_mismatch"
        or actual.get("container_identity") != container_id[:12]
        or actual.get("worker_source_revision") != source_revision
        or any(expected.get(key) != gate.get(column) for key, column in (
            ("baseline_version", "baseline_version"), ("worker_sha", "worker_sha"),
            ("container_image_id", "container_image_id"), ("worker_container_id", "worker_container_id"),
            ("configuration_fingerprint", "configuration_fingerprint"), ("decision_schema_version", "decision_schema_version"),
        ))
        or actual.get("configuration_fingerprint") != configuration_fingerprint(config)
        or actual.get("configuration_fingerprint") != gate.get("configuration_fingerprint")
        or actual.get("decision_schema_version") != _decision_descriptor(config)["schema_version"]
        or actual.get("decision_version") != _decision_descriptor(config)["version"]):
        raise RuntimeContractError("handoff_recorded_runtime_mismatch")
    return {"first_attestation_sha256": proof["first_attestation_sha256"],
            "first_worker_sha": receipt["worker_sha"], "first_container_id": container_id,
            "first_source_revision": source_revision,
            "latest_trip_observed_at_epoch": tripped,
            "invalidation_evidence_sha256": proof["invalidation_evidence_sha256"],
            "new_work_observed": False}


def _generic_collision_incident(
    connection: sqlite3.Connection,
    *,
    root_cause: Mapping[str, Any],
    breaker: Mapping[str, Any],
    threshold: int,
    validate_counters: bool = True,
    config: Any = None,
) -> dict[str, Any]:
    """Bind this narrow repair to immutable current events, never legacy reasons."""
    from m2_observation_store import latest_gate, meta_state
    from m2_production_observation import _outcome_failure_signature
    from m2_production_recovery import normalize_failure_signature

    gate_id = str(root_cause.get("expected_old_gate_id") or "")
    members = root_cause.get("members")
    gate = latest_gate(connection)
    if not gate_id or not gate or gate.get("gate_id") != gate_id:
        raise RuntimeContractError("collision_current_gate_mismatch")
    if not isinstance(members, list) or len(members) != threshold or not 2 <= threshold <= 20:
        raise RuntimeContractError("collision_member_evidence_incomplete")
    try:
        trip_epoch = float(root_cause.get("expected_breaker_updated_at"))
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError("collision_trip_timestamp_invalid") from exc
    if not math.isfinite(trip_epoch) or trip_epoch < float(gate["gate_start_epoch"]):
        raise RuntimeContractError("collision_latest_trip_mismatch")
    handoff = None
    if trip_epoch != breaker.get("updated_at"):
        handoff = _verified_deployment_handoff(
            connection, config=config, root_cause=root_cause, breaker=breaker,
            gate=gate, origin_epoch=trip_epoch,
        )
    sources: list[dict[str, Any]] = []
    clusters: dict[str, int] = {}
    member_keys: list[str] = []
    prior_event_epoch = float(gate["gate_start_epoch"])
    for index, supplied in enumerate(members, 1):
        if not isinstance(supplied, Mapping):
            raise RuntimeContractError("collision_member_evidence_incomplete")
        attempt_id = str(supplied.get("attempt_id") or "")
        claim_hash = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
        cursor = connection.execute(
            "SELECT a.attempt_id,a.obligation_id,a.attempt_number,a.finished_at,a.status,"
            "a.stage,a.error_code,a.detail,o.canonical_path,o.media_fingerprint,"
            "o.media_size,o.media_mtime_ns FROM ai_delivery_attempts a "
            "JOIN ai_delivery_obligations o ON o.obligation_id=a.obligation_id "
            "WHERE a.attempt_id=?", (attempt_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeContractError("collision_delivery_evidence_missing")
        delivery = dict(zip((column[0] for column in cursor.description), row, strict=True))
        cursor = connection.execute(
            "SELECT * FROM m2_observation_result_events WHERE claim_identity_hash=?",
            (claim_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeContractError("collision_event_evidence_missing")
        event = dict(zip((column[0] for column in cursor.description), row, strict=True))
        detail = str(delivery["detail"] or "")
        detail_digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()
        payload_text = str(event["event_payload_json"])
        job_hash = hashlib.sha256(str(delivery["obligation_id"]).encode("utf-8")).hexdigest()
        if (
            event["gate_id"] != gate_id or event["job_id"] != job_hash
            or event["event_sha256"] != supplied.get("event_sha256")
            or event["event_sha256"] != hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
            or not detail or detail_digest != supplied.get("detail_sha256")
            or delivery["stage"] != "worker" or delivery["error_code"] != "worker_unknown"
            or delivery["status"] not in {"failed", "retryable_failure", "review_required"}
        ):
            raise RuntimeContractError("collision_member_binding_mismatch")
        payload = json.loads(payload_text)
        outcome = payload.get("outcome", {})
        event_breaker = payload.get("breaker", {})
        event_epoch = float(event["created_at"])
        if (
            not prior_event_epoch < event_epoch <= trip_epoch
            or trip_epoch - event_epoch > 3600
            or not 0 <= event_epoch - float(delivery["finished_at"] or 0) <= 30
            or event_breaker.get("identical_failure_streak") != index
            or event_breaker.get("oom_streak") != 0
            or event_breaker.get("tripped") is not (index == threshold)
            or event_breaker.get("reason_code", "") != ("repeated_identical_stage_failure" if index == threshold else "")
            or outcome.get("stage") != "worker" or outcome.get("error_code") != "worker_unknown"
            or outcome.get("failed") is not True
        ):
            raise RuntimeContractError("collision_event_sequence_mismatch")
        prior_event_epoch = event_epoch
        job_key = job_hash[:16]
        if job_key in member_keys:
            raise RuntimeContractError("collision_distinct_members_required")
        member_keys.append(job_key)
        signature = normalize_failure_signature("worker", "worker_unknown", detail)
        if _outcome_failure_signature({**outcome, "_classification_detail": detail}) != signature:
            raise RuntimeContractError("collision_signature_fix_not_loaded")
        clusters[signature] = clusters.get(signature, 0) + 1
        path = Path(str(delivery["canonical_path"]))
        try:
            stat = path.stat()
        except OSError as exc:
            raise RuntimeContractError("breaker_source_identity_unavailable") from exc
        if stat.st_size != delivery["media_size"] or stat.st_mtime_ns != delivery["media_mtime_ns"]:
            raise RuntimeContractError("breaker_source_identity_changed")
        sources.append({
            "attempt_key": _job_key(attempt_id), "obligation_key": job_key,
            "path_key": _job_key(str(path)), "media_fingerprint": delivery["media_fingerprint"],
            "media_size": stat.st_size, "media_mtime_ns": stat.st_mtime_ns,
            "event_sha256": event["event_sha256"], "event_at_epoch": event_epoch,
            "detail_sha256": detail_digest, "normalized_failure_signature": signature,
            "stage": delivery["stage"], "error_code": delivery["error_code"],
            # Original details remain in the exact durable attempt row; logs carry
            # hashes and the FK key, not private media paths from arbitrary errors.
        })
    if trip_epoch - prior_event_epoch > 5.0 or len(clusters) < 2 or max(clusters.values()) >= threshold:
        raise RuntimeContractError("collision_not_proven")
    if validate_counters:
        meta = meta_state(connection)
        counter_epoch = connection.execute(
            "SELECT updated_at FROM m2_observation_meta WHERE key='identical_failure_job_ids'"
        ).fetchone()[0]
        if (meta["identical_failure_signature"] != "worker:worker_unknown"
            or meta["identical_failure_streak"] != threshold
            or meta["identical_failure_job_ids"] != member_keys
            or counter_epoch != root_cause.get("expected_counter_updated_at")
            or meta["oom_streak"] != 0):
            raise RuntimeContractError("collision_current_counters_changed")
    encoded = json.dumps(sources, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result = {"mode": "generic_failure_signature_collision", "gate_id": gate_id,
            "trip_observed_at_epoch": trip_epoch, "distinct_job_count": threshold,
            "normalized_failure_clusters": clusters, "sources": sources,
            "source_identity_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest()}
    if handoff is not None:
        result["deployment_handoff"] = handoff
    return result


def _recover_verified_quality_pause(
    connection: sqlite3.Connection, *, root_cause: Mapping[str, Any],
    old_worker_sha: str, recovery_record_id: str, now: float,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Release only a proven local-QC canary pause; keep its failed job intact."""
    from m2_production_recovery import (
        _current_checkpoint_sha, _meta, _record_event, _set_meta, classify_failure,
    )
    lane = _meta(connection, "lane_state", "EMPTY")
    proof = root_cause.get("lane_quality_pause")
    if lane != "PAUSED":
        if proof:
            raise RuntimeContractError("quality_pause_lane_changed")
        return {"changed": False, "lane_state": lane}
    if not isinstance(proof, Mapping):
        raise RuntimeContractError("quality_pause_evidence_missing")
    if connection.execute("SELECT 1 FROM m2_recovery_jobs WHERE status IN ('DISPATCHED','CLAIMED') LIMIT 1").fetchone():
        raise RuntimeContractError("quality_pause_work_inflight")

    def one(query: str, args: tuple[Any, ...]) -> dict[str, Any]:
        cursor = connection.execute(query, args)
        row = cursor.fetchone()
        if row is None:
            raise RuntimeContractError("quality_pause_evidence_missing")
        return dict(zip((item[0] for item in cursor.description), row, strict=True))

    job = one("SELECT * FROM m2_recovery_jobs WHERE recovery_id=?", (str(proof.get("recovery_id") or ""),))
    dispatch = one("SELECT * FROM m2_recovery_events WHERE event_id=?", (str(proof.get("dispatch_event_id") or ""),))
    settled = one("SELECT * FROM m2_recovery_events WHERE event_id=?", (str(proof.get("settlement_event_id") or ""),))
    latest = one("SELECT event_id FROM m2_recovery_events WHERE recovery_id=? ORDER BY created_at DESC,event_id DESC LIMIT 1", (job["recovery_id"],))
    attempt = one("SELECT * FROM ai_delivery_attempts WHERE attempt_id=?", (str(proof.get("claim_attempt_id") or ""),))
    queue = one("SELECT * FROM ai_candidate_queue WHERE path=?", (job["canonical_path"],))
    gate = one("SELECT gate_start_epoch FROM m2_observation_gates WHERE gate_id=?", (str(root_cause.get("expected_old_gate_id") or ""),))
    for event, event_type, digest_key in (
        (dispatch, "RECOVERY_DISPATCHED", "dispatch_payload_sha256"),
        (settled, "RECOVERY_FAILED", "settlement_payload_sha256"),
    ):
        if (event["recovery_id"] != job["recovery_id"] or event["event_type"] != event_type
            or event["run_id"] != _meta(connection, "last_run_id", "")
            or hashlib.sha256(str(event["payload_json"]).encode("utf-8")).hexdigest() != proof.get(digest_key)):
            raise RuntimeContractError("quality_pause_event_binding_mismatch")
    payload = json.loads(str(settled["payload_json"]))
    detail = str(attempt["detail"] or "")
    if (job["status"] != "FAILED" or job["failure_category"] != "PERMANENT_SYSTEM_ERROR"
        or job["last_recovery_version"] != old_worker_sha
        or payload.get("runtime_version") != old_worker_sha
        or payload.get("failure_category") != "PERMANENT_SYSTEM_ERROR"
        or payload.get("next_status") != "FAILED"
        or job["claim_attempt_id"] != attempt["attempt_id"]
        or payload.get("attempt_id") != attempt["attempt_id"]
        or attempt["status"] != "review_required" or attempt["stage"] != "translation"
        or attempt["error_code"] != "translation_unknown"
        or not detail.startswith("Targeted subtitle readability repair exceeded its hard display limit at index ")
        or detail != job["failure_reason"] or detail != queue["last_error"]
        or classify_failure(attempt["stage"], attempt["error_code"], detail) != "QUALITY_BLOCKED"
        or latest["event_id"] != settled["event_id"]
        or float(_meta(connection, "last_dispatch_at", "0")) != dispatch["created_at"]
        or not float(gate["gate_start_epoch"]) <= dispatch["created_at"] < settled["created_at"] <= float(root_cause["expected_breaker_updated_at"])
        or not 0 <= settled["created_at"] - float(attempt["finished_at"] or 0) <= 30
        or queue["status"] != "paused" or queue["last_error_code"] != "translation_unknown"
        or queue["mtime_ns"] != job["media_mtime_ns"]):
        raise RuntimeContractError("quality_pause_cause_not_proven")
    checkpoint = str(proof.get("checkpoint_sha256") or "")
    if (not re.fullmatch(r"[0-9a-f]{64}", checkpoint)
        or checkpoint != job["checkpoint_sha256"] or checkpoint != payload.get("checkpoint_after")
        or checkpoint != _current_checkpoint_sha(connection, job["canonical_path"], job["media_mtime_ns"])):
        raise RuntimeContractError("quality_pause_checkpoint_changed")
    evidence = {
        "changed": True, "lane_before": "PAUSED", "lane_after": "CANARY_READY",
        "recovery_record_id": recovery_record_id, "recovery_id": job["recovery_id"],
        "original_canary_status": "FAILED", "delivery_status": "review_required",
        "corrected_failure_category": "QUALITY_BLOCKED", "checkpoint_sha256": checkpoint,
        "dispatch_event_id": dispatch["event_id"], "settlement_event_id": settled["event_id"],
        "job_states_changed": 0, "queue_states_changed": 0,
        "retry_budget_preserved": True, "retry_deadlines_preserved": True,
    }
    if not validate_only:
        _set_meta(connection, "lane_state", "CANARY_READY", now)
        _record_event(connection, event_key=f"quality-pause-release:{recovery_record_id}",
                      recovery_id=job["recovery_id"], run_id=str(settled["run_id"]),
                      event_type="RECOVERY_QUALITY_PAUSE_RELEASED", payload=evidence, now=now)
    return evidence


def _validate_collision_regression(config: Any, evidence: Mapping[str, Any], root_cause: Mapping[str, Any], now: float) -> None:
    proof = root_cause.get("regression_results")
    if (not isinstance(proof, Mapping)
        or proof.get("contract") != "m2-generic-failure-collision-regression-v1"
        or proof.get("worker_source_revision") != evidence.get("worker_source_revision")
        or proof.get("production_resources_affected") is not False
        or any(proof.get(key) is not True for key in (
            "mixed_causes_separated", "same_cause_distinct_jobs_trips",
            "scanner_second_writer_during_inventory_io"))):
        raise RuntimeContractError("collision_regression_evidence_invalid")
    log_root = Path(str(getattr(config, "log_path", config.work_path))).resolve()
    log_path = Path(str(proof.get("full_log_path") or "")).resolve()
    if not log_path.is_relative_to(log_root) or not log_path.is_file() or "sha256:" + sha256_file(log_path) != proof.get("full_log_sha256"):
        raise RuntimeContractError("collision_regression_log_invalid")
    fault = evidence["fault_results"]
    try:
        started = float(fault["container_started_at_epoch"])
        testing = float(fault["started_at_epoch"])
        finished = float(fault["finished_at_epoch"])
        trip_epoch = float(root_cause["expected_breaker_updated_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeContractError("collision_fault_evidence_not_fresh") from exc
    if not all(math.isfinite(value) for value in (started, testing, finished, trip_epoch)) or not trip_epoch < started <= testing <= finished <= now:
        raise RuntimeContractError("collision_fault_evidence_not_fresh")


def _prepare_pending_recovery_resume(
    config: Any,
    evidence: Mapping[str, Any],
    *,
    source_revision_file: str | Path,
    state_path_override: str | Path | None,
    now: float,
) -> dict[str, Any]:
    """Resume a verified recovery that stopped after reconciliation but before arm."""

    if not bool(getattr(config, "m2_recovery_enabled", False)):
        raise RuntimeContractError("m2_recovery_policy_not_loaded")
    decision = _decision_descriptor(config)
    local_status, reason_code = _local_guardrail_status(config, decision)
    if local_status != "ARMED":
        raise RuntimeContractError(reason_code, status=local_status)

    worker_commit = _require_sha(evidence.get("worker_commit_sha"), "worker_commit")
    _require_sha(evidence.get("webui_commit_sha"), "webui_commit")
    expected_source = _require_source_revision(
        evidence.get("worker_source_revision"), "worker"
    )
    _require_source_revision(evidence.get("webui_source_revision"), "webui")
    if _read_source_revision(source_revision_file) != expected_source:
        raise RuntimeContractError("worker_source_revision_mismatch")
    if worker_runtime_code_revision(config) != _require_source_revision(
        evidence.get("worker_runtime_code_revision"), "worker_runtime_code"
    ):
        raise RuntimeContractError("worker_runtime_code_revision_mismatch")
    if configuration_fingerprint(config) != str(
        evidence.get("configuration_fingerprint") or ""
    ):
        raise RuntimeContractError("configuration_fingerprint_mismatch")
    if evidence.get("decision") != decision:
        raise RuntimeContractError("decision_schema_mismatch")
    live_instance = worker_runtime_instance_fingerprint(config)
    if live_instance["container_identity"] != _require_container_identity(
        evidence.get("worker_container_identity"), "worker"
    ):
        raise RuntimeContractError("worker_container_identity_mismatch")
    if live_instance["runtime_instance_fingerprint"] != str(
        evidence.get("worker_runtime_instance_fingerprint") or ""
    ):
        raise RuntimeContractError("worker_runtime_instance_mismatch")
    runtime_checks = evidence.get("runtime_checks")
    if not isinstance(runtime_checks, Mapping) or any(
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
    fault_results = evidence.get("fault_results")
    if (
        not isinstance(fault_results, Mapping)
        or fault_results.get("contract") != FAULT_RESULT_CONTRACT
        or int(fault_results.get("passed_count") or 0) != len(REQUIRED_BREAKERS)
        or int(fault_results.get("required_count") or 0) != len(REQUIRED_BREAKERS)
        or fault_results.get("worker_source_revision") != expected_source
        or fault_results.get("production_resources_affected") is not False
    ):
        raise RuntimeContractError("recovery_fault_evidence_invalid")

    root_cause = evidence.get("root_cause")
    if not isinstance(root_cause, Mapping):
        raise RuntimeContractError("recovery_root_cause_missing")
    expected_reason = _safe_code(
        root_cause.get("breaker_reason"), "recovery_reason_missing"
    )
    affected_stage = _safe_code(root_cause.get("affected_stage"), "stage_missing")
    failure_code = _safe_code(root_cause.get("failure_code"), "failure_code_missing")
    expected_old_gate = str(root_cause.get("expected_old_gate_id") or "")
    planned_mode = root_cause.get("mode") == "planned_runtime_change"
    if expected_reason != ("runtime_change" if planned_mode else "repeated_identical_stage_failure") or not expected_old_gate:
        raise RuntimeContractError("pending_recovery_root_cause_mismatch")
    from m2_production_recovery import breaker_streak_eligible, classify_failure

    collision_mode = root_cause.get("mode") == "generic_failure_signature_collision"
    if planned_mode:
        receipt = _planned_change_receipt(config, root_cause)
        if worker_commit != receipt.get("expected_new_worker_sha"):
            raise RuntimeContractError("planned_change_new_sha_mismatch")
    elif collision_mode:
        _validate_collision_regression(config, evidence, root_cause, now)
    elif root_cause.get("mode") or classify_failure(affected_stage, failure_code) != "QUALITY_BLOCKED" or breaker_streak_eligible(
        {
            "terminal_status": "RETRYING",
            "stage": affected_stage,
            "error_code": failure_code,
        }
    ):
        raise RuntimeContractError("breaker_root_cause_fix_not_loaded")

    state_path = runtime_state_path(config, state_path_override)
    prior_runtime = load_runtime_state(config, state_path_override)
    if not isinstance(prior_runtime, dict) or prior_runtime.get("status") not in {
        "DISARMED",
        "ARMED",
    }:
        raise RuntimeContractError("pending_recovery_runtime_state_missing")
    if prior_runtime.get("status") == "DISARMED" and prior_runtime.get("disarm_reason") != (
        "controlled_breaker_recovery_pending_new_gate"
    ):
        raise RuntimeContractError("pending_recovery_runtime_state_invalid")

    breaker_path = Path(
        str(getattr(config, "m2_server_canary_circuit_breaker_state_path", ""))
    )
    if not breaker_path.is_absolute():
        breaker_path = Path(config.work_path) / breaker_path
    breaker = _read_json(breaker_path)
    breaker_record = breaker.get("recovery_record") if isinstance(breaker, Mapping) else None
    if (
        not isinstance(breaker, dict)
        or breaker.get("tripped") is not False
        or not isinstance(breaker_record, Mapping)
        or breaker_record.get("contract") != BREAKER_RECOVERY_CONTRACT
    ):
        raise RuntimeContractError("pending_recovery_breaker_record_invalid")
    recovery_record_id = str(breaker_record.get("recovery_record_id") or "")
    if not recovery_record_id.startswith("m2breakerrec_"):
        raise RuntimeContractError("pending_recovery_record_id_invalid")
    if str(breaker_record.get("old_gate_id") or "") != expected_old_gate:
        raise RuntimeContractError("pending_recovery_old_gate_mismatch")
    original_new_worker_sha = _require_sha(
        breaker_record.get("new_worker_sha"), "pending_recovery_worker"
    )
    state_record = prior_runtime.get("recovery_record")
    if isinstance(state_record, Mapping) and (
        state_record.get("contract") != BREAKER_RECOVERY_CONTRACT
        or str(state_record.get("recovery_record_id") or "") != recovery_record_id
    ):
        raise RuntimeContractError("pending_recovery_state_record_mismatch")

    expected_log_digest = str(
        (state_record.get("log_sha256") if isinstance(state_record, Mapping) else "")
        or breaker_record.get("log_sha256")
        or ""
    )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_log_digest):
        raise RuntimeContractError("pending_recovery_log_digest_invalid")
    log_root = Path(str(getattr(config, "log_path", config.work_path)))
    candidates = [
        path
        for path in log_root.glob(
            f"m2-production-recovery-*-{recovery_record_id[-8:]}.json"
        )
        if not path.name.startswith("m2-production-recovery-resume-")
    ]
    if len(candidates) != 1 or "sha256:" + sha256_file(candidates[0]) != expected_log_digest:
        raise RuntimeContractError("pending_recovery_log_unavailable")
    recovery_log = _read_json(candidates[0])
    if (
        not isinstance(recovery_log, Mapping)
        or recovery_log.get("contract") != BREAKER_RECOVERY_CONTRACT
        or recovery_log.get("recovery_record_id") != recovery_record_id
        or recovery_log.get("old_gate_id") != expected_old_gate
        or recovery_log.get("new_worker_sha") != original_new_worker_sha
        or recovery_log.get("production_resources_affected") is not False
    ):
        raise RuntimeContractError("pending_recovery_log_invalid")
    if collision_mode and (
        recovery_log.get("recovery_mode") != "generic_failure_signature_collision"
        or recovery_log.get("incident", {}).get("trip_observed_at_epoch") != root_cause.get("expected_breaker_updated_at")
        or recovery_log.get("root_cause_evidence_sha256") != "sha256:" + hashlib.sha256(
            json.dumps(dict(root_cause), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    ):
        raise RuntimeContractError("pending_collision_evidence_mismatch")
    if planned_mode and (
        recovery_log.get("recovery_mode") != "planned_runtime_change"
        or recovery_log.get("planned_change_receipt_sha256") != root_cause.get("planned_change_receipt_sha256")
        or not isinstance(recovery_log.get("completion_runtime"), Mapping)
        or any(evidence.get(key) != value for key, value in recovery_log["completion_runtime"].items())
    ):
        raise RuntimeContractError("pending_planned_change_evidence_mismatch")

    from m2_observation_store import INVALIDATED_RUNTIME, active_gate, gate_by_id
    from scan_state import ScanStateStore

    store = ScanStateStore.from_config(config)
    try:
        old_gate = gate_by_id(store.observation_connection, expected_old_gate)
        if not isinstance(old_gate, Mapping) or old_gate.get("status") != INVALIDATED_RUNTIME:
            raise RuntimeContractError("pending_recovery_old_gate_not_invalidated")
        current_active = active_gate(store.observation_connection)
        if prior_runtime.get("status") == "DISARMED" and current_active is not None:
            raise RuntimeContractError("pending_recovery_unexpected_active_gate")
        if planned_mode and prior_runtime.get("status") == "DISARMED":
            _require_planned_change_idle(store.observation_connection, config, now)
            if _planned_change_snapshot(store.observation_connection, expected_old_gate) != receipt["snapshot"]:
                raise RuntimeContractError("planned_change_new_work_or_evidence_changed")
        if prior_runtime.get("status") == "ARMED":
            current_gate_id = str((prior_runtime.get("gate") or {}).get("gate_id") or "")
            if not isinstance(current_active, Mapping) or current_active.get("gate_id") != current_gate_id:
                raise RuntimeContractError("pending_recovery_active_gate_mismatch")
            if planned_mode and runtime_guardrail_status(
                config, source_revision_file=source_revision_file,
                state_path_override=state_path_override,
            ).get("status") != "ARMED":
                raise RuntimeContractError("pending_planned_change_runtime_mismatch")
    finally:
        store.close()

    claim_pause = _set_durable_claim_control(
        config,
        paused=True,
        requested_by="m2-controlled-breaker-recovery",
        now=now,
    )
    resume_evidence = {
        "contract": "m2-controlled-breaker-recovery-resume-v1",
        "recovery_record_id": recovery_record_id,
        "prepared_at": _utc_timestamp(now),
        "prepared_at_epoch": now,
        "old_gate_id": expected_old_gate,
        "original_recovery_worker_sha": original_new_worker_sha,
        "completion_worker_sha": worker_commit,
        "durable_claim_pause": claim_pause,
        "production_resources_affected": False,
    }
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    resume_log_path = log_root / (
        f"m2-production-recovery-resume-{stamp}-{recovery_record_id[-8:]}.json"
    )
    atomic_write_text(
        resume_log_path,
        json.dumps(resume_evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    completion = {
        "worker_sha": worker_commit,
        "prepared_at_epoch": now,
        "log_sha256": "sha256:" + sha256_file(resume_log_path),
    }
    updated_breaker = dict(breaker)
    updated_breaker_record = dict(breaker_record)
    updated_breaker_record["completion"] = completion
    updated_breaker["recovery_record"] = updated_breaker_record
    updated_breaker["updated_at"] = now
    atomic_write_text(
        breaker_path,
        json.dumps(updated_breaker, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    if prior_runtime.get("status") == "DISARMED" and isinstance(state_record, Mapping):
        updated_runtime = dict(prior_runtime)
        updated_state_record = dict(state_record)
        updated_state_record["completion"] = completion
        updated_runtime["recovery_record"] = updated_state_record
        atomic_write_text(
            state_path,
            json.dumps(updated_runtime, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
    return {
        "status": "DISARMED",
        "recovery_record_id": recovery_record_id,
        "old_worker_sha": str(recovery_log.get("old_worker_sha") or ""),
        "new_worker_sha": worker_commit,
        "old_gate_id": expected_old_gate,
        "old_gate_status": "INVALIDATED_BY_RUNTIME_CHANGE",
        "breaker_before": "TRIPPED",
        "breaker_after": "ARMED_PENDING_NEW_GATE",
        "log_path": str(resume_log_path),
        "log_sha256": completion["log_sha256"],
        "reconciliation": recovery_log.get("reconciliation"),
        "production_resources_affected": False,
    }


def recover_runtime_local(
    config: Any,
    evidence: Mapping[str, Any],
    *,
    source_revision_file: str | Path = "/app/.source-revision",
    state_path_override: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Perform one fail-closed, evidence-bound breaker recovery inside Worker."""

    timestamp = time.time() if now is None else float(now)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise RuntimeContractError("recovery_timestamp_invalid")
    if not bool(getattr(config, "m2_recovery_enabled", False)):
        raise RuntimeContractError("m2_recovery_policy_not_loaded")
    decision = _decision_descriptor(config)
    local_status, reason_code = _local_guardrail_status(config, decision)
    if local_status != "TRIPPED":
        if local_status == "ARMED":
            return _prepare_pending_recovery_resume(
                config,
                evidence,
                source_revision_file=source_revision_file,
                state_path_override=state_path_override,
                now=timestamp,
            )
        raise RuntimeContractError("breaker_not_tripped", status=local_status)
    if reason_code != "circuit_breaker_tripped":
        raise RuntimeContractError("breaker_status_unexpected", status=local_status)

    worker_commit = _require_sha(evidence.get("worker_commit_sha"), "worker_commit")
    _require_sha(evidence.get("webui_commit_sha"), "webui_commit")
    _require_image_id(evidence.get("worker_image_id"), "worker")
    _require_image_id(evidence.get("webui_image_id"), "webui")
    _require_container_id(evidence.get("worker_container_id"), "worker")
    _require_source_revision(evidence.get("webui_source_revision"), "webui")
    expected_source = _require_source_revision(
        evidence.get("worker_source_revision"), "worker"
    )
    if _read_source_revision(source_revision_file) != expected_source:
        raise RuntimeContractError("worker_source_revision_mismatch")
    if worker_runtime_code_revision(config) != _require_source_revision(
        evidence.get("worker_runtime_code_revision"), "worker_runtime_code"
    ):
        raise RuntimeContractError("worker_runtime_code_revision_mismatch")
    if configuration_fingerprint(config) != str(
        evidence.get("configuration_fingerprint") or ""
    ):
        raise RuntimeContractError("configuration_fingerprint_mismatch")
    if evidence.get("decision") != decision:
        raise RuntimeContractError("decision_schema_mismatch")
    runtime_checks = evidence.get("runtime_checks")
    if not isinstance(runtime_checks, Mapping) or any(
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
    live_instance = worker_runtime_instance_fingerprint(config)
    if live_instance["container_identity"] != _require_container_identity(
        evidence.get("worker_container_identity"), "worker"
    ):
        raise RuntimeContractError("worker_container_identity_mismatch")
    if live_instance["runtime_instance_fingerprint"] != str(
        evidence.get("worker_runtime_instance_fingerprint") or ""
    ):
        raise RuntimeContractError("worker_runtime_instance_mismatch")
    fault_results = evidence.get("fault_results")
    if (
        not isinstance(fault_results, Mapping)
        or fault_results.get("contract") != FAULT_RESULT_CONTRACT
        or int(fault_results.get("passed_count") or 0) != len(REQUIRED_BREAKERS)
        or int(fault_results.get("required_count") or 0) != len(REQUIRED_BREAKERS)
        or fault_results.get("worker_source_revision") != expected_source
        or fault_results.get("production_resources_affected") is not False
    ):
        raise RuntimeContractError("recovery_fault_evidence_invalid")

    root_cause = evidence.get("root_cause")
    if not isinstance(root_cause, Mapping):
        raise RuntimeContractError("recovery_root_cause_missing")
    expected_reason = _safe_code(
        root_cause.get("breaker_reason"), "recovery_reason_missing"
    )
    affected_stage = _safe_code(root_cause.get("affected_stage"), "stage_missing")
    failure_code = _safe_code(root_cause.get("failure_code"), "failure_code_missing")
    planned_mode = root_cause.get("mode") == "planned_runtime_change"
    if expected_reason != ("runtime_change" if planned_mode else "repeated_identical_stage_failure"):
        raise RuntimeContractError("unsupported_breaker_recovery_reason")
    from m2_production_recovery import (
        breaker_streak_eligible,
        classify_failure,
        normalize_failure_signature,
        reconcile_historical_jobs,
        record_breaker_recovery,
    )

    collision_mode = root_cause.get("mode") == "generic_failure_signature_collision"
    category = classify_failure(affected_stage, failure_code)
    if planned_mode:
        if affected_stage != "runtime_validation" or failure_code != "live_worker_container_identity_mismatch":
            raise RuntimeContractError("planned_change_signature_invalid")
        category = "PLANNED_RUNTIME_CHANGE"
    elif collision_mode:
        if affected_stage != "worker" or failure_code != "worker_unknown":
            raise RuntimeContractError("unsupported_collision_recovery_signature")
        _validate_collision_regression(config, evidence, root_cause, timestamp)
        category = "GENERIC_FAILURE_SIGNATURE_COLLISION"
    elif root_cause.get("mode") or category != "QUALITY_BLOCKED" or breaker_streak_eligible(
        {
            "terminal_status": "RETRYING",
            "stage": affected_stage,
            "error_code": failure_code,
        }
    ):
        raise RuntimeContractError("breaker_root_cause_fix_not_loaded")

    state_path = runtime_state_path(config, state_path_override)
    prior_runtime = load_runtime_state(config, state_path_override)
    if not isinstance(prior_runtime, dict):
        raise RuntimeContractError("prior_runtime_state_missing")
    prior_baseline = prior_runtime.get("baseline")
    prior_gate = prior_runtime.get("gate")
    if not isinstance(prior_baseline, Mapping) or not isinstance(prior_gate, Mapping):
        raise RuntimeContractError("prior_runtime_state_invalid")
    old_worker_commit = _require_sha(
        prior_baseline.get("worker_commit_sha"), "prior_worker_commit"
    )
    if old_worker_commit == worker_commit:
        raise RuntimeContractError("recovery_requires_new_worker_runtime")
    expected_old_gate = str(root_cause.get("expected_old_gate_id") or "")
    if expected_old_gate and str(prior_gate.get("gate_id") or "") != expected_old_gate:
        raise RuntimeContractError("prior_gate_identity_mismatch")
    claim_pause = _set_durable_claim_control(
        config,
        paused=True,
        requested_by="m2-controlled-breaker-recovery",
        now=timestamp,
    )

    breaker_path = Path(
        str(getattr(config, "m2_server_canary_circuit_breaker_state_path", ""))
    )
    if not breaker_path.is_absolute():
        breaker_path = Path(config.work_path) / breaker_path
    breaker = _read_json(breaker_path)
    if not isinstance(breaker, dict) or breaker.get("tripped") is not True:
        raise RuntimeContractError("breaker_evidence_missing")
    reasons = breaker.get("reasons")
    if not isinstance(reasons, list) or not any(
        isinstance(item, Mapping) and item.get("reason_code") == expected_reason
        for item in reasons
    ):
        raise RuntimeContractError("breaker_reason_mismatch")
    try:
        tripped_at = float(breaker.get("tripped_at"))
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError("breaker_timestamp_invalid") from exc

    from m2_observation_store import (
        INVALIDATED_RUNTIME,
        active_gate,
        invalidate_active_gate,
        latest_gate,
        reset_failure_streaks,
    )
    from pipeline_state import PIPELINE_SCHEMA_VERSION
    from scan_state import ScanStateStore

    store = ScanStateStore.from_config(config)
    recovery_record_id = "m2breakerrec_" + uuid.uuid4().hex
    try:
        connection = store.observation_connection
        connection.execute("BEGIN IMMEDIATE")
        stale_seconds = int(
            getattr(config, "m2_recovery_stale_running_seconds", 21600) or 21600
        )
        stale_before = timestamp - max(60, stale_seconds)
        fresh_delivery = int(
            connection.execute(
                "SELECT COUNT(1) FROM ai_delivery_attempts "
                "WHERE status='running' AND updated_at>?",
                (stale_before,),
            ).fetchone()[0]
        )
        fresh_pipeline = int(
            connection.execute(
                "SELECT COUNT(1) FROM pipeline_stage_attempts "
                "WHERE status='RUNNING' AND heartbeat_at>?",
                (stale_before,),
            ).fetchone()[0]
        )
        if fresh_delivery or fresh_pipeline:
            raise RuntimeContractError("running_work_has_not_reached_safe_boundary")
        if collision_mode and connection.execute(
            "SELECT 1 FROM ai_candidate_queue WHERE status='running' AND running_at>? LIMIT 1",
            (stale_before,),
        ).fetchone() is not None:
            raise RuntimeContractError("running_work_has_not_reached_safe_boundary")
        before = _durable_recovery_snapshot(connection)
        threshold = int(getattr(config, "m2_server_canary_identical_failure_threshold", 3) or 3)
        incident = _planned_change_incident(
            connection, config=config, evidence=evidence, breaker=breaker, now=timestamp,
        ) if planned_mode else _generic_collision_incident(
            connection, root_cause=root_cause, breaker=breaker, threshold=threshold, config=config,
        ) if collision_mode else _breaker_incident_sources(
            connection,
            stage=affected_stage,
            error_code=failure_code,
            tripped_at=tripped_at,
            threshold=int(
                getattr(config, "m2_server_canary_identical_failure_threshold", 3)
                or 3
            ),
        )
        active = active_gate(connection)
        if active is not None:
            if expected_old_gate and str(active.get("gate_id") or "") != expected_old_gate:
                raise RuntimeContractError("active_gate_identity_mismatch")
            invalidated = invalidate_active_gate(
                connection,
                INVALIDATED_RUNTIME,
                evidence={
                    "recovery_record_id": recovery_record_id,
                    "old_worker_sha": old_worker_commit,
                    "new_worker_sha": worker_commit,
                    "breaker_reason": expected_reason,
                },
                now=timestamp,
            )
        else:
            invalidated = latest_gate(connection)
            if (
                not isinstance(invalidated, Mapping)
                or invalidated.get("status") != INVALIDATED_RUNTIME
                or (expected_old_gate and invalidated.get("gate_id") != expected_old_gate)
            ):
                raise RuntimeContractError("active_gate_missing_for_recovery")
        recovery = {"scope": "planned_runtime_change" if planned_mode else "exact_incident_only",
                    "historical_reconciliation_skipped": True,
                    "requeued": 0, "job_states_changed": 0} if (collision_mode or planned_mode) else reconcile_historical_jobs(
            connection,
            current_worker_version=worker_commit,
            current_analyzer_version=str(
                getattr(config, "source_analyzer_version", "unknown") or "unknown"
            ),
            current_decision_schema_version=int(
                getattr(config, "source_decision_schema_version", 0) or 0
            ),
            current_checkpoint_schema_version=PIPELINE_SCHEMA_VERSION,
            retry_budget=int(getattr(config, "m2_recovery_retry_budget", 2) or 2),
            stale_after_seconds=int(
                stale_seconds
            ),
            now=timestamp,
            evidence={
                "recovery_record_id": recovery_record_id,
                "breaker_reason": expected_reason,
                "normalized_failure_signature": normalize_failure_signature(
                    affected_stage, failure_code
                ),
            },
        )
        if collision_mode:
            recovery["lane_recovery"] = _recover_verified_quality_pause(
                connection, root_cause=root_cause, old_worker_sha=old_worker_commit,
                recovery_record_id=recovery_record_id, now=timestamp,
            )
        reset_failure_streaks(connection)
        after = _durable_recovery_snapshot(connection)
        for key in (
            "queue_count",
            "queue_identity_sha256",
            "checkpoint_count",
            "checkpoint_identity_sha256",
            "formal_output_record_count",
            "formal_output_identity_sha256",
        ):
            if before[key] != after[key]:
                raise RuntimeContractError(f"recovery_{key}_changed")
        source_after = _planned_change_incident(
            connection, config=config, evidence=evidence, breaker=breaker, now=timestamp,
        ) if planned_mode else _generic_collision_incident(
            connection, root_cause=root_cause, breaker=breaker, threshold=threshold, config=config,
            validate_counters=False,
        ) if collision_mode else _breaker_incident_sources(
            connection,
            stage=affected_stage,
            error_code=failure_code,
            tripped_at=tripped_at,
            threshold=int(
                getattr(config, "m2_server_canary_identical_failure_threshold", 3)
                or 3
            ),
        )
        identity_key = "planned_snapshot_sha256" if planned_mode else "source_identity_sha256"
        if source_after[identity_key] != incident[identity_key]:
            raise RuntimeContractError("recovery_source_identity_changed")
        recovery_evidence = {
            "contract": BREAKER_RECOVERY_CONTRACT,
            "recovery_record_id": recovery_record_id,
            "recovered_at_epoch": timestamp,
            "breaker_reason": expected_reason,
            "affected_stage": affected_stage,
            "failure_code": failure_code,
            "failure_category": category,
            "normalized_failure_signature": normalize_failure_signature(
                affected_stage, failure_code
            ),
            "old_worker_sha": old_worker_commit,
            "new_worker_sha": worker_commit,
            "old_gate_id": str(invalidated.get("gate_id") or ""),
            "old_gate_status": str(invalidated.get("status") or ""),
            "queue_identity_preserved": True,
            "checkpoint_identity_preserved": True,
            "source_identity_preserved": True,
            "formal_output_identity_preserved": True,
            "production_resources_affected": False,
            "durable_claim_pause": claim_pause,
            "before": before,
            "after": after,
            "incident": incident,
            "reconciliation": recovery,
        }
        if collision_mode:
            recovery_evidence.update({
                "recovery_mode": "generic_failure_signature_collision",
                "root_cause_evidence_sha256": "sha256:" + hashlib.sha256(
                    json.dumps(dict(root_cause), sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "regression_results": dict(root_cause["regression_results"]),
                "remaining_language_failures_unresolved": True,
            })
        if planned_mode:
            recovery_evidence.update({
                "recovery_mode": "planned_runtime_change",
                "source_identity_preserved": None,
                "source_integrity_verification": "not_reprobed_no_media_mutation_operations",
                "planned_change_receipt_sha256": root_cause["planned_change_receipt_sha256"],
                "completion_runtime": {key: evidence[key] for key in (
                    "worker_commit_sha", "worker_source_revision", "worker_runtime_code_revision",
                    "worker_container_id", "worker_container_identity", "worker_runtime_instance_fingerprint",
                    "worker_image_id", "webui_commit_sha", "webui_source_revision",
                    "configuration_fingerprint", "decision",
                )},
            })
        record_breaker_recovery(
            connection,
            recovery_record_id=recovery_record_id,
            evidence=recovery_evidence,
            now=timestamp,
        )
        connection.commit()
    except BaseException:
        store.rollback()
        raise
    finally:
        store.close()

    stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_root = Path(str(getattr(config, "log_path", config.work_path)))
    log_path = log_root / f"m2-production-recovery-{stamp}-{recovery_record_id[-8:]}.json"
    atomic_write_text(
        log_path,
        json.dumps(recovery_evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    if planned_mode and _read_json(breaker_path) != breaker:
        raise RuntimeContractError("planned_change_breaker_changed_before_retirement")
    disarmed_state = dict(prior_runtime)
    disarmed_state.update(
        {
            "status": "DISARMED",
            "disarmed_at": _utc_timestamp(timestamp),
            "disarm_reason": "controlled_breaker_recovery_pending_new_gate",
            "recovery_record": {
                "contract": BREAKER_RECOVERY_CONTRACT,
                "recovery_record_id": recovery_record_id,
                "recovered_at_epoch": timestamp,
                "log_sha256": "sha256:" + sha256_file(log_path),
            },
        }
    )
    atomic_write_text(
        state_path,
        json.dumps(disarmed_state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    archive_path = breaker_path.with_name(
        f"{breaker_path.stem}.tripped-{stamp}-{recovery_record_id[-8:]}.json"
    )
    if collision_mode:
        # Repair the missing current occurrence in the old runtime's reason-code
        # de-duplicated history, without replacing the historical incident.
        occurrence = {"reason_code": expected_reason,
                      "observed_at": incident["trip_observed_at_epoch"],
                      "evidence": incident}
        breaker = {**breaker, "reasons": [*list(breaker.get("reasons") or []), occurrence],
                   "recovered_incident": occurrence,
                   "latest_trip": breaker.get("latest_trip") or occurrence}
    if planned_mode and _read_json(breaker_path) != breaker:
        raise RuntimeContractError("planned_change_breaker_changed_before_archive")
    atomic_write_text(
        archive_path,
        json.dumps(breaker, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    breaker_recovery_record = {
        "contract": BREAKER_RECOVERY_CONTRACT,
        "recovery_record_id": recovery_record_id,
        "recovered_at": _utc_timestamp(timestamp),
        "recovered_at_epoch": timestamp,
        "old_worker_sha": old_worker_commit,
        "new_worker_sha": worker_commit,
        "old_gate_id": str(recovery_evidence["old_gate_id"]),
        "log_sha256": "sha256:" + sha256_file(log_path),
    }
    cleared_breaker = dict(breaker)
    cleared_breaker.update(
        {
            "status": "ARMED",
            "tripped": False,
            "updated_at": timestamp,
            "action": "allow_new_claims_after_new_gate_is_armed",
            "recovery_record": breaker_recovery_record,
        }
    )
    if planned_mode and _read_json(breaker_path) != breaker:
        raise RuntimeContractError("planned_change_breaker_changed_before_clear")
    atomic_write_text(
        breaker_path,
        json.dumps(cleared_breaker, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return {
        "status": "DISARMED",
        "recovery_record_id": recovery_record_id,
        "old_worker_sha": old_worker_commit,
        "new_worker_sha": worker_commit,
        "old_gate_id": str(recovery_evidence["old_gate_id"]),
        "old_gate_status": str(recovery_evidence["old_gate_status"]),
        "breaker_before": "TRIPPED",
        "breaker_after": "ARMED_PENDING_NEW_GATE",
        "log_path": str(log_path),
        "log_sha256": "sha256:" + sha256_file(log_path),
        "reconciliation": recovery,
        "production_resources_affected": False,
    }


def resume_claims_local(
    config: Any,
    *,
    source_revision_file: str | Path = "/app/.source-revision",
    state_path_override: str | Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Release the durable operator pause only after the new Gate is ARMED."""

    timestamp = time.time() if now is None else float(now)
    status = runtime_guardrail_status(
        config,
        source_revision_file=source_revision_file,
        state_path_override=state_path_override,
    )
    if status.get("status") != "ARMED" or status.get("reason_code") != (
        "runtime_baseline_match"
    ):
        raise RuntimeContractError(
            str(status.get("reason_code") or "runtime_not_armed_for_claim_resume"),
            status=str(status.get("status") or "DEGRADED"),
        )
    state = status.get("state")
    gate = state.get("gate") if isinstance(state, Mapping) else None
    gate_id = str(gate.get("gate_id") or "") if isinstance(gate, Mapping) else ""
    if not gate_id:
        raise RuntimeContractError("claim_resume_gate_missing")
    from m2_production_observation import public_status

    observation = public_status(config)
    if (
        observation.get("status") != "ARMED"
        or observation.get("gate_status") != "ACTIVE"
        or observation.get("gate_id") != gate_id
    ):
        raise RuntimeContractError("claim_resume_observation_not_ready")
    control = _set_durable_claim_control(
        config,
        paused=False,
        requested_by="m2-controlled-breaker-recovery-complete",
        now=timestamp,
    )
    return {
        "status": "ARMED",
        "claims_resumed": True,
        "gate_id": gate_id,
        "gate_progress": str(observation.get("gate_progress") or "0/20"),
        "claim_control": control,
    }


def recover_runtime_on_host(
    *,
    docker_binary: str,
    worker_container: str,
    webui_container: str,
    expected_worker_commit_sha: str,
    expected_webui_commit_sha: str,
    expected_old_gate_id: str,
    expected_breaker_reason: str,
    affected_stage: str,
    failure_code: str,
    fault_summary_path: str,
    worker_repo: str | Path = ".",
    webui_repo: str | Path = "../anime-subtitle-worker-webui",
    worker_config_path: str = "/app/config.yaml",
    worker_source_revision_file: str = "/app/.source-revision",
    webui_source_revision_file: str = "/app/.source-revision",
    runtime_state_path_override: str = "",
    root_cause_evidence: Mapping[str, Any] | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Attest, recover, re-arm, and seed one recovery canary without waiting."""

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
    worker_runtime_code_revision_expected = compute_worker_runtime_code_revision(
        worker_repo
    )
    webui_source_revision = compute_webui_source_revision(webui_repo)
    worker_inspect = _inspect_container(docker_binary, worker_name, run)
    webui_inspect = _inspect_container(docker_binary, webui_name, run)
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
    worker_container_config = worker_inspect.get("Config")
    worker_container_identity = _require_container_identity(
        worker_container_config.get("Hostname")
        if isinstance(worker_container_config, Mapping)
        else "",
        "worker",
    )
    probe_result = _run_json(
        [
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
        ],
        run,
        "worker_recovery_probe_failed",
    )
    probe_status = str(probe_result.get("status") or "DEGRADED")
    if probe_status not in {"TRIPPED", "ARMED"}:
        raise RuntimeContractError("worker_breaker_not_recoverable", status=probe_status)
    if probe_status == "ARMED" and probe_result.get("reason_code") != (
        "runtime_guardrails_loaded"
    ):
        raise RuntimeContractError("pending_recovery_runtime_probe_invalid")
    if probe_result.get("worker_source_revision") != worker_source_revision:
        raise RuntimeContractError("worker_source_revision_mismatch")
    if (
        probe_result.get("worker_runtime_code_revision")
        != worker_runtime_code_revision_expected
    ):
        raise RuntimeContractError("worker_runtime_code_revision_mismatch")
    if probe_result.get("worker_container_identity") != worker_container_identity:
        raise RuntimeContractError("worker_container_identity_mismatch")
    live_webui = _run_command(
        [docker_binary, "exec", webui_name, "cat", webui_source_revision_file],
        run,
        reason_code="webui_runtime_probe_failed",
    )
    if _require_source_revision(live_webui.stdout.strip(), "webui") != webui_source_revision:
        raise RuntimeContractError("webui_source_revision_mismatch")
    evidence = {
        "worker_commit_sha": worker_commit,
        "webui_commit_sha": webui_commit,
        "worker_source_revision": worker_source_revision,
        "worker_runtime_code_revision": worker_runtime_code_revision_expected,
        "webui_source_revision": webui_source_revision,
        "worker_image_id": _require_image_id(worker_inspect.get("Image"), "worker"),
        "webui_image_id": _require_image_id(webui_inspect.get("Image"), "webui"),
        "worker_container_id": _require_container_id(worker_inspect.get("Id"), "worker"),
        "worker_container_identity": worker_container_identity,
        "worker_runtime_instance_fingerprint": probe_result.get(
            "worker_runtime_instance_fingerprint"
        ),
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
        "root_cause": {
            **dict(root_cause_evidence or {}),
            "breaker_reason": expected_breaker_reason,
            "affected_stage": affected_stage,
            "failure_code": failure_code,
            "expected_old_gate_id": expected_old_gate_id,
        },
    }
    recover_command = [
        docker_binary,
        "exec",
        "-i",
        worker_name,
        "python",
        "/app/m2_guardrail_runtime.py",
        "recover-local",
        "--config",
        worker_config_path,
        "--source-revision-file",
        worker_source_revision_file,
    ]
    if runtime_state_path_override:
        recover_command.extend(["--state-path", runtime_state_path_override])
    recovered = _run_json(
        recover_command,
        run,
        "controlled_breaker_recovery_failed",
        stdin=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    )
    if recovered.get("status") != "DISARMED":
        raise RuntimeContractError("controlled_breaker_recovery_incomplete")
    armed = arm_runtime_on_host(
        docker_binary=docker_binary,
        worker_container=worker_name,
        webui_container=webui_name,
        expected_worker_commit_sha=worker_commit,
        expected_webui_commit_sha=webui_commit,
        fault_summary_path=fault_summary_path,
        worker_repo=worker_repo,
        webui_repo=webui_repo,
        worker_config_path=worker_config_path,
        worker_source_revision_file=worker_source_revision_file,
        webui_source_revision_file=webui_source_revision_file,
        runtime_state_path_override=runtime_state_path_override,
        runner=run,
    )
    dispatch = {"dispatched": False, "reason_code": "planned_runtime_change_preserves_recovery_lane"} if (
        (root_cause_evidence or {}).get("mode") == "planned_runtime_change"
    ) else _run_json(
        [
            docker_binary,
            "exec",
            worker_name,
            "python",
            "/app/m2_production_recovery.py",
            "dispatch",
            "--config",
            worker_config_path,
        ],
        run,
        "recovery_canary_dispatch_failed",
    )
    resume_command = [
        docker_binary,
        "exec",
        worker_name,
        "python",
        "/app/m2_guardrail_runtime.py",
        "resume-local",
        "--config",
        worker_config_path,
        "--source-revision-file",
        worker_source_revision_file,
    ]
    if runtime_state_path_override:
        resume_command.extend(["--state-path", runtime_state_path_override])
    claim_resume = _run_json(
        resume_command,
        run,
        "claim_resume_failed",
    )
    if claim_resume.get("status") != "ARMED" or claim_resume.get(
        "claims_resumed"
    ) is not True:
        raise RuntimeContractError("claim_resume_incomplete")
    return {
        **armed,
        "breaker_before": "TRIPPED",
        "breaker_after": "ARMED",
        "recovery_record_id": str(recovered.get("recovery_record_id") or ""),
        "recovery_log_path": str(recovered.get("log_path") or ""),
        "recovery_log_sha256": str(recovered.get("log_sha256") or ""),
        "reconciliation": recovered.get("reconciliation"),
        "recovery_dispatch": dispatch,
        "claim_resume": claim_resume,
    }


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
    worker_runtime_code_revision_expected = compute_worker_runtime_code_revision(
        worker_repo
    )
    webui_source_revision = compute_webui_source_revision(webui_repo)
    worker_inspect = _inspect_container(docker_binary, worker_name, run)
    webui_inspect = _inspect_container(docker_binary, webui_name, run)
    worker_image_id = _require_image_id(worker_inspect.get("Image"), "worker")
    webui_image_id = _require_image_id(webui_inspect.get("Image"), "webui")
    worker_container_id = _require_container_id(worker_inspect.get("Id"), "worker")
    worker_container_config = worker_inspect.get("Config")
    worker_container_identity = _require_container_identity(
        worker_container_config.get("Hostname")
        if isinstance(worker_container_config, Mapping)
        else "",
        "worker",
    )
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
    if (
        probe_result.get("worker_runtime_code_revision")
        != worker_runtime_code_revision_expected
    ):
        raise RuntimeContractError("worker_runtime_code_revision_mismatch")
    if probe_result.get("worker_container_identity") != worker_container_identity:
        raise RuntimeContractError("worker_container_identity_mismatch")
    _require_image_id(
        probe_result.get("worker_runtime_instance_fingerprint"),
        "worker_runtime_instance",
    )

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
        "worker_runtime_code_revision": worker_runtime_code_revision_expected,
        "webui_source_revision": webui_source_revision,
        "worker_image_id": worker_image_id,
        "webui_image_id": webui_image_id,
        "worker_container_id": worker_container_id,
        "worker_container_identity": worker_container_identity,
        "worker_runtime_instance_fingerprint": probe_result.get(
            "worker_runtime_instance_fingerprint"
        ),
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
        try:
            child = json.loads(result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            child = None
        if isinstance(child, Mapping) and child.get("reason_code"):
            raise RuntimeContractError(
                str(child.get("reason_code")),
                status=str(child.get("status") or "DEGRADED"),
            )
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
        "worker_runtime_code_revision": str(
            baseline.get("worker_runtime_code_revision") or ""
        ),
        "webui_source_revision": str(baseline.get("webui_source_revision") or ""),
        "worker_image_id": str(baseline.get("worker_image_id") or ""),
        "webui_image_id": str(baseline.get("webui_image_id") or ""),
        "configuration_fingerprint": str(
            baseline.get("configuration_fingerprint") or ""
        ),
        "decision_schema_version": baseline.get("decision_schema_version"),
        "eligibility_policy_version": str(
            baseline.get("eligibility_policy_version") or ""
        ),
        "gate_id": str(gate.get("gate_id") or ""),
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


def _require_container_id(value: Any, component: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _CONTAINER_ID_RE.fullmatch(normalized):
        raise RuntimeContractError(
            f"{_safe_code(component, 'component')}_container_id_invalid"
        )
    return normalized.removeprefix("sha256:")


def _require_container_identity(value: Any, component: str) -> str:
    normalized = str(value or "").strip()
    if not _CONTAINER_RE.fullmatch(normalized):
        raise RuntimeContractError(
            f"{_safe_code(component, 'component')}_container_identity_invalid"
        )
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

    recover = subparsers.add_parser(
        "recover",
        help="Verify and recover a tripped runtime, then initialize a new 0/20 gate",
    )
    recover.add_argument("--docker", default="docker")
    recover.add_argument("--worker-container", default="anime-subtitle-worker")
    recover.add_argument("--webui-container", default="anime-subtitle-worker-webui")
    recover.add_argument("--expected-worker-commit-sha", required=True)
    recover.add_argument("--expected-webui-commit-sha", required=True)
    recover.add_argument("--expected-old-gate-id", required=True)
    recover.add_argument("--expected-breaker-reason", required=True)
    recover.add_argument("--affected-stage", required=True)
    recover.add_argument("--failure-code", required=True)
    recover.add_argument("--worker-repo", default=".")
    recover.add_argument("--webui-repo", default="../anime-subtitle-worker-webui")
    recover.add_argument("--fault-summary", required=True)
    recover.add_argument("--worker-config", default="/app/config.yaml")
    recover.add_argument("--worker-source-revision-file", default="/app/.source-revision")
    recover.add_argument("--webui-source-revision-file", default="/app/.source-revision")
    recover.add_argument("--state-path", default="")
    recover.add_argument("--planned-change-receipt", default="")
    recover.add_argument("--planned-change-receipt-sha256", default="")

    prepare = subparsers.add_parser("prepare-runtime-change", help="Bind a paused, idle ARMED Gate to one necessary deployment")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--source-revision-file", default="/app/.source-revision")
    prepare.add_argument("--state-path", default="")
    prepare.add_argument("--expected-old-gate-id", required=True)
    prepare.add_argument("--expected-new-worker-sha", required=True)
    prepare.add_argument("--receipt-id", required=True)

    probe = subparsers.add_parser("probe", help=argparse.SUPPRESS)
    probe.add_argument("--config", required=True)
    probe.add_argument("--source-revision-file", required=True)
    probe.add_argument("--fault-summary", required=True)
    probe.add_argument("--fault-not-before-epoch", required=True, type=float)

    initialize = subparsers.add_parser("initialize", help=argparse.SUPPRESS)
    initialize.add_argument("--config", required=True)
    initialize.add_argument("--source-revision-file", required=True)
    initialize.add_argument("--state-path", default="")

    recover_local = subparsers.add_parser("recover-local", help=argparse.SUPPRESS)
    recover_local.add_argument("--config", required=True)
    recover_local.add_argument("--source-revision-file", required=True)
    recover_local.add_argument("--state-path", default="")
    resume_local = subparsers.add_parser("resume-local", help=argparse.SUPPRESS)
    resume_local.add_argument("--config", required=True)
    resume_local.add_argument("--source-revision-file", required=True)
    resume_local.add_argument("--state-path", default="")
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
        elif args.command == "recover":
            planned = None
            if args.planned_change_receipt or args.planned_change_receipt_sha256:
                if not args.planned_change_receipt or not args.planned_change_receipt_sha256:
                    raise RuntimeContractError("planned_change_receipt_invalid")
                planned = {"mode": "planned_runtime_change",
                           "planned_change_receipt": args.planned_change_receipt,
                           "planned_change_receipt_sha256": args.planned_change_receipt_sha256}
            result = recover_runtime_on_host(
                docker_binary=args.docker,
                worker_container=args.worker_container,
                webui_container=args.webui_container,
                expected_worker_commit_sha=args.expected_worker_commit_sha,
                expected_webui_commit_sha=args.expected_webui_commit_sha,
                expected_old_gate_id=args.expected_old_gate_id,
                expected_breaker_reason=args.expected_breaker_reason,
                affected_stage=args.affected_stage,
                failure_code=args.failure_code,
                fault_summary_path=args.fault_summary,
                worker_repo=args.worker_repo,
                webui_repo=args.webui_repo,
                worker_config_path=args.worker_config,
                worker_source_revision_file=args.worker_source_revision_file,
                webui_source_revision_file=args.webui_source_revision_file,
                runtime_state_path_override=args.state_path,
                root_cause_evidence=planned,
            )
        elif args.command == "prepare-runtime-change":
            from config import load_config

            result = prepare_runtime_change(
                load_config(args.config), expected_old_gate_id=args.expected_old_gate_id,
                expected_new_worker_sha=args.expected_new_worker_sha, receipt_id=args.receipt_id,
                source_revision_file=args.source_revision_file, state_path_override=args.state_path or None,
            )
        elif args.command == "probe":
            result = probe_local_runtime(
                args.config,
                source_revision_file=args.source_revision_file,
                fault_summary_path=args.fault_summary,
                fault_summary_not_before_epoch=args.fault_not_before_epoch,
            )
        elif args.command == "resume-local":
            from config import load_config

            result = resume_claims_local(
                load_config(args.config),
                source_revision_file=args.source_revision_file,
                state_path_override=args.state_path or None,
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
            if args.command == "recover-local":
                result = recover_runtime_local(
                    config,
                    evidence,
                    source_revision_file=args.source_revision_file,
                    state_path_override=args.state_path or None,
                )
            else:
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
    except Exception as exc:  # noqa: BLE001 - CLI output must remain bounded and path-free.
        reason = _safe_code(
            getattr(exc, "reason_code", type(exc).__name__),
            "runtime_recovery_failed",
        )
        print(
            json.dumps(
                {"status": "DEGRADED", "reason_code": reason},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
