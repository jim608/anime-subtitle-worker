from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from source_integrity import (
    SourceIntegrityError,
    capture_source_snapshot,
    verify_source_snapshot,
)


class SourceIntegrityTest(unittest.TestCase):
    def test_source_checksum_is_unchanged_after_read_only_pipeline_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Episode.mkv"
            video.write_bytes(b"representative immutable source")
            before = capture_source_snapshot(video, hash_content=True)

            # Representative pipeline work writes only a sibling staged output.
            staged = video.with_name(".Episode.mkv.job.tmp")
            staged.write_bytes(b"subtitle output")

            evidence = verify_source_snapshot(before)
            self.assertTrue(evidence["verified"])
            self.assertEqual(evidence["sha256"], before.sha256)
            self.assertEqual(video.read_bytes(), b"representative immutable source")

    def test_detects_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "Episode.mkv"
            video.write_bytes(b"before")
            before = capture_source_snapshot(video, hash_content=True)
            video.write_bytes(b"after with a different length")

            with self.assertRaises(SourceIntegrityError):
                verify_source_snapshot(before)


if __name__ == "__main__":
    unittest.main()

