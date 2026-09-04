from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from config import load_config
from lock import VideoLock
from output_manifest import (
    begin_output_publication,
    delivery_identity,
    finish_output_publication,
    manifest_publication_semantics,
    output_manifest_path,
    publication_is_traditional_chinese_delivery,
    validate_output_manifest,
    write_output_manifest,
)
from scan_state import ScanStateStore
from source_integrity import capture_source_snapshot, verify_source_snapshot


def reconcile_delivery_evidence_visibility_race(
    config: Any,
    video: Path,
    *,
    expected_media_mtime_ns: int,
    expected_failure_revision: str,
    expected_attempt_id: str,
    logger: logging.Logger | None = None,
) -> dict[str, object]:
    """Republish and settle one exact ``delivery_evidence_missing`` failure.

    The global queue must be paused. The persisted failure identity is used
    for an exact queue CAS and a normal delivery attempt. Only the existing
    verified manifest is republished from unchanged output files; ASR,
    translation, and ASS generation are never invoked.
    """

    from main import _ai_queue_paused, _mark_queue_result, _mark_queue_running

    resolved = Path(video).resolve()
    if not _ai_queue_paused(config):
        raise RuntimeError("AI queue must be paused before delivery evidence recovery")
    if bool(getattr(config, "completed_delivery_enabled", False)):
        raise RuntimeError("Completed-media delivery requires its own receipt republish path")
    lock = VideoLock(resolved)
    if not lock.acquire():
        raise RuntimeError("Target video is currently locked by another operation")

    state = ScanStateStore.from_config(config)
    log = logger or logging.getLogger("delivery_evidence_recovery")
    new_attempt_id = ""
    marker_started = False
    settled = False
    try:
        snapshot = state.ai_queue_candidate_snapshot(resolved)
        if not isinstance(snapshot, dict) or (
            str(snapshot.get("status") or "") != "failed_retry"
            or str(snapshot.get("last_error_code") or "")
            != "delivery_evidence_missing"
            or int(snapshot.get("mtime_ns") or 0) != int(expected_media_mtime_ns)
            or str(snapshot.get("failure_revision") or "")
            != str(expected_failure_revision)
        ):
            raise RuntimeError("Exact AI queue failure binding changed")

        identity = delivery_identity(resolved, config)
        obligation_id = str(identity["obligation_id"])
        obligation = state.get_ai_delivery_obligation(obligation_id)
        previous_attempt = state.latest_ai_delivery_attempt(obligation_id)
        if not isinstance(obligation, dict) or str(obligation.get("state") or "") != "open":
            raise RuntimeError("Delivery obligation is not open")
        if not isinstance(previous_attempt, dict) or (
            str(previous_attempt.get("attempt_id") or "") != str(expected_attempt_id)
            or str(previous_attempt.get("status") or "") != "retryable_failure"
            or str(previous_attempt.get("stage") or "") != "delivery_verification"
            or str(previous_attempt.get("error_code") or "")
            != "delivery_evidence_missing"
        ):
            raise RuntimeError("Latest delivery attempt is not the exact recoverable failure")

        manifest = output_manifest_path(resolved, config)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        publication = manifest_publication_semantics(payload)
        outputs = payload.get("outputs") if isinstance(payload, dict) else None
        if (
            publication is None
            or not publication_is_traditional_chinese_delivery(publication)
            or not isinstance(outputs, list)
            or not outputs
            or not validate_output_manifest(
                resolved,
                config,
                verify_hashes=True,
                require_delivery_evidence=True,
                expected_obligation_id=obligation_id,
                expected_policy_revision=str(obligation["policy_revision"]),
                expected_publication_kind=str(publication["kind"]),
                expected_output_languages=tuple(publication["output_languages"]),
                require_publication_semantics=True,
            )
        ):
            raise RuntimeError("Existing manifest is not strict reusable delivery evidence")
        output_paths = [Path(str(item.get("path") or "")) for item in outputs]
        if any(not path.is_file() for path in output_paths):
            raise RuntimeError("Existing manifest output set changed before republish")

        source_snapshot = capture_source_snapshot(
            resolved,
            hash_content=bool(
                getattr(config, "source_integrity_sha256_enabled", False)
            ),
        )
        state.prioritize_ai_queue_candidate(resolved)
        new_attempt_id = _mark_queue_running(
            state,
            resolved,
            config,
            canary_binding={
                "expected_failure_revision": str(expected_failure_revision),
                "expected_failure_code": "delivery_evidence_missing",
                "expected_media_mtime_ns": int(expected_media_mtime_ns),
            },
        )
        if not new_attempt_id:
            raise RuntimeError("Exact recovery claim did not create a delivery attempt")

        begin_output_publication(resolved, config)
        marker_started = True
        republished = write_output_manifest(
            resolved,
            config,
            output_paths,
            provenance=dict(payload.get("provenance") or {}),
            obligation_id=obligation_id,
            publication_kind=str(publication["kind"]),
            output_languages=tuple(publication["output_languages"]),
        )
        verify_source_snapshot(source_snapshot)
        finish_output_publication(resolved, config)
        marker_started = False
        if not validate_output_manifest(
            resolved,
            config,
            verify_hashes=True,
            required_outputs=tuple(output_paths),
            require_delivery_evidence=True,
            expected_obligation_id=obligation_id,
            expected_policy_revision=str(obligation["policy_revision"]),
            expected_publication_kind=str(publication["kind"]),
            expected_output_languages=tuple(publication["output_languages"]),
            require_publication_semantics=True,
        ):
            raise RuntimeError("Republished manifest failed strict verification")

        _mark_queue_result(
            state,
            resolved,
            True,
            config,
            delivery_attempt_id=new_attempt_id,
        )
        settled = True
        final_queue = state.ai_queue_candidate_snapshot(resolved)
        final_obligation = state.get_ai_delivery_obligation(obligation_id)
        final_attempt = state.get_ai_delivery_attempt(new_attempt_id)
        if (
            not isinstance(final_queue, dict)
            or str(final_queue.get("status") or "") != "done"
            or not isinstance(final_obligation, dict)
            or str(final_obligation.get("state") or "") != "succeeded"
            or not isinstance(final_attempt, dict)
            or str(final_attempt.get("status") or "") != "succeeded"
        ):
            raise RuntimeError("Exact delivery recovery did not reach a consistent terminal state")
        return {
            "queue_status": "done",
            "obligation_id": obligation_id,
            "obligation_state": "succeeded",
            "attempt_id": new_attempt_id,
            "attempt_status": "succeeded",
            "manifest_path": str(republished),
        }
    except Exception:
        if new_attempt_id and not settled:
            _mark_queue_result(
                state,
                resolved,
                False,
                config,
                delivery_attempt_id=new_attempt_id,
            )
        else:
            state.rollback()
        raise
    finally:
        if marker_started:
            log.error("Publication marker remains for fail-closed retry: %s", resolved)
        state.close()
        lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Republish and reconcile one exact delivery visibility race."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--expected-media-mtime-ns", required=True, type=int)
    parser.add_argument("--expected-failure-revision", required=True)
    parser.add_argument("--expected-attempt-id", required=True)
    args = parser.parse_args()
    result = reconcile_delivery_evidence_visibility_race(
        load_config(args.config),
        Path(args.video),
        expected_media_mtime_ns=args.expected_media_mtime_ns,
        expected_failure_revision=args.expected_failure_revision,
        expected_attempt_id=args.expected_attempt_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
