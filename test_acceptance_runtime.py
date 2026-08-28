from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from acceptance_runtime import (
    AcceptanceRuntimeContextError,
    build_acceptance_attempt_context,
    load_and_verify_acceptance_attempt_context,
    serialize_acceptance_attempt_context,
)
from scan_state import ScanStateStore, ai_delivery_identity


class AcceptanceRuntimeImageTests(unittest.TestCase):
    def test_worker_image_includes_acceptance_package(self) -> None:
        dockerfile = Path(__file__).with_name("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY acceptance /app/acceptance", dockerfile)


class AcceptanceRuntimeContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"
        self.work = self.root / "work"
        self.input.mkdir()
        self.work.mkdir()
        self.video = self.input / "episode-000.mkv"
        self.video.write_bytes(b"acceptance-media")
        stat = self.video.stat()
        self.policy_revision = "a" * 64
        self.identity = ai_delivery_identity(
            self.video,
            media_size=stat.st_size,
            media_mtime_ns=stat.st_mtime_ns,
            policy_revision=self.policy_revision,
        )
        self.run_id = "accrun_" + "1" * 48
        self.plan_path = self.work / "plan.json"
        cases = []
        for index in range(100):
            if index == 0:
                media = {
                    "canonical_path": self.identity["canonical_path"],
                    "media_size": self.identity["media_size"],
                    "media_mtime_ns": self.identity["media_mtime_ns"],
                    "media_fingerprint": self.identity["media_fingerprint"],
                    "policy_revision": self.identity["policy_revision"],
                    "obligation_id": self.identity["obligation_id"],
                    "source_sha256": "f" * 64,
                }
                faults = [
                    {
                        "fault_id": "fault-000-worker-crash",
                        "scenario": "worker_crash",
                        "trigger": "after durable attempt claim",
                    }
                ]
            else:
                media = {
                    "canonical_path": str((self.input / f"episode-{index:03d}.mkv").resolve()),
                    "media_size": index + 1,
                    "media_mtime_ns": index + 1000,
                    "media_fingerprint": f"{index + 10000:064x}",
                    "policy_revision": self.policy_revision,
                    "obligation_id": f"aiobl_{index + 20000:064x}",
                    "source_sha256": f"{index + 30000:064x}",
                }
                faults = []
            cases.append(
                {
                    "case_id": f"case-{index:03d}",
                    "media": media,
                    "faults": faults,
                }
            )
        self.plan_path.write_text(
            json.dumps(
                {
                    "contract": "anime-unattended-acceptance-plan-v1",
                    "schema_version": 3,
                    "suite_id": self.run_id,
                    "run_id": self.run_id,
                    "nonce": "b" * 64,
                    "pre_admission": {},
                    "cases": cases,
                }
            ),
            encoding="utf-8",
        )
        self.config = SimpleNamespace(
            acceptance_queue_lane_enabled=True,
            acceptance_queue_lane_plan_path=self.plan_path,
            input_path=self.input,
            work_path=self.work,
            scanner_state_path="state.sqlite3",
        )
        self.state = ScanStateStore.from_config(self.config)
        self.state.upsert_ai_queue_candidate(self.video, stat.st_mtime_ns)
        obligation = self.state.ensure_ai_delivery_obligation(
            self.video,
            media_size=stat.st_size,
            media_mtime_ns=stat.st_mtime_ns,
            policy_revision=self.policy_revision,
            obligation_id=self.identity["obligation_id"],
            acceptance_run_id=self.run_id,
            eligible_at=1000,
        )
        self.state.mark_ai_queue_running(self.video)
        self.attempt = self.state.begin_ai_delivery_attempt(
            obligation["obligation_id"],
            acceptance_run_id=self.run_id,
            started_at=1001,
        )
        self.state.commit()

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_child_revalidates_exact_running_attempt_read_only(self) -> None:
        context = build_acceptance_attempt_context(
            self.state,
            self.config,
            self.video,
            self.attempt["attempt_id"],
        )
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.case_id, "case-000")
        self.assertEqual(context.fault_id, "fault-000-worker-crash")
        self.assertEqual(context.fault_scenario, "worker_crash")

        verified = load_and_verify_acceptance_attempt_context(
            self.config,
            self.video,
            serialized=serialize_acceptance_attempt_context(context),
        )

        self.assertEqual(verified, context)
        self.assertEqual(
            self.state.get_ai_delivery_attempt(self.attempt["attempt_id"])["status"],
            "running",
        )
        self.assertEqual(
            self.state.get_ai_delivery_obligation(self.identity["obligation_id"])[
                "attempt_count"
            ],
            1,
        )

    def test_child_rejects_attempt_that_is_no_longer_running(self) -> None:
        context = build_acceptance_attempt_context(
            self.state,
            self.config,
            self.video,
            self.attempt["attempt_id"],
        )
        assert context is not None
        self.state.finish_ai_delivery_attempt(
            self.attempt["attempt_id"],
            status="retryable_failure",
            error_code="test_failure",
            finished_at=1002,
        )
        self.state.commit()

        with self.assertRaisesRegex(AcceptanceRuntimeContextError, "exact running"):
            load_and_verify_acceptance_attempt_context(
                self.config,
                self.video,
                serialized=serialize_acceptance_attempt_context(context),
            )

    def test_lane_off_ignores_context_and_preserves_production_path(self) -> None:
        context = build_acceptance_attempt_context(
            self.state,
            self.config,
            self.video,
            self.attempt["attempt_id"],
        )
        assert context is not None
        self.config.acceptance_queue_lane_enabled = False

        self.assertIsNone(
            build_acceptance_attempt_context(
                object(),
                self.config,
                self.video,
                "not-an-attempt",
            )
        )
        self.assertIsNone(
            load_and_verify_acceptance_attempt_context(
                self.config,
                self.video,
                serialized=serialize_acceptance_attempt_context(context),
            )
        )


if __name__ == "__main__":
    unittest.main()
