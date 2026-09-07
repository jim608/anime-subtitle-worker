"""Exact incident recovery; isolated fixture DBs only."""
import hashlib
import json
from pathlib import Path
import unittest

import m2_guardrail_runtime as runtime
from m2_production_observation import circuit_breaker_state_path
from safe_files import sha256_file
from test_m2_authorized_reconciliation import AuthorizedReconciliationTests


class PostprocessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = AuthorizedReconciliationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config = self.f.config
        self.connection = self.f.connection
        attempt = self.f.f.old_delivery_attempt(self.f.now - 86400)
        self.connection.execute(
            "UPDATE ai_delivery_attempts SET status='review_required',error_code='incorrect_completion',"
            "stage='m2_strict_completion' WHERE attempt_id=?", (attempt,))
        obligation = self.connection.execute(
            'SELECT obligation_id FROM ai_delivery_attempts WHERE attempt_id=?', (attempt,)).fetchone()[0]
        self.f.f.state.commit()
        trip = {'reason_code':'incorrect_completion', 'observed_at':self.f.now+5,
                'evidence':{'stage':'m2_strict_completion','strict_failure_count':2,
                    'gate_id':self.f.f.old_gate,'claim_identity_hash':hashlib.sha256(attempt.encode()).hexdigest()}}
        breaker_path = circuit_breaker_state_path(self.config)
        breaker = json.loads(breaker_path.read_text())
        breaker['latest_trip'] = trip
        breaker['reasons'].append(trip)
        breaker_path.write_text(json.dumps(breaker))
        self.f.request['breaker'] = breaker
        self.incident = {'attempt_id':attempt, 'obligation_id':obligation,
                         'canonical_path':str(self.f.f.media), 'trip':trip}
        self.f.request['asr_postprocess_incident'] = self.incident
        self.f.request['verified_snapshot'] = runtime._planned_change_snapshot(self.connection, self.f.f.old_gate)
        self.proof_path = Path(self.config.log_path)/'postprocess-proof.json'
        self.report = {'contract':'m2-asr-postprocess-regression-v1','status':'PASS',
            'worker_source_revision':self.f.f.evidence['worker_source_revision'],
            'worker_image_id':self.f.f.evidence['worker_image_id'],'production_resources_affected':False,
            **{key:True for key in ('actual_container_restart','checkpoint_restore','idempotency',
                'shared_quality_gate','missing_evidence_rejected','source_unchanged','tested_runtime_image')},
            'code_sha256':{name:sha256_file(Path(runtime.__file__).parent/name)
                for name in ('worker.py','asr_postprocess.py','m2_guardrail_runtime.py')}}
        self.report['logs'] = []
        for name in ('isolated-tests.log','isolated-restart.log'):
            path = Path(self.config.log_path)/name
            path.write_text('isolated test fixture evidence')
            self.report['logs'].append({'path':str(path),'sha256':'sha256:'+sha256_file(path)})

    def evidence(self):
        prepared = self.f.prepare()
        evidence = self.f.recovery_evidence(prepared)
        self.proof_path.write_text(json.dumps(self.report))
        evidence['root_cause'].update({'incident_kind':'asr_postprocess_diagnostics_loss',
            'breaker_reason':'incorrect_completion','affected_stage':'m2_strict_completion',
            'failure_code':'incorrect_completion','incident':self.incident,
            'regression_results':{'path':str(self.proof_path),'sha256':'sha256:'+sha256_file(self.proof_path)}})
        return evidence

    def recover(self, evidence):
        return runtime.recover_runtime_local(self.config,evidence,
            source_revision_file=self.f.f.fixture.revision,now=self.f.now+10)

    def test_exact_incident_recovers_without_relabeling_or_requeue(self):
        evidence = self.evidence()
        original = Path(self.f.f.prepared['receipt_path']).read_bytes()
        result = self.recover(evidence)
        self.assertEqual(result['status'],'DISARMED')
        self.assertEqual(result['reconciliation']['requeued'],0)
        self.assertEqual(Path(self.f.f.prepared['receipt_path']).read_bytes(),original)
        self.assertEqual(self.connection.execute('SELECT status FROM ai_delivery_attempts WHERE attempt_id=?',
            (self.incident['attempt_id'],)).fetchone()[0],'review_required')
        # Same sealed evidence resumes an interrupted handoff; no new dispatch.
        resumed = self.recover(evidence)
        self.assertEqual(resumed['recovery_record_id'],result['recovery_record_id'])

    def test_unproven_restart_cannot_release_breaker(self):
        self.report['actual_container_restart'] = False
        evidence = self.evidence()
        before = circuit_breaker_state_path(self.config).read_bytes()
        with self.assertRaisesRegex(runtime.RuntimeContractError,'regression_unproven'):
            self.recover(evidence)
        self.assertEqual(circuit_breaker_state_path(self.config).read_bytes(),before)

    def test_other_code_cannot_release_breaker(self):
        self.report['code_sha256']['worker.py'] = '0'*64
        with self.assertRaisesRegex(runtime.RuntimeContractError,'code_not_tested'):
            self.recover(self.evidence())

    def test_incident_without_hold_cannot_release_breaker(self):
        self.f.request['differences'][0]['disposition'] = 'VERIFIED_NEW'
        self.f.request['recoverable_identities'].append({'path':str(self.f.f.media),
            'mtime_ns':self.f.f.media.stat().st_mtime_ns})
        with self.assertRaisesRegex(runtime.RuntimeContractError,'incident_not_held'):
            self.recover(self.evidence())

    def test_new_unrelated_trip_cannot_release_breaker(self):
        path = circuit_breaker_state_path(self.config)
        breaker = self.f.request['breaker']
        breaker['latest_trip'] = {'reason_code':'source_mutation','observed_at':self.f.now+5.5}
        breaker['reasons'].append(breaker['latest_trip'])
        path.write_text(json.dumps(breaker))
        with self.assertRaisesRegex(runtime.RuntimeContractError,'unresolved_breaker'):
            self.recover(self.evidence())

    def test_unknown_incident_mode_is_not_generic_override(self):
        evidence = self.evidence()
        evidence['root_cause']['incident_kind'] = 'unproven_other_failure'
        with self.assertRaisesRegex(runtime.RuntimeContractError,'unsupported_reconciliation_incident'):
            self.recover(evidence)


if __name__ == '__main__':
    unittest.main()
