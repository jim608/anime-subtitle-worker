from __future__ import annotations

import unittest

from m2_strict_observation import (
    PROCESSING_STRATEGIES,
    STRICT_EVIDENCE_KEYS,
    SUMMARY_COUNTER_KEYS,
    StrictObservationInputError,
    empty_summary_counters,
    normalize_processing_strategy,
    qualify_strict_output,
    strict_evidence_template,
    update_summary_counters,
    validate_summary_counters,
)


class M2StrictObservationTests(unittest.TestCase):
    @staticmethod
    def _completed(**overrides: object) -> dict[str, object]:
        outcome: dict[str, object] = {
            "event_kind": "terminal",
            "terminal_status": "COMPLETED",
            "processing_strategy": "ASR_JA_AUDIO",
        }
        outcome.update(overrides)
        return outcome

    def test_all_eleven_required_facts_qualify(self) -> None:
        evidence = strict_evidence_template(passed=True)

        result = qualify_strict_output(self._completed(), evidence)

        self.assertEqual(tuple(evidence), STRICT_EVIDENCE_KEYS)
        self.assertEqual(len(STRICT_EVIDENCE_KEYS), 11)
        self.assertTrue(result["qualified"])
        self.assertEqual(result["missing_evidence"], [])
        self.assertEqual(result["failed_evidence"], [])

    def test_each_false_fact_fails_closed(self) -> None:
        for key in STRICT_EVIDENCE_KEYS:
            with self.subTest(key=key):
                evidence = strict_evidence_template(passed=True)
                evidence[key] = False

                result = qualify_strict_output(self._completed(), evidence)

                self.assertFalse(result["qualified"])
                self.assertIn(key, result["failed_evidence"])

    def test_each_missing_fact_fails_closed(self) -> None:
        for key in STRICT_EVIDENCE_KEYS:
            with self.subTest(key=key):
                evidence = strict_evidence_template(passed=True)
                del evidence[key]

                result = qualify_strict_output(self._completed(), evidence)

                self.assertFalse(result["qualified"])
                self.assertIn(key, result["missing_evidence"])

    def test_non_bool_or_extra_evidence_is_rejected(self) -> None:
        evidence = strict_evidence_template(passed=True)
        evidence["hard_qc_pass"] = 1  # type: ignore[assignment]
        with self.assertRaises(StrictObservationInputError):
            qualify_strict_output(self._completed(), evidence)

        with self.assertRaises(StrictObservationInputError):
            update_summary_counters(
                None,
                outcome=self._completed(),
                evidence=[],  # type: ignore[arg-type]
            )

        evidence = strict_evidence_template(passed=True)
        evidence["raw_path"] = True  # type: ignore[assignment]
        with self.assertRaises(StrictObservationInputError):
            qualify_strict_output(self._completed(), evidence)

    def test_status_and_incident_contradictions_fail_closed(self) -> None:
        evidence = strict_evidence_template(passed=True)
        review = qualify_strict_output(
            self._completed(terminal_status="review_required"),
            evidence,
        )
        self.assertFalse(review["qualified"])
        self.assertFalse(review["evidence"]["final_state_completed"])

        incidents = {
            "output_parse_failure": "output_parse_pass",
            "source_mutation_incident": "source_checksum_unchanged",
            "duplicate_job": "no_duplicate_job",
            "duplicate_publish": "no_duplicate_publish",
        }
        for flag, fact in incidents.items():
            with self.subTest(flag=flag):
                result = qualify_strict_output(
                    self._completed(**{flag: True}),
                    evidence,
                )
                self.assertFalse(result["qualified"])
                self.assertFalse(result["evidence"][fact])

    def test_unresolved_states_and_incorrect_completion_fail_closed(self) -> None:
        evidence = strict_evidence_template(passed=True)
        for outcome in (
            self._completed(quarantined=True),
            self._completed(unresolved_retry=True),
            self._completed(unresolved_fallback=True),
            self._completed(incorrect_completion=True),
        ):
            with self.subTest(outcome=outcome):
                self.assertFalse(qualify_strict_output(outcome, evidence)["qualified"])

    def test_only_deidentified_outcome_keys_and_codes_are_accepted(self) -> None:
        for key in ("path", "title", "detail", "full_log", "transcript"):
            with self.subTest(key=key):
                with self.assertRaises(StrictObservationInputError):
                    qualify_strict_output(
                        {**self._completed(), key: "private"},
                        strict_evidence_template(passed=True),
                    )
        with self.assertRaises(StrictObservationInputError):
            normalize_processing_strategy("server/path/private title")

    def test_processing_strategy_normalization_is_bounded(self) -> None:
        aliases = {
            "USE_EXISTING_ZH_TW": "USE_EXISTING_ZH_TW",
            "adopted_zh_tw": "USE_EXISTING_ZH_TW",
            "converted_zh_cn": "CONVERT_ZH_CN",
            "translated-japanese-subtitle": "TRANSLATE_JA_SUBTITLE",
            "japanese_audio_asr": "ASR_JA_AUDIO",
            "needs_review": "NEEDS_REVIEW",
            "future_safe_code": "OTHER",
            "": "UNREPORTED",
            None: "UNREPORTED",
        }
        for raw, expected in aliases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_processing_strategy(raw), expected)

    def test_named_counters_track_claim_and_strict_terminal_separately(self) -> None:
        counters, qualification = update_summary_counters(
            None,
            outcome={
                "event_kind": "claim",
                "terminal_status": "running",
                "claimed_after_gate_start": True,
                "processing_strategy": "japanese_audio_asr",
            },
        )
        self.assertIsNone(qualification)
        self.assertEqual(counters["claimed_after_gate_start"], 1)
        self.assertEqual(counters["processing_strategy_counts"]["ASR_JA_AUDIO"], 1)
        self.assertEqual(counters["gate_progress"], 0)

        counters, qualification = update_summary_counters(
            counters,
            outcome=self._completed(),
            evidence=strict_evidence_template(passed=True),
        )
        self.assertTrue(qualification and qualification["qualified"])
        self.assertEqual(counters["completed_strict_verified"], 1)
        self.assertEqual(counters["gate_progress"], 1)

    def test_all_required_named_incident_counters_update(self) -> None:
        outcome = self._completed(
            terminal_status="FAILED",
            quarantined=True,
            hallucination_blocked=True,
            output_parse_failure=True,
            source_mutation_incident=True,
            duplicate_job=True,
            duplicate_publish=True,
            incorrect_completion=True,
            breaker_tripped=True,
            checkpoint_resumed=True,
            oom_event=True,
        )

        counters, qualification = update_summary_counters(
            None,
            outcome=outcome,
            evidence=strict_evidence_template(passed=True),
        )

        self.assertFalse(qualification and qualification["qualified"])
        for key in (
            "failed",
            "quarantined",
            "hallucination_blocked",
            "output_parse_failures",
            "source_mutation_incidents",
            "duplicate_jobs",
            "duplicate_publishes",
            "incorrect_completions",
            "breaker_trips",
            "checkpoint_resumes",
            "oom_events",
        ):
            self.assertEqual(counters[key], 1, key)

    def test_completed_without_all_evidence_never_advances_gate(self) -> None:
        counters, qualification = update_summary_counters(
            None,
            outcome=self._completed(),
            evidence={"final_state_completed": True},
        )

        self.assertFalse(qualification and qualification["qualified"])
        self.assertEqual(counters["completed_unverified"], 1)
        self.assertEqual(counters["completed_strict_verified"], 0)
        self.assertEqual(counters["gate_progress"], 0)

    def test_counter_schema_is_fixed_non_negative_and_input_is_not_mutated(self) -> None:
        counters = empty_summary_counters()
        original = {**counters, "processing_strategy_counts": dict(counters["processing_strategy_counts"])}
        updated, _ = update_summary_counters(
            counters,
            outcome={"event_kind": "observation", "checkpoint_resumed": True},
        )

        self.assertEqual(counters, original)
        self.assertEqual(updated["checkpoint_resumes"], 1)
        self.assertEqual(set(SUMMARY_COUNTER_KEYS), set(updated) - {"processing_strategy_counts"})
        self.assertEqual(set(PROCESSING_STRATEGIES), set(updated["processing_strategy_counts"]))

        invalid = empty_summary_counters()
        invalid["failed"] = -1
        with self.assertRaises(StrictObservationInputError):
            update_summary_counters(invalid, outcome={"event_kind": "observation"})

        missing = empty_summary_counters()
        missing.pop("gate_progress")
        validated = validate_summary_counters(missing)
        self.assertEqual(validated["gate_progress"], 0)


if __name__ == "__main__":
    unittest.main()
