from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from typing import Any, Callable, Sequence
import uuid

from acceptance_queue_lane import (
    ACCEPTANCE_QUEUE_TARGET_COUNT,
    AcceptanceQueueTarget,
)
from mikan_source import extract_episode_number, release_season_number
from subtitle_extract import SIDECAR_SUBTITLE_EXTENSIONS
from subtitle_paths import finished_subtitle_paths


SCHEMA_VERSION = 1
SQLITE_BUSY_TIMEOUT_SECONDS = 60
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
SQLITE_JOURNAL_MODE = "WAL"
SQLITE_SYNCHRONOUS = "NORMAL"
SQLITE_WAL_AUTOCHECKPOINT_PAGES = 1000
AI_STAGE_EVENT_MAX_ROWS = 25_000
AI_STAGE_EVENT_RETENTION_DAYS = 30
AI_STAGE_EVENT_PRUNE_INTERVAL = 500
AI_DELIVERY_SCHEMA_VERSION = 1
AI_DELIVERY_DEADLINE_SECONDS = 72 * 60 * 60
AI_DELIVERY_SLO_WINDOW_SECONDS = 30 * 24 * 60 * 60
AI_DELIVERY_SLO_TARGET = 0.9999
AI_DELIVERY_SLO_MINIMUM_SAMPLE = 10_000
AI_DELIVERY_MEASUREMENT_REVISION = (
    "ai-delivery-99.99-strict-traditional-chinese-source-priority-full-inventory-continuous-anytime-eprocess-v5"
)
AI_DELIVERY_PUBLICATION_CONTRACT = "ai-publication-semantics-v2"
AI_DELIVERY_TRANSLATED_CHINESE_LANGUAGES = ("zh-CN", "zh-TW")
AI_DELIVERY_NON_DELIVERABLE_SOURCE_LANGUAGES = frozenset(
    {"", "auto", "ja", "jpn", "und", "unknown"}
)
AI_DELIVERY_TRADITIONAL_CHINESE_LANGUAGES = ("zh-TW",)
AI_DELIVERY_TRADITIONAL_CHINESE_PUBLICATION_KINDS = frozenset(
    {"translated_trilingual", "adopted_zh_tw", "converted_zh_cn"}
)
AI_DELIVERY_EXCLUSION_CODES = frozenset(
    {
        "official_subtitle_present_before_attempt",
        "local_chinese_subtitle_present_before_attempt",
        "embedded_chinese_subtitle_present_before_attempt",
        "standalone_theme_policy",
        "unsupported_media_before_attempt",
        "media_missing_before_attempt",
        "superseded_before_attempt",
    }
)
AI_DELIVERY_ATTEMPT_STATUSES = frozenset(
    {
        "running",
        "succeeded",
        "retryable_failure",
        "review_required",
        "deferred",
        "failed",
    }
)
AI_QUEUE_AUTOMATIC_RETRY_STRATEGIES = frozenset(
    {
        "bounded_retry",
        "lower_memory_same_pipeline",
        "same_pipeline",
    }
)
AI_QUEUE_RETRY_ATTEMPT_LINK_MAX_SKEW_SECONDS = 60
AI_INVENTORY_SCHEMA_VERSION = 1
AI_INVENTORY_MAX_AGE_SECONDS = 28_800
AI_INVENTORY_RUNNING_STALE_SECONDS = 7_200
AI_DELIVERY_ANYTIME_ALPHA = 0.05
AI_DELIVERY_ANYTIME_LOG_THRESHOLD = math.log(1.0 / AI_DELIVERY_ANYTIME_ALPHA)
AI_DELIVERY_ANYTIME_BETTING_FRACTIONS = (0.5, 0.9)
AI_DELIVERY_ANYTIME_METHOD = "two_strategy_fixed_betting_eprocess_cs_v1"
AI_DELIVERY_DUE_TOLERANCE_SECONDS = 1e-6
AI_INVENTORY_EPOCH_STATES = frozenset({"running", "completed", "failed", "abandoned"})
AI_INVENTORY_DISPOSITIONS = frozenset(
    {
        "delivery_required",
        "delivered",
        "policy_excluded",
        "legacy_preinstrumented_ai",
        "missing",
    }
)


def ai_delivery_anytime_log_e(
    theta: float,
    successes: int,
    misses: int,
) -> float:
    """Return the log e-value for the fixed, non-Bayesian betting portfolio."""

    probability = float(theta)
    success_count = int(successes)
    miss_count = int(misses)
    if not 0.0 < probability < 1.0:
        raise ValueError("theta must be strictly between zero and one")
    if success_count < 0 or miss_count < 0:
        raise ValueError("successes and misses must be non-negative")
    terms = []
    for fraction in AI_DELIVERY_ANYTIME_BETTING_FRACTIONS:
        log_lr = (
            success_count
            * math.log1p(fraction * ((1.0 - probability) / probability))
            + miss_count * math.log1p(-fraction)
        )
        terms.append(math.log(0.5) + log_lr)
    pivot = max(terms)
    value = pivot + math.log(sum(math.exp(term - pivot) for term in terms))
    if not math.isfinite(value):
        raise ArithmeticError("anytime e-process returned a non-finite log value")
    return value


def ai_delivery_anytime_lower_bound(successes: int, misses: int) -> float | None:
    """Return a conservative one-sided 95% anytime-valid lower bound."""

    success_count = int(successes)
    miss_count = int(misses)
    if success_count < 0 or miss_count < 0:
        raise ValueError("successes and misses must be non-negative")
    if success_count + miss_count == 0:
        return None
    if success_count == 0:
        return 0.0
    rejecting = 0.0
    accepting = 1.0
    for _iteration in range(80):
        midpoint = (rejecting + accepting) / 2.0
        if midpoint == rejecting or midpoint == accepting:
            break
        if ai_delivery_anytime_log_e(midpoint, success_count, miss_count) >= AI_DELIVERY_ANYTIME_LOG_THRESHOLD:
            rejecting = midpoint
        else:
            accepting = midpoint
    return rejecting
VALID_STATUSES = {
    "needs_ai",
    "finished",
    "local_chinese",
    "embedded_chinese",
    "unsupported_media",
}
AI_QUEUE_READY_STATUSES = {"queued"}
SQLITE_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "database schema is corrupt",
    "malformed database schema",
    "file is not a database",
    "not a database",
)
SQLITE_TRANSIENT_MARKERS = (
    "database is locked",
    "database table is locked",
    "database is busy",
    "disk i/o error",
)


def _configure_scan_state_connection(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    row = conn.execute("PRAGMA journal_mode").fetchone()
    current_journal_mode = str(row[0] if row else "").casefold()
    if current_journal_mode != SQLITE_JOURNAL_MODE.casefold():
        conn.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}")
    conn.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS}")
    conn.execute(f"PRAGMA wal_autocheckpoint={SQLITE_WAL_AUTOCHECKPOINT_PAGES}")
    conn.execute("PRAGMA temp_store=MEMORY")


@dataclass(frozen=True)
class VideoScanSignature:
    path: Path
    size: int
    mtime_ns: int
    sidecar_signature: str
    config_signature: str


class ScanStateStore:
    def __init__(
        self,
        path: Path,
        *,
        stage_event_max_rows: int | None = None,
        stage_event_retention_days: int | None = None,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        self._stage_events_since_prune = 0
        self._stage_event_max_rows = max(
            1,
            int(AI_STAGE_EVENT_MAX_ROWS if stage_event_max_rows is None else stage_event_max_rows),
        )
        self._stage_event_retention_days = max(
            1,
            int(
                AI_STAGE_EVENT_RETENTION_DAYS
                if stage_event_retention_days is None
                else stage_event_retention_days
            ),
        )
        try:
            _configure_scan_state_connection(self._conn)
            self._ensure_schema()
        except Exception:
            self._conn.close()
            raise

    @classmethod
    def from_config(cls, config: Any) -> "ScanStateStore":
        return cls(
            scan_state_path(config),
            stage_event_max_rows=int(getattr(config, "ai_stage_event_max_rows", AI_STAGE_EVENT_MAX_ROWS)),
            stage_event_retention_days=int(
                getattr(config, "ai_stage_event_retention_days", AI_STAGE_EVENT_RETENTION_DAYS)
            ),
        )

    def close(self) -> None:
        self._conn.close()

    @property
    def in_transaction(self) -> bool:
        """Return whether this connection currently owns an open transaction."""

        return bool(self._conn.in_transaction)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def get_status(self, signature: VideoScanSignature) -> str | None:
        row = self._conn.execute(
            """
            SELECT status
            FROM video_scan_cache
            WHERE path = ?
              AND size = ?
              AND mtime_ns = ?
              AND sidecar_signature = ?
              AND config_signature = ?
              AND schema_version = ?
            """,
            (
                str(signature.path),
                signature.size,
                signature.mtime_ns,
                signature.sidecar_signature,
                signature.config_signature,
                SCHEMA_VERSION,
            ),
        ).fetchone()
        if row is None:
            return None
        status = str(row[0])
        return status if status in VALID_STATUSES else None

    def put_status(self, signature: VideoScanSignature, status: str) -> None:
        if status not in VALID_STATUSES:
            return
        self._conn.execute(
            """
            INSERT INTO video_scan_cache (
                path,
                size,
                mtime_ns,
                sidecar_signature,
                config_signature,
                episode,
                status,
                schema_version,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                sidecar_signature = excluded.sidecar_signature,
                config_signature = excluded.config_signature,
                episode = excluded.episode,
                status = excluded.status,
                schema_version = excluded.schema_version,
                updated_at = excluded.updated_at
            """,
            (
                str(signature.path),
                signature.size,
                signature.mtime_ns,
                signature.sidecar_signature,
                signature.config_signature,
                extract_episode_number(signature.path.name),
                status,
                SCHEMA_VERSION,
                time.time(),
            ),
        )

    def upsert_ai_queue_candidate(
        self,
        path: Path,
        mtime_ns: int,
        *,
        source: str = "scan",
        added_at: float | None = None,
    ) -> bool:
        normalized_path = _queue_path(path)
        normalized_source = str(source or "scan")
        filename_season, filename_episode = _filename_sequence(normalized_path)
        existing = self._conn.execute(
            "SELECT mtime_ns FROM ai_candidate_queue WHERE path = ?",
            (normalized_path,),
        ).fetchone()
        if existing is not None and int(existing[0] or 0) == int(mtime_ns):
            return False

        now = time.time()
        queue_added_at = now if added_at is None else float(added_at)
        cursor = self._conn.execute(
            """
            INSERT INTO ai_candidate_queue (
                path,
                mtime_ns,
                filename_season,
                filename_episode,
                status,
                source,
                added_at,
                updated_at,
                next_retry_at
            )
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, 0)
            ON CONFLICT(path) DO UPDATE SET
                mtime_ns = excluded.mtime_ns,
                filename_season = excluded.filename_season,
                filename_episode = excluded.filename_episode,
                source = CASE
                    WHEN ai_candidate_queue.status IN ('running', 'paused', 'skipped') THEN ai_candidate_queue.source
                    WHEN ai_candidate_queue.status = 'done'
                         AND ai_candidate_queue.mtime_ns = excluded.mtime_ns THEN ai_candidate_queue.source
                    ELSE excluded.source
                END,
                status = CASE
                    WHEN ai_candidate_queue.status IN ('running', 'paused', 'skipped') THEN ai_candidate_queue.status
                    WHEN ai_candidate_queue.status = 'done'
                         AND ai_candidate_queue.mtime_ns = excluded.mtime_ns THEN 'done'
                    ELSE 'queued'
                END,
                updated_at = CASE
                    WHEN ai_candidate_queue.status IN ('running', 'paused', 'skipped') THEN ai_candidate_queue.updated_at
                    WHEN ai_candidate_queue.status = 'done'
                         AND ai_candidate_queue.mtime_ns = excluded.mtime_ns THEN ai_candidate_queue.updated_at
                    ELSE excluded.updated_at
                END,
                next_retry_at = CASE
                    WHEN ai_candidate_queue.status IN ('running', 'paused', 'skipped') THEN ai_candidate_queue.next_retry_at
                    WHEN ai_candidate_queue.status = 'done'
                         AND ai_candidate_queue.mtime_ns = excluded.mtime_ns THEN ai_candidate_queue.next_retry_at
                    ELSE 0
                END
            WHERE ai_candidate_queue.mtime_ns != excluded.mtime_ns
            """,
            (
                normalized_path,
                mtime_ns,
                filename_season,
                filename_episode,
                source,
                queue_added_at,
                now,
            ),
        )
        changed = int(cursor.rowcount or 0) > 0
        # Watchdog can report several closed/modified events for the same stable
        # media identity.  Only a real queue identity change invalidates a proof
        # epoch; otherwise a duplicate event can make a multi-hour census
        # impossible to finalize even though the media set did not change.
        if changed and normalized_source != "scan":
            self.mark_ai_inventory_dirty(observed_at=now)
        return changed

    def mark_ai_inventory_dirty(self, *, observed_at: float | None = None) -> None:
        """Persist a post-snapshot filesystem/manual delta until the next epoch."""

        now = float(time.time() if observed_at is None else observed_at)
        rows = dict(
            self._conn.execute(
                "SELECT key, value FROM ai_delivery_meta "
                "WHERE key IN ('inventory_dirty_at', 'inventory_dirty_generation')"
            ).fetchall()
        )
        try:
            prior = float(rows.get("inventory_dirty_at") or 0)
        except (TypeError, ValueError):
            prior = 0.0
        try:
            prior_generation = int(rows.get("inventory_dirty_generation") or 0)
        except (TypeError, ValueError):
            prior_generation = 0
        dirty_at = max(prior, now)
        for key, value in (
            ("inventory_dirty_at", str(dirty_at)),
            ("inventory_dirty_generation", str(prior_generation + 1)),
        ):
            self._conn.execute(
                """
                INSERT INTO ai_delivery_meta(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def force_ai_queue_candidate(self, path: Path) -> None:
        now = time.time()
        # Queue identity must be the media identity.  A wall-clock timestamp
        # makes the forced row impossible to match to a delivery obligation
        # until some later scan rewrites it.
        media_mtime_ns = int(Path(path).stat().st_mtime_ns)
        normalized_path = _queue_path(path)
        filename_season, filename_episode = _filename_sequence(normalized_path)
        self.mark_ai_inventory_dirty(observed_at=now)
        self._conn.execute(
            """
            INSERT INTO ai_candidate_queue (
                path,
                mtime_ns,
                filename_season,
                filename_episode,
                status,
                source,
                attempts,
                running_at,
                last_error,
                last_error_at,
                last_error_code,
                retry_strategy,
                failure_revision,
                next_retry_at,
                force_ai,
                added_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'queued', 'manual_force', 0, 0, '', 0, '', 'manual_force', '', 0, 1, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime_ns = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.mtime_ns
                    ELSE excluded.mtime_ns
                END,
                filename_season = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.filename_season
                    ELSE excluded.filename_season
                END,
                filename_episode = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.filename_episode
                    ELSE excluded.filename_episode
                END,
                status = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.status
                    ELSE 'queued'
                END,
                source = 'manual_force',
                attempts = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.attempts
                    ELSE 0
                END,
                running_at = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.running_at
                    ELSE 0
                END,
                last_error = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.last_error
                    ELSE ''
                END,
                last_error_at = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.last_error_at
                    ELSE 0
                END,
                last_error_code = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.last_error_code
                    ELSE ''
                END,
                retry_strategy = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.retry_strategy
                    ELSE 'manual_force'
                END,
                failure_revision = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.failure_revision
                    ELSE ''
                END,
                next_retry_at = CASE
                    WHEN ai_candidate_queue.status = 'running' THEN ai_candidate_queue.next_retry_at
                    ELSE 0
                END,
                force_ai = 1,
                updated_at = excluded.updated_at
            """,
            (
                normalized_path,
                media_mtime_ns,
                filename_season,
                filename_episode,
                now,
                now,
            ),
        )
        self.update_ai_job_stage(path, "force_ai", "queued", "Manual force AI queued")

    def queue_paused_review_remediation(
        self,
        path: Path,
        *,
        expected_failure_revision: str,
        policy_revision: str,
    ) -> bool:
        """Queue exactly one evidence-bound review repair without resetting attempts.

        Automatic review handling must not inherit the unlimited semantics of
        a manual force action.  The compare-and-set on the paused row and its
        failure revision prevents stale review cards from reviving a changed
        job, while preserving the consumed retry budget makes a failed repair
        return immediately to review instead of looping.
        """

        normalized_path = _queue_path(path)
        normalized_failure_revision = str(expected_failure_revision or "").strip()
        normalized_policy_revision = str(policy_revision or "").strip()
        if not normalized_failure_revision:
            return False
        if not normalized_policy_revision or len(normalized_policy_revision) > 100:
            raise ValueError("review remediation policy revision is invalid")
        now = time.time()
        changed = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status='queued',
                source='auto_review_remediation',
                running_at=0,
                next_retry_at=0,
                retry_strategy=?,
                force_ai=1,
                updated_at=?
            WHERE path=?
              AND status='paused'
              AND failure_revision=?
            """,
            (
                normalized_policy_revision,
                now,
                normalized_path,
                normalized_failure_revision,
            ),
        ).rowcount
        if changed:
            self.mark_ai_inventory_dirty(observed_at=now)
            self.update_ai_job_stage(
                path,
                "queued",
                "queued",
                "Safety-gated automatic review remediation queued",
            )
        return changed == 1

    def active_review_remediation_count(self) -> int:
        """Return review-created AI jobs that are still queued or running."""

        row = self._conn.execute(
            """
            SELECT COUNT(*)
            FROM ai_candidate_queue
            WHERE source='auto_review_remediation'
              AND status IN ('queued', 'running')
            """
        ).fetchone()
        return int((row or (0,))[0] or 0)

    def running_ai_queue_count(self) -> int:
        """Return AI queue jobs currently holding normal processing capacity."""

        row = self._conn.execute(
            "SELECT COUNT(*) FROM ai_candidate_queue WHERE status='running'"
        ).fetchone()
        return int((row or (0,))[0] or 0)

    def is_force_ai_queue_candidate(self, path: Path) -> bool:
        force_ai, _bypass_failure_cooldown = self.ai_queue_candidate_policy(path)
        return force_ai

    def ai_queue_candidate_policy(self, path: Path) -> tuple[bool, bool]:
        """Return force-AI and manual-retry cooldown policy for a queue item.

        Manual retry actions should retry immediately, but they must not inherit
        all force-AI behavior (notably, bulk retry must still exclude standalone
        OP/ED files).  Keeping the two flags separate preserves that distinction.
        """
        row = self._conn.execute(
            """
            SELECT force_ai, source
            FROM ai_candidate_queue
            WHERE path = ?
            """,
            (_queue_path(path),),
        ).fetchone()
        if row is None:
            return False, False
        force_ai = bool(row[0])
        source = str(row[1] or "")
        manual_retry = source in {
            "manual_retry",
            "manual_retry_failed",
            "manual_clear_failure",
            "manual_priority",
        }
        return force_ai, force_ai or manual_retry

    def mark_ai_queue_running(
        self,
        path: Path,
        *,
        acceptance_target: AcceptanceQueueTarget | None = None,
        expected_failure_revision: str | None = None,
        expected_failure_code: str | None = None,
        expected_media_mtime_ns: int | None = None,
    ) -> None:
        """Claim queued work, optionally with an exact durable canary identity.

        Ordinary queue claims retain their compatibility behavior.  A caller
        that supplies any canary precondition must supply all three; the
        compare-and-set then refuses a row whose status or persisted failure
        identity changed after selection.
        """

        now = time.time()
        exact_identity_requested = any(
            value is not None
            for value in (
                expected_failure_revision,
                expected_failure_code,
                expected_media_mtime_ns,
            )
        )
        if acceptance_target is not None and exact_identity_requested:
            raise ValueError(
                "acceptance and exact canary queue identities cannot be combined"
            )
        if exact_identity_requested:
            if (
                expected_failure_revision is None
                or expected_failure_code is None
                or expected_media_mtime_ns is None
            ):
                raise ValueError("exact canary queue claim requires every expected identity field")
            normalized_revision = str(expected_failure_revision).strip()
            normalized_code = str(expected_failure_code).strip().casefold()
            if not re.fullmatch(r"[0-9a-f]{24}", normalized_revision):
                raise ValueError("exact canary queue claim has an invalid failure revision")
            if not normalized_code:
                raise ValueError("exact canary queue claim has an invalid failure code")
            if isinstance(expected_media_mtime_ns, bool):
                raise ValueError("exact canary queue claim has an invalid media mtime")
            normalized_mtime_ns = int(expected_media_mtime_ns)
            normalized_path = _queue_path(path)
            try:
                current_mtime_ns = int(Path(normalized_path).stat().st_mtime_ns)
            except OSError as exc:
                raise ValueError("exact canary queue claim media is unavailable") from exc
            if normalized_mtime_ns <= 0 or current_mtime_ns != normalized_mtime_ns:
                raise ValueError("exact canary queue claim media identity changed")
            changed = self._conn.execute(
                """
                UPDATE ai_candidate_queue
                SET status = 'running',
                    running_at = ?,
                    next_retry_at = 0,
                    updated_at = ?
                WHERE path = ?
                  AND status = 'queued'
                  AND mtime_ns = ?
                  AND failure_revision = ?
                  AND last_error_code = ?
                """,
                (
                    now,
                    now,
                    normalized_path,
                    normalized_mtime_ns,
                    normalized_revision,
                    normalized_code,
                ),
            ).rowcount
            try:
                post_claim_mtime_ns = int(Path(normalized_path).stat().st_mtime_ns)
            except OSError as exc:
                raise ValueError("exact canary queue claim media is unavailable") from exc
            if post_claim_mtime_ns != normalized_mtime_ns:
                raise ValueError("exact canary queue claim media identity changed")
            if int(changed or 0) != 1:
                raise ValueError(
                    "exact canary queue target is not the expected queued failure identity"
                )
        elif acceptance_target is None:
            self._conn.execute(
                """
                UPDATE ai_candidate_queue
                SET status = 'running',
                    running_at = ?,
                    next_retry_at = 0,
                    updated_at = ?
                WHERE path = ?
                """,
                (now, now, _queue_path(path)),
            )
        else:
            retry_strategies = tuple(sorted(AI_QUEUE_AUTOMATIC_RETRY_STRATEGIES))
            retry_placeholders = ", ".join("?" for _item in retry_strategies)
            changed = self._conn.execute(
                f"""
                UPDATE ai_candidate_queue AS q
                SET status = 'running',
                    running_at = ?,
                    next_retry_at = 0,
                    updated_at = ?
                WHERE q.path = ?
                  AND q.mtime_ns = ?
                  AND (
                        q.status = 'queued'
                        OR (
                            q.status = 'failed_retry'
                            AND q.next_retry_at <= ?
                            AND q.last_error_at > 0
                            AND q.last_error_code != ''
                            AND q.retry_strategy IN ({retry_placeholders})
                            AND EXISTS (
                                SELECT 1
                                FROM ai_delivery_attempts a
                                WHERE a.obligation_id = ?
                                  AND a.attempt_number = (
                                      SELECT attempt_count
                                      FROM ai_delivery_obligations
                                      WHERE obligation_id = ?
                                  )
                                  AND a.status = 'retryable_failure'
                                  AND a.error_code = q.last_error_code
                                  AND a.finished_at >= q.last_error_at
                                  AND a.finished_at <= q.last_error_at + ?
                            )
                        )
                  )
                  AND EXISTS (
                        SELECT 1
                        FROM ai_delivery_obligations o
                        WHERE o.obligation_id = ?
                          AND o.canonical_path = q.path
                          AND o.media_fingerprint = ?
                          AND o.media_size = ?
                          AND o.media_mtime_ns = q.mtime_ns
                          AND o.policy_revision = ?
                          AND o.state = 'open'
                  )
                """,
                (
                    now,
                    now,
                    acceptance_target.canonical_path,
                    acceptance_target.media_mtime_ns,
                    now,
                    *retry_strategies,
                    acceptance_target.obligation_id,
                    acceptance_target.obligation_id,
                    AI_QUEUE_RETRY_ATTEMPT_LINK_MAX_SKEW_SECONDS,
                    acceptance_target.obligation_id,
                    acceptance_target.media_fingerprint,
                    acceptance_target.media_size,
                    acceptance_target.policy_revision,
                ),
            ).rowcount
            if int(changed or 0) != 1:
                raise ValueError(
                    "acceptance queue target is not an exact, open, claimable identity"
                )
        self.update_ai_job_stage(path, "worker", "running", "Worker started")

    def mark_ai_queue_done(
        self,
        path: Path,
        message: str = "AI subtitle job completed",
        *,
        completed_at: float | None = None,
        detected_existing: bool = False,
        mark_inventory_dirty: bool = False,
    ) -> bool:
        now = time.time()
        effective_time = float(completed_at or now)
        queue_updated_at = effective_time if detected_existing else now
        stage = "detected_existing" if detected_existing else "complete"
        normalized_path = _queue_path(path)
        mtime_ns = _safe_mtime_ns(path)
        if detected_existing and self._detected_existing_ai_done_is_current(
            normalized_path,
            mtime_ns=mtime_ns,
            completed_at=effective_time,
        ):
            return False
        if mark_inventory_dirty:
            self.mark_ai_inventory_dirty(observed_at=now)
        preserve_completed_provenance = bool(
            detected_existing
            and self._same_media_has_completed_ai_job_provenance(
                normalized_path,
                mtime_ns=mtime_ns,
            )
        )
        existing_skipped_stage: tuple[str, str] | None = None
        if message == "AI subtitle job completed":
            row = self._conn.execute(
                "SELECT stage, message FROM ai_job_state WHERE path = ? AND status = 'skipped'",
                (normalized_path,),
            ).fetchone()
            if row is not None:
                existing_skipped_stage = (str(row[0] or "skipped"), str(row[1] or "Skipped"))
        self._conn.execute(
            """
            INSERT INTO ai_candidate_queue (
                path,
                mtime_ns,
                status,
                source,
                attempts,
                running_at,
                last_error,
                last_error_at,
                next_retry_at,
                force_ai,
                added_at,
                updated_at
            )
            VALUES (?, ?, 'done', 'scan', 0, 0, '', 0, 0, 0, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime_ns = excluded.mtime_ns,
                status = 'done',
                attempts = 0,
                updated_at = excluded.updated_at,
                running_at = 0,
                next_retry_at = 0,
                last_error = '',
                last_error_at = 0,
                last_error_code = '',
                retry_strategy = '',
                failure_revision = '',
                force_ai = 0
            """,
            (normalized_path, mtime_ns, queue_updated_at, queue_updated_at),
        )
        if existing_skipped_stage is not None:
            stage, skipped_message = existing_skipped_stage
            self.update_ai_job_stage(path, stage, "skipped", skipped_message)
        elif not preserve_completed_provenance:
            self.update_ai_job_stage(
                path,
                stage,
                "ok",
                message,
                event_time=effective_time if detected_existing else None,
                started_at=0.0 if detected_existing else None,
                finished_at=effective_time if detected_existing else None,
            )
        return True

    def _same_media_has_completed_ai_job_provenance(self, path: str, *, mtime_ns: int) -> bool:
        """Keep measured Worker success when a scan rediscovers its output.

        ``detected_existing`` is discovery evidence, not a second completion.
        Replacing a same-media ``complete`` row with a zero-duration discovery
        row destroys the only durable service-time sample.  The queue row is
        still reconciled to ``done``; only the already stronger job provenance
        is retained.  A changed media identity cannot reuse that provenance.
        """

        row = self._conn.execute(
            """
            SELECT q.status, q.mtime_ns, q.updated_at,
                   j.stage, j.status, j.started_at, j.finished_at
            FROM ai_candidate_queue q
            JOIN ai_job_state j ON j.path = q.path
            WHERE q.path = ?
            """,
            (path,),
        ).fetchone()
        if row is None:
            return False
        queue_status, queue_mtime_ns, queue_updated_at, stage, status, started_at, finished_at = row
        return (
            str(queue_status or "") == "done"
            and int(queue_mtime_ns or 0) == int(mtime_ns)
            and str(stage or "") == "complete"
            and str(status or "") in {"ok", "done", "success", "finished"}
            and float(started_at or 0) > 0
            and float(finished_at or 0) > float(started_at or 0)
            and float(finished_at or 0) >= float(queue_updated_at or 0)
        )

    def _detected_existing_ai_done_is_current(self, path: str, *, mtime_ns: int, completed_at: float) -> bool:
        row = self._conn.execute(
            """
            SELECT
                q.status,
                q.mtime_ns,
                q.updated_at,
                q.running_at,
                q.next_retry_at,
                q.force_ai,
                q.last_error,
                q.last_error_at,
                j.stage,
                j.status,
                j.started_at,
                j.updated_at,
                j.finished_at
            FROM ai_candidate_queue q
            LEFT JOIN ai_job_state j ON j.path = q.path
            WHERE q.path = ?
            """,
            (path,),
        ).fetchone()
        if row is None:
            return False

        (
            queue_status,
            queue_mtime_ns,
            queue_updated_at,
            running_at,
            next_retry_at,
            force_ai,
            last_error,
            last_error_at,
            job_stage,
            job_status,
            job_started_at,
            job_updated_at,
            job_finished_at,
        ) = row
        queue_is_current = (
            str(queue_status or "") == "done"
            and int(queue_mtime_ns or 0) == int(mtime_ns)
            and _same_timestamp(queue_updated_at, completed_at)
            and not float(running_at or 0)
            and not float(next_retry_at or 0)
            and not int(force_ai or 0)
            and not str(last_error or "")
            and not float(last_error_at or 0)
        )
        if not queue_is_current:
            return False
        detected_existing_is_current = (
            str(job_stage or "") == "detected_existing"
            and str(job_status or "") == "ok"
            and not float(job_started_at or 0)
            and _same_timestamp(job_updated_at, completed_at)
            and _same_timestamp(job_finished_at, completed_at)
        )
        completed_provenance_is_current = (
            str(job_stage or "") == "complete"
            and str(job_status or "") in {"ok", "done", "success", "finished"}
            and float(job_started_at or 0) > 0
            and float(job_finished_at or 0) > float(job_started_at or 0)
        )
        return detected_existing_is_current or completed_provenance_is_current

    def mark_ai_queue_failed(
        self,
        path: Path,
        error: str,
        *,
        retry_after_seconds: int = 0,
        max_attempts: int = 0,
        error_code: str = "",
        retry_strategy: str = "",
    ) -> bool:
        """Record a transient failure and pause jobs that exhaust their retry budget.

        A zero retry limit preserves the legacy unlimited-retry behaviour.  The
        return value tells callers whether this failure moved the job to manual
        review instead of scheduling another automatic attempt.
        """

        now = time.time()
        next_retry_at = now + max(0, retry_after_seconds)
        normalized_path = _queue_path(path)
        error_text = str(error)[:1000]
        normalized_error_code = str(error_code or "unknown_failure")[:100]
        normalized_retry_strategy = str(retry_strategy or "same_pipeline")[:100]
        attempt_row = self._conn.execute(
            "SELECT attempts FROM ai_candidate_queue WHERE path = ? LIMIT 1",
            (normalized_path,),
        ).fetchone()
        next_attempt = int(attempt_row[0] or 0) + 1 if attempt_row is not None else 1
        review_required = int(max_attempts or 0) > 0 and next_attempt >= int(max_attempts)
        preserve_job_failure = False
        if error_text == "worker returned false":
            existing = self._conn.execute(
                "SELECT stage, status, message FROM ai_job_state WHERE path = ? LIMIT 1",
                (normalized_path,),
            ).fetchone()
            if existing is not None:
                _stage, status, message = existing
                detailed_message = str(message or "").strip()
                if str(status or "") == "failed" and detailed_message and detailed_message != error_text:
                    error_text = detailed_message[:1000]
                    preserve_job_failure = True
        failure_revision = _failure_revision(
            normalized_path,
            normalized_error_code,
            error_text,
        )
        self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = ?,
                source = CASE WHEN ? THEN 'failure_review' ELSE source END,
                attempts = attempts + 1,
                updated_at = ?,
                running_at = 0,
                last_error = ?,
                last_error_at = ?,
                last_error_code = ?,
                retry_strategy = ?,
                failure_revision = ?,
                next_retry_at = ?
            WHERE path = ?
            """,
            (
                "paused" if review_required else "failed_retry",
                1 if review_required else 0,
                now,
                error_text,
                now,
                normalized_error_code,
                normalized_retry_strategy,
                failure_revision,
                0 if review_required else next_retry_at,
                normalized_path,
            ),
        )
        if not preserve_job_failure:
            self.update_ai_job_stage(path, "failed", "failed", error_text)
        return review_required

    def mark_ai_queue_review_required(
        self,
        path: Path,
        message: str,
        *,
        source: str = "asr_review",
        error_code: str = "deterministic_asr_quality",
    ) -> None:
        """Pause a deterministic failure until a human or safe repair campaign acts."""

        now = time.time()
        normalized_path = _queue_path(path)
        message_text = str(message or "ASR review required")[:1000]
        normalized_error_code = str(error_code or "review_required")[:100]
        self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'paused',
                source = ?,
                attempts = attempts + 1,
                updated_at = ?,
                running_at = 0,
                last_error = ?,
                last_error_at = ?,
                last_error_code = ?,
                retry_strategy = 'manual_review',
                failure_revision = ?,
                next_retry_at = 0
            WHERE path = ?
            """,
            (
                str(source or "review_required")[:100],
                now,
                message_text,
                now,
                normalized_error_code,
                _failure_revision(normalized_path, normalized_error_code, message_text),
                normalized_path,
            ),
        )

    def ai_job_failure(self, path: Path) -> tuple[str, str] | None:
        row = self._conn.execute(
            "SELECT stage, message FROM ai_job_state WHERE path = ? AND status = 'failed' LIMIT 1",
            (_queue_path(path),),
        ).fetchone()
        if row is None:
            return None
        return str(row[0] or ""), str(row[1] or "")

    def remove_ai_queue_candidate(
        self,
        path: Path,
        *,
        clear_job_state: bool = False,
        mark_inventory_dirty: bool = False,
    ) -> bool:
        normalized_path = _queue_path(path)
        if clear_job_state:
            exists = self._conn.execute(
                """
                SELECT 1
                WHERE EXISTS (SELECT 1 FROM ai_candidate_queue WHERE path = ?)
                   OR EXISTS (SELECT 1 FROM ai_job_state WHERE path = ?)
                   OR EXISTS (SELECT 1 FROM ai_stage_events WHERE path = ?)
                """,
                (normalized_path, normalized_path, normalized_path),
            ).fetchone()
        else:
            exists = self._conn.execute(
                "SELECT 1 FROM ai_candidate_queue WHERE path = ? LIMIT 1",
                (normalized_path,),
            ).fetchone()
        if exists is None:
            # Avoid issuing no-op DELETE statements. SQLite opens a write
            # transaction even when DELETE changes zero rows; callers that
            # correctly skip commits for unchanged state would otherwise hold
            # the single WAL writer lock for the rest of a library scan.
            return False
        if mark_inventory_dirty:
            self.mark_ai_inventory_dirty()
        changed = self._conn.execute(
            "DELETE FROM ai_candidate_queue WHERE path = ?",
            (normalized_path,),
        ).rowcount > 0
        if clear_job_state:
            changed = self._conn.execute(
                "DELETE FROM ai_job_state WHERE path = ?",
                (normalized_path,),
            ).rowcount > 0 or changed
            changed = self._conn.execute(
                "DELETE FROM ai_stage_events WHERE path = ?",
                (normalized_path,),
            ).rowcount > 0 or changed
        return changed

    def ai_inventory_observation_is_current(
        self,
        path: Path,
        *,
        media_size: int,
        media_mtime_ns: int,
        policy_revision: str,
        classification: str,
        dispositions: set[str],
    ) -> bool:
        """Whether the latest contract-matching epoch already attests this fact."""

        meta = dict(self._conn.execute("SELECT key, value FROM ai_delivery_meta").fetchall())
        measurement_revision = str(meta.get("measurement_revision") or "")
        root_signature = str(meta.get("inventory_current_root_signature") or "")
        normalized_policy = str(policy_revision or "").strip()
        normalized_dispositions = sorted(
            {str(item or "").strip().casefold() for item in dispositions if str(item or "").strip()}
        )
        if (
            measurement_revision != AI_DELIVERY_MEASUREMENT_REVISION
            or not root_signature
            or not normalized_policy
            or not normalized_dispositions
        ):
            return False
        identity = ai_delivery_identity(
            path,
            media_size=int(media_size),
            media_mtime_ns=int(media_mtime_ns),
            policy_revision=normalized_policy,
        )
        latest = self._conn.execute(
            """
            SELECT epoch_id FROM ai_inventory_epochs
            WHERE state='completed' AND measurement_revision=?
              AND policy_revision=? AND root_signature=?
            ORDER BY completed_at DESC LIMIT 1
            """,
            (measurement_revision, normalized_policy, root_signature),
        ).fetchone()
        if latest is None:
            return False
        placeholders = ",".join("?" for _item in normalized_dispositions)
        row = self._conn.execute(
            f"""
            SELECT 1 FROM ai_media_inventory
            WHERE epoch_id=? AND canonical_path=? AND media_fingerprint=?
              AND media_size=? AND media_mtime_ns=? AND policy_revision=?
              AND classification=? AND disposition IN ({placeholders})
            LIMIT 1
            """,
            (
                str(latest[0]), identity["canonical_path"], identity["media_fingerprint"],
                int(media_size), int(media_mtime_ns), normalized_policy,
                str(classification or "").strip().casefold(), *normalized_dispositions,
            ),
        ).fetchone()
        return row is not None

    def retry_ai_queue_candidate(self, path: Path) -> None:
        now = time.time()
        self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'queued',
                source = 'manual_retry',
                attempts = 0,
                updated_at = ?,
                running_at = 0,
                last_error = '',
                last_error_at = 0,
                last_error_code = '',
                retry_strategy = 'manual_retry',
                failure_revision = '',
                next_retry_at = 0
            WHERE path = ?
            """,
            (now, _queue_path(path)),
        )
        self.update_ai_job_stage(path, "queued", "queued", "Manual retry queued")

    def retry_all_failed_ai_queue_candidates(self) -> int:
        """Requeue transient failures without bypassing manual review items."""

        now = time.time()
        rows = self._conn.execute(
            """
            SELECT path
            FROM ai_candidate_queue
            WHERE status IN ('failed', 'failed_retry')
            """
        ).fetchall()
        paths = [str(row[0] or "") for row in rows if str(row[0] or "")]
        if not paths:
            return 0
        changed = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'queued', source = 'manual_retry_failed', attempts = 0,
                updated_at = ?, running_at = 0, last_error = '',
                last_error_at = 0, last_error_code = '',
                retry_strategy = 'manual_bulk_retry', failure_revision = '',
                next_retry_at = 0
            WHERE status IN ('failed', 'failed_retry')
            """,
            (now,),
        ).rowcount
        self._conn.execute(
            """
            UPDATE ai_job_state
            SET stage = 'queued', status = 'queued',
                message = 'Manual bulk retry queued', updated_at = ?, finished_at = NULL
            WHERE status = 'failed'
              AND path IN (
                  SELECT path FROM ai_candidate_queue
                  WHERE source = 'manual_retry_failed' AND updated_at = ?
              )
            """,
            (now, now),
        )
        return max(0, int(changed or 0))

    def failed_retry_candidates(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Return structured retry candidates without mutating queue state."""

        cursor = self._conn.execute(
            """
            SELECT
                q.path,
                q.status,
                q.source,
                q.attempts,
                q.last_error,
                q.last_error_at,
                q.last_error_code,
                q.retry_strategy,
                q.failure_revision,
                q.next_retry_at,
                q.updated_at,
                q.mtime_ns,
                j.stage AS job_stage,
                j.status AS job_status,
                j.message AS job_message,
                j.updated_at AS job_updated_at
            FROM ai_candidate_queue q
            LEFT JOIN ai_job_state j ON j.path=q.path
            WHERE q.status='failed_retry'
            ORDER BY q.next_retry_at, q.updated_at, q.path COLLATE NOCASE
            LIMIT ?
            """,
            (max(1, min(50_000, int(limit or 0))),),
        )
        columns = [str(description[0]) for description in cursor.description or ()]
        rows = cursor.fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(zip(columns, row, strict=True))
            path = str(payload.get("path") or "")
            code = str(payload.get("last_error_code") or "")
            if not code:
                code = _legacy_failure_code(
                    str(payload.get("job_stage") or ""),
                    str(payload.get("last_error") or payload.get("job_message") or ""),
                )
            revision = str(payload.get("failure_revision") or "")
            if not revision:
                revision = _failure_revision(
                    path,
                    code,
                    str(payload.get("last_error") or payload.get("job_message") or ""),
                )
            payload["last_error_code"] = code
            payload["failure_revision"] = revision
            candidates.append(payload)
        return candidates

    def queue_failed_retry_preserving_budget(
        self,
        path: Path,
        *,
        expected_failure_revision: str,
        expected_failure_code: str,
        expected_media_mtime_ns: int,
    ) -> bool:
        """Atomically queue one exact failure without resetting its budget."""

        normalized_path = _queue_path(path)
        normalized_expected_revision = str(expected_failure_revision or "").strip()
        normalized_expected_code = str(expected_failure_code or "").strip().casefold()
        if isinstance(expected_media_mtime_ns, bool):
            return False
        try:
            normalized_expected_mtime_ns = int(expected_media_mtime_ns)
        except (TypeError, ValueError):
            return False
        if (
            not normalized_expected_revision
            or not normalized_expected_code
            or normalized_expected_mtime_ns <= 0
        ):
            return False
        try:
            if Path(normalized_path).stat().st_mtime_ns != normalized_expected_mtime_ns:
                return False
        except OSError:
            return False
        cursor = self._conn.execute(
            """
            SELECT q.path, q.mtime_ns, q.status, q.last_error, q.last_error_code,
                   q.failure_revision, j.stage AS job_stage, j.message AS job_message
            FROM ai_candidate_queue q
            LEFT JOIN ai_job_state j ON j.path=q.path
            WHERE q.path=?
            """,
            (normalized_path,),
        )
        columns = [str(description[0]) for description in cursor.description or ()]
        row = cursor.fetchone()
        payload = dict(zip(columns, row, strict=True)) if row is not None else None
        if payload is None or str(payload["status"] or "") != "failed_retry":
            return False
        code = str(payload["last_error_code"] or "") or _legacy_failure_code(
            str(payload["job_stage"] or ""),
            str(payload["last_error"] or payload["job_message"] or ""),
        )
        revision = str(payload["failure_revision"] or "") or _failure_revision(
            normalized_path,
            code,
            str(payload["last_error"] or payload["job_message"] or ""),
        )
        if (
            int(payload["mtime_ns"] or 0) != normalized_expected_mtime_ns
            or code != normalized_expected_code
            or revision != normalized_expected_revision
        ):
            return False
        observed_last_error = str(payload["last_error"] or "")
        observed_last_error_code = str(payload["last_error_code"] or "")
        observed_failure_revision = str(payload["failure_revision"] or "")
        now = time.time()
        changed = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status='queued',
                source='auto_retry_sweep',
                updated_at=?,
                running_at=0,
                next_retry_at=0,
                last_error_code=?,
                retry_strategy='auto_same_pipeline',
                failure_revision=?
            WHERE path=?
              AND status='failed_retry'
              AND mtime_ns=?
              AND last_error=?
              AND last_error_code=?
              AND failure_revision=?
            """,
            (
                now,
                code,
                revision,
                normalized_path,
                normalized_expected_mtime_ns,
                observed_last_error,
                observed_last_error_code,
                observed_failure_revision,
            ),
        ).rowcount
        try:
            if Path(normalized_path).stat().st_mtime_ns != normalized_expected_mtime_ns:
                raise ValueError("exact retry media identity changed during queue transition")
        except OSError as exc:
            raise ValueError("exact retry media disappeared during queue transition") from exc
        if changed:
            self.update_ai_job_stage(
                path,
                "queued",
                "queued",
                "Safety-gated automatic retry queued without resetting its retry budget",
            )
        return changed == 1

    def pause_failed_retry_for_review(
        self,
        path: Path,
        *,
        expected_failure_revision: str,
        message: str,
    ) -> bool:
        """Reclassify an overlapping open review without spending GPU time."""

        normalized_path = _queue_path(path)
        cursor = self._conn.execute(
            """
            SELECT q.status, q.last_error, q.last_error_code, q.failure_revision,
                   j.stage AS job_stage, j.message AS job_message
            FROM ai_candidate_queue q
            LEFT JOIN ai_job_state j ON j.path=q.path
            WHERE q.path=?
            """,
            (normalized_path,),
        )
        columns = [str(description[0]) for description in cursor.description or ()]
        row = cursor.fetchone()
        payload = dict(zip(columns, row, strict=True)) if row is not None else None
        if payload is None or str(payload["status"] or "") != "failed_retry":
            return False
        code = str(payload["last_error_code"] or "") or _legacy_failure_code(
            str(payload["job_stage"] or ""),
            str(payload["last_error"] or payload["job_message"] or ""),
        )
        revision = str(payload["failure_revision"] or "") or _failure_revision(
            normalized_path,
            code,
            str(payload["last_error"] or payload["job_message"] or ""),
        )
        if revision != str(expected_failure_revision):
            return False
        now = time.time()
        changed = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status='paused',
                source='existing_quality_review',
                updated_at=?,
                running_at=0,
                next_retry_at=0,
                last_error_code=?,
                retry_strategy='manual_review',
                failure_revision=?
            WHERE path=? AND status='failed_retry'
            """,
            (now, code, revision, normalized_path),
        ).rowcount
        if changed:
            self.update_ai_job_stage(
                path,
                "review",
                "failed",
                str(message or "Open quality review blocks automatic retry")[:1000],
            )
        return changed == 1

    def ai_queue_candidate_snapshot(self, path: Path) -> dict[str, Any] | None:
        normalized_path = _queue_path(path)
        cursor = self._conn.execute(
            """
            SELECT
                q.*,
                j.stage AS job_stage,
                j.status AS job_status,
                j.message AS job_message,
                j.updated_at AS job_updated_at
            FROM ai_candidate_queue q
            LEFT JOIN ai_job_state j ON j.path=q.path
            WHERE q.path=?
            """,
            (normalized_path,),
        )
        columns = [str(description[0]) for description in cursor.description or ()]
        row = cursor.fetchone()
        return dict(zip(columns, row, strict=True)) if row is not None else None

    def recover_stale_ai_queue_candidate(self, path: Path, stale_after_seconds: int) -> bool:
        """Requeue exactly one running item only when its heartbeat is stale."""

        self.reconcile_completed_running()
        now = time.time()
        cutoff = now - max(60, int(stale_after_seconds or 0))
        normalized_path = _queue_path(path)
        row = self._conn.execute(
            """
            SELECT status,
                   COALESCE(
                       (SELECT ai_job_state.updated_at
                        FROM ai_job_state
                        WHERE ai_job_state.path = ai_candidate_queue.path),
                       updated_at, running_at, added_at, 0
                   ) AS heartbeat_at
            FROM ai_candidate_queue
            WHERE path = ?
            """,
            (normalized_path,),
        ).fetchone()
        if row is None or str(row[0] or "") != "running" or float(row[1] or 0) > cutoff:
            return False
        changed = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status='queued', source='manual_recover_running', attempts=0,
                running_at=0, last_error='', last_error_at=0,
                next_retry_at=0, updated_at=?
            WHERE path=? AND status='running'
              AND COALESCE(
                    (SELECT ai_job_state.updated_at
                     FROM ai_job_state
                     WHERE ai_job_state.path = ai_candidate_queue.path),
                    updated_at, running_at, added_at, 0
                  ) <= ?
            """,
            (now, normalized_path, cutoff),
        ).rowcount
        if changed:
            self.update_ai_job_stage(path, "queued", "queued", "Recovered stale running AI job")
        return changed == 1

    def pause_ai_queue_candidate(self, path: Path) -> None:
        now = time.time()
        self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'paused',
                updated_at = ?,
                running_at = 0,
                next_retry_at = 0
            WHERE path = ?
            """,
            (now, _queue_path(path)),
        )
        self.update_ai_job_stage(path, "paused", "paused", "Manual pause")

    def pause_exact_queued_ai_queue_candidate(
        self,
        path: Path,
        *,
        expected_failure_revision: str,
        expected_failure_code: str,
        expected_media_mtime_ns: int,
    ) -> bool:
        """Contain one stale canary row only while its persisted identity is exact."""

        revision = str(expected_failure_revision or "").strip()
        code = str(expected_failure_code or "").strip().casefold()
        if isinstance(expected_media_mtime_ns, bool):
            return False
        try:
            mtime_ns = int(expected_media_mtime_ns)
        except (TypeError, ValueError):
            return False
        if not re.fullmatch(r"[0-9a-f]{24}", revision) or not code or mtime_ns <= 0:
            return False
        now = time.time()
        changed = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status='paused', source='canary_binding_changed',
                running_at=0, next_retry_at=0, updated_at=?
            WHERE path=? AND status='queued' AND mtime_ns=?
              AND failure_revision=? AND last_error_code=?
            """,
            (now, _queue_path(path), mtime_ns, revision, code),
        ).rowcount
        if int(changed or 0) == 1:
            self.update_ai_job_stage(
                path,
                "paused",
                "paused",
                "Exact canary target binding changed before claim",
            )
            return True
        return False

    def skip_ai_queue_candidate(self, path: Path) -> None:
        now = time.time()
        self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'skipped',
                updated_at = ?,
                running_at = 0,
                next_retry_at = 0,
                force_ai = 0
            WHERE path = ?
            """,
            (now, _queue_path(path)),
        )
        self.update_ai_job_stage(path, "skipped", "skipped", "Manual skip")

    def prioritize_ai_queue_candidate(self, path: Path) -> None:
        now = time.time()
        cursor = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'queued',
                source = 'manual_priority',
                mtime_ns = ?,
                added_at = ?,
                updated_at = ?,
                next_retry_at = 0
            WHERE path = ?
              AND status NOT IN ('running', 'done')
            """,
            (time.time_ns(), now, now, _queue_path(path)),
        )
        if cursor.rowcount:
            self.update_ai_job_stage(path, "queued", "queued", "Manual priority boost")

    def update_ai_job_stage(
        self,
        path: Path,
        stage: str,
        status: str,
        message: str = "",
        *,
        event_time: float | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> None:
        now = time.time()
        event_at = float(event_time) if event_time is not None else now
        started_at_value = float(started_at) if started_at is not None else now
        normalized_path = _queue_path(path)
        normalized_stage = str(stage)
        normalized_status = str(status)
        normalized_message = str(message)[:1000]
        previous = self._conn.execute(
            "SELECT stage, status, message FROM ai_job_state WHERE path = ?",
            (normalized_path,),
        ).fetchone()
        suppress_event = bool(
            previous
            and str(previous[0]) == normalized_stage
            and str(previous[1]) == normalized_status
            and (
                normalized_status == "running"
                or str(previous[2] or "") == normalized_message
            )
        )
        finished_at_value = (
            float(finished_at)
            if finished_at is not None
            else (event_at if status in {"ok", "failed", "skipped"} else None)
        )
        self._conn.execute(
            """
            INSERT INTO ai_job_state (
                path,
                stage,
                status,
                message,
                started_at,
                updated_at,
                finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                stage = excluded.stage,
                status = excluded.status,
                message = excluded.message,
                started_at = CASE
                    WHEN excluded.stage = 'detected_existing' THEN excluded.started_at
                    WHEN excluded.status = 'running' THEN COALESCE(ai_job_state.started_at, excluded.started_at)
                    ELSE ai_job_state.started_at
                END,
                updated_at = excluded.updated_at,
                finished_at = CASE
                    WHEN excluded.status IN ('ok', 'failed', 'skipped') THEN excluded.finished_at
                    ELSE NULL
                END
            """,
            (
                normalized_path,
                normalized_stage,
                normalized_status,
                normalized_message,
                started_at_value,
                event_at,
                finished_at_value,
            ),
        )
        # A complete stage is only a progress heartbeat.  Queue completion and
        # delivery-ledger success are settled together by the parent after it
        # strictly verifies the current manifest.  This deliberately leaves the
        # queue row running across the child/parent crash window.
        if not suppress_event:
            self._conn.execute(
                """
                INSERT INTO ai_stage_events(path, stage, status, message, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (normalized_path, normalized_stage, normalized_status, normalized_message, now),
            )
            self._stage_events_since_prune += 1
            self._prune_stage_events()

    def requeue_stale_running(
        self,
        stale_after_seconds: int,
        *,
        reconcile_completed: bool = True,
    ) -> int:
        if reconcile_completed:
            self.reconcile_completed_running()
        stale_after_seconds = max(60, int(stale_after_seconds or 0))
        now = time.time()
        cutoff = now - stale_after_seconds
        rows = self._conn.execute(
            """
            SELECT path
            FROM ai_candidate_queue
            WHERE status = 'running'
              AND COALESCE(
                    (
                        SELECT ai_job_state.updated_at
                        FROM ai_job_state
                        WHERE ai_job_state.path = ai_candidate_queue.path
                    ),
                    updated_at,
                    running_at,
                    added_at,
                    0
                  ) <= ?
            ORDER BY COALESCE(running_at, updated_at, added_at, 0) ASC, path COLLATE NOCASE ASC
            """,
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0
        requeued = 0
        for (path,) in rows:
            cursor = self._conn.execute(
                """
                UPDATE ai_candidate_queue
                SET status = 'queued',
                    updated_at = ?,
                    running_at = 0,
                    next_retry_at = 0,
                    last_error = '',
                    last_error_at = 0
                WHERE path = ?
                  AND status = 'running'
                  AND COALESCE(
                        (
                            SELECT ai_job_state.updated_at
                            FROM ai_job_state
                            WHERE ai_job_state.path = ai_candidate_queue.path
                        ),
                        updated_at,
                        running_at,
                        added_at,
                        0
                      ) <= ?
                """,
                (now, str(path), cutoff),
            )
            if int(cursor.rowcount or 0) != 1:
                continue
            running_attempts = self._conn.execute(
                """
                SELECT a.attempt_id
                FROM ai_delivery_attempts a
                JOIN ai_delivery_obligations o
                  ON o.obligation_id = a.obligation_id
                WHERE o.canonical_path = ?
                  AND o.state = 'open'
                  AND a.status = 'running'
                ORDER BY a.started_at ASC, a.attempt_id ASC
                """,
                (str(path),),
            ).fetchall()
            for (attempt_id,) in running_attempts:
                self.finish_ai_delivery_attempt(
                    str(attempt_id),
                    status="deferred",
                    stage="stale_recovery",
                    error_code="stale_running_requeued",
                    detail="Timed-out running AI queue row was safely requeued",
                )
            self.update_ai_job_stage(
                Path(str(path)),
                "queued",
                "queued",
                "Requeued stale running AI job",
            )
            requeued += 1
        return requeued

    def requeue_acceptance_running_targets(
        self,
        targets: Sequence[AcceptanceQueueTarget],
        *,
        stale_after_seconds: int | None = None,
        message: str = "Worker restarted before this acceptance job finished",
    ) -> int:
        """Requeue only exact running identities from the fixed acceptance lane."""

        if len(targets) != ACCEPTANCE_QUEUE_TARGET_COUNT:
            raise ValueError(
                f"acceptance queue lane requires exactly {ACCEPTANCE_QUEUE_TARGET_COUNT} targets"
            )
        now = time.time()
        cutoff = (
            now - max(60, int(stale_after_seconds or 0))
            if stale_after_seconds is not None
            else None
        )
        requeued = 0
        for target in targets:
            stale_sql = ""
            parameters: list[Any] = [
                now,
                target.canonical_path,
                target.media_mtime_ns,
            ]
            if cutoff is not None:
                stale_sql = """
                  AND COALESCE(
                        (
                            SELECT ai_job_state.updated_at
                            FROM ai_job_state
                            WHERE ai_job_state.path = q.path
                        ),
                        q.updated_at,
                        q.running_at,
                        q.added_at,
                        0
                      ) <= ?
                """
                parameters.append(cutoff)
            parameters.extend(
                (
                    target.obligation_id,
                    target.media_fingerprint,
                    target.media_size,
                    target.policy_revision,
                )
            )
            changed = self._conn.execute(
                f"""
                UPDATE ai_candidate_queue AS q
                SET status = 'queued',
                    updated_at = ?,
                    running_at = 0,
                    next_retry_at = 0,
                    last_error = '',
                    last_error_at = 0
                WHERE q.path = ?
                  AND q.mtime_ns = ?
                  AND q.status = 'running'
                  {stale_sql}
                  AND EXISTS (
                        SELECT 1
                        FROM ai_delivery_obligations o
                        WHERE o.obligation_id = ?
                          AND o.canonical_path = q.path
                          AND o.media_fingerprint = ?
                          AND o.media_size = ?
                          AND o.media_mtime_ns = q.mtime_ns
                          AND o.policy_revision = ?
                          AND o.state = 'open'
                  )
                """,
                tuple(parameters),
            ).rowcount
            if int(changed or 0) != 1:
                continue
            attempts = self._conn.execute(
                """
                SELECT attempt_id
                FROM ai_delivery_attempts
                WHERE obligation_id = ? AND status = 'running'
                ORDER BY started_at ASC, attempt_id ASC
                """,
                (target.obligation_id,),
            ).fetchall()
            stage = "stale_recovery" if cutoff is not None else "restart_recovery"
            error_code = (
                "stale_running_requeued" if cutoff is not None else "worker_restarted"
            )
            for (attempt_id,) in attempts:
                self.finish_ai_delivery_attempt(
                    str(attempt_id),
                    status="deferred",
                    stage=stage,
                    error_code=error_code,
                    detail=message,
                )
            self.update_ai_job_stage(
                Path(target.canonical_path),
                "queued",
                "queued",
                message,
            )
            requeued += 1
        return requeued

    def requeue_running_from_previous_worker(
        self,
        message: str = "Worker restarted before this job finished",
        *,
        delivery_evidence_resolver: Callable[
            [Path, dict[str, Any], dict[str, Any]],
            dict[str, Any] | None,
        ]
        | None = None,
    ) -> int:
        self.reconcile_completed_running(
            delivery_evidence_resolver=delivery_evidence_resolver,
        )
        now = time.time()
        rows = self._conn.execute(
            """
            SELECT path
            FROM ai_candidate_queue
            WHERE status = 'running'
            ORDER BY COALESCE(running_at, updated_at, added_at, 0) ASC, path COLLATE NOCASE ASC
            """
        ).fetchall()
        if not rows:
            return 0

        interrupted_attempts = self._conn.execute(
            """
            SELECT a.attempt_id
            FROM ai_delivery_attempts a
            JOIN ai_delivery_obligations o
              ON o.obligation_id = a.obligation_id
            JOIN ai_candidate_queue q
              ON q.path = o.canonical_path
            WHERE q.status = 'running'
              AND o.state = 'open'
              AND a.status = 'running'
            ORDER BY a.started_at ASC, a.attempt_id ASC
            """
        ).fetchall()
        for (attempt_id,) in interrupted_attempts:
            self.finish_ai_delivery_attempt(
                str(attempt_id),
                status="deferred",
                stage="restart_recovery",
                error_code="worker_restarted",
                detail=message,
            )

        self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET status = 'queued',
                updated_at = ?,
                running_at = 0,
                next_retry_at = 0,
                last_error = '',
                last_error_at = 0
            WHERE status = 'running'
            """,
            (now,),
        )
        for (path,) in rows:
            self.update_ai_job_stage(Path(str(path)), "queued", "queued", message)
        return len(rows)

    def reconcile_completed_running(
        self,
        *,
        delivery_evidence_resolver: Callable[
            [Path, dict[str, Any], dict[str, Any]],
            dict[str, Any] | None,
        ]
        | None = None,
    ) -> int:
        """Settle crash-window completions only from strict delivery evidence.

        A terminal success stage is a progress heartbeat, not delivery proof.
        A parent process can die after that heartbeat but before it verifies the
        manifest and atomically settles the delivery ledger.  When this storage
        layer has no verifier, the row deliberately remains ``running`` so the
        caller's normal restart/stale recovery requeues it.
        """

        if delivery_evidence_resolver is None:
            return 0

        rows = self._conn.execute(
            """
            SELECT q.path
            FROM ai_candidate_queue q
            JOIN ai_job_state j ON j.path = q.path
            WHERE q.status = 'running'
              AND j.stage IN ('complete', 'source_translation')
              AND j.status = 'ok'
            ORDER BY q.path COLLATE NOCASE
            """
        ).fetchall()
        settled = 0
        for (raw_path,) in rows:
            path = Path(str(raw_path))
            attempt_row = self._conn.execute(
                """
                SELECT a.attempt_id
                FROM ai_delivery_attempts a
                JOIN ai_delivery_obligations o
                  ON o.obligation_id = a.obligation_id
                WHERE o.canonical_path = ?
                  AND o.state = 'open'
                  AND a.status = 'running'
                ORDER BY a.attempt_number DESC
                LIMIT 1
                """,
                (str(raw_path),),
            ).fetchone()
            if attempt_row is None:
                continue
            attempt = self.get_ai_delivery_attempt(str(attempt_row[0]))
            if attempt is None:
                continue
            obligation = self.get_ai_delivery_obligation(str(attempt["obligation_id"]))
            if obligation is None:
                continue
            try:
                evidence = delivery_evidence_resolver(path, attempt, obligation)
            except Exception:
                # Verification is read-only recovery work.  Any verifier error
                # must fail closed and let the caller requeue the job.
                continue
            if not _strict_recovery_delivery_evidence(evidence, attempt):
                continue

            verification = dict(evidence["verification"])
            self.finish_ai_delivery_attempt(
                str(attempt["attempt_id"]),
                status="succeeded",
                stage="delivery_verification",
            )
            self.mark_ai_delivery_verified(
                str(obligation["obligation_id"]),
                manifest_path=str(evidence["manifest_path"]),
                manifest_sha256=str(evidence["manifest_sha256"]),
                verification=verification,
                evidence_verified=True,
                verified_at=float(evidence["verified_at"]),
            )
            self.mark_ai_queue_done(path)
            settled += 1
        return settled

    def iter_ai_queue_candidates(
        self,
        *,
        oldest_first: bool = False,
        now: float | None = None,
        acceptance_targets: Sequence[AcceptanceQueueTarget] | None = None,
        exact_target: Path | None = None,
    ) -> list[Path]:
        """Return queued work plus only provenance-backed due retries.

        Historical ``failed_retry`` rows remain behind the explicit remediation
        sweep.  A normal automatic retry is dispatchable only when the queue
        failure is tied to the latest attempt of an open delivery obligation,
        that attempt ended as ``retryable_failure``, and its recorded strategy
        is one of the bounded automatic strategies.  This prevents legacy or
        manually-reviewable failures from silently entering the scheduler.
        Except for explicit manual/force priority, current media identities
        with an open delivery obligation use earliest-deadline-first ordering.
        Queue age remains only the compatibility fallback for untracked rows.
        """

        observed_at = float(time.time() if now is None else now)
        if acceptance_targets is not None:
            return self._iter_acceptance_queue_candidates(
                acceptance_targets,
                observed_at=observed_at,
                exact_target=exact_target,
            )

        if oldest_first:
            queue_tail_order = """
                COALESCE(q.added_at, 0) ASC,
                q.mtime_ns ASC
            """
        else:
            queue_tail_order = """
                CASE WHEN q.filename_season IS NULL THEN 1 ELSE 0 END ASC,
                q.filename_season DESC,
                CASE WHEN q.filename_episode IS NULL THEN 1 ELSE 0 END ASC,
                q.filename_episode DESC,
                q.mtime_ns DESC,
                COALESCE(q.added_at, 0) DESC
            """
        retry_strategies = tuple(sorted(AI_QUEUE_AUTOMATIC_RETRY_STRATEGIES))
        retry_placeholders = ", ".join("?" for _item in retry_strategies)
        normalized_exact_target = (
            _queue_path(exact_target) if exact_target is not None else None
        )
        exact_target_sql = (
            " AND q.path = ?" if normalized_exact_target is not None else ""
        )
        parameters: list[Any] = [
            observed_at,
            *retry_strategies,
            AI_QUEUE_RETRY_ATTEMPT_LINK_MAX_SKEW_SECONDS,
        ]
        if normalized_exact_target is not None:
            parameters.append(normalized_exact_target)
        rows = self._conn.execute(
            f"""
            SELECT q.path
            FROM ai_candidate_queue q
            LEFT JOIN (
                SELECT canonical_path, media_mtime_ns, MIN(due_at) AS earliest_due_at
                FROM ai_delivery_obligations
                WHERE state = 'open'
                GROUP BY canonical_path, media_mtime_ns
            ) deadline
              ON deadline.canonical_path = q.path
             AND deadline.media_mtime_ns = q.mtime_ns
            WHERE (
                    q.status = 'queued'
                 OR (
                    q.status = 'failed_retry'
                    AND q.next_retry_at <= ?
                    AND q.last_error_at > 0
                    AND q.last_error_code != ''
                    AND q.retry_strategy IN ({retry_placeholders})
                    AND EXISTS (
                        SELECT 1
                        FROM ai_delivery_obligations o
                        JOIN ai_delivery_attempts a
                          ON a.obligation_id = o.obligation_id
                        WHERE o.canonical_path = q.path
                          AND o.state = 'open'
                          AND a.attempt_number = o.attempt_count
                          AND a.status = 'retryable_failure'
                          AND a.error_code = q.last_error_code
                          AND a.finished_at >= q.last_error_at
                          AND a.finished_at <= q.last_error_at + ?
                    )
                 )
            )
            {exact_target_sql}
            ORDER BY
                CASE WHEN q.source = 'manual_priority' THEN 1 ELSE 0 END DESC,
                COALESCE(q.force_ai, 0) DESC,
                CASE WHEN deadline.earliest_due_at IS NULL THEN 1 ELSE 0 END ASC,
                deadline.earliest_due_at ASC,
                CASE WHEN q.status = 'failed_retry' THEN 0 ELSE 1 END ASC,
                CASE WHEN q.status = 'failed_retry' THEN q.next_retry_at ELSE 0 END ASC,
                {queue_tail_order},
                q.path COLLATE NOCASE ASC
            """,
            parameters,
        ).fetchall()
        return [Path(str(row[0])) for row in rows]

    def _iter_acceptance_queue_candidates(
        self,
        targets: Sequence[AcceptanceQueueTarget],
        *,
        observed_at: float,
        exact_target: Path | None = None,
    ) -> list[Path]:
        """Return only exact open identities from the fixed 100-case lane."""

        if len(targets) != ACCEPTANCE_QUEUE_TARGET_COUNT:
            raise ValueError(
                f"acceptance queue lane requires exactly {ACCEPTANCE_QUEUE_TARGET_COUNT} targets"
            )
        values_sql = ",".join("(?,?,?,?,?,?,?)" for _target in targets)
        target_parameters: list[Any] = []
        for target in targets:
            target_parameters.extend(
                (
                    target.ordinal,
                    target.canonical_path,
                    target.media_size,
                    target.media_mtime_ns,
                    target.media_fingerprint,
                    target.policy_revision,
                    target.obligation_id,
                )
            )
        retry_strategies = tuple(sorted(AI_QUEUE_AUTOMATIC_RETRY_STRATEGIES))
        retry_placeholders = ", ".join("?" for _item in retry_strategies)
        normalized_exact_target = (
            _queue_path(exact_target) if exact_target is not None else None
        )
        exact_target_sql = (
            " AND q.path = ?" if normalized_exact_target is not None else ""
        )
        parameters: list[Any] = [
            *target_parameters,
            observed_at,
            *retry_strategies,
            AI_QUEUE_RETRY_ATTEMPT_LINK_MAX_SKEW_SECONDS,
        ]
        if normalized_exact_target is not None:
            parameters.append(normalized_exact_target)
        rows = self._conn.execute(
            f"""
            WITH acceptance_targets(
                ordinal, canonical_path, media_size, media_mtime_ns,
                media_fingerprint, policy_revision, obligation_id
            ) AS (VALUES {values_sql})
            SELECT q.path
            FROM acceptance_targets t
            JOIN ai_candidate_queue q
              ON q.path = t.canonical_path
             AND q.mtime_ns = t.media_mtime_ns
            JOIN ai_delivery_obligations o
              ON o.obligation_id = t.obligation_id
             AND o.canonical_path = t.canonical_path
             AND o.media_size = t.media_size
             AND o.media_mtime_ns = t.media_mtime_ns
             AND o.media_fingerprint = t.media_fingerprint
             AND o.policy_revision = t.policy_revision
             AND o.state = 'open'
            WHERE (
                    q.status = 'queued'
                 OR (
                    q.status = 'failed_retry'
                    AND q.next_retry_at <= ?
                    AND q.last_error_at > 0
                    AND q.last_error_code != ''
                    AND q.retry_strategy IN ({retry_placeholders})
                    AND EXISTS (
                        SELECT 1
                        FROM ai_delivery_attempts a
                        WHERE a.obligation_id = t.obligation_id
                          AND a.attempt_number = o.attempt_count
                          AND a.status = 'retryable_failure'
                          AND a.error_code = q.last_error_code
                          AND a.finished_at >= q.last_error_at
                          AND a.finished_at <= q.last_error_at + ?
                    )
                 )
            )
            {exact_target_sql}
            ORDER BY t.ordinal ASC
            """,
            parameters,
        ).fetchall()
        return [Path(str(row[0])) for row in rows]

    def untracked_active_ai_queue_candidates(
        self,
        *,
        policy_revision: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return active queue rows without an exact current open obligation."""

        normalized_policy = str(policy_revision or "").strip()
        if not normalized_policy:
            raise ValueError("policy_revision is required")
        rows = self._conn.execute(
            """
            SELECT q.path, q.mtime_ns, q.status, q.force_ai, q.source
            FROM ai_candidate_queue AS q
            WHERE q.status IN ('queued', 'running', 'failed_retry', 'paused')
              AND NOT EXISTS (
                    SELECT 1
                    FROM ai_delivery_obligations AS o
                    WHERE o.canonical_path=q.path
                      AND o.media_mtime_ns=q.mtime_ns
                      AND o.policy_revision=?
                      AND o.state='open'
              )
            ORDER BY
                CASE WHEN q.status='running' THEN 1 ELSE 0 END,
                q.added_at ASC,
                q.path COLLATE NOCASE ASC
            LIMIT ?
            """,
            (normalized_policy, max(1, int(limit or 1))),
        ).fetchall()
        return [
            {
                "path": Path(str(row[0])),
                "mtime_ns": int(row[1] or 0),
                "status": str(row[2] or ""),
                "force_ai": bool(row[3]),
                "source": str(row[4] or ""),
            }
            for row in rows
        ]

    def begin_active_ai_queue_ledger_backfill(
        self,
        path: Path,
        *,
        expected_mtime_ns: int,
        expected_status: str,
        expected_force_ai: bool,
        expected_source: str,
    ) -> bool:
        """Lock one unchanged active queue row before repairing its ledger."""

        cursor = self._conn.execute(
            """
            UPDATE ai_candidate_queue
            SET updated_at=updated_at
            WHERE path=? AND mtime_ns=? AND status=? AND force_ai=? AND source=?
              AND status IN ('queued', 'running', 'failed_retry', 'paused')
            """,
            (
                _queue_path(path),
                int(expected_mtime_ns),
                str(expected_status or ""),
                1 if expected_force_ai else 0,
                str(expected_source or ""),
            ),
        )
        return int(cursor.rowcount or 0) == 1

    def ensure_ai_delivery_obligation(
        self,
        path: Path,
        *,
        media_size: int,
        media_mtime_ns: int,
        policy_revision: str,
        eligible_at: float | None = None,
        source: str = "scan",
        obligation_id: str | None = None,
        acceptance_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one durable AI delivery denominator unit, idempotently.

        The caller owns policy-revision calculation. Retries must reuse the
        returned obligation id; the database identity prevents a repeated scan
        from adding another denominator unit for the same media revision. A
        zero-attempt exclusion is reopened in place when that exact identity
        becomes eligible again; attempted and other terminal obligations are
        never reopened.
        """

        identity = ai_delivery_identity(
            path,
            media_size=media_size,
            media_mtime_ns=media_mtime_ns,
            policy_revision=policy_revision,
        )
        expected_id = str(identity["obligation_id"])
        normalized_id = str(obligation_id or expected_id).strip()
        normalized_acceptance_run_id = str(acceptance_run_id or "").strip()
        if normalized_id != expected_id:
            raise ValueError(
                "obligation_id does not match canonical path/media/policy identity: "
                f"expected={expected_id} actual={normalized_id}"
            )
        now = time.time()
        requested_eligible_at = float(now if eligible_at is None else eligible_at)
        if requested_eligible_at <= 0:
            raise ValueError("eligible_at must be positive")
        started_outer_transaction = not self._conn.in_transaction
        if started_outer_transaction:
            self._conn.execute("BEGIN")
        savepoint = "ensure_ai_delivery_obligation"
        self._conn.execute(f"SAVEPOINT {savepoint}")
        try:
            lineage_row = self._conn.execute(
                """
                SELECT MIN(eligible_at), MIN(due_at)
                FROM ai_delivery_obligations
                WHERE canonical_path=? AND state='open' AND attempt_count=0
                """,
                (identity["canonical_path"],),
            ).fetchone()
            admitted_at = requested_eligible_at
            due_at = admitted_at + AI_DELIVERY_DEADLINE_SECONDS
            if lineage_row is not None and lineage_row[0] is not None:
                lineage_eligible_at = float(lineage_row[0])
                lineage_due_at = float(lineage_row[1])
                if lineage_eligible_at <= 0 or lineage_due_at < lineage_eligible_at:
                    raise RuntimeError("AI delivery path lineage has invalid deadline timestamps")
                admitted_at = lineage_eligible_at
                due_at = lineage_due_at
            self._conn.execute(
                """
                INSERT OR IGNORE INTO ai_delivery_obligations(
                    obligation_id, canonical_path, media_fingerprint, media_size,
                    media_mtime_ns, policy_revision, acceptance_run_id, source, state, eligible_at,
                    due_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    identity["canonical_path"],
                    identity["media_fingerprint"],
                    int(media_size),
                    int(media_mtime_ns),
                    identity["policy_revision"],
                    normalized_acceptance_run_id or None,
                    str(source or "scan")[:100],
                    admitted_at,
                    due_at,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                f"SELECT {', '.join(_AI_DELIVERY_OBLIGATION_FIELDS)} "
                "FROM ai_delivery_obligations "
                "WHERE canonical_path=? AND media_fingerprint=? AND media_size=? "
                "AND media_mtime_ns=? AND policy_revision=?",
                (
                    identity["canonical_path"],
                    identity["media_fingerprint"],
                    int(media_size),
                    int(media_mtime_ns),
                    identity["policy_revision"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("AI delivery obligation insert could not be read back")
            stored = _ai_delivery_obligation_dict(row)
            if stored["obligation_id"] != normalized_id:
                raise ValueError(
                    "AI delivery identity is already owned by another obligation id: "
                    f"stored={stored['obligation_id']} requested={normalized_id}"
                )
            if stored.get("acceptance_run_id", "") != normalized_acceptance_run_id:
                raise ValueError(
                    "AI delivery identity is bound to a different acceptance run: "
                    f"stored={stored.get('acceptance_run_id') or '-'} "
                    f"requested={normalized_acceptance_run_id or '-'}"
                )
            if stored["state"] == "excluded" and int(stored["attempt_count"]) == 0:
                reopened = self._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET source=?, state='open',
                        verified_at=0, terminal_at=0, outcome_code='',
                        exclusion_code='', exclusion_detail='', manifest_path='',
                        manifest_sha256='', verification_json='{}', updated_at=?
                    WHERE obligation_id=? AND state='excluded' AND attempt_count=0
                    """,
                    (
                        str(source or "scan")[:100],
                        now,
                        normalized_id,
                    ),
                ).rowcount
                if reopened:
                    stored = self.get_ai_delivery_obligation(normalized_id) or {}
                    if not stored:
                        raise RuntimeError("Reopened AI delivery obligation could not be read back")
            if stored["state"] == "open":
                self.exclude_pre_attempt_ai_delivery_obligations_for_path(
                    path,
                    exclusion_code="superseded_before_attempt",
                    detail=(
                        "Superseded by the current media/policy revision "
                        f"obligation_id={normalized_id}"
                    ),
                    keep_obligation_id=normalized_id,
                )
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return stored
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            if started_outer_transaction:
                self._conn.rollback()
            raise

    def get_ai_delivery_obligation(self, obligation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_AI_DELIVERY_OBLIGATION_FIELDS)} "
            "FROM ai_delivery_obligations WHERE obligation_id=?",
            (str(obligation_id),),
        ).fetchone()
        return _ai_delivery_obligation_dict(row) if row is not None else None

    def begin_ai_delivery_attempt(
        self,
        obligation_id: str,
        *,
        attempt_id: str | None = None,
        started_at: float | None = None,
        acceptance_run_id: str | None = None,
    ) -> dict[str, Any]:
        obligation = self.get_ai_delivery_obligation(obligation_id)
        if obligation is None:
            raise ValueError(f"AI delivery obligation does not exist: {obligation_id}")
        if obligation["state"] != "open":
            raise ValueError(
                f"AI delivery obligation is not open: {obligation_id} state={obligation['state']}"
            )
        normalized_acceptance_run_id = str(acceptance_run_id or "").strip()
        if obligation.get("acceptance_run_id", "") != normalized_acceptance_run_id:
            raise ValueError(
                "AI delivery attempt acceptance run does not match its obligation"
            )
        now = time.time()
        started = float(now if started_at is None else started_at)
        attempt_number = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 "
                "FROM ai_delivery_attempts WHERE obligation_id=?",
                (str(obligation_id),),
            ).fetchone()[0]
        )
        expected_id = _stable_ai_delivery_attempt_id(str(obligation_id), attempt_number)
        normalized_id = str(attempt_id or expected_id).strip()
        if not normalized_id:
            raise ValueError("attempt_id is required")
        self._conn.execute(
            """
            INSERT INTO ai_delivery_attempts(
                attempt_id, obligation_id, acceptance_run_id, attempt_number, status, started_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                normalized_id,
                str(obligation_id),
                normalized_acceptance_run_id or None,
                attempt_number,
                started,
                now,
                now,
            ),
        )
        self._conn.execute(
            """
            UPDATE ai_delivery_obligations
            SET attempt_count=attempt_count + 1, updated_at=?
            WHERE obligation_id=?
            """,
            (now, str(obligation_id)),
        )
        return self.get_ai_delivery_attempt(normalized_id) or {}

    def get_ai_delivery_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            f"SELECT {', '.join(_AI_DELIVERY_ATTEMPT_FIELDS)} "
            "FROM ai_delivery_attempts WHERE attempt_id=?",
            (str(attempt_id),),
        ).fetchone()
        return _ai_delivery_attempt_dict(row) if row is not None else None

    def latest_ai_delivery_attempt(self, obligation_id: str) -> dict[str, Any] | None:
        """Return the newest durable attempt for one delivery obligation."""

        row = self._conn.execute(
            f"SELECT {', '.join(_AI_DELIVERY_ATTEMPT_FIELDS)} "
            "FROM ai_delivery_attempts WHERE obligation_id=? "
            "ORDER BY attempt_number DESC LIMIT 1",
            (str(obligation_id),),
        ).fetchone()
        return _ai_delivery_attempt_dict(row) if row is not None else None

    def finish_ai_delivery_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        stage: str = "",
        error_code: str = "",
        detail: str = "",
        finished_at: float | None = None,
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip().casefold()
        if normalized_status not in AI_DELIVERY_ATTEMPT_STATUSES - {"running"}:
            raise ValueError(f"invalid AI delivery attempt status: {status}")
        now = time.time()
        finished = float(now if finished_at is None else finished_at)
        changed = self._conn.execute(
            """
            UPDATE ai_delivery_attempts
            SET status=?, stage=?, error_code=?, detail=?, finished_at=?, updated_at=?
            WHERE attempt_id=? AND status='running'
            """,
            (
                normalized_status,
                str(stage or "")[:100],
                str(error_code or "")[:100],
                str(detail or "")[:1000],
                finished,
                now,
                str(attempt_id),
            ),
        ).rowcount
        row = self.get_ai_delivery_attempt(attempt_id)
        if row is None:
            raise ValueError(f"AI delivery attempt does not exist: {attempt_id}")
        if not changed and row["status"] != normalized_status:
            raise ValueError(
                f"AI delivery attempt is already terminal: {attempt_id} status={row['status']}"
            )
        self._conn.execute(
            """
            UPDATE ai_delivery_obligations
            SET outcome_code=?, updated_at=?
            WHERE obligation_id=? AND state='open'
            """,
            (str(error_code or normalized_status)[:100], now, row["obligation_id"]),
        )
        return row

    def mark_ai_delivery_verified(
        self,
        obligation_id: str,
        *,
        manifest_path: str,
        manifest_sha256: str,
        verification: dict[str, Any],
        evidence_verified: bool,
        verified_at: float | None = None,
    ) -> dict[str, Any]:
        """Record success only after the caller has strictly verified v2 evidence."""

        if evidence_verified is not True:
            raise ValueError("strictly verified delivery evidence is required")
        normalized_manifest = str(manifest_path or "").strip()
        normalized_sha = str(manifest_sha256 or "").strip().casefold()
        if not normalized_manifest or not re.fullmatch(r"[0-9a-f]{64}", normalized_sha):
            raise ValueError("manifest path and SHA-256 are required")
        if not isinstance(verification, dict) or not verification:
            raise ValueError("verification evidence is required")
        obligation = self.get_ai_delivery_obligation(obligation_id)
        if obligation is None:
            raise ValueError(f"AI delivery obligation does not exist: {obligation_id}")
        if obligation["state"] == "excluded":
            raise ValueError("excluded AI delivery obligation cannot be marked successful")
        publication = _verified_ai_delivery_publication_semantics(
            verification,
            expected_policy_revision=str(obligation["policy_revision"]),
        )
        if publication is None:
            raise ValueError(
                "strict delivery evidence must include audited publication kind, "
                "output languages, and exact policy identity"
            )
        verified = float(time.time() if verified_at is None else verified_at)
        now = time.time()
        if obligation["state"] == "succeeded":
            return obligation
        self._conn.execute(
            """
            UPDATE ai_delivery_obligations
            SET state='succeeded', verified_at=?, terminal_at=?,
                outcome_code=?, manifest_path=?, manifest_sha256=?,
                verification_json=?, updated_at=?
            WHERE obligation_id=? AND state='open'
            """,
            (
                verified,
                verified,
                "verified_on_time" if verified <= float(obligation["due_at"]) else "verified_late",
                normalized_manifest,
                normalized_sha,
                json.dumps(verification, ensure_ascii=False, sort_keys=True),
                now,
                str(obligation_id),
            ),
        )
        return self.get_ai_delivery_obligation(obligation_id) or {}

    def exclude_ai_delivery_obligation(
        self,
        obligation_id: str,
        *,
        exclusion_code: str,
        detail: str = "",
        excluded_at: float | None = None,
    ) -> dict[str, Any]:
        normalized_code = str(exclusion_code or "").strip().casefold()
        if normalized_code not in AI_DELIVERY_EXCLUSION_CODES:
            raise ValueError(f"invalid AI delivery exclusion code: {exclusion_code}")
        obligation = self.get_ai_delivery_obligation(obligation_id)
        if obligation is None:
            raise ValueError(f"AI delivery obligation does not exist: {obligation_id}")
        if obligation["state"] == "excluded" and obligation["exclusion_code"] == normalized_code:
            if not (
                int(obligation["attempt_count"]) == 0
                and 0 < float(obligation["terminal_at"])
                and float(obligation["eligible_at"])
                <= float(obligation["terminal_at"])
                <= float(obligation["updated_at"])
                <= float(obligation["due_at"])
            ):
                raise ValueError("existing AI delivery exclusion is not valid SLO evidence")
            return obligation
        if obligation["state"] != "open" or int(obligation["attempt_count"]) != 0:
            raise ValueError("AI delivery exclusions are allowed only before the first attempt")
        terminal = float(time.time() if excluded_at is None else excluded_at)
        now = time.time()
        # Equality at the deadline is deliberate: the 72-hour contract is
        # inclusive, matching verified_at <= due_at for successful delivery.
        if terminal <= 0 or not (
            float(obligation["eligible_at"])
            <= terminal
            <= now
            <= float(obligation["due_at"])
        ):
            raise ValueError(
                "AI delivery exclusions must be recorded between eligibility and the delivery deadline"
            )
        changed = self._conn.execute(
            """
            UPDATE ai_delivery_obligations
            SET state='excluded', terminal_at=?, outcome_code='excluded',
                exclusion_code=?, exclusion_detail=?, updated_at=?
            WHERE obligation_id=? AND state='open' AND attempt_count=0
              AND ?>0 AND ?>=eligible_at AND ?<=? AND ?<=due_at
            """,
            (
                terminal,
                normalized_code,
                str(detail or "")[:1000],
                now,
                str(obligation_id),
                terminal,
                terminal,
                terminal,
                now,
                now,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("AI delivery obligation changed before exclusion could be recorded")
        return self.get_ai_delivery_obligation(obligation_id) or {}

    def exclude_pre_attempt_ai_delivery_obligations_for_path(
        self,
        path: str | Path,
        *,
        exclusion_code: str,
        detail: str = "",
        excluded_at: float | None = None,
        keep_obligation_id: str | None = None,
    ) -> int:
        """Exclude exact-path obligations that have never started an attempt.

        ``keep_obligation_id`` preserves the current media/policy identity while
        superseding older revisions.  The SQL predicate is deliberately
        fail-closed: attempted or terminal obligations are historical SLO facts
        and can never be removed from the denominator by scanner reconciliation.
        Returns the number of obligations newly excluded.
        """

        normalized_code = str(exclusion_code or "").strip().casefold()
        if normalized_code not in AI_DELIVERY_EXCLUSION_CODES:
            raise ValueError(f"invalid AI delivery exclusion code: {exclusion_code}")
        canonical_path = str(Path(path).resolve())
        preserved_id = str(keep_obligation_id or "").strip()
        terminal = float(time.time() if excluded_at is None else excluded_at)
        if terminal <= 0:
            raise ValueError("excluded_at must be positive")
        now = time.time()
        if terminal > now:
            raise ValueError("excluded_at cannot be later than the observation time")
        predicate = (
            "canonical_path=? AND state='open' AND attempt_count=0 "
            "AND ?>0 AND ?>=eligible_at AND ?<=? AND ?<=due_at"
        )
        parameters: list[Any] = [
            terminal,
            normalized_code,
            str(detail or "")[:1000],
            now,
            canonical_path,
            terminal,
            terminal,
            terminal,
            now,
            now,
        ]
        if preserved_id:
            predicate += " AND obligation_id<>?"
            parameters.append(preserved_id)
        changed = self._conn.execute(
            f"""
            UPDATE ai_delivery_obligations
            SET state='excluded', terminal_at=?, outcome_code='excluded',
                exclusion_code=?, exclusion_detail=?, updated_at=?
            WHERE {predicate}
            """,
            parameters,
        ).rowcount
        return max(0, int(changed))

    def resume_ai_inventory_epoch(
        self,
        *,
        policy_revision: str,
        root_signature: str,
        resumed_at: float | None = None,
    ) -> dict[str, Any] | None:
        """Renew a matching interrupted proof epoch for a fresh root walk.

        The caller must re-walk the complete root.  Existing observations are
        retained as restart progress, but ``started_at`` becomes the boundary
        for the renewed walk.  Finalization removes observations that were not
        seen again after that boundary, so deleted or replaced media cannot be
        carried into the completed proof.
        """

        normalized_policy = str(policy_revision or "").strip()
        normalized_root = str(root_signature or "").strip()
        if not normalized_policy or not normalized_root:
            raise ValueError("inventory policy and root signatures are required")
        now = float(time.time() if resumed_at is None else resumed_at)
        if now <= 0:
            raise ValueError("inventory epoch resumed_at must be positive")
        meta = dict(self._conn.execute("SELECT key, value FROM ai_delivery_meta").fetchall())
        measurement_revision = str(meta.get("measurement_revision") or "").strip()
        if measurement_revision != AI_DELIVERY_MEASUREMENT_REVISION:
            raise RuntimeError("inventory epoch cannot resume under a different measurement revision")
        try:
            instrumented_at = float(meta.get("instrumented_at") or 0)
            dirty_generation = int(meta.get("inventory_dirty_generation") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("inventory epoch resume metadata is invalid") from exc
        if instrumented_at <= 0:
            raise RuntimeError("inventory epoch requires a positive instrumented_at")
        running = self._conn.execute(
            """
            SELECT epoch_id, started_at, eligibility_bound
            FROM ai_inventory_epochs
            WHERE state='running' AND schema_version=? AND measurement_revision=?
              AND policy_revision=? AND root_signature=? AND walk_error_count=0
            ORDER BY started_at DESC LIMIT 1
            """,
            (
                AI_INVENTORY_SCHEMA_VERSION,
                measurement_revision,
                normalized_policy,
                normalized_root,
            ),
        ).fetchone()
        if running is None:
            return None
        epoch_id = str(running[0])
        prior_started_at = float(running[1])
        changed = self._conn.execute(
            """
            UPDATE ai_inventory_epochs
            SET started_at=?, updated_at=?, dirty_generation=?, completed_at=0,
                observed_count=0, classified_count=0, delivery_required_count=0,
                tracked_count=0, untracked_count=0,
                legacy_preinstrumented_ai_count=0, coverage_complete=0,
                failure_code='', failure_detail=''
            WHERE epoch_id=? AND state='running' AND walk_error_count=0
            """,
            (now, now, dirty_generation, epoch_id),
        ).rowcount
        if int(changed or 0) != 1:
            raise RuntimeError("inventory epoch changed before it could be resumed")
        for key, value in (
            ("inventory_schema_version", str(AI_INVENTORY_SCHEMA_VERSION)),
            ("inventory_current_policy_revision", normalized_policy),
            ("inventory_current_root_signature", normalized_root),
        ):
            self._conn.execute(
                """
                INSERT INTO ai_delivery_meta(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
        return {
            "epoch_id": epoch_id,
            "measurement_revision": measurement_revision,
            "policy_revision": normalized_policy,
            "root_signature": normalized_root,
            "started_at": now,
            "resumed_from_started_at": prior_started_at,
            "eligibility_bound": float(running[2]),
            "instrumented_at": instrumented_at,
            "resumed": True,
        }

    def begin_ai_inventory_epoch(
        self,
        *,
        policy_revision: str,
        root_signature: str,
        started_at: float | None = None,
    ) -> dict[str, Any]:
        """Start a new root proof epoch and abandon any interrupted epoch."""

        normalized_policy = str(policy_revision or "").strip()
        normalized_root = str(root_signature or "").strip()
        if not normalized_policy or not normalized_root:
            raise ValueError("inventory policy and root signatures are required")
        now = float(time.time() if started_at is None else started_at)
        if now <= 0:
            raise ValueError("inventory epoch started_at must be positive")
        meta = dict(self._conn.execute("SELECT key, value FROM ai_delivery_meta").fetchall())
        measurement_revision = str(meta.get("measurement_revision") or "").strip()
        if measurement_revision != AI_DELIVERY_MEASUREMENT_REVISION:
            raise RuntimeError("inventory epoch cannot start under a different measurement revision")
        try:
            instrumented_at = float(meta.get("instrumented_at") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("inventory epoch requires a valid instrumented_at") from exc
        if instrumented_at <= 0:
            raise RuntimeError("inventory epoch requires a positive instrumented_at")

        self._conn.execute(
            """
            UPDATE ai_inventory_epochs
            SET state='abandoned', updated_at=?, failure_code='worker_restarted',
                failure_detail='Interrupted proof epoch was restarted from the media root',
                coverage_complete=0
            WHERE state='running'
            """,
            (now,),
        )
        previous = self._conn.execute(
            """
            SELECT completed_at FROM ai_inventory_epochs
            WHERE state='completed' AND measurement_revision=?
              AND policy_revision=? AND root_signature=? AND completed_at>=?
            ORDER BY completed_at DESC LIMIT 1
            """,
            (measurement_revision, normalized_policy, normalized_root, instrumented_at),
        ).fetchone()
        eligibility_bound = max(
            instrumented_at,
            float(previous[0] or 0) if previous is not None else instrumented_at,
        )
        try:
            dirty_generation = int(meta.get("inventory_dirty_generation") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("inventory epoch requires a valid dirty generation") from exc
        epoch_id = f"aiinv_{uuid.uuid4().hex}"
        self._conn.execute(
            """
            INSERT INTO ai_inventory_epochs(
                epoch_id, schema_version, measurement_revision, policy_revision,
                root_signature, state, started_at, eligibility_bound,
                dirty_generation, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                epoch_id,
                AI_INVENTORY_SCHEMA_VERSION,
                measurement_revision,
                normalized_policy,
                normalized_root,
                now,
                eligibility_bound,
                dirty_generation,
                now,
            ),
        )
        for key, value in (
            ("inventory_schema_version", str(AI_INVENTORY_SCHEMA_VERSION)),
            ("inventory_current_policy_revision", normalized_policy),
            ("inventory_current_root_signature", normalized_root),
        ):
            self._conn.execute(
                """
                INSERT INTO ai_delivery_meta(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )
        return {
            "epoch_id": epoch_id,
            "measurement_revision": measurement_revision,
            "policy_revision": normalized_policy,
            "root_signature": normalized_root,
            "started_at": now,
            "eligibility_bound": eligibility_bound,
            "instrumented_at": instrumented_at,
        }

    def begin_ai_inventory_path(self, epoch_id: str) -> None:
        if self._conn.in_transaction:
            raise RuntimeError("inventory path write requires a clean transaction")
        # Acquire the WAL writer before reading the epoch. A deferred read
        # transaction cannot be upgraded after the active AI child commits a
        # heartbeat, which otherwise produces an immediate SQLITE_BUSY_SNAPSHOT
        # loop even though every individual writer is short-lived.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT state FROM ai_inventory_epochs WHERE epoch_id=?",
                (str(epoch_id),),
            ).fetchone()
            if row is None or str(row[0]) != "running":
                raise RuntimeError("inventory path write requires a running epoch")
            self._conn.execute("SAVEPOINT ai_inventory_path")
        except Exception:
            self._conn.rollback()
            raise

    def commit_ai_inventory_path(self) -> None:
        self._conn.execute("RELEASE SAVEPOINT ai_inventory_path")

    def rollback_ai_inventory_path(self) -> None:
        try:
            self._conn.execute("ROLLBACK TO SAVEPOINT ai_inventory_path")
        finally:
            self._conn.execute("RELEASE SAVEPOINT ai_inventory_path")

    def record_ai_inventory_observation(
        self,
        epoch_id: str,
        path: Path,
        *,
        media_size: int,
        media_mtime_ns: int,
        policy_revision: str,
        classification: str,
        disposition: str,
        ai_output_detected: bool = False,
        ai_output_mtime: float = 0.0,
        observed_at: float | None = None,
    ) -> dict[str, Any]:
        normalized_disposition = str(disposition or "").strip().casefold()
        if normalized_disposition not in AI_INVENTORY_DISPOSITIONS:
            raise ValueError(f"invalid AI inventory disposition: {disposition}")
        normalized_classification = str(classification or "").strip().casefold()
        if not normalized_classification:
            raise ValueError("AI inventory classification is required")
        epoch = self._conn.execute(
            """
            SELECT policy_revision, state, eligibility_bound
            FROM ai_inventory_epochs WHERE epoch_id=?
            """,
            (str(epoch_id),),
        ).fetchone()
        if epoch is None or str(epoch[1]) != "running":
            raise RuntimeError("AI inventory observation requires a running epoch")
        normalized_policy = str(policy_revision or "").strip()
        if normalized_policy != str(epoch[0]):
            raise ValueError("AI inventory observation policy does not match its epoch")
        identity = ai_delivery_identity(
            path,
            media_size=int(media_size),
            media_mtime_ns=int(media_mtime_ns),
            policy_revision=normalized_policy,
        )
        expected_obligation_id = str(identity["obligation_id"])
        requires_ledger = normalized_disposition in {"delivery_required", "delivered"}
        obligation = self._conn.execute(
            """
            SELECT obligation_id FROM ai_delivery_obligations
            WHERE canonical_path=? AND media_fingerprint=? AND media_size=?
              AND media_mtime_ns=? AND policy_revision=?
            """,
            (
                identity["canonical_path"], identity["media_fingerprint"],
                int(media_size), int(media_mtime_ns), normalized_policy,
            ),
        ).fetchone()
        obligation_id = str(obligation[0]) if obligation is not None else expected_obligation_id
        first_seen_row = self._conn.execute(
            """
            SELECT MIN(first_seen_at) FROM ai_media_inventory
            WHERE canonical_path=? AND media_fingerprint=? AND media_size=?
              AND media_mtime_ns=? AND policy_revision=?
            """,
            (
                identity["canonical_path"], identity["media_fingerprint"],
                int(media_size), int(media_mtime_ns), normalized_policy,
            ),
        ).fetchone()
        now = float(time.time() if observed_at is None else observed_at)
        prior_first_seen = float(first_seen_row[0] or 0) if first_seen_row is not None else 0.0
        first_seen_at = prior_first_seen or float(epoch[2])
        inventory_id_raw = json.dumps(
            {"epoch_id": str(epoch_id), "obligation_id": expected_obligation_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        inventory_id = f"aiinvitem_{hashlib.sha256(inventory_id_raw.encode('utf-8')).hexdigest()}"
        self._conn.execute(
            """
            INSERT INTO ai_media_inventory(
                inventory_id, epoch_id, canonical_path, media_fingerprint,
                media_size, media_mtime_ns, policy_revision, classification,
                disposition, requires_ledger, obligation_id, ai_output_detected,
                ai_output_mtime, first_seen_at, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(inventory_id) DO UPDATE SET
                classification=excluded.classification,
                disposition=excluded.disposition,
                requires_ledger=excluded.requires_ledger,
                obligation_id=excluded.obligation_id,
                ai_output_detected=excluded.ai_output_detected,
                ai_output_mtime=excluded.ai_output_mtime,
                observed_at=excluded.observed_at
            """,
            (
                inventory_id, str(epoch_id), identity["canonical_path"],
                identity["media_fingerprint"], int(media_size), int(media_mtime_ns),
                normalized_policy, normalized_classification, normalized_disposition,
                int(requires_ledger), obligation_id, int(bool(ai_output_detected)),
                float(ai_output_mtime or 0), first_seen_at, now,
            ),
        )
        self._conn.execute(
            "UPDATE ai_inventory_epochs SET updated_at=? WHERE epoch_id=? AND state='running'",
            (now, str(epoch_id)),
        )
        return {
            "inventory_id": inventory_id,
            "obligation_id": obligation_id,
            "requires_ledger": requires_ledger,
            "first_seen_at": first_seen_at,
        }

    def fail_ai_inventory_epoch(
        self,
        epoch_id: str,
        *,
        failure_code: str,
        detail: str = "",
        failed_at: float | None = None,
    ) -> bool:
        now = float(time.time() if failed_at is None else failed_at)
        changed = self._conn.execute(
            """
            UPDATE ai_inventory_epochs
            SET state='failed', updated_at=?, walk_error_count=walk_error_count + 1,
                failure_code=?, failure_detail=?, coverage_complete=0
            WHERE epoch_id=? AND state='running'
            """,
            (
                now,
                str(failure_code or "inventory_failure")[:100],
                str(detail or "")[:1000],
                str(epoch_id),
            ),
        ).rowcount
        return int(changed or 0) == 1

    def _recount_ai_inventory_tracked_ledgers(self, epoch_id: str) -> int:
        """Revalidate exact live ledger ownership and strict publication evidence."""

        ledger_rows = self._conn.execute(
            """
            SELECT i.disposition, i.obligation_id,
                   o.obligation_id, o.state, o.policy_revision,
                   o.manifest_path, o.manifest_sha256, o.verification_json,
                   o.verified_at
            FROM ai_media_inventory AS i
            LEFT JOIN ai_delivery_obligations AS o
              ON o.obligation_id=i.obligation_id
             AND o.canonical_path=i.canonical_path
             AND o.media_fingerprint=i.media_fingerprint
             AND o.media_size=i.media_size
             AND o.media_mtime_ns=i.media_mtime_ns
             AND o.policy_revision=i.policy_revision
            WHERE i.epoch_id=? AND i.requires_ledger=1
            """,
            (str(epoch_id),),
        ).fetchall()
        tracked_count = 0
        for row in ledger_rows:
            disposition = str(row[0] or "")
            expected_obligation = str(row[1] or "")
            actual_obligation = str(row[2] or "")
            state = str(row[3] or "")
            policy_revision = str(row[4] or "")
            if not actual_obligation or actual_obligation != expected_obligation:
                continue
            strict_success = False
            if state == "succeeded":
                try:
                    verification = json.loads(str(row[7] or "{}"))
                except (json.JSONDecodeError, TypeError):
                    verification = {}
                strict_success = bool(
                    float(row[8] or 0) > 0
                    and str(row[5] or "").strip()
                    and re.fullmatch(r"[0-9a-f]{64}", str(row[6] or "").strip().casefold())
                    and _verified_ai_delivery_publication_semantics(
                        verification,
                        expected_policy_revision=policy_revision,
                    ) is not None
                )
            if disposition == "delivery_required" and (state == "open" or strict_success):
                tracked_count += 1
            elif disposition == "delivered" and strict_success:
                tracked_count += 1
        return tracked_count

    def finalize_ai_inventory_epoch(
        self,
        epoch_id: str,
        *,
        completed_at: float | None = None,
    ) -> dict[str, Any]:
        """Atomically recount and complete an exhausted, error-free root walk."""

        now = float(time.time() if completed_at is None else completed_at)
        owns_transaction = not self._conn.in_transaction
        savepoint_started = False
        try:
            # Acquire SQLite's WAL writer before any finalization reads.  A
            # deferred read snapshot cannot be upgraded after an AI heartbeat
            # commits, which otherwise strands an already exhausted census in a
            # SQLITE_BUSY_SNAPSHOT retry loop.
            if owns_transaction:
                self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("SAVEPOINT ai_inventory_finalize")
            savepoint_started = True
            epoch = self._conn.execute(
                """
                SELECT state, started_at, walk_error_count, dirty_generation
                FROM ai_inventory_epochs WHERE epoch_id=?
                """,
                (str(epoch_id),),
            ).fetchone()
            if epoch is None or str(epoch[0]) != "running":
                raise RuntimeError("only a running inventory epoch can be finalized")
            if int(epoch[2] or 0) != 0:
                raise RuntimeError("an inventory epoch with walk errors cannot be finalized")
            if now < float(epoch[1]):
                raise ValueError("inventory completed_at cannot precede started_at")
            dirty_rows = dict(
                self._conn.execute(
                    "SELECT key, value FROM ai_delivery_meta "
                    "WHERE key IN ('inventory_dirty_at', 'inventory_dirty_generation')"
                ).fetchall()
            )
            try:
                dirty_at = float(dirty_rows.get("inventory_dirty_at") or 0)
                dirty_generation = int(dirty_rows.get("inventory_dirty_generation") or 0)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("inventory dirty marker is invalid") from exc
            # Generation closes the equal-timestamp ambiguity: a delta after
            # begin always increments even if its wall-clock timestamp equals
            # started_at/completed_at. A marker already present at begin is
            # intentionally covered by the root walk.
            if dirty_generation != int(epoch[3] or 0) or dirty_at > float(epoch[1]):
                raise RuntimeError("media inventory changed during the proof walk")
            # A resumed worker re-walks the complete root and refreshes each
            # surviving observation.  Anything older than the renewed walk
            # boundary was deleted or replaced while the worker was down and
            # must not be counted in the completed proof.
            self._conn.execute(
                "DELETE FROM ai_media_inventory WHERE epoch_id=? AND observed_at<?",
                (str(epoch_id), float(epoch[1])),
            )
            counts = self._conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN classification!='' THEN 1 ELSE 0 END), 0),
                       COUNT(DISTINCT canonical_path),
                       COALESCE(SUM(CASE WHEN requires_ledger=1 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN disposition='legacy_preinstrumented_ai' THEN 1 ELSE 0 END), 0)
                FROM ai_media_inventory WHERE epoch_id=?
                """,
                (str(epoch_id),),
            ).fetchone()
            observed_count = int(counts[0] or 0)
            classified_count = int(counts[1] or 0)
            distinct_paths = int(counts[2] or 0)
            delivery_required_count = int(counts[3] or 0)
            legacy_count = int(counts[4] or 0)
            if classified_count != observed_count or distinct_paths != observed_count:
                raise RuntimeError("inventory epoch contains incomplete or duplicate path observations")

            tracked_count = self._recount_ai_inventory_tracked_ledgers(str(epoch_id))
            untracked_count = delivery_required_count - tracked_count
            coverage_complete = bool(untracked_count == 0)
            changed = self._conn.execute(
                """
                UPDATE ai_inventory_epochs
                SET state='completed', updated_at=?, completed_at=?,
                    observed_count=?, classified_count=?,
                    delivery_required_count=?, tracked_count=?, untracked_count=?,
                    legacy_preinstrumented_ai_count=?, coverage_complete=?,
                    failure_code='', failure_detail=''
                WHERE epoch_id=? AND state='running' AND walk_error_count=0
                """,
                (
                    now, now, observed_count, classified_count,
                    delivery_required_count, tracked_count, untracked_count,
                    legacy_count, int(coverage_complete), str(epoch_id),
                ),
            ).rowcount
            if int(changed or 0) != 1:
                raise RuntimeError("inventory epoch changed before finalization")
            self._conn.execute("RELEASE SAVEPOINT ai_inventory_finalize")
            return {
                "epoch_id": str(epoch_id),
                "state": "completed",
                "completed_at": now,
                "observed_count": observed_count,
                "classified_count": classified_count,
                "delivery_required_count": delivery_required_count,
                "tracked_count": tracked_count,
                "untracked_count": untracked_count,
                "legacy_preinstrumented_ai_count": legacy_count,
                "coverage_complete": coverage_complete,
            }
        except Exception:
            if savepoint_started:
                self._conn.execute("ROLLBACK TO SAVEPOINT ai_inventory_finalize")
                self._conn.execute("RELEASE SAVEPOINT ai_inventory_finalize")
            if owns_transaction and self._conn.in_transaction:
                self._conn.rollback()
            raise

    def ai_delivery_admission_bound(self) -> float:
        """Conservative first-eligibility bound for a newly observed identity."""

        meta = dict(self._conn.execute("SELECT key, value FROM ai_delivery_meta").fetchall())
        try:
            instrumented_at = float(meta.get("instrumented_at") or 0)
        except (TypeError, ValueError):
            instrumented_at = 0.0
        policy_revision = str(meta.get("inventory_current_policy_revision") or "")
        root_signature = str(meta.get("inventory_current_root_signature") or "")
        if instrumented_at <= 0:
            return time.time()
        completed = None
        if policy_revision and root_signature:
            completed = self._conn.execute(
                """
                SELECT completed_at FROM ai_inventory_epochs
                WHERE state='completed' AND measurement_revision=?
                  AND policy_revision=? AND root_signature=? AND completed_at>=?
                ORDER BY completed_at DESC LIMIT 1
                """,
                (
                    AI_DELIVERY_MEASUREMENT_REVISION,
                    policy_revision,
                    root_signature,
                    instrumented_at,
                ),
            ).fetchone()
        return max(
            instrumented_at,
            float(completed[0] or 0) if completed is not None else instrumented_at,
        )

    def ai_inventory_coverage_summary(self, *, now: float | None = None) -> dict[str, Any]:
        """Recount the latest full-root attestation and current active queue."""

        observed_at = float(time.time() if now is None else now)
        result: dict[str, Any] = {
            "available": False,
            "complete": False,
            "state": "unavailable",
            "epoch_id": None,
            "completed_at": 0.0,
            "age_seconds": None,
            "inventory_total": None,
            "inventory_delivery_required": None,
            "inventory_tracked": None,
            "inventory_untracked": None,
            "legacy_preinstrumented_ai": None,
            "active_queue_total": None,
            "active_queue_tracked": None,
            "active_queue_untracked": None,
            "active_queue_complete": None,
        }
        meta = dict(self._conn.execute("SELECT key, value FROM ai_delivery_meta").fetchall())
        measurement_revision = str(meta.get("measurement_revision") or "")
        policy_revision = str(meta.get("inventory_current_policy_revision") or "")
        root_signature = str(meta.get("inventory_current_root_signature") or "")
        try:
            instrumented_at = float(meta.get("instrumented_at") or 0)
        except (TypeError, ValueError):
            return result
        if (
            measurement_revision != AI_DELIVERY_MEASUREMENT_REVISION
            or not policy_revision
            or not root_signature
            or instrumented_at <= 0
        ):
            return result
        result["available"] = True
        active = self._conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(CASE WHEN EXISTS (
                SELECT 1 FROM ai_delivery_obligations AS o
                WHERE o.canonical_path=q.path AND o.media_mtime_ns=q.mtime_ns
                  AND o.policy_revision=? AND o.state='open'
            ) THEN 1 ELSE 0 END), 0)
            FROM ai_candidate_queue AS q
            WHERE q.status IN ('queued', 'running', 'failed_retry', 'paused')
            """,
            (policy_revision,),
        ).fetchone()
        active_total = int(active[0] or 0)
        active_tracked = int(active[1] or 0)
        result.update(
            {
                "active_queue_total": active_total,
                "active_queue_tracked": active_tracked,
                "active_queue_untracked": active_total - active_tracked,
                "active_queue_complete": active_total == active_tracked,
            }
        )
        completed = self._conn.execute(
            """
            SELECT epoch_id, completed_at, observed_count, classified_count,
                   delivery_required_count, tracked_count, untracked_count,
                   legacy_preinstrumented_ai_count, coverage_complete,
                   dirty_generation
            FROM ai_inventory_epochs
            WHERE state='completed' AND measurement_revision=?
              AND policy_revision=? AND root_signature=? AND completed_at>=?
            ORDER BY completed_at DESC LIMIT 1
            """,
            (
                measurement_revision,
                policy_revision,
                root_signature,
                instrumented_at,
            ),
        ).fetchone()
        if completed is None:
            result["state"] = "inventory_missing"
            return result
        epoch_id = str(completed[0])
        completed_at = float(completed[1] or 0)
        age = observed_at - completed_at
        result.update(
            {"epoch_id": epoch_id, "completed_at": completed_at, "age_seconds": age}
        )
        try:
            dirty_at = float(meta.get("inventory_dirty_at") or 0)
            dirty_generation = int(meta.get("inventory_dirty_generation") or 0)
        except (TypeError, ValueError):
            result["state"] = "inventory_dirty"
            return result
        if dirty_generation != int(completed[9] or 0) or dirty_at > completed_at:
            result["state"] = "inventory_dirty"
            return result
        newer_bad = self._conn.execute(
            """
            SELECT state, updated_at FROM ai_inventory_epochs
            WHERE measurement_revision=? AND policy_revision=? AND root_signature=?
              AND started_at>? AND state IN ('failed', 'abandoned')
            ORDER BY started_at DESC LIMIT 1
            """,
            (measurement_revision, policy_revision, root_signature, completed_at),
        ).fetchone()
        stale_running = self._conn.execute(
            """
            SELECT 1 FROM ai_inventory_epochs
            WHERE measurement_revision=? AND policy_revision=? AND root_signature=?
              AND state='running' AND ?-updated_at>?
            LIMIT 1
            """,
            (
                measurement_revision,
                policy_revision,
                root_signature,
                observed_at,
                AI_INVENTORY_RUNNING_STALE_SECONDS,
            ),
        ).fetchone()
        if newer_bad is not None:
            result["state"] = f"inventory_{str(newer_bad[0])}"
            return result
        if stale_running is not None:
            result["state"] = "inventory_running_stale"
            return result
        if age < 0 or age > AI_INVENTORY_MAX_AGE_SECONDS:
            result["state"] = "inventory_stale"
            return result

        inventory_counts = self._conn.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN classification!='' THEN 1 ELSE 0 END), 0),
                   COUNT(DISTINCT canonical_path),
                   COALESCE(SUM(CASE WHEN requires_ledger=1 THEN 1 ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN disposition='legacy_preinstrumented_ai' THEN 1 ELSE 0 END), 0)
            FROM ai_media_inventory WHERE epoch_id=?
            """,
            (epoch_id,),
        ).fetchone()
        inventory_total = int(inventory_counts[0] or 0)
        classified_count = int(inventory_counts[1] or 0)
        distinct_paths = int(inventory_counts[2] or 0)
        required_count = int(inventory_counts[3] or 0)
        legacy_count = int(inventory_counts[4] or 0)
        tracked_count = self._recount_ai_inventory_tracked_ledgers(epoch_id)
        untracked_count = required_count - tracked_count
        stored_consistent = bool(
            inventory_total == int(completed[2] or 0)
            and classified_count == int(completed[3] or 0)
            and distinct_paths == inventory_total
            and required_count == int(completed[4] or 0)
            and tracked_count == int(completed[5] or 0)
            and untracked_count == int(completed[6] or 0)
            and legacy_count == int(completed[7] or 0)
            and bool(completed[8]) == (untracked_count == 0)
        )
        inventory_complete = bool(stored_consistent and untracked_count == 0)
        active_complete = bool(result["active_queue_complete"])
        result.update(
            {
                "inventory_total": inventory_total,
                "inventory_delivery_required": required_count,
                "inventory_tracked": tracked_count,
                "inventory_untracked": untracked_count,
                "legacy_preinstrumented_ai": legacy_count,
                "complete": bool(inventory_complete and active_complete),
                "state": "complete" if inventory_complete and active_complete else "coverage_incomplete",
            }
        )
        return result

    def ai_inventory_continuous_coverage_summary(
        self,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return the current unbroken strict full-inventory coverage segment."""

        evaluated_at = float(time.time() if now is None else now)
        current = self.ai_inventory_coverage_summary(now=evaluated_at)
        result = {
            **current,
            "continuous_coverage_since": None,
            "coverage_complete_through": None,
            "coverage_chain_epoch_count": 0,
            "coverage_max_gap_seconds": AI_INVENTORY_MAX_AGE_SECONDS,
        }
        if not current.get("complete"):
            return result
        meta = dict(self._conn.execute("SELECT key, value FROM ai_delivery_meta").fetchall())
        revision = str(meta.get("measurement_revision") or "")
        policy = str(meta.get("inventory_current_policy_revision") or "")
        root = str(meta.get("inventory_current_root_signature") or "")
        try:
            instrumented_at = float(meta.get("instrumented_at") or 0)
        except (TypeError, ValueError):
            return {**result, "complete": False, "state": "unavailable"}
        epochs = self._conn.execute(
            """
            SELECT epoch_id, state, started_at, updated_at, completed_at,
                   walk_error_count, observed_count, classified_count,
                   delivery_required_count, tracked_count, untracked_count,
                   legacy_preinstrumented_ai_count, coverage_complete
            FROM ai_inventory_epochs
            WHERE measurement_revision=? AND policy_revision=? AND root_signature=?
              AND started_at>=?
            ORDER BY started_at DESC, epoch_id DESC
            """,
            (revision, policy, root, instrumented_at),
        ).fetchall()
        chain_since: float | None = None
        latest_chain_completed: float | None = None
        newer_completed: float | None = None
        chain_count = 0
        for epoch in epochs:
            state = str(epoch[1] or "")
            if state in {"failed", "abandoned"}:
                break
            if state != "completed":
                continue
            started_at = float(epoch[2] or 0)
            completed_at = float(epoch[4] or 0)
            counts = self._conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN classification!='' THEN 1 ELSE 0 END), 0),
                       COUNT(DISTINCT canonical_path),
                       COALESCE(SUM(CASE WHEN requires_ledger=1 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN disposition='legacy_preinstrumented_ai' THEN 1 ELSE 0 END), 0)
                FROM ai_media_inventory WHERE epoch_id=?
                """,
                (str(epoch[0]),),
            ).fetchone()
            actual_total = int(counts[0] or 0)
            actual_classified = int(counts[1] or 0)
            actual_distinct = int(counts[2] or 0)
            actual_required = int(counts[3] or 0)
            actual_legacy = int(counts[4] or 0)
            actual_tracked = self._recount_ai_inventory_tracked_ledgers(str(epoch[0]))
            actual_untracked = actual_required - actual_tracked
            valid = bool(
                started_at > 0
                and completed_at >= started_at
                and int(epoch[5] or 0) == 0
                and actual_total == int(epoch[6] or 0)
                and actual_classified == int(epoch[7] or 0)
                and actual_distinct == actual_total
                and actual_required == int(epoch[8] or 0)
                and actual_tracked == int(epoch[9] or 0)
                and actual_untracked == int(epoch[10] or 0) == 0
                and actual_legacy == int(epoch[11] or 0)
                and bool(epoch[12])
            )
            if not valid:
                break
            if newer_completed is not None:
                if newer_completed - completed_at > AI_INVENTORY_MAX_AGE_SECONDS:
                    break
            else:
                latest_chain_completed = completed_at
            chain_since = completed_at
            newer_completed = completed_at
            chain_count += 1
        latest_completed = float(current.get("completed_at") or 0)
        if (
            chain_since is None
            or latest_chain_completed is None
            or abs(latest_chain_completed - latest_completed) > AI_DELIVERY_DUE_TOLERANCE_SECONDS
        ):
            return {**result, "complete": False, "state": "coverage_incomplete"}
        return {
            **result,
            "continuous_coverage_since": chain_since,
            "coverage_complete_through": latest_chain_completed,
            "coverage_chain_epoch_count": chain_count,
        }

    def ai_delivery_slo_summary(
        self,
        *,
        now: float | None = None,
        window_seconds: int = AI_DELIVERY_SLO_WINDOW_SECONDS,
        target: float = AI_DELIVERY_SLO_TARGET,
        minimum_sample: int = AI_DELIVERY_SLO_MINIMUM_SAMPLE,
    ) -> dict[str, Any]:
        """Return rolling delivery health plus cumulative anytime-valid evidence."""

        evaluated_at = float(time.time() if now is None else now)
        window = max(1, int(window_seconds))
        normalized_target = float(target)
        if not 0 < normalized_target < 1:
            raise ValueError("target must be within (0, 1)")
        meta = dict(self._conn.execute("SELECT key, value FROM ai_delivery_meta").fetchall())
        measurement_revision = str(meta.get("measurement_revision") or "").strip()
        try:
            instrumented_at = float(meta.get("instrumented_at") or 0)
        except (TypeError, ValueError):
            instrumented_at = 0.0
        coverage = self.ai_inventory_continuous_coverage_summary(now=evaluated_at)
        continuous_since = float(coverage.get("continuous_coverage_since") or 0)
        coverage_through = float(coverage.get("coverage_complete_through") or 0)
        policy_revision = str(meta.get("inventory_current_policy_revision") or "")
        rows = self._conn.execute(
            f"SELECT {', '.join(_AI_DELIVERY_OBLIGATION_FIELDS)} "
            "FROM ai_delivery_obligations WHERE policy_revision=?",
            (policy_revision,),
        ).fetchall()
        obligations = [_ai_delivery_obligation_dict(row) for row in rows]
        invalid_eligible = any(
            not isinstance(item.get("eligible_at"), (int, float))
            or not math.isfinite(item["eligible_at"])
            or item["eligible_at"] <= 0
            for item in obligations
        )
        as_of = coverage_through if coverage.get("complete") else 0.0
        rolling_due_from = as_of - window if as_of else 0.0
        rolling = _summarize_matured_ai_delivery_obligations(
            obligations,
            expected_due_from=max(
                rolling_due_from,
                continuous_since + AI_DELIVERY_DEADLINE_SECONDS,
            ),
            expected_due_to=as_of,
            policy_revision=policy_revision,
        ) if as_of else _summarize_matured_ai_delivery_obligations(
            [], expected_due_from=0, expected_due_to=0, policy_revision=policy_revision
        )
        cumulative = _summarize_matured_ai_delivery_obligations(
            obligations,
            expected_due_from=continuous_since + AI_DELIVERY_DEADLINE_SECONDS,
            expected_due_to=as_of,
            policy_revision=policy_revision,
        ) if as_of and continuous_since else _summarize_matured_ai_delivery_obligations(
            [], expected_due_from=0, expected_due_to=0, policy_revision=policy_revision
        )
        full_window = bool(
            coverage.get("complete")
            and rolling_due_from >= continuous_since + AI_DELIVERY_DEADLINE_SECONDS
        )
        coverage_ok = bool(coverage.get("complete") and not invalid_eligible)
        rolling_point = (
            bool(rolling["success_rate"] is not None and rolling["success_rate"] >= normalized_target)
            if coverage_ok and full_window and rolling["denominator"]
            else None
        )
        rolling_state = (
            "coverage_incomplete" if not coverage_ok else
            "warming" if not full_window else
            "no_matured_obligations" if not rolling["denominator"] else
            "meeting" if rolling_point else "breached"
        )
        cumulative_n = int(cumulative["denominator"])
        cumulative_s = int(cumulative["verified_on_time"])
        cumulative_m = int(cumulative["misses"])
        try:
            log_e = ai_delivery_anytime_log_e(normalized_target, cumulative_s, cumulative_m) if cumulative_n else None
            lower_bound = ai_delivery_anytime_lower_bound(cumulative_s, cumulative_m)
        except (ArithmeticError, OverflowError, ValueError):
            log_e = None
            lower_bound = None
            coverage_ok = False
        cumulative_point = (
            bool(cumulative["success_rate"] is not None and cumulative["success_rate"] >= normalized_target)
            if coverage_ok and cumulative_n else None
        )
        evidence_met = (
            bool(cumulative_point and log_e is not None and log_e >= AI_DELIVERY_ANYTIME_LOG_THRESHOLD)
            if coverage_ok and cumulative_n else None
        )
        evidence_state = (
            "coverage_incomplete" if not coverage_ok else
            "warming" if not cumulative_n else
            "supported" if evidence_met else
            "below_target" if cumulative_point is False else "collecting"
        )
        overall_met = bool(rolling_point and evidence_met) if rolling_point is not None and evidence_met is not None else None
        evaluation_state = (
            "coverage_incomplete" if not coverage_ok else
            "warming" if rolling_point is None or evidence_met is None else
            "meeting" if overall_met else "not_verified"
        )
        try:
            e_value = math.exp(log_e) if log_e is not None and log_e <= math.log(float.fromhex("0x1.fffffffffffffp+1023")) else None
        except OverflowError:
            e_value = None
        rolling_operational = {
            **rolling,
            "mode": "rolling_observed_media_census",
            "as_of": as_of or None,
            "window_seconds": window,
            "window_days": window / 86400.0,
            "expected_due_from": rolling_due_from or None,
            "expected_due_to": as_of or None,
            "point_target_met": rolling_point,
            "state": rolling_state,
            "confidence_method": "clopper_pearson_exact_one_sided",
            "fixed_sample_descriptive_only": True,
            "proof_eligible": False,
        }
        cumulative_evidence = {
            **cumulative,
            "mode": "fixed_measurement_revision_cumulative_media_cohort",
            "scope": "strict_on_time_delivery_not_semantic_accuracy",
            "cohort_started_at": continuous_since or None,
            "coverage_complete_through": as_of or None,
            "confidence_level": 1.0 - AI_DELIVERY_ANYTIME_ALPHA,
            "confidence_method": AI_DELIVERY_ANYTIME_METHOD,
            "anytime_valid": True,
            "betting_fractions": list(AI_DELIVERY_ANYTIME_BETTING_FRACTIONS),
            "e_value_threshold": 1.0 / AI_DELIVERY_ANYTIME_ALPHA,
            "log_e_value": log_e,
            "e_value": e_value,
            "e_value_overflow": bool(log_e is not None and e_value is None),
            "lower_confidence_bound": lower_bound,
            "point_target_met": cumulative_point,
            "target_evidence_met": evidence_met,
            "state": evidence_state,
        }
        return {
            "schema_version": AI_DELIVERY_SCHEMA_VERSION,
            "measurement_revision": measurement_revision or None,
            "evaluated_at": evaluated_at,
            "window_seconds": window,
            "window_days": window / 86400.0,
            "due_from": rolling_due_from,
            "due_to": as_of,
            "delivery_deadline_seconds": AI_DELIVERY_DEADLINE_SECONDS,
            "target": normalized_target,
            "state": evaluation_state,
            "target_met": overall_met,
            "instrumented_at": instrumented_at,
            "coverage_started_at": continuous_since,
            "full_window": full_window,
            "inventory_coverage": coverage,
            "minimum_sample": max(1, int(minimum_sample)),
            "denominator": rolling["denominator"],
            "verified_on_time": rolling["verified_on_time"],
            "misses": rolling["misses"],
            "success_rate": rolling["success_rate"],
            "publication_breakdown": rolling["publication_breakdown"],
            "late_successes": rolling["late_successes"],
            "overdue_open": rolling["overdue_open"],
            "exclusions": rolling["exclusions"],
            "invalid_exclusions": rolling["invalid_exclusions"],
            "invalid_contract_misses": rolling["invalid_contract_misses"],
            "error_budget_allowed": rolling["denominator"] * (1.0 - normalized_target) if rolling["denominator"] else None,
            "error_budget_used": rolling["misses"] if rolling["denominator"] else None,
            "error_budget_remaining": (
                rolling["denominator"] * (1.0 - normalized_target) - rolling["misses"]
                if rolling["denominator"] else None
            ),
            "rolling_operational": rolling_operational,
            "cumulative_evidence": cumulative_evidence,
        }

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_scan_cache (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sidecar_signature TEXT NOT NULL,
                config_signature TEXT NOT NULL,
                episode INTEGER,
                status TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._ensure_video_scan_cache_columns()
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_video_scan_cache_updated_at
            ON video_scan_cache(updated_at)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_video_scan_cache_episode_path
            ON video_scan_cache(episode, path)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_candidate_queue (
                path TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                filename_season INTEGER,
                filename_episode INTEGER,
                status TEXT NOT NULL DEFAULT 'queued',
                source TEXT NOT NULL DEFAULT 'scan',
                attempts INTEGER NOT NULL DEFAULT 0,
                running_at REAL,
                last_error TEXT,
                last_error_at REAL,
                last_error_code TEXT NOT NULL DEFAULT '',
                retry_strategy TEXT NOT NULL DEFAULT '',
                failure_revision TEXT NOT NULL DEFAULT '',
                next_retry_at REAL,
                force_ai INTEGER NOT NULL DEFAULT 0,
                added_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._ensure_ai_queue_columns()
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_candidate_queue_mtime_ns
            ON ai_candidate_queue(mtime_ns DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_candidate_queue_status_retry
            ON ai_candidate_queue(status, next_retry_at, mtime_ns DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_candidate_queue_status_retry_added
            ON ai_candidate_queue(status, next_retry_at, force_ai DESC, added_at DESC, mtime_ns DESC)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_job_state (
                path TEXT PRIMARY KEY,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                started_at REAL,
                updated_at REAL NOT NULL,
                finished_at REAL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_job_state_updated_at
            ON ai_job_state(updated_at DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_job_state_finished_at
            ON ai_job_state(finished_at DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_job_state_status_stage
            ON ai_job_state(status, stage)
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_stage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_stage_events_created_at
            ON ai_stage_events(created_at DESC)
            """
        )
        self._prune_stage_events(force=True)
        # Build the completion lookup only after pruning legacy databases. This
        # keeps startup migration fast even when an older DB accumulated
        # hundreds of thousands of progress events.
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_stage_events_completion
            ON ai_stage_events(stage, status, path, created_at DESC)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ai_stage_events_path_status_created
            ON ai_stage_events(path, status, created_at DESC)
            """
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_delivery_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_delivery_obligations (
                obligation_id TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                media_fingerprint TEXT NOT NULL,
                media_size INTEGER NOT NULL,
                media_mtime_ns INTEGER NOT NULL,
                policy_revision TEXT NOT NULL,
                acceptance_run_id TEXT,
                source TEXT NOT NULL DEFAULT 'scan',
                state TEXT NOT NULL DEFAULT 'open',
                eligible_at REAL NOT NULL,
                due_at REAL NOT NULL,
                verified_at REAL NOT NULL DEFAULT 0,
                terminal_at REAL NOT NULL DEFAULT 0,
                outcome_code TEXT NOT NULL DEFAULT '',
                exclusion_code TEXT NOT NULL DEFAULT '',
                exclusion_detail TEXT NOT NULL DEFAULT '',
                manifest_path TEXT NOT NULL DEFAULT '',
                manifest_sha256 TEXT NOT NULL DEFAULT '',
                verification_json TEXT NOT NULL DEFAULT '{}',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(canonical_path, media_fingerprint, media_size, media_mtime_ns, policy_revision)
            );
            CREATE INDEX IF NOT EXISTS idx_ai_delivery_obligations_due
                ON ai_delivery_obligations(due_at, state);
            CREATE INDEX IF NOT EXISTS idx_ai_delivery_obligations_path_updated
                ON ai_delivery_obligations(canonical_path, updated_at DESC);
            CREATE TABLE IF NOT EXISTS ai_delivery_attempts (
                attempt_id TEXT PRIMARY KEY,
                obligation_id TEXT NOT NULL,
                acceptance_run_id TEXT,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                finished_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(obligation_id, attempt_number)
            );
            CREATE INDEX IF NOT EXISTS idx_ai_delivery_attempts_obligation
                ON ai_delivery_attempts(obligation_id, attempt_number);
            CREATE INDEX IF NOT EXISTS idx_ai_delivery_attempts_status_updated
                ON ai_delivery_attempts(status, updated_at);
            CREATE TABLE IF NOT EXISTS ai_inventory_epochs (
                epoch_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                measurement_revision TEXT NOT NULL,
                policy_revision TEXT NOT NULL,
                root_signature TEXT NOT NULL,
                state TEXT NOT NULL,
                started_at REAL NOT NULL,
                eligibility_bound REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL NOT NULL DEFAULT 0,
                walk_error_count INTEGER NOT NULL DEFAULT 0,
                observed_count INTEGER NOT NULL DEFAULT 0,
                classified_count INTEGER NOT NULL DEFAULT 0,
                delivery_required_count INTEGER NOT NULL DEFAULT 0,
                tracked_count INTEGER NOT NULL DEFAULT 0,
                untracked_count INTEGER NOT NULL DEFAULT 0,
                legacy_preinstrumented_ai_count INTEGER NOT NULL DEFAULT 0,
                coverage_complete INTEGER NOT NULL DEFAULT 0,
                dirty_generation INTEGER NOT NULL DEFAULT 0,
                failure_code TEXT NOT NULL DEFAULT '',
                failure_detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ai_inventory_epochs_contract_completed
                ON ai_inventory_epochs(
                    measurement_revision, policy_revision, root_signature,
                    state, completed_at DESC
                );
            CREATE INDEX IF NOT EXISTS idx_ai_inventory_epochs_started
                ON ai_inventory_epochs(started_at DESC);
            CREATE TABLE IF NOT EXISTS ai_media_inventory (
                inventory_id TEXT PRIMARY KEY,
                epoch_id TEXT NOT NULL,
                canonical_path TEXT NOT NULL,
                media_fingerprint TEXT NOT NULL,
                media_size INTEGER NOT NULL,
                media_mtime_ns INTEGER NOT NULL,
                policy_revision TEXT NOT NULL,
                classification TEXT NOT NULL,
                disposition TEXT NOT NULL,
                requires_ledger INTEGER NOT NULL DEFAULT 0,
                obligation_id TEXT NOT NULL DEFAULT '',
                ai_output_detected INTEGER NOT NULL DEFAULT 0,
                ai_output_mtime REAL NOT NULL DEFAULT 0,
                first_seen_at REAL NOT NULL,
                observed_at REAL NOT NULL,
                UNIQUE(
                    epoch_id, canonical_path, media_fingerprint,
                    media_size, media_mtime_ns, policy_revision
                )
            );
            CREATE INDEX IF NOT EXISTS idx_ai_media_inventory_epoch
                ON ai_media_inventory(epoch_id, disposition, requires_ledger);
            CREATE INDEX IF NOT EXISTS idx_ai_media_inventory_epoch_path
                ON ai_media_inventory(epoch_id, canonical_path);
            """
        )
        self._ensure_ai_delivery_acceptance_columns()
        self._ensure_ai_inventory_epoch_columns()
        now = time.time()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ai_delivery_meta(key, value, updated_at)
            VALUES('schema_version', ?, ?)
            """,
            (str(AI_DELIVERY_SCHEMA_VERSION), now),
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO ai_delivery_meta(key, value, updated_at)
            VALUES('inventory_dirty_generation', '0', ?)
            """,
            (now,),
        )
        revision_row = self._conn.execute(
            "SELECT value FROM ai_delivery_meta WHERE key='measurement_revision'"
        ).fetchone()
        stored_revision = str(revision_row[0] if revision_row is not None else "").strip()
        if stored_revision != AI_DELIVERY_MEASUREMENT_REVISION:
            # Changing the proof contract starts a new measurement epoch. Both
            # meta writes share this transaction; historical ledger rows remain
            # immutable and available as evidence, but cannot mature the new epoch.
            self._conn.execute(
                """
                INSERT INTO ai_delivery_meta(key, value, updated_at)
                VALUES('measurement_revision', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (AI_DELIVERY_MEASUREMENT_REVISION, now),
            )
            self._conn.execute(
                """
                INSERT INTO ai_delivery_meta(key, value, updated_at)
                VALUES('instrumented_at', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(now), now),
            )
        else:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO ai_delivery_meta(key, value, updated_at)
                VALUES('instrumented_at', ?, ?)
                """,
                (str(now), now),
            )
        self._conn.commit()

    def _prune_stage_events(self, *, force: bool = False) -> int:
        if not force and self._stage_events_since_prune < AI_STAGE_EVENT_PRUNE_INTERVAL:
            return 0
        self._stage_events_since_prune = 0
        removed = self._conn.execute(
            "DELETE FROM ai_stage_events WHERE created_at < ?",
            (time.time() - self._stage_event_retention_days * 86400,),
        ).rowcount
        cutoff = self._conn.execute(
            """
            SELECT id
            FROM ai_stage_events
            ORDER BY id DESC
            LIMIT 1 OFFSET ?
            """,
            (self._stage_event_max_rows,),
        ).fetchone()
        if cutoff is None:
            return max(0, int(removed or 0))
        cursor = self._conn.execute(
            "DELETE FROM ai_stage_events WHERE id <= ?",
            (int(cutoff[0]),),
        )
        return max(0, int(removed or 0)) + max(0, int(cursor.rowcount or 0))

    def _ensure_ai_queue_columns(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(ai_candidate_queue)").fetchall()
        existing = {str(row[1]) for row in rows}
        column_sql = {
            "filename_season": "ALTER TABLE ai_candidate_queue ADD COLUMN filename_season INTEGER",
            "filename_episode": "ALTER TABLE ai_candidate_queue ADD COLUMN filename_episode INTEGER",
            "status": "ALTER TABLE ai_candidate_queue ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'",
            "source": "ALTER TABLE ai_candidate_queue ADD COLUMN source TEXT NOT NULL DEFAULT 'scan'",
            "attempts": "ALTER TABLE ai_candidate_queue ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "running_at": "ALTER TABLE ai_candidate_queue ADD COLUMN running_at REAL",
            "last_error": "ALTER TABLE ai_candidate_queue ADD COLUMN last_error TEXT",
            "last_error_at": "ALTER TABLE ai_candidate_queue ADD COLUMN last_error_at REAL",
            "last_error_code": "ALTER TABLE ai_candidate_queue ADD COLUMN last_error_code TEXT NOT NULL DEFAULT ''",
            "retry_strategy": "ALTER TABLE ai_candidate_queue ADD COLUMN retry_strategy TEXT NOT NULL DEFAULT ''",
            "failure_revision": "ALTER TABLE ai_candidate_queue ADD COLUMN failure_revision TEXT NOT NULL DEFAULT ''",
            "next_retry_at": "ALTER TABLE ai_candidate_queue ADD COLUMN next_retry_at REAL",
            "force_ai": "ALTER TABLE ai_candidate_queue ADD COLUMN force_ai INTEGER NOT NULL DEFAULT 0",
            "added_at": "ALTER TABLE ai_candidate_queue ADD COLUMN added_at REAL NOT NULL DEFAULT 0",
            "updated_at": "ALTER TABLE ai_candidate_queue ADD COLUMN updated_at REAL NOT NULL DEFAULT 0",
        }
        sequence_columns_added = False
        for column, statement in column_sql.items():
            if column not in existing:
                self._conn.execute(statement)
                if column in {"filename_season", "filename_episode"}:
                    sequence_columns_added = True
        if sequence_columns_added:
            sequence_rows = self._conn.execute(
                "SELECT path FROM ai_candidate_queue"
            ).fetchall()
            updates = []
            for row in sequence_rows:
                queue_path = str(row[0] or "")
                filename_season, filename_episode = _filename_sequence(queue_path)
                updates.append((filename_season, filename_episode, queue_path))
            if updates:
                self._conn.executemany(
                    "UPDATE ai_candidate_queue "
                    "SET filename_season=?, filename_episode=? WHERE path=?",
                    updates,
                )

    def _ensure_ai_inventory_epoch_columns(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(ai_inventory_epochs)").fetchall()
        existing = {str(row[1]) for row in rows}
        if "dirty_generation" not in existing:
            self._conn.execute(
                "ALTER TABLE ai_inventory_epochs "
                "ADD COLUMN dirty_generation INTEGER NOT NULL DEFAULT 0"
            )

    def _ensure_ai_delivery_acceptance_columns(self) -> None:
        for table in ("ai_delivery_obligations", "ai_delivery_attempts"):
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            existing = {str(row[1]) for row in rows}
            if "acceptance_run_id" not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN acceptance_run_id TEXT"
                )

    def _ensure_video_scan_cache_columns(self) -> None:
        rows = self._conn.execute("PRAGMA table_info(video_scan_cache)").fetchall()
        existing = {str(row[1]) for row in rows}
        if "episode" not in existing:
            self._conn.execute("ALTER TABLE video_scan_cache ADD COLUMN episode INTEGER")


def scan_state_path(config: Any) -> Path:
    state_path = Path(getattr(config, "scanner_state_path", "scanner_state.sqlite3"))
    if not state_path.is_absolute():
        state_path = Path(config.work_path) / state_path
    return state_path


def is_scan_state_corruption_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return isinstance(error, sqlite3.DatabaseError) and any(marker in message for marker in SQLITE_CORRUPTION_MARKERS)


def is_scan_state_transient_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return isinstance(error, sqlite3.DatabaseError) and any(marker in message for marker in SQLITE_TRANSIENT_MARKERS)


def quarantine_corrupt_scan_state(path: Path) -> list[Path]:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    moved: list[Path] = []
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{path}{suffix}")
        if not source.exists():
            continue
        destination = path.with_name(f"{path.name}{suffix}.corrupt-{timestamp}")
        counter = 1
        while destination.exists():
            destination = path.with_name(f"{path.name}{suffix}.corrupt-{timestamp}-{counter}")
            counter += 1
        os.replace(source, destination)
        moved.append(destination)
    return moved


def scan_config_signature(config: Any) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "export_ai_ass": bool(config.export_ai_ass),
        "ai_japanese_ass_suffix": config.ai_japanese_ass_suffix,
        "ai_simplified_chinese_ass_suffix": config.ai_simplified_chinese_ass_suffix,
        "ai_traditional_chinese_ass_suffix": config.ai_traditional_chinese_ass_suffix,
        "finished_subtitle_suffixes": list(config.finished_subtitle_suffixes),
        "mikan_remove_ai_after_extract": bool(getattr(config, "mikan_remove_ai_after_extract", True)),
        "require_ai_subtitles": bool(getattr(config, "require_ai_subtitles", False)),
        "completed_delivery_enabled": bool(
            getattr(config, "completed_delivery_enabled", False)
        ),
        "completed_delivery_path": str(getattr(config, "completed_delivery_path", "") or ""),
        "completed_delivery_source_policy": str(
            getattr(config, "completed_delivery_source_policy", "retain") or "retain"
        ),
        "scanner_skip_standalone_op_ed": bool(getattr(config, "scanner_skip_standalone_op_ed", True)),
        "language_gate_enabled": bool(getattr(config, "language_gate_enabled", False)),
        "allowed_source_languages": list(getattr(config, "allowed_source_languages", ["ja"]) or []),
        "skip_non_allowed_language": bool(getattr(config, "skip_non_allowed_language", True)),
        "transcribe_non_allowed_languages": bool(
            getattr(config, "transcribe_non_allowed_languages", False)
        ),
    }
    if bool(getattr(config, "export_ai_ass", False)) and bool(getattr(config, "ass_style_versioning_enabled", False)):
        try:
            from ass_utils import ass_style_from_config, ass_style_signature

            payload["ass_style_signature"] = ass_style_signature(ass_style_from_config(config))
        except AttributeError:
            pass
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def ai_inventory_root_signature(input_path: str | Path, video_extensions: Any) -> str:
    """Stable identity for the exact media-root inventory contract."""

    root = str(Path(input_path).resolve()).replace("\\", "/")
    extensions = sorted(
        {
            str(extension or "").strip().casefold()
            for extension in (video_extensions or [])
            if str(extension or "").strip()
        }
    )
    payload = {
        "schema_version": AI_INVENTORY_SCHEMA_VERSION,
        "root": root,
        "video_extensions": extensions,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def video_scan_signature(video: Path, config: Any, config_signature: str) -> VideoScanSignature:
    stat = video.stat()
    return VideoScanSignature(
        path=video.resolve(),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sidecar_signature=_sidecar_signature(video, config),
        config_signature=config_signature,
    )


def _sidecar_signature(video: Path, config: Any) -> str:
    paths: dict[str, tuple[int, int]] = {}
    for subtitle in video.parent.glob(f"{video.stem}.*"):
        if not subtitle.is_file() or subtitle.suffix.lower() not in SIDECAR_SUBTITLE_EXTENSIONS:
            continue
        try:
            stat = subtitle.stat()
        except OSError:
            continue
        paths[str(subtitle.resolve()).casefold()] = (stat.st_size, stat.st_mtime_ns)

    for subtitle in finished_subtitle_paths(video, config):
        if not subtitle.exists() or not subtitle.is_file():
            continue
        try:
            stat = subtitle.stat()
        except OSError:
            continue
        paths[str(subtitle.resolve()).casefold()] = (stat.st_size, stat.st_mtime_ns)

    encoded = json.dumps(paths, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def _queue_path(path: Path) -> str:
    return str(path.resolve())


def _filename_sequence(path: str | Path) -> tuple[int | None, int | None]:
    """Return conservative season/episode evidence from the filename only."""

    filename = Path(str(path or "")).name
    return release_season_number(filename), extract_episode_number(filename)


_AI_DELIVERY_OBLIGATION_FIELDS = (
    "obligation_id",
    "canonical_path",
    "media_fingerprint",
    "media_size",
    "media_mtime_ns",
    "policy_revision",
    "acceptance_run_id",
    "source",
    "state",
    "eligible_at",
    "due_at",
    "verified_at",
    "terminal_at",
    "outcome_code",
    "exclusion_code",
    "exclusion_detail",
    "manifest_path",
    "manifest_sha256",
    "verification_json",
    "attempt_count",
    "created_at",
    "updated_at",
)

_AI_DELIVERY_ATTEMPT_FIELDS = (
    "attempt_id",
    "obligation_id",
    "acceptance_run_id",
    "attempt_number",
    "status",
    "stage",
    "error_code",
    "detail",
    "started_at",
    "finished_at",
    "created_at",
    "updated_at",
)


def ai_delivery_identity(
    path: str | Path,
    *,
    media_size: int,
    media_mtime_ns: int,
    policy_revision: str,
) -> dict[str, Any]:
    """Return the canonical, cross-component delivery identity contract."""

    canonical_path = str(Path(path).resolve())
    normalized_size = int(media_size)
    normalized_mtime = int(media_mtime_ns)
    normalized_policy = str(policy_revision or "").strip()
    if normalized_size < 0 or normalized_mtime < 0:
        raise ValueError("media size and mtime_ns must be non-negative")
    if not normalized_policy:
        raise ValueError("policy_revision is required")
    media_payload = {
        "canonical_path": canonical_path,
        "media_mtime_ns": normalized_mtime,
        "media_size": normalized_size,
    }
    media_raw = json.dumps(
        media_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    media_fingerprint = hashlib.sha256(media_raw.encode("utf-8", errors="replace")).hexdigest()
    obligation_payload = {
        **media_payload,
        "policy_revision": normalized_policy,
    }
    obligation_raw = json.dumps(
        obligation_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    obligation_digest = hashlib.sha256(
        obligation_raw.encode("utf-8", errors="replace")
    ).hexdigest()
    return {
        **obligation_payload,
        "media_fingerprint": media_fingerprint,
        "obligation_id": f"aiobl_{obligation_digest}",
    }


def _stable_ai_delivery_attempt_id(obligation_id: str, attempt_number: int) -> str:
    raw = json.dumps(
        {"attempt_number": int(attempt_number), "obligation_id": str(obligation_id)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"aiatt_{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _ai_delivery_obligation_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    payload = dict(zip(_AI_DELIVERY_OBLIGATION_FIELDS, row, strict=True))
    payload["acceptance_run_id"] = str(payload.get("acceptance_run_id") or "")
    for key in ("media_size", "media_mtime_ns", "attempt_count"):
        try:
            payload[key] = int(payload[key] if payload[key] is not None else 0)
        except (TypeError, ValueError, OverflowError):
            payload[key] = -1
    for key in ("eligible_at", "due_at", "verified_at", "terminal_at", "created_at", "updated_at"):
        try:
            payload[key] = float(payload[key] or 0)
        except (TypeError, ValueError):
            payload[key] = math.nan
    try:
        verification = json.loads(str(payload.get("verification_json") or "{}"))
    except (json.JSONDecodeError, TypeError):
        verification = {}
    payload["verification"] = verification if isinstance(verification, dict) else {}
    return payload


def _ai_delivery_attempt_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    payload = dict(zip(_AI_DELIVERY_ATTEMPT_FIELDS, row, strict=True))
    payload["acceptance_run_id"] = str(payload.get("acceptance_run_id") or "")
    payload["attempt_number"] = int(payload["attempt_number"] or 0)
    for key in ("started_at", "finished_at", "created_at", "updated_at"):
        payload[key] = float(payload[key] or 0)
    return payload


def _ai_delivery_translated_languages_are_valid(languages: tuple[str, ...]) -> bool:
    if len(languages) != 3 or languages[1:] != AI_DELIVERY_TRANSLATED_CHINESE_LANGUAGES:
        return False
    source_language = languages[0]
    if not source_language or not all(
        re.fullmatch(r"[A-Za-z0-9]{1,8}", part) for part in source_language.split("-")
    ):
        return False
    source_primary = source_language.split("-", 1)[0].casefold()
    return source_language == "ja" or source_primary not in AI_DELIVERY_NON_DELIVERABLE_SOURCE_LANGUAGES


def _verified_ai_delivery_publication_semantics(
    verification: Any,
    *,
    expected_policy_revision: str,
) -> dict[str, Any] | None:
    """Validate the immutable publication facts carried by strict evidence."""

    if not isinstance(verification, dict):
        return None
    if verification.get("publication_semantics_verified") is not True:
        return None
    if str(verification.get("publication_contract") or "") != AI_DELIVERY_PUBLICATION_CONTRACT:
        return None
    policy_revision = str(expected_policy_revision or "").strip()
    if not policy_revision:
        return None
    if str(verification.get("expected_policy_revision") or "") != policy_revision:
        return None
    if str(verification.get("manifest_policy_revision") or "") != policy_revision:
        return None
    if verification.get("policy_revision_matched") is not True:
        return None
    kind = str(verification.get("publication_kind") or "").strip()
    languages = verification.get("output_languages")
    if not isinstance(languages, list) or not languages:
        return None
    if not all(isinstance(language, str) and language.strip() == language for language in languages):
        return None
    normalized_languages = tuple(languages)
    if kind == "translated_trilingual":
        if not _ai_delivery_translated_languages_are_valid(normalized_languages):
            return None
    elif kind in {"adopted_zh_tw", "converted_zh_cn"}:
        if normalized_languages != AI_DELIVERY_TRADITIONAL_CHINESE_LANGUAGES:
            return None
    else:
        # source_language remains a useful artifact, but this ledger proves
        # delivery of usable Traditional Chinese and therefore must count it as
        # a miss rather than a success.
        return None
    return {
        "contract": AI_DELIVERY_PUBLICATION_CONTRACT,
        "kind": kind,
        "output_languages": list(normalized_languages),
    }


def _summarize_matured_ai_delivery_obligations(
    obligations: list[dict[str, Any]],
    *,
    expected_due_from: float,
    expected_due_to: float,
    policy_revision: str,
) -> dict[str, Any]:
    """Classify matured media obligations using the immutable expected deadline."""

    matured: list[tuple[dict[str, Any], float, bool]] = []
    invalid_eligible = 0
    for item in obligations:
        if str(item.get("policy_revision") or "") != policy_revision:
            continue
        eligible_at = item.get("eligible_at")
        if not isinstance(eligible_at, (int, float)) or not math.isfinite(float(eligible_at)) or float(eligible_at) <= 0:
            invalid_eligible += 1
            continue
        expected_due = float(eligible_at) + AI_DELIVERY_DEADLINE_SECONDS
        if expected_due_from <= expected_due < expected_due_to:
            stored_due = item.get("due_at")
            due_valid = bool(
                isinstance(stored_due, (int, float))
                and math.isfinite(float(stored_due))
                and abs(float(stored_due) - expected_due) <= AI_DELIVERY_DUE_TOLERANCE_SECONDS
            )
            matured.append((item, expected_due, due_valid))

    valid_exclusions: list[dict[str, Any]] = []
    included: list[tuple[dict[str, Any], float, bool]] = []
    invalid_contract_misses = 0
    invalid_exclusions = 0
    for item, expected_due, due_valid in matured:
        valid_exclusion = bool(
            due_valid
            and item.get("state") == "excluded"
            and item.get("exclusion_code") in AI_DELIVERY_EXCLUSION_CODES
            and int(item.get("attempt_count") or 0) == 0
            and isinstance(item.get("terminal_at"), (int, float))
            and isinstance(item.get("updated_at"), (int, float))
            and math.isfinite(float(item["terminal_at"]))
            and math.isfinite(float(item["updated_at"]))
            and float(item["eligible_at"])
            <= float(item["terminal_at"])
            <= float(item["updated_at"])
            <= expected_due
        )
        if valid_exclusion:
            valid_exclusions.append(item)
            continue
        if item.get("state") == "excluded":
            invalid_exclusions += 1
        if not due_valid:
            invalid_contract_misses += 1
        included.append((item, expected_due, due_valid))

    successes: list[tuple[dict[str, Any], dict[str, Any]]] = []
    invalid_success_evidence = 0
    late_successes = 0
    for item, expected_due, due_valid in included:
        if item.get("state") != "succeeded":
            continue
        publication = _verified_ai_delivery_publication_semantics(
            item.get("verification"),
            expected_policy_revision=policy_revision,
        )
        verified_at = item.get("verified_at")
        verified_finite = bool(
            isinstance(verified_at, (int, float)) and math.isfinite(float(verified_at))
        )
        if not due_valid or publication is None or not verified_finite:
            invalid_success_evidence += 1
            continue
        if 0 < float(verified_at) <= expected_due:
            successes.append((item, publication))
        elif float(verified_at) > expected_due:
            late_successes += 1

    denominator = len(included)
    success_count = len(successes)
    misses = denominator - success_count
    exclusions_by_code: dict[str, int] = {}
    for item in valid_exclusions:
        code = str(item["exclusion_code"])
        exclusions_by_code[code] = exclusions_by_code.get(code, 0) + 1
    traditional_by_kind = {
        kind: sum(1 for _item, publication in successes if publication["kind"] == kind)
        for kind in sorted(AI_DELIVERY_TRADITIONAL_CHINESE_PUBLICATION_KINDS)
    }
    translated = traditional_by_kind["translated_trilingual"]
    return {
        "denominator": denominator,
        "verified_on_time": success_count,
        "misses": misses,
        "success_rate": success_count / denominator if denominator else None,
        "invalid_contract_misses": invalid_contract_misses,
        "invalid_eligible_obligations": invalid_eligible,
        "invalid_success_evidence": invalid_success_evidence,
        "late_successes": late_successes,
        "overdue_open": sum(1 for item, _due, _valid in included if item.get("state") == "open"),
        "invalid_exclusions": invalid_exclusions,
        "exclusions": {"total": len(valid_exclusions), "by_code": exclusions_by_code},
        "publication_breakdown": {
            "translated_chinese": {
                "publication_kinds": sorted(
                    AI_DELIVERY_TRADITIONAL_CHINESE_PUBLICATION_KINDS
                ),
                "verified_on_time": sum(traditional_by_kind.values()),
                "by_publication_kind": traditional_by_kind,
                "required_output_language": "zh-TW",
            },
            "source_language": {
                "publication_kind": "source_language",
                "verified_on_time": 0,
                "by_output_language": {},
                "counts_as_traditional_chinese_success": False,
            },
            "unclassified_misses": misses,
            "invalid_success_evidence": invalid_success_evidence,
        },
    }


def _strict_recovery_delivery_evidence(
    evidence: object,
    attempt: dict[str, Any],
) -> bool:
    """Validate the strict evidence shape before crash recovery records success."""

    if not isinstance(evidence, dict):
        return False
    verification = evidence.get("verification")
    if not isinstance(verification, dict):
        return False
    required_true = (
        "required_outputs_complete",
        "hashes_verified",
        "quality_gates_passed",
        "publication_marker_absent",
        "media_identity_matched",
        "policy_revision_matched",
    )
    if any(verification.get(key) is not True for key in required_true):
        return False
    if int(verification.get("manifest_schema_version") or 0) != 2:
        return False
    if str(verification.get("delivery_contract") or "") != "ai-delivery-v1":
        return False
    try:
        verified_at = float(evidence.get("verified_at") or 0)
        started_at = float(attempt.get("started_at") or 0)
        evidence_started_at = float(verification.get("attempt_started_at") or 0)
    except (TypeError, ValueError):
        return False
    if verified_at <= 0 or started_at <= 0 or verified_at < started_at:
        return False
    if not _same_timestamp(evidence_started_at, started_at):
        return False
    manifest_path = str(evidence.get("manifest_path") or "").strip()
    manifest_sha256 = str(evidence.get("manifest_sha256") or "").strip().casefold()
    return bool(manifest_path and re.fullmatch(r"[0-9a-f]{64}", manifest_sha256))


def _failure_revision(path: str, error_code: str, message: str) -> str:
    payload = "\x1f".join(
        (
            str(Path(path).resolve()),
            str(error_code or "unknown_failure").strip().casefold(),
            " ".join(str(message or "").strip().casefold().split()),
        )
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


def _legacy_failure_code(stage: str, message: str) -> str:
    normalized_stage = str(stage or "").strip().casefold()
    normalized_message = str(message or "").strip().casefold()
    if normalized_stage == "transcription_review":
        return "deterministic_asr_quality"
    if any(marker in normalized_message for marker in ("out of memory", "cuda failed with error out of memory")):
        return "transient_oom"
    if any(marker in normalized_message for marker in ("timed out", "timeout")):
        return "transient_timeout"
    if any(
        marker in normalized_message
        for marker in ("connection reset", "connection refused", "network is unreachable")
    ):
        return "transient_connection"
    if normalized_stage == "quality_check":
        return "legacy_quality_check"
    if normalized_stage == "transcription":
        return "legacy_transcription"
    if normalized_stage == "translation":
        return "legacy_translation"
    return f"legacy_{normalized_stage or 'unknown'}"


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return time.time_ns()


def _same_timestamp(left: object, right: object, *, tolerance: float = 0.001) -> bool:
    try:
        return abs(float(left or 0) - float(right or 0)) <= tolerance
    except (TypeError, ValueError):
        return False
