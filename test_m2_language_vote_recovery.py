"""Source-vote recovery retains the exact held incident; never a generic reset."""
from pathlib import Path

import m2_guardrail_runtime as runtime
from safe_files import sha256_file
from test_m2_postprocess_recovery import PostprocessRecoveryTests


class LanguageVoteRecoveryTests(PostprocessRecoveryTests):
    def setUp(self):
        super().setUp()
        self.f.request['language_vote_incident'] = self.f.request.pop('asr_postprocess_incident')
        self.report['contract'] = 'm2-language-vote-regression-v1'
        self.report.update({key: True for key in (
            'source_vote_reproduced', 'old_cache_reaggregated',
            'decision_artifact_mismatch_preserved', 'review_settlement_order_verified',
            'unchanged_strict_validator', 'original_incident_preserved',
        )})
        self.report['code_sha256'] = {
            name: sha256_file(Path(runtime.__file__).parent / name)
            for name in ('main.py', 'language_detector.py', 'scan_state.py',
                         'm2_strict_runtime_evidence.py', 'm2_guardrail_runtime.py')
        }

    def evidence(self):
        evidence = super().evidence()
        evidence['root_cause']['incident_kind'] = 'source_language_vote_mismatch'
        return evidence

    def test_other_code_cannot_release_breaker(self):
        self.report['code_sha256']['language_detector.py'] = '0' * 64
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'code_not_tested'):
            self.recover(self.evidence())

    def test_postprocess_proof_cannot_authorize_language_recovery(self):
        self.report['contract'] = 'm2-asr-postprocess-regression-v1'
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_unverified_source_vote_cannot_authorize_recovery(self):
        self.report['source_vote_reproduced'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_old_cache_behavior_must_be_verified(self):
        self.report['old_cache_reaggregated'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_strict_validator_cannot_be_relaxed(self):
        self.report['unchanged_strict_validator'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_safe_scope_can_resume_but_incident_remains_held(self):
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
