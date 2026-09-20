"""An exhausted deterministic display repair is a task review, not a system streak."""
import logging,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import main
from worker import VideoWorker
from translator import SubtitleTranslator,TranslationError,TranslationTimeoutError
from srt_utils import SrtBlock,write_srt
from scan_state import ScanStateStore
from m2_production_observation import admit_new_job
from m2_guardrail_runtime import runtime_guardrail_status
import test_m2_production_observation as observer_fixtures
from test_worker import _config

DETAIL='Targeted subtitle readability repair exceeded its hard display limit at index 98: allowed=23'

class ReadabilityTerminalTests(unittest.TestCase):
    def test_actual_bounded_repair_exhaustion_is_review_in_every_source_language(self):
        for language in ('ja','zh','en'):
            with self.subTest(language=language),tempfile.TemporaryDirectory() as d:
                root=Path(d);source=root/'source.srt';output=root/'previous.srt'
                block=SrtBlock(98,'00:00:01,000 --> 00:00:02,000',['trusted source'])
                write_srt(source,[block]);output.write_bytes(b'existing output must not be overwritten')
                before={p:p.read_bytes() for p in (source,output)}
                translator=SubtitleTranslator.__new__(SubtitleTranslator)
                translator.config=SimpleNamespace(translation_glossary={})
                translator.logger=logging.getLogger('readability-test');translator._progress_callback=None
                with patch.object(translator,'_translate_batch',return_value=[SrtBlock(98,block.timing,['中'*24])]) as calls:
                    with self.assertRaises(TranslationError) as caught:
                        translator.retranslate_problem_blocks([block],source,output,series_glossary={},source_language=language,max_display_chars_by_index={98:23})
                self.assertEqual(calls.call_count,2)
                self.assertEqual(str(caught.exception),DETAIL)
                stage=VideoWorker._stage_for_exception(caught.exception)
                self.assertEqual(main._ai_failure_policy(stage,str(caught.exception)),('subtitle_quality_review','manual_review'))
                self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_unrelated_faults_and_unproven_messages_keep_existing_safety(self):
        cases=[('translation','model timed out',('transient_timeout','same_pipeline')),
            ('translation','connection refused',('transient_connection','same_pipeline')),
            ('translation','CUDA out of memory',('transient_oom','lower_memory_same_pipeline')),
            ('translation','unexpected translator implementation failure',('translation_unknown','bounded_retry')),
            ('worker',DETAIL,('worker_unknown','bounded_retry')),
            ('translation',DETAIL+' source identity changed during processing',('source_mutation','manual_review')),
            ('translation',DETAIL+' extra unexplained state',('translation_unknown','bounded_retry'))]
        for stage,detail,expected in cases:
            with self.subTest(stage=stage,detail=detail):self.assertEqual(main._ai_failure_policy(stage,detail),expected)

    def test_three_distinct_exhausted_repairs_do_not_trip_and_next_claim_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture=observer_fixtures.M2ProductionObservationTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.addCleanup(fixture.tearDown)
            fixture.root=root
            values=vars(_config(root));values.pop('work_path')
            c=fixture._config('cycle',**values,control_state_path='control.sqlite3',auto_ai_max_attempts=3)
            state=ScanStateStore.from_config(c)
            self.addCleanup(state.close)
            before={};finished=[]
            for i in range(3):
                v=Path(c.input_path)/f'quality-{i}.mkv';v.write_bytes(b'isolated immutable source')
                before[v]=v.read_bytes();state.upsert_ai_queue_candidate(v,v.stat().st_mtime_ns);state.commit()
                aid=main._mark_queue_running(state,v,c)
                state.update_ai_job_stage(v,'translation','failed',DETAIL);state.commit()
                main._mark_queue_result(state,v,False,c,delivery_attempt_id=aid,observe_terminal=True)
                attempt=state.get_ai_delivery_attempt(aid);finished.append(attempt)
                self.assertEqual(attempt['status'],'review_required')
                self.assertEqual(attempt['error_code'],'subtitle_quality_review')
                self.assertTrue(admit_new_job(c))
                self.assertEqual(runtime_guardrail_status(c)['status'],'ARMED')
            state.close();state=ScanStateStore.from_config(c);self.addCleanup(state.close)
            for item in finished:self.assertEqual(state.get_ai_delivery_attempt(item['attempt_id']),item)
            v=Path(c.input_path)/'next.mkv';v.write_bytes(b'next safe source')
            state.upsert_ai_queue_candidate(v,v.stat().st_mtime_ns);state.commit()
            aid=main._mark_queue_running(state,v,c)
            self.assertGreater(state.get_ai_delivery_attempt(aid)['started_at'],finished[-1]['finished_at'])
            state.update_ai_job_stage(v,'preflight','running','next stage after safe quality review');state.commit()
            job=state.pipeline_jobs().job_for_path(v,size=v.stat().st_size,mtime_ns=v.stat().st_mtime_ns,create=False)
            self.assertEqual(state.pipeline_jobs().list_stage_attempts(job['job_id'])[-1]['status'],'RUNNING')
            self.assertEqual(before,{p:p.read_bytes() for p in before})
            self.assertTrue(admit_new_job(c))

if __name__=='__main__':unittest.main()
