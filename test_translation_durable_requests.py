"""Actual Translator-to-SQLite binding with isolated transport fixtures."""
import json
import logging
import sqlite3
import threading
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import APIStatusError

from model_request_state import ModelRequestContext
import test_model_request_state as fixtures
from translator import (SubtitleTranslator, TranslationRequestInFlightError,
                        TranslationError, LINE_TRANSLATION_SYSTEM_PROMPT)


class TranslationDurableRequestTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModelRequestStateTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.context = ModelRequestContext(self.fixture.database,
            self.fixture.stage['stage_attempt_id'], 'c'*64, 2)
        self.calls = []

    def translator(self, create):
        instance = object.__new__(SubtitleTranslator)
        instance.config = SimpleNamespace(translator_base_url='http://receipt.invalid/' + self.id(),
                                          pipeline_job_store_required=True)
        instance.logger = logging.getLogger('test.durable.translation')
        instance.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        instance._request_context = self.context
        instance._translator_models = ('primary', 'fallback')
        instance._translator_model_index = 0
        instance._translator_model = 'primary'
        instance._progress_callback = None
        return instance

    def response(self, **kwargs):
        self.calls.append(kwargs['model'])
        return SimpleNamespace(model_dump_json=lambda: '{"content":"verified transport response"}',
            choices=[SimpleNamespace(message=SimpleNamespace(content='1\t字幕'))])

    def receipts(self):
        conn = sqlite3.connect(self.fixture.database)
        try:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT payload_json FROM pipeline_stage_events WHERE event_type LIKE 'MODEL_REQUEST_%' ORDER BY id")]
        finally:
            conn.close()

    def test_worker_binds_the_committed_attempt_not_progress_video(self):
        from worker import VideoWorker
        worker = object.__new__(VideoWorker)
        worker.config = SimpleNamespace(pipeline_job_store_required=True,
            scanner_state_path=str(self.fixture.database), max_retries=2)
        worker._translator = self.translator(self.response)
        worker._translator_progress_video = None
        worker._stage_state = SimpleNamespace(pipeline_jobs=lambda: self.fixture.store)
        with patch('m2_guardrail_runtime.worker_runtime_code_revision', return_value='e'*64):
            result = worker._get_translator(self.fixture.root / 'one.mkv')
        self.assertEqual(self.context.stage_attempt_id, result._request_context.stage_attempt_id)
        self.assertEqual('1\t字幕', result._request_translation('1\tsource'))

    def test_worker_refuses_missing_active_stage(self):
        from worker import VideoWorker
        worker = object.__new__(VideoWorker)
        worker.config = SimpleNamespace(pipeline_job_store_required=True)
        worker._translator = self.translator(self.response)
        worker._stage_state = None
        with self.assertRaisesRegex(RuntimeError, 'active_stage_required'):
            worker._get_translator(self.fixture.root / 'one.mkv')
        self.assertEqual([], self.calls)

    def test_worker_request_captures_frozen_provider_binding(self):
        from worker import VideoWorker
        from model_provider_evidence import capture_provider_binding, direct_endpoint_descriptor
        endpoint = 'http://192.0.2.10:11434/v1'
        binding = capture_provider_binding({'Id': 'a'*64, 'Image': 'sha256:' + 'b'*64,
            'State': {'Running': True, 'StartedAt': '2026-01-01T00:00:00Z'},
            'NetworkSettings': {'Ports': {'11434/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '11434'}]}}},
            direct_endpoint_descriptor(endpoint), ['192.0.2.10'], observed_at=1800000000.0)
        worker = object.__new__(VideoWorker)
        worker.config = SimpleNamespace(pipeline_job_store_required=True,
            scanner_state_path=str(self.fixture.database), max_retries=2,
            m2_server_canary_observer_enabled=True, translator_base_url=endpoint)
        worker._translator = self.translator(self.response)
        worker._translator.config.translator_base_url = endpoint
        worker._stage_state = SimpleNamespace(pipeline_jobs=lambda: self.fixture.store)
        with patch('m2_guardrail_runtime.worker_runtime_code_revision', return_value='e'*64), \
                patch('m2_guardrail_runtime.load_runtime_state', return_value={'baseline': {'model_provider': binding}}):
            translator = worker._get_translator(self.fixture.root / 'one.mkv')
        with self.assertRaises(TypeError):
            translator._request_context.provider_binding['container_id'] = 'f'*64
        self.assertEqual('1\t字幕', translator._request_translation('1\tsource'))
        self.assertEqual(binding, self.receipts()[0]['provider_binding'])

    def test_reservation_exists_before_transport_and_result_is_saved(self):
        def create(**kwargs):
            self.assertEqual('RESERVED', self.receipts()[-1]['state'])
            return self.response(**kwargs)
        instance = self.translator(create)
        self.assertEqual('1\t字幕', instance._request_translation('1\tsource'))
        self.assertEqual(['RESERVED', 'SETTLED'], [r['state'] for r in self.receipts()])

    def test_transport_error_retains_owner_across_translator_recreation(self):
        def fail(**kwargs):
            self.calls.append(kwargs['model'])
            raise ConnectionError('fixture connection lost')
        with self.assertRaises(TranslationRequestInFlightError):
            self.translator(fail)._request_translation('1\tsource')
        self.assertEqual('UNKNOWN', self.receipts()[-1]['state'])
        with self.assertRaises(TranslationRequestInFlightError):
            self.translator(self.response)._request_translation('1\tsource')
        self.assertEqual(['primary'], self.calls)

    def test_http_rejection_allows_only_configured_fallback(self):
        def create(**kwargs):
            if kwargs['model'] == 'primary':
                self.calls.append('primary')
                raise APIStatusError('fixture model absent', response=httpx.Response(404,
                    request=httpx.Request('POST', 'http://fixture.invalid')), body={})
            return self.response(**kwargs)
        self.assertEqual('1\t字幕', self.translator(create)._request_translation('1\tsource'))
        self.assertEqual(['primary', 'fallback'], self.calls)
        self.assertEqual(['HTTP_ERROR', 'RESPONSE'], [r['outcome'] for r in self.receipts() if r['state'] == 'SETTLED'])

    def test_missing_required_context_cannot_call_transport(self):
        instance = self.translator(self.response)
        instance._request_context = None
        with self.assertRaisesRegex(TranslationRequestInFlightError, 'context_required'):
            instance._request_translation('1\tsource')
        self.assertEqual([], self.calls)

    def test_submit_failure_does_not_assert_cancellation(self):
        instance = self.translator(self.response)
        with patch('translator.ThreadPoolExecutor') as executor:
            executor.return_value.submit.side_effect = RuntimeError('fixture submit failure')
            with self.assertRaisesRegex(TranslationRequestInFlightError, 'dispatch_outcome_unknown'):
                instance._request_translation('1\tsource')
        self.assertEqual('UNKNOWN', self.receipts()[-1]['state'])
        self.assertEqual([], self.calls)

    def test_confirmed_future_cancellation_records_not_dispatched(self):
        instance = self.translator(self.response)
        pending = Future()
        with patch('translator.ThreadPoolExecutor') as executor, \
                patch('translator._translation_request_hard_timeout_seconds', return_value=.01):
            executor.return_value.submit.return_value = pending
            with self.assertRaises(TranslationError):
                instance._request_translation_with_model_timeout('1\tsource', 'system', 'primary')
        self.assertTrue(pending.cancelled())
        self.assertEqual('NOT_DISPATCHED', self.receipts()[-1]['outcome'])
        self.assertEqual([], self.calls)

    def test_exhausted_primary_budget_does_not_block_unused_configured_fallback(self):
        def create(**kwargs):
            if kwargs['model'] == 'primary':
                self.calls.append('primary')
                raise APIStatusError('fixture model absent', response=httpx.Response(404,
                    request=httpx.Request('POST', 'http://fixture.invalid')), body={})
            return self.response(**kwargs)
        instance = self.translator(create)
        for _ in range(2):
            with self.assertRaises(TranslationError):
                instance._request_translation_with_model_timeout('1\tsource', LINE_TRANSLATION_SYSTEM_PROMPT, 'primary')
        self.assertEqual('1\t字幕', instance._request_translation('1\tsource'))
        self.assertEqual(['primary', 'primary', 'fallback'], self.calls)

    def test_gateway_timeout_does_not_prove_remote_completion(self):
        def create(**kwargs):
            self.calls.append(kwargs['model'])
            raise APIStatusError('fixture gateway timeout', response=httpx.Response(504,
                request=httpx.Request('POST', 'http://fixture.invalid')), body={})
        with self.assertRaises(TranslationRequestInFlightError):
            self.translator(create)._request_translation('1\tsource')
        self.assertEqual(['primary'], self.calls)
        self.assertEqual('UNKNOWN', self.receipts()[-1]['state'])
        self.assertEqual(504, self.receipts()[-1]['result_evidence']['status_code'])

    def unload_config(self):
        config = self.translator(self.response).config
        config.translator_ollama_auto_unload_enabled = True
        config.translator_model = 'primary'
        config.translator_fallback_models = []
        return config

    def test_unknown_request_blocks_model_unload_without_post(self):
        from ollama_lifecycle import unload_managed_translation_models
        context = self.context
        config = self.unload_config()
        request = context.reserve(endpoint=config.translator_base_url,
                                  request={'fixture': True}, model='primary')
        context.record(request['token'], 'UNKNOWN', {'reason_code': 'transport_error'})
        with patch('ollama_lifecycle._running_models', return_value=('primary',)), \
                patch('ollama_lifecycle._post_json') as post:
            result = unload_managed_translation_models(config, logging.getLogger('test'),
                request_context=context, model_names=('primary',))
        self.assertEqual((), result)
        post.assert_not_called()

    def test_unload_owns_endpoint_even_after_stage_has_finished(self):
        from ollama_lifecycle import unload_managed_translation_models
        self.fixture.store.finish_stage_attempt(self.context.stage_attempt_id, 'RETRYABLE_FAILURE',
            reason_code='fixture', evidence={'fixture': True}, confidence=1.0)
        self.fixture.store.commit()
        config = self.unload_config()
        def post(*args):
            current = self.receipts()[-1]
            self.assertEqual('UNLOAD', current['operation_kind'])
            self.assertEqual('RESERVED', current['state'])
            from model_request_state import ModelRequestStateError
            with self.assertRaisesRegex(ModelRequestStateError, 'ownership_unresolved'):
                self.context.reserve(endpoint=config.translator_base_url,
                    request={'operation': 'another unload'}, model='primary', operation_kind='UNLOAD')
            return {'done': True}
        with patch('ollama_lifecycle._running_models', side_effect=[('primary',), ()]), \
                patch('ollama_lifecycle._post_json', side_effect=post):
            self.assertEqual(('primary',), unload_managed_translation_models(config,
                logging.getLogger('test'), request_context=self.context, model_names=('primary',)))
        self.assertEqual('SETTLED', self.receipts()[-1]['state'])

    def test_unload_transport_failure_retains_unknown(self):
        from ollama_lifecycle import unload_managed_translation_models
        config = self.unload_config()
        with patch('ollama_lifecycle._running_models', return_value=('primary',)), \
                patch('ollama_lifecycle._post_json', side_effect=ConnectionError('fixture')):
            self.assertEqual((), unload_managed_translation_models(config, logging.getLogger('test'),
                request_context=self.context, model_names=('primary',)))
        self.assertEqual('UNKNOWN', self.receipts()[-1]['state'])
        with self.assertRaises(TranslationRequestInFlightError):
            self.translator(self.response)._request_translation('1\tsource')
        self.assertEqual([], self.calls)

    def test_unload_nonterminal_http_response_retains_owner(self):
        from ollama_lifecycle import unload_managed_translation_models
        config = self.unload_config()
        with patch('ollama_lifecycle._running_models', side_effect=[('primary',), ()]), \
                patch('ollama_lifecycle._post_json', return_value={'done': False}):
            self.assertEqual((), unload_managed_translation_models(config, logging.getLogger('test'),
                request_context=self.context, model_names=('primary',)))
        self.assertEqual('UNKNOWN', self.receipts()[-1]['state'])
        self.assertEqual('provider_completion_unproven', self.receipts()[-1]['result_evidence']['reason_code'])

    def test_timeout_completion_keeps_original_context(self):
        release, finished = threading.Event(), threading.Event()
        def create(**kwargs):
            release.wait(3)
            return self.response(**kwargs)
        instance = self.translator(create)
        try:
            with patch('translator._translation_request_hard_timeout_seconds', return_value=.05):
                with self.assertRaises(TranslationRequestInFlightError):
                    instance._request_translation('1\tsource')
            original = self.context.record
            def record(*args):
                try:
                    return original(*args)
                finally:
                    finished.set()
            # New binding after timeout cannot change the captured old attempt.
            instance._request_context = None
            with patch.object(ModelRequestContext, 'record', side_effect=record):
                release.set()
                self.assertTrue(finished.wait(3))
            self.assertEqual('SETTLED', self.receipts()[-1]['state'])
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
