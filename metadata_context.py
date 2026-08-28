from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse

import requests

from config import AppConfig
from local_catalog import _read_local_series
from series_metadata import (
    SeriesMetadataStore,
    SeriesProfile,
    canonical_local_path,
    season_number_for_video,
    series_root_for_video,
    stable_series_id,
)


ANILIST_URL = "https://graphql.anilist.co"
METADATA_MISS_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class MetadataContext:
    title: str
    provider: str
    text: str
    cached: bool = False
    provider_id: str = ""
    query_title: str = ""
    local_path: str = ""
    titles: tuple[str, ...] = ()
    synopsis: str = ""
    characters: tuple[str, ...] = ()
    staff: tuple[str, ...] = ()
    cover_image_url: str = ""
    cover_image_color: str = ""
    cover_image_cache_key: str = ""
    cover_image_updated_at: float = 0.0
    match_confidence: float = 0.0
    metadata_version: str = ""
    glossary: dict[str, str] | None = None


def build_series_metadata_context(
    video_path: str | Path,
    config: AppConfig,
    logger: logging.Logger,
) -> MetadataContext | None:
    if not bool(getattr(config, "translation_metadata_context_enabled", False)):
        return None

    title = infer_series_title(Path(video_path))
    if not title:
        return None

    video = Path(video_path)
    series_root = series_root_for_video(video)
    local_series = _read_local_series(series_root)
    aliases = list(local_series.aliases) if local_series is not None else [series_root.name, title]
    premiered_year = local_series.premiered_year if local_series is not None else _year_from_path(series_root)
    anidb_id = local_series.anidb_id if local_series is not None else None
    mikan_bangumi_id = _mikan_bangumi_id_for_path(config, series_root)
    store: SeriesMetadataStore | None = None
    try:
        store = SeriesMetadataStore.from_config(config)
        existing = store.get_by_local_path(series_root)
        if existing is not None and existing.locked:
            try:
                return _context_from_profile(existing, store.glossary(series_root), cached=True)
            finally:
                store.close()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Series metadata database unavailable path=%s error=%s", series_root, exc)
        if store is not None:
            store.close()
        store = None

    cache_path = _resolve_cache_path(config)
    cached = _read_cached(cache_path, title, config)
    if cached is not None:
        context = replace(cached, local_path=str(series_root), glossary=store.glossary(series_root) if store else {})
        profile = _sync_series_profile(
            store,
            context,
            series_root=series_root,
            aliases=aliases,
            premiered_year=premiered_year,
            anidb_id=anidb_id,
            mikan_bangumi_id=mikan_bangumi_id,
            season_number=season_number_for_video(video),
            config=config,
            logger=logger,
        )
        if profile is not None:
            context = replace(
                context,
                cover_image_url=profile.cover_image_url,
                cover_image_color=profile.cover_image_color,
                cover_image_cache_key=profile.cover_image_cache_key,
                cover_image_updated_at=profile.cover_image_updated_at,
            )
        if store is not None:
            store.close()
        return context
    if _read_cached_miss(cache_path, title, config):
        if store is not None:
            store.close()
        return None

    providers = [provider.lower() for provider in getattr(config, "metadata_context_providers", ["anilist"])]
    provider_failed = False
    provider_checked = False
    for provider in providers:
        try:
            if provider == "anilist":
                provider_checked = True
                # AniList performs fuzzy title matching itself. One canonical
                # request avoids multiplying misses for every episode/alias.
                candidates = [title]
                contexts = [
                    item
                    for query_title in candidates
                    if (item := _fetch_anilist_context(query_title, config, expected_year=premiered_year)) is not None
                ]
                context = max(contexts, key=lambda item: item.match_confidence, default=None)
            else:
                continue
        except Exception as exc:  # noqa: BLE001 - metadata should improve translation, not block it.
            provider_failed = True
            logger.warning("Metadata context provider failed provider=%s title=%s error=%s", provider, title, exc)
            continue

        if context is None or not context.text.strip():
            continue
        min_confidence = max(0.0, min(1.0, float(getattr(config, "series_metadata_match_min_confidence", 0.65))))
        if context.match_confidence < min_confidence:
            logger.warning(
                "Reject low-confidence series metadata match query=%s matched=%s confidence=%.3f min=%.3f",
                title,
                context.title,
                context.match_confidence,
                min_confidence,
            )
            continue
        context = replace(context, local_path=str(series_root))
        profile = _sync_series_profile(
            store,
            context,
            series_root=series_root,
            aliases=aliases,
            premiered_year=premiered_year,
            anidb_id=anidb_id,
            mikan_bangumi_id=mikan_bangumi_id,
            season_number=season_number_for_video(video),
            config=config,
            logger=logger,
        )
        if store is not None:
            if bool(getattr(config, "series_metadata_auto_seed_terms", True)):
                store.seed_terms(series_root, context.characters, term_type="character")
            context = replace(context, glossary=store.glossary(series_root))
        if profile is not None:
            context = replace(
                context,
                cover_image_url=profile.cover_image_url,
                cover_image_color=profile.cover_image_color,
                cover_image_cache_key=profile.cover_image_cache_key,
                cover_image_updated_at=profile.cover_image_updated_at,
            )
        _write_cached(cache_path, title, context)
        if store is not None:
            store.close()
        return context

    if provider_checked and not provider_failed:
        _write_cached_miss(cache_path, title)
    if store is not None:
        store.close()
    return None


def infer_series_title(video_path: Path) -> str:
    parent = video_path.parent.name.strip()
    grandparent = video_path.parent.parent.name.strip() if video_path.parent.parent != video_path.parent else ""
    if _looks_like_season_folder(parent) and grandparent:
        return _clean_title(grandparent)
    if parent and not _looks_like_generic_folder(parent):
        return _clean_title(parent)
    return _clean_title(video_path.stem)


def _fetch_anilist_context(
    title: str,
    config: AppConfig,
    *,
    expected_year: int | None = None,
) -> MetadataContext | None:
    query = """
    query ($search: String) {
      Page(page: 1, perPage: 5) {
        media(search: $search, type: ANIME) {
          id
          seasonYear
          format
          episodes
          synonyms
          title {
            romaji
            english
            native
          }
          description(asHtml: false)
          coverImage {
            large
            medium
            color
          }
          characters(page: 1, perPage: 12) {
            nodes {
              name {
                full
                native
              }
            }
          }
          staff(page: 1, perPage: 8) {
            nodes {
              name {
                full
                native
              }
            }
          }
        }
      }
    }
    """
    response = requests.post(
        ANILIST_URL,
        json={"query": query, "variables": {"search": title}},
        headers={
            "Accept": "application/json",
            "User-Agent": "anime-subtitle-worker/1.0",
        },
        timeout=int(getattr(config, "metadata_context_timeout_seconds", 15)),
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", {})
    page = data.get("Page") if isinstance(data, dict) else None
    media_items = page.get("media", []) if isinstance(page, dict) else []
    # Backward compatibility with cached fixtures and older test payloads.
    if not media_items and isinstance(data, dict) and isinstance(data.get("Media"), dict):
        media_items = [data["Media"]]
    media_items = [item for item in media_items if isinstance(item, dict)]
    if not media_items:
        return None

    media, confidence = max(
        ((_media, _anilist_match_confidence(title, _media, expected_year)) for _media in media_items),
        key=lambda item: item[1],
    )

    title_values = media.get("title") if isinstance(media.get("title"), dict) else {}
    titles = _unique_nonempty(
        [
            title_values.get("romaji"),
            title_values.get("english"),
            title_values.get("native"),
            *(media.get("synonyms") if isinstance(media.get("synonyms"), list) else []),
            title,
        ]
    )
    description = _clean_description(str(media.get("description") or ""), config)
    characters = _names_from_nodes(media.get("characters", {}).get("nodes", []))
    staff = _names_from_nodes(media.get("staff", {}).get("nodes", []))
    cover_image = media.get("coverImage") if isinstance(media.get("coverImage"), dict) else {}
    cover_image_url = str(cover_image.get("large") or cover_image.get("medium") or "").strip()
    cover_image_color = str(cover_image.get("color") or "").strip()

    # Names and canonical titles are more useful for subtitle translation than
    # a long synopsis, so keep them ahead of the text budget.
    lines = ["Series metadata context:"]
    if titles:
        lines.append("Titles: " + " / ".join(titles[:4]))
    if characters:
        lines.append("Characters: " + ", ".join(characters[:12]))
    if description:
        lines.append("Synopsis: " + _trim_context(description, 320))
    if staff:
        lines.append("Staff: " + ", ".join(staff[:8]))

    text = _trim_context("\n".join(lines), int(getattr(config, "metadata_context_max_chars", 2000)))
    metadata_version = hashlib.sha256(
        json.dumps(
            {
                "id": media.get("id"),
                "titles": titles,
                "description": description,
                "characters": characters,
                "staff": staff,
                "cover_image_url": cover_image_url,
                "cover_image_color": cover_image_color,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return MetadataContext(
        title=titles[0] if titles else title,
        provider="anilist",
        text=text,
        cached=False,
        provider_id=str(media.get("id") or ""),
        query_title=title,
        titles=tuple(titles),
        synopsis=description,
        characters=tuple(characters),
        staff=tuple(staff),
        cover_image_url=cover_image_url,
        cover_image_color=cover_image_color,
        match_confidence=confidence,
        metadata_version=metadata_version,
        glossary={},
    )


def _anilist_match_confidence(title: str, media: dict[str, Any], expected_year: int | None) -> float:
    title_values = media.get("title") if isinstance(media.get("title"), dict) else {}
    candidates = _unique_nonempty(
        [
            title_values.get("romaji"),
            title_values.get("english"),
            title_values.get("native"),
            *(media.get("synonyms") if isinstance(media.get("synonyms"), list) else []),
        ]
    )
    query = _normalize_match_title(title)
    similarities = [SequenceMatcher(None, query, _normalize_match_title(candidate)).ratio() for candidate in candidates]
    confidence = max(similarities, default=0.0)
    if any(query and query == _normalize_match_title(candidate) for candidate in candidates):
        confidence = max(confidence, 0.96)
    media_year = _safe_int(media.get("seasonYear"))
    if expected_year and media_year:
        if expected_year == media_year:
            confidence += 0.04
        elif abs(expected_year - media_year) > 1:
            confidence -= 0.18
    return max(0.0, min(1.0, confidence))


def _sync_series_profile(
    store: SeriesMetadataStore | None,
    context: MetadataContext,
    *,
    series_root: Path,
    aliases: list[str],
    premiered_year: int | None,
    anidb_id: str | None,
    mikan_bangumi_id: int | None,
    season_number: int | None,
    config: AppConfig,
    logger: logging.Logger,
) -> SeriesProfile | None:
    if store is None:
        return None
    existing = store.get_by_local_path(series_root)
    cover_cache_key, cover_updated_at = _cache_series_artwork(
        config,
        series_root=series_root,
        image_url=context.cover_image_url,
        logger=logger,
        existing=existing,
    )
    profile = SeriesProfile(
        local_path=str(series_root),
        canonical_title=context.title,
        provider=context.provider,
        provider_id=context.provider_id,
        anidb_id=str(anidb_id or ""),
        mikan_bangumi_id=mikan_bangumi_id,
        premiered_year=premiered_year,
        season_number=season_number,
        titles=list(context.titles) or [context.title],
        aliases=_unique_nonempty([*aliases, context.query_title, context.title]),
        synopsis=context.synopsis,
        characters=list(context.characters),
        staff=list(context.staff),
        cover_image_url=context.cover_image_url or (existing.cover_image_url if existing else ""),
        cover_image_color=context.cover_image_color or (existing.cover_image_color if existing else ""),
        cover_image_cache_key=cover_cache_key,
        cover_image_updated_at=cover_updated_at,
        match_confidence=context.match_confidence,
        match_source="automatic",
        locked=False,
        metadata_version=context.metadata_version,
        updated_at=time.time(),
    )
    return store.upsert_profile(profile)


def _context_from_profile(
    profile: SeriesProfile,
    glossary: dict[str, str],
    *,
    cached: bool,
) -> MetadataContext:
    lines = ["Series metadata context:"]
    titles = _unique_nonempty([profile.canonical_title, *profile.titles])
    if titles:
        lines.append("Titles: " + " / ".join(titles[:4]))
    if profile.characters:
        lines.append("Characters: " + ", ".join(profile.characters[:12]))
    if profile.synopsis:
        lines.append("Synopsis: " + _trim_context(profile.synopsis, 320))
    if profile.staff:
        lines.append("Staff: " + ", ".join(profile.staff[:8]))
    return MetadataContext(
        title=profile.canonical_title,
        provider=profile.provider,
        text="\n".join(lines),
        cached=cached,
        provider_id=profile.provider_id,
        query_title=profile.aliases[0] if profile.aliases else profile.canonical_title,
        local_path=profile.local_path,
        titles=tuple(profile.titles),
        synopsis=profile.synopsis,
        characters=tuple(profile.characters),
        staff=tuple(profile.staff),
        cover_image_url=profile.cover_image_url,
        cover_image_color=profile.cover_image_color,
        cover_image_cache_key=profile.cover_image_cache_key,
        cover_image_updated_at=profile.cover_image_updated_at,
        match_confidence=profile.match_confidence,
        metadata_version=profile.metadata_version,
        glossary=glossary,
    )


def _mikan_bangumi_id_for_path(config: AppConfig, series_root: Path) -> int | None:
    expected = canonical_local_path(series_root)
    for mapping in getattr(config, "mikan_series_path_mappings", []) or []:
        if not isinstance(mapping, dict) or "path" not in mapping:
            continue
        try:
            mapped = canonical_local_path(str(mapping["path"]))
        except (OSError, ValueError):
            continue
        if mapped != expected:
            continue
        try:
            return int(mapping.get("bangumi_id"))
        except (TypeError, ValueError):
            return None
    return None


def _year_from_path(path: Path) -> int | None:
    match = re.search(r"\(((?:19|20)\d{2})\)\s*$", path.name)
    return int(match.group(1)) if match else None


def _normalize_match_title(value: str) -> str:
    return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", "", _clean_title(value).casefold())


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cache_series_artwork(
    config: AppConfig,
    *,
    series_root: Path,
    image_url: str,
    logger: logging.Logger,
    existing: SeriesProfile | None,
) -> tuple[str, float]:
    """Cache AniList artwork under /work without exposing remote URLs to WebUI."""

    fallback_key = str(existing.cover_image_cache_key if existing else "").strip()
    fallback_updated_at = float(existing.cover_image_updated_at if existing else 0.0)
    if not bool(getattr(config, "series_artwork_cache_enabled", True)):
        return fallback_key, fallback_updated_at
    url = str(image_url or "").strip()
    if not url:
        return fallback_key, fallback_updated_at

    configured_root = Path(str(getattr(config, "series_artwork_cache_path", "series_artwork") or "series_artwork"))
    cache_root = configured_root if configured_root.is_absolute() else Path(config.work_path) / configured_root
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_root = cache_root.resolve()
    series_id = stable_series_id(canonical_local_path(series_root))

    fallback_path = _safe_artwork_cache_path(cache_root, fallback_key)
    fallback_exists = bool(fallback_path and fallback_path.is_file())
    ttl_days = max(1, int(getattr(config, "series_artwork_ttl_days", 30) or 30))
    if (
        fallback_exists
        and existing is not None
        and existing.cover_image_url == url
        and time.time() - fallback_path.stat().st_mtime <= ttl_days * 86400
    ):
        return fallback_key, fallback_updated_at or fallback_path.stat().st_mtime

    failure_marker = cache_root / f"{series_id}.failed"
    if failure_marker.is_file() and time.time() - failure_marker.stat().st_mtime < 6 * 60 * 60:
        return (fallback_key, fallback_updated_at) if fallback_exists else ("", 0.0)

    response: Any = None
    try:
        _validate_anilist_artwork_url(url)
        response = requests.get(
            url,
            headers={"Accept": "image/jpeg,image/png,image/webp", "User-Agent": "anime-subtitle-worker/1.0"},
            timeout=int(getattr(config, "metadata_context_timeout_seconds", 15)),
            stream=True,
        )
        response.raise_for_status()
        final_url = str(getattr(response, "url", "") or url)
        _validate_anilist_artwork_url(final_url)
        max_bytes = max(64 * 1024, int(getattr(config, "series_artwork_max_bytes", 3 * 1024 * 1024) or 0))
        content_length = str(getattr(response, "headers", {}).get("Content-Length", "") or "").strip()
        if content_length.isdigit() and int(content_length) > max_bytes:
            raise ValueError("artwork response exceeds configured size limit")
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ValueError("artwork response exceeds configured size limit")
        suffix = _validated_image_suffix(bytes(payload))
        cache_key = f"{series_id}{suffix}"
        target = cache_root / cache_key
        temp = cache_root / f".{cache_key}.{os.getpid()}.tmp"
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)
        for stale in cache_root.glob(f"{series_id}.*"):
            if stale not in {target, failure_marker} and stale.is_file():
                stale.unlink(missing_ok=True)
        failure_marker.unlink(missing_ok=True)
        _prune_series_artwork_cache(
            cache_root,
            keep=target,
            max_bytes=max(16, int(getattr(config, "series_artwork_cache_max_mib", 512) or 512)) * 1024 * 1024,
        )
        return cache_key, time.time()
    except Exception as exc:  # noqa: BLE001 - artwork must never block subtitle processing.
        try:
            failure_marker.write_text(type(exc).__name__, encoding="utf-8")
        except OSError:
            pass
        logger.warning("Series artwork cache failed series=%s url=%s error=%s", series_root, url, exc)
        return (fallback_key, fallback_updated_at) if fallback_exists else ("", 0.0)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _validate_anilist_artwork_url(value: str) -> None:
    parsed = urlparse(str(value or ""))
    hostname = str(parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or not (
        hostname == "anilist.co" or hostname.endswith(".anilist.co")
    ):
        raise ValueError("artwork URL is not an AniList HTTPS resource")


def _validated_image_suffix(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("artwork payload is not a supported image")


def _safe_artwork_cache_path(cache_root: Path, cache_key: str) -> Path | None:
    key = str(cache_key or "").strip()
    if not re.fullmatch(r"series_[0-9a-f]{24}\.(?:jpg|png|webp)", key, flags=re.IGNORECASE):
        return None
    candidate = (cache_root / key).resolve()
    try:
        candidate.relative_to(cache_root)
    except ValueError:
        return None
    return candidate


def _prune_series_artwork_cache(cache_root: Path, *, keep: Path, max_bytes: int) -> None:
    files = [
        path for path in cache_root.iterdir()
        if path.is_file() and path.suffix.casefold() in {".jpg", ".png", ".webp"}
    ]
    total = sum(path.stat().st_size for path in files)
    if total <= max_bytes:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        if path == keep:
            continue
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size
        if total <= max_bytes:
            break


def _read_cached(cache_path: Path, title: str, config: AppConfig) -> MetadataContext | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    item = payload.get("items", {}).get(_cache_key(title))
    if not isinstance(item, dict):
        return None

    ttl_seconds = int(getattr(config, "metadata_context_ttl_days", 30)) * 86400
    updated_at = float(item.get("updated_at") or 0)
    if ttl_seconds > 0 and updated_at and time.time() - updated_at > ttl_seconds:
        return None

    text = str(item.get("text") or "").strip()
    if not text:
        return None
    return MetadataContext(
        title=str(item.get("title") or title),
        provider=str(item.get("provider") or "cache"),
        text=text,
        cached=True,
        provider_id=str(item.get("provider_id") or ""),
        query_title=str(item.get("query_title") or title),
        local_path=str(item.get("local_path") or ""),
        titles=tuple(_string_list(item.get("titles"))),
        synopsis=str(item.get("synopsis") or ""),
        characters=tuple(_string_list(item.get("characters"))),
        staff=tuple(_string_list(item.get("staff"))),
        cover_image_url=str(item.get("cover_image_url") or ""),
        cover_image_color=str(item.get("cover_image_color") or ""),
        cover_image_cache_key=str(item.get("cover_image_cache_key") or ""),
        cover_image_updated_at=float(item.get("cover_image_updated_at") or 0.0),
        match_confidence=float(item.get("match_confidence") or 0.0),
        metadata_version=str(item.get("metadata_version") or ""),
        glossary={},
    )


def _read_cached_miss(cache_path: Path, title: str, config: AppConfig) -> bool:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    item = payload.get("items", {}).get(_cache_key(title))
    if not isinstance(item, dict) or not bool(item.get("not_found")):
        return False
    updated_at = float(item.get("updated_at") or 0)
    configured_ttl = int(
        getattr(config, "metadata_context_miss_ttl_seconds", METADATA_MISS_TTL_SECONDS)
        or METADATA_MISS_TTL_SECONDS
    )
    return bool(updated_at and time.time() - updated_at <= max(60, configured_ttl))


def _write_cached(cache_path: Path, query_title: str, context: MetadataContext) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"version": 1, "items": {}}
        payload.setdefault("items", {})[_cache_key(query_title)] = {
            "query_title": query_title,
            "title": context.title,
            "provider": context.provider,
            "provider_id": context.provider_id,
            "local_path": context.local_path,
            "text": context.text,
            "titles": list(context.titles),
            "synopsis": context.synopsis,
            "characters": list(context.characters),
            "staff": list(context.staff),
            "cover_image_url": context.cover_image_url,
            "cover_image_color": context.cover_image_color,
            "cover_image_cache_key": context.cover_image_cache_key,
            "cover_image_updated_at": context.cover_image_updated_at,
            "match_confidence": context.match_confidence,
            "metadata_version": context.metadata_version,
            "updated_at": time.time(),
        }
        _atomic_write_json(cache_path, payload)
    except OSError:
        return


def _write_cached_miss(cache_path: Path, query_title: str) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"version": 1, "items": {}}
        payload.setdefault("items", {})[_cache_key(query_title)] = {
            "query_title": query_title,
            "not_found": True,
            "updated_at": time.time(),
        }
        _atomic_write_json(cache_path, payload)
    except OSError:
        return


def _resolve_cache_path(config: AppConfig) -> Path:
    configured = Path(str(getattr(config, "metadata_context_cache_path", "metadata_context_cache.json")))
    if configured.is_absolute():
        return configured
    return config.work_path / configured


def _cache_key(title: str) -> str:
    return hashlib.sha1(_clean_title(title).casefold().encode("utf-8")).hexdigest()


def _looks_like_season_folder(name: str) -> bool:
    return bool(re.fullmatch(r"(?i)(season|saison|staffel|series)\s*\d+|S\d+", name.strip()))


def _looks_like_generic_folder(name: str) -> bool:
    return name.strip().casefold() in {"anime", "downloads", "video", "videos", "season", "movies"}


def _clean_title(value: str) -> str:
    text = value.strip()
    text = re.sub(r"\[[^\]]+\]", " ", text)
    # Sonarr commonly appends a release year. AniList's fuzzy search treats
    # that suffix as part of the title and returns HTTP 404 for otherwise
    # valid series such as "Kingdom (2012)".
    text = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", " ", text)
    text = re.sub(r"\bS\d{1,2}E\d{1,3}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{1,3}\s*(?:END)?\b", " ", text)
    text = re.sub(r"\b(1080p|720p|2160p|webrip|web-dl|bluray|bdrip|hevc|x264|x265|flac|aac|mkv|mp4)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[_\.]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_")


def _clean_description(value: str, config: AppConfig) -> str:
    text = html.unescape(value)
    if not bool(getattr(config, "metadata_context_include_spoilers", False)):
        text = re.sub(r"(?is)<spoiler>.*?</spoiler>", " ", text)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\(Source:.*?\)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _names_from_nodes(nodes: Any) -> list[str]:
    if not isinstance(nodes, list):
        return []
    names: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = node.get("name")
        if not isinstance(name, dict):
            continue
        names.extend(_unique_nonempty([name.get("full"), name.get("native")])[:2])
    return _unique_nonempty(names)


def _unique_nonempty(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return _unique_nonempty(list(value))


def _trim_context(text: str, max_chars: int) -> str:
    max_chars = max(200, int(max_chars))
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
