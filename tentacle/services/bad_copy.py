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
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import Setting, get_setting, log_deletion

logger = logging.getLogger(__name__)

REPLACING_FOR = timedelta(days=14)

# Radarr/Sonarr delete a file before they answer, and with a recycle bin on
# another drive they copy it there first: that can outlast the 30 s timeout,
# and they carry on and delete it all the same. So a delete that got no answer
# is checked every DELETE_POLL s until DELETE_WAIT after it was sent (its own
# 30 s included; the last check can take 30 s more): the answer still reaches
# the Jellyfin plugin (it stops listening at 240 s) and the TV app (250 s).
DELETE_WAIT = 150
DELETE_POLL = 5


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


# History events that put a file on disk: from a download (it carries the
# grab's downloadId), or from a library scan / manual copy (no grab made it).
_DOWNLOAD_IMPORT = "downloadfolderimported"
_FOLDER_IMPORTS = {"moviefolderimported", "seriesfolderimported"}


def _event(h: dict) -> str:
    return (h.get("eventType") or "").lower()


def _file_id(h: dict):
    """The file an import event put on disk (Radarr/Sonarr record "FileId")."""
    data = h.get("data") or {}
    value = data.get("fileId", data.get("FileId"))
    return None if value in (None, "") else str(value)


def _newest(events: list) -> Optional[dict]:
    return max(events, key=lambda h: h.get("date") or "", default=None)


def _grab_for_file(records: list, file_id) -> Optional[dict]:
    """The grab that produced the file on disk now, or None when none did.

    The newest grab is not always it: an upgrade still downloading, or a search
    someone started after the import, is newer -- and blocklisting that one
    left the bad release free to come back (#190). The import event says which
    file it put there (data.fileId) and which grab it came from (downloadId).
    """
    records = records or []
    grabs = [h for h in records if _event(h) == "grabbed"]
    imports = [h for h in records if _event(h) == _DOWNLOAD_IMPORT or _event(h) in _FOLDER_IMPORTS]
    if not imports:
        return _newest(grabs)       # nothing to go on: the newest grab, as before
    with_ids = [h for h in imports if _file_id(h) is not None]
    if with_ids:
        imp = _newest([h for h in with_ids if _file_id(h) == str(file_id)])
    else:
        # An older Radarr/Sonarr that does not record the file: the newest
        # import is the file there now.
        imp = _newest(imports)
    if imp is None or _event(imp) != _DOWNLOAD_IMPORT:
        return None     # a rescan or a manual copy put it there: no grab to blame
    download_id = imp.get("downloadId")
    if download_id:
        same = [g for g in grabs if g.get("downloadId") == download_id]
        if same:
            return _newest(same)
    # A client that grabs without an id (blackhole): the last grab before it that
    # has no id either. A grab carrying another download's id is another release:
    # an import whose own id no grab has (a download added to the client by hand)
    # blames nothing rather than whichever grab came before it.
    return _newest([g for g in grabs if not g.get("downloadId")
                    and (g.get("date") or "") <= (imp.get("date") or "")])


def _outcome(title: str, grab: Optional[dict], blocked: bool) -> str:
    if blocked:
        return f"Getting another copy of {title}. The bad release is blocklisted so it won't come back."
    if grab is None:
        return (f"Getting another copy of {title}. Couldn't tell which release the bad file came from, "
                "so the same one could be picked again.")
    return f"Getting another copy of {title}, but the bad release couldn't be blocklisted."


def _delete_file(arr: _Arr, file_path: str, file_id, item_path: str, file_of) -> None:
    """DELETE the file; return once it is gone, raise while it is still there.

    A failed delete may have happened all the same: Radarr/Sonarr still at it
    (DELETE_WAIT), or a reply lost. So the movie/episode (item_path; file_of
    gives the file it has) is read again, and only a file still there makes
    the error stand. A refusal is checked once, without waiting.
    """
    sent = time.monotonic()
    try:
        arr.call("DELETE", file_path)
        return
    except BadCopyError as e:
        error = e
    # A read timeout: the delete reached them and may still be under way.
    no_answer = isinstance(error.__context__, requests.ReadTimeout)
    deadline = sent + DELETE_WAIT if no_answer else 0
    while True:
        try:
            item = arr.call("GET", item_path)
            if isinstance(item, dict) and (item.get("hasFile") is False
                                           or file_of(item) not in (None, 0, file_id)):
                logger.warning(f"[BadCopy] The delete of {file_path} failed ({error}), but {arr.name} "
                               f"no longer has the file: carrying on")
                return
        except BadCopyError:
            pass    # no answer about the file: it may still be there
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(DELETE_POLL, remaining))
    if no_answer:
        raise BadCopyError(error.status, f"{arr.name} hasn't finished deleting the file yet. "
                                         "Once it's gone, use Search again to get another copy.")
    raise error


def _search_failed(title: str, e: BadCopyError) -> BadCopyError:
    """The file is deleted (and logged), but the search didn't start: say so."""
    logger.warning(f"[BadCopy] Deleted the file of '{title}', but couldn't start the search: {e}")
    return BadCopyError(e.status, f"The file of {title} is deleted, but the search for another copy "
                                  f"didn't start ({e}). Use Search again.")


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

    grab = _grab_for_file(arr.call("GET", "history/movie", params={"movieId": movie["id"]}), file_id)
    blocked = False
    if grab:
        try:
            arr.call("POST", f"history/failed/{grab['id']}")
            blocked = True
        except BadCopyError as e:
            logger.warning(f"[BadCopy] Couldn't mark the grab of '{title}' failed: {e}")

    mark_replacing(db, "movie", tmdb_id)
    _delete_file(arr, f"moviefile/{file_id}", file_id, f"movie/{movie['id']}",
                 lambda now: (now.get("movieFile") or {}).get("id"))
    log_deletion(db, kind="bad-copy", name=title, media_type="movie", reason="manual", user_name=user_name,
                 detail=f"Replaced file; release {'blocklisted' if blocked else 'not blocklisted'}: "
                        f"{(grab or {}).get('sourceTitle') or 'unknown'}")
    try:
        if not movie.get("monitored"):
            arr.call("PUT", "movie/editor", json={"movieIds": [movie["id"]], "monitored": True})
        arr.call("POST", "command", json={"name": "MoviesSearch", "movieIds": [movie["id"]]})
    except BadCopyError as e:
        raise _search_failed(title, e)
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
    grab = _grab_for_file(records, ep["episodeFileId"])
    blocked = False
    if grab:
        try:
            arr.call("POST", f"history/failed/{grab['id']}")
            blocked = True
        except BadCopyError as e:
            logger.warning(f"[BadCopy] Couldn't mark the grab of '{title}' failed: {e}")

    mark_replacing(db, "series", tmdb_id)
    _delete_file(arr, f"episodefile/{ep['episodeFileId']}", ep["episodeFileId"], f"episode/{ep['id']}",
                 lambda now: now.get("episodeFileId"))
    log_deletion(db, kind="bad-copy", name=title, media_type="series", reason="manual", user_name=user_name,
                 detail=f"Replaced file; release {'blocklisted' if blocked else 'not blocklisted'}: "
                        f"{(grab or {}).get('sourceTitle') or 'unknown'}")
    try:
        arr.call("PUT", "episode/monitor", json={"episodeIds": [ep["id"]], "monitored": True})
        arr.call("POST", "command", json={"name": "EpisodeSearch", "episodeIds": [ep["id"]]})
    except BadCopyError as e:
        raise _search_failed(title, e)
    logger.info(f"[BadCopy] {user_name or '?'} replaced '{title}' (blocklisted={blocked})")
    return {"ok": True, "blocklisted": blocked, "release": (grab or {}).get("sourceTitle"),
            "message": _outcome(title, grab, blocked)}
