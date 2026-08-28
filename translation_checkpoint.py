from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from safe_files import atomic_write_text, fsync_directory
from srt_utils import SrtBlock


TRANSLATION_CHECKPOINT_SCHEMA_VERSION = 3

_QUALITY_EVENT_KEYS = frozenset(
    {
        "code",
        "severity",
        "index",
        "message",
        "source",
        "output",
        "reason",
        "batch_index",
    }
)
_QUALITY_EVENT_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_QUALITY_EVENT_BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_QUALITY_EVENT_SEVERITIES = frozenset({"fail", "warn"})


class TranslationCheckpointError(RuntimeError):
    """Raised when a translation checkpoint cannot be persisted safely."""


def translation_checkpoint_signature(
    source_blocks: Sequence[SrtBlock],
    *,
    output_path: str | Path,
    batch_size: int,
    translation_context: str,
    glossary: dict[str, str],
    model_chain: Sequence[str],
    source_language: str = "ja",
    translation_memory_decision_digest: str = "",
) -> str:
    memory_digest = str(translation_memory_decision_digest or "").strip().casefold()
    if memory_digest and (
        len(memory_digest) != 64
        or any(character not in "0123456789abcdef" for character in memory_digest)
    ):
        raise TranslationCheckpointError(
            "translation-memory decision digest must be a SHA-256 hex value"
        )
    payload = {
        "schema_version": TRANSLATION_CHECKPOINT_SCHEMA_VERSION,
        "output_path": _canonical_path(output_path),
        "batch_size": int(batch_size),
        "translation_context": str(translation_context),
        "glossary": sorted((str(key), str(value)) for key, value in glossary.items()),
        "model_chain": [str(model) for model in model_chain],
        "source_language": str(source_language or "ja").strip().replace("_", "-").casefold(),
        "translation_memory_decision_digest": memory_digest,
        "source_blocks": [_block_payload(block) for block in source_blocks],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def translation_checkpoint_path(
    work_path: str | Path,
    output_path: str | Path,
) -> Path:
    output_key = hashlib.sha256(
        _canonical_path(output_path).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return Path(work_path) / "translation_checkpoints" / output_key[:2] / f"{output_key}.json"


def load_translation_checkpoint(
    path: str | Path,
    *,
    signature: str,
    batches: Sequence[Sequence[SrtBlock]],
) -> tuple[list[SrtBlock], int, str | None, list[dict[str, Any]]]:
    checkpoint = Path(path)
    if not checkpoint.is_file():
        return [], 0, None, []
    try:
        payload = json.loads(
            checkpoint.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TranslationCheckpointError):
        return [], 0, None, []
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != TRANSLATION_CHECKPOINT_SCHEMA_VERSION
        or payload.get("signature") != signature
    ):
        return [], 0, None, []
    completed = payload.get("completed_batches")
    if not isinstance(completed, list) or len(completed) > len(batches):
        return [], 0, None, []

    restored: list[SrtBlock] = []
    for expected_position, entry in enumerate(completed, start=1):
        if not isinstance(entry, dict) or entry.get("batch_number") != expected_position:
            return [], 0, None, []
        raw_blocks = entry.get("blocks")
        if not isinstance(raw_blocks, list):
            return [], 0, None, []
        try:
            translated = [_block_from_payload(item) for item in raw_blocks]
        except (KeyError, TypeError, ValueError):
            return [], 0, None, []
        expected_batch = list(batches[expected_position - 1])
        if len(translated) != len(expected_batch):
            return [], 0, None, []
        for source, target in zip(expected_batch, translated):
            if source.index != target.index or source.timing != target.timing:
                return [], 0, None, []
            if not any(str(line).strip() for line in target.text):
                return [], 0, None, []
        restored.extend(translated)
    last_model = payload.get("last_model")
    if last_model is not None and not isinstance(last_model, str):
        return [], 0, None, []
    try:
        quality_events = _normalize_quality_events(
            payload.get("quality_events"),
            allowed_indexes={block.index for block in restored},
        )
    except TranslationCheckpointError:
        return [], 0, None, []
    return restored, len(completed), last_model, quality_events


def write_translation_checkpoint(
    path: str | Path,
    *,
    signature: str,
    output_path: str | Path,
    completed_batches: Sequence[Sequence[SrtBlock]],
    last_model: str,
    quality_events: Sequence[Mapping[str, Any]],
) -> Path:
    checkpoint = Path(path)
    completed = tuple(tuple(batch) for batch in completed_batches)
    completed_indexes = [block.index for batch in completed for block in batch]
    if len(set(completed_indexes)) != len(completed_indexes):
        raise TranslationCheckpointError(
            "completed translation checkpoint batches contain duplicate block indexes"
        )
    normalized_events = _normalize_quality_events(
        quality_events,
        allowed_indexes=set(completed_indexes),
    )
    payload: dict[str, Any] = {
        "schema_version": TRANSLATION_CHECKPOINT_SCHEMA_VERSION,
        "signature": str(signature),
        "output_path": _canonical_path(output_path),
        "last_model": str(last_model),
        "quality_events": normalized_events,
        "completed_batches": [
            {
                "batch_number": position,
                "blocks": [_block_payload(block) for block in batch],
            }
            for position, batch in enumerate(completed, start=1)
        ],
    }
    try:
        atomic_write_text(
            checkpoint,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    except Exception as exc:
        raise TranslationCheckpointError(
            f"Could not persist translation checkpoint {checkpoint}: {exc}"
        ) from exc
    return checkpoint


def remove_translation_checkpoint(path: str | Path) -> None:
    checkpoint = Path(path)
    try:
        checkpoint.unlink(missing_ok=True)
        if checkpoint.parent.exists():
            fsync_directory(checkpoint.parent)
    except OSError as exc:
        raise TranslationCheckpointError(
            f"Could not remove completed translation checkpoint {checkpoint}: {exc}"
        ) from exc


def _block_payload(block: SrtBlock) -> dict[str, Any]:
    return {
        "index": int(block.index),
        "timing": str(block.timing),
        "text": [str(line) for line in block.text],
    }


def _block_from_payload(payload: Any) -> SrtBlock:
    if not isinstance(payload, dict):
        raise TypeError("checkpoint block must be an object")
    text = payload["text"]
    if not isinstance(text, list) or not all(isinstance(line, str) for line in text):
        raise TypeError("checkpoint block text must be a string list")
    return SrtBlock(
        int(payload["index"]),
        str(payload["timing"]),
        list(text),
    )


def _normalize_quality_events(
    values: object,
    *,
    allowed_indexes: set[int],
) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes)):
        raise TranslationCheckpointError("checkpoint quality_events must be a list")
    try:
        events = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise TranslationCheckpointError(
            "checkpoint quality_events must be a list"
        ) from exc

    normalized: list[dict[str, Any]] = []
    for position, value in enumerate(events, start=1):
        if not isinstance(value, Mapping):
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} must be an object"
            )
        if frozenset(value.keys()) != _QUALITY_EVENT_KEYS:
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} has invalid fields"
            )

        index = value["index"]
        if type(index) is not int or index <= 0:
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} has an invalid index"
            )
        if index not in allowed_indexes:
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} index {index} is not restored"
            )

        raw_code = value["code"]
        raw_severity = value["severity"]
        raw_batch_index = value["batch_index"]
        if not all(
            isinstance(item, str)
            for item in (
                raw_code,
                raw_severity,
                raw_batch_index,
                value["message"],
                value["source"],
                value["output"],
                value["reason"],
            )
        ):
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} fields must be strings"
            )

        code = raw_code.strip().casefold()
        severity = raw_severity.strip().casefold()
        batch_index = raw_batch_index.strip()
        if _QUALITY_EVENT_CODE_RE.fullmatch(code) is None:
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} has an invalid code"
            )
        if severity not in _QUALITY_EVENT_SEVERITIES:
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} has an invalid severity"
            )
        if _QUALITY_EVENT_BATCH_RE.fullmatch(batch_index) is None:
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} has an invalid batch_index"
            )

        message = _normalized_event_text(value["message"], 320)
        if not message:
            raise TranslationCheckpointError(
                f"checkpoint quality event {position} has an empty message"
            )
        normalized.append(
            {
                "code": code,
                "severity": severity,
                "index": index,
                "message": message,
                "source": _normalized_event_text(value["source"], 240),
                "output": _normalized_event_text(value["output"], 240),
                "reason": _normalized_event_text(value["reason"], 320),
                "batch_index": batch_index,
            }
        )
    return normalized


def _normalized_event_text(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TranslationCheckpointError(
                f"checkpoint JSON contains duplicate key {key!r}"
            )
        result[key] = value
    return result


def _canonical_path(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(Path(path))))
