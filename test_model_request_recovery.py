"""Isolated state-level controlled settlement; no live provider or DB access."""
import unittest
import hashlib
import json
from contextlib import ExitStack
from types import SimpleNamespace
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

    def local_fixture(self):
        self.prepare()
        self.config = SimpleNamespace(m2_recovery_enabled=True, work_path=self.root,
            log_path=self.root/'logs', scanner_state_path=self.database, translator_base_url=self.url)
        self.config.log_path.mkdir()
        self.log = self.config.log_path/'recovery.json'
        self.log.write_text(json.dumps({'recovery_record_id':'fixture-recovery',
            'recovery_mode':'authorized_reconciliation', 'completion_runtime': {
                'worker_runtime_code_revision':'code', 'configuration_fingerprint':'config',
                'worker_runtime_instance_fingerprint':'instance'}}), encoding='utf-8')
        self.control = self.root/'ai_control.json'
        self.control.write_text(json.dumps({'paused':True, 'requested_by':'m2-controlled-breaker-recovery'}), encoding='utf-8')
        self.state_path = self.root/'runtime.json'
        self.state_path.write_text(json.dumps({'status':'DISARMED',
            'disarm_reason':'controlled_breaker_recovery_pending_new_gate', 'recovery_record': {
                'recovery_record_id':'fixture-recovery',
                'log_sha256':'sha256:'+hashlib.sha256(self.log.read_bytes()).hexdigest()}}), encoding='utf-8')
        self.local_evidence = {'token':self.kwargs['token'], 'model_provider':self.new,
                               'recovery_log_path':str(self.log)}

    def local_resolve(self):
        from m2_guardrail_runtime import resolve_model_request_local
        with ExitStack() as stack:
            for target, value in [
                ('m2_guardrail_runtime.worker_runtime_code_revision', 'code'),
                ('m2_guardrail_runtime.configuration_fingerprint', 'config'),
                ('m2_guardrail_runtime.worker_runtime_instance_fingerprint', {'runtime_instance_fingerprint':'instance'}),
                ('m2_production_observation.require_durable_claim_pause', {'paused':True})]:
                stack.enter_context(patch(target, return_value=value))
            return resolve_model_request_local(self.config, self.local_evidence, state_path_override=self.state_path)

    def test_local_controlled_handoff_resolves_without_arming_or_resuming(self):
        self.local_fixture()
        before = (self.state_path.read_bytes(), self.control.read_bytes(), self.log.read_bytes())
        result = self.local_resolve()
        self.assertEqual('DISARMED', result['status'])
        self.assertFalse(result['claims_resumed'])
        self.assertEqual(before, (self.state_path.read_bytes(), self.control.read_bytes(), self.log.read_bytes()))
        self.assertTrue(self.local_resolve()['replay'])

    def test_local_other_pause_owner_or_unpaused_handoff_is_rejected(self):
        from m2_guardrail_runtime import RuntimeContractError
        self.local_fixture()
        for control in ({'paused':True,'requested_by':'another-operator'},
                        {'paused':False,'requested_by':'m2-controlled-breaker-recovery'}):
            self.control.write_text(json.dumps(control), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeContractError, 'pause_owner_mismatch'):
                self.local_resolve()
        with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
            self.reserve(operation_id='operation-two')

    def test_local_modified_recovery_log_is_rejected(self):
        from m2_guardrail_runtime import RuntimeContractError
        self.local_fixture()
        self.log.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeContractError, 'log_mismatch'):
            self.local_resolve()

    def test_local_armed_gate_cannot_be_used_to_release_request(self):
        from m2_guardrail_runtime import RuntimeContractError
        self.local_fixture()
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        state['status'] = 'ARMED'
        self.state_path.write_text(json.dumps(state), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeContractError, 'controlled_handoff_required'):
            self.local_resolve()

    def test_local_runtime_change_rejects_even_hash_matching_record(self):
        from m2_guardrail_runtime import RuntimeContractError
        self.local_fixture()
        record = json.loads(self.log.read_text(encoding='utf-8'))
        record['completion_runtime']['configuration_fingerprint'] = 'different'
        self.log.write_text(json.dumps(record), encoding='utf-8')
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        state['recovery_record']['log_sha256'] = 'sha256:'+hashlib.sha256(self.log.read_bytes()).hexdigest()
        self.state_path.write_text(json.dumps(state), encoding='utf-8')
        with self.assertRaisesRegex(RuntimeContractError, 'runtime_mismatch'):
            self.local_resolve()

    def test_host_requires_explicit_provider_and_planned_handoff_before_commands(self):
        from m2_guardrail_runtime import RuntimeContractError, recover_runtime_on_host
        with patch('m2_guardrail_runtime._inspect_container') as inspect:
            with self.assertRaisesRegex(RuntimeContractError, 'planned_provider_handoff_required'):
                recover_runtime_on_host(expected_worker_commit_sha='a'*40,
                    expected_webui_commit_sha='b'*40, expected_old_gate_id='old',
                    fault_summary_path='fixture', model_request_token='token',
                    docker_binary='docker', worker_container='worker', webui_container='webui',
                    expected_breaker_reason='fixture', affected_stage='TRANSLATING', failure_code='fixture')
            inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
