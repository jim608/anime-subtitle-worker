from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deployment_backup_retention import (
    COMPLETED_STATE,
    DeploymentBackupRetentionError,
    create_sha256_manifest,
    mark_backup_status,
    prune_deployment_backups,
    verify_sha256_manifest,
)


class DeploymentBackupRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "deployment_backups"
        self.logs = Path(self.temp.name) / "logs"
        self.root.mkdir()
        self.logs.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _backup(self, deployment_id: str, *, state: str | None = None) -> Path:
        backup = self.root / deployment_id
        database = backup / "databases" / "scanner_state.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_bytes(f"database:{deployment_id}".encode())
        images = backup / "images.json"
        images.write_text('{"worker":"old","webui":"old"}\n', encoding="utf-8")
        entries = []
        for path in (database, images):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(f"{digest}  {path.relative_to(backup).as_posix()}")
        (backup / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")
        if state:
            mark_backup_status(
                backup,
                state=state,
                verified_by="unit-test",
                external_sha256_verified=True,
            )
        return backup

    def test_manifest_verification_rejects_tampering(self) -> None:
        backup = self._backup("20260715T010000Z-1")
        verified = verify_sha256_manifest(backup)
        self.assertEqual(verified["checked_files"], 2)

        (backup / "images.json").write_text("tampered", encoding="utf-8")
        with self.assertRaises(DeploymentBackupRetentionError):
            verify_sha256_manifest(backup)

    def test_manifest_creation_streams_nested_files_and_excludes_itself(self) -> None:
        backup = self.root / "20260715T010100Z-11"
        nested = backup / "cache" / "nested file.bin"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"payload" * 4096)
        (backup / "config.yaml").write_text("enabled: true\n", encoding="utf-8")

        created = create_sha256_manifest(backup)
        verified = verify_sha256_manifest(backup)

        self.assertEqual(created["files"], 2)
        self.assertEqual(verified["checked_files"], 2)
        lines = (backup / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(line.endswith("  cache/nested file.bin") for line in lines))
        self.assertFalse(any(line.endswith("  SHA256SUMS") for line in lines))

    def test_create_cli_writes_a_manifest_that_can_be_verified(self) -> None:
        backup = self.root / "20260715T010200Z-12"
        payload = backup / "runtime" / "worker-log-before.txt"
        payload.parent.mkdir(parents=True)
        payload.write_text("runtime snapshot\n", encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("deployment_backup_retention.py")),
                "create",
                "--backup",
                str(backup),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        created = json.loads(result.stdout)
        self.assertEqual(created["files"], 1)
        self.assertEqual(verify_sha256_manifest(backup)["checked_files"], 1)

    def test_tiered_retention_only_removes_completed_verified_backups(self) -> None:
        completed_ids = [
            "20260715T030000Z-1",
            "20260715T020000Z-2",
            "20260715T010000Z-3",
            "20260714T030000Z-4",
            "20260713T030000Z-5",
            "20260712T030000Z-6",
        ]
        for deployment_id in completed_ids:
            self._backup(deployment_id, state=COMPLETED_STATE)
        failed = self._backup("20260711T030000Z-7", state="deployment_failed")
        incomplete = self._backup("20260710T030000Z-8")

        result = prune_deployment_backups(
            self.root,
            apply=True,
            newest=3,
            daily=2,
            weekly=1,
        )

        self.assertEqual(
            set(result["removed"]),
            {"20260713T030000Z-5", "20260712T030000Z-6"},
        )
        self.assertTrue(failed.exists())
        self.assertTrue(incomplete.exists())
        self.assertEqual(result["protected"][failed.name], "deployment_failed")
        self.assertEqual(result["protected"][incomplete.name], "legacy_or_incomplete")
        self.assertTrue(all((self.root / name).exists() for name in result["kept"]))

    def test_legacy_backup_requires_success_log_and_full_checksum_verification(self) -> None:
        successful = self._backup("20260709T030000Z-9")
        tampered = self._backup("20260708T030000Z-10")
        (tampered / "images.json").write_text("tampered", encoding="utf-8")
        (self.logs / "safe-update-stack-history.log").write_text(
            "Stack update complete. deployment_id=20260709T030000Z-9 backup=/work/deployment_backups/20260709T030000Z-9\n"
            "Stack update complete. deployment_id=20260708T030000Z-10 backup=/work/deployment_backups/20260708T030000Z-10\n",
            encoding="utf-8",
        )

        result = prune_deployment_backups(
            self.root,
            apply=False,
            success_log_root=self.logs,
            newest=3,
            daily=7,
            weekly=4,
        )

        self.assertIn(successful.name, result["adopted_legacy"])
        self.assertEqual(json.loads((successful / "RETENTION_STATUS.json").read_text())["state"], COMPLETED_STATE)
        self.assertIn(tampered.name, result["protected"])
        self.assertIn("legacy_verification_failed", result["protected"][tampered.name])

    def test_completed_backup_is_not_deleted_when_contents_changed_after_marking(self) -> None:
        for deployment_id in (
            "20260715T030000Z-1",
            "20260715T020000Z-2",
            "20260715T010000Z-3",
        ):
            self._backup(deployment_id, state=COMPLETED_STATE)
        tampered = self.root / "20260715T010000Z-3"
        (tampered / "images.json").write_text("changed after deployment\n", encoding="utf-8")

        result = prune_deployment_backups(
            self.root,
            apply=True,
            newest=1,
            daily=0,
            weekly=0,
        )

        self.assertTrue(tampered.exists())
        self.assertIn("pre_delete_verification_failed", result["protected"][tampered.name])
        self.assertNotIn(tampered.name, result["removed"])

    def test_verified_backup_retries_transient_directory_not_empty_race(self) -> None:
        newest = self._backup("20260715T030000Z-1", state=COMPLETED_STATE)
        removable = self._backup("20260715T020000Z-2", state=COMPLETED_STATE)
        real_rmtree = shutil.rmtree
        calls = 0

        def flaky_rmtree(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
            real_rmtree(path)

        with patch(
            "deployment_backup_retention.shutil.rmtree",
            side_effect=flaky_rmtree,
        ):
            result = prune_deployment_backups(
                self.root,
                apply=True,
                newest=1,
                daily=0,
                weekly=0,
            )

        self.assertEqual(calls, 2)
        self.assertTrue(newest.exists())
        self.assertFalse(removable.exists())
        self.assertEqual(result["removed"], [removable.name])

    def test_verified_backup_tolerates_a_longer_unraid_directory_settle(self) -> None:
        newest = self._backup("20260715T030000Z-1", state=COMPLETED_STATE)
        removable = self._backup("20260715T020000Z-2", state=COMPLETED_STATE)
        real_rmtree = shutil.rmtree
        calls = 0

        def flaky_rmtree(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls < 6:
                raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
            real_rmtree(path)

        with patch(
            "deployment_backup_retention.shutil.rmtree",
            side_effect=flaky_rmtree,
        ), patch("deployment_backup_retention.time.sleep") as sleep:
            result = prune_deployment_backups(
                self.root,
                apply=True,
                newest=1,
                daily=0,
                weekly=0,
            )

        self.assertEqual(calls, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertTrue(newest.exists())
        self.assertFalse(removable.exists())
        self.assertEqual(result["removed"], [removable.name])


if __name__ == "__main__":
    unittest.main()
