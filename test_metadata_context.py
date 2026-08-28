from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from metadata_context import (
    _cache_series_artwork,
    _fetch_anilist_context,
    build_series_metadata_context,
    infer_series_title,
)
from series_metadata import SeriesProfile


class MetadataContextTest(unittest.TestCase):
    def test_infer_series_title_removes_sonarr_release_year(self) -> None:
        path = Path("/anime/Kingdom (2012)/Season 3/Kingdom - S03E01.mkv")

        self.assertEqual(infer_series_title(path), "Kingdom")

    def test_anilist_404_is_a_normal_missing_result(self) -> None:
        response = Mock(status_code=404)
        config = SimpleNamespace(metadata_context_timeout_seconds=3)

        with patch("metadata_context.requests.post", return_value=response):
            result = _fetch_anilist_context("Unknown show", config)

        self.assertIsNone(result)
        response.raise_for_status.assert_not_called()

    def test_anilist_context_carries_cover_artwork_metadata(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": {
                "Page": {
                    "media": [{
                        "id": 42,
                        "seasonYear": 2026,
                        "title": {"romaji": "Example Anime", "english": "Example Anime", "native": "例"},
                        "synonyms": [],
                        "description": "Synopsis",
                        "coverImage": {
                            "large": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/example.jpg",
                            "color": "#123456",
                        },
                        "characters": {"nodes": []},
                        "staff": {"nodes": []},
                    }]
                }
            }
        }
        config = SimpleNamespace(metadata_context_timeout_seconds=3, metadata_context_max_chars=800)

        with patch("metadata_context.requests.post", return_value=response):
            result = _fetch_anilist_context("Example Anime", config, expected_year=2026)

        self.assertIsNotNone(result)
        self.assertEqual(result.cover_image_color, "#123456")
        self.assertTrue(result.cover_image_url.startswith("https://s4.anilist.co/"))

    def test_artwork_cache_validates_and_atomically_publishes_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            response = Mock()
            response.url = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/example.jpg"
            response.headers = {"Content-Length": "7"}
            response.iter_content.return_value = [b"\xff\xd8\xfftest"]
            config = SimpleNamespace(
                work_path=root,
                series_artwork_cache_enabled=True,
                series_artwork_cache_path="series_artwork",
                series_artwork_cache_max_mib=16,
                series_artwork_max_bytes=1024,
                series_artwork_ttl_days=30,
                metadata_context_timeout_seconds=3,
            )
            with patch("metadata_context.requests.get", return_value=response):
                cache_key, updated_at = _cache_series_artwork(
                    config,
                    series_root=Path("/anime/Example Anime"),
                    image_url=response.url,
                    logger=logging.getLogger("test.metadata-context.artwork"),
                    existing=None,
                )

            self.assertRegex(cache_key, r"^series_[0-9a-f]{24}\.jpg$")
            self.assertGreater(updated_at, 0)
            self.assertEqual((root / "series_artwork" / cache_key).read_bytes(), b"\xff\xd8\xfftest")

    def test_artwork_cache_rejects_arbitrary_remote_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                series_artwork_cache_enabled=True,
                series_artwork_cache_path="series_artwork",
            )
            with patch("metadata_context.requests.get") as get:
                cache_key, _updated_at = _cache_series_artwork(
                    config,
                    series_root=Path("/anime/Example Anime"),
                    image_url="https://example.invalid/cover.jpg",
                    logger=logging.getLogger("test.metadata-context.artwork-invalid"),
                    existing=None,
                )

            self.assertEqual(cache_key, "")
            get.assert_not_called()

    def test_missing_metadata_is_cached_to_avoid_one_request_per_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                translation_metadata_context_enabled=True,
                metadata_context_providers=["anilist"],
                metadata_context_cache_path="metadata_context_cache.json",
                metadata_context_miss_ttl_seconds=3600,
                work_path=root,
            )
            video = root / "Missing Show (2026)" / "Season 1" / "Missing Show - S01E01.mkv"
            logger = logging.getLogger("test.metadata-context")

            with patch("metadata_context._fetch_anilist_context", return_value=None) as fetch:
                first = build_series_metadata_context(video, config, logger)
                second = build_series_metadata_context(video, config, logger)

            self.assertIsNone(first)
            self.assertIsNone(second)
            self.assertEqual(fetch.call_count, 1)

    def test_locked_series_profile_closes_store_before_returning(self) -> None:
        store = Mock()
        store.get_by_local_path.return_value = SeriesProfile(
            local_path="/anime/Example",
            canonical_title="Example",
            titles=["Example"],
            locked=True,
            match_source="manual",
        )
        store.glossary.return_value = {"先輩": "學長"}
        config = SimpleNamespace(
            translation_metadata_context_enabled=True,
            work_path=Path("/work"),
        )
        with patch("metadata_context.SeriesMetadataStore.from_config", return_value=store):
            context = build_series_metadata_context(
                Path("/anime/Example/Season 1/Example - S01E01.mkv"),
                config,
                logging.getLogger("test.metadata-context.locked"),
            )

        self.assertEqual(context.title, "Example")
        self.assertEqual(context.glossary, {"先輩": "學長"})
        store.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
