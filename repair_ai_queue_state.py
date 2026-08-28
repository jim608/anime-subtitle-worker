from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Any

from config import load_config
from ai_failure_markers import recent_ai_failure
from scan_state import scan_state_path
from subtitle_extract import (
    SIDECAR_SUBTITLE_EXTENSIONS,
    SubtitleExtractError,
    classify_sidecar_subtitle_language,
    normalize_sidecar_subtitles,
)
from subtitle_paths import ai_finished_subtitle_mtime, has_ai_finished_subtitle, has_finished_subtitle
from video_policy import is_standalone_theme_video


QUEUE_STATES_SAFE_TO_REPAIR = {"queued", "failed", "failed_retry"}
SKIPPED_STAGES = {"language_skip", "language_uncertain"}
SQLITE_BUSY_TIMEOUT_SECONDS = 180
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
SQLITE_LOCK_RETRY_INITIAL_SECONDS = 0.5
SQLITE_LOCK_RETRY_MAX_SECONDS = 5.0


@dataclass
class RepairSummary:
    scanned: int = 0
    missing_video: int = 0
    repaired_done: int = 0
    repaired_skipped: int = 0
    removed_missing_video: int = 0
    removed_failure_cooldown: int = 0
    removed_standalone_theme: int = 0
    removed_existing_subtitle: int = 0
    normalized_existing_subtitle: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class RepairAction:
    path: Path
    queue_status: str
    new_status: str
    source: str
    message: str
    completed_at: float | None = None


def repair_queue_state(
    config: Any,
    *,
    apply: bool = False,
    show_actions: bool = False,
    busy_timeout_seconds: int = SQLITE_BUSY_TIMEOUT_SECONDS,
    progress_interval_seconds: float = 0.0,
) -> RepairSummary:
    db_path = scan_state_path(config)
    summary = RepairSummary()
    if not db_path.exists():
        raise FileNotFoundError(f"scanner state database not found: {db_path}")

    conn = _connect_scan_state(db_path, busy_timeout_seconds=busy_timeout_seconds)
    try:
        _require_table(conn, "ai_candidate_queue")
        _require_table(conn, "ai_job_state")
        rows = conn.execute(
            """
            SELECT
                q.path,
                q.status,
                q.mtime_ns,
                q.updated_at,
                COALESCE(q.force_ai, 0),
                j.stage,
                j.status,
                j.message,
                j.finished_at,
                j.updated_at
            FROM ai_candidate_queue q
            LEFT JOIN ai_job_state j ON j.path = q.path
            WHERE q.status IN ('queued', 'failed', 'failed_retry')
            ORDER BY q.updated_at DESC
            """
        ).fetchall()

        total_rows = len(rows)
        progress_interval = max(0.0, float(progress_interval_seconds or 0.0))
        next_progress_at = time.monotonic() + progress_interval
        for row in rows:
            summary.scanned += 1
            action = _planned_repair(row, config)
            if action is None:
                summary.unchanged += 1
            else:
                if show_actions:
                    print(
                        f"{action.source}: {action.path} "
                        f"{action.queue_status} -> {action.new_status} ({action.message})",
                        flush=True,
                    )
                if apply:
                    _apply_action_with_retry(conn, action, config=config, busy_timeout_seconds=busy_timeout_seconds)
                if action.new_status == "done":
                    summary.repaired_done += 1
                elif action.new_status == "skipped":
                    summary.repaired_skipped += 1
                elif action.new_status == "removed":
                    if action.source == "repair_missing_video":
                        summary.removed_missing_video += 1
                    elif action.source == "repair_failure_cooldown":
                        summary.removed_failure_cooldown += 1
                    elif action.source == "repair_standalone_theme":
                        summary.removed_standalone_theme += 1
                    elif action.source == "repair_existing_subtitle":
                        summary.removed_existing_subtitle += 1
                    elif action.source == "repair_local_chinese_sidecar":
                        summary.removed_existing_subtitle += 1
                        summary.normalized_existing_subtitle += 1

            if progress_interval > 0 and time.monotonic() >= next_progress_at:
                repaired = (
                    summary.repaired_done
                    + summary.repaired_skipped
                    + summary.removed_missing_video
                    + summary.removed_failure_cooldown
                    + summary.removed_standalone_theme
                    + summary.removed_existing_subtitle
                )
                print(
                    "repair_ai_queue_state progress "
                    f"scanned={summary.scanned}/{total_rows} repaired={repaired} "
                    f"unchanged={summary.unchanged}",
                    flush=True,
                )
                next_progress_at = time.monotonic() + progress_interval
    finally:
        conn.close()
    return summary


def _connect_scan_state(db_path: Path, *, busy_timeout_seconds: int) -> sqlite3.Connection:
    timeout_seconds = max(1, int(busy_timeout_seconds or SQLITE_BUSY_TIMEOUT_SECONDS))
    conn = sqlite3.connect(db_path, timeout=timeout_seconds)
    try:
        conn.execute(f"PRAGMA busy_timeout={timeout_seconds * 1000}")
        conn.execute("PRAGMA temp_store=MEMORY")
    except Exception:
        conn.close()
        raise
    return conn


def _apply_action_with_retry(
    conn: sqlite3.Connection,
    action: RepairAction,
    *,
    config: Any,
    busy_timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + max(1, int(busy_timeout_seconds or SQLITE_BUSY_TIMEOUT_SECONDS))
    delay = SQLITE_LOCK_RETRY_INITIAL_SECONDS
    while True:
        try:
            _apply_action(conn, action, config=config)
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_lock_error(exc):
                raise
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(SQLITE_LOCK_RETRY_MAX_SECONDS, delay * 1.5)


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return "database is locked" in message or "database table is locked" in message or "database is busy" in message


def _planned_repair(row: tuple[Any, ...], config: Any) -> RepairAction | None:
    (
        raw_path,
        queue_status,
        _queue_mtime_ns,
        _queue_updated_at,
        force_ai,
        job_stage,
        job_status,
        job_message,
        job_finished_at,
        job_updated_at,
    ) = row
    path = Path(str(raw_path))
    queue_status = str(queue_status or "")
    job_stage_text = str(job_stage or "")
    job_status_text = str(job_status or "")
    job_message_text = str(job_message or "")

    if queue_status not in QUEUE_STATES_SAFE_TO_REPAIR:
        return None
    if not path.exists():
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="removed",
            source="repair_missing_video",
            message="Video no longer exists",
        )

    if not bool(force_ai) and is_standalone_theme_video(path, config):
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="removed",
            source="repair_standalone_theme",
            message="Standalone OP/ED asset is excluded; in-episode lyric rescue remains enabled",
        )

    has_recent_failure, remaining_seconds = recent_ai_failure(config, path)
    if has_recent_failure:
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="removed",
            source="repair_failure_cooldown",
            message=f"AI failure cooldown active; retry after {remaining_seconds}s",
        )

    if job_status_text == "skipped" and _is_language_skip(job_stage_text, job_message_text):
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="skipped",
            source="repair_language_skip",
            message=job_message_text or "Previous AI job skipped by source-language gate",
            completed_at=_first_positive(job_finished_at, job_updated_at),
        )

    if has_ai_finished_subtitle(path, config):
        completed_at = ai_finished_subtitle_mtime(path, config)
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="done",
            source="repair_finished_ai",
            message="Finished AI subtitle already exists",
            completed_at=completed_at,
        )

    if not bool(getattr(config, "require_ai_subtitles", False)) and has_finished_subtitle(path, config):
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="removed",
            source="repair_existing_subtitle",
            message="Non-AI Chinese subtitle already exists; AI is not required",
        )

    if not bool(getattr(config, "require_ai_subtitles", False)) and _has_local_chinese_sidecar(path):
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="removed",
            source="repair_local_chinese_sidecar",
            message="Local Chinese sidecar exists; normalized and removed from AI queue",
        )

    if job_status_text == "ok" and _first_positive(job_finished_at, None):
        return RepairAction(
            path=path,
            queue_status=queue_status,
            new_status="done",
            source="repair_completed_job",
            message=job_message_text or "Previous AI job completed",
            completed_at=_first_positive(job_finished_at, job_updated_at),
        )

    return None


def _apply_action(conn: sqlite3.Connection, action: RepairAction, *, config: Any) -> None:
    now = time.time()
    mtime_ns = _safe_mtime_ns(action.path)
    finished_at = action.completed_at or now
    if action.new_status == "done":
        conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'done',
                source = ?,
                mtime_ns = ?,
                running_at = NULL,
                last_error = NULL,
                last_error_at = NULL,
                next_retry_at = NULL,
                updated_at = ?
            WHERE path = ?
            """,
            (action.source, mtime_ns, finished_at, str(action.path)),
        )
        conn.execute(
            """
            INSERT INTO ai_job_state(path, stage, status, message, started_at, updated_at, finished_at)
            VALUES (?, 'detected_existing', 'ok', ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                stage = excluded.stage,
                status = excluded.status,
                message = excluded.message,
                updated_at = excluded.updated_at,
                finished_at = excluded.finished_at
            """,
            (str(action.path), action.message, finished_at, finished_at, finished_at),
        )
        return

    if action.new_status == "skipped":
        finished_at = action.completed_at or now
        conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'skipped',
                source = ?,
                mtime_ns = ?,
                running_at = NULL,
                last_error = NULL,
                last_error_at = NULL,
                next_retry_at = NULL,
                updated_at = ?
            WHERE path = ?
            """,
            (action.source, mtime_ns, finished_at, str(action.path)),
        )
        conn.execute(
            """
            INSERT INTO ai_job_state(path, stage, status, message, started_at, updated_at, finished_at)
            VALUES (?, 'language_skip', 'skipped', ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                stage = excluded.stage,
                status = excluded.status,
                message = excluded.message,
                updated_at = excluded.updated_at,
                finished_at = excluded.finished_at
            """,
            (str(action.path), action.message, finished_at, finished_at, finished_at),
        )
        return

    if action.new_status == "removed":
        if action.source == "repair_local_chinese_sidecar":
            try:
                normalize_sidecar_subtitles(action.path, config)
            except SubtitleExtractError:
                pass
        conn.execute("DELETE FROM ai_candidate_queue WHERE path = ?", (str(action.path),))
        return

    raise ValueError(f"unsupported repair status: {action.new_status}")


def _is_language_skip(stage: str, message: str) -> bool:
    lowered_stage = stage.casefold()
    lowered_message = message.casefold()
    return (
        lowered_stage in SKIPPED_STAGES
        or "source language gate" in lowered_message
        or "language_uncertain" in lowered_message
        or "reason=language_uncertain" in lowered_message
    )


def _first_positive(*values: Any) -> float | None:
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _has_local_chinese_sidecar(video: Path) -> bool:
    try:
        subtitles = list(video.parent.glob(f"{video.stem}.*"))
    except OSError:
        return False
    for subtitle in subtitles:
        if not subtitle.is_file() or subtitle.suffix.lower() not in SIDECAR_SUBTITLE_EXTENSIONS:
            continue
        try:
            language = classify_sidecar_subtitle_language(subtitle)
        except OSError:
            continue
        if language in {"zh-tw", "zh-cn"}:
            return True
    return False


def _require_table(conn: sqlite3.Connection, table: str) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"required table is missing: {table}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair AI queue rows that were re-queued after completion or language skip.")
    parser.add_argument("--config", default="/app/config.yaml")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--show-actions", action="store_true", help="print each planned repair")
    parser.add_argument(
        "--busy-timeout-seconds",
        type=int,
        default=SQLITE_BUSY_TIMEOUT_SECONDS,
        help="seconds to wait and retry when scanner_state.sqlite3 is locked",
    )
    parser.add_argument(
        "--progress-interval-seconds",
        "--progress-interval",
        type=float,
        default=5.0,
        help="print scan progress at this interval; use 0 to disable",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    mode = "apply" if args.apply else "dry-run"
    print(
        f"repair_ai_queue_state scan_start mode={mode} db={scan_state_path(config)}",
        flush=True,
    )
    summary = repair_queue_state(
        config,
        apply=bool(args.apply),
        show_actions=bool(args.show_actions),
        busy_timeout_seconds=int(args.busy_timeout_seconds),
        progress_interval_seconds=float(args.progress_interval_seconds),
    )
    print(
        "repair_ai_queue_state "
        f"mode={mode} db={scan_state_path(config)} scanned={summary.scanned} "
        f"repaired_done={summary.repaired_done} repaired_skipped={summary.repaired_skipped} "
        f"removed_missing_video={summary.removed_missing_video} "
        f"removed_failure_cooldown={summary.removed_failure_cooldown} "
        f"removed_standalone_theme={summary.removed_standalone_theme} "
        f"removed_existing_subtitle={summary.removed_existing_subtitle} "
        f"normalized_existing_subtitle={summary.normalized_existing_subtitle} "
        f"missing_video={summary.missing_video} unchanged={summary.unchanged}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
