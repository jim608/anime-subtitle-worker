from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch
import unittest

import requests

from qbit_client import QBitClient, QBitError, map_remote_path


class QBitClientTest(unittest.TestCase):
    def test_list_torrents_reads_eta_and_activity_fields(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "hash": "hash1",
                "name": "Show",
                "progress": 0.5,
                "state": "downloading",
                "dlspeed": 100,
                "downloaded": 50,
                "added_on": 10,
                "eta": 90000,
                "last_activity": 20,
                "completion_on": -1,
                "creation_date": 5,
            }
        ]
        client.session.get = Mock(return_value=response)

        torrents = client.list_torrents(tag="mikansub", category="llm-sub")

        self.assertEqual(torrents[0].eta, 90000)
        self.assertEqual(torrents[0].last_activity, 20)
        self.assertEqual(torrents[0].completion_on, -1)
        self.assertEqual(torrents[0].creation_date, 5)

    def test_torrent_creation_date_reads_general_properties(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        response = Mock(status_code=200, text='{"creation_date": 1234}')
        response.json.return_value = {"creation_date": 1234}
        client.session.get = Mock(return_value=response)

        created_at = client.torrent_creation_date("ABCDEF")

        self.assertEqual(created_at, 1234)
        client.session.get.assert_called_once_with(
            "http://qbit/api/v2/torrents/properties",
            params={"hash": "abcdef"},
            timeout=30,
        )

    def test_login_accepts_no_content_response(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        response = Mock(status_code=204, text="")
        client.session.post = Mock(return_value=response)

        client.login()

        client.session.post.assert_called_once_with(
            "http://qbit/api/v2/auth/login",
            data={"username": "user", "password": "pass"},
            timeout=30,
        )

    def test_map_remote_path_uses_longest_matching_prefix(self) -> None:
        mapped = map_remote_path(
            "/remote/media/anime/Show/Episode.mkv",
            [
                {"remote": "/remote", "local": "/local"},
                {"remote": "/remote/media/anime", "local": "/library/anime"},
            ],
        )

        self.assertEqual(mapped, Path("/library/anime/Show/Episode.mkv"))

    def test_ensure_category_creates_missing_category_with_save_path(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        get_response = Mock(status_code=200)
        get_response.json.return_value = {}
        post_response = Mock(status_code=200)
        client.session.get = Mock(return_value=get_response)
        client.session.post = Mock(return_value=post_response)

        client.ensure_category("llm-sub", save_path="/jellyfin/anime")

        client.session.post.assert_called_once_with(
            "http://qbit/api/v2/torrents/createCategory",
            data={"category": "llm-sub", "savePath": "/jellyfin/anime"},
            timeout=30,
        )

    def test_add_url_retries_transient_connection_error(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        ok_response = Mock(status_code=200, text="Ok.")
        client.session.post = Mock(side_effect=[requests.ConnectionError("dropped"), ok_response])

        with patch("qbit_client.time.sleep", return_value=None):
            client.add_url(
                "https://mikan/test.torrent",
                save_path="/anime",
                category="llm-sub",
                tags=["mikansub"],
                paused=False,
            )

        self.assertEqual(client.session.post.call_count, 2)

    def test_add_url_accepts_async_pending_response(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        response = Mock(
            status_code=202,
            text='{"added_torrent_ids":[],"failure_count":0,"pending_count":1,"success_count":0}',
        )
        client.session.post = Mock(return_value=response)

        client.add_url(
            "https://mikan/test.torrent",
            save_path="/anime",
            category="llm-sub",
            tags=["mikansub"],
            paused=False,
        )

        client.session.post.assert_called_once()

    def test_add_url_accepts_success_json_with_http_200(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        response = Mock(
            status_code=200,
            text='{"added_torrent_ids":["abc"],"failure_count":0,"pending_count":0,"success_count":1}',
        )
        client.session.post = Mock(return_value=response)

        client.add_url(
            "https://mikan/test.torrent",
            save_path="/anime",
            category="llm-sub",
            tags=["mikansub"],
            paused=False,
        )

        client.session.post.assert_called_once()

    def test_add_url_wraps_connection_error_as_qbit_error(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        client.session.post = Mock(side_effect=requests.ConnectionError("dropped"))

        with (
            patch("qbit_client.time.sleep", return_value=None),
            self.assertRaises(QBitError),
        ):
            client.add_url(
                "https://mikan/test.torrent",
                save_path="/anime",
                category="llm-sub",
                tags=["mikansub"],
                paused=False,
            )

    def test_set_category_uses_pipe_separated_hashes(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        post_response = Mock(status_code=200)
        client.session.post = Mock(return_value=post_response)

        client.set_category(["hash1", "hash2"], "llm-sub")

        client.session.post.assert_called_once_with(
            "http://qbit/api/v2/torrents/setCategory",
            data={"hashes": "hash1|hash2", "category": "llm-sub"},
            timeout=30,
        )

    def test_remove_tags_uses_pipe_separated_hashes(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        post_response = Mock(status_code=200)
        client.session.post = Mock(return_value=post_response)

        client.remove_tags(["hash1", "hash2"], ["llm-sub", "llm-sub-extracted"])

        client.session.post.assert_called_once_with(
            "http://qbit/api/v2/torrents/removeTags",
            data={"hashes": "hash1|hash2", "tags": "llm-sub,llm-sub-extracted"},
            timeout=30,
        )

    def test_list_files_reads_qbit_file_list(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        response = Mock(status_code=200)
        response.json.return_value = [
            {"name": "Release/Release.mkv", "size": 100, "progress": 1, "priority": 1},
            {"name": "Release/Release.zh-Hant.srt", "size": 10, "progress": 1, "priority": 1},
        ]
        client.session.get = Mock(return_value=response)

        files = client.list_files("hash1")

        self.assertEqual([file.name for file in files], ["Release/Release.mkv", "Release/Release.zh-Hant.srt"])
        client.session.get.assert_called_once_with(
            "http://qbit/api/v2/torrents/files",
            params={"hash": "hash1"},
            timeout=30,
        )

    def test_stop_torrents_uses_current_stop_endpoint(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        response = Mock(status_code=200, text="")
        client.session.post = Mock(return_value=response)

        client.stop_torrents(["hash1", "hash2"])

        client.session.post.assert_called_once_with(
            "http://qbit/api/v2/torrents/stop",
            data={"hashes": "hash1|hash2"},
            timeout=30,
        )

    def test_stop_torrents_falls_back_to_legacy_pause_endpoint(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        missing = Mock(status_code=404, text="missing")
        accepted = Mock(status_code=200, text="")
        client.session.post = Mock(side_effect=[missing, accepted])

        client.stop_torrents(["hash1"])

        self.assertEqual(client.session.post.call_count, 2)
        self.assertEqual(
            client.session.post.call_args_list[1].args[0],
            "http://qbit/api/v2/torrents/pause",
        )

    def test_delete_torrents_uses_pipe_separated_hashes(self) -> None:
        client = QBitClient("http://qbit", "user", "pass")
        post_response = Mock(status_code=200)
        client.session.post = Mock(return_value=post_response)

        client.delete_torrents(["hash1", "hash2"], delete_files=True)

        client.session.post.assert_called_once_with(
            "http://qbit/api/v2/torrents/delete",
            data={"hashes": "hash1|hash2", "deleteFiles": "true"},
            timeout=30,
        )


if __name__ == "__main__":
    unittest.main()
