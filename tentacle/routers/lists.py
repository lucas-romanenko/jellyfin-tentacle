"""
Tentacle - Lists Router
Trakt, Letterboxd, IMDb RSS list subscriptions
"""

import re
import logging
import threading
import requests
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from models.database import get_db, SessionLocal, ListSubscription, ListItem, Movie, Series, TentacleUser, DownloadRequest, get_setting, log_activity
from routers.auth import get_user_from_request
from services.nfo import update_nfo_tags
from services.tmdb import TMDBService
from pathlib import Path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lists", tags=["lists"])


def _record_download_request(db: Session, tmdb_id: int, media_type: str, user_id: int):
    """Record who requested a download so the scan can attribute it."""
    existing = db.query(DownloadRequest).filter(
        DownloadRequest.tmdb_id == tmdb_id,
        DownloadRequest.media_type == media_type,
    ).first()
    if not existing:
        db.add(DownloadRequest(tmdb_id=tmdb_id, media_type=media_type, user_id=user_id))
        db.commit()


def _get_radarr_root_folder(radarr_url: str, radarr_key: str) -> str:
    """Fetch the first root folder from Radarr's API.

    radarr_movies_path is Tentacle's container mount — NOT valid for Radarr API calls.
    Radarr needs its own internal path (e.g. /data/movies).
    """
    try:
        r = requests.get(
            f"{radarr_url.rstrip('/')}/api/v3/rootfolder",
            headers={"X-Api-Key": radarr_key},
            timeout=10,
        )
        r.raise_for_status()
        folders = r.json()
        if folders:
            return folders[0]["path"]
    except Exception as e:
        logger.warning(f"Failed to fetch Radarr root folders: {e}")
    return "/data/movies"  # sensible fallback


@router.get("/radarr-profiles")
def radarr_profiles(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Get Radarr quality profiles (accessible to all users for Add to Radarr modal)."""
    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    if not radarr_url or not radarr_key:
        raise HTTPException(400, "Radarr not configured")
    try:
        r = requests.get(f"{radarr_url.rstrip('/')}/api/v3/qualityprofile",
                         headers={"X-Api-Key": radarr_key}, timeout=10)
        r.raise_for_status()
        return [{"id": p["id"], "name": p["name"]} for p in r.json()]
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch Radarr profiles: {e}")


@router.get("/sonarr-profiles")
def sonarr_profiles(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Get Sonarr quality profiles (accessible to all users for Add to Sonarr modal)."""
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        raise HTTPException(400, "Sonarr not configured")
    try:
        r = requests.get(f"{sonarr_url.rstrip('/')}/api/v3/qualityprofile",
                         headers={"X-Api-Key": sonarr_key}, timeout=10)
        r.raise_for_status()
        return [{"id": p["id"], "name": p["name"]} for p in r.json()]
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch Sonarr profiles: {e}")


class ListCreate(BaseModel):
    name: str
    type: str
    url: str
    tag: str
    auto_add_radarr: bool = False
    active: bool = True


class AddMissingBody(BaseModel):
    tmdb_ids: Optional[list] = None
    quality_profile_id: Optional[int] = None
    monitor: Optional[str] = None
    season_folder: Optional[bool] = None
    selected_episodes: Optional[list] = None  # [{season, episode}] for custom episode picker


def fetch_list_tmdb_ids(lst: ListSubscription, bearer_token: str = "", trakt_client_id: str = "") -> list:
    """Fetch TMDB IDs from a list URL"""
    if lst.type == "imdb_rss":
        return fetch_imdb_rss(lst.url, bearer_token=bearer_token)
    elif lst.type == "letterboxd":
        return fetch_letterboxd_rss(lst.url)
    elif lst.type == "trakt":
        return fetch_trakt_list(lst.url, client_id=trakt_client_id)
    return []


_IMDB_GQL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Map IMDb chart URL slugs → GraphQL ChartTitleType enum values
_IMDB_CHART_MAP = {
    "top": "TOP_RATED_MOVIES",
    "moviemeter": "MOST_POPULAR_MOVIES",
    "toptv": "TOP_RATED_TV_SHOWS",
    "tvmeter": "MOST_POPULAR_TV_SHOWS",
    "bottom": "LOWEST_RATED_MOVIES",
    "top-english-movies": "TOP_RATED_ENGLISH_MOVIES",
}


def _parse_imdb_url(url: str) -> dict:
    """Parse an IMDb URL and return its type and identifier.
    Returns: {"type": "list"|"chart"|"user", "id": str, "chart_type": str|None}
    """
    # /list/ls055592025/
    m = re.search(r'/list/(ls\d+)', url)
    if m:
        return {"type": "list", "id": m.group(1)}
    # /chart/moviemeter/, /chart/top/, /chart/toptv/
    m = re.search(r'/chart/([\w-]+)', url)
    if m:
        slug = m.group(1)
        chart_type = _IMDB_CHART_MAP.get(slug)
        return {"type": "chart", "id": slug, "chart_type": chart_type}
    # /user/ur62440355/watchlist or /user/ur62440355/ratings
    m = re.search(r'/user/(ur\d+)', url)
    if m:
        return {"type": "user", "id": m.group(1)}
    return {"type": "unknown", "id": ""}


def _fetch_imdb_graphql_chart(chart_type: str) -> list:
    """Fetch an IMDb chart via GraphQL. Works for all chart types."""
    tv_types = {"tvSeries", "tvMiniSeries", "tvMovie", "tvSpecial"}
    query = (
        '{ chartTitles(chart: {chartType: %s}, first: 250) { '
        'edges { node { id titleText { text } titleType { id } releaseYear { year } } } '
        'total } }' % chart_type
    )
    try:
        r = requests.post(
            "https://graphql.imdb.com/",
            json={"query": query},
            headers={"Content-Type": "application/json", "User-Agent": _IMDB_GQL_UA},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        edges = data.get("data", {}).get("chartTitles", {}).get("edges", [])
        result = []
        for edge in edges:
            node = edge.get("node", {})
            imdb_id = node.get("id", "")
            if not imdb_id:
                continue
            title_type = node.get("titleType", {}).get("id", "movie")
            media_type = "series" if title_type in tv_types else "movie"
            year = node.get("releaseYear", {})
            result.append({
                "imdb_id": imdb_id,
                "title": node.get("titleText", {}).get("text", ""),
                "year": str(year.get("year", "")) if year else "",
                "media_type": media_type,
            })
        logger.info(f"IMDb GraphQL chart {chart_type}: {len(result)} items")
        return result
    except Exception as e:
        logger.error(f"IMDb GraphQL chart {chart_type} failed: {e}")
        return []


def _fetch_imdb_graphql_list(list_id: str) -> list:
    """Fetch an IMDb user list via GraphQL. Supports pagination."""
    tv_types = {"tvSeries", "tvMiniSeries", "tvMovie", "tvSpecial"}
    result = []
    after = ""

    while True:
        after_arg = f', after: "{after}"' if after else ""
        query = (
            'query { list(id: "%s") { items(first: 250%s) { '
            'total edges { node { item { ... on Title { '
            'id titleText { text } titleType { id } releaseYear { year } '
            '} } } cursor } pageInfo { hasNextPage endCursor } } } }'
            % (list_id, after_arg)
        )

        try:
            r = requests.post(
                "https://graphql.imdb.com/",
                json={"query": query},
                headers={"Content-Type": "application/json", "User-Agent": _IMDB_GQL_UA},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()

            items_data = data.get("data", {}).get("list", {}).get("items", {})
            edges = items_data.get("edges", [])
            if not edges:
                break

            for edge in edges:
                item = edge.get("node", {}).get("item", {})
                imdb_id = item.get("id", "")
                if not imdb_id:
                    continue
                title_type = item.get("titleType", {}).get("id", "movie")
                media_type = "series" if title_type in tv_types else "movie"
                year = item.get("releaseYear", {})
                result.append({
                    "imdb_id": imdb_id,
                    "title": item.get("titleText", {}).get("text", ""),
                    "year": str(year.get("year", "")) if year else "",
                    "media_type": media_type,
                })

            page_info = items_data.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                after = page_info["endCursor"]
            else:
                break
        except Exception as e:
            logger.warning(f"IMDb GraphQL list {list_id} failed: {e}")
            break

    movies = sum(1 for i in result if i["media_type"] == "movie")
    series = sum(1 for i in result if i["media_type"] == "series")
    logger.info(f"IMDb GraphQL list: {len(result)} items ({movies} movies, {series} series)")
    return result


def _fetch_imdb_servarr(list_id: str) -> list:
    """Last-resort fallback: fetch via Servarr API (movie-only, limited chart support)."""
    if not list_id:
        return []
    try:
        api_url = f"https://radarrapi.servarr.com/v1/list/imdb/{list_id}"
        r = requests.get(api_url, timeout=20)
        r.raise_for_status()
        data = r.json()
        result = []
        for item in data:
            entry = {}
            if item.get("ImdbId"):
                entry["imdb_id"] = item["ImdbId"]
            if item.get("TmdbId"):
                entry["tmdb_id"] = item["TmdbId"]
            if item.get("Title"):
                entry["title"] = item["Title"]
            if item.get("Year"):
                entry["year"] = str(item["Year"])
            if entry.get("imdb_id") or entry.get("tmdb_id"):
                result.append(entry)
        logger.info(f"IMDb Servarr fallback: {len(result)} items (movies only)")
        return result
    except Exception as e:
        logger.error(f"Servarr API failed for {list_id}: {e}")
        return []


def fetch_imdb_rss(url: str, bearer_token: str = "") -> list:
    """Fetch any IMDb URL — charts, lists, user pages. All via GraphQL, Servarr as fallback."""
    parsed = _parse_imdb_url(url)

    if parsed["type"] == "chart":
        if parsed.get("chart_type"):
            # Known chart → use GraphQL chartTitles query
            items = _fetch_imdb_graphql_chart(parsed["chart_type"])
            if items:
                return items
        # Unknown chart or GraphQL failed → try Servarr
        servarr_id = "top250" if parsed["id"] == "top" else parsed["id"]
        return _fetch_imdb_servarr(servarr_id)

    if parsed["type"] == "list":
        # User list → GraphQL list query, Servarr fallback
        items = _fetch_imdb_graphql_list(parsed["id"])
        if items:
            return items
        return _fetch_imdb_servarr(parsed["id"])

    if parsed["type"] == "user":
        # User watchlist/ratings → try Servarr
        return _fetch_imdb_servarr(parsed["id"])

    logger.error(f"Unrecognized IMDb URL format: {url}")
    return []


def _fetch_letterboxd_film(slug: str, session: requests.Session) -> dict:
    """Fetch TMDB ID from a single Letterboxd film page."""
    r = session.get(f"https://letterboxd.com/film/{slug}/", timeout=10)
    r.raise_for_status()
    tmdb_match = re.search(r'data-tmdb-id="(\d+)"', r.text)
    if not tmdb_match:
        return None
    tmdb_type = re.search(r'data-tmdb-type="(\w+)"', r.text)
    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
    year_match = re.search(r'<small><a href="/films/year/(\d{4})/', r.text)
    return {
        "tmdb_id": int(tmdb_match.group(1)),
        "title": title_match.group(1) if title_match else slug.replace("-", " ").title(),
        "year": year_match.group(1) if year_match else None,
        "media_type": "series" if tmdb_type and tmdb_type.group(1) == "tv" else "movie",
    }


def fetch_letterboxd_rss(url: str) -> list:
    """Fetch Letterboxd list via HTML scraping (RSS blocked by Cloudflare)"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    session = requests.Session()
    session.headers.update({"User-Agent": _UA})

    try:
        # Collect all film slugs across pages
        all_slugs = []
        page = 1
        while True:
            page_url = f"{url.rstrip('/')}/page/{page}/"
            r = session.get(page_url, timeout=15)
            if r.status_code == 404 and page > 1:
                break
            r.raise_for_status()

            slugs = re.findall(r'data-target-link="/film/([^/]+)/"', r.text)
            if not slugs:
                break
            all_slugs.extend(slugs)

            if f'/page/{page + 1}/' not in r.text:
                break
            page += 1

        logger.info(f"Letterboxd: found {len(all_slugs)} film slugs, fetching TMDB IDs...")

        # Fetch film pages in parallel
        items = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_letterboxd_film, slug, session): slug for slug in all_slugs}
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    result = future.result()
                    if result:
                        items.append(result)
                except Exception as e:
                    logger.debug(f"Letterboxd: failed to fetch film '{slug}': {e}")

        logger.info(f"Letterboxd: resolved {len(items)} films with TMDB IDs")
        return items
    except Exception as e:
        logger.error(f"Failed to fetch Letterboxd {url}: {e}")
        return []


def fetch_trakt_list(url: str, client_id: str = "") -> list:
    """Fetch Trakt list via API"""
    if not client_id:
        logger.error("Trakt client ID not configured — add it in Settings → Connections")
        return []
    try:
        # Convert URL to API endpoint
        # e.g. https://trakt.tv/users/username/lists/listname
        # → https://api.trakt.tv/users/username/lists/listname/items
        api_url = url.rstrip('/').replace('trakt.tv', 'api.trakt.tv') + '/items'
        r = requests.get(api_url, timeout=15, headers={
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": client_id,
        })
        r.raise_for_status()
        items = r.json()
        result = []
        for item in items:
            if item.get("movie"):
                m = item["movie"]
                ids = m.get("ids", {})
                if ids.get("tmdb"):
                    result.append({"tmdb_id": ids["tmdb"], "title": m.get("title"), "media_type": "movie"})
            elif item.get("show"):
                s = item["show"]
                ids = s.get("ids", {})
                if ids.get("tmdb"):
                    result.append({"tmdb_id": ids["tmdb"], "title": s.get("title"), "media_type": "series"})
        logger.info(f"Trakt: found {len(result)} items")
        return result
    except Exception as e:
        logger.error(f"Failed to fetch Trakt list {url}: {e}")
        return []


def store_list_items(lst: ListSubscription, items: list, db: Session) -> dict:
    """Store fetched items in ListItem table (replace old entries).
    Returns stats: {stored, skipped_no_tmdb, skipped_duplicate}."""
    db.query(ListItem).filter(ListItem.list_id == lst.id).delete()
    seen_tmdb = set()
    stored = 0
    skipped_no_tmdb = 0
    skipped_duplicate = 0
    for item in items:
        tmdb_id = item.get("tmdb_id")
        imdb_id = item.get("imdb_id")
        if not tmdb_id:
            skipped_no_tmdb += 1
            title = item.get("title", "unknown")
            logger.debug(f"Skipping '{title}' — no TMDB match")
            continue
        # Skip duplicate TMDB IDs (same movie with different IMDb entries)
        if tmdb_id in seen_tmdb:
            skipped_duplicate += 1
            continue
        seen_tmdb.add(tmdb_id)
        db.add(ListItem(
            list_id=lst.id,
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            media_type=item.get("media_type", "movie"),
            title=item.get("title"),
            year=item.get("year"),
            poster_path=item.get("poster_path"),
        ))
        stored += 1
    db.flush()
    if skipped_no_tmdb or skipped_duplicate:
        logger.info(f"List '{lst.name}': stored {stored}, skipped {skipped_no_tmdb} (no TMDB match), {skipped_duplicate} (duplicate)")
    return {"stored": stored, "skipped_no_tmdb": skipped_no_tmdb, "skipped_duplicate": skipped_duplicate}


def apply_list_tags_to_library(items: list, tag: str, db: Session) -> int:
    """Match list items to library content and apply tag via NFO update"""
    tagged = 0

    for item in items:
        tmdb_id = item.get("tmdb_id")
        imdb_id = item.get("imdb_id")

        # Find in movies
        movie = None
        if tmdb_id:
            movie = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()

        if movie:
            tags = list(movie.tags or [])
            if tag not in tags:
                tags.append(tag)
                movie.tags = tags
                if movie.nfo_path:
                    update_nfo_tags(Path(movie.nfo_path), tags)
                tagged += 1
            continue

        # Find in series
        series = None
        if tmdb_id:
            series = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()

        if series:
            tags = list(series.tags or [])
            if tag not in tags:
                tags.append(tag)
                series.tags = tags
                if series.nfo_path:
                    update_nfo_tags(Path(series.nfo_path), tags)
                tagged += 1

    db.commit()
    return tagged


@router.get("")
def get_lists(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    lists = db.query(ListSubscription).filter(
        ListSubscription.user_id == user.id,
    ).order_by(ListSubscription.created_at.desc()).all()
    return [
        {
            "id": l.id,
            "name": l.name,
            "type": l.type,
            "url": l.url,
            "tag": l.tag,
            "active": l.active,
            "auto_add_radarr": l.auto_add_radarr,
            "playlist_enabled": l.playlist_enabled,
            "last_fetched": l.last_fetched,
            "last_item_count": l.last_item_count,
            "created_at": l.created_at,
        }
        for l in lists
    ]


@router.post("")
def create_list(body: ListCreate, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    lst = ListSubscription(
        name=body.name,
        type=body.type,
        url=body.url,
        tag=body.tag,
        auto_add_radarr=body.auto_add_radarr,
        active=body.active,
        user_id=user.id,
    )
    db.add(lst)
    db.commit()
    db.refresh(lst)
    return {"id": lst.id, "success": True}


@router.post("/add-to-radarr")
def add_to_radarr(body: AddMissingBody, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Add specific TMDB IDs to Radarr (standalone, no list required)"""
    if not body.tmdb_ids:
        raise HTTPException(400, "No tmdb_ids provided")

    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    if not radarr_url or not radarr_key:
        raise HTTPException(400, "Radarr not configured")

    root_folder = _get_radarr_root_folder(radarr_url, radarr_key)
    quality_profile_id = body.quality_profile_id or int(get_setting(db, "radarr_quality_profile_id", "1") or "1")
    added = 0
    already_exists = 0
    failed = 0
    release_date = None

    for tmdb_id in body.tmdb_ids:
        if db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first():
            already_exists += 1
            continue
        try:
            r = requests.post(
                f"{radarr_url.rstrip('/')}/api/v3/movie",
                headers={"X-Api-Key": radarr_key},
                json={
                    "tmdbId": tmdb_id,
                    "monitored": True,
                    "qualityProfileId": quality_profile_id,
                    "rootFolderPath": root_folder,
                    "addOptions": {"searchForMovie": True},
                },
                timeout=10,
            )
            if r.status_code < 400:
                added += 1
                _record_download_request(db, tmdb_id, "movie", user.id)
                # Extract release date for feedback
                if not release_date:
                    try:
                        movie_data = r.json()
                        from datetime import datetime as _dt
                        now = _dt.utcnow()
                        for field in ("digitalRelease", "physicalRelease", "inCinemas"):
                            val = movie_data.get(field)
                            if val:
                                try:
                                    dt = _dt.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
                                    if dt > now:
                                        release_date = val[:10]
                                        break
                                except (ValueError, TypeError):
                                    pass
                    except Exception:
                        pass
            elif r.status_code == 400 and "MovieExistsValidator" in r.text:
                already_exists += 1
            else:
                logger.error(f"Radarr rejected tmdb:{tmdb_id} — HTTP {r.status_code}: {r.text}")
                failed += 1
        except Exception as e:
            logger.error(f"Failed to add tmdb:{tmdb_id} to Radarr: {e}")
            failed += 1

    result = {"added": added, "already_exists": already_exists, "failed": failed}
    if release_date:
        result["release_date"] = release_date
    return result


@router.post("/add-to-sonarr")
def add_to_sonarr(body: AddMissingBody, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Add specific TMDB IDs to Sonarr"""
    if not body.tmdb_ids:
        raise HTTPException(400, "No tmdb_ids provided")

    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        raise HTTPException(400, "Sonarr not configured")

    from services.sonarr import SonarrService
    sonarr = SonarrService(sonarr_url, sonarr_key)
    root_folders = sonarr.get_root_folders()
    root_folder = root_folders[0]["path"] if root_folders else "/data/tv"
    quality_profile_id = body.quality_profile_id or int(get_setting(db, "sonarr_quality_profile_id", "1") or "1")

    added = 0
    already_exists = 0
    failed = 0

    monitor = body.monitor or "all"
    season_folder = body.season_folder if body.season_folder is not None else True

    for tmdb_id in body.tmdb_ids:
        existing = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()
        if existing and existing.source == "sonarr":
            already_exists += 1
            continue
        result = sonarr.add_series(tmdb_id, quality_profile_id, root_folder, monitor=monitor, season_folder=season_folder, selected_episodes=body.selected_episodes)
        if result:
            added += 1
            _record_download_request(db, tmdb_id, "series", user.id)
            # Mark VOD series with sonarr_path so scan skips duplicate detection
            if existing and existing.source.startswith("provider_") and result.get("path"):
                existing.sonarr_path = result["path"]
                db.commit()
        else:
            failed += 1

    return {"added": added, "already_exists": already_exists, "failed": failed}


@router.delete("/{list_id}")
def delete_list(list_id: int, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    lst = db.query(ListSubscription).filter(
        ListSubscription.id == list_id,
        ListSubscription.user_id == user.id,
    ).first()
    if not lst:
        raise HTTPException(404, "List not found")
    db.delete(lst)
    db.commit()
    return {"success": True}


@router.post("/{list_id}/playlist-toggle")
def toggle_list_playlist(list_id: int, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Toggle whether this list generates a per-user Jellyfin playlist"""
    lst = db.query(ListSubscription).filter(
        ListSubscription.id == list_id,
        ListSubscription.user_id == user.id,
    ).first()
    if not lst:
        raise HTTPException(404, "List not found")
    lst.playlist_enabled = not lst.playlist_enabled
    db.commit()

    # Per-user sync in background
    target_user_id = user.id

    def _bg_sync():
        from services.smartlists import sync_smartlists
        bg_db = SessionLocal()
        try:
            sync_smartlists(bg_db, user_id=target_user_id)
        except Exception as e:
            logging.getLogger(__name__).error(f"Background smartlist sync failed: {e}")
        finally:
            bg_db.close()

    threading.Thread(target=_bg_sync, daemon=True).start()

    return {"success": True, "playlist_enabled": lst.playlist_enabled}


@router.post("/refresh-all")
def refresh_all_lists(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Re-fetch all active per-user lists with TMDB enrichment. Fixes missing metadata."""
    active_lists = db.query(ListSubscription).filter(
        ListSubscription.active == True,
        ListSubscription.user_id == user.id,
    ).all()
    if not active_lists:
        return {"success": True, "refreshed": 0, "message": "No active lists"}

    from services.tmdb import get_tmdb_token
    bearer = get_tmdb_token(db)
    trakt_cid = get_setting(db, "trakt_client_id") or ""
    tmdb = _get_tmdb_service(db)

    refreshed = 0
    errors = []
    for lst in active_lists:
        try:
            items = fetch_list_tmdb_ids(lst, bearer_token=bearer, trakt_client_id=trakt_cid)
            if not items:
                errors.append(f"{lst.name}: empty or failed")
                continue
            if tmdb:
                enrich_items_with_tmdb(items, tmdb)
            store_stats = store_list_items(lst, items, db)
            apply_list_tags_to_library(items, lst.tag, db)
            lst.last_fetched = datetime.utcnow()
            lst.last_item_count = store_stats["stored"]
            refreshed += 1
            logger.info(f"Refreshed list '{lst.name}': {store_stats['stored']} items")
        except Exception as e:
            errors.append(f"{lst.name}: {e}")
            logger.warning(f"Failed to refresh list '{lst.name}': {e}")

    db.commit()
    log_activity(db, "lists_refresh", f"Refreshed {refreshed} lists")
    return {"success": True, "refreshed": refreshed, "errors": errors}


@router.post("/{list_id}/fetch")
def fetch_list(list_id: int, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Fetch list items and apply tags to matching library content"""
    lst = db.query(ListSubscription).filter(
        ListSubscription.id == list_id,
        ListSubscription.user_id == user.id,
    ).first()
    if not lst:
        raise HTTPException(404, "List not found")

    from services.tmdb import get_tmdb_token
    bearer_token = get_tmdb_token(db)
    trakt_client_id = get_setting(db, "trakt_client_id") or ""
    items = fetch_list_tmdb_ids(lst, bearer_token=bearer_token, trakt_client_id=trakt_client_id)
    if not items:
        raise HTTPException(400, "Failed to fetch list or list is empty")

    # Enrich items with metadata from TMDB (poster, title, year)
    tmdb = _get_tmdb_service(db)
    if tmdb:
        enrich_items_with_tmdb(items, tmdb)

    # Store items with metadata
    store_stats = store_list_items(lst, items, db)

    tagged = apply_list_tags_to_library(items, lst.tag, db)

    lst.last_fetched = datetime.utcnow()
    lst.last_item_count = store_stats["stored"]
    db.commit()

    # Auto-add missing to Radarr
    radarr_added = 0
    if lst.auto_add_radarr:
        radarr_url = get_setting(db, "radarr_url")
        radarr_key = get_setting(db, "radarr_api_key")
        if radarr_url and radarr_key:
            root_folder = _get_radarr_root_folder(radarr_url, radarr_key)
            for item in items:
                tmdb_id = item.get("tmdb_id")
                if not tmdb_id:
                    continue
                # Check if in library
                exists = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
                if not exists:
                    try:
                        r = requests.post(
                            f"{radarr_url.rstrip('/')}/api/v3/movie",
                            headers={"X-Api-Key": radarr_key},
                            json={
                                "tmdbId": tmdb_id,
                                "monitored": True,
                                "qualityProfileId": 1,
                                "rootFolderPath": root_folder,
                                "addOptions": {"searchForMovie": True}
                            },
                            timeout=10
                        )
                        if r.status_code < 400:
                            radarr_added += 1
                        else:
                            logger.error(f"Radarr rejected tmdb:{tmdb_id} — HTTP {r.status_code}: {r.text}")
                    except Exception as e:
                        logger.error(f"Failed to add tmdb:{tmdb_id} to Radarr: {e}")

    log_activity(db, "list_fetch", f"Fetched '{lst.name}' — {store_stats['stored']} items stored")

    return {
        "success": True,
        "fetched": len(items),
        "stored": store_stats["stored"],
        "skipped_no_tmdb": store_stats["skipped_no_tmdb"],
        "skipped_duplicate": store_stats["skipped_duplicate"],
        "tagged": tagged,
        "radarr_added": radarr_added,
    }


def _get_tmdb_service(db: Session) -> Optional[TMDBService]:
    from services.tmdb import get_tmdb_token
    token = get_tmdb_token(db)
    if not token:
        return None
    cache_dir = get_setting(db, "data_dir", "/data")
    return TMDBService(bearer_token=token, cache_dir=cache_dir)


def enrich_items_with_tmdb(items: list, tmdb: TMDBService):
    """Enrich list items with TMDB metadata (poster, title, year).
    Modifies items in-place. Reusable by both the fetch endpoint and scheduled sync."""
    for item in items:
        tid = item.get("tmdb_id")
        imdb_id = item.get("imdb_id")

        if not tid and imdb_id:
            details = tmdb.find_by_imdb_id(imdb_id)
            if details:
                item["tmdb_id"] = details.get("tmdb_id")
                item.setdefault("title", details.get("title"))
                item.setdefault("year", str(details.get("year", "") or ""))
                item["poster_path"] = details.get("poster_path")
                item["media_type"] = details.get("media_type", "movie")
            continue

        if tid and not item.get("poster_path"):
            if item.get("media_type") == "series":
                details = tmdb.get_series_details(tid)
            else:
                details = tmdb.get_movie_details(tid)
            if details:
                item.setdefault("title", details.get("title"))
                item.setdefault("year", str(details.get("year", "") or ""))
                item["poster_path"] = details.get("poster_path")
                item.setdefault("media_type", details.get("media_type", "movie"))


@router.get("/{list_id}/coverage")
def get_list_coverage(list_id: int, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Get coverage breakdown: VOD / Radarr / Missing for a list"""
    lst = db.query(ListSubscription).filter(
        ListSubscription.id == list_id,
        ListSubscription.user_id == user.id,
    ).first()
    if not lst:
        raise HTTPException(404, "List not found")

    items = db.query(ListItem).filter(ListItem.list_id == list_id).all()
    if not items:
        return {"list_id": list_id, "name": lst.name, "total": 0, "vod": [], "radarr": [], "missing": []}

    # Batch-load all movies/series matching tmdb_ids in the list
    tmdb_ids = [item.tmdb_id for item in items if item.tmdb_id]
    movie_map = {}
    series_map = {}
    if tmdb_ids:
        movies = db.query(Movie).filter(Movie.tmdb_id.in_(tmdb_ids)).all()
        movie_map = {m.tmdb_id: m for m in movies}
        series = db.query(Series).filter(Series.tmdb_id.in_(tmdb_ids)).all()
        series_map = {s.tmdb_id: s for s in series}

    vod = []
    radarr = []
    sonarr = []
    missing = []

    for item in items:
        tid = item.tmdb_id
        movie = movie_map.get(tid) if tid else None
        serie = series_map.get(tid) if tid else None

        if movie:
            entry = {"tmdb_id": tid, "title": movie.title, "year": movie.year, "poster_path": movie.poster_path, "media_type": "movie"}
            if movie.source.startswith("provider_"):
                vod.append(entry)
            elif movie.source == "radarr":
                radarr.append(entry)
            else:
                vod.append(entry)
        elif serie:
            entry = {"tmdb_id": tid, "title": serie.title, "year": serie.year, "poster_path": serie.poster_path, "media_type": "series"}
            if serie.source == "sonarr":
                sonarr.append(entry)
            else:
                vod.append(entry)
        else:
            # Use cached metadata from ListItem
            missing.append({
                "tmdb_id": tid,
                "imdb_id": item.imdb_id,
                "title": item.title or (f"TMDB {tid}" if tid else f"IMDb {item.imdb_id}"),
                "year": item.year,
                "poster_path": item.poster_path,
                "media_type": item.media_type or "movie",
            })

    vod.sort(key=lambda x: (x.get("title") or "").lower())
    radarr.sort(key=lambda x: (x.get("title") or "").lower())
    sonarr.sort(key=lambda x: (x.get("title") or "").lower())
    missing.sort(key=lambda x: (x.get("title") or "").lower())

    # Count missing by type
    missing_movies = sum(1 for m in missing if m.get("media_type") != "series")
    missing_series = sum(1 for m in missing if m.get("media_type") == "series")

    return {
        "list_id": list_id,
        "name": lst.name,
        "total": len(items),
        "vod_count": len(vod),
        "radarr_count": len(radarr),
        "sonarr_count": len(sonarr),
        "missing_count": len(missing),
        "missing_movies": missing_movies,
        "missing_series": missing_series,
        "vod": vod,
        "radarr": radarr,
        "sonarr": sonarr,
        "missing": missing,
    }



@router.post("/{list_id}/add-missing-to-radarr")
def add_missing_to_radarr(list_id: int, body: AddMissingBody = None, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Add missing list items to Radarr"""
    lst = db.query(ListSubscription).filter(
        ListSubscription.id == list_id,
        ListSubscription.user_id == user.id,
    ).first()
    if not lst:
        raise HTTPException(404, "List not found")

    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    if not radarr_url or not radarr_key:
        raise HTTPException(400, "Radarr not configured")

    root_folder = _get_radarr_root_folder(radarr_url, radarr_key)
    quality_profile_id = (body.quality_profile_id if body else None) or int(get_setting(db, "radarr_quality_profile_id", "1") or "1")

    # Determine which tmdb_ids to add
    if body and body.tmdb_ids:
        target_ids = body.tmdb_ids
    else:
        # Get all missing: list items not in Movie table
        items = db.query(ListItem).filter(ListItem.list_id == list_id).all()
        all_ids = [item.tmdb_id for item in items]
        existing = {m.tmdb_id for m in db.query(Movie.tmdb_id).filter(Movie.tmdb_id.in_(all_ids)).all()}
        target_ids = [tid for tid in all_ids if tid not in existing]

    added = 0
    already_exists = 0
    failed = 0

    for tmdb_id in target_ids:
        existing = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
        if existing:
            already_exists += 1
            continue
        try:
            r = requests.post(
                f"{radarr_url.rstrip('/')}/api/v3/movie",
                headers={"X-Api-Key": radarr_key},
                json={
                    "tmdbId": tmdb_id,
                    "monitored": True,
                    "qualityProfileId": quality_profile_id,
                    "rootFolderPath": root_folder,
                    "addOptions": {"searchForMovie": True},
                },
                timeout=10,
            )
            if r.status_code < 400:
                added += 1
                _record_download_request(db, tmdb_id, "movie", user.id)
            else:
                logger.error(f"Radarr rejected tmdb:{tmdb_id} — HTTP {r.status_code}: {r.text}")
                failed += 1
        except Exception as e:
            logger.error(f"Failed to add tmdb:{tmdb_id} to Radarr: {e}")
            failed += 1

    return {"added": added, "already_exists": already_exists, "failed": failed}


@router.post("/{list_id}/add-missing-to-sonarr")
def add_missing_to_sonarr(list_id: int, body: AddMissingBody = None, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Add missing list items to Sonarr"""
    lst = db.query(ListSubscription).filter(
        ListSubscription.id == list_id,
        ListSubscription.user_id == user.id,
    ).first()
    if not lst:
        raise HTTPException(404, "List not found")

    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if not sonarr_url or not sonarr_key:
        raise HTTPException(400, "Sonarr not configured")

    from services.sonarr import SonarrService
    sonarr = SonarrService(sonarr_url, sonarr_key)
    root_folders = sonarr.get_root_folders()
    root_folder = root_folders[0]["path"] if root_folders else "/data/tv"
    quality_profile_id = (body.quality_profile_id if body else None) or int(get_setting(db, "sonarr_quality_profile_id", "1") or "1")

    if body and body.tmdb_ids:
        target_ids = body.tmdb_ids
    else:
        items = db.query(ListItem).filter(ListItem.list_id == list_id).all()
        all_ids = [item.tmdb_id for item in items]
        existing = {s.tmdb_id for s in db.query(Series.tmdb_id).filter(Series.tmdb_id.in_(all_ids)).all()}
        target_ids = [tid for tid in all_ids if tid not in existing]

    monitor = (body.monitor if body else None) or "all"
    season_folder = (body.season_folder if body else None)
    if season_folder is None:
        season_folder = True

    added = 0
    already_exists = 0
    failed = 0

    for tmdb_id in target_ids:
        if db.query(Series).filter(Series.tmdb_id == tmdb_id).first():
            already_exists += 1
            continue
        result = sonarr.add_series(tmdb_id, quality_profile_id, root_folder, monitor=monitor, season_folder=season_folder)
        if result:
            added += 1
            _record_download_request(db, tmdb_id, "series", user.id)
        else:
            failed += 1

    return {"added": added, "already_exists": already_exists, "failed": failed}
