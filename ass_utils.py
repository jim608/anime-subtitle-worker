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
    timings: dict[str, list[tuple[str, str]]] = {}
    total = 0
    for _raw_line, fields in _ass_dialogue_fields(content):
        style = fields[3].strip()
        if not style:
            continue
        start = _ass_timestamp_to_srt(fields[1].strip())
        end = _ass_timestamp_to_srt(fields[2].strip())
        if not _unescape_generated_ass_text(fields[9]):
            continue
        key = style.casefold()
        names.setdefault(key, style)
        counts[key] = counts.get(key, 0) + 1
        timings.setdefault(key, []).append((start, end))
        total += 1

    if not counts or total <= 0:
        return None
    highest = max(counts.values())
    leaders = [key for key, count in counts.items() if count == highest]
    if len(leaders) != 1:
        return None
    if highest < minimum_dialogues or highest / total < minimum_share:
        return None
    leader = leaders[0]
    leader_timings = set(timings.get(leader, ()))
    if any(
        timing not in leader_timings
        for key, style_timings in timings.items()
        if key != leader
        for timing in style_timings
    ):
        # A secondary style has unique timeline coverage.  It may contain
        # signs or real dialogue rather than a duplicated karaoke/translation
        # layer, so selecting one style would silently omit content.
        return None
    return names[leader]


def ass_dialogue_style_to_srt_blocks(content: str, style: str) -> list[SrtBlock]:
    """Convert one ASS style while retaining shadowed secondary text.

    A trusted dominant style may have alternate events at the exact same
    timestamps (for example signs, a second speaker, or karaoke text).  Those
    events must not disappear merely because their style is not dominant, so
    distinct text is appended to the first selected cue at that timestamp.
    Callers must first use :func:`dominant_ass_dialogue_style`; it rejects any
    secondary event with timeline coverage that cannot be merged safely.
    """

    requested_style = str(style or "").strip()
    if not requested_style:
        raise ValueError("style must not be empty")

    parsed_events: list[tuple[str, str, str, list[str]]] = []
    for _raw_line, fields in _ass_dialogue_fields(content):
        start = _ass_timestamp_to_srt(fields[1].strip())
        end = _ass_timestamp_to_srt(fields[2].strip())
        text_lines = _unescape_generated_ass_text(fields[9])
        if not text_lines:
            continue
        parsed_events.append((fields[3].strip(), start, end, text_lines))

    blocks: list[SrtBlock] = []
    first_block_by_timing: dict[tuple[str, str], int] = {}
    seen_text_by_timing: dict[tuple[str, str], set[str]] = {}
    for event_style, start, end, text_lines in parsed_events:
        if event_style.casefold() != requested_style.casefold():
            continue
        timing_key = (start, end)
        blocks.append(
            SrtBlock(
                index=len(blocks) + 1,
                timing=f"{start} --> {end}",
                text=list(text_lines),
            )
        )
        first_block_by_timing.setdefault(timing_key, len(blocks) - 1)
        seen_text_by_timing.setdefault(timing_key, set()).add(
            " ".join(" ".join(text_lines).split()).casefold()
        )
    if not blocks:
        raise AssExportError(
            f"ASS contains no usable Dialogue events for style {requested_style!r}"
        )

    for event_style, start, end, text_lines in parsed_events:
        if event_style.casefold() == requested_style.casefold():
            continue
        timing_key = (start, end)
        destination_index = first_block_by_timing.get(timing_key)
        if destination_index is None:
            # The dominant-style gate rejects this case.  Keep the conversion
            # helper fail closed as well so future callers cannot omit unique
            # secondary timeline coverage accidentally.
            raise AssExportError(
                "ASS secondary style contains unique timeline coverage: "
                f"style={event_style!r} start={start} end={end}"
            )
        normalized_text = " ".join(" ".join(text_lines).split()).casefold()
        seen = seen_text_by_timing.setdefault(timing_key, set())
        if normalized_text in seen:
            continue
        blocks[destination_index].text.extend(text_lines)
        seen.add(normalized_text)
    return blocks


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


def _unescape_generated_ass_text(text: str) -> list[str]:
    without_tags = re.sub(r"\{[^{}]*\}", "", text)
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
