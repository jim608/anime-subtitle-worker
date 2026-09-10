"""Regression for an old missing mount blocking newly available download data."""
import json
import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
from mikan_worker import _upsert_mikan_extract_jobs, _claim_mikan_extract_jobs, _finish_mikan_extract_job, MikanExtractResult, QBitTorrent
from test_mikan_worker import _mikan_process_config


class MappedSourceRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.downloads=self.root/'downloads';self.downloads.mkdir()
        self.c=_mikan_process_config(self.root,self.downloads)
        self.c.mikan_extract_failed_retry_seconds=0
        self.c.export_ai_ass=True
        self.c.finished_subtitle_suffixes=[]
        self.source=self.root/'anime'/'Fixture - 01.mkv';self.source.parent.mkdir()
        self.source.write_bytes(b'unchanged production-target fixture')
        self.path=self.downloads/'Fixture - 01.mkv'
        self.torrent=QBitTorrent(hash='b'*40,name='Fixture - 01 [ASSx2]',progress=1.0,
            state='uploading',dlspeed=0,downloaded=100,added_on=None,
            content_path=str(self.path),save_path=str(self.downloads),category='llm-sub',tags='mikansub')
        self.entry={'bangumi_id':123,'episode':1,'torrent_url':'https://fixture.invalid/subtitle.torrent',
            'completion_revalidation':{'request_id':'a'*64,'recorded_at':'2026-09-10T00:00:00Z',
                'authorization_ref':'fixture-reviewed-history',
                'source_identity':{'canonical_path':str(self.source.resolve()),'size':self.source.stat().st_size,
                    'mtime_ns':self.source.stat().st_mtime_ns,'sha256':hashlib.sha256(self.source.read_bytes()).hexdigest()}}}
        self.rows=[(self.torrent,[self.entry],1,True)]
        self.assertEqual(self.upsert(),1)
        self.job=_claim_mikan_extract_jobs(self.c,limit=1)[0]
        self.finish_missing()

    def upsert(self):return _upsert_mikan_extract_jobs(self.c,self.rows,state_required=True)

    @contextmanager
    def database(self):
        with closing(sqlite3.connect(self.root/'mikan_state.sqlite3')) as db:
            with db:
                yield db

    def finish_missing(self):
        _finish_mikan_extract_job(self.c,self.job.job_key,'failed',MikanExtractResult(0,
            failure_reason='source_video_missing',failure_detail='mapped file absent',retryable=False))

    def available(self):self.path.write_bytes(b'newly available isolated source fixture')

    def state(self):
        with self.database() as db:
            row=db.execute('SELECT status,attempts,result_json FROM mikan_extract_jobs WHERE job_key=?',(self.job.job_key,)).fetchone()
            events=db.execute("SELECT detail_json FROM mikan_download_events WHERE event='mapped_source_available_recovery'").fetchall()
        return row,events

    def test_available_previously_missing_source_can_resume_without_erasing_failure(self):
        self.assertEqual(self.upsert(),0)
        original=self.state()[0];self.available();before=self.path.read_bytes()
        self.assertEqual(self.upsert(),1)
        row,events=self.state()
        self.assertEqual(row[1],original[1],'Recovery must not reset attempt budget')
        self.assertEqual(row[2],original[2],'Result remains until the normal extraction lifecycle runs')
        self.assertEqual(len(events),1)
        self.assertEqual(json.loads(events[0][0])['previous_extract_job']['result_json'],original[2])
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.entry['completion_revalidation']['source_identity']['sha256'])
        self.assertEqual(self.upsert(),0)
        self.assertEqual(len(_claim_mikan_extract_jobs(self.c,limit=1)),1)
        self.assertEqual(_claim_mikan_extract_jobs(self.c,limit=1),[])

    def test_missing_review_refuses(self):
        self.available();self.entry.pop('completion_revalidation')
        self.assertEqual(self.upsert(),0);self.assertEqual(self.state()[1],[])

    def test_changed_target_checksum_refuses_even_with_same_stat(self):
        self.available();stat=self.source.stat()
        self.source.write_bytes(b'x'*stat.st_size);os.utime(self.source,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.assertEqual(self.upsert(),0);self.assertEqual(self.state()[1],[])

    def test_held_source_refuses(self):
        from m2_production_recovery import RecoveryError
        self.available()
        with patch('m2_production_recovery.require_source_not_held',side_effect=RecoveryError('source_continuity_unverified')):
            self.assertEqual(self.upsert(),0)
        self.assertEqual(self.state()[1],[])

    def test_valid_existing_output_refuses(self):
        self.available()
        with patch('subtitle_paths.has_finished_subtitle',return_value=True):self.assertEqual(self.upsert(),0)
        self.assertEqual(self.state()[1],[])

    def test_preserves_backoff(self):
        self.available();self.c.mikan_extract_failed_retry_seconds=900
        self.assertEqual(self.upsert(),0)

    def test_quality_or_no_subtitle_terminal_never_reopens(self):
        self.available()
        for reason in ('subtitle_language_not_supported','no_subtitle_streams','hard_qc_failed'):
            with self.subTest(reason=reason):
                with self.database() as db:
                    db.execute('UPDATE mikan_extract_jobs SET result_json=? WHERE job_key=?',(json.dumps({'failure_reason':reason,'extracted_count':0}),self.job.job_key))
                self.assertEqual(self.upsert(),0)
        self.assertEqual(self.state()[1],[])

    def test_effective_lease_is_preserved(self):
        self.available()
        with self.database() as db:
            db.execute('UPDATE mikan_extract_jobs SET lease_until=? WHERE job_key=?',(time.time()+300,self.job.job_key))
        self.assertEqual(self.upsert(),0)

    def test_retry_budget_survives_reopen_and_second_missing_path(self):
        self.available();self.assertEqual(self.upsert(),1)
        self.assertEqual(len(_claim_mikan_extract_jobs(self.c,limit=1)),1)
        self.finish_missing()
        self.assertEqual(self.upsert(),0)
        self.assertEqual(len(self.state()[1]),1)

    def test_transaction_interruption_rolls_back_archive_and_queue(self):
        self.available();before=self.state()
        with self.database() as db:
            db.execute("CREATE TRIGGER stop_recovery BEFORE UPDATE OF status ON mikan_extract_jobs WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT,'fixture interruption'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.upsert()
        self.assertEqual(self.state(),before)
        with self.database() as db:db.execute('DROP TRIGGER stop_recovery')
        self.assertEqual(self.upsert(),1)
        self.assertEqual(len(self.state()[1]),1)

    def test_event_retention_preserves_recovery_evidence_and_budget(self):
        from mikan_worker import _sync_mikan_state_db
        self.available();self.assertEqual(self.upsert(),1)
        self.assertEqual(len(_claim_mikan_extract_jobs(self.c,limit=1)),1)
        self.finish_missing()
        before=self.state()[1]
        with self.database() as db:
            db.executemany("INSERT INTO mikan_download_events(key,event,created_at) VALUES (?,?,?)",
                [('ordinary','status_changed',time.time()+i) for i in range(5010)])
        _sync_mikan_state_db(self.root/'mikan_pending.json',{})
        self.assertEqual(self.state()[1],before)
        self.assertEqual(self.upsert(),0)

    def test_legacy_compaction_preserves_recovery_key_and_evidence(self):
        from mikan_worker import _compact_legacy_mikan_events
        self.available();self.assertEqual(self.upsert(),1)
        self.assertEqual(len(_claim_mikan_extract_jobs(self.c,limit=1)),1)
        self.finish_missing()
        with self.database() as db:
            before=db.execute("SELECT * FROM mikan_download_events WHERE event='mapped_source_available_recovery'").fetchall()
            db.execute("DELETE FROM mikan_state_meta WHERE key='download_events_compacted_v2'")
            _compact_legacy_mikan_events(db)
            after=db.execute("SELECT * FROM mikan_download_events WHERE event='mapped_source_available_recovery'").fetchall()
        self.assertEqual(after,before)
        self.assertEqual(self.upsert(),0)

    def test_concurrent_duplicate_ingress_records_one_recovery(self):
        self.available()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.upsert(),range(2)))
        self.assertEqual(sum(results),1)
        self.assertEqual(len(self.state()[1]),1)

    def test_incomplete_or_nonproject_torrent_refuses(self):
        self.available()
        for torrent in (replace(self.torrent,progress=.5,state='downloading'),
                        replace(self.torrent,category='unrelated'),replace(self.torrent,tags='other')):
            with self.subTest(torrent=torrent):
                self.rows=[(torrent,[self.entry],1,True)]
                self.assertEqual(self.upsert(),0)
        self.assertEqual(self.state()[1],[])

    def test_empty_or_escaped_download_refuses(self):
        self.path.write_bytes(b'');self.assertEqual(self.upsert(),0)
        outside=self.root/'outside.mkv';outside.write_bytes(b'not project download')
        self.rows=[(replace(self.torrent,content_path=str(outside)),[self.entry],1,True)]
        self.assertEqual(self.upsert(),0)
        self.assertEqual(self.state()[1],[])

    def test_altered_download_during_hash_refuses(self):
        import mikan_worker as mw
        self.available();original=mw.sha256_file
        def changing_hash(path):
            digest=original(path)
            if Path(path)==self.path:
                self.path.write_bytes(b'changed during hash')
            return digest
        with patch('mikan_worker.sha256_file',side_effect=changing_hash):self.assertEqual(self.upsert(),0)
        self.assertEqual(self.state()[1],[])

    def test_source_hold_after_review_is_not_bypassed(self):
        from scan_state import scan_state_path
        db_path=scan_state_path(self.c)
        with closing(sqlite3.connect(db_path)) as db:
            db.execute('CREATE TABLE m2_recovery_source_holds(canonical_path TEXT PRIMARY KEY,reconciliation_id TEXT,reason_code TEXT,continuity TEXT,evidence_sha256 TEXT)')
            db.execute("INSERT INTO m2_recovery_source_holds VALUES (?,'fixture','source_unverified','UNKNOWN','fixture')",(str(self.source.resolve()),))
            db.commit()
        self.available()
        self.assertEqual(self.upsert(),0)
        self.assertEqual(self.state()[1],[])


if __name__=='__main__':unittest.main()
