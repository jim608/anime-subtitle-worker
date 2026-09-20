"""Report-only export failures must not corrupt or block durable job outcomes."""
import errno
import importlib
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import m2_observation_store as store
import m2_production_observation as observation
import test_m2_production_observation as fixtures
from m2_strict_observation import strict_evidence_template


class ReportExportAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.M2ProductionObservationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.config = self.fixture._config('report-export')

    def _journal(self, count=20):
        connection = store.connect_observation_database(self.config)
        try:
            for index in range(count):
                identity = f'report-{index}'
                observation.record_job_claim(self.config, job_identity=identity,
                                             claimed_at=time.time())
                observation.record_job_result(self.config, job_identity=identity,
                    outcome=self.fixture._verified(),
                    strict_evidence=strict_evidence_template(passed=True),
                    transaction_connection=connection)
            return store.latest_gate(connection)
        finally:
            connection.close()

    def test_report_permission_failure_does_not_block_next_claim(self):
        gate = self._journal()
        self.assertEqual(gate['settled_count'], 20)
        with patch.object(store, 'atomic_write_text',
                          side_effect=PermissionError(errno.EACCES, 'isolated report directory')):
            self.assertTrue(observation.admit_new_job(self.config))
        self.assertFalse(observation.circuit_breaker_active(self.config))
        observation.record_job_claim(self.config, job_identity='safe-next', claimed_at=time.time())

    def test_report_permission_failure_does_not_escape_terminal_commit(self):
        self._journal(19)
        observation.record_job_claim(self.config, job_identity='report-19', claimed_at=time.time())
        with patch.object(store, 'atomic_write_text',
                          side_effect=PermissionError(errno.EACCES, 'isolated report directory')):
            result = observation.record_job_result(self.config, job_identity='report-19',
                outcome=self.fixture._verified(),
                strict_evidence=strict_evidence_template(passed=True))
        self.assertEqual(result['settled_progress'], '20/20')
        self.assertFalse(observation.circuit_breaker_active(self.config))

    def test_summary_hash_corruption_still_blocks(self):
        gate = self._journal()
        connection = store.connect_observation_database(self.config)
        try:
            with store.immediate_transaction(connection):
                # Simulate storage corruption ONLY inside this temporary fixture;
                # restore the original immutable trigger before admission checks.
                trigger = connection.execute("SELECT sql FROM sqlite_master WHERE name=?",
                    ('trg_m2_observation_summary_write_once',)).fetchone()[0]
                connection.execute('DROP TRIGGER trg_m2_observation_summary_write_once')
                connection.execute('UPDATE m2_observation_gates SET summary_sha256=? WHERE gate_id=?',
                                   ('not-the-journal-hash', gate['gate_id']))
                connection.execute(trigger)
        finally:
            connection.close()
        self.assertFalse(observation.admit_new_job(self.config))
        self.assertTrue(observation.circuit_breaker_active(self.config))

    def test_restart_backoff_budget_and_access_change_recovery(self):
        gate = self._journal()
        output = Path(self.config.m2_server_canary_observation_output_dir)
        before = gate['summary_payload_json']
        base = time.time() + 1
        failure = PermissionError(errno.EACCES, 'isolated report directory')
        with patch.object(store, 'atomic_write_text', side_effect=failure) as write:
            for offset, expected in ((0, 1), (10, 1), (60, 2), (180, 3), (10000, 3)):
                with patch.object(store.time, 'time', return_value=base + offset):
                    self.assertTrue(observation.admit_new_job(self.config))
                self.assertEqual(write.call_count, expected)
        importlib.reload(store)
        state = observation.public_status(self.config)['summary_export']
        self.assertEqual(state['status'], 'WAITING_FOR_ACCESS_CHANGE')
        self.assertEqual(state['attempts'], 3)
        self.assertFalse(observation.circuit_breaker_active(self.config))
        # Actual directory permission metadata changes, not a fabricated retry reset.
        output.chmod(0o750)
        with patch.object(store.time, 'time', return_value=base + 10001):
            self.assertTrue(observation.admit_new_job(self.config))
        self.assertEqual((output / (gate['gate_id'] + '.json')).read_text(), before)
        self.assertEqual(observation.public_status(self.config)['summary_export']['status'], 'RESOLVED')
        self.assertEqual(store.publish_pending_summaries(self.config), [])
        observation.record_job_claim(self.config, job_identity='post-restart-next', claimed_at=time.time())

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0,
                     'real permission boundary requires non-root isolated container')
    def test_real_report_directory_permission_failure_then_recovery(self):
        gate = self._journal()
        output = Path(self.config.m2_server_canary_observation_output_dir)
        original_mode = output.stat().st_mode & 0o777
        output.chmod(0o500)
        try:
            self.assertTrue(observation.admit_new_job(self.config))
            self.assertFalse(observation.circuit_breaker_active(self.config))
            self.assertEqual(observation.public_status(self.config)['summary_export']['errno'], errno.EACCES)
            self.assertFalse((output / (gate['gate_id'] + '.json')).exists())
        finally:
            output.chmod(original_mode)
        self.assertTrue(observation.admit_new_job(self.config))
        report = output / (gate['gate_id'] + '.json')
        self.assertEqual(report.read_text(), gate['summary_payload_json'])
        self.assertEqual(json.loads(report.read_text())['settled'], '20/20')

    def test_report_collision_still_blocks_and_is_not_overwritten(self):
        gate = self._journal()
        target = Path(self.config.m2_server_canary_observation_output_dir) / (gate['gate_id'] + '.json')
        target.write_text('conflicting evidence')
        self.assertFalse(observation.admit_new_job(self.config))
        self.assertTrue(observation.circuit_breaker_active(self.config))
        self.assertEqual(target.read_text(), 'conflicting evidence')

    def _assert_safety_fault_blocks(self, error):
        self._journal()
        with patch.object(store, 'atomic_write_text', side_effect=error):
            self.assertFalse(observation.admit_new_job(self.config))
        self.assertTrue(observation.circuit_breaker_active(self.config))

    def test_disk_full_still_blocks(self):
        self._assert_safety_fault_blocks(OSError(errno.ENOSPC, 'real disk full fault'))

    def test_disk_io_still_blocks(self):
        self._assert_safety_fault_blocks(OSError(errno.EIO, 'real disk IO fault'))

    def test_unknown_code_error_still_blocks(self):
        self._assert_safety_fault_blocks(ValueError('unexpected implementation fault'))


if __name__ == '__main__':
    unittest.main()
