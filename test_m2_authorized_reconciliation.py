import json
from pathlib import Path
import unittest

import m2_guardrail_runtime as runtime
from m2_observation_store import gate_by_id
from m2_production_recovery import require_source_not_held, RecoveryError
import test_m2_planned_runtime_change as planned_tests


class AuthorizedReconciliationTests(unittest.TestCase):
    settled_origin = False

    def setUp(self):
        self.f = planned_tests.PlannedRuntimeChangeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        if self.settled_origin:
            self.f.settle_failed_cohort()
        self.f.deploy()
        self.now = self.f.now
        self.config = self.f.config
        self.connection = self.f.connection
        self.original_bytes = Path(self.f.prepared['receipt_path']).read_bytes()
        self.original = json.loads(self.original_bytes)
        self.good = self.f.fixture.root / 'new-safe.mkv'
        self.good.write_bytes(b'new-safe-source')
        self.f.state.upsert_ai_queue_candidate(self.good, self.good.stat().st_mtime_ns)
        self.f.state.commit()
        runtime.pause_reconciliation_admission(self.config, 'authorized-test', now=self.now + 6)
        identity_keys = ('worker_commit_sha', 'worker_container_id', 'worker_source_revision', 'worker_image_id',
                         'worker_runtime_instance_fingerprint', 'webui_commit_sha', 'webui_source_revision',
                         'configuration_fingerprint', 'decision')
        self.request = {
            'authorization': {'scope': 'isolate_unknown_source_and_reconcile', 'reference': 'explicit-user-authorization'},
            'old_receipt': {'path': self.f.prepared['receipt_path'], 'sha256': self.f.prepared['receipt_sha256']},
            'old_gate_id': self.f.old_gate, 'old_gate': gate_by_id(self.connection, self.f.old_gate),
            'runtime_identity': {k: self.f.evidence[k] for k in identity_keys},
            'expected_new_worker_sha': self.f.evidence['worker_commit_sha'],
            'original_queue_members': [[str(self.f.media), self.f.media.stat().st_mtime_ns]],
            'differences': [
                {'path': str(self.f.media), 'disposition': 'QUARANTINE',
                 'evidence': {'old_identity': {'mtime_ns': 1}, 'current_identity': {'mtime_ns': 2}}},
                {'path': str(self.good), 'disposition': 'VERIFIED_NEW',
                 'evidence': {'source_verified': True}},
            ],
            'recoverable_identities': [{'path': str(self.good), 'mtime_ns': self.good.stat().st_mtime_ns}],
            'verified_snapshot': runtime._planned_change_snapshot(self.connection, self.f.old_gate),
        }
        from m2_production_observation import circuit_breaker_state_path
        self.request['breaker'] = json.loads(circuit_breaker_state_path(self.config).read_text())

    def prepare(self):
        return runtime.prepare_reconciliation_local(self.config, 'authorized-test', self.request, now=self.now + 6)

    def recovery_evidence(self, prepared):
        evidence = dict(self.f.evidence)
        evidence['fault_results'] = dict(evidence['fault_results'])
        evidence['fault_results'].update({'container_started_at_epoch': self.now + 3,
                                         'started_at_epoch': self.now + 7, 'finished_at_epoch': self.now + 8})
        evidence['root_cause'] = {'mode': 'authorized_reconciliation', 'breaker_reason': 'runtime_change',
            'affected_stage': 'runtime_validation', 'failure_code': 'live_worker_container_identity_mismatch',
            'expected_old_gate_id': self.f.old_gate, 'planned_change_receipt': prepared['receipt_path'],
            'planned_change_receipt_sha256': prepared['receipt_sha256']}
        return evidence

    def test_new_record_recovers_without_rewriting_old_receipt_or_releasing_holds(self):
        prepared = self.prepare()
        evidence = self.recovery_evidence(prepared)
        result = runtime.recover_runtime_local(self.config, evidence,
            source_revision_file=self.f.fixture.revision, now=self.now + 10)
        self.assertEqual('DISARMED', result['status'])
        self.assertEqual(self.request['old_gate'], gate_by_id(self.connection, self.f.old_gate))
        self.assertEqual(self.original_bytes, Path(self.f.prepared['receipt_path']).read_bytes())
        runtime.initialize_gate(self.config, evidence, source_revision_file=self.f.fixture.revision, now=self.now + 11)
        resumed = runtime.resume_claims_local(self.config, source_revision_file=self.f.fixture.revision, now=self.now + 12)
        self.assertTrue(resumed['claims_resumed'])
        require_source_not_held(self.config, self.good)
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            require_source_not_held(self.config, self.f.media)

    def test_new_difference_after_sealing_refuses_recovery(self):
        prepared = self.prepare()
        extra = self.f.fixture.root / 'unclassified-new.mkv'
        self.f.state.upsert_ai_queue_candidate(extra, 123)
        self.f.state.commit()
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'reconciliation_new_difference'):
            runtime.recover_runtime_local(self.config, self.recovery_evidence(prepared),
                source_revision_file=self.f.fixture.revision, now=self.now + 10)
        self.assertEqual(self.original_bytes, Path(self.f.prepared['receipt_path']).read_bytes())

    def test_omitted_difference_refuses_preparation(self):
        self.request['differences'] = self.request['differences'][:1]
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'reconciliation_undisposed_queue_difference'):
            self.prepare()


class SettledAuthorizedReconciliationTests(AuthorizedReconciliationTests):
    settled_origin = True


if __name__ == '__main__':
    unittest.main()
