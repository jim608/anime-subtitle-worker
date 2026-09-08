"""Two-boot Docker probe. Requires an explicitly isolated /fixture volume."""
import hashlib
import json
import os
from pathlib import Path
import threading

from model_request_state import ModelRequestContext, ModelRequestStateError, pending_model_resource_request
from pipeline_state import PipelineJobStore
from safe_files import atomic_write_text


def main():
    if os.environ.get('M3_ISOLATED_RESTART') != '1':
        raise RuntimeError('isolated restart fixture authorization required')
    root = Path('/fixture')
    if not root.is_dir():
        raise RuntimeError('dedicated /fixture mount required')
    ready = root / 'ready.json'
    source = root / 'fixture-media.bin'
    database = root / 'state.sqlite3'
    if not ready.exists():
        if any(root.iterdir()):
            raise RuntimeError('first boot requires an empty isolated fixture directory')
        source.write_bytes(b'isolated restart source; not production media')
        with_store = PipelineJobStore(database)
        try:
            stat = source.stat()
            for state in ('STABILIZING', 'QUEUED'):
                observation = with_store.observe_ingest(source, size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns, event_type='created', state=state,
                    evidence={'isolated_restart': True}, confidence=1.0)
            attempt = with_store.start_stage_attempt(observation['job_id'], 'TRANSLATING',
                inputs={'fixture': True}, reason_code='isolated_restart',
                evidence={'isolated_restart': True}, confidence=1.0)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with_store.checkpoint_stage(attempt['stage_attempt_id'], {'source_sha256': digest, 'batch': 1},
                reason_code='isolated_checkpoint', evidence={'isolated_restart': True}, confidence=1.0)
            with_store.commit()
            context = ModelRequestContext(database, attempt['stage_attempt_id'], 'c'*64, 2, 'shared_gpu')
            receipt = context.reserve(endpoint='http://isolated.invalid/v1',
                                      request={'fixture': True}, model='fixture-model')
            context.record(receipt['token'], 'UNKNOWN', {'reason_code': 'transport_error'})
            proof = {'contract': 'm3-isolated-restart-v1', 'stage_id': attempt['stage_attempt_id'],
                'token': receipt['token'], 'source_sha256': digest, 'source_mtime_ns': stat.st_mtime_ns,
                'checkpoint_sha256': with_store._get_attempt(attempt['stage_attempt_id'])['checkpoint_sha256']}
            atomic_write_text(ready, json.dumps(proof, sort_keys=True))
        finally:
            with_store.close()
        print('ISOLATED_RESTART_READY', flush=True)
        threading.Event().wait()  # Host restarts only this named fixture container.
    else:
        proof = json.loads(ready.read_text(encoding='utf-8'))
        assert proof['contract'] == 'm3-isolated-restart-v1'
        store = PipelineJobStore(database)
        try:
            context = ModelRequestContext(database, proof['stage_id'], 'c'*64, 2, 'shared_gpu')
            try:
                context.reserve(endpoint='http://isolated.invalid/v1',
                                request={'fixture': True}, model='fixture-model')
            except ModelRequestStateError as exc:
                assert str(exc) == 'model_request_ownership_unresolved'
            else:
                raise AssertionError('restart incorrectly permitted a duplicate request')
            pending = pending_model_resource_request(database)
            assert pending['token'] == proof['token'] and pending['state'] == 'UNKNOWN'
            assert hashlib.sha256(source.read_bytes()).hexdigest() == proof['source_sha256']
            assert source.stat().st_mtime_ns == proof['source_mtime_ns']
            assert store._get_attempt(proof['stage_id'])['checkpoint_sha256'] == proof['checkpoint_sha256']
            result = {'contract': proof['contract'], 'restart_verified': True,
                'duplicate_dispatches': 0, 'ownership': 'UNKNOWN',
                'checkpoint_unchanged': True, 'fixture_source_unchanged': True,
                'production_resources_mounted': False}
            atomic_write_text(root / 'result.json', json.dumps(result, sort_keys=True))
            print(json.dumps(result, sort_keys=True), flush=True)
        finally:
            store.close()


if __name__ == '__main__':
    main()
