from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
import time
from typing import Any, Callable
import unicodedata
from urllib.parse import urlparse
import uuid

import requests

from config import AppConfig
from control_state import (
    active_review_command_ids,
    list_review_items,
    locked_series_source_mappings,
    record_daily_sample,
    resolve_review_item_if_idle,
    update_review_source_lifecycle,
    upsert_review_item,
)
from file_times import file_time_metadata
from lock import VideoLock
from mikan_source import (
    MikanSourceDeadline,
    MikanSourceError,
    MikanRelease,
    extract_episode_number,
    extract_episode_numbers,
    extract_torrent_info_hash,
    fetch_bangumi_releases,
    has_extractable_subtitle_hint,
    release_score,
    release_episode_numbers,
    release_series_identity,
    release_season_number,
    select_preferred_release_candidates_for_episodes,
    select_preferred_releases,
    select_preferred_releases_for_episodes,
)
from mikan_fallback_sources import FallbackSourcePool
from mikan_cache_store import ensure_mikan_cache_tables
from mikan_matcher import mapping_matches_torrent, normalize_match_text, resolve_mikan_series_mappings
from notifications import notify_event
from qbit_client import QBitClient, QBitError, QBitTorrent, QBitTorrentFile, map_remote_path
from safe_files import atomic_write_text, sha256_file, verified_move
from scan_state import ScanStateStore, scan_state_path
from subtitle_extract import (
    SIDECAR_SUBTITLE_EXTENSIONS,
    SubtitleExtractCancelled,
    SubtitleExtractError,
    classify_sidecar_subtitle_language,
    extract_available_subtitles,
    normalize_sidecar_subtitles_for_output,
)


class MikanWorkerError(RuntimeError):
    pass


_UNTRACKED_PRESERVE_WARNING_INTERVAL_SECONDS = 3600.0


def mikan_redownload_in_progress(config: AppConfig) -> bool:
    return (
        _mikan_job_pending_or_running(config, "redownload_all")
        or _redownload_all_active_payload(config) is not None
        or _mikan_redownload_all_request_path(config).exists()
    )


def request_mikan_redownload_cancel(config: AppConfig) -> dict[str, Any]:
    """Request a cooperative stop without touching downloaded media or torrents."""
    active = _redownload_all_active_payload(config)
    request_path = _mikan_redownload_all_request_path(config)
    pending = request_path.exists()
    if active is None and not pending:
        raise MikanWorkerError("No active or pending Mikan redownload-all operation")

    cancel_path = _mikan_redownload_all_cancel_path(config)
    payload = {
        "action": "cancel_redownload_all",
        "requested_at": _utc_now().isoformat(),
        "updated_at": time.time(),
        "requested_by": "worker-control-command",
    }
    _save_json_atomic(cancel_path, payload)
    cancelled_pending = bool(pending and active is None)
    if cancelled_pending:
        request_path.unlink(missing_ok=True)
    return {
        "cancel_requested": active is not None,
        "cancelled_pending": cancelled_pending,
        "path": str(cancel_path),
    }


def request_mikan_extract_cancel(config: AppConfig, *, job_key: str = "") -> dict[str, Any]:
    """Request a cooperative stop of one running extraction job.

    The request is bound to the current lease owner so a stale marker can never
    cancel a later retry after a Worker restart.  Cancelling only requeues the
    extraction; downloaded media and already-published subtitles are untouched.
    """
    requested_key = str(job_key or "").strip()
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        if requested_key:
            row = conn.execute(
                """
                SELECT job_key, torrent_name, worker_id
                FROM mikan_extract_jobs
                WHERE status = 'running' AND job_key = ?
                LIMIT 1
                """,
                (requested_key,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT job_key, torrent_name, worker_id
                FROM mikan_extract_jobs
                WHERE status = 'running'
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
    finally:
        if conn is not None:
            conn.close()
    if row is None:
        raise MikanWorkerError(
            f"No running Mikan subtitle extraction job matches: {requested_key or 'latest'}"
        )

    active_key = str(row[0] or "")
    torrent_name = str(row[1] or "")
    worker_id = str(row[2] or "")
    if not active_key or not worker_id:
        raise MikanWorkerError("Running extraction job has no active lease owner")
    cancel_path = _mikan_extract_cancel_path(config)
    now = time.time()
    _save_json_atomic(
        cancel_path,
        {
            "action": "cancel_mikan_extract",
            "job_key": active_key,
            "worker_id": worker_id,
            "torrent_name": torrent_name,
            "requested_at": _utc_now().isoformat(),
            "updated_at": now,
        },
    )
    return {
        "cancel_requested": True,
        "job_key": active_key,
        "torrent_name": torrent_name,
        "path": str(cancel_path),
    }


@dataclass(frozen=True)
class MikanExtractResult:
    extracted_count: int
    failure_reason: str = ""
    failure_detail: str = ""
    subtitle_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    failure_context: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    defer_seconds: float = 0.0


@dataclass(frozen=True)
class MikanDownloadPollResult:
    synced_progress_count: int = 0
    active_download_count: int = 0
    completed_pending_count: int = 0
    claimable_extract_count: int = 0
    running_extract_count: int = 0


@dataclass(frozen=True)
class MikanReplacementTarget:
    bangumi_id: int
    episode: int


_QBIT_RECOVERY_MATCH_VERSION = "qbit-recovery-v3"
_QBIT_RECOVERY_MIN_CONFIDENCE = 0.90


@dataclass(frozen=True)
class MikanUntrackedTorrentResolution:
    targets: tuple[MikanReplacementTarget, ...] = ()
    confidence: float = 0.0
    match_version: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        return bool(
            self.targets
            and self.confidence >= _QBIT_RECOVERY_MIN_CONFIDENCE
            and self.match_version.startswith(_QBIT_RECOVERY_MATCH_VERSION)
            and self.evidence
        )


@dataclass(frozen=True)
class MikanExtractJob:
    job_key: str
    torrent: QBitTorrent
    pending_entries: list[dict[str, Any]]
    worker_id: str = ""


@dataclass(frozen=True)
class MikanExtractEnqueueSummary:
    completion_tagged: int = 0
    queued: int = 0
    active: int = 0
    skipped_processed: int = 0
    reprocess_incomplete: int = 0
    skipped_missing_local: int = 0
    skipped_no_active_pending: int = 0
    stale_completed_waiting_failed: int = 0


@dataclass(frozen=True)
class _SonarrStyleScoredCandidate:
    candidate: Path
    score: int
    reasons: tuple[str, ...]
    best_ratio: float = 0.0


@dataclass(frozen=True)
class _SonarrStyleSeriesScopeCandidate:
    root: Path
    mappings: tuple[dict[str, object], ...]
    score: int
    reason: str
    best_ratio: float = 0.0


@dataclass(frozen=True)
class MikanSourceVideoSelection:
    selected: list[Path]
    failure_reason: str = ""
    failure_detail: str = ""
    skipped_extra_videos: list[Path] = field(default_factory=list)


@dataclass
class MikanJobLease:
    name: str
    worker_id: str
    db_path: Path

    def release(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    """
                    UPDATE mikan_jobs
                    SET worker_id = '',
                        lease_until = 0,
                        updated_at = ?
                    WHERE job_name = ? AND worker_id = ?
                    """,
                    (time.time(), self.name, self.worker_id),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            return


def _coerce_mikan_extract_result(value: object) -> MikanExtractResult:
    if isinstance(value, MikanExtractResult):
        return value
    try:
        return MikanExtractResult(int(value or 0))
    except (TypeError, ValueError):
        return MikanExtractResult(0, failure_reason="extract_exception", failure_detail=f"Invalid extraction result: {value!r}")


MIKAN_OPERATION_LOCK_NAME = "mikan_worker"
MIKAN_QUEUE_LOCK_NAME = "mikan_enqueue"
MIKAN_EPISODE_INDEX_LOCK_NAME = "mikan_episode_index"
MIKAN_OPERATION_LOCK_STALE_SECONDS = 43_200.0
MIKAN_OPERATION_LOCK_WAIT_SECONDS = 300.0
MIKAN_OPERATION_LOCK_POLL_SECONDS = 2.0
MIKAN_TARGET_MISSING_RETRY_SECONDS = 900.0
MIKAN_QBIT_SYNC_HEARTBEAT_SECONDS = 10.0
MIKAN_REDOWNLOAD_ACTIVE_STALE_SECONDS = 900.0
MIKAN_RESET_ALL_REQUEST_NAME = "mikan_reset_all.request.json"
MIKAN_REDOWNLOAD_ALL_REQUEST_NAME = "mikan_redownload_all.request.json"
MIKAN_REDOWNLOAD_ALL_ACTIVE_NAME = "mikan_redownload_all.active.json"
MIKAN_REDOWNLOAD_ALL_CANCEL_NAME = "mikan_redownload_all.cancel.json"
MIKAN_EXTRACT_CANCEL_NAME = "mikan_extract_cancel.request.json"
MIKAN_COMPLETED_STATE_UPDATE_REQUEST_NAME = "mikan_completed_state_update.request.json"
MIKAN_REPLACEMENT_ENQUEUE_REQUEST_NAME = "mikan_replacement_enqueue.request.json"
MIKAN_ENQUEUE_CURSOR_NAME = "mikan_enqueue.cursor.json"
MIKAN_ENQUEUE_SLICE_SECONDS = 60.0
MIKAN_REVIEW_SOURCE_RECONCILE_SECONDS = 30.0
MIKAN_REVIEW_SOURCE_MISSING_GRACE_SECONDS = 60.0
_SQLITE_AUTHORITATIVE_PENDING_PATHS: set[Path] = set()
_SQLITE_AUTHORITATIVE_PENDING_LOCK = threading.Lock()


class MikanWorker:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.seen_path = _resolve_seen_path(config)
        self.pending_path = _resolve_pending_path(config)
        _register_sqlite_authoritative_pending(
            self.pending_path,
            enabled=bool(getattr(config, "mikan_sqlite_authoritative_state", False)),
        )
        self._resolved_series_mappings: list[dict[str, object]] | None = None
        self._episode_index_ready = False
        self._episode_index_next_check_monotonic = 0.0
        self._fallback_library_scan_next_at = 0.0
        self._fallback_library_scan_offset = 0
        self._fallback_library_scan_runs = 0
        self._fallback_library_scan_roots = 0
        self._pending_episode_repair_done = False
        self._terminal_completion_repair_done = False
        self._fallback_sources = FallbackSourcePool(config, logger)
        self._qbit_unhealthy_since: dict[str, tuple[str, float]] = {}
        self._untracked_preserve_warning_at: dict[str, float] = {}
        self._next_review_source_reconcile_at = 0.0
        # The SQLite download table is a WebUI mirror of mikan_pending.json and
        # is refreshed by _save_pending(). Rebuilding the full mirror here makes
        # every worker constructor contend on SQLite before it can poll qBittorrent;
        # startup creates enqueue and completed workers concurrently, and manual
        # completed-processing creates a third one. Core work must not wait for
        # that derived UI state to be rebuilt.

    def run_once(self, *, process_completed: bool = True) -> None:
        try:
            self._repair_terminal_completed_pending_entries()
            self._repair_invalid_release_part_pending_entries()
            self.consume_completed_state_update_request()
            if process_completed and self.config.mikan_extract_completed:
                self.process_completed_downloads(required=False)
            self.consume_replacement_enqueue_request()
            if self.consume_deferred_requests() is not None:
                return
            self.enqueue_latest_releases(required=False)
            # A request may become due while discovery is in progress.  Give
            # it a turn before the outer watcher sleeps again.
            self.consume_replacement_enqueue_request()
            self.consume_deferred_requests()
        except QBitError as exc:
            self.logger.warning("Mikan qBittorrent work skipped this cycle: %s", exc)

    def _repair_invalid_release_part_pending_entries(self) -> int:
        """Remove legacy pending rows created from movie ``Part 2`` labels.

        The episode parser now rejects these labels, but already-persisted rows
        would otherwise keep submitting the same completed movie to extraction.
        This migration is conservative: it only removes an active/deferred row
        when its stored episode is absent from current parsing and is attached
        directly to a Part/Vol/Movie token in the release title.
        """

        if self._pending_episode_repair_done:
            return 0
        lock = self._acquire_operation_lock(
            "repair_invalid_release_part_pending_entries",
            required=False,
            log_busy=False,
            log_acquired=False,
        )
        if lock is None:
            return 0
        try:
            pending = _load_pending(self.pending_path)
            items = _pending_items(pending)
            removed = [
                str(key)
                for key, entry in list(items.items())
                if isinstance(entry, dict)
                and (_pending_has_active_release(entry) or _pending_has_deferred_release(entry))
                and _pending_entry_is_release_part_false_positive(entry)
            ]
            for key in removed:
                items.pop(key, None)
            if removed:
                _save_pending(self.pending_path, pending)
                self.logger.warning(
                    "Removed invalid Mikan pending row(s) created from movie/volume numbering: keys=%s",
                    ",".join(removed),
                )
            self._pending_episode_repair_done = True
            return len(removed)
        finally:
            lock.release()

    def _repair_terminal_completed_pending_entries(self) -> int:
        """Detach legacy active releases from successfully extracted episodes.

        Older reconciliation paths could restore ``torrent_url`` and
        ``queued_at`` after extraction had already completed.  The derived UI
        state still looked completed because completion wins status precedence,
        while download polling, timeout handling and replacement selection kept
        treating the same row as active.  Repair the durable pending state first
        and only then, when every row tied to an exact qB hash is terminal,
        cooperatively stop that redundant torrent without deleting its data.
        """

        if self._terminal_completion_repair_done:
            return 0
        lock = self._acquire_operation_lock(
            "repair_terminal_completed_pending_entries",
            required=False,
            log_busy=False,
            log_acquired=False,
        )
        if lock is None:
            return 0

        repaired = 0
        stop_candidates: set[str] = set()
        try:
            pending = _load_pending(self.pending_path)
            items = _pending_items(pending)
            now = _utc_now().isoformat()
            changed = False
            for entry in items.values():
                if not isinstance(entry, dict) or not _pending_is_terminal_success(entry):
                    continue
                exact_hash = _pending_active_info_hash(entry)
                requires_cleanup = bool(
                    _pending_has_active_release_fields(entry)
                    or _pending_has_deferred_release_fields(entry)
                    or _pending_has_runtime_failure_state(entry)
                )
                pending_stop = bool(
                    re.fullmatch(r"[0-9a-f]{40}", str(entry.get("completion_state_repair_hash") or ""))
                    and not entry.get("completion_state_repair_qbit_stopped")
                )
                if not requires_cleanup and not pending_stop:
                    continue

                if requires_cleanup:
                    if _pending_has_active_release_fields(entry):
                        _archive_active_release(entry, "last_superseded")
                    if exact_hash:
                        entry["completion_state_repair_hash"] = exact_hash
                    entry["completion_state_repair_at"] = now
                    entry["completion_state_repair_reason"] = "terminal_success_reactivated"
                    entry["completion_state_repair_previous"] = {
                        "failure_reason": str(entry.get("last_failure_reason") or ""),
                        "extract_failure_reason": str(entry.get("last_extract_failure_reason") or ""),
                        "failed_count": len(entry.get("failed_urls") or [])
                        if isinstance(entry.get("failed_urls"), list)
                        else 0,
                    }
                    _clear_active_pending_release(entry)
                    _clear_deferred_release(entry)
                    _clear_no_candidate_retry(entry)
                    _clear_terminal_success_runtime_failures(entry)
                    repaired += 1
                    changed = True

                repair_hash = str(entry.get("completion_state_repair_hash") or "").casefold()
                if re.fullmatch(r"[0-9a-f]{40}", repair_hash) and not entry.get(
                    "completion_state_repair_qbit_stopped"
                ):
                    stop_candidates.add(repair_hash)

            safe_stop_hashes = {
                info_hash
                for info_hash in stop_candidates
                if not any(
                    isinstance(other, dict)
                    and not _pending_is_terminal_success(other)
                    and info_hash in _pending_entry_release_hashes(other)
                    for other in items.values()
                )
            }
            if changed:
                _save_pending(self.pending_path, pending)
        finally:
            lock.release()

        stop_results: dict[str, str] = {}
        stop_failed = False
        if safe_stop_hashes:
            try:
                qbit = self._qbit()
                torrents = {str(item.hash or "").casefold(): item for item in qbit.list_torrents() if item.hash}
                for info_hash in sorted(safe_stop_hashes):
                    torrent = torrents.get(info_hash)
                    if torrent is None:
                        stop_results[info_hash] = "not_found"
                        continue
                    qbit.add_tags(info_hash, ["mikansub-superseded"])
                    if str(torrent.state or "").casefold() not in {
                        "pauseddl",
                        "pausedup",
                        "stoppeddl",
                        "stoppedup",
                    }:
                        qbit.stop_torrents([info_hash])
                    stop_results[info_hash] = "stopped"
            except QBitError as exc:
                stop_failed = True
                self.logger.warning(
                    "Completed-state repair detached redundant pending rows but could not stop qBittorrent yet; will retry. error=%s",
                    exc,
                )

        if stop_results:
            update_lock = self._acquire_operation_lock(
                "record_terminal_completed_qbit_repair",
                required=False,
                log_busy=False,
                log_acquired=False,
            )
            if update_lock is None:
                stop_failed = True
            else:
                try:
                    pending = _load_pending(self.pending_path)
                    updated = False
                    for entry in _pending_items(pending).values():
                        if not isinstance(entry, dict):
                            continue
                        info_hash = str(entry.get("completion_state_repair_hash") or "").casefold()
                        status = stop_results.get(info_hash)
                        if not status:
                            continue
                        entry["completion_state_repair_qbit_stopped"] = True
                        entry["completion_state_repair_qbit_status"] = status
                        updated = True
                    if updated:
                        _save_pending(self.pending_path, pending)
                finally:
                    update_lock.release()

        self._terminal_completion_repair_done = not stop_failed
        if repaired:
            self.logger.warning(
                "Repaired terminal Mikan entry/entries that had been reactivated after successful extraction. count=%s safe_stopped=%s",
                repaired,
                len([value for value in stop_results.values() if value == "stopped"]),
            )
        return repaired

    def videos_with_available_official_subtitles(self) -> set[Path]:
        series_mappings = self._series_mappings()
        if not series_mappings:
            return set()

        pending = _load_pending(self.pending_path)
        pending_episodes_by_bangumi = _active_pending_episodes_by_bangumi(
            pending,
            timeout_seconds=self.config.mikan_download_start_timeout_seconds,
        )
        if not pending_episodes_by_bangumi:
            return set()

        waiting: set[Path] = set()
        for mapping in series_mappings:
            bangumi_id = int(mapping["bangumi_id"])
            pending_episodes = pending_episodes_by_bangumi.get(bangumi_id)
            if not pending_episodes:
                continue
            root = Path(str(mapping["path"]))
            if not root.exists():
                continue
            for video in _find_video_files(root, self.config.video_extensions):
                if extract_episode_number(video.name) in pending_episodes and not _target_has_required_chinese_subtitles(video):
                    waiting.add(_safe_resolve(video))

        if waiting:
            self.logger.info("Mikan official subtitle downloads are pending for %s local video(s).", len(waiting))
        return waiting

    def dry_run(self) -> int:
        series_mappings = self._series_mappings()
        library_scan_mappings = _library_scan_series_mappings(self.config, self.logger, series_mappings)
        bangumi_ids = _bangumi_ids_for_run(self.config, library_scan_mappings)
        if not bangumi_ids:
            self.logger.warning("Mikan dry-run skipped because no bangumi ids or auto-matched series are available.")
            return 0

        missing_episodes_by_bangumi = _missing_episodes_by_bangumi(self.config, self.logger, library_scan_mappings)
        selected_count = 0
        for bangumi_id in bangumi_ids:
            missing_episodes = (
                missing_episodes_by_bangumi[bangumi_id]
                if bangumi_id in missing_episodes_by_bangumi
                else None
            )
            if missing_episodes is not None and not missing_episodes:
                continue
            try:
                releases = fetch_bangumi_releases(
                    self.config.mikan_base_url,
                    bangumi_id,
                    timeout_seconds=self.config.mikan_request_timeout_seconds,
                )
            except (requests.RequestException, MikanSourceError) as exc:
                self.logger.warning("Mikan dry-run skipped bangumi_id=%s because RSS fetch failed: %s", bangumi_id, exc)
                continue
            selected = _select_releases_for_bangumi(
                releases,
                bangumi_id,
                self.config,
                self.logger,
                missing_episodes=missing_episodes,
            )
            self.logger.info("Mikan dry-run bangumi_id=%s releases=%s selected=%s", bangumi_id, len(releases), len(selected))
            for release in selected:
                selected_count += 1
                self.logger.info(
                    "Mikan dry-run selected episode=%s title=%s torrent=%s",
                    release.episode,
                    release.title,
                    release.torrent_url,
                )
        return selected_count

    def reset_all_state(self) -> dict[str, Any]:
        lock = self._acquire_operation_lock(
            "reset_all_state",
            required=True,
            wait_seconds=_mikan_operation_lock_wait_seconds(self.config),
        )
        try:
            return self._reset_all_state_unlocked()
        finally:
            lock.release()

    def _reset_all_state_unlocked(self) -> dict[str, Any]:
        seen = _load_seen(self.seen_path)
        pending = _load_pending(self.pending_path)
        pending_count = len(_pending_items(pending))
        backups = _backup_mikan_state_files(self.seen_path, self.pending_path)

        _save_seen(self.seen_path, {})
        _save_pending(self.pending_path, {"items": {}})

        result = {
            "seen_entries": len(seen),
            "pending_entries": pending_count,
            "seen_backup": backups.get("seen"),
            "pending_backup": backups.get("pending"),
        }
        self.logger.warning(
            "Mikan state reset for full requeue. seen_entries=%s pending_entries=%s seen_backup=%s pending_backup=%s",
            result["seen_entries"],
            result["pending_entries"],
            result["seen_backup"] or "-",
            result["pending_backup"] or "-",
        )
        return result

    def reset_all_state_and_enqueue(self, *, defer_if_busy: bool = False) -> dict[str, Any]:
        if defer_if_busy:
            queue_lock = self._acquire_queue_lock("reset_all_state_and_enqueue", required=False, log_busy=False)
            if queue_lock is None:
                return self.request_reset_all(
                    reason="Mikan queue operation already running; reset-all will run after the current operation finishes."
                )
            state_lock = self._acquire_operation_lock("reset_all_state_and_enqueue", required=False, log_busy=False)
            if state_lock is None:
                queue_lock.release()
                return self.request_reset_all(
                    reason="Mikan state operation already running; reset-all will run after the current operation finishes."
                )
        else:
            queue_lock = self._acquire_queue_lock(
                "reset_all_state_and_enqueue",
                required=True,
                wait_seconds=_mikan_operation_lock_wait_seconds(self.config),
            )
        try:
            if not defer_if_busy:
                state_lock = self._acquire_operation_lock(
                    "reset_all_state_and_enqueue",
                    required=True,
                    wait_seconds=_mikan_operation_lock_wait_seconds(self.config),
                )
            try:
                reset = self._reset_all_state_unlocked()
            finally:
                state_lock.release()
            # The queue lock protects the destructive reset boundary, not the
            # entire network/library scan. Each qB add below acquires the lock
            # briefly, allowing progress sync, extraction, and stale-torrent
            # replacement to continue during a large reset-all run.
            queue_lock.release()
            queue_lock = None
            queued = self._enqueue_latest_releases_unlocked(
                state_required=not defer_if_busy,
                queue_lock_held=False,
            )
            return {"reset": reset, "queued": queued, "deferred": False}
        finally:
            if queue_lock is not None:
                queue_lock.release()

    def request_reset_all(self, *, reason: str = "") -> dict[str, Any]:
        request_path = _mikan_reset_all_request_path(self.config)
        existing = _load_reset_all_request(request_path)
        request_count = int(existing.get("request_count", 0) or 0) + 1
        payload = {
            "action": "reset_all_state_and_enqueue",
            "requested_at": _utc_now().isoformat(),
            "request_count": request_count,
            "reason": reason,
        }
        _save_json_atomic(request_path, payload)
        self.logger.warning(
            "Mikan reset-all deferred until current operation finishes. request_path=%s request_count=%s reason=%s",
            request_path,
            request_count,
            reason or "-",
        )
        return {
            "deferred": True,
            "queued": 0,
            "reset": None,
            "request_path": str(request_path),
            "request_count": request_count,
            "reason": reason,
        }

    def redownload_all_torrents_and_enqueue(
        self,
        *,
        defer_if_busy: bool = False,
        delete_files: bool = False,
    ) -> dict[str, Any]:
        active = _redownload_all_active_payload(self.config)
        if active is not None:
            active_path = _mikan_redownload_all_active_path(self.config)
            request_path = _mikan_redownload_all_request_path(self.config)
            existing = _load_request_file(request_path)
            self.logger.warning(
                "Mikan redownload-all already running; duplicate request ignored. active_path=%s request_path=%s delete_files=%s",
                active_path,
                request_path,
                bool(active.get("delete_files", delete_files)),
            )
            return {
                "already_running": True,
                "deferred": False,
                "queued": 0,
                "reset": None,
                "deleted_torrents": int(active.get("deleted_torrents") or 0),
                "delete_files": bool(active.get("delete_files", delete_files)),
                "active_path": str(active_path),
                "request_path": str(request_path),
                "request_count": int(existing.get("request_count", 0) or 0),
                "reason": "Mikan redownload-all is already running; duplicate request ignored.",
            }

        request = self.request_redownload_all(
            delete_files=delete_files,
            reason="redownload requested; waiting for Mikan locks before qB delete.",
            log_deferred=False,
        )
        result = self._run_redownload_all_with_active_marker(
            delete_files=delete_files,
            state_required=not defer_if_busy,
        )
        result.setdefault("request_path", request.get("request_path"))
        result.setdefault("request_count", request.get("request_count"))
        return result

    def request_redownload_all(
        self,
        *,
        delete_files: bool = False,
        reason: str = "",
        log_deferred: bool = True,
    ) -> dict[str, Any]:
        request_path = _mikan_redownload_all_request_path(self.config)
        active = _redownload_all_active_payload(self.config)
        if active is not None:
            existing = _load_request_file(request_path)
            request_count = int(existing.get("request_count", 0) or 0)
            active_path = _mikan_redownload_all_active_path(self.config)
            self.logger.warning(
                "Mikan redownload-all already running; duplicate request ignored. active_path=%s request_path=%s delete_files=%s reason=%s",
                active_path,
                request_path,
                bool(delete_files),
                reason or "-",
            )
            return {
                "already_running": True,
                "deferred": False,
                "queued": 0,
                "reset": None,
                "deleted_torrents": 0,
                "delete_files": bool(active.get("delete_files", delete_files)),
                "active_path": str(active_path),
                "request_path": str(request_path),
                "request_count": request_count,
                "reason": reason or "Mikan redownload-all is already running; duplicate request ignored.",
            }
        _mikan_redownload_all_cancel_path(self.config).unlink(missing_ok=True)
        existing = _load_request_file(request_path)
        request_count = int(existing.get("request_count", 0) or 0) + 1
        payload = {
            **existing,
            "action": "redownload_all_torrents_and_enqueue",
            "requested_at": _utc_now().isoformat(),
            "request_count": request_count,
            "delete_files": bool(delete_files),
            "reason": reason,
        }
        _request_mikan_job(self.config, "redownload_all", payload)
        _save_json_atomic(request_path, payload)
        if log_deferred:
            self.logger.warning(
                "Mikan redownload-all deferred until current operation finishes. request_path=%s request_count=%s delete_files=%s reason=%s",
                request_path,
                request_count,
                bool(delete_files),
                reason or "-",
            )
        return {
            "deferred": True,
            "queued": 0,
            "reset": None,
            "deleted_torrents": 0,
            "delete_files": bool(delete_files),
            "request_path": str(request_path),
            "request_count": request_count,
            "reason": reason,
        }

    def request_completed_state_update(
        self,
        extraction_results: list[tuple[QBitTorrent, MikanExtractResult]],
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        request_path = _mikan_completed_state_update_request_path(self.config)
        existing = _load_request_file(request_path)
        existing_records = existing.get("records")
        if not isinstance(existing_records, list):
            existing_records = []
        records = [
            {
                "torrent": _torrent_request_payload(torrent),
                "result": _extract_result_request_payload(result),
            }
            for torrent, result in extraction_results
        ]
        request_count = int(existing.get("request_count", 0) or 0) + 1
        payload = {
            "action": "process_completed_downloads_state_update",
            "requested_at": _utc_now().isoformat(),
            "request_count": request_count,
            "reason": reason,
            "records": [*existing_records, *records],
        }
        _save_json_atomic(request_path, payload)
        return {
            "deferred": True,
            "request_path": str(request_path),
            "request_count": request_count,
            "record_count": len(payload["records"]),
            "reason": reason,
        }

    def request_replacement_enqueue(
        self,
        targets: list[MikanReplacementTarget],
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        request_path = _mikan_replacement_enqueue_request_path(self.config)
        existing = _load_request_file(request_path)
        existing_targets = existing.get("targets")
        merged: list[MikanReplacementTarget] = []
        if isinstance(existing_targets, list):
            for target in existing_targets:
                if not isinstance(target, dict):
                    continue
                try:
                    merged.append(MikanReplacementTarget(int(target["bangumi_id"]), int(target["episode"])))
                except (KeyError, TypeError, ValueError):
                    continue
        merged.extend(targets)
        merged = _unique_replacement_targets(merged)
        request_count = int(existing.get("request_count", 0) or 0) + 1
        payload = {
            **existing,
            "action": "enqueue_replacements_after_extract_failure",
            "requested_at": _utc_now().isoformat(),
            "request_count": request_count,
            "reason": reason,
            "targets": [
                {"bangumi_id": target.bangumi_id, "episode": target.episode}
                for target in merged
            ],
        }
        _save_json_atomic(request_path, payload)
        self.logger.warning(
            "Mikan replacement enqueue deferred. request_path=%s targets=%s reason=%s",
            request_path,
            _format_replacement_targets(merged),
            reason or "-",
        )
        return {
            "deferred": True,
            "request_path": str(request_path),
            "request_count": request_count,
            "targets": payload["targets"],
            "reason": reason,
        }

    def _redownload_all_requested(self) -> bool:
        return _mikan_redownload_all_request_path(self.config).exists()

    def _redownload_all_cancel_requested(self) -> bool:
        return _mikan_redownload_all_cancel_path(self.config).exists()

    def consume_deferred_requests(self) -> dict[str, Any] | None:
        for consumer in (
            self.consume_completed_state_update_request,
            self.consume_replacement_enqueue_request,
            self.consume_redownload_all_request,
            self.consume_reset_all_request,
        ):
            result = consumer()
            if result is not None:
                return result
        return None

    def consume_redownload_all_request(self) -> dict[str, Any] | None:
        request_path = _mikan_redownload_all_request_path(self.config)
        if not request_path.exists():
            return None
        active = _redownload_all_active_payload(self.config)
        if active is not None:
            self.logger.info("Deferred Mikan redownload-all request is already active: %s", request_path)
            return {"already_running": True, "deferred": False, "request_path": str(request_path)}
        request = _load_request_file(request_path)
        self.logger.warning("Running deferred Mikan redownload-all request. request=%s", request)
        result = self._run_redownload_all_with_active_marker(
            delete_files=bool(request.get("delete_files", False)),
            state_required=False,
        )
        if result.get("deferred"):
            return result
        request_path.unlink(missing_ok=True)
        result["request"] = request
        self.logger.warning(
            "Deferred Mikan redownload-all complete. deleted_torrents=%s queued=%s reset=%s",
            result["deleted_torrents"],
            result["queued"],
            result["reset"],
        )
        return result

    def consume_reset_all_request(self) -> dict[str, Any] | None:
        request_path = _mikan_reset_all_request_path(self.config)
        if not request_path.exists():
            return None
        queue_lock = self._acquire_queue_lock("consume_reset_all_request", required=False)
        if queue_lock is None:
            self.logger.info("Deferred Mikan reset-all request is waiting for the queue lock: %s", request_path)
            return {"deferred": True, "request_path": str(request_path)}
        try:
            state_lock = self._acquire_operation_lock("consume_reset_all_request_state_reset", required=False)
            if state_lock is None:
                self.logger.info("Deferred Mikan reset-all request is waiting for the state lock: %s", request_path)
                return {"deferred": True, "request_path": str(request_path)}
            try:
                request = _load_reset_all_request(request_path)
                self.logger.warning("Running deferred Mikan reset-all request. request=%s", request)
                reset = self._reset_all_state_unlocked()
            finally:
                state_lock.release()
            queue_lock.release()
            queue_lock = None
            queued = self._enqueue_latest_releases_unlocked(state_required=False, queue_lock_held=False)
            request_path.unlink(missing_ok=True)
            result = {"deferred": False, "request": request, "reset": reset, "queued": queued}
            self.logger.warning("Deferred Mikan reset-all complete. queued=%s reset=%s", queued, reset)
            return result
        finally:
            if queue_lock is not None:
                queue_lock.release()

    def consume_completed_state_update_request(self) -> dict[str, Any] | None:
        request_path = _mikan_completed_state_update_request_path(self.config)
        if not request_path.exists():
            return None
        lock = self._acquire_operation_lock("consume_completed_state_update_request", required=False)
        if lock is None:
            self.logger.info("Deferred Mikan completed state update is waiting for the state lock: %s", request_path)
            return {"deferred": True, "request_path": str(request_path)}
        try:
            result = self._consume_completed_state_update_request_unlocked()
        finally:
            lock.release()

        targets = result.get("replacement_targets", []) if isinstance(result, dict) else []
        if targets:
            replacement_result = self.request_replacement_enqueue(
                [
                    MikanReplacementTarget(int(target["bangumi_id"]), int(target["episode"]))
                    for target in targets
                    if isinstance(target, dict)
                ],
                reason=(
                    "Completed-download state update produced replacement targets; "
                    "replacement lookup is delegated so subtitle extraction remains responsive."
                ),
            )
            result["replacement_deferred"] = True
            result["replacement_request_path"] = replacement_result.get("request_path")
        return result

    def consume_replacement_enqueue_request(self) -> dict[str, Any] | None:
        request_path = _mikan_replacement_enqueue_request_path(self.config)
        if not request_path.exists():
            return None
        request = _load_request_file(request_path)
        if float(request.get("next_retry_at") or 0) > time.time():
            return None  # Backoff for history must not starve unrelated enqueue work.
        raw_targets = request.get("targets")
        targets: list[MikanReplacementTarget] = []
        if isinstance(raw_targets, list):
            for target in raw_targets:
                if not isinstance(target, dict):
                    continue
                try:
                    targets.append(MikanReplacementTarget(int(target["bangumi_id"]), int(target["episode"])))
                except (KeyError, TypeError, ValueError):
                    continue
        # Preserve the durable round-robin order; the general-purpose helper
        # sorts by series and would move a yielded large series back to front.
        targets = list({(target.bangumi_id, target.episode): target for target in targets}.values())
        if not targets:
            request_path.unlink(missing_ok=True)
            return {"deferred": False, "request": request, "queued": 0, "targets": []}

        # Drain a bounded part of one series per watch interval; the remainder stays in the
        # existing durable replacement request across restarts.
        selected_bangumi = targets[0].bangumi_id
        batch = [target for target in targets if target.bangumi_id == selected_bangumi][
            :max(1, int(getattr(self.config, "mikan_max_items_per_bangumi", 1) or 1))
        ]

        lock = self._acquire_queue_lock(
            "consume_replacement_enqueue_request",
            required=False,
            log_busy=False,
        )
        if lock is None:
            self.logger.info("Deferred Mikan replacement enqueue is waiting for the queue lock: %s", request_path)
            return {"deferred": True, "request_path": str(request_path), "targets": request.get("targets", [])}
        try:
            qbit = self._qbit()
            deadline = time.monotonic() + min(
                MIKAN_ENQUEUE_SLICE_SECONDS,
                max(1.0, float(getattr(self.config, "mikan_watch_interval_seconds", 300) or 300)),
            )
            self._fallback_sources.begin_cycle(deadline_monotonic=deadline)
            queued = self._enqueue_replacements_after_extract_failure_unlocked(
                batch,
                qbit,
                queue_lock_held=True,
                deadline_monotonic=deadline,
            )
        except MikanSourceDeadline:
            latest = _load_request_file(request_path)
            current = latest.get("targets", [])
            # Local preemption must not consume failure budget or drop jobs.
            # Rotate this series so another due source gets the next turn.
            rotated = [item for item in current if int(item["bangumi_id"]) != selected_bangumi]
            rotated.extend(item for item in current if int(item["bangumi_id"]) == selected_bangumi)
            _save_json_atomic(request_path, {**latest, "targets": rotated,
                "next_retry_at": time.time() + max(1, int(getattr(self.config, "mikan_watch_interval_seconds", 300) or 300)),
                "yield_reason": "elapsed_budget_exhausted"})
            self.logger.info("Mikan replacement discovery yielded without consuming retries. bangumi_id=%s targets=%s", selected_bangumi, len(batch))
            return {"deferred": True, "request_path": str(request_path), "yield_reason": "elapsed_budget_exhausted"}
        except (QBitError, requests.RequestException, MikanSourceError) as exc:
            latest = _load_request_file(request_path)
            attempts = int(latest.get("retry_attempts") or 0) + 1
            delay = min(
                int(getattr(self.config, "mikan_no_candidate_retry_max_seconds", 86400) or 86400),
                max(1, int(getattr(self.config, "mikan_watch_interval_seconds", 300) or 300)) * 2 ** min(attempts - 1, 8),
            )
            _save_json_atomic(request_path, {**latest, "retry_attempts": attempts,
                "next_retry_at": time.time() + delay, "last_error": str(exc)[:500]})
            self.logger.warning("Deferred Mikan replacement enqueue will retry: targets=%s error=%s", _format_replacement_targets(targets), exc)
            return {"deferred": True, "request_path": str(request_path), "targets": request.get("targets", [])}
        finally:
            lock.release()

        latest = _load_request_file(request_path)
        done = {(target.bangumi_id, target.episode) for target in batch}
        remaining = [target for target in latest.get("targets", [])
                     if (int(target["bangumi_id"]), int(target["episode"])) not in done]
        remaining = [target for target in remaining if int(target["bangumi_id"]) != selected_bangumi] + [
            target for target in remaining if int(target["bangumi_id"]) == selected_bangumi
        ]
        if remaining:
            _save_json_atomic(request_path, {**latest, "targets": remaining, "retry_attempts": 0,
                "next_retry_at": time.time() + max(1, int(getattr(self.config, "mikan_watch_interval_seconds", 300) or 300))})
        else:
            request_path.unlink(missing_ok=True)
        self.logger.warning(
            "Deferred Mikan replacement enqueue complete. targets=%s queued=%s",
            _format_replacement_targets(targets),
            queued,
        )
        return {"deferred": False, "request": request, "queued": queued, "targets": request.get("targets", [])}

    def _redownload_all_torrents_and_enqueue_unlocked(
        self,
        *,
        delete_files: bool = False,
        state_required: bool = True,
        state_lock: VideoLock | None = None,
    ) -> dict[str, Any]:
        request_path = _mikan_redownload_all_request_path(self.config)
        request = _load_request_file(request_path)
        deleted_torrents = int(request.get("deleted_torrents") or 0)
        if self._redownload_all_cancel_requested():
            _update_redownload_all_active(self.config, stage="cancelled", stage_label="已取消")
            request_path.unlink(missing_ok=True)
            return {
                "deferred": False,
                "cancelled": True,
                "qbit_deleted": bool(request.get("qbit_deleted_at")),
                "deleted_torrents": deleted_torrents,
                "delete_files": bool(delete_files),
                "queued": 0,
                "reset": request.get("reset"),
            }
        queue_lock: VideoLock | None = None
        if not request.get("qbit_deleted_at"):
            if not request.get("state_reset_at") and state_lock is None:
                state_lock = self._acquire_state_lock_for_enqueue(
                    "redownload_all_torrents_and_enqueue_pre_delete_state",
                    required=state_required,
                    log_busy=state_required,
                )
                if state_lock is None:
                    reason = "Mikan state operation already running; qB torrents were not deleted and redownload-all will run later."
                    _update_redownload_all_active(self.config, stage="waiting_state", stage_label="waiting for state lock")
                    _update_request_file(request_path, reason=reason)
                    return {
                        "deferred": True,
                        "qbit_deleted": False,
                        "deleted_torrents": 0,
                        "delete_files": bool(delete_files),
                        "queued": 0,
                        "reset": None,
                        "request_path": str(request_path),
                        "reason": reason,
                    }

            queue_lock = self._acquire_queue_lock(
                "redownload_all_torrents_and_enqueue_pre_delete_queue",
                required=state_required,
                wait_seconds=_mikan_operation_lock_wait_seconds(self.config) if state_required else 0.0,
                log_busy=state_required,
            )
            if queue_lock is None:
                if state_lock is not None:
                    state_lock.release()
                    state_lock = None
                reason = "Mikan queue operation already running; qB torrents were not deleted and redownload-all will run later."
                _update_redownload_all_active(self.config, stage="waiting_queue", stage_label="waiting for queue lock")
                _update_request_file(request_path, reason=reason)
                return {
                    "deferred": True,
                    "qbit_deleted": False,
                    "deleted_torrents": 0,
                    "delete_files": bool(delete_files),
                    "queued": 0,
                    "reset": None,
                    "request_path": str(request_path),
                    "reason": reason,
                }

        if not request.get("qbit_deleted_at"):
            _update_redownload_all_active(self.config, stage="delete_qbit", stage_label="刪除舊 qB 任務")
            try:
                deleted_torrents = self._delete_managed_qbit_torrents(delete_files=delete_files)
            except Exception:
                if state_lock is not None:
                    state_lock.release()
                    state_lock = None
                if queue_lock is not None:
                    queue_lock.release()
                    queue_lock = None
                raise
            _update_request_file(
                request_path,
                qbit_deleted_at=_utc_now().isoformat(),
                deleted_torrents=deleted_torrents,
                delete_files=bool(delete_files),
            )
        _update_redownload_all_active(
            self.config,
            stage="reset_state",
            stage_label="重置 Mikan 狀態",
            deleted_torrents=deleted_torrents,
        )

        reset = request.get("reset")
        if not request.get("state_reset_at"):
            if state_lock is None:
                state_lock = self._acquire_state_lock_for_enqueue(
                    "redownload_all_torrents_and_enqueue_state_reset",
                    required=state_required,
                    log_busy=state_required,
                )
                if state_lock is None:
                    reason = "Mikan state operation already running; qB torrents were already deleted and state reset will run later."
                    _update_redownload_all_active(self.config, stage="waiting_state", stage_label="等待狀態重置")
                    _update_request_file(request_path, reason=reason)
                    return {
                        "deferred": True,
                        "qbit_deleted": True,
                        "deleted_torrents": deleted_torrents,
                        "delete_files": bool(delete_files),
                        "queued": 0,
                        "reset": None,
                        "request_path": str(request_path),
                        "reason": reason,
                    }

            try:
                reset = self._reset_all_state_unlocked()
                _update_request_file(
                    request_path,
                    state_reset_at=_utc_now().isoformat(),
                    reset=reset,
                )
            except Exception:
                if queue_lock is not None:
                    queue_lock.release()
                    queue_lock = None
                raise
            finally:
                state_lock.release()
                state_lock = None
        _update_redownload_all_active(
            self.config,
            stage="enqueue_prepare",
            stage_label="準備重新排種",
            reset=reset,
        )
        if queue_lock is not None:
            queue_lock.release()
            queue_lock = None
        if self._redownload_all_cancel_requested():
            _update_redownload_all_active(
                self.config,
                stage="cancelled",
                stage_label="已取消",
                deleted_torrents=deleted_torrents,
                reset=reset,
            )
            request_path.unlink(missing_ok=True)
            return {
                "deferred": False,
                "cancelled": True,
                "qbit_deleted": bool(request.get("qbit_deleted_at")),
                "deleted_torrents": deleted_torrents,
                "delete_files": bool(delete_files),
                "queued": 0,
                "reset": reset,
            }
        queued = self._enqueue_latest_releases_unlocked(
            state_required=state_required,
            allow_redownload_preempt=False,
            redownload_progress=True,
            queue_lock_held=False,
        )
        cancelled = self._redownload_all_cancel_requested()
        request_path.unlink(missing_ok=True)
        result = {
            "deferred": False,
            "cancelled": cancelled,
            "qbit_deleted": True,
            "deleted_torrents": deleted_torrents,
            "delete_files": bool(delete_files),
            "reset": reset,
            "queued": queued,
        }
        if cancelled:
            self.logger.warning(
                "Mikan redownload-all stopped by operator request. deleted_torrents=%s queued=%s",
                result["deleted_torrents"],
                queued,
            )
        else:
            self.logger.warning(
                "Mikan redownload-all complete. deleted_torrents=%s delete_files=%s queued=%s reset=%s",
                result["deleted_torrents"],
                result["delete_files"],
                queued,
                reset,
            )
        return result

    def _run_redownload_all_with_active_marker(
        self,
        *,
        delete_files: bool,
        state_required: bool,
        state_lock: VideoLock | None = None,
    ) -> dict[str, Any]:
        lease = _claim_mikan_job(
            self.config,
            "redownload_all",
            payload={"delete_files": bool(delete_files)},
            lease_seconds=max(300.0, _mikan_operation_lock_wait_seconds(self.config) + 300.0),
        )
        if lease is None:
            active_path = _mikan_redownload_all_active_path(self.config)
            request_path = _mikan_redownload_all_request_path(self.config)
            active = _mikan_job_payload(self.config, "redownload_all") or {}
            return {
                "already_running": True,
                "deferred": False,
                "queued": 0,
                "reset": None,
                "deleted_torrents": int(active.get("deleted_torrents") or 0),
                "delete_files": bool(active.get("delete_files", delete_files)),
                "active_path": str(active_path),
                "request_path": str(request_path),
                "reason": "Mikan redownload-all is already running; duplicate request ignored.",
            }
        _write_redownload_all_active(self.config, delete_files=delete_files)
        try:
            result = self._redownload_all_torrents_and_enqueue_unlocked(
                delete_files=delete_files,
                state_required=state_required,
                state_lock=state_lock,
            )
            if result.get("deferred"):
                _defer_mikan_job(self.config, lease, result)
            else:
                _finish_mikan_job(self.config, lease, result)
            return result
        except Exception as exc:
            _fail_mikan_job(self.config, lease, str(exc))
            raise
        finally:
            _clear_redownload_all_active(self.config)
            _mikan_redownload_all_cancel_path(self.config).unlink(missing_ok=True)

    def _delete_managed_qbit_torrents(self, *, delete_files: bool) -> int:
        qbit = self._qbit()
        primary_tag = self.config.qbit_tags[0] if self.config.qbit_tags else None
        torrents = qbit.list_torrents(tag=primary_tag, category=self.config.qbit_category)
        processed_tags = list(getattr(self.config, "mikan_processed_tags", []) or [])
        protected = [
            torrent
            for torrent in torrents
            if _is_completed(torrent) and not _has_any_tag(torrent, processed_tags)
        ]
        hashes = [
            torrent.hash
            for torrent in torrents
            if torrent.hash and torrent not in protected
        ]
        if hashes:
            qbit.delete_torrents(hashes, delete_files=delete_files)
        self.logger.warning(
            "Mikan download module deleted managed qBittorrent torrents. deleted_torrents=%s protected_completed_unprocessed=%s delete_files=%s",
            len(hashes),
            len(protected),
            bool(delete_files),
        )
        return len(hashes)

    def enqueue_latest_releases(self, *, required: bool = True) -> int:
        return self._enqueue_latest_releases_unlocked(state_required=required)

    def _enqueue_series_slice(self, bangumi_ids: list[int], *, deadline: float):
        """Resume a bounded discovery sweep without restarting at series one.

        The cursor is saved *before* yielding a series and advanced only after
        its caller finishes.  An exception, cancellation, or interrupted process
        therefore retries that series through the existing idempotent enqueue
        path.  This is scheduling evidence, not a second job/claim store.
        """
        cursor_path = self.config.work_path / MIKAN_ENQUEUE_CURSOR_NAME
        cursor = _load_request_file(cursor_path)
        next_id = cursor.get("next_bangumi_id")
        start = bangumi_ids.index(next_id) if next_id in bangumi_ids else 0
        ordered = bangumi_ids[start:] + bangumi_ids[:start]
        processed = 0
        for index, bangumi_id in enumerate(ordered):
            request = _load_request_file(_mikan_replacement_enqueue_request_path(self.config))
            recovery_due = bool(request.get("targets")) and float(request.get("next_retry_at") or 0) <= time.time()
            reason = ""
            if time.monotonic() >= deadline:
                reason = "elapsed_budget_exhausted"
            elif processed and recovery_due:
                reason = "due_recovery_request"
            _save_json_atomic(cursor_path, {
                "schema_version": 1,
                "next_bangumi_id": bangumi_id,
                "last_completed_bangumi_id": cursor.get("last_completed_bangumi_id"),
                "updated_at": _utc_now().isoformat(),
                "yield_reason": reason or "series_in_progress",
                "processed_this_slice": processed,
            })
            if reason:
                self.logger.info("Mikan discovery yielded. reason=%s processed=%s next_bangumi_id=%s", reason, processed, bangumi_id)
                return
            yield bangumi_id
            processed += 1
            cursor["last_completed_bangumi_id"] = bangumi_id
            _save_json_atomic(cursor_path, {
                "schema_version": 1,
                "next_bangumi_id": ordered[(index + 1) % len(ordered)],
                "last_completed_bangumi_id": bangumi_id,
                "updated_at": _utc_now().isoformat(),
                "yield_reason": "sweep_complete" if processed == len(ordered) else "series_complete",
                "processed_this_slice": processed,
            })

    def _enqueue_latest_releases_unlocked(
        self,
        *,
        state_required: bool = True,
        allow_redownload_preempt: bool = True,
        redownload_progress: bool = False,
        queue_lock_held: bool = False,
    ) -> int:
        deadline = None if redownload_progress else time.monotonic() + min(
            MIKAN_ENQUEUE_SLICE_SECONDS,
            max(1.0, float(getattr(self.config, "mikan_watch_interval_seconds", 300) or 300)),
        )
        self._fallback_sources.begin_cycle(deadline_monotonic=deadline)
        def stop_for_cancel(queued_count: int = 0, deferred_count: int = 0) -> bool:
            if not redownload_progress or not self._redownload_all_cancel_requested():
                return False
            _update_redownload_all_active(
                self.config,
                stage="cancelled",
                stage_label="已取消",
                queued=queued_count,
                deferred=deferred_count,
            )
            self.logger.warning(
                "Mikan redownload-all cancellation acknowledged. queued=%s deferred=%s",
                queued_count,
                deferred_count,
            )
            return True

        if stop_for_cancel():
            return 0
        if not self.config.mikan_enabled:
            self.logger.info("Mikan sync skipped because mikan_enabled=false.")
            if redownload_progress:
                _update_redownload_all_active(self.config, stage="skipped", stage_label="Mikan 未啟用")
            return 0
        qbit: QBitClient | None = None
        queued = 0
        if redownload_progress:
            _update_redownload_all_active(
                self.config,
                stage="connect_qbit",
                stage_label="連線 qBittorrent",
                total=0,
                current=0,
            )
        try:
            qbit = self._qbit()
            qbit.ensure_category(self.config.qbit_category, save_path=self.config.qbit_save_path)
        except QBitError as exc:
            self.logger.warning(
                "Mikan qBittorrent unavailable; storing selected releases for later queue and allowing AI fallback: %s",
                exc,
            )
        prepared = self._prepare_enqueue_state(
            qbit,
            state_required=state_required,
            queue_lock_held=queue_lock_held,
        )
        if prepared is None:
            self.logger.info("Mikan enqueue skipped because state lock is busy.")
            if redownload_progress:
                _update_redownload_all_active(self.config, stage="waiting_state", stage_label="等待狀態鎖")
            return 0
        pending, seen, queued, stalled_targets = prepared
        if qbit is not None and stalled_targets:
            try:
                replacement_queued = self._enqueue_replacements_after_extract_failure_unlocked(
                    stalled_targets,
                    qbit,
                    queue_lock_held=queue_lock_held,
                    deadline_monotonic=deadline,
                )
            except MikanSourceDeadline:
                self.request_replacement_enqueue(stalled_targets, reason="Stalled download replacement reached discovery scheduling deadline.")
                self.logger.info("Mikan stalled replacement yielded to durable recovery request. targets=%s", len(stalled_targets))
                return queued
            queued += replacement_queued
            self.logger.warning(
                "Mikan stalled downloads switched to replacement candidates immediately. targets=%s queued=%s",
                _format_replacement_targets(stalled_targets),
                replacement_queued,
            )
            refreshed = self._snapshot_enqueue_state("enqueue_latest_releases_after_stalled_replacements", state_required=state_required)
            if refreshed is None:
                return queued
            pending, seen = refreshed
        if redownload_progress:
            _update_redownload_all_active(self.config, stage="resolve_series", stage_label="整理番劇對應")
        # A slow unmatched series must not spend the entire discovery turn.
        # Reserve half the remaining slice for already-mapped download work;
        # partial cold lookups resume from the same durable matcher cache.
        mapping_started = time.monotonic()
        mapping_deadline = None if deadline is None else mapping_started + max(0.0, deadline - mapping_started) / 2.0
        series_mappings = self._series_mappings(cached_only=redownload_progress) if deadline is None else self._series_mappings(
            deadline_monotonic=mapping_deadline,
        )
        if redownload_progress and not series_mappings:
            self.logger.warning("Mikan cached series mappings are empty during redownload; falling back to full local series discovery.")
            series_mappings = self._series_mappings()
        library_scan_mappings = _library_scan_series_mappings(self.config, self.logger, series_mappings)
        if deadline is not None and time.monotonic() >= deadline:
            self.logger.info("Mikan discovery yielded during mapping preparation; partial matcher cache retained.")
            return queued
        library_scan_mappings, episode_index_ready = self._library_scan_plan(library_scan_mappings) if deadline is None else self._library_scan_plan(
            library_scan_mappings, deadline_monotonic=deadline,
        )
        bangumi_ids = _bangumi_ids_for_run(self.config, library_scan_mappings)
        if not bangumi_ids:
            self.logger.warning("Mikan sync skipped because no bangumi ids or auto-matched series are available.")
            if redownload_progress:
                _update_redownload_all_active(self.config, stage="skipped", stage_label="沒有可重排番劇")
            return queued

        if redownload_progress:
            _update_redownload_all_active(
                self.config,
                stage="scan_missing",
                stage_label="掃描缺少官方字幕的集數",
                total=len(library_scan_mappings),
                current=0,
            )
        deferred = 0
        mappings_by_bangumi = _series_mappings_by_bangumi(library_scan_mappings)
        total_bangumi = len(bangumi_ids)
        # Manual redownload-all retains its explicit progress/cancel lifecycle.
        # Normal discovery must yield so due durable recovery work can run.
        series_to_visit = bangumi_ids if deadline is None else self._enqueue_series_slice(bangumi_ids, deadline=deadline)
        for index, bangumi_id in enumerate(series_to_visit, start=1):
            if stop_for_cancel(queued, deferred):
                return queued
            if allow_redownload_preempt and self._redownload_all_requested():
                self.logger.warning(
                    "Mikan enqueue preempted by pending redownload-all request. queued=%s deferred=%s",
                    queued,
                    deferred,
                )
                return queued

            bangumi_mappings = mappings_by_bangumi.get(bangumi_id, [])
            if redownload_progress:
                self.logger.info(
                    "Mikan redownload enqueue progress stage=fetch_releases current=%s total=%s bangumi_id=%s queued=%s deferred=%s",
                    index,
                    total_bangumi,
                    bangumi_id,
                    queued,
                    deferred,
                )
                _update_redownload_all_active(
                    self.config,
                    stage="fetch_releases",
                    stage_label="查詢 Mikan RSS 並加種",
                    current=index,
                    total=total_bangumi,
                    bangumi_id=bangumi_id,
                    queued=queued,
                    deferred=deferred,
                )
            primary_lookup_succeeded = True
            known_retry_episodes: set[int] = set()
            try:
                releases = fetch_bangumi_releases(
                    self.config.mikan_base_url,
                    bangumi_id,
                    timeout_seconds=self.config.mikan_request_timeout_seconds if deadline is None else max(
                        0.001, min(self.config.mikan_request_timeout_seconds, deadline - time.monotonic())
                    ),
                    deadline_monotonic=deadline,
                )
            except MikanSourceDeadline:
                self.logger.info("Mikan discovery yielded during RSS lookup; current series will resume. bangumi_id=%s", bangumi_id)
                return queued
            except (requests.RequestException, MikanSourceError) as exc:
                primary_lookup_succeeded = False
                releases = []
                known_retry_episodes = _known_retry_episodes_for_bangumi(
                    pending,
                    bangumi_id,
                )
                if not known_retry_episodes or not bangumi_mappings:
                    self.logger.warning(
                        "Mikan discovery deferred because RSS fetch failed and no due known-episode retry can be scoped. "
                        "bangumi_id=%s known_retry_episodes=%s error=%s",
                        bangumi_id,
                        _format_episode_list(known_retry_episodes),
                        exc,
                    )
                    continue
                self.logger.warning(
                    "Mikan RSS discovery failed; retrying only durable known episodes through fallback sources. "
                    "bangumi_id=%s episodes=%s error=%s",
                    bangumi_id,
                    _format_episode_list(known_retry_episodes),
                    exc,
                )
            missing_episodes: set[int] | None = None
            if bangumi_mappings:
                known_retry_episodes.update(
                    _known_retry_episodes_for_bangumi(
                        pending,
                        bangumi_id,
                    )
                )
                candidate_episodes = _episodes_from_releases(releases) | known_retry_episodes
                if not candidate_episodes:
                    self.logger.info(
                        "Mikan bangumi_id=%s has no episode-numbered discovery candidates or due known retries; skip local scan.",
                        bangumi_id,
                    )
                    continue
                if not _episodes_from_releases(releases) and known_retry_episodes:
                    self.logger.info(
                        "Mikan RSS has no episode-numbered discovery candidates; scanning only durable known retries. "
                        "bangumi_id=%s episodes=%s",
                        bangumi_id,
                        _format_episode_list(candidate_episodes),
                    )
                if redownload_progress:
                    self.logger.info(
                        "Mikan redownload enqueue progress stage=scan_missing current=%s total=%s bangumi_id=%s mappings=%s candidate_episodes=%s queued=%s deferred=%s",
                        index,
                        total_bangumi,
                        bangumi_id,
                        len(bangumi_mappings),
                        _format_episode_list(candidate_episodes),
                        queued,
                        deferred,
                    )
                    _update_redownload_all_active(
                        self.config,
                        stage="scan_missing",
                        stage_label="掃描 RSS 候選集數",
                        current=index,
                        total=total_bangumi,
                        bangumi_id=bangumi_id,
                        queued=queued,
                        deferred=deferred,
                        scan_current=0,
                        scan_total=len(bangumi_mappings),
                    )

                def report_scan_progress(scan_current: int, scan_total: int, root: Path) -> None:
                    _update_redownload_all_active(
                        self.config,
                        stage="scan_missing",
                        stage_label="掃描 RSS 候選集數",
                        current=index,
                        total=total_bangumi,
                        bangumi_id=bangumi_id,
                        queued=queued,
                        deferred=deferred,
                        scan_current=scan_current,
                        scan_total=scan_total,
                        scan_path=str(root),
                    )

                last_scan_heartbeat = time.monotonic()

                def should_stop_scan() -> bool:
                    nonlocal last_scan_heartbeat
                    now_monotonic = time.monotonic()
                    if deadline is not None and now_monotonic >= deadline:
                        return True
                    if redownload_progress and now_monotonic - last_scan_heartbeat >= 30.0:
                        last_scan_heartbeat = now_monotonic
                        _update_redownload_all_active(
                            self.config,
                            stage="scan_missing",
                            stage_label="掃描 RSS 候選集數",
                            current=index,
                            total=total_bangumi,
                            bangumi_id=bangumi_id,
                            queued=queued,
                            deferred=deferred,
                        )
                    if redownload_progress:
                        return self._redownload_all_cancel_requested()
                    return allow_redownload_preempt and self._redownload_all_requested()

                missing_episodes = _missing_episodes_for_bangumi(
                    self.config,
                    self.logger,
                    bangumi_id,
                    bangumi_mappings,
                    pending=pending,
                    candidate_episodes=candidate_episodes,
                    episode_index_ready=episode_index_ready,
                    progress_callback=report_scan_progress if redownload_progress else None,
                    stop_callback=should_stop_scan if deadline is not None or redownload_progress or allow_redownload_preempt else None,
                )
                if deadline is not None and time.monotonic() >= deadline:
                    self.logger.info("Mikan discovery yielded during target lookup; current series will resume. bangumi_id=%s", bangumi_id)
                    return queued
                if stop_for_cancel(queued, deferred):
                    return queued
                if allow_redownload_preempt and self._redownload_all_requested():
                    self.logger.warning(
                        "Mikan enqueue preempted by pending redownload-all request during local scan. queued=%s deferred=%s",
                        queued,
                        deferred,
                    )
                    return queued
                if not missing_episodes:
                    self.logger.info("Mikan bangumi_id=%s has no RSS candidate episodes missing official subtitles.", bangumi_id)
                    continue
            selected = _select_releases_for_bangumi(
                releases,
                bangumi_id,
                self.config,
                self.logger,
                missing_episodes=missing_episodes,
            )
            if missing_episodes is not None:
                candidates_by_episode = _release_candidates_by_episode(releases, missing_episodes, self.config)
                selected_by_episode = {}
                selection_deferred: dict[int, list[str]] = {}
                for episode in sorted(missing_episodes, reverse=True):
                    ambiguity_reasons = selection_deferred.setdefault(episode, [])
                    release = _choose_release_for_episode(
                        bangumi_id,
                        episode,
                        candidates_by_episode.get(episode, []),
                        seen,
                        pending,
                        mappings=bangumi_mappings,
                        ambiguity_reasons=ambiguity_reasons,
                    )
                    if not ambiguity_reasons:
                        selection_deferred.pop(episode, None)
                    if release is not None:
                        selected_by_episode[episode] = release
                covered_episode_numbers = set(selected_by_episode)
                covered_episode_numbers.update(
                    episode
                    for episode in missing_episodes
                    if _has_active_pending(bangumi_id, episode, pending)
                    or _has_deferred_release(bangumi_id, episode, pending)
                )
                fallback_episodes = missing_episodes - covered_episode_numbers
                fallback_search_result: object | None = None
                if fallback_episodes:
                    fallback_search_result = self._fallback_sources.search(
                        bangumi_id,
                        bangumi_mappings,
                        fallback_episodes,
                    )
                    fallback_candidates = _release_candidates_by_episode(
                        fallback_search_result,
                        fallback_episodes,
                        self.config,
                    )
                    for episode in sorted(fallback_episodes, reverse=True):
                        ambiguity_reasons = selection_deferred.setdefault(episode, [])
                        release = _choose_release_for_episode(
                            bangumi_id,
                            episode,
                            fallback_candidates.get(episode, []),
                            seen,
                            pending,
                            mappings=bangumi_mappings,
                            ambiguity_reasons=ambiguity_reasons,
                        )
                        if not ambiguity_reasons:
                            selection_deferred.pop(episode, None)
                        if release is not None:
                            selected_by_episode[episode] = release
                            self.logger.warning(
                                "Mikan fallback source selected. bangumi_id=%s episode=%s source=%s title=%s",
                                bangumi_id,
                                episode,
                                release.source,
                                release.title,
                            )
                    covered_episode_numbers.update(selected_by_episode)
                selected = _unique_releases_by_torrent_url(selected_by_episode.values())
                ambiguous_episodes = set(selection_deferred) - covered_episode_numbers
                if ambiguous_episodes:
                    self.logger.warning(
                        "Mikan release identity requires review; no automatic candidate was selected. "
                        "bangumi_id=%s episodes=%s reasons=%s",
                        bangumi_id,
                        _format_episode_list(ambiguous_episodes),
                        {
                            episode: sorted(set(selection_deferred.get(episode, [])))
                            for episode in sorted(ambiguous_episodes)
                        },
                    )
                    self._mark_candidate_review_with_state_lock(
                        bangumi_id,
                        {
                            episode: selection_deferred.get(episode, [])
                            for episode in sorted(ambiguous_episodes)
                        },
                        state_required=state_required,
                    )
                not_selectable = sorted(
                    missing_episodes - covered_episode_numbers - ambiguous_episodes
                )
                if (
                    not_selectable
                    and primary_lookup_succeeded
                    and _fallback_search_is_conclusive(fallback_search_result)
                ):
                    retry_delays = self._mark_no_candidate_retry_with_state_lock(
                        bangumi_id,
                        not_selectable,
                        state_required=state_required,
                    )
                    self.logger.warning(
                        "Mikan bangumi_id=%s has no selectable source for episodes=%s; retry_after=%s",
                        bangumi_id,
                        _format_episode_list(not_selectable),
                        _format_no_candidate_retry_delays(retry_delays),
                    )
                elif not_selectable:
                    self.logger.info(
                        "Mikan candidate search deferred without advancing no-candidate retry. "
                        "bangumi_id=%s episodes=%s reason=%s",
                        bangumi_id,
                        _format_episode_list(not_selectable),
                        _candidate_search_deferred_reason(
                            fallback_search_result,
                            primary_lookup_succeeded=primary_lookup_succeeded,
                        ),
                    )

            for release in selected:
                if deadline is not None and time.monotonic() >= deadline:
                    self.logger.info("Mikan discovery yielded before enqueue; current series will resume. bangumi_id=%s", bangumi_id)
                    return queued
                if stop_for_cancel(queued, deferred):
                    return queued
                if allow_redownload_preempt and self._redownload_all_requested():
                    self.logger.warning(
                        "Mikan enqueue preempted by pending redownload-all request. queued=%s deferred=%s",
                        queued,
                        deferred,
                    )
                    return queued
                covered_episodes = release_episode_numbers(release)
                if covered_episodes and all(_has_active_pending(release.bangumi_id, episode, pending) for episode in covered_episodes):
                    self.logger.info(
                        "Mikan release already pending, skip episodes=%s title=%s",
                        _format_episode_list(covered_episodes),
                        release.title,
                    )
                    continue
                if covered_episodes and all(_has_deferred_release(release.bangumi_id, episode, pending) for episode in covered_episodes):
                    self.logger.info(
                        "Mikan release already stored for later qBittorrent queue, skip episodes=%s title=%s",
                        _format_episode_list(covered_episodes),
                        release.title,
                    )
                    continue
                outcome = self._queue_selected_release_with_state_lock(
                    release,
                    qbit,
                    operation="enqueue_latest_release_state_update",
                    state_required=state_required,
                    unavailable_reason="qbit_unavailable",
                    add_failed_reason="qbit_add_failed",
                    replacement=False,
                    queue_lock_held=queue_lock_held,
                )
                if outcome == "deferred":
                    deferred += 1
                elif outcome == "queued":
                    queued += 1
                if redownload_progress:
                    _update_redownload_all_active(
                        self.config,
                        stage="fetch_releases",
                        stage_label="查詢 Mikan RSS 並加種",
                        current=index,
                        total=total_bangumi,
                        bangumi_id=bangumi_id,
                        queued=queued,
                        deferred=deferred,
                    )
        if redownload_progress:
            _update_redownload_all_active(
                self.config,
                stage="enqueue_complete",
                stage_label="重排完成",
                current=total_bangumi,
                total=total_bangumi,
                queued=queued,
                deferred=deferred,
            )
        self.logger.info("Mikan enqueue complete. queued=%s deferred=%s", queued, deferred)
        return queued

    def process_completed_downloads(self, *, required: bool = True) -> int:
        return self._process_completed_downloads_unlocked()

    def process_queued_extract_jobs(self, *, limit: int = 1) -> int:
        """Claim and process already-discovered extraction jobs without another qBittorrent scan.

        The background completion watcher calls this in independent worker slots so
        slow subtitle extraction never blocks detection of newly completed torrents.
        """
        if limit <= 0:
            return 0
        qbit = self._qbit()
        # Extraction slots are short-lived MikanWorker instances. Rebuilding
        # the local series catalogue in every slot recursively walks /anime and
        # creates a scan stampede. Manual, metadata, and auto-match cache entries
        # are sufficient here; the persistent polling worker owns full refreshes.
        series_mappings = self._series_mappings(cached_only=True)
        self._ensure_episode_index(series_mappings)
        extract_jobs = _claim_mikan_extract_jobs(self.config, limit=int(limit))
        if not extract_jobs:
            return 0
        return self._process_claimed_mikan_extract_jobs(qbit, series_mappings, extract_jobs)

    def extract_dispatch_counts(self) -> tuple[int, int]:
        """Return claimable and leased extraction counts without qBittorrent I/O."""

        return _mikan_extract_dispatch_counts(self.config)

    def poll_download_progress(
        self,
        *,
        state_required: bool = False,
        cached_mappings_only: bool = False,
    ) -> MikanDownloadPollResult:
        qbit = self._qbit()
        primary_tag = self.config.qbit_tags[0] if self.config.qbit_tags else None
        torrents = qbit.list_torrents(tag=primary_tag, category=self.config.qbit_category)
        self._reconcile_target_review_sources(qbit)
        series_mappings = self._series_mappings(cached_only=cached_mappings_only)
        self._ensure_episode_index(series_mappings)
        reconciled = self._reconcile_pending_with_existing_torrents(
            torrents,
            series_mappings,
            qbit=qbit,
            state_required=state_required,
        )
        if reconciled:
            self.logger.warning("Mikan existing qBittorrent tasks restored to active pending state. entries=%s", reconciled)
        synced = self._sync_pending_download_progress_from_torrents(
            torrents,
            series_mappings,
            state_required=state_required,
        )
        pending = _load_pending(self.pending_path)
        source_tagged = self._sync_qbit_source_tags(qbit, torrents, pending, series_mappings)
        if source_tagged:
            self.logger.info("Mikan qBittorrent source tags synchronized. torrents=%s", source_tagged)
        enqueue_summary = self._enqueue_completed_extract_jobs(
            torrents,
            pending,
            series_mappings,
            qbit=qbit,
            state_required=state_required,
        )
        stale_targets = self._expire_stale_completed_waiting_extract(
            torrents,
            series_mappings,
            state_required=state_required,
        )
        if stale_targets:
            self.request_replacement_enqueue(
                stale_targets,
                reason=(
                    "Completed qBittorrent subtitle source disappeared before extraction; "
                    "replacement lookup is delegated so completed extraction remains responsive."
                ),
            )
            self.logger.warning(
                "Mikan completed downloads disappeared before extraction; replacement search deferred. targets=%s",
                _format_replacement_targets(stale_targets),
            )
        stalled_targets = self._expire_stalled_pending_targets(
            qbit,
            state_required=state_required,
            torrents_override=torrents,
            progress_already_synced=True,
            series_mappings_override=series_mappings,
        )
        if stalled_targets:
            self.request_replacement_enqueue(
                stalled_targets,
                reason="Unhealthy qBittorrent tasks were removed; replacement lookup is delegated so completed extraction remains responsive.",
            )
            self.logger.warning(
                "Mikan zero-speed torrents removed; replacement search deferred to enqueue worker. targets=%s",
                _format_replacement_targets(stalled_targets),
            )
        claimable_extract_count, running_extract_count = _mikan_extract_dispatch_counts(self.config)
        return MikanDownloadPollResult(
            synced_progress_count=synced,
            active_download_count=sum(
                1
                for torrent in torrents
                if float(torrent.progress or 0.0) < 1.0
                and str(torrent.state or "").casefold() not in {"pauseddl", "pausedup", "stoppeddl", "stoppedup"}
            ),
            # Only report newly queued torrents here. The old active-count value
            # caused the watcher to resubmit and log the same running jobs every
            # few seconds until their lease expired.
            completed_pending_count=enqueue_summary.queued,
            claimable_extract_count=claimable_extract_count,
            running_extract_count=running_extract_count,
        )

    def _reconcile_target_review_sources(self, qbit: QBitClient) -> None:
        """Refresh open source-review availability without delaying hot polls."""

        now_monotonic = time.monotonic()
        if now_monotonic < self._next_review_source_reconcile_at:
            return
        self._next_review_source_reconcile_at = (
            now_monotonic + MIKAN_REVIEW_SOURCE_RECONCILE_SECONDS
        )
        try:
            reviews = _open_target_ambiguity_reviews(self.config)
            if not reviews:
                return
            # The normal progress poll is intentionally filtered by the managed
            # category/tag.  Lifecycle proof must use the complete qB list so a
            # manually changed tag can never be mistaken for a removed torrent.
            all_torrents = qbit.list_torrents()
            summary = reconcile_target_ambiguity_review_sources(
                self.config,
                all_torrents,
                reviews=reviews,
            )
        except (QBitError, MikanWorkerError, OSError, sqlite3.Error) as exc:
            self.logger.warning(
                "Target review source reconciliation skipped because availability "
                "could not be verified safely: %s",
                exc,
            )
            return
        if summary["changed"] or summary["resolved"]:
            self.logger.info(
                "Target review source lifecycle reconciled. checked=%s changed=%s "
                "resolved=%s qbit=%s files=%s redownload=%s processing=%s pending=%s unknown=%s",
                summary["checked"],
                summary["changed"],
                summary["resolved"],
                summary["qbit_present"],
                summary["source_files_present"],
                summary["redownload_available"],
                summary["processing"],
                summary["source_unavailable_pending"],
                summary["unknown"],
            )

    def _sync_qbit_source_tags(
        self,
        qbit: QBitClient,
        torrents: list[QBitTorrent],
        pending: dict[str, Any],
        series_mappings: list[dict[str, object]],
    ) -> int:
        additions: dict[tuple[str, str], QBitTorrent] = {}
        for entry in _pending_items(pending).values():
            if not isinstance(entry, dict) or not _pending_has_active_release(entry):
                continue
            source_tag = _source_tag(entry.get("source"))
            if not source_tag:
                continue
            for torrent in _torrents_for_active_pending(entry, torrents, series_mappings):
                if not torrent.hash or not _missing_torrent_tags(torrent, [source_tag]):
                    continue
                additions[(torrent.hash, source_tag)] = torrent

        tagged_hashes: set[str] = set()
        for (torrent_hash, source_tag), torrent in additions.items():
            try:
                qbit.add_tags(torrent_hash, [source_tag])
                tagged_hashes.add(torrent_hash)
            except QBitError as exc:
                self.logger.warning(
                    "Failed to add qBittorrent source tag. torrent=%s source=%s error=%s",
                    torrent.name,
                    source_tag,
                    exc,
                )
        return len(tagged_hashes)

    def _reconcile_pending_with_existing_torrents(
        self,
        torrents: list[QBitTorrent],
        series_mappings: list[dict[str, object]],
        *,
        qbit: QBitClient | None = None,
        state_required: bool,
    ) -> int:
        snapshot = _load_pending(self.pending_path)
        # A replaced/terminal job records that this exact torrent was already
        # inspected and cannot produce a usable result.  Reconstructing its
        # pending entry from qBittorrent would make the completed-download poll
        # submit the same hash forever (notably for releases with no subtitle
        # streams).  A genuinely new replacement has a different job key and
        # is therefore still eligible for reconciliation.
        non_requeueable_job_keys = _mikan_non_requeueable_extract_job_keys(self.config)
        matches: list[dict[str, Any]] = []
        for key, entry in _pending_items(snapshot).items():
            if (
                not isinstance(entry, dict)
                or _pending_is_terminal_success(entry)
                or _pending_has_active_release(entry)
            ):
                continue

            candidate_specs: list[tuple[str, str, str]] = []
            if _pending_has_deferred_release(entry):
                candidate_specs.append(
                    (
                        "deferred",
                        str(entry.get("deferred_title") or ""),
                        str(entry.get("deferred_torrent_url") or ""),
                    )
                )
            if str(entry.get("last_failure_reason") or "") in {
                "did not start",
                "stalled",
                "zero speed",
                "eta too long",
            }:
                candidate_specs.append(
                    (
                        "failed",
                        str(entry.get("last_failed_title") or ""),
                        str(entry.get("last_failed_torrent_url") or ""),
                    )
                )

            for mode, title, torrent_url in candidate_specs:
                if not title or not torrent_url:
                    continue
                probe = dict(entry)
                probe["title"] = title
                probe["torrent_url"] = torrent_url
                matching = _torrents_for_pending(probe, torrents)
                matching = [
                    torrent
                    for torrent in matching
                    if _mikan_extract_job_key(torrent) not in non_requeueable_job_keys
                ]
                if mode == "failed":
                    matching = [torrent for torrent in matching if _is_completed(torrent)]
                if not matching:
                    continue
                torrent = max(matching, key=lambda item: (item.progress, item.downloaded, item.dlspeed))
                matches.append(
                    {
                        "key": str(key),
                        "mode": mode,
                        "title": title,
                        "torrent_url": torrent_url,
                        "torrent": torrent,
                    }
                )
                break

        reserved_keys = {str(match["key"]) for match in matches}
        recovered_by_key: dict[str, dict[str, Any]] = {}
        now_ts = time.time()
        max_age_seconds = max(
            1,
            int(getattr(self.config, "mikan_completed_reconcile_max_age_seconds", 7200) or 7200),
        )
        for torrent in torrents:
            if not _is_completed(torrent) or _has_any_tag(torrent, self.config.mikan_processed_tags):
                continue
            if _mikan_extract_job_key(torrent) in non_requeueable_job_keys:
                continue
            completed_at = _coerce_int(torrent.completion_on) or _coerce_int(torrent.added_on)
            if completed_at is None or completed_at <= 0:
                continue
            age_seconds = now_ts - completed_at
            if age_seconds < -300 or age_seconds > max_age_seconds:
                continue
            if _active_pending_entries_for_completed_torrent(snapshot, torrent, series_mappings):
                continue
            torrent_files: list[QBitTorrentFile] = []
            resolution = _resolve_untracked_torrent_targets(torrent, snapshot, series_mappings)
            if not resolution.trusted and qbit is not None and torrent.hash:
                try:
                    torrent_files = qbit.list_files(torrent.hash)
                except QBitError as exc:
                    self.logger.warning(
                        "Failed to inspect completed qBittorrent pack while restoring pending state. torrent=%s error=%s",
                        torrent.name,
                        exc,
                    )
                resolution = _resolve_untracked_torrent_targets(
                    torrent,
                    snapshot,
                    series_mappings,
                    torrent_files=torrent_files,
                )
            targets = list(resolution.targets) if resolution.trusted else []
            for target in targets:
                key = _pending_key(target.bangumi_id, target.episode)
                entry = _pending_items(snapshot).get(key)
                if key in reserved_keys or (
                    isinstance(entry, dict)
                    and (_pending_is_terminal_success(entry) or _pending_has_active_release(entry))
                ):
                    continue
                release = MikanRelease(
                    bangumi_id=target.bangumi_id,
                    title=torrent.name,
                    episode=target.episode,
                    episodes=(target.episode,),
                    torrent_url=f"qbit://{torrent.hash or _normalized_title(torrent.name)}",
                    pub_date=datetime.fromtimestamp(completed_at, timezone.utc),
                    content_length=torrent.downloaded,
                    source="qbit-recovered",
                    info_hash=str(torrent.hash or "").casefold() or None,
                )
                candidate = {
                    "key": key,
                    "release": release,
                    "torrent": torrent,
                    "resolution": resolution,
                    "rank": (
                        int(has_extractable_subtitle_hint(torrent.name)),
                        release_score(release, list(getattr(self.config, "mikan_prefer_keywords", []) or []))[0],
                        completed_at,
                        torrent.downloaded,
                    ),
                }
                existing = recovered_by_key.get(key)
                if existing is None or candidate["rank"] > existing["rank"]:
                    recovered_by_key[key] = candidate

        if not matches and not recovered_by_key:
            return 0

        lock = self._acquire_state_lock_for_enqueue(
            "reconcile_existing_qbit_pending_state_update",
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            return 0
        restored = 0
        try:
            pending = _load_pending(self.pending_path)
            seen = _load_seen(self.seen_path)
            items = _pending_items(pending)
            now = _utc_now()
            for match in matches:
                entry = items.get(match["key"])
                if (
                    not isinstance(entry, dict)
                    or _pending_is_terminal_success(entry)
                    or _pending_has_active_release(entry)
                ):
                    continue
                mode = str(match["mode"])
                if mode == "deferred":
                    if str(entry.get("deferred_torrent_url") or "") != match["torrent_url"]:
                        continue
                    source = entry.get("deferred_source") or "mikan"
                    source_page = entry.get("deferred_source_page")
                    seeders = entry.get("deferred_seeders")
                    pub_date = entry.get("deferred_pub_date")
                else:
                    if str(entry.get("last_failed_torrent_url") or "") != match["torrent_url"]:
                        continue
                    source = entry.get("last_failed_source") or "mikan"
                    source_page = entry.get("last_failed_source_page")
                    seeders = entry.get("last_failed_seeders")
                    pub_date = entry.get("pub_date")
                torrent = match["torrent"]
                entry.update(
                    {
                        "torrent_url": match["torrent_url"],
                        "title": match["title"],
                        "queued_at": now.isoformat(),
                        "source": source,
                        "source_page": source_page,
                        "info_hash": str(torrent.hash or entry.get("deferred_info_hash") or entry.get("last_failed_info_hash") or "").casefold() or None,
                        "seeders": seeders,
                        "pub_date": pub_date,
                    }
                )
                _clear_deferred_release(entry)
                _clear_no_candidate_retry(entry)
                entry.pop("timed_out_at", None)
                entry.pop("last_failure_reason", None)
                _sync_pending_entry_qbit_progress(entry, [torrent], now)
                seen[match["torrent_url"]] = _seen_payload_from_pending_entry(entry)
                restored += 1
            for recovery in recovered_by_key.values():
                key = str(recovery["key"])
                entry = items.get(key)
                if isinstance(entry, dict) and (
                    _pending_is_terminal_success(entry) or _pending_has_active_release(entry)
                ):
                    continue
                release = recovery["release"]
                torrent = recovery["torrent"]
                resolution = recovery["resolution"]
                _mark_pending(pending, release)
                entry = _pending_entry(release.bangumi_id, int(release.episode), pending)
                entry.update(
                    {
                        "recovery_match_confidence": float(resolution.confidence),
                        "recovery_match_version": resolution.match_version,
                        "recovery_match_evidence": list(resolution.evidence),
                    }
                )
                _sync_pending_entry_qbit_progress(entry, [torrent], now)
                seen[release.torrent_url] = _seen_payload_from_pending_entry(entry)
                restored += 1
            if restored:
                _save_seen(self.seen_path, seen)
                _save_pending(self.pending_path, pending)
        finally:
            lock.release()
        return restored

    def _ensure_episode_index(self, series_mappings: list[dict[str, object]]) -> None:
        """Refresh readiness only; the bounded scan plan owns reconciliation.

        This method is used by several hot workers. Recursive work here used to
        synchronize a full-library rebuild whenever the global TTL expired.
        """

        now_monotonic = time.monotonic()
        if self._episode_index_ready and now_monotonic < self._episode_index_next_check_monotonic:
            return
        check_interval = min(
            300,
            max(
                1,
                int(
                    getattr(
                        self.config,
                        "mikan_library_fallback_scan_interval_seconds",
                        3600,
                    )
                    or 3600
                ),
            ),
        )
        self._episode_index_ready = _mikan_episode_index_covers_mappings(
            self.config,
            series_mappings,
        )
        self._episode_index_next_check_monotonic = now_monotonic + check_interval

    def _enqueue_completed_extract_jobs(
        self,
        torrents: list[QBitTorrent],
        pending_snapshot: dict[str, Any],
        series_mappings: list[dict[str, object]],
        *,
        qbit: QBitClient | None = None,
        state_required: bool = False,
    ) -> MikanExtractEnqueueSummary:
        completion_tagged = 0
        active_jobs = 0
        queued_jobs = 0
        skipped_processed = 0
        reprocess_incomplete = 0
        skipped_missing_local = 0
        skipped_no_active_pending = 0
        job_rows: list[tuple[QBitTorrent, list[dict[str, Any]], int, bool]] = []
        seen: set[str] = set()
        for torrent in torrents:
            if not _is_completed(torrent):
                continue
            job_key = _mikan_extract_job_key(torrent)
            if job_key in seen:
                continue
            seen.add(job_key)
            completed_tags = list(getattr(self.config, "mikan_completed_tags", []) or [])
            missing_completed_tags = _missing_torrent_tags(torrent, completed_tags)
            if qbit is not None and torrent.hash and missing_completed_tags:
                try:
                    qbit.add_tags(torrent.hash, missing_completed_tags)
                    completion_tagged += 1
                except QBitError as exc:
                    self.logger.warning(
                        "Failed to add completed-download qBittorrent tag. torrent=%s error=%s",
                        torrent.name,
                        exc,
                    )
            active_entries = _active_pending_entries_for_completed_torrent(pending_snapshot, torrent, series_mappings)
            if not active_entries:
                skipped_no_active_pending += 1
                continue
            local_episode_exists = _completed_torrent_has_local_episode(
                torrent,
                self.config,
                series_mappings,
                pending_entries=active_entries,
            )
            if active_entries and not _series_mappings_for_pending_entries(series_mappings, active_entries):
                # Queue a review-only job for a source that is durably tied to
                # pending bangumi metadata but has no target mapping.  The
                # scoped resolver still refuses every global episode fallback,
                # so no subtitle can be imported before manual resolution.
                local_episode_exists = True
            if not local_episode_exists and active_entries and all(
                _pending_target_missing_recent(
                    entry,
                    time.time(),
                    _mikan_target_missing_retry_seconds(self.config),
                )
                for entry in active_entries
            ):
                skipped_missing_local += 1
                continue
            is_processed = _has_any_tag(torrent, self.config.mikan_processed_tags)
            # A successful extract-job row is historical state keyed by torrent hash.
            # The same torrent can be downloaded again after qBittorrent removed it,
            # so an active pending entry without the processed tag must run again.
            force_requeue = not is_processed
            if is_processed:
                if _completed_torrent_outputs_complete(
                    torrent,
                    self.config,
                    self.logger,
                    series_mappings,
                    pending_entries=active_entries,
                ):
                    skipped_processed += 1
                    continue
                reprocess_incomplete += 1
                force_requeue = True
                if qbit is not None and torrent.hash:
                    try:
                        qbit.remove_tags([torrent.hash], self.config.mikan_processed_tags)
                    except QBitError as exc:
                        self.logger.warning(
                            "Failed to remove stale extracted qBittorrent tag before reprocessing. torrent=%s error=%s",
                            torrent.name,
                            exc,
                        )

            if not local_episode_exists:
                skipped_missing_local += 1
                continue

            priority = _pending_extract_priority(active_entries)
            job_rows.append((torrent, active_entries, priority, force_requeue))

        if job_rows:
            queued_jobs = _upsert_mikan_extract_jobs(self.config, job_rows, state_required=state_required)
            active_jobs = _mikan_extract_active_job_count(self.config)
        return MikanExtractEnqueueSummary(
            completion_tagged=completion_tagged,
            queued=queued_jobs,
            active=active_jobs,
            skipped_processed=skipped_processed,
            reprocess_incomplete=reprocess_incomplete,
            skipped_missing_local=skipped_missing_local,
            skipped_no_active_pending=skipped_no_active_pending,
        )

    def _expire_stale_completed_waiting_extract(
        self,
        torrents: list[QBitTorrent],
        series_mappings: list[dict[str, object]],
        *,
        state_required: bool,
    ) -> list[MikanReplacementTarget]:
        stale_seconds = max(
            1,
            int(getattr(self.config, "mikan_completed_waiting_extract_stale_seconds", 300) or 300),
        )
        now = _utc_now()
        now_ts = now.timestamp()
        snapshot = _load_pending(self.pending_path)
        stale_keys: list[str] = []
        for key, entry in _pending_items(snapshot).items():
            if not isinstance(entry, dict) or not _completed_waiting_entry_is_stale(
                entry,
                torrents,
                series_mappings,
                now_ts=now_ts,
                stale_seconds=stale_seconds,
            ):
                continue
            stale_keys.append(str(key))
        if not stale_keys:
            return []

        lock = self._acquire_state_lock_for_enqueue(
            "expire_stale_completed_waiting_extract_state_update",
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            return []

        targets: list[MikanReplacementTarget] = []
        changed = False
        try:
            pending = _load_pending(self.pending_path)
            items = _pending_items(pending)
            for key in stale_keys:
                entry = items.get(key)
                if not isinstance(entry, dict) or not _completed_waiting_entry_is_stale(
                    entry,
                    torrents,
                    series_mappings,
                    now_ts=now_ts,
                    stale_seconds=stale_seconds,
                ):
                    continue
                failed_targets = _mark_active_pending_entry_extract_failed(
                    entry,
                    failure_reason="source_torrent_missing_before_extract",
                    failure_detail=(
                        "Completed qBittorrent source is no longer present before subtitle extraction. "
                        "qBittorrent may have removed the torrent/files before the worker could extract subtitles."
                    ),
                    failure_context={
                        "qbit_hash": str(entry.get("last_qbit_hash") or entry.get("info_hash") or ""),
                        "qbit_name": str(entry.get("last_qbit_name") or entry.get("title") or ""),
                        "replacement_recommended": True,
                    },
                    failed_info_hash=str(entry.get("last_qbit_hash") or entry.get("info_hash") or ""),
                    failed_title=str(entry.get("last_qbit_name") or entry.get("title") or ""),
                )
                changed = True
                targets.extend(failed_targets)
            if changed:
                _save_pending(self.pending_path, pending)
        finally:
            lock.release()
        return _unique_replacement_targets(targets)

    def _process_completed_downloads_unlocked(self) -> int:
        qbit = self._qbit()
        primary_tag = self.config.qbit_tags[0] if self.config.qbit_tags else None
        torrents = qbit.list_torrents(tag=primary_tag, category=self.config.qbit_category)
        series_mappings = self._series_mappings()
        self._ensure_episode_index(series_mappings)
        extract_jobs = _claim_mikan_extract_jobs(self.config, limit=max(1, int(self.config.mikan_extract_workers or 1)))
        if extract_jobs:
            self.logger.info(
                "Mikan processing existing queued extract jobs before qBittorrent sync. jobs=%s",
                len(extract_jobs),
            )
            return self._process_claimed_mikan_extract_jobs(qbit, series_mappings, extract_jobs)
        reconciled = self._reconcile_pending_with_existing_torrents(
            torrents,
            series_mappings,
            qbit=qbit,
            state_required=False,
        )
        if reconciled:
            self.logger.warning("Mikan existing qBittorrent tasks restored to active pending state. entries=%s", reconciled)
        synced = self._sync_pending_download_progress_from_torrents(
            torrents,
            series_mappings,
            state_required=False,
        )
        if synced:
            self.logger.info("Mikan qBittorrent progress synced to pending state. entries=%s", synced)
        pending_snapshot = _load_pending(self.pending_path)
        source_tagged = self._sync_qbit_source_tags(qbit, torrents, pending_snapshot, series_mappings)
        if source_tagged:
            self.logger.info("Mikan qBittorrent source tags synchronized. torrents=%s", source_tagged)
        enqueue_summary = self._enqueue_completed_extract_jobs(
            torrents,
            pending_snapshot,
            series_mappings,
            qbit=qbit,
            state_required=False,
        )
        stale_targets = self._expire_stale_completed_waiting_extract(
            torrents,
            series_mappings,
            state_required=False,
        )
        if stale_targets:
            self.request_replacement_enqueue(
                stale_targets,
                reason=(
                    "Completed qBittorrent subtitle source disappeared before extraction; "
                    "replacement lookup is delegated so completed extraction remains responsive."
                ),
            )
            self.logger.warning(
                "Mikan completed downloads disappeared before extraction; replacement search deferred. targets=%s",
                _format_replacement_targets(stale_targets),
            )

        summary_has_activity = bool(
            torrents
            or enqueue_summary.completion_tagged
            or enqueue_summary.active
            or enqueue_summary.queued
            or enqueue_summary.reprocess_incomplete
            or stale_targets
        )
        summary_logger = self.logger.info if summary_has_activity else self.logger.debug
        summary_logger(
            "Mikan completed torrents summary total=%s completion_tagged=%s extract_jobs=%s queued=%s skipped_processed=%s reprocess_incomplete=%s skipped_missing_local=%s skipped_no_active_pending=%s stale_completed_waiting_failed=%s",
            len(torrents),
            enqueue_summary.completion_tagged,
            enqueue_summary.active,
            enqueue_summary.queued,
            enqueue_summary.skipped_processed,
            enqueue_summary.reprocess_incomplete,
            enqueue_summary.skipped_missing_local,
            enqueue_summary.skipped_no_active_pending,
            len(stale_targets),
        )

        extract_jobs = _claim_mikan_extract_jobs(self.config, limit=max(1, int(self.config.mikan_extract_workers or 1)))
        if not extract_jobs:
            summary_logger("Mikan completed-download processing complete. processed=0")
            return 0
        return self._process_claimed_mikan_extract_jobs(qbit, series_mappings, extract_jobs)

    def _process_claimed_mikan_extract_jobs(
        self,
        qbit: QBitClient,
        series_mappings: list[dict[str, object]],
        extract_jobs: list[MikanExtractJob],
    ) -> int:
        processed = 0
        torrent_files_by_key: dict[str, list[QBitTorrentFile]] = {}
        enriched_jobs: list[MikanExtractJob] = []
        for job in extract_jobs:
            torrent = job.torrent
            creation_reader = getattr(qbit, "torrent_creation_date", None)
            if torrent.hash and not torrent.creation_date and callable(creation_reader):
                try:
                    created_at = _coerce_int(creation_reader(torrent.hash))
                except QBitError as exc:
                    self.logger.debug(
                        "Could not read qBittorrent torrent creation time. torrent=%s error=%s",
                        torrent.name,
                        exc,
                    )
                else:
                    if created_at:
                        torrent = replace(torrent, creation_date=created_at)
                        job = replace(job, torrent=torrent)
            enriched_jobs.append(job)
            if not torrent.hash:
                continue
            try:
                torrent_files_by_key[job.job_key] = qbit.list_files(torrent.hash)
            except QBitError as exc:
                self.logger.warning("Failed to list qBittorrent files for completed torrent: %s error=%s", torrent.name, exc)

        extract_jobs = enriched_jobs
        _persist_mikan_extract_job_torrent_snapshots(self.config, extract_jobs)

        max_workers = min(self.config.mikan_extract_workers, len(extract_jobs))
        self.logger.info("Processing completed Mikan downloads concurrently. max_workers=%s", max_workers)
        extraction_results: list[tuple[QBitTorrent, MikanExtractResult]] = []
        timeout_by_job = {
            job.job_key: _mikan_extract_job_timeout_for(
                job,
                torrent_files_by_key.get(job.job_key, []),
                self.config,
            )
            for job in extract_jobs
        }

        def extract_one(
            job: MikanExtractJob,
            cancel_event: threading.Event,
            deadline_monotonic: float,
        ) -> MikanExtractResult:
            heartbeat_stop = threading.Event()
            cancel_request_observed = threading.Event()
            lease_seconds = _mikan_extract_lease_seconds(self.config)
            heartbeat_interval = min(30.0, max(5.0, lease_seconds / 3.0))

            def renew_lease() -> None:
                next_heartbeat = time.monotonic() + heartbeat_interval
                while not heartbeat_stop.wait(1.0):
                    if _mikan_extract_cancel_requested(self.config, job):
                        cancel_request_observed.set()
                        cancel_event.set()
                        self.logger.warning(
                            "Cooperative Mikan extraction cancellation acknowledged. torrent=%s job=%s",
                            job.torrent.name,
                            job.job_key,
                        )
                        return
                    if time.monotonic() < next_heartbeat:
                        continue
                    if not _renew_mikan_extract_job_lease(self.config, job):
                        return
                    next_heartbeat = time.monotonic() + heartbeat_interval

            heartbeat_thread = threading.Thread(
                target=renew_lease,
                daemon=True,
                name="mikan-extract-lease-heartbeat",
            )
            try:
                if sys.is_finalizing():
                    return MikanExtractResult(
                        0,
                        failure_reason="worker_shutting_down",
                        failure_detail="Python interpreter is shutting down before extraction could start",
                        retryable=True,
                        defer_seconds=1,
                    )
                try:
                    heartbeat_thread.start()
                except RuntimeError as exc:
                    return MikanExtractResult(
                        0,
                        failure_reason="worker_shutting_down",
                        failure_detail=str(exc),
                        retryable=True,
                        defer_seconds=1,
                    )
                result = _coerce_mikan_extract_result(
                    self._extract_completed_torrent(
                        job.torrent,
                        series_mappings,
                        torrent_files_by_key.get(job.job_key, []),
                        pending_episodes=_pending_source_episode_numbers_from_entries(job.pending_entries),
                        pending_entries=job.pending_entries,
                        cancel_event=cancel_event,
                        deadline_monotonic=deadline_monotonic,
                        progress_callback=lambda processed, total, current: _update_mikan_extract_job_progress(
                            self.config,
                            job,
                            processed=processed,
                            total=total,
                            current=current,
                        ),
                    )
                )
                if cancel_request_observed.is_set() and result.failure_reason == "extract_cancelled":
                    return MikanExtractResult(
                        0,
                        failure_reason="extract_cancelled_by_user",
                        failure_detail="Subtitle extraction was cancelled by the user and will not retry automatically",
                        failure_context={
                            **result.failure_context,
                            "partial_extracted_count": result.extracted_count,
                        },
                        retryable=False,
                    )
                return result
            except Exception as exc:  # noqa: BLE001 - keep one bad torrent from aborting every slot.
                self.logger.exception(
                    "Unhandled completed Mikan extraction error: torrent=%s error=%s",
                    job.torrent.name,
                    exc,
                )
                return MikanExtractResult(
                    0,
                    failure_reason="extract_exception",
                    failure_detail=str(exc),
                )
            finally:
                heartbeat_stop.set()
                if heartbeat_thread.is_alive():
                    heartbeat_thread.join(timeout=0.2)
                if cancel_request_observed.is_set():
                    _acknowledge_mikan_extract_cancel(self.config, job)

        def record_result(job: MikanExtractJob, result: MikanExtractResult) -> None:
            nonlocal processed
            if result.failure_reason == "extract_cancelled" and _mikan_extract_cancel_requested(self.config, job):
                _acknowledge_mikan_extract_cancel(self.config, job)
                result = MikanExtractResult(
                    0,
                    failure_reason="extract_cancelled_by_user",
                    failure_detail="Subtitle extraction was cancelled by the user and will not retry automatically",
                    failure_context={
                        **result.failure_context,
                        "partial_extracted_count": result.extracted_count,
                    },
                    retryable=False,
                )
            if result.defer_seconds > 0:
                _requeue_claimed_mikan_extract_job(
                    self.config,
                    job,
                    reason=result.failure_detail or result.failure_reason or "Subtitle extraction deferred",
                    delay_seconds=result.defer_seconds,
                )
                self.logger.info(
                    "Deferred Mikan subtitle extraction without failure. torrent=%s retry_in=%ss reason=%s",
                    job.torrent.name,
                    int(result.defer_seconds),
                    result.failure_reason or "busy",
                )
                return

            if result.extracted_count > 0 and job.torrent.hash:
                try:
                    qbit.add_tags(job.torrent.hash, self.config.mikan_processed_tags)
                except QBitError as exc:
                    # The subtitle files are already safely written. A transient
                    # tagging failure must not strand the DB job in running.
                    self.logger.warning(
                        "Extracted subtitles but failed to add qBittorrent processed tag; will reconcile later. torrent=%s error=%s",
                        job.torrent.name,
                        exc,
                    )

            final_status = "success" if result.extracted_count > 0 else "failed"
            if (
                result.extracted_count <= 0
                and not result.retryable
                and not _mikan_extract_failure_allows_replacement(result.failure_reason)
            ):
                notify_event(
                    self.config,
                    "extract_terminal_failure",
                    "官方字幕提取需要人工確認",
                    result.failure_detail or result.failure_reason,
                    severity="error",
                    key=job.job_key,
                    details={"torrent": job.torrent.name, "reason": result.failure_reason},
                )
            finished = _finish_mikan_extract_job(
                self.config,
                job.job_key,
                final_status,
                result,
                worker_id=job.worker_id,
            )
            if finished is None:
                try:
                    _requeue_claimed_mikan_extract_job(
                        self.config,
                        job,
                        reason="Could not finalize extraction state; job returned to queue",
                        delay_seconds=5,
                    )
                except sqlite3.Error:
                    pass
                self.logger.warning(
                    "Could not finalize Mikan extraction state; job will be recovered by queue lease. torrent=%s job=%s",
                    job.torrent.name,
                    job.job_key,
                )
                return
            if not finished:
                self.logger.warning(
                    "Ignored stale Mikan extraction completion because job ownership changed. torrent=%s job=%s",
                    job.torrent.name,
                    job.job_key,
                )
                return
            if result.extracted_count > 0:
                processed += result.extracted_count
            extraction_results.append((job.torrent, result))

        if len(extract_jobs) == 1:
            # Use a daemon thread rather than a nested ThreadPoolExecutor. It
            # preserves the per-job timeout without triggering concurrent.futures
            # global shutdown failures while the container is stopping.
            job = extract_jobs[0]
            timeout_seconds = timeout_by_job[job.job_key]
            cancel_event = threading.Event()
            deadline_monotonic = time.monotonic() + timeout_seconds
            result_box: list[MikanExtractResult] = []
            extraction_thread = threading.Thread(
                target=lambda: result_box.append(extract_one(job, cancel_event, deadline_monotonic)),
                daemon=True,
                name="mikan-subtitle-extract-task",
            )
            try:
                extraction_thread.start()
            except RuntimeError as exc:
                _requeue_claimed_mikan_extract_job(
                    self.config,
                    job,
                    reason=f"Extraction thread could not start: {exc}",
                )
                return 0
            while extraction_thread.is_alive():
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    break
                if _mikan_extract_cancel_requested(self.config, job):
                    cancel_event.set()
                extraction_thread.join(timeout=min(0.5, remaining))
            if extraction_thread.is_alive():
                user_cancelled = _mikan_extract_cancel_requested(self.config, job)
                cancel_event.set()
                grace_seconds = _mikan_extract_cancel_grace_seconds(self.config)
                if grace_seconds > 0:
                    extraction_thread.join(timeout=grace_seconds)
                result = MikanExtractResult(
                    0,
                    failure_reason="extract_cancelled_by_user" if user_cancelled else "extract_timeout",
                    failure_detail=(
                        "Subtitle extraction was cancelled by the user and will not retry automatically"
                        if user_cancelled
                        else (
                            f"Subtitle extraction exceeded its adaptive {int(timeout_seconds)}s deadline; "
                            f"the same job will resume after active file operations stop: {job.torrent.name}"
                        )
                    ),
                    retryable=not user_cancelled,
                    defer_seconds=0.0 if user_cancelled else _mikan_extract_timeout_retry_seconds(self.config),
                )
            else:
                result = result_box[0] if result_box else MikanExtractResult(
                    0,
                    failure_reason="extract_exception",
                    failure_detail="Subtitle extraction thread exited without a result",
                )
            record_result(job, result)
        else:
            if sys.is_finalizing():
                for job in extract_jobs:
                    _requeue_claimed_mikan_extract_job(
                        self.config,
                        job,
                        reason="Python interpreter is shutting down before extraction could start",
                    )
                return 0

            executor = ThreadPoolExecutor(max_workers=max_workers)
            futures = {}
            controls: dict[Any, tuple[threading.Event, float]] = {}
            unsubmitted: list[MikanExtractJob] = []
            for index, job in enumerate(extract_jobs):
                try:
                    cancel_event = threading.Event()
                    deadline_monotonic = time.monotonic() + timeout_by_job[job.job_key]
                    future = executor.submit(extract_one, job, cancel_event, deadline_monotonic)
                except RuntimeError as exc:
                    unsubmitted = extract_jobs[index:]
                    self.logger.warning(
                        "Could not submit Mikan subtitle extraction job(s); returning them to queue. count=%s error=%s",
                        len(unsubmitted),
                        exc,
                    )
                    break
                futures[future] = job
                controls[future] = (cancel_event, deadline_monotonic)

            for job in unsubmitted:
                _requeue_claimed_mikan_extract_job(
                    self.config,
                    job,
                    reason="Extraction worker submission failed; job returned to queue",
                )

            unfinished = set(futures)
            try:
                while unfinished:
                    completed, _ = wait(unfinished, timeout=1.0, return_when=FIRST_COMPLETED)
                    for future in completed:
                        unfinished.discard(future)
                        job = futures[future]
                        record_result(job, _coerce_mikan_extract_result(future.result()))

                    now_monotonic = time.monotonic()
                    expired = [
                        future
                        for future in unfinished
                        if now_monotonic >= controls[future][1]
                    ]
                    for future in expired:
                        unfinished.discard(future)
                        cancel_event, _ = controls[future]
                        cancel_event.set()
                        future.cancel()
                        job = futures[future]
                        timeout_seconds = timeout_by_job[job.job_key]
                        self.logger.warning(
                            "Mikan extraction job reached adaptive deadline; cooperative cancellation requested. torrent=%s timeout_seconds=%s",
                            job.torrent.name,
                            int(timeout_seconds),
                        )
                        record_result(
                            job,
                            MikanExtractResult(
                                0,
                                failure_reason="extract_timeout",
                                failure_detail=(
                                    f"Subtitle extraction exceeded its adaptive {int(timeout_seconds)}s deadline; "
                                    "the same job will resume after active file operations stop"
                                ),
                                retryable=True,
                                defer_seconds=_mikan_extract_timeout_retry_seconds(self.config),
                            ),
                        )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        state_lock = self._acquire_operation_lock(
            "process_completed_downloads_state_update",
            required=False,
            log_busy=False,
        )
        replacement_targets: list[MikanReplacementTarget] = []
        if state_lock is None:
            result = self.request_completed_state_update(
                extraction_results,
                reason="Mikan state operation already running; completed-download state update will run after the current operation finishes.",
            )
            self.logger.warning(
                "Mikan completed-download state update deferred. request_path=%s request_count=%s records=%s reason=%s",
                result.get("request_path"),
                result.get("request_count"),
                result.get("record_count"),
                result.get("reason") or "-",
            )
            self.logger.info("Mikan completed-download processing complete. processed=%s state_update_deferred=true", processed)
            return processed
        try:
            pending = _load_pending(self.pending_path)
            pending_changed = False
            for torrent, result in extraction_results:
                if result.extracted_count > 0:
                    if _clear_completed_pending_entries(
                        pending,
                        torrent,
                        series_mappings,
                        extracted_count=result.extracted_count,
                    ):
                        pending_changed = True
                elif result.retryable:
                    if _mark_completed_pending_extract_deferred(
                        pending,
                        torrent,
                        series_mappings,
                        deferred_reason=result.failure_reason,
                        deferred_detail=result.failure_detail,
                        failure_context=result.failure_context,
                    ):
                        pending_changed = True
                else:
                    failed_entries = _active_pending_entries_for_completed_torrent(
                        pending,
                        torrent,
                        series_mappings,
                    )
                    failed_targets = _mark_completed_pending_extract_failed(
                        pending,
                        torrent,
                        series_mappings,
                        failure_reason=result.failure_reason,
                        failure_detail=result.failure_detail,
                        failure_context=result.failure_context,
                        subtitle_diagnostics=result.subtitle_diagnostics,
                    )
                    # target_ambiguity intentionally creates no replacement
                    # target, but it still archives the active release and
                    # records the review failure on the pending entry.  Save
                    # that state even when the replacement list is empty.
                    if failed_entries:
                        pending_changed = True
                    if failed_targets:
                        replacement_targets.extend(failed_targets)
            if pending_changed:
                _save_pending(self.pending_path, pending)
        finally:
            state_lock.release()

        if replacement_targets:
            self.request_replacement_enqueue(
                replacement_targets,
                reason=(
                    "Completed-download extraction produced replacement targets; "
                    "replacement lookup is delegated so completed extraction remains responsive."
                ),
            )
        self.logger.info("Mikan completed-download processing complete. processed=%s", processed)
        return processed

    def _sync_pending_download_progress_from_torrents(
        self,
        torrents: list[QBitTorrent],
        series_mappings: list[dict[str, object]],
        *,
        state_required: bool,
    ) -> int:
        now = _utc_now()
        snapshot = _load_pending(self.pending_path)
        snapshot_fields = (
            "last_downloaded",
            "last_progress",
            "last_progress_at",
            "last_qbit_sync_at",
            "last_dlspeed",
            "last_qbit_state",
            "last_qbit_hash",
            "last_qbit_name",
            "last_qbit_added_on",
            "last_qbit_completion_on",
            "info_hash",
        )
        patches: list[dict[str, Any]] = []
        for key, entry in _pending_items(snapshot).items():
            if not isinstance(entry, dict) or not _pending_has_active_release(entry):
                continue
            torrent_url = str(entry.get("torrent_url") or "")
            matching_torrents = _torrents_for_active_pending(entry, torrents, series_mappings)
            if matching_torrents:
                changed = _sync_pending_entry_qbit_progress(entry, matching_torrents, now)
            else:
                changed = _clear_invalid_qbit_snapshot_if_needed(entry, torrents, series_mappings)
            if not changed:
                continue
            patches.append(
                {
                    "key": str(key),
                    "torrent_url": torrent_url,
                    "values": {field: entry.get(field) for field in snapshot_fields if field in entry},
                    "remove": [field for field in snapshot_fields if field not in entry],
                    "observed_at": now.timestamp(),
                }
            )
        if not patches:
            return 0

        lock = self._acquire_state_lock_for_enqueue(
            "sync_pending_download_progress_state_update",
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            return 0
        applied = 0
        try:
            pending = _load_pending(self.pending_path)
            current_items = _pending_items(pending)
            for patch in patches:
                entry = current_items.get(patch["key"])
                if not isinstance(entry, dict) or str(entry.get("torrent_url") or "") != patch["torrent_url"]:
                    continue
                current_sync_at = _pending_timestamp(entry.get("last_qbit_sync_at"))
                if current_sync_at > float(patch["observed_at"]):
                    continue
                for field in patch["remove"]:
                    entry.pop(field, None)
                entry.update(patch["values"])
                applied += 1
            if applied:
                _save_pending(self.pending_path, pending)
        finally:
            lock.release()
        return applied

    def _enqueue_replacements_after_extract_failure(
        self,
        targets: list[MikanReplacementTarget],
        qbit: QBitClient,
    ) -> int:
        self._fallback_sources.begin_cycle()
        targets = _unique_replacement_targets(targets)
        if not targets:
            return 0
        if not getattr(self.config, "mikan_enabled", False):
            return 0
        lock = self._acquire_queue_lock(
            "enqueue_replacements_after_extract_failure",
            required=False,
            log_busy=False,
        )
        if lock is None:
            self.request_replacement_enqueue(
                targets,
                reason="Mikan queue operation is busy; replacement enqueue will run after the current queue operation finishes.",
            )
            return 0
        try:
            queued = self._enqueue_replacements_after_extract_failure_unlocked(
                targets,
                qbit,
                queue_lock_held=True,
            )
        except QBitError as exc:
            self.request_replacement_enqueue(
                targets,
                reason=f"qBittorrent unavailable during replacement enqueue: {exc}",
            )
            return 0
        except Exception as exc:  # noqa: BLE001 - replacement enqueue must not fail completed-download processing.
            self.logger.exception(
                "Mikan replacement enqueue failed after extract failure: targets=%s error=%s",
                _format_replacement_targets(targets),
                exc,
            )
            self.request_replacement_enqueue(
                targets,
                reason=f"Replacement enqueue raised {type(exc).__name__}: {exc}",
            )
            return 0
        finally:
            lock.release()

        self.logger.warning(
            "Mikan replacement enqueue after extract failure complete. targets=%s queued=%s",
            _format_replacement_targets(targets),
            queued,
        )
        return queued

    def _enqueue_replacements_after_extract_failure_unlocked(
        self,
        targets: list[MikanReplacementTarget],
        qbit: QBitClient,
        *,
        queue_lock_held: bool = False,
        deadline_monotonic: float | None = None,
    ) -> int:
        qbit.ensure_category(self.config.qbit_category, save_path=self.config.qbit_save_path)
        pending = _load_pending(self.pending_path)
        seen = _load_seen(self.seen_path)

        grouped_targets = _replacement_targets_by_bangumi(targets)
        mappings_by_bangumi: dict[int, list[dict[str, object]]] = {}
        if self._fallback_sources.enabled:
            mappings_by_bangumi = _series_mappings_by_bangumi(self._series_mappings(cached_only=True))
        queued = 0
        deferred = 0

        for bangumi_id, episodes in grouped_targets.items():
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise MikanSourceDeadline("replacement discovery slice ended before target reconciliation")
            if not self._reconcile_verified_history_outputs(
                bangumi_id, episodes, operation="history_output_reconciliation", state_required=False,
            ):
                continue
            pending = _load_pending(self.pending_path)
            missing_episodes = {episode for episode in episodes
                                if not _pending_is_terminal_success(_pending_entry(bangumi_id, episode, pending))}
            if not missing_episodes:
                continue
            bangumi_mappings = mappings_by_bangumi.get(bangumi_id, [])
            primary_lookup_succeeded = True
            try:
                releases = fetch_bangumi_releases(
                    self.config.mikan_base_url,
                    bangumi_id,
                    timeout_seconds=self.config.mikan_request_timeout_seconds,
                    deadline_monotonic=deadline_monotonic,
                )
            except MikanSourceDeadline:
                raise
            except (requests.RequestException, MikanSourceError) as exc:
                self.logger.warning(
                    "Mikan replacement RSS fetch failed; trying fallback sources. bangumi_id=%s error=%s",
                    bangumi_id,
                    exc,
                )
                primary_lookup_succeeded = False
                releases = []

            candidates_by_episode = _release_candidates_by_episode(releases, missing_episodes, self.config)
            selected_by_episode: dict[int, MikanRelease] = {}
            selection_deferred: dict[int, list[str]] = {}
            for episode in sorted(missing_episodes, reverse=True):
                ambiguity_reasons = selection_deferred.setdefault(episode, [])
                release = _choose_release_for_episode(
                    bangumi_id,
                    episode,
                    candidates_by_episode.get(episode, []),
                    seen,
                    pending,
                    mappings=bangumi_mappings,
                    ambiguity_reasons=ambiguity_reasons,
                )
                if not ambiguity_reasons:
                    selection_deferred.pop(episode, None)
                if release is not None:
                    selected_by_episode[episode] = release

            fallback_episodes = missing_episodes - set(selected_by_episode)
            fallback_search_result: object | None = None
            if fallback_episodes:
                fallback_search_result = self._fallback_sources.search(
                    bangumi_id,
                    bangumi_mappings,
                    fallback_episodes,
                )
                if getattr(fallback_search_result, "deferred_reason", "") == "elapsed_budget_exhausted":
                    raise MikanSourceDeadline("replacement fallback yielded at discovery deadline")
                fallback_candidates = _release_candidates_by_episode(
                    fallback_search_result,
                    fallback_episodes,
                    self.config,
                )
                for episode in sorted(fallback_episodes, reverse=True):
                    ambiguity_reasons = selection_deferred.setdefault(episode, [])
                    release = _choose_release_for_episode(
                        bangumi_id,
                        episode,
                        fallback_candidates.get(episode, []),
                        seen,
                        pending,
                        mappings=bangumi_mappings,
                        ambiguity_reasons=ambiguity_reasons,
                    )
                    if not ambiguity_reasons:
                        selection_deferred.pop(episode, None)
                    if release is not None:
                        selected_by_episode[episode] = release
                        self.logger.warning(
                            "Mikan replacement fallback selected. bangumi_id=%s episode=%s source=%s title=%s",
                            bangumi_id,
                            episode,
                            release.source,
                            release.title,
                        )

            selected = _unique_releases_by_torrent_url(selected_by_episode.values())
            covered_episode_numbers = set(selected_by_episode)
            covered_episode_numbers.update(
                episode
                for episode in missing_episodes
                if _has_active_pending(bangumi_id, episode, pending)
                or _has_deferred_release(bangumi_id, episode, pending)
            )
            ambiguous_episodes = set(selection_deferred) - covered_episode_numbers
            if ambiguous_episodes:
                self.logger.warning(
                    "Mikan replacement release identity requires review; no automatic candidate was selected. "
                    "bangumi_id=%s episodes=%s reasons=%s",
                    bangumi_id,
                    _format_episode_list(ambiguous_episodes),
                    {
                        episode: sorted(set(selection_deferred.get(episode, [])))
                        for episode in sorted(ambiguous_episodes)
                    },
                )
                self._mark_candidate_review_with_state_lock(
                    bangumi_id,
                    {
                        episode: selection_deferred.get(episode, [])
                        for episode in sorted(ambiguous_episodes)
                    },
                    state_required=False,
                )
            not_selectable = sorted(
                missing_episodes - covered_episode_numbers - ambiguous_episodes
            )
            if (
                not_selectable
                and primary_lookup_succeeded
                and _fallback_search_is_conclusive(fallback_search_result)
            ):
                retry_delays = self._mark_no_candidate_retry_with_state_lock(
                    bangumi_id,
                    not_selectable,
                    state_required=False,
                )
                self.logger.warning(
                    "Mikan replacement has no selectable source for bangumi_id=%s episodes=%s; retry_after=%s",
                    bangumi_id,
                    _format_episode_list(not_selectable),
                    _format_no_candidate_retry_delays(retry_delays),
                )
            elif not_selectable:
                self.logger.info(
                    "Mikan replacement candidate search deferred without advancing no-candidate retry. "
                    "bangumi_id=%s episodes=%s reason=%s",
                    bangumi_id,
                    _format_episode_list(not_selectable),
                    _candidate_search_deferred_reason(
                        fallback_search_result,
                        primary_lookup_succeeded=primary_lookup_succeeded,
                    ),
                )

            for release in selected:
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    raise MikanSourceDeadline("replacement discovery slice ended before enqueue")
                covered_episodes = release_episode_numbers(release)
                if covered_episodes and all(_has_active_pending(release.bangumi_id, episode, pending) for episode in covered_episodes):
                    continue
                if covered_episodes and all(_has_deferred_release(release.bangumi_id, episode, pending) for episode in covered_episodes):
                    continue
                outcome = self._queue_selected_release_with_state_lock(
                    release,
                    qbit,
                    operation="enqueue_replacement_state_update",
                    state_required=False,
                    unavailable_reason="qbit_unavailable",
                    add_failed_reason="qbit_add_failed",
                    replacement=True,
                    queue_lock_held=queue_lock_held,
                )
                if outcome == "deferred":
                    deferred += 1
                elif outcome == "queued":
                    queued += 1
        self.logger.info("Mikan replacement enqueue complete. queued=%s deferred=%s", queued, deferred)
        return queued

    def _extract_completed_torrent(
        self,
        torrent: QBitTorrent,
        series_mappings: list[dict[str, object]],
        torrent_files: list[QBitTorrentFile] | None = None,
        *,
        pending_episodes: set[int] | None = None,
        pending_entries: list[dict[str, Any]] | None = None,
        cancel_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> MikanExtractResult:
        def cancelled_result(extracted_count: int, current: str = "") -> MikanExtractResult:
            context = {"current_source": current} if current else {}
            return MikanExtractResult(
                extracted_count,
                failure_reason="extract_cancelled",
                failure_detail="Subtitle extraction paused at its cooperative deadline and will resume",
                failure_context=context,
                retryable=True,
                defer_seconds=_mikan_extract_timeout_retry_seconds(self.config),
            )

        def is_cancelled() -> bool:
            return bool(
                (cancel_event is not None and cancel_event.is_set())
                or (deadline_monotonic is not None and time.monotonic() >= deadline_monotonic)
            )

        if is_cancelled():
            return cancelled_result(0)
        root = map_remote_path(torrent.content_path or torrent.save_path, self.config.qbit_path_mappings)
        path_context = _mikan_qbit_path_context(torrent, root, torrent_files or [], self.config)
        if root is None:
            self.logger.warning("Completed torrent has no content path: %s", torrent.name)
            return MikanExtractResult(
                0,
                failure_reason="content_path_unmapped",
                failure_detail=f"qBittorrent content path cannot be mapped: {torrent.content_path or torrent.save_path or '-'}",
                failure_context=path_context,
            )
        if pending_entries is None:
            pending_entries = _active_pending_entries_for_completed_torrent(
                _load_pending(self.pending_path),
                torrent,
                series_mappings,
            )
        source_videos = _torrent_video_paths_from_file_list(torrent, torrent_files or [], self.config)
        if not source_videos and root.exists():
            source_videos.extend(
                _find_video_files(root, self.config.video_extensions, cancelled=is_cancelled)
            )
        source_videos = _unique_paths(source_videos)
        missing_source_target_candidates: list[Path] = []
        if not source_videos and not root.exists():
            missing_source_target_candidates = _fallback_video_files_for_torrent(
                torrent,
                self.config,
                self.logger,
                series_mappings,
                pending_entries=pending_entries,
                cancelled=is_cancelled,
            )
            if missing_source_target_candidates:
                self.logger.info(
                    "Completed torrent source is missing but target library candidates exist; mark source failed so replacement lookup can run. torrent=%s targets=%s",
                    torrent.name,
                    missing_source_target_candidates,
                )
        if pending_episodes is None:
            pending_episodes = _pending_source_episode_numbers_from_entries(pending_entries)
        source_selection = _select_source_videos_for_pending_episodes(source_videos, pending_episodes)
        source_videos = source_selection.selected

        if not source_videos:
            if source_selection.failure_reason:
                failure_context = dict(path_context)
                if pending_episodes:
                    failure_context["pending_episodes"] = sorted(pending_episodes)
                if source_selection.skipped_extra_videos:
                    failure_context["skipped_source_videos"] = [
                        str(path) for path in source_selection.skipped_extra_videos[:12]
                    ]
                self.logger.info(
                    "No usable completed torrent source videos after filtering: %s reason=%s detail=%s",
                    torrent.name,
                    source_selection.failure_reason,
                    source_selection.failure_detail,
                )
                return MikanExtractResult(
                    0,
                    failure_reason=source_selection.failure_reason,
                    failure_detail=source_selection.failure_detail,
                    failure_context=failure_context,
                )
            missing_detail = (
                f"No video files found in mapped completed torrent path: {root}"
                if root.exists()
                else f"Mapped completed torrent path does not exist: {root}"
            )
            if missing_source_target_candidates:
                missing_detail += (
                    "; matching target video exists in input_path, so the original qBittorrent source was "
                    "probably removed before subtitle extraction. The source will be marked failed for replacement."
                )
                path_context["replacement_recommended"] = True
                path_context["local_target_candidates"] = [
                    str(path) for path in missing_source_target_candidates[:10]
                ]
            self.logger.warning("Completed torrent has no usable source videos: %s -> %s", torrent.name, root)
            return MikanExtractResult(
                0,
                failure_reason="source_video_missing",
                failure_detail=missing_detail,
                failure_context=path_context,
                retryable=False,
            )

        extracted_count = 0
        completed_targets: set[str] = set()
        failure_reason = ""
        failure_detail = ""
        failure_context: dict[str, Any] = {}
        target_missing_detail = ""
        target_missing_context: dict[str, Any] = {}
        target_ambiguity_detail = ""
        target_ambiguity_context: dict[str, Any] = {}
        subtitle_diagnostics: list[dict[str, Any]] = []
        busy_targets: list[str] = []
        total_sources = len(source_videos)
        if progress_callback is not None:
            progress_callback(0, total_sources, "")
        for source_index, source_video in enumerate(source_videos):
            def report_source_finished() -> None:
                if progress_callback is not None:
                    progress_callback(source_index + 1, total_sources, str(source_video))

            if is_cancelled():
                return cancelled_result(extracted_count, str(source_video))
            if progress_callback is not None:
                progress_callback(source_index, total_sources, str(source_video))
            target_diagnostics: list[dict[str, Any]] = []
            target_video = _target_video_for_torrent_source(
                source_video,
                torrent,
                self.config,
                self.logger,
                series_mappings,
                pending_entries=pending_entries,
                target_diagnostics=target_diagnostics,
                cancelled=is_cancelled,
            )
            if target_video is None:
                self.logger.warning("No matching target video in input_path for completed torrent: %s", source_video)
                diagnostic_reasons = {
                    str(item.get("reason") or "")
                    for item in target_diagnostics
                    if isinstance(item, dict)
                }
                ambiguous = bool(
                    diagnostic_reasons
                    & {
                        "ambiguous_target_candidates",
                        "low_confidence_target_candidate",
                        "no_series_mapping_for_pending_bangumi",
                        "release_year_conflict",
                    }
                )
                if ambiguous and not target_ambiguity_detail:
                    bangumi_ids = sorted(
                        {
                            value
                            for entry in pending_entries
                            if (value := _coerce_int(entry.get("bangumi_id"))) is not None
                        }
                    )
                    episode = extract_episode_number(source_video.name) or extract_episode_number(torrent.name)
                    target_key = f"{torrent.hash or torrent.name}:{episode or 0}"
                    candidates = [
                        dict(item)
                        for item in target_diagnostics
                        if isinstance(item, dict) and str(item.get("path") or "")
                    ][:3]
                    review_id = ""
                    try:
                        source_time_fields = _review_source_time_fields(
                            torrent,
                            pending_entries,
                        )
                        review_id = upsert_review_item(
                            self.config,
                            kind="target_ambiguity",
                            target_key=target_key,
                            summary=f"Completed torrent target requires review: {torrent.name}",
                            diagnosis={
                                "torrent_hash": str(torrent.hash or ""),
                                "torrent_name": torrent.name,
                                "source_video": str(source_video),
                                "bangumi_ids": bangumi_ids,
                                "episode": episode,
                                "reasons": sorted(diagnostic_reasons),
                                "target_diagnostics": [
                                    dict(item)
                                    for item in target_diagnostics
                                    if isinstance(item, dict)
                                    and str(item.get("reason") or "") == "release_year_conflict"
                                ][:5],
                                **source_time_fields,
                            },
                            candidates=candidates,
                            severity="error",
                        )
                    except sqlite3.Error as exc:
                        # Keep extraction blocked when the review database is
                        # temporarily busy.  Never fall back to an unsafe target
                        # just because observability could not be persisted.
                        self.logger.warning(
                            "Could not persist target ambiguity review item; keeping extraction blocked. torrent=%s error=%s",
                            torrent.name,
                            exc,
                        )
                    target_ambiguity_detail = (
                        "Target mapping is ambiguous"
                        + (f" and was sent to review item {review_id}" if review_id else "")
                        + "; "
                        "no subtitle was imported and no replacement download was requested."
                    )
                    target_ambiguity_context = {
                        "review_id": review_id,
                        "target_candidates": candidates,
                        "bangumi_ids": bangumi_ids,
                    }
                if not target_missing_detail:
                    target_missing_detail = f"No matching target video in input_path for completed torrent source: {source_video}"
                    target_missing_context = _mikan_qbit_path_context(
                        torrent,
                        root,
                        torrent_files or [],
                        self.config,
                        source_video=source_video,
                    )
                    if target_diagnostics:
                        target_missing_context["target_candidates"] = target_diagnostics[:10]
                report_source_finished()
                continue
            target_key = str(_safe_resolve(target_video)).casefold()
            if target_key in completed_targets:
                report_source_finished()
                continue
            target_result = self._extract_completed_source_to_target(
                source_video,
                target_video,
                torrent,
                torrent_files or [],
                root,
                cancel_event=cancel_event,
                deadline_monotonic=deadline_monotonic,
            )
            if target_result.defer_seconds > 0:
                busy_targets.append(str(target_video))
                report_source_finished()
                continue
            if target_result.extracted_count > 0:
                completed_targets.add(target_key)
                extracted_count += target_result.extracted_count
                report_source_finished()
                continue
            if not failure_reason:
                failure_reason = target_result.failure_reason
                failure_detail = target_result.failure_detail
                failure_context = target_result.failure_context
                subtitle_diagnostics = target_result.subtitle_diagnostics
            report_source_finished()
        if busy_targets:
            return MikanExtractResult(
                extracted_count,
                failure_reason="target_video_busy",
                failure_detail=(
                    "AI or another subtitle operation currently owns the target video lock; "
                    "official subtitle extraction will retry shortly."
                ),
                failure_context={"busy_targets": busy_targets[:10]},
                retryable=True,
                defer_seconds=10,
            )
        if not failure_reason and target_missing_detail:
            if target_ambiguity_detail:
                failure_reason = "target_ambiguity"
                failure_detail = target_ambiguity_detail
                failure_context = target_ambiguity_context
            else:
                failure_reason = "target_video_not_found"
                failure_detail = target_missing_detail
                failure_context = target_missing_context
        return MikanExtractResult(
            extracted_count,
            failure_reason=failure_reason,
            failure_detail=failure_detail,
            subtitle_diagnostics=subtitle_diagnostics,
            failure_context=failure_context,
            retryable=(
                failure_reason == "target_video_not_found"
            ),
        )

    def _extract_completed_source_to_target(
        self,
        source_video: Path,
        target_video: Path,
        torrent: QBitTorrent,
        torrent_files: list[QBitTorrentFile],
        root: Path,
        *,
        cancel_event: threading.Event | None = None,
        deadline_monotonic: float | None = None,
    ) -> MikanExtractResult:
        if (cancel_event is not None and cancel_event.is_set()) or (
            deadline_monotonic is not None and time.monotonic() >= deadline_monotonic
        ):
            return MikanExtractResult(
                0,
                failure_reason="extract_cancelled",
                failure_detail="Subtitle extraction paused before opening the next target",
                retryable=True,
                defer_seconds=_mikan_extract_timeout_retry_seconds(self.config),
            )
        if _target_has_required_chinese_subtitles(target_video, verify_config=self.config):
            return MikanExtractResult(1)

        target_lock = VideoLock(target_video)
        if not target_lock.acquire():
            return MikanExtractResult(
                0,
                failure_reason="target_video_busy",
                failure_detail=f"Target video is locked by AI or another subtitle operation: {target_video}",
                retryable=True,
                defer_seconds=10,
            )
        try:
            # AI may have finished and an official sidecar may have appeared
            # between target resolution and lock acquisition.
            if _target_has_required_chinese_subtitles(target_video, verify_config=self.config):
                return MikanExtractResult(1)

            current_diagnostics: list[dict[str, Any]] = []
            extracted = []
            extract_error = ""
            if source_video.exists():
                try:
                    cancellation_kwargs: dict[str, Any] = {}
                    if cancel_event is not None:
                        cancellation_kwargs["cancel_event"] = cancel_event
                    if deadline_monotonic is not None:
                        cancellation_kwargs["deadline_monotonic"] = deadline_monotonic
                    extracted = extract_available_subtitles(
                        source_video,
                        self.config,
                        output_video_path=target_video,
                        diagnostics=current_diagnostics,
                        allowed_languages={"zh-tw", "zh-cn"},
                        validate_for_import=True,
                        **cancellation_kwargs,
                    )
                except SubtitleExtractCancelled as exc:
                    return MikanExtractResult(
                        0,
                        failure_reason="extract_cancelled",
                        failure_detail=str(exc),
                        retryable=True,
                        defer_seconds=_mikan_extract_timeout_retry_seconds(self.config),
                    )
                except SubtitleExtractError as exc:
                    extract_error = str(exc)
                    self.logger.warning(
                        "Failed to extract embedded subtitles for completed torrent: %s error=%s",
                        source_video,
                        exc,
                    )
                    current_diagnostics.append(
                        {
                            "source": "embedded",
                            "status": "probe_failed" if "ffprobe" in extract_error.lower() else "extract_failed",
                            "detail": extract_error[:1000],
                        }
                    )
            if not extracted:
                if (cancel_event is not None and cancel_event.is_set()) or (
                    deadline_monotonic is not None and time.monotonic() >= deadline_monotonic
                ):
                    return MikanExtractResult(
                        0,
                        failure_reason="extract_cancelled",
                        failure_detail="Subtitle extraction paused before sidecar normalization",
                        retryable=True,
                        defer_seconds=_mikan_extract_timeout_retry_seconds(self.config),
                    )
                extra_sidecars = _torrent_sidecar_paths_for_source_video(
                    source_video,
                    torrent,
                    torrent_files,
                    root,
                    self.config,
                )
                normalization_kwargs: dict[str, Any] = {}
                if deadline_monotonic is not None:
                    normalization_kwargs["deadline_monotonic"] = deadline_monotonic
                try:
                    extracted = normalize_sidecar_subtitles_for_output(
                        source_video,
                        self.config,
                        output_video_path=target_video,
                        diagnostics=current_diagnostics,
                        allowed_languages={"zh-tw", "zh-cn"},
                        extra_sidecar_paths=extra_sidecars,
                        validate_for_import=True,
                        **normalization_kwargs,
                    )
                except SubtitleExtractCancelled as exc:
                    return MikanExtractResult(
                        0,
                        failure_reason="extract_cancelled",
                        failure_detail=str(exc),
                        retryable=True,
                        defer_seconds=_mikan_extract_timeout_retry_seconds(self.config),
                    )
            if not extracted:
                reason, detail = _mikan_extract_failure_from_diagnostics(
                    source_video,
                    target_video,
                    current_diagnostics,
                    extract_error=extract_error,
                )
                self.logger.info("No extractable subtitle streams or matching sidecars found: %s", source_video)
                return MikanExtractResult(
                    0,
                    failure_reason=reason,
                    failure_detail=detail,
                    subtitle_diagnostics=current_diagnostics,
                    failure_context=_mikan_qbit_path_context(
                        torrent,
                        root,
                        torrent_files,
                        self.config,
                        source_video=source_video,
                        target_video=target_video,
                    ),
                )
            for subtitle in extracted:
                self.logger.info(
                    "Extracted subtitle language=%s stream=%s source=%s path=%s",
                    subtitle.language,
                    subtitle.stream_index,
                    source_video,
                    subtitle.path,
                )
            return MikanExtractResult(1, subtitle_diagnostics=current_diagnostics)
        finally:
            target_lock.release()

    def _prepare_enqueue_state(
        self,
        qbit: QBitClient | None,
        *,
        state_required: bool,
        queue_lock_held: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], int, list[MikanReplacementTarget]] | None:
        pending = _load_pending(self.pending_path)
        seen = _load_seen(self.seen_path)

        queued = 0
        stalled_targets: list[MikanReplacementTarget] = []
        if qbit is not None:
            queued += self._queue_deferred_releases(
                qbit,
                state_required=state_required,
                queue_lock_held=queue_lock_held,
            )
            stalled_targets = self._expire_stalled_pending_targets(
                qbit,
                state_required=state_required,
                queue_lock_held=queue_lock_held,
            )

            pending = _load_pending(self.pending_path)
            seen = _load_seen(self.seen_path)
        return pending, seen, queued, stalled_targets

    def _snapshot_enqueue_state(
        self,
        operation: str,
        *,
        state_required: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        return _load_pending(self.pending_path), _load_seen(self.seen_path)

    def _mark_no_candidate_retry_with_state_lock(
        self,
        bangumi_id: int,
        episodes: list[int],
        *,
        state_required: bool,
    ) -> dict[int, int] | None:
        if not episodes:
            return {}
        lock = self._acquire_state_lock_for_enqueue(
            "mark_no_candidate_retry",
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            self.logger.info(
                "Mikan no-candidate state update skipped because state lock is busy. bangumi_id=%s episodes=%s",
                bangumi_id,
                _format_episode_list(episodes),
            )
            return None
        try:
            pending = _load_pending(self.pending_path)
            retry_delays: dict[int, int] = {}
            for episode in episodes:
                retry_delays[episode] = _mark_no_candidate_retry(
                    pending,
                    bangumi_id,
                    episode,
                    self.config.mikan_no_candidate_retry_seconds,
                    getattr(self.config, "mikan_no_candidate_retry_max_seconds", 86400),
                )
            _save_pending(self.pending_path, pending)
            return retry_delays
        finally:
            lock.release()

    def _mark_candidate_review_with_state_lock(
        self,
        bangumi_id: int,
        reasons_by_episode: dict[int, list[str]],
        *,
        state_required: bool,
    ) -> bool:
        if not reasons_by_episode:
            return False
        lock = self._acquire_state_lock_for_enqueue(
            "mark_candidate_review",
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            self.logger.info(
                "Mikan candidate-review state update skipped because state lock is busy. "
                "bangumi_id=%s episodes=%s",
                bangumi_id,
                _format_episode_list(reasons_by_episode),
            )
            return False
        try:
            pending = _load_pending(self.pending_path)
            changed = False
            for episode, reasons in reasons_by_episode.items():
                changed = _mark_candidate_review(
                    pending,
                    bangumi_id,
                    episode,
                    reasons,
                ) or changed
            if changed:
                _save_pending(self.pending_path, pending)
            return changed
        finally:
            lock.release()

    def _queue_selected_release_with_state_lock(
        self,
        release: MikanRelease,
        qbit: QBitClient | None,
        *,
        operation: str,
        state_required: bool,
        unavailable_reason: str,
        add_failed_reason: str,
        replacement: bool,
        queue_lock_held: bool = False,
    ) -> str:
        if qbit is None:
            if not self._release_can_be_queued(release, operation=operation, state_required=state_required):
                return "skipped"
            return self._store_deferred_release_with_state_lock(
                release,
                reason=unavailable_reason,
                operation=operation,
                state_required=state_required,
            )

        queue_lock: VideoLock | None = None
        if not queue_lock_held:
            queue_lock = self._acquire_queue_lock(
                f"{operation}_qbit_add",
                required=False,
                log_busy=False,
                log_acquired=False,
            )
            if queue_lock is None:
                return self._store_deferred_release_with_state_lock(
                    release,
                    reason="queue_busy",
                    operation=operation,
                    state_required=state_required,
                )
        try:
            if not self._release_can_be_queued(release, operation=operation, state_required=state_required):
                return "skipped"
            tags = _queue_tags(self.config.qbit_tags, release.source)
            try:
                qbit.add_url(
                    release.torrent_url,
                    save_path=self.config.qbit_save_path,
                    category=self.config.qbit_category,
                    tags=tags,
                    paused=self.config.qbit_paused,
                )
            except QBitError as exc:
                log_template = (
                    "Failed to queue replacement Mikan release to qBittorrent; stored for later retry: title=%s error=%s"
                    if replacement
                    else "Failed to queue Mikan release to qBittorrent; stored for later retry: title=%s error=%s"
                )
                self.logger.warning(log_template, release.title, exc)
                return self._store_deferred_release_with_state_lock(
                    release,
                    reason=add_failed_reason,
                    operation=operation,
                    state_required=state_required,
                )

            lock = self._acquire_state_lock_for_enqueue(
                operation,
                required=state_required,
                log_busy=state_required,
            )
            if lock is None:
                self.logger.info("Mikan queued release but state update is deferred by busy lock: %s", release.title)
                return "busy"
            try:
                pending = _load_pending(self.pending_path)
                seen = _load_seen(self.seen_path)
                if _release_is_seen(release, seen) and not _release_seen_is_retryable(release, pending):
                    return "skipped"
                seen[release.torrent_url] = _seen_payload(release)
                _save_seen(self.seen_path, seen)
                _mark_pending(pending, release)
                _save_pending(self.pending_path, pending)
            finally:
                lock.release()
        finally:
            if queue_lock is not None:
                queue_lock.release()

        if replacement:
            self.logger.info("Queued replacement Mikan release to qBittorrent: %s", release.title)
        else:
            self.logger.info("Queued Mikan release to qBittorrent: %s", release.title)
        return "queued"

    def _reconcile_verified_history_outputs(self, bangumi_id, episodes, *, season_hint=None, operation, state_required):
        pending = _load_pending(self.pending_path)
        # Historical failed downloads can outlive a later successful subtitle
        # import. Revalidate those indexed targets before starting any torrent.
        historical = [episode for episode in episodes
                      if isinstance(_pending_entry(bangumi_id, episode, pending).get("download_recovery"), dict)]
        if historical:
            mappings = [mapping for mapping in self._series_mappings(cached_only=True)
                        if int(mapping.get("bangumi_id") or 0) == bangumi_id]
            for episode in historical:
                entry = _pending_entry(bangumi_id, episode, pending)
                if _pending_is_terminal_success(entry) or _pending_has_active_release(entry):
                    continue
                targets = _target_videos_from_episode_index(self.config, mappings, episode, season_hint=season_hint)
                if len(targets) != 1:
                    continue
                target = targets[0]
                video_lock = VideoLock(target)
                if not video_lock.acquire():
                    continue
                try:
                    if not _target_has_required_chinese_subtitles(target, verify_config=self.config):
                        continue
                    lock = self._acquire_state_lock_for_enqueue(operation, required=state_required, log_busy=state_required)
                    if lock is None:
                        return False
                    try:
                        current = _load_pending(self.pending_path)
                        live = _pending_entry(bangumi_id, episode, current)
                        if _pending_has_active_release(live):
                            continue
                        live["download_recovery"] = {**live.get("download_recovery", {}),
                            "decision": "SATISFIED_BY_VERIFIED_EXISTING_OUTPUT", "verified_at": _utc_now().isoformat(),
                            "target": str(target), "actual_new_imports": 0}
                        live["completion_kind"] = "verified_existing_output"
                        live["completed_at"] = _utc_now().isoformat()
                        live["last_extracted_count"] = 1  # existing availability counter, not new publications
                        _clear_no_candidate_retry(live)
                        _clear_candidate_review(live)
                        for name in ("last_failure_reason", "last_extract_failed_at", "last_extract_failure_reason", "last_extract_failure_detail"):
                            live.pop(name, None)
                        _save_pending(self.pending_path, current)
                        pending = current
                    finally:
                        lock.release()
                finally:
                    video_lock.release()
        return True

    def _release_can_be_queued(
        self,
        release: MikanRelease,
        *,
        operation: str,
        state_required: bool,
    ) -> bool:
        pending = _load_pending(self.pending_path)
        seen = _load_seen(self.seen_path)
        covered_episodes = release_episode_numbers(release)
        if not self._reconcile_verified_history_outputs(release.bangumi_id, covered_episodes, season_hint=release.season_number, operation=operation, state_required=state_required):
            return False
        pending = _load_pending(self.pending_path)
        if covered_episodes and all(_pending_is_terminal_success(_pending_entry(release.bangumi_id, episode, pending)) for episode in covered_episodes):
            return False
        if covered_episodes and all(
            _has_active_pending(release.bangumi_id, episode, pending)
            for episode in covered_episodes
        ):
            self.logger.info(
                "Mikan release already pending, skip episodes=%s title=%s",
                _format_episode_list(covered_episodes),
                release.title,
            )
            return False
        if covered_episodes and all(
            _has_deferred_release(release.bangumi_id, episode, pending)
            for episode in covered_episodes
        ):
            self.logger.info(
                "Mikan release already stored for later qBittorrent queue, skip episodes=%s title=%s",
                _format_episode_list(covered_episodes),
                release.title,
            )
            return False
        if _release_is_seen(release, seen) and not _release_seen_is_retryable(release, pending):
            self.logger.info("Mikan release already seen, skip: %s", release.title)
            return False
        return True

    def _store_deferred_release_with_state_lock(
        self,
        release: MikanRelease,
        *,
        reason: str,
        operation: str,
        state_required: bool,
    ) -> str:
        lock = self._acquire_state_lock_for_enqueue(
            operation,
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            self.logger.info(
                "Mikan deferred release state update skipped because state lock is busy. title=%s",
                release.title,
            )
            return "busy"
        try:
            pending = _load_pending(self.pending_path)
            _mark_deferred(pending, release, reason=reason)
            _save_pending(self.pending_path, pending)
        finally:
            lock.release()
        self.logger.info("Stored Mikan release for later qBittorrent queue: %s", release.title)
        return "deferred"

    def _qbit(self) -> QBitClient:
        qbit = QBitClient(
            self.config.qbit_base_url,
            self.config.qbit_username,
            self.config.qbit_password,
            timeout_seconds=self.config.qbit_timeout_seconds,
        )
        qbit.login()
        return qbit

    def _queue_deferred_releases(
        self,
        qbit: QBitClient,
        *,
        state_required: bool,
        queue_lock_held: bool,
    ) -> int:
        lock = self._acquire_state_lock_for_enqueue(
            "queue_deferred_releases_snapshot",
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            return 0
        try:
            pending = _load_pending(self.pending_path)
            seen = _load_seen(self.seen_path)
            items = _pending_items(pending)
            deferred: list[dict[str, str]] = []
            queued_keys: set[str] = set()
            seen_info_hashes = _seen_info_hashes(seen)
            pending_changed = False
            for key, entry in list(items.items()):
                if not isinstance(entry, dict) or not _pending_has_deferred_release(entry):
                    continue
                torrent_url = str(entry.get("deferred_torrent_url", ""))
                if not torrent_url:
                    _clear_deferred_release(entry)
                    pending_changed = True
                    continue
                title = str(entry.get("deferred_title") or entry.get("title") or torrent_url)
                info_hash = str(entry.get("deferred_info_hash") or extract_torrent_info_hash(torrent_url) or "").casefold()
                if torrent_url in seen or (info_hash and info_hash in seen_info_hashes):
                    for matching_entry in _pending_entries_for_deferred_url(items, torrent_url):
                        _clear_deferred_release(matching_entry)
                    pending_changed = True
                    self.logger.info("Dropped already-seen deferred Mikan release: key=%s title=%s", key, title)
                    continue
                release_key = info_hash or torrent_url.casefold()
                if release_key in queued_keys:
                    continue
                queued_keys.add(release_key)
                deferred.append(
                    {
                        "key": str(key),
                        "title": title,
                        "torrent_url": torrent_url,
                        "info_hash": info_hash,
                        "source": str(entry.get("deferred_source") or "mikan"),
                    }
                )
            if pending_changed:
                _save_pending(self.pending_path, pending)
        finally:
            lock.release()

        queued = 0
        for item in deferred:
            key = item["key"]
            title = item["title"]
            torrent_url = item["torrent_url"]
            info_hash = item["info_hash"]
            tags = _queue_tags(self.config.qbit_tags, item.get("source"))
            queue_lock: VideoLock | None = None
            if not queue_lock_held:
                queue_lock = self._acquire_queue_lock(
                    "queue_deferred_release_qbit_add",
                    required=False,
                    log_busy=False,
                    log_acquired=False,
                )
                if queue_lock is None:
                    continue
            try:
                current_pending = _load_pending(self.pending_path)
                current_seen = _load_seen(self.seen_path)
                current_items = _pending_items(current_pending)
                if (
                    torrent_url in current_seen
                    or (info_hash and info_hash in _seen_info_hashes(current_seen))
                    or not _pending_entries_for_deferred_url(current_items, torrent_url)
                ):
                    continue
                try:
                    qbit.add_url(
                        torrent_url,
                        save_path=self.config.qbit_save_path,
                        category=self.config.qbit_category,
                        tags=tags,
                        paused=self.config.qbit_paused,
                    )
                except QBitError as exc:
                    self.logger.warning("Deferred Mikan release still cannot be queued: key=%s title=%s error=%s", key, title, exc)
                    continue

                lock = self._acquire_state_lock_for_enqueue(
                    "queue_deferred_releases_state_update",
                    required=state_required,
                    log_busy=state_required,
                )
                if lock is None:
                    self.logger.info("Deferred Mikan release queued but state update is delayed by busy lock: key=%s title=%s", key, title)
                    continue
                queued_at = _utc_now().isoformat()
                try:
                    pending = _load_pending(self.pending_path)
                    seen = _load_seen(self.seen_path)
                    items = _pending_items(pending)
                    if torrent_url in seen or (info_hash and info_hash in _seen_info_hashes(seen)):
                        continue
                    matching_entries = _pending_entries_for_deferred_url(items, torrent_url)
                    if not matching_entries:
                        continue
                    seen[torrent_url] = _seen_payload_from_pending_entry(matching_entries[0])
                    for matching_entry in matching_entries:
                        matching_entry.update(
                            {
                                "torrent_url": torrent_url,
                                "title": str(matching_entry.get("deferred_title") or title),
                                "queued_at": queued_at,
                                "source": matching_entry.get("deferred_source") or "mikan",
                                "source_page": matching_entry.get("deferred_source_page"),
                                "info_hash": matching_entry.get("deferred_info_hash") or info_hash or None,
                                "seeders": matching_entry.get("deferred_seeders"),
                            }
                        )
                        _clear_deferred_release(matching_entry)
                    _save_seen(self.seen_path, seen)
                    _save_pending(self.pending_path, pending)
                finally:
                    lock.release()
            finally:
                if queue_lock is not None:
                    queue_lock.release()
            queued += 1
            self.logger.info("Queued deferred Mikan release to qBittorrent: %s", title)
        if queued:
            self.logger.info("Mikan deferred queue drain complete. queued=%s", queued)
        return queued

    def _series_mappings(self, *, cached_only: bool = False, deadline_monotonic: float | None = None) -> list[dict[str, object]]:
        if deadline_monotonic is not None:
            # Revisit persisted profiles each watch so a partial cold lookup or
            # a newly indexed series can continue; do not freeze a partial list
            # into the lifetime-only cache.
            resolved = resolve_mikan_series_mappings(self.config, self.logger, deadline_monotonic=deadline_monotonic)
        elif cached_only:
            resolved = resolve_mikan_series_mappings(self.config, self.logger, cached_only=True)
        else:
            if self._resolved_series_mappings is None:
                self._resolved_series_mappings = resolve_mikan_series_mappings(self.config, self.logger)
            resolved = self._resolved_series_mappings
        mappings = [dict(mapping) for mapping in resolved]
        try:
            locked = locked_series_source_mappings(self.config, source="mikan")
        except sqlite3.Error as exc:
            self.logger.warning("Unable to load manually locked Mikan mappings: %s", exc)
            locked = []
        if not locked:
            return mappings
        locked_keys = {
            (_coerce_int(row.get("source_id")), int(row.get("season") or 0))
            for row in locked
        }
        retained = [
            mapping
            for mapping in mappings
            if (
                _coerce_int(mapping.get("bangumi_id")),
                int(mapping.get("season") or mapping.get("season_number") or 0),
            )
            not in locked_keys
        ]
        manual = [
            {
                "bangumi_id": int(row["source_id"]),
                "path": str(row["series_path"]),
                "season": int(row.get("season") or 0),
                "match": [],
                "manual_locked": True,
                "confidence": float(row.get("confidence") or 1.0),
            }
            for row in locked
            if _coerce_int(row.get("source_id")) is not None
        ]
        return manual + retained

    def _library_scan_plan(
        self,
        mappings: list[dict[str, object]],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[list[dict[str, object]], bool]:
        """Use the persistent index and reconcile at most N roots per cooled run."""

        self._ensure_episode_index(mappings)
        index_covers_all = _mikan_episode_index_covers_mappings(self.config, mappings)
        due_mappings = _mikan_episode_index_due_mappings(self.config, mappings)
        if index_covers_all and not due_mappings:
            return list(mappings), True

        fallback_interval = max(
            1,
            int(
                getattr(
                    self.config,
                    "mikan_library_fallback_scan_interval_seconds",
                    3600,
                )
                or 3600
            ),
        )
        now_monotonic = time.monotonic()
        if (
            now_monotonic < self._fallback_library_scan_next_at
            or not _mikan_episode_index_reconcile_due(self.config)
        ):
            self.logger.warning(
                "Mikan library discovery deferred. reason=episode_index_unavailable_cooldown "
                "fallback_runs=%s fallback_roots=%s retry_after_seconds=%s",
                self._fallback_library_scan_runs,
                self._fallback_library_scan_roots,
                max(1, int(self._fallback_library_scan_next_at - now_monotonic)),
            )
            return (list(mappings), True) if index_covers_all else ([], False)

        fallback_limit = max(
            1,
            int(
                getattr(
                    self.config,
                    "mikan_library_fallback_scan_max_series_per_cycle",
                    8,
                )
                or 8
            ),
        )
        selected = due_mappings[:fallback_limit]
        if not selected:
            return (list(mappings), True) if index_covers_all else ([], False)
        self._fallback_library_scan_next_at = now_monotonic + fallback_interval
        self._fallback_library_scan_runs += 1
        self._fallback_library_scan_roots += len(selected)

        refresh_lock = _mikan_episode_index_lock(self.config)
        refresh_succeeded = False
        lock_acquired = False
        try:
            lock_acquired = bool(refresh_lock.acquire())
            if lock_acquired:
                # Recheck the persistent deadline after taking the lock so
                # concurrent worker processes cannot each scan another batch.
                if _mikan_episode_index_reconcile_due(self.config):
                    if deadline_monotonic is None:
                        _refresh_mikan_episode_index(self.config, self.logger, selected)
                    else:
                        _refresh_mikan_episode_index(self.config, self.logger, selected, deadline_monotonic=deadline_monotonic)
                    refresh_succeeded = deadline_monotonic is None or time.monotonic() < deadline_monotonic
            else:
                self.logger.warning(
                    "Mikan episode index incremental reconciliation is owned by another worker."
                )
        except (OSError, sqlite3.Error) as exc:
            self.logger.warning(
                "Mikan episode index incremental reconciliation failed; using bounded filesystem fallback: %s",
                exc,
            )
        finally:
            if lock_acquired:
                refresh_lock.release()

        if refresh_succeeded:
            index_covers_all = _mikan_episode_index_covers_mappings(self.config, mappings)
        self.logger.warning(
            "Mikan library discovery using bounded incremental reconciliation. "
            "reason=episode_index_incremental_bounded_reconcile fallback_run=%s "
            "selected_roots=%s deferred_roots=%s roots_total=%s cooldown_seconds=%s",
            self._fallback_library_scan_runs,
            len(selected),
            max(0, len(mappings) - len(selected)),
            self._fallback_library_scan_roots,
            fallback_interval,
        )
        if index_covers_all:
            return list(mappings), True
        # A successful bounded reconciliation makes these selected roots safe
        # to query through the persistent index (including zero-video roots).
        # On persistence failure the same bounded roots get one direct scan.
        return selected, refresh_succeeded

    def _consume_completed_state_update_request_unlocked(self) -> dict[str, Any]:
        request_path = _mikan_completed_state_update_request_path(self.config)
        request = _load_request_file(request_path)
        records = request.get("records")
        if not isinstance(records, list):
            records = []
        series_mappings = self._series_mappings()
        pending = _load_pending(self.pending_path)
        pending_changed = False
        replacement_targets: list[MikanReplacementTarget] = []
        applied = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            torrent = _torrent_from_request_payload(record.get("torrent"))
            result = _extract_result_from_request_payload(record.get("result"))
            if torrent is None:
                continue
            if result.extracted_count > 0:
                if _clear_completed_pending_entries(
                    pending,
                    torrent,
                    series_mappings,
                    extracted_count=result.extracted_count,
                ):
                    pending_changed = True
            elif result.retryable:
                if _mark_completed_pending_extract_deferred(
                    pending,
                    torrent,
                    series_mappings,
                    deferred_reason=result.failure_reason,
                    deferred_detail=result.failure_detail,
                    failure_context=result.failure_context,
                ):
                    pending_changed = True
            else:
                failed_targets = _mark_completed_pending_extract_failed(
                    pending,
                    torrent,
                    series_mappings,
                    failure_reason=result.failure_reason,
                    failure_detail=result.failure_detail,
                    failure_context=result.failure_context,
                    subtitle_diagnostics=result.subtitle_diagnostics,
                )
                if failed_targets:
                    pending_changed = True
                    replacement_targets.extend(failed_targets)
            applied += 1
        if pending_changed:
            _save_pending(self.pending_path, pending)
        request_path.unlink(missing_ok=True)
        targets = _unique_replacement_targets(replacement_targets)
        result = {
            "deferred": False,
            "request": request,
            "applied": applied,
            "replacement_targets": [
                {"bangumi_id": target.bangumi_id, "episode": target.episode}
                for target in targets
            ],
        }
        self.logger.warning(
            "Deferred Mikan completed state update applied. applied=%s replacement_targets=%s",
            applied,
            _format_replacement_targets(targets),
        )
        return result

    def _acquire_operation_lock(
        self,
        operation: str,
        *,
        required: bool,
        wait_seconds: float = 0.0,
        log_busy: bool = True,
        log_acquired: bool = True,
    ) -> VideoLock | None:
        return self._acquire_lock(
            operation,
            lock_factory=lambda: _mikan_operation_lock(self.config),
            required=required,
            wait_seconds=wait_seconds,
            log_busy=log_busy,
            log_acquired=log_acquired,
        )

    def _acquire_queue_lock(
        self,
        operation: str,
        *,
        required: bool,
        wait_seconds: float = 0.0,
        log_busy: bool = True,
        log_acquired: bool = True,
    ) -> VideoLock | None:
        return self._acquire_lock(
            operation,
            lock_factory=lambda: _mikan_queue_lock(self.config),
            required=required,
            wait_seconds=wait_seconds,
            log_busy=log_busy,
            log_acquired=log_acquired,
        )

    def _acquire_state_lock_for_enqueue(
        self,
        operation: str,
        *,
        required: bool,
        log_busy: bool = False,
    ) -> VideoLock | None:
        return self._acquire_operation_lock(
            operation,
            required=required,
            wait_seconds=_mikan_operation_lock_wait_seconds(self.config) if required else 0.0,
            log_busy=log_busy,
            log_acquired=False,
        )

    def _acquire_lock(
        self,
        operation: str,
        *,
        lock_factory,
        required: bool,
        wait_seconds: float = 0.0,
        log_busy: bool = True,
        log_acquired: bool = True,
    ) -> VideoLock | None:
        wait_seconds = max(0.0, float(wait_seconds or 0.0))
        deadline = time.monotonic() + wait_seconds
        lock = lock_factory()
        waiting_logged = False

        while True:
            if lock.acquire():
                if log_acquired:
                    self.logger.info("Mikan operation lock acquired: operation=%s path=%s", operation, lock.lock_path)
                return lock

            if not required:
                message = f"Mikan operation already running; skip operation={operation} lock={lock.lock_path}"
                if log_busy:
                    self.logger.warning(message)
                return None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            if not waiting_logged:
                self.logger.warning(
                    "Mikan operation already running; waiting up to %.0fs operation=%s lock=%s",
                    wait_seconds,
                    operation,
                    lock.lock_path,
                )
                waiting_logged = True
            time.sleep(min(MIKAN_OPERATION_LOCK_POLL_SECONDS, remaining))
            lock = lock_factory()

        message = f"Mikan operation already running; skip operation={operation} lock={lock.lock_path}"
        if wait_seconds > 0:
            message = (
                f"Mikan operation still running after waiting {wait_seconds:.0f}s; "
                f"skip operation={operation} lock={lock.lock_path}"
            )
        if required:
            raise MikanWorkerError(message)

    def _expire_stalled_pending(
        self,
        qbit: QBitClient,
        pending_override: dict[str, Any] | None = None,
        *,
        state_required: bool = True,
    ) -> int:
        return len(
            self._expire_stalled_pending_targets(
                qbit,
                pending_override,
                state_required=state_required,
            )
        )

    def _expire_stalled_pending_targets(
        self,
        qbit: QBitClient,
        pending_override: dict[str, Any] | None = None,
        *,
        state_required: bool = True,
        queue_lock_held: bool = False,
        torrents_override: list[QBitTorrent] | None = None,
        progress_already_synced: bool = False,
        series_mappings_override: list[dict[str, object]] | None = None,
    ) -> list[MikanReplacementTarget]:
        if pending_override is not None:
            _save_pending(self.pending_path, pending_override)
        pending_snapshot = _load_pending(self.pending_path)
        persisted_unhealthy = _load_qbit_unhealthy_since(self.config)
        for torrent_key, value in persisted_unhealthy.items():
            self._qbit_unhealthy_since.setdefault(torrent_key, value)

        items = _pending_items(pending_snapshot)
        primary_tag = self.config.qbit_tags[0] if self.config.qbit_tags else None
        torrents_value = (
            torrents_override
            if torrents_override is not None
            else qbit.list_torrents(tag=primary_tag, category=self.config.qbit_category)
        )
        torrents = torrents_value if isinstance(torrents_value, list) else []
        now = _utc_now()
        now_timestamp = now.timestamp()
        expired: list[dict[str, Any]] = []
        orphan_expired: list[dict[str, Any]] = []
        progress_updates: list[dict[str, Any]] = []
        stall_timeout_seconds = getattr(
            self.config,
            "mikan_download_stall_timeout_seconds",
            600,
        )
        start_timeout_seconds = int(getattr(self.config, "mikan_download_start_timeout_seconds", 180) or 180)
        metadata_timeout_seconds = int(
            getattr(self.config, "mikan_download_metadata_timeout_seconds", 300) or 300
        )
        unhealthy_timeout_seconds = int(
            getattr(self.config, "mikan_download_unhealthy_timeout_seconds", start_timeout_seconds)
            or start_timeout_seconds
        )
        max_eta_seconds = int(getattr(self.config, "mikan_download_max_eta_seconds", 86400) or 86400)

        present_torrent_keys = {torrent.hash or torrent.name for torrent in torrents}
        for torrent_key in list(self._qbit_unhealthy_since):
            if torrent_key not in present_torrent_keys:
                self._qbit_unhealthy_since.pop(torrent_key, None)

        def matured_unhealthy_reason(torrent: QBitTorrent) -> str:
            if _is_completed(torrent) or _torrent_waiting_for_download_slot(torrent):
                self._qbit_unhealthy_since.pop(torrent.hash or torrent.name, None)
                return ""
            if torrent.dlspeed <= 0:
                reason = "zero speed"
            elif torrent.eta is not None and torrent.eta > max_eta_seconds:
                reason = "eta too long"
            else:
                reason = ""
            torrent_key = torrent.hash or torrent.name
            if not reason:
                self._qbit_unhealthy_since.pop(torrent_key, None)
                return ""
            previous = self._qbit_unhealthy_since.get(torrent_key)
            if previous is None or previous[0] != reason:
                self._qbit_unhealthy_since[torrent_key] = (reason, now_timestamp)
                return ""
            if now_timestamp - previous[1] < unhealthy_timeout_seconds:
                return ""
            return reason

        deleted_hashes: set[str] = set()
        retained_hashes = {
            str(info_hash).casefold()
            for entry in items.values() if isinstance(entry, dict)
            for info_hash in entry.get("retained_partial_hashes", [])
        }
        active_pending_hashes: set[str] = set()
        for entry in items.values():
            if not isinstance(entry, dict) or not _pending_has_active_release(entry):
                continue
            for torrent in _torrents_for_pending(entry, torrents):
                if torrent.hash:
                    active_pending_hashes.add(torrent.hash)
        for key, entry in list(items.items()):
            if not _pending_has_active_release(entry):
                continue
            age_seconds = (now - _parse_pending_time(entry.get("queued_at"))).total_seconds()

            matching_torrents = _torrents_for_pending(entry, torrents)
            completed = any(_is_completed(torrent) for torrent in matching_torrents)
            if completed:
                continue

            # qB backpressure, rechecking, moving, and operator pauses remain
            # healthy even after pieces have already been downloaded.
            if matching_torrents and all(_torrent_waiting_for_download_slot(torrent) for torrent in matching_torrents):
                for torrent in matching_torrents:
                    self._qbit_unhealthy_since.pop(torrent.hash or torrent.name, None)
                continue

            started = any(_torrent_has_started(torrent) for torrent in matching_torrents)
            reason = ""
            if not started:
                # qBittorrent's own queue is healthy backpressure. A torrent
                # waiting for an active-download slot must not be mistaken for
                # a source that failed to start and churned every 60 seconds.
                if matching_torrents and all(_torrent_waiting_for_download_slot(torrent) for torrent in matching_torrents):
                    continue
                start_grace_seconds = (
                    metadata_timeout_seconds
                    if matching_torrents and any(_torrent_waiting_for_metadata(torrent) for torrent in matching_torrents)
                    else start_timeout_seconds
                )
                if age_seconds < start_grace_seconds:
                    continue
                reason = "did not start"
            else:
                if progress_already_synced:
                    eta_limited = [
                        torrent
                        for torrent in matching_torrents
                        if torrent.dlspeed > 0 and torrent.eta is not None and torrent.eta > max_eta_seconds
                    ]
                    if eta_limited:
                        matured_eta = any(matured_unhealthy_reason(torrent) == "eta too long" for torrent in eta_limited)
                        if not matured_eta:
                            continue
                        reason = "eta too long"
                    elif any(torrent.dlspeed > 0 for torrent in matching_torrents):
                        for torrent in matching_torrents:
                            matured_unhealthy_reason(torrent)
                        continue
                if not progress_already_synced and _pending_progress_is_active(entry, matching_torrents, now):
                    progress_updates.append(
                        {
                            "key": key,
                            "torrent_url": str(entry.get("torrent_url", "")),
                            "last_downloaded": entry.get("last_downloaded"),
                            "last_progress": entry.get("last_progress"),
                            "last_progress_at": entry.get("last_progress_at"),
                        }
                    )
                    continue
                if not reason:
                    idle_seconds = (now - _parse_pending_time(entry.get("last_progress_at") or entry.get("queued_at"))).total_seconds()
                    if idle_seconds < stall_timeout_seconds:
                        continue
                    reason = "stalled"

            hashes = list(dict.fromkeys(torrent.hash for torrent in matching_torrents if torrent.hash))
            partial_hashes = {
                torrent.hash.casefold() for torrent in matching_torrents
                if torrent.hash and _torrent_has_started(torrent)
            }
            retained_hashes.update(partial_hashes)
            hashes_to_delete = [
                info_hash for info_hash in hashes
                if info_hash not in deleted_hashes and info_hash.casefold() not in retained_hashes
            ]
            if hashes_to_delete:
                queue_lock: VideoLock | None = None
                if not queue_lock_held:
                    queue_lock = self._acquire_queue_lock(
                        "expire_stalled_pending_qbit_delete",
                        required=False,
                        log_busy=False,
                        log_acquired=False,
                    )
                    if queue_lock is None:
                        continue
                try:
                    qbit.delete_torrents(
                        hashes_to_delete,
                        # Timeout is not proof that downloaded pieces are bad.
                        delete_files=False,
                    )
                    deleted_hashes.update(hashes_to_delete)
                finally:
                    if queue_lock is not None:
                        queue_lock.release()

            failed_url = str(entry.get("torrent_url", ""))
            failed_info_hash = str(
                entry.get("info_hash")
                or entry.get("last_qbit_hash")
                or (hashes[0] if hashes else "")
                or extract_torrent_info_hash(failed_url)
                or ""
            ).casefold()
            if failed_url:
                expired.append(
                    {
                        "key": key,
                        "failed_url": failed_url,
                        "failed_info_hash": failed_info_hash,
                        "reason": reason,
                        "timed_out_at": now.isoformat(),
                        "retained_partial_hashes": sorted(partial_hashes),
                    }
                )
        if expired:
            reason_counts: dict[str, int] = {}
            for item in expired:
                reason_text = str(item.get("reason") or "unknown")
                reason_counts[reason_text] = reason_counts.get(reason_text, 0) + 1
            self.logger.warning(
                "Mikan unhealthy pending torrents switching source. count=%s reasons=%s deleted_torrents=%s sample_keys=%s",
                len(expired),
                ",".join(f"{reason}:{count}" for reason, count in sorted(reason_counts.items())),
                len(deleted_hashes),
                ",".join(str(item.get("key") or "") for item in expired[:10]),
            )

        orphan_candidates: list[tuple[QBitTorrent, str, MikanUntrackedTorrentResolution]] = []
        preserved_untrusted: list[tuple[QBitTorrent, str]] = []
        active_preserve_warning_keys: set[str] = set()
        if progress_already_synced:
            for torrent in torrents:
                if (not torrent.hash or torrent.hash in active_pending_hashes
                        or torrent.hash in deleted_hashes or torrent.hash.casefold() in retained_hashes):
                    continue
                reason = matured_unhealthy_reason(torrent)
                if reason:
                    resolution = _resolve_untracked_torrent_targets(
                        torrent,
                        pending_snapshot,
                        series_mappings_override or [],
                    )
                    if not resolution.trusted:
                        warning_key = f"{torrent.hash or torrent.name}:{reason}".casefold()
                        active_preserve_warning_keys.add(warning_key)
                        last_warning_at = self._untracked_preserve_warning_at.get(warning_key, 0.0)
                        if now_timestamp - last_warning_at >= _UNTRACKED_PRESERVE_WARNING_INTERVAL_SECONDS:
                            self._untracked_preserve_warning_at[warning_key] = now_timestamp
                            preserved_untrusted.append((torrent, reason))
                        continue
                    orphan_candidates.append((torrent, reason, resolution))

            self._untracked_preserve_warning_at = {
                key: logged_at
                for key, logged_at in self._untracked_preserve_warning_at.items()
                if key in active_preserve_warning_keys
            }
        if preserved_untrusted:
            reason_counts: dict[str, int] = {}
            for _torrent, reason in preserved_untrusted:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            sample_names = " | ".join(torrent.name[:160] for torrent, _reason in preserved_untrusted[:5])
            self.logger.warning(
                "Preserved unhealthy untracked qBittorrent torrent(s) because no trusted series mapping exists. count=%s reasons=%s sample=%s log_interval_seconds=%s",
                len(preserved_untrusted),
                ",".join(f"{reason}:{count}" for reason, count in sorted(reason_counts.items())),
                sample_names,
                int(_UNTRACKED_PRESERVE_WARNING_INTERVAL_SECONDS),
            )

        if orphan_candidates:
            orphan_hashes = list(
                dict.fromkeys(torrent.hash for torrent, _reason, _resolution in orphan_candidates
                              if torrent.hash and not _torrent_has_started(torrent))
            )
            queue_lock: VideoLock | None = None
            if not queue_lock_held:
                queue_lock = self._acquire_queue_lock(
                    "expire_untracked_qbit_torrents_delete",
                    required=False,
                    log_busy=False,
                    log_acquired=False,
                )
            if queue_lock_held or queue_lock is not None:
                try:
                    if orphan_hashes:
                        qbit.delete_torrents(orphan_hashes, delete_files=False)
                    deleted_hashes.update(orphan_hashes)
                    for torrent, reason, resolution in orphan_candidates:
                        targets = list(resolution.targets)
                        orphan_expired.append(
                            {
                                "torrent": torrent,
                                "reason": reason,
                                "timed_out_at": now.isoformat(),
                                "targets": targets,
                            }
                        )
                        self._qbit_unhealthy_since.pop(torrent.hash or torrent.name, None)
                        self.logger.warning(
                            "Mikan untracked qBittorrent source expired (partial data retained) after %ss unhealthy. reason=%s eta=%s speed=%s targets=%s torrent=%s",
                            unhealthy_timeout_seconds,
                            reason,
                            torrent.eta,
                            torrent.dlspeed,
                            _format_replacement_targets(targets),
                            torrent.name,
                        )
                finally:
                    if queue_lock is not None:
                        queue_lock.release()

        if not expired and not orphan_expired and not progress_updates:
            _save_qbit_unhealthy_since(self.config, self._qbit_unhealthy_since)
            return []

        lock = self._acquire_state_lock_for_enqueue(
            "expire_stalled_pending_state_update",
            required=state_required,
            log_busy=state_required,
        )
        if lock is None:
            _save_qbit_unhealthy_since(self.config, self._qbit_unhealthy_since)
            return []
        changed = False
        replacement_targets: list[MikanReplacementTarget] = []
        try:
            pending = _load_pending(self.pending_path)
            current_items = _pending_items(pending)
            for update in progress_updates:
                entry = current_items.get(update["key"])
                if not isinstance(entry, dict) or str(entry.get("torrent_url", "")) != update["torrent_url"]:
                    continue
                entry["last_downloaded"] = update["last_downloaded"]
                entry["last_progress"] = update["last_progress"]
                entry["last_progress_at"] = update["last_progress_at"]
                changed = True
            for item in expired:
                entry = current_items.get(item["key"])
                if not isinstance(entry, dict) or str(entry.get("torrent_url", "")) != item["failed_url"]:
                    continue
                failed_urls = _pending_failed_urls(entry)
                if item["failed_url"] and item["failed_url"] not in failed_urls:
                    failed_urls.append(item["failed_url"])
                entry["failed_urls"] = failed_urls
                failed_info_hashes = list(_raw_pending_failed_info_hashes(entry))
                if item["failed_info_hash"] and item["failed_info_hash"] not in failed_info_hashes:
                    failed_info_hashes.append(item["failed_info_hash"])
                entry["failed_info_hashes"] = failed_info_hashes
                entry["retained_partial_hashes"] = sorted(set(
                    entry.get("retained_partial_hashes", [])
                ) | set(item["retained_partial_hashes"]))
                _archive_active_release(entry, "last_failed")
                _clear_active_pending_release(entry)
                entry["timed_out_at"] = item["timed_out_at"]
                entry["last_failure_reason"] = item["reason"]
                replacement_targets.extend(_replacement_targets_from_pending_entry(entry))
                changed = True
            for item in orphan_expired:
                torrent = item["torrent"]
                for target in item["targets"]:
                    entry = _pending_entry(target.bangumi_id, target.episode, pending)
                    failed_info_hashes = list(_raw_pending_failed_info_hashes(entry))
                    failed_info_hash = str(torrent.hash or "").casefold()
                    if failed_info_hash and failed_info_hash not in failed_info_hashes:
                        failed_info_hashes.append(failed_info_hash)
                    entry["failed_info_hashes"] = failed_info_hashes

                    if _torrent_has_started(torrent):
                        entry["retained_partial_hashes"] = sorted(set(
                            entry.get("retained_partial_hashes", [])
                        ) | {failed_info_hash})

                    deferred_title = str(entry.get("deferred_title") or "")
                    if deferred_title and _torrents_for_pending({"title": deferred_title}, [torrent]):
                        deferred_url = str(entry.get("deferred_torrent_url") or "")
                        failed_urls = _pending_failed_urls(entry)
                        if deferred_url and deferred_url not in failed_urls:
                            failed_urls.append(deferred_url)
                        entry["failed_urls"] = failed_urls
                        _clear_deferred_release(entry)

                    entry["last_failed_title"] = torrent.name
                    entry["last_failed_info_hash"] = failed_info_hash or None
                    entry["last_qbit_hash"] = failed_info_hash or None
                    entry["timed_out_at"] = item["timed_out_at"]
                    entry["last_failure_reason"] = item["reason"]
                    replacement_targets.append(target)
                    changed = True
            if changed:
                _save_pending(self.pending_path, pending)
        finally:
            lock.release()
        if pending_override is not None:
            pending_override.clear()
            pending_override.update(_load_pending(self.pending_path))
        _save_qbit_unhealthy_since(self.config, self._qbit_unhealthy_since)
        if changed:
            return _unique_replacement_targets(replacement_targets)
        return []


def _select_releases_for_bangumi(
    releases: list[MikanRelease],
    bangumi_id: int,
    config: AppConfig,
    logger: logging.Logger,
    *,
    missing_episodes: set[int] | None,
) -> list[MikanRelease]:
    if missing_episodes is None:
        return select_preferred_releases(
            releases,
            max_items=config.mikan_max_items_per_bangumi,
            prefer_keywords=config.mikan_prefer_keywords,
            reject_keywords=config.mikan_reject_keywords,
            require_extractable=config.mikan_require_extractable_subtitle,
    )

    if not missing_episodes:
        logger.debug("Mikan bangumi_id=%s has no local episodes missing official Chinese subtitles.", bangumi_id)
        return []

    selected = select_preferred_releases_for_episodes(
        releases,
        episodes=missing_episodes,
        prefer_keywords=config.mikan_prefer_keywords,
        reject_keywords=config.mikan_reject_keywords,
        require_extractable=config.mikan_require_extractable_subtitle,
    )
    found_episodes = {
        episode
        for release in selected
        for episode in release_episode_numbers(release)
    }
    not_found = sorted(missing_episodes - found_episodes)
    if not_found:
        logger.warning(
            "Mikan bangumi_id=%s has no matching extractable Chinese release for local episodes: %s",
            bangumi_id,
            _format_episode_list(not_found),
        )
    logger.debug(
        "Mikan bangumi_id=%s selected %s release(s) for missing local episodes: %s",
        bangumi_id,
        len(selected),
        _format_episode_list(sorted(missing_episodes)),
    )
    return selected


def _bangumi_ids_for_run(config: AppConfig, series_mappings: list[dict[str, object]]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for mapping in series_mappings:
        bangumi_id = int(mapping["bangumi_id"])
        if bangumi_id not in seen:
            ids.append(bangumi_id)
            seen.add(bangumi_id)
    for bangumi_id in config.mikan_bangumi_ids:
        bangumi_id = int(bangumi_id)
        if bangumi_id not in seen:
            ids.append(bangumi_id)
            seen.add(bangumi_id)
    return ids


def _series_mappings_by_bangumi(series_mappings: list[dict[str, object]]) -> dict[int, list[dict[str, object]]]:
    grouped: dict[int, list[dict[str, object]]] = {}
    for mapping in series_mappings:
        bangumi_id = int(mapping["bangumi_id"])
        grouped.setdefault(bangumi_id, []).append(mapping)
    return grouped


def _library_scan_series_mappings(
    config: AppConfig,
    logger: logging.Logger,
    series_mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    mappings = list(series_mappings)
    max_series = int(getattr(config, "mikan_library_scan_max_series_per_cycle", 80) or 0)
    if max_series <= 0 or len(mappings) <= max_series:
        if bool(getattr(config, "mikan_library_scan_recent_first", True)):
            mappings.sort(key=_recent_library_scan_sort_key)
        return mappings

    selected: list[dict[str, object]] = []
    selected_keys: set[str] = set()
    queued_selected = 0
    recent_selected = 0

    for mapping in _queued_library_scan_mappings(config, mappings):
        if len(selected) >= max_series:
            break
        if _append_unique_mapping(selected, selected_keys, mapping):
            queued_selected += 1

    if bool(getattr(config, "mikan_library_scan_recent_first", True)):
        recent_limit = min(
            max_series - len(selected),
            int(getattr(config, "mikan_library_scan_recent_series_per_cycle", 20) or 0),
        )
        for mapping in sorted(mappings, key=_recent_library_scan_sort_key)[:recent_limit]:
            if _append_unique_mapping(selected, selected_keys, mapping):
                recent_selected += 1

    stable_mappings = sorted(mappings, key=lambda mapping: str(mapping.get("path", "")).casefold())
    rotating_limit = max_series - len(selected)
    rotate_offset = _library_scan_rotate_offset(len(stable_mappings), rotating_limit, config)
    for mapping in _iter_rotating(stable_mappings, rotate_offset):
        if len(selected) >= max_series:
            break
        _append_unique_mapping(selected, selected_keys, mapping)

    logger.info(
        "Mikan library scan limited total_series=%s selected=%s queued=%s recent=%s rotating=%s deferred=%s",
        len(mappings),
        len(selected),
        queued_selected,
        recent_selected,
        len(selected) - recent_selected - queued_selected,
        len(mappings) - len(selected),
    )
    return selected


def _mikan_episode_index_ttl_seconds(config: AppConfig) -> int:
    value = getattr(config, "mikan_episode_index_ttl_seconds", 21600)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 21600
    return max(1, parsed)


def _mikan_episode_index_is_fresh(config: AppConfig) -> bool:
    ttl_seconds = _mikan_episode_index_ttl_seconds(config)
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        refreshed_row = conn.execute(
            "SELECT value FROM mikan_state_meta WHERE key = 'episode_index_refreshed_at'"
        ).fetchone()
        if refreshed_row is None:
            return False
        indexed_count = int(conn.execute("SELECT COUNT(*) FROM anime_episode_index").fetchone()[0] or 0)
        if indexed_count <= 0:
            return False
        refreshed_at = float(refreshed_row[0] or 0)
        return refreshed_at > 0 and (time.time() - refreshed_at) < ttl_seconds
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        if conn is not None:
            conn.close()


def _mikan_episode_index_mapping_key(
    mapping: Mapping[str, object],
) -> tuple[int, str] | None:
    bangumi_id = _coerce_int(mapping.get("bangumi_id"))
    raw_path = str(mapping.get("path") or "").strip()
    if bangumi_id is None or not raw_path:
        return None
    return int(bangumi_id), str(_safe_resolve(Path(raw_path)))


def _mikan_episode_index_mapping_timestamps(
    config: AppConfig,
) -> dict[tuple[int, str], float]:
    """Read durable per-root timestamps, including legacy index-only rows."""

    conn: sqlite3.Connection | None = None
    result: dict[tuple[int, str], float] = {}
    try:
        conn = _mikan_state_connect(config)
        for bangumi_id, series_path, scanned_at in conn.execute(
            "SELECT bangumi_id, series_path, scanned_at FROM anime_episode_index_roots"
        ).fetchall():
            key = (int(bangumi_id), str(_safe_resolve(Path(str(series_path)))))
            result[key] = max(result.get(key, 0.0), float(scanned_at or 0))
        for bangumi_id, series_path, updated_at in conn.execute(
            """
            SELECT bangumi_id, series_path, MAX(updated_at)
            FROM anime_episode_index
            GROUP BY bangumi_id, series_path
            """
        ).fetchall():
            key = (int(bangumi_id), str(_safe_resolve(Path(str(series_path)))))
            result[key] = max(result.get(key, 0.0), float(updated_at or 0))
        return result
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {}
    finally:
        if conn is not None:
            conn.close()


def _mikan_episode_index_covers_mappings(
    config: AppConfig,
    mappings: list[dict[str, object]],
) -> bool:
    keys = [
        key
        for mapping in mappings
        if (key := _mikan_episode_index_mapping_key(mapping)) is not None
    ]
    if not keys:
        return False
    timestamps = _mikan_episode_index_mapping_timestamps(config)
    return all(timestamps.get(key, 0.0) > 0 for key in keys)


def _mikan_episode_index_due_mappings(
    config: AppConfig,
    mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    timestamps = _mikan_episode_index_mapping_timestamps(config)
    cutoff = time.time() - _mikan_episode_index_ttl_seconds(config)
    candidates: list[tuple[float, str, dict[str, object]]] = []
    for mapping in mappings:
        key = _mikan_episode_index_mapping_key(mapping)
        if key is None:
            continue
        scanned_at = float(timestamps.get(key, 0.0))
        if scanned_at <= 0 or scanned_at <= cutoff:
            candidates.append((scanned_at, key[1].casefold(), mapping))
    return [mapping for _timestamp, _path, mapping in sorted(candidates, key=lambda row: row[:2])]


def _mikan_episode_index_reconcile_due(config: AppConfig) -> bool:
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        row = conn.execute(
            "SELECT value FROM mikan_state_meta WHERE key='episode_index_next_reconcile_at'"
        ).fetchone()
        return row is None or float(row[0] or 0) <= time.time()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return True
    finally:
        if conn is not None:
            conn.close()


def _refresh_mikan_episode_index(
    config: AppConfig,
    logger: logging.Logger,
    series_mappings: list[dict[str, object]],
    *,
    deadline_monotonic: float | None = None,
) -> int:
    """Incrementally replace only the selected roots in the persistent index."""

    now = time.time()
    rows: list[tuple[int, int, int | None, str, str, float]] = []
    scanned_roots: list[tuple[int, str, float]] = []
    for mapping in series_mappings:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
        bangumi_id = _coerce_int(mapping.get("bangumi_id"))
        if bangumi_id is None:
            continue
        root = Path(str(mapping.get("path") or ""))
        if not root.exists():
            logger.warning("Mikan episode index skipped missing series path: bangumi_id=%s path=%s", bangumi_id, root)
            continue
        resolved_root = str(_safe_resolve(root))
        root_rows = []
        videos = _find_video_files(root, config.video_extensions) if deadline_monotonic is None else _find_video_files(
            root, config.video_extensions, cancelled=lambda: time.monotonic() >= deadline_monotonic,
        )
        for video in videos:
            episode = extract_episode_number(video.name)
            if episode is None:
                continue
            root_rows.append(
                (
                    bangumi_id,
                    episode,
                    _season_number_for_video(video),
                    str(_safe_resolve(video)),
                    resolved_root,
                    now,
                )
            )
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break  # Never replace a valid index with a partially scanned root.
        rows.extend(root_rows)
        # Empty roots are still durably reconciled; otherwise they would be
        # selected and walked on every fallback cycle forever.
        scanned_roots.append((bangumi_id, resolved_root, now))

    if not scanned_roots and deadline_monotonic is not None:
        return 0
    conn = _mikan_state_connect(config)
    try:
        for bangumi_id, series_path, _scanned_at in scanned_roots:
            conn.execute(
                "DELETE FROM anime_episode_index WHERE bangumi_id=? AND series_path=?",
                (bangumi_id, series_path),
            )
        conn.executemany(
            """
            INSERT OR REPLACE INTO anime_episode_index(
                bangumi_id, episode, season, path, series_path, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.executemany(
            """
            INSERT INTO anime_episode_index_roots(bangumi_id, series_path, scanned_at)
            VALUES (?, ?, ?)
            ON CONFLICT(bangumi_id, series_path) DO UPDATE SET
                scanned_at=excluded.scanned_at
            """,
            scanned_roots,
        )
        reconcile_interval = max(
            1,
            int(
                getattr(
                    config,
                    "mikan_library_fallback_scan_interval_seconds",
                    3600,
                )
                or 3600
            ),
        )
        conn.execute(
            """
            INSERT INTO mikan_state_meta(key, value, updated_at)
            VALUES ('episode_index_last_incremental_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (str(now), now),
        )
        conn.execute(
            """
            INSERT INTO mikan_state_meta(key, value, updated_at)
            VALUES ('episode_index_next_reconcile_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (str(now + reconcile_interval), now),
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _target_videos_from_episode_index(
    config: AppConfig,
    series_mappings: list[dict[str, object]],
    episode: int,
    *,
    season_hint: int | None = None,
) -> list[Path]:
    mapping_pairs = sorted(
        {
            (int(bangumi_id), str(_safe_resolve(Path(raw_path))))
            for mapping in series_mappings
            if (bangumi_id := _coerce_int(mapping.get("bangumi_id"))) is not None
            and (raw_path := str(mapping.get("path") or "").strip())
        }
    )
    if not mapping_pairs:
        return []
    pair_clause = " OR ".join(
        "(bangumi_id=? AND series_path=?)" for _pair in mapping_pairs
    )
    params: list[object] = [int(episode)]
    for bangumi_id, series_path in mapping_pairs:
        params.extend((bangumi_id, series_path))
    season_clause = ""
    if season_hint is not None:
        season_clause = " AND (season IS NULL OR season = ?)"
        params.append(int(season_hint))
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        rows = conn.execute(
            f"""
            SELECT path
            FROM anime_episode_index
            WHERE episode = ?
              AND ({pair_clause})
              {season_clause}
            ORDER BY CASE WHEN season = ? THEN 0 ELSE 1 END, path
            """,
            [*params, int(season_hint or -1)],
        ).fetchall()
        return [Path(str(row[0])) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()


def _target_videos_from_episode_index_with_hint_fallback(
    config: AppConfig,
    series_mappings: list[dict[str, object]],
    episode: int,
    *,
    season_hint: int | None = None,
) -> tuple[list[Path], int | None]:
    candidates = _target_videos_from_episode_index(
        config,
        series_mappings,
        episode,
        season_hint=season_hint,
    )
    if candidates or season_hint is None:
        return candidates, season_hint
    fallback = _target_videos_from_episode_index(
        config,
        series_mappings,
        episode,
        season_hint=None,
    )
    if fallback:
        return fallback, None
    return [], season_hint


def _missing_episodes_by_bangumi(
    config: AppConfig,
    logger: logging.Logger,
    series_mappings: list[dict[str, object]],
    *,
    pending: dict[str, Any] | None = None,
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    scanned_series = 0
    missing_series = 0
    complete_series = 0
    missing_episode_count = 0
    deferred_no_candidate = 0
    now = _utc_now()
    for mapping in series_mappings:
        bangumi_id = int(mapping["bangumi_id"])
        root = Path(str(mapping["path"]))
        missing = result.setdefault(bangumi_id, set())
        before_missing_count = len(missing)
        if not root.exists():
            logger.warning("Mikan mapped series path does not exist: bangumi_id=%s path=%s", bangumi_id, root)
            continue

        videos = _find_video_files(root, config.video_extensions) if stop_callback is None else _find_video_files(
            root, config.video_extensions, cancelled=stop_callback,
        )
        for video in videos:
            if _has_official_chinese_subtitle(video):
                continue
            episode = extract_episode_number(video.name)
            if episode is None:
                logger.warning("Could not detect episode number for Mikan library scan: %s", video)
                continue
            if pending is not None and _no_candidate_retry_active(pending, bangumi_id, episode, now):
                deferred_no_candidate += 1
                continue
            missing.add(episode)

        scanned_series += 1
        added_missing_count = len(missing) - before_missing_count
        if added_missing_count > 0:
            missing_series += 1
            missing_episode_count += added_missing_count
        else:
            complete_series += 1

    logger.info(
        "Mikan library scan summary scanned_series=%s missing_series=%s complete_series=%s missing_episodes=%s deferred_no_candidate=%s",
        scanned_series,
        missing_series,
        complete_series,
        missing_episode_count,
        deferred_no_candidate,
    )
    return result


def _missing_episodes_for_bangumi(
    config: AppConfig,
    logger: logging.Logger,
    bangumi_id: int,
    series_mappings: list[dict[str, object]],
    *,
    pending: dict[str, Any] | None = None,
    candidate_episodes: set[int] | None = None,
    episode_index_ready: bool = False,
    progress_callback: Callable[[int, int, Path], None] | None = None,
    stop_callback: Callable[[], bool] | None = None,
) -> set[int]:
    missing: set[int] = set()
    scanned_series = 0
    deferred_no_candidate = 0
    now = _utc_now()
    if episode_index_ready and candidate_episodes is not None:
        indexed_videos = 0
        for episode in sorted(candidate_episodes):
            if stop_callback is not None and stop_callback():
                logger.warning(
                    "Mikan indexed missing-episode scan stopped early because an operation stop was requested. "
                    "bangumi_id=%s",
                    bangumi_id,
                )
                return missing
            if pending is not None and _no_candidate_retry_active(pending, bangumi_id, episode, now):
                deferred_no_candidate += 1
                continue
            candidates = _target_videos_from_episode_index(
                config,
                series_mappings,
                episode,
            )
            indexed_videos += len(candidates)
            if any(video.is_file() and not _has_official_chinese_subtitle(video) for video in candidates):
                missing.add(episode)
        logger.info(
            "Mikan library scan bangumi summary reason=episode_index bangumi_id=%s "
            "candidate_episodes=%s indexed_videos=%s missing_episodes=%s deferred_no_candidate=%s",
            bangumi_id,
            len(candidate_episodes),
            indexed_videos,
            len(missing),
            deferred_no_candidate,
        )
        return missing

    total_series = len(series_mappings)
    for index, mapping in enumerate(series_mappings, start=1):
        if stop_callback is not None and stop_callback():
            logger.warning("Mikan missing-episode scan stopped early because an operation stop was requested. bangumi_id=%s", bangumi_id)
            return missing
        root = Path(str(mapping["path"]))
        if progress_callback is not None:
            progress_callback(index, total_series, root)
        if not root.exists():
            logger.warning("Mikan mapped series path does not exist: bangumi_id=%s path=%s", bangumi_id, root)
            continue
        for video in _find_video_files(root, config.video_extensions):
            if stop_callback is not None and stop_callback():
                logger.warning("Mikan missing-episode scan stopped early because an operation stop was requested. bangumi_id=%s path=%s", bangumi_id, root)
                return missing
            episode = extract_episode_number(video.name)
            if episode is None:
                if candidate_episodes is None:
                    logger.warning("Could not detect episode number for Mikan library scan: %s", video)
                continue
            if candidate_episodes is not None and episode not in candidate_episodes:
                continue
            if _has_official_chinese_subtitle(video):
                continue
            if pending is not None and _no_candidate_retry_active(pending, bangumi_id, episode, now):
                deferred_no_candidate += 1
                continue
            missing.add(episode)
        scanned_series += 1

    logger.info(
        "Mikan library scan bangumi summary reason=filesystem_fallback bangumi_id=%s "
        "scanned_series=%s missing_episodes=%s deferred_no_candidate=%s",
        bangumi_id,
        scanned_series,
        len(missing),
        deferred_no_candidate,
    )
    return missing


def _known_retry_episodes_for_bangumi(
    pending: dict[str, Any],
    bangumi_id: int,
    *,
    now: datetime | None = None,
) -> set[int]:
    """Return due episodes whose identity is already known from durable state."""

    current = now or _utc_now()
    result: set[int] = set()
    for entry in _pending_items(pending).values():
        if not isinstance(entry, dict):
            continue
        if _coerce_int(entry.get("bangumi_id")) != int(bangumi_id):
            continue
        episode = _coerce_int(entry.get("episode"))
        if episode is None or episode <= 0:
            continue
        if (
            _pending_is_terminal_success(entry)
            or _pending_has_active_release(entry)
            or _pending_has_deferred_release(entry)
            or _mikan_extract_failure_suppresses_replacement(
                str(entry.get("last_extract_failure_reason") or "")
            )
        ):
            continue
        retry_until = _safe_float(entry.get("no_candidate_until")) or 0.0
        if retry_until > current.timestamp():
            continue
        known_failure = bool(
            entry.get("no_candidate_at")
            or entry.get("no_candidate_retry_count")
            or entry.get("candidate_review_reason")
            or entry.get("failed_urls")
            or entry.get("failed_info_hashes")
            or entry.get("last_failure_reason")
            or entry.get("last_extract_failed_at")
        )
        if known_failure:
            result.add(episode)
    return result


def _episodes_from_releases(releases: list[MikanRelease]) -> set[int]:
    episodes: set[int] = set()
    for release in releases:
        episodes.update(release_episode_numbers(release))
    return episodes


def _queued_library_scan_mappings(
    config: AppConfig,
    mappings: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not bool(getattr(config, "scanner_queue_enabled", True)):
        return []

    state: ScanStateStore | None = None
    try:
        state = ScanStateStore.from_config(config)
        queued_videos = state.iter_ai_queue_candidates()
    except Exception:
        return []
    finally:
        if state is not None:
            state.close()

    selected: list[dict[str, object]] = []
    for video in queued_videos:
        for mapping in mappings:
            if _path_is_relative_to(video, Path(str(mapping.get("path", "")))):
                selected.append(mapping)
                break
    return selected


def _has_official_chinese_subtitle(video: Path) -> bool:
    return _target_has_required_chinese_subtitles(video)


def _target_has_required_chinese_subtitles(video: Path, *, verify_config: AppConfig | None = None) -> bool:
    if verify_config is not None:
        from subtitle_extract import verified_official_subtitle_languages
        try:
            return {"zh-tw", "zh-cn"}.issubset(verified_official_subtitle_languages(video, verify_config))
        except (SubtitleExtractError, OSError, ValueError):
            return False
    languages: set[str] = set()
    for subtitle in video.parent.glob(f"{video.stem}.*"):
        # The broad stem glob also matches the video itself and managed
        # metadata such as ``*.ass.quality.json``.  Passing a multi-gigabyte
        # MKV to the text classifier turns this cheap sidecar check into a full
        # media read and can hold an extraction slot for close to an hour.
        if not subtitle.is_file() or subtitle.suffix.casefold() not in SIDECAR_SUBTITLE_EXTENSIONS:
            continue
        language = classify_sidecar_subtitle_language(subtitle)
        if language in {"zh-tw", "zh-cn"}:
            languages.add(language)
            if {"zh-tw", "zh-cn"}.issubset(languages):
                return True
    return {"zh-tw", "zh-cn"}.issubset(languages)


def _unique_replacement_targets(targets: list[MikanReplacementTarget]) -> list[MikanReplacementTarget]:
    unique: list[MikanReplacementTarget] = []
    seen: set[tuple[int, int]] = set()
    for target in targets:
        key = (target.bangumi_id, target.episode)
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return sorted(unique, key=lambda item: (item.bangumi_id, item.episode))


def _replacement_targets_from_pending_entry(entry: dict[str, Any]) -> list[MikanReplacementTarget]:
    try:
        bangumi_id = int(entry.get("bangumi_id"))
    except (TypeError, ValueError):
        return []
    episodes = _pending_entry_episode_numbers(entry)
    if not episodes:
        try:
            episodes = {int(entry.get("episode"))}
        except (TypeError, ValueError):
            return []
    return [MikanReplacementTarget(bangumi_id, episode) for episode in sorted(episodes)]


def _replacement_targets_for_untracked_torrent(
    torrent: QBitTorrent,
    pending: dict[str, Any],
    series_mappings: list[dict[str, object]],
    *,
    torrent_files: list[QBitTorrentFile] | None = None,
) -> list[MikanReplacementTarget]:
    resolution = _resolve_untracked_torrent_targets(
        torrent,
        pending,
        series_mappings,
        torrent_files=torrent_files,
    )
    return list(resolution.targets) if resolution.trusted else []


def _resolve_untracked_torrent_targets(
    torrent: QBitTorrent,
    pending: dict[str, Any],
    series_mappings: list[dict[str, object]],
    *,
    torrent_files: list[QBitTorrentFile] | None = None,
) -> MikanUntrackedTorrentResolution:
    targets: list[MikanReplacementTarget] = []
    evidence: list[str] = []
    for entry in _pending_items(pending).values():
        if not isinstance(entry, dict):
            continue
        entry_evidence = _exact_untracked_pending_match_evidence(entry, torrent)
        if entry_evidence:
            targets.extend(_replacement_targets_from_pending_entry(entry))
            evidence.append(entry_evidence)
    if targets:
        unique = _unique_replacement_targets(targets)
        if len({target.bangumi_id for target in unique}) != 1:
            return MikanUntrackedTorrentResolution(evidence=tuple(sorted(set(evidence))))
        confidence = 1.0 if "pending_hash" in evidence else 0.98
        return MikanUntrackedTorrentResolution(
            targets=tuple(unique),
            confidence=confidence,
            match_version=f"{_QBIT_RECOVERY_MATCH_VERSION}-pending",
            evidence=tuple(sorted(set(evidence))),
        )

    matched_mappings = [
        mapping
        for mapping in series_mappings
        if mapping.get("bangumi_id") is not None and mapping_matches_torrent(torrent.name, mapping)
    ]
    matched_bangumi_ids = {int(mapping["bangumi_id"]) for mapping in matched_mappings}
    if len(matched_bangumi_ids) != 1:
        return MikanUntrackedTorrentResolution()
    episodes = _torrent_episode_numbers(torrent.name)
    if not episodes:
        for torrent_file in torrent_files or []:
            episodes.update(_torrent_episode_numbers(torrent_file.name))
    if not episodes:
        return MikanUntrackedTorrentResolution()
    bangumi_id = next(iter(matched_bangumi_ids))
    confidence = max((_mapping_recovery_confidence(mapping) for mapping in matched_mappings), default=0.0)
    mapping_sources = sorted(
        {
            str(mapping.get("identity_source") or "unspecified")
            for mapping in matched_mappings
        }
    )
    return MikanUntrackedTorrentResolution(
        targets=tuple(MikanReplacementTarget(bangumi_id, episode) for episode in sorted(episodes)),
        confidence=confidence,
        match_version=f"{_QBIT_RECOVERY_MATCH_VERSION}-mapping",
        evidence=tuple(f"series_mapping:{source}" for source in mapping_sources),
    )


def _exact_untracked_pending_match_evidence(entry: dict[str, Any], torrent: QBitTorrent) -> str:
    expected_hashes = {
        info_hash
        for raw in (
            entry.get("info_hash"),
            entry.get("last_qbit_hash"),
            entry.get("torrent_url"),
            entry.get("deferred_info_hash"),
            entry.get("last_failed_info_hash"),
        )
        if (info_hash := extract_torrent_info_hash(str(raw or "")))
    }
    if torrent.hash and torrent.hash.casefold() in expected_hashes:
        return "pending_hash"

    normalized_torrent = _normalized_title(torrent.name)
    if not normalized_torrent:
        return ""
    for key in ("title", "deferred_title", "last_failed_title"):
        title = _normalized_title(str(entry.get(key) or ""))
        if title and title == normalized_torrent:
            return f"pending_exact_title:{key}"
    return ""


def _mapping_recovery_confidence(mapping: dict[str, object]) -> float:
    if bool(mapping.get("locked")):
        return 1.0
    value = mapping.get("match_confidence")
    if isinstance(value, int | float):
        return max(0.0, min(float(value), 1.0))
    source = str(mapping.get("identity_source") or "").strip().casefold()
    if source in {"manual", "config"}:
        return 1.0
    # Direct mappings supplied by older callers predate confidence metadata.
    # A unique semantic title match is trusted, while persisted automatic
    # mappings must carry their actual confidence.
    if not source:
        return 0.95
    return 0.0


def _replacement_targets_by_bangumi(targets: list[MikanReplacementTarget]) -> dict[int, set[int]]:
    grouped: dict[int, set[int]] = {}
    for target in targets:
        grouped.setdefault(target.bangumi_id, set()).add(target.episode)
    return grouped


def _format_replacement_targets(targets: list[MikanReplacementTarget]) -> str:
    grouped = _replacement_targets_by_bangumi(targets)
    return "; ".join(
        f"{bangumi_id}:{_format_episode_list(episodes)}"
        for bangumi_id, episodes in sorted(grouped.items())
    ) or "-"


def _mikan_extract_failure_from_diagnostics(
    source_video: Path,
    target_video: Path,
    diagnostics: list[dict[str, Any]],
    *,
    extract_error: str = "",
) -> tuple[str, str]:
    if extract_error:
        lowered = extract_error.casefold()
        reason = "subtitle_probe_failed" if "ffprobe" in lowered else "ffmpeg_extract_failed"
        return reason, extract_error[:1000]

    if not source_video.exists():
        if diagnostics:
            return _mikan_diagnostic_reason(diagnostics, source_video, target_video)
        return "source_video_missing", f"Mapped source video does not exist: {source_video}"

    if not diagnostics:
        return "no_subtitle_streams", f"No embedded subtitle streams or sidecar subtitles were found for {source_video}"

    return _mikan_diagnostic_reason(diagnostics, source_video, target_video)


def _mikan_qbit_path_context(
    torrent: QBitTorrent,
    mapped_root: Path | None,
    torrent_files: list[QBitTorrentFile],
    config: AppConfig,
    *,
    source_video: Path | None = None,
    target_video: Path | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "qbit_hash": torrent.hash,
        "qbit_name": torrent.name,
        "qbit_content_path": torrent.content_path,
        "qbit_save_path": torrent.save_path,
        "qbit_raw_path": torrent.content_path or torrent.save_path,
        "mapped_root": str(mapped_root) if mapped_root is not None else None,
        "mapped_root_exists": bool(mapped_root.exists()) if mapped_root is not None else False,
        "source_video": str(source_video) if source_video is not None else None,
        "source_video_exists": bool(source_video.exists()) if source_video is not None else None,
        "target_video": str(target_video) if target_video is not None else None,
        "qbit_path_mappings": [
            {
                "remote": str(mapping.get("remote", "")),
                "local": str(mapping.get("local", "")),
            }
            for mapping in getattr(config, "qbit_path_mappings", [])
            if isinstance(mapping, dict)
        ],
    }
    save_root = map_remote_path(torrent.save_path, config.qbit_path_mappings)
    extension_set = {extension.lower() for extension in config.video_extensions}
    qbit_files: list[dict[str, Any]] = []
    for torrent_file in torrent_files:
        mapped_file = _qbit_file_path(save_root, torrent_file.name) if save_root is not None else None
        is_video = bool(mapped_file is not None and mapped_file.suffix.lower() in extension_set)
        if not is_video and len(qbit_files) >= 5:
            continue
        qbit_files.append(
            {
                "name": torrent_file.name,
                "size": torrent_file.size,
                "progress": torrent_file.progress,
                "mapped_path": str(mapped_file) if mapped_file is not None else None,
                "mapped_exists": bool(mapped_file.exists()) if mapped_file is not None else None,
                "video": is_video,
            }
        )
        if len(qbit_files) >= 12:
            break
    context["qbit_files"] = qbit_files
    return {key: value for key, value in context.items() if value not in (None, "", [])}


def _mikan_diagnostic_reason(
    diagnostics: list[dict[str, Any]],
    source_video: Path,
    target_video: Path,
) -> tuple[str, str]:
    embedded = [item for item in diagnostics if item.get("source") == "embedded"]
    sidecars = [item for item in diagnostics if item.get("source") == "sidecar"]
    rejected = [item for item in diagnostics if item.get("status") == "validation_failed"]
    if rejected:
        return "subtitle_validation_failed", "; ".join(str(item.get("detail") or "invalid subtitle") for item in rejected)[:1000]
    if any(item.get("status") == "extract_failed" for item in diagnostics):
        detail = _first_diagnostic_detail(diagnostics) or f"ffmpeg failed while extracting subtitles from {source_video}"
        return "ffmpeg_extract_failed", detail
    if any(item.get("status") == "probe_failed" for item in diagnostics):
        detail = _first_diagnostic_detail(diagnostics) or f"ffprobe failed while inspecting subtitles from {source_video}"
        return "subtitle_probe_failed", detail
    if embedded and all(item.get("kind") == "image" for item in embedded) and not sidecars:
        return "image_subtitles_only", f"Only image subtitle streams were found for {source_video}; OCR is not supported."
    if embedded and not any(item.get("kind") == "text" for item in embedded) and not sidecars:
        return "no_text_subtitle_streams", f"No text subtitle streams were found for {source_video}"
    if any(item.get("status") == "unclassified" for item in diagnostics):
        summary = _subtitle_score_summary(diagnostics)
        if sidecars and not embedded:
            return "sidecar_language_not_supported", f"Sidecar subtitles were found but no usable Chinese subtitle was detected. {summary}"
        return "subtitle_language_not_supported", f"Subtitle text was found but no usable Chinese subtitle was detected. {summary}"
    detected_non_chinese = sorted(
        {
            str(classification.get("language") or "")
            for item in diagnostics
            for classification in [item.get("classification")]
            if isinstance(classification, dict)
            and classification.get("language")
            and classification.get("language") not in {"zh-tw", "zh-cn"}
        }
    )
    if detected_non_chinese:
        return (
            "subtitle_language_not_supported",
            "All text subtitles were extracted, but content detection found no Chinese subtitle. "
            f"Detected languages: {','.join(detected_non_chinese)}",
        )
    return "no_usable_subtitles", f"No usable Chinese subtitles were extracted for target {target_video}"


def _first_diagnostic_detail(diagnostics: list[dict[str, Any]]) -> str:
    for item in diagnostics:
        detail = str(item.get("detail") or "")
        if detail:
            return detail[:1000]
    return ""


def _subtitle_score_summary(diagnostics: list[dict[str, Any]]) -> str:
    scores: list[str] = []
    for item in diagnostics[:5]:
        classification = item.get("classification")
        if not isinstance(classification, dict):
            continue
        scores.append(
            "source={source} status={status} lang={lang} reason={reason} zhTW={tw} zhCN={cn} ja={ja} quality={quality}".format(
                source=item.get("source") or "-",
                status=item.get("status") or "-",
                lang=classification.get("language") or "-",
                reason=classification.get("reason") or "-",
                tw=classification.get("traditional_score") or 0,
                cn=classification.get("simplified_score") or 0,
                ja=classification.get("japanese_score") or 0,
                quality=classification.get("quality_score") or 0,
            )
        )
    return "scores: " + "; ".join(scores) if scores else "No subtitle score was available."


def _format_episode_list(episodes: list[int] | set[int]) -> str:
    ordered = sorted(episodes)
    return ",".join(f"{episode:02d}" for episode in ordered) if ordered else "-"


def _resolve_seen_path(config: AppConfig) -> Path:
    path = Path(config.mikan_seen_path)
    if not path.is_absolute():
        path = config.work_path / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_pending_path(config: AppConfig) -> Path:
    path = Path(config.mikan_pending_path)
    if not path.is_absolute():
        path = config.work_path / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _mikan_operation_lock(config: AppConfig) -> VideoLock:
    stale_seconds = float(
        getattr(config, "mikan_operation_lock_stale_seconds", MIKAN_OPERATION_LOCK_STALE_SECONDS)
        or MIKAN_OPERATION_LOCK_STALE_SECONDS
    )
    return VideoLock(config.work_path / MIKAN_OPERATION_LOCK_NAME, stale_seconds=stale_seconds)


def _mikan_episode_index_lock(config: AppConfig) -> VideoLock:
    return VideoLock(
        config.work_path / MIKAN_EPISODE_INDEX_LOCK_NAME,
        stale_seconds=max(900.0, float(_mikan_episode_index_ttl_seconds(config))),
    )


def _mikan_queue_lock(config: AppConfig) -> VideoLock:
    stale_seconds = float(
        getattr(config, "mikan_operation_lock_stale_seconds", MIKAN_OPERATION_LOCK_STALE_SECONDS)
        or MIKAN_OPERATION_LOCK_STALE_SECONDS
    )
    return VideoLock(config.work_path / MIKAN_QUEUE_LOCK_NAME, stale_seconds=stale_seconds)


def _mikan_reset_all_request_path(config: AppConfig) -> Path:
    return config.work_path / MIKAN_RESET_ALL_REQUEST_NAME


def _mikan_redownload_all_request_path(config: AppConfig) -> Path:
    return config.work_path / MIKAN_REDOWNLOAD_ALL_REQUEST_NAME


def _mikan_redownload_all_active_path(config: AppConfig) -> Path:
    return config.work_path / MIKAN_REDOWNLOAD_ALL_ACTIVE_NAME


def _mikan_redownload_all_cancel_path(config: AppConfig) -> Path:
    return config.work_path / MIKAN_REDOWNLOAD_ALL_CANCEL_NAME


def _mikan_extract_cancel_path(config: AppConfig) -> Path:
    return Path(config.work_path) / MIKAN_EXTRACT_CANCEL_NAME


def _mikan_extract_cancel_requested(config: AppConfig, job: MikanExtractJob) -> bool:
    path = _mikan_extract_cancel_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("job_key") or "") == job.job_key
        and str(payload.get("worker_id") or "") == job.worker_id
    )


def _acknowledge_mikan_extract_cancel(config: AppConfig, job: MikanExtractJob) -> None:
    path = _mikan_extract_cancel_path(config)
    if not _mikan_extract_cancel_requested(config, job):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _mikan_completed_state_update_request_path(config: AppConfig) -> Path:
    return config.work_path / MIKAN_COMPLETED_STATE_UPDATE_REQUEST_NAME


def _mikan_replacement_enqueue_request_path(config: AppConfig) -> Path:
    return config.work_path / MIKAN_REPLACEMENT_ENQUEUE_REQUEST_NAME


def _mikan_operation_lock_wait_seconds(config: AppConfig) -> float:
    raw = getattr(config, "mikan_operation_lock_wait_seconds", MIKAN_OPERATION_LOCK_WAIT_SECONDS)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return MIKAN_OPERATION_LOCK_WAIT_SECONDS


def _mikan_target_missing_retry_seconds(config: AppConfig) -> float:
    raw = getattr(config, "mikan_target_missing_retry_seconds", MIKAN_TARGET_MISSING_RETRY_SECONDS)
    try:
        return max(60.0, float(raw))
    except (TypeError, ValueError):
        return MIKAN_TARGET_MISSING_RETRY_SECONDS


def _mikan_extract_failed_retry_seconds(config: AppConfig) -> float:
    raw = getattr(config, "mikan_extract_failed_retry_seconds", 900.0)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 900.0


def _mikan_extract_job_timeout_seconds(config: AppConfig) -> float:
    raw = getattr(config, "mikan_extract_job_timeout_seconds", 900.0)
    try:
        return max(60.0, float(raw))
    except (TypeError, ValueError):
        return 900.0


def _mikan_extract_job_timeout_for(
    job: MikanExtractJob,
    torrent_files: list[QBitTorrentFile],
    config: AppConfig,
) -> float:
    """Scale a torrent extraction deadline by the number of relevant videos.

    A fixed 15-minute deadline is adequate for a single episode but turns
    legitimate 12-24 episode collections into false source failures.  The
    deadline remains bounded so a corrupt source cannot occupy a slot forever.
    """

    base = _mikan_extract_job_timeout_seconds(config)
    try:
        per_video = max(30.0, float(getattr(config, "mikan_extract_job_timeout_per_video_seconds", 300)))
    except (TypeError, ValueError):
        per_video = 300.0
    try:
        maximum = max(base, float(getattr(config, "mikan_extract_job_timeout_max_seconds", 14400)))
    except (TypeError, ValueError):
        maximum = max(base, 14400.0)

    extension_set = {str(extension).lower() for extension in getattr(config, "video_extensions", [])}
    video_count = sum(
        1
        for item in torrent_files
        if Path(str(item.name or "")).suffix.lower() in extension_set
    )
    episode_count = len(_pending_source_episode_numbers_from_entries(job.pending_entries))
    work_units = max(1, video_count, episode_count)
    return min(maximum, max(base, work_units * per_video))


def _mikan_extract_cancel_grace_seconds(config: AppConfig) -> float:
    try:
        return max(0.0, float(getattr(config, "mikan_extract_cancel_grace_seconds", 15)))
    except (TypeError, ValueError):
        return 15.0


def _mikan_extract_timeout_retry_seconds(config: AppConfig) -> float:
    try:
        return max(1.0, float(getattr(config, "mikan_extract_timeout_retry_seconds", 60)))
    except (TypeError, ValueError):
        return 60.0


def _mikan_extract_lease_seconds(config: AppConfig) -> float:
    raw = getattr(config, "mikan_extract_lease_seconds", _mikan_extract_job_timeout_seconds(config))
    try:
        return max(60.0, float(raw))
    except (TypeError, ValueError):
        return _mikan_extract_job_timeout_seconds(config)


def _write_redownload_all_active(config: AppConfig, *, delete_files: bool) -> None:
    _save_json_atomic(
        _mikan_redownload_all_active_path(config),
        {
            "action": "redownload_all_torrents_and_enqueue",
            "started_at": _utc_now().isoformat(),
            "updated_at": _utc_now().isoformat(),
            "delete_files": bool(delete_files),
            "stage": "starting",
            "stage_label": "啟動重載",
        },
    )


def _update_redownload_all_active(config: AppConfig, **updates: Any) -> None:
    active_path = _mikan_redownload_all_active_path(config)
    payload = _load_request_file(active_path)
    if not payload:
        return
    payload.update({key: value for key, value in updates.items() if value is not None})
    payload["updated_at"] = _utc_now().isoformat()
    _save_json_atomic(active_path, payload)


def _clear_redownload_all_active(config: AppConfig) -> None:
    _mikan_redownload_all_active_path(config).unlink(missing_ok=True)


def _redownload_all_active_payload(config: AppConfig) -> dict[str, Any] | None:
    job = _mikan_job_payload(config, "redownload_all")
    if (
        job is not None
        and str(job.get("job_status") or "") == "running"
        and float(job.get("lease_until") or 0) > time.time()
    ):
        return job
    active_path = _mikan_redownload_all_active_path(config)
    if not active_path.exists():
        return None
    payload = _load_request_file(active_path)
    try:
        active_age = max(0.0, time.time() - active_path.stat().st_mtime)
    except OSError:
        active_age = MIKAN_REDOWNLOAD_ACTIVE_STALE_SECONDS + 1.0
    if (
        active_age <= MIKAN_REDOWNLOAD_ACTIVE_STALE_SECONDS
        or _lock_file_is_active(_mikan_queue_lock(config))
        or _lock_file_is_active(_mikan_operation_lock(config))
    ):
        return payload
    request_path = _mikan_redownload_all_request_path(config)
    if request_path.exists():
        _merge_stale_redownload_active_into_request(active_path, request_path, payload)
    active_path.unlink(missing_ok=True)
    return None


def _lock_file_is_active(lock: VideoLock) -> bool:
    return lock.lock_path.exists() and not lock._is_stale_lock()


def _merge_stale_redownload_active_into_request(
    active_path: Path,
    request_path: Path,
    active_payload: dict[str, Any],
) -> None:
    request = _load_request_file(request_path)
    updates: dict[str, Any] = {}
    if active_payload.get("deleted_torrents") and not request.get("deleted_torrents"):
        updates["deleted_torrents"] = active_payload.get("deleted_torrents")
    if active_payload.get("delete_files") is not None and request.get("delete_files") is None:
        updates["delete_files"] = bool(active_payload.get("delete_files"))
    reason = str(request.get("reason") or "")
    if "stale active" not in reason.casefold():
        updates["reason"] = (
            f"{reason} Recovered stale active marker and will resume redownload."
            if reason
            else "Recovered stale active marker and will resume redownload."
        )
    if updates:
        _update_request_file(request_path, **updates)


def _load_reset_all_request(path: Path) -> dict[str, Any]:
    return _load_request_file(path)


def _load_request_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _update_request_file(path: Path, **updates: Any) -> None:
    payload = _load_request_file(path)
    payload.update({key: value for key, value in updates.items() if value is not None})
    payload["updated_at"] = _utc_now().isoformat()
    _save_json_atomic(path, payload)


def _save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_seen(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MikanWorkerError(f"Invalid Mikan seen file: {path}") from exc


def _save_seen(path: Path, seen: dict[str, Any]) -> None:
    _save_json_atomic(path, seen)


def _register_sqlite_authoritative_pending(path: Path, *, enabled: bool) -> None:
    normalized = path.resolve()
    with _SQLITE_AUTHORITATIVE_PENDING_LOCK:
        if enabled:
            _SQLITE_AUTHORITATIVE_PENDING_PATHS.add(normalized)
        else:
            _SQLITE_AUTHORITATIVE_PENDING_PATHS.discard(normalized)


def _sqlite_authoritative_pending_enabled(path: Path) -> bool:
    normalized = path.resolve()
    with _SQLITE_AUTHORITATIVE_PENDING_LOCK:
        return normalized in _SQLITE_AUTHORITATIVE_PENDING_PATHS


def _load_pending_from_sqlite(path: Path) -> dict[str, Any] | None:
    db_path = _mikan_state_db_path_from_pending(path)
    if not db_path.is_file():
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        marker = connection.execute(
            "SELECT value FROM mikan_state_meta WHERE key='pending_authoritative'"
        ).fetchone()
        if marker is None or str(marker[0]).casefold() != "sqlite":
            return None
        rows = connection.execute(
            "SELECT key, raw_json FROM mikan_download_items ORDER BY key"
        ).fetchall()
        items: dict[str, Any] = {}
        for key, raw_json in rows:
            try:
                entry = json.loads(str(raw_json or "{}"))
            except json.JSONDecodeError as exc:
                raise MikanWorkerError(f"Invalid pending row in {db_path}: {key}") from exc
            if isinstance(entry, dict):
                items[str(key)] = entry
        return {"items": items}
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()


def _load_pending(path: Path) -> dict[str, Any]:
    if _sqlite_authoritative_pending_enabled(path):
        sqlite_payload = _load_pending_from_sqlite(path)
        if sqlite_payload is not None:
            return sqlite_payload
    if not path.exists():
        return {"items": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MikanWorkerError(f"Invalid Mikan pending file: {path}") from exc
    if not isinstance(payload, dict):
        return {"items": {}}
    if not isinstance(payload.get("items"), dict):
        payload["items"] = {}
    return payload


def _save_pending(path: Path, pending: dict[str, Any]) -> None:
    if _sqlite_authoritative_pending_enabled(path):
        try:
            _sync_mikan_state_db(path, pending)
            _mark_pending_sqlite_authoritative(path)
            _archive_legacy_pending_json(path)
        except (OSError, sqlite3.Error) as exc:
            raise MikanWorkerError(f"Could not durably save Mikan pending state to SQLite: {exc}") from exc
        return
    _save_json_atomic(path, pending)
    try:
        _sync_mikan_state_db(path, pending)
    except (OSError, sqlite3.Error):
        pass


def _mark_pending_sqlite_authoritative(path: Path) -> None:
    db_path = _mikan_state_db_path_from_pending(path)
    connection = sqlite3.connect(db_path, timeout=60)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=60000")
        _ensure_mikan_state_tables(connection)
        now = time.time()
        connection.execute(
            """
            INSERT INTO mikan_state_meta(key, value, updated_at)
            VALUES('pending_authoritative', 'sqlite', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (now,),
        )
        connection.commit()
    finally:
        connection.close()


def _archive_legacy_pending_json(path: Path) -> Path | None:
    if not path.is_file():
        return None
    destination = path.with_name(f"{path.stem}.legacy-readonly{path.suffix}")
    if destination.exists():
        return destination
    source_hash = sha256_file(path)
    verified_move(path, destination)
    if sha256_file(destination) != source_hash:
        raise MikanWorkerError(f"Legacy pending backup verification failed: {destination}")
    try:
        destination.chmod(0o444)
    except OSError:
        pass
    return destination


def _ensure_mikan_state_db_for_pending(pending_path: Path) -> None:
    try:
        pending_stat = pending_path.stat()
    except OSError:
        return
    db_path = _mikan_state_db_path_from_pending(pending_path)
    if db_path.exists():
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(db_path)
            if _mikan_state_db_matches_pending(conn, pending_stat):
                return
        except sqlite3.Error:
            pass
        finally:
            if conn is not None:
                conn.close()
    try:
        _sync_mikan_state_db(pending_path, _load_pending(pending_path))
    except (OSError, sqlite3.Error, MikanWorkerError):
        return


def _mikan_state_db_matches_pending(conn: sqlite3.Connection, pending_stat: os.stat_result) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'mikan_state_meta'"
    ).fetchone()
    if row is None:
        return False
    values = {
        str(key): str(value)
        for key, value in conn.execute(
            "SELECT key, value FROM mikan_state_meta WHERE key IN ('pending_mtime_ns', 'pending_size')"
        ).fetchall()
    }
    return (
        values.get("pending_mtime_ns") == str(pending_stat.st_mtime_ns)
        and values.get("pending_size") == str(pending_stat.st_size)
    )


def _sync_mikan_state_db(pending_path: Path, pending: dict[str, Any]) -> None:
    db_path = _mikan_state_db_path_from_pending(pending_path)
    items = _pending_items(pending)
    now = time.time()
    pending_stat: os.stat_result | None
    try:
        pending_stat = pending_path.stat()
    except OSError:
        pending_stat = None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _ensure_mikan_state_tables(conn)
        previous = {
            str(key): {
                "status": str(status or ""),
                "torrent_url": str(torrent_url or ""),
                "last_qbit_state": str(last_qbit_state or ""),
                "failed_count": int(failed_count or 0),
                "last_failure_reason": str(last_failure_reason or ""),
                "last_extract_failure_reason": str(last_extract_failure_reason or ""),
                "next_action": str(next_action or ""),
                "raw_json": str(raw_json or ""),
            }
            for (
                key,
                status,
                torrent_url,
                last_qbit_state,
                failed_count,
                last_failure_reason,
                last_extract_failure_reason,
                next_action,
                raw_json,
            ) in conn.execute(
                """
                SELECT key, status, torrent_url, last_qbit_state, failed_count,
                       last_failure_reason, last_extract_failure_reason,
                       next_action, raw_json
                FROM mikan_download_items
                """
            ).fetchall()
        }
        current_keys: set[str] = set()
        for key, entry in sorted(items.items(), key=lambda item: str(item[0])):
            if not isinstance(entry, dict):
                continue
            key_text = str(key)
            current_keys.add(key_text)
            row = _mikan_state_row(key_text, entry, now)
            previous_row = previous.get(key_text)
            if (
                previous_row is not None
                and previous_row["raw_json"] == row["raw_json"]
                and previous_row["status"] == row["status"]
                and previous_row["next_action"] == row["next_action"]
            ):
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO mikan_download_items(
                    key, bangumi_id, episode, episodes_json, status, title, torrent_url,
                    failed_urls_json, queued_at, updated_at, last_progress, last_downloaded,
                    last_dlspeed, last_qbit_state, last_qbit_hash, last_qbit_name,
                    last_qbit_sync_at, completed_at, last_extracted_at, last_extracted_count,
                    total_extracted_count, failed_count, last_failure_reason,
                    last_extract_failure_reason, last_extract_failure_detail,
                    no_candidate_until, timed_out_at, next_action, raw_json
                )
                VALUES (
                    :key, :bangumi_id, :episode, :episodes_json, :status, :title, :torrent_url,
                    :failed_urls_json, :queued_at, :updated_at, :last_progress, :last_downloaded,
                    :last_dlspeed, :last_qbit_state, :last_qbit_hash, :last_qbit_name,
                    :last_qbit_sync_at, :completed_at, :last_extracted_at, :last_extracted_count,
                    :total_extracted_count, :failed_count, :last_failure_reason,
                    :last_extract_failure_reason, :last_extract_failure_detail,
                    :no_candidate_until, :timed_out_at, :next_action, :raw_json
                )
                """,
                row,
            )
            if _mikan_state_event_needed(previous_row, row):
                _record_mikan_state_event(conn, previous_row, row, now=now)
        removed_keys = sorted(set(previous) - current_keys)
        if removed_keys:
            conn.executemany(
                "DELETE FROM mikan_download_items WHERE key = ?",
                [(key,) for key in removed_keys],
            )
        conn.execute(
            """
            DELETE FROM mikan_download_events
            WHERE id NOT IN (
                SELECT id FROM mikan_download_events
                ORDER BY id DESC
                LIMIT 5000
            )
            """
        )
        meta_rows = [("last_sync_at", str(now), now)]
        if pending_stat is not None:
            meta_rows.extend(
                [
                    ("pending_mtime_ns", str(pending_stat.st_mtime_ns), now),
                    ("pending_size", str(pending_stat.st_size), now),
                ]
            )
        conn.executemany(
            """
            INSERT INTO mikan_state_meta(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            meta_rows,
        )
        conn.commit()
    finally:
        conn.close()


def _mikan_state_db_path_from_pending(pending_path: Path) -> Path:
    return pending_path.with_name("mikan_state.sqlite3")


def _mikan_state_db_path_from_config(config: AppConfig) -> Path:
    return _mikan_state_db_path_from_pending(_resolve_pending_path(config))


def _mikan_state_connect(config: AppConfig) -> sqlite3.Connection:
    db_path = _mikan_state_db_path_from_config(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_mikan_state_tables(conn)
    # Event/schema migrations perform small DML updates.  Commit those changes
    # before returning so a caller that only performs a read and closes the
    # connection cannot roll back freshly-created tables.
    conn.commit()
    return conn


def _mikan_state_existing_connect(config: AppConfig) -> sqlite3.Connection:
    """Open the already-initialized state DB without repeating DDL/migrations.

    Hot paths such as one-second dispatch checks and extraction lease heartbeats
    only touch mikan_extract_jobs. Re-running schema setup there needlessly takes
    SQLite write locks and can starve the scanner and WebUI.
    """
    db_path = _mikan_state_db_path_from_config(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _load_qbit_unhealthy_since(config: AppConfig) -> dict[str, tuple[str, float]]:
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        row = conn.execute(
            "SELECT value FROM mikan_state_meta WHERE key = 'qbit_unhealthy_since_json'"
        ).fetchone()
        if row is None:
            return {}
        payload = json.loads(str(row[0] or "{}"))
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
        return {}
    finally:
        if conn is not None:
            conn.close()
    if not isinstance(payload, dict):
        return {}
    result: dict[str, tuple[str, float]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            continue
        if isinstance(value, dict):
            reason = str(value.get("reason") or "")
            since = _coerce_float(value.get("since"))
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            reason = str(value[0] or "")
            since = _coerce_float(value[1])
        else:
            continue
        if reason and since is not None:
            result[key] = (reason, float(since))
    return result


def _save_qbit_unhealthy_since(config: AppConfig, values: dict[str, tuple[str, float]]) -> None:
    now = time.time()
    payload = {
        str(key): {"reason": str(value[0]), "since": float(value[1])}
        for key, value in sorted(values.items())
        if key and value and len(value) >= 2
    }
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        conn.execute(
            """
            INSERT INTO mikan_state_meta(key, value, updated_at)
            VALUES ('qbit_unhealthy_since_json', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )
        conn.commit()
    except (sqlite3.Error, TypeError, ValueError):
        return
    finally:
        if conn is not None:
            conn.close()


def _mikan_extract_job_key(torrent: QBitTorrent) -> str:
    if torrent.hash:
        return f"hash:{torrent.hash}"
    return f"name:{_normalized_title(torrent.name) or torrent.name}"


def _mikan_non_requeueable_extract_job_keys(config: AppConfig) -> set[str]:
    """Return exact torrent jobs whose result must not be auto-recovered."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        rows = conn.execute(
            "SELECT job_key FROM mikan_extract_jobs WHERE status IN ('replaced', 'terminal_failed')"
        ).fetchall()
        return {str(row[0]) for row in rows if row and row[0]}
    except sqlite3.Error:
        # Reconciliation can run before the state DB has been initialized.
        return set()
    finally:
        if conn is not None:
            conn.close()


def _upsert_mikan_extract_jobs(
    config: AppConfig,
    rows: list[tuple[QBitTorrent, list[dict[str, Any]], int, bool]],
    *,
    state_required: bool,
) -> int:
    if not rows:
        return 0
    now = time.time()
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        queued = 0
        for torrent, pending_entries, priority, force_requeue in rows:
            job_key = _mikan_extract_job_key(torrent)
            existing = conn.execute(
                "SELECT status, lease_until, finished_at, result_json, attempts FROM mikan_extract_jobs WHERE job_key = ?",
                (job_key,),
            ).fetchone()
            existing_status = str(existing[0]) if existing is not None else ""
            # These are terminal states for this exact torrent/job key.  The
            # previous waiting-extract exception reset replaced jobs to queued,
            # causing deterministic no-subtitle releases to be read from disk
            # again every few seconds.
            if existing_status in {"replaced", "terminal_failed"}:
                continue
            if existing_status == "failed" and not _failed_mikan_extract_job_should_requeue(
                existing,
                now,
                config,
            ):
                continue
            reset_job_state = (
                existing is None
                or existing_status not in {"queued", "running", "success"}
                or (force_requeue and existing_status == "success")
            )
            if reset_job_state:
                queued += 1
            bangumi_ids = sorted(
                {
                    bangumi_id
                    for entry in pending_entries
                    if (bangumi_id := _coerce_int(entry.get("bangumi_id"))) is not None
                }
            )
            episodes = sorted(_pending_source_episode_numbers_from_entries(pending_entries))
            target_confidence, match_version = _mikan_extract_mapping_contract(pending_entries)
            initial_file = file_time_metadata(str(torrent.content_path or ""))
            conn.execute(
                """
                INSERT INTO mikan_extract_jobs(
                    job_key, status, priority, attempts, worker_id, lease_until,
                    torrent_hash, torrent_name, target_confidence, match_version,
                    target_path, current_file_timestamp, current_file_time_kind, current_file_size,
                    bangumi_ids_json, episodes_json,
                    pending_entries_json, torrent_json, result_json, last_error,
                    created_at, updated_at, started_at, finished_at
                )
                VALUES (?, 'queued', ?, 0, '', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '', ?, ?, 0, 0)
                ON CONFLICT(job_key) DO UPDATE SET
                    status = CASE
                        WHEN mikan_extract_jobs.status = 'running' AND mikan_extract_jobs.lease_until > excluded.updated_at THEN mikan_extract_jobs.status
                        WHEN ? THEN 'queued'
                        WHEN mikan_extract_jobs.status = 'success' THEN mikan_extract_jobs.status
                        ELSE 'queued'
                    END,
                    priority = MAX(mikan_extract_jobs.priority, excluded.priority),
                    attempts = CASE
                        WHEN ? THEN 0
                        ELSE mikan_extract_jobs.attempts
                    END,
                    worker_id = CASE
                        WHEN ? THEN ''
                        ELSE mikan_extract_jobs.worker_id
                    END,
                    lease_until = CASE
                        WHEN ? THEN 0
                        ELSE mikan_extract_jobs.lease_until
                    END,
                    torrent_hash = excluded.torrent_hash,
                    torrent_name = excluded.torrent_name,
                    target_confidence = excluded.target_confidence,
                    match_version = excluded.match_version,
                    target_path = CASE
                        WHEN excluded.current_file_timestamp > 0 THEN excluded.target_path
                        ELSE mikan_extract_jobs.target_path
                    END,
                    current_file_timestamp = CASE
                        WHEN excluded.current_file_timestamp > 0 THEN excluded.current_file_timestamp
                        ELSE mikan_extract_jobs.current_file_timestamp
                    END,
                    current_file_time_kind = CASE
                        WHEN excluded.current_file_timestamp > 0 THEN excluded.current_file_time_kind
                        ELSE mikan_extract_jobs.current_file_time_kind
                    END,
                    current_file_size = CASE
                        WHEN excluded.current_file_timestamp > 0 THEN excluded.current_file_size
                        ELSE mikan_extract_jobs.current_file_size
                    END,
                    bangumi_ids_json = excluded.bangumi_ids_json,
                    episodes_json = excluded.episodes_json,
                    pending_entries_json = excluded.pending_entries_json,
                    torrent_json = excluded.torrent_json,
                    result_json = CASE
                        WHEN ? THEN '{}'
                        ELSE mikan_extract_jobs.result_json
                    END,
                    last_error = CASE
                        WHEN ? THEN ''
                        ELSE mikan_extract_jobs.last_error
                    END,
                    updated_at = excluded.updated_at,
                    started_at = CASE
                        WHEN ? THEN 0
                        ELSE mikan_extract_jobs.started_at
                    END,
                    finished_at = CASE
                        WHEN ? THEN 0
                        WHEN mikan_extract_jobs.status = 'success' THEN mikan_extract_jobs.finished_at
                        ELSE 0
                    END
                """,
                (
                    job_key,
                    int(priority or 0),
                    str(torrent.hash or ""),
                    torrent.name,
                    target_confidence,
                    match_version,
                    str(initial_file.get("path") or ""),
                    float(initial_file.get("timestamp") or 0),
                    str(initial_file.get("kind") or ""),
                    int(initial_file.get("size") or 0),
                    json.dumps(bangumi_ids, ensure_ascii=False),
                    json.dumps(episodes, ensure_ascii=False),
                    json.dumps(pending_entries, ensure_ascii=False, sort_keys=True),
                    json.dumps(_torrent_request_payload(torrent), ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    1 if force_requeue else 0,
                    1 if reset_job_state else 0,
                    1 if reset_job_state else 0,
                    1 if reset_job_state else 0,
                    1 if reset_job_state else 0,
                    1 if reset_job_state else 0,
                    1 if reset_job_state else 0,
                    1 if force_requeue else 0,
                ),
            )
        conn.commit()
        return queued
    except sqlite3.Error:
        if state_required:
            raise
        return 0
    finally:
        if conn is not None:
            conn.close()


def _mikan_extract_mapping_contract(pending_entries: list[dict[str, Any]]) -> tuple[float, str]:
    recovered = [
        entry
        for entry in pending_entries
        if _source_tag(entry.get("source")) == "qbit-recovered"
    ]
    if not recovered:
        return 1.0, "pending-release-v1"

    confidences: list[float] = []
    versions: set[str] = set()
    for entry in recovered:
        try:
            confidences.append(float(entry.get("recovery_match_confidence") or 0.0))
        except (TypeError, ValueError):
            confidences.append(0.0)
        version = str(entry.get("recovery_match_version") or "").strip()
        if version:
            versions.add(version)
    version = next(iter(versions)) if len(versions) == 1 else "unsafe-mixed-qbit-recovery"
    return min(confidences or [0.0]), version


def _unsafe_recovered_mapping_detail(pending_entries: list[dict[str, Any]]) -> str:
    recovered = [
        entry
        for entry in pending_entries
        if _source_tag(entry.get("source")) == "qbit-recovered"
    ]
    if not recovered:
        return ""

    bangumi_ids = {
        value
        for entry in recovered
        if (value := _coerce_int(entry.get("bangumi_id"))) is not None
    }
    if len(bangumi_ids) != 1:
        return "Recovered qBittorrent job does not identify exactly one bangumi"
    for entry in recovered:
        version = str(entry.get("recovery_match_version") or "").strip()
        try:
            confidence = float(entry.get("recovery_match_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = entry.get("recovery_match_evidence")
        if (
            not version.startswith(_QBIT_RECOVERY_MATCH_VERSION)
            or confidence < _QBIT_RECOVERY_MIN_CONFIDENCE
            or not isinstance(evidence, list)
            or not any(str(item).strip() for item in evidence)
        ):
            return (
                "Recovered qBittorrent job lacks a trusted mapping contract "
                f"(version={version or '-'} confidence={confidence:.3f})"
            )
    return ""


def _failed_mikan_extract_job_should_requeue(row: sqlite3.Row | tuple[Any, ...], now: float, config: AppConfig) -> bool:
    try:
        status = str(row[0] or "")
        result_json = str(row[3] or "{}")
        finished_at = float(row[2] or 0)
    except (IndexError, TypeError, ValueError):
        return False
    if status == "terminal_failed":
        return False
    result = _json_object(result_json)
    if result.get("retryable") is not True:
        return False
    retry_seconds = _mikan_extract_failed_retry_seconds(config)
    return retry_seconds <= 0 or finished_at <= 0 or now - finished_at >= retry_seconds


def requeue_failed_mikan_extract_jobs(
    config: AppConfig,
    *,
    include_terminal: bool = True,
    include_expired_running: bool = True,
) -> int:
    statuses = ("failed", "terminal_failed") if include_terminal else ("failed",)
    placeholders = ",".join("?" for _ in statuses)
    now = time.time()
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        updated = 0
        rows = conn.execute(
            f"""
            SELECT job_key, result_json
            FROM mikan_extract_jobs
            WHERE status IN ({placeholders})
            """,
            statuses,
        ).fetchall()
        retryable_keys = [
            str(row[0])
            for row in rows
            if _json_object(str(row[1] or "{}")).get("retryable") is True
        ]
        for job_key in retryable_keys:
            cursor = conn.execute(
                f"""
                UPDATE mikan_extract_jobs
                SET status = 'queued',
                    priority = MAX(COALESCE(priority, 0), 1000000),
                    attempts = 0,
                    worker_id = '',
                    lease_until = 0,
                    result_json = '{{}}',
                    last_error = '',
                    updated_at = ?,
                    started_at = 0,
                    finished_at = 0
                WHERE job_key = ?
                  AND status IN ({placeholders})
                """,
                (now, job_key, *statuses),
            )
            updated += int(cursor.rowcount or 0)
        if include_expired_running:
            cursor = conn.execute(
                """
                UPDATE mikan_extract_jobs
                SET status = 'queued',
                    priority = MAX(COALESCE(priority, 0), 1000000),
                    worker_id = '',
                    lease_until = 0,
                    result_json = '{}',
                    last_error = '',
                    updated_at = ?,
                    started_at = 0,
                    finished_at = 0
                WHERE status = 'running'
                  AND lease_until <= ?
                """,
                (now, now),
            )
            updated += int(cursor.rowcount or 0)
        conn.commit()
        return updated
    finally:
        if conn is not None:
            conn.close()


def requeue_mikan_extract_job(config: AppConfig, *, job_key: str) -> bool:
    """Requeue exactly one completed failure without disturbing other jobs."""
    normalized_key = str(job_key or "").strip()
    if not normalized_key:
        raise MikanWorkerError("A Mikan extraction job key is required")
    now = time.time()
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        cursor = conn.execute(
            """
            UPDATE mikan_extract_jobs
            SET status = 'queued',
                priority = MAX(COALESCE(priority, 0), 2000000),
                attempts = 0,
                worker_id = '',
                lease_until = 0,
                result_json = '{}',
                last_error = '',
                updated_at = ?,
                started_at = 0,
                finished_at = 0
            WHERE job_key = ?
              AND status IN ('failed', 'terminal_failed')
            """,
            (now, normalized_key),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        if conn is not None:
            conn.close()


def requeue_target_ambiguity_jobs(
    config: AppConfig,
    *,
    bangumi_id: int,
    torrent_hash: str = "",
) -> int:
    """Requeue reviewed ambiguity jobs for one source and optional exact torrent."""

    now = time.time()
    expected_hash = str(torrent_hash or "").strip().casefold()
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        rows = conn.execute(
            """
            SELECT job_key, torrent_hash, bangumi_ids_json, result_json
            FROM mikan_extract_jobs
            WHERE status IN ('failed', 'terminal_failed')
            """
        ).fetchall()
        keys: list[str] = []
        for job_key, job_torrent_hash, bangumi_ids_json, result_json in rows:
            if expected_hash and str(job_torrent_hash or "").strip().casefold() != expected_hash:
                continue
            result = _json_object(result_json)
            if str(result.get("failure_reason") or "") != "target_ambiguity":
                continue
            ids = {_coerce_int(value) for value in _json_list(bangumi_ids_json)}
            if int(bangumi_id) in ids:
                keys.append(str(job_key))
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        cursor = conn.execute(
            f"""
            UPDATE mikan_extract_jobs
            SET status='queued', priority=MAX(COALESCE(priority, 0), 2000000),
                attempts=0, worker_id='', lease_until=0, result_json='{{}}',
                last_error='', updated_at=?, started_at=0, finished_at=0
            WHERE job_key IN ({placeholders})
            """,
            (now, *keys),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        if conn is not None:
            conn.close()


def restore_target_ambiguity_pending_entries(
    config: AppConfig,
    *,
    bangumi_id: int,
    torrent_hash: str = "",
) -> int:
    """Restore an archived release for one source and optional exact torrent."""

    pending_path = _resolve_pending_path(config)
    expected_hash = str(torrent_hash or "").strip().casefold()
    lock = _mikan_operation_lock(config)
    if not lock.acquire():
        raise MikanWorkerError("Mikan state is busy while restoring reviewed target mapping")
    restored = 0
    try:
        pending = _load_pending(pending_path)
        for entry in _pending_items(pending).values():
            if not isinstance(entry, dict):
                continue
            if _coerce_int(entry.get("bangumi_id")) != int(bangumi_id):
                continue
            if str(entry.get("last_extract_failure_reason") or "") != "target_ambiguity":
                continue
            if expected_hash:
                entry_hashes = {
                    str(entry.get(key) or "").strip().casefold()
                    for key in ("last_failed_info_hash", "last_qbit_hash", "info_hash")
                    if str(entry.get(key) or "").strip()
                }
                if expected_hash not in entry_hashes:
                    continue
            torrent_url = str(entry.get("last_failed_torrent_url") or "")
            if not torrent_url:
                continue
            entry["torrent_url"] = torrent_url
            entry["title"] = str(entry.get("last_failed_title") or "")
            entry["source"] = str(entry.get("last_failed_source") or "mikan")
            entry["source_page"] = str(entry.get("last_failed_source_page") or "")
            info_hash = str(entry.get("last_failed_info_hash") or "").casefold()
            if info_hash:
                entry["info_hash"] = info_hash
                entry["last_qbit_hash"] = info_hash
            entry["queued_at"] = _utc_now().isoformat()
            for key in (
                "last_failure_reason",
                "last_extract_failed_at",
                "last_extract_failure_reason",
                "last_extract_failure_detail",
                "last_extract_context",
                "last_subtitle_diagnostics",
            ):
                entry.pop(key, None)
            restored += 1
        if restored:
            _save_pending(pending_path, pending)
        return restored
    finally:
        lock.release()


def _restore_target_ambiguity_pending_from_job_entries(
    config: AppConfig,
    *,
    bangumi_id: int,
    torrent_hash: str,
    job_entries: list[dict[str, Any]],
) -> int:
    """Rebuild missing pending rows from the exact failed extraction snapshot.

    The extraction job stores the pending rows that produced it.  This is the
    last safe recovery source when legacy cleanup already removed the live
    pending row.  Existing rows for another torrent are never overwritten.
    """

    expected_hash = str(torrent_hash or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_hash):
        raise MikanWorkerError("Reviewed target recovery requires an exact torrent hash")
    matching_entries = [
        dict(entry)
        for entry in job_entries
        if isinstance(entry, dict)
        and _coerce_int(entry.get("bangumi_id")) == int(bangumi_id)
        and (
            not _target_review_pending_hashes(entry)
            or expected_hash in _target_review_pending_hashes(entry)
        )
        and _target_review_recoverable_url(entry)
    ]
    if not matching_entries:
        return 0

    lock = _mikan_operation_lock(config)
    if not lock.acquire():
        raise MikanWorkerError("Mikan state is busy while rebuilding the reviewed source")
    restored = 0
    try:
        pending_path = _resolve_pending_path(config)
        pending = _load_pending(pending_path)
        items = _pending_items(pending)
        now_iso = _utc_now().isoformat()
        for snapshot in matching_entries:
            episodes = sorted(_pending_source_episode_numbers_from_entries([snapshot]))
            for episode in episodes:
                key = _pending_key(int(bangumi_id), int(episode))
                existing = items.get(key)
                if isinstance(existing, dict):
                    existing_hashes = _target_review_pending_hashes(existing)
                    if existing_hashes and expected_hash not in existing_hashes:
                        raise MikanWorkerError(
                            f"Pending episode {key} already belongs to another torrent"
                        )
                    entry = existing
                else:
                    entry = dict(snapshot)
                    entry["bangumi_id"] = int(bangumi_id)
                    entry["episode"] = int(episode)
                    items[key] = entry

                torrent_url = _target_review_recoverable_url(entry) or _target_review_recoverable_url(snapshot)
                if not torrent_url:
                    continue
                entry["torrent_url"] = torrent_url
                entry["title"] = str(
                    entry.get("last_failed_title")
                    or snapshot.get("last_failed_title")
                    or entry.get("title")
                    or snapshot.get("title")
                    or torrent_url
                )
                entry["source"] = str(
                    entry.get("last_failed_source")
                    or snapshot.get("last_failed_source")
                    or entry.get("source")
                    or snapshot.get("source")
                    or "mikan"
                )
                entry["source_page"] = str(
                    entry.get("last_failed_source_page")
                    or snapshot.get("last_failed_source_page")
                    or entry.get("source_page")
                    or snapshot.get("source_page")
                    or ""
                )
                entry["info_hash"] = expected_hash
                entry["last_qbit_hash"] = expected_hash
                entry["queued_at"] = now_iso
                for field_name in (
                    "last_failure_reason",
                    "last_extract_failed_at",
                    "last_extract_failure_reason",
                    "last_extract_failure_detail",
                    "last_extract_context",
                    "last_subtitle_diagnostics",
                ):
                    entry.pop(field_name, None)
                restored += 1
        if restored:
            _save_pending(pending_path, pending)
        return restored
    finally:
        lock.release()


def _wait_target_ambiguity_jobs_for_download(
    config: AppConfig,
    *,
    bangumi_id: int,
    torrent_hash: str,
) -> int:
    """Keep exact reviewed jobs dormant until qB reports the source complete."""

    now = time.time()
    expected_hash = str(torrent_hash or "").strip().casefold()
    connection: sqlite3.Connection | None = None
    try:
        connection = _mikan_state_connect(config)
        rows = connection.execute(
            """
            SELECT job_key, bangumi_ids_json, result_json
            FROM mikan_extract_jobs
            WHERE LOWER(torrent_hash) = ?
              AND status IN ('failed', 'terminal_failed', 'waiting_download')
            """,
            (expected_hash,),
        ).fetchall()
        keys = []
        for job_key, bangumi_ids_json, result_json in rows:
            ids = {_coerce_int(value) for value in _json_list(bangumi_ids_json)}
            result = _json_object(result_json)
            if int(bangumi_id) not in ids:
                continue
            if (
                str(result.get("failure_reason") or "") != "target_ambiguity"
                and str(result.get("waiting_reason") or "") != "reviewed_target_redownload"
            ):
                continue
            keys.append(str(job_key))
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        cursor = connection.execute(
            f"""
            UPDATE mikan_extract_jobs
            SET status='waiting_download',
                attempts=0,
                worker_id='',
                lease_until=0,
                last_error='',
                result_json=?,
                updated_at=?,
                started_at=0,
                finished_at=0
            WHERE job_key IN ({placeholders})
            """,
            (
                json.dumps(
                    {
                        "waiting_reason": "reviewed_target_redownload",
                        "torrent_hash": expected_hash,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
                *keys,
            ),
        )
        connection.commit()
        return int(cursor.rowcount or 0)
    finally:
        if connection is not None:
            connection.close()


def _open_target_ambiguity_reviews(config: AppConfig) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = list_review_items(
            config,
            status="open",
            kind="target_ambiguity",
            limit=200,
            offset=offset,
        )
        reviews.extend(batch)
        if len(batch) < 200:
            break
        offset += len(batch)
    return reviews


def _target_review_torrent_hash(review: dict[str, Any]) -> str:
    diagnosis = review.get("diagnosis") if isinstance(review.get("diagnosis"), dict) else {}
    for value in (
        diagnosis.get("torrent_hash"),
        review.get("canonical_key"),
        review.get("target_key"),
    ):
        match = re.search(
            r"(?<![0-9a-f])([0-9a-f]{40})(?![0-9a-f])",
            str(value or ""),
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).casefold()
    return ""


def _target_review_pending_hashes(entry: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    values = [
        entry.get("last_qbit_hash"),
        entry.get("info_hash"),
        entry.get("deferred_info_hash"),
        entry.get("last_failed_info_hash"),
        entry.get("last_completed_info_hash"),
        entry.get("last_superseded_info_hash"),
        entry.get("torrent_url"),
        entry.get("deferred_torrent_url"),
        entry.get("last_failed_torrent_url"),
        entry.get("last_completed_torrent_url"),
        entry.get("last_superseded_torrent_url"),
    ]
    for value in values:
        candidate = str(
            extract_torrent_info_hash(str(value or "")) or value or ""
        ).strip().casefold()
        if re.fullmatch(r"[0-9a-f]{40}", candidate):
            hashes.add(candidate)
    hashes.update(
        value
        for value in _raw_pending_failed_info_hashes(entry)
        if re.fullmatch(r"[0-9a-f]{40}", str(value or ""))
    )
    return hashes


def _target_review_recoverable_url(entry: dict[str, Any]) -> str:
    for key in (
        "last_failed_torrent_url",
        "torrent_url",
        "deferred_torrent_url",
        "last_completed_torrent_url",
    ):
        value = str(entry.get(key) or "").strip()
        if not value:
            continue
        scheme = urlparse(value).scheme.casefold()
        if scheme in {"http", "https", "magnet"}:
            return value
    return ""


def _target_review_extract_jobs(
    config: AppConfig,
    torrent_hashes: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    if not torrent_hashes:
        return {}, True
    jobs: dict[str, list[dict[str, Any]]] = {}
    connection: sqlite3.Connection | None = None
    try:
        connection = _mikan_state_existing_connect(config)
        connection.row_factory = sqlite3.Row
        normalized = sorted(torrent_hashes)
        for start in range(0, len(normalized), 400):
            chunk = normalized[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT job_key, status, torrent_hash, pending_entries_json,
                       torrent_json, result_json
                FROM mikan_extract_jobs
                WHERE LOWER(torrent_hash) IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for row in rows:
                torrent_hash = str(row["torrent_hash"] or "").strip().casefold()
                if torrent_hash not in torrent_hashes:
                    continue
                jobs.setdefault(torrent_hash, []).append({
                    "job_key": str(row["job_key"] or ""),
                    "status": str(row["status"] or ""),
                    "pending_entries": [
                        item
                        for item in _json_list(row["pending_entries_json"])
                        if isinstance(item, dict)
                    ],
                    "torrent": _json_object(row["torrent_json"]),
                    "result": _json_object(row["result_json"]),
                })
        return jobs, True
    except sqlite3.Error:
        return {}, False
    finally:
        if connection is not None:
            connection.close()


def _target_review_context_paths(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [
        str(value.get(key) or "").strip()
        for key in (
            "source_video",
            "mapped_root",
            "qbit_content_path",
            "qbit_raw_path",
        )
        if str(value.get(key) or "").strip()
    ]


def _target_review_source_paths(
    config: AppConfig,
    diagnosis: dict[str, Any],
    jobs: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
) -> list[Path]:
    raw_paths = _target_review_context_paths(diagnosis)
    raw_paths.extend(_target_review_context_paths(diagnosis.get("failure_context")))
    for entry in pending_entries:
        raw_paths.extend(_target_review_context_paths(entry.get("last_extract_context")))
    for job in jobs:
        torrent = _torrent_from_request_payload(job.get("torrent"))
        if torrent is not None:
            if torrent.content_path:
                raw_paths.append(torrent.content_path)
            elif torrent.save_path and torrent.name:
                save_path = str(torrent.save_path).rstrip("/\\")
                raw_paths.append(f"{save_path}/{torrent.name}")
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        raw_paths.extend(_target_review_context_paths(result.get("failure_context")))

    paths: list[Path] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        mapped = map_remote_path(raw_path, config.qbit_path_mappings)
        if mapped is None:
            continue
        marker = str(mapped)
        if marker in seen:
            continue
        seen.add(marker)
        paths.append(mapped)
    return paths


def _target_review_path_has_video(
    path: Path,
    video_extensions: set[str],
    *,
    max_entries: int = 2048,
    max_depth: int = 4,
) -> bool:
    try:
        if path.is_file():
            return path.suffix.casefold() in video_extensions
        if not path.is_dir():
            return False
    except OSError:
        return False

    visited = 0
    stack: list[tuple[Path, int]] = [(path, 0)]
    while stack and visited < max_entries:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            visited += 1
            if visited > max_entries:
                break
            try:
                if entry.is_file(follow_symlinks=False):
                    if Path(entry.name).suffix.casefold() in video_extensions:
                        return True
                elif depth < max_depth and entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return False


def resume_target_ambiguity_source(
    config: AppConfig,
    *,
    bangumi_id: int,
    torrent_hash: str,
    diagnosis: dict[str, Any] | None = None,
    qbit: QBitClient | None = None,
) -> dict[str, Any]:
    """Resume one reviewed source without guessing or starting extraction early.

    The exact hash is checked against the unfiltered qB list.  If qB no longer
    has it but an exact historical URL remains, that URL is re-added and the
    old extraction job waits for the normal completion poll.  No review should
    be resolved by the caller if this function raises.
    """

    expected_hash = str(torrent_hash or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_hash):
        raise MikanWorkerError("Reviewed target recovery requires an exact torrent hash")

    qbit_client = qbit
    if qbit_client is None:
        qbit_client = QBitClient(
            config.qbit_base_url,
            config.qbit_username,
            config.qbit_password,
            timeout_seconds=int(getattr(config, "qbit_timeout_seconds", 30) or 30),
        )
        qbit_client.login()
    torrents = qbit_client.list_torrents()
    exact_torrent = next(
        (
            torrent
            for torrent in torrents
            if str(torrent.hash or "").strip().casefold() == expected_hash
        ),
        None,
    )

    jobs_by_hash, jobs_available = _target_review_extract_jobs(config, {expected_hash})
    if not jobs_available:
        raise MikanWorkerError("Mikan extraction state is unavailable during reviewed recovery")
    jobs = jobs_by_hash.get(expected_hash, [])
    job_entries = [
        entry
        for job in jobs
        for entry in (job.get("pending_entries") or [])
        if isinstance(entry, dict)
    ]

    pending_path = _resolve_pending_path(config)
    pending = _load_pending(pending_path)
    current_entries = [
        entry
        for entry in _pending_items(pending).values()
        if isinstance(entry, dict)
        and _coerce_int(entry.get("bangumi_id")) == int(bangumi_id)
        and expected_hash in _target_review_pending_hashes(entry)
    ]
    all_entries = current_entries + job_entries

    video_extensions = {
        str(extension).casefold()
        for extension in getattr(config, "video_extensions", [".mkv", ".mp4"])
    }
    source_paths = _target_review_source_paths(
        config,
        diagnosis if isinstance(diagnosis, dict) else {},
        jobs,
        all_entries,
    )
    source_path = next(
        (
            str(path)
            for path in source_paths
            if _target_review_path_has_video(path, video_extensions)
        ),
        "",
    )

    restored = restore_target_ambiguity_pending_entries(
        config,
        bangumi_id=int(bangumi_id),
        torrent_hash=expected_hash,
    )
    if not restored and current_entries:
        # A prior idempotent attempt may already have activated these rows.
        restored = len(current_entries)

    if exact_torrent is not None:
        if float(exact_torrent.progress or 0.0) >= 0.999:
            requeued = requeue_target_ambiguity_jobs(
                config,
                bangumi_id=int(bangumi_id),
                torrent_hash=expected_hash,
            )
            return {
                "mode": "qbit_present",
                "torrent_hash": expected_hash,
                "restored_pending": restored,
                "requeued": requeued,
                "waiting_download": 0,
                "source_path": source_path,
            }
        waiting = _wait_target_ambiguity_jobs_for_download(
            config,
            bangumi_id=int(bangumi_id),
            torrent_hash=expected_hash,
        )
        return {
            "mode": "waiting_download",
            "torrent_hash": expected_hash,
            "restored_pending": restored,
            "requeued": 0,
            "waiting_download": waiting,
            "source_path": source_path,
        }

    if source_path:
        requeued = requeue_target_ambiguity_jobs(
            config,
            bangumi_id=int(bangumi_id),
            torrent_hash=expected_hash,
        )
        return {
            "mode": "source_files_present",
            "torrent_hash": expected_hash,
            "restored_pending": restored,
            "requeued": requeued,
            "waiting_download": 0,
            "source_path": source_path,
        }

    recoverable_url = next(
        (
            value
            for entry in all_entries
            if (value := _target_review_recoverable_url(entry))
        ),
        "",
    )
    if not recoverable_url:
        raise MikanWorkerError(
            "The reviewed torrent and downloaded source files are gone, and no exact redownload URL remains"
        )
    if not restored:
        restored = _restore_target_ambiguity_pending_from_job_entries(
            config,
            bangumi_id=int(bangumi_id),
            torrent_hash=expected_hash,
            job_entries=job_entries,
        )
    if not restored:
        raise MikanWorkerError(
            "The reviewed source can be downloaded, but its episode state cannot be restored safely"
        )

    queue_lock = _mikan_queue_lock(config)
    if not queue_lock.acquire():
        raise MikanWorkerError("qBittorrent queue is busy while restoring the reviewed source")
    try:
        qbit_client.ensure_category(
            getattr(config, "qbit_category", None),
            save_path=getattr(config, "qbit_save_path", None),
        )
        source = next(
            (
                str(
                    entry.get("last_failed_source")
                    or entry.get("source")
                    or "mikan"
                )
                for entry in all_entries
                if isinstance(entry, dict)
            ),
            "mikan",
        )
        qbit_client.add_url(
            recoverable_url,
            save_path=getattr(config, "qbit_save_path", None),
            category=getattr(config, "qbit_category", None),
            tags=_queue_tags(getattr(config, "qbit_tags", []), source),
            paused=bool(getattr(config, "qbit_paused", False)),
        )
    finally:
        queue_lock.release()

    waiting = _wait_target_ambiguity_jobs_for_download(
        config,
        bangumi_id=int(bangumi_id),
        torrent_hash=expected_hash,
    )
    return {
        "mode": "redownload_queued",
        "torrent_hash": expected_hash,
        "restored_pending": restored,
        "requeued": 0,
        "waiting_download": waiting,
        "source_path": "",
    }


def reconcile_target_ambiguity_review_sources(
    config: AppConfig,
    torrents: list[QBitTorrent],
    *,
    reviews: list[dict[str, Any]] | None = None,
    now_timestamp: float | None = None,
    missing_grace_seconds: float = MIKAN_REVIEW_SOURCE_MISSING_GRACE_SECONDS,
) -> dict[str, int]:
    """Reconcile durable source reviews with qB, files, and redownload state.

    Audit rows are never deleted.  A review is moved to resolved only after the
    exact torrent is absent, no source video remains, no recoverable source URL
    exists, no extraction/command is active, and the absence survives a grace
    period.
    """

    review_items = list(reviews) if reviews is not None else _open_target_ambiguity_reviews(config)
    summary = {
        "checked": len(review_items),
        "changed": 0,
        "resolved": 0,
        "qbit_present": 0,
        "source_files_present": 0,
        "redownload_available": 0,
        "processing": 0,
        "source_unavailable_pending": 0,
        "source_gone": 0,
        "unknown": 0,
    }
    if not review_items:
        return summary

    now_ts = float(now_timestamp if now_timestamp is not None else time.time())
    hashes_by_review = {
        str(review.get("review_id") or ""): _target_review_torrent_hash(review)
        for review in review_items
    }
    wanted_hashes = {value for value in hashes_by_review.values() if value}
    qbit_hashes = {
        str(torrent.hash or "").strip().casefold()
        for torrent in torrents
        if re.fullmatch(r"[0-9a-fA-F]{40}", str(torrent.hash or "").strip())
    }
    active_commands = active_review_command_ids(
        config,
        {str(review.get("review_id") or "") for review in review_items},
    )
    jobs_by_hash, jobs_available = _target_review_extract_jobs(config, wanted_hashes)

    pending_entries_by_hash: dict[str, list[dict[str, Any]]] = {
        torrent_hash: [] for torrent_hash in wanted_hashes
    }
    pending_available = True
    try:
        pending = _load_pending(_resolve_pending_path(config))
        for entry in _pending_items(pending).values():
            if not isinstance(entry, dict):
                continue
            for torrent_hash in _target_review_pending_hashes(entry) & wanted_hashes:
                pending_entries_by_hash.setdefault(torrent_hash, []).append(entry)
    except (MikanWorkerError, OSError, sqlite3.Error):
        pending_available = False

    video_extensions = {
        str(extension).casefold()
        for extension in getattr(config, "video_extensions", [".mkv", ".mp4"])
    }
    path_cache: dict[str, bool] = {}
    storage_roots = [
        Path(str(mapping.get("local") or ""))
        for mapping in getattr(config, "qbit_path_mappings", [])
        if str(mapping.get("local") or "").strip()
    ]
    storage_available = not storage_roots or any(root.exists() for root in storage_roots)

    for review in review_items:
        review_id = str(review.get("review_id") or "")
        diagnosis = review.get("diagnosis") if isinstance(review.get("diagnosis"), dict) else {}
        torrent_hash = hashes_by_review.get(review_id, "")
        if not review_id or not torrent_hash:
            lifecycle = "unknown"
            changed = update_review_source_lifecycle(
                config,
                review_id,
                lifecycle=lifecycle,
                torrent_in_qbit=False,
                source_files_present=False,
                redownload_available=False,
                processing=False,
            ) if review_id else False
            summary["unknown"] += 1
            summary["changed"] += int(changed)
            continue

        jobs = jobs_by_hash.get(torrent_hash, [])
        entries = list(pending_entries_by_hash.get(torrent_hash, []))
        for job in jobs:
            entries.extend(job.get("pending_entries") or [])
        processing = (
            review_id in active_commands
            or any(
                str(job.get("status") or "") in {"queued", "running", "waiting_download"}
                for job in jobs
            )
        )
        torrent_in_qbit = torrent_hash in qbit_hashes
        redownload_available = any(
            _target_review_recoverable_url(entry)
            for entry in entries
        )
        existing_path = ""
        if not processing and not torrent_in_qbit:
            source_paths = _target_review_source_paths(config, diagnosis, jobs, entries)
            for source_path in source_paths:
                marker = str(source_path)
                if marker not in path_cache:
                    path_cache[marker] = _target_review_path_has_video(
                        source_path,
                        video_extensions,
                    )
                if path_cache[marker]:
                    existing_path = marker
                    break
        source_files_present = bool(existing_path)

        previous_missing_since = _safe_float(diagnosis.get("source_missing_since")) or 0.0
        missing_since = 0.0
        if processing:
            lifecycle = "processing"
        elif torrent_in_qbit:
            lifecycle = "qbit_present"
        elif source_files_present:
            lifecycle = "source_files_present"
        elif redownload_available:
            lifecycle = "redownload_available"
        elif not jobs_available or not pending_available or not storage_available:
            lifecycle = "unknown"
        else:
            missing_since = previous_missing_since or now_ts
            if now_ts - missing_since >= max(0.0, float(missing_grace_seconds)):
                lifecycle = "source_gone"
            else:
                lifecycle = "source_unavailable_pending"

        changed = update_review_source_lifecycle(
            config,
            review_id,
            lifecycle=lifecycle,
            torrent_in_qbit=torrent_in_qbit,
            source_files_present=source_files_present,
            redownload_available=redownload_available,
            processing=processing,
            source_path=existing_path,
            missing_since=missing_since,
        )
        summary[lifecycle] += 1
        summary["changed"] += int(changed)
        if lifecycle != "source_gone":
            continue
        resolved = resolve_review_item_if_idle(
            config,
            review_id,
            {
                "action": "auto_close_unavailable_source",
                "reason": "source_gone",
                "automatic": True,
                "torrent_hash": torrent_hash,
                "message": (
                    "qBittorrent torrent, downloaded source files, and a "
                    "recoverable torrent URL are no longer available."
                ),
                "resolved_by": "mikan_source_lifecycle_reconcile",
            },
        )
        summary["resolved"] += int(resolved)
    return summary


def requeue_interrupted_mikan_extract_jobs(config: AppConfig) -> int:
    """Recover jobs owned by a previous primary worker process.

    This is called once while the container's background Mikan watcher starts.
    Any extractor from the previous container/process is gone at that point, so
    waiting for the old 15-minute lease only creates an avoidable outage.
    """
    now = time.time()
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_connect(config)
        cursor = conn.execute(
            """
            UPDATE mikan_extract_jobs
            SET status = 'queued',
                priority = MAX(COALESCE(priority, 0), 1000000),
                worker_id = '',
                lease_until = 0,
                last_error = 'Recovered after worker restart',
                updated_at = ?,
                started_at = 0,
                finished_at = 0
            WHERE status = 'running'
            """,
            (now,),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        if conn is not None:
            conn.close()


def _mikan_extract_active_job_count(config: AppConfig) -> int:
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        row = conn.execute(
            "SELECT COUNT(*) FROM mikan_extract_jobs WHERE status IN ('queued', 'running')"
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0
    finally:
        if conn is not None:
            conn.close()


def _mikan_extract_dispatch_counts(config: AppConfig) -> tuple[int, int]:
    now = time.time()
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        row = conn.execute(
            """
            SELECT
                SUM(
                    CASE
                        WHEN status = 'queued' AND lease_until <= ? THEN 1
                        WHEN status = 'running' AND lease_until <= ? THEN 1
                        ELSE 0
                    END
                ),
                SUM(CASE WHEN status = 'running' AND lease_until > ? THEN 1 ELSE 0 END)
            FROM mikan_extract_jobs
            """,
            (now, now, now),
        ).fetchone()
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)
    except sqlite3.Error:
        return 0, 0
    finally:
        if conn is not None:
            conn.close()


def _claim_mikan_extract_jobs(config: AppConfig, *, limit: int) -> list[MikanExtractJob]:
    if limit <= 0:
        return []
    worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
    now = time.time()
    lease_until = now + _mikan_extract_lease_seconds(config)
    conn = _mikan_state_connect(config)
    jobs: list[MikanExtractJob] = []
    try:
        rows = conn.execute(
            """
            SELECT job_key, torrent_json, pending_entries_json, attempts
            FROM mikan_extract_jobs
            WHERE (status = 'queued' AND lease_until <= ?)
               OR (status = 'running' AND lease_until <= ?)
            ORDER BY priority DESC, created_at DESC
            LIMIT ?
            """,
            (now, now, int(limit)),
        ).fetchall()
        first_start_latencies: list[float] = []
        for job_key, torrent_json, pending_entries_json, attempts in rows:
            torrent = _torrent_from_request_payload(_json_object(torrent_json))
            pending_entries_raw = _json_list(pending_entries_json)
            pending_entries = [entry for entry in pending_entries_raw if isinstance(entry, dict)]
            unsafe_detail = _unsafe_recovered_mapping_detail(pending_entries)
            if unsafe_detail:
                review_id = ""
                try:
                    source_time_fields = _review_source_time_fields(
                        torrent,
                        pending_entries,
                    ) if torrent is not None else {
                        "source_published_at": 0.0,
                        "source_published_precision": "",
                        "torrent_created_at": 0.0,
                        "torrent_added_at": 0.0,
                        "torrent_completed_at": 0.0,
                    }
                    review_id = upsert_review_item(
                        config,
                        kind="target_ambiguity",
                        target_key=str(job_key),
                        summary=f"Recovered qBittorrent mapping requires review: {(torrent.name if torrent else job_key)}",
                        diagnosis={
                            "job_key": str(job_key),
                            "torrent_hash": str(torrent.hash if torrent else ""),
                            "torrent_name": str(torrent.name if torrent else ""),
                            "bangumi_ids": sorted(
                                {
                                    value
                                    for entry in pending_entries
                                    if (value := _coerce_int(entry.get("bangumi_id"))) is not None
                                }
                            ),
                            "reason": "unsafe_recovered_mapping",
                            "detail": unsafe_detail,
                            **source_time_fields,
                        },
                        candidates=[],
                        severity="error",
                    )
                except sqlite3.Error:
                    pass
                detail = unsafe_detail + (f"; review item {review_id}" if review_id else "")
                conn.execute(
                    """
                    UPDATE mikan_extract_jobs
                    SET status = 'terminal_failed',
                        worker_id = '',
                        lease_until = 0,
                        target_confidence = 0,
                        match_version = 'unsafe-recovery-blocked-v3',
                        result_json = ?,
                        last_error = ?,
                        finished_at = ?,
                        updated_at = ?
                    WHERE job_key = ?
                      AND ((status = 'queued' AND lease_until <= ?) OR (status = 'running' AND lease_until <= ?))
                    """,
                    (
                        json.dumps(
                            {
                                "failure_reason": "unsafe_recovered_mapping",
                                "failure_detail": detail,
                                "review_id": review_id,
                                "retryable": False,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        detail[:2000],
                        now,
                        now,
                        job_key,
                        now,
                        now,
                    ),
                )
                continue
            cursor = conn.execute(
                """
                UPDATE mikan_extract_jobs
                SET status = 'running',
                    worker_id = ?,
                    lease_until = ?,
                    attempts = attempts + 1,
                    started_at = ?,
                    finished_at = 0,
                    progress_current = 0,
                    progress_total = 0,
                    match_version = CASE WHEN match_version = '' THEN 'scoped-v2' ELSE match_version END,
                    updated_at = ?,
                    last_error = ''
                WHERE job_key = ?
                  AND ((status = 'queued' AND lease_until <= ?) OR (status = 'running' AND lease_until <= ?))
                """,
                (worker_id, lease_until, now, now, job_key, now, now),
            )
            if cursor.rowcount != 1:
                continue
            if pending_entries and all(
                _pending_entry_is_release_part_false_positive(entry)
                for entry in pending_entries
            ):
                detail = "Rejected legacy extraction job whose release Part/Vol number is not an episode"
                conn.execute(
                    """
                    UPDATE mikan_extract_jobs
                    SET status = 'terminal_failed',
                        worker_id = '',
                        lease_until = 0,
                        result_json = ?,
                        last_error = ?,
                        finished_at = ?,
                        updated_at = ?
                    WHERE job_key = ? AND worker_id = ?
                    """,
                    (
                        json.dumps(
                            {
                                "failure_reason": "invalid_episode_metadata",
                                "failure_detail": detail,
                                "retryable": False,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        detail,
                        now,
                        now,
                        job_key,
                        worker_id,
                    ),
                )
                continue
            if torrent is None:
                conn.execute(
                    """
                    UPDATE mikan_extract_jobs
                    SET status = 'failed', last_error = ?, finished_at = ?, updated_at = ?, lease_until = 0
                    WHERE job_key = ?
                    """,
                    ("Invalid torrent payload", now, now, job_key),
                )
                continue
            jobs.append(MikanExtractJob(str(job_key), torrent, pending_entries, worker_id))
            completion_on = float(torrent.completion_on or 0)
            if int(attempts or 0) == 0 and completion_on > 0 and now >= completion_on:
                latency = now - completion_on
                if latency <= 7 * 86400:
                    first_start_latencies.append(latency)
        conn.commit()
    finally:
        conn.close()
    for latency in first_start_latencies:
        try:
            record_daily_sample(config, "mikan.extract_start_latency_seconds", latency)
        except (OSError, sqlite3.Error, ValueError):
            pass
    return jobs


def _requeue_claimed_mikan_extract_job(
    config: AppConfig,
    job: MikanExtractJob,
    *,
    reason: str,
    delay_seconds: float = 0.0,
) -> bool:
    now = time.time()
    available_at = now + max(0.0, float(delay_seconds or 0))
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        cursor = conn.execute(
            """
            UPDATE mikan_extract_jobs
            SET status = 'queued',
                worker_id = '',
                lease_until = ?,
                last_error = ?,
                updated_at = ?,
                started_at = 0,
                finished_at = 0
            WHERE job_key = ?
              AND status = 'running'
              AND worker_id = ?
            """,
            (available_at, str(reason or "")[:2000], now, job.job_key, job.worker_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        if conn is not None:
            conn.close()


def _renew_mikan_extract_job_lease(config: AppConfig, job: MikanExtractJob) -> bool:
    now = time.time()
    lease_until = now + _mikan_extract_lease_seconds(config)
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        cursor = conn.execute(
            """
            UPDATE mikan_extract_jobs
            SET lease_until = ?, updated_at = ?
            WHERE job_key = ?
              AND status = 'running'
              AND worker_id = ?
            """,
            (lease_until, now, job.job_key, job.worker_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def _update_mikan_extract_job_progress(
    config: AppConfig,
    job: MikanExtractJob,
    *,
    processed: int,
    total: int,
    current: str,
) -> bool:
    now = time.time()
    total_value = max(0, int(total or 0))
    processed_value = max(0, min(int(processed or 0), total_value or int(processed or 0)))
    payload = {
        "progress": {
            "processed": processed_value,
            "total": total_value,
            "percent": round(processed_value * 100.0 / total_value, 1) if total_value else 0.0,
            "current": str(current or "")[-1000:],
        }
    }
    current_file = file_time_metadata(current) if current else {}
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        cursor = conn.execute(
            """
            UPDATE mikan_extract_jobs
            SET result_json = ?,
                progress_current = ?,
                progress_total = ?,
                target_path = CASE WHEN ? <> '' THEN ? ELSE target_path END,
                current_file_timestamp = CASE WHEN ? > 0 THEN ? ELSE current_file_timestamp END,
                current_file_time_kind = CASE WHEN ? <> '' THEN ? ELSE current_file_time_kind END,
                current_file_size = CASE WHEN ? >= 0 THEN ? ELSE current_file_size END,
                match_version = CASE WHEN match_version = '' THEN 'scoped-v2' ELSE match_version END,
                updated_at = ?
            WHERE job_key = ?
              AND status = 'running'
              AND worker_id = ?
            """,
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                processed_value,
                total_value,
                str(current or ""),
                str(current or "")[-2000:],
                float(current_file.get("timestamp") or 0),
                float(current_file.get("timestamp") or 0),
                str(current_file.get("kind") or ""),
                str(current_file.get("kind") or ""),
                int(current_file.get("size") if current_file else -1),
                int(current_file.get("size") if current_file else -1),
                now,
                job.job_key,
                job.worker_id,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()


def _finish_mikan_extract_job(
    config: AppConfig,
    job_key: str,
    status: str,
    result: MikanExtractResult,
    *,
    worker_id: str = "",
) -> bool | None:
    now = time.time()
    if status == "success":
        final_status = "success"
    elif _mikan_extract_result_should_be_replaced(result):
        final_status = "replaced"
    elif result.retryable or _mikan_extract_failure_allows_replacement(result.failure_reason):
        final_status = "failed"
    else:
        final_status = "terminal_failed"
    conn: sqlite3.Connection | None = None
    try:
        conn = _mikan_state_existing_connect(config)
        where_sql = "WHERE job_key = ?"
        params: list[Any] = [
            final_status,
            json.dumps(_extract_result_request_payload(result), ensure_ascii=False, sort_keys=True),
            result.failure_detail[:1000] if final_status != "success" else "",
            now,
            now,
            job_key,
        ]
        if worker_id:
            where_sql += " AND status = 'running' AND worker_id = ?"
            params.append(worker_id)
        cursor = conn.execute(
            f"""
            UPDATE mikan_extract_jobs
            SET status = ?,
                lease_until = 0,
                worker_id = '',
                result_json = ?,
                last_error = ?,
                updated_at = ?,
                finished_at = ?
            {where_sql}
            """,
            params,
        )
        conn.commit()
        return cursor.rowcount == 1
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()


def _mikan_extract_failure_allows_replacement(reason: str) -> bool:
    return _mikan_extract_failure_bucket(reason) in {
        "mapped_path_missing",
        "target_missing",
        "no_usable_chinese",
        "no_subtitle_streams",
        "extract_timeout",
    }


def _mikan_extract_failure_suppresses_replacement(reason: str) -> bool:
    return str(reason or "").strip().casefold() in {
        "target_ambiguity",
        "unsafe_recovered_mapping",
        "extract_cancelled_by_user",
    }


def _mikan_extract_result_should_be_replaced(result: MikanExtractResult) -> bool:
    return (
        result.extracted_count <= 0
        and not bool(result.retryable)
        and _mikan_extract_failure_allows_replacement(result.failure_reason)
    )


def _mikan_extract_failure_allows_same_source_retry(reason: str) -> bool:
    return _mikan_extract_failure_bucket(reason) in {
        "extract_error",
    }


def _mikan_extract_failure_bucket(reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    if normalized in {"source_video_missing", "content_path_unmapped"}:
        return "mapped_path_missing"
    if normalized in {"target_video_not_found", "source_episode_not_found", "extra_video_only"}:
        return "target_missing"
    if normalized in {
        "subtitle_language_not_supported",
        "sidecar_language_not_supported",
        "no_usable_subtitles",
        "subtitle_validation_failed",
    }:
        return "no_usable_chinese"
    if normalized in {"no_text_subtitle_streams", "image_subtitles_only"}:
        return "no_subtitle_streams"
    if normalized in {"extract_timeout"}:
        return "extract_timeout"
    if normalized in {"ffmpeg_extract_failed", "subtitle_probe_failed", "extract_exception"}:
        return "extract_error"
    return normalized or "unknown"


def _mikan_job_connect(config: AppConfig) -> sqlite3.Connection:
    db_path = _mikan_state_db_path_from_config(config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    _ensure_mikan_job_tables(conn)
    return conn


def _ensure_mikan_job_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mikan_jobs (
            job_name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            worker_id TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            requested_at REAL NOT NULL DEFAULT 0,
            started_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            finished_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_jobs_status ON mikan_jobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_jobs_lease_until ON mikan_jobs(lease_until)")


def _request_mikan_job(config: AppConfig, job_name: str, payload: dict[str, Any]) -> None:
    now = time.time()
    conn = _mikan_job_connect(config)
    try:
        row = conn.execute("SELECT request_count, payload_json FROM mikan_jobs WHERE job_name = ?", (job_name,)).fetchone()
        existing_payload = _json_object(row[1]) if row is not None else {}
        request_count = int(payload.get("request_count") or ((int(row[0] or 0) + 1) if row is not None else 1))
        merged_payload = {**existing_payload, **payload, "request_count": request_count}
        conn.execute(
            """
            INSERT INTO mikan_jobs(
                job_name, status, request_count, worker_id, lease_until, payload_json,
                requested_at, started_at, updated_at, finished_at, last_error
            )
            VALUES (?, 'requested', ?, '', 0, ?, ?, 0, ?, 0, '')
            ON CONFLICT(job_name) DO UPDATE SET
                status = CASE
                    WHEN mikan_jobs.status = 'running' AND mikan_jobs.lease_until > excluded.updated_at
                    THEN mikan_jobs.status
                    ELSE 'requested'
                END,
                request_count = excluded.request_count,
                payload_json = excluded.payload_json,
                requested_at = excluded.requested_at,
                updated_at = excluded.updated_at,
                finished_at = 0,
                last_error = ''
            """,
            (job_name, request_count, json.dumps(merged_payload, ensure_ascii=False, sort_keys=True), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _persist_mikan_extract_job_torrent_snapshots(
    config: AppConfig,
    jobs: list[MikanExtractJob],
) -> None:
    """Persist qB timing metadata discovered after a job was claimed."""

    snapshots = [
        (
            json.dumps(_torrent_request_payload(job.torrent), ensure_ascii=False, sort_keys=True),
            job.job_key,
            job.worker_id,
        )
        for job in jobs
        if job.worker_id and job.torrent.creation_date
    ]
    if not snapshots:
        return
    conn = _mikan_state_connect(config)
    try:
        conn.executemany(
            """
            UPDATE mikan_extract_jobs
            SET torrent_json = ?
            WHERE job_key = ? AND worker_id = ? AND status = 'running'
            """,
            snapshots,
        )
        conn.commit()
    except sqlite3.Error:
        # Timing metadata is observability only.  Extraction must not fail or
        # lose its lease because a best-effort snapshot write was contended.
        pass
    finally:
        conn.close()


def _claim_mikan_job(
    config: AppConfig,
    job_name: str,
    *,
    payload: dict[str, Any] | None = None,
    lease_seconds: float = 900.0,
) -> MikanJobLease | None:
    now = time.time()
    worker_id = f"{os.getpid()}:{uuid.uuid4().hex}"
    lease_until = now + max(30.0, float(lease_seconds or 0))
    conn = _mikan_job_connect(config)
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, lease_until, payload_json, request_count FROM mikan_jobs WHERE job_name = ?",
            (job_name,),
        ).fetchone()
        if row is not None and str(row[0]) == "running" and float(row[1] or 0) > now:
            conn.execute("ROLLBACK")
            return None
        existing_payload = _json_object(row[2]) if row is not None else {}
        merged_payload = {**existing_payload, **(payload or {})}
        request_count = int(row[3] or 0) if row is not None else int(merged_payload.get("request_count") or 1)
        conn.execute(
            """
            INSERT INTO mikan_jobs(
                job_name, status, request_count, worker_id, lease_until, payload_json,
                requested_at, started_at, updated_at, finished_at, last_error
            )
            VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?, 0, '')
            ON CONFLICT(job_name) DO UPDATE SET
                status = 'running',
                request_count = excluded.request_count,
                worker_id = excluded.worker_id,
                lease_until = excluded.lease_until,
                payload_json = excluded.payload_json,
                started_at = excluded.started_at,
                updated_at = excluded.updated_at,
                finished_at = 0,
                last_error = ''
            """,
            (
                job_name,
                request_count,
                worker_id,
                lease_until,
                json.dumps(merged_payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
                now,
            ),
        )
        conn.execute("COMMIT")
        return MikanJobLease(name=job_name, worker_id=worker_id, db_path=_mikan_state_db_path_from_config(config))
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _finish_mikan_job(config: AppConfig, lease: MikanJobLease, result: dict[str, Any]) -> None:
    _complete_mikan_job(config, lease, status="done", updates=result, error="")


def _defer_mikan_job(config: AppConfig, lease: MikanJobLease, result: dict[str, Any]) -> None:
    _complete_mikan_job(config, lease, status="deferred", updates=result, error="")


def _fail_mikan_job(config: AppConfig, lease: MikanJobLease, error: str) -> None:
    _complete_mikan_job(config, lease, status="failed", updates={}, error=error)


def _complete_mikan_job(
    config: AppConfig,
    lease: MikanJobLease,
    *,
    status: str,
    updates: dict[str, Any],
    error: str,
) -> None:
    now = time.time()
    conn = _mikan_job_connect(config)
    try:
        row = conn.execute(
            "SELECT payload_json FROM mikan_jobs WHERE job_name = ? AND worker_id = ?",
            (lease.name, lease.worker_id),
        ).fetchone()
        if row is None:
            return
        payload = {**_json_object(row[0]), **updates}
        conn.execute(
            """
            UPDATE mikan_jobs
            SET status = ?,
                worker_id = '',
                lease_until = 0,
                payload_json = ?,
                updated_at = ?,
                finished_at = CASE WHEN ? IN ('done', 'failed') THEN ? ELSE 0 END,
                last_error = ?
            WHERE job_name = ? AND worker_id = ?
            """,
            (
                status,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                status,
                now,
                error,
                lease.name,
                lease.worker_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _mikan_job_payload(config: AppConfig, job_name: str) -> dict[str, Any] | None:
    conn = _mikan_job_connect(config)
    try:
        row = conn.execute(
            """
            SELECT status, request_count, worker_id, lease_until, payload_json, requested_at, started_at, updated_at, finished_at, last_error
            FROM mikan_jobs
            WHERE job_name = ?
            """,
            (job_name,),
        ).fetchone()
        if row is None:
            return None
        payload = _json_object(row[4])
        payload.update(
            {
                "job_name": job_name,
                "job_status": str(row[0] or ""),
                "request_count": int(row[1] or 0),
                "worker_id": str(row[2] or ""),
                "lease_until": float(row[3] or 0),
                "requested_at_epoch": float(row[5] or 0),
                "started_at_epoch": float(row[6] or 0),
                "updated_at_epoch": float(row[7] or 0),
                "finished_at_epoch": float(row[8] or 0),
                "last_error": str(row[9] or ""),
            }
        )
        return payload
    finally:
        conn.close()


def _mikan_job_pending_or_running(config: AppConfig, job_name: str) -> bool:
    payload = _mikan_job_payload(config, job_name)
    if payload is None:
        return False
    status = str(payload.get("job_status") or "")
    if status in {"requested", "deferred"}:
        return True
    if status == "running":
        return float(payload.get("lease_until") or 0) > time.time()
    return False


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _ensure_mikan_state_tables(conn: sqlite3.Connection) -> None:
    _ensure_mikan_job_tables(conn)
    ensure_mikan_cache_tables(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mikan_download_items (
            key TEXT PRIMARY KEY,
            bangumi_id INTEGER,
            episode INTEGER,
            episodes_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            torrent_url TEXT NOT NULL DEFAULT '',
            failed_urls_json TEXT NOT NULL DEFAULT '[]',
            queued_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0,
            last_progress REAL,
            last_downloaded INTEGER,
            last_dlspeed INTEGER NOT NULL DEFAULT 0,
            last_qbit_state TEXT,
            last_qbit_hash TEXT,
            last_qbit_name TEXT,
            last_qbit_sync_at REAL NOT NULL DEFAULT 0,
            completed_at REAL NOT NULL DEFAULT 0,
            last_extracted_at REAL NOT NULL DEFAULT 0,
            last_extracted_count INTEGER NOT NULL DEFAULT 0,
            total_extracted_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            last_failure_reason TEXT,
            last_extract_failure_reason TEXT,
            last_extract_failure_detail TEXT,
            no_candidate_until REAL,
            timed_out_at REAL NOT NULL DEFAULT 0,
            next_action TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_items_status ON mikan_download_items(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_items_qbit_state ON mikan_download_items(last_qbit_state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_items_updated_at ON mikan_download_items(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_items_torrent_url ON mikan_download_items(torrent_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_items_status_updated_at ON mikan_download_items(status, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_items_last_qbit_hash ON mikan_download_items(last_qbit_hash)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mikan_download_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            bangumi_id INTEGER,
            episode INTEGER,
            event TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}',
            event_key TEXT NOT NULL DEFAULT '',
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            last_seen_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_events_created_at ON mikan_download_events(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_events_event_created_at ON mikan_download_events(event, created_at)")
    _ensure_mikan_event_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anime_episode_index (
            bangumi_id INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            season INTEGER,
            path TEXT NOT NULL,
            series_path TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(bangumi_id, episode, path)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_anime_episode_index_lookup ON anime_episode_index(bangumi_id, episode)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_anime_episode_index_path ON anime_episode_index(path)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS anime_episode_index_roots (
            bangumi_id INTEGER NOT NULL,
            series_path TEXT NOT NULL,
            scanned_at REAL NOT NULL,
            PRIMARY KEY(bangumi_id, series_path)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_anime_episode_index_roots_scanned "
        "ON anime_episode_index_roots(scanned_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mikan_extract_jobs (
            job_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            worker_id TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,
            torrent_hash TEXT NOT NULL DEFAULT '',
            torrent_name TEXT NOT NULL DEFAULT '',
            target_path TEXT NOT NULL DEFAULT '',
            target_confidence REAL NOT NULL DEFAULT 0,
            match_version TEXT NOT NULL DEFAULT '',
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            current_file_timestamp REAL NOT NULL DEFAULT 0,
            current_file_time_kind TEXT NOT NULL DEFAULT '',
            current_file_size INTEGER NOT NULL DEFAULT 0,
            bangumi_ids_json TEXT NOT NULL DEFAULT '[]',
            episodes_json TEXT NOT NULL DEFAULT '[]',
            pending_entries_json TEXT NOT NULL DEFAULT '[]',
            torrent_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            started_at REAL NOT NULL DEFAULT 0,
            finished_at REAL NOT NULL DEFAULT 0
        )
        """
    )
    _ensure_mikan_extract_job_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_extract_jobs_status_priority ON mikan_extract_jobs(status, priority DESC, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_extract_jobs_lease ON mikan_extract_jobs(status, lease_until)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mikan_extract_jobs_torrent_hash ON mikan_extract_jobs(torrent_hash)")
    _migrate_mikan_extract_job_terminal_statuses(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mikan_state_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    _compact_legacy_mikan_events(conn)


def _ensure_mikan_event_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(mikan_download_events)").fetchall()}
    additions = {
        "detail_json": "ALTER TABLE mikan_download_events ADD COLUMN detail_json TEXT NOT NULL DEFAULT '{}'",
        "event_key": "ALTER TABLE mikan_download_events ADD COLUMN event_key TEXT NOT NULL DEFAULT ''",
        "occurrence_count": "ALTER TABLE mikan_download_events ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1",
        "last_seen_at": "ALTER TABLE mikan_download_events ADD COLUMN last_seen_at REAL NOT NULL DEFAULT 0",
    }
    for column, statement in additions.items():
        if column not in existing:
            conn.execute(statement)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mikan_events_key_last_seen ON mikan_download_events(event_key, last_seen_at)"
    )
    conn.execute("UPDATE mikan_download_events SET last_seen_at=created_at WHERE last_seen_at<=0")


def _compact_legacy_mikan_events(conn: sqlite3.Connection) -> None:
    marker = conn.execute(
        "SELECT value FROM mikan_state_meta WHERE key='download_events_compacted_v2'"
    ).fetchone()
    if marker is not None and str(marker[0] or "") == "1":
        return
    rows = conn.execute(
        """
        SELECT id, key, event, detail, detail_json, occurrence_count,
               last_seen_at, created_at
        FROM mikan_download_events
        ORDER BY created_at, id
        """
    ).fetchall()
    recent_by_key: dict[str, tuple[int, float, int]] = {}
    for event_id, item_key, event, detail, detail_json, occurrence_count, last_seen_at, created_at in rows:
        compact_detail = _compact_mikan_event_detail(str(detail or ""))
        payload = _json_object(detail_json)
        if not payload:
            payload = _legacy_mikan_event_payload(str(event or ""), compact_detail)
        event_key = _mikan_event_key(str(item_key or ""), str(event or ""), payload)
        timestamp = float(last_seen_at or created_at or 0)
        previous = recent_by_key.get(event_key)
        if previous is not None and timestamp - previous[1] <= 600:
            keeper_id, _, keeper_count = previous
            combined_count = keeper_count + max(1, int(occurrence_count or 1))
            conn.execute(
                """
                UPDATE mikan_download_events
                SET detail=?, detail_json=?, occurrence_count=?,
                    last_seen_at=?, created_at=?
                WHERE id=?
                """,
                (
                    compact_detail,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    combined_count,
                    timestamp,
                    timestamp,
                    keeper_id,
                ),
            )
            conn.execute("DELETE FROM mikan_download_events WHERE id=?", (int(event_id),))
            recent_by_key[event_key] = (keeper_id, timestamp, combined_count)
            continue
        conn.execute(
            """
            UPDATE mikan_download_events
            SET detail=?, detail_json=?, event_key=?, occurrence_count=?, last_seen_at=?
            WHERE id=?
            """,
            (
                compact_detail,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                event_key,
                max(1, int(occurrence_count or 1)),
                timestamp,
                int(event_id),
            ),
        )
        recent_by_key[event_key] = (int(event_id), timestamp, max(1, int(occurrence_count or 1)))
    now = time.time()
    conn.execute(
        """
        INSERT INTO mikan_state_meta(key, value, updated_at)
        VALUES('download_events_compacted_v2', '1', ?)
        ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at
        """,
        (now,),
    )


def _ensure_mikan_extract_job_columns(conn: sqlite3.Connection) -> None:
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(mikan_extract_jobs)").fetchall()}
    additions = {
        "target_path": "ALTER TABLE mikan_extract_jobs ADD COLUMN target_path TEXT NOT NULL DEFAULT ''",
        "target_confidence": "ALTER TABLE mikan_extract_jobs ADD COLUMN target_confidence REAL NOT NULL DEFAULT 0",
        "match_version": "ALTER TABLE mikan_extract_jobs ADD COLUMN match_version TEXT NOT NULL DEFAULT ''",
        "progress_current": "ALTER TABLE mikan_extract_jobs ADD COLUMN progress_current INTEGER NOT NULL DEFAULT 0",
        "progress_total": "ALTER TABLE mikan_extract_jobs ADD COLUMN progress_total INTEGER NOT NULL DEFAULT 0",
        "current_file_timestamp": "ALTER TABLE mikan_extract_jobs ADD COLUMN current_file_timestamp REAL NOT NULL DEFAULT 0",
        "current_file_time_kind": "ALTER TABLE mikan_extract_jobs ADD COLUMN current_file_time_kind TEXT NOT NULL DEFAULT ''",
        "current_file_size": "ALTER TABLE mikan_extract_jobs ADD COLUMN current_file_size INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in additions.items():
        if column not in existing:
            conn.execute(statement)


def _migrate_mikan_extract_job_terminal_statuses(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT job_key, status, result_json, last_error
        FROM mikan_extract_jobs
        WHERE status IN ('failed', 'terminal_failed')
        """
    ).fetchall()
    failed_to_terminal: list[str] = []
    terminal_to_failed: list[str] = []
    failed_to_replaced: list[str] = []
    terminal_to_replaced: list[str] = []
    for job_key, status, result_json, last_error in rows:
        result = _json_object(result_json)
        retryable = bool(result.get("retryable"))
        allows_replacement = _mikan_extract_job_failure_allows_replacement(result, str(last_error or ""))
        if status == "failed" and allows_replacement and not retryable:
            failed_to_replaced.append(str(job_key))
        elif status == "terminal_failed" and allows_replacement and not retryable:
            terminal_to_replaced.append(str(job_key))
        elif status == "failed" and not retryable and not allows_replacement:
            failed_to_terminal.append(str(job_key))
        elif status == "terminal_failed" and (allows_replacement or retryable):
            terminal_to_failed.append(str(job_key))

    if failed_to_replaced:
        conn.executemany(
            "UPDATE mikan_extract_jobs SET status = 'replaced' WHERE job_key = ?",
            [(job_key,) for job_key in failed_to_replaced],
        )
    if terminal_to_replaced:
        conn.executemany(
            "UPDATE mikan_extract_jobs SET status = 'replaced' WHERE job_key = ?",
            [(job_key,) for job_key in terminal_to_replaced],
        )
    if failed_to_terminal:
        conn.executemany(
            "UPDATE mikan_extract_jobs SET status = 'terminal_failed' WHERE job_key = ?",
            [(job_key,) for job_key in failed_to_terminal],
        )
    if terminal_to_failed:
        conn.executemany(
            "UPDATE mikan_extract_jobs SET status = 'failed' WHERE job_key = ?",
            [(job_key,) for job_key in terminal_to_failed],
        )


def _mikan_extract_job_failure_allows_replacement(result: dict[str, Any], last_error: str = "") -> bool:
    reason = str(result.get("failure_reason") or result.get("failure_bucket") or "")
    if reason and _mikan_extract_failure_allows_replacement(reason):
        return True
    detail = str(result.get("failure_detail") or last_error or "").lower()
    if not detail:
        return False
    return any(
        marker in detail
        for marker in (
            "mapped completed torrent path does not exist",
            "mapped source video does not exist",
            "qBittorrent content path cannot be mapped".lower(),
            "no video files found in mapped completed torrent path",
        )
    )


def _mikan_state_row(key: str, entry: dict[str, Any], now: float) -> dict[str, Any]:
    episodes = _pending_entry_episode_numbers(entry)
    failed_urls = _pending_failed_urls(entry)
    status = _pending_state_status(entry, now)
    title = str(
        entry.get("title")
        or entry.get("deferred_title")
        or entry.get("last_failed_title")
        or entry.get("last_completed_title")
        or ""
    )
    torrent_url = str(
        entry.get("torrent_url")
        or entry.get("deferred_torrent_url")
        or entry.get("last_failed_torrent_url")
        or entry.get("last_completed_torrent_url")
        or (failed_urls[0] if failed_urls else "")
        or ""
    )
    return {
        "key": key,
        "bangumi_id": _safe_int(entry.get("bangumi_id")),
        "episode": _safe_int(entry.get("episode")),
        "episodes_json": json.dumps(sorted(episodes), ensure_ascii=False),
        "status": status,
        "title": title,
        "torrent_url": torrent_url,
        "failed_urls_json": json.dumps(failed_urls, ensure_ascii=False),
        "queued_at": _pending_timestamp(entry.get("queued_at")),
        "updated_at": _pending_state_updated_at(entry, now),
        "last_progress": _safe_float(entry.get("last_progress")),
        "last_downloaded": _safe_int(entry.get("last_downloaded")),
        "last_dlspeed": _safe_int(entry.get("last_dlspeed")) or 0,
        "last_qbit_state": str(entry.get("last_qbit_state") or ""),
        "last_qbit_hash": str(entry.get("last_qbit_hash") or ""),
        "last_qbit_name": str(entry.get("last_qbit_name") or ""),
        "last_qbit_sync_at": _pending_timestamp(entry.get("last_qbit_sync_at")),
        "completed_at": _pending_timestamp(entry.get("completed_at")),
        "last_extracted_at": _pending_timestamp(entry.get("last_extracted_at") or entry.get("completed_at")),
        "last_extracted_count": _safe_int(entry.get("last_extracted_count")) or 0,
        "total_extracted_count": _safe_int(entry.get("total_extracted_count")) or 0,
        "failed_count": len(failed_urls),
        "last_failure_reason": str(entry.get("last_failure_reason") or ""),
        "last_extract_failure_reason": str(entry.get("last_extract_failure_reason") or ""),
        "last_extract_failure_detail": str(entry.get("last_extract_failure_detail") or ""),
        "no_candidate_until": _safe_float(entry.get("no_candidate_until")),
        "timed_out_at": _pending_timestamp(entry.get("timed_out_at")),
        "next_action": _pending_next_action(entry, status, now),
        "raw_json": json.dumps(entry, ensure_ascii=False, sort_keys=True),
    }


def _pending_state_status(entry: dict[str, Any], now: float) -> str:
    if str(entry.get("candidate_review_reason") or ""):
        return "review"
    if str(entry.get("last_extract_failure_reason") or "") == "target_ambiguity":
        return "review"
    if entry.get("last_failure_reason") == "extract_failed" or entry.get("last_extract_failed_at"):
        return "extract_failed"
    if (
        entry.get("completed_at")
        or (_safe_int(entry.get("last_extracted_count")) or 0) > 0
        or (_safe_int(entry.get("total_extracted_count")) or 0) > 0
    ):
        return "completed"
    if entry.get("torrent_url") and entry.get("queued_at"):
        progress = _safe_float(entry.get("last_progress")) or 0.0
        if progress >= 1.0:
            if entry.get("last_extract_deferred_reason") == "target_video_not_found" or entry.get("last_extract_deferred_at"):
                return "target_missing"
            return "completed_waiting_extract"
        if progress > 0 or (_safe_int(entry.get("last_downloaded")) or 0) > 0:
            return "downloading"
        return "queued"
    if entry.get("deferred_torrent_url") and entry.get("deferred_at"):
        return "deferred"
    retry_until = _safe_float(entry.get("no_candidate_until"))
    if retry_until is not None and retry_until > now:
        return "no_candidate_retry"
    failed_urls = entry.get("failed_urls")
    if isinstance(failed_urls, list) and failed_urls:
        return "failed_candidate"
    return "unknown"


def _pending_target_missing_recent(entry: dict[str, Any], now: float, retry_seconds: float) -> bool:
    reason = str(entry.get("last_extract_deferred_reason") or "")
    if reason != "target_video_not_found":
        return False
    deferred_at = _pending_timestamp(entry.get("last_extract_deferred_at"))
    return deferred_at > 0 and now - deferred_at < retry_seconds


def _pending_next_action(entry: dict[str, Any], status: str, now: float) -> str:
    if status == "downloading":
        if str(entry.get("last_qbit_state") or "") == "stalledDL" and (_safe_int(entry.get("last_dlspeed")) or 0) <= 0:
            return "replace_when_stall_timeout"
        return "wait_qbit_progress"
    if status == "queued":
        return "wait_qbit_start"
    if status == "completed_waiting_extract":
        return "extract_subtitles"
    if status == "target_missing":
        return "wait_target_video"
    if status == "extract_failed":
        return "find_replacement"
    if status == "review":
        if str(entry.get("candidate_review_reason") or ""):
            return "review_release_identity"
        return "resolve_target_ambiguity"
    if status == "failed_candidate":
        return "find_replacement"
    if status == "no_candidate_retry":
        retry_until = _safe_float(entry.get("no_candidate_until")) or 0
        return "retry_candidate_search" if retry_until <= now else "wait_retry_window"
    if status == "deferred":
        return "queue_when_qbit_available"
    if status == "completed":
        return "done"
    return "inspect_state"


def _pending_state_updated_at(entry: dict[str, Any], now: float) -> float:
    values = [
        _pending_timestamp(entry.get("last_progress_at")),
        _pending_timestamp(entry.get("last_qbit_sync_at")),
        _pending_timestamp(entry.get("last_extracted_at")),
        _pending_timestamp(entry.get("last_extract_failed_at")),
        _pending_timestamp(entry.get("last_extract_deferred_at")),
        _pending_timestamp(entry.get("completed_at")),
        _pending_timestamp(entry.get("queued_at")),
        _pending_timestamp(entry.get("deferred_at")),
        _pending_timestamp(entry.get("no_candidate_at")),
        _pending_timestamp(entry.get("timed_out_at")),
        _safe_float(entry.get("no_candidate_until")) or 0.0,
    ]
    return max(values) or now


def _pending_timestamp(value: object) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return _parse_pending_time(value).timestamp()


def _safe_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mikan_state_event_needed(previous: dict[str, Any] | None, row: dict[str, Any]) -> bool:
    return bool(_mikan_state_event_name(previous, row))


def _mikan_state_event_name(previous: dict[str, Any] | None, row: dict[str, Any]) -> str:
    if previous is None:
        return "created" if str(row.get("status") or "") not in {"", "unknown", "completed"} else ""
    previous_failure = str(
        previous.get("last_extract_failure_reason")
        or previous.get("last_failure_reason")
        or ""
    )
    current_failure = str(
        row.get("last_extract_failure_reason")
        or row.get("last_failure_reason")
        or ""
    )
    if (
        int(previous.get("failed_count") or 0) != int(row.get("failed_count") or 0)
        or (current_failure and current_failure != previous_failure)
    ):
        return "failure_recorded"
    if previous["status"] != row["status"]:
        return "status_changed"
    if (
        str(row.get("status") or "") not in {"completed", "success"}
        and _mikan_source_identity(previous.get("torrent_url")) != _mikan_source_identity(row.get("torrent_url"))
    ):
        return "source_changed"
    return ""


def _mikan_state_event_detail(previous: dict[str, Any] | None, row: dict[str, Any]) -> str:
    payload = _mikan_state_event_payload(previous, row)
    parts = [f"status={payload.get('previous_status', '-') }->{payload.get('status', '-')}"]
    if payload.get("reason"):
        parts.append(f"reason={payload['reason']}")
    if payload.get("source"):
        parts.append(f"source={payload['source']}")
    if payload.get("failure_count"):
        parts.append(f"failed={payload['failure_count']}")
    return " ".join(parts)


def _mikan_state_event_payload(
    previous: dict[str, Any] | None,
    row: dict[str, Any],
) -> dict[str, Any]:
    event = _mikan_state_event_name(previous, row)
    payload: dict[str, Any] = {
        "code": event,
        "status": str(row.get("status") or ""),
    }
    if previous is not None:
        payload["previous_status"] = str(previous.get("status") or "")
    reason = str(row.get("last_extract_failure_reason") or row.get("last_failure_reason") or "")
    if reason:
        payload["reason"] = reason[:120]
    source = _mikan_source_identity(row.get("torrent_url"))
    previous_source = _mikan_source_identity((previous or {}).get("torrent_url"))
    if source:
        payload["source"] = source
    if previous_source and previous_source != source:
        payload["previous_source"] = previous_source
    failure_count = int(row.get("failed_count") or 0)
    if failure_count:
        payload["failure_count"] = failure_count
    return payload


def _record_mikan_state_event(
    conn: sqlite3.Connection,
    previous: dict[str, Any] | None,
    row: dict[str, Any],
    *,
    now: float,
) -> None:
    event = _mikan_state_event_name(previous, row)
    if not event:
        return
    payload = _mikan_state_event_payload(previous, row)
    event_key = _mikan_event_key(str(row.get("key") or ""), event, payload)
    detail = _mikan_state_event_detail(previous, row)
    existing = conn.execute(
        """
        SELECT id, occurrence_count
        FROM mikan_download_events
        WHERE event_key=? AND last_seen_at>=?
        ORDER BY last_seen_at DESC, id DESC
        LIMIT 1
        """,
        (event_key, now - 600),
    ).fetchone()
    compact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if existing is not None:
        conn.execute(
            """
            UPDATE mikan_download_events
            SET detail=?, detail_json=?, occurrence_count=?, last_seen_at=?, created_at=?
            WHERE id=?
            """,
            (detail, compact_json, int(existing[1] or 1) + 1, now, now, int(existing[0])),
        )
        return
    conn.execute(
        """
        INSERT INTO mikan_download_events(
            key, bangumi_id, episode, event, detail, detail_json,
            event_key, occurrence_count, last_seen_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            row["key"],
            row["bangumi_id"],
            row["episode"],
            event,
            detail,
            compact_json,
            event_key,
            now,
            now,
        ),
    )


def _mikan_source_identity(value: object) -> str:
    source = str(value or "").strip()
    if not source:
        return ""
    parsed = urlparse(source)
    host = str(parsed.hostname or parsed.scheme or "source").casefold()
    if host == "magnet":
        host = "magnet"
    fingerprint = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{host}#{fingerprint}"


def _mikan_event_key(item_key: str, event: str, payload: dict[str, Any]) -> str:
    identity = {
        "item": str(item_key),
        "event": str(event),
        "status": str(payload.get("status") or ""),
        "reason": str(payload.get("reason") or ""),
        "source": str(payload.get("source") or ""),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _compact_mikan_event_detail(detail: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = "-" if raw.endswith("-") else ""
        value = raw[:-1] if trailing else raw
        return _mikan_source_identity(value) + trailing

    return re.sub(r"(?:https?://|magnet:\?)[^\s>]+", replace, str(detail or ""))[:500]


def _legacy_mikan_event_payload(event: str, detail: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": str(event or "updated")}
    status = re.search(r"\bstatus=([^\s]+)", detail)
    if status:
        transition = status.group(1).split("->")
        if len(transition) > 1:
            payload["previous_status"] = transition[0]
        payload["status"] = transition[-1]
    reason = re.search(r"\breason=([^\s]+)", detail)
    if reason:
        payload["reason"] = reason.group(1)[:120]
    return payload


def _backup_mikan_state_files(seen_path: Path, pending_path: Path) -> dict[str, str]:
    timestamp = _utc_now().strftime("%Y%m%d-%H%M%S")
    backups: dict[str, str] = {}
    for key, path in (("seen", seen_path), ("pending", pending_path)):
        if not path.exists():
            continue
        backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
        shutil.copy2(path, backup_path)
        backups[key] = str(backup_path)
    return backups


def _pending_items(pending: dict[str, Any]) -> dict[str, Any]:
    items = pending.setdefault("items", {})
    if not isinstance(items, dict):
        pending["items"] = {}
        return pending["items"]
    return items


def _seen_payload(release: MikanRelease) -> dict[str, Any]:
    episodes = release_episode_numbers(release)
    return {
        "bangumi_id": release.bangumi_id,
        "title": release.title,
        "episode": release.episode,
        "episodes": list(episodes),
        "torrent_url": release.torrent_url,
        "pub_date": release.pub_date.isoformat() if release.pub_date else None,
        "source": release.source,
        "source_page": release.link,
        "info_hash": release.info_hash,
        "seeders": release.seeders,
    }


def _seen_payload_from_pending_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "bangumi_id": entry.get("bangumi_id"),
        "title": entry.get("title") or entry.get("deferred_title"),
        "episode": entry.get("episode"),
        "episodes": entry.get("episodes") or [entry.get("episode")],
        "torrent_url": entry.get("torrent_url") or entry.get("deferred_torrent_url"),
        "pub_date": entry.get("pub_date") or entry.get("deferred_pub_date"),
        "source": entry.get("source") or entry.get("deferred_source") or "mikan",
        "source_page": entry.get("source_page") or entry.get("deferred_source_page"),
        "info_hash": entry.get("info_hash") or entry.get("deferred_info_hash"),
        "seeders": entry.get("seeders") if entry.get("seeders") is not None else entry.get("deferred_seeders"),
    }


def _review_source_time_fields(
    torrent: QBitTorrent,
    pending_entries: list[dict[str, Any]],
) -> dict[str, float]:
    """Return trustworthy source and qBittorrent times for review UI.

    qbit-recovered entries synthesize ``pub_date`` from qBittorrent's
    completion time.  That timestamp is useful for recovery ordering but is
    not a torrent publication date, so it must never be presented as one.
    """

    publication_candidates = [
        _pending_entry_source_publication(entry)
        for entry in pending_entries
    ]
    published_at, published_precision = max(
        publication_candidates,
        key=lambda item: (item[0], item[1] == "time"),
        default=(0.0, ""),
    )

    return {
        "source_published_at": float(published_at),
        "source_published_precision": published_precision,
        "torrent_created_at": float(_coerce_int(torrent.creation_date) or 0),
        "torrent_added_at": float(_coerce_int(torrent.added_on) or 0),
        "torrent_completed_at": float(_coerce_int(torrent.completion_on) or 0),
    }


def _torrent_request_payload(torrent: QBitTorrent) -> dict[str, Any]:
    return {
        "hash": torrent.hash,
        "name": torrent.name,
        "progress": torrent.progress,
        "state": torrent.state,
        "dlspeed": torrent.dlspeed,
        "downloaded": torrent.downloaded,
        "added_on": torrent.added_on,
        "content_path": torrent.content_path,
        "save_path": torrent.save_path,
        "category": torrent.category,
        "tags": torrent.tags,
        "eta": torrent.eta,
        "last_activity": torrent.last_activity,
        "completion_on": torrent.completion_on,
        "creation_date": torrent.creation_date,
    }


def _torrent_from_request_payload(value: object) -> QBitTorrent | None:
    if not isinstance(value, dict):
        return None
    return QBitTorrent(
        hash=str(value.get("hash") or ""),
        name=str(value.get("name") or ""),
        progress=float(value.get("progress") or 0.0),
        state=str(value.get("state") or ""),
        dlspeed=int(value.get("dlspeed") or 0),
        downloaded=int(value.get("downloaded") or 0),
        added_on=_coerce_int(value.get("added_on")),
        content_path=str(value["content_path"]) if value.get("content_path") else None,
        save_path=str(value["save_path"]) if value.get("save_path") else None,
        category=str(value["category"]) if value.get("category") else None,
        tags=str(value["tags"]) if value.get("tags") else None,
        eta=_coerce_int(value.get("eta")),
        last_activity=_coerce_int(value.get("last_activity")),
        completion_on=_coerce_int(value.get("completion_on")),
        creation_date=_coerce_int(value.get("creation_date")),
    )


def _extract_result_request_payload(result: MikanExtractResult) -> dict[str, Any]:
    return {
        "extracted_count": result.extracted_count,
        "failure_reason": result.failure_reason,
        "failure_bucket": _mikan_extract_failure_bucket(result.failure_reason),
        "failure_detail": result.failure_detail,
        "failure_context": _public_mikan_failure_context(result.failure_context),
        "subtitle_diagnostics": result.subtitle_diagnostics[:10],
        "retryable": result.retryable,
        "defer_seconds": result.defer_seconds,
    }


def _extract_result_from_request_payload(value: object) -> MikanExtractResult:
    if not isinstance(value, dict):
        return MikanExtractResult(0, failure_reason="extract_exception", failure_detail="Missing deferred extract result")
    diagnostics = value.get("subtitle_diagnostics")
    failure_context = value.get("failure_context")
    return MikanExtractResult(
        extracted_count=int(value.get("extracted_count") or 0),
        failure_reason=str(value.get("failure_reason") or ""),
        failure_detail=str(value.get("failure_detail") or ""),
        subtitle_diagnostics=diagnostics if isinstance(diagnostics, list) else [],
        failure_context=failure_context if isinstance(failure_context, dict) else {},
        retryable=bool(value.get("retryable")),
        defer_seconds=float(value.get("defer_seconds") or 0),
    )


def _release_candidates_by_episode(
    releases: list[MikanRelease],
    missing_episodes: set[int],
    config: AppConfig,
) -> dict[int, list[MikanRelease]]:
    return select_preferred_release_candidates_for_episodes(
        releases,
        episodes=missing_episodes,
        prefer_keywords=config.mikan_prefer_keywords,
        reject_keywords=config.mikan_reject_keywords,
        require_extractable=config.mikan_require_extractable_subtitle,
    )


def _fallback_search_is_conclusive(result: object | None) -> bool:
    if result is None:
        return True
    return bool(getattr(result, "conclusive", True))


def _fallback_search_deferred_reason(result: object | None) -> str:
    if result is None:
        return "unknown"
    return str(getattr(result, "deferred_reason", "") or "inconclusive_source_search")


def _candidate_search_deferred_reason(
    result: object | None,
    *,
    primary_lookup_succeeded: bool,
) -> str:
    if not primary_lookup_succeeded:
        fallback_reason = _fallback_search_deferred_reason(result)
        return f"primary_discovery_unavailable+{fallback_reason}"
    return _fallback_search_deferred_reason(result)


@dataclass(frozen=True)
class _ReleaseIdentityAssessment:
    release: MikanRelease
    safe: bool
    identity_key: str
    reason: str = ""


def _mapping_release_identity_context(
    mappings: list[dict[str, object]],
) -> tuple[set[str], set[int], set[int]]:
    identities: set[str] = set()
    expected_seasons: set[int] = set()
    alias_seasons: set[int] = set()
    for mapping in mappings:
        raw_values: list[str] = []
        for key in ("title", "bangumi_title", "name"):
            value = str(mapping.get(key) or "").strip()
            if value:
                raw_values.append(value)
        raw_path = str(mapping.get("path") or "").strip()
        if raw_path:
            path = Path(raw_path)
            path_name = path.name
            season_match = re.fullmatch(r"Season\s+0*(\d{1,2})", path_name, re.IGNORECASE)
            if season_match:
                expected_seasons.add(int(season_match.group(1)))
                if path.parent != path:
                    raw_values.append(path.parent.name)
            elif path_name.casefold() == "specials":
                expected_seasons.add(0)
                if path.parent != path:
                    raw_values.append(path.parent.name)
            else:
                raw_values.append(path_name)
        matches = mapping.get("match")
        if isinstance(matches, list):
            raw_values.extend(str(value).strip() for value in matches if str(value).strip())
        for key in ("season", "season_number"):
            value = _coerce_int(mapping.get(key))
            if value is not None and 0 <= value <= 99:
                expected_seasons.add(value)
        for value in raw_values:
            identity = normalize_match_text(release_series_identity(value))
            if len(identity) >= 3:
                identities.add(identity)
            season = release_season_number(value)
            if season is not None:
                alias_seasons.add(season)
    return identities, expected_seasons, alias_seasons


def _assess_release_identity(
    release: MikanRelease,
    mappings: list[dict[str, object]],
) -> _ReleaseIdentityAssessment:
    source = str(release.source or "").split(":", 1)[0].casefold()
    identities, expected_seasons, alias_seasons = _mapping_release_identity_context(mappings)
    release_season = release.season_number
    if len(expected_seasons) > 1:
        return _ReleaseIdentityAssessment(
            release,
            False,
            "",
            "mapping_has_multiple_seasons",
        )
    expected_season = next(iter(expected_seasons), None)
    if (any(mapping.get("identity_source") == "cached_season_nfo" for mapping in mappings)
            and release_season is None):
        return _ReleaseIdentityAssessment(release, False, "", "scoped_source_season_unverified")
    if (
        release_season is not None
        and expected_season is not None
        and release_season != expected_season
    ):
        return _ReleaseIdentityAssessment(
            release,
            False,
            "",
            f"season_mismatch:{release_season}!={expected_season}",
        )

    effective_season = expected_season if expected_season is not None else release_season
    identity_key = f"{int(release.bangumi_id)}:{effective_season if effective_season is not None else 'unknown'}"
    if source == "mikan":
        return _ReleaseIdentityAssessment(release, True, identity_key)

    identity = normalize_match_text(release.series_identity)
    if not identity or not identities:
        return _ReleaseIdentityAssessment(
            release,
            False,
            "",
            "missing_verified_series_identity",
        )
    if identity not in identities:
        related = any(
            candidate in identity or identity in candidate
            for candidate in identities
        )
        return _ReleaseIdentityAssessment(
            release,
            False,
            "",
            "series_identity_has_sequel_suffix" if related else "series_identity_not_verified",
        )
    if (
        release_season is not None
        and expected_season is None
        and release_season not in alias_seasons
    ):
        return _ReleaseIdentityAssessment(
            release,
            False,
            "",
            f"unverified_explicit_season:{release_season}",
        )
    return _ReleaseIdentityAssessment(release, True, identity_key)


def _safe_release_choice(
    candidates: list[MikanRelease],
    mappings: list[dict[str, object]] | None,
) -> tuple[MikanRelease | None, str]:
    if not candidates:
        return None, ""
    if mappings is None:
        return candidates[0], ""

    assessed = [_assess_release_identity(candidate, mappings) for candidate in candidates]
    unsafe = [item for item in assessed if not item.safe]
    if unsafe:
        reasons = ",".join(sorted({item.reason for item in unsafe if item.reason}))
        return None, f"ambiguous_release_identity:{reasons or 'unverified'}"
    identity_keys = {item.identity_key for item in assessed}
    if len(identity_keys) != 1:
        return None, "ambiguous_release_identity:multiple_identity_groups"
    return assessed[0].release, ""


def _choose_release_for_episode(
    bangumi_id: int,
    episode: int | None,
    candidates: list[MikanRelease],
    seen: dict[str, Any],
    pending: dict[str, Any],
    *,
    mappings: list[dict[str, object]] | None = None,
    ambiguity_reasons: list[str] | None = None,
) -> MikanRelease | None:
    if episode is None or _has_active_pending(bangumi_id, episode, pending) or _has_deferred_release(bangumi_id, episode, pending):
        return None

    entry = _pending_entry(bangumi_id, episode, pending)
    if _pending_is_terminal_success(entry):
        return None
    failed_urls = set(_pending_failed_urls(entry))
    failed_info_hashes = _pending_failed_info_hashes(entry)
    retryable_seen_urls = _pending_retryable_source_urls(entry)
    retryable_seen_hashes = _pending_retryable_info_hashes(entry)
    seen_hashes = _seen_info_hashes(seen)
    selectable: list[MikanRelease] = []
    for candidate in candidates:
        if candidate.torrent_url in failed_urls:
            continue
        if candidate.info_hash and candidate.info_hash in failed_info_hashes:
            continue
        if candidate.torrent_url in seen and candidate.torrent_url not in retryable_seen_urls:
            continue
        if candidate.info_hash and candidate.info_hash in seen_hashes and candidate.info_hash not in retryable_seen_hashes:
            continue
        selectable.append(candidate)
    selected, ambiguity_reason = _safe_release_choice(selectable, mappings)
    if ambiguity_reason and ambiguity_reasons is not None:
        ambiguity_reasons.append(ambiguity_reason)
    return selected


def _release_is_seen(release: MikanRelease, seen: dict[str, Any]) -> bool:
    if release.torrent_url in seen:
        return True
    return bool(release.info_hash and release.info_hash in _seen_info_hashes(seen))


def _seen_info_hashes(seen: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for url, payload in seen.items():
        info_hash = payload.get("info_hash") if isinstance(payload, dict) else None
        normalized = str(info_hash or extract_torrent_info_hash(str(url)) or "").casefold()
        if normalized:
            hashes.add(normalized)
    return hashes


def _release_seen_is_retryable(release: MikanRelease, pending: dict[str, Any]) -> bool:
    for episode in release_episode_numbers(release):
        entry = _pending_entry(release.bangumi_id, episode, pending)
        if _pending_is_terminal_success(entry):
            continue
        if release.torrent_url in _pending_retryable_source_urls(entry):
            return True
        if release.info_hash and release.info_hash in _pending_retryable_info_hashes(entry):
            return True
    return False


def _mark_pending(
    pending: dict[str, Any],
    release: MikanRelease,
    *,
    allow_completed_reopen: bool = False,
) -> None:
    episodes = release_episode_numbers(release)
    if not episodes:
        return
    queued_at = _utc_now().isoformat()
    for episode in episodes:
        entry = _pending_entry(release.bangumi_id, episode, pending)
        if _pending_is_terminal_success(entry) and not allow_completed_reopen:
            continue
        if allow_completed_reopen and _pending_is_terminal_success(entry):
            history = entry.setdefault("completed_history", [])
            if isinstance(history, list):
                history.append(
                    {
                        "completed_at": entry.get("completed_at"),
                        "last_extracted_at": entry.get("last_extracted_at"),
                        "last_extracted_count": entry.get("last_extracted_count"),
                        "total_extracted_count": entry.get("total_extracted_count"),
                        "reopened_at": queued_at,
                    }
                )
                del history[:-10]
            for completed_key in (
                "completed_at",
                "last_extracted_at",
                "last_extracted_count",
                "total_extracted_count",
            ):
                entry.pop(completed_key, None)
        _clear_no_candidate_retry(entry)
        _clear_candidate_review(entry)
        for key in (
            "last_progress_at",
            "last_progress",
            "last_downloaded",
            "last_qbit_sync_at",
            "last_dlspeed",
            "last_qbit_state",
            "last_qbit_hash",
            "last_qbit_name",
            "last_extract_failed_at",
            "last_extract_failure_reason",
            "last_extract_failure_detail",
            "last_extract_deferred_at",
            "last_extract_deferred_reason",
            "last_extract_deferred_detail",
            "last_extract_context",
            "last_subtitle_diagnostics",
            "last_failure_reason",
            "timed_out_at",
        ):
            entry.pop(key, None)
        entry.update(
            {
                "bangumi_id": release.bangumi_id,
                "episode": episode,
                "episodes": list(episodes),
                "torrent_url": release.torrent_url,
                "title": release.title,
                "queued_at": queued_at,
                "pub_date": release.pub_date.isoformat() if release.pub_date else None,
                "source": release.source,
                "source_page": release.link,
                "info_hash": release.info_hash,
                "seeders": release.seeders,
            }
        )


def _mark_deferred(pending: dict[str, Any], release: MikanRelease, *, reason: str) -> None:
    episodes = release_episode_numbers(release)
    if not episodes:
        return
    deferred_at = _utc_now().isoformat()
    for episode in episodes:
        entry = _pending_entry(release.bangumi_id, episode, pending)
        if _pending_is_terminal_success(entry):
            continue
        _clear_no_candidate_retry(entry)
        _clear_candidate_review(entry)
        entry.update(
            {
                "bangumi_id": release.bangumi_id,
                "episode": episode,
                "episodes": list(episodes),
                "deferred_torrent_url": release.torrent_url,
                "deferred_title": release.title,
                "deferred_at": deferred_at,
                "deferred_reason": reason,
                "deferred_pub_date": release.pub_date.isoformat() if release.pub_date else None,
                "deferred_source": release.source,
                "deferred_source_page": release.link,
                "deferred_info_hash": release.info_hash,
                "deferred_seeders": release.seeders,
            }
        )


def _mark_candidate_review(
    pending: dict[str, Any],
    bangumi_id: int,
    episode: int,
    reasons: list[str],
) -> bool:
    entry = _pending_entry(bangumi_id, episode, pending)
    if (
        _pending_is_terminal_success(entry)
        or _pending_has_active_release(entry)
        or _pending_has_deferred_release(entry)
    ):
        return False
    normalized_reasons = sorted({
        str(reason or "").strip()
        for reason in reasons
        if str(reason or "").strip()
    })
    _clear_no_candidate_retry(entry)
    new_reason = "ambiguous_release_identity"
    changed = bool(
        entry.get("candidate_review_reason") != new_reason
        or entry.get("candidate_review_details") != normalized_reasons
    )
    entry.update(
        {
            "bangumi_id": int(bangumi_id),
            "episode": int(episode),
            "candidate_review_at": _utc_now().isoformat(),
            "candidate_review_reason": new_reason,
            "candidate_review_details": normalized_reasons[:20],
        }
    )
    return changed


def _clear_candidate_review(entry: dict[str, Any]) -> None:
    for key in (
        "candidate_review_at",
        "candidate_review_reason",
        "candidate_review_details",
    ):
        entry.pop(key, None)


def _clear_no_candidate_retry(entry: dict[str, Any]) -> None:
    for key in (
        "no_candidate_at",
        "no_candidate_until",
        "no_candidate_retry_count",
        "no_candidate_retry_seconds",
    ):
        entry.pop(key, None)


def _mark_no_candidate_retry(
    pending: dict[str, Any],
    bangumi_id: int,
    episode: int,
    retry_seconds: int,
    max_retry_seconds: int = 86400,
) -> int:
    entry = _pending_entry(bangumi_id, episode, pending)
    _clear_candidate_review(entry)
    now = _utc_now()
    multipliers = (1, 6, 36, 144)
    previous_count = max(0, _safe_int(entry.get("no_candidate_retry_count")) or 0)
    retry_count = min(previous_count + 1, len(multipliers))
    base_seconds = max(1, int(retry_seconds or 1))
    cap_seconds = max(base_seconds, int(max_retry_seconds or base_seconds))
    delay_seconds = min(cap_seconds, base_seconds * multipliers[retry_count - 1])
    entry["no_candidate_at"] = now.isoformat()
    entry["no_candidate_until"] = now.timestamp() + delay_seconds
    entry["no_candidate_retry_count"] = retry_count
    entry["no_candidate_retry_seconds"] = delay_seconds
    return delay_seconds


def _format_no_candidate_retry_delays(retry_delays: dict[int, int] | None) -> str:
    if retry_delays is None:
        return "state-update-skipped"
    unique_delays = sorted({max(0, int(value)) for value in retry_delays.values()})
    if not unique_delays:
        return "none"
    if len(unique_delays) == 1:
        return f"{unique_delays[0]}s"
    return f"{unique_delays[0]}s-{unique_delays[-1]}s"


def _no_candidate_retry_active(
    pending: dict[str, Any],
    bangumi_id: int,
    episode: int,
    now: datetime,
) -> bool:
    entry = _pending_items(pending).get(_pending_key(bangumi_id, episode))
    if not isinstance(entry, dict):
        return False
    if _pending_failure_allows_source_retry(entry):
        return False
    retry_until = entry.get("no_candidate_until")
    try:
        retry_until_ts = float(retry_until)
    except (TypeError, ValueError):
        return False
    return now.timestamp() < retry_until_ts


def _pending_entry(bangumi_id: int, episode: int, pending: dict[str, Any]) -> dict[str, Any]:
    items = _pending_items(pending)
    key = _pending_key(bangumi_id, episode)
    entry = items.setdefault(key, {"bangumi_id": bangumi_id, "episode": episode, "failed_urls": []})
    if not isinstance(entry, dict):
        entry = {"bangumi_id": bangumi_id, "episode": episode, "failed_urls": []}
        items[key] = entry
    if not isinstance(entry.get("failed_urls"), list):
        entry["failed_urls"] = []
    return entry


def _pending_key(bangumi_id: int, episode: int) -> str:
    return f"{bangumi_id}:{episode}"


def _source_tag(source: Any) -> str:
    value = str(source or "").split(":", 1)[0].strip().casefold()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def _queue_tags(base_tags: list[str], source: Any = None) -> list[str]:
    tags = [str(tag).strip() for tag in base_tags if str(tag).strip()]
    source_tag = _source_tag(source)
    if source_tag:
        tags.append(source_tag)
    return list(dict.fromkeys(tags))


def _unique_releases_by_torrent_url(releases: Any) -> list[MikanRelease]:
    result: list[MikanRelease] = []
    seen_keys: set[str] = set()
    for release in releases:
        key = release.info_hash or release.torrent_url.casefold()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(release)
    return result


def _has_active_pending(bangumi_id: int, episode: int, pending: dict[str, Any]) -> bool:
    return _pending_has_active_release(_pending_entry(bangumi_id, episode, pending))


def _has_deferred_release(bangumi_id: int, episode: int, pending: dict[str, Any]) -> bool:
    return _pending_has_deferred_release(_pending_entry(bangumi_id, episode, pending))


def _pending_is_terminal_success(entry: dict[str, Any]) -> bool:
    extracted_count = max(
        _safe_int(entry.get("last_extracted_count")) or 0,
        _safe_int(entry.get("total_extracted_count")) or 0,
    )
    completed_at = _pending_timestamp(entry.get("completed_at") or entry.get("last_extracted_at"))
    return bool(extracted_count > 0 and completed_at > 0)


def _pending_has_active_release_fields(entry: dict[str, Any]) -> bool:
    return bool(entry.get("torrent_url") and entry.get("queued_at"))


def _pending_has_active_release(entry: dict[str, Any]) -> bool:
    return bool(_pending_has_active_release_fields(entry) and not _pending_is_terminal_success(entry))


def _pending_has_deferred_release_fields(entry: dict[str, Any]) -> bool:
    return bool(entry.get("deferred_torrent_url") and entry.get("deferred_at"))


def _pending_has_deferred_release(entry: dict[str, Any]) -> bool:
    return bool(_pending_has_deferred_release_fields(entry) and not _pending_is_terminal_success(entry))


def _pending_active_info_hash(entry: dict[str, Any]) -> str:
    for value in (
        entry.get("last_qbit_hash"),
        entry.get("info_hash"),
        entry.get("torrent_url"),
        entry.get("deferred_info_hash"),
        entry.get("deferred_torrent_url"),
    ):
        candidate = str(extract_torrent_info_hash(str(value or "")) or value or "").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{40}", candidate):
            return candidate
    return ""


def _pending_entry_release_hashes(entry: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for value in (
        entry.get("completion_state_repair_hash"),
        entry.get("last_qbit_hash"),
        entry.get("info_hash"),
        entry.get("torrent_url"),
        entry.get("deferred_info_hash"),
        entry.get("deferred_torrent_url"),
        entry.get("last_completed_info_hash"),
        entry.get("last_superseded_info_hash"),
    ):
        candidate = str(extract_torrent_info_hash(str(value or "")) or value or "").strip().casefold()
        if re.fullmatch(r"[0-9a-f]{40}", candidate):
            hashes.add(candidate)
    return hashes


def _pending_has_runtime_failure_state(entry: dict[str, Any]) -> bool:
    return any(
        entry.get(key) not in (None, "", [], {})
        for key in (
            "failed_urls",
            "failed_info_hashes",
            "last_failure_reason",
            "last_extract_failed_at",
            "last_extract_failure_reason",
            "last_extract_failure_detail",
            "last_extract_deferred_at",
            "last_extract_deferred_reason",
            "last_extract_deferred_detail",
            "no_candidate_at",
            "no_candidate_until",
            "timed_out_at",
        )
    )


def _clear_terminal_success_runtime_failures(entry: dict[str, Any]) -> None:
    for key in (
        "failed_urls",
        "failed_info_hashes",
        "last_failed_torrent_url",
        "last_failed_info_hash",
        "last_failed_title",
        "last_failed_source",
        "last_failed_source_page",
        "last_failed_seeders",
        "last_failure_reason",
        "last_extract_failed_at",
        "last_extract_failure_reason",
        "last_extract_failure_detail",
        "last_extract_deferred_at",
        "last_extract_deferred_reason",
        "last_extract_deferred_detail",
        "last_extract_context",
        "last_subtitle_diagnostics",
        "timed_out_at",
    ):
        entry.pop(key, None)


def _public_mikan_failure_context(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    scalar_keys = [
        "qbit_hash",
        "qbit_name",
        "qbit_content_path",
        "qbit_save_path",
        "qbit_raw_path",
        "mapped_root",
        "mapped_root_exists",
        "replacement_recommended",
        "source_video",
        "source_video_exists",
        "target_video",
    ]
    for key in scalar_keys:
        if key in value and value[key] not in (None, ""):
            result[key] = value[key]

    for key in ("pending_episodes", "skipped_source_videos", "local_target_candidates"):
        items = value.get(key)
        if isinstance(items, list):
            public_items = [str(item) for item in items[:12] if str(item)]
            if public_items:
                result[key] = public_items

    target_candidates = value.get("target_candidates")
    if isinstance(target_candidates, list):
        public_candidates: list[dict[str, Any]] = []
        for item in target_candidates[:10]:
            if not isinstance(item, dict):
                continue
            public_item: dict[str, Any] = {}
            for key in ("path", "score", "runner_up_score", "margin", "reason", "reasons", "best_ratio"):
                if key in item and item[key] not in (None, "", []):
                    public_item[key] = item[key]
            if public_item:
                public_candidates.append(public_item)
        if public_candidates:
            result["target_candidates"] = public_candidates

    mappings = value.get("qbit_path_mappings")
    if isinstance(mappings, list):
        result["qbit_path_mappings"] = [
            {
                "remote": str(mapping.get("remote", "")),
                "local": str(mapping.get("local", "")),
            }
            for mapping in mappings[:10]
            if isinstance(mapping, dict)
        ]

    files = value.get("qbit_files")
    if isinstance(files, list):
        public_files: list[dict[str, Any]] = []
        for item in files[:12]:
            if not isinstance(item, dict):
                continue
            public_item: dict[str, Any] = {}
            for key in ("name", "size", "progress", "mapped_path", "mapped_exists", "video"):
                if key in item and item[key] not in (None, ""):
                    public_item[key] = item[key]
            if public_item:
                public_files.append(public_item)
        if public_files:
            result["qbit_files"] = public_files
    return {key: val for key, val in result.items() if val not in (None, "", [])}


def _clear_active_pending_release(entry: dict[str, Any]) -> bool:
    changed = False
    for key in (
        "torrent_url",
        "title",
        "queued_at",
        "tag",
        "last_progress_at",
        "last_progress",
        "last_downloaded",
        "last_qbit_sync_at",
        "last_dlspeed",
        "last_qbit_state",
        "last_qbit_hash",
        "last_qbit_name",
        "source",
        "source_page",
        "info_hash",
        "seeders",
    ):
        if key in entry:
            entry.pop(key, None)
            changed = True
    return changed


def _archive_active_release(entry: dict[str, Any], prefix: str) -> None:
    fields = {
        "title": entry.get("title"),
        "torrent_url": entry.get("torrent_url"),
        "source": entry.get("source"),
        "source_page": entry.get("source_page"),
        "info_hash": entry.get("info_hash"),
        "seeders": entry.get("seeders"),
    }
    for key, value in fields.items():
        if value not in (None, ""):
            entry[f"{prefix}_{key}"] = value


def _clear_deferred_release(entry: dict[str, Any]) -> None:
    for key in (
        "deferred_torrent_url",
        "deferred_title",
        "deferred_at",
        "deferred_reason",
        "deferred_pub_date",
        "deferred_source",
        "deferred_source_page",
        "deferred_info_hash",
        "deferred_seeders",
    ):
        entry.pop(key, None)


def _pending_entries_for_deferred_url(items: dict[str, Any], torrent_url: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in items.values()
        if isinstance(entry, dict) and str(entry.get("deferred_torrent_url", "")) == torrent_url
    ]


def _pending_failed_urls(entry: dict[str, Any]) -> list[str]:
    if _pending_failure_used_extra_video(entry) or _pending_failure_allows_source_retry(entry):
        return []
    failed = entry.get("failed_urls")
    if not isinstance(failed, list):
        return []
    return [str(url) for url in failed if str(url)]


def _pending_failed_info_hashes(entry: dict[str, Any]) -> set[str]:
    if _pending_failure_used_extra_video(entry) or _pending_failure_allows_source_retry(entry):
        return set()
    return _raw_pending_failed_info_hashes(entry)


def _raw_pending_failed_info_hashes(entry: dict[str, Any]) -> set[str]:
    hashes = {
        str(value).casefold()
        for value in entry.get("failed_info_hashes", [])
        if str(value).strip()
    } if isinstance(entry.get("failed_info_hashes"), list) else set()
    failed_urls = entry.get("failed_urls")
    if isinstance(failed_urls, list):
        hashes.update(
            info_hash
            for info_hash in (extract_torrent_info_hash(str(url)) for url in failed_urls)
            if info_hash
        )
    return hashes


def _pending_failure_allows_source_retry(entry: dict[str, Any]) -> bool:
    return _mikan_extract_failure_allows_same_source_retry(str(entry.get("last_extract_failure_reason") or ""))


def _pending_retryable_source_urls(entry: dict[str, Any]) -> set[str]:
    if not _pending_failure_allows_source_retry(entry):
        return set()
    failed = entry.get("failed_urls")
    if not isinstance(failed, list):
        return set()
    return {str(url) for url in failed if str(url)}


def _pending_retryable_info_hashes(entry: dict[str, Any]) -> set[str]:
    if not _pending_failure_allows_source_retry(entry):
        return set()
    return _raw_pending_failed_info_hashes(entry)


def _pending_failure_used_extra_video(entry: dict[str, Any]) -> bool:
    context = entry.get("last_extract_context")
    if not isinstance(context, dict):
        return False
    source_video = context.get("source_video")
    if not isinstance(source_video, str) or not source_video:
        return False
    return _is_extra_video_path(Path(source_video))


def _clear_completed_pending_entries(
    pending: dict[str, Any],
    torrent: QBitTorrent,
    series_mappings: list[dict[str, object]],
    *,
    extracted_count: int = 0,
) -> int:
    now = _utc_now().isoformat()
    changed = 0
    extracted_count = max(0, int(extracted_count or 0))
    for entry in _active_pending_entries_for_completed_torrent(pending, torrent, series_mappings):
        _archive_active_release(entry, "last_completed")
        if _clear_active_pending_release(entry):
            entry["completed_at"] = now
            entry["last_extracted_at"] = now
            entry["last_extracted_count"] = extracted_count
            entry["total_extracted_count"] = max(
                _coerce_int(entry.get("total_extracted_count")) or 0,
                extracted_count,
            )
            entry.pop("last_failure_reason", None)
            entry.pop("last_extract_failed_at", None)
            entry.pop("last_extract_failure_reason", None)
            entry.pop("last_extract_failure_detail", None)
            entry.pop("last_extract_deferred_at", None)
            entry.pop("last_extract_deferred_reason", None)
            entry.pop("last_extract_deferred_detail", None)
            entry.pop("last_extract_context", None)
            entry.pop("last_subtitle_diagnostics", None)
            changed += 1
    return changed


def _mark_completed_pending_extract_deferred(
    pending: dict[str, Any],
    torrent: QBitTorrent,
    series_mappings: list[dict[str, object]],
    *,
    deferred_reason: str = "",
    deferred_detail: str = "",
    failure_context: dict[str, Any] | None = None,
) -> bool:
    now = _utc_now().isoformat()
    changed = False
    for entry in _active_pending_entries_for_completed_torrent(pending, torrent, series_mappings):
        entry["last_extract_deferred_at"] = now
        entry["last_extract_deferred_reason"] = deferred_reason or "target_video_not_found"
        if deferred_detail:
            entry["last_extract_deferred_detail"] = deferred_detail[:1000]
        else:
            entry.pop("last_extract_deferred_detail", None)
        if failure_context:
            entry["last_extract_context"] = _public_mikan_failure_context(failure_context)
        else:
            entry.pop("last_extract_context", None)
        entry.pop("last_failure_reason", None)
        entry.pop("last_extract_failed_at", None)
        entry.pop("last_extract_failure_reason", None)
        entry.pop("last_extract_failure_detail", None)
        entry.pop("last_subtitle_diagnostics", None)
        changed = True
    return changed


def _mark_completed_pending_extract_failed(
    pending: dict[str, Any],
    torrent: QBitTorrent,
    series_mappings: list[dict[str, object]],
    *,
    failure_reason: str = "",
    failure_detail: str = "",
    failure_context: dict[str, Any] | None = None,
    subtitle_diagnostics: list[dict[str, Any]] | None = None,
) -> list[MikanReplacementTarget]:
    targets: list[MikanReplacementTarget] = []
    for entry in _active_pending_entries_for_completed_torrent(pending, torrent, series_mappings):
        targets.extend(
            _mark_active_pending_entry_extract_failed(
                entry,
                failure_reason=failure_reason or "no_usable_subtitles",
                failure_detail=failure_detail,
                failure_context=failure_context,
                subtitle_diagnostics=subtitle_diagnostics,
                failed_info_hash=str(torrent.hash or ""),
                failed_title=str(
                    entry.get("title")
                    or entry.get("deferred_title")
                    or entry.get("last_failed_title")
                    or torrent.name
                    or ""
                ),
            )
        )
    return targets


def _mark_active_pending_entry_extract_failed(
    entry: dict[str, Any],
    *,
    failure_reason: str = "",
    failure_detail: str = "",
    failure_context: dict[str, Any] | None = None,
    subtitle_diagnostics: list[dict[str, Any]] | None = None,
    failed_info_hash: str = "",
    failed_title: str = "",
) -> list[MikanReplacementTarget]:
    failed_url = str(entry.get("torrent_url", ""))
    normalized_failed_info_hash = str(
        entry.get("info_hash")
        or failed_info_hash
        or extract_torrent_info_hash(failed_url)
        or ""
    ).casefold()
    failed_title = str(failed_title or entry.get("title") or "")
    failed_urls = _pending_failed_urls(entry)
    if failed_url and failed_url not in failed_urls:
        failed_urls.append(failed_url)
    entry["failed_urls"] = failed_urls
    failed_info_hashes = list(_raw_pending_failed_info_hashes(entry))
    if normalized_failed_info_hash and normalized_failed_info_hash not in failed_info_hashes:
        failed_info_hashes.append(normalized_failed_info_hash)
    entry["failed_info_hashes"] = failed_info_hashes
    targets = [] if _mikan_extract_failure_suppresses_replacement(failure_reason) else _replacement_targets_from_pending_entry(entry)
    _archive_active_release(entry, "last_failed")
    if not _clear_active_pending_release(entry):
        return []

    entry["last_extract_failed_at"] = _utc_now().isoformat()
    entry["last_failure_reason"] = "extract_failed"
    entry["last_extract_failure_reason"] = failure_reason or "no_usable_subtitles"
    entry.pop("last_extract_deferred_at", None)
    entry.pop("last_extract_deferred_reason", None)
    entry.pop("last_extract_deferred_detail", None)
    if failure_detail:
        entry["last_extract_failure_detail"] = failure_detail[:1000]
    else:
        entry.pop("last_extract_failure_detail", None)
    if failure_context:
        entry["last_extract_context"] = _public_mikan_failure_context(failure_context)
    else:
        entry.pop("last_extract_context", None)
    if subtitle_diagnostics:
        entry["last_subtitle_diagnostics"] = subtitle_diagnostics[:10]
    else:
        entry.pop("last_subtitle_diagnostics", None)
    if failed_url:
        entry["last_failed_torrent_url"] = failed_url
    if normalized_failed_info_hash:
        entry["last_failed_info_hash"] = normalized_failed_info_hash
    if failed_title:
        entry["last_failed_title"] = failed_title
    return targets


def _completed_waiting_entry_is_stale(
    entry: dict[str, Any],
    torrents: list[QBitTorrent],
    series_mappings: list[dict[str, object]],
    *,
    now_ts: float,
    stale_seconds: int,
) -> bool:
    if not _pending_has_active_release(entry):
        return False
    if entry.get("last_extract_failed_at") or entry.get("last_extract_deferred_at") or entry.get("completed_at"):
        return False
    progress = _safe_float(entry.get("last_progress")) or 0.0
    if progress < 1.0:
        return False
    if _torrents_for_active_pending(entry, torrents, series_mappings):
        return False
    last_seen = max(
        _pending_timestamp(entry.get("last_qbit_sync_at")),
        _pending_timestamp(entry.get("last_progress_at")),
        _pending_timestamp(entry.get("queued_at")),
    )
    return last_seen > 0 and now_ts - last_seen >= stale_seconds


def _active_pending_entries_for_completed_torrent(
    pending: dict[str, Any],
    torrent: QBitTorrent,
    series_mappings: list[dict[str, object]],
) -> list[dict[str, Any]]:
    torrent_episodes = _torrent_episode_numbers(torrent.name)
    result: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for entry in _pending_items(pending).values():
        if not isinstance(entry, dict) or not _pending_has_active_release(entry):
            continue
        if _torrents_for_pending(entry, [torrent]) or _pending_entry_matches_completed_torrent(
            entry,
            torrent,
            torrent_episodes,
            series_mappings,
        ):
            entry_id = id(entry)
            if entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            result.append(entry)
    return result


def _pending_entry_matches_completed_torrent(
    entry: dict[str, Any],
    torrent: QBitTorrent,
    torrent_episodes: set[int],
    series_mappings: list[dict[str, object]],
) -> bool:
    pending_episodes = _pending_entry_episode_numbers(entry)
    if not pending_episodes or not torrent_episodes or pending_episodes.isdisjoint(torrent_episodes):
        return False

    bangumi_id = _coerce_int(entry.get("bangumi_id"))
    if bangumi_id is None:
        return False

    for mapping in series_mappings:
        mapping_bangumi_id = _coerce_int(mapping.get("bangumi_id"))
        if mapping_bangumi_id != bangumi_id:
            continue
        if _torrent_matches_series(torrent.name, mapping):
            return True
    return _sonarr_style_pending_entry_matches_completed_torrent(entry, torrent)


def _pending_entry_episode_numbers(entry: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    episodes = entry.get("episodes")
    if isinstance(episodes, (list, tuple, set)):
        for value in episodes:
            episode = _coerce_int(value)
            if episode is not None:
                result.add(episode)
    episode = _coerce_int(entry.get("episode"))
    if episode is not None:
        result.add(episode)
    return result


def _pending_entry_is_release_part_false_positive(entry: dict[str, Any]) -> bool:
    episodes = _pending_entry_episode_numbers(entry)
    if not episodes:
        return False
    title = str(
        entry.get("title")
        or entry.get("deferred_title")
        or entry.get("last_qbit_name")
        or ""
    )
    if not title:
        return False
    if episodes.intersection(extract_episode_numbers(title)):
        return False
    normalized = unicodedata.normalize("NFKC", title)
    return any(
        re.search(
            rf"(?i)(?:^|[\s._\-\[(])(?:part|pt|vol(?:ume)?|movie|film)[\s._\-\[(]*0*{episode}(?=$|[\s._\-\])])",
            normalized,
        )
        is not None
        for episode in episodes
    )


def _pending_entry_primary_episode_number(entry: dict[str, Any]) -> int | None:
    return _coerce_int(entry.get("episode"))


def _pending_source_episode_numbers_for_completed_torrent(
    pending: dict[str, Any],
    torrent: QBitTorrent,
    series_mappings: list[dict[str, object]],
) -> set[int]:
    return _completed_torrent_fallback_episode_numbers(
        torrent,
        _active_pending_entries_for_completed_torrent(pending, torrent, series_mappings),
    )


def _pending_source_episode_numbers_from_entries(entries: list[dict[str, Any]]) -> set[int]:
    episodes: set[int] = set()
    for entry in entries:
        primary = _pending_entry_primary_episode_number(entry)
        if primary is not None:
            episodes.add(primary)
        else:
            episodes.update(_pending_entry_episode_numbers(entry))
    return episodes


def _pending_extract_priority(entries: list[dict[str, Any]]) -> int:
    episodes = _pending_source_episode_numbers_from_entries(entries)
    episode_priority = max(episodes or {0})
    release_time = 0.0
    for entry in entries:
        release_time = max(
            release_time,
            _pending_timestamp(entry.get("pub_date")),
            _pending_timestamp(entry.get("deferred_pub_date")),
            _pending_timestamp(entry.get("queued_at")),
            _pending_timestamp(entry.get("deferred_at")),
        )
    return int(release_time) * 10000 + max(0, min(int(episode_priority or 0), 9999))


def _source_videos_for_pending_episodes(source_videos: list[Path], episodes: set[int]) -> list[Path]:
    return _select_source_videos_for_pending_episodes(source_videos, episodes).selected


def _select_source_videos_for_pending_episodes(
    source_videos: list[Path],
    episodes: set[int],
) -> MikanSourceVideoSelection:
    if not episodes:
        main_videos = [source_video for source_video in source_videos if not _is_extra_video_path(source_video)]
        return MikanSourceVideoSelection(main_videos or source_videos)

    parsed: list[tuple[Path, int]] = []
    unparsed: list[Path] = []
    for source_video in source_videos:
        episode = extract_episode_number(source_video.name)
        if episode is None:
            unparsed.append(source_video)
            continue
        parsed.append((source_video, episode))
    if not parsed:
        main_unparsed = [source_video for source_video in unparsed if not _is_extra_video_path(source_video)]
        if main_unparsed:
            return MikanSourceVideoSelection(main_unparsed)
        if unparsed:
            return MikanSourceVideoSelection(
                [],
                failure_reason="extra_video_only",
                failure_detail="Only extra/special source videos were found; skipping this torrent source.",
                skipped_extra_videos=unparsed,
            )
        return MikanSourceVideoSelection([])

    filtered = [source_video for source_video, episode in parsed if episode in episodes]
    main_filtered = [source_video for source_video in filtered if not _is_extra_video_path(source_video)]
    if main_filtered:
        skipped = [source_video for source_video in filtered if source_video not in main_filtered]
        return MikanSourceVideoSelection(main_filtered, skipped_extra_videos=skipped)
    if filtered:
        return MikanSourceVideoSelection(
            [],
            failure_reason="extra_video_only",
            failure_detail=(
                "Only extra/special videos matched pending episodes "
                f"{_format_episode_list(episodes)}; skipping this torrent source."
            ),
            skipped_extra_videos=filtered,
        )

    main_unparsed = [source_video for source_video in unparsed if not _is_extra_video_path(source_video)]
    if main_unparsed:
        return MikanSourceVideoSelection(
            [],
            failure_reason="source_episode_not_found",
            failure_detail=(
                "Completed torrent has episode-numbered videos, but none matched pending episodes "
                f"{_format_episode_list(episodes)}; not falling back to unparsed source videos."
            ),
            skipped_extra_videos=main_unparsed,
        )
    if unparsed:
        return MikanSourceVideoSelection(
            [],
            failure_reason="extra_video_only",
            failure_detail=(
                "Only unparsed extra/special source videos remained for pending episodes "
                f"{_format_episode_list(episodes)}; skipping this torrent source."
            ),
            skipped_extra_videos=unparsed,
        )
    return MikanSourceVideoSelection(
        [],
        failure_reason="source_episode_not_found",
        failure_detail=(
            "No completed source video matched pending episodes "
            f"{_format_episode_list(episodes)}."
        ),
    )


def _is_extra_video_path(path: Path) -> bool:
    exact_markers = {
        "pv",
        "sp",
        "cm",
        "menu",
        "menus",
        "special",
        "specials",
        "extra",
        "extras",
        "bonus",
        "bonuses",
        "scan",
        "scans",
        "tokuten",
        "eizoutokuten",
        "videoextra",
        "videoextras",
        "特典",
        "映像特典",
    }
    contains_markers = ("ncop", "nced", "creditlessop", "creditlessed")
    for part in path.parts:
        normalized = _normalized_path_marker(part)
        if normalized in exact_markers or any(marker in normalized for marker in contains_markers):
            return True
    for token in re.findall(r"[\[\(]([^\]\)]+)[\]\)]", path.stem):
        normalized = _normalized_path_marker(token)
        if normalized in exact_markers or any(marker in normalized for marker in contains_markers):
            return True
    return False


def _normalized_path_marker(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _torrent_episode_numbers(title: str) -> set[int]:
    episodes = set(extract_episode_numbers(title))
    episode = extract_episode_number(title)
    if episode is not None:
        episodes.add(episode)
    return episodes


def _completed_torrent_fallback_episode_numbers(
    torrent: QBitTorrent,
    pending_entries: list[dict[str, Any]] | None,
) -> set[int]:
    pending_episodes = (
        _pending_source_episode_numbers_from_entries(pending_entries)
        if pending_entries is not None
        else set()
    )
    torrent_episodes = _torrent_episode_numbers(torrent.name)
    if not torrent_episodes:
        return pending_episodes
    if not pending_episodes:
        return torrent_episodes
    if len(torrent_episodes) == 1 and not _torrent_title_looks_like_episode_range(torrent.name):
        return torrent_episodes
    intersection = pending_episodes.intersection(torrent_episodes)
    if intersection:
        return intersection
    return pending_episodes


def _torrent_title_looks_like_episode_range(title: str) -> bool:
    text = str(title or "")
    for match in re.finditer(r"\d{1,3}\s*[-~]\s*\d{1,3}", text):
        prefix = text[: match.start()].rstrip()
        if prefix and prefix[-1].casefold() == "s":
            continue
        return True
    return False


def _active_pending_episodes_by_bangumi(pending: dict[str, Any], *, timeout_seconds: int) -> dict[int, set[int]]:
    active: dict[int, set[int]] = {}
    for entry in _pending_items(pending).values():
        if not isinstance(entry, dict) or not _pending_has_active_release(entry):
            continue
        if (_utc_now() - _parse_pending_time(entry.get("queued_at"))).total_seconds() >= timeout_seconds:
            continue
        try:
            bangumi_id = int(entry["bangumi_id"])
            episode = int(entry["episode"])
        except (KeyError, TypeError, ValueError):
            continue
        active.setdefault(bangumi_id, set()).add(episode)
    return active


def _torrents_for_pending(entry: dict[str, Any], torrents: list[QBitTorrent]) -> list[QBitTorrent]:
    title = str(entry.get("title", ""))
    normalized_title = _normalized_title(title)
    expected_hashes = {
        info_hash
        for info_hash in (
            extract_torrent_info_hash(str(entry.get("info_hash") or "")),
            extract_torrent_info_hash(str(entry.get("last_qbit_hash") or "")),
            extract_torrent_info_hash(str(entry.get("torrent_url") or "")),
        )
        if info_hash
    }
    result: list[QBitTorrent] = []
    for torrent in torrents:
        if torrent.hash and torrent.hash.casefold() in expected_hashes:
            result.append(torrent)
            continue
        normalized_torrent_name = _normalized_title(torrent.name)
        if normalized_title and (
            normalized_title == normalized_torrent_name
            or normalized_title in normalized_torrent_name
            or normalized_torrent_name in normalized_title
        ):
            result.append(torrent)
    return result


def _torrents_for_active_pending(
    entry: dict[str, Any],
    torrents: list[QBitTorrent],
    series_mappings: list[dict[str, object]],
) -> list[QBitTorrent]:
    result: list[QBitTorrent] = []
    seen_hashes: set[str] = set()

    def add(torrent: QBitTorrent) -> None:
        key = torrent.hash or torrent.name
        if key in seen_hashes:
            return
        seen_hashes.add(key)
        result.append(torrent)

    for torrent in _torrents_for_pending(entry, torrents):
        add(torrent)

    for torrent in torrents:
        if _pending_entry_matches_completed_torrent(
            entry,
            torrent,
            _torrent_episode_numbers(torrent.name),
            series_mappings,
        ):
            add(torrent)

    remembered_hash = str(entry.get("last_qbit_hash") or "")
    if remembered_hash:
        for torrent in torrents:
            if torrent.hash == remembered_hash and _qbit_torrent_still_matches_pending_entry(
                entry,
                torrent,
                series_mappings,
            ):
                add(torrent)
    return result


def _qbit_torrent_still_matches_pending_entry(
    entry: dict[str, Any],
    torrent: QBitTorrent,
    series_mappings: list[dict[str, object]],
) -> bool:
    if _torrents_for_pending(entry, [torrent]):
        return True
    return _pending_entry_matches_completed_torrent(
        entry,
        torrent,
        _torrent_episode_numbers(torrent.name),
        series_mappings,
    )


def _clear_invalid_qbit_snapshot_if_needed(
    entry: dict[str, Any],
    torrents: list[QBitTorrent],
    series_mappings: list[dict[str, object]],
) -> bool:
    remembered_hash = str(entry.get("last_qbit_hash") or "")
    if not remembered_hash:
        return False

    remembered_torrent = next((torrent for torrent in torrents if torrent.hash == remembered_hash), None)
    if remembered_torrent is not None and _qbit_torrent_still_matches_pending_entry(entry, remembered_torrent, series_mappings):
        return False

    changed = False
    for key in (
        "last_downloaded",
        "last_progress",
        "last_progress_at",
        "last_qbit_sync_at",
        "last_dlspeed",
        "last_qbit_state",
        "last_qbit_hash",
        "last_qbit_name",
        "last_qbit_added_on",
        "last_qbit_completion_on",
    ):
        if key in entry:
            entry.pop(key, None)
            changed = True
    return changed


def _sync_pending_entry_qbit_progress(entry: dict[str, Any], torrents: list[QBitTorrent], now: datetime) -> bool:
    downloaded, progress = _pending_progress_snapshot(torrents)
    previous_downloaded = _coerce_int(entry.get("last_downloaded"))
    previous_progress = _coerce_float(entry.get("last_progress"))
    previous_dlspeed = _coerce_int(entry.get("last_dlspeed")) or 0
    primary = max(torrents, key=lambda torrent: (torrent.progress, torrent.downloaded, torrent.dlspeed))
    dlspeed = max(torrent.dlspeed for torrent in torrents)
    state = primary.state
    sync_at = now.isoformat()
    has_progressed = (
        (previous_downloaded is not None and downloaded > previous_downloaded)
        or (previous_progress is not None and progress > previous_progress)
    )
    speed_activity_changed = (previous_dlspeed > 0) != (dlspeed > 0)
    state_changed = str(entry.get("last_qbit_state") or "") != state
    identity_changed = (
        str(entry.get("last_qbit_hash") or "") != primary.hash
        or str(entry.get("last_qbit_name") or "") != primary.name
    )
    sync_age = now.timestamp() - _pending_timestamp(entry.get("last_qbit_sync_at"))
    sync_due = sync_age >= MIKAN_QBIT_SYNC_HEARTBEAT_SECONDS
    urgent_progress = has_progressed and dlspeed <= 0
    info_hash_missing = bool(primary.hash and not entry.get("info_hash"))
    qbit_time_missing = bool(
        (primary.added_on and not entry.get("last_qbit_added_on"))
        or (primary.completion_on and not entry.get("last_qbit_completion_on"))
    )
    if not (
        sync_due
        or speed_activity_changed
        or state_changed
        or identity_changed
        or urgent_progress
        or info_hash_missing
        or qbit_time_missing
    ):
        return False

    updates = {
        "last_downloaded": downloaded,
        "last_progress": progress,
        "last_qbit_sync_at": sync_at,
        "last_dlspeed": dlspeed,
        "last_qbit_state": state,
        "last_qbit_hash": primary.hash,
        "last_qbit_name": primary.name,
    }
    if primary.added_on:
        updates["last_qbit_added_on"] = int(primary.added_on)
    if primary.completion_on:
        updates["last_qbit_completion_on"] = int(primary.completion_on)
    if dlspeed > 0 or has_progressed or (speed_activity_changed and previous_dlspeed > 0):
        updates["last_progress_at"] = sync_at
    elif not entry.get("last_progress_at"):
        updates["last_progress_at"] = entry.get("queued_at") or sync_at
    if primary.hash and not entry.get("info_hash"):
        updates["info_hash"] = primary.hash.casefold()
    changed = False
    for key, value in updates.items():
        if entry.get(key) != value:
            entry[key] = value
            changed = True
    return changed


def _pending_progress_is_active(entry: dict[str, Any], torrents: list[QBitTorrent], now: datetime) -> bool:
    downloaded, progress = _pending_progress_snapshot(torrents)
    previous_downloaded = _coerce_int(entry.get("last_downloaded"))
    previous_progress = _coerce_float(entry.get("last_progress"))
    is_downloading = any(torrent.dlspeed > 0 for torrent in torrents)
    has_progressed = (
        (previous_downloaded is not None and downloaded > previous_downloaded)
        or (previous_progress is not None and progress > previous_progress)
    )

    if is_downloading or has_progressed:
        entry["last_downloaded"] = downloaded
        entry["last_progress"] = progress
        entry["last_progress_at"] = now.isoformat()
        return True

    if previous_downloaded is None and previous_progress is None:
        entry["last_downloaded"] = downloaded
        entry["last_progress"] = progress
        entry.setdefault("last_progress_at", entry.get("queued_at") or now.isoformat())

    return False


def _pending_progress_snapshot(torrents: list[QBitTorrent]) -> tuple[int, float]:
    if not torrents:
        return 0, 0.0
    return max(torrent.downloaded for torrent in torrents), max(torrent.progress for torrent in torrents)


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _torrent_has_started(torrent: QBitTorrent) -> bool:
    if _is_completed(torrent):
        return True
    return torrent.progress > 0 or torrent.downloaded > 0 or torrent.dlspeed > 0


def _torrent_waiting_for_download_slot(torrent: QBitTorrent) -> bool:
    return str(torrent.state or "").strip().casefold() in {
        "allocating",
        "checkingdl",
        "checkingresumedata",
        "moving",
        "pauseddl",
        "queueddl",
        "stoppeddl",
    }


def _torrent_waiting_for_metadata(torrent: QBitTorrent) -> bool:
    return str(torrent.state or "").strip().casefold() in {
        "forcedmetadl",
        "metadl",
    }


def _parse_pending_time(value: object) -> datetime:
    if not isinstance(value, str):
        return _utc_now()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return _utc_now()
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_title(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _is_completed(torrent: QBitTorrent) -> bool:
    return torrent.progress >= 1.0 and torrent.state not in {"error", "missingFiles"}


def _has_any_tag(torrent: QBitTorrent, tags: list[str]) -> bool:
    if not torrent.tags:
        return False
    torrent_tags = {tag.strip() for tag in torrent.tags.split(",") if tag.strip()}
    return any(tag in torrent_tags for tag in tags)


def _missing_torrent_tags(torrent: QBitTorrent, tags: list[str]) -> list[str]:
    current = {
        tag.strip()
        for tag in str(torrent.tags or "").split(",")
        if tag.strip()
    }
    return [tag for tag in tags if tag and tag not in current]


def _find_video_files(
    path: Path,
    extensions: list[str],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[Path]:
    extension_set = {extension.lower() for extension in extensions}
    if path.is_file():
        return [path] if path.suffix.lower() in extension_set else []
    result: list[Path] = []
    for item in path.rglob("*"):
        if cancelled is not None and cancelled():
            break
        if item.suffix.lower() in extension_set and item.is_file():
            result.append(item)
    return sorted(result, key=lambda item: str(item).casefold())


def _torrent_video_paths_from_file_list(
    torrent: QBitTorrent,
    torrent_files: list[QBitTorrentFile],
    config: AppConfig,
) -> list[Path]:
    save_root = map_remote_path(torrent.save_path, config.qbit_path_mappings)
    if save_root is None:
        return []

    extension_set = {extension.lower() for extension in config.video_extensions}
    paths: list[Path] = []
    for torrent_file in torrent_files:
        path = _qbit_file_path(save_root, torrent_file.name)
        if path is None or path.suffix.lower() not in extension_set:
            continue
        paths.append(path)
    return _unique_paths(paths)


def _torrent_sidecar_paths_for_source_video(
    source_video: Path,
    torrent: QBitTorrent,
    torrent_files: list[QBitTorrentFile],
    root: Path,
    config: AppConfig,
) -> list[Path]:
    candidates: list[Path] = []
    save_root = map_remote_path(torrent.save_path, config.qbit_path_mappings)
    if save_root is not None:
        for torrent_file in torrent_files:
            path = _qbit_file_path(save_root, torrent_file.name)
            if path is None or path.suffix.lower() not in SIDECAR_SUBTITLE_EXTENSIONS:
                continue
            candidates.append(path)

    if not candidates and root.exists() and root.is_dir():
        candidates.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in SIDECAR_SUBTITLE_EXTENSIONS
        )

    return _filter_sidecar_paths_for_source_video(source_video, candidates)


def _filter_sidecar_paths_for_source_video(source_video: Path, candidates: list[Path]) -> list[Path]:
    source_episode = extract_episode_number(source_video.name)
    source_stem = _normalized_path_marker(source_video.stem)
    selected: list[Path] = []
    for path in candidates:
        if _is_extra_video_path(path):
            continue
        subtitle_episode = extract_episode_number(path.name)
        subtitle_episodes = set(extract_episode_numbers(path.name))
        if subtitle_episode is not None:
            subtitle_episodes.add(subtitle_episode)
        subtitle_stem = _normalized_path_marker(path.stem)
        same_stem = bool(
            source_stem
            and subtitle_stem
            and (source_stem in subtitle_stem or subtitle_stem in source_stem)
        )
        if source_episode is not None:
            if source_episode in subtitle_episodes or same_stem:
                selected.append(path)
            continue
        if same_stem:
            selected.append(path)

    return _unique_paths(selected)


def _qbit_file_path(save_root: Path, name: str) -> Path | None:
    normalized = name.replace("\\", "/").lstrip("/")
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    return save_root.joinpath(*parts)


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(_safe_resolve(path)).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _safe_path_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _recent_library_scan_sort_key(mapping: dict[str, object]) -> tuple[int, str]:
    path = Path(str(mapping.get("path", "")))
    return (-_safe_path_mtime_ns(path), str(path).casefold())


def _append_unique_mapping(
    selected: list[dict[str, object]],
    selected_keys: set[str],
    mapping: dict[str, object],
) -> bool:
    key = f"{mapping.get('bangumi_id')}:{mapping.get('path')}"
    if key in selected_keys:
        return False
    selected.append(mapping)
    selected_keys.add(key)
    return True


def _iter_rotating(items: list[dict[str, object]], offset: int):
    if not items:
        return
    start = offset % len(items)
    for index in range(len(items)):
        yield items[(start + index) % len(items)]


def _library_scan_rotate_offset(total: int, batch_size: int, config: AppConfig) -> int:
    if total <= 0 or batch_size <= 0:
        return 0
    interval_seconds = int(
        getattr(config, "watch_interval_seconds", getattr(config, "mikan_watch_interval_seconds", 300)) or 300
    )
    interval_seconds = max(1, interval_seconds)
    window = int(datetime.now(timezone.utc).timestamp()) // interval_seconds
    return (window * batch_size) % total


def _fallback_video_files_for_torrent(
    torrent: QBitTorrent,
    config: AppConfig,
    logger: logging.Logger,
    series_mappings: list[dict[str, object]],
    *,
    pending_entries: list[dict[str, Any]] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[Path]:
    if cancelled is not None and cancelled():
        return []
    episodes = _completed_torrent_fallback_episode_numbers(torrent, pending_entries)
    if not episodes:
        logger.warning("Cannot fallback-search completed torrent without episode numbers: %s", torrent.name)
        return []

    source_aliases = _sonarr_style_pending_title_aliases(torrent, pending_entries)
    if pending_entries is not None:
        candidate_mappings = _verified_series_mappings_for_pending_entries(
            series_mappings,
            pending_entries,
            torrent,
        )
        require_series_match = False
        if not candidate_mappings:
            logger.warning(
                "Completed torrent has pending bangumi metadata but no scoped series mapping; refusing global episode fallback. torrent=%s bangumi_ids=%s",
                torrent.name,
                sorted(
                    {
                        value
                        for entry in pending_entries
                        if (value := _coerce_int(entry.get("bangumi_id"))) is not None
                    }
                ),
            )
            return []
    else:
        candidate_mappings = _trusted_unscoped_series_mappings(
            config,
            series_mappings,
            source_aliases,
            torrent_name=torrent.name,
            episode=min(episodes),
            logger=logger,
            cancelled=cancelled,
        )
        require_series_match = False
        if not candidate_mappings:
            logger.warning(
                "Completed torrent has no uniquely trusted series scope; refusing global episode fallback. torrent=%s episodes=%s",
                torrent.name,
                sorted(episodes),
            )
            return []
    matches: list[Path] = []
    for mapping in candidate_mappings:
        if cancelled is not None and cancelled():
            return []
        if require_series_match and not _torrent_matches_series(torrent.name, mapping):
            continue
        root = Path(str(mapping["path"]))
        if not root.exists():
            logger.warning("Fallback series path does not exist: %s", root)
            continue
        for video in _find_video_files(root, config.video_extensions, cancelled=cancelled):
            if extract_episode_number(video.name) in episodes:
                matches.append(video)

    if _scoped_completed_source_season_unverified(
        source_video=None, torrent_name=torrent.name, mappings=candidate_mappings,
    ) or _explicit_completed_season_conflict(
        source_video=None, torrent_name=torrent.name,
        mappings=candidate_mappings, pending_entries=pending_entries,
    ):
        return []
    season_hint = _season_hint_for_completed_target(
        source_video=None,
        torrent_name=torrent.name,
        mappings=candidate_mappings,
        pending_entries=pending_entries,
    )
    source_season_hint = _season_hint_from_completed_source_aliases(source_aliases)
    if source_season_hint is not None:
        season_hint = source_season_hint
        matches = _filter_candidates_by_strict_season_hint(matches, source_season_hint)
    elif season_hint is None:
        season_hint = _season_hint_from_mapping_titles_for_candidates(matches, candidate_mappings)
        if season_hint is not None:
            matches = _filter_candidates_by_strict_season_hint(matches, season_hint)
    matches = _filter_candidates_for_distinct_completed_source(
        matches,
        source_aliases,
        logger=logger,
        context="completed torrent fallback",
    )
    return _sonarr_style_select_target_from_candidates(
        matches,
        source_aliases,
        torrent_name=torrent.name,
        source_video=None,
        logger=logger,
        context="completed torrent fallback",
        season_hint=season_hint,
        pending_entries=pending_entries,
        trusted_mapping_scope=True,
    )


def _completed_torrent_outputs_complete(
    torrent: QBitTorrent,
    config: AppConfig,
    logger: logging.Logger,
    series_mappings: list[dict[str, object]],
    *,
    pending_entries: list[dict[str, Any]] | None = None,
) -> bool:
    root = map_remote_path(torrent.content_path or torrent.save_path, config.qbit_path_mappings)
    if root is None:
        return True
    if root.exists():
        source_videos = _find_video_files(root, config.video_extensions)
    else:
        source_videos = _fallback_video_files_for_torrent(
            torrent,
            config,
            logger,
            series_mappings,
            pending_entries=pending_entries,
        )

    if not source_videos:
        return not _completed_torrent_has_local_episode(
            torrent,
            config,
            series_mappings,
            pending_entries=pending_entries,
        )

    found_target = False
    for source_video in source_videos:
        target_video = _target_video_for_torrent_source(
            source_video,
            torrent,
            config,
            logger,
            series_mappings,
            pending_entries=pending_entries,
        )
        if target_video is None:
            continue
        found_target = True
        if not _target_has_required_chinese_subtitles(target_video, verify_config=config):
            return False
    return found_target


def _completed_torrent_has_local_episode(
    torrent: QBitTorrent,
    config: AppConfig,
    series_mappings: list[dict[str, object]],
    *,
    pending_entries: list[dict[str, Any]] | None = None,
) -> bool:
    episodes = _completed_torrent_fallback_episode_numbers(torrent, pending_entries)
    if not episodes:
        return True

    matched_series = False
    source_aliases = _sonarr_style_pending_title_aliases(torrent, pending_entries)
    if pending_entries is not None:
        # This is only a cheap readiness check used to decide whether an
        # extraction job should be queued.  Keep it Bangumi-scoped, but do not
        # require final identity proof here: the actual target resolver below
        # applies the stricter verified-mapping gate before publishing files.
        candidate_mappings = _series_mappings_for_pending_entries(
            series_mappings,
            pending_entries,
        )
        require_series_match = False
    else:
        candidate_mappings = _trusted_unscoped_series_mappings(
            config,
            series_mappings,
            source_aliases,
            torrent_name=torrent.name,
            episode=min(episodes),
            logger=None,
        )
        require_series_match = False
        if not candidate_mappings:
            return False
    season_hint = _season_hint_for_completed_target(
        source_video=None,
        torrent_name=torrent.name,
        mappings=candidate_mappings,
        pending_entries=pending_entries,
    )
    source_season_hint = _season_hint_from_completed_source_aliases(source_aliases)
    if source_season_hint is not None:
        season_hint = source_season_hint
    indexed_candidates, effective_season_hint = _target_videos_from_episode_index_with_hint_fallback(
        config,
        candidate_mappings,
        sorted(episodes)[0],
        season_hint=season_hint,
    )
    if source_season_hint is not None:
        effective_season_hint = source_season_hint
        indexed_candidates = _filter_candidates_by_strict_season_hint(indexed_candidates, source_season_hint)
    if effective_season_hint is None:
        mapping_season_hint = _season_hint_from_mapping_titles_for_candidates(indexed_candidates, candidate_mappings)
        if mapping_season_hint is not None:
            effective_season_hint = mapping_season_hint
            indexed_candidates = _filter_candidates_by_strict_season_hint(indexed_candidates, mapping_season_hint)
    indexed_candidates = _filter_candidates_for_distinct_completed_source(
        indexed_candidates,
        source_aliases,
        logger=None,
        context="completed torrent local episode index",
    )
    if indexed_candidates and len(episodes) == 1:
        return True
    if len(episodes) > 1:
        indexed_episode_count = 0
        for episode in sorted(episodes):
            episode_candidates = _target_videos_from_episode_index(
                config,
                candidate_mappings,
                episode,
                season_hint=effective_season_hint,
            )
            if source_season_hint is not None:
                episode_candidates = _filter_candidates_by_strict_season_hint(episode_candidates, source_season_hint)
            episode_candidates = _filter_candidates_for_distinct_completed_source(
                episode_candidates,
                source_aliases,
                logger=None,
                context="completed torrent local episode index range",
            )
            if episode_candidates:
                indexed_episode_count += 1
        if indexed_episode_count == len(episodes):
            return True
    for mapping in candidate_mappings:
        if require_series_match and not _torrent_matches_series(torrent.name, mapping):
            continue
        matched_series = True
        root = Path(str(mapping["path"]))
        if not root.exists():
            continue
        candidates: list[Path] = []
        for video in _find_video_files(root, config.video_extensions):
            if extract_episode_number(video.name) in episodes:
                candidates.append(video)
        if source_season_hint is not None:
            candidates = _filter_candidates_by_strict_season_hint(candidates, source_season_hint)
        candidates = _filter_candidates_for_distinct_completed_source(
            candidates,
            source_aliases,
            logger=None,
            context="completed torrent local episode",
        )
        if _disambiguate_target_videos(
            candidates,
            torrent_name=torrent.name,
            source_video=None,
            logger=None,
            context="completed torrent local episode",
            season_hint=effective_season_hint,
        ):
            return True
    return _sonarr_style_completed_torrent_has_local_episode(
        torrent,
        config,
        candidate_mappings,
        episodes=episodes,
        season_hint=effective_season_hint,
        pending_entries=pending_entries,
    )


def _target_video_for_torrent_source(
    source_video: Path,
    torrent: QBitTorrent,
    config: AppConfig,
    logger: logging.Logger,
    series_mappings: list[dict[str, object]],
    *,
    pending_entries: list[dict[str, Any]] | None = None,
    target_diagnostics: list[dict[str, Any]] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path | None:
    if cancelled is not None and cancelled():
        return None
    if _is_inside_series_paths(source_video, series_mappings):
        return source_video

    episode = extract_episode_number(source_video.name) or extract_episode_number(torrent.name)
    if episode is None and pending_entries:
        pending_episodes = _pending_source_episode_numbers_from_entries(pending_entries)
        if len(pending_episodes) == 1:
            episode = next(iter(pending_episodes))
    if episode is None:
        logger.warning("Cannot match target video without episode number: %s", source_video)
        return None

    source_aliases = _sonarr_style_source_title_aliases(source_video, torrent, pending_entries)
    if pending_entries is not None:
        candidate_mappings = _verified_series_mappings_for_pending_entries(
            series_mappings,
            pending_entries,
            torrent,
        )
        require_series_match = False
        if not candidate_mappings:
            if target_diagnostics is not None:
                target_diagnostics.append(
                    {
                        "path": "",
                        "score": 0,
                        "reason": "no_series_mapping_for_pending_bangumi",
                        "reasons": ["pending_bangumi_unmapped"],
                    }
                )
            logger.warning(
                "Completed torrent has pending bangumi metadata but no scoped series mapping; refusing global episode fallback. torrent=%s bangumi_ids=%s",
                torrent.name,
                sorted(
                    {
                        value
                        for entry in pending_entries
                        if (value := _coerce_int(entry.get("bangumi_id"))) is not None
                    }
                ),
            )
            return None
    else:
        candidate_mappings = _trusted_unscoped_series_mappings(
            config,
            series_mappings,
            source_aliases,
            torrent_name=torrent.name,
            episode=episode,
            logger=logger,
            diagnostics=target_diagnostics,
            cancelled=cancelled,
        )
        require_series_match = False
        if not candidate_mappings:
            logger.warning(
                "Completed torrent has no uniquely trusted series scope; refusing global episode fallback. torrent=%s episode=%s",
                torrent.name,
                episode,
            )
            return None
    if _scoped_completed_source_season_unverified(
        source_video=source_video, torrent_name=torrent.name, mappings=candidate_mappings,
    ):
        if target_diagnostics is not None:
            target_diagnostics.append({"path": "", "score": 0, "reason": "scoped_source_season_unverified",
                                       "reasons": ["completed_source_explicit_season_required"]})
        logger.warning("Recovered season scope needs explicit completed-source season; keep source for review: %s", source_video)
        return None
    if _explicit_completed_season_conflict(
        source_video=source_video, torrent_name=torrent.name,
        mappings=candidate_mappings, pending_entries=pending_entries,
    ):
        if target_diagnostics is not None:
            target_diagnostics.append({"path": "", "score": 0, "reason": "conflicting_explicit_seasons",
                                       "reasons": ["source_mapping_season_conflict"]})
        logger.warning("Conflicting explicit source/mapping seasons; keep completed source for review: %s", source_video)
        return None
    season_hint = _season_hint_for_completed_target(
        source_video=source_video,
        torrent_name=torrent.name,
        mappings=candidate_mappings,
        pending_entries=pending_entries,
    )
    source_season_hint = _season_hint_from_completed_source_aliases(source_aliases)
    if source_season_hint is not None:
        season_hint = source_season_hint
    indexed_candidates, effective_season_hint = _target_videos_from_episode_index_with_hint_fallback(
        config,
        candidate_mappings,
        episode,
        season_hint=season_hint,
    )
    if source_season_hint is not None:
        effective_season_hint = source_season_hint
        indexed_candidates = _filter_candidates_by_strict_season_hint(indexed_candidates, source_season_hint)
    indexed_candidates = _filter_candidates_for_distinct_completed_source(
        indexed_candidates,
        source_aliases,
        logger=logger,
        context="completed torrent indexed target",
    )
    if indexed_candidates:
        selected = _sonarr_style_select_target_from_candidates(
            indexed_candidates,
            source_aliases,
            torrent_name=torrent.name,
            source_video=source_video,
            logger=logger,
            context="completed torrent indexed target",
            season_hint=effective_season_hint,
            pending_entries=pending_entries,
            diagnostics=target_diagnostics,
            trusted_mapping_scope=True,
        )
        if selected:
            return selected[0]
    for mapping in candidate_mappings:
        if cancelled is not None and cancelled():
            return None
        if require_series_match and not _torrent_matches_series(torrent.name, mapping):
            continue
        root = Path(str(mapping["path"]))
        if not root.exists():
            logger.warning("Target series path does not exist: %s", root)
            continue
        candidates: list[Path] = []
        for candidate in _find_video_files(root, config.video_extensions, cancelled=cancelled):
            if extract_episode_number(candidate.name) == episode:
                candidates.append(candidate)
        if cancelled is not None and cancelled():
            return None
        if source_season_hint is not None:
            candidates = _filter_candidates_by_strict_season_hint(candidates, source_season_hint)
        candidate_season_hint = effective_season_hint
        if candidate_season_hint is None:
            candidate_season_hint = _season_hint_from_mapping_titles_for_candidates(candidates, [mapping])
            if candidate_season_hint is not None:
                candidates = _filter_candidates_by_strict_season_hint(candidates, candidate_season_hint)
        candidates = _filter_candidates_for_distinct_completed_source(
            candidates,
            source_aliases,
            logger=logger,
            context="completed torrent target",
        )
        selected = _sonarr_style_select_target_from_candidates(
            candidates,
            source_aliases,
            torrent_name=torrent.name,
            source_video=source_video,
            logger=logger,
            context="completed torrent target",
            season_hint=candidate_season_hint,
            pending_entries=pending_entries,
            diagnostics=target_diagnostics,
            trusted_mapping_scope=True,
        )
        if selected:
            return selected[0]
    sonarr_style_target = _sonarr_style_target_video_for_torrent_source(
        source_video,
        torrent,
        config,
        logger,
        candidate_mappings,
        episode=episode,
        season_hint=effective_season_hint,
        pending_entries=pending_entries,
        diagnostics=target_diagnostics,
        cancelled=cancelled,
    )
    if sonarr_style_target is not None:
        return sonarr_style_target
    return None


_SONARR_STYLE_AUTOMATCH_MIN_SCORE = 1200
_SONARR_STYLE_AUTOMATCH_MIN_MARGIN = 180
_SONARR_STYLE_SERIES_SCOPE_MIN_SCORE = 760
_SONARR_STYLE_SERIES_SCOPE_MIN_MARGIN = 180


def _trusted_unscoped_series_mappings(
    config: AppConfig,
    series_mappings: list[dict[str, object]],
    source_aliases: set[str],
    *,
    torrent_name: str,
    episode: int,
    logger: logging.Logger | None,
    diagnostics: list[dict[str, Any]] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    """Resolve one series before looking up an episode.

    Recovered qBittorrent entries may no longer have their original pending
    Bangumi row.  They must never fall back to searching every video with the
    same episode number.  This resolver first proves one local series from
    title aliases and only then allows episode lookup inside that scope.
    """

    if not source_aliases or (cancelled is not None and cancelled()):
        return []

    grouped: dict[str, dict[str, Any]] = {}

    def add_scope(root: Path, mapping: dict[str, object] | None = None) -> None:
        if cancelled is not None and cancelled():
            return
        root = _sonarr_style_series_scope_root(root)
        if not root.exists() or not root.is_dir():
            return
        resolved = _safe_resolve(root)
        key = str(resolved).casefold()
        entry = grouped.setdefault(
            key,
            {
                "root": resolved,
                "mappings": [],
                "raw_aliases": [resolved.name],
            },
        )
        if mapping is None:
            return
        entry["mappings"].append(mapping)
        for field_name in ("title", "bangumi_title", "name"):
            value = mapping.get(field_name)
            if value:
                entry["raw_aliases"].append(str(value))
        match_values = mapping.get("match")
        if isinstance(match_values, list):
            entry["raw_aliases"].extend(str(value) for value in match_values if str(value).strip())

    for mapping in series_mappings:
        raw_path = mapping.get("path")
        if raw_path:
            add_scope(Path(str(raw_path)), mapping)

    input_path = Path(str(getattr(config, "input_path", "") or ""))
    if input_path.exists() and input_path.is_dir():
        try:
            children = sorted(input_path.iterdir(), key=lambda item: str(item).casefold())
        except OSError:
            children = []
        for child in children:
            if cancelled is not None and cancelled():
                return []
            try:
                is_directory = child.is_dir()
            except OSError:
                is_directory = False
            if is_directory:
                add_scope(child)

    scored: list[_SonarrStyleSeriesScopeCandidate] = []
    for entry in grouped.values():
        aliases = _sonarr_style_aliases_from_raw_values(list(entry["raw_aliases"]))
        score, reason, best_ratio = _sonarr_style_alias_score(aliases, source_aliases)
        if score <= 0:
            continue
        scored.append(
            _SonarrStyleSeriesScopeCandidate(
                root=entry["root"],
                mappings=tuple(entry["mappings"]),
                score=score,
                reason=reason,
                best_ratio=best_ratio,
            )
        )
    scored.sort(
        key=lambda item: (item.score, item.best_ratio, str(item.root).casefold()),
        reverse=True,
    )

    top = scored[0] if scored else None
    runner_up = scored[1] if len(scored) > 1 else None
    margin = top.score - runner_up.score if top is not None and runner_up is not None else (top.score if top else 0)
    trusted = bool(
        top is not None
        and top.score >= _SONARR_STYLE_SERIES_SCOPE_MIN_SCORE
        and (runner_up is None or margin >= _SONARR_STYLE_SERIES_SCOPE_MIN_MARGIN)
    )
    if not trusted:
        reason = "ambiguous_target_candidates" if runner_up is not None else "low_confidence_target_candidate"
        _append_series_scope_target_diagnostics(
            diagnostics,
            scored,
            config=config,
            episode=episode,
            reason=reason,
            cancelled=cancelled,
        )
        if logger is not None:
            logger.warning(
                "Unscoped completed torrent series identity is not unique; keep for review. torrent=%s top=%s score=%s runner_up=%s margin=%s",
                torrent_name,
                str(top.root) if top else "",
                top.score if top else 0,
                runner_up.score if runner_up else 0,
                margin,
            )
        return []

    assert top is not None
    if logger is not None:
        logger.info(
            "Resolved unscoped completed torrent to one series before episode lookup. torrent=%s series=%s score=%s margin=%s reason=%s",
            torrent_name,
            top.root,
            top.score,
            margin,
            top.reason,
        )
    if top.mappings:
        return [dict(mapping) for mapping in top.mappings]
    return [
        {
            "path": str(top.root),
            "match": [top.root.name],
            "title": top.root.name,
            "identity_source": "unscoped_series_title",
            "match_confidence": min(1.0, top.score / 900.0),
            "locked": False,
        }
    ]


def _sonarr_style_series_scope_root(path: Path) -> Path:
    if re.fullmatch(r"Season\s+0*\d+|Specials", path.name, re.IGNORECASE) and path.parent != path:
        return path.parent
    return path


def _append_series_scope_target_diagnostics(
    diagnostics: list[dict[str, Any]] | None,
    scored: list[_SonarrStyleSeriesScopeCandidate],
    *,
    config: AppConfig,
    episode: int,
    reason: str,
    cancelled: Callable[[], bool] | None,
) -> None:
    if diagnostics is None:
        return
    runner_up_score = scored[1].score if len(scored) > 1 else 0
    for item in scored[:3]:
        if cancelled is not None and cancelled():
            return
        candidates = _scan_state_episode_candidates(config, [item.root], episode, cancelled=cancelled)
        if not candidates:
            candidates = [
                video
                for video in _find_video_files(item.root, config.video_extensions, cancelled=cancelled)
                if extract_episode_number(video.name) == episode
            ]
        representative = _unique_paths(candidates)[0] if candidates else None
        diagnostics.append(
            {
                "path": str(representative or item.root),
                "scope_path": str(item.root),
                "score": 1000 + item.score,
                "runner_up_score": 1000 + runner_up_score if runner_up_score else 0,
                "margin": item.score - runner_up_score if runner_up_score else item.score,
                "reason": reason,
                "reasons": ["episode", f"series_scope:{item.reason}"],
                "best_ratio": round(float(item.best_ratio or 0), 3),
                "season": _season_number_for_video(representative) if representative is not None else None,
                "has_chinese_subtitle": (
                    _target_has_required_chinese_subtitles(representative)
                    if representative is not None
                    else False
                ),
            }
        )


def _sonarr_style_select_target_from_candidates(
    candidates: list[Path],
    source_aliases: set[str],
    *,
    torrent_name: str,
    source_video: Path | None,
    logger: logging.Logger | None,
    context: str,
    season_hint: int | None,
    pending_entries: list[dict[str, Any]] | None,
    diagnostics: list[dict[str, Any]] | None = None,
    trusted_single_candidate: bool = True,
    trusted_mapping_scope: bool = False,
) -> list[Path]:
    unique = _unique_paths(candidates)
    if not unique:
        return []
    unique = _filter_candidates_by_release_year_contract(
        unique,
        torrent_name=torrent_name,
        pending_entries=pending_entries,
        diagnostics=diagnostics,
        logger=logger,
        context=context,
    )
    if not unique:
        return []

    scored = sorted(
        (
            _sonarr_style_score_candidate(
                candidate,
                source_aliases,
                season_hint=season_hint,
                pending_entries=pending_entries,
            )
            for candidate in unique
        ),
        key=lambda item: (item.score, item.best_ratio, str(item.candidate).casefold()),
        reverse=True,
    )
    if trusted_mapping_scope:
        scored = [
            _SonarrStyleScoredCandidate(
                item.candidate,
                item.score,
                (*item.reasons, "series_mapping:pending"),
                item.best_ratio,
            )
            for item in scored
        ]
    _append_sonarr_style_target_diagnostics(diagnostics, scored, context=context)

    if len(scored) == 1:
        if _sonarr_style_scored_candidate_has_identity_evidence(scored[0]) and (
            scored[0].score >= _SONARR_STYLE_AUTOMATCH_MIN_SCORE or trusted_single_candidate
        ):
            return [scored[0].candidate]
        if logger is not None:
            logger.warning(
                "Single %s candidate did not meet Sonarr-style score threshold; skip torrent=%s target=%s score=%s reasons=%s",
                context,
                torrent_name,
                scored[0].candidate,
                scored[0].score,
                ",".join(scored[0].reasons),
            )
        _append_sonarr_style_target_diagnostics(
            diagnostics,
            scored,
            context=context,
            reason="low_confidence_target_candidate",
        )
        return []

    top = scored[0]
    runner_up = scored[1]
    margin = top.score - runner_up.score
    if (
        top.score >= _SONARR_STYLE_AUTOMATCH_MIN_SCORE
        and margin >= _SONARR_STYLE_AUTOMATCH_MIN_MARGIN
        and _sonarr_style_scored_candidate_has_identity_evidence(top)
    ):
        if logger is not None:
            logger.info(
                "Resolved %s using Sonarr-style scoring. torrent=%s target=%s score=%s runner_up=%s margin=%s reasons=%s",
                context,
                torrent_name,
                top.candidate,
                top.score,
                runner_up.score,
                margin,
                ",".join(top.reasons),
            )
        return [top.candidate]

    selected = _disambiguate_target_videos(
        unique,
        torrent_name=torrent_name,
        source_video=source_video,
        logger=logger,
        context=context,
        season_hint=season_hint,
    )
    if selected:
        if len(selected) != 1:
            if logger is not None:
                logger.warning(
                    "Ambiguous %s fallback returned multiple candidates; skip torrent=%s candidates=%s",
                    context,
                    torrent_name,
                    selected,
                )
            _append_sonarr_style_target_diagnostics(
                diagnostics,
                scored,
                context=context,
                reason="ambiguous_target_candidates",
            )
            return []
        selected_set = {str(path).casefold() for path in selected}
        selected_scored = [item for item in scored if str(item.candidate).casefold() in selected_set]
        if selected_scored and _sonarr_style_scored_candidate_has_identity_evidence(selected_scored[0]):
            _append_sonarr_style_target_diagnostics(
                diagnostics,
                selected_scored,
                context=f"{context} fallback",
                reason="season_or_missing_subtitle_fallback",
            )
            return selected
        _append_sonarr_style_target_diagnostics(
            diagnostics,
            selected_scored or scored,
            context=f"{context} fallback",
            reason="missing_title_evidence",
        )
        return []

    if logger is not None:
        logger.warning(
            "Ambiguous %s after Sonarr-style scoring; skip torrent=%s top_score=%s runner_up=%s margin=%s candidates=%s",
            context,
            torrent_name,
            top.score,
            runner_up.score,
            margin,
            [item.candidate for item in scored[:6]],
        )
    _append_sonarr_style_target_diagnostics(
        diagnostics,
        scored,
        context=context,
        reason="ambiguous_target_candidates",
    )
    return []


def _sonarr_style_score_candidate(
    candidate: Path,
    source_aliases: set[str],
    *,
    season_hint: int | None,
    pending_entries: list[dict[str, Any]] | None,
) -> _SonarrStyleScoredCandidate:
    score = 1000
    reasons: list[str] = ["episode"]
    candidate_aliases = _sonarr_style_candidate_title_aliases(candidate)
    alias_score, alias_reason, best_ratio = _sonarr_style_alias_score(candidate_aliases, source_aliases)
    if alias_score:
        score += alias_score
        reasons.append(alias_reason)

    candidate_season = _season_number_for_video(candidate)
    if season_hint is not None:
        if candidate_season == season_hint:
            score += 520
            reasons.append(f"season:{season_hint}")
        elif candidate_season is None:
            score += 40
            reasons.append("season_unknown")
        else:
            score -= 700
            reasons.append(f"season_mismatch:{candidate_season}!={season_hint}")

    release_years = _pending_release_years(pending_entries)
    if release_years and candidate_season is not None:
        nfo_years = _nfo_years(candidate.parent / "season.nfo")
        if release_years.intersection(nfo_years):
            score += 280
            reasons.append("release_year")

    token = _distinct_completed_source_token(source_aliases)
    if token and _candidate_matches_distinct_completed_source_token(candidate, token):
        score += 320
        reasons.append(f"sequel_token:{token}")

    if not _target_has_required_chinese_subtitles(candidate):
        score += 40
        reasons.append("subtitle_missing")

    return _SonarrStyleScoredCandidate(candidate, score, tuple(reasons), best_ratio)


def _sonarr_style_scored_candidate_has_identity_evidence(item: _SonarrStyleScoredCandidate) -> bool:
    return any(
        reason.startswith(("title_", "sequel_token:", "series_mapping:"))
        for reason in item.reasons
    )


def _sonarr_style_alias_score(candidate_aliases: set[str], source_aliases: set[str]) -> tuple[int, str, float]:
    best_score = 0
    best_reason = ""
    best_ratio = 0.0
    for candidate_alias in candidate_aliases:
        for source_alias in source_aliases:
            if not candidate_alias or not source_alias:
                continue
            if candidate_alias == source_alias:
                return 900, "title_exact", 1.0
            if _sonarr_style_aliases_match(candidate_alias, source_alias):
                best_score, best_reason, best_ratio = max(
                    (best_score, best_reason, best_ratio),
                    (760, "title_release_noise", 0.98),
                    key=lambda item: (item[0], item[2]),
                )
                continue
            if candidate_alias in source_alias or source_alias in candidate_alias:
                ratio = min(len(candidate_alias), len(source_alias)) / max(len(candidate_alias), len(source_alias))
                score = 520 + int(160 * ratio)
                if score > best_score:
                    best_score, best_reason, best_ratio = score, "title_contains", ratio
                continue
            ratio = SequenceMatcher(None, candidate_alias, source_alias).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
            if ratio >= 0.86:
                score = int(500 * ratio)
                if score > best_score:
                    best_score, best_reason = score, "title_fuzzy"
    return best_score, best_reason, best_ratio


def _append_sonarr_style_target_diagnostics(
    diagnostics: list[dict[str, Any]] | None,
    scored: list[_SonarrStyleScoredCandidate],
    *,
    context: str,
    reason: str = "",
) -> None:
    if diagnostics is None or not scored:
        return
    runner_up_score = scored[1].score if len(scored) > 1 else 0
    for item in scored[:10]:
        diagnostics.append(
            {
                "path": str(item.candidate),
                "score": item.score,
                "runner_up_score": runner_up_score,
                "margin": item.score - runner_up_score if runner_up_score else item.score,
                "reason": reason or context,
                "reasons": list(item.reasons),
                "best_ratio": round(float(item.best_ratio or 0), 3),
                "season": _season_number_for_video(item.candidate),
                "has_chinese_subtitle": _target_has_required_chinese_subtitles(item.candidate),
            }
        )


def _sonarr_style_target_video_for_torrent_source(
    source_video: Path,
    torrent: QBitTorrent,
    config: AppConfig,
    logger: logging.Logger,
    series_mappings: list[dict[str, object]],
    *,
    episode: int,
    season_hint: int | None,
    pending_entries: list[dict[str, Any]] | None,
    diagnostics: list[dict[str, Any]] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path | None:
    if cancelled is not None and cancelled():
        return None
    source_aliases = _sonarr_style_source_title_aliases(source_video, torrent, pending_entries)
    if not source_aliases:
        return None

    candidates = _sonarr_style_library_episode_candidates(
        config,
        series_mappings,
        episode,
        cancelled=cancelled,
    )
    if cancelled is not None and cancelled():
        return None
    if not candidates:
        return None

    if season_hint is None:
        season_hint = _season_hint_from_pending_release_year_for_candidates(
            candidates,
            pending_entries,
        )

    selected = _sonarr_style_select_target_from_candidates(
        candidates,
        source_aliases,
        torrent_name=torrent.name,
        source_video=source_video,
        logger=logger,
        context="completed torrent Sonarr-style target",
        season_hint=season_hint,
        pending_entries=pending_entries,
        diagnostics=diagnostics,
        trusted_single_candidate=False,
    )
    if not selected:
        return None

    logger.info(
        "Resolved completed torrent target using Sonarr-style match. source=%s target=%s episode=%s",
        source_video,
        selected[0],
        episode,
    )
    return selected[0]


def _sonarr_style_completed_torrent_has_local_episode(
    torrent: QBitTorrent,
    config: AppConfig,
    series_mappings: list[dict[str, object]],
    *,
    episodes: set[int],
    season_hint: int | None,
    pending_entries: list[dict[str, Any]] | None,
) -> bool:
    source_aliases = _sonarr_style_pending_title_aliases(torrent, pending_entries)
    if not source_aliases:
        return False

    for episode in sorted(episodes):
        candidates = _sonarr_style_library_episode_candidates(config, series_mappings, episode)
        source_season_hint = _season_hint_from_completed_source_aliases(source_aliases)
        if source_season_hint is not None:
            candidates = _filter_candidates_by_strict_season_hint(candidates, source_season_hint)
        candidates = _filter_candidates_for_distinct_completed_source(
            candidates,
            source_aliases,
            logger=None,
            context="completed torrent Sonarr-style local episode",
        )
        if not candidates:
            return False
        episode_season_hint = source_season_hint or season_hint or _season_hint_from_pending_release_year_for_candidates(
            candidates,
            pending_entries,
        )
        if not _sonarr_style_select_target_from_candidates(
            candidates,
            source_aliases,
            torrent_name=torrent.name,
            source_video=None,
            logger=None,
            context="completed torrent Sonarr-style local episode",
            season_hint=episode_season_hint,
            pending_entries=pending_entries,
            trusted_single_candidate=False,
        ):
            return False
    return True


def _sonarr_style_pending_entry_matches_completed_torrent(
    entry: dict[str, Any],
    torrent: QBitTorrent,
) -> bool:
    entry_aliases = _sonarr_style_entry_title_aliases(entry)
    torrent_aliases = _sonarr_style_aliases_from_raw_values([torrent.name])
    if not entry_aliases or not torrent_aliases:
        return False

    for entry_alias in entry_aliases:
        for torrent_alias in torrent_aliases:
            if _sonarr_style_aliases_match(entry_alias, torrent_alias):
                return True
    return False


def _sonarr_style_library_episode_candidates(
    config: AppConfig,
    series_mappings: list[dict[str, object]],
    episode: int,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[Path]:
    roots = _sonarr_style_library_roots(config, series_mappings)
    if not roots:
        return []

    candidates = _scan_state_episode_candidates(config, roots, episode, cancelled=cancelled)
    if not candidates:
        for root in roots:
            if cancelled is not None and cancelled():
                return []
            if not root.exists():
                continue
            for video in _find_video_files(root, config.video_extensions, cancelled=cancelled):
                if extract_episode_number(video.name) == episode:
                    candidates.append(video)
    return _unique_paths(candidates)


def _scan_state_episode_candidates(
    config: AppConfig,
    roots: list[Path],
    episode: int,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[Path]:
    state_path = scan_state_path(config)
    if not state_path.exists():
        return []

    extension_set = {extension.lower() for extension in config.video_extensions}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(state_path)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(video_scan_cache)").fetchall()}
        if "episode" in columns:
            rows = conn.execute(
                "SELECT path FROM video_scan_cache WHERE episode = ? ORDER BY path",
                (int(episode),),
            ).fetchall()
            if not rows:
                rows = conn.execute("SELECT path FROM video_scan_cache ORDER BY path").fetchall()
        else:
            rows = conn.execute("SELECT path FROM video_scan_cache ORDER BY path").fetchall()
    except sqlite3.Error:
        return []
    finally:
        if conn is not None:
            conn.close()

    candidates: list[Path] = []
    for row in rows:
        if cancelled is not None and cancelled():
            return []
        path = Path(str(row[0]))
        if path.suffix.lower() not in extension_set:
            continue
        if extract_episode_number(path.name) != episode:
            continue
        if not any(_path_is_relative_to(path, root) for root in roots):
            continue
        if not path.exists():
            continue
        candidates.append(path)
    return candidates


def _sonarr_style_library_roots(
    config: AppConfig,
    series_mappings: list[dict[str, object]],
) -> list[Path]:
    roots: list[Path] = []
    for mapping in series_mappings:
        raw_path = mapping.get("path")
        if not raw_path:
            continue
        path = Path(str(raw_path))
        roots.append(path)

    # Unscoped legacy matching may search the library root.  Once a pending
    # bangumi has supplied scoped mappings, never add its parent (usually
    # ``/anime``) back into the search or an episode number can fan out across
    # the entire library.
    if not roots:
        input_path = getattr(config, "input_path", None)
        if input_path:
            roots.append(Path(str(input_path)))

    return _unique_paths(roots)


def _sonarr_style_source_title_aliases(
    source_video: Path,
    torrent: QBitTorrent,
    pending_entries: list[dict[str, Any]] | None,
) -> set[str]:
    raw_values: list[str] = [
        source_video.parent.name,
        source_video.stem,
        torrent.name,
    ]
    for entry in pending_entries or []:
        raw_values.extend(_sonarr_style_entry_raw_title_values(entry))

    return _sonarr_style_aliases_from_raw_values(raw_values)


def _sonarr_style_pending_title_aliases(
    torrent: QBitTorrent,
    pending_entries: list[dict[str, Any]] | None,
) -> set[str]:
    raw_values = [torrent.name]
    for entry in pending_entries or []:
        raw_values.extend(_sonarr_style_entry_raw_title_values(entry))
    return _sonarr_style_aliases_from_raw_values(raw_values)


def _season_hint_from_completed_source_aliases(source_aliases: set[str]) -> int | None:
    """Infer only source-title season hints that are safe enough for auto-import.

    This deliberately stays narrow.  A wrong season match imports subtitles into the
    wrong library episode, which is worse than leaving the item in target-missing
    for review.
    """
    if _source_aliases_contain(source_aliases, "zombielandsagarevenge"):
        return 2
    if _source_aliases_contain(source_aliases, "d4djallmix"):
        return 2
    if _source_aliases_contain(source_aliases, "toarukagakunorailgunt", "acertainscientificrailgunt"):
        return 3
    if _source_aliases_contain(source_aliases, "toarukagakunorailguns", "acertainscientificrailguns"):
        return 2
    if _source_aliases_contain(source_aliases, "secondseason", "2ndseason"):
        return 2
    if _source_aliases_contain(source_aliases, "thirdseason", "3rdseason"):
        return 3
    if _source_aliases_contain(
        source_aliases,
        "kaguyasamawakokurasetais2",
        "kaguyasamawakokurasetaiii",
        "kaguyasamas2",
        "kaguyasamaloveiswars2",
        "kaguyasamaloveiswarii",
    ):
        return 2
    if _source_aliases_contain(source_aliases, "bleachsennenkessenhen", "bleachsennenkessenhensoukokutan"):
        return 17
    if _source_aliases_contain(source_aliases, "monogatariseriesoffmonsterseason"):
        return 1
    if _source_aliases_contain(source_aliases, "genkoku", "genjitsushugiyuushanooukokusaisenki"):
        return 1
    return None


def _source_aliases_contain(source_aliases: set[str], *tokens: str) -> bool:
    return any(token in alias for alias in source_aliases for token in tokens)


def _filter_candidates_by_strict_season_hint(candidates: list[Path], season_hint: int) -> list[Path]:
    filtered: list[Path] = []
    for candidate in candidates:
        season = _season_number_for_video(candidate)
        if season is None or season == season_hint:
            filtered.append(candidate)
    return filtered


def _filter_candidates_for_distinct_completed_source(
    candidates: list[Path],
    source_aliases: set[str],
    *,
    logger: logging.Logger | None,
    context: str,
) -> list[Path]:
    token = _distinct_completed_source_token(source_aliases)
    if token is None or not candidates:
        return candidates

    filtered = [
        candidate
        for candidate in candidates
        if _candidate_matches_distinct_completed_source_token(candidate, token)
    ]
    if filtered:
        return filtered
    if logger is not None:
        logger.warning(
            "Completed subtitle source looks like a distinct sequel, but no library candidate contains the sequel token; skip unsafe match. context=%s token=%s candidates=%s",
            context,
            token,
            candidates,
        )
    return []


def _distinct_completed_source_token(source_aliases: set[str]) -> str | None:
    for alias in source_aliases:
        if "higurashinonakukoronigou" in alias or "higurashigou" in alias:
            return "gou"
        if "higurashinonakukoronisotsu" in alias or "higurashisotsu" in alias:
            return "sotsu"
    return None


def _candidate_matches_distinct_completed_source_token(candidate: Path, token: str) -> bool:
    for alias in _sonarr_style_candidate_title_aliases(candidate):
        if "higurashi" in alias and token in alias:
            return True
    return False


def _sonarr_style_entry_title_aliases(entry: dict[str, Any]) -> set[str]:
    return _sonarr_style_aliases_from_raw_values(_sonarr_style_entry_raw_title_values(entry))


def _sonarr_style_entry_raw_title_values(entry: dict[str, Any]) -> list[str]:
    raw_values: list[str] = []
    for key in ("title", "deferred_title", "last_failed_title"):
        value = entry.get(key)
        if value:
            raw_values.append(str(value))
    return raw_values


def _sonarr_style_aliases_from_raw_values(raw_values: list[str]) -> set[str]:
    aliases: set[str] = set()
    for value in raw_values:
        variants = _sonarr_style_title_variants(value)
        variants.extend(_sonarr_style_known_title_alias_variants(value))
        for variant in variants:
            normalized = normalize_match_text(variant)
            if _sonarr_style_alias_is_specific(normalized):
                aliases.add(normalized)
    return aliases


def _sonarr_style_known_title_alias_variants(value: str) -> list[str]:
    normalized = normalize_match_text(str(value or ""))
    aliases: list[str] = []
    if "tenseishitaraslimedattaken" in normalized or "tensura" in normalized:
        aliases.extend(["That Time I Got Reincarnated as a Slime", "Tensei Shitara Slime Datta Ken"])
    if "thattimeigotreincarnatedasaslime" in normalized:
        aliases.extend(["Tensei Shitara Slime Datta Ken", "TenSura"])
    if "tsuetotsuruginowistoria" in normalized:
        aliases.extend(["Wistoria Wand and Sword", "Wistoria - Wand and Sword"])
    if "wistoriawandandsword" in normalized:
        aliases.append("Tsue to Tsurugi no Wistoria")
    if "zombielandsagarevenge" in normalized:
        aliases.extend(["Zombie Land Saga", "Zombie Land Saga Season 2"])
    if "kaguyasamawakokurasetai" in normalized:
        aliases.extend(["Kaguya-sama Love Is War", "Kaguya-sama wa Kokurasetai"])
    if "kaguyasamaloveiswar" in normalized:
        aliases.extend(["Kaguya-sama wa Kokurasetai", "Kaguya-sama Love Is War"])
    if "monogatariseriesoffmonsterseason" in normalized:
        aliases.append("Monogatari Series - Off & Monster Season")
    if "bleachsennenkessenhen" in normalized or "bleachsennenkessenhensoukokutan" in normalized:
        aliases.append("Bleach")
    if "genkoku" in normalized or "genjitsushugiyuushanooukokusaisenki" in normalized:
        aliases.extend(["How a Realist Hero Rebuilt the Kingdom", "Genjitsu Shugi Yuusha no Oukoku Saikenki"])
    if "howarealistherorebuiltthekingdom" in normalized:
        aliases.extend(["Genkoku", "Genjitsu Shugi Yuusha no Oukoku Saikenki"])
    if "rwbyhyousetsuteikoku" in normalized:
        aliases.extend(["RWBY - Ice Queendom", "RWBY Hyousetsu Teikoku"])
    if "rwbyicequeendom" in normalized:
        aliases.append("RWBY Hyousetsu Teikoku")
    if "higurashinonakukoroni" in normalized:
        aliases.append("When They Cry Higurashi")
    if "whentheycryhigurashi" in normalized:
        aliases.append("Higurashi no Naku Koro ni")
    if "aishiterugamewoowarasetai" in normalized:
        aliases.append("I Want to End This Love Game")
    if "iwanttoendthislovegame" in normalized:
        aliases.append("Aishiteru Game wo Owarasetai")
    if "arumajogashinumade" in normalized:
        aliases.append("Once Upon a Witch's Death")
    if "onceuponawitchsdeath" in normalized:
        aliases.append("Aru Majo ga Shinu Made")
    if "toarukagakunorailgunt" in normalized:
        aliases.extend(["A Certain Scientific Railgun T", "A Certain Scientific Railgun"])
    if "toarukagakunorailguns" in normalized:
        aliases.extend(["A Certain Scientific Railgun S", "A Certain Scientific Railgun"])
    if "toarukagakunorailgun" in normalized:
        aliases.append("A Certain Scientific Railgun")
    if "acertainscientificrailgunt" in normalized:
        aliases.extend(["Toaru Kagaku no Railgun T", "A Certain Scientific Railgun"])
    if "acertainscientificrailguns" in normalized:
        aliases.extend(["Toaru Kagaku no Railgun S", "A Certain Scientific Railgun"])
    if "acertainscientificrailgun" in normalized:
        aliases.append("Toaru Kagaku no Railgun")
    if "tenseishitaradainanaoujidattanode" in normalized or "dainanaouji" in normalized:
        aliases.append("I Was Reincarnated as the 7th Prince so I Can Take My Time Perfecting My Magical Ability")
    if "iwasreincarnatedasthe7thprince" in normalized or "seventhprince" in normalized:
        aliases.append("Tensei shitara Dainana Ouji Datta node Kimama ni Majutsu wo Kiwamemasu")
    return aliases


def _sonarr_style_candidate_matches_source(candidate: Path, source_aliases: set[str]) -> bool:
    score, _reason, _ratio = _sonarr_style_alias_score(
        _sonarr_style_candidate_title_aliases(candidate),
        source_aliases,
    )
    return score > 0


def _sonarr_style_candidate_title_aliases(candidate: Path) -> set[str]:
    series_dir = _sonarr_style_series_dir_for_video(candidate)
    raw_values = [
        series_dir.name,
        candidate.stem,
    ]
    for nfo_path in (series_dir / "tvshow.nfo", series_dir / "series.nfo"):
        raw_values.extend(_nfo_title_aliases(nfo_path))
    raw_values.extend(_nfo_title_aliases(candidate.parent / "season.nfo"))

    aliases: set[str] = set()
    for value in raw_values:
        variants = _sonarr_style_title_variants(value)
        variants.extend(_sonarr_style_known_title_alias_variants(value))
        for variant in variants:
            normalized = normalize_match_text(variant)
            if _sonarr_style_alias_is_specific(normalized):
                aliases.add(normalized)
    return aliases


def _sonarr_style_series_dir_for_video(candidate: Path) -> Path:
    parent = candidate.parent
    if _season_number_from_text(parent.name) is not None and parent.parent != parent:
        return parent.parent
    return parent


def _sonarr_style_title_variants(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    variants = {text}
    without_leading_group = re.sub(r"^\s*(?:\[[^\]]+\]|\([^)]+\)|【[^】]+】)\s*", "", text).strip()
    if without_leading_group:
        variants.add(without_leading_group)
    without_brackets = re.sub(r"\[[^\]]+\]|\([^)]+\)|【[^】]+】", " ", text).strip()
    if without_brackets:
        variants.add(without_brackets)

    cleaned: set[str] = set()
    for variant in variants:
        pieces = [
            variant,
            re.sub(r"(?i)\bS\d{1,2}E\d{1,3}\b.*$", "", variant).strip(),
            re.sub(r"\s+-\s*\d{1,3}\b.*$", "", variant).strip(),
            re.sub(r"\[\s*\d{1,3}(?:\s*[-~]\s*\d{1,3})?\s*\].*$", "", variant).strip(),
        ]
        for piece in pieces:
            piece = re.sub(r"(?i)\b(?:1080p|720p|2160p|web[-_. ]?dl|webrip|bdrip|hdtv|hevc|x264|x265|aac|flac|mkv|mp4)\b", " ", piece)
            piece = re.sub(r"\s+", " ", piece).strip(" -_.")
            if piece:
                cleaned.add(piece)
    return sorted(cleaned)


def _sonarr_style_aliases_match(candidate_alias: str, source_alias: str) -> bool:
    if not candidate_alias or not source_alias:
        return False
    if candidate_alias == source_alias:
        return True
    return (
        _sonarr_style_alias_contains_only_release_noise(candidate_alias, source_alias)
        or _sonarr_style_alias_contains_only_release_noise(source_alias, candidate_alias)
    )


def _sonarr_style_alias_contains_only_release_noise(container_alias: str, contained_alias: str) -> bool:
    if not contained_alias or contained_alias not in container_alias:
        return False
    before, after = container_alias.split(contained_alias, 1)
    return _sonarr_style_alias_remainder_is_release_noise(before) and _sonarr_style_alias_remainder_is_release_noise(after)


def _sonarr_style_alias_remainder_is_release_noise(value: str) -> bool:
    if not value:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.isdigit():
        return True
    if re.fullmatch(r"(?:v\d+|10bit|8bit|x26[45]|h26[45]|hevc|avc|aac|flac|mkv|mp4|webdl|webrip|bdrip|bluray|hdtv|cht|chs|sc|tc|big5|gb|zh|jpn|jap|ass|srt)+", stripped):
        return True
    return False


def _sonarr_style_alias_is_specific(value: str) -> bool:
    if not value:
        return False
    if any(_is_cjk_or_kana_char(char) for char in value):
        return len(value) >= 3
    return len(value) >= 6


def _is_cjk_or_kana_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _nfo_title_aliases(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    aliases: list[str] = []
    for tag in ("title", "originaltitle", "sorttitle", "showtitle"):
        aliases.extend(
            re.sub(r"\s+", " ", match).strip()
            for match in re.findall(rf"(?is)<{tag}>\s*(.*?)\s*</{tag}>", text)
        )
    return [alias for alias in aliases if alias]


def _season_hint_from_mapping_titles_for_candidates(
    candidates: list[Path],
    mappings: list[dict[str, object]],
) -> int | None:
    mapping_aliases = _sonarr_style_mapping_title_aliases(mappings)
    if not candidates or not mapping_aliases:
        return None

    matched_seasons: set[int] = set()
    for candidate in candidates:
        season = _season_number_for_video(candidate)
        if season is None or season <= 0:
            continue
        season_aliases = _sonarr_style_season_title_aliases(candidate)
        if not season_aliases:
            continue
        score, _reason, _ratio = _sonarr_style_alias_score(season_aliases, mapping_aliases)
        if score > 0:
            matched_seasons.add(season)
    if len(matched_seasons) != 1:
        return None
    return next(iter(matched_seasons))


def _sonarr_style_mapping_title_aliases(mappings: list[dict[str, object]]) -> set[str]:
    raw_values: list[str] = []
    for mapping in mappings:
        for key in ("title", "bangumi_title", "name"):
            value = mapping.get(key)
            if value:
                raw_values.append(str(value))
        aliases = mapping.get("match")
        if isinstance(aliases, list):
            raw_values.extend(str(alias) for alias in aliases if str(alias).strip())
    return _sonarr_style_aliases_from_raw_values(raw_values)


def _sonarr_style_season_title_aliases(candidate: Path) -> set[str]:
    return _sonarr_style_aliases_from_raw_values(_nfo_title_aliases(candidate.parent / "season.nfo"))


def _season_hint_from_pending_release_year_for_candidates(
    candidates: list[Path],
    pending_entries: list[dict[str, Any]] | None,
) -> int | None:
    release_years = _pending_release_years(pending_entries)
    if not release_years:
        return None

    matched_seasons: set[int] = set()
    for candidate in candidates:
        season = _season_number_for_video(candidate)
        if season is None or season <= 0:
            continue
        nfo_years = _nfo_years(candidate.parent / "season.nfo")
        if release_years.intersection(nfo_years):
            matched_seasons.add(season)
    if len(matched_seasons) != 1:
        return None
    return next(iter(matched_seasons))


def _pending_release_years(pending_entries: list[dict[str, Any]] | None) -> set[int]:
    return {
        datetime.fromtimestamp(timestamp, timezone.utc).year
        for entry in pending_entries or []
        if (timestamp := _pending_entry_source_publication_timestamp(entry)) > 0
    }


def _pending_entry_source_publication_timestamp(entry: dict[str, Any]) -> float:
    """Return a real upstream publication time, never a qB recovery time."""

    return _pending_entry_source_publication(entry)[0]


def _pending_entry_source_publication(entry: dict[str, Any]) -> tuple[float, str]:
    """Return ``(timestamp, precision)`` for a trustworthy source date."""

    if not isinstance(entry, dict):
        return (0.0, "")
    source = next(
        (
            _source_tag(entry.get(key))
            for key in (
                "source",
                "last_completed_source",
                "last_failed_source",
                "last_superseded_source",
            )
            if str(entry.get(key) or "").strip()
        ),
        "",
    )
    source_url = str(
        entry.get("torrent_url")
        or entry.get("last_completed_torrent_url")
        or entry.get("last_failed_torrent_url")
        or entry.get("last_superseded_torrent_url")
        or ""
    ).strip().casefold()
    active_publication = (0.0, "")
    if source != "qbit-recovered" and (source or (source_url and not source_url.startswith("qbit://"))):
        exact = _pending_timestamp(entry.get("pub_date"))
        inferred = _torrent_url_date_timestamp(source_url) if exact <= 0 else 0.0
        active_publication = (exact or inferred, "time" if exact > 0 else "date" if inferred > 0 else "")

    deferred_source = _source_tag(entry.get("deferred_source"))
    deferred_url = str(entry.get("deferred_torrent_url") or "").strip().casefold()
    deferred_publication = (0.0, "")
    if deferred_source != "qbit-recovered" and (
        deferred_source or (deferred_url and not deferred_url.startswith("qbit://"))
    ):
        exact = _pending_timestamp(entry.get("deferred_pub_date"))
        inferred = _torrent_url_date_timestamp(deferred_url) if exact <= 0 else 0.0
        deferred_publication = (exact or inferred, "time" if exact > 0 else "date" if inferred > 0 else "")
    return max(
        active_publication,
        deferred_publication,
        key=lambda item: (item[0], item[1] == "time"),
    )


def _torrent_url_date_timestamp(value: object) -> float:
    """Recover the source day encoded by Mikan-style torrent URLs.

    This is a date-only fallback.  It is intentionally ignored for qbit://
    recovery URLs and is never derived from arbitrary digits inside a hash.
    """

    raw = str(value or "").strip()
    if not raw or raw.casefold().startswith("qbit://"):
        return 0.0
    try:
        segments = [segment for segment in urlparse(raw).path.split("/") if segment]
    except ValueError:
        return 0.0
    for segment in segments:
        if not re.fullmatch(r"(?:19|20)\d{6}", segment):
            continue
        try:
            return datetime.strptime(segment, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def _release_year_contract_is_reliable(
    torrent_name: str,
    pending_entries: list[dict[str, Any]] | None,
) -> bool:
    raw_values = [str(torrent_name or "")]
    for entry in pending_entries or []:
        raw_values.extend(_sonarr_style_entry_raw_title_values(entry))
    normalized = normalize_match_text(" ".join(raw_values))
    if any(token in normalized for token in ("bdrip", "bluray", "bdremux", "remux", "dvdrip", "dvd")):
        return False
    return any(token in normalized for token in ("webrip", "webdl", "hdtv"))


def _filter_candidates_by_release_year_contract(
    candidates: list[Path],
    *,
    torrent_name: str,
    pending_entries: list[dict[str, Any]] | None,
    diagnostics: list[dict[str, Any]] | None,
    logger: logging.Logger | None,
    context: str,
) -> list[Path]:
    """Reject an all-known, impossible year set for near-air releases.

    WEB/HDTV episode releases normally belong to the season premiere year or
    the following calendar year.  Blu-ray and archive releases are excluded
    because they can legitimately appear years later.  Unknown NFO years are
    retained, preserving the conservative no-guess contract.
    """

    release_years = _pending_release_years(pending_entries)
    if not candidates or not release_years or not _release_year_contract_is_reliable(
        torrent_name,
        pending_entries,
    ):
        return candidates

    compatible: list[Path] = []
    unknown: list[Path] = []
    candidate_years: set[int] = set()
    for candidate in candidates:
        years = _nfo_years(candidate.parent / "season.nfo")
        if not years:
            unknown.append(candidate)
            continue
        candidate_years.update(years)
        if any(0 <= release_year - candidate_year <= 1 for release_year in release_years for candidate_year in years):
            compatible.append(candidate)

    if unknown:
        return candidates
    if compatible:
        return compatible

    detail = {
        "path": "",
        "score": 0,
        "reason": "release_year_conflict",
        "reasons": ["release_year_conflict"],
        "release_years": sorted(release_years),
        "candidate_years": sorted(candidate_years),
    }
    if diagnostics is not None and detail not in diagnostics:
        diagnostics.append(detail)
    if logger is not None:
        logger.warning(
            "Completed near-air subtitle source year does not match any known candidate season; refusing unsafe mapping. context=%s torrent=%s release_years=%s candidate_years=%s",
            context,
            torrent_name,
            sorted(release_years),
            sorted(candidate_years),
        )
    return []


def _nfo_years(path: Path) -> set[int]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    return {
        int(match)
        for match in re.findall(r"(?i)<(?:year|premiered|releasedate)>\s*(\d{4})", text)
    }


def _series_mappings_for_pending_entries(
    series_mappings: list[dict[str, object]],
    pending_entries: list[dict[str, Any]] | None,
) -> list[dict[str, object]]:
    if not pending_entries:
        return []
    bangumi_ids = {
        bangumi_id
        for entry in pending_entries
        if (bangumi_id := _coerce_int(entry.get("bangumi_id"))) is not None
    }
    if not bangumi_ids:
        return []
    return [
        mapping
        for mapping in series_mappings
        if _coerce_int(mapping.get("bangumi_id")) in bangumi_ids
    ]


def _verified_series_mappings_for_pending_entries(
    series_mappings: list[dict[str, object]],
    pending_entries: list[dict[str, Any]] | None,
    torrent: QBitTorrent,
) -> list[dict[str, object]]:
    """Require Bangumi scope plus independently verifiable local-series evidence."""

    return [
        mapping
        for mapping in _series_mappings_for_pending_entries(series_mappings, pending_entries)
        if bool(mapping.get("manual_locked"))
        or _series_mapping_path_matches_identity(mapping)
        or (
            _torrent_matches_series(torrent.name, mapping)
            and _series_mapping_declared_season_matches_path(mapping)
        )
    ]


def _series_mapping_path_matches_identity(mapping: dict[str, object]) -> bool:
    path = Path(str(mapping.get("path") or ""))
    local_name = path.parent.name if re.fullmatch(r"Season\s+\d+|Specials", path.name, re.IGNORECASE) else path.name
    normalized_local = normalize_match_text(local_name)
    if len(normalized_local) < 4:
        return False
    tokens = mapping.get("match") or []
    if not isinstance(tokens, list):
        return False
    for token in tokens:
        normalized_token = normalize_match_text(str(token or ""))
        if len(normalized_token) < 4:
            continue
        if normalized_token in normalized_local or normalized_local in normalized_token:
            return True
    return False


def _series_mapping_declared_season_matches_path(mapping: dict[str, object]) -> bool:
    """Accept alternate-language aliases only when an explicit season agrees with the path."""

    declared = _coerce_int(mapping.get("season") or mapping.get("season_number"))
    if declared is None or declared < 0:
        return False
    path = Path(str(mapping.get("path") or ""))
    if path.name.casefold() == "specials":
        return declared == 0
    match = re.fullmatch(r"Season\s+0*(\d+)", path.name, re.IGNORECASE)
    return match is not None and int(match.group(1)) == declared


def _scoped_completed_source_season_unverified(
    *, source_video: Path | None, torrent_name: str, mappings: list[dict[str, object]],
) -> bool:
    scoped = [mapping for mapping in mappings if mapping.get("identity_source") == "cached_season_nfo"]
    if not scoped:
        return False
    expected = {_coerce_int(mapping.get("season")) for mapping in scoped}
    titles = [torrent_name]
    if source_video is not None:
        titles.extend((source_video.name, source_video.parent.name))
    seasons = {season for title in titles if (season := release_season_number(title)) is not None}
    return len(expected) != 1 or None in expected or seasons != expected


def _explicit_completed_season_conflict(
    *,
    source_video: Path | None,
    torrent_name: str,
    mappings: list[dict[str, object]],
    pending_entries: list[dict[str, Any]] | None,
) -> bool:
    """An explicit release/locked scope contradiction cannot be a scoring hint."""
    values = [torrent_name]
    if source_video is not None:
        values.extend((source_video.name, source_video.parent.name))
    for entry in pending_entries or []:
        values.extend(str(entry.get(key) or "") for key in ("title", "deferred_title"))
    seasons = {season for value in values if (season := _season_number_from_release_title(value)) is not None}
    for mapping in mappings:
        for key in ("season", "season_number"):
            season = _coerce_int(mapping.get(key))
            if season is not None and 0 <= season <= 99:
                seasons.add(season)
    return len(seasons) > 1


def _season_hint_for_completed_target(
    *,
    source_video: Path | None,
    torrent_name: str,
    mappings: list[dict[str, object]],
    pending_entries: list[dict[str, Any]] | None,
) -> int | None:
    candidates: list[int] = []
    for value in (
        _season_number_from_source_release(source_video),
        _season_number_from_release_title(torrent_name),
    ):
        if value is not None:
            candidates.append(value)
    for entry in pending_entries or []:
        for key in ("title", "deferred_title", "last_failed_title"):
            value = _season_number_from_release_title(str(entry.get(key) or ""))
            if value is not None:
                candidates.append(value)
    for mapping in mappings:
        explicit_season = _coerce_int(mapping.get("season") or mapping.get("season_number"))
        if explicit_season is not None and explicit_season >= 0:
            candidates.append(explicit_season)
        mapping_values = [mapping.get(key) for key in ("title", "bangumi_title", "name", "path")]
        aliases = mapping.get("match")
        if isinstance(aliases, list):
            mapping_values.extend(aliases)
        for raw_value in mapping_values:
            value = _season_number_from_text(str(raw_value or ""))
            if value is not None:
                candidates.append(value)
    release_year_season = _season_hint_from_pending_release_year(mappings, pending_entries)
    if release_year_season is not None:
        candidates.append(release_year_season)
    if not candidates:
        return None
    first = candidates[0]
    return first if all(candidate == first for candidate in candidates) else None


def _season_number_from_source_release(source_video: Path | None) -> int | None:
    if source_video is None:
        return None
    return _season_number_from_release_title(source_video.name) or _season_number_from_release_title(source_video.parent.name)


def _season_number_from_release_title(value: str) -> int | None:
    """Parse only explicit season markers from release/source titles.

    Library paths can use broad patterns like ``Season 1``.  Completed subtitle
    source names are less structured: titles such as "Off & Monster Season [06]"
    contain the word "Season" followed by the episode number, so broad parsing
    would falsely infer season 6.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    patterns = (
        r"(?i)\bS(?:eason)?\s*0*(\d{1,2})\s*E\d{1,3}\b",
        r"(?i)\bS\s*0*(\d{1,2})\s*[-_. ]+\s*\d{1,3}\b",
        r"(?i)\bSeason\s*0*(\d{1,2})\s*[-_. ]+\s*\d{1,3}\b",
        r"(?i)\b0*(\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"\u7b2c\s*0*(\d{1,2})\s*[\u5b63\u671f]",
        r"第\s*([0-9一二三四五六七八九十]+)\s*[季期]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        number = _parse_season_number(match.group(1))
        if number is not None:
            return number
    return None


def _season_hint_from_pending_release_year(
    mappings: list[dict[str, object]],
    pending_entries: list[dict[str, Any]] | None,
) -> int | None:
    release_years = _pending_release_years(pending_entries)
    if not release_years:
        return None

    matched_seasons: set[int] = set()
    for mapping in mappings:
        root = Path(str(mapping.get("path") or ""))
        try:
            season_dirs = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            continue
        for season_dir in season_dirs:
            season = _season_number_from_text(season_dir.name)
            if season is None or season <= 0:
                continue
            nfo_path = season_dir / "season.nfo"
            try:
                nfo_text = nfo_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            nfo_years = {
                int(match)
                for match in re.findall(r"(?i)<(?:year|premiered|releasedate)>\s*(\d{4})", nfo_text)
            }
            if release_years.intersection(nfo_years):
                matched_seasons.add(season)
    if len(matched_seasons) != 1:
        return None
    return next(iter(matched_seasons))


def _disambiguate_target_videos(
    candidates: list[Path],
    *,
    torrent_name: str,
    source_video: Path | None,
    logger: logging.Logger | None,
    context: str,
    season_hint: int | None = None,
) -> list[Path]:
    unique = sorted(set(candidates), key=lambda item: str(item).casefold())
    if len(unique) <= 1:
        return unique

    candidate_seasons = {
        season
        for candidate in unique
        if (season := _season_number_for_video(candidate)) is not None
    }
    if len(candidate_seasons) <= 1:
        return unique

    if season_hint is None:
        season_hint = _season_number_from_source_release(source_video)
    if season_hint is None:
        season_hint = _season_number_from_release_title(torrent_name)
    if season_hint is None:
        incomplete = [
            candidate
            for candidate in unique
            if not _target_has_required_chinese_subtitles(candidate)
        ]
        if len(incomplete) == 1:
            if logger is not None:
                logger.info(
                    "Resolved ambiguous %s using the only candidate still missing required Chinese subtitles: %s",
                    context,
                    incomplete[0],
                )
            return incomplete
        if logger is not None:
            logger.warning(
                "Ambiguous %s across seasons; skip torrent=%s candidates=%s",
                context,
                torrent_name,
                unique,
            )
        return []

    filtered = [
        candidate
        for candidate in unique
        if _season_number_for_video(candidate) == season_hint
    ]
    if filtered:
        return filtered

    incomplete = [
        candidate
        for candidate in unique
        if not _target_has_required_chinese_subtitles(candidate)
    ]
    if len(incomplete) == 1:
        if logger is not None:
            logger.info(
                "Ignored unusable season hint for %s and selected the only candidate still missing required Chinese subtitles. torrent=%s season=%s target=%s",
                context,
                torrent_name,
                season_hint,
                incomplete[0],
            )
        return incomplete

    if logger is not None:
        logger.warning(
            "No %s candidate matches season hint; skip torrent=%s season=%s candidates=%s",
            context,
            torrent_name,
            season_hint,
            unique,
        )
    return []


def _season_number_for_video(path: Path | None) -> int | None:
    if path is None:
        return None
    return _season_number_from_text(path.name) or _season_number_from_text(str(path.parent))


def _season_number_from_text(value: str) -> int | None:
    patterns = (
        r"(?i)\bS(?:eason)?\s*0*(\d{1,2})\s*E\d{1,3}\b",
        r"(?i)\bS(?:eason)?\s*0*(\d{1,2})(?=$|[\s._\-\]\)])",
        r"(?i)(?:^|[\s._\-\]\)])0*([1-9]|1\d|20)\s*-\s*\d{1,3}(?=$|[\s._\-\[\]\)])",
        r"(?i)\bSeason\s*0*(\d{1,2})\b",
        r"(?i)\b0*(\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"\u7b2c\s*0*(\d{1,2})\s*[\u5b63\u671f]",
        r"第\s*([0-9一二三四五六七八九十]+)\s*[季期]",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        number = _parse_season_number(match.group(1))
        if number is not None:
            return number

    roman = re.search(r"(?i)(?<![A-Z0-9])(II|III|IV|V|VI|VII|VIII|IX|X)(?![A-Z0-9])", value)
    if roman:
        return {
            "II": 2,
            "III": 3,
            "IV": 4,
            "V": 5,
            "VI": 6,
            "VII": 7,
            "VIII": 8,
            "IX": 9,
            "X": 10,
        }.get(roman.group(1).upper())
    return None


def _parse_season_number(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        pass

    digits = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2:
        return 10 + digits.get(value[1], 0)
    if value.endswith("十") and len(value) == 2:
        return digits.get(value[0], 0) * 10
    if len(value) == 3 and value[1] == "十":
        return digits.get(value[0], 0) * 10 + digits.get(value[2], 0)
    if len(value) == 1:
        return digits.get(value)
    return None


def _is_inside_series_paths(path: Path, series_mappings: list[dict[str, object]]) -> bool:
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path

    for mapping in series_mappings:
        root = Path(str(mapping["path"]))
        try:
            resolved_path.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _torrent_matches_series(torrent_name: str, mapping: dict[str, object]) -> bool:
    return mapping_matches_torrent(torrent_name, mapping)


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path
