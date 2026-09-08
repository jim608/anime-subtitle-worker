"""Isolated state-level controlled settlement; no live provider or DB access."""
import unittest
from unittest.mock import patch

import test_model_request_state as fixtures
from model_provider_evidence import capture_provider_binding, direct_endpoint_descriptor
from model_request_state import (ModelRequestStateError, record_model_sender_exit,
    resolve_model_request_after_provider_restart)


class ModelRequestRecoveryTest(unittest.TestCase):
    setUp = fixtures.ModelRequestStateTest.setUp
    make_stage = fixtures.ModelRequestStateTest.make_stage
    reserve = fixtures.ModelRequestStateTest.reserve
    settle = fixtures.ModelRequestStateTest.settle

    def prepare(self, sender=True):
        self.url = 'http://192.0.2.10:11434/v1'
        descriptor = direct_endpoint_descriptor(self.url)
        inspection = {'Id': 'a'*64, 'Image': 'sha256:'+'b'*64,
            'State': {'Running': True, 'StartedAt': '2026-01-01T00:00:00Z'},
            'NetworkSettings': {'Ports': {'11434/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '11434'}]}}}
        old = capture_provider_binding(inspection, descriptor, ['192.0.2.10'], observed_at=1800000000)
        inspection['State']['StartedAt'] = '2026-01-02T00:00:00Z'
        self.new = capture_provider_binding(inspection, descriptor, ['192.0.2.10'], observed_at=1800000000)
        self.args.update(endpoint=descriptor['endpoint_sha256'], provider_binding=old, sender_id='d'*32)
        with patch('model_request_state._timestamp', return_value=1767225610.0):
            request = self.reserve()
            self.settle(request, 'UNKNOWN', {'reason_code': 'process_interrupted'})
        if sender:
            with patch('model_request_state._timestamp', return_value=1767225620.0):
                record_model_sender_exit(self.database, 'd'*32, returncode=1)
        self.kwargs = dict(token=request['token'], runtime_sha256='c'*64,
                           endpoint=self.url, current_provider_binding=self.new,
                           recovery_record_sha256='f'*64)
        return request

    def resolve(self, **changes):
        return resolve_model_request_after_provider_restart(self.store._conn, **{**self.kwargs, **changes})

    def test_resolution_is_durable_replayable_and_not_completed(self):
        request = self.prepare()
        before = (self.root/'one.mkv').read_bytes()
        result = self.resolve()
        self.assertEqual('PROVIDER_TERMINATED', result['outcome'])
        self.assertFalse(result['replay'])
        self.store.close()
        self.store = fixtures.PipelineJobStore(self.database)
        self.assertTrue(self.resolve()['replay'])
        stage = self.store._conn.execute('SELECT status FROM pipeline_stage_attempts WHERE stage_attempt_id=?',
                                        (self.stage['stage_attempt_id'],)).fetchone()
        self.assertEqual('RUNNING', stage[0])
        unknown_count = self.store._conn.execute("SELECT count(*) FROM pipeline_stage_events WHERE event_type='MODEL_REQUEST_UNKNOWN'").fetchone()[0]
        self.assertEqual(1, unknown_count)
        self.assertEqual(before, (self.root/'one.mkv').read_bytes())
        next_request = self.reserve(operation_id='operation-two', provider_binding=self.new)
        self.assertTrue(next_request['dispatch'])
        self.assertTrue(self.resolve()['replay'])  # old replay must not clear new owner
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-three')
        self.settle(next_request)
        with self.assertRaisesRegex(ModelRequestStateError, 'budget'):
            self.reserve(operation_id='operation-three')

    def test_missing_sender_evidence_preserves_owner(self):
        self.prepare(sender=False)
        with self.assertRaisesRegex(ModelRequestStateError, 'sender_exit_missing'):
            self.resolve()
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-two')

    def test_failed_commit_rolls_back_event_and_owner(self):
        self.prepare()
        with patch('model_request_state._write_current', side_effect=RuntimeError('fixture interruption')):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.resolve()
        count = self.store._conn.execute("SELECT count(*) FROM pipeline_stage_events WHERE event_type='MODEL_REQUEST_SETTLED'").fetchone()[0]
        self.assertEqual(0, count)
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-two')
        self.assertFalse(self.resolve()['replay'])

    def test_wrong_runtime_and_conflicting_receipt_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(ModelRequestStateError, 'runtime_mismatch'):
            self.resolve(runtime_sha256='e'*64)
        self.resolve()
        with self.assertRaisesRegex(ModelRequestStateError, 'result_conflict'):
            self.resolve(recovery_record_sha256='e'*64)

    def test_generic_result_api_cannot_claim_provider_termination(self):
        request = self.prepare()
        with self.assertRaisesRegex(ModelRequestStateError, 'invalid_outcome'):
            self.settle(request, 'PROVIDER_TERMINATED', {'recovery_record_sha256':'f'*64})


if __name__ == '__main__':
    unittest.main()
