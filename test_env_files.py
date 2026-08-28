from pathlib import Path
import unittest


class EnvFileTest(unittest.TestCase):
    def test_translator_model_env_value_does_not_include_key_name(self) -> None:
        for path in (Path(".env"), Path(".env.example")):
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("TRANSLATOR_MODEL="):
                    continue
                value = line.split("=", 1)[1]
                self.assertFalse(
                    value.startswith("TRANSLATOR_MODEL="),
                    f"{path} has a duplicated TRANSLATOR_MODEL= prefix",
                )


if __name__ == "__main__":
    unittest.main()
