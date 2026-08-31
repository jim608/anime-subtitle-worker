from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from contextlib import closing
import io
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
import json
import logging
import os
import tempfile
import threading
import time
import unittest

import requests

from control_state import enqueue_command, get_review_item, upsert_review_item
from lock import VideoLock
from mikan_fallback_sources import FallbackSearchResult
from mikan_source import MikanRelease
from mikan_worker import (
    MikanExtractResult,
    MikanReplacementTarget,
    MikanWorker,
    MikanWorkerError,
    _claim_mikan_extract_jobs,
    _choose_release_for_episode,
    _completed_torrent_has_local_episode,
    _claim_mikan_job,
    _completed_torrent_outputs_complete,
    _ensure_mikan_state_db_for_pending,
    _fallback_video_files_for_torrent,
    _finish_mikan_extract_job,
    _library_scan_series_mappings,
    _load_qbit_unhealthy_since,
    _mark_deferred,
    _mark_active_pending_entry_extract_failed,
    _mark_no_candidate_retry,
    _mark_pending,
    _missing_episodes_for_bangumi,
    _mikan_state_connect,
    _resolve_untracked_torrent_targets,
    _no_candidate_retry_active,
    _pending_failed_urls,
    _pending_extract_priority,
    _pending_release_years,
    _pending_entry_matches_completed_torrent,
    _pending_failed_info_hashes,
    _queue_tags,
    _refresh_mikan_episode_index,
    _save_pending,
    _save_qbit_unhealthy_since,
    _save_json_atomic,
    _select_source_videos_for_pending_episodes,
    _season_number_from_release_title,
    _season_number_from_text,
    _sonarr_style_library_roots,
    _sonarr_style_known_title_alias_variants,
    _sonarr_style_select_target_from_candidates,
    _sync_pending_entry_qbit_progress,
    _review_source_time_fields,
    _target_has_required_chinese_subtitles,
    _target_video_for_torrent_source,
    _update_mikan_extract_job_progress,
    _upsert_mikan_extract_jobs,
    request_mikan_extract_cancel,
    request_mikan_redownload_cancel,
    reconcile_target_ambiguity_review_sources,
    resume_target_ambiguity_source,
    requeue_target_ambiguity_jobs,
    restore_target_ambiguity_pending_entries,
    requeue_mikan_extract_job,
    requeue_interrupted_mikan_extract_jobs,
    requeue_failed_mikan_extract_jobs,
)
from qbit_client import QBitError, QBitTorrent, QBitTorrentFile


class MikanWorkerPendingTest(unittest.TestCase):
    def _source_review_fixture(
        self,
        root: Path,
        *,
        source_exists: bool = False,
        recoverable_url: str = "",
        command_active: bool = False,
    ) -> tuple[SimpleNamespace, QBitTorrent, str]:
        download_root = root / "downloads"
        download_root.mkdir(parents=True)
        source = download_root / "Review Show - S01E01.mkv"
        if source_exists:
            source.write_bytes(b"video")
        config = _mikan_process_config(root, download_root)
        torrent_hash = "a" * 40
        torrent = QBitTorrent(
            hash=torrent_hash,
            name="[Group] Review Show - 01 [SRTx2]",
            progress=1.0,
            state="uploading",
            dlspeed=0,
            downloaded=100,
            added_on=1_700_000_100,
            content_path=str(source),
            save_path=str(download_root),
            category="llm-sub",
            tags="mikansub",
        )
        pending_entry = {
            "bangumi_id": 123,
            "episode": 1,
            "last_failed_info_hash": torrent_hash,
        }
        if recoverable_url:
            pending_entry["last_failed_torrent_url"] = recoverable_url
        _upsert_mikan_extract_jobs(
            config,
            [(torrent, [pending_entry], 1, False)],
            state_required=True,
        )
        with closing(_mikan_state_connect(config)) as connection:
            connection.execute(
                """
                UPDATE mikan_extract_jobs
                SET status='terminal_failed', result_json=?
                WHERE torrent_hash=?
                """,
                ('{"failure_reason":"target_ambiguity"}', torrent_hash),
            )
            connection.commit()
        review_id = upsert_review_item(
            config,
            kind="target_ambiguity",
            target_key=f"hash:{torrent_hash}",
            summary="Review Show target requires review",
            diagnosis={
                "torrent_hash": torrent_hash,
                "torrent_name": torrent.name,
                "source_video": str(source),
                "bangumi_ids": [123],
            },
            candidates=[],
        )
        if command_active:
            enqueue_command(
                config,
                action="review.resolve_target",
                target=review_id,
                parameters={"review_id": review_id},
                idempotency_key=f"test-{review_id}",
            )
        return config, torrent, review_id

    def test_target_review_source_lifecycle_tracks_qbit_source_and_redownload(self) -> None:
        cases = [
            ("qbit", False, "", True, "qbit_present"),
            ("source", True, "", False, "source_files_present"),
            (
                "redownload",
                False,
                "https://mikanani.me/Download/20260718/review.torrent",
                False,
                "redownload_available",
            ),
        ]
        for name, source_exists, recoverable_url, qbit_present, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config, torrent, review_id = self._source_review_fixture(
                    root,
                    source_exists=source_exists,
                    recoverable_url=recoverable_url,
                )

                summary = reconcile_target_ambiguity_review_sources(
                    config,
                    [torrent] if qbit_present else [],
                    missing_grace_seconds=0,
                )

                review = get_review_item(config, review_id)
                self.assertEqual(review["status"], "open")
                self.assertEqual(review["diagnosis"]["source_lifecycle"], expected)
                self.assertEqual(summary[expected], 1)
                self.assertEqual(summary["resolved"], 0)

    def test_target_review_source_lifecycle_auto_closes_unrecoverable_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, _torrent, review_id = self._source_review_fixture(root)

            summary = reconcile_target_ambiguity_review_sources(
                config,
                [],
                now_timestamp=1000,
                missing_grace_seconds=0,
            )

            review = get_review_item(config, review_id)
            self.assertEqual(summary["resolved"], 1)
            self.assertEqual(review["status"], "resolved")
            self.assertEqual(review["diagnosis"]["source_lifecycle"], "source_gone")
            self.assertEqual(review["resolution"]["reason"], "source_gone")
            self.assertTrue(review["resolution"]["automatic"])

    def test_target_review_source_lifecycle_requires_persistent_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, _torrent, review_id = self._source_review_fixture(root)
            original_updated_at = get_review_item(config, review_id)["updated_at"]

            first = reconcile_target_ambiguity_review_sources(
                config,
                [],
                now_timestamp=1000,
                missing_grace_seconds=60,
            )
            pending_updated_at = get_review_item(config, review_id)["updated_at"]
            second = reconcile_target_ambiguity_review_sources(
                config,
                [],
                now_timestamp=1059,
                missing_grace_seconds=60,
            )
            third = reconcile_target_ambiguity_review_sources(
                config,
                [],
                now_timestamp=1061,
                missing_grace_seconds=60,
            )

            review = get_review_item(config, review_id)
            self.assertEqual(first["source_unavailable_pending"], 1)
            self.assertEqual(second["source_unavailable_pending"], 1)
            self.assertEqual(third["resolved"], 1)
            self.assertEqual(pending_updated_at, original_updated_at)
            self.assertEqual(review["status"], "resolved")
            self.assertGreater(review["updated_at"], original_updated_at)

    def test_target_review_source_lifecycle_does_not_close_active_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, _torrent, review_id = self._source_review_fixture(
                root,
                command_active=True,
            )

            summary = reconcile_target_ambiguity_review_sources(
                config,
                [],
                missing_grace_seconds=0,
            )

            review = get_review_item(config, review_id)
            self.assertEqual(summary["resolved"], 0)
            self.assertEqual(review["status"], "open")
            self.assertEqual(review["diagnosis"]["source_lifecycle"], "processing")

    def test_reviewed_target_redownload_rehydrates_pending_and_waits_for_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            recoverable_url = "https://mikanani.me/Download/20260718/review.torrent"
            config, torrent, review_id = self._source_review_fixture(
                root,
                recoverable_url=recoverable_url,
            )
            review = get_review_item(config, review_id)
            qbit = Mock()
            qbit.list_torrents.return_value = []

            result = resume_target_ambiguity_source(
                config,
                bangumi_id=123,
                torrent_hash=torrent.hash,
                diagnosis=review["diagnosis"],
                qbit=qbit,
            )

            self.assertEqual(result["mode"], "redownload_queued")
            self.assertEqual(result["restored_pending"], 1)
            self.assertEqual(result["waiting_download"], 1)
            qbit.ensure_category.assert_called_once_with("llm-sub", save_path=None)
            qbit.add_url.assert_called_once_with(
                recoverable_url,
                save_path=None,
                category="llm-sub",
                tags=["mikansub", "mikan"],
                paused=False,
            )
            with closing(_mikan_state_connect(config)) as connection:
                status = connection.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE torrent_hash=?",
                    (torrent.hash,),
                ).fetchone()[0]
            self.assertEqual(status, "waiting_download")
            pending = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            restored_entry = pending["items"]["123:1"]
            self.assertEqual(restored_entry["torrent_url"], recoverable_url)
            self.assertEqual(restored_entry["info_hash"], torrent.hash)

    def test_reviewed_target_source_in_qbit_requeues_without_duplicate_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, torrent, review_id = self._source_review_fixture(root)
            review = get_review_item(config, review_id)
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]

            result = resume_target_ambiguity_source(
                config,
                bangumi_id=123,
                torrent_hash=torrent.hash,
                diagnosis=review["diagnosis"],
                qbit=qbit,
            )

            self.assertEqual(result["mode"], "qbit_present")
            self.assertEqual(result["requeued"], 1)
            qbit.add_url.assert_not_called()
            with closing(_mikan_state_connect(config)) as connection:
                status = connection.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE torrent_hash=?",
                    (torrent.hash,),
                ).fetchone()[0]
            self.assertEqual(status, "queued")

    def test_reviewed_target_without_source_or_url_refuses_to_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, torrent, review_id = self._source_review_fixture(root)
            review = get_review_item(config, review_id)
            qbit = Mock()
            qbit.list_torrents.return_value = []

            with self.assertRaisesRegex(MikanWorkerError, "no exact redownload URL"):
                resume_target_ambiguity_source(
                    config,
                    bangumi_id=123,
                    torrent_hash=torrent.hash,
                    diagnosis=review["diagnosis"],
                    qbit=qbit,
                )

            qbit.add_url.assert_not_called()
            with closing(_mikan_state_connect(config)) as connection:
                status = connection.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE torrent_hash=?",
                    (torrent.hash,),
                ).fetchone()[0]
            self.assertEqual(status, "terminal_failed")

    def test_reviewed_target_redownload_failure_keeps_extract_job_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config, torrent, review_id = self._source_review_fixture(
                root,
                recoverable_url="https://mikanani.me/Download/20260718/review.torrent",
            )
            review = get_review_item(config, review_id)
            qbit = Mock()
            qbit.list_torrents.return_value = []
            qbit.add_url.side_effect = QBitError("qB add rejected")

            with self.assertRaisesRegex(QBitError, "qB add rejected"):
                resume_target_ambiguity_source(
                    config,
                    bangumi_id=123,
                    torrent_hash=torrent.hash,
                    diagnosis=review["diagnosis"],
                    qbit=qbit,
                )

            with closing(_mikan_state_connect(config)) as connection:
                status = connection.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE torrent_hash=?",
                    (torrent.hash,),
                ).fetchone()[0]
            self.assertEqual(status, "terminal_failed")

    def test_review_source_times_keep_release_and_qbit_times_distinct(self) -> None:
        torrent = QBitTorrent(
            hash="a" * 40,
            name="[Group] Test - 01",
            progress=1.0,
            state="uploading",
            dlspeed=0,
            downloaded=100,
            added_on=1_700_000_100,
            content_path=None,
            save_path=None,
            category="llm-sub",
            tags="mikansub",
            completion_on=1_700_000_900,
            creation_date=1_699_999_900,
        )

        fields = _review_source_time_fields(
            torrent,
            [{
                "source": "mikan",
                "pub_date": "2023-11-14T22:13:20+00:00",
            }],
        )

        self.assertEqual(fields, {
            "source_published_at": 1_700_000_000.0,
            "source_published_precision": "time",
            "torrent_created_at": 1_699_999_900.0,
            "torrent_added_at": 1_700_000_100.0,
            "torrent_completed_at": 1_700_000_900.0,
        })

    def test_review_source_times_do_not_label_recovered_completion_as_publication(self) -> None:
        torrent = QBitTorrent(
            hash="b" * 40,
            name="[Group] Recovered - 01",
            progress=1.0,
            state="uploading",
            dlspeed=0,
            downloaded=100,
            added_on=1_700_000_100,
            content_path=None,
            save_path=None,
            category="llm-sub",
            tags="mikansub",
            completion_on=1_700_000_900,
        )

        fields = _review_source_time_fields(
            torrent,
            [{
                "source": "qbit-recovered",
                "pub_date": "2023-11-14T22:28:20+00:00",
            }],
        )

        self.assertEqual(fields["source_published_at"], 0.0)
        self.assertEqual(fields["torrent_completed_at"], 1_700_000_900.0)

    def test_recovered_completion_time_is_not_used_as_release_year(self) -> None:
        years = _pending_release_years([{
            "source": "qbit-recovered",
            "torrent_url": "qbit://hash",
            "pub_date": "2026-07-16T00:00:00+00:00",
        }])

        self.assertEqual(years, set())

    def test_mikan_torrent_url_date_is_a_publication_day_fallback(self) -> None:
        years = _pending_release_years([{
            "source": "mikan",
            "torrent_url": "https://mikanani.me/Download/20250913/abcdef.torrent",
            "pub_date": "",
        }])

        self.assertEqual(years, {2025})

    def test_atomic_json_writes_use_independent_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            destination = root / "shared.json"
            writer_count = 8
            barrier = threading.Barrier(writer_count)

            def write_payload(index: int) -> None:
                barrier.wait()
                _save_json_atomic(destination, {"writer": index})

            with ThreadPoolExecutor(max_workers=writer_count) as executor:
                list(executor.map(write_payload, range(writer_count)))

            payload = json.loads(destination.read_text(encoding="utf-8"))
            self.assertIn(payload["writer"], range(writer_count))
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_extract_progress_persists_current_file_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            source = download_root / "Show - S02E01.mkv"
            source.write_bytes(b"video")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="file-time-hash",
                name="[Group] Show S2 - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=5,
                added_on=None,
                content_path=str(source),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1}], 1, True)]
            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]

            self.assertTrue(
                _update_mikan_extract_job_progress(
                    config,
                    job,
                    processed=0,
                    total=1,
                    current=str(source),
                )
            )

            with closing(sqlite3.connect(root / "mikan_state.sqlite3")) as connection:
                stored = connection.execute(
                    """
                    SELECT target_path, current_file_timestamp,
                           current_file_time_kind, current_file_size
                    FROM mikan_extract_jobs WHERE job_key=?
                    """,
                    (job.job_key,),
                ).fetchone()
            self.assertEqual(stored[0], str(source))
            self.assertGreater(stored[1], 0)
            self.assertIn(stored[2], {"created", "modified"})
            self.assertEqual(stored[3], 5)

    def test_scoped_sonarr_search_never_readds_whole_anime_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            anime_root = Path(temp_dir) / "anime"
            series_root = anime_root / "No-Rin"
            series_root.mkdir(parents=True)

            roots = _sonarr_style_library_roots(
                SimpleNamespace(input_path=anime_root),
                [{"bangumi_id": 260, "path": str(series_root), "match": ["No-Rin"]}],
            )

            self.assertEqual(roots, [series_root])
            self.assertNotIn(anime_root, roots)

    def test_unnamed_memory_season_mapping_never_falls_back_to_same_episode_in_season_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series = root / "Unnamed Memory"
            season1 = series / "Season 1"
            season2 = series / "Season 2"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            first = season1 / "Unnamed Memory - S01E01 - WEBDL-1080p.mkv"
            second = season2 / "Unnamed Memory - S02E01 - WEBDL-1080p.mkv"
            first.write_bytes(b"video")
            second.write_bytes(b"video")
            config = SimpleNamespace(video_extensions=[".mkv"])
            torrent = QBitTorrent(
                hash="unnamed-memory-s2",
                name="[Group] Unnamed Memory Act.2 - 01 [CHT].mkv",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=1,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            pending = [{"bangumi_id": 4102, "episode": 1, "title": torrent.name}]
            mappings = [
                {"bangumi_id": 4102, "season": 2, "path": str(series), "match": ["Unnamed Memory"]},
                {"bangumi_id": 9999, "season": 1, "path": str(season1), "match": ["Other"]},
            ]

            selected = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                mappings,
                pending_entries=pending,
            )

            self.assertEqual(selected, [second])

    def test_quintessential_quintuplets_bangumi_scope_excludes_other_season_same_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            season1 = root / "The Quintessential Quintuplets" / "Season 1"
            season2 = root / "The Quintessential Quintuplets" / "Season 2"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            first = season1 / "The Quintessential Quintuplets - S01E03 - Bluray-1080p.mkv"
            second = season2 / "The Quintessential Quintuplets - S02E03 - HDTV-1080p.mkv"
            first.write_bytes(b"video")
            second.write_bytes(b"video")
            config = SimpleNamespace(video_extensions=[".mkv"])
            torrent = QBitTorrent(
                hash="quintuplets-s2",
                name="[Group] 5-toubun no Hanayome S2 - 03 [CHS&CHT].mkv",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=1,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            pending = [{"bangumi_id": 2502, "episode": 3, "title": torrent.name}]
            mappings = [
                {"bangumi_id": 1501, "season": 1, "path": str(season1), "match": ["5-toubun no Hanayome"]},
                {"bangumi_id": 2502, "season": 2, "path": str(season2), "match": ["5-toubun no Hanayome S2"]},
            ]

            selected = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                mappings,
                pending_entries=pending,
            )

            self.assertEqual(selected, [second])

    def test_no_candidate_retry_uses_bounded_exponential_backoff_and_resets_on_selection(self) -> None:
        pending = {"items": {}}

        delays = [
            _mark_no_candidate_retry(pending, 123, 1, 600, 86400)
            for _ in range(5)
        ]

        self.assertEqual(delays, [600, 3600, 21600, 86400, 86400])
        entry = pending["items"]["123:1"]
        self.assertEqual(entry["no_candidate_retry_count"], 4)
        self.assertEqual(entry["no_candidate_retry_seconds"], 86400)

        _mark_pending(pending, _release("https://mikan/found-after-backoff.torrent"))

        entry = pending["items"]["123:1"]
        self.assertNotIn("no_candidate_at", entry)
        self.assertNotIn("no_candidate_until", entry)
        self.assertNotIn("no_candidate_retry_count", entry)
        self.assertNotIn("no_candidate_retry_seconds", entry)

    def test_release_title_season_parser_ignores_episode_after_title_season_word(self) -> None:
        self.assertIsNone(_season_number_from_release_title("Monogatari Series Off Monster Season 06.mkv"))
        self.assertEqual(_season_number_from_release_title("Kaguya-sama S2 - 06.mkv"), 2)
        self.assertEqual(_season_number_from_release_title("Example 3rd Season - 05.mkv"), 3)

    def test_queue_tags_include_normalized_release_source(self) -> None:
        self.assertEqual(_queue_tags(["mikansub"], "nyaa"), ["mikansub", "nyaa"])
        self.assertEqual(_queue_tags(["mikansub"], "animegarden:dmhy"), ["mikansub", "animegarden"])

    def test_single_episode_candidate_without_title_evidence_is_never_auto_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "No-Rin" / "Season 1" / "No-Rin - S01E01.mkv"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"")
            diagnostics: list[dict] = []

            selected = _sonarr_style_select_target_from_candidates(
                [candidate],
                {"bofuri 2"},
                torrent_name="[LoliHouse] Bofuri 2 [01-12]",
                source_video=None,
                logger=None,
                context="review safety test",
                season_hint=1,
                pending_entries=[],
                diagnostics=diagnostics,
                trusted_single_candidate=True,
            )

            self.assertEqual(selected, [])
            self.assertTrue(any(item.get("reason") == "low_confidence_target_candidate" for item in diagnostics))

    def test_near_air_release_rejects_candidates_from_impossible_known_years(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            candidates: list[Path] = []
            for season, year in ((1, 2017), (2, 2019), (3, 2020)):
                season_dir = root / "BanG Dream" / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / "season.nfo").write_text(
                    f"<season><year>{year}</year></season>",
                    encoding="utf-8",
                )
                candidate = season_dir / f"BanG Dream - S{season:02d}E09.mkv"
                candidate.write_bytes(b"")
                candidates.append(candidate)
            diagnostics: list[dict] = []

            selected = _sonarr_style_select_target_from_candidates(
                candidates,
                {"bangdreamavemujica"},
                torrent_name="[Group] BanG Dream! Ave Mujica - 09 [WebRip 1080p]",
                source_video=None,
                logger=None,
                context="release year safety test",
                season_hint=None,
                pending_entries=[{
                    "source": "mikan",
                    "torrent_url": "https://example.invalid/ave-09.torrent",
                    "pub_date": "2025-03-07T00:00:00+00:00",
                    "title": "BanG Dream! Ave Mujica - 09 [WebRip 1080p]",
                }],
                diagnostics=diagnostics,
                trusted_mapping_scope=True,
            )

            self.assertEqual(selected, [])
            conflict = next(item for item in diagnostics if item.get("reason") == "release_year_conflict")
            self.assertEqual(conflict["release_years"], [2025])
            self.assertEqual(conflict["candidate_years"], [2017, 2019, 2020])

    def test_worker_init_does_not_rebuild_pending_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            pending = {"items": {}}
            _mark_pending(pending, _release("https://mikan/source.torrent"))
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            with patch("mikan_worker._sync_mikan_state_db") as sync_state_db:
                MikanWorker(config, _logger())

            sync_state_db.assert_not_called()

    def test_review_requeue_is_scoped_to_exact_torrent_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            connection = _mikan_state_connect(config)
            try:
                connection.executemany(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, torrent_hash, bangumi_ids_json, result_json,
                        created_at, updated_at
                    ) VALUES (?, 'terminal_failed', ?, '[2402]', ?, 1, 1)
                    """,
                    [
                        ("hash:chosen", "a" * 40, '{"failure_reason":"target_ambiguity"}'),
                        ("hash:other", "b" * 40, '{"failure_reason":"target_ambiguity"}'),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            changed = requeue_target_ambiguity_jobs(
                config,
                bangumi_id=2402,
                torrent_hash="A" * 40,
            )

            connection = _mikan_state_connect(config)
            try:
                statuses = dict(connection.execute(
                    "SELECT job_key, status FROM mikan_extract_jobs ORDER BY job_key"
                ).fetchall())
            finally:
                connection.close()
            self.assertEqual(changed, 1)
            self.assertEqual(statuses["hash:chosen"], "queued")
            self.assertEqual(statuses["hash:other"], "terminal_failed")

    def test_review_pending_restore_is_scoped_to_exact_torrent_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            pending_path = root / "mikan_pending.json"
            pending = {
                "items": {
                    "2402:1": {
                        "bangumi_id": 2402,
                        "last_extract_failure_reason": "target_ambiguity",
                        "last_failed_torrent_url": "https://example/chosen.torrent",
                        "last_failed_title": "chosen",
                        "last_failed_info_hash": "a" * 40,
                    },
                    "2402:2": {
                        "bangumi_id": 2402,
                        "last_extract_failure_reason": "target_ambiguity",
                        "last_failed_torrent_url": "https://example/other.torrent",
                        "last_failed_title": "other",
                        "last_failed_info_hash": "b" * 40,
                    },
                }
            }
            _save_pending(pending_path, pending)

            changed = restore_target_ambiguity_pending_entries(
                config,
                bangumi_id=2402,
                torrent_hash="A" * 40,
            )

            restored = json.loads(pending_path.read_text(encoding="utf-8"))["items"]
            self.assertEqual(changed, 1)
            self.assertEqual(restored["2402:1"]["torrent_url"], "https://example/chosen.torrent")
            self.assertNotIn("last_extract_failure_reason", restored["2402:1"])
            self.assertNotIn("torrent_url", restored["2402:2"])
            self.assertEqual(restored["2402:2"]["last_extract_failure_reason"], "target_ambiguity")

    def test_repairs_legacy_movie_part_pending_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            pending_path = root / str(config.mikan_pending_path)
            pending = {
                "items": {
                    "409:2": {
                        "bangumi_id": 409,
                        "episode": 2,
                        "episodes": [2],
                        "title": "The.Seven.Deadly.Sins.Grudge.of.Edinburgh.Part.2.2023.1080p.mkv",
                        "torrent_url": "https://example.invalid/movie-part-2.torrent",
                        "queued_at": "2026-07-13T00:00:00+00:00",
                    }
                }
            }
            _save_pending(pending_path, pending)
            worker = MikanWorker(config, _logger())

            self.assertEqual(worker._repair_invalid_release_part_pending_entries(), 1)
            repaired = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertNotIn("409:2", repaired["items"])
            self.assertEqual(worker._repair_invalid_release_part_pending_entries(), 0)

    def test_claim_terminalizes_legacy_movie_part_extract_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            entry = {
                "bangumi_id": 409,
                "episode": 2,
                "episodes": [2],
                "title": "The.Seven.Deadly.Sins.Grudge.of.Edinburgh.Part.2.2023.1080p.mkv",
            }
            torrent = QBitTorrent(
                hash="moviehash",
                name=entry["title"],
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "downloads" / entry["title"]),
                save_path=str(root / "downloads"),
                category="llm-sub",
                tags="mikansub",
            )
            self.assertEqual(
                _upsert_mikan_extract_jobs(
                    config,
                    [(torrent, [entry], 1, False)],
                    state_required=True,
                ),
                1,
            )

            self.assertEqual(_claim_mikan_extract_jobs(config, limit=1), [])
            conn = _mikan_state_connect(config)
            try:
                status, result_json = conn.execute(
                    "SELECT status, result_json FROM mikan_extract_jobs WHERE torrent_hash = ?",
                    (torrent.hash,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "terminal_failed")
            self.assertEqual(json.loads(result_json)["failure_reason"], "invalid_episode_metadata")

    def test_episode_index_refresh_skips_fresh_existing_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_episode_index_ttl_seconds = 1800
            conn = _mikan_state_connect(config)
            try:
                now = time.time()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO anime_episode_index(
                        bangumi_id, episode, season, path, series_path, updated_at
                    )
                    VALUES (123, 1, 1, ?, ?, ?)
                    """,
                    (str(root / "anime" / "Example - S01E01.mkv"), str(root / "anime"), now),
                )
                conn.execute(
                    """
                    INSERT INTO mikan_state_meta(key, value, updated_at)
                    VALUES ('episode_index_refreshed_at', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (str(now), now),
                )
                conn.commit()
            finally:
                conn.close()

            with patch("mikan_worker._refresh_mikan_episode_index") as refresh:
                MikanWorker(config, _logger())._ensure_episode_index([{"bangumi_id": 123, "path": root / "anime"}])

            refresh.assert_not_called()

    def test_episode_index_stale_check_never_recursively_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_episode_index_ttl_seconds = 1800
            conn = _mikan_state_connect(config)
            try:
                stale = time.time() - 7200
                conn.execute(
                    """
                    INSERT OR REPLACE INTO anime_episode_index(
                        bangumi_id, episode, season, path, series_path, updated_at
                    )
                    VALUES (123, 1, 1, ?, ?, ?)
                    """,
                    (str(root / "anime" / "Example - S01E01.mkv"), str(root / "anime"), stale),
                )
                conn.execute(
                    """
                    INSERT INTO mikan_state_meta(key, value, updated_at)
                    VALUES ('episode_index_refreshed_at', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (str(stale), stale),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("mikan_worker._refresh_mikan_episode_index") as refresh,
                patch(
                    "mikan_worker._find_video_files",
                    side_effect=AssertionError("readiness check must not recurse"),
                ),
            ):
                MikanWorker(config, _logger())._ensure_episode_index([{"bangumi_id": 123, "path": root / "anime"}])

            refresh.assert_not_called()

    def test_library_scan_plan_bounds_and_cools_index_unavailable_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_library_fallback_scan_interval_seconds = 3600
            config.mikan_library_fallback_scan_max_series_per_cycle = 2
            logger = Mock()
            worker = MikanWorker(config, logger)
            worker._ensure_episode_index = Mock()
            mappings = [
                {"bangumi_id": index, "path": root / f"Series-{index}"}
                for index in range(1, 6)
            ]

            refresh_lock = Mock()
            refresh_lock.acquire.return_value = True
            with (
                patch(
                    "mikan_worker._mikan_episode_index_covers_mappings",
                    side_effect=[False, False, False],
                ),
                patch("mikan_worker._mikan_episode_index_due_mappings", return_value=mappings),
                patch("mikan_worker._mikan_episode_index_reconcile_due", return_value=True),
                patch("mikan_worker._mikan_episode_index_lock", return_value=refresh_lock),
                patch("mikan_worker._refresh_mikan_episode_index", return_value=2) as refresh,
                patch("mikan_worker.time.monotonic", side_effect=[100.0, 101.0]),
            ):
                selected, indexed = worker._library_scan_plan(mappings)
                cooled, cooled_indexed = worker._library_scan_plan(mappings)

            self.assertTrue(indexed)
            self.assertFalse(cooled_indexed)
            self.assertEqual(len(selected), 2)
            self.assertEqual(cooled, [])
            self.assertEqual(len(refresh.call_args.args[2]), 2)
            refresh_lock.release.assert_called_once_with()
            self.assertEqual(worker._fallback_library_scan_runs, 1)
            self.assertEqual(worker._fallback_library_scan_roots, 2)
            messages = [str(call.args[0]) for call in logger.warning.call_args_list]
            self.assertTrue(any("reason=episode_index_incremental_bounded_reconcile" in message for message in messages))
            self.assertTrue(any("reason=episode_index_unavailable_cooldown" in message for message in messages))
            self.assertFalse(any(str(root) in message for message in messages))

    def test_indexed_missing_episode_scan_never_walks_series_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Season 1" / "Series - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            logger = Mock()

            with (
                patch(
                    "mikan_worker._target_videos_from_episode_index",
                    side_effect=lambda _config, _mappings, episode: [video] if episode == 1 else [],
                ),
                patch(
                    "mikan_worker._find_video_files",
                    side_effect=AssertionError("indexed scan must not recurse through roots"),
                ),
                patch("mikan_worker._has_official_chinese_subtitle", return_value=False),
            ):
                missing = _missing_episodes_for_bangumi(
                    config,
                    logger,
                    123,
                    [{"bangumi_id": 123, "path": root / "Series"}],
                    candidate_episodes={1, 2},
                    episode_index_ready=True,
                )

            self.assertEqual(missing, {1})
            self.assertTrue(
                any(
                    call.args and "reason=episode_index" in str(call.args[0])
                    for call in logger.info.call_args_list
                )
            )

    def test_episode_index_lock_contention_never_scans_or_releases_foreign_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            worker = MikanWorker(config, _logger())
            worker._ensure_episode_index = Mock()
            refresh_lock = Mock()
            refresh_lock.acquire.return_value = False
            mappings = [{"bangumi_id": 123, "path": root / "anime"}]

            with (
                patch("mikan_worker._mikan_episode_index_covers_mappings", return_value=False),
                patch("mikan_worker._mikan_episode_index_due_mappings", return_value=mappings),
                patch("mikan_worker._mikan_episode_index_reconcile_due", return_value=True),
                patch("mikan_worker._mikan_episode_index_lock", return_value=refresh_lock),
                patch("mikan_worker._refresh_mikan_episode_index") as refresh,
            ):
                selected, indexed = worker._library_scan_plan(mappings)

            refresh.assert_not_called()
            refresh_lock.release.assert_not_called()
            self.assertEqual(mappings, selected)
            self.assertFalse(indexed)

    def test_incremental_episode_index_refresh_preserves_unselected_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_root = root / "Series-1"
            second_root = root / "Series-2"
            first_root.mkdir()
            second_root.mkdir()
            first_video = first_root / "Series 1 - S01E01.mkv"
            second_video = second_root / "Series 2 - S01E02.mkv"
            first_video.write_bytes(b"first")
            second_video.write_bytes(b"second")
            config = _mikan_process_config(root, root / "downloads")
            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO anime_episode_index(
                        bangumi_id, episode, season, path, series_path, updated_at
                    ) VALUES (2, 2, 1, ?, ?, ?)
                    """,
                    (str(second_video.resolve()), str(second_root.resolve()), time.time()),
                )
                conn.commit()
            finally:
                conn.close()

            indexed = _refresh_mikan_episode_index(
                config,
                _logger(),
                [{"bangumi_id": 1, "path": first_root}],
            )

            conn = _mikan_state_connect(config)
            try:
                rows = conn.execute(
                    "SELECT bangumi_id, episode FROM anime_episode_index ORDER BY bangumi_id"
                ).fetchall()
                roots = conn.execute(
                    "SELECT bangumi_id, series_path FROM anime_episode_index_roots"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(1, indexed)
            self.assertEqual([(1, 1), (2, 2)], rows)
            self.assertEqual([(1, str(first_root.resolve()))], roots)

    def test_bounded_incremental_reconciliation_rotates_unindexed_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_library_fallback_scan_interval_seconds = 1
            config.mikan_library_fallback_scan_max_series_per_cycle = 2
            config.video_extensions = [".mkv"]
            mappings: list[dict[str, object]] = []
            for index in range(1, 5):
                series_root = root / f"Series-{index}"
                series_root.mkdir()
                (series_root / f"Series {index} - S01E{index:02d}.mkv").write_bytes(b"video")
                mappings.append({"bangumi_id": index, "path": series_root})
            worker = MikanWorker(config, _logger())
            worker._ensure_episode_index = Mock()
            refresh_lock = Mock()
            refresh_lock.acquire.return_value = True

            with (
                patch("mikan_worker._mikan_episode_index_reconcile_due", return_value=True),
                patch("mikan_worker._mikan_episode_index_lock", return_value=refresh_lock),
                patch("mikan_worker.time.monotonic", side_effect=[100.0, 102.0]),
            ):
                first, first_indexed = worker._library_scan_plan(mappings)
                second, second_indexed = worker._library_scan_plan(mappings)

            conn = _mikan_state_connect(config)
            try:
                roots = conn.execute(
                    "SELECT bangumi_id FROM anime_episode_index_roots ORDER BY bangumi_id"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(2, len(first))
            self.assertTrue(first_indexed)
            self.assertEqual(mappings, second)
            self.assertTrue(second_indexed)
            self.assertEqual([(1,), (2,), (3,), (4,)], roots)
            self.assertEqual(4, worker._fallback_library_scan_roots)
            self.assertEqual(2, refresh_lock.release.call_count)

    def test_extract_slot_uses_cached_series_mappings_without_local_catalog_walk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=Mock())
            worker._series_mappings = Mock(return_value=[])
            worker._ensure_episode_index = Mock()

            with patch("mikan_worker._claim_mikan_extract_jobs", return_value=[]):
                self.assertEqual(worker.process_queued_extract_jobs(limit=1), 0)

            worker._series_mappings.assert_called_once_with(cached_only=True)

    def test_qbit_unhealthy_since_persists_in_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)

            _save_qbit_unhealthy_since(config, {"hash123": ("zero speed", 123.5)})

            self.assertEqual(_load_qbit_unhealthy_since(config), {"hash123": ("zero speed", 123.5)})

    def test_non_retryable_failed_extract_job_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="terminal-failed-hash",
                name="[Group] Terminal Failed Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Terminal Failed Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/terminal.torrent"}], 1, True)]

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            _finish_mikan_extract_job(
                config,
                job.job_key,
                "failed",
                MikanExtractResult(0, failure_reason="extract_exception", failure_detail="terminal", retryable=False),
            )

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 0)
            self.assertEqual(_claim_mikan_extract_jobs(config, limit=1), [])
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status, attempts = conn.execute(
                    "SELECT status, attempts FROM mikan_extract_jobs WHERE job_key = ?",
                    (job.job_key,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "terminal_failed")
            self.assertEqual(attempts, 1)

    def test_replaceable_failed_extract_job_is_not_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="replaceable-failed-hash",
                name="[Group] Replaceable Failed Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Replaceable Failed Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/replaceable.torrent"}], 1, True)]

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            _finish_mikan_extract_job(
                config,
                job.job_key,
                "failed",
                MikanExtractResult(
                    0,
                    failure_reason="subtitle_language_not_supported",
                    failure_detail="no usable Chinese",
                    retryable=False,
                ),
            )

            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status, result_json = conn.execute(
                    "SELECT status, result_json FROM mikan_extract_jobs WHERE job_key = ?",
                    (job.job_key,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "replaced")
            self.assertEqual(json.loads(result_json)["failure_bucket"], "no_usable_chinese")

    def test_source_missing_extract_job_is_replaced_and_not_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="source-missing-hash",
                name="[Group] Source Missing Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Source Missing Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/source-missing.torrent"}], 1, True)]

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            _finish_mikan_extract_job(
                config,
                job.job_key,
                "failed",
                MikanExtractResult(
                    0,
                    failure_reason="source_video_missing",
                    failure_detail="Mapped completed torrent path does not exist: /qbit/missing",
                    retryable=False,
                ),
            )

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 0)
            self.assertEqual(_claim_mikan_extract_jobs(config, limit=1), [])
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status = conn.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE job_key = ?",
                    (job.job_key,),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "replaced")

    def test_replaced_extract_job_does_not_requeue_when_pending_is_waiting_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            release = _episode_release(
                "https://mikan/replaced-waiting.torrent",
                "[Group] Replaced Waiting Show - 11 [SRTx2]",
                11,
            )
            pending = {"items": {}}
            _mark_pending(pending, release)
            entry = pending["items"]["123:11"]
            entry["queued_at"] = datetime.now(timezone.utc).isoformat()
            entry["last_progress"] = 1.0
            entry["last_qbit_hash"] = "replaced-waiting-hash"
            torrent = QBitTorrent(
                hash="replaced-waiting-hash",
                name=release.title,
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Replaced Waiting Show - 11.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [entry], 1, False)]

            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, attempts, worker_id, lease_until,
                        torrent_hash, torrent_name, bangumi_ids_json, episodes_json,
                        pending_entries_json, torrent_json, result_json, last_error,
                        created_at, updated_at, started_at, finished_at
                    )
                    VALUES (?, 'replaced', 1, 1, '', 0, ?, ?, '[123]', '[11]', '[]', '{}',
                            '{"retryable": false}', 'old replacement', 10, 20, 15, 20)
                    """,
                    ("hash:replaced-waiting-hash", torrent.hash, torrent.name),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 0)

            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status, attempts, last_error, started_at, finished_at = conn.execute(
                    "SELECT status, attempts, last_error, started_at, finished_at FROM mikan_extract_jobs WHERE job_key = ?",
                    ("hash:replaced-waiting-hash",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "replaced")
            self.assertEqual(attempts, 1)
            self.assertEqual(last_error, "old replacement")
            self.assertEqual(started_at, 15)
            self.assertEqual(finished_at, 20)
            self.assertEqual(_claim_mikan_extract_jobs(config, limit=1), [])

    def test_extract_timeout_job_is_replaceable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="timeout-hash",
                name="[Group] Timeout Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Timeout Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/timeout.torrent"}], 1, True)]

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            _finish_mikan_extract_job(
                config,
                job.job_key,
                "failed",
                MikanExtractResult(
                    0,
                    failure_reason="extract_timeout",
                    failure_detail="Subtitle extraction did not finish within 900s",
                    retryable=False,
                ),
            )

            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status, result_json = conn.execute(
                    "SELECT status, result_json FROM mikan_extract_jobs WHERE job_key = ?",
                    (job.job_key,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "replaced")
            self.assertEqual(json.loads(result_json)["failure_bucket"], "extract_timeout")

    def test_requeue_failed_extracts_also_recovers_expired_running_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="expired-running-hash",
                name="[Group] Expired Running Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Expired Running Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/expired.torrent"}], 1, True)]

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                conn.execute(
                    "UPDATE mikan_extract_jobs SET status = 'running', lease_until = ?, updated_at = ? WHERE job_key = ?",
                    (1.0, 1.0, job.job_key),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(requeue_failed_mikan_extract_jobs(config), 1)
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status, lease_until, started_at, finished_at = conn.execute(
                    "SELECT status, lease_until, started_at, finished_at FROM mikan_extract_jobs WHERE job_key = ?",
                    (job.job_key,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "queued")
            self.assertEqual(lease_until, 0)
            self.assertEqual(started_at, 0)
            self.assertEqual(finished_at, 0)

    def test_worker_startup_recovers_running_extract_jobs_without_waiting_for_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="interrupted-running-hash",
                name="[Group] Interrupted Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Interrupted Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1}], 1, True)]
            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]

            self.assertEqual(requeue_interrupted_mikan_extract_jobs(config), 1)
            recovered = _claim_mikan_extract_jobs(config, limit=1)

            self.assertEqual([item.job_key for item in recovered], [job.job_key])
            self.assertNotEqual(recovered[0].worker_id, job.worker_id)

    def test_extract_submission_failure_returns_every_unsubmitted_job_to_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            config.mikan_extract_workers = 2
            rows = []
            for index in (1, 2):
                torrent = QBitTorrent(
                    hash=f"submit-failure-{index}",
                    name=f"[Group] Submit Failure Show - {index:02d} [CHT]",
                    progress=1.0,
                    state="uploading",
                    dlspeed=0,
                    downloaded=100,
                    added_on=None,
                    content_path=str(download_root / f"Submit Failure Show - {index:02d}.mkv"),
                    save_path=str(download_root),
                    category="llm-sub",
                    tags="mikansub",
                )
                rows.append((torrent, [{"bangumi_id": 123, "episode": index}], index, True))
            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 2)
            jobs = _claim_mikan_extract_jobs(config, limit=2)
            executor = Mock()
            executor.submit.side_effect = RuntimeError("cannot schedule new futures after interpreter shutdown")
            worker = MikanWorker(config, _logger())
            qbit = Mock()
            qbit.list_files.return_value = []
            qbit.torrent_creation_date.return_value = 1_699_999_900

            with patch("mikan_worker.ThreadPoolExecutor", return_value=executor):
                processed = worker._process_claimed_mikan_extract_jobs(qbit, [], jobs)

            self.assertEqual(processed, 0)
            executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                statuses = conn.execute(
                    "SELECT status, torrent_json FROM mikan_extract_jobs ORDER BY job_key"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual([row[0] for row in statuses], ["queued", "queued"])
            self.assertTrue(all(json.loads(row[1])["creation_date"] == 1_699_999_900 for row in statuses))

    def test_official_extract_defers_when_ai_owns_target_video_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            source = download_root / "Locked Show - 01.mkv"
            target = root / "anime" / "Locked Show - S01E01.mkv"
            target.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            target.write_bytes(b"target")
            torrent = QBitTorrent(
                hash="locked-target-hash",
                name="[Group] Locked Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            lock = VideoLock(target)
            self.assertTrue(lock.acquire())
            try:
                result = MikanWorker(config, _logger())._extract_completed_source_to_target(
                    source,
                    target,
                    torrent,
                    [],
                    download_root,
                )
            finally:
                lock.release()

            self.assertEqual(result.failure_reason, "target_video_busy")
            self.assertTrue(result.retryable)
            self.assertEqual(result.defer_seconds, 10)

    def test_completed_torrent_reports_progress_after_each_successful_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            source_one = download_root / "Progress Show - 01.mkv"
            source_two = download_root / "Progress Show - 02.mkv"
            source_one.write_bytes(b"source-one")
            source_two.write_bytes(b"source-two")
            target_one = root / "anime" / "Progress Show - S01E01.mkv"
            target_two = root / "anime" / "Progress Show - S01E02.mkv"
            target_one.parent.mkdir(parents=True)
            target_one.write_bytes(b"target-one")
            target_two.write_bytes(b"target-two")
            torrent = QBitTorrent(
                hash="progress-report-hash",
                name="[Group] Progress Show - 01-02 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            progress_updates: list[tuple[int, int, str]] = []
            target_by_source = {
                source_one: target_one,
                source_two: target_two,
            }
            worker = MikanWorker(config, _logger())

            with (
                patch(
                    "mikan_worker._target_video_for_torrent_source",
                    side_effect=lambda source, *_args, **_kwargs: target_by_source[source],
                ),
                patch.object(
                    worker,
                    "_extract_completed_source_to_target",
                    return_value=MikanExtractResult(1),
                ),
            ):
                result = worker._extract_completed_torrent(
                    torrent,
                    [],
                    pending_episodes={1, 2},
                    pending_entries=[{"bangumi_id": 123, "episode": 1}, {"bangumi_id": 123, "episode": 2}],
                    progress_callback=lambda processed, total, current: progress_updates.append(
                        (processed, total, current)
                    ),
                )

            self.assertEqual(result.extracted_count, 2)
            self.assertEqual(
                [(processed, total) for processed, total, _current in progress_updates],
                [(0, 2), (0, 2), (1, 2), (1, 2), (2, 2)],
            )
            self.assertEqual(progress_updates[-1][2], str(source_two))

    def test_required_chinese_sidecar_check_never_classifies_video_or_quality_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Large Episode.mkv"
            quality = root / "Large Episode.zh-CN.ass.quality.json"
            simplified = root / "Large Episode.zh-CN.ass"
            traditional = root / "Large Episode.zh-TW.ass"
            video.write_bytes(b"video")
            quality.write_text("{}", encoding="utf-8")
            simplified.write_text("simplified", encoding="utf-8")
            traditional.write_text("traditional", encoding="utf-8")

            def classify(path: Path) -> str | None:
                if path == simplified:
                    return "zh-cn"
                if path == traditional:
                    return "zh-tw"
                raise AssertionError(f"non-subtitle path was classified: {path}")

            with patch("mikan_worker.classify_sidecar_subtitle_language", side_effect=classify) as classifier:
                self.assertTrue(_target_has_required_chinese_subtitles(video))

            self.assertEqual(
                {call.args[0] for call in classifier.call_args_list},
                {simplified, traditional},
            )

    def test_claim_expired_running_extract_job_resets_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            config.mikan_extract_lease_seconds = 900
            torrent = QBitTorrent(
                hash="expired-direct-claim-hash",
                name="[Group] Direct Expired Claim Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Direct Expired Claim Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/direct-expired.torrent"}], 1, True)]
            job_key = "hash:expired-direct-claim-hash"

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                conn.execute(
                    """
                    UPDATE mikan_extract_jobs
                    SET status = 'running',
                        lease_until = ?,
                        started_at = ?,
                        finished_at = ?,
                        updated_at = ?
                    WHERE job_key = ?
                    """,
                    (1.0, 123.0, 456.0, 789.0, job_key),
                )
                conn.commit()
            finally:
                conn.close()

            with patch("mikan_worker.time.time", return_value=1000.0):
                jobs = _claim_mikan_extract_jobs(config, limit=1)

            self.assertEqual([job.job_key for job in jobs], [job_key])
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status, started_at, finished_at, updated_at = conn.execute(
                    """
                    SELECT status, started_at, finished_at, updated_at
                    FROM mikan_extract_jobs
                    WHERE job_key = ?
                    """,
                    (job_key,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "running")
            self.assertEqual(started_at, 1000.0)
            self.assertEqual(finished_at, 0.0)
            self.assertEqual(updated_at, 1000.0)

    def test_process_completed_prioritizes_existing_extract_jobs_before_slow_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="fast-claim-hash",
                name="[Group] Fast Claim Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Fast Claim Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/fast-claim.torrent"}], 1, True)]
            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)

            qbit = Mock()
            qbit.list_torrents.return_value = []
            qbit.list_files.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(return_value=[])
            worker._ensure_episode_index = Mock()
            worker._reconcile_pending_with_existing_torrents = Mock(side_effect=AssertionError("slow reconcile should not run"))
            worker._sync_pending_download_progress_from_torrents = Mock(side_effect=AssertionError("slow sync should not run"))
            worker._sync_qbit_source_tags = Mock(side_effect=AssertionError("source tag sync should not run"))
            worker._enqueue_completed_extract_jobs = Mock(side_effect=AssertionError("enqueue should not run before existing jobs"))
            worker._extract_completed_torrent = Mock(return_value=MikanExtractResult(1))

            processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 1)
            worker._extract_completed_torrent.assert_called_once()
            qbit.add_tags.assert_called_once_with("fast-claim-hash", config.mikan_processed_tags)
            worker._reconcile_pending_with_existing_torrents.assert_not_called()
            worker._sync_pending_download_progress_from_torrents.assert_not_called()
            worker._sync_qbit_source_tags.assert_not_called()
            worker._enqueue_completed_extract_jobs.assert_not_called()

    def test_extract_job_migration_preserves_replaceable_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, attempts, worker_id, lease_until,
                        torrent_hash, torrent_name, bangumi_ids_json, episodes_json,
                        pending_entries_json, torrent_json, result_json, last_error,
                        created_at, updated_at, started_at, finished_at
                    )
                    VALUES (?, 'failed', 1, 1, '', 0, 'hash-source-missing',
                            'Source Missing - 01', '[123]', '[1]', '[]', '{}', ?, ?, 10, 20, 15, 20)
                    """,
                    (
                        "hash:source-missing",
                        json.dumps(
                            {
                                "retryable": False,
                                "failure_reason": "source_video_missing",
                                "failure_bucket": "mapped_path_missing",
                                "failure_detail": "Mapped completed torrent path does not exist: /qbit/missing",
                            },
                            ensure_ascii=False,
                        ),
                        "Mapped completed torrent path does not exist: /qbit/missing",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            conn = _mikan_state_connect(config)
            try:
                status = conn.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE job_key = ?",
                    ("hash:source-missing",),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(status, "replaced")

    def test_extract_job_migration_restores_legacy_terminal_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, attempts, worker_id, lease_until,
                        torrent_hash, torrent_name, bangumi_ids_json, episodes_json,
                        pending_entries_json, torrent_json, result_json, last_error,
                        created_at, updated_at, started_at, finished_at
                    )
                    VALUES (?, 'terminal_failed', 1, 1, '', 0, 'hash-legacy',
                            'Legacy Missing - 01', '[123]', '[1]', '[]', '{}', ?, ?, 10, 20, 15, 20)
                    """,
                    (
                        "hash:legacy-source-missing",
                        json.dumps(
                            {
                                "retryable": False,
                                "failure_reason": "source_video_missing",
                                "failure_detail": "Mapped completed torrent path does not exist: /qbit/legacy",
                            },
                            ensure_ascii=False,
                        ),
                        "Mapped completed torrent path does not exist: /qbit/legacy",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            conn = _mikan_state_connect(config)
            try:
                status = conn.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE job_key = ?",
                    ("hash:legacy-source-missing",),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(status, "replaced")

    def test_extract_job_migration_keeps_nonreplaceable_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, attempts, worker_id, lease_until,
                        torrent_hash, torrent_name, bangumi_ids_json, episodes_json,
                        pending_entries_json, torrent_json, result_json, last_error,
                        created_at, updated_at, started_at, finished_at
                    )
                    VALUES (?, 'failed', 1, 1, '', 0, 'hash-terminal',
                            'Terminal - 01', '[123]', '[1]', '[]', '{}', ?, 'extract crashed', 10, 20, 15, 20)
                    """,
                    (
                        "hash:terminal-error",
                        json.dumps(
                            {
                                "retryable": False,
                                "failure_reason": "extract_exception",
                                "failure_detail": "extract crashed",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            conn = _mikan_state_connect(config)
            try:
                status = conn.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE job_key = ?",
                    ("hash:terminal-error",),
                ).fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(status, "terminal_failed")

    def test_retryable_failed_extract_job_respects_retry_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            config.mikan_extract_failed_retry_seconds = 9999
            torrent = QBitTorrent(
                hash="retryable-failed-hash",
                name="[Group] Retryable Failed Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Retryable Failed Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "torrent_url": "https://mikan/retryable.torrent"}], 1, True)]

            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            _finish_mikan_extract_job(
                config,
                job.job_key,
                "failed",
                MikanExtractResult(0, failure_reason="target_video_not_found", failure_detail="retry later", retryable=True),
            )
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                status = conn.execute("SELECT status FROM mikan_extract_jobs WHERE job_key = ?", (job.job_key,)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "failed")
            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 0)

            config.mikan_extract_failed_retry_seconds = 0
            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            self.assertEqual(len(_claim_mikan_extract_jobs(config, limit=1)), 1)

    def test_requeue_failed_extract_jobs_skips_legacy_nonretryable_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            conn = _mikan_state_connect(config)
            try:
                conn.executemany(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, attempts, worker_id, lease_until,
                        torrent_hash, torrent_name, bangumi_ids_json, episodes_json,
                        pending_entries_json, torrent_json, result_json, last_error,
                        created_at, updated_at, started_at, finished_at
                    )
                    VALUES (?, ?, 1, 3, 'worker', 999, ?, ?, '[123]', '[1]', '[]', '{}',
                            '{"retryable": false}', 'old error', 10, 20, 15, 20)
                    """,
                    [
                        ("hash:failed", "failed", "failed", "Failed Show - 01"),
                        ("hash:terminal", "terminal_failed", "terminal", "Terminal Show - 01"),
                        ("hash:success", "success", "success", "Success Show - 01"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(requeue_failed_mikan_extract_jobs(config), 0)

            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                rows = {
                    key: (status, priority, attempts, last_error, started_at, finished_at)
                    for key, status, priority, attempts, last_error, started_at, finished_at in conn.execute(
                        """
                        SELECT job_key, status, priority, attempts, last_error, started_at, finished_at
                        FROM mikan_extract_jobs
                        ORDER BY job_key
                        """
                    ).fetchall()
                }
            finally:
                conn.close()

            self.assertEqual(rows["hash:failed"][0], "terminal_failed")
            self.assertEqual(rows["hash:terminal"][0], "terminal_failed")
            self.assertEqual(rows["hash:success"][0], "success")

    def test_bulk_requeue_only_retries_retryable_nonterminal_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            conn = _mikan_state_connect(config)
            try:
                conn.executemany(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, attempts, worker_id, lease_until,
                        torrent_hash, torrent_name, bangumi_ids_json, episodes_json,
                        pending_entries_json, torrent_json, result_json, last_error,
                        created_at, updated_at, started_at, finished_at
                    )
                    VALUES (?, ?, 1, 3, 'worker', 999, ?, ?, '[123]', '[1]', '[]', '{}',
                            ?, 'old error', 10, 20, 15, 20)
                    """,
                    [
                        (
                            "hash:failed",
                            "failed",
                            "failed",
                            "Failed Show - 01",
                            '{"retryable": true}',
                        ),
                        (
                            "hash:terminal",
                            "terminal_failed",
                            "terminal",
                            "Terminal Show - 01",
                            '{"retryable": false}',
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(
                requeue_failed_mikan_extract_jobs(
                    config,
                    include_terminal=False,
                ),
                1,
            )

            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                rows = dict(
                    conn.execute(
                        "SELECT job_key, status FROM mikan_extract_jobs"
                    ).fetchall()
                )
            finally:
                conn.close()
            self.assertEqual(rows["hash:failed"], "queued")
            self.assertEqual(rows["hash:terminal"], "terminal_failed")

    def test_ai_exclusion_uses_only_active_pending_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            show_dir = root / "Show"
            show_dir.mkdir()
            video = show_dir / "Show - 01.mkv"
            video.write_bytes(b"video")
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                video_extensions=[".mkv"],
                mikan_download_start_timeout_seconds=600,
            )
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(return_value=[{"bangumi_id": 123, "path": str(show_dir)}])
            pending = {"items": {}}
            _mark_pending(pending, _release("https://mikan/pending.torrent"))
            worker.pending_path.write_text(json.dumps(pending), encoding="utf-8")

            with patch("mikan_worker.fetch_bangumi_releases") as fetch:
                excluded = worker.videos_with_available_official_subtitles()

            fetch.assert_not_called()
            self.assertEqual(excluded, {video.resolve()})

    def test_ai_exclusion_ignores_stale_pending_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            show_dir = root / "Show"
            show_dir.mkdir()
            (show_dir / "Show - 01.mkv").write_bytes(b"video")
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                video_extensions=[".mkv"],
                mikan_download_start_timeout_seconds=600,
            )
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(return_value=[{"bangumi_id": 123, "path": str(show_dir)}])
            pending = {"items": {}}
            _mark_pending(pending, _release("https://mikan/stale.torrent"))
            pending["items"]["123:1"]["queued_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            worker.pending_path.write_text(json.dumps(pending), encoding="utf-8")

            excluded = worker.videos_with_available_official_subtitles()

            self.assertEqual(excluded, set())

    def test_run_once_processes_completed_before_enqueue_when_qbit_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=Path(temp_dir),
                mikan_extract_completed=True,
            )
            worker = MikanWorker(config, _logger())
            worker._enqueue_latest_releases_unlocked = Mock(side_effect=QBitError("login failed"))
            worker._process_completed_downloads_unlocked = Mock()

            worker.run_once()

            worker._process_completed_downloads_unlocked.assert_called_once_with()

    def test_reset_all_state_backs_up_and_clears_seen_and_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
            )
            seen_path = root / "mikan_seen.json"
            pending_path = root / "mikan_pending.json"
            seen_path.write_text(json.dumps({"https://mikan/old.torrent": {"title": "old"}}), encoding="utf-8")
            pending_path.write_text(
                json.dumps({"items": {"123:1": {"torrent_url": "https://mikan/old.torrent"}}}),
                encoding="utf-8",
            )

            result = MikanWorker(config, _logger()).reset_all_state()

            self.assertEqual(result["seen_entries"], 1)
            self.assertEqual(result["pending_entries"], 1)
            self.assertEqual(json.loads(seen_path.read_text(encoding="utf-8")), {})
            self.assertEqual(json.loads(pending_path.read_text(encoding="utf-8")), {"items": {}})
            self.assertEqual(
                json.loads(Path(result["seen_backup"]).read_text(encoding="utf-8")),
                {"https://mikan/old.torrent": {"title": "old"}},
            )
            self.assertEqual(
                json.loads(Path(result["pending_backup"]).read_text(encoding="utf-8")),
                {"items": {"123:1": {"torrent_url": "https://mikan/old.torrent"}}},
            )

    def test_reset_all_state_times_out_when_mikan_operation_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                mikan_operation_lock_wait_seconds=0,
            )
            lock = VideoLock(root / "mikan_worker")
            self.assertTrue(lock.acquire())
            try:
                with self.assertRaises(MikanWorkerError) as ctx:
                    MikanWorker(config, _logger()).reset_all_state_and_enqueue()
                self.assertIn("Mikan operation already running", str(ctx.exception))
            finally:
                lock.release()

    def test_reset_all_state_waits_for_mikan_operation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                mikan_operation_lock_wait_seconds=30,
            )

            class FakeLock:
                def __init__(self, acquired: bool) -> None:
                    self.acquired = acquired
                    self.released = False
                    self.lock_path = root / "mikan_worker.lock"

                def acquire(self) -> bool:
                    return self.acquired

                def release(self) -> None:
                    self.released = True

            locks = [FakeLock(False), FakeLock(True)]
            with (
                patch("mikan_worker._mikan_operation_lock", side_effect=locks),
                patch("mikan_worker.time.sleep") as sleep,
            ):
                result = MikanWorker(config, _logger()).reset_all_state()

            sleep.assert_called_once()
            self.assertEqual(result["seen_entries"], 0)
            self.assertTrue(locks[1].released)

    def test_reset_all_state_defers_when_mikan_operation_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
            )
            lock = VideoLock(root / "mikan_worker")
            self.assertTrue(lock.acquire())
            try:
                result = MikanWorker(config, _logger()).reset_all_state_and_enqueue(defer_if_busy=True)
            finally:
                lock.release()

            request_path = root / "mikan_reset_all.request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertTrue(result["deferred"])
            self.assertEqual(result["request_path"], str(request_path))
            self.assertEqual(request["action"], "reset_all_state_and_enqueue")
            self.assertEqual(request["request_count"], 1)

    def test_reset_all_state_runs_while_extract_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_enabled=False,
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
            )
            extract_lock = VideoLock(root / "mikan_extract")
            self.assertTrue(extract_lock.acquire())
            try:
                result = MikanWorker(config, _logger()).reset_all_state_and_enqueue(defer_if_busy=True)
            finally:
                extract_lock.release()

            self.assertFalse(result["deferred"])
            self.assertEqual(result["queued"], 0)
            self.assertFalse((root / "mikan_reset_all.request.json").exists())

    def test_reset_all_state_defers_when_enqueue_lock_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_enabled=False,
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
            )
            queue_lock = VideoLock(root / "mikan_enqueue")
            self.assertTrue(queue_lock.acquire())
            try:
                result = MikanWorker(config, _logger()).reset_all_state_and_enqueue(defer_if_busy=True)
            finally:
                queue_lock.release()

            request_path = root / "mikan_reset_all.request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertTrue(result["deferred"])
            self.assertEqual(request["action"], "reset_all_state_and_enqueue")
            self.assertIn("queue operation", result["reason"])

    def test_process_completed_runs_while_enqueue_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                mikan_operation_lock_wait_seconds=0,
            )
            queue_lock = VideoLock(root / "mikan_enqueue")
            self.assertTrue(queue_lock.acquire())
            try:
                worker = MikanWorker(config, _logger())
                worker._process_completed_downloads_unlocked = Mock(return_value=7)
                processed = worker.process_completed_downloads(required=False)
            finally:
                queue_lock.release()

            self.assertEqual(processed, 7)
            worker._process_completed_downloads_unlocked.assert_called_once_with()

    def test_enqueue_scan_does_not_hold_global_queue_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = MikanWorker(_mikan_enqueue_config(root), _logger())
            worker._enqueue_latest_releases_unlocked = Mock(return_value=3)
            queue_lock = VideoLock(root / "mikan_enqueue")
            self.assertTrue(queue_lock.acquire())
            try:
                queued = worker.enqueue_latest_releases(required=False)
            finally:
                queue_lock.release()

            self.assertEqual(queued, 3)
            worker._enqueue_latest_releases_unlocked.assert_called_once_with(state_required=False)

    def test_replacement_enqueue_is_persisted_while_queue_lock_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            target = MikanReplacementTarget(123, 8)
            queue_lock = VideoLock(root / "mikan_enqueue")
            self.assertTrue(queue_lock.acquire())
            try:
                queued = worker._enqueue_replacements_after_extract_failure([target], Mock())
            finally:
                queue_lock.release()

            request_path = root / "mikan_replacement_enqueue.request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(queued, 0)
            self.assertEqual(request["targets"], [{"bangumi_id": 123, "episode": 8}])

            worker._qbit = Mock(return_value=Mock())
            worker._enqueue_replacements_after_extract_failure_unlocked = Mock(return_value=1)
            result = worker.consume_replacement_enqueue_request()

            self.assertFalse(result["deferred"])
            self.assertEqual(result["queued"], 1)
            self.assertFalse(request_path.exists())
            worker._enqueue_replacements_after_extract_failure_unlocked.assert_called_once()

    def test_release_snapshot_read_does_not_wait_for_state_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = MikanWorker(_mikan_enqueue_config(root), _logger())
            release = _episode_release("https://mikan/new.torrent", "[Mikan] Test Anime - 08 [CHT]", 8)
            state_lock = VideoLock(root / "mikan_worker")
            self.assertTrue(state_lock.acquire())
            try:
                can_queue = worker._release_can_be_queued(
                    release,
                    operation="test_snapshot_read",
                    state_required=False,
                )
            finally:
                state_lock.release()

            self.assertTrue(can_queue)

    def test_redownload_all_preserves_completed_unprocessed_qbit_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_processed_tags = ["mikansub-extracted"]
            qbit = Mock()
            qbit.list_torrents.return_value = [
                QBitTorrent(
                    hash="hash1",
                    name="Release 1",
                    progress=1.0,
                    state="uploading",
                    dlspeed=0,
                    downloaded=100,
                    added_on=None,
                    content_path=None,
                    save_path=None,
                    category="llm-sub",
                    tags="mikansub",
                ),
                QBitTorrent(
                    hash="hash2",
                    name="Release 2",
                    progress=0.5,
                    state="downloading",
                    dlspeed=0,
                    downloaded=50,
                    added_on=None,
                    content_path=None,
                    save_path=None,
                    category="llm-sub",
                    tags="mikansub",
                ),
            ]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._reset_all_state_unlocked = Mock(return_value={"seen_entries": 2, "pending_entries": 1})

            def enqueue_with_active_marker(**_kwargs):
                active = json.loads((root / "mikan_redownload_all.active.json").read_text(encoding="utf-8"))
                self.assertEqual(active["stage"], "enqueue_prepare")
                self.assertEqual(active["deleted_torrents"], 1)
                probe = VideoLock(root / "mikan_enqueue")
                self.assertTrue(probe.acquire(), "redownload must release the queue lock before the long scan")
                probe.release()
                return 4

            worker._enqueue_latest_releases_unlocked = Mock(side_effect=enqueue_with_active_marker)

            result = worker.redownload_all_torrents_and_enqueue(delete_files=False)

            qbit.list_torrents.assert_called_once_with(tag="mikansub", category="llm-sub")
            qbit.delete_torrents.assert_called_once_with(["hash2"], delete_files=False)
            worker._reset_all_state_unlocked.assert_called_once_with()
            worker._enqueue_latest_releases_unlocked.assert_called_once_with(
                state_required=True,
                allow_redownload_preempt=False,
                redownload_progress=True,
                queue_lock_held=False,
            )
            self.assertEqual(result["deleted_torrents"], 1)
            self.assertFalse(result["delete_files"])
            self.assertEqual(result["queued"], 4)
            self.assertFalse((root / "mikan_redownload_all.active.json").exists())
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                row = conn.execute(
                    "SELECT status, payload_json FROM mikan_jobs WHERE job_name = 'redownload_all'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row[0], "done")
            payload = json.loads(row[1])
            self.assertEqual(payload["deleted_torrents"], 1)
            self.assertEqual(payload["queued"], 4)

    def test_redownload_all_db_lease_rejects_duplicate_without_qbit_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            lease = _claim_mikan_job(
                config,
                "redownload_all",
                payload={"delete_files": False, "deleted_torrents": 99},
                lease_seconds=600,
            )
            self.assertIsNotNone(lease)
            qbit = Mock()
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)

            result = worker.redownload_all_torrents_and_enqueue(delete_files=False)

            self.assertTrue(result["already_running"])
            self.assertEqual(result["deleted_torrents"], 99)
            worker._qbit.assert_not_called()
            qbit.delete_torrents.assert_not_called()

    def test_redownload_all_does_not_wait_for_extract_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            extract_lock = VideoLock(root / "mikan_extract")
            self.assertTrue(extract_lock.acquire())
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._reset_all_state_unlocked = Mock(return_value={"seen_entries": 0, "pending_entries": 0})
            worker._enqueue_latest_releases_unlocked = Mock(return_value=2)
            try:
                result = worker.redownload_all_torrents_and_enqueue(defer_if_busy=True)
            finally:
                extract_lock.release()

            self.assertFalse(result["deferred"])
            self.assertEqual(result["queued"], 2)
            self.assertFalse((root / "mikan_redownload_all.request.json").exists())

    def test_redownload_all_defers_without_qbit_delete_when_queue_lock_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            queue_lock = VideoLock(root / "mikan_enqueue")
            self.assertTrue(queue_lock.acquire())
            log_stream = io.StringIO()
            logger = logging.getLogger("test.mikan_worker.redownload_queue_busy")
            logger.handlers = []
            logger.addHandler(logging.StreamHandler(log_stream))
            logger.setLevel(logging.INFO)
            logger.propagate = False
            qbit = Mock()
            qbit.list_torrents.return_value = [
                QBitTorrent(
                    hash="hash1",
                    name="Release 1",
                    progress=1.0,
                    state="uploading",
                    dlspeed=0,
                    downloaded=100,
                    added_on=None,
                    content_path=None,
                    save_path=None,
                    category="llm-sub",
                    tags="mikansub",
                )
            ]
            try:
                worker = MikanWorker(config, logger)
                worker._qbit = Mock(return_value=qbit)
                worker._reset_all_state_unlocked = Mock(return_value={"seen_entries": 0, "pending_entries": 0})
                result = worker.redownload_all_torrents_and_enqueue(defer_if_busy=True)
            finally:
                queue_lock.release()

            request_path = root / "mikan_redownload_all.request.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertTrue(result["deferred"])
            self.assertIn("queue operation", result["reason"])
            self.assertFalse(result["qbit_deleted"])
            self.assertEqual(result["deleted_torrents"], 0)
            self.assertNotIn("qbit_deleted_at", request)
            self.assertNotIn("deleted_torrents", request)
            worker._qbit.assert_not_called()
            qbit.delete_torrents.assert_not_called()
            self.assertIn("queue operation", request["reason"])
            self.assertNotIn("mikan_extract.lock", log_stream.getvalue())

    def test_redownload_all_defers_without_qbit_delete_when_state_lock_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            state_lock = VideoLock(root / "mikan_worker")
            self.assertTrue(state_lock.acquire())
            qbit = Mock()
            qbit.list_torrents.return_value = [
                QBitTorrent(
                    hash="hash1",
                    name="Release 1",
                    progress=0.5,
                    state="downloading",
                    dlspeed=0,
                    downloaded=50,
                    added_on=None,
                    content_path=None,
                    save_path=None,
                    category="llm-sub",
                    tags="mikansub",
                )
            ]
            try:
                worker = MikanWorker(config, _logger())
                worker._qbit = Mock(return_value=qbit)
                result = worker.redownload_all_torrents_and_enqueue(defer_if_busy=True)
            finally:
                state_lock.release()

            self.assertTrue(result["deferred"])
            self.assertIn("state operation", result["reason"])
            self.assertFalse(result["qbit_deleted"])
            self.assertEqual(result["deleted_torrents"], 0)
            request = json.loads((root / "mikan_redownload_all.request.json").read_text(encoding="utf-8"))
            self.assertNotIn("qbit_deleted_at", request)
            self.assertNotIn("deleted_torrents", request)
            self.assertNotIn("state_reset_at", request)
            worker._qbit.assert_not_called()
            qbit.delete_torrents.assert_not_called()

    def test_redownload_all_duplicate_while_active_does_not_queue_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            queue_lock = VideoLock(root / "mikan_enqueue")
            extract_lock = VideoLock(root / "mikan_extract")
            self.assertTrue(queue_lock.acquire())
            self.assertTrue(extract_lock.acquire())
            (root / "mikan_redownload_all.active.json").write_text(
                json.dumps(
                    {
                        "action": "redownload_all_torrents_and_enqueue",
                        "started_at": "2026-06-08T13:19:00+00:00",
                        "delete_files": False,
                    }
                ),
                encoding="utf-8",
            )
            try:
                result = MikanWorker(config, _logger()).redownload_all_torrents_and_enqueue(defer_if_busy=True)
            finally:
                extract_lock.release()
                queue_lock.release()

            self.assertTrue(result["already_running"])
            self.assertFalse(result["deferred"])
            self.assertFalse((root / "mikan_redownload_all.request.json").exists())

    def test_worker_cancel_redownload_removes_only_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            request = root / "mikan_redownload_all.request.json"
            request.write_text('{"action":"redownload_all_torrents_and_enqueue"}', encoding="utf-8")

            config = SimpleNamespace(
                work_path=root,
                mikan_pending_path=root / "mikan_pending.json",
                mikan_state_db_path=root / "mikan_state.sqlite3",
            )
            result = request_mikan_redownload_cancel(config)

            self.assertTrue(result["cancelled_pending"])
            self.assertFalse(result["cancel_requested"])
            self.assertFalse(request.exists())
            self.assertTrue((root / "mikan_redownload_all.cancel.json").is_file())

    def test_worker_cancel_redownload_keeps_active_operation_and_writes_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            active = root / "mikan_redownload_all.active.json"
            active.write_text(
                json.dumps({"action": "redownload_all_torrents_and_enqueue", "updated_at": time.time()}),
                encoding="utf-8",
            )

            config = SimpleNamespace(
                work_path=root,
                mikan_pending_path=root / "mikan_pending.json",
                mikan_state_db_path=root / "mikan_state.sqlite3",
            )
            result = request_mikan_redownload_cancel(config)

            self.assertTrue(result["cancel_requested"])
            self.assertFalse(result["cancelled_pending"])
            self.assertTrue(active.exists())
            marker = json.loads((root / "mikan_redownload_all.cancel.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["action"], "cancel_redownload_all")

    def test_worker_cancel_extract_targets_current_lease_without_touching_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            conn = _mikan_state_connect(config)
            try:
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, torrent_name, status, worker_id, created_at, updated_at, started_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?)
                    """,
                    ("hash:test", "Collection", "extractor:123", now, now, now),
                )
                conn.commit()
            finally:
                conn.close()

            result = request_mikan_extract_cancel(config, job_key="hash:test")

            self.assertTrue(result["cancel_requested"])
            self.assertEqual(result["job_key"], "hash:test")
            marker = json.loads(
                (root / "mikan_extract_cancel.request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["worker_id"], "extractor:123")
            self.assertEqual(marker["job_key"], "hash:test")

    def test_worker_cancel_extract_rejects_non_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            conn = _mikan_state_connect(config)
            conn.close()

            with self.assertRaisesRegex(MikanWorkerError, "No running"):
                request_mikan_extract_cancel(config, job_key="hash:missing")

    def test_worker_cancel_extract_finishes_without_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="cancel-no-retry-hash",
                name="[Group] Cancel Show - 01 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Cancel Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            rows = [(torrent, [{"bangumi_id": 123, "episode": 1, "source": "mikan"}], 1, True)]
            self.assertEqual(_upsert_mikan_extract_jobs(config, rows, state_required=True), 1)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            request_mikan_extract_cancel(config, job_key=job.job_key)
            worker = MikanWorker(config, _logger())

            def wait_for_cancel(*_args, cancel_event=None, **_kwargs):
                deadline = time.monotonic() + 3
                while cancel_event is not None and not cancel_event.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
                return MikanExtractResult(
                    0,
                    failure_reason="extract_cancelled",
                    failure_detail="cancelled",
                    retryable=True,
                    defer_seconds=60,
                )

            worker._extract_completed_torrent = Mock(side_effect=wait_for_cancel)
            qbit = Mock()
            qbit.list_files.return_value = []

            processed = worker._process_claimed_mikan_extract_jobs(qbit, [], [job])

            self.assertEqual(processed, 0)
            conn = _mikan_state_connect(config)
            try:
                status, lease_until, result_json = conn.execute(
                    "SELECT status, lease_until, result_json FROM mikan_extract_jobs WHERE job_key = ?",
                    (job.job_key,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "terminal_failed")
            self.assertEqual(lease_until, 0)
            self.assertEqual(json.loads(result_json)["failure_reason"], "extract_cancelled_by_user")
            self.assertFalse((root / "mikan_extract_cancel.request.json").exists())

    def test_worker_requeues_only_requested_failed_extract_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            conn = _mikan_state_connect(config)
            try:
                now = time.time()
                for key in ("hash:first", "hash:second"):
                    conn.execute(
                        """
                        INSERT INTO mikan_extract_jobs(
                            job_key, torrent_name, status, created_at, updated_at, finished_at
                        ) VALUES (?, ?, 'terminal_failed', ?, ?, ?)
                        """,
                        (key, key, now, now, now),
                    )
                conn.commit()
            finally:
                conn.close()

            self.assertTrue(requeue_mikan_extract_job(config, job_key="hash:first"))

            conn = _mikan_state_connect(config)
            try:
                rows = dict(conn.execute("SELECT job_key, status FROM mikan_extract_jobs"))
            finally:
                conn.close()
            self.assertEqual(rows["hash:first"], "queued")
            self.assertEqual(rows["hash:second"], "terminal_failed")

    def test_run_once_processes_completed_before_deferred_redownload_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_extract_completed = True
            request_path = root / "mikan_redownload_all.request.json"
            request_path.write_text(
                json.dumps({"action": "redownload_all_torrents_and_enqueue", "request_count": 1, "delete_files": False}),
                encoding="utf-8",
            )
            worker = MikanWorker(config, _logger())
            worker._redownload_all_torrents_and_enqueue_unlocked = Mock(
                return_value={
                    "deferred": False,
                    "deleted_torrents": 1,
                    "delete_files": False,
                    "reset": {},
                    "queued": 2,
                }
            )
            worker._enqueue_latest_releases_unlocked = Mock()
            worker._process_completed_downloads_unlocked = Mock()

            worker.run_once()

            self.assertFalse(request_path.exists())
            worker._redownload_all_torrents_and_enqueue_unlocked.assert_called_once_with(
                delete_files=False,
                state_required=False,
                state_lock=None,
            )
            worker._enqueue_latest_releases_unlocked.assert_not_called()
            worker._process_completed_downloads_unlocked.assert_called_once_with()

    def test_consume_redownload_clears_stale_active_marker_and_resumes_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            request_path = root / "mikan_redownload_all.request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "action": "redownload_all_torrents_and_enqueue",
                        "request_count": 1,
                        "delete_files": False,
                        "reason": "Mikan queue operation already running",
                    }
                ),
                encoding="utf-8",
            )
            active_path = root / "mikan_redownload_all.active.json"
            active_path.write_text(
                json.dumps(
                    {
                        "action": "redownload_all_torrents_and_enqueue",
                        "started_at": "2026-06-09T10:00:40+00:00",
                        "delete_files": False,
                        "stage": "resolve_series",
                        "deleted_torrents": 687,
                    }
                ),
                encoding="utf-8",
            )
            stale_time = time.time() - 3600
            os.utime(active_path, (stale_time, stale_time))
            worker = MikanWorker(config, _logger())
            worker._redownload_all_torrents_and_enqueue_unlocked = Mock(
                return_value={
                    "deferred": True,
                    "qbit_deleted": True,
                    "deleted_torrents": 687,
                    "delete_files": False,
                    "reset": None,
                    "queued": 0,
                    "reason": "waiting for queue",
                }
            )

            result = worker.consume_redownload_all_request()

            self.assertTrue(result["deferred"])
            self.assertFalse(active_path.exists())
            request = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(request["deleted_torrents"], 687)
            self.assertIn("Recovered stale active marker", request["reason"])
            worker._redownload_all_torrents_and_enqueue_unlocked.assert_called_once_with(
                delete_files=False,
                state_required=False,
                state_lock=None,
            )

    def test_recent_redownload_active_marker_is_live_without_long_held_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            active_path = root / "mikan_redownload_all.active.json"
            active_path.write_text(
                json.dumps(
                    {
                        "action": "redownload_all_torrents_and_enqueue",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "stage": "scan_missing",
                    }
                ),
                encoding="utf-8",
            )

            result = MikanWorker(config, _logger()).request_redownload_all(log_deferred=False)

            self.assertTrue(result["already_running"])
            self.assertFalse((root / "mikan_redownload_all.request.json").exists())

    def test_run_once_uses_db_extract_jobs_even_when_legacy_extract_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_enabled=False,
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                mikan_extract_completed=True,
            )
            extract_lock = VideoLock(root / "mikan_extract")
            self.assertTrue(extract_lock.acquire())
            try:
                worker = MikanWorker(config, _logger())
                worker._process_completed_downloads_unlocked = Mock()
                worker.run_once()
            finally:
                extract_lock.release()

            worker._process_completed_downloads_unlocked.assert_called_once_with()

    def test_reset_all_defer_does_not_log_skip_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
            )
            logger = logging.getLogger("test.mikan_worker.defer_log")
            logger.handlers = [logging.NullHandler()]
            logger.setLevel(logging.INFO)
            logger.propagate = False
            lock = VideoLock(root / "mikan_worker")
            self.assertTrue(lock.acquire())
            try:
                with self.assertLogs(logger, level="WARNING") as captured:
                    MikanWorker(config, logger).reset_all_state_and_enqueue(defer_if_busy=True)
            finally:
                lock.release()

            output = "\n".join(captured.output)
            self.assertIn("Mikan reset-all deferred", output)
            self.assertNotIn("skip operation=reset_all_state_and_enqueue", output)

    def test_completed_download_does_not_restore_pending_cleared_during_extract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            library_dir.mkdir(parents=True)
            download_root.mkdir()
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Release Show - 08.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = []
            pending = {"items": {}}
            _mark_pending(pending, _episode_release("https://mikan/release.torrent", torrent.name, 8))
            pending_path = root / "mikan_pending.json"
            pending_path.write_text(json.dumps(pending), encoding="utf-8")
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )

            def extract_and_reset(*_args, **_kwargs):
                pending_path.write_text(json.dumps({"items": {}}), encoding="utf-8")
                return 1

            worker._extract_completed_torrent = Mock(side_effect=extract_and_reset)

            processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 1)
            self.assertEqual(json.loads(pending_path.read_text(encoding="utf-8")), {"items": {}})
            self.assertEqual(
                qbit.add_tags.call_args_list,
                [call("hash1", ["mikan"]), call("hash1", ["mikansub-extracted"])],
            )

    def test_run_once_consumes_deferred_reset_all_request_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                mikan_extract_completed=True,
            )
            request_path = root / "mikan_reset_all.request.json"
            request_path.write_text(
                json.dumps({"action": "reset_all_state_and_enqueue", "request_count": 1}),
                encoding="utf-8",
            )
            worker = MikanWorker(config, _logger())
            worker._reset_all_state_unlocked = Mock(return_value={"seen_entries": 0, "pending_entries": 0})
            worker._enqueue_latest_releases_unlocked = Mock(return_value=3)
            worker._process_completed_downloads_unlocked = Mock()

            worker.run_once()

            self.assertFalse(request_path.exists())
            worker._reset_all_state_unlocked.assert_called_once_with()
            worker._enqueue_latest_releases_unlocked.assert_called_once_with(
                state_required=False,
                queue_lock_held=False,
            )
            worker._process_completed_downloads_unlocked.assert_called_once_with()

    def test_run_once_consumes_deferred_request_after_enqueue_preemption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=root,
                mikan_extract_completed=True,
            )
            worker = MikanWorker(config, _logger())
            worker.consume_deferred_requests = Mock(side_effect=[None, {"deferred": False}])
            worker.enqueue_latest_releases = Mock(return_value=0)
            worker.process_completed_downloads = Mock()

            worker.run_once()

            self.assertEqual(worker.consume_deferred_requests.call_count, 2)
            worker.enqueue_latest_releases.assert_called_once_with(required=False)
            worker.process_completed_downloads.assert_called_once_with(required=False)

    def test_enqueue_preempts_when_redownload_request_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_bangumi_ids = [123, 456]
            (root / "mikan_redownload_all.request.json").write_text(
                json.dumps({"action": "redownload_all_torrents_and_enqueue", "request_count": 1}),
                encoding="utf-8",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(return_value=[])

            with patch("mikan_worker.fetch_bangumi_releases") as fetch:
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 0)
            fetch.assert_not_called()

    def test_redownload_progress_queues_each_bangumi_before_scanning_next(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_bangumi_ids = []
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[
                    {"bangumi_id": 123, "path": str(root / "Show A")},
                    {"bangumi_id": 456, "path": str(root / "Show B")},
                ]
            )
            scanned: list[int] = []

            def missing_for_bangumi(_config, _logger_arg, bangumi_id, _mappings, **_kwargs):
                if bangumi_id == 456:
                    self.assertEqual(qbit.add_url.call_count, 1)
                scanned.append(bangumi_id)
                return {1}

            def releases_for_bangumi(_base_url, bangumi_id, **_kwargs):
                return [
                    MikanRelease(
                        bangumi_id=bangumi_id,
                        title=f"[Group] Show {bangumi_id} - 01 [CHT].mkv",
                        episode=1,
                        torrent_url=f"https://mikan/{bangumi_id}.torrent",
                        pub_date=None,
                        content_length=100,
                    )
                ]

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", side_effect=missing_for_bangumi),
                patch("mikan_worker.fetch_bangumi_releases", side_effect=releases_for_bangumi),
            ):
                queued = worker._enqueue_latest_releases_unlocked(redownload_progress=True)

            self.assertEqual(queued, 2)
            self.assertEqual(scanned, [123, 456])
            self.assertEqual(qbit.add_url.call_count, 2)

    def test_redownload_cancel_marker_stops_before_next_release_is_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_bangumi_ids = []
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[
                    {"bangumi_id": 123, "path": str(root / "Show A")},
                    {"bangumi_id": 456, "path": str(root / "Show B")},
                ]
            )

            def missing_for_bangumi(*_args, **_kwargs):
                (root / "mikan_redownload_all.cancel.json").write_text('{"requested": true}', encoding="utf-8")
                return {1}

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", side_effect=missing_for_bangumi),
                patch(
                    "mikan_worker.fetch_bangumi_releases",
                    return_value=[
                        MikanRelease(
                            bangumi_id=123,
                            title="[Group] Show - 01 [CHT].mkv",
                            episode=1,
                            torrent_url="https://mikan/123.torrent",
                            pub_date=None,
                            content_length=100,
                        )
                    ],
                ) as fetch,
            ):
                queued = worker._enqueue_latest_releases_unlocked(redownload_progress=True)

            self.assertEqual(queued, 0)
            self.assertEqual(fetch.call_count, 1)
            qbit.add_url.assert_not_called()

    def test_redownload_cancel_before_destructive_stage_removes_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            request_path = root / "mikan_redownload_all.request.json"
            request_path.write_text(
                json.dumps({"action": "redownload_all_torrents_and_enqueue", "delete_files": False}),
                encoding="utf-8",
            )
            (root / "mikan_redownload_all.cancel.json").write_text('{"requested": true}', encoding="utf-8")
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock()

            result = worker._redownload_all_torrents_and_enqueue_unlocked(delete_files=False)

            self.assertTrue(result["cancelled"])
            self.assertFalse(result["qbit_deleted"])
            self.assertFalse(request_path.exists())
            worker._qbit.assert_not_called()

    def test_missing_episode_enqueue_queues_newest_episode_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_bangumi_ids = []
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(return_value=[{"bangumi_id": 123, "path": str(root / "Show")}])
            releases = [
                MikanRelease(
                    bangumi_id=123,
                    title="[Group] Show - 01 [CHT].mkv",
                    episode=1,
                    torrent_url="https://mikan/ep01.torrent",
                    pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    content_length=100,
                ),
                MikanRelease(
                    bangumi_id=123,
                    title="[Group] Show - 12 [CHT].mkv",
                    episode=12,
                    torrent_url="https://mikan/ep12.torrent",
                    pub_date=datetime(2026, 6, 16, tzinfo=timezone.utc),
                    content_length=100,
                ),
            ]

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", return_value={1, 12}),
                patch("mikan_worker.fetch_bangumi_releases", return_value=releases),
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 2)
            urls = [call.args[0] for call in qbit.add_url.call_args_list]
            self.assertEqual(urls, ["https://mikan/ep12.torrent", "https://mikan/ep01.torrent"])

    def test_missing_episode_scan_checks_only_rss_candidate_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            show = root / "Show"
            show.mkdir()
            (show / "Show - S01E01.mkv").write_bytes(b"video")
            candidate = show / "Show - S01E02.mkv"
            candidate.write_bytes(b"video")
            (show / "Show - S01E03.mkv").write_bytes(b"video")
            config = SimpleNamespace(video_extensions=[".mkv"])

            with patch("mikan_worker._has_official_chinese_subtitle", return_value=False) as has_subtitle:
                missing = _missing_episodes_for_bangumi(
                    config,
                    _logger(),
                    123,
                    [{"bangumi_id": 123, "path": str(show)}],
                    candidate_episodes={2},
                )

            self.assertEqual(missing, {2})
            has_subtitle.assert_called_once_with(candidate)

    def test_missing_episode_scan_stops_when_redownload_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            show = root / "Show"
            show.mkdir()
            first = show / "Show - S01E01.mkv"
            first.write_bytes(b"video")
            second = show / "Show - S01E02.mkv"
            second.write_bytes(b"video")
            config = SimpleNamespace(video_extensions=[".mkv"])
            stop_values = iter([False, False, True])

            with patch("mikan_worker._has_official_chinese_subtitle", return_value=False) as has_subtitle:
                missing = _missing_episodes_for_bangumi(
                    config,
                    _logger(),
                    123,
                    [{"bangumi_id": 123, "path": str(show)}],
                    candidate_episodes={1, 2},
                    stop_callback=lambda: next(stop_values),
                )

            self.assertEqual(missing, {1})
            has_subtitle.assert_called_once_with(first)

    def test_enqueue_stores_release_when_qbit_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(return_value=[])
            worker._qbit = Mock(side_effect=QBitError("maintenance"))

            with patch("mikan_worker.fetch_bangumi_releases", return_value=[_release("https://mikan/offline.torrent")]):
                queued = worker.enqueue_latest_releases()

            self.assertEqual(queued, 0)
            pending = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending["items"]["123:1"]
            self.assertEqual(entry["deferred_torrent_url"], "https://mikan/offline.torrent")
            self.assertNotIn("torrent_url", entry)
            self.assertFalse((root / "mikan_seen.json").exists())

    def test_enqueue_queues_deferred_release_when_qbit_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            pending = {"items": {}}
            release = _release("https://mikan/deferred.torrent")
            _mark_deferred(pending, release, reason="qbit_unavailable")
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(return_value=[])
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker._qbit = Mock(return_value=qbit)

            with patch("mikan_worker.fetch_bangumi_releases", return_value=[]):
                queued = worker.enqueue_latest_releases()

            self.assertEqual(queued, 1)
            qbit.add_url.assert_called_once_with(
                "https://mikan/deferred.torrent",
                save_path="/anime",
                category="llm-sub",
                tags=["mikansub", "mikan"],
                paused=False,
            )
            pending = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending["items"]["123:1"]
            self.assertEqual(entry["torrent_url"], "https://mikan/deferred.torrent")
            self.assertIn("queued_at", entry)
            self.assertNotIn("deferred_torrent_url", entry)
            seen = json.loads((root / "mikan_seen.json").read_text(encoding="utf-8"))
            self.assertIn("https://mikan/deferred.torrent", seen)

    def test_enqueue_queues_range_deferred_release_once_when_qbit_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            pending = {"items": {}}
            release = _range_release("https://mikan/range.torrent")
            _mark_deferred(pending, release, reason="qbit_unavailable")
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(return_value=[])
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker._qbit = Mock(return_value=qbit)

            with patch("mikan_worker.fetch_bangumi_releases", return_value=[]):
                queued = worker.enqueue_latest_releases()

            self.assertEqual(queued, 1)
            qbit.add_url.assert_called_once()
            pending = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            for episode in (1, 2, 3):
                entry = pending["items"][f"123:{episode}"]
                self.assertEqual(entry["torrent_url"], "https://mikan/range.torrent")
                self.assertEqual(entry["episodes"], [1, 2, 3])
                self.assertNotIn("deferred_torrent_url", entry)

    def test_enqueue_stores_release_when_qbit_add_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(return_value=[])
            qbit = Mock()
            qbit.add_url.side_effect = QBitError("connection dropped")
            worker._qbit = Mock(return_value=qbit)

            with patch("mikan_worker.fetch_bangumi_releases", return_value=[_release("https://mikan/test.torrent")]):
                queued = worker.enqueue_latest_releases()

            self.assertEqual(queued, 0)
            qbit.add_url.assert_called_once()
            self.assertEqual(qbit.add_url.call_args.kwargs["tags"], ["mikansub", "mikan"])
            self.assertFalse((root / "mikan_seen.json").exists())
            pending = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending["items"]["123:1"]
            self.assertEqual(entry["deferred_torrent_url"], "https://mikan/test.torrent")
            self.assertEqual(entry["deferred_reason"], "qbit_add_failed")
            self.assertNotIn("torrent_url", entry)

    def test_mark_pending_marks_every_episode_in_range_release(self) -> None:
        pending = {"items": {}}
        release = _range_release("https://mikan/range.torrent")

        _mark_pending(pending, release)

        self.assertEqual(set(pending["items"]), {"123:1", "123:2", "123:3"})
        for episode in (1, 2, 3):
            entry = pending["items"][f"123:{episode}"]
            self.assertEqual(entry["torrent_url"], "https://mikan/range.torrent")
            self.assertEqual(entry["episodes"], [1, 2, 3])
            self.assertIsNone(entry["pub_date"])

    def test_mark_pending_stores_release_pub_date_for_priority(self) -> None:
        pending = {"items": {}}
        release = MikanRelease(
            bangumi_id=123,
            title="[Group] Show - 12 [CHT].mkv",
            episode=12,
            torrent_url="https://mikan/ep12.torrent",
            pub_date=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc),
            content_length=100,
        )

        _mark_pending(pending, release)

        self.assertEqual(pending["items"]["123:12"]["pub_date"], "2026-06-16T12:00:00+00:00")

    def test_extract_jobs_claim_newer_pending_release_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            config = _mikan_process_config(root, download_root)
            old_torrent = QBitTorrent(
                hash="hash-old",
                name="[Group] Show - 01 [CHT].mkv",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Show - 01.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            new_torrent = QBitTorrent(
                hash="hash-new",
                name="[Group] Show - 12 [CHT].mkv",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Show - 12.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            old_entries = [
                {
                    "bangumi_id": 123,
                    "episode": 1,
                    "queued_at": "2026-01-01T00:00:00+00:00",
                    "pub_date": "2026-01-01T00:00:00+00:00",
                }
            ]
            new_entries = [
                {
                    "bangumi_id": 123,
                    "episode": 12,
                    "queued_at": "2026-06-16T00:00:00+00:00",
                    "pub_date": "2026-06-16T00:00:00+00:00",
                }
            ]

            _upsert_mikan_extract_jobs(
                config,
                [
                    (old_torrent, old_entries, _pending_extract_priority(old_entries), False),
                    (new_torrent, new_entries, _pending_extract_priority(new_entries), False),
                ],
                state_required=True,
            )

            jobs = _claim_mikan_extract_jobs(config, limit=2)

            self.assertEqual([job.torrent.hash for job in jobs], ["hash-new", "hash-old"])

    def test_choose_release_skips_failed_and_seen_urls(self) -> None:
        first = _release("https://mikan/first.torrent")
        second = _release("https://mikan/second.torrent")
        third = _release("https://mikan/third.torrent")
        pending = {
            "items": {
                "123:1": {
                    "bangumi_id": 123,
                    "episode": 1,
                    "failed_urls": [first.torrent_url],
                }
            }
        }
        seen = {second.torrent_url: {"title": second.title}}

        selected = _choose_release_for_episode(123, 1, [first, second, third], seen, pending)

        self.assertEqual(selected, third)

    def test_choose_release_skips_same_failed_info_hash_from_another_source(self) -> None:
        info_hash = "0123456789abcdef0123456789abcdef01234567"
        candidate = MikanRelease(
            bangumi_id=123,
            title="[External] Test Anime - 01 [CHT][MKV]",
            episode=1,
            torrent_url=f"magnet:?xt=urn:btih:{info_hash}",
            pub_date=None,
            content_length=100,
            source="dmhy",
            info_hash=info_hash,
        )
        pending = {
            "items": {
                "123:1": {
                    "bangumi_id": 123,
                    "episode": 1,
                    "failed_urls": [f"https://mikanani.me/Download/20260622/{info_hash}.torrent"],
                }
            }
        }

        selected = _choose_release_for_episode(123, 1, [candidate], {}, pending)

        self.assertIsNone(selected)

    def test_choose_release_defers_unverified_cross_season_fallback(self) -> None:
        candidate = MikanRelease(
            bangumi_id=123,
            title="[External] Test Show S3 - 01 [CHT][MKV]",
            episode=1,
            torrent_url="https://fallback/test-show-s3-01.torrent",
            pub_date=None,
            content_length=100,
            source="dmhy",
        )
        reasons: list[str] = []

        selected = _choose_release_for_episode(
            123,
            1,
            [candidate],
            {},
            {"items": {}},
            mappings=[{
                "bangumi_id": 123,
                "path": "/anime/Test Show",
                "title": "Test Show",
                "match": ["Test Show"],
            }],
            ambiguity_reasons=reasons,
        )

        self.assertIsNone(selected)
        self.assertTrue(any("unverified_explicit_season:3" in reason for reason in reasons))

    def test_choose_release_allows_exact_fallback_identity_in_verified_season(self) -> None:
        candidate = MikanRelease(
            bangumi_id=123,
            title="[External] Test Show S3 - 01 [CHT][MKV]",
            episode=1,
            torrent_url="https://fallback/test-show-s3-01.torrent",
            pub_date=None,
            content_length=100,
            source="dmhy",
        )
        reasons: list[str] = []

        selected = _choose_release_for_episode(
            123,
            1,
            [candidate],
            {},
            {"items": {}},
            mappings=[{
                "bangumi_id": 123,
                "path": "/anime/Test Show/Season 3",
                "title": "Test Show",
                "match": ["Test Show"],
            }],
            ambiguity_reasons=reasons,
        )

        self.assertEqual(selected, candidate)
        self.assertEqual(reasons, [])

    def test_choose_release_defers_when_candidate_set_contains_named_sequel(self) -> None:
        exact = MikanRelease(
            bangumi_id=123,
            title="[External] Test Show - 01 [CHT][MKV]",
            episode=1,
            torrent_url="https://fallback/test-show-01.torrent",
            pub_date=None,
            content_length=100,
            source="dmhy",
        )
        sequel = MikanRelease(
            bangumi_id=123,
            title="[External] Test Show Revenge - 01 [CHT][MKV]",
            episode=1,
            torrent_url="https://fallback/test-show-revenge-01.torrent",
            pub_date=None,
            content_length=100,
            source="nyaa",
        )
        reasons: list[str] = []

        selected = _choose_release_for_episode(
            123,
            1,
            [exact, sequel],
            {},
            {"items": {}},
            mappings=[{
                "bangumi_id": 123,
                "path": "/anime/Test Show",
                "title": "Test Show",
                "match": ["Test Show"],
            }],
            ambiguity_reasons=reasons,
        )

        self.assertIsNone(selected)
        self.assertTrue(any("series_identity_has_sequel_suffix" in reason for reason in reasons))

    def test_enqueue_uses_fallback_when_mikan_candidate_is_not_extractable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_require_extractable_subtitle = True
            config.mikan_fallback_sources_enabled = True
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(root / "Test Show"), "title": "Test Show", "match": ["Test Show"]}]
            )
            worker._fallback_sources = Mock()
            worker._fallback_sources.search.return_value = [
                MikanRelease(
                    bangumi_id=123,
                    title="[External] Test Show - 01 [CHT][MKV]",
                    episode=1,
                    torrent_url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
                    pub_date=None,
                    content_length=100,
                    source="dmhy",
                    info_hash="0123456789abcdef0123456789abcdef01234567",
                )
            ]
            mikan_hardsub = MikanRelease(
                bangumi_id=123,
                title="[Mikan] Test Show - 01 [CHT][MP4]",
                episode=1,
                torrent_url="https://mikan/hardsub.torrent",
                pub_date=None,
                content_length=100,
            )

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", return_value={1}),
                patch("mikan_worker.fetch_bangumi_releases", return_value=[mikan_hardsub]),
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 1)
            qbit.add_url.assert_called_once()
            self.assertTrue(qbit.add_url.call_args.args[0].startswith("magnet:?"))
            worker._fallback_sources.search.assert_called_once()

    def test_enqueue_does_not_advance_no_candidate_retry_when_fallback_budget_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_require_extractable_subtitle = True
            config.mikan_fallback_sources_enabled = True
            config.mikan_no_candidate_retry_seconds = 600
            config.mikan_no_candidate_retry_max_seconds = 86400
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(root / "Test Show"), "title": "Test Show"}]
            )
            worker._fallback_sources = Mock()
            worker._fallback_sources.search.return_value = FallbackSearchResult(
                conclusive=False,
                deferred_reason="lookup_budget_exhausted",
            )
            mikan_hardsub = MikanRelease(
                bangumi_id=123,
                title="[Mikan] Test Show - 01 [CHT][MP4]",
                episode=1,
                torrent_url="https://mikan/hardsub.torrent",
                pub_date=None,
                content_length=100,
            )

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", return_value={1}),
                patch("mikan_worker.fetch_bangumi_releases", return_value=[mikan_hardsub]),
                patch.object(worker, "_mark_no_candidate_retry_with_state_lock") as mark_retry,
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 0)
            mark_retry.assert_not_called()
            qbit.add_url.assert_not_called()

    def test_enqueue_does_not_advance_no_candidate_retry_for_partial_empty_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_require_extractable_subtitle = True
            config.mikan_fallback_sources_enabled = True
            config.mikan_no_candidate_retry_seconds = 600
            config.mikan_no_candidate_retry_max_seconds = 86400
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{
                    "bangumi_id": 123,
                    "path": str(root / "Test Show"),
                    "title": "Test Show",
                    "match": ["Test Show"],
                }]
            )
            worker._fallback_sources = Mock()
            worker._fallback_sources.search.return_value = FallbackSearchResult(
                conclusive=False,
                lookup_performed=True,
                deferred_reason="partial_provider_coverage",
                successful_sources=("dmhy",),
                failed_sources=("acgrip",),
            )
            mikan_hardsub = MikanRelease(
                bangumi_id=123,
                title="[Mikan] Test Show - 01 [CHT][MP4]",
                episode=1,
                torrent_url="https://mikan/hardsub.torrent",
                pub_date=None,
                content_length=100,
            )

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", return_value={1}),
                patch("mikan_worker.fetch_bangumi_releases", return_value=[mikan_hardsub]),
                patch.object(worker, "_mark_no_candidate_retry_with_state_lock") as mark_retry,
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 0)
            mark_retry.assert_not_called()
            qbit.add_url.assert_not_called()

    def test_discovery_outage_retries_only_known_episode_and_uses_safe_partial_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_fallback_sources_enabled = True
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{
                    "bangumi_id": 123,
                    "path": str(root / "Test Show"),
                    "title": "Test Show",
                    "match": ["Test Show"],
                }]
            )
            _save_pending(
                worker.pending_path,
                {
                    "version": 1,
                    "items": {
                        "123:1": {
                            "bangumi_id": 123,
                            "episode": 1,
                            "failed_urls": ["https://mikan/failed-source.torrent"],
                            "last_failure_reason": "extract_failed",
                        }
                    },
                },
            )
            safe_fallback = MikanRelease(
                bangumi_id=123,
                title="[External] Test Show - 01 [CHT][MKV]",
                episode=1,
                torrent_url="https://fallback/test-show-01.torrent",
                pub_date=None,
                content_length=100,
                source="dmhy",
            )
            worker._fallback_sources = Mock()
            worker._fallback_sources.search.return_value = FallbackSearchResult(
                [safe_fallback],
                conclusive=False,
                lookup_performed=True,
                deferred_reason="partial_provider_coverage",
                successful_sources=("dmhy",),
                failed_sources=("acgrip",),
            )

            with (
                patch(
                    "mikan_worker.fetch_bangumi_releases",
                    side_effect=requests.ConnectTimeout("primary discovery unavailable"),
                ),
                patch("mikan_worker._missing_episodes_for_bangumi", return_value={1}) as scan_missing,
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 1)
            scan_missing.assert_called_once()
            worker._fallback_sources.search.assert_called_once()
            qbit.add_url.assert_called_once()
            self.assertEqual(qbit.add_url.call_args.args[0], safe_fallback.torrent_url)

    def test_ambiguous_fallback_is_persisted_for_review_without_no_candidate_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_require_extractable_subtitle = True
            config.mikan_fallback_sources_enabled = True
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{
                    "bangumi_id": 123,
                    "path": str(root / "Test Show"),
                    "title": "Test Show",
                    "match": ["Test Show"],
                }]
            )
            ambiguous = MikanRelease(
                bangumi_id=123,
                title="[External] Test Show S3 - 01 [CHT][MKV]",
                episode=1,
                torrent_url="https://fallback/test-show-s3-01.torrent",
                pub_date=None,
                content_length=100,
                source="dmhy",
            )
            worker._fallback_sources = Mock()
            worker._fallback_sources.search.return_value = FallbackSearchResult(
                [ambiguous],
                conclusive=True,
                lookup_performed=True,
                successful_sources=("dmhy",),
            )
            mikan_hardsub = MikanRelease(
                bangumi_id=123,
                title="[Mikan] Test Show - 01 [CHT][MP4]",
                episode=1,
                torrent_url="https://mikan/hardsub.torrent",
                pub_date=None,
                content_length=100,
            )

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", return_value={1}),
                patch("mikan_worker.fetch_bangumi_releases", return_value=[mikan_hardsub]),
                patch.object(worker, "_mark_no_candidate_retry_with_state_lock") as mark_retry,
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 0)
            mark_retry.assert_not_called()
            qbit.add_url.assert_not_called()
            payload = json.loads(worker.pending_path.read_text(encoding="utf-8"))
            entry = payload["items"]["123:1"]
            self.assertEqual(entry["candidate_review_reason"], "ambiguous_release_identity")
            self.assertTrue(
                any(
                    "unverified_explicit_season:3" in reason
                    for reason in entry["candidate_review_details"]
                )
            )
            self.assertNotIn("no_candidate_until", entry)

    def test_enqueue_advances_no_candidate_retry_after_conclusive_empty_fallback_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_require_extractable_subtitle = True
            config.mikan_fallback_sources_enabled = True
            config.mikan_no_candidate_retry_seconds = 600
            config.mikan_no_candidate_retry_max_seconds = 86400
            qbit = Mock()
            qbit.list_torrents.return_value = []
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(root / "Test Show"), "title": "Test Show"}]
            )
            worker._fallback_sources = Mock()
            worker._fallback_sources.search.return_value = FallbackSearchResult(
                conclusive=True,
                lookup_performed=True,
                successful_sources=("dmhy",),
            )
            mikan_hardsub = MikanRelease(
                bangumi_id=123,
                title="[Mikan] Test Show - 01 [CHT][MP4]",
                episode=1,
                torrent_url="https://mikan/hardsub.torrent",
                pub_date=None,
                content_length=100,
            )

            with (
                patch("mikan_worker._missing_episodes_for_bangumi", return_value={1}),
                patch("mikan_worker.fetch_bangumi_releases", return_value=[mikan_hardsub]),
                patch.object(
                    worker,
                    "_mark_no_candidate_retry_with_state_lock",
                    return_value={1: 600},
                ) as mark_retry,
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 0)
            mark_retry.assert_called_once_with(123, [1], state_required=True)

    def test_replacement_does_not_advance_no_candidate_retry_when_all_providers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_fallback_sources_enabled = True
            config.mikan_no_candidate_retry_seconds = 600
            config.mikan_no_candidate_retry_max_seconds = 86400
            qbit = Mock()
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(root / "Test Show"), "title": "Test Show"}]
            )
            worker._fallback_sources = Mock()
            worker._fallback_sources.enabled = True
            worker._fallback_sources.search.return_value = FallbackSearchResult(
                conclusive=False,
                lookup_performed=True,
                deferred_reason="all_providers_failed",
                failed_sources=("acgrip",),
            )

            with (
                patch("mikan_worker.fetch_bangumi_releases", return_value=[]),
                patch.object(worker, "_mark_no_candidate_retry_with_state_lock") as mark_retry,
            ):
                queued = worker._enqueue_replacements_after_extract_failure_unlocked(
                    [MikanReplacementTarget(123, 1)],
                    qbit,
                    queue_lock_held=True,
                )

            self.assertEqual(queued, 0)
            mark_retry.assert_not_called()
            qbit.add_url.assert_not_called()

    def test_expire_stalled_pending_deletes_unstarted_torrent_and_keeps_failed_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=Path(temp_dir),
                qbit_tags=["mikansub"],
                qbit_category="llm-sub",
                mikan_download_start_timeout_seconds=600,
                mikan_delete_stalled_torrents=True,
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/stalled.torrent")
            _mark_pending(pending, release)
            pending["items"]["123:1"]["queued_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            torrent = QBitTorrent(
                hash="hash1",
                name=release.title,
                progress=0.0,
                state="stalledDL",
                dlspeed=0,
                downloaded=0,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]

            expired = worker._expire_stalled_pending(qbit, pending)

            self.assertEqual(expired, 1)
            qbit.delete_torrents.assert_called_once_with(["hash1"], delete_files=True)
            entry = pending["items"]["123:1"]
            self.assertEqual(entry["failed_urls"], [release.torrent_url])
            self.assertNotIn("torrent_url", entry)
            self.assertNotIn("tag", entry)

    def test_expire_stalled_pending_keeps_qbit_queued_torrent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=Path(temp_dir),
                qbit_tags=["mikansub"],
                qbit_category="llm-sub",
                mikan_download_start_timeout_seconds=60,
                mikan_delete_stalled_torrents=True,
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/qbit-queued.torrent")
            _mark_pending(pending, release)
            pending["items"]["123:1"]["queued_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=30)
            ).isoformat()
            torrent = QBitTorrent(
                hash="hash-queued",
                name=release.title,
                progress=0.0,
                state="queuedDL",
                dlspeed=0,
                downloaded=0,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]

            expired = worker._expire_stalled_pending(qbit, pending)

            self.assertEqual(expired, 0)
            qbit.delete_torrents.assert_not_called()
            self.assertEqual(pending["items"]["123:1"]["torrent_url"], release.torrent_url)

    def test_expire_stalled_pending_gives_metadata_fetch_a_longer_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=Path(temp_dir),
                qbit_tags=["mikansub"],
                qbit_category="llm-sub",
                mikan_download_start_timeout_seconds=60,
                mikan_download_metadata_timeout_seconds=300,
                mikan_delete_stalled_torrents=True,
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/metadata.torrent")
            _mark_pending(pending, release)
            pending["items"]["123:1"]["queued_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=120)
            ).isoformat()
            torrent = QBitTorrent(
                hash="hash-metadata",
                name=release.title,
                progress=0.0,
                state="metaDL",
                dlspeed=0,
                downloaded=0,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]

            expired = worker._expire_stalled_pending(qbit, pending)

            self.assertEqual(expired, 0)
            qbit.delete_torrents.assert_not_called()

            pending["items"]["123:1"]["queued_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=301)
            ).isoformat()
            expired = worker._expire_stalled_pending(qbit, pending)

            self.assertEqual(expired, 1)
            qbit.delete_torrents.assert_called_once_with(["hash-metadata"], delete_files=True)

    def test_expire_stalled_pending_deletes_started_zero_speed_torrent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=Path(temp_dir),
                qbit_tags=["mikansub"],
                qbit_category="llm-sub",
                mikan_download_start_timeout_seconds=600,
                mikan_delete_stalled_torrents=True,
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/started.torrent")
            _mark_pending(pending, release)
            pending["items"]["123:1"]["queued_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            torrent = QBitTorrent(
                hash="hash1",
                name=release.title,
                progress=0.01,
                state="downloading",
                dlspeed=0,
                downloaded=1024,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]

            expired = worker._expire_stalled_pending(qbit, pending)

            self.assertEqual(expired, 1)
            qbit.delete_torrents.assert_called_once_with(["hash1"], delete_files=True)
            self.assertNotIn("torrent_url", pending["items"]["123:1"])

    def test_expire_stalled_pending_switches_started_torrent_without_recent_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=Path(temp_dir),
                qbit_tags=["mikansub"],
                qbit_category="llm-sub",
                mikan_download_start_timeout_seconds=600,
                mikan_download_stall_timeout_seconds=1800,
                mikan_delete_stalled_torrents=True,
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/stalled-after-start.torrent")
            _mark_pending(pending, release)
            entry = pending["items"]["123:1"]
            entry["queued_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            entry["last_downloaded"] = 1024
            entry["last_progress"] = 0.1
            entry["last_progress_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            torrent = QBitTorrent(
                hash="hash1",
                name=release.title,
                progress=0.1,
                state="stalledDL",
                dlspeed=0,
                downloaded=1024,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]

            expired = worker._expire_stalled_pending(qbit, pending)

            self.assertEqual(expired, 1)
            qbit.delete_torrents.assert_called_once_with(["hash1"], delete_files=True)
            entry = pending["items"]["123:1"]
            self.assertEqual(entry["failed_urls"], [release.torrent_url])
            self.assertNotIn("torrent_url", entry)
            self.assertNotIn("last_progress_at", entry)

    def test_enqueue_latest_releases_replaces_stalled_before_library_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_download_stall_timeout_seconds = 1800
            config.mikan_max_items_per_bangumi = 12
            worker = MikanWorker(config, _logger())
            stalled = _episode_release("https://mikan/stalled.torrent", "[Mikan] Test Anime - 01 [CHT]", 1)
            replacement = _episode_release("https://mikan/replacement.torrent", "[Mikan] Test Anime - 01 [CHT][v2]", 1)
            pending = {"items": {}}
            _mark_pending(pending, stalled)
            entry = pending["items"]["123:1"]
            entry["queued_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            entry["last_downloaded"] = 1024
            entry["last_progress"] = 0.1
            entry["last_progress_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            (root / "mikan_seen.json").write_text(json.dumps({stalled.torrent_url: {"title": stalled.title}}), encoding="utf-8")
            torrent = QBitTorrent(
                hash="hash1",
                name=stalled.title,
                progress=0.1,
                state="stalledDL",
                dlspeed=0,
                downloaded=1024,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(return_value=[])

            def assert_replaced_before_scan(config_arg, logger_arg, mappings_arg):
                qbit.add_url.assert_called_once_with(
                    replacement.torrent_url,
                    save_path="/anime",
                    category="llm-sub",
                    tags=["mikansub", "mikan"],
                    paused=False,
                )
                return []

            with (
                patch("mikan_worker.fetch_bangumi_releases", return_value=[stalled, replacement]) as fetch,
                patch("mikan_worker._library_scan_series_mappings", side_effect=assert_replaced_before_scan),
            ):
                queued = worker._enqueue_latest_releases_unlocked()

            self.assertEqual(queued, 1)
            fetch.assert_called()
            qbit.delete_torrents.assert_called_once_with(["hash1"], delete_files=True)
            qbit.add_url.assert_called_once()
            pending_after = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending_after["items"]["123:1"]
            self.assertEqual(entry["failed_urls"], [stalled.torrent_url])
            self.assertEqual(entry["torrent_url"], replacement.torrent_url)
            self.assertEqual(entry["title"], replacement.title)

    def test_save_pending_mirrors_download_state_to_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending_path = root / "mikan_pending.json"
            pending = {"items": {}}
            first = _release("https://mikan/first.torrent")
            second = _release("https://mikan/second.torrent")
            _mark_pending(pending, first)
            entry = pending["items"]["123:1"]
            entry["last_qbit_state"] = "stalledDL"
            entry["last_dlspeed"] = 0
            entry["last_progress"] = 0.2
            entry["last_downloaded"] = 100
            entry["last_qbit_sync_at"] = datetime.now(timezone.utc).isoformat()

            _save_pending(pending_path, pending)
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                initial_event_count = conn.execute(
                    "SELECT COUNT(*) FROM mikan_download_events"
                ).fetchone()[0]
            finally:
                conn.close()

            # qBittorrent heartbeats are state, not user-facing history.
            entry["last_qbit_state"] = "downloading"
            entry["last_qbit_sync_at"] = datetime.now(timezone.utc).isoformat()
            _save_pending(pending_path, pending)
            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                after_heartbeat_count = conn.execute(
                    "SELECT COUNT(*) FROM mikan_download_events"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(after_heartbeat_count, initial_event_count)

            _mark_pending(pending, second)
            pending["items"]["123:1"]["failed_urls"] = [first.torrent_url]
            _save_pending(pending_path, pending)

            db_path = root / "mikan_state.sqlite3"
            self.assertTrue(db_path.exists())
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT status, torrent_url, last_qbit_state, next_action, failed_count
                    FROM mikan_download_items
                    WHERE key = '123:1'
                    """
                ).fetchone()
                events = conn.execute(
                    "SELECT event FROM mikan_download_events ORDER BY id"
                ).fetchall()
                event_details = conn.execute(
                    "SELECT detail, detail_json FROM mikan_download_events ORDER BY id"
                ).fetchall()
            finally:
                conn.close()

            self.assertEqual(row[0], "queued")
            self.assertEqual(row[1], second.torrent_url)
            self.assertEqual(row[3], "wait_qbit_start")
            self.assertEqual(row[4], 1)
            self.assertIn(("created",), events)
            self.assertIn(("failure_recorded",), events)
            self.assertNotIn(("qbit_state_changed",), events)
            self.assertTrue(all("https://" not in detail for detail, _ in event_details))
            self.assertTrue(all(isinstance(json.loads(detail_json), dict) for _, detail_json in event_details))

            _save_pending(pending_path, pending)
            conn = sqlite3.connect(db_path)
            try:
                unchanged_event_count = conn.execute("SELECT COUNT(*) FROM mikan_download_events").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(unchanged_event_count, len(events))

            pending["items"].clear()
            _save_pending(pending_path, pending)
            conn = sqlite3.connect(db_path)
            try:
                remaining_rows = conn.execute("SELECT COUNT(*) FROM mikan_download_items").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(remaining_rows, 0)

    def test_legacy_mikan_events_are_compacted_and_coalesced_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            database = root / "mikan_state.sqlite3"
            conn = sqlite3.connect(database)
            try:
                conn.executescript(
                    """
                    CREATE TABLE mikan_download_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT NOT NULL,
                        bangumi_id INTEGER,
                        episode INTEGER,
                        event TEXT NOT NULL,
                        detail TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL
                    );
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO mikan_download_events(
                        key, bangumi_id, episode, event, detail, created_at
                    ) VALUES('123:1', 123, 1, 'qbit_state_changed', ?, ?)
                    """,
                    [
                        ("status=downloading source=https://mikan.example/secret-one.torrent", 1000.0),
                        ("status=downloading source=https://mikan.example/secret-one.torrent", 1100.0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            initialized = _mikan_state_connect(config)
            initialized.close()

            conn = sqlite3.connect(database)
            try:
                rows = conn.execute(
                    """
                    SELECT detail, occurrence_count, last_seen_at
                    FROM mikan_download_events
                    """
                ).fetchall()
                marker = conn.execute(
                    "SELECT value FROM mikan_state_meta WHERE key='download_events_compacted_v2'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], 2)
            self.assertEqual(rows[0][2], 1100.0)
            self.assertNotIn("https://", rows[0][0])
            self.assertEqual(marker, ("1",))

    def test_save_pending_keeps_json_when_state_db_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pending_path = Path(temp_dir) / "mikan_pending.json"
            pending = {"items": {}}
            _mark_pending(pending, _release("https://mikan/source.torrent"))

            with patch("mikan_worker._sync_mikan_state_db", side_effect=sqlite3.OperationalError("database is locked")):
                _save_pending(pending_path, pending)

            saved = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["items"]["123:1"]["torrent_url"], "https://mikan/source.torrent")

    def test_explicit_state_db_rebuild_from_existing_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending = {"items": {}}
            _mark_pending(pending, _release("https://mikan/startup.torrent"))
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            _ensure_mikan_state_db_for_pending(root / "mikan_pending.json")

            conn = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                row = conn.execute("SELECT torrent_url FROM mikan_download_items WHERE key = '123:1'").fetchone()
                meta = {
                    key: value
                    for key, value in conn.execute(
                        "SELECT key, value FROM mikan_state_meta WHERE key IN ('pending_mtime_ns', 'pending_size')"
                    )
                }
            finally:
                conn.close()
            pending_stat = (root / "mikan_pending.json").stat()
            self.assertEqual(row[0], "https://mikan/startup.torrent")
            self.assertEqual(meta["pending_mtime_ns"], str(pending_stat.st_mtime_ns))
            self.assertEqual(meta["pending_size"], str(pending_stat.st_size))

    def test_expire_stalled_pending_keeps_started_torrent_with_new_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(
                mikan_seen_path="mikan_seen.json",
                mikan_pending_path="mikan_pending.json",
                work_path=Path(temp_dir),
                qbit_tags=["mikansub"],
                qbit_category="llm-sub",
                mikan_download_start_timeout_seconds=600,
                mikan_download_stall_timeout_seconds=1800,
                mikan_delete_stalled_torrents=True,
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/progressing.torrent")
            _mark_pending(pending, release)
            entry = pending["items"]["123:1"]
            entry["queued_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            entry["last_downloaded"] = 1024
            entry["last_progress"] = 0.1
            entry["last_progress_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            torrent = QBitTorrent(
                hash="hash1",
                name=release.title,
                progress=0.2,
                state="stalledDL",
                dlspeed=0,
                downloaded=2048,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]

            expired = worker._expire_stalled_pending(qbit, pending)

            self.assertEqual(expired, 0)
            qbit.delete_torrents.assert_not_called()
            self.assertEqual(pending["items"]["123:1"]["last_downloaded"], 2048)

    def test_sync_pending_download_progress_records_qbit_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/progress.torrent")
            _mark_pending(pending, release)
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash="hash-progress",
                name=release.title,
                progress=0.55,
                state="downloading",
                dlspeed=2048,
                downloaded=123456,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )

            changed = worker._sync_pending_download_progress_from_torrents(
                [torrent],
                [{"bangumi_id": 123, "path": str(root), "match": ["Test Anime"]}],
                state_required=True,
            )

            self.assertEqual(changed, 1)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = saved["items"]["123:1"]
            self.assertEqual(entry["last_progress"], 0.55)
            self.assertEqual(entry["last_downloaded"], 123456)
            self.assertEqual(entry["last_dlspeed"], 2048)
            self.assertEqual(entry["last_qbit_state"], "downloading")
            self.assertEqual(entry["last_qbit_hash"], "hash-progress")
            self.assertEqual(entry["last_qbit_name"], release.title)
            self.assertIn("last_qbit_sync_at", entry)

    def test_sync_pending_download_progress_clears_stale_remembered_qbit_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            release = _release("https://mikan/progress.torrent")
            _mark_pending(pending, release)
            entry = pending["items"]["123:1"]
            entry.update(
                {
                    "last_progress": 0.75,
                    "last_downloaded": 777,
                    "last_dlspeed": 1024,
                    "last_qbit_state": "downloading",
                    "last_qbit_hash": "hash-wrong",
                    "last_qbit_name": "Wrong Show - 01",
                    "last_qbit_sync_at": "2026-06-09T00:00:00+00:00",
                }
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            wrong_torrent = QBitTorrent(
                hash="hash-wrong",
                name="[Group] Wrong Show - 01 [WebRip 1080p].mkv",
                progress=0.92,
                state="downloading",
                dlspeed=4096,
                downloaded=999999,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )

            changed = worker._sync_pending_download_progress_from_torrents(
                [wrong_torrent],
                [{"bangumi_id": 123, "path": str(root), "match": ["Test Anime"]}],
                state_required=True,
            )

            self.assertEqual(changed, 1)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = saved["items"]["123:1"]
            self.assertNotIn("last_qbit_hash", entry)
            self.assertNotIn("last_progress", entry)
            self.assertNotIn("last_qbit_state", entry)

    def test_terminal_success_cannot_be_reopened_by_automatic_mark_pending(self) -> None:
        pending = {"items": {}}
        original = _release("https://mikan/original.torrent")
        _mark_pending(pending, original)
        entry = pending["items"]["123:1"]
        entry.update(
            {
                "completed_at": "2026-07-15T00:00:00+00:00",
                "last_extracted_at": "2026-07-15T00:00:00+00:00",
                "last_extracted_count": 1,
                "total_extracted_count": 1,
            }
        )
        entry.pop("torrent_url")
        entry.pop("queued_at")

        _mark_pending(pending, _release("https://mikan/recovered-again.torrent"))

        self.assertNotIn("torrent_url", entry)
        self.assertNotIn("queued_at", entry)
        self.assertEqual(entry["total_extracted_count"], 1)

    def test_terminal_success_can_only_be_reopened_explicitly(self) -> None:
        pending = {"items": {}}
        _mark_pending(pending, _release("https://mikan/original.torrent"))
        entry = pending["items"]["123:1"]
        entry.update(
            {
                "completed_at": "2026-07-15T00:00:00+00:00",
                "last_extracted_at": "2026-07-15T00:00:00+00:00",
                "last_extracted_count": 1,
                "total_extracted_count": 1,
            }
        )

        _mark_pending(
            pending,
            _release("https://mikan/manual-reextract.torrent"),
            allow_completed_reopen=True,
        )

        self.assertEqual(entry["torrent_url"], "https://mikan/manual-reextract.torrent")
        self.assertNotIn("completed_at", entry)
        self.assertEqual(len(entry["completed_history"]), 1)

    def test_terminal_completion_repair_detaches_and_stops_redundant_qbit_torrent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            info_hash = "0123456789abcdef0123456789abcdef01234567"
            pending = {"items": {}}
            _mark_pending(pending, _release(f"magnet:?xt=urn:btih:{info_hash}"))
            entry = pending["items"]["123:1"]
            entry.update(
                {
                    "last_qbit_hash": info_hash,
                    "last_qbit_state": "downloading",
                    "completed_at": "2026-07-15T00:00:00+00:00",
                    "last_extracted_at": "2026-07-15T00:00:00+00:00",
                    "last_extracted_count": 1,
                    "total_extracted_count": 1,
                    "failed_urls": ["https://mikan/bad.torrent"],
                    "last_failure_reason": "eta too long",
                }
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash=info_hash,
                name="[Group] Test Anime - 01",
                progress=0.4,
                state="downloading",
                dlspeed=100,
                downloaded=10,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            worker._qbit = Mock(return_value=qbit)

            repaired = worker._repair_terminal_completed_pending_entries()

            self.assertEqual(repaired, 1)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            repaired_entry = saved["items"]["123:1"]
            self.assertNotIn("torrent_url", repaired_entry)
            self.assertNotIn("queued_at", repaired_entry)
            self.assertNotIn("failed_urls", repaired_entry)
            self.assertEqual(repaired_entry["completion_state_repair_qbit_status"], "stopped")
            qbit.add_tags.assert_called_once_with(info_hash, ["mikansub-superseded"])
            qbit.stop_torrents.assert_called_once_with([info_hash])
            qbit.delete_torrents.assert_not_called()

    def test_terminal_completion_repair_does_not_stop_hash_shared_with_incomplete_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            info_hash = "fedcba9876543210fedcba9876543210fedcba98"
            pending = {
                "items": {
                    "123:1": {
                        "bangumi_id": 123,
                        "episode": 1,
                        "torrent_url": f"magnet:?xt=urn:btih:{info_hash}",
                        "queued_at": "2026-07-15T00:00:00+00:00",
                        "last_qbit_hash": info_hash,
                        "completed_at": "2026-07-15T00:00:00+00:00",
                        "last_extracted_at": "2026-07-15T00:00:00+00:00",
                        "last_extracted_count": 1,
                        "total_extracted_count": 1,
                    },
                    "123:2": {
                        "bangumi_id": 123,
                        "episode": 2,
                        "torrent_url": f"magnet:?xt=urn:btih:{info_hash}",
                        "queued_at": "2026-07-15T00:00:00+00:00",
                        "last_qbit_hash": info_hash,
                    },
                }
            }
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            worker._qbit = Mock()

            repaired = worker._repair_terminal_completed_pending_entries()

            self.assertEqual(repaired, 1)
            worker._qbit.assert_not_called()
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            self.assertIn("torrent_url", saved["items"]["123:2"])

    def test_poll_download_progress_detects_completed_pending_torrent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_completed_tags = ["mikansub-completed"]
            worker = MikanWorker(config, _logger())
            (root / "Release Show - S01E08.mkv").write_bytes(b"library video")
            release = _episode_release(
                "https://mikan/completed.torrent",
                "[Group] Release Show - 08 [SRTx2]",
                8,
            )
            pending = {"items": {}}
            _mark_pending(pending, release)
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash="hash-completed",
                name=release.title,
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(root), "match": ["Release Show"]}]
            )

            result = worker.poll_download_progress(state_required=True)

            self.assertEqual(result.synced_progress_count, 1)
            self.assertEqual(result.completed_pending_count, 1)
            self.assertEqual(
                qbit.add_tags.call_args_list,
                [call("hash-completed", ["mikan"]), call("hash-completed", ["mikansub-completed"])],
            )

            repeated = worker.poll_download_progress(state_required=True)

            self.assertEqual(repeated.completed_pending_count, 0)
            self.assertEqual(repeated.claimable_extract_count, 1)

    def test_reconcile_promotes_deferred_existing_qbit_torrent_to_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            release = _episode_release(
                "https://mikan/deferred-existing.torrent",
                "[Group] Release Show - 08 [SRTx2]",
                8,
            )
            pending = {"items": {}}
            _mark_deferred(pending, release, reason="qbit_add_failed")
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash="0123456789abcdef0123456789abcdef01234567",
                name=release.title,
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            worker = MikanWorker(config, _logger())

            restored = worker._reconcile_pending_with_existing_torrents(
                [torrent],
                [],
                state_required=True,
            )

            self.assertEqual(restored, 1)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = saved["items"]["123:8"]
            self.assertEqual(entry["torrent_url"], release.torrent_url)
            self.assertEqual(entry["info_hash"], torrent.hash)
            self.assertNotIn("deferred_torrent_url", entry)

    def test_reconcile_restores_completed_torrent_marked_did_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            release = _episode_release(
                "https://mikan/completed-after-timeout.torrent",
                "[Group] Release Show - 08 [SRTx2]",
                8,
            )
            pending = {
                "items": {
                    "123:8": {
                        "bangumi_id": 123,
                        "episode": 8,
                        "episodes": [8],
                        "failed_urls": [release.torrent_url],
                        "last_failure_reason": "did not start",
                        "last_failed_torrent_url": release.torrent_url,
                        "last_failed_title": release.title,
                    }
                }
            }
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash="0123456789abcdef0123456789abcdef01234567",
                name=release.title,
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub,mikansub-extracted",
            )
            worker = MikanWorker(config, _logger())

            restored = worker._reconcile_pending_with_existing_torrents(
                [torrent],
                [],
                state_required=True,
            )

            self.assertEqual(restored, 1)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"]["123:8"]["torrent_url"], release.torrent_url)
            self.assertNotIn("last_failure_reason", saved["items"]["123:8"])

    def test_reconcile_restores_completed_torrent_by_hash_when_qbit_name_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            info_hash = "0123456789abcdef0123456789abcdef01234567"
            torrent_url = f"https://mikanani.me/Download/20250101/{info_hash}.torrent"
            pending = {
                "items": {
                    "123:8": {
                        "bangumi_id": 123,
                        "episode": 8,
                        "episodes": [8],
                        "failed_urls": [torrent_url],
                        "last_failure_reason": "did not start",
                        "last_failed_torrent_url": torrent_url,
                        "last_failed_title": "[Chinese Group] Release Show - 08 [CHT]",
                    }
                }
            }
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash=info_hash,
                name="[English Group] Release Show - 08 [CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub,mikansub-completed",
            )
            worker = MikanWorker(config, _logger())

            restored = worker._reconcile_pending_with_existing_torrents(
                [torrent],
                [],
                state_required=True,
            )

            self.assertEqual(restored, 1)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"]["123:8"]["last_qbit_hash"], info_hash)
            self.assertNotIn("last_failure_reason", saved["items"]["123:8"])

    def test_reconcile_recovers_completed_pack_episodes_from_qbit_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_completed_reconcile_max_age_seconds = 7200
            (root / "mikan_pending.json").write_text('{"items": {}}', encoding="utf-8")
            torrent = QBitTorrent(
                hash="89abcdef0123456789abcdef0123456789abcdef",
                name="[Group] Release Show [CHT][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=200,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub,mikansub-completed",
                completion_on=int(datetime.now(timezone.utc).timestamp()),
            )
            qbit = Mock()
            qbit.list_files.return_value = [
                QBitTorrentFile("Release Show/Release Show - 01.mkv", 100, 1.0, 1),
                QBitTorrentFile("Release Show/Release Show - 02.mkv", 100, 1.0, 1),
            ]
            worker = MikanWorker(config, _logger())

            restored = worker._reconcile_pending_with_existing_torrents(
                [torrent],
                [{"bangumi_id": 123, "path": str(root), "match": ["Release Show"]}],
                qbit=qbit,
                state_required=True,
            )

            self.assertEqual(restored, 2)
            qbit.list_files.assert_called_once_with(torrent.hash)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved["items"]), {"123:1", "123:2"})

    def test_reconcile_recovers_recent_completed_torrent_missing_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_completed_reconcile_max_age_seconds = 7200
            (root / "mikan_pending.json").write_text('{"items": {}}', encoding="utf-8")
            torrent = QBitTorrent(
                hash="0123456789abcdef0123456789abcdef01234567",
                name="[Group] Release Show - 08 [CHT][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub,mikansub-completed",
                completion_on=int(datetime.now(timezone.utc).timestamp()),
            )
            worker = MikanWorker(config, _logger())

            restored = worker._reconcile_pending_with_existing_torrents(
                [torrent],
                [{"bangumi_id": 123, "path": str(root), "match": ["Release Show"]}],
                state_required=True,
            )

            self.assertEqual(restored, 1)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = saved["items"]["123:8"]
            self.assertEqual(entry["torrent_url"], f"qbit://{torrent.hash}")
            self.assertEqual(entry["last_qbit_hash"], torrent.hash)
            self.assertEqual(entry["source"], "qbit-recovered")
            self.assertGreaterEqual(entry["recovery_match_confidence"], 0.9)
            self.assertTrue(entry["recovery_match_version"].startswith("qbit-recovery-v3"))
            self.assertTrue(entry["recovery_match_evidence"])

    def test_reconcile_refuses_episode_range_alias_collision(self) -> None:
        torrent = QBitTorrent(
            hash="f35f049e5397b12f7bdc036a8fdfb90702674f38",
            name="[LoliHouse] Bofuri 2 [01-12][WebRip 1080p HEVC-10bit AAC SRTx2]",
            progress=1.0,
            state="uploading",
            dlspeed=0,
            downloaded=100,
            added_on=None,
            content_path=None,
            save_path=None,
            category="llm-sub",
            tags="mikansub",
        )

        resolution = _resolve_untracked_torrent_targets(
            torrent,
            {"items": {}},
            [
                {
                    "bangumi_id": 260,
                    "path": "/anime/No-Rin",
                    "match": ["01~12", "2014冬", "No-Rin", "農林"],
                    "match_confidence": 1.0,
                }
            ],
        )

        self.assertFalse(resolution.trusted)
        self.assertEqual(resolution.targets, ())

    def test_reconcile_does_not_recover_completed_torrent_with_replaced_extract_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_completed_reconcile_max_age_seconds = 7200
            (root / "mikan_pending.json").write_text('{"items": {}}', encoding="utf-8")
            torrent = QBitTorrent(
                hash="fedcba9876543210fedcba9876543210fedcba98",
                name="[Group] Release Show - 08 [No Subs][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub,mikansub-completed",
                completion_on=int(datetime.now(timezone.utc).timestamp()),
            )
            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, attempts, worker_id, lease_until,
                        torrent_hash, torrent_name, bangumi_ids_json, episodes_json,
                        pending_entries_json, torrent_json, result_json, last_error,
                        created_at, updated_at, started_at, finished_at
                    )
                    VALUES (?, 'replaced', 1, 1, '', 0, ?, ?, '[123]', '[8]', '[]', '{}',
                            '{"failure_reason": "no_text_subtitle_streams", "retryable": false}',
                            'No extractable subtitle streams', 10, 20, 15, 20)
                    """,
                    (f"hash:{torrent.hash}", torrent.hash, torrent.name),
                )
                conn.commit()
            finally:
                conn.close()
            worker = MikanWorker(config, _logger())

            restored = worker._reconcile_pending_with_existing_torrents(
                [torrent],
                [{"bangumi_id": 123, "path": str(root), "match": ["Release Show"]}],
                state_required=True,
            )

            self.assertEqual(restored, 0)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"], {})

    def test_reconcile_ignores_old_completed_torrent_missing_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_completed_reconcile_max_age_seconds = 7200
            (root / "mikan_pending.json").write_text('{"items": {}}', encoding="utf-8")
            torrent = QBitTorrent(
                hash="89abcdef0123456789abcdef0123456789abcdef",
                name="[Group] Release Show - 08 [CHT][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
                completion_on=int((datetime.now(timezone.utc) - timedelta(seconds=7201)).timestamp()),
            )
            worker = MikanWorker(config, _logger())

            restored = worker._reconcile_pending_with_existing_torrents(
                [torrent],
                [{"bangumi_id": 123, "path": str(root), "match": ["Release Show"]}],
                state_required=True,
            )

            self.assertEqual(restored, 0)
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["items"], {})

    def test_zero_speed_poll_deletes_after_sixty_seconds_and_triggers_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_process_config(root, root / "downloads")
            config.mikan_download_start_timeout_seconds = 60
            config.mikan_download_stall_timeout_seconds = 60
            config.mikan_delete_stalled_torrents = True
            release = _release("https://mikan/zero-speed.torrent")
            pending = {"items": {}}
            _mark_pending(pending, release)
            stalled_at = (
                datetime.now(timezone.utc) - timedelta(seconds=61)
            ).isoformat()
            pending_entry = pending["items"]["123:1"]
            pending_entry["queued_at"] = stalled_at
            pending_entry["last_downloaded"] = 100
            pending_entry["last_progress"] = 0.1
            pending_entry["last_progress_at"] = stalled_at
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash="0123456789abcdef0123456789abcdef01234567",
                name=release.title,
                progress=0.1,
                state="stalledDL",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(return_value=[])
            worker.request_replacement_enqueue = Mock(return_value={"deferred": True})

            worker.poll_download_progress(state_required=True)

            qbit.delete_torrents.assert_called_once_with([torrent.hash], delete_files=True)
            worker.request_replacement_enqueue.assert_called_once()
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = saved["items"]["123:1"]
            self.assertNotIn("torrent_url", entry)
            self.assertIn(torrent.hash, entry["failed_info_hashes"])

    def test_zero_speed_sync_does_not_refresh_last_progress_time(self) -> None:
        old_progress_at = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        entry = {
            "queued_at": old_progress_at,
            "last_downloaded": 100,
            "last_progress": 0.1,
            "last_progress_at": old_progress_at,
        }
        torrent = QBitTorrent(
            hash="hash1",
            name="Test",
            progress=0.1,
            state="stalledDL",
            dlspeed=0,
            downloaded=100,
            added_on=None,
            content_path=None,
            save_path=None,
            category="llm-sub",
            tags="mikansub",
        )

        now = datetime.now(timezone.utc)
        changed = _sync_pending_entry_qbit_progress(entry, [torrent], now)
        changed_again = _sync_pending_entry_qbit_progress(entry, [torrent], now + timedelta(seconds=1))

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(entry["last_progress_at"], old_progress_at)

    def test_qbit_sync_records_added_and_completion_times_separately(self) -> None:
        entry = {"queued_at": datetime.now(timezone.utc).isoformat()}
        torrent = QBitTorrent(
            hash="hash-with-times",
            name="Timed Torrent",
            progress=1.0,
            state="uploading",
            dlspeed=0,
            downloaded=100,
            added_on=1_700_000_000,
            completion_on=1_700_000_900,
            content_path=None,
            save_path=None,
            category="llm-sub",
            tags="mikansub",
        )

        changed = _sync_pending_entry_qbit_progress(entry, [torrent], datetime.now(timezone.utc))

        self.assertTrue(changed)
        self.assertEqual(entry["last_qbit_added_on"], 1_700_000_000)
        self.assertEqual(entry["last_qbit_completion_on"], 1_700_000_900)

    def test_untracked_long_eta_torrent_is_deleted_and_mapped_to_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_download_start_timeout_seconds = 60
            config.mikan_download_max_eta_seconds = 86400
            torrent = QBitTorrent(
                hash="0123456789abcdef0123456789abcdef01234567",
                name="[Group] Test Anime - 01 [CHT][MKV]",
                progress=0.1,
                state="downloading",
                dlspeed=100,
                downloaded=100,
                added_on=1,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
                eta=172800,
            )
            qbit = Mock()
            worker = MikanWorker(config, _logger())
            worker._qbit_unhealthy_since[torrent.hash] = ("eta too long", datetime.now(timezone.utc).timestamp() - 61)

            targets = worker._expire_stalled_pending_targets(
                qbit,
                state_required=True,
                torrents_override=[torrent],
                progress_already_synced=True,
                series_mappings_override=[
                    {"bangumi_id": 123, "path": str(root / "Test Anime"), "match": ["Test Anime"]}
                ],
            )

            qbit.delete_torrents.assert_called_once_with([torrent.hash], delete_files=True)
            self.assertEqual(targets, [MikanReplacementTarget(123, 1)])
            saved = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            self.assertIn(torrent.hash, saved["items"]["123:1"]["failed_info_hashes"])
            self.assertEqual(saved["items"]["123:1"]["last_failure_reason"], "eta too long")

    def test_untracked_unmatched_torrent_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_download_start_timeout_seconds = 60
            config.mikan_download_max_eta_seconds = 86400
            torrent = QBitTorrent(
                hash="abcdef0123456789abcdef0123456789abcdef01",
                name="[Group] Completely Unrelated - 01 [CHT][MKV]",
                progress=0.1,
                state="downloading",
                dlspeed=100,
                downloaded=100,
                added_on=1,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
                eta=172800,
            )
            qbit = Mock()
            worker = MikanWorker(config, _logger())
            worker._qbit_unhealthy_since[torrent.hash] = (
                "eta too long",
                datetime.now(timezone.utc).timestamp() - 61,
            )

            targets = worker._expire_stalled_pending_targets(
                qbit,
                state_required=True,
                torrents_override=[torrent],
                progress_already_synced=True,
                series_mappings_override=[
                    {"bangumi_id": 123, "path": str(root / "Test Anime"), "match": ["Test Anime"]}
                ],
            )

            qbit.delete_torrents.assert_not_called()
            self.assertEqual(targets, [])

    def test_untracked_unmatched_warning_is_aggregated_and_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_download_start_timeout_seconds = 60
            config.mikan_download_max_eta_seconds = 86400
            torrent = QBitTorrent(
                hash="abcdef0123456789abcdef0123456789abcdef01",
                name="[Group] Completely Unrelated - 01 [CHT][MKV]",
                progress=0.1,
                state="downloading",
                dlspeed=100,
                downloaded=100,
                added_on=1,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
                eta=172800,
            )
            qbit = Mock()
            logger = Mock()
            worker = MikanWorker(config, logger)
            worker._qbit_unhealthy_since[torrent.hash] = (
                "eta too long",
                datetime.now(timezone.utc).timestamp() - 61,
            )

            for _index in range(2):
                targets = worker._expire_stalled_pending_targets(
                    qbit,
                    state_required=True,
                    torrents_override=[torrent],
                    progress_already_synced=True,
                    series_mappings_override=[
                        {"bangumi_id": 123, "path": str(root / "Test Anime"), "match": ["Test Anime"]}
                    ],
                )
                self.assertEqual(targets, [])

            qbit.delete_torrents.assert_not_called()
            logger.warning.assert_called_once()
            self.assertIn("count=%s", logger.warning.call_args.args[0])
            self.assertEqual(logger.warning.call_args.args[1], 1)

    def test_claim_blocks_legacy_qbit_recovery_without_trust_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            torrent = QBitTorrent(
                hash="f35f049e5397b12f7bdc036a8fdfb90702674f38",
                name="[LoliHouse] Bofuri 2 [01-12]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "downloads"),
                save_path=str(root / "downloads"),
                category="llm-sub",
                tags="mikansub",
            )
            legacy_entry = {
                "bangumi_id": 260,
                "episode": 1,
                "source": "qbit-recovered",
                "title": torrent.name,
            }
            _upsert_mikan_extract_jobs(
                config,
                [(torrent, [legacy_entry], 1, False)],
                state_required=True,
            )

            jobs = _claim_mikan_extract_jobs(config, limit=1)

            self.assertEqual(jobs, [])
            conn = _mikan_state_connect(config)
            try:
                status, result_json = conn.execute(
                    "SELECT status, result_json FROM mikan_extract_jobs WHERE job_key = ?",
                    ("hash:f35f049e5397b12f7bdc036a8fdfb90702674f38",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(status, "terminal_failed")
            self.assertEqual(json.loads(result_json)["failure_reason"], "unsafe_recovered_mapping")

    def test_user_cancelled_extract_does_not_generate_replacement(self) -> None:
        entry = {
            "bangumi_id": 123,
            "episode": 1,
            "episodes": [1],
            "title": "Test Anime - 01",
            "torrent_url": "https://example.invalid/test.torrent",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }

        targets = _mark_active_pending_entry_extract_failed(
            entry,
            failure_reason="extract_cancelled_by_user",
            failure_detail="cancelled",
        )

        self.assertEqual(targets, [])
        self.assertEqual(entry["last_extract_failure_reason"], "extract_cancelled_by_user")

    def test_untracked_zero_speed_torrent_is_deleted_after_sixty_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            config.mikan_download_start_timeout_seconds = 60
            torrent = QBitTorrent(
                hash="89abcdef0123456789abcdef0123456789abcdef",
                name="[Group] Test Anime - 01 [CHT][MKV]",
                progress=0.2,
                state="stalledDL",
                dlspeed=0,
                downloaded=100,
                added_on=1,
                content_path=None,
                save_path=None,
                category="llm-sub",
                tags="mikansub",
                eta=8640000,
            )
            qbit = Mock()
            worker = MikanWorker(config, _logger())
            worker._qbit_unhealthy_since[torrent.hash] = ("zero speed", datetime.now(timezone.utc).timestamp() - 61)

            worker._expire_stalled_pending_targets(
                qbit,
                state_required=True,
                torrents_override=[torrent],
                progress_already_synced=True,
                series_mappings_override=[
                    {"bangumi_id": 123, "path": str(root / "Test Anime"), "match": ["Test Anime"]}
                ],
            )

            qbit.delete_torrents.assert_called_once_with([torrent.hash], delete_files=True)

    def test_poll_download_progress_requeues_incomplete_processed_success_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            config = _mikan_process_config(root, download_root)
            worker = MikanWorker(config, _logger())
            library_dir = root / "Release Show" / "Season 1"
            library_dir.mkdir(parents=True)
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            release = _episode_release(
                "https://mikan/reprocess.torrent",
                "[Group] Release Show - 08 [SRTx2]",
                8,
            )
            pending = {"items": {}}
            _mark_pending(pending, release)
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash="hash-reprocess",
                name=release.title,
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Release Show - 08.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub,mikansub-extracted",
            )
            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, torrent_hash, torrent_name,
                        episodes_json, torrent_json, created_at, updated_at, finished_at
                    )
                    VALUES (?, 'success', 8, ?, ?, '[8]', '{}', 1, 1, 1)
                    """,
                    ("hash:hash-reprocess", torrent.hash, torrent.name),
                )
                conn.commit()
            finally:
                conn.close()
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(root), "match": ["Release Show"]}]
            )

            result = worker.poll_download_progress(state_required=True)

            self.assertEqual(result.completed_pending_count, 1)
            conn = _mikan_state_connect(config)
            try:
                status = conn.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE job_key = ?",
                    ("hash:hash-reprocess",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "queued")

    def test_poll_download_progress_requeues_untagged_historical_success_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            download_root.mkdir()
            config = _mikan_process_config(root, download_root)
            worker = MikanWorker(config, _logger())
            library_dir = root / "Release Show" / "Season 1"
            library_dir.mkdir(parents=True)
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            release = _episode_release(
                "https://mikan/repeated.torrent",
                "[Group] Release Show - 08 [SRTx2]",
                8,
            )
            pending = {"items": {}}
            _mark_pending(pending, release)
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            torrent = QBitTorrent(
                hash="hash-repeated",
                name=release.title,
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "Release Show - 08.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            conn = _mikan_state_connect(config)
            try:
                conn.execute(
                    """
                    INSERT INTO mikan_extract_jobs(
                        job_key, status, priority, torrent_hash, torrent_name,
                        episodes_json, torrent_json, created_at, updated_at, finished_at
                    )
                    VALUES (?, 'success', 8, ?, ?, '[8]', '{}', 1, 1, 1)
                    """,
                    ("hash:hash-repeated", torrent.hash, torrent.name),
                )
                conn.commit()
            finally:
                conn.close()
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(root), "match": ["Release Show"]}]
            )

            result = worker.poll_download_progress(state_required=True)

            self.assertEqual(result.completed_pending_count, 1)
            conn = _mikan_state_connect(config)
            try:
                status = conn.execute(
                    "SELECT status FROM mikan_extract_jobs WHERE job_key = ?",
                    ("hash:hash-repeated",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "queued")

    def test_library_scan_includes_recent_series_and_limits_total(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_series = root / "Old"
            new_series = root / "New"
            old_series.mkdir()
            new_series.mkdir()
            old_time = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
            new_time = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
            os.utime(old_series, (old_time, old_time))
            os.utime(new_series, (new_time, new_time))
            config = SimpleNamespace(
                mikan_library_scan_recent_first=True,
                mikan_library_scan_recent_series_per_cycle=1,
                mikan_library_scan_max_series_per_cycle=2,
                watch_interval_seconds=300,
            )

            selected = _library_scan_series_mappings(
                config,
                _logger(),
                [
                    {"bangumi_id": 1, "path": str(old_series)},
                    {"bangumi_id": 2, "path": str(new_series)},
                ],
            )

            self.assertEqual(len(selected), 2)
            self.assertEqual(selected[0], {"bangumi_id": 2, "path": str(new_series)})
            self.assertIn({"bangumi_id": 1, "path": str(old_series)}, selected)

    def test_completed_download_uses_qbit_file_list_sidecars_after_video_moves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            target_video = library_dir / "Release Show - S01E08.mkv"
            target_video.write_bytes(b"library video")
            (source_dir / "Release Show - 08.zh-Hans.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n明天选班长\n",
                encoding="utf-8",
            )
            (source_dir / "Release Show - 08.zh-Hant.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n明天選班長\n",
                encoding="utf-8",
            )
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir / "Release Show - 08.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [
                QBitTorrentFile("Release/Release Show - 08.mkv", 100, 1.0, 1),
                QBitTorrentFile("Release/Release Show - 08.zh-Hans.srt", 10, 1.0, 1),
                QBitTorrentFile("Release/Release Show - 08.zh-Hant.srt", 10, 1.0, 1),
            ]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/moved.torrent",
                    "[Mikan] Different Metadata Name - 08 [SRTx2]",
                    8,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            def convert(_source: Path, output: Path, **_kwargs: object) -> None:
                text = (
                    "Dialogue: 0,0:00:00.00,0:00:01.00,,,明天选班长"
                    if "hans" in _source.name.casefold()
                    else "Dialogue: 0,0:00:00.00,0:00:01.00,,,明天選班長"
                )
                output.write_text(text, encoding="utf-8")

            with patch("subtitle_extract._copy_or_convert_sidecar", side_effect=convert):
                processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 1)
            self.assertTrue((library_dir / "Release Show - S01E08.zh.ass").exists())
            self.assertTrue((library_dir / "Release Show - S01E08.zh-TW.ass").exists())
            pending_after = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending_after["items"]["123:8"]
            self.assertNotIn("torrent_url", entry)
            self.assertNotIn("queued_at", entry)
            self.assertIn("completed_at", entry)
            self.assertEqual(entry["last_extracted_count"], 1)
            self.assertEqual(entry["total_extracted_count"], 1)
            self.assertIn("last_extracted_at", entry)
            self.assertEqual(
                qbit.add_tags.call_args_list,
                [call("hash1", ["mikan"]), call("hash1", ["mikansub-extracted"])],
            )

    def test_completed_download_matches_pack_sidecars_by_episode_from_qbit_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "DBD Pack"
            subs_dir = source_dir / "Subs"
            library_dir = root / "anime" / "Pack Show" / "Season 1"
            subs_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            target_video = library_dir / "Pack Show - S01E01.mkv"
            target_video.write_bytes(b"library video")
            (subs_dir / "[DBD-Raws][Pack Show][01][GB].ass").write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.00,,,{text}\n".format(
                    text="\u660e\u5929\u9009\u73ed\u957f"
                ),
                encoding="utf-8",
            )
            (subs_dir / "[DBD-Raws][Pack Show][01][BIG5].ass").write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.00,,,{text}\n".format(
                    text="\u660e\u5929\u9078\u73ed\u9577"
                ),
                encoding="utf-8",
            )
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash1",
                name="[DBD-Raws][Pack Show][01-12][1080P][BDRip][HEVC-10bit][简繁外挂][FLAC][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [
                QBitTorrentFile("DBD Pack/[DBD-Raws][Pack Show][01][1080P][BDRip][HEVC-10bit][FLAC].mkv", 100, 1.0, 1),
                QBitTorrentFile("DBD Pack/Subs/[DBD-Raws][Pack Show][01][GB].ass", 10, 1.0, 1),
                QBitTorrentFile("DBD Pack/Subs/[DBD-Raws][Pack Show][01][BIG5].ass", 10, 1.0, 1),
            ]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Pack Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/pack.torrent",
                    "[Mikan] Different Metadata Name - 01 [外挂]",
                    1,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 1)
            self.assertTrue((library_dir / "Pack Show - S01E01.zh.ass").exists())
            self.assertTrue((library_dir / "Pack Show - S01E01.zh-TW.ass").exists())
            self.assertEqual(
                qbit.add_tags.call_args_list,
                [call("hash1", ["mikan"]), call("hash1", ["mikansub-extracted"])],
            )

    def test_completed_download_requires_review_when_mikan_mapping_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "[LoliHouse] Zombieland Saga [WebRip 1080p HEVC-10bit AAC]"
            season1_dir = root / "anime" / "Zombie Land Saga" / "Season 1"
            season2_dir = root / "anime" / "Zombie Land Saga" / "Season 2"
            source_dir.mkdir(parents=True)
            season1_dir.mkdir(parents=True)
            season2_dir.mkdir(parents=True)
            source_video = source_dir / "[LoliHouse] Zombieland Saga - 12 [WebRip 1080p HEVC-yuv420p10 AAC ASSx2].mkv"
            source_video.write_bytes(b"not a real mkv")
            (source_dir / "[LoliHouse] Zombieland Saga - 12 [WebRip 1080p HEVC-yuv420p10 AAC ASSx2].zh-Hans.ass").write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.00,,,{text}\n".format(
                    text="\u660e\u5929\u9009\u73ed\u957f"
                ),
                encoding="utf-8",
            )
            (source_dir / "[LoliHouse] Zombieland Saga - 12 [WebRip 1080p HEVC-yuv420p10 AAC ASSx2].zh-Hant.ass").write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.00,,,{text}\n".format(
                    text="\u660e\u5929\u9078\u73ed\u9577"
                ),
                encoding="utf-8",
            )
            target_video = season1_dir / "Zombie Land Saga - S01E12 - HDTV-1080p.mkv"
            target_video.write_bytes(b"library video")
            (season2_dir / "Zombie Land Saga - S02E12 - HDTV-1080p.mkv").write_bytes(b"library video")
            (season1_dir / "season.nfo").write_text("<season><year>2018</year></season>", encoding="utf-8")
            (season2_dir / "season.nfo").write_text("<season><year>2021</year></season>", encoding="utf-8")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-zls",
                name="[LoliHouse] Zombieland Saga [01-12 修正合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=1_546_128_100,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
                completion_on=1_546_128_900,
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [
                QBitTorrentFile(str(source_video.relative_to(download_root)), 100, 1.0, 1),
                QBitTorrentFile(str((source_video.with_suffix(".zh-Hans.ass")).relative_to(download_root)), 10, 1.0, 1),
                QBitTorrentFile(str((source_video.with_suffix(".zh-Hant.ass")).relative_to(download_root)), 10, 1.0, 1),
            ]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(return_value=[])
            pending = {"items": {}}
            _mark_pending(
                pending,
                MikanRelease(
                    bangumi_id=1772,
                    title="[Mikan] Zombieland Saga - 12 [CHT]",
                    episode=12,
                    torrent_url="https://mikan/zombieland.torrent",
                    pub_date=datetime(2018, 12, 29, tzinfo=timezone.utc),
                    content_length=100,
                ),
            )
            # The qBittorrent hash is the durable relation between this source
            # and the pending bangumi record.  Target resolution is deliberately
            # absent so the worker must create a review item instead of guessing
            # between Season 1 and Season 2.
            pending["items"]["1772:12"]["info_hash"] = "hash-zls"
            pending["items"]["1772:12"]["last_qbit_hash"] = "hash-zls"
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 0)
            self.assertFalse((season1_dir / "Zombie Land Saga - S01E12 - HDTV-1080p.zh.ass").exists())
            self.assertFalse((season1_dir / "Zombie Land Saga - S01E12 - HDTV-1080p.zh-TW.ass").exists())
            self.assertFalse((season2_dir / "Zombie Land Saga - S02E12 - HDTV-1080p.zh.ass").exists())
            pending_after = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending_after["items"]["1772:12"]
            self.assertEqual(entry.get("last_extract_failure_reason"), "target_ambiguity")
            self.assertEqual(entry.get("last_failed_info_hash"), "hash-zls")
            with closing(sqlite3.connect(root / "control_state.sqlite3")) as conn:
                review = conn.execute(
                    "SELECT kind, status, diagnosis_json FROM review_items WHERE kind='target_ambiguity'"
                ).fetchone()
            self.assertEqual(review[:2], ("target_ambiguity", "open"))
            diagnosis = json.loads(review[2])
            self.assertEqual(
                diagnosis["source_published_at"],
                datetime(2018, 12, 29, tzinfo=timezone.utc).timestamp(),
            )
            self.assertEqual(diagnosis["torrent_added_at"], 1_546_128_100.0)
            self.assertEqual(diagnosis["torrent_completed_at"], 1_546_128_900.0)

    def test_completed_torrent_match_rejects_sequel_subtitle_cross_match(self) -> None:
        pending = {"items": {}}
        _mark_pending(
            pending,
            MikanRelease(
                bangumi_id=2281,
                title="[Nekomoe kissaten&LoliHouse] Kuma Kuma Kuma Bear - 03 [WebRip 1080p HEVC-10bit AAC ASSx2]",
                episode=3,
                torrent_url="https://mikan/kuma-s1-03.torrent",
                pub_date=datetime(2020, 10, 21, tzinfo=timezone.utc),
                content_length=100,
            ),
        )
        entry = pending["items"]["2281:3"]
        sequel_torrent = QBitTorrent(
            hash="hash-punch",
            name="[Sakurato] Kuma Kuma Kuma Bear Punch! [03][HEVC-10bit 1080p AAC][CHS&CHT].mkv",
            progress=1.0,
            state="uploading",
            dlspeed=0,
            downloaded=100,
            added_on=None,
            content_path=None,
            save_path=None,
            category="llm-sub",
            tags="mikansub",
        )

        self.assertFalse(
            _pending_entry_matches_completed_torrent(
                entry,
                sequel_torrent,
                {3},
                [],
            )
        )

    def test_completed_collection_extracts_only_pending_episode_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Collection"
            library_dir = root / "anime" / "Collection Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            (source_dir / "Collection Show - 01.mkv").write_bytes(b"source 01")
            (source_dir / "Collection Show - 10.mkv").write_bytes(b"source 10")
            (library_dir / "Collection Show - S01E01.mkv").write_bytes(b"library 01")
            target_video = library_dir / "Collection Show - S01E10.mkv"
            target_video.write_bytes(b"library 10")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-collection",
                name="[Group] Collection Show [01-10][外挂][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            worker = MikanWorker(config, _logger())
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Collection Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/collection.torrent",
                    "[Group] Collection Show [01-10][外挂][MKV]",
                    10,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            seen_sources: list[Path] = []

            def extract(source: Path, _config, *, output_video_path: Path, diagnostics: list[dict], allowed_languages: set[str]) -> list[SimpleNamespace]:
                self.assertEqual(allowed_languages, {"zh-tw", "zh-cn"})
                seen_sources.append(Path(source))
                self.assertEqual(Path(source).name, "Collection Show - 10.mkv")
                self.assertEqual(Path(output_video_path), target_video)
                return [SimpleNamespace(language="zh-tw", stream_index=-1, source="sidecar", path=target_video.with_suffix(".zh-TW.ass"))]

            with patch("mikan_worker.extract_available_subtitles", side_effect=extract):
                result = worker._extract_completed_torrent(
                    torrent,
                    [{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Collection Show"]}],
                    [
                        QBitTorrentFile("Collection/Collection Show - 01.mkv", 100, 1.0, 1),
                        QBitTorrentFile("Collection/Collection Show - 10.mkv", 100, 1.0, 1),
                    ],
                )

            self.assertEqual(result.extracted_count, 1)
            self.assertEqual([source.name for source in seen_sources], ["Collection Show - 10.mkv"])

    def test_completed_collection_prefers_main_episode_over_extras(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Collection"
            library_dir = root / "anime" / "Collection Show" / "Season 1"
            source_dir.mkdir(parents=True)
            (source_dir / "PV").mkdir()
            (source_dir / "SP").mkdir()
            (source_dir / "menu").mkdir()
            library_dir.mkdir(parents=True)
            (source_dir / "Collection Show - 01.mkv").write_bytes(b"main 01")
            (source_dir / "PV" / "Collection Show [PV][01].mkv").write_bytes(b"pv 01")
            (source_dir / "SP" / "Collection Show [SP][01].mkv").write_bytes(b"sp 01")
            (source_dir / "menu" / "Collection Show [menu][01].mkv").write_bytes(b"menu 01")
            target_video = library_dir / "Collection Show - S01E01.mkv"
            target_video.write_bytes(b"library 01")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-collection",
                name="[Group] Collection Show [01-13][外挂][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/collection.torrent",
                    "[Group] Collection Show [01-13][外挂][MKV]",
                    1,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            seen_sources: list[Path] = []

            def extract(source: Path, _config, *, output_video_path: Path, diagnostics: list[dict], allowed_languages: set[str]) -> list[SimpleNamespace]:
                self.assertEqual(allowed_languages, {"zh-tw", "zh-cn"})
                seen_sources.append(Path(source))
                self.assertEqual(Path(source).name, "Collection Show - 01.mkv")
                self.assertEqual(Path(output_video_path), target_video)
                return [SimpleNamespace(language="zh-tw", stream_index=-1, source="sidecar", path=target_video.with_suffix(".zh-TW.ass"))]

            with patch("mikan_worker.extract_available_subtitles", side_effect=extract):
                result = worker._extract_completed_torrent(
                    torrent,
                    [{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Collection Show"]}],
                    [
                        QBitTorrentFile("Collection/PV/Collection Show [PV][01].mkv", 100, 1.0, 1),
                        QBitTorrentFile("Collection/SP/Collection Show [SP][01].mkv", 100, 1.0, 1),
                        QBitTorrentFile("Collection/menu/Collection Show [menu][01].mkv", 100, 1.0, 1),
                        QBitTorrentFile("Collection/Collection Show - 01.mkv", 100, 1.0, 1),
                    ],
                )

            self.assertEqual(result.extracted_count, 1)
            self.assertEqual([source.name for source in seen_sources], ["Collection Show - 01.mkv"])

    def test_completed_collection_skips_extra_only_episode_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Collection"
            library_dir = root / "anime" / "Collection Show" / "Season 1"
            source_dir.mkdir(parents=True)
            (source_dir / "Tokuten").mkdir()
            (source_dir / "SP").mkdir()
            library_dir.mkdir(parents=True)
            (source_dir / "Tokuten" / "Collection Show [Tokuten][01].mkv").write_bytes(b"extra 01")
            (source_dir / "SP" / "Collection Show [SP][01].mkv").write_bytes(b"sp 01")
            (library_dir / "Collection Show - S01E01.mkv").write_bytes(b"library 01")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-extra-only",
                name="[Group] Collection Show [01-13][BDRip][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/extra-only.torrent",
                    "[Group] Collection Show [01-13][BDRip][MKV]",
                    1,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            with patch("mikan_worker.extract_available_subtitles") as extract:
                result = worker._extract_completed_torrent(
                    torrent,
                    [{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Collection Show"]}],
                    [
                        QBitTorrentFile("Collection/Tokuten/Collection Show [Tokuten][01].mkv", 100, 1.0, 1),
                        QBitTorrentFile("Collection/SP/Collection Show [SP][01].mkv", 100, 1.0, 1),
                    ],
                )

            self.assertEqual(result.extracted_count, 0)
            self.assertEqual(result.failure_reason, "extra_video_only")
            self.assertIn("Only extra/special videos matched pending episodes", result.failure_detail)
            self.assertEqual(len(result.failure_context["skipped_source_videos"]), 2)
            extract.assert_not_called()

    def test_source_selection_does_not_fallback_to_unparsed_when_numbered_sources_miss_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_videos = [
                root / "Collection Show - 01.mkv",
                root / "Collection Show Main.mkv",
            ]

            result = _select_source_videos_for_pending_episodes(source_videos, {12})

            self.assertEqual(result.selected, [])
            self.assertEqual(result.failure_reason, "source_episode_not_found")
            self.assertIn("not falling back to unparsed source videos", result.failure_detail)

    def test_completed_download_uses_pending_title_season_hint_for_ambiguous_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_root = root / "anime" / "Ambiguous Show"
            source_dir.mkdir(parents=True)
            for season in (1, 2, 3):
                season_dir = library_root / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Ambiguous Show - S0{season}E10.mkv").write_bytes(b"library video")
            source_video = source_dir / "Ambiguous Show - 10.mkv"
            source_video.write_bytes(b"download video")
            target_video = library_root / "Season 2" / "Ambiguous Show - S02E10.mkv"
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-ambiguous-season",
                name="[Group] Ambiguous Show - 10 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/ambiguous-s2.torrent",
                    "[Group] Ambiguous Show S2 - 10 [SRTx2]",
                    10,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            pending_entry = pending["items"]["123:10"]
            mappings = [{"bangumi_id": 123, "path": str(library_root), "match": ["Ambiguous Show"]}]
            self.assertTrue(
                _completed_torrent_has_local_episode(
                    torrent,
                    config,
                    mappings,
                    pending_entries=[pending_entry],
                )
            )
            seen_targets: list[Path] = []

            def extract(source: Path, _config, *, output_video_path: Path, diagnostics: list[dict], allowed_languages: set[str]) -> list[SimpleNamespace]:
                self.assertEqual(allowed_languages, {"zh-tw", "zh-cn"})
                self.assertEqual(Path(source), source_video)
                seen_targets.append(Path(output_video_path))
                return [SimpleNamespace(language="zh-tw", stream_index=-1, source="sidecar", path=target_video.with_suffix(".zh-TW.ass"))]

            with patch("mikan_worker.extract_available_subtitles", side_effect=extract):
                result = worker._extract_completed_torrent(
                    torrent,
                    mappings,
                    [
                        QBitTorrentFile("Release/Ambiguous Show - 10.mkv", 100, 1.0, 1),
                    ],
                )

            self.assertEqual(result.extracted_count, 1)
            self.assertEqual(seen_targets, [target_video])

    def test_release_revision_v2_is_not_parsed_as_season_five(self) -> None:
        self.assertIsNone(
            _season_number_from_text(
                "[Airota][Kusuriya no Hitorigoto][04_v2][1080P HEVC-10bit AAC ASS]"
            )
        )

    def test_completed_extraction_prefers_qbit_source_over_library_fallback_for_range_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Made in Abyss" / "Season 2"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            source_video = source_dir / "[Sakurato] Made in Abyss_ Retsujitsu no Ougonkyou [08v2][1080p][CHS&CHT].mkv"
            source_video.write_bytes(b"download episode 08")
            target_video = library_dir / "Made in Abyss - S02E08 - WEBDL-1080p.mkv"
            target_video.write_bytes(b"library episode 08")
            fallback_video = library_dir / "Made in Abyss - S02E12 - WEBDL-1080p.mkv"
            fallback_video.write_bytes(b"library episode 12")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-made-in-abyss-range",
                name="[Sakurato] Made in Abyss_ Retsujitsu no Ougonkyou [01-12][HEVC-10bit 1080p AAC][CHS&CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            worker = MikanWorker(config, _logger())
            pending = {"items": {}}
            _mark_pending(
                pending,
                MikanRelease(
                    bangumi_id=123,
                    title="[Sakurato] Made in Abyss S2 [01-12][CHS&CHT]",
                    episode=1,
                    episodes=tuple(range(1, 13)),
                    torrent_url="https://mikan/made-in-abyss-range.torrent",
                    pub_date=None,
                    content_length=1200,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            seen_sources: list[Path] = []
            seen_targets: list[Path] = []

            def extract(
                source: Path,
                _config,
                *,
                output_video_path: Path,
                diagnostics: list[dict],
                allowed_languages: set[str],
            ) -> list[SimpleNamespace]:
                self.assertEqual(allowed_languages, {"zh-tw", "zh-cn"})
                seen_sources.append(Path(source))
                seen_targets.append(Path(output_video_path))
                return [
                    SimpleNamespace(
                        language="zh-tw",
                        stream_index=0,
                        source="embedded",
                        path=Path(output_video_path).with_suffix(".zh-TW.ass"),
                    )
                ]

            with (
                patch("mikan_worker._target_has_required_chinese_subtitles", return_value=False),
                patch("mikan_worker.extract_available_subtitles", side_effect=extract),
            ):
                result = worker._extract_completed_torrent(
                    torrent,
                    [{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Made in Abyss"]}],
                    [
                        QBitTorrentFile(source_video.relative_to(download_root).as_posix(), 100, 1.0, 1),
                    ],
                )

            self.assertEqual(result.extracted_count, 1)
            self.assertEqual(seen_sources, [source_video])
            self.assertEqual(seen_targets, [target_video])

    def test_completed_target_does_not_fallback_to_all_series_when_pending_mapping_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            source_dir.mkdir(parents=True)
            source_video = source_dir / "Missing Mapping Show - 04.mkv"
            source_video.write_bytes(b"download video")
            other_series = root / "anime" / "Other Show" / "Season 1"
            other_series.mkdir(parents=True)
            (other_series / "Other Show - S01E04.mkv").write_bytes(b"other library video")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-missing-mapping",
                name="[Group] Missing Mapping Show - 04 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub,mikansub-extracted",
            )
            pending_entry = {
                "bangumi_id": 999,
                "episode": 4,
                "episodes": [4],
                "torrent_url": "https://mikan/missing-mapping.torrent",
                "title": torrent.name,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            mappings = [{"bangumi_id": 123, "path": str(other_series.parent), "match": ["Other Show"]}]

            self.assertFalse(
                _completed_torrent_has_local_episode(
                    torrent,
                    config,
                    mappings,
                    pending_entries=[pending_entry],
                )
            )
            self.assertIsNone(
                _target_video_for_torrent_source(
                    source_video,
                    torrent,
                    config,
                    _logger(),
                    mappings,
                    pending_entries=[pending_entry],
                )
            )
            self.assertFalse(
                _completed_torrent_outputs_complete(
                    torrent,
                    config,
                    _logger(),
                    mappings,
                    pending_entries=[pending_entry],
                )
            )

    def test_failed_url_is_retryable_when_previous_failure_used_extra_video(self) -> None:
        entry = {
            "failed_urls": ["https://mikan/source.torrent"],
            "last_extract_context": {
                "source_video": "/qbit/Collection/PV/Collection Show [PV][01].mkv",
            },
        }

        self.assertEqual(_pending_failed_urls(entry), [])

    def test_failed_url_stays_blocked_when_previous_failure_used_main_episode(self) -> None:
        entry = {
            "failed_urls": ["https://mikan/source.torrent"],
            "last_extract_context": {
                "source_video": "/qbit/Collection/Collection Show - 01.mkv",
            },
        }

        self.assertEqual(_pending_failed_urls(entry), ["https://mikan/source.torrent"])

    def test_source_missing_candidate_stays_blocked_so_replacement_is_required(self) -> None:
        release = _episode_release(
            "https://mikan/source.torrent",
            "[Mikan] Release Show - 08 [CHT]",
            8,
        )
        pending = {
            "items": {
                "123:8": {
                    "bangumi_id": 123,
                    "episode": 8,
                    "failed_urls": [release.torrent_url],
                    "last_extract_failure_reason": "source_video_missing",
                    "no_candidate_until": datetime.now(timezone.utc).timestamp() + 6000,
                }
            }
        }
        seen = {release.torrent_url: {"title": release.title}}

        selected = _choose_release_for_episode(123, 8, [release], seen, pending)

        self.assertIsNone(selected)
        self.assertEqual(_pending_failed_urls(pending["items"]["123:8"]), [release.torrent_url])
        self.assertTrue(_no_candidate_retry_active(pending, 123, 8, datetime.now(timezone.utc)))

    def test_source_missing_info_hash_stays_blocked_for_same_source(self) -> None:
        entry = {
            "failed_info_hashes": ["ABCDEF123456"],
            "last_extract_failure_reason": "source_video_missing",
        }

        self.assertEqual(_pending_failed_info_hashes(entry), {"abcdef123456"})

    def test_extract_error_info_hash_can_retry_same_source(self) -> None:
        entry = {
            "failed_info_hashes": ["ABCDEF123456"],
            "last_extract_failure_reason": "ffmpeg_extract_failed",
        }

        self.assertEqual(_pending_failed_info_hashes(entry), set())

    def test_source_missing_candidate_requeue_skips_same_qbit_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _mikan_enqueue_config(root)
            worker = MikanWorker(config, _logger())
            release = _episode_release(
                "https://mikan/source.torrent",
                "[Mikan] Release Show - 08 [CHT]",
                8,
            )
            pending = {
                "items": {
                    "123:8": {
                        "bangumi_id": 123,
                        "episode": 8,
                        "failed_urls": [release.torrent_url],
                        "last_extract_failure_reason": "source_video_missing",
                    }
                }
            }
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")
            (root / "mikan_seen.json").write_text(
                json.dumps({release.torrent_url: {"title": release.title}}),
                encoding="utf-8",
            )
            qbit = Mock()

            def assert_short_queue_lock(*_args, **_kwargs):
                self.assertTrue((root / "mikan_enqueue.lock").exists())

            qbit.add_url.side_effect = assert_short_queue_lock

            outcome = worker._queue_selected_release_with_state_lock(
                release,
                qbit,
                operation="test_source_retry",
                state_required=False,
                unavailable_reason="qbit_unavailable",
                add_failed_reason="qbit_add_failed",
                replacement=True,
            )

            self.assertEqual(outcome, "skipped")
            self.assertFalse((root / "mikan_enqueue.lock").exists())
            qbit.add_url.assert_not_called()
            entry = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))["items"]["123:8"]
            self.assertNotIn("torrent_url", entry)

    def test_real_subtitle_failure_still_blocks_seen_candidate(self) -> None:
        release = _episode_release(
            "https://mikan/source.torrent",
            "[Mikan] Release Show - 08 [CHT]",
            8,
        )
        pending = {
            "items": {
                "123:8": {
                    "bangumi_id": 123,
                    "episode": 8,
                    "failed_urls": [release.torrent_url],
                    "last_extract_failure_reason": "subtitle_language_not_supported",
                }
            }
        }
        seen = {release.torrent_url: {"title": release.title}}

        selected = _choose_release_for_episode(123, 8, [release], seen, pending)

        self.assertIsNone(selected)

    def test_missing_qbit_source_does_not_scan_target_library_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            library_dir.mkdir(parents=True)
            target_video = library_dir / "Release Show - S01E08.mkv"
            target_video.write_bytes(b"library video")
            (library_dir / "Release Show - S01E08.en.ass").write_text(
                "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,English only.",
                encoding="utf-8",
            )
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-missing-source",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(download_root / "missing" / "Release Show - 08.mkv"),
                save_path=str(download_root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )
            worker = MikanWorker(config, _logger())

            with patch("mikan_worker.normalize_sidecar_subtitles_for_output", side_effect=AssertionError("target sidecars scanned")):
                result = worker._extract_completed_torrent(
                    torrent,
                    [{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}],
                    [],
                    pending_entries=[{"bangumi_id": 123, "episode": 8}],
                )

            self.assertEqual(result.extracted_count, 0)
            self.assertEqual(result.failure_reason, "source_video_missing")
            self.assertFalse(result.retryable)
            self.assertIn("Mapped completed torrent path does not exist", result.failure_detail)
            self.assertIn("probably removed before subtitle extraction", result.failure_detail)
            self.assertTrue(result.failure_context["replacement_recommended"])
            self.assertEqual(
                result.failure_context["local_target_candidates"],
                [str(library_dir / "Release Show - S01E08.mkv")],
            )

    def test_completed_download_source_missing_fails_active_pending_for_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir / "Release Show - 08.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [
                QBitTorrentFile("Release/Release Show - 08.mkv", 100, 1.0, 1),
            ]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/failed.torrent",
                    "[Mikan] Different Metadata Name - 08 [SRTx2]",
                    8,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            with patch("mikan_worker.extract_available_subtitles", return_value=[]):
                processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 0)
            qbit.add_tags.assert_called_once_with("hash1", ["mikan"])
            pending_after = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending_after["items"]["123:8"]
            self.assertEqual(entry.get("failed_urls", []), ["https://mikan/failed.torrent"])
            self.assertEqual(_pending_failed_urls(entry), ["https://mikan/failed.torrent"])
            self.assertNotIn("torrent_url", entry)
            self.assertNotIn("queued_at", entry)
            self.assertEqual(entry["last_failure_reason"], "extract_failed")
            self.assertEqual(entry["last_extract_failure_reason"], "source_video_missing")
            self.assertIn("Mapped source video does not exist", entry["last_extract_failure_detail"])
            context = entry["last_extract_context"]
            self.assertEqual(context["qbit_content_path"], str(source_dir / "Release Show - 08.mkv"))
            self.assertEqual(context["qbit_save_path"], str(download_root))
            self.assertEqual(context["mapped_root"], str(source_dir / "Release Show - 08.mkv"))
            self.assertFalse(context["mapped_root_exists"])
            self.assertEqual(context["qbit_files"][0]["mapped_path"], str(source_dir / "Release Show - 08.mkv"))
            self.assertTrue(context["qbit_files"][0]["video"])
            self.assertEqual(entry["last_failed_torrent_url"], "https://mikan/failed.torrent")
            self.assertEqual(entry["last_failed_title"], "[Mikan] Different Metadata Name - 08 [SRTx2]")

    def test_completed_download_skips_torrent_without_active_pending_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            (source_dir / "Release Show - 08.mkv").write_bytes(b"source video")
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-failed-before",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_dir / "Release Show - 08.mkv"),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )
            pending = {
                "items": {
                    "123:8": {
                        "bangumi_id": 123,
                        "episode": 8,
                        "last_extract_failed_at": "2026-06-10T00:00:00+00:00",
                        "last_failure_reason": "extract_failed",
                    }
                }
            }
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            with patch("mikan_worker.extract_available_subtitles") as extract:
                processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 0)
            extract.assert_not_called()
            qbit.add_tags.assert_not_called()

    def test_completed_download_failure_stores_subtitle_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            source_video = source_dir / "Release Show - 08.mkv"
            source_video.write_bytes(b"download video")
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_video),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [QBitTorrentFile("Release/Release Show - 08.mkv", 100, 1.0, 1)]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/unclassified.torrent",
                    "[Mikan] Different Metadata Name - 08 [SRTx2]",
                    8,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            def no_chinese_subtitles(
                _source: Path,
                _config: SimpleNamespace,
                *,
                output_video_path: Path,
                diagnostics: list[dict],
                allowed_languages: set[str],
                **_kwargs,
            ) -> list:
                self.assertEqual(allowed_languages, {"zh-tw", "zh-cn"})
                diagnostics.append(
                    {
                        "source": "embedded",
                        "status": "unclassified",
                        "kind": "text",
                        "codec": "ass",
                        "classification": {
                            "language": None,
                            "reason": "no_language_evidence",
                            "traditional_score": 0,
                            "simplified_score": 0,
                            "japanese_score": 0,
                            "quality_score": 0,
                        },
                    }
                )
                return []

            with patch("mikan_worker.extract_available_subtitles", side_effect=no_chinese_subtitles):
                processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 0)
            pending_after = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending_after["items"]["123:8"]
            self.assertEqual(entry["last_extract_failure_reason"], "subtitle_language_not_supported")
            self.assertIn("scores:", entry["last_extract_failure_detail"])
            self.assertEqual(entry["last_subtitle_diagnostics"][0]["status"], "unclassified")

    def test_completed_download_target_missing_is_deferred_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            source_video = source_dir / "Release Show - 08.mkv"
            source_video.write_bytes(b"download video")
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, download_root)
            torrent = QBitTorrent(
                hash="hash-target-missing",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_video),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [QBitTorrentFile("Release/Release Show - 08.mkv", 100, 1.0, 1)]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/target-missing.torrent",
                    "[Mikan] Release Show - 08 [SRTx2]",
                    8,
                ),
            )
            entry = pending["items"]["123:8"]
            entry["last_progress"] = 1.0
            entry["last_qbit_hash"] = torrent.hash
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            with patch("mikan_worker._target_video_for_torrent_source", return_value=None) as target_match:
                processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 0)
            target_match.assert_called()
            qbit.add_tags.assert_called_once_with("hash-target-missing", ["mikan"])
            pending_after = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry_after = pending_after["items"]["123:8"]
            self.assertEqual(entry_after["torrent_url"], "https://mikan/target-missing.torrent")
            self.assertIn("queued_at", entry_after)
            self.assertEqual(entry_after["last_extract_deferred_reason"], "target_video_not_found")
            self.assertIn("No matching target video", entry_after["last_extract_deferred_detail"])
            self.assertNotIn("last_extract_failed_at", entry_after)
            self.assertNotIn("last_failure_reason", entry_after)
            self.assertEqual(entry_after.get("failed_urls", []), [])
            conn = _mikan_state_connect(config)
            try:
                status = conn.execute("SELECT status FROM mikan_download_items WHERE key = '123:8'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "target_missing")

    def test_completed_download_failure_defers_replacement_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            source_video = source_dir / "Release Show - 08.mkv"
            source_video.write_bytes(b"download video")
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, download_root)
            config.mikan_enabled = True
            config.mikan_bangumi_ids = []
            config.qbit_save_path = "/anime"
            config.qbit_paused = False
            config.mikan_download_start_timeout_seconds = 600
            config.mikan_delete_stalled_torrents = True
            config.mikan_base_url = "https://mikanani.me"
            config.mikan_request_timeout_seconds = 30
            config.mikan_max_items_per_bangumi = 12
            config.mikan_prefer_keywords = ["CHT"]
            config.mikan_reject_keywords = []
            config.mikan_require_extractable_subtitle = False
            config.mikan_no_candidate_retry_seconds = 600
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_video),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [QBitTorrentFile("Release/Release Show - 08.mkv", 100, 1.0, 1)]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/failed.torrent",
                    "[Mikan] Release Show - 08 [CHT]",
                    8,
                ),
            )
            (root / "mikan_pending.json").write_text(json.dumps(pending), encoding="utf-8")

            with (
                patch("mikan_worker.extract_available_subtitles", return_value=[]),
                patch(
                    "mikan_worker.fetch_bangumi_releases",
                    return_value=[
                        _episode_release("https://mikan/failed.torrent", "[Mikan] Release Show - 08 [CHT]", 8),
                        _episode_release("https://mikan/replacement.torrent", "[Mikan] Release Show - 08 [CHT][v2]", 8),
                    ],
                ) as fetch,
                patch("mikan_worker._library_scan_series_mappings", side_effect=AssertionError("full library scan should not run")),
            ):
                processed = worker._process_completed_downloads_unlocked()

            self.assertEqual(processed, 0)
            fetch.assert_not_called()
            self.assertEqual(qbit.list_torrents.call_count, 1)
            qbit.add_url.assert_not_called()
            request = json.loads((root / "mikan_replacement_enqueue.request.json").read_text(encoding="utf-8"))
            self.assertEqual(request["targets"], [{"bangumi_id": 123, "episode": 8}])
            pending_after = json.loads((root / "mikan_pending.json").read_text(encoding="utf-8"))
            entry = pending_after["items"]["123:8"]
            self.assertEqual(entry["failed_urls"], ["https://mikan/failed.torrent"])
            self.assertEqual(entry["last_failed_torrent_url"], "https://mikan/failed.torrent")
            self.assertNotIn("torrent_url", entry)

    def test_completed_download_state_update_defers_when_state_lock_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            download_root = root / "downloads"
            source_dir = download_root / "Release"
            library_dir = root / "anime" / "Release Show" / "Season 1"
            source_dir.mkdir(parents=True)
            library_dir.mkdir(parents=True)
            source_video = source_dir / "Release Show - 08.mkv"
            source_video.write_bytes(b"download video")
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, download_root)
            config.mikan_enabled = True
            config.mikan_bangumi_ids = []
            config.qbit_save_path = "/anime"
            config.qbit_paused = False
            config.mikan_download_start_timeout_seconds = 600
            config.mikan_delete_stalled_torrents = True
            config.mikan_base_url = "https://mikanani.me"
            config.mikan_request_timeout_seconds = 30
            config.mikan_max_items_per_bangumi = 12
            config.mikan_prefer_keywords = ["CHT"]
            config.mikan_reject_keywords = []
            config.mikan_require_extractable_subtitle = False
            config.mikan_no_candidate_retry_seconds = 600
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source_video),
                save_path=str(download_root),
                category="llm-sub",
                tags="mikansub",
            )
            qbit = Mock()
            qbit.list_torrents.return_value = [torrent]
            qbit.list_files.return_value = [QBitTorrentFile("Release/Release Show - 08.mkv", 100, 1.0, 1)]
            worker = MikanWorker(config, _logger())
            worker._qbit = Mock(return_value=qbit)
            worker._series_mappings = Mock(
                return_value=[{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]
            )
            pending = {"items": {}}
            _mark_pending(
                pending,
                _episode_release(
                    "https://mikan/failed.torrent",
                    "[Mikan] Release Show - 08 [CHT]",
                    8,
                ),
            )
            pending_path = root / "mikan_pending.json"
            pending_path.write_text(json.dumps(pending), encoding="utf-8")
            state_lock = VideoLock(root / "mikan_worker")
            self.assertTrue(state_lock.acquire())
            try:
                with patch("mikan_worker.extract_available_subtitles", return_value=[]):
                    processed = worker._process_completed_downloads_unlocked()
            finally:
                state_lock.release()

            self.assertEqual(processed, 0)
            qbit.add_url.assert_not_called()
            request_path = root / "mikan_completed_state_update.request.json"
            self.assertTrue(request_path.exists())
            pending_deferred = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(pending_deferred["items"]["123:8"]["torrent_url"], "https://mikan/failed.torrent")

            with patch(
                "mikan_worker.fetch_bangumi_releases",
                return_value=[
                    _episode_release("https://mikan/failed.torrent", "[Mikan] Release Show - 08 [CHT]", 8),
                    _episode_release("https://mikan/replacement.torrent", "[Mikan] Release Show - 08 [CHT][v2]", 8),
                ],
            ):
                result = worker.consume_completed_state_update_request()

            self.assertFalse(request_path.exists())
            self.assertEqual(result["applied"], 1)
            qbit.add_url.assert_not_called()
            replacement_request = json.loads((root / "mikan_replacement_enqueue.request.json").read_text(encoding="utf-8"))
            self.assertEqual(replacement_request["targets"], [{"bangumi_id": 123, "episode": 8}])
            pending_after = json.loads(pending_path.read_text(encoding="utf-8"))
            entry = pending_after["items"]["123:8"]
            self.assertEqual(entry["failed_urls"], ["https://mikan/failed.torrent"])
            self.assertEqual(entry["last_failed_torrent_url"], "https://mikan/failed.torrent")
            self.assertNotIn("torrent_url", entry)

    def test_completed_fallback_skips_ambiguous_same_episode_across_seasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Multi Season Show"
            season1 = series_dir / "Season 1"
            season2 = series_dir / "Season 2"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            (season1 / "Multi Season Show - S01E06.mkv").write_bytes(b"video")
            (season2 / "Multi Season Show - S02E06.mkv").write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Multi Season Show - 06 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Multi Season Show - 06.mkv"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )

            matches = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                [{"bangumi_id": 123, "path": str(series_dir), "match": ["Multi Season Show"]}],
            )

            self.assertEqual(matches, [])

    def test_completed_fallback_does_not_trust_single_cross_title_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wrong_series_dir = root / "anime" / "Chivalry of a Failed Knight"
            wrong_season = wrong_series_dir / "Season 1"
            wrong_season.mkdir(parents=True)
            (wrong_season / "Chivalry of a Failed Knight - S01E06.mkv").write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Lilith-Raws] Kaguya-sama wa Kokurasetai S02 - 06 [Baha][WEB-DL][1080p][AVC AAC][CHT][MKV]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Kaguya-sama S02E06.mkv"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )

            matches = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                [{"bangumi_id": 123, "path": str(wrong_series_dir), "match": ["Kaguya-sama wa Kokurasetai"]}],
                pending_entries=[{"bangumi_id": 123, "episode": 6, "title": torrent.name}],
            )

            self.assertEqual(matches, [])

    def test_completed_fallback_prefers_torrent_episode_over_stale_pending_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Kaguya-sama Love Is War"
            season2 = series_dir / "Season 2"
            season2.mkdir(parents=True)
            target = season2 / "Kaguya-sama - Love Is War - S02E01.mkv"
            target.write_bytes(b"video")
            (season2 / "Kaguya-sama - Love Is War - S02E02.mkv").write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[FLsnow&SumiSora&LoliHouse] Kaguya-sama wa Kokurasetai S2 - 01 [WebRip 1080p HEVC-10bit AAC ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Kaguya-sama S2 - 01.mkv"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )

            matches = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                [{"bangumi_id": 456, "path": str(series_dir), "match": ["Kaguya-sama Love Is War"]}],
                pending_entries=[{"bangumi_id": 456, "episode": 2, "title": torrent.name}],
            )

            self.assertEqual(matches, [target])

    def test_completed_fallback_uses_only_season_still_missing_chinese_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Multi Season Show"
            season1 = series_dir / "Season 1"
            season2 = series_dir / "Season 2"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            completed = season1 / "Multi Season Show - S01E06.mkv"
            target = season2 / "Multi Season Show - S02E06.mkv"
            completed.write_bytes(b"video")
            target.write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Multi Season Show - 06 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Multi Season Show - 06.mkv"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )

            with patch(
                "mikan_worker._target_has_required_chinese_subtitles",
                side_effect=lambda video: video == completed,
            ):
                matches = _fallback_video_files_for_torrent(
                    torrent,
                    config,
                    _logger(),
                    [{"bangumi_id": 123, "path": str(series_dir), "match": ["Multi Season Show"]}],
                )

            self.assertEqual(matches, [target])

    def test_completed_fallback_uses_pending_bangumi_season_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Multi Season Show"
            season2 = series_dir / "Season 2"
            season3 = series_dir / "Season 3"
            season2.mkdir(parents=True)
            season3.mkdir(parents=True)
            (season2 / "Multi Season Show - S02E08.mkv").write_bytes(b"video")
            target = season3 / "Multi Season Show - S03E08.mkv"
            target.write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Multi Season Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Multi Season Show - 08.mkv"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )
            mappings = [
                {"bangumi_id": 222, "path": str(season2), "match": ["Multi Season Show"]},
                {"bangumi_id": 333, "path": str(season3), "match": ["Multi Season Show"]},
            ]

            matches = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                mappings,
                pending_entries=[{"bangumi_id": 333, "episode": 8}],
            )

            self.assertEqual(matches, [target])

    def test_completed_target_honors_manually_locked_mapping_from_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            series_dir = root / "anime" / "Reviewed Show"
            source_dir.mkdir(parents=True)
            for season in (1, 3):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Reviewed Show - S{season:02d}E09.mkv").write_bytes(b"video")
            target = series_dir / "Season 3" / "Reviewed Show - S03E09.mkv"
            source = source_dir / "[Group] Reviewed Show - 09 [WebRip 1080p].mkv"
            source.write_bytes(b"source")
            torrent = QBitTorrent(
                hash="hash-reviewed",
                name="[Group] Reviewed Show - 09 [WebRip 1080p]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{
                    "bangumi_id": 2218,
                    "path": str(series_dir),
                    "season": 3,
                    "match": [],
                    "manual_locked": True,
                    "confidence": 1.0,
                }],
                pending_entries=[{
                    "bangumi_id": 2218,
                    "episode": 9,
                    "source": "mikan",
                    "torrent_url": "https://example.invalid/reviewed-09.torrent",
                    "pub_date": "2025-03-07T00:00:00+00:00",
                    "title": "Reviewed Show - 09 [WebRip 1080p]",
                }],
            )

            self.assertEqual(selected, target)

    def test_completed_fallback_uses_pending_episode_set_for_range_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Range Show"
            season2 = series_dir / "Season 2"
            season2.mkdir(parents=True)
            target_8 = season2 / "Range Show - S02E08.mkv"
            target_12 = season2 / "Range Show - S02E12.mkv"
            target_8.write_bytes(b"video")
            target_12.write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Range Show [01-12][SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Range Show [01-12]"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )

            matches = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                [{"bangumi_id": 123, "path": str(series_dir), "match": ["Range Show"]}],
                pending_entries=[{"bangumi_id": 123, "episode": 8, "episodes": list(range(1, 13))}],
            )

            self.assertEqual(matches, [target_8])

    def test_completed_fallback_uses_release_year_to_match_library_season(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Utawarerumono"
            season2 = series_dir / "Season 2"
            season3 = series_dir / "Season 3"
            season2.mkdir(parents=True)
            season3.mkdir(parents=True)
            (season2 / "season.nfo").write_text("<season><year>2015</year></season>", encoding="utf-8")
            (season3 / "season.nfo").write_text("<season><year>2022</year></season>", encoding="utf-8")
            (season2 / "Utawarerumono - S02E20.mkv").write_bytes(b"video")
            target = season3 / "Utawarerumono - S03E20.mkv"
            target.write_bytes(b"video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Utawarerumono Futari no Hakuoro - 20 [ASS]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Utawarerumono - 20.mkv"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub",
            )

            matches = _fallback_video_files_for_torrent(
                torrent,
                config,
                _logger(),
                [{"bangumi_id": 2750, "path": str(series_dir), "match": ["Utawarerumono"]}],
                pending_entries=[
                    {
                        "bangumi_id": 2750,
                        "episode": 20,
                        "source": "mikan",
                        "torrent_url": "https://example.invalid/utawarerumono-20.torrent",
                        "pub_date": "2022-11-06T19:06:04+00:00",
                    }
                ],
            )

            self.assertEqual(matches, [target])

    def test_completed_target_uses_source_season_hint_when_episode_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Multi Season Show"
            season1 = series_dir / "Season 1"
            season2 = series_dir / "Season 2"
            source_dir = root / "downloads"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            source_dir.mkdir()
            (season1 / "Multi Season Show - S01E06.mkv").write_bytes(b"video")
            target = season2 / "Multi Season Show - S02E06.mkv"
            target.write_bytes(b"video")
            source = source_dir / "Multi Season Show - S02E06.mkv"
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Multi Season Show - 06 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 123, "path": str(series_dir), "match": ["Multi Season Show"]}],
            )

            self.assertEqual(selected, target)

    def test_completed_target_maps_d4dj_all_mix_to_season_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "D4DJ"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 2):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"D4DJ - S{season:02d}E01.mkv").write_bytes(b"video")
            target = series_dir / "Season 2" / "D4DJ - S02E01.mkv"
            source = source_dir / "[Nekomoe kissaten&LoliHouse] D4DJ All Mix - 01 [ASSx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Nekomoe kissaten&LoliHouse] D4DJ All Mix - 01 [ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 2910, "path": str(series_dir), "match": ["D4DJ"]}],
                pending_entries=[{"bangumi_id": 2910, "episode": 1}],
            )

            self.assertEqual(selected, target)

    def test_completed_target_uses_pending_episode_when_source_has_end_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "RWBY - Ice Queendom"
            season1 = series_dir / "Season 1"
            source_dir = root / "downloads"
            season1.mkdir(parents=True)
            source_dir.mkdir()
            target = season1 / "RWBY - Ice Queendom - S01E12.mkv"
            target.write_bytes(b"video")
            source = source_dir / "[Airota][RWBY - Hyousetsu Teikoku][12END][ASS].mkv"
            torrent = QBitTorrent(
                hash="hash1",
                name="[Airota][RWBY - Hyousetsu Teikoku][12END][ASS]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 2740, "path": str(series_dir), "match": ["RWBY - Ice Queendom"]}],
                pending_entries=[{"bangumi_id": 2740, "episode": 12}],
            )

            self.assertEqual(selected, target)

    def test_completed_target_uses_season_alias_from_auto_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Isekai Quartet"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 2, 3):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Isekai Quartet - S{season:02d}E01.mkv").write_bytes(b"video")
            target = series_dir / "Season 3" / "Isekai Quartet - S03E01.mkv"
            source = source_dir / "[LoliHouse] Isekai Quartet 3 - 01 [SRTx2].mkv"
            torrent = QBitTorrent(
                hash="hash1",
                name="[LoliHouse] Isekai Quartet 3 [01-11]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [
                    {
                        "bangumi_id": 3784,
                        "path": str(series_dir),
                        "match": ["Isekai Quartet", "異世界四重奏 第三季"],
                    }
                ],
                pending_entries=[{"bangumi_id": 3784, "episode": 1}],
            )

            self.assertEqual(selected, target)

    def test_completed_target_uses_mapping_title_to_match_season_nfo_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Rurouni Kenshin (2023)"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            season1 = series_dir / "Season 1"
            season2 = series_dir / "Season 2"
            season1.mkdir(parents=True)
            season2.mkdir(parents=True)
            (season1 / "season.nfo").write_text("<season><title>Tokyo Samurai</title></season>", encoding="utf-8")
            (season2 / "season.nfo").write_text(
                "<season><title>Kyoto Douran</title><originaltitle>Rurouni Kenshin Kyoto Douran</originaltitle></season>",
                encoding="utf-8",
            )
            (season1 / "Rurouni Kenshin (2023) - S01E03.mkv").write_bytes(b"video")
            target = season2 / "Rurouni Kenshin (2023) - S02E03.mkv"
            target.write_bytes(b"video")
            source = source_dir / "[LoliHouse] Rurouni Kenshin (2023) - 03 [ASSx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[LoliHouse] Rurouni Kenshin (2023) - 03 [ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [
                    {
                        "bangumi_id": 3467,
                        "path": str(series_dir),
                        "match": ["Rurouni Kenshin (2023)", "Kyoto Douran"],
                        "title": "Rurouni Kenshin Kyoto Douran",
                    }
                ],
                pending_entries=[{"bangumi_id": 3467, "episode": 3}],
            )

            self.assertEqual(selected, target)

    def test_completed_target_does_not_guess_season_from_generic_mapping_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Rurouni Kenshin (2023)"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 2):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Rurouni Kenshin (2023) - S{season:02d}E03.mkv").write_bytes(b"video")
            source = source_dir / "[LoliHouse] Rurouni Kenshin (2023) - 03 [ASSx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[LoliHouse] Rurouni Kenshin (2023) - 03 [ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 3467, "path": str(series_dir), "match": ["Rurouni Kenshin (2023)"]}],
                pending_entries=[{"bangumi_id": 3467, "episode": 3}],
            )

            self.assertIsNone(selected)

    def test_completed_target_uses_zombie_land_saga_revenge_as_season_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Zombie Land Saga"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 2):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Zombie Land Saga - S{season:02d}E12.mkv").write_bytes(b"video")
            target = series_dir / "Season 2" / "Zombie Land Saga - S02E12.mkv"
            source = source_dir / "[Erai-raws] Zombie Land Saga Revenge - 12 END [1080p].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Erai-raws] Zombie Land Saga Revenge - 12 END [1080p]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 3955, "path": str(series_dir), "match": ["Zombie Land Saga"]}],
                pending_entries=[{"bangumi_id": 3955, "episode": 12}],
            )

            self.assertEqual(selected, target)

    def test_zombie_land_saga_revenge_aliases_do_not_include_higurashi(self) -> None:
        aliases = _sonarr_style_known_title_alias_variants(
            "[Erai-raws] Zombie Land Saga Revenge - 12 END [1080p]"
        )

        self.assertIn("Zombie Land Saga", aliases)
        self.assertIn("Zombie Land Saga Season 2", aliases)
        self.assertNotIn("Higurashi no Naku Koro ni", aliases)

    def test_completed_target_uses_kaguya_roman_two_as_season_two(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Kaguya-sama Love Is War"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 2):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Kaguya-sama - Love Is War - S{season:02d}E01.mkv").write_bytes(b"video")
            target = series_dir / "Season 2" / "Kaguya-sama - Love Is War - S02E01.mkv"
            source = source_dir / "[MMWEB][Kaguya-sama wa Kokurasetai Ⅱ][01][AVC][1080P][CHT].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[MMWEB][Kaguya-sama wa Kokurasetai Ⅱ][01][AVC][1080P][CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 456, "path": str(series_dir), "match": ["Kaguya-sama Love Is War"]}],
            )

            self.assertEqual(selected, target)

    def test_completed_target_uses_monogatari_off_monster_as_season_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Monogatari Series - Off & Monster Season"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 6):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Monogatari Series - Off & Monster Season - S{season:02d}E06.mkv").write_bytes(b"video")
            target = series_dir / "Season 1" / "Monogatari Series - Off & Monster Season - S01E06.mkv"
            source = source_dir / "[Sakurato] Monogatari Series - Off & Monster Season [06][HEVC-10bit 1080p AAC][CHS&CHT].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Sakurato] Monogatari Series： Off & Monster Season [06][HEVC-10bit 1080p AAC][CHS&CHT]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 4600, "path": str(series_dir), "match": ["Monogatari Series - Off & Monster Season"]}],
            )

            self.assertEqual(selected, target)

    def test_completed_target_uses_bleach_thousand_year_blood_war_as_season_seventeen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "Bleach"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 17):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"Bleach - S{season:02d}E12.mkv").write_bytes(b"video")
            target = series_dir / "Season 17" / "Bleach - S17E12.mkv"
            source = source_dir / "[Fyy Raws] Bleach Sennen Kessenhen-Soukokutan - 12 [2160p E-AC-3 Multi-Subs].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Fyy Raws] Bleach Sennen Kessenhen-Soukokutan - 12 [2160p E-AC-3 Multi-Subs]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 98, "path": str(series_dir), "match": ["Bleach"]}],
            )

            self.assertEqual(selected, target)

    def test_sonarr_style_target_matches_genkoku_short_title_to_realist_hero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            target_dir = root / "anime" / "How a Realist Hero Rebuilt the Kingdom" / "Season 1"
            decoy_dir = root / "anime" / "Random Show" / "Season 1"
            target_dir.mkdir(parents=True)
            decoy_dir.mkdir(parents=True)
            target = target_dir / "How a Realist Hero Rebuilt the Kingdom - S01E21.mkv"
            target.write_bytes(b"video")
            (decoy_dir / "Random Show - S01E21.mkv").write_bytes(b"video")
            source = source_dir / "[Nekomoe kissaten] Genkoku 21 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Nekomoe kissaten] Genkoku 21 [WebRip 1080p HEVC-10bit AAC ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [],
            )

            self.assertEqual(selected, target)

    def test_completed_target_does_not_map_higurashi_gou_to_old_higurashi_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            series_dir = root / "anime" / "When They Cry - Higurashi"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            for season in (1, 2):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"When They Cry - Higurashi - S{season:02d}E12.mkv").write_bytes(b"video")
            source = source_dir / "[Sakurato] Higurashi no Naku Koro ni Gou [12].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Sakurato] Higurashi no Naku Koro ni Gou [12]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [{"bangumi_id": 2299, "path": str(series_dir), "match": ["Higurashi"]}],
                pending_entries=[{"bangumi_id": 2299, "episode": 12}],
            )

            self.assertIsNone(selected)

    def test_sonarr_style_target_matches_wistoria_romanized_release_to_english_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            target_dir = root / "anime" / "Wistoria - Wand and Sword" / "Season 1"
            decoy_dir = root / "anime" / "Random Show" / "Season 1"
            target_dir.mkdir(parents=True)
            decoy_dir.mkdir(parents=True)
            target = target_dir / "Wistoria - Wand and Sword - S01E07.mkv"
            target.write_bytes(b"video")
            (decoy_dir / "Random Show - S01E07.mkv").write_bytes(b"video")
            source = source_dir / "[LoliHouse] Tsue to Tsurugi no Wistoria - 07 [SRTx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[LoliHouse] Tsue to Tsurugi no Wistoria - 07 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [],
            )

            self.assertEqual(selected, target)

    def test_sonarr_style_target_matches_slime_romanized_release_to_english_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            target_dir = root / "anime" / "That Time I Got Reincarnated as a Slime" / "Season 3"
            decoy_dir = root / "anime" / "Random Slime Show" / "Season 3"
            target_dir.mkdir(parents=True)
            decoy_dir.mkdir(parents=True)
            target = target_dir / "That Time I Got Reincarnated as a Slime - S03E05.mkv"
            target.write_bytes(b"video")
            (decoy_dir / "Random Slime Show - S03E05.mkv").write_bytes(b"video")
            source = source_dir / "[LoliHouse] Tensei Shitara Slime Datta Ken 3rd Season - 05 [SRTx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[LoliHouse] Tensei Shitara Slime Datta Ken 3rd Season - 05 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [],
            )

            self.assertEqual(selected, target)

    def test_sonarr_style_target_matches_aishiteru_game_to_english_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            target_dir = root / "anime" / "I Want to End This Love Game" / "Season 1"
            target_dir.mkdir(parents=True)
            target = target_dir / "I Want to End This Love Game - S01E03.mkv"
            target.write_bytes(b"video")
            source = source_dir / "[Ends with Love&LoliHouse] Aishiteru Game wo Owarasetai - 03 [ASSx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Ends with Love&LoliHouse] Aishiteru Game wo Owarasetai - 03 [ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [],
            )

            self.assertEqual(selected, target)

    def test_sonarr_style_target_matches_railgun_t_to_english_season_three(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            series_dir = root / "anime" / "A Certain Scientific Railgun"
            for season in (1, 2, 3):
                season_dir = series_dir / f"Season {season}"
                season_dir.mkdir(parents=True)
                (season_dir / f"A Certain Scientific Railgun - S{season:02d}E03.mkv").write_bytes(b"video")
            target = series_dir / "Season 3" / "A Certain Scientific Railgun - S03E03.mkv"
            source = source_dir / "[Airota&LoliHouse] Toaru Kagaku no Railgun T - 03 [ASSx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Airota&LoliHouse] Toaru Kagaku no Railgun T - 03 [ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [],
            )

            self.assertEqual(selected, target)

    def test_sonarr_style_target_matches_aru_majo_to_english_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            target_dir = root / "anime" / "Once Upon a Witch's Death" / "Season 1"
            target_dir.mkdir(parents=True)
            target = target_dir / "Once Upon a Witch's Death - S01E08.mkv"
            target.write_bytes(b"video")
            source = source_dir / "[Nekomoe kissaten&LoliHouse] Aru Majo ga Shinu Made - 08 [ASSx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Nekomoe kissaten&LoliHouse] Aru Majo ga Shinu Made - 08 [ASSx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [],
            )

            self.assertEqual(selected, target)

    def test_sonarr_style_target_skips_close_fuzzy_tie(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            left_dir = root / "anime" / "Ambiguous Target A" / "Season 1"
            right_dir = root / "anime" / "Ambiguous Target B" / "Season 1"
            left_dir.mkdir(parents=True)
            right_dir.mkdir(parents=True)
            (left_dir / "Ambiguous Target A - S01E01.mkv").write_bytes(b"video")
            (right_dir / "Ambiguous Target B - S01E01.mkv").write_bytes(b"video")
            source = source_dir / "[Group] Ambiguous Target - 01 [SRTx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Ambiguous Target - 01 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )
            diagnostics: list[dict[str, object]] = []

            selected = _target_video_for_torrent_source(
                source,
                torrent,
                _mikan_process_config(root, source_dir),
                _logger(),
                [],
                target_diagnostics=diagnostics,
            )

            self.assertIsNone(selected)
            self.assertTrue(any(item.get("reason") == "ambiguous_target_candidates" for item in diagnostics))

    def test_unscoped_target_scans_only_the_proven_series_before_episode_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import mikan_worker

            root = Path(temp_dir)
            anime_root = root / "anime"
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            target_root = anime_root / "Wistoria - Wand and Sword"
            target_dir = target_root / "Season 1"
            decoy_root = anime_root / "Unrelated Show"
            decoy_dir = decoy_root / "Season 1"
            target_dir.mkdir(parents=True)
            decoy_dir.mkdir(parents=True)
            target = target_dir / "Wistoria - Wand and Sword - S01E07.mkv"
            target.write_bytes(b"video")
            (decoy_dir / "Unrelated Show - S01E07.mkv").write_bytes(b"video")
            source = source_dir / "[LoliHouse] Tsue to Tsurugi no Wistoria - 07 [SRTx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[LoliHouse] Tsue to Tsurugi no Wistoria - 07 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )
            original_find = mikan_worker._find_video_files
            scanned_roots: list[Path] = []

            def tracked_find(path: Path, *args, **kwargs):
                scanned_roots.append(Path(path))
                return original_find(path, *args, **kwargs)

            with patch("mikan_worker._find_video_files", side_effect=tracked_find):
                selected = _target_video_for_torrent_source(
                    source,
                    torrent,
                    _mikan_process_config(root, source_dir),
                    _logger(),
                    [],
                )

            self.assertEqual(selected, target)
            self.assertTrue(scanned_roots)
            self.assertNotIn(anime_root, scanned_roots)
            self.assertNotIn(decoy_root, scanned_roots)
            self.assertTrue(all(path == target_root for path in scanned_roots))

    def test_unscoped_target_never_uses_same_episode_without_series_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            import mikan_worker

            root = Path(temp_dir)
            source_dir = root / "downloads"
            source_dir.mkdir(parents=True)
            unrelated_dir = root / "anime" / "Completely Different Show" / "Season 1"
            unrelated_dir.mkdir(parents=True)
            (unrelated_dir / "Completely Different Show - S01E09.mkv").write_bytes(b"video")
            source = source_dir / "[Group] Unknown New Show - 09 [SRTx2].mkv"
            source.write_bytes(b"video")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Unknown New Show - 09 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(source),
                save_path=str(source_dir),
                category="llm-sub",
                tags="mikansub",
            )
            original_find = mikan_worker._find_video_files
            scanned_roots: list[Path] = []

            def tracked_find(path: Path, *args, **kwargs):
                scanned_roots.append(Path(path))
                return original_find(path, *args, **kwargs)

            with patch("mikan_worker._find_video_files", side_effect=tracked_find):
                selected = _target_video_for_torrent_source(
                    source,
                    torrent,
                    _mikan_process_config(root, source_dir),
                    _logger(),
                    [],
                )

            self.assertIsNone(selected)
            self.assertEqual(scanned_roots, [])

    def test_processed_completed_torrent_is_incomplete_when_local_episode_lacks_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library_dir = root / "anime" / "Release Show" / "Season 1"
            library_dir.mkdir(parents=True)
            (library_dir / "Release Show - S01E08.mkv").write_bytes(b"library video")
            config = _mikan_process_config(root, root / "downloads")
            torrent = QBitTorrent(
                hash="hash1",
                name="[Group] Release Show - 08 [SRTx2]",
                progress=1.0,
                state="uploading",
                dlspeed=0,
                downloaded=100,
                added_on=None,
                content_path=str(root / "missing" / "Release Show - 08.mkv"),
                save_path=str(root / "missing"),
                category="llm-sub",
                tags="mikansub,mikansub-extracted",
            )
            mappings = [{"bangumi_id": 123, "path": str(library_dir.parent), "match": ["Release Show"]}]

            complete = _completed_torrent_outputs_complete(torrent, config, _logger(), mappings)

            self.assertFalse(complete)


def _release(url: str) -> MikanRelease:
    return MikanRelease(
        bangumi_id=123,
        title="[Group] Test Anime - 01 [WebRip 1080p][CHT].mkv",
        episode=1,
        torrent_url=url,
        pub_date=None,
        content_length=100,
    )


def _episode_release(url: str, title: str, episode: int) -> MikanRelease:
    return MikanRelease(
        bangumi_id=123,
        title=title,
        episode=episode,
        torrent_url=url,
        pub_date=None,
        content_length=100,
    )


def _range_release(url: str) -> MikanRelease:
    return MikanRelease(
        bangumi_id=123,
        title="[Group] Test Anime S01E01-S01E03 [WebRip 1080p][CHT].mkv",
        episode=1,
        episodes=(1, 2, 3),
        torrent_url=url,
        pub_date=None,
        content_length=300,
    )


def _mikan_enqueue_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        mikan_enabled=True,
        mikan_seen_path="mikan_seen.json",
        mikan_pending_path="mikan_pending.json",
        work_path=root,
        mikan_bangumi_ids=[123],
        qbit_tags=["mikansub"],
        qbit_category="llm-sub",
        qbit_save_path="/anime",
        qbit_paused=False,
        mikan_download_start_timeout_seconds=600,
        mikan_delete_stalled_torrents=True,
        mikan_base_url="https://mikanani.me",
        mikan_request_timeout_seconds=30,
        mikan_max_items_per_bangumi=1,
        mikan_prefer_keywords=["CHT"],
        mikan_reject_keywords=[],
        mikan_require_extractable_subtitle=False,
    )


def _mikan_process_config(root: Path, download_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        mikan_seen_path="mikan_seen.json",
        mikan_pending_path="mikan_pending.json",
        work_path=root,
        input_path=root / "anime",
        video_extensions=[".mkv", ".mp4"],
        qbit_tags=["mikansub"],
        qbit_category="llm-sub",
        qbit_path_mappings=[{"remote": str(download_root), "local": str(download_root)}],
        mikan_processed_tags=["mikansub-extracted"],
        mikan_extract_workers=1,
        mikan_remove_ai_after_extract=True,
        require_ai_subtitles=False,
        ai_japanese_ass_suffix=".AI.ja.ass",
        ai_simplified_chinese_ass_suffix=".AI.zh.ass",
        ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
    )


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.mikan_worker")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
