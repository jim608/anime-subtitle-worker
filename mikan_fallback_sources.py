from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests

from mikan_cache_store import MikanIndexedCache, sqlite_cache_enabled
from mikan_matcher import normalize_match_text
from mikan_source import MikanRelease, extract_episode_numbers, extract_torrent_info_hash, has_english_only_subtitle_hint


DEFAULT_SOURCES = ("animegarden", "dmhy", "acgrip", "bangumimoe", "kisssub", "nyaa")
USER_AGENT = "anime-subtitle-worker/1.0 fallback-source"
FALLBACK_CACHE_VERSION = 2
FALLBACK_CACHE_MAX_ENTRIES = 256
FALLBACK_CACHE_MAX_RELEASES_PER_ENTRY = 250
PROVIDER_CIRCUIT_CACHE_KEY = "__provider_circuits_v1__"
VOLUME_ONLY_RE = re.compile(r"(?i)\bvol(?:ume)?\.?\s*0*(\d{1,3})\b")
EXPLICIT_EPISODE_RE = re.compile(
    r"(?i)(?:S\d{1,3}E\d{1,3}|(?:^|\s)-\s*\d{1,3}(?:\s|$)|[\[(]\s*\d{1,3}\s*[\])])"
)
_PROVIDER_CIRCUIT_LOCK = threading.Lock()
_FALLBACK_CACHE_LOCK = threading.RLock()


@dataclass
class _ProviderCircuitState:
    consecutive_failures: int = 0
    open_until: float = 0.0
    half_open_in_flight: bool = False


_PROVIDER_CIRCUITS: dict[tuple[str, str], _ProviderCircuitState] = {}


class FallbackSearchResult(list[MikanRelease]):
    """List-compatible fallback result with retry-scheduling evidence."""

    def __init__(
        self,
        releases: list[MikanRelease] | None = None,
        *,
        conclusive: bool = True,
        lookup_performed: bool = False,
        cache_hit: bool = False,
        deferred_reason: str = "",
        successful_sources: tuple[str, ...] = (),
        failed_sources: tuple[str, ...] = (),
        skipped_sources: tuple[str, ...] = (),
    ) -> None:
        super().__init__(releases or [])
        self.conclusive = bool(conclusive)
        self.lookup_performed = bool(lookup_performed)
        self.cache_hit = bool(cache_hit)
        self.deferred_reason = str(deferred_reason or "")
        self.successful_sources = tuple(successful_sources)
        self.failed_sources = tuple(failed_sources)
        self.skipped_sources = tuple(skipped_sources)


class FallbackSourcePool:
    def __init__(self, config: Any, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self.enabled = bool(getattr(config, "mikan_fallback_sources_enabled", False))
        configured = getattr(config, "mikan_fallback_sources", list(DEFAULT_SOURCES)) or []
        self.sources = tuple(dict.fromkeys(
            source
            for source in (str(item).strip().casefold() for item in configured)
            if source
        ))
        self.timeout_seconds = max(5, int(getattr(config, "mikan_fallback_source_timeout_seconds", 20) or 20))
        self.cache_ttl_seconds = max(60, int(getattr(config, "mikan_fallback_cache_ttl_seconds", 21600) or 21600))
        self.max_lookups = max(0, int(getattr(config, "mikan_fallback_max_lookups_per_cycle", 6) or 0))
        self.min_nyaa_seeders = max(0, int(getattr(config, "mikan_fallback_min_nyaa_seeders", 1) or 0))
        self.failure_threshold = max(
            1,
            int(getattr(config, "mikan_fallback_source_failure_threshold", 2) or 2),
        )
        self.source_cooldown_seconds = max(
            1,
            int(getattr(config, "mikan_fallback_source_cooldown_seconds", 1800) or 1800),
        )
        self.cache_path = _resolve_cache_path(config)
        self._circuit_scope = os.path.normcase(os.path.abspath(str(self.cache_path)))
        self.lookup_count = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        with _FALLBACK_CACHE_LOCK:
            self._cache = _load_cache(self.cache_path, config=config)
            if self._cache.pop("_needs_rewrite", False):
                _save_cache(self.cache_path, self._cache, config=config)
        _restore_provider_circuits(self._circuit_scope, self._cache, self.sources)
        self._kisssub_items: list[MikanRelease] | None = None

    def begin_cycle(self) -> None:
        self.lookup_count = 0
        self._kisssub_items = None

    def search(
        self,
        bangumi_id: int,
        mappings: list[dict[str, object]],
        episodes: set[int],
    ) -> FallbackSearchResult:
        if not self.enabled:
            return FallbackSearchResult(
                conclusive=False,
                deferred_reason="fallback_disabled",
            )
        if not episodes:
            return FallbackSearchResult(
                conclusive=False,
                deferred_reason="no_episodes_requested",
            )
        terms = _search_terms(mappings)
        if not terms:
            return FallbackSearchResult(
                conclusive=False,
                deferred_reason="no_search_terms",
            )
        if not self.sources:
            return FallbackSearchResult(
                conclusive=False,
                deferred_reason="no_providers_configured",
            )
        aliases = _series_aliases(mappings, terms)
        key = _cache_key(bangumi_id, terms, self.sources, episodes)
        with _FALLBACK_CACHE_LOCK:
            cached = self._cached_releases(key, bangumi_id)
        if cached is not None:
            cached[:] = _filter_releases(cached, aliases, episodes, self.min_nyaa_seeders)
            return cached
        if self.lookup_count >= self.max_lookups:
            self.logger.info(
                "Mikan fallback source lookup budget exhausted. bangumi_id=%s episodes=%s lookups=%s max=%s",
                bangumi_id,
                sorted(episodes),
                self.lookup_count,
                self.max_lookups,
            )
            return FallbackSearchResult(
                conclusive=False,
                deferred_reason="lookup_budget_exhausted",
            )

        primary_term, latin_term = terms
        releases: list[MikanRelease] = []
        successful_sources: list[str] = []
        failed_sources: list[str] = []
        skipped_sources: list[str] = []
        now_epoch = time.time()
        claimed_sources: list[str] = []
        for source in self.sources:
            if _claim_provider_circuit(self._circuit_scope, source, now_epoch):
                claimed_sources.append(source)
            else:
                skipped_sources.append(source)
        if not claimed_sources:
            return FallbackSearchResult(
                conclusive=False,
                deferred_reason="all_providers_unavailable",
                skipped_sources=tuple(skipped_sources),
            )

        self.lookup_count += 1
        circuit_dirty = False
        with ThreadPoolExecutor(max_workers=max(1, min(6, len(claimed_sources)))) as executor:
            source_results = [
                (
                    source,
                    executor.submit(self._fetch_source, source, bangumi_id, primary_term, latin_term),
                )
                for source in claimed_sources
            ]
            for source, future in source_results:
                try:
                    releases.extend(future.result())
                    successful_sources.append(source)
                    changed, closed = _record_provider_success(self._circuit_scope, source)
                    circuit_dirty = circuit_dirty or changed
                    if closed:
                        self.logger.warning(
                            "Mikan fallback source circuit closed after successful probe. source=%s",
                            source,
                        )
                except (requests.RequestException, ET.ParseError, ValueError, TypeError) as exc:
                    failed_sources.append(source)
                    opened, failure_count = _record_provider_failure(
                        self._circuit_scope,
                        source,
                        threshold=self.failure_threshold,
                        cooldown_seconds=self.source_cooldown_seconds,
                    )
                    circuit_dirty = True
                    self.logger.warning(
                        "Mikan fallback source failed. source=%s bangumi_id=%s error=%s",
                        source,
                        bangumi_id,
                        exc,
                    )
                    if opened:
                        self.logger.warning(
                            "Mikan fallback source circuit opened. source=%s failures=%s cooldown=%ss",
                            source,
                            failure_count,
                            self.source_cooldown_seconds,
                        )
        if circuit_dirty:
            self._persist_provider_circuits()
        releases = _deduplicate_releases(releases)
        filtered = _filter_releases(releases, aliases, episodes, self.min_nyaa_seeders)
        cache_releases = _cache_release_subset(filtered)
        conclusive = bool(
            len(successful_sources) == len(self.sources)
            and not failed_sources
            and not skipped_sources
        )
        if conclusive:
            deferred_reason = ""
        elif successful_sources:
            deferred_reason = "partial_provider_coverage"
        else:
            deferred_reason = "all_providers_failed"
        with _FALLBACK_CACHE_LOCK:
            self._cache["entries"][key] = {
                "fetched_at": time.time(),
                "complete": conclusive,
                "deferred_reason": deferred_reason,
                "successful_sources": successful_sources,
                "failed_sources": failed_sources,
                "skipped_sources": skipped_sources,
                "releases": [_release_payload(release) for release in cache_releases],
            }
            _prune_cache_entries(self._cache, FALLBACK_CACHE_MAX_ENTRIES)
            _save_cache(self.cache_path, self._cache, config=self.config)
        self.logger.warning(
            "Mikan fallback source search complete. bangumi_id=%s episodes=%s sources=%s "
            "succeeded=%s failed=%s skipped=%s fetched=%s matched=%s",
            bangumi_id,
            sorted(episodes),
            ",".join(self.sources),
            ",".join(successful_sources) or "-",
            ",".join(failed_sources) or "-",
            ",".join(skipped_sources) or "-",
            len(releases),
            len(filtered),
        )
        return FallbackSearchResult(
            filtered,
            conclusive=conclusive,
            lookup_performed=True,
            deferred_reason=deferred_reason,
            successful_sources=tuple(successful_sources),
            failed_sources=tuple(failed_sources),
            skipped_sources=tuple(skipped_sources),
        )

    def _persist_provider_circuits(self) -> None:
        circuit_payload = _provider_circuit_payload(self._circuit_scope, self.sources)
        with _FALLBACK_CACHE_LOCK:
            latest = _load_cache(self.cache_path, config=self.config)
            entries = latest.setdefault("entries", {})
            if circuit_payload:
                entries[PROVIDER_CIRCUIT_CACHE_KEY] = {
                    "fetched_at": time.time(),
                    "circuits": circuit_payload,
                }
            else:
                entries.pop(PROVIDER_CIRCUIT_CACHE_KEY, None)
            _prune_cache_entries(latest, FALLBACK_CACHE_MAX_ENTRIES)
            _save_cache(self.cache_path, latest, config=self.config)
            self._cache = latest

    def _cached_releases(self, key: str, bangumi_id: int) -> FallbackSearchResult | None:
        entry = self._cache.get("entries", {}).get(key)
        if not isinstance(entry, dict):
            return None
        try:
            age = time.time() - float(entry.get("fetched_at") or 0)
        except (TypeError, ValueError):
            return None
        cache_ttl = self.cache_ttl_seconds if entry.get("complete", True) else min(self.cache_ttl_seconds, 600)
        if age < 0 or age > cache_ttl:
            return None
        payloads = entry.get("releases")
        if not isinstance(payloads, list):
            return None
        conclusive = bool(entry.get("complete", True))
        deferred_reason = str(entry.get("deferred_reason") or "")
        if not conclusive and not deferred_reason:
            deferred_reason = "incomplete_cached_search"
        return FallbackSearchResult(
            [_release_from_payload(payload, bangumi_id) for payload in payloads if isinstance(payload, dict)],
            conclusive=conclusive,
            cache_hit=True,
            deferred_reason=deferred_reason,
            successful_sources=_cache_source_tuple(entry.get("successful_sources")),
            failed_sources=_cache_source_tuple(entry.get("failed_sources")),
            skipped_sources=_cache_source_tuple(entry.get("skipped_sources")),
        )

    def _fetch_source(
        self,
        source: str,
        bangumi_id: int,
        primary_term: str,
        latin_term: str,
    ) -> list[MikanRelease]:
        if source == "animegarden":
            response = self.session.get(
                "https://animes.garden/api/resources",
                params={"search": primary_term, "pageSize": 100},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return _parse_animegarden(response.json(), bangumi_id)
        if source == "dmhy":
            return self._fetch_rss(
                "https://dmhy.org/topics/rss/rss.xml",
                bangumi_id,
                source,
                params={"keyword": primary_term},
            )
        if source == "acgrip":
            return self._fetch_rss(
                "https://acg.rip/.xml",
                bangumi_id,
                source,
                params={"term": primary_term},
            )
        if source == "bangumimoe":
            return self._fetch_rss(
                f"https://bangumi.moe/rss/search/{quote(primary_term, safe='')}",
                bangumi_id,
                source,
            )
        if source == "kisssub":
            if self._kisssub_items is None:
                self._kisssub_items = self._fetch_rss(
                    "https://kisssub.org/rss.xml",
                    bangumi_id,
                    source,
                )
            return [
                _replace_bangumi_id(release, bangumi_id)
                for release in self._kisssub_items
            ]
        if source == "nyaa":
            return self._fetch_rss(
                "https://nyaa.si/",
                bangumi_id,
                source,
                params={"page": "rss", "q": latin_term or primary_term, "c": "1_0", "f": "0"},
            )
        return []

    def _fetch_rss(
        self,
        url: str,
        bangumi_id: int,
        source: str,
        *,
        params: dict[str, str] | None = None,
    ) -> list[MikanRelease]:
        response = self.session.get(url, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        return _parse_rss(response.content, bangumi_id, source)


def _provider_circuit_key(scope: str, source: str) -> tuple[str, str]:
    return str(scope), str(source).casefold()


def _claim_provider_circuit(scope: str, source: str, now: float) -> bool:
    key = _provider_circuit_key(scope, source)
    with _PROVIDER_CIRCUIT_LOCK:
        state = _PROVIDER_CIRCUITS.get(key)
        if state is None:
            return True
        if state.open_until > now:
            return False
        if state.open_until > 0:
            if state.half_open_in_flight:
                return False
            state.half_open_in_flight = True
            return True
        return not state.half_open_in_flight


def _record_provider_success(scope: str, source: str) -> tuple[bool, bool]:
    key = _provider_circuit_key(scope, source)
    with _PROVIDER_CIRCUIT_LOCK:
        state = _PROVIDER_CIRCUITS.pop(key, None)
    return (
        state is not None,
        bool(state is not None and (state.open_until > 0 or state.half_open_in_flight)),
    )


def _record_provider_failure(
    scope: str,
    source: str,
    *,
    threshold: int,
    cooldown_seconds: int,
) -> tuple[bool, int]:
    key = _provider_circuit_key(scope, source)
    now = time.time()
    with _PROVIDER_CIRCUIT_LOCK:
        state = _PROVIDER_CIRCUITS.setdefault(key, _ProviderCircuitState())
        failure_count = min(max(1, int(threshold)), state.consecutive_failures + 1)
        should_open = state.half_open_in_flight or failure_count >= max(1, int(threshold))
        state.consecutive_failures = failure_count
        state.half_open_in_flight = False
        if should_open:
            state.open_until = now + max(1, int(cooldown_seconds))
        return should_open, failure_count


def _restore_provider_circuits(
    scope: str,
    cache: dict[str, Any],
    sources: tuple[str, ...],
) -> None:
    entries = cache.get("entries")
    stored = entries.get(PROVIDER_CIRCUIT_CACHE_KEY) if isinstance(entries, dict) else None
    circuits = stored.get("circuits") if isinstance(stored, dict) else None
    if not isinstance(circuits, dict):
        return
    configured = {str(source).casefold() for source in sources}
    with _PROVIDER_CIRCUIT_LOCK:
        for source, payload in circuits.items():
            normalized_source = str(source).casefold()
            if normalized_source not in configured or not isinstance(payload, dict):
                continue
            try:
                failures = max(0, int(payload.get("consecutive_failures") or 0))
                open_until = max(0.0, float(payload.get("open_until") or 0.0))
            except (TypeError, ValueError):
                continue
            if failures <= 0 and open_until <= 0:
                continue
            key = _provider_circuit_key(scope, normalized_source)
            if key in _PROVIDER_CIRCUITS:
                continue
            _PROVIDER_CIRCUITS[key] = _ProviderCircuitState(
                consecutive_failures=failures,
                open_until=open_until,
                # An in-flight probe belongs to the old process and must never
                # survive a restart.
                half_open_in_flight=False,
            )


def _provider_circuit_payload(
    scope: str,
    sources: tuple[str, ...],
) -> dict[str, dict[str, float | int]]:
    configured = {str(source).casefold() for source in sources}
    with _PROVIDER_CIRCUIT_LOCK:
        result = {
            source: {
                "consecutive_failures": int(state.consecutive_failures),
                "open_until": float(state.open_until),
            }
            for (state_scope, source), state in _PROVIDER_CIRCUITS.items()
            if state_scope == scope
            and source in configured
            and (state.consecutive_failures > 0 or state.open_until > 0)
        }
    return result


def _cache_source_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item))


def _parse_animegarden(payload: dict[str, Any], bangumi_id: int) -> list[MikanRelease]:
    resources = payload.get("resources")
    if not isinstance(resources, list):
        return []
    releases: list[MikanRelease] = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        download_url = str(item.get("magnet") or "")
        if not title or not download_url:
            continue
        episodes = extract_episode_numbers(title)
        releases.append(
            MikanRelease(
                bangumi_id=bangumi_id,
                title=title,
                episode=episodes[0] if episodes else None,
                episodes=episodes,
                torrent_url=download_url,
                pub_date=_parse_datetime(item.get("createdAt")),
                content_length=_safe_int(item.get("size")),
                link=str(item.get("href") or "") or None,
                source=f"animegarden:{item.get('provider') or 'unknown'}",
                info_hash=extract_torrent_info_hash(download_url),
            )
        )
    return releases


def _parse_rss(content: bytes | str, bangumi_id: int, source: str) -> list[MikanRelease]:
    root = ET.fromstring(content)
    releases: list[MikanRelease] = []
    for item in root.findall("./channel/item"):
        fields: dict[str, str] = {}
        enclosure_url = ""
        for child in list(item):
            name = child.tag.rsplit("}", 1)[-1]
            if name == "enclosure":
                enclosure_url = str(child.attrib.get("url") or "")
            else:
                fields[name] = str(child.text or "")
        title = fields.get("title", "").strip()
        download_url = enclosure_url or fields.get("link", "").strip()
        if not title or not download_url:
            continue
        episodes = extract_episode_numbers(title)
        info_hash = fields.get("infoHash") or extract_torrent_info_hash(download_url)
        releases.append(
            MikanRelease(
                bangumi_id=bangumi_id,
                title=title,
                episode=episodes[0] if episodes else None,
                episodes=episodes,
                torrent_url=download_url,
                pub_date=_parse_datetime(fields.get("pubDate")),
                content_length=_parse_size(fields.get("size")) or _safe_int(fields.get("contentLength")),
                link=fields.get("guid") or fields.get("link") or None,
                source=source,
                info_hash=str(info_hash).casefold() if info_hash else None,
                seeders=_safe_int(fields.get("seeders")),
            )
        )
    return releases


def _filter_releases(
    releases: list[MikanRelease],
    aliases: list[str],
    episodes: set[int],
    min_nyaa_seeders: int,
) -> list[MikanRelease]:
    normalized_aliases = [normalize_match_text(alias) for alias in aliases]
    normalized_aliases = [alias for alias in normalized_aliases if len(alias) >= 4]
    result: list[MikanRelease] = []
    for release in releases:
        normalized_title = normalize_match_text(release.title)
        if normalized_aliases and not any(alias in normalized_title for alias in normalized_aliases):
            continue
        covered = set(release.episodes or (() if release.episode is None else (release.episode,)))
        if not covered.intersection(episodes):
            continue
        if _volume_only_title(release.title):
            continue
        if has_english_only_subtitle_hint(release.title):
            continue
        if release.source == "nyaa" and release.seeders is not None and release.seeders < min_nyaa_seeders:
            continue
        result.append(release)
    return _deduplicate_releases(result)


def _volume_only_title(title: str) -> bool:
    return bool(VOLUME_ONLY_RE.search(title)) and not EXPLICIT_EPISODE_RE.search(title)


def _search_terms(mappings: list[dict[str, object]]) -> tuple[str, str] | None:
    titles: list[str] = []
    latin: list[str] = []
    for mapping in mappings:
        for key in ("title", "bangumi_title", "name"):
            value = str(mapping.get(key) or "").strip()
            if value:
                titles.append(value)
        path_name = Path(str(mapping.get("path") or "")).name.strip()
        if path_name:
            titles.append(path_name)
        matches = mapping.get("match")
        if isinstance(matches, list):
            titles.extend(str(value).strip() for value in matches if str(value).strip())
    for value in titles:
        if re.search(r"[A-Za-z]{3}", value) and len(value) <= 100:
            latin.append(value)
    primary = next((value for value in titles if 3 <= len(value) <= 100), "")
    latin_term = next((value for value in latin if value != primary), "") or primary
    return (primary, latin_term) if primary else None


def _series_aliases(mappings: list[dict[str, object]], terms: tuple[str, str]) -> list[str]:
    aliases = [terms[0], terms[1]]
    for mapping in mappings:
        matches = mapping.get("match")
        if isinstance(matches, list):
            aliases.extend(str(value) for value in matches[:12])
        aliases.extend(str(mapping.get(key) or "") for key in ("title", "bangumi_title", "name"))
    return [alias for alias in aliases if alias.strip()]


def _deduplicate_releases(releases: list[MikanRelease]) -> list[MikanRelease]:
    result: list[MikanRelease] = []
    seen: set[str] = set()
    for release in releases:
        key = release.info_hash or release.torrent_url.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(release)
    return result


def _replace_bangumi_id(release: MikanRelease, bangumi_id: int) -> MikanRelease:
    return MikanRelease(
        bangumi_id=bangumi_id,
        title=release.title,
        episode=release.episode,
        episodes=release.episodes,
        torrent_url=release.torrent_url,
        pub_date=release.pub_date,
        content_length=release.content_length,
        link=release.link,
        source=release.source,
        info_hash=release.info_hash,
        seeders=release.seeders,
        season_number=release.season_number,
        series_identity=release.series_identity,
        identity_evidence=release.identity_evidence,
    )


def _cache_key(
    bangumi_id: int,
    terms: tuple[str, str],
    sources: tuple[str, ...],
    episodes: set[int],
) -> str:
    raw = json.dumps(
        [FALLBACK_CACHE_VERSION, bangumi_id, terms, sources, sorted(episodes)],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _resolve_cache_path(config: Any) -> Path:
    raw = str(getattr(config, "mikan_fallback_cache_path", "mikan_fallback_sources.json") or "")
    path = Path(raw)
    return path if path.is_absolute() else Path(config.work_path) / path


def _load_cache(path: Path, *, config: Any | None = None) -> dict[str, Any]:
    if config is not None and sqlite_cache_enabled(config):
        store = MikanIndexedCache(
            config,
            namespace="fallback_sources",
            legacy_path=path,
            schema_version=FALLBACK_CACHE_VERSION,
        )
        if not store.initialized():
            legacy = _load_cache_json(path)
            entries = legacy.get("entries") if isinstance(legacy.get("entries"), dict) else {}
            store.initialize_if_needed(entries)
        result = {
            "version": FALLBACK_CACHE_VERSION,
            "entries": store.load_all(),
        }
        _prune_cache_entries(result, FALLBACK_CACHE_MAX_ENTRIES)
        return result
    return _load_cache_json(path)


def _load_cache_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict) or payload.get("version") != FALLBACK_CACHE_VERSION:
        return {"version": FALLBACK_CACHE_VERSION, "entries": {}, "_needs_rewrite": path.exists()}
    entries = payload.get("entries")
    result = {
        "version": FALLBACK_CACHE_VERSION,
        "entries": entries if isinstance(entries, dict) else {},
    }
    _prune_cache_entries(result, FALLBACK_CACHE_MAX_ENTRIES)
    return result


def _save_cache(path: Path, payload: dict[str, Any], *, config: Any | None = None) -> None:
    if config is not None and sqlite_cache_enabled(config):
        entries = payload.get("entries") if isinstance(payload.get("entries"), dict) else {}
        store = MikanIndexedCache(
            config,
            namespace="fallback_sources",
            legacy_path=path,
            schema_version=FALLBACK_CACHE_VERSION,
        )
        store.initialize_if_needed({})
        store.replace_all(entries)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def _cache_release_subset(releases: list[MikanRelease]) -> list[MikanRelease]:
    if len(releases) <= FALLBACK_CACHE_MAX_RELEASES_PER_ENTRY:
        return releases
    return sorted(
        releases,
        key=lambda release: (
            release.pub_date or datetime.min.replace(tzinfo=timezone.utc),
            int(release.seeders or 0),
        ),
        reverse=True,
    )[:FALLBACK_CACHE_MAX_RELEASES_PER_ENTRY]


def _prune_cache_entries(payload: dict[str, Any], max_entries: int) -> None:
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return
    reserved = {
        key: value
        for key, value in entries.items()
        if key == PROVIDER_CIRCUIT_CACHE_KEY
    }
    searchable = [
        (key, value)
        for key, value in entries.items()
        if key != PROVIDER_CIRCUIT_CACHE_KEY
    ]
    if len(searchable) <= max_entries:
        return
    newest = sorted(
        searchable,
        key=lambda item: _cache_entry_fetched_at(item[1]),
        reverse=True,
    )[:max_entries]
    payload["entries"] = {**reserved, **dict(newest)}


def _cache_entry_fetched_at(entry: object) -> float:
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _release_payload(release: MikanRelease) -> dict[str, Any]:
    return {
        "title": release.title,
        "episode": release.episode,
        "episodes": list(release.episodes),
        "torrent_url": release.torrent_url,
        "pub_date": release.pub_date.isoformat() if release.pub_date else None,
        "content_length": release.content_length,
        "link": release.link,
        "source": release.source,
        "info_hash": release.info_hash,
        "seeders": release.seeders,
        "season_number": release.season_number,
        "series_identity": release.series_identity,
        "identity_evidence": list(release.identity_evidence),
    }


def _release_from_payload(payload: dict[str, Any], bangumi_id: int) -> MikanRelease:
    episodes = tuple(int(value) for value in payload.get("episodes", []) if str(value).isdigit())
    return MikanRelease(
        bangumi_id=bangumi_id,
        title=str(payload.get("title") or ""),
        episode=_safe_int(payload.get("episode")),
        episodes=episodes,
        torrent_url=str(payload.get("torrent_url") or ""),
        pub_date=_parse_datetime(payload.get("pub_date")),
        content_length=_safe_int(payload.get("content_length")),
        link=str(payload.get("link") or "") or None,
        source=str(payload.get("source") or "fallback"),
        info_hash=str(payload.get("info_hash") or "") or None,
        seeders=_safe_int(payload.get("seeders")),
        season_number=_safe_int(payload.get("season_number")),
        series_identity=str(payload.get("series_identity") or ""),
        identity_evidence=tuple(
            str(value)
            for value in payload.get("identity_evidence", [])
            if str(value)
        ) if isinstance(payload.get("identity_evidence"), list) else (),
    )


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_size(value: object) -> int | None:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)\s*", str(value or ""), re.I)
    if not match:
        return None
    units = {"B": 1, "KB": 1000, "KIB": 1024, "MB": 1000**2, "MIB": 1024**2, "GB": 1000**3, "GIB": 1024**3, "TB": 1000**4, "TIB": 1024**4}
    return int(float(match.group(1)) * units[match.group(2).upper()])


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
