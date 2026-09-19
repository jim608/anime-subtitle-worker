"""Opt-in real Docker lifecycle regression, only isolated fixture mounts."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

import verify_m1_docker_restart as runner


@unittest.skipUnless(os.environ.get("RUN_DOCKER_LIFECYCLE_TESTS") == "1", "UNRAID isolated Docker only")
class DockerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = os.environ["LIFECYCLE_TEST_IMAGE"]
        cls.root = Path(os.environ["LIFECYCLE_EVIDENCE_ROOT"]).resolve()
        cls.root.mkdir(parents=True, exist_ok=True)
        cls.image_id = json.loads(runner._run(["docker", "image", "inspect", cls.image]).stdout)[0]["Id"]
        cls.volumes = runner._run(["docker", "volume", "ls", "-q"]).stdout.splitlines()

    @classmethod
    def tearDownClass(cls):
        assert cls.volumes == runner._run(["docker", "volume", "ls", "-q"]).stdout.splitlines()

    def area(self):
        return Path(tempfile.mkdtemp(prefix="case-", dir=self.root))

    def check_removed(self, area):
        records = list(area.glob("restart-*/run.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertIsNone(runner._inspect(record["container_id"]))
        self.assertTrue((records[0].parent / "container.log").is_file())
        self.assertTrue((records[0].parent / "inspect-final.json").is_file())
        self.assertTrue((records[0].parent / "state").is_dir())
        self.assertEqual(runner.reap_finished_fixtures(area), {"removed": 0, "retained": 0, "warnings": 0})

    def test_real_restart_preserves_checkpoint_then_cleans(self):
        area = self.area()
        observed = []
        original = runner._run
        def tracked(command, **kwargs):
            if command[:2] == ["docker", "restart"]:
                c = runner._inspect(command[-1])
                self.assertTrue(c["State"]["Running"])
                observed.append(c["Id"])
            return original(command, **kwargs)
        with mock.patch.object(runner, "_run", side_effect=tracked):
            runner.run_smoke(self.image, pull=False, evidence_root=area)
        self.assertEqual(len(observed), 1)
        self.check_removed(area)

    def test_failure_retains_original_error_and_logs(self):
        area = self.area()
        program = "from pathlib import Path; p=Path('/state'); (p/'restart-phase.json').write_text('{}'); print('intentional fixture failure',flush=True); raise SystemExit(7)"
        with mock.patch.object(runner, "_container_program", return_value=program):
            with self.assertRaisesRegex(RuntimeError, "marker is missing"):
                runner.run_smoke(self.image, pull=False, evidence_root=area)
        self.check_removed(area)
        self.assertIn("intentional fixture failure", next(area.glob("restart-*/container.log")).read_text())

    def test_timeout_and_cancellation_cleanup_real_started_fixture(self):
        original = runner._run
        for failure in ("timeout", "cancel"):
            with self.subTest(failure=failure):
                area = self.area()
                injected = False
                def interrupted(command, **kwargs):
                    nonlocal injected
                    if not injected and command[:2] == ["docker", "start"]:
                        injected = True
                        original(command, **kwargs)
                        self.assertTrue(runner._inspect(command[-1])["State"]["Running"])
                        if failure == "cancel":
                            signal.raise_signal(signal.SIGTERM)
                        raise subprocess.TimeoutExpired(command, 10)
                    return original(command, **kwargs)
                with mock.patch.object(runner, "_run", side_effect=interrupted):
                    with self.assertRaises((subprocess.TimeoutExpired, KeyboardInterrupt)):
                        runner.run_smoke(self.image, pull=False, evidence_root=area)
                self.check_removed(area)

    def fixture(self, area, phase="cleanup_ready"):
        run_dir = Path(tempfile.mkdtemp(prefix="restart-", dir=area))
        (run_dir / "state").mkdir(); (run_dir / "owner.lock").touch()
        module = str(Path(runner.__file__).parent / "pipeline_state.py")
        command = ["-c", "from pathlib import Path; Path('/state/artifact.txt').write_text('retained evidence'); print('fixture done')"]
        labels = {runner.PROJECT_LABEL: "anime-subtitle-platform", runner.TEMP_LABEL: "true",
                  runner.RUN_LABEL: run_dir.name, runner.PURPOSE_LABEL: "restart-validation",
                  runner.CREATOR_LABEL: "verify_m1_docker_restart.py"}
        args = [a for k,v in labels.items() for a in ("--label", k + "=" + v)]
        cid = runner._run(["docker", "create", *args, "--network", "none", "--read-only",
            "--memory", "128m", "--cpus", "0.5", "--entrypoint", "python",
            "--mount", f"type=bind,source={run_dir / 'state'},target=/state",
            "--mount", f"type=bind,source={module},target=/pipeline_state.py,readonly",
            self.image_id, *command]).stdout.strip()
        record = {"container_id":cid,"image_id":self.image_id,"command":command,"state_module":module,"phase":phase}
        (run_dir / "run.json").write_text(json.dumps(record))
        runner._run(["docker", "start", "-a", cid])
        self.addCleanup(lambda: runner._archive_and_remove(run_dir, record))
        return run_dir,record

    def test_confirmed_orphan_cleanup_and_replay(self):
        area = self.area(); run_dir,record = self.fixture(area)
        self.assertEqual(runner.reap_finished_fixtures(area)["removed"], 1)
        self.assertEqual(runner.reap_finished_fixtures(area)["removed"], 0)
        self.assertEqual((run_dir / "state/artifact.txt").read_text(), "retained evidence")
        self.assertIsNone(runner._inspect(record["container_id"]))

    def test_cleanup_warning_does_not_mask_failure_and_next_entry_recovers(self):
        area = self.area()
        program = "from pathlib import Path; Path('/state/restart-phase.json').write_text('{}'); print('original failure',flush=True); raise SystemExit(7)"
        with mock.patch.object(runner, "_container_program", return_value=program), \
             mock.patch.object(runner, "_archive_and_remove", side_effect=RuntimeError("injected cleanup interruption")):
            with self.assertRaisesRegex(RuntimeError, "marker is missing"):
                runner.run_smoke(self.image, pull=False, evidence_root=area)
        run_dir = next(area.glob('restart-*'))
        self.assertIn("injected cleanup interruption", (run_dir / 'cleanup-warning.txt').read_text())
        self.assertEqual(runner.reap_finished_fixtures(area)["removed"], 1)
        self.check_removed(area)

    def test_active_lease_pending_restart_and_mismatched_record_retained(self):
        import fcntl
        area = self.area(); run_dir,record = self.fixture(area)
        with (run_dir / "owner.lock").open("r+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            self.assertEqual(runner.reap_finished_fixtures(area)["retained"], 1)
        record["phase"] = "restart_pending"
        (run_dir / "run.json").write_text(json.dumps(record))
        self.assertEqual(runner.reap_finished_fixtures(area)["retained"], 1)
        record["phase"] = "cleanup_ready"
        (run_dir / "run.json").write_text(json.dumps({**record, "image_id": "sha256:" + "0" * 64}))
        self.assertEqual(runner.reap_finished_fixtures(area)["warnings"], 1)
        self.assertIsNotNone(runner._inspect(record["container_id"]))

    def test_one_shot_success_and_failure_use_rm_with_persisted_evidence(self):
        area = self.area()
        for code in (0,7):
            cidfile = area / f"one-shot-{code}.cid"
            logfile = area / f"one-shot-{code}.log"
            with logfile.open("w") as output:
                result = subprocess.run(["docker", "run", "--rm", "--cidfile", str(cidfile),
                    "--network", "none", "--read-only", "--memory", "128m", "--cpus", "0.5",
                    "--label", runner.PROJECT_LABEL + "=anime-subtitle-platform",
                    "--label", runner.TEMP_LABEL + "=true", "--label", runner.RUN_LABEL + "=" + area.name,
                    "--label", runner.PURPOSE_LABEL + "=lifecycle-regression",
                    "--label", runner.CREATOR_LABEL + "=test_verify_m1_docker_restart.py",
                    "--entrypoint", "python", self.image_id, "-c", f"print('persistent evidence',flush=True); raise SystemExit({code})"],
                    stdout=output, stderr=subprocess.STDOUT, timeout=30)
            self.assertEqual(result.returncode, code)
            self.assertIn("persistent evidence", logfile.read_text())
            remaining = runner._run(["docker", "ps", "-aq", "--filter", "label=" + runner.RUN_LABEL + "=" + area.name,
                "--filter", "label=" + runner.PURPOSE_LABEL + "=lifecycle-regression"]).stdout.strip()
            self.assertFalse(remaining)


if __name__ == "__main__":
    unittest.main()
