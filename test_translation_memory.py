from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from translation_memory import (
    AlignedTranslationBlock,
    EvidenceRejected,
    MAX_TERM_OBSERVATIONS_PER_EPISODE,
    MemoryScope,
    ReadOnlyStoreError,
    TranslationMemoryError,
    TranslationMemoryStore,
    VerifiedEpisodeTranslation,
    build_context_key,
    learn_verified_batch,
    lookup_translations_readonly,
    normalize_source_key,
    sha256_text,
)


DEFAULT_CONTEXT = build_context_key("前の台詞", "次の台詞")


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _block(
    identity: str,
    source: str,
    target: str,
    *,
    context_key: str = DEFAULT_CONTEXT,
    qc_passed: bool = True,
    manual_reviewed: bool = False,
    safe_omission: bool = False,
) -> AlignedTranslationBlock:
    return AlignedTranslationBlock(
        block_identity=identity,
        source_text=source,
        target_text=target,
        context_key=context_key,
        qc_passed=qc_passed,
        manual_reviewed=manual_reviewed,
        safe_omission=safe_omission,
    )


def _lookup(
    store: TranslationMemoryStore,
    scope: MemoryScope,
    source_texts: list[str] | tuple[str, ...],
    *,
    context_keys: list[str] | tuple[str, ...] | None = None,
    explicit_series_glossary: dict[str, str] | None = None,
):
    contexts = context_keys or tuple(DEFAULT_CONTEXT for _ in source_texts)
    return store.lookup_batch(
        scope,
        source_texts,
        contexts,
        explicit_series_glossary=explicit_series_glossary,
    )


def _evidence(
    episode_id: str,
    manifest_identity: str,
    blocks: list[AlignedTranslationBlock] | tuple[AlignedTranslationBlock, ...],
    *,
    series_key: str = "series-a",
    policy_version: str = "policy-v1",
    verified_at: str = "2026-08-13T01:00:00Z",
) -> VerifiedEpisodeTranslation:
    return VerifiedEpisodeTranslation(
        series_key=series_key,
        policy_version=policy_version,
        episode_id=episode_id,
        manifest_identity=manifest_identity,
        publication_contract="ai-publication-semantics-v2",
        publication_kind="translated_trilingual",
        source_language="ja",
        target_language="zh-CN",
        strict_publication_verified=True,
        qc_passed=True,
        unattended=True,
        manual_reviewed=False,
        safe_omission=False,
        source_manifest_hash=_digest(f"source:{manifest_identity}"),
        target_manifest_hash=_digest(f"target:{manifest_identity}"),
        verified_at=verified_at,
        blocks=tuple(blocks),
    )


class TranslationMemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "translation_memory.sqlite3"
        self.scope = MemoryScope("series-a", "policy-v1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cross_episode_exact_source_auto_apply_ignores_block_identity(self) -> None:
        first = _evidence(
            "episode-01",
            "manifest-01",
            [_block("cue-0001@00:00:01", "おはよう\nございます", "早上好")],
        )
        second = _evidence(
            "episode-02",
            "manifest-02",
            [_block("different-cue@00:18:42", "  おはよう   ございます  ", "  早上好  ")],
        )
        with TranslationMemoryStore(self.database) as store:
            results = store.learn_batch(self.scope, [first, second])
            self.assertEqual([item.status for item in results], ["learned", "learned"])
            lookup = _lookup(store, self.scope, ["おはよう ございます"])[0]
            stats = store.diagnostics(self.scope)

        self.assertTrue(lookup.can_auto_apply)
        self.assertEqual(lookup.auto_target, "早上好")
        self.assertEqual(lookup.candidates[0].support_count, 2)
        self.assertEqual(lookup.candidates[0].episode_diversity, 2)
        self.assertEqual(stats.auto_apply_source_count, 1)
        self.assertEqual(normalize_source_key("おはよう\n  ございます"), "おはよう ございます")

    def test_support_from_one_episode_never_satisfies_diversity_threshold(self) -> None:
        first = _evidence(
            "episode-01",
            "manifest-01a",
            [_block("1", "またね", "再见")],
        )
        second = _evidence(
            "episode-01",
            "manifest-01b",
            [_block("2", "またね", "再见")],
        )
        with TranslationMemoryStore(self.database) as store:
            store.learn_batch(self.scope, [first, second])
            lookup = _lookup(store, self.scope, ["またね"])[0]

        self.assertEqual(lookup.status, "insufficient_support")
        self.assertEqual(lookup.candidates[0].support_count, 2)
        self.assertEqual(lookup.candidates[0].episode_diversity, 1)

    def test_conflicting_target_is_deterministic_and_disables_auto_apply(self) -> None:
        evidences = [
            _evidence("episode-01", "manifest-01", [_block("1", "行こう", "走吧")]),
            _evidence("episode-02", "manifest-02", [_block("2", "行こう", "走吧")]),
            _evidence("episode-03", "manifest-03", [_block("3", "行こう", "出发吧")]),
        ]
        with TranslationMemoryStore(self.database) as store:
            store.learn_batch(self.scope, evidences)
            first = _lookup(store, self.scope, ["行こう"])[0]
            second = _lookup(store, self.scope, ["行こう"])[0]
            stats = store.diagnostics(self.scope)

        self.assertEqual(first.status, "conflict")
        self.assertIsNone(first.auto_target)
        self.assertEqual(first, second)
        self.assertEqual([item.target_text for item in first.candidates], ["走吧", "出发吧"])
        self.assertEqual(stats.conflict_source_count, 1)
        self.assertEqual(stats.auto_apply_source_count, 0)

    def test_batch_lookup_preserves_input_order_duplicates_and_missing(self) -> None:
        evidences = [
            _evidence("episode-01", "manifest-01", [_block("1", "おはよう", "早上好")]),
            _evidence("episode-02", "manifest-02", [_block("2", "おはよう", "早上好")]),
        ]
        with TranslationMemoryStore(self.database) as store:
            store.learn_batch(self.scope, evidences)
            lookups = _lookup(
                store,
                self.scope,
                ["未登録", " おはよう ", "おはよう"],
            )

        self.assertEqual([item.status for item in lookups], ["not_found", "auto_apply", "auto_apply"])
        self.assertEqual([item.source_text for item in lookups], ["未登録", " おはよう ", "おはよう"])
        self.assertEqual(lookups[1].source_key, lookups[2].source_key)

    def test_series_and_policy_scopes_are_isolated(self) -> None:
        evidences = [
            _evidence("episode-01", "manifest-01", [_block("1", "はい", "好的")]),
            _evidence("episode-02", "manifest-02", [_block("2", "はい", "好的")]),
        ]
        with TranslationMemoryStore(self.database) as store:
            store.learn_batch(self.scope, evidences)
            wrong_series = _lookup(store, MemoryScope("series-b", "policy-v1"), ["はい"])[0]
            wrong_policy = _lookup(store, MemoryScope("series-a", "policy-v2"), ["はい"])[0]
            correct = _lookup(store, self.scope, ["はい"])[0]
            empty_stats = store.diagnostics(MemoryScope("series-b", "policy-v1"))

        self.assertEqual(wrong_series.status, "not_found")
        self.assertEqual(wrong_policy.status, "not_found")
        self.assertEqual(correct.status, "auto_apply")
        self.assertEqual(empty_stats.manifest_count, 0)
        self.assertEqual(empty_stats.last_verified, None)

    def test_poison_evidence_is_rejected_without_any_partial_learning(self) -> None:
        base = _evidence("episode-01", "manifest-01", [_block("1", "はい", "好的")])
        poisons: list[tuple[str, VerifiedEpisodeTranslation]] = [
            ("source_language_publication_rejected", replace(base, publication_kind="source_language")),
            ("strict_publication_not_verified", replace(base, strict_publication_verified=False)),
            ("publication_qc_failed", replace(base, qc_passed=False)),
            ("not_unattended", replace(base, unattended=False)),
            ("manual_review_rejected", replace(base, manual_reviewed=True)),
            ("safe_omission_rejected", replace(base, safe_omission=True)),
            ("source_language_mismatch", replace(base, source_language="en")),
            ("target_language_mismatch", replace(base, target_language="zh-TW")),
            ("invalid_hash", replace(base, target_manifest_hash="not-a-hash")),
            (
                "block_qc_failed",
                replace(base, blocks=(_block("1", "はい", "好的", qc_passed=False),)),
            ),
            (
                "block_manual_review_rejected",
                replace(base, blocks=(_block("1", "はい", "好的", manual_reviewed=True),)),
            ),
            (
                "block_safe_omission_rejected",
                replace(base, blocks=(_block("1", "はい", "好的", safe_omission=True),)),
            ),
            (
                "empty_aligned_text",
                replace(base, blocks=(_block("1", "はい", "   "),)),
            ),
            (
                "safe_omission_placeholder_rejected",
                replace(base, blocks=(_block("1", "はい", "……"),)),
            ),
            (
                "invalid_context_key",
                replace(base, blocks=(_block("1", "はい", "好的", context_key="scene-a"),)),
            ),
        ]

        with TranslationMemoryStore(self.database) as store:
            for expected_code, poison in poisons:
                with self.subTest(expected_code=expected_code):
                    with self.assertRaises(EvidenceRejected) as raised:
                        store.learn_episode(self.scope, poison)
                    self.assertEqual(raised.exception.code, expected_code)
            stats = store.diagnostics()

        self.assertEqual(stats.manifest_count, 0)
        self.assertEqual(stats.observation_count, 0)

    def test_manifest_learning_is_idempotent_and_refreshes_verification_time(self) -> None:
        original = _evidence(
            "episode-01",
            "manifest-01",
            [_block("1", "ありがとう", "谢谢")],
            verified_at="2026-08-13T01:00:00Z",
        )
        refreshed = replace(original, verified_at="2026-08-13T02:00:00+00:00")
        collision = replace(
            original,
            blocks=(_block("1", "ありがとう", "多谢"),),
        )
        with TranslationMemoryStore(self.database) as store:
            first = store.learn_episode(self.scope, original)
            repeated = store.learn_episode(self.scope, original)
            later = store.learn_episode(self.scope, refreshed)
            with self.assertRaises(EvidenceRejected) as raised:
                store.learn_episode(self.scope, collision)
            stats = store.diagnostics(self.scope)
            lookup = _lookup(store, self.scope, ["ありがとう"])[0]

        self.assertEqual(first.status, "learned")
        self.assertEqual(repeated.status, "idempotent")
        self.assertFalse(repeated.verification_refreshed)
        self.assertTrue(later.verification_refreshed)
        self.assertEqual(raised.exception.code, "manifest_identity_collision")
        self.assertEqual(stats.manifest_count, 1)
        self.assertEqual(stats.observation_count, 1)
        self.assertEqual(lookup.candidates[0].last_verified, "2026-08-13T02:00:00.000000Z")

    def test_batch_learning_rolls_back_every_manifest_on_sqlite_failure(self) -> None:
        first = _evidence(
            "episode-01",
            "manifest-01",
            [_block("1", "こんにちは", "你好")],
        )
        second = _evidence(
            "episode-02",
            "manifest-02",
            [_block("2", "爆発", "爆炸")],
        )
        with TranslationMemoryStore(self.database) as store:
            connection = store._require_connection()
            connection.execute(
                """
                CREATE TRIGGER reject_test_observation
                BEFORE INSERT ON tm_observation
                WHEN NEW.source_key = '爆発'
                BEGIN
                    SELECT RAISE(ABORT, 'injected atomic rollback');
                END
                """
            )
            with self.assertRaisesRegex(TranslationMemoryError, "injected atomic rollback"):
                store.learn_batch(self.scope, [first, second])
            stats = store.diagnostics()

        self.assertEqual(stats.manifest_count, 0)
        self.assertEqual(stats.observation_count, 0)

    def test_readonly_lookup_does_not_mutate_database_and_rejects_learning(self) -> None:
        evidences = [
            _evidence("episode-01", "manifest-01", [_block("1", "はい", "好的")]),
            _evidence("episode-02", "manifest-02", [_block("2", "はい", "好的")]),
        ]
        learn_verified_batch(self.database, self.scope, evidences)
        before = self.database.read_bytes()

        with TranslationMemoryStore(self.database, readonly=True) as store:
            lookup = _lookup(store, self.scope, ["はい"])[0]
            self.assertTrue(lookup.can_auto_apply)
            with self.assertRaises(ReadOnlyStoreError):
                store.learn_episode(self.scope, evidences[0])
            query_only = store._require_connection().execute("PRAGMA query_only").fetchone()[0]
            self.assertEqual(query_only, 1)

        one_shot = lookup_translations_readonly(
            self.database, self.scope, ["はい"], [DEFAULT_CONTEXT]
        )[0]
        after = self.database.read_bytes()
        self.assertEqual(one_shot.status, "auto_apply")
        self.assertEqual(before, after)

    def test_readonly_open_of_missing_store_never_creates_a_file(self) -> None:
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(TranslationMemoryError):
            TranslationMemoryStore(missing, readonly=True).open()
        self.assertFalse(missing.exists())

    def test_same_source_different_contexts_do_not_hit_or_conflict(self) -> None:
        arrival = build_context_key("門が開いた", "彼女が入ってきた")
        agreement = build_context_key("手伝ってくれる？", "ありがとう")
        unseen = build_context_key("聞こえますか", "返事がない")
        evidences = [
            _evidence(
                "episode-01", "manifest-01",
                [_block("1", "はい", "她來了", context_key=arrival)],
            ),
            _evidence(
                "episode-02", "manifest-02",
                [_block("2", "はい", "她來了", context_key=arrival)],
            ),
            _evidence(
                "episode-03", "manifest-03",
                [_block("3", "はい", "好", context_key=agreement)],
            ),
            _evidence(
                "episode-04", "manifest-04",
                [_block("4", "はい", "好", context_key=agreement)],
            ),
        ]
        with TranslationMemoryStore(self.database) as store:
            store.learn_batch(self.scope, evidences)
            lookups = _lookup(
                store,
                self.scope,
                ["はい", "はい", "はい"],
                context_keys=[arrival, agreement, unseen],
            )
            stats = store.diagnostics(self.scope)
            arrival_audit = store.audit_source(self.scope, "はい", arrival)
            agreement_audit = store.audit_source(self.scope, "はい", agreement)
            unseen_audit = store.audit_source(self.scope, "はい", unseen)

        self.assertEqual(
            [(item.status, item.auto_target) for item in lookups],
            [("auto_apply", "她來了"), ("auto_apply", "好"), ("not_found", None)],
        )
        self.assertEqual(stats.auto_apply_source_count, 2)
        self.assertEqual(stats.conflict_source_count, 0)
        self.assertEqual({item.target_text for item in arrival_audit}, {"她來了"})
        self.assertEqual({item.target_text for item in agreement_audit}, {"好"})
        self.assertEqual(unseen_audit, ())

    def test_same_context_requires_two_distinct_episodes_before_maturity(self) -> None:
        context = build_context_key("準備はいい？", "出発するよ")
        first = _evidence(
            "episode-01", "manifest-01",
            [_block("1", "行くよ", "要出發囉", context_key=context)],
        )
        second = _evidence(
            "episode-02", "manifest-02",
            [_block("2", "行くよ", "要出發囉", context_key=context)],
        )
        with TranslationMemoryStore(self.database) as store:
            store.learn_episode(self.scope, first)
            immature = _lookup(
                store, self.scope, ["行くよ"], context_keys=[context]
            )[0]
            store.learn_episode(self.scope, second)
            mature = _lookup(
                store, self.scope, ["行くよ"], context_keys=[context]
            )[0]

        self.assertEqual(immature.status, "insufficient_support")
        self.assertEqual(immature.candidates[0].episode_diversity, 1)
        self.assertEqual(mature.status, "auto_apply")
        self.assertEqual(mature.candidates[0].episode_diversity, 2)

    def test_schema_v1_database_is_rejected_without_mutation(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE tm_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO tm_meta(key, value) VALUES('schema_version', '1')")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            TranslationMemoryError,
            "Unsupported or missing translation-memory schema version",
        ):
            TranslationMemoryStore(self.database).open()
        with self.assertRaisesRegex(
            TranslationMemoryError,
            "Unsupported or missing translation-memory schema version",
        ):
            TranslationMemoryStore(self.database, readonly=True).open()

        connection = sqlite3.connect(self.database)
        rows = connection.execute("SELECT key, value FROM tm_meta ORDER BY key").fetchall()
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        connection.close()
        self.assertEqual(rows, [("schema_version", "1")])
        self.assertEqual(tables, [("tm_meta",)])

    def test_schema_v2_rejects_missing_context_lookup_index(self) -> None:
        with TranslationMemoryStore(self.database) as store:
            store._require_connection().execute("DROP INDEX idx_tm_observation_lookup")

        with self.assertRaisesRegex(
            TranslationMemoryError,
            "required index idx_tm_observation_lookup is missing",
        ):
            TranslationMemoryStore(self.database, readonly=True).open()

    def test_lookup_rejects_missing_or_noncanonical_context(self) -> None:
        with TranslationMemoryStore(self.database) as store:
            with self.assertRaisesRegex(TranslationMemoryError, "context count"):
                store.lookup_batch(self.scope, ["はい"], [])
            with self.assertRaises(EvidenceRejected) as raised:
                store.lookup_batch(self.scope, ["はい"], ["scene-a"])
        self.assertEqual(raised.exception.code, "invalid_context_key")

    def test_term_candidates_are_conservative_bounded_and_never_override_glossary(self) -> None:
        many_blocks = [
            _block(f"cue-{index:03d}", "アリス", "爱丽丝")
            for index in range(MAX_TERM_OBSERVATIONS_PER_EPISODE + 6)
        ]
        many_blocks.append(_block("sentence", "アリスです", "我是爱丽丝"))
        first = _evidence("episode-01", "manifest-01", many_blocks)
        second = _evidence(
            "episode-02",
            "manifest-02",
            [_block("1", "「アリス」", "「爱丽丝」")],
        )
        with TranslationMemoryStore(self.database) as store:
            first_result = store.learn_episode(self.scope, first)
            store.learn_episode(self.scope, second)
            terms = store.eligible_term_candidates(self.scope)
            explicit = store.eligible_term_candidates(
                self.scope,
                explicit_series_glossary={"アリス": "愛麗絲"},
            )
            term_lookup = store.lookup_term_batch(
                self.scope,
                ["アリス"],
                explicit_series_glossary={"アリス": "愛麗絲"},
            )[0]
            stats = store.diagnostics(self.scope)

        self.assertEqual(
            first_result.inserted_term_observations,
            MAX_TERM_OBSERVATIONS_PER_EPISODE,
        )
        self.assertEqual(len(terms), 1)
        self.assertEqual((terms[0].source_term, terms[0].target_term), ("アリス", "爱丽丝"))
        self.assertEqual(terms[0].episode_diversity, 2)
        self.assertEqual(explicit, ())
        self.assertEqual(term_lookup.status, "explicit_glossary")
        self.assertIsNone(term_lookup.auto_target)
        self.assertEqual(stats.term_observation_count, MAX_TERM_OBSERVATIONS_PER_EPISODE + 1)

    def test_conflicting_term_candidate_is_not_returned(self) -> None:
        evidences = [
            _evidence("episode-01", "manifest-01", [_block("1", "アリス", "爱丽丝")]),
            _evidence("episode-02", "manifest-02", [_block("2", "アリス", "爱丽丝")]),
            _evidence("episode-03", "manifest-03", [_block("3", "アリス", "艾莉丝")]),
        ]
        with TranslationMemoryStore(self.database) as store:
            store.learn_batch(self.scope, evidences)
            lookup = store.lookup_term_batch(self.scope, ["アリス"])[0]
            eligible = store.eligible_term_candidates(self.scope)
            stats = store.diagnostics(self.scope)

        self.assertEqual(lookup.status, "conflict")
        self.assertEqual(eligible, ())
        self.assertEqual(stats.conflict_term_count, 1)

    def test_explicit_glossary_blocks_incompatible_exact_memory_output(self) -> None:
        evidences = [
            _evidence("episode-01", "manifest-01", [_block("1", "アリスが来た", "爱丽丝来了")]),
            _evidence("episode-02", "manifest-02", [_block("2", "アリスが来た", "爱丽丝来了")]),
        ]
        with TranslationMemoryStore(self.database) as store:
            store.learn_batch(self.scope, evidences)
            guarded = _lookup(
                store,
                self.scope,
                ["アリスが来た"],
                explicit_series_glossary={"アリス": "愛麗絲"},
            )[0]
            compatible = _lookup(
                store,
                self.scope,
                ["アリスが来た"],
                explicit_series_glossary={"アリス": "爱丽丝"},
            )[0]

        self.assertEqual(guarded.status, "explicit_glossary_conflict")
        self.assertIsNone(guarded.auto_target)
        self.assertEqual(compatible.status, "auto_apply")

    def test_audit_trail_exposes_manifest_and_raw_text_hashes(self) -> None:
        evidence = _evidence(
            "episode-01",
            "manifest-01",
            [_block("cue-1", "おやすみ", "晚安")],
        )
        with TranslationMemoryStore(self.database) as store:
            store.learn_episode(self.scope, evidence)
            audit = store.audit_source(self.scope, "おやすみ", DEFAULT_CONTEXT)

        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].manifest_identity, "manifest-01")
        self.assertEqual(audit[0].source_manifest_hash, evidence.source_manifest_hash)
        self.assertEqual(audit[0].target_manifest_hash, evidence.target_manifest_hash)
        self.assertEqual(audit[0].source_text_hash, sha256_text("おやすみ"))
        self.assertEqual(audit[0].target_text_hash, sha256_text("晚安"))
        self.assertEqual(audit[0].block_identity, "cue-1")
        self.assertEqual(audit[0].context_key, DEFAULT_CONTEXT)


if __name__ == "__main__":
    unittest.main()
