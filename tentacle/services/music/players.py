"""Music players, behind one interface.

Tentacle finds and requests music; the player plays it. Each enabled player
integration answers the same few questions (is it healthy, rescan after an
import, where does an album open) and, from phase 3, sets artist pictures.
Navidrome and Jellyfin are interchangeable; a new player is one more class.
Players are reached only through their own APIs, never their databases.
"""
import logging
from typing import Optional

import requests

from models.database import get_setting

logger = logging.getLogger(__name__)

TIMEOUT = 15


class Player:
    id = ""
    name = ""

    def health(self) -> dict:
        """{"ok": bool, "detail": str}"""
        raise NotImplementedError

    def rescan(self) -> None:
        raise NotImplementedError

    def album_url(self, rgid: str, title: str = "") -> Optional[str]:
        """Where the user's browser opens this album (a MusicBrainz release group), or None."""
        raise NotImplementedError

    def artist_image_state(self, mbid: str, name: str) -> str:
        """"absent" (the player doesn't list the artist yet), "missing" (no real
        picture: none, or the player's placeholder) or "ok"."""
        raise NotImplementedError

    def set_artist_image(self, mbid: str, name: str, data: bytes) -> None:
        raise NotImplementedError


class NavidromePlayer(Player):
    id, name = "navidrome", "Navidrome"

    def __init__(self, db):
        from services.navidrome import NavidromeClient
        self.url = (get_setting(db, "navidrome_url") or "").rstrip("/")
        self.public_url = (get_setting(db, "navidrome_public_url") or "").strip().rstrip("/") or self.url
        self.client = NavidromeClient(self.url, get_setting(db, "navidrome_username"),
                                      get_setting(db, "navidrome_password"))
        self.rescan_enabled = (get_setting(db, "navidrome_rescan_after_import", "true") or "").lower() == "true"

    def health(self) -> dict:
        from services.navidrome import NavidromeError
        try:
            self.client.login()
        except NavidromeError as e:
            return {"ok": False, "detail": e.message}
        detail = "signed in" + (" (admin)" if self.client.is_admin else "")
        if self.rescan_enabled and not self.client.is_admin:
            detail += "; rescans need an admin account"
        return {"ok": True, "detail": detail}

    def rescan(self) -> None:
        from services.navidrome import NavidromeError
        if not self.rescan_enabled:
            return
        auth = self.client._auth or self.client.login()
        if not self.client.is_admin:
            logger.info("[Navidrome] Skipping rescan: the account Tentacle uses isn't an admin")
            return
        r = self.client._call("GET", "/rest/startScan", params={
            "u": self.client.username, "t": auth.get("subsonicToken"), "s": auth.get("subsonicSalt"),
            "v": "1.16.1", "c": "tentacle", "f": "json"})
        if r.status_code >= 400:
            raise NavidromeError(f"Navidrome's rescan answered HTTP {r.status_code}.")
        logger.info("[Navidrome] Rescan started")

    def album_url(self, rgid: str, title: str = "") -> Optional[str]:
        from services.navidrome import NavidromeError
        try:
            # Navidrome's album filter matches a MusicBrainz id against the
            # album's release and release-group ids.
            r = self.client._call("GET", "/api/album", params={"name": rgid, "_end": 1},
                                  headers=self.client._headers())
            albums = r.json() if r.status_code < 400 else []
        except (NavidromeError, ValueError):
            return None
        if not albums:
            return None
        return f"{self.public_url}/app/#/album/{albums[0]['id']}/show"

    def _find_artist(self, mbid: str, name: str) -> Optional[dict]:
        from services.music.pictures import same_name
        for query in (mbid, name):
            r = self.client._call("GET", "/api/artist", params={"name": query, "_start": 0, "_end": 10},
                                  headers=self.client._headers())
            found = r.json() if r.status_code < 400 else []
            match = next((a for a in found if a.get("mbzArtistId") == mbid), None) or \
                next((a for a in found if same_name(a.get("name") or "", name) and not a.get("mbzArtistId")), None)
            if match:
                return match
        return None

    def artist_image_state(self, mbid: str, name: str) -> str:
        from services.music.pictures import looks_like_placeholder
        artist = self._find_artist(mbid, name)
        if not artist:
            return "absent"
        auth = self.client._auth or self.client.login()
        r = self.client._call("GET", "/rest/getCoverArt", params={
            "id": f"ar-{artist['id']}", "size": 300, "u": self.client.username, "t": auth.get("subsonicToken"),
            "s": auth.get("subsonicSalt"), "v": "1.16.1", "c": "tentacle"})
        return "missing" if r.status_code >= 400 or looks_like_placeholder(r.content) else "ok"

    def set_artist_image(self, mbid: str, name: str, data: bytes) -> None:
        from services.navidrome import NavidromeError
        artist = self._find_artist(mbid, name)
        if not artist:
            raise NavidromeError("Navidrome doesn't list this artist yet.")
        r = self.client._call("POST", f"/api/artist/{artist['id']}/image", headers=self.client._headers(),
                              files={"image": ("artist.jpg", data, "application/octet-stream")})
        if r.status_code >= 400:
            raise NavidromeError(f"Navidrome refused the picture (HTTP {r.status_code}: {r.text[:120].strip()}).")


class JellyfinPlayer(Player):
    id, name = "jellyfin", "Jellyfin"

    def __init__(self, db):
        self.db = db
        self.url = (get_setting(db, "jellyfin_url") or "").rstrip("/")
        self.key = get_setting(db, "jellyfin_api_key")
        self.library_id = get_setting(db, "jellyfin_music_library_id")

    def _get(self, path, params=None):
        r = requests.get(f"{self.url}{path}", params=params, headers={"X-Emby-Token": self.key}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def health(self) -> dict:
        if not self.url or not self.key:
            return {"ok": False, "detail": "Jellyfin isn't connected"}
        if not self.library_id:
            return {"ok": False, "detail": "no music library picked"}
        try:
            libs = self._get("/Library/VirtualFolders")
        except requests.RequestException as e:
            return {"ok": False, "detail": f"can't reach Jellyfin ({e.__class__.__name__})"}
        lib = next((l for l in libs if l.get("ItemId") == self.library_id), None)
        return {"ok": bool(lib), "detail": f"library '{lib['Name']}'" if lib else "the music library is gone"}

    def rescan(self) -> None:
        if not self.library_id:
            return
        r = requests.post(f"{self.url}/Items/{self.library_id}/Refresh", headers={"X-Emby-Token": self.key},
                          params={"Recursive": "true"}, timeout=TIMEOUT)
        r.raise_for_status()
        logger.info("[Jellyfin] Music library refresh started")

    def album_url(self, rgid: str, title: str = "") -> Optional[str]:
        from routers.discover import _jellyfin_web_url
        try:
            items = self._get("/Items", {"IncludeItemTypes": "MusicAlbum", "Recursive": "true",
                                         "ParentId": self.library_id, "SearchTerm": title,
                                         "Fields": "ProviderIds", "Limit": 50}).get("Items") or []
        except requests.RequestException:
            return None
        for item in items:
            ids = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
            if rgid in (ids.get("musicbrainzreleasegroup"), ids.get("musicbrainzalbum")):
                return _jellyfin_web_url(self.db, item.get("Id"))
        return None

    def _find_artist(self, mbid: str, name: str) -> Optional[dict]:
        from services.music.pictures import same_name
        items = self._get("/Items", {"IncludeItemTypes": "MusicArtist", "Recursive": "true", "SearchTerm": name,
                                     "Fields": "ProviderIds", "Limit": 50}).get("Items") or []
        for item in items:
            ids = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
            if ids.get("musicbrainzartist") == mbid:
                return item
        return next((i for i in items if same_name(i.get("Name") or "", name)), None)

    def artist_image_state(self, mbid: str, name: str) -> str:
        artist = self._find_artist(mbid, name)
        if not artist:
            return "absent"
        return "ok" if (artist.get("ImageTags") or {}).get("Primary") else "missing"

    def set_artist_image(self, mbid: str, name: str, data: bytes) -> None:
        import base64
        artist = self._find_artist(mbid, name)
        if not artist:
            raise RuntimeError("Jellyfin doesn't list this artist yet.")
        # Jellyfin takes the image base64-encoded in the body.
        r = requests.post(f"{self.url}/Items/{artist['Id']}/Images/Primary", data=base64.b64encode(data),
                          headers={"X-Emby-Token": self.key, "Content-Type": _image_type(data)}, timeout=TIMEOUT)
        if r.status_code >= 400:
            raise RuntimeError(f"Jellyfin refused the picture (HTTP {r.status_code}).")


def _image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def enabled_players(db) -> list:
    players = []
    if (get_setting(db, "navidrome_enabled", "false") or "").lower() == "true" and get_setting(db, "navidrome_url"):
        players.append(NavidromePlayer(db))
    if (get_setting(db, "jellyfin_music_enabled", "false") or "").lower() == "true":
        players.append(JellyfinPlayer(db))
    return players


def rescan_all(db) -> None:
    for p in enabled_players(db):
        try:
            p.rescan()
        except Exception as e:
            logger.warning(f"[Music] {p.name} rescan failed: {getattr(e, 'message', e)}")
