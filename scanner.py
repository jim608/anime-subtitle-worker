from __future__ import annotations

from pathlib import Path
from collections import Counter
import json
import logging
import os
import sqlite3
import stat
import threading
import time

from ai_failure_markers import recent_ai_failure
from acceptance_queue_lane import acceptance_run_id_for_video, load_acceptance_queue_lane
from audio import (
    manifest_confirms_no_non_commentary_japanese_audio,
    probe_audio_stream_manifest,
)
from config import AppConfig
from io_pressure import io_pressure_busy
from processing_provenance import processing_config_signature
from safe_files import atomic_write_text
from source_decision import (
    SOURCE_DECISION_CONTRACT,
    USE_ZH_TW,
    select_subtitle_source,
)
from scan_state import (
    ScanStateStore,
    ai_inventory_root_signature,
    is_scan_state_corruption_error,
    is_scan_state_transient_error,
    scan_config_signature,
    scan_state_path,
    video_scan_signature,
)
from scanner_state_recovery import request_scanner_state_recovery
from subtitle_paths import ai_finished_subtitle_mtime, has_ai_finished_subtitle, has_finished_subtitle
from video_policy import is_standalone_theme_video
from subtitle_extract import (
    SubtitleExtractError,
    extract_available_subtitles,
    normalize_sidecar_subtitles,
    remove_ai_srt_outputs,
)


SCAN_STATE_COMMIT_EVERY_WRITTEN_VIDEO = 1
SCANNER_PRE_ATTEMPT_EXCLUSION_CODES = {
    "local_chinese": "local_chinese_subtitle_present_before_attempt",
    "embedded_chinese": "embedded_chinese_subtitle_present_before_attempt",
    "missing": "media_missing_before_attempt",
    "excluded": "standalone_theme_policy",
    "unsupported_media": "unsupported_media_before_attempt",
}


class InventoryWalkError(RuntimeError):
    """A media-root walk that cannot support a complete inventory attestation."""


def scan_videos(config: AppConfig, logger: logging.Logger | None = None) -> list[Path]:
    return VideoScanner(config, logger or logging.getLogger(__name__)).scan()


def _is_scan_state_lock_error(error: BaseException) -> bool:
    return is_scan_state_transient_error(error)


class VideoScanner:
    def __init__(self, config: AppConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._state: ScanStateStore | None = None
        # Bump the cache identity without mutating the shared scan-state schema.
        # Old ``local_chinese`` rows treated zh-CN as terminal and must never be
        # reused under the source-priority contract.
        self._config_signature = f"{scan_config_signature(config)}:{SOURCE_DECISION_CONTRACT}"
        if bool(getattr(config, "source_analyzer_enabled", False)):
            # Re-evaluate legacy text-only completion on the next normal scoped
            # visit, without clearing state or changing Pipeline policy identity.
            self._config_signature += ":m2-official-admission-v1"
        self._processing_policy_revision = processing_config_signature(config)
        self._cache_enabled = bool(getattr(config, "scanner_cache_enabled", True))
        self._queue_enabled = self._cache_enabled and bool(getattr(config, "scanner_queue_enabled", True))
        self._last_full_scan_monotonic: float | None = None
        self._last_scan_high_water_ns: int | None = None
        self._state_pending_writes = 0
        self._queue_batch_added_at: float | None = None
        self._queue_selection_cycle = 0
        self._last_ledger_backfill_warning_signature = ""
        self._last_ledger_backfill_warning_at = 0.0
        self._reconcile_iterator = None
        self._reconcile_pending_item: tuple[Path, int] | None = None
        self._reconcile_cycle_complete = True
        self._reconcile_cycle_started_at = 0.0
        self._reconcile_max_seen_mtime_ns = 0
        self._reconcile_batches_total = 0
        self._reconcile_paths_total = 0
        self._reconcile_cycles_total = 0
        self._last_reconcile_batch_paths = 0
        self._inventory_epoch_id = ""
        self._inventory_eligibility_bound = 0.0
        self._inventory_instrumented_at = 0.0
        self._inventory_root_signature = ai_inventory_root_signature(
            self.config.input_path,
            self.config.video_extensions,
        )
        self.last_database_error = ""
        self.last_database_error_code = ""
        self.last_database_error_operation = ""
        self.last_database_error_at = 0.0

    @property
    def reconcile_cycle_complete(self) -> bool:
        return bool(self._reconcile_cycle_complete)

    @property
    def reconcile_scan_counters(self) -> dict[str, int]:
        """Return path-free counters for scheduler diagnostics and tests."""

        return {
            "batches": int(self._reconcile_batches_total),
            "paths": int(self._reconcile_paths_total),
            "cycles": int(self._reconcile_cycles_total),
            "last_batch_paths": int(self._last_reconcile_batch_paths),
        }

    def _clear_last_database_error(self) -> None:
        self.last_database_error = ""
        self.last_database_error_code = ""
        self.last_database_error_operation = ""
        self.last_database_error_at = 0.0

    def _record_last_database_error(self, error: BaseException, operation: str) -> None:
        message = str(error or "")
        normalized = message.casefold()
        if "disk i/o error" in normalized:
            code = "scanner_database_disk_io"
        elif any(marker in normalized for marker in ("database is locked", "database table is locked", "database is busy")):
            code = "scanner_database_busy"
        elif is_scan_state_corruption_error(error):
            code = "scanner_database_corrupt"
        else:
            code = "scanner_database_error"
        self.last_database_error = message
        self.last_database_error_code = code
        self.last_database_error_operation = str(operation or "")
        self.last_database_error_at = time.time()

    def scan(
        self,
        max_candidates: int | None = None,
        *,
        _corruption_recovery_attempted: bool = False,
    ) -> list[Path]:
        self._clear_last_database_error()
        videos: list[Path] = []
        stats: Counter[str] = Counter()
        database_error: sqlite3.DatabaseError | None = None
        operation_error: BaseException | None = None
        self._queue_batch_added_at = time.time()
        try:
            for path in self.scan_all():
                status, cache_source, probe_failed = self._classify(path)
                self._update_ai_queue(path, status)
                self._commit_state_if_needed()
                stats[f"{cache_source}_{status}"] += 1
                stats[status] += 1
                if probe_failed:
                    stats["probe_failed"] += 1
                if status == "needs_ai":
                    videos.append(path)
            if self._queue_enabled:
                videos = self._queued_ai_candidates(max_candidates=max_candidates)
        except sqlite3.DatabaseError as exc:
            database_error = exc
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            self._queue_batch_added_at = None
            try:
                self._close_state(
                    commit=database_error is None and operation_error is None
                )
            except sqlite3.DatabaseError as exc:
                if database_error is None:
                    database_error = exc

        if database_error is not None:
            self._record_last_database_error(database_error, "scan")
            if is_scan_state_corruption_error(database_error) and not _corruption_recovery_attempted:
                state_path = scan_state_path(self.config)
                request = request_scanner_state_recovery(
                    state_path.parent,
                    database_error,
                    operation="scan",
                )
                self.logger.critical(
                    "Scanner state database is corrupt; fail-closed stopped-service recovery requested. path=%s recovery_id=%s source_deployment_id=%s error=%s",
                    state_path,
                    request.get("recovery_id") or "-",
                    request.get("source_deployment_id") or "-",
                    database_error,
                )
                raise database_error
            if _is_scan_state_lock_error(database_error):
                self.logger.warning(
                    "Scanner state database is busy; skip scan result this cycle to avoid worker restart: %s",
                    database_error,
                )
                return videos
            raise database_error

        total = sum(
            stats[key]
            for key in (
                "needs_ai",
                "finished",
                "local_chinese",
                "embedded_chinese",
                "unsupported_media",
                "failure_cooldown",
                "missing",
            )
        )
        self.logger.info(
            "Scan classification summary total=%s candidates=%s cached=%s fresh=%s finished=%s local_chinese=%s embedded_chinese=%s unsupported_media=%s failure_cooldown=%s missing=%s probe_failed=%s",
            total,
            len(videos),
            sum(value for key, value in stats.items() if key.startswith("cached_")),
            sum(value for key, value in stats.items() if key.startswith("fresh_")),
            stats["finished"],
            stats["local_chinese"],
            stats["embedded_chinese"],
            stats["unsupported_media"],
            stats["failure_cooldown"],
            stats["missing"],
            stats["probe_failed"],
        )

        return videos

    def refresh_queue(
        self,
        *,
        force_full: bool = False,
        reconcile_batch: bool = False,
        _corruption_recovery_attempted: bool = False,
    ) -> int:
        """Refresh scan/cache state without waiting to drain the AI queue."""
        self._clear_last_database_error()
        stats: Counter[str] = Counter()
        scan_error: Exception | None = None
        self._queue_batch_added_at = time.time()
        try:
            if reconcile_batch:
                self._backfill_active_queue_obligations()
                self._refresh_reconcile_inventory_batch(stats, force_restart=force_full)
            else:
                for path in self.scan_all(force_full=force_full):
                    status, cache_source, probe_failed = self._classify(path)
                    self._update_ai_queue(path, status)
                    self._commit_state_if_needed()
                    stats[f"{cache_source}_{status}"] += 1
                    stats[status] += 1
                    if probe_failed:
                        stats["probe_failed"] += 1
        except Exception as exc:
            scan_error = exc
            if reconcile_batch and not _is_scan_state_lock_error(exc):
                self._fail_reconcile_inventory_epoch(exc)
        finally:
            self._queue_batch_added_at = None
            try:
                self._close_state(commit=scan_error is None)
            except sqlite3.DatabaseError as exc:
                if scan_error is None:
                    scan_error = exc

        if isinstance(scan_error, sqlite3.DatabaseError):
            self._record_last_database_error(scan_error, "refresh_queue")
            if is_scan_state_corruption_error(scan_error) and not _corruption_recovery_attempted:
                state_path = scan_state_path(self.config)
                request = request_scanner_state_recovery(
                    state_path.parent,
                    scan_error,
                    operation="refresh_queue",
                )
                self.logger.critical(
                    "Scanner state database is corrupt; fail-closed stopped-service recovery requested. path=%s recovery_id=%s source_deployment_id=%s error=%s",
                    state_path,
                    request.get("recovery_id") or "-",
                    request.get("source_deployment_id") or "-",
                    scan_error,
                )
                raise scan_error
            if _is_scan_state_lock_error(scan_error):
                self.logger.warning(
                    "Scanner state database is busy; skip background queue refresh this cycle: %s",
                    scan_error,
                )
                return 0
            raise scan_error
        if scan_error is not None:
            raise scan_error

        total = sum(
            stats[key]
            for key in (
                "needs_ai",
                "finished",
                "local_chinese",
                "embedded_chinese",
                "unsupported_media",
                "failure_cooldown",
                "missing",
            )
        )
        self.logger.info(
            "Background scan queue refresh complete. total=%s queued_candidates=%s missing=%s cached=%s fresh=%s",
            total,
            stats["needs_ai"],
            stats["missing"],
            sum(value for key, value in stats.items() if key.startswith("cached_")),
            sum(value for key, value in stats.items() if key.startswith("fresh_")),
        )
        return total

    def _start_reconcile_inventory_epoch(self, *, force_restart: bool = False) -> None:
        if self._reconcile_iterator is not None and not force_restart:
            return
        if force_restart and self._inventory_epoch_id:
            self._fail_reconcile_inventory_epoch(
                InventoryWalkError("inventory reconciliation was explicitly restarted")
            )
        input_path = Path(self.config.input_path)
        state = self._state_store()
        if state is None:
            raise RuntimeError("durable inventory reconciliation requires scanner state")
        epoch = None
        if not force_restart:
            epoch = state.resume_ai_inventory_epoch(
                policy_revision=self._processing_policy_revision,
                root_signature=self._inventory_root_signature,
            )
        if epoch is None:
            epoch = state.begin_ai_inventory_epoch(
                policy_revision=self._processing_policy_revision,
                root_signature=self._inventory_root_signature,
            )
        state.commit()
        self._state_pending_writes = 0
        self._inventory_epoch_id = str(epoch["epoch_id"])
        self._inventory_eligibility_bound = float(epoch["eligibility_bound"])
        self._inventory_instrumented_at = float(epoch["instrumented_at"])
        # The legacy path cursor advances before classification commits and is
        # not globally ordered across os.walk. It may optimize ordinary scans,
        # but it can never be trusted by a proof epoch.
        self._reconcile_cursor_path().unlink(missing_ok=True)
        if not input_path.exists() or not input_path.is_dir():
            raise InventoryWalkError(f"media inventory root is unavailable: {input_path}")
        self._reconcile_iterator = iter(
            self._walk_video_files(
                input_path,
                set(self.config.video_extensions),
                fail_on_error=True,
            )
        )
        self._reconcile_cycle_complete = False
        self._reconcile_cycle_started_at = time.monotonic()
        self._reconcile_max_seen_mtime_ns = 0
        if bool(epoch.get("resumed")):
            self.logger.info(
                "Scanner inventory proof epoch resumed. epoch_id=%s prior_started_at=%s "
                "batch_size=%s budget_seconds=%s",
                self._inventory_epoch_id,
                float(epoch.get("resumed_from_started_at") or 0),
                int(getattr(self.config, "scanner_reconcile_batch_size", 1000) or 1000),
                int(getattr(self.config, "scanner_reconcile_budget_seconds", 60) or 60),
            )
        else:
            self.logger.info(
                "Scanner inventory proof epoch started. epoch_id=%s batch_size=%s budget_seconds=%s",
                self._inventory_epoch_id,
                int(getattr(self.config, "scanner_reconcile_batch_size", 1000) or 1000),
                int(getattr(self.config, "scanner_reconcile_budget_seconds", 60) or 60),
            )

    def _refresh_reconcile_inventory_batch(
        self,
        stats: Counter[str],
        *,
        force_restart: bool = False,
    ) -> None:
        self._start_reconcile_inventory_epoch(force_restart=force_restart)
        self._reconcile_batches_total += 1
        batch_number = self._reconcile_batches_total
        limit = max(1, int(getattr(self.config, "scanner_reconcile_batch_size", 1000) or 1000))
        budget = max(1.0, float(getattr(self.config, "scanner_reconcile_budget_seconds", 60) or 60))
        deadline = time.monotonic() + budget
        processed = 0
        while processed < limit and time.monotonic() < deadline:
            if self._reconcile_pending_item is None:
                try:
                    self._reconcile_pending_item = next(self._reconcile_iterator)
                except StopIteration:
                    # Keep the exhausted iterator and running epoch intact until
                    # finalization commits. A transient database lock can then
                    # retry finalization without silently starting a new epoch.
                    self._finalize_reconcile_inventory_epoch()
                    self._reconcile_iterator = None
                    self._reconcile_cycle_complete = True
                    self._reconcile_cycles_total += 1
                    elapsed = max(0.0, time.monotonic() - self._reconcile_cycle_started_at)
                    self.logger.info(
                        "Scanner inventory proof epoch exhausted. reason=cycle_complete "
                        "batch_number=%s final_batch=%s paths_total=%s cycles_total=%s elapsed_seconds=%.1f",
                        batch_number,
                        processed,
                        self._reconcile_paths_total,
                        self._reconcile_cycles_total,
                        elapsed,
                    )
                    break
            path, priority_time_ns = self._reconcile_pending_item
            self._process_reconcile_inventory_path(
                path,
                stats,
                deadline_monotonic=deadline,
            )
            # Clear the pending item only after the complete per-path savepoint
            # and transaction have committed. A transient lock retries exactly
            # the same media path instead of advancing the proof walk.
            self._reconcile_pending_item = None
            self._reconcile_max_seen_mtime_ns = max(
                self._reconcile_max_seen_mtime_ns,
                int(priority_time_ns or 0),
            )
            processed += 1
            self._reconcile_paths_total += 1
        self._last_reconcile_batch_paths = processed
        if not self._reconcile_cycle_complete:
            self.logger.info(
                "Scanner inventory reconciliation batch committed. reason=bounded_continuation "
                "batch_number=%s videos=%s paths_total=%s batch_limit=%s budget_seconds=%s",
                batch_number,
                processed,
                self._reconcile_paths_total,
                limit,
                int(budget),
            )

    def _process_reconcile_inventory_path(
        self,
        video: Path,
        stats: Counter[str],
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        state = self._state_store()
        if state is None or not self._inventory_epoch_id:
            raise RuntimeError("inventory path processing requires a running proof epoch")
        try:
            before = video.stat()
            status, cache_source, probe_failed = self._classify(
                video,
                deadline_monotonic=deadline_monotonic,
            )
            # Classification may perform slow media I/O and may update only the
            # disposable scan cache. Commit that cache separately, then acquire
            # the proof writer immediately before the atomic queue/ledger facts.
            # This avoids holding SQLite's single WAL writer across ffprobe while
            # also preventing a read-to-write snapshot upgrade race with the
            # active AI child's frequent stage heartbeats.
            if state.in_transaction:
                state.commit()
                self._state_pending_writes = 0
            state.begin_ai_inventory_path(self._inventory_epoch_id)
            self._update_ai_queue(
                video,
                status,
                eligible_at=self._inventory_eligibility_bound,
            )
            after = video.stat()
            if (
                int(before.st_size) != int(after.st_size)
                or int(before.st_mtime_ns) != int(after.st_mtime_ns)
            ):
                raise InventoryWalkError(f"media changed during inventory classification: {video}")
            ai_output_detected = bool(
                status == "finished" and has_ai_finished_subtitle(video, self.config)
            )
            ai_output_mtime = (
                float(ai_finished_subtitle_mtime(video, self.config))
                if ai_output_detected
                else 0.0
            )
            if status in {"needs_ai", "failure_cooldown"}:
                disposition = "delivery_required"
            elif ai_output_detected:
                disposition = (
                    "legacy_preinstrumented_ai"
                    if 0 < ai_output_mtime < self._inventory_instrumented_at
                    else "delivered"
                )
            elif status == "missing":
                disposition = "missing"
            else:
                disposition = "policy_excluded"
            state.record_ai_inventory_observation(
                self._inventory_epoch_id,
                video,
                media_size=int(after.st_size),
                media_mtime_ns=int(after.st_mtime_ns),
                policy_revision=self._processing_policy_revision,
                classification=status,
                disposition=disposition,
                ai_output_detected=ai_output_detected,
                ai_output_mtime=ai_output_mtime,
            )
            state.commit_ai_inventory_path()
            state.commit()
            self._state_pending_writes = 0
        except Exception:
            try:
                if state.in_transaction:
                    state.rollback_ai_inventory_path()
            except sqlite3.Error:
                pass
            if state.in_transaction:
                state.rollback()
            self._state_pending_writes = 0
            raise
        stats[f"{cache_source}_{status}"] += 1
        stats[status] += 1
        if probe_failed:
            stats["probe_failed"] += 1

    def _finalize_reconcile_inventory_epoch(self) -> None:
        if not self._inventory_epoch_id:
            raise RuntimeError("inventory finalization requires an epoch id")
        state = self._state_store()
        if state is None:
            raise RuntimeError("inventory finalization requires scanner state")
        result = state.finalize_ai_inventory_epoch(self._inventory_epoch_id)
        state.commit()
        self._state_pending_writes = 0
        self._update_scan_high_water(self._reconcile_max_seen_mtime_ns)
        self.logger.info(
            "Scanner inventory proof epoch completed. epoch_id=%s observed=%s required=%s tracked=%s untracked=%s legacy=%s coverage_complete=%s",
            result["epoch_id"],
            result["observed_count"],
            result["delivery_required_count"],
            result["tracked_count"],
            result["untracked_count"],
            result["legacy_preinstrumented_ai_count"],
            result["coverage_complete"],
        )
        self._inventory_epoch_id = ""
        self._inventory_eligibility_bound = 0.0
        self._inventory_instrumented_at = 0.0

    def _fail_reconcile_inventory_epoch(self, error: BaseException) -> None:
        epoch_id = self._inventory_epoch_id
        if epoch_id:
            try:
                state = self._state_store()
                if state is not None:
                    if state.in_transaction:
                        state.rollback()
                    code = (
                        "walk_error"
                        if isinstance(error, (InventoryWalkError, OSError))
                        else "database_error"
                        if isinstance(error, sqlite3.Error)
                        else "classification_error"
                    )
                    state.fail_ai_inventory_epoch(
                        epoch_id,
                        failure_code=code,
                        detail=str(error),
                    )
                    state.commit()
                    self._state_pending_writes = 0
            except Exception as failure_error:
                self.logger.error(
                    "Unable to persist failed inventory epoch. epoch_id=%s error=%s original=%s",
                    epoch_id,
                    failure_error,
                    error,
                )
        self._reset_reconcile_inventory_state()

    def _reset_reconcile_inventory_state(self) -> None:
        self._reconcile_iterator = None
        self._reconcile_pending_item = None
        self._reconcile_cycle_complete = True
        self._inventory_epoch_id = ""
        self._inventory_eligibility_bound = 0.0
        self._inventory_instrumented_at = 0.0

    def _next_reconcile_batch(self, *, force_restart: bool = False) -> list[Path]:
        input_path = self.config.input_path
        if not input_path.exists():
            self.logger.warning("Input path does not exist: %s", input_path)
            self._reconcile_cycle_complete = True
            return []
        if force_restart:
            self._reconcile_iterator = None
            self._reconcile_pending_item = None
            self._reconcile_cursor_path().unlink(missing_ok=True)
        if self._reconcile_iterator is None:
            resume_after = self._load_reconcile_cursor()
            source_iterator = self._walk_video_files(input_path, set(self.config.video_extensions))
            if resume_after:
                self._reconcile_iterator = (
                    item for item in source_iterator if str(item[0]).casefold() > resume_after
                )
            else:
                self._reconcile_iterator = iter(source_iterator)
            self._reconcile_cycle_complete = False
            self._reconcile_cycle_started_at = time.monotonic()
            self._reconcile_max_seen_mtime_ns = 0
            self.logger.info(
                "Scanner reconciliation cycle started. batch_size=%s budget_seconds=%s",
                int(getattr(self.config, "scanner_reconcile_batch_size", 1000) or 1000),
                int(getattr(self.config, "scanner_reconcile_budget_seconds", 60) or 60),
            )

        limit = max(1, int(getattr(self.config, "scanner_reconcile_batch_size", 1000) or 1000))
        budget = max(1.0, float(getattr(self.config, "scanner_reconcile_budget_seconds", 60) or 60))
        deadline = time.monotonic() + budget
        batch: list[Path] = []
        while len(batch) < limit and time.monotonic() < deadline:
            try:
                path, priority_time_ns = next(self._reconcile_iterator)
            except StopIteration:
                self._reconcile_iterator = None
                self._reconcile_cycle_complete = True
                self._reconcile_cursor_path().unlink(missing_ok=True)
                self._update_scan_high_water(self._reconcile_max_seen_mtime_ns)
                elapsed = max(0.0, time.monotonic() - self._reconcile_cycle_started_at)
                self.logger.info(
                    "Scanner reconciliation cycle complete. final_batch=%s elapsed_seconds=%.1f",
                    len(batch),
                    elapsed,
                )
                break
            self._reconcile_max_seen_mtime_ns = max(
                self._reconcile_max_seen_mtime_ns,
                int(priority_time_ns or 0),
            )
            batch.append(path)
        if batch and not self._reconcile_cycle_complete:
            self._write_reconcile_cursor(batch[-1])
        if not self._reconcile_cycle_complete:
            self.logger.info(
                "Scanner reconciliation batch ready. videos=%s batch_limit=%s budget_seconds=%s",
                len(batch),
                limit,
                int(budget),
            )
        return batch

    def _reconcile_cursor_path(self) -> Path:
        return Path(self.config.work_path) / "scanner_reconcile_cursor.json"

    def _load_reconcile_cursor(self) -> str:
        path = self._reconcile_cursor_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            updated_at = float(payload.get("updated_at") or 0) if isinstance(payload, dict) else 0
            cursor = str(payload.get("last_path") or "").casefold() if isinstance(payload, dict) else ""
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return ""
        if not cursor or updated_at <= 0 or time.time() - updated_at > 24 * 3600:
            path.unlink(missing_ok=True)
            return ""
        self.logger.info("Resuming scanner reconciliation after path=%s", cursor)
        return cursor

    def _write_reconcile_cursor(self, path: Path) -> None:
        atomic_write_text(
            self._reconcile_cursor_path(),
            json.dumps(
                {"last_path": str(path), "updated_at": time.time()},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def queued_candidates(
        self,
        max_candidates: int | None = None,
        *,
        exact_target: Path | None = None,
        _corruption_recovery_attempted: bool = False,
        _transient_recovery_attempted: bool = False,
    ) -> list[Path]:
        """Return existing queue work without scanning the media library first."""
        self._clear_last_database_error()
        database_error: sqlite3.DatabaseError | None = None
        operation_error: BaseException | None = None
        videos: list[Path] = []
        try:
            videos = self._queued_ai_candidates(
                max_candidates=max_candidates,
                exact_target=exact_target,
            )
        except sqlite3.DatabaseError as exc:
            database_error = exc
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            try:
                self._close_state(
                    commit=database_error is None and operation_error is None
                )
            except sqlite3.DatabaseError as exc:
                if database_error is None:
                    database_error = exc

        if database_error is not None:
            self._record_last_database_error(database_error, "queued_candidates")
            if is_scan_state_corruption_error(database_error) and not _corruption_recovery_attempted:
                state_path = scan_state_path(self.config)
                request = request_scanner_state_recovery(
                    state_path.parent,
                    database_error,
                    operation="queued_candidates",
                )
                self.logger.critical(
                    "Scanner queue database is corrupt; fail-closed stopped-service recovery requested. path=%s recovery_id=%s source_deployment_id=%s error=%s",
                    state_path,
                    request.get("recovery_id") or "-",
                    request.get("source_deployment_id") or "-",
                    database_error,
                )
                raise database_error
            if _is_scan_state_lock_error(database_error):
                if (
                    "disk i/o error" in str(database_error).casefold()
                    and not _transient_recovery_attempted
                ):
                    self.logger.warning(
                        "Scanner queue connection returned disk I/O error; reopening once before delaying AI work: %s",
                        database_error,
                    )
                    time.sleep(0.25)
                    return self.queued_candidates(
                        max_candidates=max_candidates,
                        exact_target=exact_target,
                        _corruption_recovery_attempted=_corruption_recovery_attempted,
                        _transient_recovery_attempted=True,
                    )
                self.logger.warning(
                    "Scanner state database is busy; skip AI queue drain this cycle to avoid worker restart: %s",
                    database_error,
                )
                return []
            raise database_error
        return videos

    def _classify(
        self,
        video: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[str, str, bool]:
        if not video.exists():
            self.logger.info("Skip missing video during scan refresh: %s", video)
            return "missing", "fresh", False

        force_ai, bypass_failure_cooldown = self._queue_policy(video)
        if not force_ai and is_standalone_theme_video(video, self.config):
            self.logger.info("Skip standalone OP/ED video; in-episode lyric rescue remains enabled: %s", video)
            return "excluded", "fresh", False
        has_recent_failure, remaining_seconds = recent_ai_failure(self.config, video)
        if has_recent_failure and not bypass_failure_cooldown:
            self.logger.info(
                "Skip video during AI failure cooldown: %s remaining=%ss",
                video,
                remaining_seconds,
            )
            return "failure_cooldown", "fresh", False

        if force_ai:
            if has_ai_finished_subtitle(video, self.config):
                self._cleanup_srt_sidecars(video)
                return "finished", "fresh", False
            return "needs_ai", "fresh", False

        signature = video_scan_signature(video, self.config, self._config_signature)
        state = self._state_store()
        if state is not None:
            cached_status = state.get_status(signature)
            if cached_status is not None:
                if cached_status in {"finished", "local_chinese", "embedded_chinese"}:
                    self._cleanup_srt_sidecars(video)
                return cached_status, "cached", False

        inventory_deadline = None
        if deadline_monotonic is not None:
            inventory_deadline = min(
                time.monotonic()
                + max(
                    1.0,
                    float(getattr(self.config, "scanner_inventory_file_timeout_seconds", 30) or 30),
                ),
                float(deadline_monotonic),
            )
        status, cacheable, probe_failed = self._classify_uncached(
            video,
            deadline_monotonic=inventory_deadline,
        )
        if cacheable and state is not None:
            state.put_status(signature, status)
            self._note_state_write()
        return status, "fresh", probe_failed

    def _state_store(self) -> ScanStateStore | None:
        if not self._cache_enabled:
            return None
        if self._state is None:
            self._state = ScanStateStore.from_config(self.config)
            self._state_pending_writes = 0
        return self._state

    def _close_state(self, *, commit: bool = True) -> None:
        if self._state is None:
            return
        state = self._state
        self._state = None
        self._state_pending_writes = 0
        try:
            if commit:
                state.commit()
            elif state.in_transaction:
                state.rollback()
        finally:
            state.close()

    def _note_state_write(self) -> None:
        self._state_pending_writes += 1

    def _commit_state_if_needed(self, *, force: bool = False) -> None:
        if self._state is None:
            return
        untracked_transaction = self._state.in_transaction and self._state_pending_writes <= 0
        if (
            not force
            and self._state_pending_writes < SCAN_STATE_COMMIT_EVERY_WRITTEN_VIDEO
            and not untracked_transaction
        ):
            return
        self._state.commit()
        self._state_pending_writes = 0

    def _update_ai_queue(
        self,
        video: Path,
        status: str,
        *,
        eligible_at: float | None = None,
    ) -> None:
        if not self._queue_enabled:
            return
        state = self._state_store()
        if state is None:
            return
        if eligible_at is None and video.exists():
            stat_result = video.stat()
            ai_output_detected = bool(
                status == "finished" and has_ai_finished_subtitle(video, self.config)
            )
            if status in {"needs_ai", "failure_cooldown"}:
                dispositions = {"delivery_required"}
            elif ai_output_detected:
                dispositions = {"delivered", "legacy_preinstrumented_ai"}
            elif status == "missing":
                dispositions = {"missing"}
            else:
                dispositions = {"policy_excluded"}
            if not state.ai_inventory_observation_is_current(
                video,
                media_size=int(stat_result.st_size),
                media_mtime_ns=int(stat_result.st_mtime_ns),
                policy_revision=self._processing_policy_revision,
                classification=status,
                dispositions=dispositions,
            ):
                state.mark_ai_inventory_dirty()
                self._note_state_write()
        if state.is_force_ai_queue_candidate(video):
            if has_ai_finished_subtitle(video, self.config):
                changed = state.mark_ai_queue_done(
                    video,
                    "Forced AI subtitle already exists during scan",
                    completed_at=ai_finished_subtitle_mtime(video, self.config),
                    detected_existing=True,
                    mark_inventory_dirty=False,
                )
                if changed:
                    self._note_state_write()
            elif video.exists():
                queue_snapshot = state.ai_queue_candidate_snapshot(video)
                if queue_snapshot is not None and str(queue_snapshot.get("status") or "") == "running":
                    return
                stat_result = video.stat()
                changed = state.upsert_ai_queue_candidate(
                    video,
                    int(stat_result.st_mtime_ns),
                    source="manual_force",
                    added_at=self._queue_batch_added_at,
                )
                state.ensure_ai_delivery_obligation(
                    video,
                    media_size=int(stat_result.st_size),
                    media_mtime_ns=int(stat_result.st_mtime_ns),
                    policy_revision=self._processing_policy_revision,
                    eligible_at=(
                        float(eligible_at)
                        if eligible_at is not None
                        else self._queue_batch_added_at
                    ),
                    source="scan_force_ai",
                    acceptance_run_id=acceptance_run_id_for_video(self.config, video),
                )
                if changed or state.in_transaction:
                    self._note_state_write()
            return
        exclusion_code = SCANNER_PRE_ATTEMPT_EXCLUSION_CODES.get(status)
        if exclusion_code is not None:
            excluded = state.exclude_pre_attempt_ai_delivery_obligations_for_path(
                video,
                exclusion_code=exclusion_code,
                detail=f"Scanner classified media as {status} before AI attempt",
            )
            if excluded:
                self._note_state_write()
        if status == "needs_ai":
            stat_result = video.stat()
            changed = state.upsert_ai_queue_candidate(
                video,
                int(stat_result.st_mtime_ns),
                source="scan",
                added_at=self._queue_batch_added_at,
            )
            state.ensure_ai_delivery_obligation(
                video,
                media_size=int(stat_result.st_size),
                media_mtime_ns=int(stat_result.st_mtime_ns),
                policy_revision=self._processing_policy_revision,
                eligible_at=(
                    float(eligible_at)
                    if eligible_at is not None
                    else self._queue_batch_added_at
                ),
                source="scan",
                acceptance_run_id=acceptance_run_id_for_video(self.config, video),
            )
            if changed:
                self._note_state_write()
            elif state.in_transaction:
                # A first ledger admission can be the only write when an older
                # queue row already exists. Ensure it participates in the scan's
                # normal bounded commit cadence.
                self._note_state_write()
            return
        if status == "failure_cooldown":
            # Keep the durable queue row while the separate failure marker
            # blocks dispatch. Deleting it would discard attempts and allow a
            # later scan to restart an exhausted retry budget from zero.
            stat_result = video.stat()
            state.ensure_ai_delivery_obligation(
                video,
                media_size=int(stat_result.st_size),
                media_mtime_ns=int(stat_result.st_mtime_ns),
                policy_revision=self._processing_policy_revision,
                eligible_at=(
                    float(eligible_at)
                    if eligible_at is not None
                    else self._queue_batch_added_at
                ),
                source="scan_failure_cooldown",
                acceptance_run_id=acceptance_run_id_for_video(self.config, video),
            )
            self._note_state_write()
            return
        if status == "finished" and self._has_required_finished_subtitle(video):
            if has_ai_finished_subtitle(video, self.config):
                changed = state.mark_ai_queue_done(
                    video,
                    "Finished AI subtitle detected during scan",
                    completed_at=ai_finished_subtitle_mtime(video, self.config),
                    detected_existing=True,
                    mark_inventory_dirty=False,
                )
                if changed:
                    self._note_state_write()
            else:
                if state.remove_ai_queue_candidate(
                    video,
                    clear_job_state=True,
                    mark_inventory_dirty=False,
                ):
                    self._note_state_write()
            return
        if state.remove_ai_queue_candidate(
            video,
            mark_inventory_dirty=False,
        ):
            self._note_state_write()

    def backfill_active_queue_obligations(
        self,
        *,
        limit: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, int]:
        """Run one bounded queue-only ledger drain and release its DB handle."""

        self._clear_last_database_error()
        batch_limit = max(
            1,
            int(
                limit
                if limit is not None
                else getattr(
                    self.config,
                    "scanner_active_queue_ledger_backfill_batch_size",
                    250,
                )
                or 250
            ),
        )
        try:
            result = self._backfill_active_queue_obligations(
                limit=batch_limit,
                cancel_event=cancel_event,
            )
            self._close_state(commit=True)
            return result
        except sqlite3.DatabaseError as exc:
            try:
                self._close_state(commit=False)
            except sqlite3.DatabaseError:
                pass
            if not _is_scan_state_lock_error(exc):
                raise
            self._record_last_database_error(exc, "active_queue_ledger_backfill")
            self.logger.warning(
                "Active AI queue ledger backfill deferred because the state database is busy: %s",
                exc,
            )
            return {"database_busy": 1}
        except Exception:
            try:
                self._close_state(commit=False)
            except sqlite3.DatabaseError:
                pass
            raise

    def _backfill_active_queue_obligations(
        self,
        *,
        limit: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, int]:
        """Repair only active queue ledger gaps without scanning the media root."""

        result: Counter[str] = Counter()
        if cancel_event is not None and cancel_event.is_set():
            result["cancelled"] = 1
            return dict(result)
        state = self._state_store()
        if state is None:
            return dict(result)
        batch_limit = max(
            1,
            int(
                limit
                if limit is not None
                else getattr(self.config, "scanner_reconcile_batch_size", 1000) or 1000
            ),
        )
        candidates = state.untracked_active_ai_queue_candidates(
            policy_revision=self._processing_policy_revision,
            limit=batch_limit,
        )
        result["selected"] = len(candidates)
        skipped_samples: list[str] = []
        for candidate in candidates:
            if cancel_event is not None and cancel_event.is_set():
                result["cancelled"] = 1
                break
            video = Path(candidate["path"])
            expected_mtime_ns = int(candidate["mtime_ns"])
            expected_status = str(candidate["status"])
            expected_force_ai = bool(candidate.get("force_ai"))
            expected_source = str(candidate.get("source") or "")
            try:
                before = video.stat()
            except OSError:
                result["missing_or_unreadable"] += 1
                if len(skipped_samples) < 3:
                    skipped_samples.append(f"missing_or_unreadable:{video}")
                continue

            if expected_status == "running":
                running_result = (
                    "running_identity_changed"
                    if expected_mtime_ns != int(before.st_mtime_ns)
                    else "running_active"
                )
                result[running_result] += 1
                if len(skipped_samples) < 3:
                    skipped_samples.append(f"{running_result}:{video}")
                continue

            media_identity_changed = expected_mtime_ns != int(before.st_mtime_ns)
            legacy_force_identity = (
                expected_status == "queued"
                and expected_force_ai
                and expected_source == "manual_force"
                and expected_mtime_ns > int(before.st_mtime_ns)
            )
            if media_identity_changed and not legacy_force_identity:
                result["media_identity_changed_unproven"] += 1
                if len(skipped_samples) < 3:
                    skipped_samples.append(f"media_identity_changed_unproven:{video}")
                continue

            try:
                if not state.begin_active_ai_queue_ledger_backfill(
                    video,
                    expected_mtime_ns=expected_mtime_ns,
                    expected_status=expected_status,
                    expected_force_ai=expected_force_ai,
                    expected_source=expected_source,
                ):
                    if state.in_transaction:
                        state.rollback()
                    result["queue_changed"] += 1
                    continue
                state.upsert_ai_queue_candidate(
                    video,
                    int(before.st_mtime_ns),
                    source="active_queue_ledger_backfill",
                )
                obligation = state.ensure_ai_delivery_obligation(
                    video,
                    media_size=int(before.st_size),
                    media_mtime_ns=int(before.st_mtime_ns),
                    policy_revision=self._processing_policy_revision,
                    eligible_at=time.time(),
                    source="active_queue_ledger_backfill",
                )
                after = video.stat()
                if (
                    int(before.st_size) != int(after.st_size)
                    or int(before.st_mtime_ns) != int(after.st_mtime_ns)
                ):
                    state.rollback()
                    result["media_changed"] += 1
                    continue
                if str(obligation.get("state") or "") != "open":
                    state.rollback()
                    result["matching_obligation_not_open"] += 1
                    if len(skipped_samples) < 3:
                        skipped_samples.append(f"matching_obligation_not_open:{video}")
                    continue
                state.commit()
                self._state_pending_writes = 0
                result["repaired"] += 1
            except OSError:
                if state.in_transaction:
                    state.rollback()
                self._state_pending_writes = 0
                result["missing_or_unreadable"] += 1
                if len(skipped_samples) < 3:
                    skipped_samples.append(f"missing_or_unreadable:{video}")
            except Exception:
                if state.in_transaction:
                    state.rollback()
                self._state_pending_writes = 0
                raise

        if result["repaired"] or result["queue_changed"]:
            self.logger.info(
                "Active AI queue ledger backfill complete. selected=%s repaired=%s cancelled=%s missing_or_unreadable=%s running_active=%s running_identity_changed=%s media_identity_changed_unproven=%s queue_changed=%s media_changed=%s matching_obligation_not_open=%s",
                result["selected"],
                result["repaired"],
                result["cancelled"],
                result["missing_or_unreadable"],
                result["running_active"],
                result["running_identity_changed"],
                result["media_identity_changed_unproven"],
                result["queue_changed"],
                result["media_changed"],
                result["matching_obligation_not_open"],
            )
        blocker_counts = {
            key: result[key]
            for key in (
                "missing_or_unreadable",
                "running_active",
                "running_identity_changed",
                "media_identity_changed_unproven",
                "matching_obligation_not_open",
            )
            if result[key]
        }
        if blocker_counts:
            warning_signature = json.dumps(
                {"counts": blocker_counts, "samples": skipped_samples},
                ensure_ascii=False,
                sort_keys=True,
            )
            now_monotonic = time.monotonic()
            if (
                warning_signature != self._last_ledger_backfill_warning_signature
                or now_monotonic - self._last_ledger_backfill_warning_at >= 300.0
            ):
                self.logger.warning(
                    "Active AI queue ledger backfill kept fail-closed rows unchanged. counts=%s samples=%s",
                    blocker_counts,
                    skipped_samples,
                )
                self._last_ledger_backfill_warning_signature = warning_signature
                self._last_ledger_backfill_warning_at = now_monotonic
        else:
            self._last_ledger_backfill_warning_signature = ""
        return dict(result)

    def _queued_ai_candidates(
        self,
        max_candidates: int | None = None,
        *,
        exact_target: Path | None = None,
    ) -> list[Path]:
        state = self._state_store()
        if state is None:
            return []

        acceptance_lane = load_acceptance_queue_lane(self.config)
        result_observer = None
        if bool(
            getattr(self.config, "m2_server_canary_observer_enabled", False)
        ):
            from m2_production_observation import record_state_attempt_result

            def result_observer(observed_path: Path, attempt_id: str) -> None:
                record_state_attempt_result(
                    self.config,
                    state,
                    observed_path,
                    attempt_id,
                    transaction_connection=state.observation_connection,
                    logger=self.logger,
                )

        if acceptance_lane is None:
            self._backfill_active_queue_obligations()
            requeued = state.requeue_stale_running(
                getattr(
                    self.config,
                    "ai_queue_stage_stale_seconds",
                    getattr(self.config, "ai_queue_running_stale_seconds", 21600),
                ),
                result_observer=result_observer,
            )
        else:
            requeued = state.requeue_acceptance_running_targets(
                acceptance_lane.targets,
                stale_after_seconds=getattr(
                    self.config,
                    "ai_queue_stage_stale_seconds",
                    getattr(self.config, "ai_queue_running_stale_seconds", 21600),
                ),
                message="Timed-out acceptance lane job was safely requeued",
                result_observer=result_observer,
            )
        if requeued:
            self._note_state_write()
            self._commit_state_if_needed(force=True)
            try:
                from m2_observation_store import publish_pending_summaries

                publish_pending_summaries(self.config)
            except Exception:
                # The SQLite outbox retains the summary for a later retry.
                pass
        if requeued:
            self.logger.warning("Requeued stale AI running job(s): count=%s", requeued)

        candidates: list[Path] = []
        self._queue_selection_cycle += 1
        oldest_interval = max(
            0,
            int(getattr(self.config, "scanner_queue_oldest_every_n_cycles", 12) or 0),
        )
        oldest_first = bool(
            oldest_interval > 0 and self._queue_selection_cycle % oldest_interval == 0
        )
        if oldest_first:
            self.logger.info(
                "AI queue fairness cycle selected oldest ready work. cycle=%s interval=%s",
                self._queue_selection_cycle,
                oldest_interval,
            )
        for video in state.iter_ai_queue_candidates(
            oldest_first=oldest_first,
            acceptance_targets=(
                acceptance_lane.targets if acceptance_lane is not None else None
            ),
            exact_target=exact_target,
        ):
            if not video.exists() or video.suffix.lower() not in set(self.config.video_extensions):
                if state.remove_ai_queue_candidate(video, mark_inventory_dirty=True):
                    self._note_state_write()
                    self._commit_state_if_needed()
                continue
            force_ai, bypass_failure_cooldown = state.ai_queue_candidate_policy(video)
            if not force_ai and is_standalone_theme_video(video, self.config):
                if state.remove_ai_queue_candidate(
                    video, clear_job_state=True, mark_inventory_dirty=True
                ):
                    self._note_state_write()
                    self._commit_state_if_needed()
                self.logger.info("Removed standalone OP/ED video from AI queue: %s", video)
                continue
            has_recent_failure, _remaining_seconds = recent_ai_failure(self.config, video)
            if has_recent_failure and not bypass_failure_cooldown:
                continue
            if not force_ai and _too_new_for_ai_queue(video, self.config):
                continue
            if (
                force_ai
                and has_ai_finished_subtitle(video, self.config)
                and self._completed_delivery_satisfied(video)
            ):
                changed = state.mark_ai_queue_done(
                    video,
                    "Forced AI subtitle already exists before queue processing",
                    completed_at=ai_finished_subtitle_mtime(video, self.config),
                    detected_existing=True,
                    mark_inventory_dirty=True,
                )
                if changed:
                    self._note_state_write()
                    self._commit_state_if_needed()
                continue
            if not force_ai and self._settle_existing_subtitles_before_ai(video, state):
                continue
            candidates.append(video)
            if max_candidates is not None and max_candidates > 0 and len(candidates) >= max_candidates:
                break
        return candidates

    def _settle_existing_subtitles_before_ai(self, video: Path, state: ScanStateStore) -> bool:
        if has_finished_subtitle(video, self.config) and not self._completed_delivery_satisfied(video):
            # The subtitle publication is reusable, but the playable completed
            # artifact is absent or invalid. Keep the candidate for the
            # Worker's delivery-only recovery instead of rerunning ASR.
            return False
        if self._has_required_finished_subtitle(video):
            if has_ai_finished_subtitle(video, self.config):
                try:
                    changed = state.mark_ai_queue_done(
                        video,
                        "Finished AI subtitle detected before queue processing",
                        completed_at=ai_finished_subtitle_mtime(video, self.config),
                        detected_existing=True,
                        mark_inventory_dirty=True,
                    )
                except sqlite3.DatabaseError as exc:
                    if _is_scan_state_lock_error(exc):
                        self._rollback_state_after_lock("mark_existing_ai_done", video, exc)
                        return True
                    raise
                if changed:
                    self._note_state_write()
                    self._commit_state_if_needed()
            else:
                if self._remove_ai_queue_candidate_lock_safe(
                    state,
                    video,
                    context="existing_required_subtitle_before_ai",
                    clear_job_state=True,
                ):
                    self._note_state_write()
                    self._commit_state_if_needed()
            return True

        if getattr(self.config, "require_ai_subtitles", False):
            return False

        try:
            normalized = normalize_sidecar_subtitles(video, self.config)
        except SubtitleExtractError as exc:
            self.logger.warning("Failed to normalize sidecar subtitles before AI for %s: %s", video, exc)
            normalized = []
        for subtitle in normalized:
            self.logger.info(
                "Normalized sidecar subtitle before AI language=%s video=%s path=%s",
                subtitle.language,
                video,
                subtitle.path,
            )
        normalized_decision = select_subtitle_source(
            normalized,
            self.config,
            source_kind="sidecar",
        )
        if (
            normalized_decision is not None
            and normalized_decision.strategy == USE_ZH_TW
            and (
                not bool(getattr(self.config, "source_analyzer_enabled", False))
                or self._has_required_finished_subtitle(video)
            )
        ):
            if self._remove_ai_queue_candidate_lock_safe(
                state,
                video,
                context="local_chinese_sidecar_before_ai",
                clear_job_state=True,
            ):
                self._note_state_write()
                self._commit_state_if_needed()
            return True
        return False

    def _remove_ai_queue_candidate_lock_safe(
        self,
        state: ScanStateStore,
        video: Path,
        *,
        context: str,
        clear_job_state: bool = False,
    ) -> bool:
        try:
            return state.remove_ai_queue_candidate(
                video,
                clear_job_state=clear_job_state,
                mark_inventory_dirty=True,
            )
        except sqlite3.DatabaseError as exc:
            if _is_scan_state_lock_error(exc):
                self._rollback_state_after_lock(context, video, exc)
                return False
            raise

    def _rollback_state_after_lock(self, context: str, video: Path, error: sqlite3.DatabaseError) -> None:
        self.logger.warning(
            "Scanner state database is busy; deferred queue cleanup context=%s video=%s error=%s",
            context,
            video,
            error,
        )
        if self._state is None:
            return
        try:
            self._state.rollback()
            self._state_pending_writes = 0
        except sqlite3.DatabaseError:
            pass

    def _classify_uncached(
        self,
        video: Path,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[str, bool, bool]:
        if has_finished_subtitle(video, self.config) and not self._completed_delivery_satisfied(video):
            return "needs_ai", True, False
        if getattr(self.config, "require_ai_subtitles", False):
            if self._has_required_finished_subtitle(video):
                self._cleanup_srt_sidecars(video)
                return "finished", True, False
            return self._classify_ai_audio_eligibility(video)

        if has_finished_subtitle(video, self.config):
            self._cleanup_srt_sidecars(video)
            return "finished", True, False
        try:
            if deadline_monotonic is None:
                normalized = normalize_sidecar_subtitles(video, self.config)
            else:
                normalized = normalize_sidecar_subtitles(
                    video,
                    self.config,
                    deadline_monotonic=deadline_monotonic,
                )
        except SubtitleExtractError as exc:
            self.logger.warning("Failed to normalize sidecar subtitles for %s: %s", video, exc)
            normalized = []
        for subtitle in normalized:
            self.logger.info(
                "Normalized sidecar subtitle language=%s video=%s path=%s",
                subtitle.language,
                video,
                subtitle.path,
            )
        normalized_decision = select_subtitle_source(
            normalized,
            self.config,
            source_kind="sidecar",
        )
        if (
            normalized_decision is not None
            and normalized_decision.strategy == USE_ZH_TW
            and (
                not bool(getattr(self.config, "source_analyzer_enabled", False))
                or self._has_required_finished_subtitle(video)
            )
        ):
            self._cleanup_srt_sidecars(video)
            return "local_chinese", True, False
        try:
            if deadline_monotonic is None:
                extracted = extract_available_subtitles(video, self.config)
            else:
                extracted = extract_available_subtitles(
                    video,
                    self.config,
                    deadline_monotonic=deadline_monotonic,
                )
        except SubtitleExtractError as exc:
            self.logger.warning("Failed to inspect embedded subtitles for %s: %s", video, exc)
            return "needs_ai", False, True

        for subtitle in extracted:
            self.logger.info(
                "Extracted embedded subtitle language=%s stream=%s video=%s path=%s",
                subtitle.language,
                subtitle.stream_index,
                video,
                subtitle.path,
            )
        extracted_decision = select_subtitle_source(
            [*normalized, *extracted],
            self.config,
            source_kind="sidecar_or_embedded",
        )
        if (
            extracted_decision is not None
            and extracted_decision.strategy == USE_ZH_TW
            and (
                not bool(getattr(self.config, "source_analyzer_enabled", False))
                or self._has_required_finished_subtitle(video)
            )
        ):
            self._cleanup_srt_sidecars(video)
            return "embedded_chinese", True, False
        if extracted_decision is not None:
            # A validated zh-CN or Japanese subtitle is itself the source for
            # the next Worker route.  Audio language admission is irrelevant
            # and must not exclude it or invoke an audio probe.
            return "needs_ai", True, False
        return self._classify_ai_audio_eligibility(video)

    def _classify_ai_audio_eligibility(self, video: Path) -> tuple[str, bool, bool]:
        if not _requires_japanese_audio_admission_preflight(self.config):
            return "needs_ai", True, False

        manifest = probe_audio_stream_manifest(video)
        if not manifest.complete:
            self.logger.warning(
                "Audio eligibility probe was inconclusive; retaining AI candidate. video=%s error=%s",
                video,
                manifest.error or "unknown probe failure",
            )
            # Retry the metadata probe on a later scan while failing closed in
            # favour of admission now.
            return "needs_ai", False, True
        if manifest_confirms_no_non_commentary_japanese_audio(manifest):
            self.logger.info(
                "Exclude Japanese bilingual AI admission; complete audio manifest has no non-commentary Japanese stream. video=%s streams=%s",
                video,
                [stream.to_dict() for stream in manifest.streams],
            )
            return "unsupported_media", True, False
        return "needs_ai", True, False

    def _has_required_finished_subtitle(self, video: Path) -> bool:
        if getattr(self.config, "require_ai_subtitles", False):
            finished = has_ai_finished_subtitle(video, self.config)
        else:
            finished = has_finished_subtitle(video, self.config)
        return finished and self._completed_delivery_satisfied(video)

    def _completed_delivery_satisfied(self, video: Path) -> bool:
        if not bool(getattr(self.config, "completed_delivery_enabled", False)):
            return True
        from completed_delivery import validate_completed_delivery

        return validate_completed_delivery(video, self.config, verify_streams=False)

    def _force_ai_requested(self, video: Path) -> bool:
        force_ai, _bypass_failure_cooldown = self._queue_policy(video)
        return force_ai

    def _queue_policy(self, video: Path) -> tuple[bool, bool]:
        state = self._state_store()
        if state is None:
            return False, False
        return state.ai_queue_candidate_policy(video)

    def _cleanup_srt_sidecars(self, video: Path) -> None:
        removed = remove_ai_srt_outputs(video, self.config)
        if removed:
            self.logger.info("Removed AI SRT intermediates after ASS output exists: video=%s count=%s", video, len(removed))

    def scan_all(self, *, force_full: bool = False) -> list[Path]:
        input_path = self.config.input_path
        if not input_path.exists():
            self.logger.warning("Input path does not exist: %s", input_path)
            return []

        videos: list[Path] = []
        priorities: dict[Path, int] = {}
        extensions = set(self.config.video_extensions)
        now_monotonic = time.monotonic()
        full_scan, cutoff_ns = self._scan_scope(now_monotonic, force_full=force_full)
        max_seen_mtime_ns = 0
        for path, priority_time_ns in self._walk_video_files(input_path, extensions):
            max_seen_mtime_ns = max(max_seen_mtime_ns, priority_time_ns)
            if cutoff_ns is not None and priority_time_ns < cutoff_ns:
                continue
            videos.append(path)
            priorities[path] = priority_time_ns

        self._update_scan_high_water(max_seen_mtime_ns)
        self._log_scan_scope(full_scan, len(videos), now_monotonic, cutoff_ns)
        if bool(getattr(self.config, "scanner_recent_first", True)):
            return sorted(videos, key=lambda item: (-priorities[item], str(item).casefold()))
        return sorted(videos, key=lambda item: str(item).casefold())

    def _walk_video_files(
        self,
        input_path: Path,
        extensions: set[str],
        *,
        fail_on_error: bool = False,
    ):
        """Walk a large media tree without stat'ing every subtitle and artwork file.

        pathlib.rglob followed by is_file performs metadata I/O for every entry in
        the library.  Anime folders commonly contain several subtitle and artwork
        files per video, so that approach can saturate an Unraid array even during
        an "incremental" scan.  os.walk uses scandir's cached directory entries;
        we filter by extension before the single stat required for a video and
        periodically yield so background reconciliation cannot monopolize I/O.
        """

        yield_every = max(1, int(getattr(self.config, "scanner_walk_yield_every_entries", 256) or 256))
        yield_seconds = max(0.0, float(getattr(self.config, "scanner_walk_yield_seconds", 0.025) or 0.0))
        entries_since_yield = 0

        def account_entry() -> None:
            nonlocal entries_since_yield
            if yield_seconds <= 0:
                return
            entries_since_yield += 1
            if entries_since_yield >= yield_every:
                time.sleep(yield_seconds)
                if io_pressure_busy(self.config):
                    time.sleep(
                        max(0.0, float(getattr(self.config, "storage_io_pressure_backoff_seconds", 2.0) or 0.0))
                    )
                entries_since_yield = 0

        def onerror(exc: OSError) -> None:
            if fail_on_error:
                raise InventoryWalkError(f"unable to enumerate media inventory: {exc}") from exc
            self.logger.warning("Unable to scan media directory; continuing. error=%s", exc)

        for directory, dirnames, filenames in os.walk(input_path, onerror=onerror, followlinks=False):
            dirnames.sort(key=str.casefold)
            filenames.sort(key=str.casefold)
            for _dirname in dirnames:
                account_entry()
            for filename in filenames:
                account_entry()
                if os.path.splitext(filename)[1].lower() not in extensions:
                    continue
                path = Path(directory) / filename
                try:
                    info = path.stat()
                except OSError as exc:
                    if fail_on_error:
                        raise InventoryWalkError(
                            f"unable to stat media inventory path {path}: {exc}"
                        ) from exc
                    self.logger.debug("Unable to stat candidate video during scan: path=%s error=%s", path, exc)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                yield path, max(info.st_mtime_ns, info.st_ctime_ns)

    def _scan_scope(self, now_monotonic: float, *, force_full: bool) -> tuple[bool, int | None]:
        if bool(getattr(self.config, "scanner_incremental_scan_enabled", False)):
            return self._incremental_scan_scope(now_monotonic, force_full=force_full)
        return self._legacy_scan_scope(now_monotonic, force_full=force_full)

    def _incremental_scan_scope(self, now_monotonic: float, *, force_full: bool) -> tuple[bool, int | None]:
        if force_full or self._last_scan_high_water_ns is None:
            self._last_full_scan_monotonic = now_monotonic
            return True, None

        interval_seconds = int(getattr(self.config, "scanner_full_scan_interval_seconds", 0))
        if (
            interval_seconds > 0
            and self._last_full_scan_monotonic is not None
            and now_monotonic - self._last_full_scan_monotonic >= interval_seconds
        ):
            self._last_full_scan_monotonic = now_monotonic
            return True, None

        overlap_seconds = int(getattr(self.config, "scanner_incremental_overlap_seconds", 300))
        overlap_ns = overlap_seconds * 1_000_000_000
        return False, max(0, self._last_scan_high_water_ns - overlap_ns)

    def _legacy_scan_scope(self, now_monotonic: float, *, force_full: bool) -> tuple[bool, int | None]:
        if force_full:
            return True, None
        recent_days = int(getattr(self.config, "scanner_quick_scan_recent_days", 0))
        interval_seconds = int(getattr(self.config, "scanner_full_scan_interval_seconds", 0))
        if recent_days <= 0 or interval_seconds <= 0:
            return True, None
        if self._last_full_scan_monotonic is None:
            self._last_full_scan_monotonic = now_monotonic
            return True, None
        if now_monotonic - self._last_full_scan_monotonic >= interval_seconds:
            self._last_full_scan_monotonic = now_monotonic
            return True, None
        return False, self._quick_scan_cutoff_ns()

    def _quick_scan_cutoff_ns(self) -> int | None:
        recent_days = int(getattr(self.config, "scanner_quick_scan_recent_days", 0))
        if recent_days <= 0:
            return None
        return time.time_ns() - recent_days * 24 * 60 * 60 * 1_000_000_000

    def _update_scan_high_water(self, max_seen_mtime_ns: int) -> None:
        if max_seen_mtime_ns <= 0:
            return
        if self._last_scan_high_water_ns is None or max_seen_mtime_ns > self._last_scan_high_water_ns:
            self._last_scan_high_water_ns = max_seen_mtime_ns

    def _log_scan_scope(self, full_scan: bool, video_count: int, now_monotonic: float, cutoff_ns: int | None) -> None:
        if bool(getattr(self.config, "scanner_incremental_scan_enabled", False)):
            if full_scan:
                self.logger.info("Scanner full scan scope videos=%s queue_mode=latest-first-with-fairness", video_count)
                return
            self.logger.info(
                "Scanner incremental scan scope videos=%s queue_mode=latest-first-with-fairness cutoff_mtime_ns=%s",
                video_count,
                cutoff_ns,
            )
            return

        if full_scan:
            self.logger.info("Scanner full scan scope videos=%s", video_count)
            return
        interval_seconds = int(getattr(self.config, "scanner_full_scan_interval_seconds", 0))
        elapsed = 0 if self._last_full_scan_monotonic is None else int(now_monotonic - self._last_full_scan_monotonic)
        next_full_scan_seconds = max(0, interval_seconds - elapsed)
        self.logger.info(
            "Scanner quick scan scope recent_days=%s videos=%s next_full_scan_in=%ss",
            int(getattr(self.config, "scanner_quick_scan_recent_days", 0)),
            video_count,
            next_full_scan_seconds,
        )

def _requires_japanese_audio_admission_preflight(config: AppConfig) -> bool:
    """Whether policy admits only Japanese-source bilingual AI work."""

    if not bool(getattr(config, "language_gate_enabled", False)):
        return False
    if not bool(getattr(config, "skip_non_allowed_language", True)):
        return False
    if bool(getattr(config, "transcribe_non_allowed_languages", False)):
        return False
    configured = getattr(config, "allowed_source_languages", ["ja"])
    if isinstance(configured, str):
        configured = [configured]
    allowed = {
        str(language or "").strip().casefold().replace("_", "-")
        for language in (configured or [])
    }
    japanese = {"ja", "jpn", "japanese", "jp", "jap"}
    return bool(allowed) and allowed.issubset(japanese)


def _safe_priority_time_ns(path: Path) -> int:
    try:
        stat = path.stat()
        return max(stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return 0


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _too_new_for_ai_queue(path: Path, config: AppConfig) -> bool:
    min_age_seconds = int(getattr(config, "scanner_candidate_min_age_seconds", 0) or 0)
    if min_age_seconds <= 0:
        return False
    priority_time_ns = _safe_priority_time_ns(path)
    if priority_time_ns <= 0:
        return False
    return time.time_ns() - priority_time_ns < min_age_seconds * 1_000_000_000
