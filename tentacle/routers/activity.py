"""
Tentacle - Activity Router
Real-time download queue from Radarr/Sonarr, plus the two waits either side of
it: titles still searching for a release, and titles not released yet.
Queue data fetched fresh every request; the wanted lists are cached (5min) and
dropped early whenever an item leaves the queue.
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from models.database import get_db, get_setting, Movie, Series, DownloadRequest, TentacleUser
from routers.auth import get_user_from_request
from services.download_health import classify_queue_item, get_stall_state

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/activity", tags=["activity"])

# ── Separate cache for the wanted lists (expensive, rarely changes) ───────
# Holds {"unreleased": [...], "searching": [...]}.
_unreleased_cache: dict = {"data": None, "ts": 0}
UNRELEASED_TTL = 300  # 5 minutes
SEARCHING_LIMIT = 20
# Queue ids seen on the previous poll. When one disappears a download finished
# (or was removed), so the wanted lists are re-read rather than showing that
# title as "searching" again until the cache expires.
_last_queue_keys: set = set()

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
    """Monitored movies without files whose release is still ahead."""
    return _fetch_radarr_wanted(url, api_key)["unreleased"]


def _fetch_radarr_wanted(url: str, api_key: str) -> dict:
    """Monitored movies without files, split by why there is no file yet.

    "unreleased": a release date is still ahead. "searching": nothing is ahead
    and Radarr considers the movie available, so it is looking for a release —
    the gap between asking for a title and a download starting, which can be
    minutes or days and used to show nothing at all. One /movie read serves
    both, so the new list costs no extra request.
    """
    empty = {"unreleased": [], "searching": []}
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
        searching = []
        release_labels = {
            "digitalRelease": "Digital",
            "physicalRelease": "Physical",
            "inCinemas": "Theatrical",
        }
        for m in movies:
            if not m.get("monitored") or m.get("hasFile"):
                continue
            release = None
            release_type = None
            all_dates = {}
            for field in ("digitalRelease", "physicalRelease", "inCinemas"):
                val = m.get(field)
                if val:
                    try:
                        dt = datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
                        all_dates[release_labels[field]] = dt.strftime("%Y-%m-%d")
                        if dt > now and (release is None or dt < release):
                            release = dt
                            release_type = release_labels[field]
                    except (ValueError, TypeError):
                        pass
            if not release:
                # Released. Radarr only searches once the movie meets its
                # minimum availability; before that it is waiting, not looking.
                if m.get("isAvailable", True):
                    searching.append({
                        "tmdb_id": m.get("tmdbId"),
                        "title": m.get("title", ""),
                        "year": str(m.get("year", "")),
                        "overview": m.get("overview", ""),
                        "media_type": "movie",
                        "source": "radarr",
                        "status": "searching",
                        "waiting_since": _iso_date(m.get("added")),
                        "radarr_poster": _extract_poster(m),
                    })
                continue

            # Extract YouTube trailer from Radarr metadata
            trailer_url = None
            for yt in (m.get("youTubeTrailerId"),):
                if yt:
                    trailer_url = f"https://www.youtube.com/watch?v={yt}"
                    break

            unreleased.append({
                "tmdb_id": m.get("tmdbId"),
                "title": m.get("title", ""),
                "year": str(m.get("year", "")),
                "overview": m.get("overview", ""),
                "media_type": "movie",
                "source": "radarr",
                "release_date": release.strftime("%Y-%m-%d") if release else "TBA",
                "release_type": release_type or "TBA",
                "all_dates": all_dates,
                "status": "unreleased",
                "radarr_poster": _extract_poster(m),
                "trailer_url": trailer_url,
            })
        unreleased.sort(key=lambda x: x["release_date"] if x["release_date"] != "TBA" else "9999-99-99")
        return {"unreleased": unreleased, "searching": searching}
    except Exception as e:
        logger.debug(f"Radarr wanted fetch failed: {e}")
        return empty


def _parse_dt(val) -> Optional[datetime]:
    """Radarr/Sonarr timestamp -> naive UTC datetime, or None."""
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _iso_date(val) -> Optional[str]:
    dt = _parse_dt(val)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _fetch_sonarr_searching(url: str, api_key: str) -> list:
    """Monitored series with aired episodes Sonarr has not found yet.

    Sonarr's own Wanted → Missing list: monitored, aired, no file. One entry
    per series, with how many episodes are outstanding and the first of them.
    `waiting_since` is when the newest of those became wanted — the later of
    the series being added and the episode airing — so a new episode of a
    long-followed show counts from its air date, not from years ago.
    """
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/wanted/missing",
            headers={"X-Api-Key": api_key},
            params={"page": 1, "pageSize": 250, "monitored": "true",
                    "includeSeries": "true",
                    "sortKey": "episodes.airDateUtc", "sortDirection": "descending"},
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        records = body.get("records", []) if isinstance(body, dict) else (body or [])
    except Exception as e:
        logger.debug(f"Sonarr wanted/missing fetch failed: {e}")
        return []

    now = datetime.utcnow()
    by_series: dict = {}
    for ep in records:
        if ep.get("hasFile") or ep.get("monitored") is False:
            continue
        aired = _parse_dt(ep.get("airDateUtc"))
        if not aired or aired > now:
            continue
        series = ep.get("series") or {}
        if series.get("monitored") is False:
            continue
        sid = ep.get("seriesId") or series.get("id")
        if not sid:
            continue
        added = _parse_dt(series.get("added"))
        wanted_since = max(d for d in (aired, added) if d)
        entry = by_series.get(sid)
        if entry is None:
            entry = by_series[sid] = {
                "series": series, "episodes": [], "wanted_since": wanted_since,
            }
        entry["episodes"].append((ep.get("seasonNumber", 0), ep.get("episodeNumber", 0)))
        if wanted_since > entry["wanted_since"]:
            entry["wanted_since"] = wanted_since

    searching = []
    for entry in by_series.values():
        series = entry["series"]
        eps = sorted(entry["episodes"])
        first = f"S{eps[0][0]:02d}E{eps[0][1]:02d}"
        searching.append({
            "tmdb_id": series.get("tmdbId") or 0,
            "tvdb_id": series.get("tvdbId") or 0,
            "title": series.get("title", ""),
            "year": str(series.get("year", "")),
            "overview": series.get("overview", ""),
            "media_type": "series",
            "source": "sonarr",
            "status": "searching",
            "episode": first if len(eps) == 1 else f"{first} +{len(eps) - 1}",
            "missing_episodes": len(eps),
            "waiting_since": entry["wanted_since"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sonarr_poster": _extract_poster(series),
        })
    return searching


def _fetch_sonarr_unreleased(url: str, api_key: str) -> list:
    """Fetch monitored series from Sonarr that haven't aired yet."""
    try:
        r = requests.get(
            f"{url.rstrip('/')}/api/v3/series",
            headers={"X-Api-Key": api_key},
            timeout=15,
        )
        r.raise_for_status()
        all_series = r.json()
        now = datetime.utcnow()
        unreleased = []
        for s in all_series:
            if not s.get("monitored"):
                continue
            # Series with no episodes yet — check firstAired or nextAiring
            stats = s.get("statistics", {})
            if stats.get("episodeFileCount", 0) > 0:
                continue  # Already has episodes downloaded

            # Find the next air date
            release = None
            release_type = None
            next_airing = s.get("nextAiring")
            first_aired = s.get("firstAired")

            if next_airing:
                try:
                    dt = datetime.fromisoformat(next_airing.replace("Z", "+00:00")).replace(tzinfo=None)
                    if dt > now:
                        release = dt
                        release_type = "Premiere"
                except (ValueError, TypeError):
                    pass

            if not release and first_aired:
                try:
                    dt = datetime.fromisoformat(first_aired.replace("Z", "+00:00")).replace(tzinfo=None)
                    if dt > now:
                        release = dt
                        release_type = "Premiere"
                except (ValueError, TypeError):
                    pass

            if not release:
                continue

            unreleased.append({
                "tmdb_id": s.get("tmdbId") or 0,
                "tvdb_id": s.get("tvdbId") or 0,
                "title": s.get("title", ""),
                "year": str(s.get("year", "")),
                "overview": s.get("overview", ""),
                "media_type": "series",
                "source": "sonarr",
                "release_date": release.strftime("%Y-%m-%d") if release else "TBA",
                "release_type": release_type or "TBA",
                "all_dates": {release_type: release.strftime("%Y-%m-%d")} if release and release_type else {},
                "status": "unreleased",
                "sonarr_poster": _extract_poster(s),
            })
        unreleased.sort(key=lambda x: x["release_date"] if x["release_date"] != "TBA" else "9999-99-99")
        return unreleased
    except Exception as e:
        logger.debug(f"Sonarr unreleased fetch failed: {e}")
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
    """Extract poster path from Radarr/Sonarr images array.
    Returns TMDB relative path (e.g. /abc.jpg) or full TVDB URL."""
    for img in arr_item.get("images", []):
        if img.get("coverType") == "poster":
            # Try remoteUrl first (full TMDB URL like https://image.tmdb.org/t/p/original/abc.jpg)
            url = img.get("remoteUrl", "")
            if "/t/p/" in url:
                after = url.split("/t/p/")[1]  # "original/abc.jpg"
                slash_idx = after.find("/")
                if slash_idx >= 0:
                    return after[slash_idx:]  # "/abc.jpg"
            # TVDB CDN URL — return full URL (will be proxied later)
            if url and "thetvdb.com" in url:
                return url
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
    stall_state = get_stall_state(db)
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
                        cls = classify_queue_item(item, stall_state)

                        poster = _get_poster(db, tmdb_id, "movie") or _extract_poster(movie) or _fetch_tmdb_poster(tmdb_id, "movie", db)
                        downloads.append({
                            "tmdb_id": tmdb_id,
                            "title": movie.get("title", item.get("title", "")),
                            "year": str(movie.get("year", "")),
                            "poster_path": poster,
                            "media_type": "movie",
                            "source": "radarr",
                            "status": cls["status"],
                            "reason": cls["reason"],
                            "stalled_minutes": cls["stalled_minutes"],
                            "queue_id": item.get("id"),
                            "download_id": item.get("downloadId"),
                            "protocol": item.get("protocol"),
                            "indexer": item.get("indexer"),
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
                        cls = classify_queue_item(item, stall_state)

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
                            "status": cls["status"],
                            "reason": cls["reason"],
                            "stalled_minutes": cls["stalled_minutes"],
                            "queue_id": item.get("id"),
                            "download_id": item.get("downloadId"),
                            "protocol": item.get("protocol"),
                            "indexer": item.get("indexer"),
                            "progress": round(progress, 1),
                            "size_remaining": _format_size(item.get("sizeleft")),
                            "eta": _format_time(item.get("timeleft", "")),
                            "quality": item.get("quality", {}).get("quality", {}).get("name", ""),
                            "episode": ep_label,
                        })
            except Exception as e:
                logger.debug(f"Activity fetch {key} failed: {e}")

    status_order = {"stuck": 0, "import_blocked": 1, "downloading": 2, "importing": 3, "queued": 4, "warning": 5}
    downloads.sort(key=lambda d: (status_order.get(d["status"], 9), -d["progress"]))
    return downloads


def _enrich_posters(db: Session, items: list) -> None:
    """Poster from the local DB first, then Radarr/Sonarr's own image."""
    from routers.discover import _rewrite_tvdb_url
    for item in items:
        fallback_poster = item.pop("radarr_poster", None) or item.pop("sonarr_poster", None)
        poster = _get_poster(db, item.get("tmdb_id"), item.get("media_type", "movie")) or fallback_poster
        # Rewrite TVDB CDN URLs to proxy paths
        if poster:
            poster = _rewrite_tvdb_url(poster)
        item["poster_path"] = poster


def _same_title(a: dict, b_tmdb: set, b_tvdb: set) -> bool:
    return bool((a.get("tmdb_id") and a["tmdb_id"] in b_tmdb)
                or (a.get("tvdb_id") and a["tvdb_id"] in b_tvdb))


def _get_wanted(db: Session) -> dict:
    """Unreleased and still-searching titles — cached for 5 minutes (expensive calls)."""
    now = time.time()
    if _unreleased_cache["data"] is not None and (now - _unreleased_cache["ts"]) < UNRELEASED_TTL:
        return _unreleased_cache["data"]

    unreleased, searching = [], []

    radarr_url = get_setting(db, "radarr_url")
    radarr_key = get_setting(db, "radarr_api_key")
    if radarr_url and radarr_key:
        wanted = _fetch_radarr_wanted(radarr_url, radarr_key)
        unreleased.extend(wanted["unreleased"])
        searching.extend(wanted["searching"])

    sonarr_url = get_setting(db, "sonarr_url")
    sonarr_key = get_setting(db, "sonarr_api_key")
    if sonarr_url and sonarr_key:
        unreleased.extend(_fetch_sonarr_unreleased(sonarr_url, sonarr_key))
        searching.extend(_fetch_sonarr_searching(sonarr_url, sonarr_key))

    # A series with no files whose next episode is ahead used to be listed as
    # upcoming even when earlier episodes had already aired. If Sonarr is
    # looking for aired ones, "searching" is the true state; never show both.
    s_tmdb = {x["tmdb_id"] for x in searching if x.get("media_type") == "series" and x.get("tmdb_id")}
    s_tvdb = {x["tvdb_id"] for x in searching if x.get("tvdb_id")}
    unreleased = [u for u in unreleased
                  if not (u.get("media_type") == "series" and _same_title(u, s_tmdb, s_tvdb))]

    # Sort all unreleased by release date
    unreleased.sort(key=lambda x: x["release_date"] if x["release_date"] != "TBA" else "9999-99-99")
    # Newest request first; the rest of the backlog lives in Radarr/Sonarr.
    searching.sort(key=lambda x: x.get("waiting_since") or "", reverse=True)

    result = {"unreleased": unreleased[:20], "searching": searching[:SEARCHING_LIMIT]}
    _enrich_posters(db, result["unreleased"])
    _enrich_posters(db, result["searching"])
    _unreleased_cache["data"] = result
    _unreleased_cache["ts"] = now
    return result


def _get_unreleased(db: Session) -> list:
    """Get unreleased movies and series — cached for 5 minutes (expensive call)."""
    return _get_wanted(db)["unreleased"]


def invalidate_wanted_cache() -> None:
    _unreleased_cache["data"] = None
    _unreleased_cache["ts"] = 0


def _note_queue(downloads: list) -> None:
    """Re-read the wanted lists when something has left the download queue."""
    global _last_queue_keys
    keys = {(d.get("source"), d.get("queue_id")) for d in downloads if d.get("queue_id") is not None}
    if _last_queue_keys - keys:
        invalidate_wanted_cache()
    _last_queue_keys = keys


def _hours_remaining(date_added) -> int:
    """Compute hours until 24h window expires, calculated in UTC."""
    from datetime import timedelta
    import math
    if not date_added:
        return 24
    expires_at = date_added + timedelta(hours=24)
    remaining = (expires_at - datetime.utcnow()).total_seconds()
    return max(0, math.ceil(remaining / 3600))


def _get_recently_downloaded(db: Session) -> list:
    """Return items downloaded in the last 24 hours, oldest first (expiring soonest)."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=24)

    result = []

    movies = db.query(Movie).filter(
        Movie.source == "radarr",
        Movie.date_added >= cutoff,
    ).order_by(Movie.date_added.asc()).all()

    for m in movies:
        result.append({
            "tmdb_id": m.tmdb_id,
            "title": m.title,
            "year": m.year or "",
            "poster_path": m.poster_path,
            "media_type": "movie",
            "hours_remaining": _hours_remaining(m.date_added),
            "jellyfin_item_id": m.jellyfin_item_id or "",
        })

    series = db.query(Series).filter(
        Series.source == "sonarr",
        Series.date_added >= cutoff,
    ).order_by(Series.date_added.asc()).all()

    for s in series:
        result.append({
            "tmdb_id": s.tmdb_id,
            "title": s.title,
            "year": s.year or "",
            "poster_path": s.poster_path,
            "media_type": "series",
            "episode": s.last_downloaded_episode or "",
            "hours_remaining": _hours_remaining(s.date_added),
            "jellyfin_item_id": s.jellyfin_item_id or "",
        })

    result.sort(key=lambda x: x["hours_remaining"])
    return result


@router.get("")
def get_activity(request: Request, db: Session = Depends(get_db),
                 user: TentacleUser = Depends(get_user_from_request)):
    """Return current download queue (always fresh) and unreleased (5min cache).
    Admin users see all downloads with requester names. Non-admin users only see their own.

    Requires authentication (dashboard cookie or plugin-forwarded user token) — an
    anonymous caller previously received the full, unfiltered download queue."""
    downloads = _build_downloads(db)
    _note_queue(downloads)
    wanted = _get_wanted(db)
    # Copies: the lists are shared through the cache and edited per user below.
    unreleased = [dict(u) for u in wanted["unreleased"]]
    searching = [dict(x) for x in wanted["searching"]]
    recently_downloaded = _get_recently_downloaded(db)

    # Build lookup: tmdb_id -> requester display name
    all_requests = db.query(DownloadRequest, TentacleUser.display_name).join(
        TentacleUser, DownloadRequest.user_id == TentacleUser.id
    ).all()
    requester_map: dict[int, str] = {}
    user_requests: set[int] = set()
    for dr, display_name in all_requests:
        requester_map[dr.tmdb_id] = display_name
        if user and dr.user_id == user.id:
            user_requests.add(dr.tmdb_id)

    # Remove items from unreleased that are already showing in downloads (prevents duplicates)
    downloading_tmdb_ids = {d.get("tmdb_id") for d in downloads if d.get("tmdb_id")}
    downloading_tvdb_ids = {d.get("tvdb_id") for d in downloads if d.get("tvdb_id")}
    unreleased = [u for u in unreleased
                  if not _same_title(u, downloading_tmdb_ids, downloading_tvdb_ids)]
    # Same for searching: the moment a grab lands in the queue it is a download.
    # A movie Tentacle already holds a Radarr file for is found, whatever the
    # cached list says.
    searching_movie_ids = [x["tmdb_id"] for x in searching
                           if x.get("media_type") == "movie" and x.get("tmdb_id")]
    have_movie_file = {tid for (tid,) in db.query(Movie.tmdb_id).filter(
        Movie.source == "radarr", Movie.tmdb_id.in_(searching_movie_ids)).all()} if searching_movie_ids else set()
    searching = [x for x in searching
                 if not _same_title(x, downloading_tmdb_ids, downloading_tvdb_ids)
                 and not (x.get("media_type") == "movie" and x.get("tmdb_id") in have_movie_file)]

    is_admin = user and user.is_admin

    if not is_admin and user:
        # Non-admin: only show items they requested
        downloads = [d for d in downloads if d.get("tmdb_id") in user_requests]
        unreleased = [u for u in unreleased if u.get("tmdb_id") in user_requests]
        searching = [x for x in searching if x.get("tmdb_id") in user_requests]
        recently_downloaded = [r for r in recently_downloaded if r.get("tmdb_id") in user_requests]

    if is_admin:
        # Admin: attach requester name to each item
        for d in downloads:
            d["requested_by"] = requester_map.get(d.get("tmdb_id"))
        for u in unreleased:
            u["requested_by"] = requester_map.get(u.get("tmdb_id"))
        for x in searching:
            x["requested_by"] = requester_map.get(x.get("tmdb_id"))
        for r in recently_downloaded:
            r["requested_by"] = requester_map.get(r.get("tmdb_id"))

    if downloads:
        logger.info(f"Activity: {len(downloads)} download(s) in queue")
    return {"downloads": downloads, "searching": searching, "unreleased": unreleased,
            "recently_downloaded": recently_downloaded}
