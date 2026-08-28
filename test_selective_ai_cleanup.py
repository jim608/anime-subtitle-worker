from __future__ import annotations

from pathlib import Path
import os
import sqlite3
import tempfile
import unittest

from subtitle_quality import managed_quality_report_path

from selective_ai_cleanup import (
    CleanupTarget,
    _ai_srt_cache_base_from_container_path,
    _ass_first_dialogue_seconds,
    _ass_translation_pollution_reason,
    _container_to_local_video_path,
    _requeue_targets,
    _japanese_ai_ass_candidates,
    _select_asr_prompt_echo_targets,
    _select_leading_gap_targets,
    _select_translation_pollution_targets,
)


class SelectiveAiCleanupTest(unittest.TestCase):
    def test_leading_gap_audit_selects_late_opening_and_records_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime_root = root / "anime"
            work_path = root / "work"
            video = anime_root / "Show" / "Season 1" / "Show - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            container_path = "/anime/Show/Season 1/Show - S01E01.mkv"
            japanese = video.with_name(f"{video.stem}.AI日本語.ja.ass")
            japanese.write_text(
                "[Events]\n"
                "Dialogue: 0,0:00:25.50,0:00:27.00,Default,,0,0,0,,遅い最初の字幕\n",
                encoding="utf-8",
            )
            translated = video.with_name(f"{video.stem}.AI繁日雙語.zh-TW.ass")
            translated.write_text(japanese.read_text(encoding="utf-8"), encoding="utf-8")
            work_path.mkdir(parents=True)
            db_path = work_path / "scanner_state.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY, status TEXT NOT NULL)")
                conn.execute("INSERT INTO ai_candidate_queue(path, status) VALUES (?, 'done')", (container_path,))
                conn.commit()
            finally:
                conn.close()

            targets = _select_leading_gap_targets(
                db_path,
                work_path,
                anime_root,
                "/anime",
                minimum_gap_seconds=12.0,
                progress_interval_seconds=0,
            )

            self.assertEqual(_ass_first_dialogue_seconds(japanese), 25.5)
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].detected_leading_gap_seconds, 25.5)
            self.assertEqual(targets[0].pollution_reasons, ("leading_gap",))
            self.assertIn(japanese, targets[0].media_outputs)
            self.assertIn(translated, targets[0].media_outputs)

    def test_container_path_maps_with_native_path_components(self) -> None:
        root = Path("/media/anime")

        mapped = _container_to_local_video_path(
            "/anime/Show/Season 1/Show - S01E01.mkv",
            root,
            "/anime",
        )

        self.assertEqual(mapped, root / "Show" / "Season 1" / "Show - S01E01.mkv")

    def test_detects_prompt_leak_and_collects_all_outputs_for_clean_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime_root = root / "anime"
            work_path = root / "work"
            video = anime_root / "Show" / "Season 1" / "Show - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_text("", encoding="utf-8")
            container_path = "/anime/Show/Season 1/Show - S01E01.mkv"
            running_video = video.with_name("Show - S01E02.mkv")
            running_video.write_text("", encoding="utf-8")
            running_container_path = "/anime/Show/Season 1/Show - S01E02.mkv"

            translated = video.with_name(f"{video.stem}.AI繁日雙語.zh-TW.ass")
            translated.write_text(
                "[Events]\n"
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
                "請逐行翻譯下列字幕。每一行都必須輸出，格式必須是：原編號<TAB>中文字幕。\n",
                encoding="utf-8",
            )
            translated.with_name(translated.name + ".quality.json").write_text("{}", encoding="utf-8")
            managed_report = managed_quality_report_path(translated, work_path)
            managed_report.parent.mkdir(parents=True, exist_ok=True)
            managed_report.write_text("{}", encoding="utf-8")
            japanese = video.with_name(f"{video.stem}.AI日本語.ja.ass")
            japanese.write_text("[Events]\n", encoding="utf-8")
            running_translated = running_video.with_name(
                f"{running_video.stem}.AI繁日雙語.zh-TW.ass"
            )
            running_translated.write_text(translated.read_text(encoding="utf-8"), encoding="utf-8")

            cache_root = work_path / "ai_srt_cache"
            cache_root.mkdir(parents=True)
            cache_base = _ai_srt_cache_base_from_container_path(container_path, cache_root)
            cache = cache_root / f"{cache_base.name}.AI繁日雙語.zh-TW.srt"
            cache.write_text("bad cache", encoding="utf-8")
            japanese_cache = cache_root / f"{cache_base.name}.AI日本語.ja.srt"
            japanese_cache.write_text("clean Japanese cache", encoding="utf-8")

            db_path = work_path / "scanner_state.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY, status TEXT NOT NULL)")
                conn.execute("INSERT INTO ai_candidate_queue(path, status) VALUES (?, 'done')", (container_path,))
                conn.execute(
                    "INSERT INTO ai_candidate_queue(path, status) VALUES (?, 'running')",
                    (running_container_path,),
                )
                conn.commit()
            finally:
                conn.close()

            targets = _select_translation_pollution_targets(
                db_path,
                work_path,
                anime_root,
                "/anime",
                pollution_kind="prompt-leak",
                progress_interval_seconds=0,
            )

            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].container_video_path, container_path)
            self.assertEqual(targets[0].pollution_reasons, ("prompt_leak",))
            self.assertIn(translated, targets[0].media_outputs)
            self.assertIn(translated.with_name(translated.name + ".quality.json"), targets[0].media_outputs)
            self.assertNotIn(japanese, targets[0].media_outputs)
            self.assertEqual(set(targets[0].cache_outputs), {cache, managed_report})
            self.assertNotIn(japanese_cache, targets[0].cache_outputs)

    def test_requeue_refreshes_priority_and_clears_finished_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "anime" / "Show" / "Season 1" / "Show - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_text("", encoding="utf-8")
            container_path = "/anime/Show/Season 1/Show - S01E01.mkv"
            db_path = root / "scanner_state.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE ai_candidate_queue(
                        path TEXT PRIMARY KEY,
                        mtime_ns INTEGER,
                        status TEXT,
                        source TEXT,
                        attempts INTEGER,
                        running_at REAL,
                        last_error TEXT,
                        last_error_at REAL,
                        next_retry_at REAL,
                        force_ai INTEGER,
                        added_at REAL,
                        updated_at REAL
                    );
                    CREATE TABLE ai_job_state(path TEXT);
                    CREATE TABLE ai_stage_events(path TEXT);
                    """
                )
                conn.execute(
                    """
                    INSERT INTO ai_candidate_queue(
                        path, mtime_ns, status, source, attempts, running_at,
                        last_error, last_error_at, next_retry_at, force_ai,
                        added_at, updated_at
                    ) VALUES (?, 0, 'done', 'old', 4, 0, 'old error', 1, 2, 0, 1, 1)
                    """,
                    (container_path,),
                )
                conn.execute("INSERT INTO ai_job_state(path) VALUES (?)", (container_path,))
                conn.execute("INSERT INTO ai_stage_events(path) VALUES (?)", (container_path,))
                conn.commit()
            finally:
                conn.close()

            before = 1.0
            _requeue_targets(
                db_path,
                [
                    CleanupTarget(
                        container_video_path=container_path,
                        local_video_path=video,
                        media_outputs=[],
                        cache_outputs=[],
                        pollution_reasons=("prompt_leak",),
                    )
                ],
            )

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT status, source, attempts, added_at FROM ai_candidate_queue WHERE path = ?",
                    (container_path,),
                ).fetchone()
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_job_state").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_stage_events").fetchone()[0], 0)
            finally:
                conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row[:3], ("queued", "selective-ai-cleanup", 0))
            self.assertGreater(row[3], before)

    def test_clean_translation_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            path.write_text(
                "[Events]\n"
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,正常翻譯。\n",
                encoding="utf-8",
            )

            self.assertIsNone(_ass_translation_pollution_reason(path))

    def test_required_prompt_leak_is_found_after_other_pollution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "Episode.AI繁日雙語.zh-TW.ass"
            path.write_text(
                "[Events]\n"
                f"Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{'西耶娜' * 30}\n"
                "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,"
                "請逐行翻譯下列字幕。每一行都必須輸出，格式必須是：原編號<TAB>中文字幕。\n",
                encoding="utf-8",
            )

            self.assertEqual(
                _ass_translation_pollution_reason(path),
                "runaway_repetition",
            )
            self.assertEqual(
                _ass_translation_pollution_reason(path, required_reason="prompt_leak"),
                "prompt_leak",
            )

    def test_asr_prompt_echo_cleanup_collects_japanese_and_translated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime_root = root / "anime"
            work_path = root / "work"
            video = anime_root / "Show" / "Season 1" / "Show - S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_text("", encoding="utf-8")
            container_path = "/anime/Show/Season 1/Show - S01E01.mkv"
            japanese = video.with_name(f"{video.stem}.AI日本語.ja.ass")
            japanese.write_text(
                "[Events]\n"
                "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,日本ニメ\n"
                "Dialogue: 0,0:00:02.10,0:00:03.00,Default,,0,0,0,,ング\n"
                "Dialogue: 0,0:00:03.10,0:00:04.00,Default,,0,0,0,,挿入\n"
                "Dialogue: 0,0:00:04.10,0:00:05.00,Default,,0,0,0,,歌。\n",
                encoding="utf-8",
            )
            translated = video.with_name(f"{video.stem}.AI繁日雙語.zh-TW.ass")
            translated.write_text("[Events]\n", encoding="utf-8")
            cache_root = work_path / "ai_srt_cache"
            cache_root.mkdir(parents=True)
            cache_base = _ai_srt_cache_base_from_container_path(container_path, cache_root)
            cache = cache_root / f"{cache_base.name}.AI日本語.ja.srt"
            cache.write_text("prompt echo", encoding="utf-8")

            db_path = work_path / "scanner_state.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY, status TEXT NOT NULL)")
                conn.execute(
                    "INSERT INTO ai_candidate_queue(path, status) VALUES (?, 'done')",
                    (container_path,),
                )
                conn.commit()
            finally:
                conn.close()

            targets = _select_asr_prompt_echo_targets(
                db_path,
                work_path,
                anime_root,
                "/anime",
                progress_interval_seconds=0,
            )

            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].pollution_reasons, ("asr_prompt_echo",))
            self.assertIn(japanese, targets[0].media_outputs)
            self.assertIn(translated, targets[0].media_outputs)
            self.assertIn(cache, targets[0].cache_outputs)

    def test_asr_artifact_cleanup_limit_prioritizes_newest_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime_root = root / "anime"
            work_path = root / "work"
            work_path.mkdir()
            rows: list[str] = []
            outputs: list[Path] = []
            for episode, timestamp in ((1, 1000), (2, 2000)):
                video = anime_root / "Show" / "Season 1" / f"Show - S01E{episode:02d}.mkv"
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_text("", encoding="utf-8")
                output = _japanese_ai_ass_candidates(video)[0]
                output.write_text(
                    "[Events]\n"
                    "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                    "\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046\u3054\u3056\u3044\u307e\u3057\u305f\n",
                    encoding="utf-8",
                )
                os.utime(output, ns=(timestamp, timestamp))
                rows.append(f"/anime/Show/Season 1/{video.name}")
                outputs.append(output)

            db_path = work_path / "scanner_state.sqlite3"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE ai_candidate_queue(path TEXT PRIMARY KEY, status TEXT NOT NULL)")
                conn.executemany(
                    "INSERT INTO ai_candidate_queue(path, status) VALUES (?, 'done')",
                    [(path,) for path in rows],
                )
                conn.commit()
            finally:
                conn.close()

            targets = _select_asr_prompt_echo_targets(
                db_path,
                work_path,
                anime_root,
                "/anime",
                progress_interval_seconds=0,
                max_targets=1,
            )

            self.assertEqual([target.container_video_path for target in targets], [rows[1]])
            self.assertIn(outputs[1], targets[0].media_outputs)


if __name__ == "__main__":
    unittest.main()
