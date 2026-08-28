from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from config import AppConfig


@dataclass(frozen=True)
class LocalSeries:
    path: Path
    aliases: list[str]
    premiered_year: int | None
    anidb_id: str | None


def discover_local_series(config: AppConfig) -> list[LocalSeries]:
    roots = _series_roots(config.input_path, config.video_extensions)
    return [series for root in roots if (series := _read_local_series(root)) is not None]


def _series_roots(input_path: Path, video_extensions: list[str]) -> list[Path]:
    if not input_path.exists():
        return []

    roots: set[Path] = {path.parent for path in input_path.rglob("tvshow.nfo")}
    extension_set = {extension.lower() for extension in video_extensions}
    for video in input_path.rglob("*"):
        if video.suffix.lower() not in extension_set or not video.is_file():
            continue
        try:
            relative = video.parent.relative_to(input_path)
        except ValueError:
            continue
        root = input_path / relative.parts[0] if relative.parts else video.parent
        roots.add(root)
    return sorted(roots, key=lambda item: str(item).casefold())


def _read_local_series(root: Path) -> LocalSeries | None:
    aliases: list[str] = [root.name]
    premiered_year: int | None = None
    anidb_id: str | None = None

    tvshow = _parse_xml(root / "tvshow.nfo")
    if tvshow is not None:
        aliases.extend(_text_values(tvshow, ["title", "originaltitle"]))
        premiered_year = _year_from_text(_first_text(tvshow, "premiered") or _first_text(tvshow, "releasedate"))
        anidb_id = _first_text(tvshow, "anidbid")

    for episode_nfo in sorted(root.rglob("*.nfo"), key=lambda item: str(item).casefold())[:12]:
        if episode_nfo.name.casefold() in {"tvshow.nfo", "season.nfo"}:
            continue
        episode = _parse_xml(episode_nfo)
        if episode is None:
            continue
        aliases.extend(_text_values(episode, ["showtitle"]))
        if premiered_year is None:
            premiered_year = _year_from_text(_first_text(episode, "aired"))

    split_aliases: list[str] = []
    for alias in aliases:
        split_aliases.extend(_split_aliases(alias))

    unique_aliases = _unique_aliases([*aliases, *split_aliases])
    if not unique_aliases:
        return None
    return LocalSeries(root, unique_aliases, premiered_year, anidb_id)


def _parse_xml(path: Path) -> ET.Element | None:
    if not path.exists():
        return None
    try:
        return ET.fromstring(path.read_text(encoding="utf-8-sig", errors="replace"))
    except ET.ParseError:
        return None


def _text_values(root: ET.Element, tags: list[str]) -> list[str]:
    values: list[str] = []
    for tag in tags:
        value = _first_text(root, tag)
        if value:
            values.append(value)
    return values


def _first_text(root: ET.Element, tag: str) -> str | None:
    child = root.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _split_aliases(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*/\s*|\s+\|\s+|;", value) if part.strip()]


def _unique_aliases(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if len(cleaned) < 2:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _year_from_text(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(19|20)\d{2}", value)
    return int(match.group(0)) if match else None
