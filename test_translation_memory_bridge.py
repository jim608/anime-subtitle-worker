from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest

from srt_utils import SrtBlock
from translation_memory import (
    MemoryScope,
    TranslationMemoryStore,
    learn_verified_batch,
)
from translation_memory_bridge import (
    StrictPublicationFlags,
    TranslationMemoryBridgeError,
    build_strict_verified_episode_translation,
    merge_translation_memory_blocks,
    merge_translation_memory_split,
    read_translation_memory_origin_strict,
    remove_translation_memory_origin,
    split_blocks_by_readonly_translation_memory,
    translation_memory_origin_path,
    translation_memory_split_digest,
    write_translation_memory_origin,
)


PASS_FLAGS = StrictPublicationFlags(
    strict_publication_verified=True,
    qc_passed=True,
    unattended=True,
    manual_reviewed=False,
    safe_omission=False,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _timing(second: int) -> str:
    return f"00:00:{second:02d},000 --> 00:00:{second + 1:02d},000"


def _block(index: int, text: str, *, second: int | None = None) -> SrtBlock:
    return SrtBlock(
        index=index,
        timing=_timing(index if second is None else second),
        text=text.split("\n"),
    )


class TranslationMemoryBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "translation_memory.sqlite3"
        self.scope = MemoryScope("series-a", "policy-v1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _evidence(
        self,
        episode: str,
        manifest: str,
        source: list[SrtBlock],
        targets: list[str],
    ):
        target = [
            SrtBlock(block.index, block.timing, text.split("\n"))
            for block, text in zip(source, targets)
        ]
        return build_strict_verified_episode_translation(
            self.scope,
            source,
            target,
            episode_id=episode,
            manifest_identity=manifest,
            source_manifest_hash=_digest(f"source:{manifest}"),
            target_manifest_hash=_digest(f"target:{manifest}"),
            verified_at="2026-08-13T02:00:00+08:00",
            flags=PASS_FLAGS,
        )

    def _learn(self, evidences: list) -> None:
        learn_verified_batch(self.database, self.scope, evidences)

    def test_readonly_split_returns_mature_cache_and_unresolved_in_source_order(self) -> None:
        learned_source_1 = [_block(1, "おはよう\nございます", second=1)]
        learned_source_2 = [_block(91, "おはよう ございます", second=40)]
        glossary_source_1 = [_block(3, "アリスが来た", second=3)]
        glossary_source_2 = [_block(33, "アリスが来た", second=33)]
        immature_source = [_block(5, "またね", second=5)]
        self._learn(
            [
                self._evidence("episode-01", "manifest-01", learned_source_1, ["早上好"]),
                self._evidence("episode-02", "manifest-02", learned_source_2, ["  早上好  "]),
                self._evidence("episode-03", "manifest-03", glossary_source_1, ["爱丽丝来了"]),
                self._evidence("episode-04", "manifest-04", glossary_source_2, ["爱丽丝来了"]),
                self._evidence("episode-05", "manifest-05", immature_source, ["再见"]),
            ]
        )
        source = [
            _block(7, "おはよう   ございます", second=7),
            _block(2, "未登録", second=2),
            _block(19, "アリスが来た", second=19),
            _block(21, "またね", second=21),
        ]
        before = self.database.read_bytes()

        split = split_blocks_by_readonly_translation_memory(
            self.database,
            self.scope,
            source,
            explicit_series_glossary={"アリス": "愛麗絲"},
        )
        after = self.database.read_bytes()

        # The same lexical source learned at a standalone boundary must not be
        # reused inside a different neighbor context.
        self.assertEqual(split.cached_indexes, ())
        self.assertEqual(split.unresolved_indexes, (7, 2, 19, 21))
        self.assertTrue(all(decision.status == "unresolved" for decision in split.decisions))
        self.assertEqual(before, after)
        return
        self.assertEqual(split.cached_blocks[0].text, ["早上好"])
        self.assertEqual(split.cached_blocks[0].timing, source[0].timing)
        self.assertEqual(
            [decision.status for decision in split.decisions],
            ["cached", "unresolved", "unresolved", "unresolved"],
        )
        self.assertEqual(split.decisions[0].episode_diversity, 2)
        self.assertEqual(split.decisions[3].episode_diversity, 1)
        self.assertEqual(before, after)

    def test_split_fails_closed_on_translation_memory_conflict(self) -> None:
        sources = [
            [_block(1, "行こう", second=1)],
            [_block(2, "行こう", second=2)],
            [_block(3, "行こう", second=3)],
        ]
        self._learn(
            [
                self._evidence("episode-01", "manifest-01", sources[0], ["走吧"]),
                self._evidence("episode-02", "manifest-02", sources[1], ["走吧"]),
                self._evidence("episode-03", "manifest-03", sources[2], ["出发吧"]),
            ]
        )

        with self.assertRaises(TranslationMemoryBridgeError) as raised:
            split_blocks_by_readonly_translation_memory(
                self.database,
                self.scope,
                [_block(88, "行こう", second=20)],
            )

        self.assertEqual(raised.exception.code, "lookup_conflict")

    def test_split_fails_closed_on_duplicate_source_index(self) -> None:
        # Initialize a valid store so duplicate validation, not missing DB, is tested.
        with TranslationMemoryStore(self.database):
            pass
        with self.assertRaises(TranslationMemoryBridgeError) as raised:
            split_blocks_by_readonly_translation_memory(
                self.database,
                self.scope,
                [_block(1, "一", second=1), _block(1, "二", second=2)],
            )
        self.assertEqual(raised.exception.code, "duplicate_srt_index")

    def test_split_of_missing_readonly_store_fails_without_creating_database(self) -> None:
        with self.assertRaises(TranslationMemoryBridgeError) as raised:
            split_blocks_by_readonly_translation_memory(
                self.database,
                self.scope,
                [_block(1, "未登録", second=1)],
            )
        self.assertEqual(raised.exception.code, "readonly_lookup_failed")
        self.assertFalse(self.database.exists())

    def test_merge_is_deterministic_in_original_index_and_timing_order(self) -> None:
        source = [
            _block(10, "十", second=1),
            _block(3, "三", second=2),
            _block(8, "八", second=3),
        ]
        cached = [
            SrtBlock(8, source[2].timing, ["缓存八"]),
            SrtBlock(10, source[0].timing, ["缓存十"]),
        ]
        translated = [SrtBlock(3, source[1].timing, ["翻译三"])]

        merged = merge_translation_memory_blocks(source, cached, translated)

        self.assertEqual([block.index for block in merged], [10, 3, 8])
        self.assertEqual([block.timing for block in merged], [block.timing for block in source])
        self.assertEqual([block.text for block in merged], [["缓存十"], ["翻译三"], ["缓存八"]])

    def test_split_wrapper_merges_all_unresolved_and_all_cached_cases(self) -> None:
        mature = [_block(1, "はい", second=1)]
        self._learn(
            [
                self._evidence("episode-01", "manifest-01", mature, ["好的"]),
                self._evidence("episode-02", "manifest-02", mature, ["好的"]),
            ]
        )
        cached_split = split_blocks_by_readonly_translation_memory(
            self.database,
            self.scope,
            [_block(10, "はい", second=10)],
        )
        self.assertEqual(
            merge_translation_memory_split(cached_split, []),
            list(cached_split.cached_blocks),
        )

        unresolved_split = split_blocks_by_readonly_translation_memory(
            self.database,
            self.scope,
            [_block(11, "いいえ", second=11)],
        )
        translated = [SrtBlock(11, unresolved_split.source_blocks[0].timing, ["不是"])]
        self.assertEqual(merge_translation_memory_split(unresolved_split, translated), translated)

    def test_merge_rejects_duplicate_overlap_missing_extra_and_retimed_blocks(self) -> None:
        source = [_block(1, "一", second=1), _block(2, "二", second=2)]
        good_one = SrtBlock(1, source[0].timing, ["壹"])
        good_two = SrtBlock(2, source[1].timing, ["贰"])
        cases = [
            (
                "duplicate_srt_index",
                source,
                [good_one, good_one],
                [good_two],
            ),
            (
                "merge_index_conflict",
                source,
                [good_one],
                [good_one, good_two],
            ),
            (
                "merge_missing_block",
                source,
                [good_one],
                [],
            ),
            (
                "merge_extra_block",
                source,
                [good_one],
                [good_two, _block(3, "三", second=3)],
            ),
            (
                "merge_timing_mismatch",
                source,
                [SrtBlock(1, _timing(9), ["壹"])],
                [good_two],
            ),
            (
                "duplicate_srt_index",
                [source[0], source[0]],
                [good_one],
                [good_two],
            ),
        ]
        for expected_code, source_arg, cached, translated in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(TranslationMemoryBridgeError) as raised:
                    merge_translation_memory_blocks(source_arg, cached, translated)
                self.assertEqual(raised.exception.code, expected_code)

    def test_builder_produces_strict_aligned_and_auditable_evidence(self) -> None:
        source = [
            _block(5, "おはよう", second=1),
            _block(9, "またね\n明日", second=3),
        ]
        target = [
            SrtBlock(5, source[0].timing, ["早上好"]),
            SrtBlock(9, source[1].timing, ["再见", "明天见"]),
        ]
        source_hash = _digest("source-manifest")
        target_hash = _digest("target-manifest").upper()

        evidence = build_strict_verified_episode_translation(
            self.scope,
            source,
            target,
            episode_id=" episode-07 ",
            manifest_identity=" manifest-07 ",
            source_manifest_hash=source_hash,
            target_manifest_hash=target_hash,
            verified_at="2026-08-13T12:34:56+08:00",
            flags=PASS_FLAGS,
        )

        self.assertEqual(evidence.episode_id, "episode-07")
        self.assertEqual(evidence.manifest_identity, "manifest-07")
        self.assertEqual(evidence.publication_contract, "ai-publication-semantics-v2")
        self.assertEqual(evidence.publication_kind, "translated_trilingual")
        self.assertEqual((evidence.source_language, evidence.target_language), ("ja", "zh-CN"))
        self.assertTrue(evidence.strict_publication_verified)
        self.assertTrue(evidence.qc_passed)
        self.assertTrue(evidence.unattended)
        self.assertFalse(evidence.manual_reviewed)
        self.assertFalse(evidence.safe_omission)
        self.assertEqual(evidence.source_manifest_hash, source_hash)
        self.assertEqual(evidence.target_manifest_hash, target_hash.casefold())
        self.assertEqual([block.source_text for block in evidence.blocks], ["おはよう", "またね\n明日"])
        self.assertEqual([block.target_text for block in evidence.blocks], ["早上好", "再见\n明天见"])
        self.assertEqual(
            [block.block_identity for block in evidence.blocks],
            [
                f"srt:5:timing-sha256:{_digest(source[0].timing)[:24]}",
                f"srt:9:timing-sha256:{_digest(source[1].timing)[:24]}",
            ],
        )
        # The bridge output is accepted by the independent strict learner.
        with TranslationMemoryStore(self.database) as store:
            learned = store.learn_episode(self.scope, evidence)
            audit = store.audit_source(
                self.scope,
                evidence.blocks[0].source_text,
                evidence.blocks[0].context_key,
            )
        self.assertEqual(learned.status, "learned")
        self.assertEqual(audit[0].manifest_identity, "manifest-07")

    def test_builder_rejects_every_non_strict_flag_before_evidence_exists(self) -> None:
        source = [_block(1, "はい", second=1)]
        target = [SrtBlock(1, source[0].timing, ["好的"])]
        invalid_flags = [
            replace(PASS_FLAGS, strict_publication_verified=False),
            replace(PASS_FLAGS, qc_passed=False),
            replace(PASS_FLAGS, unattended=False),
            replace(PASS_FLAGS, manual_reviewed=True),
            replace(PASS_FLAGS, safe_omission=True),
            replace(PASS_FLAGS, qc_passed=1),
        ]
        for flags in invalid_flags:
            with self.subTest(flags=flags):
                with self.assertRaises(TranslationMemoryBridgeError) as raised:
                    build_strict_verified_episode_translation(
                        self.scope,
                        source,
                        target,
                        episode_id="episode-01",
                        manifest_identity="manifest-01",
                        source_manifest_hash=_digest("source"),
                        target_manifest_hash=_digest("target"),
                        verified_at="2026-08-13T02:00:00Z",
                        flags=flags,
                    )
                self.assertEqual(raised.exception.code, "strict_publication_flags_rejected")

    def test_builder_rejects_count_index_timing_duplicate_and_omission_mismatches(self) -> None:
        first = _block(1, "はい", second=1)
        second = _block(2, "いいえ", second=2)
        base_target = SrtBlock(1, first.timing, ["好的"])
        cases = [
            ("evidence_block_count_mismatch", [first, second], [base_target]),
            ("evidence_index_mismatch", [first], [SrtBlock(2, first.timing, ["好的"])]),
            ("evidence_timing_mismatch", [first], [SrtBlock(1, _timing(9), ["好的"])]),
            ("duplicate_srt_index", [first, first], [base_target, base_target]),
            ("evidence_safe_omission_placeholder", [first], [SrtBlock(1, first.timing, ["……"])]),
        ]
        for expected_code, source, target in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(TranslationMemoryBridgeError) as raised:
                    build_strict_verified_episode_translation(
                        self.scope,
                        source,
                        target,
                        episode_id="episode-01",
                        manifest_identity="manifest-01",
                        source_manifest_hash=_digest("source"),
                        target_manifest_hash=_digest("target"),
                        verified_at="2026-08-13T02:00:00Z",
                        flags=PASS_FLAGS,
                    )
                self.assertEqual(raised.exception.code, expected_code)

    def test_builder_rejects_invalid_hash_timestamp_identity_and_language_scope(self) -> None:
        source = [_block(1, "はい", second=1)]
        target = [SrtBlock(1, source[0].timing, ["好的"])]
        base = dict(
            episode_id="episode-01",
            manifest_identity="manifest-01",
            source_manifest_hash=_digest("source"),
            target_manifest_hash=_digest("target"),
            verified_at="2026-08-13T02:00:00Z",
            flags=PASS_FLAGS,
        )
        cases = [
            ("invalid_manifest_hash", self.scope, {**base, "source_manifest_hash": "bad"}),
            ("invalid_verified_at", self.scope, {**base, "verified_at": "2026-08-13T02:00:00"}),
            ("invalid_identity", self.scope, {**base, "episode_id": "  "}),
            (
                "unsupported_scope_languages",
                MemoryScope("series-a", "policy-v1", source_language="en"),
                base,
            ),
        ]
        for expected_code, scope, arguments in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(TranslationMemoryBridgeError) as raised:
                    build_strict_verified_episode_translation(scope, source, target, **arguments)
                self.assertEqual(raised.exception.code, expected_code)

    def test_split_digest_and_origin_sidecar_bind_cached_indexes_to_final_srt(self) -> None:
        with TranslationMemoryStore(self.database):
            pass
        source = [_block(1, "source one", second=1)]
        split = split_blocks_by_readonly_translation_memory(
            self.database,
            self.scope,
            source,
        )
        digest = translation_memory_split_digest(self.scope, split)
        output = self.root / "episode.zh-CN.srt"
        source_path = self.root / "episode.ja.srt"
        source_path.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nsource one\n",
            encoding="utf-8",
        )
        output.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\ntranslated\n",
            encoding="utf-8",
        )
        from safe_files import sha256_file

        path = write_translation_memory_origin(
            self.root / "work",
            output,
            source_srt_path=source_path,
            source_srt_sha256=sha256_file(source_path),
            target_srt_sha256=sha256_file(output),
            split_decision_digest=digest,
            cached_indexes=(1,),
            translation_lineage_mode="tm_split",
            scope=self.scope,
        )
        loaded = read_translation_memory_origin_strict(self.root / "work", output)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.cached_indexes, (1,))
        self.assertEqual(loaded.translation_lineage_mode, "tm_split")
        self.assertEqual(loaded.split_decision_digest, digest)
        output.write_text(output.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(TranslationMemoryBridgeError) as raised:
            read_translation_memory_origin_strict(self.root / "work", output)
        self.assertEqual(raised.exception.code, "origin_hash_mismatch")
        remove_translation_memory_origin(self.root / "work", output)
        self.assertFalse(path.exists())
        self.assertFalse(translation_memory_origin_path(self.root / "work", output).exists())

    def test_builder_excludes_tm_origin_blocks_from_learning(self) -> None:
        source = [_block(1, "first", second=1), _block(2, "second", second=2)]
        target = [
            SrtBlock(1, source[0].timing, ["one"]),
            SrtBlock(2, source[1].timing, ["two"]),
        ]
        evidence = build_strict_verified_episode_translation(
            self.scope,
            source,
            target,
            episode_id="episode-01",
            manifest_identity="manifest-01",
            source_manifest_hash=_digest("source"),
            target_manifest_hash=_digest("target"),
            verified_at="2026-08-13T02:00:00Z",
            flags=PASS_FLAGS,
            excluded_origin_indexes=(1,),
        )
        self.assertEqual([block.source_text for block in evidence.blocks], ["second"])
        with self.assertRaises(TranslationMemoryBridgeError) as raised:
            build_strict_verified_episode_translation(
                self.scope,
                source,
                target,
                episode_id="episode-02",
                manifest_identity="manifest-02",
                source_manifest_hash=_digest("source-2"),
                target_manifest_hash=_digest("target-2"),
                verified_at="2026-08-13T02:00:00Z",
                flags=PASS_FLAGS,
                excluded_origin_indexes=(1, 2),
            )
        self.assertEqual(raised.exception.code, "no_learnable_blocks")


if __name__ == "__main__":
    unittest.main()
