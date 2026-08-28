from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest

from video_policy import is_standalone_theme_video


class StandaloneThemeVideoPolicyTest(unittest.TestCase):
    def test_detects_common_standalone_theme_names(self) -> None:
        config = SimpleNamespace(scanner_skip_standalone_op_ed=True)

        for path in (
            Path("/anime/Show/Extras/ED.mkv"),
            Path("/anime/Show/Extras/ED01.mkv"),
            Path("/anime/Show/Extras/S02OP.mkv"),
            Path("/anime/Show/Specials/Show - NCED.mkv"),
            Path("/anime/Show/Extras/Show - Creditless Opening.mkv"),
        ):
            with self.subTest(path=path):
                self.assertTrue(is_standalone_theme_video(path, config))

    def test_keeps_episodes_and_non_theme_extras(self) -> None:
        config = SimpleNamespace(scanner_skip_standalone_op_ed=True)

        for path in (
            Path("/anime/Show/Season 1/Show - S01E01 - Opening Battle.mkv"),
            Path("/anime/Show/Extras/OVA.mkv"),
            Path("/anime/Show/Specials/Show - S00E01 - Ending Ceremony.mkv"),
        ):
            with self.subTest(path=path):
                self.assertFalse(is_standalone_theme_video(path, config))

    def test_policy_can_be_disabled(self) -> None:
        config = SimpleNamespace(scanner_skip_standalone_op_ed=False)

        self.assertFalse(is_standalone_theme_video(Path("/anime/Show/Extras/NCOP.mkv"), config))


if __name__ == "__main__":
    unittest.main()
