"""Bind source-transcript evidence repair to the new, exact durable incident."""
import hashlib
import json
from pathlib import Path
from unittest import mock

import m2_guardrail_runtime as runtime
from m2_observation_store import enroll_claim, gate_by_id
from safe_files import sha256_file
import test_m2_postprocess_recovery as parent
import test_m2_planned_runtime_change as planned


class SourceTranscriptRecoveryTests(parent.PostprocessRecoveryTests):
    def setUp(self):
        original_deploy = planned.PlannedRuntimeChangeTests.deploy
        def deploy_with_historical_member(fixture):
            obligation = fixture.state.ensure_ai_delivery_obligation(fixture.media,
                media_size=fixture.media.stat().st_size, media_mtime_ns=fixture.media.stat().st_mtime_ns,
                policy_revision='planned-change-test')
            enroll_claim(fixture.connection, fixture.old, claim_identity='original-historical-attempt',
                gate_job_identity=obligation['obligation_id'], input_fingerprint='incident-source',
                claimed_at=fixture.now-5, processing_strategy='ASR_JA_AUDIO', eligible=True,
                eligibility_reason='eligible', now=fixture.now-5)
            fixture.state.commit()
            original_deploy(fixture)
        with mock.patch.object(planned.PlannedRuntimeChangeTests, 'deploy', deploy_with_historical_member):
            super().setUp()
        self.f.request['source_transcript_incident'] = self.f.request.pop('asr_postprocess_incident')
        self.report['contract'] = 'm2-source-transcript-evidence-regression-v1'
        self.report.update({key: True for key in (
            'actual_transcript_mismatch_reproduced', 'bound_manifest_transcript_verified',
            'unaccepted_cache_rejected', 'full_hallucination_scan', 'terminal_next_claim',
            'original_incident_preserved', 'unchanged_strict_predicates',
        )})
        self.report['code_sha256'] = {name: sha256_file(Path(runtime.__file__).parent / name)
            for name in ('m2_strict_runtime_evidence.py', 'm2_strict_observation.py', 'output_manifest.py',
                         'm2_guardrail_runtime.py', 'main.py', 'worker.py')}
        attempt = self.incident['attempt_id']; obligation = self.incident['obligation_id']
        self.connection.execute("UPDATE ai_delivery_attempts SET started_at=?,finished_at=?,detail=? WHERE attempt_id=?",
            (self.f.now, self.f.now + 4, 'M2 strict completion rejected: final_state_completed,hallucination_validation_pass', attempt))
        payload = json.dumps({'outcome': {'stage': 'm2_strict_completion', 'error_code': 'incorrect_completion',
                                         'terminal_status': 'NEEDS_REVIEW'}}, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        self.connection.execute('INSERT INTO m2_observation_result_events VALUES(?,?,?,?,?,?,?)',
            (hashlib.sha256(attempt.encode()).hexdigest(), self.f.f.old_gate, hashlib.sha256(obligation.encode()).hexdigest(),
             'NEEDS_REVIEW', digest, payload, self.f.now + 4.5))
        self.incident.update(result_gate_id=self.f.f.old_gate, event_sha256=digest)
        self.f.f.state.commit()
        self.f.request['old_gate'] = gate_by_id(self.connection, self.f.f.old_gate)
        self.f.request['verified_snapshot'] = runtime._planned_change_snapshot(self.connection, self.f.f.old_gate)

    def evidence(self):
        result = super().evidence()
        result['root_cause']['incident_kind'] = 'source_transcript_evidence_bridge'
        return result

    def test_other_code_cannot_release_breaker(self):
        self.report['code_sha256']['m2_strict_runtime_evidence.py'] = '0' * 64
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'code_not_tested'):
            self.recover(self.evidence())

    def test_old_incident_proof_cannot_authorize_current_recovery(self):
        self.report['contract'] = 'm2-source-srt-parse-regression-v1'
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_missing_actual_transcript_scan_refuses_recovery(self):
        self.report['full_hallucination_scan'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_result_event_hash_or_gate_mismatch_refuses_recovery(self):
        self.incident['event_sha256'] = '0' * 64
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'claim_binding_unproven'):
            self.recover(self.evidence())

    def test_prior_cohort_binding_does_not_require_moving_member(self):
        request = {**self.f.request, 'old_gate_id': 'different-current-gate'}
        self.assertTrue(runtime._source_transcript_incident_bound(self.connection, request, self.incident, self.f.request['breaker']))
        self.incident['result_gate_id'] = 'different-current-gate'
        self.assertFalse(runtime._source_transcript_incident_bound(self.connection, request, self.incident, self.f.request['breaker']))
