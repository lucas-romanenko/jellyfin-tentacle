"""
Tentacle - Library Router
Unified view of movies and series
"""

import threading
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional
from models.database import get_db, get_setting, Movie, Series, ListItem, DownloadRequest, TentacleUser, Duplicate, log_deletion
from routers.auth import get_user_from_request, require_admin
from services.media_files import delete_movie_files, delete_series_files
from services.cleaner import clean_list_title
from services.logstream import library_event_generator, emit_library_event
from services.tmdb import TMDBService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["library"])

# Jellyfin item ids are GUIDs, with or without dashes. Anything else must be
# refused before it reaches a request URL: `requests` resolves dot-segments
# client-side, so an id of "../Users/<guid>" turns
# DELETE {jellyfin_url}/Items/{id} into DELETE {jellyfin_url}/Users/<guid> —
# sent with the stored Jellyfin *admin* API key.
_JELLYFIN_ID_RE = re.compile(r"\A[0-9a-fA-F]{32}\Z|\A[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")


def _validate_jellyfin_item_id(item_id: str) -> str:
    if not _JELLYFIN_ID_RE.match(item_id or ""):
        raise HTTPException(400, "Invalid Jellyfin item id")
    return item_id


@router.get("/stream")
async def stream_library_events(user: TentacleUser = Depends(get_user_from_request)):
    """SSE endpoint for real-time library change events. Requires a session —
    the event stream narrates the whole library as it changes."""
    return StreamingResponse(
        library_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@router.get("/items")
def get_library_items(
    media_type: Optional[str] = None,
    source: Optional[str] = None,
    source_tag: Optional[str] = None,
    search: Optional[str] = None,
    list_id: Optional[int] = None,
    sort: Optional[str] = None,
    list_status: Optional[str] = None,
    limit: int = 48,
    offset: int = 0,
    db: Session = Depends(get_db),
    user: TentacleUser = Depends(get_user_from_request),
):
    # List mode: return all items from the list with in_library status
    if list_id is not None:
        return _get_list_items(list_id, search, sort, list_status, limit, offset, db)

    movies_q = db.query(Movie)
    series_q = db.query(Series)

    # Source filter
    if source == "vod":
        movies_q = movies_q.filter(Movie.source.like("provider_%"))
        series_q = series_q.filter(Series.source.like("provider_%"))
    elif source == "radarr":
        movies_q = movies_q.filter(Movie.source == "radarr")
        series_q = series_q.filter(Series.source.in_(["radarr", "sonarr"]))

    # Source tag filter
    if source_tag:
        movies_q = movies_q.filter(Movie.source_tag == source_tag)
        series_q = series_q.filter(Series.source_tag == source_tag)

    # Search
    if search:
        movies_q = movies_q.filter(Movie.title.ilike(f"%{search}%"))
        series_q = series_q.filter(Series.title.ilike(f"%{search}%"))

    # Build combined result
    items = []

    if not media_type or media_type == "movie":
        movies = movies_q.order_by(Movie.date_added.desc()).all()
        for m in movies:
            items.append({
                "tmdb_id": m.tmdb_id,
                "title": m.title,
                "year": m.year,
                "poster_path": m.poster_path,
                "rating": m.rating,
                "genres": m.genres,
                "source": m.source,
                "source_tag": m.source_tag,
                "tags": m.tags,
                "media_type": "movie",
                "date_added": m.date_added,
            })

    if not media_type or media_type == "series":
        series = series_q.order_by(Series.date_added.desc()).all()
        for s in series:
            items.append({
                "tmdb_id": s.tmdb_id,
                "title": s.title,
                "year": s.year,
                "poster_path": s.poster_path,
                "genres": s.genres,
                "source": s.source,
                "source_tag": s.source_tag,
                "tags": s.tags,
                "media_type": "series",
                "date_added": s.date_added,
                "following": s.sonarr_monitored or False,
            })

    # Sort
    if sort == "title_asc":
        items.sort(key=lambda x: (x.get("title") or "").lower())
    elif sort == "title_desc":
        items.sort(key=lambda x: (x.get("title") or "").lower(), reverse=True)
    elif sort == "rating":
        items.sort(key=lambda x: x.get("rating") or 0, reverse=True)
    elif sort == "year_desc":
        items.sort(key=lambda x: x.get("year") or "", reverse=True)
    elif sort == "year_asc":
        items.sort(key=lambda x: x.get("year") or "")
    else:
        items.sort(key=lambda x: x.get("date_added") or "", reverse=True)

    # Source tag breakdown (respects media_type + source filters, ignores source_tag + search)
    breakdown = {}
    if not media_type or media_type == "movie":
        m_q = db.query(Movie.source_tag)
        if source == "vod":
            m_q = m_q.filter(Movie.source.like("provider_%"))
        elif source == "radarr":
            m_q = m_q.filter(Movie.source == "radarr")
        for (tag,) in m_q.all():
            if tag:
                breakdown[tag] = breakdown.get(tag, 0) + 1
    if not media_type or media_type == "series":
        s_q = db.query(Series.source_tag)
        if source == "vod":
            s_q = s_q.filter(Series.source.like("provider_%"))
        elif source == "radarr":
            s_q = s_q.filter(Series.source.in_(["radarr", "sonarr"]))
        for (tag,) in s_q.all():
            if tag:
                breakdown[tag] = breakdown.get(tag, 0) + 1

    total = len(items)
    paginated = items[offset:offset + limit]

    return {"total": total, "items": paginated, "source_breakdown": breakdown}


def _get_list_items(list_id: int, search: Optional[str], sort: Optional[str],
                    list_status: Optional[str], limit: int, offset: int, db):
    """Return all items from a list with in_library and source fields."""
    list_items = db.query(ListItem).filter(ListItem.list_id == list_id).all()
    if not list_items:
        return {"total": 0, "items": [], "source_breakdown": {}}

    # Batch-load library movies and series by tmdb_id
    tmdb_ids = [li.tmdb_id for li in list_items if li.tmdb_id]
    movie_map = {}
    series_map = {}
    if tmdb_ids:
        movies = db.query(Movie).filter(Movie.tmdb_id.in_(tmdb_ids)).all()
        movie_map = {m.tmdb_id: m for m in movies}
        series_list = db.query(Series).filter(Series.tmdb_id.in_(tmdb_ids)).all()
        series_map = {s.tmdb_id: s for s in series_list}

    items = []
    for li in list_items:
        movie = movie_map.get(li.tmdb_id) if li.tmdb_id else None
        serie = series_map.get(li.tmdb_id) if li.tmdb_id else None

        if movie:
            items.append({
                "tmdb_id": movie.tmdb_id,
                "title": movie.title,
                "year": movie.year,
                "poster_path": movie.poster_path,
                "rating": movie.rating,
                "genres": movie.genres,
                "source": movie.source,
                "source_tag": movie.source_tag,
                "tags": movie.tags,
                "media_type": "movie",
                "date_added": movie.date_added,
                "in_library": True,
            })
        elif serie:
            items.append({
                "tmdb_id": serie.tmdb_id,
                "title": serie.title,
                "year": serie.year,
                "poster_path": serie.poster_path,
                "rating": serie.rating,
                "genres": serie.genres,
                "source": serie.source,
                "source_tag": serie.source_tag,
                "tags": serie.tags,
                "media_type": "series",
                "date_added": serie.date_added,
                "in_library": True,
            })
        else:
            # Clean pre-fix rows (HTML entities + baked-in year) at serving time
            clean_name, clean_year = clean_list_title(li.title, li.year)
            items.append({
                "tmdb_id": li.tmdb_id,
                "imdb_id": li.imdb_id,
                "title": clean_name or (f"TMDB {li.tmdb_id}" if li.tmdb_id else f"IMDb {li.imdb_id}"),
                "year": clean_year,
                "poster_path": li.poster_path,
                "source": None,
                "source_tag": None,
                "tags": [],
                "media_type": li.media_type or "movie",
                "in_library": False,
            })

    # Search filter
    if search:
        search_lower = search.lower()
        items = [i for i in items if search_lower in (i.get("title") or "").lower()]

    # List status filter
    if list_status == "in_library":
        items = [i for i in items if i.get("in_library")]
    elif list_status == "missing":
        items = [i for i in items if not i.get("in_library")]

    # Sort
    if sort == "title_asc":
        items.sort(key=lambda x: (x.get("title") or "").lower())
    elif sort == "title_desc":
        items.sort(key=lambda x: (x.get("title") or "").lower(), reverse=True)
    elif sort == "rating":
        items.sort(key=lambda x: x.get("rating") or 0, reverse=True)
    elif sort == "year_desc":
        items.sort(key=lambda x: x.get("year") or "", reverse=True)
    elif sort == "year_asc":
        items.sort(key=lambda x: x.get("year") or "")
    else:
        # Default for lists: in-library first, then by title
        items.sort(key=lambda x: (not x.get("in_library"), (x.get("title") or "").lower()))

    total = len(items)
    paginated = items[offset:offset + limit]

    return {"total": total, "items": paginated, "source_breakdown": {}}


@router.get("/item/{media_type}/{tmdb_id}")
def get_item_detail(
    media_type: str,
    tmdb_id: int,
    db: Session = Depends(get_db),
    user: TentacleUser = Depends(get_user_from_request),
):
    """Library item detail. Requires a session (#74): the response carries the
    on-disk .strm path and the playlist tags for the title. The dashboard
    sends its cookie and the plugin forwards the caller's token."""
    if media_type == "movie":
        item = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
        if not item:
            raise HTTPException(404, "Movie not found")
        result = {
            "tmdb_id": item.tmdb_id,
            "title": item.title,
            "year": item.year,
            "overview": item.overview,
            "runtime": item.runtime,
            "rating": item.rating,
            "genres": item.genres,
            "poster_path": item.poster_path,
            "backdrop_path": item.backdrop_path,
            "source": item.source,
            "source_tag": item.source_tag,
            "tags": item.tags,
            "strm_path": item.strm_path,
            "date_added": item.date_added,
            "media_type": "movie",
        }
    elif media_type == "series":
        item = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
        if not item:
            raise HTTPException(404, "Series not found")
        result = {
            "tmdb_id": item.tmdb_id,
            "title": item.title,
            "year": item.year,
            "overview": item.overview,
            "genres": item.genres,
            "poster_path": item.poster_path,
            "backdrop_path": item.backdrop_path,
            "source": item.source,
            "source_tag": item.source_tag,
            "tags": item.tags,
            "strm_path": item.strm_path,
            "date_added": item.date_added,
            "media_type": "series",
            "following": item.sonarr_monitored or False,
            "status": item.status,
        }
    else:
        raise HTTPException(400, "Invalid media type")

    # strm_managed: False when the user has opted this title out of .strm
    # writing/repair (it stays in the catalog; the sync just leaves its files
    # alone). Only meaningful for provider-sourced titles.
    result["strm_managed"] = not bool(item.strm_disabled)
    result["is_vod"] = bool(item.source and item.source.startswith("provider_"))

    # can_delete: True if downloaded content AND (admin OR user requested it)
    result["can_delete"] = False
    if item.source in ("radarr", "sonarr"):
        if user.is_admin:
            result["can_delete"] = True
        else:
            has_request = db.query(DownloadRequest).filter(
                DownloadRequest.tmdb_id == tmdb_id,
                DownloadRequest.media_type == media_type,
                DownloadRequest.user_id == user.id,
            ).first()
            result["can_delete"] = bool(has_request)

    return result


def _get_followers_for_series(db, tmdb_id: int) -> list:
    """Return user IDs that have a DownloadRequest for this series (i.e. followers)."""
    return [
        dr.user_id for dr in
        db.query(DownloadRequest.user_id).filter(
            DownloadRequest.tmdb_id == tmdb_id,
            DownloadRequest.media_type == "series",
        ).all()
    ]


def _cleanup_playlists_all_users(tmdb_id: int, media_type: str, jellyfin_item_id: str = None):
    """Background: remove an item from all users' playlists."""
    from models.database import SessionLocal
    from services.jellyfin import JellyfinService
    from services.smartlists import remove_item_from_playlists
    cleanup_db = SessionLocal()
    try:
        jf_url = get_setting(cleanup_db, "jellyfin_url", "")
        jf_key = get_setting(cleanup_db, "jellyfin_api_key", "")
        if not jf_url or not jf_key:
            return

        # Find the Jellyfin item ID if not provided
        jf_item_id = jellyfin_item_id
        if not jf_item_id:
            users = cleanup_db.query(TentacleUser).all()
            if not users:
                return
            jf = JellyfinService(jf_url, jf_key, users[0].jellyfin_user_id)
            jf_type = "Movie" if media_type == "movie" else "Series"
            jf_item = jf.search_by_tmdb_id(tmdb_id, media_type=jf_type)
            jf_item_id = jf_item["Id"] if jf_item else None

        if not jf_item_id:
            logger.debug(f"No Jellyfin item found for tmdb:{tmdb_id}, skipping playlist cleanup")
            return

        users = cleanup_db.query(TentacleUser).all()
        total_removed = 0
        for user in users:
            try:
                result = remove_item_from_playlists(cleanup_db, jf_item_id, user.id)
                total_removed += result.get("removed_from", 0)
            except Exception as e:
                logger.warning(f"Playlist cleanup for user {user.id} failed: {e}")
        if total_removed:
            from services.smartlists import bump_playlist_version, _notify_jellyfin_plugin
            bump_playlist_version()
            _notify_jellyfin_plugin(cleanup_db)
            logger.info(f"Playlist cleanup for tmdb:{tmdb_id}: removed from {total_removed} playlists across {len(users)} users")
    except Exception as e:
        logger.warning(f"Playlist cleanup failed for tmdb:{tmdb_id}: {e}")
    finally:
        cleanup_db.close()


@router.delete("/item/{media_type}/{tmdb_id}")
def delete_library_item(
    media_type: str,
    tmdb_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Lightweight: remove from Tentacle DB + playlists only (Jellyfin item already gone).

    Called by the C# plugin's ItemRemoved handler (a server-side hosted service with no
    user context) when items are deleted through Jellyfin's native UI. Left unauthenticated
    to keep the plugin zero-config; it's self-healing if abused — the Jellyfin item still
    exists, so the next Radarr/Sonarr/VOD scan re-creates the DB record and the nightly
    orphan sweep reconciles anything stale. Keep the backend on the internal network.
    """
    if media_type not in ("movie", "series"):
        raise HTTPException(400, "Invalid media type")

    model = Movie if media_type == "movie" else Series
    item = db.query(model).filter(model.tmdb_id == tmdb_id).first()
    if not item:
        # Nothing of this title is in the catalogue, so there is no deletion to
        # mirror. Stop here: the request history and the duplicate tombstones
        # are not ours to drop on a title we never had, and a library scan that
        # drops thousands of items would otherwise open thousands of playlist
        # sweeps for rows that don't exist.
        logger.info(
            f"[Library] Jellyfin reported {media_type} tmdb:{tmdb_id} deleted, "
            f"but it is not in the catalogue — nothing to clean up"
        )
        return {"success": True, "deleted": False}

    title = item.title if hasattr(item, "title") else str(tmdb_id)
    db.delete(item)

    # Also clean up DownloadRequest + duplicate tombstones (a deliberate full
    # delete is a clean slate — the title may re-import from VOD later).
    # Not while a bad copy is being replaced: the request still stands.
    from services.bad_copy import is_replacing
    if not is_replacing(db, media_type, tmdb_id):
        db.query(DownloadRequest).filter(
            DownloadRequest.tmdb_id == tmdb_id,
            DownloadRequest.media_type == media_type,
        ).delete()
    db.query(Duplicate).filter(
        Duplicate.tmdb_id == tmdb_id,
        Duplicate.media_type == media_type,
    ).delete()
    db.commit()

    log_deletion(db, kind="jellyfin-delete", name=title, media_type=media_type, reason="webhook",
                 detail="Deleted via Jellyfin native UI — Tentacle DB record and playlists cleaned up")
    emit_library_event(f"{media_type}_removed", {"tmdb_id": tmdb_id, "media_type": media_type})

    # Remove from all users' playlists in background
    threading.Thread(
        target=_cleanup_playlists_all_users,
        args=(tmdb_id, media_type),
        daemon=True,
    ).start()

    return {"success": True, "deleted": True}


@router.delete("/delete-download/{tmdb_id}")
def delete_download(
    tmdb_id: int,
    media_type: str,
    request: Request,
    jellyfin_item_id: str = None,
    db: Session = Depends(get_db),
):
    """Full delete: permission check, delete from Radarr/Sonarr + Jellyfin + Tentacle DB + playlists.

    Non-admin users can only delete content they requested (via DownloadRequest table).
    Admin users can delete any downloaded content.
    Pass jellyfin_item_id to skip the expensive library search.
    """
    if media_type not in ("movie", "series"):
        raise HTTPException(400, "Invalid media type")

    user = get_user_from_request(request, db)

    # Permission check
    if not user.is_admin:
        has_request = db.query(DownloadRequest).filter(
            DownloadRequest.tmdb_id == tmdb_id,
            DownloadRequest.media_type == media_type,
            DownloadRequest.user_id == user.id,
        ).first()
        if not has_request:
            raise HTTPException(403, "You can only delete content you requested")

    # Check item exists in Tentacle DB
    model = Movie if media_type == "movie" else Series
    item = db.query(model).filter(model.tmdb_id == tmdb_id).first()
    if not item:
        raise HTTPException(404, "Item not found in library")

    # Check it's downloaded content (not VOD)
    if hasattr(item, "source") and item.source not in ("radarr", "sonarr"):
        raise HTTPException(400, "Only downloaded content can be deleted from here")

    title = item.title if hasattr(item, "title") else str(tmdb_id)

    # Use provided Jellyfin item ID or fall back to search
    from services.jellyfin import JellyfinService
    jf_url = get_setting(db, "jellyfin_url", "")
    jf_key = get_setting(db, "jellyfin_api_key", "")
    jf_item_id = _validate_jellyfin_item_id(jellyfin_item_id) if jellyfin_item_id else None
    jf = None
    if jf_url and jf_key:
        jf = JellyfinService(jf_url, jf_key, user.jellyfin_user_id)
        jf_type = "Movie" if media_type == "movie" else "Series"
        if jf_item_id:
            # The permission check above is about tmdb_id; the id actually
            # deleted is this caller-supplied one. Only use it if it IS that
            # title — otherwise a user who requested one film could delete any
            # item in Jellyfin (another user's film, a collection, a library
            # folder) by passing its id. Fetched through the user-scoped path,
            # so it must also be an item this user can see.
            try:
                supplied = jf.get_item_by_id(jf_item_id) or {}
            except Exception as e:
                # Already gone from Jellyfin (a retried fire-and-forget delete)
                # or not visible to this user: either way it is not the title
                # we were asked about. Fall back to the lookup below.
                logger.info(f"Delete-download tmdb:{tmdb_id}: supplied Jellyfin id {jf_item_id} "
                            f"could not be read ({e}) — ignoring it")
                supplied = {}
            if (supplied.get("Type") != jf_type
                    or (supplied.get("ProviderIds") or {}).get("Tmdb") != str(tmdb_id)):
                logger.warning(
                    f"Delete-download tmdb:{tmdb_id}: supplied Jellyfin id {jf_item_id} is not "
                    f"that {jf_type.lower()} — ignoring it (user={user.display_name})")
                jf_item_id = None
        if not jf_item_id:
            jf_item = jf.search_by_tmdb_id(tmdb_id, media_type=jf_type)
            jf_item_id = jf_item["Id"] if jf_item else None

    # Delete from Radarr/Sonarr (removes files from disk). Track whether the
    # backend that owns the files was attempted and whether it actually succeeded
    # so we don't claim success — and don't drop the DB record — when the disk
    # files may still be present (which would orphan them outside Tentacle).
    radarr_deleted = False
    sonarr_deleted = False
    arr_attempted = False
    arr_ok = True
    try:
        if media_type == "movie":
            radarr_url = get_setting(db, "radarr_url", "")
            radarr_key = get_setting(db, "radarr_api_key", "")
            if radarr_url and radarr_key:
                arr_attempted = True
                from services.radarr import RadarrService
                radarr = RadarrService(radarr_url, radarr_key)
                radarr_deleted = radarr.delete_movie(tmdb_id, delete_files=True)
                arr_ok = bool(radarr_deleted)
        else:
            sonarr_url = get_setting(db, "sonarr_url", "")
            sonarr_key = get_setting(db, "sonarr_api_key", "")
            if sonarr_url and sonarr_key:
                arr_attempted = True
                from services.sonarr import SonarrService
                sonarr = SonarrService(sonarr_url, sonarr_key)
                sonarr_deleted = sonarr.delete_series(tmdb_id, delete_files=True)
                arr_ok = bool(sonarr_deleted)
    except Exception as e:
        arr_ok = False
        logger.error(f"Delete-download tmdb:{tmdb_id} ({media_type}): Radarr/Sonarr delete error: {e}")

    # If the *arr backend was configured and attempted but failed, the files are
    # still on disk. Abort: keep the Tentacle DB record (and playlists) so the
    # user can retry / the nightly orphan sweep can reconcile, and surface 502.
    if arr_attempted and not arr_ok:
        logger.warning(
            f"Delete-download tmdb:{tmdb_id} ({media_type}): backend delete failed — "
            f"keeping DB record for retry (user={user.display_name})"
        )
        raise HTTPException(502, f"Failed to delete '{title}' from {'Radarr' if media_type == 'movie' else 'Sonarr'} — files may still exist")

    # Delete from Jellyfin
    jf_deleted = False
    if jf_item_id and jf_url and jf_key:
        jf_deleted = jf.delete_item(jf_item_id)

    # Delete from Tentacle DB (+ duplicate tombstones — deliberate delete is a
    # clean slate, the title may legitimately re-import from VOD later)
    db.delete(item)
    db.query(DownloadRequest).filter(
        DownloadRequest.tmdb_id == tmdb_id,
        DownloadRequest.media_type == media_type,
    ).delete()
    db.query(Duplicate).filter(
        Duplicate.tmdb_id == tmdb_id,
        Duplicate.media_type == media_type,
    ).delete()
    db.commit()

    arr_name = "Radarr" if media_type == "movie" else "Sonarr"
    log_deletion(db, kind="download-delete", name=title, media_type=media_type, reason="manual",
                 user_name=user.display_name,
                 detail=f"{arr_name} delete (files removed): {'yes' if (radarr_deleted or sonarr_deleted) else 'not configured'}; "
                        f"Jellyfin delete: {'yes' if jf_deleted else 'item not found'}")

    emit_library_event(f"{media_type}_removed", {
        "tmdb_id": tmdb_id, "title": title, "media_type": media_type,
    })

    # Remove from all users' playlists in background
    if jf_item_id:
        threading.Thread(
            target=_cleanup_playlists_all_users,
            args=(tmdb_id, media_type, jf_item_id),
            daemon=True,
        ).start()

    logger.info(
        f"Delete-download tmdb:{tmdb_id} ({media_type}): "
        f"radarr={radarr_deleted}, sonarr={sonarr_deleted}, "
        f"jellyfin={jf_deleted}, user={user.display_name}"
    )

    # jf_deleted may be False if the item wasn't found in Jellyfin (already gone) —
    # that's not a failure for the caller, but report it so clients can react.
    return {
        "success": True,
        "deleted": True,
        "title": title,
        "radarr_deleted": radarr_deleted,
        "sonarr_deleted": sonarr_deleted,
        "jellyfin_deleted": jf_deleted,
    }


@router.get("/tmdb/{media_type}/{tmdb_id}")
def get_tmdb_detail(
    media_type: str,
    tmdb_id: int,
    db: Session = Depends(get_db),
    user: TentacleUser = Depends(get_user_from_request),
):
    """Fetch item details from TMDB (for items not in library).

    Requires a session: this spends the server's TMDB token on behalf of the
    caller, so leaving it open turns Tentacle into a free TMDB proxy."""
    from services.tmdb import get_tmdb_token
    bearer = get_tmdb_token(db)
    data_dir = get_setting(db, "data_dir", "/data")
    tmdb = TMDBService(bearer, data_dir)
    if media_type == "series":
        details = tmdb.get_series_details(tmdb_id)
    else:
        details = tmdb.get_movie_details(tmdb_id)
    if not details:
        raise HTTPException(404, "Not found on TMDB")
    return details


@router.get("/following")
def get_following_series(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return series being followed for new episodes by the current user."""
    # Get TMDB IDs this user has requested (their downloads)
    user_tmdb_ids = {
        dr.tmdb_id for dr in
        db.query(DownloadRequest.tmdb_id).filter(
            DownloadRequest.user_id == user.id,
            DownloadRequest.media_type == "series",
        ).all()
    }

    series = db.query(Series).filter(
        Series.sonarr_monitored == True,
        or_(Series.status == None, ~Series.status.in_(["Ended", "Canceled"])),
    ).order_by(Series.title).all()

    # Admin sees all followed series; non-admin only sees series they requested
    if not user.is_admin:
        series = [s for s in series if s.tmdb_id in user_tmdb_ids]

    return [
        {
            "tmdb_id": s.tmdb_id,
            "title": s.title,
            "year": s.year,
            "poster_path": s.poster_path,
            "genres": s.genres,
            "source": s.source,
            "source_tag": s.source_tag,
            "tags": s.tags,
            "media_type": "series",
            "date_added": s.date_added,
            "following": True,
            "status": s.status,
        }
        for s in series
    ]


class StrmManagedBody(BaseModel):
    enabled: bool               # False = stop writing/repairing .strm for this title
    delete_files: bool = False  # also remove the .strm/.nfo Tentacle wrote


@router.post("/strm-managed/{media_type}/{tmdb_id}")
def set_strm_managed(media_type: str, tmdb_id: int, body: StrmManagedBody,
                     db: Session = Depends(get_db),
                     user: TentacleUser = Depends(require_admin)):
    """Turn .strm management on or off for one title.

    Switching it off keeps the title in the catalog (Discover, tags, playlists
    all behave as before) but stops the sync from writing or repairing its
    .strm files — for shows the user has deliberately moved to downloaded
    copies, typically because the provider's stream is broken. Without this the
    nightly sync regenerates every .strm, and in a merged folder the download
    and the stream fight each other.
    """
    if media_type not in ("movie", "series"):
        raise HTTPException(400, "media_type must be 'movie' or 'series'")
    Model = Movie if media_type == "movie" else Series
    item = db.query(Model).filter(Model.tmdb_id == tmdb_id).first()
    if not item:
        raise HTTPException(404, f"No {media_type} with tmdb_id {tmdb_id}")

    item.strm_disabled = not body.enabled
    # The VOD sweep skips opted-out titles, so a missing-file mark set before
    # the opt-out is never cleared by it. Left in place, that stale mark would
    # count as the first strike on the first sweep after re-enabling.
    item.file_missing_since = None
    deleted = 0
    if not body.enabled and body.delete_files and item.strm_path:
        # Removes only the .strm/.nfo Tentacle wrote — downloaded episodes in
        # the same folder are left alone.
        deleted = (delete_movie_files(item.strm_path) if media_type == "movie"
                   else delete_series_files(item.strm_path))
        log_deletion(db, kind="strm-optout", name=item.title, media_type=media_type,
                     reason="manual",
                     user_name=getattr(user, "display_name", None),  # None in bootstrap mode
                     detail=f"{deleted} .strm/.nfo file(s) removed — .strm management disabled")
    db.commit()

    logger.info(
        f"[Library] .strm management {'enabled' if body.enabled else 'disabled'} for "
        f"{media_type} '{item.title}' (tmdb:{tmdb_id})"
        + (f", {deleted} file(s) removed" if deleted else "")
    )
    return {"success": True, "strm_managed": body.enabled, "files_deleted": deleted}


class FollowBody(BaseModel):
    follow: bool


@router.post("/follow/{tmdb_id}")
def toggle_follow(tmdb_id: int, body: FollowBody, db: Session = Depends(get_db),
                  user: TentacleUser = Depends(get_user_from_request)):
    """Enable or disable following for new episodes on a series. Requires auth
    (dashboard cookie or the plugin-forwarded user token)."""
    from services.sonarr import SonarrService

    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        raise HTTPException(400, "Sonarr not configured")

    sonarr = SonarrService(sonarr_url, sonarr_key)
    success = sonarr.set_follow(tmdb_id, body.follow)
    if not success:
        raise HTTPException(400, "Series not found in Sonarr")

    # Update local DB
    series = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
    if series:
        series.sonarr_monitored = body.follow
        db.commit()

    return {"success": True, "following": body.follow}


# ── Wrong movie: mislabelled provider streams ─────────────────────────────────
# See services/wrong_match.py. Admin only: removing a VOD title and blocking its
# stream affects every user.

@router.post("/wrong-match/movie/{tmdb_id}")
def report_wrong_match(tmdb_id: int, db: Session = Depends(get_db),
                       user: Optional[TentacleUser] = Depends(require_admin)):
    """This VOD movie plays a different film: block its stream, remove the copy."""
    from services.wrong_match import WrongMatchError, block_and_remove_movie
    try:
        result = block_and_remove_movie(db, tmdb_id, user_name=user.display_name if user else None)
    except WrongMatchError as e:
        raise HTTPException(e.status, str(e))
    emit_library_event("movie_removed", {"tmdb_id": tmdb_id, "media_type": "movie"})
    return result


@router.get("/match-suspects", dependencies=[Depends(require_admin)])
def list_match_suspects(db: Session = Depends(get_db)):
    """VOD movies whose played length is far from their TMDB runtime."""
    from models.database import MatchSuspect
    rows = db.query(MatchSuspect).filter(MatchSuspect.dismissed == False).order_by(  # noqa: E712
        MatchSuspect.detected_at.desc()).all()
    posters = {m.tmdb_id: m.poster_path for m in db.query(Movie.tmdb_id, Movie.poster_path).filter(
        Movie.tmdb_id.in_([r.tmdb_id for r in rows])).all()} if rows else {}
    return {"suspects": [{
        "tmdb_id": r.tmdb_id, "media_type": r.media_type, "title": r.title,
        "expected_minutes": r.expected_minutes, "actual_minutes": r.actual_minutes,
        "jellyfin_item_id": r.jellyfin_item_id, "poster_path": posters.get(r.tmdb_id),
        "detected_at": r.detected_at.isoformat() + "Z" if r.detected_at else None,
    } for r in rows]}


@router.post("/match-suspects/{tmdb_id}/dismiss", dependencies=[Depends(require_admin)])
def dismiss_match_suspect(tmdb_id: int, db: Session = Depends(get_db)):
    """It really is the right film (a different cut, say) — stop flagging it."""
    from models.database import MatchSuspect
    row = db.query(MatchSuspect).filter(MatchSuspect.tmdb_id == tmdb_id,
                                        MatchSuspect.media_type == "movie").first()
    if not row:
        raise HTTPException(404, "Not flagged")
    row.dismissed = True
    db.commit()
    return {"ok": True}


@router.post("/match-suspects/check", dependencies=[Depends(require_admin)])
def check_match_suspects_now(db: Session = Depends(get_db)):
    from services.wrong_match import check_runtime_mismatches
    return check_runtime_mismatches(db)


@router.get("/blocked-streams", dependencies=[Depends(require_admin)])
def list_blocked_streams(db: Session = Depends(get_db)):
    from models.database import BlockedStream, Provider
    names = {p.id: p.name for p in db.query(Provider.id, Provider.name).all()}
    return {"blocked": [{
        "id": b.id, "provider": names.get(b.provider_id, f"provider {b.provider_id}"),
        "media_type": b.media_type, "stream": b.stream_key if b.stream_key.isdigit() else "(stream URL)",
        "tmdb_id": b.tmdb_id, "title": b.title, "reason": b.reason, "blocked_by": b.blocked_by,
        "created_at": b.created_at.isoformat() + "Z" if b.created_at else None,
    } for b in db.query(BlockedStream).order_by(BlockedStream.created_at.desc()).all()]}


@router.delete("/blocked-streams/{block_id}", dependencies=[Depends(require_admin)])
def unblock_stream(block_id: int, db: Session = Depends(get_db)):
    """Undo a block; the stream is imported again on the next sync."""
    from models.database import BlockedStream
    row = db.query(BlockedStream).filter(BlockedStream.id == block_id).first()
    if not row:
        raise HTTPException(404, "Not blocked")
    db.delete(row)
    db.commit()
    return {"ok": True}


class FixMatchBody(BaseModel):
    tmdb_id: int


@router.get("/fix-match/movie/{tmdb_id}/suggestions", dependencies=[Depends(require_admin)])
def fix_match_suggestions(tmdb_id: int, q: Optional[str] = None, db: Session = Depends(get_db)):
    """Films this VOD movie might really be — ranked by its real length when known."""
    from services.wrong_match import WrongMatchError, suggest_matches
    try:
        return suggest_matches(db, tmdb_id, q)
    except WrongMatchError as e:
        raise HTTPException(e.status, str(e))


class ReplaceCopyBody(BaseModel):
    season_number: Optional[int] = None
    episode_number: Optional[int] = None


@router.post("/replace/{media_type}/{tmdb_id}")
def replace_copy(media_type: str, tmdb_id: int, body: ReplaceCopyBody, db: Session = Depends(get_db),
                 user: TentacleUser = Depends(get_user_from_request)):
    """'Bad copy? Get another one': blocklist the release a downloaded file came
    from, delete the file and search for a different one. A movie, or one
    episode (season_number + episode_number). Admin, or whoever requested it."""
    from services.bad_copy import BadCopyError, replace_episode, replace_movie
    if media_type not in ("movie", "series"):
        raise HTTPException(400, "Invalid media type")
    if not user.is_admin and not db.query(DownloadRequest).filter(
            DownloadRequest.tmdb_id == tmdb_id, DownloadRequest.media_type == media_type,
            DownloadRequest.user_id == user.id).first():
        raise HTTPException(403, "You can only replace content you requested")
    try:
        if media_type == "movie":
            result = replace_movie(db, tmdb_id, user_name=user.display_name)
        else:
            if body.season_number is None or body.episode_number is None:
                raise HTTPException(400, "Which episode? (season_number and episode_number)")
            result = replace_episode(db, tmdb_id, body.season_number, body.episode_number,
                                     user_name=user.display_name)
    except BadCopyError as e:
        raise HTTPException(e.status, str(e))
    try:
        from routers.activity import invalidate_wanted_cache
        invalidate_wanted_cache()
    except Exception:
        pass
    return result


@router.get("/fix-match/movie/{tmdb_id}/frames", dependencies=[Depends(require_admin)])
def fix_match_frames(tmdb_id: int, db: Session = Depends(get_db)):
    """A few stills from the stream, for when its length and language aren't enough."""
    from services.wrong_match import WrongMatchError, stream_frames
    try:
        return stream_frames(db, tmdb_id)
    except WrongMatchError as e:
        raise HTTPException(e.status, str(e))


@router.post("/fix-match/movie/{tmdb_id}")
def fix_match(tmdb_id: int, body: FixMatchBody, db: Session = Depends(get_db),
              user: Optional[TentacleUser] = Depends(require_admin)):
    """This VOD movie is really `body.tmdb_id`: move it there and keep it there."""
    from services.wrong_match import WrongMatchError, rematch_movie
    try:
        result = rematch_movie(db, tmdb_id, body.tmdb_id, user_name=user.display_name if user else None)
    except WrongMatchError as e:
        raise HTTPException(e.status, str(e))
    emit_library_event("movie_removed", {"tmdb_id": tmdb_id, "media_type": "movie"})
    return result
