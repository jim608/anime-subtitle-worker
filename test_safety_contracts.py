from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from control_state import (
    claim_next_command,
    dismiss_review_item,
    enqueue_command,
    get_review_item,
    initialize_control_state,
    list_review_items,
    resolve_ai_quality_reviews_for_target_if_idle,
    resolve_sibling_target_reviews,
    upsert_review_item,
)
from mikan_worker import (
    _load_pending,
    _register_sqlite_authoritative_pending,
    _save_pending,
)
from migration_preflight import rehearse_database_backups
from output_manifest import (
    begin_output_publication,
    delivery_identity,
    finish_output_publication,
    output_manifest_path,
    validate_output_manifest,
    write_output_manifest,
)
from subtitle_paths import has_ai_finished_subtitle, paths_for_video, source_transcript_paths_for_video
from repair_ai_outputs import RepairSummary, _repair_ass_name
from resource_scheduler import decide_extraction_resources
from safe_files import VerifiedMoveError, _short_temporary_sibling, verified_copy_replace, verified_move
from sqlite_safety import quick_check_path


class SafeFileContractTests(unittest.TestCase):
    def test_cross_device_move_copies_verifies_then_removes_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.ass"
            destination = root / "archive" / "source.ass"
            source.write_bytes(b"subtitle-content")
            original_replace = Path.replace

            def replace_with_exdev(path: Path, target: Path) -> Path:
                if path == source:
                    raise OSError(errno.EXDEV, "cross-device link")
                return original_replace(path, target)

            with patch.object(Path, "replace", autospec=True, side_effect=replace_with_exdev):
                result = verified_move(source, destination)

            self.assertEqual(result, destination)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"subtitle-content")

    def test_cross_device_hash_mismatch_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.ass"
            destination = root / "archive" / "source.ass"
            source.write_bytes(b"subtitle-content")
            original_replace = Path.replace

            def replace_with_exdev(path: Path, target: Path) -> Path:
                if path == source:
                    raise OSError(errno.EXDEV, "cross-device link")
                return original_replace(path, target)

            with (
                patch.object(Path, "replace", autospec=True, side_effect=replace_with_exdev),
                patch("safe_files.sha256_file", side_effect=["source-hash", "different-hash"]),
            ):
                with self.assertRaises(VerifiedMoveError):
                    verified_move(source, destination)

            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob("*.copying")), [])

    def test_verified_copy_replace_preserves_source_and_replaces_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.ass"
            destination = root / "media" / "episode.ass"
            source.write_bytes(b"new subtitle")
            destination.parent.mkdir()
            destination.write_bytes(b"old subtitle")

            verified_copy_replace(source, destination)

            self.assertEqual(source.read_bytes(), b"new subtitle")
            self.assertEqual(destination.read_bytes(), b"new subtitle")

    def test_temporary_name_does_not_append_long_destination_basename(self) -> None:
        destination = Path("x" * 240 + ".ass")
        temporary = _short_temporary_sibling(destination, kind="copying", suffix=".copying")

        self.assertLess(len(temporary.name.encode("utf-8")), 100)
        self.assertTrue(temporary.name.endswith(".copying"))

    def test_output_manifest_detects_modified_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, ai_output_manifest_path="manifests")
            video = root / "episode.mkv"
            output = root / "episode.AIEnglish.en.ass"
            video.write_bytes(b"video")
            output.write_bytes(b"valid")

            write_output_manifest(video, config, [output], provenance={"prompt_version": "v1"})
            self.assertTrue(validate_output_manifest(video, config, verify_hashes=True))
            output.write_bytes(b"tampered")
            self.assertFalse(validate_output_manifest(video, config, verify_hashes=True))

    def test_acceptance_manifest_is_run_bound_only_for_the_enabled_lane(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, ai_output_manifest_path="manifests")
            video = root / "episode.mkv"
            output = root / "episode.AI.ass"
            video.write_bytes(b"video")
            output.write_bytes(b"subtitle")
            run_id = "accrun_" + "3" * 48

            with patch("output_manifest.acceptance_run_id_for_video", return_value=run_id):
                manifest_path = write_output_manifest(video, config, [output])
                self.assertTrue(validate_output_manifest(video, config))
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["acceptance_run_id"], run_id)

            payload.pop("acceptance_run_id")
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("output_manifest.acceptance_run_id_for_video", return_value=run_id):
                self.assertFalse(validate_output_manifest(video, config))

            other_video = root / "production.mkv"
            other_output = root / "production.AI.ass"
            other_video.write_bytes(b"video")
            other_output.write_bytes(b"subtitle")
            with patch("output_manifest.acceptance_run_id_for_video", return_value=""):
                other_manifest = write_output_manifest(other_video, config, [other_output])
            self.assertNotIn(
                "acceptance_run_id",
                json.loads(other_manifest.read_text(encoding="utf-8")),
            )

    def test_manifest_v2_is_strict_delivery_evidence_for_one_media_policy_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                ai_output_manifest_path="manifests",
                whisper_model="model-a",
                translator_model="translator-a",
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            )
            video = root / "episode.mkv"
            outputs = [
                root / "episode.AI.ja.ass",
                root / "episode.AI.zh-CN.ass",
                root / "episode.AI.zh-TW.ass",
            ]
            video.write_bytes(b"video")
            for output in outputs:
                output.write_bytes(b"valid")
            obligation_id = delivery_identity(video, config)["obligation_id"]
            policy_revision = delivery_identity(video, config)["policy_revision"]

            write_output_manifest(
                video,
                config,
                outputs,
                obligation_id=obligation_id,
                publication_kind="translated_trilingual",
                output_languages=("ja", "zh-CN", "zh-TW"),
            )

            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    require_delivery_evidence=True,
                    expected_obligation_id=obligation_id,
                    expected_policy_revision=policy_revision,
                )
            )
            changed_policy = SimpleNamespace(
                work_path=root,
                ai_output_manifest_path="manifests",
                whisper_model="model-b",
                translator_model="translator-a",
            )
            self.assertFalse(
                validate_output_manifest(
                    video,
                    changed_policy,
                    require_delivery_evidence=True,
                    expected_obligation_id=obligation_id,
                    expected_policy_revision=policy_revision,
                )
            )

    def test_strict_delivery_evidence_rejects_active_publication_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, ai_output_manifest_path="manifests")
            video = root / "episode.mkv"
            output = root / "episode.AIEnglish.en.ass"
            video.write_bytes(b"video")
            output.write_bytes(b"valid")
            identity = delivery_identity(video, config)
            write_output_manifest(
                video,
                config,
                [output],
                publication_kind="source_language",
                output_languages=("en",),
            )

            begin_output_publication(video, config)
            self.assertFalse(
                validate_output_manifest(
                    video,
                    config,
                    require_delivery_evidence=True,
                    expected_obligation_id=identity["obligation_id"],
                    expected_policy_revision=identity["policy_revision"],
                )
            )
            finish_output_publication(video, config)
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    require_delivery_evidence=True,
                    expected_obligation_id=identity["obligation_id"],
                    expected_policy_revision=identity["policy_revision"],
                )
            )

    def test_strict_manifest_rejects_missing_or_unsafe_publication_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, ai_output_manifest_path="manifests")
            video = root / "episode.mkv"
            output = root / "episode.AIEnglish.en.ass"
            video.write_bytes(b"video")
            output.write_bytes(b"valid")
            identity = delivery_identity(video, config)

            write_output_manifest(video, config, [output])
            self.assertFalse(
                validate_output_manifest(
                    video,
                    config,
                    require_delivery_evidence=True,
                    expected_obligation_id=identity["obligation_id"],
                    expected_policy_revision=identity["policy_revision"],
                )
            )
            for language in ("", "ja", "jpn", "und", "unknown"):
                with self.subTest(language=language), self.assertRaises(ValueError):
                    write_output_manifest(
                        video,
                        config,
                        [output],
                        publication_kind="source_language",
                        output_languages=(language,),
                    )
            with self.assertRaises(ValueError):
                write_output_manifest(
                    video,
                    config,
                    [output],
                    publication_kind="arbitrary_success",
                    output_languages=("en",),
                )

    def test_legacy_manifest_is_compatible_but_never_counts_as_strict_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, ai_output_manifest_path="manifests")
            video = root / "episode.mkv"
            output = root / "episode.AI.ja.ass"
            video.write_bytes(b"video")
            output.write_bytes(b"valid")
            stat = output.stat()
            path = output_manifest_path(video, config)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "video": str(video),
                        "completed_at": 1,
                        "outputs": [
                            {
                                "path": str(output),
                                "size": stat.st_size,
                                "mtime_ns": stat.st_mtime_ns,
                                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                            }
                        ],
                        "provenance": {},
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(validate_output_manifest(video, config, verify_hashes=True))
            self.assertFalse(
                validate_output_manifest(video, config, require_delivery_evidence=True)
            )

    def test_output_manifest_detects_same_size_mtime_change_without_rehashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, ai_output_manifest_path="manifests")
            video = root / "episode.mkv"
            output = root / "episode.AI.ja.ass"
            video.write_bytes(b"video")
            output.write_bytes(b"first")
            write_output_manifest(video, config, [output])
            original = output.stat()

            output.write_bytes(b"other")
            os.utime(
                output,
                ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000),
            )

            self.assertEqual(output.stat().st_size, original.st_size)
            self.assertNotEqual(output.stat().st_mtime_ns, original.st_mtime_ns)
            self.assertFalse(validate_output_manifest(video, config))

    def test_ai_completion_rejects_manifest_missing_any_required_ass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                ai_output_manifest_path="manifests",
                ai_srt_cache_path="ai_srt_cache",
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
                ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
                export_ai_ass=True,
                ass_style_versioning_enabled=False,
            )
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            for output in (paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass):
                output.write_text("valid", encoding="utf-8")

            write_output_manifest(video, config, [paths.ai_zh_tw_ass])

            self.assertFalse(has_ai_finished_subtitle(video, config))

    def test_legacy_complete_ai_output_without_manifest_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                ai_output_manifest_path="manifests",
                ai_srt_cache_path="ai_srt_cache",
                ai_japanese_ass_suffix=".AI.ja.ass",
                ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
                ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
                export_ai_ass=True,
                ass_style_versioning_enabled=False,
            )
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            paths.ai_zh_tw_ass.write_text(
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
                "這裡會選擇開啟網路連線並顯示資訊\n",
                encoding="utf-8",
            )

            self.assertTrue(has_ai_finished_subtitle(video, config))

    def test_canonical_japanese_ass_is_not_misclassified_as_source_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                ai_output_manifest_path="manifests",
                ai_srt_cache_path="ai_srt_cache",
                ai_japanese_ass_suffix=".AIJapanese.ja.ass",
                ai_simplified_chinese_ass_suffix=".AISimplified.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AITraditional.zh-TW.ass",
                ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
                export_ai_ass=True,
                ass_style_versioning_enabled=False,
            )
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            paths = paths_for_video(video, config)
            paths.ai_ja_ass.write_text("japanese only", encoding="utf-8")

            self.assertFalse(has_ai_finished_subtitle(video, config))

    def test_legacy_japanese_ass_is_not_misclassified_as_source_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                ai_output_manifest_path="manifests",
                ai_srt_cache_path="ai_srt_cache",
                ai_japanese_ass_suffix=".AIJapanese.ja.ass",
                ai_simplified_chinese_ass_suffix=".AISimplified.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AITraditional.zh-TW.ass",
                ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
                export_ai_ass=True,
                ass_style_versioning_enabled=False,
            )
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            (root / "episode.AILegacy.ja.ass").write_text("legacy Japanese only", encoding="utf-8")

            self.assertFalse(has_ai_finished_subtitle(video, config))

    def test_noncanonical_source_language_ass_is_not_a_finished_zh_tw_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                ai_output_manifest_path="manifests",
                ai_srt_cache_path="ai_srt_cache",
                ai_japanese_ass_suffix=".AIJapanese.ja.ass",
                ai_simplified_chinese_ass_suffix=".AISimplified.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AITraditional.zh-TW.ass",
                ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
                export_ai_ass=True,
                ass_style_versioning_enabled=False,
            )
            video = root / "episode.mkv"
            video.write_bytes(b"video")
            source = source_transcript_paths_for_video(video, config, "en")
            source.ass.write_text("english source transcript", encoding="utf-8")

            self.assertFalse(has_ai_finished_subtitle(video, config))


class ControlStateContractTests(unittest.TestCase):
    def test_verified_publication_resolves_only_stale_ai_quality_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            target = str((root / "Anime S01E01.mkv").resolve())
            subtitle_review = upsert_review_item(
                config,
                kind="subtitle_quality",
                target_key=target,
                summary="translation omitted a line",
            )
            asr_review = upsert_review_item(
                config,
                kind="asr_quality",
                target_key=target,
                summary="ASR range was rejected",
            )
            ambiguity_review = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key=target,
                summary="choose a target",
            )

            resolved = resolve_ai_quality_reviews_for_target_if_idle(
                config,
                target,
                {
                    "source": "worker",
                    "reason": "quality_gate_and_publication_succeeded",
                },
            )

            self.assertEqual(set(resolved), {subtitle_review, asr_review})
            for review_id in (subtitle_review, asr_review):
                review = get_review_item(config, review_id)
                self.assertEqual(review["status"], "resolved")
                self.assertEqual(review["resolution"]["source"], "worker")
                self.assertEqual(
                    review["resolution"]["reason"],
                    "quality_gate_and_publication_succeeded",
                )
            self.assertEqual(get_review_item(config, ambiguity_review)["status"], "open")

    def test_active_review_command_blocks_automatic_quality_review_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            target = str((root / "Anime S01E01.mkv").resolve())
            review_id = upsert_review_item(
                config,
                kind="subtitle_quality",
                target_key=target,
                summary="translation omitted a line",
            )
            enqueue_command(
                config,
                action="review.resolve_ai",
                target=target,
                parameters={"review_id": review_id},
                idempotency_key="quality-review-in-flight",
            )

            self.assertEqual(
                resolve_ai_quality_reviews_for_target_if_idle(config, target, {}),
                [],
            )
            command = claim_next_command(config, worker_id="test-worker")
            self.assertIsNotNone(command)
            self.assertEqual(
                resolve_ai_quality_reviews_for_target_if_idle(config, target, {}),
                [],
            )
            self.assertEqual(get_review_item(config, review_id)["status"], "open")

    def test_dismissed_target_source_is_not_reopened_by_later_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            torrent_hash = "d" * 40
            review_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="completed:first",
                summary="choose a season",
                diagnosis={"torrent_hash": torrent_hash},
            )

            self.assertTrue(dismiss_review_item(config, review_id))
            dismissed = get_review_item(config, review_id)
            self.assertEqual(dismissed["status"], "resolved")
            self.assertTrue(dismissed["resolution"]["dismissed"])
            self.assertTrue(dismissed["resolution"]["suppress_reopen"])

            duplicate = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="recovered:same-source",
                summary="same torrent seen again",
                diagnosis={"torrent_hash": torrent_hash.upper()},
            )
            unrelated = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="completed:new-source",
                summary="new torrent needs review",
                diagnosis={"torrent_hash": "e" * 40},
            )

            self.assertEqual(duplicate, review_id)
            self.assertEqual(get_review_item(config, review_id)["status"], "resolved")
            self.assertEqual([item["review_id"] for item in list_review_items(config)], [unrelated])

    def test_quality_review_cannot_be_dismissed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            review_id = upsert_review_item(
                config,
                kind="subtitle_quality",
                target_key="/anime/Example/Episode.mkv",
                summary="translation failed quality checks",
            )

            with self.assertRaisesRegex(ValueError, "only target ambiguity"):
                dismiss_review_item(config, review_id)
            self.assertEqual(get_review_item(config, review_id)["status"], "open")

    def test_same_torrent_ambiguities_share_one_open_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            torrent_hash = "a" * 40
            first = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="completed:one",
                summary="first",
                diagnosis={"torrent_hash": torrent_hash},
                candidates=[{"path": "/anime/One/Season 1/One - S01E01.mkv", "score": 10}],
            )
            sibling = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="recovered:one",
                summary="sibling",
                diagnosis={"torrent_hash": torrent_hash.upper()},
                candidates=[{"path": "/anime/One/Season 2/One - S02E01.mkv", "score": 20}],
            )
            unrelated = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="completed:other",
                summary="other",
                diagnosis={"torrent_hash": "b" * 40},
            )

            self.assertEqual(first, sibling)
            merged = get_review_item(config, first)
            self.assertEqual(merged["canonical_key"], f"torrent:{torrent_hash}")
            self.assertEqual(
                {candidate["path"] for candidate in merged["candidates"]},
                {
                    "/anime/One/Season 1/One - S01E01.mkv",
                    "/anime/One/Season 2/One - S02E01.mkv",
                },
            )

            resolved = resolve_sibling_target_reviews(
                config,
                torrent_hash=torrent_hash,
                exclude_review_id=first,
                resolution={"source_id": "2911", "season": 2},
            )

            self.assertEqual(resolved, [])
            self.assertEqual(get_review_item(config, first)["status"], "open")
            self.assertEqual(get_review_item(config, unrelated)["status"], "open")

    def test_schema_v3_migration_merges_existing_open_torrent_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "control_state.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE control_meta(
                        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
                    );
                    INSERT INTO control_meta(key, value, updated_at)
                    VALUES('schema_version', '2', 1);
                    CREATE TABLE review_items (
                        review_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        target_key TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        severity TEXT NOT NULL DEFAULT 'warning',
                        summary TEXT NOT NULL,
                        diagnosis_json TEXT NOT NULL DEFAULT '{}',
                        candidates_json TEXT NOT NULL DEFAULT '[]',
                        resolution_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        resolved_at REAL NOT NULL DEFAULT 0,
                        UNIQUE(kind, target_key)
                    );
                    """
                )
                torrent_hash = "c" * 40
                rows = [
                    (
                        "review_old_empty",
                        "completed:key",
                        json.dumps({"torrent_hash": torrent_hash}),
                        "[]",
                        1.0,
                    ),
                    (
                        "review_old_rich",
                        "hash:key",
                        json.dumps({"torrent_hash": torrent_hash, "torrent_name": "Show"}),
                        json.dumps([{"path": "/anime/Show/Season 2/Episode.mkv", "score": 50}]),
                        2.0,
                    ),
                ]
                connection.executemany(
                    """
                    INSERT INTO review_items(
                        review_id, kind, target_key, summary, diagnosis_json,
                        candidates_json, created_at, updated_at
                    ) VALUES (?, 'target_ambiguity', ?, 'review', ?, ?, ?, ?)
                    """,
                    [(review_id, target, diagnosis, candidates, updated, updated) for review_id, target, diagnosis, candidates, updated in rows],
                )
                connection.commit()
            finally:
                connection.close()

            config = SimpleNamespace(work_path=root, control_state_path=database)
            initialize_control_state(config)

            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                migrated = connection.execute(
                    """
                    SELECT review_id, canonical_key, status, resolution_json
                    FROM review_items ORDER BY review_id
                    """
                ).fetchall()
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(review_items)").fetchall()
                }
            finally:
                connection.close()
            by_id = {str(row["review_id"]): row for row in migrated}
            self.assertEqual(by_id["review_old_rich"]["status"], "open")
            self.assertEqual(by_id["review_old_empty"]["status"], "resolved")
            self.assertEqual(
                json.loads(by_id["review_old_empty"]["resolution_json"])["duplicate_of"],
                "review_old_rich",
            )
            self.assertEqual(
                by_id["review_old_rich"]["canonical_key"],
                f"torrent:{'c' * 40}",
            )
            self.assertIn("idx_review_items_open_canonical", indexes)

    def test_deployment_hold_claims_only_readonly_health_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            enqueue_command(
                config,
                action="ai.retry",
                target="/anime/show/episode.mkv",
                parameters={},
                idempotency_key="blocked-write",
            )
            health = enqueue_command(
                config,
                action="system.health_probe",
                target="",
                parameters={},
                idempotency_key="health-probe",
            )

            claimed = claim_next_command(
                config,
                worker_id="test-worker",
                allowed_actions={"system.health_probe"},
            )

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.command_id, health["command_id"])
            connection = sqlite3.connect(root / "control_state.sqlite3")
            try:
                blocked_status = connection.execute(
                    "SELECT status FROM control_commands WHERE idempotency_key='blocked-write'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(blocked_status, "queued")

    def test_migration_creates_verified_online_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "control_state.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE control_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO control_meta(key, value, updated_at) VALUES('schema_version', '1', 1)"
                )
                connection.commit()
            finally:
                connection.close()
            config = SimpleNamespace(work_path=root, control_state_path=database)

            initialize_control_state(config)

            backups = list((root / "sqlite_migration_backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertTrue(backups[0].with_suffix(".sqlite3.sha256").is_file())
            quick_check_path(database)
            quick_check_path(backups[0])

    def test_concurrent_idempotent_commands_create_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            barrier = threading.Barrier(12)
            command_ids: list[str] = []
            errors: list[BaseException] = []

            def enqueue() -> None:
                try:
                    barrier.wait(timeout=10)
                    payload = enqueue_command(
                        config,
                        action="ai.retry",
                        target="/anime/show/episode.mkv",
                        parameters={},
                        idempotency_key="same-user-operation",
                    )
                    command_ids.append(str(payload["command_id"]))
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=enqueue) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

            self.assertEqual(errors, [])
            self.assertEqual(len(set(command_ids)), 1)
            connection = sqlite3.connect(root / "control_state.sqlite3")
            try:
                count = connection.execute("SELECT COUNT(*) FROM control_commands").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)

    def test_all_database_migrations_rehearse_on_copies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            databases = root / "databases"
            databases.mkdir()
            for name in (
                "scanner_state.sqlite3",
                "mikan_state.sqlite3",
                "control_state.sqlite3",
                "series_metadata.sqlite3",
            ):
                connection = sqlite3.connect(databases / name)
                try:
                    connection.execute("CREATE TABLE seed(value TEXT)")
                    connection.commit()
                finally:
                    connection.close()

            result = rehearse_database_backups(root)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(
                all(item["status"] == "ok" for item in result["databases"].values())
            )
            self.assertEqual(
                set(result["databases"]),
                {
                    "scanner_state.sqlite3",
                    "mikan_state.sqlite3",
                    "control_state.sqlite3",
                    "series_metadata.sqlite3",
                },
            )


class ResourceSchedulerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = SimpleNamespace(
            mikan_extract_workers=2,
            mikan_extract_workers_during_ai=1,
            storage_io_pressure_enabled=True,
            storage_io_pressure_some_avg10_threshold=35.0,
            storage_io_pressure_full_avg10_threshold=10.0,
        )

    def test_translation_allows_two_extractors(self) -> None:
        decision = decide_extraction_resources(self.config, ai_stage="translation")
        self.assertEqual(decision.extract_workers, 2)
        self.assertFalse(decision.ai_disk_active)

    def test_whisper_stage_limits_extraction_to_one(self) -> None:
        decision = decide_extraction_resources(self.config, ai_stage="transcription")
        self.assertEqual(decision.extract_workers, 1)
        self.assertTrue(decision.ai_disk_active)

    def test_io_pressure_throttles_or_pauses_claims(self) -> None:
        some = decide_extraction_resources(
            self.config,
            ai_stage="translation",
            io_pressure={"some": {"avg10": 40}, "full": {"avg10": 0}},
        )
        full = decide_extraction_resources(
            self.config,
            ai_stage="translation",
            io_pressure={"some": {"avg10": 40}, "full": {"avg10": 12}},
        )
        self.assertEqual(some.extract_workers, 1)
        self.assertEqual(full.extract_workers, 0)
        self.assertTrue(full.pause_existing_extracts)


class MikanStateContractTests(unittest.TestCase):
    def test_sqlite_becomes_authoritative_and_legacy_json_is_readonly_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending_path = root / "mikan_pending.json"
            legacy = {"items": {"123:1": {"key": "123:1", "status": "queued", "title": "legacy"}}}
            pending_path.write_text(json.dumps(legacy), encoding="utf-8")
            updated = {
                "items": {
                    "123:1": {
                        "key": "123:1",
                        "bangumi_id": 123,
                        "episode": 1,
                        "status": "completed_waiting_extract",
                        "title": "authoritative",
                    }
                }
            }
            _register_sqlite_authoritative_pending(pending_path, enabled=True)
            try:
                _save_pending(pending_path, updated)
                loaded = _load_pending(pending_path)
            finally:
                _register_sqlite_authoritative_pending(pending_path, enabled=False)

            self.assertFalse(pending_path.exists())
            self.assertTrue((root / "mikan_pending.legacy-readonly.json").is_file())
            self.assertEqual(loaded["items"]["123:1"]["title"], "authoritative")
            quick_check_path(root / "mikan_state.sqlite3")


class RepairIsolationContractTests(unittest.TestCase):
    def test_overlong_canonical_name_is_quarantined_without_stopping_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "Anime S01E01.AIEnglish.eng.ass"
            source.write_text("[Events]\n", encoding="utf-8")
            config = SimpleNamespace(
                work_path=root / "work",
                ai_japanese_ass_suffix=".AIJapanese.ja.ass",
                ai_simplified_chinese_ass_suffix=".AIChinese.zh-CN.ass",
                ai_traditional_chinese_ass_suffix=".AIChinese.zh-TW.ass",
                ai_source_transcript_ass_suffix_template=".AI{label}.{language}.ass",
            )
            summary = RepairSummary()
            quarantine_root = config.work_path / "repair_ai_quarantine" / "test"
            original_exists = Path.exists

            def exists_with_long_target(path: Path) -> bool:
                if path.name == "Anime S01E01.AIEnglish.en.ass":
                    raise OSError(errno.ENAMETOOLONG, "File name too long")
                return original_exists(path)

            with patch.object(Path, "exists", autospec=True, side_effect=exists_with_long_target):
                result = _repair_ass_name(
                    source,
                    config,
                    apply=True,
                    summary=summary,
                    quarantine_root=quarantine_root,
                    root_path=root,
                )

            self.assertEqual(summary.invalid_name_quarantined, 1)
            self.assertFalse(source.exists())
            self.assertTrue(result.is_file())
            self.assertIn(".invalid-", result.name)


if __name__ == "__main__":
    unittest.main()
