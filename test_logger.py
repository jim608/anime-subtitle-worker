from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from logger import LOGGER_NAME, log_failure, setup_logging


class LoggerTest(unittest.TestCase):
    def test_failure_preserves_original_database_exception_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            logger = setup_logging(root)
            self._remove_console_handler(logger)
            secret_local = "LOCAL_VALUE_MUST_NOT_BE_LOGGED"
            try:
                raise sqlite3.OperationalError("database is locked")
            except sqlite3.OperationalError as error:
                saved_error = error
            # Logging may happen after the original exception handler exited.
            log_failure(root, "video.mkv", "worker", saved_error)
            for handler in logger.handlers:
                handler.flush()
            report = (root / "app.log").read_text(encoding="utf-8")
            self._close_logger()
            self.assertIn("Traceback (most recent call last)", report)
            self.assertIn("test_failure_preserves_original_database_exception_traceback", report)
            self.assertIn("sqlite3.OperationalError: database is locked", report)
            self.assertNotIn(secret_local, report)
            self.assertEqual(len((root / "failed.log").read_text(encoding="utf-8").splitlines()), 1)

    def test_string_failure_does_not_attach_unrelated_active_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            logger = setup_logging(root)
            self._remove_console_handler(logger)
            try:
                raise ValueError("unrelated exception")
            except ValueError:
                log_failure(root, "video.mkv", "worker", "explicit failure")
            for handler in logger.handlers:
                handler.flush()
            report = (root / "app.log").read_text(encoding="utf-8")
            self._close_logger()
            self.assertIn("explicit failure", report)
            self.assertNotIn("unrelated exception", report)
            self.assertNotIn("Traceback", report)

    def tearDown(self) -> None:
        self._close_logger()

    def _close_logger(self) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    @staticmethod
    def _remove_console_handler(logger: logging.Logger) -> None:
        for handler in list(logger.handlers):
            if type(handler) is logging.StreamHandler:
                logger.removeHandler(handler)
                handler.close()

    def test_app_log_rotates_at_configured_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.dict(
                os.environ,
                {"APP_LOG_MAX_BYTES": "1024", "APP_LOG_BACKUP_COUNT": "2"},
                clear=False,
            ):
                logger = setup_logging(root)
                self._remove_console_handler(logger)
                for index in range(100):
                    logger.info("entry=%s %s", index, "x" * 80)
                for handler in logger.handlers:
                    handler.flush()

            self.assertTrue((root / "app.log").exists())
            self.assertTrue((root / "app.log.1").exists())
            self.assertLess((root / "app.log").stat().st_size, 2048)
            self._close_logger()

    def test_failure_log_rotation_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            logger = setup_logging(root)
            self._remove_console_handler(logger)
            with patch.dict(
                os.environ,
                {"FAILURE_LOG_MAX_BYTES": "1024", "FAILURE_LOG_BACKUP_COUNT": "2"},
                clear=False,
            ):
                for index in range(30):
                    log_failure(root, f"video-{index}.mkv", "translation", "x" * 80)

            self.assertTrue((root / "failed.log").exists())
            self.assertTrue((root / "failed.log.1").exists())
            self.assertFalse((root / "failed.log.3").exists())
            self._close_logger()

    def test_setup_trims_legacy_unbounded_app_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "app.log").write_bytes((b"old log line\n" * 500))
            with patch.dict(os.environ, {"APP_LOG_MAX_BYTES": "1024"}, clear=False):
                logger = setup_logging(root)
                self._remove_console_handler(logger)
                for handler in logger.handlers:
                    handler.flush()
                self._close_logger()

            content = (root / "app.log").read_bytes()
            self.assertIn(b"older log content trimmed", content)
            self.assertLess(len(content), 1024)


if __name__ == "__main__":
    unittest.main()
