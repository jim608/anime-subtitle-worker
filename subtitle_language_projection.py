"""Fail-closed projection of explicitly paired Chinese/Japanese ASS layers.

No QC thresholds or source-selection decisions are changed here. The caller
must run the normal target-aware import validation on the returned artifact.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from ass_utils import (
    _ass_dialogue_fields, _ass_timestamp_to_srt,
    _srt_timestamp_milliseconds, _unescape_generated_ass_text,
    ass_dialogue_style_to_srt_blocks, ass_style_from_config,
    dominant_ass_dialogue_style, format_ass,
)
from source_analyzer import AnalyzerThresholds, analyze_subtitle_candidate


class ParallelSubtitleError(ValueError):
    """A potentially bilingual source lacks safe projection evidence."""


_JAPANESE_STYLE = re.compile(r"(?<![A-Za-z])JP(?![A-Za-z])", re.IGNORECASE)
_EVENT_FORMAT = "layer,start,end,style,name,marginl,marginr,marginv,effect,text"
_MAX_PROJECTION_EVENTS = 4096


def _event_times(fields: list[str]) -> tuple[int, int]:
    start, end = (_srt_timestamp_milliseconds(_ass_timestamp_to_srt(fields[i].strip())) for i in (1, 2))
    if end <= start:
        raise ParallelSubtitleError("parallel_nonpositive_timing")
    return start, end


def _plain_lines(fields: list[str]) -> list[str]:
    return _unescape_generated_ass_text(fields[9])


def _canonical(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def normalize_parallel_chinese_ass(
    content: str, *, language: str, config: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Return a proven text-preserving candidate, never publication authority.

    Only explicit JP styles with CN counterparts and exact event timing pairs
    are removable. Names alone are insufficient: aggregate content-language
    evidence must pass the existing analyzer's auto-accept confidence. Every
    retained text line must survive in an overlapping normalized cue window.
    Unknown/other styles are retained, not silently treated as non-dialogue.
    """
    if language not in {"zh-tw", "zh-cn"}:
        return None
    events = _ass_dialogue_fields(content)
    japanese_styles = {f[3] for _, f in events if _JAPANESE_STYLE.search(f[3])}
    if not japanese_styles:
        return None
    if len(events) > _MAX_PROJECTION_EVENTS:
        raise ParallelSubtitleError("parallel_projection_event_limit")
    event_section = False
    event_format = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            event_section = stripped.casefold() == "[events]"
        elif event_section and stripped.casefold().startswith("format:"):
            event_format = re.sub(r"\s+", "", stripped.split(":", 1)[1]).casefold()
        elif event_section and stripped.startswith("Dialogue:") and event_format != _EVENT_FORMAT:
            raise ParallelSubtitleError("parallel_unsupported_event_format")
    if event_format != _EVENT_FORMAT:
        raise ParallelSubtitleError("parallel_unsupported_event_format")
    by_style: dict[str, list[list[str]]] = {}
    for _, fields in events:
        by_style.setdefault(fields[3], []).append(fields)
        _event_times(fields)
    names: dict[str, list[str]] = {}
    for style in by_style:
        names.setdefault(style.casefold(), []).append(style)
    counterparts: set[str] = set()
    pairings = []
    for style in sorted(japanese_styles):
        expected = _JAPANESE_STYLE.sub("CN", style).casefold()
        matches = names.get(expected, [])
        if len(matches) != 1:
            raise ParallelSubtitleError("parallel_chinese_counterpart_ambiguous_or_missing")
        chinese = matches[0]
        counterparts.add(chinese)
        timings = {_event_times(f) for f in by_style[chinese] if _plain_lines(f)}
        for fields in by_style[style]:
            if not _plain_lines(fields) or _event_times(fields) not in timings:
                raise ParallelSubtitleError("parallel_unpaired_japanese_event")
        pairings.append({"japanese_style": style, "chinese_style": chinese, "paired_events": len(by_style[style])})
    factory = getattr(config, "source_analyzer_thresholds", None)
    thresholds = factory() if callable(factory) else AnalyzerThresholds()
    analyses = {}
    for key, styles, accepted_languages in (
        ("chinese", counterparts, {"zh-tw", "zh-hant"} if language == "zh-tw" else {"zh-cn", "zh-hans"}),
        ("japanese", japanese_styles, {"ja"}),
    ):
        fields = [f for style in sorted(styles) for f in by_style[style]]
        if len(fields) < thresholds.min_subtitle_events:
            raise ParallelSubtitleError("parallel_insufficient_language_events")
        analysis = analyze_subtitle_candidate({
            "codec": "ass", "event_count": len(fields), "valid_timing_count": len(fields),
            "sample_text": "\n".join(text for f in fields for text in _plain_lines(f)),
            "container_language_tag": "und", "title": "",
        }, media_duration_seconds=None, thresholds=thresholds)
        if analysis.detected_language not in accepted_languages or analysis.language_confidence < thresholds.auto_accept_confidence:
            raise ParallelSubtitleError("parallel_content_language_unproven")
        analyses[key] = {
            "detected_language": analysis.detected_language,
            "confidence": analysis.language_confidence,
            "japanese_character_ratio": analysis.japanese_character_ratio,
            "traditional_markers": analysis.traditional_marker_count,
            "simplified_markers": analysis.simplified_marker_count,
        }
    removed = {line for line, fields in events if fields[3] in japanese_styles}
    projected = "\n".join(line for line in content.splitlines() if line not in removed) + "\n"
    dominant = dominant_ass_dialogue_style(projected)
    if dominant not in counterparts:
        raise ParallelSubtitleError("parallel_chinese_dominant_style_unproven")
    blocks = ass_dialogue_style_to_srt_blocks(projected, dominant)
    windows = []
    for block in blocks:
        start, end = block.timing.split(" --> ", 1)
        windows.append((_srt_timestamp_milliseconds(start), _srt_timestamp_milliseconds(end), _canonical("".join(block.text))))
    retained_lines = 0
    retained_events = [f for _, f in events if f[3] not in japanese_styles]
    for fields in retained_events:
        start, end = _event_times(fields)
        text_window = "".join(text for left, right, text in windows if max(start, left) < min(end, right))
        for line in _plain_lines(fields):
            if _canonical(line) not in text_window:
                raise ParallelSubtitleError("parallel_retained_text_not_preserved_in_time_window")
            retained_lines += 1
    normalized = format_ass(blocks, ass_style_from_config(config))
    return normalized, {
        "strategy": "parallel_chinese_ass_projection", "version": "parallel-chinese-ass-v1",
        "original_content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "original_events": len(events), "removed_paired_japanese_events": sum(p["paired_events"] for p in pairings),
        "retained_events": len(retained_events), "retained_text_lines": retained_lines,
        "normalized_events": len(blocks), "dominant_style": dominant,
        "pairings": pairings, "language_evidence": analyses,
        "target_import_validation_required": True,
    }
