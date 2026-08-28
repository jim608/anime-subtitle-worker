from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
import time

from asr_quality import asr_artifact_line_indexes
from translation_quality import translation_pollution_reason
from subtitle_quality import managed_quality_report_path
from safe_files import verified_move


@dataclass(frozen=True)
class CleanupTarget:
    container_video_path: str
    local_video_path: Path
    media_outputs: list[Path]
    cache_outputs: list[Path]
    pollution_reasons: tuple[str, ...] = ()
    detected_leading_gap_seconds: float | None = None

    @property
    def all_outputs(self) -> list[Path]:
        return [*self.media_outputs, *self.cache_outputs]


def main() -> int:
    _configure_console_output()
    parser = argparse.ArgumentParser(
        description="Remove only selected contaminated AI subtitle outputs and requeue those videos.",
    )
    parser.add_argument("--work-path", default="X:/anime-subtitle-worker/work")
    parser.add_argument("--anime-root", default="Z:/anime")
    parser.add_argument("--container-anime-root", default="/anime")
    parser.add_argument(
        "--reason",
        default="kotoba",
        choices=["kotoba", "translation-pollution", "asr-prompt-echo", "leading-gap"],
    )
    parser.add_argument(
        "--pollution-kind",
        default="all",
        choices=["all", "prompt-leak", "runaway-repetition"],
        help="translation-pollution subtype to scan; prompt-leak is the safest targeted cleanup",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=5.0,
        help="seconds between translation-pollution scan progress updates; use 0 to disable",
    )
    parser.add_argument(
        "--requeue-batch-size",
        type=int,
        default=50,
        help="AI queue rows committed per short transaction",
    )
    parser.add_argument(
        "--requeue-lock-timeout-seconds",
        type=int,
        default=300,
        help="maximum total time to wait for scanner DB writers while requeueing",
    )
    parser.add_argument(
        "--max-targets",
        type=int,
        default=0,
        help="maximum matched videos to archive in this run; 0 means all",
    )
    parser.add_argument(
        "--leading-gap-seconds",
        type=float,
        default=12.0,
        help="minimum first-dialogue timestamp for the leading-gap audit",
    )
    parser.add_argument("--apply", action="store_true", help="Move matched outputs to a backup folder and requeue AI.")
    args = parser.parse_args()

    work_path = Path(args.work_path)
    anime_root = Path(args.anime_root)
    db_path = work_path / "scanner_state.sqlite3"
    if not db_path.exists():
        raise SystemExit(f"scanner DB does not exist: {db_path}")
    if not anime_root.exists():
        raise SystemExit(f"anime root does not exist: {anime_root}")

    if args.reason == "translation-pollution":
        targets = _select_translation_pollution_targets(
            db_path,
            work_path,
            anime_root,
            args.container_anime_root,
            pollution_kind=args.pollution_kind,
            progress_interval_seconds=max(0.0, float(args.progress_interval or 0.0)),
        )
    elif args.reason == "asr-prompt-echo":
        targets = _select_asr_prompt_echo_targets(
            db_path,
            work_path,
            anime_root,
            args.container_anime_root,
            progress_interval_seconds=max(0.0, float(args.progress_interval or 0.0)),
            max_targets=max(0, int(args.max_targets or 0)),
        )
    elif args.reason == "leading-gap":
        targets = _select_leading_gap_targets(
            db_path,
            work_path,
            anime_root,
            args.container_anime_root,
            minimum_gap_seconds=max(0.1, float(args.leading_gap_seconds or 0.1)),
            progress_interval_seconds=max(0.0, float(args.progress_interval or 0.0)),
            max_targets=max(0, int(args.max_targets or 0)),
        )
    else:
        targets = _select_kotoba_targets(
            db_path,
            work_path,
            anime_root,
            args.container_anime_root,
            include_without_outputs=bool(args.apply),
        )
    if args.reason != "asr-prompt-echo" and args.max_targets > 0:
        targets = targets[: args.max_targets]
    media_count = sum(len(target.media_outputs) for target in targets)
    cache_count = sum(len(target.cache_outputs) for target in targets)
    output_count = media_count + cache_count
    pollution_reason_counts = Counter(
        reason
        for target in targets
        for reason in target.pollution_reasons
    )

    backup_root = work_path / "selective_ai_cleanup" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": args.reason,
        "pollution_kind": args.pollution_kind if args.reason == "translation-pollution" else None,
        "pollution_reason_counts": dict(sorted(pollution_reason_counts.items())),
        "apply": bool(args.apply),
        "db_path": str(db_path),
        "anime_root": str(anime_root),
        "container_anime_root": args.container_anime_root,
        "target_videos": len(targets),
        "media_outputs": media_count,
        "cache_outputs": cache_count,
        "total_outputs": output_count,
        "targets": [
            {
                "container_video_path": target.container_video_path,
                "local_video_path": str(target.local_video_path),
                "pollution_reasons": list(target.pollution_reasons),
                "detected_leading_gap_seconds": target.detected_leading_gap_seconds,
                "media_outputs": [str(path) for path in target.media_outputs],
                "cache_outputs": [str(path) for path in target.cache_outputs],
            }
            for target in targets
        ],
    }

    print(json.dumps({k: manifest[k] for k in manifest if k != "targets"}, ensure_ascii=False, indent=2))
    for target in targets[:20]:
        reason_label = ",".join(target.pollution_reasons)
        leading_label = (
            f" first_dialogue={target.detected_leading_gap_seconds:.1f}s"
            if target.detected_leading_gap_seconds is not None
            else ""
        )
        prefix = f"[{reason_label}{leading_label}] " if reason_label or leading_label else ""
        print(f"- {prefix}{target.container_video_path}")
        for output in target.all_outputs[:6]:
            print(f"  {output}")
    if len(targets) > 20:
        print(f"... {len(targets) - 20} more target video(s)")

    if not args.apply:
        print("dry-run only; pass --apply to move outputs and requeue")
        return 0

    backup_root.mkdir(parents=True, exist_ok=True)
    manifest_path = backup_root / "manifest.json"
    manifest["status"] = "planned"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    moved: list[dict[str, str]] = []
    archive_total = sum(len(target.all_outputs) for target in targets)
    archive_processed = 0
    archive_next_progress = time.monotonic() + max(1.0, float(args.progress_interval or 0.0))
    print(f"archive_start: targets={len(targets)} outputs={archive_total}", flush=True)
    for target in targets:
        for output in target.all_outputs:
            archive_processed += 1
            if not output.exists() or not output.is_file():
                continue
            destination = _backup_destination(backup_root, output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            verified_move(output, destination)
            moved.append({"from": str(output), "to": str(destination)})
            if time.monotonic() >= archive_next_progress:
                print(
                    "archive_progress: "
                    f"processed={archive_processed}/{archive_total} moved={len(moved)}",
                    flush=True,
                )
                archive_next_progress = time.monotonic() + max(1.0, float(args.progress_interval or 0.0))

    print(
        f"archive_complete: processed={archive_processed}/{archive_total} moved={len(moved)}",
        flush=True,
    )

    manifest["moved"] = moved
    manifest["status"] = "moved"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    requeued_videos = _requeue_targets(
        db_path,
        targets,
        batch_size=max(1, int(args.requeue_batch_size or 1)),
        busy_timeout_seconds=max(1, int(args.requeue_lock_timeout_seconds or 1)),
        progress_interval_seconds=max(0.0, float(args.progress_interval or 0.0)),
    )
    manifest["status"] = "requeued"
    manifest["requeued_videos"] = requeued_videos
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"moved_outputs={len(moved)}")
    print(f"requeued_videos={requeued_videos}")
    print(f"manifest={manifest_path}")
    return 0


def _configure_console_output() -> None:
    # Windows cp950 cannot represent every simplified-Chinese filename.  A
    # cleanup audit must never abort merely while printing an otherwise valid
    # path; unsupported characters are escaped while the manifest keeps UTF-8.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass


def _select_kotoba_targets(
    db_path: Path,
    work_path: Path,
    anime_root: Path,
    container_anime_root: str,
    include_without_outputs: bool = False,
) -> list[CleanupTarget]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT path
            FROM ai_stage_events
            WHERE lower(message) LIKE '%kotoba%'
            ORDER BY path COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()

    container_paths = [str(row[0]) for row in rows]
    local_video_paths = [
        _container_to_local_video_path(path, anime_root, container_anime_root)
        for path in container_paths
    ]
    media_index = _media_ai_output_index(local_video_paths)
    cache_index = _cache_ai_output_index(
        work_path / "ai_srt_cache",
        (
            _ai_srt_cache_base_from_container_path(path, work_path / "ai_srt_cache").name
            for path in container_paths
        ),
    )

    targets: list[CleanupTarget] = []
    for container_video_path in container_paths:
        local_video_path = _container_to_local_video_path(container_video_path, anime_root, container_anime_root)
        media_outputs = media_index.get(str(local_video_path).casefold(), [])
        cache_base = _ai_srt_cache_base_from_container_path(container_video_path, work_path / "ai_srt_cache").name
        cache_outputs = _include_managed_quality_reports(
            cache_index.get(cache_base.casefold(), []),
            media_outputs,
            work_path,
        )
        if media_outputs or cache_outputs or include_without_outputs:
            targets.append(
                CleanupTarget(
                    container_video_path=str(container_video_path),
                    local_video_path=local_video_path,
                    media_outputs=media_outputs,
                    cache_outputs=cache_outputs,
                )
            )
    return targets


def _select_translation_pollution_targets(
    db_path: Path,
    work_path: Path,
    anime_root: Path,
    container_anime_root: str,
    *,
    pollution_kind: str = "all",
    progress_interval_seconds: float = 5.0,
) -> list[CleanupTarget]:
    required_reason = {
        "all": None,
        "prompt-leak": "prompt_leak",
        "runaway-repetition": "runaway_repetition",
    }.get(pollution_kind)
    if pollution_kind not in {"all", "prompt-leak", "runaway-repetition"}:
        raise ValueError(f"unsupported pollution kind: {pollution_kind}")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT path
            FROM ai_candidate_queue
            WHERE status IN ('done', 'failed', 'failed_retry')
            ORDER BY path COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()

    translated_suffixes = (
        ".AI简日双语.zh-CN.ass",
        ".AI繁日雙語.zh-TW.ass",
        ".AI简日双语.zh.ass",
        ".AI繁日雙語.zh.ass",
    )
    contaminated: list[tuple[str, Path, tuple[str, ...]]] = []
    scanned_videos = 0
    scanned_ass = 0
    next_progress = time.monotonic() + progress_interval_seconds if progress_interval_seconds > 0 else None
    print(f"translation_pollution_scan start root={anime_root} queue_rows={len(rows)}", flush=True)

    for (raw_container_path,) in rows:
        container_video_path = str(raw_container_path)
        local_video_path = _container_to_local_video_path(
            container_video_path,
            anime_root,
            container_anime_root,
        )
        scanned_videos += 1
        matched_reasons: set[str] = set()
        for suffix in translated_suffixes:
            output = local_video_path.with_name(f"{local_video_path.stem}{suffix}")
            if not output.is_file():
                continue
            scanned_ass += 1
            reason = _ass_translation_pollution_reason(output, required_reason=required_reason)
            if reason is not None:
                matched_reasons.add(reason)
                break
        if matched_reasons:
            contaminated.append(
                (container_video_path, local_video_path, tuple(sorted(matched_reasons)))
            )
        if next_progress is not None and time.monotonic() >= next_progress:
            print(
                "translation_pollution_scan "
                f"videos={scanned_videos}/{len(rows)} scanned_ai_ass={scanned_ass} "
                f"matched_videos={len(contaminated)}",
                flush=True,
            )
            next_progress = time.monotonic() + progress_interval_seconds

    container_paths = [path for path, _video, _reasons in contaminated]
    local_video_paths = [video for _path, video, _reasons in contaminated]
    media_index = _media_ai_output_index(local_video_paths)
    cache_index = _cache_ai_output_index(
        work_path / "ai_srt_cache",
        (
            _ai_srt_cache_base_from_container_path(path, work_path / "ai_srt_cache").name
            for path in container_paths
        ),
    )
    targets: list[CleanupTarget] = []
    for container_video_path, local_video_path, pollution_reasons in contaminated:
        cache_base = _ai_srt_cache_base_from_container_path(
            container_video_path,
            work_path / "ai_srt_cache",
        ).name
        media_outputs = _translated_ai_outputs(
            local_video_path.stem,
            media_index.get(str(local_video_path).casefold(), []),
        )
        cache_outputs = _translated_ai_outputs(
            cache_base,
            cache_index.get(cache_base.casefold(), []),
        )
        cache_outputs = _include_managed_quality_reports(cache_outputs, media_outputs, work_path)
        targets.append(
            CleanupTarget(
                container_video_path=container_video_path,
                local_video_path=local_video_path,
                media_outputs=media_outputs,
                cache_outputs=cache_outputs,
                pollution_reasons=pollution_reasons,
            )
        )
    print(
        "translation_pollution_scan complete "
        f"videos={scanned_videos} scanned_ai_ass={scanned_ass} matched_videos={len(targets)}",
        flush=True,
    )
    return targets


def _ass_translation_pollution_reason(
    path: Path,
    *,
    required_reason: str | None = None,
) -> str | None:
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for raw_line in handle:
                if not raw_line.startswith("Dialogue:"):
                    continue
                payload = raw_line.split(":", 1)[1].lstrip()
                parts = payload.split(",", 9)
                text = parts[9] if len(parts) >= 10 else parts[-1]
                primary = re.sub(r"\{[^{}]*\}", "", text.split(r"\N", 1)[0])
                reason = translation_pollution_reason(primary)
                if reason is not None and (required_reason is None or reason == required_reason):
                    return reason
    except OSError:
        return None
    return None


def _select_asr_prompt_echo_targets(
    db_path: Path,
    work_path: Path,
    anime_root: Path,
    container_anime_root: str,
    *,
    progress_interval_seconds: float = 5.0,
    max_targets: int = 0,
) -> list[CleanupTarget]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT path
            FROM ai_candidate_queue
            WHERE status IN ('done', 'failed', 'failed_retry')
            ORDER BY path COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()

    container_paths = [str(row[0]) for row in rows]
    local_video_paths = [
        _container_to_local_video_path(path, anime_root, container_anime_root)
        for path in container_paths
    ]
    contaminated: list[tuple[str, Path]] = []
    scanned_videos = 0
    scanned_ass = 0
    next_progress = time.monotonic() + progress_interval_seconds if progress_interval_seconds > 0 else None
    print(f"asr_prompt_echo_scan start root={anime_root} queue_rows={len(rows)}", flush=True)

    work_items = list(zip(container_paths, local_video_paths))
    max_workers = min(8, max(1, len(work_items)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="asr-prompt-scan") as executor:
        futures = {
            executor.submit(_scan_video_for_asr_prompt_echo, container_path, local_path): (
                container_path,
                local_path,
            )
            for container_path, local_path in work_items
        }
        for future in as_completed(futures):
            container_video_path, local_video_path, checked_ass, matched = future.result()
            scanned_videos += 1
            scanned_ass += checked_ass
            if matched:
                contaminated.append((container_video_path, local_video_path))
            if next_progress is not None and time.monotonic() >= next_progress:
                print(
                    "asr_prompt_echo_scan "
                    f"videos={scanned_videos}/{len(rows)} scanned_ai_ass={scanned_ass} "
                    f"matched_videos={len(contaminated)}",
                    flush=True,
                )
                next_progress = time.monotonic() + progress_interval_seconds

    # Repair the newest generated subtitles first.  Quality regressions are
    # normally noticed on the latest episodes, and alphabetical batching made
    # --max-targets spend hours on old series before reaching those files.
    contaminated.sort(
        key=lambda item: (
            -_newest_japanese_ai_ass_mtime_ns(item[1]),
            item[0].casefold(),
        )
    )
    matched_total = len(contaminated)
    if max_targets > 0:
        contaminated = contaminated[:max_targets]
    contaminated_paths = [path for path, _video in contaminated]
    contaminated_videos = [video for _path, video in contaminated]
    media_index = _media_ai_output_index(contaminated_videos)
    cache_index = _cache_ai_output_index(
        work_path / "ai_srt_cache",
        (
            _ai_srt_cache_base_from_container_path(path, work_path / "ai_srt_cache").name
            for path in contaminated_paths
        ),
    )
    targets: list[CleanupTarget] = []
    for container_video_path, local_video_path in contaminated:
        cache_base = _ai_srt_cache_base_from_container_path(
            container_video_path,
            work_path / "ai_srt_cache",
        ).name
        media_outputs = media_index.get(str(local_video_path).casefold(), [])
        cache_outputs = _include_managed_quality_reports(
            cache_index.get(cache_base.casefold(), []),
            media_outputs,
            work_path,
        )
        targets.append(
            CleanupTarget(
                container_video_path=container_video_path,
                local_video_path=local_video_path,
                media_outputs=media_outputs,
                cache_outputs=cache_outputs,
                pollution_reasons=("asr_prompt_echo",),
            )
        )
    print(
        "asr_prompt_echo_scan complete "
        f"videos={scanned_videos} scanned_ai_ass={scanned_ass} "
        f"matched_videos={matched_total} selected_videos={len(targets)}",
        flush=True,
    )
    return targets


def _select_leading_gap_targets(
    db_path: Path,
    work_path: Path,
    anime_root: Path,
    container_anime_root: str,
    *,
    minimum_gap_seconds: float = 12.0,
    progress_interval_seconds: float = 5.0,
    max_targets: int = 0,
) -> list[CleanupTarget]:
    """Find published Japanese AI subtitles whose first line starts late.

    Timestamp alone is only an audit signal, not proof of missing speech.  The
    default command is therefore dry-run.  Applying the selected set archives
    every generated output and lets the upgraded Worker verify the actual
    opening audio before publishing replacements.
    """

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT path
            FROM ai_candidate_queue
            WHERE status IN ('done', 'failed', 'failed_retry')
            ORDER BY path COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()

    container_paths = [str(row[0]) for row in rows]
    local_video_paths = [
        _container_to_local_video_path(path, anime_root, container_anime_root)
        for path in container_paths
    ]
    matched: list[tuple[str, Path, float]] = []
    scanned_videos = 0
    scanned_ass = 0
    next_progress = time.monotonic() + progress_interval_seconds if progress_interval_seconds > 0 else None
    print(
        "leading_gap_scan start "
        f"root={anime_root} queue_rows={len(rows)} threshold={minimum_gap_seconds:.1f}s",
        flush=True,
    )

    work_items = list(zip(container_paths, local_video_paths))
    max_workers = min(8, max(1, len(work_items)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="leading-gap-scan") as executor:
        futures = {
            executor.submit(_scan_video_for_leading_gap, container_path, local_path): (
                container_path,
                local_path,
            )
            for container_path, local_path in work_items
        }
        for future in as_completed(futures):
            container_video_path, local_video_path, checked_ass, first_dialogue = future.result()
            scanned_videos += 1
            scanned_ass += checked_ass
            if first_dialogue is not None and first_dialogue >= minimum_gap_seconds:
                matched.append((container_video_path, local_video_path, first_dialogue))
            if next_progress is not None and time.monotonic() >= next_progress:
                print(
                    "leading_gap_scan "
                    f"videos={scanned_videos}/{len(rows)} scanned_ai_ass={scanned_ass} "
                    f"matched_videos={len(matched)}",
                    flush=True,
                )
                next_progress = time.monotonic() + progress_interval_seconds

    matched.sort(
        key=lambda item: (
            -_newest_japanese_ai_ass_mtime_ns(item[1]),
            -item[2],
            item[0].casefold(),
        )
    )
    matched_total = len(matched)
    if max_targets > 0:
        matched = matched[:max_targets]
    selected_paths = [path for path, _video, _gap in matched]
    selected_videos = [video for _path, video, _gap in matched]
    media_index = _media_ai_output_index(selected_videos)
    cache_index = _cache_ai_output_index(
        work_path / "ai_srt_cache",
        (
            _ai_srt_cache_base_from_container_path(path, work_path / "ai_srt_cache").name
            for path in selected_paths
        ),
    )
    targets: list[CleanupTarget] = []
    for container_video_path, local_video_path, first_dialogue in matched:
        cache_base = _ai_srt_cache_base_from_container_path(
            container_video_path,
            work_path / "ai_srt_cache",
        ).name
        media_outputs = media_index.get(str(local_video_path).casefold(), [])
        cache_outputs = _include_managed_quality_reports(
            cache_index.get(cache_base.casefold(), []),
            media_outputs,
            work_path,
        )
        if not media_outputs and not cache_outputs:
            continue
        targets.append(
            CleanupTarget(
                container_video_path=container_video_path,
                local_video_path=local_video_path,
                media_outputs=media_outputs,
                cache_outputs=cache_outputs,
                pollution_reasons=("leading_gap",),
                detected_leading_gap_seconds=round(first_dialogue, 3),
            )
        )
    print(
        "leading_gap_scan complete "
        f"videos={scanned_videos} scanned_ai_ass={scanned_ass} "
        f"matched_videos={matched_total} selected_videos={len(targets)}",
        flush=True,
    )
    return targets


def _scan_video_for_leading_gap(
    container_video_path: str,
    local_video_path: Path,
) -> tuple[str, Path, int, float | None]:
    candidates = _japanese_ai_ass_candidates(local_video_path)
    checked = 0
    for output in candidates:
        if not output.is_file():
            continue
        checked += 1
        first_dialogue = _ass_first_dialogue_seconds(output)
        if first_dialogue is not None:
            return container_video_path, local_video_path, checked, first_dialogue
    return container_video_path, local_video_path, checked, None


def _ass_first_dialogue_seconds(path: Path) -> float | None:
    first: float | None = None
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for raw_line in handle:
                if not raw_line.startswith("Dialogue:"):
                    continue
                payload = raw_line.split(":", 1)[1].lstrip()
                parts = payload.split(",", 9)
                if len(parts) < 3:
                    continue
                seconds = _ass_timestamp_seconds(parts[1].strip())
                if seconds is not None:
                    first = seconds if first is None else min(first, seconds)
    except OSError:
        return None
    return first


def _ass_timestamp_seconds(value: str) -> float | None:
    match = re.fullmatch(
        r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})(?:\.(?P<fraction>\d{1,3}))?",
        str(value or "").strip(),
    )
    if match is None:
        return None
    fraction = match.group("fraction") or "0"
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(fraction) / (10 ** len(fraction))
    )


def _japanese_ai_ass_candidates(video: Path) -> tuple[Path, ...]:
    stems = (
        f"{video.stem}.AI日本語.ja.ass",
        f"{video.stem}.AI日語.ja.ass",
        f"{video.stem}.AI日语.ja.ass",
        f"{video.stem}.AI.ja.ass",
    )
    return tuple(video.with_name(name) for name in dict.fromkeys(stems))


def _newest_japanese_ai_ass_mtime_ns(video: Path) -> int:
    newest = 0
    for candidate in _japanese_ai_ass_candidates(video):
        try:
            newest = max(newest, int(candidate.stat().st_mtime_ns))
        except OSError:
            continue
    return newest


def _scan_video_for_asr_prompt_echo(
    container_video_path: str,
    local_video_path: Path,
) -> tuple[str, Path, int, bool]:
    candidates = _japanese_ai_ass_candidates(local_video_path)
    primary = candidates[0]
    if primary.is_file():
        return container_video_path, local_video_path, 1, _ass_has_asr_prompt_echo(primary)

    checked = 0
    for output in candidates[1:]:
        if not output.is_file():
            continue
        checked += 1
        if _ass_has_asr_prompt_echo(output):
            return container_video_path, local_video_path, checked, True
    return container_video_path, local_video_path, checked, False


def _ass_has_asr_prompt_echo(path: Path) -> bool:
    texts: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for raw_line in handle:
                if not raw_line.startswith("Dialogue:"):
                    continue
                payload = raw_line.split(":", 1)[1].lstrip()
                parts = payload.split(",", 9)
                text = parts[9] if len(parts) >= 10 else parts[-1]
                texts.append(re.sub(r"\{[^{}]*\}", "", text.replace(r"\N", " ")))
    except OSError:
        return False
    return bool(asr_artifact_line_indexes(texts))


def _container_to_local_video_path(container_video_path: str, anime_root: Path, container_anime_root: str) -> Path:
    root = container_anime_root.rstrip("/")
    if container_video_path.startswith(root + "/"):
        relative = container_video_path[len(root) + 1 :]
    else:
        relative = container_video_path.lstrip("/")
    parts = [part for part in PurePosixPath(relative).parts if part not in {"", ".", ".."}]
    return anime_root.joinpath(*parts)


def _media_ai_output_index(videos: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    parent_to_stems: dict[Path, set[str]] = {}
    lookup: dict[tuple[str, str], str] = {}
    for video in videos:
        parent_to_stems.setdefault(video.parent, set()).add(video.stem)
        key = str(video).casefold()
        lookup[(str(video.parent).casefold(), video.stem)] = key
        index.setdefault(key, [])

    for parent, stems in parent_to_stems.items():
        if not parent.exists():
            continue
        try:
            entries = list(os.scandir(parent))
        except OSError:
            continue
        for entry in entries:
            lowered_name = entry.name.casefold()
            is_ass = lowered_name.endswith(".ass")
            is_quality_report = lowered_name.endswith(".ass.quality.json")
            try:
                is_file = entry.is_file()
            except OSError:
                is_file = False
            if not is_file or not (is_ass or is_quality_report) or ".ai" not in lowered_name:
                continue
            output = Path(entry.path)
            for stem in stems:
                if entry.name.startswith(f"{stem}.AI"):
                    key = lookup.get((str(parent).casefold(), stem))
                    if key is not None:
                        index[key].append(output)
                    break
    for key, outputs in list(index.items()):
        index[key] = sorted(set(outputs), key=lambda path: path.name.casefold())
    return index


def _cache_ai_output_index(cache_root: Path, cache_base_names: object) -> dict[str, list[Path]]:
    bases = {str(name).casefold() for name in cache_base_names}
    index: dict[str, list[Path]] = {base: [] for base in bases}
    if not cache_root.exists():
        return index
    try:
        entries = list(cache_root.iterdir())
    except OSError:
        return index
    for path in entries:
        if not path.is_file():
            continue
        name = path.name.casefold()
        if ".ai" not in name:
            continue
        for base in bases:
            if name.startswith(base + ".ai"):
                index[base].append(path)
                break
    for key, outputs in list(index.items()):
        index[key] = sorted(set(outputs), key=lambda path: path.name.casefold())
    return index


def _include_managed_quality_reports(
    cache_outputs: list[Path],
    media_outputs: list[Path],
    work_path: Path,
) -> list[Path]:
    combined = set(cache_outputs)
    for output in media_outputs:
        if output.suffix.casefold() != ".ass":
            continue
        report = managed_quality_report_path(output, work_path)
        if report.is_file():
            combined.add(report)
    return sorted(combined, key=lambda path: str(path).casefold())


def _translated_ai_outputs(base_name: str, outputs: list[Path]) -> list[Path]:
    translated: list[Path] = []
    for output in outputs:
        if not output.name.startswith(base_name):
            continue
        suffix = output.name[len(base_name) :].casefold()
        if not suffix.startswith(".ai"):
            continue
        if ".zh" in suffix or any(marker in suffix for marker in ("简", "繁", "簡", "體")):
            translated.append(output)
    return translated


def _ai_srt_cache_base_from_container_path(container_video_path: str, cache_root: Path) -> Path:
    digest = hashlib.sha1(container_video_path.encode("utf-8")).hexdigest()[:16]
    stem = Path(container_video_path).stem
    safe_stem = _safe_cache_name(stem)
    return cache_root / f"{safe_stem}.{digest}"


def _safe_cache_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return cleaned[:80] or "video"


def _backup_destination(backup_root: Path, source: Path) -> Path:
    drive = source.drive.rstrip(":").replace("\\", "_") or "root"
    relative_parts = [drive, *source.parts[1:]]
    destination = backup_root.joinpath(*relative_parts)
    if not destination.exists():
        return destination
    suffix = 1
    while True:
        candidate = destination.with_name(f"{destination.stem}.{suffix}{destination.suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


def _requeue_targets(
    db_path: Path,
    targets: list[CleanupTarget],
    *,
    batch_size: int = 50,
    busy_timeout_seconds: int = 300,
    progress_interval_seconds: float = 5.0,
) -> int:
    now = time.time()
    print(f"requeue_start: targets={len(targets)}", flush=True)
    if not targets:
        print("requeue_complete: targets=0", flush=True)
        return 0

    batch_size = max(1, int(batch_size))
    busy_timeout_seconds = max(1, int(busy_timeout_seconds))
    deadline = time.monotonic() + busy_timeout_seconds
    processed = 0
    next_progress = time.monotonic() + max(1.0, progress_interval_seconds)

    for offset in range(0, len(targets), batch_size):
        batch = targets[offset : offset + batch_size]
        while True:
            conn = sqlite3.connect(db_path, timeout=min(5, busy_timeout_seconds))
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("BEGIN IMMEDIATE")
                for target in batch:
                    _upsert_cleanup_requeue_target(conn, target, now)
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                conn.rollback()
                if "locked" not in str(exc).casefold() and "busy" not in str(exc).casefold():
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "scanner_state.sqlite3 remained locked while requeueing; "
                        f"committed={processed}/{len(targets)} timeout={busy_timeout_seconds}s"
                    ) from exc
                if progress_interval_seconds > 0 and time.monotonic() >= next_progress:
                    remaining = max(0, int(deadline - time.monotonic()))
                    print(
                        "requeue_wait: "
                        f"committed={processed}/{len(targets)} retry_in=2s remaining_timeout={remaining}s",
                        flush=True,
                    )
                    next_progress = time.monotonic() + max(1.0, progress_interval_seconds)
                time.sleep(2.0)
            finally:
                conn.close()

        processed += len(batch)
        if processed == len(targets) or (
            progress_interval_seconds > 0 and time.monotonic() >= next_progress
        ):
            print(f"requeue_progress: committed={processed}/{len(targets)}", flush=True)
            next_progress = time.monotonic() + max(1.0, progress_interval_seconds)

    print(f"requeue_complete: targets={processed}", flush=True)
    return processed


def _upsert_cleanup_requeue_target(conn: sqlite3.Connection, target: CleanupTarget, now: float) -> None:
    try:
        stat = target.local_video_path.stat()
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        mtime_ns = 0
    conn.execute(
        """
        INSERT INTO ai_candidate_queue(
            path, mtime_ns, status, source, attempts, running_at,
            last_error, last_error_at, next_retry_at, force_ai,
            added_at, updated_at
        )
        VALUES (?, ?, 'queued', 'selective-ai-cleanup', 0, 0, '', 0, 0, 0, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            mtime_ns = excluded.mtime_ns,
            status = 'queued',
            source = 'selective-ai-cleanup',
            attempts = 0,
            running_at = 0,
            last_error = '',
            last_error_at = 0,
            next_retry_at = 0,
            force_ai = 0,
            added_at = excluded.added_at,
            updated_at = excluded.updated_at
        """,
        (target.container_video_path, mtime_ns, now, now),
    )
    conn.execute("DELETE FROM ai_job_state WHERE path = ?", (target.container_video_path,))
    conn.execute("DELETE FROM ai_stage_events WHERE path = ?", (target.container_video_path,))


if __name__ == "__main__":
    raise SystemExit(main())
