"""More Like This (#292): what the plugin's details row is built from.

Jellyfin 10.11's /Items/{id}/Similar picks at random among everything that
shares one genre or one tag, and Tentacle's own tags ("Recently Added
Movies", "Netflix Movies") are on most of a library, so the row was random.
The plugin answers that endpoint itself (Services/SimilarItemsFilter.cs):
it maps these TMDB ids to the library as the caller (their libraries,
parental rating and blocked tags), scores them with the library's own
metadata and leaves Tentacle's tags out of the score.
"""
import logging
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from models.database import get_db, get_setting
from routers.auth import get_user_from_request
from services.exceptions import TMDBConnectionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/similar", tags=["similar"])


@router.get("/{media_type}/{tmdb_id}", dependencies=[Depends(get_user_from_request)])
def related(media_type: str, tmdb_id: int, db: Session = Depends(get_db)):
    """TMDB's related titles for a movie or show ("movie" / "tv"), ids only,
    and the tags Tentacle writes itself. `tmdb_ok` False: TMDB could not be
    asked just now (the plugin scores the library alone)."""
    if media_type not in ("movie", "tv"):
        raise HTTPException(400, "media_type is movie or tv")
    from services.tagger import tentacle_owned_tags
    from services.tmdb import TMDBService, get_tmdb_token
    rel = None
    try:
        tmdb = TMDBService(bearer_token=get_tmdb_token(db), cache_dir=get_setting(db, "data_dir", "/data"))
        tmdb.fail_fast = True
        rel = tmdb.get_related(media_type, tmdb_id)
    except TMDBConnectionError as e:
        logger.info(f"[Similar] TMDB unreachable for {media_type}/{tmdb_id}: {e}")
    except Exception as e:   # the row falls back to the library alone
        logger.warning(f"[Similar] {media_type}/{tmdb_id}: {e}")
    ok, rel = rel is not None, rel or {}
    out = {
        "tmdb_ok": ok,
        "collection": rel.get("collection") or [],
        "recommendations": rel.get("recommendations") or [],
        "similar": rel.get("similar") or [],
        "tentacle_tags": sorted(tentacle_owned_tags(db)),
    }
    out["library"] = _library_ids(db, media_type, out["collection"] + out["recommendations"] + out["similar"])
    return out


_refreshing = {"movie": False, "series": False}
_refresh_lock = threading.Lock()


def _jellyfin_map(media_type: str) -> dict:
    """Discover's {tmdb id: Jellyfin item id} of the whole library
    (routers/discover.py, kept 5 min), as it is now: a details page never
    waits for a library listing. A missing or stale map is fetched again in
    the background for the next open."""
    from routers import discover
    key = "series" if media_type == "tv" else "movie"
    current = discover._jf_ids_cache.get(key)
    stale = current is None or time.time() - discover._jf_ids_cache["ts"][key] >= discover.JF_IDS_TTL
    if stale:
        with _refresh_lock:
            start, _refreshing[key] = not _refreshing[key], True
        if start:
            def run():
                try:
                    discover._get_jellyfin_tmdb_items(key)
                except Exception as e:
                    logger.info(f"[Similar] library map for {key}: {e}")
                finally:
                    _refreshing[key] = False
            threading.Thread(target=run, name=f"similar-map-{key}", daemon=True).start()
    return current or {}


def _library_ids(db: Session, media_type: str, tmdb_ids: list) -> dict:
    """{tmdb id: [Jellyfin item ids]} of the related titles in the library
    (Jellyfin's own listing, else Tentacle's rows), so the plugin checks them
    in one query as the caller instead of one lookup per id."""
    from models.database import Movie, Series
    model = Movie if media_type == "movie" else Series
    wanted = sorted({int(i) for i in tmdb_ids if isinstance(i, int) and i > 0})
    out = {}
    jf = _jellyfin_map(media_type)
    for tmdb_id in wanted:
        item_id = (jf.get(tmdb_id) or "").replace("-", "").lower()
        if item_id:
            out[str(tmdb_id)] = [item_id]
    for start in range(0, len(wanted), 500):
        rows = db.query(model.tmdb_id, model.jellyfin_item_id).filter(
            model.tmdb_id.in_(wanted[start:start + 500]), model.jellyfin_item_id.isnot(None)).all()
        for tmdb_id, item_id in rows:
            item_id = (item_id or "").replace("-", "").lower()
            if item_id and item_id not in out.setdefault(str(tmdb_id), []):
                out[str(tmdb_id)].append(item_id)
    return out
