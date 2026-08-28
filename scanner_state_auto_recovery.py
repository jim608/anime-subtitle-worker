from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import time
from typing import Any
from urllib.parse import quote

from scanner_state_recovery import (
    DEPLOYMENT_HOLD_NAME,
    DEPLOYMENT_ID_RE,
    RECOVERY_REQUEST_NAME,
    _atomic_write_json,
    _read_json_object,
    restore_scanner_state_backup,
    verify_scanner_state_backup,
)


class DockerApiError(RuntimeError):
    pass


def _decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(body):
        line_end = body.find(b"\r\n", index)
        if line_end < 0:
            return body
        try:
            size = int(body[index:line_end].split(b";", 1)[0].strip(), 16)
        except ValueError:
            return body
        index = line_end + 2
        if size == 0:
            break
        decoded.extend(body[index : index + size])
        index += size + 2
    return bytes(decoded)


def _docker_request(
    docker_socket: Path,
    method: str,
    path: str,
    *,
    timeout_seconds: float = 75.0,
) -> Any:
    payload = b""
    headers = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Connection: close\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode("ascii")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(1.0, float(timeout_seconds)))
        client.connect(str(docker_socket))
        client.sendall(headers + payload)
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    header_bytes, separator, response_body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise DockerApiError("Docker API returned a malformed response")
    header_text = header_bytes.decode("iso-8859-1", errors="replace")
    first_line = header_text.splitlines()[0] if header_text else "HTTP/1.1 500"
    try:
        status_code = int(first_line.split()[1])
    except (IndexError, ValueError) as exc:
        raise DockerApiError(f"Docker API returned an invalid status line: {first_line}") from exc
    if "transfer-encoding: chunked" in header_text.casefold():
        response_body = _decode_chunked(response_body)
    text = response_body.decode("utf-8", errors="replace")
    if status_code >= 400:
        raise DockerApiError(f"Docker API error {status_code}: {text[:500]}")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _container_running(docker_socket: Path, name: str) -> bool:
    payload = _docker_request(
        docker_socket,
        "GET",
        f"/containers/{quote(name, safe='')}/json",
        timeout_seconds=10,
    )
    state = payload.get("State") if isinstance(payload, dict) else {}
    return bool(state.get("Running")) if isinstance(state, dict) else False


def _stop_container(docker_socket: Path, name: str) -> None:
    if not _container_running(docker_socket, name):
        return
    _docker_request(
        docker_socket,
        "POST",
        f"/containers/{quote(name, safe='')}/stop?t=60",
        timeout_seconds=75,
    )


def _start_container(docker_socket: Path, name: str) -> None:
    if _container_running(docker_socket, name):
        return
    _docker_request(
        docker_socket,
        "POST",
        f"/containers/{quote(name, safe='')}/start",
        timeout_seconds=30,
    )


def _wait_for_container_state(
    docker_socket: Path,
    name: str,
    *,
    running: bool,
    timeout_seconds: float = 60.0,
) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if _container_running(docker_socket, name) is running:
            return
        time.sleep(0.5)
    raise DockerApiError(
        f"container {name} did not become {'running' if running else 'stopped'}"
    )


def _update_request(request_path: Path, request: dict[str, Any], **changes: Any) -> None:
    request.update(changes)
    request["updated_at"] = time.time()
    _atomic_write_json(request_path, request)


def run_auto_recovery(
    work_root: Path,
    *,
    worker_container: str,
    webui_container: str,
    docker_socket: Path,
    request_path: Path | None = None,
) -> dict[str, Any]:
    work_root = work_root.resolve()
    request_path = (request_path or work_root / RECOVERY_REQUEST_NAME).resolve()
    if request_path.parent != work_root:
        raise RuntimeError("scanner recovery request must stay directly under the work root")
    request = _read_json_object(request_path)
    if str(request.get("status") or "") not in {
        "pending",
        "helper_started",
        "backup_verified",
        "restoring",
    }:
        raise RuntimeError("scanner recovery request is not pending")
    recovery_id = str(request.get("recovery_id") or "")
    source_deployment_id = str(request.get("source_deployment_id") or "")
    if DEPLOYMENT_ID_RE.fullmatch(recovery_id) is None:
        raise RuntimeError("scanner recovery id is invalid")
    if DEPLOYMENT_ID_RE.fullmatch(source_deployment_id) is None:
        raise RuntimeError("scanner recovery has no verified source deployment")
    hold_path = work_root / DEPLOYMENT_HOLD_NAME
    hold = _read_json_object(hold_path)
    if (
        not bool(hold.get("active"))
        or str(hold.get("deployment_id") or "") != recovery_id
        or str(hold.get("reason") or "") != "scanner-state-corruption"
    ):
        raise RuntimeError("scanner recovery requires its matching corruption hold")

    _update_request(
        request_path,
        request,
        status="helper_started",
        attempts=int(request.get("attempts") or 0) + 1,
        helper_pid=os.getpid(),
    )
    verified = verify_scanner_state_backup(work_root, source_deployment_id)
    _update_request(request_path, request, status="backup_verified", verified=verified)

    _stop_container(docker_socket, worker_container)
    _wait_for_container_state(docker_socket, worker_container, running=False)
    _stop_container(docker_socket, webui_container)
    _wait_for_container_state(docker_socket, webui_container, running=False)
    _update_request(request_path, request, status="restoring")

    restored = restore_scanner_state_backup(
        work_root,
        source_deployment_id,
        hold_deployment_id=recovery_id,
    )
    _start_container(docker_socket, worker_container)
    _start_container(docker_socket, webui_container)
    _wait_for_container_state(docker_socket, worker_container, running=True)
    _wait_for_container_state(docker_socket, webui_container, running=True)

    current_hold = _read_json_object(hold_path)
    if (
        not bool(current_hold.get("active"))
        or str(current_hold.get("deployment_id") or "") != recovery_id
    ):
        raise RuntimeError("scanner recovery hold changed before release")
    hold_path.unlink()
    result = {
        "status": "completed",
        "recovery_id": recovery_id,
        "source_deployment_id": source_deployment_id,
        "queue": restored.get("queue") or {},
        "completed_at": time.time(),
    }
    _update_request(request_path, request, **result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore scanner state from a verified anchor with database consumers stopped"
    )
    parser.add_argument("--work-root", type=Path, default=Path("/work"))
    parser.add_argument("--worker-container", default="anime-subtitle-worker")
    parser.add_argument("--webui-container", default="anime-subtitle-worker-webui")
    parser.add_argument("--docker-socket", type=Path, default=Path("/var/run/docker.sock"))
    parser.add_argument("--request-path", type=Path)
    args = parser.parse_args()
    request_path = args.request_path or args.work_root / RECOVERY_REQUEST_NAME
    try:
        result = run_auto_recovery(
            args.work_root,
            worker_container=args.worker_container,
            webui_container=args.webui_container,
            docker_socket=args.docker_socket,
            request_path=request_path,
        )
    except Exception as exc:  # noqa: BLE001 - recovery must leave a durable failure record.
        try:
            request = _read_json_object(request_path)
            _update_request(
                request_path,
                request,
                status="failed",
                failure=str(exc),
                failed_at=time.time(),
            )
        except Exception:
            pass
        try:
            _start_container(args.docker_socket, args.webui_container)
        except Exception:
            pass
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
