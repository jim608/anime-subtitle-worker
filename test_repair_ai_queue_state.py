from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_failure_markers import mark_ai_failure
import repair_ai_queue_state as repair_module
from repair_ai_queue_state import repair_queue_state
from scan_state import ScanStateStore
from subtitle_paths import paths_for_video


VALID_ZH_TW_ASS = """[Script Info]
ScriptType: v4.00+
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,這是一段繁體中文字幕，測試佇列修復。
"""


class RepairAiQueueStateTest(unittest.TestCase):
    def test_removes_standalone_op_ed_from_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            extras = root / "Show" / "Extras"
            extras.mkdir(parents=True)
            video = extras / "ED01.mkv"
            video.write_bytes(b"video")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.removed_standalone_theme, 1)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()
                )
            finally:
                conn.close()

    def test_repairs_queued_item_with_existing_ai_subtitle_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E01.mkv"
            subtitle = root / "Anime S01E01.AI繁日雙語.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            subtitle.write_text(VALID_ZH_TW_ASS, encoding="utf-8")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.repaired_done, 1)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertEqual(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()[0],
                    "done",
                )
                self.assertEqual(
                    conn.execute("SELECT status FROM ai_job_state WHERE path = ?", (str(video),)).fetchone()[0],
                    "ok",
                )
            finally:
                conn.close()

    def test_named_but_unusable_ai_subtitle_stays_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E01-invalid.mkv"
            video.write_text("", encoding="utf-8")
            paths_for_video(video, config).ai_zh_tw_ass.write_text(
                "[Script Info]\n",
                encoding="utf-8",
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.repaired_done, 0)
            self.assertEqual(summary.unchanged, 1)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM ai_candidate_queue WHERE path = ?",
                        (str(video),),
                    ).fetchone()[0],
                    "queued",
                )
            finally:
                conn.close()

    def test_repairs_queued_language_skip_to_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E01.mkv"
            video.write_text("", encoding="utf-8")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.update_ai_job_stage(
                    video,
                    "language_uncertain",
                    "skipped",
                    "Skipped source language gate: reason=language_uncertain language=en probability=0.37",
                )
                state.commit()
            finally:
                state.close()

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.repaired_skipped, 1)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertEqual(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()[0],
                    "skipped",
                )
                row = conn.execute("SELECT stage, status FROM ai_job_state WHERE path = ?", (str(video),)).fetchone()
                self.assertEqual(row, ("language_skip", "skipped"))
            finally:
                conn.close()

    def test_removes_queued_item_during_ai_failure_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E02.mkv"
            video.write_text("", encoding="utf-8")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()
            mark_ai_failure(config, video, "translation", "bad output")

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.removed_failure_cooldown, 1)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()
                )
            finally:
                conn.close()

    def test_removes_queued_item_when_non_ai_subtitle_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E03.mkv"
            subtitle = root / "Anime S01E03.official.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            subtitle.write_text(VALID_ZH_TW_ASS, encoding="utf-8")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.removed_existing_subtitle, 1)
            self.assertEqual(summary.repaired_done, 0)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()
                )
            finally:
                conn.close()

    def test_retries_when_database_is_temporarily_locked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E04.mkv"
            subtitle = root / "Anime S01E04.official.zh-TW.ass"
            video.write_text("", encoding="utf-8")
            subtitle.write_text(VALID_ZH_TW_ASS, encoding="utf-8")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            original_apply = repair_module._apply_action
            attempts = 0

            def flaky_apply(
                conn: sqlite3.Connection,
                action: repair_module.RepairAction,
                *,
                config: object,
            ) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                original_apply(conn, action, config=config)

            with patch.object(repair_module, "_apply_action", side_effect=flaky_apply):
                summary = repair_queue_state(config, apply=True, busy_timeout_seconds=2)

            self.assertEqual(attempts, 2)
            self.assertEqual(summary.removed_existing_subtitle, 1)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()
                )
            finally:
                conn.close()

    def test_removes_missing_video_from_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E05.mkv"
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, 123)
                state.commit()
            finally:
                state.close()

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.removed_missing_video, 1)
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()
                )
            finally:
                conn.close()

    def test_normalizes_local_chinese_sidecar_and_removes_from_ai_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = _config(root)
            video = root / "Anime S01E06.mkv"
            subtitle = root / "Anime S01E06.somegroup.chs.ass"
            video.write_text("", encoding="utf-8")
            subtitle.write_text(
                "\n".join(
                    [
                        "[Script Info]",
                        "[Events]",
                        "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,这是一个用于测试的中文字幕内容，确保系统可以识别中文并跳过人工智能。",
                    ]
                ),
                encoding="utf-8",
            )
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.commit()
            finally:
                state.close()

            summary = repair_queue_state(config, apply=True)

            self.assertEqual(summary.removed_existing_subtitle, 1)
            self.assertEqual(summary.normalized_existing_subtitle, 1)
            self.assertTrue((root / "Anime S01E06.zh.ass").exists())
            conn = sqlite3.connect(root / "work" / "scanner_state.sqlite3")
            try:
                self.assertIsNone(
                    conn.execute("SELECT status FROM ai_candidate_queue WHERE path = ?", (str(video),)).fetchone()
                )
            finally:
                conn.close()


def _config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        input_path=root,
        work_path=root / "work",
        scanner_state_path=root / "work" / "scanner_state.sqlite3",
        auto_ai_failure_cooldown_seconds=86400,
        export_ai_ass=True,
        ass_style_versioning_enabled=False,
        ai_japanese_ass_suffix=".AI日本語.ja.ass",
        ai_simplified_chinese_ass_suffix=".AI简日双语.zh-CN.ass",
        ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
        ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
        finished_subtitle_suffixes=[".official.zh-TW.ass"],
        mikan_remove_ai_after_extract=True,
        require_ai_subtitles=False,
        scanner_skip_standalone_op_ed=True,
    )


if __name__ == "__main__":
    unittest.main()
