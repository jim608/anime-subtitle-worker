"""Real ffmpeg extraction/import on generated isolated media only."""
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mikan_worker import MikanWorker
from qbit_client import QBitTorrent
from subtitle_extract import extract_available_subtitles, SubtitleExtractCancelled
from test_mikan_import_validation import dialogue_ass
from test_mikan_worker import _mikan_process_config, _logger


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe unavailable")
class DownloadRuntimeTest(unittest.TestCase):
    def test_real_embedded_import_no_subtitles_interrupt_restart(self):
        with tempfile.TemporaryDirectory(prefix="m2-download-fi-") as tmp:
            root = Path(tmp)
            downloads = root / "downloads"
            downloads.mkdir()
            library = root / "library"
            library.mkdir()
            target = library / "Show - S01E01.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=16x16:r=1:d=100", "-c:v", "ffv1", str(target)], check=True, timeout=60)
            source = downloads / "Show - 01.mkv"
            ass = downloads / "fixture.ass"
            ass.write_text(dialogue_ass(), encoding="utf-8")
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(target), "-i", str(ass), "-map", "0", "-map", "1", "-c", "copy", "-metadata:s:s:0", "language=zho", str(source)], check=True, timeout=60)
            hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, target)}
            config = _mikan_process_config(root, downloads)
            worker = MikanWorker(config, _logger())
            torrent = QBitTorrent(hash="f" * 40, name="[Fixture] Show - 01 [CHT]", progress=1.0,
                state="uploading", dlspeed=0, downloaded=source.stat().st_size, added_on=None,
                content_path=str(source), save_path=str(downloads), category="llm-sub", tags="mikansub")
            # An interrupted extraction cannot retire or publish any artifact.
            with patch("subtitle_extract._extract_subtitle_stream", side_effect=SubtitleExtractCancelled("fixture interruption")):
                result = worker._extract_completed_source_to_target(source, target, torrent, [], downloads)
            self.assertEqual(result.failure_reason, "extract_cancelled")
            self.assertFalse(target.with_suffix(".zh-TW.ass").exists())
            # A fresh worker reuses the same downloaded container.
            restarted = MikanWorker(config, _logger())
            result = restarted._extract_completed_source_to_target(source, target, torrent, [], downloads)
            self.assertEqual(result.extracted_count, 1)
            self.assertTrue(any(row.get("output_parse") == "PASS" for row in result.subtitle_diagnostics))
            self.assertTrue(target.with_suffix(".zh-TW.ass").is_file())
            # No embedded subtitle is a bounded input classification, not a crash.
            diagnostics = []
            self.assertEqual(extract_available_subtitles(target, config, diagnostics=diagnostics, validate_for_import=True), [])
            for path, expected in hashes.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)


if __name__ == "__main__":
    unittest.main()
