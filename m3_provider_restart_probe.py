"""Disposable-container provider restart evidence probe; never Production data."""
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, urlopen
import uuid

from model_provider_evidence import capture_provider_binding, direct_endpoint_descriptor
from model_request_state import (ModelRequestContext, ModelRequestStateError,
    pending_model_resource_request, record_model_sender_exit,
    resolve_model_request_after_provider_restart)
from pipeline_state import PipelineJobStore
from safe_files import atomic_write_text


def main():
    if os.environ.get('M3_ISOLATED_PROVIDER_TEST') != '1':
        raise RuntimeError('dedicated fixture authorization required')
    root = Path('/fixture')
    if not root.is_dir():
        raise RuntimeError('dedicated fixture mount required')
    mode = sys.argv[1]
    def save(name, value):
        atomic_write_text(root/name, json.dumps(value, sort_keys=True))
    def read(name):
        return json.loads((root/name).read_text(encoding='utf-8'))
    if mode == 'provider':
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != '/v1/fixture' or int(self.headers.get('Content-Length', 0)) > 64:
                    self.send_error(400)
                    return
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if body != b'{"fixture":true}':
                    self.send_error(400)
                    return
                save('request-received.json', {'received':True, 'pid':os.getpid(), 'at':time.time()})
                threading.Event().wait(90)  # host restarts only this labelled container
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        HTTPServer(('0.0.0.0', 11434), Handler).serve_forever()
        return
    endpoint = os.environ['M3_FIXTURE_ENDPOINT']
    descriptor = direct_endpoint_descriptor(endpoint)
    if descriptor['port'] < 20000:
        raise RuntimeError('fixture must use an ephemeral high port, never Production 11434')
    if mode in {'bind-old','bind-new'}:
        inspection = json.load(sys.stdin)
        binding = capture_provider_binding(inspection, descriptor,
            os.environ['M3_HOST_ADDRESSES'].split(), observed_at=time.time())
        save(mode+'.json', binding)
        print(json.dumps(binding), flush=True)
        return
    database = root/'state.sqlite3'
    if mode == 'sender':
        source = root/'fixture-media.bin'
        if source.exists() or database.exists():
            raise RuntimeError('sender fixture must be new')
        source.write_bytes(b'isolated media; no production mount')
        store = PipelineJobStore(database)
        try:
            stat = source.stat()
            for state in ('STABILIZING','QUEUED'):
                obs = store.observe_ingest(source, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    event_type='created', state=state, evidence={'fixture':True}, confidence=1.0)
            stage = store.start_stage_attempt(obs['job_id'], 'TRANSLATING', inputs={'fixture':True},
                reason_code='fixture', evidence={'fixture':True}, confidence=1.0)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            store.checkpoint_stage(stage['stage_attempt_id'], {'batch':1,'source_sha256':digest},
                reason_code='fixture', evidence={'fixture':True}, confidence=1.0)
            store.commit()
            sender = uuid.uuid4().hex
            context = ModelRequestContext(database, stage['stage_attempt_id'], 'c'*64, 2,
                'shared_gpu', sender_id=sender, provider_binding=read('bind-old.json'))
            receipt = context.reserve(endpoint=endpoint, request={'fixture':True}, model='fixture-model')
            try:
                with urlopen(Request(endpoint+'/fixture', data=b'{"fixture":true}', method='POST'), timeout=2) as response:
                    response.read()
            except TimeoutError:
                context.record(receipt['token'], 'UNKNOWN', {'reason_code':'transport_error'})
            else:
                raise RuntimeError('expected an in-flight provider request')
            assert read('request-received.json')['received'] is True
            save('sender.json', {'sender_id':sender, 'token':receipt['token'],
                'stage_id':stage['stage_attempt_id'], 'source_sha256':digest,
                'source_mtime_ns':stat.st_mtime_ns,
                'checkpoint_sha256':store._get_attempt(stage['stage_attempt_id'])['checkpoint_sha256']})
        finally:
            store.close()
        return
    saved = read('sender.json')
    if mode == 'sender-exited':
        # Host invokes only after docker wait + inspect prove the sender is reaped.
        inspection = json.load(sys.stdin)
        if inspection.get('Running') is not False or inspection.get('ExitCode') != 0:
            raise RuntimeError('fixture sender exit unproven')
        assert record_model_sender_exit(database, saved['sender_id'], returncode=0) == 1
        save('sender-exit-inspect.json', inspection)
        return
    if mode != 'resolve':
        raise RuntimeError('unknown fixture mode')
    store = PipelineJobStore(database)
    try:
        assert pending_model_resource_request(database)['state'] == 'UNKNOWN'
        kwargs = dict(token=saved['token'], runtime_sha256='c'*64, endpoint=endpoint,
            current_provider_binding=read('bind-new.json'),
            recovery_record_sha256=hashlib.sha256((root/'sender-exit-inspect.json').read_bytes()).hexdigest())
        result = resolve_model_request_after_provider_restart(store._conn, **kwargs)
        assert result['outcome'] == 'PROVIDER_TERMINATED'
        assert resolve_model_request_after_provider_restart(store._conn, **kwargs)['replay'] is True
        assert pending_model_resource_request(database) is None
        stage = store._get_attempt(saved['stage_id'])
        assert stage['checkpoint_sha256'] == saved['checkpoint_sha256']
        assert stage['status'] == 'RUNNING'  # ownership settlement is not job completion
        source = root/'fixture-media.bin'
        assert hashlib.sha256(source.read_bytes()).hexdigest() == saved['source_sha256']
        assert source.stat().st_mtime_ns == saved['source_mtime_ns']
        context = ModelRequestContext(database, saved['stage_id'], 'c'*64, 2,
            'shared_gpu', provider_binding=read('bind-new.json'))
        retry = context.reserve(endpoint=endpoint, request={'fixture':True}, model='fixture-model')
        assert retry['dispatch'] is True
        context.record(retry['token'], 'NOT_DISPATCHED', {'cancelled_before_start':True})
        try:
            context.reserve(endpoint=endpoint, request={'fixture':True}, model='fixture-model')
        except ModelRequestStateError as exc:
            assert 'budget' in str(exc)
        else:
            raise RuntimeError('recovery reset the budget')
        summary = {'provider_terminated_after_sender_exit':True, 'real_http_received':True,
            'settlement_replay_idempotent':True, 'checkpoint_unchanged':True,
            'source_unchanged':True, 'job_not_completed':True, 'budget_preserved':True,
            'production_resources_mounted':False}
        save('result.json', summary)
        print(json.dumps(summary, sort_keys=True), flush=True)
    finally:
        store.close()


if __name__ == '__main__':
    main()
