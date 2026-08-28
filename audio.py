from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid
import wave

from safe_files import atomic_write_bytes, atomic_write_text, fsync_directory, sha256_file


class AudioExtractionError(RuntimeError):
    pass


AUDIO_CACHE_METADATA_SCHEMA_VERSION = 1
AUDIO_EXTRACTION_TIMEOUT_SECONDS = 60 * 60
MIN_AUDIO_DURATION_SECONDS = 0.05
MAX_AUDIO_DURATION_SECONDS = 24 * 60 * 60
SOURCE_DURATION_TOLERANCE_RATIO = 0.05
SOURCE_DURATION_TOLERANCE_MIN_SECONDS = 5.0
SOURCE_DURATION_TOLERANCE_MAX_SECONDS = 120.0
PROBE_DURATION_TOLERANCE_SECONDS = 0.25


@dataclass(frozen=True)
class WaveValidationInfo:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int
    frame_count: int
    data_bytes: int


@dataclass(frozen=True)
class AudioStreamInfo:
    index: int
    language: str
    title: str
    default: bool
    commentary: bool
    codec_name: str = ""
    channels: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AudioStreamManifest:
    """Complete, metadata-only ffprobe result used by admission checks.

    ``complete`` is deliberately separate from ``streams``. An empty list
    after a failed or timed-out probe must never be interpreted as proof that
    a media file has no Japanese audio.
    """

    streams: tuple[AudioStreamInfo, ...]
    complete: bool
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "error": self.error,
            "streams": [stream.to_dict() for stream in self.streams],
        }


def extract_audio(
    video_path: str | Path,
    wav_path: str | Path,
    *,
    stream_index: int | None = None,
    source_duration_seconds: float | None = None,
    timeout_seconds: float | None = AUDIO_EXTRACTION_TIMEOUT_SECONDS,
) -> Path:
    """Extract one crash-safe, identity-bound Whisper WAV.

    ffmpeg never receives the public destination path.  It writes a unique
    sibling partial which is structurally validated, fsynced, and only then
    atomically replaces the destination.  A killed ffmpeg process can leave at
    most an untrusted partial; the previous final WAV is never truncated.

    Cache metadata is published immediately before the WAV.  Its audio hash
    makes the brief two-file publication window fail closed: readers see either
    the old valid pair, a hash mismatch, or the new valid pair, never a partial
    WAV that validates as complete.
    """

    video = Path(video_path)
    output = Path(wav_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    cleanup_audio_partials(output)
    remaining_partials = [
        candidate
        for candidate in output.parent.glob(f"{_audio_partial_prefix(output)}*.partial.wav")
        if candidate.is_file()
    ]
    if remaining_partials:
        raise AudioExtractionError(
            "Unable to remove stale audio extraction partials before retry: "
            + ", ".join(str(path) for path in remaining_partials[:5])
        )

    try:
        source_identity = _source_identity(video)
    except OSError as exc:
        raise AudioExtractionError(f"Audio source is unavailable: {video}: {exc}") from exc

    partial = _new_audio_partial_path(output)

    selected_audio_stream = stream_index if stream_index is not None else _select_preferred_audio_stream(video)
    known_source_duration = _positive_finite_duration(source_duration_seconds)
    if known_source_duration is None:
        known_source_duration = probe_media_duration(video)
    command = [
        _resolve_ffmpeg(),
        "-nostdin",
        "-y",
        "-i",
        str(video),
    ]
    if selected_audio_stream is not None:
        command.extend(["-map", f"0:{selected_audio_stream}"])
    command.extend(
        [
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(partial),
        ]
    )

    try:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_normalized_timeout(timeout_seconds),
            )
        except FileNotFoundError as exc:
            raise AudioExtractionError("ffmpeg not found. Install ffmpeg and add it to PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioExtractionError(
                f"ffmpeg timed out while extracting audio for {video} after "
                f"{_normalized_timeout(timeout_seconds)}s"
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise AudioExtractionError(f"ffmpeg failed for {video}: {stderr}")

        wave_info = _validate_wave_file(
            partial,
            source_duration_seconds=known_source_duration,
        )
        probed_duration = probe_media_duration(partial)
        if probed_duration is None:
            raise AudioExtractionError(f"ffprobe could not parse extracted WAV: {partial}")
        if abs(probed_duration - wave_info.duration_seconds) > PROBE_DURATION_TOLERANCE_SECONDS:
            raise AudioExtractionError(
                "Extracted WAV duration disagrees between RIFF and ffprobe: "
                f"riff={wave_info.duration_seconds:.3f}s probe={probed_duration:.3f}s path={partial}"
            )
        _validate_source_duration(wave_info.duration_seconds, known_source_duration, partial)
        _fsync_file(partial)

        metadata_path = audio_cache_metadata_path(output)
        previous_metadata = _read_previous_metadata(metadata_path)
        metadata = _build_audio_cache_metadata(
            output,
            partial,
            video,
            source_identity,
            selected_audio_stream,
            known_source_duration,
            wave_info,
        )
        metadata_published = False
        final_published = False
        try:
            atomic_write_text(
                metadata_path,
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            )
            metadata_published = True
            os.replace(partial, output)
            final_published = True
            fsync_directory(output.parent)
        except Exception:
            if metadata_published and not final_published:
                _restore_previous_metadata(metadata_path, previous_metadata)
            raise
        return output
    except AudioExtractionError:
        raise
    except OSError as exc:
        raise AudioExtractionError(f"Unable to publish extracted WAV for {video}: {exc}") from exc
    finally:
        _unlink_audio_partial(partial)


def audio_cache_metadata_path(wav_path: str | Path) -> Path:
    output = Path(wav_path)
    return output.with_name(f"{output.name}.cache.json")


def cleanup_audio_partials(wav_path: str | Path) -> int:
    """Remove orphaned extraction partials for one destination.

    This is also the recovery path for a Python/host SIGKILL, where no finally
    block can execute.  The next extraction removes all destination-scoped
    orphan partials before starting ffmpeg.
    """

    output = Path(wav_path)
    removed = 0
    for candidate in output.parent.glob(f"{_audio_partial_prefix(output)}*.partial.wav"):
        if not candidate.is_file():
            continue
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        fsync_directory(output.parent)
    return removed


def validate_cached_audio(
    wav_path: str | Path,
    source_path: str | Path | None = None,
    *,
    stream_index: int | None = None,
    source_duration_seconds: float | None = None,
) -> bool:
    """Return whether a cached WAV is complete and bound to its source.

    Supplying ``source_path`` enables the strict worker contract and requires
    atomic extraction metadata.  Legacy WAVs without metadata fail closed and
    must be rebuilt.  With no source path this remains a read-only structural
    WAV validator for callers that do not need cache identity checks.
    """

    audio = Path(wav_path)
    try:
        wave_info = _validate_wave_file(
            audio,
            source_duration_seconds=_positive_finite_duration(source_duration_seconds),
        )
    except (AudioExtractionError, OSError, ValueError, wave.Error):
        return False

    if source_path is None:
        return True

    source = Path(source_path)
    metadata_path = audio_cache_metadata_path(audio)
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        if int(payload.get("schema_version") or 0) != AUDIO_CACHE_METADATA_SCHEMA_VERSION:
            return False
        if str(payload.get("status") or "") != "complete":
            return False
        if payload.get("source") != _source_identity(source):
            return False

        recorded_stream = payload.get("stream_index")
        if stream_index is not None and recorded_stream != int(stream_index):
            return False

        audio_payload = payload.get("audio")
        if not isinstance(audio_payload, dict):
            return False
        if int(audio_payload.get("size") or -1) != audio.stat().st_size:
            return False
        if str(audio_payload.get("sha256") or "") != sha256_file(audio):
            return False
        if int(audio_payload.get("sample_rate") or 0) != wave_info.sample_rate:
            return False
        if int(audio_payload.get("channels") or 0) != wave_info.channels:
            return False
        if int(audio_payload.get("sample_width") or 0) != wave_info.sample_width:
            return False
        recorded_duration = float(audio_payload.get("duration_seconds"))
        if not math.isfinite(recorded_duration):
            return False
        if abs(recorded_duration - wave_info.duration_seconds) > PROBE_DURATION_TOLERANCE_SECONDS:
            return False

        known_source_duration = _positive_finite_duration(source_duration_seconds)
        if known_source_duration is None:
            known_source_duration = _positive_finite_duration(payload.get("source_duration_seconds"))
        _validate_source_duration(wave_info.duration_seconds, known_source_duration, audio)
    except (AudioExtractionError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _validate_wave_file(
    path: Path,
    *,
    source_duration_seconds: float | None = None,
) -> WaveValidationInfo:
    """Strictly parse the RIFF layout and independently verify it with wave."""

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise AudioExtractionError(f"WAV is unavailable: {path}: {exc}") from exc
    if file_size < 44:
        raise AudioExtractionError(f"WAV is truncated or header-only: {path}")

    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise AudioExtractionError(f"Not a RIFF/WAVE file: {path}")
        declared_size = struct.unpack("<I", header[4:8])[0] + 8
        if declared_size > file_size:
            raise AudioExtractionError(
                f"WAV RIFF length exceeds file size: declared={declared_size} actual={file_size} path={path}"
            )

        fmt_payload: bytes | None = None
        data_size: int | None = None
        offset = 12
        while offset + 8 <= min(file_size, declared_size):
            handle.seek(offset)
            chunk_header = handle.read(8)
            if len(chunk_header) != 8:
                raise AudioExtractionError(f"WAV chunk header is truncated: {path}")
            chunk_id = chunk_header[:4]
            chunk_size = struct.unpack("<I", chunk_header[4:])[0]
            payload_offset = offset + 8
            payload_end = payload_offset + chunk_size
            if payload_end > file_size or payload_end > declared_size:
                raise AudioExtractionError(
                    f"WAV chunk is truncated: id={chunk_id!r} size={chunk_size} path={path}"
                )
            if chunk_id == b"fmt " and fmt_payload is None:
                handle.seek(payload_offset)
                fmt_payload = handle.read(chunk_size)
            elif chunk_id == b"data" and data_size is None:
                data_size = chunk_size
            offset = payload_end + (chunk_size & 1)

    if fmt_payload is None or len(fmt_payload) < 16:
        raise AudioExtractionError(f"WAV has no valid fmt chunk: {path}")
    if data_size is None or data_size <= 0:
        raise AudioExtractionError(f"WAV has no audio frames: {path}")

    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack(
        "<HHIIHH", fmt_payload[:16]
    )
    if audio_format != 1:
        raise AudioExtractionError(f"WAV is not PCM: format={audio_format} path={path}")
    if channels != 1 or sample_rate != 16000 or bits_per_sample != 16:
        raise AudioExtractionError(
            "WAV does not match the Whisper PCM contract: "
            f"channels={channels} sample_rate={sample_rate} bits={bits_per_sample} path={path}"
        )
    if block_align != channels * (bits_per_sample // 8) or byte_rate != sample_rate * block_align:
        raise AudioExtractionError(f"WAV fmt byte alignment is invalid: {path}")
    if data_size % block_align:
        raise AudioExtractionError(f"WAV data chunk ends in a partial sample frame: {path}")

    frame_count = data_size // block_align
    duration_seconds = frame_count / sample_rate
    if not math.isfinite(duration_seconds) or not (
        MIN_AUDIO_DURATION_SECONDS <= duration_seconds <= MAX_AUDIO_DURATION_SECONDS
    ):
        raise AudioExtractionError(
            f"WAV duration is not reasonable: duration={duration_seconds!r}s path={path}"
        )

    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getnchannels() != channels:
                raise AudioExtractionError(f"WAV channel count is inconsistent: {path}")
            if reader.getframerate() != sample_rate:
                raise AudioExtractionError(f"WAV sample rate is inconsistent: {path}")
            if reader.getsampwidth() != bits_per_sample // 8:
                raise AudioExtractionError(f"WAV sample width is inconsistent: {path}")
            if reader.getnframes() != frame_count:
                raise AudioExtractionError(f"WAV frame count is inconsistent: {path}")
            reader.setpos(frame_count - 1)
            if len(reader.readframes(1)) != block_align:
                raise AudioExtractionError(f"WAV final frame is truncated: {path}")
    except wave.Error as exc:
        raise AudioExtractionError(f"WAV cannot be parsed: {path}: {exc}") from exc

    _validate_source_duration(duration_seconds, source_duration_seconds, path)
    return WaveValidationInfo(
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=bits_per_sample // 8,
        frame_count=frame_count,
        data_bytes=data_size,
    )


def _validate_source_duration(
    audio_duration_seconds: float,
    source_duration_seconds: float | None,
    path: Path,
) -> None:
    source_duration = _positive_finite_duration(source_duration_seconds)
    if source_duration is None:
        return
    tolerance = min(
        SOURCE_DURATION_TOLERANCE_MAX_SECONDS,
        max(SOURCE_DURATION_TOLERANCE_MIN_SECONDS, source_duration * SOURCE_DURATION_TOLERANCE_RATIO),
    )
    if abs(audio_duration_seconds - source_duration) > tolerance:
        raise AudioExtractionError(
            "WAV duration does not match source duration: "
            f"audio={audio_duration_seconds:.3f}s source={source_duration:.3f}s "
            f"tolerance={tolerance:.3f}s path={path}"
        )


def _source_identity(source: Path) -> dict[str, Any]:
    stat_result = source.stat()
    return {
        "canonical_path": os.path.normcase(os.path.abspath(str(source.resolve()))),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def _build_audio_cache_metadata(
    output: Path,
    partial: Path,
    source: Path,
    source_identity: dict[str, Any],
    stream_index: int | None,
    source_duration_seconds: float | None,
    wave_info: WaveValidationInfo,
) -> dict[str, Any]:
    return {
        "schema_version": AUDIO_CACHE_METADATA_SCHEMA_VERSION,
        "status": "complete",
        "created_at": time.time(),
        "source_path": str(source),
        "source": source_identity,
        "stream_index": stream_index,
        "source_duration_seconds": source_duration_seconds,
        "audio": {
            "path": str(output),
            "size": int(partial.stat().st_size),
            "sha256": sha256_file(partial),
            "duration_seconds": wave_info.duration_seconds,
            "sample_rate": wave_info.sample_rate,
            "channels": wave_info.channels,
            "sample_width": wave_info.sample_width,
            "frame_count": wave_info.frame_count,
            "data_bytes": wave_info.data_bytes,
        },
    }


def _positive_finite_duration(value: Any) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def _normalized_timeout(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return float(AUDIO_EXTRACTION_TIMEOUT_SECONDS)
    return max(1.0, timeout)


def _audio_partial_prefix(output: Path) -> str:
    digest = hashlib.sha256(output.name.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f".audio-{digest}-"


def _new_audio_partial_path(output: Path) -> Path:
    return output.parent / (
        f"{_audio_partial_prefix(output)}{os.getpid()}-{uuid.uuid4().hex}.partial.wav"
    )


def _fsync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _unlink_audio_partial(path: Path) -> None:
    for attempt in range(5):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 4:
                return
            time.sleep(0.01 * (2**attempt))
        except OSError:
            return


def _read_previous_metadata(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AudioExtractionError(f"Unable to preserve existing audio cache metadata: {path}: {exc}") from exc


def _restore_previous_metadata(path: Path, previous: bytes | None) -> None:
    try:
        if previous is None:
            path.unlink(missing_ok=True)
            fsync_directory(path.parent)
        else:
            atomic_write_bytes(path, previous)
    except OSError:
        # A stale/missing metadata file always makes validate_cached_audio fail
        # closed.  Do not mask the original final-WAV publication error.
        pass


def _select_preferred_audio_stream(video: Path) -> int | None:
    streams = _probe_audio_streams(video)
    if not streams:
        return None

    japanese_streams = [stream for stream in streams if _is_japanese_audio_stream(stream)]
    if japanese_streams:
        return _preferred_stream_index(japanese_streams)
    return _preferred_stream_index(streams)


def probe_audio_streams(video_path: str | Path) -> list[AudioStreamInfo]:
    result: list[AudioStreamInfo] = []
    for stream in _probe_audio_streams(Path(video_path)):
        parsed = _audio_stream_info(stream)
        if parsed is not None:
            result.append(parsed)
    return result


def probe_audio_stream_manifest(video_path: str | Path) -> AudioStreamManifest:
    """Return a strict audio manifest without conflating probe failure and no audio."""

    result: list[AudioStreamInfo] = []
    streams, error = _probe_audio_streams_with_error(Path(video_path))
    if error:
        return AudioStreamManifest((), False, error)
    for stream in streams:
        parsed = _audio_stream_info(stream)
        if parsed is None:
            return AudioStreamManifest((), False, "ffprobe audio stream is missing a valid index")
        result.append(parsed)
    return AudioStreamManifest(tuple(result), True)


def _audio_stream_info(stream: dict[str, Any]) -> AudioStreamInfo | None:
    index = _stream_index(stream)
    if index is None:
        return None
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
    title = str(tags.get("title") or "").strip()
    language = str(tags.get("language") or "").strip().casefold()
    try:
        channels = int(stream.get("channels")) if stream.get("channels") is not None else None
    except (TypeError, ValueError):
        channels = None
    return AudioStreamInfo(
        index=index,
        language=language,
        title=title,
        default=disposition.get("default") == 1,
        commentary=(
            disposition.get("comment") == 1
            or disposition.get("commentary") == 1
            or _is_commentary_title(title)
        ),
        codec_name=str(stream.get("codec_name") or ""),
        channels=channels,
    )


def manifest_confirms_no_non_commentary_japanese_audio(manifest: AudioStreamManifest) -> bool:
    """Return true only for a complete, unambiguous non-Japanese main-track manifest.

    Every non-commentary stream must carry an explicit non-Japanese language
    tag, and a Japanese marker in either the tag or title vetoes exclusion.
    Unknown and special language tags remain candidates for the Worker's
    content-based language checks.
    """

    if not manifest.complete or not manifest.streams:
        return False
    main_streams = [stream for stream in manifest.streams if not stream.commentary]
    if not main_streams:
        return False
    return all(_is_explicit_non_japanese_audio_stream(stream) for stream in main_streams)


def preferred_audio_stream_info(video_path: str | Path) -> AudioStreamInfo | None:
    streams = probe_audio_streams(video_path)
    if not streams:
        return None
    japanese = [stream for stream in streams if _is_japanese_language(stream.language, stream.title)]
    return _preferred_info(japanese or streams)


def extract_audio_stream_sample(
    video_path: str | Path,
    output_path: str | Path,
    *,
    stream_index: int,
    start_seconds: float,
    duration_seconds: int,
) -> Path:
    video = Path(video_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _resolve_ffmpeg(),
        "-y",
        "-ss",
        f"{max(0.0, float(start_seconds)):.3f}",
        "-t",
        str(max(1, int(duration_seconds))),
        "-i",
        str(video),
        "-map",
        f"0:{int(stream_index)}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
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
            timeout=max(30, int(duration_seconds) * 3),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AudioExtractionError(f"Unable to sample audio stream {stream_index} from {video}: {exc}") from exc
    if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "ffmpeg produced no sample").strip()
        raise AudioExtractionError(f"ffmpeg failed sampling audio stream {stream_index} from {video}: {detail}")
    return output


def probe_media_duration(video_path: str | Path) -> float | None:
    command = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(Path(video_path)),
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
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        duration = float((result.stdout or "").strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def has_japanese_audio_stream(video_path: str | Path) -> bool:
    return any(_is_japanese_audio_stream(stream) for stream in _probe_audio_streams(Path(video_path)))


def _probe_audio_streams(video: Path) -> list[dict[str, Any]]:
    streams, _error = _probe_audio_streams_with_error(video)
    return streams


def _probe_audio_streams_with_error(video: Path) -> tuple[list[dict[str, Any]], str]:
    command = [
        _resolve_ffprobe(),
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index,codec_name,channels:stream_tags=language,title:stream_disposition=default,comment",
        "-of",
        "json",
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
            timeout=30,
        )
    except FileNotFoundError:
        return [], "ffprobe not found"
    except subprocess.TimeoutExpired:
        return [], f"ffprobe timed out for {video}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffprobe failed").strip()
        return [], f"ffprobe failed for {video}: {detail}"
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return [], f"ffprobe returned invalid JSON for {video}"
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return [], f"ffprobe returned no audio stream list for {video}"
    if any(not isinstance(stream, dict) for stream in streams):
        return [], f"ffprobe returned an invalid audio stream entry for {video}"
    return list(streams), ""


def _is_japanese_audio_stream(stream: dict[str, Any]) -> bool:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    language = str(tags.get("language") or "").strip().casefold()
    title = str(tags.get("title") or "").strip().casefold()
    return _is_japanese_language(language, title)


def _is_japanese_language(language: str, title: str = "") -> bool:
    if str(language).strip().casefold() in {"ja", "jpn", "japanese", "jp", "jap"}:
        return True
    lowered = str(title).strip().casefold()
    return any(marker in lowered for marker in ("japanese", "jpn", "日本語", "日語", "日语"))


def _is_explicit_non_japanese_audio_stream(stream: AudioStreamInfo) -> bool:
    language = str(stream.language or "").strip().casefold().replace("_", "-")
    if language in {"", "und", "unk", "unknown", "mis", "mul", "zxx"}:
        return False
    if language.startswith("qaa"):
        return False
    return not _is_japanese_language(language, stream.title)


def _is_commentary_title(title: str) -> bool:
    lowered = str(title or "").casefold()
    return any(
        marker in lowered
        for marker in (
            "commentary",
            "audio comment",
            "director comment",
            "cast comment",
            "副音声",
            "コメンタリー",
            "評論",
            "评论",
        )
    )


def _preferred_stream_index(streams: list[dict[str, Any]]) -> int | None:
    scored: list[tuple[tuple[int, int, int, int], int]] = []
    for stream in streams:
        index = _stream_index(stream)
        if index is None:
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        title = str(tags.get("title") or "")
        try:
            channels = int(stream.get("channels") or 0)
        except (TypeError, ValueError):
            channels = 0
        score = (
            0 if _is_commentary_title(title) else 1,
            1 if disposition.get("default") == 1 else 0,
            channels,
            -index,
        )
        scored.append((score, index))
    return max(scored, default=((0, 0, 0, 0), None))[1]


def _preferred_info(streams: list[AudioStreamInfo]) -> AudioStreamInfo | None:
    if not streams:
        return None
    return max(
        streams,
        key=lambda stream: (
            0 if stream.commentary else 1,
            1 if stream.default else 0,
            int(stream.channels or 0),
            -stream.index,
        ),
    )


def _stream_index(stream: dict[str, Any]) -> int | None:
    try:
        return int(stream.get("index"))
    except (TypeError, ValueError):
        return None


def separate_vocals(audio_path: str | Path, output_path: str | Path, engine: str, output_label: str) -> Path:
    normalized_engine = engine.strip().lower()
    if normalized_engine in {"none", ""}:
        return Path(audio_path)
    if normalized_engine != "demucs":
        raise AudioExtractionError(f"Unsupported vocal separation engine: {engine!r}. Supported: demucs")

    return _separate_vocals_with_demucs(audio_path, output_path, output_label)


def _separate_vocals_with_demucs(audio_path: str | Path, output_path: str | Path, output_label: str) -> Path:
    source = Path(audio_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    demucs = _resolve_demucs()
    with tempfile.TemporaryDirectory(prefix="anime-subtitle-demucs-") as temp_dir:
        temp_path = Path(temp_dir)
        command = [
            *demucs,
            "--two-stems",
            "vocals",
            "--name",
            "htdemucs",
            "--out",
            str(temp_path),
            str(source),
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            raise AudioExtractionError(f"demucs failed for {source}: {stderr}")

        separated = _find_demucs_stem(temp_path, source, output_label)
        _convert_audio_to_whisper_wav(separated, output)

    if not output.exists() or output.stat().st_size == 0:
        raise AudioExtractionError(f"demucs did not create a valid vocal wav file: {output}")
    return output


def _find_demucs_stem(root: Path, source: Path, output_label: str) -> Path:
    candidates = sorted(root.rglob(f"{output_label}.wav"))
    if candidates:
        return candidates[0]

    source_stem = source.stem
    candidates = sorted(root.rglob(f"{source_stem}.wav"))
    if candidates:
        return candidates[0]

    raise AudioExtractionError(f"demucs did not create expected stem {output_label!r} under {root}")


def _convert_audio_to_whisper_wav(source: Path, output: Path) -> None:
    command = [
        _resolve_ffmpeg(),
        "-y",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(output),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise AudioExtractionError(f"ffmpeg failed while converting demucs output {source}: {stderr}")


def _resolve_demucs() -> list[str]:
    demucs = shutil.which("demucs")
    if demucs:
        return [demucs]
    return [sys.executable, "-m", "demucs"]


def _resolve_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        matches = sorted(
            winget_packages.glob("Gyan.FFmpeg*_8wekyb3d8bbwe/ffmpeg-*/bin/ffmpeg.exe"),
            key=lambda path: str(path).casefold(),
            reverse=True,
        )
        if matches:
            return str(matches[0])

    return "ffmpeg"


def _resolve_ffprobe() -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe

    ffmpeg_path = Path(_resolve_ffmpeg())
    ffprobe_name = "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
    sibling = ffmpeg_path.with_name(ffprobe_name)
    if sibling.exists():
        return str(sibling)

    return "ffprobe"
