from __future__ import annotations

import logging
import json
from pathlib import Path
from types import SimpleNamespace
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ai_failure_markers import mark_ai_failure
from audio import AudioStreamInfo, AudioStreamManifest
from output_manifest import write_output_manifest
from scan_state import ScanStateStore, video_scan_signature
from processing_provenance import processing_config_signature
import scanner as scanner_module
from scanner import VideoScanner
from subtitle_extract import SubtitleExtractCancelled, SubtitleExtractError
from subtitle_paths import paths_for_video


class VideoScannerTest(unittest.TestCase):
    def test_english_only_audio_excludes_existing_zero_attempt_obligation_by_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "Anime S01E20.mkv"
            sibling = root / "Anime S01E21.mkv"
            target.write_bytes(b"episode-20")
            sibling.write_bytes(b"episode-21")
            route_enabled = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                transcribe_non_allowed_languages=True,
            )
            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertCountEqual(
                    VideoScanner(route_enabled, _logger()).scan(),
                    [target.resolve(), sibling.resolve()],
                )

            state = ScanStateStore.from_config(route_enabled)
            try:
                obligations = {
                    Path(row[0]).name: str(row[1])
                    for row in state._conn.execute(
                        "SELECT canonical_path, obligation_id FROM ai_delivery_obligations"
                    ).fetchall()
                }
            finally:
                state.close()

            japanese_only = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                transcribe_non_allowed_languages=False,
            )
            manifests = {
                target.resolve(): _english_manifest(codec="truehd", title=""),
                sibling.resolve(): AudioStreamManifest(
                    (AudioStreamInfo(1, "jpn", "Japanese main", True, False),),
                    True,
                ),
            }
            with (
                patch("scanner.extract_available_subtitles", return_value=[]),
                patch(
                    "scanner.probe_audio_stream_manifest",
                    side_effect=lambda video: manifests[Path(video).resolve()],
                ),
            ):
                self.assertEqual(VideoScanner(japanese_only, _logger()).scan(), [sibling.resolve()])

            state = ScanStateStore.from_config(japanese_only)
            try:
                excluded = state.get_ai_delivery_obligation(obligations[target.name])
                self.assertEqual(excluded["state"], "excluded")
                self.assertEqual(excluded["attempt_count"], 0)
                self.assertEqual(excluded["exclusion_code"], "unsupported_media_before_attempt")
                sibling_states = state._conn.execute(
                    "SELECT state, exclusion_code FROM ai_delivery_obligations WHERE canonical_path=?",
                    (str(sibling.resolve()),),
                ).fetchall()
                self.assertIn(("open", ""), sibling_states)
                self.assertNotIn(("excluded", "unsupported_media_before_attempt"), sibling_states)
                queued = {
                    Path(row[0]).name
                    for row in state._conn.execute(
                        "SELECT path FROM ai_candidate_queue"
                    ).fetchall()
                }
                self.assertNotIn(target.name, queued)
                self.assertIn(sibling.name, queued)
            finally:
                state.close()

    def test_verified_sidecar_without_completed_artifact_stays_delivery_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode.mkv"
            video.write_bytes(b"video")
            scanner = VideoScanner(
                _config(root, completed_delivery_enabled=True),
                logging.getLogger("test.completed.delivery.scanner"),
            )
            with (
                patch.object(scanner_module, "has_finished_subtitle", return_value=True),
                patch.object(scanner, "_completed_delivery_satisfied", return_value=False),
            ):
                self.assertEqual(scanner._classify_uncached(video), ("needs_ai", True, False))

    def test_english_only_audio_never_excludes_an_attempted_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S02E01.mkv"
            video.write_bytes(b"episode")
            config = _japanese_only_config(root)
            unknown = AudioStreamManifest(
                (AudioStreamInfo(1, "und", "5.1Ch FLAC 24bit", True, False),),
                True,
            )
            with (
                patch("scanner.extract_available_subtitles", return_value=[]),
                patch("scanner.probe_audio_stream_manifest", return_value=unknown),
            ):
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video.resolve()])

            state = ScanStateStore.from_config(config)
            try:
                obligation_id = str(
                    state._conn.execute(
                        "SELECT obligation_id FROM ai_delivery_obligations WHERE canonical_path=?",
                        (str(video.resolve()),),
                    ).fetchone()[0]
                )
                state.begin_ai_delivery_attempt(obligation_id, started_at=2000)
                state.commit()
            finally:
                state.close()

            video.write_bytes(b"episode-updated")
            with (
                patch("scanner.extract_available_subtitles", return_value=[]),
                patch(
                    "scanner.probe_audio_stream_manifest",
                    return_value=_english_manifest(codec="flac", title="5.1Ch FLAC 24bit"),
                ),
            ):
                VideoScanner(config, _logger()).scan()

            state = ScanStateStore.from_config(config)
            try:
                obligation = state.get_ai_delivery_obligation(obligation_id)
                self.assertEqual(obligation["state"], "open")
                self.assertEqual(obligation["attempt_count"], 1)
                self.assertEqual(obligation["exclusion_code"], "")
            finally:
                state.close()

    def test_audio_admission_preflight_fails_closed_for_unknown_and_probe_error(self) -> None:
        cases = (
            AudioStreamManifest(
                (AudioStreamInfo(1, "und", "Main", True, False),),
                True,
            ),
            AudioStreamManifest((), False, "ffprobe failed"),
        )
        for index, manifest in enumerate(cases, start=1):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / f"Anime S01E{index:02d}.mkv"
                video.write_bytes(b"episode")
                with (
                    patch("scanner.extract_available_subtitles", return_value=[]),
                    patch("scanner.probe_audio_stream_manifest", return_value=manifest),
                ):
                    self.assertEqual(
                        VideoScanner(_japanese_only_config(root), _logger()).scan(),
                        [video.resolve()],
                    )

    def test_audio_admission_preflight_is_bypassed_for_force_ai_and_non_japanese_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            forced = root / "Forced S01E01.mkv"
            routed = root / "Routed S01E02.mkv"
            forced.write_bytes(b"forced")
            routed.write_bytes(b"routed")
            config = _japanese_only_config(root)
            state = ScanStateStore.from_config(config)
            try:
                state.force_ai_queue_candidate(forced)
                state.commit()
            finally:
                state.close()

            with (
                patch.object(VideoScanner, "scan_all", return_value=[forced]),
                patch(
                    "scanner.probe_audio_stream_manifest",
                    side_effect=AssertionError("force AI must bypass audio eligibility"),
                ),
            ):
                self.assertEqual(VideoScanner(config, _logger()).scan(), [forced.resolve()])

            routed_config = _config(
                root,
                language_gate_enabled=True,
                allowed_source_languages=["ja"],
                skip_non_allowed_language=True,
                transcribe_non_allowed_languages=True,
            )
            with (
                patch.object(VideoScanner, "scan_all", return_value=[routed]),
                patch("scanner.extract_available_subtitles", return_value=[]),
                patch(
                    "scanner.probe_audio_stream_manifest",
                    side_effect=AssertionError("non-Japanese route must bypass exclusion"),
                ),
            ):
                routed_candidates = VideoScanner(routed_config, _logger()).scan()
                self.assertIn(routed.resolve(), routed_candidates)

    def test_confirmed_needs_ai_creates_one_delivery_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root)

            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video.resolve()])
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video.resolve()])

            connection = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                row = connection.execute(
                    """
                    SELECT canonical_path, media_size, media_mtime_ns, policy_revision, state
                    FROM ai_delivery_obligations
                    """
                ).fetchone()
                count = connection.execute("SELECT COUNT(*) FROM ai_delivery_obligations").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)
            self.assertEqual(row[0], str(video.resolve()))
            self.assertEqual(row[1], video.stat().st_size)
            self.assertEqual(row[2], video.stat().st_mtime_ns)
            self.assertEqual(row[3], processing_config_signature(config))
            self.assertEqual(row[4], "open")

    def test_scanner_terminal_statuses_exclude_only_pre_attempt_obligations(self) -> None:
        expected_codes = {
            "local_chinese": "local_chinese_subtitle_present_before_attempt",
            "embedded_chinese": "embedded_chinese_subtitle_present_before_attempt",
            "missing": "media_missing_before_attempt",
            "excluded": "standalone_theme_policy",
            "unsupported_media": "unsupported_media_before_attempt",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            scanner = VideoScanner(config, _logger())
            state = scanner._state_store()
            self.assertIsNotNone(state)
            try:
                for index, (status, exclusion_code) in enumerate(expected_codes.items(), start=1):
                    video = root / f"{status} S01E{index:02d}.mkv"
                    video.write_bytes(status.encode("utf-8"))
                    stat_result = video.stat()
                    obligation = state.ensure_ai_delivery_obligation(
                        video,
                        media_size=stat_result.st_size,
                        media_mtime_ns=stat_result.st_mtime_ns,
                        policy_revision=processing_config_signature(config),
                        eligible_at=time.time(),
                    )
                    state.upsert_ai_queue_candidate(video, stat_result.st_mtime_ns)

                    scanner._update_ai_queue(video, status)

                    current = state.get_ai_delivery_obligation(obligation["obligation_id"])
                    self.assertEqual(current["state"], "excluded")
                    self.assertEqual(current["attempt_count"], 0)
                    self.assertEqual(current["exclusion_code"], exclusion_code)

                attempted_video = root / "attempted S01E06.mkv"
                attempted_video.write_bytes(b"attempted")
                attempted_stat = attempted_video.stat()
                attempted = state.ensure_ai_delivery_obligation(
                    attempted_video,
                    media_size=attempted_stat.st_size,
                    media_mtime_ns=attempted_stat.st_mtime_ns,
                    policy_revision=processing_config_signature(config),
                    eligible_at=time.time(),
                )
                state.begin_ai_delivery_attempt(attempted["obligation_id"], started_at=time.time())
                scanner._update_ai_queue(attempted_video, "local_chinese")

                attempted_now = state.get_ai_delivery_obligation(attempted["obligation_id"])
                self.assertEqual(attempted_now["state"], "open")
                self.assertEqual(attempted_now["attempt_count"], 1)
                self.assertEqual(attempted_now["exclusion_code"], "")
            finally:
                scanner._close_state()

    def test_finished_ai_manifest_is_not_mapped_to_scanner_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root)

            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video.resolve()])

            state = ScanStateStore.from_config(config)
            try:
                obligation_id = str(
                    state._conn.execute(
                        "SELECT obligation_id FROM ai_delivery_obligations WHERE canonical_path=?",
                        (str(video.resolve()),),
                    ).fetchone()[0]
                )
            finally:
                state.close()

            outputs = paths_for_video(video, config)
            _write_usable_ass(outputs.ai_ja_ass, "今日は学校へ行きます。")
            _write_usable_ass(outputs.ai_zh_cn_ass, "这里会选择开启网络连接并显示信息")
            _write_usable_ass(outputs.ai_zh_tw_ass, "這裡會選擇開啟網路連線並顯示資訊")
            write_output_manifest(
                video,
                config,
                [outputs.ai_ja_ass, outputs.ai_zh_cn_ass, outputs.ai_zh_tw_ass],
                obligation_id=obligation_id,
            )

            with patch("scanner.extract_available_subtitles") as extract:
                self.assertEqual(VideoScanner(config, _logger()).scan(), [])
            extract.assert_not_called()

            state = ScanStateStore.from_config(config)
            try:
                obligation = state.get_ai_delivery_obligation(obligation_id)
                queue_status = state._conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
                self.assertNotEqual(obligation["state"], "excluded")
                self.assertEqual(obligation["exclusion_code"], "")
                self.assertEqual(queue_status, "done")
            finally:
                state.close()

    def test_local_zh_tw_subtitle_suspends_then_reuses_same_zero_attempt_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root)

            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video.resolve()])

            state = ScanStateStore.from_config(config)
            try:
                original = state._conn.execute(
                    "SELECT obligation_id FROM ai_delivery_obligations WHERE canonical_path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
            finally:
                state.close()

            sidecar = root / "Anime S01E01.zh.ass"
            _write_usable_ass(sidecar, "這裡會選擇開啟網路連線並顯示資訊")
            with patch("scanner.extract_available_subtitles") as extract:
                self.assertEqual(VideoScanner(config, _logger()).scan(), [])
            extract.assert_not_called()

            state = ScanStateStore.from_config(config)
            try:
                excluded = state.get_ai_delivery_obligation(str(original))
                # Valid local zh-TW satisfies delivery without becoming a
                # scanner pre-attempt exclusion.  The still-open zero-attempt
                # identity can be claimed unchanged if that subtitle is later
                # removed.
                self.assertEqual(excluded["state"], "open")
                self.assertEqual(excluded["exclusion_code"], "")
                self.assertEqual(excluded["attempt_count"], 0)
            finally:
                state.close()

            for subtitle in root.glob(f"{video.stem}*.ass"):
                subtitle.unlink()
            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video.resolve()])

            state = ScanStateStore.from_config(config)
            try:
                rows = state._conn.execute(
                    "SELECT obligation_id FROM ai_delivery_obligations WHERE canonical_path=?",
                    (str(video.resolve()),),
                ).fetchall()
                self.assertEqual(rows, [(original,)])
                reopened = state.get_ai_delivery_obligation(str(original))
                self.assertEqual(reopened["state"], "open")
                self.assertEqual(reopened["attempt_count"], 0)
                self.assertEqual(reopened["exclusion_code"], "")
                attempt = state.begin_ai_delivery_attempt(str(original))
                self.assertEqual(attempt["attempt_number"], 1)
            finally:
                state.close()

    def test_scan_skips_standalone_op_ed_but_keeps_normal_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            extras = root / "Show" / "Extras"
            season = root / "Show" / "Season 1"
            extras.mkdir(parents=True)
            season.mkdir(parents=True)
            theme = extras / "S02OP.mkv"
            episode = season / "Show - S01E01.mkv"
            theme.write_bytes(b"theme")
            episode.write_bytes(b"episode")

            with patch("scanner.extract_available_subtitles", return_value=[]):
                videos = VideoScanner(_config(root), _logger()).scan()

            self.assertEqual(videos, [episode.resolve()])

    def test_scan_queues_video_when_embedded_simplified_subtitle_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            extracted_path = root / "Anime S01E01.zh.ass"
            extracted_path.write_text(
                "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,"
                "\u8fd9\u91cc\u4f1a\u9009\u62e9\u5f00\u542f\u7f51\u7edc\u8fde\u63a5\u5e76\u663e\u793a\u4fe1\u606f\n",
                encoding="utf-8",
            )
            extracted = SimpleNamespace(path=extracted_path, language="zh-cn", stream_index=2)

            with patch("scanner.extract_available_subtitles", return_value=[extracted]) as extract:
                videos = VideoScanner(_config(root), _logger()).scan()

            self.assertEqual(videos, [video.resolve()])
            extract.assert_called_once_with(video, _config(root))

    def test_scan_queues_existing_simplified_chinese_sidecar_for_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.zh.ass"
            video.write_text("", encoding="utf-8")
            sidecar.write_text(
                "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,"
                "\u8fd9\u91cc\u4f1a\u9009\u62e9\u5f00\u542f\u7f51\u7edc\u8fde\u63a5\u5e76\u663e\u793a\u4fe1\u606f\n",
                encoding="utf-8",
            )

            with patch("scanner.extract_available_subtitles", return_value=[]) as extract:
                videos = VideoScanner(_config(root), _logger()).scan()

            self.assertEqual(videos, [video.resolve()])
            extract.assert_called_once_with(video, _config(root))

    def test_scan_does_not_mark_existing_official_chinese_as_ai_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.official.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            _write_usable_ass(sidecar, "這裡會選擇開啟網路連線並顯示資訊")
            config = _config(root)

            with patch("scanner.extract_available_subtitles") as extract:
                videos = VideoScanner(config, _logger()).scan()

            self.assertEqual(videos, [])
            extract.assert_not_called()
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute(
                        "SELECT status FROM ai_candidate_queue WHERE path = ?",
                        (str(video.resolve()),),
                    ).fetchone()
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT status FROM ai_job_state WHERE path = ?",
                        (str(video.resolve()),),
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_scan_clears_stale_ai_done_when_only_official_chinese_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.official.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            _write_usable_ass(sidecar, "這裡會選擇開啟網路連線並顯示資訊")
            config = _config(root)
            state = ScanStateStore.from_config(config)
            try:
                state.mark_ai_queue_done(video, "Finished AI subtitle detected before queue processing")
                state.commit()
            finally:
                state.close()

            with patch("scanner.extract_available_subtitles") as extract:
                videos = VideoScanner(config, _logger()).scan()

            self.assertEqual(videos, [])
            extract.assert_not_called()
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute(
                        "SELECT status FROM ai_candidate_queue WHERE path = ?",
                        (str(video.resolve()),),
                    ).fetchone()
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT status FROM ai_job_state WHERE path = ?",
                        (str(video.resolve()),),
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_queued_candidates_does_not_crash_when_existing_subtitle_cleanup_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.official.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            _write_usable_ass(sidecar, "這裡會選擇開啟網路連線並顯示資訊")
            config = _config(root)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            with patch.object(
                scanner_module.ScanStateStore,
                "remove_ai_queue_candidate",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                videos = VideoScanner(config, _logger()).queued_candidates()

            self.assertEqual(videos, [])
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertEqual(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()[0],
                    "queued",
                )
            finally:
                conn.close()

    def test_queued_candidates_reopens_once_after_disk_io_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            scanner = VideoScanner(_config(root), _logger())

            with (
                patch.object(
                    scanner,
                    "_queued_ai_candidates",
                    side_effect=[sqlite3.OperationalError("disk I/O error"), [video]],
                ) as queued,
                patch("scanner.time.sleep") as sleep,
            ):
                videos = scanner.queued_candidates(max_candidates=1)

            self.assertEqual(videos, [video])
            self.assertEqual(queued.call_count, 2)
            sleep.assert_called_once_with(0.25)
            self.assertEqual(scanner.last_database_error, "")
            self.assertEqual(scanner.last_database_error_code, "")

    def test_queued_candidates_exposes_persistent_disk_io_error_to_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scanner = VideoScanner(_config(root), _logger())

            with (
                patch.object(
                    scanner,
                    "_queued_ai_candidates",
                    side_effect=sqlite3.OperationalError("disk I/O error"),
                ) as queued,
                patch("scanner.time.sleep"),
            ):
                videos = scanner.queued_candidates(max_candidates=1)

            self.assertEqual(videos, [])
            self.assertEqual(queued.call_count, 2)
            self.assertEqual(scanner.last_database_error, "disk I/O error")
            self.assertEqual(scanner.last_database_error_code, "scanner_database_disk_io")
            self.assertEqual(scanner.last_database_error_operation, "queued_candidates")

    def test_scan_skips_existing_srt_sidecar_before_ai(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.zh-TW.srt"
            video.write_text("", encoding="utf-8")
            sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\n明天選班長\n", encoding="utf-8")
            normalized = SimpleNamespace(path=root / "Anime S01E01.zh-TW.ass", language="zh-tw", stream_index=-1)

            with (
                patch("scanner.normalize_sidecar_subtitles", return_value=[normalized]) as normalize,
                patch("scanner.extract_available_subtitles") as extract,
            ):
                videos = VideoScanner(_config(root), _logger()).scan()

            self.assertEqual(videos, [])
            normalize.assert_not_called()
            extract.assert_not_called()

    def test_scan_requires_ai_subtitle_even_when_official_chinese_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.official.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            _write_usable_ass(sidecar, "這裡會選擇開啟網路連線並顯示資訊")
            config = _config(root, require_ai_subtitles=True)

            with (
                patch("scanner.normalize_sidecar_subtitles") as normalize,
                patch("scanner.extract_available_subtitles") as extract,
            ):
                videos = VideoScanner(config, _logger()).scan()

            self.assertEqual(videos, [video])
            normalize.assert_not_called()
            extract.assert_not_called()

    def test_scan_force_ai_queues_video_even_when_official_chinese_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.official.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            _write_usable_ass(sidecar, "這裡會選擇開啟網路連線並顯示資訊")
            config = _config(root)

            self.assertEqual(VideoScanner(config, _logger()).scan(), [])
            state = ScanStateStore.from_config(config)
            try:
                state.force_ai_queue_candidate(video)
                self.assertEqual(
                    state.ai_queue_candidate_snapshot(video)["mtime_ns"],
                    video.stat().st_mtime_ns,
                )
                state.commit()
            finally:
                state.close()

            with patch("scanner.extract_available_subtitles") as extract:
                videos = VideoScanner(config, _logger()).scan()

            self.assertEqual(videos, [video.resolve()])
            extract.assert_not_called()
            state = ScanStateStore.from_config(config)
            try:
                queue_mtime = state._conn.execute(
                    "SELECT mtime_ns FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
                obligation = state._conn.execute(
                    """
                    SELECT media_mtime_ns, policy_revision, state, source
                    FROM ai_delivery_obligations
                    WHERE canonical_path=?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
            finally:
                state.close()
            self.assertEqual(queue_mtime, video.stat().st_mtime_ns)
            self.assertEqual(
                obligation,
                (
                    video.stat().st_mtime_ns,
                    processing_config_signature(config),
                    "open",
                    "scan_force_ai",
                ),
            )

    def test_scan_force_ai_bypasses_new_file_age_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, scanner_candidate_min_age_seconds=3600)
            state = ScanStateStore.from_config(config)
            try:
                state.force_ai_queue_candidate(video)
                state.commit()
            finally:
                state.close()

            with patch("scanner.extract_available_subtitles") as extract:
                videos = VideoScanner(config, _logger()).scan(max_candidates=1)

            self.assertEqual(videos, [video.resolve()])
            extract.assert_not_called()

    def test_active_queue_ledger_backfill_repairs_only_stable_current_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            current_policy = processing_config_signature(config)
            missing_obligation = root / "Missing Obligation S01E01.mkv"
            media_mismatch = root / "Media Mismatch S01E02.mkv"
            policy_mismatch = root / "Policy Mismatch S01E03.mkv"
            running_changed = root / "Running Changed S01E04.mkv"
            legacy_force_identity = root / "Legacy Force Identity S01E05.mkv"
            for video in (
                missing_obligation,
                media_mismatch,
                policy_mismatch,
                running_changed,
                legacy_force_identity,
            ):
                video.write_bytes(video.name.encode("utf-8"))

            state = ScanStateStore.from_config(config)
            try:
                for video in (
                    missing_obligation,
                    media_mismatch,
                    policy_mismatch,
                    running_changed,
                    legacy_force_identity,
                ):
                    state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                legacy_force_stat = legacy_force_identity.stat()
                state._conn.execute(
                    """
                    UPDATE ai_candidate_queue
                    SET mtime_ns=?, source='manual_force', force_ai=1
                    WHERE path=?
                    """,
                    (
                        legacy_force_stat.st_mtime_ns + 1_000_000_000,
                        str(legacy_force_identity.resolve()),
                    ),
                )
                media_stat = media_mismatch.stat()
                state.ensure_ai_delivery_obligation(
                    media_mismatch,
                    media_size=media_stat.st_size,
                    media_mtime_ns=media_stat.st_mtime_ns - 1,
                    policy_revision=current_policy,
                )
                policy_stat = policy_mismatch.stat()
                state.ensure_ai_delivery_obligation(
                    policy_mismatch,
                    media_size=policy_stat.st_size,
                    media_mtime_ns=policy_stat.st_mtime_ns,
                    policy_revision="old-policy",
                )
                running_queue_mtime = running_changed.stat().st_mtime_ns
                state.mark_ai_queue_running(running_changed)
                state.commit()
            finally:
                state.close()

            running_changed.write_bytes(b"changed while running")
            os.utime(
                running_changed,
                ns=(running_queue_mtime + 1_000_000_000, running_queue_mtime + 1_000_000_000),
            )
            scanner = VideoScanner(config, _logger())
            with patch.object(scanner, "_classify") as classify:
                result = scanner._backfill_active_queue_obligations(limit=10)
            classify.assert_not_called()
            scanner._close_state()

            self.assertEqual(result["selected"], 5)
            self.assertEqual(result["repaired"], 4)
            self.assertEqual(result["running_identity_changed"], 1)
            connection = sqlite3.connect(config.scanner_state_path)
            try:
                tracked = connection.execute(
                    """
                    SELECT canonical_path, media_mtime_ns, policy_revision, state
                    FROM ai_delivery_obligations
                    WHERE state='open' AND policy_revision=?
                    """,
                    (current_policy,),
                ).fetchall()
                queue_rows = dict(
                    connection.execute(
                        "SELECT path, mtime_ns FROM ai_candidate_queue"
                    ).fetchall()
                )
                running_obligations = connection.execute(
                    "SELECT COUNT(*) FROM ai_delivery_obligations WHERE canonical_path=?",
                    (str(running_changed.resolve()),),
                ).fetchone()[0]
            finally:
                connection.close()

            tracked_by_path = {row[0]: row[1:] for row in tracked}
            for video in (
                missing_obligation,
                media_mismatch,
                policy_mismatch,
                legacy_force_identity,
            ):
                self.assertEqual(
                    tracked_by_path[str(video.resolve())],
                    (video.stat().st_mtime_ns, current_policy, "open"),
                )
            self.assertEqual(queue_rows[str(media_mismatch.resolve())], media_mismatch.stat().st_mtime_ns)
            self.assertEqual(
                queue_rows[str(legacy_force_identity.resolve())],
                legacy_force_identity.stat().st_mtime_ns,
            )
            self.assertEqual(queue_rows[str(running_changed.resolve())], running_queue_mtime)
            self.assertEqual(running_obligations, 0)

    def test_active_queue_ledger_backfill_preserves_failed_media_revision_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            failed_retry = root / "Failed Retry Changed S01E01.mkv"
            paused = root / "Paused Changed S01E02.mkv"
            for video in (failed_retry, paused):
                video.write_bytes(video.name.encode("utf-8"))

            state = ScanStateStore.from_config(config)
            try:
                for video in (failed_retry, paused):
                    state.force_ai_queue_candidate(video)
                state.mark_ai_queue_failed(
                    failed_retry,
                    "retry evidence must remain bound to the old media",
                    retry_after_seconds=60,
                    max_attempts=3,
                    error_code="transient_timeout",
                    retry_strategy="same_pipeline",
                )
                state.mark_ai_queue_failed(
                    paused,
                    "review evidence must remain bound to the old media",
                    max_attempts=1,
                    error_code="deterministic_asr_quality",
                    retry_strategy="manual_review",
                )
                state.commit()
                before_rows = {
                    video: state._conn.execute(
                        """
                        SELECT mtime_ns, status, source, attempts, last_error,
                               last_error_at, last_error_code, retry_strategy,
                               failure_revision, next_retry_at, force_ai
                        FROM ai_candidate_queue WHERE path=?
                        """,
                        (str(video.resolve()),),
                    ).fetchone()
                    for video in (failed_retry, paused)
                }
            finally:
                state.close()

            for video in (failed_retry, paused):
                old_mtime_ns = int(before_rows[video][0])
                video.write_bytes(b"genuine replacement media")
                os.utime(
                    video,
                    ns=(old_mtime_ns + 2_000_000_000, old_mtime_ns + 2_000_000_000),
                )

            scanner = VideoScanner(config, _logger())
            with patch.object(scanner, "_classify") as classify:
                result = scanner._backfill_active_queue_obligations(limit=10)
            classify.assert_not_called()
            scanner._close_state()

            self.assertEqual(result["selected"], 2)
            self.assertEqual(result["media_identity_changed_unproven"], 2)
            self.assertEqual(result.get("repaired", 0), 0)
            connection = sqlite3.connect(config.scanner_state_path)
            try:
                for video in (failed_retry, paused):
                    after_row = connection.execute(
                        """
                        SELECT mtime_ns, status, source, attempts, last_error,
                               last_error_at, last_error_code, retry_strategy,
                               failure_revision, next_retry_at, force_ai
                        FROM ai_candidate_queue WHERE path=?
                        """,
                        (str(video.resolve()),),
                    ).fetchone()
                    self.assertEqual(after_row, before_rows[video])
                obligation_count = connection.execute(
                    "SELECT COUNT(*) FROM ai_delivery_obligations"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(obligation_count, 0)

    def test_active_queue_ledger_backfill_cancelled_before_db_or_stat_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Cancelled S01E01.mkv"
            video.write_bytes(b"episode")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            cancel_event = Mock()
            cancel_event.is_set.return_value = True
            scanner = VideoScanner(config, _logger())
            result = scanner.backfill_active_queue_obligations(cancel_event=cancel_event)

            self.assertEqual(result, {"cancelled": 1})
            connection = sqlite3.connect(config.scanner_state_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM ai_delivery_obligations").fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_active_queue_ledger_backfill_defers_transient_database_busy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scanner = VideoScanner(_config(Path(temp_dir)), _logger())
            with patch.object(
                scanner,
                "_backfill_active_queue_obligations",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                result = scanner.backfill_active_queue_obligations(limit=5)

            self.assertEqual(result, {"database_busy": 1})
            self.assertEqual(scanner.last_database_error_code, "scanner_database_busy")
            self.assertEqual(
                scanner.last_database_error_operation,
                "active_queue_ledger_backfill",
            )

    def test_active_queue_ledger_backfill_logs_info_only_for_repair_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Repair Progress S01E01.mkv"
            video.write_bytes(b"episode")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            logger = Mock()
            result = VideoScanner(config, logger).backfill_active_queue_obligations(limit=1)

            self.assertEqual(result["repaired"], 1)
            logger.info.assert_called_once()

    def test_active_queue_ledger_backfill_throttles_unchanged_blockers_without_info(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            missing = root / "Missing Blocker S01E01.mkv"
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(missing, 123)
                state.commit()
            finally:
                state.close()

            logger = Mock()
            scanner = VideoScanner(config, logger)
            with patch("scanner.time.monotonic", side_effect=[100.0, 200.0, 401.0]):
                results = [
                    scanner._backfill_active_queue_obligations(limit=1)
                    for _ in range(3)
                ]
            scanner._close_state()

            self.assertTrue(all(result["missing_or_unreadable"] == 1 for result in results))
            logger.info.assert_not_called()
            self.assertEqual(logger.warning.call_count, 2)

    def test_scan_state_records_episode_for_fast_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E12.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(root)
            state = ScanStateStore.from_config(config)
            try:
                state.put_status(video_scan_signature(video, config, "cfg"), "needs_ai")
                state.commit()
            finally:
                state.close()

            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                row = conn.execute("SELECT episode FROM video_scan_cache WHERE path = ?", (str(video),)).fetchone()
            finally:
                conn.close()

            self.assertEqual(row[0], 12)

    def test_scan_skips_when_required_ai_subtitle_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            _write_usable_ass(
                root / "Anime S01E01.AI.zh-TW.ass",
                "這裡會選擇開啟網路連線並顯示資訊",
            )
            config = _config(root, require_ai_subtitles=True)

            with patch("scanner.extract_available_subtitles") as extract:
                videos = VideoScanner(config, _logger()).scan()

            self.assertEqual(videos, [])
            extract.assert_not_called()
            db = root / "work" / "scanner_state.sqlite3"
            conn = sqlite3.connect(db)
            try:
                status = conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "done")

    def test_scan_marks_existing_queue_candidate_done_when_ai_subtitle_appears(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, require_ai_subtitles=True)

            self.assertEqual(VideoScanner(config, _logger()).scan(), [video])

            _write_usable_ass(
                root / "Anime S01E01.AI.zh-TW.ass",
                "這裡會選擇開啟網路連線並顯示資訊",
            )
            self.assertEqual(VideoScanner(config, _logger()).scan(), [])

            db = root / "work" / "scanner_state.sqlite3"
            conn = sqlite3.connect(db)
            try:
                status = conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path = ?",
                    (str(video.resolve()),),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(status, "done")

    def test_scan_continues_to_ai_when_embedded_subtitle_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")

            with patch(
                "scanner.extract_available_subtitles",
                side_effect=SubtitleExtractError("ffprobe failed"),
            ):
                videos = VideoScanner(_config(root), _logger()).scan()

            self.assertEqual(videos, [video])

    def test_scan_skips_video_during_ai_failure_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, work_path=root / "work", auto_ai_failure_cooldown_seconds=86400)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.mark_ai_queue_failed(
                    video,
                    "bad model output",
                    retry_after_seconds=86400,
                    max_attempts=3,
                )
                state.commit()
                before = state._conn.execute(
                    """
                    SELECT status, attempts, last_error, next_retry_at
                    FROM ai_candidate_queue
                    WHERE path = ?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
            finally:
                state.close()
            mark_ai_failure(config, video, "translation", "bad model output")

            with patch("scanner.extract_available_subtitles") as extract:
                videos = VideoScanner(config, _logger()).scan()

            self.assertEqual(videos, [])
            extract.assert_not_called()
            state = ScanStateStore.from_config(config)
            try:
                after = state._conn.execute(
                    """
                    SELECT status, attempts, last_error, next_retry_at
                    FROM ai_candidate_queue
                    WHERE path = ?
                    """,
                    (str(video.resolve()),),
                ).fetchone()
                self.assertEqual(after, before)
                self.assertEqual(after[:3], ("failed_retry", 1, "bad model output"))
                self.assertEqual(state.iter_ai_queue_candidates(), [])
            finally:
                state.close()

    def test_manual_retry_runs_during_failure_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, work_path=root / "work", auto_ai_failure_cooldown_seconds=86400)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns, source="manual_retry_failed")
                state.commit()
            finally:
                state.close()
            mark_ai_failure(config, video, "translation", "bad model output")

            with patch("scanner.extract_available_subtitles", return_value=[]) as extract:
                videos = VideoScanner(config, _logger()).scan()

            self.assertEqual(videos, [video.resolve()])
            extract.assert_called_once_with(video.resolve(), config)

    def test_scan_retries_ai_failure_after_ai_model_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            old_config = _config(
                root,
                work_path=root / "work",
                auto_ai_failure_cooldown_seconds=86400,
                whisper_model="old-model",
            )
            new_config = _config(
                root,
                work_path=root / "work",
                auto_ai_failure_cooldown_seconds=86400,
                whisper_model="new-model",
            )
            mark_ai_failure(old_config, video, "transcription", "old model failed")

            with patch("scanner.extract_available_subtitles", return_value=[]) as extract:
                videos = VideoScanner(new_config, _logger()).scan()

            self.assertEqual(videos, [video])
            extract.assert_called_once_with(video, new_config)

    def test_refresh_queue_removes_missing_video_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "Missing S01E01.mkv"
            config = _config(root)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(missing, 1, source="fs_event")
                state.commit()
            finally:
                state.close()

            scanner = VideoScanner(config, _logger())
            with patch.object(scanner, "scan_all", return_value=[missing]):
                refreshed = scanner.refresh_queue()

            self.assertEqual(refreshed, 1)
            state = ScanStateStore.from_config(config)
            try:
                self.assertEqual(state.iter_ai_queue_candidates(), [])
            finally:
                state.close()

    def test_scan_reuses_cached_needs_ai_status_for_unchanged_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root)

            with patch("scanner.extract_available_subtitles", return_value=[]) as extract:
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video])
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video])

            extract.assert_called_once_with(video, config)

    def test_scan_invalidates_cached_status_when_sidecar_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root)

            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(VideoScanner(config, _logger()).scan(), [video])

            sidecar = root / "Anime S01E01.zh.ass"
            _write_usable_ass(sidecar, "這裡會選擇開啟網路連線並顯示資訊")

            with patch("scanner.extract_available_subtitles") as extract:
                self.assertEqual(VideoScanner(config, _logger()).scan(), [])

            extract.assert_not_called()

    def test_scan_prioritizes_recent_videos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "A Old S01E01.mkv"
            new_video = root / "Z New S01E01.mkv"
            old_video.write_text("", encoding="utf-8")
            new_video.write_text("", encoding="utf-8")
            os.utime(old_video, (1_700_000_000, 1_700_000_000))
            os.utime(new_video, (1_800_000_000, 1_800_000_000))

            with patch("scanner.extract_available_subtitles", return_value=[]):
                videos = VideoScanner(_config(root), _logger()).scan()

            self.assertEqual(videos, [new_video, old_video])

    def test_scan_uses_persistent_queue_with_newest_candidate_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "A Old S01E01.mkv"
            new_video = root / "Z New S01E01.mkv"
            old_video.write_text("", encoding="utf-8")
            new_video.write_text("", encoding="utf-8")
            os.utime(old_video, (1_700_000_000, 1_700_000_000))
            os.utime(new_video, (1_800_000_000, 1_800_000_000))
            config = _config(root, scanner_queue_enabled=True)

            with patch("scanner.extract_available_subtitles", return_value=[]):
                videos = VideoScanner(config, _logger()).scan(max_candidates=1)

            self.assertEqual(videos, [new_video])

    def test_scan_preserves_done_queue_when_priority_time_differs_from_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, scanner_queue_enabled=True)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.mark_ai_queue_done(video, "Language gate skipped")
                state.commit()
            finally:
                state.close()

            with (
                patch("scanner.extract_available_subtitles", return_value=[]),
                patch("scanner._safe_priority_time_ns", return_value=video.stat().st_mtime_ns + 999_999_999),
            ):
                videos = VideoScanner(config, _logger()).scan(max_candidates=1)

            self.assertEqual(videos, [])
            state = ScanStateStore.from_config(config)
            try:
                self.assertEqual(state.iter_ai_queue_candidates(), [])
            finally:
                state.close()

    def test_queued_candidates_does_not_scan_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, scanner_queue_enabled=True)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, 10)
                state.commit()
            finally:
                state.close()

            scanner = VideoScanner(config, _logger())
            with patch.object(scanner, "scan_all", side_effect=AssertionError("library scan must not run")):
                self.assertEqual(scanner.queued_candidates(max_candidates=1), [video.resolve()])

    def test_queued_candidates_exact_target_never_returns_neighbor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            neighbor = root / "Neighbor S01E01.mkv"
            target = root / "Target S01E01.mkv"
            missing = root / "Missing S01E01.mkv"
            neighbor.write_bytes(b"neighbor")
            target.write_bytes(b"target")
            config = _config(root, scanner_queue_enabled=True)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(
                    neighbor,
                    neighbor.stat().st_mtime_ns,
                    added_at=2000.0,
                )
                state.upsert_ai_queue_candidate(
                    target,
                    target.stat().st_mtime_ns,
                    added_at=1000.0,
                )
                state.commit()
            finally:
                state.close()

            scanner = VideoScanner(config, _logger())
            self.assertEqual(
                scanner.queued_candidates(max_candidates=1, exact_target=target),
                [target.resolve()],
            )
            self.assertEqual(
                scanner.queued_candidates(max_candidates=1, exact_target=missing),
                [],
            )

            state = ScanStateStore.from_config(config)
            try:
                state.mark_ai_queue_done(target)
                state.commit()
            finally:
                state.close()
            self.assertEqual(
                scanner.queued_candidates(max_candidates=1, exact_target=target),
                [],
            )

    def test_queue_fairness_periodically_selects_oldest_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "Old S01E01.mkv"
            new_video = root / "New S01E01.mkv"
            old_video.write_text("", encoding="utf-8")
            new_video.write_text("", encoding="utf-8")
            config = _config(
                root,
                scanner_queue_enabled=True,
                scanner_queue_oldest_every_n_cycles=2,
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(old_video, 1, added_at=1000.0)
                state.upsert_ai_queue_candidate(new_video, 2, added_at=2000.0)
                state.commit()
            finally:
                state.close()

            scanner = VideoScanner(config, _logger())
            self.assertEqual(scanner.queued_candidates(max_candidates=1), [new_video.resolve()])
            self.assertEqual(scanner.queued_candidates(max_candidates=1), [old_video.resolve()])

    def test_queued_candidate_with_simplified_sidecar_remains_for_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            sidecar = root / "Anime S01E01.\u7b80\u4f53\u4e2d\u6587.zh.ass"
            video.write_text("", encoding="utf-8")
            sidecar.write_text(
                "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,"
                "\u8fd9\u91cc\u4f1a\u9009\u62e9\u5f00\u542f\u7f51\u7edc\u8fde\u63a5\u5e76\u663e\u793a\u4fe1\u606f\n",
                encoding="utf-8",
            )
            config = _config(root, scanner_queue_enabled=True)
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, 10)
                state.commit()
            finally:
                state.close()

            scanner = VideoScanner(config, _logger())
            with patch("scanner.extract_available_subtitles") as extract:
                self.assertEqual(
                    scanner.queued_candidates(max_candidates=1),
                    [video.resolve()],
                )

            extract.assert_not_called()
            state = ScanStateStore.from_config(config)
            try:
                self.assertEqual(state.iter_ai_queue_candidates(), [video.resolve()])
            finally:
                state.close()

    def test_scan_requeues_stale_running_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            config = _config(root, scanner_queue_enabled=True, ai_queue_running_stale_seconds=60)

            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(VideoScanner(config, _logger()).scan(max_candidates=1), [video])

            db = root / "work" / "scanner_state.sqlite3"
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    """
                    UPDATE ai_candidate_queue
                    SET status = 'running', running_at = ?, updated_at = ?
                    WHERE path = ?
                    """,
                    (time.time() - 120, time.time() - 120, str(video.resolve())),
                )
                conn.execute(
                    """
                    UPDATE ai_job_state
                    SET updated_at = ?
                    WHERE path = ?
                    """,
                    (time.time() - 120, str(video.resolve())),
                )
                conn.commit()
            finally:
                conn.close()

            self.assertEqual(VideoScanner(config, _logger()).scan(max_candidates=1), [video])

    def test_quick_scan_filters_old_files_but_keeps_existing_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "A Old S01E01.mkv"
            new_video = root / "Z New S01E01.mkv"
            old_video.write_text("", encoding="utf-8")
            new_video.write_text("", encoding="utf-8")
            now_ns = 1_800_000_000 * 1_000_000_000
            old_time = 1_700_000_000
            new_time = 1_800_000_000
            os.utime(old_video, (old_time, old_time))
            os.utime(new_video, (new_time, new_time))
            config = _config(
                root,
                scanner_queue_enabled=True,
                scanner_incremental_scan_enabled=True,
                scanner_incremental_overlap_seconds=0,
                scanner_full_scan_interval_seconds=0,
            )
            scanner = VideoScanner(config, _logger())

            with (
                patch("scanner.time.monotonic", return_value=1000.0),
                patch("scanner.time.time_ns", return_value=now_ns),
                patch("scanner.extract_available_subtitles", return_value=[]),
            ):
                self.assertEqual(scanner.scan(max_candidates=2), [new_video, old_video])

            with (
                patch("scanner.time.monotonic", return_value=1100.0),
                patch("scanner.time.time_ns", return_value=now_ns),
                patch("scanner.extract_available_subtitles", return_value=[]),
            ):
                self.assertEqual(scanner.scan(max_candidates=2), [new_video, old_video])

            with (
                patch("scanner.time.monotonic", return_value=1100.0),
                patch("scanner.time.time_ns", return_value=now_ns),
            ):
                scanned = scanner.scan_all()
            self.assertEqual(scanned, [new_video])

    def test_incremental_scan_preserves_earlier_delivery_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_video = root / "A Old S01E01.mkv"
            old_video.write_text("", encoding="utf-8")
            old_time = 1_700_000_000
            new_time = 1_800_000_000
            os.utime(old_video, (old_time, old_time))
            config = _config(
                root,
                scanner_queue_enabled=True,
                scanner_incremental_scan_enabled=True,
                scanner_incremental_overlap_seconds=0,
                scanner_full_scan_interval_seconds=0,
            )
            scanner = VideoScanner(config, _logger())

            with (
                patch("scanner.time.monotonic", return_value=1000.0),
                patch("scanner.extract_available_subtitles", return_value=[]),
            ):
                self.assertEqual(scanner.scan(max_candidates=2), [old_video])

            new_video = root / "Z New S01E01.mkv"
            new_video.write_text("", encoding="utf-8")
            os.utime(new_video, (new_time, new_time))

            with (
                patch("scanner.time.monotonic", return_value=1100.0),
                patch("scanner.extract_available_subtitles", return_value=[]),
            ):
                self.assertEqual(scanner.scan(max_candidates=2), [old_video, new_video])

    def test_media_walk_yields_io_and_filters_non_video_before_stat_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            subtitle = root / "Anime S01E01.zh.ass"
            video.write_text("", encoding="utf-8")
            subtitle.write_text("subtitle", encoding="utf-8")
            scanner = VideoScanner(
                _config(
                    root,
                    scanner_walk_yield_every_entries=1,
                    scanner_walk_yield_seconds=0.01,
                ),
                _logger(),
            )

            with patch("scanner.time.sleep") as sleep:
                scanned = scanner.scan_all()

            self.assertEqual(scanned, [video])
            self.assertGreaterEqual(sleep.call_count, 2)

    def test_scan_fails_closed_and_requests_stopped_service_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            state_path = root / "work" / "scanner_state.sqlite3"
            state_path.parent.mkdir(parents=True)
            state_path.write_bytes(b"this is not a sqlite database")
            source_deployment_id = "20260827T090539Z-1500094"
            (state_path.parent / "scanner_state_recovery_anchor.json").write_text(
                json.dumps({
                    "status": "verified",
                    "source_deployment_id": source_deployment_id,
                }),
                encoding="utf-8",
            )

            with patch("scanner.extract_available_subtitles", return_value=[]):
                with self.assertRaises(sqlite3.DatabaseError):
                    VideoScanner(_config(root), _logger()).scan()

            self.assertEqual(state_path.read_bytes(), b"this is not a sqlite database")
            self.assertEqual(list(state_path.parent.glob("scanner_state.sqlite3.corrupt-*")), [])
            request = json.loads(
                (state_path.parent / "scanner_state_recovery_required.json").read_text(
                    encoding="utf-8"
                )
            )
            hold = json.loads(
                (state_path.parent / "deployment_hold.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request["status"], "pending")
            self.assertEqual(request["source_deployment_id"], source_deployment_id)
            self.assertEqual(request["operation"], "scan")
            self.assertTrue(hold["active"])
            self.assertEqual(hold["deployment_id"], request["recovery_id"])
            self.assertEqual(hold["reason"], "scanner-state-corruption")

    def test_batched_reconciliation_finalizes_durable_inventory_after_path_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
            scanner = VideoScanner(config, _logger())

            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 1)

            connection = sqlite3.connect(config.scanner_state_path)
            try:
                epoch = connection.execute(
                    """
                    SELECT state, observed_count, delivery_required_count,
                           tracked_count, untracked_count, coverage_complete
                    FROM ai_inventory_epochs ORDER BY started_at DESC LIMIT 1
                    """
                ).fetchone()
                observation = connection.execute(
                    "SELECT classification, disposition FROM ai_media_inventory"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(epoch, ("completed", 1, 1, 1, 0, 1))
            self.assertEqual(observation, ("needs_ai", "delivery_required"))
            self.assertTrue(scanner.reconcile_cycle_complete)

    def test_reconcile_caps_uncached_extract_deadline_and_does_not_cache_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(
                root,
                scanner_reconcile_batch_size=10,
                scanner_reconcile_budget_seconds=5,
                scanner_inventory_file_timeout_seconds=300,
            )
            scanner = VideoScanner(config, _logger())
            clock = [100.0]
            normalization_deadlines: list[float] = []
            extract_deadlines: list[float] = []
            classifications: list[tuple[str, str, bool]] = []
            original_classify = scanner._classify

            def capture_normalization(_video: Path, _config: object, **kwargs: object):
                normalization_deadlines.append(float(kwargs["deadline_monotonic"]))
                return []

            def timeout_extract(_video: Path, _config: object, **kwargs: object):
                deadline = float(kwargs["deadline_monotonic"])
                extract_deadlines.append(deadline)
                clock[0] = deadline
                raise SubtitleExtractCancelled("injected inventory deadline")

            def capture_classification(
                path: Path,
                *,
                deadline_monotonic: float | None = None,
            ) -> tuple[str, str, bool]:
                result = original_classify(path, deadline_monotonic=deadline_monotonic)
                classifications.append(result)
                return result

            with (
                patch("scanner.time.monotonic", side_effect=lambda: clock[0]),
                patch("scanner.normalize_sidecar_subtitles", side_effect=capture_normalization),
                patch("scanner.extract_available_subtitles", side_effect=timeout_extract),
                patch.object(scanner, "_classify", side_effect=capture_classification),
            ):
                self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 1)

            self.assertEqual(normalization_deadlines, [105.0])
            self.assertEqual(extract_deadlines, [105.0])
            self.assertEqual(classifications, [("needs_ai", "fresh", True)])
            self.assertFalse(scanner.reconcile_cycle_complete)

            state = ScanStateStore.from_config(config)
            try:
                signature = video_scan_signature(video, config, scanner._config_signature)
                self.assertIsNone(state.get_status(signature))
            finally:
                state.close()

    def test_ordinary_uncached_classification_preserves_no_extract_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root, scanner_inventory_file_timeout_seconds=1)
            scanner = VideoScanner(config, _logger())
            extract_deadlines: list[float | None] = []

            def capture_extract(
                _video: Path,
                _config: object,
                *,
                deadline_monotonic: float | None = None,
            ) -> list[object]:
                extract_deadlines.append(deadline_monotonic)
                return []

            with (
                patch("scanner.normalize_sidecar_subtitles", return_value=[]) as normalize,
                patch("scanner.extract_available_subtitles", side_effect=capture_extract) as extract,
            ):
                self.assertEqual(scanner._classify(video), ("needs_ai", "fresh", False))

            normalize.assert_called_once_with(video, config)
            extract.assert_called_once_with(video, config)
            self.assertEqual(extract_deadlines, [None])
            scanner._close_state()

    def test_batched_reconciliation_retries_transient_locks_without_failing_or_skipping(self) -> None:
        for error_message in ("database is locked", "locking protocol"):
            with self.subTest(error_message=error_message), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                videos = [root / f"Anime S01E{episode:02d}.mkv" for episode in (1, 2)]
                for episode, video in enumerate(videos, start=1):
                    video.write_bytes(str(episode).encode())
                config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
                scanner = VideoScanner(config, _logger())
                original_record = ScanStateStore.record_ai_inventory_observation
                attempted_paths: list[Path] = []

                def record_with_one_lock(
                    state: ScanStateStore,
                    epoch_id: str,
                    path: Path,
                    **kwargs: object,
                ) -> dict[str, object]:
                    attempted_paths.append(path)
                    if len(attempted_paths) == 1:
                        raise sqlite3.OperationalError(error_message)
                    return original_record(state, epoch_id, path, **kwargs)

                with (
                    patch("scanner.extract_available_subtitles", return_value=[]),
                    patch.object(
                        ScanStateStore,
                        "record_ai_inventory_observation",
                        new=record_with_one_lock,
                    ),
                ):
                    self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 0)
                    self.assertFalse(scanner.reconcile_cycle_complete)
                    self.assertEqual(scanner._reconcile_pending_item[0], videos[0])
                    connection = sqlite3.connect(config.scanner_state_path)
                    try:
                        self.assertEqual(
                            connection.execute(
                                "SELECT state, observed_count FROM ai_inventory_epochs"
                            ).fetchone(),
                            ("running", 0),
                        )
                    finally:
                        connection.close()

                    self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 2)

                self.assertEqual(attempted_paths, [videos[0], videos[0], videos[1]])
                connection = sqlite3.connect(config.scanner_state_path)
                try:
                    self.assertEqual(
                        connection.execute(
                            "SELECT state, observed_count FROM ai_inventory_epochs"
                        ).fetchone(),
                        ("completed", 2),
                    )
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM ai_media_inventory").fetchone()[0],
                        2,
                    )
                finally:
                    connection.close()

    def test_batched_reconciliation_avoids_wal_snapshot_upgrade_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
            scanner = VideoScanner(config, _logger())

            def classify_while_ai_child_commits(
                _video: Path,
                *,
                deadline_monotonic: float | None = None,
            ) -> tuple[str, str, bool]:
                del deadline_monotonic
                competing_writer = ScanStateStore(config.scanner_state_path)
                try:
                    competing_writer.update_ai_job_stage(
                        video,
                        "transcription",
                        "running",
                        "AI child heartbeat",
                    )
                    competing_writer.commit()
                finally:
                    competing_writer.close()
                return "needs_ai", "fresh", False

            with patch.object(scanner, "_classify", side_effect=classify_while_ai_child_commits):
                self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 1)

            connection = sqlite3.connect(config.scanner_state_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT state, observed_count FROM ai_inventory_epochs"
                    ).fetchone(),
                    ("completed", 1),
                )
            finally:
                connection.close()

    def test_restarted_scanner_resumes_partial_epoch_and_rewalks_from_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for episode in (1, 2):
                (root / f"Anime S01E{episode:02d}.mkv").write_bytes(str(episode).encode())
            config = _config(root, scanner_reconcile_batch_size=1, scanner_reconcile_budget_seconds=60)
            with patch("scanner.extract_available_subtitles", return_value=[]):
                first_scanner = VideoScanner(config, _logger())
                self.assertEqual(first_scanner.refresh_queue(reconcile_batch=True), 1)
                second_scanner = VideoScanner(config, _logger())
                self.assertEqual(second_scanner.refresh_queue(reconcile_batch=True), 1)
                self.assertEqual(second_scanner.refresh_queue(reconcile_batch=True), 1)
                self.assertEqual(second_scanner.refresh_queue(reconcile_batch=True), 0)

            connection = sqlite3.connect(config.scanner_state_path)
            try:
                states = connection.execute(
                    "SELECT state, observed_count FROM ai_inventory_epochs ORDER BY started_at"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(states, [("completed", 2)])

    def test_walk_error_persists_failed_epoch_and_never_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
            scanner = VideoScanner(config, _logger())

            def broken_walk(*_args, **_kwargs):
                raise scanner_module.InventoryWalkError("permission denied")
                yield  # pragma: no cover

            with patch.object(scanner, "_walk_video_files", side_effect=broken_walk):
                with self.assertRaisesRegex(scanner_module.InventoryWalkError, "permission denied"):
                    scanner.refresh_queue(reconcile_batch=True)

            connection = sqlite3.connect(config.scanner_state_path)
            try:
                row = connection.execute(
                    "SELECT state, completed_at, failure_code FROM ai_inventory_epochs"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("failed", 0.0, "walk_error"))

    def test_classification_error_persists_failed_epoch_and_never_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Anime S01E01.mkv").write_bytes(b"episode")
            config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
            scanner = VideoScanner(config, _logger())

            with patch.object(scanner, "_classify", side_effect=RuntimeError("classifier failed")):
                with self.assertRaisesRegex(RuntimeError, "classifier failed"):
                    scanner.refresh_queue(reconcile_batch=True)

            connection = sqlite3.connect(config.scanner_state_path)
            try:
                row = connection.execute(
                    "SELECT state, completed_at, failure_code FROM ai_inventory_epochs"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("failed", 0.0, "classification_error"))

    def test_inventory_observation_failure_rolls_back_queue_and_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "Anime S01E01.mkv").write_bytes(b"episode")
            config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
            scanner = VideoScanner(config, _logger())
            with (
                patch("scanner.extract_available_subtitles", return_value=[]),
                patch.object(
                    ScanStateStore,
                    "record_ai_inventory_observation",
                    side_effect=RuntimeError("injected observation failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected observation failure"):
                    scanner.refresh_queue(reconcile_batch=True)

            connection = sqlite3.connect(config.scanner_state_path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_candidate_queue").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_delivery_obligations").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_media_inventory").fetchone()[0], 0)
                self.assertEqual(
                    connection.execute("SELECT state FROM ai_inventory_epochs").fetchone()[0],
                    "failed",
                )
            finally:
                connection.close()

    def test_ordinary_scan_new_needs_ai_dirties_completed_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            scanner = VideoScanner(config, _logger())
            state = ScanStateStore(config.scanner_state_path)
            epoch = state.begin_ai_inventory_epoch(
                policy_revision=scanner._processing_policy_revision,
                root_signature=scanner._inventory_root_signature,
            )
            state.finalize_ai_inventory_epoch(epoch["epoch_id"])
            state.commit()
            state.close()
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")

            with (
                patch.object(scanner, "scan_all", return_value=[video]),
                patch.object(scanner, "_classify", return_value=("needs_ai", "fresh", False)),
            ):
                self.assertEqual(scanner.refresh_queue(), 1)

            state = ScanStateStore(config.scanner_state_path)
            try:
                self.assertEqual(state.ai_inventory_coverage_summary()["state"], "inventory_dirty")
            finally:
                state.close()

    def test_ordinary_scan_unchanged_attested_media_does_not_dirty_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
            scanner = VideoScanner(config, _logger())
            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 1)

            scanner = VideoScanner(config, _logger())
            with (
                patch.object(scanner, "scan_all", return_value=[video]),
                patch.object(scanner, "_classify", return_value=("needs_ai", "cached", False)),
            ):
                self.assertEqual(scanner.refresh_queue(), 1)

            state = ScanStateStore(config.scanner_state_path)
            try:
                self.assertTrue(state.ai_inventory_coverage_summary()["complete"])
                generation = dict(
                    state._conn.execute("SELECT key, value FROM ai_delivery_meta")
                )["inventory_dirty_generation"]
                self.assertEqual(int(generation), 0)
            finally:
                state.close()

    def test_ordinary_scan_new_finished_ai_dirties_completed_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            scanner = VideoScanner(config, _logger())
            state = ScanStateStore(config.scanner_state_path)
            epoch = state.begin_ai_inventory_epoch(
                policy_revision=scanner._processing_policy_revision,
                root_signature=scanner._inventory_root_signature,
            )
            state.finalize_ai_inventory_epoch(epoch["epoch_id"])
            state.commit()
            state.close()
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")

            with (
                patch.object(scanner, "scan_all", return_value=[video]),
                patch.object(scanner, "_classify", return_value=("finished", "fresh", False)),
                patch("scanner.has_ai_finished_subtitle", return_value=True),
                patch("scanner.ai_finished_subtitle_mtime", return_value=time.time()),
                patch.object(scanner, "_has_required_finished_subtitle", return_value=True),
            ):
                self.assertEqual(scanner.refresh_queue(), 1)

            state = ScanStateStore(config.scanner_state_path)
            try:
                self.assertEqual(state.ai_inventory_coverage_summary()["state"], "inventory_dirty")
            finally:
                state.close()

    def test_ordinary_queue_cleanup_for_removed_media_dirties_completed_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"episode")
            config = _config(root, scanner_reconcile_batch_size=10, scanner_reconcile_budget_seconds=60)
            scanner = VideoScanner(config, _logger())
            with patch("scanner.extract_available_subtitles", return_value=[]):
                self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 1)
            video.unlink()

            scanner = VideoScanner(config, _logger())
            self.assertEqual(scanner._queued_ai_candidates(), [])
            scanner._close_state()

            state = ScanStateStore(config.scanner_state_path)
            try:
                self.assertEqual(state.ai_inventory_coverage_summary()["state"], "inventory_dirty")
            finally:
                state.close()


def _config(root: Path, **overrides: object) -> SimpleNamespace:
    config = {
        "input_path": root,
        "work_path": root / "work",
        "auto_ai_failure_cooldown_seconds": 0,
        "ai_queue_running_stale_seconds": 21600,
        "scanner_cache_enabled": True,
        "scanner_state_path": root / "work" / "scanner_state.sqlite3",
        "scanner_recent_first": True,
        "scanner_queue_enabled": True,
        "scanner_skip_standalone_op_ed": True,
        "scanner_incremental_scan_enabled": False,
        "scanner_incremental_overlap_seconds": 300,
        "scanner_quick_scan_recent_days": 0,
        "scanner_full_scan_interval_seconds": 0,
        "scanner_inventory_file_timeout_seconds": 30,
        "video_extensions": [".mkv"],
        "export_ai_ass": True,
        "ai_japanese_ass_suffix": ".AI.ja.ass",
        "ai_simplified_chinese_ass_suffix": ".AI.zh.ass",
        "ai_traditional_chinese_ass_suffix": ".AI.zh-TW.ass",
        "finished_subtitle_suffixes": [".official.zh-TW.ass"],
        "mikan_remove_ai_after_extract": True,
        "require_ai_subtitles": False,
    }
    config.update(overrides)
    return SimpleNamespace(**config)


def _japanese_only_config(root: Path, **overrides: object) -> SimpleNamespace:
    return _config(
        root,
        language_gate_enabled=True,
        allowed_source_languages=["ja"],
        skip_non_allowed_language=True,
        transcribe_non_allowed_languages=False,
        **overrides,
    )


def _write_usable_ass(path: Path, text: str) -> None:
    path.write_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{text}\n",
        encoding="utf-8-sig",
    )


def _english_manifest(*, codec: str, title: str) -> AudioStreamManifest:
    return AudioStreamManifest(
        (
            AudioStreamInfo(
                index=1,
                language="eng",
                title=title,
                default=True,
                commentary=False,
                codec_name=codec,
                channels=6,
            ),
        ),
        True,
    )


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.scanner")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


if __name__ == "__main__":
    unittest.main()
