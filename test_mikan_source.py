from __future__ import annotations

import unittest
from unittest.mock import patch

from mikan_source import (
    MikanRelease,
    MikanSourceError,
    extract_episode_number,
    extract_episode_numbers,
    fetch_bangumi_releases,
    has_english_only_subtitle_hint,
    parse_mikan_rss,
    release_score,
    release_episode_numbers,
    release_season_number,
    release_series_identity,
    select_preferred_release_candidates_for_episodes,
    select_preferred_releases,
    select_preferred_releases_for_episodes,
)


RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[Group] Test Anime [06][1080p][简繁日内封]</title>
      <link>https://mikanani.me/Home/Episode/hash-a</link>
      <torrent xmlns="https://mikanani.me/0.1/">
        <pubDate>2026-05-15T23:32:50</pubDate>
        <contentLength>100</contentLength>
      </torrent>
      <enclosure type="application/x-bittorrent" length="100" url="/Download/20260515/hash-a.torrent" />
    </item>
    <item>
      <title>[Group] Test Anime [06][1080p][繁日内嵌]</title>
      <link>https://mikanani.me/Home/Episode/hash-b</link>
      <torrent xmlns="https://mikanani.me/0.1/">
        <pubDate>2026-05-15T23:33:50</pubDate>
        <contentLength>90</contentLength>
      </torrent>
      <enclosure type="application/x-bittorrent" length="90" url="/Download/20260515/hash-b.torrent" />
    </item>
    <item>
      <title>[Raw] Test Anime [07][1080p][RAW]</title>
      <link>https://mikanani.me/Home/Episode/hash-c</link>
      <enclosure type="application/x-bittorrent" length="90" url="/Download/20260515/hash-c.torrent" />
    </item>
  </channel>
</rss>
"""


class MikanSourceTest(unittest.TestCase):
    def test_release_identity_records_explicit_season_evidence(self) -> None:
        release = MikanRelease(
            bangumi_id=123,
            title="[Group] Test Show S2 - 07 [CHT][MKV]",
            episode=7,
            torrent_url="https://example.test/show-s2-07.torrent",
            pub_date=None,
            content_length=100,
            source="dmhy",
        )

        self.assertEqual(release_season_number(release.title), 2)
        self.assertEqual(release_series_identity(release.title), "test show")
        self.assertEqual(release.season_number, 2)
        self.assertEqual(release.series_identity, "test show")
        self.assertIn("explicit_season:2", release.identity_evidence)

    def test_release_season_parser_does_not_treat_episode_after_named_season_as_season(self) -> None:
        title = "Monogatari Series Off & Monster Season [06][CHT][MKV]"

        self.assertIsNone(release_season_number(title))
        self.assertEqual(
            release_series_identity(title),
            "monogatari series off monster season",
        )

    def test_parse_invalid_rss_raises_source_error(self) -> None:
        with self.assertRaises(MikanSourceError):
            parse_mikan_rss("<rss><channel>", "https://mikanani.me", 3905)

    def test_fetch_retries_truncated_rss_response(self) -> None:
        class Response:
            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        with (
            patch("mikan_source.requests.get", side_effect=[Response("<rss><channel>"), Response(RSS)]) as get,
            patch("mikan_source.time.sleep", return_value=None),
        ):
            releases = fetch_bangumi_releases("https://mikanani.me", 3905, max_attempts=2)

        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(releases), 3)

    def test_parse_rss_and_selects_extractable_chinese_release(self) -> None:
        releases = parse_mikan_rss(RSS, "https://mikanani.me", 3905)
        selected = select_preferred_releases(
            releases,
            max_items=1,
            prefer_keywords=["简繁日内封", "繁日内嵌"],
            reject_keywords=[],
            require_extractable=True,
        )

        self.assertEqual(len(releases), 3)
        self.assertEqual(selected[0].episode, 6)
        self.assertIn("简繁日内封", selected[0].title)
        self.assertEqual(selected[0].torrent_url, "https://mikanani.me/Download/20260515/hash-a.torrent")

    def test_selects_only_requested_missing_episodes(self) -> None:
        releases = parse_mikan_rss(RSS, "https://mikanani.me", 3905)
        selected = select_preferred_releases_for_episodes(
            releases,
            episodes={6, 7},
            prefer_keywords=["简繁日内封", "繁日内嵌"],
            reject_keywords=[],
            require_extractable=True,
        )

        self.assertEqual([release.episode for release in selected], [6])

    def test_selects_episode_range_release_once_for_multiple_missing_episodes(self) -> None:
        rss = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[Group] Test Anime S01E01-S01E03 [WebRip 1080p][CHT][MKV]</title>
      <enclosure type="application/x-bittorrent" length="100" url="/Download/range.torrent" />
    </item>
  </channel>
</rss>
"""

        releases = parse_mikan_rss(rss, "https://mikanani.me", 3905)
        selected = select_preferred_releases_for_episodes(
            releases,
            episodes={1, 2, 3, 4},
            prefer_keywords=["CHT"],
            reject_keywords=[],
            require_extractable=True,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(release_episode_numbers(selected[0]), (1, 2, 3))
        self.assertEqual(selected[0].episode, 1)

    def test_lists_episode_range_release_for_each_covered_episode(self) -> None:
        rss = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[Group] Test Anime [01~03][WebRip 1080p][CHT][MKV]</title>
      <enclosure type="application/x-bittorrent" length="100" url="/Download/range.torrent" />
    </item>
  </channel>
</rss>
"""

        releases = parse_mikan_rss(rss, "https://mikanani.me", 3905)
        candidates = select_preferred_release_candidates_for_episodes(
            releases,
            episodes={1, 2, 3, 4},
            prefer_keywords=["CHT"],
            reject_keywords=[],
            require_extractable=True,
        )

        self.assertEqual([release.torrent_url for release in candidates[1]], ["https://mikanani.me/Download/range.torrent"])
        self.assertEqual([release.torrent_url for release in candidates[2]], ["https://mikanani.me/Download/range.torrent"])
        self.assertEqual([release.torrent_url for release in candidates[3]], ["https://mikanani.me/Download/range.torrent"])
        self.assertEqual(candidates[4], [])

    def test_lists_alternate_release_candidates_for_episode(self) -> None:
        releases = parse_mikan_rss(RSS, "https://mikanani.me", 3905)
        candidates = select_preferred_release_candidates_for_episodes(
            releases,
            episodes={6},
            prefer_keywords=["蝞蝜??", "蝜??"],
            reject_keywords=[],
            require_extractable=False,
        )

        self.assertEqual([release.torrent_url for release in candidates[6]], [
            "https://mikanani.me/Download/20260515/hash-a.torrent",
            "https://mikanani.me/Download/20260515/hash-b.torrent",
        ])

    def test_english_only_subtitle_release_is_rejected(self) -> None:
        rss = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>[Group] Test Anime [01-12][HEVC][MKV][English subtitles]</title>
      <enclosure type="application/x-bittorrent" length="100" url="/Download/english.torrent" />
    </item>
    <item>
      <title>[Group] Test Anime [01-12][HEVC][MKV][CHS_CHT_ENG_SRT]</title>
      <enclosure type="application/x-bittorrent" length="100" url="/Download/chinese.torrent" />
    </item>
  </channel>
</rss>
"""

        releases = parse_mikan_rss(rss, "https://mikanani.me", 3905)
        candidates = select_preferred_release_candidates_for_episodes(
            releases,
            episodes={1},
            prefer_keywords=["CHT", "CHS"],
            reject_keywords=[],
            require_extractable=True,
        )

        self.assertTrue(has_english_only_subtitle_hint("[Group] Test Anime [English subtitles]"))
        self.assertFalse(has_english_only_subtitle_hint("[Group] Test Anime [CHS_CHT_ENG_SRT]"))
        self.assertEqual([release.torrent_url for release in candidates[1]], ["https://mikanani.me/Download/chinese.torrent"])

    def test_release_score_prefers_explicit_chinese_seeded_extractable_sources(self) -> None:
        explicit = MikanRelease(
            bangumi_id=1,
            title="[Group] Test Anime - 01 [1080p][CHS&CHT][MKV]",
            episode=1,
            torrent_url="magnet:?xt=urn:btih:a",
            pub_date=None,
            content_length=100,
            source="nyaa",
            seeders=20,
        )
        multi_only = MikanRelease(
            bangumi_id=1,
            title="[Group] Test Anime - 01 [1080p][Multi-Subs][MKV]",
            episode=1,
            torrent_url="magnet:?xt=urn:btih:b",
            pub_date=None,
            content_length=100,
            source="acgrip",
            seeders=1,
        )

        self.assertGreater(release_score(explicit, []), release_score(multi_only, []))

    def test_extract_episode_number_supports_library_names(self) -> None:
        self.assertEqual(extract_episode_number("MARRIAGETOXIN - S01E06 - WEBDL-1080p.mkv"), 6)
        self.assertEqual(extract_episode_number("[LoliHouse] Anime - 06 [WebRip].mkv"), 6)
        self.assertEqual(extract_episode_number("[Sakurato] Anime [08v2][1080p][CHT].mkv"), 8)
        self.assertEqual(extract_episode_numbers("[Sakurato] Anime [08v2][1080p][CHT].mkv"), (8,))
        self.assertEqual(
            extract_episode_number("[LoliHouse] Class de 2-banme ni Kawaii Onnanoko - 06 [WebRip].mkv"),
            6,
        )

    def test_extract_episode_number_ignores_movie_part_and_volume_numbers(self) -> None:
        movie = "The.Seven.Deadly.Sins.Grudge.of.Edinburgh.Part.2.2023.1080p.mkv"
        self.assertIsNone(extract_episode_number(movie))
        self.assertEqual(extract_episode_numbers(movie), ())
        self.assertIsNone(extract_episode_number("Anime Movie Part - 2 [1080p].mkv"))
        self.assertIsNone(extract_episode_number("Anime Vol.3 [BDRip].mkv"))

        # An explicit episode marker must still win even when the show title has
        # a numbered part in it.
        self.assertEqual(extract_episode_number("Anime Part 2 - S01E06.mkv"), 6)

    def test_extract_episode_numbers_supports_ranges(self) -> None:
        self.assertEqual(extract_episode_numbers("Anime S01E01-S01E03 [CHT].mkv"), (1, 2, 3))
        self.assertEqual(extract_episode_numbers("[Group] Anime [01~03][CHT].mkv"), (1, 2, 3))
        self.assertEqual(extract_episode_numbers("[Group] Anime - 01-03 [CHT].mkv"), (1, 2, 3))
        self.assertEqual(extract_episode_numbers("MARRIAGETOXIN - S01E06 - WEBDL-1080p.mkv"), (6,))


if __name__ == "__main__":
    unittest.main()
