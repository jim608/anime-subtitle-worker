from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from safe_files import atomic_write_text


TIMING_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}"
    r"(?:\s+[^\r\n]+)?$"
)


@dataclass(frozen=True)
class SrtBlock:
    index: int
    timing: str
    text: list[str]


class SrtFormatError(ValueError):
    pass


def read_srt(path: str | Path) -> list[SrtBlock]:
    text = Path(path).read_text(encoding="utf-8-sig")
    return parse_srt(text)


def write_srt(path: str | Path, blocks: list[SrtBlock]) -> None:
    output = Path(path)
    atomic_write_text(output, format_srt(blocks), encoding="utf-8-sig")


def parse_srt(content: str) -> list[SrtBlock]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip("\ufeff \n")
    if not normalized:
        return []

    raw_blocks = re.split(r"\n{2,}", normalized)
    blocks: list[SrtBlock] = []
    for raw_block in raw_blocks:
        lines = raw_block.split("\n")
        if len(lines) < 3:
            raise SrtFormatError(f"Invalid SRT block, expected at least 3 lines: {raw_block!r}")

        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise SrtFormatError(f"Invalid SRT index: {lines[0]!r}") from exc

        timing = lines[1].strip()
        if not TIMING_RE.match(timing):
            raise SrtFormatError(f"Invalid SRT timing at index {index}: {timing!r}")

        text_lines = [line.rstrip() for line in lines[2:]]
        if not any(line.strip() for line in text_lines):
            raise SrtFormatError(f"Missing subtitle text at index {index}.")

        blocks.append(SrtBlock(index=index, timing=timing, text=text_lines))

    validate_srt_structure(blocks)
    return blocks


def format_srt(blocks: list[SrtBlock]) -> str:
    validate_srt_structure(blocks)
    parts: list[str] = []
    for block in blocks:
        parts.append(str(block.index))
        parts.append(block.timing)
        parts.extend(block.text)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def validate_srt_structure(blocks: list[SrtBlock]) -> None:
    if not blocks:
        raise SrtFormatError("SRT contains no subtitle blocks.")

    seen: set[int] = set()
    for block in blocks:
        if block.index in seen:
            raise SrtFormatError(f"Duplicate SRT index: {block.index}")
        seen.add(block.index)
        if not TIMING_RE.match(block.timing):
            raise SrtFormatError(f"Invalid SRT timing at index {block.index}: {block.timing!r}")
        if not block.text or not any(line.strip() for line in block.text):
            raise SrtFormatError(f"Missing subtitle text at index {block.index}.")


def validate_same_numbering(original: list[SrtBlock], translated: list[SrtBlock]) -> None:
    original_indexes = [block.index for block in original]
    translated_indexes = [block.index for block in translated]
    if original_indexes != translated_indexes:
        raise SrtFormatError(
            "Translated SRT indexes do not match original indexes. "
            f"Expected {original_indexes}, got {translated_indexes}."
        )


def validate_same_timings(original: list[SrtBlock], translated: list[SrtBlock]) -> None:
    original_timings = [block.timing.strip() for block in original]
    translated_timings = [block.timing.strip() for block in translated]
    if original_timings != translated_timings:
        raise SrtFormatError("Translated SRT timings do not match original timings.")


def validate_translation(original: list[SrtBlock], translated: list[SrtBlock]) -> None:
    validate_srt_structure(translated)
    if len(original) != len(translated):
        raise SrtFormatError(
            f"Translated SRT block count mismatch. Expected {len(original)}, got {len(translated)}."
        )
    validate_same_numbering(original, translated)
    validate_same_timings(original, translated)


def chunk_blocks(blocks: list[SrtBlock], batch_size: int) -> list[list[SrtBlock]]:
    return [blocks[index : index + batch_size] for index in range(0, len(blocks), batch_size)]
