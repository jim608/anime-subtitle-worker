from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import threading
import unittest

import m2_observation_store as observation_store


class M2FrozenObservationTests(unittest.TestCase):
    """Contract tests for the durable, no-backfill 20-job cohort."""

    GATE_START = 1_725_460_000.0

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.config = SimpleNamespace(
            work_path=self.root / "work",
            scanner_state_path=self.root / "work" / "scanner_state.sqlite3",
            m2_server_canary_observation_output_dir=self.root / "observations",
        )
        Path(self.config.work_path).mkdir(parents=True, exist_ok=True)
        Path(self.config.m2_server_canary_observation_output_dir).mkdir(
            parents=True,
            exist_ok=True,
        )
        self.runtime_state = self._runtime_state()
        self.connection = observation_store.connect_observation_database(self.config)
        self.addCleanup(self._close_connection)
        with observation_store.immediate_transaction(self.connection):
            self.gate = observation_store.create_gate(
                self.connection,
                self.runtime_state,
                now=self.GATE_START,
            )

    def _close_connection(self) -> None:
        if getattr(self, "connection", None) is not None:
            self.connection.close()
            self.connection = None

    def _runtime_state(
        self,
        *,
        worker_sha: str = "1" * 40,
        config_fingerprint: str = "sha256:" + "4" * 64,
        pre_gate_attempts: int = 8,
    ) -> dict[str, object]:
        baseline = {
            "worker_commit_sha": worker_sha,
            "webui_commit_sha": "2" * 40,
            "worker_image_id": "sha256:" + "3" * 64,
            "worker_container_id": "5" * 64,
            "worker_runtime_instance_fingerprint": "sha256:" + "6" * 64,
            "configuration_fingerprint": config_fingerprint,
            "decision_schema_version": 1,
            "eligibility_policy_version": observation_store.ELIGIBILITY_POLICY_VERSION,
        }
        return {
            "status": "ARMED",
            "gate_start_at": "2024-09-04T11:46:40Z",
            "gate_start_epoch": self.GATE_START,
            "gate_baseline_version": "m2-guardrail-v1:test-baseline",
            "baseline": baseline,
            "gate": {"target": 20, "progress": 0},
            "pre_gate_running": {"attempt_count": pre_gate_attempts},
        }

    @staticmethod
    def _job_hash(job_identity: str) -> str:
        return hashlib.sha256(job_identity.encode("utf-8")).hexdigest()

    def _claim(
        self,
        number: int,
        *,
        connection: sqlite3.Connection | None = None,
        job_identity: str | None = None,
        claim_identity: str | None = None,
        eligible: bool = True,
        reason: str = "eligible",
    ) -> dict[str, object]:
        active_connection = connection or self.connection
        obligation = job_identity or f"job-{number:03d}"
        attempt = claim_identity or f"attempt-{number:03d}"
        with observation_store.immediate_transaction(active_connection):
            return observation_store.enroll_claim(
                active_connection,
                self.runtime_state,
                claim_identity=attempt,
                gate_job_identity=obligation,
                input_fingerprint=f"source-{obligation}",
                claimed_at=self.GATE_START + number + 1,
                processing_strategy="TRANSLATE_FROM_JAPANESE",
                eligible=eligible,
                eligibility_reason=reason,
                now=self.GATE_START + number + 1,
            )

    def _fill_cohort(self) -> None:
        for number in range(20):
            result = self._claim(number)
            self.assertTrue(result["enrolled"])
            self.assertEqual(result["ordinal"], number + 1)

    @staticmethod
    def _strict_qualification(
        *,
        qualified: bool = True,
        missing: tuple[str, ...] = (),
    ) -> dict[str, object]:
        evidence = {
            "final_state_completed": True,
            "output_parse_pass": True,
            "hard_qc_pass": True,
            "hallucination_validation_pass": True,
            "source_checksum_unchanged": True,
            "no_duplicate_job": True,
            "no_duplicate_publish": True,
            "decision_record_complete": True,
            "stage_checkpoint_history_complete": True,
            "runtime_commit_matches_gate_baseline": True,
            "no_unresolved_retry_quarantine_fallback": True,
        }
        for key in missing:
            evidence.pop(key, None)
        return {
            "qualified": qualified,
            "evidence": evidence,
            "missing_evidence": list(missing),
            "reason_codes": [] if qualified and not missing else ["strict_evidence_incomplete"],
        }

    @staticmethod
    def _completed_outcome() -> dict[str, object]:
        return {
            "terminal_status": "COMPLETED",
            "processing_strategy": "TRANSLATE_FROM_JAPANESE",
            "reason_code": "strict_delivery_verified",
        }

    def _settle(
        self,
        number: int,
        *,
        connection: sqlite3.Connection | None = None,
        outcome: dict[str, object] | None = None,
        qualification: dict[str, object] | None = None,
    ) -> dict[str, object]:
        active_connection = connection or self.connection
        with observation_store.immediate_transaction(active_connection):
            return observation_store.record_terminal_evidence(
                active_connection,
                gate_job_identity=f"job-{number:03d}",
                claim_identity=f"attempt-{number:03d}",
                outcome=outcome or self._completed_outcome(),
                qualification=qualification or self._strict_qualification(),
                now=self.GATE_START + 100 + number,
            )

    def _latest_gate(self) -> dict[str, object]:
        gate = observation_store.latest_gate(self.connection)
        self.assertIsNotNone(gate)
        return gate  # type: ignore[return-value]

    def _v1_database_with_history(self) -> tuple[sqlite3.Connection, str]:
        """Build the previous schema shape while retaining real gate history."""

        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys=ON")
        observation_store.ensure_observation_schema(connection)
        with observation_store.immediate_transaction(connection):
            gate = observation_store.create_gate(
                connection,
                self.runtime_state,
                now=self.GATE_START,
            )
            observation_store.enroll_claim(
                connection,
                self.runtime_state,
                claim_identity="v1-attempt",
                gate_job_identity="v1-job",
                input_fingerprint="v1-source",
                claimed_at=self.GATE_START + 1,
                processing_strategy="TRANSLATE_FROM_JAPANESE",
                eligible=True,
                eligibility_reason="eligible",
                now=self.GATE_START + 1,
            )
            observation_store.record_terminal_evidence(
                connection,
                gate_job_identity="v1-job",
                claim_identity="v1-attempt",
                outcome=self._completed_outcome(),
                qualification=self._strict_qualification(),
                now=self.GATE_START + 2,
            )
            observation_store.enroll_claim(
                connection,
                self.runtime_state,
                claim_identity="v1-supplemental-attempt",
                gate_job_identity="v1-supplemental-job",
                input_fingerprint="v1-supplemental-source",
                claimed_at=self.GATE_START + 3,
                processing_strategy="TRANSLATE_FROM_JAPANESE",
                eligible=False,
                eligibility_reason="fixture_excluded",
                now=self.GATE_START + 3,
            )

        trigger_names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'trg_m2_observation_%'"
        ).fetchall()
        for (name,) in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DROP TABLE m2_observation_result_events")
        connection.execute(
            "ALTER TABLE m2_observation_gates DROP COLUMN worker_container_id"
        )
        connection.execute(
            "ALTER TABLE m2_observation_gates "
            "DROP COLUMN worker_runtime_instance_fingerprint"
        )
        connection.execute(
            "UPDATE m2_observation_gates SET schema_version=1 WHERE gate_id=?",
            (gate["gate_id"],),
        )
        connection.execute(
            "UPDATE m2_observation_meta SET value='1' WHERE key='schema_version'"
        )
        connection.commit()
        return connection, str(gate["gate_id"])

    def _assert_nonpassing_slot_is_not_backfilled(
        self,
        *,
        outcome: dict[str, object],
        expected_state: str,
        expected_incident: str | None = None,
    ) -> None:
        self._fill_cohort()
        result = self._settle(
            6,
            outcome=outcome,
            qualification=self._strict_qualification(qualified=False),
        )
        self.assertTrue(result["settled"])
        excluded = self._claim(20)
        self.assertFalse(excluded["enrolled"])
        self.assertEqual(excluded["eligibility_reason"], "frozen_cohort_full")
        gate = self._latest_gate()
        self.assertEqual(gate["enrolled_count"], 20)
        self.assertEqual(gate["settled_count"], 1)
        member = observation_store.member_for_job(
            self.connection,
            str(gate["gate_id"]),
            self._job_hash("job-006"),
        )
        self.assertIsNotNone(member)
        self.assertEqual(member["ordinal"], 7)
        self.assertEqual(member["final_state"], expected_state)
        self.assertEqual(member["strict_verified"], 0)
        if expected_incident is not None:
            flags = json.loads(str(member["incident_flags_json"]))
            self.assertTrue(flags[expected_incident])

    def test_01_first_twenty_eligible_claims_are_frozen_in_ordinal_order(self) -> None:
        self._fill_cohort()
        members = observation_store.members_for_gate(
            self.connection,
            str(self.gate["gate_id"]),
        )
        self.assertEqual([row["ordinal"] for row in members], list(range(1, 21)))
        self.assertEqual(
            [row["job_id"] for row in members],
            [self._job_hash(f"job-{number:03d}") for number in range(20)],
        )
        self.assertEqual(self._latest_gate()["enrolled_count"], 20)

    def test_02_twenty_first_claim_is_supplemental_and_never_a_member(self) -> None:
        self._fill_cohort()
        result = self._claim(20)
        self.assertFalse(result["enrolled"])
        self.assertEqual(result["eligibility_reason"], "frozen_cohort_full")
        supplemental = self.connection.execute(
            "SELECT exclusion_reason FROM m2_observation_supplemental WHERE gate_id=? AND job_id=?",
            (self.gate["gate_id"], self._job_hash("job-020")),
        ).fetchone()
        self.assertEqual(supplemental, ("frozen_cohort_full",))
        self.assertIsNone(
            observation_store.member_for_job(
                self.connection,
                str(self.gate["gate_id"]),
                self._job_hash("job-020"),
            )
        )

    def test_03_failed_member_keeps_its_slot_without_backfill(self) -> None:
        self._assert_nonpassing_slot_is_not_backfilled(
            outcome={
                "terminal_status": "FAILED",
                "failed": True,
                "processing_strategy": "TRANSLATE_FROM_JAPANESE",
                "error_code": "model_timeout",
            },
            expected_state="FAILED",
        )

    def test_04_needs_review_member_keeps_its_slot_without_backfill(self) -> None:
        self._assert_nonpassing_slot_is_not_backfilled(
            outcome={
                "terminal_status": "NEEDS_REVIEW",
                "processing_strategy": "TRANSLATE_FROM_JAPANESE",
                "reason_code": "manual_review_required",
            },
            expected_state="NEEDS_REVIEW",
        )

    def test_05_quarantined_member_keeps_its_slot_without_backfill(self) -> None:
        self._assert_nonpassing_slot_is_not_backfilled(
            outcome={
                "terminal_status": "QUARANTINED",
                "quarantined": True,
                "processing_strategy": "TRANSLATE_FROM_JAPANESE",
                "reason_code": "quarantined_output",
            },
            expected_state="QUARANTINED",
            expected_incident="quarantined",
        )

    def test_06_hallucination_blocked_member_keeps_its_slot_without_backfill(self) -> None:
        self._assert_nonpassing_slot_is_not_backfilled(
            outcome={
                "terminal_status": "FAILED",
                "failed": True,
                "hallucination_blocked": True,
                "processing_strategy": "TRANSLATE_FROM_JAPANESE",
                "reason_code": "hallucination_blocked",
            },
            expected_state="FAILED",
            expected_incident="hallucination_blocked",
        )

    def test_07_pre_gate_attempt_is_preserved_as_supplemental_only(self) -> None:
        with observation_store.immediate_transaction(self.connection):
            self.connection.execute(
                "CREATE TABLE ai_delivery_attempts(obligation_id TEXT, attempt_id TEXT, started_at REAL)"
            )
            self.connection.execute(
                "INSERT INTO ai_delivery_attempts(obligation_id, attempt_id, started_at) VALUES(?, ?, ?)",
                ("pre-gate-obligation", "old-attempt", self.GATE_START - 10),
            )
        result = self._claim(
            50,
            job_identity="pre-gate-obligation",
            claim_identity="retry-after-gate",
        )
        self.assertFalse(result["enrolled"])
        self.assertEqual(result["eligibility_reason"], "job_started_before_gate")
        self.assertEqual(self._latest_gate()["pre_gate_attempt_count"], 8)
        self.assertEqual(self._latest_gate()["enrolled_count"], 0)

    def test_08_duplicate_claim_for_same_job_reuses_original_slot(self) -> None:
        original = self._claim(
            0,
            job_identity="stable-obligation",
            claim_identity="attempt-one",
        )
        duplicate = self._claim(
            1,
            job_identity="stable-obligation",
            claim_identity="attempt-two",
        )
        self.assertEqual(original["ordinal"], 1)
        self.assertEqual(duplicate["ordinal"], 1)
        self.assertTrue(duplicate["duplicate_claim_ignored"])
        self.assertEqual(self._latest_gate()["enrolled_count"], 1)
        members = observation_store.members_for_gate(
            self.connection,
            str(self.gate["gate_id"]),
        )
        self.assertEqual(len(members), 1)

    def test_09_concurrent_workers_enroll_exactly_twenty_unique_members(self) -> None:
        database_path = Path(self.config.scanner_state_path)
        barrier = threading.Barrier(32)

        def concurrent_claim(number: int) -> dict[str, object]:
            connection = sqlite3.connect(database_path, timeout=30)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                barrier.wait(timeout=30)
                with observation_store.immediate_transaction(connection):
                    return observation_store.enroll_claim(
                        connection,
                        self.runtime_state,
                        claim_identity=f"concurrent-attempt-{number:03d}",
                        gate_job_identity=f"concurrent-job-{number:03d}",
                        input_fingerprint=f"concurrent-source-{number:03d}",
                        claimed_at=self.GATE_START + number + 1,
                        processing_strategy="TRANSLATE_FROM_JAPANESE",
                        eligible=True,
                        eligibility_reason="eligible",
                    )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=32) as executor:
            results = list(executor.map(concurrent_claim, range(32)))
        self.assertEqual(sum(bool(item["enrolled"]) for item in results), 20)
        self.assertEqual(
            sum(item.get("eligibility_reason") == "frozen_cohort_full" for item in results),
            12,
        )
        gate = self._latest_gate()
        members = observation_store.members_for_gate(
            self.connection,
            str(gate["gate_id"]),
        )
        self.assertEqual(gate["enrolled_count"], 20)
        self.assertEqual(len(members), 20)
        self.assertEqual(sorted(row["ordinal"] for row in members), list(range(1, 21)))
        self.assertEqual(len({row["job_id"] for row in members}), 20)

    def test_10_module_restart_preserves_membership_and_next_ordinal(self) -> None:
        for number in range(5):
            self._claim(number)
        self._close_connection()
        importlib.reload(observation_store)
        self.connection = observation_store.connect_observation_database(self.config)
        result = self._claim(5)
        self.assertEqual(result["ordinal"], 6)
        self.assertEqual(self._latest_gate()["enrolled_count"], 6)

    def test_11_database_reopen_preserves_gate_and_all_member_evidence(self) -> None:
        for number in range(3):
            self._claim(number)
        self._settle(0)
        gate_id = str(self.gate["gate_id"])
        self._close_connection()
        self.connection = observation_store.connect_observation_database(self.config)
        gate = observation_store.gate_by_id(self.connection, gate_id)
        self.assertEqual(gate["enrolled_count"], 3)
        self.assertEqual(gate["settled_count"], 1)
        member = observation_store.member_for_job(
            self.connection,
            gate_id,
            self._job_hash("job-000"),
        )
        self.assertEqual(member["final_state"], "COMPLETED")
        self.assertEqual(member["strict_verified"], 1)
        self.assertTrue(member["terminal_evidence_sha256"])

    def test_12_same_baseline_restart_is_idempotent_and_does_not_invalidate(self) -> None:
        self._claim(0)
        gate_id = str(self.gate["gate_id"])
        self._close_connection()
        self.connection = observation_store.connect_observation_database(self.config)
        with observation_store.immediate_transaction(self.connection):
            gate = observation_store.create_gate(
                self.connection,
                self.runtime_state,
                now=self.GATE_START + 100,
            )
        self.assertEqual(gate["gate_id"], gate_id)
        self.assertEqual(gate["status"], observation_store.ACTIVE)
        self.assertEqual(gate["enrolled_count"], 1)

    def test_13_worker_sha_drift_atomically_invalidates_active_gate(self) -> None:
        actual = self._actual_snapshot()
        actual["worker_sha"] = "9" * 40
        error = None
        with observation_store.immediate_transaction(self.connection):
            try:
                observation_store.validate_active_runtime(
                    self.connection,
                    self.runtime_state,
                    actual_snapshot=actual,
                    now=self.GATE_START + 1,
                )
            except observation_store.ObservationStoreError as exc:
                error = exc
        self.assertIsNotNone(error)
        self.assertEqual(error.reason_code, observation_store.INVALIDATED_RUNTIME)
        gate = self._latest_gate()
        self.assertEqual(gate["status"], observation_store.INVALIDATED_RUNTIME)
        evidence = json.loads(str(gate["invalidation_evidence_json"]))
        self.assertEqual(evidence["reason_code"], "runtime_drift_worker_sha")
        self.assertEqual(evidence["expected"]["worker_sha"], "1" * 40)
        self.assertEqual(evidence["actual"]["worker_sha"], "9" * 40)

    def test_14_configuration_drift_atomically_invalidates_active_gate(self) -> None:
        actual = self._actual_snapshot()
        actual["configuration_fingerprint"] = "sha256:" + "f" * 64
        error = None
        with observation_store.immediate_transaction(self.connection):
            try:
                observation_store.validate_active_runtime(
                    self.connection,
                    self.runtime_state,
                    actual_snapshot=actual,
                    now=self.GATE_START + 1,
                )
            except observation_store.ObservationStoreError as exc:
                error = exc
        self.assertIsNotNone(error)
        self.assertEqual(error.reason_code, observation_store.INVALIDATED_RUNTIME)
        gate = self._latest_gate()
        self.assertEqual(gate["status"], observation_store.INVALIDATED_RUNTIME)
        evidence = json.loads(str(gate["invalidation_evidence_json"]))
        self.assertEqual(
            evidence["reason_code"],
            "runtime_drift_configuration_fingerprint",
        )

    def _actual_snapshot(self) -> dict[str, object]:
        baseline = self.runtime_state["baseline"]
        return {
            "baseline_version": self.runtime_state["gate_baseline_version"],
            "worker_sha": baseline["worker_commit_sha"],
            "webui_sha": baseline["webui_commit_sha"],
            "container_image_id": baseline["worker_image_id"],
            "worker_container_id": baseline["worker_container_id"],
            "worker_runtime_instance_fingerprint": baseline[
                "worker_runtime_instance_fingerprint"
            ],
            "configuration_fingerprint": baseline["configuration_fingerprint"],
            "decision_schema_version": baseline["decision_schema_version"],
            "eligibility_policy_version": baseline["eligibility_policy_version"],
        }

    def test_15_invalidated_gate_rejects_claim_without_touching_queue_or_checkpoint(self) -> None:
        checkpoint = self.root / "checkpoint.sentinel"
        checkpoint.write_text("preserve-me", encoding="utf-8")
        with observation_store.immediate_transaction(self.connection):
            self.connection.execute(
                "CREATE TABLE production_queue(job_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
            )
            self.connection.execute(
                "INSERT INTO production_queue(job_id, status) VALUES('production-job', 'queued')"
            )
            observation_store.invalidate_active_gate(
                self.connection,
                observation_store.INVALIDATED_RUNTIME,
                evidence={"reason_code": "runtime_drift_worker_sha"},
                now=self.GATE_START + 1,
            )
        with self.assertRaisesRegex(
            observation_store.ObservationStoreError,
            "observation_gate_invalidated",
        ):
            self._claim(0)
        queue_row = self.connection.execute(
            "SELECT status FROM production_queue WHERE job_id='production-job'"
        ).fetchone()
        self.assertEqual(queue_row, ("queued",))
        self.assertEqual(checkpoint.read_text(encoding="utf-8"), "preserve-me")

    def test_16_missing_terminal_evidence_can_never_be_strict_pass(self) -> None:
        self._claim(0)
        result = self._settle(
            0,
            qualification=self._strict_qualification(
                qualified=True,
                missing=("output_parse_pass",),
            ),
        )
        self.assertFalse(result["strictly_qualified"])
        member = observation_store.member_for_job(
            self.connection,
            str(self.gate["gate_id"]),
            self._job_hash("job-000"),
        )
        self.assertEqual(member["strict_verified"], 0)
        self.assertEqual(member["output_parse_result"], "MISSING")

    def test_17_summary_is_journaled_once_only_after_all_twenty_are_terminal(self) -> None:
        self._fill_cohort()
        for number in range(19):
            self._settle(number)
        gate = self._latest_gate()
        self.assertEqual(gate["status"], observation_store.ACTIVE)
        self.assertEqual(gate["settled_count"], 19)
        self.assertFalse(gate["summary_payload_json"])
        self.assertEqual(observation_store.publish_pending_summaries(self.config), [])

        final = self._settle(19)
        self.assertTrue(final["emission_pending"])
        gate = self._latest_gate()
        self.assertEqual(gate["status"], observation_store.SETTLED)
        self.assertEqual(gate["settled_count"], 20)
        emitted = observation_store.publish_pending_summaries(self.config)
        self.assertEqual(emitted, [f"{gate['gate_id']}.json"])
        self.assertEqual(observation_store.publish_pending_summaries(self.config), [])
        summary_path = Path(self.config.m2_server_canary_observation_output_dir) / emitted[0]
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["contract"], "m2-frozen-observation-summary-v1")
        self.assertEqual(summary["gate_progress"], "20/20")
        self.assertEqual(summary["claimed_after_gate_start_count"], 20)
        self.assertEqual(summary["enrolled"], "20/20")
        self.assertEqual(summary["settled"], "20/20")
        self.assertEqual(summary["checkpoint_resume_count"], 0)
        self.assertEqual(summary["oom_event_count"], 0)

        post_gate_claim = self._claim(20)
        self.assertEqual(post_gate_claim["status"], observation_store.SETTLED)
        self.assertFalse(post_gate_claim["enrolled"])
        self.assertEqual(
            post_gate_claim["eligibility_reason"],
            "frozen_cohort_settled",
        )
        settled_gate = self._latest_gate()
        self.assertEqual(settled_gate["enrolled_count"], 20)
        self.assertEqual(settled_gate["settled_count"], 20)
        self.assertEqual(
            len(
                observation_store.members_for_gate(
                    self.connection,
                    str(settled_gate["gate_id"]),
                )
            ),
            20,
        )
        supplemental = self.connection.execute(
            "SELECT exclusion_reason FROM m2_observation_supplemental WHERE gate_id=? AND job_id=?",
            (settled_gate["gate_id"], self._job_hash("job-020")),
        ).fetchone()
        self.assertEqual(supplemental, ("frozen_cohort_settled",))

    def test_18_restart_and_terminal_replay_do_not_duplicate_summary(self) -> None:
        self._fill_cohort()
        for number in range(20):
            self._settle(number)
        first_emission = observation_store.publish_pending_summaries(self.config)
        self.assertEqual(len(first_emission), 1)
        gate_id = str(self.gate["gate_id"])
        self._close_connection()
        importlib.reload(observation_store)
        self.connection = observation_store.connect_observation_database(self.config)
        replay = self._settle(19)
        self.assertTrue(replay["duplicate_observation_ignored"])
        self.assertEqual(observation_store.publish_pending_summaries(self.config), [])
        gate = observation_store.gate_by_id(self.connection, gate_id)
        self.assertEqual(gate["settled_count"], 20)
        self.assertIsNotNone(gate["summary_emitted_at"])
        outputs = list(Path(self.config.m2_server_canary_observation_output_dir).glob("*.json"))
        self.assertEqual(len(outputs), 1)

    def test_19_v1_to_v2_migration_is_atomic_and_preserves_gate_history(self) -> None:
        connection, gate_id = self._v1_database_with_history()
        self.addCleanup(connection.close)
        before_member = connection.execute(
            "SELECT ordinal, final_state, terminal_evidence_sha256 "
            "FROM m2_observation_gate_jobs WHERE gate_id=?",
            (gate_id,),
        ).fetchone()
        before_supplemental = connection.execute(
            "SELECT job_id, exclusion_reason FROM m2_observation_supplemental "
            "WHERE gate_id=?",
            (gate_id,),
        ).fetchone()

        observation_store.ensure_observation_schema(connection)

        self.assertEqual(
            connection.execute(
                "SELECT value FROM m2_observation_meta WHERE key='schema_version'"
            ).fetchone(),
            (str(observation_store.SCHEMA_VERSION),),
        )
        gate_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(m2_observation_gates)"
            ).fetchall()
        }
        self.assertIn("worker_container_id", gate_columns)
        self.assertIn("worker_runtime_instance_fingerprint", gate_columns)
        event_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(m2_observation_result_events)"
            ).fetchall()
        }
        self.assertIn("event_payload_json", event_columns)
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'trg_m2_observation_%'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "trg_m2_observation_terminal_write_once",
                "trg_m2_observation_result_event_no_update",
                "trg_m2_observation_result_event_no_delete",
            }.issubset(trigger_names)
        )
        migrated_gate = connection.execute(
            "SELECT schema_version, worker_container_id, "
            "worker_runtime_instance_fingerprint, enrolled_count, settled_count "
            "FROM m2_observation_gates WHERE gate_id=?",
            (gate_id,),
        ).fetchone()
        self.assertEqual(migrated_gate, (1, "", "", 1, 1))
        self.assertEqual(
            connection.execute(
                "SELECT ordinal, final_state, terminal_evidence_sha256 "
                "FROM m2_observation_gate_jobs WHERE gate_id=?",
                (gate_id,),
            ).fetchone(),
            before_member,
        )
        self.assertEqual(
            connection.execute(
                "SELECT job_id, exclusion_reason FROM m2_observation_supplemental "
                "WHERE gate_id=?",
                (gate_id,),
            ).fetchone(),
            before_supplemental,
        )

    def test_20_future_and_invalid_schema_fail_without_partial_migration(self) -> None:
        future = sqlite3.connect(":memory:")
        self.addCleanup(future.close)
        observation_store.ensure_observation_schema(future)
        future.execute(
            "UPDATE m2_observation_meta SET value=? WHERE key='schema_version'",
            (str(observation_store.SCHEMA_VERSION + 1),),
        )
        future.commit()
        with self.assertRaisesRegex(
            observation_store.ObservationStoreError,
            "observation_schema_version_unsupported",
        ):
            observation_store.ensure_observation_schema(future)
        self.assertEqual(
            future.execute(
                "SELECT value FROM m2_observation_meta WHERE key='schema_version'"
            ).fetchone(),
            (str(observation_store.SCHEMA_VERSION + 1),),
        )

        invalid, gate_id = self._v1_database_with_history()
        self.addCleanup(invalid.close)
        invalid.execute(
            "ALTER TABLE m2_observation_gate_jobs DROP COLUMN failure_review_reason"
        )
        invalid.commit()
        with self.assertRaisesRegex(
            observation_store.ObservationStoreError,
            "observation_schema_migration_invalid_missing_columns_",
        ):
            observation_store.ensure_observation_schema(invalid)
        self.assertEqual(
            invalid.execute(
                "SELECT value FROM m2_observation_meta WHERE key='schema_version'"
            ).fetchone(),
            ("1",),
        )
        rolled_back_gate_columns = {
            str(row[1])
            for row in invalid.execute(
                "PRAGMA table_info(m2_observation_gates)"
            ).fetchall()
        }
        self.assertNotIn("worker_container_id", rolled_back_gate_columns)
        self.assertNotIn(
            "worker_runtime_instance_fingerprint",
            rolled_back_gate_columns,
        )
        self.assertEqual(
            invalid.execute(
                "SELECT enrolled_count, settled_count FROM m2_observation_gates "
                "WHERE gate_id=?",
                (gate_id,),
            ).fetchone(),
            (1, 1),
        )
        self.assertIsNone(
            invalid.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='m2_observation_result_events'"
            ).fetchone()
        )

    def _assert_result_event_tamper_blocks_summary(
        self,
        *,
        tamper_sql: str,
        tamper_value: str,
    ) -> None:
        self._fill_cohort()
        with observation_store.immediate_transaction(self.connection):
            observation_store.reserve_result_event(
                self.connection,
                gate_job_identity="job-000",
                claim_identity="tamper-event",
                observed_state="FAILED",
                event_payload={
                    "normalized_outcome": {"output_parse_failure": True},
                },
                now=self.GATE_START + 50,
            )
            self.connection.execute(
                "DROP TRIGGER trg_m2_observation_result_event_no_update"
            )
            self.connection.execute(tamper_sql, (tamper_value,))
        for number in range(19):
            self._settle(number)
        with self.assertRaisesRegex(
            observation_store.ObservationStoreError,
            "observation_result_event_digest_mismatch",
        ):
            self._settle(19)
        gate = self._latest_gate()
        self.assertEqual(gate["status"], observation_store.ACTIVE)
        self.assertEqual(gate["settled_count"], 19)
        self.assertFalse(gate["summary_payload_json"])
        final_member = observation_store.member_for_job(
            self.connection,
            str(gate["gate_id"]),
            self._job_hash("job-019"),
        )
        self.assertIsNone(final_member["terminal_at"])

    def test_21_result_event_payload_tamper_blocks_summary_settlement(self) -> None:
        self._assert_result_event_tamper_blocks_summary(
            tamper_sql=(
                "UPDATE m2_observation_result_events "
                "SET event_payload_json=? WHERE observed_state='FAILED'"
            ),
            tamper_value='{"normalized_outcome":{}}',
        )

    def test_22_result_event_digest_tamper_blocks_summary_settlement(self) -> None:
        self._assert_result_event_tamper_blocks_summary(
            tamper_sql=(
                "UPDATE m2_observation_result_events "
                "SET event_sha256=? WHERE observed_state='FAILED'"
            ),
            tamper_value="0" * 64,
        )

    def test_23_retry_incidents_remain_counted_after_strict_success(self) -> None:
        self._fill_cohort()
        with observation_store.immediate_transaction(self.connection):
            failed_event = observation_store.reserve_result_event(
                self.connection,
                gate_job_identity="job-000",
                claim_identity="attempt-000-output-parse-retry",
                observed_state="FAILED",
                event_payload={
                    "normalized_outcome": {
                        "terminal_status": "FAILED",
                        "output_parse_failure": True,
                        "breaker_tripped": True,
                    },
                },
                now=self.GATE_START + 50,
            )
            success_event = observation_store.reserve_result_event(
                self.connection,
                gate_job_identity="job-000",
                claim_identity="attempt-000-success",
                observed_state="COMPLETED",
                event_payload={
                    "normalized_outcome": {"terminal_status": "COMPLETED"},
                },
                now=self.GATE_START + 51,
            )
        self.assertTrue(failed_event["reserved"])
        self.assertTrue(success_event["reserved"])
        for number in range(20):
            self._settle(number)

        gate = self._latest_gate()
        summary = json.loads(str(gate["summary_payload_json"]))
        self.assertEqual(summary["strict_verified_count"], 20)
        self.assertEqual(summary["output_parse_failure_count"], 1)
        self.assertEqual(summary["breaker_trip_count"], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM m2_observation_result_events WHERE gate_id=?",
                (gate["gate_id"],),
            ).fetchone(),
            (2,),
        )

    def test_24_supplemental_result_events_are_excluded_from_frozen_summary(self) -> None:
        pre_gate = self._claim(
            30,
            job_identity="pre-gate-job",
            claim_identity="pre-gate-attempt",
            eligible=False,
            reason="running_before_gate_start",
        )
        self.assertFalse(pre_gate["enrolled"])
        self._fill_cohort()
        overflow = self._claim(20)
        self.assertFalse(overflow["enrolled"])
        with observation_store.immediate_transaction(self.connection):
            for gate_job_identity, claim_identity, incident in (
                ("pre-gate-job", "pre-gate-result", "output_parse_failure"),
                ("job-020", "overflow-result", "oom_event"),
            ):
                reserved = observation_store.reserve_result_event(
                    self.connection,
                    gate_job_identity=gate_job_identity,
                    claim_identity=claim_identity,
                    observed_state="FAILED",
                    event_payload={
                        "normalized_outcome": {
                            "terminal_status": "FAILED",
                            incident: True,
                            "breaker_tripped": True,
                        },
                    },
                    now=self.GATE_START + 60,
                )
                self.assertTrue(reserved["reserved"])
                self.assertFalse(reserved["enrolled"])
        for number in range(20):
            self._settle(number)

        summary = json.loads(str(self._latest_gate()["summary_payload_json"]))
        self.assertEqual(summary["output_parse_failure_count"], 0)
        self.assertEqual(summary["oom_event_count"], 0)
        self.assertEqual(summary["breaker_trip_count"], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM m2_observation_result_events"
            ).fetchone(),
            (2,),
        )

    def test_25_terminal_evidence_digest_tamper_blocks_summary_settlement(self) -> None:
        self._fill_cohort()
        for number in range(19):
            self._settle(number)
        with observation_store.immediate_transaction(self.connection):
            self.connection.execute(
                "DROP TRIGGER trg_m2_observation_terminal_write_once"
            )
            self.connection.execute(
                """
                UPDATE m2_observation_gate_jobs
                SET strict_verified=0
                WHERE gate_id=? AND ordinal=1
                """,
                (self.gate["gate_id"],),
            )

        with self.assertRaisesRegex(
            observation_store.ObservationStoreError,
            "terminal_evidence_digest_mismatch",
        ):
            self._settle(19)

        gate = self._latest_gate()
        self.assertEqual(gate["status"], observation_store.ACTIVE)
        self.assertEqual(gate["settled_count"], 19)
        self.assertIsNone(gate["summary_ready_at"])

    def test_26_same_attempt_replay_rejects_intrinsic_payload_conflict(self) -> None:
        self._claim(0)
        with observation_store.immediate_transaction(self.connection):
            observation_store.reserve_result_event(
                self.connection,
                gate_job_identity="job-000",
                claim_identity="same-attempt",
                observed_state="RETRYING",
                event_payload={
                    "outcome": {"terminal_status": "RETRYING", "failed": True},
                    "normalized_outcome": {
                        "terminal_status": "RETRYING",
                        "output_parse_failure": True,
                    },
                    "qualification": {"qualified": False},
                    "breaker": {"oom_streak": 1},
                },
                now=self.GATE_START + 50,
            )

        with self.assertRaisesRegex(
            observation_store.ObservationStoreError,
            "result_event_payload_conflict",
        ):
            with observation_store.immediate_transaction(self.connection):
                observation_store.reserve_result_event(
                    self.connection,
                    gate_job_identity="job-000",
                    claim_identity="same-attempt",
                    observed_state="COMPLETED",
                    event_payload={
                        "outcome": {"terminal_status": "COMPLETED"},
                        "normalized_outcome": {"terminal_status": "COMPLETED"},
                        "qualification": {"qualified": True},
                        "breaker": {"oom_streak": 99},
                    },
                    now=self.GATE_START + 51,
                )


if __name__ == "__main__":
    unittest.main()
