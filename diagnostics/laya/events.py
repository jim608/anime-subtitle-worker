"""Consume the existing post-commit JSONL via filesystem notifications, never Queue scans."""
import base64
import hashlib
import json
from pathlib import Path
import time

from sidecar import atomic_json, digest


def envelope(event, evidence_id, runtime):
    if not isinstance(event, dict) or not isinstance(event.get("evidence") or {}, dict):
        raise ValueError("event_must_be_object_with_object_evidence")
    ev = event.get("evidence") or {}
    if event.get("state") not in ("NEEDS_REVIEW", "FAILED", "RETRYING", "QUARANTINED") and "FAILED" not in event.get("event", ""):
        return None
    baseline = runtime.get("baseline", {})
    if str(event.get("timestamp", "")) < str(runtime.get("armed_at", "")):
        raise ValueError("event_predates_available_runtime_attestation")
    incident_id = digest({"job": event.get("job_id"), "attempt": event.get("attempt") or ev.get("delivery_attempt_id"),
                          "stage": event.get("stage") or ev.get("reported_legacy_stage"),
                          "reason": event.get("reason_code"), "state": event.get("state")})
    return {
        "event_id": incident_id, "failure_signature": event.get("reason_code") or ev.get("error_code"),
        "error_code": ev.get("error_code") or event.get("reason_code"),
        "raw_reason": ev.get("message") or event.get("reason_code"),
        "stage": event.get("stage") or ev.get("reported_legacy_stage") or "NOT_RECORDED",
        "attempt": event.get("attempt") or ev.get("delivery_attempt_id") or "NOT_RECORDED",
        "state_transition": {"from": ev.get("from_state", "NOT_RECORDED"), "to": event.get("state")},
        "scope": {"kind": "job", "job_id": event.get("job_id")},
        "runtime": {"worker_sha": baseline.get("worker_commit_sha"),
                    "attested_at": runtime.get("armed_at"), "source": "durable_runtime_attestation"},
        "resources": ev.get("resources") or {"status": "NOT_RECORDED"},
        "checkpoint": {"outputs_verified": ev.get("outputs_verified"),
                       "attempt_status": ev.get("attempt_status", "NOT_RECORDED")},
        "evidence_ids": [evidence_id],
    }


def consume(path, runtime_path, state_root, advisor):
    """Bounded, restart-idempotent drain. Commit cursor only after durable result."""
    path, state_root = Path(path), Path(state_root)
    cursor_path = state_root / "event-cursor.json"
    stat = path.stat()
    cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else None
    if cursor is None:
        # No automatic historical backlog evaluation; explicitly selected incidents only.
        atomic_json(cursor_path, {"inode": stat.st_ino, "offset": stat.st_size, "initial_tail_at": time.time()})
        return 0
    if cursor["inode"] != stat.st_ino or cursor["offset"] > stat.st_size:
        atomic_json(state_root / ("event-rotation-" + str(time.time_ns()) + ".json"), cursor)
        cursor = {"inode": stat.st_ino, "offset": 0}
    processed = 0
    with path.open("rb") as stream:
        stream.seek(cursor["offset"])
        for _ in range(32):
            start = stream.tell()
            raw = stream.readline(32769)
            if not raw:
                break
            if cursor.get("discarding_line") or (len(raw) == 32769 and not raw.endswith(b"\n")):
                # Preserve each bounded chunk before advancing, including across restart.
                # Never interpret a tail of an oversized line as a separate JSON event.
                discard = cursor.get("discarding_line") or {"start": start, "bytes": 0}
                end = stream.tell()
                atomic_json(state_root / "rejected-event-chunks" / f"{stat.st_ino}-{start}-{end}.json",
                            {"reason": "event_line_over_limit", "inode": stat.st_ino,
                             "line_start": discard["start"], "start": start, "end": end,
                             "chunk_sha256": hashlib.sha256(raw).hexdigest(),
                             "chunk_base64": base64.b64encode(raw).decode(),
                             "line_ended": raw.endswith(b"\n")})
                cursor = {"inode": stat.st_ino, "offset": end}
                if not raw.endswith(b"\n"):
                    cursor["discarding_line"] = {"start": discard["start"], "bytes": discard["bytes"] + len(raw)}
                atomic_json(state_root / "event-consumer-warning.json", {"reason": "event_line_over_limit",
                            "inode": stat.st_ino, "offset": discard["start"], "at": time.time()})
                atomic_json(cursor_path, cursor)
                processed += 1
                continue
            if not raw.endswith(b"\n"):
                if len(raw) < 32769:
                    break  # Writer has not committed a whole line yet.
            evidence_id = "raw:" + hashlib.sha256(raw).hexdigest()
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("event_not_object")
                # A repeated timestamp/log location is not new diagnostic evidence.
                semantic = {key: value for key, value in event.items() if key != "timestamp"}
                semantic_sha = digest(semantic)
                evidence_id = "event:" + semantic_sha
                bound = state_root / "event-evidence" / (semantic_sha + ".json")
                if not bound.exists():
                    atomic_json(bound, {"event": event, "source_file": str(path), "inode": stat.st_ino,
                                       "offset": start, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                                       "semantic_sha256": semantic_sha})
                runtime = json.loads(Path(runtime_path).read_text())
                item = envelope(event, evidence_id, runtime)
                if item is not None:
                    result = advisor.analyze(item)
                    if result.get("reason") == "diagnostic_busy":
                        break
                    if not result.get("record_id"):
                        atomic_json(state_root / "unavailable" / (hashlib.sha256(raw).hexdigest() + ".json"),
                                    dict(result, evidence_id=evidence_id, at=time.time()))
            except (ValueError, OSError, TypeError, RecursionError) as exc:
                atomic_json(state_root / "unavailable" / (hashlib.sha256(raw).hexdigest() + ".json"),
                            {"reason": "event_evidence_unavailable", "error_type": type(exc).__name__,
                             "evidence_id": evidence_id, "at": time.time(),
                             "raw_base64": base64.b64encode(raw).decode(), "offset": start})
            cursor = {"inode": stat.st_ino, "offset": stream.tell()}
            atomic_json(cursor_path, cursor)
            processed += 1
    return processed


def watch(path, runtime_path, root, advisor):
    from inotify_simple import INotify, flags
    with INotify() as notify:
        notify.add_watch(str(Path(path).parent), flags.MODIFY | flags.MOVED_TO | flags.CREATE)
        while True:
            processed = 0
            try:
                processed = consume(path, runtime_path, root, advisor)
            except OSError as exc:
                atomic_json(Path(root) / "event-consumer-warning.json",
                            {"reason": "event_source_unavailable", "error_type": type(exc).__name__, "at": time.time()})
            # Blocking OS notification; timeout is bounded recovery after lost notification,
            # never an SQL/Queue/media/log-history scan.
            # One drain is <=32 bounded records/chunks. Yield between backlog batches.
            notify.read(timeout=1000 if processed == 32 else 60000)
