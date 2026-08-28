from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from html import unescape
import json
import logging
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin

import requests

from config import AppConfig
from local_catalog import LocalSeries, discover_local_series
from mikan_cache_store import MikanIndexedCache, sqlite_cache_enabled
from mikan_source import MikanSourceError, fetch_bangumi_releases
from series_metadata import SeriesMetadataStore


_OPENCC_T2S = None
_OPENCC_T2S_UNAVAILABLE = False
_AUTO_MATCH_MATCHER_VERSION = 2
_AUTO_MATCH_MIN_MARGIN = 0.08


@dataclass(frozen=True)
class MikanSearchCandidate:
    bangumi_id: int
    title: str
    source_query: str


def resolve_mikan_series_mappings(
    config: AppConfig,
    logger: logging.Logger,
    *,
    cached_only: bool = False,
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

    for series in discover_local_series(config):
        series_key = str(series.path.resolve())
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
        result = _auto_match_series(series, config, logger)
        cache[series_key] = result
        cache_changed = True
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
) -> dict[str, object]:
    candidates = _search_candidates_for_series(series, config)
    if not candidates:
        return _miss_cache_entry(series, "no_candidates")

    scored: list[tuple[float, MikanSearchCandidate, list[str]]] = []
    for candidate in candidates:
        try:
            releases = fetch_bangumi_releases(
                config.mikan_base_url,
                candidate.bangumi_id,
                timeout_seconds=config.mikan_request_timeout_seconds,
            )
        except (requests.RequestException, MikanSourceError) as exc:
            logger.warning("Mikan auto-match failed to fetch RSS bangumi_id=%s: %s", candidate.bangumi_id, exc)
            continue

        release_titles = [release.title for release in releases[:20]]
        confidence = _candidate_confidence(series, candidate, release_titles)
        match_tokens = _match_tokens(series, candidate, release_titles)
        scored.append((confidence, candidate, match_tokens))

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


def _search_candidates_for_series(series: LocalSeries, config: AppConfig) -> list[MikanSearchCandidate]:
    candidates: dict[int, MikanSearchCandidate] = {}
    for alias in _search_aliases(series):
        try:
            results = search_mikan_bangumi(
                config.mikan_base_url,
                alias,
                timeout_seconds=config.mikan_request_timeout_seconds,
            )
        except requests.RequestException:
            continue
        for result in results:
            candidates.setdefault(result.bangumi_id, result)
    return list(candidates.values())[: config.mikan_auto_match_max_candidates]


def search_mikan_bangumi(base_url: str, query: str, timeout_seconds: int = 30) -> list[MikanSearchCandidate]:
    response = requests.get(
        urljoin(base_url.rstrip("/") + "/", "Home/Search"),
        params={"searchstr": query},
        timeout=timeout_seconds,
        headers={"User-Agent": "Mozilla/5.0"},
    )
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
    for mapping in mappings:
        bangumi_id = int(mapping["bangumi_id"])
        path = str(mapping["path"])
        key = (bangumi_id, path.casefold())
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
