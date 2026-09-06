from pathlib import Path
from contextlib import closing
import tempfile
from types import SimpleNamespace
import unittest

from scan_state import ScanStateStore
from m2_production_recovery import (RecoveryError, install_source_hold, source_hold,
                                    require_source_not_held, mark_recovery_claimed,
                                    dispatch_next_recovery, persist_authorized_reconciliation)


class SourceHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'scanner.sqlite3'
        self.state = ScanStateStore(self.db)
        self.addCleanup(self.state.close)
        self.video = self.root / 'held.mkv'
        self.video.write_bytes(b'read-only-original')
        self.good = self.root / 'safe.mkv'
        self.good.write_bytes(b'safe-original')
        for p in (self.video, self.good):
            self.state.upsert_ai_queue_candidate(p, p.stat().st_mtime_ns)
        self.state.commit()
        self.config = SimpleNamespace(work_path=self.root, scanner_state_path=self.db)
        self.evidence = {'old_identity': {'mtime_ns': 1}, 'current_identity': {'mtime_ns': 2},
                         'historical_continuity': 'UNKNOWN'}

    def hold(self):
        result = install_source_hold(self.state.observation_connection, self.video,
                                     reconciliation_id='recon-test', evidence=self.evidence, now=100)
        self.state.commit()
        return result

    def test_normal_claim_and_selection_blocked_safe_job_continues(self):
        self.hold()
        self.assertNotIn(self.video, self.state.iter_ai_queue_candidates())
        self.assertIn(self.good, self.state.iter_ai_queue_candidates())
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            self.state.mark_ai_queue_running(self.video)
        self.state.mark_ai_queue_running(self.good)
        self.assertEqual(b'read-only-original', self.video.read_bytes())

    def test_restart_idempotency_and_revision_change_do_not_release_hold(self):
        first = self.hold()
        self.assertEqual(first, self.hold())
        self.state.upsert_ai_queue_candidate(self.video, 300)
        self.state.commit()
        with closing(ScanStateStore(self.db)) as restarted:
            self.assertEqual(first, source_hold(restarted.observation_connection, self.video))
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            require_source_not_held(self.config, self.video)
        require_source_not_held(self.config, self.good)

    def test_rollback_does_not_install_partial_hold(self):
        connection = self.state.observation_connection
        connection.execute('BEGIN')
        install_source_hold(connection, self.video, reconciliation_id='recon-test', evidence=self.evidence, now=100)
        connection.rollback()
        self.assertIsNone(source_hold(connection, self.video))

    def test_recovery_claim_and_ai_publication_cannot_bypass_hold(self):
        self.hold()
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            mark_recovery_claimed(self.state.observation_connection, self.video, 'new-attempt')
        from worker import VideoWorker
        worker = VideoWorker.__new__(VideoWorker)
        worker.config = self.config
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            worker._publish_ai_ass(self.video, None)
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            worker._publish_source_ass(self.video, self.root / 'unused.srt', self.root / 'unused.ass')

    def test_active_claim_cannot_be_quarantined_over(self):
        self.state.mark_ai_queue_running(self.video)
        with self.assertRaisesRegex(RecoveryError, 'source_hold_active_claim'):
            self.hold()
        self.assertIsNone(source_hold(self.state.observation_connection, self.video))

    def test_recovery_dispatch_skips_hold_and_dispatches_verified_peer(self):
        from test_m2_production_recovery import M2ProductionRecoveryTests
        fixture = M2ProductionRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        held, good = fixture._media('held.mkv'), fixture._media('good.mkv')
        fixture._queue(held)
        fixture._queue(good)
        fixture._reconcile()
        install_source_hold(fixture.connection, held, reconciliation_id='recon-test', evidence=self.evidence, now=100)
        import time
        result = dispatch_next_recovery(fixture.connection, runtime_status='ARMED', now=time.time() + 1000)
        self.assertTrue(result['dispatched'])
        self.assertEqual('DISPATCHED', fixture._recovery_row(good)['status'])
        self.assertEqual('READY', fixture._recovery_row(held)['status'])

    def test_conflicting_evidence_cannot_overwrite_original(self):
        first = self.hold()
        self.evidence['current_identity'] = {'mtime_ns': 999}
        with self.assertRaisesRegex(RecoveryError, 'source_hold_evidence_conflict'):
            self.hold()
        self.assertEqual(first, source_hold(self.state.observation_connection, self.video))

    def test_official_publish_and_worker_processing_refuse_before_side_effects(self):
        self.hold()
        from subtitle_extract import _publish_official_subtitle_set
        from worker import VideoWorker
        worker = VideoWorker.__new__(VideoWorker)
        worker.config = self.config
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            worker.process(self.video)
        with self.assertRaisesRegex(RecoveryError, 'source_continuity_unverified'):
            _publish_official_subtitle_set(self.video, [], self.config)
        self.assertEqual(b'read-only-original', self.video.read_bytes())

    def reconciliation_request(self):
        return {'authorization': {'scope': 'explicit_test_authorization'},
                'old_receipt': {'status': 'FAILED_PRESERVATION_NOT_PROVEN'},
                'old_gate_id': 'old-gate', 'runtime_identity': {'worker_sha': 'a' * 40},
                'differences': [{'path': str(self.video), 'disposition': 'QUARANTINE', 'evidence': self.evidence}],
                'recoverable_identities': [{'path': str(self.good), 'mtime_ns': self.good.stat().st_mtime_ns}]}

    def test_reconciliation_retains_missing_obligation_across_restart(self):
        request = self.reconciliation_request()
        absent = self.root / 'removed-from-queue.mkv'
        request['differences'].append({'path': str(absent), 'disposition': 'PENDING_REVIEW',
                                      'evidence': {'old_identity': {'mtime_ns': 4},
                                                   'current_identity': {'queue_member': False}}})
        connection = self.state.observation_connection
        record = persist_authorized_reconciliation(connection, reconciliation_id='rec1', request=request,
                                                    verified_snapshot={'unit_fixture': True}, now=100)
        self.state.commit()
        self.assertEqual('UNPROVEN', record['historical_preservation'])
        self.assertEqual(2, len(record['retained_holds']))
        self.assertFalse(absent.exists())
        with closing(ScanStateStore(self.db)) as restarted:
            again = persist_authorized_reconciliation(restarted.observation_connection, reconciliation_id='rec1',
                request=request, verified_snapshot={'unit_fixture': True}, now=200)
            self.assertEqual(record, again)
            self.assertIsNotNone(source_hold(restarted.observation_connection, absent))

    def test_reconciliation_failure_rolls_back_holds_and_record_together(self):
        request = self.reconciliation_request()
        request['recoverable_identities'].append({'path': str(self.video)})
        with self.assertRaisesRegex(RecoveryError, 'reconciliation_recoverable_member_held'):
            persist_authorized_reconciliation(self.state.observation_connection, reconciliation_id='rec1',
                request=request, verified_snapshot={'unit_fixture': True}, now=100)
        self.assertIsNone(source_hold(self.state.observation_connection, self.video))
        self.assertEqual(0, self.state.observation_connection.execute(
            'SELECT COUNT(*) FROM m2_recovery_reconciliations').fetchone()[0])

    def test_reconciliation_cannot_absorb_new_differences_under_existing_id(self):
        request = self.reconciliation_request()
        persist_authorized_reconciliation(self.state.observation_connection, reconciliation_id='rec1',
            request=request, verified_snapshot={'unit_fixture': True}, now=100)
        self.state.commit()
        request['differences'][0]['evidence'] = {'old_identity': {'mtime_ns': 999}, 'current_identity': {'mtime_ns': 2}}
        with self.assertRaisesRegex(RecoveryError, 'reconciliation_immutable_conflict'):
            persist_authorized_reconciliation(self.state.observation_connection, reconciliation_id='rec1',
                request=request, verified_snapshot={'unit_fixture': True}, now=200)


if __name__ == '__main__':
    unittest.main()
