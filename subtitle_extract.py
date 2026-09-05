from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from config import AppConfig
from resource_scheduler import wait_for_extraction_pressure
from safe_files import atomic_write_text, fsync_directory, sha256_file, verified_copy_replace


class SubtitleExtractError(RuntimeError):
    pass


class SubtitleExtractCancelled(SubtitleExtractError):
    """Raised when a cooperative Mikan extraction deadline is reached."""


@dataclass(frozen=True)
class ExtractedSubtitle:
    path: Path
    language: str
    stream_index: int
    classification: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubtitleClassification:
    language: str | None
    reason: str
    metadata_language: str | None
    traditional_score: int
    simplified_score: int
    japanese_score: int
    cjk_chars: int
    text_chars: int
    quality_score: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "reason": self.reason,
            "metadata_language": self.metadata_language,
            "traditional_score": self.traditional_score,
            "simplified_score": self.simplified_score,
            "japanese_score": self.japanese_score,
            "cjk_chars": self.cjk_chars,
            "text_chars": self.text_chars,
            "quality_score": self.quality_score,
        }


@dataclass(frozen=True)
class _SubtitleCandidate:
    source_path: Path
    language: str
    stream_index: int
    classification: SubtitleClassification
    priority: tuple[int, int]
    title: str = ""
    forced: bool = False


TEXT_SUBTITLE_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text"}
IMAGE_SUBTITLE_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "pgssub", "xsub"}
SIDECAR_SUBTITLE_EXTENSIONS = {".ass", ".ssa", ".srt", ".vtt"}
DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS = 300

_ENGLISH_SIDECAR_METADATA_RE = re.compile(
    r"(?:^|[._ -])(?:english|eng|en)"
    r"(?:[._ -](?:english|eng|en))*\.(?:ass|ssa|srt|vtt)$",
    re.IGNORECASE,
)
_ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_ENGLISH_EVIDENCE_WORDS = frozenset(
    {
        "and",
        "are",
        "but",
        "can",
        "do",
        "does",
        "from",
        "has",
        "have",
        "he",
        "here",
        "how",
        "is",
        "it",
        "not",
        "she",
        "should",
        "that",
        "the",
        "their",
        "there",
        "they",
        "this",
        "was",
        "we",
        "were",
        "what",
        "where",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


def extract_available_subtitles(
    video_path: str | Path,
    config: AppConfig,
    output_video_path: str | Path | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    allowed_languages: set[str] | None = None,
    cancel_event: Any | None = None,
    deadline_monotonic: float | None = None,
    validate_for_import: bool = False,
) -> list[ExtractedSubtitle]:
    source_video = Path(video_path)
    output_video = Path(output_video_path) if output_video_path is not None else source_video
    _raise_if_extract_cancelled(cancel_event, deadline_monotonic)
    timeout_seconds = _subtitle_extract_timeout_seconds(config)
    streams = _probe_subtitle_streams(
        source_video,
        timeout_seconds=timeout_seconds,
        deadline_monotonic=deadline_monotonic,
    )
    subtitle_ordinals = _subtitle_stream_ordinals(streams)
    extracted: list[ExtractedSubtitle] = []
    candidates: list[_SubtitleCandidate] = []
    output_video.parent.mkdir(parents=True, exist_ok=True)

    temp_prefix = f".subtitle-{hashlib.sha1(str(output_video).encode('utf-8', errors='replace')).hexdigest()[:12]}-"
    with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=str(output_video.parent)) as temp_dir:
        extracted_streams: list[tuple[Path, dict[str, Any], str]] = []
        for stream in sorted(streams, key=_stream_priority):
            _raise_if_extract_cancelled(cancel_event, deadline_monotonic)
            codec = str(stream.get("codec_name", "")).lower()
            if codec not in TEXT_SUBTITLE_CODECS:
                _append_subtitle_diagnostic(
                    diagnostics,
                    source="embedded",
                    status="unsupported_codec",
                    stream=stream,
                    codec=codec,
                )
                continue

            temp_output = Path(temp_dir) / f"stream_{int(stream['index'])}.ass"
            try:
                _extract_subtitle_stream(
                    source_video,
                    int(stream["index"]),
                    temp_output,
                    codec,
                    timeout_seconds=timeout_seconds,
                    subtitle_ordinal=subtitle_ordinals.get(int(stream["index"])),
                    deadline_monotonic=deadline_monotonic,
                )
            except SubtitleExtractCancelled:
                raise
            except SubtitleExtractError as exc:
                _append_subtitle_diagnostic(
                    diagnostics,
                    source="embedded",
                    status="extract_failed",
                    stream=stream,
                    codec=codec,
                    detail=str(exc),
                )
                continue

            extracted_streams.append((temp_output, stream, codec))

        # Language metadata is not trusted. Every supported text stream is
        # extracted first, then classified from its actual subtitle text.
        for temp_output, stream, codec in extracted_streams:
            _raise_if_extract_cancelled(cancel_event, deadline_monotonic)
            classification = _classify_extracted_subtitle(temp_output, stream)
            _append_subtitle_diagnostic(
                diagnostics,
                source="embedded",
                status="candidate" if classification.language else "unclassified",
                stream=stream,
                codec=codec,
                classification=classification,
            )
            if classification.language is None:
                continue
            candidates.append(
                _SubtitleCandidate(
                    temp_output,
                    classification.language,
                    int(stream["index"]),
                    classification,
                    _stream_priority(stream),
                    str((stream.get("tags") or {}).get("title") or ""),
                    bool((stream.get("disposition") or {}).get("forced")),
                )
            )

        _raise_if_extract_cancelled(cancel_event, deadline_monotonic)

        if validate_for_import:
            candidates = _validated_import_candidates(
                candidates, output_video, config, diagnostics, deadline_monotonic,
            )
        selected = _select_best_subtitle_candidates(candidates)
        if allowed_languages is not None:
            selected = [candidate for candidate in selected if candidate.language in allowed_languages]
        publications = [
            (candidate.source_path, _subtitle_output_path(output_video, candidate.language), candidate.language)
            for candidate in selected
        ]
        _publish_official_subtitle_set(output_video, publications, config)
        for candidate, (_source, output, _language) in zip(selected, publications, strict=True):
            extracted.append(
                ExtractedSubtitle(
                    output,
                    candidate.language,
                    candidate.stream_index,
                    candidate.classification.as_dict(),
                )
            )

    if extracted and _should_remove_ai_after_official_extract(config):
        remove_ai_subtitle_outputs(output_video, config)

    return extracted


def _validated_import_candidates(
    candidates: list[_SubtitleCandidate],
    target_video: Path,
    config: AppConfig,
    diagnostics: list[dict[str, Any]] | None,
    deadline_monotonic: float | None,
) -> list[_SubtitleCandidate]:
    """Apply the existing source policy before any external subtitle is published."""
    from source_analyzer import AnalyzerThresholds, analyze_subtitle_candidate
    from source_inventory import _probe_media, _subtitle_metrics

    if not candidates:
        return []
    try:
        probe = _probe_media(
            target_video, ffprobe_path=None,
            timeout_seconds=_remaining_extract_timeout(
                _subtitle_extract_timeout_seconds(config), deadline_monotonic,
            ),
        )
        duration = float(probe.get("format", {}).get("duration") or 0)
        if duration <= 0:
            raise ValueError("target duration unavailable")
    except Exception as exc:
        raise SubtitleExtractError(f"Import target ffprobe validation failed: {exc}") from exc

    policy_factory = getattr(config, "source_analyzer_thresholds", None)
    policy = policy_factory() if callable(policy_factory) else AnalyzerThresholds()
    accepted: list[_SubtitleCandidate] = []
    for candidate in candidates:
        _raise_if_extract_deadline_reached(deadline_monotonic)
        metrics = _subtitle_metrics(candidate.source_path)
        analysis = analyze_subtitle_candidate(
            {
                **vars(metrics),
                "track_index": candidate.stream_index,
                "codec": candidate.source_path.suffix.lstrip("."),
                "source_reference": str(candidate.source_path),
                "container_language_tag": candidate.language,
                "title": candidate.title or candidate.source_path.stem,
                "forced": candidate.forced,
            },
            media_duration_seconds=duration, thresholds=policy,
        )
        parse_pass = metrics.event_count > 0 and metrics.valid_timing_count == metrics.event_count
        passed = parse_pass and analysis.eligible
        if diagnostics is not None:
            diagnostics.append({
                "source": "import_validation", "status": "validated" if passed else "validation_failed",
                "path": str(candidate.source_path), "target": str(target_video),
                "output_parse": "PASS" if parse_pass else "FAIL",
                "source_analysis": analysis.to_dict(),
                "detail": ",".join(analysis.rejection_reasons) or ("" if parse_pass else "invalid_timing"),
            })
        if passed:
            accepted.append(candidate)
    return accepted


def verified_official_subtitle_languages(video: Path, config: AppConfig) -> set[str]:
    candidates = []
    for path in video.parent.glob(f"{video.stem}.*"):
        if not path.is_file() or path.suffix.casefold() not in SIDECAR_SUBTITLE_EXTENSIONS:
            continue
        classification = classify_sidecar_subtitle(path)
        if classification.language in {"zh-tw", "zh-cn"}:
            candidates.append(_SubtitleCandidate(path, classification.language, -1, classification, (0, 0)))
    return {candidate.language for candidate in _validated_import_candidates(candidates, video, config, None, None)}


def _raise_if_extract_cancelled(cancel_event: Any | None, deadline_monotonic: float | None) -> None:
    wait_for_extraction_pressure(
        cancel_event=cancel_event,
        deadline_monotonic=deadline_monotonic,
    )
    cancelled = bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())
    deadline_reached = deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic)
    if cancelled or deadline_reached:
        raise SubtitleExtractCancelled("Subtitle extraction was cancelled before the next stream")


def _remaining_extract_timeout(configured_seconds: float, deadline_monotonic: float | None) -> float:
    timeout = max(0.001, float(configured_seconds or 0.001))
    if deadline_monotonic is None:
        return timeout
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining <= 0:
        raise SubtitleExtractCancelled("Subtitle extraction reached its absolute deadline")
    return min(timeout, remaining)


def _raise_if_extract_deadline_reached(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and time.monotonic() >= float(deadline_monotonic):
        raise SubtitleExtractCancelled("Subtitle extraction reached its absolute deadline")


def _format_timeout_seconds(timeout_seconds: float) -> str:
    return f"{float(timeout_seconds):g}"


def _publish_official_subtitle_set(
    output_video: Path,
    publications: list[tuple[Path, Path, str]],
    config: AppConfig,
) -> None:
    """Validate and atomically publish one complete official subtitle set."""

    if not publications:
        return

    effective: list[tuple[Path, Path, str, str]] = []
    seen_outputs: set[str] = set()
    for source, output, expected_language in publications:
        if not source.is_file():
            raise SubtitleExtractError(f"Official subtitle staging file is missing: {source}")
        output_key = str(output.resolve())
        if output_key in seen_outputs:
            raise SubtitleExtractError(f"Duplicate official subtitle publication target: {output}")
        seen_outputs.add(output_key)
        classification = _classify_subtitle_content_detail(
            _read_subtitle_sample(source),
            metadata_language=expected_language,
        )
        if classification.language != expected_language:
            raise SubtitleExtractError(
                "Official subtitle changed language during staging: "
                f"expected={expected_language} detected={classification.language or 'unknown'} source={source}"
            )
        source_sha256 = sha256_file(source)
        if output.is_file() and sha256_file(output) == source_sha256:
            continue
        effective.append((source, output, expected_language, source_sha256))
    if not effective:
        return

    video_digest = hashlib.sha1(
        str(output_video.resolve()).encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    work_path = Path(getattr(config, "work_path", output_video.parent / ".subtitle-worker-work"))
    version_root = work_path / "official_subtitle_versions" / video_digest / str(time.time_ns())
    version_root.mkdir(parents=True, exist_ok=False)
    manifest_path = version_root / "manifest.json"
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    prepared_manifest: dict[str, Any] = {}
    try:
        for index, (_source, output, _language, _sha256) in enumerate(effective):
            if not output.is_file():
                continue
            backup = version_root / f"previous-{index}.ass"
            verified_copy_replace(output, backup)
            backups[output] = backup
        prepared_manifest = {
            "schema_version": 1,
            "status": "prepared",
            "video": str(output_video),
            "created_at": time.time(),
            "publications": [
                {
                    "source": str(source),
                    "output": str(output),
                    "language": language,
                    "sha256": source_sha256,
                }
                for source, output, language, source_sha256 in effective
            ],
            "backups": [
                {
                    "output": str(output),
                    "backup": str(backup),
                    "sha256": sha256_file(backup),
                }
                for output, backup in backups.items()
            ],
        }
        atomic_write_text(
            manifest_path,
            json.dumps(prepared_manifest, ensure_ascii=False, indent=2) + "\n",
        )
        for source, output, _language, _sha256 in effective:
            published.append(output)
            verified_copy_replace(source, output)
        completed_manifest = {
            **prepared_manifest,
            "status": "completed",
            "completed_at": time.time(),
            "published": [
                {"output": str(output), "sha256": sha256_file(output)}
                for _source, output, _language, _sha256 in effective
            ],
        }
        atomic_write_text(
            manifest_path,
            json.dumps(completed_manifest, ensure_ascii=False, indent=2) + "\n",
        )
        _prune_official_subtitle_versions(config, video_digest, work_path=work_path)
    except Exception as publish_error:
        rollback_errors: list[dict[str, str]] = []
        for output in reversed(published):
            try:
                backup = backups.get(output)
                if backup is None:
                    output.unlink(missing_ok=True)
                    fsync_directory(output.parent)
                else:
                    verified_copy_replace(backup, output)
            except Exception as rollback_error:
                rollback_errors.append(
                    {
                        "output": str(output),
                        "error": f"{type(rollback_error).__name__}: {rollback_error}",
                    }
                )
        if manifest_path.is_file():
            try:
                atomic_write_text(
                    manifest_path,
                    json.dumps(
                        {
                            **prepared_manifest,
                            "status": "rollback_failed" if rollback_errors else "rolled_back",
                            "rolled_back_at": time.time(),
                            "publication_error": f"{type(publish_error).__name__}: {publish_error}",
                            "rollback_errors": rollback_errors,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
            except Exception:
                pass
        if rollback_errors:
            failed = ", ".join(item["output"] for item in rollback_errors)
            raise SubtitleExtractError(
                "Official subtitle publication failed and rollback was incomplete; "
                f"manual restore required for: {failed}"
            ) from publish_error
        raise


def _prune_official_subtitle_versions(
    config: AppConfig,
    video_digest: str,
    *,
    work_path: Path | None = None,
) -> None:
    keep = max(1, int(getattr(config, "official_subtitle_versions_keep", 3) or 3))
    root = (work_path or Path(getattr(config, "work_path", "."))) / "official_subtitle_versions" / video_digest
    try:
        resolved_root = root.resolve()
        completed: list[tuple[int, Path]] = []
        for candidate in root.iterdir():
            if not candidate.is_dir() or not candidate.name.isdigit():
                continue
            try:
                payload = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("status") != "completed":
                continue
            completed.append((int(candidate.name), candidate))
        completed.sort(key=lambda item: item[0], reverse=True)
        for _stamp, candidate in completed[keep:]:
            resolved = candidate.resolve()
            if resolved.parent != resolved_root or not resolved.name.isdigit():
                continue
            shutil.rmtree(resolved)
    except (OSError, TypeError, ValueError):
        # Retention is best-effort and must never invalidate a verified publish.
        return


def normalize_sidecar_subtitles(
    video_path: str | Path,
    config: AppConfig,
    *,
    deadline_monotonic: float | None = None,
) -> list[ExtractedSubtitle]:
    if deadline_monotonic is None:
        return normalize_sidecar_subtitles_for_output(
            video_path,
            config,
            output_video_path=video_path,
        )
    return normalize_sidecar_subtitles_for_output(
        video_path,
        config,
        output_video_path=video_path,
        deadline_monotonic=deadline_monotonic,
    )


def normalize_sidecar_subtitles_for_output(
    video_path: str | Path,
    config: AppConfig,
    output_video_path: str | Path | None = None,
    diagnostics: list[dict[str, Any]] | None = None,
    allowed_languages: set[str] | None = None,
    extra_sidecar_paths: list[str | Path] | None = None,
    deadline_monotonic: float | None = None,
    validate_for_import: bool = False,
) -> list[ExtractedSubtitle]:
    video = Path(video_path)
    output_video = Path(output_video_path) if output_video_path is not None else video
    normalized: list[ExtractedSubtitle] = []
    candidates: list[_SubtitleCandidate] = []

    for subtitle in sorted(
        _iter_sidecar_subtitles(video, extra_sidecar_paths=extra_sidecar_paths),
        key=lambda item: str(item).casefold(),
    ):
        classification = (
            _empty_classification("ai_sidecar_skipped")
            if is_configured_ai_source_transcript_sidecar(video, subtitle, config)
            else classify_sidecar_subtitle(subtitle)
        )
        _append_subtitle_diagnostic(
            diagnostics,
            source="sidecar",
            status="candidate" if classification.language else "unclassified",
            path=subtitle,
            classification=classification,
        )
        if classification.language is None:
            continue
        candidates.append(
            _SubtitleCandidate(
                subtitle,
                classification.language,
                -1,
                classification,
                _sidecar_priority(classification.language),
                subtitle.stem,
            )
        )

    if validate_for_import:
        candidates = _validated_import_candidates(
            candidates, output_video, config, diagnostics, deadline_monotonic,
        )
    selected = _select_best_subtitle_candidates(candidates)
    if allowed_languages is not None:
        selected = [candidate for candidate in selected if candidate.language in allowed_languages]
    temp_prefix = f".subtitle-{hashlib.sha1(str(output_video).encode('utf-8', errors='replace')).hexdigest()[:12]}-"
    with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=str(output_video.parent)) as temp_dir:
        publications: list[tuple[Path, Path, str]] = []
        staged_by_output: dict[Path, Path] = {}
        for index, candidate in enumerate(selected):
            output = _subtitle_output_path(output_video, candidate.language)
            if candidate.source_path.resolve() == output.resolve():
                continue
            staged = Path(temp_dir) / f"candidate-{index}.ass"
            if deadline_monotonic is None:
                _copy_or_convert_sidecar(candidate.source_path, staged)
            else:
                _copy_or_convert_sidecar(
                    candidate.source_path,
                    staged,
                    timeout_seconds=_subtitle_extract_timeout_seconds(config),
                    deadline_monotonic=deadline_monotonic,
                )
            publications.append((staged, output, candidate.language))
            staged_by_output[output] = staged
        if validate_for_import and publications:
            staged_candidates = [
                _SubtitleCandidate(staged, language, -1, candidate.classification, candidate.priority, candidate.title, candidate.forced)
                for candidate in selected
                for staged, output, language in publications
                if output == _subtitle_output_path(output_video, candidate.language)
            ]
            if len(_validated_import_candidates(
                staged_candidates, output_video, config, diagnostics, deadline_monotonic,
            )) != len(staged_candidates):
                return []
        _publish_official_subtitle_set(output_video, publications, config)

        for candidate in selected:
            output = _subtitle_output_path(output_video, candidate.language)
            if output in staged_by_output:
                _remove_source_sidecar_if_final_ass_mode(candidate.source_path, output, output_video, config)
            normalized.append(
                ExtractedSubtitle(
                    output,
                    candidate.language,
                    -1,
                    candidate.classification.as_dict(),
                )
            )

    if normalized and _should_remove_ai_after_official_extract(config):
        remove_ai_subtitle_outputs(output_video, config)

    return normalized


def classify_sidecar_subtitle_language(subtitle_path: str | Path) -> str | None:
    return classify_sidecar_subtitle(subtitle_path).language


def classify_sidecar_subtitle(subtitle_path: str | Path) -> SubtitleClassification:
    subtitle = Path(subtitle_path)
    lowered = subtitle.name.casefold()
    if _is_ai_generated_sidecar_name(lowered):
        return _empty_classification("ai_sidecar_skipped")

    metadata_language = None
    if _has_traditional_marker(lowered):
        metadata_language = "zh-tw"
    elif _has_simplified_marker(lowered):
        metadata_language = "zh-cn"
    elif any(marker in lowered for marker in ("jpn", ".ja", "japanese", "日本語", "日文")):
        metadata_language = "ja"
    elif _has_english_sidecar_metadata(lowered):
        metadata_language = "en"

    return _classify_subtitle_content_detail(_read_subtitle_sample(subtitle), metadata_language=metadata_language)


def classify_subtitle_content_file(
    subtitle_path: str | Path,
    *,
    metadata_language: str | None = None,
) -> SubtitleClassification:
    """Classify subtitle text without trusting or filtering its filename.

    Canonical AI filenames are intentionally ignored by
    :func:`classify_sidecar_subtitle` so they cannot be mistaken for official
    sources.  Delivery verification still needs to prove that a file named
    ``zh-TW`` actually contains Traditional Chinese, so that verifier uses this
    content-only entry point.
    """

    return _classify_subtitle_content_detail(
        _read_subtitle_sample(Path(subtitle_path)),
        metadata_language=metadata_language,
    )


def remove_ai_subtitle_outputs(video_path: str | Path, config: AppConfig) -> list[Path]:
    video = Path(video_path)
    suffixes = [
        config.ai_japanese_ass_suffix,
        config.ai_simplified_chinese_ass_suffix,
        config.ai_traditional_chinese_ass_suffix,
        ".AI日本語.ja.ass",
        ".AI简体中文.zh.ass",
        ".AI简日双语.zh.ass",
        ".AI繁體中文.zh-TW.ass",
        ".AI繁日雙語.zh-TW.ass",
    ]

    candidates: list[Path] = []
    seen: set[str] = set()
    for suffix in dict.fromkeys(suffixes):
        path = video.with_name(f"{video.stem}{suffix}")
        if not path.is_file():
            continue
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(path)

    if not candidates:
        return []

    configured_work_path = getattr(config, "work_path", None)
    if configured_work_path is None:
        raise SubtitleExtractError(
            "Refusing to remove AI subtitles without a work_path restore archive"
        )

    return _archive_and_retire_ai_subtitle_outputs(
        video,
        candidates,
        config,
        work_path=Path(configured_work_path),
    )


def _archive_and_retire_ai_subtitle_outputs(
    video: Path,
    sources: list[Path],
    config: AppConfig,
    *,
    work_path: Path,
) -> list[Path]:
    """Retire superseded AI ASS files only after a verified restore set exists."""

    video_digest = hashlib.sha1(
        str(video.resolve()).encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    version_root = work_path / "retired_ai_outputs" / video_digest / str(time.time_ns())
    version_root.mkdir(parents=True, exist_ok=False)
    manifest_path = version_root / "manifest.json"
    archived: list[tuple[Path, Path, str]] = []
    removed: list[Path] = []
    prepared_manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "archiving",
        "video": str(video),
        "created_at": time.time(),
        "files": [],
    }
    try:
        # Copy and verify the complete restore set before touching the media
        # directory.  This remains safe when /anime and /work are on different
        # filesystems.
        for index, source in enumerate(sources):
            archive = version_root / f"ai-output-{index}{source.suffix.casefold()}"
            verified_copy_replace(source, archive)
            digest = sha256_file(archive)
            if digest != sha256_file(source):
                raise SubtitleExtractError(
                    f"AI subtitle archive checksum changed before retirement: {source}"
                )
            archived.append((source, archive, digest))

        prepared_manifest = {
            **prepared_manifest,
            "status": "prepared",
            "files": [
                {
                    "source": str(source),
                    "archive": str(archive),
                    "sha256": digest,
                }
                for source, archive, digest in archived
            ],
        }
        atomic_write_text(
            manifest_path,
            json.dumps(prepared_manifest, ensure_ascii=False, indent=2) + "\n",
        )

        for source, _archive, _digest in archived:
            _unlink_retired_ai_output(source)
            fsync_directory(source.parent)
            removed.append(source)

        atomic_write_text(
            manifest_path,
            json.dumps(
                {
                    **prepared_manifest,
                    "status": "completed",
                    "completed_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        _prune_retired_ai_output_versions(config, video_digest, work_path=work_path)
        return removed
    except Exception as retirement_error:
        rollback_errors: list[dict[str, str]] = []
        archives_by_source = {source: archive for source, archive, _digest in archived}
        for source in reversed(removed):
            try:
                verified_copy_replace(archives_by_source[source], source)
            except Exception as rollback_error:
                rollback_errors.append(
                    {
                        "source": str(source),
                        "error": f"{type(rollback_error).__name__}: {rollback_error}",
                    }
                )
        try:
            atomic_write_text(
                manifest_path,
                json.dumps(
                    {
                        **prepared_manifest,
                        "status": "rollback_failed" if rollback_errors else "rolled_back",
                        "rolled_back_at": time.time(),
                        "retirement_error": f"{type(retirement_error).__name__}: {retirement_error}",
                        "rollback_errors": rollback_errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        except Exception:
            # The verified archive files remain authoritative even if the
            # diagnostic manifest cannot be updated.
            pass
        if rollback_errors:
            failed = ", ".join(item["source"] for item in rollback_errors)
            raise SubtitleExtractError(
                "AI subtitle retirement failed and rollback was incomplete; "
                f"manual restore required for: {failed}"
            ) from retirement_error
        raise SubtitleExtractError(
            f"AI subtitle retirement failed; original outputs were preserved: {retirement_error}"
        ) from retirement_error


def _unlink_retired_ai_output(path: Path) -> None:
    path.unlink()


def _prune_retired_ai_output_versions(
    config: AppConfig,
    video_digest: str,
    *,
    work_path: Path,
) -> None:
    keep = max(1, int(getattr(config, "ai_output_versions_keep", 3) or 3))
    root = work_path / "retired_ai_outputs" / video_digest
    try:
        resolved_root = root.resolve()
        completed: list[tuple[int, Path]] = []
        for candidate in root.iterdir():
            if not candidate.is_dir() or not candidate.name.isdigit():
                continue
            try:
                payload = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("status") != "completed":
                continue
            completed.append((int(candidate.name), candidate))
        completed.sort(key=lambda item: item[0], reverse=True)
        for _stamp, candidate in completed[keep:]:
            resolved = candidate.resolve()
            if resolved.parent != resolved_root or not resolved.name.isdigit():
                continue
            shutil.rmtree(resolved)
    except (OSError, TypeError, ValueError):
        return


def _should_remove_ai_after_official_extract(config: AppConfig) -> bool:
    return bool(config.mikan_remove_ai_after_extract) and not bool(getattr(config, "require_ai_subtitles", False))


def remove_ai_srt_outputs(video_path: str | Path, config: AppConfig, *, force: bool = False) -> list[Path]:
    if not force and getattr(config, "keep_intermediate_files", True):
        return []

    video = Path(video_path)
    suffixes = [
        _srt_suffix_from_ass_suffix(config.ai_japanese_ass_suffix),
        _srt_suffix_from_ass_suffix(config.ai_simplified_chinese_ass_suffix),
        _srt_suffix_from_ass_suffix(config.ai_traditional_chinese_ass_suffix),
        ".AI日本語.ja.srt",
        ".AI简日双语.zh.srt",
        ".AI繁日雙語.zh-TW.srt",
        ".AI简体中文.zh.srt",
        ".AI繁體中文.zh-TW.srt",
        ".AI.ja.srt",
        ".AI.zh.srt",
        ".AI.zh-TW.srt",
    ]
    removed: list[Path] = []
    try:
        from subtitle_paths import paths_for_video

        paths = paths_for_video(video, config)
        # Japanese is the expensive, quality-sensitive ASR source of truth.
        # Keep it even when translated intermediates are disabled so a later
        # line repair or republish never has to run Whisper again.
        cache_paths = [paths.zh_cn_srt, paths.zh_tw_srt]
    except Exception:
        cache_paths = []

    for subtitle in cache_paths:
        if not subtitle.exists():
            continue
        try:
            subtitle.unlink(missing_ok=True)
            removed.append(subtitle)
        except OSError:
            continue

    for suffix in dict.fromkeys(suffixes):
        subtitle = video.with_name(f"{video.stem}{suffix}")
        if not subtitle.exists():
            continue
        try:
            subtitle.unlink(missing_ok=True)
            removed.append(subtitle)
        except OSError:
            continue

    try:
        sidecars = list(video.parent.iterdir())
    except OSError:
        sidecars = []
    for subtitle in sidecars:
        if not subtitle.is_file() or subtitle.suffix.casefold() != ".srt":
            continue
        if not subtitle.name.startswith(video.stem) or ".ai" not in subtitle.name.casefold():
            continue
        try:
            subtitle.unlink(missing_ok=True)
            removed.append(subtitle)
        except OSError:
            continue
    return removed


def _subtitle_extract_timeout_seconds(config: AppConfig) -> int:
    try:
        return max(1, int(getattr(config, "subtitle_extract_timeout_seconds", DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS) or DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS


def _probe_subtitle_streams(
    video: Path,
    *,
    timeout_seconds: float = DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS,
    deadline_monotonic: float | None = None,
) -> list[dict[str, Any]]:
    command = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        str(video),
    ]
    command_timeout = _remaining_extract_timeout(timeout_seconds, deadline_monotonic)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=command_timeout,
        )
        _raise_if_extract_deadline_reached(deadline_monotonic)
    except subprocess.TimeoutExpired as exc:
        _raise_if_extract_deadline_reached(deadline_monotonic)
        raise SubtitleExtractError(
            f"ffprobe timed out after {_format_timeout_seconds(command_timeout)}s for {video}"
        ) from exc
    if result.returncode != 0:
        raise SubtitleExtractError(f"ffprobe failed for {video}: {result.stderr.strip() or result.stdout.strip()}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SubtitleExtractError(f"ffprobe returned invalid JSON for {video}") from exc

    return [
        stream
        for stream in payload.get("streams", [])
        if isinstance(stream, dict) and stream.get("codec_type") == "subtitle" and "index" in stream
    ]


def _subtitle_stream_ordinals(streams: list[dict[str, Any]]) -> dict[int, int]:
    ordinals: dict[int, int] = {}
    for ordinal, stream in enumerate(sorted(streams, key=lambda item: int(item.get("index", 9999)))):
        try:
            ordinals[int(stream["index"])] = ordinal
        except (KeyError, TypeError, ValueError):
            continue
    return ordinals


def _extract_subtitle_stream(
    video: Path,
    stream_index: int,
    output: Path,
    codec: str,
    *,
    timeout_seconds: float = DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS,
    subtitle_ordinal: int | None = None,
    deadline_monotonic: float | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    preferred_mkvextract_error = ""
    if _prefer_mkvextract_for_source(video):
        try:
            _extract_subtitle_stream_with_mkvextract(
                video,
                stream_index,
                output,
                codec,
                timeout_seconds=timeout_seconds,
                ffmpeg_error="",
                subtitle_ordinal=subtitle_ordinal,
                deadline_monotonic=deadline_monotonic,
            )
            return
        except SubtitleExtractCancelled:
            raise
        except SubtitleExtractError as exc:
            # ffmpeg remains a compatibility fallback for unusual Matroska
            # tracks, but mkvextract is normally much faster because it does
            # not demux the entire video once per subtitle stream.
            preferred_mkvextract_error = str(exc)

    command = [
        _resolve_ffmpeg(),
        "-y",
        "-i",
        str(video),
        "-map",
        f"0:{stream_index}",
    ]
    if codec in {"ass", "ssa"}:
        command.extend(["-c:s", "copy"])
    else:
        command.extend(["-c:s", "ass"])
    command.append(str(output))

    command_timeout = _remaining_extract_timeout(timeout_seconds, deadline_monotonic)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=command_timeout,
        )
        _raise_if_extract_deadline_reached(deadline_monotonic)
    except subprocess.TimeoutExpired as exc:
        _raise_if_extract_deadline_reached(deadline_monotonic)
        ffmpeg_error = (
            f"ffmpeg timed out after {_format_timeout_seconds(command_timeout)}s "
            f"while extracting subtitle stream {stream_index} from {video}"
        )
        if preferred_mkvextract_error:
            raise SubtitleExtractError(
                f"{ffmpeg_error}; preferred mkvextract attempt failed: {preferred_mkvextract_error}"
            ) from exc
        _extract_subtitle_stream_with_mkvextract(
            video,
            stream_index,
            output,
            codec,
            timeout_seconds=timeout_seconds,
            ffmpeg_error=ffmpeg_error,
            subtitle_ordinal=subtitle_ordinal,
            deadline_monotonic=deadline_monotonic,
        )
        return
    if result.returncode != 0:
        ffmpeg_error = (
            f"ffmpeg failed while extracting subtitle stream {stream_index} from {video}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        if preferred_mkvextract_error:
            raise SubtitleExtractError(
                f"{ffmpeg_error}; preferred mkvextract attempt failed: {preferred_mkvextract_error}"
            )
        _extract_subtitle_stream_with_mkvextract(
            video,
            stream_index,
            output,
            codec,
            timeout_seconds=timeout_seconds,
            ffmpeg_error=ffmpeg_error,
            subtitle_ordinal=subtitle_ordinal,
            deadline_monotonic=deadline_monotonic,
        )
        return
    if not output.exists() or output.stat().st_size == 0:
        ffmpeg_error = f"ffmpeg did not create subtitle file: {output}"
        if preferred_mkvextract_error:
            raise SubtitleExtractError(
                f"{ffmpeg_error}; preferred mkvextract attempt failed: {preferred_mkvextract_error}"
            )
        _extract_subtitle_stream_with_mkvextract(
            video,
            stream_index,
            output,
            codec,
            timeout_seconds=timeout_seconds,
            ffmpeg_error=ffmpeg_error,
            subtitle_ordinal=subtitle_ordinal,
            deadline_monotonic=deadline_monotonic,
        )


def _extract_subtitle_stream_with_mkvextract(
    video: Path,
    stream_index: int,
    output: Path,
    codec: str,
    *,
    timeout_seconds: float,
    ffmpeg_error: str,
    subtitle_ordinal: int | None = None,
    deadline_monotonic: float | None = None,
) -> None:
    _raise_if_extract_deadline_reached(deadline_monotonic)
    error_prefix = f"{ffmpeg_error}; " if ffmpeg_error else ""
    executable = shutil.which("mkvextract")
    if not executable:
        raise SubtitleExtractError(f"{error_prefix}mkvextract unavailable: mkvextract not installed")

    raw_output = output if codec in {"ass", "ssa"} else output.with_suffix(".mkvextract.srt")
    track_ids = _mkvextract_track_id_candidates(
        video,
        stream_index=stream_index,
        subtitle_ordinal=subtitle_ordinal,
        timeout_seconds=timeout_seconds,
        deadline_monotonic=deadline_monotonic,
    )
    errors: list[str] = []
    for track_id in track_ids:
        raw_output.unlink(missing_ok=True)
        command = [executable, "tracks", str(video), f"{track_id}:{raw_output}"]
        command_timeout = _remaining_extract_timeout(timeout_seconds, deadline_monotonic)
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=command_timeout,
            )
            _raise_if_extract_deadline_reached(deadline_monotonic)
        except subprocess.TimeoutExpired:
            _raise_if_extract_deadline_reached(deadline_monotonic)
            errors.append(
                f"track {track_id} timed out after {_format_timeout_seconds(command_timeout)}s"
            )
            continue
        if result.returncode != 0:
            errors.append(
                f"track {track_id}: {result.stderr.strip() or result.stdout.strip() or 'unknown mkvextract error'}"
            )
            continue
        if not raw_output.exists() or raw_output.stat().st_size == 0:
            errors.append(f"track {track_id}: mkvextract did not create subtitle file")
            continue
        if raw_output != output:
            if deadline_monotonic is None:
                _copy_or_convert_sidecar(raw_output, output)
            else:
                _copy_or_convert_sidecar(
                    raw_output,
                    output,
                    timeout_seconds=timeout_seconds,
                    deadline_monotonic=deadline_monotonic,
                )
            raw_output.unlink(missing_ok=True)
        return

    detail = "; ".join(errors[-4:]) if errors else "no mkvextract track ids were available"
    raise SubtitleExtractError(
        f"{error_prefix}mkvextract failed while extracting subtitle stream {stream_index} from {video}: {detail}"
    )


def _prefer_mkvextract_for_source(video: Path) -> bool:
    return video.suffix.casefold() in {".mkv", ".mks", ".webm"}


def _mkvextract_track_id_candidates(
    video: Path,
    *,
    stream_index: int,
    subtitle_ordinal: int | None,
    timeout_seconds: float,
    deadline_monotonic: float | None = None,
) -> list[int]:
    candidates: list[int] = []

    def add(value: Any) -> None:
        try:
            track_id = int(value)
        except (TypeError, ValueError):
            return
        if track_id not in candidates:
            candidates.append(track_id)

    tracks = _mkvmerge_subtitle_tracks(
        video,
        timeout_seconds=timeout_seconds,
        deadline_monotonic=deadline_monotonic,
    )
    if subtitle_ordinal is not None and 0 <= subtitle_ordinal < len(tracks):
        add(tracks[subtitle_ordinal].get("id"))

    for track in tracks:
        properties = track.get("properties") if isinstance(track.get("properties"), dict) else {}
        add_if_number_matches = properties.get("number")
        try:
            if int(add_if_number_matches) == stream_index + 1:
                add(track.get("id"))
        except (TypeError, ValueError):
            pass

    add(stream_index)
    return candidates


def _mkvmerge_subtitle_tracks(
    video: Path,
    *,
    timeout_seconds: float,
    deadline_monotonic: float | None = None,
) -> list[dict[str, Any]]:
    executable = shutil.which("mkvmerge")
    if not executable:
        return []
    command = [executable, "-J", str(video)]
    command_timeout = _remaining_extract_timeout(timeout_seconds, deadline_monotonic)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=command_timeout,
        )
        _raise_if_extract_deadline_reached(deadline_monotonic)
    except subprocess.TimeoutExpired:
        _raise_if_extract_deadline_reached(deadline_monotonic)
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []
    tracks = payload.get("tracks") if isinstance(payload, dict) else None
    if not isinstance(tracks, list):
        return []
    return [
        track
        for track in tracks
        if isinstance(track, dict) and str(track.get("type") or "").lower() in {"subtitle", "subtitles"}
    ]


def _subtitle_output_path(video: Path, language: str) -> Path:
    suffix_by_language = {
        "zh-tw": ".zh-TW.ass",
        "zh-cn": ".zh.ass",
        "ja": ".ja.ass",
        "en": ".English.eng.ass",
    }
    return video.with_name(f"{video.stem}{suffix_by_language[language]}")


def _classify_subtitle_language(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    text = " ".join(str(value) for value in tags.values()).casefold()
    language = str(tags.get("language", "")).casefold()

    if language in {"jpn", "ja", "jp"} or any(keyword in text for keyword in ("日本", "日文", "jpn", "japanese")):
        return "ja"
    if any(keyword in text for keyword in ("繁", "cht", "traditional", "tc", "zh-hant", "zh-tw")):
        return "zh-tw"
    if any(keyword in text for keyword in ("简", "簡", "chs", "simplified", "sc", "zh-hans", "zh-cn")):
        return "zh-cn"
    if language in {"chs", "zh-cn"}:
        return "zh-cn"
    if language in {"chi", "zho", "zh", "cht"}:
        return "zh-tw"
    return None


def _stream_priority(stream: dict[str, Any]) -> tuple[int, int]:
    language = _classify_subtitle_language(stream)
    return (_language_sort_score(language), int(stream.get("index", 9999)))


def _sidecar_priority(language: str | None) -> tuple[int, int]:
    return (_language_sort_score(language), 9999)


def _select_best_subtitle_candidates(candidates: list[_SubtitleCandidate]) -> list[_SubtitleCandidate]:
    best_by_language: dict[str, _SubtitleCandidate] = {}
    for candidate in candidates:
        current = best_by_language.get(candidate.language)
        if current is None or _candidate_quality_key(candidate) > _candidate_quality_key(current):
            best_by_language[candidate.language] = candidate
    return sorted(best_by_language.values(), key=lambda candidate: (_language_sort_score(candidate.language), candidate.priority))


def _language_sort_score(language: str | None) -> int:
    if language is None:
        return 9
    return {"zh-tw": 0, "zh-cn": 1, "ja": 2, "en": 3}.get(language, 9)


def _candidate_quality_key(candidate: _SubtitleCandidate) -> tuple[int, int, int]:
    return (
        candidate.classification.quality_score,
        -candidate.priority[0],
        -candidate.priority[1],
    )


def _classify_extracted_subtitle_language(subtitle: Path, stream: dict[str, Any]) -> str | None:
    return _classify_extracted_subtitle(subtitle, stream).language


def _classify_extracted_subtitle(subtitle: Path, stream: dict[str, Any]) -> SubtitleClassification:
    metadata_language = _classify_subtitle_language(stream)
    return _classify_subtitle_content_detail(_read_subtitle_sample(subtitle), metadata_language=metadata_language)


def _classify_subtitle_content(text: str, *, metadata_language: str | None) -> str | None:
    return _classify_subtitle_content_detail(text, metadata_language=metadata_language).language


def _classify_subtitle_content_detail(text: str, *, metadata_language: str | None) -> SubtitleClassification:
    cleaned = _subtitle_text_for_classification(text)
    if not cleaned:
        return _empty_classification("no_clean_text", metadata_language=metadata_language)

    japanese_score = _japanese_kana_score(cleaned)
    traditional_score, simplified_score = _chinese_script_scores(cleaned)
    cjk_chars = _cjk_char_count(cleaned)
    text_chars = len(cleaned)
    chinese_language = _classify_chinese_text(cleaned)
    chinese_evidence = max(traditional_score, simplified_score)
    if chinese_language is not None and chinese_evidence >= 2:
        return _classification(
            chinese_language,
            "chinese_script_score",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    if (
        metadata_language in {"zh-tw", "zh-cn"}
        and cjk_chars >= 24
        and _cjk_without_kana_line_chars(cleaned) >= 12
    ):
        return _classification(
            metadata_language,
            "metadata_chinese_bilingual_cjk_content",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    if japanese_score > 0 and (
        chinese_evidence == 0
        or (chinese_evidence == 1 and japanese_score >= 2)
    ):
        # A Japanese subtitle naturally contains kanji, including characters
        # that overlap a simplified/traditional discriminator.  Strong kana
        # evidence must win before Chinese script scoring or valid Japanese
        # tracks such as "勉強します" are misrouted as zh-CN.
        return _classification(
            "ja",
            "japanese_kana_strong" if japanese_score >= 8 else "japanese_kana",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    if chinese_language is not None:
        return _classification(
            chinese_language,
            "chinese_script_score",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    if japanese_score > 0:
        return _classification(
            "ja",
            "japanese_kana",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    if metadata_language in {"zh-tw", "zh-cn"} and cjk_chars >= 4:
        return _classification(
            metadata_language,
            "metadata_chinese_with_cjk_content",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    if metadata_language is None and cjk_chars >= 24:
        return _classification(
            "zh-cn",
            "cjk_content_without_kana",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    if metadata_language == "en" and _has_english_latin_content_evidence(cleaned):
        return _classification(
            "en",
            "metadata_english_latin_content",
            metadata_language,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        )
    return _classification(
        None,
        "metadata_ignored_no_content_evidence" if metadata_language else "no_language_evidence",
        metadata_language,
        traditional_score,
        simplified_score,
        japanese_score,
        cjk_chars,
        text_chars,
    )


def _subtitle_text_for_classification(text: str) -> str:
    dialogue_lines: list[str] = []
    fallback_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold()
        if lowered.startswith("dialogue:"):
            payload = line.split(":", 1)[1].lstrip()
            parts = payload.split(",", 9)
            dialogue_lines.append(parts[9] if len(parts) >= 10 else parts[-1])
            continue
        if _is_subtitle_format_line(lowered):
            continue
        fallback_lines.append(line)

    source_lines = dialogue_lines or fallback_lines
    cleaned_lines = [_strip_subtitle_formatting(line) for line in source_lines]
    return "\n".join(line for line in cleaned_lines if line)


def _cjk_without_kana_line_chars(text: str) -> int:
    total = 0
    for line in text.splitlines():
        if _japanese_kana_score(line) == 0:
            total += _cjk_char_count(line)
    return total


def _is_subtitle_format_line(lowered_line: str) -> bool:
    if lowered_line.isdigit():
        return True
    if "-->" in lowered_line:
        return True
    if lowered_line in {"webvtt", "script info"}:
        return True
    if lowered_line.startswith(("[", "format:", "style:", "comment:", "note", "title:", "original script:")):
        return True
    return False


def _strip_subtitle_formatting(text: str) -> str:
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    text = re.sub(r"\{[^{}]*\}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return text.strip()


def _classification(
    language: str | None,
    reason: str,
    metadata_language: str | None,
    traditional_score: int,
    simplified_score: int,
    japanese_score: int,
    cjk_chars: int,
    text_chars: int,
) -> SubtitleClassification:
    return SubtitleClassification(
        language=language,
        reason=reason,
        metadata_language=metadata_language,
        traditional_score=traditional_score,
        simplified_score=simplified_score,
        japanese_score=japanese_score,
        cjk_chars=cjk_chars,
        text_chars=text_chars,
        quality_score=_subtitle_quality_score(
            language,
            reason,
            traditional_score,
            simplified_score,
            japanese_score,
            cjk_chars,
            text_chars,
        ),
    )


def _empty_classification(reason: str, *, metadata_language: str | None = None) -> SubtitleClassification:
    return SubtitleClassification(
        language=None,
        reason=reason,
        metadata_language=metadata_language,
        traditional_score=0,
        simplified_score=0,
        japanese_score=0,
        cjk_chars=0,
        text_chars=0,
        quality_score=0,
    )


def _subtitle_quality_score(
    language: str | None,
    reason: str,
    traditional_score: int,
    simplified_score: int,
    japanese_score: int,
    cjk_chars: int,
    text_chars: int,
) -> int:
    if language is None:
        return 0
    language_evidence = {
        "zh-tw": traditional_score,
        "zh-cn": simplified_score,
        "ja": japanese_score,
    }.get(language, 0)
    score = language_evidence * 1000 + cjk_chars * 8 + japanese_score * 4 + min(text_chars, 20_000) // 20
    if reason.startswith("metadata_"):
        score -= 500
    return max(1, score)


def _append_subtitle_diagnostic(
    diagnostics: list[dict[str, Any]] | None,
    *,
    source: str,
    status: str,
    stream: dict[str, Any] | None = None,
    path: Path | None = None,
    codec: str | None = None,
    classification: SubtitleClassification | None = None,
    detail: str = "",
) -> None:
    if diagnostics is None:
        return
    payload: dict[str, Any] = {
        "source": source,
        "status": status,
    }
    if stream is not None:
        payload["stream_index"] = int(stream.get("index", -1))
        payload["codec"] = codec or str(stream.get("codec_name", "")).lower()
        payload["kind"] = _subtitle_codec_kind(payload["codec"])
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        if tags:
            payload["metadata_language"] = tags.get("language")
            payload["title"] = tags.get("title")
    if path is not None:
        payload["path"] = str(path)
        payload["codec"] = path.suffix.lower().lstrip(".")
        payload["kind"] = "text"
    if classification is not None:
        payload["classification"] = classification.as_dict()
    if detail:
        payload["detail"] = detail[:1000]
    diagnostics.append(payload)


def _subtitle_codec_kind(codec: str) -> str:
    normalized = codec.lower()
    if normalized in TEXT_SUBTITLE_CODECS:
        return "text"
    if normalized in IMAGE_SUBTITLE_CODECS:
        return "image"
    return "unsupported"


def _iter_sidecar_subtitles(video: Path, *, extra_sidecar_paths: list[str | Path] | None = None) -> list[Path]:
    candidates = [
        path
        for path in video.parent.glob(f"{video.stem}.*")
        if path.is_file() and path.suffix.lower() in SIDECAR_SUBTITLE_EXTENSIONS
    ]
    for raw_path in extra_sidecar_paths or []:
        path = Path(raw_path)
        if path.is_file() and path.suffix.lower() in SIDECAR_SUBTITLE_EXTENSIONS:
            candidates.append(path)

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def is_configured_ai_source_transcript_sidecar(
    video: Path,
    subtitle: Path,
    config: AppConfig,
) -> bool:
    """Reject every sidecar name reserved by the configurable AI template."""

    template = str(
        getattr(
            config,
            "ai_source_transcript_ass_suffix_template",
            ".AI{label}.{language}.ass",
        )
    )
    if "{language}" not in template:
        return False
    suffix_expression = re.escape(template)
    suffix_expression = suffix_expression.replace(re.escape("{label}"), r".+?")
    suffix_expression = suffix_expression.replace(
        re.escape("{language}"),
        r"[A-Za-z0-9_-]+",
    )
    return (
        re.fullmatch(
            re.escape(video.stem) + suffix_expression,
            subtitle.name,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _copy_or_convert_sidecar(
    source: Path,
    output: Path,
    *,
    timeout_seconds: float = DEFAULT_SUBTITLE_EXTRACT_TIMEOUT_SECONDS,
    deadline_monotonic: float | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _raise_if_extract_deadline_reached(deadline_monotonic)
    if source.suffix.lower() in {".ass", ".ssa"}:
        shutil.copy2(source, output)
        _raise_if_extract_deadline_reached(deadline_monotonic)
        return

    command = [_resolve_ffmpeg(), "-y", "-i", str(source), str(output)]
    command_timeout = _remaining_extract_timeout(timeout_seconds, deadline_monotonic)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=command_timeout,
        )
        _raise_if_extract_deadline_reached(deadline_monotonic)
    except subprocess.TimeoutExpired as exc:
        _raise_if_extract_deadline_reached(deadline_monotonic)
        raise SubtitleExtractError(
            f"ffmpeg timed out after {_format_timeout_seconds(command_timeout)}s "
            f"while converting sidecar subtitle {source}"
        ) from exc
    if result.returncode != 0:
        raise SubtitleExtractError(
            f"ffmpeg failed while converting sidecar subtitle {source}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _remove_source_sidecar_if_final_ass_mode(source: Path, output: Path, output_video: Path, config: AppConfig) -> None:
    if not getattr(config, "export_ai_ass", False) or getattr(config, "keep_intermediate_files", True):
        return
    if ".ai" not in source.name.casefold():
        return
    if source.suffix.lower() not in {".srt", ".vtt"} or output.suffix.lower() != ".ass":
        return
    try:
        if source.parent.resolve() != output_video.parent.resolve():
            return
        if source.resolve() == output.resolve():
            return
        source.unlink(missing_ok=True)
    except OSError:
        return


def _srt_suffix_from_ass_suffix(suffix: str) -> str:
    if suffix.casefold().endswith(".ass"):
        return f"{suffix[:-4]}.srt"
    if suffix.casefold().endswith(".srt"):
        return suffix
    return f"{suffix}.srt"


def _has_traditional_marker(text: str) -> bool:
    return any(marker in text for marker in ("繁體", "繁体", "繁中", "繁日", "简繁", "簡繁", "traditional", "cht", "tc", "zh-hant", "zh-tw"))


def _has_simplified_marker(text: str) -> bool:
    return any(marker in text for marker in ("简体", "簡體", "简中", "簡中", "简日", "簡日", "simplified", "chs", "sc", "zh-hans", "zh-cn"))


def _has_english_sidecar_metadata(lowered_name: str) -> bool:
    return _ENGLISH_SIDECAR_METADATA_RE.search(lowered_name) is not None


def _has_english_latin_content_evidence(text: str) -> bool:
    words = [match.group(0).casefold() for match in _ENGLISH_WORD_RE.finditer(text)]
    if len(words) < 12:
        return False

    latin_chars = sum(char.isascii() and char.isalpha() for char in text)
    alphabetic_chars = sum(char.isalpha() for char in text)
    if latin_chars < 48 or alphabetic_chars <= 0:
        return False
    if latin_chars / alphabetic_chars < 0.85:
        return False

    evidence = [word for word in words if word in _ENGLISH_EVIDENCE_WORDS]
    return len(evidence) >= 3 and len(set(evidence)) >= 2


def _is_ai_generated_sidecar_name(lowered_name: str) -> bool:
    return any(
        marker in lowered_name
        for marker in (
            ".ai.",
            ".ai_",
            ".ai-",
            ".ai日",
            ".ai简",
            ".ai簡",
            ".ai繁",
            ".ai.zh",
            ".ai.ja",
            ".aienglish",
            ".ai原語言",
            ".ai原语言",
            ".ai中文",
        )
    )


def _read_subtitle_sample(path: Path, max_chars: int = 30000) -> str:
    # Read only the classification window. ``Path.read_bytes()[:limit]`` reads
    # the complete file before slicing and is catastrophic if a caller ever
    # passes a video or another large non-subtitle file by mistake.
    with path.open("rb") as handle:
        raw = handle.read(max_chars * 8)
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "cp950"):
        try:
            return raw.decode(encoding)[:max_chars]
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")[:max_chars]


def _classify_chinese_text(text: str) -> str | None:
    traditional_score, simplified_score = _chinese_script_scores(text)
    if traditional_score == 0 and simplified_score == 0:
        return None
    if traditional_score == simplified_score:
        return None
    return "zh-tw" if traditional_score > simplified_score else "zh-cn"


def _chinese_script_scores(text: str) -> tuple[int, int]:
    traditional_only = set(
        "選麼為個這裡對時會說們來讓還後著與學國臺聲點體關開間見經過無發將樣話長"
        "實現問題電腦畫隻雲風龍門聽頭買賣車書氣壓"
    )
    simplified_only = set(
        "选么为个这里对时会说们来让还后着与学国台声点体关开间见经过无发将样话长"
        "实现问题电脑画只云风龙门听头买卖车书气压"
    )
    return (
        sum(1 for char in text if char in traditional_only),
        sum(1 for char in text if char in simplified_only),
    )


def _japanese_kana_score(text: str) -> int:
    return sum(1 for char in text if "\u3040" <= char <= "\u30ff")


def _cjk_char_count(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def _contains_cjk(text: str) -> bool:
    return _cjk_char_count(text) > 0


def _resolve_ffprobe() -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    return "ffprobe"


def _resolve_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    return "ffmpeg"
