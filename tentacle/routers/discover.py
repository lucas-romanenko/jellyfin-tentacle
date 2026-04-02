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
        item["in_library"] = tid in known_ids
        result.append(item)
    return result


@router.get("")
def get_discover(
    type: str = "movies",
    db: Session = Depends(get_db)
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
        popular = tmdb.get_popular("series", page=1)
        if popular:
            sections.append({
                "id": "popular",
                "title": "Popular",
                "items": _dedup_and_mark(popular, known_ids),
            })

        # ── TV: On the Air ──
        on_the_air = tmdb.get_on_the_air(page=1)
        if on_the_air:
            sections.append({
                "id": "on_the_air",
                "title": "On the Air",
                "items": _dedup_and_mark(on_the_air, known_ids),
            })

        # ── TV: Top Rated ──
        top_rated = tmdb.get_top_rated("series", page=1)
        if top_rated:
            sections.append({
                "id": "top_rated",
                "title": "Top Rated",
                "items": _dedup_and_mark(top_rated, known_ids),
            })
    else:
        # ── Movies: Popular ──
        popular = tmdb.get_popular("movie", page=1)
        if popular:
            sections.append({
                "id": "popular",
                "title": "Popular",
                "items": _dedup_and_mark(popular, known_ids),
            })

        # ── Movies: Now Playing ──
        now_playing = tmdb.get_now_playing(page=1)
        if now_playing:
            sections.append({
                "id": "now_playing",
                "title": "Now Playing",
                "items": _dedup_and_mark(now_playing, known_ids),
            })

        # ── Movies: Upcoming ──
        upcoming = tmdb.get_upcoming(page=1)
        if upcoming:
            sections.append({
                "id": "upcoming",
                "title": "Upcoming",
                "items": _dedup_and_mark(upcoming, known_ids),
            })

    # ── From Your Lists (both types) ──
    missing = _get_missing_from_lists(db, known_ids, type)
    if missing:
        sections.append({
            "id": "missing",
            "title": "From My Lists",
            "items": missing,
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
