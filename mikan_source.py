from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import base64
import re
import time
from typing import Any
import unicodedata
from urllib.parse import parse_qs, urljoin, urlparse
import xml.etree.ElementTree as ET

import requests


EPISODE_RE = re.compile(
    r"(?:第\s*(\d{1,3})\s*(?:話|话)|[\[\s\-_\(（](\d{1,3})[\]\s\-_\)）])"
)
SXXEYY_RE = re.compile(r"[Ss]\d{1,3}[Ee](\d{1,3})")
SXXEYY_RANGE_RE = re.compile(r"[Ss]\d{1,3}[Ee](\d{1,3})\s*(?:-|~|～|–|—|至|到)\s*(?:[Ss]\d{1,3}[Ee])?(\d{1,3})")
TRAILING_RELEASE_EPISODE_RANGE_RE = re.compile(r"\s-\s*(\d{1,3})\s*(?:-|~|～|–|—|至|到)\s*(\d{1,3})(?=\s*(?:\[|$))")
BRACKET_EPISODE_RANGE_RE = re.compile(r"[\[\(]\s*(\d{1,3})\s*(?:-|~|～|–|—|至|到)\s*(\d{1,3})\s*[\]\)]")
TRAILING_RELEASE_EPISODE_RE = re.compile(r"\s-\s*(\d{1,3})(?=\s*(?:\[|$))")
BRACKET_EPISODE_RE = re.compile(r"[\[\(【]\s*(\d{1,3})(?:\s*[vV]\d+)?\s*[\]\)】]")
DASH_EPISODE_RE = re.compile(
    r"(?:^|[\s._-])(?:EP?|第)?\s*(\d{1,3})(?:\s*(?:話|话|集))?(?=$|[\s._\]])",
    re.IGNORECASE,
)


MAX_EPISODE_RANGE_COUNT = 64


TRADITIONAL_KEYWORDS = (
    "繁日",
    "繁體",
    "繁体",
    "繁中",
    "繁",
    "cht",
    "tc",
    "traditional",
)
SIMPLIFIED_KEYWORDS = (
    "简繁",
    "簡繁",
    "简日",
    "簡日",
    "简体",
    "簡體",
    "简中",
    "簡中",
    "chs",
    "sc",
    "simplified",
)
CHINESE_KEYWORDS = (
    *TRADITIONAL_KEYWORDS,
    *SIMPLIFIED_KEYWORDS,
    "字幕",
    "内封",
    "內封",
    "内嵌",
    "內嵌",
)
EXTRACTABLE_KEYWORDS = ("内封", "內封", "内挂", "內掛", "mkv", "ass", "srt")
HARDCODED_KEYWORDS = ("内嵌", "內嵌", "mp4")
MULTISUB_KEYWORDS = ("multi-sub", "multi sub", "multisub", "multiple subtitles", "multiple subtitle")
EXPLICIT_CHINESE_SUBTITLE_KEYWORDS = (
    "chs",
    "cht",
    "chinese",
    "zh-cn",
    "zh_tw",
    "zh-tw",
    "big5",
    "gb_big5",
    "gb&big5",
    "gb/big5",
    "中文",
    "中字",
    "简体",
    "簡體",
    "繁体",
    "繁體",
    "简繁",
    "簡繁",
    "双语",
    "雙語",
)
ENGLISH_SUBTITLE_KEYWORDS = (
    "english subtitle",
    "english subtitles",
    "eng subtitle",
    "eng subtitles",
    "eng sub",
    "eng subs",
    "eng-sub",
    "eng-subs",
    "eng_srt",
    "[eng]",
    "(eng)",
    "_eng_",
    "英文字幕",
    "英語字幕",
    "英语字幕",
)


SOURCE_PRIORITY_SCORE = {
    "mikan": 16,
    "animegarden": 14,
    "dmhy": 12,
    "nyaa": 10,
    "bangumimoe": 8,
    "kisssub": 6,
    "acgrip": 4,
}


def release_season_number(value: str) -> int | None:
    """Return only an explicit season marker from a release title.

    A bare ``Season`` word followed by an episode marker is deliberately not
    accepted.  For example, ``Off & Monster Season [06]`` is episode 6, not
    season 6.
    """

    text = unicodedata.normalize("NFKC", str(value or ""))
    patterns = (
        r"(?i)\bS(?:eason)?\s*0*(\d{1,2})\s*E\d{1,3}\b",
        r"(?i)\bS\s*0*(\d{1,2})\s*[-_. ]+\s*\d{1,3}\b",
        r"(?i)\bSeason\s*0*(\d{1,2})\s*[-_. ]+\s*\d{1,3}\b",
        r"(?i)\b0*(\d{1,2})(?:st|nd|rd|th)\s+Season\b",
        r"\u7b2c\s*0*(\d{1,2})\s*[\u5b63\u671f]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            season = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 0 <= season <= 99:
            return season
    return None


def release_series_identity(value: str) -> str:
    """Extract a conservative, normalized series identity from a release."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    leading_group = re.match(r"^\s*\[[^\]]{1,80}\]\s*", text)
    if leading_group:
        text = text[leading_group.end():]

    boundaries = (
        r"(?i)\bS(?:eason)?\s*\d{1,2}\s*E\d{1,3}\b",
        r"(?i)\s+-\s+\d{1,3}(?:\s*[vV]\d+)?(?=\s*(?:\[|\(|$))",
        r"(?i)[\[(]\s*\d{1,3}(?:\s*[vV]\d+)?\s*[\])]",
        r"(?i)(?:^|[\s._-])EP?\s*\d{1,3}\b",
    )
    starts = [
        match.start()
        for pattern in boundaries
        if (match := re.search(pattern, text)) is not None
    ]
    if starts:
        text = text[:min(starts)]

    season_suffixes = (
        r"(?i)\bS(?:eason)?\s*0*\d{1,2}\s*$",
        r"(?i)\bSeason\s*0*\d{1,2}\s*$",
        r"(?i)\b0*\d{1,2}(?:st|nd|rd|th)\s+Season\s*$",
        r"\u7b2c\s*0*\d{1,2}\s*[\u5b63\u671f]\s*$",
    )
    for pattern in season_suffixes:
        text = re.sub(pattern, "", text).strip()

    text = re.sub(r"[\[\](){}]+", " ", text)
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class MikanRelease:
    bangumi_id: int
    title: str
    episode: int | None
    torrent_url: str
    pub_date: datetime | None
    content_length: int | None
    link: str | None = None
    episodes: tuple[int, ...] = ()
    source: str = "mikan"
    info_hash: str | None = None
    seeders: int | None = None
    season_number: int | None = None
    series_identity: str = ""
    identity_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        season = self.season_number
        if season is None:
            season = release_season_number(self.title)
            object.__setattr__(self, "season_number", season)
        identity = str(self.series_identity or "").strip()
        if not identity:
            identity = release_series_identity(self.title)
            object.__setattr__(self, "series_identity", identity)
        if not self.identity_evidence:
            evidence: list[str] = []
            if identity:
                evidence.append("release_title_identity")
            if season is not None:
                evidence.append(f"explicit_season:{season}")
            if str(self.source or "").split(":", 1)[0].casefold() == "mikan":
                evidence.append(f"mikan_bangumi:{int(self.bangumi_id)}")
            object.__setattr__(self, "identity_evidence", tuple(evidence))


def release_episode_numbers(release: MikanRelease) -> tuple[int, ...]:
    if release.episodes:
        return release.episodes
    if release.episode is not None:
        return (release.episode,)
    return ()


class MikanSourceError(RuntimeError):
    pass


def fetch_bangumi_releases(
    base_url: str,
    bangumi_id: int,
    timeout_seconds: int = 30,
    max_attempts: int = 3,
) -> list[MikanRelease]:
    url = urljoin(base_url.rstrip("/") + "/", f"RSS/Bangumi?bangumiId={bangumi_id}")
    last_error: Exception | None = None
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=timeout_seconds)
            response.raise_for_status()
            return parse_mikan_rss(response.text, base_url, bangumi_id)
        except (requests.RequestException, MikanSourceError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            time.sleep(min(0.25 * attempt, 1.0))
    if last_error is not None:
        raise last_error
    return []


def parse_mikan_rss(content: str, base_url: str, bangumi_id: int) -> list[MikanRelease]:
    if not content.strip():
        raise MikanSourceError(f"Empty Mikan RSS response for bangumi_id={bangumi_id}")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise MikanSourceError(
            f"Invalid Mikan RSS XML for bangumi_id={bangumi_id}: {exc} chars={len(content)}"
        ) from exc
    releases: list[MikanRelease] = []
    for item in root.findall("./channel/item"):
        title = _element_text(item, "title")
        if not title:
            continue

        torrent_url = _enclosure_url(item)
        if not torrent_url:
            continue

        pub_date = _mikan_pub_date(item)
        content_length = _mikan_content_length(item)
        episodes = extract_episode_numbers(title)
        releases.append(
            MikanRelease(
                bangumi_id=bangumi_id,
                title=title,
                episode=episodes[0] if episodes else None,
                torrent_url=urljoin(base_url.rstrip("/") + "/", torrent_url),
                pub_date=pub_date,
                content_length=content_length,
                link=_element_text(item, "link"),
                episodes=episodes,
                source="mikan",
                info_hash=extract_torrent_info_hash(torrent_url),
            )
        )
    return releases


def select_preferred_releases(
    releases: list[MikanRelease],
    *,
    max_items: int,
    prefer_keywords: list[str],
    reject_keywords: list[str],
    require_extractable: bool = True,
) -> list[MikanRelease]:
    by_episode = _best_release_by_episode(
        releases,
        prefer_keywords=prefer_keywords,
        reject_keywords=reject_keywords,
        require_extractable=require_extractable,
    )

    sorted_releases = sorted(
        _unique_releases(by_episode.values()),
        key=lambda release: (
            _release_sort_episode(release),
            release.pub_date or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return sorted_releases[:max_items]


def select_preferred_releases_for_episodes(
    releases: list[MikanRelease],
    *,
    episodes: set[int],
    prefer_keywords: list[str],
    reject_keywords: list[str],
    require_extractable: bool = True,
) -> list[MikanRelease]:
    by_episode = _best_release_by_episode(
        releases,
        prefer_keywords=prefer_keywords,
        reject_keywords=reject_keywords,
        require_extractable=require_extractable,
    )
    return _unique_releases(by_episode[episode] for episode in sorted(episodes, reverse=True) if episode in by_episode)


def select_preferred_release_candidates_for_episodes(
    releases: list[MikanRelease],
    *,
    episodes: set[int],
    prefer_keywords: list[str],
    reject_keywords: list[str],
    require_extractable: bool = True,
) -> dict[int, list[MikanRelease]]:
    candidates = _candidate_releases(
        releases,
        prefer_keywords=prefer_keywords,
        reject_keywords=reject_keywords,
        require_extractable=require_extractable,
    )
    result: dict[int, list[MikanRelease]] = {episode: [] for episode in sorted(episodes)}
    for release in candidates:
        for episode in release_episode_numbers(release):
            if episode in result:
                result[episode].append(release)
    for episode, episode_releases in result.items():
        result[episode] = sorted(
            episode_releases,
            key=lambda release: release_score(release, prefer_keywords),
            reverse=True,
        )
    return result


def _best_release_by_episode(
    releases: list[MikanRelease],
    *,
    prefer_keywords: list[str],
    reject_keywords: list[str],
    require_extractable: bool,
) -> dict[int | None, MikanRelease]:
    candidates = _candidate_releases(
        releases,
        prefer_keywords=prefer_keywords,
        reject_keywords=reject_keywords,
        require_extractable=require_extractable,
    )

    by_episode: dict[int | None, MikanRelease] = {}
    for release in candidates:
        episodes = release_episode_numbers(release) or (None,)
        for episode in episodes:
            current = by_episode.get(episode)
            if current is None or release_score(release, prefer_keywords) > release_score(current, prefer_keywords):
                by_episode[episode] = release
    return by_episode


def _unique_releases(releases: Any) -> list[MikanRelease]:
    result: list[MikanRelease] = []
    seen_keys: set[str] = set()
    for release in releases:
        key = release.info_hash or release.torrent_url.casefold()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        result.append(release)
    return result


def _release_sort_episode(release: MikanRelease) -> int:
    episodes = release_episode_numbers(release)
    if episodes:
        return max(episodes)
    return -1


def _candidate_releases(
    releases: list[MikanRelease],
    *,
    prefer_keywords: list[str],
    reject_keywords: list[str],
    require_extractable: bool,
) -> list[MikanRelease]:
    candidates = [
        release
        for release in releases
        if not has_english_only_subtitle_hint(release.title)
        and (
            has_chinese_subtitle_hint(release.title)
            or (release.source != "mikan" and has_multisubtitle_hint(release.title))
        )
    ]
    if require_extractable:
        candidates = [
            release
            for release in candidates
            if has_extractable_subtitle_hint(release.title)
            or (
                release.source != "mikan"
                and has_multisubtitle_hint(release.title)
                and "mkv" in release.title.casefold()
            )
        ]
    if reject_keywords:
        lowered_rejects = [keyword.casefold() for keyword in reject_keywords]
        candidates = [
            release
            for release in candidates
            if not any(keyword in release.title.casefold() for keyword in lowered_rejects)
        ]
    return candidates


def has_chinese_subtitle_hint(title: str) -> bool:
    lowered = title.casefold()
    return any(keyword.casefold() in lowered for keyword in CHINESE_KEYWORDS)


def has_multisubtitle_hint(title: str) -> bool:
    lowered = title.casefold()
    return any(keyword in lowered for keyword in MULTISUB_KEYWORDS)


def has_explicit_chinese_subtitle_hint(title: str) -> bool:
    lowered = title.casefold()
    compact = re.sub(r"[\s._\-]+", "", lowered)
    return any(
        keyword.casefold() in lowered or keyword.casefold() in compact
        for keyword in EXPLICIT_CHINESE_SUBTITLE_KEYWORDS
    )


def has_english_only_subtitle_hint(title: str) -> bool:
    if has_explicit_chinese_subtitle_hint(title):
        return False
    lowered = title.casefold()
    compact = re.sub(r"[\s._\-]+", " ", lowered)
    return any(keyword.casefold() in lowered or keyword.casefold() in compact for keyword in ENGLISH_SUBTITLE_KEYWORDS)


def has_extractable_subtitle_hint(title: str) -> bool:
    lowered = title.casefold()
    if any(keyword.casefold() in lowered for keyword in ("内封", "內封", "内挂", "內掛", "内封字幕", "內封字幕")):
        return True
    if "mkv" in lowered and has_chinese_subtitle_hint(title) and not any(
        keyword.casefold() in lowered for keyword in ("内嵌", "內嵌")
    ):
        return True
    return False


def release_score(release: MikanRelease, prefer_keywords: list[str]) -> tuple[int, datetime, int]:
    title = release.title.casefold()
    score = 0
    source_base = str(release.source or "").split(":", 1)[0].casefold()
    score += SOURCE_PRIORITY_SCORE.get(source_base, 0)
    for offset, keyword in enumerate(prefer_keywords):
        if keyword.casefold() in title:
            score += max(1, 100 - offset)
    if has_explicit_chinese_subtitle_hint(release.title):
        score += 80
    elif has_chinese_subtitle_hint(release.title):
        score += 35
    if has_multisubtitle_hint(release.title):
        score += 15
    if any(keyword.casefold() in title for keyword in EXTRACTABLE_KEYWORDS):
        score += 30
    if any(keyword.casefold() in title for keyword in HARDCODED_KEYWORDS):
        score -= 25
    if has_english_only_subtitle_hint(release.title):
        score -= 200
    if release.seeders is not None:
        try:
            score += min(max(int(release.seeders), 0), 50)
        except (TypeError, ValueError):
            pass
    if "1080" in title:
        score += 5
    if "720" in title:
        score -= 5
    return (
        score,
        release.pub_date or datetime.min.replace(tzinfo=timezone.utc),
        release.content_length or 0,
    )


def extract_torrent_info_hash(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme.casefold() == "magnet":
        for xt in parse_qs(parsed.query).get("xt", []):
            prefix = "urn:btih:"
            if not xt.casefold().startswith(prefix):
                continue
            raw = xt[len(prefix):].strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", raw):
                return raw.casefold()
            if re.fullmatch(r"[A-Z2-7]{32}", raw.upper()):
                try:
                    return base64.b32decode(raw.upper()).hex()
                except (ValueError, TypeError):
                    return None
    query_hash = parse_qs(parsed.query).get("hash", [])
    if query_hash and re.fullmatch(r"[0-9a-fA-F]{40}", query_hash[0]):
        return query_hash[0].casefold()
    path_match = re.search(r"(?i)([0-9a-f]{40})(?:\.torrent)?(?:$|[/?#])", parsed.path)
    if path_match:
        return path_match.group(1).casefold()
    return None


def extract_episode_numbers(title: str) -> tuple[int, ...]:
    for pattern in (SXXEYY_RANGE_RE, TRAILING_RELEASE_EPISODE_RANGE_RE, BRACKET_EPISODE_RANGE_RE):
        match = pattern.search(title)
        if match:
            episode_range = _safe_episode_range(match.group(1), match.group(2))
            if episode_range:
                return episode_range

    episode = extract_episode_number(title)
    return (episode,) if episode is not None else ()


def extract_episode_number(title: str) -> int | None:
    match = SXXEYY_RE.search(title)
    if match:
        value = _safe_episode_number(match.group(1))
        if value is not None:
            return value

    for pattern in (TRAILING_RELEASE_EPISODE_RE, BRACKET_EPISODE_RE, DASH_EPISODE_RE):
        for match in pattern.finditer(title):
            if _episode_match_is_release_part(title, match):
                continue
            value = _safe_episode_number(match.group(1))
            if value is not None:
                return value

    for match in EPISODE_RE.finditer(title):
        group_index = 1 if match.group(1) is not None else 2
        if _episode_match_is_release_part(title, match, group_index=group_index):
            continue
        raw = match.group(group_index)
        value = _safe_episode_number(raw)
        if value is not None:
            return value
    return None


def _episode_match_is_release_part(
    title: str,
    match: re.Match[str],
    *,
    group_index: int = 1,
) -> bool:
    """Reject movie/volume numbering that resembles a bare episode number.

    Releases such as ``Grudge.of.Edinburgh.Part.2.2023`` previously entered the
    episode-2 queue because the permissive bare-number matcher accepted ``.2``.
    Explicit SxxExx markers remain authoritative; this guard only applies to
    generic trailing, bracketed, and delimiter-separated matches.
    """

    prefix = title[: match.start(group_index)]
    return re.search(
        r"(?i)(?:^|[\s._\-\[(])(?:part|pt|vol(?:ume)?|movie|film)\s*(?:[\s._\-\[(]\s*)*$",
        prefix,
    ) is not None


def _safe_episode_range(start_raw: str | None, end_raw: str | None) -> tuple[int, ...]:
    start = _safe_episode_number(start_raw)
    end = _safe_episode_number(end_raw)
    if start is None or end is None or end < start:
        return ()
    if end - start + 1 > MAX_EPISODE_RANGE_COUNT:
        return ()
    return tuple(range(start, end + 1))


def _safe_episode_number(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if 0 < value < 1000:
        return value
    return None


def _element_text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None


def _enclosure_url(item: ET.Element) -> str | None:
    enclosure = item.find("enclosure")
    if enclosure is None:
        return None
    url = enclosure.attrib.get("url", "").strip()
    return url or None


def _mikan_pub_date(item: ET.Element) -> datetime | None:
    for child in item:
        if _strip_namespace(child.tag) != "torrent":
            continue
        for subchild in child:
            if _strip_namespace(subchild.tag) == "pubDate" and subchild.text:
                return _parse_datetime(subchild.text.strip())
    return None


def _mikan_content_length(item: ET.Element) -> int | None:
    enclosure = item.find("enclosure")
    if enclosure is not None:
        raw_length = enclosure.attrib.get("length", "").strip()
        if raw_length.isdigit():
            return int(raw_length)

    for child in item:
        if _strip_namespace(child.tag) != "torrent":
            continue
        for subchild in child:
            if _strip_namespace(subchild.tag) == "contentLength" and subchild.text and subchild.text.strip().isdigit():
                return int(subchild.text.strip())
    return None


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag
