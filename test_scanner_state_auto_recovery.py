from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scanner_state_auto_recovery import run_auto_recovery


SOURCE_DEPLOYMENT_ID = "20260827T090539Z-1500094"
RECOVERY_ID = "20260827T110000Z-777"


class ScannerStateAutoRecoveryTest(unittest.TestCase):
    def _backup(self, work: Path) -> None:
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
                INSERT INTO ai_candidate_queue(status) VALUES('queued'),('paused');
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
            json.dumps({
                "deployment_id": SOURCE_DEPLOYMENT_ID,
                "state": "deployment_completed",
            }),
            encoding="utf-8",
        )

    def test_stops_both_consumers_restores_then_restarts_and_releases_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            self._backup(work)
            (work / "scanner_state.sqlite3").write_bytes(b"corrupt")
            request_path = work / "scanner_state_recovery_required.json"
            request_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "status": "pending",
                    "recovery_id": RECOVERY_ID,
                    "source_deployment_id": SOURCE_DEPLOYMENT_ID,
                    "attempts": 0,
                }),
                encoding="utf-8",
            )
            (work / "deployment_hold.json").write_text(
                json.dumps({
                    "active": True,
                    "deployment_id": RECOVERY_ID,
                    "reason": "scanner-state-corruption",
                }),
                encoding="utf-8",
            )
            states = {"worker": True, "webui": True}
            operations: list[str] = []

            def docker_request(_socket, method: str, path: str, **_kwargs):
                name = "worker" if "/worker/" in path else "webui"
                if method == "GET":
                    return {"State": {"Running": states[name]}}
                if path.endswith("/stop?t=60"):
                    states[name] = False
                    operations.append(f"stop:{name}")
                    return {}
                if path.endswith("/start"):
                    states[name] = True
                    operations.append(f"start:{name}")
                    return {}
                raise AssertionError((method, path))

            with patch(
                "scanner_state_auto_recovery._docker_request",
                side_effect=docker_request,
            ):
                result = run_auto_recovery(
                    work,
                    worker_container="worker",
                    webui_container="webui",
                    docker_socket=work / "docker.sock",
                    request_path=request_path,
                )

            self.assertEqual(
                operations,
                ["stop:worker", "stop:webui", "start:worker", "start:webui"],
            )
            self.assertEqual(result["status"], "completed")
            self.assertFalse((work / "deployment_hold.json").exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["status"], "completed")
            connection = sqlite3.connect(work / "scanner_state.sqlite3")
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM ai_candidate_queue").fetchone()[0],
                    2,
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
