import json
import logging
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import main
from model_request_state import ModelRequestContext, pending_model_resource_request
import test_model_request_state as fixtures


class ModelSenderSupervisionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModelRequestStateTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.config = SimpleNamespace(pipeline_job_store_required=True,
            config_path=self.fixture.root / 'config.yaml', work_path=self.fixture.root,
            scanner_state_path=str(self.fixture.database), ai_subprocess_timeout_seconds=1)
        self.sender_ids = []

    def launch(self, command, **kwargs):
        sender = kwargs['env']['ANIME_MODEL_SENDER_ID']
        self.sender_ids.append(sender)
        context = ModelRequestContext(self.fixture.database, self.fixture.stage['stage_attempt_id'],
                                      'c'*64, 2, sender_id=sender)
        context.reserve(endpoint='http://fixture.invalid/v1', request={'fixture': True}, model='fixture-model')
        return subprocess.CompletedProcess(command, 0)

    def run_child(self, effect):
        with patch('main.subprocess.run', side_effect=effect), \
                patch('main._persist_isolated_resource_failure'):
            return main._process_video_subprocess(self.config, self.fixture.root / 'one.mkv',
                                                  logging.getLogger('test.sender'))

    def exit_events(self):
        return [json.loads(row[0]) for row in self.fixture.store._conn.execute(
            "SELECT payload_json FROM pipeline_stage_events WHERE event_type='MODEL_REQUEST_SENDER_EXITED'")]

    def test_normal_return_records_exit_without_releasing_remote_owner(self):
        self.assertTrue(self.run_child(self.launch))
        proof = self.exit_events()[0]
        self.assertEqual(self.sender_ids[0], proof['sender_id'])
        self.assertEqual(0, proof['returncode'])
        self.assertFalse(proof['timeout_reaped'])
        self.assertIsNotNone(pending_model_resource_request(self.fixture.database))

    def test_reaped_timeout_records_sender_exit_only(self):
        def timed_out(command, **kwargs):
            self.launch(command, **kwargs)
            raise subprocess.TimeoutExpired(command, 1)
        self.assertFalse(self.run_child(timed_out))
        proof = self.exit_events()[0]
        self.assertTrue(proof['timeout_reaped'])
        self.assertIsNone(proof['returncode'])
        self.assertIsNotNone(pending_model_resource_request(self.fixture.database))

    def test_launch_error_does_not_assert_sender_exit(self):
        def failed(command, **kwargs):
            self.launch(command, **kwargs)
            raise OSError('fixture launch uncertainty')
        self.assertFalse(self.run_child(failed))
        self.assertEqual([], self.exit_events())
        self.assertIsNotNone(pending_model_resource_request(self.fixture.database))

    def test_real_subprocess_timeout_is_reaped_before_exit_evidence(self):
        self.config.ai_subprocess_timeout_seconds = 2
        real_run = subprocess.run
        code = (
            'from pathlib import Path; import time; '
            'from model_request_state import ModelRequestContext; '
            f'c=ModelRequestContext(Path({str(self.fixture.database)!r}), '
            f'{self.fixture.stage["stage_attempt_id"]!r}, "c"*64, 2); '
            'c.reserve(endpoint="http://fixture.invalid/v1", request={"fixture":True}, model="fixture-model"); '
            'time.sleep(20)'
        )
        def actual_child(_command, **kwargs):
            return real_run([sys.executable, '-B', '-c', code], **kwargs)
        self.assertFalse(self.run_child(actual_child))
        proof = self.exit_events()[0]
        self.assertTrue(proof['timeout_reaped'])
        self.assertIsNotNone(pending_model_resource_request(self.fixture.database))


if __name__ == '__main__':
    unittest.main()
