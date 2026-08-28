from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from asr_review_checkpoint import (
    ASR_REVIEW_CHECKPOINT_MANIFEST_NAME,
    AsrReviewCheckpointError,
    create_asr_review_checkpoint,
    load_asr_review_checkpoint,
)
from safe_files import atomic_write_text, sha256_file


class AsrReviewCheckpointTest(unittest.TestCase):
    def test_concurrent_publishers_reuse_one_immutable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video, rejected, diagnostics, _evidence = self._inputs(root)

            def publish(_index: int):
                return create_asr_review_checkpoint(
                    root / "work",
                    target_path=video,
                    language="ja",
                    rejected_srt_path=rejected,
                    diagnostics_path=diagnostics,
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                checkpoints = list(executor.map(publish, range(8)))

            self.assertTrue(all(item == checkpoints[0] for item in checkpoints))
            self.assertEqual(
                list((root / "work").rglob(ASR_REVIEW_CHECKPOINT_MANIFEST_NAME)),
                [checkpoints[0].manifest_path],
            )
            self.assertEqual(
                list(checkpoints[0].manifest_path.parent.parent.glob(".stage-*")),
                [],
            )

    def test_v2_round_trip_is_normalized_hash_bound_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video, rejected, diagnostics, evidence = self._inputs(root)

            checkpoint = create_asr_review_checkpoint(
                root / "work",
                target_path=video,
                language="JA_jp",
                rejected_srt_path=rejected,
                diagnostics_path=diagnostics,
                review_ranges=[[12.0, 14.0], [8.0, 10.0], [9.5, 12.0]],
                repair_fingerprint=evidence["repair_fingerprint"],
                fingerprints=evidence["fingerprints"],
            )

            self.assertEqual(checkpoint.schema_version, 2)
            self.assertEqual(checkpoint.language, "ja-jp")
            self.assertEqual(checkpoint.review_ranges, ((8.0, 14.0),))
            self.assertEqual(
                checkpoint.manifest_sha256,
                sha256_file(checkpoint.manifest_path),
            )
            self.assertEqual(checkpoint.rejected_srt_path.read_bytes(), rejected.read_bytes())
            self.assertEqual(checkpoint.diagnostics_path.read_bytes(), diagnostics.read_bytes())

            loaded = load_asr_review_checkpoint(
                checkpoint.manifest_path,
                expected_manifest_sha256=checkpoint.manifest_sha256,
                expected_checkpoint_id=checkpoint.checkpoint_id,
                expected_target_path=video,
                expected_language="ja-JP",
                expected_review_ranges=[[8.0, 14.0]],
                expected_repair_fingerprint=evidence["repair_fingerprint"],
                expected_fingerprints=evidence["fingerprints"],
            )
            repeated = create_asr_review_checkpoint(
                root / "work",
                target_path=video,
                language="ja-jp",
                rejected_srt_path=rejected,
                diagnostics_path=diagnostics,
            )
            self.assertEqual(loaded, checkpoint)
            self.assertEqual(repeated, checkpoint)

    def test_loader_reads_strict_schema_v1_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video, rejected, diagnostics, evidence = self._inputs(root)
            current = create_asr_review_checkpoint(
                root / "work",
                target_path=video,
                language="ja",
                rejected_srt_path=rejected,
                diagnostics_path=diagnostics,
            )
            legacy_dir = root / "legacy" / current.checkpoint_id
            legacy_dir.mkdir(parents=True)
            legacy_srt = legacy_dir / "legacy-rejected.srt"
            legacy_diagnostics = legacy_dir / "legacy-diagnostics.json"
            legacy_srt.write_bytes(current.rejected_srt_path.read_bytes())
            legacy_diagnostics.write_bytes(current.diagnostics_path.read_bytes())
            legacy_manifest = legacy_dir / ASR_REVIEW_CHECKPOINT_MANIFEST_NAME
            atomic_write_text(
                legacy_manifest,
                json.dumps(
                    {
                        "schema_version": 1,
                        "checkpoint_id": current.checkpoint_id,
                        "created_at": current.created_at,
                        "target_path": str(current.target_path),
                        "language": current.language,
                        "review_ranges": [list(item) for item in current.review_ranges],
                        "repair_fingerprint": current.repair_fingerprint,
                        "fingerprints": current.fingerprints,
                        "rejected_srt": {
                            "path": legacy_srt.name,
                            "sha256": current.rejected_srt_sha256,
                            "size": legacy_srt.stat().st_size,
                        },
                        "diagnostics": {
                            "path": legacy_diagnostics.name,
                            "sha256": current.diagnostics_sha256,
                            "size": legacy_diagnostics.stat().st_size,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )

            loaded = load_asr_review_checkpoint(
                legacy_manifest,
                expected_checkpoint_id=current.checkpoint_id,
                expected_target_path=video,
                expected_repair_fingerprint=evidence["repair_fingerprint"],
            )

            self.assertEqual(loaded.schema_version, 1)
            self.assertEqual(loaded.rejected_srt_sha256, current.rejected_srt_sha256)
            self.assertEqual(loaded.diagnostics_sha256, current.diagnostics_sha256)

    def test_tampered_artifact_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video, rejected, diagnostics, _evidence = self._inputs(root)
            checkpoint = create_asr_review_checkpoint(
                root / "work",
                target_path=video,
                language="ja",
                rejected_srt_path=rejected,
                diagnostics_path=diagnostics,
            )
            checkpoint.rejected_srt_path.write_text("tampered", encoding="utf-8")

            with self.assertRaisesRegex(AsrReviewCheckpointError, "size|SHA-256"):
                load_asr_review_checkpoint(checkpoint.manifest_path)

    def test_diagnostics_evidence_mismatch_fails_closed_even_with_updated_file_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video, rejected, diagnostics, _evidence = self._inputs(root)
            checkpoint = create_asr_review_checkpoint(
                root / "work",
                target_path=video,
                language="ja",
                rejected_srt_path=rejected,
                diagnostics_path=diagnostics,
            )
            payload = json.loads(checkpoint.diagnostics_path.read_text(encoding="utf-8"))
            payload["repair_fingerprint"] = "f" * 64
            checkpoint.diagnostics_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest = json.loads(checkpoint.manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["diagnostics"]["sha256"] = sha256_file(
                checkpoint.diagnostics_path
            )
            manifest["artifacts"]["diagnostics"]["size"] = checkpoint.diagnostics_path.stat().st_size
            atomic_write_text(
                checkpoint.manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )

            with self.assertRaisesRegex(AsrReviewCheckpointError, "repair fingerprint"):
                load_asr_review_checkpoint(checkpoint.manifest_path)

    def test_manifest_path_escape_and_expected_manifest_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video, rejected, diagnostics, _evidence = self._inputs(root)
            checkpoint = create_asr_review_checkpoint(
                root / "work",
                target_path=video,
                language="ja",
                rejected_srt_path=rejected,
                diagnostics_path=diagnostics,
            )
            with self.assertRaisesRegex(AsrReviewCheckpointError, "manifest SHA-256"):
                load_asr_review_checkpoint(
                    checkpoint.manifest_path,
                    expected_manifest_sha256="0" * 64,
                )

            manifest = json.loads(checkpoint.manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["rejected_srt"]["file"] = "../outside.srt"
            atomic_write_text(
                checkpoint.manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            with self.assertRaisesRegex(AsrReviewCheckpointError, "unsafe"):
                load_asr_review_checkpoint(checkpoint.manifest_path)

    def test_create_rejects_diagnostics_not_bound_to_rejected_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video, rejected, diagnostics, _evidence = self._inputs(root)
            payload = json.loads(diagnostics.read_text(encoding="utf-8"))
            payload["srt_sha256"] = "0" * 64
            diagnostics.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(AsrReviewCheckpointError, "not bound"):
                create_asr_review_checkpoint(
                    root / "work",
                    target_path=video,
                    language="ja",
                    rejected_srt_path=rejected,
                    diagnostics_path=diagnostics,
                )
            self.assertEqual(list((root / "work").rglob(ASR_REVIEW_CHECKPOINT_MANIFEST_NAME)), [])

    @staticmethod
    def _inputs(
        root: Path,
    ) -> tuple[Path, Path, Path, dict[str, object]]:
        video = root / "anime" / "Example" / "Season 1" / "Example - S01E01.mkv"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"video")
        rejected = root / "Example.ja.srt"
        rejected.write_text(
            "1\n00:00:08,000 --> 00:00:14,000\n聞き取れない\n",
            encoding="utf-8",
        )
        repair_fingerprint = "9" * 64
        fingerprints = {
            "media_fingerprint": {"fingerprint": "1" * 64, "size": 5},
            "audio_fingerprint": {"fingerprint": "2" * 64, "size": 10},
            "audio_stream_fingerprint": {"fingerprint": "3" * 64, "index": 1},
            "cache_fingerprint": {"fingerprint": "4" * 64, "size": rejected.stat().st_size},
        }
        diagnostics = root / "Example.ja.srt.diagnostics.json"
        diagnostics.write_text(
            json.dumps(
                {
                    "status": "selective_retry_required",
                    "reason_code": "rescue_low_confidence",
                    "srt_sha256": sha256_file(rejected),
                    "review_ranges": [[12.0, 14.0], [8.0, 10.0], [9.5, 12.0]],
                    "repair_fingerprint": repair_fingerprint,
                    **fingerprints,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return video, rejected, diagnostics, {
            "repair_fingerprint": repair_fingerprint,
            "fingerprints": fingerprints,
        }


if __name__ == "__main__":
    unittest.main()
