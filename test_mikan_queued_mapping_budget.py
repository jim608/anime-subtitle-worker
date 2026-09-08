import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mikan_worker import _queued_library_scan_mappings
from scan_state import ScanStateStore


class QueuedMappingBudgetTest(unittest.TestCase):
    def test_real_sqlite_queue_and_files_preserved_across_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(work_path=root, scanner_queue_enabled=True)
            series = root / 'series'; series.mkdir()
            video = series / 'episode.mkv'; video.write_bytes(b'readonly-fixture')
            original = video.stat()
            state = ScanStateStore.from_config(config)
            state.upsert_ai_queue_candidate(video, original.st_mtime_ns)
            state.commit()
            before = state.ai_queue_candidate_snapshot(video)
            state.close()
            mappings = [{'path': str(series)}]
            self.assertEqual(_queued_library_scan_mappings(config, mappings), mappings)
            self.assertEqual(_queued_library_scan_mappings(config, mappings), mappings)
            state = ScanStateStore.from_config(config)
            self.assertEqual(state.ai_queue_candidate_snapshot(video), before)
            state.close()
            self.assertEqual(video.read_bytes(), b'readonly-fixture')
            self.assertEqual(video.stat().st_mtime_ns, original.st_mtime_ns)

    def test_resolve_cost_is_linear_not_queue_times_mappings(self):
        mappings = [{'path': f'/fixture/series{i}', 'bangumi_id': i} for i in range(30)]
        videos = [Path(f'/fixture/series29/episode{i}.mkv') for i in range(40)]
        state = MagicMock()
        state.iter_ai_queue_candidates.return_value = videos
        calls = []
        def resolve(path, *args, **kwargs):
            calls.append(str(path))
            return path
        with patch('mikan_worker.ScanStateStore.from_config', return_value=state), patch.object(Path, 'resolve', resolve):
            selected = _queued_library_scan_mappings(SimpleNamespace(scanner_queue_enabled=True), mappings)
        self.assertTrue(selected)
        self.assertTrue(all(m == mappings[-1] for m in selected))
        self.assertLessEqual(len(calls), len(videos) + len(mappings))
        state.close.assert_called_once()

    def test_expired_budget_does_not_open_queue(self):
        with patch('mikan_worker.ScanStateStore.from_config') as factory, patch('mikan_worker.time.monotonic', return_value=5):
            self.assertEqual(_queued_library_scan_mappings(SimpleNamespace(), [{'path': '/x'}], deadline_monotonic=4), [])
        factory.assert_not_called()

    def test_symlink_escape_is_not_treated_as_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inside = root / 'inside'; outside = root / 'outside'
            inside.mkdir(); outside.mkdir()
            try:
                (inside / 'link').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('symlink unavailable')
            video = inside / 'link' / 'episode.mkv'
            state = MagicMock(); state.iter_ai_queue_candidates.return_value = [video]
            mappings = [{'path': str(inside)}, {'path': str(outside)}]
            with patch('mikan_worker.ScanStateStore.from_config', return_value=state):
                self.assertEqual(_queued_library_scan_mappings(SimpleNamespace(), mappings), [mappings[1]])

    def test_budget_yield_then_restart_does_not_drop_or_mutate_queue(self):
        state = MagicMock()
        videos = [Path('/fixture/a/one.mkv'), Path('/fixture/b/two.mkv')]
        state.iter_ai_queue_candidates.return_value = videos
        mappings = [{'path': '/fixture/a'}, {'path': '/fixture/b'}]
        clock = [0.0]
        def resolve(path, *args, **kwargs):
            clock[0] += 1
            return path
        with patch('mikan_worker.ScanStateStore.from_config', return_value=state), patch.object(Path, 'resolve', resolve), patch('mikan_worker.time.monotonic', side_effect=lambda: clock[0]):
            partial = _queued_library_scan_mappings(SimpleNamespace(), mappings, deadline_monotonic=2.5)
            complete = _queued_library_scan_mappings(SimpleNamespace(), mappings, deadline_monotonic=100)
        self.assertEqual(partial, [mappings[0]])
        self.assertEqual(complete, mappings)
        self.assertEqual(videos, [Path('/fixture/a/one.mkv'), Path('/fixture/b/two.mkv')])
        self.assertEqual(set(name for name, _, _ in state.mock_calls), {'iter_ai_queue_candidates', 'close'})


if __name__ == '__main__':
    unittest.main()
