"""Targeted recurrence tests: unproven cache, transient admission, settled gate."""
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import m2_production_observation as observation
from m2_guardrail_runtime import runtime_guardrail_status
from m2_strict_observation import strict_evidence_template
from test_m2_production_observation import M2ProductionObservationTests
from test_worker import _config, _logger
from worker import VideoWorker, SourceSelectionReviewError
from subtitle_paths import paths_for_video
from transcriber import asr_diagnostics_path, asr_transcription_hold_path
from safe_files import sha256_file
from srt_utils import SrtBlock, write_srt
from ass_utils import format_ass


class AdmissionContinuityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = M2ProductionObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)

    def test_unproven_ass_never_seeds_cache_or_erases_evidence(self):
        root = self.fixture.root
        video = root / 'episode.mkv'
        video.write_bytes(b'read-only-source')
        config = _config(root, m2_server_canary_observer_enabled=True)
        paths = paths_for_video(video, config)
        paths.ai_ja_ass.write_text(format_ass([SrtBlock(1,'00:00:01,000 --> 00:00:03,000',['安全な字幕です'])]), encoding='utf-8')
        paths.ai_zh_tw_ass.write_text('existing formal output must stay', encoding='utf-8')
        diagnostic = asr_diagnostics_path(paths.ja_srt, config)
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        diagnostic.write_text('{"status":"old-evidence-preserved"}')
        hold = asr_transcription_hold_path(paths.ja_srt, config)
        hold.write_text('{"pending":true}')
        saved = {p: p.read_bytes() for p in (video, paths.ai_ja_ass, paths.ai_zh_tw_ass, diagnostic, hold)}
        for _ in range(2):
            worker = VideoWorker(config, _logger())
            with self.assertRaisesRegex(SourceSelectionReviewError, 'asr_ass_cache_acceptance_unproven'):
                worker._restore_japanese_srt_cache_from_ass(paths)
            self.assertFalse(paths.ja_srt.exists())
            self.assertEqual(saved, {p:p.read_bytes() for p in saved})

    def test_missing_diagnostic_reviews_before_translation_without_archival(self):
        root = self.fixture.root
        video = root/'episode.mkv'; video.write_bytes(b'read-only-source')
        config = _config(root, m2_server_canary_observer_enabled=True, log_path=root)
        paths = paths_for_video(video,config)
        write_srt(paths.ja_srt,[SrtBlock(1,'00:00:01,000 --> 00:00:03,000',['検証が必要です'])])
        before = paths.ja_srt.read_bytes()
        worker = VideoWorker(config,_logger())
        with self.assertRaisesRegex(SourceSelectionReviewError,'asr_cache_acceptance_unproven') as caught:
            worker._repair_cached_asr_rejection(video,root/'audio.wav',paths,audio_ready=False)
        self.assertEqual(worker._stage_for_exception(caught.exception),'source_selection_review')
        self.assertFalse(worker._requires_asr_review(caught.exception))
        self.assertEqual(paths.ja_srt.read_bytes(),before)
        self.assertEqual(video.read_bytes(),b'read-only-source')
        self.assertFalse((root/'asr_review_archive').exists())

    def test_accepted_cache_and_non_m2_legacy_compatibility(self):
        root=self.fixture.root; video=root/'episode.mkv'; video.write_bytes(b'source')
        config=_config(root,m2_server_canary_observer_enabled=True)
        paths=paths_for_video(video,config)
        write_srt(paths.ja_srt,[SrtBlock(1,'00:00:01,000 --> 00:00:03,000',['受理済みです'])])
        diagnostic=asr_diagnostics_path(paths.ja_srt,config); diagnostic.parent.mkdir(parents=True,exist_ok=True)
        diagnostic.write_text(json.dumps({'status':'accepted','srt_sha256':sha256_file(paths.ja_srt)}))
        self.assertFalse(VideoWorker(config,_logger())._repair_cached_asr_rejection(video,root/'a.wav',paths,audio_ready=False))
        diagnostic.unlink()  # Isolated fixture only, not Production evidence.
        config.m2_server_canary_observer_enabled=False
        self.assertFalse(VideoWorker(config,_logger())._repair_cached_asr_rejection(video,root/'a.wav',paths,audio_ready=False))

    def test_real_sqlite_busy_refuses_once_then_next_claim_succeeds(self):
        config=self.fixture._config('busy')
        database=Path(config.work_path)/'busy-fixture.sqlite'
        first=sqlite3.connect(database,timeout=0); second=sqlite3.connect(database,timeout=0)
        try:
            first.execute('CREATE TABLE lock_fixture (id INTEGER)'); first.commit()
            first.execute('BEGIN IMMEDIATE')
            try:
                second.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as exc:
                locked=exc
            else:
                self.fail('fixture did not reproduce SQLITE_BUSY')
            with patch.object(observation,'connect_observation_database',side_effect=locked):
                self.assertFalse(observation.admit_new_job(config))
            self.assertFalse(observation.circuit_breaker_active(config))
            first.rollback()
            self.assertTrue(observation.admit_new_job(config))
            self.fixture._record_verified(config,'after-busy')
            self.assertTrue(observation.admit_new_job(config))
        finally:
            second.close(); first.close()

    def test_nonbusy_database_fault_still_trips(self):
        config=self.fixture._config('fault')
        with patch.object(observation,'connect_observation_database',side_effect=sqlite3.DatabaseError('database disk image is malformed')):
            self.assertFalse(observation.admit_new_job(config))
        self.assertTrue(observation.circuit_breaker_active(config))

    def test_publish_review_terminal_next_claim_and_failed_gate_restart(self):
        config=self.fixture._config('cycle')
        for index in range(20):
            self.assertTrue(observation.admit_new_job(config))
            identity=f'cycle-{index}'
            observation.record_job_claim(config,job_identity=identity,claimed_at=time.time())
            if index % 2:
                outcome={'terminal_status':'NEEDS_REVIEW','failed':True,'verified_completed':False,
                         'stage':'source_selection_review','error_code':'source_selection_needs_review',
                         'reason_code':'asr_cache_acceptance_unproven'}
                evidence=strict_evidence_template(passed=False)
            else:
                outcome=self.fixture._verified(); evidence=strict_evidence_template(passed=True)
            result=observation.record_job_result(config,job_identity=identity,outcome=outcome,strict_evidence=evidence)
            self.assertTrue(observation.admit_new_job(config))
        gate_path=Path(config.m2_server_canary_observation_output_dir)/(result['gate_id']+'.json')
        original=gate_path.read_bytes()
        report=json.loads(original)
        self.assertEqual(report['status'],'SETTLED')
        self.assertEqual(report['strict_verified_count'],10)
        importlib.reload(observation)
        self.assertEqual(runtime_guardrail_status(config)['status'],'ARMED')
        self.assertTrue(observation.admit_new_job(config))
        observation.record_job_claim(config,job_identity='automatic-next-outside-cohort',claimed_at=time.time())
        observation.record_job_result(config,job_identity='automatic-next-outside-cohort',outcome=self.fixture._verified(),strict_evidence=strict_evidence_template(passed=True))
        self.assertEqual(gate_path.read_bytes(),original)
        self.assertTrue(observation.admit_new_job(config))

