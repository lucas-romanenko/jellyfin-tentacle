"""'Bad copy? Get another one': replace a downloaded file with a different release.

Wrong language, burned-in subtitles, broken audio, a fake: the file is a dud.
Radarr/Sonarr would happily grab the same release again, so the release that
produced it is marked failed first (that blocklists it), then the file is
deleted, the title kept monitored, and a new search started.

While a title is being replaced its Tentacle record briefly goes away (Radarr's
file-delete webhook and Jellyfin's delete hook both drop it). The request
history must survive that, or the requester loses the right to manage it and
never hears the replacement arrived — so the title is marked "replacing" and
those paths keep its DownloadRequest.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import Setting, get_setting, log_deletion

logger = logging.getLogger(__name__)

REPLACING_FOR = timedelta(days=14)


class BadCopyError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _key(media_type: str, tmdb_id: int) -> str:
    return f"replacing:{'series' if media_type == 'series' else 'movie'}:{tmdb_id}"


def mark_replacing(db: Session, media_type: str, tmdb_id: int) -> None:
    from models.database import set_setting
    set_setting(db, _key(media_type, tmdb_id), datetime.utcnow().isoformat())


def is_replacing(db: Session, media_type: str, tmdb_id: int) -> bool:
    raw = get_setting(db, _key(media_type, tmdb_id))
    if not raw:
        return False
    try:
        return datetime.utcnow() - datetime.fromisoformat(raw) < REPLACING_FOR
    except ValueError:
        return False


def clear_replacing(db: Session, media_type: str, tmdb_id: int) -> None:
    db.query(Setting).filter(Setting.key == _key(media_type, tmdb_id)).delete()
    db.commit()


class _Arr:
    def __init__(self, db: Session, app: str):
        self.name = app.capitalize()
        self.url = (get_setting(db, f"{app}_url") or "").rstrip("/")
        self.key = get_setting(db, f"{app}_api_key") or ""
        if not (self.url and self.key):
            raise BadCopyError(503, f"{self.name} is not configured")

    def call(self, method: str, path: str, **kw):
        try:
            r = requests.request(method, f"{self.url}/api/v3/{path}", headers={"X-Api-Key": self.key},
                                 timeout=30, **kw)
        except Exception as e:
            raise BadCopyError(502, f"Couldn't reach {self.name}: {e}")
        if r.status_code >= 400:
            raise BadCopyError(502, f"{self.name} refused ({r.status_code}) on {path.split('?')[0]}")
        return r.json() if r.text else None


def _latest_grab(records: list) -> Optional[dict]:
    grabs = [h for h in records or [] if (h.get("eventType") or "").lower() == "grabbed"]
    return max(grabs, key=lambda h: h.get("date") or "", default=None)


def _outcome(title: str, grab: Optional[dict], blocked: bool) -> str:
    if blocked:
        return f"Getting another copy of {title}. The bad release is blocklisted so it won't come back."
    if grab is None:
        return (f"Getting another copy of {title}. Couldn't tell which release the bad file came from, "
                "so the same one could be picked again.")
    return f"Getting another copy of {title}, but the bad release couldn't be blocklisted."


def replace_movie(db: Session, tmdb_id: int, user_name: str = None) -> dict:
    from models.database import Movie
    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is not None and (row.source or "").startswith("provider_"):
        raise BadCopyError(400, "This is an IPTV stream, not a download. Use “Wrong movie? Fix it” instead.")
    arr = _Arr(db, "radarr")
    movie = next((m for m in arr.call("GET", "movie", params={"tmdbId": tmdb_id}) or []
                  if m.get("tmdbId") == tmdb_id), None)
    if not movie:
        raise BadCopyError(404, "This movie is not in Radarr")
    file_id = (movie.get("movieFile") or {}).get("id")
    if not movie.get("hasFile") or not file_id:
        raise BadCopyError(409, "Radarr has no file for this movie")
    title = movie.get("title") or (row.title if row else str(tmdb_id))

    grab = _latest_grab(arr.call("GET", "history/movie", params={"movieId": movie["id"]}))
    blocked = False
    if grab:
        try:
            arr.call("POST", f"history/failed/{grab['id']}")
            blocked = True
        except BadCopyError as e:
            logger.warning(f"[BadCopy] Couldn't mark the grab of '{title}' failed: {e}")

    mark_replacing(db, "movie", tmdb_id)
    arr.call("DELETE", f"moviefile/{file_id}")
    if not movie.get("monitored"):
        arr.call("PUT", "movie/editor", json={"movieIds": [movie["id"]], "monitored": True})
    arr.call("POST", "command", json={"name": "MoviesSearch", "movieIds": [movie["id"]]})
    log_deletion(db, kind="bad-copy", name=title, media_type="movie", reason="manual", user_name=user_name,
                 detail=f"Replaced file; release {'blocklisted' if blocked else 'not blocklisted'}: "
                        f"{(grab or {}).get('sourceTitle') or 'unknown'}")
    logger.info(f"[BadCopy] {user_name or '?'} replaced '{title}' (blocklisted={blocked})")
    return {"ok": True, "blocklisted": blocked, "release": (grab or {}).get("sourceTitle"),
            "message": _outcome(title, grab, blocked)}


def replace_episode(db: Session, tmdb_id: int, season: int, episode: int, user_name: str = None) -> dict:
    arr = _Arr(db, "sonarr")
    series = next((s for s in arr.call("GET", "series") or [] if s.get("tmdbId") == tmdb_id), None)
    if not series:
        raise BadCopyError(404, "This show is not in Sonarr")
    ep = next((e for e in arr.call("GET", "episode", params={"seriesId": series["id"]}) or []
               if e.get("seasonNumber") == season and e.get("episodeNumber") == episode), None)
    label = f"S{season:02d}E{episode:02d}"
    if not ep:
        raise BadCopyError(404, f"Sonarr doesn't know {label}")
    if not ep.get("hasFile") or not ep.get("episodeFileId"):
        raise BadCopyError(409, f"Sonarr has no file for {label} (it may be an IPTV stream)")
    title = f"{series.get('title')} {label}"

    history = arr.call("GET", "history", params={"episodeId": ep["id"], "page": 1, "pageSize": 50,
                                                 "sortKey": "date", "sortDirection": "descending"})
    records = history.get("records", []) if isinstance(history, dict) else history
    grab = _latest_grab(records)
    blocked = False
    if grab:
        try:
            arr.call("POST", f"history/failed/{grab['id']}")
            blocked = True
        except BadCopyError as e:
            logger.warning(f"[BadCopy] Couldn't mark the grab of '{title}' failed: {e}")

    mark_replacing(db, "series", tmdb_id)
    arr.call("DELETE", f"episodefile/{ep['episodeFileId']}")
    arr.call("PUT", "episode/monitor", json={"episodeIds": [ep["id"]], "monitored": True})
    arr.call("POST", "command", json={"name": "EpisodeSearch", "episodeIds": [ep["id"]]})
    log_deletion(db, kind="bad-copy", name=title, media_type="series", reason="manual", user_name=user_name,
                 detail=f"Replaced file; release {'blocklisted' if blocked else 'not blocklisted'}: "
                        f"{(grab or {}).get('sourceTitle') or 'unknown'}")
    logger.info(f"[BadCopy] {user_name or '?'} replaced '{title}' (blocklisted={blocked})")
    return {"ok": True, "blocklisted": blocked, "release": (grab or {}).get("sourceTitle"),
            "message": _outcome(title, grab, blocked)}
