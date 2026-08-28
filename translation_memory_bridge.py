from __future__ import annotations

"""SRT integration primitives for :mod:`translation_memory`.

This module intentionally does not wire itself into the Worker or translator.
It provides three narrow operations for a future integration point:

* read-only lookup and separation of mature cached blocks from unresolved blocks;
* strict deterministic merge after the unresolved subset has been translated;
* construction of a fail-closed ``VerifiedEpisodeTranslation`` evidence envelope.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Sequence
import unicodedata

from safe_files import atomic_write_text, sha256_file
from srt_utils import SrtBlock, SrtFormatError, validate_srt_structure
from translation_memory import (
    AlignedTranslationBlock,
    MIN_AUTO_APPLY_EPISODE_DIVERSITY,
    MemoryScope,
    STRICT_PUBLICATION_CONTRACT,
    STRICT_PUBLICATION_KIND,
    TranslationMemoryError,
    VerifiedEpisodeTranslation,
    build_context_key,
    lookup_translations_readonly,
    normalize_source_key,
    sha256_text,
)


class TranslationMemoryBridgeError(RuntimeError):
    """Fail-closed bridge error with a deterministic machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


@dataclass(frozen=True)
class StrictPublicationFlags:
    """Flags callers must explicitly obtain from publication and QC evidence."""

    strict_publication_verified: bool
    qc_passed: bool
    unattended: bool
    manual_reviewed: bool
    safe_omission: bool


@dataclass(frozen=True)
class BlockMemoryDecision:
    index: int
    timing: str
    status: str
    source_key: str
    context_key: str
    support_count: int
    episode_diversity: int
    target_hash: str | None


@dataclass(frozen=True)
class TranslationMemorySplit:
    """Detached SRT snapshots returned by the read-only lookup phase."""

    source_blocks: tuple[SrtBlock, ...]
    cached_blocks: tuple[SrtBlock, ...]
    unresolved_blocks: tuple[SrtBlock, ...]
    decisions: tuple[BlockMemoryDecision, ...]

    @property
    def cached_indexes(self) -> tuple[int, ...]:
        return tuple(block.index for block in self.cached_blocks)

    @property
    def unresolved_indexes(self) -> tuple[int, ...]:
        return tuple(block.index for block in self.unresolved_blocks)


@dataclass(frozen=True)
class TranslationMemoryOrigin:
    source_srt_path: str
    translated_srt_path: str
    source_srt_sha256: str
    target_srt_sha256: str
    split_decision_digest: str
    cached_indexes: tuple[int, ...]
    translation_lineage_mode: str
    series_key: str
    policy_version: str


_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_OMISSION_TARGETS = frozenset({"……", "......"})
_MAX_IDENTITY_CHARS = 512
TRANSLATION_MEMORY_SPLIT_CONTRACT = "translation-memory-split-v2"
TRANSLATION_MEMORY_LINEAGE_CONTRACT = "translation-memory-lineage-v2"
TRANSLATION_MEMORY_LINEAGE_MODES = frozenset(
    {"tm_split", "no_hits", "lookup_fallback", "tm_disabled"}
)
_ORIGIN_SCHEMA_VERSION = 2
_ORIGIN_DIRECTORY_NAME = "translation_memory_origins"


def split_blocks_by_readonly_translation_memory(
    database_path: str | Path,
    scope: MemoryScope,
    source_blocks: Sequence[SrtBlock],
    *,
    explicit_series_glossary: Mapping[str, str] | None = None,
) -> TranslationMemorySplit:
    """Separate mature exact-memory hits from blocks requiring translation.

    The lookup path always opens SQLite in read-only mode. A source with
    conflicting learned targets aborts the split; it is never silently sent to
    either the cache or translator branch.
    """

    source = _validated_blocks(source_blocks, role="source", allow_empty=False)
    source_texts = tuple(_block_text(block) for block in source)
    context_keys = _context_keys(source)
    try:
        lookups = lookup_translations_readonly(
            database_path,
            scope,
            source_texts,
            context_keys,
            explicit_series_glossary=explicit_series_glossary,
        )
    except TranslationMemoryError as exc:
        raise TranslationMemoryBridgeError(
            "readonly_lookup_failed",
            str(exc),
        ) from exc
    if len(lookups) != len(source):
        raise TranslationMemoryBridgeError(
            "lookup_block_count_mismatch",
            f"expected {len(source)} lookup decisions, received {len(lookups)}",
        )

    cached: list[SrtBlock] = []
    unresolved: list[SrtBlock] = []
    decisions: list[BlockMemoryDecision] = []
    unresolved_statuses = {
        "not_found",
        "insufficient_support",
        "explicit_glossary_conflict",
    }
    for block, source_text, context_key, lookup in zip(
        source,
        source_texts,
        context_keys,
        lookups,
        strict=True,
    ):
        expected_key = normalize_source_key(source_text)
        if (
            lookup.source_text != source_text
            or lookup.source_key != expected_key
            or lookup.context_key != context_key
        ):
            raise TranslationMemoryBridgeError(
                "lookup_source_mismatch",
                f"lookup decision does not match source block {block.index}",
            )
        if lookup.status == "conflict":
            targets = ", ".join(candidate.target_hash for candidate in lookup.candidates)
            raise TranslationMemoryBridgeError(
                "lookup_conflict",
                f"source block {block.index} has conflicting targets: {targets or 'unknown'}",
            )
        if lookup.status == "auto_apply":
            if (
                lookup.auto_target is None
                or not lookup.auto_target.strip()
                or len(lookup.candidates) != 1
            ):
                raise TranslationMemoryBridgeError(
                    "invalid_mature_lookup",
                    f"source block {block.index} has an incomplete auto-apply decision",
                )
            candidate = lookup.candidates[0]
            normalized_target = normalize_source_key(lookup.auto_target)
            if (
                candidate.target_text != lookup.auto_target
                or candidate.episode_diversity < MIN_AUTO_APPLY_EPISODE_DIVERSITY
                or candidate.support_count < candidate.episode_diversity
                or candidate.target_hash != sha256_text(normalized_target)
            ):
                raise TranslationMemoryBridgeError(
                    "invalid_mature_lookup",
                    f"source block {block.index} does not satisfy mature-memory evidence",
                )
            if not normalized_target or normalized_target in _SAFE_OMISSION_TARGETS:
                raise TranslationMemoryBridgeError(
                    "invalid_mature_target",
                    f"source block {block.index} resolved to an empty or omission target",
                )
            cached.append(
                SrtBlock(
                    index=block.index,
                    timing=block.timing,
                    text=_target_lines(lookup.auto_target),
                )
            )
            decisions.append(
                BlockMemoryDecision(
                    index=block.index,
                    timing=block.timing,
                    status="cached",
                    source_key=lookup.source_key,
                    context_key=context_key,
                    support_count=candidate.support_count,
                    episode_diversity=candidate.episode_diversity,
                    target_hash=candidate.target_hash,
                )
            )
            continue
        if lookup.status not in unresolved_statuses:
            raise TranslationMemoryBridgeError(
                "unknown_lookup_status",
                f"source block {block.index} returned unsupported status {lookup.status!r}",
            )
        unresolved.append(_copy_block(block))
        decisions.append(
            BlockMemoryDecision(
                index=block.index,
                timing=block.timing,
                status="unresolved",
                source_key=lookup.source_key,
                context_key=context_key,
                support_count=(lookup.candidates[0].support_count if len(lookup.candidates) == 1 else 0),
                episode_diversity=(
                    lookup.candidates[0].episode_diversity if len(lookup.candidates) == 1 else 0
                ),
                target_hash=(lookup.candidates[0].target_hash if len(lookup.candidates) == 1 else None),
            )
        )

    if len(cached) + len(unresolved) != len(source):
        raise TranslationMemoryBridgeError(
            "lookup_partition_incomplete",
            "every source block must be assigned to exactly one branch",
        )
    return TranslationMemorySplit(
        source_blocks=tuple(_copy_block(block) for block in source),
        cached_blocks=tuple(cached),
        unresolved_blocks=tuple(unresolved),
        decisions=tuple(decisions),
    )


def merge_translation_memory_blocks(
    source_blocks: Sequence[SrtBlock],
    cached_blocks: Sequence[SrtBlock],
    translated_unresolved_blocks: Sequence[SrtBlock],
) -> list[SrtBlock]:
    """Merge two disjoint result subsets in original source order.

    Index coverage must be exact. Duplicate, overlapping, missing, extra, or
    retimed blocks abort before any partial output is returned.
    """

    source = _validated_blocks(source_blocks, role="source", allow_empty=False)
    cached = _validated_blocks(cached_blocks, role="cached", allow_empty=True)
    translated = _validated_blocks(
        translated_unresolved_blocks,
        role="translated_unresolved",
        allow_empty=True,
    )
    source_by_index = {block.index: block for block in source}
    cached_by_index = {block.index: block for block in cached}
    translated_by_index = {block.index: block for block in translated}

    overlap = sorted(set(cached_by_index) & set(translated_by_index))
    if overlap:
        raise TranslationMemoryBridgeError(
            "merge_index_conflict",
            f"indexes appear in both cached and translated branches: {overlap}",
        )
    supplied_indexes = set(cached_by_index) | set(translated_by_index)
    extra = sorted(supplied_indexes - set(source_by_index))
    if extra:
        raise TranslationMemoryBridgeError(
            "merge_extra_block",
            f"result branches contain indexes absent from source: {extra}",
        )
    missing = sorted(set(source_by_index) - supplied_indexes)
    if missing:
        raise TranslationMemoryBridgeError(
            "merge_missing_block",
            f"result branches do not cover source indexes: {missing}",
        )

    selected = {**cached_by_index, **translated_by_index}
    merged: list[SrtBlock] = []
    for source_block in source:
        result = selected[source_block.index]
        if result.timing != source_block.timing:
            raise TranslationMemoryBridgeError(
                "merge_timing_mismatch",
                f"index {source_block.index} changed timing from {source_block.timing!r} "
                f"to {result.timing!r}",
            )
        merged.append(
            SrtBlock(
                index=source_block.index,
                timing=source_block.timing,
                text=list(result.text),
            )
        )
    try:
        validate_srt_structure(merged)
    except SrtFormatError as exc:  # pragma: no cover - guarded above, kept fail closed
        raise TranslationMemoryBridgeError("invalid_merged_srt", str(exc)) from exc
    return merged


def merge_translation_memory_split(
    split: TranslationMemorySplit,
    translated_unresolved_blocks: Sequence[SrtBlock],
) -> list[SrtBlock]:
    """Convenience wrapper that merges a previously returned split."""

    if not isinstance(split, TranslationMemorySplit):
        raise TranslationMemoryBridgeError(
            "invalid_split",
            "split must be a TranslationMemorySplit",
        )
    return merge_translation_memory_blocks(
        split.source_blocks,
        split.cached_blocks,
        translated_unresolved_blocks,
    )


def translation_memory_split_digest(
    scope: MemoryScope,
    split: TranslationMemorySplit,
) -> str:
    """Hash every lookup decision so a checkpoint cannot cross TM state."""

    validated_scope = _validated_scope(scope)
    if not isinstance(split, TranslationMemorySplit):
        raise TranslationMemoryBridgeError("invalid_split", "split must be a TranslationMemorySplit")
    payload = {
        "contract": TRANSLATION_MEMORY_SPLIT_CONTRACT,
        "scope": {
            "series_key": validated_scope.series_key,
            "policy_version": validated_scope.policy_version,
            "source_language": validated_scope.source_language,
            "target_language": validated_scope.target_language,
        },
        "source_blocks": [
            {
                "index": block.index,
                "timing": block.timing,
                "text_sha256": sha256_text(_block_text(block)),
            }
            for block in split.source_blocks
        ],
        "decisions": [
            {
                "index": decision.index,
                "timing": decision.timing,
                "status": decision.status,
                "source_key": decision.source_key,
                "context_key": decision.context_key,
                "support_count": decision.support_count,
                "episode_diversity": decision.episode_diversity,
                "target_hash": decision.target_hash,
            }
            for decision in split.decisions
        ],
        "cached_blocks": [
            {
                "index": block.index,
                "timing": block.timing,
                "target_sha256": sha256_text(_block_text(block)),
            }
            for block in split.cached_blocks
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def translation_memory_full_plan_digest(
    scope: MemoryScope,
    source_blocks: Sequence[SrtBlock],
    *,
    translation_lineage_mode: str,
) -> str:
    """Hash a zero-hit, disabled, or lookup-fallback full translation plan."""

    validated_scope = _validated_scope(scope)
    mode = _validated_lineage_mode(translation_lineage_mode)
    if mode == "tm_split":
        raise TranslationMemoryBridgeError(
            "invalid_lineage_mode",
            "tm_split plans require translation_memory_split_digest",
        )
    source = _validated_blocks(source_blocks, role="source", allow_empty=False)
    contexts = _context_keys(source)
    payload = {
        "contract": TRANSLATION_MEMORY_SPLIT_CONTRACT,
        "mode": mode,
        "scope": {
            "series_key": validated_scope.series_key,
            "policy_version": validated_scope.policy_version,
            "source_language": validated_scope.source_language,
            "target_language": validated_scope.target_language,
        },
        "source_blocks": [
            {
                "index": block.index,
                "timing": block.timing,
                "text_sha256": sha256_text(_block_text(block)),
                "context_key": context_key,
            }
            for block, context_key in zip(source, contexts, strict=True)
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def translation_memory_origin_path(
    work_path: str | Path,
    translated_srt_path: str | Path,
) -> Path:
    canonical = os.path.normcase(os.path.abspath(str(Path(translated_srt_path))))
    digest = hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()
    return Path(work_path) / _ORIGIN_DIRECTORY_NAME / digest[:2] / f"{digest}.json"


def write_translation_memory_origin(
    work_path: str | Path,
    translated_srt_path: str | Path,
    *,
    source_srt_path: str | Path,
    source_srt_sha256: str,
    target_srt_sha256: str,
    split_decision_digest: str,
    cached_indexes: Sequence[int],
    translation_lineage_mode: str,
    scope: MemoryScope,
) -> Path:
    """Persist the complete translation plan before merged SRT commit.

    Every production translation plan has lineage, including zero-hit,
    lookup-fallback, and TM-disabled plans.  This prevents a missing sidecar
    after restart from being misinterpreted as an empty set of TM origins.
    """

    validated_scope = _validated_scope(scope)
    output = Path(translated_srt_path)
    source = Path(source_srt_path)
    canonical = os.path.normcase(os.path.abspath(str(output)))
    canonical_source = os.path.normcase(os.path.abspath(str(source)))
    source_hash = _hash(source_srt_sha256, "source_srt_sha256")
    output_hash = _hash(target_srt_sha256, "target_srt_sha256")
    decision_hash = _hash(split_decision_digest, "split_decision_digest")
    indexes = _validated_positive_indexes(cached_indexes, field="cached_indexes")
    mode = _validated_lineage_mode(translation_lineage_mode)
    if mode == "tm_split" and not indexes:
        raise TranslationMemoryBridgeError(
            "empty_origin_indexes",
            "tm_split lineage requires at least one cached index",
        )
    if mode != "tm_split" and indexes:
        raise TranslationMemoryBridgeError(
            "unexpected_origin_indexes",
            f"{mode} lineage requires an explicit empty cached-index set",
        )
    try:
        actual_source_hash = sha256_file(source)
    except OSError as exc:
        raise TranslationMemoryBridgeError("origin_source_unavailable", str(source)) from exc
    if actual_source_hash != source_hash:
        raise TranslationMemoryBridgeError(
            "origin_source_hash_mismatch",
            f"expected {source_hash}, got {actual_source_hash}",
        )
    path = translation_memory_origin_path(work_path, output)
    payload = {
        "schema_version": _ORIGIN_SCHEMA_VERSION,
        "contract": TRANSLATION_MEMORY_LINEAGE_CONTRACT,
        "source_srt_path": canonical_source,
        "translated_srt_path": canonical,
        "source_srt_sha256": source_hash,
        "target_srt_sha256": output_hash,
        "split_decision_digest": decision_hash,
        "cached_indexes": list(indexes),
        "translation_lineage_mode": mode,
        "scope": {
            "series_key": validated_scope.series_key,
            "policy_version": validated_scope.policy_version,
            "source_language": validated_scope.source_language,
            "target_language": validated_scope.target_language,
        },
    }
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return path


def remove_translation_memory_origin(
    work_path: str | Path,
    translated_srt_path: str | Path,
) -> None:
    translation_memory_origin_path(work_path, translated_srt_path).unlink(missing_ok=True)


def read_translation_memory_origin_strict(
    work_path: str | Path,
    translated_srt_path: str | Path,
) -> TranslationMemoryOrigin | None:
    output = Path(translated_srt_path)
    path = translation_memory_origin_path(work_path, output)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TranslationMemoryBridgeError("origin_unreadable", str(exc)) from exc
    canonical = os.path.normcase(os.path.abspath(str(output)))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _ORIGIN_SCHEMA_VERSION
        or payload.get("contract") != TRANSLATION_MEMORY_LINEAGE_CONTRACT
        or payload.get("translated_srt_path") != canonical
    ):
        raise TranslationMemoryBridgeError("origin_contract_mismatch", str(path))
    scope_payload = payload.get("scope")
    if not isinstance(scope_payload, dict):
        raise TranslationMemoryBridgeError("origin_scope_invalid", str(path))
    scope = _validated_scope(
        MemoryScope(
            str(scope_payload.get("series_key") or ""),
            str(scope_payload.get("policy_version") or ""),
            str(scope_payload.get("source_language") or ""),
            str(scope_payload.get("target_language") or ""),
        )
    )
    source_path = Path(str(payload.get("source_srt_path") or ""))
    canonical_source = os.path.normcase(os.path.abspath(str(source_path)))
    if payload.get("source_srt_path") != canonical_source:
        raise TranslationMemoryBridgeError("origin_source_path_invalid", str(path))
    expected_source_hash = _hash(
        payload.get("source_srt_sha256"),
        "source_srt_sha256",
    )
    expected_hash = _hash(payload.get("target_srt_sha256"), "target_srt_sha256")
    try:
        actual_source_hash = sha256_file(source_path)
        actual_hash = sha256_file(output)
    except OSError as exc:
        raise TranslationMemoryBridgeError("origin_target_unavailable", str(output)) from exc
    if actual_source_hash != expected_source_hash:
        raise TranslationMemoryBridgeError("origin_source_hash_mismatch", str(path))
    if actual_hash != expected_hash:
        raise TranslationMemoryBridgeError("origin_hash_mismatch", str(path))
    indexes = _validated_positive_indexes(payload.get("cached_indexes"), field="cached_indexes")
    mode = _validated_lineage_mode(payload.get("translation_lineage_mode"))
    if mode == "tm_split" and not indexes:
        raise TranslationMemoryBridgeError("empty_origin_indexes", str(path))
    if mode != "tm_split" and indexes:
        raise TranslationMemoryBridgeError("unexpected_origin_indexes", str(path))
    return TranslationMemoryOrigin(
        source_srt_path=canonical_source,
        translated_srt_path=canonical,
        source_srt_sha256=expected_source_hash,
        target_srt_sha256=expected_hash,
        split_decision_digest=_hash(
            payload.get("split_decision_digest"),
            "split_decision_digest",
        ),
        cached_indexes=indexes,
        translation_lineage_mode=mode,
        series_key=scope.series_key,
        policy_version=scope.policy_version,
    )


def build_strict_verified_episode_translation(
    scope: MemoryScope,
    source_blocks: Sequence[SrtBlock],
    target_blocks: Sequence[SrtBlock],
    *,
    episode_id: str,
    manifest_identity: str,
    source_manifest_hash: str,
    target_manifest_hash: str,
    verified_at: str,
    flags: StrictPublicationFlags,
    excluded_origin_indexes: Sequence[int] = (),
) -> VerifiedEpisodeTranslation:
    """Build strict ja→zh-CN learning evidence from aligned full SRT output.

    Hashes and all publication/QC flags are mandatory caller inputs. Invalid
    flags never produce a partially trusted evidence object.
    """

    validated_scope = _validated_scope(scope)
    _validate_strict_flags(flags)
    source = _validated_blocks(source_blocks, role="evidence_source", allow_empty=False)
    target = _validated_blocks(target_blocks, role="evidence_target", allow_empty=False)
    if len(source) != len(target):
        raise TranslationMemoryBridgeError(
            "evidence_block_count_mismatch",
            f"source has {len(source)} blocks while target has {len(target)}",
        )
    source_indexes = [block.index for block in source]
    target_indexes = [block.index for block in target]
    if source_indexes != target_indexes:
        raise TranslationMemoryBridgeError(
            "evidence_index_mismatch",
            f"source indexes {source_indexes} do not match target indexes {target_indexes}",
        )
    for source_block, target_block in zip(source, target):
        if source_block.timing != target_block.timing:
            raise TranslationMemoryBridgeError(
                "evidence_timing_mismatch",
                f"index {source_block.index} changed timing from {source_block.timing!r} "
                f"to {target_block.timing!r}",
            )
        normalized_target = normalize_source_key(_block_text(target_block))
        if normalized_target in _SAFE_OMISSION_TARGETS:
            raise TranslationMemoryBridgeError(
                "evidence_safe_omission_placeholder",
                f"target block {target_block.index} is the safe-omission placeholder",
            )

    normalized_episode = _identity(episode_id, "episode_id")
    normalized_manifest = _identity(manifest_identity, "manifest_identity")
    normalized_source_hash = _hash(source_manifest_hash, "source_manifest_hash")
    normalized_target_hash = _hash(target_manifest_hash, "target_manifest_hash")
    normalized_verified_at = _timestamp(verified_at)
    excluded = _validated_excluded_indexes(excluded_origin_indexes, source_indexes)
    context_keys = _context_keys(source)
    aligned = tuple(
        AlignedTranslationBlock(
            block_identity=_block_identity(source_block),
            source_text=_block_text(source_block),
            target_text=_block_text(target_block),
            context_key=context_key,
            qc_passed=True,
            manual_reviewed=False,
            safe_omission=False,
        )
        for source_block, target_block, context_key in zip(
            source,
            target,
            context_keys,
            strict=True,
        )
        if source_block.index not in excluded
    )
    if not aligned:
        raise TranslationMemoryBridgeError(
            "no_learnable_blocks",
            "every aligned block originated from translation memory",
        )
    return VerifiedEpisodeTranslation(
        series_key=validated_scope.series_key,
        policy_version=validated_scope.policy_version,
        episode_id=normalized_episode,
        manifest_identity=normalized_manifest,
        publication_contract=STRICT_PUBLICATION_CONTRACT,
        publication_kind=STRICT_PUBLICATION_KIND,
        source_language=validated_scope.source_language,
        target_language=validated_scope.target_language,
        strict_publication_verified=True,
        qc_passed=True,
        unattended=True,
        manual_reviewed=False,
        safe_omission=False,
        source_manifest_hash=normalized_source_hash,
        target_manifest_hash=normalized_target_hash,
        verified_at=normalized_verified_at,
        blocks=aligned,
    )


def _context_keys(blocks: Sequence[SrtBlock]) -> tuple[str, ...]:
    texts = tuple(_block_text(block) for block in blocks)
    return tuple(
        build_context_key(
            texts[position - 1] if position > 0 else None,
            texts[position + 1] if position + 1 < len(texts) else None,
        )
        for position in range(len(texts))
    )


def _validated_positive_indexes(values: object, *, field: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise TranslationMemoryBridgeError("invalid_indexes", f"{field} must be an integer list")
    try:
        raw_values = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TranslationMemoryBridgeError(
            "invalid_indexes",
            f"{field} must be an integer list",
        ) from exc
    normalized: list[int] = []
    for value in raw_values:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise TranslationMemoryBridgeError(
                "invalid_indexes",
                f"{field} contains invalid index {value!r}",
            )
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise TranslationMemoryBridgeError("duplicate_indexes", f"{field} contains duplicates")
    if normalized != sorted(normalized):
        raise TranslationMemoryBridgeError("unsorted_indexes", f"{field} must be sorted")
    return tuple(normalized)


def _validated_excluded_indexes(
    values: Sequence[int],
    source_indexes: Sequence[int],
) -> frozenset[int]:
    normalized = _validated_positive_indexes(values, field="excluded_origin_indexes")
    unknown = sorted(set(normalized) - set(source_indexes))
    if unknown:
        raise TranslationMemoryBridgeError(
            "unknown_origin_indexes",
            f"excluded origin indexes are absent from source: {unknown}",
        )
    return frozenset(normalized)


def _validated_blocks(
    blocks: Sequence[SrtBlock],
    *,
    role: str,
    allow_empty: bool,
) -> tuple[SrtBlock, ...]:
    try:
        items = tuple(blocks)
    except TypeError as exc:
        raise TranslationMemoryBridgeError(
            "invalid_srt_blocks",
            f"{role} blocks must be an iterable of SrtBlock values",
        ) from exc
    if not items:
        if allow_empty:
            return ()
        raise TranslationMemoryBridgeError("empty_srt_blocks", f"{role} blocks cannot be empty")
    seen: set[int] = set()
    detached: list[SrtBlock] = []
    for position, block in enumerate(items, start=1):
        if not isinstance(block, SrtBlock):
            raise TranslationMemoryBridgeError(
                "invalid_srt_block",
                f"{role} item {position} is not an SrtBlock",
            )
        if not isinstance(block.index, int) or isinstance(block.index, bool) or block.index <= 0:
            raise TranslationMemoryBridgeError(
                "invalid_srt_index",
                f"{role} item {position} has invalid index {block.index!r}",
            )
        if block.index in seen:
            raise TranslationMemoryBridgeError(
                "duplicate_srt_index",
                f"{role} contains duplicate index {block.index}",
            )
        seen.add(block.index)
        if (
            not isinstance(block.timing, str)
            or not isinstance(block.text, list)
            or not block.text
            or not all(isinstance(line, str) for line in block.text)
        ):
            raise TranslationMemoryBridgeError(
                "invalid_srt_block",
                f"{role} index {block.index} has invalid timing or text fields",
            )
        detached.append(_copy_block(block))
    try:
        validate_srt_structure(detached)
    except SrtFormatError as exc:
        raise TranslationMemoryBridgeError("invalid_srt_structure", f"{role}: {exc}") from exc
    return tuple(detached)


def _validated_scope(scope: MemoryScope) -> MemoryScope:
    if not isinstance(scope, MemoryScope):
        raise TranslationMemoryBridgeError("invalid_scope", "scope must be a MemoryScope")
    series_key = _identity(scope.series_key, "scope.series_key")
    policy_version = _identity(scope.policy_version, "scope.policy_version")
    if scope.source_language != "ja" or scope.target_language != "zh-CN":
        raise TranslationMemoryBridgeError(
            "unsupported_scope_languages",
            "strict evidence bridge supports only ja to zh-CN",
        )
    return MemoryScope(
        series_key=series_key,
        policy_version=policy_version,
        source_language=scope.source_language,
        target_language=scope.target_language,
    )


def _validated_lineage_mode(value: object) -> str:
    mode = str(value or "").strip()
    if mode not in TRANSLATION_MEMORY_LINEAGE_MODES:
        raise TranslationMemoryBridgeError(
            "invalid_lineage_mode",
            f"translation lineage mode must be one of {sorted(TRANSLATION_MEMORY_LINEAGE_MODES)}",
        )
    return mode


def _validate_strict_flags(flags: StrictPublicationFlags) -> None:
    if not isinstance(flags, StrictPublicationFlags):
        raise TranslationMemoryBridgeError(
            "invalid_publication_flags",
            "flags must be StrictPublicationFlags",
        )
    required_true = {
        "strict_publication_verified": flags.strict_publication_verified,
        "qc_passed": flags.qc_passed,
        "unattended": flags.unattended,
    }
    required_false = {
        "manual_reviewed": flags.manual_reviewed,
        "safe_omission": flags.safe_omission,
    }
    invalid = [name for name, value in required_true.items() if value is not True]
    invalid.extend(name for name, value in required_false.items() if value is not False)
    if invalid:
        raise TranslationMemoryBridgeError(
            "strict_publication_flags_rejected",
            "strict evidence requirements failed: " + ", ".join(invalid),
        )


def _block_text(block: SrtBlock) -> str:
    return "\n".join(block.text)


def _copy_block(block: SrtBlock) -> SrtBlock:
    return SrtBlock(index=block.index, timing=block.timing, text=list(block.text))


def _target_lines(target: str) -> list[str]:
    normalized = target.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or not any(line.strip() for line in lines):
        raise TranslationMemoryBridgeError("invalid_mature_target", "mature target is empty")
    return lines


def _block_identity(block: SrtBlock) -> str:
    timing_hash = sha256_text(block.timing)[:24]
    return f"srt:{block.index}:timing-sha256:{timing_hash}"


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TranslationMemoryBridgeError("invalid_identity", f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or "\x00" in normalized or len(normalized) > _MAX_IDENTITY_CHARS:
        raise TranslationMemoryBridgeError(
            "invalid_identity",
            f"{field} cannot be empty, contain NUL, or exceed {_MAX_IDENTITY_CHARS} characters",
        )
    return normalized


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise TranslationMemoryBridgeError(
            "invalid_manifest_hash",
            f"{field} must be a 64-character SHA-256 hex digest",
        )
    return value.casefold()


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranslationMemoryBridgeError(
            "invalid_verified_at",
            "verified_at must be an ISO-8601 timestamp with timezone",
        )
    candidate = value.strip()
    parsed_input = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
    try:
        parsed = datetime.fromisoformat(parsed_input)
    except ValueError as exc:
        raise TranslationMemoryBridgeError(
            "invalid_verified_at",
            "verified_at must be an ISO-8601 timestamp with timezone",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TranslationMemoryBridgeError(
            "invalid_verified_at",
            "verified_at must include a timezone",
        )
    return candidate


__all__ = [
    "BlockMemoryDecision",
    "StrictPublicationFlags",
    "TranslationMemoryBridgeError",
    "TranslationMemoryOrigin",
    "TranslationMemorySplit",
    "TRANSLATION_MEMORY_LINEAGE_CONTRACT",
    "TRANSLATION_MEMORY_LINEAGE_MODES",
    "TRANSLATION_MEMORY_SPLIT_CONTRACT",
    "build_strict_verified_episode_translation",
    "merge_translation_memory_blocks",
    "merge_translation_memory_split",
    "read_translation_memory_origin_strict",
    "remove_translation_memory_origin",
    "split_blocks_by_readonly_translation_memory",
    "translation_memory_origin_path",
    "translation_memory_full_plan_digest",
    "translation_memory_split_digest",
    "write_translation_memory_origin",
]
