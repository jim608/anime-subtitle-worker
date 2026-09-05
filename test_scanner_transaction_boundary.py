from __future__ import annotations

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from completed_delivery import completed_delivery_receipt_path
from scan_state import ScanStateStore, video_scan_signature
from scanner import InventoryWalkError, VideoScanner
from test_scanner import _config, _logger


class ScannerTransactionBoundaryTest(unittest.TestCase):
    def test_reconcile_completion_proof_allows_an_independent_sqlite_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"unchanged-media")
            config = _config(root, scanner_reconcile_batch_size=1)
            scanner = VideoScanner(config, _logger())
            writer_checks: list[bool] = []

            def full_completion_proof(_video: Path) -> bool:
                # A slow hash/ffprobe/QC callback must not monopolize the WAL
                # writer needed by the active AI child's stage heartbeat.
                with closing(sqlite3.connect(config.scanner_state_path, timeout=0)) as other:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute(
                        "INSERT OR REPLACE INTO ai_delivery_meta(key,value,updated_at) VALUES('boundary-test','1',1)"
                    )
                    other.commit()
                writer_checks.append(True)
                return True

            with (
                patch.object(scanner, "_classify", return_value=("finished", "cached", False)),
                patch.object(scanner, "_has_required_finished_subtitle", side_effect=full_completion_proof),
                patch("scanner.has_ai_finished_subtitle", return_value=False),
            ):
                self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 1)
            self.assertEqual(writer_checks, [True])

    def test_ordinary_scan_commits_disposable_cache_before_completion_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"unchanged-media")
            config = _config(root)
            scanner = VideoScanner(config, _logger())
            checked: list[bool] = []

            def classify(path: Path) -> tuple[str, str, bool]:
                state = scanner._state_store()
                state.put_status(video_scan_signature(path, config, scanner._config_signature), "finished")
                scanner._note_state_write()
                self.assertTrue(state.in_transaction)
                return "finished", "fresh", False

            def verify(_video: Path) -> bool:
                with closing(sqlite3.connect(config.scanner_state_path, timeout=0)) as other:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("UPDATE video_scan_cache SET updated_at=updated_at")
                    other.commit()
                checked.append(True)
                return True

            with (
                patch.object(scanner, "_classify", side_effect=classify),
                patch.object(scanner, "_has_required_finished_subtitle", side_effect=verify),
                patch("scanner.has_ai_finished_subtitle", return_value=False),
            ):
                self.assertEqual(scanner.refresh_queue(), 1)
            self.assertEqual(checked, [True])

    def test_completion_revision_change_rolls_back_without_false_completion(self) -> None:
        for changed_kind in ("source", "receipt", "subtitle", "completed_output"):
            for phase in ("during_proof", "after_begin", "after_record"):
                with self.subTest(changed_kind=changed_kind, phase=phase), tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir) / "media"
                    root.mkdir()
                    video = root / "Anime S01E01.mkv"
                    video.write_bytes(b"unchanged-media")
                    config = _config(
                        root,
                        completed_delivery_enabled=True,
                        completed_delivery_path=Path(temp_dir) / "completed",
                        scanner_reconcile_batch_size=1,
                    )
                    receipt = completed_delivery_receipt_path(video, config)
                    receipt.parent.mkdir(parents=True)
                    receipt.write_bytes(b"original-receipt")
                    subtitle = video.with_suffix(".official.zh-TW.ass")
                    subtitle.write_bytes(b"original-subtitle")
                    completed_output = config.completed_delivery_path / video.name
                    completed_output.parent.mkdir(parents=True)
                    completed_output.write_bytes(b"original-output")
                    changed_path = {
                        "source": video,
                        "receipt": receipt,
                        "subtitle": subtitle,
                        "completed_output": completed_output,
                    }[changed_kind]
                    scanner = VideoScanner(config, _logger())
                    seed = ScanStateStore.from_config(config)
                    seed.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
                    seed.ensure_ai_delivery_obligation(
                        video,
                        media_size=video.stat().st_size,
                        media_mtime_ns=video.stat().st_mtime_ns,
                        policy_revision=scanner._processing_policy_revision,
                    )
                    seed.commit()
                    before_queue = seed.ai_queue_candidate_snapshot(video)
                    seed.close()
                    original_begin = ScanStateStore.begin_ai_inventory_path
                    original_record = ScanStateStore.record_ai_inventory_observation

                    def mutate_evidence() -> None:
                        changed_path.write_bytes(b"changed-evidence-revision")

                    def verify(_video: Path) -> bool:
                        if phase == "during_proof":
                            mutate_evidence()
                        return True

                    def begin(state: ScanStateStore, epoch_id: str) -> None:
                        original_begin(state, epoch_id)
                        if phase == "after_begin":
                            mutate_evidence()

                    def record(state: ScanStateStore, epoch_id: str, path: Path, **kwargs):
                        result = original_record(state, epoch_id, path, **kwargs)
                        if phase == "after_record":
                            mutate_evidence()
                        return result

                    with (
                        patch.object(scanner, "_classify", return_value=("finished", "cached", False)),
                        patch.object(scanner, "_has_required_finished_subtitle", side_effect=verify),
                        patch("scanner.has_ai_finished_subtitle", return_value=False),
                        patch.object(ScanStateStore, "begin_ai_inventory_path", new=begin),
                        patch.object(ScanStateStore, "record_ai_inventory_observation", new=record),
                        self.assertRaisesRegex(InventoryWalkError, "completion evidence changed"),
                    ):
                        scanner.refresh_queue(reconcile_batch=True)
                    state = ScanStateStore.from_config(config)
                    try:
                        self.assertEqual(state.ai_queue_candidate_snapshot(video), before_queue)
                        self.assertEqual(
                            state.observation_connection.execute(
                                "SELECT state, attempt_count FROM ai_delivery_obligations"
                            ).fetchall(),
                            [("open", 0)],
                        )
                        self.assertEqual(
                            state.observation_connection.execute("SELECT COUNT(*) FROM ai_media_inventory").fetchone()[0],
                            0,
                        )
                    finally:
                        state.close()

    def test_reconcile_restart_is_idempotent_and_retains_existing_attempt_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Anime S01E01.mkv"
            video.write_bytes(b"unchanged-media")
            config = _config(root, scanner_reconcile_batch_size=1)
            seed = ScanStateStore.from_config(config)
            seed.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
            seed.observation_connection.execute(
                "UPDATE ai_candidate_queue SET attempts=2,retry_strategy='verified-checkpoint',next_retry_at=17 WHERE path=?",
                (str(video.resolve()),),
            )
            seed.commit()
            seed.close()
            for _ in range(2):
                scanner = VideoScanner(config, _logger())
                with patch.object(scanner, "_classify", return_value=("needs_ai", "cached", False)):
                    self.assertEqual(scanner.refresh_queue(reconcile_batch=True), 1)
            state = ScanStateStore.from_config(config)
            try:
                self.assertEqual(
                    state.observation_connection.execute(
                        "SELECT status,attempts,retry_strategy,next_retry_at FROM ai_candidate_queue"
                    ).fetchall(),
                    [("queued", 2, "verified-checkpoint", 17)],
                )
                self.assertEqual(
                    state.observation_connection.execute("SELECT COUNT(*) FROM ai_delivery_obligations").fetchone()[0],
                    1,
                )
            finally:
                state.close()

    def test_new_or_renamed_matching_sidecar_during_proof_is_rejected(self) -> None:
        for operation in ("create", "rename"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "Anime S01E01.mkv"
                video.write_bytes(b"unchanged-media")
                staged = root / "pending-subtitle.bin"
                staged.write_bytes(b"new-official-subtitle")
                matching = root / f"{video.stem}.custom.cht.ass"
                config = _config(root, scanner_reconcile_batch_size=1)
                scanner = VideoScanner(config, _logger())

                def verify(_video: Path) -> bool:
                    if operation == "create":
                        matching.write_bytes(b"new-official-subtitle")
                    else:
                        staged.rename(matching)
                    return True

                with (
                    patch.object(scanner, "_classify", return_value=("finished", "cached", False)),
                    patch.object(scanner, "_has_required_finished_subtitle", side_effect=verify),
                    patch("scanner.has_ai_finished_subtitle", return_value=False),
                    self.assertRaisesRegex(InventoryWalkError, "completion evidence changed"),
                ):
                    scanner.refresh_queue(reconcile_batch=True)
                self.assertEqual(video.read_bytes(), b"unchanged-media")
                state = ScanStateStore.from_config(config)
                try:
                    self.assertEqual(
                        state.observation_connection.execute("SELECT COUNT(*) FROM ai_media_inventory").fetchone()[0],
                        0,
                    )
                finally:
                    state.close()


if __name__ == "__main__":
    unittest.main()
