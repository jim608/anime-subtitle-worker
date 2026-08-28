from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scanner_state_recovery import (
    request_scanner_state_recovery,
    restore_scanner_state_backup,
    verify_scanner_state_backup,
    write_recovery_anchor,
)


SOURCE_DEPLOYMENT_ID = "20260827T090539Z-1500094"
HOLD_DEPLOYMENT_ID = "20260827T100000Z-999"


class ScannerStateRecoveryTest(unittest.TestCase):
    def _backup(self, work: Path) -> Path:
        backup = work / "deployment_backups" / SOURCE_DEPLOYMENT_ID
        database = backup / "databases" / "scanner_state.sqlite3"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE ai_candidate_queue(status TEXT NOT NULL);
                CREATE TABLE ai_job_state(path TEXT PRIMARY KEY);
                CREATE TABLE ai_delivery_obligations(obligation_id TEXT PRIMARY KEY);
                INSERT INTO ai_candidate_queue(status) VALUES('queued'),('queued'),('paused');
                """
            )
            connection.commit()
        finally:
            connection.close()
        digest = hashlib.sha256(database.read_bytes()).hexdigest()
        (backup / "SHA256SUMS").write_text(
            f"{digest}  databases/scanner_state.sqlite3\n",
            encoding="utf-8",
        )
        (backup / "RETENTION_STATUS.json").write_text(
            json.dumps(
                {
                    "deployment_id": SOURCE_DEPLOYMENT_ID,
                    "state": "deployment_completed",
                }
            ),
            encoding="utf-8",
        )
        return database

    def test_verified_backup_restores_only_under_matching_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            self._backup(work)
            live = work / "scanner_state.sqlite3"
            sqlite3.connect(live).close()
            (work / "scanner_state.sqlite3-wal").write_bytes(b"stale")
            (work / "scanner_state.sqlite3-shm").write_bytes(b"stale")
            (work / "deployment_hold.json").write_text(
                json.dumps({"active": True, "deployment_id": HOLD_DEPLOYMENT_ID}),
                encoding="utf-8",
            )

            verified = verify_scanner_state_backup(work, SOURCE_DEPLOYMENT_ID)
            restored = restore_scanner_state_backup(
                work,
                SOURCE_DEPLOYMENT_ID,
                hold_deployment_id=HOLD_DEPLOYMENT_ID,
            )

            self.assertEqual(verified["queue"], {"paused": 1, "queued": 2})
            self.assertEqual(restored["status"], "restored")
            self.assertFalse((work / "scanner_state.sqlite3-wal").exists())
            self.assertFalse((work / "scanner_state.sqlite3-shm").exists())
            connection = sqlite3.connect(live)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_candidate_queue").fetchone()[0], 3)
            finally:
                connection.close()

    def test_tampered_backup_is_rejected_before_live_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            database = self._backup(work)
            database.write_bytes(database.read_bytes() + b"tampered")
            live = work / "scanner_state.sqlite3"
            live.write_bytes(b"keep-me")

            with self.assertRaisesRegex(RuntimeError, "checksum"):
                verify_scanner_state_backup(work, SOURCE_DEPLOYMENT_ID)
            self.assertEqual(live.read_bytes(), b"keep-me")

    def test_verified_anchor_drives_idempotent_fail_closed_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            self._backup(work)

            anchor = write_recovery_anchor(work, SOURCE_DEPLOYMENT_ID)
            first = request_scanner_state_recovery(
                work,
                sqlite3.DatabaseError("database disk image is malformed"),
                operation="refresh_queue",
            )
            second = request_scanner_state_recovery(
                work,
                sqlite3.DatabaseError("database disk image is malformed"),
                operation="queued_candidates",
            )

            self.assertEqual(anchor["status"], "verified")
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "pending")
            self.assertEqual(first["source_deployment_id"], SOURCE_DEPLOYMENT_ID)
            self.assertEqual(first["operation"], "refresh_queue")
            hold = json.loads((work / "deployment_hold.json").read_text(encoding="utf-8"))
            self.assertTrue(hold["active"])
            self.assertEqual(hold["deployment_id"], first["recovery_id"])
            self.assertEqual(hold["reason"], "scanner-state-corruption")


if __name__ == "__main__":
    unittest.main()
