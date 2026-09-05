from contextlib import ExitStack
import json
import logging
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from mikan_matcher import MikanSearchCandidate, resolve_mikan_series_mappings
from mikan_source import MikanSourceDeadline
from mikan_worker import MikanWorker
from series_metadata import SeriesMetadataStore, SeriesProfile
from test_mikan_worker import _mikan_enqueue_config


class MikanMatcherDeadlineTest(TestCase):
    def config(self, root):
        config = _mikan_enqueue_config(root)
        config.input_path = root / "anime"
        config.input_path.mkdir()
        config.video_extensions = [".mkv"]
        config.mikan_series_path_mappings = []
        config.mikan_auto_match_enabled = True
        config.mikan_auto_match_threshold = 0.86
        config.mikan_auto_match_max_candidates = 6
        config.mikan_auto_match_max_lookups_per_cycle = 25
        config.mikan_auto_match_cache_path = "auto_matches.json"
        return config

    def profile(self, config, title):
        path = config.input_path / title
        path.mkdir()
        with SeriesMetadataStore.from_config(config) as store:
            store.upsert_profile(SeriesProfile(local_path=str(path), canonical_title=title, aliases=[title]))
        return path

    def test_cold_lookup_is_index_only_and_resumes_alias_checkpoint_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            config = self.config(root)
            path = self.profile(config, "Alpha Show")
            stack.enter_context(patch("mikan_matcher.discover_local_series", side_effect=AssertionError("must not scan library")))
            stack.enter_context(patch("mikan_matcher._search_aliases", return_value=["Alpha Show"]))
            search = stack.enter_context(patch("mikan_matcher.search_mikan_bangumi", return_value=[MikanSearchCandidate(101, "Alpha Show", "Alpha Show")]))
            stack.enter_context(patch("mikan_matcher._candidate_confidence", return_value=0.99))
            with patch("mikan_matcher.fetch_bangumi_releases", side_effect=MikanSourceDeadline("local scheduling deadline")):
                self.assertEqual(resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12), [])
            cached = json.loads((root / "auto_matches.json").read_text())
            self.assertEqual(cached[str(path)]["status"], "deferred")
            self.assertEqual(cached[str(path)]["progress"]["next_alias"], 1)
            with patch("mikan_matcher.fetch_bangumi_releases", return_value=[]):
                mappings = resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12)
            self.assertEqual([mapping["bangumi_id"] for mapping in mappings], [101])
            search.assert_called_once()

    def test_partial_candidate_scores_cannot_select_highest_before_remaining_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            config = self.config(root)
            path = self.profile(config, "Ambiguous Show")
            stack.enter_context(patch("mikan_matcher.discover_local_series", side_effect=AssertionError("must not scan library")))
            stack.enter_context(patch("mikan_matcher._search_aliases", return_value=["Ambiguous Show"]))
            search = stack.enter_context(patch("mikan_matcher.search_mikan_bangumi", return_value=[
                MikanSearchCandidate(101, "Ambiguous Show", "Ambiguous Show"),
                MikanSearchCandidate(102, "Ambiguous Show", "Ambiguous Show"),
            ]))
            stack.enter_context(patch("mikan_matcher._candidate_confidence", return_value=0.95))
            with patch("mikan_matcher.fetch_bangumi_releases", side_effect=[[], MikanSourceDeadline("deadline")]):
                self.assertEqual(resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12), [])
            with patch("mikan_matcher.fetch_bangumi_releases", return_value=[]) as fetch:
                self.assertEqual(resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12), [])
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[1], 102)
            search.assert_called_once()
            self.assertEqual(json.loads((root / "auto_matches.json").read_text())[str(path)]["reason"], "ambiguous_candidates")

    def test_existing_worker_discovers_a_later_profile_without_cache_freeze_or_media_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            config = self.config(root)
            self.profile(config, "Alpha Show")
            worker = MikanWorker(config, logging.getLogger("test"))
            stack.enter_context(patch("mikan_matcher.discover_local_series", side_effect=AssertionError("must not scan library")))
            stack.enter_context(patch("mikan_matcher._search_aliases", side_effect=lambda series: [series.aliases[0]]))
            stack.enter_context(patch("mikan_matcher.search_mikan_bangumi", side_effect=lambda _url, title, **kw: [
                MikanSearchCandidate(101 if title == "Alpha Show" else 201, title, title)]))
            stack.enter_context(patch("mikan_matcher.fetch_bangumi_releases", return_value=[]))
            stack.enter_context(patch("mikan_matcher._candidate_confidence", return_value=0.99))
            self.assertEqual({row["bangumi_id"] for row in worker._series_mappings(deadline_monotonic=10**12)}, {101})
            self.profile(config, "Beta Show")
            self.assertEqual({row["bangumi_id"] for row in worker._series_mappings(deadline_monotonic=10**12)}, {101, 201})

    def test_failed_earlier_rss_is_not_skipped_or_turned_into_winner_on_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            config = self.config(root)
            path = self.profile(config, "Retry Show")
            stack.enter_context(patch("mikan_matcher._search_aliases", return_value=["Retry Show"]))
            stack.enter_context(patch("mikan_matcher.search_mikan_bangumi", return_value=[
                MikanSearchCandidate(101, "Retry Show", "Retry Show"), MikanSearchCandidate(102, "Retry Show", "Retry Show"),
            ]))
            stack.enter_context(patch("mikan_matcher._candidate_confidence", return_value=0.99))
            with patch("mikan_matcher.fetch_bangumi_releases", side_effect=[requests.Timeout("source temporary failure"), []]):
                self.assertEqual(resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12), [])
            cached = json.loads((root / "auto_matches.json").read_text())[str(path)]
            self.assertEqual(cached["status"], "deferred")
            self.assertEqual(cached["reason"], "source_lookup_failed")
            self.assertEqual(cached["progress"]["next_candidate"], 0)
            with patch("mikan_matcher.fetch_bangumi_releases", return_value=[]) as fetch:
                self.assertEqual(resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12), [])
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[1], 101)
            self.assertEqual(json.loads((root / "auto_matches.json").read_text())[str(path)]["reason"], "ambiguous_candidates")

    def test_alias_outage_is_not_cached_as_no_candidates_and_retries_only_unverified_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            config = self.config(root)
            path = self.profile(config, "Retry Show")
            stack.enter_context(patch("mikan_matcher._search_aliases", return_value=["First Alias", "Second Alias"]))
            with patch("mikan_matcher.search_mikan_bangumi", side_effect=[requests.Timeout("temporary source outage"), []]):
                self.assertEqual(resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12), [])
            cached = json.loads((root / "auto_matches.json").read_text())[str(path)]
            self.assertEqual(cached["status"], "deferred")
            self.assertEqual(cached["progress"]["next_alias"], 0)
            with patch("mikan_matcher.search_mikan_bangumi", return_value=[]) as search:
                self.assertEqual(resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=10**12), [])
            search.assert_called_once()
            self.assertEqual(search.call_args.args[1], "First Alias")
            self.assertEqual(json.loads((root / "auto_matches.json").read_text())[str(path)]["reason"], "no_candidates")

    def test_cold_alias_timeouts_share_total_deadline_instead_of_eight_full_timeouts(self):
        with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
            root = Path(temp_dir)
            config = self.config(root)
            path = self.profile(config, "Slow Show")
            now = [0.0]
            timeouts = []

            def timeout(*_args, **kwargs):
                timeouts.append(kwargs["timeout"])
                now[0] += kwargs["timeout"]
                raise requests.Timeout("isolated slow alias endpoint")

            stack.enter_context(patch("mikan_matcher.time.monotonic", side_effect=lambda: now[0]))
            stack.enter_context(patch("mikan_matcher._search_aliases", return_value=[f"Alias {i}" for i in range(8)]))
            stack.enter_context(patch("mikan_matcher.requests.get", side_effect=timeout))
            resolve_mikan_series_mappings(config, logging.getLogger("test"), deadline_monotonic=45.0)
            self.assertEqual(timeouts, [30, 15])
            self.assertEqual(now[0], 45)
            self.assertEqual(json.loads((root / "auto_matches.json").read_text())[str(path)]["status"], "deferred")
