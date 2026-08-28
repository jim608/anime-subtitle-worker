from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from safe_files import sha256_file
from subtitle_extract import (
    ExtractedSubtitle,
    classify_subtitle_content_file,
    normalize_sidecar_subtitles,
)
from subtitle_quality import analyze_subtitle_file


SOURCE_DECISION_CONTRACT = "subtitle-source-priority-v1"
USE_ZH_TW = "adopted_zh_tw"
CONVERT_ZH_CN = "converted_zh_cn"
TRANSLATE_JAPANESE = "translated_japanese_subtitle"
ASR_JAPANESE_AUDIO = "japanese_audio_asr"

_LANGUAGE_PRIORITY = {
    "zh-tw": 0,
    "zh-cn": 1,
    "ja": 2,
}
_STRATEGY_BY_LANGUAGE = {
    "zh-tw": USE_ZH_TW,
    "zh-cn": CONVERT_ZH_CN,
    "ja": TRANSLATE_JAPANESE,
}


@dataclass(frozen=True)
class SubtitleSourceDecision:
    strategy: str
    source_path: Path
    source_language: str
    source_kind: str
    stream_index: int
    classification: dict[str, Any]
    quality: dict[str, Any]
    source_sha256: str
    source_size: int
    source_mtime_ns: int

    def provenance(self, *, output_quality: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract": SOURCE_DECISION_CONTRACT,
            "strategy": self.strategy,
            "source_kind": self.source_kind,
            "source_path": str(self.source_path.resolve()),
            "source_language": self.source_language,
            "stream_index": int(self.stream_index),
            "source_sha256": self.source_sha256,
            "source_size": int(self.source_size),
            "source_mtime_ns": int(self.source_mtime_ns),
            "classification": dict(self.classification),
            "source_quality": dict(self.quality),
            "asr_used": False,
        }
        if output_quality is not None:
            payload["output_quality"] = dict(output_quality)
        return payload


def select_subtitle_source(
    candidates: Iterable[ExtractedSubtitle],
    config: Any,
    *,
    source_kind: str,
) -> SubtitleSourceDecision | None:
    """Select one structurally usable subtitle using the fixed language priority.

    Filename and container metadata are never sufficient.  Every candidate is
    reclassified from its current content and passed through the structural QC
    gate before it can suppress a lower-priority route.
    """

    usable: list[SubtitleSourceDecision] = []
    for candidate in candidates:
        decision = _validated_candidate(candidate, config, source_kind=source_kind)
        if decision is not None:
            usable.append(decision)
    if not usable:
        return None
    return min(
        usable,
        key=lambda item: (
            _LANGUAGE_PRIORITY[item.source_language],
            -int(item.quality.get("score") or 0),
            -int(item.classification.get("quality_score") or 0),
            str(item.source_path).casefold(),
            int(item.stream_index),
        ),
    )


def discover_normalized_subtitle_source(
    video: str | Path,
    config: Any,
) -> SubtitleSourceDecision | None:
    """Discover already-extracted/sidecar text subtitles without probing audio.

    The scanner extracts supported embedded subtitle streams before queueing a
    video.  Re-normalizing here makes direct sidecars and those scanner outputs
    converge on the same canonical files while keeping every subtitle route in
    front of audio extraction and Whisper language detection.
    """

    try:
        normalized = normalize_sidecar_subtitles(video, config)
    except Exception:
        return None
    return select_subtitle_source(normalized, config, source_kind="sidecar_or_extracted")


def _validated_candidate(
    candidate: ExtractedSubtitle,
    config: Any,
    *,
    source_kind: str,
) -> SubtitleSourceDecision | None:
    path = Path(candidate.path)
    if not path.is_file():
        return None
    try:
        stat = path.stat()
        if stat.st_size <= 0:
            return None
        classification = classify_subtitle_content_file(path)
        language = str(classification.language or "").strip().casefold()
        declared = str(getattr(candidate, "language", "") or "").strip().casefold()
        if language not in _LANGUAGE_PRIORITY or declared != language:
            return None
        role = "japanese" if language == "ja" else "unknown"
        quality = analyze_subtitle_file(path, config, role=role)
        if quality.has_failures or quality.dialogues <= 0:
            return None
        return SubtitleSourceDecision(
            strategy=_STRATEGY_BY_LANGUAGE[language],
            source_path=path,
            source_language=language,
            source_kind=source_kind,
            stream_index=int(getattr(candidate, "stream_index", -1)),
            classification=classification.as_dict(),
            quality=quality.to_dict(),
            source_sha256=sha256_file(path),
            source_size=int(stat.st_size),
            source_mtime_ns=int(stat.st_mtime_ns),
        )
    except Exception:
        return None
