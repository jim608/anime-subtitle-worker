"""Controlled reopening of an incorrect completion reuses its exact downloaded bytes."""
import hashlib
import json
import logging
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import mikan_worker as mw
from qbit_client import QBitTorrent, QBitTorrentFile
import test_mikan_completion_revalidation as revalidation_fixture


class RetainedCompletionRevalidationTest(unittest.TestCase):
    def setUp(self):
        self.f = revalidation_fixture.GuardedCompletionRevalidationTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        f.entry.pop('download_recovery')
        f.revision = hashlib.sha256(json.dumps(f.entry, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        mw._save_pending(f.worker.pending_path, {'items': {'2565:12': f.entry}})
        c = f.worker.config
        c.work_path = f.root
        c.mikan_pending_path = str(f.worker.pending_path)
        c.qbit_category = 'llm-sub'
        c.qbit_tags = ['mikansub']
        c.video_extensions = ['.mkv']
        f.worker.logger = logging.getLogger('fixture.retained-download')
        self.download = f.root/'download'/'Show - 12.mkv'
        self.download.parent.mkdir()
        self.download.write_bytes(b'complete downloaded fixture media')
        self.torrent = QBitTorrent(hash='b'*40, name='[Group] Show - 12 [CHT]', progress=1,
            state='uploading', dlspeed=0, downloaded=self.download.stat().st_size, added_on=None,
            content_path=str(self.download.parent), save_path=str(self.download.parent),
            category='llm-sub', tags='mikansub')
        self.snapshot = {'bangumi_id':2565,'episode':12,'info_hash':'b'*40,
            'torrent_url':'https://fixture.invalid/retained.torrent','title':self.torrent.name,'source':'mikan'}
        mw._upsert_mikan_extract_jobs(c, [(self.torrent, [self.snapshot], 1, False)], state_required=True)
        self.old_result = json.dumps({'extracted_count':1, 'failure_reason':'subtitle_validation_failed', 'failure_detail':'hard_qc_failed'})
        db = mw._mikan_state_connect(c)
        try:
            # Historical bad row is fixture seed data, never a Production edit.
            db.execute("UPDATE mikan_extract_jobs SET status='success',attempts=1,result_json=?", (self.old_result,))
            db.commit()
        finally:
            db.close()
        self.raw = {**mw._torrent_request_payload(self.torrent), 'amount_left':0}
        self.response = Mock()
        self.response.json.return_value = [self.raw]
        self.qbit = Mock(base_url='http://fixture.invalid')
        self.qbit._get_with_retry.return_value = self.response
        self.qbit.list_files.return_value = [QBitTorrentFile(self.download.name, self.download.stat().st_size, 1.0, 1)]
        f.worker._qbit = Mock(return_value=self.qbit)
        self.match = f.stack.enter_context(patch('mikan_worker._target_video_for_torrent_source', return_value=f.video))
        self.languages = f.stack.enter_context(patch('subtitle_extract.verified_official_subtitle_languages', return_value=set()))
        self.retained = {'torrent_hash':'b'*40,
            'extract_result_sha256':hashlib.sha256(self.old_result.encode()).hexdigest(),
            'source_identity':{'canonical_path':str(self.download.resolve()),
                'size':self.download.stat().st_size,'mtime_ns':self.download.stat().st_mtime_ns,
                'sha256':hashlib.sha256(self.download.read_bytes()).hexdigest()}}

    def invoke(self, **overrides):
        f = self.f
        return f.worker.revalidate_recorded_completion(mw.MikanReplacementTarget(2565,12),
            expected_entry_sha256=f.revision, source_identity=f.source, request_id='a'*64,
            authorization_ref='explicit-reviewed-retained-download',
            retained_download=overrides.get('retained_download', self.retained))

    def test_exact_reuse_archives_false_completion_then_normal_poller_claims_without_add(self):
        self.assertEqual(self.invoke()['status'], 'recorded')
        current = self.f.current()
        self.assertFalse(mw._pending_is_terminal_success(current))
        self.assertEqual(current['info_hash'], 'b'*40)
        review = current['completion_revalidation']
        self.assertEqual(review['prior_entry'], self.f.entry)
        self.assertEqual(review['download_evidence']['prior_extraction_job']['result_json'], self.old_result)
        self.assertEqual(review['historical_continuity'], 'UNKNOWN')
        self.assertEqual(mw._upsert_mikan_extract_jobs(self.f.worker.config,
            [(self.torrent,[current],1,True)], state_required=True), 1)
        claimed = mw._claim_mikan_extract_jobs(self.f.worker.config, limit=1)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].pending_entries[0]['episode'], 12)
        self.assertEqual(mw._claim_mikan_extract_jobs(self.f.worker.config, limit=1), [])
        db = mw._mikan_state_connect(self.f.worker.config)
        try:
            self.assertEqual(db.execute('SELECT attempts FROM mikan_extract_jobs').fetchone()[0], 2)
        finally:
            db.close()
        self.assertEqual(self.invoke()['status'], 'already_recorded')
        self.qbit.add_url.assert_not_called()
        self.assertEqual(hashlib.sha256(self.download.read_bytes()).hexdigest(), self.retained['source_identity']['sha256'])
        self.assertEqual(hashlib.sha256(self.f.video.read_bytes()).hexdigest(), self.f.source['sha256'])

    def test_incomplete_or_foreign_download_refused(self):
        for update in ({'progress':0.5}, {'amount_left':1}, {'category':'unrelated'}, {'tags':'foreign'}, {'hash':'c'*40}):
            with self.subTest(update=update):
                self.response.json.return_value = [{**self.raw, **update}]
                with self.assertRaises(mw.MikanWorkerError):
                    self.invoke()
                self.assertEqual(self.f.current(), self.f.entry)
        self.qbit.add_url.assert_not_called()

    def test_preallocated_incomplete_episode_refused(self):
        self.qbit.list_files.return_value = [QBitTorrentFile(self.download.name, self.download.stat().st_size, 0.5, 1)]
        with self.assertRaisesRegex(mw.MikanWorkerError, 'source_not_unique'):
            self.invoke()
        self.assertEqual(self.f.current(), self.f.entry)

    def test_complete_percentage_with_wrong_file_size_refused(self):
        self.qbit.list_files.return_value = [QBitTorrentFile(self.download.name, self.download.stat().st_size+1, 1.0, 1)]
        with self.assertRaisesRegex(mw.MikanWorkerError, 'file_size_mismatch'):
            self.invoke()
        self.assertEqual(self.f.current(), self.f.entry)

    def test_changed_source_receipt_and_wrong_match_refused(self):
        for field, value in (('extract_result_sha256','d'*64), ('source_identity', {**self.retained['source_identity'],'sha256':'e'*64})):
            with self.subTest(field=field), self.assertRaises(mw.MikanWorkerError):
                self.invoke(retained_download={**self.retained,field:value})
            self.assertEqual(self.f.current(), self.f.entry)
        self.match.return_value = self.f.root/'wrong-episode.mkv'
        with self.assertRaisesRegex(mw.MikanWorkerError, 'match_unproven'):
            self.invoke()

    def test_valid_traditional_output_is_not_reopened(self):
        self.languages.return_value = {'zh-tw'}
        with self.assertRaisesRegex(mw.MikanWorkerError, 'valid_output_exists'):
            self.invoke()
        self.assertEqual(self.f.current(), self.f.entry)

    def test_committed_request_survives_interruption_without_duplicate_request_or_download(self):
        save = mw._save_pending
        def interrupted(*args):
            save(*args)
            raise OSError('fixture exit after commit')
        with patch('mikan_worker._save_pending', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.invoke()
        archived = json.dumps(self.f.current(), sort_keys=True)
        self.assertEqual(self.invoke()['status'], 'already_recorded')
        self.assertEqual(json.dumps(self.f.current(), sort_keys=True), archived)
        with self.assertRaisesRegex(mw.MikanWorkerError, 'download_request_changed'):
            self.invoke(retained_download={**self.retained,'torrent_hash':'f'*40})
        self.qbit.add_url.assert_not_called()


if __name__ == '__main__':
    unittest.main()
