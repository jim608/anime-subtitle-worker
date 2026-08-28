from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any

from output_manifest import delivery_identity, output_manifest_path
from processing_provenance import provenance_path_for_video
from safe_files import sha256_file

from .harness import (
    AcceptanceInputError,
    COMPLETED_DELIVERY_FAULT_SCENARIOS,
    FRESH_PLAN_SCHEMA_VERSION,
    OBSERVATION_CONTRACT,
    ROUTES,
    _COMPLETED_FAULT_LEDGER_STAGE,
    _fault_attempt_recovery_error,
    _provenance_route,
    _verify_structured_fault_evidence,
    read_json_object,
    validate_plan,
    validate_plan_structure,
)


MISSING_SHA256 = "0" * 64
_HEX64 = frozenset("0123456789abcdef")


def collect_observations(
    plan_path: str | Path,
    config: Any,
    *,
    collected_at: float | None = None,
) -> dict[str, Any]:
    """Collect durable acceptance observations without running the pipeline.

    The collector only reads the fixed plan, current source/artifact files and
    scanner ledger.  It deliberately performs no fault injection and does not
    start or enqueue Worker.  Qualification remains the responsibility of
    :func:`acceptance.harness.evaluate_acceptance`.
    """

    plan_file = Path(plan_path)
    plan = read_json_object(plan_file)
    structure_errors = validate_plan_structure(plan)
    if plan.get("schema_version") == FRESH_PLAN_SCHEMA_VERSION:
        structure_errors = validate_plan(plan, config)
    if structure_errors:
        preview = "; ".join(structure_errors[:8])
        suffix = f"; plus {len(structure_errors) - 8} more" if len(structure_errors) > 8 else ""
        raise AcceptanceInputError(f"plan is not collectable: {preview}{suffix}")

    observed_at = _positive_timestamp(collected_at) or time.time()
    plan_digest = sha256_file(plan_file)
    plan_reference = {
        "kind": "acceptance_plan",
        "path": str(plan_file),
        "sha256": plan_digest,
    }
    fault_root = plan_file.parent / "fault-evidence"
    cases: list[dict[str, Any]] = []

    with _ReadonlyLedger(config) as ledger:
        for planned in plan["cases"]:
            cases.append(
                _collect_case(
                    planned,
                    config,
                    suite_id=str(plan["suite_id"]),
                    plan_schema_version=int(plan["schema_version"]),
                    acceptance_run_id=str(plan.get("run_id") or ""),
                    plan_created_at=(
                        float(plan["created_at"])
                        if plan.get("schema_version") == FRESH_PLAN_SCHEMA_VERSION
                        else None
                    ),
                    plan_reference=plan_reference,
                    fault_root=fault_root,
                    ledger=ledger,
                    collected_at=observed_at,
                )
            )

    starts = [float(item["started_at"]) for item in cases]
    finishes = [float(item["finished_at"]) for item in cases]
    result = {
        "contract": OBSERVATION_CONTRACT,
        "schema_version": int(plan["schema_version"]),
        "suite_id": str(plan["suite_id"]),
        "plan_sha256": plan_digest,
        "started_at": min(starts) if starts else observed_at,
        "finished_at": max(finishes) if finishes else observed_at,
        "manual_interventions": [],
        "cases": cases,
    }
    if plan.get("schema_version") == FRESH_PLAN_SCHEMA_VERSION:
        result["run_id"] = str(plan.get("run_id") or "")
    return result


def observation_summary(observations: dict[str, Any]) -> dict[str, Any]:
    cases = observations.get("cases")
    cases = cases if isinstance(cases, list) else []
    completed = sum(
        1 for item in cases if isinstance(item, dict) and item.get("outcome") == "completed"
    )
    failed = sum(
        1 for item in cases if isinstance(item, dict) and item.get("outcome") == "failed"
    )
    reviews = sum(
        1
        for item in cases
        if isinstance(item, dict) and item.get("outcome") == "review_required"
    )
    planned_faults = 0
    recovered_faults = 0
    for item in cases:
        if not isinstance(item, dict):
            continue
        faults = item.get("faults")
        faults = faults if isinstance(faults, list) else []
        planned_faults += len(faults)
        recovered_faults += sum(
            1
            for fault in faults
            if isinstance(fault, dict) and fault.get("status") == "recovered"
        )
    return {
        "cases": len(cases),
        "completed": completed,
        "failed": failed,
        "review_required": reviews,
        "planned_faults": planned_faults,
        "recovered_faults": recovered_faults,
        "complete": bool(cases)
        and completed == len(cases)
        and recovered_faults == planned_faults,
    }


def _collect_case(
    planned: dict[str, Any],
    config: Any,
    *,
    suite_id: str,
    plan_schema_version: int,
    acceptance_run_id: str = "",
    plan_created_at: float | None = None,
    plan_reference: dict[str, Any],
    fault_root: Path,
    ledger: "_ReadonlyLedger",
    collected_at: float,
) -> dict[str, Any]:
    case_id = str(planned.get("case_id") or "")
    media = planned.get("media") if isinstance(planned.get("media"), dict) else {}
    video = Path(str(media.get("canonical_path") or ""))
    obligation_id = str(media.get("obligation_id") or "")
    expected_route = str(planned.get("expected_route") or "")
    errors: list[str] = []
    manual_interventions: list[Any] = []
    time_values: list[float] = []
    evidence: list[dict[str, Any]] = []
    fresh_case = plan_schema_version == FRESH_PLAN_SCHEMA_VERSION

    try:
        identity = delivery_identity(video, config)
        expected_identity = {
            "obligation_id": obligation_id,
            "policy_revision": media.get("policy_revision"),
            "media": {
                "canonical_path": media.get("canonical_path"),
                "media_fingerprint": media.get("media_fingerprint"),
                "media_size": media.get("media_size"),
                "media_mtime_ns": media.get("media_mtime_ns"),
            },
        }
        if identity != expected_identity:
            errors.append("source_identity_mismatch")
    except (OSError, TypeError, ValueError):
        errors.append("source_missing_or_unreadable")

    manifest_file = output_manifest_path(video, config)
    manifest, manifest_digest = _read_artifact_json(
        manifest_file,
        kind="output_manifest",
        evidence=evidence,
        errors=errors,
        missing_error="output_manifest_missing_or_unreadable",
    )
    if isinstance(manifest, dict):
        if fresh_case and str(manifest.get("acceptance_run_id") or "") != acceptance_run_id:
            errors.append("output_manifest_acceptance_run_id_mismatch")
        _add_timestamp(time_values, manifest.get("completed_at"))
        if fresh_case:
            _require_fresh_timestamp(
                manifest.get("completed_at"),
                plan_created_at,
                "output_manifest_completed_before_plan",
                errors,
            )
        delivery = manifest.get("delivery")
        if not isinstance(delivery, dict):
            errors.append("output_manifest_delivery_missing")
        else:
            _add_timestamp(time_values, delivery.get("verified_at"))
            if fresh_case:
                _require_fresh_timestamp(
                    delivery.get("verified_at"),
                    plan_created_at,
                    "output_manifest_verified_before_plan",
                    errors,
                )
            if delivery.get("obligation_id") != obligation_id:
                errors.append("output_manifest_obligation_mismatch")
            if delivery.get("policy_revision") != media.get("policy_revision"):
                errors.append("output_manifest_policy_mismatch")
        quality_gate = manifest.get("quality_gate")
        if not isinstance(quality_gate, dict) or quality_gate.get("passed") is not True:
            errors.append("output_manifest_quality_gate_missing")
        if not isinstance(manifest.get("outputs"), list) or not manifest.get("outputs"):
            errors.append("output_manifest_outputs_missing")

    provenance_file = provenance_path_for_video(config, video)
    provenance, _ = _read_artifact_json(
        provenance_file,
        kind="processing_provenance",
        evidence=evidence,
        errors=errors,
        missing_error="processing_provenance_missing_or_unreadable",
    )
    actual_route = ""
    if isinstance(provenance, dict):
        if fresh_case and str(provenance.get("acceptance_run_id") or "") != acceptance_run_id:
            errors.append("processing_provenance_acceptance_run_id_mismatch")
        for field in ("created_at", "run_started_at", "updated_at", "finished_at"):
            _add_timestamp(time_values, provenance.get(field))
        if fresh_case:
            for field in ("run_started_at", "finished_at"):
                _require_fresh_timestamp(
                    provenance.get(field),
                    plan_created_at,
                    f"processing_provenance_{field}_before_plan",
                    errors,
                )
        if provenance.get("status") != "complete":
            errors.append("processing_provenance_not_complete")
        if str(provenance.get("video_path") or "") != str(video):
            errors.append("processing_provenance_video_mismatch")
        if provenance.get("config_signature") != media.get("policy_revision"):
            errors.append("processing_provenance_policy_mismatch")
        actual_route = _provenance_route(provenance, manifest)
        if actual_route != expected_route:
            errors.append("processing_route_mismatch")
    else:
        errors.append("processing_route_unavailable")

    ledger_row, attempts, ledger_error = ledger.read(obligation_id)
    if ledger_error:
        errors.append(f"delivery_ledger:{ledger_error}")
    elif ledger_row is None:
        errors.append("delivery_ledger:obligation_missing")
    else:
        for field in ("eligible_at", "verified_at"):
            _add_timestamp(time_values, ledger_row.get(field))
            if fresh_case:
                _require_fresh_timestamp(
                    ledger_row.get(field),
                    plan_created_at,
                    f"delivery_ledger_{field}_before_plan",
                    errors,
                )
        if fresh_case and str(ledger_row.get("acceptance_run_id") or "") != acceptance_run_id:
            errors.append("delivery_ledger_acceptance_run_id_mismatch")
        for attempt in attempts:
            if fresh_case and str(attempt.get("acceptance_run_id") or "") != acceptance_run_id:
                errors.append("delivery_attempt_acceptance_run_id_mismatch")
            _add_timestamp(time_values, attempt.get("started_at"))
            _add_timestamp(time_values, attempt.get("finished_at"))
            if fresh_case:
                for field in ("started_at", "finished_at"):
                    _require_fresh_timestamp(
                        attempt.get(field),
                        plan_created_at,
                        f"delivery_attempt_{field}_before_plan",
                        errors,
                    )
        _check_ledger(
            ledger_row,
            attempts,
            media=media,
            manifest_file=manifest_file,
            manifest_digest=manifest_digest,
            errors=errors,
        )

    planned_completed = planned.get("completed_delivery")
    completed_observation: dict[str, Any] | None = None
    completed_ok = True
    committed_at: float | None = None
    if isinstance(planned_completed, dict):
        completed_observation, completed_errors, committed_at = _collect_completed_delivery(
            planned_completed,
            media=media,
            manifest_file=manifest_file,
            manifest_digest=manifest_digest,
            acceptance_run_id=acceptance_run_id if fresh_case else "",
            plan_created_at=plan_created_at,
        )
        errors.extend(completed_errors)
        completed_ok = not completed_errors
        _add_timestamp(time_values, committed_at)

    raw_faults: list[tuple[dict[str, Any], Path, dict[str, Any] | None, dict[str, Any]]] = []
    for fault in planned.get("faults", []):
        if not isinstance(fault, dict):
            continue
        fault_id = str(fault.get("fault_id") or "")
        fault_file = fault_root / f"{fault_id}.json"
        payload: dict[str, Any] | None = None
        reference = _missing_reference(fault_file, "fault_injection_event")
        try:
            payload = read_json_object(fault_file)
            reference = {
                "kind": "fault_injection_event",
                "path": str(fault_file),
                "sha256": sha256_file(fault_file),
            }
        except (AcceptanceInputError, OSError):
            pass
        if isinstance(payload, dict):
            _add_timestamp(time_values, payload.get("injected_at"))
            failure = payload.get("observed_failure")
            if isinstance(failure, dict):
                _add_timestamp(time_values, failure.get("observed_at"))
            recovery = payload.get("recovery")
            if isinstance(recovery, dict):
                _add_timestamp(time_values, recovery.get("started_at"))
                _add_timestamp(time_values, recovery.get("completed_at"))
            interventions = payload.get("manual_interventions")
            if isinstance(interventions, list) and interventions:
                manual_interventions.extend(interventions)
        raw_faults.append((fault, fault_file, payload, reference))

    if time_values:
        started_at = min(time_values)
        finished_at = max(time_values)
    else:
        started_at = collected_at
        finished_at = collected_at

    observed_faults: list[dict[str, Any]] = []
    for fault, fault_file, payload, reference in raw_faults:
        fault_id = str(fault.get("fault_id") or "")
        injected_at = (
            _positive_timestamp(payload.get("injected_at"))
            if isinstance(payload, dict)
            else None
        )
        recovery = payload.get("recovery") if isinstance(payload, dict) else None
        recovered_at = (
            _positive_timestamp(recovery.get("completed_at"))
            if isinstance(recovery, dict)
            else None
        )
        # The v1/v2 observation contract requires injected_at even when the
        # evidence is absent.  The case start is a fail-closed schema sentinel;
        # status remains not_recovered and therefore can never qualify.
        fault_observation = {
            "fault_id": fault_id,
            "status": "not_recovered",
            "injected_at": injected_at or started_at,
            "recovered_at": recovered_at,
            "evidence": [reference],
        }
        fault_errors: list[str] = []
        if payload is None:
            fault_errors.append("evidence_missing_or_unreadable")
        else:
            structured_error = _verify_structured_fault_evidence(
                fault_file,
                suite_id=suite_id,
                case_id=case_id,
                obligation_id=obligation_id,
                fault=fault,
                observation=fault_observation,
                plan_schema_version=plan_schema_version,
                acceptance_run_id=acceptance_run_id,
                plan_sha256=str(plan_reference.get("sha256") or ""),
                attempts=attempts,
            )
            if structured_error:
                fault_errors.append(structured_error)
        attempt_error = _fault_attempt_recovery_error(
            attempts,
            fault_observation,
            expected_stage=_COMPLETED_FAULT_LEDGER_STAGE.get(str(fault.get("scenario") or "")),
        )
        if attempt_error:
            fault_errors.append(attempt_error)
        if str(fault.get("scenario") or "") in COMPLETED_DELIVERY_FAULT_SCENARIOS:
            if not completed_ok or committed_at is None:
                fault_errors.append("completed_delivery_not_committed")
            elif (
                injected_at is None
                or recovered_at is None
                or not (injected_at <= committed_at <= recovered_at)
            ):
                fault_errors.append("completed_delivery_commit_outside_recovery_window")
        if not fault_errors:
            fault_observation["status"] = "recovered"
        else:
            errors.extend(f"fault:{fault_id}:{value}" for value in fault_errors)
        observed_faults.append(fault_observation)

    if not evidence:
        evidence.append(dict(plan_reference))
    review_required = bool(
        isinstance(ledger_row, dict)
        and (
            str(ledger_row.get("state") or "").casefold() in {"needs_review", "review_required"}
            or "review" in str(ledger_row.get("outcome_code") or "").casefold()
        )
    )
    route = actual_route if actual_route in ROUTES else expected_route
    outcome = "completed"
    if review_required:
        outcome = "review_required"
    elif errors or manual_interventions:
        outcome = "failed"
    result: dict[str, Any] = {
        "case_id": case_id,
        "canonical_path": str(media.get("canonical_path") or ""),
        "obligation_id": obligation_id,
        "route": route,
        "started_at": started_at,
        "finished_at": finished_at,
        "outcome": outcome,
        "review_required": review_required,
        "manual_interventions": manual_interventions,
        "errors": _deduplicate(errors),
        "evidence": evidence,
        "faults": observed_faults,
    }
    if fresh_case:
        result["acceptance_run_id"] = acceptance_run_id
    if completed_observation is not None:
        result["completed_delivery"] = completed_observation
    return result


def _collect_completed_delivery(
    planned: dict[str, Any],
    *,
    media: dict[str, Any],
    manifest_file: Path,
    manifest_digest: str,
    acceptance_run_id: str = "",
    plan_created_at: float | None = None,
) -> tuple[dict[str, Any], list[str], float | None]:
    receipt_file = Path(str(planned.get("receipt_path") or ""))
    destination = Path(str(planned.get("destination") or ""))
    errors: list[str] = []
    receipt: dict[str, Any] | None = None
    receipt_reference = _missing_reference(receipt_file, "completed_delivery_receipt")
    try:
        receipt = read_json_object(receipt_file)
        receipt_reference["sha256"] = sha256_file(receipt_file)
    except (AcceptanceInputError, OSError):
        errors.append("completed_delivery_receipt_missing_or_unreadable")

    output_digest = ""
    committed_at: float | None = None
    if isinstance(receipt, dict):
        if acceptance_run_id and str(receipt.get("acceptance_run_id") or "") != acceptance_run_id:
            errors.append("completed_delivery_acceptance_run_id_mismatch")
        committed_at = _positive_timestamp(receipt.get("committed_at"))
        if plan_created_at is not None and (
            committed_at is None or committed_at < plan_created_at
        ):
            errors.append("completed_delivery_committed_before_plan")
        if receipt.get("state") != "committed" or committed_at is None:
            errors.append("completed_delivery_receipt_not_committed")
        if receipt.get("source_retained") is not True:
            errors.append("completed_delivery_source_not_retained")
        source = receipt.get("source")
        if not isinstance(source, dict) or source.get("sha256") != planned.get("source_sha256"):
            errors.append("completed_delivery_source_hash_binding_mismatch")
        delivery = receipt.get("delivery")
        if not isinstance(delivery, dict) or (
            delivery.get("obligation_id") != media.get("obligation_id")
            or delivery.get("policy_revision") != media.get("policy_revision")
        ):
            errors.append("completed_delivery_identity_mismatch")
        publication_manifest = receipt.get("publication_manifest")
        try:
            expected_manifest_path = str(manifest_file.resolve())
        except OSError:
            expected_manifest_path = str(manifest_file)
        if not isinstance(publication_manifest, dict) or (
            publication_manifest.get("path") != expected_manifest_path
            or publication_manifest.get("sha256") != manifest_digest
        ):
            errors.append("completed_delivery_manifest_binding_mismatch")
        if str(receipt.get("destination") or "") != str(destination):
            errors.append("completed_delivery_destination_mismatch")
        output = receipt.get("output")
        if isinstance(output, dict):
            candidate = str(output.get("sha256") or "")
            if _is_sha256(candidate):
                output_digest = candidate
            else:
                errors.append("completed_delivery_output_hash_missing")
            if str(output.get("path") or "") != str(destination):
                errors.append("completed_delivery_output_path_mismatch")
            try:
                stat = destination.stat()
                if int(output.get("size") or -1) != int(stat.st_size):
                    errors.append("completed_delivery_output_size_mismatch")
                if int(output.get("mtime_ns") or -1) != int(stat.st_mtime_ns):
                    errors.append("completed_delivery_output_mtime_mismatch")
            except (OSError, TypeError, ValueError):
                errors.append("completed_delivery_output_missing_or_unreadable")
        else:
            errors.append("completed_delivery_output_evidence_missing")
    elif destination.is_file():
        errors.append("completed_delivery_unreceipted_output")

    if not destination.is_file() and "completed_delivery_output_missing_or_unreadable" not in errors:
        errors.append("completed_delivery_output_missing_or_unreadable")
    final_reference = {
        "kind": "completed_mkv",
        "path": str(destination),
        # A committed receipt already contains the production-computed full
        # MKV digest.  The evaluator independently re-hashes the file, avoiding
        # an unnecessary second full-media read during collection.
        "sha256": output_digest if output_digest else MISSING_SHA256,
    }
    return {
        "receipt": receipt_reference,
        "final_mkv": final_reference,
    }, _deduplicate(errors), committed_at


def _check_ledger(
    row: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    media: dict[str, Any],
    manifest_file: Path,
    manifest_digest: str,
    errors: list[str],
) -> None:
    for field in (
        "obligation_id",
        "canonical_path",
        "media_fingerprint",
        "media_size",
        "media_mtime_ns",
        "policy_revision",
    ):
        if row.get(field) != media.get(field):
            errors.append(f"delivery_ledger_identity_mismatch:{field}")
    if row.get("state") != "succeeded":
        errors.append("delivery_ledger_not_succeeded")
    try:
        expected_manifest = str(manifest_file.resolve())
        actual_manifest = str(Path(str(row.get("manifest_path") or "")).resolve())
    except OSError:
        expected_manifest = str(manifest_file)
        actual_manifest = str(row.get("manifest_path") or "")
    if actual_manifest != expected_manifest:
        errors.append("delivery_ledger_manifest_path_mismatch")
    if not manifest_digest or row.get("manifest_sha256") != manifest_digest:
        errors.append("delivery_ledger_manifest_hash_mismatch")
    verification = row.get("verification")
    if not isinstance(verification, dict) or verification.get("publication_semantics_verified") is not True:
        errors.append("delivery_ledger_publication_not_verified")
    try:
        if int(row.get("attempt_count") or -1) != len(attempts):
            errors.append("delivery_ledger_attempt_count_mismatch")
    except (TypeError, ValueError):
        errors.append("delivery_ledger_attempt_count_mismatch")
    if not attempts or str(attempts[-1].get("status") or "") != "succeeded":
        errors.append("delivery_ledger_terminal_success_missing")


def _read_artifact_json(
    path: Path,
    *,
    kind: str,
    evidence: list[dict[str, Any]],
    errors: list[str],
    missing_error: str,
) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = read_json_object(path)
        digest = sha256_file(path)
    except (AcceptanceInputError, OSError):
        errors.append(missing_error)
        return None, ""
    evidence.append({"kind": kind, "path": str(path), "sha256": digest})
    return payload, digest


def _missing_reference(path: Path, kind: str) -> dict[str, Any]:
    return {"kind": kind, "path": str(path), "sha256": MISSING_SHA256}


def _add_timestamp(values: list[float], value: Any) -> None:
    parsed = _positive_timestamp(value)
    if parsed is not None:
        values.append(parsed)


def _require_fresh_timestamp(
    value: Any,
    plan_created_at: float | None,
    error: str,
    errors: list[str],
) -> None:
    parsed = _positive_timestamp(value)
    if plan_created_at is None or parsed is None or parsed < plan_created_at:
        errors.append(error)


def _positive_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in _HEX64 for character in value)


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _configured_path(config: Any, field: str, default: str) -> Path:
    configured = Path(str(getattr(config, field, default) or default))
    return configured if configured.is_absolute() else Path(config.work_path) / configured


class _ReadonlyLedger:
    def __init__(self, config: Any) -> None:
        self.path = _configured_path(config, "scanner_state_path", "scanner_state.sqlite3")
        self.connection: sqlite3.Connection | None = None
        self.error = ""

    def __enter__(self) -> "_ReadonlyLedger":
        if not self.path.is_file():
            self.error = f"scanner state database is missing: {self.path}"
            return self
        try:
            self.connection = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=30,
            )
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("PRAGMA busy_timeout=30000")
            self.connection.execute("BEGIN")
        except sqlite3.Error as exc:
            self.error = str(exc)
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.connection is not None:
            try:
                self.connection.rollback()
            finally:
                self.connection.close()
                self.connection = None

    def read(
        self,
        obligation_id: str,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
        if self.error:
            return None, [], self.error
        if self.connection is None:
            return None, [], "scanner state database is unavailable"
        try:
            obligation_columns = {
                str(item[1])
                for item in self.connection.execute(
                    "PRAGMA table_info(ai_delivery_obligations)"
                ).fetchall()
            }
            run_id_projection = (
                "acceptance_run_id"
                if "acceptance_run_id" in obligation_columns
                else "NULL AS acceptance_run_id"
            )
            row = self.connection.execute(
                f"""
                SELECT obligation_id, canonical_path, media_fingerprint, media_size,
                       media_mtime_ns, policy_revision, state, outcome_code,
                       manifest_path, manifest_sha256, verification_json,
                       eligible_at, due_at, verified_at, attempt_count,
                       {run_id_projection}
                FROM ai_delivery_obligations WHERE obligation_id=?
                """,
                (obligation_id,),
            ).fetchone()
            if row is None:
                return None, [], "obligation is missing"
            ledger = dict(row)
            try:
                ledger["verification"] = json.loads(str(ledger.pop("verification_json") or "{}"))
            except json.JSONDecodeError:
                ledger["verification"] = None
            attempt_columns = {
                str(item[1])
                for item in self.connection.execute(
                    "PRAGMA table_info(ai_delivery_attempts)"
                ).fetchall()
            }
            attempt_run_id_projection = (
                "acceptance_run_id"
                if "acceptance_run_id" in attempt_columns
                else "NULL AS acceptance_run_id"
            )
            attempts = [
                dict(item)
                for item in self.connection.execute(
                    f"""
                    SELECT attempt_id, attempt_number, status, stage, error_code,
                           detail, started_at, finished_at,
                           {attempt_run_id_projection}
                    FROM ai_delivery_attempts
                    WHERE obligation_id=? ORDER BY attempt_number
                    """,
                    (obligation_id,),
                ).fetchall()
            ]
            return ledger, attempts, ""
        except sqlite3.Error as exc:
            return None, [], str(exc)
