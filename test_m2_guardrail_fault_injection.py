from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import m2_guardrail_fault_injection as guardrail_fi


WORKER_SOURCE_REVISION = "a" * 64


def _source_revision_marker(root: str | Path) -> Path:
    marker = Path(root) / ".source-revision"
    marker.write_text(WORKER_SOURCE_REVISION + "\n", encoding="utf-8")
    return marker


class M2GuardrailFaultInjectionTests(unittest.TestCase):
    def test_claim_attempt_calls_the_production_main_admission_wrapper(self) -> None:
        import main as worker_main

        with tempfile.TemporaryDirectory() as raw:
            config, fixtures = guardrail_fi._create_isolated_fixture(Path(raw))
            queue_before = fixtures["queue"].read_bytes()
            with patch.object(
                worker_main,
                "_m2_server_canary_admit_new_job",
                return_value=False,
            ) as admission:
                claimed = guardrail_fi._attempt_isolated_claim(
                    config, fixtures["queue"]
                )

            self.assertFalse(claimed)
            admission.assert_called_once_with(config)
            self.assertEqual(fixtures["queue"].read_bytes(), queue_before)

    def test_all_seven_faults_pass_required_isolation_checks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            marker = _source_revision_marker(raw)
            result = guardrail_fi.run_fault_suite(
                Path(raw) / "server-logs",
                worker_source_revision_file=marker,
            )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["breaker_tests_passed"], 7)
            self.assertEqual(result["breaker_tests_total"], 7)
            self.assertFalse(result["production_resources_affected"])
            self.assertFalse(result["production_config_loaded"])
            self.assertEqual(result["contract"], guardrail_fi.FAULT_RESULT_CONTRACT)
            self.assertEqual(result["worker_source_revision"], WORKER_SOURCE_REVISION)
            self.assertLessEqual(result["started_at_epoch"], result["finished_at_epoch"])
            self.assertEqual(
                [item["fault"] for item in result["case_results"]],
                list(guardrail_fi.FAULT_NAMES),
            )
            for case in result["case_results"]:
                self.assertTrue(case["passed"], case)
                self.assertEqual(
                    set(case["checks"]), set(guardrail_fi.CHECK_NAMES)
                )
                self.assertTrue(all(case["checks"].values()), case)

            log_path = Path(result["log_path"])
            result_path = Path(result["result_path"])
            self.assertTrue(log_path.is_file())
            self.assertTrue(result_path.is_file())
            persisted = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["breaker_tests_passed"], 7)
            events = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            verified_events = [
                event for event in events if event["event"] == "case_verified"
            ]
            self.assertEqual(len(verified_events), 7)
            for event in verified_events:
                self.assertEqual(event["breaker_reason"], event["fault"])
                self.assertTrue(event["breaker_evidence"])
                self.assertTrue(all(event["checks"].values()))
                self.assertEqual(
                    event["recovery"]["scope"], "isolated_fixture_only"
                )
            self.assertEqual(events[-1]["event"], "suite_finished")

    def test_unrelated_source_and_output_sentinels_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            unrelated_source = root / "production-source.sentinel"
            unrelated_output = root / "production-output.sentinel"
            unrelated_source.write_bytes(b"source-must-not-change")
            unrelated_output.write_bytes(b"output-must-not-change")

            marker = _source_revision_marker(root)
            result = guardrail_fi.run_fault_suite(
                root / "isolated-server-logs",
                worker_source_revision_file=marker,
            )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(
                unrelated_source.read_bytes(), b"source-must-not-change"
            )
            self.assertEqual(
                unrelated_output.read_bytes(), b"output-must-not-change"
            )

    def test_repeated_runs_use_distinct_timestamped_artifact_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_root = Path(raw) / "server-logs"
            marker = _source_revision_marker(raw)
            first = guardrail_fi.run_fault_suite(
                log_root,
                worker_source_revision_file=marker,
            )
            second = guardrail_fi.run_fault_suite(
                log_root,
                worker_source_revision_file=marker,
            )

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertNotEqual(first["log_path"], second["log_path"])
            self.assertTrue(Path(first["log_path"]).is_file())
            self.assertTrue(Path(second["log_path"]).is_file())

    def test_success_cli_writes_one_summary_line(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            marker = _source_revision_marker(raw)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = guardrail_fi.main(
                    [
                        "--log-dir",
                        str(Path(raw) / "server-logs"),
                        "--source-revision-file",
                        str(marker),
                    ]
                )

            self.assertEqual(exit_code, 0)
            lines = stdout.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["breaker_tests_passed"], 7)
            self.assertTrue(Path(payload["result_path"]).is_file())
            self.assertNotIn("case_results", payload)
            self.assertNotIn("log_tail", payload)

    def test_failed_log_tail_is_strictly_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "events.jsonl"
            log_path.write_text(
                "\n".join(f"{index:02d}:" + ("x" * 1000) for index in range(50)),
                encoding="utf-8",
            )

            tail = guardrail_fi.bounded_log_tail(
                log_path, max_lines=20, max_chars=8000
            )

            self.assertLessEqual(len(tail), 20)
            self.assertLessEqual(sum(map(len, tail)), 8000)
            self.assertTrue(tail[0].startswith("30:"))


if __name__ == "__main__":
    unittest.main()
