"""
Tentacle - Providers Router
Handles provider management, testing, category browsing, and preview
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from pathlib import Path
import requests
import re
import shutil
import logging

logger = logging.getLogger(__name__)

from models.database import (
    get_db, Provider, ProviderCategory, CategorySnapshot, Setting,
    Movie, Series, Duplicate, SyncRun, LiveChannel, LiveChannelGroup, EPGProgram,
)
from routers.auth import require_admin

router = APIRouter(prefix="/api/providers", tags=["providers"], dependencies=[Depends(require_admin)])

HEADERS = {"User-Agent": "TiviMate/4.7.0 (Linux; Android 12)"}

FOREIGN_PREFIXES = {
    'AF', 'AL', 'AR', 'BE', 'BG', 'BN', 'BR', 'CN', 'CZ', 'DE', 'DK',
    'ES', 'EX', 'FI', 'FR', 'GR', 'HU', 'IL', 'IN', 'IR', 'IT', 'JP',
    'KR', 'KU', 'LA', 'MT', 'NL', 'NO', 'PH', 'PK', 'PL', 'PT', 'QC',
    'RO', 'RU', 'SE', 'SO', 'TH', 'TR', 'UA', 'VN'
}

# Auto-detect source tag from category name
SOURCE_TAG_MAP = {
    "NETFLIX": "Netflix",
    "AMAZON": "Amazon Prime",
    "APPLE+": "Apple TV+",
    "DISNEY+": "Disney+",
    "HBO": "HBO",
    "HULU": "Hulu",
    "PARAMOUNT+": "Paramount+",
    "PARAMOUNT PICTURES": "Paramount",
    "PEACOCK": "Peacock",
    "SHOWTIME": "Showtime",
    "DISCOVERY+": "Discovery+",
    "MARVEL": "Marvel",
    "JAMES BOND": "James Bond",
    "DREAMWORKS": "DreamWorks",
    "PIXAR": "Pixar",
    "UNIVERSAL": "Universal",
    "NICKELODEON": "Nickelodeon",
    "IMDB TOP": "IMDb Top 250",
}


class ProviderCreate(BaseModel):
    name: str
    server_url: str
    username: str
    password: str
    priority: Optional[int] = 1
    require_tmdb_match: Optional[bool] = True


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    server_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    priority: Optional[int] = None
    active: Optional[bool] = None
    require_tmdb_match: Optional[bool] = None


class CategoryUpdate(BaseModel):
    category_ids: List[str]
    whitelisted: bool


def has_foreign_prefix(name: str) -> bool:
    match = re.match(r'^([A-Z]{2})(?:/[A-Z]{2})?\s*-\s*', name.upper())
    if match:
        return match.group(1) in FOREIGN_PREFIXES
    return False


def detect_source_tag(category_name: str) -> Optional[str]:
    upper = category_name.upper()
    for keyword, tag in SOURCE_TAG_MAP.items():
        if keyword in upper:
            return tag
    return None


COUNTRY_PREFIXES = [
    'GERMANY', 'GERMAN', 'FRANCE', 'FRENCH', 'NORDIC', 'NORWAY', 'NORWEGIAN',
    'SWEDEN', 'SWEDISH', 'DENMARK', 'DANISH', 'FINLAND', 'FINNISH',
    'TURKEY', 'TURKISH', 'TURKSIH', 'PT/BR', 'PORTUGAL', 'PORTUGUESE', 'BRAZIL',
    'NETHERLANDS', 'DUTCH', 'BELGIUM', 'ITALY', 'ITALIAN', 'SPAIN', 'SPANISH',
    'GREECE', 'GREEK', 'INDIA', 'HINDI', 'PERSIAN', 'IRAN', 'PAKISTAN',
    'AFRICA', 'SOUTH AFRICA', 'MALTA', 'ROMANIA', 'RUSSIA', 'RUSSIAN', 'RUSSAIN', 'POLAND', 'POLISH',
    'CZECH', 'HUNGARY', 'HUNGARIAN', 'UKRAINE', 'UKRAINIAN', 'ARABIC',
    'ARAB', 'ISRAEL', 'HEBREW', 'JAPAN', 'JAPANESE', 'KOREA', 'KOREAN',
    'CHINA', 'CHINESE', 'THAILAND', 'THAI', 'VIETNAM', 'PHILIPPINES',
    'INDONESIA', 'MALAYSIA', 'PHILIPPINES', 'ASIA', 'LATIN', 'MEXICO',
    'MEXICAN', 'COLOMBIA', 'ARGENTINA', 'CHILE', 'VENEZUELA', 'PERU',
    'SCANDINAVIA', 'SCANDINAVIAN', 'BALKANS', 'VIAPLAY', 'VIDEOLAND',
    'CRUNCHYROLL', 'CHRISTIAN', 'AFRICA',
]

def is_likely_english(name: str) -> bool:
    if has_foreign_prefix(name):
        return False
    if not all(ord(c) < 128 for c in name):
        return False
    upper = name.upper()
    # Exclude anything starting with a country/region name
    for prefix in COUNTRY_PREFIXES:
        if upper.startswith(prefix):
            return False
    english_signals = [
        "EN -", "EN-", "NETFLIX", "AMAZON", "APPLE+", "DISNEY+", "HBO",
        "MOVIES", "SERIES", "COMEDY", "ACTION", "HORROR", "THRILLER",
        "DRAMA", "DOCUMENTARY", "MARVEL", "PARAMOUNT", "UNIVERSAL",
        "IMDB", "4K", "BLURAY", "NEW RELEASE", "DISCOVERY", "PEACOCK",
        "HULU", "SHOWTIME", "MAX ", "HBO MAX",
    ]
    return any(s in upper for s in english_signals)


def fetch_provider_categories(provider: Provider):
    """Fetch all categories and stream counts from Xtream API"""
    base = f"{provider.server_url.rstrip('/')}/player_api.php?username={provider.username}&password={provider.password}"
    session = requests.Session()
    session.headers.update(HEADERS)

    vod_cats = session.get(f"{base}&action=get_vod_categories", timeout=15).json()
    series_cats = session.get(f"{base}&action=get_series_categories", timeout=15).json()

    # Fetch all streams to count per category
    vod_counts = {}
    series_counts = {}
    try:
        vod_streams = session.get(f"{base}&action=get_vod_streams", timeout=60).json()
        if isinstance(vod_streams, list):
            for s in vod_streams:
                cid = str(s.get("category_id", ""))
                vod_counts[cid] = vod_counts.get(cid, 0) + 1
    except Exception:
        pass
    try:
        all_series = session.get(f"{base}&action=get_series", timeout=60).json()
        if isinstance(all_series, list):
            for s in all_series:
                cid = str(s.get("category_id", ""))
                series_counts[cid] = series_counts.get(cid, 0) + 1
    except Exception:
        pass

    return vod_cats, series_cats, vod_counts, series_counts


def test_provider_connection(provider: Provider):
    """Test connection and get account info"""
    url = f"{provider.server_url.rstrip('/')}/player_api.php?username={provider.username}&password={provider.password}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("user_info", {}).get("auth") == 0:
        raise Exception("Authentication failed")
    return data


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
def list_providers(db: Session = Depends(get_db)):
    from models.database import SyncRun
    providers = db.query(Provider).order_by(Provider.priority).all()

    # Get last completed sync per provider
    last_syncs = {}
    for p in providers:
        last_run = db.query(SyncRun).filter(
            SyncRun.provider_id == p.id,
            SyncRun.status == "completed"
        ).order_by(SyncRun.completed_at.desc()).first()
        if last_run and last_run.completed_at:
            last_syncs[p.id] = last_run.completed_at.isoformat()

    return [
        {
            "id": p.id,
            "name": p.name,
            "server_url": p.server_url,
            "username": p.username,
            "active": p.active,
            "priority": p.priority,
            "status": p.status,
            "last_tested": p.last_tested,
            "last_synced": last_syncs.get(p.id),
            "expiry": p.expiry,
            "max_connections": p.max_connections,
            "created_at": p.created_at,
            "has_live": p.has_live,
            "has_vod": p.has_vod,
            "has_series": p.has_series,
            "live_tv_enabled": p.live_tv_enabled,
            "require_tmdb_match": p.require_tmdb_match if p.require_tmdb_match is not None else True,
        }
        for p in providers
    ]


@router.post("")
def create_provider(body: ProviderCreate, db: Session = Depends(get_db)):
    provider = Provider(
        name=body.name,
        server_url=body.server_url,
        username=body.username,
        password=body.password,
        priority=body.priority,
        require_tmdb_match=body.require_tmdb_match,
        active=True,
        status="untested",
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return {"id": provider.id, "success": True}


@router.put("/{provider_id}")
def update_provider(provider_id: int, body: ProviderUpdate, db: Session = Depends(get_db)):
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")
    if body.name is not None:
        p.name = body.name
    if body.server_url is not None:
        p.server_url = body.server_url
        p.status = "untested"
    if body.username is not None:
        p.username = body.username
    if body.password is not None:
        p.password = body.password
    if body.priority is not None:
        p.priority = body.priority
    if body.active is not None:
        p.active = body.active
    if body.require_tmdb_match is not None:
        p.require_tmdb_match = body.require_tmdb_match
    db.commit()
    return {"success": True}


@router.delete("/{provider_id}")
def delete_provider(provider_id: int, db: Session = Depends(get_db)):
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")

    # ── Delete VOD files from disk ──────────────────────────────────────────
    deleted_files = 0

    # Movies: strm_path points to .strm file, delete file + .nfo + parent folder
    movies = db.query(Movie).filter(Movie.provider_id == provider_id).all()
    for m in movies:
        if m.strm_path:
            try:
                strm = Path(m.strm_path)
                if strm.exists() and strm.suffix == ".strm":
                    strm.unlink()
                    deleted_files += 1
                    nfo = strm.with_suffix(".nfo")
                    if nfo.exists():
                        nfo.unlink()
                        deleted_files += 1
                    # Remove parent folder if empty
                    if strm.parent.exists() and not any(strm.parent.iterdir()):
                        strm.parent.rmdir()
            except Exception as e:
                logger.warning(f"Failed to delete movie files at {m.strm_path}: {e}")

    # Series: strm_path points to show directory, delete entire directory tree
    series = db.query(Series).filter(Series.provider_id == provider_id).all()
    for s in series:
        if s.strm_path:
            try:
                show_dir = Path(s.strm_path)
                if show_dir.exists() and show_dir.is_dir():
                    file_count = sum(1 for _ in show_dir.rglob("*") if _.is_file())
                    shutil.rmtree(show_dir)
                    deleted_files += file_count
            except Exception as e:
                logger.warning(f"Failed to delete series files at {s.strm_path}: {e}")

    logger.info(f"Deleted {deleted_files} VOD files from disk for provider {p.name}")

    # ── Cascade-delete all DB records ────────────────────────────────────────
    # Remove duplicates that reference movies from this provider
    provider_tmdb_ids = [m.tmdb_id for m in movies]
    if provider_tmdb_ids:
        db.query(Duplicate).filter(Duplicate.tmdb_id.in_(provider_tmdb_ids)).delete(synchronize_session=False)

    deleted_movies = len(movies)
    deleted_series = len(series)
    db.query(Movie).filter(Movie.provider_id == provider_id).delete()
    db.query(Series).filter(Series.provider_id == provider_id).delete()
    # Delete category snapshots via category IDs, then categories
    cat_ids = [c.id for c in db.query(ProviderCategory.id).filter(ProviderCategory.provider_id == provider_id).all()]
    if cat_ids:
        db.query(CategorySnapshot).filter(CategorySnapshot.category_id.in_(cat_ids)).delete(synchronize_session=False)
    db.query(ProviderCategory).filter(ProviderCategory.provider_id == provider_id).delete()
    db.query(SyncRun).filter(SyncRun.provider_id == provider_id).delete()
    # Delete EPG programs for channels belonging to this provider, then channels/groups
    channel_epg_ids = [c.epg_channel_id for c in db.query(LiveChannel.epg_channel_id).filter(
        LiveChannel.provider_id == provider_id, LiveChannel.epg_channel_id.isnot(None)
    ).all()]
    if channel_epg_ids:
        db.query(EPGProgram).filter(EPGProgram.channel_id.in_(channel_epg_ids)).delete(synchronize_session=False)
    db.query(LiveChannel).filter(LiveChannel.provider_id == provider_id).delete()
    db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider_id).delete()

    provider_name = p.name
    db.delete(p)
    db.commit()

    # ── Rebuild playlists and home config ────────────────────────────────────
    # Source-tag playlists (e.g. "Netflix Movies") may now be orphaned if all
    # content came from this provider. sync_smartlists removes orphaned playlists
    # from Jellyfin and disk, refresh repopulates remaining ones, and
    # write_home_config drops stale rows/hero references.
    try:
        from services.smartlists import sync_smartlists, refresh_smartlist_playlists, write_home_config
        from models.database import TentacleUser as _TU
        logger.info(f"Rebuilding playlists after deleting provider {provider_name}...")
        sync_smartlists(db)  # loops all users
        refresh_smartlist_playlists(db)  # loops all users
        for _u in db.query(_TU).all():
            write_home_config(db, user_id=_u.id)
        logger.info(f"Playlist rebuild complete after provider deletion")
    except Exception as e:
        logger.warning(f"Playlist rebuild after provider delete failed (non-fatal): {e}")

    return {
        "success": True,
        "deleted_movies": deleted_movies,
        "deleted_series": deleted_series,
        "deleted_files": deleted_files,
    }


@router.post("/{provider_id}/test")
def test_provider(provider_id: int, db: Session = Depends(get_db)):
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")
    try:
        data = test_provider_connection(p)
        info = data.get("user_info", {})
        exp_ts = info.get("exp_date")
        p.status = "ok"
        p.last_tested = datetime.utcnow()
        if exp_ts:
            p.expiry = datetime.fromtimestamp(int(exp_ts))
        p.max_connections = int(info.get("max_connections", 1))

        # Probe capabilities
        base = f"{p.server_url.rstrip('/')}/player_api.php?username={p.username}&password={p.password}"
        session = requests.Session()
        session.headers.update(HEADERS)
        for attr, action in [("has_vod", "get_vod_categories"), ("has_series", "get_series_categories"), ("has_live", "get_live_categories")]:
            try:
                cats = session.get(f"{base}&action={action}", timeout=10).json()
                setattr(p, attr, bool(cats and len(cats) > 0))
            except Exception:
                setattr(p, attr, False)

        db.commit()
        return {
            "success": True,
            "status": info.get("status"),
            "expiry": p.expiry,
            "max_connections": p.max_connections,
            "active_connections": info.get("active_cons"),
            "has_live": p.has_live,
            "has_vod": p.has_vod,
            "has_series": p.has_series,
        }
    except Exception as e:
        p.status = "error"
        p.last_tested = datetime.utcnow()
        db.commit()
        raise HTTPException(400, str(e))


@router.post("/{provider_id}/fetch-categories")
def fetch_categories(provider_id: int, db: Session = Depends(get_db)):
    """Fetch categories from provider and store in DB"""
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")

    try:
        vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(p)
    except Exception as e:
        raise HTTPException(400, f"Failed to fetch categories: {str(e)}")

    # Get existing categories for this provider
    existing = {
        (c.category_id, c.type): c
        for c in db.query(ProviderCategory).filter(
            ProviderCategory.provider_id == provider_id
        ).all()
    }

    new_count = 0
    for cat_type, cats, counts in [("movie", vod_cats, vod_counts), ("series", series_cats, series_counts)]:
        for cat in cats:
            cid = str(cat["category_id"])
            cname = cat["category_name"]
            key = (cid, cat_type)
            count = counts.get(cid, 0)

            if key in existing:
                existing[key].category_name = cname
                existing[key].title_count = count
                existing[key].last_seen = datetime.utcnow()
            else:
                new_cat = ProviderCategory(
                    provider_id=provider_id,
                    category_id=cid,
                    category_name=cname,
                    type=cat_type,
                    whitelisted=False,
                    source_tag=detect_source_tag(cname),
                    title_count=count,
                    last_seen=datetime.utcnow(),
                )
                db.add(new_cat)
                new_count += 1

    db.commit()
    return {"success": True, "new_categories": new_count}


@router.get("/{provider_id}/categories")
def get_categories(
    provider_id: int,
    type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get stored categories for a provider"""
    query = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id
    )
    if type:
        query = query.filter(ProviderCategory.type == type)

    cats = query.order_by(ProviderCategory.category_name).all()

    return [
        {
            "id": c.id,
            "category_id": c.category_id,
            "name": c.category_name,
            "type": c.type,
            "whitelisted": c.whitelisted,
            "source_tag": c.source_tag,
            "title_count": c.title_count,
            "last_sync_matched": c.last_sync_matched,
            "last_sync_skipped": c.last_sync_skipped,
            "is_foreign": has_foreign_prefix(c.category_name),
            "is_likely_english": is_likely_english(c.category_name),
        }
        for c in cats
    ]


@router.post("/{provider_id}/categories/update")
def update_categories(
    provider_id: int,
    body: CategoryUpdate,
    db: Session = Depends(get_db)
):
    """Bulk update category whitelist status"""
    cats = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id,
        ProviderCategory.id.in_(body.category_ids)
    ).all()

    for cat in cats:
        cat.whitelisted = body.whitelisted

    db.commit()
    return {"success": True, "updated": len(cats)}


@router.post("/{provider_id}/categories/{category_id}/tag")
def update_category_tag(
    provider_id: int,
    category_id: int,
    tag: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Update source tag for a category"""
    cat = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id,
        ProviderCategory.id == category_id
    ).first()
    if not cat:
        raise HTTPException(404, "Category not found")
    cat.source_tag = tag
    db.commit()
    return {"success": True}


@router.get("/{provider_id}/preview")
def preview_sync(provider_id: int, db: Session = Depends(get_db)):
    """
    Preview what a sync would bring in.
    Returns stats without actually syncing.
    """
    p = db.query(Provider).filter(Provider.id == provider_id).first()
    if not p:
        raise HTTPException(404, "Provider not found")

    whitelisted = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == provider_id,
        ProviderCategory.whitelisted == True
    ).all()

    if not whitelisted:
        return {
            "whitelisted_categories": 0,
            "estimated_movies": 0,
            "estimated_series": 0,
            "message": "No categories whitelisted yet"
        }

    # Get stream counts per category
    base = f"{p.server_url.rstrip('/')}/player_api.php?username={p.username}&password={p.password}"
    session = requests.Session()
    session.headers.update(HEADERS)

    movie_cats = [c for c in whitelisted if c.type == "movie"]
    series_cats = [c for c in whitelisted if c.type == "series"]

    # Sample first few categories to estimate
    sample_size = min(5, len(movie_cats))
    movie_count = 0
    sample_categories = []

    for cat in movie_cats[:sample_size]:
        try:
            r = session.get(
                f"{base}&action=get_vod_streams&category_id={cat.category_id}",
                timeout=15
            )
            count = len(r.json())
            movie_count += count
            sample_categories.append({
                "name": cat.category_name,
                "count": count,
                "tag": cat.source_tag
            })
        except:
            pass

    # Extrapolate if we only sampled
    if len(movie_cats) > sample_size:
        avg = movie_count / sample_size if sample_size else 0
        estimated_movies = int(avg * len(movie_cats))
    else:
        estimated_movies = movie_count

    return {
        "whitelisted_categories": len(whitelisted),
        "movie_categories": len(movie_cats),
        "series_categories": len(series_cats),
        "estimated_movies": estimated_movies,
        "sample_categories": sample_categories,
        "note": "Estimates based on category sampling. Actual count after TMDB matching may differ."
    }
