from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping

from acceptance_queue_lane import (
    AcceptanceQueueLane,
    acceptance_queue_lane_enabled,
    load_acceptance_queue_lane,
)
from safe_files import fsync_directory


FAULT_EVIDENCE_CONTRACT = "anime-fault-injection-evidence-v1"
FAULT_STATE_CONTRACT = "anime-acceptance-fault-claim-v1"
FAULT_STATE_SCHEMA_VERSION = 1
FAULT_EVIDENCE_SCHEMA_VERSION = 3

FAULT_SCENARIOS = (
    "worker_kill",
    "translation_timeout",
    "asr_process_crash",
    "gpu_oom",
    "model_unavailable",
    "output_publish_interrupt",
    "temporary_io_error",
    "temporary_database_busy",
    "mux_process_crash",
    "completed_publish_interrupt",
)
FAULT_TRIGGERS = {
    "worker_kill": "planned-only: after queue claim and before transcription",
    "translation_timeout": "planned-only: during the first translation request",
    "asr_process_crash": "planned-only: after ASR process start and before checkpoint commit",
    "gpu_oom": "planned-only: during GPU model inference",
    "model_unavailable": "planned-only: before the first model request",
    "output_publish_interrupt": "planned-only: after publication marker creation and before manifest commit",
    "temporary_io_error": "planned-only: during a checkpoint-safe output write",
    "temporary_database_busy": "planned-only: during scanner-ledger attempt persistence",
    "mux_process_crash": "planned-only: during completed-delivery mux",
    "completed_publish_interrupt": "planned-only: after completed marker creation and before receipt commit",
}
_FAULT_ROUTE_COMPATIBILITY = {
    "worker_kill": frozenset({"japanese_audio_asr"}),
    "translation_timeout": frozenset(
        {"japanese_subtitle_translation", "japanese_audio_asr"}
    ),
    "asr_process_crash": frozenset({"japanese_audio_asr"}),
    "gpu_oom": frozenset({"japanese_audio_asr"}),
    "model_unavailable": frozenset(
        {"japanese_subtitle_translation", "japanese_audio_asr"}
    ),
    "output_publish_interrupt": frozenset(
        {
            "existing_zh_tw",
            "zh_cn_opencc",
            "japanese_subtitle_translation",
            "japanese_audio_asr",
        }
    ),
    "temporary_io_error": frozenset(
        {
            "existing_zh_tw",
            "zh_cn_opencc",
            "japanese_subtitle_translation",
            "japanese_audio_asr",
        }
    ),
    "temporary_database_busy": frozenset(
        {
            "existing_zh_tw",
            "zh_cn_opencc",
            "japanese_subtitle_translation",
            "japanese_audio_asr",
        }
    ),
    "mux_process_crash": frozenset(
        {
            "existing_zh_tw",
            "zh_cn_opencc",
            "japanese_subtitle_translation",
            "japanese_audio_asr",
        }
    ),
    "completed_publish_interrupt": frozenset(
        {
            "existing_zh_tw",
            "zh_cn_opencc",
            "japanese_subtitle_translation",
            "japanese_audio_asr",
        }
    ),
}
_EVIDENCE_STAGE = {
    "worker_kill": "worker",
    "translation_timeout": "translation",
    "asr_process_crash": "transcription",
    "gpu_oom": "transcription",
    "model_unavailable": "model_request",
    "output_publish_interrupt": "output_publish",
    "temporary_io_error": "checkpoint_write",
    "temporary_database_busy": "scanner_ledger",
    "mux_process_crash": "mux",
    "completed_publish_interrupt": "completed_publish",
}
_FAILED_ATTEMPT_STATUSES = frozenset({"retryable_failure", "deferred", "failed"})
_RUN_ID = re.compile(r"accrun_[0-9a-f]{48}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_OBLIGATION_ID = re.compile(r"aiobl_[0-9a-f]{64}")
_ATTEMPT_ID = re.compile(r"aiatt_[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class AcceptanceFaultExecutorError(ValueError):
    """The opt-in acceptance fault contract cannot be loaded or trusted."""


class AcceptanceFaultAlreadyClaimedError(AcceptanceFaultExecutorError):
    """A planned fault already has a durable one-use claim."""


@dataclass(frozen=True)
class AcceptanceFault:
    case_id: str
    fault_id: str
    scenario: str
    trigger: str
    canonical_path: str
    obligation_id: str
    expected_route: str


@dataclass(frozen=True)
class AcceptanceFaultClaim:
    fault: AcceptanceFault
    state_path: Path
    claimed_at: float
    attempt_id: str
    attempt_number: int
    attempt_started_at: float
    attempt_binding_sha256: str


@dataclass(frozen=True)
class AcceptanceFaultExecutor:
    lane: AcceptanceQueueLane
    faults: tuple[AcceptanceFault, ...]
    state_root: Path
    evidence_root: Path

    @property
    def run_id(self) -> str:
        return self.lane.run_id

    @property
    def plan_sha256(self) -> str:
        return self.lane.plan_sha256

    def fault_for_id(self, fault_id: str) -> AcceptanceFault | None:
        normalized = str(fault_id or "")
        return next((fault for fault in self.faults if fault.fault_id == normalized), None)

    def fault_for_video(self, video: str | Path) -> AcceptanceFault | None:
        normalized = str(Path(video).resolve())
        return next(
            (fault for fault in self.faults if fault.canonical_path == normalized),
            None,
        )

    def claim_fault(
        self,
        fault_id: str,
        attempt: Mapping[str, Any],
        *,
        claimed_at: float | None = None,
    ) -> AcceptanceFaultClaim:
        fault = self._required_fault(fault_id)
        normalized = _normalize_attempt_identity(
            attempt,
            obligation_id=fault.obligation_id,
            acceptance_run_id=self.run_id,
        )
        if str(attempt.get("status") or "").casefold() != "running":
            raise AcceptanceFaultExecutorError("fault claim requires a running attempt")
        finished_at = attempt.get("finished_at")
        if finished_at not in (None, "", 0, 0.0):
            raise AcceptanceFaultExecutorError("running fault claim already has finished_at")
        injected = time.time() if claimed_at is None else _positive_float(
            claimed_at,
            "claimed_at",
        )
        if injected < normalized["started_at"]:
            raise AcceptanceFaultExecutorError("fault claim predates its bound attempt")
        binding_sha256 = attempt_binding_digest(
            plan_sha256=self.plan_sha256,
            acceptance_run_id=self.run_id,
            case_id=fault.case_id,
            fault_id=fault.fault_id,
            obligation_id=fault.obligation_id,
            attempt=attempt,
        )
        payload = {
            "contract": FAULT_STATE_CONTRACT,
            "schema_version": FAULT_STATE_SCHEMA_VERSION,
            "plan_sha256": self.plan_sha256,
            "acceptance_run_id": self.run_id,
            "suite_id": self.run_id,
            "case_id": fault.case_id,
            "obligation_id": fault.obligation_id,
            "fault_id": fault.fault_id,
            "scenario": fault.scenario,
            "trigger": fault.trigger,
            "state": "claimed",
            "claimed_at": injected,
            "attempt_binding": {
                "attempt_id": normalized["attempt_id"],
                "attempt_number": normalized["attempt_number"],
                "started_at": normalized["started_at"],
                "sha256": binding_sha256,
            },
        }
        _ensure_private_directory(self.state_root)
        state_path = self.state_root / f"{fault.fault_id}.json"
        try:
            _exclusive_write_json(state_path, payload)
        except FileExistsError as exc:
            raise AcceptanceFaultAlreadyClaimedError(
                f"acceptance fault already has a one-use claim: {fault.fault_id}"
            ) from exc
        return _claim_from_payload(state_path, payload, fault)

    def claim_fault_from_runtime_context(
        self,
        context: Any,
        *,
        claimed_at: float | None = None,
    ) -> AcceptanceFaultClaim:
        """Claim one fault from the immutable, ledger-verified child context."""

        def value(name: str) -> Any:
            if isinstance(context, Mapping):
                return context.get(name)
            return getattr(context, name, None)

        if (
            value("contract") != "anime-acceptance-attempt-context-v1"
            or value("schema_version") != 1
        ):
            raise AcceptanceFaultExecutorError("acceptance runtime context is unsupported")
        fault = self._required_fault(str(value("fault_id") or ""))
        expected = {
            "run_id": self.run_id,
            "plan_sha256": self.plan_sha256,
            "case_id": fault.case_id,
            "fault_scenario": fault.scenario,
            "canonical_path": fault.canonical_path,
            "obligation_id": fault.obligation_id,
        }
        for field, expected_value in expected.items():
            if value(field) != expected_value:
                raise AcceptanceFaultExecutorError(
                    f"acceptance runtime context {field} does not match the fixed plan"
                )
        return self.claim_fault(
            fault.fault_id,
            {
                "attempt_id": value("delivery_attempt_id"),
                "obligation_id": value("obligation_id"),
                "acceptance_run_id": value("run_id"),
                "attempt_number": value("attempt_number"),
                "started_at": value("started_at"),
                "status": "running",
            },
            claimed_at=claimed_at,
        )

    def load_fault_claim(self, fault_id: str) -> AcceptanceFaultClaim:
        fault = self._required_fault(fault_id)
        state_path = self.state_root / f"{fault.fault_id}.json"
        payload = _read_json_object(state_path, label="fault claim")
        return _claim_from_payload(state_path, payload, fault, executor=self)

    def write_recovery_evidence(
        self,
        fault_id: str,
        failed_attempt: Mapping[str, Any],
        recovery_attempt: Mapping[str, Any],
        *,
        checkpoint: str,
        observed_failure_stage: str | None = None,
    ) -> Path:
        fault = self._required_fault(fault_id)
        claim = self.load_fault_claim(fault_id)
        failed = _normalize_terminal_attempt(
            failed_attempt,
            obligation_id=fault.obligation_id,
            acceptance_run_id=self.run_id,
            allowed_statuses=_FAILED_ATTEMPT_STATUSES,
        )
        recovered = _normalize_terminal_attempt(
            recovery_attempt,
            obligation_id=fault.obligation_id,
            acceptance_run_id=self.run_id,
            allowed_statuses=frozenset({"succeeded"}),
        )
        if (
            failed["attempt_id"] != claim.attempt_id
            or failed["attempt_number"] != claim.attempt_number
            or failed["started_at"] != claim.attempt_started_at
        ):
            raise AcceptanceFaultExecutorError(
                "failed terminal attempt does not match the one-use fault claim"
            )
        expected_binding = attempt_binding_digest(
            plan_sha256=self.plan_sha256,
            acceptance_run_id=self.run_id,
            case_id=fault.case_id,
            fault_id=fault.fault_id,
            obligation_id=fault.obligation_id,
            attempt=failed_attempt,
        )
        if expected_binding != claim.attempt_binding_sha256:
            raise AcceptanceFaultExecutorError("failed attempt binding digest changed")
        if recovered["attempt_id"] == failed["attempt_id"]:
            raise AcceptanceFaultExecutorError("recovery must use a later distinct attempt")
        if recovered["attempt_number"] <= failed["attempt_number"]:
            raise AcceptanceFaultExecutorError("recovery attempt number is not later")
        if failed["finished_at"] < claim.claimed_at:
            raise AcceptanceFaultExecutorError("failed attempt finished before fault injection")
        if recovered["started_at"] < failed["finished_at"]:
            raise AcceptanceFaultExecutorError("recovery attempt started before failure was terminal")
        failure_stage = str(
            observed_failure_stage or _EVIDENCE_STAGE[fault.scenario]
        ).strip()
        failure_code = str(failed["error_code"] or "").strip()
        normalized_checkpoint = str(checkpoint or "").strip()
        if not failure_stage or not failure_code:
            raise AcceptanceFaultExecutorError(
                "fault evidence requires a failure stage and terminal error_code"
            )
        if not normalized_checkpoint:
            raise AcceptanceFaultExecutorError("fault recovery checkpoint is required")
        if fault.scenario in {"mux_process_crash", "completed_publish_interrupt"} and (
            normalized_checkpoint != "completed_delivery_committed"
        ):
            raise AcceptanceFaultExecutorError(
                "completed-delivery fault recovery checkpoint must be completed_delivery_committed"
            )
        failed_sha256 = terminal_attempt_row_sha256(
            failed_attempt,
            obligation_id=fault.obligation_id,
            acceptance_run_id=self.run_id,
        )
        recovery_sha256 = terminal_attempt_row_sha256(
            recovery_attempt,
            obligation_id=fault.obligation_id,
            acceptance_run_id=self.run_id,
        )
        payload = {
            "contract": FAULT_EVIDENCE_CONTRACT,
            "schema_version": FAULT_EVIDENCE_SCHEMA_VERSION,
            "suite_id": self.run_id,
            "acceptance_run_id": self.run_id,
            "case_id": fault.case_id,
            "obligation_id": fault.obligation_id,
            "fault_id": fault.fault_id,
            "scenario": fault.scenario,
            "trigger": fault.trigger,
            "injected_at": claim.claimed_at,
            "observed_failure": {
                "stage": failure_stage,
                "error_code": failure_code,
                "observed_at": failed["finished_at"],
            },
            "recovery": {
                "automatic": True,
                "started_at": recovered["started_at"],
                "completed_at": recovered["finished_at"],
                "checkpoint": normalized_checkpoint,
            },
            "attempt_binding": {
                "plan_sha256": self.plan_sha256,
                "claim_sha256": claim.attempt_binding_sha256,
                "failed_attempt": {
                    "attempt_id": failed["attempt_id"],
                    "row_sha256": failed_sha256,
                },
                "recovery_attempt": {
                    "attempt_id": recovered["attempt_id"],
                    "row_sha256": recovery_sha256,
                },
            },
            "manual_interventions": [],
        }
        _ensure_private_directory(self.evidence_root)
        evidence_path = self.evidence_root / f"{fault.fault_id}.json"
        try:
            _exclusive_write_json(evidence_path, payload)
        except FileExistsError:
            existing = _read_json_object(evidence_path, label="fault evidence")
            if existing != payload:
                raise AcceptanceFaultExecutorError(
                    f"fault evidence already exists with different content: {evidence_path}"
                )
        return evidence_path

    def _required_fault(self, fault_id: str) -> AcceptanceFault:
        normalized = str(fault_id or "")
        fault = self.fault_for_id(normalized)
        if fault is None:
            raise AcceptanceFaultExecutorError(
                f"fault is not in the fixed acceptance plan: {normalized}"
            )
        return fault


def acceptance_fault_execution_enabled(config: Any) -> bool:
    return bool(getattr(config, "acceptance_fault_execution_enabled", False))


def load_acceptance_fault_executor(config: Any) -> AcceptanceFaultExecutor | None:
    """Load a fixed schema-v3 executor contract without executing any fault."""

    if not acceptance_fault_execution_enabled(config):
        return None
    if not acceptance_queue_lane_enabled(config):
        raise AcceptanceFaultExecutorError(
            "acceptance fault execution requires the acceptance queue lane"
        )
    configured_run_id = str(
        getattr(config, "acceptance_fault_execution_run_id", "") or ""
    ).strip()
    configured_plan_sha256 = str(
        getattr(config, "acceptance_fault_execution_plan_sha256", "") or ""
    ).strip()
    if not _RUN_ID.fullmatch(configured_run_id):
        raise AcceptanceFaultExecutorError(
            "acceptance_fault_execution_run_id must be an explicit fresh run id"
        )
    if not _HEX64.fullmatch(configured_plan_sha256):
        raise AcceptanceFaultExecutorError(
            "acceptance_fault_execution_plan_sha256 must be explicit lowercase SHA-256"
        )
    lane = load_acceptance_queue_lane(config)
    if lane is None:
        raise AcceptanceFaultExecutorError("acceptance queue lane did not load")
    if configured_run_id != lane.run_id:
        raise AcceptanceFaultExecutorError("acceptance fault run id does not match the lane")
    if configured_plan_sha256 != lane.plan_sha256:
        raise AcceptanceFaultExecutorError(
            "acceptance fault plan SHA-256 does not match the immutable lane plan"
        )
    payload = _read_plan_bound_to_lane(lane)
    faults = _parse_faults(payload, lane)
    return AcceptanceFaultExecutor(
        lane=lane,
        faults=faults,
        state_root=lane.plan_path.parent / "fault-state",
        evidence_root=lane.plan_path.parent / "fault-evidence",
    )


def attempt_binding_digest(
    *,
    plan_sha256: str,
    acceptance_run_id: str,
    case_id: str,
    fault_id: str,
    obligation_id: str,
    attempt: Mapping[str, Any],
) -> str:
    normalized = _normalize_attempt_identity(
        attempt,
        obligation_id=obligation_id,
        acceptance_run_id=acceptance_run_id,
    )
    payload = {
        "contract": "anime-acceptance-fault-attempt-binding-v1",
        "plan_sha256": str(plan_sha256),
        "acceptance_run_id": str(acceptance_run_id),
        "case_id": str(case_id),
        "fault_id": str(fault_id),
        "obligation_id": str(obligation_id),
        "attempt_id": normalized["attempt_id"],
        "attempt_number": normalized["attempt_number"],
        "started_at": normalized["started_at"],
    }
    if not _HEX64.fullmatch(payload["plan_sha256"]):
        raise AcceptanceFaultExecutorError("attempt binding plan SHA-256 is invalid")
    if not _SAFE_ID.fullmatch(payload["case_id"]) or not _SAFE_ID.fullmatch(
        payload["fault_id"]
    ):
        raise AcceptanceFaultExecutorError("attempt binding case or fault id is invalid")
    return _canonical_sha256(payload)


def terminal_attempt_row_sha256(
    attempt: Mapping[str, Any],
    *,
    obligation_id: str,
    acceptance_run_id: str,
) -> str:
    normalized = _normalize_terminal_attempt(
        attempt,
        obligation_id=obligation_id,
        acceptance_run_id=acceptance_run_id,
        allowed_statuses=_FAILED_ATTEMPT_STATUSES | frozenset({"succeeded"}),
    )
    return _canonical_sha256(
        {
            "contract": "anime-ai-delivery-attempt-terminal-row-v1",
            **normalized,
        }
    )


def _read_plan_bound_to_lane(lane: AcceptanceQueueLane) -> dict[str, Any]:
    try:
        before = lane.plan_path.stat()
        raw = lane.plan_path.read_bytes()
        after = lane.plan_path.stat()
    except OSError as exc:
        raise AcceptanceFaultExecutorError(
            f"acceptance fault plan is unreadable: {lane.plan_path}"
        ) from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise AcceptanceFaultExecutorError("acceptance fault plan changed while being read")
    if hashlib.sha256(raw).hexdigest() != lane.plan_sha256:
        raise AcceptanceFaultExecutorError("acceptance fault plan changed after lane load")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceFaultExecutorError("acceptance fault plan is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AcceptanceFaultExecutorError("acceptance fault plan must be an object")
    return payload


def _parse_faults(
    payload: dict[str, Any],
    lane: AcceptanceQueueLane,
) -> tuple[AcceptanceFault, ...]:
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != len(lane.targets):
        raise AcceptanceFaultExecutorError("acceptance fault plan cases do not match the lane")
    parsed: list[AcceptanceFault] = []
    for ordinal, (case, target) in enumerate(zip(cases, lane.targets, strict=True)):
        if not isinstance(case, dict):
            raise AcceptanceFaultExecutorError(f"acceptance case {ordinal + 1} is invalid")
        case_id = str(case.get("case_id") or "")
        if not _SAFE_ID.fullmatch(case_id):
            raise AcceptanceFaultExecutorError(
                f"acceptance case {ordinal + 1} has an invalid case_id"
            )
        expected_route = str(case.get("expected_route") or "")
        raw_faults = case.get("faults")
        if not isinstance(raw_faults, list) or len(raw_faults) > 1:
            raise AcceptanceFaultExecutorError(
                f"acceptance case {case_id} must contain zero or one fault"
            )
        for raw_fault in raw_faults:
            if not isinstance(raw_fault, dict) or set(raw_fault) != {
                "fault_id",
                "scenario",
                "trigger",
            }:
                raise AcceptanceFaultExecutorError(
                    f"acceptance case {case_id} has malformed fault metadata"
                )
            fault_id = str(raw_fault.get("fault_id") or "")
            scenario = str(raw_fault.get("scenario") or "")
            trigger = str(raw_fault.get("trigger") or "")
            if not _SAFE_ID.fullmatch(fault_id):
                raise AcceptanceFaultExecutorError(
                    f"acceptance case {case_id} has an invalid fault_id"
                )
            if scenario not in FAULT_TRIGGERS or trigger != FAULT_TRIGGERS[scenario]:
                raise AcceptanceFaultExecutorError(
                    f"acceptance fault {fault_id} scenario/trigger is not fixed"
                )
            if expected_route not in _FAULT_ROUTE_COMPATIBILITY[scenario]:
                raise AcceptanceFaultExecutorError(
                    f"acceptance fault {fault_id} cannot reach its trigger route"
                )
            parsed.append(
                AcceptanceFault(
                    case_id=case_id,
                    fault_id=fault_id,
                    scenario=scenario,
                    trigger=trigger,
                    canonical_path=target.canonical_path,
                    obligation_id=target.obligation_id,
                    expected_route=expected_route,
                )
            )
    if len(parsed) != len(FAULT_SCENARIOS):
        raise AcceptanceFaultExecutorError(
            f"acceptance fault execution requires exactly {len(FAULT_SCENARIOS)} faults"
        )
    scenarios = [fault.scenario for fault in parsed]
    fault_ids = [fault.fault_id for fault in parsed]
    if set(scenarios) != set(FAULT_SCENARIOS) or len(set(scenarios)) != len(
        FAULT_SCENARIOS
    ):
        raise AcceptanceFaultExecutorError(
            "acceptance fault execution requires each fixed scenario exactly once"
        )
    if len(set(fault_ids)) != len(fault_ids):
        raise AcceptanceFaultExecutorError("acceptance fault ids must be unique")
    return tuple(parsed)


def _normalize_attempt_identity(
    attempt: Mapping[str, Any],
    *,
    obligation_id: str,
    acceptance_run_id: str,
) -> dict[str, Any]:
    expected_obligation = str(obligation_id)
    expected_run_id = str(acceptance_run_id)
    if not _OBLIGATION_ID.fullmatch(expected_obligation):
        raise AcceptanceFaultExecutorError("attempt obligation id is invalid")
    if not _RUN_ID.fullmatch(expected_run_id):
        raise AcceptanceFaultExecutorError("attempt acceptance run id is invalid")
    if str(attempt.get("obligation_id") or "") != expected_obligation:
        raise AcceptanceFaultExecutorError("attempt obligation does not match the fault")
    if str(attempt.get("acceptance_run_id") or "") != expected_run_id:
        raise AcceptanceFaultExecutorError("attempt acceptance run id does not match")
    attempt_id = str(attempt.get("attempt_id") or "")
    if not _ATTEMPT_ID.fullmatch(attempt_id):
        raise AcceptanceFaultExecutorError("attempt_id must be a stable SHA-256 id")
    try:
        attempt_number = int(attempt.get("attempt_number"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AcceptanceFaultExecutorError("attempt_number is invalid") from exc
    if attempt_number <= 0:
        raise AcceptanceFaultExecutorError("attempt_number must be positive")
    started_at = _positive_float(attempt.get("started_at"), "attempt started_at")
    return {
        "attempt_id": attempt_id,
        "obligation_id": expected_obligation,
        "acceptance_run_id": expected_run_id,
        "attempt_number": attempt_number,
        "started_at": started_at,
    }


def _normalize_terminal_attempt(
    attempt: Mapping[str, Any],
    *,
    obligation_id: str,
    acceptance_run_id: str,
    allowed_statuses: frozenset[str],
) -> dict[str, Any]:
    normalized = _normalize_attempt_identity(
        attempt,
        obligation_id=obligation_id,
        acceptance_run_id=acceptance_run_id,
    )
    status = str(attempt.get("status") or "").casefold()
    if status not in allowed_statuses:
        raise AcceptanceFaultExecutorError(
            f"attempt status is not an allowed terminal state: {status}"
        )
    finished_at = _positive_float(attempt.get("finished_at"), "attempt finished_at")
    if finished_at < normalized["started_at"]:
        raise AcceptanceFaultExecutorError("attempt finished before it started")
    return {
        **normalized,
        "status": status,
        "stage": str(attempt.get("stage") or ""),
        "error_code": str(attempt.get("error_code") or ""),
        "detail": str(attempt.get("detail") or ""),
        "finished_at": finished_at,
    }


def _claim_from_payload(
    path: Path,
    payload: dict[str, Any],
    fault: AcceptanceFault,
    *,
    executor: AcceptanceFaultExecutor | None = None,
) -> AcceptanceFaultClaim:
    expected = {
        "contract": FAULT_STATE_CONTRACT,
        "schema_version": FAULT_STATE_SCHEMA_VERSION,
        "case_id": fault.case_id,
        "obligation_id": fault.obligation_id,
        "fault_id": fault.fault_id,
        "scenario": fault.scenario,
        "trigger": fault.trigger,
        "state": "claimed",
    }
    if set(payload) != {
        "contract",
        "schema_version",
        "plan_sha256",
        "acceptance_run_id",
        "suite_id",
        "case_id",
        "obligation_id",
        "fault_id",
        "scenario",
        "trigger",
        "state",
        "claimed_at",
        "attempt_binding",
    }:
        raise AcceptanceFaultExecutorError("fault claim fields are not exact")
    for key, value in expected.items():
        if payload.get(key) != value:
            raise AcceptanceFaultExecutorError(f"fault claim {key} does not match")
    if executor is not None and (
        payload.get("plan_sha256") != executor.plan_sha256
        or payload.get("acceptance_run_id") != executor.run_id
        or payload.get("suite_id") != executor.run_id
    ):
        raise AcceptanceFaultExecutorError("fault claim run or plan binding does not match")
    binding = payload.get("attempt_binding")
    if not isinstance(binding, dict) or set(binding) != {
        "attempt_id",
        "attempt_number",
        "started_at",
        "sha256",
    }:
        raise AcceptanceFaultExecutorError("fault claim attempt binding is malformed")
    attempt_id = str(binding.get("attempt_id") or "")
    digest = str(binding.get("sha256") or "")
    if not _ATTEMPT_ID.fullmatch(attempt_id) or not _HEX64.fullmatch(digest):
        raise AcceptanceFaultExecutorError("fault claim attempt binding ids are invalid")
    try:
        attempt_number = int(binding.get("attempt_number"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AcceptanceFaultExecutorError("fault claim attempt number is invalid") from exc
    if attempt_number <= 0:
        raise AcceptanceFaultExecutorError("fault claim attempt number must be positive")
    return AcceptanceFaultClaim(
        fault=fault,
        state_path=path,
        claimed_at=_positive_float(payload.get("claimed_at"), "fault claimed_at"),
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        attempt_started_at=_positive_float(binding.get("started_at"), "attempt started_at"),
        attempt_binding_sha256=digest,
    )


def _exclusive_write_json(path: Path, payload: dict[str, Any]) -> None:
    content = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AcceptanceFaultExecutorError(f"{label} is unreadable: {path}") from exc
    if len(raw) > 1024 * 1024:
        raise AcceptanceFaultExecutorError(f"{label} exceeds the 1 MiB safety limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceFaultExecutorError(f"{label} is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise AcceptanceFaultExecutorError(f"{label} must be a JSON object")
    return payload


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise AcceptanceFaultExecutorError(
            f"acceptance fault directory is not a trusted directory: {path}"
        )


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _positive_float(value: Any, label: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AcceptanceFaultExecutorError(f"{label} is invalid") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise AcceptanceFaultExecutorError(f"{label} must be finite and positive")
    return normalized
