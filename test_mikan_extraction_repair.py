"""Controlled repair tests use real SQLite/files; runtime/qB are explicit fixtures."""
from contextlib import ExitStack, closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import mikan_worker as mw
import mikan_extraction_repair as repair
from qbit_client import QBitTorrent, QBitTorrentFile
from scan_state import ScanStateStore
from test_mikan_worker import _logger, _mikan_process_config


class ReviewedExtractionRepairTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();downloads=self.root/'downloads';downloads.mkdir()
        self.config=_mikan_process_config(self.root,downloads)
        self.config.mikan_sqlite_authoritative_state=True
        self.config.export_ai_ass=True
        self.config.mikan_extract_failed_retry_seconds=0
        self.config.m2_server_canary_observer_enabled=True
        self.config.scanner_state_path=self.root/'scanner_state.sqlite3'
        self.config.log_path=self.root/'logs';self.config.log_path.mkdir()
        store=ScanStateStore(self.config.scanner_state_path);store.close()
        self.source=downloads/'Show - 01.mkv';self.source.write_bytes(b'unchanged complete fixture download')
        self.target=self.root/'anime'/'Show - S01E01.mkv';self.target.parent.mkdir()
        self.target.write_bytes(b'unchanged production target fixture')
        self.torrent=QBitTorrent(hash='a'*40,name='Show - 01 [CHT]',progress=1.0,state='stalledUP',dlspeed=0,
            downloaded=self.source.stat().st_size,added_on=None,content_path=str(self.source),save_path=str(downloads),
            category='llm-sub',tags='mikansub')
        self.snapshot={'bangumi_id':123,'episode':1,'info_hash':self.torrent.hash,'source':'mikan',
            'torrent_url':'https://fixture.invalid/a.torrent','title':self.torrent.name,'queued_at':'2026-01-01T00:00:00Z'}
        self.worker=mw.MikanWorker(self.config,_logger())
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.runtime=self.stack.enter_context(patch('mikan_extraction_repair.runtime_identity',return_value='b'*64))
        self.stack.enter_context(patch('mikan_worker._mikan_admission_allowed',return_value=True))
        mw._upsert_mikan_extract_jobs(self.config,[(self.torrent,[self.snapshot],1,False)],state_required=True)
        job=mw._claim_mikan_extract_jobs(self.config,limit=1)[0]
        mw._finish_mikan_extract_job(self.config,job.job_key,'failed',mw.MikanExtractResult(0,
            failure_reason='subtitle_validation_failed',failure_detail='hard_qc_failed'),worker_id=job.worker_id)
        self.entry={'bangumi_id':123,'episode':1,'last_failed_info_hash':self.torrent.hash,
            'last_failure_reason':'extract_failed','last_extract_failed_at':'2026-01-01T00:00:00Z',
            'last_extract_failure_reason':'subtitle_validation_failed',
            'failed_info_hashes':[self.torrent.hash,'c'*40],'failed_urls':['https://fixture.invalid/a.torrent'],
            'completion_revalidation':{'request_id':'d'*64,'source_identity':self.identity(self.target)}}
        mw._save_pending(self.worker.pending_path,{'items':{'123:1':self.entry}})
        self.raw={**mw._torrent_request_payload(self.torrent),'amount_left':0}
        response=Mock();response.json.return_value=[self.raw]
        self.qbit=Mock(base_url='http://fixture.invalid');self.qbit._get_with_retry.return_value=response
        self.qbit.list_files.return_value=[QBitTorrentFile(self.source.name,self.source.stat().st_size,1.0,1)]
        self.stack.enter_context(patch.object(mw.MikanWorker,'_qbit',return_value=self.qbit))
        self.stack.enter_context(patch.object(mw.MikanWorker,'_series_mappings',return_value=[{'bangumi_id':123}]))
        self.match=self.stack.enter_context(patch('mikan_worker._target_video_for_torrent_source',return_value=self.target))
        code=repair.processing_identity()
        proof={'phase':1,'fixture_new_publications':1,'production_publications':0,'source_unchanged':True,'target_unchanged':True,
            'output_sha256':'e'*64,'source_identity':{'source':self.identity(self.source),'target':self.identity(self.target)},
            'transformation':{'version':'parallel-chinese-ass-v1','normalized_sha256':'e'*64,
                'target_validation':{'output_parse':'PASS','hard_qc':'PASS','source_analysis':{'eligible':True}}}}
        evidence=self.config.log_path/'validation.json';evidence.write_text(json.dumps(proof),encoding='utf-8')
        attestation=self.config.log_path/'code.sha256'
        attestation.write_text(''.join(f'{sha}  /app/{name}\n' for name,sha in code.items()),encoding='utf-8')
        self.request={'contract':repair.CONTRACT,'request_id':'f'*64,'authorization_ref':'isolated-reviewed-repair',
            'torrent_hash':self.torrent.hash,'processing_identity':code,'runtime_fingerprint':'b'*64,
            'expected_job_sha256':repair.digest(self.state()[0]),'expected_pending_sha256':repair.digest(self.entry),
            'target_identity':self.identity(self.target),'download_identity':self.identity(self.source),
            'validation_evidence':self.descriptor(evidence),'code_attestation':self.descriptor(attestation)}

    @staticmethod
    def identity(path):
        stat=path.stat();return {'canonical_path':str(path.resolve()),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    @staticmethod
    def descriptor(path):return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    def state(self):
        with closing(mw._mikan_state_existing_connect(self.config)) as db:
            job=repair._read_row(db,'mikan_extract_jobs','job_key','hash:'+self.torrent.hash)
            item=repair._read_row(db,'mikan_download_items','key','123:1')
            events=db.execute('SELECT event_key,detail_json FROM mikan_download_events WHERE event=?',(repair.EVENT,)).fetchall()
            return job,json.loads(item['raw_json']),events

    def invoke(self,request=None):
        return mw.requeue_mikan_extract_job(self.config,job_key='hash:'+self.torrent.hash,
                                          reviewed_repair=self.request if request is None else request)

    def test_atomic_repair_preserves_receipt_budget_then_normal_claim_and_replay(self):
        old=self.state();self.assertEqual(old[0]['status'],'replaced')
        self.assertEqual(self.invoke()['queued'],1)
        new=self.state();self.assertEqual(new[0]['status'],'queued')
        self.assertEqual(new[0]['attempts'],old[0]['attempts'])
        self.assertEqual(new[0]['result_json'],old[0]['result_json'])
        self.assertEqual(new[1]['failed_info_hashes'],self.entry['failed_info_hashes'])
        self.assertEqual(new[1]['failed_urls'],self.entry['failed_urls'])
        self.assertEqual(new[1]['completion_revalidation'],self.entry['completion_revalidation'])
        self.assertEqual(json.loads(new[2][0][1])['previous_extract_job'],old[0])
        self.assertEqual(json.loads(json.loads(new[2][0][1])['previous_pending_item']['raw_json']),old[1])
        # The DB is reopened by the normal claim path, not by a second queue.
        jobs=mw._claim_mikan_extract_jobs(self.config,limit=1);self.assertEqual(len(jobs),1)
        self.assertEqual(self.state()[0]['attempts'],old[0]['attempts']+1)
        self.assertTrue(mw._renew_mikan_extract_job_lease(self.config,jobs[0]))
        self.assertEqual(mw._claim_mikan_extract_jobs(self.config,limit=1),[])
        before_replay=self.state();self.assertEqual(self.invoke()['queued'],0);self.assertEqual(self.state(),before_replay)
        self.qbit.add_url.assert_not_called()

    def test_normal_retry_does_not_reopen_replaced_without_review(self):
        before=self.state()
        self.assertFalse(mw.requeue_mikan_extract_job(self.config,job_key='hash:'+self.torrent.hash))
        self.assertEqual(self.state(),before)

    def test_pending_job_runtime_and_code_differences_refuse_without_mutation(self):
        for field in ('expected_job_sha256','expected_pending_sha256','runtime_fingerprint'):
            with self.subTest(field=field):
                request=copy.deepcopy(self.request);request[field]='0'*64;before=self.state()
                with self.assertRaises(mw.MikanWorkerError):self.invoke(request)
                self.assertEqual(self.state(),before)
        request=copy.deepcopy(self.request);request['processing_identity']['subtitle_extract.py']='0'*64
        with self.assertRaisesRegex(mw.MikanWorkerError,'repair_code_changed'):self.invoke(request)

    def test_foreign_partial_or_wrong_mapping_download_refused(self):
        before=self.state()
        for changes in ({'category':'other'},{'progress':0.5},{'amount_left':1},{'tags':'unrelated'}):
            with self.subTest(changes=changes):
                self.qbit._get_with_retry.return_value.json.return_value=[{**self.raw,**changes}]
                with self.assertRaises(mw.MikanWorkerError):self.invoke()
                self.assertEqual(self.state(),before)
        self.qbit._get_with_retry.return_value.json.return_value=[self.raw]
        self.match.return_value=self.root/'other.mkv'
        with self.assertRaisesRegex(mw.MikanWorkerError,'target_match_unproven'):self.invoke()
        self.assertEqual(self.state(),before)

    def test_source_change_and_persistent_hold_refused(self):
        from m2_production_recovery import install_source_hold,RecoveryError
        before=self.state();self.source.write_bytes(b'changed fixture')
        with self.assertRaisesRegex(mw.MikanWorkerError,'source_changed'):self.invoke()
        self.assertEqual(self.state(),before)
        with closing(sqlite3.connect(self.config.scanner_state_path)) as db:
            with db:
                install_source_hold(db,self.target,reconciliation_id='isolated-hold',now=time.time(),
                    evidence={'old_identity':{'continuity':'UNKNOWN'},'current_identity':self.identity(self.target)})
        with self.assertRaisesRegex(RecoveryError,'source_continuity_unverified'):self.invoke()
        self.assertEqual(self.state(),before)

    def test_transaction_interruption_rolls_back_event_and_both_rows(self):
        original=self.state();database=self.root/'mikan_state.sqlite3'
        class Interrupted(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                if sql.startswith("UPDATE mikan_extract_jobs SET status='queued'"):
                    raise KeyboardInterrupt('isolated transaction interruption')
                return super().execute(sql,*args,**kwargs)
        with patch('mikan_worker._mikan_state_existing_connect',side_effect=lambda _c:sqlite3.connect(database,factory=Interrupted)):
            with self.assertRaises(KeyboardInterrupt):self.invoke()
        self.assertEqual(self.state(),original)
        self.assertEqual(self.invoke()['queued'],1)

    def test_runtime_change_after_enqueue_defers_claim_without_consuming_attempt(self):
        self.invoke();before=self.state();self.runtime.return_value='0'*64
        self.assertEqual(mw._claim_mikan_extract_jobs(self.config,limit=1),[])
        after=self.state();self.assertEqual(after[0]['attempts'],before[0]['attempts'])
        self.assertGreater(after[0]['lease_until'],time.time())
        self.assertIn('runtime_or_receipt_changed',after[0]['last_error'])
        self.assertEqual(after[2],before[2])

    def test_repaired_failure_cannot_receive_second_budget_and_receipt_survives_sync(self):
        self.invoke();job=mw._claim_mikan_extract_jobs(self.config,limit=1)[0]
        mw._finish_mikan_extract_job(self.config,job.job_key,'failed',mw.MikanExtractResult(0,
            failure_reason='subtitle_validation_failed',failure_detail='still not acceptable'),worker_id=job.worker_id)
        frozen=self.state()[2]
        mw._save_pending(self.worker.pending_path,mw._load_pending(self.worker.pending_path))
        with closing(mw._mikan_state_connect(self.config)) as db:pass
        self.assertEqual(self.state()[2],frozen)
        changed=copy.deepcopy(self.request);changed['request_id']='1'*64
        with self.assertRaisesRegex(mw.MikanWorkerError,'retry_budget_spent'):self.invoke(changed)
        self.assertEqual(self.state()[0]['status'],'replaced')

    def test_evidence_backoff_valid_output_and_lock_refuse_without_mutation(self):
        before=self.state()
        changed=copy.deepcopy(self.request);changed['validation_evidence']['sha256']='0'*64
        with self.assertRaisesRegex(mw.MikanWorkerError,'evidence_changed'):self.invoke(changed)
        self.config.mikan_extract_failed_retry_seconds=900
        with self.assertRaisesRegex(mw.MikanWorkerError,'normal_backoff_active'):self.invoke()
        self.config.mikan_extract_failed_retry_seconds=0
        with patch('subtitle_extract.verified_official_subtitle_languages',return_value={'zh-tw'}):
            with self.assertRaisesRegex(mw.MikanWorkerError,'valid_output_exists'):self.invoke()
        lock=mw.VideoLock(self.target);self.assertTrue(lock.acquire())
        try:
            with self.assertRaisesRegex(mw.MikanWorkerError,'target_busy'):self.invoke()
        finally:lock.release()
        self.assertEqual(self.state(),before)

    def test_changed_proof_or_file_cannot_pass_publication_guard(self):
        self.invoke();binding=self.state()[1][repair.EVENT]
        evidence=repair.guard_reviewed_extraction(self.config,binding)
        self.assertEqual(evidence['download_identity'],self.request['download_identity'])
        self.source.write_bytes(b'changed after claim fixture')
        with self.assertRaisesRegex(mw.MikanWorkerError,'source_changed'):
            repair.guard_reviewed_extraction(self.config,binding)

    def test_held_repair_does_not_starve_unrelated_queued_job(self):
        self.invoke();self.runtime.return_value='0'*64
        unrelated=copy.copy(self.torrent)
        from dataclasses import replace
        unrelated=replace(unrelated,hash='9'*40,name='Other - 02 [CHT]')
        entry={'bangumi_id':999,'episode':2,'info_hash':unrelated.hash,'source':'mikan',
               'torrent_url':'https://fixture.invalid/other.torrent','queued_at':'2026-01-01T00:00:00Z'}
        mw._upsert_mikan_extract_jobs(self.config,[(unrelated,[entry],1,False)],state_required=True)
        self.assertEqual(mw._claim_mikan_extract_jobs(self.config,limit=1),[])
        jobs=mw._claim_mikan_extract_jobs(self.config,limit=1)
        self.assertEqual([j.torrent.hash for j in jobs],[unrelated.hash])

    def test_immutable_repair_receipt_is_not_pruned_with_ui_events(self):
        self.invoke();original=self.state()[2]
        with closing(mw._mikan_state_existing_connect(self.config)) as db:
            db.executemany("INSERT INTO mikan_download_events(key,event,detail,detail_json,created_at) VALUES('fixture','status','transient','{}',?)",
                           [(time.time()+i,) for i in range(5001)])
            db.commit()
        mw._save_pending(self.worker.pending_path,mw._load_pending(self.worker.pending_path))
        self.assertEqual(self.state()[2],original)

    def test_existing_control_command_supports_reviewed_repair_and_idempotent_replay(self):
        from main import _execute_control_command
        params={'job_key':'hash:'+self.torrent.hash,'reviewed_repair':self.request}
        first=_execute_control_command(self.config,_logger(),'mikan.requeue_extract','',params)
        second=_execute_control_command(self.config,_logger(),'mikan.requeue_extract','',params)
        self.assertEqual(first['queued'],1);self.assertEqual(second['queued'],0)
        self.assertEqual(second['status'],'already_recorded')

    def test_restored_pending_member_accepts_only_its_exact_completion_result(self):
        self.invoke();pending=mw._load_pending(self.worker.pending_path)
        self.assertTrue(mw._pending_has_active_release(pending['items']['123:1']))
        changed,replacements=mw._apply_completed_extract_result(pending,self.torrent,[],mw.MikanExtractResult(1))
        self.assertTrue(changed);self.assertEqual(replacements,[])
        self.assertTrue(mw._pending_is_terminal_success(pending['items']['123:1']))
        self.assertEqual(len(self.state()[2]),1)

    def test_publication_manifest_is_bound_to_actual_durable_repair_receipt(self):
        from subtitle_extract import _publish_official_subtitle_set
        from subtitle_language_projection import normalize_parallel_chinese_ass
        from test_subtitle_language_projection import configuration,bilingual_ass
        for key,value in vars(configuration()).items():setattr(self.config,key,value)
        self.invoke();entry=self.state()[1]
        options=mw._reviewed_extraction_publication_options(self.config,[entry],self.source,self.target)
        content,_=normalize_parallel_chinese_ass(bilingual_ass(),language='zh-tw',config=self.config)
        stage=self.root/'validated.ass';stage.write_bytes(content.encode())
        output=self.target.with_suffix('.zh-TW.ass')
        _publish_official_subtitle_set(self.target,[(stage,output,'zh-tw')],self.config,**options)
        manifest=json.loads(next(self.root.glob('official_subtitle_versions/*/*/manifest.json')).read_text(encoding='utf-8'))
        evidence=manifest['reviewed_recovery']
        self.assertEqual(evidence['event_key'],entry[repair.EVENT]['event_key'])
        self.assertEqual(evidence['target_identity'],self.identity(self.target))
        self.assertEqual(evidence['download_identity'],self.identity(self.source))
        self.assertEqual(manifest['status'],'completed')


if __name__=='__main__':unittest.main()
