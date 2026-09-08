from copy import deepcopy
import unittest
import multiprocessing
from pathlib import Path
import tempfile
import threading

from model_provider_evidence import (ModelProviderEvidenceError, direct_endpoint_descriptor,
                                     capture_provider_binding, validate_provider_binding,
                                     prove_provider_restart_after_sender_exit)


def _hold_provider_writer(directory, ready):
    from model_provider_evidence import provider_observation_lock
    with provider_observation_lock(Path(directory)):
        ready.set()
        threading.Event().wait(15)


class ModelProviderEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.url = 'http://192.0.2.10:11434/v1'
        self.endpoint = direct_endpoint_descriptor(self.url)
        self.inspection = {'Id': 'a'*64, 'Image': 'sha256:' + 'b'*64,
            'State': {'Running': True, 'StartedAt': '2026-01-01T00:00:00.000000000Z'},
            'NetworkSettings': {'Ports': {'11434/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '11434'}]}}}

    def capture(self, inspection=None, hosts=None):
        return capture_provider_binding(inspection or self.inspection, self.endpoint,
            ['192.0.2.10'] if hosts is None else hosts, observed_at=1800000000.0)

    def test_direct_binding_roundtrips_and_retains_generation(self):
        binding = self.capture()
        self.assertEqual(binding, validate_provider_binding(binding, endpoint=self.url))
        self.assertEqual(self.inspection['State']['StartedAt'], binding['started_at'])
        self.assertNotIn('host', binding)

    def test_remote_host_and_wrong_published_port_are_rejected(self):
        with self.assertRaisesRegex(ModelProviderEvidenceError, 'not_on_attested_host'):
            self.capture(hosts=['192.0.2.11'])
        changed = deepcopy(self.inspection)
        changed['NetworkSettings']['Ports']['11434/tcp'][0]['HostPort'] = '9999'
        with self.assertRaisesRegex(ModelProviderEvidenceError, 'published_port_unproven'):
            self.capture(changed)

    def test_ambiguous_or_unpublished_endpoint_is_rejected(self):
        for bindings in ([], [{'HostIp': '0.0.0.0', 'HostPort': '11434'}]*2):
            changed = deepcopy(self.inspection)
            changed['NetworkSettings']['Ports']['11434/tcp'] = bindings
            with self.assertRaises(ModelProviderEvidenceError):
                self.capture(changed)

    def test_unstable_provider_cannot_be_attested(self):
        for field, value in [('Running', False), ('Paused', True), ('Restarting', True), ('Dead', True)]:
            changed = deepcopy(self.inspection)
            changed['State'][field] = value
            with self.assertRaisesRegex(ModelProviderEvidenceError, 'not_running_stably'):
                self.capture(changed)

    def test_future_or_missing_generation_is_rejected(self):
        for started in ('2099-01-01T00:00:00Z', '', '2026-01-01T00:00:00'):
            changed = deepcopy(self.inspection)
            changed['State']['StartedAt'] = started
            with self.assertRaisesRegex(ModelProviderEvidenceError, 'generation_unproven'):
                self.capture(changed)

    def test_changed_binding_or_endpoint_is_rejected(self):
        binding = self.capture()
        with self.assertRaises(ModelProviderEvidenceError):
            validate_provider_binding({**binding, 'container_id': 'c'*64}, endpoint=self.url)
        with self.assertRaises(ModelProviderEvidenceError):
            validate_provider_binding(binding, endpoint='http://192.0.2.11:11434/v1')

    def test_indirect_or_credential_bearing_routes_are_not_guessed(self):
        for endpoint in ('https://192.0.2.10:11434/v1', 'http://ollama:11434/v1',
                         'http://127.0.0.1:11434/v1', 'http://user:fixture@192.0.2.10:11434/v1',
                         self.url + '?token=fixture', self.url + '#fragment'):
            with self.assertRaises(ModelProviderEvidenceError):
                direct_endpoint_descriptor(endpoint)

    def restart_fixture(self):
        old = self.capture()
        changed = deepcopy(self.inspection)
        changed['State']['StartedAt'] = '2026-01-02T00:00:00Z'
        new = self.capture(changed)
        request = dict(provider_binding=old, sender_id='d'*32, token='fixture-token',
                       endpoint=old['endpoint_sha256'], runtime_sha256='e'*64,
                       created_at=1767225610.0)
        exit_proof = dict(request, sender_exited_no_later_than=1767225620.0,
                          returncode=1, timeout_reaped=False)
        return request, exit_proof, new

    def test_restart_after_reaped_sender_proves_order_without_mutating_inputs(self):
        request, exit_proof, new = self.restart_fixture()
        before = deepcopy((request, exit_proof, new))
        proof = prove_provider_restart_after_sender_exit(request, exit_proof, new, endpoint=self.url)
        self.assertEqual('same_provider_restarted_after_sender_exit', proof['reason_code'])
        self.assertEqual(before, (request, exit_proof, new))
        self.assertEqual(proof, prove_provider_restart_after_sender_exit(request, exit_proof, new,
                                                                        endpoint=self.url))

    def test_restart_before_sender_exit_and_invalid_clocks_cannot_release(self):
        for exited in (1767312000.0, 1767312001.0, 0, float('nan'), True):
            request, exit_proof, new = self.restart_fixture()
            exit_proof['sender_exited_no_later_than'] = exited
            with self.subTest(exited=exited), self.assertRaisesRegex(ModelProviderEvidenceError, 'order_unproven'):
                prove_provider_restart_after_sender_exit(request, exit_proof, new, endpoint=self.url)

    def test_other_sender_or_unbound_history_cannot_supply_proof(self):
        for field, value in [('token', 'other'), ('sender_id', 'f'*32),
                             ('runtime_sha256', 'f'*64), ('returncode', None)]:
            request, exit_proof, new = self.restart_fixture()
            exit_proof[field] = value
            with self.subTest(field=field), self.assertRaises(ModelProviderEvidenceError):
                prove_provider_restart_after_sender_exit(request, exit_proof, new, endpoint=self.url)
        request, exit_proof, new = self.restart_fixture()
        request.pop('provider_binding')
        with self.assertRaisesRegex(ModelProviderEvidenceError, 'original_binding_missing'):
            prove_provider_restart_after_sender_exit(request, exit_proof, new, endpoint=self.url)

    def test_replacement_container_or_unchanged_generation_is_not_termination(self):
        request, exit_proof, new = self.restart_fixture()
        changed = deepcopy(self.inspection)
        changed['Id'] = 'f'*64
        changed['State']['StartedAt'] = new['started_at']
        for binding in (self.capture(changed), request['provider_binding']):
            with self.assertRaises(ModelProviderEvidenceError):
                prove_provider_restart_after_sender_exit(request, exit_proof, binding, endpoint=self.url)

    def test_writer_lock_crosses_processes_and_releases_on_process_exit(self):
        from model_provider_evidence import provider_observation_lock
        context = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            ready = context.Event()
            process = context.Process(target=_hold_provider_writer, args=(directory, ready))
            process.start()
            try:
                self.assertTrue(ready.wait(10), 'fixture writer did not acquire lock')
                with self.assertRaisesRegex(ModelProviderEvidenceError, 'writer_busy'):
                    with provider_observation_lock(Path(directory)):
                        self.fail('concurrent writer entered')
                from types import SimpleNamespace
                from m2_guardrail_runtime import initialize_gate, refresh_provider_observation_local
                config = SimpleNamespace(work_path=Path(directory))
                for writer in (initialize_gate, refresh_provider_observation_local):
                    with self.assertRaisesRegex(ModelProviderEvidenceError, 'writer_busy'):
                        writer(config, {'model_provider':{}})
                self.assertEqual(['m3-provider-observation.lock'], sorted(path.name for path in Path(directory).iterdir()))
                process.terminate()  # only this disposable fixture process
                process.join(10)
                self.assertFalse(process.is_alive())
                with provider_observation_lock(Path(directory)):
                    self.assertTrue((Path(directory)/'m3-provider-observation.lock').exists())
                self.assertTrue((Path(directory)/'m3-provider-observation.lock').exists())
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(10)
                process.close()


if __name__ == '__main__':
    unittest.main()
