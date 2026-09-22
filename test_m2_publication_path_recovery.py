"""Exact current-incident recovery, no reuse of source-transcript/SRT receipts."""
from pathlib import Path
import m2_guardrail_runtime as runtime
from safe_files import sha256_file
from test_m2_postprocess_recovery import PostprocessRecoveryTests


class PublicationPathRecoveryTests(PostprocessRecoveryTests):
    def setUp(self):
        super().setUp()
        self.f.request['publication_path_incident'] = self.f.request.pop('asr_postprocess_incident')
        self.report['contract'] = 'm2-publication-path-regression-v1'
        self.report.update({key: True for key in (
            'normalizer_rename_reproduced', 'canonical_publication_verified',
            'legacy_receipt_exact_path_required', 'valid_existing_output_preserved',
            'original_incident_preserved', 'terminal_next_claim',
        )})
        self.report['code_sha256'] = {
            name: sha256_file(Path(runtime.__file__).parent / name)
            for name in ('worker.py', 'subtitle_paths.py', 'output_manifest.py', 'subtitle_quality.py',
                         'm2_strict_runtime_evidence.py', 'main.py', 'm2_guardrail_runtime.py')
        }

    def evidence(self):
        evidence = super().evidence()
        evidence['root_cause']['incident_kind'] = 'publication_path_normalization'
        return evidence

    def test_other_code_cannot_release_breaker(self):
        self.report['code_sha256']['subtitle_paths.py'] = '0' * 64
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'code_not_tested'):
            self.recover(self.evidence())

    def test_old_incident_proof_cannot_release(self):
        self.report['contract'] = 'm2-source-transcript-evidence-regression-v1'
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_unverified_legacy_relocation_cannot_release(self):
        self.report['legacy_receipt_exact_path_required'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_safe_scope_resumes_but_incident_stays_held(self):
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
