from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from typing import Any

from output_manifest import (
    delivery_identity,
    manifest_publication_semantics,
    output_manifest_path,
    publication_is_traditional_chinese_delivery,
    validate_output_manifest,
)
from safe_files import atomic_write_text, fsync_directory, sha256_file
from acceptance_queue_lane import acceptance_run_id_for_video


COMPLETED_DELIVERY_SCHEMA_VERSION = 1
COMPLETED_DELIVERY_CONTRACT = "completed-mkv-delivery-v1"
_TITLE_BY_LANGUAGE = {
    "ja": "AI 日本語",
    "zh-CN": "AI 简体中文",
    "zh-TW": "AI 繁體中文",
}


class CompletedDeliveryError(RuntimeError):
    """A completed-media delivery could not be proven safe."""


class CompletedDeliveryCollisionError(CompletedDeliveryError):
    """The destination already contains an artifact without matching evidence."""


@dataclass(frozen=True)
class CompletedDeliveryResult:
    destination: str
    receipt: str
    output_sha256: str
    output_size: int
    publication_manifest_sha256: str
    recovered: bool
    source_retained: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def completed_delivery_enabled(config: Any) -> bool:
    return bool(getattr(config, "completed_delivery_enabled", False))


def completed_delivery_root(config: Any) -> Path:
    configured = str(getattr(config, "completed_delivery_path", "") or "").strip()
    if not configured:
        raise CompletedDeliveryError("completed_delivery_path must be configured")
    path = Path(configured)
    if not path.is_absolute():
        path = Path(config.work_path) / path
    return path.resolve()


def completed_delivery_destination(video: str | Path, config: Any) -> Path:
    source = Path(video).resolve()
    input_root = Path(config.input_path).resolve()
    root = completed_delivery_root(config)
    try:
        relative = source.relative_to(input_root)
    except ValueError as exc:
        raise CompletedDeliveryError(
            f"source must stay inside input_path: source={source} input={input_root}"
        ) from exc
    if _is_within(root, input_root):
        raise CompletedDeliveryError(
            "completed_delivery_path must stay outside input_path to prevent scanner re-admission"
        )
    relative_output = (
        relative
        if relative.suffix.casefold() == ".mkv"
        else relative.with_name(f"{relative.name}.mkv")
    )
    destination = (root / relative_output).resolve()
    if not _is_within(destination, root) or destination == source:
        raise CompletedDeliveryError("completed delivery destination escaped its configured root")
    return destination


def completed_delivery_receipt_path(video: str | Path, config: Any) -> Path:
    root_value = str(
        getattr(config, "completed_delivery_manifest_path", "completed_delivery_manifests")
        or "completed_delivery_manifests"
    )
    root = Path(root_value)
    if not root.is_absolute():
        root = Path(config.work_path) / root
    digest = hashlib.sha256(str(Path(video).resolve()).encode("utf-8", errors="replace")).hexdigest()
    return root / digest[:2] / f"{digest}.json"


def completed_delivery_marker_path(video: str | Path, config: Any) -> Path:
    return completed_delivery_receipt_path(video, config).with_suffix(".delivering")


def deliver_completed_mkv(
    video: str | Path,
    config: Any,
    *,
    logger: Any | None = None,
) -> CompletedDeliveryResult:
    """Mux a strict Chinese publication into an independently committed MKV.

    The source media is deliberately retained.  Existing queue, obligation,
    Mikan seeding, and acceptance identities all bind the original path and
    stat; removing that source here would make a valid subtitle delivery look
    corrupt before the parent can commit its ledger result.
    """

    if not completed_delivery_enabled(config):
        raise CompletedDeliveryError("completed delivery is disabled")
    source_policy = str(
        getattr(config, "completed_delivery_source_policy", "retain") or "retain"
    ).strip().lower()
    if source_policy != "retain":
        raise CompletedDeliveryError(
            "completed_delivery_source_policy must be 'retain'; destructive source moves are unsupported"
        )

    source = Path(video).resolve()
    if not source.is_file():
        raise CompletedDeliveryError(f"source media is unavailable: {source}")
    destination = completed_delivery_destination(source, config)
    receipt_path = completed_delivery_receipt_path(source, config)
    marker_path = completed_delivery_marker_path(source, config)
    timeout = _positive_timeout(config)
    publication = _strict_publication(source, config)
    identity = delivery_identity(source, config)
    source_stat = source.stat()
    source_identity = {
        "canonical_path": str(source),
        "media_size": int(source_stat.st_size),
        "media_mtime_ns": int(source_stat.st_mtime_ns),
        "media_fingerprint": str(identity["media"]["media_fingerprint"]),
        "sha256": sha256_file(source),
    }
    manifest_path = output_manifest_path(source, config).resolve()
    manifest_sha256 = sha256_file(manifest_path)
    transaction_identity = {
        "contract": COMPLETED_DELIVERY_CONTRACT,
        "source": source_identity,
        "delivery": {
            "obligation_id": str(identity["obligation_id"]),
            "policy_revision": str(identity["policy_revision"]),
        },
        "publication_manifest": {
            "path": str(manifest_path),
            "sha256": manifest_sha256,
        },
        "publication": publication["semantics"],
        "destination": str(destination),
    }
    acceptance_run_id = acceptance_run_id_for_video(config, source)
    if acceptance_run_id:
        transaction_identity["acceptance_run_id"] = acceptance_run_id

    existing = _load_json(receipt_path)
    if existing is not None and _receipt_identity_matches(existing, transaction_identity):
        if _validate_committed_receipt(
            existing,
            source=source,
            destination=destination,
            publication=publication,
            timeout=timeout,
        ):
            marker_path.unlink(missing_ok=True)
            return _result_from_receipt(existing, receipt_path, recovered=True)
        raise CompletedDeliveryError(
            f"committed completed-delivery receipt or artifact failed revalidation: {receipt_path}"
        )
    if destination.exists() and existing is None and not marker_path.exists():
        raise CompletedDeliveryCollisionError(
            f"completed destination exists without a matching receipt: {destination}"
        )
    if destination.exists() and existing is not None and not _receipt_identity_matches(
        existing, transaction_identity
    ):
        raise CompletedDeliveryCollisionError(
            f"completed destination belongs to different delivery evidence: {destination}"
        )

    marker = _load_json(marker_path)
    recovered = marker is not None
    if marker is not None:
        if not _marker_identity_matches(marker, transaction_identity):
            stale_staged = Path(str(marker.get("staged_path") or ""))
            safe_stale = (
                marker.get("schema_version") == COMPLETED_DELIVERY_SCHEMA_VERSION
                and marker.get("state") == "muxing"
                and stale_staged.name.startswith(".muxing-")
                and stale_staged.suffix.casefold() == ".mkv"
                and _is_within(stale_staged.resolve(), destination.parent.resolve())
            )
            if not safe_stale:
                raise CompletedDeliveryError(
                    f"interrupted delivery marker is malformed or unowned: {marker_path}"
                )
            stale_staged.unlink(missing_ok=True)
            marker_path.unlink()
            marker = None
            if destination.exists():
                raise CompletedDeliveryCollisionError(
                    f"stale transaction left a completed-path collision: {destination}"
                )
        if marker is None:
            attempt_id = uuid.uuid4().hex
            staged = destination.parent / f".muxing-{hashlib.sha256(destination.name.encode()).hexdigest()[:16]}-{attempt_id}.mkv"
            marker = {
                "schema_version": COMPLETED_DELIVERY_SCHEMA_VERSION,
                **transaction_identity,
                "state": "muxing",
                "attempt_id": attempt_id,
                "staged_path": str(staged),
                "started_at": time.time(),
            }
            atomic_write_text(marker_path, json.dumps(marker, ensure_ascii=False, indent=2) + "\n")
        else:
            staged = Path(str(marker.get("staged_path") or ""))
        if not _is_within(staged.resolve(), destination.parent.resolve()):
            raise CompletedDeliveryError("interrupted delivery staged path escaped destination directory")
    else:
        attempt_id = uuid.uuid4().hex
        staged = destination.parent / f".muxing-{hashlib.sha256(destination.name.encode()).hexdigest()[:16]}-{attempt_id}.mkv"
        marker = {
            "schema_version": COMPLETED_DELIVERY_SCHEMA_VERSION,
            **transaction_identity,
            "state": "muxing",
            "attempt_id": attempt_id,
            "staged_path": str(staged),
            "started_at": time.time(),
        }
        atomic_write_text(marker_path, json.dumps(marker, ensure_ascii=False, indent=2) + "\n")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _verify_muxed_output(source, destination, publication, timeout=timeout)
        _assert_publication_unchanged(publication, manifest_path, manifest_sha256)
        _assert_source_identity(source, source_identity)
    else:
        staged_valid = staged.is_file()
        if staged_valid:
            try:
                _verify_muxed_output(source, staged, publication, timeout=timeout)
            except CompletedDeliveryError:
                staged_valid = False
        if not staged_valid:
            staged.unlink(missing_ok=True)
            _run_mux(source, staged, publication, timeout=timeout, logger=logger)
            _verify_muxed_output(source, staged, publication, timeout=timeout)
        _assert_source_identity(source, source_identity)
        _assert_publication_unchanged(publication, manifest_path, manifest_sha256)
        if destination.exists():
            raise CompletedDeliveryCollisionError(
                f"completed destination appeared during mux transaction: {destination}"
            )
        try:
            os.link(staged, destination)
        except FileExistsError as exc:
            raise CompletedDeliveryCollisionError(
                f"completed destination appeared during atomic publish: {destination}"
            ) from exc
        except OSError as exc:
            raise CompletedDeliveryError(
                "completed filesystem does not support fail-if-exists atomic publication"
            ) from exc
        staged.unlink()
        fsync_directory(destination.parent)

    output_stat = destination.stat()
    receipt = {
        "schema_version": COMPLETED_DELIVERY_SCHEMA_VERSION,
        **transaction_identity,
        "state": "committed",
        "attempt_id": str(marker.get("attempt_id") or ""),
        "output": {
            "path": str(destination),
            "size": int(output_stat.st_size),
            "mtime_ns": int(output_stat.st_mtime_ns),
            "sha256": sha256_file(destination),
        },
        "source_retained": True,
        "committed_at": time.time(),
    }
    atomic_write_text(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    marker_path.unlink(missing_ok=True)
    fsync_directory(marker_path.parent)
    return _result_from_receipt(receipt, receipt_path, recovered=recovered)


def validate_completed_delivery(
    video: str | Path,
    config: Any,
    *,
    verify_streams: bool = True,
) -> bool:
    try:
        source = Path(video).resolve()
        destination = completed_delivery_destination(source, config)
        receipt_path = completed_delivery_receipt_path(source, config)
        if completed_delivery_marker_path(source, config).exists():
            return False
        publication = _strict_publication(source, config)
        identity = delivery_identity(source, config)
        source_stat = source.stat()
        manifest = output_manifest_path(source, config).resolve()
        expected = {
            "contract": COMPLETED_DELIVERY_CONTRACT,
            "source": {
                "canonical_path": str(source),
                "media_size": int(source_stat.st_size),
                "media_mtime_ns": int(source_stat.st_mtime_ns),
                "media_fingerprint": str(identity["media"]["media_fingerprint"]),
                "sha256": sha256_file(source),
            },
            "delivery": {
                "obligation_id": str(identity["obligation_id"]),
                "policy_revision": str(identity["policy_revision"]),
            },
            "publication_manifest": {
                "path": str(manifest),
                "sha256": sha256_file(manifest),
            },
            "publication": publication["semantics"],
            "destination": str(destination),
        }
        acceptance_run_id = acceptance_run_id_for_video(config, source)
        if acceptance_run_id:
            expected["acceptance_run_id"] = acceptance_run_id
        receipt = _load_json(receipt_path)
        if receipt is None or not _receipt_identity_matches(receipt, expected):
            return False
        if not _validate_committed_receipt(
            receipt,
            source=source,
            destination=destination,
            publication=publication,
            timeout=_positive_timeout(config),
            verify_streams=verify_streams,
        ):
            return False
        return True
    except (CompletedDeliveryError, OSError, TypeError, ValueError):
        return False


def _strict_publication(video: Path, config: Any) -> dict[str, Any]:
    identity = delivery_identity(video, config)
    if not validate_output_manifest(
        video,
        config,
        verify_hashes=True,
        require_delivery_evidence=True,
        expected_obligation_id=str(identity["obligation_id"]),
        expected_policy_revision=str(identity["policy_revision"]),
        require_publication_semantics=True,
    ):
        raise CompletedDeliveryError("strict subtitle publication evidence is missing or invalid")
    manifest_path = output_manifest_path(video, config)
    payload = _load_json(manifest_path)
    if payload is None:
        raise CompletedDeliveryError("subtitle publication manifest is unreadable")
    semantics = manifest_publication_semantics(payload)
    if semantics is None or not publication_is_traditional_chinese_delivery(semantics):
        raise CompletedDeliveryError("publication is not a Traditional-Chinese delivery")
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise CompletedDeliveryError("publication has no subtitle outputs")
    tracks: list[dict[str, str]] = []
    for entry in outputs:
        if not isinstance(entry, dict):
            raise CompletedDeliveryError("publication output entry is malformed")
        language = str(entry.get("language") or "").strip()
        path = Path(str(entry.get("path") or "")).resolve()
        if not path.is_file():
            raise CompletedDeliveryError(f"publication subtitle is missing: {path}")
        tracks.append(
            {
                "path": str(path),
                "language": language,
                "title": _TITLE_BY_LANGUAGE.get(language, f"AI {language}"),
                "sha256": str(entry.get("sha256") or ""),
            }
        )
    if sum(1 for track in tracks if track["language"] == "zh-TW") != 1:
        raise CompletedDeliveryError("publication must contain exactly one zh-TW subtitle output")
    return {"semantics": semantics, "tracks": tracks}


def _run_mux(
    source: Path,
    staged: Path,
    publication: dict[str, Any],
    *,
    timeout: float,
    logger: Any | None,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CompletedDeliveryError("ffmpeg executable is unavailable")
    source_probe = _probe(source, timeout=timeout)
    source_subtitles = sum(
        1 for stream in source_probe["streams"] if stream.get("codec_type") == "subtitle"
    )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
    ]
    for track in publication["tracks"]:
        command.extend(["-i", str(track["path"])])
    command.extend(["-map", "0"])
    for input_index in range(1, len(publication["tracks"]) + 1):
        command.extend(["-map", f"{input_index}:0"])
    command.extend(["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"])
    for existing_subtitle_index in range(source_subtitles):
        command.extend([f"-disposition:s:{existing_subtitle_index}", "-default"])
    for index, track in enumerate(publication["tracks"]):
        output_subtitle_index = source_subtitles + index
        command.extend(
            [
                f"-metadata:s:s:{output_subtitle_index}",
                f"language={track['language']}",
                f"-metadata:s:s:{output_subtitle_index}",
                f"title={track['title']}",
                f"-disposition:s:{output_subtitle_index}",
                "default" if track["language"] == "zh-TW" else "0",
            ]
        )
    command.extend(["-f", "matroska", str(staged)])
    if logger is not None:
        logger.info(
            "Muxing verified subtitle publication to completed staging path: source=%s staged=%s tracks=%s",
            source,
            staged,
            [track["language"] for track in publication["tracks"]],
        )
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CompletedDeliveryError(f"completed MKV mux timed out after {timeout:g}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "ffmpeg mux failed")[-2000:]
        raise CompletedDeliveryError(f"completed MKV mux failed: {detail}")
    if not staged.is_file() or staged.stat().st_size <= 0:
        raise CompletedDeliveryError("ffmpeg returned success without a non-empty staged MKV")


def _verify_muxed_output(
    source: Path,
    output: Path,
    publication: dict[str, Any],
    *,
    timeout: float,
) -> None:
    source_probe = _probe(source, timeout=timeout)
    output_probe = _probe(output, timeout=timeout)
    source_streams = source_probe["streams"]
    output_streams = output_probe["streams"]
    if len(output_streams) != len(source_streams) + len(publication["tracks"]):
        raise CompletedDeliveryError("muxed MKV stream count does not preserve the source plus publication tracks")

    source_subtitle_count = sum(
        1 for stream in source_streams if stream.get("codec_type") == "subtitle"
    )
    output_subtitle_entries = [
        (position, stream)
        for position, stream in enumerate(output_streams)
        if stream.get("codec_type") == "subtitle"
    ]
    expected_subtitle_count = source_subtitle_count + len(publication["tracks"])
    if len(output_subtitle_entries) != expected_subtitle_count:
        raise CompletedDeliveryError(
            "muxed MKV subtitle stream count does not preserve the source plus publication tracks"
        )
    generated_entries = output_subtitle_entries[source_subtitle_count:]
    generated_positions = {position for position, _stream in generated_entries}
    source_signatures = Counter(_stream_signature(stream) for stream in source_streams)
    preserved_signatures = Counter(
        _stream_signature(stream)
        for position, stream in enumerate(output_streams)
        if position not in generated_positions
    )
    if preserved_signatures != source_signatures:
        raise CompletedDeliveryError("muxed MKV changed the preserved source stream set")
    _validate_duration(source_probe, output_probe)
    if _av_copy_hash(source, timeout=timeout) != _av_copy_hash(output, timeout=timeout):
        raise CompletedDeliveryError("muxed MKV audio/video packet content differs from the source")

    for index, track in enumerate(publication["tracks"]):
        _absolute_index, stream = generated_entries[index]
        if stream.get("codec_type") != "subtitle" or str(stream.get("codec_name") or "").lower() not in {
            "ass",
            "ssa",
        }:
            raise CompletedDeliveryError(f"generated track {index} is not an ASS subtitle stream")
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        language = str(tags.get("language") or "")
        title = str(tags.get("title") or "")
        if language.casefold() != str(track["language"]).casefold() or title != track["title"]:
            raise CompletedDeliveryError(f"generated track {index} language/title metadata mismatch")
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        expected_default = 1 if track["language"] == "zh-TW" else 0
        if int(disposition.get("default") or 0) != expected_default:
            raise CompletedDeliveryError(f"generated track {index} default disposition mismatch")
        source_packets = _subtitle_packet_fingerprint(
            Path(track["path"]), "s:0", timeout=timeout
        )
        output_packets = _subtitle_packet_fingerprint(
            output, f"s:{source_subtitle_count + index}", timeout=timeout
        )
        if not source_packets or source_packets != output_packets:
            raise CompletedDeliveryError(f"generated track {index} subtitle packet content/timing mismatch")
    default_subtitles = [
        stream
        for stream in output_streams
        if stream.get("codec_type") == "subtitle"
        and int((stream.get("disposition") or {}).get("default") or 0) == 1
    ]
    if len(default_subtitles) != 1:
        raise CompletedDeliveryError("muxed MKV must have exactly one default subtitle track")


def _probe(path: Path, *, timeout: float) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise CompletedDeliveryError("ffprobe executable is unavailable")
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    payload = _run_json_command(command, timeout=timeout, label="ffprobe")
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        raise CompletedDeliveryError(f"media probe returned no streams: {path}")
    return payload


def _subtitle_packet_fingerprint(path: Path, selector: str, *, timeout: float) -> tuple[tuple[Any, ...], ...]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise CompletedDeliveryError("ffprobe executable is unavailable")
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        selector,
        "-show_packets",
        "-show_entries",
        "packet=pts_time,duration_time,data_hash",
        "-show_data_hash",
        "sha256",
        "-of",
        "json",
        str(path),
    ]
    payload = _run_json_command(command, timeout=timeout, label="subtitle packet probe")
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return ()
    normalized: list[tuple[Any, ...]] = []
    for packet in packets:
        if not isinstance(packet, dict):
            raise CompletedDeliveryError("subtitle packet probe returned malformed data")
        data_hash = str(packet.get("data_hash") or "")
        if not data_hash.startswith("SHA256:"):
            raise CompletedDeliveryError("subtitle packet probe did not return SHA-256 data hashes")
        normalized.append(
            (
                _rounded_time(packet.get("pts_time")),
                _rounded_time(packet.get("duration_time")),
                data_hash,
            )
        )
    return tuple(normalized)


def _run_json_command(command: list[str], *, timeout: float, label: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CompletedDeliveryError(f"{label} timed out after {timeout:g}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or f"{label} failed")[-2000:]
        raise CompletedDeliveryError(f"{label} failed: {detail}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CompletedDeliveryError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CompletedDeliveryError(f"{label} returned a non-object JSON payload")
    return payload


def _av_copy_hash(path: Path, *, timeout: float) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CompletedDeliveryError("ffmpeg executable is unavailable")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-f",
        "hash",
        "-hash",
        "sha256",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CompletedDeliveryError("audio/video packet hash timed out") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "audio/video packet hash failed")[-2000:]
        raise CompletedDeliveryError(f"audio/video packet hash failed: {detail}")
    value = str(completed.stdout or "").strip()
    if not value.startswith("SHA256=") or len(value) != 71:
        raise CompletedDeliveryError("audio/video packet hash returned malformed evidence")
    return value


def _validate_committed_receipt(
    receipt: dict[str, Any],
    *,
    source: Path,
    destination: Path,
    publication: dict[str, Any],
    timeout: float,
    verify_streams: bool = True,
) -> bool:
    try:
        if receipt.get("schema_version") != COMPLETED_DELIVERY_SCHEMA_VERSION:
            return False
        if receipt.get("contract") != COMPLETED_DELIVERY_CONTRACT or receipt.get("state") != "committed":
            return False
        if receipt.get("source_retained") is not True or not source.is_file():
            return False
        output = receipt.get("output")
        if not isinstance(output, dict) or str(output.get("path") or "") != str(destination):
            return False
        stat = destination.stat()
        if int(output.get("size") or -1) != int(stat.st_size):
            return False
        if int(output.get("mtime_ns") or -1) != int(stat.st_mtime_ns):
            return False
        if str(output.get("sha256") or "") != sha256_file(destination):
            return False
        if verify_streams:
            _verify_muxed_output(source, destination, publication, timeout=timeout)
        return True
    except (CompletedDeliveryError, OSError, TypeError, ValueError):
        return False


def _receipt_identity_matches(receipt: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(receipt.get(key) == value for key, value in expected.items())


def _marker_identity_matches(marker: dict[str, Any], expected: dict[str, Any]) -> bool:
    return (
        marker.get("schema_version") == COMPLETED_DELIVERY_SCHEMA_VERSION
        and marker.get("state") == "muxing"
        and _receipt_identity_matches(marker, expected)
        and isinstance(marker.get("attempt_id"), str)
        and bool(marker.get("attempt_id"))
    )


def _result_from_receipt(
    receipt: dict[str, Any], receipt_path: Path, *, recovered: bool
) -> CompletedDeliveryResult:
    output = receipt["output"]
    publication_manifest = receipt["publication_manifest"]
    return CompletedDeliveryResult(
        destination=str(output["path"]),
        receipt=str(receipt_path),
        output_sha256=str(output["sha256"]),
        output_size=int(output["size"]),
        publication_manifest_sha256=str(publication_manifest["sha256"]),
        recovered=recovered,
        source_retained=True,
    )


def _stream_signature(stream: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(stream.get("codec_type") or ""),
        str(stream.get("codec_name") or ""),
        str(stream.get("profile") or ""),
        int(stream.get("width") or 0),
        int(stream.get("height") or 0),
        int(stream.get("channels") or 0),
        str(stream.get("sample_rate") or ""),
    )


def _assert_source_identity(source: Path, expected: dict[str, Any]) -> None:
    try:
        stat = source.stat()
    except OSError as exc:
        raise CompletedDeliveryError("source media disappeared during mux") from exc
    if (
        int(stat.st_size) != int(expected.get("media_size") or -1)
        or int(stat.st_mtime_ns) != int(expected.get("media_mtime_ns") or -1)
        or sha256_file(source) != str(expected.get("sha256") or "")
    ):
        raise CompletedDeliveryError("source media changed during mux; staged artifact was not published")


def _assert_publication_unchanged(
    publication: dict[str, Any], manifest_path: Path, manifest_sha256: str
) -> None:
    if sha256_file(manifest_path) != manifest_sha256:
        raise CompletedDeliveryError("subtitle publication manifest changed during mux")
    for track in publication["tracks"]:
        path = Path(track["path"])
        if sha256_file(path) != str(track.get("sha256") or ""):
            raise CompletedDeliveryError(
                f"subtitle publication output changed during mux: {path}"
            )


def _validate_duration(source: dict[str, Any], output: dict[str, Any]) -> None:
    source_duration = _duration(source)
    output_duration = _duration(output)
    if source_duration is None or output_duration is None:
        raise CompletedDeliveryError("source or muxed MKV duration is unavailable")
    tolerance = max(1.0, source_duration * 0.002)
    if abs(source_duration - output_duration) > tolerance:
        raise CompletedDeliveryError(
            f"muxed MKV duration changed beyond tolerance: source={source_duration} output={output_duration}"
        )


def _duration(probe: dict[str, Any]) -> float | None:
    fmt = probe.get("format")
    if not isinstance(fmt, dict):
        return None
    try:
        value = float(fmt.get("duration"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _rounded_time(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CompletedDeliveryError("subtitle packet timing is unavailable") from exc
    if not math.isfinite(number):
        raise CompletedDeliveryError("subtitle packet timing is not finite")
    return round(number, 3)


def _positive_timeout(config: Any) -> float:
    try:
        value = float(getattr(config, "completed_delivery_timeout_seconds", 7200.0) or 0)
    except (TypeError, ValueError) as exc:
        raise CompletedDeliveryError("completed_delivery_timeout_seconds is invalid") from exc
    if not math.isfinite(value) or value <= 0:
        raise CompletedDeliveryError("completed_delivery_timeout_seconds must be greater than zero")
    return value


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise CompletedDeliveryError(f"durable delivery evidence is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise CompletedDeliveryError(f"durable delivery evidence is not an object: {path}")
    return payload


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
