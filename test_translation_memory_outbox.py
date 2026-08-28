from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from safe_files import sha256_file
from scan_state import ai_delivery_identity
from srt_utils import SrtBlock, write_srt
from translation_memory import MemoryScope
from translation_memory_outbox import (
    TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION,
    TranslationMemoryLearnConfirmation,
    TranslationMemoryOutboxAcknowledgementError,
    TranslationMemoryOutboxCollision,
    TranslationMemoryOutboxCorrupt,
    TranslationMemoryOutboxError,
    TranslationMemoryOutboxNotReplayReady,
    acknowledge_translation_memory_outbox_intent,
    load_replay_ready_translation_memory_outbox_intent,
    load_translation_memory_outbox_intent,
    record_translation_memory_outbox_intent,
)


def _block(index: int, text: str, second: int) -> SrtBlock:
    return SrtBlock(
        index,
        f"00:00:{second:02d},000 --> 00:00:{second + 1:02d},000",
        [text],
    )


class TranslationMemoryOutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.outbox = self.root / "tm-outbox"
        self.manifest = self.root / "episode.output-manifest.json"
        self.source = self.root / "episode.ja.srt"
        self.target = self.root / "episode.zh-CN.srt"
        self.video = self.root / "episode.mkv"
        self.video.write_bytes(b"real-media-revision")
        video_stat = self.video.stat()
        self.delivery_identity = ai_delivery_identity(
            self.video,
            media_size=video_stat.st_size,
            media_mtime_ns=video_stat.st_mtime_ns,
            policy_revision="policy-v5",
        )
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
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "video": str(self.video),
                    "media": {
                        key: self.delivery_identity[key]
                        for key in (
                            "canonical_path",
                            "media_fingerprint",
                            "media_size",
                            "media_mtime_ns",
                        )
                    },
                    "delivery": {
                        "contract": "ai-delivery-v1",
                        "obligation_id": self.delivery_identity["obligation_id"],
                        "policy_revision": "policy-v5",
                    },
                    "quality_gate": {"passed": True},
                    "publication_contract": "ai-publication-semantics-v2",
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
                            "mode": "tm_split",
                            "split_decision_digest": hashlib.sha256(
                                b"tm-split-decision-v1"
                            ).hexdigest(),
                            "tm_origin_indexes": [3],
                            "source_srt_sha256": "pending",
                            "target_srt_sha256": "pending",
                        }
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
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
        manifest_payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        lineage = manifest_payload["provenance"]["translation_memory"]
        lineage["source_srt_sha256"] = sha256_file(self.source)
        lineage["target_srt_sha256"] = sha256_file(self.target)
        self.manifest.write_text(
            json.dumps(manifest_payload, sort_keys=True),
            encoding="utf-8",
        )
        self.scope = MemoryScope("series-a", "policy-v5")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record(
        self,
        *,
        origins: tuple[int, ...] = (3,),
        lineage_mode: str | None = None,
        created_at: str = "2026-08-13T06:30:00Z",
        **overrides: object,
    ):
        values: dict[str, object] = {
            "manifest_path": self.manifest,
            "manifest_sha256": sha256_file(self.manifest),
            "video_identity": self.delivery_identity["obligation_id"],
            "scope": self.scope,
            "episode_id": "episode-01",
            "source_srt_path": self.source,
            "source_srt_sha256": sha256_file(self.source),
            "target_srt_path": self.target,
            "target_srt_sha256": sha256_file(self.target),
            "tm_origin_indexes": origins,
            "translation_lineage_mode": lineage_mode or ("tm_split" if origins else "no_hits"),
            "split_decision_digest": hashlib.sha256(b"tm-split-decision-v1").hexdigest(),
            "created_at": created_at,
        }
        manifest_payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        lineage = manifest_payload["provenance"]["translation_memory"]
        lineage["mode"] = values["translation_lineage_mode"]
        lineage["tm_origin_indexes"] = sorted(set(origins))
        lineage["split_decision_digest"] = values["split_decision_digest"]
        lineage["source_srt_sha256"] = values["source_srt_sha256"]
        lineage["target_srt_sha256"] = values["target_srt_sha256"]
        self.manifest.write_text(json.dumps(manifest_payload, sort_keys=True), encoding="utf-8")
        values["manifest_sha256"] = sha256_file(self.manifest)
        values.update(overrides)
        return record_translation_memory_outbox_intent(self.outbox, **values)

    def _confirmation(self, intent, **overrides: object) -> TranslationMemoryLearnConfirmation:
        values: dict[str, object] = {
            "intent_sha256": intent.intent_sha256,
            "manifest_sha256": intent.manifest_sha256,
            "status": "learned",
            "idempotent_learn_confirmed": True,
        }
        values.update(overrides)
        return TranslationMemoryLearnConfirmation(**values)

    @staticmethod
    def _resign(envelope: dict[str, object]) -> None:
        binding = {
            "schema_version": envelope["schema_version"],
            "intent": envelope["intent"],
        }
        encoded = json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        envelope["intent_sha256"] = hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _write_envelope(path: Path, envelope: dict[str, object]) -> None:
        path.write_text(
            json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def test_record_load_and_replay_ready_bind_every_required_field(self) -> None:
        recorded = self._record(origins=(7, 3, 3))

        self.assertTrue(recorded.created)
        self.assertEqual(recorded.path.parent, self.outbox.resolve())
        loaded = load_translation_memory_outbox_intent(recorded.path)
        self.assertEqual(loaded, recorded.intent)
        self.assertEqual(loaded.manifest_path, os.path.normcase(os.path.abspath(self.manifest)))
        self.assertEqual(loaded.manifest_sha256, sha256_file(self.manifest))
        self.assertEqual(loaded.video_identity, self.delivery_identity["obligation_id"])
        self.assertEqual(loaded.scope, self.scope)
        self.assertEqual(loaded.episode_id, "episode-01")
        self.assertEqual(loaded.source_srt_sha256, sha256_file(self.source))
        self.assertEqual(loaded.target_srt_sha256, sha256_file(self.target))
        self.assertEqual(loaded.source_block_indexes, (1, 3, 7))
        self.assertEqual(loaded.tm_origin_indexes, (3, 7))
        self.assertEqual(loaded.translation_lineage_mode, "tm_split")
        self.assertEqual(
            loaded.split_decision_digest,
            hashlib.sha256(b"tm-split-decision-v1").hexdigest(),
        )
        self.assertEqual(sha256_file(loaded.source_snapshot_path), loaded.source_srt_sha256)
        self.assertEqual(sha256_file(loaded.target_snapshot_path), loaded.target_srt_sha256)
        self.assertEqual(loaded.created_at, "2026-08-13T06:30:00.000000Z")
        self.assertRegex(loaded.intent_id, r"^[0-9a-f]{64}$")
        self.assertRegex(loaded.intent_sha256, r"^[0-9a-f]{64}$")

        replay = load_replay_ready_translation_memory_outbox_intent(recorded.path)
        self.assertEqual(replay.intent, loaded)
        self.assertEqual(replay.learnable_indexes, (1,))
        self.assertFalse(replay.is_auditable_noop)

    def test_retry_is_idempotent_and_preserves_first_created_at(self) -> None:
        first = self._record(created_at="2026-08-13T06:30:00Z")
        second = self._record(created_at="2026-08-13T06:35:00Z")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.path, second.path)
        self.assertEqual(first.intent, second.intent)
        self.assertEqual(second.intent.created_at, "2026-08-13T06:30:00.000000Z")

    def test_same_publication_identity_with_different_origin_evidence_is_collision(self) -> None:
        self._record(origins=(3,))

        with self.assertRaises(TranslationMemoryOutboxCollision):
            self._record(origins=(7,))

        self.assertEqual(len(list(self.outbox.glob("*.json"))), 1)

    def test_origin_index_tampering_breaks_hash_and_quarantines(self) -> None:
        recorded = self._record()
        envelope = json.loads(recorded.path.read_text(encoding="utf-8"))
        envelope["intent"]["tm_origin_indexes"] = [1, 3]
        self._write_envelope(recorded.path, envelope)

        with self.assertRaises(TranslationMemoryOutboxCorrupt) as raised:
            load_translation_memory_outbox_intent(recorded.path)

        self.assertFalse(recorded.path.exists())
        self.assertIsNotNone(raised.exception.quarantine_path)
        self.assertTrue(raised.exception.quarantine_path.is_file())

    def test_malformed_json_and_duplicate_keys_are_quarantined(self) -> None:
        malformed = self.outbox / "malformed.json"
        self.outbox.mkdir(parents=True)
        malformed.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(TranslationMemoryOutboxCorrupt):
            load_translation_memory_outbox_intent(malformed)
        self.assertFalse(malformed.exists())

        recorded = self._record()
        raw = recorded.path.read_text(encoding="utf-8")
        raw = raw.replace(
            '"episode_id":"episode-01"',
            '"episode_id":"episode-01","episode_id":"episode-01"',
            1,
        )
        recorded.path.write_text(raw, encoding="utf-8")
        with self.assertRaises(TranslationMemoryOutboxCorrupt):
            load_translation_memory_outbox_intent(recorded.path)
        self.assertFalse(recorded.path.exists())

    def test_unknown_field_is_rejected_even_with_a_valid_recomputed_hash(self) -> None:
        recorded = self._record()
        envelope = json.loads(recorded.path.read_text(encoding="utf-8"))
        envelope["intent"]["unexpected"] = "poison"
        self._resign(envelope)
        self._write_envelope(recorded.path, envelope)

        with self.assertRaises(TranslationMemoryOutboxCorrupt) as raised:
            load_translation_memory_outbox_intent(recorded.path)

        self.assertIn("fields mismatch", str(raised.exception))

    def test_noncanonical_timestamp_and_indexes_are_corrupt(self) -> None:
        transformations = (
            lambda intent: intent.__setitem__("created_at", "2026-08-13T06:30:00+00:00"),
            lambda intent: intent.__setitem__("tm_origin_indexes", [7, 3]),
            lambda intent: intent.__setitem__("source_block_indexes", [1, 3, 3, 7]),
        )
        for transform in transformations:
            with self.subTest(transform=transform):
                recorded = self._record()
                envelope = json.loads(recorded.path.read_text(encoding="utf-8"))
                transform(envelope["intent"])
                self._resign(envelope)
                self._write_envelope(recorded.path, envelope)
                with self.assertRaises(TranslationMemoryOutboxCorrupt):
                    load_translation_memory_outbox_intent(recorded.path)
                # Each corruption is quarantined, so the next subtest can
                # safely recreate the deterministic path.
                self.assertFalse(recorded.path.exists())

    def test_invalid_scope_is_corrupt_and_quarantined(self) -> None:
        recorded = self._record()
        envelope = json.loads(recorded.path.read_text(encoding="utf-8"))
        envelope["intent"]["scope"]["target_language"] = "zh-TW"
        self._resign(envelope)
        self._write_envelope(recorded.path, envelope)

        with self.assertRaises(TranslationMemoryOutboxCorrupt) as raised:
            load_translation_memory_outbox_intent(recorded.path)

        self.assertIn("unsupported_target_language", str(raised.exception))
        self.assertFalse(recorded.path.exists())

    def test_corrupt_quarantine_can_be_disabled_for_forensics(self) -> None:
        recorded = self._record()
        recorded.path.write_text("broken", encoding="utf-8")

        with self.assertRaises(TranslationMemoryOutboxCorrupt) as raised:
            load_translation_memory_outbox_intent(recorded.path, quarantine_corrupt=False)

        self.assertIsNone(raised.exception.quarantine_path)
        self.assertTrue(recorded.path.exists())

    def test_changed_or_missing_snapshot_is_not_replay_ready_but_stays_pending(self) -> None:
        recorded = self._record()
        target_snapshot = Path(recorded.intent.target_snapshot_path)
        target_snapshot.write_text("changed", encoding="utf-8")

        with self.assertRaises(TranslationMemoryOutboxNotReplayReady) as raised:
            load_replay_ready_translation_memory_outbox_intent(recorded.path)

        self.assertEqual(raised.exception.code, "artifact_hash_mismatch")
        self.assertTrue(recorded.path.is_file())

        target_snapshot.unlink()
        with self.assertRaises(TranslationMemoryOutboxNotReplayReady):
            load_replay_ready_translation_memory_outbox_intent(recorded.path)
        self.assertTrue(recorded.path.is_file())
        self.assertFalse((self.outbox / "quarantine").exists())

    def test_replay_survives_intermediate_srt_cleanup_via_atomic_snapshots(self) -> None:
        recorded = self._record()
        self.source.unlink()
        self.target.unlink()

        replay = load_replay_ready_translation_memory_outbox_intent(recorded.path)

        self.assertEqual(replay.learnable_indexes, (1, 7))
        self.assertTrue(Path(replay.intent.source_snapshot_path).is_file())
        self.assertTrue(Path(replay.intent.target_snapshot_path).is_file())

    def test_replay_requires_strict_manifest_and_no_publication_marker(self) -> None:
        recorded = self._record()
        marker = self.manifest.with_suffix(".publishing")
        marker.write_text("in progress", encoding="utf-8")
        with self.assertRaisesRegex(
            TranslationMemoryOutboxNotReplayReady,
            "publication_in_progress",
        ):
            load_replay_ready_translation_memory_outbox_intent(recorded.path)
        self.assertTrue(recorded.path.exists())

        marker.unlink()
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["publication_kind"] = "source_language"
        self.manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        # Re-signing the intent would only prove the new manifest bytes; its
        # semantics are independently rejected as not trainable evidence.
        envelope = json.loads(recorded.path.read_text(encoding="utf-8"))
        envelope["intent"]["manifest_sha256"] = sha256_file(self.manifest)
        self._resign(envelope)
        self._write_envelope(recorded.path, envelope)
        with self.assertRaisesRegex(
            TranslationMemoryOutboxNotReplayReady,
            "manifest_not_strict",
        ):
            # Even a correctly re-hashed envelope cannot make a source-only
            # manifest acceptable training evidence.
            load_replay_ready_translation_memory_outbox_intent(recorded.path)

    def test_record_requires_explicit_crash_window_opt_in_for_publication_marker(self) -> None:
        marker = self.manifest.with_suffix(".publishing")
        marker.write_text("in progress", encoding="utf-8")

        with self.assertRaisesRegex(
            TranslationMemoryOutboxNotReplayReady,
            "publication_in_progress",
        ):
            self._record()
        self.assertEqual(list(self.outbox.glob("*.json")), [])

        recorded = self._record(allow_publication_in_progress=True)
        self.assertTrue(recorded.path.is_file())
        self.assertTrue(Path(recorded.intent.source_snapshot_path).is_file())
        self.assertTrue(Path(recorded.intent.target_snapshot_path).is_file())

        # A crash here leaves durable evidence but replay remains fail-closed
        # until startup recovery verifies the publication and removes marker.
        with self.assertRaisesRegex(
            TranslationMemoryOutboxNotReplayReady,
            "publication_in_progress",
        ):
            load_replay_ready_translation_memory_outbox_intent(recorded.path)
        self.assertTrue(recorded.path.exists())

        marker.unlink()
        replay = load_replay_ready_translation_memory_outbox_intent(recorded.path)
        self.assertEqual(replay.intent, recorded.intent)

    def test_record_rejects_non_strict_manifest_without_visible_intent(self) -> None:
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["publication"]["kind"] = "source_language"
        self.manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        with self.assertRaisesRegex(
            TranslationMemoryOutboxNotReplayReady,
            "manifest_not_strict",
        ):
            self._record(manifest_sha256=sha256_file(self.manifest))

        self.assertEqual(list(self.outbox.glob("*.json")), [])

    def test_record_rejects_wrong_artifact_hash_without_writing(self) -> None:
        with self.assertRaises(TranslationMemoryOutboxNotReplayReady):
            self._record(target_srt_sha256="0" * 64)

        self.assertFalse(self.outbox.exists())

    def test_record_rejects_unaligned_srt_and_unknown_origin(self) -> None:
        write_srt(
            self.target,
            [
                _block(1, "早安", 1),
                _block(3, "謝謝", 3),
                _block(7, "再見", 8),
            ],
        )
        with self.assertRaises(TranslationMemoryOutboxNotReplayReady):
            self._record(target_srt_sha256=sha256_file(self.target))
        self.assertFalse(self.outbox.exists())

        write_srt(
            self.target,
            [
                _block(1, "早安", 1),
                _block(3, "謝謝", 3),
                _block(7, "再見", 5),
            ],
        )
        with self.assertRaisesRegex(TranslationMemoryOutboxError, "tm_origin_index_missing"):
            self._record(origins=(99,))
        self.assertFalse(self.outbox.exists())

    def test_nonmonotonic_but_aligned_srt_indexes_preserve_source_order(self) -> None:
        source_blocks = [
            _block(7, "またね", 1),
            _block(1, "おはよう", 3),
            _block(3, "ありがとう", 5),
        ]
        target_blocks = [
            _block(7, "再見", 1),
            _block(1, "早安", 3),
            _block(3, "謝謝", 5),
        ]
        write_srt(self.source, source_blocks)
        write_srt(self.target, target_blocks)

        recorded = self._record(origins=(1,))
        replay = load_replay_ready_translation_memory_outbox_intent(recorded.path)

        self.assertEqual(replay.intent.source_block_indexes, (7, 1, 3))
        self.assertEqual(replay.intent.tm_origin_indexes, (1,))
        self.assertEqual(replay.learnable_indexes, (7, 3))

    def test_atomic_write_failure_never_leaves_a_visible_intent(self) -> None:
        with patch(
            "translation_memory_outbox.atomic_write_text",
            side_effect=OSError("injected write failure"),
        ):
            with self.assertRaisesRegex(TranslationMemoryOutboxError, "outbox_write_failed"):
                self._record()

        self.assertEqual(list(self.outbox.glob("*.json")), [])

    def test_ack_requires_exact_positive_idempotent_confirmation(self) -> None:
        bad_confirmations = (
            {"idempotent_learn_confirmed": False},
            {"intent_sha256": "0" * 64},
            {"manifest_sha256": "0" * 64},
            {"status": "maybe"},
        )
        for override in bad_confirmations:
            with self.subTest(override=override):
                recorded = self._record()
                with self.assertRaises(TranslationMemoryOutboxAcknowledgementError):
                    acknowledge_translation_memory_outbox_intent(
                        recorded.path,
                        self._confirmation(recorded.intent, **override),
                    )
                self.assertTrue(recorded.path.is_file())

    def test_ack_after_learned_or_idempotent_result_durably_deletes(self) -> None:
        for status in ("learned", "idempotent"):
            with self.subTest(status=status):
                recorded = self._record()
                removed = acknowledge_translation_memory_outbox_intent(
                    recorded.path,
                    self._confirmation(recorded.intent, status=status),
                )
                self.assertTrue(removed)
                self.assertFalse(recorded.path.exists())
                self.assertFalse(
                    acknowledge_translation_memory_outbox_intent(
                        recorded.path,
                        self._confirmation(recorded.intent, status=status),
                    )
                )

    def test_ack_does_not_depend_on_artifacts_after_durable_learn(self) -> None:
        recorded = self._record()
        self.source.unlink()
        self.target.unlink()
        self.manifest.unlink()

        self.assertTrue(
            acknowledge_translation_memory_outbox_intent(
                recorded.path,
                self._confirmation(recorded.intent),
            )
        )

    def test_all_tm_origin_is_auditable_noop_and_cannot_be_relearned(self) -> None:
        recorded = self._record(origins=(1, 3, 7))
        replay = load_replay_ready_translation_memory_outbox_intent(recorded.path)
        self.assertTrue(replay.is_auditable_noop)
        self.assertEqual(replay.learnable_indexes, ())

        with self.assertRaisesRegex(
            TranslationMemoryOutboxAcknowledgementError,
            "tm_origin_relearn_rejected",
        ):
            acknowledge_translation_memory_outbox_intent(
                recorded.path,
                self._confirmation(recorded.intent, status="learned"),
            )
        self.assertTrue(recorded.path.exists())

        self.assertTrue(
            acknowledge_translation_memory_outbox_intent(
                recorded.path,
                self._confirmation(recorded.intent, status="no_new_blocks"),
            )
        )

    def test_partial_tm_origin_cannot_be_falsely_acknowledged_as_noop(self) -> None:
        recorded = self._record(origins=(3,))

        with self.assertRaisesRegex(
            TranslationMemoryOutboxAcknowledgementError,
            "false_noop_confirmation",
        ):
            acknowledge_translation_memory_outbox_intent(
                recorded.path,
                self._confirmation(recorded.intent, status="no_new_blocks"),
            )

        self.assertTrue(recorded.path.exists())

    def test_strict_zero_origin_lineage_is_unambiguous_and_learnable(self) -> None:
        for mode in ("no_hits", "lookup_fallback", "tm_disabled"):
            with self.subTest(mode=mode):
                recorded = self._record(origins=(), lineage_mode=mode)
                replay = load_replay_ready_translation_memory_outbox_intent(recorded.path)
                self.assertEqual(replay.intent.translation_lineage_mode, mode)
                self.assertEqual(replay.intent.tm_origin_indexes, ())
                self.assertEqual(replay.learnable_indexes, (1, 3, 7))
                self.assertTrue(
                    acknowledge_translation_memory_outbox_intent(
                        recorded.path,
                        self._confirmation(recorded.intent),
                    )
                )

    def test_lineage_mode_and_origin_set_must_be_consistent(self) -> None:
        poisons = (
            {"origins": (), "lineage_mode": "tm_split"},
            {"origins": (3,), "lineage_mode": "no_hits"},
            {"origins": (), "lineage_mode": "unknown"},
        )
        for poison in poisons:
            with self.subTest(poison=poison):
                with self.assertRaises(TranslationMemoryOutboxError):
                    self._record(**poison)

    def test_noncanonical_or_unsupported_inputs_are_rejected_before_record(self) -> None:
        poisons = (
            {"created_at": "2026-08-13T06:30:00"},
            {"video_identity": "   "},
            {"scope": MemoryScope("series-a", "policy-v5", target_language="zh-TW")},
            {"origins": (True,)},
            {"manifest_sha256": "not-a-hash"},
        )
        for poison in poisons:
            with self.subTest(poison=poison):
                with self.assertRaises(TranslationMemoryOutboxError):
                    origins = poison.pop("origins", (3,))
                    self._record(origins=origins, **poison)
        self.assertFalse(self.outbox.exists())

    def test_schema_version_is_hash_bound_and_strict(self) -> None:
        recorded = self._record()
        envelope = json.loads(recorded.path.read_text(encoding="utf-8"))
        self.assertEqual(
            envelope["schema_version"],
            TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION,
        )
        envelope["schema_version"] = 2
        self._resign(envelope)
        self._write_envelope(recorded.path, envelope)

        with self.assertRaises(TranslationMemoryOutboxCorrupt):
            load_translation_memory_outbox_intent(recorded.path)


if __name__ == "__main__":
    unittest.main()
