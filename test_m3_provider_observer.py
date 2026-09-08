"""Execute the actual Bash adapter with a disposable Docker command fixture."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid


@unittest.skipUnless(os.name == 'posix' and all(shutil.which(tool) for tool in ('bash','flock','timeout')), 'Linux host adapter test')
class ProviderObserverShellTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root/'commands.jsonl'
        self.worker = 'm3-observer-fixture-' + uuid.uuid4().hex
        self.env = {**os.environ, 'PATH':str(self.root)+os.pathsep+os.environ['PATH'],
            'M3_WORKER_CONTAINER':self.worker, 'M3_PROVIDER_CONTAINER':'provider-fixture',
            'M3_WORKER_CONFIG':'/fixture/config.yaml', 'FIXTURE_LOG':str(self.log), 'FIXTURE_FAIL':''}
        docker = self.root/'docker'
        docker.write_text('#!'+sys.executable+'\n'+'''import json,os,sys,time
args=sys.argv[1:]
with open(os.environ['FIXTURE_LOG'],'a') as log: log.write(json.dumps(args)+'\\n')
fail=os.environ['FIXTURE_FAIL']
if 'provider-context' in args:
 if fail=='timeout': time.sleep(30)
 if fail=='context': sys.exit(7)
 print(json.dumps({'gate_baseline_version':'fixture'}))
elif 'inspect' in args:
 if fail=='inspect': sys.exit(8)
 print(json.dumps({'Id':'fixture'}))
elif 'provider-refresh-inspected' in args:
 lines=sys.stdin.read().splitlines()
 assert len(lines)==5
 assert json.loads(lines[0])['gate_baseline_version']=='fixture'
 assert json.loads(lines[1])==json.loads(lines[2])
 assert float(lines[4])>0
 print(json.dumps({'status':'VERIFIED'}))
else: sys.exit(9)
''', encoding='utf-8')
        docker.chmod(0o755)
        sleep = self.root/'sleep'
        sleep.write_text('#!/bin/sh\n[ "$1" = 20 ]\n', encoding='utf-8')
        sleep.chmod(0o755)

    def run_script(self, mode='--once'):
        return subprocess.run(['bash', str(Path(__file__).with_name('m3-provider-observer.sh')), mode],
            env=self.env, text=True, capture_output=True, timeout=12)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_once_is_bounded_context_double_inspect_and_publish(self):
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('VERIFIED', json.loads(result.stdout)['status'])
        commands = self.commands()
        self.assertEqual(4, len(commands))
        self.assertIn('provider-context', commands[0])
        self.assertEqual('inspect', commands[1][0])
        self.assertEqual(commands[1], commands[2])
        self.assertIn('provider-refresh-inspected', commands[3])

    def test_upstream_failure_does_not_publish(self):
        for failure, expected in [('context',7), ('inspect',8)]:
            self.env['FIXTURE_FAIL'] = failure
            self.log.unlink(missing_ok=True)
            self.assertEqual(expected, self.run_script().returncode)
            self.assertFalse(any('provider-refresh-inspected' in args for args in self.commands()))

    def test_timeout_does_not_publish_or_retry_forever(self):
        self.env['FIXTURE_FAIL'] = 'timeout'
        self.assertEqual(124, self.run_script().returncode)
        self.assertEqual(1, len(self.commands()))

    def test_invalid_mode_does_not_execute_docker(self):
        self.assertEqual(2, self.run_script('--install').returncode)
        self.assertEqual([], self.commands())

    def test_scheduled_minute_makes_two_bounded_refreshes(self):
        result = self.run_script('--scheduled-minute')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(2, len(result.stdout.splitlines()))
        self.assertEqual(8, len(self.commands()))

    def test_existing_scheduler_owner_is_not_preempted(self):
        import fcntl
        lock = Path('/var/run')/('m3-provider-observer-'+self.worker+'.lock')
        with lock.open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(0, self.run_script().returncode)
            self.assertEqual([], self.commands())


if __name__ == '__main__':
    unittest.main()
