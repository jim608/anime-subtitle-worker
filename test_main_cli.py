from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from main import _refresh_ai_queue_state


ROOT = Path(__file__).resolve().parent


class MainCliTest(unittest.TestCase):
    def test_help_includes_refresh_ai_queue_state(self) -> None:
        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("--refresh-ai-queue-state", result.stdout)

    def test_refresh_ai_queue_state_only_refreshes_queue(self) -> None:
        calls: list[bool] = []

        class FakeScanner:
            def refresh_queue(self, *, force_full: bool = False) -> int:
                calls.append(force_full)
                return 7

        logger = SimpleNamespace(messages=[], info=lambda *args: logger.messages.append(args))

        _refresh_ai_queue_state(FakeScanner(), logger)

        self.assertEqual(calls, [True])
        self.assertIn("AI queue state refresh complete. scanned=%s", logger.messages[0])


if __name__ == "__main__":
    unittest.main()
