from __future__ import annotations

from pathlib import Path
import logging
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from event_watcher import _FilesystemEventQueue, _QueueEventHandler, _RunningEventWatcher
from scan_state import ScanStateStore


class _NoopEventHandler:
    def dispatch(self, _event) -> None:
        return None


class FilesystemEventQueueTest(unittest.TestCase):
    def test_handler_ignores_read_only_and_delete_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            event_queue = Mock()
            handler = _QueueEventHandler(
                SimpleNamespace(video_extensions=[".mkv"]),
                logging.getLogger("test_event_watcher"),
                _NoopEventHandler,
                event_queue,
            )

            for event_type in ("opened", "closed_no_write", "deleted"):
                handler.dispatch(
                    SimpleNamespace(
                        event_type=event_type,
                        src_path=str(video),
                        dest_path=None,
                        is_directory=False,
                    )
                )

            event_queue.submit.assert_not_called()

    def test_handler_queues_content_changing_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            event_queue = Mock()
            handler = _QueueEventHandler(
                SimpleNamespace(video_extensions=[".mkv"]),
                logging.getLogger("test_event_watcher"),
                _NoopEventHandler,
                event_queue,
            )

            for event_type in ("created", "modified", "closed"):
                handler.dispatch(
                    SimpleNamespace(
                        event_type=event_type,
                        src_path=str(video),
                        dest_path=None,
                        is_directory=False,
                    )
                )

            self.assertEqual(event_queue.submit.call_count, 3)

    def test_handler_ignores_standalone_op_ed_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            extras = Path(temp_dir) / "Show" / "Extras"
            extras.mkdir(parents=True)
            video = extras / "NCED.mkv"
            video.write_bytes(b"video")
            event_queue = Mock()
            handler = _QueueEventHandler(
                SimpleNamespace(video_extensions=[".mkv"], scanner_skip_standalone_op_ed=True),
                logging.getLogger("test_event_watcher"),
                _NoopEventHandler,
                event_queue,
            )

            handler.dispatch(
                SimpleNamespace(
                    event_type="created",
                    src_path=str(video),
                    dest_path=None,
                    is_directory=False,
                )
            )

            event_queue.submit.assert_not_called()

    def test_debounces_duplicate_video_events_into_one_queue_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            state_path = root / "scanner_state.sqlite3"
            config = SimpleNamespace(
                video_extensions=[".mkv"],
                work_path=root,
                scanner_state_path=str(state_path),
            )

            queue = _FilesystemEventQueue(
                config,
                logging.getLogger("test_event_watcher"),
                debounce_seconds=0.1,
                retry_seconds=1.0,
                stability_interval_seconds=0.1,
            )
            queue.start()
            try:
                # start() is a readiness boundary: callers must never observe
                # a newly created but only partially initialized state DB.
                conn = sqlite3.connect(state_path)
                try:
                    tables = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                finally:
                    conn.close()
                self.assertIn("ai_candidate_queue", tables)

                queue.submit(video)
                queue.submit(video)
                queue.submit(video)
                self._wait_for_queue_row(state_path, video)
            finally:
                queue.stop()
                queue.join(2)

            conn = sqlite3.connect(state_path)
            try:
                rows = conn.execute(
                    """
                    SELECT path, status, source
                    FROM ai_candidate_queue
                    WHERE path = ?
                    """,
                    (str(video.resolve()),),
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(rows, [(str(video.resolve()), "queued", "fs_event")])

    def test_event_requires_two_identical_size_mtime_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E01.mkv"
            video.write_bytes(b"partial")
            config = SimpleNamespace(
                video_extensions=[".mkv"],
                work_path=root,
                scanner_state_path=str(root / "scanner_state.sqlite3"),
            )
            queue = _FilesystemEventQueue(
                config,
                logging.getLogger("test_event_watcher"),
                debounce_seconds=0.1,
                retry_seconds=1.0,
                stability_interval_seconds=0.1,
            )
            state = Mock()
            with patch("event_watcher.ScanStateStore.from_config", return_value=state):
                queue._write_batch([video])
                state.upsert_ai_queue_candidate.assert_not_called()

                video.write_bytes(b"still-growing")
                queue._write_batch([video])
                state.upsert_ai_queue_candidate.assert_not_called()

                queue._write_batch([video])

            state.upsert_ai_queue_candidate.assert_called_once_with(
                video,
                video.stat().st_mtime_ns,
                source="fs_event",
            )

    def test_event_does_not_requeue_done_item_when_ctime_differs_from_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            state_path = root / "scanner_state.sqlite3"
            config = SimpleNamespace(
                video_extensions=[".mkv"],
                work_path=root,
                scanner_state_path=str(state_path),
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.mark_ai_queue_done(video, "Finished AI subtitle detected during scan")
                state.commit()
            finally:
                state.close()

            queue = _FilesystemEventQueue(
                config,
                logging.getLogger("test_event_watcher"),
                debounce_seconds=0.1,
                retry_seconds=1.0,
            )
            queue.start()
            try:
                queue.submit(video)
                time.sleep(0.5)
            finally:
                queue.stop()
                queue.join(2)

            conn = sqlite3.connect(state_path)
            try:
                row = conn.execute(
                    """
                    SELECT status, source
                    FROM ai_candidate_queue
                    WHERE path = ?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(row, ("done", "scan"))

    def test_each_event_batch_releases_its_scanner_state_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            config = SimpleNamespace(
                video_extensions=[".mkv"],
                work_path=root,
                scanner_state_path=str(root / "scanner_state.sqlite3"),
            )
            queue = _FilesystemEventQueue(
                config,
                logging.getLogger("test_event_watcher"),
            )
            first = Mock()
            second = Mock()

            with patch(
                "event_watcher.ScanStateStore.from_config",
                side_effect=[first, second],
            ) as factory:
                queue._write_batch([video])
                queue._write_batch([video])
                queue._write_batch([video])
                queue._write_batch([video])

            self.assertEqual(factory.call_count, 2)
            first.commit.assert_called_once_with()
            second.commit.assert_called_once_with()
            first.close.assert_called_once_with()
            second.close.assert_called_once_with()

    def test_dead_observer_is_restarted_by_supervisor(self) -> None:
        old_observer = Mock()
        old_observer.is_alive.return_value = False
        old_queue = Mock()
        replacement_observer = Mock()
        replacement_observer.is_alive.return_value = True
        replacement_queue = Mock()
        config = SimpleNamespace(scanner_event_watch_health_interval_seconds=3600)
        watcher = _RunningEventWatcher(
            old_observer,
            old_queue,
            config=config,
            logger=logging.getLogger("test_event_watcher.restart"),
            base_handler_type=_NoopEventHandler,
            observer_type=Mock,
        )
        try:
            with patch(
                "event_watcher._create_event_watcher_components",
                return_value=(replacement_observer, replacement_queue),
            ) as factory:
                self.assertTrue(watcher._restart_if_needed())

            factory.assert_called_once()
            old_observer.stop.assert_called_once_with()
            old_queue.stop.assert_called_once_with()
            self.assertTrue(watcher.is_alive())
        finally:
            watcher.stop()
            watcher.join(1)

    def _wait_for_queue_row(self, state_path: Path, video: Path) -> None:
        deadline = time.time() + 3
        while time.time() < deadline:
            if not state_path.exists():
                time.sleep(0.05)
                continue
            conn = sqlite3.connect(state_path)
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()[0]
            finally:
                conn.close()
            if count:
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for filesystem event queue row")


if __name__ == "__main__":
    unittest.main()
