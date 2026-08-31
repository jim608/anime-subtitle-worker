"""Run the M1 restart/resume smoke test in an actual disposable container.

The test creates uniquely named Docker resources, interrupts a running ASR
attempt, restarts the same container, and verifies that the WAL database and
checkpoint resume from the mounted volume.  It never touches a deployment
container or media directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


PASS_MARKER = "M1_DOCKER_RESTART_PASS"


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


def run_smoke(image: str, *, pull: bool) -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("docker CLI is not available")

    repository = Path(__file__).resolve().parent
    state_module = repository / "pipeline_state.py"
    if not state_module.is_file():
        raise FileNotFoundError(state_module)

    suffix = uuid.uuid4().hex[:12]
    container = f"anime-m1-restart-{suffix}"
    volume = f"anime-m1-state-{suffix}"
    created_container = False
    created_volume = False
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

        _run(["docker", "volume", "create", volume], timeout=30)
        created_volume = True
        _run(
            [
                "docker",
                "create",
                "--name",
                container,
                "--mount",
                f"type=volume,source={volume},target=/state",
                image,
                "python",
                "-u",
                "-c",
                _container_program(),
            ],
            timeout=30,
        )
        created_container = True
        _run(["docker", "cp", str(state_module), f"{container}:/pipeline_state.py"], timeout=30)
        _run(["docker", "start", container], timeout=30)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = _run(
                ["docker", "exec", container, "test", "-f", "/state/restart-phase.json"],
                check=False,
                timeout=10,
            )
            if ready.returncode == 0:
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
        if created_container:
            _run(["docker", "rm", "--force", container], check=False, timeout=30)
        if created_volume:
            _run(["docker", "volume", "rm", volume], check=False, timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.12-slim")
    parser.add_argument(
        "--no-pull",
        action="store_true",
        help="fail instead of pulling the disposable Python image when absent",
    )
    args = parser.parse_args()
    try:
        run_smoke(str(args.image), pull=not bool(args.no_pull))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"M1_DOCKER_RESTART_FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
