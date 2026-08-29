from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from control_state import (
    active_auto_remediation_campaign,
    auto_remediation_claim_guard,
    claim_next_command,
    create_and_bind_auto_remediation_item,
    create_auto_remediation_campaign,
    enqueue_command,
    finish_command,
    increment_daily_metric,
    initialize_control_state,
    latest_review_autopilot_command,
    list_open_ai_quality_review_targets,
    list_open_review_autopilot_candidates,
    next_review_autopilot_retry_attempt,
    record_daily_sample,
    resolve_review_item,
    resolve_sibling_target_reviews,
    resume_auto_remediation_campaign,
    review_autopilot_revision_attempt_allowed,
    upsert_review_item,
    update_auto_remediation_campaign,
)


class ControlStateMetricsTest(unittest.TestCase):
    def test_create_and_bind_remediation_item_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            campaign = create_auto_remediation_campaign(
                config,
                campaign_key="atomic-bind",
                parameters={"strategy_version": "canary-once-v1"},
            )
            arguments = {
                "campaign_id": campaign["campaign_id"],
                "path": str(root / "Show - S01E01.mkv"),
                "failure_revision": "a" * 24,
                "strategy": "retry_preserve_budget",
                "before": {"status": "failed_retry", "attempts": 1},
                "next_run_at": 123.5,
            }

            item = create_and_bind_auto_remediation_item(config, **arguments)
            repeated = create_and_bind_auto_remediation_item(
                config,
                **{**arguments, "next_run_at": 999.0},
            )

            self.assertEqual(item["item_id"], repeated["item_id"])
            self.assertEqual(item["status"], "preparing")
            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM auto_remediation_items"
                ).fetchone()[0]
                current_item_id, next_run_at = connection.execute(
                    """
                    SELECT current_item_id, next_run_at
                    FROM auto_remediation_campaigns
                    WHERE campaign_id=?
                    """,
                    (campaign["campaign_id"],),
                ).fetchone()
            self.assertEqual(item_count, 1)
            self.assertEqual(current_item_id, item["item_id"])
            self.assertEqual(next_run_at, 123.5)

    def test_create_and_bind_remediation_item_rolls_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            campaign = create_auto_remediation_campaign(
                config,
                campaign_key="atomic-rollback",
                parameters={},
            )

            with patch("control_state._audit", side_effect=RuntimeError("audit failed")):
                with self.assertRaisesRegex(RuntimeError, "audit failed"):
                    create_and_bind_auto_remediation_item(
                        config,
                        campaign_id=campaign["campaign_id"],
                        path=str(root / "Show - S01E02.mkv"),
                        failure_revision="b" * 24,
                        strategy="retry_preserve_budget",
                        before={"status": "failed_retry"},
                        next_run_at=456.0,
                    )

            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM auto_remediation_items"
                ).fetchone()[0]
                current_item_id, next_run_at = connection.execute(
                    """
                    SELECT current_item_id, next_run_at
                    FROM auto_remediation_campaigns
                    WHERE campaign_id=?
                    """,
                    (campaign["campaign_id"],),
                ).fetchone()
            self.assertEqual(item_count, 0)
            self.assertEqual(current_item_id, "")
            self.assertEqual(next_run_at, campaign["next_run_at"])

    def test_create_and_bind_remediation_item_requires_free_running_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            campaign = create_auto_remediation_campaign(
                config,
                campaign_key="atomic-guards",
                parameters={},
            )
            arguments = {
                "campaign_id": campaign["campaign_id"],
                "path": str(root / "Show - S01E03.mkv"),
                "failure_revision": "c" * 24,
                "strategy": "retry_preserve_budget",
                "before": {"status": "failed_retry"},
                "next_run_at": 789.0,
            }

            update_auto_remediation_campaign(
                config,
                campaign["campaign_id"],
                state="paused",
            )
            with self.assertRaisesRegex(ValueError, "running campaign"):
                create_and_bind_auto_remediation_item(config, **arguments)
            resume_auto_remediation_campaign(
                config,
                campaign["campaign_id"],
            )
            update_auto_remediation_campaign(
                config,
                campaign["campaign_id"],
                current_item_id="sweepitem_other",
            )
            with self.assertRaisesRegex(ValueError, "already has a current item"):
                create_and_bind_auto_remediation_item(config, **arguments)

            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM auto_remediation_items"
                ).fetchone()[0]
            self.assertEqual(item_count, 0)

    def test_active_remediation_campaign_is_strategy_bound_and_includes_paused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            canary = create_auto_remediation_campaign(
                config,
                campaign_key="canary",
                parameters={"strategy_version": "canary-once-v1"},
            )

            self.assertEqual(
                active_auto_remediation_campaign(
                    config,
                    strategy_version="canary-once-v1",
                )["campaign_id"],
                canary["campaign_id"],
            )
            update_auto_remediation_campaign(
                config,
                canary["campaign_id"],
                state="completed",
            )
            ordinary = create_auto_remediation_campaign(
                config,
                campaign_key="ordinary",
                parameters={"strategy_version": "safe-sweep-v1"},
            )
            update_auto_remediation_campaign(
                config,
                ordinary["campaign_id"],
                state="paused",
            )

            self.assertIsNone(
                active_auto_remediation_campaign(
                    config,
                    strategy_version="canary-once-v1",
                )
            )
            self.assertEqual(
                active_auto_remediation_campaign(config)["campaign_id"],
                ordinary["campaign_id"],
            )

    def test_concurrent_campaign_creates_admit_only_one_active_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            initialize_control_state(config)
            barrier = threading.Barrier(2)

            def create(key: str) -> dict[str, object]:
                barrier.wait()
                return create_auto_remediation_campaign(
                    config,
                    campaign_key=key,
                    parameters={"strategy_version": key},
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(create, key) for key in ("race-a", "race-b")]
                successes = []
                failures = []
                for future in futures:
                    try:
                        successes.append(future.result())
                    except ValueError as exc:
                        failures.append(str(exc))

            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIn("only one running or paused", failures[0])
            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                active_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM auto_remediation_campaigns
                    WHERE state IN ('running', 'paused')
                    """
                ).fetchone()[0]
            self.assertEqual(active_count, 1)

    def test_concurrent_resume_uses_paused_state_compare_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            campaign = create_auto_remediation_campaign(
                config,
                campaign_key="resume-race",
                parameters={"strategy_version": "safe-sweep-v1"},
            )
            update_auto_remediation_campaign(
                config,
                campaign["campaign_id"],
                state="paused",
            )
            barrier = threading.Barrier(2)

            def resume() -> dict[str, object]:
                barrier.wait()
                return resume_auto_remediation_campaign(
                    config,
                    campaign["campaign_id"],
                    next_run_at=123.0,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(resume) for _ in range(2)]
                successes = []
                failures = []
                for future in futures:
                    try:
                        successes.append(future.result())
                    except ValueError as exc:
                        failures.append(str(exc))

            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIn("only a paused", failures[0])
            self.assertEqual(
                active_auto_remediation_campaign(config)["state"],
                "running",
            )

    def test_claim_guard_waits_for_campaign_pause_handoff_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            initialize_control_state(config)
            callback_entered = threading.Event()
            allow_pause_write = threading.Event()
            claim_started = threading.Event()
            claim_acquired = threading.Event()

            def publish_pause() -> None:
                callback_entered.set()
                self.assertTrue(allow_pause_write.wait(2.0))

            def create() -> dict[str, object]:
                return create_auto_remediation_campaign(
                    config,
                    campaign_key="create-claim-race",
                    parameters={"strategy_version": "canary-once-v1"},
                    on_running=publish_pause,
                )

            def claim_snapshot() -> dict[str, object] | None:
                claim_started.set()
                with auto_remediation_claim_guard(config) as campaign:
                    claim_acquired.set()
                    return campaign

            with ThreadPoolExecutor(max_workers=2) as executor:
                create_future = executor.submit(create)
                self.assertTrue(callback_entered.wait(2.0))
                claim_future = executor.submit(claim_snapshot)
                self.assertTrue(claim_started.wait(2.0))
                self.assertFalse(claim_acquired.wait(0.1))
                allow_pause_write.set()
                created = create_future.result(timeout=2.0)
                observed = claim_future.result(timeout=2.0)

            self.assertIsNotNone(observed)
            self.assertEqual(observed["campaign_id"], created["campaign_id"])

    def test_daily_counts_and_samples_are_aggregated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")

            increment_daily_metric(config, "ai.completed")
            increment_daily_metric(config, "ai.completed", 2)
            record_daily_sample(config, "mikan.extract_start_latency_seconds", 4)
            record_daily_sample(config, "mikan.extract_start_latency_seconds", 10)

            connection = sqlite3.connect(root / "control_state.sqlite3")
            try:
                values = {
                    metric: float(value)
                    for metric, value in connection.execute("SELECT metric, value FROM daily_metrics")
                }
            finally:
                connection.close()
            self.assertEqual(values["ai.completed"], 3)
            self.assertEqual(values["mikan.extract_start_latency_seconds.count"], 2)
            self.assertEqual(values["mikan.extract_start_latency_seconds.sum"], 14)
            self.assertEqual(values["mikan.extract_start_latency_seconds.max"], 10)

    def test_v5_migration_backfills_durable_review_command_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "control_state.sqlite3"
            review_id = "review_" + "a" * 24
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE control_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE control_commands (
                        command_id TEXT PRIMARY KEY,
                        action TEXT NOT NULL,
                        target TEXT NOT NULL DEFAULT '',
                        parameters_json TEXT NOT NULL DEFAULT '{}',
                        idempotency_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'queued',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT NOT NULL DEFAULT '',
                        requested_at REAL NOT NULL,
                        started_at REAL NOT NULL DEFAULT 0,
                        finished_at REAL NOT NULL DEFAULT 0,
                        worker_id TEXT NOT NULL DEFAULT ''
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO control_meta VALUES ('schema_version', '3', 1)"
                )
                connection.execute(
                    """
                    INSERT INTO control_commands(
                        command_id, action, target, parameters_json, idempotency_key,
                        status, requested_at
                    ) VALUES ('cmd_test', 'review.resolve_ai', '/anime/test.mkv', ?, 'key', 'running', 10)
                    """,
                    (f'{{"review_id":"{review_id}"}}',),
                )
                connection.commit()
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")

            initialize_control_state(config)

            with closing(sqlite3.connect(database)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(control_commands)")}
                stored_review = connection.execute(
                    "SELECT review_id FROM control_commands WHERE command_id='cmd_test'"
                ).fetchone()[0]
                indexes = {row[1] for row in connection.execute("PRAGMA index_list(control_commands)")}
                version = connection.execute(
                    "SELECT value FROM control_meta WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertIn("review_id", columns)
            self.assertEqual(stored_review, review_id)
            self.assertIn("idx_control_commands_review_requested", indexes)
            self.assertEqual(version, "6")

    def test_review_items_persist_truthful_media_file_times(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "anime" / "Show" / "Season 2" / "Show - S02E01.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")

            review_id = upsert_review_item(
                config,
                kind="subtitle_quality",
                target_key=str(video),
                summary="quality review",
                diagnosis={"video": str(video)},
                candidates=[{"path": str(video), "score": 100}],
            )

            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                diagnosis_json, candidates_json = connection.execute(
                    "SELECT diagnosis_json, candidates_json FROM review_items WHERE review_id=?",
                    (review_id,),
                ).fetchone()
            media_file = json.loads(diagnosis_json)["media_file"]
            candidate_file = json.loads(candidates_json)[0]["file_info"]
            self.assertGreater(media_file["timestamp"], 0)
            self.assertIn(media_file["kind"], {"created", "modified"})
            self.assertEqual(media_file["size"], 5)
            self.assertEqual(candidate_file, media_file)

    def test_review_candidate_rebuild_replaces_stale_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            old_path = root / "anime" / "Wrong Show" / "Season 1" / "Wrong Show - S01E05.mkv"
            new_path = root / "anime" / "Right Show" / "Season 1" / "Right Show - S01E05.mkv"

            review_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="release:5",
                canonical_key="target:release:5",
                summary="target review",
                candidates=[{"path": str(old_path), "score": 100}],
            )
            rebuilt_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="release:5",
                canonical_key="target:release:5",
                summary="target review",
                candidates=[{"path": str(new_path), "score": 2000}],
                replace_candidates=True,
            )

            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                candidates_json = connection.execute(
                    "SELECT candidates_json FROM review_items WHERE review_id=?",
                    (review_id,),
                ).fetchone()[0]
            self.assertEqual(rebuilt_id, review_id)
            self.assertEqual(
                [candidate["path"] for candidate in json.loads(candidates_json)],
                [str(new_path)],
            )

    def test_review_candidate_upsert_still_merges_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            first_path = root / "anime" / "Show" / "Season 1" / "Show - S01E01.mkv"
            second_path = root / "anime" / "Show" / "Season 2" / "Show - S02E01.mkv"

            review_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="release:1",
                canonical_key="target:release:1",
                summary="target review",
                candidates=[{"path": str(first_path), "score": 100}],
            )
            upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="release:1",
                canonical_key="target:release:1",
                summary="target review",
                candidates=[{"path": str(second_path), "score": 200}],
            )

            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                candidates_json = connection.execute(
                    "SELECT candidates_json FROM review_items WHERE review_id=?",
                    (review_id,),
                ).fetchone()[0]
            self.assertEqual(
                {candidate["path"] for candidate in json.loads(candidates_json)},
                {str(first_path), str(second_path)},
            )

    def test_open_ai_quality_review_targets_are_compact_grouped_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            target = str(root / "anime" / "Show" / "Season 1" / "Show - S01E01.mkv")
            first = upsert_review_item(
                config,
                kind="subtitle_quality",
                target_key=target,
                summary="subtitle quality",
            )
            second = upsert_review_item(
                config,
                kind="asr_quality",
                target_key=target,
                summary="asr quality",
            )
            upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="release:ambiguous",
                summary="must stay manual",
            )
            resolved_target = str(root / "anime" / "Old.mkv")
            resolved = upsert_review_item(
                config,
                kind="subtitle_quality",
                target_key=resolved_target,
                summary="already resolved",
            )
            self.assertTrue(resolve_review_item(config, resolved, {"reason": "test"}))

            targets = list_open_ai_quality_review_targets(config)

            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["target_key"], target)
            self.assertEqual(targets[0]["review_count"], 2)
            self.assertGreater(targets[0]["latest_review_at"], 0)
            self.assertNotEqual(first, second)

    def test_review_autopilot_candidates_are_once_per_policy_and_operator_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            first_video = root / "anime" / "Show" / "Season 1" / "Show - S01E01.mkv"
            second_video = root / "anime" / "Show" / "Season 1" / "Show - S01E02.mkv"
            first = upsert_review_item(
                config,
                kind="asr_quality",
                target_key=str(first_video),
                summary="first",
                diagnosis={"video": str(first_video)},
                candidates=[{"action": "ai.retranscribe"}],
            )
            second = upsert_review_item(
                config,
                kind="asr_quality",
                target_key=str(second_video),
                summary="second",
                diagnosis={"video": str(second_video)},
                candidates=[{"action": "ai.retranscribe"}],
            )
            prefix = "review-autopilot:asr-v1:review.resolve_ai:"

            enqueue_command(
                config,
                action="review.resolve_ai",
                target=str(first_video),
                parameters={"review_id": first},
                idempotency_key=f"{prefix}{first}",
            )
            enqueue_command(
                config,
                action="review.resolve_ai",
                target=str(second_video),
                parameters={"review_id": second},
                idempotency_key="operator-command",
            )

            candidates = list_open_review_autopilot_candidates(
                config,
                kind="asr_quality",
                idempotency_prefix=prefix,
            )
            latest = latest_review_autopilot_command(
                config,
                idempotency_prefix=prefix,
            )

            self.assertEqual(candidates, [])
            self.assertEqual(latest["review_id"], first)
            self.assertEqual(latest["parameters"]["review_id"], first)

    def test_review_autopilot_followup_requires_completed_prior_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            video = root / "anime" / "Show" / "Season 1" / "Show - S01E03.mkv"
            review_id = upsert_review_item(
                config,
                kind="asr_quality",
                target_key=str(video),
                summary="followup",
                diagnosis={"video": str(video)},
                candidates=[{"action": "ai.retranscribe"}],
            )
            first_prefix = "review-autopilot:asr-v1:review.resolve_ai:"
            followup_prefix = "review-autopilot:translation-v1:review.resolve_ai:"

            before = list_open_review_autopilot_candidates(
                config,
                kind="asr_quality",
                idempotency_prefix=followup_prefix,
                required_completed_prefix=first_prefix,
            )
            command = enqueue_command(
                config,
                action="review.resolve_ai",
                target=str(video),
                parameters={
                    "review_id": review_id,
                    "expected_failure_revision": "revision-1",
                },
                idempotency_key=f"{first_prefix}{review_id}:revision-1",
            )
            claimed = claim_next_command(config, worker_id="test-worker")
            self.assertEqual(claimed.command_id, command["command_id"])
            finish_command(config, command["command_id"], result={"queued": True})
            after = list_open_review_autopilot_candidates(
                config,
                kind="asr_quality",
                idempotency_prefix=followup_prefix,
                required_completed_prefix=first_prefix,
            )

            self.assertEqual(before, [])
            self.assertEqual([item["review_id"] for item in after], [review_id])

    def test_revision_scoped_review_autopilot_is_bounded_and_legacy_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                control_state_path="control_state.sqlite3",
            )
            video = root / "Anime.mkv"
            review_id = upsert_review_item(
                config,
                kind="asr_quality",
                target_key=str(video),
                summary="bounded revision retries",
                diagnosis={"video": str(video)},
                candidates=[{"action": "ai.retranscribe"}],
            )
            prefix = "review-autopilot:asr-full-v1:review.resolve_ai:"

            legacy = enqueue_command(
                config,
                action="review.resolve_ai",
                target=str(video),
                parameters={
                    "review_id": review_id,
                    "expected_failure_revision": "revision-1",
                },
                idempotency_key=f"{prefix}{review_id}",
            )
            claimed = claim_next_command(config, worker_id="test-worker-1")
            self.assertEqual(claimed.command_id, legacy["command_id"])
            finish_command(config, legacy["command_id"], result={"queued": True})

            candidates = list_open_review_autopilot_candidates(
                config,
                kind="asr_quality",
                idempotency_prefix=prefix,
                allow_revision_scoped_attempts=True,
            )
            self.assertEqual([item["review_id"] for item in candidates], [review_id])
            self.assertFalse(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=prefix,
                    review_id=review_id,
                    failure_revision="revision-1",
                    max_attempts=3,
                )
            )

            for attempt, revision in enumerate(("revision-2", "revision-3"), start=2):
                self.assertTrue(
                    review_autopilot_revision_attempt_allowed(
                        config,
                        idempotency_prefix=prefix,
                        review_id=review_id,
                        failure_revision=revision,
                        max_attempts=3,
                    )
                )
                command = enqueue_command(
                    config,
                    action="review.resolve_ai",
                    target=str(video),
                    parameters={
                        "review_id": review_id,
                        "expected_failure_revision": revision,
                    },
                    idempotency_key=f"{prefix}{review_id}:{revision}",
                )
                claimed = claim_next_command(
                    config,
                    worker_id=f"test-worker-{attempt}",
                )
                self.assertEqual(claimed.command_id, command["command_id"])
                finish_command(config, command["command_id"], result={"queued": True})

            self.assertFalse(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=prefix,
                    review_id=review_id,
                    failure_revision="revision-4",
                    max_attempts=3,
                )
            )

    def test_revision_scoped_review_autopilot_still_excludes_operator_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                control_state_path="control_state.sqlite3",
            )
            video = root / "Anime.mkv"
            review_id = upsert_review_item(
                config,
                kind="asr_quality",
                target_key=str(video),
                summary="operator owns review",
            )
            prefix = "review-autopilot:asr-full-v1:review.resolve_ai:"
            enqueue_command(
                config,
                action="review.resolve_ai",
                target=str(video),
                parameters={"review_id": review_id},
                idempotency_key="operator-command",
            )

            candidates = list_open_review_autopilot_candidates(
                config,
                kind="asr_quality",
                idempotency_prefix=prefix,
                allow_revision_scoped_attempts=True,
            )
            self.assertEqual(candidates, [])
            self.assertFalse(
                review_autopilot_revision_attempt_allowed(
                    config,
                    idempotency_prefix=prefix,
                    review_id=review_id,
                    failure_revision="revision-1",
                    max_attempts=3,
                )
            )

    def test_retry_failed_command_requeues_only_the_identical_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(
                work_path=root,
                control_state_path="control_state.sqlite3",
            )
            review_id = "review_" + "f" * 24
            parameters = {
                "review_id": review_id,
                "expected_failure_revision": "revision-1",
            }
            key = f"review-autopilot:selective:{review_id}:revision-1"
            created = enqueue_command(
                config,
                action="review.resolve_ai",
                target=str(root / "Anime.mkv"),
                parameters=parameters,
                idempotency_key=key,
            )
            claimed = claim_next_command(config, worker_id="test-worker")
            self.assertEqual(claimed.command_id, created["command_id"])
            finish_command(config, created["command_id"], error="video busy")

            with self.assertRaisesRegex(ValueError, "different command payload"):
                enqueue_command(
                    config,
                    action="review.resolve_ai",
                    target=str(root / "Anime.mkv"),
                    parameters={**parameters, "expected_failure_revision": "revision-2"},
                    idempotency_key=key,
                    retry_failed=True,
                )

            retried = enqueue_command(
                config,
                action="review.resolve_ai",
                target=str(root / "Anime.mkv"),
                parameters=parameters,
                idempotency_key=key,
                retry_failed=True,
            )
            self.assertEqual(retried["command_id"], created["command_id"])
            self.assertEqual(retried["status"], "queued")
            claimed_again = claim_next_command(config, worker_id="test-worker-2")
            self.assertEqual(claimed_again.command_id, created["command_id"])

    def test_target_review_autopilot_retry_is_bounded_to_three_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            review_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="torrent:" + "a" * 40,
                summary="target review",
            )
            prefix = "review-autopilot:target-v2:review.auto_rebuild_target_candidates:"

            for attempt in range(1, 4):
                self.assertEqual(
                    next_review_autopilot_retry_attempt(
                        config,
                        idempotency_prefix=prefix,
                        review_id=review_id,
                        max_attempts=3,
                    ),
                    attempt,
                )
                command = enqueue_command(
                    config,
                    action="review.auto_rebuild_target_candidates",
                    target=review_id,
                    parameters={"review_id": review_id, "attempt": attempt},
                    idempotency_key=f"{prefix}{review_id}:attempt-{attempt}",
                )
                claimed = claim_next_command(config, worker_id=f"worker-{attempt}")
                self.assertEqual(claimed.command_id, command["command_id"])
                finish_command(config, command["command_id"], error="temporary source error")

            self.assertIsNone(
                next_review_autopilot_retry_attempt(
                    config,
                    idempotency_prefix=prefix,
                    review_id=review_id,
                    max_attempts=3,
                )
            )

    def test_sibling_target_review_with_active_command_remains_open(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            torrent_hash = "b" * 40
            primary_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="primary",
                canonical_key="torrent:" + torrent_hash,
                summary="primary",
                diagnosis={"torrent_hash": torrent_hash},
            )
            sibling_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="sibling",
                canonical_key="torrent:" + torrent_hash,
                summary="sibling",
                diagnosis={"torrent_hash": torrent_hash},
            )
            enqueue_command(
                config,
                action="review.resolve_target",
                target=sibling_id,
                parameters={"review_id": sibling_id},
                idempotency_key="operator-sibling-command",
            )

            resolved = resolve_sibling_target_reviews(
                config,
                torrent_hash=torrent_hash,
                exclude_review_id=primary_id,
                resolution={"source_id": "1234"},
            )

            self.assertEqual(resolved, [])
            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                status = connection.execute(
                    "SELECT status FROM review_items WHERE review_id=?",
                    (sibling_id,),
                ).fetchone()[0]
            self.assertEqual(status, "open")

    def test_sibling_target_review_rechecks_command_ownership_at_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = SimpleNamespace(work_path=root, control_state_path="control_state.sqlite3")
            torrent_hash = "c" * 40
            primary_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="primary-race",
                canonical_key="torrent:" + torrent_hash,
                summary="primary",
                diagnosis={"torrent_hash": torrent_hash},
            )
            sibling_id = upsert_review_item(
                config,
                kind="target_ambiguity",
                target_key="sibling-race",
                canonical_key="torrent:" + torrent_hash,
                summary="sibling",
                diagnosis={"torrent_hash": torrent_hash},
            )
            original_loads = json.loads
            inserted = False

            def enqueue_during_resolution(payload):
                nonlocal inserted
                value = original_loads(payload)
                if not inserted:
                    inserted = True
                    enqueue_command(
                        config,
                        action="review.resolve_target",
                        target=sibling_id,
                        parameters={"review_id": sibling_id},
                        idempotency_key="operator-race-command",
                    )
                return value

            with patch("control_state.json.loads", side_effect=enqueue_during_resolution):
                resolved = resolve_sibling_target_reviews(
                    config,
                    torrent_hash=torrent_hash,
                    exclude_review_id=primary_id,
                    resolution={"source_id": "1234"},
                )

            self.assertEqual(resolved, [])
            with closing(sqlite3.connect(root / "control_state.sqlite3")) as connection:
                status = connection.execute(
                    "SELECT status FROM review_items WHERE review_id=?",
                    (sibling_id,),
                ).fetchone()[0]
            self.assertEqual(status, "open")


if __name__ == "__main__":
    unittest.main()
