from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Mapping, Sequence

from safe_files import atomic_write_bytes, atomic_write_text, fsync_directory, sha256_file


ASR_REVIEW_CHECKPOINT_SCHEMA_VERSION = 2
ASR_REVIEW_CHECKPOINT_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
ASR_REVIEW_CHECKPOINT_MANIFEST_NAME = "manifest.json"
ASR_REVIEW_CHECKPOINT_SRT_NAME = "rejected.srt"
ASR_REVIEW_CHECKPOINT_DIAGNOSTICS_NAME = "diagnostics.json"

_CHECKPOINT_ID_PATTERN = re.compile(r"asrchk_[0-9a-f]{64}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*")
_FINGERPRINT_KEYS = (
    "media_fingerprint",
    "audio_fingerprint",
    "audio_stream_fingerprint",
    "cache_fingerprint",
)
_MAX_REVIEW_RANGES = 64
_MAX_REVIEW_RANGE_END_SECONDS = 86400.0


class AsrReviewCheckpointError(RuntimeError):
    """Raised when an ASR review checkpoint cannot be trusted or published."""


@dataclass(frozen=True)
class AsrReviewCheckpoint:
    schema_version: int
    checkpoint_id: str
    manifest_path: Path
    manifest_sha256: str
    target_path: Path
    language: str
    review_ranges: tuple[tuple[float, float], ...]
    repair_fingerprint: str
    fingerprints: dict[str, dict[str, Any]]
    rejected_srt_path: Path
    rejected_srt_sha256: str
    diagnostics_path: Path
    diagnostics_sha256: str
    created_at: float


def asr_review_checkpoint_root(work_path: str | Path) -> Path:
    return Path(work_path) / "asr_review_checkpoints"


def create_asr_review_checkpoint(
    work_path: str | Path,
    *,
    target_path: str | Path,
    language: str,
    rejected_srt_path: str | Path,
    diagnostics_path: str | Path,
    review_ranges: Sequence[Sequence[float]] | None = None,
    repair_fingerprint: str = "",
    fingerprints: Mapping[str, Mapping[str, Any]] | None = None,
) -> AsrReviewCheckpoint:
    """Publish an immutable, content-addressed rejected-ASR checkpoint.

    The two artifacts are written first and ``manifest.json`` is the atomic
    publication marker.  An existing manifest is never repaired or replaced;
    it must pass the same strict loader before an idempotent caller can reuse it.
    """

    canonical_target = _canonical_target_path(target_path)
    normalized_language = _normalize_language(language)
    source_srt_bytes, source_srt_sha256 = _read_stable_file(
        Path(rejected_srt_path),
        label="rejected SRT",
    )
    source_diagnostics_bytes, source_diagnostics_sha256 = _read_stable_file(
        Path(diagnostics_path),
        label="ASR diagnostics",
    )
    diagnostics = _decode_json_object(
        source_diagnostics_bytes,
        label="ASR diagnostics",
    )
    (
        normalized_ranges,
        normalized_repair_fingerprint,
        normalized_fingerprints,
    ) = _validated_diagnostics_evidence(
        diagnostics,
        srt_sha256=source_srt_sha256,
        expected_review_ranges=review_ranges,
        expected_repair_fingerprint=repair_fingerprint or None,
        expected_fingerprints=fingerprints,
    )

    artifact_metadata = {
        "rejected_srt": {
            "file": ASR_REVIEW_CHECKPOINT_SRT_NAME,
            "sha256": source_srt_sha256,
            "size": len(source_srt_bytes),
        },
        "diagnostics": {
            "file": ASR_REVIEW_CHECKPOINT_DIAGNOSTICS_NAME,
            "sha256": source_diagnostics_sha256,
            "size": len(source_diagnostics_bytes),
        },
    }
    checkpoint_id = _checkpoint_id(
        target_path=canonical_target,
        language=normalized_language,
        review_ranges=normalized_ranges,
        repair_fingerprint=normalized_repair_fingerprint,
        fingerprints=normalized_fingerprints,
        artifacts=artifact_metadata,
    )
    shard_dir = (
        asr_review_checkpoint_root(work_path)
        / checkpoint_id.removeprefix("asrchk_")[:2]
    )
    checkpoint_dir = shard_dir / checkpoint_id
    manifest_path = checkpoint_dir / ASR_REVIEW_CHECKPOINT_MANIFEST_NAME
    if checkpoint_dir.is_symlink():
        raise AsrReviewCheckpointError(
            f"checkpoint directory must not be a symlink: {checkpoint_dir}"
        )
    if manifest_path.exists():
        return load_asr_review_checkpoint(
            manifest_path,
            expected_checkpoint_id=checkpoint_id,
            expected_target_path=canonical_target,
            expected_language=normalized_language,
            expected_review_ranges=normalized_ranges,
            expected_repair_fingerprint=normalized_repair_fingerprint,
            expected_fingerprints=normalized_fingerprints,
        )
    if checkpoint_dir.exists():
        raise AsrReviewCheckpointError(
            f"existing checkpoint directory is incomplete or unsafe: {checkpoint_dir}"
        )
    shard_dir.mkdir(parents=True, exist_ok=True)
    if shard_dir.is_symlink():
        raise AsrReviewCheckpointError(
            f"checkpoint shard directory must not be a symlink: {shard_dir}"
        )
    manifest = {
        "schema_version": ASR_REVIEW_CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "created_at": time.time(),
        "target": {
            "path": canonical_target,
            "language": normalized_language,
        },
        "evidence": {
            "review_ranges": _ranges_json(normalized_ranges),
            "repair_fingerprint": normalized_repair_fingerprint,
            "fingerprints": normalized_fingerprints,
        },
        "artifacts": artifact_metadata,
    }
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=f".stage-{checkpoint_id[-12:]}-",
            dir=shard_dir,
        )
    )
    try:
        atomic_write_bytes(
            staging_dir / ASR_REVIEW_CHECKPOINT_SRT_NAME,
            source_srt_bytes,
        )
        atomic_write_bytes(
            staging_dir / ASR_REVIEW_CHECKPOINT_DIAGNOSTICS_NAME,
            source_diagnostics_bytes,
        )
        atomic_write_text(
            staging_dir / ASR_REVIEW_CHECKPOINT_MANIFEST_NAME,
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
        )
        fsync_directory(staging_dir)
        try:
            staging_dir.rename(checkpoint_dir)
        except OSError as exc:
            # A concurrent publisher may have won the content-addressed name.
            # A complete winner is reused below; an incomplete one fails closed.
            if not checkpoint_dir.exists() and not checkpoint_dir.is_symlink():
                raise AsrReviewCheckpointError(
                    f"could not atomically publish checkpoint: {checkpoint_dir}"
                ) from exc
        fsync_directory(shard_dir)
    finally:
        _discard_staging_checkpoint(staging_dir)
    return load_asr_review_checkpoint(
        manifest_path,
        expected_checkpoint_id=checkpoint_id,
        expected_target_path=canonical_target,
        expected_language=normalized_language,
        expected_review_ranges=normalized_ranges,
        expected_repair_fingerprint=normalized_repair_fingerprint,
        expected_fingerprints=normalized_fingerprints,
    )


def load_asr_review_checkpoint(
    manifest_path: str | Path,
    *,
    expected_manifest_sha256: str = "",
    expected_checkpoint_id: str = "",
    expected_target_path: str | Path | None = None,
    expected_language: str = "",
    expected_review_ranges: Sequence[Sequence[float]] | None = None,
    expected_repair_fingerprint: str = "",
    expected_fingerprints: Mapping[str, Mapping[str, Any]] | None = None,
) -> AsrReviewCheckpoint:
    """Load schema v1/v2 and reject every unbound or hash-mismatched input."""

    manifest = Path(manifest_path)
    if manifest.parent.is_symlink():
        raise AsrReviewCheckpointError(
            f"checkpoint directory must not be a symlink: {manifest.parent}"
        )
    manifest_bytes, manifest_sha256 = _read_stable_file(
        manifest,
        label="ASR review checkpoint manifest",
    )
    if expected_manifest_sha256:
        expected_digest = _normalize_sha256(
            expected_manifest_sha256,
            label="expected manifest SHA-256",
        )
        if manifest_sha256 != expected_digest:
            raise AsrReviewCheckpointError(
                "ASR review checkpoint manifest SHA-256 does not match the expected digest"
            )
    payload = _decode_json_object(
        manifest_bytes,
        label="ASR review checkpoint manifest",
    )
    fields = _manifest_fields(payload)

    checkpoint_id = str(fields["checkpoint_id"])
    if expected_checkpoint_id and checkpoint_id != str(expected_checkpoint_id):
        raise AsrReviewCheckpointError("ASR review checkpoint id does not match the expected id")
    target_path = _canonical_target_path(str(fields["target_path"]))
    language = _normalize_language(str(fields["language"]))
    review_ranges = normalize_asr_review_ranges(fields["review_ranges"])
    repair_fingerprint = _normalize_sha256(
        fields["repair_fingerprint"],
        label="repair fingerprint",
    )
    fingerprints = _normalize_fingerprints(fields["fingerprints"])
    created_at = _positive_finite_float(fields["created_at"], label="checkpoint created_at")

    artifact_fields = fields["artifacts"]
    rejected_srt_path, rejected_srt_bytes, rejected_srt_sha256 = _load_artifact(
        manifest.parent,
        artifact_fields["rejected_srt"],
        label="rejected SRT",
    )
    diagnostics_path, diagnostics_bytes, diagnostics_sha256 = _load_artifact(
        manifest.parent,
        artifact_fields["diagnostics"],
        label="ASR diagnostics",
    )
    diagnostics = _decode_json_object(diagnostics_bytes, label="ASR diagnostics")
    _validated_diagnostics_evidence(
        diagnostics,
        srt_sha256=rejected_srt_sha256,
        expected_review_ranges=review_ranges,
        expected_repair_fingerprint=repair_fingerprint,
        expected_fingerprints=fingerprints,
    )

    computed_checkpoint_id = _checkpoint_id(
        target_path=target_path,
        language=language,
        review_ranges=review_ranges,
        repair_fingerprint=repair_fingerprint,
        fingerprints=fingerprints,
        artifacts=artifact_fields,
    )
    if checkpoint_id != computed_checkpoint_id:
        raise AsrReviewCheckpointError(
            "ASR review checkpoint id does not match its immutable evidence"
        )

    if (
        expected_target_path is not None
        and target_path != _canonical_target_path(expected_target_path)
    ):
        raise AsrReviewCheckpointError("checkpoint target path does not match the expected target")
    if expected_language and language != _normalize_language(expected_language):
        raise AsrReviewCheckpointError("checkpoint language does not match the expected language")
    if expected_review_ranges is not None and review_ranges != normalize_asr_review_ranges(
        expected_review_ranges
    ):
        raise AsrReviewCheckpointError("checkpoint review ranges do not match expected evidence")
    if expected_repair_fingerprint and repair_fingerprint != _normalize_sha256(
        expected_repair_fingerprint,
        label="expected repair fingerprint",
    ):
        raise AsrReviewCheckpointError(
            "checkpoint repair fingerprint does not match expected evidence"
        )
    if expected_fingerprints is not None and fingerprints != _normalize_fingerprints(
        expected_fingerprints
    ):
        raise AsrReviewCheckpointError("checkpoint fingerprints do not match expected evidence")

    return AsrReviewCheckpoint(
        schema_version=int(fields["schema_version"]),
        checkpoint_id=checkpoint_id,
        manifest_path=manifest,
        manifest_sha256=manifest_sha256,
        target_path=Path(target_path),
        language=language,
        review_ranges=review_ranges,
        repair_fingerprint=repair_fingerprint,
        fingerprints=fingerprints,
        rejected_srt_path=rejected_srt_path,
        rejected_srt_sha256=rejected_srt_sha256,
        diagnostics_path=diagnostics_path,
        diagnostics_sha256=diagnostics_sha256,
        created_at=created_at,
    )


def normalize_asr_review_ranges(
    raw_ranges: Sequence[Sequence[float]] | object,
) -> tuple[tuple[float, float], ...]:
    if isinstance(raw_ranges, (str, bytes, bytearray)):
        raise AsrReviewCheckpointError("review ranges must be a list of pairs")
    try:
        items = list(raw_ranges)  # type: ignore[arg-type]
    except TypeError as exc:
        raise AsrReviewCheckpointError("review ranges must be a list of pairs") from exc
    if not items or len(items) > _MAX_REVIEW_RANGES:
        raise AsrReviewCheckpointError(
            f"review ranges must contain 1-{_MAX_REVIEW_RANGES} pairs"
        )
    valid: list[tuple[float, float]] = []
    for index, item in enumerate(items):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise AsrReviewCheckpointError(f"review range {index} must contain start and end")
        if isinstance(item[0], bool) or isinstance(item[1], bool):
            raise AsrReviewCheckpointError(f"review range {index} must contain numeric values")
        try:
            start = float(item[0])
            end = float(item[1])
        except (TypeError, ValueError) as exc:
            raise AsrReviewCheckpointError(
                f"review range {index} must contain numeric values"
            ) from exc
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or end > _MAX_REVIEW_RANGE_END_SECONDS
        ):
            raise AsrReviewCheckpointError(f"review range {index} is outside safe bounds")
        valid.append((start, end))
    valid.sort()
    merged: list[list[float]] = []
    for start, end in valid:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def _manifest_fields(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        schema_version = int(payload.get("schema_version"))
    except (TypeError, ValueError) as exc:
        raise AsrReviewCheckpointError("checkpoint manifest schema version is invalid") from exc
    if schema_version not in ASR_REVIEW_CHECKPOINT_SUPPORTED_SCHEMA_VERSIONS:
        raise AsrReviewCheckpointError(
            f"unsupported ASR review checkpoint schema version: {schema_version}"
        )
    checkpoint_id = str(payload.get("checkpoint_id") or "").strip().casefold()
    if _CHECKPOINT_ID_PATTERN.fullmatch(checkpoint_id) is None:
        raise AsrReviewCheckpointError("checkpoint manifest id is invalid")
    if schema_version == 1:
        artifacts = {
            "rejected_srt": _normalize_artifact_entry(payload.get("rejected_srt"), "rejected SRT"),
            "diagnostics": _normalize_artifact_entry(payload.get("diagnostics"), "ASR diagnostics"),
        }
        return {
            "schema_version": schema_version,
            "checkpoint_id": checkpoint_id,
            "created_at": payload.get("created_at"),
            "target_path": payload.get("target_path"),
            "language": payload.get("language"),
            "review_ranges": payload.get("review_ranges"),
            "repair_fingerprint": payload.get("repair_fingerprint"),
            "fingerprints": payload.get("fingerprints"),
            "artifacts": artifacts,
        }

    target = payload.get("target")
    evidence = payload.get("evidence")
    raw_artifacts = payload.get("artifacts")
    if not isinstance(target, dict) or not isinstance(evidence, dict) or not isinstance(raw_artifacts, dict):
        raise AsrReviewCheckpointError("schema v2 checkpoint manifest sections are invalid")
    artifacts = {
        "rejected_srt": _normalize_artifact_entry(raw_artifacts.get("rejected_srt"), "rejected SRT"),
        "diagnostics": _normalize_artifact_entry(raw_artifacts.get("diagnostics"), "ASR diagnostics"),
    }
    return {
        "schema_version": schema_version,
        "checkpoint_id": checkpoint_id,
        "created_at": payload.get("created_at"),
        "target_path": target.get("path"),
        "language": target.get("language"),
        "review_ranges": evidence.get("review_ranges"),
        "repair_fingerprint": evidence.get("repair_fingerprint"),
        "fingerprints": evidence.get("fingerprints"),
        "artifacts": artifacts,
    }


def _normalize_artifact_entry(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AsrReviewCheckpointError(f"{label} artifact metadata is missing")
    file_name = str(raw.get("file") or raw.get("path") or "").strip()
    if (
        not file_name
        or file_name in {".", ".."}
        or "/" in file_name
        or "\\" in file_name
        or Path(file_name).name != file_name
    ):
        raise AsrReviewCheckpointError(f"{label} artifact path is unsafe")
    digest = _normalize_sha256(raw.get("sha256"), label=f"{label} SHA-256")
    try:
        size = int(raw.get("size"))
    except (TypeError, ValueError) as exc:
        raise AsrReviewCheckpointError(f"{label} artifact size is invalid") from exc
    if size <= 0:
        raise AsrReviewCheckpointError(f"{label} artifact size must be positive")
    return {"file": file_name, "sha256": digest, "size": size}


def _load_artifact(
    manifest_directory: Path,
    metadata: Mapping[str, Any],
    *,
    label: str,
) -> tuple[Path, bytes, str]:
    file_name = str(metadata["file"])
    artifact = manifest_directory / file_name
    try:
        resolved_directory = manifest_directory.resolve()
        resolved_artifact = artifact.resolve()
    except OSError as exc:
        raise AsrReviewCheckpointError(f"could not resolve {label} artifact path") from exc
    if resolved_artifact.parent != resolved_directory or artifact.is_symlink():
        raise AsrReviewCheckpointError(f"{label} artifact escapes the checkpoint directory")
    content, digest = _read_stable_file(artifact, label=label)
    if len(content) != int(metadata["size"]):
        raise AsrReviewCheckpointError(f"{label} artifact size does not match its manifest")
    if digest != str(metadata["sha256"]):
        raise AsrReviewCheckpointError(f"{label} artifact SHA-256 does not match its manifest")
    return artifact, content, digest


def _validated_diagnostics_evidence(
    diagnostics: Mapping[str, Any],
    *,
    srt_sha256: str,
    expected_review_ranges: Sequence[Sequence[float]] | None,
    expected_repair_fingerprint: str | None,
    expected_fingerprints: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[tuple[tuple[float, float], ...], str, dict[str, dict[str, Any]]]:
    diagnosed_srt_sha256 = _normalize_sha256(
        diagnostics.get("srt_sha256"),
        label="diagnostics SRT SHA-256",
    )
    if diagnosed_srt_sha256 != srt_sha256:
        raise AsrReviewCheckpointError(
            "ASR diagnostics are not bound to the rejected SRT SHA-256"
        )
    review_ranges = normalize_asr_review_ranges(diagnostics.get("review_ranges"))
    if expected_review_ranges is not None and review_ranges != normalize_asr_review_ranges(
        expected_review_ranges
    ):
        raise AsrReviewCheckpointError(
            "ASR diagnostics review ranges do not match expected evidence"
        )
    repair_fingerprint = _normalize_sha256(
        diagnostics.get("repair_fingerprint"),
        label="diagnostics repair fingerprint",
    )
    if expected_repair_fingerprint and repair_fingerprint != _normalize_sha256(
        expected_repair_fingerprint,
        label="expected repair fingerprint",
    ):
        raise AsrReviewCheckpointError(
            "ASR diagnostics repair fingerprint does not match expected evidence"
        )
    fingerprints = _normalize_fingerprints(
        {key: diagnostics.get(key) for key in _FINGERPRINT_KEYS}
    )
    if expected_fingerprints is not None and fingerprints != _normalize_fingerprints(
        expected_fingerprints
    ):
        raise AsrReviewCheckpointError(
            "ASR diagnostics fingerprints do not match expected evidence"
        )
    return review_ranges, repair_fingerprint, fingerprints


def _normalize_fingerprints(
    raw: Mapping[str, Mapping[str, Any]] | object,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise AsrReviewCheckpointError("checkpoint fingerprints must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for key in _FINGERPRINT_KEYS:
        value = raw.get(key)
        if not isinstance(value, Mapping):
            raise AsrReviewCheckpointError(f"checkpoint {key} is missing or invalid")
        try:
            canonical = json.loads(
                json.dumps(
                    dict(value),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as exc:
            raise AsrReviewCheckpointError(f"checkpoint {key} is not valid JSON evidence") from exc
        if not isinstance(canonical, dict):
            raise AsrReviewCheckpointError(f"checkpoint {key} is invalid")
        canonical["fingerprint"] = _normalize_sha256(
            canonical.get("fingerprint"),
            label=f"checkpoint {key}",
        )
        normalized[key] = canonical
    return normalized


def _checkpoint_id(
    *,
    target_path: str,
    language: str,
    review_ranges: tuple[tuple[float, float], ...],
    repair_fingerprint: str,
    fingerprints: Mapping[str, Mapping[str, Any]],
    artifacts: Mapping[str, Mapping[str, Any]],
) -> str:
    identity = {
        "target_path": target_path,
        "language": language,
        "review_ranges": _ranges_json(review_ranges),
        "repair_fingerprint": repair_fingerprint,
        "fingerprints": fingerprints,
        "artifacts": {
            key: {
                "sha256": str(artifacts[key]["sha256"]),
                "size": int(artifacts[key]["size"]),
            }
            for key in ("rejected_srt", "diagnostics")
        },
    }
    digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    return f"asrchk_{digest}"


def _canonical_json(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AsrReviewCheckpointError("checkpoint evidence is not canonical JSON") from exc


def _ranges_json(
    ranges: Sequence[Sequence[float]],
) -> list[list[float]]:
    return [[float(start), float(end)] for start, end in ranges]


def _discard_staging_checkpoint(directory: Path) -> None:
    """Best-effort cleanup of the fixed files in our private staging directory."""

    for file_name in (
        ASR_REVIEW_CHECKPOINT_MANIFEST_NAME,
        ASR_REVIEW_CHECKPOINT_DIAGNOSTICS_NAME,
        ASR_REVIEW_CHECKPOINT_SRT_NAME,
    ):
        try:
            (directory / file_name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


def _read_stable_file(path: Path, *, label: str) -> tuple[bytes, str]:
    if path.is_symlink() or not path.is_file():
        raise AsrReviewCheckpointError(f"{label} must be a regular file: {path}")
    try:
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
        digest = hashlib.sha256(content).hexdigest()
        verified_digest = sha256_file(path)
    except OSError as exc:
        raise AsrReviewCheckpointError(f"could not read {label}: {path}") from exc
    if not content:
        raise AsrReviewCheckpointError(f"{label} must not be empty: {path}")
    if (
        int(before.st_size) != len(content)
        or int(after.st_size) != len(content)
        or int(before.st_mtime_ns) != int(after.st_mtime_ns)
        or digest != verified_digest
    ):
        raise AsrReviewCheckpointError(f"{label} changed while it was being read: {path}")
    return content, digest


def _decode_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        decoded = content.decode("utf-8-sig")
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
    except AsrReviewCheckpointError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AsrReviewCheckpointError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise AsrReviewCheckpointError(f"{label} must contain a JSON object")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AsrReviewCheckpointError(f"checkpoint JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _canonical_target_path(path: str | Path) -> str:
    raw = str(path or "").strip()
    candidate = Path(raw)
    if not raw or not candidate.is_absolute():
        raise AsrReviewCheckpointError("checkpoint target path must be absolute")
    try:
        return os.path.normcase(str(candidate.resolve(strict=False)))
    except OSError as exc:
        raise AsrReviewCheckpointError("checkpoint target path could not be resolved") from exc


def _normalize_language(language: object) -> str:
    normalized = str(language or "").strip().replace("_", "-").casefold()
    if _LANGUAGE_PATTERN.fullmatch(normalized) is None:
        raise AsrReviewCheckpointError("checkpoint language is invalid")
    return normalized


def _normalize_sha256(value: object, *, label: str) -> str:
    normalized = str(value or "").strip().casefold()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise AsrReviewCheckpointError(f"{label} must be a SHA-256 hex digest")
    return normalized


def _positive_finite_float(value: object, *, label: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise AsrReviewCheckpointError(f"{label} is invalid") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise AsrReviewCheckpointError(f"{label} must be a positive finite number")
    return normalized
