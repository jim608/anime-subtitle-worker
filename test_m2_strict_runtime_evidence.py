from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from test_support import configure_isolated_test_tempdir

configure_isolated_test_tempdir()

from m2_guardrail_runtime import configuration_fingerprint
from m2_strict_observation import STRICT_EVIDENCE_KEYS
from m2_strict_runtime_evidence import build_m2_strict_runtime_evidence
from output_manifest import output_manifest_path
from pipeline_state import PipelineJobStore
from processing_provenance import (
    processing_config_signature,
    provenance_path_for_video,
)
from safe_files import sha256_file
from source_analysis_service import SOURCE_ANALYSIS_SERVICE_VERSION
from source_analyzer import (
    ANALYZER_VERSION,
    DECISION_SCHEMA_VERSION,
    DECISION_VERSION,
    AnalyzerThresholds,
)
from source_decision import SOURCE_DECISION_CONTRACT
from source_integrity import capture_source_snapshot
from subtitle_paths import SubtitlePaths
from transcriber import asr_diagnostics_path


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class _FakePipeline:
    def __init__(
        self,
        connection: sqlite3.Connection,
        job: dict[str, object],
        decision: dict[str, object],
    ) -> None:
        self._conn = connection
        self.job = job
        self.decision = decision

    def job_for_path(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return dict(self.job)

    def reusable_source_decision(
        self,
        *_args: object,
        with_reason: bool = False,
        **_kwargs: object,
    ) -> object:
        if with_reason:
            return dict(self.decision), "source_decision_reusable"
        return dict(self.decision)


class _FakeState:
    def __init__(self, connection: sqlite3.Connection, pipeline: _FakePipeline) -> None:
        self._conn = connection
        self._pipeline = pipeline

    def _one(self, sql: str, value: str) -> dict[str, object] | None:
        cursor = self._conn.execute(sql, (value,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(zip((item[0] for item in cursor.description), row, strict=True))

    def get_ai_delivery_attempt(self, attempt_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT * FROM ai_delivery_attempts WHERE attempt_id=?",
            attempt_id,
        )

    def get_ai_delivery_obligation(self, obligation_id: str) -> dict[str, object] | None:
        return self._one(
            "SELECT * FROM ai_delivery_obligations WHERE obligation_id=?",
            obligation_id,
        )

    def ai_queue_candidate_snapshot(self, path: Path) -> dict[str, object] | None:
        return self._one("SELECT * FROM ai_candidate_queue WHERE path=?", str(path))

    def pipeline_jobs(self) -> _FakePipeline:
        return self._pipeline


class M2StrictRuntimeEvidenceTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.video = (self.root / "private-episode-name.mkv").resolve()
        self.video.write_bytes(b"immutable-source-media")
        self.output = (self.root / "private-episode-name.zh-TW.srt").resolve()
        self.output.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\n這是一段繁體中文字幕。\n\n",
            encoding="utf-8",
        )
        self.ja_srt = (self.work / "source.ja.srt").resolve()
        self.zh_cn_srt = (self.work / "source.zh-CN.srt").resolve()
        self.config = SimpleNamespace(
            work_path=self.work,
            input_path=self.root,
            log_path=self.work / "logs",
            ai_output_manifest_path="manifests",
            processing_provenance_path="provenance",
            asr_diagnostics_path="asr-diagnostics",
            completed_delivery_enabled=False,
            source_integrity_sha256_enabled=True,
            source_analyzer_version=ANALYZER_VERSION,
            source_decision_schema_version=DECISION_SCHEMA_VERSION,
            source_decision_version=DECISION_VERSION,
            ai_japanese_ass_suffix=".AI.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
            ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            finished_subtitle_suffixes=[],
            export_ai_ass=True,
            source_analyzer_thresholds=lambda: AnalyzerThresholds(),
        )
        self.connection = sqlite3.connect(":memory:")
        self._create_schema()
        self._build_fixture(strategy="USE_EXISTING_ZH_TW")
        self.paths = SubtitlePaths(
            ja_srt=self.ja_srt,
            zh_cn_srt=self.zh_cn_srt,
            zh_tw_srt=self.output,
            ai_ja_ass=self.root / "unused.ja.ass",
            ai_zh_cn_ass=self.root / "unused.zh-CN.ass",
            ai_zh_tw_ass=self.root / "unused.zh-TW.ass",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE ai_delivery_attempts (
                attempt_id TEXT, obligation_id TEXT, attempt_number INTEGER,
                status TEXT, stage TEXT, error_code TEXT, detail TEXT,
                started_at REAL, finished_at REAL
            );
            CREATE TABLE ai_delivery_obligations (
                obligation_id TEXT, canonical_path TEXT, media_fingerprint TEXT,
                media_size INTEGER, media_mtime_ns INTEGER, policy_revision TEXT,
                state TEXT, verified_at REAL, manifest_path TEXT,
                manifest_sha256 TEXT
            );
            CREATE TABLE ai_candidate_queue (path TEXT, status TEXT);
            CREATE TABLE pipeline_jobs (
                job_id TEXT, canonical_path TEXT, media_revision TEXT,
                media_fingerprint TEXT, media_size INTEGER, media_mtime_ns INTEGER,
                state TEXT, active_stage_attempt_id TEXT, completed_at REAL,
                terminal_error_json TEXT
            );
            CREATE TABLE pipeline_job_transitions (
                transition_id TEXT, job_id TEXT, sequence INTEGER,
                from_state TEXT, to_state TEXT, reason_code TEXT,
                evidence_json TEXT, confidence REAL, actor TEXT,
                stage_attempt_id TEXT, idempotency_key TEXT
            );
            CREATE TABLE pipeline_stage_attempts (
                stage_attempt_id TEXT, job_id TEXT, stage TEXT,
                attempt_number INTEGER, status TEXT, input_json TEXT,
                input_sha256 TEXT, output_json TEXT, outputs_verified INTEGER,
                model_json TEXT, checkpoint_json TEXT, checkpoint_sha256 TEXT,
                error_json TEXT, started_at REAL, finished_at REAL
            );
            CREATE TABLE pipeline_source_decisions (
                decision_id TEXT, job_id TEXT, stage_attempt_id TEXT,
                input_identity_json TEXT, input_identity_sha256 TEXT,
                media_revision TEXT, source_fingerprint TEXT,
                analyzer_version TEXT, decision_schema_version TEXT,
                decision_version TEXT, config_fingerprint TEXT,
                candidate_fingerprint TEXT, strategy TEXT, reason_code TEXT,
                decision_json TEXT, decision_sha256 TEXT
            );
            """
        )

    def _build_fixture(self, *, strategy: str) -> None:
        stat = self.video.stat()
        canonical, size, mtime_ns, _kind, fingerprint, revision = (
            PipelineJobStore._media_identity(
                self.video,
                size=int(stat.st_size),
                mtime_ns=int(stat.st_mtime_ns),
            )
        )
        self.job: dict[str, object] = {
            "job_id": "job-current",
            "canonical_path": canonical,
            "media_revision": revision,
            "media_fingerprint": fingerprint,
            "media_size": size,
            "media_mtime_ns": mtime_ns,
            "state": "COMPLETED",
            "active_stage_attempt_id": "",
            "completed_at": 400.0,
            "terminal_error": {},
            "terminal_error_json": "{}",
        }
        input_identity = {"media_revision": revision, "contract": "test-input-v1"}
        decision_payload = {
            "media_revision": revision,
            "source_fingerprint": fingerprint,
            "analyzer_version": ANALYZER_VERSION,
            "decision_schema_version": DECISION_SCHEMA_VERSION,
            "decision_version": DECISION_VERSION,
            "config_fingerprint": "c" * 64,
            "candidate_fingerprint": "d" * 64,
            "strategy": strategy,
            "reason_code": "test_source_selected",
        }
        input_raw = _canonical(input_identity)
        decision_raw = _canonical(decision_payload)
        input_sha = hashlib.sha256(input_raw.encode("utf-8")).hexdigest()
        decision_sha = hashlib.sha256(decision_raw.encode("utf-8")).hexdigest()
        decision_id = "decision-current"
        checkpoint = {
            "kind": "source_decision",
            "decision_id": decision_id,
            "decision_sha256": decision_sha,
            "input_identity_sha256": input_sha,
            "analyzer_version": ANALYZER_VERSION,
            "decision_schema_version": str(DECISION_SCHEMA_VERSION),
        }
        output = {
            "no_artifact_required": True,
            "checkpoint_evidence": checkpoint,
        }
        checkpoint_raw = _canonical(checkpoint)
        stage_input_raw = _canonical({"video_identity": revision})
        self.decision: dict[str, object] = {
            "decision_id": decision_id,
            "job_id": "job-current",
            "stage_attempt_id": "source-attempt",
            "input_identity": input_identity,
            "input_identity_sha256": input_sha,
            "media_revision": revision,
            "source_fingerprint": fingerprint,
            "analyzer_version": ANALYZER_VERSION,
            "decision_schema_version": str(DECISION_SCHEMA_VERSION),
            "decision_version": DECISION_VERSION,
            "config_fingerprint": "c" * 64,
            "candidate_fingerprint": "d" * 64,
            "strategy": strategy,
            "reason_code": "test_source_selected",
            "decision": decision_payload,
            "decision_sha256": decision_sha,
            "integrity_valid": True,
        }

        manifest = output_manifest_path(self.video, self.config)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        output_stat = self.output.stat()
        policy_revision = processing_config_signature(self.config)
        manifest_payload = {
            "schema_version": 2,
            "video": str(self.video),
            "media": {
                "canonical_path": str(self.video),
                "media_size": size,
                "media_mtime_ns": mtime_ns,
                "media_fingerprint": "e" * 64,
            },
            "delivery": {
                "contract": "ai-delivery-v1",
                "obligation_id": "obligation-current",
                "policy_revision": policy_revision,
                "verified_at": 300.0,
            },
            "quality_gate": {"passed": True, "contract": "worker-prepublication-v1"},
            "publication_kind": "adopted_zh_tw",
            "publication": {
                "contract": "ai-publication-semantics-v2",
                "kind": "adopted_zh_tw",
                "output_languages": ["zh-TW"],
            },
            "outputs": [
                {
                    "path": str(self.output),
                    "size": int(output_stat.st_size),
                    "mtime_ns": int(output_stat.st_mtime_ns),
                    "sha256": sha256_file(self.output),
                    "language": "zh-TW",
                }
            ],
        }
        manifest.write_text(
            json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_sha = sha256_file(manifest)

        self.connection.execute(
            "INSERT INTO ai_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "attempt-current",
                "obligation-current",
                1,
                "succeeded",
                "delivery_verification",
                "",
                "",
                200.0,
                300.0,
            ),
        )
        self.connection.execute(
            "INSERT INTO ai_delivery_obligations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "obligation-current",
                str(self.video),
                "e" * 64,
                size,
                mtime_ns,
                policy_revision,
                "succeeded",
                300.0,
                str(manifest),
                manifest_sha,
            ),
        )
        self.connection.execute(
            "INSERT INTO ai_candidate_queue VALUES(?, 'done')",
            (str(self.video),),
        )
        self.connection.execute(
            "INSERT INTO pipeline_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                self.job["job_id"],
                self.job["canonical_path"],
                self.job["media_revision"],
                self.job["media_fingerprint"],
                self.job["media_size"],
                self.job["media_mtime_ns"],
                self.job["state"],
                self.job["active_stage_attempt_id"],
                self.job["completed_at"],
                self.job["terminal_error_json"],
            ),
        )
        completion_evidence = {
            "verification": {
                "required_outputs_complete": True,
                "hashes_verified": True,
                "publication_marker_absent": True,
                "media_identity_matched": True,
            },
            "pipeline_completion_gate": {
                "source_identity_verified": True,
                "required_artifacts_rehashed": True,
            },
        }
        self.connection.execute(
            "INSERT INTO pipeline_job_transitions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "transition-completed",
                "job-current",
                1,
                "QC",
                "COMPLETED",
                "verified_delivery_completed",
                _canonical(completion_evidence),
                1.0,
                "publisher",
                None,
                "complete-current",
            ),
        )
        self.connection.execute(
            "INSERT INTO pipeline_stage_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "source-attempt",
                "job-current",
                "SUBTITLE_DETECTION",
                1,
                "SUCCEEDED",
                stage_input_raw,
                hashlib.sha256(stage_input_raw.encode("utf-8")).hexdigest(),
                _canonical(output),
                1,
                "{}",
                checkpoint_raw,
                hashlib.sha256(checkpoint_raw.encode("utf-8")).hexdigest(),
                "{}",
                210.0,
                220.0,
            ),
        )
        self.connection.execute(
            "INSERT INTO pipeline_source_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                "job-current",
                "source-attempt",
                input_raw,
                input_sha,
                revision,
                fingerprint,
                ANALYZER_VERSION,
                str(DECISION_SCHEMA_VERSION),
                DECISION_VERSION,
                "c" * 64,
                "d" * 64,
                strategy,
                "test_source_selected",
                decision_raw,
                decision_sha,
            ),
        )
        self.connection.commit()
        self.pipeline = _FakePipeline(self.connection, self.job, self.decision)
        self.state = _FakeState(self.connection, self.pipeline)
        self._write_provenance(strategy=strategy, decision_sha=decision_sha)
        self.runtime = self._runtime_state()

    def _write_provenance(self, *, strategy: str, decision_sha: str) -> None:
        source = capture_source_snapshot(self.video, hash_content=True)
        path = provenance_path_for_video(self.config, self.video)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "video_path": str(self.video),
            "video": {
                "size": int(self.video.stat().st_size),
                "mtime_ns": int(self.video.stat().st_mtime_ns),
            },
            "config_signature": processing_config_signature(self.config),
            "created_at": 190.0,
            "run_started_at": 205.0,
            "finished_at": 300.0,
            "status": "complete",
            "source_integrity": {
                **source.as_evidence(),
                "verified": True,
                "verification": "sha256",
            },
            "source_analysis": {
                "contract": SOURCE_ANALYSIS_SERVICE_VERSION,
                "decision_id": "decision-current",
                "decision_sha256": decision_sha,
                "strategy": strategy,
                "decision_schema_version": DECISION_SCHEMA_VERSION,
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _runtime_state(self) -> dict[str, object]:
        baseline = {
            "worker_commit_sha": "a" * 40,
            "webui_commit_sha": "b" * 40,
            "worker_source_revision": "c" * 64,
            "webui_source_revision": "d" * 64,
            "worker_image_id": "sha256:" + "e" * 64,
            "webui_image_id": "sha256:" + "f" * 64,
            "configuration_fingerprint": configuration_fingerprint(self.config),
            "decision_schema_version": DECISION_SCHEMA_VERSION,
            "decision_version": DECISION_VERSION,
            "decision_contract": SOURCE_DECISION_CONTRACT,
        }
        encoded = json.dumps(
            baseline,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        state = {
            "status": "ARMED",
            "gate_start_epoch": 100.0,
            "gate_baseline_version": (
                "m2-guardrail-v1:" + hashlib.sha256(encoded).hexdigest()[:24]
            ),
            "baseline": baseline,
            "pre_gate_running": {"attempt_keys": [], "queue_job_keys": []},
            "production_resources_affected": False,
        }
        return {
            "status": "ARMED",
            "reason_code": "runtime_baseline_match",
            "state": state,
        }

    def _collect(self) -> dict[str, object]:
        with (
            mock.patch(
                "output_manifest.validate_output_manifest",
                return_value=True,
            ),
            mock.patch("subtitle_paths.paths_for_video", return_value=self.paths),
            mock.patch(
                "control_state.open_ai_quality_review_for_target",
                return_value=None,
            ),
        ):
            return build_m2_strict_runtime_evidence(
                self.state,
                self.video,
                self.config,
                "attempt-current",
                self.runtime,
            )

    def test_complete_non_asr_fixture_proves_exact_eleven_facts(self) -> None:
        result = self._collect()

        self.assertEqual(set(result), {"outcome", "evidence", "processing_strategy"})
        self.assertEqual(tuple(result["evidence"]), STRICT_EVIDENCE_KEYS)
        self.assertTrue(all(result["evidence"].values()), result)
        self.assertEqual(result["processing_strategy"], "USE_EXISTING_ZH_TW")
        self.assertEqual(result["outcome"]["terminal_status"], "COMPLETED")
        self.assertFalse(result["outcome"]["incorrect_completion"])
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(self.video.name, serialized)

    def test_missing_final_provenance_fails_closed(self) -> None:
        provenance_path_for_video(self.config, self.video).unlink()

        result = self._collect()

        self.assertFalse(result["evidence"]["source_checksum_unchanged"])
        self.assertFalse(result["evidence"]["decision_record_complete"])
        self.assertFalse(
            result["evidence"]["no_unresolved_retry_quarantine_fallback"]
        )
        self.assertTrue(result["outcome"]["incorrect_completion"])

    def test_duplicate_rows_fail_job_and_publish_uniqueness(self) -> None:
        obligation = self.connection.execute(
            "SELECT * FROM ai_delivery_obligations WHERE obligation_id='obligation-current'"
        ).fetchone()
        duplicate = list(obligation)
        duplicate[0] = "obligation-duplicate"
        self.connection.execute(
            "INSERT INTO ai_delivery_obligations VALUES(?,?,?,?,?,?,?,?,?,?)",
            duplicate,
        )
        self.connection.execute(
            "INSERT INTO ai_delivery_attempts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "attempt-duplicate",
                "obligation-current",
                2,
                "succeeded",
                "delivery_verification",
                "",
                "",
                250.0,
                310.0,
            ),
        )
        self.connection.commit()

        result = self._collect()

        self.assertFalse(result["evidence"]["no_duplicate_job"])
        self.assertFalse(result["evidence"]["no_duplicate_publish"])
        self.assertTrue(result["outcome"]["duplicate_job"])
        self.assertTrue(result["outcome"]["duplicate_publish"])

    def test_raw_stage_hash_corruption_is_not_hidden_by_decoded_rows(self) -> None:
        self.connection.execute(
            "UPDATE pipeline_stage_attempts SET input_sha256=? WHERE stage_attempt_id=?",
            ("0" * 64, "source-attempt"),
        )
        self.connection.commit()

        result = self._collect()

        self.assertTrue(result["evidence"]["decision_record_complete"])
        self.assertFalse(result["evidence"]["stage_checkpoint_history_complete"])
        self.assertTrue(result["outcome"]["incorrect_completion"])

    def test_accepted_asr_diagnostics_still_require_full_hallucination_scan(self) -> None:
        decision_raw = self.connection.execute(
            "SELECT decision_json FROM pipeline_source_decisions"
        ).fetchone()[0]
        decision_payload = json.loads(decision_raw)
        decision_payload["strategy"] = "ASR_JA_AUDIO"
        new_raw = _canonical(decision_payload)
        new_sha = hashlib.sha256(new_raw.encode("utf-8")).hexdigest()
        self.connection.execute(
            """
            UPDATE pipeline_source_decisions
            SET strategy='ASR_JA_AUDIO', decision_json=?, decision_sha256=?
            """,
            (new_raw, new_sha),
        )
        input_sha = self.connection.execute(
            "SELECT input_identity_sha256 FROM pipeline_source_decisions"
        ).fetchone()[0]
        checkpoint = {
            "kind": "source_decision",
            "decision_id": "decision-current",
            "decision_sha256": new_sha,
            "input_identity_sha256": input_sha,
            "analyzer_version": ANALYZER_VERSION,
            "decision_schema_version": str(DECISION_SCHEMA_VERSION),
        }
        self.connection.execute(
            """
            UPDATE pipeline_stage_attempts
            SET checkpoint_json=?, checkpoint_sha256=?, output_json=?
            WHERE stage_attempt_id='source-attempt'
            """,
            (
                _canonical(checkpoint),
                hashlib.sha256(_canonical(checkpoint).encode("utf-8")).hexdigest(),
                _canonical(
                    {
                        "no_artifact_required": True,
                        "checkpoint_evidence": checkpoint,
                    }
                ),
            ),
        )
        self.connection.commit()
        self.decision.update(
            {
                "strategy": "ASR_JA_AUDIO",
                "decision": decision_payload,
                "decision_sha256": new_sha,
            }
        )
        self.pipeline.decision = self.decision
        self._write_provenance(strategy="ASR_JA_AUDIO", decision_sha=new_sha)
        self.ja_srt.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\n可疑的模型幻覺內容\n\n",
            encoding="utf-8",
        )
        diagnostic = asr_diagnostics_path(self.ja_srt, self.config)
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        diagnostic.write_text(
            json.dumps(
                {
                    "status": "accepted",
                    "srt_path": str(self.ja_srt),
                    "srt_sha256": sha256_file(self.ja_srt),
                }
            ),
            encoding="utf-8",
        )

        with mock.patch("transcriber._is_hallucination_text", return_value=True):
            result = self._collect()

        self.assertTrue(result["evidence"]["decision_record_complete"])
        self.assertFalse(result["evidence"]["hallucination_validation_pass"])
        self.assertTrue(result["outcome"]["hallucination_blocked"])
        self.assertTrue(result["outcome"]["incorrect_completion"])

    def test_runtime_pre_gate_attempt_never_matches_baseline(self) -> None:
        self.runtime["state"]["gate_start_epoch"] = 250.0

        result = self._collect()

        self.assertFalse(
            result["evidence"]["runtime_commit_matches_gate_baseline"]
        )
        self.assertFalse(result["outcome"]["claimed_after_gate_start"])
        self.assertFalse(result["outcome"]["incorrect_completion"])


if __name__ == "__main__":
    unittest.main()
