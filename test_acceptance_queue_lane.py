from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from acceptance_queue_lane import (
    AcceptanceQueueLaneError,
    acceptance_run_id_for_video,
    load_acceptance_queue_lane,
)


class AcceptanceQueueLaneTest(unittest.TestCase):
    def test_lane_is_disabled_by_default_without_reading_a_plan(self) -> None:
        config = SimpleNamespace(work_path=Path("missing"), input_path=Path("missing"))

        self.assertIsNone(load_acceptance_queue_lane(config))
        self.assertEqual(acceptance_run_id_for_video(config, Path("anything.mkv")), "")

    def test_loads_exact_schema_v3_fixed_100_case_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, plan_path = self._write_plan(root, case_count=100)

            lane = load_acceptance_queue_lane(config)

            self.assertIsNotNone(lane)
            assert lane is not None
            self.assertEqual(lane.run_id, "accrun_" + "1" * 48)
            self.assertEqual(len(lane.targets), 100)
            self.assertEqual(lane.targets[0].canonical_path, str((root / "input" / "0.mkv").resolve()))
            self.assertEqual(lane.plan_path, plan_path.resolve())
            self.assertEqual(
                acceptance_run_id_for_video(config, root / "input" / "0.mkv"),
                lane.run_id,
            )
            with self.assertRaisesRegex(AcceptanceQueueLaneError, "non-allowlisted"):
                acceptance_run_id_for_video(config, root / "input" / "other.mkv")

    def test_rejects_non_100_case_plan_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config, _plan_path = self._write_plan(Path(temp_dir), case_count=99)

            with self.assertRaisesRegex(AcceptanceQueueLaneError, "exactly 100"):
                load_acceptance_queue_lane(config)

    @staticmethod
    def _write_plan(root: Path, *, case_count: int):
        input_root = root / "input"
        work_root = root / "work"
        input_root.mkdir()
        work_root.mkdir()
        run_id = "accrun_" + "1" * 48
        cases = []
        for index in range(case_count):
            cases.append(
                {
                    "case_id": f"case-{index:03d}",
                    "media": {
                        "canonical_path": str((input_root / f"{index}.mkv").resolve()),
                        "media_size": index + 1,
                        "media_mtime_ns": index + 100,
                        "media_fingerprint": f"{index + 1000:064x}",
                        "policy_revision": "a" * 64,
                        "obligation_id": f"aiobl_{index + 2000:064x}",
                        "source_sha256": f"{index + 3000:064x}",
                    },
                }
            )
        plan_path = work_root / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "contract": "anime-unattended-acceptance-plan-v1",
                    "schema_version": 3,
                    "suite_id": run_id,
                    "run_id": run_id,
                    "nonce": "b" * 64,
                    "pre_admission": {},
                    "cases": cases,
                }
            ),
            encoding="utf-8",
        )
        config = SimpleNamespace(
            acceptance_queue_lane_enabled=True,
            acceptance_queue_lane_plan_path=plan_path,
            work_path=work_root,
            input_path=input_root,
        )
        return config, plan_path


if __name__ == "__main__":
    unittest.main()
