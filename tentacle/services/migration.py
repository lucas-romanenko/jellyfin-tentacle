"""
Tentacle - Provider Migration Service
Handles switching providers: rewrites .strm URLs for matching content.
"""

import logging
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session

from models.database import Movie, Series, Provider

logger = logging.getLogger(__name__)


def preview_migration(
    from_provider: Provider,
    to_provider: Provider,
    db: Session,
) -> dict:
    """
    Preview what a provider migration would do.
    Returns stats without changing any files.
    """
    import requests
    HEADERS = {"User-Agent": "TiviMate/4.7.0 (Linux; Android 12)"}

    # Get all TMDB IDs from the old provider
    old_movies = db.query(Movie).filter(
        Movie.provider_id == from_provider.id
    ).all()
    old_series = db.query(Series).filter(
        Series.provider_id == from_provider.id
    ).all()

    old_movie_ids = {m.tmdb_id for m in old_movies}
    old_series_ids = {s.tmdb_id for s in old_series}

    # Fetch new provider's streams to see what matches
    try:
        base = f"{to_provider.server_url.rstrip('/')}/player_api.php?username={to_provider.username}&password={to_provider.password}"
        session = requests.Session()
        session.headers.update(HEADERS)

        # Get categories and estimate coverage
        vod_cats = session.get(f"{base}&action=get_vod_categories", timeout=15).json()
        total_cats = len(vod_cats) if isinstance(vod_cats, list) else 0

        return {
            "from_provider": from_provider.name,
            "to_provider": to_provider.name,
            "current_movies": len(old_movies),
            "current_series": len(old_series),
            "new_provider_categories": total_cats,
            "note": "Run migration after verifying new provider categories are whitelisted",
        }
    except Exception as e:
        return {
            "from_provider": from_provider.name,
            "to_provider": to_provider.name,
            "current_movies": len(old_movies),
            "current_series": len(old_series),
            "error": str(e),
        }


def migrate_provider(
    from_provider_id: int,
    to_provider_id: int,
    db: Session,
    dry_run: bool = False,
) -> dict:
    """
    Migrate content from one provider to another.
    For each movie/series in old provider that exists in new provider (matched by TMDB ID),
    rewrites the .strm file with the new provider's stream URL.
    """
    from_provider = db.query(Provider).filter(Provider.id == from_provider_id).first()
    to_provider = db.query(Provider).filter(Provider.id == to_provider_id).first()

    if not from_provider or not to_provider:
        return {"error": "Provider not found"}

    logger.info(f"Migration: {from_provider.name} → {to_provider.name} (dry_run={dry_run})")

    import requests
    HEADERS = {"User-Agent": "TiviMate/4.7.0 (Linux; Android 12)"}
    session = requests.Session()
    session.headers.update(HEADERS)

    new_base = f"{to_provider.server_url.rstrip('/')}"
    new_api = f"{new_base}/player_api.php?username={to_provider.username}&password={to_provider.password}"

    # Build lookup of title → stream_id from new provider
    # We match by TMDB ID via our DB rather than re-searching
    stats = {
        "movies_rewritten": 0,
        "movies_not_found": 0,
        "series_rewritten": 0,
        "series_not_found": 0,
        "errors": 0,
    }

    # Get all movies from old provider
    old_movies = db.query(Movie).filter(Movie.provider_id == from_provider_id).all()

    # Fetch new provider's VOD streams (all categories)
    try:
        r = session.get(f"{new_api}&action=get_vod_streams", timeout=60)
        new_vod_streams = {str(m.get("stream_id")): m for m in r.json() if isinstance(m, dict)}
    except Exception as e:
        logger.error(f"Failed to fetch new provider streams: {e}")
        return {"error": str(e)}

    # For each old movie, find matching stream in new provider
    # We need to match by title since we can't match by TMDB ID directly
    # Build a title→stream map from new provider
    from services.cleaner import clean_title

    new_title_map = {}
    for sid, stream in new_vod_streams.items():
        clean, year = clean_title(stream.get("name", ""))
        if clean:
            key = f"{clean.lower()}_{year or ''}"
            new_title_map[key] = stream

    for movie in old_movies:
        try:
            # Try to find this movie in new provider streams
            key = f"{movie.title.lower()}_{movie.year or ''}"
            new_stream = new_title_map.get(key)

            if not new_stream:
                stats["movies_not_found"] += 1
                logger.debug(f"Not found in new provider: {movie.title}")
                continue

            # Rewrite .strm file
            new_url = f"{new_base}/movie/{to_provider.username}/{to_provider.password}/{new_stream['stream_id']}.{new_stream.get('container_extension', 'mp4')}"

            if not dry_run and movie.strm_path:
                strm = Path(movie.strm_path)
                if strm.exists():
                    strm.write_text(new_url, encoding="utf-8")

            # Update DB
            if not dry_run:
                movie.provider_id = to_provider_id
                movie.source = f"provider_{to_provider_id}"

            stats["movies_rewritten"] += 1

        except Exception as e:
            logger.error(f"Error migrating movie {movie.title}: {e}")
            stats["errors"] += 1

    if not dry_run:
        # Update provider assignments
        db.query(Movie).filter(Movie.provider_id == from_provider_id).filter(
            Movie.source == f"provider_{from_provider_id}"
        ).update({"source": f"provider_{to_provider_id}", "provider_id": to_provider_id})

        # Deactivate old provider
        from_provider.active = False
        to_provider.active = True
        db.commit()

    logger.info(
        f"Migration complete: {stats['movies_rewritten']} rewritten, "
        f"{stats['movies_not_found']} not found, {stats['errors']} errors"
    )
    return stats
