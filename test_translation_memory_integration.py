from __future__ import annotations

import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from output_manifest import (
    begin_output_publication,
    delivery_identity,
    output_publication_marker_path,
    validate_output_manifest,
    write_output_manifest,
)
from safe_files import sha256_file
from srt_utils import SrtBlock, read_srt, write_srt
from subtitle_paths import paths_for_video
from test_worker import _config, _logger
from translation_memory import MemoryScope
from translation_memory_bridge import (
    TRANSLATION_MEMORY_LINEAGE_CONTRACT,
    read_translation_memory_origin_strict,
    translation_memory_full_plan_digest,
    write_translation_memory_origin,
)
from translation_memory_outbox import load_translation_memory_outbox_intent
from translation_quality import (
    read_translation_quality_hold_strict,
    translation_quality_hold_path,
    write_translation_quality_hold,
)
from translator import SubtitleTranslator
from worker import VideoWorker


def _block(index: int, source: str) -> SrtBlock:
    return SrtBlock(
        index,
        f"00:00:0{index},000 --> 00:00:0{index + 1},000",
        [source],
    )


class TranslationMemoryProductionIntegrationTest(unittest.TestCase):
    def _translator(self, root: Path) -> SubtitleTranslator:
        config = _config(
            root,
            batch_size=1,
            translation_context_enabled=False,
            translation_context_max_blocks=10,
            translation_context_max_chars=1000,
            translation_context_max_output_chars=500,
            translation_glossary={},
            translation_reject_residual_kana=True,
            translation_max_line_chars=320,
            translation_max_line_expansion_ratio=8.0,
            subtitle_quality_hard_max_primary_chars=64,
        )
        translator = object.__new__(SubtitleTranslator)
        translator.config = config
        translator.logger = logging.getLogger("test.translation.memory.integration")
        translator._progress_callback = None
        translator._translator_models = ("primary",)
        translator._translator_model_index = 0
        translator._translator_model = "primary"
        translator._build_translation_context = lambda _blocks, _path: ""
        translator._pending_translation_memory_plan = None
        return translator

    def test_partial_hit_translates_only_miss_then_commits_full_hash_bound_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = [_block(1, "こんにちは"), _block(2, "ありがとう")]
            source_path = root / "episode.ja.srt"
            output = root / "episode.zh-CN.srt"
            write_srt(source_path, source)
            scope = MemoryScope("series-test", "policy-test")
            translator = self._translator(root)
            calls: list[list[int]] = []

            def translate(batch: list[SrtBlock], _batch: str, _context: str) -> list[SrtBlock]:
                calls.append([block.index for block in batch])
                return [SrtBlock(2, source[1].timing, ["謝謝"])]

            translator._translate_batch = translate
            translator.set_translation_memory_plan(
                pretranslated_blocks=[SrtBlock(1, source[0].timing, ["你好"])],
                scope=scope,
                decision_digest="a" * 64,
                lineage_mode="tm_split",
            )

            translator.translate_blocks(source, source_path, output)

            self.assertEqual(calls, [[2]])
            self.assertEqual([block.text[0] for block in read_srt(output)], ["你好", "謝謝"])
            origin = read_translation_memory_origin_strict(root, output)
            self.assertIsNotNone(origin)
            self.assertEqual(origin.cached_indexes, (1,))
            self.assertEqual(origin.target_srt_sha256, sha256_file(output))
            self.assertEqual(origin.source_srt_sha256, sha256_file(source_path))
            hold = read_translation_quality_hold_strict(output)
            self.assertIsNotNone(hold)
            self.assertEqual(hold["srt_sha256"], sha256_file(output))

    def test_all_hit_skips_model_and_still_commits_complete_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = [_block(1, "こんにちは"), _block(2, "ありがとう")]
            source_path = root / "episode.ja.srt"
            output = root / "episode.zh-CN.srt"
            write_srt(source_path, source)
            scope = MemoryScope("series-test", "policy-test")
            translator = self._translator(root)
            translator._translate_batch = Mock(side_effect=AssertionError("model must not run"))
            translator.set_translation_memory_plan(
                pretranslated_blocks=[
                    SrtBlock(1, source[0].timing, ["你好"]),
                    SrtBlock(2, source[1].timing, ["謝謝"]),
                ],
                scope=scope,
                decision_digest="b" * 64,
                lineage_mode="tm_split",
            )

            translator.translate_blocks(source, source_path, output)

            translator._translate_batch.assert_not_called()
            self.assertEqual(len(read_srt(output)), 2)
            origin = read_translation_memory_origin_strict(root, output)
            self.assertEqual(origin.cached_indexes, (1, 2))

    def test_lookup_failure_discards_all_hits_and_uses_full_translation_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(
                root,
                translation_memory_enabled=True,
                translation_memory_auto_apply_enabled=True,
                translation_memory_path=root / "missing.sqlite3",
            )
            worker = VideoWorker(config, _logger())
            translator = Mock()
            source = [_block(1, "こんにちは")]

            worker._configure_translation_memory_plan(
                video,
                translator,
                source,
                series_glossary={},
            )

            call = translator.set_translation_memory_plan.call_args.kwargs
            self.assertEqual(tuple(call["pretranslated_blocks"]), ())
            self.assertEqual(call["lineage_mode"], "lookup_fallback")
            self.assertRegex(call["decision_digest"], r"^[0-9a-f]{64}$")

    def test_worker_records_pre_finish_outbox_with_exact_manifest_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(
                root,
                export_ai_ass=True,
                translation_memory_enabled=True,
                translation_memory_auto_apply_enabled=True,
                translation_memory_outbox_path=root / "tm-outbox",
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [_block(1, "こんにちは")]
            target = [SrtBlock(1, source[0].timing, ["你好"])]
            write_srt(paths.ja_srt, source)
            write_srt(paths.zh_cn_srt, target)
            for path, text in (
                (paths.ai_ja_ass, "ja"),
                (paths.ai_zh_cn_ass, "zh-cn"),
                (paths.ai_zh_tw_ass, "zh-tw"),
            ):
                path.write_text(text, encoding="utf-8")
            scope = worker._translation_memory_scope(video)
            digest = translation_memory_full_plan_digest(
                scope,
                source,
                translation_lineage_mode="no_hits",
            )
            write_translation_memory_origin(
                root,
                paths.zh_cn_srt,
                source_srt_path=paths.ja_srt,
                source_srt_sha256=sha256_file(paths.ja_srt),
                target_srt_sha256=sha256_file(paths.zh_cn_srt),
                split_decision_digest=digest,
                cached_indexes=(),
                translation_lineage_mode="no_hits",
                scope=scope,
            )
            origin = worker._read_translation_memory_origin_for_video(video, paths)
            provenance = {
                "translation_memory": worker._translation_memory_lineage_payload(origin)
            }
            begin_output_publication(video, config)
            manifest = write_output_manifest(
                video,
                config,
                [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                provenance=provenance,
                publication_kind="translated_trilingual",
                output_languages=("ja", "zh-CN", "zh-TW"),
            )

            recorded = worker._record_translation_memory_outbox(
                video,
                paths,
                manifest,
                origin,
            )

            self.assertIsNotNone(recorded)
            intent = load_translation_memory_outbox_intent(recorded.path)
            self.assertEqual(intent.translation_lineage_mode, "no_hits")
            self.assertEqual(intent.tm_origin_indexes, ())
            payload = provenance["translation_memory"]
            self.assertEqual(payload["contract"], TRANSLATION_MEMORY_LINEAGE_CONTRACT)
            self.assertEqual(intent.split_decision_digest, payload["split_decision_digest"])

    def test_manual_or_unknown_run_never_records_learning_intent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(
                root,
                translation_memory_enabled=True,
                translation_memory_outbox_path=root / "tm-outbox",
                scanner_cache_enabled=False,
                scanner_queue_enabled=False,
            )
            worker = VideoWorker(config, _logger())
            self.assertTrue(worker._manual_ai_requested(video))
            worker._translation_memory_manual_run = True

            paths = paths_for_video(video, config)
            source = [_block(1, "source")]
            target = [SrtBlock(1, source[0].timing, ["target"])]
            write_srt(paths.ja_srt, source)
            write_srt(paths.zh_cn_srt, target)
            for path, text in (
                (paths.ai_ja_ass, "ja"),
                (paths.ai_zh_cn_ass, "zh-cn"),
                (paths.ai_zh_tw_ass, "zh-tw"),
            ):
                path.write_text(text, encoding="utf-8")
            scope = worker._translation_memory_scope(video)
            digest = translation_memory_full_plan_digest(
                scope,
                source,
                translation_lineage_mode="no_hits",
            )
            write_translation_memory_origin(
                root,
                paths.zh_cn_srt,
                source_srt_path=paths.ja_srt,
                source_srt_sha256=sha256_file(paths.ja_srt),
                target_srt_sha256=sha256_file(paths.zh_cn_srt),
                split_decision_digest=digest,
                cached_indexes=(),
                translation_lineage_mode="no_hits",
                scope=scope,
            )
            origin = worker._read_translation_memory_origin_for_video(video, paths)
            begin_output_publication(video, config)
            manifest = write_output_manifest(
                video,
                config,
                [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                provenance={
                    "translation_memory": worker._translation_memory_lineage_payload(origin)
                },
                publication_kind="translated_trilingual",
                output_languages=("ja", "zh-CN", "zh-TW"),
            )

            self.assertIsNone(
                worker._record_translation_memory_outbox(
                    video,
                    paths,
                    manifest,
                    origin,
                )
            )
            self.assertFalse((root / "tm-outbox").exists())

    def test_valid_hash_bound_hold_resumes_after_zh_cn_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(root, translation_memory_enabled=True)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [_block(1, "source")]
            target = [SrtBlock(1, source[0].timing, ["target"])]
            write_srt(paths.ja_srt, source)
            write_srt(paths.zh_cn_srt, target)
            write_srt(paths.zh_tw_srt, target)
            write_translation_quality_hold(
                paths.zh_cn_srt,
                srt_sha256=sha256_file(paths.zh_cn_srt),
                reason="crash after complete zh-CN commit",
            )
            scope = worker._translation_memory_scope(video)
            write_translation_memory_origin(
                root,
                paths.zh_cn_srt,
                source_srt_path=paths.ja_srt,
                source_srt_sha256=sha256_file(paths.ja_srt),
                target_srt_sha256=sha256_file(paths.zh_cn_srt),
                split_decision_digest=translation_memory_full_plan_digest(
                    scope,
                    source,
                    translation_lineage_mode="no_hits",
                ),
                cached_indexes=(),
                translation_lineage_mode="no_hits",
                scope=scope,
            )

            worker._validate_translation_cache_chain(video, paths)

            self.assertTrue(paths.zh_cn_srt.is_file())
            self.assertTrue(translation_quality_hold_path(paths.zh_cn_srt).is_file())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertIsNotNone(
                worker._read_translation_memory_origin_for_video(video, paths)
            )

    def test_hold_without_lineage_is_invalidated_instead_of_learned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(root, translation_memory_enabled=True)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [_block(1, "source")]
            target = [SrtBlock(1, source[0].timing, ["target"])]
            write_srt(paths.ja_srt, source)
            write_srt(paths.zh_cn_srt, target)
            write_srt(paths.zh_tw_srt, target)
            write_translation_quality_hold(
                paths.zh_cn_srt,
                srt_sha256=sha256_file(paths.zh_cn_srt),
                reason="missing lineage crash",
            )

            worker._validate_translation_cache_chain(video, paths)

            self.assertFalse(paths.zh_cn_srt.exists())
            self.assertFalse(paths.zh_tw_srt.exists())
            self.assertFalse(translation_quality_hold_path(paths.zh_cn_srt).exists())

    def test_qc_rebind_preserves_tm_origins_and_binds_final_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(root, translation_memory_enabled=True)
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [_block(1, "source one"), _block(2, "source two")]
            before = [
                SrtBlock(1, source[0].timing, ["cached"]),
                SrtBlock(2, source[1].timing, ["model before QC"]),
            ]
            after = [
                SrtBlock(1, source[0].timing, ["cached"]),
                SrtBlock(2, source[1].timing, ["model after QC"]),
            ]
            write_srt(paths.ja_srt, source)
            write_srt(paths.zh_cn_srt, before)
            scope = worker._translation_memory_scope(video)
            write_translation_memory_origin(
                root,
                paths.zh_cn_srt,
                source_srt_path=paths.ja_srt,
                source_srt_sha256=sha256_file(paths.ja_srt),
                target_srt_sha256=sha256_file(paths.zh_cn_srt),
                split_decision_digest="d" * 64,
                cached_indexes=(1,),
                translation_lineage_mode="tm_split",
                scope=scope,
            )
            origin = worker._read_translation_memory_origin_for_video(video, paths)
            write_srt(paths.zh_cn_srt, after)

            rebound = worker._rebind_translation_memory_origin_after_qc(
                video,
                paths,
                origin,
            )

            self.assertIsNotNone(rebound)
            self.assertEqual(rebound.cached_indexes, (1,))
            self.assertEqual(rebound.target_srt_sha256, sha256_file(paths.zh_cn_srt))

    def test_startup_replay_is_bounded_and_retains_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = root / "tm-outbox"
            outbox.mkdir()
            for name in ("a.json", "b.json", "c.json"):
                (outbox / name).write_text("{}", encoding="utf-8")
            worker = VideoWorker(
                _config(
                    root,
                    translation_memory_enabled=True,
                    translation_memory_outbox_path=outbox,
                ),
                _logger(),
            )
            with patch(
                "translation_memory_replay.replay_translation_memory_outbox_intent",
                side_effect=(object(), RuntimeError("locked")),
            ) as replay:
                summary = worker.replay_pending_translation_memory_outbox(limit=2)

            self.assertEqual(summary, {"attempted": 2, "completed": 1, "retained": 1})
            self.assertEqual([call.args[0].name for call in replay.call_args_list], ["a.json", "b.json"])

    def test_startup_replay_import_failure_is_optional_and_retains_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outbox = root / "tm-outbox"
            outbox.mkdir()
            for name in ("a.json", "b.json", "c.json"):
                (outbox / name).write_text("{}", encoding="utf-8")
            worker = VideoWorker(
                _config(
                    root,
                    translation_memory_enabled=True,
                    translation_memory_outbox_path=outbox,
                ),
                _logger(),
            )
            original_import = __import__

            def fail_replay_import(name, *args, **kwargs):
                if name == "translation_memory_replay":
                    raise ImportError("consumer unavailable")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fail_replay_import):
                summary = worker.replay_pending_translation_memory_outbox(limit=2)

            self.assertEqual(summary, {"attempted": 2, "completed": 0, "retained": 2})
            self.assertTrue((outbox / "a.json").exists())
            self.assertTrue((outbox / "b.json").exists())

    def test_interrupted_publication_finishes_existing_manifest_without_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(
                root,
                export_ai_ass=True,
                translation_memory_enabled=True,
                translation_memory_outbox_path=root / "tm-outbox",
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [_block(1, "source")]
            target = [SrtBlock(1, source[0].timing, ["target"])]
            write_srt(paths.ja_srt, source)
            write_srt(paths.zh_cn_srt, target)
            for path, text in (
                (paths.ai_ja_ass, "ja"),
                (paths.ai_zh_cn_ass, "zh-cn"),
                (paths.ai_zh_tw_ass, "zh-tw"),
            ):
                path.write_text(text, encoding="utf-8")
            scope = worker._translation_memory_scope(video)
            write_translation_memory_origin(
                root,
                paths.zh_cn_srt,
                source_srt_path=paths.ja_srt,
                source_srt_sha256=sha256_file(paths.ja_srt),
                target_srt_sha256=sha256_file(paths.zh_cn_srt),
                split_decision_digest=translation_memory_full_plan_digest(
                    scope,
                    source,
                    translation_lineage_mode="no_hits",
                ),
                cached_indexes=(),
                translation_lineage_mode="no_hits",
                scope=scope,
            )
            origin = worker._read_translation_memory_origin_for_video(video, paths)
            begin_output_publication(video, config)
            manifest = write_output_manifest(
                video,
                config,
                [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                provenance={
                    "translation_memory": worker._translation_memory_lineage_payload(origin)
                },
                publication_kind="translated_trilingual",
                output_languages=("ja", "zh-CN", "zh-TW"),
            )
            recorded = worker._record_translation_memory_outbox(
                video,
                paths,
                manifest,
                origin,
            )
            manifest_hash = sha256_file(manifest)

            with patch.object(worker, "_replay_translation_memory_outbox_best_effort") as replay:
                recovered = worker._recover_interrupted_output_publication(video, paths)

            self.assertTrue(recovered)
            self.assertFalse(output_publication_marker_path(video, config).exists())
            self.assertEqual(sha256_file(manifest), manifest_hash)
            self.assertTrue(recorded.path.exists())
            replay.assert_called_once_with(recorded.path)
            identity = delivery_identity(video, config)
            self.assertTrue(
                validate_output_manifest(
                    video,
                    config,
                    verify_hashes=True,
                    required_outputs=(paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass),
                    require_delivery_evidence=True,
                    expected_obligation_id=str(identity["obligation_id"]),
                    expected_policy_revision=str(identity["policy_revision"]),
                    expected_publication_kind="translated_trilingual",
                    expected_output_languages=("ja", "zh-CN", "zh-TW"),
                    require_publication_semantics=True,
                )
            )

    def test_outbox_write_failure_keeps_publication_replayable_until_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Anime S01E01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"video")
            config = _config(
                root,
                export_ai_ass=True,
                translation_memory_enabled=True,
                translation_memory_outbox_path=root / "tm-outbox",
            )
            worker = VideoWorker(config, _logger())
            paths = paths_for_video(video, config)
            source = [_block(1, "source")]
            target = [SrtBlock(1, source[0].timing, ["target"])]
            write_srt(paths.ja_srt, source)
            write_srt(paths.zh_cn_srt, target)
            for path, text in (
                (paths.ai_ja_ass, "ja"),
                (paths.ai_zh_cn_ass, "zh-cn"),
                (paths.ai_zh_tw_ass, "zh-tw"),
            ):
                path.write_text(text, encoding="utf-8")
            scope = worker._translation_memory_scope(video)
            write_translation_memory_origin(
                root,
                paths.zh_cn_srt,
                source_srt_path=paths.ja_srt,
                source_srt_sha256=sha256_file(paths.ja_srt),
                target_srt_sha256=sha256_file(paths.zh_cn_srt),
                split_decision_digest=translation_memory_full_plan_digest(
                    scope,
                    source,
                    translation_lineage_mode="no_hits",
                ),
                cached_indexes=(),
                translation_lineage_mode="no_hits",
                scope=scope,
            )
            origin = worker._read_translation_memory_origin_for_video(video, paths)
            begin_output_publication(video, config)
            manifest = write_output_manifest(
                video,
                config,
                [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
                provenance={
                    "translation_memory": worker._translation_memory_lineage_payload(origin)
                },
                publication_kind="translated_trilingual",
                output_languages=("ja", "zh-CN", "zh-TW"),
            )
            manifest_hash = sha256_file(manifest)

            with patch.object(
                worker,
                "_record_translation_memory_outbox",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    worker._recover_interrupted_output_publication(video, paths)

            self.assertTrue(output_publication_marker_path(video, config).is_file())
            self.assertTrue(paths.ja_srt.is_file())
            self.assertTrue(paths.zh_cn_srt.is_file())
            self.assertIsNotNone(
                worker._read_translation_memory_origin_for_video(video, paths)
            )
            self.assertEqual(sha256_file(manifest), manifest_hash)

            with patch.object(worker, "_replay_translation_memory_outbox_best_effort") as replay:
                self.assertTrue(
                    worker._recover_interrupted_output_publication(video, paths)
                )

            self.assertFalse(output_publication_marker_path(video, config).exists())
            intents = sorted((root / "tm-outbox").glob("*.json"))
            self.assertEqual(len(intents), 1)
            replay.assert_called_once_with(intents[0])
            self.assertEqual(sha256_file(manifest), manifest_hash)


if __name__ == "__main__":
    unittest.main()
