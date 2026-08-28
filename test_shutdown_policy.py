from pathlib import Path
import unittest


class ShutdownPolicyTest(unittest.TestCase):
    def test_shutdown_handler_does_not_promise_to_finish_current_video(self) -> None:
        source = Path("main.py").read_text(encoding="utf-8")

        self.assertIn("Docker stop timeout controls active work", source)
        self.assertNotIn("finishing current video before exit", source)
        self.assertNotIn("still waiting for current work to finish", source)


if __name__ == "__main__":
    unittest.main()
