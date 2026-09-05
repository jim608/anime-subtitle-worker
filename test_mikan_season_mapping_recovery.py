import copy
import hashlib
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mikan_matcher import _deduplicate_mappings, _season_scoped_cached_mappings, resolve_mikan_series_mappings
from mikan_worker import _assess_release_identity, _explicit_completed_season_conflict, _target_video_for_torrent_source
from mikan_source import MikanRelease


class MikanSeasonMappingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Show"
        self.scope = self.root / "Season 2"
        self.scope.mkdir(parents=True)
        self.show_nfo = self.root / "tvshow.nfo"
        self.season_nfo = self.scope / "season.nfo"
        self.show_nfo.write_text("<tvshow><title>Show</title><originaltitle>Show / Example Show</originaltitle><tmdbid>123</tmdbid></tvshow>", encoding="utf-8")
        self.season_nfo.write_text("<season><title>Season 2</title><seasonnumber>2</seasonnumber></season>", encoding="utf-8")
        self.config = SimpleNamespace(mikan_auto_match_threshold=0.9)
        self.mapping = {"path": str(self.root), "bangumi_id": 22, "match": ["Show"], "title": "Show",
                        "metadata_title": "Show", "metadata_provider": "anilist", "metadata_provider_id": "123",
                        "identity_source": "series_metadata", "locked": False}
        self.cache = {str(self.root): {"status": "miss", "matcher_version": 2,
                      "reason": "ambiguous_candidates", "confidence": 1.0,
                      "best": {"bangumi_id": 22, "title": "Show 第二季", "source_query": "Show / Example Show"},
                      "runner_up": {"bangumi_id": 11, "title": "Show", "source_query": "Show"}}}

    def resolve(self, mapping=None):
        return _season_scoped_cached_mappings(self.cache, [mapping or self.mapping], self.config)

    def test_explicit_season_is_scoped_without_clearing_original_miss(self):
        before = copy.deepcopy(self.cache)
        results = self.resolve()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], str(self.scope))
        self.assertEqual(results[0]["season"], 2)
        self.assertEqual(results[0]["bangumi_id"], 22)
        self.assertEqual(results[0]["identity_evidence"]["source_id"], 22)
        self.assertEqual(self.cache, before)

    def test_no_season_one_inference_or_provider_identity_substitution(self):
        self.assertEqual(self.resolve({**self.mapping, "bangumi_id": 11}), [])
        self.assertEqual(self.resolve({**self.mapping, "metadata_provider_id": ""}), [])
        self.assertEqual(self.resolve({**self.mapping, "metadata_title": "Another Show"}), [])

    def test_already_matched_explicit_season_replaces_only_unlocked_root(self):
        self.cache[str(self.root)] = {"status": "matched", "matcher_version": 2, "confidence": 1.0,
                                     "mapping": {"path": str(self.root), "bangumi_id": 22, "title": "Show 第二季", "match": ["Show"]}}
        scoped = self.resolve()
        self.assertEqual(len(scoped), 1)
        self.assertEqual(_deduplicate_mappings([self.mapping, *scoped]), scoped)
        locked = {**self.mapping, "manual_locked": True}
        self.assertEqual(_deduplicate_mappings([locked, *scoped]), [locked, *scoped])

    def test_resolve_cached_and_budgeted_paths_keep_scope_instead_of_root(self):
        self.cache[str(self.root)] = {"status": "matched", "matcher_version": 2, "confidence": 1.0,
                                     "mapping": {"path": str(self.root), "bangumi_id": 22, "title": "Show 第二季", "match": ["Show"]}}
        config = SimpleNamespace(mikan_auto_match_threshold=0.9, mikan_series_path_mappings=[],
                                 mikan_auto_match_enabled=True, mikan_auto_match_max_lookups_per_cycle=25)
        profile = SimpleNamespace(local_path=str(self.root), canonical_title="Show", aliases=["Show"],
                                  titles=[], premiered_year=2020, anidb_id="123")

        class Store:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def list_profiles(self, *, limit, offset=0):
                return [profile] if offset == 0 else []

        with patch("mikan_matcher._series_metadata_mappings", return_value=[self.mapping]), \
                patch("mikan_matcher._resolve_cache_path", return_value=self.root / "cache.json"), \
                patch("mikan_matcher._load_cache", return_value=self.cache), \
                patch("mikan_matcher.SeriesMetadataStore.from_config", return_value=Store()), \
                patch("mikan_matcher._auto_match_series") as remote_lookup:
            for kwargs in ({"cached_only": True}, {"deadline_monotonic": time.monotonic() + 5}):
                result = resolve_mikan_series_mappings(config, logging.getLogger("test"), **kwargs)
                self.assertEqual([m["path"] for m in result], [str(self.scope)])
                self.assertEqual(result[0]["identity_source"], "cached_season_nfo")
            remote_lookup.assert_not_called()

    def test_different_series_and_special_episode_are_not_season_siblings(self):
        self.cache[str(self.root)]["runner_up"]["title"] = "Another Show"
        self.assertEqual(self.resolve(), [])
        self.cache[str(self.root)]["runner_up"]["title"] = "Show [6.5]"
        self.assertEqual(self.resolve(), [])

    def test_nfo_must_agree_and_season_scope_must_exist(self):
        self.season_nfo.write_text("<season><seasonnumber>1</seasonnumber></season>", encoding="utf-8")
        self.assertEqual(self.resolve(), [])
        self.season_nfo.unlink()
        self.assertEqual(self.resolve(), [])

    def test_query_must_be_an_independent_nfo_alias(self):
        self.cache[str(self.root)]["best"]["source_query"] = "untrusted broad query"
        self.assertEqual(self.resolve(), [])

    def test_restart_is_deterministic_and_nfo_change_rechecks_evidence(self):
        original = [p.read_bytes() for p in (self.show_nfo, self.season_nfo)]
        first = self.resolve()[0]
        self.cache = json.loads(json.dumps(self.cache))
        second = self.resolve()[0]
        self.assertEqual(first["identity_fingerprint"], second["identity_fingerprint"])
        self.assertEqual([p.read_bytes() for p in (self.show_nfo, self.season_nfo)], original)
        self.assertEqual(first["identity_evidence"]["season_nfo_sha256"], hashlib.sha256(original[1]).hexdigest())
        self.season_nfo.write_text("<season><seasonnumber>0</seasonnumber></season>", encoding="utf-8")
        self.assertEqual(self.resolve(), [])

    def test_conflicting_source_season_is_not_overridden_by_mapping_or_score(self):
        source = Path(self.temp.name) / "Show S01E01.mkv"
        source.write_bytes(b"isolated source")
        mapping = {**self.mapping, "path": str(self.scope), "season": 2}
        kwargs = dict(source_video=source, torrent_name="Show S01E01", mappings=[mapping],
                      pending_entries=[{"bangumi_id": 22, "episode": 1}])
        self.assertTrue(_explicit_completed_season_conflict(**kwargs))
        diagnostics = []
        with patch("mikan_worker._target_videos_from_episode_index_with_hint_fallback") as target_lookup:
            result = _target_video_for_torrent_source(
                source, SimpleNamespace(name="Show S01E01"), self.config, logging.getLogger("test"),
                [mapping], pending_entries=kwargs["pending_entries"], target_diagnostics=diagnostics,
            )
        self.assertIsNone(result)
        target_lookup.assert_not_called()
        self.assertEqual(diagnostics[0]["reason"], "conflicting_explicit_seasons")
        self.assertEqual(source.read_bytes(), b"isolated source")

    def test_special_and_regular_scopes_do_not_merge(self):
        self.assertTrue(_explicit_completed_season_conflict(source_video=None, torrent_name="Show S01E01",
                        mappings=[{"season": 0}], pending_entries=None))

    def test_unknown_historical_batch_cannot_use_recovered_season_scope(self):
        scoped = self.resolve()
        for source in ("mikan", "qbit-recovered", "dmhy"):
            release = MikanRelease(22, "[DMG&LoliHouse] Show [01-25][WebRip 1080p HEVC AAC ASSx2]", 1,
                                   "https://example.test/old.torrent", None, 100, source=source)
            assessment = _assess_release_identity(release, scoped)
            self.assertFalse(assessment.safe)
            self.assertEqual(assessment.reason, "scoped_source_season_unverified")
        release = MikanRelease(22, "[Group] Show 第二季 [01][CHT][MKV]", 1,
                               "https://example.test/new.torrent", None, 100, source="mikan")
        self.assertTrue(_assess_release_identity(release, scoped).safe)
        source_video = Path(self.temp.name) / "Show - 01.mkv"
        source_video.write_bytes(b"isolated unknown season source")
        diagnostics = []
        result = _target_video_for_torrent_source(
            source_video, SimpleNamespace(name="Show [01-25]"), self.config, logging.getLogger("test"),
            scoped, pending_entries=[{"bangumi_id": 22, "episode": 1, "title": "Show 第二季"}], target_diagnostics=diagnostics)
        self.assertIsNone(result)
        self.assertEqual(diagnostics[0]["reason"], "scoped_source_season_unverified")
        self.assertEqual(source_video.read_bytes(), b"isolated unknown season source")


if __name__ == "__main__":
    unittest.main()
