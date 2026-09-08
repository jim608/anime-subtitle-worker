"""Real HTTP and process-loss tests; loopback and disposable fixture state only."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import multiprocessing
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest

from model_request_state import (ModelRequestContext, pending_model_resource_request,
                                 record_model_sender_exit)
import test_model_request_state as fixtures


def _request_in_child(database, stage_id, endpoint, result, sender_id):
    from openai import OpenAI
    from translator import SubtitleTranslator, TranslationRequestInFlightError
    os.environ['ANIME_MODEL_SENDER_ID'] = sender_id
    translator = object.__new__(SubtitleTranslator)
    translator.config = SimpleNamespace(translator_base_url=endpoint,
        pipeline_job_store_required=True, translation_request_hard_timeout_seconds=30)
    translator.logger = logging.getLogger('test.restart')
    translator.client = OpenAI(base_url=endpoint, api_key='isolated-fixture', timeout=20, max_retries=0)
    translator._request_context = ModelRequestContext(Path(database), stage_id, 'c'*64, 2, 'shared_gpu')
    try:
        translator._request_translation_with_model_timeout('1\tfixture', 'fixture system', 'fixture-model')
        result.put('response')
    except TranslationRequestInFlightError:
        result.put('ownership_refused')
    finally:
        translator.client.close()


class ModelRequestRestartTest(unittest.TestCase):
    def test_sender_process_loss_does_not_replay_or_clear_remote_request(self):
        fixture = fixtures.ModelRequestStateTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        source = fixture.root / 'one.mkv'
        before = (source.stat().st_mtime_ns, hashlib.sha256(source.read_bytes()).hexdigest())
        stage_id = fixture.stage['stage_attempt_id']
        fixture.store.checkpoint_stage(stage_id, {'completed_batches': 1, 'source_sha256': before[1]},
            reason_code='fixture_checkpoint', evidence={'fixture': True}, confidence=1.0)
        fixture.store.commit()
        checkpoint = fixture.store._get_attempt(stage_id)['checkpoint_sha256']
        self.assertTrue(checkpoint)
        started, release, answered = threading.Event(), threading.Event(), threading.Event()
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(payload['model'])
                started.set()
                release.wait(15)
                body = json.dumps({'id': 'fixture-response', 'object': 'chat.completion',
                    'created': 1, 'model': 'fixture-model', 'choices': [{'index': 0,
                    'message': {'role': 'assistant', 'content': '1\tfixture result'},
                    'finish_reason': 'stop'}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    answered.set()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        service = threading.Thread(target=server.serve_forever, daemon=True)
        service.start()
        endpoint = 'http://127.0.0.1:%s/v1' % server.server_port
        context = multiprocessing.get_context('spawn')
        result = context.Queue()
        children = []
        try:
            first = context.Process(target=_request_in_child,
                args=(str(fixture.database), stage_id, endpoint, result, 'e'*32))
            children.append(first)
            first.start()
            self.assertTrue(started.wait(15), 'fixture model never received the request')
            owner = pending_model_resource_request(fixture.database, endpoint=endpoint)
            self.assertEqual('RESERVED', owner['state'])
            # Terminate only the isolated sender created by this test, while
            # the separate HTTP service demonstrably continues the request.
            first.terminate()
            first.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(answered.is_set())
            self.assertEqual(1, record_model_sender_exit(fixture.database, 'e'*32,
                                                        returncode=first.exitcode))
            second = context.Process(target=_request_in_child,
                args=(str(fixture.database), stage_id, endpoint, result, 'f'*32))
            children.append(second)
            second.start()
            self.assertEqual('ownership_refused', result.get(timeout=15))
            second.join(5)
            self.assertEqual(0, second.exitcode)
            self.assertEqual(['fixture-model'], calls)
            release.set()
            self.assertTrue(answered.wait(5))
            # An unobserved response is not a trusted completion receipt.
            remaining = pending_model_resource_request(fixture.database, endpoint=endpoint)
            self.assertEqual(owner['token'], remaining['token'])
            self.assertEqual(checkpoint, fixture.store._get_attempt(stage_id)['checkpoint_sha256'])
            self.assertEqual(before, (source.stat().st_mtime_ns,
                                     hashlib.sha256(source.read_bytes()).hexdigest()))
        finally:
            release.set()
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(5)
            server.shutdown()
            server.server_close()
            service.join(5)
            result.close()


if __name__ == '__main__':
    unittest.main()
