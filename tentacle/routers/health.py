"""
Tentacle - Health Router
Library housekeeping: deletion audit log, download health, missing content,
stream health. Admin-only.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_db, get_setting, set_setting, DeletionLog
from routers.auth import require_admin
from services.download_health import _arr_conn, _arr_get, _arr_post

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/health", tags=["health"], dependencies=[Depends(require_admin)])


# ─── Download health ──────────────────────────────────────────────────────────

class DownloadActionRequest(BaseModel):
    source: str  # radarr | sonarr
    queue_id: int
    delete_file: bool = True


class DownloadImportRequest(BaseModel):
    source: str
    download_id: str
    movie_id: int | None = None
    episode_id: int | None = None


class DownloadTogglesRequest(BaseModel):
    auto_fix: bool | None = None
    auto_import: bool | None = None


def _validate_source(source: str):
    if source not in ("radarr", "sonarr"):
        raise HTTPException(400, "source must be radarr or sonarr")


@router.post("/downloads/fix")
def fix_stuck_download(body: DownloadActionRequest, db: Session = Depends(get_db)):
    """Cancel + blocklist a stuck download and grab a replacement release."""
    _validate_source(body.source)
    from services.download_health import resolve_stuck_download
    result = resolve_stuck_download(db, body.source, body.queue_id, reason="manual")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Fix failed"))
    return result


@router.post("/downloads/remove")
def remove_queued_download(body: DownloadActionRequest, db: Session = Depends(get_db)):
    """Dequeue without blocklisting or re-grabbing."""
    _validate_source(body.source)
    from services.download_health import remove_download
    result = remove_download(db, body.source, body.queue_id, delete_file=body.delete_file, reason="manual")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Remove failed"))
    return result


@router.post("/downloads/manual-import")
def manual_import_download(body: DownloadImportRequest, db: Session = Depends(get_db)):
    """Force-import a blocked download when the arr's own lookup is unambiguous."""
    _validate_source(body.source)
    from services.download_health import attempt_manual_import
    try:
        result = attempt_manual_import(db, body.source, body.download_id,
                                       movie_id=body.movie_id, episode_id=body.episode_id)
    except Exception as e:
        raise HTTPException(502, f"Manual import failed: {e}")
    return result


@router.get("/downloads/settings")
def get_download_settings(db: Session = Depends(get_db)):
    return {
        "auto_fix": get_setting(db, "downloads_auto_fix_enabled", "false") == "true",
        "auto_import": get_setting(db, "downloads_auto_import_enabled", "false") == "true",
    }


@router.post("/downloads/settings")
def save_download_settings(body: DownloadTogglesRequest, db: Session = Depends(get_db)):
    if body.auto_fix is not None:
        set_setting(db, "downloads_auto_fix_enabled", "true" if body.auto_fix else "false")
    if body.auto_import is not None:
        set_setting(db, "downloads_auto_import_enabled", "true" if body.auto_import else "false")
    return {"success": True}


# ─── Missing content ──────────────────────────────────────────────────────────

class GrabReleaseRequest(BaseModel):
    kind: str  # movie | episode
    guid: str
    indexer_id: int


class MissingSearchRequest(BaseModel):
    kind: str  # movie | episode
    id: int


def _validate_kind(kind: str):
    if kind not in ("movie", "episode"):
        raise HTTPException(400, "kind must be movie or episode")


def _parse_arr_date(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _movie_release_info(m: dict) -> bool:
    """True when the movie is actually acquirable — Radarr says released, or a
    digital/physical date has passed. A cinema-only release isn't missing (no
    real download normally exists yet), it's upcoming."""
    if m.get("status") == "released":
        return True
    now = datetime.utcnow()
    acquirable = [d for d in (_parse_arr_date(m.get("digitalRelease")),
                              _parse_arr_date(m.get("physicalRelease"))) if d]
    return bool(acquirable) and min(acquirable) <= now


def _extract_arr_poster(item: dict):
    for img in item.get("images", []) or []:
        if img.get("coverType") == "poster":
            return img.get("remoteUrl") or img.get("url")
    return None


@router.get("/missing/movies")
def missing_movies(db: Session = Depends(get_db)):
    """Monitored Radarr movies with no file that are actually released."""
    url, key = _arr_conn(db, "radarr")
    if not url or not key:
        return []
    movies = _arr_get(url, key, "movie")
    out = []
    for m in movies:
        if not m.get("monitored") or m.get("hasFile"):
            continue
        if not _movie_release_info(m):
            continue
        out.append({
            "id": m["id"],
            "tmdb_id": m.get("tmdbId"),
            "title": m.get("title", ""),
            "year": m.get("year"),
            "poster_url": _extract_arr_poster(m),
        })
    out.sort(key=lambda x: (x["title"] or "").lower())
    return out


@router.get("/missing/episodes")
def missing_episodes(limit: int = 300, db: Session = Depends(get_db)):
    """Missing monitored episodes from Sonarr's own wanted list (already
    excludes unaired episodes)."""
    url, key = _arr_conn(db, "sonarr")
    if not url or not key:
        return []
    limit = max(1, min(limit, 1000))
    out, page = [], 1
    page_size = min(limit, 200)
    while len(out) < limit:
        data = _arr_get(url, key, "wanted/missing", page=page, pageSize=page_size,
                        sortKey="series.sortTitle", sortDirection="ascending",
                        includeSeries="true")
        records = data.get("records", [])
        for ep in records:
            series = ep.get("series") or {}
            out.append({
                "id": ep["id"],
                "series_id": ep.get("seriesId"),
                "series": series.get("title", "?"),
                "season": ep.get("seasonNumber"),
                "episode": ep.get("episodeNumber"),
                "title": ep.get("title"),
                "air_date": ep.get("airDateUtc"),
                "poster_url": _extract_arr_poster(series),
            })
        if page * page_size >= data.get("totalRecords", 0) or not records:
            break
        page += 1
    return out[:limit]


def _summarize_releases(releases: list) -> dict:
    """Group an interactive-search result into something scannable: what came
    back, what's grabbable now, and the top rejection reasons verbatim (the
    arrs already word these accurately)."""
    candidates = []
    reasons_seen = {}
    for r in releases:
        rejections = r.get("rejections") or []
        for reason in rejections:
            reasons_seen[reason] = reasons_seen.get(reason, 0) + 1
        candidates.append({
            "title": r.get("title"),
            "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
            "size_bytes": r.get("size"),
            "seeders": r.get("seeders"),
            "protocol": r.get("protocol"),
            "indexer": r.get("indexer"),
            "rejected": bool(r.get("rejected")),
            "rejections": rejections,
            "guid": r.get("guid"),
            "indexer_id": r.get("indexerId"),
        })
    grabbable = [c for c in candidates if not c["rejected"]]
    return {
        "total_found": len(candidates),
        "grabbable_now": len(grabbable),
        "top_reasons": sorted(reasons_seen.items(), key=lambda kv: -kv[1])[:5],
        "candidates": candidates,
    }


@router.get("/missing/diagnose")
def diagnose_missing(kind: str, id: int, db: Session = Depends(get_db)):
    """Run an interactive release search and summarize why nothing was grabbed."""
    _validate_kind(kind)
    app = "radarr" if kind == "movie" else "sonarr"
    url, key = _arr_conn(db, app)
    if not url or not key:
        raise HTTPException(400, f"{app} not configured")
    try:
        if kind == "movie":
            releases = _arr_get(url, key, "release", movieId=id)
        else:
            releases = _arr_get(url, key, "release", episodeId=id)
    except Exception as e:
        raise HTTPException(502, f"Release search failed: {e}")
    return _summarize_releases(releases)


@router.post("/missing/grab")
def grab_missing_release(body: GrabReleaseRequest, db: Session = Depends(get_db)):
    """Grab a specific release from a diagnose result."""
    _validate_kind(body.kind)
    app = "radarr" if body.kind == "movie" else "sonarr"
    url, key = _arr_conn(db, app)
    if not url or not key:
        raise HTTPException(400, f"{app} not configured")
    try:
        _arr_post(url, key, "release", {"guid": body.guid, "indexerId": body.indexer_id})
    except Exception as e:
        raise HTTPException(502, f"Grab failed: {e}")
    return {"success": True}


@router.post("/missing/search")
def search_missing_item(body: MissingSearchRequest, db: Session = Depends(get_db)):
    """Trigger a normal (non-interactive) search for one movie/episode."""
    _validate_kind(body.kind)
    app = "radarr" if body.kind == "movie" else "sonarr"
    url, key = _arr_conn(db, app)
    if not url or not key:
        raise HTTPException(400, f"{app} not configured")
    try:
        if body.kind == "movie":
            _arr_post(url, key, "command", {"name": "MoviesSearch", "movieIds": [body.id]})
        else:
            _arr_post(url, key, "command", {"name": "EpisodeSearch", "episodeIds": [body.id]})
    except Exception as e:
        raise HTTPException(502, f"Search trigger failed: {e}")
    return {"success": True}


# ─── Deletion audit log ───────────────────────────────────────────────────────

@router.get("/deletions")
def get_deletions(limit: int = 200, db: Session = Depends(get_db)):
    """Recent deletion-audit entries, newest first."""
    limit = max(1, min(limit, 1000))
    entries = db.query(DeletionLog).order_by(DeletionLog.created_at.desc()).limit(limit).all()
    return [{
        "id": e.id,
        "kind": e.kind,
        "name": e.name,
        "media_type": e.media_type,
        "size_bytes": e.size_bytes,
        "reason": e.reason,
        "detail": e.detail,
        "user_name": e.user_name,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in entries]
