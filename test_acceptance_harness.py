from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness import (
    ACCEPTANCE_CONTRACT,
    FAULT_EVIDENCE_CONTRACT,
    OBSERVATION_CONTRACT,
    _evaluate_completed_delivery,
    _fault_attempt_recovery_error,
    _verify_structured_fault_evidence,
    evaluate_acceptance,
    validate_plan,
    validate_plan_structure,
)
import completed_delivery as completed_delivery_module
from output_manifest import (
    ADOPTED_ZH_TW_PUBLICATION_KIND,
    CONVERTED_ZH_CN_PUBLICATION_KIND,
    TRANSLATED_PUBLICATION_KIND,
    delivery_identity,
    output_manifest_path,
    write_output_manifest,
)
from processing_provenance import processing_config_signature, provenance_path_for_video
from safe_files import sha256_file
from source_decision import CONVERT_ZH_CN, SOURCE_DECISION_CONTRACT, TRANSLATE_JAPANESE, USE_ZH_TW
from subtitle_extract import classify_subtitle_content_file
from subtitle_paths import paths_for_video, source_transcript_paths_for_video
from subtitle_quality import analyze_subtitle_file


SUITE_STARTED = 1_800_000_000.0
SUITE_FINISHED = SUITE_STARTED + 2_000.0
VALID_ASS = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,這是繁體中文字幕
"""
EMPTY_ASS = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,
"""
SIMPLIFIED_ASS = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,这是简体中文字幕
"""
JAPANESE_ASS = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,これは日本語の字幕です
"""


class AcceptanceHarnessTests(unittest.TestCase):
    FAULT_SCENARIOS_BY_INDEX = {
        0: "output_publish_interrupt",
        1: "temporary_io_error",
        2: "translation_timeout",
        3: "worker_kill",
        4: "temporary_database_busy",
        5: "output_publish_interrupt",
        6: "model_unavailable",
        7: "asr_process_crash",
        8: "temporary_io_error",
        15: "gpu_oom",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.work = cls.root / "work"
        cls.work.mkdir()
        cls.config = SimpleNamespace(
            work_path=cls.work,
            ai_output_manifest_path="ai_output_manifests",
            processing_provenance_path="provenance",
            scanner_state_path="scanner_state.sqlite3",
            control_state_path="control_state.sqlite3",
            ai_japanese_ass_suffix=".AI.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
            ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            ai_source_transcript_ass_suffix_template=".AI.source.{language}.ass",
            whisper_model="acceptance-whisper",
            translator_model="acceptance-translator",
            export_ai_ass=True,
            subtitle_quality_max_duration_seconds=5.5,
            subtitle_quality_max_primary_chars=42,
            subtitle_quality_hard_max_primary_chars=64,
            subtitle_quality_max_gap_seconds=45.0,
            subtitle_quality_max_leading_gap_seconds=30.0,
            whisper_hallucination_phrases=[],
        )
        cls.policy_revision = processing_config_signature(cls.config)
        cls.evidence_file = cls.root / "acceptance-run.log"
        cls.evidence_file.write_text("hash-bound unattended evidence\n", encoding="utf-8")
        cls.evidence_ref = {
            "kind": "run_log",
            "path": str(cls.evidence_file),
            "sha256": sha256_file(cls.evidence_file),
        }
        cls._create_databases()
        cls.plan = cls._create_corpus()
        cls.plan_path = cls.root / "plan.json"
        cls._write_json(cls.plan_path, cls.plan)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @classmethod
    def _create_databases(cls) -> None:
        scanner = sqlite3.connect(cls.work / "scanner_state.sqlite3")
        scanner.executescript(
            """
            CREATE TABLE ai_delivery_obligations (
                obligation_id TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                media_fingerprint TEXT NOT NULL,
                media_size INTEGER NOT NULL,
                media_mtime_ns INTEGER NOT NULL,
                policy_revision TEXT NOT NULL,
                state TEXT NOT NULL,
                outcome_code TEXT NOT NULL,
                manifest_path TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                eligible_at REAL NOT NULL,
                due_at REAL NOT NULL,
                verified_at REAL NOT NULL,
                attempt_count INTEGER NOT NULL
            );
            CREATE TABLE ai_delivery_attempts (
                attempt_id TEXT PRIMARY KEY,
                obligation_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                error_code TEXT NOT NULL,
                detail TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL NOT NULL
            );
            """
        )
        scanner.commit()
        scanner.close()
        control = sqlite3.connect(cls.work / "control_state.sqlite3")
        control.executescript(
            """
            CREATE TABLE control_commands (
                command_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                requested_at REAL NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE review_items (
                review_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                target_key TEXT NOT NULL,
                status TEXT NOT NULL,
                diagnosis_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                resolved_at REAL NOT NULL
            );
            """
        )
        control.commit()
        control.close()

    @classmethod
    def _create_corpus(cls) -> dict:
        # Keep every fixture fault on a route that reaches its trigger stage.
        # Ten distinct cases are still faulted, while all eight v1 scenarios
        # remain covered.
        routes = [
            "existing_zh_tw",
            "zh_cn_opencc",
            "japanese_subtitle_translation",
            "japanese_audio_asr",
        ]
        cases: list[dict] = []
        for index in range(100):
            extension = ".mkv" if index % 2 == 0 else ".mp4"
            video = cls.root / "media" / f"series-{index % 10:02d}" / f"episode-{index:03d}{extension}"
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(f"real-media-fixture-{index}".encode("ascii"))
            route = routes[index % len(routes)]
            cls._publish_route(index, video=video, route=route)
            identity = delivery_identity(video, cls.config)
            fault = []
            scenario = cls.FAULT_SCENARIOS_BY_INDEX.get(index)
            if scenario is not None:
                fault = [
                    {
                        "fault_id": f"fault-{index:03d}",
                        "scenario": scenario,
                        "trigger": f"deterministic checkpoint {index}",
                    }
                ]
            cases.append(
                {
                    "case_id": f"case-{index:03d}",
                    "media": {
                        **identity["media"],
                        "policy_revision": identity["policy_revision"],
                        "obligation_id": identity["obligation_id"],
                    },
                    "expected_route": route,
                    "strata": {
                        "series_id": f"series-{index % 10:02d}",
                        "container": "matroska" if extension == ".mkv" else "mov",
                        "duration_bucket": ["short", "standard", "long"][index % 3],
                        "audio_layout": "japanese-stereo",
                        "subtitle_layout": route,
                        "release_profile": "bluray" if index % 2 == 0 else "web",
                    },
                    "faults": fault,
                }
            )
        return {
            "contract": ACCEPTANCE_CONTRACT,
            "schema_version": 1,
            "suite_id": "acceptance-test-suite",
            "created_at": SUITE_STARTED - 100,
            "cases": cases,
        }

    @classmethod
    def _publish_route(
        cls,
        index: int,
        *,
        video: Path | None = None,
        route: str | None = None,
        traditional_text: str = VALID_ASS,
    ) -> None:
        if video is None:
            video = Path(cls.plan["cases"][index]["media"]["canonical_path"])
        if route is None:
            route = cls.plan["cases"][index]["expected_route"]
        source_payload: dict | None = None
        if route == "existing_zh_tw":
            output = video.with_name(f"{video.stem}.zh-TW.ass")
            output.write_text(traditional_text, encoding="utf-8")
            source_payload = cls._subtitle_source_payload(output, "zh-tw", USE_ZH_TW, output=output)
            outputs = [output]
            publication_kind = ADOPTED_ZH_TW_PUBLICATION_KIND
            output_languages = ["zh-TW"]
        elif route == "zh_cn_opencc":
            source = video.with_name(f"{video.stem}.zh-CN.ass")
            source.write_text(SIMPLIFIED_ASS, encoding="utf-8")
            output = video.with_name(f"{video.stem}.zh-TW.ass")
            output.write_text(traditional_text, encoding="utf-8")
            source_payload = cls._subtitle_source_payload(source, "zh-cn", CONVERT_ZH_CN, output=output)
            outputs = [output]
            publication_kind = CONVERTED_ZH_CN_PUBLICATION_KIND
            output_languages = ["zh-TW"]
        else:
            paths = paths_for_video(video, cls.config)
            paths.ai_ja_ass.write_text(JAPANESE_ASS, encoding="utf-8")
            paths.ai_zh_cn_ass.write_text(SIMPLIFIED_ASS, encoding="utf-8")
            paths.ai_zh_tw_ass.write_text(traditional_text, encoding="utf-8")
            outputs = [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass]
            publication_kind = TRANSLATED_PUBLICATION_KIND
            output_languages = ["ja", "zh-CN", "zh-TW"]
            if route == "japanese_subtitle_translation":
                source = video.with_name(f"{video.stem}.ja.ass")
                source.write_text(JAPANESE_ASS, encoding="utf-8")
                source_payload = cls._subtitle_source_payload(source, "ja", TRANSLATE_JAPANESE)
        identity = delivery_identity(video, cls.config)
        manifest_provenance = (
            {"subtitle_source": source_payload}
            if source_payload is not None
            else {"asr_used": True}
        )
        write_output_manifest(
            video,
            cls.config,
            outputs,
            provenance=manifest_provenance,
            obligation_id=identity["obligation_id"],
            publication_kind=publication_kind,
            output_languages=output_languages,
        )
        cls._write_provenance(
            video,
            route,
            identity["policy_revision"],
            index,
            subtitle_source=source_payload,
        )
        cls._upsert_ledger(
            video,
            identity,
            publication_kind,
            output_languages,
            index=index,
        )

    @classmethod
    def _subtitle_source_payload(
        cls,
        source: Path,
        language: str,
        strategy: str,
        *,
        output: Path | None = None,
    ) -> dict:
        classification = classify_subtitle_content_file(source)
        role = "japanese" if language == "ja" else "unknown"
        quality = analyze_subtitle_file(source, cls.config, role=role)
        stat = source.stat()
        payload = {
            "contract": SOURCE_DECISION_CONTRACT,
            "strategy": strategy,
            "source_kind": "sidecar",
            "source_path": str(source.resolve()),
            "source_language": language,
            "stream_index": -1,
            "source_sha256": sha256_file(source),
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "classification": classification.as_dict(),
            "source_quality": quality.to_dict(),
            "asr_used": False,
        }
        if output is not None:
            payload["output_quality"] = analyze_subtitle_file(
                output,
                cls.config,
                role="unknown",
            ).to_dict()
        return payload

    @classmethod
    def _publish_source_only(cls, index: int) -> None:
        case = cls.plan["cases"][index]
        video = Path(case["media"]["canonical_path"])
        route = case["expected_route"]
        output = source_transcript_paths_for_video(video, cls.config, "en").ass
        output.write_text(VALID_ASS, encoding="utf-8")
        identity = delivery_identity(video, cls.config)
        write_output_manifest(
            video,
            cls.config,
            [output],
            provenance={"delivery_route": route, "asr_used": route == "japanese_audio_asr"},
            obligation_id=identity["obligation_id"],
            publication_kind="source_language",
            output_languages=("en",),
        )
        cls._upsert_ledger(video, identity, "source_language", ["en"], index=index)

    @classmethod
    def _write_provenance(
        cls,
        video: Path,
        route: str,
        policy: str,
        index: int,
        *,
        subtitle_source: dict | None = None,
    ) -> None:
        started = SUITE_STARTED + index * 10 + 1
        finished = started + 2
        payload = {
            "schema_version": 1,
            "video_path": str(video),
            "video": {"size": video.stat().st_size, "mtime_ns": video.stat().st_mtime_ns},
            "config_signature": policy,
            "created_at": started,
            "updated_at": finished,
            "run_started_at": started,
            "finished_at": finished,
            "status": "complete",
            "current_stage": "complete",
            "current_stage_status": "ok",
            "stages": [
                {"stage": "complete", "status": "ok", "message": "done", "updated_at": finished}
            ],
        }
        if route == "japanese_audio_asr":
            payload["asr"] = {"backend": "fixture", "model": "fixture"}
            payload["stages"].insert(
                0,
                {"stage": "transcription", "status": "ok", "message": "fixture", "updated_at": started + 1},
            )
        elif subtitle_source is not None:
            payload["subtitle_source"] = subtitle_source
        path = provenance_path_for_video(cls.config, video)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def _upsert_ledger(
        cls,
        video: Path,
        identity: dict,
        publication_kind: str,
        output_languages: list[str],
        *,
        index: int,
    ) -> None:
        manifest = output_manifest_path(video, cls.config)
        verification = {
            "publication_semantics_verified": True,
            "publication_contract": "ai-publication-semantics-v1",
            "expected_policy_revision": identity["policy_revision"],
            "manifest_policy_revision": identity["policy_revision"],
            "policy_revision_matched": True,
            "publication_kind": publication_kind,
            "output_languages": output_languages,
        }
        connection = sqlite3.connect(cls.work / "scanner_state.sqlite3")
        connection.execute("DELETE FROM ai_delivery_obligations WHERE obligation_id=?", (identity["obligation_id"],))
        connection.execute("DELETE FROM ai_delivery_attempts WHERE obligation_id=?", (identity["obligation_id"],))
        has_fault = index in cls.FAULT_SCENARIOS_BY_INDEX
        attempt_count = 2 if has_fault else 1
        connection.execute(
            """
            INSERT INTO ai_delivery_obligations(
                obligation_id, canonical_path, media_fingerprint, media_size,
                media_mtime_ns, policy_revision, state, outcome_code,
                manifest_path, manifest_sha256, verification_json,
                eligible_at, due_at, verified_at, attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, 'succeeded', 'verified_on_time', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity["obligation_id"],
                identity["media"]["canonical_path"],
                identity["media"]["media_fingerprint"],
                identity["media"]["media_size"],
                identity["media"]["media_mtime_ns"],
                identity["policy_revision"],
                str(manifest),
                sha256_file(manifest),
                json.dumps(verification, ensure_ascii=False),
                SUITE_STARTED,
                SUITE_FINISHED + 10_000,
                SUITE_STARTED + 10,
                attempt_count,
            ),
        )
        case_started = SUITE_STARTED + index * 10
        if has_fault:
            connection.execute(
                """
                INSERT INTO ai_delivery_attempts(
                    attempt_id, obligation_id, attempt_number, status, stage,
                    error_code, detail, started_at, finished_at
                ) VALUES (?, ?, 1, 'retryable_failure', 'fault_injection', ?, ?, ?, ?)
                """,
                (
                    f"attempt-failed-{identity['obligation_id']}",
                    identity["obligation_id"],
                    "injected_fault",
                    "automatic fault injection fixture",
                    case_started,
                    case_started + 0.4,
                ),
            )
        connection.execute(
            """
            INSERT INTO ai_delivery_attempts(
                attempt_id, obligation_id, attempt_number, status, stage,
                error_code, detail, started_at, finished_at
            ) VALUES (?, ?, ?, 'succeeded', 'complete', '', '', ?, ?)
            """,
            (
                f"attempt-{identity['obligation_id']}",
                identity["obligation_id"],
                attempt_count,
                case_started + 0.5,
                case_started + 2,
            ),
        )
        connection.commit()
        connection.close()

    @classmethod
    def _observations(cls, *, reviews: set[int] | None = None) -> dict:
        reviews = reviews or set()
        cases: list[dict] = []
        for index, planned in enumerate(cls.plan["cases"]):
            started = SUITE_STARTED + index * 10
            faults = []
            for fault in planned["faults"]:
                injected_at = started + 0.25
                recovered_at = started + 0.75
                faults.append(
                    {
                        "fault_id": fault["fault_id"],
                        "status": "recovered",
                        "injected_at": injected_at,
                        "recovered_at": recovered_at,
                        "evidence": [
                            cls._fault_evidence_ref(
                                planned,
                                fault,
                                injected_at=injected_at,
                                recovered_at=recovered_at,
                            )
                        ],
                    }
                )
            needs_review = index in reviews
            cases.append(
                {
                    "case_id": planned["case_id"],
                    "canonical_path": planned["media"]["canonical_path"],
                    "obligation_id": planned["media"]["obligation_id"],
                    "route": planned["expected_route"],
                    "started_at": started,
                    "finished_at": started + 5,
                    "outcome": "review_required" if needs_review else "completed",
                    "review_required": needs_review,
                    "manual_interventions": [],
                    "errors": [],
                    "evidence": [deepcopy(cls.evidence_ref)],
                    "faults": faults,
                }
            )
        return {
            "contract": OBSERVATION_CONTRACT,
            "schema_version": 1,
            "suite_id": cls.plan["suite_id"],
            "plan_sha256": sha256_file(cls.plan_path),
            "started_at": SUITE_STARTED,
            "finished_at": SUITE_FINISHED,
            "manual_interventions": [],
            "cases": cases,
        }

    @classmethod
    def _fault_evidence_ref(
        cls,
        planned: dict,
        fault: dict,
        *,
        injected_at: float,
        recovered_at: float,
    ) -> dict:
        path = cls.root / "fault-evidence" / f"{fault['fault_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "contract": FAULT_EVIDENCE_CONTRACT,
            "schema_version": 1,
            "suite_id": cls.plan["suite_id"],
            "case_id": planned["case_id"],
            "obligation_id": planned["media"]["obligation_id"],
            "fault_id": fault["fault_id"],
            "scenario": fault["scenario"],
            "trigger": fault["trigger"],
            "injected_at": injected_at,
            "observed_failure": {
                "stage": "fixture_fault_stage",
                "error_code": fault["scenario"],
                "observed_at": injected_at + 0.1,
            },
            "recovery": {
                "automatic": True,
                "started_at": injected_at + 0.2,
                "completed_at": recovered_at,
                "checkpoint": "fixture_resume_checkpoint",
            },
            "manual_interventions": [],
        }
        cls._write_json(path, payload)
        return {"kind": "fault_injection_event", "path": str(path), "sha256": sha256_file(path)}

    @classmethod
    def _write_observations(cls, payload: dict, name: str) -> Path:
        path = cls.root / name
        cls._write_json(path, payload)
        return path

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _probe(path: Path) -> dict:
        index = int(path.stem.rsplit("-", 1)[-1])
        durations = [300.0, 1_440.0, 5_400.0]
        return {
            "duration_seconds": durations[index % 3],
            "format_names": ["matroska"] if path.suffix == ".mkv" else ["mov", "mp4"],
            "video_streams": 1,
            "audio_streams": 1,
        }

    def _evaluate(self, observations: dict, name: str) -> dict:
        path = self._write_observations(observations, name)
        with patch("acceptance.harness.subprocess.run", side_effect=self._mock_ffprobe):
            return evaluate_acceptance(self.plan_path, path, self.config)

    @classmethod
    def _mock_ffprobe(cls, command: list[str], *args, **kwargs) -> SimpleNamespace:
        path = Path(command[-1])
        probe = cls._probe(path)
        payload = {
            "format": {
                "duration": str(probe["duration_seconds"]),
                "format_name": ",".join(probe["format_names"]),
            },
            "streams": [
                {"codec_type": "video", "codec_name": "fixture-video"},
                {"codec_type": "audio", "codec_name": "fixture-audio", "channels": 2},
            ],
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    def test_99_of_100_with_one_review_qualifies(self) -> None:
        report = self._evaluate(self._observations(reviews={99}), "observations-99.json")

        self.assertTrue(report["qualified"], report["qualification_reasons"])
        self.assertEqual(report["counts"]["unattended_successes"], 99)
        self.assertEqual(report["counts"]["review_cases"], 1)
        self.assertEqual(report["counts"]["manual_interventions"], 0)

    def test_98_of_100_does_not_qualify(self) -> None:
        report = self._evaluate(self._observations(reviews={98, 99}), "observations-98.json")

        self.assertFalse(report["qualified"])
        self.assertEqual(report["counts"]["unattended_successes"], 98)
        self.assertIn("unattended_success_below_99_of_100", report["qualification_reasons"])

    def test_source_language_only_manifest_never_counts_as_chinese_success(self) -> None:
        self._publish_source_only(0)
        self.addCleanup(self._publish_route, 0)

        report = self._evaluate(self._observations(reviews={99}), "observations-source-only.json")

        case = report["cases"][0]
        self.assertFalse(case["success"])
        self.assertIn("final_traditional_chinese_output_missing", case["oracle_failures"])

    def test_output_hash_mismatch_fails_strict_manifest(self) -> None:
        video = Path(self.plan["cases"][1]["media"]["canonical_path"])
        manifest = json.loads(output_manifest_path(video, self.config).read_text(encoding="utf-8"))
        output = Path(manifest["outputs"][-1]["path"])
        output.write_text(VALID_ASS + "tampered\n", encoding="utf-8")
        self.addCleanup(self._publish_route, 1)

        report = self._evaluate(self._observations(reviews={99}), "observations-hash.json")

        self.assertIn("strict_output_manifest_validation_failed", report["cases"][1]["oracle_failures"])

    def test_empty_traditional_chinese_fails_live_qc_even_with_fresh_hashes(self) -> None:
        self._publish_route(2, traditional_text=EMPTY_ASS)
        self.addCleanup(self._publish_route, 2)

        report = self._evaluate(self._observations(reviews={99}), "observations-qc.json")

        self.assertIn("traditional_chinese_qc_failed", report["cases"][2]["oracle_failures"])

    def test_simplified_content_mislabeled_as_zh_tw_does_not_count(self) -> None:
        self._publish_route(3, traditional_text=SIMPLIFIED_ASS)
        self.addCleanup(self._publish_route, 3)

        report = self._evaluate(self._observations(reviews={99}), "observations-simplified.json")

        self.assertIn(
            "traditional_chinese_content_not_verified",
            report["cases"][3]["oracle_failures"],
        )

    def test_media_identity_drift_fails_closed(self) -> None:
        changed = deepcopy(self.plan)
        changed["cases"][0]["media"]["media_size"] += 1

        errors = validate_plan(changed, self.config, media_probe=self._probe)

        self.assertTrue(any("identity drifted" in error for error in errors))

    def test_manual_intervention_prevents_qualification(self) -> None:
        observations = self._observations(reviews={99})
        observations["cases"][0]["manual_interventions"] = ["operator clicked retry"]

        report = self._evaluate(observations, "observations-manual.json")

        self.assertFalse(report["qualified"])
        self.assertEqual(report["counts"]["manual_interventions"], 1)
        self.assertIn("manual_interventions_not_zero", report["qualification_reasons"])

    def test_planned_fault_that_did_not_recover_prevents_qualification(self) -> None:
        observations = self._observations(reviews={99})
        observations["cases"][0]["faults"][0]["status"] = "not_recovered"
        observations["cases"][0]["faults"][0]["recovered_at"] = None

        report = self._evaluate(observations, "observations-fault.json")

        self.assertFalse(report["qualified"])
        self.assertEqual(report["counts"]["unrecovered_faults"], 1)
        self.assertIn("planned_fault_not_recovered", report["qualification_reasons"])

    def test_arbitrary_log_cannot_substitute_for_structured_fault_evidence(self) -> None:
        observations = self._observations(reviews={99})
        observations["cases"][0]["faults"][0]["evidence"] = [deepcopy(self.evidence_ref)]

        report = self._evaluate(observations, "observations-fault-log-only.json")

        fault = report["cases"][0]["faults"][0]
        self.assertFalse(fault["recovered"])
        self.assertIn("no_verified_structured_fault_evidence", fault["errors"])

    def test_manifest_must_contain_exactly_100_cases(self) -> None:
        changed = deepcopy(self.plan)
        changed["cases"] = changed["cases"][:99]

        errors = validate_plan_structure(changed)

        self.assertTrue(any("exactly 100" in error for error in errors))

    def test_duplicate_media_identity_is_rejected(self) -> None:
        changed = deepcopy(self.plan)
        changed["cases"][1]["media"] = deepcopy(changed["cases"][0]["media"])

        errors = validate_plan_structure(changed)

        self.assertTrue(any("duplicate canonical media path" in error for error in errors))
        self.assertTrue(any("duplicate obligation id" in error for error in errors))

    def test_v2_plan_requires_completed_delivery_and_new_fault_scenarios(self) -> None:
        changed = deepcopy(self.plan)
        changed["schema_version"] = 2
        for index, case in enumerate(changed["cases"]):
            case["completed_delivery"] = {
                "source_sha256": f"{index + 1:064x}",
                "receipt_path": str(self.root / "receipts" / f"{index}.json"),
                "destination": str(self.root / "completed" / f"{index}.mkv"),
            }
        changed["cases"][10]["faults"] = [
            {
                "fault_id": "fault-mux-crash",
                "scenario": "mux_process_crash",
                "trigger": "kill ffmpeg after mux marker fsync",
            }
        ]
        changed["cases"][11]["faults"] = [
            {
                "fault_id": "fault-completed-publish",
                "scenario": "completed_publish_interrupt",
                "trigger": "kill worker after final publish before receipt commit",
            }
        ]

        self.assertEqual(validate_plan_structure(changed), [])
        del changed["cases"][99]["completed_delivery"]
        self.assertTrue(
            any(
                "cases[99].completed_delivery is required" in error
                for error in validate_plan_structure(changed)
            )
        )

    def test_v2_mux_fault_evidence_is_stage_and_checkpoint_bound(self) -> None:
        fault = {
            "fault_id": "fault-v2-mux",
            "scenario": "mux_process_crash",
            "trigger": "kill ffmpeg after durable marker",
        }
        observation = {
            "injected_at": SUITE_STARTED + 0.25,
            "recovered_at": SUITE_STARTED + 0.75,
        }
        payload = {
            "contract": FAULT_EVIDENCE_CONTRACT,
            "schema_version": 2,
            "suite_id": self.plan["suite_id"],
            "case_id": self.plan["cases"][0]["case_id"],
            "obligation_id": self.plan["cases"][0]["media"]["obligation_id"],
            "fault_id": fault["fault_id"],
            "scenario": fault["scenario"],
            "trigger": fault["trigger"],
            "injected_at": observation["injected_at"],
            "observed_failure": {
                "stage": "mux",
                "error_code": "process_killed",
                "observed_at": SUITE_STARTED + 0.35,
            },
            "recovery": {
                "automatic": True,
                "started_at": SUITE_STARTED + 0.5,
                "completed_at": observation["recovered_at"],
                "checkpoint": "completed_delivery_committed",
            },
            "manual_interventions": [],
        }
        path = self.root / "fault-evidence" / "fault-v2-mux.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(path, payload)

        self.assertEqual(
            _verify_structured_fault_evidence(
                path,
                suite_id=self.plan["suite_id"],
                case_id=self.plan["cases"][0]["case_id"],
                obligation_id=self.plan["cases"][0]["media"]["obligation_id"],
                fault=fault,
                observation=observation,
                plan_schema_version=2,
            ),
            "",
        )
        payload["observed_failure"]["stage"] = "translation"
        self._write_json(path, payload)
        self.assertIn(
            "stage must be mux",
            _verify_structured_fault_evidence(
                path,
                suite_id=self.plan["suite_id"],
                case_id=self.plan["cases"][0]["case_id"],
                obligation_id=self.plan["cases"][0]["media"]["obligation_id"],
                fault=fault,
                observation=observation,
                plan_schema_version=2,
            ),
        )

        attempts = [
            {
                "status": "retryable_failure",
                "stage": "translation",
                "finished_at": SUITE_STARTED + 0.4,
            },
            {
                "status": "succeeded",
                "stage": "complete",
                "finished_at": SUITE_STARTED + 1,
            },
        ]
        self.assertIn(
            "no terminal failed attempt",
            _fault_attempt_recovery_error(
                attempts, observation, expected_stage="completed_delivery"
            ),
        )
        attempts[0]["stage"] = "completed_delivery"
        self.assertEqual(
            _fault_attempt_recovery_error(
                attempts, observation, expected_stage="completed_delivery"
            ),
            "",
        )

    def test_readonly_evaluation_does_not_mutate_artifacts_or_sqlite(self) -> None:
        observations = self._observations(reviews={99})
        observations_path = self._write_observations(observations, "observations-readonly.json")
        watched = [
            self.work / "scanner_state.sqlite3",
            self.work / "control_state.sqlite3",
            observations_path,
            output_manifest_path(Path(self.plan["cases"][0]["media"]["canonical_path"]), self.config),
            provenance_path_for_video(
                self.config,
                Path(self.plan["cases"][0]["media"]["canonical_path"]),
            ),
        ]
        before = {str(path): (path.stat().st_mtime_ns, sha256_file(path)) for path in watched}

        with patch("acceptance.harness.subprocess.run", side_effect=self._mock_ffprobe):
            evaluate_acceptance(self.plan_path, observations_path, self.config)

        after = {str(path): (path.stat().st_mtime_ns, sha256_file(path)) for path in watched}
        self.assertEqual(after, before)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg tools unavailable")
class CompletedDeliveryAcceptanceOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.input_root = cls.root / "input"
        cls.work = cls.root / "work"
        cls.input_root.mkdir()
        cls.work.mkdir()
        cls.video = cls.input_root / "Series" / "Episode.mkv"
        cls.video.parent.mkdir(parents=True)
        _make_tiny_media(cls.video)
        cls.config = SimpleNamespace(
            input_path=cls.input_root,
            work_path=cls.work,
            ai_output_manifest_path="ai_output_manifests",
            ai_japanese_ass_suffix=".AI.ja.ass",
            ai_simplified_chinese_ass_suffix=".AI.zh-CN.ass",
            ai_traditional_chinese_ass_suffix=".AI.zh-TW.ass",
            ai_source_transcript_ass_suffix_template=".AI.source.{language}.ass",
            export_ai_ass=True,
            completed_delivery_enabled=True,
            completed_delivery_path=str(cls.root / "completed"),
            completed_delivery_manifest_path=str(cls.work / "completed_delivery_manifests"),
            completed_delivery_source_policy="retain",
            completed_delivery_timeout_seconds=30,
        )
        paths = paths_for_video(cls.video, cls.config)
        for path, text in (
            (paths.ai_ja_ass, "こんにちは"),
            (paths.ai_zh_cn_ass, "你好"),
            (paths.ai_zh_tw_ass, "您好，這是繁體中文字幕"),
        ):
            path.write_text(_real_ass(text), encoding="utf-8")
        identity = delivery_identity(cls.video, cls.config)
        write_output_manifest(
            cls.video,
            cls.config,
            [paths.ai_ja_ass, paths.ai_zh_cn_ass, paths.ai_zh_tw_ass],
            provenance={"asr_used": True},
            obligation_id=identity["obligation_id"],
            publication_kind=TRANSLATED_PUBLICATION_KIND,
            output_languages=["ja", "zh-CN", "zh-TW"],
        )
        result = completed_delivery_module.deliver_completed_mkv(cls.video, cls.config)
        cls.destination = Path(result.destination)
        cls.receipt_path = Path(result.receipt)
        cls.manifest_path = output_manifest_path(cls.video, cls.config)
        cls.manifest_digest = sha256_file(cls.manifest_path)
        manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))
        cls.publication = manifest["publication"]
        cls.media = {
            **identity["media"],
            "policy_revision": identity["policy_revision"],
            "obligation_id": identity["obligation_id"],
        }
        cls.planned = {
            "source_sha256": sha256_file(cls.video),
            "receipt_path": str(cls.receipt_path),
            "destination": str(cls.destination),
        }
        cls._baseline_output = cls.destination.read_bytes()
        cls._baseline_output_mtime_ns = cls.destination.stat().st_mtime_ns
        cls._baseline_receipt = cls.receipt_path.read_bytes()
        cls._baseline_receipt_payload = json.loads(cls._baseline_receipt.decode("utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def setUp(self) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_bytes(self._baseline_output)
        os.utime(
            self.destination,
            ns=(self._baseline_output_mtime_ns, self._baseline_output_mtime_ns),
        )
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        self.receipt_path.write_bytes(self._baseline_receipt)
        completed_delivery_module.completed_delivery_marker_path(
            self.video, self.config
        ).unlink(missing_ok=True)
        partial_digest = hashlib.sha256(self.destination.name.encode()).hexdigest()[:16]
        for path in self.destination.parent.glob(f".muxing-{partial_digest}-*.mkv"):
            path.unlink(missing_ok=True)

    def _observed_delivery(self) -> dict:
        return {
            "receipt": {
                "kind": "completed_delivery_receipt",
                "path": str(self.receipt_path),
                "sha256": sha256_file(self.receipt_path),
            },
            "final_mkv": {
                "kind": "completed_mkv",
                "path": str(self.destination),
                "sha256": sha256_file(self.destination),
            },
        }

    def _evaluate(self, *, planned: dict | None = None) -> tuple[dict, list[str]]:
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        committed_at = float(receipt["committed_at"])
        report, reasons, _evidence = _evaluate_completed_delivery(
            planned or deepcopy(self.planned),
            self._observed_delivery(),
            self.config,
            video=self.video,
            media=self.media,
            manifest_path=self.manifest_path,
            manifest_digest=self.manifest_digest,
            publication=self.publication,
            observed_started_at=committed_at - 30,
            observed_finished_at=committed_at + 30,
        )
        return report, reasons

    def _rewrite_receipt_for_current_output(self) -> None:
        payload = deepcopy(self._baseline_receipt_payload)
        stat = self.destination.stat()
        payload["output"] = {
            "path": str(self.destination),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256_file(self.destination),
        }
        self.receipt_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_real_ffmpeg_completed_delivery_passes_strict_oracle(self) -> None:
        report, reasons = self._evaluate()

        self.assertEqual(reasons, [])
        self.assertTrue(report["verified"])
        self.assertTrue(report["production_validator_passed"])
        self.assertEqual(report["streams"]["default_zh_tw_count"], 1)

    def test_tampered_final_mkv_fails_closed(self) -> None:
        with self.destination.open("ab") as handle:
            handle.write(b"tampered")

        report, reasons = self._evaluate()

        self.assertFalse(report["verified"])
        self.assertTrue(
            {"completed_delivery_output_hash_mismatch", "completed_delivery_final_mkv_evidence:artifact hash mismatch: " + str(self.destination)}
            & set(reasons)
        )

    def test_wrong_default_subtitle_fails_closed_even_with_fresh_hashes(self) -> None:
        wrong = self.destination.with_name("wrong-default.mkv")
        wrong.unlink(missing_ok=True)
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(self.destination),
                "-map",
                "0",
                "-c",
                "copy",
                "-disposition:s:0",
                "0",
                "-disposition:s:1",
                "default",
                "-disposition:s:2",
                "0",
                "-f",
                "matroska",
                str(wrong),
            ],
            check=True,
            timeout=30,
        )
        wrong.replace(self.destination)
        self._rewrite_receipt_for_current_output()

        report, reasons = self._evaluate()

        self.assertFalse(report["verified"])
        self.assertIn("completed_delivery_unique_default_zh_tw_missing", reasons)

    def test_destination_outside_completed_root_fails_closed(self) -> None:
        outside = self.root / "outside" / "escape.mkv"
        outside.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.destination, outside)
        planned = deepcopy(self.planned)
        planned["destination"] = str(outside)

        report, reasons = self._evaluate(planned=planned)

        self.assertFalse(report["verified"])
        self.assertIn("completed_delivery_destination_outside_root", reasons)

    def test_owned_mux_partial_prevents_success(self) -> None:
        partial_digest = hashlib.sha256(self.destination.name.encode()).hexdigest()[:16]
        partial = self.destination.parent / f".muxing-{partial_digest}-crashed.mkv"
        partial.write_bytes(b"partial")

        report, reasons = self._evaluate()

        self.assertFalse(report["verified"])
        self.assertIn("completed_delivery_partial_exists", reasons)


def _make_tiny_media(path: Path) -> None:
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        timeout=30,
    )


def _real_ass(text: str) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 320\n"
        "PlayResY: 180\n"
        "\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,"
        "100,100,0,0,1,1,0,2,10,10,10,1\n"
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.20,0:00:00.80,Default,,0,0,0,,{text}\n"
    )


if __name__ == "__main__":
    unittest.main()
