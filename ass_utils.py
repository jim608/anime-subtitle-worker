from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re
from typing import Any

from srt_utils import SrtBlock, read_srt, validate_translation, write_srt


TIMING_PAIR_RE = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})"
)
ASS_TIMESTAMP_RE = re.compile(
    r"^(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})\.(?P<centiseconds>\d{1,2})$"
)
ASS_VECTOR_DRAWING_MODE_RE = re.compile(r"\\p\s*(?P<scale>\d+)", re.IGNORECASE)
ASS_SECONDARY_HARD_CPS = 25
ASS_SECONDARY_TARGET_CPS = 24
ASS_SECONDARY_MAX_VISUAL_LINES = 2
ASS_SECONDARY_MAX_NONSPACE_CHARS = 80
GENERATED_SECONDARY_STYLE_RE = re.compile(
    r"\{\\fs[^\\{}]+\\c&H[0-9A-Fa-f]+&\\alpha&H[0-9A-Fa-f]+&\\bord[^\\{}]+\\shad[^\\{}]+\}"
)
STYLE_VERSION_PREFIX = "; AIStyleVersion: "


@dataclass(frozen=True)
class AssStyle:
    play_res_x: int = 1920
    play_res_y: int = 1080
    font_name: str = "Noto Sans CJK TC"
    primary_font_size: int = 58
    secondary_font_size: int = 32
    primary_color: str = "&H00FFFFFF"
    secondary_color: str = "&HE6E6E6&"
    outline_color: str = "&H00000000"
    back_color: str = "&H80000000"
    secondary_alpha: str = "&H18&"
    primary_outline: float = 2.2
    secondary_outline: float = 1.4
    shadow: float = 0.0
    margin_l: int = 40
    margin_r: int = 40
    margin_v: int = 70


ASS_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
{style_version_line}
PlayResX: {play_res_x}
PlayResY: {play_res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{primary_font_size},{primary_color},&H000000FF,{outline_color},{back_color},0,0,0,0,100,100,0,0,1,{primary_outline},{shadow},2,{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


class AssExportError(RuntimeError):
    pass


def ass_style_from_config(config: Any) -> AssStyle:
    return AssStyle(
        play_res_x=config.ass_play_res_x,
        play_res_y=config.ass_play_res_y,
        font_name=config.ass_font_name,
        primary_font_size=config.ass_primary_font_size,
        secondary_font_size=config.ass_secondary_font_size,
        primary_color=config.ass_primary_color,
        secondary_color=config.ass_secondary_color,
        outline_color=config.ass_outline_color,
        back_color=config.ass_back_color,
        secondary_alpha=config.ass_secondary_alpha,
        primary_outline=config.ass_primary_outline,
        secondary_outline=config.ass_secondary_outline,
        shadow=config.ass_shadow,
        margin_l=config.ass_margin_l,
        margin_r=config.ass_margin_r,
        margin_v=config.ass_margin_v,
    )


def convert_srt_file_to_ass(srt_path: str | Path, ass_path: str | Path, style: AssStyle | None = None) -> Path:
    source = Path(srt_path)
    output = Path(ass_path)
    try:
        blocks = read_srt(source)
        output.write_text(format_ass(blocks, style), encoding="utf-8-sig", newline="\n")
    except Exception as exc:
        raise AssExportError(f"Failed to export ASS from {source} to {output}: {exc}") from exc
    return output


def convert_bilingual_srt_files_to_ass(
    primary_srt_path: str | Path,
    secondary_srt_path: str | Path,
    ass_path: str | Path,
    style: AssStyle | None = None,
) -> Path:
    primary_source = Path(primary_srt_path)
    secondary_source = Path(secondary_srt_path)
    output = Path(ass_path)
    try:
        primary_blocks = read_srt(primary_source)
        secondary_blocks = read_srt(secondary_source)
        validate_translation(secondary_blocks, primary_blocks)
        output.write_text(
            format_bilingual_ass(primary_blocks, secondary_blocks, style),
            encoding="utf-8-sig",
            newline="\n",
        )
    except Exception as exc:
        raise AssExportError(
            f"Failed to export bilingual ASS from {primary_source} and {secondary_source} to {output}: {exc}"
        ) from exc
    return output


def convert_ass_file_to_srt(ass_path: str | Path, srt_path: str | Path) -> Path:
    source = Path(ass_path)
    output = Path(srt_path)
    try:
        blocks = _generated_ass_to_srt_blocks(source.read_text(encoding="utf-8-sig"))
        write_srt(output, blocks)
    except Exception as exc:
        raise AssExportError(f"Failed to restore SRT from {source} to {output}: {exc}") from exc
    return output


def dominant_ass_dialogue_style(
    content: str,
    *,
    minimum_dialogues: int = 20,
    minimum_share: float = 0.5,
) -> str | None:
    """Return the unique dominant usable ASS dialogue style, if trustworthy."""

    if minimum_dialogues < 1:
        raise ValueError("minimum_dialogues must be at least 1")
    if not 0 < minimum_share <= 1:
        raise ValueError("minimum_share must be greater than 0 and at most 1")

    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    seen_events: set[tuple[str, str, str, str]] = set()
    total = 0
    for _raw_line, fields in _ass_dialogue_fields(content):
        style = fields[3].strip()
        if not style:
            continue
        start = _ass_timestamp_to_srt(fields[1].strip())
        end = _ass_timestamp_to_srt(fields[2].strip())
        text_lines = _unescape_generated_ass_text(fields[9])
        if not text_lines:
            continue
        key = style.casefold()
        normalized_text = " ".join(" ".join(text_lines).split()).casefold()
        event_key = (key, start, end, normalized_text)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        names.setdefault(key, style)
        counts[key] = counts.get(key, 0) + 1
        total += 1

    if not counts or total <= 0:
        return None
    highest = max(counts.values())
    leaders = [key for key, count in counts.items() if count == highest]
    if len(leaders) != 1:
        return None
    if highest < minimum_dialogues or highest / total < minimum_share:
        return None
    return names[leaders[0]]


def ass_dialogue_style_to_srt_blocks(content: str, style: str) -> list[SrtBlock]:
    """Convert every ASS Dialogue event under a trusted dominant-style gate.

    The requested style must be the independently recomputed dominant style.
    Its non-overlapping cues form the timeline.  Secondary dialogue that
    overlaps that timeline is merged once into the cue with the greatest
    overlap; remaining secondary dialogue is clustered into non-overlapping
    cues.  ASS ``Comment:`` events are not dialogue and remain excluded by
    :func:`_ass_dialogue_fields`.
    """

    requested_style = str(style or "").strip()
    if not requested_style:
        raise ValueError("style must not be empty")
    trusted_style = dominant_ass_dialogue_style(content)
    if (
        trusted_style is None
        or trusted_style.casefold() != requested_style.casefold()
    ):
        raise AssExportError(
            "ASS requested style does not match its trusted dominant dialogue style: "
            f"requested={requested_style!r} dominant={trusted_style!r}"
        )

    parsed_events: list[tuple[str, str, str, list[str]]] = []
    for _raw_line, fields in _ass_dialogue_fields(content):
        start = _ass_timestamp_to_srt(fields[1].strip())
        end = _ass_timestamp_to_srt(fields[2].strip())
        text_lines = _unescape_generated_ass_text(fields[9])
        if not text_lines:
            continue
        event_style = fields[3].strip()
        if not event_style:
            raise AssExportError(
                "ASS contains usable Dialogue text without a style: "
                f"start={start} end={end}"
            )
        if _srt_timestamp_milliseconds(end) <= _srt_timestamp_milliseconds(start):
            raise AssExportError(
                "ASS Dialogue event has a non-positive duration: "
                f"style={event_style!r} start={start} end={end}"
            )
        parsed_events.append((event_style, start, end, text_lines))

    if not any(
        event_style.casefold() == requested_style.casefold()
        for event_style, _start, _end, _text_lines in parsed_events
    ):
        raise AssExportError(
            f"ASS contains no usable Dialogue events for style {requested_style!r}"
        )

    dominant_events = [
        event
        for event in parsed_events
        if event[0].casefold() == requested_style.casefold()
    ]
    secondary_events = [
        event
        for event in parsed_events
        if event[0].casefold() != requested_style.casefold()
    ]

    dominant_text_by_timing: dict[tuple[str, str], list[str]] = {}
    dominant_seen_by_timing: dict[tuple[str, str], set[str]] = {}
    for _event_style, start, end, text_lines in dominant_events:
        timing_key = (start, end)
        normalized_text = " ".join(" ".join(text_lines).split()).casefold()
        seen = dominant_seen_by_timing.setdefault(timing_key, set())
        if normalized_text in seen:
            continue
        dominant_text_by_timing.setdefault(timing_key, []).extend(text_lines)
        seen.add(normalized_text)

    dominant_timings = sorted(
        dominant_text_by_timing,
        key=lambda timing: (
            _srt_timestamp_milliseconds(timing[0]),
            _srt_timestamp_milliseconds(timing[1]),
        ),
    )
    for previous, current in zip(dominant_timings, dominant_timings[1:]):
        if _srt_timestamp_milliseconds(current[0]) < _srt_timestamp_milliseconds(previous[1]):
            raise AssExportError(
                "ASS dominant Dialogue cues overlap and cannot form a safe timeline: "
                f"previous={previous!r} current={current!r}"
            )
    dominant_original_character_counts = {
        timing: len(re.sub(r"\s+", "", "".join(dominant_text_by_timing[timing])))
        for timing in dominant_timings
    }

    def overlap_milliseconds(
        first: tuple[str, str],
        second: tuple[str, str],
    ) -> int:
        return max(
            0,
            min(
                _srt_timestamp_milliseconds(first[1]),
                _srt_timestamp_milliseconds(second[1]),
            )
            - max(
                _srt_timestamp_milliseconds(first[0]),
                _srt_timestamp_milliseconds(second[0]),
            ),
        )

    orphan_secondary: list[tuple[str, str, str, list[str]]] = []
    attached_dominant_timings: set[tuple[str, str]] = set()
    ordered_secondary = sorted(
        secondary_events,
        key=lambda event: (
            _srt_timestamp_milliseconds(event[1]),
            _srt_timestamp_milliseconds(event[2]),
        ),
    )
    for event_style, start, end, text_lines in ordered_secondary:
        timing_key = (start, end)
        overlaps = [
            (overlap_milliseconds(timing_key, dominant_timing), position)
            for position, dominant_timing in enumerate(dominant_timings)
        ]
        greatest_overlap, target_position = max(
            overlaps,
            key=lambda item: (item[0], -item[1]),
        )
        if greatest_overlap <= 0:
            orphan_secondary.append((event_style, start, end, text_lines))
            continue
        target_timing = dominant_timings[target_position]
        normalized_text = " ".join(" ".join(text_lines).split()).casefold()
        seen = dominant_seen_by_timing.setdefault(target_timing, set())
        if normalized_text not in seen:
            dominant_text_by_timing[target_timing].extend(text_lines)
            seen.add(normalized_text)
            attached_dominant_timings.add(target_timing)

    secondary_clusters: list[dict[str, object]] = []
    for _event_style, start, end, text_lines in orphan_secondary:
        start_ms = _srt_timestamp_milliseconds(start)
        end_ms = _srt_timestamp_milliseconds(end)
        if not secondary_clusters or start_ms >= int(secondary_clusters[-1]["end_ms"]):
            secondary_clusters.append(
                {
                    "start": start,
                    "end": end,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": [],
                    "seen": set(),
                }
            )
        cluster = secondary_clusters[-1]
        if end_ms > int(cluster["end_ms"]):
            cluster["end"] = end
            cluster["end_ms"] = end_ms
        normalized_text = " ".join(" ".join(text_lines).split()).casefold()
        duplicate_key = (start, end, normalized_text)
        cluster_seen = cluster["seen"]
        cluster_text = cluster["text"]
        if not isinstance(cluster_seen, set) or not isinstance(cluster_text, list):
            raise AssExportError("Invalid internal ASS secondary cluster state")
        if duplicate_key not in cluster_seen:
            cluster_text.extend(text_lines)
            cluster_seen.add(duplicate_key)

    dominant_start_ms = [_srt_timestamp_milliseconds(start) for start, _end in dominant_timings]
    dominant_end_ms = [_srt_timestamp_milliseconds(end) for _start, end in dominant_timings]
    for position, cluster in enumerate(secondary_clusters):
        start_ms = int(cluster["start_ms"])
        end_ms = int(cluster["end_ms"])
        cluster_text = cluster["text"]
        if not isinstance(cluster_text, list):
            raise AssExportError("Invalid internal ASS secondary cluster text")
        chunks = _chunk_secondary_text(cluster_text)
        if not chunks:
            raise AssExportError("ASS secondary Dialogue cluster has no usable text chunks")
        cluster["chunks"] = chunks
        character_count = sum(_nonspace_character_count(chunk) for chunk in chunks)
        duration_ms = end_ms - start_ms
        required_ms = sum(
            (
                _nonspace_character_count(chunk) * 1000
                + ASS_SECONDARY_TARGET_CPS
                - 1
            )
            // ASS_SECONDARY_TARGET_CPS
            for chunk in chunks
        )
        if required_ms <= duration_ms:
            continue

        extra_ms = required_ms - duration_ms
        previous_boundaries = [value for value in dominant_end_ms if value <= start_ms]
        if position > 0:
            previous_boundaries.append(int(secondary_clusters[position - 1]["end_ms"]))
        left_boundary_ms = max(previous_boundaries, default=0)

        next_boundaries = [value for value in dominant_start_ms if value >= end_ms]
        if position + 1 < len(secondary_clusters):
            next_boundaries.append(int(secondary_clusters[position + 1]["start_ms"]))
        right_boundary_ms = min(next_boundaries, default=end_ms)

        available_left_ms = start_ms - left_boundary_ms
        available_right_ms = right_boundary_ms - end_ms
        if extra_ms > available_left_ms + available_right_ms:
            raise AssExportError(
                "ASS secondary Dialogue cluster cannot reach safe CPS within its gap: "
                f"characters={character_count} duration_ms={duration_ms} "
                f"available_ms={available_left_ms + available_right_ms}"
            )
        minimum_left_ms = max(0, extra_ms - available_right_ms)
        maximum_left_ms = min(extra_ms, available_left_ms)
        left_ms = min(max(extra_ms // 2, minimum_left_ms), maximum_left_ms)
        right_ms = extra_ms - left_ms
        start_ms -= left_ms
        end_ms += right_ms
        cluster["start_ms"] = start_ms
        cluster["end_ms"] = end_ms
        cluster["start"] = _milliseconds_to_srt_timestamp(start_ms)
        cluster["end"] = _milliseconds_to_srt_timestamp(end_ms)

    dominant_output_timings = {timing: timing for timing in dominant_timings}
    for timing in dominant_timings:
        if timing not in attached_dominant_timings:
            continue
        start_ms = _srt_timestamp_milliseconds(timing[0])
        end_ms = _srt_timestamp_milliseconds(timing[1])
        duration_ms = end_ms - start_ms
        original_characters = dominant_original_character_counts[timing]
        final_characters = len(
            re.sub(r"\s+", "", "".join(dominant_text_by_timing[timing]))
        )
        if original_characters * 1000 > ASS_SECONDARY_HARD_CPS * duration_ms:
            continue
        if final_characters * 1000 <= ASS_SECONDARY_HARD_CPS * duration_ms:
            continue

        required_ms = (
            final_characters * 1000 + ASS_SECONDARY_TARGET_CPS - 1
        ) // ASS_SECONDARY_TARGET_CPS
        extra_ms = required_ms - duration_ms
        other_timings = [
            output_timing
            for source_timing, output_timing in dominant_output_timings.items()
            if source_timing != timing
        ]
        previous_boundaries = [
            _srt_timestamp_milliseconds(other_end)
            for _other_start, other_end in other_timings
            if _srt_timestamp_milliseconds(other_end) <= start_ms
        ]
        previous_boundaries.extend(
            int(cluster["end_ms"])
            for cluster in secondary_clusters
            if int(cluster["end_ms"]) <= start_ms
        )
        left_boundary_ms = max(previous_boundaries, default=0)

        next_boundaries = [
            _srt_timestamp_milliseconds(other_start)
            for other_start, _other_end in other_timings
            if _srt_timestamp_milliseconds(other_start) >= end_ms
        ]
        next_boundaries.extend(
            int(cluster["start_ms"])
            for cluster in secondary_clusters
            if int(cluster["start_ms"]) >= end_ms
        )
        right_boundary_ms = min(next_boundaries, default=end_ms)

        available_left_ms = start_ms - left_boundary_ms
        available_right_ms = right_boundary_ms - end_ms
        if extra_ms > available_left_ms + available_right_ms:
            raise AssExportError(
                "ASS dominant Dialogue with attached secondary text cannot reach safe CPS: "
                f"characters={final_characters} duration_ms={duration_ms} "
                f"available_ms={available_left_ms + available_right_ms}"
            )
        minimum_left_ms = max(0, extra_ms - available_right_ms)
        maximum_left_ms = min(extra_ms, available_left_ms)
        left_ms = min(max(extra_ms // 2, minimum_left_ms), maximum_left_ms)
        right_ms = extra_ms - left_ms
        dominant_output_timings[timing] = (
            _milliseconds_to_srt_timestamp(start_ms - left_ms),
            _milliseconds_to_srt_timestamp(end_ms + right_ms),
        )

    cues: list[tuple[str, str, list[str]]] = [
        (
            dominant_output_timings[timing][0],
            dominant_output_timings[timing][1],
            list(dominant_text_by_timing[timing]),
        )
        for timing in dominant_timings
    ]
    for cluster in secondary_clusters:
        start_ms = int(cluster["start_ms"])
        end_ms = int(cluster["end_ms"])
        chunks = cluster.get("chunks")
        if not isinstance(chunks, list):
            raise AssExportError("Invalid internal ASS secondary cluster output")
        durations = _allocate_secondary_chunk_durations(chunks, end_ms - start_ms)
        cursor_ms = start_ms
        for chunk, duration_ms in zip(chunks, durations, strict=True):
            if not isinstance(chunk, list):
                raise AssExportError("Invalid internal ASS secondary chunk output")
            next_ms = cursor_ms + duration_ms
            cues.append(
                (
                    _milliseconds_to_srt_timestamp(cursor_ms),
                    _milliseconds_to_srt_timestamp(next_ms),
                    list(chunk),
                )
            )
            cursor_ms = next_ms
        if cursor_ms != end_ms:
            raise AssExportError("ASS secondary chunk allocation did not fill its safe window")
    cues.sort(
        key=lambda cue: (
            _srt_timestamp_milliseconds(cue[0]),
            _srt_timestamp_milliseconds(cue[1]),
        )
    )
    for previous, current in zip(cues, cues[1:]):
        if _srt_timestamp_milliseconds(current[0]) < _srt_timestamp_milliseconds(previous[1]):
            raise AssExportError(
                "ASS normalization produced overlapping cues: "
                f"previous={(previous[0], previous[1])!r} "
                f"current={(current[0], current[1])!r}"
            )

    return [
        SrtBlock(
            index=index,
            timing=f"{start} --> {end}",
            text=text_lines,
        )
        for index, (start, end, text_lines) in enumerate(cues, start=1)
    ]


def restyle_ass_file(ass_path: str | Path, style: AssStyle | None = None) -> bool:
    path = Path(ass_path)
    resolved_style = style or AssStyle()
    content = path.read_text(encoding="utf-8-sig")
    lines: list[str] = []
    changed = False
    original_marker_seen = any(line.startswith(STYLE_VERSION_PREFIX) for line in content.splitlines())
    style_version_line = _style_version_line(resolved_style)

    for line in content.splitlines():
        append_marker_after_line = False
        if line.startswith(STYLE_VERSION_PREFIX):
            replacement = style_version_line
        elif line.startswith("ScriptType:"):
            replacement = line
            append_marker_after_line = not original_marker_seen
        elif line.startswith("PlayResX:"):
            replacement = f"PlayResX: {resolved_style.play_res_x}"
        elif line.startswith("PlayResY:"):
            replacement = f"PlayResY: {resolved_style.play_res_y}"
        elif line.startswith("Style: Default,"):
            replacement = _format_default_style_line(resolved_style)
        else:
            replacement = line

        if replacement != line:
            changed = True
        lines.append(replacement)
        if append_marker_after_line:
            lines.append(style_version_line)
            changed = True

    if not original_marker_seen and style_version_line not in lines:
        insert_at = 0
        for index, line in enumerate(lines):
            if line.strip() == "[Script Info]":
                insert_at = index + 1
                break
        lines.insert(insert_at, style_version_line)
        changed = True

    restyled = "\n".join(lines).rstrip() + "\n"
    restyled = GENERATED_SECONDARY_STYLE_RE.sub(lambda _match: _secondary_style_tag(resolved_style), restyled)
    if restyled == content.replace("\r\n", "\n").rstrip() + "\n" and not changed:
        return False

    path.write_text(restyled, encoding="utf-8-sig", newline="\n")
    return True


def ass_style_is_current(ass_path: str | Path, style: AssStyle | None = None) -> bool:
    path = Path(ass_path)
    if not path.exists():
        return False
    resolved_style = style or AssStyle()
    expected = _style_version_line(resolved_style)
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith(STYLE_VERSION_PREFIX):
                return line == expected
    except OSError:
        return False
    return False


def ass_style_signature(style: AssStyle | None = None) -> str:
    resolved_style = style or AssStyle()
    payload = {
        "play_res_x": resolved_style.play_res_x,
        "play_res_y": resolved_style.play_res_y,
        "font_name": resolved_style.font_name,
        "primary_font_size": resolved_style.primary_font_size,
        "secondary_font_size": resolved_style.secondary_font_size,
        "primary_color": resolved_style.primary_color,
        "secondary_color": resolved_style.secondary_color,
        "outline_color": resolved_style.outline_color,
        "back_color": resolved_style.back_color,
        "secondary_alpha": resolved_style.secondary_alpha,
        "primary_outline": resolved_style.primary_outline,
        "secondary_outline": resolved_style.secondary_outline,
        "shadow": resolved_style.shadow,
        "margin_l": resolved_style.margin_l,
        "margin_r": resolved_style.margin_r,
        "margin_v": resolved_style.margin_v,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def format_ass(blocks: list[SrtBlock], style: AssStyle | None = None) -> str:
    resolved_style = style or AssStyle()
    lines = [_format_ass_header(resolved_style).rstrip()]
    for block in blocks:
        start, end = _parse_timing(block.timing)
        text = _format_ass_text(block.text)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return "\n".join(lines).rstrip() + "\n"


def format_bilingual_ass(
    primary_blocks: list[SrtBlock],
    secondary_blocks: list[SrtBlock],
    style: AssStyle | None = None,
) -> str:
    validate_translation(secondary_blocks, primary_blocks)
    resolved_style = style or AssStyle()

    lines = [_format_ass_header(resolved_style).rstrip()]
    for primary, secondary in zip(primary_blocks, secondary_blocks, strict=True):
        start, end = _parse_timing(primary.timing)
        primary_text = _format_ass_single_line(primary.text)
        secondary_text = _format_ass_single_line(secondary.text)
        text = rf"{primary_text}\N{_secondary_style_tag(resolved_style)}{secondary_text}"
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return "\n".join(lines).rstrip() + "\n"


def _format_ass_header(style: AssStyle) -> str:
    return ASS_HEADER_TEMPLATE.format(
        style_version_line=_style_version_line(style),
        play_res_x=style.play_res_x,
        play_res_y=style.play_res_y,
        font_name=style.font_name,
        primary_font_size=style.primary_font_size,
        primary_color=style.primary_color,
        outline_color=style.outline_color,
        back_color=style.back_color,
        primary_outline=_format_ass_number(style.primary_outline),
        shadow=_format_ass_number(style.shadow),
        margin_l=style.margin_l,
        margin_r=style.margin_r,
        margin_v=style.margin_v,
    )


def _style_version_line(style: AssStyle) -> str:
    return f"{STYLE_VERSION_PREFIX}{ass_style_signature(style)}"


def _format_default_style_line(style: AssStyle) -> str:
    return (
        f"Style: Default,{style.font_name},{style.primary_font_size},{style.primary_color},&H000000FF,"
        f"{style.outline_color},{style.back_color},0,0,0,0,100,100,0,0,1,"
        f"{_format_ass_number(style.primary_outline)},{_format_ass_number(style.shadow)},2,"
        f"{style.margin_l},{style.margin_r},{style.margin_v},1"
    )


def _secondary_style_tag(style: AssStyle) -> str:
    return (
        r"{"
        rf"\fs{style.secondary_font_size}"
        rf"\c{style.secondary_color}"
        rf"\alpha{style.secondary_alpha}"
        rf"\bord{_format_ass_number(style.secondary_outline)}"
        rf"\shad{_format_ass_number(style.shadow)}"
        r"}"
    )


def _parse_timing(timing: str) -> tuple[str, str]:
    match = TIMING_PAIR_RE.match(timing.strip())
    if not match:
        raise AssExportError(f"Invalid SRT timing for ASS export: {timing!r}")
    return _srt_timestamp_to_ass(match.group("start")), _srt_timestamp_to_ass(match.group("end"))


def _srt_timestamp_to_ass(timestamp: str) -> str:
    hours, minutes, rest = timestamp.split(":")
    seconds, milliseconds = rest.split(",")
    centiseconds = int(milliseconds) // 10
    return f"{int(hours)}:{minutes}:{seconds}.{centiseconds:02d}"


def _generated_ass_to_srt_blocks(content: str) -> list[SrtBlock]:
    blocks: list[SrtBlock] = []
    for _raw_line, fields in _ass_dialogue_fields(content):
        start = _ass_timestamp_to_srt(fields[1].strip())
        end = _ass_timestamp_to_srt(fields[2].strip())
        text_lines = _unescape_generated_ass_text(fields[9])
        if not text_lines:
            continue
        blocks.append(
            SrtBlock(
                index=len(blocks) + 1,
                timing=f"{start} --> {end}",
                text=text_lines,
            )
        )
    if not blocks:
        raise AssExportError("ASS contains no usable Dialogue events")
    return blocks


def _ass_dialogue_fields(content: str) -> list[tuple[str, list[str]]]:
    parsed: list[tuple[str, list[str]]] = []
    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw_line.startswith("Dialogue:"):
            continue
        fields = raw_line.split(":", 1)[1].lstrip().split(",", 9)
        if len(fields) < 10:
            raise AssExportError(f"Invalid ASS Dialogue line: {raw_line!r}")
        parsed.append((raw_line, fields))
    return parsed


def _ass_timestamp_to_srt(timestamp: str) -> str:
    match = ASS_TIMESTAMP_RE.match(timestamp)
    if match is None:
        raise AssExportError(f"Invalid ASS timestamp: {timestamp!r}")
    centiseconds = int(match.group("centiseconds").ljust(2, "0"))
    return (
        f"{int(match.group('hours')):02d}:"
        f"{match.group('minutes')}:"
        f"{match.group('seconds')},{centiseconds * 10:03d}"
    )


def _srt_timestamp_milliseconds(timestamp: str) -> int:
    hours, minutes, remainder = timestamp.split(":", 2)
    seconds, milliseconds = remainder.split(",", 1)
    return (
        ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
        + int(milliseconds)
    )


def _milliseconds_to_srt_timestamp(milliseconds: int) -> str:
    if milliseconds < 0:
        raise AssExportError("SRT timestamp cannot be negative")
    total_seconds, millis = divmod(milliseconds, 1000)
    total_minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _nonspace_character_count(lines: list[str]) -> int:
    return len(re.sub(r"\s+", "", "".join(lines)))


def _split_secondary_text_line(line: str) -> list[str]:
    pieces: list[str] = []
    buffer: list[str] = []
    nonspace_count = 0
    for character in str(line or "").strip():
        if not character.isspace() and nonspace_count >= ASS_SECONDARY_MAX_NONSPACE_CHARS:
            piece = "".join(buffer).strip()
            if piece:
                pieces.append(piece)
            buffer = []
            nonspace_count = 0
        buffer.append(character)
        if not character.isspace():
            nonspace_count += 1
    piece = "".join(buffer).strip()
    if piece:
        pieces.append(piece)
    return pieces


def _chunk_secondary_text(lines: list[str]) -> list[list[str]]:
    ordered_lines: list[str] = []
    for line in lines:
        normalized = " ".join(str(line or "").split()).casefold()
        if not normalized:
            continue
        ordered_lines.extend(_split_secondary_text_line(line))

    chunks: list[list[str]] = []
    current: list[str] = []
    current_characters = 0
    for line in ordered_lines:
        line_characters = _nonspace_character_count([line])
        if current and (
            len(current) >= ASS_SECONDARY_MAX_VISUAL_LINES
            or current_characters + line_characters > ASS_SECONDARY_MAX_NONSPACE_CHARS
        ):
            chunks.append(current)
            current = []
            current_characters = 0
        current.append(line)
        current_characters += line_characters
    if current:
        chunks.append(current)
    return chunks


def _allocate_secondary_chunk_durations(
    chunks: list[list[str]],
    total_duration_ms: int,
) -> list[int]:
    character_counts = [_nonspace_character_count(chunk) for chunk in chunks]
    minimums = [
        (count * 1000 + ASS_SECONDARY_TARGET_CPS - 1)
        // ASS_SECONDARY_TARGET_CPS
        for count in character_counts
    ]
    required_ms = sum(minimums)
    if not chunks or required_ms > total_duration_ms:
        raise AssExportError(
            "ASS secondary Dialogue chunks do not fit their safe CPS window: "
            f"required_ms={required_ms} available_ms={total_duration_ms}"
        )
    allocations = list(minimums)
    extra_ms = total_duration_ms - required_ms
    total_characters = sum(character_counts)
    if extra_ms <= 0 or total_characters <= 0:
        return allocations
    remainders: list[tuple[int, int]] = []
    distributed = 0
    for index, character_count in enumerate(character_counts):
        numerator = extra_ms * character_count
        quotient, remainder = divmod(numerator, total_characters)
        allocations[index] += quotient
        distributed += quotient
        remainders.append((remainder, index))
    for _remainder, index in sorted(remainders, key=lambda item: (-item[0], item[1]))[
        : extra_ms - distributed
    ]:
        allocations[index] += 1
    return allocations


def _unescape_generated_ass_text(text: str) -> list[str]:
    text_segments: list[str] = []
    drawing_mode = False
    cursor = 0
    for override in re.finditer(r"\{([^{}]*)\}", text):
        if not drawing_mode:
            text_segments.append(text[cursor : override.start()])
        for match in ASS_VECTOR_DRAWING_MODE_RE.finditer(override.group(1)):
            drawing_mode = int(match.group("scale")) > 0
        cursor = override.end()
    if not drawing_mode:
        text_segments.append(text[cursor:])
    without_tags = "".join(text_segments)
    normalized = without_tags.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    return [line.replace(r"\\", "\\").strip() for line in normalized.split("\n") if line.strip()]


def _format_ass_number(value: float) -> str:
    return f"{value:g}"


def _format_ass_text(lines: list[str]) -> str:
    safe_lines = []
    for line in lines:
        safe_lines.append(_escape_ass_text(line))
    return r"\N".join(safe_lines)


def _format_ass_single_line(lines: list[str]) -> str:
    joined = " ".join(line.strip() for line in lines if line.strip())
    return _escape_ass_text(joined)


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "｛").replace("}", "｝").rstrip()
