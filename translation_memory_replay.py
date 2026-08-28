from __future__ import annotations

"""Fail-closed consumer for one durable translation-memory outbox intent."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any
import unicodedata

from srt_utils import SrtBlock, SrtFormatError, parse_srt
from translation_memory import MAX_IDENTITY_CHARS, TranslationMemoryError, TranslationMemoryStore
from translation_memory_bridge import (
    StrictPublicationFlags,
    TranslationMemoryBridgeError,
    build_strict_verified_episode_translation,
)
from translation_memory_outbox import (
    TranslationMemoryLearnConfirmation,
    TranslationMemoryOutboxError,
    acknowledge_translation_memory_outbox_intent,
    load_replay_ready_translation_memory_outbox_intent,
)


class TranslationMemoryReplayError(RuntimeError):
    """One replay attempt failed without acknowledging its durable intent."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


@dataclass(frozen=True)
class TranslationMemoryReplayResult:
    outbox_path: Path
    intent_id: str
    status: str
    manifest_identity: str | None
    inserted_blocks: int
    acknowledged: bool


def replay_translation_memory_outbox_intent(
    outbox_path: str | Path,
    *,
    database_path: str | Path,
) -> TranslationMemoryReplayResult:
    """Replay exactly one outbox entry and acknowledge only durable success.

    Corrupt envelopes are deliberately not quarantined by this consumer.  All
    load, evidence, snapshot, and database failures leave the original outbox
    pathname present so the supervisor can retry or surface the same evidence.
    """

    candidate = Path(outbox_path)
    try:
        replay_ready = load_replay_ready_translation_memory_outbox_intent(
            candidate,
            quarantine_corrupt=False,
        )
    except TranslationMemoryOutboxError as exc:
        raise TranslationMemoryReplayError("outbox_not_replay_ready", str(exc)) from exc

    intent = replay_ready.intent
    if replay_ready.is_auditable_noop:
        acknowledged = _acknowledge(candidate, intent, status="no_new_blocks")
        return TranslationMemoryReplayResult(
            outbox_path=candidate,
            intent_id=intent.intent_id,
            status="no_new_blocks",
            manifest_identity=None,
            inserted_blocks=0,
            acknowledged=acknowledged,
        )

    try:
        manifest_identity = _strict_manifest_identity(intent)
        source_blocks = _read_hash_bound_srt(
            intent.source_snapshot_path,
            intent.source_srt_sha256,
            role="source",
        )
        target_blocks = _read_hash_bound_srt(
            intent.target_snapshot_path,
            intent.target_srt_sha256,
            role="target",
        )
        source_indexes = tuple(block.index for block in source_blocks)
        target_indexes = tuple(block.index for block in target_blocks)
        if source_indexes != intent.source_block_indexes or target_indexes != source_indexes:
            raise TranslationMemoryReplayError(
                "snapshot_index_mismatch",
                "immutable source/target indexes do not match the replay-ready intent",
            )
        expected_learnable = tuple(
            index for index in source_indexes if index not in frozenset(intent.tm_origin_indexes)
        )
        if expected_learnable != replay_ready.learnable_indexes:
            raise TranslationMemoryReplayError(
                "learnable_index_mismatch",
                "derived learnable indexes do not exactly match the outbox contract",
            )

        evidence = build_strict_verified_episode_translation(
            intent.scope,
            source_blocks,
            target_blocks,
            episode_id=intent.episode_id,
            manifest_identity=manifest_identity,
            source_manifest_hash=intent.source_srt_sha256,
            target_manifest_hash=intent.target_srt_sha256,
            verified_at=intent.created_at,
            flags=StrictPublicationFlags(
                strict_publication_verified=True,
                qc_passed=True,
                unattended=True,
                manual_reviewed=False,
                safe_omission=False,
            ),
            excluded_origin_indexes=intent.tm_origin_indexes,
        )
        with TranslationMemoryStore(database_path) as store:
            learned = store.learn_episode(intent.scope, evidence)
    except TranslationMemoryReplayError:
        raise
    except (OSError, UnicodeError, SrtFormatError) as exc:
        raise TranslationMemoryReplayError("snapshot_read_failed", str(exc)) from exc
    except TranslationMemoryBridgeError as exc:
        raise TranslationMemoryReplayError("evidence_rejected", str(exc)) from exc
    except TranslationMemoryError as exc:
        raise TranslationMemoryReplayError("database_learn_failed", str(exc)) from exc

    if learned.status not in {"learned", "idempotent"}:
        raise TranslationMemoryReplayError(
            "unexpected_learn_status",
            f"translation-memory learner returned {learned.status!r}",
        )
    acknowledged = _acknowledge(candidate, intent, status=learned.status)
    return TranslationMemoryReplayResult(
        outbox_path=candidate,
        intent_id=intent.intent_id,
        status=learned.status,
        manifest_identity=manifest_identity,
        inserted_blocks=learned.inserted_blocks,
        acknowledged=acknowledged,
    )


def _acknowledge(candidate: Path, intent: Any, *, status: str) -> bool:
    try:
        return acknowledge_translation_memory_outbox_intent(
            candidate,
            TranslationMemoryLearnConfirmation(
                intent_sha256=intent.intent_sha256,
                manifest_sha256=intent.manifest_sha256,
                status=status,
                idempotent_learn_confirmed=True,
            ),
        )
    except TranslationMemoryOutboxError as exc:
        raise TranslationMemoryReplayError("outbox_ack_failed", str(exc)) from exc


def _strict_manifest_identity(intent: Any) -> str:
    try:
        raw = Path(intent.manifest_path).read_bytes()
    except OSError as exc:
        raise TranslationMemoryReplayError("manifest_read_failed", str(exc)) from exc
    if hashlib.sha256(raw).hexdigest() != intent.manifest_sha256:
        raise TranslationMemoryReplayError(
            "manifest_hash_mismatch",
            "publication manifest changed after replay-ready validation",
        )
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TranslationMemoryReplayError("manifest_invalid", str(exc)) from exc
    if not isinstance(payload, dict):
        raise TranslationMemoryReplayError("manifest_invalid", "manifest root must be an object")
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        raise TranslationMemoryReplayError("manifest_invalid", "delivery must be an object")
    if delivery.get("contract") != "ai-delivery-v1":
        raise TranslationMemoryReplayError("manifest_invalid", "delivery contract is not strict")
    obligation_id = _canonical_identity(
        delivery.get("obligation_id"),
        "delivery.obligation_id",
    )
    policy_revision = _canonical_identity(
        delivery.get("policy_revision"),
        "delivery.policy_revision",
    )
    if policy_revision != intent.scope.policy_version:
        raise TranslationMemoryReplayError(
            "manifest_scope_mismatch",
            "delivery policy revision does not exactly match the TM scope",
        )
    return obligation_id


def _read_hash_bound_srt(path: str | Path, expected_hash: str, *, role: str) -> list[SrtBlock]:
    candidate = Path(path)
    raw = candidate.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_hash:
        raise TranslationMemoryReplayError(
            "snapshot_hash_mismatch",
            f"{role} snapshot changed after replay-ready validation",
        )
    return parse_srt(raw.decode("utf-8-sig"))


def _canonical_identity(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TranslationMemoryReplayError("manifest_invalid", f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or normalized != value
        or "\x00" in normalized
        or len(normalized) > MAX_IDENTITY_CHARS
    ):
        raise TranslationMemoryReplayError(
            "manifest_invalid",
            f"{field} must be a canonical non-empty identity",
        )
    return normalized


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


__all__ = [
    "TranslationMemoryReplayError",
    "TranslationMemoryReplayResult",
    "replay_translation_memory_outbox_intent",
]
