from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Any, Iterable

from sqlite_safety import online_backup_before_migration, quick_check_connection

SERIES_METADATA_SCHEMA_VERSION = 3
SEASON_FOLDER_RE = re.compile(r"(?i)^(?:season|saison|staffel|series)\s*(\d+)|^S(\d+)$")
_SCHEMA_LOCK = threading.Lock()


@dataclass(frozen=True)
class SeriesProfile:
    local_path: str
    canonical_title: str
    provider: str = ""
    provider_id: str = ""
    anidb_id: str = ""
    mikan_bangumi_id: int | None = None
    premiered_year: int | None = None
    season_number: int | None = None
    titles: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    synopsis: str = ""
    characters: list[str] = field(default_factory=list)
    staff: list[str] = field(default_factory=list)
    cover_image_url: str = ""
    cover_image_color: str = ""
    cover_image_cache_key: str = ""
    cover_image_updated_at: float = 0.0
    match_confidence: float = 0.0
    match_source: str = "automatic"
    locked: bool = False
    metadata_version: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SeriesMetadataStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _SCHEMA_LOCK:
            existing_version = _existing_schema_version(self.path)
            if self.path.is_file() and self.path.stat().st_size > 0 and existing_version < SERIES_METADATA_SCHEMA_VERSION:
                online_backup_before_migration(
                    self.path,
                    backup_dir=self.path.parent / "sqlite_migration_backups",
                    reason=f"series-v{existing_version}-to-v{SERIES_METADATA_SCHEMA_VERSION}",
                )
            self.conn = sqlite3.connect(self.path, timeout=60)
            try:
                self.conn.row_factory = sqlite3.Row
                self.conn.execute("PRAGMA busy_timeout=60000")
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self._ensure_schema()
            except Exception:
                self.conn.close()
                raise

    @classmethod
    def from_config(cls, config: Any) -> "SeriesMetadataStore":
        return cls(resolve_series_metadata_db_path(config))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SeriesMetadataStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

    def get_by_local_path(self, local_path: str | Path) -> SeriesProfile | None:
        key = canonical_local_path(local_path)
        row = self.conn.execute("SELECT * FROM series_profiles WHERE local_path_key = ?", (key,)).fetchone()
        return _profile_from_row(row) if row is not None else None

    def get_by_series_id(self, series_id: str) -> SeriesProfile | None:
        row = self.conn.execute("SELECT * FROM series_profiles WHERE series_id = ?", (str(series_id),)).fetchone()
        return _profile_from_row(row) if row is not None else None

    def list_by_mikan_ids(self, bangumi_ids: Iterable[int]) -> list[SeriesProfile]:
        """Return profiles carrying one of the verified Mikan identities."""

        normalized_values: set[int] = set()
        for value in bangumi_ids:
            try:
                normalized_value = int(value)
            except (TypeError, ValueError):
                continue
            if normalized_value > 0:
                normalized_values.add(normalized_value)
        normalized = sorted(normalized_values)
        if not normalized:
            return []
        placeholders = ",".join("?" for _value in normalized)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM series_profiles
            WHERE mikan_bangumi_id IN ({placeholders})
            ORDER BY locked DESC, match_confidence DESC, updated_at DESC, canonical_title
            """,
            normalized,
        ).fetchall()
        return [_profile_from_row(row) for row in rows]

    def get_for_video(self, video_path: str | Path) -> SeriesProfile | None:
        return self.get_by_local_path(series_root_for_video(video_path))

    def upsert_profile(self, profile: SeriesProfile, *, commit: bool = True) -> SeriesProfile:
        local_path = str(Path(profile.local_path))
        key = canonical_local_path(local_path)
        existing = self.get_by_local_path(local_path)
        if existing is not None and existing.locked and profile.match_source != "manual":
            self.conn.execute(
                "UPDATE series_profiles SET last_seen_at = ?, updated_at = MAX(updated_at, ?) WHERE local_path_key = ?",
                (time.time(), float(existing.updated_at or 0), key),
            )
            if commit:
                self.conn.commit()
            return existing

        now = time.time()
        payload = profile.to_dict()
        payload["local_path"] = local_path
        payload["updated_at"] = float(profile.updated_at or now)
        self.conn.execute(
            """
            INSERT INTO series_profiles (
                local_path_key, series_id, local_path, canonical_title, provider, provider_id,
                anidb_id, mikan_bangumi_id, premiered_year, season_number,
                titles_json, aliases_json, synopsis, characters_json, staff_json,
                cover_image_url, cover_image_color, cover_image_cache_key, cover_image_updated_at,
                match_confidence, match_source, locked, metadata_version,
                created_at, updated_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(local_path_key) DO UPDATE SET
                series_id = excluded.series_id,
                local_path = excluded.local_path,
                canonical_title = excluded.canonical_title,
                provider = excluded.provider,
                provider_id = excluded.provider_id,
                anidb_id = COALESCE(NULLIF(excluded.anidb_id, ''), series_profiles.anidb_id),
                mikan_bangumi_id = COALESCE(excluded.mikan_bangumi_id, series_profiles.mikan_bangumi_id),
                premiered_year = COALESCE(excluded.premiered_year, series_profiles.premiered_year),
                season_number = COALESCE(excluded.season_number, series_profiles.season_number),
                titles_json = excluded.titles_json,
                aliases_json = excluded.aliases_json,
                synopsis = excluded.synopsis,
                characters_json = excluded.characters_json,
                staff_json = excluded.staff_json,
                cover_image_url = CASE
                    WHEN excluded.cover_image_cache_key <> '' THEN excluded.cover_image_url
                    ELSE COALESCE(NULLIF(series_profiles.cover_image_url, ''), excluded.cover_image_url)
                END,
                cover_image_color = CASE
                    WHEN excluded.cover_image_cache_key <> '' THEN excluded.cover_image_color
                    ELSE COALESCE(NULLIF(series_profiles.cover_image_color, ''), excluded.cover_image_color)
                END,
                cover_image_cache_key = COALESCE(
                    NULLIF(excluded.cover_image_cache_key, ''),
                    series_profiles.cover_image_cache_key
                ),
                cover_image_updated_at = CASE
                    WHEN excluded.cover_image_cache_key <> '' THEN excluded.cover_image_updated_at
                    ELSE series_profiles.cover_image_updated_at
                END,
                match_confidence = excluded.match_confidence,
                match_source = excluded.match_source,
                locked = excluded.locked,
                metadata_version = excluded.metadata_version,
                updated_at = excluded.updated_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                key,
                stable_series_id(key),
                local_path,
                profile.canonical_title,
                profile.provider,
                profile.provider_id,
                profile.anidb_id,
                profile.mikan_bangumi_id,
                profile.premiered_year,
                profile.season_number,
                _json(profile.titles),
                _json(profile.aliases),
                profile.synopsis,
                _json(profile.characters),
                _json(profile.staff),
                profile.cover_image_url,
                profile.cover_image_color,
                profile.cover_image_cache_key,
                max(0.0, float(profile.cover_image_updated_at or 0)),
                max(0.0, min(1.0, float(profile.match_confidence))),
                profile.match_source,
                int(profile.locked),
                profile.metadata_version,
                now,
                payload["updated_at"],
                now,
            ),
        )
        self._record_event(key, "profile_upsert", {"provider": profile.provider, "provider_id": profile.provider_id})
        if commit:
            self.conn.commit()
        return self.get_by_local_path(local_path) or profile

    def list_profiles(self, *, search: str = "", limit: int = 200, offset: int = 0) -> list[SeriesProfile]:
        where = ""
        params: list[Any] = []
        if search.strip():
            where = "WHERE canonical_title LIKE ? OR local_path LIKE ? OR aliases_json LIKE ?"
            token = f"%{search.strip()}%"
            params.extend([token, token, token])
        params.extend([max(1, min(1000, int(limit))), max(0, int(offset))])
        rows = self.conn.execute(
            f"SELECT * FROM series_profiles {where} ORDER BY updated_at DESC, canonical_title LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [_profile_from_row(row) for row in rows]

    def count_profiles(self, *, search: str = "") -> int:
        if not search.strip():
            return int(self.conn.execute("SELECT COUNT(*) FROM series_profiles").fetchone()[0])
        token = f"%{search.strip()}%"
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM series_profiles WHERE canonical_title LIKE ? OR local_path LIKE ? OR aliases_json LIKE ?",
                (token, token, token),
            ).fetchone()[0]
        )

    def set_manual_match(
        self,
        local_path: str | Path,
        *,
        provider: str,
        provider_id: str,
        canonical_title: str,
        locked: bool = True,
    ) -> SeriesProfile:
        existing = self.get_by_local_path(local_path)
        profile = SeriesProfile(
            local_path=str(series_root_for_video(local_path)),
            canonical_title=canonical_title or (existing.canonical_title if existing else Path(local_path).name),
            provider=provider,
            provider_id=str(provider_id),
            anidb_id=existing.anidb_id if existing else "",
            mikan_bangumi_id=existing.mikan_bangumi_id if existing else None,
            premiered_year=existing.premiered_year if existing else None,
            season_number=existing.season_number if existing else None,
            titles=existing.titles if existing else [canonical_title],
            aliases=existing.aliases if existing else [Path(local_path).name],
            synopsis=existing.synopsis if existing else "",
            characters=existing.characters if existing else [],
            staff=existing.staff if existing else [],
            cover_image_url=existing.cover_image_url if existing else "",
            cover_image_color=existing.cover_image_color if existing else "",
            cover_image_cache_key=existing.cover_image_cache_key if existing else "",
            cover_image_updated_at=existing.cover_image_updated_at if existing else 0.0,
            match_confidence=1.0,
            match_source="manual",
            locked=locked,
            metadata_version=existing.metadata_version if existing else "manual",
            updated_at=time.time(),
        )
        return self.upsert_profile(profile)

    def set_locked(self, local_path: str | Path, locked: bool) -> bool:
        key = canonical_local_path(local_path)
        cursor = self.conn.execute(
            "UPDATE series_profiles SET locked = ?, updated_at = ? WHERE local_path_key = ?",
            (int(locked), time.time(), key),
        )
        if cursor.rowcount:
            self._record_event(key, "profile_lock", {"locked": bool(locked)})
        self.conn.commit()
        return bool(cursor.rowcount)

    def set_mikan_identity(
        self,
        local_path: str | Path,
        *,
        bangumi_id: int,
        title: str,
        confidence: float,
        aliases: Iterable[str] = (),
        premiered_year: int | None = None,
        anidb_id: str = "",
        commit: bool = True,
    ) -> SeriesProfile:
        existing = self.get_by_local_path(local_path)
        if existing is None:
            return self.upsert_profile(
                SeriesProfile(
                    local_path=str(Path(local_path)),
                    canonical_title=title or Path(local_path).name,
                    provider="mikan",
                    provider_id=str(int(bangumi_id)),
                    anidb_id=anidb_id,
                    mikan_bangumi_id=int(bangumi_id),
                    premiered_year=premiered_year,
                    titles=[title] if title else [],
                    aliases=[str(item) for item in aliases if str(item).strip()],
                    match_confidence=max(0.0, min(1.0, float(confidence))),
                    match_source="mikan-auto",
                    metadata_version=f"mikan:{int(bangumi_id)}",
                    updated_at=time.time(),
                ),
                commit=commit,
            )
        key = canonical_local_path(local_path)
        merged_aliases = list(dict.fromkeys([*existing.aliases, *[str(item) for item in aliases], title]))
        self.conn.execute(
            """
            UPDATE series_profiles
            SET mikan_bangumi_id = ?, aliases_json = ?, last_seen_at = ?, updated_at = ?
            WHERE local_path_key = ?
            """,
            (int(bangumi_id), _json(merged_aliases), time.time(), time.time(), key),
        )
        self._record_event(key, "mikan_identity", {"bangumi_id": int(bangumi_id), "confidence": confidence})
        if commit:
            self.conn.commit()
        return self.get_by_local_path(local_path) or existing

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM series_metadata_meta WHERE key = ?",
            (str(key),),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def set_meta(self, key: str, value: str, *, commit: bool = True) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO series_metadata_meta(key, value) VALUES (?, ?)",
            (str(key), str(value)),
        )
        if commit:
            self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def glossary(self, local_path: str | Path) -> dict[str, str]:
        key = canonical_local_path(local_path)
        rows = self.conn.execute(
            "SELECT source_text, target_text FROM series_glossary WHERE local_path_key = ? AND target_text <> '' ORDER BY locked DESC, source_text",
            (key,),
        ).fetchall()
        return {str(row["source_text"]): str(row["target_text"]) for row in rows}

    def glossary_rows(self, local_path: str | Path) -> list[dict[str, Any]]:
        key = canonical_local_path(local_path)
        rows = self.conn.execute(
            "SELECT source_text, target_text, term_type, locked, source, updated_at FROM series_glossary WHERE local_path_key = ? ORDER BY locked DESC, source_text",
            (key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_glossary_term(
        self,
        local_path: str | Path,
        source_text: str,
        target_text: str,
        *,
        term_type: str = "term",
        locked: bool = True,
        source: str = "manual",
    ) -> None:
        source_text = str(source_text).strip()
        if not source_text:
            raise ValueError("Glossary source text cannot be empty")
        key = canonical_local_path(local_path)
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO series_glossary (
                local_path_key, source_text, target_text, term_type, locked, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(local_path_key, source_text) DO UPDATE SET
                target_text = excluded.target_text,
                term_type = excluded.term_type,
                locked = excluded.locked,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (key, source_text, str(target_text).strip(), term_type, int(locked), source, now, now),
        )
        self._record_event(key, "glossary_upsert", {"source_text": source_text, "term_type": term_type})
        self.conn.commit()

    def delete_glossary_term(self, local_path: str | Path, source_text: str) -> bool:
        key = canonical_local_path(local_path)
        cursor = self.conn.execute(
            "DELETE FROM series_glossary WHERE local_path_key = ? AND source_text = ?",
            (key, str(source_text).strip()),
        )
        self.conn.commit()
        return bool(cursor.rowcount)

    def seed_terms(self, local_path: str | Path, terms: Iterable[str], *, term_type: str = "character") -> int:
        existing = {row["source_text"] for row in self.glossary_rows(local_path)}
        inserted = 0
        for term in terms:
            value = str(term).strip()
            if not value or value in existing:
                continue
            self.upsert_glossary_term(
                local_path,
                value,
                "",
                term_type=term_type,
                locked=False,
                source="metadata",
            )
            existing.add(value)
            inserted += 1
        return inserted

    def _record_event(self, local_path_key: str, event: str, detail: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO series_metadata_events (local_path_key, event, detail_json, created_at) VALUES (?, ?, ?, ?)",
            (local_path_key, event, _json(detail), time.time()),
        )

    def _ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS series_profiles (
                local_path_key TEXT PRIMARY KEY,
                series_id TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL,
                canonical_title TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '',
                anidb_id TEXT NOT NULL DEFAULT '',
                mikan_bangumi_id INTEGER,
                premiered_year INTEGER,
                season_number INTEGER,
                titles_json TEXT NOT NULL DEFAULT '[]',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                synopsis TEXT NOT NULL DEFAULT '',
                characters_json TEXT NOT NULL DEFAULT '[]',
                staff_json TEXT NOT NULL DEFAULT '[]',
                cover_image_url TEXT NOT NULL DEFAULT '',
                cover_image_color TEXT NOT NULL DEFAULT '',
                cover_image_cache_key TEXT NOT NULL DEFAULT '',
                cover_image_updated_at REAL NOT NULL DEFAULT 0,
                match_confidence REAL NOT NULL DEFAULT 0,
                match_source TEXT NOT NULL DEFAULT 'automatic',
                locked INTEGER NOT NULL DEFAULT 0,
                metadata_version TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_seen_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_series_profiles_title ON series_profiles(canonical_title);
            CREATE INDEX IF NOT EXISTS idx_series_profiles_provider ON series_profiles(provider, provider_id);
            CREATE INDEX IF NOT EXISTS idx_series_profiles_mikan ON series_profiles(mikan_bangumi_id);

            CREATE TABLE IF NOT EXISTS series_glossary (
                local_path_key TEXT NOT NULL,
                source_text TEXT NOT NULL,
                target_text TEXT NOT NULL DEFAULT '',
                term_type TEXT NOT NULL DEFAULT 'term',
                locked INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(local_path_key, source_text)
            );
            CREATE INDEX IF NOT EXISTS idx_series_glossary_path ON series_glossary(local_path_key, locked DESC);

            CREATE TABLE IF NOT EXISTS series_metadata_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_path_key TEXT NOT NULL,
                event TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_series_metadata_events_path ON series_metadata_events(local_path_key, created_at DESC);

            CREATE TABLE IF NOT EXISTS series_metadata_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(series_profiles)").fetchall()}
        if "series_id" not in columns:
            self.conn.execute("ALTER TABLE series_profiles ADD COLUMN series_id TEXT NOT NULL DEFAULT ''")
        additive_columns = {
            "cover_image_url": "TEXT NOT NULL DEFAULT ''",
            "cover_image_color": "TEXT NOT NULL DEFAULT ''",
            "cover_image_cache_key": "TEXT NOT NULL DEFAULT ''",
            "cover_image_updated_at": "REAL NOT NULL DEFAULT 0",
        }
        for name, definition in additive_columns.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE series_profiles ADD COLUMN {name} {definition}")
        for local_path_key, current_id in self.conn.execute(
            "SELECT local_path_key, series_id FROM series_profiles"
        ).fetchall():
            expected_id = stable_series_id(str(local_path_key))
            if str(current_id or "") != expected_id:
                self.conn.execute(
                    "UPDATE series_profiles SET series_id = ? WHERE local_path_key = ?",
                    (expected_id, str(local_path_key)),
                )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_series_profiles_series_id ON series_profiles(series_id)"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO series_metadata_meta(key, value) VALUES ('schema_version', ?)",
            (str(SERIES_METADATA_SCHEMA_VERSION),),
        )
        self.conn.commit()
        quick_check_connection(self.conn)


def stable_series_id(local_path_key: str) -> str:
    digest = hashlib.sha256(str(local_path_key).encode("utf-8")).hexdigest()[:24]
    return f"series_{digest}"


def _existing_schema_version(path: Path) -> int:
    if not path.is_file() or path.stat().st_size <= 0:
        return 0
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='series_metadata_meta'"
        ).fetchone() is None:
            return 0
        row = connection.execute(
            "SELECT value FROM series_metadata_meta WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return 0
    finally:
        if connection is not None:
            connection.close()


def resolve_series_metadata_db_path(config: Any) -> Path:
    configured = Path(str(getattr(config, "series_metadata_db_path", "series_metadata.sqlite3") or "series_metadata.sqlite3"))
    return configured if configured.is_absolute() else Path(config.work_path) / configured


def series_root_for_video(video_path: str | Path) -> Path:
    path = Path(video_path)
    directory = path if path.is_dir() else path.parent
    if SEASON_FOLDER_RE.fullmatch(directory.name.strip()) and directory.parent != directory:
        return directory.parent
    return directory


def season_number_for_video(video_path: str | Path) -> int | None:
    path = Path(video_path)
    directory = path if path.is_dir() else path.parent
    match = SEASON_FOLDER_RE.fullmatch(directory.name.strip())
    if match is None:
        return None
    raw = match.group(1) or match.group(2)
    return int(raw) if raw else None


def canonical_local_path(path: str | Path) -> str:
    value = str(Path(path).resolve())
    return value.casefold()


def _profile_from_row(row: sqlite3.Row) -> SeriesProfile:
    return SeriesProfile(
        local_path=str(row["local_path"]),
        canonical_title=str(row["canonical_title"]),
        provider=str(row["provider"]),
        provider_id=str(row["provider_id"]),
        anidb_id=str(row["anidb_id"]),
        mikan_bangumi_id=int(row["mikan_bangumi_id"]) if row["mikan_bangumi_id"] is not None else None,
        premiered_year=int(row["premiered_year"]) if row["premiered_year"] is not None else None,
        season_number=int(row["season_number"]) if row["season_number"] is not None else None,
        titles=_json_list(row["titles_json"]),
        aliases=_json_list(row["aliases_json"]),
        synopsis=str(row["synopsis"]),
        characters=_json_list(row["characters_json"]),
        staff=_json_list(row["staff_json"]),
        cover_image_url=str(_row_value(row, "cover_image_url", "")),
        cover_image_color=str(_row_value(row, "cover_image_color", "")),
        cover_image_cache_key=str(_row_value(row, "cover_image_cache_key", "")),
        cover_image_updated_at=float(_row_value(row, "cover_image_updated_at", 0.0) or 0.0),
        match_confidence=float(row["match_confidence"]),
        match_source=str(row["match_source"]),
        locked=bool(row["locked"]),
        metadata_version=str(row["metadata_version"]),
        updated_at=float(row["updated_at"]),
    )


def _row_value(row: sqlite3.Row, key: str, default: Any) -> Any:
    return row[key] if key in row.keys() else default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]
