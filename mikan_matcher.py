from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from html import unescape
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from typing import Callable
import unicodedata
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests

from config import AppConfig
from local_catalog import LocalSeries, discover_local_series
from mikan_cache_store import MikanIndexedCache, sqlite_cache_enabled
from mikan_source import MikanSourceDeadline, MikanSourceError, fetch_bangumi_releases, release_season_number, release_series_identity
from series_metadata import SeriesMetadataStore


_OPENCC_T2S = None
_OPENCC_T2S_UNAVAILABLE = False
_AUTO_MATCH_MATCHER_VERSION = 2
_AUTO_MATCH_MIN_MARGIN = 0.08
_AUTO_MATCH_CURSOR_KEY = "__auto_match_cursor_v1__"


@dataclass(frozen=True)
class MikanSearchCandidate:
    bangumi_id: int
    title: str
    source_query: str


class _AutoMatchLookupDeferred(MikanSourceDeadline):
    """Some external lookup failed; partial evidence cannot decide identity."""


def resolve_mikan_series_mappings(
    config: AppConfig,
    logger: logging.Logger,
    *,
    cached_only: bool = False,
    deadline_monotonic: float | None = None,
) -> list[dict[str, object]]:
    mappings: list[dict[str, object]] = []
    for configured in config.mikan_series_path_mappings:
        mapping = dict(configured)
        mapping["match"] = _unique_tokens(
            [str(token) for token in mapping.get("match", [])]
            if isinstance(mapping.get("match"), list)
            else []
        )
        mapping.setdefault("identity_source", "manual")
        mapping.setdefault("match_confidence", 1.0)
        mapping.setdefault("locked", True)
        mappings.append(mapping)
    metadata_mappings = _series_metadata_mappings(config, logger)
    mappings.extend(metadata_mappings)
    if not config.mikan_auto_match_enabled:
        return mappings

    protected_paths = {
        _mapping_path_key(mapping)
        for mapping in mappings
        if "path" in mapping and _mapping_is_protected(mapping)
    }
    cache_path = _resolve_cache_path(config)
    cache = _load_cache(cache_path, config=config)
    mappings = _suppress_invalidated_unlocked_metadata(mappings, cache, config)
    mappings.extend(_season_scoped_cached_mappings(cache, metadata_mappings, config))
    if cached_only:
        cached_mappings = _cached_mappings_from_cache(cache, config, protected_paths)
        if cached_mappings:
            logger.info("Mikan auto-match cached-only mappings loaded: count=%s", len(cached_mappings))
        for cached_mapping in cached_mappings:
            mappings = _replace_unlocked_metadata_mapping(mappings, cached_mapping)
        return _deduplicate_mappings(mappings)

    matched_cache_hits = 0
    miss_cache_hits = 0
    lookup_count = 0
    new_matches = 0
    new_misses = 0
    deferred_lookups = 0
    cache_changed = False
    max_lookups = int(getattr(config, "mikan_auto_match_max_lookups_per_cycle", 25) or 0)

    if deadline_monotonic is None:
        local_series = discover_local_series(config)
    else:
        # The normal watcher already has scanner/metadata-sync indexes.  Cold
        # online matching must not rediscover the whole media tree here.
        local_series = []
        with SeriesMetadataStore.from_config(config) as store:
            offset = 0
            while time.monotonic() < deadline_monotonic:
                profiles = store.list_profiles(limit=1000, offset=offset)
                local_series.extend(LocalSeries(
                    Path(profile.local_path),
                    _unique_tokens([*profile.aliases, *profile.titles, profile.canonical_title]),
                    profile.premiered_year, profile.anidb_id or None,
                ) for profile in profiles)
                if len(profiles) < 1000:
                    break
                offset += len(profiles)
        cursor = cache.get(_AUTO_MATCH_CURSOR_KEY, {})
        next_path = cursor.get("next_path") if isinstance(cursor, dict) else None
        paths = [str(series.path) for series in local_series]
        start = paths.index(next_path) if next_path in paths else 0
        local_series = local_series[start:] + local_series[:start]

    for series_index, series in enumerate(local_series):
        series_key = str(series.path.resolve())
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
        if series_key.casefold() in protected_paths:
            continue

        cached = cache.get(series_key)
        if _valid_cached_mapping(cached, config):
            cached_mapping = _cached_mapping_from_entry(cached)
            mappings = _replace_unlocked_metadata_mapping(mappings, cached_mapping)
            matched_cache_hits += 1
            continue
        if _valid_cached_miss(cached, config):
            miss_cache_hits += 1
            continue

        if max_lookups > 0 and lookup_count >= max_lookups:
            deferred_lookups += 1
            continue

        lookup_count += 1
        progress: dict[str, object] | None = None
        checkpoint = None
        if deadline_monotonic is not None:
            identity = hashlib.sha256(json.dumps([
                _AUTO_MATCH_MATCHER_VERSION, series_key, series.aliases, series.premiered_year,
                series.anidb_id, config.mikan_base_url, config.mikan_auto_match_threshold,
                config.mikan_auto_match_max_candidates,
            ], ensure_ascii=True, sort_keys=True).encode()).hexdigest()
            progress = dict(cached.get("progress", {})) if isinstance(cached, dict) and cached.get("progress_identity") == identity else {}

            def checkpoint() -> None:
                cache[series_key] = {"status": "deferred", "reason": "elapsed_budget_exhausted",
                    "matcher_version": _AUTO_MATCH_MATCHER_VERSION, "progress_identity": identity,
                    "progress": progress}
                cache[_AUTO_MATCH_CURSOR_KEY] = {"next_path": str(local_series[(series_index + 1) % len(local_series)].path)}
                _save_cache(cache_path, cache, config=config)

        try:
            result = _auto_match_series(series, config, logger) if deadline_monotonic is None else _auto_match_series(
                series, config, logger, deadline_monotonic=deadline_monotonic,
                progress=progress, checkpoint=checkpoint,
            )
        except MikanSourceDeadline as exc:
            if checkpoint is not None:
                checkpoint()
            if isinstance(exc, _AutoMatchLookupDeferred):
                cache[series_key]["reason"] = "source_lookup_failed"
                _save_cache(cache_path, cache, config=config)
                logger.info("Mikan auto-match retained incomplete source evidence for retry. path=%s", series.path)
                continue
            logger.info("Mikan auto-match yielded with persistent partial evidence. path=%s", series.path)
            break
        cache[series_key] = result
        cache_changed = True
        if deadline_monotonic is not None:
            cache[_AUTO_MATCH_CURSOR_KEY] = {"next_path": str(local_series[(series_index + 1) % len(local_series)].path)}
            _save_cache(cache_path, cache, config=config)
        if result.get("status") != "matched":
            if _cache_miss_invalidates_unlocked_metadata(result):
                mappings = _remove_unlocked_metadata_mapping(mappings, series_key)
            new_misses += 1
            continue
        matched_mapping = dict(result["mapping"])
        mappings = _replace_unlocked_metadata_mapping(mappings, matched_mapping)
        new_matches += 1

    if cache_changed:
        _save_cache(cache_path, cache, config=config)
    if matched_cache_hits or miss_cache_hits or lookup_count:
        logger.info(
            "Mikan auto-match cache summary matched_hits=%s miss_hits=%s lookups=%s new_matches=%s new_misses=%s deferred=%s",
            matched_cache_hits,
            miss_cache_hits,
            lookup_count,
            new_matches,
            new_misses,
            deferred_lookups,
        )
    return _deduplicate_mappings(mappings)


def _cached_mappings_from_cache(
    cache: dict[str, object],
    config: AppConfig,
    protected_paths: set[str],
) -> list[dict[str, object]]:
    mappings: list[dict[str, object]] = []
    for series_key, cached in cache.items():
        if not _valid_cached_mapping(cached, config):
            continue
        mapping = _cached_mapping_from_entry(cached)
        path = Path(str(mapping.get("path", "")))
        try:
            resolved_key = str(path.resolve()).casefold()
        except OSError:
            resolved_key = str(path).casefold()
        if resolved_key in protected_paths:
            continue
        if not path.exists():
            continue
        mappings.append(dict(mapping))
    return mappings


def _cached_mapping_from_entry(cached: dict[str, object]) -> dict[str, object]:
    mapping = dict(cached["mapping"])
    if cached.get("title") and not mapping.get("title"):
        mapping["title"] = str(cached["title"])
    confidence = cached.get("confidence")
    if isinstance(confidence, int | float):
        mapping.setdefault("match_confidence", float(confidence))
    mapping.setdefault("identity_source", "auto_match_cache")
    mapping["match"] = _unique_tokens(
        [str(token) for token in mapping.get("match", [])]
        if isinstance(mapping.get("match"), list)
        else []
    )
    return mapping


def mapping_matches_torrent(torrent_name: str, mapping: dict[str, object]) -> bool:
    tokens = mapping.get("match") or []
    if not isinstance(tokens, list):
        return False
    normalized_torrent = normalize_match_text(torrent_name)
    return any(
        normalized_token in normalized_torrent
        for token in tokens
        if (normalized_token := _semantic_match_token(str(token)))
    )


def normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _to_simplified_chinese(value)).casefold()
    return "".join(char for char in normalized if char.isalnum() or _is_cjk_or_kana(char))


def _auto_match_series(
    series: LocalSeries,
    config: AppConfig,
    logger: logging.Logger,
    *,
    deadline_monotonic: float | None = None,
    progress: dict[str, object] | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, object]:
    candidates = _search_candidates_for_series(series, config) if progress is None else _search_candidates_for_series(
        series, config, deadline_monotonic=deadline_monotonic, progress=progress, checkpoint=checkpoint,
    )
    if not candidates:
        return _miss_cache_entry(series, "no_candidates")

    scored: list[tuple[float, MikanSearchCandidate, list[str]]] = [] if progress is None else [
        (float(row["confidence"]), MikanSearchCandidate(**row["candidate"]), list(row["tokens"]))
        for row in progress.get("scored", [])
    ]
    start = 0 if progress is None else int(progress.get("next_candidate", 0))
    completed = set() if progress is None else set(progress.get("completed_candidates", range(start)))
    lookup_failed = False
    for index, candidate in enumerate(candidates[start:], start=start):
        if index in completed:
            continue
        try:
            releases = fetch_bangumi_releases(
                config.mikan_base_url,
                candidate.bangumi_id,
                timeout_seconds=config.mikan_request_timeout_seconds,
                deadline_monotonic=deadline_monotonic,
            )
        except MikanSourceDeadline:
            raise
        except (requests.RequestException, MikanSourceError) as exc:
            logger.warning("Mikan auto-match failed to fetch RSS bangumi_id=%s: %s", candidate.bangumi_id, exc)
            lookup_failed = True
            continue

        release_titles = [release.title for release in releases[:20]]
        confidence = _candidate_confidence(series, candidate, release_titles)
        match_tokens = _match_tokens(series, candidate, release_titles)
        scored.append((confidence, candidate, match_tokens))
        if progress is not None:
            completed.add(index)
            progress["completed_candidates"] = sorted(completed)
            progress["next_candidate"] = next((item for item in range(len(candidates)) if item not in completed), len(candidates))
            progress["scored"] = [{"confidence": score,
                "candidate": {"bangumi_id": item.bangumi_id, "title": item.title, "source_query": item.source_query},
                "tokens": tokens} for score, item, tokens in scored]
            if checkpoint is not None:
                checkpoint()

    if progress is not None and lookup_failed:
        raise _AutoMatchLookupDeferred("candidate RSS evidence incomplete")

    if not scored:
        return _miss_cache_entry(series, "no_scored_candidates")

    scored.sort(key=lambda item: item[0], reverse=True)
    best_confidence, best_candidate, best_tokens = scored[0]
    if best_confidence < config.mikan_auto_match_threshold:
        return _miss_cache_entry(
            series,
            "below_threshold",
            confidence=best_confidence,
            best_candidate=best_candidate,
        )
    runner_up = scored[1] if len(scored) > 1 else None
    if runner_up is not None and best_confidence - runner_up[0] < _AUTO_MATCH_MIN_MARGIN:
        return _miss_cache_entry(
            series,
            "ambiguous_candidates",
            confidence=best_confidence,
            best_candidate=best_candidate,
            runner_up_candidate=runner_up[1],
            margin=best_confidence - runner_up[0],
        )

    logger.info(
        "Mikan auto-matched bangumi_id=%s confidence=%.3f title=%s path=%s",
        best_candidate.bangumi_id,
        best_confidence,
        best_candidate.title,
        series.path,
    )
    try:
        with SeriesMetadataStore.from_config(config) as store:
            store.set_mikan_identity(
                series.path,
                bangumi_id=best_candidate.bangumi_id,
                title=best_candidate.title,
                confidence=best_confidence,
                aliases=series.aliases,
                premiered_year=series.premiered_year,
                anidb_id=series.anidb_id or "",
            )
    except Exception as exc:  # noqa: BLE001 - matching must continue if metadata persistence is unavailable.
        logger.debug("Unable to persist Mikan series identity path=%s error=%s", series.path, exc)
    return {
        "status": "matched",
        "matcher_version": _AUTO_MATCH_MATCHER_VERSION,
        "confidence": best_confidence,
        "title": best_candidate.title,
        "mapping": {
            "bangumi_id": best_candidate.bangumi_id,
            "path": str(series.path),
            "match": best_tokens,
            "title": best_candidate.title,
            "match_confidence": best_confidence,
            "identity_source": "auto_match",
            "locked": False,
        },
    }


def _series_metadata_mappings(config: AppConfig, logger: logging.Logger) -> list[dict[str, object]]:
    try:
        with SeriesMetadataStore.from_config(config) as store:
            profiles = []
            offset = 0
            while True:
                batch = store.list_profiles(limit=1000, offset=offset)
                profiles.extend(batch)
                if len(batch) < 1000:
                    break
                offset += len(batch)
    except Exception as exc:  # noqa: BLE001 - this cache is an optional accelerator.
        logger.debug("Unable to load series metadata mappings: %s", exc)
        return []
    result: list[dict[str, object]] = []
    for profile in profiles:
        if profile.mikan_bangumi_id is None:
            continue
        path = Path(profile.local_path)
        if not path.exists():
            continue
        tokens = _unique_tokens([*profile.aliases, *profile.titles, profile.canonical_title])
        result.append(
            {
                "bangumi_id": profile.mikan_bangumi_id,
                "path": profile.local_path,
                "match": tokens[:12],
                "title": profile.canonical_title,
                "identity_source": (
                    "manual"
                    if str(profile.match_source or "").strip().casefold() == "manual"
                    else "series_metadata"
                ),
                "metadata_match_source": str(profile.match_source or ""),
                "match_confidence": float(profile.match_confidence or (1.0 if profile.locked else 0.0)),
                "locked": bool(profile.locked),
                "metadata_title": profile.canonical_title,
                "metadata_provider": str(profile.provider or ""),
                "metadata_provider_id": str(profile.provider_id or ""),
            }
        )
    return result


def _miss_cache_entry(
    series: LocalSeries,
    reason: str,
    *,
    confidence: float | None = None,
    best_candidate: MikanSearchCandidate | None = None,
    runner_up_candidate: MikanSearchCandidate | None = None,
    margin: float | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "miss",
        "matcher_version": _AUTO_MATCH_MATCHER_VERSION,
        "reason": reason,
        "path": str(series.path),
        "aliases": series.aliases[:8],
    }
    if confidence is not None:
        payload["confidence"] = confidence
    if best_candidate is not None:
        payload["best"] = {
            "bangumi_id": best_candidate.bangumi_id,
            "title": best_candidate.title,
            "source_query": best_candidate.source_query,
        }
    if runner_up_candidate is not None:
        payload["runner_up"] = {
            "bangumi_id": runner_up_candidate.bangumi_id,
            "title": runner_up_candidate.title,
            "source_query": runner_up_candidate.source_query,
        }
    if margin is not None:
        payload["margin"] = float(margin)
    return payload


def _search_candidates_for_series(
    series: LocalSeries, config: AppConfig, *, deadline_monotonic: float | None = None,
    progress: dict[str, object] | None = None, checkpoint: Callable[[], None] | None = None,
) -> list[MikanSearchCandidate]:
    candidates: dict[int, MikanSearchCandidate] = {} if progress is None else {
        int(item["bangumi_id"]): MikanSearchCandidate(**item) for item in progress.get("candidates", [])
    }
    aliases = _search_aliases(series)
    start = 0 if progress is None else int(progress.get("next_alias", 0))
    completed = set() if progress is None else set(progress.get("completed_aliases", range(start)))
    lookup_failed = False
    for index, alias in enumerate(aliases[start:], start=start):
        if index in completed:
            continue
        try:
            results = search_mikan_bangumi(
                config.mikan_base_url,
                alias,
                timeout_seconds=config.mikan_request_timeout_seconds,
                deadline_monotonic=deadline_monotonic,
            )
        except MikanSourceDeadline:
            raise
        except requests.RequestException:
            lookup_failed = True
            continue
        for result in results:
            candidates.setdefault(result.bangumi_id, result)
        if progress is not None:
            completed.add(index)
            progress["completed_aliases"] = sorted(completed)
            progress["next_alias"] = next((item for item in range(len(aliases)) if item not in completed), len(aliases))
            progress["candidates"] = [{"bangumi_id": item.bangumi_id, "title": item.title, "source_query": item.source_query}
                for item in candidates.values()]
            if checkpoint is not None:
                checkpoint()
    if progress is not None and lookup_failed:
        raise _AutoMatchLookupDeferred("alias search coverage incomplete")
    return list(candidates.values())[: config.mikan_auto_match_max_candidates]


def search_mikan_bangumi(base_url: str, query: str, timeout_seconds: int = 30, *, deadline_monotonic: float | None = None) -> list[MikanSearchCandidate]:
    if deadline_monotonic is not None:
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise MikanSourceDeadline("auto-match alias lookup reached scheduling deadline")
        timeout_seconds = min(timeout_seconds, remaining)
    try:
        response = requests.get(
        urljoin(base_url.rstrip("/") + "/", "Home/Search"),
        params={"searchstr": query},
        timeout=timeout_seconds,
        headers={"User-Agent": "Mozilla/5.0"},
        )
    except requests.RequestException as exc:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise MikanSourceDeadline("auto-match alias lookup reached scheduling deadline") from exc
        raise
    response.raise_for_status()
    return parse_mikan_search_results(response.text, query)


def parse_mikan_search_results(html: str, query: str) -> list[MikanSearchCandidate]:
    results: list[MikanSearchCandidate] = []
    seen: set[int] = set()
    pattern = re.compile(
        r"""<a[^>]+href=(?P<quote>["'])(?:https?://[^"']+)?(?P<href>/Home/Bangumi/(?P<id>\d+))(?P=quote)[^>]*>(?P<title>.*?)</a>""",
        re.S,
    )
    for match in pattern.finditer(html):
        bangumi_id = int(match.group("id"))
        if bangumi_id in seen:
            continue
        seen.add(bangumi_id)
        title = re.sub(r"<.*?>", "", match.group("title"))
        title = " ".join(unescape(title).split())
        results.append(MikanSearchCandidate(bangumi_id, title, query))
    return results


def _candidate_confidence(
    series: LocalSeries,
    candidate: MikanSearchCandidate,
    release_titles: list[str],
) -> float:
    candidate_texts = [candidate.title]
    for release_title in release_titles:
        release_aliases = _release_aliases(release_title)
        candidate_texts.extend(release_aliases or [release_title])
    best = 0.0
    for alias in series.aliases:
        alias_norm = normalize_match_text(alias)
        if len(alias_norm) < 4:
            continue
        for text in candidate_texts:
            text_norm = normalize_match_text(text)
            if not text_norm:
                continue
            best = max(best, _title_identity_similarity(alias_norm, text_norm))
    return min(best, 1.0)


def _title_identity_similarity(left: str, right: str) -> float:
    """Score title identity without treating a shared franchise stem as exact.

    Search results and release titles frequently contain a real show's title as
    a short substring of a different spin-off (for example ``BanG Dream!`` in
    ``ARGONAVIS from BanG Dream!``).  Length-aware containment keeps ordinary
    release noise useful while refusing to award those franchise-only matches
    the old unconditional 0.98 confidence.
    """

    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    if left in right or right in left:
        containment_ratio = min(len(left), len(right)) / max(len(left), len(right))
        if containment_ratio >= 0.82:
            return max(ratio, 0.97)
        if containment_ratio >= 0.68:
            return max(ratio, 0.90)
    return ratio


def _match_tokens(
    series: LocalSeries,
    candidate: MikanSearchCandidate,
    release_titles: list[str],
) -> list[str]:
    tokens: list[str] = [*series.aliases, candidate.title]
    for title in release_titles[:5]:
        tokens.extend(_release_aliases(title))
    return _unique_tokens(tokens)[:24]


def _release_aliases(title: str) -> list[str]:
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)
    title = re.sub(r"\s-\s\d{1,3}\s.*$", "", title)
    title = re.sub(r"\[[^\]]+\]", "", title)
    return [part.strip() for part in title.split("/") if len(part.strip()) >= 3]


def _search_aliases(series: LocalSeries) -> list[str]:
    aliases: list[str] = []
    for alias in series.aliases:
        aliases.append(alias)
        aliases.append(_to_simplified_chinese(alias))
    aliases = [alias for alias in aliases if len(normalize_match_text(alias)) >= 4]
    return _unique_tokens(sorted(aliases, key=len, reverse=True))[:8]


def _unique_tokens(tokens: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        cleaned = re.sub(r"\s+", " ", token).strip()
        key = _semantic_match_token(cleaned)
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _mapping_path_key(mapping: dict[str, object]) -> str:
    path = Path(str(mapping.get("path") or ""))
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path).casefold()


def _mapping_is_protected(mapping: dict[str, object]) -> bool:
    return bool(
        mapping.get("locked")
        or mapping.get("manual_locked")
        or str(mapping.get("identity_source") or "").strip().casefold() == "manual"
    )


def _remove_unlocked_metadata_mapping(
    mappings: list[dict[str, object]],
    path: str,
) -> list[dict[str, object]]:
    path_key = _mapping_path_key({"path": path})
    return [
        mapping
        for mapping in mappings
        if not (
            _mapping_path_key(mapping) == path_key
            and str(mapping.get("identity_source") or "").strip().casefold() == "series_metadata"
            and not _mapping_is_protected(mapping)
        )
    ]


def _replace_unlocked_metadata_mapping(
    mappings: list[dict[str, object]],
    replacement: dict[str, object],
) -> list[dict[str, object]]:
    return [
        *_remove_unlocked_metadata_mapping(mappings, str(replacement.get("path") or "")),
        replacement,
    ]


def _cache_miss_invalidates_unlocked_metadata(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("matcher_version") == _AUTO_MATCH_MATCHER_VERSION
        and value.get("status") == "miss"
        and value.get("reason") in {"ambiguous_candidates", "below_threshold"}
    )


def _suppress_invalidated_unlocked_metadata(
    mappings: list[dict[str, object]],
    cache: dict[str, object],
    config: AppConfig,
) -> list[dict[str, object]]:
    result = list(mappings)
    for path, cached in cache.items():
        if _valid_cached_miss(cached, config) and _cache_miss_invalidates_unlocked_metadata(cached):
            result = _remove_unlocked_metadata_mapping(result, path)
    return result


def _season_scoped_cached_mappings(
    cache: dict[str, object],
    metadata_mappings: list[dict[str, object]],
    config: AppConfig,
) -> list[dict[str, object]]:
    """Narrow matched or season-only ambiguous source IDs with local NFOs.

    Do not restore an ambiguous whole-series mapping or infer season one from
    an unnumbered release. Only the already persisted source ID with an
    explicit season can gain a matching, independently declared NFO scope.
    Original miss/failed records remain intact and changed NFOs are rechecked.
    Completed-source import still requires independently explicit season data;
    this local scope never makes an unnumbered historical batch safe to use.
    """
    result: list[dict[str, object]] = []
    for mapping in metadata_mappings:
        if _mapping_is_protected(mapping):
            continue
        root = Path(str(mapping.get("path") or ""))
        cached = cache.get(str(root))
        if not (isinstance(cached, dict) and mapping.get("metadata_provider") and mapping.get("metadata_provider_id")):
            continue
        if _valid_cached_mapping(cached, config):
            raw = cached["mapping"]
            candidates = [{"bangumi_id": raw.get("bangumi_id"), "title": raw.get("title") or cached.get("title"),
                           "local_aliases": raw.get("match") or []}]
        elif (_valid_cached_miss(cached, config) and cached.get("reason") == "ambiguous_candidates"
              and float(cached.get("confidence") or 0) >= config.mikan_auto_match_threshold):
            pair = [cached.get("best"), cached.get("runner_up")]
            if not all(isinstance(candidate, dict) for candidate in pair):
                continue
            candidates = [candidate for candidate in pair if isinstance(candidate, dict)]
        else:
            continue
        identities = {normalize_match_text(release_series_identity(str(c.get("title") or ""))) for c in candidates}
        if len(identities) != 1 or not next(iter(identities), ""):
            continue
        selected = [c for c in candidates if c.get("bangumi_id") == mapping.get("bangumi_id")]
        if len(selected) != 1:
            continue
        candidate = selected[0]
        season = release_season_number(str(candidate.get("title") or ""))
        if season is None or season <= 0:
            continue
        scope = root / f"Season {season}"
        try:
            show_bytes = (root / "tvshow.nfo").read_bytes()
            season_bytes = (scope / "season.nfo").read_bytes()
            if len(show_bytes) > 1024 * 1024 or len(season_bytes) > 1024 * 1024:
                continue
            show = ET.fromstring(show_bytes)
            season_nfo = ET.fromstring(season_bytes)
            if int(season_nfo.findtext("seasonnumber") or "-1") != season:
                continue
            nfo_aliases = {
                normalize_match_text(alias.strip())
                for tag in ("title", "originaltitle", "sorttitle")
                for value in show.findall(tag)
                for alias in [str(value.text or ""), *str(value.text or "").split(" / ")]
            }
            if normalize_match_text(str(mapping.get("metadata_title") or "")) not in nfo_aliases:
                continue
            source_aliases = [candidate.get("source_query"), *(candidate.get("local_aliases") or [])]
            if not any(normalize_match_text(str(alias or "")) in nfo_aliases for alias in source_aliases if alias):
                continue
        except (OSError, ET.ParseError, TypeError, ValueError):
            continue
        evidence = {
            "rule": "persisted-source-id-explicit-season-nfo-v1",
            "source_id": int(candidate["bangumi_id"]),
            "source_title": str(candidate["title"]),
            "source_family": next(iter(identities)),
            "provider": mapping["metadata_provider"],
            "provider_id": mapping["metadata_provider_id"],
            "season": season,
            "show_nfo_sha256": hashlib.sha256(show_bytes).hexdigest(),
            "season_nfo_sha256": hashlib.sha256(season_bytes).hexdigest(),
        }
        result.append({
            **mapping, "path": str(scope), "season": season,
            "title": str(candidate["title"]), "identity_source": "cached_season_nfo",
            "identity_evidence": evidence,
            "identity_fingerprint": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
        })
    return result


def _semantic_match_token(token: str) -> str:
    """Return a normalized title token only when it carries series identity.

    Release metadata frequently contributes aliases such as ``01~12``, ``86``
    or ``2014冬``.  Those values describe an episode range, number or season;
    treating them as title aliases can map an unrelated torrent to a random
    series.  Matching therefore requires real alphabetic/CJK title content.
    Two-character CJK titles remain valid (for example ``農林``), while other
    tokens need at least three normalized characters.
    """

    normalized = normalize_match_text(token)
    if not normalized or normalized.isdecimal():
        return ""

    semantic = [char for char in normalized if char.isalpha() or _is_cjk_or_kana(char)]
    if not semantic:
        return ""
    digits = sum(char.isdecimal() for char in normalized)
    if digits >= 4 and len(semantic) <= 1:
        return ""
    if len(normalized) < 3:
        if len(normalized) != 2 or not all(_is_cjk_or_kana(char) for char in normalized):
            return ""
    return normalized


def _deduplicate_mappings(mappings: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    scoped_roots = {
        (int(mapping["bangumi_id"]), str(Path(str(mapping["path"])).parent).casefold())
        for mapping in mappings if mapping.get("identity_source") == "cached_season_nfo"
    }
    for mapping in mappings:
        bangumi_id = int(mapping["bangumi_id"])
        path = str(mapping["path"])
        key = (bangumi_id, path.casefold())
        if key in scoped_roots and not _mapping_is_protected(mapping):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(mapping)
    return result


def _resolve_cache_path(config: AppConfig) -> Path:
    path = Path(config.mikan_auto_match_cache_path)
    if not path.is_absolute():
        path = config.work_path / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_cache(path: Path, *, config: AppConfig | None = None) -> dict[str, object]:
    if config is not None and sqlite_cache_enabled(config):
        store = MikanIndexedCache(
            config,
            namespace="auto_match",
            legacy_path=path,
            schema_version=1,
        )
        if not store.initialized():
            store.initialize_if_needed(_load_cache_json(path))
        return store.load_all()
    return _load_cache_json(path)


def _load_cache_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_cache(
    path: Path,
    cache: dict[str, object],
    *,
    config: AppConfig | None = None,
) -> None:
    if config is not None and sqlite_cache_enabled(config):
        store = MikanIndexedCache(
            config,
            namespace="auto_match",
            legacy_path=path,
            schema_version=1,
        )
        store.initialize_if_needed({})
        store.replace_all(cache)
        return
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _valid_cached_mapping(value: object, config: AppConfig) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("matcher_version") != _AUTO_MATCH_MATCHER_VERSION:
        return False
    confidence = value.get("confidence")
    mapping = value.get("mapping")
    if not isinstance(confidence, int | float) or confidence < config.mikan_auto_match_threshold:
        return False
    if not isinstance(mapping, dict):
        return False
    return isinstance(mapping.get("bangumi_id"), int) and isinstance(mapping.get("path"), str)


def _valid_cached_miss(value: object, config: AppConfig) -> bool:
    if not isinstance(value, dict) or value.get("status") != "miss":
        return False
    if value.get("matcher_version") != _AUTO_MATCH_MATCHER_VERSION:
        return False
    reason = value.get("reason")
    if reason == "below_threshold":
        confidence = value.get("confidence")
        return isinstance(confidence, int | float) and confidence < config.mikan_auto_match_threshold
    return reason in {"ambiguous_candidates", "no_candidates", "no_scored_candidates"}


def _is_cjk_or_kana(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _to_simplified_chinese(value: str) -> str:
    global _OPENCC_T2S, _OPENCC_T2S_UNAVAILABLE
    if _OPENCC_T2S_UNAVAILABLE:
        return value
    if _OPENCC_T2S is None:
        try:
            from opencc import OpenCC

            _OPENCC_T2S = OpenCC("t2s")
        except Exception:
            _OPENCC_T2S_UNAVAILABLE = True
            return value
    try:
        return _OPENCC_T2S.convert(value)
    except Exception:
        return value
