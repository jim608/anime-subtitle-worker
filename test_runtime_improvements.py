from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from database_maintenance import optimize_databases
from io_pressure import io_pressure_busy, read_io_pressure
from mikan_worker import MikanExtractJob, _mikan_extract_job_timeout_for
from notifications import notify_event
from qbit_client import QBitTorrent, QBitTorrentFile
from subtitle_extract import SubtitleExtractCancelled, extract_available_subtitles


class RuntimeImprovementTests(unittest.TestCase):
    def test_long_running_command_inbox_starts_before_library_housekeeping(self) -> None:
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        main_body = source[source.index("def main()") : source.index("\ndef _scan_and_process")]

        self.assertIn("if args.watch or args.auto_watch or args.mikan_watch:", main_body)
        self.assertLess(
            main_body.index("_start_background_control_commands(config, logger, shutdown_event)"),
            main_body.index("cleanup_backup_files(config, logger)"),
        )
        self.assertEqual(main_body.count("_start_background_control_commands(config, logger, shutdown_event)"), 1)

    def test_collection_extract_timeout_scales_with_video_count(self) -> None:
        config = SimpleNamespace(
            video_extensions=[".mkv"],
            mikan_extract_job_timeout_seconds=900,
            mikan_extract_job_timeout_per_video_seconds=300,
            mikan_extract_job_timeout_max_seconds=14400,
        )
        torrent = QBitTorrent(
            hash="hash",
            name="Collection",
            progress=1.0,
            state="uploading",
            dlspeed=0,
            downloaded=1,
            added_on=None,
            content_path="/downloads/Collection",
            save_path="/downloads",
            category="llm-sub",
            tags="mikansub",
        )
        job = MikanExtractJob("hash:hash", torrent, [{"episode": value} for value in range(1, 25)])
        files = [QBitTorrentFile(f"Episode {value:02d}.mkv", 1, 1.0, 1) for value in range(1, 25)]

        self.assertEqual(_mikan_extract_job_timeout_for(job, files, config), 7200)

    def test_cancelled_extract_stops_before_ffprobe(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(SubtitleExtractCancelled):
            extract_available_subtitles(
                "/does/not/matter.mkv",
                SimpleNamespace(subtitle_extract_timeout_seconds=300),
                cancel_event=cancel,
            )

    def test_io_pressure_parser_and_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "io"
            path.write_text("some avg10=42.50 avg60=1.0 avg300=0.5 total=1\nfull avg10=2.0 avg60=1.0 avg300=0.5 total=1\n", encoding="utf-8")
            sample = read_io_pressure(path)
        config = SimpleNamespace(
            storage_io_pressure_enabled=True,
            storage_io_pressure_some_avg10_threshold=35.0,
            storage_io_pressure_full_avg10_threshold=10.0,
        )
        self.assertEqual(sample["some"]["avg10"], 42.5)
        self.assertTrue(io_pressure_busy(config, sample))

    def test_notification_is_deduplicated_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                notification_webhook_url="https://notify.invalid/hook",
                notification_events=["ai_failure"],
                notification_min_interval_seconds=300,
                notification_state_path="notifications.json",
            )
            response = Mock()
            response.raise_for_status.return_value = None
            with patch("notifications.requests.post", return_value=response) as post:
                self.assertTrue(notify_event(config, "ai_failure", "title", "message", key="video"))
                self.assertFalse(notify_event(config, "ai_failure", "title", "message", key="video"))
            post.assert_called_once()

    def test_database_maintenance_backs_up_then_compacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                scanner_state_path="scanner_state.sqlite3",
                control_state_path="control_state.sqlite3",
                mikan_pending_path="mikan_pending.json",
                series_metadata_db_path="series_metadata.sqlite3",
            )
            db = root / "scanner_state.sqlite3"
            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY, status TEXT)")
                conn.execute("CREATE TABLE payload(value BLOB)")
                conn.executemany("INSERT INTO payload(value) VALUES (?)", [(b"x" * 4096,)] * 128)
                conn.execute("DELETE FROM payload")
                conn.commit()
            finally:
                conn.close()
            before_size = db.stat().st_size
            with patch("database_maintenance.create_state_backup", return_value={"status": "complete", "path": "backup"}) as backup:
                result = optimize_databases(
                    config,
                    apply=True,
                    min_reclaim_mib=0,
                    min_freelist_ratio=0,
                )
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["optimized"])
            self.assertLess(db.stat().st_size, before_size)
            backup.assert_called_once()

    def test_database_maintenance_does_not_scan_databases_while_runtime_is_busy(self) -> None:
        config = SimpleNamespace(work_path=Path("/work"), mikan_pending_path="mikan_pending.json")
        with (
            patch("database_maintenance._runtime_busy_reasons", return_value=["ai_running:1"]),
            patch("database_maintenance._database_stats") as stats,
        ):
            result = optimize_databases(config, apply=True, wait_seconds=0)

        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["before"], [])
        stats.assert_not_called()

    def test_online_database_maintenance_never_vacuums_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                scanner_state_path="scanner_state.sqlite3",
                control_state_path="control_state.sqlite3",
                mikan_pending_path="mikan_pending.json",
                series_metadata_db_path="series_metadata.sqlite3",
            )
            database = root / "scanner_state.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY, status TEXT)")
                connection.commit()
            finally:
                connection.close()

            with (
                patch("database_maintenance._vacuum_database") as vacuum,
                patch("database_maintenance.create_state_backup") as backup,
            ):
                result = optimize_databases(
                    config,
                    apply=True,
                    online_only=True,
                )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["mode"], "online")
            self.assertEqual([item["before"]["name"] for item in result["optimized"]], ["scanner_state"])
            vacuum.assert_not_called()
            backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
