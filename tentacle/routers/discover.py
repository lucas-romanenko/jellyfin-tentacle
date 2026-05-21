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
from fastapi import Request
from models.database import get_db, get_setting, Movie, Series, ListSubscription, ListItem, DownloadRequest, LiveChannel, TentacleUser
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


def _get_missing_from_lists(db: Session, known_ids: set, type_filter: str, user: TentacleUser = None) -> list:
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

    poster = None
    backdrop = None
    for img in lookup.get("images", []):
        if img.get("coverType") == "poster" and img.get("remoteUrl"):
            poster = img["remoteUrl"]
        elif img.get("coverType") == "fanart" and img.get("remoteUrl"):
            backdrop = img["remoteUrl"]

    genres = [g.strip() for g in lookup.get("genres", [])]

    details = {
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

    return details


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
    """Convert Radarr lookup results to standard discover item format."""
    items = []
    for m in results:
        poster = None
        backdrop = None
        for img in m.get("images", []):
            if img.get("coverType") == "poster" and img.get("remoteUrl"):
                poster = img["remoteUrl"]
            elif img.get("coverType") == "fanart" and img.get("remoteUrl"):
                backdrop = img["remoteUrl"]
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

            tmdb_results = tmdb_future.result(timeout=15)
            sonarr_results = sonarr_future.result(timeout=15)
            radarr_results = radarr_future.result(timeout=15)

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
                item["in_library"] = tmdb_id in known_ids
            else:
                item["in_library"] = False
            if tvdb_id:
                seen_tvdb.add(tvdb_id)
            items.append(item)

        for item in _radarr_lookup_to_items(radarr_results):
            tmdb_id = item.get("tmdb_id")
            if tmdb_id and tmdb_id in seen_tmdb:
                continue
            if tmdb_id:
                seen_tmdb.add(tmdb_id)
                item["in_library"] = tmdb_id in known_ids
            else:
                item["in_library"] = False
            items.append(item)

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
