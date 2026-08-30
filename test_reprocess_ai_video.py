from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_failure_markers import ai_failure_marker_path, mark_ai_failure
from completed_delivery import (
    COMPLETED_DELIVERY_CONTRACT,
    COMPLETED_DELIVERY_SCHEMA_VERSION,
    completed_delivery_destination,
    completed_delivery_receipt_path,
)
from output_manifest import delivery_identity, output_manifest_path
from reprocess_ai_video import reprocess_video, restore_reprocess_manifest
from safe_files import sha256_file, verified_move as real_verified_move
from scan_state import ScanStateStore
from subtitle_paths import paths_for_video, source_transcript_paths_for_video
from transcriber import asr_diagnostics_path, asr_transcription_hold_path


class ReprocessAiVideoTest(unittest.TestCase):
    def test_retranscribe_archives_dynamic_source_language_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            source = source_transcript_paths_for_video(video, config, "en")
            source.srt.write_text("english", encoding="utf-8")
            source.ass.write_text("english-ass", encoding="utf-8")
            diagnostic = asr_diagnostics_path(source.srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text('{"status":"accepted"}', encoding="utf-8")
            hold = asr_transcription_hold_path(source.srt, config)
            hold.write_text(
                '{"status":"transcription_commit_pending"}',
                encoding="utf-8",
            )
            gaps = source.srt.with_name(f"{source.srt.stem}.gaps.txt")
            gaps.write_text("gap", encoding="utf-8")

            result = reprocess_video(config, video, mode="retranscribe")

            self.assertEqual(result["moved_outputs"], 5)
            self.assertFalse(source.srt.exists())
            self.assertFalse(source.ass.exists())
            self.assertFalse(diagnostic.exists())
            self.assertFalse(hold.exists())
            self.assertFalse(gaps.exists())

    def test_retranscribe_archives_all_outputs_with_hashes_and_forces_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            outputs = (
                paths.ja_srt,
                paths.zh_cn_srt,
                paths.zh_tw_srt,
                paths.ai_ja_ass,
                paths.ai_zh_cn_ass,
                paths.ai_zh_tw_ass,
            )
            for path in outputs:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"output:{path.name}", encoding="utf-8")
            diagnostic = asr_diagnostics_path(paths.ja_srt, config)
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text('{"status":"accepted"}', encoding="utf-8")
            hold = asr_transcription_hold_path(paths.ja_srt, config)
            hold.write_text(
                '{"status":"transcription_commit_pending"}',
                encoding="utf-8",
            )

            result = reprocess_video(config, video, mode="retranscribe")

            self.assertEqual(result["moved_outputs"], 8)
            self.assertEqual(result["strategy"], "full_transcription_rerun")
            self.assertFalse(result["selective"])
            self.assertFalse(any(path.exists() for path in outputs))
            self.assertFalse(diagnostic.exists())
            self.assertFalse(hold.exists())
            manifest_path = Path(result["manifest"])
            self.assertEqual(result["manifest_sha256"], sha256_file(manifest_path))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["operation"], "reprocess_archive")
            self.assertFalse(manifest["selective"])
            self.assertEqual(len(manifest["entries"]), 8)
            for entry in manifest["entries"]:
                self.assertEqual(entry["state"], "moved")
                self.assertEqual(entry["sha256"], entry["archive_sha256"])
                self.assertEqual(entry["sha256"], sha256_file(entry["archive"]))
            state = ScanStateStore.from_config(config)
            try:
                self.assertTrue(state.is_force_ai_queue_candidate(video))
                self.assertEqual(state.iter_ai_queue_candidates(), [video.resolve()])
            finally:
                state.close()

    def test_retranscribe_archives_owned_completed_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_bytes(b"source-video")
            config = _config(anime, work)
            config.completed_delivery_enabled = True
            config.completed_delivery_path = str(root / "completed")
            config.completed_delivery_manifest_path = str(work / "completed_manifests")
            destination = completed_delivery_destination(video, config)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"old-completed-video")
            receipt = completed_delivery_receipt_path(video, config)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            identity = delivery_identity(video, config)
            manifest = output_manifest_path(video, config)
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text("old-manifest", encoding="utf-8")
            video_stat = video.stat()
            destination_stat = destination.stat()
            receipt.write_text(
                json.dumps(
                    {
                        "schema_version": COMPLETED_DELIVERY_SCHEMA_VERSION,
                        "contract": COMPLETED_DELIVERY_CONTRACT,
                        "state": "committed",
                        "source": {
                            "canonical_path": str(video.resolve()),
                            "media_size": video_stat.st_size,
                            "media_mtime_ns": video_stat.st_mtime_ns,
                            "media_fingerprint": identity["media"]["media_fingerprint"],
                            "sha256": sha256_file(video),
                        },
                        "delivery": {
                            "obligation_id": identity["obligation_id"],
                            "policy_revision": identity["policy_revision"],
                        },
                        "publication_manifest": {
                            "path": str(manifest.resolve()),
                            "sha256": sha256_file(manifest),
                        },
                        "destination": str(destination),
                        "output": {
                            "path": str(destination),
                            "size": destination_stat.st_size,
                            "mtime_ns": destination_stat.st_mtime_ns,
                            "sha256": sha256_file(destination),
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = reprocess_video(config, video, mode="retranscribe")

            archived_sources = {item["source"] for item in result["moved"]}
            self.assertIn(str(destination), archived_sources)
            self.assertIn(str(receipt), archived_sources)
            self.assertFalse(destination.exists())
            self.assertFalse(receipt.exists())

            restored = restore_reprocess_manifest(config, result["manifest"])
            self.assertEqual(restored["restored_outputs"], result["moved_outputs"])
            self.assertEqual(destination.read_bytes(), b"old-completed-video")
            self.assertTrue(receipt.is_file())

            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            destination.write_bytes(b"X" * destination.stat().st_size)
            receipt_payload["output"]["mtime_ns"] = destination.stat().st_mtime_ns
            receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not own"):
                reprocess_video(config, video, mode="retranscribe")
            self.assertTrue(destination.is_file())
            self.assertTrue(receipt.is_file())

    def test_reprocess_refuses_current_terminal_delivery_before_archiving(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_bytes(b"source-video")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("preserve me", encoding="utf-8")
            identity = delivery_identity(video, config)
            state = ScanStateStore.from_config(config)
            try:
                media = identity["media"]
                obligation = state.ensure_ai_delivery_obligation(
                    video,
                    media_size=media["media_size"],
                    media_mtime_ns=media["media_mtime_ns"],
                    policy_revision=identity["policy_revision"],
                    obligation_id=identity["obligation_id"],
                )
                attempt = state.begin_ai_delivery_attempt(obligation["obligation_id"])
                state.finish_ai_delivery_attempt(attempt["attempt_id"], status="succeeded")
                state.mark_ai_delivery_verified(
                    obligation["obligation_id"],
                    manifest_path="/work/manifest.json",
                    manifest_sha256="a" * 64,
                    verification={
                        "publication_semantics_verified": True,
                        "publication_contract": "ai-publication-semantics-v2",
                        "publication_kind": "translated_trilingual",
                        "output_languages": ["ja", "zh-CN", "zh-TW"],
                        "expected_policy_revision": identity["policy_revision"],
                        "manifest_policy_revision": identity["policy_revision"],
                        "policy_revision_matched": True,
                    },
                    evidence_verified=True,
                )
                state.commit()
            finally:
                state.close()

            with self.assertRaisesRegex(RuntimeError, "terminal"):
                reprocess_video(config, video, mode="retranscribe")

            self.assertEqual(paths.ja_srt.read_text(encoding="utf-8"), "preserve me")
            self.assertFalse((work / "manual_ai_reprocess").exists())

            with patch(
                "transcriber.ASR_TRANSCRIPTION_CONTRACT",
                "asr-short-gap-rescue-fail-closed-v-next",
            ):
                result = reprocess_video(config, video, mode="retranscribe")
            self.assertTrue(result["ok"])
            self.assertFalse(paths.ja_srt.exists())

    def test_reprocess_refuses_completed_pair_appearing_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_bytes(b"source-video")
            config = _config(anime, work)
            config.completed_delivery_enabled = True
            config.completed_delivery_path = str(root / "completed")
            config.completed_delivery_manifest_path = str(work / "completed_manifests")
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("preserve me", encoding="utf-8")
            destination = completed_delivery_destination(video, config)
            receipt = completed_delivery_receipt_path(video, config)

            def appear_after_validation(*_args: object, **_kwargs: object):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"unexpected-completed")
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text("{}", encoding="utf-8")
                return set(), {}

            with (
                patch(
                    "reprocess_ai_video._validate_completed_delivery_for_reprocess",
                    side_effect=appear_after_validation,
                ),
                self.assertRaisesRegex(RuntimeError, "changed after ownership validation"),
            ):
                reprocess_video(config, video, mode="retranscribe")

            self.assertEqual(paths.ja_srt.read_text(encoding="utf-8"), "preserve me")
            self.assertEqual(destination.read_bytes(), b"unexpected-completed")
            self.assertTrue(receipt.is_file())
            self.assertFalse((work / "manual_ai_reprocess").exists())

    def test_automatic_review_retranscribe_preserves_consumed_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("rejected transcription", encoding="utf-8")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.mark_ai_queue_running(video)
                state.mark_ai_queue_failed(
                    video,
                    "deterministic ASR quality review",
                    max_attempts=1,
                    error_code="deterministic_asr_quality",
                    retry_strategy="manual_review",
                )
                state.commit()
                before = state.ai_queue_candidate_snapshot(video)
            finally:
                state.close()

            result = reprocess_video(
                config,
                video,
                mode="retranscribe",
                queue_mode="auto_review",
                expected_failure_revision=before["failure_revision"],
                policy_revision="asr-full-retranscribe-v1",
            )

            self.assertEqual(result["queue_mode"], "auto_review")
            self.assertFalse(paths.ja_srt.exists())
            state = ScanStateStore.from_config(config)
            try:
                after = state.ai_queue_candidate_snapshot(video)
                self.assertEqual(after["status"], "queued")
                self.assertEqual(after["attempts"], before["attempts"])
                self.assertEqual(after["failure_revision"], before["failure_revision"])
                self.assertEqual(after["source"], "auto_review_remediation")
            finally:
                state.close()

    def test_retranslate_keeps_japanese_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.parent.mkdir(parents=True, exist_ok=True)
            paths.ja_srt.write_text("japanese", encoding="utf-8")
            paths.ai_zh_tw_ass.write_text("translated", encoding="utf-8")

            result = reprocess_video(config, video, mode="retranslate")

            self.assertEqual(result["strategy"], "translation_only_rerun")
            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.ai_zh_tw_ass.exists())

    def test_automatic_translation_followup_keeps_japanese_cache_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E02.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("verified japanese", encoding="utf-8")
            paths.ai_zh_tw_ass.write_text("omitted translation", encoding="utf-8")
            state = ScanStateStore.from_config(config)
            try:
                state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                state.mark_ai_queue_running(video)
                state.mark_ai_queue_failed(
                    video,
                    "Translation safe-omission remained after bounded same-job recovery: indexes=[4]",
                    max_attempts=1,
                    error_code="asr_quality_review",
                    retry_strategy="manual_review",
                )
                state.commit()
                before = state.ai_queue_candidate_snapshot(video)
            finally:
                state.close()

            result = reprocess_video(
                config,
                video,
                mode="retranslate",
                queue_mode="auto_review",
                expected_failure_revision=before["failure_revision"],
                policy_revision="translation-omission-retranslate-v1",
            )

            self.assertEqual(result["strategy"], "translation_only_rerun")
            self.assertTrue(paths.ja_srt.exists())
            self.assertFalse(paths.ai_zh_tw_ass.exists())
            state = ScanStateStore.from_config(config)
            try:
                after = state.ai_queue_candidate_snapshot(video)
                self.assertEqual(after["status"], "queued")
                self.assertEqual(after["attempts"], before["attempts"])
                self.assertEqual(after["source"], "auto_review_remediation")
            finally:
                state.close()

    def test_mid_archive_failure_rolls_back_every_moved_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("original-ja", encoding="utf-8")
            paths.zh_cn_srt.write_text("original-zh", encoding="utf-8")
            calls = 0

            def fail_second_move(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("archive unavailable")
                return real_verified_move(source, destination)

            with patch("reprocess_ai_video.verified_move", side_effect=fail_second_move):
                with self.assertRaisesRegex(OSError, "archive unavailable"):
                    reprocess_video(config, video, mode="retranscribe")

            self.assertEqual(paths.ja_srt.read_text(encoding="utf-8"), "original-ja")
            self.assertEqual(paths.zh_cn_srt.read_text(encoding="utf-8"), "original-zh")
            manifest_path = next((work / "manual_ai_reprocess").glob("*/manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rolled_back")
            state = ScanStateStore.from_config(config)
            try:
                self.assertFalse(state.is_force_ai_queue_candidate(video))
            finally:
                state.close()

    def test_queue_commit_failure_rolls_back_and_keeps_failure_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("original-ja", encoding="utf-8")
            mark_ai_failure(config, video, "transcription", "failure")

            with patch.object(
                ScanStateStore,
                "commit",
                side_effect=RuntimeError("queue commit failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "queue commit failed"):
                    reprocess_video(config, video, mode="retranscribe")

            self.assertEqual(paths.ja_srt.read_text(encoding="utf-8"), "original-ja")
            self.assertTrue(ai_failure_marker_path(config, video).is_file())
            manifest_path = next((work / "manual_ai_reprocess").glob("*/manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rolled_back")

    def test_restore_archives_current_outputs_then_restores_verified_originals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("original-ja", encoding="utf-8")
            archived = reprocess_video(config, video, mode="retranscribe")
            paths.ja_srt.write_text("current-ja", encoding="utf-8")

            restored = restore_reprocess_manifest(
                config,
                archived["manifest"],
                expected_manifest_sha256=archived["manifest_sha256"],
            )

            self.assertEqual(restored["restored_outputs"], 1)
            self.assertEqual(paths.ja_srt.read_text(encoding="utf-8"), "original-ja")
            restore_manifest = json.loads(
                Path(restored["manifest"]).read_text(encoding="utf-8")
            )
            self.assertEqual(restore_manifest["status"], "complete")
            self.assertEqual(len(restore_manifest["backed_up"]), 1)
            current_backup = Path(restore_manifest["backed_up"][0]["archive"])
            self.assertEqual(current_backup.read_text(encoding="utf-8"), "current-ja")
            self.assertEqual(
                restore_manifest["backed_up"][0]["sha256"],
                sha256_file(current_backup),
            )

    def test_restore_tampered_archive_is_zero_mutation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("original-ja", encoding="utf-8")
            archived = reprocess_video(config, video, mode="retranscribe")
            manifest = json.loads(Path(archived["manifest"]).read_text(encoding="utf-8"))
            Path(manifest["entries"][0]["archive"]).write_text(
                "tampered",
                encoding="utf-8",
            )
            paths.ja_srt.write_text("current-ja", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                restore_reprocess_manifest(config, archived["manifest"])

            self.assertEqual(paths.ja_srt.read_text(encoding="utf-8"), "current-ja")
            self.assertFalse((work / "manual_ai_reprocess_restore").exists())

    def test_restore_mid_failure_rolls_back_original_archive_and_current_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            anime = root / "anime"
            work = root / "work"
            anime.mkdir()
            work.mkdir()
            video = anime / "Anime S01E01.mkv"
            video.write_text("video", encoding="utf-8")
            config = _config(anime, work)
            paths = paths_for_video(video, config)
            paths.ja_srt.write_text("original-ja", encoding="utf-8")
            paths.zh_cn_srt.write_text("original-zh", encoding="utf-8")
            archived = reprocess_video(config, video, mode="retranscribe")
            source_manifest = json.loads(
                Path(archived["manifest"]).read_text(encoding="utf-8")
            )
            paths.ja_srt.write_text("current-ja", encoding="utf-8")
            calls = 0

            def fail_third_move(source, destination):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("restore unavailable")
                return real_verified_move(source, destination)

            with patch("reprocess_ai_video.verified_move", side_effect=fail_third_move):
                with self.assertRaisesRegex(OSError, "restore unavailable"):
                    restore_reprocess_manifest(config, archived["manifest"])

            self.assertEqual(paths.ja_srt.read_text(encoding="utf-8"), "current-ja")
            self.assertFalse(paths.zh_cn_srt.exists())
            for entry in source_manifest["entries"]:
                archive = Path(entry["archive"])
                self.assertTrue(archive.is_file())
                self.assertEqual(entry["sha256"], sha256_file(archive))
            restore_manifest_path = next(
                (work / "manual_ai_reprocess_restore").glob("*/manifest.json")
            )
            restore_manifest = json.loads(
                restore_manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(restore_manifest["status"], "rolled_back")


def _config(anime: Path, work: Path) -> SimpleNamespace:
    (work / "ai_srt_cache").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        input_path=anime,
        work_path=work,
        video_extensions=[".mkv"],
        scanner_state_path=str(work / "scanner_state.sqlite3"),
        ai_japanese_ass_suffix=".AI.ja.ass",
        ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
        ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
    )


if __name__ == "__main__":
    unittest.main()
