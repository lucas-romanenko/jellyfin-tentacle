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


def _library_ids(db: Session, media_type: str, tmdb_ids: list) -> dict:
    """{tmdb id: [Jellyfin item ids]} of the ones Tentacle knows in the
    library, so the plugin checks them in one query as the caller instead
    of one lookup per id."""
    from models.database import Movie, Series
    model = Movie if media_type == "movie" else Series
    wanted = sorted({int(i) for i in tmdb_ids if isinstance(i, int) and i > 0})
    out = {}
    for start in range(0, len(wanted), 500):
        rows = db.query(model.tmdb_id, model.jellyfin_item_id).filter(
            model.tmdb_id.in_(wanted[start:start + 500]), model.jellyfin_item_id.isnot(None)).all()
        for tmdb_id, item_id in rows:
            item_id = (item_id or "").replace("-", "").lower()
            if item_id and item_id not in out.setdefault(str(tmdb_id), []):
                out[str(tmdb_id)].append(item_id)
    return out
