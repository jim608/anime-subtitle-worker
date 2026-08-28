from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest

from mikan_cache_store import MikanIndexedCache, ensure_mikan_cache_tables
from mikan_fallback_sources import FALLBACK_CACHE_VERSION, _load_cache as load_fallback_cache
from mikan_matcher import _load_cache as load_auto_match_cache


class MikanIndexedCacheTest(unittest.TestCase):
    def _config(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            work_path=root,
            mikan_pending_path="mikan_pending.json",
            mikan_sqlite_authoritative_state=True,
        )

    def test_imports_json_once_archives_it_and_uses_indexed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "mikan_auto_matches.json"
            legacy.write_text(json.dumps({"series-a": {"status": "miss"}}), encoding="utf-8")
            config = self._config(root)

            loaded = load_auto_match_cache(legacy, config=config)

            self.assertEqual(loaded, {"series-a": {"status": "miss"}})
            self.assertFalse(legacy.exists())
            self.assertTrue((root / "mikan_auto_matches.legacy-readonly.json").is_file())
            connection = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                row = connection.execute(
                    """
                    SELECT value_json FROM mikan_cache_entries
                    WHERE namespace = 'auto_match' AND cache_key = 'series-a'
                    """
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(row)
            self.assertEqual(json.loads(str(row[0])), {"status": "miss"})

            # Even a stale file recreated at the old path is never reparsed on
            # subsequent starts once the SQLite namespace marker exists.
            legacy.write_text("not-json", encoding="utf-8")
            self.assertEqual(load_auto_match_cache(legacy, config=config), loaded)

    def test_fallback_entries_are_imported_without_top_level_json_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "mikan_fallback_sources.json"
            legacy.write_text(
                json.dumps(
                    {
                        "version": FALLBACK_CACHE_VERSION,
                        "entries": {
                            "lookup-a": {"fetched_at": 123.0, "complete": True, "releases": []},
                            "lookup-b": {"fetched_at": 456.0, "complete": False, "releases": []},
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = self._config(root)

            loaded = load_fallback_cache(legacy, config=config)

            self.assertEqual(loaded["version"], FALLBACK_CACHE_VERSION)
            self.assertEqual(set(loaded["entries"]), {"lookup-a", "lookup-b"})
            connection = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM mikan_cache_entries WHERE namespace = 'fallback_sources'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 2)

    def test_replace_all_is_atomic_and_schema_is_additive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self._config(root)
            store = MikanIndexedCache(
                config,
                namespace="test",
                legacy_path=root / "legacy.json",
                schema_version=1,
            )
            store.initialize_if_needed({"old": {"value": 1}})
            store.replace_all({"new": {"value": 2}})

            self.assertEqual(store.load_all(), {"new": {"value": 2}})
            upgraded = MikanIndexedCache(
                config,
                namespace="test",
                legacy_path=root / "legacy.json",
                schema_version=2,
            )
            self.assertFalse(upgraded.initialized())
            upgraded.initialize_if_needed({})
            self.assertEqual(upgraded.load_all(), {})
            connection = sqlite3.connect(root / "mikan_state.sqlite3")
            try:
                ensure_mikan_cache_tables(connection)
                ensure_mikan_cache_tables(connection)
                connection.commit()
                result = connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(result, "ok")


if __name__ == "__main__":
    unittest.main()
