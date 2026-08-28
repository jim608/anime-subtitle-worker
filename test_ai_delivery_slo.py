from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scan_state import (
    AI_DELIVERY_ANYTIME_LOG_THRESHOLD,
    AI_DELIVERY_DEADLINE_SECONDS,
    AI_DELIVERY_MEASUREMENT_REVISION,
    AI_DELIVERY_SLO_MINIMUM_SAMPLE,
    AI_DELIVERY_SLO_TARGET,
    AI_DELIVERY_SLO_WINDOW_SECONDS,
    ScanStateStore,
    ai_delivery_anytime_log_e,
    ai_delivery_anytime_lower_bound,
    ai_delivery_identity,
)


def _strict_verification(
    policy_revision: str,
    *,
    publication_kind: str = "translated_trilingual",
    output_languages: list[str] | None = None,
) -> dict[str, object]:
    languages = output_languages or ["ja", "zh-CN", "zh-TW"]
    return {
        "publication_semantics_verified": True,
        "publication_contract": "ai-publication-semantics-v2",
        "publication_kind": publication_kind,
        "output_languages": languages,
        "expected_policy_revision": policy_revision,
        "manifest_policy_revision": policy_revision,
        "policy_revision_matched": True,
    }


class AiDeliveryLedgerTest(unittest.TestCase):
    def test_fresh_measurement_epoch_records_current_revision_and_actual_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("scan_state.time.time", return_value=1234.5):
                store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                meta = dict(
                    store._conn.execute(
                        "SELECT key, value FROM ai_delivery_meta"
                    ).fetchall()
                )
                self.assertEqual(
                    meta["measurement_revision"],
                    AI_DELIVERY_MEASUREMENT_REVISION,
                )
                self.assertEqual(float(meta["instrumented_at"]), 1234.5)
                self.assertEqual(
                    store.ai_delivery_slo_summary()["measurement_revision"],
                    AI_DELIVERY_MEASUREMENT_REVISION,
                )
            finally:
                store.close()

    def test_operational_slo_contract_is_99_99_percent_with_10k_sample(self) -> None:
        self.assertEqual(AI_DELIVERY_SLO_TARGET, 0.9999)
        self.assertGreaterEqual(AI_DELIVERY_SLO_MINIMUM_SAMPLE, 10_000)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                summary = store.ai_delivery_slo_summary()
                self.assertEqual(summary["target"], 0.9999)
                self.assertGreaterEqual(summary["minimum_sample"], 10_000)
            finally:
                store.close()

    def test_existing_instrumented_at_is_not_reset_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.sqlite3"
            original = ScanStateStore(database)
            try:
                original._conn.execute(
                    "UPDATE ai_delivery_meta SET value=?, updated_at=? WHERE key='instrumented_at'",
                    ("1234.5", 1234.5),
                )
                original.commit()
            finally:
                original.close()

            reopened = ScanStateStore(database)
            try:
                meta = dict(
                    reopened._conn.execute(
                        "SELECT key, value FROM ai_delivery_meta"
                    ).fetchall()
                )
                row = reopened._conn.execute(
                    "SELECT value, updated_at FROM ai_delivery_meta WHERE key='instrumented_at'"
                ).fetchone()
                self.assertEqual(row, ("1234.5", 1234.5))
                self.assertEqual(
                    meta["measurement_revision"],
                    AI_DELIVERY_MEASUREMENT_REVISION,
                )
            finally:
                reopened.close()

    def test_legacy_or_changed_revision_resets_epoch_once_and_preserves_ledger(self) -> None:
        for stored_revision in (None, "legacy-partial-proof-v0"):
            with self.subTest(stored_revision=stored_revision):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    database = root / "state.sqlite3"
                    with patch("scan_state.time.time", return_value=1000.0):
                        original = ScanStateStore(database)
                        obligation = original.ensure_ai_delivery_obligation(
                            root / "Episode.mkv",
                            media_size=1,
                            media_mtime_ns=1,
                            policy_revision="policy-v1",
                            eligible_at=1000.0,
                        )
                        attempt = original.begin_ai_delivery_attempt(
                            obligation["obligation_id"],
                            started_at=1001.0,
                        )
                        original.commit()
                        original.close()

                    connection = sqlite3.connect(database)
                    try:
                        connection.execute(
                            "UPDATE ai_delivery_meta SET value='500', updated_at=500 "
                            "WHERE key='instrumented_at'"
                        )
                        if stored_revision is None:
                            connection.execute(
                                "DELETE FROM ai_delivery_meta WHERE key='measurement_revision'"
                            )
                        else:
                            connection.execute(
                                "UPDATE ai_delivery_meta SET value=?, updated_at=500 "
                                "WHERE key='measurement_revision'",
                                (stored_revision,),
                            )
                        connection.commit()
                    finally:
                        connection.close()

                    with patch("scan_state.time.time", return_value=2000.0):
                        migrated = ScanStateStore(database)
                    try:
                        meta = dict(
                            migrated._conn.execute(
                                "SELECT key, value FROM ai_delivery_meta"
                            ).fetchall()
                        )
                        self.assertEqual(
                            meta["measurement_revision"],
                            AI_DELIVERY_MEASUREMENT_REVISION,
                        )
                        self.assertEqual(float(meta["instrumented_at"]), 2000.0)
                        self.assertEqual(
                            migrated._conn.execute(
                                "SELECT obligation_id, attempt_count "
                                "FROM ai_delivery_obligations"
                            ).fetchall(),
                            [(obligation["obligation_id"], 1)],
                        )
                        self.assertEqual(
                            migrated._conn.execute(
                                "SELECT attempt_id, obligation_id "
                                "FROM ai_delivery_attempts"
                            ).fetchall(),
                            [(attempt["attempt_id"], obligation["obligation_id"])],
                        )
                    finally:
                        migrated.close()

                    with patch("scan_state.time.time", return_value=3000.0):
                        stable = ScanStateStore(database)
                    try:
                        epoch = stable._conn.execute(
                            "SELECT value, updated_at FROM ai_delivery_meta "
                            "WHERE key='instrumented_at'"
                        ).fetchone()
                        self.assertEqual(epoch, ("2000.0", 2000.0))
                    finally:
                        stable.close()

    def test_old_database_migrates_without_losing_queue_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.sqlite3"
            video = root / "Episode 01.mkv"
            video.write_bytes(b"media")
            store = ScanStateStore(database)
            store.upsert_ai_queue_candidate(video, video.stat().st_mtime_ns)
            store.commit()
            store.close()

            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE ai_delivery_attempts")
                connection.execute("DROP TABLE ai_delivery_obligations")
                connection.execute("DROP TABLE ai_delivery_meta")
                connection.commit()
            finally:
                connection.close()

            migrated = ScanStateStore(database)
            try:
                tables = {
                    str(row[0])
                    for row in migrated._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                queue_status = migrated._conn.execute(
                    "SELECT status FROM ai_candidate_queue WHERE path=?",
                    (str(video.resolve()),),
                ).fetchone()[0]
                self.assertIn("ai_delivery_obligations", tables)
                self.assertIn("ai_delivery_attempts", tables)
                self.assertIn("ai_delivery_meta", tables)
                self.assertEqual(queue_status, "queued")
            finally:
                migrated.close()

    def test_identity_is_canonical_and_retries_do_not_duplicate_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Series" / "Episode 01.mkv"
            video.parent.mkdir()
            video.write_bytes(b"media")
            stat = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                first = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat.st_size,
                    media_mtime_ns=stat.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                duplicate = store.ensure_ai_delivery_obligation(
                    video.parent / "." / video.name,
                    media_size=stat.st_size,
                    media_mtime_ns=stat.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=2000,
                )
                first_attempt = store.begin_ai_delivery_attempt(first["obligation_id"], started_at=1100)
                store.finish_ai_delivery_attempt(
                    first_attempt["attempt_id"],
                    status="retryable_failure",
                    error_code="transient_timeout",
                    finished_at=1200,
                )
                second_attempt = store.begin_ai_delivery_attempt(first["obligation_id"], started_at=1300)
                store.finish_ai_delivery_attempt(
                    second_attempt["attempt_id"],
                    status="review_required",
                    error_code="asr_quality_review",
                    finished_at=1400,
                )
                store.commit()

                self.assertEqual(first["obligation_id"], duplicate["obligation_id"])
                self.assertEqual(first["due_at"], 1000 + AI_DELIVERY_DEADLINE_SECONDS)
                self.assertEqual(
                    store._conn.execute("SELECT COUNT(*) FROM ai_delivery_obligations").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    store._conn.execute("SELECT COUNT(*) FROM ai_delivery_attempts").fetchone()[0],
                    2,
                )
                current = store.get_ai_delivery_obligation(first["obligation_id"])
                self.assertEqual(current["attempt_count"], 2)
                self.assertEqual(current["outcome_code"], "asr_quality_review")
            finally:
                store.close()

    def test_identity_changes_for_media_or_policy_revision(self) -> None:
        path = Path("Episode.mkv")
        first = ai_delivery_identity(
            path,
            media_size=10,
            media_mtime_ns=20,
            policy_revision="policy-v1",
        )
        changed_media = ai_delivery_identity(
            path,
            media_size=11,
            media_mtime_ns=20,
            policy_revision="policy-v1",
        )
        changed_policy = ai_delivery_identity(
            path,
            media_size=10,
            media_mtime_ns=20,
            policy_revision="policy-v2",
        )

        self.assertNotEqual(first["obligation_id"], changed_media["obligation_id"])
        self.assertNotEqual(first["obligation_id"], changed_policy["obligation_id"])
        self.assertEqual(first["media_fingerprint"], changed_policy["media_fingerprint"])

    def test_media_and_policy_supersede_only_unattempted_old_obligations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode.mkv"
            store = ScanStateStore(root / "state.sqlite3")
            try:
                with patch("scan_state.time.time", return_value=1500):
                    attempted = store.ensure_ai_delivery_obligation(
                        video,
                        media_size=1,
                        media_mtime_ns=1,
                        policy_revision="policy-attempted",
                        eligible_at=1000,
                    )
                    store.begin_ai_delivery_attempt(attempted["obligation_id"], started_at=1100)

                    old_media = store.ensure_ai_delivery_obligation(
                        video,
                        media_size=2,
                        media_mtime_ns=2,
                        policy_revision="policy-v1",
                        eligible_at=1200,
                    )
                    old_policy = store.ensure_ai_delivery_obligation(
                        video,
                        media_size=3,
                        media_mtime_ns=3,
                        policy_revision="policy-v1",
                        eligible_at=1300,
                    )
                    current = store.ensure_ai_delivery_obligation(
                        video,
                        media_size=3,
                        media_mtime_ns=3,
                        policy_revision="policy-v2",
                        eligible_at=1400,
                    )

                attempted_now = store.get_ai_delivery_obligation(attempted["obligation_id"])
                old_media_now = store.get_ai_delivery_obligation(old_media["obligation_id"])
                old_policy_now = store.get_ai_delivery_obligation(old_policy["obligation_id"])
                current_now = store.get_ai_delivery_obligation(current["obligation_id"])
                self.assertEqual(attempted_now["state"], "open")
                self.assertEqual(attempted_now["attempt_count"], 1)
                self.assertEqual(old_media_now["state"], "excluded")
                self.assertEqual(old_media_now["exclusion_code"], "superseded_before_attempt")
                self.assertEqual(old_policy_now["state"], "excluded")
                self.assertEqual(old_policy_now["exclusion_code"], "superseded_before_attempt")
                self.assertEqual(current_now["state"], "open")
                self.assertEqual(current_now["attempt_count"], 0)
                self.assertEqual(old_media_now["eligible_at"], 1200)
                self.assertEqual(old_policy_now["eligible_at"], 1200)
                self.assertEqual(current_now["eligible_at"], 1200)
                self.assertEqual(
                    current_now["due_at"],
                    1200 + AI_DELIVERY_DEADLINE_SECONDS,
                )
            finally:
                store.close()

    def test_revision_admission_and_pre_attempt_supersede_share_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode.mkv"
            store = ScanStateStore(root / "state.sqlite3")
            try:
                old = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                store.commit()

                with patch("scan_state.time.time", return_value=1100):
                    current = store.ensure_ai_delivery_obligation(
                        video,
                        media_size=2,
                        media_mtime_ns=2,
                        policy_revision="policy-v2",
                        eligible_at=1100,
                    )
                self.assertEqual(
                    store.get_ai_delivery_obligation(old["obligation_id"])["state"],
                    "excluded",
                )
                store.rollback()

                self.assertEqual(
                    store.get_ai_delivery_obligation(old["obligation_id"])["state"],
                    "open",
                )
                self.assertIsNone(store.get_ai_delivery_obligation(current["obligation_id"]))
            finally:
                store.close()

    def test_same_identity_zero_attempt_exclusion_reopens_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode.mkv"
            store = ScanStateStore(root / "state.sqlite3")
            try:
                original = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=10,
                    media_mtime_ns=20,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                    source="initial_scan",
                )
                with patch("scan_state.time.time", return_value=1100):
                    store.exclude_ai_delivery_obligation(
                        original["obligation_id"],
                        exclusion_code="local_chinese_subtitle_present_before_attempt",
                        detail="local subtitle appeared",
                        excluded_at=1100,
                    )
                # Exercise every terminal/evidence field that reopen promises to
                # clear, including defensive cleanup of malformed legacy data.
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET verified_at=1099, manifest_path='/stale/manifest.json',
                        manifest_sha256=?, verification_json=?
                    WHERE obligation_id=?
                    """,
                    ("a" * 64, '{"stale": true}', original["obligation_id"]),
                )
                store.commit()

                reopened = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=10,
                    media_mtime_ns=20,
                    policy_revision="policy-v1",
                    eligible_at=2000,
                    source="scanner_readmission",
                )
                self.assertEqual(reopened["obligation_id"], original["obligation_id"])
                self.assertEqual(reopened["state"], "open")
                self.assertEqual(reopened["eligible_at"], 1000)
                self.assertEqual(reopened["due_at"], 1000 + AI_DELIVERY_DEADLINE_SECONDS)
                self.assertEqual(reopened["source"], "scanner_readmission")
                self.assertEqual(reopened["attempt_count"], 0)
                for field in ("verified_at", "terminal_at"):
                    self.assertEqual(reopened[field], 0)
                for field in (
                    "outcome_code",
                    "exclusion_code",
                    "exclusion_detail",
                    "manifest_path",
                    "manifest_sha256",
                ):
                    self.assertEqual(reopened[field], "")
                self.assertEqual(reopened["verification"], {})

                store.rollback()
                rolled_back = store.get_ai_delivery_obligation(original["obligation_id"])
                self.assertEqual(rolled_back["state"], "excluded")
                self.assertEqual(rolled_back["exclusion_code"], "local_chinese_subtitle_present_before_attempt")

                reopened_again = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=10,
                    media_mtime_ns=20,
                    policy_revision="policy-v1",
                    eligible_at=3000,
                    source="scanner_readmission",
                )
                attempt = store.begin_ai_delivery_attempt(
                    reopened_again["obligation_id"],
                    started_at=3100,
                )
                self.assertEqual(attempt["attempt_number"], 1)
            finally:
                store.close()

    def test_failed_supersede_rolls_back_new_identity_without_touching_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode.mkv"
            store = ScanStateStore(root / "state.sqlite3")
            try:
                old = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                store.commit()
                new_identity = ai_delivery_identity(
                    video,
                    media_size=2,
                    media_mtime_ns=2,
                    policy_revision="policy-v2",
                )

                with (
                    patch("scan_state.time.time", return_value=1100),
                    patch.object(
                        store,
                        "exclude_pre_attempt_ai_delivery_obligations_for_path",
                        side_effect=RuntimeError("injected exclusion failure"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "injected exclusion failure"),
                ):
                    store.ensure_ai_delivery_obligation(
                        video,
                        media_size=2,
                        media_mtime_ns=2,
                        policy_revision="policy-v2",
                        eligible_at=1100,
                    )

                self.assertEqual(
                    store.get_ai_delivery_obligation(old["obligation_id"])["state"],
                    "open",
                )
                self.assertIsNone(
                    store.get_ai_delivery_obligation(new_identity["obligation_id"])
                )
            finally:
                store.close()

    def test_exclusions_require_positive_timestamp_at_or_before_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            try:
                at_due = store.ensure_ai_delivery_obligation(
                    root / "At Due.mkv",
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                due_at = float(at_due["due_at"])
                with patch("scan_state.time.time", return_value=due_at):
                    excluded = store.exclude_ai_delivery_obligation(
                        at_due["obligation_id"],
                        exclusion_code="media_missing_before_attempt",
                        excluded_at=due_at,
                    )
                self.assertEqual(excluded["state"], "excluded")
                self.assertEqual(excluded["terminal_at"], due_at)

                after_due = store.ensure_ai_delivery_obligation(
                    root / "After Due.mkv",
                    media_size=2,
                    media_mtime_ns=2,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                with (
                    patch(
                        "scan_state.time.time",
                        return_value=float(after_due["due_at"]) + 1,
                    ),
                    self.assertRaisesRegex(ValueError, "delivery deadline"),
                ):
                    store.exclude_ai_delivery_obligation(
                        after_due["obligation_id"],
                        exclusion_code="media_missing_before_attempt",
                        # A backdated caller timestamp cannot hide that the
                        # exclusion was actually written after the deadline.
                        excluded_at=float(after_due["due_at"]) - 1,
                    )
                self.assertEqual(
                    store.get_ai_delivery_obligation(after_due["obligation_id"])["state"],
                    "open",
                )

                before_eligible = store.ensure_ai_delivery_obligation(
                    root / "Before Eligible.mkv",
                    media_size=5,
                    media_mtime_ns=5,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                with (
                    patch("scan_state.time.time", return_value=999),
                    self.assertRaisesRegex(ValueError, "between eligibility"),
                ):
                    store.exclude_ai_delivery_obligation(
                        before_eligible["obligation_id"],
                        exclusion_code="media_missing_before_attempt",
                        excluded_at=999,
                    )
                self.assertEqual(
                    store.get_ai_delivery_obligation(before_eligible["obligation_id"])["state"],
                    "open",
                )

                path_due = store.ensure_ai_delivery_obligation(
                    root / "Path At Due.mkv",
                    media_size=3,
                    media_mtime_ns=3,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                with patch("scan_state.time.time", return_value=float(path_due["due_at"])):
                    self.assertEqual(
                        store.exclude_pre_attempt_ai_delivery_obligations_for_path(
                            root / "Path At Due.mkv",
                            exclusion_code="media_missing_before_attempt",
                            excluded_at=float(path_due["due_at"]),
                        ),
                        1,
                    )

                path_overdue = store.ensure_ai_delivery_obligation(
                    root / "Path Overdue.mkv",
                    media_size=4,
                    media_mtime_ns=4,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                with patch(
                    "scan_state.time.time",
                    return_value=float(path_overdue["due_at"]) + 1,
                ):
                    self.assertEqual(
                        store.exclude_pre_attempt_ai_delivery_obligations_for_path(
                            root / "Path Overdue.mkv",
                            exclusion_code="media_missing_before_attempt",
                            excluded_at=float(path_overdue["due_at"]) - 1,
                        ),
                        0,
                    )
                self.assertEqual(
                    store.get_ai_delivery_obligation(path_overdue["obligation_id"])["state"],
                    "open",
                )
            finally:
                store.close()

    def test_attempted_or_successful_terminal_obligation_is_never_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            try:
                attempted = store.ensure_ai_delivery_obligation(
                    root / "Attempted.mkv",
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                store.begin_ai_delivery_attempt(attempted["obligation_id"], started_at=1100)
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='excluded', terminal_at=1200,
                        outcome_code='excluded',
                        exclusion_code='media_missing_before_attempt'
                    WHERE obligation_id=?
                    """,
                    (attempted["obligation_id"],),
                )
                attempted_again = store.ensure_ai_delivery_obligation(
                    root / "Attempted.mkv",
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=2000,
                    source="scanner_readmission",
                )
                self.assertEqual(attempted_again["state"], "excluded")
                self.assertEqual(attempted_again["attempt_count"], 1)
                self.assertEqual(attempted_again["eligible_at"], 1000)
                with self.assertRaises(ValueError):
                    store.begin_ai_delivery_attempt(attempted_again["obligation_id"])

                succeeded = store.ensure_ai_delivery_obligation(
                    root / "Succeeded.mkv",
                    media_size=2,
                    media_mtime_ns=2,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                success_attempt = store.begin_ai_delivery_attempt(
                    succeeded["obligation_id"],
                    started_at=1100,
                )
                store.finish_ai_delivery_attempt(
                    success_attempt["attempt_id"],
                    status="succeeded",
                    finished_at=1200,
                )
                store.mark_ai_delivery_verified(
                    succeeded["obligation_id"],
                    manifest_path="/work/manifest.json",
                    manifest_sha256="b" * 64,
                    verification=_strict_verification("policy-v1"),
                    evidence_verified=True,
                    verified_at=1200,
                )
                succeeded_again = store.ensure_ai_delivery_obligation(
                    root / "Succeeded.mkv",
                    media_size=2,
                    media_mtime_ns=2,
                    policy_revision="policy-v1",
                    eligible_at=3000,
                    source="scanner_readmission",
                )
                self.assertEqual(succeeded_again["state"], "succeeded")
                self.assertEqual(succeeded_again["eligible_at"], 1000)
                self.assertEqual(succeeded_again["manifest_path"], "/work/manifest.json")
                with self.assertRaises(ValueError):
                    store.begin_ai_delivery_attempt(succeeded_again["obligation_id"])
            finally:
                store.close()

    def test_path_scoped_pre_attempt_exclusion_matches_only_exact_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "Series" / "Episode.mkv"
            other = root / "Series Extra" / "Episode.mkv"
            store = ScanStateStore(root / "state.sqlite3")
            try:
                target_obligation = store.ensure_ai_delivery_obligation(
                    target,
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                unrelated = store.ensure_ai_delivery_obligation(
                    other,
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )

                with patch("scan_state.time.time", return_value=1200):
                    changed = store.exclude_pre_attempt_ai_delivery_obligations_for_path(
                        target.parent / "." / target.name,
                        exclusion_code="media_missing_before_attempt",
                        excluded_at=1200,
                    )

                self.assertEqual(changed, 1)
                self.assertEqual(
                    store.get_ai_delivery_obligation(target_obligation["obligation_id"])["exclusion_code"],
                    "media_missing_before_attempt",
                )
                self.assertEqual(
                    store.get_ai_delivery_obligation(unrelated["obligation_id"])["state"],
                    "open",
                )
            finally:
                store.close()

    def test_only_allowlisted_pre_attempt_exclusions_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode.mkv"
            video.write_bytes(b"media")
            stat = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                excluded = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat.st_size,
                    media_mtime_ns=stat.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                with self.assertRaises(ValueError):
                    store.exclude_ai_delivery_obligation(
                        excluded["obligation_id"],
                        exclusion_code="manual_skip",
                    )
                with patch("scan_state.time.time", return_value=1100):
                    result = store.exclude_ai_delivery_obligation(
                        excluded["obligation_id"],
                        exclusion_code="official_subtitle_present_before_attempt",
                        excluded_at=1100,
                    )
                self.assertEqual(result["state"], "excluded")

                attempted = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat.st_size + 1,
                    media_mtime_ns=stat.st_mtime_ns + 1,
                    policy_revision="policy-v1",
                    eligible_at=2000,
                )
                store.begin_ai_delivery_attempt(attempted["obligation_id"], started_at=2100)
                with self.assertRaises(ValueError):
                    store.exclude_ai_delivery_obligation(
                        attempted["obligation_id"],
                        exclusion_code="superseded_before_attempt",
                    )
            finally:
                store.close()

    def test_verified_success_requires_strict_manifest_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "Episode.mkv"
            video.write_bytes(b"media")
            stat = video.stat()
            store = ScanStateStore(root / "state.sqlite3")
            try:
                obligation = store.ensure_ai_delivery_obligation(
                    video,
                    media_size=stat.st_size,
                    media_mtime_ns=stat.st_mtime_ns,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                with self.assertRaises(ValueError):
                    store.mark_ai_delivery_verified(
                        obligation["obligation_id"],
                        manifest_path="/work/manifest.json",
                        manifest_sha256="a" * 64,
                        verification={"required_outputs_complete": True},
                        evidence_verified=False,
                        verified_at=1100,
                    )
                with self.assertRaisesRegex(ValueError, "publication kind"):
                    store.mark_ai_delivery_verified(
                        obligation["obligation_id"],
                        manifest_path="/work/manifest.json",
                        manifest_sha256="a" * 64,
                        verification={"required_outputs_complete": True},
                        evidence_verified=True,
                        verified_at=1100,
                    )
                verified = store.mark_ai_delivery_verified(
                    obligation["obligation_id"],
                    manifest_path="/work/manifest.json",
                    manifest_sha256="a" * 64,
                    verification=_strict_verification("policy-v1"),
                    evidence_verified=True,
                    verified_at=1100,
                )
                self.assertEqual(verified["state"], "succeeded")
                self.assertEqual(verified["outcome_code"], "verified_on_time")

                non_japanese = store.ensure_ai_delivery_obligation(
                    root / "English Episode.mkv",
                    media_size=2,
                    media_mtime_ns=2,
                    policy_revision="policy-v1",
                    eligible_at=1000,
                )
                translated = store.mark_ai_delivery_verified(
                    non_japanese["obligation_id"],
                    manifest_path="/work/english-manifest.json",
                    manifest_sha256="b" * 64,
                    verification=_strict_verification(
                        "policy-v1",
                        output_languages=["en", "zh-CN", "zh-TW"],
                    ),
                    evidence_verified=True,
                    verified_at=1100,
                )
                self.assertEqual(translated["state"], "succeeded")
            finally:
                store.close()

    def test_rolling_slo_counts_open_and_late_as_misses_and_excludes_only_valid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            now = 10_000_000.0
            due = now - 1000
            eligible = due - AI_DELIVERY_DEADLINE_SECONDS
            try:
                store._conn.execute(
                    "UPDATE ai_delivery_meta SET value=? WHERE key='instrumented_at'",
                    (str(now - AI_DELIVERY_SLO_WINDOW_SECONDS - AI_DELIVERY_DEADLINE_SECONDS - 1),),
                )

                def create(index: int) -> dict[str, object]:
                    return store.ensure_ai_delivery_obligation(
                        root / f"Episode {index}.mkv",
                        media_size=index,
                        media_mtime_ns=index,
                        policy_revision="policy-v1",
                        eligible_at=eligible,
                    )

                on_time = create(1)
                late = create(2)
                create(3)
                excluded = create(4)
                invalid_attempted_exclusion = create(5)
                corrupt_exclusion = create(6)
                late_exclusion = create(7)
                backdated_exclusion = create(8)
                post_deadline_write = create(9)
                store.mark_ai_delivery_verified(
                    on_time["obligation_id"],
                    manifest_path="/work/on-time.json",
                    manifest_sha256="1" * 64,
                    verification=_strict_verification("policy-v1"),
                    evidence_verified=True,
                    verified_at=due - 1,
                )
                store.mark_ai_delivery_verified(
                    late["obligation_id"],
                    manifest_path="/work/late.json",
                    manifest_sha256="2" * 64,
                    verification=_strict_verification("policy-v1"),
                    evidence_verified=True,
                    verified_at=due + 1,
                )
                with patch("scan_state.time.time", return_value=eligible + 1):
                    store.exclude_ai_delivery_obligation(
                        excluded["obligation_id"],
                        exclusion_code="media_missing_before_attempt",
                        excluded_at=eligible + 1,
                    )
                store.begin_ai_delivery_attempt(
                    invalid_attempted_exclusion["obligation_id"],
                    started_at=eligible + 1,
                )
                # Simulate a legacy/manual row that bypassed the write API. The
                # SLO reader must still fail closed and keep it in the denominator.
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='excluded', terminal_at=?, updated_at=?,
                        exclusion_code='media_missing_before_attempt'
                    WHERE obligation_id=?
                    """,
                    (
                        eligible + 1,
                        eligible + 1,
                        invalid_attempted_exclusion["obligation_id"],
                    ),
                )
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='excluded', terminal_at=0, updated_at=?,
                        exclusion_code='media_missing_before_attempt'
                    WHERE obligation_id=?
                    """,
                    (eligible + 1, corrupt_exclusion["obligation_id"]),
                )
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='excluded', terminal_at=?, updated_at=?,
                        exclusion_code='media_missing_before_attempt'
                    WHERE obligation_id=?
                    """,
                    (due + 1, due + 1, late_exclusion["obligation_id"]),
                )
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='excluded', terminal_at=?, updated_at=?,
                        exclusion_code='media_missing_before_attempt'
                    WHERE obligation_id=?
                    """,
                    (eligible - 1, eligible + 1, backdated_exclusion["obligation_id"]),
                )
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='excluded', terminal_at=?, updated_at=?,
                        exclusion_code='media_missing_before_attempt'
                    WHERE obligation_id=?
                    """,
                    (due - 1, due + 1, post_deadline_write["obligation_id"]),
                )
                inventory = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=now - 2,
                )
                store.finalize_ai_inventory_epoch(
                    inventory["epoch_id"],
                    completed_at=now - 1,
                )
                summary = store.ai_delivery_slo_summary(now=now, minimum_sample=1)

                self.assertEqual(summary["state"], "warming")
                self.assertFalse(summary["full_window"])
                self.assertEqual(summary["denominator"], 0)
                self.assertEqual(
                    summary["publication_breakdown"]["translated_chinese"]["verified_on_time"],
                    0,
                )
                self.assertEqual(
                    summary["publication_breakdown"]["source_language"]["verified_on_time"],
                    0,
                )
                self.assertEqual(
                    summary["publication_breakdown"]["invalid_success_evidence"],
                    0,
                )
            finally:
                store.close()

    def test_slo_breaks_down_source_language_and_rejects_malformed_success_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            now = 10_000_000.0
            due = now - 1000
            eligible = due - AI_DELIVERY_DEADLINE_SECONDS
            try:
                store._conn.execute(
                    "UPDATE ai_delivery_meta SET value=? WHERE key='instrumented_at'",
                    (str(now - AI_DELIVERY_SLO_WINDOW_SECONDS - AI_DELIVERY_DEADLINE_SECONDS - 1),),
                )
                source = store.ensure_ai_delivery_obligation(
                    root / "English.mkv",
                    media_size=1,
                    media_mtime_ns=1,
                    policy_revision="policy-v1",
                    eligible_at=eligible,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "strict delivery evidence",
                ):
                    store.mark_ai_delivery_verified(
                        source["obligation_id"],
                        manifest_path="/work/source.json",
                        manifest_sha256="1" * 64,
                        verification=_strict_verification(
                            "policy-v1",
                            publication_kind="source_language",
                            output_languages=["en"],
                        ),
                        evidence_verified=True,
                        verified_at=due - 1,
                    )
                malformed = store.ensure_ai_delivery_obligation(
                    root / "Malformed.mkv",
                    media_size=2,
                    media_mtime_ns=2,
                    policy_revision="policy-v1",
                    eligible_at=eligible,
                )
                store._conn.execute(
                    """
                    UPDATE ai_delivery_obligations
                    SET state='succeeded', verified_at=?, terminal_at=?,
                        verification_json='{"publication_kind":"arbitrary_success"}'
                    WHERE obligation_id=?
                    """,
                    (due - 1, due - 1, malformed["obligation_id"]),
                )

                summary = store.ai_delivery_slo_summary(now=now, minimum_sample=1)

                self.assertEqual(summary["denominator"], 0)
                self.assertEqual(summary["verified_on_time"], 0)
                self.assertEqual(summary["misses"], 0)
                self.assertIsNone(summary["success_rate"])
                breakdown = summary["publication_breakdown"]
                self.assertEqual(breakdown["source_language"]["verified_on_time"], 0)
                self.assertEqual(breakdown["source_language"]["by_output_language"], {})
                self.assertEqual(breakdown["translated_chinese"]["verified_on_time"], 0)
                self.assertEqual(breakdown["invalid_success_evidence"], 0)
                self.assertEqual(breakdown["unclassified_misses"], 0)
            finally:
                store.close()

    def test_strict_evidence_accepts_all_traditional_chinese_source_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            try:
                for position, kind in enumerate(
                    ("adopted_zh_tw", "converted_zh_cn"),
                    start=1,
                ):
                    obligation = store.ensure_ai_delivery_obligation(
                        root / f"Route-{position}.mkv",
                        media_size=position,
                        media_mtime_ns=position,
                        policy_revision="policy-v1",
                        eligible_at=1000.0,
                    )
                    store.mark_ai_delivery_verified(
                        obligation["obligation_id"],
                        manifest_path=f"/work/{kind}.json",
                        manifest_sha256=str(position) * 64,
                        verification=_strict_verification(
                            "policy-v1",
                            publication_kind=kind,
                            output_languages=["zh-TW"],
                        ),
                        evidence_verified=True,
                        verified_at=1001.0,
                    )
                states = store._conn.execute(
                    "SELECT state FROM ai_delivery_obligations ORDER BY canonical_path"
                ).fetchall()
                self.assertEqual(states, [("succeeded",), ("succeeded",)])
            finally:
                store.close()

    def test_slo_is_warming_until_deadline_plus_full_window_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScanStateStore(Path(temp_dir) / "state.sqlite3")
            try:
                instrumented_at = float(
                    store._conn.execute(
                        "SELECT value FROM ai_delivery_meta WHERE key='instrumented_at'"
                    ).fetchone()[0]
                )
                evaluated_at = instrumented_at + AI_DELIVERY_DEADLINE_SECONDS
                inventory = store.begin_ai_inventory_epoch(
                    policy_revision="policy-v1",
                    root_signature="root-v1",
                    started_at=evaluated_at - 1,
                )
                store.finalize_ai_inventory_epoch(
                    inventory["epoch_id"],
                    completed_at=evaluated_at,
                )
                summary = store.ai_delivery_slo_summary(
                    now=evaluated_at,
                    minimum_sample=1,
                )
                self.assertEqual(summary["state"], "warming")
                self.assertFalse(summary["full_window"])
                self.assertIsNone(summary["target_met"])
            finally:
                store.close()

    def test_window_is_lower_bound_inclusive_and_upper_bound_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ScanStateStore(root / "state.sqlite3")
            now = 20_000_000.0
            lower = now - AI_DELIVERY_SLO_WINDOW_SECONDS
            try:
                store._conn.execute(
                    "UPDATE ai_delivery_meta SET value=? WHERE key='instrumented_at'",
                    (str(lower - AI_DELIVERY_DEADLINE_SECONDS - 1),),
                )
                for index, due_at in enumerate((lower, now), start=1):
                    store.ensure_ai_delivery_obligation(
                        root / f"Boundary {index}.mkv",
                        media_size=index,
                        media_mtime_ns=index,
                        policy_revision="policy-v1",
                        eligible_at=due_at - AI_DELIVERY_DEADLINE_SECONDS,
                    )
                summary = store.ai_delivery_slo_summary(now=now, minimum_sample=1)
                self.assertEqual(summary["denominator"], 0)
            finally:
                store.close()

    def test_anytime_eprocess_exact_crossings_and_non_latching_miss(self) -> None:
        vectors = ((0, 38_856), (1, 62_786), (2, 85_517))
        for misses, sample in vectors:
            before = ai_delivery_anytime_log_e(
                AI_DELIVERY_SLO_TARGET,
                sample - 1 - misses,
                misses,
            )
            at = ai_delivery_anytime_log_e(
                AI_DELIVERY_SLO_TARGET,
                sample - misses,
                misses,
            )
            self.assertLess(before, AI_DELIVERY_ANYTIME_LOG_THRESHOLD)
            self.assertGreaterEqual(at, AI_DELIVERY_ANYTIME_LOG_THRESHOLD)
            self.assertLess(
                ai_delivery_anytime_lower_bound(sample - 1 - misses, misses),
                AI_DELIVERY_SLO_TARGET,
            )
            self.assertGreaterEqual(
                ai_delivery_anytime_lower_bound(sample - misses, misses),
                AI_DELIVERY_SLO_TARGET,
            )
        crossed = ai_delivery_anytime_log_e(AI_DELIVERY_SLO_TARGET, 38_856, 0)
        after_miss = ai_delivery_anytime_log_e(AI_DELIVERY_SLO_TARGET, 38_856, 1)
        self.assertGreaterEqual(crossed, AI_DELIVERY_ANYTIME_LOG_THRESHOLD)
        self.assertLess(after_miss, AI_DELIVERY_ANYTIME_LOG_THRESHOLD)

    def test_anytime_eprocess_stays_finite_at_extreme_sample_sizes(self) -> None:
        high = ai_delivery_anytime_log_e(AI_DELIVERY_SLO_TARGET, 1_000_000_000, 0)
        at_target = ai_delivery_anytime_log_e(
            AI_DELIVERY_SLO_TARGET,
            999_900_000,
            100_000,
        )
        self.assertAlmostEqual(high, 90004.2571858, places=6)
        self.assertAlmostEqual(at_target, -19316.6612865, places=6)
        self.assertGreater(
            ai_delivery_anytime_lower_bound(1_000_000_000, 0),
            AI_DELIVERY_SLO_TARGET,
        )


if __name__ == "__main__":
    unittest.main()
