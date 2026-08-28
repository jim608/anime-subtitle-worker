from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from safe_files import sha256_file
from srt_utils import SrtBlock, read_srt, validate_same_numbering, validate_translation, write_srt
from subtitle_remediation import (
    RemediationRoundLimitError,
    SubtitleRemediationError,
    next_remediation_round,
    remediate_srt,
    remediate_srt_in_place,
)


class SubtitleRemediationTest(unittest.TestCase):
    def test_opencc_s2twp_changes_only_text_and_writes_hash_bound_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.zh-TW.srt"
            output = root / "episode.fixed.srt"
            original = [
                SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["\u8f6f\u4ef6\u5728\u91cc\u9762"]),
                SrtBlock(2, "00:00:04,000 --> 00:00:06,000", ["\u5934\u53d1\u548c\u9762\u6761"]),
            ]
            write_srt(source, original)

            result = remediate_srt(
                source,
                output,
                role="zh-TW",
                issue_codes=["simplified_chinese_remnant"],
                config=_config(),
                work_path=root / "work",
            )

            converted = read_srt(output)
            self.assertTrue(result.changed)
            self.assertTrue(result.requires_qc)
            self.assertNotEqual(result.input_sha256, result.output_sha256)
            self.assertIn("\u8edf\u9ad4", converted[0].text[0])
            self.assertNotIn("\u8f6f\u4ef6", converted[0].text[0])
            validate_translation(original, converted)
            diagnostic = json.loads(Path(result.diagnostic_path).read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["input"]["sha256"], sha256_file(source))
            self.assertEqual(diagnostic["candidate_output"]["sha256"], sha256_file(output))
            self.assertEqual(diagnostic["rules"], ["opencc_s2twp"])
            self.assertEqual(diagnostic["round"], 1)
            self.assertTrue(diagnostic["caller_must_re_qc"])
            self.assertIn("timing", diagnostic["changes"][0]["before"][0])
            self.assertIn("text", diagnostic["changes"][0]["after"][0])

    def test_glossary_is_exact_single_pass_and_validate_translation_holds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.zh-CN.srt"
            output = root / "episode.fixed.srt"
            original = [
                SrtBlock(7, "00:00:01,000 --> 00:00:02,500", ["Alice \u548c \u611b\u9e97\u7d72\u4f86\u4e86"]),
            ]
            write_srt(source, original)
            config = _config()
            config.translation_glossary = {
                "Alice": "\u611b\u9e97\u7d72",
                "\u611b\u9e97\u7d72": "\u827e\u8389\u7d72",
            }

            result = remediate_srt(
                source,
                output,
                role="zh-CN",
                issue_codes=["glossary_term_inconsistent"],
                config=config,
                work_path=root / "work",
            )

            translated = read_srt(output)
            self.assertEqual(translated[0].text, ["\u611b\u9e97\u7d72 \u548c \u827e\u8389\u7d72\u4f86\u4e86"])
            self.assertEqual(result.applied_rules, ("glossary_exact_replacement",))
            validate_translation(original, translated)

    def test_repeated_punctuation_clamps_only_excessive_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            original = [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["Really???? Fine!!!"])]
            write_srt(source, original)

            remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["repeated_punctuation"],
                config=_config(),
                work_path=root / "work",
            )

            repaired = read_srt(output)
            self.assertEqual(repaired[0].text, ["Really??? Fine!!!"])
            validate_translation(original, repaired)

    def test_long_line_wraps_only_at_punctuation_without_changing_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            original_text = "1234\uff0c5678\uff0c90"
            original = [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", [original_text])]
            write_srt(source, original)
            config = _config()
            config.subtitle_remediation_wrap_max_chars = 8

            remediate_srt(
                source,
                output,
                role="zh-TW",
                issue_codes=["long_line"],
                config=config,
                work_path=root / "work",
            )

            repaired = read_srt(output)
            self.assertEqual(len(repaired[0].text), 2)
            self.assertEqual("".join(repaired[0].text), original_text)
            self.assertTrue(all(len(line) <= 8 for line in repaired[0].text))
            validate_translation(original, repaired)

    def test_unbreakable_long_line_is_noop_and_does_not_touch_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "do-not-touch.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["12345678901234567890"])],
            )
            output.write_bytes(b"existing-output")
            before = output.read_bytes()
            config = _config()
            config.subtitle_remediation_wrap_max_chars = 8

            result = remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["very_long_line"],
                config=config,
                work_path=root / "work",
            )

            self.assertEqual(result.status, "no_safe_change")
            self.assertFalse(result.changed)
            self.assertFalse(result.requires_qc)
            self.assertEqual(output.read_bytes(), before)

    def test_long_line_can_wrap_at_whitespace_without_changing_words(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            original = [
                SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["alpha beta gamma"]),
            ]
            write_srt(source, original)
            config = _config()
            config.subtitle_remediation_wrap_max_chars = 9

            remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["long_line"],
                config=config,
                work_path=root / "work",
            )

            repaired = read_srt(output)
            self.assertEqual(repaired[0].text, ["alpha", "beta gamma"])
            self.assertEqual("".join(repaired[0].text).replace(" ", ""), "alphabetagamma")
            validate_translation(original, repaired)

    def test_small_overlap_is_trimmed_with_hard_minimum_and_total_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            original = [
                SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["one"]),
                SrtBlock(2, "00:00:01,850 --> 00:00:03,000", ["two"]),
            ]
            write_srt(source, original)

            result = remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["timing_overlap"],
                config=_config(),
                work_path=root / "work",
            )

            repaired = read_srt(output)
            self.assertEqual(repaired[0].timing, "00:00:01,000 --> 00:00:01,850")
            self.assertEqual(repaired[1].timing, original[1].timing)
            self.assertEqual([block.text for block in repaired], [block.text for block in original])
            validate_same_numbering(original, repaired)
            diagnostic = json.loads(Path(result.diagnostic_path).read_text(encoding="utf-8"))
            self.assertLessEqual(sum(change.get("shift_ms", 0) for change in diagnostic["changes"]), 500)
            self.assertGreaterEqual(_duration_ms(repaired[0]), 120)
            self.assertGreaterEqual(_duration_ms(repaired[1]), 120)

    def test_large_overlap_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            write_srt(
                source,
                [
                    SrtBlock(1, "00:00:01,000 --> 00:00:02,000", ["one"]),
                    SrtBlock(2, "00:00:01,600 --> 00:00:03,000", ["two"]),
                ],
            )

            result = remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["timing_overlap"],
                config=_config(),
                work_path=root / "work",
            )

            self.assertEqual(result.status, "no_safe_change")
            self.assertFalse(output.exists())

    def test_aligned_bundle_budget_preserves_shorter_cue_during_large_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            original = [
                SrtBlock(1, "00:00:45,560 --> 00:00:50,260", ["long cue"]),
                SrtBlock(2, "00:00:48,460 --> 00:00:50,360", ["short cue"]),
                SrtBlock(3, "00:00:50,320 --> 00:00:51,120", ["byH."]),
                SrtBlock(4, "00:00:50,420 --> 00:00:55,100", ["following cue"]),
            ]
            write_srt(source, original)
            config = _config()
            config.subtitle_remediation_max_timing_shift_seconds = 2.0
            config.subtitle_remediation_max_total_timing_shift_seconds = 3.0
            config.subtitle_remediation_max_overlap_repair_seconds = 2.0

            result = remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["timing_overlap"],
                config=config,
                work_path=root / "work",
            )

            self.assertTrue(result.changed)
            repaired = read_srt(output)
            self.assertEqual(repaired[0].timing, "00:00:45,560 --> 00:00:48,460")
            self.assertEqual(repaired[1].timing, original[1].timing)
            self.assertEqual(repaired[2].timing, original[2].timing)
            self.assertEqual(repaired[3].timing, "00:00:51,120 --> 00:00:55,100")
            self.assertEqual(_duration_ms(repaired[2]), 800)

    def test_too_short_cue_uses_verified_adjacent_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            original = [
                SrtBlock(1, "00:00:01,000 --> 00:00:01,050", ["short"]),
                SrtBlock(2, "00:00:01,500 --> 00:00:02,500", ["next"]),
            ]
            write_srt(source, original)

            remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["too_short"],
                config=_config(),
                work_path=root / "work",
            )

            repaired = read_srt(output)
            self.assertEqual(repaired[0].timing, "00:00:01,000 --> 00:00:01,120")
            self.assertEqual(repaired[1].timing, original[1].timing)
            self.assertEqual(_duration_ms(repaired[0]), 120)

    def test_duration_repair_changes_only_evidence_bound_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            original = [
                SrtBlock(1, "00:00:01,000 --> 00:00:01,200", ["first"]),
                SrtBlock(2, "00:00:02,000 --> 00:00:02,200", ["second"]),
                SrtBlock(3, "00:00:03,000 --> 00:00:04,000", ["next"]),
            ]
            write_srt(source, original)

            result = remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["short_duration"],
                config=_config(),
                work_path=root / "work",
                timing_duration_indexes=[2],
            )

            repaired = read_srt(output)
            self.assertEqual(result.changed_indexes, (2,))
            self.assertEqual(repaired[0].timing, original[0].timing)
            self.assertEqual(repaired[1].timing, "00:00:02,000 --> 00:00:02,350")
            self.assertEqual(repaired[2].timing, original[2].timing)

    def test_too_short_single_cue_without_adjacent_space_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:01,050", ["short"])],
            )

            result = remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["too_short"],
                config=_config(),
                work_path=root / "work",
            )

            self.assertEqual(result.status, "no_safe_change")
            self.assertFalse(output.exists())

    def test_non_positive_timing_is_not_guessed_by_too_short_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            write_srt(
                source,
                [
                    SrtBlock(1, "00:00:01,100 --> 00:00:01,000", ["invalid"]),
                    SrtBlock(2, "00:00:02,000 --> 00:00:03,000", ["next"]),
                ],
            )

            result = remediate_srt(
                source,
                output,
                role="source",
                issue_codes=["too_short"],
                config=_config(),
                work_path=root / "work",
            )

            self.assertEqual(result.status, "no_safe_change")
            self.assertFalse(output.exists())

    def test_same_issue_fingerprint_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["What????"])],
            )
            kwargs = {
                "role": "source",
                "issue_codes": ["repeated_punctuation"],
                "config": _config(),
                "work_path": root / "work",
            }

            first = remediate_srt(source, output, **kwargs)
            diagnostic_before = Path(first.diagnostic_path).read_bytes()
            second = remediate_srt(source, output, **kwargs)

            self.assertEqual(second.status, "already_applied")
            self.assertFalse(second.changed)
            self.assertTrue(second.requires_qc)
            self.assertEqual(second.issue_fingerprint, first.issue_fingerprint)
            self.assertEqual(Path(first.diagnostic_path).read_bytes(), diagnostic_before)
            self.assertEqual(len(list((root / "work" / "subtitle_remediation").glob("*.json"))), 1)

    def test_diagnostic_failure_is_fail_closed_for_in_place_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["What????"])],
            )
            original_bytes = source.read_bytes()
            original_hash = hashlib.sha256(original_bytes).hexdigest()

            with patch(
                "subtitle_remediation.atomic_write_text",
                side_effect=OSError("diagnostic disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "diagnostic disk unavailable"):
                    remediate_srt(
                        source,
                        source,
                        role="source",
                        issue_codes=["repeated_punctuation"],
                        config=_config(),
                        work_path=root / "work",
                    )

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(sha256_file(source), original_hash)

    def test_explicit_in_place_entrypoint_atomically_replaces_after_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["What????"])],
            )
            original_hash = sha256_file(source)

            result = remediate_srt_in_place(
                source,
                role="source",
                issue_codes=["repeated_punctuation"],
                config=_config(),
                work_path=root / "work",
            )

            self.assertEqual(result.status, "remediated")
            self.assertEqual(result.input_sha256, original_hash)
            self.assertEqual(result.output_sha256, sha256_file(source))
            self.assertNotEqual(result.input_sha256, result.output_sha256)
            self.assertEqual(read_srt(source)[0].text, ["What???"])
            diagnostic = json.loads(Path(result.diagnostic_path).read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["input"]["sha256"], original_hash)
            self.assertEqual(diagnostic["candidate_output"]["sha256"], sha256_file(source))
            self.assertEqual(_partial_files(root), [])

    def test_candidate_write_failure_preserves_in_place_input_and_leaves_no_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["What????"])],
            )
            original_bytes = source.read_bytes()

            with patch(
                "subtitle_remediation.atomic_write_bytes",
                side_effect=OSError("candidate disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "candidate disk unavailable"):
                    remediate_srt_in_place(
                        source,
                        role="source",
                        issue_codes=["repeated_punctuation"],
                        config=_config(),
                        work_path=root / "work",
                    )

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(_partial_files(root), [])
            diagnostics = list((root / "work" / "subtitle_remediation").glob("*.json"))
            self.assertEqual(len(diagnostics), 1)
            # The durable diagnostic is complete, never a partial JSON file;
            # its hash proves the candidate that failed to publish.
            payload = json.loads(diagnostics[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "candidate_ready")
            self.assertNotEqual(payload["candidate_output"]["sha256"], sha256_file(source))

    def test_second_round_requires_hash_chain_and_third_round_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["A arrived"])],
            )
            work = root / "work"

            first = remediate_srt(
                source,
                source,
                role="zh-TW",
                issue_codes=["glossary_term_inconsistent"],
                glossary={"A": "B"},
                config=_config(),
                work_path=work,
                round_number=1,
            )
            self.assertEqual(next_remediation_round(source, work_path=work), 2)
            second = remediate_srt(
                source,
                source,
                role="zh-TW",
                issue_codes=["glossary_term_inconsistent"],
                glossary={"B": "C"},
                config=_config(),
                work_path=work,
                round_number=2,
            )

            self.assertNotEqual(first.output_sha256, second.output_sha256)
            self.assertEqual(read_srt(source)[0].text, ["C arrived"])
            with self.assertRaisesRegex(RemediationRoundLimitError, "already consumed"):
                next_remediation_round(source, work_path=work)
            with self.assertRaises(RemediationRoundLimitError):
                remediate_srt(
                    source,
                    source,
                    role="zh-TW",
                    issue_codes=["glossary_term_inconsistent"],
                    glossary={"C": "D"},
                    config=_config(),
                    work_path=work,
                    round_number=3,
                )

    def test_round_two_without_matching_predecessor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["A arrived"])],
            )

            with self.assertRaisesRegex(RemediationRoundLimitError, "hash-matching"):
                remediate_srt(
                    source,
                    source,
                    role="zh-TW",
                    issue_codes=["glossary_term_inconsistent"],
                    glossary={"A": "B"},
                    config=_config(),
                    work_path=root / "work",
                    round_number=2,
                )

    def test_unsupported_or_role_unsafe_issue_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["\u8f6f\u4ef6 Alice"])],
            )

            result = remediate_srt(
                source,
                output,
                role="ja",
                issue_codes=["simplified_chinese_remnant", "glossary_term_inconsistent", "hallucination_text"],
                glossary={"Alice": "\u611b\u9e97\u7d72"},
                config=_config(),
                work_path=root / "work",
            )

            self.assertEqual(result.status, "no_safe_change")
            self.assertFalse(output.exists())

    def test_unsafe_opencc_configuration_fails_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "episode.srt"
            output = root / "episode.fixed.srt"
            write_srt(
                source,
                [SrtBlock(1, "00:00:01,000 --> 00:00:03,000", ["\u8f6f\u4ef6"])],
            )
            config = _config()
            config.opencc_config = "t2s"

            with self.assertRaisesRegex(SubtitleRemediationError, "requires OpenCC s2twp"):
                remediate_srt(
                    source,
                    output,
                    role="zh-TW",
                    issue_codes=["simplified_chinese_remnant"],
                    config=config,
                    work_path=root / "work",
                )
            self.assertFalse(output.exists())


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        opencc_config="s2twp.json",
        translation_glossary={},
        subtitle_quality_max_primary_chars=42,
        subtitle_quality_max_overlap_seconds=0.10,
        subtitle_quality_hard_min_duration_seconds=0.12,
        subtitle_quality_min_duration_seconds=0.35,
        subtitle_remediation_punctuation_repeat_limit=3,
        subtitle_remediation_max_visual_lines=2,
        subtitle_remediation_max_timing_shift_seconds=0.20,
        subtitle_remediation_max_total_timing_shift_seconds=0.50,
        subtitle_remediation_max_overlap_repair_seconds=0.20,
    )


def _duration_ms(block: SrtBlock) -> int:
    start, end = (value.strip() for value in block.timing.split("-->", 1))
    return _timestamp_ms(end.split()[0]) - _timestamp_ms(start)


def _timestamp_ms(value: str) -> int:
    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1000
        + int(milliseconds)
    )


def _partial_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and (
            path.name.endswith(".tmp")
            or path.name.endswith(".copying")
            or path.name.startswith(".write-")
        )
    )


if __name__ == "__main__":
    unittest.main()
