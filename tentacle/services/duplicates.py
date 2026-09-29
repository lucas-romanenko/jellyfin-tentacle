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
import unicodedata
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def is_downloaded_file(path: Optional[str]) -> bool:
    """A file Radarr/Sonarr imported, not one of Tentacle's own .strm files.
    Sonarr 4 counts .strm as video: a rescan of a folder the VOD sync also
    writes to lists Tentacle's .strm files as the series' episode files."""
    return bool(path) and not path.lower().endswith(".strm")


def series_has_real_download(sonarr, series_id) -> Optional[bool]:
    """True when Sonarr holds at least one episode file that is not a .strm,
    False when it holds none, None when Sonarr could not be asked."""
    try:
        files = sonarr.get_episode_files(series_id)
    except Exception as e:
        logger.warning(f"Could not read Sonarr's episode files for series {series_id}: {e}")
        return None
    return any(is_downloaded_file(f.get("path")) for f in files or [])


def _folder_name(path: Optional[str]) -> str:
    name = (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return unicodedata.normalize("NFC", name).casefold()


def arr_folder_is_vod_folder(media_type: str, arr_path: Optional[str], record) -> bool:
    """Is the Radarr/Sonarr folder of this title also its VOD folder (the
    merged layout)? Radarr/Sonarr see the folder under their own mount, so the
    names are compared, not the paths. Errs towards True: the answer decides
    whether the arr may delete the whole folder."""
    if "/vod/" in (arr_path or "").replace("\\", "/").lower():
        return True
    strm_path = getattr(record, "strm_path", None) if record is not None else None
    if strm_path and arr_path:
        vod_folder = Path(strm_path).parent if media_type == "movie" else Path(strm_path)
        if _folder_name(str(vod_folder)) == _folder_name(arr_path):
            return True
    from routers.activity import _has_vod_folder
    return _has_vod_folder(media_type, arr_path)


def delete_vod_files(strm_path: str):
    """Delete a VOD .strm file and its companion .nfo, plus empty parent folder.

    Delegates to media_files.delete_movie_files, which keeps the NFO when a
    downloaded copy shares the .strm's stem in a merged folder ("Heat
    (1995).mkv" + "Heat (1995).nfo"): that NFO then describes the download
    the user chose to keep. This helper unlinked it unconditionally, and it is
    what "Keep Downloaded" and the sync's enforcement of it call (#28 guarded
    only the media_files path).
    """
    from services.media_files import delete_movie_files
    if delete_movie_files(strm_path):
        logger.info(f"Deleted VOD files for {strm_path}")


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
