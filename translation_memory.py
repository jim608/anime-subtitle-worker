from __future__ import annotations

"""Fail-closed, cross-episode translation memory for verified subtitle output.

The store deliberately has no dependency on the Worker pipeline.  Integration is
expected to happen only after strict publication and subtitle QC have succeeded.
Lookup can then be performed through a SQLite connection opened in read-only mode.

The reusable block key is the exact pair of normalized source text and a
deterministic surrounding-context digest.  Cue timing and block identity are
kept for audit, but never participate in the episode-diversity threshold.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence
import unicodedata


TRANSLATION_MEMORY_SCHEMA_VERSION = 2
TRANSLATION_MEMORY_CONTEXT_CONTRACT = "source-context-exact-v2"
CONTEXT_KEY_VERSION = 1
CONTEXT_KEY_PREFIX = f"tm-context-v{CONTEXT_KEY_VERSION}:"
STRICT_PUBLICATION_CONTRACT = "ai-publication-semantics-v2"
STRICT_PUBLICATION_KIND = "translated_trilingual"
SUPPORTED_SOURCE_LANGUAGE = "ja"
SUPPORTED_TARGET_LANGUAGE = "zh-CN"
MIN_AUTO_APPLY_EPISODE_DIVERSITY = 2
MAX_BLOCKS_PER_EPISODE = 5000
MAX_LOOKUP_BATCH = 5000
MAX_TERM_OBSERVATIONS_PER_EPISODE = 64
MAX_TEXT_CHARS = 4096
MAX_IDENTITY_CHARS = 512
MAX_TERM_RESULTS = 1000
TERM_EXTRACTOR_VERSION = 1
# NFKC expands each U+2026 horizontal ellipsis to three ASCII full stops.
SAFE_OMISSION_PLACEHOLDERS = frozenset({"……", "......"})


class TranslationMemoryError(RuntimeError):
    """Base error for a translation-memory operation."""


class EvidenceRejected(TranslationMemoryError):
    """Raised before learning when evidence does not meet the strict contract."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


class ReadOnlyStoreError(TranslationMemoryError):
    """Raised when a mutating operation is attempted through a read-only store."""


@dataclass(frozen=True)
class MemoryScope:
    """Isolation boundary for every lookup and observation."""

    series_key: str
    policy_version: str
    source_language: str = SUPPORTED_SOURCE_LANGUAGE
    target_language: str = SUPPORTED_TARGET_LANGUAGE


@dataclass(frozen=True)
class AlignedTranslationBlock:
    """One source/target alignment from a strictly verified publication."""

    block_identity: str
    source_text: str
    target_text: str
    context_key: str
    qc_passed: bool
    manual_reviewed: bool
    safe_omission: bool


@dataclass(frozen=True)
class VerifiedEpisodeTranslation:
    """Evidence envelope required before any block can enter the memory."""

    series_key: str
    policy_version: str
    episode_id: str
    manifest_identity: str
    publication_contract: str
    publication_kind: str
    source_language: str
    target_language: str
    strict_publication_verified: bool
    qc_passed: bool
    unattended: bool
    manual_reviewed: bool
    safe_omission: bool
    source_manifest_hash: str
    target_manifest_hash: str
    verified_at: str
    blocks: Sequence[AlignedTranslationBlock]


@dataclass(frozen=True)
class CandidateEvidence:
    target_text: str
    target_hash: str
    support_count: int
    episode_diversity: int
    last_verified: str


@dataclass(frozen=True)
class TranslationLookup:
    source_text: str
    source_key: str
    context_key: str
    status: str
    auto_target: str | None
    candidates: tuple[CandidateEvidence, ...]

    @property
    def can_auto_apply(self) -> bool:
        return self.status == "auto_apply" and self.auto_target is not None


@dataclass(frozen=True)
class TermSuggestion:
    source_term: str
    target_term: str
    target_hash: str
    support_count: int
    episode_diversity: int
    last_verified: str


@dataclass(frozen=True)
class LearnResult:
    status: str
    manifest_identity: str
    inserted_blocks: int
    inserted_term_observations: int
    verification_refreshed: bool


@dataclass(frozen=True)
class TranslationMemoryStats:
    manifest_count: int
    episode_count: int
    observation_count: int
    source_key_count: int
    auto_apply_source_count: int
    conflict_source_count: int
    insufficient_source_count: int
    term_observation_count: int
    term_source_count: int
    auto_apply_term_count: int
    conflict_term_count: int
    insufficient_term_count: int
    last_verified: str | None


@dataclass(frozen=True)
class AuditObservation:
    episode_id: str
    manifest_identity: str
    publication_contract: str
    publication_kind: str
    source_manifest_hash: str
    target_manifest_hash: str
    block_identity: str
    context_key: str
    source_text: str
    target_text: str
    source_text_hash: str
    target_text_hash: str
    verified_at: str


@dataclass(frozen=True)
class _PreparedBlock:
    block_identity: str
    source_text: str
    target_text: str
    source_key: str
    context_key: str
    target_key: str
    source_text_hash: str
    target_text_hash: str
    target_key_hash: str


@dataclass(frozen=True)
class _PreparedTerm:
    block_identity: str
    context_key: str
    source_term: str
    target_term: str
    source_term_key: str
    source_term_hash: str
    target_term_hash: str


@dataclass(frozen=True)
class _PreparedEvidence:
    series_key: str
    policy_version: str
    episode_id: str
    manifest_identity: str
    publication_contract: str
    publication_kind: str
    source_language: str
    target_language: str
    source_manifest_hash: str
    target_manifest_hash: str
    verified_at: str
    evidence_hash: str
    blocks: tuple[_PreparedBlock, ...]
    terms: tuple[_PreparedTerm, ...]


_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTEXT_KEY_RE = re.compile(
    rf"^{re.escape(CONTEXT_KEY_PREFIX)}[0-9a-f]{{64}}$"
)
_WHITESPACE_RE = re.compile(r"\s+")
_KATAKANA_TERM_RE = re.compile(r"^[\u30a1-\u30fa\u30fc\u30fb]{2,24}$")
_COMPACT_CHINESE_TERM_RE = re.compile(r"^[\u3400-\u9fffA-Za-z0-9\u00b7\u30fb\-]{1,24}$")
_HAS_HAN_RE = re.compile(r"[\u3400-\u9fff]")
_QUOTED_TERM_PAIRS = (
    ("\u300c", "\u300d"),
    ("\u300e", "\u300f"),
    ('"', '"'),
)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tm_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tm_scope (
    id INTEGER PRIMARY KEY,
    series_key TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    source_language TEXT NOT NULL CHECK(source_language = 'ja'),
    target_language TEXT NOT NULL CHECK(target_language = 'zh-CN'),
    created_at TEXT NOT NULL,
    UNIQUE(series_key, policy_version, source_language, target_language)
);

CREATE TABLE IF NOT EXISTS tm_manifest (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES tm_scope(id) ON DELETE RESTRICT,
    episode_id TEXT NOT NULL,
    manifest_identity TEXT NOT NULL,
    publication_contract TEXT NOT NULL CHECK(publication_contract = 'ai-publication-semantics-v2'),
    publication_kind TEXT NOT NULL CHECK(publication_kind = 'translated_trilingual'),
    source_language TEXT NOT NULL CHECK(source_language = 'ja'),
    target_language TEXT NOT NULL CHECK(target_language = 'zh-CN'),
    strict_publication_verified INTEGER NOT NULL CHECK(strict_publication_verified = 1),
    qc_passed INTEGER NOT NULL CHECK(qc_passed = 1),
    unattended INTEGER NOT NULL CHECK(unattended = 1),
    manual_reviewed INTEGER NOT NULL CHECK(manual_reviewed = 0),
    safe_omission INTEGER NOT NULL CHECK(safe_omission = 0),
    source_manifest_hash TEXT NOT NULL,
    target_manifest_hash TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    block_count INTEGER NOT NULL CHECK(block_count > 0),
    term_count INTEGER NOT NULL CHECK(term_count >= 0),
    term_extractor_version INTEGER NOT NULL,
    verified_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_id, manifest_identity),
    UNIQUE(id, scope_id)
);

CREATE TABLE IF NOT EXISTS tm_observation (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES tm_scope(id) ON DELETE RESTRICT,
    manifest_id INTEGER NOT NULL,
    episode_id TEXT NOT NULL,
    block_identity TEXT NOT NULL,
    source_key TEXT NOT NULL,
    context_key TEXT NOT NULL,
    source_text TEXT NOT NULL,
    target_key TEXT NOT NULL,
    target_text TEXT NOT NULL,
    source_text_hash TEXT NOT NULL,
    target_text_hash TEXT NOT NULL,
    target_key_hash TEXT NOT NULL,
    qc_passed INTEGER NOT NULL CHECK(qc_passed = 1),
    manual_reviewed INTEGER NOT NULL CHECK(manual_reviewed = 0),
    safe_omission INTEGER NOT NULL CHECK(safe_omission = 0),
    verified_at TEXT NOT NULL,
    FOREIGN KEY(manifest_id, scope_id) REFERENCES tm_manifest(id, scope_id) ON DELETE RESTRICT,
    UNIQUE(manifest_id, block_identity)
);

CREATE TABLE IF NOT EXISTS tm_term_observation (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES tm_scope(id) ON DELETE RESTRICT,
    manifest_id INTEGER NOT NULL,
    episode_id TEXT NOT NULL,
    block_identity TEXT NOT NULL,
    context_key TEXT NOT NULL,
    source_term_key TEXT NOT NULL,
    source_term TEXT NOT NULL,
    target_term TEXT NOT NULL,
    source_term_hash TEXT NOT NULL,
    target_term_hash TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    FOREIGN KEY(manifest_id, scope_id) REFERENCES tm_manifest(id, scope_id) ON DELETE RESTRICT,
    UNIQUE(manifest_id, block_identity, source_term_key, target_term_hash)
);

CREATE INDEX IF NOT EXISTS idx_tm_observation_lookup
    ON tm_observation(scope_id, source_key, context_key, target_key_hash, episode_id);
CREATE INDEX IF NOT EXISTS idx_tm_observation_manifest
    ON tm_observation(manifest_id, block_identity);
CREATE INDEX IF NOT EXISTS idx_tm_term_lookup
    ON tm_term_observation(scope_id, source_term_key, target_term_hash, episode_id);
CREATE INDEX IF NOT EXISTS idx_tm_manifest_episode
    ON tm_manifest(scope_id, episode_id, verified_at);
"""


class TranslationMemoryStore:
    """SQLite-backed translation memory.

    A store instance owns one connection and is intentionally not thread-shared.
    Use ``readonly=True`` in the translation path.  Only the post-publication
    learner should open a writable store.
    """

    def __init__(self, database_path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(database_path)
        self.readonly = bool(readonly)
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> TranslationMemoryStore:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def open(self) -> TranslationMemoryStore:
        if self._connection is not None:
            return self
        try:
            if self.readonly:
                if not self.path.is_file():
                    raise TranslationMemoryError(
                        f"Translation-memory database does not exist: {self.path}"
                    )
                uri = self.path.resolve().as_uri() + "?mode=ro"
                connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=30,
                    isolation_level=None,
                )
                connection.execute("PRAGMA query_only=ON")
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    self.path,
                    timeout=30,
                    isolation_level=None,
                )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            if self.readonly:
                self._connection = connection
                self._validate_schema()
            else:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                self._connection = connection
                self._initialize_schema()
            return self
        except TranslationMemoryError:
            if "connection" in locals():
                connection.close()
            self._connection = None
            raise
        except (OSError, sqlite3.Error) as exc:
            if "connection" in locals():
                connection.close()
            self._connection = None
            raise TranslationMemoryError(
                f"Could not open translation-memory database {self.path}: {exc}"
            ) from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def learn_episode(
        self,
        scope: MemoryScope,
        evidence: VerifiedEpisodeTranslation,
    ) -> LearnResult:
        """Atomically learn one verified episode."""

        return self.learn_batch(scope, [evidence])[0]

    def learn_batch(
        self,
        scope: MemoryScope,
        evidences: Sequence[VerifiedEpisodeTranslation],
    ) -> tuple[LearnResult, ...]:
        """Atomically learn a batch of manifests.

        Every envelope and block is validated before ``BEGIN IMMEDIATE``.  A
        collision or SQLite failure rolls back all manifests in this call.
        """

        connection = self._require_connection()
        if self.readonly:
            raise ReadOnlyStoreError("Cannot learn through a read-only translation-memory store")
        validated_scope = _validate_scope(scope)
        items = tuple(evidences)
        if not items:
            return ()
        prepared = tuple(_prepare_evidence(validated_scope, item) for item in items)
        identities = [item.manifest_identity for item in prepared]
        if len(set(identities)) != len(identities):
            raise EvidenceRejected(
                "duplicate_manifest_identity",
                "a learn batch cannot contain the same manifest identity twice",
            )

        try:
            connection.execute("BEGIN IMMEDIATE")
            scope_id = self._scope_id(validated_scope, create=True)
            if scope_id is None:  # pragma: no cover - defensive invariant
                raise TranslationMemoryError("Could not create translation-memory scope")
            results = tuple(
                self._learn_prepared(scope_id, item)
                for item in prepared
            )
            connection.execute("COMMIT")
            return results
        except Exception as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, TranslationMemoryError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise TranslationMemoryError(
                    f"Atomic translation-memory learn failed: {exc}"
                ) from exc
            raise

    def lookup_batch(
        self,
        scope: MemoryScope,
        source_texts: Sequence[str],
        context_keys: Sequence[str],
        *,
        explicit_series_glossary: Mapping[str, str] | None = None,
    ) -> tuple[TranslationLookup, ...]:
        """Look up exact normalized ``(source, context)`` pairs read-only."""

        self._require_connection()
        validated_scope = _validate_scope(scope)
        prepared_sources = _prepare_context_lookups(source_texts, context_keys)
        glossary = _normalize_glossary(explicit_series_glossary or {})
        scope_id = self._scope_id(validated_scope, create=False)
        if scope_id is None:
            return tuple(
                _not_found_lookup(raw, key, context_key)
                for raw, key, context_key in prepared_sources
            )

        grouped = self._context_candidate_rows(
            scope_id=scope_id,
            lookup_keys={(key, context_key) for _, key, context_key in prepared_sources},
        )
        return tuple(
            _resolve_lookup(
                raw,
                key,
                context_key,
                grouped.get((key, context_key), ()),
                explicit_glossary=glossary,
                term_mode=False,
            )
            for raw, key, context_key in prepared_sources
        )

    def lookup_term_batch(
        self,
        scope: MemoryScope,
        source_terms: Sequence[str],
        *,
        explicit_series_glossary: Mapping[str, str] | None = None,
    ) -> tuple[TranslationLookup, ...]:
        """Look up conservative proper-name candidates in one read-only query path."""

        self._require_connection()
        validated_scope = _validate_scope(scope)
        prepared_sources = _prepare_lookup_sources(source_terms)
        glossary = _normalize_glossary(explicit_series_glossary or {})
        explicit_keys = set(glossary)
        scope_id = self._scope_id(validated_scope, create=False)
        if scope_id is None:
            return tuple(_not_found_lookup(raw, key, "") for raw, key in prepared_sources)

        grouped = self._candidate_rows(
            table="tm_term_observation",
            source_column="source_term_key",
            target_column="target_term",
            target_hash_column="target_term_hash",
            scope_id=scope_id,
            source_keys={key for _, key in prepared_sources},
        )
        results: list[TranslationLookup] = []
        for raw, key in prepared_sources:
            if key in explicit_keys:
                results.append(
                    TranslationLookup(
                        source_text=raw,
                        source_key=key,
                        context_key="",
                        status="explicit_glossary",
                        auto_target=None,
                        candidates=tuple(grouped.get(key, ())),
                    )
                )
                continue
            results.append(
                _resolve_lookup(
                    raw,
                    key,
                    "",
                    grouped.get(key, ()),
                    explicit_glossary=glossary,
                    term_mode=True,
                )
            )
        return tuple(results)

    def eligible_term_candidates(
        self,
        scope: MemoryScope,
        *,
        explicit_series_glossary: Mapping[str, str] | None = None,
        limit: int = 100,
    ) -> tuple[TermSuggestion, ...]:
        """Return mature term suggestions; this method never edits a glossary."""

        self._require_connection()
        validated_scope = _validate_scope(scope)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_TERM_RESULTS:
            raise TranslationMemoryError(
                f"term candidate limit must be between 1 and {MAX_TERM_RESULTS}"
            )
        glossary = _normalize_glossary(explicit_series_glossary or {})
        scope_id = self._scope_id(validated_scope, create=False)
        if scope_id is None:
            return ()
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT
                source_term_key,
                MIN(source_term) AS source_term,
                target_term,
                target_term_hash,
                COUNT(*) AS support_count,
                COUNT(DISTINCT episode_id) AS episode_diversity,
                MAX(verified_at) AS last_verified
            FROM tm_term_observation
            WHERE scope_id = ?
            GROUP BY source_term_key, target_term_hash, target_term
            ORDER BY source_term_key, episode_diversity DESC, support_count DESC,
                     target_term_hash, target_term
            """,
            (scope_id,),
        ).fetchall()
        by_source: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_source.setdefault(str(row["source_term_key"]), []).append(row)
        suggestions: list[TermSuggestion] = []
        for source_key in sorted(by_source):
            if source_key in glossary:
                continue
            candidates = by_source[source_key]
            if len(candidates) != 1:
                continue
            candidate = candidates[0]
            diversity = int(candidate["episode_diversity"])
            if diversity < MIN_AUTO_APPLY_EPISODE_DIVERSITY:
                continue
            suggestions.append(
                TermSuggestion(
                    source_term=str(candidate["source_term"]),
                    target_term=str(candidate["target_term"]),
                    target_hash=str(candidate["target_term_hash"]),
                    support_count=int(candidate["support_count"]),
                    episode_diversity=diversity,
                    last_verified=str(candidate["last_verified"]),
                )
            )
            if len(suggestions) >= limit:
                break
        return tuple(suggestions)

    def diagnostics(self, scope: MemoryScope | None = None) -> TranslationMemoryStats:
        """Return deterministic aggregate health and eligibility counters."""

        connection = self._require_connection()
        scope_id: int | None = None
        if scope is not None:
            scope_id = self._scope_id(_validate_scope(scope), create=False)
            if scope_id is None:
                return TranslationMemoryStats(*(0 for _ in range(12)), last_verified=None)

        manifest_where = "WHERE scope_id = ?" if scope_id is not None else ""
        manifest_params: tuple[Any, ...] = (scope_id,) if scope_id is not None else ()
        manifest_row = connection.execute(
            f"""
            SELECT COUNT(*) AS manifest_count,
                   COUNT(DISTINCT episode_id) AS episode_count,
                   MAX(verified_at) AS last_verified
            FROM tm_manifest
            {manifest_where}
            """,
            manifest_params,
        ).fetchone()

        observation = self._diagnostic_group_counts(
            table="tm_observation",
            source_column="source_key",
            target_hash_column="target_key_hash",
            scope_id=scope_id,
        )
        terms = self._diagnostic_group_counts(
            table="tm_term_observation",
            source_column="source_term_key",
            target_hash_column="target_term_hash",
            scope_id=scope_id,
        )
        observation_count = self._table_count("tm_observation", scope_id)
        term_observation_count = self._table_count("tm_term_observation", scope_id)
        return TranslationMemoryStats(
            manifest_count=int(manifest_row["manifest_count"] or 0),
            episode_count=int(manifest_row["episode_count"] or 0),
            observation_count=observation_count,
            source_key_count=observation[0],
            auto_apply_source_count=observation[1],
            conflict_source_count=observation[2],
            insufficient_source_count=observation[3],
            term_observation_count=term_observation_count,
            term_source_count=terms[0],
            auto_apply_term_count=terms[1],
            conflict_term_count=terms[2],
            insufficient_term_count=terms[3],
            last_verified=(
                str(manifest_row["last_verified"])
                if manifest_row["last_verified"] is not None
                else None
            ),
        )

    def audit_source(
        self,
        scope: MemoryScope,
        source_text: str,
        context_key: str,
    ) -> tuple[AuditObservation, ...]:
        """Return evidence for one exact normalized source/context key."""

        connection = self._require_connection()
        validated_scope = _validate_scope(scope)
        source_key = normalize_source_key(source_text)
        if not source_key:
            raise TranslationMemoryError("audit source text cannot be empty")
        validated_context_key = _validated_context_key(context_key)
        scope_id = self._scope_id(validated_scope, create=False)
        if scope_id is None:
            return ()
        rows = connection.execute(
            """
            SELECT m.episode_id, m.manifest_identity, m.publication_contract,
                    m.publication_kind, m.source_manifest_hash,
                    m.target_manifest_hash, o.block_identity, o.context_key, o.source_text,
                   o.target_text, o.source_text_hash, o.target_text_hash,
                   o.verified_at
            FROM tm_observation AS o
            JOIN tm_manifest AS m ON m.id = o.manifest_id
            WHERE o.scope_id = ? AND o.source_key = ? AND o.context_key = ?
            ORDER BY m.episode_id, m.manifest_identity, o.block_identity,
                     o.target_key_hash
            """,
            (scope_id, source_key, validated_context_key),
        ).fetchall()
        return tuple(
            AuditObservation(
                episode_id=str(row["episode_id"]),
                manifest_identity=str(row["manifest_identity"]),
                publication_contract=str(row["publication_contract"]),
                publication_kind=str(row["publication_kind"]),
                source_manifest_hash=str(row["source_manifest_hash"]),
                target_manifest_hash=str(row["target_manifest_hash"]),
                block_identity=str(row["block_identity"]),
                context_key=str(row["context_key"]),
                source_text=str(row["source_text"]),
                target_text=str(row["target_text"]),
                source_text_hash=str(row["source_text_hash"]),
                target_text_hash=str(row["target_text_hash"]),
                verified_at=str(row["verified_at"]),
            )
            for row in rows
        )

    def _initialize_schema(self) -> None:
        connection = self._require_connection()
        try:
            existing_tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if existing_tables:
                if "tm_meta" not in existing_tables:
                    raise TranslationMemoryError(
                        "Refusing to initialize translation memory over an unrelated or partial database"
                    )
                # Reject an incompatible or partial same-name database before
                # running any DDL. Schema migration must be an explicit action.
                self._validate_schema()
                return
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + _SCHEMA_SQL
                + "\nINSERT INTO tm_meta(key, value) VALUES('schema_version', '"
                + str(TRANSLATION_MEMORY_SCHEMA_VERSION)
                + "');\nINSERT INTO tm_meta(key, value) VALUES('context_contract', '"
                + TRANSLATION_MEMORY_CONTEXT_CONTRACT
                + "');\nCOMMIT;"
            )
            self._validate_schema()
        except TranslationMemoryError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise TranslationMemoryError(
                f"Could not initialize translation-memory schema: {exc}"
            ) from exc

    def _validate_schema(self) -> None:
        connection = self._require_connection()
        try:
            row = connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or str(row["value"]) != str(TRANSLATION_MEMORY_SCHEMA_VERSION):
                raise TranslationMemoryError(
                    "Unsupported or missing translation-memory schema version"
                )
            contract = connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'context_contract'"
            ).fetchone()
            if (
                contract is None
                or str(contract["value"]) != TRANSLATION_MEMORY_CONTEXT_CONTRACT
            ):
                raise TranslationMemoryError(
                    "Unsupported or missing translation-memory context contract"
                )
            required = {
                "tm_meta",
                "tm_scope",
                "tm_manifest",
                "tm_observation",
                "tm_term_observation",
            }
            present = {
                str(item["name"])
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing = sorted(required - present)
            if missing:
                raise TranslationMemoryError(
                    "Translation-memory schema is incomplete: " + ", ".join(missing)
                )
            required_columns = {
                "tm_observation": {
                    "scope_id", "manifest_id", "episode_id", "block_identity",
                    "source_key", "context_key", "source_text", "target_key",
                    "target_text", "source_text_hash", "target_text_hash",
                    "target_key_hash", "qc_passed", "manual_reviewed",
                    "safe_omission", "verified_at",
                },
                "tm_term_observation": {
                    "scope_id", "manifest_id", "episode_id", "block_identity",
                    "context_key", "source_term_key", "source_term", "target_term",
                    "source_term_hash", "target_term_hash", "verified_at",
                },
            }
            for table, columns in required_columns.items():
                actual = {
                    str(item["name"])
                    for item in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                absent = sorted(columns - actual)
                if absent:
                    raise TranslationMemoryError(
                        f"Translation-memory table {table} lacks v2 columns: "
                        + ", ".join(absent)
                    )
            required_indexes = {
                "idx_tm_observation_lookup": (
                    "scope_id", "source_key", "context_key", "target_key_hash", "episode_id"
                ),
                "idx_tm_observation_manifest": ("manifest_id", "block_identity"),
                "idx_tm_term_lookup": (
                    "scope_id", "source_term_key", "target_term_hash", "episode_id"
                ),
                "idx_tm_manifest_episode": ("scope_id", "episode_id", "verified_at"),
            }
            for index_name, expected_columns in required_indexes.items():
                index_row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
                    (index_name,),
                ).fetchone()
                if index_row is None:
                    raise TranslationMemoryError(
                        f"Translation-memory required index {index_name} is missing"
                    )
                actual_columns = tuple(
                    str(item["name"])
                    for item in connection.execute(
                        f"PRAGMA index_info({index_name})"
                    ).fetchall()
                )
                if actual_columns != expected_columns:
                    raise TranslationMemoryError(
                        f"Translation-memory index {index_name} is incompatible"
                    )
        except sqlite3.Error as exc:
            raise TranslationMemoryError(
                f"Could not validate translation-memory schema: {exc}"
            ) from exc

    def _scope_id(self, scope: MemoryScope, *, create: bool) -> int | None:
        connection = self._require_connection()
        if create:
            connection.execute(
                """
                INSERT OR IGNORE INTO tm_scope(
                    series_key, policy_version, source_language,
                    target_language, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    scope.series_key,
                    scope.policy_version,
                    scope.source_language,
                    scope.target_language,
                    _utc_now(),
                ),
            )
        row = connection.execute(
            """
            SELECT id FROM tm_scope
            WHERE series_key = ? AND policy_version = ?
              AND source_language = ? AND target_language = ?
            """,
            (
                scope.series_key,
                scope.policy_version,
                scope.source_language,
                scope.target_language,
            ),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def _learn_prepared(self, scope_id: int, evidence: _PreparedEvidence) -> LearnResult:
        connection = self._require_connection()
        existing = connection.execute(
            """
            SELECT id, evidence_hash, block_count, term_count, verified_at
            FROM tm_manifest
            WHERE scope_id = ? AND manifest_identity = ?
            """,
            (scope_id, evidence.manifest_identity),
        ).fetchone()
        if existing is not None:
            if str(existing["evidence_hash"]) != evidence.evidence_hash:
                raise EvidenceRejected(
                    "manifest_identity_collision",
                    f"manifest {evidence.manifest_identity!r} already has different evidence",
                )
            self._verify_idempotent_manifest(existing, evidence)
            refreshed = evidence.verified_at > str(existing["verified_at"])
            if refreshed:
                manifest_id = int(existing["id"])
                connection.execute(
                    "UPDATE tm_manifest SET verified_at = ? WHERE id = ?",
                    (evidence.verified_at, manifest_id),
                )
                connection.execute(
                    "UPDATE tm_observation SET verified_at = ? WHERE manifest_id = ?",
                    (evidence.verified_at, manifest_id),
                )
                connection.execute(
                    "UPDATE tm_term_observation SET verified_at = ? WHERE manifest_id = ?",
                    (evidence.verified_at, manifest_id),
                )
            return LearnResult(
                status="idempotent",
                manifest_identity=evidence.manifest_identity,
                inserted_blocks=0,
                inserted_term_observations=0,
                verification_refreshed=refreshed,
            )

        cursor = connection.execute(
            """
            INSERT INTO tm_manifest(
                scope_id, episode_id, manifest_identity, publication_contract,
                publication_kind, source_language, target_language,
                strict_publication_verified, qc_passed, unattended,
                manual_reviewed, safe_omission, source_manifest_hash,
                target_manifest_hash, evidence_hash, block_count, term_count,
                term_extractor_version, verified_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope_id,
                evidence.episode_id,
                evidence.manifest_identity,
                evidence.publication_contract,
                evidence.publication_kind,
                evidence.source_language,
                evidence.target_language,
                evidence.source_manifest_hash,
                evidence.target_manifest_hash,
                evidence.evidence_hash,
                len(evidence.blocks),
                len(evidence.terms),
                TERM_EXTRACTOR_VERSION,
                evidence.verified_at,
                _utc_now(),
            ),
        )
        manifest_id = int(cursor.lastrowid)
        for block in evidence.blocks:
            connection.execute(
                """
                INSERT INTO tm_observation(
                    scope_id, manifest_id, episode_id, block_identity,
                    source_key, context_key, source_text, target_key, target_text,
                    source_text_hash, target_text_hash, target_key_hash,
                    qc_passed, manual_reviewed, safe_omission, verified_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?)
                """,
                (
                    scope_id,
                    manifest_id,
                    evidence.episode_id,
                    block.block_identity,
                    block.source_key,
                    block.context_key,
                    block.source_text,
                    block.target_key,
                    block.target_text,
                    block.source_text_hash,
                    block.target_text_hash,
                    block.target_key_hash,
                    evidence.verified_at,
                ),
            )
        for term in evidence.terms:
            connection.execute(
                """
                INSERT INTO tm_term_observation(
                    scope_id, manifest_id, episode_id, block_identity,
                    context_key, source_term_key, source_term, target_term,
                    source_term_hash, target_term_hash, verified_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope_id,
                    manifest_id,
                    evidence.episode_id,
                    term.block_identity,
                    term.context_key,
                    term.source_term_key,
                    term.source_term,
                    term.target_term,
                    term.source_term_hash,
                    term.target_term_hash,
                    evidence.verified_at,
                ),
            )
        return LearnResult(
            status="learned",
            manifest_identity=evidence.manifest_identity,
            inserted_blocks=len(evidence.blocks),
            inserted_term_observations=len(evidence.terms),
            verification_refreshed=False,
        )

    def _verify_idempotent_manifest(
        self,
        existing: sqlite3.Row,
        evidence: _PreparedEvidence,
    ) -> None:
        if (
            int(existing["block_count"]) != len(evidence.blocks)
            or int(existing["term_count"]) != len(evidence.terms)
        ):
            raise TranslationMemoryError(
                f"Stored manifest {evidence.manifest_identity!r} has inconsistent row counts"
            )
        manifest_id = int(existing["id"])
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT block_identity, source_text_hash, target_text_hash,
                   source_key, context_key, target_key_hash
            FROM tm_observation WHERE manifest_id = ?
            ORDER BY block_identity, source_text_hash, target_text_hash
            """,
            (manifest_id,),
        ).fetchall()
        actual = tuple(
            (
                str(row["block_identity"]),
                str(row["source_text_hash"]),
                str(row["target_text_hash"]),
                str(row["source_key"]),
                str(row["context_key"]),
                str(row["target_key_hash"]),
            )
            for row in rows
        )
        expected = tuple(
            sorted(
                (
                    block.block_identity,
                    block.source_text_hash,
                    block.target_text_hash,
                    block.source_key,
                    block.context_key,
                    block.target_key_hash,
                )
                for block in evidence.blocks
            )
        )
        if actual != expected:
            raise TranslationMemoryError(
                f"Stored manifest {evidence.manifest_identity!r} observations are inconsistent"
            )

    def _candidate_rows(
        self,
        *,
        table: str,
        source_column: str,
        target_column: str,
        target_hash_column: str,
        scope_id: int,
        source_keys: set[str],
    ) -> dict[str, tuple[CandidateEvidence, ...]]:
        if not source_keys:
            return {}
        allowed = {
            ("tm_observation", "source_key", "target_key", "target_key_hash"),
            (
                "tm_term_observation",
                "source_term_key",
                "target_term",
                "target_term_hash",
            ),
        }
        if (table, source_column, target_column, target_hash_column) not in allowed:
            raise TranslationMemoryError("Unsafe translation-memory query shape")
        connection = self._require_connection()
        grouped: dict[str, list[CandidateEvidence]] = {}
        ordered = sorted(source_keys)
        for start in range(0, len(ordered), 400):
            chunk = ordered[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT {source_column} AS source_key,
                       {target_column} AS target_text,
                       {target_hash_column} AS target_hash,
                       COUNT(*) AS support_count,
                       COUNT(DISTINCT episode_id) AS episode_diversity,
                       MAX(verified_at) AS last_verified
                FROM {table}
                WHERE scope_id = ? AND {source_column} IN ({placeholders})
                GROUP BY {source_column}, {target_hash_column}, {target_column}
                ORDER BY {source_column}, episode_diversity DESC,
                         support_count DESC, {target_hash_column}, {target_column}
                """,
                (scope_id, *chunk),
            ).fetchall()
            for row in rows:
                grouped.setdefault(str(row["source_key"]), []).append(
                    CandidateEvidence(
                        target_text=str(row["target_text"]),
                        target_hash=str(row["target_hash"]),
                        support_count=int(row["support_count"]),
                        episode_diversity=int(row["episode_diversity"]),
                        last_verified=str(row["last_verified"]),
                    )
                )
        return {key: tuple(value) for key, value in grouped.items()}

    def _context_candidate_rows(
        self,
        *,
        scope_id: int,
        lookup_keys: set[tuple[str, str]],
    ) -> dict[tuple[str, str], tuple[CandidateEvidence, ...]]:
        if not lookup_keys:
            return {}
        connection = self._require_connection()
        grouped: dict[tuple[str, str], list[CandidateEvidence]] = {}
        ordered = sorted(lookup_keys)
        # 350 pairs stay below conservative SQLite bind-variable limits.
        for start in range(0, len(ordered), 350):
            chunk = ordered[start : start + 350]
            predicates = " OR ".join(
                "(source_key = ? AND context_key = ?)" for _ in chunk
            )
            parameters: list[Any] = [scope_id]
            for source_key, context_key in chunk:
                parameters.extend((source_key, context_key))
            rows = connection.execute(
                f"""
                SELECT source_key, context_key, target_key AS target_text,
                       target_key_hash AS target_hash,
                       COUNT(*) AS support_count,
                       COUNT(DISTINCT episode_id) AS episode_diversity,
                       MAX(verified_at) AS last_verified
                FROM tm_observation
                WHERE scope_id = ? AND ({predicates})
                GROUP BY source_key, context_key, target_key_hash, target_key
                ORDER BY source_key, context_key, episode_diversity DESC,
                         support_count DESC, target_key_hash, target_key
                """,
                tuple(parameters),
            ).fetchall()
            for row in rows:
                pair = (str(row["source_key"]), str(row["context_key"]))
                grouped.setdefault(pair, []).append(
                    CandidateEvidence(
                        target_text=str(row["target_text"]),
                        target_hash=str(row["target_hash"]),
                        support_count=int(row["support_count"]),
                        episode_diversity=int(row["episode_diversity"]),
                        last_verified=str(row["last_verified"]),
                    )
                )
        return {key: tuple(value) for key, value in grouped.items()}

    def _diagnostic_group_counts(
        self,
        *,
        table: str,
        source_column: str,
        target_hash_column: str,
        scope_id: int | None,
    ) -> tuple[int, int, int, int]:
        allowed = {
            ("tm_term_observation", "source_term_key", "target_term_hash"),
        }
        observation_shape = (
            table,
            source_column,
            target_hash_column,
        ) == ("tm_observation", "source_key", "target_key_hash")
        if not observation_shape and (table, source_column, target_hash_column) not in allowed:
            raise TranslationMemoryError("Unsafe diagnostic query shape")
        where = "WHERE scope_id = ?" if scope_id is not None else ""
        params: tuple[Any, ...] = (scope_id,) if scope_id is not None else ()
        row = self._require_connection().execute(
            f"""
            SELECT COUNT(*) AS source_count,
                   COALESCE(SUM(CASE WHEN variants = 1 AND diversity >= ? THEN 1 ELSE 0 END), 0)
                       AS auto_count,
                   COALESCE(SUM(CASE WHEN variants > 1 THEN 1 ELSE 0 END), 0)
                       AS conflict_count,
                   COALESCE(SUM(CASE WHEN variants = 1 AND diversity < ? THEN 1 ELSE 0 END), 0)
                       AS insufficient_count
            FROM (
                SELECT scope_id, {source_column},
                       COUNT(DISTINCT {target_hash_column}) AS variants,
                       COUNT(DISTINCT episode_id) AS diversity
                FROM {table}
                {where}
                 GROUP BY scope_id, {source_column}
                    {', context_key' if observation_shape else ''}
            )
            """,
            (
                MIN_AUTO_APPLY_EPISODE_DIVERSITY,
                MIN_AUTO_APPLY_EPISODE_DIVERSITY,
                *params,
            ),
        ).fetchone()
        return (
            int(row["source_count"] or 0),
            int(row["auto_count"] or 0),
            int(row["conflict_count"] or 0),
            int(row["insufficient_count"] or 0),
        )

    def _table_count(self, table: str, scope_id: int | None) -> int:
        if table not in {"tm_observation", "tm_term_observation"}:
            raise TranslationMemoryError("Unsafe table count query")
        where = " WHERE scope_id = ?" if scope_id is not None else ""
        params: tuple[Any, ...] = (scope_id,) if scope_id is not None else ()
        row = self._require_connection().execute(
            f"SELECT COUNT(*) AS count FROM {table}{where}",
            params,
        ).fetchone()
        return int(row["count"] or 0)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            return self.open()._require_connection()
        return self._connection


def learn_verified_batch(
    database_path: str | Path,
    scope: MemoryScope,
    evidences: Sequence[VerifiedEpisodeTranslation],
) -> tuple[LearnResult, ...]:
    """One-shot writable batch-learning integration API."""

    with TranslationMemoryStore(database_path) as store:
        return store.learn_batch(scope, evidences)


def lookup_translations_readonly(
    database_path: str | Path,
    scope: MemoryScope,
    source_texts: Sequence[str],
    context_keys: Sequence[str],
    *,
    explicit_series_glossary: Mapping[str, str] | None = None,
) -> tuple[TranslationLookup, ...]:
    """One-shot read-only exact translation lookup integration API."""

    with TranslationMemoryStore(database_path, readonly=True) as store:
        return store.lookup_batch(
            scope,
            source_texts,
            context_keys,
            explicit_series_glossary=explicit_series_glossary,
        )


def normalize_source_key(text: str) -> str:
    """Normalize Unicode and whitespace while preserving exact lexical content."""

    if not isinstance(text, str):
        raise TranslationMemoryError("source text must be a string")
    return _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def sha256_text(text: str) -> str:
    """Return the audit hash used for raw source and target block text."""

    if not isinstance(text, str):
        raise TranslationMemoryError("hash input must be a string")
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def build_context_key(
    previous_source_text: str | None,
    next_source_text: str | None,
    *,
    speaker_identity: str | None = None,
) -> str:
    """Build the only accepted deterministic context-key representation.

    ``None`` is a boundary sentinel and remains distinct from an empty cue.
    Normalization matches source-key normalization so whitespace-only rendering
    differences do not fork otherwise identical context evidence.
    """

    def component(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TranslationMemoryError(f"{field} must be a string or None")
        if "\x00" in value or len(value) > MAX_TEXT_CHARS:
            raise TranslationMemoryError(
                f"{field} contains NUL or exceeds {MAX_TEXT_CHARS} characters"
            )
        normalized = normalize_source_key(value)
        if not normalized:
            raise TranslationMemoryError(f"{field} cannot be empty when provided")
        return normalized

    payload = {
        "contract": TRANSLATION_MEMORY_CONTEXT_CONTRACT,
        "next_source": component(next_source_text, "next_source_text"),
        "previous_source": component(previous_source_text, "previous_source_text"),
        "speaker": component(speaker_identity, "speaker_identity"),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return CONTEXT_KEY_PREFIX + digest


def _validate_scope(scope: MemoryScope) -> MemoryScope:
    if not isinstance(scope, MemoryScope):
        raise TranslationMemoryError("scope must be a MemoryScope")
    series_key = _identity(scope.series_key, "series_key")
    policy_version = _identity(scope.policy_version, "policy_version")
    if scope.source_language != SUPPORTED_SOURCE_LANGUAGE:
        raise EvidenceRejected(
            "unsupported_source_language",
            f"only {SUPPORTED_SOURCE_LANGUAGE!r} evidence may be learned",
        )
    if scope.target_language != SUPPORTED_TARGET_LANGUAGE:
        raise EvidenceRejected(
            "unsupported_target_language",
            f"only {SUPPORTED_TARGET_LANGUAGE!r} evidence may be learned",
        )
    return MemoryScope(
        series_key=series_key,
        policy_version=policy_version,
        source_language=scope.source_language,
        target_language=scope.target_language,
    )


def _prepare_evidence(
    scope: MemoryScope,
    evidence: VerifiedEpisodeTranslation,
) -> _PreparedEvidence:
    if not isinstance(evidence, VerifiedEpisodeTranslation):
        raise EvidenceRejected("invalid_evidence_type", "evidence must be VerifiedEpisodeTranslation")
    series_key = _identity(evidence.series_key, "evidence.series_key")
    policy_version = _identity(evidence.policy_version, "evidence.policy_version")
    if series_key != scope.series_key or policy_version != scope.policy_version:
        raise EvidenceRejected(
            "scope_mismatch",
            "evidence series and policy must exactly match the memory scope",
        )
    if evidence.source_language != scope.source_language:
        raise EvidenceRejected("source_language_mismatch", "evidence source language is not ja")
    if evidence.target_language != scope.target_language:
        raise EvidenceRejected("target_language_mismatch", "evidence target language is not zh-CN")
    if evidence.publication_contract != STRICT_PUBLICATION_CONTRACT:
        raise EvidenceRejected(
            "publication_contract_rejected",
            f"publication contract must be {STRICT_PUBLICATION_CONTRACT!r}",
        )
    if evidence.publication_kind != STRICT_PUBLICATION_KIND:
        code = (
            "source_language_publication_rejected"
            if evidence.publication_kind == "source_language"
            else "publication_kind_rejected"
        )
        raise EvidenceRejected(
            code,
            f"publication kind must be {STRICT_PUBLICATION_KIND!r}",
        )
    if evidence.strict_publication_verified is not True:
        raise EvidenceRejected(
            "strict_publication_not_verified",
            "strict publication evidence is required",
        )
    if evidence.qc_passed is not True:
        raise EvidenceRejected("publication_qc_failed", "publication QC must pass")
    if evidence.unattended is not True:
        raise EvidenceRejected("not_unattended", "reviewed or indeterminate output cannot train memory")
    if evidence.manual_reviewed is not False:
        raise EvidenceRejected("manual_review_rejected", "manual-review output cannot train memory")
    if evidence.safe_omission is not False:
        raise EvidenceRejected("safe_omission_rejected", "safe-omission output cannot train memory")

    episode_id = _identity(evidence.episode_id, "episode_id")
    manifest_identity = _identity(evidence.manifest_identity, "manifest_identity")
    source_manifest_hash = _validated_hash(
        evidence.source_manifest_hash,
        "source_manifest_hash",
    )
    target_manifest_hash = _validated_hash(
        evidence.target_manifest_hash,
        "target_manifest_hash",
    )
    verified_at = _normalized_timestamp(evidence.verified_at)
    raw_blocks = tuple(evidence.blocks)
    if not raw_blocks:
        raise EvidenceRejected("empty_episode", "at least one aligned block is required")
    if len(raw_blocks) > MAX_BLOCKS_PER_EPISODE:
        raise EvidenceRejected(
            "episode_too_large",
            f"an episode cannot exceed {MAX_BLOCKS_PER_EPISODE} aligned blocks",
        )

    blocks: list[_PreparedBlock] = []
    block_identities: set[str] = set()
    terms: list[_PreparedTerm] = []
    for raw in raw_blocks:
        if not isinstance(raw, AlignedTranslationBlock):
            raise EvidenceRejected("invalid_block_type", "every block must be AlignedTranslationBlock")
        block_identity = _identity(raw.block_identity, "block_identity")
        if block_identity in block_identities:
            raise EvidenceRejected(
                "duplicate_block_identity",
                f"block identity {block_identity!r} occurs more than once",
            )
        block_identities.add(block_identity)
        if raw.qc_passed is not True:
            raise EvidenceRejected("block_qc_failed", f"block {block_identity!r} failed QC")
        if raw.manual_reviewed is not False:
            raise EvidenceRejected(
                "block_manual_review_rejected",
                f"block {block_identity!r} was manually reviewed",
            )
        if raw.safe_omission is not False:
            raise EvidenceRejected(
                "block_safe_omission_rejected",
                f"block {block_identity!r} is a safe omission",
            )
        source_text = _validated_text(raw.source_text, "source_text", block_identity)
        target_text = _validated_text(raw.target_text, "target_text", block_identity)
        context_key = _validated_context_key(raw.context_key, block_identity=block_identity)
        source_key = normalize_source_key(source_text)
        target_key = normalize_source_key(target_text)
        if not source_key or not target_key:
            raise EvidenceRejected(
                "empty_aligned_text",
                f"block {block_identity!r} has empty normalized text",
            )
        if target_key in SAFE_OMISSION_PLACEHOLDERS:
            raise EvidenceRejected(
                "safe_omission_placeholder_rejected",
                f"block {block_identity!r} contains the safe-omission placeholder",
            )
        block = _PreparedBlock(
            block_identity=block_identity,
            source_text=source_text,
            target_text=target_text,
            source_key=source_key,
            context_key=context_key,
            target_key=target_key,
            source_text_hash=sha256_text(source_text),
            target_text_hash=sha256_text(target_text),
            target_key_hash=sha256_text(target_key),
        )
        blocks.append(block)
        if len(terms) < MAX_TERM_OBSERVATIONS_PER_EPISODE:
            term_pair = _conservative_term_pair(source_key, target_key)
            if term_pair is not None:
                source_term, target_term = term_pair
                terms.append(
                    _PreparedTerm(
                        block_identity=block_identity,
                        context_key=context_key,
                        source_term=source_term,
                        target_term=target_term,
                        source_term_key=normalize_source_key(source_term),
                        source_term_hash=sha256_text(source_term),
                        target_term_hash=sha256_text(normalize_source_key(target_term)),
                    )
                )

    canonical = {
        "schema_version": TRANSLATION_MEMORY_SCHEMA_VERSION,
        "term_extractor_version": TERM_EXTRACTOR_VERSION,
        "series_key": series_key,
        "policy_version": policy_version,
        "episode_id": episode_id,
        "manifest_identity": manifest_identity,
        "publication_contract": evidence.publication_contract,
        "publication_kind": evidence.publication_kind,
        "source_language": evidence.source_language,
        "target_language": evidence.target_language,
        "strict_publication_verified": True,
        "qc_passed": True,
        "unattended": True,
        "manual_reviewed": False,
        "safe_omission": False,
        "source_manifest_hash": source_manifest_hash,
        "target_manifest_hash": target_manifest_hash,
        "blocks": [
            {
                "block_identity": block.block_identity,
                "source_text_hash": block.source_text_hash,
                "target_text_hash": block.target_text_hash,
                "source_key": block.source_key,
                "context_key": block.context_key,
                "target_key_hash": block.target_key_hash,
            }
            for block in blocks
        ],
    }
    evidence_hash = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return _PreparedEvidence(
        series_key=series_key,
        policy_version=policy_version,
        episode_id=episode_id,
        manifest_identity=manifest_identity,
        publication_contract=evidence.publication_contract,
        publication_kind=evidence.publication_kind,
        source_language=evidence.source_language,
        target_language=evidence.target_language,
        source_manifest_hash=source_manifest_hash,
        target_manifest_hash=target_manifest_hash,
        verified_at=verified_at,
        evidence_hash=evidence_hash,
        blocks=tuple(blocks),
        terms=tuple(terms),
    )


def _prepare_lookup_sources(source_texts: Sequence[str]) -> tuple[tuple[str, str], ...]:
    items = tuple(source_texts)
    if len(items) > MAX_LOOKUP_BATCH:
        raise TranslationMemoryError(f"lookup batch cannot exceed {MAX_LOOKUP_BATCH} entries")
    prepared: list[tuple[str, str]] = []
    for raw in items:
        if not isinstance(raw, str):
            raise TranslationMemoryError("every lookup source must be a string")
        key = normalize_source_key(raw)
        if not key:
            raise TranslationMemoryError("lookup source text cannot be empty")
        if len(raw) > MAX_TEXT_CHARS:
            raise TranslationMemoryError(f"lookup source exceeds {MAX_TEXT_CHARS} characters")
        prepared.append((raw, key))
    return tuple(prepared)


def _prepare_context_lookups(
    source_texts: Sequence[str],
    context_keys: Sequence[str],
) -> tuple[tuple[str, str, str], ...]:
    prepared_sources = _prepare_lookup_sources(source_texts)
    raw_contexts = tuple(context_keys)
    if len(raw_contexts) != len(prepared_sources):
        raise TranslationMemoryError(
            "lookup context count must exactly match source text count"
        )
    return tuple(
        (raw, source_key, _validated_context_key(context_key))
        for (raw, source_key), context_key in zip(prepared_sources, raw_contexts)
    )


def _resolve_lookup(
    raw: str,
    source_key: str,
    context_key: str,
    candidates: Sequence[CandidateEvidence],
    *,
    explicit_glossary: Mapping[str, str],
    term_mode: bool,
) -> TranslationLookup:
    ordered = tuple(candidates)
    if not ordered:
        return _not_found_lookup(raw, source_key, context_key)
    if len(ordered) > 1:
        return TranslationLookup(raw, source_key, context_key, "conflict", None, ordered)
    candidate = ordered[0]
    if candidate.episode_diversity < MIN_AUTO_APPLY_EPISODE_DIVERSITY:
        return TranslationLookup(
            raw, source_key, context_key, "insufficient_support", None, ordered
        )
    if _violates_explicit_glossary(
        source_key,
        candidate.target_text,
        explicit_glossary,
        term_mode=term_mode,
    ):
        return TranslationLookup(
            raw, source_key, context_key, "explicit_glossary_conflict", None, ordered
        )
    return TranslationLookup(
        raw, source_key, context_key, "auto_apply", candidate.target_text, ordered
    )


def _not_found_lookup(raw: str, source_key: str, context_key: str) -> TranslationLookup:
    return TranslationLookup(raw, source_key, context_key, "not_found", None, ())


def _normalize_glossary(glossary: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_source, raw_target in glossary.items():
        if not isinstance(raw_source, str) or not isinstance(raw_target, str):
            raise TranslationMemoryError("explicit glossary keys and values must be strings")
        source = normalize_source_key(raw_source)
        target = normalize_source_key(raw_target)
        if not source or not target:
            raise TranslationMemoryError("explicit glossary keys and values cannot be empty")
        previous = normalized.get(source)
        if previous is not None and previous != target:
            raise TranslationMemoryError(f"explicit glossary has a conflict for {source!r}")
        normalized[source] = target
    return normalized


def _violates_explicit_glossary(
    source_text: str,
    target_text: str,
    glossary: Mapping[str, str],
    *,
    term_mode: bool,
) -> bool:
    normalized_target = normalize_source_key(target_text)
    for source_term, required_target in glossary.items():
        applies = source_text == source_term if term_mode else source_term in source_text
        if applies and required_target not in normalized_target:
            return True
    return False


def _conservative_term_pair(source_key: str, target_key: str) -> tuple[str, str] | None:
    source = source_key
    target = target_key
    for opening, closing in _QUOTED_TERM_PAIRS:
        if (
            source.startswith(opening)
            and source.endswith(closing)
            and target.startswith(opening)
            and target.endswith(closing)
            and len(source) > len(opening) + len(closing)
            and len(target) > len(opening) + len(closing)
        ):
            source = source[len(opening) : len(source) - len(closing)].strip()
            target = target[len(opening) : len(target) - len(closing)].strip()
            break
    if not _KATAKANA_TERM_RE.fullmatch(source):
        return None
    if not _COMPACT_CHINESE_TERM_RE.fullmatch(target) or not _HAS_HAN_RE.search(target):
        return None
    return source, target


def _identity(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceRejected("invalid_identity", f"{field} must be a string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise EvidenceRejected("empty_identity", f"{field} cannot be empty")
    if "\x00" in normalized or len(normalized) > MAX_IDENTITY_CHARS:
        raise EvidenceRejected(
            "invalid_identity",
            f"{field} contains NUL or exceeds {MAX_IDENTITY_CHARS} characters",
        )
    return normalized


def _validated_text(value: str, field: str, block_identity: str) -> str:
    if not isinstance(value, str):
        raise EvidenceRejected(
            "invalid_aligned_text",
            f"{field} for block {block_identity!r} must be a string",
        )
    if not value.strip():
        raise EvidenceRejected(
            "empty_aligned_text",
            f"{field} for block {block_identity!r} cannot be empty",
        )
    if "\x00" in value or len(value) > MAX_TEXT_CHARS:
        raise EvidenceRejected(
            "invalid_aligned_text",
            f"{field} for block {block_identity!r} contains NUL or exceeds {MAX_TEXT_CHARS} characters",
        )
    return value


def _validated_context_key(
    value: str,
    *,
    block_identity: str | None = None,
) -> str:
    location = f" for block {block_identity!r}" if block_identity is not None else ""
    if not isinstance(value, str) or not _CONTEXT_KEY_RE.fullmatch(value):
        raise EvidenceRejected(
            "invalid_context_key",
            f"context_key{location} must use {CONTEXT_KEY_PREFIX}<sha256>",
        )
    return value


def _validated_hash(value: str, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise EvidenceRejected("invalid_hash", f"{field} must be a 64-character SHA-256 hex digest")
    return value.casefold()


def _normalized_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceRejected("invalid_verified_at", "verified_at must be an ISO-8601 timestamp")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EvidenceRejected(
            "invalid_verified_at",
            "verified_at must be an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceRejected("invalid_verified_at", "verified_at must include a timezone")
    utc = parsed.astimezone(timezone.utc)
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "AlignedTranslationBlock",
    "AuditObservation",
    "CandidateEvidence",
    "CONTEXT_KEY_PREFIX",
    "CONTEXT_KEY_VERSION",
    "EvidenceRejected",
    "LearnResult",
    "MAX_TERM_OBSERVATIONS_PER_EPISODE",
    "MIN_AUTO_APPLY_EPISODE_DIVERSITY",
    "MemoryScope",
    "ReadOnlyStoreError",
    "STRICT_PUBLICATION_CONTRACT",
    "STRICT_PUBLICATION_KIND",
    "TRANSLATION_MEMORY_CONTEXT_CONTRACT",
    "TRANSLATION_MEMORY_SCHEMA_VERSION",
    "TermSuggestion",
    "TranslationLookup",
    "TranslationMemoryError",
    "TranslationMemoryStats",
    "TranslationMemoryStore",
    "VerifiedEpisodeTranslation",
    "build_context_key",
    "learn_verified_batch",
    "lookup_translations_readonly",
    "normalize_source_key",
    "sha256_text",
]
