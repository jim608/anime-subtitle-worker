"""Evidence-bound, one-shot repair through the existing Mikan extraction queue.

No torrent is added and no subtitle is published here. The original row, pending
revision and retry budget survive in an immutable existing-ledger event. Normal
Worker claim/extraction/validation/publication remains responsible for delivery.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

EVENT = "reviewed_extraction_repair"
CONTRACT = "m2-reviewed-extraction-repair-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _fail(reason: str) -> None:
    from mikan_worker import MikanWorkerError
    raise MikanWorkerError("reviewed_extraction_repair_" + reason)


def processing_identity() -> dict[str, str]:
    import subtitle_extract
    import subtitle_language_projection
    return {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for module in (subtitle_extract, subtitle_language_projection)}


def runtime_identity(config: Any) -> str:
    from m2_guardrail_runtime import runtime_guardrail_status
    from mikan_worker import _mikan_admission_allowed
    status = runtime_guardrail_status(config)
    if status.get("status") != "ARMED" or not _mikan_admission_allowed(config):
        _fail("admission_closed")
    baseline = (status.get("state") or {}).get("baseline")
    if not isinstance(baseline, dict) or not baseline.get("worker_commit_sha"):
        _fail("runtime_unproven")
    return digest(baseline)


def verify_identity(config: Any, identity: dict[str, Any]) -> Path:
    from m2_production_recovery import require_source_not_held
    from safe_files import sha256_file
    if (not isinstance(identity, dict) or not identity.get("canonical_path")
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("sha256") or ""))):
        _fail("source_identity_invalid")
    path = Path(identity["canonical_path"])
    if not path.is_absolute() or str(path.resolve()) != str(path):
        _fail("source_identity_invalid")
    require_source_not_held(config, path)
    before = path.stat()
    if (not path.is_file() or before.st_size <= 0 or before.st_size != identity.get("size")
            or before.st_mtime_ns != identity.get("mtime_ns")
            or ("device" in identity and before.st_dev != identity["device"])
            or ("inode" in identity and before.st_ino != identity["inode"])):
        _fail("source_changed")
    content_hash = sha256_file(path)
    after = path.stat()
    if content_hash != identity["sha256"] or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        _fail("source_changed")
    return path


def _read_evidence(config: Any, descriptor: dict[str, Any]) -> bytes:
    path = Path(descriptor.get("path", "")).resolve()
    if not path.is_relative_to(Path(config.log_path).resolve()) or path.stat().st_size > 8 * 1024 * 1024:
        _fail("evidence_path_invalid")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor.get("sha256"):
        _fail("evidence_changed")
    return raw


def verify_repair_evidence(config: Any, request: dict[str, Any]) -> None:
    report = json.loads(_read_evidence(config, request["validation_evidence"]))
    attestation = _read_evidence(config, request["code_attestation"]).decode("utf-8")
    current_code = processing_identity()
    observed: dict[str, str] = {}
    for line in attestation.splitlines():
        sha, name = line.split(None, 1)
        name = Path(name.strip()).name
        if name in current_code:
            if name in observed:
                _fail("code_attestation_ambiguous")
            observed[name] = sha
    if observed != current_code or request["processing_identity"] != observed:
        _fail("repair_code_changed")
    evidence = report.get("transformation") or {}
    validation = evidence.get("target_validation") or {}
    if (report.get("phase") != 1 or report.get("fixture_new_publications") != 1
            or report.get("production_publications") != 0
            or report.get("source_unchanged") is not True or report.get("target_unchanged") is not True
            or evidence.get("version") != "parallel-chinese-ass-v1"
            or validation.get("output_parse") != "PASS" or validation.get("hard_qc") != "PASS"
            or (validation.get("source_analysis") or {}).get("eligible") is not True
            or report.get("output_sha256") != evidence.get("normalized_sha256")):
        _fail("repair_validation_unproven")
    for role, requested in (("source", "download_identity"), ("target", "target_identity")):
        actual = (report.get("source_identity") or {}).get(role) or {}
        if any(actual.get(k) != request[requested].get(k) for k in ("sha256", "size", "mtime_ns")):
            _fail("repair_evidence_source_changed")


def _read_row(db: sqlite3.Connection, table: str, key_column: str, key: str) -> dict[str, Any] | None:
    # Table/column names are call-site constants, never request parameters.
    cursor = db.execute(f"SELECT * FROM {table} WHERE {key_column}=?", (key,))
    row = cursor.fetchone()
    return dict(zip((c[0] for c in cursor.description), row)) if row else None


def _verified_download(worker: Any, request: dict[str, Any], snapshot: dict[str, Any], target: Path) -> Any:
    from mikan_worker import (_torrent_from_request_payload, _torrent_video_paths_from_file_list,
        _select_source_videos_for_pending_episodes, _target_video_for_torrent_source)
    wanted = request["torrent_hash"]
    qbit = worker._qbit()
    response = qbit._get_with_retry(qbit.base_url + "/api/v2/torrents/info", params={"hashes": wanted}, attempts=1)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("hash") != wanted:
        _fail("download_unavailable")
    raw = rows[0]
    if (raw.get("category") != worker.config.qbit_category
            or not set(worker.config.qbit_tags).issubset({v.strip() for v in str(raw.get("tags") or "").split(",")})
            or float(raw.get("progress") or 0) < 1 or int(raw.get("amount_left") or 0) != 0):
        _fail("download_incomplete_or_foreign")
    torrent = _torrent_from_request_payload(raw)
    if torrent is None:
        _fail("download_invalid")
    files = [f for f in qbit.list_files(wanted) if f.progress >= 1 and f.size > 0]
    paths = _torrent_video_paths_from_file_list(torrent, files, worker.config)
    selection = _select_source_videos_for_pending_episodes(paths, {int(snapshot["episode"])})
    if len(selection.selected) != 1:
        _fail("download_member_ambiguous")
    source = verify_identity(worker.config, request["download_identity"])
    sizes = [f.size for f in files if source in _torrent_video_paths_from_file_list(torrent, [f], worker.config)]
    if selection.selected[0].resolve() != source or sizes != [source.stat().st_size]:
        _fail("download_member_changed")
    mappings = [m for m in worker._series_mappings(cached_only=True)
                if int(m.get("bangumi_id") or 0) == int(snapshot["bangumi_id"])]
    matched = _target_video_for_torrent_source(source, torrent, worker.config, worker.logger,
                                              mappings, pending_entries=[snapshot])
    if matched is None or matched.resolve() != target:
        _fail("target_match_unproven")
    return torrent


def request_reviewed_repair(config: Any, *, job_key: str, request: dict[str, Any]) -> dict[str, Any]:
    from mikan_worker import (MikanWorker, VideoLock, _mikan_state_existing_connect,
        _mikan_state_row, _pending_completion_key, _pending_has_active_release_fields,
        _pending_has_deferred_release_fields, _pending_is_terminal_success,
        _mikan_extract_failed_retry_seconds, _unsafe_recovered_mapping_detail,
        _sqlite_authoritative_pending_enabled, _utc_now)
    from scan_state import scan_state_path
    from subtitle_extract import verified_official_subtitle_languages
    from subtitle_paths import has_ai_finished_subtitle
    if (not isinstance(request, dict) or request.get("contract") != CONTRACT
            or not isinstance(request.get("authorization_ref"), str) or not request["authorization_ref"].strip()
            or not re.fullmatch(r"[0-9a-f]{64}", str(request.get("request_id") or ""))
            or not re.fullmatch(r"[0-9a-f]{40}", str(request.get("torrent_hash") or ""))
            or job_key != "hash:" + request["torrent_hash"]):
        _fail("request_invalid")
    if (any(not re.fullmatch(r"[0-9a-f]{64}", str(request.get(k) or ""))
            for k in ("expected_job_sha256", "expected_pending_sha256", "runtime_fingerprint"))
            or any(not isinstance(request.get(k), dict) for k in
                   ("target_identity", "download_identity", "validation_evidence", "code_attestation", "processing_identity"))):
        _fail("request_invalid")
    event_key = EVENT + ":" + job_key + ":" + digest(request["processing_identity"])
    worker = MikanWorker(config, logging.getLogger(__name__))
    if not _sqlite_authoritative_pending_enabled(worker.pending_path):
        _fail("durable_store_required")
    with ExitStack() as stack:
        for acquire in (lambda: worker._acquire_queue_lock(EVENT, required=False),
                        lambda: worker._acquire_state_lock_for_enqueue(EVENT, required=False)):
            lock = acquire()
            if lock is None:
                _fail("state_busy")
            stack.callback(lock.release)
        db = stack.enter_context(closing(_mikan_state_existing_connect(config)))
        prior_event = db.execute("SELECT detail_json FROM mikan_download_events WHERE event_key=?", (event_key,)).fetchone()
        if prior_event:
            if json.loads(prior_event[0]).get("request") != request:
                _fail("retry_budget_spent")
            return {"status": "already_recorded", "queued": 0, "job_key": job_key, "event_key": event_key}
        if runtime_identity(config) != request["runtime_fingerprint"]:
            _fail("runtime_changed")
        original = _read_row(db, "mikan_extract_jobs", "job_key", job_key)
        if original is None or digest(original) != request["expected_job_sha256"]:
            _fail("job_changed")
        failure = json.loads(original["result_json"])
        now = time.time()
        if (original["status"] != "replaced" or original["worker_id"] or float(original["lease_until"] or 0) > now
                or failure.get("failure_reason") != "subtitle_validation_failed" or failure.get("extracted_count") != 0):
            _fail("job_not_terminal_quality_failure")
        snapshots = json.loads(original["pending_entries_json"])
        if len(snapshots) != 1 or _unsafe_recovered_mapping_detail(snapshots):
            _fail("member_unproven")
        snapshot = snapshots[0]
        key = _pending_completion_key(snapshot)
        item = _read_row(db, "mikan_download_items", "key", key)
        if (not key or item is None or digest(json.loads(item["raw_json"])) != request["expected_pending_sha256"]
                or snapshot.get("info_hash") != request["torrent_hash"] or not snapshot.get("torrent_url")):
            _fail("pending_changed")
        entry = json.loads(item["raw_json"])
        if (_pending_has_active_release_fields(entry) or _pending_has_deferred_release_fields(entry)
                or _pending_is_terminal_success(entry) or entry.get("candidate_review_reason")
                or entry.get("last_failed_info_hash") != request["torrent_hash"]):
            _fail("pending_not_idle")
        if max(float(entry.get("no_candidate_until") or 0), float(entry.get("next_retry_at") or 0),
               float(original["finished_at"] or 0) + _mikan_extract_failed_retry_seconds(config)) > now:
            _fail("normal_backoff_active")
        previous_identity = (entry.get("completion_revalidation") or {}).get("source_identity") or {}
        if any(previous_identity.get(k) != request["target_identity"].get(k)
               for k in ("canonical_path", "sha256", "size", "mtime_ns")):
            _fail("reviewed_target_identity_unproven")
        target = Path(request["target_identity"]["canonical_path"])
        video_lock = VideoLock(target)
        if not video_lock.acquire():
            _fail("target_busy")
        stack.callback(video_lock.release)
        target = verify_identity(config, request["target_identity"])
        with closing(sqlite3.connect(scan_state_path(config).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as ownership:
            ownership.execute("PRAGMA query_only=ON")
            if (ownership.execute("SELECT 1 FROM ai_candidate_queue WHERE path=? AND status='running'", (str(target),)).fetchone()
                    or ownership.execute("SELECT 1 FROM ai_job_state WHERE path=? AND status='running'", (str(target),)).fetchone()):
                _fail("target_owner_active")
        if has_ai_finished_subtitle(target, config) or "zh-tw" in verified_official_subtitle_languages(target, config):
            _fail("valid_output_exists")
        _verified_download(worker, request, snapshot, target)
        verify_repair_evidence(config, request)
        restored = json.loads(item["raw_json"])
        for name in ("torrent_url", "title", "source", "source_page", "pub_date", "seeders"):
            if name in snapshot:
                restored[name] = snapshot[name]
        restored.update(info_hash=request["torrent_hash"], last_qbit_hash=request["torrent_hash"],
                        queued_at=_utc_now().isoformat(), last_progress=1.0)
        # Current failure is superseded only by this explicit recovery transition;
        # all original fields survive in the immutable event, exclusions stay active.
        for name in ("last_failure_reason", "last_extract_failed_at", "last_extract_failure_reason",
                     "last_extract_failure_detail", "last_extract_context", "last_subtitle_diagnostics"):
            restored.pop(name, None)
        binding = {"contract": CONTRACT, "event_key": event_key, "request_sha256": digest(request)}
        restored[EVENT] = binding
        claim_snapshot = {**snapshot, EVENT: binding}
        row = _mikan_state_row(key, restored, now)
        if runtime_identity(config) != request["runtime_fingerprint"]:
            _fail("runtime_changed")
        verify_identity(config, request["target_identity"])
        verify_identity(config, request["download_identity"])
        db.execute("BEGIN IMMEDIATE")
        try:
            if (_read_row(db, "mikan_extract_jobs", "job_key", job_key) != original
                    or _read_row(db, "mikan_download_items", "key", key) != item):
                _fail("handoff_changed")
            receipt = {"contract": CONTRACT, "request": request, "previous_extract_job": original,
                       "previous_pending_item": item, "restored_pending": restored,
                       "recorded_at": now, "historical_continuity": "UNKNOWN", "retry_budget": 1}
            db.execute("INSERT INTO mikan_download_events(key,bangumi_id,episode,event,detail,detail_json,event_key,last_seen_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, item["bangumi_id"], item["episode"], EVENT, "One reviewed extraction retry; no quality approval",
                        json.dumps(receipt, ensure_ascii=False, sort_keys=True), event_key, now, now))
            columns = [name for name in row if name != "key"]
            db.execute("UPDATE mikan_download_items SET " + ",".join(name + "=?" for name in columns) + " WHERE key=?",
                       [row[name] for name in columns] + [key])
            db.execute("UPDATE mikan_extract_jobs SET status='queued',priority=MAX(priority,2000000),pending_entries_json=?,updated_at=? WHERE job_key=?",
                       (json.dumps([claim_snapshot], ensure_ascii=False, sort_keys=True), now, job_key))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return {"status": "recorded", "queued": 1, "job_key": job_key, "event_key": event_key}


def guard_reviewed_extraction(config: Any, binding: dict[str, Any]) -> dict[str, Any]:
    """Recheck the persisted permit at claim and immediately before publication."""
    from mikan_worker import _mikan_state_existing_connect
    if binding.get("contract") != CONTRACT:
        _fail("claim_contract_invalid")
    with closing(_mikan_state_existing_connect(config)) as db:
        row = db.execute("SELECT detail_json FROM mikan_download_events WHERE event=? AND event_key=?",
                         (EVENT, binding.get("event_key"))).fetchone()
    if row is None:
        _fail("claim_receipt_missing")
    request = json.loads(row[0])["request"]
    if (digest(request) != binding.get("request_sha256") or request["processing_identity"] != processing_identity()
            or runtime_identity(config) != request["runtime_fingerprint"]):
        _fail("claim_runtime_or_receipt_changed")
    verify_identity(config, request["target_identity"])
    verify_identity(config, request["download_identity"])
    return {**binding, "target_identity": request["target_identity"], "download_identity": request["download_identity"],
            "runtime_fingerprint": request["runtime_fingerprint"], "verified_at": time.time()}
