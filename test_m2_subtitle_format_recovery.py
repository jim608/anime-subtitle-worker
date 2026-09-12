"""Exact format incident recovery in isolated durable stores; never generic reset."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import m2_guardrail_runtime as runtime
from m2_observation_store import enroll_claim, gate_by_id
from m2_production_observation import circuit_breaker_state_path
from m2_production_recovery import require_source_not_held, RecoveryError
from safe_files import sha256_file
from source_integrity import capture_source_snapshot
import test_m2_planned_runtime_change as fixtures


class SubtitleFormatRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PlannedRuntimeChangeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config, self.connection, self.now = self.f.config, self.f.connection, self.f.now
        self.members = []
        for index in range(3):
            video = self.f.fixture.root / f'format-{index}.mkv'
            video.write_bytes(f'synthetic-video-{index}'.encode())
            source = video.with_suffix('.zh.srt')
            source.write_text('1\n00:00:01,000 --> 00:00:05,000\n我们已经准备好了。\n', encoding='utf-8')
            self.f.state.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
            obligation = self.f.state.ensure_ai_delivery_obligation(video,
                media_size=video.stat().st_size, media_mtime_ns=video.stat().st_mtime_ns,
                policy_revision='planned-change-test')['obligation_id']
            attempt = self.f.state.begin_ai_delivery_attempt(obligation)['attempt_id']
            started = self.now - 9 + index
            self.connection.execute("UPDATE ai_delivery_attempts SET status='retryable_failure',"
                "error_code='opencc_unknown',stage='opencc',detail='ASS source contains no Dialogue events',"
                "started_at=?,finished_at=?,updated_at=? WHERE attempt_id=?",
                (started, started + 0.5, started + 0.5, attempt))
            enroll_claim(self.connection, self.f.old, claim_identity=attempt,
                gate_job_identity=obligation, input_fingerprint=sha256_file(video), claimed_at=started,
                processing_strategy='CONVERT_ZH_CN', eligible=True, eligibility_reason='eligible', now=started)
            self.members.append({'attempt_id': attempt, 'obligation_id': obligation,
                'canonical_path': str(video), 'disposition': 'VERIFIED_CURRENT_SOURCE',
                'media_snapshot': capture_source_snapshot(video, hash_content=True).as_evidence(),
                'selected_source_snapshot': capture_source_snapshot(source, hash_content=True).as_evidence()})
        self.f.state.commit()
        self.queue_members = [list(row) for row in self.connection.execute(
            'SELECT path,mtime_ns FROM ai_candidate_queue ORDER BY path')]
        self.f.deploy()
        runtime.pause_reconciliation_admission(self.config, 'format-test', now=self.now + 6)
        keys = [hashlib.sha256(m['obligation_id'].encode()).hexdigest()[:16] for m in self.members]
        self.trip = {'reason_code': 'repeated_identical_stage_failure', 'observed_at': self.now - 1,
            'evidence': {'stage': 'opencc', 'error_code': 'opencc_unknown',
                'normalized_failure_signature': 'opencc:opencc_unknown', 'identical_failure_job_ids': keys,
                'identical_failure_streak': 3, 'gate_id': self.f.old_gate, 'job_key': keys[-1],
                'claim_identity_hash': hashlib.sha256(self.members[-1]['attempt_id'].encode()).hexdigest()}}
        self.breaker = json.loads(circuit_breaker_state_path(self.config).read_text())
        self.breaker['reasons'].insert(0, self.trip)
        self.save_breaker()
        self.incident = {'trip': self.trip, 'members': self.members}
        identity_keys = ('worker_commit_sha', 'worker_container_id', 'worker_source_revision', 'worker_image_id',
                        'worker_runtime_instance_fingerprint', 'webui_commit_sha', 'webui_source_revision',
                        'configuration_fingerprint', 'decision')
        self.request = {
            'authorization': {'reference': 'explicit-isolation-current-state-reconciliation', 'historical_preservation': 'UNPROVEN'},
            'old_receipt': {'path': self.f.prepared['receipt_path'], 'sha256': self.f.prepared['receipt_sha256']},
            'old_gate_id': self.f.old_gate, 'old_gate': gate_by_id(self.connection, self.f.old_gate),
            'runtime_identity': {k: self.f.evidence[k] for k in identity_keys},
            'expected_new_worker_sha': self.f.evidence['worker_commit_sha'],
            'original_queue_members': self.queue_members,
            'differences': [{'path': str(self.f.media), 'disposition': 'QUARANTINE',
                'evidence': {'old_identity': {'mtime_ns': 1}, 'current_identity': {'mtime_ns': 2}}}],
            'recoverable_identities': [{'path': m['canonical_path'], 'mtime_ns': m['media_snapshot']['mtime_ns']} for m in self.members],
            'verified_snapshot': runtime._planned_change_snapshot(self.connection, self.f.old_gate),
            'breaker': self.breaker, 'subtitle_format_incident': self.incident,
        }
        self.proof_path = Path(self.config.log_path) / 'format-proof.json'
        self.report = {'contract': 'm2-subtitle-format-regression-v1', 'status': 'PASS',
            'worker_source_revision': self.f.evidence['worker_source_revision'],
            'worker_image_id': self.f.evidence['worker_image_id'], 'production_resources_affected': False,
            **{key: True for key in ('actual_container_restart', 'checkpoint_restore', 'idempotency',
                'shared_quality_gate', 'missing_evidence_rejected', 'source_unchanged', 'tested_runtime_image',
                'srt_misdispatch_reproduced', 'srt_and_ass_verified', 'valid_output_preserved',
                'original_incident_preserved', 'late_hold_refused', 'unchanged_strict_validator')},
            'code_sha256': {name: sha256_file(Path(runtime.__file__).parent / name) for name in (
                'worker.py', 'opencc_convert.py', 'ass_utils.py', 'output_manifest.py', 'source_decision.py',
                'm2_strict_runtime_evidence.py', 'm2_guardrail_runtime.py')}, 'logs': []}
        for name in ('format-tests.log', 'format-restart.log'):
            path = Path(self.config.log_path) / name
            path.write_text('isolated verifier contract fixture, not Production acceptance')
            self.report['logs'].append({'path': str(path), 'sha256': 'sha256:' + sha256_file(path)})

    def save_breaker(self):
        circuit_breaker_state_path(self.config).write_text(json.dumps(self.breaker))

    def evidence(self):
        prepared = runtime.prepare_reconciliation_local(self.config, 'format-test', self.request, now=self.now + 6)
        evidence = copy.deepcopy(self.f.evidence)
        evidence['fault_results'].update({'container_started_at_epoch': self.now + 3,
            'started_at_epoch': self.now + 7, 'finished_at_epoch': self.now + 8})
        self.proof_path.write_text(json.dumps(self.report))
        evidence['root_cause'] = {'mode': 'authorized_reconciliation', 'incident_kind': 'subtitle_format_dispatch',
            'breaker_reason': 'repeated_identical_stage_failure', 'affected_stage': 'opencc', 'failure_code': 'opencc_unknown',
            'expected_old_gate_id': self.f.old_gate, 'planned_change_receipt': prepared['receipt_path'],
            'planned_change_receipt_sha256': prepared['receipt_sha256'], 'incident': copy.deepcopy(self.incident),
            'regression_results': {'path': str(self.proof_path), 'sha256': 'sha256:' + sha256_file(self.proof_path)}}
        return evidence

    def recover(self, evidence):
        return runtime.recover_runtime_local(self.config, evidence,
            source_revision_file=self.f.fixture.revision, now=self.now + 10)

    def assert_refused(self, reason):
        evidence = self.evidence()
        before = circuit_breaker_state_path(self.config).read_bytes()
        with self.assertRaisesRegex(runtime.RuntimeContractError, reason):
            self.recover(evidence)
        self.assertEqual(before, circuit_breaker_state_path(self.config).read_bytes())

    def test_exact_incident_recovery_preserves_attempts_gate_receipt_and_holds(self):
        evidence = self.evidence()
        old_receipt = Path(self.f.prepared['receipt_path']).read_bytes()
        before = self.connection.execute('SELECT * FROM ai_delivery_attempts ORDER BY attempt_id').fetchall()
        gate = gate_by_id(self.connection, self.f.old_gate)
        result = self.recover(evidence)
        self.assertEqual(result['status'], 'DISARMED')
        self.assertEqual(result['reconciliation']['requeued'], 0)
        self.assertEqual(before, self.connection.execute('SELECT * FROM ai_delivery_attempts ORDER BY attempt_id').fetchall())
        self.assertEqual(gate, gate_by_id(self.connection, self.f.old_gate))
        self.assertEqual(old_receipt, Path(self.f.prepared['receipt_path']).read_bytes())
        replay = self.recover(evidence)
        self.assertEqual(result['recovery_record_id'], replay['recovery_record_id'])
        runtime.initialize_gate(self.config, evidence, source_revision_file=self.f.fixture.revision, now=self.now + 11)
        self.assertTrue(runtime.resume_claims_local(self.config,
            source_revision_file=self.f.fixture.revision, now=self.now + 12)['claims_resumed'])
        require_source_not_held(self.config, self.members[0]['canonical_path'])
        with self.assertRaises(RecoveryError):
            require_source_not_held(self.config, self.f.media)

    def test_missing_actual_restart_refused(self):
        self.report['actual_container_restart'] = False
        self.assert_refused('regression_unproven')

    def test_wrong_candidate_code_refused(self):
        self.report['code_sha256']['worker.py'] = '0' * 64
        self.assert_refused('code_not_tested')

    def test_unverified_valid_output_protection_refused(self):
        self.report['valid_output_preserved'] = False
        self.assert_refused('regression_unproven')

    def test_missing_trip_member_refused(self):
        self.members.pop()
        self.assert_refused('trip_signature_invalid')

    def test_duplicate_trip_member_refused(self):
        self.members[-1] = self.members[0]
        self.assert_refused('member_binding_invalid')

    def test_wrong_failure_detail_refused(self):
        self.connection.execute('UPDATE ai_delivery_attempts SET detail=? WHERE attempt_id=?',
            ('different root cause', self.members[0]['attempt_id']))
        self.f.state.commit()
        self.request['verified_snapshot'] = runtime._planned_change_snapshot(self.connection, self.f.old_gate)
        self.assert_refused('attempt_not_preserved')

    def test_other_gate_binding_refused(self):
        self.trip['evidence']['gate_id'] = 'other-gate'
        self.save_breaker()
        self.assert_refused('trip_signature_invalid')

    def test_wrong_trip_claim_refused(self):
        self.trip['evidence']['claim_identity_hash'] = '0' * 64
        self.save_breaker()
        self.assert_refused('trip_claim_unproven')

    def test_source_mutation_since_snapshot_refused(self):
        Path(self.members[0]['selected_source_snapshot']['canonical_path']).write_text('changed fixture')
        self.assert_refused('current_source_unproven')

    def test_new_or_earlier_unrelated_trip_refused(self):
        self.breaker['reasons'].insert(0, {'reason_code': 'source_mutation', 'observed_at': self.now - 100})
        self.save_breaker()
        self.assert_refused('unresolved_breaker')

    def test_unclassified_delta_after_seal_refused(self):
        evidence = self.evidence()
        extra = self.f.fixture.root / 'unexplained.mkv'
        self.f.state.upsert_ai_queue_candidate(extra, 123)
        self.f.state.commit()
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'reconciliation_new_difference'):
            self.recover(evidence)

    def test_current_unknown_source_can_stay_held_without_blocking_safe_scope(self):
        member = self.members[0]
        member['disposition'] = 'HELD_UNKNOWN_CONTINUITY'
        self.request['recoverable_identities'] = self.request['recoverable_identities'][1:]
        self.request['differences'].append({'path': member['canonical_path'], 'disposition': 'QUARANTINE',
            'evidence': {'old_identity': member['media_snapshot'], 'current_identity': {'continuity': 'UNKNOWN'}}})
        evidence = self.evidence()
        Path(member['selected_source_snapshot']['canonical_path']).unlink()
        result = self.recover(evidence)
        self.assertEqual(result['status'], 'DISARMED')
        runtime.initialize_gate(self.config, evidence, source_revision_file=self.f.fixture.revision, now=self.now + 11)
        runtime.resume_claims_local(self.config, source_revision_file=self.f.fixture.revision, now=self.now + 12)
        with self.assertRaises(RecoveryError):
            require_source_not_held(self.config, member['canonical_path'])
        require_source_not_held(self.config, self.members[1]['canonical_path'])

    def test_missing_manifest_idempotency_proof_refused(self):
        self.report['idempotency'] = False
        self.assert_refused('regression_unproven')

    def test_invalidated_gate_claim_cannot_be_replaced_by_other_attempt(self):
        self.members[0]['attempt_id'] = self.members[1]['attempt_id']
        self.assert_refused('attempt_not_preserved')

    def test_root_incident_changed_after_sealing_refused(self):
        evidence = self.evidence()
        evidence['root_cause']['incident']['members'][0]['disposition'] = 'HELD_UNKNOWN_CONTINUITY'
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'incident_changed'):
            self.recover(evidence)

    def test_restart_after_recovery_db_commit_reuses_one_durable_record(self):
        evidence = self.evidence()
        real_write = runtime.atomic_write_text
        def interrupt(path, *args, **kwargs):
            result = real_write(path, *args, **kwargs)
            if Path(path).name.startswith('m2-production-recovery-'):
                raise RuntimeError('interrupted after durable recovery record')
            return result
        with patch.object(runtime, 'atomic_write_text', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'interrupted after durable'):
                self.recover(evidence)
        rows = self.connection.execute("SELECT event_id,payload_json FROM m2_recovery_events "
            "WHERE event_type='CONTROLLED_BREAKER_RECOVERY'").fetchall()
        self.assertEqual(len(rows), 1)
        original_id = json.loads(rows[0][1])['recovery_record_id']
        result = self.recover(evidence)
        self.assertEqual(result['recovery_record_id'], original_id)
        self.assertEqual(rows, self.connection.execute("SELECT event_id,payload_json FROM m2_recovery_events "
            "WHERE event_type='CONTROLLED_BREAKER_RECOVERY'").fetchall())

    def interrupt_committed_recovery(self):
        evidence = self.evidence()
        real_write = runtime.atomic_write_text
        def interrupt(path, *args, **kwargs):
            result = real_write(path, *args, **kwargs)
            if Path(path).name.startswith('m2-production-recovery-'):
                raise RuntimeError('committed interrupt')
            return result
        with patch.object(runtime, 'atomic_write_text', side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, 'committed interrupt'):
                self.recover(evidence)
        return evidence

    def test_committed_recovery_refuses_new_snapshot_difference(self):
        evidence = self.interrupt_committed_recovery()
        self.f.state.upsert_ai_queue_candidate(self.f.fixture.root / 'late-delta.mkv', 456)
        self.f.state.commit()
        before = circuit_breaker_state_path(self.config).read_bytes()
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'committed_recovery_snapshot_changed'):
            self.recover(evidence)
        self.assertEqual(before, circuit_breaker_state_path(self.config).read_bytes())

    def test_committed_recovery_does_not_overwrite_changed_export(self):
        evidence = self.interrupt_committed_recovery()
        path, = Path(self.config.log_path).glob('m2-production-recovery-*.json')
        path.write_text('{"different": "fixture evidence"}')
        before = path.read_bytes()
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'export_conflict'):
            self.recover(evidence)
        self.assertEqual(before, path.read_bytes())

    def test_committed_recovery_preserves_export_mtime_on_retry(self):
        evidence = self.interrupt_committed_recovery()
        path, = Path(self.config.log_path).glob('m2-production-recovery-*.json')
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.recover(evidence)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))


if __name__ == '__main__':
    unittest.main()
