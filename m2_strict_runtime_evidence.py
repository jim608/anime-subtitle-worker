from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from m2_strict_observation import (
    STRICT_EVIDENCE_KEYS,
    normalize_processing_strategy,
    strict_evidence_template,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SAFE_CODE_RE = re.compile(r"[^a-z0-9_.-]+")
_COMPLETED_SOURCE_STRATEGIES = frozenset(
    {
        "USE_EXISTING_ZH_TW",
        "NORMALIZE_ZH_HANT",
        "CONVERT_ZH_CN",
        "TRANSLATE_JA_SUBTITLE",
        "ASR_JA_AUDIO",
    }
)
_STAGE_ATTEMPT_STATUSES = frozenset(
    {
        "RUNNING",
        "SUCCEEDED",
        "RETRYABLE_FAILURE",
        "PERMANENT_FAILURE",
        "INTERRUPTED",
        "NEEDS_REVIEW",
    }
)
_TERMINAL_STATUS = {
    "succeeded": "COMPLETED",
    "review_required": "NEEDS_REVIEW",
    "retryable_failure": "RETRYING",
    "deferred": "RETRYING",
    "failed": "FAILED",
    "running": "RUNNING",
}
_FINAL_ARTIFACT_SUFFIXES = frozenset({".ass", ".srt"})
_FORBIDDEN_ARTIFACT_MARKERS = frozenset(
    {"tmp", "part", "partial", "publishing", "staging"}
)


def build_m2_strict_runtime_evidence(
    state: Any,
    video: str | Path,
    config: Any,
    attempt_id: str,
    runtime_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the post-commit M2 gate facts without returning private paths.

    This is deliberately a read-only verifier.  It treats every missing,
    malformed, stale, or internally contradictory record as absent evidence.
    The caller may pass either ``runtime_guardrail_status(config)`` or its
    validated ``state`` member as ``runtime_state``.
    """

    evidence = strict_evidence_template(passed=False)
    resolved_video = _resolved_path(video)
    attempt = _delivery_attempt(state, attempt_id)
    obligation = _delivery_obligation(state, attempt)
    queue = _queue_snapshot(state, resolved_video)
    pipeline, job = _pipeline_context(state, resolved_video, obligation)
    provenance = _processing_provenance(config, resolved_video, attempt)

    decision_ok, strategy, decision_row = _decision_evidence(
        pipeline,
        job,
        resolved_video,
        config,
        runtime_state,
        provenance,
    )
    strategy = normalize_processing_strategy(strategy if decision_ok else "")

    manifest = _manifest_evidence(
        resolved_video,
        config,
        obligation,
        strategy=strategy,
    )
    source_ok, source_mutation = _source_checksum_evidence(
        resolved_video,
        config,
        attempt,
        obligation,
        provenance,
    )
    duplicate = _duplicate_evidence(
        state,
        pipeline,
        resolved_video,
        attempt,
        obligation,
        job,
        manifest,
    )
    history_ok, history_meta = _stage_history_evidence(
        pipeline,
        job,
        decision_ok=decision_ok,
        decision_row=decision_row,
    )
    hallucination_ok = _hallucination_evidence(
        resolved_video,
        config,
        decision_ok=decision_ok,
        strategy=strategy,
    )
    runtime_ok, claimed_after_gate_start = _runtime_evidence(
        config,
        runtime_state,
        attempt_id=str(attempt_id or ""),
        attempt=attempt,
        video=resolved_video,
    )
    final_state_ok = _final_state_evidence(
        resolved_video,
        attempt,
        obligation,
        queue,
        job,
        manifest,
    )
    unresolved_ok, unresolved_meta = _no_unresolved_state_evidence(
        state,
        pipeline,
        resolved_video,
        config,
        attempt,
        obligation,
        queue,
        job,
        provenance,
        manifest,
    )

    evidence.update(
        {
            "final_state_completed": final_state_ok,
            "output_parse_pass": bool(manifest.get("output_parse_pass")),
            "hard_qc_pass": bool(manifest.get("hard_qc_pass")),
            "hallucination_validation_pass": hallucination_ok,
            "source_checksum_unchanged": source_ok,
            "no_duplicate_job": bool(duplicate.get("no_duplicate_job")),
            "no_duplicate_publish": bool(duplicate.get("no_duplicate_publish")),
            "decision_record_complete": decision_ok,
            "stage_checkpoint_history_complete": history_ok,
            "runtime_commit_matches_gate_baseline": runtime_ok,
            "no_unresolved_retry_quarantine_fallback": unresolved_ok,
        }
    )
    # Keep the contract exact even if STRICT_EVIDENCE_KEYS changes later.
    evidence = {key: evidence.get(key) is True for key in STRICT_EVIDENCE_KEYS}

    terminal_status = _terminal_status(attempt, queue, job)
    completed = terminal_status == "COMPLETED"
    error_text = " ".join(
        str((attempt or {}).get(key) or "")
        for key in ("stage", "error_code", "detail")
    ).casefold()
    duplicate_job = bool(duplicate.get("duplicate_job_detected"))
    duplicate_publish = bool(duplicate.get("duplicate_publish_detected"))
    output_parse_failure = completed and not evidence["output_parse_pass"]
    hallucination_blocked = (
        (strategy == "ASR_JA_AUDIO" and not evidence["hallucination_validation_pass"])
        or "hallucination" in error_text
        or "prompt_echo" in error_text
    )
    unresolved = not evidence["no_unresolved_retry_quarantine_fallback"]
    all_strict = all(evidence.values())
    outcome = {
        "verified_completed": completed,
        "failed": not completed,
        "terminal_status": terminal_status,
        "stage": _safe_code((attempt or {}).get("stage"), "worker"),
        "error_code": _safe_code((attempt or {}).get("error_code"), ""),
        "reason_code": _safe_code((attempt or {}).get("status"), "unknown"),
        "processing_strategy": strategy,
        "claimed_after_gate_start": claimed_after_gate_start,
        "quarantined": bool(unresolved_meta.get("quarantined")),
        "hallucination_blocked": hallucination_blocked,
        "output_parse_failure": output_parse_failure,
        "source_mutation_incident": source_mutation,
        "duplicate_job": duplicate_job,
        "duplicate_publish": duplicate_publish,
        "incorrect_completion": bool(
            (completed and claimed_after_gate_start and not all_strict)
            or "incorrect_completion" in error_text
        ),
        "breaker_tripped": _runtime_is_tripped(runtime_state),
        "checkpoint_resumed": bool(history_meta.get("checkpoint_resumed")),
        "oom_event": "oom" in error_text or "out_of_memory" in error_text,
        "unresolved_retry": bool(unresolved_meta.get("unresolved_retry")),
        "unresolved_fallback": bool(unresolved_meta.get("unresolved_fallback")),
    }
    return {
        "outcome": outcome,
        "evidence": evidence,
        "processing_strategy": strategy,
    }


def _delivery_attempt(state: Any, attempt_id: str) -> dict[str, Any] | None:
    if not str(attempt_id or "").strip():
        return None
    try:
        rows = _query_rows(
            getattr(state, "_conn", None),
            "SELECT * FROM ai_delivery_attempts WHERE attempt_id=?",
            (str(attempt_id),),
        )
        public = state.get_ai_delivery_attempt(str(attempt_id))
    except Exception:
        return None
    if len(rows) != 1 or not isinstance(public, dict):
        return None
    row = rows[0]
    required = {
        "attempt_id",
        "obligation_id",
        "attempt_number",
        "status",
        "started_at",
        "finished_at",
    }
    if not required.issubset(row):
        return None
    text_fields = ("attempt_id", "obligation_id", "status")
    if any(str(public.get(key) or "") != str(row.get(key) or "") for key in text_fields):
        return None
    if _strict_int(public.get("attempt_number")) != _strict_int(
        row.get("attempt_number")
    ):
        return None
    if any(
        _strict_float(public.get(key)) != _strict_float(row.get(key))
        for key in ("started_at", "finished_at")
    ):
        return None
    return row


def _delivery_obligation(
    state: Any,
    attempt: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(attempt, Mapping):
        return None
    obligation_id = str(attempt.get("obligation_id") or "")
    try:
        rows = _query_rows(
            getattr(state, "_conn", None),
            "SELECT * FROM ai_delivery_obligations WHERE obligation_id=?",
            (obligation_id,),
        )
        public = state.get_ai_delivery_obligation(obligation_id)
    except Exception:
        return None
    if len(rows) != 1 or not isinstance(public, dict):
        return None
    row = rows[0]
    required = {
        "obligation_id",
        "canonical_path",
        "media_size",
        "media_mtime_ns",
        "policy_revision",
        "state",
    }
    if not required.issubset(row):
        return None
    if any(str(public.get(key) or "") != str(row.get(key) or "") for key in required):
        return None
    return row


def _queue_snapshot(state: Any, video: Path | None) -> dict[str, Any] | None:
    if video is None:
        return None
    try:
        rows = _query_rows(
            getattr(state, "_conn", None),
            "SELECT * FROM ai_candidate_queue WHERE path=?",
            (str(video),),
        )
        public = state.ai_queue_candidate_snapshot(video)
    except Exception:
        return None
    if len(rows) != 1 or not isinstance(public, dict):
        return None
    if str(public.get("status") or "") != str(rows[0].get("status") or ""):
        return None
    return public


def _pipeline_context(
    state: Any,
    video: Path | None,
    obligation: Mapping[str, Any] | None,
) -> tuple[Any | None, dict[str, Any] | None]:
    if video is None or not isinstance(obligation, Mapping):
        return None, None
    try:
        pipeline = state.pipeline_jobs()
        job = pipeline.job_for_path(
            video,
            size=_strict_int(obligation.get("media_size")),
            mtime_ns=_strict_int(obligation.get("media_mtime_ns")),
            create=False,
        )
    except Exception:
        return None, None
    return pipeline, job if isinstance(job, dict) else None


def _processing_provenance(
    config: Any,
    video: Path | None,
    attempt: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if video is None or not isinstance(attempt, Mapping):
        return None
    try:
        from processing_provenance import (
            PROVENANCE_SCHEMA_VERSION,
            load_provenance,
            processing_config_signature,
        )

        payload = load_provenance(config, video)
        stat = video.stat()
        started_at = _strict_float(attempt.get("started_at"))
        if not isinstance(payload, dict):
            return None
        if (
            payload.get("schema_version") != PROVENANCE_SCHEMA_VERSION
            or str(payload.get("video_path") or "") != str(video)
            or payload.get("video")
            != {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}
            or str(payload.get("config_signature") or "")
            != processing_config_signature(config)
            or str(payload.get("status") or "") != "complete"
            or _strict_float(payload.get("run_started_at")) < started_at
            or _strict_float(payload.get("finished_at"))
            < _strict_float(payload.get("run_started_at"))
        ):
            return None
        return payload
    except Exception:
        return None


def _manifest_evidence(
    video: Path | None,
    config: Any,
    obligation: Mapping[str, Any] | None,
    *,
    strategy: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "valid": False,
        "output_parse_pass": False,
        "hard_qc_pass": False,
        "publication_marker_absent": False,
        "completed_marker_absent": False,
        "completed_delivery_valid": False,
        "manifest_path": None,
        "manifest_sha256": "",
        "output_paths": (),
    }
    if video is None or not isinstance(obligation, Mapping):
        return result
    try:
        from output_manifest import (
            manifest_publication_semantics,
            output_manifest_path,
            output_publication_marker_path,
            publication_is_traditional_chinese_delivery,
            validate_output_manifest,
        )
        from subtitle_quality import analyze_subtitle_file
        from subtitle_paths import paths_for_video
        from translation_quality import (
            read_translation_quality_events_strict,
            read_translation_quality_hold_strict,
            translation_quality_hold_path,
        )

        manifest_path = output_manifest_path(video, config)
        publication_marker_absent = not output_publication_marker_path(
            video, config
        ).exists()
        if not validate_output_manifest(
            video,
            config,
            verify_hashes=True,
            require_delivery_evidence=True,
            expected_obligation_id=str(obligation.get("obligation_id") or ""),
            expected_policy_revision=str(obligation.get("policy_revision") or ""),
            require_publication_semantics=True,
        ):
            return result
        payload, manifest_sha256 = _stable_json_file(manifest_path)
        if (
            str(obligation.get("manifest_path") or "") != str(manifest_path)
            or not _valid_sha256(str(obligation.get("manifest_sha256") or ""))
            or str(obligation.get("manifest_sha256") or "").casefold()
            != manifest_sha256
        ):
            return result
        semantics = manifest_publication_semantics(payload)
        if not publication_is_traditional_chinese_delivery(semantics):
            return result
        outputs = payload.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            return result

        reports: list[Any] = []
        output_paths: list[Path] = []
        output_keys: set[str] = set()
        for entry in outputs:
            if not isinstance(entry, dict):
                return result
            output = Path(str(entry.get("path") or ""))
            output_key = os.path.normcase(str(output.resolve(strict=True)))
            if output_key in output_keys or not _is_final_subtitle_path(output):
                return result
            output_keys.add(output_key)
            expected_hash = str(entry.get("sha256") or "").casefold()
            before_hash, before_signature = _stable_file_hash(output)
            if not _valid_sha256(expected_hash) or before_hash != expected_hash:
                return result
            report = analyze_subtitle_file(
                output,
                config,
                role=_quality_role(entry.get("language"), strategy),
            )
            after_hash, after_signature = _stable_file_hash(output)
            if before_hash != after_hash or before_signature != after_signature:
                return result
            reports.append(report)
            output_paths.append(output)

        subtitle_paths = paths_for_video(video, config)
        hold_path = translation_quality_hold_path(subtitle_paths.zh_cn_srt)
        translation_hold_absent = (
            not hold_path.exists()
            and read_translation_quality_hold_strict(subtitle_paths.zh_cn_srt)
            is None
        )
        translation_events = read_translation_quality_events_strict(
            subtitle_paths.zh_cn_srt
        )
        hard_translation_event_absent = not any(
            isinstance(item, dict)
            and str(item.get("severity") or "").casefold() == "fail"
            for item in translation_events
        )
        output_parse_pass = bool(reports) and all(
            _strict_int(getattr(report, "dialogues", 0)) > 0 for report in reports
        )
        hard_qc_pass = bool(
            output_parse_pass
            and translation_hold_absent
            and hard_translation_event_absent
            and all(getattr(report, "has_failures", True) is False for report in reports)
        )

        completed_marker_absent = True
        completed_delivery_valid = True
        if bool(getattr(config, "completed_delivery_enabled", False)):
            from completed_delivery import (
                completed_delivery_marker_path,
                validate_completed_delivery,
            )

            completed_marker_absent = not completed_delivery_marker_path(
                video, config
            ).exists()
            completed_delivery_valid = validate_completed_delivery(
                video,
                config,
                verify_streams=True,
            )
        result.update(
            {
                "valid": bool(
                    publication_marker_absent
                    and completed_marker_absent
                    and completed_delivery_valid
                ),
                "output_parse_pass": output_parse_pass
                and completed_delivery_valid,
                "hard_qc_pass": hard_qc_pass,
                "publication_marker_absent": publication_marker_absent,
                "completed_marker_absent": completed_marker_absent,
                "completed_delivery_valid": completed_delivery_valid,
                "manifest_path": manifest_path,
                "manifest_sha256": manifest_sha256,
                "output_paths": tuple(output_paths),
            }
        )
    except Exception:
        return result
    return result


def _source_checksum_evidence(
    video: Path | None,
    config: Any,
    attempt: Mapping[str, Any] | None,
    obligation: Mapping[str, Any] | None,
    provenance: Mapping[str, Any] | None,
) -> tuple[bool, bool]:
    if (
        video is None
        or not isinstance(attempt, Mapping)
        or not isinstance(obligation, Mapping)
        or not isinstance(provenance, Mapping)
        or bool(getattr(config, "source_integrity_sha256_enabled", False)) is not True
    ):
        return False, False
    source = provenance.get("source_integrity")
    if not isinstance(source, Mapping):
        return False, False
    expected_sha = str(source.get("sha256") or "").casefold()
    if not _valid_sha256(expected_sha):
        return False, False
    try:
        from source_integrity import capture_source_snapshot

        current = capture_source_snapshot(video, hash_content=True)
        expected_identity = (
            str(video),
            _strict_int(obligation.get("media_size")),
            _strict_int(obligation.get("media_mtime_ns")),
        )
        provenance_identity = (
            str(source.get("canonical_path") or ""),
            _strict_int(source.get("size")),
            _strict_int(source.get("mtime_ns")),
        )
        current_identity = (
            current.canonical_path,
            current.size,
            current.mtime_ns,
        )
        sha_matches = current.sha256.casefold() == expected_sha
        identity_matches = (
            expected_identity == provenance_identity == current_identity
            and _strict_int(source.get("device")) == current.device
            and _strict_int(source.get("inode")) == current.inode
        )
        verified = (
            source.get("verified") is True
            and str(source.get("verification") or "") == "sha256"
            and sha_matches
            and identity_matches
        )
        return bool(verified), bool(not sha_matches or not identity_matches)
    except Exception:
        return False, False


def _decision_evidence(
    pipeline: Any,
    job: Mapping[str, Any] | None,
    video: Path | None,
    config: Any,
    runtime_state: Mapping[str, Any] | None,
    provenance: Mapping[str, Any] | None,
) -> tuple[bool, str, dict[str, Any] | None]:
    if (
        pipeline is None
        or video is None
        or not isinstance(job, Mapping)
        or not isinstance(provenance, Mapping)
    ):
        return False, "", None
    source_analysis = provenance.get("source_analysis")
    if not isinstance(source_analysis, Mapping):
        return False, "", None
    try:
        from source_analysis_service import SOURCE_ANALYSIS_SERVICE_VERSION
        from source_analyzer import (
            ANALYZER_VERSION,
            DECISION_SCHEMA_VERSION,
            DECISION_VERSION,
            canonical_json_sha256,
        )
        from source_inventory import (
            SOURCE_INPUT_IDENTITY_VERSION,
            SOURCE_INVENTORY_VERSION,
            build_source_input_identity,
        )

        identity = build_source_input_identity(video, job, config=config)
        thresholds = config.source_analyzer_thresholds()
        config_fingerprint = canonical_json_sha256(
            {
                "service_version": SOURCE_ANALYSIS_SERVICE_VERSION,
                "input_identity_version": SOURCE_INPUT_IDENTITY_VERSION,
                "inventory_version": SOURCE_INVENTORY_VERSION,
                "analyzer_version": ANALYZER_VERSION,
                "decision_schema_version": DECISION_SCHEMA_VERSION,
                "decision_version": DECISION_VERSION,
                "configured_analyzer_revision": str(
                    getattr(config, "source_analyzer_version", "")
                ),
                "configured_decision_schema_revision": int(
                    getattr(
                        config,
                        "source_decision_schema_version",
                        DECISION_SCHEMA_VERSION,
                    )
                ),
                "configured_decision_revision": str(
                    getattr(config, "source_decision_version", "")
                ),
                "thresholds": thresholds.to_dict(),
            }
        )
        decision, reason = pipeline.reusable_source_decision(
            str(job.get("job_id") or ""),
            expected_identity=identity.to_dict(),
            expected_media_revision=str(job.get("media_revision") or ""),
            expected_source_fingerprint=str(job.get("media_fingerprint") or ""),
            expected_analyzer_version=ANALYZER_VERSION,
            expected_decision_schema_version=DECISION_SCHEMA_VERSION,
            expected_decision_version=DECISION_VERSION,
            expected_config_fingerprint=config_fingerprint,
            expected_candidate_fingerprint=identity.candidate_fingerprint,
            with_reason=True,
        )
        if (
            not isinstance(decision, dict)
            or reason != "source_decision_reusable"
            or decision.get("integrity_valid") is not True
        ):
            return False, "", None
        strategy = str(decision.get("strategy") or "").upper()
        decision_payload = decision.get("decision")
        if (
            strategy not in _COMPLETED_SOURCE_STRATEGIES
            or not isinstance(decision_payload, Mapping)
            or str(decision_payload.get("strategy") or "").upper() != strategy
        ):
            return False, "", None
        baseline = _runtime_payload(runtime_state)
        baseline_values = (
            baseline.get("baseline") if isinstance(baseline, Mapping) else None
        )
        if not isinstance(baseline_values, Mapping) or (
            str(baseline_values.get("decision_schema_version") or "")
            != str(DECISION_SCHEMA_VERSION)
            or str(baseline_values.get("decision_version") or "")
            != str(DECISION_VERSION)
        ):
            return False, "", None
        decision_id = str(decision.get("decision_id") or "")
        decision_sha = str(decision.get("decision_sha256") or "").casefold()
        if (
            str(source_analysis.get("contract") or "")
            != SOURCE_ANALYSIS_SERVICE_VERSION
            or str(source_analysis.get("decision_id") or "") != decision_id
            or str(source_analysis.get("decision_sha256") or "").casefold()
            != decision_sha
            or str(source_analysis.get("strategy") or "").upper() != strategy
            or str(source_analysis.get("decision_schema_version") or "")
            != str(DECISION_SCHEMA_VERSION)
        ):
            return False, "", None

        rows = _query_rows(
            getattr(pipeline, "_conn", None),
            "SELECT * FROM pipeline_source_decisions WHERE decision_id=? AND job_id=?",
            (decision_id, str(job.get("job_id") or "")),
        )
        if len(rows) != 1:
            return False, "", None
        row = rows[0]
        if not _raw_source_decision_valid(row, decision):
            return False, "", None
        attempts = _query_rows(
            getattr(pipeline, "_conn", None),
            "SELECT * FROM pipeline_stage_attempts WHERE stage_attempt_id=? AND job_id=?",
            (str(row.get("stage_attempt_id") or ""), str(job.get("job_id") or "")),
        )
        if (
            len(attempts) != 1
            or str(attempts[0].get("stage") or "") != "SUBTITLE_DETECTION"
            or str(attempts[0].get("status") or "") != "SUCCEEDED"
        ):
            return False, "", None
        return True, strategy, row
    except Exception:
        return False, "", None


def _raw_source_decision_valid(
    row: Mapping[str, Any],
    decoded: Mapping[str, Any],
) -> bool:
    try:
        input_identity, input_raw = _canonical_json_object(
            row.get("input_identity_json")
        )
        decision, decision_raw = _canonical_json_object(row.get("decision_json"))
        input_sha = hashlib.sha256(input_raw.encode("utf-8")).hexdigest()
        decision_sha = hashlib.sha256(decision_raw.encode("utf-8")).hexdigest()
        if (
            input_sha != str(row.get("input_identity_sha256") or "").casefold()
            or decision_sha != str(row.get("decision_sha256") or "").casefold()
            or input_identity != decoded.get("input_identity")
            or decision != decoded.get("decision")
        ):
            return False
        comparisons = (
            "media_revision",
            "source_fingerprint",
            "analyzer_version",
            "decision_schema_version",
            "decision_version",
            "config_fingerprint",
            "candidate_fingerprint",
            "strategy",
            "reason_code",
        )
        if any(
            str(decision.get(field) or "") != str(row.get(field) or "")
            for field in comparisons
        ):
            return False
        return bool(
            _valid_sha256(input_sha)
            and _valid_sha256(decision_sha)
            and decoded.get("integrity_valid") is True
        )
    except Exception:
        return False


def _hallucination_evidence(
    video: Path | None,
    config: Any,
    *,
    decision_ok: bool,
    strategy: str,
) -> bool:
    if not decision_ok:
        return False
    if strategy != "ASR_JA_AUDIO":
        return strategy in _COMPLETED_SOURCE_STRATEGIES
    if video is None:
        return False
    try:
        from safe_files import sha256_file
        from srt_utils import read_srt
        from subtitle_paths import paths_for_video
        from transcriber import (
            _is_hallucination_text,
            asr_diagnostics_path,
            asr_transcription_hold_path,
            read_asr_diagnostics,
        )

        source = paths_for_video(video, config).ja_srt
        diagnostic_path = asr_diagnostics_path(source, config)
        if (
            not source.is_file()
            or not diagnostic_path.is_file()
            or asr_transcription_hold_path(source, config).exists()
        ):
            return False
        diagnostic = read_asr_diagnostics(source, config)
        source_sha = sha256_file(source)
        if (
            str(diagnostic.get("status") or "")
            not in {"accepted", "accepted_after_selective_retry"}
            or str(diagnostic.get("srt_sha256") or "").casefold() != source_sha
        ):
            return False
        blocks = read_srt(source)
        if not blocks:
            return False
        for block in blocks:
            text = " ".join(str(line).strip() for line in block.text if str(line).strip())
            if not text or _is_hallucination_text(text, config):
                return False
        return sha256_file(source) == source_sha
    except Exception:
        return False


def _duplicate_evidence(
    state: Any,
    pipeline: Any,
    video: Path | None,
    attempt: Mapping[str, Any] | None,
    obligation: Mapping[str, Any] | None,
    job: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
) -> dict[str, bool]:
    result = {
        "no_duplicate_job": False,
        "no_duplicate_publish": False,
        "duplicate_job_detected": False,
        "duplicate_publish_detected": False,
    }
    if (
        video is None
        or not isinstance(attempt, Mapping)
        or not isinstance(obligation, Mapping)
        or not isinstance(job, Mapping)
        or pipeline is None
    ):
        return result
    try:
        state_conn = getattr(state, "_conn", None)
        pipeline_conn = getattr(pipeline, "_conn", None)
        obligation_rows = _query_rows(
            state_conn,
            """
            SELECT obligation_id FROM ai_delivery_obligations
            WHERE canonical_path=? AND media_size=? AND media_mtime_ns=?
              AND policy_revision=?
            """,
            (
                str(video),
                _strict_int(obligation.get("media_size")),
                _strict_int(obligation.get("media_mtime_ns")),
                str(obligation.get("policy_revision") or ""),
            ),
        )
        all_attempts = _query_rows(
            state_conn,
            "SELECT attempt_id, status FROM ai_delivery_attempts WHERE obligation_id=?",
            (str(obligation.get("obligation_id") or ""),),
        )
        pipeline_rows = _query_rows(
            pipeline_conn,
            """
            SELECT job_id, media_revision FROM pipeline_jobs
            WHERE canonical_path=? AND media_size=? AND media_mtime_ns=?
            """,
            (
                str(job.get("canonical_path") or ""),
                _strict_int(job.get("media_size")),
                _strict_int(job.get("media_mtime_ns")),
            ),
        )
        running_attempts = [
            item for item in all_attempts if str(item.get("status") or "") == "running"
        ]
        succeeded_attempts = [
            item for item in all_attempts if str(item.get("status") or "") == "succeeded"
        ]
        duplicate_job = (
            len(obligation_rows) > 1
            or len(pipeline_rows) > 1
            or len(running_attempts) > 1
        )
        job_unique = bool(
            len(obligation_rows) == 1
            and str(obligation_rows[0].get("obligation_id") or "")
            == str(obligation.get("obligation_id") or "")
            and len(pipeline_rows) == 1
            and str(pipeline_rows[0].get("job_id") or "")
            == str(job.get("job_id") or "")
            and not running_attempts
        )

        manifest_path = manifest.get("manifest_path")
        manifest_rows = (
            _query_rows(
                state_conn,
                """
                SELECT obligation_id FROM ai_delivery_obligations
                WHERE state='succeeded' AND manifest_path=?
                """,
                (str(manifest_path),),
            )
            if manifest_path is not None
            else []
        )
        output_paths = tuple(manifest.get("output_paths") or ())
        normalized_outputs = {
            os.path.normcase(str(Path(item).resolve())) for item in output_paths
        }
        duplicate_publish = len(succeeded_attempts) > 1 or len(manifest_rows) > 1
        publish_unique = bool(
            manifest.get("valid") is True
            and len(succeeded_attempts) == 1
            and str(succeeded_attempts[0].get("attempt_id") or "")
            == str(attempt.get("attempt_id") or "")
            and len(manifest_rows) == 1
            and str(manifest_rows[0].get("obligation_id") or "")
            == str(obligation.get("obligation_id") or "")
            and len(output_paths) > 0
            and len(normalized_outputs) == len(output_paths)
            and manifest.get("publication_marker_absent") is True
            and manifest.get("completed_marker_absent") is True
            and manifest.get("completed_delivery_valid") is True
        )
        result.update(
            {
                "no_duplicate_job": job_unique,
                "no_duplicate_publish": publish_unique,
                "duplicate_job_detected": duplicate_job,
                "duplicate_publish_detected": duplicate_publish,
            }
        )
    except Exception:
        return result
    return result


def _stage_history_evidence(
    pipeline: Any,
    job: Mapping[str, Any] | None,
    *,
    decision_ok: bool,
    decision_row: Mapping[str, Any] | None,
) -> tuple[bool, dict[str, bool]]:
    meta = {"checkpoint_resumed": False}
    if pipeline is None or not isinstance(job, Mapping) or not decision_ok:
        return False, meta
    try:
        conn = getattr(pipeline, "_conn", None)
        job_id = str(job.get("job_id") or "")
        transitions = _query_rows(
            conn,
            "SELECT * FROM pipeline_job_transitions WHERE job_id=? ORDER BY sequence",
            (job_id,),
        )
        attempts = _query_rows(
            conn,
            """
            SELECT * FROM pipeline_stage_attempts
            WHERE job_id=? ORDER BY started_at, attempt_number
            """,
            (job_id,),
        )
        if not transitions or not attempts or str(job.get("state") or "") != "COMPLETED":
            return False, meta
        if str(job.get("active_stage_attempt_id") or ""):
            return False, meta

        prior_to_state: str | None = None
        seen_transition_ids: set[str] = set()
        seen_idempotency: set[str] = set()
        for expected_sequence, row in enumerate(transitions, start=1):
            if _strict_int(row.get("sequence")) != expected_sequence:
                return False, meta
            transition_id = str(row.get("transition_id") or "")
            if not transition_id or transition_id in seen_transition_ids:
                return False, meta
            seen_transition_ids.add(transition_id)
            if prior_to_state is not None and str(row.get("from_state") or "") != prior_to_state:
                return False, meta
            prior_to_state = str(row.get("to_state") or "")
            reason_code = str(row.get("reason_code") or "")
            confidence = _strict_float(row.get("confidence"))
            if not reason_code or not 0 <= confidence <= 1:
                return False, meta
            _canonical_json_object(row.get("evidence_json"))
            idem = str(row.get("idempotency_key") or "")
            if idem:
                if idem in seen_idempotency:
                    return False, meta
                seen_idempotency.add(idem)
        final_transition = transitions[-1]
        final_evidence, _ = _canonical_json_object(final_transition.get("evidence_json"))
        verification = final_evidence.get("verification")
        completion_gate = final_evidence.get("pipeline_completion_gate")
        if (
            str(final_transition.get("to_state") or "") != "COMPLETED"
            or str(final_transition.get("actor") or "") != "publisher"
            or not isinstance(verification, Mapping)
            or not all(
                verification.get(key) is True
                for key in (
                    "required_outputs_complete",
                    "hashes_verified",
                    "publication_marker_absent",
                    "media_identity_matched",
                )
            )
            or not isinstance(completion_gate, Mapping)
            or completion_gate.get("source_identity_verified") is not True
            or completion_gate.get("required_artifacts_rehashed") is not True
        ):
            return False, meta

        attempts_by_id: dict[str, Mapping[str, Any]] = {}
        seen_stage_numbers: set[tuple[str, int]] = set()
        succeeded = 0
        for row in attempts:
            attempt_id = str(row.get("stage_attempt_id") or "")
            stage = str(row.get("stage") or "")
            number = _strict_int(row.get("attempt_number"))
            status = str(row.get("status") or "")
            if (
                not attempt_id
                or attempt_id in attempts_by_id
                or not stage
                or number <= 0
                or (stage, number) in seen_stage_numbers
                or status not in _STAGE_ATTEMPT_STATUSES
                or status == "RUNNING"
            ):
                return False, meta
            attempts_by_id[attempt_id] = row
            seen_stage_numbers.add((stage, number))
            started = _strict_float(row.get("started_at"))
            finished = _strict_float(row.get("finished_at"))
            if started <= 0 or finished < started:
                return False, meta
            input_value, input_raw = _canonical_json_object(row.get("input_json"))
            if (
                not input_value
                or hashlib.sha256(input_raw.encode("utf-8")).hexdigest()
                != str(row.get("input_sha256") or "").casefold()
            ):
                return False, meta
            _canonical_json_object(row.get("output_json"))
            _canonical_json_object(row.get("model_json"))
            _canonical_json_object(row.get("error_json"))
            checkpoint, checkpoint_raw = _canonical_json_object(
                row.get("checkpoint_json")
            )
            checkpoint_sha = str(row.get("checkpoint_sha256") or "").casefold()
            if checkpoint:
                if (
                    not _valid_sha256(checkpoint_sha)
                    or hashlib.sha256(checkpoint_raw.encode("utf-8")).hexdigest()
                    != checkpoint_sha
                ):
                    return False, meta
            elif checkpoint_sha:
                return False, meta
            if status == "SUCCEEDED":
                succeeded += 1
            if status in {"INTERRUPTED", "RETRYABLE_FAILURE"}:
                meta["checkpoint_resumed"] = True
        if succeeded <= 0 or not isinstance(decision_row, Mapping):
            return False, meta
        source_attempt = attempts_by_id.get(
            str(decision_row.get("stage_attempt_id") or "")
        )
        if not isinstance(source_attempt, Mapping):
            return False, meta
        expected_checkpoint = {
            "kind": "source_decision",
            "decision_id": str(decision_row.get("decision_id") or ""),
            "decision_sha256": str(decision_row.get("decision_sha256") or ""),
            "input_identity_sha256": str(
                decision_row.get("input_identity_sha256") or ""
            ),
            "analyzer_version": str(decision_row.get("analyzer_version") or ""),
            "decision_schema_version": str(
                decision_row.get("decision_schema_version") or ""
            ),
        }
        source_checkpoint, _ = _canonical_json_object(
            source_attempt.get("checkpoint_json")
        )
        source_output, _ = _canonical_json_object(source_attempt.get("output_json"))
        if (
            str(source_attempt.get("stage") or "") != "SUBTITLE_DETECTION"
            or str(source_attempt.get("status") or "") != "SUCCEEDED"
            or _strict_int(source_attempt.get("outputs_verified")) != 1
            or source_checkpoint != expected_checkpoint
            or source_output
            != {
                "no_artifact_required": True,
                "checkpoint_evidence": expected_checkpoint,
            }
        ):
            return False, meta
        return True, meta
    except Exception:
        return False, meta


def _runtime_evidence(
    config: Any,
    runtime_state: Mapping[str, Any] | None,
    *,
    attempt_id: str,
    attempt: Mapping[str, Any] | None,
    video: Path | None,
) -> tuple[bool, bool]:
    if not isinstance(attempt, Mapping) or video is None:
        return False, False
    claimed_after_gate_start = False
    try:
        from m2_guardrail_runtime import configuration_fingerprint
        from source_analyzer import DECISION_SCHEMA_VERSION, DECISION_VERSION
        from source_decision import SOURCE_DECISION_CONTRACT

        payload = _runtime_payload(runtime_state)
        if not isinstance(payload, Mapping):
            return False, False
        gate_start = _strict_float(payload.get("gate_start_epoch"))
        claimed_at = _strict_float(attempt.get("started_at"))
        pre_gate = payload.get("pre_gate_running")
        if gate_start > 0 and claimed_at > gate_start and isinstance(pre_gate, Mapping):
            attempt_key = _job_key(attempt_id)
            queue_key = _job_key(str(video))
            claimed_after_gate_start = not (
                attempt_key in set(pre_gate.get("attempt_keys") or ())
                or queue_key in set(pre_gate.get("queue_job_keys") or ())
            )
        wrapper = runtime_state if isinstance(runtime_state, Mapping) else {}
        if isinstance(wrapper.get("state"), Mapping) and (
            str(wrapper.get("status") or "") != "ARMED"
            or str(wrapper.get("reason_code") or "") != "runtime_baseline_match"
        ):
            return False, claimed_after_gate_start
        baseline = payload.get("baseline")
        if (
            str(payload.get("status") or "") != "ARMED"
            or not isinstance(baseline, Mapping)
            or not _valid_commit(baseline.get("worker_commit_sha"))
            or not _valid_commit(baseline.get("webui_commit_sha"))
            or not _valid_sha256(baseline.get("worker_source_revision"))
            or not _valid_sha256(baseline.get("webui_source_revision"))
            or not _IMAGE_RE.fullmatch(
                str(baseline.get("worker_image_id") or "").casefold()
            )
            or not _IMAGE_RE.fullmatch(
                str(baseline.get("webui_image_id") or "").casefold()
            )
            or str(baseline.get("configuration_fingerprint") or "")
            != configuration_fingerprint(config)
            or str(baseline.get("decision_schema_version") or "")
            != str(DECISION_SCHEMA_VERSION)
            or str(baseline.get("decision_version") or "") != str(DECISION_VERSION)
            or str(baseline.get("decision_contract") or "")
            != SOURCE_DECISION_CONTRACT
            or payload.get("production_resources_affected") is not False
        ):
            return False, claimed_after_gate_start
        encoded = json.dumps(
            dict(baseline),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_baseline_version = (
            "m2-guardrail-v1:" + hashlib.sha256(encoded).hexdigest()[:24]
        )
        if str(payload.get("gate_baseline_version") or "") != expected_baseline_version:
            return False, claimed_after_gate_start
        return claimed_after_gate_start, claimed_after_gate_start
    except Exception:
        return False, claimed_after_gate_start


def _final_state_evidence(
    video: Path | None,
    attempt: Mapping[str, Any] | None,
    obligation: Mapping[str, Any] | None,
    queue: Mapping[str, Any] | None,
    job: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
) -> bool:
    if not all(
        isinstance(item, Mapping)
        for item in (attempt, obligation, queue, job)
    ) or video is None:
        return False
    try:
        stat = video.stat()
        return bool(
            str(attempt.get("status") or "") == "succeeded"
            and _strict_float(attempt.get("finished_at"))
            >= _strict_float(attempt.get("started_at"))
            > 0
            and str(attempt.get("obligation_id") or "")
            == str(obligation.get("obligation_id") or "")
            and str(obligation.get("state") or "") == "succeeded"
            and _strict_float(obligation.get("verified_at")) > 0
            and _same_path(obligation.get("canonical_path"), video)
            and _strict_int(obligation.get("media_size")) == int(stat.st_size)
            and _strict_int(obligation.get("media_mtime_ns"))
            == int(stat.st_mtime_ns)
            and str(queue.get("status") or "") == "done"
            and str(job.get("state") or "") == "COMPLETED"
            and _strict_float(job.get("completed_at")) > 0
            and _same_path(job.get("canonical_path"), video)
            and _strict_int(job.get("media_size")) == int(stat.st_size)
            and _strict_int(job.get("media_mtime_ns")) == int(stat.st_mtime_ns)
            and manifest.get("valid") is True
        )
    except Exception:
        return False


def _no_unresolved_state_evidence(
    state: Any,
    pipeline: Any,
    video: Path | None,
    config: Any,
    attempt: Mapping[str, Any] | None,
    obligation: Mapping[str, Any] | None,
    queue: Mapping[str, Any] | None,
    job: Mapping[str, Any] | None,
    provenance: Mapping[str, Any] | None,
    manifest: Mapping[str, Any],
) -> tuple[bool, dict[str, bool]]:
    meta = {
        "quarantined": False,
        "unresolved_retry": False,
        "unresolved_fallback": False,
    }
    if (
        video is None
        or pipeline is None
        or not all(
            isinstance(item, Mapping)
            for item in (attempt, obligation, queue, job, provenance)
        )
    ):
        return False, meta
    try:
        from control_state import open_ai_quality_review_for_target
        from output_manifest import output_publication_marker_path
        from subtitle_paths import paths_for_video
        from transcriber import asr_transcription_hold_path
        from translation_quality import translation_quality_hold_path

        delivery_running = _query_rows(
            getattr(state, "_conn", None),
            """
            SELECT attempt_id FROM ai_delivery_attempts
            WHERE obligation_id=? AND status='running'
            """,
            (str(obligation.get("obligation_id") or ""),),
        )
        pipeline_running = _query_rows(
            getattr(pipeline, "_conn", None),
            """
            SELECT stage_attempt_id FROM pipeline_stage_attempts
            WHERE job_id=? AND status='RUNNING'
            """,
            (str(job.get("job_id") or ""),),
        )
        paths = paths_for_video(video, config)
        asr_hold = asr_transcription_hold_path(paths.ja_srt, config).exists()
        translation_hold = translation_quality_hold_path(paths.zh_cn_srt).exists()
        publication_hold = output_publication_marker_path(video, config).exists()
        completed_hold = False
        if bool(getattr(config, "completed_delivery_enabled", False)):
            from completed_delivery import completed_delivery_marker_path

            completed_hold = completed_delivery_marker_path(video, config).exists()
        open_review = open_ai_quality_review_for_target(config, str(video))
        text = " ".join(
            str((attempt or {}).get(key) or "")
            for key in ("stage", "error_code", "detail")
        ).casefold()
        meta["quarantined"] = bool(
            "quarantine" in text
            and str(attempt.get("status") or "") != "succeeded"
        )
        meta["unresolved_retry"] = bool(
            str(queue.get("status") or "")
            in {"queued", "running", "failed_retry"}
            or str(job.get("state") or "") == "RETRYING"
            or delivery_running
            or pipeline_running
        )
        meta["unresolved_fallback"] = bool(
            "fallback" in text
            and str(attempt.get("status") or "") != "succeeded"
        )
        terminal_error = job.get("terminal_error")
        if terminal_error is None:
            terminal_error = _decode_json_mapping(job.get("terminal_error_json"))
        clean = bool(
            str(attempt.get("status") or "") == "succeeded"
            and str(obligation.get("state") or "") == "succeeded"
            and str(queue.get("status") or "") == "done"
            and str(job.get("state") or "") == "COMPLETED"
            and not str(job.get("active_stage_attempt_id") or "")
            and not delivery_running
            and not pipeline_running
            and open_review is None
            and not asr_hold
            and not translation_hold
            and not publication_hold
            and not completed_hold
            and not terminal_error
            and str(provenance.get("status") or "") == "complete"
            and manifest.get("valid") is True
            and not any(meta.values())
        )
        return clean, meta
    except Exception:
        return False, meta


def _terminal_status(
    attempt: Mapping[str, Any] | None,
    queue: Mapping[str, Any] | None,
    job: Mapping[str, Any] | None,
) -> str:
    status = str((attempt or {}).get("status") or "").casefold()
    mapped = _TERMINAL_STATUS.get(status)
    if mapped:
        return mapped
    pipeline_state = str((job or {}).get("state") or "").upper()
    if pipeline_state in {"COMPLETED", "NEEDS_REVIEW", "FAILED", "RETRYING"}:
        return pipeline_state
    queue_state = str((queue or {}).get("status") or "").casefold()
    if queue_state == "done":
        return "COMPLETED"
    if queue_state in {"queued", "running"}:
        return queue_state.upper()
    return "UNKNOWN"


def _runtime_payload(
    runtime_state: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if not isinstance(runtime_state, Mapping):
        return None
    nested = runtime_state.get("state")
    return nested if isinstance(nested, Mapping) else runtime_state


def _runtime_is_tripped(runtime_state: Mapping[str, Any] | None) -> bool:
    if not isinstance(runtime_state, Mapping):
        return False
    payload = _runtime_payload(runtime_state)
    return bool(
        str(runtime_state.get("status") or "") == "TRIPPED"
        or str((payload or {}).get("status") or "") == "TRIPPED"
    )


def _quality_role(language: Any, strategy: str) -> str:
    normalized = str(language or "").strip().casefold()
    if normalized in {"ja", "jpn"}:
        return "japanese" if strategy in {
            "ASR_JA_AUDIO",
            "TRANSLATE_JA_SUBTITLE",
        } else "source"
    if normalized in {"zh-cn", "zh-hans"}:
        return "translated_zh_cn"
    if normalized in {"zh-tw", "zh-hant"}:
        return "translated_zh_tw"
    return "unknown"


def _is_final_subtitle_path(path: Path) -> bool:
    if path.suffix.casefold() not in _FINAL_ARTIFACT_SUFFIXES or not path.is_file():
        return False
    filename_markers = set(path.name.casefold().split(".")[1:])
    directory_markers = {
        part.casefold().strip(".") for part in path.parts[:-1] if part
    }
    return not bool(
        _FORBIDDEN_ARTIFACT_MARKERS.intersection(filename_markers)
        or _FORBIDDEN_ARTIFACT_MARKERS.intersection(directory_markers)
    )


def _stable_json_file(path: Path) -> tuple[dict[str, Any], str]:
    data, _signature = _stable_read(path)
    payload = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
    if not isinstance(payload, dict):
        raise ValueError("JSON root is not an object")
    return payload, hashlib.sha256(data).hexdigest()


def _stable_file_hash(path: Path) -> tuple[str, tuple[int, int, int, int]]:
    data, signature = _stable_read(path)
    return hashlib.sha256(data).hexdigest(), signature


def _stable_read(path: Path) -> tuple[bytes, tuple[int, int, int, int]]:
    with path.open("rb") as handle:
        before = _stat_signature(os.fstat(handle.fileno()))
        data = handle.read()
        after = _stat_signature(os.fstat(handle.fileno()))
    current = _stat_signature(path.stat())
    if before != after or after != current:
        raise OSError("file changed while evidence was collected")
    return data, current


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(getattr(value, "st_dev", 0) or 0),
        int(getattr(value, "st_ino", 0) or 0),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _canonical_json_object(value: Any) -> tuple[dict[str, Any], str]:
    raw = str(value if value is not None else "")
    decoded = json.loads(raw, parse_constant=_reject_json_constant)
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON is not an object")
    canonical = json.dumps(
        decoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != raw:
        raise ValueError("stored JSON is not canonical")
    return decoded, raw


def _decode_json_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"), parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"invalid": True}
    return decoded if isinstance(decoded, dict) else {"invalid": True}


def _query_rows(
    connection: Any,
    sql: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    if connection is None:
        raise RuntimeError("database connection is unavailable")
    cursor = connection.execute(sql, tuple(parameters))
    columns = [str(item[0]) for item in cursor.description or ()]
    rows: list[dict[str, Any]] = []
    for row in cursor.fetchall():
        if isinstance(row, Mapping):
            rows.append(dict(row))
        else:
            rows.append(dict(zip(columns, row, strict=True)))
    return rows


def _resolved_path(value: str | Path) -> Path | None:
    try:
        return Path(value).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return None


def _same_path(left: Any, right: str | Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
            os.path.abspath(os.fspath(right))
        )
    except (TypeError, ValueError):
        return False


def _strict_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer evidence value")
    return int(value)


def _strict_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric evidence value")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite evidence value")
    return parsed


def _valid_sha256(value: Any) -> bool:
    return _SHA256_RE.fullmatch(str(value or "").casefold()) is not None


def _valid_commit(value: Any) -> bool:
    return _COMMIT_RE.fullmatch(str(value or "").casefold()) is not None


def _safe_code(value: Any, default: str) -> str:
    normalized = str(value or default).strip().casefold()
    return _SAFE_CODE_RE.sub("_", normalized).strip("_.-")[:120]


def _job_key(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")
