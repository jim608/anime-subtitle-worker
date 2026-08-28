from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

from config import AppConfig, load_config
from metadata_context import build_series_metadata_context
from scan_state import scan_state_path
from series_metadata import SeriesMetadataStore, SeriesProfile, series_root_for_video


@dataclass
class _SeriesCandidate:
    root: Path
    title: str
    aliases: set[str] = field(default_factory=set)
    mikan_bangumi_id: int | None = None
    confidence: float = 0.0
    source: str = "library-index"
    sample_video: Path | None = None


def sync_series_metadata(config: AppConfig, logger: logging.Logger | None = None) -> dict[str, Any]:
    """Seed the series knowledge base from existing state without walking /anime.

    The scanner queue and Mikan episode index already contain canonical media
    paths. Reusing those databases avoids another recursive library scan and
    makes the WebUI useful immediately after upgrading an existing install.
    AniList enrichment remains incremental when AI processes a series.
    """

    log = logger or logging.getLogger(__name__)
    candidates: dict[str, _SeriesCandidate] = {}
    sources = {
        "manual_mappings": _add_manual_mappings(config, candidates),
        "mikan_cache": _add_mikan_cache(config, candidates, log),
        "episode_index": _add_episode_index(config, candidates, log),
        "scanner_state": _add_scanner_state(config, candidates, log),
    }

    created = 0
    linked_mikan = 0
    unchanged = 0
    with SeriesMetadataStore.from_config(config) as store:
        for candidate in sorted(candidates.values(), key=lambda item: str(item.root).casefold()):
            existing = store.get_by_local_path(candidate.root)
            aliases = sorted({candidate.root.name, candidate.title, *candidate.aliases} - {""})
            if existing is not None:
                if candidate.mikan_bangumi_id is not None and existing.mikan_bangumi_id != candidate.mikan_bangumi_id:
                    store.set_mikan_identity(
                        candidate.root,
                        bangumi_id=candidate.mikan_bangumi_id,
                        title=candidate.title,
                        confidence=candidate.confidence,
                        aliases=aliases,
                        commit=False,
                    )
                    linked_mikan += 1
                else:
                    unchanged += 1
                continue

            if candidate.mikan_bangumi_id is not None:
                store.set_mikan_identity(
                    candidate.root,
                    bangumi_id=candidate.mikan_bangumi_id,
                    title=candidate.title,
                    confidence=candidate.confidence,
                    aliases=aliases,
                    commit=False,
                )
                linked_mikan += 1
            else:
                store.upsert_profile(
                    SeriesProfile(
                        local_path=str(candidate.root),
                        canonical_title=candidate.title or candidate.root.name,
                        provider="local",
                        titles=[candidate.title] if candidate.title else [],
                        aliases=aliases,
                        match_confidence=0.0,
                        match_source=candidate.source,
                        metadata_version="local-index:v1",
                        updated_at=time.time(),
                    ),
                    commit=False,
                )
            created += 1

        completed_at = time.time()
        summary = {
            "candidates": len(candidates),
            "created": created,
            "linked_mikan": linked_mikan,
            "unchanged": unchanged,
            "sources": sources,
            "completed_at": completed_at,
        }
        store.set_meta("last_index_sync_at", str(completed_at), commit=False)
        store.set_meta("last_index_sync_summary", json.dumps(summary, ensure_ascii=False), commit=False)
        store.commit()

    enrichment = _enrich_candidates(config, candidates, log)
    summary["enrichment"] = enrichment
    log.info(
        "Series metadata index sync complete. candidates=%s created=%s linked_mikan=%s unchanged=%s enriched=%s sources=%s",
        len(candidates),
        created,
        linked_mikan,
        unchanged,
        enrichment.get("enriched", 0),
        sources,
    )
    return summary


def _add_manual_mappings(config: AppConfig, candidates: dict[str, _SeriesCandidate]) -> int:
    added = 0
    for mapping in getattr(config, "mikan_series_path_mappings", []):
        if not isinstance(mapping, dict):
            continue
        root = Path(str(mapping.get("path") or ""))
        bangumi_id = _as_int(mapping.get("bangumi_id"))
        if not str(root) or bangumi_id is None:
            continue
        title = str(mapping.get("title") or root.name).strip() or root.name
        aliases = _string_values(mapping.get("match"))
        _merge_candidate(candidates, root, title, aliases, bangumi_id, 1.0, "mikan-manual")
        added += 1
    return added


def _add_mikan_cache(
    config: AppConfig,
    candidates: dict[str, _SeriesCandidate],
    logger: logging.Logger,
) -> int:
    path = Path(str(getattr(config, "mikan_auto_match_cache_path", "mikan_auto_matches.json")))
    if not path.is_absolute():
        path = Path(config.work_path) / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unable to read Mikan auto-match cache for series sync: %s", exc)
        return 0
    if not isinstance(payload, dict):
        return 0

    added = 0
    for value in payload.values():
        if not isinstance(value, dict) or value.get("status") != "matched":
            continue
        mapping = value.get("mapping")
        if not isinstance(mapping, dict):
            continue
        root = Path(str(mapping.get("path") or ""))
        bangumi_id = _as_int(mapping.get("bangumi_id"))
        if not str(root) or bangumi_id is None:
            continue
        title = str(value.get("title") or mapping.get("title") or root.name).strip() or root.name
        aliases = _string_values(mapping.get("match"))
        confidence = _as_float(value.get("confidence"), 0.9)
        _merge_candidate(candidates, root, title, aliases, bangumi_id, confidence, "mikan-cache")
        added += 1
    return added


def _add_episode_index(
    config: AppConfig,
    candidates: dict[str, _SeriesCandidate],
    logger: logging.Logger,
) -> int:
    pending_path = Path(str(getattr(config, "mikan_pending_path", "mikan_pending.json")))
    if not pending_path.is_absolute():
        pending_path = Path(config.work_path) / pending_path
    db_path = pending_path.with_name("mikan_state.sqlite3")
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(anime_episode_index)").fetchall()}
            sample_expr = "MIN(path)" if "path" in columns else "NULL"
            rows = conn.execute(
                f"""
                SELECT bangumi_id, series_path, {sample_expr}
                FROM anime_episode_index
                WHERE series_path <> ''
                GROUP BY bangumi_id, series_path
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Unable to read Mikan episode index for series sync: %s", exc)
        return 0

    for bangumi_id, series_path, sample_video in rows:
        root = Path(str(series_path))
        _merge_candidate(
            candidates,
            root,
            root.name,
            [],
            _as_int(bangumi_id),
            0.85,
            "mikan-episode-index",
            sample_video=Path(str(sample_video)) if sample_video else None,
        )
    return len(rows)


def _add_scanner_state(
    config: AppConfig,
    candidates: dict[str, _SeriesCandidate],
    logger: logging.Logger,
) -> int:
    db_path = scan_state_path(config)
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute("SELECT path FROM ai_candidate_queue WHERE path <> ''").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Unable to read scanner state for series sync: %s", exc)
        return 0

    for (video_path,) in rows:
        root = series_root_for_video(str(video_path))
        _merge_candidate(
            candidates,
            root,
            root.name,
            [],
            None,
            0.0,
            "scanner-state",
            sample_video=Path(str(video_path)),
        )
    return len(rows)


def _merge_candidate(
    candidates: dict[str, _SeriesCandidate],
    root: Path,
    title: str,
    aliases: list[str],
    bangumi_id: int | None,
    confidence: float,
    source: str,
    *,
    sample_video: Path | None = None,
) -> None:
    key = str(root).casefold()
    candidate = candidates.get(key)
    if candidate is None:
        candidate = _SeriesCandidate(root=root, title=title or root.name, source=source)
        candidates[key] = candidate
    candidate.aliases.update(item for item in aliases if item)
    if title:
        candidate.aliases.add(title)
    if candidate.sample_video is None and sample_video is not None:
        candidate.sample_video = sample_video
    if bangumi_id is not None and (candidate.mikan_bangumi_id is None or confidence > candidate.confidence):
        candidate.mikan_bangumi_id = bangumi_id
        candidate.title = title or candidate.title
        candidate.confidence = max(0.0, min(1.0, confidence))
        candidate.source = source


def _enrich_candidates(
    config: AppConfig,
    candidates: dict[str, _SeriesCandidate],
    logger: logging.Logger,
) -> dict[str, Any]:
    limit = max(0, int(getattr(config, "series_metadata_enrich_per_cycle", 5) or 0))
    if not bool(getattr(config, "series_metadata_enrich_enabled", True)) or limit <= 0:
        return {"enabled": False, "attempted": 0, "enriched": 0, "missed": 0}
    if not bool(getattr(config, "translation_metadata_context_enabled", False)):
        return {
            "enabled": False,
            "reason": "metadata_context_disabled",
            "attempted": 0,
            "enriched": 0,
            "missed": 0,
        }

    ordered = sorted(candidates.values(), key=lambda item: str(item.root).casefold())
    with SeriesMetadataStore.from_config(config) as store:
        cursor = str(store.get_meta("enrichment_cursor") or "").casefold()
        pending: list[_SeriesCandidate] = []
        for candidate in ordered:
            profile = store.get_by_local_path(candidate.root)
            if profile is None or profile.locked:
                continue
            if profile.provider == "anilist" and profile.synopsis and profile.characters:
                continue
            pending.append(candidate)

    if cursor and pending:
        split = next(
            (index for index, item in enumerate(pending) if str(item.root).casefold() > cursor),
            0,
        )
        pending = pending[split:] + pending[:split]

    attempted = 0
    enriched = 0
    missed = 0
    last_root = ""
    delay = max(0.0, float(getattr(config, "series_metadata_enrich_delay_seconds", 1.0) or 0.0))
    selected = pending[:limit]
    for candidate in selected:
        sample_video = candidate.sample_video or candidate.root / "Season 1" / f"{candidate.title} - S01E01.mkv"
        context = build_series_metadata_context(sample_video, config, logger)
        attempted += 1
        last_root = str(candidate.root)
        if context is not None and context.provider == "anilist":
            enriched += 1
        else:
            missed += 1
        if delay > 0 and attempted < len(selected):
            time.sleep(delay)

    if last_root:
        with SeriesMetadataStore.from_config(config) as store:
            store.set_meta("enrichment_cursor", last_root, commit=False)
            store.set_meta("last_enrichment_at", str(time.time()), commit=False)
            store.set_meta(
                "last_enrichment_summary",
                json.dumps(
                    {"attempted": attempted, "enriched": enriched, "missed": missed, "cursor": last_root},
                    ensure_ascii=False,
                ),
                commit=False,
            )
            store.commit()
    return {
        "enabled": True,
        "eligible": len(pending),
        "attempted": attempted,
        "enriched": enriched,
        "missed": missed,
        "cursor": last_root,
    }


def _string_values(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed series metadata from existing Worker indexes")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = sync_series_metadata(config, logging.getLogger("series-metadata-sync"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
