from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from ai_scheduler_state import (
    ai_scheduler_next_retry_at,
    report_ai_scheduler_error,
    request_ai_scheduler_retry,
    start_ai_scheduler_heartbeat,
    update_ai_scheduler_state,
)
from acceptance_queue_lane import (
    AcceptanceQueueLaneError,
    load_acceptance_queue_lane,
    verify_acceptance_queue_target_source,
)


AI_SCHEDULER_WAKE_EVENT = threading.Event()
_AI_REVIEW_REMEDIATION_HANDOFF_LOCK = threading.RLock()
_AI_REVIEW_REMEDIATION_LOGGED_RESERVATION: tuple[str, str] | None = None
_CONTROL_COMMAND_RECONCILE_INTERVAL_SECONDS = 60.0


def main() -> int:
    parser = argparse.ArgumentParser(description="Anime subtitle automation tool")
    parser.add_argument("--config", required=True, help="Path to config.yaml")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan-once", action="store_true", help="Scan once and exit")
    mode.add_argument("--watch", action="store_true", help="Scan periodically")
    mode.add_argument("--auto-once", action="store_true", help="Run Mikan official-sub workflow, then AI fallback scan once")
    mode.add_argument("--auto-watch", action="store_true", help="Run integrated Mikan + AI fallback workflow periodically")
    mode.add_argument("--cleanup-generated-artifacts", action="store_true", help="Remove generated AI SRT intermediates and QA reports")
    mode.add_argument("--refresh-ai-queue-state", action="store_true", help="Refresh AI queue classification without processing videos")
    mode.add_argument(
        "--requeue-stale-ai-running",
        action="store_true",
        help="Requeue only timed-out running AI queue rows without scanning media",
    )
    mode.add_argument("--refresh-ass", action="store_true", help="Re-export ASS files from existing SRT files")
    mode.add_argument("--reexport-ass", action="store_true", help="Alias for --refresh-ass")
    mode.add_argument("--mikan-sync-once", action="store_true", help="Queue latest Mikan releases and process completed qBittorrent downloads once")
    mode.add_argument("--mikan-dry-run", action="store_true", help="List selected Mikan releases without adding torrents")
    mode.add_argument("--mikan-watch", action="store_true", help="Run Mikan/qBittorrent sync periodically")
    mode.add_argument("--mikan-process-completed", action="store_true", help="Extract subtitles from completed qBittorrent downloads only")
    mode.add_argument("--mikan-reset-all", action="store_true", help="Reset Mikan seen/pending state and queue missing releases once")
    mode.add_argument("--mikan-redownload-all", action="store_true", help="Delete Mikan qBittorrent tasks without deleting files, then reset and requeue")
    mode.add_argument("--mikan-requeue-failed-extracts", action="store_true", help="Requeue failed Mikan subtitle extraction jobs")
    mode.add_argument("--process-video", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--video-path", help=argparse.SUPPRESS)
    parser.add_argument(
        "--mikan-redownload-delete-files",
        action="store_true",
        help="With --mikan-redownload-all, also delete downloaded files. Dangerous for library paths.",
    )

    args = parser.parse_args()

    from config import ConfigError, load_config
    from logger import setup_logging
    from backup_cleanup import cleanup_backup_files
    from work_cleanup import cleanup_work_artifacts

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(config.log_path)
    shutdown_event = _install_shutdown_handler(logger)
    if not _run_safety_checks(config, logger):
        return 2
    if args.requeue_stale_ai_running:
        _requeue_stale_ai_running(config, logger)
        return 0
    if args.watch or args.auto_watch:
        start_ai_scheduler_heartbeat(config, logger, shutdown_event)
        _start_background_ai_ledger_backfill(config, logger, shutdown_event)

    # Long-running modes must expose the command inbox before any library-wide
    # startup housekeeping.  cleanup_backup_files() intentionally walks the
    # media tree and can take minutes on array-backed storage; starting the
    # inbox first keeps deployment health probes and cooperative cancellation
    # responsive while that low-priority scan is still running.
    if args.watch or args.auto_watch or args.mikan_watch:
        _start_background_control_commands(config, logger, shutdown_event)
    if not args.process_video:
        cleanup_backup_files(config, logger)
        cleanup_work_artifacts(config, logger)

    if args.process_video:
        if not args.video_path:
            print("--video-path is required with --process-video", file=sys.stderr)
            return 2
        from worker import VideoWorker

        ok = VideoWorker(config, logger).process(Path(args.video_path))
        return 0 if ok else 1

    if args.mikan_sync_once:
        _mikan_run_once(config, logger)
        return 0

    if args.mikan_dry_run:
        _mikan_dry_run(config, logger)
        return 0

    if args.mikan_process_completed:
        _mikan_process_completed(config, logger)
        return 0

    if args.mikan_reset_all:
        _mikan_reset_all(config, logger)
        return 0

    if args.mikan_redownload_all:
        _mikan_redownload_all(config, logger, delete_files=args.mikan_redownload_delete_files)
        return 0

    if args.mikan_requeue_failed_extracts:
        _mikan_requeue_failed_extracts(config, logger)
        return 0

    if args.mikan_watch:
        watcher = _start_event_watcher(config, logger)
        _start_background_mikan_watch(config, logger, shutdown_event, require_parallel_flag=False)
        _start_background_state_backup(config, logger, shutdown_event)
        _start_background_database_maintenance(config, logger, shutdown_event)
        _start_background_series_metadata_sync(config, logger, shutdown_event)
        logger.info(
            "Mikan watch mode started. enqueue_interval=%s completed_poll_interval=%s",
            config.mikan_watch_interval_seconds,
            getattr(config, "mikan_completed_poll_interval_seconds", config.mikan_watch_interval_seconds),
        )
        while not shutdown_event.is_set():
            if shutdown_event.wait(3600):
                break
        logger.info("Shutdown requested; exiting Mikan watch mode.")
        return 0

    from scanner import VideoScanner
    from worker import VideoWorker

    scanner = VideoScanner(config, logger)
    worker = VideoWorker(config, logger)
    try:
        review_reconciliation = worker.reconcile_published_ai_quality_reviews()
    except Exception as exc:  # noqa: BLE001 - stale review cleanup cannot block the Worker.
        logger.warning(
            "Startup AI quality-review reconciliation was unavailable; reviews remain durable: %s",
            exc,
        )
        review_reconciliation = {"examined": 0, "resolved": 0, "errors": 1}
    if review_reconciliation.get("examined") or review_reconciliation.get("errors"):
        logger.info(
            "Startup AI quality-review reconciliation: examined=%s resolved=%s missing=%s stale=%s invalid=%s errors=%s",
            review_reconciliation.get("examined", 0),
            review_reconciliation.get("resolved", 0),
            review_reconciliation.get("missing", 0),
            review_reconciliation.get("stale", 0),
            review_reconciliation.get("invalid", 0),
            review_reconciliation.get("errors", 0),
        )
    try:
        tm_replay = worker.replay_pending_translation_memory_outbox(limit=32)
    except Exception as exc:  # noqa: BLE001 - optional learning cannot block subtitle delivery startup.
        logger.warning(
            "Translation-memory startup replay was unavailable; pending intents remain durable: %s",
            exc,
        )
        tm_replay = {"attempted": 0, "completed": 0, "retained": 0}
    if tm_replay["attempted"]:
        logger.info(
            "Translation-memory startup outbox replay: attempted=%s completed=%s retained=%s",
            tm_replay["attempted"],
            tm_replay["completed"],
            tm_replay["retained"],
        )

    if args.scan_once:
        _scan_and_process(scanner, worker, logger)
        return 0

    if args.auto_once:
        _auto_run_once(scanner, worker, config, logger)
        return 0

    if args.cleanup_generated_artifacts:
        _cleanup_generated_artifacts(config, logger)
        return 0

    if args.refresh_ai_queue_state:
        _refresh_ai_queue_state(scanner, logger)
        return 0

    if args.refresh_ass or args.reexport_ass:
        _refresh_ass_exports(scanner, worker, logger)
        return 0

    if args.auto_watch:
        _requeue_previous_worker_running(config, logger)
        watcher = _start_event_watcher(config, logger)
        mikan_background = _start_background_mikan_watch(config, logger, shutdown_event)
        ai_scan_background = _start_background_ai_scan(
            config,
            logger,
            shutdown_event,
            event_watcher=watcher,
        )
        _start_background_state_backup(config, logger, shutdown_event)
        _start_background_database_maintenance(config, logger, shutdown_event)
        _start_background_series_metadata_sync(config, logger, shutdown_event)
        logger.info("Integrated auto watch mode started. interval=%s seconds", config.watch_interval_seconds)
        while not shutdown_event.is_set():
            try:
                _auto_run_once(
                    scanner,
                    worker,
                    config,
                    logger,
                    mikan_managed_externally=mikan_background is not None,
                    ai_scan_managed_externally=ai_scan_background is not None,
                    shutdown_event=shutdown_event,
                )
            except Exception as exc:  # noqa: BLE001 - one transient DB/API failure must not kill auto-watch.
                report_ai_scheduler_error(config, exc, logger=logger)
                logger.exception(
                    "Unhandled integrated auto cycle error; worker remains alive and will retry. error=%s",
                    exc,
                )
                if shutdown_event.wait(min(30, max(1, int(config.watch_interval_seconds)))):
                    break
                continue
            if _wait_for_next_cycle_or_ai_resume(shutdown_event, config.watch_interval_seconds, config):
                break
        update_ai_scheduler_state(
            config,
            state="stopping",
            reason_code="shutdown",
            message="Worker shutdown requested; no new AI work will start.",
            current_video=None,
            logger=logger,
        )
        logger.info("Shutdown requested; exiting integrated auto watch mode.")
        return 0

    _requeue_previous_worker_running(config, logger)
    watcher = _start_event_watcher(config, logger)
    _start_background_state_backup(config, logger, shutdown_event)
    _start_background_database_maintenance(config, logger, shutdown_event)
    _start_background_series_metadata_sync(config, logger, shutdown_event)
    logger.info("Watch mode started. interval=%s seconds", config.watch_interval_seconds)
    while not shutdown_event.is_set():
        try:
            _scan_and_process(scanner, worker, logger, shutdown_event=shutdown_event)
        except Exception as exc:  # noqa: BLE001 - keep the long-running worker alive after transient failures.
            report_ai_scheduler_error(config, exc, logger=logger)
            logger.exception(
                "Unhandled watch cycle error; worker remains alive and will retry. error=%s",
                exc,
            )
            if shutdown_event.wait(min(30, max(1, int(config.watch_interval_seconds)))):
                break
            continue
        if _wait_for_next_cycle_or_ai_resume(shutdown_event, config.watch_interval_seconds, config):
            break
    update_ai_scheduler_state(
        config,
        state="stopping",
        reason_code="shutdown",
        message="Worker shutdown requested; no new AI work will start.",
        current_video=None,
        logger=logger,
    )
    logger.info("Shutdown requested; exiting watch mode.")
    return 0


def _scan_and_process(
    scanner: VideoScanner,
    worker: VideoWorker,
    logger,
    *,
    exclude_videos: set | None = None,
    max_videos: int | None = None,
    queue_only: bool = False,
    shutdown_event: threading.Event | None = None,
) -> int:
    acceptance_lane = load_acceptance_queue_lane(worker.config)
    if _shutdown_requested(shutdown_event):
        update_ai_scheduler_state(
            worker.config,
            state="stopping",
            reason_code="shutdown",
            message="Worker shutdown requested; AI queue was not scanned.",
            current_video=None,
            logger=logger,
        )
        logger.info("Shutdown already requested; skipping scan cycle.")
        return 0
    if _deployment_hold_active(worker.config):
        update_ai_scheduler_state(
            worker.config,
            state="deployment_hold",
            reason_code="deployment_hold",
            message="Deployment hold is active; AI work is preserved but not claimed.",
            current_video=None,
            logger=logger,
        )
        logger.info("Deployment hold is active; no AI work will be claimed.")
        return 0
    if _ai_queue_paused(worker.config) and acceptance_lane is None:
        update_ai_scheduler_state(
            worker.config,
            state="paused",
            reason_code="ai_queue_paused",
            message="AI queue is paused by the user.",
            current_video=None,
            logger=logger,
        )
        logger.info("AI queue is paused; skipping this processing cycle while queue discovery remains active.")
        return 0

    scan_limit = None if exclude_videos or max_videos == 0 else max_videos
    cycle_started_at = time.time()
    update_ai_scheduler_state(
        worker.config,
        state="scanning",
        reason_code="",
        message="Reading the AI queue.",
        current_video=None,
        queue_only=queue_only,
        cycle_started_at=cycle_started_at,
        next_retry_at=0.0,
        logger=logger,
    )
    try:
        if queue_only or acceptance_lane is not None:
            videos = scanner.queued_candidates(max_candidates=scan_limit)
        else:
            videos = scanner.scan(max_candidates=scan_limit)
    except Exception as exc:
        report_ai_scheduler_error(
            worker.config,
            exc,
            message="AI queue scan failed; Worker will retry automatically.",
            logger=logger,
        )
        raise

    scanner_error = getattr(scanner, "last_database_error", "")
    if not isinstance(scanner_error, str):
        scanner_error = ""
    if scanner_error:
        scanner_error_code = getattr(scanner, "last_database_error_code", "")
        if not isinstance(scanner_error_code, str):
            scanner_error_code = ""
        report_ai_scheduler_error(
            worker.config,
            scanner_error,
            reason_code=scanner_error_code or None,
            message="AI queue database is temporarily unavailable; no work was claimed and Worker will retry automatically.",
            logger=logger,
        )
        return 0
    if exclude_videos:
        excluded = {_safe_resolve_path(video) for video in exclude_videos}
        before = len(videos)
        videos = [video for video in videos if _safe_resolve_path(video) not in excluded]
        skipped = before - len(videos)
        if skipped:
            logger.info("Skipped %s video(s) from AI fallback because Mikan official subtitle downloads are pending.", skipped)

    if max_videos is not None and max_videos > 0 and len(videos) > max_videos:
        logger.info("Limiting AI fallback batch from %s to %s video(s).", len(videos), max_videos)
        videos = videos[:max_videos]

    logger.info("Scan found %s video(s) to process.", len(videos))
    if not videos:
        _report_ai_scheduler_cycle_finished(
            worker.config,
            logger,
            processed=0,
            shutdown_event=shutdown_event,
        )
        return 0
    if _shutdown_requested(shutdown_event):
        update_ai_scheduler_state(
            worker.config,
            state="stopping",
            reason_code="shutdown",
            message="Shutdown requested after queue scan; queued work remains untouched.",
            current_video=None,
            processed_last_cycle=0,
            logger=logger,
        )
        logger.info("Shutdown requested after scan; leaving %s video(s) queued.", len(videos))
        return 0

    max_workers = worker.config.max_concurrent_videos
    if bool(getattr(worker.config, "resource_admission_enabled", False)) and max_workers != 1:
        raise RuntimeError(
            "resource admission requires exactly one video lane; refusing an unplanned parallel launch"
        )
    resource_launch_plan = _resource_launch_plan_for_video(
        worker.config,
        videos[0],
        logger,
    )
    if resource_launch_plan is not None and not bool(resource_launch_plan.get("admitted")):
        _report_resource_admission_deferred(
            worker.config,
            videos[0],
            resource_launch_plan,
            logger,
            processed=0,
        )
        return 0
    try:
        queue_state = _open_ai_queue_state(worker.config)
    except Exception as exc:
        report_ai_scheduler_error(
            worker.config,
            exc,
            message="AI queue state could not be opened; no work was claimed and Worker will retry automatically.",
            logger=logger,
        )
        raise
    processed = 0
    if max_workers <= 1 or len(videos) == 1:
        deferred: tuple[Path, dict] | None = None
        try:
            for video in videos:
                if _shutdown_requested(shutdown_event):
                    logger.info("Shutdown requested; stopping before next queued video.")
                    break
                if _ai_queue_paused(worker.config) and acceptance_lane is None:
                    logger.info("AI queue pause requested; current video is complete and no additional video will start.")
                    break
                # Every candidate gets a fresh decision before either the
                # scheduler heartbeat or durable queue state records a claim.
                # Reusing only the first decision would let the second video
                # become running before pressure can defer it.
                plan = resource_launch_plan if video == videos[0] else _resource_launch_plan_for_video(
                    worker.config, video, logger
                )
                if plan is not None and not bool(plan.get("admitted")):
                    deferred = (Path(video), plan)
                    break
                with _AI_REVIEW_REMEDIATION_HANDOFF_LOCK:
                    reserved_command = _active_translation_omission_line_command(
                        worker.config
                    )
                    if reserved_command is None:
                        update_ai_scheduler_state(
                            worker.config,
                            state="processing",
                            reason_code="",
                            message="AI subtitle processing is active.",
                            current_video=video,
                            mark_claim=True,
                            reset_errors=True,
                            logger=logger,
                        )
                        delivery_attempt_id = _mark_queue_running(
                            queue_state,
                            video,
                            worker.config,
                        )
                if reserved_command is not None:
                    logger.info(
                        "Yielding normal AI queue claim to automatic line remediation. "
                        "command=%s path=%s",
                        reserved_command.get("command_id"),
                        reserved_command.get("target"),
                    )
                    break
                ok = _process_video_with_policy(worker, video, logger, resource_launch_plan=plan)
                _mark_queue_result(
                    queue_state,
                    video,
                    ok,
                    worker.config,
                    delivery_attempt_id=delivery_attempt_id,
                )
                processed += 1
                update_ai_scheduler_state(
                    worker.config,
                    state="processing",
                    current_video=None,
                    processed_last_cycle=processed,
                    mark_completed=True,
                    logger=logger,
                )
        except Exception as exc:
            report_ai_scheduler_error(worker.config, exc, logger=logger)
            raise
        finally:
            _close_ai_queue_state(queue_state)
        if deferred is not None:
            deferred_video, deferred_plan = deferred
            _report_resource_admission_deferred(
                worker.config,
                deferred_video,
                deferred_plan,
                logger,
                processed=processed,
            )
            return processed
        _report_ai_scheduler_cycle_finished(
            worker.config,
            logger,
            processed=processed,
            shutdown_event=shutdown_event,
        )
        return processed

    logger.info("Processing videos concurrently. max_workers=%s", max_workers)
    try:
        from worker import VideoWorker

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            pending_videos = iter(videos)
            futures = {}

            def submit_available() -> None:
                while len(futures) < max_workers:
                    if _shutdown_requested(shutdown_event):
                        logger.info("Shutdown requested; not starting additional queued videos.")
                        return
                    if _ai_queue_paused(worker.config) and acceptance_lane is None:
                        logger.info("AI queue pause requested; running videos may finish but no additional video will start.")
                        return
                    with _AI_REVIEW_REMEDIATION_HANDOFF_LOCK:
                        reserved_command = _active_translation_omission_line_command(
                            worker.config
                        )
                        if reserved_command is not None:
                            logger.info(
                                "Yielding concurrent AI queue claim to automatic line remediation. "
                                "command=%s path=%s",
                                reserved_command.get("command_id"),
                                reserved_command.get("target"),
                            )
                            return
                        try:
                            video = next(pending_videos)
                        except StopIteration:
                            return
                        update_ai_scheduler_state(
                            worker.config,
                            state="processing",
                            reason_code="",
                            message="AI subtitle processing is active.",
                            current_video=video,
                            mark_claim=True,
                            reset_errors=True,
                            logger=logger,
                        )
                        delivery_attempt_id = _mark_queue_running(
                            queue_state,
                            video,
                            worker.config,
                        )
                    future = executor.submit(
                        _process_video_with_policy,
                        VideoWorker(worker.config, logger),
                        video,
                        logger,
                    )
                    futures[future] = (video, delivery_attempt_id)

            submit_available()
            if not futures:
                return 0
            while futures:
                completed, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in completed:
                    video, delivery_attempt_id = futures.pop(future)
                    ok = False
                    try:
                        ok = bool(future.result())
                    except Exception as exc:
                        logger.exception("Unhandled concurrent processing error for %s: %s", video, exc)
                    _mark_queue_result(
                        queue_state,
                        video,
                        ok,
                        worker.config,
                        delivery_attempt_id=delivery_attempt_id,
                    )
                    processed += 1
                    update_ai_scheduler_state(
                        worker.config,
                        state="processing",
                        current_video=None,
                        processed_last_cycle=processed,
                        mark_completed=True,
                        logger=logger,
                    )
                submit_available()
    except Exception as exc:
        report_ai_scheduler_error(worker.config, exc, logger=logger)
        raise
    finally:
        _close_ai_queue_state(queue_state)
    _report_ai_scheduler_cycle_finished(
        worker.config,
        logger,
        processed=processed,
        shutdown_event=shutdown_event,
    )
    return processed


def _report_ai_scheduler_cycle_finished(
    config,
    logger,
    *,
    processed: int,
    shutdown_event: threading.Event | None,
) -> None:
    if _shutdown_requested(shutdown_event):
        state = "stopping"
        reason_code = "shutdown"
        message = "Worker shutdown requested; no new AI work will start."
    elif _ai_queue_paused(config):
        state = "paused"
        reason_code = "ai_queue_paused"
        message = "AI queue is paused by the user."
    elif _deployment_hold_active(config):
        state = "deployment_hold"
        reason_code = "deployment_hold"
        message = "Deployment hold is active; AI work is preserved but not claimed."
    else:
        state = "idle"
        reason_code = ""
        message = "AI scheduler is ready for queued work."
    update_ai_scheduler_state(
        config,
        state=state,
        reason_code=reason_code,
        message=message,
        current_video=None,
        processed_last_cycle=processed,
        mark_success=True,
        reset_errors=True,
        logger=logger,
    )


def _resource_launch_plan_for_video(config, video, logger):
    if not bool(getattr(config, "resource_admission_enabled", False)):
        return None
    try:
        from resource_runtime import build_resource_launch_plan

        plan = build_resource_launch_plan(
            config,
            Path(video),
            stage="transcription",
            running_gpu_jobs=0,
        )
        reasons = {str(value) for value in (plan.get("reason_codes") or ())}
        vram_blocked = bool(
            reasons
            & {
                "vram_primary_insufficient",
                "vram_no_model_route_fits",
            }
        )
        if not bool(plan.get("admitted")) and vram_blocked:
            from ollama_lifecycle import unload_managed_translation_models

            released = unload_managed_translation_models(config, logger)
            if released:
                logger.info(
                    "Retrying resource admission after managed Ollama unload. video=%s models=%s",
                    video,
                    released,
                )
                plan = build_resource_launch_plan(
                    config,
                    Path(video),
                    stage="transcription",
                    running_gpu_jobs=0,
                )
        return plan
    except Exception as exc:  # noqa: BLE001 - unknown resource state must fail closed.
        logger.error("Resource admission probe failed; leaving queue item unclaimed. video=%s error=%s", video, exc)
        return {
            "schema_version": 1,
            "contract": "resource-launch-plan-v1",
            "admitted": False,
            "reason_codes": ["resource_admission_probe_failed"],
            "retry_at": time.time() + 60.0,
        }


def _report_resource_admission_deferred(
    config,
    video: Path,
    plan: dict,
    logger,
    *,
    processed: int,
) -> None:
    retry_at = float(plan.get("retry_at") or 0.0)
    reasons = plan.get("reason_codes") or []
    retry_at = retry_at if retry_at > time.time() else time.time() + 60.0
    update_ai_scheduler_state(
        config,
        state="resource_deferred",
        reason_code="resource_admission_deferred",
        message=f"AI claim deferred by resource admission: {reasons}",
        current_video=None,
        processed_last_cycle=processed,
        next_retry_at=retry_at,
        logger=logger,
    )
    logger.info(
        "Resource admission deferred AI queue claim. video=%s retry_at=%s reasons=%s",
        video,
        retry_at,
        reasons,
    )


def _process_video_with_policy(worker: VideoWorker, video, logger, *, resource_launch_plan=None) -> bool:
    config = worker.config
    resource_enabled = bool(getattr(config, "resource_admission_enabled", False))
    if resource_enabled and (
        not isinstance(resource_launch_plan, dict)
        or resource_launch_plan.get("admitted") is not True
    ):
        logger.error("Resource-enabled AI launch has no admitted plan; refusing worker start. video=%s", video)
        return False
    if resource_enabled and not bool(getattr(config, "ai_process_isolation_enabled", False)):
        logger.error("Resource-enabled AI launch requires process isolation; refusing worker start. video=%s", video)
        return False
    if not bool(getattr(config, "ai_process_isolation_enabled", False)):
        try:
            return bool(worker.process(video))
        except Exception as exc:  # noqa: BLE001 - one bad AI job must not stop the scheduler.
            logger.exception("Unhandled AI processing error for %s: %s", video, exc)
            return False

    return _process_video_subprocess(
        config,
        video,
        logger,
        resource_launch_plan=resource_launch_plan,
    )


def _process_video_subprocess(config, video, logger, *, resource_launch_plan=None) -> bool:
    config_path = getattr(config, "config_path", None)
    if not config_path:
        if bool(getattr(config, "resource_admission_enabled", False)):
            logger.error(
                "Resource-enabled isolated AI launch has no config_path; refusing in-process fallback. video=%s",
                video,
            )
            return False
        logger.warning("AI process isolation requested but config_path is unavailable; processing in current worker. video=%s", video)
        from worker import VideoWorker

        try:
            return bool(VideoWorker(config, logger).process(video))
        except Exception as exc:  # noqa: BLE001 - keep queue runner alive.
            logger.exception("Unhandled AI processing error for %s: %s", video, exc)
            return False

    timeout = int(getattr(config, "ai_subprocess_timeout_seconds", 0) or 0)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--config",
        str(config_path),
        "--process-video",
        "--video-path",
        str(video),
    ]
    logger.info(
        "Starting isolated AI subprocess. video=%s timeout_seconds=%s",
        video,
        timeout if timeout > 0 else "none",
    )
    environment = None
    if resource_launch_plan is not None:
        from resource_runtime import serialize_launch_plan

        environment = os.environ.copy()
        environment["ANIME_RESOURCE_LAUNCH_PLAN"] = serialize_launch_plan(resource_launch_plan)
    try:
        completed = subprocess.run(
            command,
            check=False,
            timeout=timeout if timeout > 0 else None,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        detail = f"AI subprocess timed out and was terminated after {timeout}s"
        _persist_isolated_resource_failure(config, Path(video), "resource_runtime", detail, logger)
        logger.error("%s. video=%s", detail, video)
        return False
    except OSError as exc:
        _persist_isolated_resource_failure(
            config,
            Path(video),
            "resource_runtime",
            f"AI subprocess launch failed: {exc}",
            logger,
        )
        logger.exception("Failed to start AI subprocess for %s: %s", video, exc)
        return False

    if completed.returncode == 0:
        logger.info("Isolated AI subprocess completed. video=%s", video)
        return True

    if completed.returncode in {-9, 137}:
        detail = f"isolated resource process was killed (SIGKILL/OOM), returncode={completed.returncode}"
        try:
            from resource_runtime import record_resource_oom

            record_resource_oom(
                config,
                Path(video),
                detail,
            )
        except Exception as exc:  # noqa: BLE001 - OOM telemetry cannot mask queue failure.
            logger.warning("Unable to persist resource OOM cooldown: %s", exc)
        _persist_isolated_resource_failure(
            config,
            Path(video),
            "resource_runtime",
            detail,
            logger,
        )
        logger.error(
            "AI subprocess was killed, likely by OOM or GPU/runtime failure. video=%s returncode=%s",
            video,
            completed.returncode,
        )
    else:
        logger.error("AI subprocess failed. video=%s returncode=%s", video, completed.returncode)
    return False


def _persist_isolated_resource_failure(config, video: Path, stage: str, detail: str, logger) -> None:
    """Persist parent-observed child failure so queue retry routing is exact."""

    try:
        from ai_failure_markers import mark_ai_failure

        mark_ai_failure(config, video, stage, detail)
    except Exception as exc:  # noqa: BLE001 - the queue database remains the primary record.
        logger.warning("Unable to persist isolated AI failure marker video=%s error=%s", video, exc)
    if not getattr(config, "scanner_cache_enabled", True) or not getattr(
        config, "scanner_queue_enabled", True
    ):
        return
    state = None
    try:
        from scan_state import ScanStateStore

        state = ScanStateStore.from_config(config)
        state.update_ai_job_stage(video, stage, "failed", detail)
        state.commit()
    except Exception as exc:  # noqa: BLE001 - preserve the subprocess result and resource cooldown.
        if state is not None:
            try:
                state.rollback()
            except Exception:
                pass
        logger.warning("Unable to persist isolated AI resource failure video=%s error=%s", video, exc)
    finally:
        if state is not None:
            state.close()


def _drain_ai_queue_between_cycles(
    scanner: VideoScanner,
    worker: VideoWorker,
    logger,
    config,
    *,
    shutdown_event: threading.Event | None = None,
) -> None:
    if not bool(getattr(config, "auto_ai_drain_queue_between_cycles", True)):
        return
    if not getattr(config, "auto_enable_ai_fallback", True):
        return

    while not _shutdown_requested(shutdown_event):
        if _mikan_redownload_blocks_ai(config):
            logger.info("Stopping AI queue drain because Mikan redownload is pending or active.")
            return
        if _yield_normal_ai_queue_to_review_remediation(config, logger):
            return
        processed = _scan_and_process(
            scanner,
            worker,
            logger,
            max_videos=config.auto_ai_max_videos_per_cycle,
            queue_only=True,
            shutdown_event=shutdown_event,
        )
        if processed <= 0:
            return
        logger.info("AI queue drain processed %s video(s); checking for more queued work.", processed)


def _auto_run_once(
    scanner: VideoScanner,
    worker: VideoWorker,
    config,
    logger,
    *,
    mikan_managed_externally: bool = False,
    ai_scan_managed_externally: bool = False,
    shutdown_event: threading.Event | None = None,
) -> None:
    if _shutdown_requested(shutdown_event):
        logger.info("Shutdown already requested; skipping integrated auto cycle.")
        return
    if _deployment_hold_active(config):
        update_ai_scheduler_state(
            config,
            state="deployment_hold",
            reason_code="deployment_hold",
            message="Deployment hold is active; AI work is preserved but not claimed.",
            current_video=None,
            logger=logger,
        )
        logger.info("Deployment hold is active; integrated auto cycle is paused.")
        return
    if _yield_normal_ai_queue_to_review_remediation(config, logger):
        return

    if mikan_managed_externally:
        if config.auto_enable_ai_fallback:
            if _mikan_redownload_blocks_ai(config):
                update_ai_scheduler_state(
                    config,
                    state="blocked",
                    reason_code="mikan_redownload",
                    message="AI scheduling is waiting for the active Mikan redownload operation.",
                    current_video=None,
                    logger=logger,
                )
                logger.info("Integrated auto mode skipped AI fallback because Mikan redownload is pending or active.")
                return
            logger.info("Integrated auto mode running AI queue while background Mikan/qB worker is active.")
            _scan_and_process(
                scanner,
                worker,
                logger,
                max_videos=config.auto_ai_max_videos_per_cycle,
                queue_only=ai_scan_managed_externally,
                shutdown_event=shutdown_event,
            )
            _drain_ai_queue_between_cycles(
                scanner,
                worker,
                logger,
                config,
                shutdown_event=shutdown_event,
            )
        else:
            update_ai_scheduler_state(
                config,
                state="disabled",
                reason_code="ai_fallback_disabled",
                message="Automatic AI fallback is disabled by configuration.",
                current_video=None,
                logger=logger,
            )
            logger.info("Integrated auto mode skipped AI fallback because auto_enable_ai_fallback=false.")
        return

    if getattr(config, "auto_mikan_parallel_with_ai", False):
        _auto_run_once_parallel(
            scanner,
            worker,
            config,
            logger,
            ai_scan_managed_externally=ai_scan_managed_externally,
            shutdown_event=shutdown_event,
        )
        return

    if getattr(config, "auto_ai_run_before_mikan", False):
        if config.auto_enable_ai_fallback:
            logger.info("Integrated auto mode running AI queue before Mikan because auto_ai_run_before_mikan=true.")
            _scan_and_process(
                scanner,
                worker,
                logger,
                max_videos=config.auto_ai_max_videos_per_cycle,
                queue_only=ai_scan_managed_externally,
                shutdown_event=shutdown_event,
            )
            _drain_ai_queue_between_cycles(
                scanner,
                worker,
                logger,
                config,
                shutdown_event=shutdown_event,
            )
        else:
            logger.info("Integrated auto mode skipped pre-Mikan AI because auto_enable_ai_fallback=false.")

    official_candidate_videos: set = set()
    if config.mikan_enabled:
        from mikan_worker import MikanWorker

        mikan_worker = MikanWorker(config, logger)
        mikan_worker.run_once()
        official_candidate_videos = mikan_worker.videos_with_available_official_subtitles()
        if getattr(config, "require_ai_subtitles", False) and official_candidate_videos:
            logger.info(
                "AI subtitle requirement enabled; keeping %s Mikan candidate video(s) eligible for AI fallback.",
                len(official_candidate_videos),
            )
            official_candidate_videos = set()
    else:
        logger.info("Integrated auto mode skipped Mikan because mikan_enabled=false.")

    if getattr(config, "auto_ai_run_before_mikan", False):
        return

    if not config.auto_enable_ai_fallback:
        logger.info("Integrated auto mode skipped AI fallback because auto_enable_ai_fallback=false.")
        return

    _scan_and_process(
        scanner,
        worker,
        logger,
        exclude_videos=official_candidate_videos,
        max_videos=config.auto_ai_max_videos_per_cycle,
        queue_only=ai_scan_managed_externally,
        shutdown_event=shutdown_event,
    )
    _drain_ai_queue_between_cycles(
        scanner,
        worker,
        logger,
        config,
        shutdown_event=shutdown_event,
    )


def _auto_run_once_parallel(
    scanner: VideoScanner,
    worker: VideoWorker,
    config,
    logger,
    *,
    ai_scan_managed_externally: bool = False,
    shutdown_event: threading.Event | None = None,
) -> None:
    future = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        if config.mikan_enabled:
            logger.info("Integrated auto mode running Mikan/qB in parallel with AI queue.")
            future = executor.submit(_run_mikan_integrated, config, logger)
        else:
            logger.info("Integrated auto mode skipped Mikan because mikan_enabled=false.")

        if config.auto_enable_ai_fallback:
            if _mikan_redownload_blocks_ai(config):
                logger.info("Integrated auto mode skipped AI fallback because Mikan redownload is pending or active.")
            else:
                _scan_and_process(
                    scanner,
                    worker,
                    logger,
                    max_videos=config.auto_ai_max_videos_per_cycle,
                    queue_only=ai_scan_managed_externally,
                    shutdown_event=shutdown_event,
                )
                _drain_ai_queue_between_cycles(
                    scanner,
                    worker,
                    logger,
                    config,
                    shutdown_event=shutdown_event,
                )
        else:
            logger.info("Integrated auto mode skipped AI fallback because auto_enable_ai_fallback=false.")

        if future is not None:
            try:
                future.result()
            except Exception as exc:
                logger.exception("Unhandled parallel Mikan/qB error: %s", exc)


def _run_mikan_integrated(config, logger) -> set:
    if not config.mikan_enabled:
        logger.info("Integrated auto mode skipped Mikan because mikan_enabled=false.")
        return set()

    from mikan_worker import MikanWorker

    mikan_worker = MikanWorker(config, logger)
    mikan_worker.run_once()
    official_candidate_videos = mikan_worker.videos_with_available_official_subtitles()
    if getattr(config, "require_ai_subtitles", False) and official_candidate_videos:
        logger.info(
            "AI subtitle requirement enabled; keeping %s Mikan candidate video(s) eligible for AI fallback.",
            len(official_candidate_videos),
        )
        return set()
    return official_candidate_videos


def _cleanup_generated_artifacts(config, logger) -> None:
    from cleanup_generated import cleanup_generated_artifacts

    cleanup_generated_artifacts(config, logger)


def _refresh_ass_exports(scanner: VideoScanner, worker: VideoWorker, logger) -> None:
    refreshed = 0
    skipped = 0
    for video in scanner.scan_all(force_full=True):
        if worker.refresh_ass(video):
            refreshed += 1
        else:
            skipped += 1
    logger.info("ASS refresh complete. refreshed=%s skipped=%s", refreshed, skipped)


def _refresh_ai_queue_state(scanner: VideoScanner, logger) -> None:
    refreshed = scanner.refresh_queue(force_full=True)
    logger.info("AI queue state refresh complete. scanned=%s", refreshed)


def _requeue_stale_ai_running(config, logger) -> int:
    """Requeue expired running rows without walking or reclassifying media."""

    acceptance_lane = load_acceptance_queue_lane(config)
    state = _open_ai_queue_state(config)
    if state is None:
        logger.info("Stale AI running requeue skipped because the queue state is disabled.")
        return 0
    stale_after_seconds = int(
        getattr(
            config,
            "ai_queue_stage_stale_seconds",
            getattr(config, "ai_queue_running_stale_seconds", 21600),
        )
    )
    try:
        if acceptance_lane is None:
            count = state.requeue_stale_running(
                stale_after_seconds,
                reconcile_completed=False,
            )
        else:
            count = state.requeue_acceptance_running_targets(
                acceptance_lane.targets,
                stale_after_seconds=stale_after_seconds,
                message="Timed-out acceptance lane job was safely requeued",
            )
        state.commit()
    except BaseException:
        state.rollback()
        raise
    finally:
        state.close()
    logger.warning(
        "Stale AI running requeue complete. stale_after_seconds=%s requeued=%s",
        stale_after_seconds,
        count,
    )
    return count


def _mikan_run_once(config, logger, *, process_completed: bool = True) -> None:
    from mikan_worker import MikanWorker

    MikanWorker(config, logger).run_once(process_completed=process_completed)


def _mikan_process_completed_once(config, logger) -> None:
    from mikan_worker import MikanWorker

    if not getattr(config, "mikan_extract_completed", False):
        return
    worker = MikanWorker(config, logger)
    worker.consume_completed_state_update_request()
    worker.process_completed_downloads(required=False)


def _mikan_dry_run(config, logger) -> None:
    from mikan_worker import MikanWorker

    MikanWorker(config, logger).dry_run()


def _mikan_process_completed(config, logger) -> None:
    from mikan_worker import MikanWorker

    MikanWorker(config, logger).process_completed_downloads()


def _mikan_reset_all(config, logger) -> None:
    from mikan_worker import MikanWorker

    result = MikanWorker(config, logger).reset_all_state_and_enqueue(defer_if_busy=True)
    if result.get("deferred"):
        logger.warning(
            "Mikan reset-all deferred. request_path=%s reason=%s",
            result.get("request_path"),
            result.get("reason") or "-",
        )
        return
    logger.warning("Mikan reset-all complete. queued=%s reset=%s", result["queued"], result["reset"])


def _mikan_redownload_all(config, logger, *, delete_files: bool = False) -> None:
    from mikan_worker import MikanWorker

    result = MikanWorker(config, logger).redownload_all_torrents_and_enqueue(
        defer_if_busy=True,
        delete_files=delete_files,
    )
    if result.get("deferred"):
        logger.warning(
            "Mikan redownload-all deferred. request_path=%s delete_files=%s reason=%s",
            result.get("request_path"),
            result.get("delete_files"),
            result.get("reason") or "-",
        )
        return
    if result.get("already_running"):
        logger.warning(
            "Mikan redownload-all already running; duplicate request ignored. active_path=%s delete_files=%s reason=%s",
            result.get("active_path"),
            result.get("delete_files"),
            result.get("reason") or "-",
        )
        return
    logger.warning(
        "Mikan redownload-all complete. deleted_torrents=%s delete_files=%s queued=%s reset=%s",
        result["deleted_torrents"],
        result["delete_files"],
        result["queued"],
        result["reset"],
    )


def _mikan_requeue_failed_extracts(config, logger) -> None:
    from mikan_worker import requeue_failed_mikan_extract_jobs

    count = requeue_failed_mikan_extract_jobs(config, include_terminal=False)
    logger.warning("Mikan failed subtitle extraction jobs requeued. count=%s", count)


def _safe_resolve_path(path) -> object:
    try:
        return path.resolve()
    except OSError:
        return path


def _run_safety_checks(config, logger) -> bool:
    try:
        from safety import RuntimeSafetyError, ensure_runtime_safety

        ensure_runtime_safety(config, logger)
        return True
    except RuntimeSafetyError as exc:
        logger.error("Startup safety check failed: %s", exc)
        return False


def _start_event_watcher(config, logger):
    if not getattr(config, "scanner_event_watch_enabled", False):
        return None
    from event_watcher import start_event_watcher

    return start_event_watcher(config, logger)


def _start_background_ai_scan(
    config,
    logger,
    shutdown_event: threading.Event,
    *,
    event_watcher=None,
):
    if not (
        getattr(config, "scanner_cache_enabled", True)
        and getattr(config, "scanner_queue_enabled", True)
    ):
        logger.info("Background AI scanner disabled because the persistent AI queue is disabled.")
        return None

    thread = threading.Thread(
        target=_background_ai_scan_loop,
        args=(config, logger, shutdown_event, event_watcher),
        daemon=True,
        name="ai-queue-scan-watch",
    )
    thread.start()
    interval, startup_delay = _background_ai_scan_schedule(
        config,
        event_watcher=event_watcher,
    )
    logger.info(
        "Background AI scanner started. interval=%s startup_delay=%s incremental=%s io_yield_every=%s io_yield_seconds=%s",
        interval,
        startup_delay,
        bool(getattr(config, "scanner_incremental_scan_enabled", False)),
        int(getattr(config, "scanner_walk_yield_every_entries", 256) or 256),
        float(getattr(config, "scanner_walk_yield_seconds", 0.025) or 0.0),
    )
    return thread


def _start_background_ai_ledger_backfill(
    config,
    logger,
    shutdown_event: threading.Event,
):
    if not (
        getattr(config, "scanner_cache_enabled", True)
        and getattr(config, "scanner_queue_enabled", True)
        and getattr(config, "scanner_active_queue_ledger_backfill_enabled", True)
    ):
        logger.info("Background AI queue ledger backfill is disabled.")
        return None

    thread = threading.Thread(
        target=_background_ai_ledger_backfill_loop,
        args=(config, logger, shutdown_event),
        daemon=True,
        name="ai-queue-ledger-backfill",
    )
    thread.start()
    logger.info(
        "Background AI queue ledger backfill started. interval=%s no_progress_interval=%s batch_size=%s",
        max(
            1,
            int(
                getattr(
                    config,
                    "scanner_active_queue_ledger_backfill_interval_seconds",
                    10,
                )
                or 10
            ),
        ),
        max(
            1,
            int(
                getattr(
                    config,
                    "scanner_active_queue_ledger_backfill_no_progress_seconds",
                    300,
                )
                or 300
            ),
        ),
        max(
            1,
            int(
                getattr(
                    config,
                    "scanner_active_queue_ledger_backfill_batch_size",
                    250,
                )
                or 250
            ),
        ),
    )
    return thread


def _start_background_control_commands(config, logger, shutdown_event: threading.Event):
    from control_state import initialize_control_state

    initialize_control_state(config)
    thread = threading.Thread(
        target=_background_control_command_loop,
        args=(config, logger, shutdown_event),
        daemon=True,
        name="worker-control-command-inbox",
    )
    thread.start()
    logger.info("Worker control command inbox started. path=%s", getattr(config, "control_state_path", "control_state.sqlite3"))
    return thread


def _reconcile_stale_control_commands(config, logger) -> None:
    from control_state import reconcile_stale_running_commands

    try:
        reconciled = reconcile_stale_running_commands(config)
        if reconciled:
            logger.warning("Failed stale running Worker command(s) closed safely. count=%s", reconciled)
    except Exception as exc:  # noqa: BLE001 - command recovery must not stop the inbox.
        logger.error("Unable to reconcile stale Worker commands: %s", exc)


def _background_control_command_loop(config, logger, shutdown_event: threading.Event) -> None:
    from control_state import (
        claim_next_command,
        finish_command,
        ingest_command_inbox,
    )

    worker_id = f"worker:{os.getpid()}"
    _reconcile_stale_control_commands(config, logger)
    # A command that was younger than the stale cutoff during a fast restart
    # used to remain running forever because recovery only ran at startup.
    # Recheck at low frequency while this single-consumer inbox is alive.  The
    # reconciler remains fail-closed and never replays an arbitrary command.
    next_reconcile_at = time.monotonic() + _CONTROL_COMMAND_RECONCILE_INTERVAL_SECONDS
    while not shutdown_event.is_set():
        command = None
        try:
            now = time.monotonic()
            if now >= next_reconcile_at:
                _reconcile_stale_control_commands(config, logger)
                next_reconcile_at = now + _CONTROL_COMMAND_RECONCILE_INTERVAL_SECONDS
            ingested = ingest_command_inbox(config)
            if ingested:
                logger.info("Ingested Worker control command(s) from atomic inbox. count=%s", ingested)
            hold_active = _deployment_hold_active(config)
            command = claim_next_command(
                config,
                worker_id=worker_id,
                # A cooperative extraction cancel is safe during deployment
                # hold and may be required to reach the maintenance boundary.
                allowed_actions={"system.health_probe", "mikan.cancel_extract"} if hold_active else None,
            )
            if command is None:
                if not hold_active:
                    _ensure_configured_ai_failed_retry_sweep(config, logger)
                    _advance_ai_failed_retry_sweep(config, logger)
                    _advance_ai_quality_review_autopilot(config, logger)
                    _advance_target_review_autopilot(config, logger)
                if shutdown_event.wait(1.0):
                    break
                continue
            result = _execute_control_command(config, logger, command.action, command.target, command.parameters)
            finish_command(config, command.command_id, result=result)
        except Exception as exc:  # noqa: BLE001 - a malformed command must never stop the worker.
            logger.exception(
                "Worker control command failed. command_id=%s action=%s error=%s",
                getattr(command, "command_id", "-"),
                getattr(command, "action", "-"),
                exc,
            )
            if command is not None:
                try:
                    finish_command(config, command.command_id, error=str(exc))
                except Exception as finish_error:  # noqa: BLE001
                    logger.error("Unable to record failed control command: %s", finish_error)
            if shutdown_event.wait(1.0):
                break


def _execute_control_command(config, logger, action: str, target: str, parameters: dict) -> dict[str, object]:
    normalized = str(action or "").strip().casefold()
    if normalized == "system.ai_scheduler_retry":
        state = request_ai_scheduler_retry(config, logger=logger)
        AI_SCHEDULER_WAKE_EVENT.set()
        return {
            "action": normalized,
            "applied": True,
            "retry_requested_at": float(state.get("retry_requested_at") or time.time()),
        }
    if normalized == "system.health_probe":
        checks: dict[str, str] = {}
        database_paths = {
            "scanner": Path(config.work_path) / str(getattr(config, "scanner_state_path", "scanner_state.sqlite3")),
            "mikan": Path(config.work_path) / str(getattr(config, "mikan_state_db_path", "mikan_state.sqlite3")),
            "control": Path(config.work_path) / str(getattr(config, "control_state_path", "control_state.sqlite3")),
        }
        for name, path in database_paths.items():
            if not path.is_file():
                checks[name] = "missing"
                continue
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
                checks[name] = str(row[0] if row else "unknown")
            finally:
                connection.close()
        failed = {name: value for name, value in checks.items() if value not in {"ok", "missing"}}
        if failed:
            raise RuntimeError(f"SQLite health probe failed: {failed}")
        return {
            "action": normalized,
            "worker_pid": os.getpid(),
            "deployment_hold": _deployment_hold_active(config),
            "databases": checks,
        }
    if normalized in {"ai.retry", "ai.force", "ai.pause", "ai.skip", "ai.prioritize", "ai.recover"}:
        from scan_state import ScanStateStore

        if not target:
            raise ValueError(f"{normalized} requires a video path target")
        path = _validated_control_target_path(config, target, require_file=True)
        state = ScanStateStore.from_config(config)
        recovered = False
        try:
            def apply_queue_action() -> None:
                nonlocal recovered
                if normalized == "ai.retry":
                    state.retry_ai_queue_candidate(path)
                elif normalized == "ai.force":
                    state.force_ai_queue_candidate(path)
                elif normalized == "ai.pause":
                    state.pause_ai_queue_candidate(path)
                elif normalized == "ai.skip":
                    state.skip_ai_queue_candidate(path)
                elif normalized == "ai.recover":
                    recovered = state.recover_stale_ai_queue_candidate(
                        path,
                        int(getattr(config, "ai_queue_running_stale_seconds", 21600) or 21600),
                    )
                    if not recovered:
                        raise ValueError("Running AI task is not stale or is no longer running")
                else:
                    state.prioritize_ai_queue_candidate(path)

            _commit_ai_queue_state_write(state, apply_queue_action, attempts=5)
        finally:
            state.close()
        return {"action": normalized, "target": target, "applied": True}

    if normalized in {"system.ai_queue_pause", "system.ai_queue_resume"}:
        from safe_files import atomic_write_text

        paused = normalized.endswith("pause")
        now = time.time()
        payload = {
            "paused": paused,
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "updated_at": now,
            "requested_by": "worker-control-command",
        }
        atomic_write_text(_ai_control_path(config), json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return {"action": normalized, "paused": paused, "applied": True}

    if normalized == "mikan.requeue_failed_extracts":
        from mikan_worker import requeue_failed_mikan_extract_jobs

        count = requeue_failed_mikan_extract_jobs(config, include_terminal=False)
        return {"action": normalized, "requeued": count}

    if normalized == "mikan.cancel_extract":
        from mikan_worker import request_mikan_extract_cancel

        job_key = str(parameters.get("job_key") or target or "").strip()
        return {
            "action": normalized,
            **request_mikan_extract_cancel(config, job_key=job_key),
        }

    if normalized == "mikan.requeue_extract":
        from mikan_worker import requeue_mikan_extract_job

        job_key = str(parameters.get("job_key") or target or "").strip()
        if not job_key:
            raise ValueError("mikan.requeue_extract requires a job_key")
        requeued = requeue_mikan_extract_job(config, job_key=job_key)
        if not requeued:
            raise ValueError("Extraction job is not in a retryable failed state")
        return {"action": normalized, "job_key": job_key, "requeued": True}

    if normalized == "mikan.process_completed":
        from mikan_worker import MikanWorker

        processed = MikanWorker(config, logger).process_completed_downloads(required=False)
        return {"action": normalized, "processed": int(processed or 0)}

    if normalized == "mikan.request_reset_all":
        from mikan_worker import MikanWorker

        result = MikanWorker(config, logger).request_reset_all(
            reason="Requested through the Worker control command inbox."
        )
        return {"action": normalized, **result}

    if normalized == "mikan.request_redownload_all":
        from mikan_worker import MikanWorker

        if bool(parameters.get("delete_files", False)):
            raise ValueError("Deleting downloaded files is forbidden by the media read-only safety contract")
        result = MikanWorker(config, logger).request_redownload_all(
            delete_files=False,
            reason="Requested through the Worker control command inbox.",
        )
        return {"action": normalized, **result}

    if normalized == "mikan.cancel_redownload":
        from mikan_worker import request_mikan_redownload_cancel

        return {"action": normalized, **request_mikan_redownload_cancel(config)}

    if normalized == "system.ai_failed_retry_sweep":
        operation, sweep_parameters = _validated_ai_failed_retry_sweep_parameters(parameters)
        if operation == "preview":
            return {
                "action": normalized,
                "operation": operation,
                **_preview_ai_failed_retry_sweep(config, sweep_parameters),
            }
        from control_state import (
            create_auto_remediation_campaign,
            get_auto_remediation_campaign,
            update_auto_remediation_campaign,
        )

        if operation == "start":
            campaign = create_auto_remediation_campaign(
                config,
                campaign_key=str(sweep_parameters["campaign_key"]),
                parameters=sweep_parameters,
            )
            return {"action": normalized, "operation": operation, "campaign": campaign}
        campaign_id = str(sweep_parameters.get("campaign_id") or "")
        campaign = get_auto_remediation_campaign(config, campaign_id)
        if campaign is None:
            raise ValueError(f"auto remediation campaign does not exist: {campaign_id}")
        target_state = {
            "pause": "paused",
            "resume": "running",
            "cancel": "cancelled",
        }[operation]
        campaign = update_auto_remediation_campaign(
            config,
            campaign_id,
            state=target_state,
            next_run_at=time.time() if target_state == "running" else None,
        )
        return {"action": normalized, "operation": operation, "campaign": campaign}

    if normalized == "system.ai_retry_all_failures":
        from scan_state import ScanStateStore

        state = ScanStateStore.from_config(config)
        ai_requeued = 0
        try:
            def retry_ai_failures_only() -> None:
                nonlocal ai_requeued
                ai_requeued = state.retry_all_failed_ai_queue_candidates()

            _commit_ai_queue_state_write(state, retry_ai_failures_only, attempts=5)
        finally:
            state.close()
        return {"action": normalized, "ai_requeued": ai_requeued}

    if normalized == "system.retry_all_failures":
        from mikan_worker import requeue_failed_mikan_extract_jobs
        from scan_state import ScanStateStore

        state = ScanStateStore.from_config(config)
        ai_requeued = 0
        try:
            def retry_ai_failures() -> None:
                nonlocal ai_requeued
                ai_requeued = state.retry_all_failed_ai_queue_candidates()

            _commit_ai_queue_state_write(state, retry_ai_failures, attempts=5)
        finally:
            state.close()
        # Terminal extraction failures are explicitly retryable=false and have
        # review items. A bulk retry must not bypass that safety boundary; they
        # remain available through the single-item/review resolution path.
        extraction_requeued = requeue_failed_mikan_extract_jobs(config, include_terminal=False)
        return {
            "action": normalized,
            "ai_requeued": ai_requeued,
            "extraction_requeued": int(extraction_requeued or 0),
        }

    if normalized in {"series.lock", "series.match", "series.glossary_upsert", "series.glossary_delete"}:
        from series_metadata import SeriesMetadataStore

        series_path = _validated_control_target_path(config, target, require_file=False)
        with SeriesMetadataStore.from_config(config) as store:
            if normalized == "series.lock":
                changed = store.set_locked(series_path, bool(parameters.get("locked")))
                if not changed:
                    raise ValueError(f"Series profile does not exist: {series_path}")
                return {"action": normalized, "target": str(series_path), "locked": bool(parameters.get("locked"))}
            if normalized == "series.match":
                provider = str(parameters.get("provider") or "anilist").strip() or "anilist"
                provider_id = str(parameters.get("provider_id") or "").strip()
                title = str(parameters.get("title") or "").strip()
                if not provider_id or not title:
                    raise ValueError("series.match requires provider_id and title")
                profile = store.set_manual_match(
                    series_path,
                    provider=provider,
                    provider_id=provider_id,
                    canonical_title=title,
                    locked=True,
                )
                return {
                    "action": normalized,
                    "target": str(series_path),
                    "provider": profile.provider,
                    "provider_id": profile.provider_id,
                    "title": profile.canonical_title,
                }
            source_text = str(parameters.get("source_text") or "").strip()
            if not source_text:
                raise ValueError(f"{normalized} requires source_text")
            if normalized == "series.glossary_upsert":
                store.upsert_glossary_term(
                    series_path,
                    source_text,
                    str(parameters.get("target_text") or "").strip(),
                    term_type=str(parameters.get("term_type") or "term").strip() or "term",
                    locked=True,
                    source="manual",
                )
                return {"action": normalized, "target": str(series_path), "source_text": source_text}
            deleted = store.delete_glossary_term(series_path, source_text)
            return {
                "action": normalized,
                "target": str(series_path),
                "source_text": source_text,
                "deleted": deleted,
            }

    maintenance_commands = {
        "system.refresh_ass": [sys.executable, "/app/main.py", "--config", "/app/config.yaml", "--refresh-ass"],
        "system.cleanup_generated": [
            sys.executable,
            "/app/main.py",
            "--config",
            "/app/config.yaml",
            "--cleanup-generated-artifacts",
        ],
        "system.refresh_ai_queue_state": [
            sys.executable,
            "/app/main.py",
            "--config",
            "/app/config.yaml",
            "--refresh-ai-queue-state",
        ],
        "system.backup_state": [sys.executable, "/app/backup_state.py", "--config", "/app/config.yaml"],
        "system.database_maintenance": [
            sys.executable,
            "/app/database_maintenance.py",
            "--config",
            "/app/config.yaml",
            "--apply",
            "--wait-seconds",
            "900",
        ],
        "series.sync": [sys.executable, "/app/series_metadata_sync.py", "--config", "/app/config.yaml"],
    }
    if normalized in maintenance_commands:
        timeout_seconds = 1800 if normalized == "system.database_maintenance" else 900
        output = _run_control_subprocess(maintenance_commands[normalized], timeout_seconds=timeout_seconds)
        return {"action": normalized, "output": output}

    if normalized == "review.auto_rebuild_target_candidates":
        from control_state import get_review_item

        review_id = str(parameters.get("review_id") or target or "").strip()
        if not review_id:
            raise ValueError("review.auto_rebuild_target_candidates requires review_id")
        review = get_review_item(config, review_id)
        if review is None:
            raise ValueError(f"review item does not exist: {review_id}")
        if str(review.get("kind") or "") != "target_ambiguity":
            raise ValueError(f"review item is not a target ambiguity: {review_id}")
        if str(review.get("status") or "") != "open":
            raise ValueError(f"review item is no longer open: {review_id}")
        original_source_ids = {
            int(value)
            for value in (review.get("diagnosis") or {}).get("bangumi_ids", [])
            if str(value or "").strip().isdigit() and int(value) > 0
        }
        result = _auto_rebuild_review_target_candidates(
            config,
            review,
            review_id=review_id,
        )
        selected_source_id = str(result.get("source_id") or "").strip()
        if (
            not selected_source_id.isdigit()
            or int(selected_source_id) not in original_source_ids
        ):
            result["auto_resolved"] = False
            result["resolution_skipped"] = "source_not_in_original_diagnosis"
            return result
        resolution = _resolve_automatic_target_review(
            config,
            logger,
            review_id=review_id,
            result=result,
            original_source_ids=original_source_ids,
            policy_revision=str(parameters.get("policy_revision") or _TARGET_REVIEW_AUTOPILOT_POLICY),
        )
        result["auto_resolved"] = True
        result["resolution"] = resolution
        return result

    if normalized == "review.rebuild_target_candidates":
        from control_state import get_review_item

        review_id = str(parameters.get("review_id") or target or "").strip()
        series_id = str(parameters.get("series_id") or "").strip()
        try:
            season = int(parameters.get("season"))
        except (TypeError, ValueError) as exc:
            raise ValueError("review.rebuild_target_candidates requires a numeric season") from exc
        if not review_id or not re.fullmatch(r"series_[0-9a-f]{24}", series_id):
            raise ValueError(
                "review.rebuild_target_candidates requires review_id and a stable series_id"
            )
        if not 0 <= season <= 99:
            raise ValueError("review recovery season must be between 0 and 99")
        review = get_review_item(config, review_id)
        if review is None:
            raise ValueError(f"review item does not exist: {review_id}")
        if str(review.get("kind") or "") != "target_ambiguity":
            raise ValueError(f"review item is not a target ambiguity: {review_id}")
        if str(review.get("status") or "") != "open":
            raise ValueError(f"review item is no longer open: {review_id}")
        return _rebuild_review_target_candidates(
            config,
            review,
            review_id=review_id,
            series_id=series_id,
            season=season,
        )

    if normalized == "review.dismiss":
        from control_state import dismiss_review_item, get_review_item

        review_id = str(parameters.get("review_id") or target or "").strip()
        if not review_id:
            raise ValueError("review.dismiss requires review_id")
        review = get_review_item(config, review_id)
        if review is None:
            raise ValueError(f"review item does not exist: {review_id}")
        if str(review.get("kind") or "") != "target_ambiguity":
            raise ValueError(f"only target ambiguity reviews can be dismissed: {review_id}")
        if str(review.get("status") or "") != "open":
            raise ValueError(f"review item is no longer open: {review_id}")
        dismissed = dismiss_review_item(config, review_id)
        return {
            "action": normalized,
            "review_id": review_id,
            "dismissed": dismissed,
            "media_deleted": False,
            "subtitle_deleted": False,
            "torrent_deleted": False,
        }

    if normalized == "review.resolve_target":
        from control_state import (
            get_review_item,
            resolve_review_item,
            resolve_sibling_target_reviews,
            upsert_series_source_mapping,
        )
        from mikan_worker import resume_target_ambiguity_source

        review_id = str(parameters.get("review_id") or target or "").strip()
        source_id = parameters.get("source_id") or parameters.get("bangumi_id")
        candidate_path = str(parameters.get("candidate_path") or "").strip()
        if not review_id or not source_id or not candidate_path:
            raise ValueError(
                "review.resolve_target requires review_id, bangumi_id/source_id and candidate_path"
            )
        review = get_review_item(config, review_id)
        if review is None:
            raise ValueError(f"review item does not exist: {review_id}")
        if str(review.get("kind") or "") != "target_ambiguity":
            raise ValueError(f"review item is not a target ambiguity: {review_id}")
        if str(review.get("status") or "") != "open":
            raise ValueError(f"review item is no longer open: {review_id}")
        try:
            bangumi_id = int(source_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("review.resolve_target requires a numeric Mikan bangumi_id") from exc
        diagnosed_ids = {
            int(value)
            for value in (review.get("diagnosis") or {}).get("bangumi_ids", [])
            if str(value).strip().isdigit()
        }
        if diagnosed_ids and bangumi_id not in diagnosed_ids:
            raise ValueError(
                f"bangumi_id {bangumi_id} is not part of review {review_id}: {sorted(diagnosed_ids)}"
            )
        selected_video, series_directory, season = _validated_review_target_candidate(
            config,
            review,
            candidate_path,
        )
        supplied_series_path = str(parameters.get("series_path") or "").strip()
        if supplied_series_path:
            supplied_series = _validated_control_target_path(
                config,
                supplied_series_path,
                require_file=False,
            )
            if supplied_series != series_directory:
                raise ValueError("review series_path does not match selected candidate")
        if "season" in parameters and int(parameters.get("season") or 0) != season:
            raise ValueError("review season does not match selected candidate")
        torrent_hash = str((review.get("diagnosis") or {}).get("torrent_hash") or "").strip().casefold()
        if not torrent_hash:
            raise ValueError("target ambiguity review is missing its torrent hash")
        series_path = str(series_directory)
        source_resume = resume_target_ambiguity_source(
            config,
            bangumi_id=bangumi_id,
            torrent_hash=torrent_hash,
            diagnosis=review.get("diagnosis") if isinstance(review.get("diagnosis"), dict) else {},
        )
        durable_resume_count = sum(
            int(source_resume.get(key) or 0)
            for key in ("restored_pending", "requeued", "waiting_download")
        )
        if durable_resume_count <= 0:
            raise RuntimeError(
                "reviewed source produced no durable pending, queue, or download state"
            )
        upsert_series_source_mapping(
            config,
            source="mikan",
            source_id=str(bangumi_id),
            season=season,
            series_path=series_path,
            series_id=str(parameters.get("series_id") or ""),
            confidence=1.0,
            locked=True,
        )
        resolution = {
            "source": "mikan",
            "source_id": str(bangumi_id),
            "season": season,
            "series_path": series_path,
            "candidate_path": str(selected_video),
            "torrent_hash": torrent_hash,
            "source_resume": source_resume,
        }
        resolved = resolve_review_item(config, review_id, resolution)
        resolved_siblings = resolve_sibling_target_reviews(
            config,
            torrent_hash=torrent_hash,
            exclude_review_id=review_id,
            resolution=resolution,
        ) if resolved else []
        return {
            "action": normalized,
            "review_id": review_id,
            "resolved": resolved,
            "resolved_siblings": resolved_siblings,
            "source_resume": source_resume,
            "restored_pending": int(source_resume.get("restored_pending") or 0),
            "requeued": int(source_resume.get("requeued") or 0),
            "waiting_download": int(source_resume.get("waiting_download") or 0),
        }

    if normalized == "review.resolve_ai":
        from control_state import get_review_item, resolve_review_item

        review_id = str(parameters.get("review_id") or "").strip()
        remediation = str(parameters.get("remediation") or "").strip().casefold()
        if not review_id or remediation not in {
            "ai.retranslate",
            "ai.retranscribe",
            "ai.retranslate_lines",
            "ai.retry_selective_asr",
        }:
            raise ValueError("review.resolve_ai requires review_id and a supported remediation")
        review = get_review_item(config, review_id)
        if review is None:
            raise ValueError(f"review item does not exist: {review_id}")
        if str(review.get("kind") or "") not in {"subtitle_quality", "asr_quality"}:
            raise ValueError(f"review item is not an AI quality review: {review_id}")
        if str(review.get("status") or "") != "open":
            raise ValueError(f"review item is no longer open: {review_id}")
        video = _validated_control_target_path(config, target, require_file=True)
        diagnosed_video = str((review.get("diagnosis") or {}).get("video") or "").strip()
        if diagnosed_video and Path(diagnosed_video).resolve() != video:
            raise ValueError(f"review target does not match diagnosed video: {review_id}")
        lines = ""
        resolved = False
        if remediation == "ai.retry_selective_asr":
            if str(review.get("kind") or "") != "asr_quality":
                raise ValueError("selective ASR remediation requires an ASR quality review")
            automatic_review = bool(parameters.get("automatic_review"))
            policy_revision = str(parameters.get("policy_revision") or "").strip()
            expected_failure_revision = str(
                parameters.get("expected_failure_revision") or ""
            ).strip()
            expected_review_evidence_revision = str(
                parameters.get("expected_review_evidence_revision") or ""
            ).strip()
            evidence = _selective_asr_review_evidence(config, review, video)
            if evidence is None:
                raise ValueError("selective ASR review checkpoint is missing or stale")
            if automatic_review:
                if (
                    policy_revision
                    != _AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY
                    or not expected_failure_revision
                    or str(evidence.get("evidence_revision") or "")
                    != expected_review_evidence_revision
                ):
                    raise ValueError(
                        "selective ASR remediation requires current revision-bound evidence"
                    )
            else:
                from scan_state import ScanStateStore

                state = ScanStateStore.from_config(config)
                try:
                    snapshot = state.ai_queue_candidate_snapshot(video) or {}
                finally:
                    state.close()
                expected_failure_revision = str(
                    snapshot.get("failure_revision") or ""
                ).strip()
                if (
                    str(snapshot.get("status") or "").strip().casefold()
                    != "paused"
                    or not expected_failure_revision
                ):
                    raise ValueError(
                        "selective ASR remediation requires the current paused review"
                    )
                policy_revision = _AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY
                expected_review_evidence_revision = str(
                    evidence["evidence_revision"]
                )
            mode = "selective_asr"
            output = _queue_selective_asr_review_command(
                config,
                video,
                review=review,
                expected_failure_revision=expected_failure_revision,
                expected_review_evidence_revision=expected_review_evidence_revision,
                policy_revision=policy_revision,
            )
        elif remediation == "ai.retranslate_lines":
            lines = str(parameters.get("lines") or "").strip()
            if not re.fullmatch(r"\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*", lines):
                raise ValueError("review.resolve_ai line repair requires valid subtitle indexes")
            automatic_review = bool(parameters.get("automatic_review"))
            line_repair_evidence: dict[str, object] | None = None
            if automatic_review:
                policy_revision = str(parameters.get("policy_revision") or "").strip()
                expected_failure_revision = str(
                    parameters.get("expected_failure_revision") or ""
                ).strip()
                from scan_state import ScanStateStore

                state = ScanStateStore.from_config(config)
                try:
                    snapshot = state.ai_queue_candidate_snapshot(video) or {}
                    running_jobs = state.running_ai_queue_count()
                finally:
                    state.close()
                evidence = _translation_omission_line_repair_evidence(snapshot)
                if (
                    str(review.get("kind") or "") != "asr_quality"
                    or policy_revision
                    != _AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY
                    or not expected_failure_revision
                    or evidence is None
                    or str(evidence.get("failure_revision") or "")
                    != expected_failure_revision
                    or str(evidence.get("lines") or "") != lines
                ):
                    raise ValueError(
                        "automatic line remediation requires current revision-bound omission evidence"
                    )
                if running_jobs > 0:
                    raise RuntimeError(
                        "automatic line remediation refused while another AI queue job is running"
                    )
                line_repair_evidence = evidence
            mode = "retranslate_lines"
            escalated_to_full_asr = False
            try:
                output = _run_ai_retranslate_lines_command(config, video, lines=lines)
            except RuntimeError as exc:
                asr_review_index = _line_repair_asr_review_index(exc)
                expected_indexes = {
                    index
                    for index in (line_repair_evidence or {}).get("indexes", [])
                    if isinstance(index, int)
                }
                if (
                    not automatic_review
                    or asr_review_index is None
                    or asr_review_index not in expected_indexes
                ):
                    raise

                from scan_state import ScanStateStore

                state = ScanStateStore.from_config(config)
                try:
                    current_snapshot = state.ai_queue_candidate_snapshot(video) or {}
                    current_running_jobs = state.running_ai_queue_count()
                finally:
                    state.close()
                current_evidence = _translation_omission_line_repair_evidence(
                    current_snapshot
                )
                if (
                    current_evidence is None
                    or str(current_evidence.get("failure_revision") or "")
                    != expected_failure_revision
                    or str(current_evidence.get("lines") or "") != lines
                ):
                    raise ValueError(
                        "automatic ASR escalation requires current revision-bound omission evidence"
                    ) from exc
                if current_running_jobs > 0:
                    raise RuntimeError(
                        "automatic ASR escalation refused while another AI queue job is running"
                    ) from exc
                mode = "retranscribe"
                output = _run_ai_reprocess_command(
                    config,
                    video,
                    mode=mode,
                    queue_mode="auto_review",
                    expected_failure_revision=expected_failure_revision,
                    policy_revision=policy_revision,
                )
                escalated_to_full_asr = True
                logger.warning(
                    "Escalated exact failed line repair to one revision-bound full ASR retry. "
                    "review=%s path=%s policy=%s index=%s",
                    review_id,
                    video,
                    policy_revision,
                    asr_review_index,
                )
            if not escalated_to_full_asr:
                from subtitle_paths import has_ai_finished_subtitle

                if not has_ai_finished_subtitle(video, config):
                    raise RuntimeError(
                        "AI line repair returned success without a verified Traditional-Chinese publication"
                    )
                resolved = resolve_review_item(
                    config,
                    review_id,
                    {
                        "action": remediation,
                        "source": "control_command",
                        "reason": "quality_gate_and_publication_succeeded",
                        "video": str(video),
                        "lines": lines,
                    },
                )
        else:
            mode = "retranslate" if remediation == "ai.retranslate" else "retranscribe"
            automatic_review = bool(parameters.get("automatic_review"))
            policy_revision = str(parameters.get("policy_revision") or "").strip()
            expected_failure_revision = str(
                parameters.get("expected_failure_revision") or ""
            ).strip()
            if automatic_review:
                valid_asr_retry = (
                    remediation == "ai.retranscribe"
                    and policy_revision == _AI_QUALITY_REVIEW_AUTOPILOT_POLICY
                    and bool(expected_failure_revision)
                )
                valid_translation_retry = (
                    remediation == "ai.retranslate"
                    and policy_revision == _AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY
                    and bool(expected_failure_revision)
                )
                expected_review_evidence_revision = str(
                    parameters.get("expected_review_evidence_revision") or ""
                ).strip()
                valid_timing_retry = (
                    remediation == "ai.retranslate"
                    and policy_revision == _AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY
                    and bool(expected_failure_revision)
                    and bool(expected_review_evidence_revision)
                )
                if valid_asr_retry:
                    from scan_state import ScanStateStore

                    state = ScanStateStore.from_config(config)
                    try:
                        snapshot = state.ai_queue_candidate_snapshot(video) or {}
                    finally:
                        state.close()
                    valid_asr_retry = bool(
                        _asr_review_snapshot_requires_full_retranscription(snapshot)
                        and str(snapshot.get("failure_revision") or "")
                        == expected_failure_revision
                    )
                if valid_translation_retry:
                    from scan_state import ScanStateStore

                    state = ScanStateStore.from_config(config)
                    try:
                        snapshot = state.ai_queue_candidate_snapshot(video) or {}
                    finally:
                        state.close()
                    classification = _classify_historical_failed_retry(
                        str(snapshot.get("job_stage") or ""),
                        str(snapshot.get("last_error") or snapshot.get("job_message") or ""),
                    )
                    valid_translation_retry = bool(
                        str(snapshot.get("status") or "").strip().casefold() == "paused"
                        and int(snapshot.get("attempts") or 0) >= 2
                        and str(snapshot.get("failure_revision") or "") == expected_failure_revision
                        and (classification or {}).get("failure_code")
                        == "translation_safe_omission"
                    )
                if valid_timing_retry:
                    from scan_state import ScanStateStore

                    state = ScanStateStore.from_config(config)
                    try:
                        snapshot = state.ai_queue_candidate_snapshot(video) or {}
                    finally:
                        state.close()
                    evidence = _exact_aligned_timing_review_evidence(review)
                    valid_timing_retry = bool(
                        _timing_review_snapshot_is_recoverable(snapshot)
                        and str(snapshot.get("failure_revision") or "")
                        == expected_failure_revision
                        and str((evidence or {}).get("evidence_revision") or "")
                        == expected_review_evidence_revision
                    )
                if not valid_asr_retry and not valid_translation_retry and not valid_timing_retry:
                    raise ValueError(
                        "automatic review remediation requires current revision-bound evidence"
                    )
                output = _run_ai_reprocess_command(
                    config,
                    video,
                    mode=mode,
                    queue_mode="auto_review",
                    expected_failure_revision=expected_failure_revision,
                    policy_revision=policy_revision,
                )
            else:
                output = _run_ai_reprocess_command(config, video, mode=mode)
        return {
            "action": normalized,
            "review_id": review_id,
            # Full reprocessing remains asynchronous. Line repair is synchronous
            # and may close the review only after its manifest and zh-TW output
            # pass the same finished-subtitle verification used by the Worker.
            "resolved": resolved,
            "queued": not resolved,
            "target": str(video),
            "output": output,
        }

    if normalized in {"ai.retranslate", "ai.retranscribe", "ai.retranslate_lines"}:
        if not target:
            raise ValueError(f"{normalized} requires a video path target")
        video = _validated_control_target_path(config, target, require_file=True)
        if normalized == "ai.retranslate_lines":
            lines = str(parameters.get("lines") or "").strip()
            if not re.fullmatch(r"\d+(?:\s*-\s*\d+)?(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*", lines):
                raise ValueError("ai.retranslate_lines requires valid line indexes")
            output = _run_ai_retranslate_lines_command(config, video, lines=lines)
            return {"action": normalized, "target": str(video), "lines": lines, "output": output}
        mode = "retranslate" if normalized.endswith("retranslate") else "retranscribe"
        output = _run_ai_reprocess_command(config, video, mode=mode)
        return {"action": normalized, "target": str(video), "output": output}

    raise ValueError(f"Unsupported control command: {action}")


_AI_SWEEP_ALLOWED_FAILURE_CODES = {
    "transient_oom",
    "transient_timeout",
    "transient_connection",
    "translation_safe_omission",
}

_AI_QUALITY_REVIEW_AUTOPILOT_POLICY = "asr-full-retranscribe-v1"
_AI_QUALITY_REVIEW_AUTOPILOT_MAX_ATTEMPTS = 3
_REVIEW_AUTOPILOT_PAGE_SIZE = 100
_REVIEW_AUTOPILOT_MAX_SCAN = 10_000
_AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY = "asr-checkpoint-selective-v1"
_AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY = "translation-omission-retranslate-v1"
_AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY = "translation-omission-lines-v4"
_AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_MAX_ATTEMPTS = 1
_AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_MAX_INDEXES = 32
_AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY = "subtitle-aligned-timing-v2"
_TARGET_REVIEW_AUTOPILOT_POLICY = "target-unique-title-episode-resolve-v2"
_TARGET_REVIEW_AUTOPILOT_MAX_ATTEMPTS = 3

_HISTORICAL_ASR_REJECTION_MARKER = (
    "asr artifacts or low-confidence rescue candidates were rejected; "
    "the affected audio must be re-transcribed without a prompt:"
)
_HISTORICAL_TRANSLATION_OMISSION_MARKER = (
    "translation safe-omission remained after bounded same-job recovery:"
)
_CACHED_ASR_SELECTIVE_FAILURE_MARKER = (
    "cached asr selective repair refused fail-closed:"
)
_CACHED_ASR_CONTEXT_MISMATCH_REASONS = {
    "extracted audio fingerprint mismatch",
    "audio stream fingerprint mismatch",
    "japanese srt cache fingerprint mismatch",
    "repair fingerprint mismatch",
}


def _classify_historical_failed_retry(stage: str, message: str) -> dict[str, object] | None:
    """Classify only evidence-bearing historical failures without side effects.

    Old queue rows predate structured failure codes.  Keep this parser narrow:
    it recognizes the exact deterministic messages emitted by the Worker and
    refuses partial or malformed evidence.  The result is advisory review
    metadata, never an automatic retry classification.
    """

    normalized_stage = str(stage or "").strip().casefold()
    normalized_message = " ".join(str(message or "").strip().split())
    folded_message = normalized_message.casefold()

    if (
        normalized_stage in {"transcription", "transcription_review", "failed"}
        and folded_message.startswith(_HISTORICAL_ASR_REJECTION_MARKER)
    ):
        ranges: list[list[float]] = []
        seen_ranges: set[tuple[float, float]] = set()
        for match in re.finditer(
            r"(?<![\d.])(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)s?(?![\d.])",
            normalized_message,
            flags=re.IGNORECASE,
        ):
            start = round(float(match.group(1)), 3)
            end = round(float(match.group(2)), 3)
            if start < 0 or end <= start or end > 86400:
                continue
            key = (start, end)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            ranges.append([start, end])
            if len(ranges) >= 64:
                break
        if not ranges:
            return None
        return {
            "failure_code": "deterministic_asr_quality",
            "review_required": True,
            "review_kind": "asr_quality",
            "summary": "Historical ASR rejection requires evidence-gated remediation",
            "evidence": {"review_ranges": ranges},
            "remediation_candidates": [
                {
                    "action": "ai.retranscribe",
                    "strategy": "full_transcription_rerun",
                    "selective": False,
                }
            ],
        }

    if (
        normalized_stage in {"quality_check", "failed"}
        and folded_message.startswith(_HISTORICAL_TRANSLATION_OMISSION_MARKER)
    ):
        indexes_match = re.search(r"\bindexes\s*=\s*\[([^\]]*)\]", normalized_message, flags=re.IGNORECASE)
        if indexes_match is None:
            return None
        raw_indexes = indexes_match.group(1)
        if not re.fullmatch(r"\s*\d+(?:\s*,\s*\d+)*\s*", raw_indexes):
            return None
        indexes = sorted({int(value.strip()) for value in raw_indexes.split(",")})
        if not indexes or len(indexes) > 500 or any(index <= 0 or index > 1_000_000 for index in indexes):
            return None
        lines = ",".join(str(index) for index in indexes)
        return {
            "failure_code": "translation_safe_omission",
            "review_required": True,
            "review_kind": "subtitle_quality",
            "summary": "Historical translation omissions require targeted line repair",
            "evidence": {"indexes": indexes},
            "remediation_candidates": [
                {
                    "action": "ai.retranslate_lines",
                    "strategy": "targeted_translation_repair",
                    "lines": lines,
                    "selective": True,
                }
            ],
        }
    return None


def _translation_omission_line_repair_evidence(
    snapshot: dict[str, object],
) -> dict[str, object] | None:
    """Bind an exact omission-only line repair to one paused failure revision."""

    if str(snapshot.get("status") or "").strip().casefold() != "paused":
        return None
    failure_revision = str(snapshot.get("failure_revision") or "").strip()
    if not failure_revision:
        return None
    normalized_message = " ".join(
        str(snapshot.get("last_error") or snapshot.get("job_message") or "")
        .strip()
        .split()
    )
    if re.fullmatch(
        rf"{re.escape(_HISTORICAL_TRANSLATION_OMISSION_MARKER)}"
        r"\s*indexes\s*=\s*\[([^\]]*)\]\s*",
        normalized_message,
        flags=re.IGNORECASE,
    ) is None:
        return None
    classification = _classify_historical_failed_retry(
        str(snapshot.get("job_stage") or ""),
        normalized_message,
    )
    if (classification or {}).get("failure_code") != "translation_safe_omission":
        return None
    evidence = classification.get("evidence") if isinstance(classification, dict) else None
    indexes = evidence.get("indexes") if isinstance(evidence, dict) else None
    if (
        not isinstance(indexes, list)
        or not indexes
        or len(indexes) > _AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_MAX_INDEXES
        or any(not isinstance(index, int) or index <= 0 or index > 1_000_000 for index in indexes)
    ):
        return None
    normalized_indexes = sorted(set(indexes))
    if normalized_indexes != indexes:
        return None
    return {
        "failure_revision": failure_revision,
        "indexes": normalized_indexes,
        "lines": ",".join(str(index) for index in normalized_indexes),
    }


def _line_repair_asr_review_index(error: BaseException) -> int | None:
    """Extract a directly terminal or publication-bound ASR review request."""

    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    if not lines:
        return None

    def direct_index(line: str) -> int | None:
        for prefix in (
            "translator.AsrReviewError: ",
            "AsrReviewError: ",
            "RuntimeError: ",
        ):
            if line.startswith(prefix):
                line = line[len(prefix) :]
                break
        match = re.fullmatch(
            r"ASR review requested for subtitle index ([1-9]\d*): "
            r"source transcription is unreliable",
            line,
        )
        if match is None:
            return None
        index = int(match.group(1))
        return index if index <= 1_000_000 else None

    final_index = direct_index(lines[-1])
    if final_index is not None:
        return final_index

    terminal_match = re.fullmatch(
        r"subtitle_quality\.SubtitleQualityError: "
        r"Translation quality event blocks publication: indexes=\[([1-9]\d*)\]",
        lines[-1],
    )
    if terminal_match is None:
        return None
    terminal_index = int(terminal_match.group(1))
    if terminal_index > 1_000_000:
        return None

    staged_indexes: set[int] = set()
    staged_pattern = re.compile(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[WARNING\] "
        r"Translation batch \d+(?:\.\d+)* staged unresolved line at index "
        r"([1-9]\d*) after [1-9]\d* repair attempt\(s\); "
        r"publication will be blocked: source=.+ output=.+ reason="
        r"ASR review requested for subtitle index ([1-9]\d*): "
        r"source transcription is unreliable"
    )
    for line in lines[:-1]:
        staged_match = staged_pattern.fullmatch(line)
        if staged_match is None:
            continue
        staged_index = int(staged_match.group(1))
        reason_index = int(staged_match.group(2))
        if (
            staged_index != reason_index
            or staged_index > 1_000_000
        ):
            return None
        staged_indexes.add(staged_index)
    if staged_indexes != {terminal_index}:
        return None
    return terminal_index


def _selective_asr_review_evidence(
    config,
    review: dict[str, object],
    video: Path,
) -> dict[str, object] | None:
    """Load an immutable Japanese ASR checkpoint and bind it to one review."""

    from asr_review_checkpoint import (
        AsrReviewCheckpointError,
        asr_review_checkpoint_root,
        load_asr_review_checkpoint,
        normalize_asr_review_ranges,
    )
    from subtitle_paths import paths_for_video

    diagnosis = review.get("diagnosis")
    if not isinstance(diagnosis, dict):
        return None
    checkpoint_evidence = diagnosis.get("asr_review_checkpoint")
    if not isinstance(checkpoint_evidence, dict):
        return None
    if (
        not bool(checkpoint_evidence.get("selective_retry_supported"))
        or bool(checkpoint_evidence.get("repair_attempted"))
        or bool(diagnosis.get("repair_attempted"))
        or str(checkpoint_evidence.get("language") or "").strip().casefold()
        != "ja"
    ):
        return None
    selective_candidates = [
        candidate
        for candidate in review.get("candidates") or []
        if isinstance(candidate, dict)
        and str(candidate.get("action") or "").strip().casefold()
        == "ai.retry_selective_asr"
        and str(candidate.get("strategy") or "").strip().casefold()
        == "selective_asr_repair"
        and bool(candidate.get("selective"))
    ]
    if len(selective_candidates) != 1:
        return None
    candidate = selective_candidates[0]
    checkpoint_id = str(checkpoint_evidence.get("checkpoint_id") or "").strip()
    manifest_path = Path(
        str(checkpoint_evidence.get("manifest_path") or "").strip()
    )
    manifest_sha256 = str(
        checkpoint_evidence.get("manifest_sha256") or ""
    ).strip().casefold()
    repair_fingerprint = str(
        checkpoint_evidence.get("repair_fingerprint") or ""
    ).strip().casefold()
    fingerprints = checkpoint_evidence.get("fingerprints")
    raw_ranges = checkpoint_evidence.get("review_ranges")
    if (
        str(candidate.get("checkpoint_id") or "").strip() != checkpoint_id
        or str(candidate.get("manifest_sha256") or "").strip().casefold()
        != manifest_sha256
        or str(candidate.get("repair_fingerprint") or "").strip().casefold()
        != repair_fingerprint
        or not isinstance(fingerprints, dict)
        or not manifest_path.is_absolute()
    ):
        return None
    try:
        checkpoint_root = asr_review_checkpoint_root(
            config.work_path
        ).resolve(strict=False)
        resolved_manifest = manifest_path.resolve(strict=False)
        if checkpoint_root not in resolved_manifest.parents:
            return None
        expected_target = paths_for_video(video, config).ja_srt.resolve(
            strict=False
        )
        normalized_ranges = normalize_asr_review_ranges(raw_ranges)
        if normalized_ranges != normalize_asr_review_ranges(
            diagnosis.get("review_ranges")
        ):
            return None
        checkpoint = load_asr_review_checkpoint(
            resolved_manifest,
            expected_manifest_sha256=manifest_sha256,
            expected_checkpoint_id=checkpoint_id,
            expected_target_path=expected_target,
            expected_language="ja",
            expected_review_ranges=normalized_ranges,
            expected_repair_fingerprint=repair_fingerprint,
            expected_fingerprints=fingerprints,
        )
    except (AsrReviewCheckpointError, OSError, ValueError, TypeError):
        return None
    if (
        str(checkpoint_evidence.get("rejected_srt_sha256") or "").strip().casefold()
        != checkpoint.rejected_srt_sha256
        or str(checkpoint_evidence.get("diagnostics_sha256") or "").strip().casefold()
        != checkpoint.diagnostics_sha256
    ):
        return None
    revision_payload = {
        "review_id": str(review.get("review_id") or ""),
        "target_key": str(review.get("target_key") or ""),
        "checkpoint_id": checkpoint.checkpoint_id,
        "manifest_sha256": checkpoint.manifest_sha256,
        "target_path": str(checkpoint.target_path),
        "language": checkpoint.language,
        "review_ranges": [list(item) for item in checkpoint.review_ranges],
        "repair_fingerprint": checkpoint.repair_fingerprint,
        "fingerprints": checkpoint.fingerprints,
        "rejected_srt_sha256": checkpoint.rejected_srt_sha256,
        "diagnostics_sha256": checkpoint.diagnostics_sha256,
    }
    evidence_revision = hashlib.sha256(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        **revision_payload,
        "evidence_revision": evidence_revision,
        "checkpoint": checkpoint,
    }


def _exact_aligned_timing_review_evidence(review: dict[str, object]) -> dict[str, object] | None:
    """Accept only mechanical timing failures with exact cue-level evidence."""

    diagnosis = review.get("diagnosis")
    if not isinstance(diagnosis, dict):
        return None
    if str(diagnosis.get("stage") or "").strip().casefold() != "quality_check":
        return None
    reports = diagnosis.get("reports")
    if not isinstance(reports, list) or not reports:
        return None
    normalized_reports: list[dict[str, object]] = []
    failure_codes: set[str] = set()
    failure_indexes: set[int] = set()
    duration_indexes: set[int] = set()
    cps_indexes: set[int] = set()
    for report in reports:
        if not isinstance(report, dict):
            return None
        issues = report.get("issues")
        if not isinstance(issues, list):
            return None
        normalized_issues: list[dict[str, object]] = []
        for issue in issues:
            if not isinstance(issue, dict):
                return None
            code = str(issue.get("code") or "").strip().casefold()
            severity = str(issue.get("severity") or "").strip().casefold()
            raw_indexes = issue.get("indexes")
            if not isinstance(raw_indexes, list):
                return None
            try:
                indexes = sorted({int(index) for index in raw_indexes})
            except (TypeError, ValueError):
                return None
            if any(index <= 0 for index in indexes) or len(indexes) > 500:
                return None
            normalized_issues.append(
                {"code": code, "severity": severity, "indexes": indexes}
            )
            if code in {"short_duration", "too_short"}:
                duration_indexes.update(indexes)
            if severity == "fail":
                if not indexes:
                    return None
                failure_codes.add(code)
                failure_indexes.update(indexes)
                if code == "cps_too_high":
                    cps_indexes.update(indexes)
        normalized_reports.append(
            {
                "role": str(report.get("role") or "").strip().casefold(),
                "status": str(report.get("status") or "").strip().casefold(),
                "issues": normalized_issues,
            }
        )
    if not failure_codes or failure_codes - {"timing_overlap", "too_short", "cps_too_high"}:
        return None
    if cps_indexes and not cps_indexes.issubset(duration_indexes):
        return None
    if not failure_indexes or len(failure_indexes) > 500:
        return None
    revision_payload = {
        "review_id": str(review.get("review_id") or ""),
        "target_key": str(review.get("target_key") or ""),
        "updated_at": float(review.get("updated_at") or 0.0),
        "video": str(diagnosis.get("video") or ""),
        "reports": normalized_reports,
    }
    evidence_revision = hashlib.sha256(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "failure_codes": sorted(failure_codes),
        "indexes": sorted(failure_indexes),
        "evidence_revision": evidence_revision,
    }


def _timing_review_snapshot_is_recoverable(snapshot: dict[str, object]) -> bool:
    """Accept the original QC pause or an exact transient follow-up failure."""

    if str(snapshot.get("status") or "").strip().casefold() != "paused":
        return False
    if not str(snapshot.get("failure_revision") or "").strip():
        return False
    stage = str(snapshot.get("job_stage") or "").strip().casefold()
    if stage == "quality_check":
        return True
    message = str(snapshot.get("last_error") or snapshot.get("job_message") or "")
    error_code, _retry_strategy = _ai_failure_policy(stage, message)
    return error_code in _AI_SWEEP_ALLOWED_FAILURE_CODES


def _historical_asr_selective_preview_candidate(
    config,
    path: str,
    classification: dict[str, object],
) -> dict[str, object] | None:
    """Offer selective ASR only when durable static fingerprint evidence exists.

    Runtime audio extraction and fingerprint comparison remain authoritative in
    ``VideoWorker``.  This read-only preflight merely suppresses a selective
    suggestion when the cache cannot possibly pass that later verification.
    """

    if not bool(getattr(config, "asr_selective_retry_enabled", True)):
        return None
    try:
        from safe_files import sha256_file
        from subtitle_paths import paths_for_video
        from transcriber import read_asr_diagnostics

        ja_srt = paths_for_video(Path(path), config).ja_srt
        if not ja_srt.is_file():
            return None
        diagnostics = read_asr_diagnostics(ja_srt, config)
        if str(diagnostics.get("status") or "") not in {
            "selective_retry_required",
            "selective_repair_rejected",
        }:
            return None
        diagnosed_sha256 = str(diagnostics.get("srt_sha256") or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", diagnosed_sha256):
            return None
        if sha256_file(ja_srt).casefold() != diagnosed_sha256:
            return None
        required_fingerprints = (
            "media_fingerprint",
            "audio_fingerprint",
            "audio_stream_fingerprint",
            "cache_fingerprint",
        )
        if any(
            not isinstance(diagnostics.get(key), dict)
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str((diagnostics.get(key) or {}).get("fingerprint") or "").strip().casefold(),
            )
            for key in required_fingerprints
        ):
            return None
        repair_fingerprint = str(diagnostics.get("repair_fingerprint") or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", repair_fingerprint):
            return None
        attempts = diagnostics.get("repair_attempts")
        if isinstance(attempts, list) and any(
            isinstance(item, dict)
            and str(item.get("fingerprint") or "").strip().casefold() == repair_fingerprint
            for item in attempts
        ):
            return None
        evidence = classification.get("evidence")
        review_ranges = evidence.get("review_ranges") if isinstance(evidence, dict) else None
        if not isinstance(review_ranges, list) or not review_ranges:
            return None
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return {
        "action": "ai.retry_selective_asr",
        "strategy": "selective_asr_repair",
        "selective": True,
        "repair_fingerprint": repair_fingerprint,
        "requires_runtime_fingerprint_verification": True,
    }


def _review_autopilot_prefix(policy_revision: str, action: str) -> str:
    return f"review-autopilot:{policy_revision}:{action}:"


def _iter_review_autopilot_candidates(
    config,
    *,
    kind: str,
    idempotency_prefix: str,
    required_completed_prefix: str = "",
    allow_revision_scoped_attempts: bool = False,
):
    """Page past ineligible old reviews without an unbounded one-shot query."""

    from control_state import list_open_review_autopilot_candidates

    offset = 0
    while offset < _REVIEW_AUTOPILOT_MAX_SCAN:
        page_limit = min(
            _REVIEW_AUTOPILOT_PAGE_SIZE,
            _REVIEW_AUTOPILOT_MAX_SCAN - offset,
        )
        page = list_open_review_autopilot_candidates(
            config,
            kind=kind,
            idempotency_prefix=idempotency_prefix,
            required_completed_prefix=required_completed_prefix,
            allow_revision_scoped_attempts=allow_revision_scoped_attempts,
            limit=page_limit,
            offset=offset,
        )
        yield from page
        if len(page) < page_limit:
            break
        offset += len(page)


def _asr_review_snapshot_requires_full_retranscription(
    snapshot: dict[str, object],
) -> bool:
    """Accept only a current, evidence-bearing deterministic ASR failure."""

    if (
        str(snapshot.get("status") or "").strip().casefold() != "paused"
        or not str(snapshot.get("failure_revision") or "").strip()
    ):
        return False
    failure_code = str(snapshot.get("last_error_code") or "").strip().casefold()
    if failure_code == "deterministic_asr_quality":
        return True
    if failure_code not in {"", "legacy_transcription"}:
        return False
    message = " ".join(
        str(snapshot.get("last_error") or snapshot.get("job_message") or "")
        .strip()
        .split()
    ).casefold()
    if (
        str(snapshot.get("job_stage") or "").strip().casefold()
        in {"transcription_review", "failed"}
        and message.startswith(_CACHED_ASR_SELECTIVE_FAILURE_MARKER)
    ):
        reasons = {
            item.strip()
            for item in message[len(_CACHED_ASR_SELECTIVE_FAILURE_MARKER) :].split(";")
            if item.strip()
        }
        if reasons and reasons.issubset(_CACHED_ASR_CONTEXT_MISMATCH_REASONS):
            return True
    classification = _classify_historical_failed_retry(
        str(snapshot.get("job_stage") or ""),
        message,
    )
    return bool(
        (classification or {}).get("failure_code")
        == "deterministic_asr_quality"
    )


def _review_autopilot_interval_elapsed(
    config,
    *,
    idempotency_prefix: str,
    interval_seconds: int,
    wait_for_ai_queue: bool,
) -> bool:
    from control_state import latest_review_autopilot_command

    latest = latest_review_autopilot_command(
        config,
        idempotency_prefix=idempotency_prefix,
    )
    if latest is None:
        return True
    command_status = str(latest.get("status") or "").strip().casefold()
    if command_status in {"accepted", "queued", "running"}:
        return False
    if wait_for_ai_queue:
        target = str(latest.get("target") or "").strip()
        if target:
            from scan_state import ScanStateStore

            state = ScanStateStore.from_config(config)
            try:
                snapshot = state.ai_queue_candidate_snapshot(Path(target)) or {}
            finally:
                state.close()
            if str(snapshot.get("status") or "").strip().casefold() in {
                "queued",
                "running",
            }:
                return False
    terminal_at = float(latest.get("finished_at") or latest.get("requested_at") or 0)
    return time.time() - terminal_at >= max(60, min(86400, int(interval_seconds)))


def _active_translation_omission_line_command(config) -> dict[str, object] | None:
    # Real Worker configs always expose work_path. Lightweight callers that do
    # not own the durable command inbox cannot hold a remediation reservation.
    if not hasattr(config, "work_path"):
        return None
    from control_state import active_review_autopilot_command

    return active_review_autopilot_command(
        config,
        idempotency_prefix=_review_autopilot_prefix(
            _AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
            "review.resolve_ai",
        ),
    )


def _yield_normal_ai_queue_to_review_remediation(config, logger) -> bool:
    """Atomically reserve the next idle GPU slot for exact line remediation."""

    global _AI_REVIEW_REMEDIATION_LOGGED_RESERVATION
    with _AI_REVIEW_REMEDIATION_HANDOFF_LOCK:
        active = _active_translation_omission_line_command(config)
        if active is not None:
            reservation = (
                str(active.get("command_id") or ""),
                str(active.get("target") or ""),
            )
            if reservation != _AI_REVIEW_REMEDIATION_LOGGED_RESERVATION:
                _AI_REVIEW_REMEDIATION_LOGGED_RESERVATION = reservation
                logger.info(
                    "Normal AI queue yielded to reserved automatic line remediation. "
                    "command=%s path=%s",
                    active.get("command_id"),
                    active.get("target"),
                )
            return True
        _AI_REVIEW_REMEDIATION_LOGGED_RESERVATION = None
        if _advance_ai_quality_review_autopilot(config, logger):
            logger.info("Normal AI queue yielded after review autopilot queued remediation.")
            return True
    return False


def _advance_ai_quality_review_autopilot(config, logger) -> bool:
    with _AI_REVIEW_REMEDIATION_HANDOFF_LOCK:
        return _advance_ai_quality_review_autopilot_locked(config, logger)


def _advance_ai_quality_review_autopilot_locked(config, logger) -> bool:
    """Queue one old ASR review through a bounded, revision-scoped repair."""

    if not bool(getattr(config, "auto_ai_quality_review_autopilot_enabled", False)):
        return False
    if _ai_queue_paused(config):
        return False
    from control_state import (
        enqueue_command,
        review_autopilot_revision_attempt_allowed,
    )
    from scan_state import ScanStateStore

    action = "review.resolve_ai"
    asr_prefix = _review_autopilot_prefix(
        _AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
        action,
    )
    selective_asr_prefix = _review_autopilot_prefix(
        _AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
        action,
    )
    omission_prefix = _review_autopilot_prefix(
        _AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY,
        action,
    )
    omission_line_prefix = _review_autopilot_prefix(
        _AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
        action,
    )
    timing_prefix = _review_autopilot_prefix(
        _AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY,
        action,
    )
    interval_seconds = int(
        getattr(config, "auto_ai_quality_review_autopilot_interval_seconds", 60)
        or 60
    )
    asr_ready = _review_autopilot_interval_elapsed(
        config,
        idempotency_prefix=asr_prefix,
        interval_seconds=interval_seconds,
        wait_for_ai_queue=True,
    )
    selective_asr_ready = _review_autopilot_interval_elapsed(
        config,
        idempotency_prefix=selective_asr_prefix,
        interval_seconds=interval_seconds,
        wait_for_ai_queue=True,
    )
    omission_ready = _review_autopilot_interval_elapsed(
        config,
        idempotency_prefix=omission_prefix,
        interval_seconds=interval_seconds,
        wait_for_ai_queue=True,
    )
    # A new revision-scoped line policy must not inherit the coarse
    # retranslation policy's global cooldown.
    omission_line_ready = _review_autopilot_interval_elapsed(
        config,
        idempotency_prefix=omission_line_prefix,
        interval_seconds=interval_seconds,
        wait_for_ai_queue=True,
    )
    timing_ready = _review_autopilot_interval_elapsed(
        config,
        idempotency_prefix=timing_prefix,
        interval_seconds=interval_seconds,
        wait_for_ai_queue=True,
    )
    if (
        not asr_ready
        and not selective_asr_ready
        and not omission_ready
        and not omission_line_ready
        and not timing_ready
    ):
        return False
    state = ScanStateStore.from_config(config)
    try:
        if state.active_review_remediation_count() > 0:
            return False
        if omission_ready and asr_ready:
            omission_reviews_by_id: dict[str, dict[str, object]] = {}
            for prerequisite_prefix in (asr_prefix, selective_asr_prefix):
                for review in _iter_review_autopilot_candidates(
                    config,
                    kind="asr_quality",
                    idempotency_prefix=omission_prefix,
                    required_completed_prefix=prerequisite_prefix,
                ):
                    review_id = str(review.get("review_id") or "").strip()
                    if review_id:
                        omission_reviews_by_id.setdefault(review_id, review)
            omission_reviews = list(omission_reviews_by_id.values())
            for review in omission_reviews:
                review_id = str(review.get("review_id") or "").strip()
                target = str(
                    (review.get("diagnosis") or {}).get("video")
                    or review.get("target_key")
                    or ""
                ).strip()
                if not review_id or not target:
                    continue
                try:
                    video = _validated_control_target_path(config, target, require_file=True)
                except (OSError, ValueError):
                    continue
                snapshot = state.ai_queue_candidate_snapshot(video) or {}
                failure_revision = str(snapshot.get("failure_revision") or "").strip()
                classification = _classify_historical_failed_retry(
                    str(snapshot.get("job_stage") or ""),
                    str(snapshot.get("last_error") or snapshot.get("job_message") or ""),
                )
                if (
                    str(snapshot.get("status") or "").strip().casefold() != "paused"
                    or int(snapshot.get("attempts") or 0) < 2
                    or not failure_revision
                    or (classification or {}).get("failure_code")
                    != "translation_safe_omission"
                ):
                    continue
                command = enqueue_command(
                    config,
                    action=action,
                    target=str(video),
                    parameters={
                        "review_id": review_id,
                        "remediation": "ai.retranslate",
                        "automatic_review": True,
                        "policy_revision": _AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY,
                        "expected_failure_revision": failure_revision,
                    },
                    idempotency_key=f"{omission_prefix}{review_id}",
                )
                logger.warning(
                    "Queued one evidence-gated translation omission follow-up. "
                    "review=%s command=%s path=%s policy=%s",
                    review_id,
                    command.get("command_id"),
                    video,
                    _AI_TRANSLATION_OMISSION_AUTOPILOT_POLICY,
                )
                return True
        if omission_line_ready:
            omission_line_reviews = _iter_review_autopilot_candidates(
                config,
                kind="asr_quality",
                idempotency_prefix=omission_line_prefix,
                required_completed_prefix=omission_prefix,
                allow_revision_scoped_attempts=True,
            )
            for review in omission_line_reviews:
                review_id = str(review.get("review_id") or "").strip()
                target = str(
                    (review.get("diagnosis") or {}).get("video")
                    or review.get("target_key")
                    or ""
                ).strip()
                if not review_id or not target:
                    continue
                try:
                    video = _validated_control_target_path(config, target, require_file=True)
                except (OSError, ValueError):
                    continue
                snapshot = state.ai_queue_candidate_snapshot(video) or {}
                evidence = _translation_omission_line_repair_evidence(snapshot)
                if evidence is None or int(snapshot.get("attempts") or 0) < 2:
                    continue
                failure_revision = str(evidence["failure_revision"])
                if not review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=omission_line_prefix,
                    review_id=review_id,
                    failure_revision=failure_revision,
                    max_attempts=_AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_MAX_ATTEMPTS,
                ):
                    continue
                if state.running_ai_queue_count() > 0:
                    continue
                from lock import VideoLock

                availability_lock = VideoLock(video)
                if not availability_lock.acquire():
                    continue
                availability_lock.release()
                try:
                    command = enqueue_command(
                        config,
                        action=action,
                        target=str(video),
                        parameters={
                            "review_id": review_id,
                            "remediation": "ai.retranslate_lines",
                            "lines": str(evidence["lines"]),
                            "automatic_review": True,
                            "policy_revision": _AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
                            "expected_failure_revision": failure_revision,
                        },
                        idempotency_key=(
                            f"{omission_line_prefix}{review_id}:{failure_revision}"
                        ),
                    )
                except ValueError as exc:
                    if str(exc) != (
                        "idempotency key was already used with a different command payload"
                    ):
                        raise
                    continue
                if str(command.get("status") or "").strip().casefold() != "queued":
                    continue
                logger.warning(
                    "Queued one revision-bound translation omission line repair. "
                    "review=%s command=%s path=%s policy=%s lines=%s",
                    review_id,
                    command.get("command_id"),
                    video,
                    _AI_TRANSLATION_OMISSION_LINE_AUTOPILOT_POLICY,
                    evidence["lines"],
                )
                return True
        if timing_ready:
            timing_reviews = _iter_review_autopilot_candidates(
                config,
                kind="subtitle_quality",
                idempotency_prefix=timing_prefix,
            )
            for review in timing_reviews:
                review_id = str(review.get("review_id") or "").strip()
                target = str(
                    (review.get("diagnosis") or {}).get("video")
                    or review.get("target_key")
                    or ""
                ).strip()
                actions = {
                    str(candidate.get("action") or "").strip().casefold()
                    for candidate in review.get("candidates") or []
                    if isinstance(candidate, dict)
                }
                evidence = _exact_aligned_timing_review_evidence(review)
                if (
                    not review_id
                    or not target
                    or "ai.retranslate" not in actions
                    or evidence is None
                ):
                    continue
                try:
                    video = _validated_control_target_path(config, target, require_file=True)
                except (OSError, ValueError):
                    continue
                snapshot = state.ai_queue_candidate_snapshot(video) or {}
                failure_revision = str(snapshot.get("failure_revision") or "").strip()
                if (
                    not _timing_review_snapshot_is_recoverable(snapshot)
                    or int(snapshot.get("attempts") or 0) < 1
                    or not failure_revision
                ):
                    continue
                command = enqueue_command(
                    config,
                    action=action,
                    target=str(video),
                    parameters={
                        "review_id": review_id,
                        "remediation": "ai.retranslate",
                        "automatic_review": True,
                        "policy_revision": _AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY,
                        "expected_failure_revision": failure_revision,
                        "expected_review_evidence_revision": str(
                            evidence["evidence_revision"]
                        ),
                    },
                    idempotency_key=f"{timing_prefix}{review_id}",
                )
                logger.warning(
                    "Queued one evidence-gated aligned timing remediation. "
                    "review=%s command=%s path=%s policy=%s indexes=%s",
                    review_id,
                    command.get("command_id"),
                    video,
                    _AI_SUBTITLE_TIMING_REVIEW_AUTOPILOT_POLICY,
                    evidence.get("indexes"),
                )
                return True
        if selective_asr_ready:
            selective_reviews = _iter_review_autopilot_candidates(
                config,
                kind="asr_quality",
                idempotency_prefix=selective_asr_prefix,
            )
            for review in selective_reviews:
                review_id = str(review.get("review_id") or "").strip()
                target = str(
                    (review.get("diagnosis") or {}).get("video")
                    or review.get("target_key")
                    or ""
                ).strip()
                if not review_id or not target:
                    continue
                try:
                    video = _validated_control_target_path(
                        config,
                        target,
                        require_file=True,
                    )
                except (OSError, ValueError):
                    continue
                evidence = _selective_asr_review_evidence(
                    config,
                    review,
                    video,
                )
                if evidence is None:
                    continue
                snapshot = state.ai_queue_candidate_snapshot(video) or {}
                failure_revision = str(
                    snapshot.get("failure_revision") or ""
                ).strip()
                if (
                    str(snapshot.get("status") or "").strip().casefold()
                    != "paused"
                    or not failure_revision
                ):
                    continue
                from lock import VideoLock

                availability_lock = VideoLock(video)
                if not availability_lock.acquire():
                    # Do not create (and therefore consume) the one-shot
                    # selective command while another process owns the video.
                    continue
                availability_lock.release()
                try:
                    command = enqueue_command(
                        config,
                        action=action,
                        target=str(video),
                        parameters={
                            "review_id": review_id,
                            "remediation": "ai.retry_selective_asr",
                            "automatic_review": True,
                            "policy_revision": _AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                            "expected_failure_revision": failure_revision,
                            "expected_review_evidence_revision": str(
                                evidence["evidence_revision"]
                            ),
                        },
                        idempotency_key=(
                            f"{selective_asr_prefix}{review_id}:{failure_revision}"
                        ),
                    )
                except ValueError as exc:
                    if str(exc) != (
                        "idempotency key was already used with a different command payload"
                    ):
                        raise
                    # A prior command already consumed this failure-scoped
                    # selective attempt with different checkpoint evidence.
                    # Keep the one-shot key fail-closed and consider the next
                    # candidate (or the separately bounded full fallback).
                    continue
                if str(command.get("status") or "").strip().casefold() != "queued":
                    # An already-completed command for this exact failure
                    # revision cannot be replayed. Continue to the explicit
                    # full fallback candidate instead of reporting a phantom
                    # selective queue action.
                    continue
                logger.warning(
                    "Queued one checkpoint-bound selective ASR remediation. "
                    "review=%s command=%s path=%s checkpoint=%s policy=%s",
                    review_id,
                    command.get("command_id"),
                    video,
                    evidence.get("checkpoint_id"),
                    _AI_SELECTIVE_ASR_REVIEW_AUTOPILOT_POLICY,
                )
                return True
        if not asr_ready:
            return False
        reviews = _iter_review_autopilot_candidates(
            config,
            kind="asr_quality",
            idempotency_prefix=asr_prefix,
            allow_revision_scoped_attempts=True,
        )
        for review in reviews:
            review_id = str(review.get("review_id") or "").strip()
            target = str(
                (review.get("diagnosis") or {}).get("video")
                or review.get("target_key")
                or ""
            ).strip()
            actions = {
                str(candidate.get("action") or "").strip().casefold()
                for candidate in review.get("candidates") or []
                if isinstance(candidate, dict)
            }
            if not review_id or "ai.retranscribe" not in actions or not target:
                continue
            if not selective_asr_ready and "ai.retry_selective_asr" in actions:
                # Give the lower-cost checkpoint path its own bounded turn
                # before falling back to the existing full re-transcription.
                continue
            try:
                video = _validated_control_target_path(config, target, require_file=True)
            except (OSError, ValueError):
                continue
            snapshot = state.ai_queue_candidate_snapshot(video) or {}
            failure_revision = str(snapshot.get("failure_revision") or "").strip()
            if not _asr_review_snapshot_requires_full_retranscription(snapshot):
                continue
            if not review_autopilot_revision_attempt_allowed(
                config,
                idempotency_prefix=asr_prefix,
                review_id=review_id,
                failure_revision=failure_revision,
                max_attempts=_AI_QUALITY_REVIEW_AUTOPILOT_MAX_ATTEMPTS,
            ):
                continue
            command = enqueue_command(
                config,
                action=action,
                target=str(video),
                parameters={
                    "review_id": review_id,
                    "remediation": "ai.retranscribe",
                    "automatic_review": True,
                    "policy_revision": _AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
                    "expected_failure_revision": failure_revision,
                },
                idempotency_key=f"{asr_prefix}{review_id}:{failure_revision}",
            )
            if str(command.get("status") or "").strip().casefold() != "queued":
                continue
            logger.warning(
                "Queued one safety-gated ASR review remediation. "
                "review=%s command=%s path=%s policy=%s",
                review_id,
                command.get("command_id"),
                video,
                _AI_QUALITY_REVIEW_AUTOPILOT_POLICY,
            )
            return True
    finally:
        state.close()
    return False


def _target_review_has_multiple_stored_paths(review: dict[str, object]) -> bool:
    """Reject a provably non-unique target before consuming a retry attempt."""

    candidates = review.get("candidates")
    if not isinstance(candidates, list):
        return False
    paths = {
        str(candidate.get("path") or candidate.get("candidate_path") or "").strip()
        for candidate in candidates
        if isinstance(candidate, dict)
    }
    paths.discard("")
    return len(paths) > 1


def _advance_target_review_autopilot(config, logger) -> bool:
    """Resolve one ambiguity only through the existing unique-evidence checks."""

    if not bool(getattr(config, "auto_target_review_autopilot_enabled", False)):
        return False
    from control_state import (
        enqueue_command,
        next_review_autopilot_retry_attempt,
    )

    action = "review.auto_rebuild_target_candidates"
    prefix = _review_autopilot_prefix(_TARGET_REVIEW_AUTOPILOT_POLICY, action)
    if not _review_autopilot_interval_elapsed(
        config,
        idempotency_prefix=prefix,
        interval_seconds=int(
            getattr(config, "auto_target_review_autopilot_interval_seconds", 300)
            or 300
        ),
        wait_for_ai_queue=False,
    ):
        return False
    review_id = ""
    attempt = None
    for review in _iter_review_autopilot_candidates(
        config,
        kind="target_ambiguity",
        idempotency_prefix=prefix,
        allow_revision_scoped_attempts=True,
    ):
        candidate_review_id = str(review.get("review_id") or "").strip()
        if not candidate_review_id:
            continue
        if _target_review_has_multiple_stored_paths(review):
            # A rebuild command would fail closed, spend one durable attempt,
            # and globally throttle later reviews.  Preserve this ambiguity for
            # human resolution while continuing to later evidence-safe items.
            continue
        candidate_attempt = next_review_autopilot_retry_attempt(
            config,
            idempotency_prefix=prefix,
            review_id=candidate_review_id,
            max_attempts=_TARGET_REVIEW_AUTOPILOT_MAX_ATTEMPTS,
        )
        if candidate_attempt is None:
            continue
        review_id = candidate_review_id
        attempt = candidate_attempt
        break
    if not review_id or attempt is None:
        return False
    command = enqueue_command(
        config,
        action=action,
        target=review_id,
        parameters={
            "review_id": review_id,
            "automatic_review": True,
            "policy_revision": _TARGET_REVIEW_AUTOPILOT_POLICY,
            "attempt": attempt,
        },
        idempotency_key=f"{prefix}{review_id}:attempt-{attempt}",
    )
    logger.info(
        "Queued one fail-closed target review evidence resolution. "
        "review=%s command=%s policy=%s attempt=%s/%s",
        review_id,
        command.get("command_id"),
        _TARGET_REVIEW_AUTOPILOT_POLICY,
        attempt,
        _TARGET_REVIEW_AUTOPILOT_MAX_ATTEMPTS,
    )
    return True


def _ensure_configured_ai_failed_retry_sweep(config, logger) -> None:
    """Create the next bounded campaign only after the previous one settled."""

    if not bool(getattr(config, "auto_ai_failed_retry_sweep_enabled", False)):
        return
    from control_state import (
        create_auto_remediation_campaign,
        latest_auto_remediation_campaign,
    )

    now = time.time()
    interval_seconds = max(
        300,
        min(
            86400,
            int(getattr(config, "auto_ai_failed_retry_sweep_interval_seconds", 300) or 300),
        ),
    )
    latest = latest_auto_remediation_campaign(config)
    if latest is not None:
        state = str(latest.get("state") or "")
        if state in {"running", "paused", "cancelled"}:
            return
        if now - float(latest.get("updated_at") or 0) < interval_seconds:
            return
    max_items = max(
        1,
        min(
            5,
            int(getattr(config, "auto_ai_failed_retry_sweep_max_items", 1) or 1),
        ),
    )
    _, parameters = _validated_ai_failed_retry_sweep_parameters(
        {
            "operation": "start",
            "campaign_key": f"configured:{int(now // interval_seconds)}",
            "max_items": max_items,
            "interval_seconds": interval_seconds,
            "min_age_seconds": 0,
            "max_attempts": max(
                1,
                min(10, int(getattr(config, "auto_ai_max_attempts", 3) or 3)),
            ),
        }
    )
    campaign = create_auto_remediation_campaign(
        config,
        campaign_key=str(parameters["campaign_key"]),
        parameters=parameters,
    )
    logger.info(
        "Created configured safety-gated AI retry campaign. campaign=%s max_items=%s",
        campaign.get("campaign_id"),
        max_items,
    )


def _validated_ai_failed_retry_sweep_parameters(
    parameters: dict,
) -> tuple[str, dict[str, object]]:
    operation = str(parameters.get("operation") or "preview").strip().casefold()
    if operation not in {"preview", "start", "pause", "resume", "cancel"}:
        raise ValueError("AI failed retry sweep operation must be preview, start, pause, resume, or cancel")
    if operation in {"pause", "resume", "cancel"}:
        campaign_id = str(parameters.get("campaign_id") or "").strip()
        if not re.fullmatch(r"sweep_[0-9a-f]{24}", campaign_id):
            raise ValueError("AI failed retry sweep operation requires a valid campaign_id")
        return operation, {"operation": operation, "campaign_id": campaign_id}

    max_items = int(parameters.get("max_items", 1) or 0)
    interval_seconds = int(parameters.get("interval_seconds", 300) or 0)
    min_age_seconds = int(parameters.get("min_age_seconds", 0) or 0)
    max_attempts = int(parameters.get("max_attempts", 3) or 0)
    if not 1 <= max_items <= 5:
        raise ValueError("AI failed retry sweep max_items must be between 1 and 5")
    if not 300 <= interval_seconds <= 86400:
        raise ValueError("AI failed retry sweep interval_seconds must be between 300 and 86400")
    if not 0 <= min_age_seconds <= 365 * 86400:
        raise ValueError("AI failed retry sweep min_age_seconds is outside the safe range")
    if not 1 <= max_attempts <= 10:
        raise ValueError("AI failed retry sweep max_attempts must be between 1 and 10")
    requested_codes = parameters.get("allowed_failure_codes")
    if requested_codes is None:
        allowed_codes = sorted(_AI_SWEEP_ALLOWED_FAILURE_CODES)
    elif isinstance(requested_codes, list):
        allowed_codes = sorted({str(value or "").strip() for value in requested_codes if str(value or "").strip()})
    else:
        raise ValueError("AI failed retry sweep allowed_failure_codes must be an array")
    if not allowed_codes or not set(allowed_codes).issubset(_AI_SWEEP_ALLOWED_FAILURE_CODES):
        raise ValueError("AI failed retry sweep contains a non-allowlisted failure code")
    normalized: dict[str, object] = {
        "operation": operation,
        "max_items": max_items,
        "interval_seconds": interval_seconds,
        "min_age_seconds": min_age_seconds,
        "max_attempts": max_attempts,
        "max_in_flight": 1,
        "max_consecutive_failures": 1,
        "allowed_failure_codes": allowed_codes,
        "strategy_version": "safe-sweep-v1",
    }
    if operation == "start":
        campaign_key = str(parameters.get("campaign_key") or "").strip()
        if not campaign_key or len(campaign_key) > 200:
            raise ValueError("AI failed retry sweep start requires a campaign_key")
        normalized["campaign_key"] = campaign_key
    return operation, normalized


def _preview_ai_failed_retry_sweep(
    config,
    parameters: dict[str, object],
) -> dict[str, object]:
    from control_state import (
        open_ai_quality_review_for_target,
        processed_auto_remediation_keys,
    )
    from scan_state import ScanStateStore

    now = time.time()
    max_attempts = int(parameters.get("max_attempts") or 3)
    min_age_seconds = int(parameters.get("min_age_seconds") or 0)
    allowed_codes = {
        str(value)
        for value in parameters.get("allowed_failure_codes", [])
        if str(value)
    }
    counters = {
        "total": 0,
        "eligible": 0,
        "review_blocked": 0,
        "review_required": 0,
        "not_due": 0,
        "too_new": 0,
        "attempt_ceiling": 0,
        "unsupported": 0,
        "missing_media": 0,
        "already_processed": 0,
    }
    eligible: list[dict[str, object]] = []
    review_required_items: list[dict[str, object]] = []
    processed_keys = processed_auto_remediation_keys(config)
    state = ScanStateStore.from_config(config)
    try:
        candidates = state.failed_retry_candidates(limit=50_000)
    finally:
        state.close()
    for candidate in candidates:
        counters["total"] += 1
        path = str(candidate.get("path") or "")
        review = open_ai_quality_review_for_target(config, path)
        if review is not None:
            counters["review_blocked"] += 1
            semantic_key = (
                path,
                str(candidate.get("failure_revision") or ""),
                "pause_existing_review",
            )
            if semantic_key in processed_keys:
                counters["already_processed"] += 1
                continue
            counters["eligible"] += 1
            eligible.append(
                {
                    "path": path,
                    "failure_revision": str(candidate.get("failure_revision") or ""),
                    "failure_code": str(candidate.get("last_error_code") or ""),
                    "attempts": int(candidate.get("attempts") or 0),
                    "strategy": "pause_existing_review",
                    "review_id": str(review.get("review_id") or ""),
                }
            )
            continue
        stored_failure_code = str(candidate.get("last_error_code") or "")
        if stored_failure_code in {
            "",
            "legacy_transcription",
            "legacy_quality_check",
        }:
            message = str(candidate.get("last_error") or candidate.get("job_message") or "")
            classification = _classify_historical_failed_retry(
                str(candidate.get("job_stage") or ""),
                message,
            )
            if classification is not None:
                counters["review_required"] += 1
                remediation_candidates = [
                    dict(item)
                    for item in classification.get("remediation_candidates", [])
                    if isinstance(item, dict)
                ]
                if str(classification.get("review_kind") or "") == "asr_quality":
                    selective = _historical_asr_selective_preview_candidate(
                        config,
                        path,
                        classification,
                    )
                    if selective is not None:
                        remediation_candidates.insert(0, selective)
                review_required_items.append(
                    {
                        "path": path,
                        "failure_revision": str(candidate.get("failure_revision") or ""),
                        "stored_failure_code": stored_failure_code,
                        "failure_code": str(classification.get("failure_code") or ""),
                        "attempts": int(candidate.get("attempts") or 0),
                        "status": "review_required",
                        "review_kind": str(classification.get("review_kind") or ""),
                        "summary": str(classification.get("summary") or ""),
                        "evidence": dict(classification.get("evidence") or {}),
                        "remediation_candidates": remediation_candidates,
                    }
                )
                failure_code = str(classification.get("failure_code") or "")
                # Exact translation safe-omission evidence can use the same
                # bounded retry budget and compare-and-set queue transition as
                # transient sweep items. ASR evidence still goes through the
                # review autopilot because it may require an immutable rejected
                # transcript checkpoint or a full prompt-free rebuild.
                if failure_code != "translation_safe_omission":
                    continue
                if failure_code not in allowed_codes:
                    counters["unsupported"] += 1
                    continue
                if float(candidate.get("next_retry_at") or 0) > now:
                    counters["not_due"] += 1
                    continue
                if now - float(candidate.get("updated_at") or 0) < min_age_seconds:
                    counters["too_new"] += 1
                    continue
                if int(candidate.get("attempts") or 0) >= max_attempts:
                    counters["attempt_ceiling"] += 1
                    continue
                if not Path(path).is_file():
                    counters["missing_media"] += 1
                    continue
                semantic_key = (
                    path,
                    str(candidate.get("failure_revision") or ""),
                    "retry_preserve_budget",
                )
                if semantic_key in processed_keys:
                    counters["already_processed"] += 1
                    continue
                counters["eligible"] += 1
                eligible.append(
                    {
                        "path": path,
                        "failure_revision": str(candidate.get("failure_revision") or ""),
                        "failure_code": failure_code,
                        "attempts": int(candidate.get("attempts") or 0),
                        "strategy": "retry_preserve_budget",
                        "review_id": "",
                    }
                )
                continue
        if float(candidate.get("next_retry_at") or 0) > now:
            counters["not_due"] += 1
            continue
        if now - float(candidate.get("updated_at") or 0) < min_age_seconds:
            counters["too_new"] += 1
            continue
        if int(candidate.get("attempts") or 0) >= max_attempts:
            counters["attempt_ceiling"] += 1
            continue
        failure_code = str(candidate.get("last_error_code") or "")
        if failure_code not in allowed_codes:
            counters["unsupported"] += 1
            continue
        if not Path(path).is_file():
            counters["missing_media"] += 1
            continue
        semantic_key = (
            path,
            str(candidate.get("failure_revision") or ""),
            "retry_preserve_budget",
        )
        if semantic_key in processed_keys:
            counters["already_processed"] += 1
            continue
        counters["eligible"] += 1
        eligible.append(
            {
                "path": path,
                "failure_revision": str(candidate.get("failure_revision") or ""),
                "failure_code": failure_code,
                "attempts": int(candidate.get("attempts") or 0),
                "strategy": "retry_preserve_budget",
                "review_id": "",
            }
        )
    return {
        "previewed_at": now,
        "counters": counters,
        "eligible_items": eligible[:20],
        "eligible_truncated": len(eligible) > 20,
        "review_required_items": review_required_items[:20],
        "review_required_truncated": len(review_required_items) > 20,
    }


def _advance_ai_failed_retry_sweep(config, logger) -> None:
    from control_state import (
        create_auto_remediation_item,
        due_auto_remediation_campaign,
        get_auto_remediation_item,
        update_auto_remediation_campaign,
        update_auto_remediation_item,
    )
    from scan_state import ScanStateStore

    campaign = due_auto_remediation_campaign(config)
    if campaign is None:
        return
    campaign_id = str(campaign.get("campaign_id") or "")
    parameters = campaign.get("parameters") if isinstance(campaign.get("parameters"), dict) else {}
    counters = dict(campaign.get("counters") or {})
    counters.setdefault("selected", 0)
    counters.setdefault("processed", 0)
    counters.setdefault("succeeded", 0)
    counters.setdefault("failed", 0)
    counters.setdefault("blocked_review", 0)
    interval_seconds = int(parameters.get("interval_seconds") or 300)
    max_items = int(parameters.get("max_items") or 1)
    current_item_id = str(campaign.get("current_item_id") or "")

    if current_item_id:
        item = get_auto_remediation_item(config, current_item_id)
        if item is None:
            update_auto_remediation_campaign(
                config,
                campaign_id,
                state="failed",
                counters=counters,
                last_error="current remediation item is missing",
            )
            return
        state = ScanStateStore.from_config(config)
        try:
            snapshot = state.ai_queue_candidate_snapshot(Path(str(item.get("path") or "")))
        finally:
            state.close()
        if snapshot is None:
            update_auto_remediation_item(
                config,
                current_item_id,
                status="failed",
                error="AI queue row disappeared during remediation",
            )
            counters["processed"] += 1
            counters["failed"] += 1
            update_auto_remediation_campaign(
                config,
                campaign_id,
                state="paused",
                counters=counters,
                current_item_id="",
                last_error="AI queue row disappeared during remediation",
            )
            return
        queue_status = str(snapshot.get("status") or "")
        if queue_status in {"queued", "running"}:
            update_auto_remediation_item(
                config,
                current_item_id,
                status="running",
                result={"queue_status": queue_status},
            )
            update_auto_remediation_campaign(
                config,
                campaign_id,
                counters=counters,
                next_run_at=time.time() + 5,
            )
            return
        if queue_status == "done":
            update_auto_remediation_item(
                config,
                current_item_id,
                status="succeeded",
                result={"queue_status": queue_status},
            )
            counters["processed"] += 1
            counters["succeeded"] += 1
            finished = counters["processed"] >= max_items
            update_auto_remediation_campaign(
                config,
                campaign_id,
                state="completed" if finished else "running",
                counters=counters,
                current_item_id="",
                next_run_at=time.time() + interval_seconds,
            )
            return
        terminal_status = "blocked_review" if queue_status == "paused" else "failed"
        update_auto_remediation_item(
            config,
            current_item_id,
            status=terminal_status,
            result={
                "queue_status": queue_status,
                "failure_revision": str(snapshot.get("failure_revision") or ""),
                "failure_code": str(snapshot.get("last_error_code") or ""),
            },
            error=str(snapshot.get("last_error") or f"queue entered {queue_status}"),
        )
        counters["processed"] += 1
        counters["blocked_review" if terminal_status == "blocked_review" else "failed"] += 1
        update_auto_remediation_campaign(
            config,
            campaign_id,
            state="paused",
            counters=counters,
            current_item_id="",
            last_error=(
                "automatic retry created or reached a review"
                if terminal_status == "blocked_review"
                else f"automatic retry ended in {queue_status}"
            ),
        )
        return

    preview = _preview_ai_failed_retry_sweep(config, parameters)
    eligible = list(preview.get("eligible_items") or [])
    if not eligible:
        update_auto_remediation_campaign(
            config,
            campaign_id,
            state="completed",
            counters={**counters, "last_preview": preview.get("counters") or {}},
            current_item_id="",
        )
        return
    selected = dict(eligible[0])
    path = Path(str(selected["path"]))
    before: dict[str, object]
    state = ScanStateStore.from_config(config)
    try:
        before = state.ai_queue_candidate_snapshot(path) or {}
    finally:
        state.close()
    item = create_auto_remediation_item(
        config,
        campaign_id=campaign_id,
        path=str(path),
        failure_revision=str(selected["failure_revision"]),
        strategy=str(selected["strategy"]),
        before=before,
    )
    item_id = str(item.get("item_id") or "")
    counters["selected"] += 1
    state = ScanStateStore.from_config(config)
    try:
        if selected["strategy"] == "pause_existing_review":
            applied = state.pause_failed_retry_for_review(
                path,
                expected_failure_revision=str(selected["failure_revision"]),
                message=f"Open review {selected.get('review_id') or '-'} blocks automatic retry",
            )
        else:
            applied = state.queue_failed_retry_preserving_budget(
                path,
                expected_failure_revision=str(selected["failure_revision"]),
            )
        if applied:
            state.commit()
        else:
            state.rollback()
    finally:
        state.close()
    if not applied:
        update_auto_remediation_item(
            config,
            item_id,
            status="failed",
            error="failure revision or queue state changed before mutation",
        )
        counters["processed"] += 1
        counters["failed"] += 1
        update_auto_remediation_campaign(
            config,
            campaign_id,
            state="paused",
            counters=counters,
            current_item_id="",
            last_error="failure revision or queue state changed before mutation",
        )
        return
    if selected["strategy"] == "pause_existing_review":
        update_auto_remediation_item(
            config,
            item_id,
            status="blocked_review",
            result={"review_id": selected.get("review_id"), "queue_status": "paused"},
        )
        counters["processed"] += 1
        counters["blocked_review"] += 1
        finished = counters["processed"] >= max_items
        update_auto_remediation_campaign(
            config,
            campaign_id,
            state="completed" if finished else "running",
            counters=counters,
            current_item_id="",
            next_run_at=time.time() + interval_seconds,
        )
        return
    update_auto_remediation_item(
        config,
        item_id,
        status="running",
        result={"queue_status": "queued"},
    )
    update_auto_remediation_campaign(
        config,
        campaign_id,
        counters=counters,
        current_item_id=item_id,
        next_run_at=time.time() + 5,
    )
    logger.warning(
        "Safety-gated AI retry sweep queued one item without resetting attempts. "
        "campaign=%s item=%s path=%s",
        campaign_id,
        item_id,
        path,
    )


def _queue_selective_asr_review_command(
    config,
    video: Path,
    *,
    review: dict[str, object],
    expected_failure_revision: str,
    expected_review_evidence_revision: str,
    policy_revision: str,
) -> str:
    """Restore one verified rejected transcript and queue its range repair."""

    from lock import VideoLock
    from safe_files import sha256_file, verified_copy_replace
    from scan_state import ScanStateStore
    from transcriber import asr_diagnostics_path

    evidence = _selective_asr_review_evidence(config, review, video)
    if (
        evidence is None
        or str(evidence.get("evidence_revision") or "")
        != str(expected_review_evidence_revision or "")
    ):
        raise ValueError("selective ASR review evidence changed before execution")
    checkpoint = evidence["checkpoint"]
    lock = VideoLock(video)
    lock_deadline = time.monotonic() + 10.0
    while not lock.acquire():
        if time.monotonic() >= lock_deadline:
            raise RuntimeError(
                f"video remained busy; selective ASR checkpoint was not restored: {video}"
            )
        time.sleep(0.25)
    state = None
    created: list[tuple[Path, str]] = []
    committed = False
    try:
        # Re-read every immutable artifact after acquiring the same video lock
        # used by the Worker. A changed checkpoint remains a zero-mutation
        # failure, while an interrupted prior restore may resume idempotently.
        evidence = _selective_asr_review_evidence(config, review, video)
        if (
            evidence is None
            or str(evidence.get("evidence_revision") or "")
            != str(expected_review_evidence_revision or "")
        ):
            raise ValueError("selective ASR review evidence is no longer current")
        checkpoint = evidence["checkpoint"]
        target_srt = Path(checkpoint.target_path)
        target_diagnostics = asr_diagnostics_path(target_srt, config)
        artifacts = (
            (
                Path(checkpoint.diagnostics_path),
                target_diagnostics,
                str(checkpoint.diagnostics_sha256),
            ),
            # Publish the rejected SRT last. A crash may leave diagnostics
            # without a transcript (safe and ignored), but never a rejected
            # transcript without the diagnostic that forces selective repair.
            (
                Path(checkpoint.rejected_srt_path),
                target_srt,
                str(checkpoint.rejected_srt_sha256),
            ),
        )
        for source, destination, expected_sha256 in artifacts:
            if sha256_file(source) != expected_sha256:
                raise RuntimeError(
                    f"selective ASR checkpoint artifact changed: {source}"
                )
            if destination.exists():
                if not destination.is_file() or sha256_file(destination) != expected_sha256:
                    raise RuntimeError(
                        "selective ASR destination already contains different evidence: "
                        f"{destination}"
                    )
                continue
            verified_copy_replace(source, destination)
            if sha256_file(destination) != expected_sha256:
                destination.unlink(missing_ok=True)
                raise RuntimeError(
                    f"selective ASR checkpoint restore hash mismatch: {destination}"
                )
            created.append((destination, expected_sha256))

        state = ScanStateStore.from_config(config)
        snapshot = state.ai_queue_candidate_snapshot(video) or {}
        if (
            str(snapshot.get("status") or "").strip().casefold() != "paused"
            or str(snapshot.get("failure_revision") or "").strip()
            != str(expected_failure_revision or "").strip()
        ):
            raise ValueError(
                "selective ASR remediation no longer matches the paused failure revision"
            )
        if not state.queue_paused_review_remediation(
            video,
            expected_failure_revision=expected_failure_revision,
            policy_revision=policy_revision,
        ):
            raise ValueError("selective ASR remediation lost its queue revision race")
        state.commit()
        committed = True
        return (
            "Queued selective ASR repair from checkpoint "
            f"{checkpoint.checkpoint_id} ranges={len(checkpoint.review_ranges)}"
        )
    except Exception:
        if state is not None and not committed:
            try:
                state.rollback()
            except Exception:
                pass
        if not committed:
            for destination, expected_sha256 in reversed(created):
                try:
                    if destination.is_file() and sha256_file(destination) == expected_sha256:
                        destination.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        if state is not None:
            state.close()
        lock.release()


def _run_ai_reprocess_command(
    config,
    video: Path,
    *,
    mode: str,
    queue_mode: str = "manual_force",
    expected_failure_revision: str = "",
    policy_revision: str = "",
) -> str:
    command = [
        sys.executable,
        "/app/reprocess_ai_video.py",
        "--config",
        "/app/config.yaml",
        "--video-path",
        str(video),
        "--mode",
        str(mode),
    ]
    if str(queue_mode) == "auto_review":
        command.extend(
            [
                "--queue-mode",
                "auto_review",
                "--expected-failure-revision",
                str(expected_failure_revision),
                "--policy-revision",
                str(policy_revision),
            ]
        )
    completed = subprocess.run(command, text=True, capture_output=True, timeout=900, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "AI reprocess command failed")[-2000:])
    return completed.stdout[-2000:]


def _run_control_subprocess(command: list[str], *, timeout_seconds: int) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Worker maintenance command failed")[-4000:])
    return completed.stdout[-4000:]


def _run_ai_retranslate_lines_command(config, video: Path, *, lines: str) -> str:
    command = [
        sys.executable,
        "/app/retranslate_ai_lines.py",
        "--config",
        "/app/config.yaml",
        "--video-path",
        str(video),
        "--lines",
        str(lines),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=900, check=False)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "AI line retranslation failed")[-2000:])
    return completed.stdout[-2000:]


def _validated_control_target_path(config, value: str, *, require_file: bool) -> Path:
    root = Path(config.input_path).resolve()
    candidate = Path(str(value or "").strip()).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"control target must stay inside input_path: {candidate}") from exc
    if require_file:
        if not candidate.is_file() or candidate.suffix.casefold() not in {
            str(extension).casefold() for extension in config.video_extensions
        }:
            raise ValueError(f"control target is not a supported video file: {candidate}")
    elif not candidate.exists():
        raise ValueError(f"control target does not exist: {candidate}")
    return candidate


def _validated_review_target_candidate(config, review: dict, value: str) -> tuple[Path, Path, int]:
    """Validate an exact review candidate and derive its series mapping."""

    selected = _validated_control_target_path(config, value, require_file=True)
    matched_candidate: dict | None = None
    for candidate in review.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if not _review_candidate_has_semantic_evidence(candidate):
            continue
        candidate_value = str(candidate.get("path") or candidate.get("series_path") or "").strip()
        if not candidate_value:
            continue
        try:
            stored = _validated_control_target_path(config, candidate_value, require_file=True)
        except ValueError:
            continue
        if stored == selected:
            matched_candidate = candidate
            break
    if matched_candidate is None:
        raise ValueError("selected target is not one of the stored review candidates")

    season_directory = selected.parent.name
    match = re.fullmatch(r"Season\s+(\d+)", season_directory, flags=re.IGNORECASE)
    if match:
        season = int(match.group(1))
    elif season_directory.casefold() == "specials":
        season = 0
    else:
        raise ValueError("selected review candidate is not inside Season N or Specials")
    declared_season = matched_candidate.get("season")
    if declared_season not in (None, "") and int(declared_season) != season:
        raise ValueError("stored review candidate season does not match its path")

    series_directory = _validated_control_target_path(
        config,
        str(selected.parent.parent),
        require_file=False,
    )
    return selected, series_directory, season


def _review_release_identity(review: dict) -> tuple[str, int | None]:
    diagnosis = dict(review.get("diagnosis") or {})
    explicit = diagnosis.get("season")
    if explicit in (None, ""):
        explicit = (diagnosis.get("recovery") or {}).get("season")
    try:
        season = int(explicit) if explicit not in (None, "") else None
    except (TypeError, ValueError):
        season = None
    if season is not None and not 0 <= season <= 99:
        season = None

    value = str(
        diagnosis.get("torrent_name")
        or diagnosis.get("title")
        or diagnosis.get("source_title")
        or review.get("summary")
        or ""
    ).strip()
    value = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", value).strip()
    value = value.split("[", 1)[0].strip()
    named_season = re.search(r"\b(?:season|s)\s*(\d{1,2})\s*$", value, flags=re.IGNORECASE)
    if named_season:
        if season is None:
            season = int(named_season.group(1))
        value = value[: named_season.start()].strip()
    elif not re.search(r"\s-\s\d{1,4}\s*$", value):
        trailing_season = re.search(r"\s+(\d{1,2})\s*$", value)
        if trailing_season:
            if season is None:
                season = int(trailing_season.group(1))
            value = value[: trailing_season.start()].strip()
    value = re.sub(r"\s-\s\d{1,4}\s*$", "", value).strip()
    return value, season


def _review_target_episode(review: dict) -> int | None:
    from mikan_source import extract_episode_numbers

    diagnosis = dict(review.get("diagnosis") or {})
    episode_values: list[int] = []
    raw_episode = diagnosis.get("episode")
    if str(raw_episode or "").strip().isdigit():
        episode_values.append(int(raw_episode))
    for value in diagnosis.get("episodes") or []:
        if str(value or "").strip().isdigit():
            episode_values.append(int(value))
    for key in ("torrent_name", "title", "source_title"):
        episode_values.extend(extract_episode_numbers(str(diagnosis.get(key) or "")))
    return next((value for value in episode_values if 0 < value <= 9999), None)


def _review_profile_title_score(profile, release_title: str) -> float:
    from mikan_matcher import _title_identity_similarity, normalize_match_text

    release = normalize_match_text(release_title)
    if len(release) < 3:
        return 0.0
    values = [
        profile.canonical_title,
        Path(profile.local_path).name,
        *profile.titles,
        *profile.aliases,
    ]
    best = 0.0
    for value in values:
        candidate = normalize_match_text(str(value or ""))
        if len(candidate) < 3:
            continue
        best = max(best, _title_identity_similarity(release, candidate))
    return min(best, 1.0)


_REVIEW_PROFILE_EXACT_TITLE_SCORE = 0.999


def _auto_rebuild_review_target_candidates(
    config,
    review: dict,
    *,
    review_id: str,
) -> dict[str, object]:
    """Find one verifiable local target without asking the operator for IDs or paths."""

    from mikan_source import extract_episode_number
    from series_metadata import SeriesMetadataStore, canonical_local_path, stable_series_id

    diagnosis = dict(review.get("diagnosis") or {})
    bangumi_ids = {
        int(value)
        for value in diagnosis.get("bangumi_ids") or []
        if str(value or "").strip().isdigit() and int(value) > 0
    }
    if not bangumi_ids:
        raise ValueError("automatic safety check needs at least one diagnosed Mikan id")

    release_title, season_hint = _review_release_identity(review)
    if not release_title:
        raise ValueError("automatic safety check could not derive a series title")
    target_episode = _review_target_episode(review)
    if target_episode is None:
        raise ValueError("automatic safety check could not derive the target episode")

    with SeriesMetadataStore.from_config(config) as store:
        profiles_by_path = {
            str(profile.local_path).casefold(): profile
            for profile in store.list_by_mikan_ids(bangumi_ids)
        }
        offset = 0
        while True:
            batch = store.list_profiles(limit=1000, offset=offset)
            for profile in batch:
                if profile.mikan_bangumi_id is not None:
                    profiles_by_path.setdefault(str(profile.local_path).casefold(), profile)
            if len(batch) < 1000:
                break
            offset += len(batch)
    profiles = list(profiles_by_path.values())
    profiles = [
        (profile, _review_profile_title_score(profile, release_title))
        for profile in profiles
    ]
    profiles = [
        (profile, score)
        for profile, score in profiles
        if score >= _REVIEW_PROFILE_EXACT_TITLE_SCORE
    ]
    if not profiles:
        raise ValueError(
            "automatic safety check found no uniquely title-matched local series profile"
        )

    extensions = {str(extension).casefold() for extension in config.video_extensions}
    possible: dict[str, tuple[object, int, float]] = {}
    for profile, title_score in profiles:
        try:
            series_directory = _validated_control_target_path(
                config,
                profile.local_path,
                require_file=False,
            )
        except (OSError, ValueError):
            continue
        if not series_directory.is_dir():
            continue
        season_directories: list[tuple[int, Path]] = []
        try:
            children = list(series_directory.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            match = re.fullmatch(r"Season\s+0*(\d+)", child.name, flags=re.IGNORECASE)
            child_season = int(match.group(1)) if match else (0 if child.name.casefold() == "specials" else None)
            if child_season is None or (season_hint is not None and child_season != season_hint):
                continue
            season_directories.append((child_season, child))
        for child_season, season_directory in season_directories:
            try:
                videos = (
                    path.resolve()
                    for path in season_directory.rglob("*")
                    if path.is_file() and path.suffix.casefold() in extensions
                )
                for video in videos:
                    if extract_episode_number(video.name) == target_episode:
                        possible[str(video)] = (profile, child_season, title_score)
            except OSError:
                continue

    if len(possible) != 1:
        raise ValueError(
            "automatic safety check did not find exactly one target video; "
            f"found {len(possible)} and manual selection is required"
        )
    selected_path, (profile, season, title_score) = next(iter(possible.items()))
    series_id = stable_series_id(canonical_local_path(profile.local_path))
    result = _rebuild_review_target_candidates(
        config,
        review,
        review_id=review_id,
        series_id=series_id,
        season=season,
        candidate_reasons=[
            "title_verified",
            "series_mapping:auto_review_recovery",
            "episode_exact",
        ],
        recovery_method="automatic",
    )
    result.update(
        {
            "action": "review.auto_rebuild_target_candidates",
            "auto_selected": True,
            "title_score": round(float(title_score), 3),
            "candidate_path": selected_path,
        }
    )
    return result


def _resolve_automatic_target_review(
    config,
    logger,
    *,
    review_id: str,
    result: dict[str, object],
    original_source_ids: set[int],
    policy_revision: str,
) -> dict[str, object]:
    """Resolve one uniquely rebuilt target through the normal strict command path."""

    source_id = str(result.get("source_id") or "").strip()
    candidate_path = str(result.get("candidate_path") or "").strip()
    series_id = str(result.get("series_id") or "").strip()
    season = result.get("season")
    if (
        not bool(result.get("auto_selected"))
        or not source_id
        or not candidate_path
        or not series_id
        or season is None
        or not source_id.isdigit()
        or int(source_id) not in original_source_ids
    ):
        raise ValueError(
            "automatic target resolution requires one verified rebuilt candidate"
        )
    resolution = _execute_control_command(
        config,
        logger,
        "review.resolve_target",
        review_id,
        {
            "review_id": review_id,
            "source_id": source_id,
            "candidate_path": candidate_path,
            "series_id": series_id,
            "season": int(season),
            "automatic_review": True,
            "policy_revision": str(policy_revision or "").strip(),
        },
    )
    if not bool(resolution.get("resolved")):
        raise RuntimeError(
            f"automatic target resolution did not close review {review_id}"
        )
    return resolution


def _rebuild_review_target_candidates(
    config,
    review: dict,
    *,
    review_id: str,
    series_id: str,
    season: int,
    candidate_reasons: list[str] | None = None,
    recovery_method: str = "manual",
) -> dict[str, object]:
    """Rebuild safe candidates inside one operator-selected series/season.

    The stable series id is resolved by the Worker, and every resulting path
    is constrained to that profile and checked on disk.  This deliberately
    does not accept an arbitrary media path from the WebUI.
    """

    from control_state import upsert_review_item
    from mikan_source import extract_episode_number
    from series_metadata import SeriesMetadataStore

    with SeriesMetadataStore.from_config(config) as store:
        profile = store.get_by_series_id(series_id)
    if profile is None:
        raise ValueError(f"series profile does not exist: {series_id}")

    bangumi_id = profile.mikan_bangumi_id
    if bangumi_id is None and profile.provider.casefold() == "mikan" and profile.provider_id.isdigit():
        bangumi_id = int(profile.provider_id)
    if bangumi_id is None:
        raise ValueError("selected series profile has no verified Mikan bangumi id")

    series_directory = _validated_control_target_path(
        config,
        profile.local_path,
        require_file=False,
    )
    if not series_directory.is_dir():
        raise ValueError(f"selected series path is not a directory: {series_directory}")

    season_directories: list[Path] = []
    for child in series_directory.iterdir():
        if not child.is_dir():
            continue
        match = re.fullmatch(r"Season\s+0*(\d+)", child.name, flags=re.IGNORECASE)
        child_season = int(match.group(1)) if match else (0 if child.name.casefold() == "specials" else None)
        if child_season == season:
            season_directories.append(child)
    if len(season_directories) != 1:
        raise ValueError(
            f"selected series must contain exactly one season {season} directory; "
            f"found {len(season_directories)}"
        )
    season_directory = season_directories[0]

    diagnosis = dict(review.get("diagnosis") or {})
    target_episode = _review_target_episode(review)

    extensions = {str(extension).casefold() for extension in config.video_extensions}
    videos = sorted(
        (
            path.resolve()
            for path in season_directory.rglob("*")
            if path.is_file() and path.suffix.casefold() in extensions
        ),
        key=lambda path: str(path).casefold(),
    )
    if target_episode is None:
        if len(videos) == 1:
            target_episode = extract_episode_number(videos[0].name)
        else:
            raise ValueError(
                "review does not identify an episode; select a series profile with usable episode evidence"
            )
    matches = [path for path in videos if extract_episode_number(path.name) == target_episode]
    if not matches:
        raise ValueError(
            f"selected series season {season} has no video for episode {target_episode}"
        )

    candidate_count = len(matches)
    verified_reasons = list(candidate_reasons or [
        "manual_mapping",
        "series_mapping:review_recovery",
        "episode_exact",
    ])
    candidates = [
        {
            "path": str(path),
            "series_path": str(series_directory),
            "series_id": series_id,
            "series_title": profile.canonical_title,
            "source_id": str(int(bangumi_id)),
            "season": season,
            "episode": target_episode,
            "score": 2000,
            "margin": 1000 if candidate_count == 1 else 0,
            "reasons": list(verified_reasons),
        }
        for path in matches
    ]
    prior_ids = [
        int(value)
        for value in diagnosis.get("bangumi_ids") or []
        if str(value or "").strip().isdigit() and int(value) != int(bangumi_id)
    ]
    diagnosis["bangumi_ids"] = [int(bangumi_id), *prior_ids]
    diagnosis["recovery"] = {
        "series_id": series_id,
        "series_path": str(series_directory),
        "series_title": profile.canonical_title,
        "source_id": str(int(bangumi_id)),
        "season": season,
        "episode": target_episode,
        "candidate_count": candidate_count,
        "method": recovery_method,
        "verified_at": time.time(),
    }
    target_key = str(review.get("target_key") or "").strip()
    if not target_key:
        raise ValueError(f"target ambiguity review is missing its target key: {review_id}")
    persisted_id = upsert_review_item(
        config,
        kind="target_ambiguity",
        target_key=target_key,
        summary=str(review.get("summary") or "Target mapping requires review"),
        diagnosis=diagnosis,
        candidates=candidates,
        severity=str(review.get("severity") or "warning"),
        replace_candidates=True,
    )
    if persisted_id != review_id:
        raise RuntimeError("review recovery changed the stable review id")
    return {
        "action": "review.rebuild_target_candidates",
        "review_id": review_id,
        "series_id": series_id,
        "source_id": str(int(bangumi_id)),
        "season": season,
        "episode": target_episode,
        "candidate_count": candidate_count,
    }


def _review_candidate_has_semantic_evidence(candidate: dict) -> bool:
    reasons = {
        str(reason or "").strip().casefold()
        for reason in candidate.get("reasons") or []
        if str(reason or "").strip()
    }
    return any(
        reason.startswith(("title_", "sequel_token:", "series_mapping:", "locked_mapping:"))
        or reason in {"manual_mapping", "bangumi_mapping"}
        for reason in reasons
    )


def _background_ai_scan_loop(
    config,
    logger,
    shutdown_event: threading.Event,
    event_watcher=None,
) -> None:
    from scanner import VideoScanner

    scanner = VideoScanner(config, logger)
    interval, startup_delay = _background_ai_scan_schedule(
        config,
        event_watcher=event_watcher,
    )
    if startup_delay and shutdown_event.wait(startup_delay):
        return
    while not shutdown_event.is_set():
        if _deployment_hold_active(config):
            if shutdown_event.wait(1.0):
                break
            continue
        try:
            scanner.refresh_queue(reconcile_batch=True)
        except Exception as exc:
            logger.exception("Unhandled background AI scan error: %s", exc)
        interval, _unused_startup_delay = _background_ai_scan_schedule(
            config,
            event_watcher=event_watcher,
        )
        wait_seconds = interval if scanner.reconcile_cycle_complete else max(
            5,
            int(getattr(config, "storage_io_pressure_backoff_seconds", 2.0) or 2.0),
        )
        if _wait_for_background_ai_scan(
            shutdown_event,
            wait_seconds,
            config,
            event_watcher=event_watcher,
        ):
            break


def _background_ai_ledger_backfill_loop(
    config,
    logger,
    shutdown_event: threading.Event,
) -> None:
    """Continuously drain queue-ledger gaps without walking the media root."""

    from scanner import VideoScanner

    scanner = VideoScanner(config, logger)
    interval = max(
        1,
        int(
            getattr(
                config,
                "scanner_active_queue_ledger_backfill_interval_seconds",
                10,
            )
            or 10
        ),
    )
    no_progress_interval = max(
        interval,
        int(
            getattr(
                config,
                "scanner_active_queue_ledger_backfill_no_progress_seconds",
                300,
            )
            or 300
        ),
    )
    batch_size = max(
        1,
        int(
            getattr(
                config,
                "scanner_active_queue_ledger_backfill_batch_size",
                250,
            )
            or 250
        ),
    )
    while not shutdown_event.is_set():
        if _deployment_hold_active(config):
            if shutdown_event.wait(min(1.0, float(interval))):
                break
            continue
        result: dict[str, int] = {}
        retry_soon = False
        try:
            result = scanner.backfill_active_queue_obligations(
                limit=batch_size,
                cancel_event=shutdown_event,
            )
        except Exception as exc:  # noqa: BLE001 - next bounded interval retries safely.
            logger.exception("Unhandled active queue ledger backfill error: %s", exc)
            retry_soon = True
        made_progress = bool(result.get("repaired") or result.get("queue_changed"))
        retry_soon = retry_soon or bool(
            result.get("database_busy")
            or result.get("media_changed")
            or result.get("running_active")
            or result.get("cancelled")
        )
        wait_seconds = interval if made_progress or retry_soon else no_progress_interval
        if shutdown_event.wait(wait_seconds):
            break


def _background_ai_scan_schedule(config, *, event_watcher=None) -> tuple[int, int]:
    watcher_configured = bool(getattr(config, "scanner_event_watch_enabled", False))
    watcher_alive = False
    if watcher_configured and event_watcher is not None:
        try:
            watcher_alive = bool(event_watcher.is_alive())
        except Exception:
            watcher_alive = False
    if not watcher_configured or not watcher_alive:
        return max(1, int(config.watch_interval_seconds)), 0
    return (
        max(1, int(getattr(config, "scanner_background_scan_interval_seconds", 21600) or 21600)),
        max(0, int(getattr(config, "scanner_background_scan_startup_delay_seconds", 600) or 0)),
    )


def _wait_for_background_ai_scan(
    shutdown_event: threading.Event,
    wait_seconds: int | float,
    config,
    *,
    event_watcher=None,
) -> bool:
    """Wait for the next reconciliation while continuously auditing watcher health.

    A six-hour reconciliation sleep must not hide a watcher that dies after the
    schedule was selected.  Long watcher-backed waits are split into bounded
    health intervals; a dead watcher ends the wait so the loop immediately runs
    a full reconciliation.  Ordinary polling mode remains a single wait and
    therefore cannot spin when watchdog is unavailable.
    """

    remaining = max(0.0, float(wait_seconds))
    fallback_interval = max(1.0, float(getattr(config, "watch_interval_seconds", 1) or 1))
    watcher_backed = (
        bool(getattr(config, "scanner_event_watch_enabled", False))
        and event_watcher is not None
        and remaining > fallback_interval
    )
    if not watcher_backed:
        return bool(shutdown_event.wait(remaining))

    health_interval = max(
        1.0,
        float(
            getattr(
                config,
                "scanner_event_watch_health_interval_seconds",
                30.0,
            )
            or 30.0
        ),
    )
    while remaining > 0:
        try:
            watcher_alive = bool(event_watcher.is_alive())
        except Exception:
            watcher_alive = False
        if not watcher_alive:
            return False
        chunk = min(remaining, health_interval)
        if shutdown_event.wait(chunk):
            return True
        remaining -= chunk
    return False


def _start_background_state_backup(config, logger, shutdown_event: threading.Event):
    if not bool(getattr(config, "state_backup_enabled", True)):
        logger.info("Background state backup disabled by configuration.")
        return None
    thread = threading.Thread(
        target=_background_state_backup_loop,
        args=(config, logger, shutdown_event),
        daemon=True,
        name="worker-state-backup",
    )
    thread.start()
    logger.info(
        "Background state backup started. interval_hours=%s retention=%s",
        getattr(config, "state_backup_interval_hours", 24),
        getattr(config, "state_backup_retention_count", 14),
    )
    return thread


def _start_background_series_metadata_sync(config, logger, shutdown_event: threading.Event):
    if not bool(getattr(config, "series_metadata_sync_enabled", True)):
        logger.info("Background series metadata index sync disabled by configuration.")
        return None
    thread = threading.Thread(
        target=_background_series_metadata_sync_loop,
        args=(config, logger, shutdown_event),
        daemon=True,
        name="series-metadata-index-sync",
    )
    thread.start()
    logger.info(
        "Background series metadata index sync started. interval=%s startup_delay=%s",
        int(getattr(config, "series_metadata_sync_interval_seconds", 21600) or 21600),
        int(getattr(config, "series_metadata_sync_startup_delay_seconds", 30) or 0),
    )
    return thread


def _start_background_database_maintenance(config, logger, shutdown_event: threading.Event):
    if not bool(getattr(config, "database_maintenance_enabled", True)):
        logger.info("Background database maintenance disabled by configuration.")
        return None
    thread = threading.Thread(
        target=_background_database_maintenance_loop,
        args=(config, logger, shutdown_event),
        daemon=True,
        name="worker-database-maintenance",
    )
    thread.start()
    logger.info(
        "Background database maintenance started. interval_hours=%s startup_delay=%s",
        int(getattr(config, "database_maintenance_interval_hours", 168) or 168),
        int(getattr(config, "database_maintenance_startup_delay_seconds", 1800) or 0),
    )
    return thread


def _background_database_maintenance_loop(config, logger, shutdown_event: threading.Event) -> None:
    from database_maintenance import optimize_databases

    startup_delay = max(
        0,
        int(getattr(config, "database_maintenance_startup_delay_seconds", 1800) or 0),
    )
    if startup_delay and shutdown_event.wait(startup_delay):
        return

    interval = max(
        3600,
        int(getattr(config, "database_maintenance_interval_hours", 168) or 168) * 3600,
    )
    retry_seconds = min(3600, interval)
    state_path = Path(config.work_path) / "database_maintenance_state.json"
    while not shutdown_event.is_set():
        last_checked_at = _database_maintenance_last_checked_at(state_path)
        now = time.time()
        if last_checked_at > 0 and now - last_checked_at < interval:
            wait_seconds = min(3600.0, max(1.0, interval - (now - last_checked_at)))
            if shutdown_event.wait(wait_seconds):
                break
            continue

        try:
            result = optimize_databases(
                config,
                apply=True,
                wait_seconds=0,
                online_only=True,
                min_reclaim_mib=max(
                    0.0,
                    float(getattr(config, "database_maintenance_min_reclaim_mib", 64.0) or 0.0),
                ),
                min_freelist_ratio=max(
                    0.0,
                    min(
                        1.0,
                        float(getattr(config, "database_maintenance_min_freelist_ratio", 0.25) or 0.0),
                    ),
                ),
            )
            status = str(result.get("status") or "unknown")
            if status == "busy":
                logger.info(
                    "Scheduled database maintenance deferred until idle. reasons=%s retry_in=%ss",
                    ",".join(str(item) for item in result.get("busy_reasons") or []) or "busy",
                    retry_seconds,
                )
                if shutdown_event.wait(retry_seconds):
                    break
                continue
            _write_database_maintenance_state(
                state_path,
                {
                    "last_checked_at": time.time(),
                    "status": status,
                    "optimized": len(result.get("optimized") or []),
                },
            )
            logger.info(
                "Scheduled database maintenance complete. status=%s optimized=%s",
                status,
                len(result.get("optimized") or []),
            )
        except Exception as exc:  # noqa: BLE001 - maintenance must never stop the worker.
            logger.exception("Scheduled database maintenance failed; will retry. error=%s", exc)
            if shutdown_event.wait(retry_seconds):
                break


def _database_maintenance_last_checked_at(path: Path) -> float:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return max(0.0, float(payload.get("last_checked_at") or 0.0)) if isinstance(payload, dict) else 0.0
    except (OSError, TypeError, ValueError):
        return 0.0


def _write_database_maintenance_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _background_series_metadata_sync_loop(config, logger, shutdown_event: threading.Event) -> None:
    from series_metadata_sync import sync_series_metadata

    startup_delay = max(0, int(getattr(config, "series_metadata_sync_startup_delay_seconds", 30) or 0))
    if startup_delay and shutdown_event.wait(startup_delay):
        return
    interval = max(300, int(getattr(config, "series_metadata_sync_interval_seconds", 21600) or 21600))
    while not shutdown_event.is_set():
        try:
            sync_series_metadata(config, logger)
        except Exception as exc:  # noqa: BLE001 - optional metadata must never stop auto-watch.
            logger.exception("Background series metadata index sync failed: %s", exc)
        if shutdown_event.wait(interval):
            break


def _background_state_backup_loop(config, logger, shutdown_event: threading.Event) -> None:
    from backup_state import StateBackupError, create_state_backup

    startup_delay = max(0, int(getattr(config, "state_backup_startup_delay_seconds", 120) or 0))
    if startup_delay and shutdown_event.wait(startup_delay):
        return
    interval = max(3600, int(getattr(config, "state_backup_interval_hours", 24) or 24) * 3600)
    root = Path(str(getattr(config, "state_backup_path", "state_backups") or "state_backups"))
    if not root.is_absolute():
        root = Path(config.work_path) / root
    while not shutdown_event.is_set():
        try:
            manifests = list(root.glob("*/manifest.json")) if root.is_dir() else []
            latest_mtime = max((item.stat().st_mtime for item in manifests), default=0.0)
            if not latest_mtime or time.time() - latest_mtime >= interval:
                result = create_state_backup(config)
                logger.info(
                    "Scheduled Worker state backup complete. backup=%s entries=%s verified=%s",
                    result.get("backup"),
                    result.get("available_entries"),
                    result.get("verified"),
                )
        except (OSError, ValueError, StateBackupError) as exc:
            logger.warning("Scheduled Worker state backup failed; will retry. error=%s", exc)
        if shutdown_event.wait(min(interval, 3600)):
            break


def _start_background_mikan_watch(
    config,
    logger,
    shutdown_event: threading.Event,
    *,
    require_parallel_flag: bool = True,
):
    if require_parallel_flag and not getattr(config, "auto_mikan_parallel_with_ai", False):
        return None
    if not getattr(config, "mikan_enabled", False):
        logger.info("Background Mikan/qB worker disabled because mikan_enabled=false.")
        return None

    if getattr(config, "mikan_extract_completed", False):
        from mikan_worker import requeue_interrupted_mikan_extract_jobs

        recovered = requeue_interrupted_mikan_extract_jobs(config)
        if recovered:
            logger.warning(
                "Recovered interrupted Mikan subtitle extraction job(s) during worker startup. count=%s",
                recovered,
            )

    enqueue_thread = threading.Thread(
        target=_background_mikan_enqueue_loop,
        args=(config, logger, shutdown_event),
        daemon=True,
        name="mikan-qbit-enqueue-watch",
    )
    enqueue_thread.start()
    threads = [enqueue_thread]
    if getattr(config, "mikan_extract_completed", False):
        completed_thread = threading.Thread(
            target=_background_mikan_completed_loop,
            args=(config, logger, shutdown_event),
            daemon=True,
            name="mikan-qbit-completed-watch",
        )
        completed_thread.start()
        threads.append(completed_thread)
    logger.info(
        "Background Mikan/qB worker started. enqueue_interval=%s completed_poll_interval=%s",
        config.mikan_watch_interval_seconds,
        getattr(config, "mikan_completed_poll_interval_seconds", config.mikan_watch_interval_seconds),
    )
    return threads


def _background_mikan_enqueue_loop(config, logger, shutdown_event: threading.Event) -> None:
    from mikan_worker import MikanWorker

    worker = MikanWorker(config, logger)
    next_full_run_at = 0.0
    while not shutdown_event.is_set():
        if _deployment_hold_active(config):
            if shutdown_event.wait(1.0):
                break
            continue
        try:
            now = time.monotonic()
            if now >= next_full_run_at:
                worker.run_once(process_completed=False)
                next_full_run_at = time.monotonic() + max(1, int(config.mikan_watch_interval_seconds))
            else:
                worker.consume_deferred_requests()
        except Exception as exc:
            logger.exception("Unhandled background Mikan enqueue error: %s", exc)
        if shutdown_event.wait(1):
            break


def _background_mikan_completed_loop(config, logger, shutdown_event: threading.Event) -> None:
    from mikan_worker import MikanWorker
    from io_pressure import read_io_pressure
    from resource_scheduler import decide_extraction_resources, set_extraction_pressure_pause

    idle_poll_interval = max(1, int(getattr(config, "mikan_completed_poll_interval_seconds", 30) or 30))
    active_poll_interval = max(1, int(getattr(config, "mikan_active_poll_interval_seconds", 5) or 5))
    dispatch_interval = 1
    max_extract_workers = max(1, int(getattr(config, "mikan_extract_workers", 1) or 1))
    max_extract_workers_during_ai = min(
        max_extract_workers,
        max(1, int(getattr(config, "mikan_extract_workers_during_ai", 1) or 1)),
    )
    next_state_update_at = 0.0
    worker = MikanWorker(config, logger)
    extract_threads: set[threading.Thread] = set()
    poll_results: queue.Queue[object | None] = queue.Queue(maxsize=1)
    poll_thread: threading.Thread | None = None
    next_poll_at = 0.0
    last_effective_workers: int | None = None

    def run_poll() -> None:
        try:
            result = worker.poll_download_progress(
                state_required=False,
                cached_mappings_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - keep extraction scheduling alive when reconciliation stalls/fails.
            logger.exception("Unhandled background Mikan completed-download poll error: %s", exc)
            result = None
        poll_results.put(result)

    while not shutdown_event.is_set():
        try:
            if _deployment_hold_active(config):
                set_extraction_pressure_pause(False)
                if shutdown_event.wait(1.0):
                    break
                continue
            extract_threads = {thread for thread in extract_threads if thread.is_alive()}
            now = time.monotonic()
            ai_stage = _ai_processing_stage(config)
            ai_active = _ai_processing_active(config)
            if ai_active and not ai_stage:
                # Preserve compatibility for mocked/legacy callers that can
                # only report a disk-active boolean.
                ai_stage = "transcription"
            io_pressure = read_io_pressure()
            decision = decide_extraction_resources(
                config,
                ai_stage=ai_stage,
                io_pressure=io_pressure,
            )
            effective_max_workers = decision.extract_workers
            set_extraction_pressure_pause(decision.pause_existing_extracts)
            if effective_max_workers != last_effective_workers:
                logger.info(
                    "Mikan extraction resource limit updated. max_workers=%s ai_stage=%s ai_disk_active=%s pause_existing=%s reason=%s io_some_avg10=%s io_full_avg10=%s configured_idle=%s configured_during_ai=%s",
                    effective_max_workers,
                    decision.ai_stage or "idle",
                    int(decision.ai_disk_active),
                    int(decision.pause_existing_extracts),
                    decision.reason,
                    decision.io_some_avg10,
                    decision.io_full_avg10,
                    max_extract_workers,
                    max_extract_workers_during_ai,
                )
                last_effective_workers = effective_max_workers

            # Existing extract jobs are durable and already contain everything
            # needed by an extraction slot. Dispatch them before qBittorrent and
            # pending-state reconciliation so one slow/ambiguous title match can
            # never starve the ready queue.
            claimable, running_jobs = worker.extract_dispatch_counts()
            extract_threads, _started = _dispatch_background_mikan_extract_jobs(
                config,
                logger,
                shutdown_event,
                extract_threads,
                claimable=claimable,
                running_jobs=running_jobs,
                max_workers=effective_max_workers,
            )

            # Reconciliation can include qBittorrent network I/O and title
            # matching. Keep it in one daemon thread so the one-second dispatcher
            # remains responsive even if that poll takes minutes.
            poll_ready = False
            poll = None
            if poll_thread is not None and not poll_thread.is_alive():
                poll_thread.join(timeout=0)
                poll_thread = None
            if poll_thread is None:
                try:
                    poll = poll_results.get_nowait()
                    poll_ready = True
                except queue.Empty:
                    pass
            if poll_thread is None and not poll_ready and now >= next_poll_at:
                poll_thread = threading.Thread(
                    target=run_poll,
                    daemon=True,
                    name="mikan-qbit-completed-poll",
                )
                poll_thread.start()
                next_poll_at = now + idle_poll_interval
                # Fast mocked/local polls keep their historical same-cycle
                # behavior without allowing a real slow poll to block dispatch.
                poll_thread.join(timeout=0.02)
                if not poll_thread.is_alive():
                    poll_thread = None
                    try:
                        poll = poll_results.get_nowait()
                        poll_ready = True
                    except queue.Empty:
                        pass

            if poll_ready and poll is not None:
                if shutdown_event.is_set() or sys.is_finalizing():
                    break
                next_poll_at = time.monotonic() + (
                    active_poll_interval
                    if int(getattr(poll, "active_download_count", 0) or 0) > 0
                    else idle_poll_interval
                )
                if poll.completed_pending_count > 0:
                    logger.info(
                        "Completed Mikan/qB download detected and queued for immediate extraction. torrents=%s",
                        poll.completed_pending_count,
                    )
                if now >= next_state_update_at:
                    worker.consume_completed_state_update_request()
                    next_state_update_at = time.monotonic() + idle_poll_interval

                claimable = max(
                    0,
                    int(getattr(poll, "claimable_extract_count", poll.completed_pending_count) or 0),
                )
                running_jobs = max(0, int(getattr(poll, "running_extract_count", 0) or 0))
                extract_threads, dispatch_count = _dispatch_background_mikan_extract_jobs(
                    config,
                    logger,
                    shutdown_event,
                    extract_threads,
                    claimable=claimable,
                    running_jobs=running_jobs,
                    max_workers=effective_max_workers,
                )
                if not dispatch_count and poll.synced_progress_count and poll.completed_pending_count <= 0:
                    logger.info("Mikan qBittorrent progress synced. entries=%s", poll.synced_progress_count)
        except Exception as exc:
            logger.exception("Unhandled background Mikan completed-download error: %s", exc)
            if shutdown_event.wait(min(idle_poll_interval, 30)):
                break
            continue
        if shutdown_event.wait(dispatch_interval):
            break
    set_extraction_pressure_pause(False)

    # Give short jobs a chance to finish cleanly without making Docker shutdown
    # wait for a long ffmpeg/mkvextract operation indefinitely.
    join_deadline = time.monotonic() + 2.0
    for extract_thread in list(extract_threads):
        remaining = join_deadline - time.monotonic()
        if remaining <= 0:
            break
        extract_thread.join(timeout=remaining)


def _dispatch_background_mikan_extract_jobs(
    config,
    logger,
    shutdown_event: threading.Event,
    extract_threads: set[threading.Thread],
    *,
    claimable: int,
    running_jobs: int,
    max_workers: int,
) -> tuple[set[threading.Thread], int]:
    extract_threads = {thread for thread in extract_threads if thread.is_alive()}
    claimable = max(0, int(claimable or 0))
    running_jobs = max(0, int(running_jobs or 0))
    occupied_slots = max(len(extract_threads), running_jobs)
    available_slots = max(0, int(max_workers) - occupied_slots)
    dispatch_count = min(claimable, available_slots)
    started = 0
    for _index in range(dispatch_count):
        if shutdown_event.is_set() or sys.is_finalizing():
            break
        extract_thread = threading.Thread(
            target=_run_background_mikan_extract_job,
            args=(config, logger),
            daemon=True,
            name=f"mikan-subtitle-extract-{len(extract_threads) + 1}",
        )
        extract_thread.start()
        extract_threads.add(extract_thread)
        started += 1
    if started:
        logger.info(
            "Dispatched Mikan subtitle extraction worker(s). started=%s active=%s max_workers=%s queued=%s",
            started,
            len(extract_threads),
            max_workers,
            claimable,
        )
    return extract_threads, started


def _run_background_mikan_extract_job(config, logger) -> None:
    if sys.is_finalizing():
        return
    from mikan_worker import MikanWorker

    try:
        MikanWorker(config, logger).process_queued_extract_jobs(limit=1)
    except Exception as exc:  # noqa: BLE001 - isolate one extraction slot from the scheduler.
        logger.exception("Unhandled Mikan subtitle extraction worker error: %s", exc)


def _ai_processing_stage(config) -> str:
    """Return the current AI stage using a short-lived read-only query.

    The resource scheduler uses the stage to distinguish disk-heavy audio/ASR
    work from GPU/network-bound translation.
    """

    try:
        from scan_state import scan_state_path

        path = scan_state_path(config)
        if not path.exists():
            return ""
        conn = sqlite3.connect(str(path), timeout=0.05)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=50")
            row = conn.execute(
                """
                SELECT COALESCE(job.stage, 'worker')
                FROM ai_candidate_queue AS queue
                LEFT JOIN ai_job_state AS job ON job.path = queue.path
                WHERE queue.status = 'running'
                LIMIT 1
                """
            ).fetchone()
            return str(row[0] or "worker") if row is not None else ""
        finally:
            conn.close()
    except (AttributeError, OSError, sqlite3.Error, TypeError, ValueError):
        return ""


def _ai_processing_active(config) -> bool:
    """Return whether AI currently owns a disk-heavy media read slot."""

    from resource_scheduler import DISK_HEAVY_AI_STAGES

    return _ai_processing_stage(config).casefold() in DISK_HEAVY_AI_STAGES


def _mikan_redownload_pending_or_active(config) -> bool:
    if not getattr(config, "mikan_enabled", False):
        return False
    from mikan_worker import mikan_redownload_in_progress

    return mikan_redownload_in_progress(config)


def _mikan_redownload_blocks_ai(config) -> bool:
    if getattr(config, "auto_mikan_parallel_with_ai", False):
        return False
    return _mikan_redownload_pending_or_active(config)


def _ai_control_path(config) -> Path:
    work_path = getattr(config, "work_path", None)
    return Path(work_path) / "ai_control.json" if work_path else Path("/__ai_control_disabled__")


def _deployment_hold_path(config) -> Path:
    work_path = getattr(config, "work_path", None)
    return Path(work_path) / "deployment_hold.json" if work_path else Path("/__deployment_hold_disabled__")


def _deployment_hold_active(config) -> bool:
    path = _deployment_hold_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, TypeError, ValueError):
        # A partially written or unreadable hold must fail closed.  The deploy
        # script writes atomically, but storage faults must never start work.
        return path.exists()
    return bool(payload.get("active", True)) if isinstance(payload, dict) else True


def _ai_queue_paused(config) -> bool:
    path = _ai_control_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(payload.get("paused")) if isinstance(payload, dict) else False


def _open_ai_queue_state(config):
    if not getattr(config, "scanner_cache_enabled", True) or not getattr(config, "scanner_queue_enabled", True):
        return None
    from scan_state import ScanStateStore

    return ScanStateStore.from_config(config)


def _requeue_previous_worker_running(config, logger) -> int:
    acceptance_lane = load_acceptance_queue_lane(config)
    state = _open_ai_queue_state(config)
    if state is None:
        return 0
    try:
        def resolve_delivery_evidence(video, attempt, obligation):
            return _verified_ai_delivery_evidence(
                Path(video),
                config,
                obligation_id=str(obligation["obligation_id"]),
                expected_policy_revision=str(obligation["policy_revision"]),
                attempt_started_at=float(attempt["started_at"]),
            )

        if acceptance_lane is None:
            count = state.requeue_running_from_previous_worker(
                delivery_evidence_resolver=resolve_delivery_evidence,
            )
        else:
            count = state.requeue_acceptance_running_targets(
                acceptance_lane.targets,
                message="Worker restarted before this acceptance job finished",
            )
        state.commit()
        if count:
            logger.warning(
                "Requeued running AI job(s) left by previous worker process: count=%s acceptance_run_id=%s",
                count,
                acceptance_lane.run_id if acceptance_lane is not None else "-",
            )
        return count
    finally:
        state.close()


def _mark_queue_running(state, video, config=None) -> str:
    if state is None:
        return ""
    acceptance_target = None
    acceptance_run_id = ""
    if config is not None:
        acceptance_lane = load_acceptance_queue_lane(config)
        if acceptance_lane is not None:
            acceptance_target = acceptance_lane.target_for_path(video)
            if acceptance_target is None:
                raise AcceptanceQueueLaneError(
                    f"refusing non-allowlisted acceptance queue claim: {video}"
                )
            verify_acceptance_queue_target_source(acceptance_target, config)
            acceptance_run_id = acceptance_lane.run_id
    claimed: dict[str, str] = {"attempt_id": ""}

    def claim() -> None:
        state.mark_ai_queue_running(video, acceptance_target=acceptance_target)
        if config is None:
            return
        from output_manifest import delivery_identity

        identity = delivery_identity(video, config)
        media = dict(identity["media"])
        obligation = state.ensure_ai_delivery_obligation(
            Path(video),
            media_size=int(media["media_size"]),
            media_mtime_ns=int(media["media_mtime_ns"]),
            policy_revision=str(identity["policy_revision"]),
            eligible_at=state.ai_delivery_admission_bound(),
            source="queue_claim",
            obligation_id=str(identity["obligation_id"]),
            acceptance_run_id=acceptance_run_id,
        )
        attempt = state.begin_ai_delivery_attempt(
            str(obligation["obligation_id"]),
            acceptance_run_id=acceptance_run_id,
        )
        claimed["attempt_id"] = str(attempt["attempt_id"])

    _commit_ai_queue_state_write(state, claim)
    return claimed["attempt_id"]


def _verified_ai_delivery_evidence(
    video: Path,
    config,
    *,
    obligation_id: str,
    expected_policy_revision: str,
    attempt_started_at: float,
) -> dict[str, object] | None:
    """Return strict manifest-v2 evidence attributable to this exact claim."""

    from output_manifest import (
        manifest_publication_semantics,
        output_manifest_path,
        publication_is_traditional_chinese_delivery,
        validate_output_manifest,
    )
    from safe_files import sha256_file

    manifest = output_manifest_path(video, config)
    try:
        valid = validate_output_manifest(
            video,
            config,
            require_delivery_evidence=True,
            expected_obligation_id=obligation_id,
            expected_policy_revision=expected_policy_revision,
        )
        if not valid:
            return None
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        delivery = payload.get("delivery") if isinstance(payload, dict) else None
        publication = (
            manifest_publication_semantics(payload) if isinstance(payload, dict) else None
        )
        if publication is None:
            return None
        if not publication_is_traditional_chinese_delivery(publication):
            # A source-language transcript can remain a useful artifact, but it
            # is not the Traditional-Chinese delivery promised by this queue.
            return None
        completed_delivery_receipt = None
        completed_delivery_committed_at = 0.0
        if bool(getattr(config, "completed_delivery_enabled", False)):
            from completed_delivery import (
                completed_delivery_receipt_path,
                validate_completed_delivery,
            )

            completed_delivery_receipt = completed_delivery_receipt_path(video, config)
            completed_receipt_sha256 = sha256_file(completed_delivery_receipt)
            completed_receipt_text = completed_delivery_receipt.read_text(encoding="utf-8")
            if not validate_completed_delivery(video, config, verify_streams=False):
                return None
            if sha256_file(completed_delivery_receipt) != completed_receipt_sha256:
                return None
            completed_payload = json.loads(completed_receipt_text)
            completed_delivery_committed_at = float(completed_payload.get("committed_at") or 0)
        manifest_verified_at = (
            float(delivery.get("verified_at") or 0) if isinstance(delivery, dict) else 0.0
        )
        verified_at = (
            completed_delivery_committed_at
            if completed_delivery_receipt is not None
            else manifest_verified_at
        )
        if verified_at <= 0 or verified_at < float(attempt_started_at or 0):
            # A stale manifest from before this queue claim is not evidence that
            # the current end-to-end obligation succeeded.
            return None
        return {
            "manifest_path": str(manifest),
            "manifest_sha256": sha256_file(manifest),
            "verified_at": verified_at,
            "verification": {
                "manifest_schema_version": int(payload.get("schema_version") or 0),
                "delivery_contract": str(delivery.get("contract") or ""),
                "required_outputs_complete": True,
                "hashes_verified": True,
                "quality_gates_passed": True,
                "publication_marker_absent": True,
                "media_identity_matched": True,
                "policy_revision_matched": True,
                "expected_policy_revision": str(expected_policy_revision),
                "manifest_policy_revision": str(delivery.get("policy_revision") or ""),
                "publication_semantics_verified": True,
                "publication_contract": str(publication["contract"]),
                "publication_kind": str(publication["kind"]),
                "output_languages": list(publication["output_languages"]),
                "attempt_started_at": float(attempt_started_at or 0),
                "completed_delivery_verified": completed_delivery_receipt is not None,
                "completed_delivery_receipt": (
                    str(completed_delivery_receipt) if completed_delivery_receipt is not None else ""
                ),
                "subtitle_manifest_verified_at": manifest_verified_at,
                "completed_delivery_committed_at": completed_delivery_committed_at,
                "completed_delivery_receipt_sha256": (
                    completed_receipt_sha256 if completed_delivery_receipt is not None else ""
                ),
            },
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _mark_queue_result(
    state,
    video,
    ok: bool,
    config,
    *,
    delivery_attempt_id: str = "",
) -> None:
    if state is None:
        return
    metric_result = {"ok": bool(ok)}

    def write_result() -> None:
        delivery_attempt = (
            state.get_ai_delivery_attempt(delivery_attempt_id)
            if delivery_attempt_id
            else None
        )
        delivery_obligation = (
            state.get_ai_delivery_obligation(str(delivery_attempt["obligation_id"]))
            if delivery_attempt is not None
            else None
        )
        if ok:
            if delivery_attempt is not None and delivery_obligation is not None:
                evidence = _verified_ai_delivery_evidence(
                    Path(video),
                    config,
                    obligation_id=str(delivery_obligation["obligation_id"]),
                    expected_policy_revision=str(delivery_obligation["policy_revision"]),
                    attempt_started_at=float(delivery_attempt["started_at"]),
                )
                if evidence is not None:
                    state.finish_ai_delivery_attempt(
                        delivery_attempt_id,
                        status="succeeded",
                        stage="delivery_verification",
                    )
                    state.mark_ai_delivery_verified(
                        str(delivery_obligation["obligation_id"]),
                        manifest_path=str(evidence["manifest_path"]),
                        manifest_sha256=str(evidence["manifest_sha256"]),
                        verification=dict(evidence["verification"]),
                        evidence_verified=True,
                        verified_at=float(evidence["verified_at"]),
                    )
                    state.mark_ai_queue_done(video)
                    metric_result["ok"] = True
                    return

                failure_message = (
                    "Worker returned success without a current strictly verified "
                    "AI delivery manifest"
                )
                review_required = state.mark_ai_queue_failed(
                    video,
                    failure_message,
                    retry_after_seconds=int(
                        getattr(config, "auto_ai_failure_cooldown_seconds", 0) or 0
                    ),
                    max_attempts=max(
                        0,
                        int(getattr(config, "auto_ai_max_attempts", 3) or 0),
                    ),
                    error_code="delivery_evidence_missing",
                    retry_strategy="bounded_retry",
                )
                state.finish_ai_delivery_attempt(
                    delivery_attempt_id,
                    status="review_required" if review_required else "retryable_failure",
                    stage="delivery_verification",
                    error_code="delivery_evidence_missing",
                    detail=failure_message,
                )
                metric_result["ok"] = False
                return

            # Compatibility for direct/unit callers that predate the durable
            # delivery ledger. Production queue claims always provide an
            # attempt id and therefore cannot enter this legacy branch.
            if state.is_force_ai_queue_candidate(video):
                from subtitle_paths import has_ai_finished_subtitle

                if not has_ai_finished_subtitle(video, config):
                    state.force_ai_queue_candidate(video)
                    return
            state.mark_ai_queue_done(video)
        else:
            retry_seconds = int(getattr(config, "auto_ai_failure_cooldown_seconds", 0) or 0)
            failure = state.ai_job_failure(video)
            if failure is not None and failure[0] == "transcription_review":
                state.mark_ai_queue_review_required(
                    video,
                    failure[1],
                    source="asr_review",
                    error_code="deterministic_asr_quality",
                )
                if delivery_attempt is not None:
                    state.finish_ai_delivery_attempt(
                        delivery_attempt_id,
                        status="review_required",
                        stage="transcription_review",
                        error_code="deterministic_asr_quality",
                        detail=str(failure[1]),
                    )
            else:
                from control_state import open_ai_quality_review_for_target

                failure_stage = str(failure[0] if failure is not None else "worker")
                failure_message = str(failure[1] if failure is not None else "worker returned false")
                error_code, retry_strategy = _ai_failure_policy(
                    failure_stage,
                    failure_message,
                )
                open_review = open_ai_quality_review_for_target(config, str(video.resolve()))
                if open_review is not None and error_code in _AI_SWEEP_ALLOWED_FAILURE_CODES:
                    # A review remains open, but infrastructure failures such
                    # as OOM/timeouts are not evidence that the subtitle itself
                    # needs an operator. Preserve the same bounded attempt
                    # budget and let the ordinary lower-memory/cooldown path
                    # recover before returning the item to review.
                    review_required = state.mark_ai_queue_failed(
                        video,
                        failure_message,
                        retry_after_seconds=retry_seconds,
                        max_attempts=max(
                            0,
                            int(getattr(config, "auto_ai_max_attempts", 3) or 0),
                        ),
                        error_code=error_code,
                        retry_strategy=retry_strategy,
                    )
                    if delivery_attempt is not None:
                        state.finish_ai_delivery_attempt(
                            delivery_attempt_id,
                            status=(
                                "review_required"
                                if review_required
                                else "retryable_failure"
                            ),
                            stage=failure_stage,
                            error_code=error_code,
                            detail=failure_message,
                        )
                    return
                if open_review is not None:
                    state.mark_ai_queue_review_required(
                        video,
                        str(failure[1] if failure is not None else "Open AI quality review requires attention"),
                        source="quality_review",
                        error_code=(
                            "asr_quality_review"
                            if str(open_review.get("kind") or "") == "asr_quality"
                            else "subtitle_quality_review"
                        ),
                    )
                    if delivery_attempt is not None:
                        review_error_code = (
                            "asr_quality_review"
                            if str(open_review.get("kind") or "") == "asr_quality"
                            else "subtitle_quality_review"
                        )
                        state.finish_ai_delivery_attempt(
                            delivery_attempt_id,
                            status="review_required",
                            stage="quality_review",
                            error_code=review_error_code,
                            detail=str(
                                failure[1]
                                if failure is not None
                                else "Open AI quality review requires attention"
                            ),
                        )
                    return
                review_required = state.mark_ai_queue_failed(
                    video,
                    failure_message,
                    retry_after_seconds=retry_seconds,
                    max_attempts=max(0, int(getattr(config, "auto_ai_max_attempts", 3) or 0)),
                    error_code=error_code,
                    retry_strategy=retry_strategy,
                )
                if delivery_attempt is not None:
                    state.finish_ai_delivery_attempt(
                        delivery_attempt_id,
                        status="review_required" if review_required else "retryable_failure",
                        stage=failure_stage,
                        error_code=error_code,
                        detail=failure_message,
                    )

    _commit_ai_queue_state_write(state, write_result)
    try:
        from control_state import increment_daily_metric

        increment_daily_metric(config, "ai.completed" if metric_result["ok"] else "ai.failed")
    except Exception:  # noqa: BLE001 - metrics must never affect queue correctness.
        # Metrics are observational and must never turn a completed subtitle
        # transition into a failed queue operation.
        pass


def _ai_failure_policy(stage: str, message: str) -> tuple[str, str]:
    normalized_stage = str(stage or "").strip().casefold()
    normalized_message = str(message or "").strip().casefold()
    if normalized_stage == "resource_runtime" and any(
        marker in normalized_message for marker in ("sigkill", "returncode=-9", "returncode=137", "oom")
    ):
        return "transient_resource_killed", "lower_memory_same_pipeline"
    if any(marker in normalized_message for marker in ("out of memory", "cuda failed with error out of memory")):
        return "transient_oom", "lower_memory_same_pipeline"
    if any(marker in normalized_message for marker in ("timed out", "timeout")):
        return "transient_timeout", "same_pipeline"
    if any(
        marker in normalized_message
        for marker in ("connection reset", "connection refused", "network is unreachable", "temporary failure")
    ):
        return "transient_connection", "same_pipeline"
    if normalized_stage == "quality_check":
        return "subtitle_quality_unknown", "manual_review"
    if normalized_stage == "transcription":
        return "asr_unknown", "bounded_retry"
    if normalized_stage == "translation":
        return "translation_unknown", "bounded_retry"
    return f"{normalized_stage or 'worker'}_unknown", "bounded_retry"


def _commit_ai_queue_state_write(state, operation, *, attempts: int = 5) -> None:
    """Commit one queue transition and recover cleanly from transient writers."""

    attempts = max(1, int(attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            operation()
            state.commit()
            return
        except sqlite3.OperationalError as exc:
            try:
                state.rollback()
            except sqlite3.Error:
                pass
            from scan_state import is_scan_state_transient_error

            if not is_scan_state_transient_error(exc) or attempt >= attempts:
                raise
            if attempt == 1:
                delay = 0.1
            else:
                # SQLite already waited for its busy_timeout.  Back off with
                # jitter before reopening the write race against scanners and
                # filesystem events; do not spin or restart the Worker.
                import random

                delay = min(2.0, 0.1 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.1)
            time.sleep(delay)
        except Exception:
            try:
                state.rollback()
            except sqlite3.Error:
                pass
            raise


def _close_ai_queue_state(state) -> None:
    if state is not None:
        try:
            state.commit()
        except Exception:
            try:
                state.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            state.close()


def _install_shutdown_handler(logger) -> threading.Event:
    shutdown_event = threading.Event()

    def _request_shutdown(signum, _frame) -> None:
        if shutdown_event.is_set():
            logger.warning("Shutdown signal received again; stop already requested. signal=%s", signum)
            return
        shutdown_event.set()
        logger.warning("Shutdown signal received; no new work will start. Docker stop timeout controls active work. signal=%s", signum)

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)
    return shutdown_event


def _shutdown_requested(shutdown_event: threading.Event | None) -> bool:
    return bool(shutdown_event and shutdown_event.is_set())


def _wait_for_next_cycle_or_ai_resume(
    shutdown_event: threading.Event,
    interval_seconds: float,
    config,
) -> bool:
    now_monotonic = time.monotonic()
    deadline = now_monotonic + max(0.0, float(interval_seconds))
    next_retry_at = ai_scheduler_next_retry_at(config)
    retry_delay = next_retry_at - time.time()
    if retry_delay > 0:
        deadline = min(
            deadline,
            now_monotonic + retry_delay,
        )
    pause_seen = _ai_queue_paused(config)
    deployment_hold_seen = _deployment_hold_active(config)
    while True:
        if AI_SCHEDULER_WAKE_EVENT.is_set():
            AI_SCHEDULER_WAKE_EVENT.clear()
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if shutdown_event.wait(min(2.0, remaining)):
            return True
        paused = _ai_queue_paused(config)
        if pause_seen and not paused:
            return False
        pause_seen = pause_seen or paused
        deployment_hold_active = _deployment_hold_active(config)
        if deployment_hold_seen and not deployment_hold_active:
            return False
        deployment_hold_seen = deployment_hold_seen or deployment_hold_active


if __name__ == "__main__":
    raise SystemExit(main())
