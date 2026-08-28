from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from acceptance.collector import MISSING_SHA256, _ReadonlyLedger, _collect_case, collect_observations
from acceptance.harness import AcceptanceInputError
from output_manifest import delivery_identity, output_manifest_path
from processing_provenance import provenance_path_for_video
from run_unattended_acceptance import _write_new_observations
from safe_files import sha256_file


class AcceptanceCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.config = SimpleNamespace(
            work_path=self.work,
            scanner_state_path="scanner.sqlite3",
            ai_output_manifest_path="manifests",
            processing_provenance_path="provenance",
        )
        self.video = self.root / "episode.mkv"
        self.video.write_bytes(b"source")
        identity = delivery_identity(self.video, self.config)
        self.started = 1_800_000_000.0
        self.manifest = output_manifest_path(self.video, self.config)
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "video": str(self.video),
                    "delivery": {
                        "obligation_id": identity["obligation_id"],
                        "policy_revision": identity["policy_revision"],
                        "verified_at": self.started + 3,
                    },
                    "quality_gate": {"passed": True},
                    "outputs": [{"path": "subtitle.ass"}],
                    "completed_at": self.started + 3,
                }
            ),
            encoding="utf-8",
        )
        provenance = provenance_path_for_video(self.config, self.video)
        provenance.parent.mkdir(parents=True)
        provenance.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "video_path": str(self.video),
                    "config_signature": identity["policy_revision"],
                    "status": "complete",
                    "delivery_route": "existing_zh_tw",
                    "run_started_at": self.started,
                    "finished_at": self.started + 4,
                }
            ),
            encoding="utf-8",
        )
        self.final = self.root / "completed" / "episode.mkv"
        self.final.parent.mkdir()
        self.final.write_bytes(b"final")
        final_stat = self.final.stat()
        self.receipt = self.root / "receipts" / "episode.json"
        self.receipt.parent.mkdir()
        self.receipt.write_text(
            json.dumps(
                {
                    "state": "committed",
                    "source_retained": True,
                    "source": {"sha256": sha256_file(self.video)},
                    "delivery": {
                        "obligation_id": identity["obligation_id"],
                        "policy_revision": identity["policy_revision"],
                    },
                    "publication_manifest": {
                        "path": str(self.manifest.resolve()),
                        "sha256": sha256_file(self.manifest),
                    },
                    "destination": str(self.final),
                    "output": {
                        "path": str(self.final),
                        "size": final_stat.st_size,
                        "mtime_ns": final_stat.st_mtime_ns,
                        "sha256": sha256_file(self.final),
                    },
                    "committed_at": self.started + 3.5,
                }
            ),
            encoding="utf-8",
        )
        self.case = {
            "case_id": "case-001",
            "media": {
                **identity["media"],
                "policy_revision": identity["policy_revision"],
                "obligation_id": identity["obligation_id"],
            },
            "expected_route": "existing_zh_tw",
            "strata": {},
            "completed_delivery": {
                "source_sha256": sha256_file(self.video),
                "receipt_path": str(self.receipt),
                "destination": str(self.final),
            },
            "faults": [],
        }
        self.database = self.work / "scanner.sqlite3"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE ai_delivery_obligations (
              obligation_id TEXT PRIMARY KEY, canonical_path TEXT, media_fingerprint TEXT,
              media_size INTEGER, media_mtime_ns INTEGER, policy_revision TEXT, state TEXT,
              outcome_code TEXT, manifest_path TEXT, manifest_sha256 TEXT,
              verification_json TEXT, eligible_at REAL, due_at REAL, verified_at REAL,
              attempt_count INTEGER
            );
            CREATE TABLE ai_delivery_attempts (
              attempt_id TEXT PRIMARY KEY, obligation_id TEXT, attempt_number INTEGER,
              status TEXT, stage TEXT, error_code TEXT, detail TEXT,
              started_at REAL, finished_at REAL
            );
            """
        )
        media = self.case["media"]
        connection.execute(
            "INSERT INTO ai_delivery_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                media["obligation_id"], media["canonical_path"], media["media_fingerprint"],
                media["media_size"], media["media_mtime_ns"], media["policy_revision"],
                "succeeded", "verified_on_time", str(self.manifest), sha256_file(self.manifest),
                json.dumps({"publication_semantics_verified": True}), self.started,
                self.started + 100, self.started + 4, 1,
            ),
        )
        connection.execute(
            "INSERT INTO ai_delivery_attempts VALUES (?,?,?,?,?,?,?,?,?)",
            ("attempt-1", media["obligation_id"], 1, "succeeded", "complete", "", "",
             self.started + 1, self.started + 4),
        )
        connection.commit()
        connection.close()
        self.plan_file = self.root / "plan.json"
        self.plan_file.write_text("{}", encoding="utf-8")
        self.plan_ref = {
            "kind": "acceptance_plan",
            "path": str(self.plan_file),
            "sha256": sha256_file(self.plan_file),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def collect_case(self, planned: dict | None = None) -> dict:
        with _ReadonlyLedger(self.config) as ledger:
            return _collect_case(
                planned or self.case,
                self.config,
                suite_id="suite",
                plan_schema_version=2,
                plan_reference=self.plan_ref,
                fault_root=self.root / "fault-evidence",
                ledger=ledger,
                collected_at=self.started + 10,
            )

    def test_successful_v2_case_is_hash_bound(self) -> None:
        self.plan_file.write_text(
            json.dumps(
                {
                    "contract": "anime-unattended-acceptance-plan-v1",
                    "schema_version": 2,
                    "suite_id": "suite",
                    "created_at": self.started - 1,
                    "cases": [self.case],
                }
            ),
            encoding="utf-8",
        )
        with patch("acceptance.collector.validate_plan_structure", return_value=[]):
            observations = collect_observations(
                self.plan_file,
                self.config,
                collected_at=self.started + 10,
            )
        observed = observations["cases"][0]
        self.assertEqual("completed", observed["outcome"])
        self.assertEqual([], observed["errors"])
        self.assertEqual(sha256_file(self.plan_file), observations["plan_sha256"])
        self.assertEqual(sha256_file(self.receipt), observed["completed_delivery"]["receipt"]["sha256"])
        self.assertEqual(sha256_file(self.final), observed["completed_delivery"]["final_mkv"]["sha256"])

    def test_missing_case_artifact_is_explicit_failure(self) -> None:
        self.manifest.unlink()
        observed = self.collect_case()
        self.assertEqual("failed", observed["outcome"])
        self.assertIn("output_manifest_missing_or_unreadable", observed["errors"])

    def test_missing_fault_is_not_recovered(self) -> None:
        planned = deepcopy(self.case)
        planned["faults"] = [
            {"fault_id": "fault-1", "scenario": "worker_kill", "trigger": "kill worker"}
        ]
        observed = self.collect_case(planned)
        self.assertEqual("failed", observed["outcome"])
        self.assertEqual("not_recovered", observed["faults"][0]["status"])
        self.assertEqual(MISSING_SHA256, observed["faults"][0]["evidence"][0]["sha256"])

    def test_database_stays_unchanged_and_observations_refuse_overwrite(self) -> None:
        before = (sha256_file(self.database), self.database.stat().st_mtime_ns)
        self.collect_case()
        after = (sha256_file(self.database), self.database.stat().st_mtime_ns)
        self.assertEqual(before, after)
        output = self.root / "observations.json"
        _write_new_observations(output, "first\n")
        with self.assertRaises(AcceptanceInputError):
            _write_new_observations(output, "second\n")
        self.assertEqual("first\n", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
