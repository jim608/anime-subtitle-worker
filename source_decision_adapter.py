from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

from audio import AudioStreamInfo, probe_audio_stream_manifest
from safe_files import sha256_file
from source_analyzer import (
    ASR_JA_AUDIO,
    CONVERT_ZH_CN as M2_CONVERT_ZH_CN,
    NEEDS_REVIEW,
    NORMALIZE_ZH_HANT,
    TRANSLATE_JA_SUBTITLE,
    UNSUPPORTED,
    USE_EXISTING_ZH_TW,
    normalize_language_tag,
)
from source_decision import (
    CONVERT_ZH_CN,
    TRANSLATE_JAPANESE,
    USE_ZH_TW,
    SubtitleSourceDecision,
)
from source_inventory import (
    SourceInventoryError,
    build_source_input_identity,
    materialize_selected_subtitle,
)
from subtitle_extract import classify_subtitle_content_file
from subtitle_quality import analyze_subtitle_file


class SourceDecisionAdapterError(RuntimeError):
    """A persisted M2 decision cannot be safely routed to the legacy worker."""


class SourceDecisionReviewError(SourceDecisionAdapterError):
    """The materialized source fails content validation and needs review."""


@dataclass(frozen=True)
class ResolvedSourceDecision:
    strategy: str
    decision_id: str
    decision: Mapping[str, Any]
    subtitle: SubtitleSourceDecision | None = None
    audio: AudioStreamInfo | None = None


_SUBTITLE_STRATEGIES = frozenset(
    {
        USE_EXISTING_ZH_TW,
        NORMALIZE_ZH_HANT,
        M2_CONVERT_ZH_CN,
        TRANSLATE_JA_SUBTITLE,
    }
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def resolve_source_decision(
    video_path: str | Path,
    persisted: Mapping[str, Any],
    media_job_identity: Mapping[str, Any],
    config: object,
) -> ResolvedSourceDecision:
    """Materialize and verify the exact source named by a persisted decision."""

    video = Path(video_path)
    decision = _decision_payload(persisted)
    strategy = str(decision.get("strategy") or "").strip().upper()
    decision_id = str(persisted.get("decision_id") or decision.get("decision_id") or "").strip()
    if strategy not in _SUBTITLE_STRATEGIES | {
        ASR_JA_AUDIO,
        NEEDS_REVIEW,
        UNSUPPORTED,
    }:
        raise SourceDecisionAdapterError(f"unsupported M2 source strategy: {strategy or 'missing'}")

    if strategy in {NEEDS_REVIEW, UNSUPPORTED}:
        return ResolvedSourceDecision(strategy, decision_id, decision)

    candidate_fingerprint = _validate_executable_source_context(
        video,
        persisted,
        decision,
        media_job_identity,
        config,
    )
    if strategy == ASR_JA_AUDIO:
        selected_audio = _required_mapping(
            decision.get("selected_audio_track"),
            "selected_audio_track",
        )
        index = _required_nonnegative_int(selected_audio.get("index"), "audio index")
        if str(selected_audio.get("kind") or "").strip().casefold() != "audio":
            raise SourceDecisionAdapterError("ASR_JA_AUDIO selected candidate kind is invalid")
        if str(selected_audio.get("source_kind") or "").strip().casefold() != "embedded":
            raise SourceDecisionAdapterError("ASR_JA_AUDIO source_kind must be embedded")
        if str(selected_audio.get("source_reference") or "").strip() != f"stream:{index}":
            raise SourceDecisionAdapterError(
                "ASR_JA_AUDIO source_reference does not match selected stream index"
            )
        persisted_languages = (
            selected_audio.get("detected_language"),
            selected_audio.get("normalized_language_tag"),
            selected_audio.get("container_language_tag"),
        )
        if not any(normalize_language_tag(value) == "ja" for value in persisted_languages):
            raise SourceDecisionAdapterError("ASR_JA_AUDIO selected track is not Japanese")
        if selected_audio.get("commentary") is not False:
            raise SourceDecisionAdapterError("ASR_JA_AUDIO cannot route commentary audio")

        try:
            manifest = probe_audio_stream_manifest(video)
        except (OSError, TypeError, ValueError) as exc:
            raise SourceDecisionAdapterError("ASR_JA_AUDIO ffprobe validation failed") from exc
        if not manifest.complete:
            detail = str(manifest.error or "unknown ffprobe error")
            raise SourceDecisionAdapterError(
                f"ASR_JA_AUDIO audio inventory is incomplete: {detail}"
            )
        matches = tuple(stream for stream in manifest.streams if stream.index == index)
        if len(matches) != 1:
            raise SourceDecisionAdapterError(
                "ASR_JA_AUDIO selected stream index is missing or ambiguous"
            )
        current_audio = matches[0]
        if current_audio.commentary:
            raise SourceDecisionAdapterError("ASR_JA_AUDIO cannot route commentary audio")
        if normalize_language_tag(current_audio.language) != "ja":
            raise SourceDecisionAdapterError(
                "ASR_JA_AUDIO current stream has no Japanese container-language evidence"
            )
        expected_codec = str(selected_audio.get("codec") or "").strip().casefold()
        if expected_codec and expected_codec != str(current_audio.codec_name or "").casefold():
            raise SourceDecisionAdapterError("ASR_JA_AUDIO stream codec changed")
        expected_channels = _optional_int(selected_audio.get("channels"))
        if expected_channels is not None and expected_channels != current_audio.channels:
            raise SourceDecisionAdapterError("ASR_JA_AUDIO stream channel count changed")
        audio = AudioStreamInfo(
            index=current_audio.index,
            language="ja",
            title=current_audio.title,
            default=current_audio.default,
            commentary=current_audio.commentary,
            codec_name=current_audio.codec_name,
            channels=current_audio.channels,
        )
        return ResolvedSourceDecision(strategy, decision_id, decision, audio=audio)

    selected_subtitle = _required_mapping(
        decision.get("selected_subtitle_track"),
        "selected_subtitle_track",
    )
    materialized = materialize_selected_subtitle(
        video,
        selected_subtitle,
        media_job_identity,
        config,
        expected_candidate_fingerprint=candidate_fingerprint,
    )
    classification = classify_subtitle_content_file(materialized)
    detected_language = str(classification.language or "").strip().casefold()
    expected_languages = {
        USE_EXISTING_ZH_TW: {"zh-tw"},
        NORMALIZE_ZH_HANT: {"zh-tw"},
        M2_CONVERT_ZH_CN: {"zh-cn"},
        TRANSLATE_JA_SUBTITLE: {"ja"},
    }[strategy]
    if detected_language not in expected_languages:
        raise SourceDecisionReviewError(
            "materialized subtitle language conflicts with persisted strategy: "
            f"strategy={strategy} detected={detected_language or 'unknown'}"
        )
    quality = analyze_subtitle_file(
        materialized,
        config,
        role="japanese" if strategy == TRANSLATE_JA_SUBTITLE else "unknown",
    )
    if quality.has_failures or quality.dialogues <= 0:
        raise SourceDecisionReviewError("materialized subtitle failed legacy structural QC")
    legacy_strategy = {
        USE_EXISTING_ZH_TW: USE_ZH_TW,
        NORMALIZE_ZH_HANT: USE_ZH_TW,
        M2_CONVERT_ZH_CN: CONVERT_ZH_CN,
        TRANSLATE_JA_SUBTITLE: TRANSLATE_JAPANESE,
    }[strategy]
    source_kind = str(selected_subtitle.get("source_kind") or "").strip().casefold()
    if source_kind not in {"sidecar", "embedded"}:
        raise SourceDecisionAdapterError("selected subtitle source_kind is invalid")
    stat = materialized.stat()
    subtitle = SubtitleSourceDecision(
        strategy=legacy_strategy,
        source_path=materialized,
        source_language=detected_language,
        source_kind=source_kind,
        stream_index=(
            -1
            if source_kind == "sidecar"
            else _required_nonnegative_int(selected_subtitle.get("index"), "subtitle index")
        ),
        classification=classification.as_dict(),
        quality=quality.to_dict(),
        source_sha256=sha256_file(materialized),
        source_size=int(stat.st_size),
        source_mtime_ns=int(stat.st_mtime_ns),
    )
    return ResolvedSourceDecision(strategy, decision_id, decision, subtitle=subtitle)


def _validate_executable_source_context(
    video: Path,
    persisted: Mapping[str, Any],
    decision: Mapping[str, Any],
    media_job_identity: Mapping[str, Any],
    config: object,
) -> str:
    expected = _required_candidate_fingerprint(persisted, decision)
    try:
        current = build_source_input_identity(
            video,
            media_job_identity,
            config=config,
        )
    except (OSError, TypeError, ValueError, SourceInventoryError) as exc:
        raise SourceDecisionAdapterError(
            "cannot verify the persisted decision against the current source"
        ) from exc
    if current.candidate_fingerprint.casefold() != expected:
        raise SourceDecisionAdapterError(
            "current source candidate fingerprint does not match persisted decision"
        )
    return expected


def _required_candidate_fingerprint(
    persisted: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> str:
    values: list[tuple[str, str]] = []
    locations: tuple[tuple[str, Mapping[str, Any]], ...] = (
        ("decision", decision),
        ("record", persisted),
    )
    for label, payload in locations:
        if "candidate_fingerprint" not in payload:
            continue
        value = str(payload.get("candidate_fingerprint") or "").strip().casefold()
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise SourceDecisionAdapterError(
                f"{label} candidate_fingerprint must be a SHA-256 hex digest"
            )
        values.append((label, value))
    if not values:
        raise SourceDecisionAdapterError(
            "persisted executable decision is missing candidate_fingerprint"
        )
    expected = values[0][1]
    if any(value != expected for _, value in values[1:]):
        raise SourceDecisionAdapterError(
            "persisted candidate_fingerprint values conflict"
        )
    return expected


def _decision_payload(persisted: Mapping[str, Any]) -> dict[str, Any]:
    nested = persisted.get("decision")
    payload = nested if isinstance(nested, Mapping) else persisted
    if not isinstance(payload, Mapping):
        raise SourceDecisionAdapterError("persisted source decision payload is missing")
    return {str(key): value for key, value in payload.items()}


def _required_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise SourceDecisionAdapterError(f"{field} must be a non-empty mapping")
    return {str(key): item for key, item in value.items()}


def _required_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise SourceDecisionAdapterError(f"{field} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceDecisionAdapterError(f"{field} must be a non-negative integer") from exc
    if result < 0:
        raise SourceDecisionAdapterError(f"{field} must be a non-negative integer")
    return result


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SourceDecisionAdapterError("audio channels must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SourceDecisionAdapterError("audio channels must be an integer") from exc
