from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from work_cleanup import cleanup_work_artifacts


class WorkCleanupTest(unittest.TestCase):
    def test_cleanup_only_removes_stale_generated_audio_and_old_corrupt_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            now = 2_000_000.0
            stale_audio = root / "Anime S01E01.0123456789ab.wav"
            recent_audio = root / "Anime S01E02.abcdef012345.vocals.wav"
            unrelated_audio = root / "recording.wav"
            for path in (stale_audio, recent_audio, unrelated_audio):
                path.write_bytes(b"1234")
            os.utime(stale_audio, (now - 100_000, now - 100_000))
            os.utime(recent_audio, (now - 60, now - 60))
            os.utime(unrelated_audio, (now - 100_000, now - 100_000))

            backups = []
            for index in range(4):
                path = root / f"scanner_state.sqlite3.corrupt-{index}"
                path.write_bytes(str(index).encode("ascii"))
                os.utime(path, (now - index, now - index))
                backups.append(path)

            config = SimpleNamespace(work_path=root)
            summary = cleanup_work_artifacts(config, _logger(), apply=True, now=now)

            self.assertEqual(summary.removed_audio, 1)
            self.assertEqual(summary.removed_corrupt_backups, 2)
            self.assertFalse(stale_audio.exists())
            self.assertTrue(recent_audio.exists())
            self.assertTrue(unrelated_audio.exists())
            self.assertTrue(backups[0].exists())
            self.assertTrue(backups[1].exists())
            self.assertFalse(backups[2].exists())
            self.assertFalse(backups[3].exists())

    def test_dry_run_reports_candidates_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "Anime S01E01.0123456789ab.wav"
            path.write_bytes(b"1234")
            os.utime(path, (1.0, 1.0))

            summary = cleanup_work_artifacts(
                SimpleNamespace(work_path=root),
                _logger(),
                apply=False,
                now=200_000.0,
            )

            self.assertEqual(summary.stale_audio, 1)
            self.assertEqual(summary.removed_audio, 0)
            self.assertEqual(summary.candidate_bytes, 4)
            self.assertTrue(path.exists())


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.work_cleanup")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
