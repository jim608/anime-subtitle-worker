from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from m2_strict_observation import STRICT_EVIDENCE_KEYS


class LineRepairReviewBoundaryTests(unittest.TestCase):
    def _run(self, *, failure=None, target="/anime/episode.mkv", close=True):
        evidence = {key: True for key in STRICT_EVIDENCE_KEYS}
        evidence["no_unresolved_retry_quarantine_fallback"] = False
        if failure:
            evidence[failure] = False
        with (
            patch("m2_guardrail_runtime.load_runtime_state", return_value={}),
            patch("m2_strict_runtime_evidence.build_m2_strict_runtime_evidence", return_value={"evidence": evidence}),
            patch("control_state.get_review_item", return_value={"target_key": target, "status": "open", "kind": "subtitle_quality"}),
            patch("control_state.resolve_review_item", return_value=close) as resolve,
        ):
            try:
                main._resolve_verified_m2_line_repair_review(
                    object(), Path("/anime/episode.mkv"),
                    SimpleNamespace(m2_server_canary_observer_enabled=True), "attempt-1",
                    review_id="review-1", delivery_evidence={"manifest_sha256": "a" * 64},
                )
            except main._M2StrictCompletionRejected:
                return False, resolve.call_args_list
            return True, resolve.call_args_list

    def test_verified_quality_remediation_is_durable_before_final_strict_commit(self):
        accepted, calls = self._run()
        self.assertTrue(accepted)
        self.assertEqual(len(calls), 1)
        proof = calls[0].args[2]
        self.assertEqual(proof["attempt_id"], "attempt-1")
        self.assertEqual(proof["manifest_sha256"], "a" * 64)
        self.assertTrue(proof["completion_pending_strict_commit"])

    def test_any_other_missing_strict_evidence_keeps_review_open(self):
        for key in STRICT_EVIDENCE_KEYS:
            if key == "no_unresolved_retry_quarantine_fallback":
                continue
            with self.subTest(key=key):
                accepted, calls = self._run(failure=key)
                self.assertFalse(accepted)
                self.assertEqual(calls, [])

    def test_unrelated_review_cannot_be_closed(self):
        accepted, calls = self._run(target="/anime/unrelated.mkv")
        self.assertFalse(accepted)
        self.assertEqual(calls, [])

    def test_lost_review_update_refuses_completion(self):
        accepted, calls = self._run(close=False)
        self.assertFalse(accepted)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
