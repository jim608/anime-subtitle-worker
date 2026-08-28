from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from repair_ai_outputs import RepairSummary, _canonical_source_language_tag, _repair_ass_name, repair_tree


CONFIG = SimpleNamespace(
    ai_japanese_ass_suffix=".AI日本語.ja.ass",
    ai_simplified_chinese_ass_suffix=".AI简日双语.zh-CN.ass",
    ai_traditional_chinese_ass_suffix=".AI繁日雙語.zh-TW.ass",
    ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
    whisper_hallucination_phrases=[],
    work_path=Path("work"),
)


class RepairAiOutputsTest(unittest.TestCase):
    def test_dry_run_reports_wrong_ai_name_and_hallucination_line_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wrong = root / "Anime S01E01.AI繁日雙語.日本語.ja.ass"
            wrong.write_text(_ass_content(), encoding="utf-8-sig")

            summary = repair_tree(root, CONFIG, apply=False)

            self.assertEqual(summary.scanned_ass, 1)
            self.assertEqual(summary.renamed, 1)
            self.assertEqual(summary.rewritten, 1)
            self.assertEqual(summary.hallucination_lines_removed, 1)
            self.assertTrue(wrong.exists())
            self.assertFalse((root / "Anime S01E01.AI繁日雙語.zh-TW.ass").exists())
            self.assertIn("字幕製作人 初音未來", wrong.read_text(encoding="utf-8-sig"))

    def test_apply_repairs_wrong_ai_name_and_removes_hallucination_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wrong = root / "Anime S01E01.AI繁日雙語.日本語.ja.ass"
            target = root / "Anime S01E01.AI繁日雙語.zh-TW.ass"
            wrong.write_text(_ass_content(), encoding="utf-8-sig")

            summary = repair_tree(root, CONFIG, apply=True)

            self.assertEqual(summary.renamed, 1)
            self.assertEqual(summary.rewritten, 1)
            self.assertEqual(summary.hallucination_lines_removed, 1)
            self.assertFalse(wrong.exists())
            self.assertTrue(target.exists())
            repaired = target.read_text(encoding="utf-8-sig")
            self.assertNotIn("字幕製作人 初音未來", repaired)
            self.assertIn("正常台詞", repaired)

    def test_keeps_real_dialogue_with_hatsune_miku_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "Anime S01E01.AI日本語.ja.ass"
            path.write_text(
                "\n".join(
                    [
                        "[Events]",
                        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,初音ミクの曲が好きです",
                    ]
                )
                + "\n",
                encoding="utf-8-sig",
            )

            summary = repair_tree(root, CONFIG, apply=True)

            self.assertEqual(summary.rewritten, 0)
            self.assertIn("初音ミクの曲が好きです", path.read_text(encoding="utf-8-sig"))

    def test_does_not_rewrite_non_ai_ass_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "Anime S01E01.English.eng.ass"
            path.write_text(_ass_content(), encoding="utf-8-sig")

            summary = repair_tree(root, CONFIG, apply=True)

            self.assertEqual(summary.rewritten, 0)
            self.assertIn("字幕製作人 初音未來", path.read_text(encoding="utf-8-sig"))

    def test_normalizes_legacy_source_transcript_language_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "Anime S01E01.AI原語言.English.eng.ass"
            target = root / "Anime S01E01.AIEnglish.en.ass"
            legacy.write_text("[Events]\n", encoding="utf-8-sig")

            summary = repair_tree(root, CONFIG, apply=True)

            self.assertEqual(summary.renamed, 1)
            self.assertFalse(legacy.exists())
            self.assertTrue(target.exists())

    def test_streams_actions_while_repairing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "Anime S01E01.AIEnglish.eng.ass"
            target = root / "Anime S01E01.AIEnglish.en.ass"
            legacy.write_text("[Events]\n", encoding="utf-8-sig")
            streamed: list[str] = []

            summary = repair_tree(root, CONFIG, apply=True, action_callback=lambda action: streamed.append(action.action))

            self.assertEqual(summary.renamed, 1)
            self.assertEqual(streamed, ["rename"])
            self.assertFalse(legacy.exists())
            self.assertTrue(target.exists())

    def test_removes_duplicate_legacy_source_transcript_when_canonical_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "Anime S01E01.AI原語言.English.eng.ass"
            target = root / "Anime S01E01.AIEnglish.en.ass"
            content = "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,hello\n"
            legacy.write_text(content, encoding="utf-8-sig")
            target.write_text(content, encoding="utf-8-sig")

            summary = repair_tree(root, CONFIG, apply=True)

            self.assertEqual(summary.duplicate_removed, 1)
            self.assertFalse(legacy.exists())
            self.assertTrue(target.exists())

    def test_archive_source_conflict_moves_legacy_file_to_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backup"
            legacy = root / "Season 1" / "Anime S01E01.AIEnglish.eng.ass"
            target = root / "Season 1" / "Anime S01E01.AIEnglish.en.ass"
            legacy.parent.mkdir()
            legacy.write_text("[Events]\nold\n", encoding="utf-8-sig")
            target.write_text("[Events]\nnew\n", encoding="utf-8-sig")

            summary = repair_tree(
                root,
                CONFIG,
                apply=True,
                conflict_policy="archive-source",
                conflict_backup_root=backup_root,
            )

            archived = backup_root / "Season 1" / legacy.name
            self.assertEqual(summary.rename_conflicts, 1)
            self.assertEqual(summary.conflict_archived, 1)
            self.assertFalse(legacy.exists())
            self.assertTrue(target.exists())
            self.assertTrue(archived.exists())
            self.assertIn("old", archived.read_text(encoding="utf-8-sig"))

    def test_delete_source_conflict_is_safely_archived_for_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_root = root / "backup"
            legacy = root / "Anime S01E01.AIEnglish.eng.ass"
            target = root / "Anime S01E01.AIEnglish.en.ass"
            legacy.write_text("[Events]\nold\n", encoding="utf-8-sig")
            target.write_text("[Events]\nnew\n", encoding="utf-8-sig")

            summary = repair_tree(
                root,
                CONFIG,
                apply=True,
                conflict_policy="delete-source",
                conflict_backup_root=backup_root,
            )

            self.assertEqual(summary.rename_conflicts, 1)
            self.assertEqual(summary.conflict_archived, 1)
            self.assertEqual(summary.conflict_source_deleted, 0)
            self.assertFalse(legacy.exists())
            self.assertTrue(target.exists())
            self.assertEqual((backup_root / legacy.name).read_text(encoding="utf-8-sig"), "[Events]\nold\n")

    def test_skips_absurd_source_language_tag(self) -> None:
        self.assertEqual(_canonical_source_language_tag("NN-ASS-" * 40), "")

    def test_target_exists_os_error_is_reported_without_crashing(self) -> None:
        path = Path("Anime S01E01.AIEnglish.eng.ass")
        summary = RepairSummary()

        with patch.object(Path, "exists", side_effect=OSError(36, "File name too long")):
            result = _repair_ass_name(path, CONFIG, apply=False, summary=summary)

        self.assertEqual(result, path)
        self.assertEqual([action.action for action in summary.actions], ["skip_target_error"])


def _ass_content() -> str:
    return (
        "[Events]\n"
        "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,字幕製作人 初音未來\\N{\\fs50\\c&HE6E6E6&\\alpha&H18&\\bord1.6\\shad0}字幕作成者 初音ミク\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,正常台詞\\N{\\fs50\\c&HE6E6E6&\\alpha&H18&\\bord1.6\\shad0}普通の台詞\n"
    )


if __name__ == "__main__":
    unittest.main()
