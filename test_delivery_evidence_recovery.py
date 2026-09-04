from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from delivery_evidence_recovery import reconcile_delivery_evidence_visibility_race


class _State:
    def __init__(self) -> None:
        self.completed = False
        self.closed = False

    def rollback(self):
        return None

    def close(self):
        self.closed = True

    def prioritize_ai_queue_candidate(self, _video):
        return None

    def ai_queue_candidate_snapshot(self, _video):
        return (
            {"status": "done"}
            if self.completed
            else {
                "status": "failed_retry",
                "last_error_code": "delivery_evidence_missing",
                "mtime_ns": 123,
                "failure_revision": "a" * 24,
            }
        )

    def get_ai_delivery_obligation(self, _obligation_id):
        return {
            "state": "succeeded" if self.completed else "open",
            "policy_revision": "policy",
        }

    def latest_ai_delivery_attempt(self, _obligation_id):
        return {
            "attempt_id": "old-attempt",
            "status": "retryable_failure",
            "stage": "delivery_verification",
            "error_code": "delivery_evidence_missing",
        }

    def get_ai_delivery_attempt(self, _attempt_id):
        return {"status": "succeeded" if self.completed else "running"}


class DeliveryEvidenceRecoveryTest(unittest.TestCase):
    def test_exact_visibility_race_republishes_without_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "episode.mkv"
            output = root / "episode.zh-TW.ass"
            manifest = root / "manifest.json"
            video.write_bytes(b"media")
            output.write_text("subtitle", encoding="utf-8")
            manifest.write_text(
                json.dumps(
                    {
                        "outputs": [{"path": str(output)}],
                        "provenance": {"source": "verified"},
                    }
                ),
                encoding="utf-8",
            )
            state = _State()
            lock = Mock()
            lock.acquire.return_value = True
            publication = {
                "kind": "adopted_zh_tw",
                "output_languages": ["zh-TW"],
            }

            def mark_result(*_args, **_kwargs):
                state.completed = True

            with (
                patch("delivery_evidence_recovery.VideoLock", return_value=lock),
                patch(
                    "delivery_evidence_recovery.ScanStateStore.from_config",
                    return_value=state,
                ),
                patch(
                    "delivery_evidence_recovery.delivery_identity",
                    return_value={"obligation_id": "obligation"},
                ),
                patch(
                    "delivery_evidence_recovery.output_manifest_path",
                    return_value=manifest,
                ),
                patch(
                    "delivery_evidence_recovery.manifest_publication_semantics",
                    return_value=publication,
                ),
                patch(
                    "delivery_evidence_recovery.publication_is_traditional_chinese_delivery",
                    return_value=True,
                ),
                patch("delivery_evidence_recovery.validate_output_manifest", return_value=True),
                patch(
                    "delivery_evidence_recovery.capture_source_snapshot",
                    return_value=SimpleNamespace(),
                ),
                patch("delivery_evidence_recovery.verify_source_snapshot"),
                patch("delivery_evidence_recovery.begin_output_publication"),
                patch("delivery_evidence_recovery.finish_output_publication"),
                patch(
                    "delivery_evidence_recovery.write_output_manifest",
                    return_value=manifest,
                ) as republish,
                patch("main._ai_queue_paused", return_value=True),
                patch("main._m2_server_canary_admit_new_job", return_value=True),
                patch("main._mark_queue_running", return_value="new-attempt") as claim,
                patch(
                    "main._mark_queue_result_and_observe",
                    side_effect=mark_result,
                ) as settle,
            ):
                result = reconcile_delivery_evidence_visibility_race(
                    SimpleNamespace(
                        completed_delivery_enabled=False,
                        source_integrity_sha256_enabled=False,
                    ),
                    video,
                    expected_media_mtime_ns=123,
                    expected_failure_revision="a" * 24,
                    expected_attempt_id="old-attempt",
                )

        self.assertEqual(result["queue_status"], "done")
        self.assertEqual(result["attempt_id"], "new-attempt")
        claim.assert_called_once()
        republish.assert_called_once()
        settle.assert_called_once()
        self.assertTrue(state.closed)
        lock.release.assert_called_once()

    def test_guardrail_refuses_recovery_before_queue_or_checkpoint_mutation(self) -> None:
        config = SimpleNamespace(completed_delivery_enabled=False)
        state = _State()
        with (
            patch(
                "delivery_evidence_recovery.ScanStateStore.from_config",
                return_value=state,
            ) as open_state,
            patch("main._m2_server_canary_admit_new_job", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "guardrail stopped"):
                reconcile_delivery_evidence_visibility_race(
                    config,
                    Path("synthetic.mkv"),
                    expected_media_mtime_ns=123,
                    expected_failure_revision="a" * 24,
                    expected_attempt_id="old-attempt",
                )

        open_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
