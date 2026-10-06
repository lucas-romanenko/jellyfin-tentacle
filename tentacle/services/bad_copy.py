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

Radarr/Sonarr answer the file DELETE only once the file is gone; with a
recycle bin on another drive, or a slow share, that is a full copy and can
outlast the 30 s request timeout while they carry on and delete it (#442).
So a failed DELETE is not the end: Tentacle checks whether the file is gone,
waits a while for a delete that timed out, and finishes (Deletion log,
monitor, search) as soon as it is; a delete still running when the button
must answer is finished in the background.
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import Setting, get_setting, log_deletion

logger = logging.getLogger(__name__)

REPLACING_FOR = timedelta(days=14)
# A delete that timed out: how long the button keeps checking for the file to
# be gone (the plugin gives up after 240 s, the TV app after 250 s), then how
# long the background keeps checking.
SLOW_DELETE_WAIT = 120
BACKGROUND_WAIT = 3600


class BadCopyError(Exception):
    def __init__(self, status: int, message: str, timed_out: bool = False):
        super().__init__(message)
        self.status = status
        self.timed_out = timed_out


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
        except requests.Timeout as e:
            raise BadCopyError(502, f"Couldn't reach {self.name}: {e}", timed_out=True)
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


def _gone_within(is_gone, seconds: float, every: float) -> bool:
    """Whether is_gone() turns true within `seconds` (checked at once, then
    every `every` s). A check that fails counts as not gone yet."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            if is_gone():
                return True
        except BadCopyError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(every)


def _delete_and_search(db: Session, arr: _Arr, *, delete: str, is_gone, search, title: str,
                       media_type: str, user_name: str, grab: Optional[dict], blocked: bool) -> dict:
    """Delete the file, then (once it is gone) log it and start the search."""
    release = (grab or {}).get("sourceTitle")

    def finish(session: Session) -> None:
        log_deletion(session, kind="bad-copy", name=title, media_type=media_type, reason="manual",
                     user_name=user_name,
                     detail=f"Replaced file; release {'blocklisted' if blocked else 'not blocklisted'}: "
                            f"{release or 'unknown'}")
        try:
            search()
        except BadCopyError as e:
            raise BadCopyError(502, f"Deleted the bad file of {title}, but couldn't start the search for "
                                    f"another copy ({e}). Search for it in {arr.name}.")
        logger.info(f"[BadCopy] {user_name or '?'} replaced '{title}' (blocklisted={blocked})")

    try:
        arr.call("DELETE", delete)
    except BadCopyError as e:
        # The delete may have gone through anyway: after a timeout it is still
        # running (Radarr/Sonarr carry on without us).
        if not _gone_within(is_gone, SLOW_DELETE_WAIT if e.timed_out else 0, 5):
            if not e.timed_out:
                raise
            logger.warning(f"[BadCopy] {arr.name} is still deleting the file of '{title}'; "
                           "the search starts once it's gone")
            _finish_in_background(is_gone, finish, title)
            return {"ok": True, "blocklisted": blocked, "release": release, "pending": True,
                    "message": f"{arr.name} is still deleting the bad file of {title}. "
                               "The search for another copy starts as soon as it's gone."}
        logger.warning(f"[BadCopy] {arr.name} deleted the file of '{title}' although the delete failed: {e}")
    finish(db)
    return {"ok": True, "blocklisted": blocked, "release": release, "message": _outcome(title, grab, blocked)}


def _finish_in_background(is_gone, finish, title: str) -> None:
    def run():
        if not _gone_within(is_gone, BACKGROUND_WAIT, 15):
            logger.warning(f"[BadCopy] The file of '{title}' is still there after {BACKGROUND_WAIT // 60} min: "
                           "no search started")
            return
        from models.database import SessionLocal
        db = SessionLocal()
        try:
            finish(db)
        except BadCopyError as e:
            logger.warning(f"[BadCopy] {e}")
        except Exception:
            logger.exception(f"[BadCopy] Finishing the replacement of '{title}' failed")
        finally:
            db.close()
    threading.Thread(target=run, daemon=True, name="bad-copy-finish").start()


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

    def is_gone():
        now = arr.call("GET", f"movie/{movie['id']}") or {}
        return not now.get("hasFile") or (now.get("movieFile") or {}).get("id") != file_id

    def search():
        # After the delete: deleting a file unmonitors the movie when Radarr's
        # "Unmonitor Deleted Movies" is on.
        if not movie.get("monitored"):
            arr.call("PUT", "movie/editor", json={"movieIds": [movie["id"]], "monitored": True})
        arr.call("POST", "command", json={"name": "MoviesSearch", "movieIds": [movie["id"]]})

    return _delete_and_search(db, arr, delete=f"moviefile/{file_id}", is_gone=is_gone, search=search,
                              title=title, media_type="movie", user_name=user_name, grab=grab, blocked=blocked)


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

    def is_gone():
        now = arr.call("GET", f"episode/{ep['id']}") or {}
        return not now.get("hasFile") or now.get("episodeFileId") != ep["episodeFileId"]

    def search():
        arr.call("PUT", "episode/monitor", json={"episodeIds": [ep["id"]], "monitored": True})
        arr.call("POST", "command", json={"name": "EpisodeSearch", "episodeIds": [ep["id"]]})

    return _delete_and_search(db, arr, delete=f"episodefile/{ep['episodeFileId']}", is_gone=is_gone,
                              search=search, title=title, media_type="series", user_name=user_name,
                              grab=grab, blocked=blocked)
