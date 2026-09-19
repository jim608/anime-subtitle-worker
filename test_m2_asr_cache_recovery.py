"""Existing controlled reconciliation, exact cached-ASR admission incident only."""
from datetime import datetime, timezone
import json
from pathlib import Path
import m2_guardrail_runtime as runtime
from m2_production_observation import circuit_breaker_state_path
from safe_files import sha256_file
from test_m2_postprocess_recovery import PostprocessRecoveryTests


class AsrCacheRecoveryTests(PostprocessRecoveryTests):
    def setUp(self):
        super().setUp()
        self.f.request['asr_cache_incident'] = self.f.request.pop('asr_postprocess_incident')
        self.report['contract'] = 'm2-asr-cache-admission-regression-v1'
        self.report.update({key:True for key in (
            'legacy_cache_reproduced','prepublication_review','terminal_next_claim',
            'busy_not_latched','corrupt_database_latched','settled_gate_continues',
            'original_incident_preserved','unchanged_strict_validator')})
        self.report['code_sha256'] = {name:sha256_file(Path(runtime.__file__).parent/name)
            for name in ('worker.py','m2_guardrail_runtime.py','m2_production_observation.py',
                         'm2_strict_runtime_evidence.py','m2_strict_observation.py')}

    def evidence(self):
        evidence = super().evidence()
        evidence['root_cause']['incident_kind'] = 'asr_cache_acceptance_unproven'
        return evidence

    def test_unproven_next_claim_cannot_release(self):
        self.report['terminal_next_claim'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError,'regression_unproven'):
            self.recover(self.evidence())

    def add_busy(self, declared=True):
        event = {'reason_code':'observation_state_degraded','observed_at':self.f.now+5.5,
                 'evidence':{'stage':'admission','error_code':'state_recovery_failed'}}
        breaker=self.f.request['breaker']; breaker['reasons'].append(event); breaker['latest_trip']=event
        circuit_breaker_state_path(self.config).write_text(json.dumps(breaker))
        if declared:
            log=Path(self.config.log_path)/'original-busy.log'
            stamp=datetime.fromtimestamp(event['observed_at'],tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            log.write_text(stamp+' M2 observation admission validation failed: database is locked\n'
                           'sqlite3.OperationalError: database is locked\n')
            self.incident['followup_dispositions']=[{'trip':event,'disposition':'TRANSIENT_SQLITE_BUSY_RESOLVED',
                'original_traceback':{'path':str(log),'sha256':'sha256:'+sha256_file(log)}}]

    def test_exact_busy_followup_and_healthy_database_can_recover(self):
        self.add_busy()
        self.assertEqual(self.recover(self.evidence())['status'],'DISARMED')

    def test_unclassified_busy_cannot_be_ignored(self):
        self.add_busy(declared=False)
        with self.assertRaisesRegex(runtime.RuntimeContractError,'unresolved_breaker'):
            self.recover(self.evidence())

    def test_forged_busy_traceback_cannot_release(self):
        self.add_busy()
        Path(self.config.log_path,'original-busy.log').write_text('some other incident')
        with self.assertRaisesRegex(runtime.RuntimeContractError,'busy_evidence_unproven'):
            self.recover(self.evidence())

