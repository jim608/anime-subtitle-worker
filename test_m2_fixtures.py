from __future__ import annotations

import json
from pathlib import Path
import unittest

from source_analyzer import analyze_sources


FIXTURE_PATH = Path(__file__).parent / "tests" / "fixtures" / "m2" / "source_selection_cases.json"


class M2RepresentativeFixtureTest(unittest.TestCase):
    def test_all_required_representative_source_cases(self) -> None:
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertEqual("m2-representative-fixtures-v1", payload["schema_version"])
        cases = payload["cases"]
        self.assertEqual(10, len(cases))
        subtitle_templates = payload["subtitle_templates"]
        audio_templates = payload["audio_templates"]

        for case in cases:
            with self.subTest(case=case["name"]):
                subtitles = [
                    self._candidate(subtitle_templates, item)
                    for item in case.get("subtitles", [])
                ]
                audios = [
                    self._candidate(audio_templates, item)
                    for item in case.get("audios", [])
                ]
                decision = analyze_sources(
                    subtitles,
                    audios,
                    media_duration_seconds=payload["media_duration_seconds"],
                )
                self.assertEqual(case["expected_strategy"], decision.strategy)
                self.assertEqual(
                    case.get("expected_subtitle_track"),
                    decision.selected_subtitle_track,
                )
                self.assertEqual(
                    case.get("expected_audio_track"),
                    decision.selected_audio_track,
                )

    @staticmethod
    def _candidate(templates: dict[str, dict[str, object]], item: dict[str, object]) -> dict[str, object]:
        template_name = str(item["template"])
        return {
            **templates[template_name],
            **{key: value for key, value in item.items() if key != "template"},
            "source_reference": f"stream:{item['track_index']}",
        }


if __name__ == "__main__":
    unittest.main()
