"""
Tentacle - Activity Router
Real-time download queue from Radarr/Sonarr + unreleased monitored items.
Designed for frequent polling (10-15s intervals) with server-side caching.
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import get_db, get_setting, Movie, Series

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/activity", tags=["activity"])

# ── In-memory cache (10s TTL) ─────────────────────────────────────────────
_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 10  # seconds


def _fetch_radarr_queue(url: str, api_key: str) -> list:
    """Fetch active download queue from Radarr."""
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/queue",
            headers={"X-Api-Key": api_key},
            params={"pageSize": 100, "includeUnknownMovieItems": False, "includeMovie": True},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("records", [])
    except Exception as e:
        logger.debug(f"Radarr queue fetch failed: {e}")
        return []


def _fetch_sonarr_queue(url: str, api_key: str) -> list:
    """Fetch active download queue from Sonarr."""
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/queue",
            headers={"X-Api-Key": api_key},
            params={"pageSize": 100, "includeUnknownSeriesItems": False, "includeSeries": True, "includeEpisode": True},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("records", [])
    except Exception as e:
        logger.debug(f"Sonarr queue fetch failed: {e}")
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
            # Check if any release date is in the future
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
        parts = timeleft.split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            if h > 0:
                return f"{h}h {m}m"
            return f"{m}m"
        # Handle day format like "1.02:30:00"
        if "." in timeleft:
            day_rest = timeleft.split(".")
            days = int(day_rest[0])
            rest = day_rest[1].split(":")
            h = int(rest[0]) if len(rest) > 0 else 0
            if days > 0:
                return f"{days}d {h}h"
            return f"{h}h"
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


def _build_activity(db: Session) -> dict:
    """Build activity data from Radarr/Sonarr APIs."""
    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")

    downloads = []
    unreleased = []

    # Parallel fetch from Radarr and Sonarr
    futures = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        if radarr_url and radarr_key:
            futures["radarr_queue"] = pool.submit(_fetch_radarr_queue, radarr_url, radarr_key)
            futures["radarr_unreleased"] = pool.submit(_fetch_radarr_unreleased, radarr_url, radarr_key)
        if sonarr_url and sonarr_key:
            futures["sonarr_queue"] = pool.submit(_fetch_sonarr_queue, sonarr_url, sonarr_key)

        for key, future in futures.items():
            try:
                result = future.result(timeout=15)
                if key == "radarr_queue":
                    for item in result:
                        movie = item.get("movie", {})
                        tmdb_id = movie.get("tmdbId")
                        # Calculate progress from size and sizeleft
                        total = item.get("size", 0) or 0
                        left = item.get("sizeleft", 0) or 0
                        progress = ((total - left) / total * 100) if total > 0 else 0
                        # Map Radarr status to simple status
                        status = "downloading"
                        tracked = item.get("trackedDownloadStatus", "")
                        dl_state = item.get("trackedDownloadState", "")
                        if tracked == "warning":
                            status = "warning"
                        elif dl_state == "importPending":
                            status = "importing"
                        elif dl_state == "downloading" and progress == 0:
                            status = "queued"

                        poster = _get_poster(db, tmdb_id, "movie")
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

                elif key == "sonarr_queue":
                    for item in result:
                        series = item.get("series", {})
                        episode = item.get("episode", {})
                        tmdb_id = series.get("tmdbId")
                        # Calculate progress from size and sizeleft
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

                        poster = _get_poster(db, tmdb_id, "series")
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

                elif key == "radarr_unreleased":
                    # Enrich with poster from local DB
                    for item in result:
                        item["poster_path"] = _get_poster(db, item.get("tmdb_id"), "movie")
                    unreleased.extend(result)
            except Exception as e:
                logger.debug(f"Activity fetch {key} failed: {e}")

    # Sort downloads: actively downloading first, then by progress desc
    status_order = {"downloading": 0, "importing": 1, "queued": 2, "warning": 3}
    downloads.sort(key=lambda d: (status_order.get(d["status"], 9), -d["progress"]))

    return {"downloads": downloads, "unreleased": unreleased[:20]}


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


@router.get("")
def get_activity(db: Session = Depends(get_db)):
    """Return current download queue and unreleased monitored items."""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    data = _build_activity(db)
    _cache["data"] = data
    _cache["ts"] = now
    return data
