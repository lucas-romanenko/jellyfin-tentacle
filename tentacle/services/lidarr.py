"""Lidarr API client, with guard rails built in.

A previous tool took a server down by asking Lidarr for too much, so every
call here goes through one gate:

* one request at a time, across the whole process (a lock);
* a 15 second timeout;
* no include* flags and no history or "since" reads (they make Lidarr build
  huge responses);
* paged reads capped at 50 items per page;
* at most two gentle retries (connection errors, timeouts, 5xx), then it gives
  up with a logged reason. Nothing loops.

Lidarr is the only thing that writes music files. Tentacle only asks it to,
through this API.
"""
import logging
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_PAGE_SIZE = 50
RETRY_DELAYS = (2, 5)

# One request at a time to Lidarr, whoever is asking.
_gate = threading.Lock()


class LidarrError(Exception):
    """A Lidarr call failed. `message` is written for the user."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status


def check_request_is_safe(path: str, params: Optional[dict]) -> None:
    """Refuse the kinds of request that make Lidarr do unbounded work.

    A programming error, not a runtime condition, so it raises ValueError.
    """
    low_path = path.lower()
    if not low_path.startswith("/api/v1/"):
        raise ValueError(f"Lidarr path must be under /api/v1/: {path}")
    if "/history" in low_path:
        raise ValueError("Tentacle never reads Lidarr's history")
    for key, value in (params or {}).items():
        k = key.lower()
        if k.startswith("include"):
            raise ValueError(f"Tentacle never sends include* flags to Lidarr ({key})")
        if k == "since":
            raise ValueError("Tentacle never reads Lidarr data 'since' a date")
        if k == "pagesize":
            try:
                size = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"Bad pageSize {value!r}")
            if size > MAX_PAGE_SIZE:
                raise ValueError(f"pageSize {size} exceeds {MAX_PAGE_SIZE}")


class LidarrClient:
    def __init__(self, url: str, api_key: str):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""

    def _request(self, method: str, path: str, params: Optional[dict] = None,
                 json_body=None, retries: int = len(RETRY_DELAYS)):
        check_request_is_safe(path, params)
        attempt = 0
        while True:
            try:
                with _gate:
                    r = requests.request(method, f"{self.url}{path}", params=params, json=json_body,
                                         headers={"X-Api-Key": self.api_key}, timeout=TIMEOUT)
            except requests.exceptions.Timeout:
                reason = f"Lidarr did not answer within {TIMEOUT}s"
                retryable = True
                r = None
            except requests.exceptions.ConnectionError as e:
                reason = f"Can't reach Lidarr at {self.url} ({e.__class__.__name__})"
                retryable = True
                r = None
            except requests.exceptions.RequestException as e:
                # A malformed URL and the like: retrying won't help.
                raise LidarrError(f"Can't call Lidarr at {self.url!r}: {e}")
            else:
                if r.status_code == 401:
                    raise LidarrError("Lidarr rejected the API key.", 401)
                if r.status_code >= 500:
                    reason = f"Lidarr answered HTTP {r.status_code}"
                    retryable = True
                elif r.status_code >= 400:
                    raise LidarrError(self._explain(r), r.status_code)
                else:
                    if not r.content:
                        return None
                    try:
                        return r.json()
                    except ValueError:
                        raise LidarrError("Lidarr returned something that isn't JSON. "
                                          "Is a proxy or login page in front of it?", r.status_code)
            if not retryable or attempt >= retries:
                logger.warning(f"[Lidarr] Giving up on {method} {path} after {attempt + 1} "
                               f"attempt{'s' if attempt else ''}: {reason}")
                raise LidarrError(reason, getattr(r, "status_code", None))
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            logger.info(f"[Lidarr] {method} {path}: {reason}; retrying in {delay}s")
            time.sleep(delay)
            attempt += 1

    @staticmethod
    def _explain(r) -> str:
        try:
            body = r.json()
        except ValueError:
            body = None
        msgs = []
        for entry in body if isinstance(body, list) else [body]:
            if isinstance(entry, dict):
                msg = entry.get("errorMessage") or entry.get("message")
                if msg:
                    msgs.append(str(msg).replace("<", " ").replace(">", " ").strip())
        detail = "; ".join(dict.fromkeys(msgs))
        return f"Lidarr refused it (HTTP {r.status_code}){': ' + detail if detail else ''}."

    def get(self, path: str, params: Optional[dict] = None, retries: int = len(RETRY_DELAYS)):
        return self._request("GET", path, params=params, retries=retries)

    def post(self, path: str, body=None, params: Optional[dict] = None, retries: int = 0):
        # Writes are not retried: a timed-out POST may have landed.
        return self._request("POST", path, params=params, json_body=body, retries=retries)

    # ── Small, bounded reads ─────────────────────────────────────────────
    def system_status(self, retries: int = 0) -> dict:
        return self.get("/api/v1/system/status", retries=retries) or {}

    def root_folders(self) -> list:
        return self.get("/api/v1/rootfolder") or []

    def quality_profiles(self) -> list:
        return self.get("/api/v1/qualityprofile") or []

    def metadata_profiles(self) -> list:
        return self.get("/api/v1/metadataprofile") or []

    def notifications(self) -> list:
        return self.get("/api/v1/notification") or []

    def test_notification(self, notification: dict):
        """Ask Lidarr to fire this notification's test event (it calls the webhook)."""
        return self.post("/api/v1/notification/test", notification)

    def put(self, path: str, body=None):
        return self._request("PUT", path, json_body=body, retries=0)

    # ── Library ──────────────────────────────────────────────────────────
    # Lidarr's album list can't be paged, so the whole library is never read
    # in one request: artists first, then one artist's albums at a time.

    def artists(self) -> list:
        return self.get("/api/v1/artist") or []

    def artist(self, artist_id: int) -> dict:
        return self.get(f"/api/v1/artist/{int(artist_id)}") or {}

    def albums_by_artist(self, artist_id: int) -> list:
        return self.get("/api/v1/album", {"artistId": int(artist_id)}) or []

    def album(self, album_id: int) -> dict:
        return self.get(f"/api/v1/album/{int(album_id)}") or {}

    def album_by_mbid(self, rgid: str) -> Optional[dict]:
        found = self.get("/api/v1/album", {"foreignAlbumId": rgid}) or []
        return found[0] if found else None

    def lookup_album(self, rgid: str) -> Optional[dict]:
        """Lidarr's metadata for an album it doesn't have yet (its own metadata server)."""
        results = self.get("/api/v1/album/lookup", {"term": f"lidarr:{rgid}"}) or []
        return next((a for a in results if a.get("foreignAlbumId") == rgid), None)

    def queue(self, max_pages: int = 20) -> list:
        """What is downloading, 50 records per page."""
        records, page = [], 1
        while page <= max_pages:
            data = self.get("/api/v1/queue", {"page": page, "pageSize": MAX_PAGE_SIZE}) or {}
            batch = data.get("records") or []
            records += batch
            if not batch or len(records) >= (data.get("totalRecords") or 0):
                break
            page += 1
        return records

    # ── Changes ──────────────────────────────────────────────────────────

    def add_album(self, resource: dict) -> dict:
        return self.post("/api/v1/album", resource) or {}

    def update_album(self, resource: dict) -> dict:
        return self.put(f"/api/v1/album/{int(resource['id'])}", resource) or {}

    def set_monitored(self, album_ids: list, monitored: bool):
        return self.put("/api/v1/album/monitor", {"albumIds": [int(i) for i in album_ids], "monitored": monitored})

    def command(self, name: str, **body) -> dict:
        return self.post("/api/v1/command", dict(body, name=name)) or {}

    def search_albums(self, album_ids: list) -> dict:
        return self.command("AlbumSearch", albumIds=[int(i) for i in album_ids])

    def pin_release(self, album: dict, foreign_release_id: str) -> dict:
        """Pin = that release monitored, every other release not, "any release OK" off."""
        resource = dict(album)
        releases = [dict(r, monitored=(r.get("foreignReleaseId") == foreign_release_id))
                    for r in album.get("releases") or []]
        if not any(r["monitored"] for r in releases):
            raise LidarrError(f"Lidarr's album '{album.get('title')}' has no release {foreign_release_id}.")
        resource["releases"] = releases
        resource["anyReleaseOk"] = False
        return self.update_album(resource)

    def delete(self, path: str):
        return self._request("DELETE", path, retries=0)

    # ── Files and commands (for re-pinning an album that has files) ─────

    def trackfiles(self, album_id: int) -> list:
        """The album's files that are linked to tracks of its pinned release."""
        return self.get("/api/v1/trackfile", {"albumId": int(album_id)}) or []

    def trackfiles_by_id(self, ids: list) -> list:
        if not ids:
            return []
        return self.get("/api/v1/trackfile", {"trackFileIds": [int(i) for i in ids]}) or []

    def delete_trackfile(self, trackfile_id: int):
        """Lidarr deletes the file (into its recycle bin, if one is set)."""
        return self.delete(f"/api/v1/trackfile/{int(trackfile_id)}")

    # Lidarr's metadata server (MusicBrainz data, cached by the Lidarr project): no
    # one-request-a-second limit, so Discover and playlist imports match names here.
    # Its search ranks loosely: callers pick exact title / artist matches themselves.
    def lookup_albums(self, term: str) -> list:
        return self.get("/api/v1/album/lookup", {"term": term}, retries=0) or []

    def lookup_artists(self, term: str) -> list:
        return self.get("/api/v1/artist/lookup", {"term": term}, retries=0) or []

    def recycle_bin(self) -> str:
        """Lidarr's recycle bin folder; "" when none is set (deleted files are then gone for good)."""
        return (self.get("/api/v1/config/mediamanagement", retries=0) or {}).get("recycleBin") or ""

    def commands(self) -> list:
        return self.get("/api/v1/command") or []
