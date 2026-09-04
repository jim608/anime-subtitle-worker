from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
import uuid


SOURCE_INPUT_IDENTITY_VERSION = "source-input-identity-v1"
SOURCE_INVENTORY_VERSION = "source-inventory-v1"
MATERIALIZED_SUBTITLE_CACHE_VERSION = "materialized-subtitle-v1"
SIDECAR_SUBTITLE_EXTENSIONS = frozenset({".ass", ".ssa", ".srt", ".vtt"})
TEXT_SUBTITLE_CODECS = frozenset({"ass", "ssa", "subrip", "srt", "webvtt", "mov_text"})
DEFAULT_PROBE_TIMEOUT_SECONDS = 60.0
DEFAULT_EXTRACT_TIMEOUT_SECONDS = 300.0
MAX_SUBTITLE_BYTES = 32 * 1024 * 1024
MAX_SAMPLE_CHARACTERS = 30_000

_AI_SIDECAR_MARKERS = (
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
_STABLE_MEDIA_JOB_KEYS = (
    "job_id",
    "media_fingerprint",
    "media_revision",
    "identity_kind",
    "canonical_path",
    "media_size",
    "media_mtime_ns",
)
_ASS_TIMESTAMP_RE = re.compile(r"^(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d{1,2}(?:\.\d+)?)$")
_TIMELINE_RE = re.compile(
    r"(?P<start>(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>(?:\d{1,3}:)?\d{2}:\d{2}[,.]\d{1,3})"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


class SourceInventoryError(RuntimeError):
    """Base error for fail-closed source inventory operations."""


class SourceProbeError(SourceInventoryError):
    """Raised when the single metadata probe is incomplete or invalid."""


class SourceChangedError(SourceInventoryError):
    """Raised when an input changes while its identity or inventory is built."""


@dataclass(frozen=True)
class SidecarIdentity:
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceInputIdentity:
    media_job_identity: Mapping[str, Any]
    sidecars: tuple[SidecarIdentity, ...]
    fingerprint: str
    schema_version: str = SOURCE_INPUT_IDENTITY_VERSION

    @property
    def candidate_fingerprint(self) -> str:
        """Cheap pre-analysis fingerprint suitable for checkpoint lookup."""

        return self.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "media_job_identity": _json_safe(self.media_job_identity),
            "sidecars": [item.to_dict() for item in self.sidecars],
        }


@dataclass(frozen=True)
class SubtitleInventoryCandidate:
    track_index: int
    codec: str
    container_language_tag: str
    title: str
    default: bool
    forced: bool
    hearing_impaired: bool | None
    event_count: int
    first_timestamp_seconds: float | None
    last_timestamp_seconds: float | None
    valid_timing_count: int | None
    empty_event_count: int | None
    sample_text: str
    extraction_error: str
    content_sha256: str
    source_size: int | None
    source_mtime_ns: int | None
    source_sha256: str
    source_kind: str
    source_reference: str

    def to_analyzer_dict(self) -> dict[str, Any]:
        return {
            "track_index": self.track_index,
            "codec": self.codec,
            "container_language_tag": self.container_language_tag,
            "title": self.title,
            "default": self.default,
            "forced": self.forced,
            "hearing_impaired": self.hearing_impaired,
            "event_count": self.event_count,
            "first_timestamp_seconds": self.first_timestamp_seconds,
            "last_timestamp_seconds": self.last_timestamp_seconds,
            "valid_timing_count": self.valid_timing_count,
            "empty_event_count": self.empty_event_count,
            "sample_text": self.sample_text,
            "extraction_error": self.extraction_error,
            "content_sha256": self.content_sha256,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "source_sha256": self.source_sha256,
            "source_kind": self.source_kind,
            "source_reference": self.source_reference,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_analyzer_dict()


@dataclass(frozen=True)
class AudioInventoryCandidate:
    track_index: int
    codec: str
    container_language_tag: str
    title: str
    default: bool
    commentary: bool
    channels: int | None
    sample_rate: int | None
    duration_seconds: float | None
    detected_language: str
    language_confidence: float
    detection_source: str
    probing_error: str
    source_kind: str
    source_reference: str

    def to_analyzer_dict(self) -> dict[str, Any]:
        return {
            "track_index": self.track_index,
            "codec": self.codec,
            "container_language_tag": self.container_language_tag,
            "title": self.title,
            "default": self.default,
            "commentary": self.commentary,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "detected_language": self.detected_language,
            "language_confidence": self.language_confidence,
            "detection_source": self.detection_source,
            "probing_error": self.probing_error,
            "source_kind": self.source_kind,
            "source_reference": self.source_reference,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.to_analyzer_dict()


@dataclass(frozen=True)
class SourceInventory:
    input_identity: SourceInputIdentity
    media_duration_seconds: float | None
    subtitle_candidates: tuple[SubtitleInventoryCandidate, ...]
    audio_candidates: tuple[AudioInventoryCandidate, ...]
    subtitle_inventory_complete: bool
    audio_inventory_complete: bool
    probing_errors: tuple[str, ...]
    inventory_fingerprint: str
    schema_version: str = SOURCE_INVENTORY_VERSION

    @property
    def candidate_fingerprint(self) -> str:
        return self.input_identity.candidate_fingerprint

    def analyzer_arguments(self) -> dict[str, Any]:
        return {
            "subtitle_candidates": [item.to_analyzer_dict() for item in self.subtitle_candidates],
            "audio_candidates": [item.to_analyzer_dict() for item in self.audio_candidates],
            "media_duration_seconds": self.media_duration_seconds,
            "subtitle_inventory_complete": self.subtitle_inventory_complete,
            "audio_inventory_complete": self.audio_inventory_complete,
        }

    def to_analyzer_arguments(self) -> dict[str, Any]:
        return self.analyzer_arguments()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_identity": self.input_identity.to_dict(),
            "candidate_fingerprint": self.candidate_fingerprint,
            "inventory_fingerprint": self.inventory_fingerprint,
            "media_duration_seconds": self.media_duration_seconds,
            "subtitle_inventory_complete": self.subtitle_inventory_complete,
            "audio_inventory_complete": self.audio_inventory_complete,
            "probing_errors": list(self.probing_errors),
            "subtitle_candidates": [item.to_dict() for item in self.subtitle_candidates],
            "audio_candidates": [item.to_dict() for item in self.audio_candidates],
        }


@dataclass(frozen=True)
class _SubtitleMetrics:
    event_count: int
    first_timestamp_seconds: float | None
    last_timestamp_seconds: float | None
    valid_timing_count: int
    empty_event_count: int
    sample_text: str
    content_sha256: str


def discover_source_sidecars(
    video_path: str | Path,
    *,
    config: object | None = None,
    sidecar_paths: Iterable[str | Path] | None = None,
    generated_sidecar_suffixes: Iterable[str] = (),
) -> tuple[Path, ...]:
    """Return stable, deduplicated source sidecars without generated outputs."""

    video = Path(video_path)
    generated_suffixes = tuple(str(item) for item in generated_sidecar_suffixes)
    if sidecar_paths is None:
        try:
            supplied = tuple(video.parent.iterdir())
        except OSError as exc:
            raise SourceInventoryError(f"cannot list source sidecars: {exc}") from exc
    else:
        supplied = tuple(Path(item) for item in sidecar_paths)

    prefix = f"{video.stem}.".casefold()
    unique: dict[str, Path] = {}
    for candidate in supplied:
        path = Path(candidate)
        if not path.is_file():
            if sidecar_paths is not None:
                raise SourceInventoryError(f"explicit source sidecar is not a file: {path.name}")
            continue
        if not path.name.casefold().startswith(prefix):
            continue
        if path.suffix.casefold() not in SIDECAR_SUBTITLE_EXTENSIONS:
            continue
        try:
            direct_sibling = path.resolve().parent == video.parent.resolve()
        except OSError as exc:
            if sidecar_paths is not None:
                raise SourceInventoryError(f"cannot resolve explicit source sidecar: {path.name}") from exc
            continue
        if not direct_sibling:
            if sidecar_paths is not None:
                raise SourceInventoryError("explicit source sidecar must be in the media directory")
            continue
        if _is_generated_sidecar(
            video,
            path,
            config=config,
            generated_sidecar_suffixes=generated_suffixes,
        ):
            continue
        relative = _relative_sidecar_path(video, path)
        try:
            identity_key = os.path.normcase(str(path.resolve()))
        except OSError:
            identity_key = os.path.normcase(str(path.absolute()))
        unique.setdefault(identity_key, path)
    return tuple(
        path
        for _key, path in sorted(
            unique.items(),
            key=lambda item: (
                _relative_sidecar_path(video, item[1]).casefold(),
                _relative_sidecar_path(video, item[1]),
            ),
        )
    )


def build_source_input_identity(
    video_path: str | Path,
    media_job_identity: Mapping[str, Any] | str,
    *,
    config: object | None = None,
    sidecar_paths: Iterable[str | Path] | None = None,
    generated_sidecar_suffixes: Iterable[str] = (),
) -> SourceInputIdentity:
    """Build a cheap identity without probing media or extracting streams."""

    video = Path(video_path)
    generated_suffixes = tuple(str(item) for item in generated_sidecar_suffixes)
    source_stat = _source_signature(video)
    normalized_job = _normalize_media_job_identity(media_job_identity, video, source_stat)
    sidecars = discover_source_sidecars(
        video,
        config=config,
        sidecar_paths=sidecar_paths,
        generated_sidecar_suffixes=generated_suffixes,
    )
    identities = tuple(_sidecar_identity(video, path) for path in sidecars)
    _assert_source_unchanged(video, source_stat)
    payload = {
        "schema_version": SOURCE_INPUT_IDENTITY_VERSION,
        "media_job_identity": normalized_job,
        "sidecars": [item.to_dict() for item in identities],
    }
    return SourceInputIdentity(
        media_job_identity=normalized_job,
        sidecars=identities,
        fingerprint=_canonical_sha256(payload),
    )


def inventory_sources(
    video_path: str | Path,
    media_job_identity: Mapping[str, Any] | str,
    *,
    config: object | None = None,
    sidecar_paths: Iterable[str | Path] | None = None,
    generated_sidecar_suffixes: Iterable[str] = (),
    ffprobe_path: str | None = None,
    ffmpeg_path: str | None = None,
    temp_root: str | Path | None = None,
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    extract_timeout_seconds: float = DEFAULT_EXTRACT_TIMEOUT_SECONDS,
) -> SourceInventory:
    """Inventory sidecars and container streams using metadata and temporary files."""

    video = Path(video_path)
    generated_suffixes = tuple(str(item) for item in generated_sidecar_suffixes)
    requested_sidecars = None if sidecar_paths is None else tuple(Path(item) for item in sidecar_paths)
    source_stat = _source_signature(video)
    resolved_sidecars = discover_source_sidecars(
        video,
        config=config,
        sidecar_paths=requested_sidecars,
        generated_sidecar_suffixes=generated_suffixes,
    )
    identity = build_source_input_identity(
        video,
        media_job_identity,
        config=config,
        sidecar_paths=resolved_sidecars,
        generated_sidecar_suffixes=generated_suffixes,
    )
    try:
        probe = _probe_media(
            video,
            ffprobe_path=ffprobe_path,
            timeout_seconds=probe_timeout_seconds,
        )
    except SourceProbeError as exc:
        _assert_source_unchanged(video, source_stat)
        _assert_identity_unchanged(
            identity,
            video,
            media_job_identity,
            config=config,
            sidecar_paths=requested_sidecars,
            generated_sidecar_suffixes=generated_suffixes,
        )
        return _make_inventory(
            identity,
            media_duration_seconds=None,
            subtitle_candidates=(),
            audio_candidates=(),
            subtitle_inventory_complete=False,
            audio_inventory_complete=False,
            probing_errors=(str(exc),),
        )

    streams = probe["streams"]
    media_duration = _positive_float(probe.get("format", {}).get("duration"))
    errors: list[str] = []
    subtitle_complete = True
    audio_complete = True
    if media_duration is None:
        errors.append("media_duration_unavailable")

    subtitles: list[SubtitleInventoryCandidate] = []
    sidecar_identity_by_reference = {
        item.relative_path: item for item in identity.sidecars
    }
    for ordinal, sidecar in enumerate(resolved_sidecars, start=1):
        reference = _relative_sidecar_path(video, sidecar)
        sidecar_identity = sidecar_identity_by_reference[reference]
        try:
            metrics = _subtitle_metrics(sidecar)
            error = ""
        except SourceInventoryError as exc:
            metrics = None
            error = _bounded_error("sidecar_read_failed", exc)
            subtitle_complete = False
            errors.append(error)
        subtitles.append(
            _subtitle_candidate(
                track_index=-ordinal,
                codec=sidecar.suffix.casefold().lstrip("."),
                language=_language_from_sidecar_name(sidecar.name),
                title=sidecar.name,
                metrics=metrics,
                extraction_error=error,
                source_kind="sidecar",
                source_reference=reference,
                source_size=sidecar_identity.size,
                source_mtime_ns=sidecar_identity.mtime_ns,
                source_sha256=sidecar_identity.sha256,
            )
        )

    embedded = sorted(
        (item for item in streams if str(item.get("codec_type", "")).casefold() == "subtitle"),
        key=_stream_index_sort_key,
    )
    audio_streams = sorted(
        (item for item in streams if str(item.get("codec_type", "")).casefold() == "audio"),
        key=_stream_index_sort_key,
    )
    root = None if temp_root is None else Path(temp_root)
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="source-inventory-", dir=None if root is None else str(root)) as raw_temp:
        temp_dir = Path(raw_temp)
        for stream in embedded:
            try:
                index = _required_stream_index(stream)
            except SourceProbeError as exc:
                subtitle_complete = False
                errors.append(_bounded_error("subtitle_stream_invalid", exc))
                continue
            codec = str(stream.get("codec_name", "") or "").strip().casefold()
            metrics: _SubtitleMetrics | None = None
            error = ""
            if codec not in TEXT_SUBTITLE_CODECS:
                error = f"unsupported_subtitle_codec:{codec or 'unknown'}"
            else:
                output = temp_dir / f"subtitle-{index}.ass"
                try:
                    _extract_embedded_subtitle(
                        video,
                        index,
                        output,
                        ffmpeg_path=ffmpeg_path,
                        timeout_seconds=extract_timeout_seconds,
                    )
                    metrics = _subtitle_metrics(output)
                except SourceInventoryError as exc:
                    error = _bounded_error("subtitle_extract_failed", exc)
                    subtitle_complete = False
                    errors.append(error)
            tags = _stream_tags(stream)
            disposition = _stream_disposition(stream)
            subtitles.append(
                _subtitle_candidate(
                    track_index=index,
                    codec=codec,
                    language=str(tags.get("language", "") or ""),
                    title=str(tags.get("title", "") or ""),
                    metrics=metrics,
                    extraction_error=error,
                    source_kind="embedded",
                    source_reference=f"stream:{index}",
                    default=_flag(disposition.get("default")),
                    forced=_flag(disposition.get("forced")),
                    hearing_impaired=_hearing_impaired_flag(disposition),
                )
            )

    audios: list[AudioInventoryCandidate] = []
    for stream in audio_streams:
        try:
            index = _required_stream_index(stream)
        except SourceProbeError as exc:
            audio_complete = False
            errors.append(_bounded_error("audio_stream_invalid", exc))
            continue
        tags = _stream_tags(stream)
        disposition = _stream_disposition(stream)
        duration = _stream_duration(stream, tags)
        probing_error = ""
        if duration is None or media_duration is None:
            probing_error = "audio_duration_unavailable"
            audio_complete = False
            errors.append(f"{probing_error}:stream:{index}")
        audios.append(
            AudioInventoryCandidate(
                track_index=index,
                codec=str(stream.get("codec_name", "") or "").strip().casefold(),
                container_language_tag=str(tags.get("language", "") or ""),
                title=str(tags.get("title", "") or ""),
                default=_flag(disposition.get("default")),
                commentary=_flag(disposition.get("comment")) or _flag(disposition.get("commentary")),
                channels=_optional_int(stream.get("channels")),
                sample_rate=_optional_int(stream.get("sample_rate")),
                duration_seconds=duration,
                detected_language="",
                language_confidence=0.0,
                detection_source="container_metadata_only",
                probing_error=probing_error,
                source_kind="embedded",
                source_reference=f"stream:{index}",
            )
        )

    if media_duration is None:
        subtitle_complete = False
        audio_complete = False
    _assert_source_unchanged(video, source_stat)
    _assert_identity_unchanged(
        identity,
        video,
        media_job_identity,
        config=config,
        sidecar_paths=requested_sidecars,
        generated_sidecar_suffixes=generated_suffixes,
    )
    return _make_inventory(
        identity,
        media_duration_seconds=media_duration,
        subtitle_candidates=tuple(sorted(subtitles, key=_subtitle_candidate_sort_key)),
        audio_candidates=tuple(sorted(audios, key=lambda item: item.track_index)),
        subtitle_inventory_complete=subtitle_complete,
        audio_inventory_complete=audio_complete,
        probing_errors=tuple(dict.fromkeys(errors)),
    )


def _assert_identity_unchanged(
    expected: SourceInputIdentity,
    video: Path,
    media_job_identity: Mapping[str, Any] | str,
    *,
    config: object | None,
    sidecar_paths: Iterable[str | Path] | None,
    generated_sidecar_suffixes: Iterable[str],
) -> None:
    current = build_source_input_identity(
        video,
        media_job_identity,
        config=config,
        sidecar_paths=sidecar_paths,
        generated_sidecar_suffixes=generated_sidecar_suffixes,
    )
    if current.fingerprint != expected.fingerprint:
        raise SourceChangedError("source sidecar identity changed during inventory")


def materialize_selected_subtitle(
    video_path: str | Path,
    selected_candidate: Mapping[str, Any] | SubtitleInventoryCandidate | object,
    media_job_identity: Mapping[str, Any] | str,
    config: object | None,
    *,
    expected_candidate_fingerprint: str | None = None,
    work_path: str | Path | None = None,
    ffmpeg_path: str | None = None,
    extract_timeout_seconds: float = DEFAULT_EXTRACT_TIMEOUT_SECONDS,
) -> Path:
    """Resolve a selected sidecar or atomically cache one selected embedded track."""

    video = Path(video_path)
    source_signature = _source_signature(video)
    candidate = _candidate_mapping(selected_candidate)
    source_kind = str(candidate.get("source_kind", "") or "").strip().casefold()
    source_reference = str(candidate.get("source_reference", "") or "").strip()
    expected_content = str(candidate.get("content_sha256", "") or "").strip().casefold()
    if not _valid_sha256(expected_content):
        raise SourceInventoryError("selected subtitle is missing a valid content_sha256")

    current_identity = build_source_input_identity(video, media_job_identity, config=config)
    expected_fingerprint = str(
        expected_candidate_fingerprint or candidate.get("candidate_fingerprint", "") or ""
    ).strip().casefold()
    if expected_fingerprint and (
        not _valid_sha256(expected_fingerprint)
        or current_identity.candidate_fingerprint.casefold() != expected_fingerprint
    ):
        raise SourceChangedError("source candidate fingerprint changed before materialization")

    if source_kind == "sidecar":
        materialized = _materialize_sidecar_candidate(
            video,
            candidate,
            source_reference=source_reference,
            expected_content_sha256=expected_content,
            config=config,
        )
    elif source_kind == "embedded":
        materialized = _materialize_embedded_candidate(
            video,
            candidate,
            media_job_identity=current_identity.media_job_identity,
            source_reference=source_reference,
            expected_content_sha256=expected_content,
            config=config,
            work_path=work_path,
            ffmpeg_path=ffmpeg_path,
            extract_timeout_seconds=extract_timeout_seconds,
            source_signature=source_signature,
        )
    else:
        raise SourceInventoryError(f"unsupported selected subtitle source_kind: {source_kind or 'missing'}")

    _assert_source_unchanged(video, source_signature)
    final_identity = build_source_input_identity(video, media_job_identity, config=config)
    if final_identity.fingerprint != current_identity.fingerprint:
        raise SourceChangedError("source identity changed during subtitle materialization")
    return materialized


def _materialize_sidecar_candidate(
    video: Path,
    candidate: Mapping[str, Any],
    *,
    source_reference: str,
    expected_content_sha256: str,
    config: object | None,
) -> Path:
    relative = Path(source_reference)
    if (
        not source_reference
        or relative.is_absolute()
        or len(relative.parts) != 1
        or relative.name != source_reference
        or relative.name in {".", ".."}
    ):
        raise SourceInventoryError("selected sidecar reference must be one relative filename")
    path = video.parent / relative.name
    try:
        if path.resolve().parent != video.parent.resolve():
            raise SourceInventoryError("selected sidecar resolves outside the media directory")
    except OSError as exc:
        raise SourceInventoryError("cannot resolve selected sidecar") from exc
    if not path.is_file() or path.suffix.casefold() not in SIDECAR_SUBTITLE_EXTENSIONS:
        raise SourceInventoryError("selected sidecar is missing or unsupported")
    if _is_generated_sidecar(video, path, config=config, generated_sidecar_suffixes=()):
        raise SourceInventoryError("selected sidecar is a generated output")

    expected_size = _required_nonnegative_int(candidate.get("source_size"), "source_size")
    expected_mtime = _required_nonnegative_int(candidate.get("source_mtime_ns"), "source_mtime_ns")
    expected_raw_sha256 = str(candidate.get("source_sha256", "") or "").strip().casefold()
    if not _valid_sha256(expected_raw_sha256):
        raise SourceInventoryError("selected sidecar is missing a valid source_sha256")
    before = _file_signature(path)
    if before[2] != expected_size or before[3] != expected_mtime:
        raise SourceChangedError("selected sidecar size or mtime changed")
    raw_identity = _sidecar_identity(video, path)
    if raw_identity.sha256.casefold() != expected_raw_sha256:
        raise SourceChangedError("selected sidecar bytes changed")
    metrics = _subtitle_metrics(path)
    if metrics.event_count <= 0 or metrics.content_sha256.casefold() != expected_content_sha256:
        raise SourceChangedError("selected sidecar subtitle content changed")
    final_raw_identity = _sidecar_identity(video, path)
    if (
        final_raw_identity.size != expected_size
        or final_raw_identity.mtime_ns != expected_mtime
        or final_raw_identity.sha256.casefold() != expected_raw_sha256
    ):
        raise SourceChangedError("selected sidecar changed during validation")
    return path


def _materialize_embedded_candidate(
    video: Path,
    candidate: Mapping[str, Any],
    *,
    media_job_identity: Mapping[str, Any],
    source_reference: str,
    expected_content_sha256: str,
    config: object | None,
    work_path: str | Path | None,
    ffmpeg_path: str | None,
    extract_timeout_seconds: float,
    source_signature: tuple[int, int, int, int],
) -> Path:
    match = re.fullmatch(r"stream:(\d+)", source_reference)
    if match is None:
        raise SourceInventoryError("selected embedded subtitle has an invalid stream reference")
    stream_index = int(match.group(1))
    persisted_index = _required_nonnegative_int(
        candidate.get("track_index", candidate.get("index")),
        "track_index",
    )
    if stream_index != persisted_index:
        raise SourceInventoryError("selected embedded subtitle stream index is inconsistent")

    root_value = work_path if work_path is not None else getattr(config, "work_path", None)
    if root_value is None or not str(root_value).strip():
        raise SourceInventoryError("work_path is required to materialize an embedded subtitle")
    root = Path(root_value)
    cache_identity = {
        "cache_version": MATERIALIZED_SUBTITLE_CACHE_VERSION,
        "media_job_identity": media_job_identity,
        "stream_index": stream_index,
        "content_sha256": expected_content_sha256,
    }
    cache_key = _canonical_sha256(cache_identity)
    cache_dir = root / "source_inventory_cache" / cache_key[:24]
    cache_path = cache_dir / f"subtitle-stream-{stream_index}-{expected_content_sha256[:16]}.ass"
    _ensure_cache_directory(root, cache_dir)
    if _valid_materialized_subtitle(cache_path, expected_content_sha256):
        return cache_path

    partial = cache_dir / f".{cache_path.stem}.{os.getpid()}.{uuid.uuid4().hex}.partial.ass"
    try:
        _extract_embedded_subtitle(
            video,
            stream_index,
            partial,
            ffmpeg_path=ffmpeg_path,
            timeout_seconds=extract_timeout_seconds,
        )
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise SourceInventoryError("selected embedded subtitle extraction is empty")
        metrics = _subtitle_metrics(partial)
        if metrics.event_count <= 0:
            raise SourceInventoryError("selected embedded subtitle has no events")
        if metrics.content_sha256.casefold() != expected_content_sha256:
            raise SourceChangedError("selected embedded subtitle content no longer matches the decision")
        _assert_source_unchanged(video, source_signature)
        with partial.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _assert_cache_directory_safe(root, cache_dir)
        os.replace(partial, cache_path)
        _fsync_directory(cache_dir)
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
    if not _valid_materialized_subtitle(cache_path, expected_content_sha256):
        raise SourceInventoryError("materialized subtitle cache failed validation")
    return cache_path


def _valid_materialized_subtitle(path: Path, expected_content_sha256: str) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
            return False
        metrics = _subtitle_metrics(path)
    except (OSError, SourceInventoryError):
        return False
    return metrics.event_count > 0 and metrics.content_sha256.casefold() == expected_content_sha256


def _ensure_cache_directory(root: Path, cache_dir: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    cache_root = root / "source_inventory_cache"
    for path in (cache_root, cache_dir):
        if path.exists() and _is_reparse_path(path):
            raise SourceInventoryError("materialized subtitle cache contains a reparse path")
        path.mkdir(parents=False, exist_ok=True)
        if _is_reparse_path(path):
            raise SourceInventoryError("materialized subtitle cache became a reparse path")
    _assert_cache_directory_safe(root, cache_dir)


def _assert_cache_directory_safe(root: Path, cache_dir: Path) -> None:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_cache = cache_dir.resolve(strict=True)
    except OSError as exc:
        raise SourceInventoryError("cannot resolve materialized subtitle cache") from exc
    if not resolved_cache.is_relative_to(resolved_root):
        raise SourceInventoryError("materialized subtitle cache resolves outside work_path")
    cache_root = root / "source_inventory_cache"
    if _is_reparse_path(cache_root) or _is_reparse_path(cache_dir):
        raise SourceInventoryError("materialized subtitle cache contains a reparse path")


def _is_reparse_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            return bool(is_junction())
        except OSError:
            return True
    return False


def _candidate_mapping(value: Mapping[str, Any] | SubtitleInventoryCandidate | object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return {str(key): item for key, item in payload.items()}
    raise TypeError("selected_candidate must be a mapping or expose to_dict()")


def _make_inventory(
    identity: SourceInputIdentity,
    *,
    media_duration_seconds: float | None,
    subtitle_candidates: Sequence[SubtitleInventoryCandidate],
    audio_candidates: Sequence[AudioInventoryCandidate],
    subtitle_inventory_complete: bool,
    audio_inventory_complete: bool,
    probing_errors: Sequence[str],
) -> SourceInventory:
    inventory_payload = {
        "schema_version": SOURCE_INVENTORY_VERSION,
        "candidate_fingerprint": identity.candidate_fingerprint,
        "media_duration_seconds": media_duration_seconds,
        "subtitle_inventory_complete": bool(subtitle_inventory_complete),
        "audio_inventory_complete": bool(audio_inventory_complete),
        "probing_errors": list(probing_errors),
        "subtitle_candidates": [item.to_dict() for item in subtitle_candidates],
        "audio_candidates": [item.to_dict() for item in audio_candidates],
    }
    return SourceInventory(
        input_identity=identity,
        media_duration_seconds=media_duration_seconds,
        subtitle_candidates=tuple(subtitle_candidates),
        audio_candidates=tuple(audio_candidates),
        subtitle_inventory_complete=bool(subtitle_inventory_complete),
        audio_inventory_complete=bool(audio_inventory_complete),
        probing_errors=tuple(probing_errors),
        inventory_fingerprint=_canonical_sha256(inventory_payload),
    )


def _probe_media(video: Path, *, ffprobe_path: str | None, timeout_seconds: float) -> dict[str, Any]:
    executable = ffprobe_path or shutil.which("ffprobe") or "ffprobe"
    command = [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(video),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(0.001, float(timeout_seconds)),
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceProbeError(_bounded_error("ffprobe_timeout", exc)) from exc
    except OSError as exc:
        raise SourceProbeError(_bounded_error("ffprobe_unavailable", exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise SourceProbeError(f"ffprobe_failed:{_bounded_text(detail, 500)}")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SourceProbeError("ffprobe_invalid_json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise SourceProbeError("ffprobe_incomplete_payload")
    if any(not isinstance(stream, dict) for stream in payload["streams"]):
        raise SourceProbeError("ffprobe_invalid_stream_payload")
    format_payload = payload.get("format")
    if not isinstance(format_payload, dict):
        payload["format"] = {}
    return payload


def _extract_embedded_subtitle(
    video: Path,
    stream_index: int,
    output: Path,
    *,
    ffmpeg_path: str | None,
    timeout_seconds: float,
) -> None:
    executable = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
    command = [
        executable,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(video),
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "ass",
        str(output),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=max(0.001, float(timeout_seconds)),
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceInventoryError(_bounded_error("ffmpeg_timeout", exc)) from exc
    except OSError as exc:
        raise SourceInventoryError(_bounded_error("ffmpeg_unavailable", exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise SourceInventoryError(f"ffmpeg_failed:{_bounded_text(detail, 500)}")
    if not output.is_file() or output.stat().st_size <= 0:
        raise SourceInventoryError("ffmpeg_missing_subtitle_output")


def _subtitle_candidate(
    *,
    track_index: int,
    codec: str,
    language: str,
    title: str,
    metrics: _SubtitleMetrics | None,
    extraction_error: str,
    source_kind: str,
    source_reference: str,
    source_size: int | None = None,
    source_mtime_ns: int | None = None,
    source_sha256: str = "",
    default: bool = False,
    forced: bool = False,
    hearing_impaired: bool | None = None,
) -> SubtitleInventoryCandidate:
    return SubtitleInventoryCandidate(
        track_index=track_index,
        codec=codec,
        container_language_tag=language,
        title=title,
        default=default,
        forced=forced,
        hearing_impaired=hearing_impaired,
        event_count=0 if metrics is None else metrics.event_count,
        first_timestamp_seconds=None if metrics is None else metrics.first_timestamp_seconds,
        last_timestamp_seconds=None if metrics is None else metrics.last_timestamp_seconds,
        valid_timing_count=None if metrics is None else metrics.valid_timing_count,
        empty_event_count=None if metrics is None else metrics.empty_event_count,
        sample_text="" if metrics is None else metrics.sample_text,
        extraction_error=extraction_error,
        content_sha256="" if metrics is None else metrics.content_sha256,
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
        source_sha256=source_sha256,
        source_kind=source_kind,
        source_reference=source_reference,
    )


def _subtitle_metrics(path: Path) -> _SubtitleMetrics:
    text = _read_subtitle_text(path)
    if path.suffix.casefold() in {".ass", ".ssa"}:
        events = _ass_events(text)
    else:
        events = _timeline_events(text)
    valid = 0
    empty = 0
    starts: list[float] = []
    ends: list[float] = []
    samples: list[str] = []
    semantic_events: list[dict[str, Any]] = []
    sample_length = 0
    for start, end, content in events:
        if start is not None and end is not None and start >= 0 and end > start:
            valid += 1
            starts.append(start)
            ends.append(end)
        plain = _plain_subtitle_text(content)
        semantic_events.append(
            {
                "start": None if start is None else round(start, 3),
                "end": None if end is None else round(end, 3),
                "text": plain,
            }
        )
        if not plain:
            empty += 1
        elif sample_length < MAX_SAMPLE_CHARACTERS:
            remaining = MAX_SAMPLE_CHARACTERS - sample_length
            fragment = plain[:remaining]
            samples.append(fragment)
            sample_length += len(fragment) + 1
    return _SubtitleMetrics(
        event_count=len(events),
        first_timestamp_seconds=min(starts) if starts else None,
        last_timestamp_seconds=max(ends) if ends else None,
        valid_timing_count=valid,
        empty_event_count=empty,
        sample_text="\n".join(samples)[:MAX_SAMPLE_CHARACTERS],
        content_sha256=_canonical_sha256(semantic_events),
    )


def _ass_events(text: str) -> list[tuple[float | None, float | None, str]]:
    fields = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
    in_events = False
    events: list[tuple[float | None, float | None, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = line.casefold() == "[events]"
            continue
        if not in_events:
            continue
        lowered = line.casefold()
        if lowered.startswith("format:"):
            parsed = [item.strip().casefold() for item in line.split(":", 1)[1].split(",")]
            if parsed:
                fields = parsed
            continue
        if not lowered.startswith("dialogue:"):
            continue
        payload = line.split(":", 1)[1].lstrip()
        parts = payload.split(",", max(0, len(fields) - 1))
        values = {field: parts[index] if index < len(parts) else "" for index, field in enumerate(fields)}
        events.append(
            (
                _parse_ass_timestamp(values.get("start", "")),
                _parse_ass_timestamp(values.get("end", "")),
                values.get("text", parts[-1] if parts else ""),
            )
        )
    return events


def _timeline_events(text: str) -> list[tuple[float | None, float | None, str]]:
    lines = text.splitlines()
    events: list[tuple[float | None, float | None, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if "-->" not in line:
            index += 1
            continue
        match = _TIMELINE_RE.search(line)
        start = _parse_timeline_timestamp(match.group("start")) if match else None
        end = _parse_timeline_timestamp(match.group("end")) if match else None
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() and "-->" not in lines[index]:
            body.append(lines[index])
            index += 1
        events.append((start, end, "\n".join(body)))
    return events


def _parse_ass_timestamp(value: str) -> float | None:
    match = _ASS_TIMESTAMP_RE.fullmatch(str(value).strip())
    if match is None:
        return None
    seconds = int(match.group("h")) * 3600 + int(match.group("m")) * 60 + float(match.group("s"))
    return seconds if math.isfinite(seconds) else None


def _parse_timeline_timestamp(value: str) -> float | None:
    parts = str(value).replace(",", ".").split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        if len(parts) == 3:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        else:
            seconds = int(parts[0]) * 60 + float(parts[1])
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) else None


def _plain_subtitle_text(value: str) -> str:
    text = str(value).replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    text = _ASS_OVERRIDE_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    return " ".join(text.split()).strip()


def _read_subtitle_text(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SUBTITLE_BYTES + 1)
    except OSError as exc:
        raise SourceInventoryError(f"cannot read subtitle: {path.name}") from exc
    if len(raw) > MAX_SUBTITLE_BYTES:
        raise SourceInventoryError(f"subtitle exceeds bounded inventory limit: {path.name}")
    encodings = ("utf-8-sig", "utf-8", "utf-16", "gb18030", "cp950")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _sidecar_identity(video: Path, path: Path) -> SidecarIdentity:
    before = _file_signature(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise SourceInventoryError(f"cannot fingerprint source sidecar: {path.name}") from exc
    after = _file_signature(path)
    if after != before:
        raise SourceChangedError(f"source sidecar changed while hashing: {path.name}")
    return SidecarIdentity(
        relative_path=_relative_sidecar_path(video, path),
        size=before[2],
        mtime_ns=before[3],
        sha256=digest.hexdigest(),
    )


def _is_generated_sidecar(
    video: Path,
    path: Path,
    *,
    config: object | None,
    generated_sidecar_suffixes: Iterable[str],
) -> bool:
    lowered = path.name.casefold()
    if any(marker in lowered for marker in _AI_SIDECAR_MARKERS):
        return True
    if _is_verified_generated_publication_sidecar(video, path, config=config):
        return True
    configured: list[str] = [str(item) for item in generated_sidecar_suffixes]
    for attribute in (
        "ai_japanese_ass_suffix",
        "ai_simplified_chinese_ass_suffix",
        "ai_traditional_chinese_ass_suffix",
    ):
        value = getattr(config, attribute, "") if config is not None else ""
        if value:
            configured.extend((str(value), _srt_suffix(str(value))))
    for suffix in configured:
        if lowered == f"{video.stem}{suffix}".casefold():
            return True
    template = (
        str(getattr(config, "ai_source_transcript_ass_suffix_template", ""))
        if config is not None
        else ""
    )
    if template and "{language}" in template:
        expression = re.escape(template)
        expression = expression.replace(re.escape("{label}"), r".+?")
        expression = expression.replace(re.escape("{language}"), r"[A-Za-z0-9_-]+")
        if re.fullmatch(re.escape(video.stem) + expression, path.name, flags=re.IGNORECASE):
            return True
        srt_template = _srt_suffix(template)
        expression = re.escape(srt_template)
        expression = expression.replace(re.escape("{label}"), r".+?")
        expression = expression.replace(re.escape("{language}"), r"[A-Za-z0-9_-]+")
        if re.fullmatch(re.escape(video.stem) + expression, path.name, flags=re.IGNORECASE):
            return True
    return False


def _is_verified_generated_publication_sidecar(
    video: Path,
    path: Path,
    *,
    config: object | None,
) -> bool:
    """Exclude only a verified worker-owned conventional zh-TW output.

    A plain ``<video>.zh-TW.ass`` may be an original user sidecar, so its name
    alone is never enough.  Conversion/normalization outputs are excluded only
    when the current strict manifest binds that exact file and records the
    generating strategy.
    """

    if config is None or path.name.casefold() != f"{video.stem}.zh-tw.ass".casefold():
        return False
    try:
        from output_manifest import (
            ADOPTED_ZH_TW_PUBLICATION_KIND,
            CONVERTED_ZH_CN_PUBLICATION_KIND,
            output_manifest_path,
            validate_output_manifest,
        )

        if not validate_output_manifest(
            video,
            config,
            verify_hashes=True,
            required_outputs=(path,),
            require_publication_semantics=True,
        ):
            return False
        payload = json.loads(output_manifest_path(video, config).read_text(encoding="utf-8"))
        publication = payload.get("publication")
        provenance = payload.get("provenance")
        if not isinstance(publication, dict) or not isinstance(provenance, dict):
            return False
        kind = str(publication.get("kind") or "")
        if kind == CONVERTED_ZH_CN_PUBLICATION_KIND:
            return True
        source_analysis = provenance.get("source_analysis")
        return (
            kind == ADOPTED_ZH_TW_PUBLICATION_KIND
            and isinstance(source_analysis, dict)
            and str(source_analysis.get("strategy") or "") == "NORMALIZE_ZH_HANT"
        )
    except (AttributeError, ImportError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _normalize_media_job_identity(
    value: Mapping[str, Any] | str,
    video: Path,
    source_signature: tuple[int, int, int, int],
) -> dict[str, Any]:
    if isinstance(value, Mapping):
        supplied = {key: _json_safe(value[key]) for key in _STABLE_MEDIA_JOB_KEYS if key in value}
        if not supplied:
            raise ValueError("media_job_identity has no stable pipeline identity fields")
    else:
        job_id = str(value).strip()
        if not job_id:
            raise ValueError("media_job_identity must not be empty")
        supplied = {"job_id": job_id}
    size = source_signature[2]
    mtime_ns = source_signature[3]
    canonical = os.path.normcase(os.path.abspath(os.fspath(video)))
    device, inode = source_signature[:2]
    if device and inode:
        identity_kind = "filesystem_object"
        filesystem_identity = {"dev": device, "ino": inode, "size": size, "mtime_ns": mtime_ns}
    else:
        identity_kind = "path_stat_fallback"
        filesystem_identity = {"path": canonical, "size": size, "mtime_ns": mtime_ns}
    media_fingerprint = _canonical_sha256(filesystem_identity)
    media_revision = _canonical_sha256(
        {"fingerprint": media_fingerprint, "size": size, "mtime_ns": mtime_ns}
    )

    if "canonical_path" in supplied:
        supplied_canonical = os.path.normcase(
            os.path.abspath(os.fspath(supplied["canonical_path"]))
        )
        same_filesystem_object = (
            identity_kind == "filesystem_object"
            and str(supplied.get("media_fingerprint") or "").casefold() == media_fingerprint
        )
        if supplied_canonical != canonical and not same_filesystem_object:
            raise SourceChangedError("pipeline canonical path does not match source")
    if "identity_kind" in supplied and str(supplied["identity_kind"]) != identity_kind:
        raise SourceChangedError("pipeline media identity kind no longer matches source")
    if "media_size" in supplied and int(supplied["media_size"]) != size:
        raise SourceChangedError("pipeline media size no longer matches source")
    if "media_mtime_ns" in supplied and int(supplied["media_mtime_ns"]) != mtime_ns:
        raise SourceChangedError("pipeline media mtime no longer matches source")
    if "media_fingerprint" in supplied and str(supplied["media_fingerprint"]).casefold() != media_fingerprint:
        raise SourceChangedError("pipeline media fingerprint does not match source")
    if "media_revision" in supplied and str(supplied["media_revision"]).casefold() != media_revision:
        raise SourceChangedError("pipeline media revision does not match source")
    normalized = {
        **supplied,
        "canonical_path": canonical,
        "identity_kind": identity_kind,
        "media_size": size,
        "media_mtime_ns": mtime_ns,
        "media_fingerprint": media_fingerprint,
        "media_revision": media_revision,
    }
    return normalized


def _language_from_sidecar_name(name: str) -> str:
    lowered = name.casefold()
    if any(marker in lowered for marker in ("zh-tw", "zh_tw", "zh-hant", "cht", "traditional", "繁")):
        return "zh-TW"
    if any(marker in lowered for marker in ("zh-cn", "zh_cn", "zh-hans", "chs", "simplified", "简", "簡")):
        return "zh-CN"
    if any(marker in lowered for marker in ("japanese", "jpn", "日本", ".ja.")):
        return "ja"
    return ""


def _stream_duration(stream: Mapping[str, Any], tags: Mapping[str, Any]) -> float | None:
    direct = _positive_float(stream.get("duration"))
    if direct is not None:
        return direct
    for key, value in tags.items():
        if str(key).casefold().startswith("duration"):
            parsed = _parse_duration_tag(value)
            if parsed is not None:
                return parsed
    return None


def _parse_duration_tag(value: Any) -> float | None:
    parts = str(value or "").replace(",", ".").split(":")
    if len(parts) != 3:
        return _positive_float(value)
    try:
        seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _stream_tags(stream: Mapping[str, Any]) -> Mapping[str, Any]:
    value = stream.get("tags")
    return value if isinstance(value, Mapping) else {}


def _stream_disposition(stream: Mapping[str, Any]) -> Mapping[str, Any]:
    value = stream.get("disposition")
    return value if isinstance(value, Mapping) else {}


def _hearing_impaired_flag(disposition: Mapping[str, Any]) -> bool | None:
    keys = ("hearing_impaired", "captions", "descriptions")
    present = [key for key in keys if key in disposition]
    if not present:
        return None
    return any(_flag(disposition.get(key)) for key in present)


def _required_stream_index(stream: Mapping[str, Any]) -> int:
    try:
        index = int(stream["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceProbeError("stream_index_missing_or_invalid") from exc
    if index < 0:
        raise SourceProbeError("stream_index_negative")
    return index


def _stream_index_sort_key(stream: Mapping[str, Any]) -> tuple[int, str]:
    try:
        return int(stream.get("index", 2**31 - 1)), str(stream.get("codec_name", ""))
    except (TypeError, ValueError):
        return 2**31 - 1, str(stream.get("codec_name", ""))


def _subtitle_candidate_sort_key(item: SubtitleInventoryCandidate) -> tuple[int, int, str]:
    return (0 if item.source_kind == "sidecar" else 1, item.track_index, item.source_reference.casefold())


def _relative_sidecar_path(video: Path, sidecar: Path) -> str:
    try:
        relative = sidecar.resolve().relative_to(video.parent.resolve())
    except (OSError, ValueError) as exc:
        raise SourceInventoryError("source sidecar must stay inside the media directory") from exc
    if len(relative.parts) != 1:
        raise SourceInventoryError("source sidecar must be a direct media-directory sibling")
    return relative.as_posix()


def _source_signature(path: Path) -> tuple[int, int, int, int]:
    if not path.is_file():
        raise SourceInventoryError(f"source media is not a file: {path.name}")
    return _file_signature(path)


def _file_signature(path: Path) -> tuple[int, int, int, int]:
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise SourceInventoryError(f"cannot stat source input: {path.name}") from exc
    return (
        int(getattr(stat_result, "st_dev", 0) or 0),
        int(getattr(stat_result, "st_ino", 0) or 0),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _assert_source_unchanged(path: Path, expected: tuple[int, int, int, int]) -> None:
    if _file_signature(path) != expected:
        raise SourceChangedError("source media changed during inventory")


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("identity contains a non-finite float")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _required_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SourceInventoryError(f"selected subtitle {field} is invalid")
    parsed = _optional_int(value)
    if parsed is None:
        raise SourceInventoryError(f"selected subtitle {field} is missing or invalid")
    return parsed


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _valid_sha256(value: Any) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")) is not None


def _fsync_directory(path: Path) -> None:
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _srt_suffix(suffix: str) -> str:
    if suffix.casefold().endswith(".ass"):
        return f"{suffix[:-4]}.srt"
    return suffix


def _bounded_error(prefix: str, error: BaseException) -> str:
    detail = _bounded_text(str(error).replace("\r", " ").replace("\n", " "), 500)
    return f"{prefix}:{detail}" if detail else prefix


def _bounded_text(value: str, limit: int) -> str:
    return str(value)[: max(0, int(limit))]
