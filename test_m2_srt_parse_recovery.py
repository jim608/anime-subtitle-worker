"""Exact source-parse incident binding through the existing reconciliation flow."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

import m2_guardrail_runtime as runtime
from safe_files import sha256_file
from source_integrity import capture_source_snapshot
import test_m2_subtitle_format_recovery as format_fixture


class SourceSrtParseRecoveryTest(format_fixture.SubtitleFormatRecoveryTest):
    def setUp(self):
        super().setUp()
        self.trip['evidence'].update(stage='worker',error_code='worker_unknown',
            normalized_failure_signature='worker:worker_unknown:e3b0c44298fc1c14')
        for index,member in enumerate(self.members,1):
            source=Path(member['selected_source_snapshot']['canonical_path'])
            raw=f'{300+index}\n00:00:00,000 --> 00:00:05,000'
            source.write_text(raw,encoding='utf-8')
            detail='Invalid SRT block, expected at least 3 lines: '+repr(raw)
            member.update(original_detail=detail,result_gate_id=self.f.old_gate,
                selected_source_snapshot=capture_source_snapshot(source,hash_content=True).as_evidence())
            self.connection.execute("UPDATE ai_delivery_attempts SET error_code='worker_unknown',stage='worker',detail=? WHERE attempt_id=?",
                (detail,member['attempt_id']))
            finished=self.connection.execute('SELECT finished_at FROM ai_delivery_attempts WHERE attempt_id=?',(member['attempt_id'],)).fetchone()[0]
            payload=json.dumps({'outcome':{'stage':'worker','error_code':'worker_unknown','failed':True},
                'breaker':{'identical_failure_job_ids':self.trip['evidence']['identical_failure_job_ids'][:index],
                    'identical_failure_streak':index,'normalized_failure_signature':self.trip['evidence']['normalized_failure_signature'],
                    'oom_streak':0,'tripped':index==3}},sort_keys=True)
            member['event_sha256']=hashlib.sha256(payload.encode()).hexdigest()
            self.connection.execute('INSERT INTO m2_observation_result_events VALUES(?,?,?,?,?,?,?)',
                (hashlib.sha256(member['attempt_id'].encode()).hexdigest(),self.f.old_gate,
                 hashlib.sha256(member['obligation_id'].encode()).hexdigest(),'RETRYING',member['event_sha256'],payload,finished+.1))
        self.f.state.commit();self.save_breaker()
        self.request['source_parse_incident']=self.request.pop('subtitle_format_incident')
        self.request['verified_snapshot']=runtime._planned_change_snapshot(self.connection,self.f.old_gate)
        self.report['contract']='m2-source-srt-parse-regression-v1'
        self.report.update({k:True for k in ('source_parse_reproduced','only_format_error_caught',
            'safe_fallback_or_review','terminal_next_claim','asr_cache_guard_preserved','busy_retry_preserved')})
        self.report['code_sha256']={n:sha256_file(Path(runtime.__file__).parent/n) for n in (
            'source_inventory.py','source_analyzer.py','srt_utils.py','subtitle_quality.py',
            'worker.py','m2_guardrail_runtime.py','m2_production_observation.py','m2_strict_runtime_evidence.py')}

    def evidence(self):
        evidence=super().evidence()
        evidence['root_cause'].update(incident_kind='source_srt_parse_classification',
            affected_stage='worker',failure_code='worker_unknown')
        return evidence

    def test_mismatched_result_event_digest_refused(self):
        self.members[0]['event_sha256']='0'*64
        self.assert_refused('result_event_unproven')

    def test_unrelated_value_error_not_accepted_as_parse_incident(self):
        self.members[0]['original_detail']='unexpected implementation exception'
        self.assert_refused('detail_not_proven')

    def test_result_gate_cannot_be_invented(self):
        self.members[0]['result_gate_id']='unproven-gate'
        self.assert_refused('result_event_unproven')

    def test_missing_safe_fallback_proof_refused(self):
        self.report['safe_fallback_or_review']=False
        self.assert_refused('regression_unproven')

    def test_missing_asr_guard_regression_refused(self):
        self.report['asr_cache_guard_preserved']=False
        self.assert_refused('regression_unproven')


if __name__=='__main__':unittest.main()
