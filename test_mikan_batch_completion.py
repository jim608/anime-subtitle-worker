"""Per-obligation completion for mixed-result collection downloads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mikan_worker import (MikanExtractResult, MikanWorker, _claim_mikan_extract_jobs,
    _finish_mikan_extract_job, _load_pending, _mark_pending, _pending_is_terminal_success,
    _save_pending, _upsert_mikan_extract_jobs, _mikan_state_connect,
    _extract_result_request_payload, _extract_result_from_request_payload,
    _apply_completed_extract_result, _requeue_claimed_mikan_extract_job)
from qbit_client import QBitTorrent
from test_mikan_worker import _episode_release, _logger, _mikan_process_config


class MikanBatchCompletionTest(unittest.TestCase):
    def fixture(self, root):
        downloads = root / 'downloads'
        downloads.mkdir()
        config = _mikan_process_config(root, downloads)
        worker = MikanWorker(config, _logger())
        worker._series_mappings = Mock(return_value=[])
        torrent = QBitTorrent(hash='a'*40, name='[Group] Show - 01-02 [CHT]',
            progress=1.0, state='uploading', dlspeed=0, downloaded=100, added_on=None,
            content_path=str(downloads), save_path=str(downloads), category='llm-sub', tags='mikansub')
        pending = {'items': {}}
        for ep in (1, 2):
            _mark_pending(pending, _episode_release('https://mikan/batch.torrent',
                          '[Group] Show - 01-02 [CHT]', ep))
            pending['items'][f'123:{ep}']['info_hash'] = torrent.hash
        _save_pending(worker.pending_path, pending)
        return config, worker, torrent, pending

    def test_legacy_mixed_count_cannot_complete_any_unverified_episode(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, worker, torrent, pending = self.fixture(Path(tmp))
            result = MikanExtractResult(1, failure_reason='subtitle_validation_failed',
                failure_detail='hard_qc_failed', subtitle_diagnostics=[{'reason': 'hard_qc_failed'}])
            worker.request_completed_state_update([(torrent, result)], reason='fixture')
            worker._consume_completed_state_update_request_unlocked()
            entries = _load_pending(worker.pending_path)['items']
            self.assertFalse(any(_pending_is_terminal_success(e) for e in entries.values()))
            self.assertTrue(all('hard_qc_failed' in json.dumps(e) for e in entries.values()))

    def test_durable_job_refuses_success_when_batch_contains_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, worker, torrent, pending = self.fixture(Path(tmp))
            _upsert_mikan_extract_jobs(config, [(torrent, list(pending['items'].values()), 2, True)], state_required=True)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            result = MikanExtractResult(1, failure_reason='subtitle_validation_failed', failure_detail='hard_qc_failed')
            self.assertTrue(_finish_mikan_extract_job(config, job.job_key, 'success', result, worker_id=job.worker_id))
            db = _mikan_state_connect(config)
            try:
                status, payload = db.execute('SELECT status,result_json FROM mikan_extract_jobs WHERE job_key=?', (job.job_key,)).fetchone()
            finally:
                db.close()
            self.assertNotEqual(status, 'success')
            self.assertEqual(json.loads(payload)['failure_reason'], 'subtitle_validation_failed')

    def test_real_batch_aggregator_keeps_exact_episode_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, worker, torrent, pending = self.fixture(root)
            sources = [root/'downloads'/f'Show - {ep:02}.mkv' for ep in (1, 2)]
            for p in sources:
                p.write_bytes(b'fixture-read-only-source')
            before = [p.read_bytes() for p in sources]
            with patch('mikan_worker._target_video_for_torrent_source', side_effect=lambda source, *a, **k: root/source.name), \
                 patch.object(worker, '_extract_completed_source_to_target', side_effect=[
                     MikanExtractResult(1), MikanExtractResult(0, failure_reason='subtitle_validation_failed', failure_detail='hard_qc_failed')]):
                result = worker._extract_completed_torrent(torrent, [], pending_entries=list(pending['items'].values()))
            encoded = _extract_result_request_payload(result)
            decoded = _extract_result_from_request_payload(json.loads(json.dumps(encoded)))
            self.assertEqual(set(decoded.entry_results), {'123:1', '123:2'})
            self.assertEqual(decoded.entry_results['123:1']['extracted_count'], 1)
            self.assertEqual(decoded.entry_results['123:2']['failure_reason'], 'subtitle_validation_failed')
            self.assertEqual([p.read_bytes() for p in sources], before)

    def mixed_result(self, *, deferred=False):
        second = MikanExtractResult(0,
            failure_reason='target_video_busy' if deferred else 'subtitle_validation_failed',
            failure_detail='busy' if deferred else 'hard_qc_failed',
            subtitle_diagnostics=[{'reason': 'hard_qc_failed'}] if not deferred else [],
            retryable=deferred, defer_seconds=10 if deferred else 0)
        return MikanExtractResult(1, failure_reason=second.failure_reason,
            failure_detail=second.failure_detail, retryable=deferred,
            defer_seconds=second.defer_seconds, entry_results={
                '123:1': _extract_result_request_payload(MikanExtractResult(1)),
                '123:2': _extract_result_request_payload(second)})

    def test_mixed_members_survive_deferred_write_restart_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, worker, torrent, pending = self.fixture(Path(tmp))
            result = self.mixed_result()
            worker.request_completed_state_update([(torrent, result)], reason='fixture')
            restarted = MikanWorker(config, _logger())
            restarted._series_mappings = Mock(return_value=[])
            applied = restarted._consume_completed_state_update_request_unlocked()
            after = _load_pending(worker.pending_path)
            good, bad = after['items']['123:1'], after['items']['123:2']
            self.assertTrue(_pending_is_terminal_success(good))
            self.assertFalse(_pending_is_terminal_success(bad))
            self.assertEqual(bad['last_extract_failure_reason'], 'subtitle_validation_failed')
            self.assertEqual(bad['last_subtitle_diagnostics'], [{'reason': 'hard_qc_failed'}])
            self.assertEqual(applied['replacement_targets'], [{'bangumi_id': 123, 'episode': 2}])
            frozen = json.dumps(after, sort_keys=True)
            changed, replacements = _apply_completed_extract_result(after, torrent, [], result)
            self.assertFalse(changed)
            self.assertEqual(replacements, [])
            self.assertEqual(json.dumps(after, sort_keys=True), frozen)

    def test_missing_member_evidence_is_deferred_not_filled_from_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, worker, torrent, pending = self.fixture(Path(tmp))
            result = MikanExtractResult(1, entry_results={
                '123:1': _extract_result_request_payload(MikanExtractResult(1))})
            _apply_completed_extract_result(pending, torrent, [], result)
            good, unknown = pending['items']['123:1'], pending['items']['123:2']
            self.assertTrue(_pending_is_terminal_success(good))
            self.assertFalse(_pending_is_terminal_success(unknown))
            self.assertEqual(unknown['info_hash'], torrent.hash)
            self.assertEqual(unknown['last_extract_deferred_reason'], 'completion_evidence_missing')
            self.assertNotIn(torrent.hash, unknown.get('failed_info_hashes', []))

    def test_partial_defer_receipt_survives_sqlite_reopen_and_keeps_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, worker, torrent, pending = self.fixture(Path(tmp))
            _upsert_mikan_extract_jobs(config, [(torrent, list(pending['items'].values()), 2, True)], state_required=True)
            job = _claim_mikan_extract_jobs(config, limit=1)[0]
            result = self.mixed_result(deferred=True)
            self.assertTrue(_requeue_claimed_mikan_extract_job(config, job, reason='busy', delay_seconds=30, result=result))
            self.assertFalse(_requeue_claimed_mikan_extract_job(config, job, reason='stale', result=MikanExtractResult(99)))
            self.assertEqual(_claim_mikan_extract_jobs(config, limit=1), [])
            db = _mikan_state_connect(config)
            try:
                status, payload = db.execute('SELECT status,result_json FROM mikan_extract_jobs WHERE job_key=?', (job.job_key,)).fetchone()
            finally:
                db.close()
            self.assertEqual(status, 'queued')
            restored = _extract_result_from_request_payload(json.loads(payload))
            self.assertEqual(restored.entry_results, result.entry_results)
            _apply_completed_extract_result(pending, torrent, [], restored)
            self.assertTrue(_pending_is_terminal_success(pending['items']['123:1']))
            busy = pending['items']['123:2']
            self.assertFalse(_pending_is_terminal_success(busy))
            self.assertEqual(busy['info_hash'], torrent.hash)
            self.assertEqual(busy['last_extract_deferred_reason'], 'target_video_busy')

    def test_all_verified_members_complete_without_cross_member_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, worker, torrent, pending = self.fixture(Path(tmp))
            result = MikanExtractResult(2, entry_results={
                key: _extract_result_request_payload(MikanExtractResult(1)) for key in pending['items']})
            changed, replacements = _apply_completed_extract_result(pending, torrent, [], result)
            self.assertTrue(changed)
            self.assertEqual(replacements, [])
            self.assertTrue(all(_pending_is_terminal_success(e) for e in pending['items'].values()))
            self.assertTrue(all(e['last_extracted_count'] == 1 for e in pending['items'].values()))

    def test_durable_success_requires_all_persisted_members_not_only_returned_members(self):
        for scoped in (True, False):
            with self.subTest(scoped=scoped), tempfile.TemporaryDirectory() as tmp:
                config, worker, torrent, pending = self.fixture(Path(tmp))
                _upsert_mikan_extract_jobs(config, [(torrent, list(pending['items'].values()), 2, True)], state_required=True)
                job = _claim_mikan_extract_jobs(config, limit=1)[0]
                result = MikanExtractResult(1, entry_results={
                    '123:1': _extract_result_request_payload(MikanExtractResult(1))} if scoped else None)
                self.assertTrue(_finish_mikan_extract_job(config, job.job_key, 'success', result, worker_id=job.worker_id))
                db = _mikan_state_connect(config)
                try:
                    status, raw = db.execute('SELECT status,result_json FROM mikan_extract_jobs WHERE job_key=?', (job.job_key,)).fetchone()
                finally:
                    db.close()
                self.assertNotEqual(status, 'success')
                self.assertEqual(json.loads(raw)['failure_reason'], 'completion_evidence_missing')

    def test_real_breaker_latch_refuses_claim_without_changing_queued_job(self):
        from m2_production_observation import trip_circuit_breaker
        with tempfile.TemporaryDirectory() as tmp, \
             patch('m2_production_observation._PROCESS_LOCAL_CIRCUIT_OPEN', False), \
             patch('m2_production_observation._PROCESS_LOCAL_CIRCUIT_OPEN_AT', 0):
            config, worker, torrent, pending = self.fixture(Path(tmp))
            _upsert_mikan_extract_jobs(config, [(torrent, list(pending['items'].values()), 2, True)], state_required=True)
            config.m2_server_canary_circuit_breaker_enabled = True
            config.m2_server_canary_circuit_breaker_state_path = 'fixture_breaker.json'
            trip_circuit_breaker(config, 'incorrect_completion', evidence={'stage': 'mikan_batch_completion'})
            self.assertEqual(_claim_mikan_extract_jobs(config, limit=1), [])
            db = _mikan_state_connect(config)
            try:
                row = db.execute('SELECT status,attempts,worker_id FROM mikan_extract_jobs').fetchone()
            finally:
                db.close()
            self.assertEqual(tuple(row), ('queued', 0, ''))

    def test_old_receipt_cannot_complete_a_new_active_source_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, worker, torrent, pending = self.fixture(Path(tmp))
            for entry in pending['items'].values():
                entry['info_hash'] = 'b'*40
            frozen = json.dumps(pending, sort_keys=True)
            result = MikanExtractResult(2, entry_results={
                key: _extract_result_request_payload(MikanExtractResult(1)) for key in pending['items']})
            changed, targets = _apply_completed_extract_result(pending, torrent, [], result)
            self.assertFalse(changed)
            self.assertEqual(targets, [])
            self.assertEqual(json.dumps(pending, sort_keys=True), frozen)

    def test_reconciliation_hold_blocks_normal_replacement_and_deferred_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, worker, torrent, pending = self.fixture(root)
            (root/'ai_control.json').write_text(json.dumps({'paused': True,
                'reconciliation_hold': True, 'reconciliation_id': 'fixture-owned-hold'}))
            qbit = Mock()
            worker._release_can_be_queued = Mock(return_value=True)
            release = _episode_release('https://mikan/new.torrent', '[Group] Show - 03 [CHT]', 3)
            for replacement in (False, True):
                result = worker._queue_selected_release_with_state_lock(release, qbit,
                    operation='fixture', state_required=True, unavailable_reason='unavailable',
                    add_failed_reason='failed', replacement=replacement)
                self.assertNotEqual(result, 'queued')
            frozen = json.dumps(_load_pending(worker.pending_path), sort_keys=True)
            self.assertEqual(worker._queue_deferred_releases(qbit, state_required=True, queue_lock_held=False), 0)
            self.assertEqual(json.dumps(_load_pending(worker.pending_path), sort_keys=True), frozen)
            qbit.add_url.assert_not_called()
            worker._qbit = Mock(side_effect=AssertionError('No API login while admission held'))
            self.assertEqual(worker.process_queued_extract_jobs(limit=1), 0)


if __name__ == '__main__':
    unittest.main()
