"""Conservative matching of independently verified slash-separated release aliases."""
from copy import deepcopy
from dataclasses import replace
import unittest

from mikan_source import MikanRelease
from mikan_worker import _assess_release_identity, _choose_release_for_episode


class MikanReleaseAliasIdentityTest(unittest.TestCase):
    def setUp(self):
        self.mapping = {
            'bangumi_id': 4005, 'path': '/anime/KAIJU GIRL CARAMELISE',
            'title': '少女怪兽焦糖味',
            'match': ['少女怪兽焦糖味', '乙女怪獣キャラメリゼ',
                      'Otome Kaijuu Caramelise S01E07', 'Otome Kaijuu Carameliser'],
        }
        self.release = MikanRelease(
            bangumi_id=4005,
            title='[Nix-Raws] 少女怪兽焦糖味 / 乙女怪獣キャラメリゼ / Otome Kaijuu Caramelise S01E01 [CR WEB-DL 1080p AVC AAC][简繁内封]',
            episode=1, torrent_url='https://example.invalid/known.torrent',
            pub_date=None, content_length=None, source='animegarden:mikan',
            info_hash='a24e17e86b9633120b73419abaeadead68176d21',
        )

    def assessment(self, release=None, mapping=None):
        return _assess_release_identity(release or self.release, [mapping or self.mapping])

    def titled(self, title):
        return replace(self.release, title=title, series_identity='', season_number=None, identity_evidence=[])

    def test_all_three_known_aliases_are_not_a_sequel(self):
        result = self.assessment()
        self.assertTrue(result.safe, result.reason)
        self.assertEqual(result.identity_key, '4005:1')

    def test_unknown_alias_is_not_discarded(self):
        result = self.assessment(self.titled('[G] 少女怪兽焦糖味 / An Unverified Work S01E01 [CHT][MKV]'))
        self.assertFalse(result.safe)

    def test_sequel_suffix_is_not_discarded(self):
        result = self.assessment(self.titled('[G] 少女怪兽焦糖味 / Otome Kaijuu Caramelise Returns S01E01 [CHT][MKV]'))
        self.assertFalse(result.safe)

    def test_conflicting_alias_seasons_refuse(self):
        result = self.assessment(self.titled('[G] 少女怪兽焦糖味 S01 / Otome Kaijuu Caramelise S02E01 [CHT][MKV]'))
        self.assertFalse(result.safe)

    def test_conflicting_provided_identity_refuses(self):
        result = self.assessment(replace(self.release, series_identity='Unrelated Series'))
        self.assertFalse(result.safe)

    def test_mapping_season_mismatch_still_refuses(self):
        result = self.assessment(mapping={**self.mapping, 'season': 2})
        self.assertFalse(result.safe)
        self.assertIn('season_mismatch', result.reason)

    def test_provided_season_cannot_contradict_alias_title(self):
        result = self.assessment(replace(self.release, season_number=2), {**self.mapping, 'season': 2})
        self.assertFalse(result.safe)

    def test_aliases_do_not_authorize_unverified_season(self):
        mapping = {**self.mapping, 'match': ['少女怪兽焦糖味', '乙女怪獣キャラメリゼ', 'Otome Kaijuu Caramelise']}
        result = self.assessment(mapping=mapping)
        self.assertFalse(result.safe)

    def test_missing_alias_is_not_accepted(self):
        result = self.assessment(self.titled('[G] 少女怪兽焦糖味 / / Otome Kaijuu Caramelise S01E01 [CHT][MKV]'))
        self.assertFalse(result.safe)

    def test_metadata_suffix_is_not_treated_as_verified_alias(self):
        result = self.assessment(self.titled('[G] 少女怪兽焦糖味 / 乙女怪獣キャラメリゼ / Otome Kaijuu Caramelise S01E01 [HEVC / AAC][CHT]'))
        self.assertFalse(result.safe)

    def test_failed_hash_still_cannot_be_readded(self):
        pending = {'items': {'4005:1': {'bangumi_id': 4005, 'episode': 1,
                   'failed_info_hashes': [self.release.info_hash], 'failed_urls': []}}}
        result = _choose_release_for_episode(4005, 1, [self.release], {}, pending, mappings=[self.mapping])
        self.assertIsNone(result)

    def test_unhashed_alias_mirror_is_not_newly_admitted(self):
        mirror = replace(self.release, info_hash=None, torrent_url='https://example.invalid/mirror/42.torrent')
        self.assertFalse(self.assessment(mirror).safe)
        self.assertEqual(self.assessment(mirror).reason, 'release_content_identity_unverified')
        selected = _choose_release_for_episode(4005, 1, [mirror], {}, {'items': {}}, mappings=[self.mapping])
        self.assertIsNone(selected)

    def test_invalid_content_identity_is_not_newly_admitted(self):
        for info_hash in ('bad', 'g' * 40, 'a' * 39, 'A' * 40):
            with self.subTest(info_hash=info_hash):
                self.assertFalse(self.assessment(replace(self.release, info_hash=info_hash)).safe)

    def test_known_and_unknown_season_groups_remain_ambiguous(self):
        unnumbered = self.titled('[G] 少女怪兽焦糖味 / Otome Kaijuu Carameliser - 01 [CHT][MKV]')
        reasons = []
        result = _choose_release_for_episode(4005, 1, [self.release, unnumbered], {},
                    {'items': {}}, mappings=[self.mapping], ambiguity_reasons=reasons)
        self.assertIsNone(result)
        self.assertTrue(any('multiple_identity_groups' in reason for reason in reasons), reasons)

    def test_cached_nfo_scope_still_requires_explicit_release_season(self):
        unnumbered = self.titled('[G] 少女怪兽焦糖味 / Otome Kaijuu Carameliser - 01 [CHT][MKV]')
        result = self.assessment(unnumbered, {**self.mapping, 'season': 1, 'identity_source': 'cached_season_nfo'})
        self.assertFalse(result.safe)
        self.assertEqual(result.reason, 'scoped_source_season_unverified')

    def test_repeated_selection_is_idempotent_and_does_not_mutate_mapping(self):
        pending = {'items': {'4005:1': {'bangumi_id': 4005, 'episode': 1, 'failed_urls': []}}}
        mappings = [deepcopy(self.mapping)]
        original = deepcopy(mappings)
        first = _choose_release_for_episode(4005, 1, [self.release], {}, pending, mappings=mappings)
        stored = deepcopy(pending)
        second = _choose_release_for_episode(4005, 1, [self.release], {}, pending, mappings=mappings)
        self.assertEqual(first, self.release)
        self.assertEqual(second, first)
        self.assertEqual(pending, stored)
        self.assertEqual(mappings, original)


class MikanCollectionAliasIdentityTest(unittest.TestCase):
    def setUp(self):
        self.mapping = {'bangumi_id': 3583, 'path': '/anime/Once Upon a Witch Death',
            'title': 'Aru Majo ga Shinu Made',
            'match': ['直至魔女消逝', '直到某魔女死去', 'Aru Majo ga Shinu Made']}
        self.release = MikanRelease(bangumi_id=3583,
            title='[個人製作合集][喵萌奶茶屋&LoliHouse] 直至魔女消逝 / 直到某魔女死去 / Aru Majo ga Shinu Made - 01-12 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]',
            episode=1, episodes=tuple(range(1, 13)), torrent_url='https://example.invalid/batch.torrent',
            pub_date=None, content_length=None, source='animegarden:dmhy',
            info_hash='e22d51b05c55f0b9ddccba88daf42a2d2d8a5d36')

    def titled(self, title, **kwargs):
        return replace(self.release, title=title, series_identity='', season_number=None,
                       identity_evidence=[], **kwargs)

    def assess(self, release=None):
        return _assess_release_identity(release or self.release, [self.mapping])

    def test_independently_verified_collection_aliases_match(self):
        result = self.assess()
        self.assertTrue(result.safe, result.reason)
        self.assertEqual(result.identity_key, '3583:unknown')

    def test_episode_range_with_single_group_matches(self):
        release = self.titled(self.release.title.replace('[個人製作合集]', ''))
        self.assertTrue(self.assess(release).safe)

    def test_unknown_extra_prefix_is_not_discarded(self):
        release = self.titled(self.release.title.replace('[個人製作合集]', '[Unverified Sequel]'))
        self.assertFalse(self.assess(release).safe)

    def test_range_requires_matching_recorded_episode_members(self):
        for episodes in ((), (6,), (1, 2), tuple(range(1, 14)), (1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)):
            with self.subTest(episodes=episodes):
                self.assertFalse(self.assess(replace(self.release, episodes=episodes)).safe)

    def test_range_rejects_conflicting_primary_episode(self):
        self.assertFalse(self.assess(replace(self.release, episode=6)).safe)

    def test_invalid_ranges_are_not_title_boundaries(self):
        for value in ('12-01', '00-12', '01-5000', '01-12 Returns'):
            with self.subTest(value=value):
                self.assertFalse(self.assess(self.titled(self.release.title.replace('01-12', value))).safe)

    def test_unknown_collection_alias_remains_blocked(self):
        release = self.titled(self.release.title.replace('直到某魔女死去', 'Unverified Sequel'))
        self.assertFalse(self.assess(release).safe)

    def test_collection_does_not_authorize_a_season(self):
        release = self.titled(self.release.title.replace('Aru Majo ga Shinu Made -', 'Aru Majo ga Shinu Made S02 -'))
        self.assertFalse(self.assess(release).safe)

    def test_collection_requires_resolved_content_identity(self):
        result = self.assess(replace(self.release, info_hash=None))
        self.assertFalse(result.safe)
        self.assertEqual(result.reason, 'release_content_identity_unverified')

    def test_collection_failed_hash_is_still_excluded(self):
        pending = {'items': {'3583:6': {'bangumi_id': 3583, 'episode': 6,
            'failed_info_hashes': [self.release.info_hash], 'failed_urls': []}}}
        self.assertIsNone(_choose_release_for_episode(3583, 6, [self.release], {}, pending, mappings=[self.mapping]))

    def test_collection_choice_is_idempotent_without_metadata_mutation(self):
        original = deepcopy(self.mapping)
        pending = {'items': {'3583:6': {'bangumi_id': 3583, 'episode': 6, 'failed_urls': []}}}
        before = deepcopy(pending)
        for _ in range(2):
            self.assertEqual(_choose_release_for_episode(3583, 6, [self.release], {}, pending,
                mappings=[self.mapping]), self.release)
        self.assertEqual(self.mapping, original)
        self.assertEqual(pending, before)


if __name__ == '__main__':
    unittest.main()
