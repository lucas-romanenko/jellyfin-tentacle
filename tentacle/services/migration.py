"""
Tentacle - Provider Migration Service
Handles switching providers: rewrites .strm URLs for matching content.
"""

import logging
from collections import Counter
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session

from models.database import Movie, Series, Provider, ProviderCategory

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
    Each film of the old provider that the new provider lists under the same
    title and year, in a movie category the new provider syncs, gets its .strm
    rewritten to the new provider's stream URL and moves to it.

    Everything else stays with the old provider, files untouched: films the new
    provider does not list there or only on a stream an admin blocked or
    re-matched ("Wrong movie"), films sharing their name with another film in
    the library (one name can't tell which of them it lists) and all series.
    The new provider's sync prunes the films it owns but does not list (two
    syncs, then the row, .strm and .nfo are deleted), so it must never be
    handed one.
    """
    from_provider = db.query(Provider).filter(Provider.id == from_provider_id).first()
    to_provider = db.query(Provider).filter(Provider.id == to_provider_id).first()

    if not from_provider or not to_provider:
        return {"error": "Provider not found"}

    # The movie categories the new provider's sync reads. A film it lists only
    # elsewhere is pruned by that sync, the same as one it doesn't list at all.
    synced_categories = {
        str(c.category_id) for c in db.query(ProviderCategory).filter(
            ProviderCategory.provider_id == to_provider_id,
            ProviderCategory.type == "movie",
            ProviderCategory.whitelisted == True,  # noqa: E712
        ).all()
    }
    if not synced_categories:
        return {"error": f"Choose {to_provider.name}'s movie categories first: "
                         f"Migrate only moves films in the categories {to_provider.name} syncs"}

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
    from services.sync import _write_strm
    from services.wrong_match import blocked_keys, is_blocked, override_keys, override_for

    def title_key(title, year):
        return f"{title.lower()}_{year or ''}"

    # Streams an admin fixed with "Wrong movie": the new provider's sync skips a
    # blocked one and files a re-matched one under the film it really is, so a
    # film moved onto either is pruned the same way.
    blocked = blocked_keys(db, to_provider_id, "movie")
    overrides = override_keys(db, to_provider_id, "movie")

    new_title_map = {}
    for sid, stream in new_vod_streams.items():
        if str(stream.get("category_id")) not in synced_categories:
            continue
        url = f"{new_base}/movie/{to_provider.username}/{to_provider.password}/{stream.get('stream_id')}.{stream.get('container_extension', 'mp4')}"
        if is_blocked(blocked, stream.get("stream_id"), url) or \
                override_for(overrides, stream.get("stream_id"), url) is not None:
            continue
        clean, year = clean_title(stream.get("name", ""))
        if clean:
            new_title_map[title_key(clean, year)] = stream

    # Films of the same name (namesakes, any owner): the new provider's sync
    # gives a listed name to one film, so a name shared by two moves neither.
    namesakes = Counter(title_key(t, y) for t, y in db.query(Movie.title, Movie.year).all())

    for movie in old_movies:
        try:
            # Try to find this movie in new provider streams
            key = title_key(movie.title, movie.year)
            new_stream = new_title_map.get(key) if namesakes[key] == 1 else None

            if not new_stream:
                stats["movies_not_found"] += 1
                logger.debug(f"Not found in new provider: {movie.title}")
                continue

            # Rewrite .strm file
            new_url = f"{new_base}/movie/{to_provider.username}/{to_provider.password}/{new_stream['stream_id']}.{new_stream.get('container_extension', 'mp4')}"

            if not dry_run and movie.strm_path:
                strm = Path(movie.strm_path)
                if strm.exists():
                    # Through a temp file and a rename (#283): a write cut
                    # short leaves the old file whole, and the film stays.
                    _write_strm(strm, new_url)

            # Update DB
            if not dry_run:
                movie.provider_id = to_provider_id
                movie.source = f"provider_{to_provider_id}"

            stats["movies_rewritten"] += 1

        except Exception as e:
            logger.error(f"Error migrating movie {movie.title}: {e}")
            stats["errors"] += 1

    if not dry_run:
        # Only the films moved above change provider: the rest stay with the
        # old provider, files untouched. Deactivate old provider
        from_provider.active = False
        to_provider.active = True
        db.commit()

    logger.info(
        f"Migration complete: {stats['movies_rewritten']} rewritten, "
        f"{stats['movies_not_found']} not found, {stats['errors']} errors"
    )
    return stats
