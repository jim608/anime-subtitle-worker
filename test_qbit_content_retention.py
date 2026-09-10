"""Project-scoped retention must never inherit destructive seeding expiry."""
import unittest
from unittest.mock import Mock, patch
from qbit_client import QBitClient, QBitError

class ContentRetentionTests(unittest.TestCase):
    def client(self):
        c=QBitClient('http://qbit','fixture','fixture')
        c.session.get=Mock(return_value=Mock(status_code=200,text='2.15.1'))
        c.session.post=Mock(return_value=Mock(status_code=200,text='Ok.'))
        return c

    def test_protected_add_stops_at_limit_instead_of_deleting_content(self):
        c=self.client()
        c.add_url('https://fixture.invalid/episode.torrent',save_path='/downloads',
            category='llm-sub',tags=['mikan'],paused=False,preserve_content=True)
        data=c.session.post.call_args.kwargs['data']
        self.assertEqual(data['shareLimitAction'],'Stop')
        self.assertNotIn('seedingTimeLimit',data,'Preserve configured seeding duration')
        self.assertNotIn('ratioLimit',data)

    def test_unsupported_api_refuses_before_adding(self):
        c=self.client();c.session.get.return_value.text='2.11.0'
        with self.assertRaises(QBitError):
            c.add_url('https://fixture.invalid/episode.torrent',save_path='/downloads',
                category='llm-sub',tags=['mikan'],paused=False,preserve_content=True)
        c.session.post.assert_not_called()

    def test_unrelated_add_keeps_legacy_payload(self):
        c=self.client()
        c.add_url('https://fixture.invalid/other.torrent',save_path=None,
            category='other',tags=[],paused=True)
        self.assertNotIn('shareLimitAction',c.session.post.call_args.kwargs['data'])
        c.session.get.assert_not_called()

    def snapshot(self, **changes):
        return dict({'hash':'b'*40,'category':'llm-sub','tags':'mikan',
            'share_limit_action':'Default','ratio_limit':-2,'seeding_time_limit':120,
            'inactive_seeding_time_limit':-2},**changes)

    def test_exact_existing_torrent_retention_preserves_thresholds_and_replays(self):
        c=self.client();c._content_retention_supported=True
        before=self.snapshot();after=self.snapshot(share_limit_action='Stop')
        c.session.get.side_effect=[Mock(status_code=200,json=Mock(return_value=[row])) for row in (before,after,after)]
        self.assertTrue(c.ensure_content_retained('b'*40,category='llm-sub',tags=['mikan'])['changed'])
        self.assertFalse(c.ensure_content_retained('b'*40,category='llm-sub',tags=['mikan'])['changed'])
        c.session.post.assert_called_once_with('http://qbit/api/v2/torrents/setShareLimits',
            data={'hashes':'b'*40,'ratioLimit':-2,'seedingTimeLimit':120,
                  'inactiveSeedingTimeLimit':-2,'shareLimitAction':'Stop'},timeout=30)

    def test_scope_mismatch_never_changes_other_torrents(self):
        for changes in ({'hash':'c'*40},{'category':'other'},{'tags':'other'}):
            c=self.client();c._content_retention_supported=True
            c.session.get.return_value=Mock(status_code=200,json=Mock(return_value=[self.snapshot(**changes)]))
            with self.assertRaises(QBitError):c.ensure_content_retained('b'*40,category='llm-sub',tags=['mikan'])
            c.session.post.assert_not_called()

    def test_successful_http_without_readback_is_not_verification(self):
        c=self.client();c._content_retention_supported=True
        c.session.get.return_value=Mock(status_code=200,json=Mock(return_value=[self.snapshot()]))
        with self.assertRaisesRegex(QBitError,'readback_mismatch'):
            c.ensure_content_retained('b'*40,category='llm-sub',tags=['mikan'])

    def test_unknown_action_and_nan_threshold_fail_closed(self):
        for changes in ({'share_limit_action':None},{'ratio_limit':float('nan')}):
            c=self.client();c._content_retention_supported=True
            c.session.get.return_value=Mock(status_code=200,json=Mock(return_value=[self.snapshot(**changes)]))
            with self.assertRaises(QBitError):c.ensure_content_retained('b'*40,category='llm-sub',tags=['mikan'])
            c.session.post.assert_not_called()

    def test_all_selector_is_forbidden(self):
        c=self.client()
        with self.assertRaises(QBitError):c.ensure_content_retained('all',category='llm-sub',tags=['mikan'])
        c.session.get.assert_not_called();c.session.post.assert_not_called()

    def test_malformed_snapshot_is_a_scoped_client_error(self):
        c=self.client();c._content_retention_supported=True
        c.session.get.return_value=Mock(status_code=200,json=Mock(return_value=['not an object']))
        with self.assertRaises(QBitError):c.ensure_content_retained('b'*40,category='llm-sub',tags=['mikan'])
        c.session.post.assert_not_called()

    def test_invalid_json_remains_a_scoped_client_error(self):
        c=self.client();c._content_retention_supported=True
        c.session.get.return_value=Mock(status_code=200,json=Mock(side_effect=ValueError('not JSON')))
        with self.assertRaisesRegex(QBitError,'response_invalid'):
            c.ensure_content_retained('b'*40,category='llm-sub',tags=['mikan'])
        c.session.post.assert_not_called()


class ExtractionRetentionBoundaryTests(unittest.TestCase):
    def fixture(self):
        from test_mikan_mapped_source_recovery import MappedSourceRecoveryTest
        from mikan_worker import MikanWorker
        fixture=MappedSourceRecoveryTest();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.available()
        worker=MikanWorker.__new__(MikanWorker);worker.config=fixture.c;worker.logger=Mock()
        return fixture,worker

    def enqueue(self,fixture,worker,qbit):
        with patch('mikan_worker._active_pending_entries_for_completed_torrent',return_value=[fixture.entry]), \
             patch('mikan_worker._completed_torrent_has_local_episode',return_value=True):
            return worker._enqueue_completed_extract_jobs([fixture.torrent],{},[],qbit=qbit,state_required=True)

    def test_retention_failure_preserves_pending_extraction_and_source(self):
        f,w=self.fixture();before=f.state();source=f.source.read_bytes()
        q=Mock(spec=QBitClient);q.ensure_content_retained.side_effect=QBitError('readback_mismatch')
        self.enqueue(f,w,q)
        self.assertEqual(f.state(),before)
        self.assertEqual(f.source.read_bytes(),source)

    def test_verified_retention_allows_existing_missing_path_recovery(self):
        f,w=self.fixture()
        q=Mock(spec=QBitClient);q.ensure_content_retained.return_value={'status':'VERIFIED','changed':False}
        self.enqueue(f,w,q)
        self.assertEqual(f.state()[0][0],'queued')
        q.ensure_content_retained.assert_called_once_with(f.torrent.hash,category=f.c.qbit_category,tags=f.c.qbit_tags)
