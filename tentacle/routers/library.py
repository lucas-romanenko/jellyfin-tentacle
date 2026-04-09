"""
Tentacle - Library Router
Unified view of movies and series
"""

import threading
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional
from models.database import get_db, get_setting, Movie, Series, ListItem, DownloadRequest, TentacleUser
from routers.auth import get_user_from_request
from services.logstream import library_event_generator, emit_library_event
from services.tmdb import TMDBService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/stream")
async def stream_library_events():
    """SSE endpoint for real-time library change events"""
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
    db: Session = Depends(get_db)
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
            items.append({
                "tmdb_id": li.tmdb_id,
                "imdb_id": li.imdb_id,
                "title": li.title or (f"TMDB {li.tmdb_id}" if li.tmdb_id else f"IMDb {li.imdb_id}"),
                "year": li.year,
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
def get_item_detail(media_type: str, tmdb_id: int, request: Request, db: Session = Depends(get_db)):
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

    # can_delete: True if downloaded content AND (admin OR user requested it)
    result["can_delete"] = False
    if item.source in ("radarr", "sonarr"):
        try:
            user = get_user_from_request(request, db)
            if user.is_admin:
                result["can_delete"] = True
            else:
                has_request = db.query(DownloadRequest).filter(
                    DownloadRequest.tmdb_id == tmdb_id,
                    DownloadRequest.media_type == media_type,
                    DownloadRequest.user_id == user.id,
                ).first()
                result["can_delete"] = bool(has_request)
        except HTTPException:
            pass

    return result


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

    Used by the C# plugin event handler when items are deleted through Jellyfin's native UI.
    """
    if media_type not in ("movie", "series"):
        raise HTTPException(400, "Invalid media type")

    model = Movie if media_type == "movie" else Series
    item = db.query(model).filter(model.tmdb_id == tmdb_id).first()
    deleted = False
    if item:
        title = item.title if hasattr(item, "title") else str(tmdb_id)
        db.delete(item)
        deleted = True

    # Also clean up DownloadRequest
    db.query(DownloadRequest).filter(
        DownloadRequest.tmdb_id == tmdb_id,
        DownloadRequest.media_type == media_type,
    ).delete()
    db.commit()

    if deleted:
        emit_library_event(f"{media_type}_removed", {"tmdb_id": tmdb_id, "media_type": media_type})

    # Remove from all users' playlists in background
    threading.Thread(
        target=_cleanup_playlists_all_users,
        args=(tmdb_id, media_type),
        daemon=True,
    ).start()

    return {"success": True, "deleted": deleted}


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
    jf_item_id = jellyfin_item_id
    jf = None
    if jf_url and jf_key:
        jf = JellyfinService(jf_url, jf_key, user.jellyfin_user_id)
        if not jf_item_id:
            jf_type = "Movie" if media_type == "movie" else "Series"
            jf_item = jf.search_by_tmdb_id(tmdb_id, media_type=jf_type)
            jf_item_id = jf_item["Id"] if jf_item else None

    # Delete from Radarr/Sonarr (removes files from disk)
    radarr_deleted = False
    sonarr_deleted = False
    if media_type == "movie":
        radarr_url = get_setting(db, "radarr_url", "")
        radarr_key = get_setting(db, "radarr_api_key", "")
        if radarr_url and radarr_key:
            from services.radarr import RadarrService
            radarr = RadarrService(radarr_url, radarr_key)
            radarr_deleted = radarr.delete_movie(tmdb_id, delete_files=True)
    else:
        sonarr_url = get_setting(db, "sonarr_url", "")
        sonarr_key = get_setting(db, "sonarr_api_key", "")
        if sonarr_url and sonarr_key:
            from services.sonarr import SonarrService
            sonarr = SonarrService(sonarr_url, sonarr_key)
            sonarr_deleted = sonarr.delete_series(tmdb_id, delete_files=True)

    # Delete from Jellyfin
    jf_deleted = False
    if jf_item_id and jf_url and jf_key:
        jf_deleted = jf.delete_item(jf_item_id)

    # Delete from Tentacle DB
    db.delete(item)
    db.query(DownloadRequest).filter(
        DownloadRequest.tmdb_id == tmdb_id,
        DownloadRequest.media_type == media_type,
    ).delete()
    db.commit()

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

    return {"success": True, "deleted": True, "title": title}


@router.get("/tmdb/{media_type}/{tmdb_id}")
def get_tmdb_detail(media_type: str, tmdb_id: int, db: Session = Depends(get_db)):
    """Fetch item details from TMDB (for items not in library)"""
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
def get_following_series(db: Session = Depends(get_db)):
    """Return all series being followed for new episodes."""
    series = db.query(Series).filter(
        Series.sonarr_monitored == True,
        or_(Series.status == None, ~Series.status.in_(["Ended", "Canceled"])),
    ).order_by(Series.title).all()
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


class FollowBody(BaseModel):
    follow: bool


@router.post("/follow/{tmdb_id}")
def toggle_follow(tmdb_id: int, body: FollowBody, db: Session = Depends(get_db)):
    """Enable or disable following for new episodes on a series."""
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
