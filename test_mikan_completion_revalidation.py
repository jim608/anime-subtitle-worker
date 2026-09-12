"""Pure archival transition tests; these do not prove a guarded Production entry."""
import copy
import hashlib
import json
import unittest
import tempfile
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from contextlib import ExitStack
from unittest.mock import Mock, patch
import mikan_worker as mw
from lock import VideoLock
from mikan_worker import MikanWorkerError,_pending_is_terminal_success,_revalidated_completed_entry


class CompletionRevalidationTest(unittest.TestCase):
    def setUp(self):
        self.entry={'bangumi_id':2565,'episode':12,'completed_at':'2026-08-27T10:23:15+00:00',
                    'last_extracted_count':1,'total_extracted_count':12,
                    'failed_urls':['retained-failure'],'failed_info_hashes':['b'*40],
                    'download_recovery':{'decision':'KEEP_RECORDED_COMPLETE_NOT_REVERIFIED'}}
        self.source={'canonical_path':'/fixture/media.mkv','size':100,'mtime_ns':1000,'sha256':'c'*64}
        self.revision=hashlib.sha256(json.dumps(self.entry,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def apply(self,entry=None,**kwargs):
        return _revalidated_completed_entry(self.entry if entry is None else entry,
            expected_entry_sha256=kwargs.get('revision',self.revision),
            request_id=kwargs.get('request_id','a'*64),source_identity=kwargs.get('source',self.source))

    def test_archive_retains_full_history_and_failure_budget(self):
        before=copy.deepcopy(self.entry);result=self.apply()
        self.assertEqual(self.entry,before)
        self.assertTrue(_pending_is_terminal_success(before))
        self.assertFalse(_pending_is_terminal_success(result))
        self.assertEqual(result['completion_revalidation']['prior_entry'],before)
        self.assertEqual(result['failed_urls'],before['failed_urls'])
        self.assertEqual(result['failed_info_hashes'],before['failed_info_hashes'])
        self.assertEqual(result['completion_revalidation']['historical_continuity'],'UNKNOWN')

    def test_serialized_restart_replay_preserves_later_progress(self):
        saved=json.loads(json.dumps(self.apply()));saved['downloaded']=321
        replay=self.apply(saved)
        self.assertEqual(replay,saved)
        with self.assertRaisesRegex(MikanWorkerError,'already_recorded'):
            self.apply(saved,request_id='d'*64)

    def test_stale_entry_refused_without_mutation(self):
        changed=copy.deepcopy(self.entry);changed['total_extracted_count']=13
        with self.assertRaisesRegex(MikanWorkerError,'entry_changed'):self.apply(changed)
        self.assertEqual(changed['total_extracted_count'],13)

    def test_active_or_unreviewed_history_refused(self):
        for update in ({'torrent_url':'https://fixture.invalid/a.torrent','queued_at':'2026-09-08T00:00:00Z'}, {'download_recovery':{}}):
            entry={**self.entry,**update}
            digest=hashlib.sha256(json.dumps(entry,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            with self.assertRaises(MikanWorkerError):self.apply(entry,revision=digest)

    def test_missing_source_fingerprint_refused(self):
        source={**self.source,'sha256':''}
        with self.assertRaisesRegex(MikanWorkerError,'source_identity_invalid'):self.apply(source=source)


class GuardedCompletionRevalidationTest(unittest.TestCase):
    """Real source locks/pending persistence; external inspection boundaries stubbed."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'fixture.mkv'
        self.video.write_bytes(b'read-only source fixture')
        self.source = {'canonical_path': str(self.video.resolve()),
                       'size': self.video.stat().st_size, 'mtime_ns': self.video.stat().st_mtime_ns,
                       'sha256': hashlib.sha256(self.video.read_bytes()).hexdigest()}
        self.entry = {'bangumi_id': 2565, 'episode': 12,
                      'failed_urls': [],
                      'completed_at': '2026-08-27T10:23:15+00:00', 'last_extracted_count': 1,
                      'last_completed_info_hash': 'b'*40,
                      'download_recovery': {'decision': 'KEEP_RECORDED_COMPLETE_NOT_REVERIFIED'}}
        self.revision = hashlib.sha256(json.dumps(self.entry, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.worker = mw.MikanWorker.__new__(mw.MikanWorker)
        self.worker.config = SimpleNamespace(qbit_path_mappings=[], mikan_enabled=True)
        self.worker.pending_path = self.root / 'mikan_pending.json'
        mw._save_pending(self.worker.pending_path, {'items': {'2565:12': self.entry}})
        self.worker._series_mappings = Mock(return_value=[{'bangumi_id': 2565}])
        def acquire(name, **kwargs):
            lock = VideoLock(self.root / name)
            return lock if lock.acquire() else None
        self.worker._acquire_queue_lock = lambda *a, **kw: acquire('queue')
        self.worker._acquire_state_lock_for_enqueue = lambda *a, **kw: acquire('state')
        self.response = Mock()
        self.response.json.return_value = []
        self.worker._qbit = Mock(return_value=SimpleNamespace(base_url='http://fixture.invalid',
                    _get_with_retry=Mock(return_value=self.response)))
        self.db = self.root / 'scan.db'
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE ai_candidate_queue(path TEXT, status TEXT)')
            db.execute('CREATE TABLE ai_job_state(path TEXT, status TEXT)')
        db.close()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def patched(name, **kwargs):
            return self.stack.enter_context(patch(name, **kwargs))
        self.targets = patched('mikan_worker._target_videos_from_episode_index', return_value=[self.video])
        self.hold = patched('m2_production_recovery.require_source_not_held')
        patched('scan_state.scan_state_path', return_value=self.db)
        self.output = patched('subtitle_paths.has_finished_subtitle', return_value=False)
        patched('subtitle_paths.has_ai_finished_subtitle', return_value=False)
        patched('mikan_worker._target_has_required_chinese_subtitles', return_value=False)
        self.jobs = patched('mikan_worker._target_review_extract_jobs', return_value=({'b'*40: [
            {'status': 'success', 'torrent': {'content_path': str(self.root / 'absent.mkv')}}]}, True))
        patched('mikan_worker.map_remote_path', side_effect=lambda path, mappings: Path(path) if path else None)

    def invoke(self):
        return self.worker.revalidate_recorded_completion(mw.MikanReplacementTarget(2565, 12),
            expected_entry_sha256=self.revision, request_id='a'*64,
            source_identity=self.source, authorization_ref='fixture-reviewed-current-identity')

    def current(self):
        return mw._pending_entry(2565, 12, mw._load_pending(self.worker.pending_path))

    def test_persistent_replay_and_existing_discovery(self):
        self.assertEqual(self.invoke()['status'], 'recorded')
        saved = self.current()
        self.assertEqual(saved['completion_revalidation']['prior_entry'], self.entry)
        self.assertEqual(mw._known_retry_episodes_for_bangumi(mw._load_pending(self.worker.pending_path), 2565), {12})
        self.assertEqual(self.invoke()['status'], 'already_recorded')
        self.assertEqual(self.current(), saved)
        self.worker._qbit.assert_called_once()
        self.assertEqual(hashlib.sha256(self.video.read_bytes()).hexdigest(), self.source['sha256'])
        self.assertFalse(list(self.root.glob('*.lock')))

    def test_refusal_preserves_history(self):
        reasons = {'output': 'valid_output_exists', 'ambiguous': 'target_not_unique',
                   'held': 'held', 'torrent': 'existing_download_or_invalid_response',
                   'reuse': 'reuse_download_first', 'extract_running': 'extract_not_terminal',
                   'ai_running': 'ai_owner_active', 'checksum': 'source_changed'}
        for condition in ('output', 'ambiguous', 'held', 'torrent', 'reuse', 'extract_running', 'ai_running', 'checksum'):
            with self.subTest(condition=condition):
                self.output.return_value = condition == 'output'
                self.targets.return_value = [] if condition == 'ambiguous' else [self.video]
                self.hold.side_effect = RuntimeError('held') if condition == 'held' else None
                self.response.json.return_value = [{}] if condition == 'torrent' else []
                self.jobs.return_value = ({'b'*40: [{'status': 'running' if condition == 'extract_running' else 'success',
                    'torrent': {'content_path': str(self.video if condition == 'reuse' else self.root / 'absent.mkv')}}]}, True)
                with sqlite3.connect(self.db) as db:
                    db.execute('DELETE FROM ai_candidate_queue')
                    if condition == 'ai_running':
                        db.execute('INSERT INTO ai_candidate_queue VALUES (?,?)', (str(self.video), 'running'))
                db.close()
                if condition == 'checksum': self.source['sha256'] = 'f'*64
                with self.assertRaisesRegex(RuntimeError, reasons[condition]): self.invoke()
                self.assertEqual(self.current(), self.entry)
                self.assertFalse(list(self.root.glob('*.lock')))

    def test_failed_atomic_save_can_retry_without_losing_old_snapshot(self):
        with patch('mikan_worker._save_pending', side_effect=OSError('simulated interrupted commit')):
            with self.assertRaises(OSError): self.invoke()
        self.assertEqual(self.current(), self.entry)
        self.assertEqual(self.invoke()['status'], 'recorded')
        self.assertEqual(self.current()['completion_revalidation']['prior_entry'], self.entry)

    def test_restart_after_committed_write_rediscovers_without_new_request(self):
        save = mw._save_pending
        def commit_then_interrupt(*args):
            save(*args)
            raise OSError('simulated exit after commit')
        with patch('mikan_worker._save_pending', side_effect=commit_then_interrupt):
            with self.assertRaises(OSError): self.invoke()
        for _ in range(2):
            result = subprocess.run([sys.executable, '-c',
                'import json,sys; from pathlib import Path; '
                'from mikan_worker import _load_pending,_known_retry_episodes_for_bangumi; '
                'print(json.dumps(sorted(_known_retry_episodes_for_bangumi(_load_pending(Path(sys.argv[1])),2565))))',
                str(self.worker.pending_path)], capture_output=True, text=True, timeout=30, check=True)
            self.assertEqual(json.loads(result.stdout), [12])
        saved = self.current()
        self.assertEqual(self.invoke()['status'], 'already_recorded')
        self.assertEqual(self.current(), saved)

    def test_reopened_history_keeps_backoff_and_terminal_guards(self):
        self.invoke()
        pending = mw._load_pending(self.worker.pending_path)
        entry = mw._pending_entry(2565, 12, pending)
        entry['no_candidate_until'] = time.time() + 3600
        self.assertEqual(mw._known_retry_episodes_for_bangumi(pending, 2565), set())
        entry.pop('no_candidate_until')
        entry.update(completed_at='2026-09-08T00:00:00Z', last_extracted_count=1)
        self.assertEqual(mw._known_retry_episodes_for_bangumi(pending, 2565), set())

    def test_sqlite_authoritative_state_keeps_atomic_archive(self):
        with patch('mikan_worker._sqlite_authoritative_pending_enabled', return_value=True):
            self.assertEqual(self.invoke()['status'], 'recorded')
            saved = self.current()
            self.assertEqual(saved['completion_revalidation']['prior_entry'], self.entry)
            self.assertEqual(self.invoke()['status'], 'already_recorded')
            self.assertEqual(self.current(), saved)

    def test_failed_source_identity_is_not_ignored(self):
        self.entry['failed_info_hashes'] = ['d'*40]
        self.revision = hashlib.sha256(json.dumps(self.entry, ensure_ascii=False,
                        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        mw._save_pending(self.worker.pending_path, {'items': {'2565:12': self.entry}})
        with self.assertRaisesRegex(mw.MikanWorkerError, 'extract_evidence_unavailable'):
            self.invoke()
        self.assertEqual(self.jobs.call_args.args[1], {'b'*40, 'd'*40})
        self.assertEqual(self.current(), self.entry)

    def test_dispatch_rechecks_authorized_source_revision(self):
        self.invoke()
        self.assertTrue(self.worker._reconcile_verified_history_outputs(2565, [12],
                        operation='fixture', state_required=False))
        self.video.write_bytes(b'changed fixture source')
        self.assertFalse(self.worker._reconcile_verified_history_outputs(2565, [12],
                         operation='fixture', state_required=False))
        saved = self.current()
        self.assertEqual('revalidation_source_revision_changed', saved['completion_revalidation']['blocked_reason'])
        self.assertEqual(saved['completion_revalidation']['prior_entry'], self.entry)

    def test_dispatch_rechecks_new_source_hold(self):
        from m2_production_recovery import RecoveryError
        self.invoke()
        self.hold.side_effect = RecoveryError('source_continuity_unverified')
        self.assertFalse(self.worker._reconcile_verified_history_outputs(2565, [12],
                         operation='fixture', state_required=False))
        self.assertEqual('source_continuity_unverified', self.current()['completion_revalidation']['blocked_reason'])


if __name__=='__main__':unittest.main()
