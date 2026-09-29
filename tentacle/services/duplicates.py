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


# ── Users' watched state on the copy that is removed (#297) ──────────────────
#
# Jellyfin 10.11 keeps played / play count / resume point / favourite per item,
# and two items of the same film (a VOD .strm and a download in another folder)
# do not share it. Resolving a duplicate deletes one copy, and with it
# everything every user had on it. So before anything is deleted, each user's
# data on the removed copy is merged onto the kept copy: played = either, play
# count and last played = the larger, favourite = either, the resume point =
# the removed copy's when the kept one has none and isn't played. The merge is
# monotonic, so a retry (or Resolve All after a partial run) changes nothing
# twice.

class UserDataCarryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_JF_TIMEOUT = 30


def _jf_paged(jf, params: dict) -> list:
    items, start = [], 0
    while True:
        r = jf.session.get(f"{jf.url}/Items", params=dict(params, StartIndex=start, Limit=5000),
                           timeout=_JF_TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
        page = data.get("Items") or []
        items.extend(page)
        start += len(page)
        if not page or start >= (data.get("TotalRecordCount") or 0):
            return items


def _items_for_tmdb(jf, kind: str, tmdb_id: int) -> list:
    """Every Jellyfin item of this kind with this TMDB id, with Path. Unscoped,
    so the hidden half of a merged multi-version film is listed too."""
    tmdb = str(tmdb_id)
    return [i for i in _jf_paged(jf, {"IncludeItemTypes": kind, "Recursive": "true",
                                      "Fields": "ProviderIds,Path", "EnableImages": "false",
                                      "EnableUserData": "false"})
            if (i.get("ProviderIds") or {}).get("Tmdb") == tmdb]


def _episodes(jf, series_id: str) -> list:
    """The show's episodes that have a file (missing/virtual ones have no Path)."""
    return [e for e in _jf_paged(jf, {"ParentId": series_id, "Recursive": "true",
                                      "IncludeItemTypes": "Episode", "Fields": "Path",
                                      "EnableImages": "false", "EnableUserData": "false"})
            if e.get("Path")]


def _path(item: dict) -> str:
    return (item.get("Path") or "").replace("\\", "/")


def _vod_path(dup, record) -> Optional[str]:
    path = getattr(record, "strm_path", None) if record is not None else None
    return path or next((s.get("path") for s in dup.sources or []
                         if (s.get("source") or "").startswith("provider_") and s.get("path")), None)


def _copy_pairs(jf, dup, record, keep: str) -> tuple:
    """([(removed_item_id, [kept_item_id, ...])], orphans): whose user data goes
    where. `orphans` are removed-copy films with no item on the kept side (a
    download Jellyfin hasn't scanned yet)."""
    vod_path = _vod_path(dup, record)
    if not vod_path:
        return [], []
    if dup.media_type == "movie":
        parts = Path(vod_path).parts
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else None
        items = _items_for_tmdb(jf, "Movie", dup.tmdb_id)
        vod = [i["Id"] for i in items if tail and _path(i).endswith("/" + tail)]
        dl = [i["Id"] for i in items if i["Id"] not in vod and is_downloaded_file(_path(i))]
        removed, kept = (vod, dl) if keep == "download" else (dl, vod)
        if not kept:
            return [], removed
        return [(i, kept) for i in removed], []

    # A show: its show items (separate folders make two), then every episode,
    # matched by season and episode number. An episode's side is its file's:
    # a .strm is the VOD copy, anything else the download (in a merged folder
    # both are episodes of one show). Episodes only the removed copy has are
    # gone with it, as the user chose; they don't block the resolution.
    shows = _items_for_tmdb(jf, "Series", dup.tmdb_id)
    episodes = {s["Id"]: _episodes(jf, s["Id"]) for s in shows}
    # The VOD show is the one with .strm episodes (both folders can have the
    # same name under different mounts); without episodes, go by the name.
    vod_name = _folder_name(vod_path)
    vod_shows = [s["Id"] for s in shows
                 if any(not is_downloaded_file(_path(e)) for e in episodes[s["Id"]])
                 or (not episodes[s["Id"]] and _folder_name(_path(s)) == vod_name)]
    dl_shows = [s["Id"] for s in shows if s["Id"] not in vod_shows]
    removed, kept = (vod_shows, dl_shows) if keep == "download" else (dl_shows, vod_shows)
    pairs = [(i, kept) for i in removed if kept]
    removed_eps, kept_eps = {}, {}
    for show in shows:
        for ep in episodes[show["Id"]]:
            if ep.get("ParentIndexNumber") is None or ep.get("IndexNumber") is None:
                continue
            is_vod = not is_downloaded_file(_path(ep))
            side = removed_eps if is_vod == (keep == "download") else kept_eps
            side.setdefault((ep["ParentIndexNumber"], ep["IndexNumber"]), []).append(ep["Id"])
    for key, ids in removed_eps.items():
        if kept_eps.get(key):
            pairs.extend((i, kept_eps[key]) for i in ids)
    return pairs, []


def _user_data(jf, user_id: str, item_id: str) -> dict:
    r = jf.session.get(f"{jf.url}/UserItems/{item_id}/UserData", params={"userId": user_id},
                       timeout=_JF_TIMEOUT)
    if r.status_code == 404:  # not visible to this user: nothing of theirs on it
        return {}
    r.raise_for_status()
    return r.json() or {}


def _has_user_data(d: dict) -> bool:
    return bool(d.get("Played") or d.get("PlayCount") or d.get("PlaybackPositionTicks") or d.get("IsFavorite"))


def merged_user_data(src: dict, dst: dict) -> dict:
    """The fields to write on the kept copy; empty when nothing changes."""
    out = {}
    if src.get("Played") and not dst.get("Played"):
        out["Played"] = True
    if (src.get("PlayCount") or 0) > (dst.get("PlayCount") or 0):
        out["PlayCount"] = src["PlayCount"]
    if src.get("IsFavorite") and not dst.get("IsFavorite"):
        out["IsFavorite"] = True
    if (src.get("PlaybackPositionTicks") and not dst.get("PlaybackPositionTicks")
            and not (dst.get("Played") or out.get("Played"))):
        out["PlaybackPositionTicks"] = src["PlaybackPositionTicks"]
    s_last, d_last = src.get("LastPlayedDate"), dst.get("LastPlayedDate")
    if s_last and (not d_last or str(s_last)[:19] > str(d_last)[:19]):
        out["LastPlayedDate"] = s_last
    return out


def carry_user_data(db, dup, record, keep: str) -> int:
    """Merge every Jellyfin user's data on the copy a resolution removes onto
    the copy it keeps (keep = "download" | "vod"), before anything is deleted.
    Returns the number of writes. Raises UserDataCarryError when that can't be
    done safely; the caller then deletes nothing. Jellyfin not configured, or
    the removed copy not in Jellyfin: nothing to carry."""
    from models.database import get_setting
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return 0
    from services.jellyfin import JellyfinService
    jf = JellyfinService(url, key)
    writes = 0
    try:
        pairs, orphans = _copy_pairs(jf, dup, record, keep)
        if not pairs and not orphans:
            return 0
        users = jf.get_user_ids()
        if users is None:
            raise RuntimeError("could not list Jellyfin's users")
        for user_id in users:
            if any(_has_user_data(_user_data(jf, user_id, i)) for i in orphans):
                raise UserDataCarryError(
                    409, "The copy you are keeping isn't in Jellyfin yet, and users have watched state "
                         "(played, resume point, favourite) on the one you are removing. Scan the library "
                         "in Jellyfin, then try again. Nothing was deleted.")
            for src_id, dst_ids in pairs:
                src = _user_data(jf, user_id, src_id)
                if not _has_user_data(src):
                    continue
                for dst_id in dst_ids:
                    change = merged_user_data(src, _user_data(jf, user_id, dst_id))
                    if not change:
                        continue
                    r = jf.session.post(f"{jf.url}/UserItems/{dst_id}/UserData", params={"userId": user_id},
                                        json=change, timeout=_JF_TIMEOUT)
                    r.raise_for_status()
                    writes += 1
    except UserDataCarryError:
        raise
    except Exception as e:
        logger.error(f"Duplicate tmdb:{dup.tmdb_id}: could not carry users' watched state over: {e}")
        raise UserDataCarryError(
            502, "Couldn't carry users' watched state (played, resume point, favourite) over to the copy "
                 "you are keeping: Jellyfin didn't answer. Nothing was deleted; try again.")
    if writes:
        logger.info(f"Duplicate tmdb:{dup.tmdb_id}: carried {writes} user-data record(s) over to the kept copy")
    return writes
