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


# ── Fixing the match instead of removing it ──────────────────────────────────
# Usually the stream IS a real film, just not the one on the label. Tentacle
# can find it the way a person would: the stream's real length (Jellyfin's
# probe) plus the label's own words — a mislabel tends to be a similarly named
# film ("The Decline of Western Civilization" → "The Decline", 83 min = 83 min).

RUNTIME_CLOSE = 3        # minutes: "this is the length of that film"
MAX_SUGGESTIONS = 8


def _tmdb(db: Session):
    from services.tmdb import TMDBService, get_tmdb_token
    token = get_tmdb_token(db)
    if not token:
        raise WrongMatchError(503, "TMDB is not available")
    return TMDBService(token, get_setting(db, "data_dir", "/data"))


def _jf(db: Session):
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return None
    from services.jellyfin import JellyfinService
    return JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))


def probed_minutes(db: Session, row: Movie) -> Optional[int]:
    """The stream's real length from Jellyfin's probe, if it has been played."""
    jf = _jf(db)
    if jf is None:
        return None
    try:
        item_id = row.jellyfin_item_id
        if not item_id:
            found = jf.search_by_tmdb_id(row.tmdb_id, media_type="Movie")
            item_id = found["Id"] if found else None
        if not item_id:
            return None
        item = jf.get_item_by_id(item_id) or {}
        sources = item.get("MediaSources") or []
        ticks = (sources[0].get("RunTimeTicks") if sources else None) or 0
        return round(ticks / 600_000_000) or None
    except Exception as e:
        logger.debug(f"[WrongMatch] No probed length for tmdb:{row.tmdb_id}: {e}")
        return None


def _title_queries(title: str) -> list:
    """The label, then shorter and shorter prefixes of it (at least one real word)."""
    words = (title or "").split()
    out = []
    for n in range(len(words), 0, -1):
        q = " ".join(words[:n]).strip(" :-,")
        if q and q.lower() not in ("the", "a", "an") and q not in out:
            out.append(q)
        if len(out) >= 6:
            break
    return out


def suggest_matches(db: Session, tmdb_id: int, query: Optional[str] = None) -> dict:
    """Films this VOD movie might really be, best first."""
    from services.tmdb import TMDBService  # noqa: F401 (typing)
    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is None:
        raise WrongMatchError(404, "This movie is not in Tentacle's library")
    tmdb = _tmdb(db)
    actual = probed_minutes(db, row)
    queries = [query.strip()] if query and query.strip() else _title_queries(row.title)

    seen, found = {tmdb_id}, []
    for q in queries:
        data = tmdb._request("search/movie", {"query": q}) or {}
        for r in (data.get("results") or [])[:10]:
            if r.get("id") in seen:
                continue
            seen.add(r["id"])
            found.append(r)
        if len(found) >= 24:
            break

    in_lib = {t for (t,) in db.query(Movie.tmdb_id).filter(Movie.tmdb_id.in_([r["id"] for r in found])).all()} if found else set()
    label = (row.title or "").lower()
    candidates = []
    for r in found[:24]:
        details = tmdb.get_movie_details(r["id"]) or {}
        runtime = details.get("runtime") or None
        title = r.get("title") or details.get("title") or ""
        close = bool(actual and runtime and abs(runtime - actual) <= RUNTIME_CLOSE)
        candidates.append({
            "tmdb_id": r["id"], "title": title,
            "year": (r.get("release_date") or "")[:4] or None,
            "runtime": runtime, "poster_path": r.get("poster_path"),
            "overview": (r.get("overview") or "")[:240],
            "runtime_matches": close, "in_library": r["id"] in in_lib,
            "_sim": tmdb._similarity(label, title), "_pop": r.get("popularity") or 0,
        })

    def rank(c):
        if actual and c["runtime"]:
            gap = abs(c["runtime"] - actual)
        else:
            gap = 999
        # Length first (when known), then how much of the label it shares, then fame.
        return (0 if c["runtime_matches"] else 1, gap if actual else 0, -c["_sim"], -c["_pop"])

    candidates.sort(key=rank)
    for c in candidates:
        c.pop("_sim"), c.pop("_pop")
    return {
        "current": {"tmdb_id": row.tmdb_id, "title": row.title, "year": row.year, "runtime": row.runtime},
        "actual_minutes": actual,
        "searched": queries,
        "candidates": candidates[:MAX_SUGGESTIONS],
    }


def rematch_movie(db: Session, tmdb_id: int, new_tmdb_id: int, user_name: str = None) -> dict:
    """This VOD stream is really `new_tmdb_id`: move the copy to that film.

    Same stream, new identity: the .strm and NFO move to the right film's
    folder with its metadata, tags tied to the old film (lists, rules) are
    recomputed, and a MatchOverride keeps the sync from undoing it. If the
    right film is already in the library, this copy is simply a duplicate —
    it is removed and its stream blocked instead.
    """
    from services.media_files import delete_movie_files
    from services.nfo import vod_folder_name, write_movie_nfo
    from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id
    from models.database import Duplicate, MatchOverride

    if new_tmdb_id == tmdb_id:
        raise WrongMatchError(400, "That is the film it is already matched to")
    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is None:
        raise WrongMatchError(404, "This movie is not in Tentacle's library")
    if not (row.source or "").startswith("provider_") or not row.provider_id:
        raise WrongMatchError(400, "Only IPTV (VOD) titles can be re-matched — this one is a download")
    try:
        stream_url = Path(row.strm_path).read_text(encoding="utf-8").strip()
    except (OSError, TypeError):
        stream_url = ""
    key = stream_key_for_url(stream_url)
    if not key:
        raise WrongMatchError(409, "Could not read which provider stream this title plays (its .strm file is missing)")

    if db.query(Movie).filter(Movie.tmdb_id == new_tmdb_id).first() is not None:
        result = block_and_remove_movie(db, tmdb_id, user_name=user_name,
                                        reason=f"really tmdb:{new_tmdb_id}, already in library")
        result["message"] = "That film is already in your library — removed this duplicate copy"
        result["merged"] = True
        return result

    new = _tmdb(db).get_movie_details(new_tmdb_id)
    if not new:
        raise WrongMatchError(404, "TMDB has no movie with that id")

    old_meta = {"tmdb_id": row.tmdb_id, "title": row.title, "year": row.year, "genres": row.genres or [],
                "rating": row.rating, "runtime": row.runtime}
    source = row.source
    stale = set(get_list_tags_for_tmdb_id(row.tmdb_id, "movie", db)) \
        | set(apply_tag_rules(old_meta, "movie", source, row.source_tag, db))
    tags = [t for t in (row.tags or []) if t not in stale]
    new_meta = dict(new, tags=tags)
    for t in list(get_list_tags_for_tmdb_id(new_tmdb_id, "movie", db)) \
            + list(apply_tag_rules(new_meta, "movie", source, row.source_tag, db)):
        if t not in tags:
            tags.append(t)

    old_strm, old_title, old_jf = row.strm_path, row.title, row.jellyfin_item_id
    root = Path(old_strm).parent.parent
    folder = vod_folder_name(new.get("title") or "", new.get("year"))
    new_dir = root / folder
    new_strm, new_nfo = new_dir / f"{folder}.strm", new_dir / f"{folder}.nfo"
    try:
        from services.sync import chown_path
    except Exception:  # pragma: no cover
        def chown_path(_p):
            return None
    new_dir.mkdir(parents=True, exist_ok=True)
    chown_path(new_dir)
    new_strm.write_text(stream_url, encoding="utf-8")
    chown_path(new_strm)
    write_movie_nfo(new_nfo, new, tags)
    chown_path(new_nfo)
    if Path(old_strm) != new_strm:
        delete_movie_files(old_strm)

    # A duplicate record pairing the old film with THIS stream no longer holds.
    for dup in db.query(Duplicate).filter(Duplicate.tmdb_id == tmdb_id, Duplicate.media_type == "movie").all():
        if any((src or {}).get("path") == old_strm for src in (dup.sources or [])):
            db.delete(dup)
    row.tmdb_id = new_tmdb_id
    row.title = new.get("title") or row.title
    row.year = new.get("year")
    row.overview = new.get("overview")
    row.runtime = new.get("runtime")
    row.rating = new.get("rating")
    row.genres = new.get("genres") or []
    row.poster_path = new.get("poster_path")
    row.backdrop_path = new.get("backdrop_path")
    row.strm_path, row.nfo_path = str(new_strm), str(new_nfo)
    row.tags = tags
    row.jellyfin_item_id = None
    ov = db.query(MatchOverride).filter(MatchOverride.provider_id == row.provider_id,
                                        MatchOverride.media_type == "movie",
                                        MatchOverride.stream_key == key).first()
    if ov is None:
        db.add(MatchOverride(provider_id=row.provider_id, media_type="movie", stream_key=key,
                             tmdb_id=new_tmdb_id, previous_tmdb_id=tmdb_id, title=row.title, set_by=user_name))
    else:
        ov.tmdb_id, ov.title, ov.set_by = new_tmdb_id, row.title, user_name
    db.query(MatchSuspect).filter(MatchSuspect.tmdb_id == tmdb_id,
                                  MatchSuspect.media_type == "movie").delete()
    db.commit()

    log_deletion(db, kind="rematch", name=old_title, media_type="movie", reason="manual", user_name=user_name,
                 detail=f"Stream {key if key.isdigit() else '(URL)'} re-matched: '{old_title}' (tmdb:{tmdb_id}) "
                        f"→ '{row.title}' (tmdb:{new_tmdb_id})")
    logger.info(f"[WrongMatch] Re-matched stream {key if key.isdigit() else '(URL)'}: '{old_title}' → "
                f"'{row.title}' ({row.year}) by {user_name}")

    # The old Jellyfin item's files are gone; remove it now (its delete hook finds
    # nothing under the old id — the row carries the new one) and scan so the
    # new folder comes in with the right metadata.
    _delete_from_jellyfin(db, tmdb_id, old_jf)
    jf = _jf(db)
    if jf is not None:
        try:
            jf.trigger_library_scan(None)
        except Exception as e:
            logger.debug(f"[WrongMatch] Library scan request failed: {e}")
    _refresh_caches()
    return {"ok": True, "title": row.title, "year": row.year, "tmdb_id": new_tmdb_id,
            "message": f"Fixed: this is {row.title} ({row.year}). Jellyfin is picking it up now."}


def override_keys(db: Session, provider_id: int, media_type: str = "movie") -> dict:
    from models.database import MatchOverride
    return {o.stream_key: o.tmdb_id for o in db.query(MatchOverride).filter(
        MatchOverride.provider_id == provider_id, MatchOverride.media_type == media_type).all()}


def override_for(overrides: dict, stream_id, url: str = "") -> Optional[int]:
    if not overrides:
        return None
    if stream_id is not None and str(stream_id) in overrides:
        return overrides[str(stream_id)]
    return overrides.get(url) if url else None
