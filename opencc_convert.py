from __future__ import annotations

from pathlib import Path
import re

from safe_files import atomic_write_text


class OpenCCError(RuntimeError):
    pass


_ASS_OVERRIDE_RE = re.compile(r"\{[^{}]*\}")


def convert_srt_to_zh_tw(zh_cn_srt_path: str | Path, zh_tw_srt_path: str | Path, config_name: str) -> Path:
    try:
        from opencc import OpenCC
    except ImportError as exc:
        raise OpenCCError(
            "OpenCC Python package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    source = Path(zh_cn_srt_path)
    output = Path(zh_tw_srt_path)

    try:
        converter = _create_converter(OpenCC, config_name)
        content = source.read_text(encoding="utf-8-sig")
        converted = converter.convert(content)
        converted = converted.replace("\r\n", "\n").replace("\r", "\n")
        atomic_write_text(output, converted, encoding="utf-8-sig")
        return output
    except Exception as exc:
        raise OpenCCError(f"OpenCC conversion failed for {source}: {exc}") from exc


def convert_ass_to_zh_tw(
    zh_cn_ass_path: str | Path,
    zh_tw_ass_path: str | Path,
    config_name: str,
) -> Path:
    """Convert only ASS Dialogue payloads while preserving the event timeline.

    Headers, styles, event fields, timestamps, and inline override tags are
    byte-for-byte stable apart from newline normalization.  This avoids sending
    style names or ASS commands through OpenCC and makes a zh-CN source a safe,
    deterministic no-ASR route.
    """

    try:
        from opencc import OpenCC
    except ImportError as exc:
        raise OpenCCError(
            "OpenCC Python package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    source = Path(zh_cn_ass_path)
    output = Path(zh_tw_ass_path)
    try:
        converter = _create_converter(OpenCC, config_name)
        content = source.read_text(encoding="utf-8-sig")
        converted_lines: list[str] = []
        dialogue_count = 0
        for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not raw_line.startswith("Dialogue:"):
                converted_lines.append(raw_line)
                continue
            fields = raw_line.split(":", 1)[1].lstrip().split(",", 9)
            if len(fields) != 10:
                raise OpenCCError(f"Invalid ASS Dialogue event: {raw_line!r}")
            fields[9] = _convert_ass_dialogue_text(fields[9], converter)
            converted_lines.append(f"Dialogue: {','.join(fields)}")
            dialogue_count += 1
        if dialogue_count <= 0:
            raise OpenCCError("ASS source contains no Dialogue events")
        atomic_write_text(output, "\n".join(converted_lines), encoding="utf-8-sig")
        return output
    except OpenCCError:
        raise
    except Exception as exc:
        raise OpenCCError(f"OpenCC ASS conversion failed for {source}: {exc}") from exc


def _convert_ass_dialogue_text(text: str, converter: object) -> str:
    overrides: list[str] = []

    def protect(match: re.Match[str]) -> str:
        overrides.append(match.group(0))
        return f"OPENCCASSTAG{len(overrides) - 1}PLACEHOLDER"

    protected = _ASS_OVERRIDE_RE.sub(protect, text)
    converted = converter.convert(protected)
    for index, override in enumerate(overrides):
        converted = converted.replace(f"OPENCCASSTAG{index}PLACEHOLDER", override)
    return converted


def _create_converter(opencc_class, config_name: str):
    candidates = [config_name]
    if config_name.endswith(".json"):
        candidates.append(config_name[:-5])

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return opencc_class(candidate)
        except Exception as exc:
            last_error = exc

    raise OpenCCError(f"Unable to load OpenCC config {config_name!r}: {last_error}")
