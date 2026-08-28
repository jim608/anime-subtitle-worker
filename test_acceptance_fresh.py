from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import time
import unittest

from acceptance.admission import admit_fresh_plan
from acceptance.fresh import prepare_fresh_plan
from acceptance.harness import AcceptanceInputError, validate_observations, validate_plan, validate_plan_structure
from acceptance_queue_lane import load_acceptance_queue_lane
from output_manifest import output_manifest_path
from run_unattended_acceptance import _write_new_fresh_plan, build_parser
from safe_files import sha256_file
from scan_state import ScanStateStore


class FreshAcceptancePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"
        self.work = self.root / "work"
        self.completed = self.root / "completed"
        self.input.mkdir()
        self.work.mkdir()
        self.completed.mkdir()
        self.config = SimpleNamespace(
            input_path=self.input,
            work_path=self.work,
            scanner_state_path="scanner.sqlite3",
            control_state_path="control.sqlite3",
            completed_delivery_enabled=True,
            completed_delivery_path=str(self.completed),
            completed_delivery_manifest_path="completed_delivery_manifests",
            completed_delivery_source_policy="retain",
            ai_output_manifest_path="manifests",
            processing_provenance_path="provenance",
        )
        self.paths: list[Path] = []
        cases = []
        routes = (
            "existing_zh_tw",
            "zh_cn_opencc",
            "japanese_subtitle_translation",
            "japanese_audio_asr",
        )
        for index in range(100):
            series = self.input / f"series-{index // 10:02d}"
            series.mkdir(exist_ok=True)
            suffix = ".mkv" if index < 50 else ".mp4"
            path = series / f"episode-{index:03d}{suffix}"
            path.write_bytes(f"fresh-media-{index}\n".encode("ascii"))
            self.paths.append(path)
            cases.append(
                {
                    "case_id": f"fresh-{index:03d}",
                    "canonical_path": str(path.resolve()),
                    "expected_route": routes[index % len(routes)],
                    "release_profile": f"fixture-{index % 5}",
                }
            )
        self.corpus = self.root / "corpus.json"
        self.corpus.write_text(
            json.dumps(
                {
                    "contract": "anime-unattended-pre-admission-corpus-v1",
                    "schema_version": 1,
                    "nonce": "a" * 64,
                    "cases": cases,
                }
            ),
            encoding="utf-8",
        )
        self.plan_path = self.root / "run" / "plan.json"
        self.now = time.time()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _index(path: Path) -> int:
        return int(path.stem.rsplit("-", 1)[1])

    @classmethod
    def _probe(cls, path: Path) -> dict:
        index = cls._index(path)
        return {
            "duration_seconds": (300.0, 1_200.0, 2_500.0)[index % 3],
            "format_names": ["matroska"] if path.suffix == ".mkv" else ["mov", "mp4"],
            "video_streams": 1,
            "audio_streams": 1,
            "audio_layout": "aac:ja:2:stereo:default=1:forced=0",
            "subtitle_layout": "none",
        }

    def _prepare(self, *, clock=None) -> dict:
        return prepare_fresh_plan(
            self.corpus,
            self.config,
            plan_output=self.plan_path,
            media_probe=self._probe,
            clock=clock or (lambda: self.now),
        )

    def _write_admission_plan(self) -> dict:
        state = ScanStateStore.from_config(self.config)
        state.close()
        payload = self._prepare()
        self.assertTrue(payload["ready"], payload["gaps"])
        _write_new_fresh_plan(self.plan_path, payload["plan"], payload["run_claim"])
        self.config.acceptance_queue_lane_enabled = True
        self.config.acceptance_queue_lane_plan_path = str(self.plan_path)
        return payload

    def _admission_counts(self) -> tuple[int, int, int]:
        connection = sqlite3.connect(self.work / "scanner.sqlite3")
        try:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "ai_candidate_queue",
                    "ai_delivery_obligations",
                    "ai_delivery_attempts",
                )
            )
        finally:
            connection.close()

    def test_pre_admission_plan_is_fixed_fresh_and_claimed_once(self) -> None:
        payload = self._prepare()
        self.assertTrue(payload["ready"], payload["gaps"])
        plan = payload["plan"]
        self.assertEqual(3, plan["schema_version"])
        self.assertEqual(plan["suite_id"], plan["run_id"])
        self.assertEqual(self.now, plan["created_at"])
        self.assertEqual([], validate_plan_structure(plan))
        self.assertEqual(100, len(plan["cases"]))
        self.assertEqual(10, sum(bool(case["faults"]) for case in plan["cases"]))
        for case in plan["cases"]:
            self.assertEqual(case["media"]["source_sha256"], case["completed_delivery"]["source_sha256"])
            self.assertTrue(all(value is True for key, value in case["pre_admission"].items() if key != "checked_at"))

        _write_new_fresh_plan(self.plan_path, plan, payload["run_claim"])
        self.assertEqual([], validate_plan(plan, self.config, media_probe=self._probe))
        self.config.acceptance_queue_lane_enabled = True
        self.config.acceptance_queue_lane_plan_path = str(self.plan_path)
        lane = load_acceptance_queue_lane(self.config)
        self.assertIsNotNone(lane)
        self.assertEqual(plan["run_id"], lane.run_id)
        self.assertEqual(100, len(lane.targets))
        with self.assertRaises(AcceptanceInputError):
            _write_new_fresh_plan(self.root / "run" / "second.json", plan, payload["run_claim"])

    def test_existing_artifact_fails_pre_admission_baseline(self) -> None:
        manifest = output_manifest_path(self.paths[0], self.config)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}", encoding="utf-8")
        payload = self._prepare()
        self.assertFalse(payload["ready"])
        self.assertTrue(
            any(
                "output_manifest_absent" in gap.get("reasons", [])
                for gap in payload["gaps"]
            ),
            payload["gaps"],
        )

    def test_baseline_timestamps_are_recorded_after_each_case_check(self) -> None:
        tick = {"value": self.now}

        def clock() -> float:
            tick["value"] += 0.001
            return tick["value"]

        plan = self._prepare(clock=clock)["plan"]
        checked = [case["pre_admission"]["checked_at"] for case in plan["cases"]]
        self.assertEqual(100, len(set(checked)))
        self.assertEqual(max(checked), plan["pre_admission"]["baseline_checked_at"])
        self.assertGreaterEqual(plan["created_at"], max(checked))

    def test_fresh_admission_commits_exact_100_once_and_rejects_repeat(self) -> None:
        payload = self._write_admission_plan()
        run_id = payload["plan"]["run_id"]

        result = admit_fresh_plan(
            self.plan_path,
            self.config,
            clock=lambda: self.now + 1,
        )

        self.assertTrue(result["admitted"])
        self.assertEqual((100, 100, 0), self._admission_counts())
        connection = sqlite3.connect(self.work / "scanner.sqlite3")
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_delivery_obligations "
                    "WHERE acceptance_run_id=? AND state='open' AND attempt_count=0",
                    (run_id,),
                ).fetchone()[0],
                100,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_candidate_queue "
                    "WHERE source='acceptance_fresh_admission' AND status='queued'",
                ).fetchone()[0],
                100,
            )
        finally:
            connection.close()

        lane = load_acceptance_queue_lane(self.config)
        self.assertIsNotNone(lane)
        assert lane is not None
        state = ScanStateStore.from_config(self.config)
        try:
            self.assertEqual(
                state.iter_ai_queue_candidates(acceptance_targets=lane.targets),
                [Path(target.canonical_path) for target in lane.targets],
            )
        finally:
            state.close()

        with self.assertRaisesRegex(AcceptanceInputError, "globally empty"):
            admit_fresh_plan(
                self.plan_path,
                self.config,
                source_verifier=lambda _target, _config: None,
            )
        self.assertEqual((100, 100, 0), self._admission_counts())

    def test_fresh_admission_rolls_back_all_rows_on_in_transaction_drift(self) -> None:
        self._write_admission_plan()
        calls = {"count": 0}

        def drift_during_second_pass(_target, _config) -> None:
            calls["count"] += 1
            if calls["count"] == 150:
                raise AcceptanceInputError("source drifted during admission")

        with self.assertRaisesRegex(AcceptanceInputError, "source drifted"):
            admit_fresh_plan(
                self.plan_path,
                self.config,
                source_verifier=drift_during_second_pass,
            )

        self.assertEqual((0, 0, 0), self._admission_counts())

    def test_fresh_admission_rejects_claim_mismatch_without_database_mutation(self) -> None:
        payload = self._write_admission_plan()
        claim_path = Path(payload["plan"]["pre_admission"]["run_claim_path"])
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        claim["unexpected"] = True
        claim_path.write_text(json.dumps(claim), encoding="utf-8")

        with self.assertRaisesRegex(AcceptanceInputError, "claim"):
            admit_fresh_plan(
                self.plan_path,
                self.config,
                source_verifier=lambda _target, _config: None,
            )

        self.assertEqual((0, 0, 0), self._admission_counts())

    def test_fresh_admission_never_migrates_missing_run_id_columns(self) -> None:
        payload = self._write_admission_plan()
        database = self.work / "scanner.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "ALTER TABLE ai_delivery_attempts DROP COLUMN acceptance_run_id"
            )
            connection.execute(
                "ALTER TABLE ai_delivery_obligations DROP COLUMN acceptance_run_id"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(AcceptanceInputError, "will not be migrated"):
            admit_fresh_plan(
                self.plan_path,
                self.config,
                source_verifier=lambda _target, _config: None,
            )

        connection = sqlite3.connect(database)
        try:
            for table in ("ai_delivery_obligations", "ai_delivery_attempts"):
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                self.assertNotIn("acceptance_run_id", columns)
        finally:
            connection.close()
        self.assertEqual((0, 0, 0), self._admission_counts())

    def test_fresh_admission_rejects_non_finite_clock_without_mutation(self) -> None:
        self._write_admission_plan()

        with self.assertRaisesRegex(AcceptanceInputError, "positive timestamp"):
            admit_fresh_plan(
                self.plan_path,
                self.config,
                clock=lambda: float("nan"),
                source_verifier=lambda _target, _config: None,
            )

        self.assertEqual((0, 0, 0), self._admission_counts())

    def test_cli_parser_exposes_explicit_fresh_admission_mode(self) -> None:
        args = build_parser().parse_args(
            [
                "--admit-fresh-plan",
                str(self.plan_path),
                "--config",
                str(self.root / "config.json"),
            ]
        )
        self.assertEqual(args.admit_fresh_plan, self.plan_path)

    def test_fresh_observations_before_plan_are_rejected(self) -> None:
        plan = self._prepare()["plan"]
        evidence = {"kind": "fixture", "path": str(self.corpus), "sha256": sha256_file(self.corpus)}
        cases = []
        for planned in plan["cases"]:
            cases.append(
                {
                    "case_id": planned["case_id"],
                    "acceptance_run_id": plan["run_id"],
                    "canonical_path": planned["media"]["canonical_path"],
                    "obligation_id": planned["media"]["obligation_id"],
                    "route": planned["expected_route"],
                    "started_at": plan["created_at"] - 1,
                    "finished_at": plan["created_at"] + 1,
                    "outcome": "completed",
                    "review_required": False,
                    "manual_interventions": [],
                    "errors": [],
                    "evidence": [evidence],
                    "completed_delivery": {"receipt": evidence, "final_mkv": evidence},
                    "faults": [],
                }
            )
        observations = {
            "contract": "anime-unattended-acceptance-observations-v1",
            "schema_version": 3,
            "suite_id": plan["suite_id"],
            "run_id": plan["run_id"],
            "plan_sha256": "0" * 64,
            "started_at": plan["created_at"] - 1,
            "finished_at": plan["created_at"] + 1,
            "manual_interventions": [],
            "cases": cases,
        }
        errors = validate_observations(observations, plan=plan, plan_file_sha256="0" * 64)
        self.assertIn("fresh observations started before plan created_at", errors)


if __name__ == "__main__":
    unittest.main()
