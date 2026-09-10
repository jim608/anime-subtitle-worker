from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from pathlib import Path
import time
from typing import Any

import requests


class QBitError(RuntimeError):
    pass


@dataclass(frozen=True)
class QBitTorrent:
    hash: str
    name: str
    progress: float
    state: str
    dlspeed: int
    downloaded: int
    added_on: int | None
    content_path: str | None
    save_path: str | None
    category: str | None
    tags: str | None
    eta: int | None = None
    last_activity: int | None = None
    completion_on: int | None = None
    creation_date: int | None = None


@dataclass(frozen=True)
class QBitTorrentFile:
    name: str
    size: int
    progress: float
    priority: int | None


class QBitClient:
    def __init__(self, base_url: str, username: str, password: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self._content_retention_supported = False

    def login(self) -> None:
        response = self._post_with_retry(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        body = response.text.strip()
        if response.status_code not in {200, 204} or body not in {"", "Ok.", "Ok"}:
            raise QBitError(f"qBittorrent login failed: status={response.status_code} body={response.text[:200]!r}")

    def add_url(
        self,
        url: str,
        *,
        save_path: str | None,
        category: str | None,
        tags: list[str],
        paused: bool,
        preserve_content: bool = False,
    ) -> None:
        data: dict[str, Any] = {
            "urls": url,
            "paused": "true" if paused else "false",
        }
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category
        if tags:
            data["tags"] = ",".join(tags)

        if preserve_content:
            if not category or not tags:
                raise QBitError('content_retention_project_scope_missing')
            self._require_content_retention_support()
            # Keep the user's time/ratio limits, but retain the file at expiry.
            # This survives qB restart and does not enable unlimited seeding.
            data['shareLimitAction'] = 'Stop'

        response = self._post_with_retry(f"{self.base_url}/api/v2/torrents/add", data=data)
        if not _add_torrent_response_accepted(response):
            raise QBitError(f"qBittorrent add torrent failed: status={response.status_code} body={response.text[:200]!r}")

    def _require_content_retention_support(self) -> None:
        if self._content_retention_supported:
            return
        response = self._get_with_retry(f'{self.base_url}/api/v2/app/webapiVersion')
        version = response.text.strip()
        if response.status_code != 200 or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise QBitError('content_retention_api_version_unverified')
        if tuple(map(int, version.split('.'))) < (2, 12, 0):
            raise QBitError('content_retention_api_unsupported')
        self._content_retention_supported = True

    def ensure_content_retained(self, torrent_hash: str, *, category: str, tags: list[str]) -> dict[str, Any]:
        """Verify one owned torrent's non-destructive expiry, preserving thresholds.

        Never changes global preferences, starts a torrent or deletes content.
        HTTP acceptance alone is not verification: read back the effective action.
        """
        if not re.fullmatch(r'[0-9a-fA-F]{40}', torrent_hash) or not category or not tags:
            raise QBitError('content_retention_project_scope_invalid')
        torrent_hash = torrent_hash.lower()
        self._require_content_retention_support()

        def snapshot() -> dict[str, Any]:
            response = self._get_with_retry(f'{self.base_url}/api/v2/torrents/info', params={'hashes':torrent_hash})
            if response.status_code != 200:
                raise QBitError('content_retention_lookup_failed')
            try:
                rows = response.json()
            except ValueError as exc:
                raise QBitError('content_retention_response_invalid') from exc
            if not isinstance(rows, list) or len(rows) != 1:
                raise QBitError('content_retention_torrent_missing_or_ambiguous')
            row = rows[0]
            if (not isinstance(row, dict) or row.get('hash') != torrent_hash or row.get('category') != category
                or not set(tags).issubset({s.strip() for s in str(row.get('tags') or '').split(',')})):
                raise QBitError('content_retention_project_scope_mismatch')
            return row

        before = snapshot()
        keys = ('ratio_limit', 'seeding_time_limit', 'inactive_seeding_time_limit')
        if before.get('share_limit_action') == 'Stop':
            return {'status':'VERIFIED', 'changed':False, 'hash':torrent_hash, 'action':'Stop'}
        if before.get('share_limit_action') not in {'Default','Remove','RemoveWithContent','EnableSuperSeeding'}:
            raise QBitError('content_retention_action_unverified')
        if any(type(before.get(k)) not in (int, float) or not math.isfinite(before[k]) or before[k] < -2 for k in keys):
            raise QBitError('content_retention_thresholds_unverified')
        if any(type(before[k]) is not int for k in keys[1:]):
            raise QBitError('content_retention_thresholds_unverified')
        data = {'hashes':torrent_hash, 'ratioLimit':before[keys[0]],
            'seedingTimeLimit':before[keys[1]], 'inactiveSeedingTimeLimit':before[keys[2]], 'shareLimitAction':'Stop'}
        response = self._post_with_retry(f'{self.base_url}/api/v2/torrents/setShareLimits', data=data)
        if response.status_code != 200:
            raise QBitError('content_retention_write_failed')
        after = snapshot()
        if after.get('share_limit_action') != 'Stop' or any(after.get(k) != before[k] for k in keys):
            raise QBitError('content_retention_readback_mismatch')
        return {'status':'VERIFIED', 'changed':True, 'hash':torrent_hash, 'action':'Stop',
                'previous_action':before['share_limit_action'], 'thresholds':{k:after[k] for k in keys}}

    def _post_with_retry(self, url: str, *, data: dict[str, Any], attempts: int = 3) -> requests.Response:
        last_error: requests.RequestException | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self.session.post(url, data=data, timeout=self.timeout_seconds)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                time.sleep(min(0.5 * attempt, 2.0))
        raise QBitError(f"qBittorrent request failed: {last_error}") from last_error

    def _get_with_retry(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        attempts: int = 3,
    ) -> requests.Response:
        last_error: requests.RequestException | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self.session.get(url, params=params, timeout=self.timeout_seconds)
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                time.sleep(min(0.5 * attempt, 2.0))
        raise QBitError(f"qBittorrent request failed: {last_error}") from last_error

    def ensure_category(self, category: str | None, *, save_path: str | None = None) -> None:
        if not category:
            return
        response = self._get_with_retry(f"{self.base_url}/api/v2/torrents/categories")
        if response.status_code != 200:
            raise QBitError(f"qBittorrent categories failed: status={response.status_code} body={response.text[:200]!r}")
        if category in response.json():
            return

        data: dict[str, str] = {"category": category}
        if save_path:
            data["savePath"] = save_path
        response = self._post_with_retry(f"{self.base_url}/api/v2/torrents/createCategory", data=data)
        if response.status_code != 200:
            raise QBitError(f"qBittorrent createCategory failed: status={response.status_code} body={response.text[:200]!r}")

    def list_torrents(self, *, tag: str | None = None, category: str | None = None) -> list[QBitTorrent]:
        params: dict[str, str] = {}
        if tag:
            params["tag"] = tag
        if category:
            params["category"] = category

        response = self._get_with_retry(f"{self.base_url}/api/v2/torrents/info", params=params)
        if response.status_code != 200:
            raise QBitError(f"qBittorrent torrents/info failed: status={response.status_code} body={response.text[:200]!r}")

        torrents: list[QBitTorrent] = []
        for item in response.json():
            torrents.append(
                QBitTorrent(
                    hash=str(item.get("hash", "")),
                    name=str(item.get("name", "")),
                    progress=float(item.get("progress", 0.0)),
                    state=str(item.get("state", "")),
                    dlspeed=int(item.get("dlspeed", 0) or 0),
                    downloaded=int(item.get("downloaded", 0) or 0),
                    added_on=int(item["added_on"]) if item.get("added_on") is not None else None,
                    content_path=item.get("content_path") or None,
                    save_path=item.get("save_path") or None,
                    category=item.get("category") or None,
                    tags=item.get("tags") or None,
                    eta=int(item["eta"]) if item.get("eta") is not None else None,
                    last_activity=int(item["last_activity"]) if item.get("last_activity") is not None else None,
                    completion_on=int(item["completion_on"]) if item.get("completion_on") is not None else None,
                    creation_date=int(item["creation_date"]) if item.get("creation_date") is not None else None,
                )
            )
        return torrents

    def torrent_creation_date(self, torrent_hash: str) -> int | None:
        """Return the timestamp embedded in the torrent metainfo.

        qBittorrent exposes this separately from the normal torrent list.  It
        is not the same thing as the website publication, qB addition, or
        download completion time, so callers must keep it as a distinct field.
        """

        normalized_hash = str(torrent_hash or "").strip().casefold()
        if not normalized_hash:
            return None
        response = self._get_with_retry(
            f"{self.base_url}/api/v2/torrents/properties",
            params={"hash": normalized_hash},
        )
        if response.status_code != 200:
            raise QBitError(
                "qBittorrent torrents/properties failed: "
                f"status={response.status_code} body={response.text[:200]!r}"
            )
        try:
            value = int((response.json() or {}).get("creation_date") or 0)
        except (AttributeError, TypeError, ValueError) as exc:
            raise QBitError("qBittorrent torrents/properties returned an invalid creation_date") from exc
        return value if value > 0 else None

    def list_files(self, torrent_hash: str) -> list[QBitTorrentFile]:
        response = self._get_with_retry(
            f"{self.base_url}/api/v2/torrents/files",
            params={"hash": torrent_hash},
        )
        if response.status_code != 200:
            raise QBitError(f"qBittorrent torrents/files failed: status={response.status_code} body={response.text[:200]!r}")

        files: list[QBitTorrentFile] = []
        for item in response.json():
            files.append(
                QBitTorrentFile(
                    name=str(item.get("name", "")),
                    size=int(item.get("size", 0) or 0),
                    progress=float(item.get("progress", 0.0)),
                    priority=int(item["priority"]) if item.get("priority") is not None else None,
                )
            )
        return files

    def add_tags(self, torrent_hash: str, tags: list[str]) -> None:
        if not tags:
            return
        response = self._post_with_retry(
            f"{self.base_url}/api/v2/torrents/addTags",
            data={"hashes": torrent_hash, "tags": ",".join(tags)},
        )
        if response.status_code != 200:
            raise QBitError(f"qBittorrent addTags failed: status={response.status_code} body={response.text[:200]!r}")

    def remove_tags(self, torrent_hashes: list[str], tags: list[str]) -> None:
        if not torrent_hashes or not tags:
            return
        response = self._post_with_retry(
            f"{self.base_url}/api/v2/torrents/removeTags",
            data={"hashes": "|".join(torrent_hashes), "tags": ",".join(tags)},
        )
        if response.status_code != 200:
            raise QBitError(f"qBittorrent removeTags failed: status={response.status_code} body={response.text[:200]!r}")

    def set_category(self, torrent_hashes: list[str], category: str | None) -> None:
        if not torrent_hashes or not category:
            return
        response = self._post_with_retry(
            f"{self.base_url}/api/v2/torrents/setCategory",
            data={"hashes": "|".join(torrent_hashes), "category": category},
        )
        if response.status_code != 200:
            raise QBitError(f"qBittorrent setCategory failed: status={response.status_code} body={response.text[:200]!r}")

    def stop_torrents(self, torrent_hashes: list[str]) -> None:
        """Cooperatively stop torrents without deleting their data.

        qBittorrent 5 renamed the legacy ``pause`` endpoint to ``stop``.  Try
        the current endpoint first and fall back only when an older server
        reports that the route does not exist.  This operation is intentionally
        reversible and is used by state repair instead of deleting a torrent.
        """

        if not torrent_hashes:
            return
        data = {"hashes": "|".join(torrent_hashes)}
        response = self._post_with_retry(f"{self.base_url}/api/v2/torrents/stop", data=data)
        if response.status_code in {404, 405}:
            response = self._post_with_retry(f"{self.base_url}/api/v2/torrents/pause", data=data)
        if response.status_code != 200:
            raise QBitError(
                f"qBittorrent stop failed: status={response.status_code} body={response.text[:200]!r}"
            )

    def delete_torrents(self, torrent_hashes: list[str], *, delete_files: bool) -> None:
        if not torrent_hashes:
            return
        response = self._post_with_retry(
            f"{self.base_url}/api/v2/torrents/delete",
            data={"hashes": "|".join(torrent_hashes), "deleteFiles": "true" if delete_files else "false"},
        )
        if response.status_code != 200:
            raise QBitError(f"qBittorrent delete failed: status={response.status_code} body={response.text[:200]!r}")


def map_remote_path(path: str | None, mappings: list[dict[str, str]]) -> Path | None:
    if not path:
        return None

    normalized = path.replace("\\", "/")
    best_mapping: dict[str, str] | None = None
    for mapping in mappings:
        remote = mapping["remote"].replace("\\", "/").rstrip("/")
        if normalized == remote or normalized.startswith(remote + "/"):
            if best_mapping is None or len(remote) > len(best_mapping["remote"]):
                best_mapping = mapping

    if best_mapping is None:
        return Path(path)

    remote = best_mapping["remote"].replace("\\", "/").rstrip("/")
    local = Path(best_mapping["local"])
    relative = normalized[len(remote) :].lstrip("/")
    return local / Path(*relative.split("/")) if relative else local


def _add_torrent_response_accepted(response: requests.Response) -> bool:
    body = response.text.strip()
    if response.status_code == 200 and body in {"Ok.", "Ok"}:
        return True
    if response.status_code not in {200, 202}:
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    failure_count = int(payload.get("failure_count") or 0)
    success_count = int(payload.get("success_count") or 0)
    pending_count = int(payload.get("pending_count") or 0)
    added_ids = payload.get("added_torrent_ids")
    added_count = len(added_ids) if isinstance(added_ids, list) else 0
    return failure_count == 0 and (success_count > 0 or pending_count > 0 or added_count > 0)
