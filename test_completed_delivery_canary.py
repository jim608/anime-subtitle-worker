from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import completed_delivery_canary as canary
from completed_delivery import CompletedDeliveryError, CompletedDeliveryResult


@dataclass(frozen=True)
class _ConfigStub:
    completed_delivery_enabled: bool = False
    completed_delivery_source_policy: str = "remove"


class CompletedDeliveryCanaryTest(unittest.TestCase):
    def test_load_config_enables_only_in_memory_and_revalidates(self) -> None:
        persisted = _ConfigStub()
        with (
            patch("config.load_config", return_value=persisted),
            patch("config._validate_config") as validate,
        ):
            active, persisted_enabled, persisted_source_policy = canary.load_canary_config(
                Path("config.yaml")
            )

        self.assertFalse(persisted_enabled)
        self.assertEqual(persisted_source_policy, "remove")
        self.assertFalse(persisted.completed_delivery_enabled)
        self.assertEqual(persisted.completed_delivery_source_policy, "remove")
        self.assertTrue(active.completed_delivery_enabled)
        self.assertEqual(active.completed_delivery_source_policy, "retain")
        validate.assert_called_once_with(active)

    def test_preview_is_read_only_and_bound_to_one_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input" / "Series" / "Episode.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"media")
            (root / "completed").mkdir()
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                input_path=root / "input",
                work_path=root / "work",
                completed_delivery_path=str(root / "completed"),
                completed_delivery_manifest_path=str(root / "receipts"),
            )

            with (
                patch.object(
                    canary,
                    "_terminal_queue_evidence",
                    return_value={"status": "succeeded", "source": "ai", "mtime_ns": source.stat().st_mtime_ns},
                ),
                patch.object(
                    canary,
                    "_strict_publication",
                    return_value={"tracks": [{"language": "zh-TW"}, {"language": "ja"}]},
                ),
                patch.object(canary, "output_manifest_path", return_value=manifest),
            ):
                payload = canary.inspect_completed_delivery_canary(source, config)

            self.assertTrue(payload["readonly"])
            self.assertTrue(payload["ready"])
            self.assertEqual(payload["target_count"], 1)
            self.assertEqual(payload["target"], str(source.resolve()))
            self.assertFalse(Path(payload["destination"]).exists())
            self.assertFalse(Path(payload["receipt"]).exists())
            self.assertFalse(Path(payload["recovery_marker"]).exists())

    def test_preview_reports_owned_recovery_marker_as_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input" / "Series" / "Episode.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"media")
            (root / "completed").mkdir()
            manifest = root / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            config = SimpleNamespace(
                input_path=root / "input",
                work_path=root / "work",
                completed_delivery_path=str(root / "completed"),
                completed_delivery_manifest_path=str(root / "receipts"),
            )
            marker = canary.completed_delivery_marker_path(source, config)
            marker.parent.mkdir(parents=True)
            marker.write_text('{"state":"muxing"}', encoding="utf-8")

            with (
                patch.object(
                    canary,
                    "_terminal_queue_evidence",
                    return_value={"status": "succeeded", "source": "ai", "mtime_ns": source.stat().st_mtime_ns},
                ),
                patch.object(
                    canary,
                    "_strict_publication",
                    return_value={"tracks": [{"language": "zh-TW"}]},
                ),
                patch.object(canary, "output_manifest_path", return_value=manifest),
            ):
                payload = canary.inspect_completed_delivery_canary(source, config)

            self.assertFalse(payload["ready"])
            self.assertTrue(payload["recovery_pending"])
            self.assertEqual(payload["reason_code"], "recovery_pending")
            self.assertEqual(marker.read_text(encoding="utf-8"), '{"state":"muxing"}')

    def test_nonterminal_queue_item_fails_before_publication_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "Episode.mkv"
            source.write_bytes(b"media")
            row = SimpleNamespace(
                path=str(source),
                status="running",
                source="ai",
                mtime_ns=source.stat().st_mtime_ns,
            )
            with patch("acceptance.planner.read_queue_candidates", return_value=[row]):
                with self.assertRaisesRegex(CompletedDeliveryError, "not terminal"):
                    canary._terminal_queue_evidence(source.resolve(), SimpleNamespace())

    def test_terminal_queue_item_with_stale_media_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "Episode.mkv"
            source.write_bytes(b"media")
            row = SimpleNamespace(
                path=str(source),
                status="succeeded",
                source="ai",
                mtime_ns=source.stat().st_mtime_ns - 1,
            )
            with patch("acceptance.planner.read_queue_candidates", return_value=[row]):
                with self.assertRaisesRegex(CompletedDeliveryError, "identity"):
                    canary._terminal_queue_evidence(source.resolve(), SimpleNamespace())

    def test_commit_preflights_then_delivers_only_the_requested_target(self) -> None:
        target = Path("/anime/Series/Episode.mkv")
        config = SimpleNamespace()
        result = CompletedDeliveryResult(
            destination="/completed/Series/Episode.mkv",
            receipt="/work/receipts/receipt.json",
            output_sha256="a" * 64,
            output_size=123,
            publication_manifest_sha256="b" * 64,
            recovered=False,
        )
        with (
            patch.object(
                canary,
                "inspect_completed_delivery_canary",
                return_value={
                    "ready": True,
                    "target": str(target),
                    "queue": {"status": "succeeded"},
                    "strict_publication": {"manifest_sha256": "b" * 64},
                },
            ) as inspect,
            patch.object(canary, "deliver_completed_mkv", return_value=result) as deliver,
            patch.object(canary, "validate_completed_delivery", return_value=True) as validate,
        ):
            payload = canary.commit_completed_delivery_canary(target, config)

        inspect.assert_called_once_with(target, config)
        deliver.assert_called_once_with(target.resolve(), config)
        validate.assert_called_once_with(target.resolve(), config, verify_streams=True)
        self.assertEqual(payload["target_count"], 1)
        self.assertTrue(payload["source_retained"])

    def test_commit_refuses_when_preview_is_not_ready(self) -> None:
        target = Path("/anime/Series/Episode.mkv")
        with (
            patch.object(
                canary,
                "inspect_completed_delivery_canary",
                return_value={"ready": False, "reason_code": "recovery_pending"},
            ),
            patch.object(canary, "deliver_completed_mkv") as deliver,
        ):
            with self.assertRaisesRegex(CompletedDeliveryError, "recovery_pending"):
                canary.commit_completed_delivery_canary(target, SimpleNamespace())

        deliver.assert_not_called()

    def test_commit_fails_closed_when_full_post_commit_validation_fails(self) -> None:
        target = Path("/anime/Series/Episode.mkv")
        result = CompletedDeliveryResult(
            destination="/completed/Series/Episode.mkv",
            receipt="/work/receipts/receipt.json",
            output_sha256="a" * 64,
            output_size=123,
            publication_manifest_sha256="b" * 64,
            recovered=False,
        )
        with (
            patch.object(
                canary,
                "inspect_completed_delivery_canary",
                return_value={
                    "ready": True,
                    "target": str(target),
                    "queue": {"status": "succeeded"},
                    "strict_publication": {"manifest_sha256": "b" * 64},
                },
            ),
            patch.object(canary, "deliver_completed_mkv", return_value=result),
            patch.object(canary, "validate_completed_delivery", return_value=False),
        ):
            with self.assertRaisesRegex(CompletedDeliveryError, "full receipt and stream"):
                canary.commit_completed_delivery_canary(target, SimpleNamespace())

    def test_cli_rejects_repeated_video_arguments_without_loading_config(self) -> None:
        with patch.object(canary, "load_canary_config") as load:
            exit_code = canary.main(
                [
                    "--config",
                    "config.yaml",
                    "--video",
                    "/anime/a.mkv",
                    "--video",
                    "/anime/b.mkv",
                    "--commit",
                ]
            )

        self.assertEqual(exit_code, 2)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
