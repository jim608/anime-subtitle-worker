from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from safe_files import sha256_file
from scan_state import ai_delivery_identity
from srt_utils import SrtBlock, write_srt
from translation_memory import MemoryScope, TranslationMemoryStore
from translation_memory_outbox import (
    TranslationMemoryOutboxNotReplayReady,
    record_translation_memory_outbox_intent,
)
from translation_memory_replay import (
    TranslationMemoryReplayError,
    replay_translation_memory_outbox_intent,
)


def _block(index: int, text: str, second: int) -> SrtBlock:
    return SrtBlock(
        index=index,
        timing=f"00:00:{second:02d},000 --> 00:00:{second + 1:02d},000",
        text=[text],
    )


class TranslationMemoryReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.outbox_root = self.root / "tm-outbox"
        self.database = self.root / "translation-memory.sqlite3"
        self.manifest = self.root / "episode.output-manifest.json"
        self.source = self.root / "episode.ja.srt"
        self.target = self.root / "episode.zh-CN.srt"
        self.video = self.root / "episode.mkv"
        self.video.write_bytes(b"real-media-revision")
        self.published_outputs = tuple(
            self.root / f"episode.{language}.ass"
            for language in ("ja", "zh-CN", "zh-TW")
        )
        for output, content in zip(
            self.published_outputs,
            ("japanese", "simplified", "traditional"),
            strict=True,
        ):
            output.write_text(content, encoding="utf-8")
        self.scope = MemoryScope("series-a", "policy-v5")
        self.split_digest = hashlib.sha256(b"replay-split-v2").hexdigest()
        write_srt(
            self.source,
            [
                _block(1, "おはよう", 1),
                _block(3, "ありがとう", 3),
                _block(7, "またね", 5),
            ],
        )
        write_srt(
            self.target,
            [
                _block(1, "早安", 1),
                _block(3, "謝謝", 3),
                _block(7, "再見", 5),
            ],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record(
        self,
        *,
        origins: tuple[int, ...] = (3,),
        policy_revision: str = "policy-v5",
    ):
        source_hash = sha256_file(self.source)
        target_hash = sha256_file(self.target)
        lineage_mode = "tm_split" if origins else "no_hits"
        video_stat = self.video.stat()
        identity = ai_delivery_identity(
            self.video,
            media_size=video_stat.st_size,
            media_mtime_ns=video_stat.st_mtime_ns,
            policy_revision=policy_revision,
        )
        output_entries = []
        for language, output in zip(
            ("ja", "zh-CN", "zh-TW"),
            self.published_outputs,
            strict=True,
        ):
            stat = output.stat()
            output_entries.append(
                {
                    "language": language,
                    "path": str(output),
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": sha256_file(output),
                }
            )
        payload = {
            "schema_version": 2,
            "video": str(self.video),
            "media": {
                key: identity[key]
                for key in (
                    "canonical_path",
                    "media_fingerprint",
                    "media_size",
                    "media_mtime_ns",
                )
            },
            "delivery": {
                "contract": "ai-delivery-v1",
                "obligation_id": identity["obligation_id"],
                "policy_revision": policy_revision,
            },
            "quality_gate": {"passed": True},
            "publication_kind": "translated_trilingual",
            "publication": {
                "contract": "ai-publication-semantics-v2",
                "kind": "translated_trilingual",
                "output_languages": ["ja", "zh-CN", "zh-TW"],
            },
            "outputs": output_entries,
            "provenance": {
                "translation_memory": {
                    "contract": "translation-memory-lineage-v2",
                    "mode": lineage_mode,
                    "split_decision_digest": self.split_digest,
                    "tm_origin_indexes": list(origins),
                    "source_srt_sha256": source_hash,
                    "target_srt_sha256": target_hash,
                }
            },
        }
        self.manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return record_translation_memory_outbox_intent(
            self.outbox_root,
            manifest_path=self.manifest,
            manifest_sha256=sha256_file(self.manifest),
            video_identity=identity["obligation_id"],
            scope=self.scope,
            episode_id="episode-01",
            source_srt_path=self.source,
            source_srt_sha256=source_hash,
            target_srt_path=self.target,
            target_srt_sha256=target_hash,
            tm_origin_indexes=origins,
            translation_lineage_mode=lineage_mode,
            split_decision_digest=self.split_digest,
            created_at="2026-08-13T06:30:00Z",
        )

    def test_learned_replay_uses_obligation_identity_excludes_tm_origins_and_acks(self) -> None:
        recorded = self._record()

        result = replay_translation_memory_outbox_intent(
            recorded.path,
            database_path=self.database,
        )

        self.assertEqual(result.status, "learned")
        expected_identity = ai_delivery_identity(
            self.video,
            media_size=self.video.stat().st_size,
            media_mtime_ns=self.video.stat().st_mtime_ns,
            policy_revision="policy-v5",
        )["obligation_id"]
        self.assertEqual(result.manifest_identity, expected_identity)
        self.assertEqual(result.inserted_blocks, 2)
        self.assertTrue(result.acknowledged)
        self.assertFalse(recorded.path.exists())
        with TranslationMemoryStore(self.database, readonly=True) as store:
            stats = store.diagnostics(self.scope)
            stored = store._require_connection().execute(
                "SELECT manifest_identity, source_text FROM tm_manifest "
                "JOIN tm_observation ON tm_observation.manifest_id = tm_manifest.id "
                "ORDER BY source_text"
            ).fetchall()
        self.assertEqual(stats.manifest_count, 1)
        self.assertEqual(stats.observation_count, 2)
        self.assertEqual({row["source_text"] for row in stored}, {"おはよう", "またね"})
        self.assertEqual({row["manifest_identity"] for row in stored}, {expected_identity})

    def test_recreated_same_intent_replays_idempotently_and_acks(self) -> None:
        first = self._record()
        learned = replay_translation_memory_outbox_intent(
            first.path,
            database_path=self.database,
        )
        self.assertEqual(learned.status, "learned")

        repeated = self._record()
        result = replay_translation_memory_outbox_intent(
            repeated.path,
            database_path=self.database,
        )

        self.assertEqual(result.status, "idempotent")
        self.assertEqual(result.inserted_blocks, 0)
        self.assertTrue(result.acknowledged)
        self.assertFalse(repeated.path.exists())
        with TranslationMemoryStore(self.database, readonly=True) as store:
            stats = store.diagnostics(self.scope)
        self.assertEqual((stats.manifest_count, stats.observation_count), (1, 2))

    def test_all_tm_hit_intent_is_auditable_noop_without_opening_database(self) -> None:
        recorded = self._record(origins=(1, 3, 7))

        result = replay_translation_memory_outbox_intent(
            recorded.path,
            database_path=self.database,
        )

        self.assertEqual(result.status, "no_new_blocks")
        self.assertEqual(result.inserted_blocks, 0)
        self.assertIsNone(result.manifest_identity)
        self.assertTrue(result.acknowledged)
        self.assertFalse(recorded.path.exists())
        self.assertFalse(self.database.exists())

    def test_database_failure_rolls_back_and_retains_outbox(self) -> None:
        recorded = self._record()
        with TranslationMemoryStore(self.database) as store:
            store._require_connection().execute(
                """
                CREATE TRIGGER reject_replay_observation
                BEFORE INSERT ON tm_observation
                BEGIN
                    SELECT RAISE(ABORT, 'injected replay database failure');
                END
                """
            )

        with self.assertRaisesRegex(
            TranslationMemoryReplayError,
            "database_learn_failed.*injected replay database failure",
        ):
            replay_translation_memory_outbox_intent(
                recorded.path,
                database_path=self.database,
            )

        self.assertTrue(recorded.path.is_file())
        with TranslationMemoryStore(self.database, readonly=True) as store:
            stats = store.diagnostics(self.scope)
        self.assertEqual((stats.manifest_count, stats.observation_count), (0, 0))

    def test_snapshot_tamper_fails_closed_and_retains_outbox(self) -> None:
        recorded = self._record()
        snapshot = Path(recorded.intent.source_snapshot_path)
        snapshot.write_bytes(snapshot.read_bytes() + b"\nTAMPER")

        with self.assertRaisesRegex(
            TranslationMemoryReplayError,
            "outbox_not_replay_ready.*artifact_hash_mismatch",
        ):
            replay_translation_memory_outbox_intent(
                recorded.path,
                database_path=self.database,
            )

        self.assertTrue(recorded.path.is_file())
        self.assertFalse(self.database.exists())

    def test_missing_published_ass_fails_closed_and_retains_outbox(self) -> None:
        recorded = self._record()
        self.published_outputs[-1].unlink()

        with self.assertRaisesRegex(
            TranslationMemoryReplayError,
            "outbox_not_replay_ready.*manifest_output_missing",
        ):
            replay_translation_memory_outbox_intent(
                recorded.path,
                database_path=self.database,
            )

        self.assertTrue(recorded.path.is_file())
        self.assertFalse(self.database.exists())

    def test_manifest_policy_scope_mismatch_is_rejected_before_record(self) -> None:
        with self.assertRaisesRegex(
            TranslationMemoryOutboxNotReplayReady,
            "manifest_not_strict",
        ):
            self._record(policy_revision="different-policy")

        self.assertEqual(list(self.outbox_root.glob("*.json")), [])
        self.assertFalse(self.database.exists())

    def test_current_media_replacement_rejects_normal_and_all_hit_replay(self) -> None:
        for origins in ((3,), (1, 3, 7)):
            with self.subTest(origins=origins):
                recorded = self._record(origins=origins)
                self.video.write_bytes(b"different-media-revision-with-new-size")

                with self.assertRaisesRegex(
                    TranslationMemoryReplayError,
                    "outbox_not_replay_ready.*manifest_media_identity_mismatch",
                ):
                    replay_translation_memory_outbox_intent(
                        recorded.path,
                        database_path=self.database,
                    )

                self.assertTrue(recorded.path.is_file())
                self.assertFalse(self.database.exists())
                recorded.path.unlink()
                self.video.write_bytes(b"real-media-revision")


if __name__ == "__main__":
    unittest.main()
