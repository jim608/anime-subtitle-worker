"""Run the M1 restart/resume smoke test in an actual disposable container.

The test creates uniquely named Docker resources, interrupts a running ASR
attempt, restarts the same container, and verifies that the WAL database and
checkpoint resume from the mounted volume.  It never touches a deployment
container or media directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from safe_files import atomic_write_text


PASS_MARKER = "M1_DOCKER_RESTART_PASS"
PROJECT_LABEL = "org.anime-subtitle.project"
TEMP_LABEL = "org.anime-subtitle.temporary"
RUN_LABEL = "org.anime-subtitle.run-id"
PURPOSE_LABEL = "org.anime-subtitle.purpose"
CREATOR_LABEL = "org.anime-subtitle.creator"


def _inspect(container_id: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("cleanup requires an exact container ID")
    result = _run(["docker", "inspect", container_id], check=False, timeout=30)
    if result.returncode:
        if "No such" in result.stderr:
            return None
        raise RuntimeError("container inspection failed; refusing cleanup")
    return json.loads(result.stdout)[0]


def _archive_and_remove(run_dir: Path, record: dict, *, stop_owned: bool = False) -> bool:
    """Only this entrypoint's recorded fixture; never names, volumes or services."""
    cid = record["container_id"]
    before = _inspect(cid)
    if before is None:
        (run_dir / "already-absent.json").write_text(json.dumps({"id": cid, "at": time.time()}))
        return True  # idempotent replay after a confirmed prior removal
    labels = before["Config"].get("Labels") or {}
    expected = {PROJECT_LABEL: "anime-subtitle-platform", TEMP_LABEL: "true",
                PURPOSE_LABEL: "restart-validation", RUN_LABEL: run_dir.name,
                CREATOR_LABEL: "verify_m1_docker_restart.py"}
    if any(labels.get(k) != v for k, v in expected.items()):
        raise RuntimeError("fixture ownership labels changed")
    if before["Image"] != record["image_id"] or before["Config"]["Cmd"] != record["command"]:
        raise RuntimeError("fixture image/command changed")
    mounts = before.get("Mounts", [])
    if len(mounts) != 2 or any(m.get("Type") != "bind" for m in mounts):
        raise RuntimeError("unexpected fixture mount/volume; retain for investigation")
    expected_mounts = {"/state": str(run_dir / "state"), "/pipeline_state.py": record["state_module"]}
    if {m["Destination"]: m["Source"] for m in mounts} != expected_mounts:
        raise RuntimeError("fixture mounts changed")
    (run_dir / "inspect-before-cleanup.json").write_text(json.dumps(before))
    if before["State"]["Running"]:
        if not stop_owned:
            return False
        _run(["docker", "stop", "--time", "5", cid], timeout=30)
    current = _inspect(cid)
    if current is None:
        raise RuntimeError("fixture disappeared before evidence export")
    if current["State"]["Running"] or current["State"]["Status"] not in {"exited", "created"}:
        return False
    logs = _run(["docker", "logs", "--timestamps", cid], timeout=30)
    (run_dir / "container.log").write_text(logs.stdout + logs.stderr)
    (run_dir / "inspect-final.json").write_text(json.dumps(current))
    # The writable layer is read-only; all test artifacts live in this retained
    # bind mount. A fresh state comparison closes the inspect/remove race.
    again = _inspect(cid)
    if (again is None or again["State"] != current["State"]
        or sorted(again["Mounts"], key=lambda m: m["Destination"]) != sorted(current["Mounts"], key=lambda m: m["Destination"])):
        return False
    _run(["docker", "rm", cid], timeout=30)
    (run_dir / "removed.json").write_text(json.dumps({"id": cid, "at": time.time(), "volumes_removed": 0}))
    return True


def reap_finished_fixtures(root: Path, *, limit: int = 20) -> dict[str, int]:
    """Bounded next-entry sweep of this script's durable run records only."""
    import fcntl
    result = {"removed": 0, "retained": 0, "warnings": 0}
    candidates = (p for p in root.glob("restart-*/run.json")
                  if not (p.parent / "removed.json").exists() and not (p.parent / "already-absent.json").exists())
    for record_path in sorted(candidates)[:limit]:
        run_dir = record_path.parent
        if record_path.is_symlink() or run_dir.is_symlink() or not (run_dir / "owner.lock").is_file():
            result["retained"] += 1
            continue
        with (run_dir / "owner.lock").open("r+") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result["retained"] += 1
                continue
            try:
                record = json.loads(record_path.read_text())
                if not record.get("container_id") and (run_dir / "container.id").is_file():
                    record["container_id"] = (run_dir / "container.id").read_text().strip()
            except (OSError, ValueError):
                result["warnings"] += 1
                continue
            # A pending restart is not garbage, even if its owner disappeared.
            if record.get("phase") not in {"creating", "cleanup_ready", "finished"} or not record.get("container_id"):
                result["retained"] += 1
                continue
            try:
                existed = _inspect(record["container_id"]) is not None
                if _archive_and_remove(run_dir, record):
                    result["removed"] += int(existed)
                else:
                    result["retained"] += 1
            except Exception as exc:
                result["warnings"] += 1
                (run_dir / "cleanup-warning.txt").write_text(str(exc))
    return result


def _run(
    command: list[str],
    *,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}: {detail}")
    return result


def _container_program() -> str:
    return r'''
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, "/")
from pipeline_state import PipelineJobStore

root = Path("/state")
database = root / "scanner_state.sqlite3"
media = root / "episode.mkv"
phase = root / "restart-phase.json"
inputs = {"media_revision_test": "docker-volume", "segment_plan": "all"}

if not phase.exists():
    media.write_bytes(b"immutable-docker-restart-source")
    stat = media.stat()
    store = PipelineJobStore(database)
    observed = store.observe_ingest(
        media,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        event_type="closed",
        state="QUEUED",
        evidence={"smoke_test": "docker_restart"},
        confidence=1.0,
    )
    job_id = str(observed["job_id"])
    attempt = store.start_stage_attempt(
        job_id,
        "ASR",
        inputs=inputs,
        model={"adapter": "smoke", "name": "none"},
        retry_limit=1,
        timeout_seconds=300,
        checkpoint={"segment": 17},
        reason_code="docker_smoke_asr_started",
        evidence={"container_phase": 1},
        confidence=1.0,
    )
    store.checkpoint_stage(
        str(attempt["stage_attempt_id"]),
        {"segment": 17, "durable": True},
        reason_code="docker_smoke_checkpoint",
        evidence={"container_phase": 1},
        confidence=1.0,
    )
    store.commit()
    store.close()
    phase.write_text(json.dumps({"job_id": job_id}), encoding="utf-8")
    print("M1_DOCKER_RESTART_READY", flush=True)
    time.sleep(3600)
else:
    payload = json.loads(phase.read_text(encoding="utf-8"))
    job_id = str(payload["job_id"])
    store = PipelineJobStore(database)
    recovered = store.recover_interrupted_stages(recover_all_running=True)
    assert len(recovered) == 1, recovered
    assert recovered[0]["stage"] == "ASR", recovered
    assert recovered[0]["checkpoint"] == {"segment": 17, "durable": True}, recovered
    job = store.get_job(job_id)
    assert job is not None and job["state"] == "RETRYING", job
    resumed = store.start_stage_attempt(
        job_id,
        "ASR",
        inputs=inputs,
        model={"adapter": "smoke", "name": "none"},
        retry_limit=1,
        timeout_seconds=300,
        checkpoint=recovered[0]["checkpoint"],
        reason_code="docker_smoke_asr_resumed",
        evidence={"container_phase": 2},
        confidence=1.0,
    )
    assert resumed["attempt_number"] == 2, resumed
    store.finish_stage_attempt(
        str(resumed["stage_attempt_id"]),
        "SUCCEEDED",
        outputs={
            "no_artifact_required": True,
            "checkpoint_evidence": {"segment": 17},
        },
        outputs_verified=True,
        reason_code="docker_smoke_asr_resumed_success",
        evidence={"container_phase": 2},
        confidence=1.0,
    )
    store.commit()
    assert media.read_bytes() == b"immutable-docker-restart-source"
    store.close()
    print("M1_DOCKER_RESTART_PASS", flush=True)
'''


def run_smoke(image: str, *, pull: bool, evidence_root: Path | None = None) -> None:
    import fcntl
    if shutil.which("docker") is None:
        raise RuntimeError("docker CLI is not available")

    repository = Path(__file__).resolve().parent
    state_module = repository / "pipeline_state.py"
    if not state_module.is_file():
        raise FileNotFoundError(state_module)

    root = (evidence_root or repository / "logs" / "container-validation").resolve()
    root.mkdir(parents=True, exist_ok=True)
    reap_finished_fixtures(root)
    run_dir = Path(tempfile.mkdtemp(prefix="restart-", dir=root))
    (run_dir / "state").mkdir()
    lease = (run_dir / "owner.lock").open("w+")
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    container = "anime-m1-" + run_dir.name
    record: dict = {}
    previous_signals = {}
    def cancelled(signum, _frame):
        raise KeyboardInterrupt(f"validation cancelled by signal {signum}")
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_signals[signum] = signal.signal(signum, cancelled)
    try:
        inspected = _run(
            ["docker", "image", "inspect", image],
            check=False,
            timeout=30,
        )
        if inspected.returncode != 0:
            if not pull:
                raise RuntimeError(f"Docker image is not present: {image}")
            _run(["docker", "pull", image], timeout=600)

        image_id = json.loads(_run(["docker", "image", "inspect", image]).stdout)[0]["Id"]
        labels = {PROJECT_LABEL: "anime-subtitle-platform", TEMP_LABEL: "true",
                  PURPOSE_LABEL: "restart-validation", RUN_LABEL: run_dir.name,
                  CREATOR_LABEL: "verify_m1_docker_restart.py"}
        label_args = [arg for k, v in labels.items() for arg in ("--label", k + "=" + v)]
        command = ["python", "-u", "-c", _container_program()]
        record = {"image_id": image_id, "state_module": str(state_module),
                  "command": command[1:], "phase": "creating", "created_at": time.time()}
        atomic_write_text(run_dir / "run.json", json.dumps(record))
        created = _run(
            [
                "docker",
                "create",
                "--name",
                container,
                "--cidfile", str(run_dir / "container.id"),
                *label_args,
                "--read-only", "--network", "none", "--memory", "256m", "--cpus", "0.5",
                "--tmpfs", "/tmp",
                "--mount",
                f"type=bind,source={run_dir / 'state'},target=/state",
                "--mount", f"type=bind,source={state_module},target=/pipeline_state.py,readonly",
                "--entrypoint", command[0], image_id, *command[1:],
            ],
            timeout=30,
        )
        container = created.stdout.strip()
        record.update(container_id=container, phase="restart_pending")
        atomic_write_text(run_dir / "run.json", json.dumps(record))
        _run(["docker", "start", container], timeout=30)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            # The checkpoint is bind-persisted. docker exec cannot inspect it
            # after a failed process exits and would hide the original failure.
            if (run_dir / "state" / "restart-phase.json").is_file():
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("first container run did not persist its checkpoint")

        _run(["docker", "restart", "--time", "1", container], timeout=30)
        _run(["docker", "wait", container], timeout=30)
        logs = _run(["docker", "logs", container], timeout=30).stdout
        if PASS_MARKER not in logs:
            raise RuntimeError(f"restart validation marker is missing; logs: {logs.strip()}")
        print(PASS_MARKER)
    finally:
        if record:
            if not record.get("container_id") and (run_dir / "container.id").is_file():
                record["container_id"] = (run_dir / "container.id").read_text().strip()
            record["phase"] = "cleanup_ready"
            atomic_write_text(run_dir / "run.json", json.dumps(record))
            try:
                if record.get("container_id") and not _archive_and_remove(run_dir, record, stop_owned=True):
                    raise RuntimeError("fixture changed state; retained without force")
            except Exception as exc:
                (run_dir / "cleanup-warning.txt").write_text(str(exc))
                print(f"CLEANUP_WARNING evidence={run_dir}: {exc}", file=sys.stderr)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
        lease.close()
        print(f"evidence={run_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="fail instead of pulling the disposable Python image when absent",
    )
    args = parser.parse_args()
    try:
        run_smoke(str(args.image), pull=not bool(args.no_pull), evidence_root=args.evidence_root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"M1_DOCKER_RESTART_FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
