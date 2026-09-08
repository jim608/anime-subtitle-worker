"""Isolated durable request receipt contracts; no production DB or network."""
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pipeline_state import PipelineJobStore
from model_request_state import (
    ModelRequestStateError, reserve_model_request, record_model_request_result,
)


def _compete(database, args, ready, start, result):
    conn = sqlite3.connect(database, timeout=10)
    try:
        ready.put(True)
        if not start.wait(10):
            raise RuntimeError('race start timed out')
        try:
            receipt = reserve_model_request(conn, **args)
            result.put(('reserved', receipt['token']))
        except ModelRequestStateError as exc:
            result.put(('rejected', str(exc)))
    finally:
        conn.close()


class ModelRequestStateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'state.sqlite3'
        self.store = PipelineJobStore(self.database)
        self.addCleanup(lambda: self.store.close())
        self.stage = self.make_stage('one')
        self.args = dict(stage_attempt_id=self.stage['stage_attempt_id'],
                         operation_id='operation-one', endpoint='a'*64,
                         request_sha256='b'*64, model='configured-model',
                         runtime_sha256='c'*64, attempt_limit=2)

    def make_stage(self, name):
        media = self.root / (name + '.mkv')
        media.write_bytes(b'isolated-source')
        stat = media.stat()
        for state in ('STABILIZING', 'QUEUED'):
            obs = self.store.observe_ingest(media, size=stat.st_size,
                mtime_ns=stat.st_mtime_ns, event_type='created', state=state,
                evidence={'fixture': True}, confidence=1.0)
        stage = self.store.start_stage_attempt(obs['job_id'], 'TRANSLATING',
            inputs={'source': str(media)}, model={'name': 'configured-model'},
            reason_code='fixture', evidence={'fixture': True}, confidence=1.0)
        self.store.commit()
        return stage

    def reserve(self, **changes):
        return reserve_model_request(self.store._conn, **{**self.args, **changes})

    def settle(self, request, outcome='RESPONSE', evidence=None):
        return record_model_request_result(self.store._conn, token=request['token'],
            runtime_sha256='c'*64, outcome=outcome,
            evidence=evidence or {'response_sha256': 'd'*64})

    def test_reservation_is_durable_and_replay_is_not_dispatch(self):
        receipt = self.reserve()
        self.assertTrue(receipt['dispatch'])
        self.store.close()
        self.store = PipelineJobStore(self.database)
        self.assertFalse(self.reserve()['dispatch'])
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-two')
        with self.assertRaisesRegex(ModelRequestStateError, 'operation_conflict'):
            self.reserve(model='another-model')

    def test_endpoint_ownership_crosses_jobs(self):
        self.reserve()
        other = self.make_stage('two')['stage_attempt_id']
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(stage_attempt_id=other, operation_id='operation-two')
        self.assertTrue(self.reserve(stage_attempt_id=other, operation_id='operation-two',
                                     endpoint='e'*64)['dispatch'])

    def test_unknown_is_not_cancelled_or_age_reclaimed(self):
        receipt = self.reserve()
        self.settle(receipt, 'UNKNOWN', {'reason_code': 'hard_timeout'})
        self.assertTrue(self.settle(receipt, 'UNKNOWN', {'reason_code': 'hard_timeout'})['replay'])
        self.store.close()
        self.store = PipelineJobStore(self.database)
        with self.assertRaisesRegex(ModelRequestStateError, 'cannot_relabel'):
            self.settle(receipt, 'NOT_DISPATCHED', {'cancelled_before_start': True})
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-two')
        self.assertEqual('SETTLED', self.settle(receipt)['state'])

    def test_old_callback_does_not_release_new_owner(self):
        old = self.reserve()
        self.settle(old)
        new = self.reserve(operation_id='operation-two')
        self.assertTrue(self.settle(old)['replay'])
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-three')
        self.settle(new)

    def test_budget_survives_reopen(self):
        for operation in ('operation-one', 'operation-two'):
            self.settle(self.reserve(operation_id=operation))
        self.store.close()
        self.store = PipelineJobStore(self.database)
        with self.assertRaisesRegex(ModelRequestStateError, 'budget_exhausted'):
            self.reserve(operation_id='operation-three')

    def test_rebinding_cannot_expand_existing_request_budget(self):
        self.settle(self.reserve(attempt_limit=1))
        with self.assertRaisesRegex(ModelRequestStateError, 'budget_exhausted'):
            self.reserve(operation_id='operation-two', attempt_limit=3)

    def test_budget_survives_failed_stage_attempt(self):
        self.settle(self.reserve(attempt_limit=1))
        self.store.finish_stage_attempt(self.args['stage_attempt_id'], 'RETRYABLE_FAILURE',
            reason_code='fixture_retry', evidence={'fixture': True}, confidence=1.0)
        self.store.commit()
        next_stage = self.store.start_stage_attempt(self.stage['job_id'], 'TRANSLATING',
            inputs={'retry': True}, reason_code='fixture_retry',
            evidence={'fixture': True}, confidence=1.0)
        self.store.commit()
        with self.assertRaisesRegex(ModelRequestStateError, 'budget_exhausted'):
            self.reserve(stage_attempt_id=next_stage['stage_attempt_id'],
                         operation_id='operation-two', attempt_limit=1)

    def test_two_processes_cannot_own_same_endpoint(self):
        other = self.make_stage('two')
        context = multiprocessing.get_context('spawn')
        ready, result, start = context.Queue(), context.Queue(), context.Event()
        args = [self.args, {**self.args, 'stage_attempt_id': other['stage_attempt_id'],
                           'operation_id': 'operation-two'}]
        processes = [context.Process(target=_compete,
            args=(str(self.database), arg, ready, start, result)) for arg in args]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=10))
            start.set()
            results = [result.get(timeout=15) for _ in processes]
            self.assertEqual(['rejected', 'reserved'], sorted(row[0] for row in results))
            self.assertIn(('rejected', 'model_request_ownership_unresolved'), results)
            for process in processes:
                process.join(10)
                self.assertEqual(0, process.exitcode)
        finally:
            start.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()  # Only this test's isolated child processes.
                process.join(10)
            ready.close()
            result.close()

    def test_pending_owner_cannot_be_deleted(self):
        self.reserve()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'ownership_unresolved'):
            self.store._conn.execute('DELETE FROM pipeline_stage_attempts WHERE stage_attempt_id=?',
                                     (self.args['stage_attempt_id'],))
        self.store.rollback()
        self.assertFalse(self.reserve()['dispatch'])

    def test_pending_owner_cannot_be_erased_by_metadata_update(self):
        self.reserve()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'ownership_unresolved'):
            self.store._conn.execute("UPDATE pipeline_stage_attempts SET model_json='{}' WHERE stage_attempt_id=?",
                                     (self.args['stage_attempt_id'],))
        self.store.rollback()

    def test_receipt_history_cannot_be_deleted_or_rewritten(self):
        self.settle(self.reserve())
        for sql in ("DELETE FROM pipeline_stage_events WHERE event_type='MODEL_REQUEST_RESERVED'",
                    "UPDATE pipeline_stage_events SET payload_json='{}' WHERE event_type='MODEL_REQUEST_SETTLED'"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'receipt_immutable'):
                self.store._conn.execute(sql)
            self.store.rollback()

    def test_source_and_checkpoint_unchanged(self):
        source = self.root / 'one.mkv'
        before = (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())
        query = 'SELECT checkpoint_json,checkpoint_sha256 FROM pipeline_stage_attempts WHERE stage_attempt_id=?'
        checkpoint = tuple(self.store._conn.execute(query, (self.args['stage_attempt_id'],)).fetchone())
        self.settle(self.reserve())
        self.assertEqual(checkpoint, tuple(self.store._conn.execute(query, (self.args['stage_attempt_id'],)).fetchone()))
        self.assertEqual(before, (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest()))
        model = json.loads(self.store._conn.execute('SELECT model_json FROM pipeline_stage_attempts WHERE stage_attempt_id=?',
            (self.args['stage_attempt_id'],)).fetchone()[0])
        self.assertEqual('configured-model', model['name'])

    def test_caller_transaction_is_not_committed(self):
        self.store._conn.execute('BEGIN')
        with self.assertRaisesRegex(ModelRequestStateError, 'own_short_transaction'):
            self.reserve()
        self.assertTrue(self.store._conn.in_transaction)
        self.store.rollback()

    def test_runtime_mismatch_cannot_release_owner(self):
        receipt = self.reserve()
        with self.assertRaisesRegex(ModelRequestStateError, 'owner_runtime_mismatch'):
            record_model_request_result(self.store._conn, token=receipt['token'],
                runtime_sha256='f'*64, outcome='RESPONSE', evidence={'response_sha256': 'd'*64})
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-two')

    def test_unproven_cancellation_keeps_owner(self):
        receipt = self.reserve()
        with self.assertRaisesRegex(ModelRequestStateError, 'cancellation_unproven'):
            self.settle(receipt, 'NOT_DISPATCHED', {'cancelled_before_start': False})
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-two')


if __name__ == '__main__':
    unittest.main()
