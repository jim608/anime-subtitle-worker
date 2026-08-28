from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from series_metadata import (
    SeriesMetadataStore,
    SeriesProfile,
    stable_series_id,
    season_number_for_video,
    series_root_for_video,
)


class SeriesMetadataStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.series = root / "Example Anime"
        self.season = self.series / "Season 2"
        self.season.mkdir(parents=True)
        self.video = self.season / "Example Anime - S02E01.mkv"
        self.video.touch()
        self.store = SeriesMetadataStore(root / "series.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_series_and_season_are_derived_from_video_path(self) -> None:
        self.assertEqual(series_root_for_video(self.video), self.series)
        self.assertEqual(season_number_for_video(self.video), 2)

    def test_locked_manual_profile_is_not_overwritten_by_automatic_refresh(self) -> None:
        manual = self.store.upsert_profile(
            SeriesProfile(
                local_path=str(self.series),
                canonical_title="人工名稱",
                provider="anilist",
                provider_id="123",
                match_confidence=1,
                match_source="manual",
                locked=True,
            )
        )
        refreshed = self.store.upsert_profile(
            SeriesProfile(
                local_path=str(self.series),
                canonical_title="Wrong Automatic Name",
                provider="anilist",
                provider_id="999",
                match_confidence=0.8,
                match_source="automatic",
            )
        )
        self.assertEqual(refreshed.canonical_title, manual.canonical_title)
        self.assertEqual(refreshed.provider_id, "123")
        self.assertTrue(refreshed.locked)

    def test_series_glossary_supports_seed_override_and_delete(self) -> None:
        self.store.upsert_profile(SeriesProfile(local_path=str(self.series), canonical_title="Example"))
        self.assertEqual(self.store.seed_terms(self.series, ["山田", "山田", "東京"]), 2)
        self.assertEqual(self.store.glossary(self.series), {})
        self.store.upsert_glossary_term(self.series, "山田", "山田同學", term_type="name")
        self.assertEqual(self.store.glossary(self.series), {"山田": "山田同學"})
        self.assertTrue(self.store.delete_glossary_term(self.series, "山田"))
        self.assertEqual(self.store.glossary(self.series), {})

    def test_mikan_identity_is_attached_without_replacing_manual_title(self) -> None:
        self.store.set_manual_match(
            self.series,
            provider="anilist",
            provider_id="42",
            canonical_title="Manual Title",
        )
        profile = self.store.set_mikan_identity(
            self.series,
            bangumi_id=3669,
            title="Mikan Title",
            confidence=0.95,
            aliases=["Alias"],
        )
        self.assertEqual(profile.canonical_title, "Manual Title")
        self.assertEqual(profile.provider_id, "42")
        self.assertEqual(profile.mikan_bangumi_id, 3669)
        self.assertIn("Mikan Title", profile.aliases)

    def test_stable_series_id_is_persisted_and_indexed(self) -> None:
        self.store.upsert_profile(SeriesProfile(local_path=str(self.series), canonical_title="Example"))
        key = str(self.series.resolve()).casefold()
        expected = stable_series_id(key)
        row = self.store.conn.execute(
            "SELECT series_id FROM series_profiles WHERE local_path_key = ?",
            (key,),
        ).fetchone()
        self.assertEqual(row[0], expected)
        self.assertIsNotNone(self.store.get_by_series_id(expected))
        indexes = {
            str(item[1])
            for item in self.store.conn.execute("PRAGMA index_list(series_profiles)").fetchall()
        }
        self.assertIn("idx_series_profiles_series_id", indexes)

    def test_profiles_can_be_listed_by_verified_mikan_identity(self) -> None:
        other_series = self.series.parent / "Other Anime"
        self.store.upsert_profile(SeriesProfile(
            local_path=str(self.series),
            canonical_title="Example",
            mikan_bangumi_id=2911,
        ))
        self.store.upsert_profile(SeriesProfile(
            local_path=str(other_series),
            canonical_title="Other",
            mikan_bangumi_id=260,
        ))

        profiles = self.store.list_by_mikan_ids([2911, "invalid", -1])

        self.assertEqual([profile.canonical_title for profile in profiles], ["Example"])

    def test_cover_artwork_fields_are_persisted_by_additive_schema(self) -> None:
        profile = self.store.upsert_profile(SeriesProfile(
            local_path=str(self.series),
            canonical_title="Example",
            cover_image_url="https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/example.jpg",
            cover_image_color="#223344",
            cover_image_cache_key="series_0123456789abcdef01234567.jpg",
            cover_image_updated_at=1234.5,
        ))

        self.assertEqual(profile.cover_image_color, "#223344")
        self.assertEqual(profile.cover_image_cache_key, "series_0123456789abcdef01234567.jpg")
        columns = {
            str(item[1])
            for item in self.store.conn.execute("PRAGMA table_info(series_profiles)").fetchall()
        }
        self.assertTrue({
            "cover_image_url",
            "cover_image_color",
            "cover_image_cache_key",
            "cover_image_updated_at",
        }.issubset(columns))

    def test_schema_upgrade_creates_verified_online_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE series_metadata_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO series_metadata_meta VALUES ('schema_version', '1')")
                connection.commit()
            finally:
                connection.close()

            upgraded = SeriesMetadataStore(database)
            upgraded.close()

            backups = list((root / "sqlite_migration_backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertTrue(backups[0].with_suffix(".sqlite3.sha256").is_file())


if __name__ == "__main__":
    unittest.main()
