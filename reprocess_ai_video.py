from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from ai_failure_markers import ai_failure_marker_path, clear_ai_failure_marker
from completed_delivery import (
    COMPLETED_DELIVERY_CONTRACT,
    COMPLETED_DELIVERY_SCHEMA_VERSION,
    completed_delivery_destination,
    completed_delivery_enabled,
    completed_delivery_marker_path,
    completed_delivery_receipt_path,
)
from config import AppConfig, load_config
from lock import VideoLock
from output_manifest import (
    delivery_identity,
    output_manifest_path,
    output_publication_marker_path,
)
from scan_state import ScanStateStore, ai_delivery_identity
from safe_files import atomic_write_text, sha256_file, verified_move
from subtitle_paths import paths_for_video, source_transcript_artifacts_for_video
from subtitle_quality import quality_report_candidates
from transcriber import asr_diagnostics_path, asr_transcription_hold_path
from translation_quality import (
    translation_quality_events_path,
    translation_quality_hold_path,
)


VALID_MODES = {"retranslate", "retranscribe"}
VALID_QUEUE_MODES = {"manual_force", "auto_review"}
MANIFEST_SCHEMA_VERSION = 2
MANIFEST_NAME = "manifest.json"


class ReprocessRollbackError(RuntimeError):
    """Raised when a failed reprocess operation cannot be rolled back fully."""


def reprocess_video(
    config: AppConfig | Any,
    video_path: str | Path,
    *,
    mode: str,
    queue_mode: str = "manual_force",
    expected_failure_revision: str = "",
    policy_revision: str = "",
) -> dict[str, Any]:
    normalized_mode = _validated_mode(mode)
    normalized_queue_mode = str(queue_mode or "manual_force").strip().casefold()
    if normalized_queue_mode not in VALID_QUEUE_MODES:
        raise ValueError(f"unsupported reprocess queue mode: {queue_mode}")
    normalized_failure_revision = str(expected_failure_revision or "").strip()
    normalized_policy_revision = str(policy_revision or "").strip()
    if normalized_queue_mode == "auto_review" and (
        not normalized_failure_revision or not normalized_policy_revision
    ):
        raise ValueError(
            "automatic review reprocess requires failure and policy revisions"
        )
    video = _validated_video(config, video_path)

    lock = VideoLock(video)
    if not lock.acquire():
        raise RuntimeError(f"video is currently being processed: {video}")
    try:
        _assert_current_delivery_reprocessable(config, video)
        completed_required, completed_hashes = _validate_completed_delivery_for_reprocess(
            config,
            video,
        )
        candidates = _reprocess_candidates(
            config,
            video,
            mode=normalized_mode,
            completed_paths=completed_required,
        )
        digest = hashlib.sha1(str(video).encode("utf-8", errors="replace")).hexdigest()[:16]
        archive_dir = (
            Path(config.work_path)
            / "manual_ai_reprocess"
            / f"{time.time_ns()}-{digest}-{normalized_mode}"
        )
        entries = _prepare_archive_entries(
            candidates,
            archive_dir,
            precomputed_sha256=completed_hashes,
        )
        _assert_required_archive_entries(completed_required, entries)
        _assert_completed_delivery_snapshot(config, video, completed_required)
        archive_dir.mkdir(parents=True, exist_ok=False)
        manifest_path = archive_dir / MANIFEST_NAME
        manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "operation": "reprocess_archive",
            "status": "prepared",
            "strategy": (
                "full_transcription_rerun"
                if normalized_mode == "retranscribe"
                else "translation_only_rerun"
            ),
            "queue_mode": normalized_queue_mode,
            "policy_revision": normalized_policy_revision,
            "expected_failure_revision": normalized_failure_revision,
            "selective": False,
            "mode": normalized_mode,
            "video": str(video),
            "created_at": time.time(),
            "entries": entries,
            "moved": [],
            "failures": [],
        }
        _write_manifest(manifest_path, manifest)

        moved: list[dict[str, Any]] = []
        try:
            manifest["status"] = "archiving"
            _write_manifest(manifest_path, manifest)
            for entry in entries:
                source = Path(str(entry["source"]))
                archive = Path(str(entry["archive"]))
                expected_sha256 = str(entry["sha256"])
                if sha256_file(source) != expected_sha256:
                    raise RuntimeError(f"source changed after reprocess manifest preparation: {source}")
                verified_move(source, archive)
                moved.append(entry)
                archive_sha256 = sha256_file(archive)
                if archive_sha256 != expected_sha256:
                    raise RuntimeError(f"archived output checksum mismatch: {archive}")
                entry["state"] = "moved"
                entry["archive_sha256"] = archive_sha256
                entry["moved_at"] = time.time()
                manifest["moved"] = [_public_archive_entry(item) for item in moved]
                _write_manifest(manifest_path, manifest)

            result = {
                "ok": True,
                "mode": normalized_mode,
                "video": str(video),
                "strategy": str(manifest["strategy"]),
                "queue_mode": normalized_queue_mode,
                "policy_revision": normalized_policy_revision,
                "selective": False,
                "moved_outputs": len(moved),
                "archive": str(archive_dir),
                "manifest": str(manifest_path),
                "moved": [_public_archive_entry(entry) for entry in moved],
            }
            manifest.update(result)
            manifest["status"] = "complete"
            manifest["completed_at"] = time.time()
            _write_manifest(manifest_path, manifest)

            state = ScanStateStore.from_config(config)
            committed = False
            try:
                if normalized_queue_mode == "auto_review":
                    queued = state.queue_paused_review_remediation(
                        video,
                        expected_failure_revision=normalized_failure_revision,
                        policy_revision=normalized_policy_revision,
                    )
                    if not queued:
                        raise RuntimeError(
                            "paused review queue state changed before automatic remediation"
                        )
                else:
                    state.force_ai_queue_candidate(video)
                state.commit()
                committed = True
            except Exception:
                try:
                    state.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    state.close()
                except Exception:
                    if not committed:
                        raise

            # Clearing the cooldown marker happens only after the durable queue
            # commit. The helper is intentionally best-effort and does not
            # introduce a post-commit rollback boundary.
            clear_ai_failure_marker(config, video)
            result["manifest_sha256"] = sha256_file(manifest_path)
            return result
        except Exception as operation_error:
            rollback_failures = _rollback_archive_entries(moved)
            manifest["status"] = "rollback_failed" if rollback_failures else "rolled_back"
            manifest["failed_at"] = time.time()
            manifest["failures"] = [
                {
                    "stage": "archive_or_queue",
                    "error": f"{type(operation_error).__name__}: {operation_error}",
                },
                *rollback_failures,
            ]
            manifest["moved"] = [_public_archive_entry(entry) for entry in moved]
            try:
                _write_manifest(manifest_path, manifest)
            except Exception as manifest_error:
                rollback_failures.append(
                    {
                        "stage": "rollback_manifest",
                        "error": f"{type(manifest_error).__name__}: {manifest_error}",
                    }
                )
            if rollback_failures:
                raise ReprocessRollbackError(
                    "AI reprocess failed and verified rollback was incomplete; "
                    f"manual recovery manifest: {manifest_path}"
                ) from operation_error
            raise
    finally:
        lock.release()


def restore_reprocess_manifest(
    config: AppConfig | Any,
    manifest_path: str | Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Restore a completed reprocess archive after checksum validation.

    The current managed outputs are first moved into their own recoverable
    archive. Any failure while restoring the requested archive reverses both
    sets of moves, leaving the pre-restore state intact whenever possible.
    """

    source_manifest = Path(manifest_path).resolve()
    actual_manifest_sha256 = sha256_file(source_manifest)
    expected_digest = str(expected_manifest_sha256 or "").strip().casefold()
    if expected_digest and actual_manifest_sha256.casefold() != expected_digest:
        raise RuntimeError(
            "reprocess manifest checksum mismatch: "
            f"expected={expected_digest} actual={actual_manifest_sha256}"
        )

    payload = _read_reprocess_manifest(config, source_manifest)
    video = _validated_video(config, str(payload["video"]))
    mode = _validated_mode(str(payload["mode"]))
    restore_entries = _validated_restore_entries(
        config,
        video,
        mode=mode,
        manifest_path=source_manifest,
        payload=payload,
    )

    # Validate every requested archive before touching the currently published
    # outputs. A missing or tampered archive is therefore a zero-mutation error.
    for entry in restore_entries:
        archive = Path(str(entry["archive"]))
        expected_sha256 = str(entry["sha256"])
        if not archive.is_file() or sha256_file(archive) != expected_sha256:
            raise RuntimeError(f"archived restore checksum mismatch: {archive}")

    lock = VideoLock(video)
    if not lock.acquire():
        raise RuntimeError(f"video is currently being processed: {video}")
    try:
        digest = hashlib.sha1(str(video).encode("utf-8", errors="replace")).hexdigest()[:16]
        restore_dir = (
            Path(config.work_path)
            / "manual_ai_reprocess_restore"
            / f"{time.time_ns()}-{digest}-{mode}"
        )
        completed_required, completed_hashes = _validate_completed_delivery_for_reprocess(
            config,
            video,
        )
        current_entries = _prepare_archive_entries(
            _reprocess_candidates(
                config,
                video,
                mode=mode,
                completed_paths=completed_required,
            ),
            restore_dir,
            precomputed_sha256=completed_hashes,
        )
        _assert_required_archive_entries(completed_required, current_entries)
        _assert_completed_delivery_snapshot(config, video, completed_required)
        restore_dir.mkdir(parents=True, exist_ok=False)
        restore_manifest_path = restore_dir / MANIFEST_NAME
        restore_manifest: dict[str, Any] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "operation": "reprocess_restore",
            "status": "prepared",
            "mode": mode,
            "video": str(video),
            "created_at": time.time(),
            "source_manifest": str(source_manifest),
            "source_manifest_sha256": actual_manifest_sha256,
            "current_entries": current_entries,
            "restore_entries": [
                {
                    **_public_archive_entry(entry),
                    "state": "pending",
                }
                for entry in restore_entries
            ],
            "backed_up": [],
            "restored": [],
            "failures": [],
        }
        _write_manifest(restore_manifest_path, restore_manifest)

        backed_up: list[dict[str, Any]] = []
        restored: list[dict[str, Any]] = []
        try:
            restore_manifest["status"] = "backing_up_current_outputs"
            _write_manifest(restore_manifest_path, restore_manifest)
            for entry in current_entries:
                source = Path(str(entry["source"]))
                archive = Path(str(entry["archive"]))
                expected_sha256 = str(entry["sha256"])
                if sha256_file(source) != expected_sha256:
                    raise RuntimeError(f"current output changed during restore preparation: {source}")
                verified_move(source, archive)
                backed_up.append(entry)
                if sha256_file(archive) != expected_sha256:
                    raise RuntimeError(f"current output backup checksum mismatch: {archive}")
                entry["state"] = "moved"
                entry["archive_sha256"] = expected_sha256
                entry["moved_at"] = time.time()
                restore_manifest["backed_up"] = [
                    _public_archive_entry(item) for item in backed_up
                ]
                _write_manifest(restore_manifest_path, restore_manifest)

            restore_manifest["status"] = "restoring"
            _write_manifest(restore_manifest_path, restore_manifest)
            for index, entry in enumerate(restore_entries):
                source = Path(str(entry["source"]))
                archive = Path(str(entry["archive"]))
                expected_sha256 = str(entry["sha256"])
                verified_move(archive, source)
                restored.append(entry)
                if sha256_file(source) != expected_sha256:
                    raise RuntimeError(f"restored output checksum mismatch: {source}")
                restore_manifest["restore_entries"][index]["state"] = "restored"
                restore_manifest["restore_entries"][index]["restored_at"] = time.time()
                restore_manifest["restored"] = [
                    _public_archive_entry(item) for item in restored
                ]
                _write_manifest(restore_manifest_path, restore_manifest)

            restore_manifest["status"] = "complete"
            restore_manifest["completed_at"] = time.time()
            _write_manifest(restore_manifest_path, restore_manifest)
            return {
                "ok": True,
                "operation": "restore",
                "mode": mode,
                "video": str(video),
                "restored_outputs": len(restored),
                "source_manifest": str(source_manifest),
                "source_manifest_sha256": actual_manifest_sha256,
                "current_outputs_archive": str(restore_dir),
                "manifest": str(restore_manifest_path),
                "manifest_sha256": sha256_file(restore_manifest_path),
                "restored": [_public_archive_entry(entry) for entry in restored],
            }
        except Exception as operation_error:
            rollback_failures = _rollback_restore_entries(restored, backed_up)
            restore_manifest["status"] = (
                "rollback_failed" if rollback_failures else "rolled_back"
            )
            restore_manifest["failed_at"] = time.time()
            restore_manifest["failures"] = [
                {
                    "stage": "restore",
                    "error": f"{type(operation_error).__name__}: {operation_error}",
                },
                *rollback_failures,
            ]
            restore_manifest["backed_up"] = [
                _public_archive_entry(item) for item in backed_up
            ]
            restore_manifest["restored"] = [
                _public_archive_entry(item) for item in restored
            ]
            try:
                _write_manifest(restore_manifest_path, restore_manifest)
            except Exception as manifest_error:
                rollback_failures.append(
                    {
                        "stage": "rollback_manifest",
                        "error": f"{type(manifest_error).__name__}: {manifest_error}",
                    }
                )
            if rollback_failures:
                raise ReprocessRollbackError(
                    "AI reprocess restore failed and verified rollback was incomplete; "
                    f"manual recovery manifest: {restore_manifest_path}"
                ) from operation_error
            raise
    finally:
        lock.release()


def _validated_mode(mode: str) -> str:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in VALID_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    return normalized_mode


def _validated_video(config: AppConfig | Any, video_path: str | Path) -> Path:
    video = Path(video_path).resolve()
    input_root = Path(config.input_path).resolve()
    if not video.is_relative_to(input_root):
        raise ValueError(f"video path must stay inside input_path: {video}")
    if not video.is_file():
        raise FileNotFoundError(f"video does not exist: {video}")
    allowed_extensions = {
        str(extension).strip().casefold()
        for extension in getattr(config, "video_extensions", ())
        if str(extension).strip()
    }
    if video.suffix.casefold() not in allowed_extensions:
        raise ValueError(f"unsupported video extension: {video.suffix}")
    return video


def _reprocess_candidates(
    config: AppConfig | Any,
    video: Path,
    *,
    mode: str,
    completed_paths: set[Path] | None = None,
) -> list[Path]:
    paths = paths_for_video(video, config)
    candidates = {
        paths.zh_cn_srt,
        paths.zh_tw_srt,
        paths.ai_zh_cn_ass,
        paths.ai_zh_tw_ass,
        translation_quality_events_path(paths.zh_cn_srt),
        translation_quality_hold_path(paths.zh_cn_srt),
        ai_failure_marker_path(config, video),
        output_manifest_path(video, config),
        output_publication_marker_path(video, config),
    }
    if mode == "retranscribe":
        candidates.update({paths.ja_srt, paths.ai_ja_ass})
        candidates.add(asr_diagnostics_path(paths.ja_srt, config))
        candidates.add(asr_transcription_hold_path(paths.ja_srt, config))
        candidates.add(paths.ja_srt.with_name(f"{paths.ja_srt.stem}.gaps.txt"))
        for source_artifact in source_transcript_artifacts_for_video(video, config):
            candidates.add(source_artifact)
            if source_artifact.suffix.casefold() != ".srt":
                continue
            candidates.add(asr_diagnostics_path(source_artifact, config))
            candidates.add(asr_transcription_hold_path(source_artifact, config))
            candidates.add(
                source_artifact.with_name(f"{source_artifact.stem}.gaps.txt")
            )
    report_candidates: set[Path] = set()
    for path in list(candidates):
        report_candidates.update(quality_report_candidates(path, config.work_path))
    candidates.update(report_candidates)
    candidates.update(completed_paths or set())
    return sorted(candidates, key=lambda path: str(path).casefold())


def _assert_current_delivery_reprocessable(
    config: AppConfig | Any,
    video: Path,
) -> None:
    identity = delivery_identity(video, config)
    state = ScanStateStore.from_config(config)
    try:
        obligation = state.get_ai_delivery_obligation(str(identity["obligation_id"]))
    finally:
        state.close()
    if obligation is None or obligation["state"] == "open":
        return
    if obligation["state"] == "excluded" and int(obligation["attempt_count"]) == 0:
        return
    raise RuntimeError(
        "current AI delivery identity is terminal and cannot be reprocessed in place: "
        f"obligation_id={identity['obligation_id']} state={obligation['state']}"
    )


def _completed_delivery_managed_paths(
    config: AppConfig | Any,
    video: Path,
) -> set[Path]:
    if not completed_delivery_enabled(config):
        return set()
    return {
        completed_delivery_destination(video, config),
        completed_delivery_receipt_path(video, config),
    }


def _validate_completed_delivery_for_reprocess(
    config: AppConfig | Any,
    video: Path,
) -> tuple[set[Path], dict[str, str]]:
    managed = _completed_delivery_managed_paths(config, video)
    if not managed:
        return set(), {}
    destination = completed_delivery_destination(video, config)
    receipt = completed_delivery_receipt_path(video, config)
    marker = completed_delivery_marker_path(video, config)
    if marker.exists():
        raise RuntimeError(
            f"completed delivery is incomplete and must be recovered before reprocess: {marker}"
        )
    if destination.exists() != receipt.exists():
        raise RuntimeError(
            "completed delivery artifact and receipt must either both exist or both be absent"
        )
    if not destination.exists():
        return set(), {}
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"completed delivery receipt is unreadable: {receipt}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"completed delivery receipt is malformed: {receipt}")
    output = payload.get("output")
    source = payload.get("source")
    delivery = payload.get("delivery")
    publication_manifest = payload.get("publication_manifest")
    video_stat = video.stat()
    current_media = delivery_identity(video, config)["media"]
    source_sha256 = sha256_file(video)
    destination_sha256 = sha256_file(destination)
    receipt_sha256 = sha256_file(receipt)
    stored_policy_revision = (
        str(delivery.get("policy_revision") or "")
        if isinstance(delivery, dict)
        else ""
    )
    stored_identity = (
        ai_delivery_identity(
            video,
            media_size=int(video_stat.st_size),
            media_mtime_ns=int(video_stat.st_mtime_ns),
            policy_revision=stored_policy_revision,
        )
        if stored_policy_revision
        else None
    )
    configured_manifest = output_manifest_path(video, config).resolve()
    manifest_matches = bool(
        isinstance(publication_manifest, dict)
        and publication_manifest.get("path") == str(configured_manifest)
        and len(str(publication_manifest.get("sha256") or "")) == 64
        and all(
            character in "0123456789abcdef"
            for character in str(publication_manifest.get("sha256") or "").casefold()
        )
    )
    if configured_manifest.exists() and manifest_matches:
        manifest_matches = (
            sha256_file(configured_manifest)
            == str(publication_manifest.get("sha256") or "").casefold()
        )
    if not (
        payload.get("schema_version") == COMPLETED_DELIVERY_SCHEMA_VERSION
        and payload.get("contract") == COMPLETED_DELIVERY_CONTRACT
        and payload.get("state") == "committed"
        and isinstance(source, dict)
        and source.get("canonical_path") == str(video.resolve())
        and int(source.get("media_size") or -1) == int(video_stat.st_size)
        and int(source.get("media_mtime_ns") or -1) == int(video_stat.st_mtime_ns)
        and source.get("media_fingerprint") == current_media["media_fingerprint"]
        and str(source.get("sha256") or "").casefold() == source_sha256
        and isinstance(delivery, dict)
        and stored_identity is not None
        and delivery.get("obligation_id") == stored_identity["obligation_id"]
        and manifest_matches
        and payload.get("destination") == str(destination)
        and isinstance(output, dict)
        and output.get("path") == str(destination)
        and int(output.get("size") or -1) == destination.stat().st_size
        and int(output.get("mtime_ns") or -1) == destination.stat().st_mtime_ns
        and str(output.get("sha256") or "").casefold() == destination_sha256
    ):
        raise RuntimeError(
            f"completed delivery receipt does not own the configured artifact: {receipt}"
        )
    return (
        {destination, receipt},
        {
            str(destination.resolve()).casefold(): destination_sha256,
            str(receipt.resolve()).casefold(): receipt_sha256,
        },
    )


def _prepare_archive_entries(
    candidates: list[Path],
    archive_dir: Path,
    *,
    precomputed_sha256: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, source in enumerate(candidates):
        if not source.is_file():
            continue
        source_digest = hashlib.sha1(
            str(source).encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        suffix = source.suffix if source.suffix else ".bin"
        destination = archive_dir / f"item-{index:03d}-{source_digest}{suffix}"
        stat = source.stat()
        source_key = str(source.resolve()).casefold()
        entries.append(
            {
                "source": str(source),
                "archive": str(destination),
                "sha256": (precomputed_sha256 or {}).get(source_key) or sha256_file(source),
                "size": int(stat.st_size),
                "state": "pending",
            }
        )
    return entries


def _assert_required_archive_entries(
    required: set[Path],
    entries: list[dict[str, Any]],
) -> None:
    prepared = {
        str(Path(str(entry["source"])).resolve()).casefold()
        for entry in entries
    }
    missing = [
        str(path)
        for path in required
        if str(path.resolve()).casefold() not in prepared
    ]
    if missing:
        raise RuntimeError(
            "required completed delivery changed before archive preparation: "
            + ", ".join(sorted(missing, key=str.casefold))
        )


def _assert_completed_delivery_snapshot(
    config: AppConfig | Any,
    video: Path,
    expected: set[Path],
) -> None:
    managed = _completed_delivery_managed_paths(config, video)
    actual = {path for path in managed if path.exists()}
    marker = (
        completed_delivery_marker_path(video, config)
        if completed_delivery_enabled(config)
        else None
    )
    if actual != expected or (marker is not None and marker.exists()):
        raise RuntimeError(
            "completed delivery changed after ownership validation; refusing archive"
        )


def _public_archive_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: entry[key]
        for key in (
            "source",
            "archive",
            "sha256",
            "size",
            "state",
            "archive_sha256",
            "moved_at",
        )
        if key in entry
    }


def _rollback_archive_entries(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for entry in reversed(entries):
        source = Path(str(entry["source"]))
        archive = Path(str(entry["archive"]))
        expected_sha256 = str(entry["sha256"])
        try:
            if source.is_file():
                if sha256_file(source) != expected_sha256:
                    raise RuntimeError(f"rollback target already exists with different content: {source}")
                entry["state"] = "rolled_back"
                continue
            if not archive.is_file() or sha256_file(archive) != expected_sha256:
                raise RuntimeError(f"rollback archive checksum mismatch: {archive}")
            verified_move(archive, source)
            if sha256_file(source) != expected_sha256:
                raise RuntimeError(f"rollback source checksum mismatch: {source}")
            entry["state"] = "rolled_back"
            entry["rolled_back_at"] = time.time()
        except Exception as exc:
            failures.append(
                {
                    "stage": "rollback",
                    "source": str(source),
                    "archive": str(archive),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return failures


def _rollback_restore_entries(
    restored: list[dict[str, Any]],
    backed_up: list[dict[str, Any]],
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for entry in reversed(restored):
        source = Path(str(entry["source"]))
        archive = Path(str(entry["archive"]))
        expected_sha256 = str(entry["sha256"])
        try:
            if archive.is_file():
                if sha256_file(archive) != expected_sha256:
                    raise RuntimeError(f"restore rollback archive checksum mismatch: {archive}")
                if source.is_file() and sha256_file(source) == expected_sha256:
                    continue
                raise RuntimeError(f"restore rollback target conflict: {source}")
            if not source.is_file() or sha256_file(source) != expected_sha256:
                raise RuntimeError(f"restored output changed before rollback: {source}")
            verified_move(source, archive)
            if sha256_file(archive) != expected_sha256:
                raise RuntimeError(f"restore rollback checksum mismatch: {archive}")
        except Exception as exc:
            failures.append(
                {
                    "stage": "rollback_restored_output",
                    "source": str(source),
                    "archive": str(archive),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    for entry in reversed(backed_up):
        source = Path(str(entry["source"]))
        archive = Path(str(entry["archive"]))
        expected_sha256 = str(entry["sha256"])
        try:
            if source.is_file():
                if sha256_file(source) != expected_sha256:
                    raise RuntimeError(f"current output rollback target conflict: {source}")
                continue
            if not archive.is_file() or sha256_file(archive) != expected_sha256:
                raise RuntimeError(f"current output backup checksum mismatch: {archive}")
            verified_move(archive, source)
            if sha256_file(source) != expected_sha256:
                raise RuntimeError(f"current output rollback checksum mismatch: {source}")
        except Exception as exc:
            failures.append(
                {
                    "stage": "rollback_current_output",
                    "source": str(source),
                    "archive": str(archive),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return failures


def _read_reprocess_manifest(
    config: AppConfig | Any,
    manifest_path: Path,
) -> dict[str, Any]:
    allowed_root = (Path(config.work_path) / "manual_ai_reprocess").resolve()
    if manifest_path.name != MANIFEST_NAME or not manifest_path.is_relative_to(allowed_root):
        raise RuntimeError(f"refused reprocess manifest outside managed archive root: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid reprocess archive manifest: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid reprocess archive manifest: {manifest_path}")
    if int(payload.get("schema_version") or 0) != MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(f"unsupported reprocess archive manifest schema: {manifest_path}")
    if payload.get("operation") != "reprocess_archive" or payload.get("status") != "complete":
        raise RuntimeError(f"reprocess archive is not complete and cannot be restored: {manifest_path}")
    if bool(payload.get("selective")):
        raise RuntimeError(f"refused archive with ambiguous selective strategy: {manifest_path}")
    return payload


def _validated_restore_entries(
    config: AppConfig | Any,
    video: Path,
    *,
    mode: str,
    manifest_path: Path,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise RuntimeError(f"invalid reprocess archive entries: {manifest_path}")
    allowed = {
        str(path.resolve()).casefold(): path
        for path in _reprocess_candidates(
            config,
            video,
            mode=mode,
            completed_paths=_completed_delivery_managed_paths(config, video),
        )
    }
    archive_root = manifest_path.parent.resolve()
    seen_sources: set[str] = set()
    seen_archives: set[str] = set()
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict) or raw.get("state") != "moved":
            raise RuntimeError(f"incomplete reprocess archive entry: {manifest_path}")
        source = Path(str(raw.get("source") or "")).resolve()
        source_key = str(source).casefold()
        archive = Path(str(raw.get("archive") or "")).resolve()
        archive_key = str(archive).casefold()
        expected_sha256 = str(raw.get("sha256") or "").strip().casefold()
        if source_key not in allowed:
            raise RuntimeError(f"refused unexpected reprocess restore target: {source}")
        if not archive.is_relative_to(archive_root):
            raise RuntimeError(f"refused archive path outside restore directory: {archive}")
        if source_key in seen_sources or archive_key in seen_archives:
            raise RuntimeError(f"duplicate reprocess archive entry: {manifest_path}")
        if len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            raise RuntimeError(f"invalid reprocess archive checksum: {archive}")
        seen_sources.add(source_key)
        seen_archives.add(archive_key)
        entries.append(
            {
                "source": str(allowed[source_key]),
                "archive": str(archive),
                "sha256": expected_sha256,
                "size": int(raw.get("size") or 0),
                "state": "moved",
            }
        )
    return entries


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive AI subtitle outputs for a full retry, or restore a verified archive."
    )
    parser.add_argument("--config", default="/app/config.yaml")
    parser.add_argument("--video-path")
    parser.add_argument("--mode", choices=sorted(VALID_MODES))
    parser.add_argument("--queue-mode", choices=sorted(VALID_QUEUE_MODES), default="manual_force")
    parser.add_argument("--expected-failure-revision", default="")
    parser.add_argument("--policy-revision", default="")
    parser.add_argument("--restore-manifest")
    parser.add_argument("--expected-manifest-sha256")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.restore_manifest:
        if (
            args.video_path
            or args.mode
            or args.queue_mode != "manual_force"
            or args.expected_failure_revision
            or args.policy_revision
        ):
            parser.error(
                "--restore-manifest cannot be combined with reprocess queue options"
            )
        result = restore_reprocess_manifest(
            config,
            args.restore_manifest,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
    else:
        if not args.video_path or not args.mode:
            parser.error("--video-path and --mode are required unless --restore-manifest is used")
        if args.expected_manifest_sha256:
            parser.error("--expected-manifest-sha256 requires --restore-manifest")
        result = reprocess_video(
            config,
            args.video_path,
            mode=args.mode,
            queue_mode=args.queue_mode,
            expected_failure_revision=args.expected_failure_revision,
            policy_revision=args.policy_revision,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
