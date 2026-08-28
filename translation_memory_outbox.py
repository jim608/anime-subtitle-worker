from __future__ import annotations

"""Crash-safe outbox for post-publication translation-memory learning.

The outbox closes the publication/SQLite-learning crash window without making
subtitle publication depend on a successful translation-memory write.  An
intent is useful only when all three immutable publication artifacts still
match the hashes recorded by the caller.  Replayed evidence must exclude every
``tm_origin_index`` so a reused translation cannot vote for itself as evidence
from a new episode.

Corrupt envelope data is quarantined.  Missing or changed referenced artifacts
are *not* quarantined or deleted: the intent remains pending and replay fails
closed so a transient mount problem cannot discard durable work.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping, Sequence
import uuid

from safe_files import (
    atomic_write_text,
    fsync_directory,
    sha256_file,
    verified_copy_replace,
    verified_move,
)
from scan_state import ai_delivery_identity
from srt_utils import SrtFormatError, read_srt, validate_translation
from translation_memory import (
    MAX_BLOCKS_PER_EPISODE,
    MAX_IDENTITY_CHARS,
    MemoryScope,
    SUPPORTED_SOURCE_LANGUAGE,
    SUPPORTED_TARGET_LANGUAGE,
)


TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION = 1
MAX_OUTBOX_BYTES = 512 * 1024
MAX_PATH_CHARS = 8192
LEARN_CONFIRMATION_STATUSES = frozenset({"learned", "idempotent", "no_new_blocks"})
STRICT_MANIFEST_SCHEMA_VERSION = 2
STRICT_DELIVERY_CONTRACT = "ai-delivery-v1"
STRICT_PUBLICATION_CONTRACT = "ai-publication-semantics-v2"
STRICT_PUBLICATION_KIND = "translated_trilingual"
STRICT_OUTPUT_LANGUAGES = ("ja", "zh-CN", "zh-TW")
STRICT_LINEAGE_CONTRACT = "translation-memory-lineage-v2"
TRANSLATION_LINEAGE_MODES = frozenset(
    {"tm_split", "no_hits", "lookup_fallback", "tm_disabled"}
)


class TranslationMemoryOutboxError(RuntimeError):
    """Base error for an outbox operation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


class TranslationMemoryOutboxCorrupt(TranslationMemoryOutboxError):
    """An envelope was invalid and was quarantined when possible."""

    def __init__(
        self,
        detail: str,
        *,
        path: str | Path,
        quarantine_path: str | Path | None,
    ) -> None:
        self.path = Path(path)
        self.quarantine_path = Path(quarantine_path) if quarantine_path is not None else None
        suffix = (
            f"; quarantined={self.quarantine_path}"
            if self.quarantine_path is not None
            else "; quarantine_failed_or_unavailable"
        )
        super().__init__("corrupt_outbox_intent", f"{detail}{suffix}")


class TranslationMemoryOutboxNotReplayReady(TranslationMemoryOutboxError):
    """A valid intent cannot be replayed against the current artifacts."""


class TranslationMemoryOutboxCollision(TranslationMemoryOutboxError):
    """The deterministic intent key already refers to different evidence."""


class TranslationMemoryOutboxAcknowledgementError(TranslationMemoryOutboxError):
    """Deletion was requested without a matching durable learn confirmation."""


@dataclass(frozen=True)
class TranslationMemoryOutboxIntent:
    """Hash-bound learning intent persisted in one JSON envelope."""

    intent_id: str
    intent_sha256: str
    manifest_path: str
    manifest_sha256: str
    video_identity: str
    scope: MemoryScope
    episode_id: str
    source_srt_path: str
    source_srt_sha256: str
    target_srt_path: str
    target_srt_sha256: str
    source_snapshot_path: str
    target_snapshot_path: str
    source_block_indexes: tuple[int, ...]
    tm_origin_indexes: tuple[int, ...]
    translation_lineage_mode: str
    split_decision_digest: str
    created_at: str

    @property
    def learnable_indexes(self) -> tuple[int, ...]:
        origins = frozenset(self.tm_origin_indexes)
        return tuple(index for index in self.source_block_indexes if index not in origins)

    @property
    def is_auditable_noop(self) -> bool:
        return not self.learnable_indexes


@dataclass(frozen=True)
class RecordedTranslationMemoryOutboxIntent:
    """Result of an atomic record attempt."""

    path: Path
    intent: TranslationMemoryOutboxIntent
    created: bool


@dataclass(frozen=True)
class ReplayReadyTranslationMemoryOutboxIntent:
    """A valid intent whose referenced files and SRT alignment were rechecked."""

    path: Path
    intent: TranslationMemoryOutboxIntent

    @property
    def learnable_indexes(self) -> tuple[int, ...]:
        return self.intent.learnable_indexes

    @property
    def is_auditable_noop(self) -> bool:
        return self.intent.is_auditable_noop


@dataclass(frozen=True)
class TranslationMemoryLearnConfirmation:
    """Caller proof required before an outbox entry may be deleted.

    ``idempotent_learn_confirmed`` means the caller has received a durable,
    retry-safe result from the learner.  ``no_new_blocks`` is permitted only
    when every source block was already TM-origin and therefore deliberately
    not submitted to the learner.
    """

    intent_sha256: str
    manifest_sha256: str
    status: str
    idempotent_learn_confirmed: bool


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ENVELOPE_KEYS = frozenset({"schema_version", "intent", "intent_sha256"})
_INTENT_KEYS = frozenset(
    {
        "intent_id",
        "manifest_path",
        "manifest_sha256",
        "video_identity",
        "scope",
        "episode_id",
        "source_srt_path",
        "source_srt_sha256",
        "target_srt_path",
        "target_srt_sha256",
        "source_snapshot_path",
        "target_snapshot_path",
        "source_block_indexes",
        "tm_origin_indexes",
        "translation_lineage_mode",
        "split_decision_digest",
        "created_at",
    }
)
_SCOPE_KEYS = frozenset(
    {"series_key", "policy_version", "source_language", "target_language"}
)


def record_translation_memory_outbox_intent(
    outbox_root: str | Path,
    *,
    manifest_path: str | Path,
    manifest_sha256: str,
    video_identity: str,
    scope: MemoryScope,
    episode_id: str,
    source_srt_path: str | Path,
    source_srt_sha256: str,
    target_srt_path: str | Path,
    target_srt_sha256: str,
    tm_origin_indexes: Sequence[int] = (),
    translation_lineage_mode: str,
    split_decision_digest: str,
    created_at: str | None = None,
    allow_publication_in_progress: bool = False,
) -> RecordedTranslationMemoryOutboxIntent:
    """Validate immutable artifacts and atomically record one learning intent.

    The filename is deterministic for a publication identity.  Retrying the
    exact same intent is idempotent and preserves its first ``created_at``.
    Different evidence at the same deterministic key is rejected, never
    overwritten.  The Worker publication transaction may explicitly set
    ``allow_publication_in_progress=True`` while its marker still exists; the
    durable intent is then written before the marker is removed.  Replay never
    accepts that marker.
    """

    root = _canonical_root(outbox_root)
    intent = _build_intent(
        outbox_root=root,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        video_identity=video_identity,
        scope=scope,
        episode_id=episode_id,
        source_srt_path=source_srt_path,
        source_srt_sha256=source_srt_sha256,
        target_srt_path=target_srt_path,
        target_srt_sha256=target_srt_sha256,
        tm_origin_indexes=tm_origin_indexes,
        translation_lineage_mode=translation_lineage_mode,
        split_decision_digest=split_decision_digest,
        created_at=created_at or _utc_now(),
    )
    path = root / f"{intent.intent_id}.json"
    if path.exists():
        existing = load_translation_memory_outbox_intent(path)
        if _semantic_payload(existing) != _semantic_payload(intent):
            raise TranslationMemoryOutboxCollision(
                "outbox_intent_collision",
                f"{path} already contains different evidence for intent {intent.intent_id}",
            )
        return RecordedTranslationMemoryOutboxIntent(path, existing, False)

    root.mkdir(parents=True, exist_ok=True)
    _materialize_snapshots(intent)
    # Creation is itself a replay-readiness gate.  Publication callers must not
    # persist an intent around stale hashes or invalid source/target alignment.
    _validate_referenced_artifacts(
        intent,
        allow_publication_in_progress=allow_publication_in_progress,
    )
    try:
        atomic_write_text(path, _render_envelope(intent), encoding="utf-8")
    except Exception as exc:
        raise TranslationMemoryOutboxError(
            "outbox_write_failed",
            f"could not atomically write {path}: {exc}",
        ) from exc

    persisted = load_translation_memory_outbox_intent(path)
    if persisted != intent:
        # This should be unreachable with atomic_write_text, but never return a
        # record that differs from the exact evidence the caller supplied.
        raise TranslationMemoryOutboxError(
            "outbox_postwrite_mismatch",
            f"persisted outbox intent differs from the requested intent: {path}",
        )
    return RecordedTranslationMemoryOutboxIntent(path, persisted, True)


def load_translation_memory_outbox_intent(
    path: str | Path,
    *,
    quarantine_corrupt: bool = True,
) -> TranslationMemoryOutboxIntent:
    """Load and strictly validate a hash-bound JSON envelope.

    JSON syntax, duplicate keys, unknown/missing fields, noncanonical values,
    and an envelope-hash mismatch are corruption.  They are moved to a sibling
    ``quarantine`` directory by default before this function raises.
    """

    candidate = Path(path)
    try:
        stat = candidate.stat()
        if not candidate.is_file():
            raise TranslationMemoryOutboxError(
                "outbox_not_file",
                f"outbox path is not a regular file: {candidate}",
            )
        if stat.st_size <= 0 or stat.st_size > MAX_OUTBOX_BYTES:
            raise _CorruptPayload(
                f"outbox size must be between 1 and {MAX_OUTBOX_BYTES} bytes"
            )
        raw = candidate.read_bytes()
    except _CorruptPayload as exc:
        return _raise_corrupt(candidate, str(exc), quarantine_corrupt)
    except FileNotFoundError as exc:
        raise TranslationMemoryOutboxError(
            "outbox_missing",
            f"outbox intent does not exist: {candidate}",
        ) from exc
    except TranslationMemoryOutboxError:
        raise
    except OSError as exc:
        raise TranslationMemoryOutboxError(
            "outbox_read_failed",
            f"could not read outbox intent {candidate}: {exc}",
        ) from exc

    try:
        text = raw.decode("utf-8")
        envelope = json.loads(text, object_pairs_hook=_unique_json_object)
        intent = _intent_from_envelope(envelope)
        _validate_snapshot_locations(intent, candidate.parent)
    except (UnicodeError, json.JSONDecodeError, _CorruptPayload, TypeError, ValueError) as exc:
        return _raise_corrupt(candidate, str(exc), quarantine_corrupt)
    return intent


def load_replay_ready_translation_memory_outbox_intent(
    path: str | Path,
    *,
    quarantine_corrupt: bool = True,
) -> ReplayReadyTranslationMemoryOutboxIntent:
    """Load an intent and prove its current artifacts are safe to replay."""

    candidate = Path(path)
    intent = load_translation_memory_outbox_intent(
        candidate,
        quarantine_corrupt=quarantine_corrupt,
    )
    _validate_referenced_artifacts(intent)
    return ReplayReadyTranslationMemoryOutboxIntent(candidate, intent)


def acknowledge_translation_memory_outbox_intent(
    path: str | Path,
    confirmation: TranslationMemoryLearnConfirmation,
) -> bool:
    """Durably delete an intent only after a matching idempotent confirmation.

    Returns ``False`` when a previous acknowledgement already removed the
    entry.  No artifact revalidation is required here: the learn operation may
    already have succeeded before files were archived or moved.
    """

    candidate = Path(path)
    if not candidate.exists():
        return False
    if not isinstance(confirmation, TranslationMemoryLearnConfirmation):
        raise TranslationMemoryOutboxAcknowledgementError(
            "invalid_learn_confirmation",
            "confirmation must be TranslationMemoryLearnConfirmation",
        )
    intent = load_translation_memory_outbox_intent(candidate)
    confirmation_hash = _validated_hash(
        confirmation.intent_sha256,
        "confirmation.intent_sha256",
    )
    manifest_hash = _validated_hash(
        confirmation.manifest_sha256,
        "confirmation.manifest_sha256",
    )
    if confirmation.idempotent_learn_confirmed is not True:
        raise TranslationMemoryOutboxAcknowledgementError(
            "learn_not_confirmed",
            "outbox deletion requires idempotent_learn_confirmed=True",
        )
    status = str(confirmation.status)
    if status not in LEARN_CONFIRMATION_STATUSES:
        raise TranslationMemoryOutboxAcknowledgementError(
            "invalid_learn_status",
            f"learn status must be one of {sorted(LEARN_CONFIRMATION_STATUSES)}, got {status!r}",
        )
    if confirmation_hash != intent.intent_sha256:
        raise TranslationMemoryOutboxAcknowledgementError(
            "confirmation_intent_mismatch",
            "confirmation does not identify the current outbox envelope",
        )
    if manifest_hash != intent.manifest_sha256:
        raise TranslationMemoryOutboxAcknowledgementError(
            "confirmation_manifest_mismatch",
            "confirmation does not identify the intent publication manifest",
        )
    if status == "no_new_blocks" and not intent.is_auditable_noop:
        raise TranslationMemoryOutboxAcknowledgementError(
            "false_noop_confirmation",
            "no_new_blocks is valid only when every source block is TM-origin",
        )
    if status in {"learned", "idempotent"} and intent.is_auditable_noop:
        raise TranslationMemoryOutboxAcknowledgementError(
            "tm_origin_relearn_rejected",
            "an all-TM-origin intent must be acknowledged as no_new_blocks, not learned",
        )

    try:
        # Re-read immediately before deletion to reduce the replacement race and
        # make sure a different valid envelope cannot be removed by a stale ack.
        current = load_translation_memory_outbox_intent(candidate)
        if current.intent_sha256 != confirmation_hash:
            raise TranslationMemoryOutboxAcknowledgementError(
                "confirmation_intent_changed",
                "outbox intent changed before acknowledgement",
            )
        candidate.unlink()
        fsync_directory(candidate.parent)
    except TranslationMemoryOutboxError:
        raise
    except OSError as exc:
        raise TranslationMemoryOutboxAcknowledgementError(
            "outbox_delete_failed",
            f"learn was confirmed but outbox deletion failed for {candidate}: {exc}",
        ) from exc
    return True


def quarantine_corrupt_translation_memory_outbox_intent(path: str | Path) -> Path:
    """Move one existing corrupt intent to a unique sibling quarantine path."""

    source = Path(path)
    quarantine_root = source.parent / "quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = quarantine_root / (
        f"{source.name}.corrupt-{timestamp}-{time.time_ns()}-{uuid.uuid4().hex}.json"
    )
    try:
        verified_move(source, destination)
        fsync_directory(quarantine_root)
    except Exception as exc:
        raise TranslationMemoryOutboxError(
            "outbox_quarantine_failed",
            f"could not quarantine {source}: {exc}",
        ) from exc
    return destination


def _build_intent(
    *,
    outbox_root: Path,
    manifest_path: str | Path,
    manifest_sha256: str,
    video_identity: str,
    scope: MemoryScope,
    episode_id: str,
    source_srt_path: str | Path,
    source_srt_sha256: str,
    target_srt_path: str | Path,
    target_srt_sha256: str,
    tm_origin_indexes: Sequence[int],
    translation_lineage_mode: str,
    split_decision_digest: str,
    created_at: str,
) -> TranslationMemoryOutboxIntent:
    validated_scope = _validated_scope(scope)
    canonical_manifest = _canonical_artifact_path(manifest_path, "manifest_path")
    canonical_source = _canonical_artifact_path(source_srt_path, "source_srt_path")
    canonical_target = _canonical_artifact_path(target_srt_path, "target_srt_path")
    if len({canonical_manifest, canonical_source, canonical_target}) != 3:
        raise TranslationMemoryOutboxError(
            "artifact_path_collision",
            "manifest, source SRT, and target SRT paths must be distinct",
        )
    manifest_hash = _validated_hash(manifest_sha256, "manifest_sha256")
    source_hash = _validated_hash(source_srt_sha256, "source_srt_sha256")
    target_hash = _validated_hash(target_srt_sha256, "target_srt_sha256")
    split_digest = _validated_hash(split_decision_digest, "split_decision_digest")
    lineage_mode = _validated_lineage_mode(translation_lineage_mode)
    normalized_video = _identity(video_identity, "video_identity")
    normalized_episode = _identity(episode_id, "episode_id")
    normalized_origins = _normalized_indexes(
        tm_origin_indexes,
        "tm_origin_indexes",
        canonical_required=False,
    )
    normalized_created = _normalized_timestamp(created_at)
    originals = (
        (canonical_manifest, manifest_hash, "manifest"),
        (canonical_source, source_hash, "source_srt"),
        (canonical_target, target_hash, "target_srt"),
    )
    for raw_path, expected, label in originals:
        actual = _stable_file_hash(Path(raw_path), label)
        if actual != expected:
            raise TranslationMemoryOutboxNotReplayReady(
                "artifact_hash_mismatch",
                f"{label} hash changed: expected {expected}, got {actual}; path={raw_path}",
            )
    source_indexes = _read_aligned_indexes(Path(canonical_source), Path(canonical_target))
    provisional = TranslationMemoryOutboxIntent(
        intent_id="0" * 64,
        intent_sha256="0" * 64,
        manifest_path=canonical_manifest,
        manifest_sha256=manifest_hash,
        video_identity=normalized_video,
        scope=validated_scope,
        episode_id=normalized_episode,
        source_srt_path=canonical_source,
        source_srt_sha256=source_hash,
        target_srt_path=canonical_target,
        target_srt_sha256=target_hash,
        source_snapshot_path=str(_snapshot_path(outbox_root, "source", source_hash)),
        target_snapshot_path=str(_snapshot_path(outbox_root, "target", target_hash)),
        source_block_indexes=source_indexes,
        tm_origin_indexes=normalized_origins,
        translation_lineage_mode=lineage_mode,
        split_decision_digest=split_digest,
        created_at=normalized_created,
    )
    missing_origins = sorted(set(normalized_origins) - set(source_indexes))
    if missing_origins:
        raise TranslationMemoryOutboxError(
            "tm_origin_index_missing",
            f"TM-origin indexes are absent from source SRT: {missing_origins}",
        )
    _validate_lineage_consistency(lineage_mode, normalized_origins)
    intent_id = _intent_identity_hash(provisional)
    with_identity = replace(provisional, intent_id=intent_id)
    return replace(with_identity, intent_sha256=_intent_payload_hash(with_identity))


def _validate_referenced_artifacts(
    intent: TranslationMemoryOutboxIntent,
    *,
    allow_publication_in_progress: bool = False,
) -> None:
    checks = (
        (intent.manifest_path, intent.manifest_sha256, "manifest"),
        (intent.source_snapshot_path, intent.source_srt_sha256, "source_snapshot"),
        (intent.target_snapshot_path, intent.target_srt_sha256, "target_snapshot"),
    )
    for raw_path, expected, label in checks:
        actual = _stable_file_hash(Path(raw_path), label)
        if actual != expected:
            raise TranslationMemoryOutboxNotReplayReady(
                "artifact_hash_mismatch",
                f"{label} hash changed: expected {expected}, got {actual}; path={raw_path}",
            )

    marker = Path(intent.manifest_path).with_suffix(".publishing")
    if marker.exists() and allow_publication_in_progress is not True:
        raise TranslationMemoryOutboxNotReplayReady(
            "publication_in_progress",
            f"strict publication marker still exists: {marker}",
        )
    _validate_strict_publication_manifest(Path(intent.manifest_path), intent)

    source_indexes = _read_aligned_source_indexes(intent)
    if source_indexes != intent.source_block_indexes:
        raise TranslationMemoryOutboxNotReplayReady(
            "source_index_drift",
            f"source indexes changed from {intent.source_block_indexes} to {source_indexes}",
        )
    missing_origins = sorted(set(intent.tm_origin_indexes) - set(source_indexes))
    if missing_origins:
        raise TranslationMemoryOutboxNotReplayReady(
            "tm_origin_index_drift",
            f"TM-origin indexes are no longer in the source SRT: {missing_origins}",
        )


def _materialize_snapshots(intent: TranslationMemoryOutboxIntent) -> None:
    """Publish immutable content-addressed SRT copies before the JSON intent.

    A crash between the two copies can leave an unreferenced blob, but can
    never expose a replayable JSON intent with a missing snapshot.  A later
    retry safely reuses or replaces the content-addressed blob after hashing.
    """

    copies = (
        (
            Path(intent.source_srt_path),
            Path(intent.source_snapshot_path),
            intent.source_srt_sha256,
            "source_snapshot",
        ),
        (
            Path(intent.target_srt_path),
            Path(intent.target_snapshot_path),
            intent.target_srt_sha256,
            "target_snapshot",
        ),
    )
    for source, destination, expected, label in copies:
        actual_source = _stable_file_hash(source, f"{label}_source")
        if actual_source != expected:
            raise TranslationMemoryOutboxNotReplayReady(
                "artifact_hash_mismatch",
                f"{label} source hash changed: expected {expected}, got {actual_source}; path={source}",
            )
        if destination.is_file():
            actual_snapshot = _stable_file_hash(destination, label)
            if actual_snapshot == expected:
                continue
        try:
            verified_copy_replace(source, destination)
        except Exception as exc:
            raise TranslationMemoryOutboxError(
                "snapshot_write_failed",
                f"could not persist {label} {destination}: {exc}",
            ) from exc
        actual_snapshot = _stable_file_hash(destination, label)
        if actual_snapshot != expected:
            raise TranslationMemoryOutboxError(
                "snapshot_postwrite_mismatch",
                f"{label} does not match expected SHA-256 after atomic copy",
            )


def _validate_strict_publication_manifest(
    path: Path,
    intent: TranslationMemoryOutboxIntent,
) -> None:
    """Require the minimum immutable strict-publication proof before replay.

    The Worker must still call its full output-manifest verifier before learning;
    this local gate prevents an outbox consumer from accidentally replaying a
    source-language or incomplete publication merely because its JSON hash is
    intact.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError, _CorruptPayload) as exc:
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_not_strict",
            f"publication manifest is unreadable or invalid JSON: {path}: {exc}",
        ) from exc
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int:
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_not_strict",
            "publication manifest must be a schema-versioned object",
        )
    delivery = payload.get("delivery")
    quality_gate = payload.get("quality_gate")
    publication = payload.get("publication")
    outputs = payload.get("outputs")
    provenance = payload.get("provenance")
    lineage = (
        provenance.get("translation_memory")
        if isinstance(provenance, dict)
        else None
    )
    valid = (
        payload.get("schema_version") == STRICT_MANIFEST_SCHEMA_VERSION
        and isinstance(delivery, dict)
        and delivery.get("contract") == STRICT_DELIVERY_CONTRACT
        and delivery.get("obligation_id") == intent.video_identity
        and delivery.get("policy_revision") == intent.scope.policy_version
        and isinstance(quality_gate, dict)
        and quality_gate.get("passed") is True
        and payload.get("publication_kind") == STRICT_PUBLICATION_KIND
        and isinstance(publication, dict)
        and publication.get("contract") == STRICT_PUBLICATION_CONTRACT
        and publication.get("kind") == STRICT_PUBLICATION_KIND
        and tuple(publication.get("output_languages") or ()) == STRICT_OUTPUT_LANGUAGES
        and isinstance(outputs, list)
        and len(outputs) == len(STRICT_OUTPUT_LANGUAGES)
        and tuple(
            item.get("language") if isinstance(item, dict) else None
            for item in outputs
        )
        == STRICT_OUTPUT_LANGUAGES
        and all(
            isinstance(item, dict) and _HASH_RE.fullmatch(str(item.get("sha256") or ""))
            for item in outputs
        )
        and isinstance(lineage, dict)
        and lineage.get("contract") == STRICT_LINEAGE_CONTRACT
        and lineage.get("mode") == intent.translation_lineage_mode
        and lineage.get("split_decision_digest") == intent.split_decision_digest
        and type(lineage.get("tm_origin_indexes")) is list
        and all(type(index) is int and index > 0 for index in lineage["tm_origin_indexes"])
        and tuple(lineage["tm_origin_indexes"]) == intent.tm_origin_indexes
        and lineage.get("source_srt_sha256") == intent.source_srt_sha256
        and lineage.get("target_srt_sha256") == intent.target_srt_sha256
    )
    if not valid:
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_not_strict",
            "manifest is not strict translated_trilingual evidence with matching TM lineage",
        )
    _validate_manifest_media_identity(payload, intent)
    _validate_manifest_output_artifacts(outputs)


def _validate_manifest_media_identity(
    payload: Mapping[str, Any],
    intent: TranslationMemoryOutboxIntent,
) -> None:
    """Bind replay to the current media revision behind the delivery identity."""

    media = payload.get("media")
    delivery = payload.get("delivery")
    if not isinstance(media, dict) or not isinstance(delivery, dict):
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_media_invalid",
            "strict manifest requires media and delivery objects",
        )
    required_media_keys = {
        "canonical_path",
        "media_fingerprint",
        "media_size",
        "media_mtime_ns",
    }
    if set(media) != required_media_keys:
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_media_invalid",
            "strict manifest media identity has missing or unknown fields",
        )
    canonical_path = media.get("canonical_path")
    if not isinstance(canonical_path, str) or not canonical_path.strip():
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_media_invalid",
            "media canonical_path is required",
        )
    video = Path(canonical_path)
    try:
        if str(video.resolve()) != canonical_path:
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_media_invalid",
                "media canonical_path is not canonical",
            )
        if str(Path(str(payload.get("video") or "")).resolve()) != canonical_path:
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_video_mismatch",
                "manifest video path does not match media canonical_path",
            )
        stat = video.stat()
        current = ai_delivery_identity(
            video,
            media_size=int(stat.st_size),
            media_mtime_ns=int(stat.st_mtime_ns),
            policy_revision=str(delivery.get("policy_revision") or ""),
        )
    except TranslationMemoryOutboxError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_media_unavailable",
            f"current media identity cannot be verified: {video}: {exc}",
        ) from exc
    expected_media = {
        "canonical_path": current["canonical_path"],
        "media_fingerprint": current["media_fingerprint"],
        "media_size": current["media_size"],
        "media_mtime_ns": current["media_mtime_ns"],
    }
    if media != expected_media or current["obligation_id"] != intent.video_identity:
        raise TranslationMemoryOutboxNotReplayReady(
            "manifest_media_identity_mismatch",
            "current media revision no longer matches the strict delivery identity",
        )


def _validate_manifest_output_artifacts(outputs: Sequence[Any]) -> None:
    """Re-prove every published ASS artifact instead of trusting JSON claims.

    An outbox can survive a crash after its manifest was written but before the
    Worker's post-publication verification completed.  Learning must therefore
    independently require the manifested files, sizes, mtimes, and hashes to be
    present at replay time.
    """

    seen_paths: set[str] = set()
    for index, raw in enumerate(outputs):
        if not isinstance(raw, dict):  # Kept explicit for callers of this helper.
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_output_invalid",
                f"manifest output {index} is not an object",
            )
        raw_path = raw.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_output_invalid",
                f"manifest output {index} has no path",
            )
        output = Path(raw_path)
        canonical = os.path.normcase(os.path.abspath(str(output)))
        if canonical in seen_paths:
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_output_duplicate",
                f"manifest repeats output path {output}",
            )
        seen_paths.add(canonical)
        try:
            stat = output.stat()
            if not output.is_file() or stat.st_size <= 0:
                raise TranslationMemoryOutboxNotReplayReady(
                    "manifest_output_missing",
                    f"published output is missing or empty: {output}",
                )
            if type(raw.get("size")) is not int or int(raw["size"]) != int(stat.st_size):
                raise TranslationMemoryOutboxNotReplayReady(
                    "manifest_output_size_mismatch",
                    f"published output size changed: {output}",
                )
            if type(raw.get("mtime_ns")) is not int or int(raw["mtime_ns"]) != int(stat.st_mtime_ns):
                raise TranslationMemoryOutboxNotReplayReady(
                    "manifest_output_mtime_mismatch",
                    f"published output mtime changed: {output}",
                )
            expected_hash = _validated_hash(raw.get("sha256"), "output.sha256")
            actual_hash = _stable_file_hash(output, f"published_output_{index}")
        except TranslationMemoryOutboxError:
            raise
        except FileNotFoundError as exc:
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_output_missing",
                f"published output is missing: {output}",
            ) from exc
        except OSError as exc:
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_output_unavailable",
                f"could not validate published output {output}: {exc}",
            ) from exc
        if actual_hash != expected_hash:
            raise TranslationMemoryOutboxNotReplayReady(
                "manifest_output_hash_mismatch",
                f"published output hash changed: {output}",
            )


def _read_aligned_source_indexes(intent: TranslationMemoryOutboxIntent) -> tuple[int, ...]:
    return _read_aligned_indexes(
        Path(intent.source_snapshot_path),
        Path(intent.target_snapshot_path),
    )


def _read_aligned_indexes(source_path: Path, target_path: Path) -> tuple[int, ...]:
    try:
        source = read_srt(source_path)
        target = read_srt(target_path)
        validate_translation(source, target)
    except (OSError, UnicodeError, SrtFormatError) as exc:
        raise TranslationMemoryOutboxNotReplayReady(
            "srt_not_replay_ready",
            f"source/target SRT is unreadable, invalid, or unaligned: {exc}",
        ) from exc
    indexes = tuple(block.index for block in source)
    if any(isinstance(index, bool) or not isinstance(index, int) or index <= 0 for index in indexes):
        raise TranslationMemoryOutboxNotReplayReady(
            "invalid_source_indexes",
            "source SRT indexes must be positive integers",
        )
    if len(indexes) > MAX_BLOCKS_PER_EPISODE:
        raise TranslationMemoryOutboxNotReplayReady(
            "source_too_large",
            f"source SRT exceeds {MAX_BLOCKS_PER_EPISODE} blocks",
        )
    return indexes


def _snapshot_path(root: Path, role: str, digest: str) -> Path:
    if role not in {"source", "target"}:  # pragma: no cover - internal invariant
        raise TranslationMemoryOutboxError("invalid_snapshot_role", f"unsupported role {role!r}")
    return root / "blobs" / digest[:2] / f"{digest}.{role}.srt"


def _validate_snapshot_locations(intent: TranslationMemoryOutboxIntent, root: Path) -> None:
    canonical_root = _canonical_root(root)
    expected_source = str(_snapshot_path(canonical_root, "source", intent.source_srt_sha256))
    expected_target = str(_snapshot_path(canonical_root, "target", intent.target_srt_sha256))
    if intent.source_snapshot_path != expected_source:
        raise _CorruptPayload(
            f"source_snapshot_path must be the content-addressed outbox path {expected_source}"
        )
    if intent.target_snapshot_path != expected_target:
        raise _CorruptPayload(
            f"target_snapshot_path must be the content-addressed outbox path {expected_target}"
        )


def _stable_file_hash(path: Path, label: str) -> str:
    try:
        before = path.stat()
        if not path.is_file() or before.st_size <= 0:
            raise TranslationMemoryOutboxNotReplayReady(
                "artifact_missing_or_empty",
                f"{label} must be a nonempty regular file: {path}",
            )
        digest = sha256_file(path)
        after = path.stat()
    except TranslationMemoryOutboxError:
        raise
    except (OSError, PermissionError) as exc:
        raise TranslationMemoryOutboxNotReplayReady(
            "artifact_unreadable",
            f"could not read {label} {path}: {exc}",
        ) from exc
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise TranslationMemoryOutboxNotReplayReady(
            "artifact_changed_during_hash",
            f"{label} changed while being hashed: {path}",
        )
    return digest


def _intent_from_envelope(raw: Any) -> TranslationMemoryOutboxIntent:
    envelope = _exact_mapping(raw, _ENVELOPE_KEYS, "envelope")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION:
        raise _CorruptPayload(
            f"unsupported schema_version {envelope['schema_version']!r}"
        )
    supplied_hash = _canonical_hash(envelope["intent_sha256"], "intent_sha256")
    payload = _exact_mapping(envelope["intent"], _INTENT_KEYS, "intent")
    scope_payload = _exact_mapping(payload["scope"], _SCOPE_KEYS, "intent.scope")
    try:
        scope = _validated_scope(
            MemoryScope(
                series_key=_canonical_identity(scope_payload["series_key"], "scope.series_key"),
                policy_version=_canonical_identity(
                    scope_payload["policy_version"],
                    "scope.policy_version",
                ),
                source_language=_canonical_identity(
                    scope_payload["source_language"],
                    "scope.source_language",
                ),
                target_language=_canonical_identity(
                    scope_payload["target_language"],
                    "scope.target_language",
                ),
            )
        )
    except TranslationMemoryOutboxError as exc:
        raise _CorruptPayload(str(exc)) from exc
    intent = TranslationMemoryOutboxIntent(
        intent_id=_canonical_hash(payload["intent_id"], "intent.intent_id"),
        intent_sha256=supplied_hash,
        manifest_path=_canonical_loaded_path(payload["manifest_path"], "intent.manifest_path"),
        manifest_sha256=_canonical_hash(
            payload["manifest_sha256"],
            "intent.manifest_sha256",
        ),
        video_identity=_canonical_identity(
            payload["video_identity"],
            "intent.video_identity",
        ),
        scope=scope,
        episode_id=_canonical_identity(payload["episode_id"], "intent.episode_id"),
        source_srt_path=_canonical_loaded_path(
            payload["source_srt_path"],
            "intent.source_srt_path",
        ),
        source_srt_sha256=_canonical_hash(
            payload["source_srt_sha256"],
            "intent.source_srt_sha256",
        ),
        target_srt_path=_canonical_loaded_path(
            payload["target_srt_path"],
            "intent.target_srt_path",
        ),
        target_srt_sha256=_canonical_hash(
            payload["target_srt_sha256"],
            "intent.target_srt_sha256",
        ),
        source_snapshot_path=_canonical_loaded_path(
            payload["source_snapshot_path"],
            "intent.source_snapshot_path",
        ),
        target_snapshot_path=_canonical_loaded_path(
            payload["target_snapshot_path"],
            "intent.target_snapshot_path",
        ),
        source_block_indexes=_ordered_source_indexes(
            payload["source_block_indexes"],
            "intent.source_block_indexes",
        ),
        tm_origin_indexes=_normalized_indexes(
            payload["tm_origin_indexes"],
            "intent.tm_origin_indexes",
            canonical_required=True,
        ),
        translation_lineage_mode=_canonical_lineage_mode(
            payload["translation_lineage_mode"]
        ),
        split_decision_digest=_canonical_hash(
            payload["split_decision_digest"],
            "intent.split_decision_digest",
        ),
        created_at=_canonical_timestamp(payload["created_at"]),
    )
    if len(
        {
            intent.manifest_path,
            intent.source_srt_path,
            intent.target_srt_path,
            intent.source_snapshot_path,
            intent.target_snapshot_path,
        }
    ) != 5:
        raise _CorruptPayload("manifest, original SRT, and snapshot paths must be distinct")
    if not intent.source_block_indexes:
        raise _CorruptPayload("source_block_indexes cannot be empty")
    missing_origins = sorted(set(intent.tm_origin_indexes) - set(intent.source_block_indexes))
    if missing_origins:
        raise _CorruptPayload(
            f"TM-origin indexes are absent from source_block_indexes: {missing_origins}"
        )
    try:
        _validate_lineage_consistency(
            intent.translation_lineage_mode,
            intent.tm_origin_indexes,
        )
    except TranslationMemoryOutboxError as exc:
        raise _CorruptPayload(str(exc)) from exc
    expected_id = _intent_identity_hash(intent)
    if intent.intent_id != expected_id:
        raise _CorruptPayload(
            f"intent_id mismatch: expected {expected_id}, got {intent.intent_id}"
        )
    expected_hash = _intent_payload_hash(intent)
    if supplied_hash != expected_hash:
        raise _CorruptPayload(
            f"intent_sha256 mismatch: expected {expected_hash}, got {supplied_hash}"
        )
    return intent


def _render_envelope(intent: TranslationMemoryOutboxIntent) -> str:
    envelope = {
        "schema_version": TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION,
        "intent": _intent_payload(intent),
        "intent_sha256": intent.intent_sha256,
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _intent_payload(intent: TranslationMemoryOutboxIntent) -> dict[str, Any]:
    return {
        "intent_id": intent.intent_id,
        "manifest_path": intent.manifest_path,
        "manifest_sha256": intent.manifest_sha256,
        "video_identity": intent.video_identity,
        "scope": {
            "series_key": intent.scope.series_key,
            "policy_version": intent.scope.policy_version,
            "source_language": intent.scope.source_language,
            "target_language": intent.scope.target_language,
        },
        "episode_id": intent.episode_id,
        "source_srt_path": intent.source_srt_path,
        "source_srt_sha256": intent.source_srt_sha256,
        "target_srt_path": intent.target_srt_path,
        "target_srt_sha256": intent.target_srt_sha256,
        "source_snapshot_path": intent.source_snapshot_path,
        "target_snapshot_path": intent.target_snapshot_path,
        "source_block_indexes": list(intent.source_block_indexes),
        "tm_origin_indexes": list(intent.tm_origin_indexes),
        "translation_lineage_mode": intent.translation_lineage_mode,
        "split_decision_digest": intent.split_decision_digest,
        "created_at": intent.created_at,
    }


def _intent_payload_hash(intent: TranslationMemoryOutboxIntent) -> str:
    binding = {
        "schema_version": TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION,
        "intent": _intent_payload(intent),
    }
    return _sha256_json(binding)


def _intent_identity_hash(intent: TranslationMemoryOutboxIntent) -> str:
    # Stable across retry timestamps while unique to one published video and
    # policy-scoped manifest.  Changed SRT/origin evidence at the same identity
    # is a collision, not an overwrite.
    identity = {
        "schema_version": TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION,
        "manifest_path": intent.manifest_path,
        "video_identity": intent.video_identity,
        "scope": {
            "series_key": intent.scope.series_key,
            "policy_version": intent.scope.policy_version,
            "source_language": intent.scope.source_language,
            "target_language": intent.scope.target_language,
        },
        "episode_id": intent.episode_id,
    }
    return _sha256_json(identity)


def _semantic_payload(intent: TranslationMemoryOutboxIntent) -> dict[str, Any]:
    payload = _intent_payload(intent)
    payload.pop("created_at", None)
    return payload


def _sha256_json(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _validated_scope(scope: MemoryScope) -> MemoryScope:
    if not isinstance(scope, MemoryScope):
        raise TranslationMemoryOutboxError(
            "invalid_scope",
            "scope must be a MemoryScope",
        )
    series = _identity(scope.series_key, "scope.series_key")
    policy = _identity(scope.policy_version, "scope.policy_version")
    if scope.source_language != SUPPORTED_SOURCE_LANGUAGE:
        raise TranslationMemoryOutboxError(
            "unsupported_source_language",
            f"scope source language must be {SUPPORTED_SOURCE_LANGUAGE!r}",
        )
    if scope.target_language != SUPPORTED_TARGET_LANGUAGE:
        raise TranslationMemoryOutboxError(
            "unsupported_target_language",
            f"scope target language must be {SUPPORTED_TARGET_LANGUAGE!r}",
        )
    return MemoryScope(series, policy, scope.source_language, scope.target_language)


def _validated_lineage_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in TRANSLATION_LINEAGE_MODES:
        raise TranslationMemoryOutboxError(
            "invalid_translation_lineage_mode",
            f"translation_lineage_mode must be one of {sorted(TRANSLATION_LINEAGE_MODES)}",
        )
    return value


def _canonical_lineage_mode(value: Any) -> str:
    try:
        return _validated_lineage_mode(value)
    except TranslationMemoryOutboxError as exc:
        raise _CorruptPayload(str(exc)) from exc


def _validate_lineage_consistency(mode: str, indexes: Sequence[int]) -> None:
    if mode == "tm_split" and not indexes:
        raise TranslationMemoryOutboxError(
            "tm_split_without_origins",
            "tm_split lineage requires at least one TM-origin index",
        )
    if mode != "tm_split" and indexes:
        raise TranslationMemoryOutboxError(
            "unexpected_tm_origin_indexes",
            f"{mode} lineage must have an explicit empty TM-origin set",
        )


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TranslationMemoryOutboxError(
            "invalid_identity",
            f"{field} must be a string",
        )
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_IDENTITY_CHARS or _CONTROL_RE.search(normalized):
        raise TranslationMemoryOutboxError(
            "invalid_identity",
            f"{field} must be nonempty, control-free, and at most {MAX_IDENTITY_CHARS} characters",
        )
    return normalized


def _canonical_identity(value: Any, field: str) -> str:
    try:
        normalized = _identity(value, field)
    except TranslationMemoryOutboxError as exc:
        raise _CorruptPayload(str(exc)) from exc
    if value != normalized:
        raise _CorruptPayload(f"{field} is not canonical")
    return normalized


def _validated_hash(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TranslationMemoryOutboxError("invalid_hash", f"{field} must be a SHA-256 string")
    normalized = value.strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise TranslationMemoryOutboxError("invalid_hash", f"{field} must be 64 lowercase hex digits")
    return normalized


def _canonical_hash(value: Any, field: str) -> str:
    try:
        normalized = _validated_hash(value, field)
    except TranslationMemoryOutboxError as exc:
        raise _CorruptPayload(str(exc)) from exc
    if value != normalized:
        raise _CorruptPayload(f"{field} is not canonical lowercase SHA-256")
    return normalized


def _canonical_root(path: str | Path) -> Path:
    root = Path(path)
    if not str(root).strip():
        raise TranslationMemoryOutboxError("invalid_outbox_root", "outbox root cannot be empty")
    return Path(_canonical_artifact_path(root, "outbox_root"))


def _canonical_artifact_path(path: str | Path, field: str) -> str:
    raw = str(path)
    if not raw.strip() or len(raw) > MAX_PATH_CHARS or "\x00" in raw:
        raise TranslationMemoryOutboxError(
            "invalid_path",
            f"{field} must be nonempty, NUL-free, and at most {MAX_PATH_CHARS} characters",
        )
    return os.path.normcase(os.path.abspath(raw))


def _canonical_loaded_path(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _CorruptPayload(f"{field} must be a string")
    try:
        canonical = _canonical_artifact_path(value, field)
    except TranslationMemoryOutboxError as exc:
        raise _CorruptPayload(str(exc)) from exc
    if value != canonical:
        raise _CorruptPayload(f"{field} must be an absolute canonical path")
    return canonical


def _normalized_indexes(
    values: Any,
    field: str,
    *,
    canonical_required: bool,
) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        error = f"{field} must be an integer sequence"
        if canonical_required:
            raise _CorruptPayload(error)
        raise TranslationMemoryOutboxError("invalid_indexes", error)
    try:
        items = tuple(values)
    except TypeError as exc:
        error = f"{field} must be an integer sequence"
        if canonical_required:
            raise _CorruptPayload(error) from exc
        raise TranslationMemoryOutboxError("invalid_indexes", error) from exc
    if len(items) > MAX_BLOCKS_PER_EPISODE:
        error = f"{field} cannot exceed {MAX_BLOCKS_PER_EPISODE} indexes"
        if canonical_required:
            raise _CorruptPayload(error)
        raise TranslationMemoryOutboxError("invalid_indexes", error)
    if any(type(item) is not int or item <= 0 for item in items):
        error = f"{field} must contain only positive integers"
        if canonical_required:
            raise _CorruptPayload(error)
        raise TranslationMemoryOutboxError("invalid_indexes", error)
    normalized = tuple(sorted(set(items)))
    if canonical_required and items != normalized:
        raise _CorruptPayload(f"{field} must be strictly increasing and duplicate-free")
    return normalized


def _ordered_source_indexes(values: Any, field: str) -> tuple[int, ...]:
    """Validate exact SRT order without requiring numerically sorted indexes."""

    if not isinstance(values, list):
        raise _CorruptPayload(f"{field} must be a JSON array")
    items = tuple(values)
    if not items or len(items) > MAX_BLOCKS_PER_EPISODE:
        raise _CorruptPayload(
            f"{field} must contain 1..{MAX_BLOCKS_PER_EPISODE} indexes"
        )
    if any(type(item) is not int or item <= 0 for item in items):
        raise _CorruptPayload(f"{field} must contain only positive integers")
    if len(set(items)) != len(items):
        raise _CorruptPayload(f"{field} must be duplicate-free")
    return items


def _normalized_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise TranslationMemoryOutboxError(
            "invalid_created_at",
            "created_at must be an ISO-8601 string with timezone",
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise TranslationMemoryOutboxError(
            "invalid_created_at",
            "created_at must be an ISO-8601 string with timezone",
        ) from exc
    if parsed.tzinfo is None:
        raise TranslationMemoryOutboxError(
            "invalid_created_at",
            "created_at must include a timezone",
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_timestamp(value: Any) -> str:
    try:
        normalized = _normalized_timestamp(value)
    except TranslationMemoryOutboxError as exc:
        raise _CorruptPayload(str(exc)) from exc
    if value != normalized:
        raise _CorruptPayload("created_at must use canonical UTC microsecond Z form")
    return normalized


def _exact_mapping(value: Any, expected: frozenset[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _CorruptPayload(f"{field} must be an object")
    actual = frozenset(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise _CorruptPayload(
            f"{field} fields mismatch; missing={missing}, unknown={unknown}"
        )
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _CorruptPayload(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _raise_corrupt(
    path: Path,
    detail: str,
    quarantine_corrupt: bool,
) -> TranslationMemoryOutboxIntent:
    quarantine_path: Path | None = None
    quarantine_failure: str | None = None
    if quarantine_corrupt and path.exists():
        try:
            quarantine_path = quarantine_corrupt_translation_memory_outbox_intent(path)
        except TranslationMemoryOutboxError as exc:
            quarantine_failure = str(exc)
    combined = detail if quarantine_failure is None else f"{detail}; {quarantine_failure}"
    raise TranslationMemoryOutboxCorrupt(
        combined,
        path=path,
        quarantine_path=quarantine_path,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class _CorruptPayload(ValueError):
    pass


__all__ = [
    "LEARN_CONFIRMATION_STATUSES",
    "MAX_OUTBOX_BYTES",
    "RecordedTranslationMemoryOutboxIntent",
    "ReplayReadyTranslationMemoryOutboxIntent",
    "TRANSLATION_MEMORY_OUTBOX_SCHEMA_VERSION",
    "TRANSLATION_LINEAGE_MODES",
    "TranslationMemoryLearnConfirmation",
    "TranslationMemoryOutboxAcknowledgementError",
    "TranslationMemoryOutboxCollision",
    "TranslationMemoryOutboxCorrupt",
    "TranslationMemoryOutboxError",
    "TranslationMemoryOutboxIntent",
    "TranslationMemoryOutboxNotReplayReady",
    "acknowledge_translation_memory_outbox_intent",
    "load_replay_ready_translation_memory_outbox_intent",
    "load_translation_memory_outbox_intent",
    "quarantine_corrupt_translation_memory_outbox_intent",
    "record_translation_memory_outbox_intent",
]
