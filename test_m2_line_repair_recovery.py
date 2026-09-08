"""Line-repair recovery is a distinct, held, code-bound incident, not an override."""
from pathlib import Path
import json
import hashlib
import sqlite3
import unittest
from unittest.mock import patch

import m2_guardrail_runtime as runtime
from safe_files import sha256_file
from test_m2_postprocess_recovery import PostprocessRecoveryTests


class LineRepairRecoveryTests(PostprocessRecoveryTests):
    def setUp(self):
        super().setUp()
        self.f.request['line_repair_incident'] = self.f.request.pop('asr_postprocess_incident')
        self.report['contract'] = 'm2-line-repair-regression-v1'
        self.report.update({key: True for key in (
            'historical_source_proof_required', 'review_settlement_order_verified',
            'unchanged_strict_validator', 'original_incident_preserved',
        )})
        self.report['code_sha256'] = {
            name: sha256_file(Path(runtime.__file__).parent/name)
            for name in ('main.py','retranslate_ai_lines.py','m2_strict_runtime_evidence.py','m2_guardrail_runtime.py')
        }

    def evidence(self):
        evidence = super().evidence()
        evidence['root_cause']['incident_kind'] = 'line_repair_evidence_incomplete'
        return evidence

    def test_other_code_cannot_release_breaker(self):
        self.report['code_sha256']['main.py'] = '0'*64
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'code_not_tested'):
            self.recover(self.evidence())

    def test_old_asr_proof_is_not_valid_for_line_repair(self):
        self.report['contract'] = 'm2-asr-postprocess-regression-v1'
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())

    def test_source_history_cannot_be_inferred_from_new_hash(self):
        self.report['historical_source_proof_required'] = False
        with self.assertRaisesRegex(runtime.RuntimeContractError, 'regression_unproven'):
            self.recover(self.evidence())


class NoncohortLineClaimTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.executescript('''
            CREATE TABLE pipeline_jobs(job_id TEXT, canonical_path TEXT);
            CREATE TABLE ai_delivery_attempts(attempt_id TEXT, started_at REAL, finished_at REAL);
            CREATE TABLE pipeline_stage_attempts(stage_attempt_id TEXT, job_id TEXT, stage TEXT,
                input_json TEXT, input_sha256 TEXT, started_at REAL, finished_at REAL, status TEXT);
        ''')
        inputs = json.dumps({'delivery_attempt_id':'attempt-1','queue':'line_retranslation'},sort_keys=True,separators=(',',':'))
        self.connection.execute('INSERT INTO pipeline_jobs VALUES (?,?)',('job-1','/anime/episode.mkv'))
        self.connection.execute('INSERT INTO ai_delivery_attempts VALUES (?,?,?)',('attempt-1',101,105.1))
        self.connection.execute('INSERT INTO pipeline_stage_attempts VALUES (?,?,?,?,?,?,?,?)',
            ('stage-1','job-1','TRANSLATING',inputs,hashlib.sha256(inputs.encode()).hexdigest(),102,104,'SUCCEEDED'))
        self.incident={'attempt_id':'attempt-1','claim_stage_attempt_id':'stage-1','canonical_path':'/anime/episode.mkv',
            'command_id':'cmd-1','review_id':'review-1','failure_revision':'revision-1','trip':{'observed_at':105}}
        self.command={'action':'review.resolve_ai','status':'failed','target':'/anime/episode.mkv',
            'error':'M2 strict completion evidence rejected line repair','started_at':100.5,'finished_at':106,
            'parameters':{'remediation':'ai.retranslate_lines','review_id':'review-1','expected_failure_revision':'revision-1'}}

    def bound(self):
        with patch('control_state.get_command',return_value=self.command):
            return runtime._line_repair_noncohort_claim_bound(self.connection,object(),self.incident)

    def test_real_stage_and_command_binding_needs_no_fabricated_gate_row(self):
        self.assertTrue(self.bound())

    def test_missing_or_different_attempt_is_rejected(self):
        self.incident['attempt_id']='unrelated-attempt'
        self.assertFalse(self.bound())

    def test_unrelated_command_cannot_supply_binding(self):
        self.command['target']='/anime/another.mkv'
        self.assertFalse(self.bound())

    def test_changed_stage_evidence_is_rejected(self):
        self.connection.execute("UPDATE pipeline_stage_attempts SET input_sha256=?",('0'*64,))
        self.assertFalse(self.bound())

    def test_later_unrelated_incident_time_is_rejected(self):
        self.incident['trip']['observed_at']=200
        self.assertFalse(self.bound())


if __name__ == '__main__':
    unittest.main()
