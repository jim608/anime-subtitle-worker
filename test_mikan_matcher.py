from __future__ import annotations

import logging
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mikan_matcher import (
    MikanSearchCandidate,
    _auto_match_series,
    _candidate_confidence,
    _series_metadata_mappings,
    mapping_matches_torrent,
    parse_mikan_search_results,
    resolve_mikan_series_mappings,
)
from mikan_source import MikanRelease
from local_catalog import LocalSeries
from series_metadata import SeriesProfile


class MikanMatcherTest(unittest.TestCase):
    def test_parse_search_results_accepts_relative_and_absolute_links(self) -> None:
        html = """
<a href="/Home/Bangumi/3921">MARRIAGETOXIN</a>
<a href='https://mikanani.me/Home/Bangumi/3914'><span>Class de 2-banme</span></a>
"""
        results = parse_mikan_search_results(html, "test")

        self.assertEqual([result.bangumi_id for result in results], [3921, 3914])
        self.assertEqual(results[0].title, "MARRIAGETOXIN")
        self.assertEqual(results[1].title, "Class de 2-banme")

    def test_mapping_matches_torrent_uses_normalized_tokens(self) -> None:
        mapping = {
            "bangumi_id": 3921,
            "path": "/example/input/MARRIAGETOXIN",
            "match": [
                "Marriage Toxin",
                "マリッジトキシン",
                "\u6211\u548c\u73ed\u4e0a\u7b2c\u4e8c\u53ef\u611b\u7684\u5973\u751f\u6210\u70ba\u670b\u53cb",
            ],
        }

        self.assertTrue(mapping_matches_torrent("[LoliHouse] Marriage Toxin - 01 [WebRip].mkv", mapping))
        self.assertTrue(mapping_matches_torrent("[Group] マリッジトキシン - 01.mkv", mapping))
        self.assertTrue(
            mapping_matches_torrent(
                "[Group] \u6211\u548c\u73ed\u4e0a\u7b2c\u4e8c\u53ef\u7231\u7684\u5973\u751f\u6210\u4e3a\u670b\u53cb - 01.mkv",
                mapping,
            )
        )
        self.assertFalse(mapping_matches_torrent("[Group] Other Anime - 01.mkv", mapping))

    def test_mapping_rejects_episode_range_and_season_noise_as_series_identity(self) -> None:
        mapping = {
            "bangumi_id": 260,
            "path": "/anime/No-Rin",
            "match": ["01~12", "2014冬", "No-Rin", "農林"],
        }

        self.assertFalse(
            mapping_matches_torrent(
                "[LoliHouse] Bofuri 2 [01-12][WebRip 1080p HEVC-10bit AAC SRTx2]",
                mapping,
            )
        )
        self.assertTrue(mapping_matches_torrent("[Group] No-Rin - 01 [BDRip]", mapping))
        self.assertTrue(mapping_matches_torrent("[Group] 農林 - 01 [BDRip]", mapping))

    def test_mapping_rejects_numeric_only_title_token(self) -> None:
        self.assertFalse(
            mapping_matches_torrent(
                "[Group] Random Collection 86 [01-12]",
                {"bangumi_id": 86, "path": "/anime/86", "match": ["86"]},
            )
        )
        self.assertTrue(
            mapping_matches_torrent(
                "[Group] 86 - Eighty Six - 01",
                {"bangumi_id": 86, "path": "/anime/86", "match": ["86", "86 - Eighty Six"]},
            )
        )

    def test_resolve_auto_match_discovers_local_series_and_writes_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "input"
            work_path = temp_path / "work"
            series_root = input_path / "MARRIAGETOXIN"
            (series_root / "Season 1").mkdir(parents=True)
            (series_root / "tvshow.nfo").write_text(
                "<tvshow><title>MARRIAGETOXIN</title><originaltitle>Marriage Toxin</originaltitle></tvshow>",
                encoding="utf-8",
            )
            (series_root / "Season 1" / "MARRIAGETOXIN - S01E01 - WEBDL-1080p.mkv").write_text(
                "",
                encoding="utf-8",
            )

            config = SimpleNamespace(
                input_path=input_path,
                work_path=work_path,
                video_extensions=[".mkv", ".mp4"],
                mikan_series_path_mappings=[],
                mikan_auto_match_enabled=True,
                mikan_auto_match_threshold=0.86,
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_auto_match_max_candidates=6,
                mikan_base_url="https://mikanani.me",
                mikan_request_timeout_seconds=30,
            )
            release = MikanRelease(
                bangumi_id=3921,
                title="[LoliHouse] MARRIAGETOXIN - 01 [WebRip 1080p][CHT].mkv",
                episode=1,
                torrent_url="https://mikanani.me/Download/test.torrent",
                pub_date=None,
                content_length=100,
            )

            with (
                patch(
                    "mikan_matcher.search_mikan_bangumi",
                    return_value=[MikanSearchCandidate(3921, "MARRIAGETOXIN", "MARRIAGETOXIN")],
                ),
                patch("mikan_matcher.fetch_bangumi_releases", return_value=[release]),
            ):
                mappings = resolve_mikan_series_mappings(config, logging.getLogger("test"))

            self.assertEqual(len(mappings), 1)
            self.assertEqual(mappings[0]["bangumi_id"], 3921)
            self.assertEqual(Path(str(mappings[0]["path"])), series_root)
            self.assertTrue((work_path / "mikan_auto_matches.json").exists())

    def test_resolve_auto_match_caches_no_candidate_misses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "input"
            work_path = temp_path / "work"
            series_root = input_path / "Unknown Series"
            (series_root / "Season 1").mkdir(parents=True)
            (series_root / "Season 1" / "Unknown Series - S01E01.mkv").write_text("", encoding="utf-8")

            config = SimpleNamespace(
                input_path=input_path,
                work_path=work_path,
                video_extensions=[".mkv"],
                mikan_series_path_mappings=[],
                mikan_auto_match_enabled=True,
                mikan_auto_match_threshold=0.86,
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_auto_match_max_candidates=6,
                mikan_base_url="https://mikanani.me",
                mikan_request_timeout_seconds=30,
            )

            with patch("mikan_matcher.search_mikan_bangumi", return_value=[]) as search:
                first = resolve_mikan_series_mappings(config, logging.getLogger("test"))
                first_call_count = search.call_count
                second = resolve_mikan_series_mappings(config, logging.getLogger("test"))

            self.assertEqual(first, [])
            self.assertEqual(second, [])
            self.assertGreater(first_call_count, 0)
            self.assertEqual(search.call_count, first_call_count)

    def test_resolve_cached_only_uses_cache_without_discovering_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            work_path = temp_path / "work"
            series_root = temp_path / "input" / "Cached Series"
            series_root.mkdir(parents=True)
            cache_path = work_path / "mikan_auto_matches.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        str(series_root.resolve()): {
                            "status": "matched",
                            "matcher_version": 2,
                            "confidence": 0.99,
                            "mapping": {
                                "bangumi_id": 1234,
                                "path": str(series_root),
                                "match": ["Cached Series"],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                input_path=temp_path / "input",
                work_path=work_path,
                video_extensions=[".mkv"],
                mikan_series_path_mappings=[],
                mikan_auto_match_enabled=True,
                mikan_auto_match_threshold=0.86,
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_auto_match_max_candidates=6,
                mikan_base_url="https://mikanani.me",
                mikan_request_timeout_seconds=30,
            )

            with patch("mikan_matcher.discover_local_series", side_effect=AssertionError("should not scan")):
                mappings = resolve_mikan_series_mappings(config, logging.getLogger("test"), cached_only=True)

            self.assertEqual(len(mappings), 1)
            self.assertEqual(mappings[0]["bangumi_id"], 1234)
            self.assertEqual(Path(str(mappings[0]["path"])), series_root)

    def test_resolve_auto_match_restores_title_from_older_cache_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "input"
            work_path = temp_path / "work"
            series_root = input_path / "Rurouni Kenshin (2023)"
            (series_root / "Season 2").mkdir(parents=True)
            (series_root / "Season 2" / "Rurouni Kenshin (2023) - S02E03.mkv").write_text("", encoding="utf-8")
            cache_path = work_path / "mikan_auto_matches.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        str(series_root.resolve()): {
                            "status": "matched",
                            "matcher_version": 2,
                            "confidence": 0.99,
                            "title": "Rurouni Kenshin Kyoto Douran",
                            "mapping": {
                                "bangumi_id": 3467,
                                "path": str(series_root),
                                "match": ["Rurouni Kenshin (2023)", "Kyoto Douran"],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                input_path=input_path,
                work_path=work_path,
                video_extensions=[".mkv"],
                mikan_series_path_mappings=[],
                mikan_auto_match_enabled=True,
                mikan_auto_match_threshold=0.86,
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_auto_match_max_candidates=6,
                mikan_base_url="https://mikanani.me",
                mikan_request_timeout_seconds=30,
            )

            mappings = resolve_mikan_series_mappings(config, logging.getLogger("test"))

            self.assertEqual(len(mappings), 1)
            self.assertEqual(mappings[0]["title"], "Rurouni Kenshin Kyoto Douran")

    def test_candidate_confidence_rejects_franchise_only_substring(self) -> None:
        series = LocalSeries(
            Path("/anime/BanG Dream!"),
            ["BanG Dream!"],
            2017,
            None,
        )
        for bangumi_id, title in (
            (2218, "ARGONAVIS from BanG Dream! ANIMATION"),
            (3518, "BanG Dream! Ave Mujica"),
        ):
            with self.subTest(title=title):
                candidate = MikanSearchCandidate(bangumi_id, title, "BanG Dream!")
                confidence = _candidate_confidence(
                    series,
                    candidate,
                    [f"[Group] {title} - 01 [WebRip]"],
                )
                self.assertLess(confidence, 0.86)

    def test_auto_match_requires_unique_runner_up_margin(self) -> None:
        series = LocalSeries(
            Path("/anime/BanG Dream! Ave Mujica"),
            ["BanG Dream! Ave Mujica"],
            2025,
            None,
        )
        candidates = [
            MikanSearchCandidate(3518, "BanG Dream! Ave Mujica", "BanG Dream! Ave Mujica"),
            MikanSearchCandidate(9999, "BanG Dream! Ave Mujica", "BanG Dream! Ave Mujica"),
        ]
        config = SimpleNamespace(
            mikan_base_url="https://mikanani.me",
            mikan_request_timeout_seconds=30,
            mikan_auto_match_threshold=0.86,
        )

        with (
            patch("mikan_matcher._search_candidates_for_series", return_value=candidates),
            patch("mikan_matcher.fetch_bangumi_releases", return_value=[]),
        ):
            result = _auto_match_series(series, config, logging.getLogger("test"))

        self.assertEqual(result["status"], "miss")
        self.assertEqual(result["reason"], "ambiguous_candidates")
        self.assertEqual(result["best"]["bangumi_id"], 3518)
        self.assertEqual(result["runner_up"]["bangumi_id"], 9999)
        self.assertEqual(result["margin"], 0.0)

    def test_unlocked_metadata_mapping_is_revalidated_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "input"
            work_path = temp_path / "work"
            series_root = input_path / "BanG Dream!"
            (series_root / "Season 1").mkdir(parents=True)
            (series_root / "Season 1" / "BanG Dream! - S01E01.mkv").write_bytes(b"")
            config = SimpleNamespace(
                input_path=input_path,
                work_path=work_path,
                video_extensions=[".mkv"],
                mikan_series_path_mappings=[],
                mikan_auto_match_enabled=True,
                mikan_auto_match_threshold=0.86,
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_auto_match_max_candidates=6,
                mikan_base_url="https://mikanani.me",
                mikan_request_timeout_seconds=30,
            )
            stale = {
                "bangumi_id": 2218,
                "path": str(series_root),
                "match": ["BanG Dream!"],
                "identity_source": "series_metadata",
                "locked": False,
            }
            release = MikanRelease(
                bangumi_id=1234,
                title="[Group] BanG Dream! - 01 [WebRip]",
                episode=1,
                torrent_url="https://example.invalid/bang-dream.torrent",
                pub_date=None,
                content_length=100,
            )

            with (
                patch("mikan_matcher._series_metadata_mappings", return_value=[stale]),
                patch(
                    "mikan_matcher.search_mikan_bangumi",
                    return_value=[MikanSearchCandidate(1234, "BanG Dream!", "BanG Dream!")],
                ) as search,
                patch("mikan_matcher.fetch_bangumi_releases", return_value=[release]),
            ):
                mappings = resolve_mikan_series_mappings(config, logging.getLogger("test"))

            self.assertGreater(search.call_count, 0)
            self.assertEqual([mapping["bangumi_id"] for mapping in mappings], [1234])

    def test_locked_metadata_mapping_still_blocks_auto_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "input"
            series_root = input_path / "Reviewed Show"
            (series_root / "Season 1").mkdir(parents=True)
            (series_root / "Season 1" / "Reviewed Show - S01E01.mkv").write_bytes(b"")
            config = SimpleNamespace(
                input_path=input_path,
                work_path=temp_path / "work",
                video_extensions=[".mkv"],
                mikan_series_path_mappings=[],
                mikan_auto_match_enabled=True,
                mikan_auto_match_threshold=0.86,
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_auto_match_max_candidates=6,
                mikan_base_url="https://mikanani.me",
                mikan_request_timeout_seconds=30,
            )
            locked = {
                "bangumi_id": 4321,
                "path": str(series_root),
                "match": ["Reviewed Show"],
                "identity_source": "series_metadata",
                "locked": True,
            }

            with (
                patch("mikan_matcher._series_metadata_mappings", return_value=[locked]),
                patch("mikan_matcher.search_mikan_bangumi") as search,
            ):
                mappings = resolve_mikan_series_mappings(config, logging.getLogger("test"))

            search.assert_not_called()
            self.assertEqual([mapping["bangumi_id"] for mapping in mappings], [4321])

    def test_manual_metadata_mapping_still_blocks_auto_revalidation_when_unlocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "input"
            series_root = input_path / "Manual Show"
            (series_root / "Season 1").mkdir(parents=True)
            (series_root / "Season 1" / "Manual Show - S01E01.mkv").write_bytes(b"")
            config = SimpleNamespace(
                input_path=input_path,
                work_path=temp_path / "work",
                video_extensions=[".mkv"],
                mikan_series_path_mappings=[],
                mikan_auto_match_enabled=True,
                mikan_auto_match_threshold=0.86,
                mikan_auto_match_cache_path="mikan_auto_matches.json",
                mikan_auto_match_max_candidates=6,
                mikan_base_url="https://mikanani.me",
                mikan_request_timeout_seconds=30,
            )
            manual = {
                "bangumi_id": 5432,
                "path": str(series_root),
                "match": ["Manual Show"],
                "identity_source": "manual",
                "locked": False,
            }

            with (
                patch("mikan_matcher._series_metadata_mappings", return_value=[manual]),
                patch("mikan_matcher.search_mikan_bangumi") as search,
            ):
                mappings = resolve_mikan_series_mappings(config, logging.getLogger("test"))

            search.assert_not_called()
            self.assertEqual([mapping["bangumi_id"] for mapping in mappings], [5432])

    def test_series_metadata_mappings_page_past_first_thousand_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir)
            profiles = [
                SeriesProfile(
                    local_path=str(path),
                    canonical_title=f"Series {index}",
                    mikan_bangumi_id=index,
                )
                for index in range(1, 1002)
            ]

            class Store:
                offsets: list[int] = []

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def list_profiles(self, *, limit: int, offset: int = 0):
                    self.offsets.append(offset)
                    return profiles[offset:offset + limit]

            store = Store()
            with patch("mikan_matcher.SeriesMetadataStore.from_config", return_value=store):
                mappings = _series_metadata_mappings(SimpleNamespace(), logging.getLogger("test"))

            self.assertEqual(len(mappings), 1001)
            self.assertEqual(store.offsets, [0, 1000])
            self.assertEqual(mappings[-1]["bangumi_id"], 1001)


if __name__ == "__main__":
    unittest.main()
