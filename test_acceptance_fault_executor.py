from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness import _verify_structured_fault_evidence
from acceptance_fault_executor import (
    AcceptanceFaultAlreadyClaimedError,
    AcceptanceFaultExecutorError,
    FAULT_SCENARIOS,
    FAULT_TRIGGERS,
    attempt_binding_digest,
    load_acceptance_fault_executor,
    terminal_attempt_row_sha256,
)
from safe_files import sha256_file


class AcceptanceFaultExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.input_root = self.root / "input"
        self.work = self.root / "work"
        self.input_root.mkdir()
        self.work.mkdir()
        self.run_id = "accrun_" + "1" * 48
        self.plan = self._plan()
        self.plan_path = self.work / "acceptance-plan.json"
        self.plan_path.write_text(
            json.dumps(self.plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self.plan_sha256 = sha256_file(self.plan_path)
        self.config = SimpleNamespace(
            input_path=self.input_root,
            work_path=self.work,
            acceptance_queue_lane_enabled=True,
            acceptance_queue_lane_plan_path=str(self.plan_path),
            acceptance_fault_execution_enabled=True,
            acceptance_fault_execution_run_id=self.run_id,
            acceptance_fault_execution_plan_sha256=self.plan_sha256,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_default_off_does_not_load_or_create_state(self) -> None:
        self.config.acceptance_fault_execution_enabled = False

        self.assertIsNone(load_acceptance_fault_executor(self.config))
        self.assertFalse((self.plan_path.parent / "fault-state").exists())
        self.assertFalse((self.plan_path.parent / "fault-evidence").exists())

    def test_load_requires_exact_lane_run_and_plan_hash(self) -> None:
        executor = load_acceptance_fault_executor(self.config)
        self.assertIsNotNone(executor)
        self.assertEqual(set(FAULT_SCENARIOS), {fault.scenario for fault in executor.faults})

        self.config.acceptance_fault_execution_run_id = "accrun_" + "2" * 48
        with self.assertRaisesRegex(AcceptanceFaultExecutorError, "run id"):
            load_acceptance_fault_executor(self.config)
        self.config.acceptance_fault_execution_run_id = self.run_id
        self.config.acceptance_fault_execution_plan_sha256 = "f" * 64
        with self.assertRaisesRegex(AcceptanceFaultExecutorError, "SHA-256"):
            load_acceptance_fault_executor(self.config)

    def test_load_rejects_missing_fixed_scenario(self) -> None:
        self.plan["cases"][0]["faults"] = []
        self._rewrite_plan_and_config()

        with self.assertRaisesRegex(AcceptanceFaultExecutorError, "exactly 10"):
            load_acceptance_fault_executor(self.config)

    def test_runtime_context_claim_is_o_excl_and_hash_bound(self) -> None:
        executor = load_acceptance_fault_executor(self.config)
        fault = executor.faults[0]
        attempt = self._attempt(
            fault.obligation_id,
            number=1,
            status="running",
            started_at=1_800_000_000.0,
        )
        context = SimpleNamespace(
            contract="anime-acceptance-attempt-context-v1",
            schema_version=1,
            run_id=self.run_id,
            plan_sha256=self.plan_sha256,
            case_id=fault.case_id,
            fault_id=fault.fault_id,
            fault_scenario=fault.scenario,
            canonical_path=fault.canonical_path,
            obligation_id=fault.obligation_id,
            delivery_attempt_id=attempt["attempt_id"],
            attempt_number=attempt["attempt_number"],
            started_at=attempt["started_at"],
        )

        claim = executor.claim_fault_from_runtime_context(
            context,
            claimed_at=attempt["started_at"] + 0.25,
        )

        self.assertTrue(claim.state_path.is_file())
        self.assertEqual(
            attempt_binding_digest(
                plan_sha256=self.plan_sha256,
                acceptance_run_id=self.run_id,
                case_id=fault.case_id,
                fault_id=fault.fault_id,
                obligation_id=fault.obligation_id,
                attempt=attempt,
            ),
            claim.attempt_binding_sha256,
        )
        with self.assertRaises(AcceptanceFaultAlreadyClaimedError):
            executor.claim_fault_from_runtime_context(
                context,
                claimed_at=attempt["started_at"] + 0.5,
            )

    def test_claim_rejects_missing_obligation_id(self) -> None:
        executor = load_acceptance_fault_executor(self.config)
        fault = executor.faults[0]
        attempt = self._attempt(
            fault.obligation_id,
            number=1,
            status="running",
            started_at=1_800_000_000.0,
        )
        attempt.pop("obligation_id")

        with self.assertRaisesRegex(
            AcceptanceFaultExecutorError,
            "attempt obligation does not match",
        ):
            executor.claim_fault(fault.fault_id, attempt)

        self.assertFalse((executor.state_root / f"{fault.fault_id}.json").exists())

    def test_v3_evidence_binds_exact_failed_and_recovery_rows(self) -> None:
        executor = load_acceptance_fault_executor(self.config)
        fault = executor.faults[0]
        started = 1_800_000_000.0
        running = self._attempt(
            fault.obligation_id,
            number=1,
            status="running",
            started_at=started,
        )
        claim = executor.claim_fault(
            fault.fault_id,
            running,
            claimed_at=started + 0.25,
        )
        failed = {
            **running,
            "status": "retryable_failure",
            "stage": "worker",
            "error_code": "acceptance_worker_kill",
            "detail": "isolated child exited after the planned claim",
            "finished_at": started + 0.5,
        }
        recovered = self._attempt(
            fault.obligation_id,
            number=2,
            status="succeeded",
            started_at=started + 0.75,
            finished_at=started + 2.0,
            stage="delivery_verification",
        )

        evidence_path = executor.write_recovery_evidence(
            fault.fault_id,
            failed,
            recovered,
            checkpoint="delivery_verified",
        )
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(self.plan_sha256, payload["attempt_binding"]["plan_sha256"])
        self.assertEqual(
            terminal_attempt_row_sha256(
                failed,
                obligation_id=fault.obligation_id,
                acceptance_run_id=self.run_id,
            ),
            payload["attempt_binding"]["failed_attempt"]["row_sha256"],
        )
        observation = {
            "injected_at": claim.claimed_at,
            "recovered_at": recovered["finished_at"],
        }
        planned_fault = next(
            item
            for case in self.plan["cases"]
            for item in case["faults"]
            if item["fault_id"] == fault.fault_id
        )
        self.assertEqual(
            "",
            _verify_structured_fault_evidence(
                evidence_path,
                suite_id=self.run_id,
                case_id=fault.case_id,
                obligation_id=fault.obligation_id,
                fault=planned_fault,
                observation=observation,
                plan_schema_version=3,
                acceptance_run_id=self.run_id,
                plan_sha256=self.plan_sha256,
                attempts=[failed, recovered],
            ),
        )
        tampered_failed = {**failed, "detail": "tampered terminal detail"}
        self.assertIn(
            "failed attempt row SHA-256 mismatch",
            _verify_structured_fault_evidence(
                evidence_path,
                suite_id=self.run_id,
                case_id=fault.case_id,
                obligation_id=fault.obligation_id,
                fault=planned_fault,
                observation=observation,
                plan_schema_version=3,
                acceptance_run_id=self.run_id,
                plan_sha256=self.plan_sha256,
                attempts=[tampered_failed, recovered],
            ),
        )

    def test_completed_fault_requires_completed_checkpoint(self) -> None:
        executor = load_acceptance_fault_executor(self.config)
        fault = next(
            item for item in executor.faults if item.scenario == "mux_process_crash"
        )
        started = 1_800_001_000.0
        running = self._attempt(
            fault.obligation_id,
            number=1,
            status="running",
            started_at=started,
        )
        executor.claim_fault(fault.fault_id, running, claimed_at=started + 0.1)
        failed = {
            **running,
            "status": "retryable_failure",
            "stage": "completed_delivery",
            "error_code": "acceptance_mux_process_crash",
            "detail": "ffmpeg child killed",
            "finished_at": started + 0.2,
        }
        recovered = self._attempt(
            fault.obligation_id,
            number=2,
            status="succeeded",
            started_at=started + 0.3,
            finished_at=started + 1,
        )

        with self.assertRaisesRegex(
            AcceptanceFaultExecutorError,
            "completed_delivery_committed",
        ):
            executor.write_recovery_evidence(
                fault.fault_id,
                failed,
                recovered,
                checkpoint="delivery_verified",
            )

    def test_evidence_o_excl_race_preserves_competing_payload(self) -> None:
        executor = load_acceptance_fault_executor(self.config)
        fault = executor.faults[0]
        started = 1_800_002_000.0
        running = self._attempt(
            fault.obligation_id,
            number=1,
            status="running",
            started_at=started,
        )
        executor.claim_fault(fault.fault_id, running, claimed_at=started + 0.1)
        failed = {
            **running,
            "status": "retryable_failure",
            "stage": "worker",
            "error_code": "acceptance_worker_kill",
            "detail": "isolated child exited",
            "finished_at": started + 0.2,
        }
        recovered = self._attempt(
            fault.obligation_id,
            number=2,
            status="succeeded",
            started_at=started + 0.3,
            finished_at=started + 1.0,
        )
        winner: dict[str, dict] = {}

        def competing_writer(path: Path, payload: dict) -> None:
            competing = json.loads(json.dumps(payload))
            competing["recovery"]["checkpoint"] = "competing_checkpoint"
            winner["payload"] = competing
            path.write_text(
                json.dumps(competing, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            raise FileExistsError(path)

        with patch(
            "acceptance_fault_executor._exclusive_write_json",
            side_effect=competing_writer,
        ):
            with self.assertRaisesRegex(
                AcceptanceFaultExecutorError,
                "already exists with different content",
            ):
                executor.write_recovery_evidence(
                    fault.fault_id,
                    failed,
                    recovered,
                    checkpoint="delivery_verified",
                )

        evidence_path = executor.evidence_root / f"{fault.fault_id}.json"
        self.assertEqual(
            winner["payload"],
            json.loads(evidence_path.read_text(encoding="utf-8")),
        )

    def _rewrite_plan_and_config(self) -> None:
        self.plan_path.write_text(
            json.dumps(self.plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self.plan_sha256 = sha256_file(self.plan_path)
        self.config.acceptance_fault_execution_plan_sha256 = self.plan_sha256

    def _plan(self) -> dict:
        route_by_scenario = {
            "worker_kill": "japanese_audio_asr",
            "translation_timeout": "japanese_subtitle_translation",
            "asr_process_crash": "japanese_audio_asr",
            "gpu_oom": "japanese_audio_asr",
            "model_unavailable": "japanese_subtitle_translation",
            "output_publish_interrupt": "existing_zh_tw",
            "temporary_io_error": "zh_cn_opencc",
            "temporary_database_busy": "existing_zh_tw",
            "mux_process_crash": "zh_cn_opencc",
            "completed_publish_interrupt": "existing_zh_tw",
        }
        cases = []
        for index in range(100):
            scenario = FAULT_SCENARIOS[index] if index < len(FAULT_SCENARIOS) else ""
            route = route_by_scenario.get(scenario, "existing_zh_tw")
            path = self.input_root / f"series-{index:03d}" / f"episode-{index:03d}.mkv"
            faults = []
            if scenario:
                faults.append(
                    {
                        "fault_id": f"fault-{index:02d}-{scenario}",
                        "scenario": scenario,
                        "trigger": FAULT_TRIGGERS[scenario],
                    }
                )
            cases.append(
                {
                    "case_id": f"case-{index:03d}",
                    "media": {
                        "canonical_path": str(path.resolve()),
                        "media_size": index + 1,
                        "media_mtime_ns": 1_800_000_000_000_000_000 + index,
                        "media_fingerprint": f"{index + 1:064x}",
                        "policy_revision": "a" * 64,
                        "obligation_id": f"aiobl_{index + 1:064x}",
                        "source_sha256": f"{index + 1001:064x}",
                    },
                    "expected_route": route,
                    "strata": {},
                    "completed_delivery": {},
                    "faults": faults,
                }
            )
        return {
            "contract": "anime-unattended-acceptance-plan-v1",
            "schema_version": 3,
            "suite_id": self.run_id,
            "run_id": self.run_id,
            "nonce": "b" * 64,
            "created_at": 1_800_000_000.0,
            "pre_admission": {},
            "cases": cases,
        }

    def _attempt(
        self,
        obligation_id: str,
        *,
        number: int,
        status: str,
        started_at: float,
        finished_at: float | None = None,
        stage: str = "",
    ) -> dict:
        attempt_id = "aiatt_" + hashlib.sha256(
            f"{obligation_id}:{number}".encode("utf-8")
        ).hexdigest()
        return {
            "attempt_id": attempt_id,
            "obligation_id": obligation_id,
            "acceptance_run_id": self.run_id,
            "attempt_number": number,
            "status": status,
            "stage": stage,
            "error_code": "",
            "detail": "",
            "started_at": started_at,
            "finished_at": finished_at,
        }


if __name__ == "__main__":
    unittest.main()
