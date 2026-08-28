from __future__ import annotations

import logging
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import requests

import mikan_fallback_sources as fallback_module
from mikan_fallback_sources import (
    FallbackSourcePool,
    _filter_releases,
    _parse_animegarden,
    _parse_rss,
)
from mikan_source import MikanRelease, extract_torrent_info_hash
from mikan_source import select_preferred_release_candidates_for_episodes


INFO_HASH = "0123456789abcdef0123456789abcdef01234567"


class MikanFallbackSourceTest(unittest.TestCase):
    def test_extracts_info_hash_from_magnet_and_torrent_url(self) -> None:
        self.assertEqual(
            extract_torrent_info_hash(f"magnet:?xt=urn:btih:{INFO_HASH.upper()}&dn=Show"),
            INFO_HASH,
        )
        self.assertEqual(
            extract_torrent_info_hash(f"https://mikanani.me/Download/20260622/{INFO_HASH}.torrent"),
            INFO_HASH,
        )

    def test_parses_animegarden_resource(self) -> None:
        releases = _parse_animegarden(
            {
                "resources": [
                    {
                        "title": "[Group] Test Show - 07 [CHT][MKV]",
                        "magnet": f"magnet:?xt=urn:btih:{INFO_HASH}",
                        "href": "https://dmhy.org/topics/view/1.html",
                        "provider": "dmhy",
                        "createdAt": "2026-06-22T01:02:03Z",
                        "size": 123,
                    }
                ]
            },
            123,
        )

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0].episodes, (7,))
        self.assertEqual(releases[0].source, "animegarden:dmhy")
        self.assertEqual(releases[0].info_hash, INFO_HASH)

    def test_parses_nyaa_rss_metadata(self) -> None:
        rss = f"""<?xml version="1.0"?>
<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><item>
  <title>[Group] Test Show - 07 [CHT][MKV]</title>
  <guid>https://nyaa.si/view/123</guid>
  <enclosure url="https://nyaa.si/download/123.torrent" />
  <nyaa:infoHash>{INFO_HASH.upper()}</nyaa:infoHash>
  <nyaa:seeders>8</nyaa:seeders>
  <nyaa:size>1.5 GiB</nyaa:size>
  <pubDate>Sun, 22 Jun 2026 01:02:03 +0000</pubDate>
</item></channel></rss>"""

        releases = _parse_rss(rss, 123, "nyaa")

        self.assertEqual(releases[0].info_hash, INFO_HASH)
        self.assertEqual(releases[0].seeders, 8)
        self.assertEqual(releases[0].content_length, int(1.5 * 1024**3))
        self.assertEqual(releases[0].link, "https://nyaa.si/view/123")

    def test_rejects_volume_number_false_episode_and_dead_nyaa(self) -> None:
        releases = [
            _release("Test Show Vol.7 [CHT][MKV]", source="dmhy"),
            _release("Test Show - 07 [CHT][MKV]", source="nyaa", seeders=0),
            _release("Test Show S01E07 [English subtitles][MKV]", source="dmhy"),
            _release("Test Show S01E07 [CHT][MKV]", source="nyaa", seeders=2),
        ]

        filtered = _filter_releases(releases, ["Test Show"], {7}, min_nyaa_seeders=1)

        self.assertEqual([release.title for release in filtered], ["Test Show S01E07 [CHT][MKV]"])

    def test_search_uses_persistent_cache_and_deduplicates_info_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                mikan_fallback_sources_enabled=True,
                mikan_fallback_sources=["dmhy", "nyaa"],
                mikan_fallback_max_lookups_per_cycle=2,
                mikan_fallback_cache_ttl_seconds=3600,
            )
            pool = FallbackSourcePool(config, _logger())
            release = _release("Test Show - 07 [CHT][MKV]", source="dmhy", seeders=3)
            mappings = [{"title": "Test Show", "match": ["Test Show"], "path": str(Path(temp_dir) / "Test Show")}]

            with patch.object(pool, "_fetch_source", return_value=[release]) as fetch:
                first = pool.search(123, mappings, {7})
                second = pool.search(123, mappings, {7})

            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertEqual(fetch.call_count, 2)
            payload = json.loads((Path(temp_dir) / "mikan_fallback_sources.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(len(next(iter(payload["entries"].values()))["releases"]), 1)

    def test_lookup_budget_deferred_result_is_not_conclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                mikan_fallback_sources_enabled=True,
                mikan_fallback_sources=["dmhy"],
                mikan_fallback_max_lookups_per_cycle=0,
            )
            pool = FallbackSourcePool(config, _logger())
            mappings = [{"title": "Test Show", "match": ["Test Show"]}]

            result = pool.search(123, mappings, {7})

            self.assertEqual(result, [])
            self.assertFalse(result.conclusive)
            self.assertFalse(result.lookup_performed)
            self.assertEqual(result.deferred_reason, "lookup_budget_exhausted")

    def test_provider_circuit_opens_half_opens_and_recovers_without_blocking_healthy_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                mikan_fallback_sources_enabled=True,
                mikan_fallback_sources=["acgrip", "dmhy"],
                mikan_fallback_max_lookups_per_cycle=10,
                mikan_fallback_cache_ttl_seconds=3600,
                mikan_fallback_source_failure_threshold=2,
                mikan_fallback_source_cooldown_seconds=30,
            )
            pool = FallbackSourcePool(config, _logger())
            mappings = [{"title": "Test Show", "match": ["Test Show"]}]
            calls: list[str] = []
            acgrip_healthy = False

            def fetch(source: str, *_args):
                nonlocal acgrip_healthy
                calls.append(source)
                if source == "acgrip" and not acgrip_healthy:
                    raise requests.ConnectTimeout("acgrip unavailable")
                return [_release("Test Show - 07 [CHT][MKV]", source=source, seeders=3)]

            with patch.object(pool, "_fetch_source", side_effect=fetch):
                with patch("mikan_fallback_sources.time.time", return_value=100.0):
                    first = pool.search(123, mappings, {7})
                    second = pool.search(123, mappings, {8})
                    third = pool.search(123, mappings, {9})

                self.assertFalse(first.conclusive)
                self.assertFalse(second.conclusive)
                self.assertFalse(third.conclusive)
                self.assertEqual(len(first), 1)
                self.assertEqual(calls.count("acgrip"), 2)
                self.assertEqual(calls.count("dmhy"), 3)
                self.assertIn("acgrip", third.skipped_sources)

                acgrip_healthy = True
                with patch("mikan_fallback_sources.time.time", return_value=131.0):
                    recovered = pool.search(123, mappings, {10})
                with patch("mikan_fallback_sources.time.time", return_value=132.0):
                    closed = pool.search(123, mappings, {11})

            self.assertIn("acgrip", recovered.successful_sources)
            self.assertIn("acgrip", closed.successful_sources)
            self.assertEqual(calls.count("acgrip"), 4)
            self.assertEqual(calls.count("dmhy"), 5)

    def test_partial_success_is_inconclusive_and_uses_short_cache_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                mikan_fallback_sources_enabled=True,
                mikan_fallback_sources=["acgrip", "dmhy"],
                mikan_fallback_max_lookups_per_cycle=4,
                mikan_fallback_cache_ttl_seconds=3600,
                mikan_fallback_source_failure_threshold=2,
                mikan_fallback_source_cooldown_seconds=1800,
            )
            pool = FallbackSourcePool(config, _logger())
            mappings = [{"title": "Test Show", "match": ["Test Show"]}]

            def fetch(source: str, *_args):
                if source == "acgrip":
                    raise requests.ConnectTimeout("acgrip unavailable")
                return [_release("Test Show - 07 [CHT][MKV]", source=source, seeders=3)]

            with patch.object(pool, "_fetch_source", side_effect=fetch) as fetch_mock:
                with patch("mikan_fallback_sources.time.time", return_value=100.0):
                    first = pool.search(123, mappings, {7})
                with patch("mikan_fallback_sources.time.time", return_value=500.0):
                    cached = pool.search(123, mappings, {7})
                with patch("mikan_fallback_sources.time.time", return_value=701.0):
                    retried = pool.search(123, mappings, {7})

            self.assertFalse(first.conclusive)
            self.assertEqual(first.deferred_reason, "partial_provider_coverage")
            self.assertEqual(len(first), 1)
            self.assertFalse(cached.conclusive)
            self.assertTrue(cached.cache_hit)
            self.assertFalse(retried.conclusive)
            self.assertEqual(fetch_mock.call_count, 4)

    def test_provider_circuit_survives_pool_and_process_state_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                mikan_fallback_sources_enabled=True,
                mikan_fallback_sources=["acgrip"],
                mikan_fallback_max_lookups_per_cycle=10,
                mikan_fallback_cache_ttl_seconds=3600,
                mikan_fallback_source_failure_threshold=1,
                mikan_fallback_source_cooldown_seconds=30,
            )
            mappings = [{"title": "Test Show", "match": ["Test Show"]}]
            first_pool = FallbackSourcePool(config, _logger())
            with (
                patch.object(
                    first_pool,
                    "_fetch_source",
                    side_effect=requests.ConnectTimeout("provider unavailable"),
                ),
                patch("mikan_fallback_sources.time.time", return_value=100.0),
            ):
                failed = first_pool.search(123, mappings, {7})
            self.assertFalse(failed.conclusive)

            with fallback_module._PROVIDER_CIRCUIT_LOCK:
                fallback_module._PROVIDER_CIRCUITS.clear()

            restarted_pool = FallbackSourcePool(config, _logger())
            with (
                patch.object(restarted_pool, "_fetch_source") as blocked_fetch,
                patch("mikan_fallback_sources.time.time", return_value=110.0),
            ):
                blocked = restarted_pool.search(123, mappings, {8})
            self.assertEqual(blocked.deferred_reason, "all_providers_unavailable")
            self.assertEqual(blocked.skipped_sources, ("acgrip",))
            blocked_fetch.assert_not_called()

            with (
                patch.object(
                    restarted_pool,
                    "_fetch_source",
                    return_value=[_release("Test Show - 09 [CHT][MKV]", source="acgrip")],
                ) as recovered_fetch,
                patch("mikan_fallback_sources.time.time", return_value=131.0),
            ):
                recovered = restarted_pool.search(123, mappings, {9})
            self.assertTrue(recovered.conclusive)
            recovered_fetch.assert_called_once()

    def test_all_provider_failure_uses_short_cache_and_remains_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                work_path=Path(temp_dir),
                mikan_fallback_sources_enabled=True,
                mikan_fallback_sources=["acgrip"],
                mikan_fallback_max_lookups_per_cycle=4,
                mikan_fallback_cache_ttl_seconds=3600,
                mikan_fallback_source_failure_threshold=2,
                mikan_fallback_source_cooldown_seconds=1800,
            )
            pool = FallbackSourcePool(config, _logger())
            mappings = [{"title": "Test Show", "match": ["Test Show"]}]

            with patch.object(
                pool,
                "_fetch_source",
                side_effect=requests.ConnectTimeout("all providers unavailable"),
            ) as fetch:
                with patch("mikan_fallback_sources.time.time", return_value=100.0):
                    first = pool.search(123, mappings, {7})
                with patch("mikan_fallback_sources.time.time", return_value=500.0):
                    cached = pool.search(123, mappings, {7})
                with patch("mikan_fallback_sources.time.time", return_value=701.0):
                    retried = pool.search(123, mappings, {7})

            self.assertFalse(first.conclusive)
            self.assertEqual(first.deferred_reason, "all_providers_failed")
            self.assertFalse(cached.conclusive)
            self.assertTrue(cached.cache_hit)
            self.assertFalse(retried.conclusive)
            self.assertEqual(fetch.call_count, 2)

    def test_legacy_fallback_cache_is_rewritten_without_unbounded_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_path = root / "mikan_fallback_sources.json"
            cache_path.write_text(
                json.dumps({"entries": {"legacy": {"fetched_at": 1, "releases": [{"title": "x"}]}}}),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                work_path=root,
                mikan_fallback_sources_enabled=True,
                mikan_fallback_sources=["dmhy"],
            )

            FallbackSourcePool(config, _logger())

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(payload, {"version": 2, "entries": {}})

    def test_external_mkv_multisub_is_allowed_only_as_fallback(self) -> None:
        fallback = _release("Test Show - 07 [Multi-Subs][MKV]", source="nyaa", seeders=2)
        mikan = MikanRelease(
            bangumi_id=fallback.bangumi_id,
            title=fallback.title,
            episode=fallback.episode,
            episodes=fallback.episodes,
            torrent_url=fallback.torrent_url,
            pub_date=None,
            content_length=100,
            source="mikan",
            info_hash=fallback.info_hash,
        )

        fallback_candidates = select_preferred_release_candidates_for_episodes(
            [fallback],
            episodes={7},
            prefer_keywords=["CHT"],
            reject_keywords=[],
            require_extractable=True,
        )
        mikan_candidates = select_preferred_release_candidates_for_episodes(
            [mikan],
            episodes={7},
            prefer_keywords=["CHT"],
            reject_keywords=[],
            require_extractable=True,
        )

        self.assertEqual(fallback_candidates[7], [fallback])
        self.assertEqual(mikan_candidates[7], [])


def _release(title: str, *, source: str, seeders: int | None = None) -> MikanRelease:
    return MikanRelease(
        bangumi_id=123,
        title=title,
        episode=7,
        episodes=(7,),
        torrent_url=f"magnet:?xt=urn:btih:{INFO_HASH}",
        pub_date=None,
        content_length=100,
        source=source,
        info_hash=INFO_HASH,
        seeders=seeders,
    )


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.mikan_fallback_sources")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
