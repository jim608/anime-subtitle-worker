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

    def test_srt_commit_lineage_is_hash_bound_immutable_and_not_publication(self):
        from model_request_state import ModelRequestContext, find_model_output_lineage
        from translator import SubtitleTranslator
        from srt_utils import SrtBlock
        import sqlite3
        request = self.prepare()
        self.settle(request)
        context = ModelRequestContext(self.database, self.stage['stage_attempt_id'], 'c'*64, 2,
                                      provider_binding=self.args['provider_binding'])
        translator = object.__new__(SubtitleTranslator)
        translator.config = SimpleNamespace(translator_base_url=self.url)
        translator._request_context = context
        translator._quality_events = []
        output = self.root/'fixture.zh-CN.srt'
        blocks = [SrtBlock(1, '00:00:01,000 --> 00:00:02,000', ['測試字幕'])]
        translator._commit_translation_output(output, blocks, reason='fixture')
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        kwargs = dict(job_id=self.stage['job_id'], output_path=output, output_sha256=digest,
                      execution_digest=context.checkpoint_identity(endpoint=self.url))
        proof = find_model_output_lineage(self.store._conn, **kwargs)
        self.assertIsNotNone(proof)
        self.assertFalse(proof['publication_verified'])
        self.store.close()
        self.store = fixtures.PipelineJobStore(self.database)
        self.assertEqual(proof, find_model_output_lineage(self.store._conn, **kwargs))
        self.assertEqual(b'isolated-source', (self.root/'one.mkv').read_bytes())
        self.assertEqual('RUNNING', self.store._get_attempt(self.stage['stage_attempt_id'])['status'])
        self.assertIsNone(find_model_output_lineage(self.store._conn, **{**kwargs, 'output_sha256':'a'*64}))
        self.assertIsNone(find_model_output_lineage(self.store._conn, **{**kwargs, 'job_id':'other'}))
        self.assertIsNone(find_model_output_lineage(self.store._conn, **{**kwargs, 'execution_digest':'e'*64}))
        self.assertTrue(context.record_output_lineage(endpoint=self.url, output_path=output, output_sha256=digest)['replay'])
        for action in ("DELETE FROM pipeline_stage_events WHERE event_type='MODEL_OUTPUT_PREPARED'",
                       "UPDATE pipeline_stage_events SET payload_json='{}' WHERE event_type='MODEL_OUTPUT_PREPARED'"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'lineage_immutable'):
                self.store._conn.execute(action)
            self.store._conn.rollback()
        second = self.reserve(operation_id='operation-two')
        self.settle(second)
        later = context.record_output_lineage(endpoint=self.url, output_path=output, output_sha256=digest)
        self.assertFalse(later['replay'])
        self.assertNotEqual(proof['token'], later['token'])
        self.assertGreater(later['request_event_watermark'], proof['request_event_watermark'])

    def test_confirmation_requires_later_matching_evidence_and_current_request_history(self):
        from model_request_state import ModelRequestContext, confirm_model_output_provider
        from model_provider_evidence import ModelProviderEvidenceError
        import sqlite3
        request = self.prepare()
        self.settle(request)
        context = ModelRequestContext(self.database, self.stage['stage_attempt_id'], 'c'*64, 2,
                                      provider_binding=self.args['provider_binding'])
        with patch('model_request_state._timestamp', return_value=1788000000.0):
            prepared = context.record_output_lineage(endpoint=self.url, output_path=self.root/'fixture.srt',
                                                     output_sha256='a'*64)
        observation = {'contract':'m3-provider-observation-v1', 'status':'VERIFIED',
            'gate_baseline_version':'fixture-gate', 'model_provider':dict(context.provider_binding),
            'checked_at':1788000001.0}
        def confirm(proof=observation, gate='fixture-gate'):
            return confirm_model_output_provider(self.store._conn, prepared_token=prepared['token'],
                                                  gate_baseline_version=gate, observation=proof)
        with patch('model_request_state._timestamp', return_value=1788000010.0):
            for timestamp in (1787999999.0, 1788000000.0):
                with self.assertRaisesRegex(ModelRequestStateError, 'confirmation_pending'):
                    confirm({**observation, 'checked_at':timestamp})
            for changed in ({'model_provider':{}}, {'status':'UNPROVEN'},
                            {'checked_at':1788000999.0}, {'gate_baseline_version':'other'}):
                with self.assertRaises(ModelProviderEvidenceError):
                    confirm({**observation, **changed})
            proof = confirm()
            self.assertTrue(proof['provider_continuity_verified'])
            self.assertFalse(proof['publication_verified'])
            self.store.close()
            self.store = fixtures.PipelineJobStore(self.database)
            self.assertTrue(confirm()['replay'])
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'confirmation_immutable'):
                self.store._conn.execute("DELETE FROM pipeline_stage_events WHERE event_type='MODEL_OUTPUT_PROVIDER_CONFIRMED'")
            self.store._conn.rollback()
            self.reserve(operation_id='operation-two')
            with self.assertRaisesRegex(ModelRequestStateError, 'request_history_changed'):
                confirm()

    def test_preparation_refuses_pending_or_different_execution_requests(self):
        from model_request_state import ModelRequestContext
        from dataclasses import replace
        request = self.prepare()
        context = ModelRequestContext(self.database, self.stage['stage_attempt_id'], 'c'*64, 2,
                                      provider_binding=self.args['provider_binding'])
        kwargs = dict(endpoint=self.url, output_path=self.root/'fixture.srt', output_sha256='a'*64)
        with self.assertRaisesRegex(ModelRequestStateError, 'requests_unresolved'):
            context.record_output_lineage(**kwargs)
        self.settle(request)
        for changed in (replace(context, runtime_sha256='d'*64), replace(context, provider_binding=self.new)):
            with self.assertRaisesRegex(ModelRequestStateError, 'execution_mismatch'):
                changed.record_output_lineage(**kwargs)
        self.assertFalse(context.record_output_lineage(**kwargs)['publication_verified'])

    def test_worker_cache_guard_consumes_exact_lineage_without_deleting_unproven_cache(self):
        from model_request_state import ModelRequestContext
        from model_provider_evidence import ModelProviderEvidenceError
        from worker import VideoWorker, SourceSelectionReviewError
        self.prepare()
        stage = self.make_stage('cache-source')
        video = self.root/'cache-source.mkv'
        output = self.root/'cache.zh-CN.srt'
        output.write_bytes(b'fixture cache bytes')
        baseline = {'model_provider':self.args['provider_binding'],
                    'worker_runtime_code_revision':'code', 'configuration_fingerprint':'config'}
        state = {'status':'ARMED', 'baseline':baseline, 'gate_baseline_version':'fixture-gate'}
        identity = hashlib.sha256(json.dumps({'code':'code','config':'config'}, sort_keys=True).encode()).hexdigest()
        context = ModelRequestContext(self.database, stage['stage_attempt_id'], identity, 2,
                                      provider_binding=self.args['provider_binding'])
        worker = object.__new__(VideoWorker)
        worker.config = SimpleNamespace(m2_server_canary_observer_enabled=True, work_path=self.root,
                                        translator_base_url=self.url)
        worker._stage_state = SimpleNamespace(pipeline_jobs=lambda:self.store)
        worker._pipeline_stage_inputs = lambda path: {'media_size':path.stat().st_size,
                                                       'media_mtime_ns':path.stat().st_mtime_ns}
        with patch('m2_guardrail_runtime.load_runtime_state', return_value=state), \
             patch('m2_guardrail_runtime.runtime_guardrail_status', return_value={'status':'ARMED','state':state}):
            with self.assertRaisesRegex(SourceSelectionReviewError, 'cache_lineage_unproven'):
                worker._validate_translation_cache_chain(video, SimpleNamespace(zh_cn_srt=output))
            self.assertEqual(b'fixture cache bytes', output.read_bytes())
            proof = context.record_output_lineage(endpoint=self.url, output_path=output,
                output_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(proof['token'], worker._require_model_output_lineage(video, output)['token'])
            baseline['model_provider'] = self.new
            with self.assertRaisesRegex(SourceSelectionReviewError, 'cache_lineage_unproven'):
                worker._require_model_output_lineage(video, output)
            self.assertEqual(b'fixture cache bytes', output.read_bytes())
            self.assertEqual(b'isolated-source', video.read_bytes())
            self.assertEqual('source_selection_review', worker._stage_for_exception(
                SourceSelectionReviewError('model_output_cache_lineage_unproven')))
            from m2_production_recovery import classify_failure
            self.assertEqual('QUALITY_BLOCKED', classify_failure('source_selection_review',
                'source_selection_needs_review', 'model_output_cache_lineage_unproven'))
            baseline.pop('model_provider')
            self.assertIsNone(worker._require_model_output_lineage(video, output))

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
