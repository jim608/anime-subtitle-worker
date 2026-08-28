from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import tempfile
import unittest

from migrate_quality_sidecars import migrate_quality_sidecars
from subtitle_quality import managed_quality_report_path


class QualitySidecarMigrationTests(unittest.TestCase):
    def test_dry_run_does_not_modify_media_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "anime"
            work = Path(temp_dir) / "work"
            root.mkdir()
            subtitle = root / "Episode.AI繁日雙語.zh-TW.ass"
            subtitle.write_text("[Events]\n", encoding="utf-8")
            legacy = Path(str(subtitle) + ".quality.json")
            legacy.write_text(json.dumps({"status": "watchable"}), encoding="utf-8")

            summary = migrate_quality_sidecars(root, work)

            self.assertEqual(summary.matched_reports, 1)
            self.assertEqual(summary.migrated, 0)
            self.assertTrue(legacy.exists())
            self.assertFalse(managed_quality_report_path(subtitle, work).exists())

    def test_apply_moves_valid_report_without_touching_ass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "anime"
            work = Path(temp_dir) / "work"
            root.mkdir()
            subtitle = root / "Episode.AI繁日雙語.zh-TW.ass"
            subtitle.write_text("[Events]\n", encoding="utf-8")
            legacy = Path(str(subtitle) + ".quality.json")
            legacy.write_text(json.dumps({"status": "watchable", "score": 98}), encoding="utf-8")

            summary = migrate_quality_sidecars(root, work, apply=True)
            managed = managed_quality_report_path(subtitle, work)

            self.assertEqual(summary.migrated, 1)
            self.assertFalse(legacy.exists())
            self.assertTrue(subtitle.exists())
            self.assertEqual(json.loads(managed.read_text(encoding="utf-8"))["score"], 98)

    def test_apply_quarantines_invalid_report_and_ignores_other_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "anime"
            work = Path(temp_dir) / "work"
            root.mkdir()
            invalid = root / "Episode.ass.quality.json"
            invalid.write_text("not-json", encoding="utf-8")
            unrelated = root / "movie.mkv.quality.json"
            unrelated.write_text("{}", encoding="utf-8")

            summary = migrate_quality_sidecars(root, work, apply=True)

            self.assertEqual(summary.quarantined, 1)
            self.assertFalse(invalid.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(len(list((work / "subtitle_quality_reports" / "quarantine").glob("*.json"))), 1)

    def test_apply_removes_only_hash_verified_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "anime"
            work = Path(temp_dir) / "work"
            root.mkdir()
            subtitle = root / "Episode.ass"
            subtitle.write_text("[Events]\n", encoding="utf-8")
            legacy = Path(str(subtitle) + ".quality.json")
            payload = json.dumps({"status": "watchable", "score": 99})
            legacy.write_text(payload, encoding="utf-8")
            managed = managed_quality_report_path(subtitle, work)
            managed.parent.mkdir(parents=True)
            managed.write_text(payload, encoding="utf-8")

            summary = migrate_quality_sidecars(root, work, apply=True)

            self.assertEqual(summary.duplicate_removed, 1)
            self.assertFalse(legacy.exists())
            self.assertEqual(managed.read_text(encoding="utf-8"), payload)

    def test_host_root_can_publish_using_container_media_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "mounted-anime"
            work = Path(temp_dir) / "work"
            subtitle = root / "Series" / "Season 2" / "Episode S02E03.zh-CN.ass"
            subtitle.parent.mkdir(parents=True)
            subtitle.write_text("[Events]\n", encoding="utf-8")
            legacy = Path(str(subtitle) + ".quality.json")
            payload = {"path": "/anime/Series/Season 2/Episode S02E03.zh-CN.ass", "score": 91}
            legacy.write_text(json.dumps(payload), encoding="utf-8")

            summary = migrate_quality_sidecars(
                root,
                work,
                container_anime_root="/anime",
                apply=True,
            )

            logical_path = payload["path"]
            digest = hashlib.sha256(logical_path.encode("utf-8")).hexdigest()[:24]
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(logical_path).name).strip("._-")[:96]
            managed = work / "subtitle_quality_reports" / f"{safe_name}.{digest}.quality.json"
            self.assertEqual(summary.migrated, 1)
            self.assertFalse(legacy.exists())
            self.assertEqual(json.loads(managed.read_text(encoding="utf-8"))["score"], 91)


if __name__ == "__main__":
    unittest.main()
