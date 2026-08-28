from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from series_metadata import SeriesMetadataStore, SeriesProfile
from series_metadata_sync import sync_series_metadata


class SeriesMetadataSyncTests(unittest.TestCase):
    def test_sync_enriches_only_configured_batch_and_advances_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir)
            config = SimpleNamespace(
                work_path=work,
                scanner_state_path="scanner_state.sqlite3",
                series_metadata_db_path="series_metadata.sqlite3",
                mikan_pending_path="mikan_pending.json",
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_series_path_mappings=[],
                series_metadata_enrich_enabled=True,
                series_metadata_enrich_per_cycle=2,
                series_metadata_enrich_delay_seconds=0,
                translation_metadata_context_enabled=True,
            )
            conn = sqlite3.connect(work / "scanner_state.sqlite3")
            try:
                conn.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY)")
                conn.executemany(
                    "INSERT INTO ai_candidate_queue(path) VALUES (?)",
                    [(f"/anime/Show {index}/Season 1/Show {index} - S01E01.mkv",) for index in range(3)],
                )
                conn.commit()
            finally:
                conn.close()

            with patch(
                "series_metadata_sync.build_series_metadata_context",
                return_value=SimpleNamespace(provider="anilist"),
            ) as enrich:
                summary = sync_series_metadata(config)

            self.assertEqual(summary["enrichment"]["attempted"], 2)
            self.assertEqual(summary["enrichment"]["enriched"], 2)
            self.assertEqual(enrich.call_count, 2)
            with SeriesMetadataStore.from_config(config) as store:
                self.assertTrue(store.get_meta("enrichment_cursor"))

    def test_sync_seeds_cache_episode_index_scanner_and_manual_profiles_without_media_walk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work = root / "work"
            work.mkdir()
            config = SimpleNamespace(
                work_path=work,
                scanner_state_path="scanner_state.sqlite3",
                series_metadata_db_path="series_metadata.sqlite3",
                mikan_pending_path="mikan_pending.json",
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_series_path_mappings=[
                    {"bangumi_id": 303, "path": "/anime/Manual Show", "title": "Manual Show", "match": ["Manual"]}
                ],
            )

            conn = sqlite3.connect(work / "scanner_state.sqlite3")
            try:
                conn.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY)")
                conn.executemany(
                    "INSERT INTO ai_candidate_queue(path) VALUES (?)",
                    [
                        ("/anime/Cached Show/Season 1/Cached Show - S01E01.mkv",),
                        ("/anime/Local Only/Season 2/Local Only - S02E01.mkv",),
                        ("/anime/Rich Show/Season 1/Rich Show - S01E01.mkv",),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            conn = sqlite3.connect(work / "mikan_state.sqlite3")
            try:
                conn.execute(
                    "CREATE TABLE anime_episode_index(bangumi_id INTEGER, series_path TEXT)"
                )
                conn.execute(
                    "INSERT INTO anime_episode_index VALUES (?, ?)",
                    (101, "/anime/Cached Show"),
                )
                conn.commit()
            finally:
                conn.close()

            (work / "mikan_auto_matches.json").write_text(
                json.dumps(
                    {
                        "/anime/Cached Show": {
                            "status": "matched",
                            "confidence": 0.97,
                            "title": "Cached Official Title",
                            "mapping": {
                                "bangumi_id": 101,
                                "path": "/anime/Cached Show",
                                "match": ["Cached Show", "キャッシュ"],
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with SeriesMetadataStore.from_config(config) as store:
                store.upsert_profile(
                    SeriesProfile(
                        local_path="/anime/Rich Show",
                        canonical_title="AniList Rich Show",
                        provider="anilist",
                        provider_id="999",
                        synopsis="Keep this synopsis",
                        match_confidence=0.99,
                    )
                )

            summary = sync_series_metadata(config)

            self.assertEqual(summary["candidates"], 4)
            with SeriesMetadataStore.from_config(config) as store:
                self.assertEqual(store.count_profiles(), 4)
                cached = store.get_by_local_path("/anime/Cached Show")
                self.assertEqual(cached.canonical_title, "Cached Official Title")
                self.assertEqual(cached.mikan_bangumi_id, 101)
                self.assertIn("キャッシュ", cached.aliases)
                local = store.get_by_local_path("/anime/Local Only")
                self.assertEqual(local.provider, "local")
                manual = store.get_by_local_path("/anime/Manual Show")
                self.assertEqual(manual.mikan_bangumi_id, 303)
                rich = store.get_by_local_path("/anime/Rich Show")
                self.assertEqual(rich.provider, "anilist")
                self.assertEqual(rich.synopsis, "Keep this synopsis")
                self.assertIsNotNone(store.get_meta("last_index_sync_summary"))


if __name__ == "__main__":
    unittest.main()
