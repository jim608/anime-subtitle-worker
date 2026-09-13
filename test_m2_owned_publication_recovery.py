"""Existing controlled reconciliation, bound to the exact publication incident."""
from pathlib import Path
import m2_guardrail_runtime as runtime
from safe_files import sha256_file
from test_m2_postprocess_recovery import PostprocessRecoveryTests


class OwnedPublicationRecoveryTests(PostprocessRecoveryTests):
    def setUp(self):
        super().setUp()
        self.f.request['owned_publication_incident'] = self.f.request.pop('asr_postprocess_incident')
        self.report['contract'] = 'm2-owned-publication-regression-v1'
        self.report.update({key: True for key in (
            'input_diff_reproduced', 'owned_journal_verified', 'arbitrary_drift_rejected',
            'original_incident_preserved', 'unchanged_strict_predicates',
        )})
        self.report['code_sha256'] = {
            name: sha256_file(Path(runtime.__file__).parent / name)
            for name in ('m2_strict_runtime_evidence.py', 'm2_guardrail_runtime.py', 'worker.py',
                         'source_inventory.py', 'source_decision.py', 'm2_production_observation.py')
        }

    def evidence(self):
        evidence = super().evidence()
        evidence['root_cause']['incident_kind'] = 'owned_publication_identity_mismatch'
        return evidence

    def test_other_code_cannot_release_breaker(self):
        self.report['code_sha256']['m2_strict_runtime_evidence.py'] = '0' * 64
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'code_not_tested'):
            self.recover(self.evidence())

    def test_incomplete_publication_proof_cannot_release_breaker(self):
        self.report['owned_journal_verified'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_old_incident_contract_cannot_authorize_new_incident(self):
        self.report['contract'] = 'm2-asr-postprocess-regression-v1'
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_safe_scope_resumes_but_original_incident_stays_held(self):
        from m2_production_recovery import require_source_not_held, RecoveryError
        evidence = self.evidence()
        self.recover(evidence)
        runtime.initialize_gate(self.config, evidence,
            source_revision_file=self.f.f.fixture.revision, now=self.f.now + 11)
        result = runtime.resume_claims_local(self.config,
            source_revision_file=self.f.f.fixture.revision, now=self.f.now + 12)
        self.assertTrue(result['claims_resumed'])
        require_source_not_held(self.config, self.f.good)
        with self.assertRaises(RecoveryError):
            require_source_not_held(self.config, self.f.f.media)
