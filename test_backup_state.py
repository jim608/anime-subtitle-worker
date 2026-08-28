from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest

from backup_state import (
    StateBackupError,
    create_state_backup,
    restore_state_backup,
    verify_state_backup,
)


class StateBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text("input_path: /anime\n", encoding="utf-8")
        self.config = SimpleNamespace(
            work_path=self.root,
            config_path=self.config_path,
            state_backup_path="state_backups",
            state_backup_retention_count=2,
            scanner_state_path="scanner_state.sqlite3",
            control_state_path="control_state.sqlite3",
            mikan_pending_path="mikan_pending.json",
            mikan_seen_path="mikan_seen.json",
            series_metadata_db_path="series_metadata.sqlite3",
            language_detect_cache_path="language_detection_cache.json",
            metadata_context_cache_path="metadata_context_cache.json",
            mikan_auto_match_cache_path="mikan_auto_matches.json",
        )
        self._create_scanner_db("before")
        self._create_db(self.root / "mikan_state.sqlite3", "mikan", "ready")
        self._create_db(self.root / "control_state.sqlite3", "control", "ready")
        self._create_db(self.root / "series_metadata.sqlite3", "series", "matched")
        for name in (
            "mikan_pending.json",
            "mikan_seen.json",
            "language_detection_cache.json",
            "metadata_context_cache.json",
            "mikan_auto_matches.json",
        ):
            (self.root / name).write_text(json.dumps({"name": name}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_scanner_db(self, value: str) -> None:
        path = self.root / "scanner_state.sqlite3"
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS state(value TEXT)")
            conn.execute("DELETE FROM state")
            conn.execute("INSERT INTO state VALUES (?)", (value,))
            conn.execute("CREATE TABLE IF NOT EXISTS ai_candidate_queue(path TEXT, status TEXT)")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _create_db(path: Path, table: str, value: str) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute(f"CREATE TABLE {table}(value TEXT)")
            conn.execute(f"INSERT INTO {table} VALUES (?)", (value,))
            conn.commit()
        finally:
            conn.close()

    def test_create_and_verify_consistent_backup(self) -> None:
        result = create_state_backup(self.config)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        verified = verify_state_backup(result["backup"])
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["checked_entries"], 10)
        entries = verified["manifest"]["entries"]
        self.assertTrue(all(item.get("sha256") for item in entries if not item.get("missing")))
        self.assertEqual(
            {item["name"] for item in entries if item.get("kind") == "sqlite"},
            {"scanner_state", "mikan_state", "control_state", "series_metadata"},
        )

    def test_verify_rejects_tampered_file(self) -> None:
        result = create_state_backup(self.config)
        backup = Path(result["backup"])
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        first = next(item for item in manifest["entries"] if item.get("backup"))
        (backup / first["backup"]).write_bytes(b"tampered")
        with self.assertRaises(StateBackupError):
            verify_state_backup(backup)

    def test_restore_is_dry_run_until_apply_and_preserves_current_state(self) -> None:
        result = create_state_backup(self.config)
        self._create_scanner_db("after")
        preview = restore_state_backup(self.config, result["backup"])
        self.assertFalse(preview["apply"])

        applied = restore_state_backup(self.config, result["backup"], apply=True)
        self.assertTrue(applied["apply"])
        self.assertTrue(Path(applied["pre_restore_backup"]).is_dir())
        conn = sqlite3.connect(self.root / "scanner_state.sqlite3")
        try:
            value = conn.execute("SELECT value FROM state").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(value, "before")

    def test_retention_keeps_requested_number(self) -> None:
        paths = [Path(create_state_backup(self.config)["backup"]) for _ in range(4)]
        remaining = [item for item in (self.root / "state_backups").iterdir() if (item / "manifest.json").is_file()]
        self.assertEqual(len(remaining), 2)
        self.assertTrue(paths[-1].exists())


if __name__ == "__main__":
    unittest.main()
