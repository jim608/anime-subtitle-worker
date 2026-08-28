from pathlib import Path
import unittest


class AcceptanceRuntimeImageTests(unittest.TestCase):
    def test_worker_image_includes_acceptance_package(self):
        dockerfile = Path(__file__).with_name("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY acceptance /app/acceptance", dockerfile)


if __name__ == "__main__":
    unittest.main()
