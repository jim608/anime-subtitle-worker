from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import errno
import os
import tempfile
import unittest

from lock import VideoLock, _read_lock_info


class VideoLockTest(unittest.TestCase):
    def test_lock_payload_includes_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mkv"
            with patch("lock._process_start_epoch", return_value=1234.5):
                lock = VideoLock(target)
                self.assertTrue(lock.acquire())
                try:
                    info = _read_lock_info(lock.lock_path)
                finally:
                    lock.release()

            self.assertEqual(info["pid"], os.getpid())
            self.assertEqual(info["process_start"], 1234.5)

    def test_lock_without_process_start_is_stale_when_lock_predates_current_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "mikan_worker"
            lock_path = target.with_name(f"{target.name}.lock")
            lock_path.write_text(f"pid={os.getpid()}\ncreated=old\n", encoding="utf-8")
            os.utime(lock_path, (2500, 2500))

            with (
                patch("lock.time.time", return_value=2600),
                patch("lock._process_start_epoch", return_value=2550),
            ):
                lock = VideoLock(target, stale_seconds=1000)
                self.assertTrue(lock.acquire())
                try:
                    info = _read_lock_info(lock.lock_path)
                finally:
                    lock.release()

            self.assertEqual(info["pid"], os.getpid())
            self.assertEqual(info["process_start"], 2550)

    def test_live_owner_is_not_stale_only_because_lock_is_old(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "mikan_enqueue"
            lock = VideoLock(target, stale_seconds=60)
            lock.lock_path.write_text(
                f"pid={os.getpid()}\nprocess_start=1000\ncreated=old\n",
                encoding="utf-8",
            )
            os.utime(lock.lock_path, (1000, 1000))

            with (
                patch("lock.time.time", return_value=10_000),
                patch("lock._is_process_running", return_value=True),
                patch("lock._process_start_epoch", return_value=1000),
            ):
                self.assertFalse(lock._is_stale_lock())

    def test_release_removes_owned_lock_when_descriptor_is_already_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "mikan_worker"
            lock = VideoLock(target)
            self.assertTrue(lock.acquire())
            self.assertIsNotNone(lock._fd)

            os.close(lock._fd)
            lock.release()

            self.assertFalse(lock.lock_path.exists())
            self.assertIsNone(lock._fd)
            self.assertFalse(lock.acquired)

    def test_acquire_cleans_up_descriptor_and_lock_when_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mkv"
            lock = VideoLock(target)
            real_open = os.open
            opened_fds: list[int] = []
            write_error = OSError(errno.ENOSPC, "disk full")

            def tracked_open(*args, **kwargs) -> int:
                fd = real_open(*args, **kwargs)
                opened_fds.append(fd)
                return fd

            with (
                patch("lock.os.open", side_effect=tracked_open),
                patch("lock.os.write", side_effect=write_error),
            ):
                with self.assertRaises(OSError) as raised:
                    lock.acquire()

            self.assertIs(raised.exception, write_error)
            self.assertEqual(len(opened_fds), 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(opened_fds[0])
            self.assertEqual(closed.exception.errno, errno.EBADF)
            self.assertFalse(lock.lock_path.exists())
            self.assertIsNone(lock._fd)
            self.assertFalse(lock.acquired)

            retry = VideoLock(target)
            self.assertTrue(retry.acquire())
            retry.release()

    def test_acquire_retries_short_writes_until_payload_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mkv"
            lock = VideoLock(target)
            real_write = os.write
            write_calls = 0

            def short_write(fd: int, data: bytes) -> int:
                nonlocal write_calls
                write_calls += 1
                return real_write(fd, data[:1])

            with patch("lock.os.write", side_effect=short_write):
                self.assertTrue(lock.acquire())
            try:
                info = _read_lock_info(lock.lock_path)
            finally:
                lock.release()

            self.assertGreater(write_calls, 1)
            self.assertEqual(info["pid"], os.getpid())

    def test_acquire_cleans_up_when_write_makes_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mkv"
            lock = VideoLock(target)

            with patch("lock.os.write", return_value=0):
                with self.assertRaises(OSError) as raised:
                    lock.acquire()

            self.assertEqual(raised.exception.errno, errno.EIO)
            self.assertFalse(lock.lock_path.exists())
            self.assertIsNone(lock._fd)
            self.assertFalse(lock.acquired)

    def test_acquire_cleans_up_when_interrupted_during_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mkv"
            lock = VideoLock(target)
            interruption = KeyboardInterrupt()

            with patch("lock.os.write", side_effect=interruption):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    lock.acquire()

            self.assertIs(raised.exception, interruption)
            self.assertFalse(lock.lock_path.exists())
            self.assertIsNone(lock._fd)
            self.assertFalse(lock.acquired)


if __name__ == "__main__":
    unittest.main()
