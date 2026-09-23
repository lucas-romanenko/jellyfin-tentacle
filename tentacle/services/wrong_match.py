"""Mislabelled provider streams: block them, remove them, and spot them.

An IPTV provider's catalogue is just names and stream numbers, and Tentacle can
only trust the name. When the provider mislabels a stream — found live: stream
188327, listed as "The Decline of Western Civilization" (1981), actually plays
the 2020 Netflix film "The Decline" — Tentacle files it under the wrong film:
the right poster, "In Library", and the wrong movie on Play. Worse, it hides
the real title: a Radarr request for it looked already satisfied.

- block_and_remove_movie(): the admin "Wrong movie" action. Blocks the stream
  so no sync re-imports it, then removes Tentacle's copy everywhere.
- check_runtime_mismatches(): Jellyfin probes a .strm on first play; a probed
  length far from TMDB's runtime flags the title for the admin.
"""
import logging
import re
import threading
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (
    BlockedStream, MatchSuspect, Movie, get_setting, log_deletion,
)

logger = logging.getLogger(__name__)

# Xtream movie URL: {server}/movie/{user}/{pass}/{stream_id}.{ext}
_XTREAM_MOVIE = re.compile(r"/movie/[^/]+/[^/]+/(\d+)\.[A-Za-z0-9]+$")

# A probed length this far from TMDB's is a different film, not a different cut:
# both absolute (trailers/credits vary by a few minutes) and relative.
MISMATCH_MINUTES = 12
MISMATCH_FRACTION = 0.15


def stream_key_for_url(url: str) -> Optional[str]:
    """The key a stream is blocked under: its Xtream id, else the whole URL."""
    url = (url or "").strip()
    if not url:
        return None
    m = _XTREAM_MOVIE.search(url)
    return m.group(1) if m else url


def stream_key_from_strm(strm_path: str) -> Optional[str]:
    try:
        return stream_key_for_url(Path(strm_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None


def blocked_keys(db: Session, provider_id: int, media_type: str = "movie") -> set:
    return {k for (k,) in db.query(BlockedStream.stream_key).filter(
        BlockedStream.provider_id == provider_id,
        BlockedStream.media_type == media_type,
    ).all()}


def is_blocked(keys: set, stream_id, url: str = "") -> bool:
    """Whether a catalogue entry is blocked — by id, or by URL for M3U."""
    if not keys:
        return False
    if stream_id is not None and str(stream_id) in keys:
        return True
    return bool(url) and url in keys


class WrongMatchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def block_and_remove_movie(db: Session, tmdb_id: int, user_name: str = None,
                           reason: str = "wrong movie") -> dict:
    """Block the provider stream behind a VOD movie and remove the movie.

    Order matters: Tentacle's record goes before the Jellyfin item. Deleting
    the Jellyfin item fires the plugin's delete hook, whose clean-up also drops
    the title's download request — and a request for the REAL film (Radarr
    searching for it) is exactly what must survive. With the record already
    gone, the hook finds nothing to clean.
    """
    from services.media_files import delete_movie_files

    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is None:
        raise WrongMatchError(404, "This movie is not in Tentacle's library")
    if not (row.source or "").startswith("provider_") or not row.provider_id:
        raise WrongMatchError(400, "Only IPTV (VOD) titles can be reported — this one is a download")
    key = stream_key_from_strm(row.strm_path)
    if not key:
        raise WrongMatchError(409, "Could not read which provider stream this title plays, so it "
                                   "cannot be blocked (its .strm file is missing)")

    title, provider_id, jf_item_id = row.title, row.provider_id, row.jellyfin_item_id
    exists = db.query(BlockedStream).filter(
        BlockedStream.provider_id == provider_id,
        BlockedStream.media_type == "movie",
        BlockedStream.stream_key == key,
    ).first()
    if exists is None:
        db.add(BlockedStream(provider_id=provider_id, media_type="movie", stream_key=key,
                             tmdb_id=tmdb_id, title=title, reason=reason, blocked_by=user_name))

    files = delete_movie_files(row.strm_path)
    db.delete(row)
    db.query(MatchSuspect).filter(MatchSuspect.tmdb_id == tmdb_id,
                                  MatchSuspect.media_type == "movie").delete()
    db.commit()

    shown_key = key if key.isdigit() else "its stream URL"
    log_deletion(db, kind="wrong-match", name=title, media_type="movie", reason="manual",
                 user_name=user_name,
                 detail=f"Provider {provider_id} stream {shown_key} blocked ({reason}); "
                        f"{files} VOD file(s) removed")
    logger.info(f"[WrongMatch] '{title}' (tmdb:{tmdb_id}): blocked provider {provider_id} "
                f"stream {shown_key}, removed {files} file(s) — by {user_name}")

    jf_deleted = _delete_from_jellyfin(db, tmdb_id, jf_item_id)
    _refresh_caches()
    return {"ok": True, "title": title, "blocked": shown_key, "files_removed": files,
            "jellyfin_deleted": jf_deleted,
            "message": f"Removed the wrong copy of {title} and blocked that stream"}


def _delete_from_jellyfin(db: Session, tmdb_id: int, jf_item_id: Optional[str]) -> bool:
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return False
    try:
        from services.jellyfin import JellyfinService
        jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
        if not jf_item_id:
            found = jf.search_by_tmdb_id(tmdb_id, media_type="Movie")
            jf_item_id = found["Id"] if found else None
        if not jf_item_id:
            return False
        ok = jf.delete_item(jf_item_id)
    except Exception as e:
        logger.warning(f"[WrongMatch] Could not delete tmdb:{tmdb_id} from Jellyfin: {e}")
        return False
    if ok:
        from routers.library import _cleanup_playlists_all_users
        threading.Thread(target=_cleanup_playlists_all_users,
                         args=(tmdb_id, "movie", jf_item_id), daemon=True).start()
    return ok


def _refresh_caches() -> None:
    """In-library state changed: Discover badges and Activity must re-read it."""
    for mod, fn in (("routers.activity", "invalidate_wanted_cache"),
                    ("routers.discover", "bust_arr_ids_cache"),
                    ("routers.discover", "bust_known_ids_cache")):
        try:
            getattr(__import__(mod, fromlist=[fn]), fn)()
        except Exception:
            pass


# ── Detection ────────────────────────────────────────────────────────────────

def is_mismatch(expected_minutes, actual_minutes) -> bool:
    if not expected_minutes or not actual_minutes or expected_minutes <= 0 or actual_minutes <= 0:
        return False
    diff = abs(actual_minutes - expected_minutes)
    return diff >= MISMATCH_MINUTES and diff / expected_minutes >= MISMATCH_FRACTION


def check_runtime_mismatches(db: Session) -> dict:
    """Flag VOD movies whose probed length is far from their TMDB runtime.

    One Jellyfin listing; only titles Jellyfin has probed (played at least once)
    carry a real length, so the set grows as the library gets watched. A
    dismissed flag stays dismissed; flags for titles that are gone or now match
    are cleared.
    """
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return {"checked": 0, "flagged": 0}
    from services.jellyfin import JellyfinService
    jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
    items = jf.query_movies_with_media_sources()
    if items is None:
        return {"checked": 0, "flagged": 0, "error": "Jellyfin listing failed"}

    expected = {m.tmdb_id: (m.runtime, m.title) for m in db.query(
        Movie.tmdb_id, Movie.runtime, Movie.title).filter(Movie.source.like("provider_%")).all()}
    existing = {s.tmdb_id: s for s in db.query(MatchSuspect).filter(MatchSuspect.media_type == "movie").all()}

    checked, flagged, still = 0, 0, set()
    for item in items:
        try:
            tmdb_id = int((item.get("ProviderIds") or {}).get("Tmdb") or 0)
        except (TypeError, ValueError):
            continue
        if tmdb_id not in expected:
            continue
        sources = item.get("MediaSources") or []
        ticks = (sources[0].get("RunTimeTicks") if sources else None) or 0
        actual = round(ticks / 600_000_000) if ticks else 0
        runtime, title = expected[tmdb_id]
        if not actual or not runtime:
            continue
        checked += 1
        if not is_mismatch(runtime, actual):
            continue
        still.add(tmdb_id)
        s = existing.get(tmdb_id)
        if s is None:
            db.add(MatchSuspect(tmdb_id=tmdb_id, media_type="movie", title=title,
                                expected_minutes=runtime, actual_minutes=actual,
                                jellyfin_item_id=item.get("Id")))
            flagged += 1
            logger.warning(f"[WrongMatch] '{title}' plays {actual} min but TMDB says {runtime} — "
                           f"probably a different film under the provider's label")
        else:
            s.expected_minutes, s.actual_minutes = runtime, actual
    # Flags whose title is gone, or that no longer mismatch (unless dismissed —
    # the admin's call stands).
    for tmdb_id, s in existing.items():
        if tmdb_id not in still and not s.dismissed:
            db.delete(s)
    db.commit()
    logger.info(f"[WrongMatch] Runtime check: {checked} probed VOD movie(s) compared, {flagged} newly flagged")
    return {"checked": checked, "flagged": flagged}
