from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import traceback
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch
import uuid

import m2_production_observation as observation
from safe_files import atomic_write_text


FAULT_RESULT_CONTRACT = "m2-isolated-circuit-breaker-test-v1"
FAULT_NAMES = (
    "source_mutation",
    "duplicate_publish",
    "output_parse_failure",
    "incorrect_completion",
    "repeated_oom",
    "repeated_identical_stage_failure",
    "insufficient_disk_space",
)
CHECK_NAMES = (
    "production_admission_path_called",
    "new_job_claim_stopped",
    "queue_preserved",
    "checkpoint_preserved",
    "running_job_not_interrupted",
    "no_false_completed",
    "reason_evidence_persisted",
    "safe_recovery",
    "source_unchanged",
    "production_output_untouched",
)


class GuardrailFaultInjectionError(RuntimeError):
    pass


class _JsonlAudit:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = path.open("x", encoding="utf-8", newline="\n")

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        self._handle.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            + "\n"
        )
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        self._handle.close()


def run_fault_suite(
    log_dir: str | Path,
    *,
    worker_source_revision_file: str | Path = "/app/.source-revision",
) -> dict[str, Any]:
    """Run all M2 breaker faults against generated, isolated fixtures only.

    This function deliberately does not load the production configuration. The
    only caller-provided path is used for append-only validation artifacts; all
    source, queue, checkpoint, and output fixtures live in fresh OS temp dirs.
    """

    worker_source_revision = _read_worker_source_revision(
        worker_source_revision_file
    )
    started_at_epoch = time.time()
    started_at = _utc_timestamp(started_at_epoch)
    run_id = _run_id()
    run_dir = Path(log_dir).expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    log_path = run_dir / "events.jsonl"
    result_path = run_dir / "result.json"
    audit = _JsonlAudit(log_path)
    results: list[dict[str, Any]] = []
    audit.emit(
        "suite_started",
        run_id=run_id,
        faults=list(FAULT_NAMES),
        production_config_loaded=False,
        fixture_scope="fresh_os_temporary_directories",
        worker_source_revision=worker_source_revision,
        started_at=started_at,
        started_at_epoch=started_at_epoch,
    )
    try:
        for fault_name in FAULT_NAMES:
            results.append(_run_fault_case(fault_name, audit))
    except Exception as exc:
        audit.emit(
            "suite_aborted",
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        raise
    finally:
        passed = sum(1 for item in results if item.get("passed"))
        audit.emit(
            "suite_finished",
            passed=passed,
            total=len(FAULT_NAMES),
            production_resources_affected=False,
        )
        audit.close()

    passed = sum(1 for item in results if item.get("passed"))
    finished_at_epoch = time.time()
    summary = {
        "contract": FAULT_RESULT_CONTRACT,
        "schema_version": 1,
        "run_id": run_id,
        "worker_source_revision": worker_source_revision,
        "started_at": started_at,
        "started_at_epoch": started_at_epoch,
        "finished_at": _utc_timestamp(finished_at_epoch),
        "finished_at_epoch": finished_at_epoch,
        "status": "PASS" if passed == len(FAULT_NAMES) else "FAIL",
        "breaker_tests_passed": passed,
        "breaker_tests_total": len(FAULT_NAMES),
        "production_resources_affected": False,
        "production_config_loaded": False,
        "case_results": results,
        "log_path": str(log_path),
        "result_path": str(result_path),
    }
    atomic_write_text(
        result_path,
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return summary


def _run_fault_case(fault_name: str, audit: _JsonlAudit) -> dict[str, Any]:
    previous_process_latch = observation._PROCESS_LOCAL_CIRCUIT_OPEN
    checks = {name: False for name in CHECK_NAMES}
    audit.emit("case_started", fault=fault_name)
    try:
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
        with tempfile.TemporaryDirectory(prefix=f"m2-guardrail-fi-{fault_name}-") as raw:
            sandbox = Path(raw).resolve()
            config, fixtures = _create_isolated_fixture(sandbox)
            _assert_isolated_config(config, sandbox)
            before = _fixture_snapshot(fixtures)
            audit.emit(
                "fixture_created",
                fault=fault_name,
                fixture_hashes=before,
                all_runtime_paths_under_sandbox=True,
            )

            _inject_fault(fault_name, config, audit)
            breaker = _read_json(observation.circuit_breaker_state_path(config))
            _require(
                observation.circuit_breaker_active(config),
                fault_name,
                "breaker_latched",
            )
            _require(
                _attempt_isolated_claim(config, fixtures["queue"]) is False,
                fault_name,
                "new_job_claim_stopped",
            )
            checks["production_admission_path_called"] = True
            checks["new_job_claim_stopped"] = True

            after_trip = _fixture_snapshot(fixtures)
            _require(
                after_trip["queue"] == before["queue"],
                fault_name,
                "queue_preserved",
            )
            checks["queue_preserved"] = True
            _require(
                after_trip["checkpoint"] == before["checkpoint"],
                fault_name,
                "checkpoint_preserved",
            )
            checks["checkpoint_preserved"] = True
            _require(
                after_trip["running_job"] == before["running_job"],
                fault_name,
                "running_job_not_interrupted",
            )
            checks["running_job_not_interrupted"] = True
            _require(
                after_trip["source"] == before["source"],
                fault_name,
                "source_unchanged",
            )
            checks["source_unchanged"] = True
            _require(
                after_trip["published_outputs"] == [],
                fault_name,
                "production_output_untouched",
            )
            checks["production_output_untouched"] = True

            queue_payload = _read_json(fixtures["queue"])
            running_payload = _read_json(fixtures["running_job"])
            observation_payload = _read_optional_json(
                observation.observation_state_path(config)
            )
            _require(
                queue_payload.get("items", [{}])[0].get("state") == "QUEUED"
                and running_payload.get("state") == "PROCESSING"
                and not _contains_completed_state(observation_payload),
                fault_name,
                "no_false_completed",
            )
            checks["no_false_completed"] = True

            reasons = [
                item
                for item in breaker.get("reasons", [])
                if isinstance(item, dict)
                and item.get("reason_code") == fault_name
            ]
            _require(
                bool(reasons)
                and isinstance(reasons[-1].get("evidence"), dict)
                and bool(reasons[-1]["evidence"]),
                fault_name,
                "reason_evidence_persisted",
            )
            _require(
                breaker.get("action") == "stop_claiming_new_jobs"
                and breaker.get("running_job_policy")
                == "finish_without_interruption"
                and breaker.get("checkpoint_policy") == "preserve",
                fault_name,
                "breaker_policy_persisted",
            )
            checks["reason_evidence_persisted"] = True

            recovery = _recover_isolated_latch(config, sandbox, fault_name)
            _require(
                recovery["archived_reason"] == fault_name
                and _production_admit_new_job(config) is True,
                fault_name,
                "safe_recovery",
            )
            checks["safe_recovery"] = True
            after_recovery = _fixture_snapshot(fixtures)
            _require(
                after_recovery == after_trip,
                fault_name,
                "recovery_preserved_fixtures",
            )
            audit.emit(
                "case_verified",
                fault=fault_name,
                checks=checks,
                breaker_reason=fault_name,
                breaker_evidence=reasons[-1]["evidence"],
                recovery=recovery,
            )
        return {"fault": fault_name, "passed": True, "checks": checks}
    except Exception as exc:
        audit.emit(
            "case_failed",
            fault=fault_name,
            checks=checks,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return {
            "fault": fault_name,
            "passed": False,
            "checks": checks,
            "error": str(exc),
        }
    finally:
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = previous_process_latch


def _inject_fault(
    fault_name: str,
    config: SimpleNamespace,
    audit: _JsonlAudit,
) -> None:
    immediate: dict[str, Mapping[str, Any]] = {
        "source_mutation": {
            "failed": True,
            "verified_completed": False,
            "stage": "source_integrity",
            "error_code": "source_checksum_changed",
            "reason_code": "source_mutation",
        },
        "duplicate_publish": {
            "failed": True,
            "verified_completed": False,
            "stage": "publish",
            "error_code": "duplicate_publish",
        },
        "output_parse_failure": {
            "failed": True,
            "verified_completed": False,
            "stage": "delivery_verification",
            "error_code": "output_parse_failure",
        },
    }
    if fault_name == "incorrect_completion":
        job_identity = "isolated-incorrect_completion-1"
        claim = _record_isolated_claim(config, job_identity)
        _require(
            claim.get("recorded") is True and claim.get("gate_eligible") is True,
            fault_name,
            "gate_eligible_claim_recorded",
        )
        result = observation.record_job_result(
            config,
            job_identity=job_identity,
            outcome={
                "failed": False,
                "verified_completed": True,
                "terminal_status": "COMPLETED",
                "stage": "delivery_verification",
                "reason_code": "succeeded",
            },
            strict_evidence={},
        )
        audit.emit("fault_injected", fault=fault_name, step=1, result=result)
        _require(
            result.get("strictly_qualified") is False
            and result.get("circuit_breaker_tripped") is True,
            fault_name,
            "incorrect_completion_intercepted",
        )
        return
    if fault_name in immediate:
        result = observation.record_job_result(
            config,
            job_identity=f"isolated-{fault_name}-1",
            outcome=immediate[fault_name],
        )
        audit.emit("fault_injected", fault=fault_name, step=1, result=result)
        return
    if fault_name == "repeated_oom":
        outcome = {
            "failed": True,
            "verified_completed": False,
            "stage": "transcription",
            "error_code": "transient_oom",
        }
        _inject_repeated_fault(fault_name, config, outcome, audit)
        return
    if fault_name == "repeated_identical_stage_failure":
        outcome = {
            "failed": True,
            "verified_completed": False,
            "stage": "translation",
            "error_code": "model_timeout",
        }
        _inject_repeated_fault(fault_name, config, outcome, audit)
        return
    if fault_name == "insufficient_disk_space":
        with patch.object(
            observation.shutil,
            "disk_usage",
            return_value=SimpleNamespace(total=4, used=4, free=0),
        ):
            admitted = _production_admit_new_job(config)
        audit.emit(
            "fault_injected",
            fault=fault_name,
            step=1,
            admitted=admitted,
            injection_hook="isolated_disk_usage",
        )
        _require(not admitted, fault_name, "disk_hook_stopped_claim")
        return
    raise GuardrailFaultInjectionError(f"unknown fault: {fault_name}")


def _inject_repeated_fault(
    fault_name: str,
    config: SimpleNamespace,
    outcome: Mapping[str, Any],
    audit: _JsonlAudit,
) -> None:
    for step in range(1, 4):
        result = observation.record_job_result(
            config,
            job_identity=f"isolated-{fault_name}-{step}",
            outcome=outcome,
        )
        tripped = observation.circuit_breaker_active(config)
        audit.emit(
            "fault_injected",
            fault=fault_name,
            step=step,
            tripped=tripped,
            result=result,
        )
        if step < 3:
            _require(not tripped, fault_name, f"threshold_not_reached_{step}")
        else:
            _require(tripped, fault_name, "threshold_reached")


def _create_isolated_fixture(
    sandbox: Path,
) -> tuple[SimpleNamespace, dict[str, Path]]:
    source_dir = sandbox / "fixtures" / "input"
    output_dir = sandbox / "fixtures" / "output"
    state_dir = sandbox / "fixtures" / "state"
    checkpoint_dir = state_dir / "checkpoints"
    work_dir = sandbox / "runtime" / "work"
    log_dir = sandbox / "runtime" / "log"
    for directory in (
        source_dir,
        output_dir,
        state_dir,
        checkpoint_dir,
        work_dir,
        log_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    source = source_dir / "synthetic-source.bin"
    queue = state_dir / "queue.json"
    running_job = state_dir / "running-job.json"
    checkpoint = checkpoint_dir / "source-analysis.json"
    source.write_bytes(b"M2 isolated synthetic media sentinel\x00\x01")
    atomic_write_text(
        queue,
        json.dumps(
            {"items": [{"id": "isolated-next-job", "state": "QUEUED"}]},
            sort_keys=True,
        )
        + "\n",
    )
    atomic_write_text(
        running_job,
        json.dumps(
            {"id": "isolated-running-job", "state": "PROCESSING"},
            sort_keys=True,
        )
        + "\n",
    )
    atomic_write_text(
        checkpoint,
        json.dumps(
            {
                "job_id": "isolated-running-job",
                "stage": "source_analysis",
                "state": "COMPLETED",
                "valid": True,
            },
            sort_keys=True,
        )
        + "\n",
    )
    config = SimpleNamespace(
        input_path=str(source_dir),
        work_path=str(work_dir),
        log_path=str(log_dir),
        completed_delivery_enabled=False,
        completed_delivery_path=str(output_dir),
        # Keep the isolated baseline independent of host free space while
        # retaining a positive threshold for the free=0 fault hook.
        disk_min_free_gb=1.0 / (1024 * 1024 * 1024),
        m2_server_canary_observer_enabled=True,
        m2_server_canary_observation_gate_size=20,
        m2_server_canary_observation_state_path="observation-state.json",
        m2_server_canary_observation_output_dir="observation-summaries",
        m2_guardrail_runtime_state_path="m2-guardrail-runtime.json",
        m2_server_canary_circuit_breaker_enabled=True,
        m2_server_canary_circuit_breaker_state_path="circuit-breaker.json",
        m2_server_canary_repeated_oom_threshold=3,
        m2_server_canary_identical_failure_threshold=3,
        max_concurrent_videos=1,
        source_integrity_sha256_enabled=True,
        source_decision_schema_version=1,
        source_decision_version="m2-source-decision-v1",
    )
    _initialize_isolated_runtime(config, sandbox)
    return config, {
        "source": source,
        "queue": queue,
        "running_job": running_job,
        "checkpoint": checkpoint,
        "output_dir": output_dir,
    }


def _assert_isolated_config(config: Any, sandbox: Path) -> None:
    roots = (
        Path(str(config.input_path)),
        Path(str(config.work_path)),
        Path(str(config.log_path)),
        Path(str(config.completed_delivery_path)),
    )
    if not all(_is_within(path, sandbox) for path in roots):
        raise GuardrailFaultInjectionError(
            "refusing fault injection because a runtime path escapes the sandbox"
        )


def _recover_isolated_latch(
    config: Any,
    sandbox: Path,
    fault_name: str,
) -> dict[str, Any]:
    breaker_path = observation.circuit_breaker_state_path(config).resolve()
    if not _is_within(breaker_path, sandbox):
        raise GuardrailFaultInjectionError(
            "refusing recovery because the breaker path escapes the sandbox"
        )
    breaker = _read_json(breaker_path)
    reasons = [
        item
        for item in breaker.get("reasons", [])
        if isinstance(item, dict) and item.get("reason_code") == fault_name
    ]
    if not reasons:
        raise GuardrailFaultInjectionError(
            f"{fault_name}: refusing recovery without persisted reason evidence"
        )
    archive = breaker_path.with_name(f"{breaker_path.stem}.recovered.json")
    if archive.exists():
        raise GuardrailFaultInjectionError(
            f"{fault_name}: recovery archive already exists"
        )
    breaker_path.replace(archive)
    observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
    if observation.circuit_breaker_active(config):
        raise GuardrailFaultInjectionError(
            f"{fault_name}: isolated breaker remained active after recovery"
        )
    recovery_record = {
        "fault": fault_name,
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "archived_breaker_sha256": _sha256(archive),
        "archived_reason": fault_name,
        "scope": "isolated_fixture_only",
    }
    atomic_write_text(
        breaker_path.with_name("recovery-record.json"),
        json.dumps(recovery_record, sort_keys=True, indent=2) + "\n",
    )
    return recovery_record


def _fixture_snapshot(fixtures: Mapping[str, Path]) -> dict[str, Any]:
    output_dir = fixtures["output_dir"]
    return {
        "source": _sha256(fixtures["source"]),
        "queue": _sha256(fixtures["queue"]),
        "running_job": _sha256(fixtures["running_job"]),
        "checkpoint": _sha256(fixtures["checkpoint"]),
        "published_outputs": sorted(
            path.name for path in output_dir.iterdir() if path.is_file()
        ),
    }


def _attempt_isolated_claim(config: Any, queue_path: Path) -> bool:
    """Exercise the runtime gate before the synthetic queue can be mutated."""

    if not _production_admit_new_job(config):
        return False
    queue = _read_json(queue_path)
    items = list(queue.get("items") or [])
    if not items:
        return False
    claimed = dict(items.pop(0))
    claimed["state"] = "PROCESSING"
    queue["items"] = items
    queue["claimed"] = claimed
    atomic_write_text(
        queue_path,
        json.dumps(queue, sort_keys=True, indent=2) + "\n",
    )
    return True


def _production_admit_new_job(config: Any) -> bool:
    """Call the same fail-closed wrapper used immediately before real claims."""

    from main import _m2_server_canary_admit_new_job
    from m2_guardrail_runtime import runtime_guardrail_status

    marker = Path(str(config._isolated_source_revision_file))
    with patch(
        "m2_guardrail_runtime.runtime_guardrail_status",
        side_effect=lambda active_config: runtime_guardrail_status(
            active_config,
            source_revision_file=marker,
        ),
    ):
        return bool(_m2_server_canary_admit_new_job(config))


def _record_isolated_claim(config: Any, job_identity: str) -> dict[str, Any]:
    """Bind a synthetic attempt through the real runtime-aware claim path."""

    from m2_guardrail_runtime import runtime_guardrail_status

    marker = Path(str(config._isolated_source_revision_file))
    with patch(
        "m2_guardrail_runtime.runtime_guardrail_status",
        side_effect=lambda active_config: runtime_guardrail_status(
            active_config,
            source_revision_file=marker,
        ),
    ):
        return observation.record_job_claim(
            config,
            job_identity=job_identity,
            claimed_at=time.time() + 1.0,
        )


def _initialize_isolated_runtime(config: Any, sandbox: Path) -> None:
    """Create a complete sandbox-only ARMED baseline for admission testing."""

    from m2_guardrail_runtime import (
        RUNTIME_CONTRACT,
        RUNTIME_SCHEMA_VERSION,
        _baseline_version,
        configuration_fingerprint,
        runtime_state_path,
        worker_runtime_code_revision,
        worker_runtime_instance_fingerprint,
    )
    from m2_observation_store import ELIGIBILITY_POLICY_VERSION
    from source_decision import SOURCE_DECISION_CONTRACT

    marker = sandbox / "runtime" / "source-revision"
    marker.write_text("a" * 64 + "\n", encoding="utf-8")
    config._isolated_source_revision_file = str(marker)
    config.m2_guardrail_source_revision_file = str(marker)
    container_identity = sandbox / "runtime" / "container-identity"
    container_identity.write_text("isolated-worker\n", encoding="utf-8")
    config.m2_guardrail_container_identity_file = str(container_identity)
    runtime_app = sandbox / "runtime" / "app"
    runtime_app.mkdir(parents=True, exist_ok=True)
    (runtime_app / "requirements.txt").write_text(
        "isolated-fixture==1\n",
        encoding="utf-8",
    )
    (runtime_app / "worker.py").write_text(
        "ISOLATED_GUARDRAIL_FIXTURE = True\n",
        encoding="utf-8",
    )
    config.m2_guardrail_runtime_app_root = str(runtime_app)
    runtime_instance = worker_runtime_instance_fingerprint(config)
    baseline = {
        "worker_commit_sha": "1" * 40,
        "webui_commit_sha": "2" * 40,
        "worker_source_revision": "a" * 64,
        "worker_runtime_code_revision": worker_runtime_code_revision(config),
        "webui_source_revision": "b" * 64,
        "worker_image_id": "sha256:" + "3" * 64,
        "webui_image_id": "sha256:" + "4" * 64,
        "worker_container_id": "7" * 64,
        "worker_container_identity": runtime_instance["container_identity"],
        "worker_runtime_instance_fingerprint": runtime_instance[
            "runtime_instance_fingerprint"
        ],
        "configuration_fingerprint": configuration_fingerprint(config),
        "decision_schema_version": 1,
        "decision_version": "m2-source-decision-v1",
        "decision_contract": SOURCE_DECISION_CONTRACT,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
    }
    now = time.time()
    state = {
        "contract": RUNTIME_CONTRACT,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "status": "ARMED",
        "armed_at": datetime.now(timezone.utc).isoformat(),
        "gate_start_at": datetime.now(timezone.utc).isoformat(),
        "gate_start_epoch": now,
        "gate_baseline_version": _baseline_version(baseline),
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
            "attempt_keys": [],
            "queue_job_keys": [],
            "attempt_count": 0,
            "queue_job_count": 0,
            "policy": "record_result_but_exclude_from_formal_gate",
        },
        "breaker_tests": {
            "contract": FAULT_RESULT_CONTRACT,
            "passed_count": len(FAULT_NAMES),
            "required_count": len(FAULT_NAMES),
            "worker_source_revision": "a" * 64,
            "container_started_at_epoch": now - 3.0,
            "started_at_epoch": now - 2.0,
            "finished_at_epoch": now - 1.0,
            "summary_sha256": "sha256:" + "c" * 64,
            "full_log_sha256": "sha256:" + "d" * 64,
            "production_resources_affected": False,
        },
        "runtime_checks": {
            "worker_container_running": True,
            "webui_container_running": True,
            "worker_config_mount_readonly": True,
            "worker_command_uses_config": True,
            "worker_config_unchanged_since_start": True,
        },
        "production_resources_affected": False,
    }
    atomic_write_text(
        Path(str(config.work_path)) / "ai_control.json",
        json.dumps(
            {
                "paused": True,
                "updated_at": now,
                "requested_by": "isolated-fault-harness",
            },
            sort_keys=True,
        )
        + "\n",
    )
    gate = observation.initialize_observation_gate(config, state, now=now)
    state["gate"]["gate_id"] = str(gate["gate_id"])
    atomic_write_text(
        runtime_state_path(config),
        json.dumps(state, sort_keys=True, indent=2) + "\n",
    )


def _contains_completed_state(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    last_event = payload.get("last_event")
    totals = payload.get("totals")
    window = payload.get("window")
    window_counters = window.get("counters") if isinstance(window, Mapping) else None
    accepted_last_event = bool(
        isinstance(last_event, Mapping)
        and str(last_event.get("terminal_status") or "") == "COMPLETED"
        and last_event.get("gate_qualified") is True
    )
    accepted_total = bool(
        isinstance(totals, Mapping)
        and int(totals.get("completed_strict_verified") or 0) > 0
    )
    accepted_window = bool(
        isinstance(window_counters, Mapping)
        and int(window_counters.get("completed_strict_verified") or 0) > 0
    )
    return accepted_last_event or accepted_total or accepted_window


def _require(condition: bool, fault_name: str, check: str) -> None:
    if not condition:
        raise GuardrailFaultInjectionError(f"{fault_name}: check failed: {check}")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GuardrailFaultInjectionError(f"expected JSON object: {path.name}")
    return payload


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    try:
        return _read_json(path)
    except FileNotFoundError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _read_worker_source_revision(path: str | Path) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip().casefold()
    except OSError as exc:
        raise GuardrailFaultInjectionError(
            "live Worker source revision marker is unavailable"
        ) from exc
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise GuardrailFaultInjectionError(
            "live Worker source revision marker is invalid"
        )
    return value


def _utc_timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"m2-guardrail-fi-{timestamp}-{uuid.uuid4().hex[:8]}"


def bounded_log_tail(
    path: str | Path,
    *,
    max_lines: int = 20,
    max_chars: int = 8000,
) -> list[str]:
    """Return a deliberately bounded tail for failed CLI summaries."""

    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"log unavailable: {type(exc).__name__}"]
    tail = lines[-max(1, max_lines) :]
    remaining = max(1, max_chars)
    bounded: list[str] = []
    for line in tail:
        if remaining <= 0:
            break
        clipped = line[:remaining]
        bounded.append(clipped)
        remaining -= len(clipped)
    return bounded


def cli_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": str(result.get("status") or "FAIL"),
        "breaker_tests_passed": int(result.get("breaker_tests_passed") or 0),
        "breaker_tests_total": int(result.get("breaker_tests_total") or 7),
        "production_resources_affected": bool(
            result.get("production_resources_affected")
        ),
        "run_id": str(result.get("run_id") or ""),
        "log_path": str(result.get("log_path") or ""),
        "result_path": str(result.get("result_path") or ""),
    }
    if payload["status"] != "PASS":
        payload["failed_faults"] = [
            str(item.get("fault") or "unknown")
            for item in result.get("case_results", [])
            if isinstance(item, Mapping) and not item.get("passed")
        ]
        payload["log_tail"] = bounded_log_tail(payload["log_path"])
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run all seven M2 guardrail faults with generated isolated fixtures."
        )
    )
    parser.add_argument(
        "--log-dir",
        required=True,
        help="Parent directory for one new timestamped validation artifact folder.",
    )
    parser.add_argument(
        "--source-revision-file",
        default="/app/.source-revision",
        help="Live Worker source revision marker used to bind this run.",
    )
    args = parser.parse_args(argv)
    try:
        result = run_fault_suite(
            args.log_dir,
            worker_source_revision_file=args.source_revision_file,
        )
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "breaker_tests_passed": 0,
            "breaker_tests_total": len(FAULT_NAMES),
            "production_resources_affected": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(cli_summary(result), ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
