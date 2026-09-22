"""Consume the existing post-commit JSONL via filesystem notifications, never Queue scans."""
import hashlib
import json
from pathlib import Path
import time

from sidecar import atomic_json, digest


def envelope(event, evidence_id, runtime):
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
            if not raw.endswith(b"\n"):
                if len(raw) < 32769:
                    break  # Writer has not committed a whole line yet.
                # Do not feed a partial event; bounded defer, no infinite model retries.
                atomic_json(state_root / "event-consumer-warning.json", {"reason": "event_line_over_limit",
                            "inode": stat.st_ino, "offset": start, "at": time.time()})
                return processed
            evidence_id = "raw:" + hashlib.sha256(raw).hexdigest()
            try:
                event = json.loads(raw)
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
            except (ValueError, OSError, TypeError) as exc:
                atomic_json(state_root / "unavailable" / (hashlib.sha256(raw).hexdigest() + ".json"),
                            {"reason": "event_evidence_unavailable", "error_type": type(exc).__name__,
                             "evidence_id": evidence_id, "at": time.time()})
            cursor = {"inode": stat.st_ino, "offset": stream.tell()}
            atomic_json(cursor_path, cursor)
            processed += 1
    return processed


def watch(path, runtime_path, root, advisor):
    from inotify_simple import INotify, flags
    with INotify() as notify:
        notify.add_watch(str(Path(path).parent), flags.MODIFY | flags.MOVED_TO | flags.CREATE)
        while True:
            try:
                while consume(path, runtime_path, root, advisor) == 32:
                    pass
            except OSError as exc:
                atomic_json(Path(root) / "event-consumer-warning.json",
                            {"reason": "event_source_unavailable", "error_type": type(exc).__name__, "at": time.time()})
            # Blocking OS notification; timeout is bounded recovery after lost notification,
            # never an SQL/Queue/media/log-history scan.
            notify.read(timeout=60000)
