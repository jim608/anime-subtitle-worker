"""A replacement's active identity must not inherit progress from the prior torrent."""
from datetime import datetime, timezone
import unittest
from mikan_worker import _torrents_for_pending, _sync_pending_entry_qbit_progress
from qbit_client import QBitTorrent


def torrent(value, name='[Group] Show - 02', progress=0.25):
    return QBitTorrent(hash=value, name=name, progress=progress, state='downloading' if progress<1 else 'stalledUP',
        dlspeed=0, downloaded=250 if progress<1 else 4000, added_on=None,
        content_path=None, save_path=None, category='llm-sub', tags='mikansub')


class ProgressIdentityTests(unittest.TestCase):
    def test_replacement_ignores_stale_cached_hash_and_title_match(self):
        old=torrent('a'*40,progress=1)
        current=torrent('b'*40)
        entry={'info_hash':'b'*40,'torrent_url':'magnet:?xt=urn:btih:'+'b'*40,
            'last_qbit_hash':'a'*40,'title':old.name,'failed_info_hashes':['a'*40]}
        matched=_torrents_for_pending(entry,[old,current])
        self.assertEqual([x.hash for x in matched],['b'*40])
        _sync_pending_entry_qbit_progress(entry,matched,datetime.now(timezone.utc))
        self.assertEqual(entry['last_progress'],0.25)
        self.assertEqual(entry['last_downloaded'],250)
        self.assertEqual(entry['last_qbit_hash'],'b'*40)
        self.assertEqual(entry['failed_info_hashes'],['a'*40])

    def test_url_identity_is_authoritative_when_explicit_field_missing(self):
        entry={'torrent_url':'magnet:?xt=urn:btih:'+'b'*40,'last_qbit_hash':'a'*40,'title':'[Group] Show - 02'}
        self.assertEqual(_torrents_for_pending(entry,[torrent('a'*40,progress=1)]),[])

    def test_explicit_identity_does_not_fall_back_to_matching_title(self):
        entry={'info_hash':'b'*40,'title':'[Group] Show - 02'}
        self.assertEqual(_torrents_for_pending(entry,[torrent('a'*40,progress=1)]),[])

    def test_conflicting_current_identities_refuse_progress_association(self):
        entry={'info_hash':'b'*40,'torrent_url':'magnet:?xt=urn:btih:'+'c'*40,'title':'[Group] Show - 02'}
        self.assertEqual(_torrents_for_pending(entry,[torrent('b'*40),torrent('c'*40)]),[])

    def test_legacy_identityless_title_and_cached_hash_paths_remain_supported(self):
        current=torrent('b'*40)
        self.assertEqual(_torrents_for_pending({'title':current.name},[current]),[current])
        self.assertEqual(_torrents_for_pending({'last_qbit_hash':'b'*40},[current]),[current])


if __name__=='__main__':unittest.main()
