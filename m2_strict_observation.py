from __future__ import annotations

import re
from typing import Any, Mapping


# These keys are the complete M2 production-observation contract.  A caller
# must supply every key as an actual bool; missing, false, or contradictory
# evidence can never become a strict success through truthiness/defaulting.
STRICT_EVIDENCE_KEYS = (
    "final_state_completed",
    "output_parse_pass",
    "hard_qc_pass",
    "hallucination_validation_pass",
    "source_checksum_unchanged",
    "no_duplicate_job",
    "no_duplicate_publish",
    "decision_record_complete",
    "stage_checkpoint_history_complete",
    "runtime_commit_matches_gate_baseline",
    "no_unresolved_retry_quarantine_fallback",
)

PROCESSING_STRATEGIES = (
    "USE_EXISTING_ZH_TW",
    "NORMALIZE_ZH_HANT",
    "CONVERT_ZH_CN",
    "TRANSLATE_JA_SUBTITLE",
    "ASR_JA_AUDIO",
    "NEEDS_REVIEW",
    "UNSUPPORTED",
    "OTHER",
    "UNREPORTED",
)

SUMMARY_COUNTER_KEYS = (
    "gate_progress",
    "claimed_after_gate_start",
    "completed_strict_verified",
    "completed_unverified",
    "needs_review",
    "failed",
    "quarantined",
    "hallucination_blocked",
    "output_parse_failures",
    "source_mutation_incidents",
    "duplicate_jobs",
    "duplicate_publishes",
    "incorrect_completions",
    "breaker_trips",
    "checkpoint_resumes",
    "oom_events",
)

_OUTCOME_FLAG_KEYS = (
    "claimed_after_gate_start",
    "quarantined",
    "hallucination_blocked",
    "output_parse_failure",
    "source_mutation_incident",
    "duplicate_job",
    "duplicate_publish",
    "incorrect_completion",
    "breaker_tripped",
    "checkpoint_resumed",
    "oom_event",
    "unresolved_retry",
    "unresolved_fallback",
)
_ALLOWED_OUTCOME_KEYS = frozenset(
    {"event_kind", "terminal_status", "processing_strategy", *_OUTCOME_FLAG_KEYS}
)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_EVENT_KINDS = frozenset({"claim", "terminal", "breaker", "observation"})
_TERMINAL_STATUS_ALIASES = {
    "completed": "COMPLETED",
    "complete": "COMPLETED",
    "succeeded": "COMPLETED",
    "success": "COMPLETED",
    "needs_review": "NEEDS_REVIEW",
    "review_required": "NEEDS_REVIEW",
    "failed": "FAILED",
    "permanent_failure": "FAILED",
    "quarantined": "QUARANTINED",
    "retrying": "RETRYING",
    "retryable_failure": "RETRYING",
    "running": "RUNNING",
    "processing": "RUNNING",
    "queued": "QUEUED",
    "unknown": "UNKNOWN",
}
_STRATEGY_ALIASES = {
    "use_existing_zh_tw": "USE_EXISTING_ZH_TW",
    "use_zh_tw": "USE_EXISTING_ZH_TW",
    "adopted_zh_tw": "USE_EXISTING_ZH_TW",
    "normalize_zh_hant": "NORMALIZE_ZH_HANT",
    "converted_zh_hant": "NORMALIZE_ZH_HANT",
    "convert_zh_cn": "CONVERT_ZH_CN",
    "converted_zh_cn": "CONVERT_ZH_CN",
    "translate_ja_subtitle": "TRANSLATE_JA_SUBTITLE",
    "translated_japanese_subtitle": "TRANSLATE_JA_SUBTITLE",
    "translate_japanese": "TRANSLATE_JA_SUBTITLE",
    "asr_ja_audio": "ASR_JA_AUDIO",
    "japanese_audio_asr": "ASR_JA_AUDIO",
    "needs_review": "NEEDS_REVIEW",
    "unsupported": "UNSUPPORTED",
}


class StrictObservationInputError(ValueError):
    """The caller supplied non-deidentified or structurally unsafe input."""


def strict_evidence_template(*, passed: bool = False) -> dict[str, bool]:
    if type(passed) is not bool:
        raise StrictObservationInputError("passed must be an actual bool")
    return {key: passed for key in STRICT_EVIDENCE_KEYS}


def normalize_processing_strategy(value: Any) -> str:
    """Return one bounded strategy bucket without persisting caller text."""

    if value is None or value == "":
        return "UNREPORTED"
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value.strip()):
        raise StrictObservationInputError(
            "processing_strategy must be a deidentified code"
        )
    normalized = value.strip().casefold().replace("-", "_").replace(".", "_")
    return _STRATEGY_ALIASES.get(normalized, "OTHER")


def normalize_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a deliberately path-free observation outcome."""

    if not isinstance(outcome, Mapping):
        raise StrictObservationInputError("outcome must be a mapping")
    unknown = sorted(str(key) for key in set(outcome).difference(_ALLOWED_OUTCOME_KEYS))
    if unknown:
        raise StrictObservationInputError(
            "outcome contains non-contract keys: " + ", ".join(unknown)
        )

    event_kind_raw = outcome.get("event_kind", "terminal")
    if not isinstance(event_kind_raw, str):
        raise StrictObservationInputError("event_kind must be a deidentified code")
    event_kind = event_kind_raw.strip().casefold()
    if event_kind not in _EVENT_KINDS:
        raise StrictObservationInputError("event_kind is not supported")

    status_raw = outcome.get("terminal_status", "unknown")
    if not isinstance(status_raw, str) or not _SAFE_CODE.fullmatch(status_raw.strip()):
        raise StrictObservationInputError(
            "terminal_status must be a deidentified code"
        )
    terminal_status = _TERMINAL_STATUS_ALIASES.get(
        status_raw.strip().casefold().replace("-", "_"),
        "UNKNOWN",
    )

    normalized: dict[str, Any] = {
        "event_kind": event_kind,
        "terminal_status": terminal_status,
        "processing_strategy": normalize_processing_strategy(
            outcome.get("processing_strategy")
        ),
    }
    for key in _OUTCOME_FLAG_KEYS:
        value = outcome.get(key, False)
        if type(value) is not bool:
            raise StrictObservationInputError(f"{key} must be an actual bool")
        normalized[key] = value
    return normalized


def qualify_strict_output(
    outcome: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed unless all eleven independent evidence facts are true."""

    normalized_outcome = normalize_outcome(outcome)
    if not isinstance(evidence, Mapping):
        raise StrictObservationInputError("evidence must be a mapping")
    unknown = sorted(str(key) for key in set(evidence).difference(STRICT_EVIDENCE_KEYS))
    if unknown:
        raise StrictObservationInputError(
            "evidence contains non-contract keys: " + ", ".join(unknown)
        )

    missing = [key for key in STRICT_EVIDENCE_KEYS if key not in evidence]
    invalid = [
        key
        for key in STRICT_EVIDENCE_KEYS
        if key in evidence and type(evidence[key]) is not bool
    ]
    if invalid:
        raise StrictObservationInputError(
            "strict evidence values must be actual bools: " + ", ".join(invalid)
        )
    effective = {
        key: bool(evidence.get(key)) if key not in missing else False
        for key in STRICT_EVIDENCE_KEYS
    }

    contradictions: list[str] = []
    status = normalized_outcome["terminal_status"]
    if status != "COMPLETED":
        effective["final_state_completed"] = False
        contradictions.append("terminal_status_not_completed")
    incident_guards = (
        ("output_parse_failure", "output_parse_pass"),
        ("source_mutation_incident", "source_checksum_unchanged"),
        ("duplicate_job", "no_duplicate_job"),
        ("duplicate_publish", "no_duplicate_publish"),
    )
    for outcome_key, evidence_key in incident_guards:
        if normalized_outcome[outcome_key]:
            effective[evidence_key] = False
            contradictions.append(f"outcome_contradicts_{evidence_key}")
    if (
        normalized_outcome["quarantined"]
        or normalized_outcome["unresolved_retry"]
        or normalized_outcome["unresolved_fallback"]
        or status in {"QUARANTINED", "RETRYING"}
    ):
        effective["no_unresolved_retry_quarantine_fallback"] = False
        contradictions.append("unresolved_terminal_state")
    if normalized_outcome["incorrect_completion"]:
        effective["final_state_completed"] = False
        contradictions.append("incorrect_completion_reported")

    failed = [key for key in STRICT_EVIDENCE_KEYS if effective[key] is not True]
    reason_codes = [*(f"missing_{key}" for key in missing)]
    reason_codes.extend(f"failed_{key}" for key in failed if key not in missing)
    reason_codes.extend(contradictions)
    qualified = (
        normalized_outcome["event_kind"] == "terminal"
        and status == "COMPLETED"
        and not missing
        and not failed
    )
    if normalized_outcome["event_kind"] != "terminal":
        reason_codes.append("not_terminal_observation")
    return {
        "qualified": qualified,
        "terminal_status": status,
        "processing_strategy": normalized_outcome["processing_strategy"],
        "evidence": effective,
        "missing_evidence": missing,
        "failed_evidence": failed,
        "reason_codes": _deduplicated(reason_codes),
    }


def empty_summary_counters() -> dict[str, Any]:
    counters: dict[str, Any] = {key: 0 for key in SUMMARY_COUNTER_KEYS}
    counters["processing_strategy_counts"] = {
        strategy: 0 for strategy in PROCESSING_STRATEGIES
    }
    return counters


def validate_summary_counters(counters: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated copy of the fixed machine-summary counter schema."""

    return _validated_counter_copy(counters)


def update_summary_counters(
    counters: Mapping[str, Any] | None,
    *,
    outcome: Mapping[str, Any],
    evidence: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return updated bounded counters and an optional strict qualification."""

    current = _validated_counter_copy(counters)
    normalized = normalize_outcome(outcome)
    event_kind = normalized["event_kind"]
    qualification: dict[str, Any] | None = None

    if event_kind == "claim" and normalized["claimed_after_gate_start"]:
        current["claimed_after_gate_start"] += 1
        strategy = normalized["processing_strategy"]
        current["processing_strategy_counts"][strategy] += 1

    if event_kind == "terminal":
        qualification = qualify_strict_output(
            outcome,
            {} if evidence is None else evidence,
        )
        status = normalized["terminal_status"]
        if qualification["qualified"]:
            current["completed_strict_verified"] += 1
            current["gate_progress"] += 1
        elif status == "COMPLETED":
            current["completed_unverified"] += 1
        elif status == "NEEDS_REVIEW":
            current["needs_review"] += 1
        elif status == "FAILED":
            current["failed"] += 1

    if normalized["quarantined"] or normalized["terminal_status"] == "QUARANTINED":
        current["quarantined"] += 1
    flag_counters = (
        ("hallucination_blocked", "hallucination_blocked"),
        ("output_parse_failure", "output_parse_failures"),
        ("source_mutation_incident", "source_mutation_incidents"),
        ("duplicate_job", "duplicate_jobs"),
        ("duplicate_publish", "duplicate_publishes"),
        ("incorrect_completion", "incorrect_completions"),
        ("breaker_tripped", "breaker_trips"),
        ("checkpoint_resumed", "checkpoint_resumes"),
        ("oom_event", "oom_events"),
    )
    for flag, counter in flag_counters:
        if normalized[flag]:
            current[counter] += 1
    return current, qualification


def _validated_counter_copy(counters: Mapping[str, Any] | None) -> dict[str, Any]:
    if counters is None:
        return empty_summary_counters()
    if not isinstance(counters, Mapping):
        raise StrictObservationInputError("counters must be a mapping")
    allowed = {*SUMMARY_COUNTER_KEYS, "processing_strategy_counts"}
    unknown = sorted(str(key) for key in set(counters).difference(allowed))
    if unknown:
        raise StrictObservationInputError(
            "counters contain unknown keys: " + ", ".join(unknown)
        )
    result = empty_summary_counters()
    for key in SUMMARY_COUNTER_KEYS:
        value = counters.get(key, 0)
        if type(value) is not int or value < 0:
            raise StrictObservationInputError(
                f"counter {key} must be a non-negative integer"
            )
        result[key] = value
    strategies = counters.get("processing_strategy_counts", {})
    if not isinstance(strategies, Mapping):
        raise StrictObservationInputError(
            "processing_strategy_counts must be a mapping"
        )
    unknown_strategies = sorted(
        str(key) for key in set(strategies).difference(PROCESSING_STRATEGIES)
    )
    if unknown_strategies:
        raise StrictObservationInputError(
            "unknown processing strategy counters: "
            + ", ".join(unknown_strategies)
        )
    for strategy in PROCESSING_STRATEGIES:
        value = strategies.get(strategy, 0)
        if type(value) is not int or value < 0:
            raise StrictObservationInputError(
                f"strategy counter {strategy} must be a non-negative integer"
            )
        result["processing_strategy_counts"][strategy] = value
    return result


def _deduplicated(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
