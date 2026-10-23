"""▶ Play and Open in Jellyfin for titles shown in Tentacle (Oct 2026).

Every title Tentacle shows that can be watched now (Library, its details,
Recently downloaded, Discover and search results that are in the library,
Following) carries `jellyfin: {"play": url, "open": url}`; a title that is
downloading, requested or still being got ready carries none.

  open  {base}/web/#/details?id=<item>&serverId=<server>
  play  the same with &tentaclePlay=1: the plugin's script in Jellyfin Web
        (Inject/tentacle-play.js) starts it there, from where the person
        stopped, or a show's next-up episode. Without the plugin it is the
        details page.

Links are plain https links inside Jellyfin Web's scope (/web/), so on
Android a phone with Jellyfin installed as an app (PWA) can open them in it.

base: jellyfin_public_url (Settings → Services), or jellyfin_url only for a
browser on the home network (it is usually a name only Tentacle reaches, and
from outside it never works). Neither: no links.

The Jellyfin item comes from Discover's cached TMDB → item maps (made as the
configured Jellyfin user), or else the id Tentacle stored for the title (a
download Jellyfin has just added). People who see only part of the library
(kids, some libraries) get links only for what Jellyfin lets them open
(services.family.visible_titles).
"""
import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from models.database import Movie, Series, TentacleUser, get_setting

logger = logging.getLogger(__name__)

PLAY_FLAG = "tentaclePlay=1"
# How long a page waits for the first Jellyfin map ever (later ones are
# refreshed in the background while the previous map is used).
MAP_WAIT_S = 1.5


def _kind(media_type: str) -> str:
    return "series" if media_type == "series" else "movie"


def browser_base(db: Session, request) -> Optional[str]:
    """Where this browser opens Jellyfin, or None."""
    public = (get_setting(db, "jellyfin_public_url") or "").strip().rstrip("/")
    if public:
        return public
    from services import sign_in
    internal = (get_setting(db, "jellyfin_url") or "").strip().rstrip("/")
    if internal and request is not None and sign_in.at_home(request):
        return internal
    return None


def _server_id(db: Session) -> Optional[str]:
    # Discover's cached lookup (resolved once per process, retried on failure).
    from routers import discover
    cache = discover._jf_server_id_cache
    if not cache["checked"]:
        discover._jellyfin_web_url(db, "x")
    return cache["id"]


def urls(base: str, item_id: str, server_id: Optional[str]) -> dict:
    suffix = f"&serverId={server_id}" if server_id else ""
    open_url = f"{base}/web/#/details?id={item_id}{suffix}"
    return {"play": f"{open_url}&{PLAY_FLAG}", "open": open_url}


class Linker:
    """Links for one request: built once, used for every title it shows."""

    def __init__(self, db: Session, base: Optional[str], user: Optional[TentacleUser]):
        self.db = db
        self.base = base
        self.user = user
        self._server = None
        self._maps = {}
        self._stored = {}
        self._allowed = None
        self._limited = None

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.user)

    def _server_id(self):
        if self._server is None:
            try:
                self._server = _server_id(self.db) or ""
            except Exception as e:
                logger.debug(f"Watch links: no Jellyfin server id: {e}")
                self._server = ""
        return self._server or None

    def _map(self, kind: str) -> dict:
        if kind not in self._maps:
            from routers import discover
            try:
                self._maps[kind] = discover._search_jellyfin_ids(kind, time.time() + MAP_WAIT_S) or {}
            except Exception as e:
                logger.debug(f"Watch links: no Jellyfin {kind} map: {e}")
                self._maps[kind] = {}
        return self._maps[kind]

    def _stored_id(self, kind: str, tmdb_id: int) -> Optional[str]:
        if kind not in self._stored:
            model = Series if kind == "series" else Movie
            rows = self.db.query(model.tmdb_id, model.jellyfin_item_id).filter(
                model.jellyfin_item_id.isnot(None), model.jellyfin_item_id != "").all()
            self._stored[kind] = {t: j for t, j in rows}
        return self._stored[kind].get(tmdb_id)

    def _sees(self, kind: str, tmdb_id: int) -> bool:
        if self._limited is None:
            from routers.library import _sees_only_some
            self._limited = _sees_only_some(self.db, self.user)
        if not self._limited:
            return True
        if self._allowed is None:
            from services.family import visible_titles
            self._allowed = visible_titles(self.db, self.user)
        return (kind, tmdb_id) in self._allowed

    def item_id(self, media_type: str, tmdb_id, stored: Optional[str] = None) -> Optional[str]:
        """The Jellyfin item to open for a title, if it can be watched now."""
        if not self.enabled or not tmdb_id:
            return None
        kind = _kind(media_type)
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return None
        from services import download_readiness
        if download_readiness.held_back(kind, tmdb_id):
            return None
        item = self._map(kind).get(tmdb_id) or stored or self._stored_id(kind, tmdb_id)
        if not item or not self._sees(kind, tmdb_id):
            return None
        return item

    def links_for_item(self, item_id: Optional[str]) -> Optional[dict]:
        if not self.enabled or not item_id:
            return None
        return urls(self.base, str(item_id).replace("-", ""), self._server_id())

    def links(self, media_type: str, tmdb_id, stored: Optional[str] = None) -> Optional[dict]:
        return self.links_for_item(self.item_id(media_type, tmdb_id, stored))

    def attach(self, items: list, only_in_library: bool = False) -> list:
        """Copies of the titles, each with `jellyfin` (links) or without it.
        Copies: some lists are shared through caches, and links are per person."""
        out = []
        for it in items or []:
            it = dict(it)
            it.pop("jellyfin", None)
            mt = it.get("media_type")
            if mt in ("movie", "series") and not (only_in_library and not it.get("in_library")):
                links = self.links(mt, it.get("tmdb_id"), it.get("jellyfin_item_id") or None)
                if links:
                    it["jellyfin"] = links
            out.append(it)
        return out


def linker(db: Session, request, user: Optional[TentacleUser] = None) -> Linker:
    if user is None and request is not None:
        try:
            from routers.auth import get_user_from_request
            user = get_user_from_request(request, db)
        except Exception:
            user = None
    return Linker(db, browser_base(db, request) if user else None, user)
