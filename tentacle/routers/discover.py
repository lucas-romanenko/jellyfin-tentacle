"""
Tentacle - Discover Router
Trending, popular, upcoming content from TMDB + missing from user lists
"""

import logging
import random
import re
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from models.database import get_db, get_setting, Movie, Series, ListSubscription, ListItem
from routers.auth import get_user_from_request
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

    # Enrich with library source info
    if media_type == "series":
        series = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
        if series:
            details["in_library"] = True
            details["library_source"] = series.source
        else:
            details["in_library"] = False
    else:
        movie = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
        details["in_library"] = bool(movie)

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
