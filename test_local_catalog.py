from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from local_catalog import discover_local_series


class LocalCatalogTest(unittest.TestCase):
    def test_discovers_series_root_and_aliases_from_nfo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input"
            series_root = input_path / "MARRIAGETOXIN"
            season_path = series_root / "Season 1"
            season_path.mkdir(parents=True)
            (series_root / "tvshow.nfo").write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<tvshow>
  <title>MARRIAGETOXIN / マリッジトキシン</title>
  <originaltitle>Marriage Toxin</originaltitle>
  <premiered>2026-05-01</premiered>
  <anidbid>12345</anidbid>
</tvshow>
""",
                encoding="utf-8",
            )
            (season_path / "MARRIAGETOXIN - S01E01 - WEBDL-1080p.mkv").write_text("", encoding="utf-8")

            config = SimpleNamespace(input_path=input_path, video_extensions=[".mkv", ".mp4"])
            series = discover_local_series(config)

            self.assertEqual(len(series), 1)
            self.assertEqual(series[0].path, series_root)
            self.assertIn("MARRIAGETOXIN", series[0].aliases)
            self.assertIn("マリッジトキシン", series[0].aliases)
            self.assertIn("Marriage Toxin", series[0].aliases)
            self.assertEqual(series[0].premiered_year, 2026)
            self.assertEqual(series[0].anidb_id, "12345")


if __name__ == "__main__":
    unittest.main()
