from __future__ import annotations

from pathlib import Path
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from event_watcher import (
    _FilesystemEventQueue,
    _IngestStoreAdapter,
    _QueueEventHandler,
    _RunningEventWatcher,
)
from scan_state import ScanStateStore


class _NoopEventHandler:
    def dispatch(self, _event) -> None:
        return None


class _FakeDurableState:
    def __init__(self) -> None:
        self.observations: dict[str, dict[str, object]] = {}
        self.queue_rows: set[tuple[str, int]] = set()
        self.transitions: list[str] = []
        self.event_types: list[str] = []
        self.commit_calls = 0
        self.close_calls = 0
        self.in_transaction = False

    def upsert_ingest_observation(
        self,
        path: Path,
        size: int,
        mtime_ns: int,
        *,
        observed_at: float | None = None,
        event_type: str = "",
        state: str = "stabilizing",
    ) -> dict[str, object]:
        key = str(path.resolve())
        previous = self.observations.get(key, {})
        same = bool(
            previous
            and int(previous.get("size") or 0) == int(size)
            and int(previous.get("mtime_ns") or 0) == int(mtime_ns)
        )
        row = {
            "path": key,
            "canonical_path": key,
            "size": int(size),
            "mtime_ns": int(mtime_ns),
            "first_seen_at": (
                float(previous.get("first_seen_at") or observed_at or time.time())
                if same
                else float(observed_at or time.time())
            ),
            "last_seen_at": float(observed_at or time.time()),
            "observation_count": int(previous.get("observation_count") or 0) + 1 if same else 1,
            "event_type": event_type,
            "last_event_type": event_type,
            "close_observed": bool(previous.get("close_observed")) or event_type == "closed",
            "state": state,
        }
        self.observations[key] = row
        self.transitions.append(state)
        self.event_types.append(event_type)
        return row

    def iter_pending_ingest_observations(self) -> list[dict[str, object]]:
        return list(self.observations.values())

    def clear_ingest_observation(self, path: Path) -> bool:
        return self.observations.pop(str(path.resolve()), None) is not None

    def upsert_ai_queue_candidate(self, path: Path, mtime_ns: int, *, source: str) -> bool:
        before = len(self.queue_rows)
        self.queue_rows.add((str(path.resolve()), int(mtime_ns)))
        return len(self.queue_rows) != before

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1


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

    def test_handler_ignores_incomplete_names_including_inner_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            incomplete = [
                root / "Episode.part.mkv",
                root / "Episode.partial.mkv",
                root / "Episode.tmp.mkv",
                root / ".Episode.mkv",
            ]
            complete = root / "Movie.Part.1.mkv"
            for path in [*incomplete, complete]:
                path.write_bytes(b"video")
            event_queue = Mock()
            handler = _QueueEventHandler(
                SimpleNamespace(video_extensions=[".mkv"]),
                logging.getLogger("test_event_watcher"),
                _NoopEventHandler,
                event_queue,
            )

            for path in [*incomplete, complete]:
                handler.dispatch(
                    SimpleNamespace(
                        event_type="created",
                        src_path=str(path),
                        dest_path=None,
                        is_directory=False,
                    )
                )

            event_queue.submit.assert_called_once_with(complete, event_type="created")

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
                quiet_window_seconds=0.1,
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

                queue.submit(video, event_type="closed")
                queue.submit(video, event_type="closed")
                queue.submit(video, event_type="closed")
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
                quiet_window_seconds=0,
                file_complete_probe=lambda _path: True,
            )
            state = Mock()
            with patch("event_watcher.ScanStateStore.from_config", return_value=state):
                queue._write_batch([video])
                state.upsert_ai_queue_candidate.assert_not_called()

                previous_mtime = video.stat().st_mtime_ns
                video.write_bytes(b"changed")
                changed_mtime = max(time.time_ns(), previous_mtime + 1_000_000_000)
                os.utime(video, ns=(changed_mtime, changed_mtime))
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
                stability_interval_seconds=0.1,
                quiet_window_seconds=0.1,
            )
            queue.start()
            try:
                queue.submit(video, event_type="closed")
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
                quiet_window_seconds=0,
                file_complete_probe=lambda _path: True,
            )

            opened: list[Mock] = []

            def open_state(_config) -> Mock:
                state = Mock()
                # Prevent Mock's dynamic attributes from masquerading as a
                # durable observation API in this connection-lifetime test.
                state.upsert_ingest_observation = None
                state.observe_ingest = None
                state.iter_pending_ingest_observations = None
                state.list_pending_ingest_observations = None
                state.clear_ingest_observation = None
                state.in_transaction = False
                opened.append(state)
                return state

            with patch(
                "event_watcher.ScanStateStore.from_config",
                side_effect=open_state,
            ) as factory:
                queue._write_batch([video])
                queue._write_batch([video])

            self.assertEqual(factory.call_count, 3)
            self.assertEqual(len(opened), 3)
            for state in opened:
                state.commit.assert_called_once_with()
                state.close.assert_called_once_with()

    def test_quiet_close_write_is_strong_evidence_after_stat_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            now = [1_000.0]
            probe = Mock(side_effect=AssertionError("close-write must not need ffprobe"))
            state = _FakeDurableState()
            queue = _FilesystemEventQueue(
                SimpleNamespace(video_extensions=[".mkv"], work_path=root),
                logging.getLogger("test_event_watcher"),
                quiet_window_seconds=5,
                file_complete_probe=probe,
                wall_clock=lambda: now[0],
                monotonic_clock=lambda: now[0],
            )
            queue.submit(video, event_type="closed")

            with patch("event_watcher.ScanStateStore.from_config", return_value=state):
                queue._write_batch([video])
                now[0] += 1
                queue._write_batch([video])
                probe.assert_not_called()
                self.assertNotIn("queued", state.transitions)

                now[0] += 4
                queue._write_batch([video])

            probe.assert_not_called()
            self.assertEqual(len(state.queue_rows), 1)
            self.assertEqual(
                state.transitions,
                ["stabilizing", "stabilizing", "stabilizing", "queued"],
            )
            self.assertIn("closed", state.event_types)
            self.assertEqual(state.observations, {})

    def test_stat_change_during_probe_keeps_candidate_stabilizing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            state = _FakeDurableState()

            def mutate_during_probe(path: Path) -> bool:
                path.write_bytes(b"video-is-still-growing")
                return True

            queue = _FilesystemEventQueue(
                SimpleNamespace(video_extensions=[".mkv"], work_path=root),
                logging.getLogger("test_event_watcher"),
                quiet_window_seconds=0,
                file_complete_probe=mutate_during_probe,
            )

            with patch("event_watcher.ScanStateStore.from_config", return_value=state):
                queue._write_batch([video])
                queue._write_batch([video])

            self.assertEqual(state.queue_rows, set())
            self.assertNotIn("queued", state.transitions)
            self.assertEqual(state.transitions[-1], "stabilizing")
            self.assertTrue(state.observations)

    def test_restart_rehydrates_pending_observation_and_duplicate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E01.mkv"
            video.write_bytes(b"video")
            config = SimpleNamespace(video_extensions=[".mkv"], work_path=root)
            state = _FakeDurableState()
            first_queue = _FilesystemEventQueue(
                config,
                logging.getLogger("test_event_watcher"),
                quiet_window_seconds=0,
                file_complete_probe=lambda _path: True,
            )
            with patch("event_watcher.ScanStateStore.from_config", return_value=state):
                first_queue._write_batch([video])

            restarted_queue = _FilesystemEventQueue(
                config,
                logging.getLogger("test_event_watcher"),
                quiet_window_seconds=0,
                file_complete_probe=lambda _path: True,
            )
            self.assertEqual(
                restarted_queue._rehydrate_pending_observations(_IngestStoreAdapter(state)),
                1,
            )
            with patch("event_watcher.ScanStateStore.from_config", return_value=state):
                restarted_queue._write_batch([video])
                restarted_queue._write_batch([video])
                restarted_queue._write_batch([video])

            self.assertEqual(len(state.queue_rows), 1)

    def test_no_close_runs_default_ffprobe_only_in_debounce_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E05.mkv"
            video.write_bytes(b"container-bytes")
            state = _FakeDurableState()
            config = SimpleNamespace(
                video_extensions=[".mkv"],
                work_path=root,
                scanner_event_ffprobe_path="custom-ffprobe",
                scanner_event_media_probe_timeout_seconds=7,
            )
            queue = _FilesystemEventQueue(
                config,
                logging.getLogger("test_event_watcher.default_probe"),
                quiet_window_seconds=0,
            )
            completed = SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"format":{"format_name":"matroska"},'
                    '"streams":[{"index":0,"codec_type":"video",'
                    '"nb_read_packets":"12"}]}'
                ),
                stderr="",
            )

            with (
                patch("event_watcher.ScanStateStore.from_config", return_value=state),
                patch("event_watcher.subprocess.run", return_value=completed) as run,
            ):
                queue._write_batch([video])
                run.assert_not_called()
                queue._write_batch([video])

            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual("custom-ffprobe", command[0])
            self.assertIn("-count_packets", command)
            self.assertEqual(7, run.call_args.kwargs["timeout"])
            self.assertEqual(1, len(state.queue_rows))

    def test_long_paused_writer_without_close_stays_stabilizing_when_ffprobe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E06.mkv"
            video.write_bytes(b"stable-size-but-incomplete-container")
            state = _FakeDurableState()
            queue = _FilesystemEventQueue(
                SimpleNamespace(video_extensions=[".mkv"], work_path=root),
                logging.getLogger("test_event_watcher.incomplete_probe"),
                quiet_window_seconds=0,
            )
            rejected = SimpleNamespace(returncode=1, stdout="", stderr="truncated")

            with (
                patch("event_watcher.ScanStateStore.from_config", return_value=state),
                patch("event_watcher.subprocess.run", return_value=rejected) as run,
            ):
                queue._write_batch([video])
                queue._write_batch([video])
                queue._write_batch([video])

            self.assertGreaterEqual(run.call_count, 1)
            self.assertEqual(set(), state.queue_rows)
            self.assertTrue(state.observations)
            self.assertEqual("stabilizing", state.transitions[-1])

    def test_large_media_probe_timeout_is_size_aware_and_capped(self) -> None:
        from event_watcher import _media_probe_timeout_seconds

        config = SimpleNamespace(
            scanner_event_media_probe_timeout_seconds=30,
            scanner_event_media_probe_min_throughput_mib_per_second=10,
            scanner_event_media_probe_max_timeout_seconds=300,
        )
        with patch("pathlib.Path.stat", return_value=SimpleNamespace(st_size=2_000 * 1024 * 1024)):
            self.assertEqual(200, _media_probe_timeout_seconds(Path("large.mkv"), config))
        with patch("pathlib.Path.stat", return_value=SimpleNamespace(st_size=10_000 * 1024 * 1024)):
            self.assertEqual(300, _media_probe_timeout_seconds(Path("huge.mkv"), config))

    def test_probe_failures_back_off_then_quarantine_durably(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E08.mkv"
            video.write_bytes(b"incomplete")
            state = _FakeDurableState()
            queue = _FilesystemEventQueue(
                SimpleNamespace(
                    video_extensions=[".mkv"],
                    work_path=root,
                    scanner_event_media_probe_max_attempts=2,
                    scanner_event_media_probe_max_retry_seconds=60,
                ),
                logging.getLogger("test_event_watcher.bounded_probe"),
                retry_seconds=10,
                quiet_window_seconds=0,
                file_complete_probe=lambda _path: False,
            )
            with patch("event_watcher.ScanStateStore.from_config", return_value=state):
                queue._write_batch([video])
                queue._write_batch([video])
                first_due = queue._pending.pop(video.resolve())
                queue._write_batch([video])

            self.assertGreater(first_due, time.monotonic())
            self.assertFalse(queue._pending)
            row = state.observations[str(video.resolve())]
            self.assertEqual("media_probe_exhausted_attempt_2", row["event_type"])
            self.assertEqual(set(), state.queue_rows)

    def test_event_callback_never_runs_completed_media_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series - S01E07.mkv"
            video.write_bytes(b"video")
            event_queue = Mock()
            handler = _QueueEventHandler(
                SimpleNamespace(video_extensions=[".mkv"]),
                logging.getLogger("test_event_watcher.callback"),
                _NoopEventHandler,
                event_queue,
            )

            with patch("event_watcher._ffprobe_completed_media") as probe:
                handler.dispatch(
                    SimpleNamespace(
                        event_type="moved",
                        src_path=str(video),
                        dest_path=None,
                        is_directory=False,
                    )
                )

            event_queue.submit.assert_called_once_with(video, event_type="moved")
            probe.assert_not_called()

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
