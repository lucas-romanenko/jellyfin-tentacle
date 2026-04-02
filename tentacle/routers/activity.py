"""
Tentacle - Activity Router
Real-time download queue from Radarr/Sonarr + unreleased monitored items.
Queue data fetched fresh every request. Unreleased cached separately (5min).
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_db, get_setting, Movie, Series

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/activity", tags=["activity"])

# ── Separate cache for unreleased only (expensive, rarely changes) ────────
_unreleased_cache: dict = {"data": None, "ts": 0}
UNRELEASED_TTL = 300  # 5 minutes

# ── Throttled refresh (don't spam Radarr/Sonarr command queue) ────────────
_last_refresh: dict = {"radarr": 0, "sonarr": 0}
REFRESH_INTERVAL = 5  # seconds between RefreshMonitoredDownloads calls


def _trigger_refresh_throttled(key: str, url: str, api_key: str) -> None:
    """Tell Radarr/Sonarr to re-check download client progress.
    Throttled to once per REFRESH_INTERVAL to avoid command queue backlog."""
    now = time.time()
    if (now - _last_refresh[key]) < REFRESH_INTERVAL:
        return
    _last_refresh[key] = now
    try:
        requests.post(
            f"{url.rstrip('/')}/api/v3/command",
            headers={"X-Api-Key": api_key},
            json={"name": "RefreshMonitoredDownloads"},
            timeout=2,
        )
    except Exception:
        pass


def _fetch_radarr_queue(url: str, api_key: str) -> list:
    """Fetch active download queue from Radarr."""
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/queue",
            headers={"X-Api-Key": api_key},
            params={"pageSize": 100, "includeUnknownMovieItems": False, "includeMovie": True},
            timeout=5,
        )
        r.raise_for_status()
        return r.json().get("records", [])
    except Exception as e:
        logger.warning(f"Radarr queue fetch failed: {e}")
        return []


def _fetch_sonarr_queue(url: str, api_key: str) -> list:
    """Fetch active download queue from Sonarr."""
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/queue",
            headers={"X-Api-Key": api_key},
            params={"pageSize": 100, "includeUnknownSeriesItems": False, "includeSeries": True, "includeEpisode": True},
            timeout=5,
        )
        r.raise_for_status()
        return r.json().get("records", [])
    except Exception as e:
        logger.warning(f"Sonarr queue fetch failed: {e}")
        return []


def _fetch_radarr_unreleased(url: str, api_key: str) -> list:
    """Fetch monitored movies without files from Radarr."""
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/movie",
            headers={"X-Api-Key": api_key},
            timeout=15,
        )
        r.raise_for_status()
        movies = r.json()
        now = datetime.utcnow()
        unreleased = []
        for m in movies:
            if not m.get("monitored") or m.get("hasFile"):
                continue
            release = None
            for field in ("digitalRelease", "physicalRelease", "inCinemas"):
                val = m.get(field)
                if val:
                    try:
                        dt = datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
                        if dt > now and (release is None or dt < release):
                            release = dt
                    except (ValueError, TypeError):
                        pass
            if release:
                unreleased.append({
                    "tmdb_id": m.get("tmdbId"),
                    "title": m.get("title", ""),
                    "year": str(m.get("year", "")),
                    "media_type": "movie",
                    "source": "radarr",
                    "release_date": release.strftime("%Y-%m-%d"),
                    "status": "unreleased",
                    "radarr_poster": _extract_poster(m),
                })
        unreleased.sort(key=lambda x: x["release_date"])
        return unreleased
    except Exception as e:
        logger.debug(f"Radarr unreleased fetch failed: {e}")
        return []


def _format_time(timeleft: str) -> str:
    """Convert Radarr/Sonarr time string like '00:12:34' to readable format."""
    if not timeleft:
        return ""
    try:
        # Handle day format like "1.02:30:00" first
        if "." in timeleft.split(":")[0]:
            day_rest = timeleft.split(".")
            days = int(day_rest[0])
            rest = day_rest[1].split(":")
            h = int(rest[0]) if len(rest) > 0 else 0
            if days > 0:
                return f"{days}d {h}h"
            return f"{h}h"
        parts = timeleft.split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            if h == 0 and m == 0 and s == 0:
                return ""
            if h > 0:
                return f"{h}h {m}m"
            return f"{m}m {s}s" if m < 2 else f"{m}m"
    except (ValueError, IndexError):
        pass
    return timeleft


def _format_size(size_bytes) -> str:
    """Format bytes to human-readable size."""
    if not size_bytes:
        return ""
    try:
        size = float(size_bytes)
        if size >= 1073741824:
            return f"{size / 1073741824:.1f} GB"
        if size >= 1048576:
            return f"{size / 1048576:.0f} MB"
        return f"{size / 1024:.0f} KB"
    except (ValueError, TypeError):
        return ""


def _get_poster(db: Session, tmdb_id: int, media_type: str) -> Optional[str]:
    """Look up poster path from local DB."""
    if not tmdb_id:
        return None
    if media_type == "movie":
        m = db.query(Movie.poster_path).filter(Movie.tmdb_id == tmdb_id).first()
        return m[0] if m else None
    else:
        s = db.query(Series.poster_path).filter(Series.tmdb_id == tmdb_id).first()
        return s[0] if s else None


def _extract_poster(arr_item: dict) -> Optional[str]:
    """Extract TMDB poster path from Radarr/Sonarr images array."""
    for img in arr_item.get("images", []):
        if img.get("coverType") == "poster":
            # Try remoteUrl first (full TMDB URL like https://image.tmdb.org/t/p/original/abc.jpg)
            url = img.get("remoteUrl", "")
            if "/t/p/" in url:
                after = url.split("/t/p/")[1]  # "original/abc.jpg"
                slash_idx = after.find("/")
                if slash_idx >= 0:
                    return after[slash_idx:]  # "/abc.jpg"
            # Some Sonarr responses only have local proxy URLs — try url field too
            url = img.get("url", "")
            if "/t/p/" in url:
                after = url.split("/t/p/")[1]
                slash_idx = after.find("/")
                if slash_idx >= 0:
                    return after[slash_idx:]
    return None


def _fetch_tmdb_poster(tmdb_id: int, media_type: str, db: Session) -> Optional[str]:
    """Last-resort poster fetch from TMDB API."""
    try:
        from services.tmdb import TMDBService, get_tmdb_token
        bearer = get_tmdb_token(db)
        if not bearer:
            return None
        data_dir = get_setting(db, "data_dir", "/data")
        tmdb = TMDBService(bearer, data_dir)
        if media_type == "series":
            details = tmdb.get_series_details(tmdb_id)
        else:
            details = tmdb.get_movie_details(tmdb_id)
        return details.get("poster_path") if details else None
    except Exception:
        return None


def _build_downloads(db: Session) -> list:
    """Fetch queue from Radarr/Sonarr — always fresh, no cache."""
    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")

    downloads = []
    futures = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        # Fire throttled refresh commands (preps data for next poll cycle)
        if radarr_url and radarr_key:
            pool.submit(_trigger_refresh_throttled, "radarr", radarr_url, radarr_key)
            futures["radarr"] = pool.submit(_fetch_radarr_queue, radarr_url, radarr_key)
        if sonarr_url and sonarr_key:
            pool.submit(_trigger_refresh_throttled, "sonarr", sonarr_url, sonarr_key)
            futures["sonarr"] = pool.submit(_fetch_sonarr_queue, sonarr_url, sonarr_key)

        for key, future in futures.items():
            try:
                result = future.result(timeout=6)
                if key == "radarr":
                    for item in result:
                        movie = item.get("movie", {})
                        tmdb_id = movie.get("tmdbId")
                        total = item.get("size", 0) or 0
                        left = item.get("sizeleft", 0) or 0
                        progress = ((total - left) / total * 100) if total > 0 else 0
                        status = "downloading"
                        tracked = item.get("trackedDownloadStatus", "")
                        dl_state = item.get("trackedDownloadState", "")
                        if tracked == "warning":
                            status = "warning"
                        elif dl_state == "importPending":
                            status = "importing"
                        elif dl_state == "downloading" and progress == 0:
                            status = "queued"

                        poster = _get_poster(db, tmdb_id, "movie") or _extract_poster(movie) or _fetch_tmdb_poster(tmdb_id, "movie", db)
                        downloads.append({
                            "tmdb_id": tmdb_id,
                            "title": movie.get("title", item.get("title", "")),
                            "year": str(movie.get("year", "")),
                            "poster_path": poster,
                            "media_type": "movie",
                            "source": "radarr",
                            "status": status,
                            "progress": round(progress, 1),
                            "size_remaining": _format_size(item.get("sizeleft")),
                            "eta": _format_time(item.get("timeleft", "")),
                            "quality": item.get("quality", {}).get("quality", {}).get("name", ""),
                        })

                elif key == "sonarr":
                    for item in result:
                        series = item.get("series", {})
                        episode = item.get("episode", {})
                        tmdb_id = series.get("tmdbId")
                        total = item.get("size", 0) or 0
                        left = item.get("sizeleft", 0) or 0
                        progress = ((total - left) / total * 100) if total > 0 else 0
                        status = "downloading"
                        tracked = item.get("trackedDownloadStatus", "")
                        dl_state = item.get("trackedDownloadState", "")
                        if tracked == "warning":
                            status = "warning"
                        elif dl_state == "importPending":
                            status = "importing"
                        elif dl_state == "downloading" and progress == 0:
                            status = "queued"

                        ep_label = ""
                        if episode:
                            ep_label = f"S{episode.get('seasonNumber', 0):02d}E{episode.get('episodeNumber', 0):02d}"

                        poster = _get_poster(db, tmdb_id, "series") or _extract_poster(series) or _fetch_tmdb_poster(tmdb_id, "series", db)
                        downloads.append({
                            "tmdb_id": tmdb_id,
                            "title": series.get("title", item.get("title", "")),
                            "year": str(series.get("year", "")),
                            "poster_path": poster,
                            "media_type": "series",
                            "source": "sonarr",
                            "status": status,
                            "progress": round(progress, 1),
                            "size_remaining": _format_size(item.get("sizeleft")),
                            "eta": _format_time(item.get("timeleft", "")),
                            "quality": item.get("quality", {}).get("quality", {}).get("name", ""),
                            "episode": ep_label,
                        })
            except Exception as e:
                logger.debug(f"Activity fetch {key} failed: {e}")

    status_order = {"downloading": 0, "importing": 1, "queued": 2, "warning": 3}
    downloads.sort(key=lambda d: (status_order.get(d["status"], 9), -d["progress"]))
    return downloads


def _get_unreleased(db: Session) -> list:
    """Get unreleased movies — cached for 5 minutes (expensive call)."""
    now = time.time()
    if _unreleased_cache["data"] is not None and (now - _unreleased_cache["ts"]) < UNRELEASED_TTL:
        return _unreleased_cache["data"]

    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")

    if not radarr_url or not radarr_key:
        return []

    unreleased = _fetch_radarr_unreleased(radarr_url, radarr_key)
    # Enrich with posters (DB first, then Radarr fallback)
    for item in unreleased:
        item["poster_path"] = _get_poster(db, item.get("tmdb_id"), "movie") or item.pop("radarr_poster", None)

    result = unreleased[:20]
    _unreleased_cache["data"] = result
    _unreleased_cache["ts"] = now
    return result


@router.get("")
def get_activity(db: Session = Depends(get_db)):
    """Return current download queue (always fresh) and unreleased (5min cache)."""
    downloads = _build_downloads(db)
    unreleased = _get_unreleased(db)
    if downloads:
        logger.info(f"Activity: {len(downloads)} download(s) in queue")
    return {"downloads": downloads, "unreleased": unreleased}
