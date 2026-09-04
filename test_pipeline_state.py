from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from test_support import configure_isolated_test_tempdir

configure_isolated_test_tempdir()

from pipeline_state import (
    InvalidPipelineTransition,
    PIPELINE_STATES,
    PipelineJobStore,
    PipelineStateConflict,
    PipelineStateError,
    TerminalPipelineStateError,
    legacy_stage_to_pipeline_state,
)
from scan_state import ScanStateStore


class PipelineJobStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.media = self.root / "episode.mkv"
        self.media.write_bytes(b"video-v1")
        self.database = self.root / "scanner_state.sqlite3"
        self.store = PipelineJobStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def test_final_artifact_path_rejects_temp_descendants(self) -> None:
        system_temp_root = self.root / "tmp"
        isolated_root = system_temp_root / "tmp-isolated-case"
        isolated_root.mkdir(parents=True)
        nested_artifact = isolated_root / "delivery.manifest.json"
        nested_artifact.write_text("verified", encoding="utf-8")
        direct_artifact = system_temp_root / "delivery.manifest.json"
        direct_artifact.write_text("not-final", encoding="utf-8")
        nested_staging = isolated_root / "staging"
        nested_staging.mkdir()
        staged_artifact = nested_staging / "delivery.manifest.json"
        staged_artifact.write_text("not-final", encoding="utf-8")

        self.assertFalse(PipelineJobStore._is_final_artifact_path(nested_artifact))
        self.assertFalse(PipelineJobStore._is_final_artifact_path(direct_artifact))
        self.assertFalse(PipelineJobStore._is_final_artifact_path(staged_artifact))

    def _observe(self, state: str = "STABILIZING", event_type: str = "created") -> tuple[dict, dict]:
        stat = self.media.stat()
        observation = self.store.observe_ingest(
            self.media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type=event_type,
            state=state,
            evidence={"test": True},
            confidence=0.99,
        )
        job = self.store.get_job(observation["job_id"])
        assert job is not None
        return observation, job

    def _queued(self) -> dict:
        _observation, _job = self._observe()
        observation, job = self._observe("QUEUED")
        self.assertEqual("QUEUED", observation["state"])
        return job

    def _strict_manifest(self, job: dict) -> tuple[Path, Path]:
        artifact = self.root / "episode.zh_tw.srt"
        artifact.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
        artifact_stat = artifact.stat()
        canonical_path = str(job["canonical_path"])
        media_identity = {
            "canonical_path": canonical_path,
            "media_mtime_ns": int(job["media_mtime_ns"]),
            "media_size": int(job["media_size"]),
        }
        media_fingerprint = hashlib.sha256(
            json.dumps(
                media_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        policy_revision = "test-policy-v1"
        obligation_id = "aiobl_" + hashlib.sha256(
            json.dumps(
                {**media_identity, "policy_revision": policy_revision},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": 2,
            "video": canonical_path,
            "media": {**media_identity, "media_fingerprint": media_fingerprint},
            "delivery": {
                "contract": "ai-delivery-v1",
                "obligation_id": obligation_id,
                "policy_revision": policy_revision,
                "verified_at": time.time(),
            },
            "quality_gate": {"passed": True, "contract": "test-quality-v1"},
            "publication_kind": "adopted_zh_tw",
            "publication": {
                "contract": "ai-publication-semantics-v2",
                "kind": "adopted_zh_tw",
                "output_languages": ["zh-TW"],
            },
            "outputs": [
                {
                    "path": str(artifact),
                    "size": artifact_stat.st_size,
                    "mtime_ns": artifact_stat.st_mtime_ns,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "language": "zh-TW",
                }
            ],
        }
        manifest = self.root / "delivery.manifest.json"
        manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return manifest, artifact

    def test_exact_states_and_first_observation_audit(self) -> None:
        self.assertEqual(14, len(PIPELINE_STATES))
        self.assertEqual(
            {
                "DISCOVERED", "STABILIZING", "ANALYZING", "QUEUED",
                "SUBTITLE_DETECTION", "ASR", "TRANSLATING", "POST_PROCESSING",
                "QC", "MUXING", "RETRYING", "NEEDS_REVIEW", "FAILED", "COMPLETED",
            },
            set(PIPELINE_STATES),
        )
        _observation, job = self._observe()
        transitions = self.store.list_transitions(job["job_id"])
        self.assertEqual(["DISCOVERED", "STABILIZING"], [row["to_state"] for row in transitions])
        self.assertEqual("media_discovered", transitions[0]["reason_code"])
        self.assertEqual("ingest_stabilization_started", transitions[1]["reason_code"])
        self.assertIn("canonical_path", transitions[1]["evidence"])

    def test_duplicate_observation_is_one_job_and_close_is_sticky(self) -> None:
        first, first_job = self._observe(event_type="closed_write")
        second, second_job = self._observe(event_type="modified")
        self.assertEqual(first_job["job_id"], second_job["job_id"])
        self.assertEqual(2, second["observation_count"])
        self.assertTrue(first["close_observed"])
        self.assertTrue(second["close_observed"])
        self.assertEqual("modified", second["event_type"])

    def test_growth_while_stabilizing_updates_revision_but_keeps_job(self) -> None:
        first, first_job = self._observe()
        with self.media.open("ab") as handle:
            handle.write(b"-growing")
        os.utime(self.media, None)
        second, second_job = self._observe()
        self.assertEqual(first_job["job_id"], second_job["job_id"])
        self.assertNotEqual(first["media_revision"], second["media_revision"])
        self.assertEqual(self.media.stat().st_size, second_job["media_size"])

    def test_hardlink_alias_reuses_job_when_supported(self) -> None:
        alias = self.root / "alias.mkv"
        try:
            os.link(self.media, alias)
        except OSError:
            self.skipTest("hard links unavailable")
        first, first_job = self._observe()
        stat = alias.stat()
        second = self.store.observe_ingest(
            alias, size=stat.st_size, mtime_ns=stat.st_mtime_ns, state="STABILIZING"
        )
        self.assertEqual(first_job["job_id"], second["job_id"])
        self.assertEqual(first["media_fingerprint"], second["media_fingerprint"])

    def test_changed_media_after_queue_fails_old_and_creates_new_job(self) -> None:
        old = self._queued()
        with self.media.open("ab") as handle:
            handle.write(b"-replacement")
        os.utime(self.media, None)
        _observation, new = self._observe()
        old_after = self.store.get_job(old["job_id"])
        assert old_after is not None
        self.assertEqual("FAILED", old_after["state"])
        self.assertEqual("media_changed_during_pipeline", old_after["terminal_reason_code"])
        self.assertNotEqual(old["job_id"], new["job_id"])

    def test_transition_cas_idempotency_and_terminal_protection(self) -> None:
        _observation, job = self._observe()
        updated = self.store.transition_job(
            job["job_id"],
            "ANALYZING",
            reason_code="probe_started",
            evidence={"probe": "ffprobe"},
            confidence=0.95,
            expected_state="STABILIZING",
            expected_version=job["state_version"],
            idempotency_key="probe-once",
        )
        duplicate = self.store.transition_job(
            job["job_id"],
            "ANALYZING",
            reason_code="probe_started",
            evidence={"probe": "ffprobe"},
            confidence=0.95,
            idempotency_key="probe-once",
        )
        self.assertEqual(updated["state_version"], duplicate["state_version"])
        with self.assertRaises(PipelineStateConflict):
            self.store.transition_job(
                job["job_id"], "QUEUED", reason_code="wrong_cas", evidence={},
                confidence=1, expected_version=1,
            )
        failed = self.store.transition_job(
            job["job_id"], "FAILED", reason_code="unsupported", evidence={"code": 1}, confidence=1
        )
        self.assertEqual("FAILED", failed["state"])
        with self.assertRaises(TerminalPipelineStateError):
            self.store.transition_job(
                job["job_id"], "RETRYING", reason_code="illegal_resume", evidence={}, confidence=1
            )

    def test_stage_attempt_checkpoint_retry_budget_and_structured_error(self) -> None:
        job = self._queued()
        attempt = self.store.start_stage_attempt(
            job["job_id"], "TRANSLATING", inputs={"source_hash": "abc"},
            model={"adapter": "test", "name": "model-a"}, retry_limit=1,
            timeout_seconds=30, checkpoint={"line": 0}, reason_code="translation_started",
            evidence={"route": "test"}, confidence=0.9,
        )
        checkpointed = self.store.checkpoint_stage(
            attempt["stage_attempt_id"], {"line": 5}, outputs={"partial_lines": 5},
            reason_code="batch_finished", evidence={"batch": 1}, confidence=0.9,
        )
        self.assertEqual({"line": 5}, checkpointed["checkpoint"])
        first_failure = self.store.finish_stage_attempt(
            attempt["stage_attempt_id"], "RETRYABLE_FAILURE", error_class="resource",
            error_code="gpu_oom", error={"requested_mb": 4096}, retry_after_seconds=2,
            reason_code="gpu_resource_failure", evidence={"free_mb": 100}, confidence=1,
        )
        self.assertEqual("RETRYABLE_FAILURE", first_failure["status"])
        self.assertEqual("RETRYING", self.store.get_job(job["job_id"])["state"])
        second = self.store.start_stage_attempt(
            job["job_id"], "TRANSLATING", inputs={"source_hash": "abc", "batch": "smaller"},
            retry_limit=1, reason_code="translation_retry", evidence={"fallback": "smaller_batch"},
            confidence=1,
        )
        self.store.finish_stage_attempt(
            second["stage_attempt_id"], "RETRYABLE_FAILURE", error_class="transient",
            error_code="timeout", reason_code="model_timeout", evidence={"seconds": 30}, confidence=1,
        )
        self.assertEqual("NEEDS_REVIEW", self.store.get_job(job["job_id"])["state"])

    def test_verified_success_reuse_requires_matching_valid_artifact(self) -> None:
        job = self._queued()
        artifact = self.root / "translated.srt"
        artifact.write_text("subtitle", encoding="utf-8")
        sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
        inputs = {"source_hash": "stable"}
        attempt = self.store.start_stage_attempt(
            job["job_id"], "TRANSLATING", inputs=inputs, reason_code="started",
            evidence={}, confidence=1,
        )
        self.store.finish_stage_attempt(
            attempt["stage_attempt_id"], "SUCCEEDED",
            outputs={"artifacts": [{"path": str(artifact), "size": artifact.stat().st_size, "sha256": sha}]},
            outputs_verified=True, reason_code="validated", evidence={"parser": "ok"}, confidence=1,
        )
        reusable = self.store.reusable_stage_attempt(job["job_id"], "TRANSLATING", inputs=inputs)
        self.assertIsNotNone(reusable)
        artifact.unlink()
        self.assertIsNone(
            self.store.reusable_stage_attempt(job["job_id"], "TRANSLATING", inputs=inputs)
        )
        empty_inputs = {"source_hash": "no-artifact"}
        empty = self.store.start_stage_attempt(
            job["job_id"], "TRANSLATING", inputs=empty_inputs, reason_code="started",
            evidence={}, confidence=1,
        )
        self.store.finish_stage_attempt(
            empty["stage_attempt_id"], "SUCCEEDED", outputs={}, outputs_verified=True,
            reason_code="legacy_claim", evidence={}, confidence=1,
        )
        self.assertIsNone(
            self.store.reusable_stage_attempt(job["job_id"], "TRANSLATING", inputs=empty_inputs)
        )
        temporary = self.root / "translated.srt.tmp"
        temporary.write_text("temporary", encoding="utf-8")
        temporary_inputs = {"source_hash": "temporary"}
        temp_attempt = self.store.start_stage_attempt(
            job["job_id"], "TRANSLATING", inputs=temporary_inputs,
            reason_code="started", evidence={}, confidence=1,
        )
        self.store.finish_stage_attempt(
            temp_attempt["stage_attempt_id"], "SUCCEEDED",
            outputs={"artifacts": [{"path": str(temporary)}]}, outputs_verified=True,
            reason_code="unpublished", evidence={}, confidence=1,
        )
        self.assertIsNone(
            self.store.reusable_stage_attempt(
                job["job_id"], "TRANSLATING", inputs=temporary_inputs
            )
        )

    def test_running_attempt_recovery_preserves_checkpoint_after_reopen(self) -> None:
        job = self._queued()
        attempt = self.store.start_stage_attempt(
            job["job_id"], "ASR", inputs={"audio_hash": "abc"}, model="whisper",
            retry_limit=1, timeout_seconds=10, checkpoint={"segment": 12},
            reason_code="asr_started", evidence={}, confidence=1,
        )
        self.store.commit()
        self.store.close()
        self.store = PipelineJobStore(self.database)
        recovered = self.store.recover_interrupted_stages(recover_all_running=True)
        self.assertEqual(1, len(recovered))
        self.assertEqual({"segment": 12}, recovered[0]["checkpoint"])
        self.assertEqual("whisper", recovered[0]["model"]["name"])
        self.assertEqual("INTERRUPTED", self.store._get_attempt(attempt["stage_attempt_id"])["status"])
        self.assertEqual("RETRYING", self.store.get_job(job["job_id"])["state"])
        self.assertEqual(1, self.store.get_job(job["job_id"])["retry_count"])
        self.assertEqual("wal", self.store._conn.execute("PRAGMA journal_mode").fetchone()[0].casefold())

    def test_repeated_restart_interruptions_exhaust_retry_budget(self) -> None:
        job = self._queued()
        first = self.store.start_stage_attempt(
            job["job_id"], "ASR", inputs={"audio_hash": "abc"}, retry_limit=1,
            reason_code="asr_started", evidence={}, confidence=1,
        )
        self.store.recover_interrupted_stages(recover_all_running=True)
        self.assertEqual("INTERRUPTED", self.store._get_attempt(first["stage_attempt_id"])["status"])
        self.assertEqual("RETRYING", self.store.get_job(job["job_id"])["state"])
        second = self.store.start_stage_attempt(
            job["job_id"], "ASR", inputs={"audio_hash": "abc"}, retry_limit=1,
            reason_code="asr_resumed", evidence={"restart": 1}, confidence=1,
        )
        self.store.recover_interrupted_stages(recover_all_running=True)
        self.assertEqual("INTERRUPTED", self.store._get_attempt(second["stage_attempt_id"])["status"])
        exhausted = self.store.get_job(job["job_id"])
        self.assertEqual("NEEDS_REVIEW", exhausted["state"])
        self.assertEqual(2, exhausted["retry_count"])

    def test_legacy_formal_stage_reuse_supersede_and_completion_gate(self) -> None:
        job = self._queued()
        self.assertEqual("SUBTITLE_DETECTION", legacy_stage_to_pipeline_state("worker"))
        worker = self.store.transition_legacy_stage(job["job_id"], "worker", "running")
        preflight = self.store.transition_legacy_stage(job["job_id"], "preflight", "running")
        self.assertEqual(worker["stage_attempt_id"], preflight["stage_attempt_id"])
        audio = self.store.transition_legacy_stage(job["job_id"], "audio", "running")
        worker_after = self.store._get_attempt(worker["stage_attempt_id"])
        self.assertEqual("SUCCEEDED", worker_after["status"])
        self.assertFalse(worker_after["outputs_verified"])
        transcription = self.store.transition_legacy_stage(
            job["job_id"], "transcription", "running", inputs={"different": True}
        )
        self.assertEqual(audio["stage_attempt_id"], transcription["stage_attempt_id"])
        self.store.transition_legacy_stage(job["job_id"], "complete", "complete")
        self.assertEqual("QC", self.store.get_job(job["job_id"])["state"])
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(job["job_id"], delivery_evidence={"output_path": "x"})
        with self.assertRaises(InvalidPipelineTransition):
            self.store.transition_job(
                job["job_id"], "COMPLETED", reason_code="bypass", evidence={}, confidence=1
            )
        strict_verification = {
            "required_outputs_complete": True,
            "hashes_verified": True,
            "publication_marker_absent": True,
            "media_identity_matched": True,
        }
        temporary = self.root / "delivery.manifest.tmp"
        temporary.write_text("temporary", encoding="utf-8")
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(temporary),
                    "manifest_sha256": hashlib.sha256(temporary.read_bytes()).hexdigest(),
                    "verification": strict_verification,
                },
            )
        manifest = self.root / "delivery.manifest.json"
        manifest.write_text("verified", encoding="utf-8")
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "verification": strict_verification,
                },
            )
        # Caller booleans and a matching manifest hash are not proof: arbitrary
        # text must never satisfy the formal completion gate.
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    "verification": strict_verification,
                },
            )
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "manifest_sha256": "not-a-sha256",
                    "verification": strict_verification,
                },
            )
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "manifest_sha256": "0" * 64,
                    "verification": strict_verification,
                },
            )
        staging = self.root / "staging"
        staging.mkdir()
        staged_manifest = staging / "delivery.manifest.json"
        staged_manifest.write_text("verified", encoding="utf-8")
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(staged_manifest),
                    "manifest_sha256": hashlib.sha256(staged_manifest.read_bytes()).hexdigest(),
                    "verification": strict_verification,
                },
            )
        manifest, artifact = self._strict_manifest(job)
        wrong_media_payload = json.loads(manifest.read_text(encoding="utf-8"))
        wrong_media_payload["media"]["media_size"] += 1
        manifest.write_text(
            json.dumps(wrong_media_payload, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    "verification": strict_verification,
                },
            )
        manifest, artifact = self._strict_manifest(job)
        original_manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
        artifact.write_text("tampered", encoding="utf-8")
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "manifest_sha256": original_manifest_hash,
                    "verification": strict_verification,
                },
            )
        manifest, _artifact = self._strict_manifest(job)
        marker = manifest.with_suffix(".publishing")
        marker.write_text("publishing", encoding="utf-8")
        with self.assertRaises(PipelineStateError):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    "verification": strict_verification,
                },
            )
        marker.unlink()
        completed = self.store.complete_job(
            job["job_id"],
            delivery_evidence={
                "manifest_path": str(manifest),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "verification": strict_verification,
            },
        )
        self.assertEqual("COMPLETED", completed["state"])

    def test_legacy_failed_classification_is_not_terminal_by_default(self) -> None:
        job = self._queued()
        self.store.transition_legacy_stage(job["job_id"], "translation", "running")
        failed = self.store.transition_legacy_stage(
            job["job_id"], "translation", "failed", error_class="legacy_unknown",
            error_code="legacy_unknown",
        )
        self.assertEqual("RETRYABLE_FAILURE", failed["status"])
        self.assertEqual("RETRYING", self.store.get_job(job["job_id"])["state"])

    def test_completion_rejects_same_stat_source_inode_replacement(self) -> None:
        job = self._queued()
        if job["identity_kind"] != "filesystem_object":
            self.skipTest("filesystem does not expose stable object identity")
        self.store.transition_legacy_stage(job["job_id"], "complete", "complete")
        manifest, _artifact = self._strict_manifest(job)
        before = self.media.stat()
        replacement = self.root / "replacement.mkv"
        replacement.write_bytes(b"VIDEO-v2")
        self.assertEqual(before.st_size, replacement.stat().st_size)
        os.utime(
            replacement,
            ns=(int(before.st_atime_ns), int(before.st_mtime_ns)),
        )
        os.replace(replacement, self.media)
        after = self.media.stat()
        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        if (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino):
            self.skipTest("filesystem replacement retained the same object identity")
        verification = {
            "required_outputs_complete": True,
            "hashes_verified": True,
            "publication_marker_absent": True,
            "media_identity_matched": True,
        }
        with self.assertRaisesRegex(PipelineStateError, "object identity changed"):
            self.store.complete_job(
                job["job_id"],
                delivery_evidence={
                    "manifest_path": str(manifest),
                    "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    "verification": verification,
                },
            )

    def test_completion_rehashes_manifest_and_outputs_before_commit(self) -> None:
        job = self._queued()
        self.store.transition_legacy_stage(job["job_id"], "complete", "complete")
        verification = {
            "required_outputs_complete": True,
            "hashes_verified": True,
            "publication_marker_absent": True,
            "media_identity_matched": True,
        }
        for target_kind in ("manifest", "output"):
            with self.subTest(target=target_kind):
                manifest, artifact = self._strict_manifest(job)
                target = manifest if target_kind == "manifest" else artifact
                before = target.stat()
                original_bytes = target.read_bytes()
                tampered_bytes = b"X" * len(original_bytes)
                original_gate = self.store._completion_manifest_gate

                def verify_then_tamper(snapshot, evidence):
                    verified = original_gate(snapshot, evidence)
                    target.write_bytes(tampered_bytes)
                    os.utime(
                        target,
                        ns=(int(before.st_atime_ns), int(before.st_mtime_ns)),
                    )
                    current = target.stat()
                    self.assertEqual(before.st_size, current.st_size)
                    self.assertEqual(before.st_mtime_ns, current.st_mtime_ns)
                    return verified

                with mock.patch.object(
                    self.store,
                    "_completion_manifest_gate",
                    side_effect=verify_then_tamper,
                ):
                    with self.assertRaisesRegex(PipelineStateError, "content changed"):
                        self.store.complete_job(
                            job["job_id"],
                            delivery_evidence={
                                "manifest_path": str(manifest),
                                "manifest_sha256": hashlib.sha256(
                                    manifest.read_bytes()
                                ).hexdigest(),
                                "verification": verification,
                            },
                        )

    def test_legacy_result_idempotency_does_not_consume_retry_twice(self) -> None:
        job = self._queued()
        self.store.transition_legacy_stage(
            job["job_id"], "worker", "running", retry_limit=2,
            idempotency_key="claim-1",
        )
        first = self.store.transition_legacy_stage(
            job["job_id"], "worker", "failed", retry_limit=2,
            error_class="transient", error_code="timeout",
            idempotency_key="result-1",
        )
        version = self.store.get_job(job["job_id"])["state_version"]
        duplicate = self.store.transition_legacy_stage(
            job["job_id"], "worker", "failed", retry_limit=2,
            error_class="transient", error_code="timeout",
            idempotency_key="result-1",
        )
        self.assertEqual(first["stage_attempt_id"], duplicate["stage_attempt_id"])
        self.assertEqual(version, self.store.get_job(job["job_id"])["state_version"])
        self.assertEqual(1, len(self.store.list_stage_attempts(job["job_id"])))
        with self.assertRaises(PipelineStateConflict):
            self.store.transition_legacy_stage(
                job["job_id"], "worker", "failed", retry_limit=2,
                error_class="resource", error_code="gpu_oom",
                idempotency_key="result-1",
            )

    def test_real_legacy_order_stays_forward_and_late_success_is_idempotent(self) -> None:
        job = self._queued()
        for stage, status in (
            ("worker", "running"),
            ("preflight", "running"),
            ("audio", "running"),
            ("transcription", "running"),
            ("postprocess", "running"),
            ("translation", "running"),
            ("translation", "ok"),
            ("opencc", "running"),
            ("ass_export", "running"),
            ("quality_check", "running"),
            ("ass_export", "ok"),
            ("complete", "complete"),
            ("mux", "running"),
            ("move_completed", "running"),
            ("delivery_verification", "ok"),
        ):
            self.store.transition_legacy_stage(job["job_id"], stage, status)
        current = self.store.get_job(job["job_id"])
        self.assertEqual("MUXING", current["state"])
        states = [row["to_state"] for row in self.store.list_transitions(job["job_id"])]
        self.assertNotIn("RETRYING", states)
        post_attempts = self.store.list_stage_attempts(job["job_id"], "POST_PROCESSING")
        self.assertEqual(1, len(post_attempts))

    def test_late_or_non_active_failure_never_regresses_or_consumes_retry(self) -> None:
        job = self._queued()
        asr = self.store.transition_legacy_stage(
            job["job_id"], "audio", "running", retry_limit=2
        )
        ignored_future_failure = self.store.transition_legacy_stage(
            job["job_id"], "translation", "failed",
            error_class="transient", error_code="late_timeout",
        )
        self.assertEqual(asr["stage_attempt_id"], ignored_future_failure["stage_attempt_id"])
        current = self.store.get_job(job["job_id"])
        self.assertEqual("ASR", current["state"])
        self.assertEqual(0, current["retry_count"])

        translation = self.store.transition_legacy_stage(
            job["job_id"], "translation", "running", retry_limit=2
        )
        self.store.transition_legacy_stage(job["job_id"], "translation", "ok")
        mux = self.store.transition_legacy_stage(
            job["job_id"], "mux", "running", retry_limit=2
        )
        late_failure = self.store.transition_legacy_stage(
            job["job_id"], "translation", "failed",
            error_class="transient", error_code="delayed_failure",
        )
        self.assertEqual(translation["stage_attempt_id"], late_failure["stage_attempt_id"])
        current = self.store.get_job(job["job_id"])
        self.assertEqual("MUXING", current["state"])
        self.assertEqual(mux["stage_attempt_id"], current["active_stage_attempt_id"])
        self.assertEqual(0, current["retry_count"])

        self.store.transition_legacy_stage(
            job["job_id"], "mux", "failed",
            error_class="transient", error_code="mux_timeout",
        )
        retrying = self.store.get_job(job["job_id"])
        self.assertEqual("RETRYING", retrying["state"])
        self.assertEqual("MUXING", retrying["resume_state"])
        self.assertEqual(1, retrying["retry_count"])
        self.store.transition_legacy_stage(
            job["job_id"], "translation", "failed",
            error_class="transient", error_code="very_late_failure",
        )
        preserved = self.store.get_job(job["job_id"])
        self.assertEqual("RETRYING", preserved["state"])
        self.assertEqual("MUXING", preserved["resume_state"])
        self.assertEqual(1, preserved["retry_count"])
        self.assertEqual(
            1, len(self.store.list_stage_attempts(job["job_id"], "TRANSLATING"))
        )
        late_event_count = self.store._conn.execute(
            """
            SELECT COUNT(*) FROM pipeline_stage_events
            WHERE job_id=? AND event_type='LATE_LEGACY_TELEMETRY'
            """,
            (job["job_id"],),
        ).fetchone()[0]
        self.assertGreaterEqual(late_event_count, 3)

    def test_additional_legacy_stage_mappings(self) -> None:
        self.assertEqual("ASR", legacy_stage_to_pipeline_state("source_transcription"))
        self.assertEqual("ASR", legacy_stage_to_pipeline_state("resource_runtime"))
        self.assertEqual("MUXING", legacy_stage_to_pipeline_state("completed_delivery"))

    def test_job_store_never_modifies_source_media(self) -> None:
        before_bytes = self.media.read_bytes()
        before_stat = self.media.stat()
        job = self._queued()
        attempt = self.store.start_stage_attempt(
            job["job_id"], "ASR", inputs={"media_revision": job["media_revision"]},
            checkpoint={"segment": 1}, reason_code="readonly_check", evidence={}, confidence=1,
        )
        self.store.checkpoint_stage(
            attempt["stage_attempt_id"], {"segment": 2}, reason_code="readonly_checkpoint",
            evidence={}, confidence=1,
        )
        self.store.recover_interrupted_stages()
        after_stat = self.media.stat()
        self.assertEqual(before_bytes, self.media.read_bytes())
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)

    def test_scan_state_store_compatibility_wrappers_share_transaction(self) -> None:
        shared_path = self.root / "shared.sqlite3"
        scan = ScanStateStore(shared_path)
        try:
            stat = self.media.stat()
            row = scan.upsert_ingest_observation(
                self.media, stat.st_size, stat.st_mtime_ns, event_type="created"
            )
            self.assertTrue(scan.in_transaction)
            self.assertEqual("STABILIZING", row["state"])
            self.assertEqual(1, len(scan.iter_pending_ingest_observations()))
            self.assertTrue(scan.clear_ingest_observation(self.media))
            scan.commit()
            self.assertEqual([], scan.iter_pending_ingest_observations())
        finally:
            scan.close()


if __name__ == "__main__":
    unittest.main()
