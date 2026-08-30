from __future__ import annotations

import argparse
from pathlib import Path
import hashlib
import json
import logging
import time

from ass_utils import convert_ass_file_to_srt
from config import load_config
from lock import VideoLock
from metadata_context import build_series_metadata_context
from output_manifest import (
    begin_output_publication,
    finish_output_publication,
    output_manifest_path,
    output_publication_marker_path,
    write_output_manifest,
)
from safe_files import atomic_write_text, sha256_file, verified_copy_replace
from srt_utils import SrtBlock, read_srt, validate_translation, write_srt
from subtitle_paths import paths_for_video
from subtitle_quality import quality_report_candidates
from transcriber import _is_hallucination_text, asr_diagnostics_path, asr_transcription_hold_path
from translation_quality import (
    read_translation_quality_events_strict,
    read_translation_quality_hold_strict,
    translation_quality_events_path,
    translation_quality_hold_path,
    write_translation_quality_hold,
    write_translation_quality_events,
)
from translator import SubtitleTranslator
from worker import VideoWorker


def parse_line_spec(value: str) -> set[int]:
    result: set[int] = set()
    for token in str(value).split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start, end = int(start_raw), int(end_raw)
            if start <= 0 or end < start or end - start > 500:
                raise ValueError(f"Invalid subtitle line range: {token}")
            result.update(range(start, end + 1))
        else:
            index = int(token)
            if index <= 0:
                raise ValueError(f"Invalid subtitle line index: {token}")
            result.add(index)
    if not result:
        raise ValueError("At least one subtitle line must be selected")
    return result


def retranslate_lines(config: object, video: Path, indexes: set[int], logger: logging.Logger) -> dict[str, object]:
    root = Path(config.input_path).resolve()
    resolved_video = video.resolve()
    try:
        resolved_video.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Video must stay under input_path: {video}") from exc
    if not resolved_video.is_file() or resolved_video.suffix.lower() not in config.video_extensions:
        raise ValueError(f"Video does not exist or is unsupported: {resolved_video}")

    lock = VideoLock(resolved_video)
    if not lock.acquire():
        raise RuntimeError(f"Video is currently being processed: {resolved_video}")
    archive_dir: Path | None = None
    temp_translation: Path | None = None
    try:
        paths = paths_for_video(resolved_video, config)
        worker = VideoWorker(config, logger)
        # Snapshot every mutable cache, sidecar, report and publication marker
        # before cache restoration or any translation output is written.
        archive_dir = _archive_translation_outputs(config, resolved_video, paths, indexes)
        worker._recover_pending_asr_commit(resolved_video, paths)
        worker._restore_japanese_srt_cache_from_ass(paths)
        if not paths.ja_srt.is_file():
            raise RuntimeError("Japanese transcript is unavailable; use full retranscribe instead")

        temporary_zh_cn = False
        if not paths.zh_cn_srt.is_file():
            if not paths.ai_zh_cn_ass.is_file():
                raise RuntimeError("Existing simplified Chinese subtitle is unavailable; use full retranslate instead")
            convert_ass_file_to_srt(paths.ai_zh_cn_ass, paths.zh_cn_srt)
            restored = [
                SrtBlock(block.index, block.timing, [block.text[0]] if block.text else [])
                for block in read_srt(paths.zh_cn_srt)
            ]
            write_srt(paths.zh_cn_srt, restored)
            temporary_zh_cn = True

        source_blocks = read_srt(paths.ja_srt)
        translated_blocks = read_srt(paths.zh_cn_srt)
        validate_translation(source_blocks, translated_blocks)
        existing_events = read_translation_quality_events_strict(paths.zh_cn_srt)
        source_by_index = {block.index: block for block in source_blocks}
        translated_by_index = {block.index: block for block in translated_blocks}
        missing = sorted(index for index in indexes if index not in source_by_index)
        if missing:
            raise ValueError(f"Subtitle line indexes do not exist: {missing[:20]}")

        selected_source = [source_by_index[index] for index in sorted(indexes)]
        hallucination_indexes = [
            block.index
            for block in source_blocks
            if _is_hallucination_text(" ".join(block.text), config)
        ]
        if hallucination_indexes:
            raise RuntimeError(
                "Japanese transcript contains known ASR hallucination text at lines "
                f"{hallucination_indexes[:20]}; retranscription is required; "
                "use full retranscribe instead"
            )
        temp_translation = Path(config.work_path) / f"line-retranslate-{hashlib.sha1(str(resolved_video).encode()).hexdigest()[:16]}.srt"
        context = build_series_metadata_context(resolved_video, config, logger)
        translator = SubtitleTranslator(config, logger)
        translator.translate_blocks(
            selected_source,
            paths.ja_srt,
            temp_translation,
            series_context=context.text if context else "",
            series_glossary=(context.glossary or {}) if context else {},
        )
        replacement_events = read_translation_quality_events_strict(temp_translation)
        replacement_blocks = read_srt(temp_translation)
        validate_translation(selected_source, replacement_blocks)
        replacements = {block.index: block for block in replacement_blocks}
        translated_by_index.update(replacements)
        merged = [translated_by_index[block.index] for block in source_blocks if block.index in translated_by_index]
        if len(merged) != len(source_blocks):
            raise RuntimeError("Existing translation is incomplete; use full retranslate instead")
        validate_translation(source_blocks, merged)
        retained_events = [
            event
            for event in existing_events
            if int(event.get("index") or 0) not in indexes
        ]
        merged_events = [*retained_events, *replacement_events]
        write_srt(temp_translation, merged)
        planned_sha256 = sha256_file(temp_translation)
        if merged_events:
            write_translation_quality_events(
                paths.zh_cn_srt,
                merged_events,
                srt_sha256=planned_sha256,
            )
        else:
            write_translation_quality_hold(
                paths.zh_cn_srt,
                srt_sha256=planned_sha256,
                reason="manual line retranslation pending zh-TW regeneration",
            )
        temp_translation.replace(paths.zh_cn_srt)
        worker._convert_to_zh_tw(paths.zh_cn_srt, paths.zh_tw_srt)
        validate_translation(
            read_srt(paths.zh_cn_srt),
            read_srt(paths.zh_tw_srt),
        )
        pending_hold = read_translation_quality_hold_strict(paths.zh_cn_srt)
        if pending_hold is not None:
            write_translation_quality_events(paths.zh_cn_srt, [])
            translation_quality_hold_path(paths.zh_cn_srt).unlink()
        begin_output_publication(resolved_video, config)
        worker._publish_ai_ass(resolved_video, paths)
        write_output_manifest(
            resolved_video,
            config,
            [
                paths.ai_ja_ass,
                paths.ai_zh_cn_ass,
                paths.ai_zh_tw_ass,
            ],
            provenance={
                "operation": "manual_line_retranslate",
                "lines": sorted(indexes),
            },
            publication_kind="translated_trilingual",
            output_languages=("ja", "zh-CN", "zh-TW"),
        )
        finish_output_publication(resolved_video, config)
        worker._deliver_completed_media_if_required(resolved_video)
        if not config.keep_intermediate_files:
            # The Japanese SRT is the verified, expensive ASR source of truth.
            # Keep it after a successful line repair so another review action
            # can reuse the same transcript instead of rerunning Whisper.
            paths.zh_cn_srt.unlink(missing_ok=True)
            paths.zh_tw_srt.unlink(missing_ok=True)
        elif temporary_zh_cn:
            logger.info("Keeping restored zh-CN SRT after line retranslation: %s", paths.zh_cn_srt)

        manifest = {
            "video": str(resolved_video),
            "lines": sorted(indexes),
            "archive": str(archive_dir),
            "updated_outputs": [str(paths.ai_zh_cn_ass), str(paths.ai_zh_tw_ass)],
            "completed_at": time.time(),
        }
        atomic_write_text(
            archive_dir / "result.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

        return manifest
    except Exception as operation_error:
        if archive_dir is not None:
            try:
                _restore_archived_translation_outputs(archive_dir, paths, config, resolved_video)
            except Exception as restore_error:
                raise RuntimeError(
                    "Line retranslation failed and the verified output restore was incomplete; "
                    f"manual recovery archive: {archive_dir}"
                ) from operation_error
        raise
    finally:
        if temp_translation is not None:
            temp_translation.unlink(missing_ok=True)
            translation_quality_events_path(temp_translation).unlink(missing_ok=True)
            translation_quality_hold_path(temp_translation).unlink(missing_ok=True)
        lock.release()


def _archive_translation_outputs(config: object, video: Path, paths: object, indexes: set[int]) -> Path:
    digest = hashlib.sha1(str(video).encode("utf-8")).hexdigest()[:16]
    archive = Path(config.work_path) / "manual_line_retranslate" / f"{int(time.time() * 1000)}-{digest}"
    archive.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, str]] = []
    candidates = _translation_restore_candidates(config, video, paths)
    for index, source in enumerate(candidates):
        if not source.is_file():
            continue
        source_digest = hashlib.sha1(str(source).encode("utf-8", errors="replace")).hexdigest()[:16]
        suffix = source.suffix if source.suffix else ".bin"
        destination = archive / f"item-{index:03d}-{source_digest}{suffix}"
        verified_copy_replace(source, destination)
        copied.append(
            {
                "source": str(source),
                "archive": str(destination),
                "sha256": sha256_file(destination),
            }
        )
    atomic_write_text(
        archive / "manifest.json",
        json.dumps(
            {"video": str(video), "lines": sorted(indexes), "created_at": time.time(), "copied": copied},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return archive


def _translation_restore_candidates(config: object, video: Path, paths: object) -> list[Path]:
    subtitle_outputs = (
        paths.ja_srt,
        paths.zh_cn_srt,
        paths.zh_tw_srt,
        paths.ai_ja_ass,
        paths.ai_zh_cn_ass,
        paths.ai_zh_tw_ass,
    )
    candidates: list[Path] = [
        *subtitle_outputs,
        translation_quality_events_path(paths.zh_cn_srt),
        translation_quality_hold_path(paths.zh_cn_srt),
        asr_diagnostics_path(paths.ja_srt, config),
        asr_transcription_hold_path(paths.ja_srt, config),
        output_manifest_path(video, config),
        output_publication_marker_path(video, config),
        *[
            report_path
            for path in subtitle_outputs
            for report_path in quality_report_candidates(path, config.work_path)
        ],
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _restore_archived_translation_outputs(
    archive: Path,
    paths: object,
    config: object,
    video: Path,
) -> None:
    manifest_path = archive / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    copied = payload.get("copied") if isinstance(payload, dict) else None
    if not isinstance(copied, list):
        raise RuntimeError(f"Invalid line retranslation archive manifest: {manifest_path}")

    allowed = {str(path): path for path in _translation_restore_candidates(config, video, paths)}
    archived_by_source: dict[str, dict[str, str]] = {}
    resolved_archive = archive.resolve()
    for raw_entry in copied:
        if not isinstance(raw_entry, dict):
            raise RuntimeError(f"Invalid line retranslation archive entry: {manifest_path}")
        source = str(raw_entry.get("source") or "")
        if source not in allowed:
            raise RuntimeError(f"Refused unexpected line retranslation restore target: {source}")
        archived = Path(str(raw_entry.get("archive") or ""))
        try:
            archived.resolve().relative_to(resolved_archive)
        except ValueError as exc:
            raise RuntimeError(f"Refused archive path outside restore directory: {archived}") from exc
        archived_by_source[source] = {
            "archive": str(archived),
            "sha256": str(raw_entry.get("sha256") or ""),
        }

    errors: list[str] = []
    for source, target in allowed.items():
        archived_entry = archived_by_source.get(source)
        try:
            if archived_entry is None:
                target.unlink(missing_ok=True)
                continue
            saved = Path(archived_entry["archive"])
            expected_sha256 = archived_entry["sha256"]
            if not saved.is_file() or not expected_sha256 or sha256_file(saved) != expected_sha256:
                raise RuntimeError(f"Archived restore checksum mismatch: {saved}")
            verified_copy_replace(saved, target)
        except Exception as exc:
            errors.append(f"{target}: {type(exc).__name__}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Retranslate selected AI subtitle lines")
    parser.add_argument("--config", required=True)
    parser.add_argument("--video-path", required=True)
    parser.add_argument("--lines", required=True, help="Comma-separated indexes and ranges, for example 1,4,8-12")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = retranslate_lines(load_config(args.config), Path(args.video_path), parse_line_spec(args.lines), logging.getLogger("line-retranslate"))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
