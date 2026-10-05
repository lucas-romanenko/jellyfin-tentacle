"""
Tentacle - Provider Migration Service
Handles switching providers: rewrites .strm URLs for the films the new
provider lists; everything else stays with the old provider.
"""

import logging
from pathlib import Path
from sqlalchemy.orm import Session

from models.database import Movie, Series, Provider, ProviderCategory

logger = logging.getLogger(__name__)


def preview_migration(
    from_provider: Provider,
    to_provider: Provider,
    db: Session,
) -> dict:
    """
    Preview what a provider migration would do: a dry run of
    migrate_provider(), so the counts are the ones Migrate would give.
    Returns stats without changing any files.
    """
    preview = {
        "from_provider": from_provider.name,
        "to_provider": to_provider.name,
        "current_movies": db.query(Movie).filter(Movie.provider_id == from_provider.id).count(),
        "current_series": db.query(Series).filter(Series.provider_id == from_provider.id).count(),
    }
    return {**preview, **migrate_provider(from_provider.id, to_provider.id, db, dry_run=True)}


def migrate_provider(
    from_provider_id: int,
    to_provider_id: int,
    db: Session,
    dry_run: bool = False,
) -> dict:
    """
    Move the old provider's films onto the new provider's streams.

    A film moves only where the new provider's sync would find it on the
    same stream: listed in one of the new provider's chosen (whitelisted)
    movie categories under the film's title and year, not on a stream an
    admin blocked or re-matched to another film, and not under a title and
    year two films in the library share. Its .strm is rewritten and its row
    changes owner. Everything else (films the new provider doesn't list,
    and every series) stays with the old provider, files untouched: handed
    to the new provider, its sync never lists them and deletes them (#460).
    """
    from_provider = db.query(Provider).filter(Provider.id == from_provider_id).first()
    to_provider = db.query(Provider).filter(Provider.id == to_provider_id).first()

    if not from_provider or not to_provider:
        return {"error": "Provider not found"}
    if from_provider.id == to_provider.id:
        return {"error": "Select different providers"}

    from services.cleaner import clean_title
    from services.sync import make_provider_client, vod_links_for, _write_strm, chown_path
    from services.wrong_match import blocked_keys, is_blocked, override_keys, override_for

    # What the new provider's sync reads: only its chosen movie categories
    cats = db.query(ProviderCategory).filter(
        ProviderCategory.provider_id == to_provider.id,
        ProviderCategory.type == "movie",
        ProviderCategory.whitelisted == True,  # noqa: E712
    ).all()
    if not cats:
        return {"error": f"Choose {to_provider.name}'s movie categories first: "
                         f"only the films it lists there can move to it"}

    logger.info(f"Migration: {from_provider.name} → {to_provider.name} (dry_run={dry_run})")

    client = make_provider_client(to_provider)
    client.vod_links = vod_links_for(db, to_provider)   # the URLs its sync writes
    # The streams its sync skips (blocked) or files under another film (overrides)
    blocked = blocked_keys(db, to_provider.id, "movie")
    overrides = override_keys(db, to_provider.id, "movie")

    # title_year -> URL of the first stream listing it, in category order as
    # the sync meets them; tmdb_id -> URL of a stream re-matched to that film.
    by_title = {}
    by_override = {}
    for cat in cats:
        try:
            streams = client.get_vod_streams(cat.category_id)
        except Exception as e:
            logger.error(f"Migration: could not read {to_provider.name}'s category {cat.category_name}: {e}")
            return {"error": f"Could not read {to_provider.name}'s category {cat.category_name}: {e}"}
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            sid = stream.get("stream_id")
            url = client.movie_stream_url(sid, stream.get("container_extension", "mp4"))
            if not url or (blocked and is_blocked(blocked, sid, url)):
                continue
            fixed = override_for(overrides, sid, url) if overrides else None
            if fixed is not None:
                by_override.setdefault(fixed, url)
                continue
            clean, year = clean_title(stream.get("name", ""))
            if clean:
                by_title.setdefault(f"{clean.lower()}_{year or ''}", url)

    stats = {
        "from_provider": from_provider.name,
        "to_provider": to_provider.name,
        "movies_rewritten": 0,
        "movies_not_found": 0,   # not listed in the new provider's chosen categories
        "movies_skipped": 0,     # listed, but not safely theirs: namesakes, no .strm, .strm opted out
        "series_kept": db.query(Series).filter(Series.provider_id == from_provider.id).count(),
        "errors": 0,
    }

    old_movies = db.query(Movie).filter(Movie.provider_id == from_provider.id).all()
    namesakes = {}
    for movie in old_movies:
        key = f"{(movie.title or '').lower()}_{movie.year or ''}"
        namesakes[key] = namesakes.get(key, 0) + 1

    for movie in old_movies:
        try:
            key = f"{(movie.title or '').lower()}_{movie.year or ''}"
            new_url = by_override.get(movie.tmdb_id)
            if new_url is None:
                new_url = by_title.get(key)
                if new_url is not None and namesakes[key] > 1:
                    # One stream, two films: the sync gives the name to one
                    # of them and deletes the other.
                    stats["movies_skipped"] += 1
                    logger.info(f"Migration: {movie.title} ({movie.year}) stays with {from_provider.name}: "
                                f"another film has the same title and year")
                    continue
            if new_url is None:
                stats["movies_not_found"] += 1
                logger.debug(f"Not found in new provider: {movie.title}")
                continue
            strm = Path(movie.strm_path) if movie.strm_path else None
            if movie.strm_disabled or strm is None or not strm.is_file():
                stats["movies_skipped"] += 1
                continue

            if not dry_run:
                _write_strm(strm, new_url)
                chown_path(strm)
                movie.provider_id = to_provider.id
                if movie.source == f"provider_{from_provider.id}":
                    movie.source = f"provider_{to_provider.id}"
                # The old provider's "no longer listed" mark isn't the new one's
                movie.provider_missing_since = None
            stats["movies_rewritten"] += 1

        except Exception as e:
            logger.error(f"Error migrating movie {movie.title}: {e}")
            stats["errors"] += 1

    if not dry_run:
        # Deactivate old provider
        from_provider.active = False
        to_provider.active = True
        db.commit()

    logger.info(
        f"Migration complete: {stats['movies_rewritten']} rewritten, "
        f"{stats['movies_not_found']} not found, {stats['movies_skipped']} skipped, "
        f"{stats['series_kept']} series kept, {stats['errors']} errors"
    )
    return stats
