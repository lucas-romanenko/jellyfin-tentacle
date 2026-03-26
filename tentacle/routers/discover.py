"""
Tentacle - Discover Router
Trending, popular, upcoming content from TMDB + missing from user lists
"""

import random
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import Optional
from models.database import get_db, get_setting, Movie, Series, ListSubscription, ListItem
from services.tmdb import TMDBService

router = APIRouter(prefix="/api/discover", tags=["discover"])


def _get_tmdb(db: Session) -> Optional[TMDBService]:
    from services.tmdb import get_tmdb_token
    bearer = get_tmdb_token(db)
    data_dir = get_setting(db, "data_dir", "/data")
    if not bearer:
        return None
    return TMDBService(bearer, data_dir)


def _known_tmdb_ids(db: Session) -> set:
    """Set of all TMDB IDs already in library."""
    movie_ids = {m.tmdb_id for m in db.query(Movie.tmdb_id).all()}
    series_ids = {s.tmdb_id for s in db.query(Series.tmdb_id).all()}
    return movie_ids | series_ids


def _dedup_and_mark(items: list, known_ids: set) -> list:
    """Deduplicate by tmdb_id and annotate in_library status."""
    seen = set()
    result = []
    for item in items:
        tid = item.get("tmdb_id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        if tid in known_ids:
            continue
        item["in_library"] = False
        result.append(item)
    return result


@router.get("")
def get_discover(
    type: str = "all",
    db: Session = Depends(get_db)
):
    """Return discover sections: trending, popular, from your lists, coming soon."""
    tmdb = _get_tmdb(db)
    if not tmdb:
        return {"sections": []}

    known_ids = _known_tmdb_ids(db)

    sections = []

    # ── Trending ──
    trending = []
    if type in ("all", "movies"):
        trending.extend(tmdb.get_trending("movie", page=1))
    if type in ("all", "series"):
        trending.extend(tmdb.get_trending("series", page=1))
    if trending:
        sections.append({
            "id": "trending",
            "title": "Trending This Week",
            "items": _dedup_and_mark(trending, known_ids),
        })

    # ── Popular ──
    popular = []
    if type in ("all", "movies"):
        popular.extend(tmdb.get_popular("movie", page=1))
    if type in ("all", "series"):
        popular.extend(tmdb.get_popular("series", page=1))
    if popular:
        sections.append({
            "id": "popular",
            "title": "Popular",
            "items": _dedup_and_mark(popular, known_ids),
        })

    # ── From Your Lists (missing items) ──
    missing = _get_missing_from_lists(db, known_ids, type)
    if missing:
        sections.append({
            "id": "missing",
            "title": "Missing from Your Lists",
            "items": missing,
        })

    # ── Coming Soon (digital/physical releases) ──
    if type in ("all", "movies"):
        upcoming = tmdb.get_upcoming(page=1)
        if upcoming:
            sections.append({
                "id": "upcoming",
                "title": "Coming Soon",
                "items": _dedup_and_mark(upcoming, known_ids),
            })

    return {"sections": sections}


def _get_missing_from_lists(db: Session, known_ids: set, type_filter: str) -> list:
    """Get items from active list subscriptions that aren't in the library."""
    active_lists = db.query(ListSubscription).filter(
        ListSubscription.active == True
    ).all()

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
        if item.tmdb_id in known_ids or item.tmdb_id in seen:
            continue
        if not item.poster_path:
            continue
        seen.add(item.tmdb_id)
        result.append({
            "tmdb_id": item.tmdb_id,
            "title": item.title or "Unknown",
            "year": item.year or "",
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
    db: Session = Depends(get_db)
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

    return details


@router.get("/search")
def search_discover(
    q: str = "",
    type: str = "all",
    db: Session = Depends(get_db)
):
    """Search TMDB for movies/series. Returns results with in_library excluded."""
    if not q or not q.strip():
        return {"items": []}

    tmdb = _get_tmdb(db)
    if not tmdb:
        return {"items": []}

    media_type = "all"
    if type == "movies":
        media_type = "movie"
    elif type == "series":
        media_type = "series"

    results = tmdb.search_multi_results(q, media_type)
    known_ids = _known_tmdb_ids(db)
    items = _dedup_and_mark(results, known_ids)
    return {"items": items}


@router.get("/config")
def get_discover_config(db: Session = Depends(get_db)):
    """Return whether the Discover tab is enabled for Jellyfin."""
    enabled = get_setting(db, "discover_in_jellyfin", "false")
    return {"discover_in_jellyfin": enabled.lower() == "true"}
