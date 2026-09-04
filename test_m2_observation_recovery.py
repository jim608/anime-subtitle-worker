from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest import mock

from acceptance_queue_lane import (
    ACCEPTANCE_QUEUE_TARGET_COUNT,
    AcceptanceQueueTarget,
)
import m2_observation_store as observation_store
import m2_production_observation as observation
import main as main_module
import scanner as scanner_module
from m2_strict_observation import strict_evidence_template
from scan_state import ScanStateStore, ai_delivery_identity


class M2ObservationRecoveryTests(unittest.TestCase):
    """Integration contracts for crash recovery and replay-safe observation."""

    GATE_START = 1_725_460_000.0
    POLICY_REVISION = "policy-v1"

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.input_path = self.root / "input"
        self.work_path = self.root / "work"
        self.log_path = self.root / "logs"
        self.output_path = self.root / "observations"
        for path in (
            self.input_path,
            self.work_path,
            self.log_path,
            self.output_path,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.config = SimpleNamespace(
            input_path=self.input_path,
            work_path=self.work_path,
            log_path=self.log_path,
            scanner_state_path="scanner_state.sqlite3",
            scanner_cache_enabled=True,
            scanner_queue_enabled=True,
            acceptance_queue_lane_enabled=False,
            video_extensions=[".mkv"],
            m2_server_canary_observer_enabled=True,
            m2_server_canary_circuit_breaker_enabled=True,
            m2_server_canary_observation_gate_size=20,
            m2_server_canary_observation_state_path="m2-observation-state.json",
            m2_server_canary_observation_output_dir=self.output_path,
            m2_server_canary_circuit_breaker_state_path="m2-breaker.json",
            m2_server_canary_repeated_oom_threshold=2,
            m2_server_canary_identical_failure_threshold=2,
            m2_guardrail_runtime_state_path="m2-runtime.json",
            m2_guardrail_source_revision_file=self.work_path / ".source-revision",
            source_decision_schema_version=1,
            source_decision_version="source-decision-v1",
            auto_ai_max_attempts=3,
        )
        Path(self.config.m2_guardrail_source_revision_file).write_text(
            "7" * 64 + "\n",
            encoding="utf-8",
        )
        self.runtime_state = self._runtime_state()
        connection = observation_store.connect_observation_database(self.config)
        try:
            with observation_store.immediate_transaction(connection):
                self.gate = observation_store.create_gate(
                    connection,
                    self.runtime_state,
                    now=self.GATE_START,
                )
        finally:
            connection.close()
        self.runtime_state["gate"]["gate_id"] = str(self.gate["gate_id"])
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
        self.addCleanup(self._reset_process_breaker)

    def _reset_process_breaker(self) -> None:
        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False

    def _runtime_state(self) -> dict[str, object]:
        baseline = {
            "worker_commit_sha": "1" * 40,
            "webui_commit_sha": "2" * 40,
            "worker_image_id": "sha256:" + "3" * 64,
            "worker_container_id": "4" * 64,
            "worker_runtime_instance_fingerprint": "sha256:" + "5" * 64,
            "configuration_fingerprint": "sha256:" + "6" * 64,
            "decision_schema_version": 1,
            "eligibility_policy_version": observation_store.ELIGIBILITY_POLICY_VERSION,
        }
        return {
            "status": "ARMED",
            "gate_start_at": "2024-09-04T11:46:40Z",
            "gate_start_epoch": self.GATE_START,
            "gate_baseline_version": "m2-guardrail-v1:recovery-test",
            "baseline": baseline,
            "gate": {
                "target": 20,
                "progress": 0,
                "gate_id": "",
                "eligibility_policy_version": observation_store.ELIGIBILITY_POLICY_VERSION,
            },
            "pre_gate_running": {"attempt_count": 0, "attempt_keys": []},
        }

    @contextmanager
    def _armed_runtime(self):
        result = {"status": "ARMED", "state": self.runtime_state}
        with (
            mock.patch(
                "m2_guardrail_runtime.runtime_guardrail_status",
                return_value=result,
            ),
            mock.patch(
                "m2_guardrail_runtime.load_runtime_state",
                return_value=self.runtime_state,
            ),
        ):
            yield

    def _seed_running_member(
        self,
        name: str,
        *,
        line_retranslation: bool = False,
        completed_heartbeat: bool = False,
    ) -> dict[str, object]:
        video = self.input_path / f"{name}.mkv"
        video.write_bytes(b"isolated-media")
        stat = video.stat()
        identity = ai_delivery_identity(
            video,
            media_size=stat.st_size,
            media_mtime_ns=stat.st_mtime_ns,
            policy_revision=self.POLICY_REVISION,
        )
        state = ScanStateStore.from_config(self.config)
        try:
            state.begin_immediate()
            state.upsert_ai_queue_candidate(video, stat.st_mtime_ns)
            state.mark_ai_queue_running(video)
            obligation = state.ensure_ai_delivery_obligation(
                video,
                media_size=stat.st_size,
                media_mtime_ns=stat.st_mtime_ns,
                policy_revision=self.POLICY_REVISION,
                eligible_at=self.GATE_START + 1,
                source="line_retranslation" if line_retranslation else "queue_claim",
                obligation_id=str(identity["obligation_id"]),
            )
            attempt = state.begin_ai_delivery_attempt(
                str(obligation["obligation_id"]),
                started_at=self.GATE_START + 10,
            )
            observation_store.enroll_claim(
                state.observation_connection,
                self.runtime_state,
                claim_identity=str(attempt["attempt_id"]),
                gate_job_identity=str(obligation["obligation_id"]),
                input_fingerprint=str(identity["media_fingerprint"]),
                claimed_at=float(attempt["started_at"]),
                processing_strategy="ASR_JA_AUDIO",
                eligible=True,
                eligibility_reason="eligible",
                now=self.GATE_START + 10,
            )
            if line_retranslation:
                state.observation_connection.execute(
                    "UPDATE ai_candidate_queue SET source='line_retranslation' WHERE path=?",
                    (str(video.resolve()),),
                )
                state.update_ai_job_stage(
                    video,
                    "line_retranslation",
                    "running",
                    "isolated line repair",
                )
            elif completed_heartbeat:
                state.update_ai_job_stage(
                    video,
                    "complete",
                    "ok",
                    "isolated completed heartbeat",
                )
            state.commit()
        finally:
            state.close()
        return {
            "video": video,
            "obligation_id": str(obligation["obligation_id"]),
            "attempt_id": str(attempt["attempt_id"]),
            "attempt_started_at": float(attempt["started_at"]),
        }

    def _recovery_delivery_evidence(self, attempt_started_at: float) -> dict[str, object]:
        return {
            "manifest_path": str(self.work_path / "manifest.json"),
            "manifest_sha256": "a" * 64,
            "verified_at": attempt_started_at + 1,
            "verification": {
                "required_outputs_complete": True,
                "hashes_verified": True,
                "quality_gates_passed": True,
                "publication_marker_absent": True,
                "media_identity_matched": True,
                "policy_revision_matched": True,
                "manifest_schema_version": 2,
                "delivery_contract": "ai-delivery-v1",
                "attempt_started_at": attempt_started_at,
                "publication_semantics_verified": True,
                "publication_contract": "ai-publication-semantics-v2",
                "expected_policy_revision": self.POLICY_REVISION,
                "manifest_policy_revision": self.POLICY_REVISION,
                "publication_kind": "adopted_zh_tw",
                "output_languages": ["zh-TW"],
            },
        }

    @staticmethod
    def _strict_result(final_state: str) -> dict[str, object]:
        completed = final_state == "COMPLETED"
        return {
            "outcome": {
                "verified_completed": completed,
                "failed": not completed,
                "terminal_status": final_state,
                "stage": "delivery_verification" if completed else "restart_recovery",
                "error_code": "" if completed else "worker_restarted",
                "reason_code": "succeeded" if completed else "review_required",
                "processing_strategy": "ASR_JA_AUDIO",
            },
            "evidence": strict_evidence_template(passed=completed),
            "processing_strategy": "ASR_JA_AUDIO",
        }

    def _member(self, obligation_id: str) -> dict[str, object]:
        connection = observation_store.connect_observation_database(self.config)
        try:
            member = observation_store.member_for_job(
                connection,
                str(self.gate["gate_id"]),
                hashlib.sha256(obligation_id.encode("utf-8")).hexdigest(),
            )
            self.assertIsNotNone(member)
            return member  # type: ignore[return-value]
        finally:
            connection.close()

    def _result_event(self, attempt_id: str) -> dict[str, object] | None:
        connection = observation_store.connect_observation_database(self.config)
        try:
            cursor = connection.execute(
                "SELECT * FROM m2_observation_result_events "
                "WHERE claim_identity_hash=?",
                (hashlib.sha256(attempt_id.encode("utf-8")).hexdigest(),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            columns = [str(item[0]) for item in cursor.description or []]
            return dict(zip(columns, row, strict=True))
        finally:
            connection.close()

    def _acceptance_targets(
        self,
        seeded: dict[str, object],
    ) -> tuple[AcceptanceQueueTarget, ...]:
        video = Path(seeded["video"])
        stat = video.stat()
        first_identity = ai_delivery_identity(
            video,
            media_size=stat.st_size,
            media_mtime_ns=stat.st_mtime_ns,
            policy_revision=self.POLICY_REVISION,
        )
        targets = [
            AcceptanceQueueTarget(
                ordinal=0,
                canonical_path=str(video.resolve()),
                media_size=stat.st_size,
                media_mtime_ns=stat.st_mtime_ns,
                media_fingerprint=str(first_identity["media_fingerprint"]),
                policy_revision=self.POLICY_REVISION,
                obligation_id=str(seeded["obligation_id"]),
                source_sha256="a" * 64,
            )
        ]
        for ordinal in range(1, ACCEPTANCE_QUEUE_TARGET_COUNT):
            dummy = self.input_path / f"acceptance-unused-{ordinal:03d}.mkv"
            media_size = ordinal + 100
            media_mtime_ns = self.GATE_START.__int__() * 1_000_000_000 + ordinal
            identity = ai_delivery_identity(
                dummy,
                media_size=media_size,
                media_mtime_ns=media_mtime_ns,
                policy_revision=self.POLICY_REVISION,
            )
            targets.append(
                AcceptanceQueueTarget(
                    ordinal=ordinal,
                    canonical_path=str(dummy.resolve()),
                    media_size=media_size,
                    media_mtime_ns=media_mtime_ns,
                    media_fingerprint=str(identity["media_fingerprint"]),
                    policy_revision=self.POLICY_REVISION,
                    obligation_id=str(identity["obligation_id"]),
                    source_sha256=f"{ordinal:064x}",
                )
            )
        return tuple(targets)

    def _attempt(self, attempt_id: str) -> dict[str, object]:
        state = ScanStateStore.from_config(self.config)
        try:
            attempt = state.get_ai_delivery_attempt(attempt_id)
            self.assertIsNotNone(attempt)
            return attempt  # type: ignore[return-value]
        finally:
            state.close()

    def _queue_status(self, video: Path) -> str:
        state = ScanStateStore.from_config(self.config)
        try:
            snapshot = state.ai_queue_candidate_snapshot(video)
            self.assertIsNotNone(snapshot)
            return str(snapshot["status"])
        finally:
            state.close()

    def _age_running_job(self, video: Path) -> None:
        state = ScanStateStore.from_config(self.config)
        try:
            state.begin_immediate()
            normalized_path = str(video.resolve())
            state.observation_connection.execute(
                "UPDATE ai_candidate_queue SET updated_at=?, running_at=? WHERE path=?",
                (self.GATE_START, self.GATE_START, normalized_path),
            )
            state.observation_connection.execute(
                "UPDATE ai_job_state SET updated_at=? WHERE path=?",
                (self.GATE_START, normalized_path),
            )
            state.commit()
        finally:
            state.close()

    def test_restart_verified_success_settles_member_in_recovery_transaction(self) -> None:
        seeded = self._seed_running_member(
            "recovered-success",
            completed_heartbeat=True,
        )
        recovery_evidence = self._recovery_delivery_evidence(
            float(seeded["attempt_started_at"])
        )
        with (
            self._armed_runtime(),
            mock.patch.object(
                main_module,
                "_verified_ai_delivery_evidence",
                return_value=recovery_evidence,
            ),
            mock.patch.object(
                main_module,
                "_record_terminal_m2_observation",
                side_effect=RuntimeError("isolated observation write failure"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "isolated observation write failure",
            ):
                main_module._requeue_previous_worker_running(
                    self.config,
                    mock.Mock(),
                )

        # Queue, delivery ledger, and gate evidence must share one commit.  A
        # failed observation write cannot strand a durable success outside the
        # frozen member table.
        self.assertEqual(self._attempt(str(seeded["attempt_id"]))["status"], "running")
        self.assertEqual(self._queue_status(Path(seeded["video"])), "running")
        self.assertIsNone(self._member(str(seeded["obligation_id"]))["terminal_at"])

        with (
            self._armed_runtime(),
            mock.patch.object(
                main_module,
                "_verified_ai_delivery_evidence",
                return_value=recovery_evidence,
            ),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                return_value=self._strict_result("COMPLETED"),
            ),
        ):
            main_module._requeue_previous_worker_running(self.config, mock.Mock())

        self.assertEqual(self._attempt(str(seeded["attempt_id"]))["status"], "succeeded")
        member = self._member(str(seeded["obligation_id"]))
        self.assertEqual(member["final_state"], "COMPLETED")
        self.assertIsNotNone(member["terminal_at"])

    def test_restart_interrupted_line_retranslation_settles_needs_review(self) -> None:
        seeded = self._seed_running_member(
            "recovered-line-retranslation",
            line_retranslation=True,
        )
        with (
            self._armed_runtime(),
            mock.patch.object(
                main_module,
                "_verified_ai_delivery_evidence",
                return_value=None,
            ),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                return_value=self._strict_result("NEEDS_REVIEW"),
            ),
        ):
            main_module._requeue_previous_worker_running(self.config, mock.Mock())

        self.assertEqual(
            self._attempt(str(seeded["attempt_id"]))["status"],
            "review_required",
        )
        member = self._member(str(seeded["obligation_id"]))
        self.assertEqual(member["final_state"], "NEEDS_REVIEW")
        self.assertIsNotNone(member["terminal_at"])

    def test_acceptance_restart_records_deferred_attempt_in_gate_transaction(self) -> None:
        seeded = self._seed_running_member("acceptance-restart")
        lane = SimpleNamespace(
            run_id="accrun_" + "a" * 48,
            targets=self._acceptance_targets(seeded),
        )
        with (
            self._armed_runtime(),
            mock.patch.object(
                main_module,
                "load_acceptance_queue_lane",
                return_value=lane,
            ),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                return_value=self._strict_result("RETRYING"),
            ),
        ):
            count = main_module._requeue_previous_worker_running(
                self.config,
                mock.Mock(),
            )

        self.assertEqual(count, 1)
        self.assertEqual(self._attempt(str(seeded["attempt_id"]))["status"], "deferred")
        self.assertEqual(self._queue_status(Path(seeded["video"])), "queued")
        member = self._member(str(seeded["obligation_id"]))
        self.assertEqual(member["current_state"], "RETRYING")
        self.assertIsNone(member["terminal_at"])
        event = self._result_event(str(seeded["attempt_id"]))
        self.assertIsNotNone(event)
        self.assertEqual(event["observed_state"], "RETRYING")

    def test_acceptance_stale_observation_failure_rolls_back_then_replays_once(self) -> None:
        seeded = self._seed_running_member("acceptance-stale")
        self._age_running_job(Path(seeded["video"]))
        lane = SimpleNamespace(
            run_id="accrun_" + "b" * 48,
            targets=self._acceptance_targets(seeded),
        )
        with (
            self._armed_runtime(),
            mock.patch.object(
                main_module,
                "load_acceptance_queue_lane",
                return_value=lane,
            ),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                side_effect=RuntimeError("isolated acceptance observation failure"),
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "isolated acceptance observation failure",
            ),
        ):
            main_module._requeue_stale_ai_running(self.config, mock.Mock())

        self.assertEqual(self._attempt(str(seeded["attempt_id"]))["status"], "running")
        self.assertEqual(self._queue_status(Path(seeded["video"])), "running")
        self.assertEqual(
            self._member(str(seeded["obligation_id"]))["current_state"],
            "CLAIMED",
        )
        self.assertIsNone(self._result_event(str(seeded["attempt_id"])))

        observation._PROCESS_LOCAL_CIRCUIT_OPEN = False
        observation.circuit_breaker_state_path(self.config).unlink(missing_ok=True)
        with (
            self._armed_runtime(),
            mock.patch.object(
                main_module,
                "load_acceptance_queue_lane",
                return_value=lane,
            ),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                return_value=self._strict_result("RETRYING"),
            ),
        ):
            count = main_module._requeue_stale_ai_running(self.config, mock.Mock())

        self.assertEqual(count, 1)
        self.assertEqual(self._attempt(str(seeded["attempt_id"]))["status"], "deferred")
        self.assertEqual(self._queue_status(Path(seeded["video"])), "queued")
        self.assertIsNotNone(self._result_event(str(seeded["attempt_id"])))

    def test_stale_requeue_interrupted_line_retranslation_settles_member(self) -> None:
        seeded = self._seed_running_member(
            "stale-line-retranslation",
            line_retranslation=True,
        )
        self._age_running_job(Path(seeded["video"]))
        with (
            self._armed_runtime(),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                return_value=self._strict_result("NEEDS_REVIEW"),
            ),
        ):
            state = ScanStateStore.from_config(self.config)
            try:
                state.begin_immediate()

                def observe(observed_path: Path, attempt_id: str) -> None:
                    observation.record_state_attempt_result(
                        self.config,
                        state,
                        observed_path,
                        attempt_id,
                        transaction_connection=state.observation_connection,
                    )

                count = state.requeue_stale_running(
                    60,
                    result_observer=observe,
                )
                state.commit()
            finally:
                state.close()

        self.assertEqual(count, 1)
        self.assertEqual(
            self._attempt(str(seeded["attempt_id"]))["status"],
            "review_required",
        )
        member = self._member(str(seeded["obligation_id"]))
        self.assertEqual(member["final_state"], "NEEDS_REVIEW")
        self.assertIsNotNone(member["terminal_at"])

    def test_scanner_stale_observer_failure_rolls_back_and_trips_breaker(self) -> None:
        seeded = self._seed_running_member(
            "scanner-stale-observer-failure",
            line_retranslation=True,
        )
        self._age_running_job(Path(seeded["video"]))

        with (
            mock.patch.object(
                scanner_module,
                "scan_config_signature",
                return_value="scan-v1",
            ),
            mock.patch.object(
                scanner_module,
                "processing_config_signature",
                return_value=self.POLICY_REVISION,
            ),
            mock.patch.object(
                scanner_module,
                "ai_inventory_root_signature",
                return_value="inventory-v1",
            ),
        ):
            scanner = scanner_module.VideoScanner(self.config, mock.Mock())

        with (
            self._armed_runtime(),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                side_effect=RuntimeError("isolated strict evidence failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "isolated strict evidence failure"),
        ):
            scanner.queued_candidates(max_candidates=1)

        # The terminal ledger mutation happens before the observer callback.
        # Any observer failure must therefore roll the entire scanner unit back.
        self.assertEqual(self._attempt(str(seeded["attempt_id"]))["status"], "running")
        self.assertEqual(self._queue_status(Path(seeded["video"])), "running")
        self.assertIsNone(self._member(str(seeded["obligation_id"]))["terminal_at"])
        self.assertTrue(observation.circuit_breaker_active(self.config))
        breaker_payload = json.loads(
            observation.circuit_breaker_state_path(self.config).read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            "observation_pipeline_failure",
            {
                str(item.get("reason_code") or "")
                for item in breaker_payload.get("reasons", [])
                if isinstance(item, dict)
            },
        )

    def test_ai_skip_terminalizes_enrolled_running_job_as_needs_review(self) -> None:
        seeded = self._seed_running_member("manual-skip")
        with (
            self._armed_runtime(),
            mock.patch(
                "m2_strict_runtime_evidence.build_m2_strict_runtime_evidence",
                return_value=self._strict_result("NEEDS_REVIEW"),
            ),
        ):
            main_module._execute_control_command(
                self.config,
                mock.Mock(),
                "ai.skip",
                str(seeded["video"]),
                {},
            )

        member = self._member(str(seeded["obligation_id"]))
        self.assertEqual(member["final_state"], "NEEDS_REVIEW")
        self.assertIsNotNone(member["terminal_at"])

    def test_summary_publish_race_reports_one_emitter(self) -> None:
        connection = observation_store.connect_observation_database(self.config)
        try:
            with observation_store.immediate_transaction(connection):
                for index in range(20):
                    observation_store.enroll_claim(
                        connection,
                        self.runtime_state,
                        claim_identity=f"summary-attempt-{index}",
                        gate_job_identity=f"summary-job-{index}",
                        input_fingerprint=f"summary-source-{index}",
                        claimed_at=self.GATE_START + index + 1,
                        processing_strategy="ASR_JA_AUDIO",
                        eligible=True,
                        eligibility_reason="eligible",
                    )
                qualification = {
                    "qualified": True,
                    "evidence": strict_evidence_template(passed=True),
                    "missing_evidence": [],
                    "failed_evidence": [],
                    "reason_codes": [],
                }
                for index in range(20):
                    observation_store.record_terminal_evidence(
                        connection,
                        gate_job_identity=f"summary-job-{index}",
                        claim_identity=f"summary-attempt-{index}",
                        outcome={
                            "terminal_status": "COMPLETED",
                            "processing_strategy": "ASR_JA_AUDIO",
                        },
                        qualification=qualification,
                    )
        finally:
            connection.close()

        barrier = threading.Barrier(2)
        original_write = observation_store.atomic_write_text

        def synchronized_write(path, content, *, encoding="utf-8"):
            barrier.wait(timeout=5)
            return original_write(path, content, encoding=encoding)

        with (
            mock.patch.object(
                observation_store,
                "atomic_write_text",
                side_effect=synchronized_write,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(
                executor.map(
                    lambda _index: observation_store.publish_pending_summaries(
                        self.config
                    ),
                    range(2),
                )
            )
        self.assertEqual(sum(len(result) for result in results), 1)

    def test_failed_attempt_replay_does_not_advance_failure_streaks(self) -> None:
        seeded = self._seed_running_member("failed-replay")
        outcome = {
            "verified_completed": False,
            "failed": True,
            "terminal_status": "FAILED",
            "stage": "resource_runtime",
            "error_code": "transient_oom",
            "reason_code": "failed",
            "oom_event": True,
            "processing_strategy": "ASR_JA_AUDIO",
        }
        with self._armed_runtime():
            first = observation.record_job_result(
                self.config,
                job_identity=str(seeded["attempt_id"]),
                gate_job_identity=str(seeded["obligation_id"]),
                outcome=outcome,
                strict_evidence=strict_evidence_template(passed=False),
            )
            replay = observation.record_job_result(
                self.config,
                job_identity=str(seeded["attempt_id"]),
                gate_job_identity=str(seeded["obligation_id"]),
                outcome=outcome,
                strict_evidence=strict_evidence_template(passed=False),
            )
        self.assertTrue(first["settled"])
        self.assertTrue(replay["duplicate_observation_ignored"])
        connection = observation_store.connect_observation_database(self.config)
        try:
            streaks = observation_store.meta_state(connection)
        finally:
            connection.close()
        self.assertEqual(streaks["oom_streak"], 1)
        self.assertEqual(streaks["identical_failure_streak"], 1)
        self.assertFalse(observation.circuit_breaker_active(self.config))

    def test_terminal_replay_ignores_later_ambient_breaker_state(self) -> None:
        seeded = self._seed_running_member("ambient-breaker-replay")
        first_outcome = {
            "verified_completed": True,
            "failed": False,
            "terminal_status": "COMPLETED",
            "stage": "delivery_verification",
            "reason_code": "succeeded",
            "processing_strategy": "ASR_JA_AUDIO",
            "breaker_tripped": False,
        }
        with self._armed_runtime():
            observation.record_job_result(
                self.config,
                job_identity=str(seeded["attempt_id"]),
                gate_job_identity=str(seeded["obligation_id"]),
                outcome=first_outcome,
                strict_evidence=strict_evidence_template(passed=True),
            )
            observation.trip_circuit_breaker(
                self.config,
                "insufficient_disk_space",
                evidence={"stage": "later_unrelated_event"},
            )
            replay = observation.record_job_result(
                self.config,
                job_identity=str(seeded["attempt_id"]),
                gate_job_identity=str(seeded["obligation_id"]),
                outcome={**first_outcome, "breaker_tripped": True},
                strict_evidence=strict_evidence_template(passed=True),
            )
        self.assertTrue(replay["duplicate_observation_ignored"])
        member = self._member(str(seeded["obligation_id"]))
        self.assertEqual(member["final_state"], "COMPLETED")
        self.assertEqual(member["strict_verified"], 1)

    def test_retry_then_success_summary_retains_prior_oom_event(self) -> None:
        seeded = self._seed_running_member("retry-then-success")
        with self._armed_runtime():
            observation.record_job_result(
                self.config,
                job_identity=str(seeded["attempt_id"]),
                gate_job_identity=str(seeded["obligation_id"]),
                outcome={
                    "terminal_status": "RETRYING",
                    "failed": True,
                    "stage": "resource_runtime",
                    "error_code": "transient_oom",
                    "oom_event": True,
                    "unresolved_retry": True,
                    "processing_strategy": "ASR_JA_AUDIO",
                },
                strict_evidence=strict_evidence_template(passed=False),
            )
            observation.record_job_result(
                self.config,
                job_identity="retry-then-success-attempt-2",
                gate_job_identity=str(seeded["obligation_id"]),
                outcome={
                    "terminal_status": "COMPLETED",
                    "verified_completed": True,
                    "stage": "delivery_verification",
                    "processing_strategy": "ASR_JA_AUDIO",
                },
                strict_evidence=strict_evidence_template(passed=True),
            )

        connection = observation_store.connect_observation_database(self.config)
        try:
            with observation_store.immediate_transaction(connection):
                qualification = {
                    "qualified": True,
                    "evidence": strict_evidence_template(passed=True),
                    "missing_evidence": [],
                    "failed_evidence": [],
                    "reason_codes": [],
                }
                for index in range(1, 20):
                    observation_store.enroll_claim(
                        connection,
                        self.runtime_state,
                        claim_identity=f"remaining-attempt-{index}",
                        gate_job_identity=f"remaining-job-{index}",
                        input_fingerprint=f"remaining-source-{index}",
                        claimed_at=self.GATE_START + 100 + index,
                        processing_strategy="ASR_JA_AUDIO",
                        eligible=True,
                        eligibility_reason="eligible",
                    )
                    observation_store.record_terminal_evidence(
                        connection,
                        gate_job_identity=f"remaining-job-{index}",
                        claim_identity=f"remaining-attempt-{index}",
                        outcome={
                            "terminal_status": "COMPLETED",
                            "processing_strategy": "ASR_JA_AUDIO",
                        },
                        qualification=qualification,
                    )
            gate = observation_store.gate_by_id(
                connection,
                str(self.gate["gate_id"]),
            )
            self.assertIsNotNone(gate)
            summary = json.loads(str(gate["summary_payload_json"]))
        finally:
            connection.close()
        self.assertEqual(summary["oom_event_count"], 1)


if __name__ == "__main__":
    unittest.main()
