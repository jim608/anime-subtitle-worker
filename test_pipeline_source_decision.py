from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pipeline_state import (
    PIPELINE_SCHEMA_VERSION,
    PipelineJobStore,
    PipelineStateConflict,
    PipelineStateError,
    ensure_pipeline_state_schema,
)


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class PipelineSourceDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.media = self.root / "episode.mkv"
        self.media.write_bytes(b"immutable-video")
        self.database = self.root / "scanner_state.sqlite3"
        self.store = PipelineJobStore(self.database)
        stat = self.media.stat()
        self.store.observe_ingest(
            self.media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type="created",
            state="STABILIZING",
            evidence={"test": True},
            confidence=1.0,
        )
        observed = self.store.observe_ingest(
            self.media,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            event_type="closed",
            state="QUEUED",
            evidence={"test": True},
            confidence=1.0,
        )
        job = self.store.get_job(str(observed["job_id"]))
        assert job is not None
        self.job = job

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _attempt(self) -> dict:
        return self.store.start_stage_attempt(
            str(self.job["job_id"]),
            "SUBTITLE_DETECTION",
            inputs={"contract": "m2-source-decision"},
            retry_limit=1,
            reason_code="source_analysis_started",
            evidence={"test": True},
            confidence=1.0,
        )

    def _decision(self, *, reason_code: str = "complete_zh_tw_selected") -> dict:
        selected = {
            "kind": "subtitle",
            "index": 2,
            "score": 0.97,
            "selected": True,
            "language": "zh-TW",
        }
        return {
            "strategy": "USE_EXISTING_ZH_TW",
            "confidence": 0.97,
            "reason_code": reason_code,
            "evidence": {"content_language": "zh-TW", "dialogue_complete": True},
            "selected_subtitle_track": dict(selected),
            "selected_audio_track": None,
            "candidates": [selected],
            "unselected_reasons": [],
        }

    def _context(self) -> dict:
        return {
            "input_identity": {
                "canonical_path": str(self.job["canonical_path"]),
                "media_revision": str(self.job["media_revision"]),
                "inventory_revision": "inventory-v1",
            },
            "media_revision": str(self.job["media_revision"]),
            "source_fingerprint": str(self.job["media_fingerprint"]),
            "analyzer_version": "source-analyzer-v1",
            "decision_schema_version": "1",
            "decision_version": "source-selection-v1",
            "config_fingerprint": _sha256("decision-config-v1"),
            "candidate_fingerprint": _sha256("candidate-inventory-v1"),
        }

    def _persist(self, attempt: dict | None = None, **overrides) -> dict:
        selected_attempt = attempt or self._attempt()
        context = self._context()
        context.update(overrides.pop("context", {}))
        decision = overrides.pop("decision", self._decision())
        return self.store.persist_source_decision(
            str(self.job["job_id"]),
            stage_attempt_id=str(selected_attempt["stage_attempt_id"]),
            decision=decision,
            idempotency_key=overrides.pop("idempotency_key", "decision:test:1"),
            created_at=overrides.pop("created_at", 1234.5),
            **context,
            **overrides,
        )

    def _reuse(self, **overrides):
        context = self._context()
        context.update(overrides)
        return self.store.reusable_source_decision(
            str(self.job["job_id"]),
            expected_identity=context["input_identity"],
            expected_media_revision=context["media_revision"],
            expected_source_fingerprint=context["source_fingerprint"],
            expected_analyzer_version=context["analyzer_version"],
            expected_decision_schema_version=context["decision_schema_version"],
            expected_decision_version=context["decision_version"],
            expected_config_fingerprint=context["config_fingerprint"],
            expected_candidate_fingerprint=context["candidate_fingerprint"],
            with_reason=True,
        )

    def test_v1_database_migrates_even_when_old_sentinel_exists(self) -> None:
        job_id = str(self.job["job_id"])
        self.store.commit()
        self.store.close()
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='pipeline_operation_idempotency'"
                ).fetchone()
            )
            connection.execute("DROP TABLE pipeline_source_decisions")
            connection.execute(
                "UPDATE pipeline_schema_meta SET value='1' WHERE key='schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        self.store = PipelineJobStore(self.database)
        self.assertEqual(
            str(PIPELINE_SCHEMA_VERSION),
            self.store._conn.execute(
                "SELECT value FROM pipeline_schema_meta WHERE key='schema_version'"
            ).fetchone()[0],
        )
        self.assertIsNotNone(
            self.store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_source_decisions'"
            ).fetchone()
        )
        self.assertIsNotNone(
            self.store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='idx_pipeline_source_decisions_job_created'"
            ).fetchone()
        )
        self.assertIsNotNone(self.store.get_job(job_id))
        self.store.commit()
        self.store.close()
        self.store = PipelineJobStore(self.database)
        self.assertEqual(0, len(self.store.list_source_decisions(job_id)))

    def test_future_schema_is_rejected_without_downgrade(self) -> None:
        self.store._conn.execute(
            "UPDATE pipeline_schema_meta SET value='99' WHERE key='schema_version'"
        )
        self.store.commit()
        self.store.close()
        with self.assertRaises(PipelineStateError):
            PipelineJobStore(self.database)
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                "99",
                connection.execute(
                    "SELECT value FROM pipeline_schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
            )
            connection.execute(
                "UPDATE pipeline_schema_meta SET value=? WHERE key='schema_version'",
                (str(PIPELINE_SCHEMA_VERSION),),
            )
            connection.commit()
        finally:
            connection.close()
        self.store = PipelineJobStore(self.database)

    def test_current_schema_check_does_not_commit_caller_transaction(self) -> None:
        self.store.commit()
        self.store._conn.execute(
            "UPDATE pipeline_jobs SET updated_at=updated_at WHERE job_id=?",
            (str(self.job["job_id"]),),
        )
        self.assertTrue(self.store.in_transaction)
        facade = PipelineJobStore.from_connection(self.store._conn)
        try:
            self.assertTrue(self.store.in_transaction)
            self.assertTrue(facade.in_transaction)
        finally:
            facade.close()
        self.store.rollback()

    def test_v1_migration_is_atomic_inside_caller_transaction(self) -> None:
        job_id = str(self.job["job_id"])
        self.store.commit()
        self.store.close()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DROP TABLE pipeline_source_decisions")
            connection.execute(
                "UPDATE pipeline_schema_meta SET value='1' WHERE key='schema_version'"
            )
            connection.commit()
            connection.execute(
                "UPDATE pipeline_jobs SET terminal_reason_code='pending' WHERE job_id=?",
                (job_id,),
            )
            self.assertTrue(connection.in_transaction)
            facade = PipelineJobStore.from_connection(connection)
            try:
                self.assertTrue(connection.in_transaction)
                self.assertEqual(
                    "2",
                    connection.execute(
                        "SELECT value FROM pipeline_schema_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='pipeline_source_decisions'"
                    ).fetchone()
                )
            finally:
                facade.close()
            connection.rollback()
            self.assertEqual(
                "1",
                connection.execute(
                    "SELECT value FROM pipeline_schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='pipeline_source_decisions'"
                ).fetchone()
            )
            self.assertEqual(
                "",
                connection.execute(
                    "SELECT terminal_reason_code FROM pipeline_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
            )
        finally:
            connection.close()
        self.store = PipelineJobStore(self.database)

    def test_fresh_schema_install_does_not_commit_caller_transaction(self) -> None:
        fresh_database = self.root / "fresh-caller.sqlite3"
        connection = sqlite3.connect(fresh_database)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("CREATE TABLE caller_data(value TEXT NOT NULL)")
            connection.commit()
            connection.execute("INSERT INTO caller_data(value) VALUES('pending')")
            self.assertTrue(connection.in_transaction)

            facade = PipelineJobStore.from_connection(connection)
            try:
                self.assertTrue(connection.in_transaction)
            finally:
                facade.close()
            connection.rollback()
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM caller_data").fetchone()[0])
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_jobs'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_caller_transaction_with_foreign_keys_off_fails_closed(self) -> None:
        database = self.root / "foreign-keys-off.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE caller_data(value TEXT NOT NULL)")
            connection.commit()
            connection.execute("INSERT INTO caller_data(value) VALUES('pending')")
            self.assertEqual(0, connection.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertTrue(connection.in_transaction)

            with self.assertRaises(PipelineStateError):
                PipelineJobStore.from_connection(connection)
            self.assertTrue(connection.in_transaction)
            connection.rollback()
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM caller_data").fetchone()[0])
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_jobs'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_partial_v1_schema_fails_closed_without_false_upgrade(self) -> None:
        broken_database = self.root / "broken-v1.sqlite3"
        connection = sqlite3.connect(broken_database)
        try:
            connection.execute(
                "CREATE TABLE pipeline_schema_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT INTO pipeline_schema_meta(key, value, updated_at) "
                "VALUES('schema_version', '1', 1)"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(PipelineStateError):
            PipelineJobStore(broken_database)
        connection = sqlite3.connect(broken_database)
        try:
            self.assertEqual(
                "1",
                connection.execute(
                    "SELECT value FROM pipeline_schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='pipeline_source_decisions'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_current_schema_missing_m1_object_fails_closed(self) -> None:
        self.store.commit()
        self.store.close()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP INDEX idx_pipeline_jobs_state_updated")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(PipelineStateError):
            PipelineJobStore(self.database)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "CREATE INDEX idx_pipeline_jobs_state_updated "
                "ON pipeline_jobs(state, updated_at)"
            )
            connection.commit()
        finally:
            connection.close()
        self.store = PipelineJobStore(self.database)

    def test_v2_migration_without_outer_transaction_rolls_back_on_failure(self) -> None:
        migration_database = self.root / "migration-failure.sqlite3"
        bootstrap = PipelineJobStore(migration_database)
        bootstrap.close()
        connection = sqlite3.connect(migration_database)
        try:
            connection.execute("DROP TABLE pipeline_source_decisions")
            connection.execute(
                "UPDATE pipeline_schema_meta SET value='1' WHERE key='schema_version'"
            )
            connection.commit()

            def deny_version_update(action, arg1, _arg2, _database, _trigger):
                if action == sqlite3.SQLITE_UPDATE and arg1 == "pipeline_schema_meta":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_version_update)
            with self.assertRaises(sqlite3.DatabaseError):
                ensure_pipeline_state_schema(connection)
            connection.set_authorizer(None)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(
                "1",
                connection.execute(
                    "SELECT value FROM pipeline_schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='pipeline_source_decisions'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='index' AND name='idx_pipeline_source_decisions_job_created'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='index' AND name='uq_pipeline_source_decisions_stage_attempt'"
                ).fetchone()
            )
        finally:
            connection.set_authorizer(None)
            connection.close()

    def test_v2_required_unique_index_shape_is_verified(self) -> None:
        cases = {
            "wrong_table": (
                "CREATE TABLE bogus_index_target(x TEXT)",
                "CREATE UNIQUE INDEX uq_pipeline_source_decisions_stage_attempt "
                "ON bogus_index_target(x)",
            ),
            "non_unique": (
                None,
                "CREATE INDEX uq_pipeline_source_decisions_stage_attempt "
                "ON pipeline_source_decisions(stage_attempt_id)",
            ),
            "wrong_column": (
                None,
                "CREATE UNIQUE INDEX uq_pipeline_source_decisions_stage_attempt "
                "ON pipeline_source_decisions(job_id)",
            ),
            "partial": (
                None,
                "CREATE UNIQUE INDEX uq_pipeline_source_decisions_stage_attempt "
                "ON pipeline_source_decisions(stage_attempt_id) WHERE 0",
            ),
        }
        for label, (setup_sql, index_sql) in cases.items():
            with self.subTest(label=label):
                database = self.root / f"bad-index-{label}.sqlite3"
                bootstrap = PipelineJobStore(database)
                bootstrap.close()
                connection = sqlite3.connect(database)
                try:
                    connection.execute("DROP INDEX uq_pipeline_source_decisions_stage_attempt")
                    connection.execute(
                        "UPDATE pipeline_schema_meta SET value='1' WHERE key='schema_version'"
                    )
                    if setup_sql is not None:
                        connection.execute(setup_sql)
                    connection.execute(index_sql)
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaises(PipelineStateError):
                    PipelineJobStore(database)
                connection = sqlite3.connect(database)
                try:
                    self.assertEqual(
                        "1",
                        connection.execute(
                            "SELECT value FROM pipeline_schema_meta WHERE key='schema_version'"
                        ).fetchone()[0],
                    )
                finally:
                    connection.close()

    def test_v2_table_with_all_columns_but_missing_constraints_fails_closed(self) -> None:
        self.store.commit()
        self.store.close()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TABLE pipeline_source_decisions")
            connection.execute(
                """
                CREATE TABLE pipeline_source_decisions (
                    decision_id TEXT, job_id TEXT, stage_attempt_id TEXT,
                    input_identity_json TEXT, input_identity_sha256 TEXT,
                    media_revision TEXT, source_fingerprint TEXT, analyzer_version TEXT,
                    decision_schema_version TEXT, decision_version TEXT,
                    config_fingerprint TEXT, candidate_fingerprint TEXT,
                    strategy TEXT, confidence REAL, reason_code TEXT,
                    decision_json TEXT, decision_sha256 TEXT,
                    idempotency_key TEXT, created_at REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX idx_pipeline_source_decisions_job_created "
                "ON pipeline_source_decisions(job_id, created_at, decision_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX uq_pipeline_source_decisions_stage_attempt "
                "ON pipeline_source_decisions(stage_attempt_id)"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(PipelineStateError):
            PipelineJobStore(self.database)

    def test_v2_strategy_check_requires_the_exact_supported_set(self) -> None:
        replacements = {
            "missing_required": lambda sql: sql.replace("'NORMALIZE_ZH_HANT', ", ""),
            "extra_value": lambda sql: sql.replace(
                "'USE_EXISTING_ZH_TW'",
                "'USE_EXISTING_ZH_TW', 'BOGUS_STRATEGY'",
            ),
        }
        for label, transform in replacements.items():
            with self.subTest(label=label):
                database = self.root / f"bad-strategy-check-{label}.sqlite3"
                bootstrap = PipelineJobStore(database)
                bootstrap.close()
                connection = sqlite3.connect(database)
                try:
                    original = str(
                        connection.execute(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='table' AND name='pipeline_source_decisions'"
                        ).fetchone()[0]
                    )
                    altered = transform(original)
                    self.assertNotEqual(original, altered)
                    schema_version = int(
                        connection.execute("PRAGMA schema_version").fetchone()[0]
                    )
                    connection.execute("PRAGMA writable_schema=ON")
                    connection.execute(
                        "UPDATE sqlite_master SET sql=? "
                        "WHERE type='table' AND name='pipeline_source_decisions'",
                        (altered,),
                    )
                    connection.execute("PRAGMA writable_schema=OFF")
                    connection.execute(f"PRAGMA schema_version={schema_version + 1}")
                    connection.commit()
                finally:
                    connection.execute("PRAGMA writable_schema=OFF")
                    connection.close()

                with self.assertRaises(PipelineStateError):
                    PipelineJobStore(database)

    def test_persist_is_append_only_idempotent_and_checkpoints_attempt(self) -> None:
        attempt = self._attempt()
        saved = self._persist(attempt)
        self.assertEqual("USE_EXISTING_ZH_TW", saved["decision"]["strategy"])
        self.assertEqual(str(self.job["job_id"]), saved["decision"]["job_id"])
        self.assertTrue(saved["integrity_valid"])
        durable_attempt = self.store._get_attempt(str(attempt["stage_attempt_id"]))
        assert durable_attempt is not None
        self.assertTrue(durable_attempt["outputs_verified"])
        self.assertEqual(saved["decision_id"], durable_attempt["checkpoint"]["decision_id"])
        self.assertEqual(saved["decision_sha256"], durable_attempt["checkpoint"]["decision_sha256"])
        event_count_before = self.store._conn.execute(
            "SELECT COUNT(*) FROM pipeline_stage_events "
            "WHERE stage_attempt_id=? AND event_type='SOURCE_DECISION_PERSISTED'",
            (str(attempt["stage_attempt_id"]),),
        ).fetchone()[0]

        repeated = self._persist(attempt, created_at=9999.0)
        self.assertEqual(saved["decision_id"], repeated["decision_id"])
        self.assertEqual(1, len(self.store.list_source_decisions(str(self.job["job_id"]))))
        durable_repeated = self.store._get_attempt(str(attempt["stage_attempt_id"]))
        assert durable_repeated is not None
        self.assertEqual(durable_attempt["heartbeat_at"], durable_repeated["heartbeat_at"])
        self.assertEqual(durable_attempt["updated_at"], durable_repeated["updated_at"])
        self.assertEqual(
            event_count_before,
            self.store._conn.execute(
                "SELECT COUNT(*) FROM pipeline_stage_events "
                "WHERE stage_attempt_id=? AND event_type='SOURCE_DECISION_PERSISTED'",
                (str(attempt["stage_attempt_id"]),),
            ).fetchone()[0],
        )
        with self.assertRaises(PipelineStateConflict):
            self._persist(
                attempt,
                decision=self._decision(reason_code="different_result"),
            )

    def test_new_idempotency_alias_cannot_rebind_an_existing_decision(self) -> None:
        first_attempt = self._attempt()
        saved = self._persist(first_attempt)
        self.store.finish_stage_attempt(
            str(first_attempt["stage_attempt_id"]),
            "SUCCEEDED",
            reason_code="source_decision_completed",
            evidence={"decision_id": saved["decision_id"]},
            confidence=float(saved["confidence"]),
        )
        second_attempt = self._attempt()
        with self.assertRaises(PipelineStateConflict):
            self._persist(
                second_attempt,
                idempotency_key="decision:test:alias",
            )
        self.assertEqual(1, len(self.store.list_source_decisions(str(self.job["job_id"]))))

        replayed = self._persist(
            second_attempt,
            idempotency_key="decision:test:1",
        )
        self.assertEqual(saved["decision_id"], replayed["decision_id"])

    def test_cross_attempt_replay_cannot_replace_checkpoint_without_a_key(self) -> None:
        first_attempt = self._attempt()
        saved = self._persist(first_attempt, idempotency_key=None)
        self.store.finish_stage_attempt(
            str(first_attempt["stage_attempt_id"]),
            "SUCCEEDED",
            reason_code="source_decision_completed",
            evidence={"decision_id": saved["decision_id"]},
            confidence=float(saved["confidence"]),
        )
        second_attempt = self._attempt()
        replayed = self._persist(second_attempt, idempotency_key=None)
        self.assertEqual(saved["decision_id"], replayed["decision_id"])

        with self.assertRaises(PipelineStateConflict):
            self._persist(
                second_attempt,
                context={
                    "candidate_fingerprint": _sha256("candidate-inventory-v2"),
                },
                idempotency_key=None,
            )
        self.assertEqual(1, len(self.store.list_source_decisions(str(self.job["job_id"]))))
        durable_attempt = self.store._get_attempt(str(second_attempt["stage_attempt_id"]))
        assert durable_attempt is not None
        self.assertEqual(saved["decision_id"], durable_attempt["checkpoint"]["decision_id"])

    def test_input_inventory_and_decision_result_hashes_are_independent(self) -> None:
        saved = self._persist()
        candidates_json = json.dumps(
            saved["decision"]["candidates"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        results_sha256 = hashlib.sha256(candidates_json.encode("utf-8")).hexdigest()
        self.assertEqual(
            self._context()["candidate_fingerprint"],
            saved["candidate_fingerprint"],
        )
        self.assertEqual(
            results_sha256,
            saved["decision"]["candidate_results_sha256"],
        )
        self.assertNotEqual(saved["candidate_fingerprint"], results_sha256)

        payload = dict(saved["decision"])
        payload["candidates"] = [dict(payload["candidates"][0], score=0.10)]
        payload["selected_subtitle_track"] = dict(
            payload["selected_subtitle_track"],
            score=0.10,
        )
        tampered_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.store._conn.execute(
            "UPDATE pipeline_source_decisions SET decision_json=?, decision_sha256=? "
            "WHERE decision_id=?",
            (
                tampered_json,
                hashlib.sha256(tampered_json.encode("utf-8")).hexdigest(),
                str(saved["decision_id"]),
            ),
        )
        reused, reason = self._reuse()
        self.assertIsNone(reused)
        self.assertEqual(
            "source_decision_candidate_results_hash_mismatch",
            reason,
        )

    def test_restart_reuses_committed_decision_after_attempt_interruption(self) -> None:
        saved = self._persist()
        self.store.commit()
        self.store.close()
        self.store = PipelineJobStore(self.database)
        self.store.recover_interrupted_stages(recover_all_running=True)
        reused, reason = self._reuse()
        self.assertEqual("source_decision_reusable", reason)
        assert reused is not None
        self.assertEqual(saved["decision_id"], reused["decision_id"])
        self.assertEqual("INTERRUPTED", self.store._get_attempt(saved["stage_attempt_id"])["status"])
        self.assertEqual(1, len(self.store.list_source_decisions(str(self.job["job_id"]))))

    def test_successful_finish_preserves_decision_checkpoint_for_restart_reuse(self) -> None:
        attempt = self._attempt()
        saved = self._persist(attempt)
        durable_before = self.store._get_attempt(str(attempt["stage_attempt_id"]))
        assert durable_before is not None

        finished = self.store.finish_stage_attempt(
            str(attempt["stage_attempt_id"]),
            "SUCCEEDED",
            outputs={},
            outputs_verified=False,
            reason_code="source_decision_completed",
            evidence={"decision_id": saved["decision_id"]},
            confidence=float(saved["confidence"]),
        )
        self.assertEqual("SUCCEEDED", finished["status"])
        self.assertTrue(finished["outputs_verified"])
        self.assertEqual(durable_before["checkpoint"], finished["checkpoint"])
        self.assertEqual(durable_before["output"], finished["output"])

        self.store.commit()
        self.store.close()
        self.store = PipelineJobStore(self.database)
        reused, reason = self._reuse()
        self.assertEqual("source_decision_reusable", reason)
        assert reused is not None
        self.assertEqual(saved["decision_id"], reused["decision_id"])

    def test_one_attempt_cannot_persist_two_different_decisions(self) -> None:
        attempt = self._attempt()
        saved = self._persist(attempt)
        with self.assertRaises(PipelineStateConflict):
            self._persist(
                attempt,
                context={
                    "candidate_fingerprint": _sha256("candidate-inventory-v2"),
                },
                idempotency_key="decision:test:2",
            )

        decisions = self.store.list_source_decisions(str(self.job["job_id"]))
        self.assertEqual(1, len(decisions))
        self.assertEqual(saved["decision_id"], decisions[0]["decision_id"])
        reused, reason = self._reuse()
        self.assertEqual("source_decision_reusable", reason)
        assert reused is not None
        self.assertEqual(saved["decision_id"], reused["decision_id"])

    def test_every_expected_context_component_invalidates_exact_reuse(self) -> None:
        self._persist()
        changed_identity = dict(self._context()["input_identity"])
        changed_identity["inventory_revision"] = "inventory-v2"
        cases = (
            (
                {"input_identity": changed_identity},
                "source_decision_input_identity_changed",
            ),
            (
                {"media_revision": _sha256("another-media-revision")},
                "source_decision_media_revision_changed",
            ),
            (
                {"source_fingerprint": _sha256("another-source")},
                "source_decision_source_fingerprint_changed",
            ),
            (
                {"analyzer_version": "source-analyzer-v2"},
                "source_decision_analyzer_version_changed",
            ),
            (
                {"decision_schema_version": "2"},
                "source_decision_schema_version_changed",
            ),
            (
                {"decision_version": "source-selection-v2"},
                "source_decision_version_changed",
            ),
            (
                {"config_fingerprint": _sha256("decision-config-v2")},
                "source_decision_config_changed",
            ),
            (
                {"candidate_fingerprint": _sha256("candidate-inventory-v2")},
                "source_decision_candidate_fingerprint_changed",
            ),
        )
        for overrides, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                reused, reason = self._reuse(**overrides)
                self.assertIsNone(reused)
                self.assertEqual(expected_reason, reason)

    def test_corrupt_missing_and_hash_mismatch_are_fail_closed_with_reason(self) -> None:
        saved = self._persist()
        decision_id = str(saved["decision_id"])
        original_json, original_sha = self.store._conn.execute(
            "SELECT decision_json, decision_sha256 FROM pipeline_source_decisions "
            "WHERE decision_id=?",
            (decision_id,),
        ).fetchone()

        self.store._conn.execute(
            "UPDATE pipeline_source_decisions SET decision_json='{' WHERE decision_id=?",
            (decision_id,),
        )
        reused, reason = self._reuse()
        self.assertIsNone(reused)
        self.assertEqual("source_decision_corrupt", reason)
        listed = self.store.list_source_decisions(str(self.job["job_id"]))
        self.assertFalse(listed[0]["integrity_valid"])
        self.assertEqual("source_decision_corrupt", listed[0]["integrity_reason_code"])

        self.store._conn.execute(
            "UPDATE pipeline_source_decisions SET decision_json=?, decision_sha256=? "
            "WHERE decision_id=?",
            (original_json, "0" * 64, decision_id),
        )
        reused, reason = self._reuse()
        self.assertIsNone(reused)
        self.assertEqual("source_decision_hash_mismatch", reason)

        payload = json.loads(original_json)
        payload.pop("evidence")
        incomplete_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.store._conn.execute(
            "UPDATE pipeline_source_decisions SET decision_json=?, decision_sha256=? "
            "WHERE decision_id=?",
            (
                incomplete_json,
                hashlib.sha256(incomplete_json.encode("utf-8")).hexdigest(),
                decision_id,
            ),
        )
        reused, reason = self._reuse()
        self.assertIsNone(reused)
        self.assertEqual("source_decision_incomplete", reason)

        self.store._conn.execute(
            "UPDATE pipeline_source_decisions SET decision_json=?, decision_sha256=? "
            "WHERE decision_id=?",
            (original_json, original_sha, decision_id),
        )
        self.assertIsNotNone(self._reuse()[0])

        self.store._conn.execute(
            "UPDATE pipeline_source_decisions SET created_at='broken' WHERE decision_id=?",
            (decision_id,),
        )
        reused, reason = self._reuse()
        self.assertIsNone(reused)
        self.assertEqual("source_decision_incomplete", reason)

    def test_input_identity_hash_corruption_is_not_reused(self) -> None:
        saved = self._persist()
        self.store._conn.execute(
            "UPDATE pipeline_source_decisions SET input_identity_sha256=? WHERE decision_id=?",
            ("0" * 64, str(saved["decision_id"])),
        )
        reused, reason = self._reuse()
        self.assertIsNone(reused)
        self.assertEqual("source_decision_input_identity_hash_mismatch", reason)
        listed = self.store.list_source_decisions(str(self.job["job_id"]))
        self.assertEqual(
            "source_decision_input_identity_hash_mismatch",
            listed[0]["integrity_reason_code"],
        )


if __name__ == "__main__":
    unittest.main()
