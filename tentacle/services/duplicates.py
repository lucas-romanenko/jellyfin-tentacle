"""
Tentacle - Duplicate resolution helpers

Shared by the duplicates router (user-initiated resolution) and the VOD sync
engine (continuous enforcement of past resolutions).

Key invariant: tmdb_id is UNIQUE on movies/series — a title has exactly ONE
DB row, whichever source owns it. Resolving a duplicate as "keep downloaded"
must therefore CONVERT the row to a downloaded-only row, never delete it:
with no row in the DB, the nightly VOD sync sees the provider still offers
the title and re-imports it as brand new (fresh .strm + "Recently Added"),
silently undoing the user's resolution.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def delete_vod_files(strm_path: str):
    """Delete a VOD movie's .strm and its own .nfo, plus the folder if emptied.

    Delegates to media_files.delete_movie_files, whose merged-folder guard
    (#28) this copy never had: in a folder where Radarr's "Heat (1995).mkv"
    sits beside the provider's "Heat (1995).strm", the .nfo of that name is
    Radarr's and describes the download the user chose to keep (#166)."""
    from services.media_files import delete_movie_files
    delete_movie_files(strm_path)


def convert_record_to_downloaded(record, media_type: str):
    """Turn a provider-owned Movie/Series row into a downloaded-only row in place.

    After conversion the VOD sync skips the title (source is radarr/sonarr) and
    the Radarr/Sonarr scan sees an existing record — nothing shows up as "new".
    """
    old_source_tag = record.source_tag

    record.source = "radarr" if media_type == "movie" else "sonarr"
    record.provider_id = None
    record.strm_path = None
    record.nfo_path = None
    record.source_tag = None
    # Pointed at the VOD Jellyfin item (now deleted) — let the pipeline re-match
    record.jellyfin_item_id = None

    # Drop provider tags (e.g. "Netflix Movies"); keep list/recently-added tags
    tags = [
        t for t in (record.tags or [])
        if not (old_source_tag and t.startswith(old_source_tag))
    ]
    if media_type == "movie" and "Downloaded Movies" not in tags:
        tags.append("Downloaded Movies")
    record.tags = tags

    logger.info(f"Converted tmdb:{record.tmdb_id} to downloaded-only record ({record.source})")
