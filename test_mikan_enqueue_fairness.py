from __future__ import annotations

from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from mikan_fallback_sources import FallbackSourcePool
from mikan_source import MikanRelease, MikanSourceDeadline, fetch_bangumi_releases
from mikan_worker import MikanReplacementTarget, MikanWorker, _mikan_state_connect, _mikan_state_db_path_from_config
from test_mikan_worker import _logger, _mikan_enqueue_config


class MikanEnqueueFairnessTest(unittest.TestCase):
    def worker(self, root: Path) -> MikanWorker:
        config = _mikan_enqueue_config(root)
        config.mikan_watch_interval_seconds = 60
        config.mikan_request_timeout_seconds = 60
        config.mikan_bangumi_ids = [101, 102, 103, 104]
        config.mikan_extract_completed = False
        worker = MikanWorker(config, _logger())
        worker._qbit = Mock(return_value=Mock())
        worker._prepare_enqueue_state = Mock(return_value=({"items": {}}, {}, 0, []))
        worker._series_mappings = Mock(return_value=[])
        worker._library_scan_plan = Mock(return_value=([], True))
        worker._repair_terminal_completed_pending_entries = Mock()
        worker._repair_invalid_release_part_pending_entries = Mock()
        worker.consume_completed_state_update_request = Mock()
        worker.consume_deferred_requests = Mock(return_value=None)
        return worker

    def test_elapsed_budget_slices_and_persisted_cursor_resumes_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            now = [0.0]
            calls = []

            def fetch(_base, bangumi_id, *, timeout_seconds, **_kwargs):
                calls.append((bangumi_id, timeout_seconds))
                now[0] += 35
                return []

            stack.enter_context(patch("mikan_worker.time.monotonic", side_effect=lambda: now[0]))
            stack.enter_context(patch("mikan_worker.fetch_bangumi_releases", side_effect=fetch))
            self.worker(root).enqueue_latest_releases(required=False)
            self.assertEqual([item[0] for item in calls], [101, 102])
            self.assertLessEqual(calls[1][1], 25)
            self.worker(root).enqueue_latest_releases(required=False)
            self.assertEqual([item[0] for item in calls], [101, 102, 103, 104])

    def test_recovery_due_during_lookup_runs_before_next_watch_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            worker = self.worker(root)
            now = [1000.0]
            calls = []
            request_path = root / "mikan_replacement_enqueue.request.json"
            request_path.write_text(json.dumps({"next_retry_at": 1005, "targets": [{"bangumi_id": 201, "episode": 1}]}))

            def consume():
                if now[0] >= 1005:
                    calls.append("recovery_claim")
                    request_path.unlink(missing_ok=True)

            def fetch(_base, bangumi_id, **_kwargs):
                calls.append(bangumi_id)
                now[0] += 10
                return []

            worker.consume_replacement_enqueue_request = Mock(side_effect=consume)
            stack.enter_context(patch("mikan_worker.time.time", side_effect=lambda: now[0]))
            stack.enter_context(patch("mikan_worker.fetch_bangumi_releases", side_effect=fetch))
            worker.run_once(process_completed=False)
            self.assertEqual(calls, [101, "recovery_claim"])

    def test_cold_mapping_elapsed_time_is_part_of_discovery_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            worker = self.worker(Path(temp_dir))
            now = [0.0]

            def slow_mapping(**kwargs):
                self.assertEqual(kwargs["deadline_monotonic"], 30.0)
                now[0] = 60.0
                return []

            worker._series_mappings = Mock(side_effect=slow_mapping)
            stack.enter_context(patch("mikan_worker.time.monotonic", side_effect=lambda: now[0]))
            fetch = stack.enter_context(patch("mikan_worker.fetch_bangumi_releases"))
            worker.enqueue_latest_releases(required=False)
            fetch.assert_not_called()
            worker._library_scan_plan.assert_not_called()

    def test_stalled_replacement_shortcut_shares_deadline_and_persists_on_yield(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = self.worker(root)
            targets = [MikanReplacementTarget(101, 1)]
            worker._prepare_enqueue_state = Mock(return_value=({"items": {}}, {}, 0, targets))
            worker._enqueue_replacements_after_extract_failure_unlocked = Mock(side_effect=MikanSourceDeadline("local deadline"))
            with patch("mikan_worker.time.monotonic", return_value=100.0):
                self.assertEqual(worker.enqueue_latest_releases(required=False), 0)
            self.assertEqual(worker._enqueue_replacements_after_extract_failure_unlocked.call_args.kwargs["deadline_monotonic"], 160.0)
            self.assertEqual(json.loads((root / "mikan_replacement_enqueue.request.json").read_text())["targets"], [{"bangumi_id": 101, "episode": 1}])
            worker._series_mappings.assert_not_called()

    def test_slow_cold_mapping_cannot_consume_every_ready_download_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            worker = self.worker(root)
            now = [0.0]
            cold_budgets = []
            downloads = []

            def slow_mapping(**kwargs):
                cold_budgets.append(kwargs["deadline_monotonic"] - now[0])
                now[0] = kwargs["deadline_monotonic"]
                return [{"bangumi_id": 101, "path": str(root), "title": "Verified Show"}]

            def fetch(_base, bangumi_id, **kwargs):
                downloads.append((bangumi_id, now[0]))
                now[0] += 10
                return []

            worker._series_mappings = Mock(side_effect=slow_mapping)
            stack.enter_context(patch("mikan_worker.time.monotonic", side_effect=lambda: now[0]))
            stack.enter_context(patch("mikan_worker.fetch_bangumi_releases", side_effect=fetch))
            worker.enqueue_latest_releases(required=False)
            self.assertTrue(downloads, "Ready downloads were starved by the first cold lookup slice")
            first_count = len(downloads)
            worker.enqueue_latest_releases(required=False)
            self.assertGreater(len(downloads), first_count, "Repeated slow cold lookups starved ready downloads")
            self.assertEqual(cold_budgets, [30.0, 30.0])

    def test_interrupted_series_retries_same_cursor_without_repeating_finished_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            calls = []

            def interrupted(_base, bangumi_id, **_kwargs):
                calls.append(bangumi_id)
                if bangumi_id == 102:
                    raise RuntimeError("isolated process interruption")
                return []

            with patch("mikan_worker.fetch_bangumi_releases", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "process interruption"):
                    self.worker(root).enqueue_latest_releases(required=False)
            with patch("mikan_worker.fetch_bangumi_releases", side_effect=lambda _base, bid, **kw: calls.append(bid) or []):
                self.worker(root).enqueue_latest_releases(required=False)
            self.assertEqual(calls, [101, 102, 102, 103, 104, 101])

    def test_source_lookup_does_not_hold_sqlite_write_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = self.worker(root)
            worker.config.mikan_bangumi_ids = [101]
            worker._prepare_enqueue_state = MikanWorker._prepare_enqueue_state.__get__(worker)
            worker._qbit.return_value.list_torrents.return_value = []
            database = _mikan_state_db_path_from_config(worker.config)
            with closing(_mikan_state_connect(worker.config)) as connection:
                connection.execute("CREATE TABLE isolated_probe (id INTEGER)")
                connection.commit()

            def fetch(*_args, **_kwargs):
                with closing(sqlite3.connect(database, timeout=0)) as other:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("INSERT INTO isolated_probe VALUES (1)")
                    other.commit()
                return []

            with patch("mikan_worker.fetch_bangumi_releases", side_effect=fetch):
                worker.enqueue_latest_releases(required=False)
                worker._reconcile_verified_history_outputs = Mock(return_value=True)
                worker.request_replacement_enqueue([MikanReplacementTarget(101, 1)])
                worker.consume_replacement_enqueue_request()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM isolated_probe").fetchone()[0], 2)

    def test_restart_preserves_existing_idempotency_and_original_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = root / "original.mkv"
            subtitle = root / "original.zh-TW.ass"
            original.write_bytes(b"untouched source fixture")
            subtitle.write_bytes(b"untouched valid-output fixture")
            qbit = Mock()
            qbit.list_torrents.return_value = []
            release = MikanRelease(
                bangumi_id=101, title="[Group] Test Show - 01 [CHT][MKV]", episode=1,
                torrent_url="https://mikan/unique.torrent", pub_date=None, content_length=100,
            )

            def fetch(_base, bangumi_id, **_kwargs):
                if bangumi_id == 102:
                    raise RuntimeError("isolated interruption after committed enqueue")
                return [release] if bangumi_id == 101 else []

            first = self.worker(root)
            first._qbit = Mock(return_value=qbit)
            with patch("mikan_worker.fetch_bangumi_releases", side_effect=fetch):
                with self.assertRaisesRegex(RuntimeError, "isolated interruption"):
                    first.enqueue_latest_releases(required=False)
            resumed = self.worker(root)
            resumed._qbit = Mock(return_value=qbit)
            with patch("mikan_worker.fetch_bangumi_releases", side_effect=lambda _base, bid, **kw: [release] if bid == 101 else []):
                resumed.enqueue_latest_releases(required=False)
            qbit.add_url.assert_called_once()
            self.assertEqual(original.read_bytes(), b"untouched source fixture")
            self.assertEqual(subtitle.read_bytes(), b"untouched valid-output fixture")

    def test_busy_due_recovery_does_not_starve_all_normal_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = self.worker(root)
            worker.consume_replacement_enqueue_request = Mock(return_value={"deferred": True})
            (root / "mikan_replacement_enqueue.request.json").write_text(json.dumps({
                "next_retry_at": 0, "targets": [{"bangumi_id": 201, "episode": 1}],
            }))
            with patch("mikan_worker.fetch_bangumi_releases", return_value=[]) as fetch:
                worker.run_once(process_completed=False)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(worker.consume_replacement_enqueue_request.call_count, 2)

    def test_local_budget_does_not_become_provider_failure_or_negative_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_fallback_sources_enabled = True
            config.mikan_fallback_sources = ["dmhy"]
            pool = FallbackSourcePool(config, _logger())
            pool.begin_cycle(deadline_monotonic=105.0)
            now = [100.0]

            def timeout(*_args, **kwargs):
                self.assertEqual(kwargs["timeout"], 5.0)
                now[0] = 105.0
                raise requests.Timeout("bounded local request")

            pool.session.get = Mock(side_effect=timeout)
            with patch("mikan_fallback_sources.time.monotonic", side_effect=lambda: now[0]), patch(
                "mikan_fallback_sources._record_provider_failure"
            ) as failure:
                result = pool.search(101, [{"title": "Test Show"}], {1})
            self.assertFalse(result.conclusive)
            self.assertEqual(result.deferred_reason, "elapsed_budget_exhausted")
            failure.assert_not_called()
            self.assertEqual(pool._cache["entries"], {})

    def test_expired_fallback_cycle_does_not_start_network_and_next_cycle_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _mikan_enqueue_config(Path(temp_dir))
            config.mikan_fallback_sources_enabled = True
            config.mikan_fallback_sources = ["dmhy"]
            pool = FallbackSourcePool(config, _logger())
            pool._fetch_source = Mock(return_value=[])
            pool.begin_cycle(deadline_monotonic=100.0)
            with patch("mikan_fallback_sources.time.monotonic", return_value=100.0):
                result = pool.search(101, [{"title": "Test Show"}], {1})
            self.assertEqual(result.deferred_reason, "elapsed_budget_exhausted")
            pool._fetch_source.assert_not_called()
            pool.begin_cycle()
            self.assertTrue(pool.search(101, [{"title": "Test Show"}], {1}).conclusive)
            pool._fetch_source.assert_called_once()

    def test_rss_retry_timeout_and_sleep_share_one_absolute_budget(self) -> None:
        now = [0.0]
        timeouts = []

        def timeout(*_args, **kwargs):
            timeouts.append(kwargs["timeout"])
            now[0] += kwargs["timeout"]
            raise requests.Timeout("isolated slow RSS")

        with patch("mikan_source.time.monotonic", side_effect=lambda: now[0]), patch(
            "mikan_source.time.sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)
        ), patch("mikan_source.requests.get", side_effect=timeout):
            with self.assertRaises(MikanSourceDeadline):
                fetch_bangumi_releases("https://mikan", 101, timeout_seconds=30, max_attempts=3, deadline_monotonic=45)
        self.assertEqual(timeouts, [30, 14.75])
        self.assertEqual(now[0], 45)

    def test_rss_deadline_keeps_cursor_without_marking_negative_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = self.worker(root)
            worker._mark_no_candidate_retry_with_state_lock = Mock()
            worker._fallback_sources = Mock()
            with patch("mikan_worker.fetch_bangumi_releases", side_effect=MikanSourceDeadline("local budget")):
                self.assertEqual(worker.enqueue_latest_releases(required=False), 0)
            worker._mark_no_candidate_retry_with_state_lock.assert_not_called()
            worker._fallback_sources.search.assert_not_called()
            self.assertEqual(json.loads((root / "mikan_enqueue.cursor.json").read_text())["next_bangumi_id"], 101)

    def test_replacement_deadline_preserves_request_and_rotates_without_failure_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = self.worker(root)
            worker._reconcile_verified_history_outputs = Mock(return_value=True)
            worker._mark_no_candidate_retry_with_state_lock = Mock()
            worker.request_replacement_enqueue([MikanReplacementTarget(101, 1), MikanReplacementTarget(201, 1)])
            path = root / "mikan_replacement_enqueue.request.json"
            payload = json.loads(path.read_text())
            payload["retry_attempts"] = 2
            path.write_text(json.dumps(payload))
            with patch("mikan_worker.fetch_bangumi_releases", side_effect=MikanSourceDeadline("local budget")):
                result = worker.consume_replacement_enqueue_request()
            self.assertEqual(result["yield_reason"], "elapsed_budget_exhausted")
            saved = json.loads(path.read_text())
            self.assertEqual(saved["retry_attempts"], 2)
            self.assertEqual(saved["targets"], [{"bangumi_id": 201, "episode": 1}, {"bangumi_id": 101, "episode": 1}])
            worker._mark_no_candidate_retry_with_state_lock.assert_not_called()

    def test_replacement_large_series_uses_existing_item_limit_then_yields_to_other_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = self.worker(root)
            worker.config.mikan_max_items_per_bangumi = 2
            worker._enqueue_replacements_after_extract_failure_unlocked = Mock(return_value=0)
            worker.request_replacement_enqueue([
                MikanReplacementTarget(101, 1), MikanReplacementTarget(101, 2),
                MikanReplacementTarget(101, 3), MikanReplacementTarget(201, 1),
            ])
            with patch("mikan_worker.time.time", return_value=1000):
                worker.consume_replacement_enqueue_request()
            self.assertEqual(worker._enqueue_replacements_after_extract_failure_unlocked.call_args.args[0], [
                MikanReplacementTarget(101, 1), MikanReplacementTarget(101, 2),
            ])
            with patch("mikan_worker.time.time", return_value=1061):
                worker.consume_replacement_enqueue_request()
            self.assertEqual(worker._enqueue_replacements_after_extract_failure_unlocked.call_args.args[0], [MikanReplacementTarget(201, 1)])


if __name__ == "__main__":
    unittest.main()
