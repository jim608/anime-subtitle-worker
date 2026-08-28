from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


ACCEPTANCE_PLAN_CONTRACT = "anime-unattended-acceptance-plan-v1"
ACCEPTANCE_PLAN_SCHEMA_VERSION = 3
ACCEPTANCE_QUEUE_TARGET_COUNT = 100
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RUN_ID = re.compile(r"accrun_[0-9a-f]{48}")
_OBLIGATION_ID = re.compile(r"aiobl_[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class AcceptanceQueueLaneError(ValueError):
    """The opt-in acceptance lane cannot safely identify its fixed targets."""


@dataclass(frozen=True)
class AcceptanceQueueTarget:
    ordinal: int
    canonical_path: str
    media_size: int
    media_mtime_ns: int
    media_fingerprint: str
    policy_revision: str
    obligation_id: str
    source_sha256: str
    case_id: str = ""
    fault_id: str = ""
    fault_scenario: str = ""


@dataclass(frozen=True)
class AcceptanceQueueLane:
    run_id: str
    nonce: str
    plan_path: Path
    plan_sha256: str
    targets: tuple[AcceptanceQueueTarget, ...]

    def target_for_path(self, path: str | Path) -> AcceptanceQueueTarget | None:
        normalized = str(Path(path).resolve())
        return next(
            (target for target in self.targets if target.canonical_path == normalized),
            None,
        )


def acceptance_queue_lane_enabled(config: Any) -> bool:
    return bool(getattr(config, "acceptance_queue_lane_enabled", False))


def acceptance_run_id_for_video(config: Any, video: str | Path) -> str:
    """Return the exact lane run id, or an empty string when the lane is disabled."""

    lane = load_acceptance_queue_lane(config)
    if lane is None:
        return ""
    if lane.target_for_path(video) is None:
        raise AcceptanceQueueLaneError(
            f"refusing acceptance evidence for a non-allowlisted path: {video}"
        )
    return lane.run_id


def load_acceptance_queue_lane(config: Any) -> AcceptanceQueueLane | None:
    """Load the immutable schema-v3 pre-admission plan only when explicitly enabled."""

    if not acceptance_queue_lane_enabled(config):
        return None
    configured = str(getattr(config, "acceptance_queue_lane_plan_path", "") or "").strip()
    if not configured:
        raise AcceptanceQueueLaneError(
            "acceptance_queue_lane_plan_path is required while the acceptance lane is enabled"
        )
    plan_path = Path(configured)
    if not plan_path.is_absolute():
        plan_path = Path(config.work_path) / plan_path
    plan_path = plan_path.resolve()
    try:
        before = plan_path.stat()
        raw = plan_path.read_bytes()
        after = plan_path.stat()
    except OSError as exc:
        raise AcceptanceQueueLaneError(f"acceptance lane plan is unreadable: {plan_path}") from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise AcceptanceQueueLaneError("acceptance lane plan changed while it was being read")
    if len(raw) > 10 * 1024 * 1024:
        raise AcceptanceQueueLaneError("acceptance lane plan exceeds the 10 MiB safety limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceQueueLaneError("acceptance lane plan is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise AcceptanceQueueLaneError("acceptance lane plan must be a JSON object")
    if payload.get("contract") != ACCEPTANCE_PLAN_CONTRACT:
        raise AcceptanceQueueLaneError("acceptance lane plan contract is unsupported")
    if payload.get("schema_version") != ACCEPTANCE_PLAN_SCHEMA_VERSION:
        raise AcceptanceQueueLaneError("acceptance lane requires a schema-v3 pre-admission plan")
    run_id = str(payload.get("run_id") or "")
    if not _RUN_ID.fullmatch(run_id) or payload.get("suite_id") != run_id:
        raise AcceptanceQueueLaneError("acceptance lane suite_id/run_id binding is invalid")
    nonce = str(payload.get("nonce") or "")
    if not _HEX64.fullmatch(nonce):
        raise AcceptanceQueueLaneError("acceptance lane nonce must be 64 lowercase hex characters")
    if not isinstance(payload.get("pre_admission"), dict):
        raise AcceptanceQueueLaneError("acceptance lane plan is missing pre_admission evidence")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != ACCEPTANCE_QUEUE_TARGET_COUNT:
        raise AcceptanceQueueLaneError(
            f"acceptance lane requires exactly {ACCEPTANCE_QUEUE_TARGET_COUNT} cases"
        )

    input_root = Path(config.input_path).resolve()
    targets: list[AcceptanceQueueTarget] = []
    for ordinal, case in enumerate(cases):
        media = case.get("media") if isinstance(case, dict) else None
        if not isinstance(media, dict):
            raise AcceptanceQueueLaneError(f"acceptance case {ordinal + 1} has no media identity")
        target = _parse_target(
            media,
            ordinal=ordinal,
            input_root=input_root,
            case=case,
        )
        targets.append(target)
    _validate_unique_targets(targets)
    return AcceptanceQueueLane(
        run_id=run_id,
        nonce=nonce,
        plan_path=plan_path,
        plan_sha256=hashlib.sha256(raw).hexdigest(),
        targets=tuple(targets),
    )


def verify_acceptance_queue_target_source(
    target: AcceptanceQueueTarget,
    config: Any,
) -> None:
    """Hash and re-stat one target before its durable queue claim."""

    from output_manifest import delivery_identity
    from safe_files import sha256_file

    video = Path(target.canonical_path)
    try:
        before = delivery_identity(video, config)
        digest = sha256_file(video)
        after = delivery_identity(video, config)
    except (OSError, TypeError, ValueError) as exc:
        raise AcceptanceQueueLaneError(
            f"acceptance target identity is unreadable: {target.canonical_path}"
        ) from exc
    for identity in (before, after):
        media = identity.get("media") if isinstance(identity, dict) else None
        if not isinstance(media, dict) or any(
            (
                int(media.get("media_size") or 0) != target.media_size,
                int(media.get("media_mtime_ns") or 0) != target.media_mtime_ns,
                str(media.get("media_fingerprint") or "") != target.media_fingerprint,
                str(identity.get("policy_revision") or "") != target.policy_revision,
                str(identity.get("obligation_id") or "") != target.obligation_id,
            )
        ):
            raise AcceptanceQueueLaneError(
                f"acceptance target identity drifted before claim: {target.canonical_path}"
            )
    if digest != target.source_sha256:
        raise AcceptanceQueueLaneError(
            f"acceptance target source SHA-256 mismatch: {target.canonical_path}"
        )


def _parse_target(
    media: dict[str, Any],
    *,
    ordinal: int,
    input_root: Path,
    case: dict[str, Any],
) -> AcceptanceQueueTarget:
    raw_path = str(media.get("canonical_path") or "").strip()
    path = Path(raw_path)
    if not path.is_absolute():
        raise AcceptanceQueueLaneError(f"acceptance case {ordinal + 1} path is not absolute")
    canonical_path = str(path.resolve())
    try:
        Path(canonical_path).relative_to(input_root)
    except ValueError as exc:
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} path is outside input_path"
        ) from exc
    try:
        media_size = int(media.get("media_size") or 0)
        media_mtime_ns = int(media.get("media_mtime_ns") or 0)
    except (TypeError, ValueError) as exc:
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} has invalid numeric identity fields"
        ) from exc
    fingerprint = str(media.get("media_fingerprint") or "")
    policy_revision = str(media.get("policy_revision") or "")
    obligation_id = str(media.get("obligation_id") or "")
    source_sha256 = str(media.get("source_sha256") or "")
    case_id = str(case.get("case_id") or "")
    if not _SAFE_ID.fullmatch(case_id):
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} has an invalid case_id"
        )
    faults = case.get("faults", [])
    if not isinstance(faults, list) or len(faults) > 1:
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} must have zero or one planned fault"
        )
    fault_id = ""
    fault_scenario = ""
    if faults:
        fault = faults[0]
        if not isinstance(fault, dict):
            raise AcceptanceQueueLaneError(
                f"acceptance case {ordinal + 1} has an invalid planned fault"
            )
        fault_id = str(fault.get("fault_id") or "")
        fault_scenario = str(fault.get("scenario") or "")
        if not _SAFE_ID.fullmatch(fault_id) or not _SAFE_ID.fullmatch(fault_scenario):
            raise AcceptanceQueueLaneError(
                f"acceptance case {ordinal + 1} has invalid fault identity"
            )
    if media_size <= 0 or media_mtime_ns <= 0:
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} has non-positive media identity fields"
        )
    if not _HEX64.fullmatch(fingerprint) or not _HEX64.fullmatch(policy_revision):
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} has invalid fingerprint or policy revision"
        )
    if not _OBLIGATION_ID.fullmatch(obligation_id):
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} has an invalid obligation_id"
        )
    if not _HEX64.fullmatch(source_sha256):
        raise AcceptanceQueueLaneError(
            f"acceptance case {ordinal + 1} has an invalid source_sha256"
        )
    return AcceptanceQueueTarget(
        ordinal=ordinal,
        canonical_path=canonical_path,
        media_size=media_size,
        media_mtime_ns=media_mtime_ns,
        media_fingerprint=fingerprint,
        policy_revision=policy_revision,
        obligation_id=obligation_id,
        source_sha256=source_sha256,
        case_id=case_id,
        fault_id=fault_id,
        fault_scenario=fault_scenario,
    )


def _validate_unique_targets(targets: list[AcceptanceQueueTarget]) -> None:
    for label, values in (
        ("case_id", [target.case_id for target in targets]),
        ("canonical_path", [target.canonical_path.casefold() for target in targets]),
        ("media_fingerprint", [target.media_fingerprint for target in targets]),
        ("obligation_id", [target.obligation_id for target in targets]),
        ("source_sha256", [target.source_sha256 for target in targets]),
    ):
        if len(set(values)) != ACCEPTANCE_QUEUE_TARGET_COUNT:
            raise AcceptanceQueueLaneError(f"acceptance lane {label} values must be unique")
    fault_ids = [target.fault_id for target in targets if target.fault_id]
    if len(fault_ids) != len(set(fault_ids)):
        raise AcceptanceQueueLaneError("acceptance lane fault_id values must be unique")
