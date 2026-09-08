"""Durable model ownership at the existing resource admission boundaries."""
import unittest

from model_request_state import ModelRequestContext
from resource_runtime import build_resource_launch_plan, validate_authorized_resource_launch_plan
import test_model_request_state as fixtures
from test_resource_runtime import config, sample


class ModelResourceAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModelRequestStateTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.config = config(self.fixture.root)
        self.config.pipeline_job_store_required = True
        self.config.scanner_state_path = str(self.fixture.database)
        self.config.translator_base_url = 'http://fixture.invalid/v1'
        self.video = self.fixture.root / 'one.mkv'

    def pending(self, scope='shared_gpu'):
        context = ModelRequestContext(self.fixture.database, self.fixture.stage['stage_attempt_id'],
                                      'c'*64, 2, scope)
        receipt = context.reserve(endpoint=self.config.translator_base_url,
                                  request={'fixture': True}, model='primary')
        context.record(receipt['token'], 'UNKNOWN', {'reason_code': 'transport_error'})
        return context, receipt

    def plan(self, stage='transcription'):
        return build_resource_launch_plan(self.config, self.video, stage=stage, now=1000,
                                          telemetry_sampler=lambda _: sample())

    def test_unknown_shared_request_defers_gpu_despite_free_vram(self):
        self.pending()
        plan = self.plan()
        self.assertFalse(plan['admitted'])
        self.assertIn('model_request_resource_unresolved', plan['reason_codes'])
        self.assertEqual(1060, plan['retry_at'])

    def test_independent_request_does_not_block_local_gpu_but_blocks_endpoint(self):
        self.pending('independent')
        self.assertTrue(self.plan()['admitted'])
        self.assertFalse(self.plan('translation')['admitted'])

    def test_changed_endpoint_does_not_forget_old_unverified_owner(self):
        self.pending('unverified')
        self.config.translator_base_url = 'http://new-fixture.invalid/v1'
        self.assertFalse(self.plan()['admitted'])

    def test_unrelated_cpu_work_is_not_blocked(self):
        self.pending()
        self.assertTrue(self.plan('subtitle_extract')['admitted'])

    def test_owner_created_after_plan_is_rejected_at_launch(self):
        plan = self.plan()
        self.assertTrue(plan['admitted'])
        self.pending()
        with self.assertRaisesRegex(ValueError, 'model_request_resource_unresolved'):
            validate_authorized_resource_launch_plan(self.config, plan, self.video, now=1001)

    def test_verified_response_allows_admission_again(self):
        context, receipt = self.pending()
        self.assertFalse(self.plan()['admitted'])
        context.record(receipt['token'], 'RESPONSE', {'response_sha256': 'd'*64})
        self.assertTrue(self.plan()['admitted'])

    def test_missing_authority_is_not_proof_of_idle_gpu(self):
        self.config.scanner_state_path = str(self.fixture.root / 'missing.sqlite3')
        plan = self.plan()
        self.assertFalse(plan['admitted'])
        self.assertIn('model_request_authority_unavailable', plan['reason_codes'])


if __name__ == '__main__':
    unittest.main()
