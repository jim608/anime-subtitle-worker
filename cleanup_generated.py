from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import shutil

from config import AppConfig
from subtitle_extract import remove_ai_srt_outputs


@dataclass(frozen=True)
class CleanupGeneratedResult:
    removed_ai_srt: int = 0
    removed_qa: int = 0
    removed_cache_dirs: int = 0

    @property
    def total_removed(self) -> int:
        return self.removed_ai_srt + self.removed_qa + self.removed_cache_dirs


def cleanup_generated_artifacts(config: AppConfig, logger: logging.Logger) -> CleanupGeneratedResult:
    removed_ai_srt = 0

    for video in _iter_videos(config):
        removed_ai_srt += len(remove_ai_srt_outputs(video, config, force=True))

    removed_qa = _remove_qa_reports(config, logger)
    removed_cache_dirs = _remove_ai_srt_cache(config, logger)
    result = CleanupGeneratedResult(
        removed_ai_srt=removed_ai_srt,
        removed_qa=removed_qa,
        removed_cache_dirs=removed_cache_dirs,
    )
    logger.info(
        "Generated artifact cleanup complete. ai_srt=%s qa=%s cache_dirs=%s total=%s",
        result.removed_ai_srt,
        result.removed_qa,
        result.removed_cache_dirs,
        result.total_removed,
    )
    return result


def _iter_videos(config: AppConfig) -> list[Path]:
    input_path = Path(config.input_path)
    if not input_path.exists():
        return []
    extensions = set(config.video_extensions)
    return [
        path
        for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    ]


def _remove_qa_reports(config: AppConfig, logger: logging.Logger) -> int:
    input_path = Path(config.input_path)
    if not input_path.exists():
        return 0
    removed = 0
    for qa_path in input_path.rglob("*.qa.txt"):
        if not qa_path.is_file():
            continue
        try:
            qa_path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            logger.warning("Failed to remove QA report %s: %s", qa_path, exc)
    return removed


def _remove_ai_srt_cache(config: AppConfig, logger: logging.Logger) -> int:
    cache_path = Path(config.work_path) / "ai_srt_cache"
    if not cache_path.exists():
        return 0
    try:
        shutil.rmtree(cache_path)
        return 1
    except OSError as exc:
        logger.warning("Failed to remove AI SRT cache %s: %s", cache_path, exc)
        return 0
