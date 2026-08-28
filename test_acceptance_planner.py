from __future__ import annotations

from collections import Counter
from copy import copy
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest

from acceptance.harness import AcceptanceInputError, validate_plan_structure
from acceptance.planner import (
    FAULT_ORDER,
    ROUTE_ORDER,
    CorpusCandidateError,
    QueueCandidate,
    prepare_corpus_plan,
    read_queue_candidates,
)
from run_unattended_acceptance import _write_new_plan
from safe_files import sha256_file


class AcceptancePlannerTests(unittest.TestCase):
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
            completed_delivery_enabled=True,
            completed_delivery_path=str(self.completed),
            completed_delivery_manifest_path="completed_delivery_manifests",
            completed_delivery_source_policy="retain",
            ai_output_manifest_path="manifests",
            processing_provenance_path="provenance",
        )
        self.rows: list[QueueCandidate] = []
        for index in range(100):
            series = self.input / f"series-{index // 10:02d}"
            series.mkdir(exist_ok=True)
            suffix = ".mkv" if index < 50 else ".mp4"
            path = series / f"episode-{index:03d}{suffix}"
            path.write_bytes(f"media-{index}\n".encode("ascii"))
            self.rows.append(
                QueueCandidate(
                    path=str(path.resolve()),
                    mtime_ns=path.stat().st_mtime_ns,
                    status="done",
                    source="existing-library",
                )
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _index(path: Path) -> int:
        return int(path.stem.rsplit("-", 1)[1])

    @classmethod
    def _route(cls, path: Path, _config, _identity) -> str:
        return ROUTE_ORDER[cls._index(path) % len(ROUTE_ORDER)]

    @classmethod
    def _probe(cls, path: Path) -> dict:
        index = cls._index(path)
        duration = (300.0, 1_200.0, 2_500.0)[index % 3]
        formats = ["matroska", "webm"] if path.suffix == ".mkv" else ["mov", "mp4"]
        return {
            "duration_seconds": duration,
            "format_names": formats,
            "video_streams": 1,
            "audio_streams": 1,
            "audio_layout": "aac:ja:2:stereo:default=1:forced=0",
            "subtitle_layout": "none",
        }

    def _prepare(self, **kwargs) -> dict:
        return prepare_corpus_plan(
            self.config,
            queue_reader=lambda _config: list(self.rows),
            media_probe=self._probe,
            route_resolver=self._route,
            **kwargs,
        )

    def test_deterministic_exact_100_plan_meets_all_strata(self) -> None:
        first = self._prepare()
        second = self._prepare()
        self.assertTrue(first["ready"])
        self.assertEqual([], first["gaps"])
        self.assertEqual(first["plan"], second["plan"])
        self.assertFalse(first["fault_execution_performed"])

        plan = first["plan"]
        self.assertEqual([], validate_plan_structure(plan))
        self.assertEqual(100, len(plan["cases"]))
        routes = Counter(case["expected_route"] for case in plan["cases"])
        self.assertTrue(all(routes[route] >= 10 for route in ROUTE_ORDER))
        buckets = Counter(case["strata"]["duration_bucket"] for case in plan["cases"])
        self.assertTrue(all(buckets[bucket] >= 10 for bucket in ("short", "standard", "long")))
        containers = Counter(case["strata"]["container"] for case in plan["cases"])
        self.assertGreaterEqual(sum(count >= 10 for count in containers.values()), 2)
        self.assertGreaterEqual(len({case["strata"]["series_id"] for case in plan["cases"]}), 10)

        faulted = [case for case in plan["cases"] if case["faults"]]
        self.assertEqual(10, len(faulted))
        self.assertEqual(
            set(FAULT_ORDER),
            {fault["scenario"] for case in faulted for fault in case["faults"]},
        )
        for case in plan["cases"]:
            path = Path(case["media"]["canonical_path"])
            self.assertEqual(sha256_file(path), case["completed_delivery"]["source_sha256"])
            self.assertEqual(path.stat().st_mtime_ns, case["media"]["media_mtime_ns"])
            self.assertTrue(case["media"]["policy_revision"])

    def test_unproven_routes_fail_closed_without_a_partial_plan(self) -> None:
        def unresolved(path: Path, _config, _identity) -> str:
            raise CorpusCandidateError("expected_route_unproven", str(path))

        result = prepare_corpus_plan(
            self.config,
            queue_reader=lambda _config: list(self.rows),
            media_probe=self._probe,
            route_resolver=unresolved,
        )
        self.assertFalse(result["ready"])
        self.assertNotIn("plan", result)
        self.assertEqual(100, result["rejections"]["expected_route_unproven"])
        self.assertEqual(20, len(result["unresolved_expected_routes"]))
        self.assertTrue(any(gap["code"] == "candidate_count" for gap in result["gaps"]))

    def test_queue_reader_is_read_only(self) -> None:
        database = self.work / "scanner.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE ai_candidate_queue(path TEXT, mtime_ns INTEGER, status TEXT, source TEXT)"
        )
        row = self.rows[0]
        connection.execute(
            "INSERT INTO ai_candidate_queue VALUES (?,?,?,?)",
            (row.path, row.mtime_ns, row.status, row.source),
        )
        connection.commit()
        connection.close()
        before = sha256_file(database)
        rows = read_queue_candidates(self.config)
        after = sha256_file(database)
        self.assertEqual(before, after)
        self.assertEqual([row], rows)

    def test_explicit_plan_output_refuses_overwrite(self) -> None:
        output = self.root / "acceptance" / "plan.json"
        _write_new_plan(output, "first\n")
        with self.assertRaises(AcceptanceInputError):
            _write_new_plan(output, "second\n")
        self.assertEqual("first\n", output.read_text(encoding="utf-8"))

    def test_disabled_completed_delivery_reports_gap_without_plan(self) -> None:
        disabled = copy(self.config)
        disabled.completed_delivery_enabled = False
        result = prepare_corpus_plan(
            disabled,
            queue_reader=lambda _config: list(self.rows),
            media_probe=self._probe,
            route_resolver=self._route,
        )
        self.assertFalse(result["ready"])
        self.assertNotIn("plan", result)
        self.assertIn("completed_delivery_disabled", {gap["code"] for gap in result["gaps"]})


if __name__ == "__main__":
    unittest.main()
