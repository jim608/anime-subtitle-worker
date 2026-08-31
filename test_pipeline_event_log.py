from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from pipeline_event_log import append_pipeline_event


class PipelineEventLogTest(unittest.TestCase):
    def test_transition_and_retry_evidence_are_structured_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = append_pipeline_event(
                temp_dir,
                "state_transition",
                job_id="job-1",
                state="RETRYING",
                stage="ASR",
                attempt=2,
                reason_code="transient_timeout",
                confidence=0.95,
                evidence={"retry_after_seconds": 30},
            )
            second = append_pipeline_event(
                temp_dir,
                "state_transition",
                job_id="job-1",
                state="QUEUED",
                reason_code="retry_due",
            )

            self.assertEqual(first, second)
            rows = [json.loads(line) for line in Path(first).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["state"] for row in rows], ["RETRYING", "QUEUED"])
            self.assertEqual(rows[0]["reason_code"], "transient_timeout")
            self.assertEqual(rows[0]["evidence"]["retry_after_seconds"], 30)
            self.assertEqual(rows[0]["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()

