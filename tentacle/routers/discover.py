"""
Tentacle - Discover Router
Trending, popular, upcoming content from TMDB + missing from user lists
"""

import hashlib
import logging
import threading
import random
import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
import httpx
from models.database import get_db, get_setting, Movie, Series, ListSubscription, ListItem, DownloadRequest, LiveChannel, TentacleUser
from routers.auth import get_user_from_request
from services.cleaner import clean_list_title
from services.ssrf import is_safe_url
from services.tmdb import TMDBService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discover", tags=["discover"])


def _get_tmdb(db: Session) -> Optional[TMDBService]:
    from services.tmdb import get_tmdb_token
    bearer = get_tmdb_token(db)
    data_dir = get_setting(db, "data_dir", "/data")
    if not bearer:
        return None
    return TMDBService(bearer, data_dir)


_jf_server_id_cache: dict = {"id": None, "checked": False}


def _jellyfin_web_url(db: Session, item_id: str):
    """Deep link to an item in Jellyfin's web UI, or None if not configured."""
    base = (get_setting(db, "jellyfin_url") or "").rstrip("/")
    if not base or not item_id:
        return None
    # serverId is optional in the route but makes the link work from a client
    # that has more than one server configured. Resolved once per process.
    # Only latch once a lookup actually succeeds. Marking it checked up front
    # meant an unreachable Jellyfin on the first call left every deep link
    # without a serverId until Tentacle restarted.
    if not _jf_server_id_cache["checked"]:
        try:
            from services.jellyfin import JellyfinService
            api_key = get_setting(db, "jellyfin_api_key")
            if api_key:
                server_id = JellyfinService(base, api_key).get_server_id()
                if server_id:
                    _jf_server_id_cache["id"] = server_id
                    _jf_server_id_cache["checked"] = True
        except Exception as e:
            logger.debug(f"Could not resolve Jellyfin server id: {e}")
    server_id = _jf_server_id_cache["id"]
    suffix = f"&serverId={server_id}" if server_id else ""
    return f"{base}/web/#/details?id={item_id}{suffix}"


def _known_tmdb_ids(db: Session) -> dict:
    """Separate sets of TMDB IDs by media type (TMDB uses separate ID spaces for movies vs series)."""
    movie_ids = {m.tmdb_id for m in db.query(Movie.tmdb_id).all()}
    series_ids = {s.tmdb_id for s in db.query(Series.tmdb_id).all()}
    return {"movie": movie_ids, "series": series_ids}


# ── Jellyfin is the authority on "can the user watch this right now" ──
# Tentacle's Movie/Series tables are essentially the provider VOD catalog, so
# anything Jellyfin has that Tentacle's scanners never recorded (downloaded
# content added outside Tentacle, titles that predate a scan) looked addable in
# Discover — and adding it then reported "already in Radarr/Sonarr". Jellyfin
# sees both downloaded files and .strm, so it closes that gap for good.
_jf_ids_cache: dict = {"movie": None, "series": None, "ts": {"movie": 0, "series": 0}}
_jf_ids_locks = {"movie": threading.Lock(), "series": threading.Lock()}
JF_IDS_TTL = 300  # 5 minutes, matching ARR_IDS_TTL
# A failed or partial fetch is retried soon rather than being trusted for the
# full TTL — caching "Jellyfin has nothing" made every owned title look
# addable, and every .strm title lose its Watch link, for five minutes.
JF_IDS_FAILURE_TTL = 30


def _get_jellyfin_tmdb_items(media_type: str) -> dict:
    """Cached {tmdb_id: jellyfin_item_id} for one media type.

    Fetched lazily per type: browsing movies must not pay for a full series
    library fetch. Only a COMPLETE fetch is cached for the full TTL; a failure
    or a partial page keeps the previous map (if any) and is retried in
    JF_IDS_FAILURE_TTL seconds. An unreachable Jellyfin with no previous map
    degrades to the DB-only behaviour rather than failing the page.
    """
    import time as _time
    key = "series" if media_type == "series" else "movie"

    def _fresh() -> bool:
        return _jf_ids_cache[key] is not None and _time.time() - _jf_ids_cache["ts"][key] < JF_IDS_TTL

    if _fresh():
        return _jf_ids_cache[key]

    # One fetch at a time per media type: several Discover rows resolve in
    # parallel threads, and without this each one runs its own full library
    # fetch the moment the cache expires.
    with _jf_ids_locks[key]:
        if _fresh():
            return _jf_ids_cache[key]

        from models.database import SessionLocal
        out = None
        complete = False
        db = SessionLocal()
        try:
            url = get_setting(db, "jellyfin_url")
            api_key = get_setting(db, "jellyfin_api_key")
            jf_user = get_setting(db, "jellyfin_user_id", "")
            if url and api_key:
                from services.jellyfin import JellyfinService
                jf = JellyfinService(url, api_key, jf_user)
                # user_scoped: link the item users are shown, not a hidden
                # merged-version alternate (e.g. the .strm next to a download).
                lookup, complete = jf.get_tmdb_lookup_checked(
                    "Series" if key == "series" else "Movie", user_scoped=True
                )
                out = {tid: item.get("Id") for tid, item in lookup.items() if item.get("Id")}
        except Exception as e:
            logger.warning(f"Jellyfin id fetch for in-library check failed: {e}")
        finally:
            db.close()

        now = _time.time()
        if out is not None and complete:
            _jf_ids_cache[key] = out
            _jf_ids_cache["ts"][key] = now
            return out

        # Incomplete or failed. Keep whatever we had rather than replacing a
        # good map with a worse one, and come back sooner than the full TTL.
        previous = _jf_ids_cache[key]
        if previous is not None:
            logger.warning(
                f"Jellyfin {key} listing was incomplete — keeping the previous map "
                f"({len(previous)} items) and retrying in {JF_IDS_FAILURE_TTL}s"
            )
            _jf_ids_cache["ts"][key] = now - JF_IDS_TTL + JF_IDS_FAILURE_TTL
            return previous

        # Nothing cached yet: serve what we got but don't treat it as the truth.
        partial = out or {}
        _jf_ids_cache[key] = partial
        _jf_ids_cache["ts"][key] = now - JF_IDS_TTL + JF_IDS_FAILURE_TTL
        return partial


def _jellyfin_item_id(tmdb_id: int, media_type: str):
    """Jellyfin item id for a TMDB id, or None. Used to offer Play / Open."""
    if not tmdb_id:
        return None
    return _get_jellyfin_tmdb_items(media_type).get(tmdb_id)


def _bust_jellyfin_ids_cache():
    _jf_ids_cache["ts"] = {"movie": 0, "series": 0}


def _is_in_library(item: dict, known_ids: dict) -> bool:
    """Check if item is in library using the correct media-type-specific ID set.

    Tentacle's own tables first (cheap), then Jellyfin as the authority.
    """
    tid = item.get("tmdb_id")
    if not tid:
        return False
    mt = item.get("media_type", "movie")
    if mt == "series":
        if tid in known_ids["series"]:
            return True
        return tid in _get_jellyfin_tmdb_items("series")
    elif mt == "movie":
        if tid in known_ids["movie"]:
            return True
        return tid in _get_jellyfin_tmdb_items("movie")
    # Unknown type — check both (backward compat)
    if tid in known_ids["movie"] or tid in known_ids["series"]:
        return True
    return tid in _get_jellyfin_tmdb_items("movie") or tid in _get_jellyfin_tmdb_items("series")


# ── "Requested" annotation: titles known to Radarr/Sonarr with no file yet ──
# A movie added to Radarr that hasn't downloaded (searching, or no findable
# release) is otherwise invisible: not in the Tentacle DB (scans import
# files), not "unreleased" (already released), not in the download queue.
# Without this, Discover shows it as addable — and adding reports
# "Already in Radarr".
_arr_ids_cache: dict = {"data": None, "ts": 0}
ARR_IDS_TTL = 300  # 5 minutes


def _get_arr_tmdb_ids() -> dict:
    """Cached tmdb-id sets of everything Radarr/Sonarr track (incl. no-file)."""
    import time as _time
    now = _time.time()
    if _arr_ids_cache["data"] is not None and now - _arr_ids_cache["ts"] < ARR_IDS_TTL:
        return _arr_ids_cache["data"]

    from models.database import SessionLocal
    result = {"movie": set(), "series": set()}
    db = SessionLocal()
    try:
        try:
            url, key = get_setting(db, "radarr_url"), get_setting(db, "radarr_api_key")
            if url and key:
                from services.radarr import RadarrService
                for m in RadarrService(url, key).get_all_movies():
                    if m.get("tmdbId"):
                        result["movie"].add(m["tmdbId"])
        except Exception as e:
            logger.debug(f"Radarr id fetch for requested-badges failed: {e}")
        try:
            url, key = get_setting(db, "sonarr_url"), get_setting(db, "sonarr_api_key")
            if url and key:
                from services.sonarr import SonarrService
                for s in SonarrService(url, key).get_all_series():
                    if s.get("tmdbId"):
                        result["series"].add(s["tmdbId"])
        except Exception as e:
            logger.debug(f"Sonarr id fetch for requested-badges failed: {e}")
    finally:
        db.close()

    _arr_ids_cache["data"] = result
    _arr_ids_cache["ts"] = now
    return result


def bust_arr_ids_cache():
    """Called after add-to-arr so new requests badge immediately.

    Deliberately does NOT clear the Jellyfin map: a title just handed to
    Radarr/Sonarr has no file yet, so Jellyfin's answer cannot have changed.
    Clearing it only forced a full library re-fetch after every add — N of them
    during a bulk "add missing".
    """
    _arr_ids_cache["ts"] = 0


def _mark_requested(item: dict):
    """requested=True when the title is in Radarr/Sonarr but not in the library."""
    tid = item.get("tmdb_id")
    if item.get("in_library") or not tid:
        item["requested"] = False
        return
    ids = _get_arr_tmdb_ids()
    mt = item.get("media_type", "movie")
    item["requested"] = tid in (ids["series"] if mt == "series" else ids["movie"])


def _dedup_and_mark(items: list, known_ids: dict) -> list:
    """Deduplicate by tmdb_id+media_type and annotate in_library status."""
    seen = set()
    result = []
    for item in items:
        tid = item.get("tmdb_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        item["in_library"] = _is_in_library(item, known_ids)
        _mark_requested(item)
        result.append(item)
    return result


@router.get("")
def get_discover(
    type: str = "movies",
    db: Session = Depends(get_db),
    user: TentacleUser = Depends(get_user_from_request),
):
    """Return discover sections based on media type.
    Movies: Popular, Now Playing, Upcoming, From Your Lists
    TV: Popular, On the Air, Top Rated, From Your Lists
    """
    tmdb = _get_tmdb(db)
    if not tmdb:
        return {"sections": []}

    known_ids = _known_tmdb_ids(db)
    sections = []

    if type == "series":
        # ── TV: Popular ──
        popular = tmdb.get_popular("series")
        if popular:
            sections.append({
                "id": "popular",
                "title": "Popular",
                "items": _dedup_and_mark(popular, known_ids),
            })

        # ── TV: On the Air ──
        on_the_air = tmdb.get_on_the_air()
        if on_the_air:
            sections.append({
                "id": "on_the_air",
                "title": "On the Air",
                "items": _dedup_and_mark(on_the_air, known_ids),
            })

        # ── TV: Top Rated ──
        top_rated = tmdb.get_top_rated("series")
        if top_rated:
            sections.append({
                "id": "top_rated",
                "title": "Top Rated",
                "items": _dedup_and_mark(top_rated, known_ids),
            })
    else:
        # ── Movies: Popular ──
        popular = tmdb.get_popular("movie")
        if popular:
            sections.append({
                "id": "popular",
                "title": "Popular",
                "items": _dedup_and_mark(popular, known_ids),
            })

        # ── Movies: Now Playing ──
        now_playing = tmdb.get_now_playing()
        if now_playing:
            sections.append({
                "id": "now_playing",
                "title": "Now Playing",
                "items": _dedup_and_mark(now_playing, known_ids),
            })

        # ── Movies: Upcoming ──
        upcoming = tmdb.get_upcoming()
        if upcoming:
            sections.append({
                "id": "upcoming",
                "title": "Upcoming",
                "items": _dedup_and_mark(upcoming, known_ids),
            })

    # ── From Your Lists (both types) ──
    missing = _get_missing_from_lists(db, known_ids, type, user)
    if missing:
        sections.append({
            "id": "missing",
            "title": "From My Lists",
            "items": missing,
        })

    return {"sections": sections}


def _get_missing_from_lists(db: Session, known_ids: dict, type_filter: str, user: TentacleUser = None) -> list:
    """Get items from active list subscriptions that aren't in the library."""
    query = db.query(ListSubscription).filter(ListSubscription.active == True)
    if user:
        query = query.filter(ListSubscription.user_id == user.id)
    active_lists = query.all()

    if not active_lists:
        return []

    list_ids = [ls.id for ls in active_lists]
    list_names = {ls.id: ls.name for ls in active_lists}

    query = db.query(ListItem).filter(
        ListItem.list_id.in_(list_ids),
        ListItem.tmdb_id.isnot(None),
    )
    if type_filter == "movies":
        query = query.filter(ListItem.media_type == "movie")
    elif type_filter == "series":
        query = query.filter(ListItem.media_type == "series")

    all_items = query.all()

    seen = set()
    result = []
    for item in all_items:
        mt = item.media_type or "movie"
        type_ids = known_ids.get("series" if mt == "series" else "movie", set())
        if item.tmdb_id in type_ids or item.tmdb_id in seen:
            continue
        if not item.poster_path:
            continue
        seen.add(item.tmdb_id)
        # Clean pre-fix rows (HTML entities + baked-in year) at serving time
        clean_name, clean_year = clean_list_title(item.title, item.year)
        result.append({
            "tmdb_id": item.tmdb_id,
            "title": clean_name or "Unknown",
            "year": clean_year or "",
            "overview": "",
            "rating": 0,
            "poster_path": item.poster_path,
            "backdrop_path": None,
            "media_type": item.media_type or "movie",
            "in_library": False,
            "list_name": list_names.get(item.list_id, ""),
        })

    # Shuffle and cap at 40
    random.shuffle(result)
    return result[:40]


@router.get("/detail/{media_type}/{tmdb_id}")
def get_discover_detail(
    media_type: str,
    tmdb_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Fetch full TMDB details for a single item (used by modal)."""
    tmdb = _get_tmdb(db)
    if not tmdb:
        return {"error": "TMDB not configured"}

    if media_type == "series":
        details = tmdb.get_series_details(tmdb_id)
    else:
        details = tmdb.get_movie_details(tmdb_id)

    if not details:
        return {"error": "Not found"}

    # Enrich with library source info + can_delete permission
    db_item = None
    if media_type == "series":
        db_item = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
        if db_item:
            details["in_library"] = True
            details["library_source"] = db_item.source
        else:
            details["in_library"] = False
    else:
        db_item = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
        details["in_library"] = bool(db_item)

    # Resolve a Jellyfin item id so an owned title can actually be opened and
    # played. VOD (.strm) rows essentially never have jellyfin_item_id set, so
    # "In Library" used to lead nowhere — the modal only offered download-again
    # actions for episodes the user already had. Prefer the stored id, fall
    # back to a cached TMDB→item lookup, and backfill the row when we resolve
    # one so the next open is free.
    details["jellyfin_item_id"] = None
    stored_id = getattr(db_item, "jellyfin_item_id", None) if db_item else None
    # A stored id goes stale whenever Jellyfin re-creates the item (library
    # rebuild, folder rename, a .strm folder recreated by a sync), and a stale
    # id sends Watch to an error page permanently. Trust it only while the
    # current map still contains it; otherwise re-resolve.
    live_id = _jellyfin_item_id(tmdb_id, media_type)
    if stored_id and live_id and stored_id != live_id:
        logger.info(
            f"Jellyfin item id for tmdb:{tmdb_id} changed ({stored_id} → {live_id}) — re-resolving"
        )
        stored_id = None
    resolved_id = live_id or stored_id
    if resolved_id:
        details["jellyfin_item_id"] = resolved_id
        details["jellyfin_url"] = _jellyfin_web_url(db, resolved_id)
        if db_item is not None and getattr(db_item, "jellyfin_item_id", None) != resolved_id:
            try:
                db_item.jellyfin_item_id = resolved_id
                db.commit()
            except Exception:
                db.rollback()
        # Jellyfin knows about it even if Tentacle's tables don't (issue #5).
        details["in_library"] = True

    # requested: in Radarr/Sonarr but no file yet (searching / no release found)
    details["media_type"] = details.get("media_type") or media_type
    _mark_requested(details)

    # can_delete: True if downloaded content AND (admin OR user requested it)
    details["can_delete"] = False
    if db_item and hasattr(db_item, "source") and db_item.source in ("radarr", "sonarr"):
        try:
            user = get_user_from_request(request, db)
            if user.is_admin:
                details["can_delete"] = True
            else:
                has_request = db.query(DownloadRequest).filter(
                    DownloadRequest.tmdb_id == tmdb_id,
                    DownloadRequest.media_type == media_type,
                    DownloadRequest.user_id == user.id,
                ).first()
                details["can_delete"] = bool(has_request)
        except HTTPException:
            pass

    return details


@router.get("/detail-tvdb/{tvdb_id}")
def get_discover_detail_tvdb(
    tvdb_id: int,
    db: Session = Depends(get_db),
):
    """Fetch detail for a TheTVDB-only series via Sonarr lookup."""
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        return {"error": "Sonarr not configured"}

    from services.sonarr import SonarrService
    sonarr = SonarrService(sonarr_url, sonarr_key)
    lookup = sonarr.lookup_by_tvdb(tvdb_id)
    if not lookup:
        return {"error": "Not found"}

    # Get images from TMDB via tvdbId cross-reference (TVDB CDN blocks direct access)
    poster = None
    backdrop = None
    for img in lookup.get("images", []):
        if img.get("coverType") == "poster" and img.get("remoteUrl"):
            poster = img["remoteUrl"]
        elif img.get("coverType") == "fanart" and img.get("remoteUrl"):
            backdrop = img["remoteUrl"]

    genres = [g.strip() for g in lookup.get("genres", [])]

    # Rewrite TVDB image URLs to go through proxy
    poster = _rewrite_tvdb_url(poster) if poster else None
    backdrop = _rewrite_tvdb_url(backdrop) if backdrop else None

    return {
        "tmdb_id": lookup.get("tmdbId") or 0,
        "tvdb_id": tvdb_id,
        "title": lookup.get("title", ""),
        "year": str(lookup.get("year", "")),
        "overview": lookup.get("overview", ""),
        "runtime": lookup.get("runtime", 0),
        "rating": round(lookup.get("ratings", {}).get("value", 0), 1),
        "vote_count": lookup.get("ratings", {}).get("votes", 0),
        "genres": genres,
        "poster_path": poster,
        "backdrop_path": backdrop,
        "tagline": "",
        "status": lookup.get("status", ""),
        "cast": [],
        "directors": [],
        "media_type": "series",
        "in_library": False,
        "can_delete": False,
        "source": "sonarr",
    }


def _sonarr_lookup_to_items(results: list) -> list:
    """Convert Sonarr lookup results to standard discover item format."""
    items = []
    for s in results:
        poster = None
        backdrop = None
        for img in s.get("images", []):
            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                poster = img["remoteUrl"]
            elif img.get("coverType") == "fanart" and img.get("remoteUrl"):
                backdrop = img["remoteUrl"]
        items.append({
            "tmdb_id": s.get("tmdbId") or 0,
            "tvdb_id": s.get("tvdbId") or 0,
            "title": s.get("title", ""),
            "year": str(s.get("year", "")),
            "overview": s.get("overview", ""),
            "rating": round(s.get("ratings", {}).get("value", 0), 1),
            "poster_path": poster,
            "backdrop_path": backdrop,
            "media_type": "series",
            "popularity": 0,
            "source": "sonarr",
        })
    return items


def _radarr_lookup_to_items(results: list) -> list:
    """Convert Radarr lookup results to standard discover item format.
    Radarr uses TMDB images natively so no cross-reference needed."""
    items = []
    for m in results:
        poster = None
        backdrop = None
        for img in m.get("images", []):
            cover = img.get("coverType")
            remote = img.get("remoteUrl")
            if cover == "poster" and remote:
                poster = remote
            elif cover == "fanart" and remote:
                backdrop = remote
        items.append({
            "tmdb_id": m.get("tmdbId") or 0,
            "title": m.get("title", ""),
            "year": str(m.get("year", "")),
            "overview": m.get("overview", ""),
            "rating": round(m.get("ratings", {}).get("value", 0), 1),
            "poster_path": poster,
            "backdrop_path": backdrop,
            "media_type": "movie",
            "popularity": 0,
            "source": "radarr",
        })
    return items


@router.get("/search")
def search_discover(
    q: str = "",
    type: str = "all",
    db: Session = Depends(get_db)
):
    """Search TMDB for movies/series, supplemented by Sonarr/Radarr lookup for TheTVDB coverage."""
    if not q or not q.strip():
        return {"items": []}

    tmdb = _get_tmdb(db)

    media_type = "all"
    if type == "movies":
        media_type = "movie"
    elif type == "series":
        media_type = "series"

    # Search TMDB + Sonarr + Radarr in parallel for speed
    items = []
    known_ids = _known_tmdb_ids(db)

    if type != "channels":
        from concurrent.futures import ThreadPoolExecutor

        # Read settings in main thread (SQLAlchemy sessions aren't thread-safe)
        sonarr_url = get_setting(db, "sonarr_url") if media_type in ("all", "series") else None
        sonarr_key = get_setting(db, "sonarr_api_key") if sonarr_url else None
        radarr_url = get_setting(db, "radarr_url") if media_type in ("all", "movie") else None
        radarr_key = get_setting(db, "radarr_api_key") if radarr_url else None

        def _tmdb_search():
            if not tmdb:
                return []
            return tmdb.search_multi_results(q, media_type)

        def _sonarr_search():
            if not sonarr_url or not sonarr_key:
                return []
            from services.sonarr import SonarrService
            try:
                return SonarrService(sonarr_url, sonarr_key).lookup_by_term(q.strip())
            except Exception as e:
                logger.warning(f"Sonarr lookup supplement failed: {e}")
                return []

        def _radarr_search():
            if not radarr_url or not radarr_key:
                return []
            from services.radarr import RadarrService
            try:
                return RadarrService(radarr_url, radarr_key).lookup_by_term(q.strip())
            except Exception as e:
                logger.warning(f"Radarr lookup supplement failed: {e}")
                return []

        with ThreadPoolExecutor(max_workers=3) as pool:
            tmdb_future = pool.submit(_tmdb_search)
            sonarr_future = pool.submit(_sonarr_search)
            radarr_future = pool.submit(_radarr_search)

            try:
                tmdb_results = tmdb_future.result(timeout=15)
            except Exception as e:
                logger.warning(f"TMDB search failed: {e}")
                tmdb_results = []
            try:
                sonarr_results = sonarr_future.result(timeout=15)
            except Exception as e:
                logger.warning(f"Sonarr search failed: {e}")
                sonarr_results = []
            try:
                radarr_results = radarr_future.result(timeout=15)
            except Exception as e:
                logger.warning(f"Radarr search failed: {e}")
                radarr_results = []

        # TMDB results first
        items = _dedup_and_mark(tmdb_results, known_ids)

        # Supplement with Sonarr/Radarr (TheTVDB coverage)
        seen_tmdb = {item["tmdb_id"] for item in items if item.get("tmdb_id")}
        seen_tvdb = set()

        for item in _sonarr_lookup_to_items(sonarr_results):
            tmdb_id = item.get("tmdb_id")
            tvdb_id = item.get("tvdb_id")
            if tmdb_id and tmdb_id in seen_tmdb:
                continue
            if tvdb_id and tvdb_id in seen_tvdb:
                continue
            if tmdb_id:
                seen_tmdb.add(tmdb_id)
                item["in_library"] = _is_in_library(item, known_ids)
            else:
                item["in_library"] = False
            _mark_requested(item)
            if tvdb_id:
                seen_tvdb.add(tvdb_id)
            items.append(item)

        for item in _radarr_lookup_to_items(radarr_results):
            tmdb_id = item.get("tmdb_id")
            if tmdb_id and tmdb_id in seen_tmdb:
                continue
            if tmdb_id:
                seen_tmdb.add(tmdb_id)
                item["in_library"] = _is_in_library(item, known_ids)
            else:
                item["in_library"] = False
            _mark_requested(item)
            items.append(item)

    # Rewrite TVDB image URLs to go through proxy
    items = [_rewrite_item_images(item) for item in items]

    # Search Live TV channels — prepend to items list
    if type in ("all", "channels"):
        channel_rows = db.query(LiveChannel).filter(
            LiveChannel.enabled == True,
            LiveChannel.name.ilike(f"%{q.strip()}%"),
        ).order_by(LiveChannel.sort_order).limit(20).all()
        channel_items = [{
            "media_type": "channel",
            "title": ch.name,
            "channel_id": ch.id,
            "logo_url": ch.logo_url,
            "group_title": ch.group_title,
        } for ch in channel_rows]
        items = channel_items + items

    return {"items": items}


@router.get("/config")
def get_discover_config(db: Session = Depends(get_db)):
    """DEPRECATED: the global discover_in_jellyfin toggle is retired — per-user
    toolbar config (Home Screen tab) now controls which tabs each client shows.
    Always returns enabled for backwards compatibility with older plugin builds
    that still gate the Discover tab / unified search on this flag."""
    return {"discover_in_jellyfin": True}


@router.get("/seasons/{tmdb_id}")
def get_seasons(
    tmdb_id: int,
    db: Session = Depends(get_db)
):
    """Fetch season list for a TV series from TMDB."""
    tmdb = _get_tmdb(db)
    if not tmdb:
        return {"error": "TMDB not configured"}

    details = tmdb.get_series_details(tmdb_id)
    if not details:
        return {"error": "Not found"}

    return {
        "title": details.get("title", ""),
        "seasons": details.get("seasons", []),
    }


@router.get("/season/{tmdb_id}/{season_number}")
def get_season_episodes(
    tmdb_id: int,
    season_number: int,
    db: Session = Depends(get_db)
):
    """Fetch episode list for a specific season from TMDB."""
    tmdb = _get_tmdb(db)
    if not tmdb:
        return {"error": "TMDB not configured"}

    episodes = tmdb.get_season_episodes(tmdb_id, season_number)
    if episodes is None:
        return {"error": "Not found"}

    return {"episodes": episodes}


@router.get("/seasons-tvdb/{tvdb_id}")
def get_seasons_tvdb(
    tvdb_id: int,
    db: Session = Depends(get_db)
):
    """Fetch season list for a TheTVDB-only series via Sonarr lookup."""
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        return {"error": "Sonarr not configured"}

    from services.sonarr import SonarrService
    sonarr = SonarrService(sonarr_url, sonarr_key)

    # Check if series is already in Sonarr (has full episode data)
    all_series = sonarr.get_all_series()
    existing = next((s for s in all_series if s.get("tvdbId") == tvdb_id), None)

    if existing:
        # Series is in Sonarr — fetch real episode data to build accurate season info
        episodes = sonarr.get_episodes(existing["id"])
        season_map = {}
        for ep in episodes:
            sn = ep.get("seasonNumber", 0)
            if sn not in season_map:
                season_map[sn] = {"count": 0, "first_air": None}
            season_map[sn]["count"] += 1
            air = ep.get("airDateUtc")
            if air and (season_map[sn]["first_air"] is None or air < season_map[sn]["first_air"]):
                season_map[sn]["first_air"] = air

        seasons = [
            {
                "season_number": sn,
                "name": f"Season {sn}" if sn > 0 else "Specials",
                "episode_count": info["count"],
                "air_date": info["first_air"][:10] if info["first_air"] else None,
                "poster_path": None,
            }
            for sn, info in sorted(season_map.items())
        ]
    else:
        # Not in Sonarr — use lookup data (season-level only)
        lookup = sonarr.lookup_by_tvdb(tvdb_id)
        if not lookup:
            return {"error": "Not found"}

        seasons = []
        for s in lookup.get("seasons", []):
            sn = s.get("seasonNumber", 0)
            stats = s.get("statistics", {})
            seasons.append({
                "season_number": sn,
                "name": f"Season {sn}" if sn > 0 else "Specials",
                "episode_count": stats.get("totalEpisodeCount", 0),
                "air_date": None,
                "poster_path": None,
            })

    return {
        "title": existing.get("title", "") if existing else "",
        "seasons": seasons,
    }


@router.get("/season-tvdb/{tvdb_id}/{season_number}")
def get_season_episodes_tvdb(
    tvdb_id: int,
    season_number: int,
    db: Session = Depends(get_db)
):
    """Fetch episode list for a specific season via Sonarr (TheTVDB data)."""
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        return {"error": "Sonarr not configured"}

    from services.sonarr import SonarrService
    sonarr = SonarrService(sonarr_url, sonarr_key)

    # Check if series is already in Sonarr
    all_series = sonarr.get_all_series()
    existing = next((s for s in all_series if s.get("tvdbId") == tvdb_id), None)

    if existing:
        # Fetch episodes from Sonarr — has titles, air dates, etc.
        all_eps = sonarr.get_episodes(existing["id"])
        episodes = [
            {
                "episode_number": ep.get("episodeNumber"),
                "name": ep.get("title", ""),
                "overview": "",
                "air_date": ep["airDateUtc"][:10] if ep.get("airDateUtc") else None,
                "runtime": None,
                "still_path": None,
            }
            for ep in all_eps
            if ep.get("seasonNumber") == season_number
        ]
        return {"episodes": episodes}

    # Not in Sonarr — we only have season-level data from lookup
    # Return placeholder episodes based on episode count
    lookup = sonarr.lookup_by_tvdb(tvdb_id)
    if not lookup:
        return {"error": "Not found"}

    for s in lookup.get("seasons", []):
        if s.get("seasonNumber") == season_number:
            count = s.get("statistics", {}).get("totalEpisodeCount", 0)
            episodes = [
                {
                    "episode_number": i + 1,
                    "name": f"Episode {i + 1}",
                    "overview": "",
                    "air_date": None,
                    "runtime": None,
                    "still_path": None,
                }
                for i in range(count)
            ]
            return {"episodes": episodes}

    return {"episodes": []}


@router.get("/sonarr-episodes/{tmdb_id}")
def get_sonarr_episodes(
    tmdb_id: int,
    db: Session = Depends(get_db)
):
    """Fetch current episode monitoring state from Sonarr for an existing series."""
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        return {"in_sonarr": False, "reason": "not_configured"}

    from services.sonarr import SonarrService
    sonarr = SonarrService(sonarr_url, sonarr_key)
    series = sonarr.get_series_by_tmdb(tmdb_id)
    if not series:
        return {"in_sonarr": False}

    episodes = sonarr.get_episodes(series["id"])
    return {
        "in_sonarr": True,
        "sonarr_id": series["id"],
        "episodes": episodes,
    }


@router.get("/vod-episodes/{tmdb_id}")
def get_vod_episodes(
    tmdb_id: int,
    db: Session = Depends(get_db)
):
    """Scan VOD folder for existing .strm episodes of a series."""
    series = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
    if not series or not series.strm_path:
        return {"has_episodes": False}

    if not series.source.startswith("provider_"):
        return {"has_episodes": False}

    show_dir = Path(series.strm_path)
    if not show_dir.exists() or not show_dir.is_dir():
        return {"has_episodes": False}

    episodes = {}
    ep_pattern = re.compile(r'S(\d+)E(\d+)', re.IGNORECASE)

    for item in sorted(show_dir.iterdir()):
        if not item.is_dir() or not item.name.startswith("Season"):
            continue
        for strm_file in sorted(item.iterdir()):
            if strm_file.suffix.lower() != ".strm":
                continue
            match = ep_pattern.search(strm_file.name)
            if match:
                season = int(match.group(1))
                episode = int(match.group(2))
                episodes.setdefault(season, []).append(episode)

    for season in episodes:
        episodes[season].sort()

    return {
        "has_episodes": bool(episodes),
        "episodes": episodes,
    }


class ManageEpisodesBody(BaseModel):
    tmdb_id: int
    selected_episodes: list  # [{season: int, episode: int}]


@router.post("/manage-episodes")
def manage_episodes(
    body: ManageEpisodesBody,
    db: Session = Depends(get_db),
    user=Depends(get_user_from_request),
):
    """Apply episode monitoring changes to an existing Sonarr series."""
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        raise HTTPException(400, "Sonarr not configured")

    from services.sonarr import SonarrService
    sonarr = SonarrService(sonarr_url, sonarr_key)
    series = sonarr.get_series_by_tmdb(body.tmdb_id)
    if not series:
        raise HTTPException(404, "Series not found in Sonarr")

    episodes = sonarr.get_episodes(series["id"])
    ep_lookup = {(ep["seasonNumber"], ep["episodeNumber"]): ep for ep in episodes}

    # Map selected episodes to Sonarr episode IDs
    selected_ids = []
    for sel in body.selected_episodes:
        ep = ep_lookup.get((sel["season"], sel["episode"]))
        if ep:
            selected_ids.append(ep["id"])

    # Track which are newly monitored (for search)
    currently_monitored = {ep["id"] for ep in episodes if ep.get("monitored")}

    # Unmonitor all, then monitor selected
    all_ids = [ep["id"] for ep in episodes]
    if all_ids:
        sonarr.set_episode_monitoring(all_ids, False)
    if selected_ids:
        sonarr.set_episode_monitoring(selected_ids, True)

    # Search for newly monitored episodes that don't have files
    need_search = []
    for sel in body.selected_episodes:
        ep = ep_lookup.get((sel["season"], sel["episode"]))
        if ep and ep["id"] not in currently_monitored and not ep.get("hasFile"):
            need_search.append(ep["id"])
    if need_search:
        sonarr.search_episodes(need_search)

    logger.info(f"Managed episodes for tmdb:{body.tmdb_id} — monitoring {len(selected_ids)}, searching {len(need_search)}")
    return {"success": True, "monitored": len(selected_ids), "searching": len(need_search)}


# ---------------------------------------------------------------------------
# Image proxy — TVDB CDN blocks non-browser HTTP clients (TLS fingerprinting),
# so we proxy TVDB images through Tentacle using httpx with HTTP/2
# ---------------------------------------------------------------------------

TVDB_PROXY_CACHE = Path("/data/tvdb_image_cache")


def _rewrite_tvdb_url(url: str) -> str:
    """Rewrite a TVDB CDN URL to a relative proxy path (no host).
    The C# plugin prepends its own Tentacle base URL when proxying."""
    if not url or "thetvdb.com" not in url:
        return url
    from urllib.parse import quote
    cache_key = hashlib.md5(url.encode()).hexdigest()
    return f"/api/discover/image-proxy/{cache_key}?url={quote(url, safe='')}"


def _rewrite_item_images(item: dict) -> dict:
    """Rewrite TVDB image URLs in a discover item to use the proxy."""
    if item.get("poster_path"):
        item["poster_path"] = _rewrite_tvdb_url(item["poster_path"])
    if item.get("backdrop_path"):
        item["backdrop_path"] = _rewrite_tvdb_url(item["backdrop_path"])
    return item


def _normalize_proxy_url(url: str) -> str:
    """Undo extra layers of percent-encoding on a forwarded image URL.

    These URLs are minted percent-encoded; clients differ in how many times
    they re-encode the query value before handing it back, and a still-encoded
    value has no scheme or host, so it fails the allowlist check and every
    image is rejected. Decode until the value stops changing or parses.
    """
    from urllib.parse import unquote, urlparse
    for _ in range(3):
        if urlparse(url).scheme in ("http", "https"):
            return url
        decoded = unquote(url)
        if decoded == url:
            return url
        url = decoded
    return url


@router.get("/image-proxy/{cache_key}")
async def image_proxy(cache_key: str, url: str = ""):
    """Proxy TVDB images through the server to bypass CDN TLS fingerprinting."""
    url = _normalize_proxy_url(url)
    # Strict host allowlist + public-IP check (substring matching like
    # "thetvdb.com" in url is trivially bypassed, e.g. ?url=http://169.254.169.254/?x=thetvdb.com).
    if not is_safe_url(url, allowed_hosts={"thetvdb.com"}):
        raise HTTPException(status_code=400, detail="Invalid URL")

    # Check disk cache
    TVDB_PROXY_CACHE.mkdir(parents=True, exist_ok=True)
    # Extract extension from URL path (strip query params first)
    url_path = url.split("?")[0]
    ext = Path(url_path).suffix if Path(url_path).suffix in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"
    cached = TVDB_PROXY_CACHE / f"{cache_key}{ext}"
    if cached.exists():
        media_type = "image/jpeg"
        if ext == ".png":
            media_type = "image/png"
        elif ext == ".webp":
            media_type = "image/webp"
        return Response(content=cached.read_bytes(), media_type=media_type)

    # Fetch from TVDB using httpx with HTTP/2 (better TLS fingerprint).
    # follow_redirects=False: a redirect could send us to an internal host that
    # the up-front allowlist check would not have covered (TVDB artwork is direct).
    try:
        async with httpx.AsyncClient(http2=True, follow_redirects=False, timeout=15) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://thetvdb.com/",
            })
            if resp.status_code != 200:
                logger.warning(f"TVDB image proxy failed: {resp.status_code} for {url}")
                raise HTTPException(status_code=502, detail="Upstream image fetch failed")

            content = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg")

            # Cache to disk
            cached.write_bytes(content)
            return Response(content=content, media_type=content_type)
    except httpx.HTTPError as e:
        logger.warning(f"TVDB image proxy error: {e}")
        raise HTTPException(status_code=502, detail="Upstream image fetch failed")