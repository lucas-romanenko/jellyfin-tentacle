"""
Tentacle - Jellyfin Service
Manages Jellyfin items, tags, and playlists via the REST API.
"""

import json
import os
import re
import threading
import time
import logging
import requests
from pathlib import Path
from typing import Optional, List
from services.exceptions import JellyfinConnectionError

logger = logging.getLogger(__name__)

# Written into every YouTube video's NFO by services/youtube/library.py.
YOUTUBE_TAG = "youtube"


def is_youtube_video(item: dict) -> bool:
    """True for a video written by services/youtube, identified by the
    <uniqueid type="youtube"> its NFO carries (services/youtube/library.py),
    which Jellyfin exposes as ProviderIds["youtube"].

    Deliberately NOT tag-based: Jellyfin imports TMDB keywords as tags, and
    real films carry a "youtube" keyword ("Bo Burnham: Inside", "The Deep
    House", "The Sidemen Story"), so the tag alone matches genuine library
    content. Both callers already request ProviderIds."""
    return any(k.lower() == YOUTUBE_TAG for k in (item.get("ProviderIds") or {}))


# Seconds to wait for the plugin's PruneDead answer (see prune_dead_playlist_entries).
# A current plugin answers at once with a run id; only an old one prunes inside the call.
PRUNE_DEAD_READ_TIMEOUT = 900
# Polling a prune run: seconds between polls, failed polls in a row before giving
# up, and a ceiling on the whole wait (the playlist lock is held throughout).
PRUNE_POLL_SECONDS = 5
PRUNE_POLL_MAX_FAILURES = 24
PRUNE_POLL_MAX_SECONDS = 3 * 3600
# Jellyfin scheduled tasks that keep its database busy; the hourly prune skips
# an hour while one runs (each slow prune run coincided with one, #181).
BUSY_LIBRARY_TASKS = ("RefreshLibrary", "RefreshGuide")


class PartialListing(RuntimeError):
    """A paged Jellyfin listing lost a page after the first: it is incomplete."""


# Entries per DELETE /Playlists/{id}/Items call — see remove_from_playlist.
REMOVE_CHUNK_SIZE = 150


# ── Pending rating restores ────────────────────────────────────────────────
# A series update that Jellyfin cascaded, whose children's own ratings could
# not all be written back (a 500 on one child, Jellyfin going away mid-way).
# Kept on disk until every restore succeeds, and retried from these saved
# values on the next push: once the cascade has run, re-reading the children
# returns the series' copies, and the tags being correct means no later push
# would otherwise touch that series again.
#
# File shape: {series_id: {"copy": [official, custom] | null,
#                          "attempts": n, "since": epoch,
#                          "children": {child_id: {"type": "Season"|"Episode",
#                                                   "official": ..., "custom": ...,
#                                                   "season": season_id | null}}}}
# "copy" is what the series' update wrote onto its children.
_PENDING_RESTORES_LOCK = threading.RLock()
# One cascading update at a time (list -> save -> update -> restore). Two
# passes over one series at once (Refresh Tags beside the nightly push) let
# the second list the first one's copies as "own" ratings and flatten the
# series with nothing pending. Tentacle runs one worker process.
_CASCADE_LOCK = threading.RLock()
PENDING_MAX_ATTEMPTS = 10
PENDING_MAX_AGE_SECONDS = 7 * 86400
# How long a push waits for another series' cascading update to finish before
# it skips this series (its tags are still wrong, so the next run pushes it).
CASCADE_LOCK_TIMEOUT = 90
# A failed restore is tried once more after this pause, in the same run: most
# failures are transient, and the next run can be a day away.
IN_RUN_RETRY_DELAY = 5


def _in_run_retry_wait() -> None:
    if IN_RUN_RETRY_DELAY > 0:
        time.sleep(IN_RUN_RETRY_DELAY)


def _log_activity_safe(event: str, message: str) -> None:
    """Write an Activity entry (redacted); never fail the caller over it."""
    try:
        from models.database import SessionLocal, log_activity
        from services.log_redaction import redact
        db = SessionLocal()
        try:
            log_activity(db, event, redact(message))
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"[Jellyfin] Could not write '{event}' to the Activity log: {e}")


def _log_restore_given_up(parent_id: str, left: dict) -> None:
    """Put a give-up on the Activity page, not only in the log."""
    ids = ", ".join(f"{cid} ({c['official'] or '-'}/{c['custom'] or '-'})"
                    for cid, c in list(left.items())[:20])
    _log_activity_safe("rating_restore_failed",
                       f"Could not restore the own ratings of {len(left)} season(s)/episode(s) of "
                       f"Jellyfin series {parent_id} after a tag push; Jellyfin gave them the series' "
                       f"rating. Set these by hand in Jellyfin: {ids}")


def _pending_restores_path() -> Path:
    return Path(os.getenv("DATA_DIR", "/data")) / "pending_rating_restores.json"


def _valid_pending_entry(entry) -> Optional[dict]:
    """A normalised entry, or None if it cannot be used. Accepts the
    first on-disk form ({child: [type, official, custom]})."""
    if not isinstance(entry, dict):
        return None
    if "children" not in entry:                       # first form: child map only
        entry = {"copy": None, "attempts": 0, "since": time.time(), "children": entry}
    children = entry.get("children")
    if not isinstance(children, dict):
        return None
    out = {}
    for cid, c in children.items():
        if isinstance(c, list) and len(c) >= 3:
            c = {"type": c[0], "official": c[1], "custom": c[2], "season": None}
        if not isinstance(c, dict) or c.get("type") not in ("Season", "Episode"):
            continue
        if not all(v is None or isinstance(v, str)
                   for v in (c.get("official"), c.get("custom"), c.get("season"))):
            continue
        sr = c.get("season_rating")
        if not (isinstance(sr, list) and len(sr) == 2 and all(x is None or isinstance(x, str) for x in sr)):
            sr = None
        out[str(cid)] = {"type": c["type"], "official": c.get("official"),
                         "custom": c.get("custom"), "season": c.get("season"),
                         "season_rating": sr}
    def _pair(v):
        return v if isinstance(v, list) and len(v) == 2 and all(
            x is None or isinstance(x, str) for x in v) else None
    copy, prev_copy = _pair(entry.get("copy")), _pair(entry.get("prev_copy"))
    try:
        attempts, since = int(entry.get("attempts") or 0), float(entry.get("since") or time.time())
    except (TypeError, ValueError):
        attempts, since = 0, time.time()
    known = {}
    for cid, v in (entry.get("known") or {}).items() if isinstance(entry.get("known"), dict) else ():
        if isinstance(v, list) and len(v) == 2 and all(x is None or isinstance(x, str) for x in v):
            known[str(cid)] = v
    return ({"copy": copy, "prev_copy": prev_copy, "attempts": attempts, "since": since,
             "children": out, "known": known} if out else None)


def _pending_restores_load() -> dict:
    """The pending restores, validated. A file that cannot be read as that is
    reported and kept aside as <name>.corrupt-<time>, never dropped silently."""
    with _PENDING_RESTORES_LOCK:
        path = _pending_restores_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as e:
            logger.warning(f"[Jellyfin] Could not read pending rating restores {path}: {e}")
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        bad = not isinstance(data, dict)
        out = {}
        for sid, entry in (data.items() if isinstance(data, dict) else ()):
            norm = _valid_pending_entry(entry)
            if norm is None:
                bad = True
            else:
                out[str(sid)] = norm
        if bad and raw.strip():
            keep = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                keep.write_text(raw, encoding="utf-8")
            except OSError:
                pass
            logger.warning(f"[Jellyfin] {path} was unreadable or held entries of the wrong shape; "
                           f"kept a copy as {keep.name} and used the {len(out)} valid entr(y/ies)")
            _pending_write(out)
        return out


def _pending_write(data: dict) -> None:
    path = _pending_restores_path()
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[Jellyfin] Could not save pending rating restores to {path}: {e}")


# A series whose seasons/episodes can't be listed is not updated: with no
# snapshot, Jellyfin's cascade would give every episode the series' rating and
# nothing could put an episode's own back (a TV-MA episode left visible to a
# TV-PG profile for good). The update waits for the next push; only after this
# many failed listings in a row is it made anyway (reported), so a series can't
# stay out of its tag playlists for good. {series_id: {"count", "since"}}
DEFER_SERIES_MAX_ATTEMPTS = 3


def _deferred_series_path() -> Path:
    return Path(os.getenv("DATA_DIR", "/data")) / "deferred_series_updates.json"


# The same, in memory, used while the file can't be written: the count still
# grows, so a series is never deferred for good.
_deferred_series_mem: dict = {}
_deferred_series_disk_ok = True


def _deferred_series_load() -> dict:
    with _PENDING_RESTORES_LOCK:
        try:
            data = json.loads(_deferred_series_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {} if _deferred_series_disk_ok else dict(_deferred_series_mem)
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items()
                if isinstance(v, dict) and isinstance(v.get("count"), int)}


def _deferred_series_set(series_id: str, entry: Optional[dict]) -> None:
    global _deferred_series_disk_ok
    with _PENDING_RESTORES_LOCK:
        data = _deferred_series_load()
        if entry:
            data[series_id] = entry
        elif series_id in data:
            del data[series_id]
        else:
            return
        _deferred_series_mem.clear()
        _deferred_series_mem.update(data)
        path = _deferred_series_path()
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
            _deferred_series_disk_ok = True
        except OSError as e:
            _deferred_series_disk_ok = False
            logger.warning(f"[Jellyfin] Could not save deferred series updates to {path}: {e}")


def _pending_restores_set(parent_id: str, entry) -> None:
    """Store (or, with None, clear) the pending restore of one series. `entry`
    is a full entry or, as before, a bare {child_id: [...]} map."""
    with _PENDING_RESTORES_LOCK:
        data = _pending_restores_load()
        norm = _valid_pending_entry(entry) if entry else None
        if norm:
            data[parent_id] = norm
        elif parent_id in data:
            del data[parent_id]
        else:
            return
        _pending_write(data)


# Fields sent back as the GET returned them, and only when it returned one.
# Jellyfin's ItemUpdate treats the body as a full replacement: a field left
# out is cleared (checked live on 10.11.8 — CriticRating, CustomRating,
# ForcedSortName, PreferredMetadataLanguage on a movie; Status, EndDate,
# DisplayOrder, CustomRating on a series), and CustomRating feeds parental
# ratings. Echoing only what is set keeps the body small; the full DTO is
# what made some Jellyfin versions answer 500.
_ECHOED_ITEM_FIELDS = (
    "CustomRating", "CriticRating", "ForcedSortName", "PreferredMetadataLanguage",
    "PreferredMetadataCountryCode", "Status", "EndDate", "DisplayOrder",
    "AirDays", "AirTime", "RunTimeTicks", "AspectRatio", "Video3DFormat",
    "ProductionLocations", "DateCreated",
    # Numbering: cleared on an Episode/Season/track when omitted, and a
    # child's own rating is restored through this same body (see
    # _restore_child_ratings).
    "IndexNumber", "ParentIndexNumber", "AirsBeforeSeasonNumber",
    "AirsBeforeEpisodeNumber", "AirsAfterSeasonNumber", "Album",
)


def _item_update_payload(item: dict, **changes) -> dict:
    """An ItemUpdate body that changes only `changes` and keeps everything else.

    LockData is sent back as read: without it the item is unlocked, and on a
    series the unlock cascades to every season and episode. LockedFields is
    left out on purpose — Jellyfin leaves it alone when it is null. One thing
    no body can avoid: on a series Jellyfin copies OfficialRating and
    CustomRating to its seasons and episodes on every ItemUpdate.
    """
    payload = {
        "Id": item["Id"],
        "Name": item.get("Name", ""),
        "OriginalTitle": item.get("OriginalTitle", ""),
        "Overview": item.get("Overview", ""),
        "Genres": item.get("Genres", []),
        "Tags": item.get("Tags", []),
        "Studios": item.get("Studios", []),
        "People": item.get("People", []),
        "ProviderIds": item.get("ProviderIds", {}),
        "ProductionYear": item.get("ProductionYear"),
        "PremiereDate": item.get("PremiereDate"),
        "CommunityRating": item.get("CommunityRating"),
        "OfficialRating": item.get("OfficialRating", ""),
        "Taglines": item.get("Taglines", []),
        "LockData": item.get("LockData"),
    }
    for field in _ECHOED_ITEM_FIELDS:
        if item.get(field) is not None:
            payload[field] = item[field]
    payload.update(changes)
    return payload


# Tentacle marks every playlist it creates with this provider id (#152). Jellyfin
# shows no provider id it has no provider for, and keeps it in the playlist's own
# playlist.xml (<TentacleId>): the mark survives entry changes, a library scan, a
# full "replace all metadata" refresh, a restart, and a wiped Tentacle data
# directory (checked on 10.11.11). Only a marked playlist is ever taken over by
# name or deleted; a user's own playlist of the same name never carries it.
TENTACLE_PLAYLIST_PROVIDER = "Tentacle"
TENTACLE_PLAYLIST_MARK = "managed"


def is_tentacle_playlist(item: Optional[dict]) -> bool:
    """Whether a Jellyfin playlist (a listing entry with ProviderIds) is Tentacle's own."""
    ids = (item or {}).get("ProviderIds") or {}
    return any(k.lower() == TENTACLE_PLAYLIST_PROVIDER.lower() and v for k, v in ids.items())


class JellyfinService:
    def __init__(self, url: str, api_key: str, user_id: str = ""):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.session = requests.Session()
        self.session.headers.update({
            "X-Emby-Token": api_key,
            "Content-Type": "application/json",
        })

    def _check_401(self, r, path: str):
        if r.status_code == 401:
            logger.error("[Jellyfin] API key is invalid or expired — update in Settings → Connections")
            raise requests.HTTPError(
                f"401 Unauthorized on {path}", response=r
            )

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            r = self.session.get(f"{self.url}{path}", params=params, timeout=15)
            self._check_401(r, path)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.debug(f"Jellyfin GET {path} failed: {e}")
            return None

    def _post(self, path: str, data=None) -> bool:
        try:
            r = self.session.post(f"{self.url}{path}", json=data, timeout=15)
            self._check_401(r, path)
            r.raise_for_status()
            return True
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.debug(f"Jellyfin POST {path} failed: {e}")
            return False

    def test_connection(self) -> bool:
        """Test Jellyfin connection. Returns True if healthy, False otherwise."""
        try:
            r = self.session.get(f"{self.url}/System/Info", timeout=10)
            if r.status_code == 401:
                logger.error("[Jellyfin] API key is invalid or expired — update in Settings → Connections")
                return False
            r.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"[Jellyfin] Connection failed: {e}")
            return False

    def get_server_id(self) -> Optional[str]:
        """Jellyfin's server id, used to build web deep links. None if unreachable."""
        try:
            r = self.session.get(f"{self.url}/System/Info", timeout=10)
            r.raise_for_status()
            return r.json().get("Id")
        except Exception as e:
            logger.debug(f"[Jellyfin] Could not read server id: {e}")
            return None

    def _fetch_all_items(self, media_type: str = "Movie", with_path: bool = False) -> List[dict]:
        """Fetch all items of a type from Jellyfin with ProviderIds and Tags
        (and Path with with_path). Paginates automatically for libraries with
        more than 10,000 items."""
        items, _complete = self._fetch_all_items_checked(media_type, with_path=with_path)
        return items

    def _fetch_all_items_checked(self, media_type: str = "Movie", user_scoped: bool = False,
                                 ids_only: bool = False, with_path: bool = False) -> tuple:
        """Same as _fetch_all_items, plus whether every page actually arrived.

        A page that times out returns None from _get, and silently breaking out
        of the loop hands back a PARTIAL list that looks complete. Callers that
        cache the result need to know, or they persist a half-empty library.
        Returns (items, complete).

        user_scoped=True lists what the configured user is shown, with only
        ProviderIds. An unscoped /Items listing also returns items Jellyfin
        hides from every user (the non-primary half of merged movie versions,
        hidden duplicate series folders), so an id taken from it can open the
        wrong copy. Raises requests.HTTPError if Jellyfin rejects the user id.
        """
        all_items = []
        start_index = 0
        page_size = 10000
        complete = True
        while True:
            params = {
                "IncludeItemTypes": media_type,
                "Recursive": "true",
                "Fields": "ProviderIds,Tags,Path" if with_path else "ProviderIds,Tags",
                "Limit": page_size,
                "StartIndex": start_index,
            }
            if user_scoped and self.user_id:
                params.update({
                    "UserId": self.user_id,
                    "Fields": "ProviderIds",
                    "EnableImages": "false",
                    "EnableUserData": "false",
                })
            if ids_only:
                # The lightest possible rows, for callers that only need
                # provider ids and paths (the orphan sweep reads the whole
                # library and matches by path too, #261).
                params.update({"Fields": "ProviderIds,Path", "EnableImages": "false",
                               "EnableUserData": "false", "EnableTotalRecordCount": "true"})
            data = self._get("/Items", params=params)
            if not data:
                # Timeout / transport failure mid-pagination.
                complete = False
                logger.warning(
                    f"[Jellyfin] {media_type} listing stopped early at {start_index} items "
                    f"— the result is incomplete"
                )
                break
            items = data.get("Items", [])
            all_items.extend(items)
            total = data.get("TotalRecordCount", 0)
            start_index += len(items)
            if not items and start_index < total:
                # An empty page before the reported total is just as incomplete.
                complete = False
                break
            if start_index >= total or not items:
                break
        return all_items, complete

    def get_tmdb_lookup_checked(self, media_type: str = "Movie", user_scoped: bool = False) -> tuple:
        """(tmdb_lookup, complete) — see _fetch_all_items_checked."""
        if user_scoped and self.user_id:
            try:
                items, complete = self._fetch_all_items_checked(media_type, user_scoped=True)
            except requests.HTTPError as e:
                # A configured user id Jellyfin no longer knows (user deleted
                # or re-created) must not turn the lookup off: list unscoped.
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status not in (400, 404):
                    raise
                logger.warning(
                    f"[Jellyfin] jellyfin_user_id rejected (HTTP {status}); listing {media_type} unscoped"
                )
                items, complete = self._fetch_all_items_checked(media_type)
        else:
            items, complete = self._fetch_all_items_checked(media_type)
        lookup = {}
        for item in items:
            tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
            if tmdb_id:
                try:
                    lookup[int(tmdb_id)] = item
                except ValueError:
                    pass
        return lookup, complete

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize a title for flexible matching.

        Strips year suffixes like '(1979)', converts colons to ' -',
        collapses whitespace, lowercases.
        """
        title = re.sub(r'\s*\(\d{4}\)\s*$', '', title)
        title = title.replace(':', ' -').replace('  ', ' ')
        return title.lower().strip()

    def _title_variants(self, title: str) -> list:
        """Return a list of normalized title variants to try when matching."""
        variants = {self._normalize_title(title)}
        # Also try with colons kept (in case Jellyfin has the colon version)
        stripped = re.sub(r'\s*\(\d{4}\)\s*$', '', title).lower().strip()
        variants.add(stripped)
        return list(variants)

    def search_by_tmdb_id(self, tmdb_id: int, media_type: str = "Movie",
                          title: str = None, year: str = None,
                          file_name: str = None) -> Optional[dict]:
        """Find a Jellyfin item by TMDB ID, with title+year fallback.

        Jellyfin has no server-side filter for a specific provider ID value.
        We fetch all items and filter client-side. Falls back to normalized
        title+year matching for items without TMDB metadata (e.g. scanned MKVs).

        file_name: only an item whose file has this name (any folder, any
        case) matches. Right after a quality upgrade the listing can still hold
        the replaced file's item, which Jellyfin removes on its next scan, and
        a VOD copy of the same film shares its TMDB id.
        """
        items = self._fetch_all_items(media_type, with_path=bool(file_name))
        tmdb_str = str(tmdb_id)

        def same_file(item):
            if not file_name:
                return True
            name = re.split(r"[\\/]", item.get("Path") or "")[-1]
            return name.casefold() == file_name.casefold()

        # Primary: match by TMDB ID
        for item in items:
            if item.get("ProviderIds", {}).get("Tmdb") == tmdb_str and same_file(item):
                return item

        # Fallback: match by normalized title + year
        if title:
            variants = self._title_variants(title)
            for item in items:
                item_norm = self._normalize_title(item.get("Name", ""))
                if (item_norm in variants
                        and (not year or str(item.get("ProductionYear", "")) == str(year))
                        and same_file(item)):
                    return item

        return None

    def query_movies_with_media_sources(self, page_size: int = 2000) -> Optional[List[dict]]:
        """Every movie with ProviderIds + MediaSources (for probed lengths), paged.

        None — not [] — when any page fails: a partial listing must not read as
        "these titles are gone" to a caller that clears flags.
        """
        items, start = [], 0
        path = f"/Users/{self.user_id}/Items" if self.user_id else "/Items"
        while True:
            try:
                data = self._get(path, params={
                    "Recursive": "true", "IncludeItemTypes": "Movie",
                    "Fields": "ProviderIds,MediaSources,Path", "EnableImages": "false",
                    "StartIndex": start, "Limit": page_size,
                })
            except Exception as e:
                logger.warning(f"[Jellyfin] Movie listing failed at {start}: {e}")
                return None
            if not isinstance(data, dict) or "Items" not in data:
                return None
            page = data["Items"]
            items.extend(page)
            start += len(page)
            if not page or start >= (data.get("TotalRecordCount") or 0):
                return items

    def get_tmdb_lookup(self, media_type: str = "Movie") -> dict:
        """Build a {tmdb_id: jellyfin_item} lookup for all items of a type.

        Also builds a (title_lower, year) fallback index for items
        without TMDB metadata. Returns (tmdb_lookup, title_lookup).
        Use get_tmdb_lookup_with_fallback() if you need the title fallback.

        Much more efficient than calling search_by_tmdb_id per item —
        makes one API call instead of N.
        """
        items = self._fetch_all_items(media_type)
        lookup = {}
        for item in items:
            tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
            if tmdb_id:
                try:
                    lookup[int(tmdb_id)] = item
                except ValueError:
                    pass
        return lookup

    def _fresh_tags_by_id(self, media_type: str = "Movie") -> dict:
        """{item id: current Tags}, from a USER-scoped listing.

        Measured on Jellyfin 10.11.8: a recursive /Items listing without a user
        answers with Tags an item carried several writes ago, while the same
        listing with UserId (and /Users/{id}/Items, and /Items?ids=) answers
        with the current ones. The unscoped listing stays the source of WHAT
        exists -- a scoped one hides items that user cannot see -- and this only
        overlays the tags. Empty on any failure: the caller keeps what it had.
        """
        if not self.user_id:
            return {}
        out, start = {}, 0
        while True:
            data = self._get("/Items", params={
                "IncludeItemTypes": media_type, "Recursive": "true", "UserId": self.user_id,
                "Fields": "Tags", "EnableImages": "false", "EnableUserData": "false",
                "Limit": 10000, "StartIndex": start,
            })
            if not data:
                return {}
            page = data.get("Items", [])
            for it in page:
                out[it.get("Id")] = it.get("Tags") or []
            start += len(page)
            if not page or start >= data.get("TotalRecordCount", 0):
                return out

    def get_tmdb_lookup_with_fallback(self, media_type: str = "Movie", with_counts: bool = False) -> tuple:
        """Build both TMDB and title+year lookups in one API call.

        Returns (tmdb_lookup, title_lookup) where:
        - tmdb_lookup: {int(tmdb_id): item}
        - title_lookup: {(normalized_title, year_str): item}
        With `with_counts`, also {int(tmdb_id): number of items with that id}:
        one row can be two items (a provider .strm next to a downloaded file).

        Title keys are normalized (year suffixes stripped, colons → hyphens).
        Callers should normalize their lookup keys with _normalize_title().
        """
        items = self._fetch_all_items(media_type)
        # The callers of this lookup decide what to write to an item's TAGS, and
        # on Jellyfin 10.11 an unscoped recursive listing returns stale ones.
        fresh = self._fresh_tags_by_id(media_type)
        if fresh:
            for it in items:
                if it.get("Id") in fresh:
                    it["Tags"] = fresh[it["Id"]]
        tmdb_lookup = {}
        title_lookup = {}
        tmdb_counts: dict = {}
        for item in items:
            # YouTube videos live in their own Movies library, so they show up
            # in this listing. They have no TMDB id, which makes them reachable
            # only through the title fallback — and a video sharing a name with
            # a real film ("Frozen") would then be tagged as that film and pulled
            # into its playlists. They are never a valid fallback target.
            # Match the video itself, not the tag: a real film with the TMDB
            # keyword "youtube" must stay reachable.
            if is_youtube_video(item):
                continue
            tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
            if tmdb_id:
                try:
                    tmdb_lookup[int(tmdb_id)] = item
                    tmdb_counts[int(tmdb_id)] = tmdb_counts.get(int(tmdb_id), 0) + 1
                except ValueError:
                    pass
            name = item.get("Name", "")
            year = str(item.get("ProductionYear", ""))
            if name:
                norm = self._normalize_title(name)
                title_lookup[(norm, year)] = item
                # Also index the raw lowercase for exact matches
                raw = name.lower().strip()
                if raw != norm:
                    title_lookup[(raw, year)] = item
        if with_counts:
            return tmdb_lookup, title_lookup, tmdb_counts
        return tmdb_lookup, title_lookup

    def _item_path(self, item_id: str) -> str:
        """Return the user-scoped item path if user_id is set, otherwise the global path"""
        if self.user_id:
            return f"/Users/{self.user_id}/Items/{item_id}"
        return f"/Items/{item_id}"

    def get_item_tags(self, item_id: str) -> List[str]:
        """Get current tags for a Jellyfin item"""
        data = self._get(self._item_path(item_id))
        if data:
            return data.get("Tags", [])
        return []

    def set_item_tags(self, item_id: str, tags: List[str]) -> bool:
        """Set tags on a Jellyfin item (replaces existing).

        GET via user-scoped endpoint, build a minimal payload with only the
        fields Jellyfin needs for ItemUpdate, POST to global /Items/{id}.
        Using the full DTO causes 500 errors on some Jellyfin versions.
        """
        item = self._get(self._item_path(item_id))
        if not item:
            logger.warning(f"[Jellyfin] Cannot GET item {item_id} — set_item_tags aborted")
            return False

        old_tags = item.get("Tags", [])
        logger.debug(f"[Jellyfin] set_item_tags {item_id}: {old_tags} → {tags}")

        return self._post_item_update(item, _item_update_payload(item, Tags=tags), "set tags on")

    def set_item_owned_tags(self, item_id: str, desired: List[str], owned: set,
                            add_only: bool = False) -> str:
        """Bring Tentacle's tags on one item in line with `desired`, computed
        from a fresh GET of the item, never from a listing: the unscoped
        listing can be stale, and a keyword added since it was read would be
        wiped (#180). Tentacle's own tags (`owned`) not in `desired` come off,
        unless `add_only`; every other tag stays.

        Returns "written", "unchanged", "get_failed" (nothing written: merging
        into an empty list would drop every keyword) or "post_failed"."""
        from services.tagger import merge_owned_tags
        item = self._get(self._item_path(item_id))
        if not item:
            logger.warning(f"[Jellyfin] Cannot GET item {item_id} — its tags were left as they are")
            return "get_failed"
        fresh = list(item.get("Tags") or [])
        if add_only:
            merged = fresh + [t for t in desired if t not in fresh]
        else:
            merged = merge_owned_tags(fresh, desired, owned)
        if sorted(merged) == sorted(fresh):
            return "unchanged"
        ok = self._post_item_update(item, _item_update_payload(item, Tags=merged), "set tags on")
        return "written" if ok else "post_failed"

    # ── ItemUpdate and Jellyfin's rating cascade ──────────────────────────
    # Jellyfin 10.11.8 ItemUpdateController.UpdateItem: for a Series it sets
    # every Season's and Episode's OfficialRating (unless that child has
    # OfficialRating in LockedFields) and CustomRating (always) to the values
    # in the body; for a Season it does the same to its Episodes. There is no
    # condition — it happens on every update, a tags-only one included. A tag
    # push to a series therefore rated every TV-MA episode as the series (e.g.
    # TV-14), and a profile limited to TV-14 could then play it. Until 7444a25
    # a DisplayOrder mismatch in the body queued a ReplaceAllMetadata refresh
    # that re-read the episode NFOs and hid this for NFO-rated episodes.
    # _post_item_update snapshots the children first and writes back, child
    # by child, any rating of the child's OWN that the cascade changed.
    #
    # A child with no rating of its own is left with the copy: Jellyfin rates
    # an unrated item by its display parent (OfficialRatingForComparison /
    # GetCustomRatingForComparision walk up to the season, then the series),
    # so the copy is the rating it already had for parental control. Restoring
    # those too would cost a write per episode — on a live library 156,053 of
    # 158,485 seasons/episodes have no rating of their own, and only 2 have
    # one that differs from their series.

    _CASCADING_TYPES = ("Series", "Season")
    # Unscoped (no UserId): a jellyfin_user_id with parental limits would not
    # be shown exactly the episodes that need their rating back.
    _CHILD_FIELDS = ("Overview,Genres,Tags,Studios,People,ProviderIds,Taglines,CustomRating,"
                     "Settings,DateCreated,ProductionLocations,OriginalTitle,SpecialEpisodeNumbers")

    def _child_ratings(self, parent_id: str) -> Optional[dict]:
        """{id: (type, OfficialRating, CustomRating, SeasonId)} for every season
        and episode under parent_id, or None if the listing could not be read
        completely (a timeout, an HTTP error, a short page)."""
        out = {}
        start, page = 0, 1000
        params = {"ParentId": parent_id, "Recursive": "true",
                  "IncludeItemTypes": "Season,Episode",
                  "Fields": "CustomRating,Settings", "EnableImages": "false",
                  "EnableUserData": "false"}
        while True:
            params.update(StartIndex=start, Limit=page)
            try:
                data = self._get("/Items", params=dict(params))
            except requests.HTTPError as e:
                logger.debug(f"[Jellyfin] Listing the children of {parent_id} failed: {e}")
                return None
            if not data:
                return None
            items = data.get("Items") or []
            for it in items:
                out[it["Id"]] = (it.get("Type"), it.get("OfficialRating") or None,
                                 it.get("CustomRating") or None, it.get("SeasonId"))
            start += len(items)
            total = data.get("TotalRecordCount") or 0
            if not items or start >= total:
                return out if start >= total else None

    def _get_child(self, child_id: str) -> Optional[dict]:
        """One season/episode with every field the update body echoes."""
        try:
            data = self._get("/Items", params={"Ids": child_id, "Fields": self._CHILD_FIELDS,
                                               "EnableImages": "false", "EnableUserData": "false"})
        except requests.HTTPError as e:
            logger.debug(f"[Jellyfin] Reading {child_id} failed: {e}")
            return None
        if data is None:
            return None
        items = data.get("Items") or []
        return items[0] if items else {}

    @staticmethod
    def _own_rating_changed(own: tuple, now: tuple) -> bool:
        """Whether a child's own (non-empty) OfficialRating/CustomRating differs
        from what it has now. An empty own value inherits, so it never counts."""
        return any(o is not None and o != n for o, n in zip(own, now))

    @staticmethod
    def _copy_candidates(entry: dict, c: dict) -> Optional[list]:
        """The (official, custom) pairs a cascade can have left on child `c`:
        this push's series rating, an earlier push's (the series' rating may
        have changed in between), and its season's own — as recorded at push
        time, and as saved if the season itself is still pending. None for an
        entry of the first on-disk form, which did not record them."""
        if not entry.get("copy"):
            return None
        copies = [tuple(entry["copy"])]
        if entry.get("prev_copy"):
            copies.append(tuple(entry["prev_copy"]))
        if c.get("season_rating"):
            copies.append(tuple(c["season_rating"]))
        season = entry["children"].get(c.get("season") or "")
        if season:
            copies.append((season["official"], season["custom"]))
        return copies

    @staticmethod
    def _resolve_own(own: tuple, now: tuple, copies: Optional[list]) -> tuple:
        """Per field, what the child should carry: its saved own value (None =
        inherits) where it now shows that or a cascade's copy, the current value
        where that was set by hand since. Without recorded copies (first
        on-disk form) the saved value wins."""
        out = []
        for i, (o, n) in enumerate(zip(own, now)):
            if n == o or n is None or copies is None or any(cp[i] == n for cp in copies):
                out.append(o)          # the saved value, "inherit" (None) included
            else:
                out.append(n)          # set by hand since: keep it
        return tuple(out)

    @staticmethod
    def _own_after(own: tuple, shown: tuple, copies: Optional[list]) -> tuple:
        """A saved child's own rating in view of what it shows now: per field,
        a value no cascade can have written was set by hand and becomes the
        own value — also where the snapshot had none (it inherited then)."""
        out = []
        for i, (o, n) in enumerate(zip(own, shown)):
            if n == o or n is None or copies is None or any(cp[i] == n for cp in copies):
                out.append(o)
            else:
                out.append(n)
        return tuple(out)

    def _restore_from_snapshot(self, parent_id: str, entry: dict) -> dict:
        """Write back each child's own ratings from a pending entry. Seasons
        first; then EVERY saved episode, since any season written in this pass
        (or an earlier one) cascades its rating onto its episodes. Returns the
        children still pending: those that failed, plus all saved episodes of a
        season that failed — they must be written again once it succeeds.

        Values written are always the saved ones, and a child is only written
        while it still carries a cascaded copy: a rating someone set in between
        is left alone.
        """
        children = entry["children"]
        failed_seasons, failed, restored, skipped = set(), {}, 0, 0
        # Writing a season below cascades its rating onto its episodes — over
        # a rating someone set by hand on one of them since. So those episodes
        # are read first, and a hand edit found there becomes the episode's own
        # rating (saved as such if it has to stay pending). If one of them
        # cannot be read, its season is not written in this pass: its cascade
        # could destroy an edit nobody has seen.
        def own_now(c, shown):
            return self._own_after((c["official"], c["custom"]), shown, self._copy_candidates(entry, c))

        work = {cid: dict(c) for cid, c in children.items()}
        seasons = {cid for cid, c in work.items() if c["type"] == "Season"}
        unreadable = set()
        # Episodes of a season about to be written that are NOT in the entry
        # (unrated when the snapshot was taken) may have been given a rating
        # since, by hand. The season's cascade would overwrite it, so any
        # episode there showing a value no cascade wrote joins the entry.
        for sid in sorted(seasons):
            try:
                eps = self._child_ratings(sid)
            except Exception:
                eps = None
            if eps is None:
                unreadable.add(sid)
                continue
            season_own = [work[sid]["official"], work[sid]["custom"]]
            for eid, (etype, o, c, _s) in eps.items():
                if etype != "Episode" or eid in work:
                    continue
                probe = {"type": "Episode", "official": None, "custom": None,
                         "season": sid, "season_rating": season_own}
                copies = self._copy_candidates(entry, probe) or []
                own = tuple(v if v is not None and not any(cp[i] == v for cp in copies) else None
                            for i, v in enumerate((o, c)))
                if own != (None, None):
                    work[eid] = dict(probe, official=own[0], custom=own[1])
        for cid, c in work.items():
            if c["type"] == "Episode" and c.get("season") in seasons:
                try:
                    child = self._get_child(cid)
                except Exception:
                    child = None
                if child is None:
                    unreadable.add(c["season"])
                elif child:
                    c["official"], c["custom"] = own_now(c, (child.get("OfficialRating") or None,
                                                             child.get("CustomRating") or None))
        if work != children and _pending_restores_load().get(parent_id):
            # A hand edit found above is saved before any season is written: if
            # the process dies after that season's cascade, the retry must not
            # read the cascade's copy and put back the old value.
            saved = _pending_restores_load()[parent_id]
            known = dict(saved.get("known") or {})
            known.update({cid: [c["official"], c["custom"]] for cid, c in work.items()})
            _pending_restores_set(parent_id, dict(saved, children=work, known=known))
        for kind in ("Season", "Episode"):
            for cid, c in work.items():
                if c["type"] != kind:
                    continue
                if kind == "Season" and cid in unreadable:
                    failed[cid] = c
                    failed_seasons.add(cid)
                    continue
                try:
                    child = self._get_child(cid)
                    if child is None:
                        failed[cid] = c
                        if kind == "Season":
                            failed_seasons.add(cid)
                        continue
                    if not child:
                        continue                  # gone from the library: nothing to restore
                    now = (child.get("OfficialRating") or None, child.get("CustomRating") or None)
                    if kind == "Season" or c.get("season") not in seasons:
                        # Not pre-read: decide on what it shows now.
                        c["official"], c["custom"] = own_now(c, now)
                    own = (c["official"], c["custom"])
                    if not self._own_rating_changed(own, now):
                        continue
                    # Field by field: a field still holding a cascade's copy
                    # gets its own value back; a field set by hand since is
                    # kept (and has already become the own value above).
                    target = self._resolve_own(own, now, self._copy_candidates(entry, c))
                    if target == now:
                        skipped += 1
                        logger.info(f"[Jellyfin] Not restoring {cid}: its rating was changed to "
                                    f"{now[0] or '-'}/{now[1] or '-'} since the push")
                        continue
                    payload = _item_update_payload(child, OfficialRating=target[0] or "", CustomRating=target[1])
                    if self._post(f"/Items/{cid}", payload):
                        restored += 1
                    else:
                        failed[cid] = c
                        if kind == "Season":
                            failed_seasons.add(cid)
                except Exception as e:
                    logger.debug(f"[Jellyfin] Restoring the rating of {cid} failed: {e}")
                    failed[cid] = c
                    if kind == "Season":
                        failed_seasons.add(cid)
        # A season still pending will cascade onto its episodes when it is
        # finally written, so every saved episode of it stays pending too.
        for cid, c in work.items():
            if c["type"] == "Episode" and c.get("season") in failed_seasons:
                failed.setdefault(cid, c)
        if restored:
            logger.info(f"[Jellyfin] Restored the own ratings of {restored} season(s)/episode(s) "
                        f"under {parent_id} after Jellyfin's rating cascade")
        if failed:
            logger.warning(
                f"[Jellyfin] Could not restore the own ratings of {len(failed)} season(s)/episode(s) "
                f"under {parent_id} after Jellyfin's rating cascade; they now carry the series' "
                f"rating and will be retried on the next tag push: "
                + ", ".join(f"{cid}={c['official'] or '-'}/{c['custom'] or '-'}"
                            for cid, c in list(failed.items())[:10]))
        return failed

    def _finish_restore(self, parent_id: str, entry: dict) -> None:
        """Run one restore pass for `entry` and store what is left, if anything.
        A pass that leaves failures is followed, after a short pause, by one
        more pass over only those — costing nothing when nothing failed."""
        left = self._restore_from_snapshot(parent_id, entry)
        if left:
            _in_run_retry_wait()
            left = self._restore_from_snapshot(parent_id, dict(entry, children=left))
        if not left:
            _pending_restores_set(parent_id, None)
            return
        attempts = entry.get("attempts", 0) + 1
        age = time.time() - entry.get("since", time.time())
        if attempts >= PENDING_MAX_ATTEMPTS or age >= PENDING_MAX_AGE_SECONDS:
            logger.error(
                f"[Jellyfin] Giving up restoring the own ratings of {len(left)} season(s)/episode(s) "
                f"under {parent_id} after {attempts} attempt(s) over {int(age // 3600)} h; set them by "
                f"hand: " + ", ".join(f"{cid}={c['official'] or '-'}/{c['custom'] or '-'}"
                                      for cid, c in list(left.items())[:20]))
            _log_restore_given_up(parent_id, left)
            _pending_restores_set(parent_id, None)
            return
        _pending_restores_set(parent_id, dict(entry, children=left, attempts=attempts))

    def retry_pending_rating_restores(self) -> int:
        """Retry every restore a previous push could not finish. Returns how
        many series are still pending afterwards."""
        # The lock is taken per series and released in between, so a long
        # retry list never holds up a push for its whole length.
        for parent_id in list(_pending_restores_load()):
            if not _CASCADE_LOCK.acquire(timeout=CASCADE_LOCK_TIMEOUT):
                logger.warning(f"[Jellyfin] Another series update is still running; the rating "
                               f"restore of {parent_id} stays pending for the next run")
                continue
            try:
                entry = _pending_restores_load().get(parent_id)
                if entry:
                    self._finish_restore(parent_id, entry)
            finally:
                _CASCADE_LOCK.release()
        return len(_pending_restores_load())

    def _post_item_update(self, item: dict, payload: dict, what: str) -> bool:
        """POST an ItemUpdate; for a Series/Season keep the children's ratings."""
        if item.get("Type") not in self._CASCADING_TYPES:
            return self._post_update(item["Id"], payload, what)
        if not _CASCADE_LOCK.acquire(timeout=CASCADE_LOCK_TIMEOUT):
            # Nothing was changed, so nothing needs saving: the tags are still
            # wrong and the next push updates this series.
            logger.warning(f"[Jellyfin] Not updating {item.get('Type')} {item['Id']} now: another "
                           f"series update has held the lock for {CASCADE_LOCK_TIMEOUT} s; the next "
                           f"push will do it")
            return False
        try:
            return self._post_cascading_update(item, payload, what)
        finally:
            _CASCADE_LOCK.release()

    def _post_cascading_update(self, item: dict, payload: dict, what: str) -> bool:
        item_id = item["Id"]
        cascaded = [(payload.get("OfficialRating") or "").strip() or None,
                    payload.get("CustomRating") or None]
        # An earlier push that could not finish its restore: those values are
        # the children's real ones — a fresh read now would only see copies.
        pending = _pending_restores_load().get(item_id)
        need = {cid: dict(c) for cid, c in pending["children"].items()} if pending else {}
        children = self._child_ratings(item_id)
        if children is None:
            # Usually transient: one more look after a short pause before
            # updating without a snapshot.
            _in_run_retry_wait()
            children = self._child_ratings(item_id)
        if children is not None and pending:
            # A pending child that was set by hand since the failed restore:
            # the edit is now its own rating, not the saved one.
            for cid, c in need.items():
                if cid in children:
                    now = (children[cid][1], children[cid][2])
                    c["official"], c["custom"] = self._own_after(
                        (c["official"], c["custom"]), now, self._copy_candidates(pending, c))
        if children is None:
            deferred = _deferred_series_load().get(item_id) or {}
            count = deferred.get("count", 0) + 1
            if count < DEFER_SERIES_MAX_ATTEMPTS:
                _deferred_series_set(item_id, {"count": count, "since": deferred.get("since") or time.time()})
                logger.warning(
                    f"[Jellyfin] Could not list the seasons/episodes of {item.get('Type')} {item_id}; "
                    f"not updating it now, so Jellyfin can't give them its rating (attempt {count} of "
                    f"{DEFER_SERIES_MAX_ATTEMPTS}; the next push tries again)")
                return False
            _deferred_series_set(item_id, None)
            logger.warning(
                f"[Jellyfin] Could not list the seasons/episodes of {item.get('Type')} {item_id} "
                f"{count} times in a row; updating it anyway, so Jellyfin may give them its rating"
                + (" (a pending restore will still be applied)" if need else ""))
            _log_activity_safe(
                "rating_cascade_unprotected",
                f"Updated the tags of Jellyfin {item.get('Type', 'item').lower()} '{item.get('Name', '')}' "
                f"({item_id}) without being able to list its seasons and episodes first "
                f"({DEFER_SERIES_MAX_ATTEMPTS} tries in a row). Jellyfin copies a series' parental "
                f"rating onto all of them on every update, so episodes with a rating of their own may "
                f"now carry the series' rating, and one rated above the series may now be visible to "
                f"restricted profiles — check them in Jellyfin."
                + (" A saved restore was still applied." if need else ""))
        else:
            _deferred_series_set(item_id, None)
            seasons_to_restore = {cid for cid, c in need.items() if c["type"] == "Season"}
            # Values an earlier, unfinished push's cascade wrote. A child that
            # is not in that push's entry but shows one of them did not choose
            # it: it had no rating of its own then (every rated child the
            # cascade changed was saved), so it is an unrated child holding
            # the copy. Recording the copy as its "own" rating would pin it: a
            # season restored now cascades its rating onto the episode, and the
            # episode's "restore" would then write the series' copy back — a
            # TV-14 copy on an episode of a TV-MA season, for good.
            # This push's own cascaded value is NOT treated so: before any
            # cascade, an episode rated like its series is its real rating.
            copies = [tuple(pending[k]) for k in ("copy", "prev_copy") if pending and pending.get(k)]
            # Own ratings this unfinished entry already recorded, for children
            # restored since (and so no longer in its children): those are the
            # authority — an episode rated like its series keeps that rating.
            recorded = (pending or {}).get("known") or {}

            def own_of(cid, o, c):
                if cid in recorded:
                    return self._own_after(tuple(recorded[cid]), (o, c), copies)
                return tuple(None if v is not None and any(k[i] == v for k in copies) else v
                             for i, v in enumerate((o, c)))

            for cid, (ctype, o, c, season) in children.items():
                if cid in need:
                    continue
                own = own_of(cid, o, c)
                if self._own_rating_changed(own, tuple(cascaded)):
                    need[cid] = {"type": ctype, "official": own[0], "custom": own[1], "season": season}
                    if ctype == "Season":
                        seasons_to_restore.add(cid)
            # Restoring a season cascades its rating onto its episodes, so
            # every episode there with a rating of its own is restored too,
            # even one the series' cascade itself would not have changed.
            for cid, (ctype, o, c, season) in children.items():
                if ctype == "Episode" and season in seasons_to_restore and cid not in need:
                    own = own_of(cid, o, c)
                    if own != (None, None):
                        need[cid] = {"type": ctype, "official": own[0], "custom": own[1], "season": season}
            for cid, (ctype, o, c, season) in children.items():
                if cid in need and need[cid].get("season") is None and season:
                    need[cid]["season"] = season
            # Each episode also keeps its season's own rating as read now: if
            # the season is restored and the episode is not, the season's
            # cascade leaves that rating on the episode, and a retry must
            # recognise it as a copy, not a hand edit.
            for cid, e in need.items():
                if e["type"] == "Episode" and not e.get("season_rating"):
                    sid = e.get("season") or ""
                    if sid in need:
                        # The season is itself being restored (or still pending):
                        # what it shows now may be a copy — use its own rating.
                        e["season_rating"] = [need[sid]["official"], need[sid]["custom"]]
                    else:
                        s_row = children.get(sid)
                        if s_row and s_row[0] == "Season":
                            e["season_rating"] = [s_row[1], s_row[2]]
        entry = None
        if need:
            prev = pending.get("copy") if pending else None
            known = dict((pending or {}).get("known") or {})
            known.update({cid: [c["official"], c["custom"]] for cid, c in need.items()})
            entry = {"copy": cascaded, "prev_copy": prev if prev != cascaded else pending.get("prev_copy") if pending else None,
                     "children": need, "known": known,
                     "attempts": pending["attempts"] if pending else 0,
                     "since": pending["since"] if pending else time.time()}
            # Saved before the update, so a failure from here on is retried
            # from these values instead of being lost.
            _pending_restores_set(item_id, entry)
        try:
            ok = self._post_update(item_id, payload, what)
        except requests.HTTPError:
            ok = None
            raise
        finally:
            # Also when the update call failed: Jellyfin may have applied it
            # and lost only the response. The restore compares each child with
            # its saved rating, so if nothing was cascaded nothing is written
            # and the saved entry is simply cleared.
            if entry:
                self._finish_restore(item_id, _pending_restores_load().get(item_id)
                                     or _valid_pending_entry(entry))
        return ok

    def _post_update(self, item_id: str, payload: dict, what: str) -> bool:
        try:
            r = self.session.post(f"{self.url}/Items/{item_id}", json=payload, timeout=15)
            self._check_401(r, f"/Items/{item_id}")
            if r.status_code >= 400:
                body = r.text[:200] if r.text else "(empty)"
                logger.error(f"[Jellyfin] POST /Items/{item_id} returned {r.status_code}: {body}")
                return False
            logger.debug(f"[Jellyfin] POST /Items/{item_id} returned {r.status_code} OK")
            return True
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[Jellyfin] Failed to {what} {item_id}: {e}")
            return False

    def set_item_name(self, item_id: str, name: str) -> bool:
        """Rename a Jellyfin item in place — same item, same id, same user data.

        Same minimal ItemUpdate payload as set_item_tags, plus LockData: an
        update that leaves it out unlocks the item (checked on 10.11.8), and a
        YouTube item's lock is what keeps remote providers from re-identifying
        it as some film of the same name.
        """
        item = self._get(self._item_path(item_id))
        if not item:
            logger.warning(f"[Jellyfin] Cannot GET item {item_id} — set_item_name aborted")
            return False
        return self._post_item_update(item, _item_update_payload(item, Name=name), "rename")

    def add_tag_to_item(self, item_id: str, tag: str) -> bool:
        """Add a single tag without removing existing tags"""
        current = self.get_item_tags(item_id)
        if tag in current:
            return True  # Already tagged
        return self.set_item_tags(item_id, current + [tag])

    def notify_media_updated(self, paths: List[str], update_type: str = "Created") -> bool:
        """Tell Jellyfin specific paths just appeared, so it imports only them.

        This is what Radarr and Sonarr's own Jellyfin connection does, and why
        a download shows up in seconds: no library scan at all, just the new
        folders. Paths must be as Jellyfin sees them, not as Tentacle does.
        """
        if not paths:
            return False
        return self._post("/Library/Media/Updated", {
            "Updates": [{"Path": p, "UpdateType": update_type} for p in paths]
        })

    def trigger_library_scan(self, library_id: Optional[str] = None) -> bool:
        """Scan for new files — everywhere, or in one library.

        For one library this has to say Recursive=true. Without it, Jellyfin
        refreshes the library folder's own metadata and never looks inside,
        so nothing new is found: a targeted "scan" that did nothing, which is
        what this was. Default refresh modes, so existing tags survive.
        """
        path = "/Library/Refresh"
        if library_id:
            path = (f"/Items/{library_id}/Refresh?Recursive=true"
                    f"&MetadataRefreshMode=Default&ImageRefreshMode=Default"
                    f"&ReplaceAllMetadata=false&ReplaceAllImages=false")
        return self._post(path)

    def refresh_item_metadata(self, item_id: str, replace_all: bool = False) -> bool:
        """Trigger a metadata refresh on a single item (identify, fetch images).

        Uses Default mode so Jellyfin fills in missing metadata/images
        without replacing existing fields like tags. `replace_all` re-reads
        everything (a full refresh that replaces metadata and images): only
        for a .strm whose NFO now names another film ("Fix it"), whose tags
        live in that NFO, so nothing set through the API is lost.
        """
        mode, replace = ("FullRefresh", "true") if replace_all else ("Default", "false")
        params = {
            "MetadataRefreshMode": mode,
            "ImageRefreshMode": mode,
            "ReplaceAllMetadata": replace,
            "ReplaceAllImages": replace,
        }
        try:
            r = self.session.post(
                f"{self.url}/Items/{item_id}/Refresh",
                params=params,
                timeout=15,
            )
            self._check_401(r, f"/Items/{item_id}/Refresh")
            r.raise_for_status()
            return True
        except Exception as e:
            logger.debug(f"Metadata refresh failed for {item_id}: {e}")
            return False

    def wait_for_images(self, item_id: str, max_wait: int = 30, poll_interval: int = 3) -> bool:
        """Wait for Jellyfin to fetch poster/backdrop images after a metadata refresh.

        Polls the item until ImageTags.Primary exists or timeout is reached.
        Returns True if images are available, False on timeout.
        """
        import time
        elapsed = 0
        while elapsed < max_wait:
            item = self.get_item_by_id(item_id)
            if item and item.get("ImageTags", {}).get("Primary"):
                return True
            time.sleep(poll_interval)
            elapsed += poll_interval
        return False

    def get_libraries(self) -> List[dict]:
        """Get all libraries"""
        data = self._get("/Library/VirtualFolders")
        return data or []

    def get_genres(self) -> List[str]:
        """Get all genres from Jellyfin library."""
        data = self._get("/Genres", params={
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        })
        if data:
            return [item["Name"] for item in data.get("Items", []) if item.get("Name")]
        return []

    # ── Item Queries ─────────────────────────────────────────────────────

    def query_items(self, include_types: List[str], tags: List[str] = None,
                    genres: List[str] = None, years: List[int] = None,
                    min_rating: float = None, max_rating: float = None,
                    sort_by: str = None, sort_order: str = "Ascending",
                    limit: int = None, min_premiere_date: str = None,
                    max_premiere_date: str = None, user_id: str = None) -> List[dict]:
        """Query Jellyfin items with filters matching SmartList expression logic.

        ``user_id`` asks as that user. On Jellyfin 10.11 a recursive query
        without a user filters on metadata as it was several edits ago (the
        same quirk as the stale Tags behind #107), so a playlist built for a
        user should pass that user's id: it then sees current values and only
        what that user may see.
        """
        params = {
            "Recursive": "true",
            "Fields": "ProviderIds,Tags,Genres,CommunityRating",
        }
        if user_id:
            params["UserId"] = user_id
        if include_types:
            params["IncludeItemTypes"] = ",".join(include_types)
        if tags:
            params["Tags"] = "|".join(tags)
        if genres:
            params["Genres"] = "|".join(genres)
        if years:
            params["Years"] = ",".join(str(y) for y in years)
        if min_rating is not None:
            params["MinCommunityRating"] = min_rating
        if max_rating is not None:
            params["MaxCommunityRating"] = max_rating
        if min_premiere_date:
            params["MinPremiereDate"] = min_premiere_date
        if max_premiere_date:
            params["MaxPremiereDate"] = max_premiere_date
        if sort_by:
            params["SortBy"] = sort_by
            params["SortOrder"] = sort_order

        if limit:
            # An explicit limit is requested — a single page is enough.
            params["Limit"] = limit
            data = self._get("/Items", params=params)
            return data.get("Items", []) if data else []

        # No limit: page through ALL matching items. A single un-limited /Items
        # response is capped at ~500 by Jellyfin, which silently truncated large
        # playlists (e.g. Netflix Movies stuck at ~500 of 3,600+ tagged). Paging
        # with StartIndex/Limit preserves the sort (Jellyfin sorts before paging).
        all_items = []
        start = 0
        page = 2000
        while True:
            params["Limit"] = page
            params["StartIndex"] = start
            data = self._get("/Items", params=params)
            if not data:
                if all_items:
                    # A later page failed (timeout, reset). What arrived is
                    # the start of the answer, not all of it, and a playlist
                    # diffed against it lost every entry past this page (1,600
                    # of a 3,600-entry playlist, #167). Callers already treat
                    # an exception as "leave it be". A failed FIRST page still
                    # returns [] for the empty-result guard the callers have.
                    raise PartialListing(
                        f"Jellyfin /Items page at {start} failed; "
                        f"only {len(all_items)} item(s) read")
                break
            items = data.get("Items", [])
            all_items.extend(items)
            start += len(items)
            total = data.get("TotalRecordCount") or 0
            if len(items) < page or start >= total or start > 100000:
                break
        return all_items

    # ── Playlist Management ──────────────────────────────────────────────

    def create_playlist(self, name: str, item_ids: List[str] = None,
                        user_id: str = None, is_public: bool = False) -> Optional[str]:
        """Create a new playlist. Returns the playlist ID or None.

        Args:
            user_id: Override self.user_id for the playlist owner.
            is_public: If False, only the owner can see the playlist.
        """
        uid = user_id or self.user_id
        body = {
            "Name": name,
            "MediaType": "Video",
            "IsPublic": is_public,
        }
        if uid:
            body["UserId"] = uid
        if item_ids:
            body["Ids"] = item_ids
        try:
            r = self.session.post(f"{self.url}/Playlists", json=body, timeout=120)
            self._check_401(r, "/Playlists")
            r.raise_for_status()
            return r.json().get("Id")
        except requests.HTTPError:
            raise
        except Exception as e:
            # A warning, not debug: a playlist that silently fails to exist is
            # the hardest kind of missing to diagnose from outside.
            logger.warning(f"Failed to create playlist '{name}': {e}")
            return None

    def get_playlist_items(self, playlist_id: str, limit: int = 50000) -> Optional[List[dict]]:
        """Get all items in a playlist. Includes SeriesId so callers can group
        episodes (Jellyfin expands series into episodes inside playlists) back to
        their series for comparison.

        Returns None when the playlist could not be read (timeout / transport
        error). Callers must not treat that as an empty playlist: doing so
        re-appends every desired item, or counts a full playlist as holding 0.
        """
        # Page through the playlist. One request capped at `limit` silently
        # truncated anything longer (a TV playlist stores episodes, so a few
        # hundred series is tens of thousands of entries), and a truncated
        # listing read as "these entries are gone" (#31). A page that fails
        # makes the whole read unknown, never a shorter playlist.
        items: List[dict] = []
        start = 0
        while True:
            params = {"Limit": limit, "StartIndex": start, "Fields": "SeriesId"}
            if self.user_id:
                params["UserId"] = self.user_id
            data = self._get(f"/Playlists/{playlist_id}/Items", params=params)
            if data is None:
                return None
            page = data.get("Items", [])
            items.extend(page)
            total = data.get("TotalRecordCount")
            start += len(page)
            if not page or total is None or start >= total:
                return items

    def count_series_episodes(self, series_id: str) -> Optional[int]:
        """Episodes Jellyfin would expand a Series into when it is added to a
        playlist. None = unknown (request failed)."""
        params = {"ParentId": series_id, "IncludeItemTypes": "Episode",
                  "Recursive": "true", "Limit": 0}
        if self.user_id:
            params["UserId"] = self.user_id
        try:
            data = self._get("/Items", params=params)
        except Exception:
            return None
        if data is None:
            return None
        return data.get("TotalRecordCount")

    def add_to_playlist(self, playlist_id: str, item_ids: List[str]) -> bool:
        """Add items to an existing playlist in chunks of 25.

        Jellyfin re-saves and re-indexes the whole playlist on every add, so the
        request time grows with playlist size. Smaller chunks keep each request
        light and the timeout is generous — a 50-item chunk at a 15s timeout was
        timing out on larger playlists, which aborted the whole rebuild and left
        playlists frozen at their previous (partial) contents.
        """
        if not item_ids:
            return True
        chunk_size = 25
        for i in range(0, len(item_ids), chunk_size):
            chunk = item_ids[i:i + chunk_size]
            try:
                params = {"Ids": ",".join(chunk)}
                if self.user_id:
                    params["UserId"] = self.user_id
                r = self.session.post(
                    f"{self.url}/Playlists/{playlist_id}/Items",
                    params=params,
                    timeout=120,
                )
                self._check_401(r, f"/Playlists/{playlist_id}/Items")
                if r.status_code >= 400:
                    body = r.text[:200] if r.text else "(empty)"
                    logger.error(f"[Jellyfin] Failed to add items to playlist {playlist_id}: {r.status_code} {body}")
                    return False
            except requests.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[Jellyfin] Failed to add items to playlist {playlist_id}: {e}")
                return False
        return True

    def move_playlist_item(self, playlist_id: str, item_id: str, new_index: int) -> bool:
        """Move an item within a playlist to a new position.

        Asks the Tentacle plugin first: Jellyfin 10.11's own Move endpoint takes
        the user from the caller's token and ignores ?UserId=, so with a server
        API key (no user) it always answers 400. The plugin runs inside Jellyfin
        and moves the entry as the owning user. 404/405 = no plugin, or one too
        old to have the route: fall back to the native call as before.
        """
        try:
            if self.user_id:
                path = f"/Tentacle/Playlists/{playlist_id}/Items/{item_id}/Move/{new_index}"
                r = self.session.post(f"{self.url}{path}", params={"userId": self.user_id}, timeout=10)
                self._check_401(r, path)
                if r.status_code not in (404, 405):
                    if r.status_code >= 400:
                        logger.warning(f"Move playlist item failed: plugin answered HTTP {r.status_code} for playlist={playlist_id} item={item_id} index={new_index}")
                    return r.status_code < 400
            # UserId for private per-user playlists (same reason as remove_from_playlist).
            params = {}
            if self.user_id:
                params["UserId"] = self.user_id
            r = self.session.post(
                f"{self.url}/Playlists/{playlist_id}/Items/{item_id}/Move/{new_index}",
                params=params,
                timeout=10,
            )
            self._check_401(r, f"/Playlists/{playlist_id}/Items/{item_id}/Move/{new_index}")
            if r.status_code >= 400:
                logger.warning(f"Move playlist item failed: HTTP {r.status_code} for playlist={playlist_id} item={item_id} index={new_index}")
            return r.status_code < 400
        except Exception as e:
            logger.warning(f"Move playlist item exception: {e}")
            return False

    def prune_dead_playlist_entries(self, playlist_ids: List[str]) -> Optional[dict]:
        """Drop entries whose item no longer exists from these playlists (#120).

        Jellyfin hides an entry whose file is gone from /Playlists/{id}/Items,
        so nothing Tentacle reads can see it to remove it; it stays in
        playlist.xml and is warned about on every read. The Tentacle plugin
        removes them in-process. Returns the plugin's summary, or None when the
        plugin is missing or too old for the route (404/405) or the call failed.

        A current plugin runs the prune in the background and answers 202 with a
        run id; this polls the run until the plugin says it is done, so the
        caller's playlist lock is held exactly as long as the plugin works. With
        one long call Tentacle gave up at its read timeout while Jellyfin was
        busy, released the lock under a plugin still rewriting playlists, and
        lost the summary (#181). An old plugin prunes inside the call and answers
        the summary directly.
        """
        if not playlist_ids:
            return {"checkedPlaylists": 0, "prunedPlaylists": 0, "removed": 0}
        path = "/Tentacle/Playlists/PruneDead"
        try:
            # One call for every playlist: the plugin's storage-offline guard
            # works across the whole run. The long read timeout is for an old
            # plugin, which prunes inside the call (~80 s for 30 playlists of up
            # to 16k entries, far longer while Jellyfin scans).
            r = self.session.post(f"{self.url}{path}", json={"Ids": list(playlist_ids), "Async": True},
                                  timeout=(10, PRUNE_DEAD_READ_TIMEOUT))
            self._check_401(r, path)
            if r.status_code in (404, 405):
                logger.debug("[Jellyfin] Tentacle plugin has no PruneDead route — dead playlist entries not cleaned")
                return None
            if r.status_code >= 400:
                logger.warning(f"[Jellyfin] Pruning dead playlist entries failed: HTTP {r.status_code}")
                return None
            body = r.json()
            if r.status_code == 202 and isinstance(body, dict) and body.get("runId"):
                return self._wait_for_prune_run(str(body["runId"]))
            return body
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.warning(f"[Jellyfin] Pruning dead playlist entries failed: {e}")
            return None

    def _wait_for_prune_run(self, run_id: str) -> Optional[dict]:
        """Poll a plugin prune run until it has finished; its summary, or None."""
        path = f"/Tentacle/Playlists/PruneDead/{run_id}"
        deadline = time.monotonic() + PRUNE_POLL_MAX_SECONDS
        failures = 0
        while True:
            time.sleep(PRUNE_POLL_SECONDS)
            try:
                r = self.session.get(f"{self.url}{path}", timeout=(10, 30))
                self._check_401(r, path)
                if r.status_code == 404:
                    logger.warning("[Jellyfin] Jellyfin no longer knows the dead-entry prune it was running "
                                   "(restarted?) — it will be tried again next hour")
                    return None
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}")
                run = r.json() or {}
                failures = 0
                if run.get("state") == "done":
                    return run.get("result") or {}
                if run.get("state") == "failed":
                    logger.warning(f"[Jellyfin] Pruning dead playlist entries failed in Jellyfin: {run.get('error')}")
                    return None
            except requests.HTTPError:
                raise
            except Exception as e:
                failures += 1
                if failures >= PRUNE_POLL_MAX_FAILURES:
                    logger.warning(f"[Jellyfin] Lost track of the dead-entry prune after {failures} failed polls: {e}")
                    return None
            if time.monotonic() > deadline:
                logger.warning(f"[Jellyfin] The dead-entry prune is still running after "
                               f"{PRUNE_POLL_MAX_SECONDS // 60} min — no longer waiting for it")
                return None

    def running_library_tasks(self) -> List[str]:
        """Names of Jellyfin's library scan / guide refresh tasks running now.
        [] when there are none or Jellyfin can't say (never blocks the caller)."""
        try:
            r = self.session.get(f"{self.url}/ScheduledTasks", params={"isHidden": "false"}, timeout=10)
            if r.status_code >= 400:
                return []
            tasks = r.json()
            return [t.get("Name") or t.get("Key") for t in tasks or []
                    if isinstance(t, dict) and t.get("Key") in BUSY_LIBRARY_TASKS and t.get("State") == "Running"]
        except Exception:
            return []

    def get_ownerless_playlists(self) -> Optional[List[dict]]:
        """Playlists with no owner, from the Tentacle plugin: [{id, name, entries,
        openAccess, shares}]. None when the plugin can't answer — Jellyfin's own
        API never says who owns a playlist."""
        path = "/Tentacle/Playlists/Ownerless"
        try:
            r = self.session.get(f"{self.url}{path}", timeout=30)
            self._check_401(r, path)
            if r.status_code >= 400:
                if r.status_code not in (404, 405):
                    logger.warning(f"[Jellyfin] Listing ownerless playlists failed: HTTP {r.status_code}")
                return None
            data = r.json()
            return data if isinstance(data, list) else None
        except requests.HTTPError:
            raise
        except Exception as e:
            logger.warning(f"[Jellyfin] Listing ownerless playlists failed: {e}")
            return None

    def remove_from_playlist(self, playlist_id: str, entry_ids: List[str]) -> bool:
        """Remove items from a playlist by their PlaylistItemId, in chunks.

        UserId is REQUIRED for private (per-user) playlists — without it Jellyfin
        returns 204 but silently removes nothing, which is what bloated playlists
        over time. Each chunk is retried a few times and a chunk that ultimately
        fails is skipped rather than aborting the whole clear — when many
        playlists rebuild at once Jellyfin can transiently error, and aborting on
        the first bad chunk left playlists only partially cleared. Returns True
        only if every chunk succeeded.
        """
        if not entry_ids:
            return True
        # Jellyfin rewrites the whole playlist on every DELETE, so a call costs
        # the same whether it removes 25 entries or 200 (measured on a
        # 40k-entry playlist: ~23 s either way). At 25 a call, clearing a
        # large playlist for an order-changing rebuild took many hours, with
        # the playlist half-empty and the refresh lock held throughout. The
        # ceiling is the URL: 400 ids (13 KB) is refused with 414, 200 (6.6 KB)
        # is accepted, so 150 leaves headroom under Kestrel's 8 KB request line.
        chunk_size = REMOVE_CHUNK_SIZE
        all_ok = True
        for i in range(0, len(entry_ids), chunk_size):
            chunk = entry_ids[i:i + chunk_size]
            params = {"EntryIds": ",".join(chunk)}
            if self.user_id:
                params["UserId"] = self.user_id
            removed = False
            for attempt in range(3):
                try:
                    r = self.session.delete(
                        f"{self.url}/Playlists/{playlist_id}/Items",
                        params=params,
                        timeout=120,
                    )
                    self._check_401(r, f"/Playlists/{playlist_id}/Items")
                    if r.status_code < 400:
                        removed = True
                        break
                    body = r.text[:200] if r.text else "(empty)"
                    logger.warning(f"[Jellyfin] Remove chunk failed (try {attempt + 1}/3) for playlist {playlist_id}: {r.status_code} {body}")
                except requests.HTTPError:
                    raise
                except Exception as e:
                    logger.warning(f"[Jellyfin] Remove chunk error (try {attempt + 1}/3) for playlist {playlist_id}: {e}")
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
            if not removed:
                all_ok = False
                logger.error(f"[Jellyfin] Gave up on a remove chunk for playlist {playlist_id} after 3 tries — continuing with the rest")
        return all_ok

    def get_playlists(self, user_id: str = None) -> List[dict]:
        """List all playlists owned by / visible to the user (name + id + counts)."""
        uid = user_id or self.user_id
        data = self._get("/Items", params={
            "IncludeItemTypes": "Playlist",
            "Recursive": "true",
            "UserId": uid,
            "Fields": "ChildCount,ProviderIds",
        })
        if data:
            return data.get("Items", [])
        return []

    def get_playlists_checked(self, user_id: str = None):
        """get_playlists(), but None — not [] — when the listing did not arrive.

        _get() swallows timeouts, so get_playlists() answers [] for a user whose
        listing failed; a caller deciding what is safe to DELETE must be able to
        tell that apart from "this user sees no playlists".
        """
        uid = user_id or self.user_id
        try:
            data = self._get("/Items", params={
                "IncludeItemTypes": "Playlist",
                "Recursive": "true",
                "UserId": uid,
                "Fields": "ChildCount,ProviderIds",
            })
        except Exception as e:
            logger.warning(f"[Jellyfin] Playlist listing for user {uid} failed: {e}")
            return None
        if not isinstance(data, dict) or "Items" not in data:
            return None
        return data["Items"]

    def get_user_ids(self):
        """Ids of EVERY Jellyfin user, or None when Jellyfin would not say.

        Not every Jellyfin user is a Tentacle user: family members who only ever
        open a Jellyfin client never log into the dashboard, yet their playlists
        live in the same store.
        """
        try:
            r = self.session.get(f"{self.url}/Users", timeout=15)
            self._check_401(r, "/Users")
            r.raise_for_status()
            return [u["Id"] for u in r.json() if u.get("Id")]
        except Exception as e:
            logger.warning(f"[Jellyfin] Could not list users: {e}")
            return None

    def _owned_playlist(self, playlist_id: str, user_id: str = None) -> Optional[dict]:
        """The playlist as its owner sees it, or None when it can't be read. A
        private playlist is visible only to its owner, so the admin's view is no
        use here."""
        uid = user_id or self.user_id
        path = f"/Users/{uid}/Items/{playlist_id}" if uid else f"/Items/{playlist_id}"
        return self._get(path)

    def mark_tentacle_playlist(self, playlist_id: str, user_id: str = None) -> bool:
        """Mark a playlist as Tentacle's own (see TENTACLE_PLAYLIST_PROVIDER).
        True when it carries the mark afterwards."""
        item = self._owned_playlist(playlist_id, user_id)
        if not item:
            logger.warning(f"[Jellyfin] Cannot read playlist {playlist_id} to mark it as Tentacle's")
            return False
        if is_tentacle_playlist(item):
            return True
        ids = dict(item.get("ProviderIds") or {})
        ids[TENTACLE_PLAYLIST_PROVIDER] = TENTACLE_PLAYLIST_MARK
        return self._post_item_update(item, _item_update_payload(item, ProviderIds=ids),
                                      "mark as Tentacle's")

    def rename_tentacle_playlist(self, playlist_id: str, name: str, user_id: str = None) -> bool:
        """Rename a playlist in place (same id, entries and mark), only if it
        carries Tentacle's mark: a user's own playlist is never changed (#152).
        True when it has that name afterwards."""
        item = self._owned_playlist(playlist_id, user_id)
        if not item:
            logger.warning(f"[Jellyfin] Cannot read playlist {playlist_id} to rename it")
            return False
        if item.get("Name") == name:
            return True
        if not is_tentacle_playlist(item):
            logger.info(f"[Jellyfin] Not renaming playlist '{item.get('Name')}' ({playlist_id}): "
                        f"Tentacle didn't make it")
            return False
        return self._post_item_update(item, _item_update_payload(item, Name=name), "rename")

    def delete_tentacle_playlist(self, playlist_id: str, user_id: str = None) -> bool:
        """Delete a playlist only if it carries Tentacle's mark (#152).

        Every path that deletes a playlist it has linked by id (a rule deleted, a
        playlist switched off, an orphaned SmartList, a removed YouTube source)
        goes through here. A linked playlist without the mark was the user's own,
        taken over by name before marks existed: it is unlinked and kept. One
        that can't be read is kept too."""
        item = self._owned_playlist(playlist_id, user_id)
        if not item:
            logger.info(f"[Jellyfin] Playlist {playlist_id} is gone or can't be read; nothing deleted")
            return False
        if not is_tentacle_playlist(item):
            logger.info(f"[Jellyfin] Keeping playlist '{item.get('Name')}' ({playlist_id}): "
                        f"Tentacle didn't make it, so it is only unlinked")
            return False
        return self.delete_item(playlist_id)

    def delete_item(self, item_id: str) -> bool:
        """Delete an item (playlist, collection, etc.) from Jellyfin."""
        try:
            r = self.session.delete(f"{self.url}/Items/{item_id}", timeout=15)
            self._check_401(r, f"/Items/{item_id}")
            if r.status_code < 400:
                logger.info(f"[Jellyfin] Deleted item {item_id}")
                return True
            logger.warning(f"[Jellyfin] Failed to delete item {item_id}: {r.status_code}")
            return False
        except Exception as e:
            logger.error(f"[Jellyfin] Failed to delete item {item_id}: {e}")
            return False

    def get_item_by_id(self, item_id: str) -> Optional[dict]:
        """Check if an item exists by ID."""
        return self._get(self._item_path(item_id))

    def item_exists(self, item_id: str) -> Optional[bool]:
        """Tri-state existence probe: True = present, False = definitely gone
        (Jellyfin answered 404), None = unknown (timeout / transport failure).

        `get_item_by_id()` conflates the last two — it returns None for a
        timeout and *raises* on a real 404 — which read as "deleted, recreate
        it" and produced runaway duplicate playlists. Callers must only act on
        an explicit False. The timeout is generous because the user-scoped item
        endpoint is slow for playlist-sized items on a large library.
        """
        path = self._item_path(item_id)
        try:
            r = self.session.get(f"{self.url}{path}", timeout=45)
            self._check_401(r, path)
            if r.status_code == 404:
                return False
            r.raise_for_status()
            return True
        except requests.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404:
                return False
            logger.warning(f"[Jellyfin] Existence check for {item_id} failed: HTTP {status}")
            return None
        except Exception as e:
            logger.warning(f"[Jellyfin] Existence check for {item_id} could not complete: {e}")
            return None

    def wait_for_library_scan(self, expected_count: int = 0, media_type: str = "Movie",
                              max_wait: int = 120, poll_interval: int = 10) -> bool:
        """Wait for a library scan to complete by polling item count.

        Triggers a scan, then polls until item count stabilizes or increases
        past expected_count. Returns True if scan appears complete.
        """
        import time

        self.trigger_library_scan()
        logger.info(f"[Jellyfin] Triggered library scan, waiting for indexing (max {max_wait}s)...")

        # Give Jellyfin a moment to start scanning
        time.sleep(5)

        prev_count = 0
        stable_checks = 0
        elapsed = 5

        while elapsed < max_wait:
            items = self._fetch_all_items(media_type)
            current_count = len(items)

            if current_count == prev_count and current_count > 0:
                stable_checks += 1
                if stable_checks >= 2:
                    logger.info(f"[Jellyfin] Library scan appears complete: {current_count} {media_type} items (stable)")
                    return True
            else:
                stable_checks = 0

            if expected_count > 0 and current_count >= expected_count:
                logger.info(f"[Jellyfin] Library scan complete: {current_count} >= {expected_count} expected {media_type} items")
                return True

            prev_count = current_count
            time.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning(f"[Jellyfin] Library scan wait timed out after {max_wait}s")
        return False

    def get_home_sections(self) -> dict:
        """Read the user's current homesection0-9 display preferences.

        Returns {"homesection0": "resumevideo", ...} ("" for unset slots),
        or {} when the preferences could not be fetched.
        """
        if not self.user_id:
            return {}
        data = self._get("/DisplayPreferences/usersettings", params={"userId": self.user_id, "client": "emby"})
        if not data:
            return {}
        prefs = data.get("CustomPrefs", {}) or {}
        return {f"homesection{i}": prefs.get(f"homesection{i}") or "" for i in range(10)}

    def disable_home_sections(self):
        """Set all Jellyfin home sections to 'none' for the current user.

        This prevents overlap between Tentacle's managed home screen
        and Jellyfin's built-in home sections.

        Returns a snapshot dict of the PREVIOUS homesection values when
        sections were actively disabled (so callers can preserve what the
        user had configured in Jellyfin), {} if they were already disabled,
        or None on failure.
        """
        if not self.user_id:
            return None

        path = "/DisplayPreferences/usersettings"
        params = {"userId": self.user_id, "client": "emby"}
        data = self._get(path, params=params)
        if not data:
            return None

        custom_prefs = data.get("CustomPrefs", {})

        # Check if already all "none"
        already_disabled = all(
            custom_prefs.get(f"homesection{i}") in ("none", "")
            for i in range(10)
        )
        if already_disabled:
            return {}

        # Snapshot the user's configuration before overwriting it
        snapshot = {f"homesection{i}": custom_prefs.get(f"homesection{i}") or "" for i in range(10)}

        # Set all home sections to "none"
        for i in range(10):
            custom_prefs[f"homesection{i}"] = "none"
        data["CustomPrefs"] = custom_prefs

        try:
            r = self.session.post(
                f"{self.url}{path}",
                params=params,
                json=data,
                timeout=15,
            )
            self._check_401(r, path)
            if r.status_code < 400:
                logger.info(f"[Jellyfin] Disabled built-in home sections for user {self.user_id}")
                return snapshot
            logger.warning(f"[Jellyfin] Failed to disable home sections: {r.status_code}")
            return None
        except Exception as e:
            logger.warning(f"[Jellyfin] Failed to disable home sections: {e}")
            return None

    def restore_home_sections(self, snapshot: dict) -> bool:
        """Write saved homesection values back to Jellyfin.

        Only restores when the current values are all 'none'/empty (i.e. the
        blank state Tentacle wrote) — never clobbers settings the user has
        since changed by hand.
        """
        if not self.user_id or not snapshot:
            return False

        path = "/DisplayPreferences/usersettings"
        params = {"userId": self.user_id, "client": "emby"}
        data = self._get(path, params=params)
        if not data:
            return False

        custom_prefs = data.get("CustomPrefs", {}) or {}
        currently_blank = all(
            custom_prefs.get(f"homesection{i}") in ("none", "", None)
            for i in range(10)
        )
        if not currently_blank:
            logger.info(f"[Jellyfin] Home sections were changed manually for user {self.user_id} — not restoring snapshot")
            return False

        for i in range(10):
            custom_prefs[f"homesection{i}"] = snapshot.get(f"homesection{i}") or ""
        data["CustomPrefs"] = custom_prefs

        try:
            r = self.session.post(
                f"{self.url}{path}",
                params=params,
                json=data,
                timeout=15,
            )
            self._check_401(r, path)
            if r.status_code < 400:
                logger.info(f"[Jellyfin] Restored built-in home sections for user {self.user_id}")
                return True
            logger.warning(f"[Jellyfin] Failed to restore home sections: {r.status_code}")
            return False
        except Exception as e:
            logger.warning(f"[Jellyfin] Failed to restore home sections: {e}")
            return False


# The orphan sweep may never remove more than this share of the download rows
# in one night (floor SWEEP_MIN_ALLOWANCE). Genuine orphans trickle in by the
# dozen; "every downloaded title vanished overnight" is a failed Jellyfin read
# of some kind, whatever the completeness flag says (#27).
SWEEP_MAX_FRACTION = 0.05
SWEEP_MIN_ALLOWANCE = 50


def _path_tail(path, parts: int):
    """The last `parts` components of a path, case-folded ("a/b.mkv"), or None."""
    if not path:
        return None
    pieces = [p for p in str(path).replace("\\", "/").split("/") if p]
    if len(pieces) < parts:
        return None
    return "/".join(pieces[-parts:]).casefold()


def sweep_orphaned_downloads(db) -> int:
    """Remove Tentacle DB records for downloaded content no longer in Jellyfin.

    Fetches all Jellyfin movie/series TMDB IDs, then deletes any Tentacle DB
    records with source='radarr' or 'sonarr' whose tmdb_id is missing from
    Jellyfin. Also cleans up associated DownloadRequest records.

    Only a complete listing is diffed against, an empty listing next to
    existing rows is not treated as deletions, and a removal larger than the
    blast-radius cap is refused and logged (#27).

    Should run BEFORE the per-user playlist rebuild so rebuilt playlists
    won't reference dead items.
    """
    from models.database import Movie, Series, DownloadRequest, get_setting, log_deletion

    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    jf_uid = get_setting(db, "jellyfin_user_id", "")
    if not jf_url or not jf_key:
        return 0

    jf = JellyfinService(jf_url, jf_key, jf_uid)

    # Fetch all TMDB IDs currently in Jellyfin. Only a COMPLETE listing may be
    # diffed against: a page that timed out (common while the library scan the
    # nightly job has just triggered is running) returns a partial or empty
    # list, and every downloaded title missing from it would be deleted.
    movie_items, movies_complete = jf._fetch_all_items_checked("Movie", ids_only=True)
    series_items, series_complete = jf._fetch_all_items_checked("Series", ids_only=True)
    if not (movies_complete and series_complete):
        logger.warning(
            "[Orphan sweep] Jellyfin item listing was incomplete "
            f"(movies={'ok' if movies_complete else 'partial'}, series={'ok' if series_complete else 'partial'}) "
            "— skipping, nothing removed. It will run again on the next sync.")
        return 0

    jf_movie_ids = set()
    for item in movie_items:
        tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
        if tmdb_id:
            try:
                jf_movie_ids.add(int(tmdb_id))
            except (ValueError, TypeError):
                pass

    jf_series_ids = set()
    for item in series_items:
        tmdb_id = item.get("ProviderIds", {}).get("Tmdb")
        if tmdb_id:
            try:
                jf_series_ids.add(int(tmdb_id))
            except (ValueError, TypeError):
                pass

    orphans_removed = 0
    swept_titles = []

    # A complete-but-empty answer next to existing download rows means the
    # library is unavailable (e.g. mid-rebuild), not that everything was deleted.
    radarr_movies = db.query(Movie).filter(Movie.source == "radarr").all()
    sonarr_series = db.query(Series).filter(Series.source == "sonarr").all()
    total_rows = len(radarr_movies) + len(sonarr_series)
    if radarr_movies and not jf_movie_ids:
        logger.warning(f"[Orphan sweep] Jellyfin listed no movies at all while {len(radarr_movies)} "
                       "downloaded movies are recorded — not treating that as deletions")
        radarr_movies = []
    if sonarr_series and not jf_series_ids:
        logger.warning(f"[Orphan sweep] Jellyfin listed no series at all while {len(sonarr_series)} "
                       "downloaded series are recorded — not treating that as deletions")
        sonarr_series = []

    # Radarr/Sonarr and Jellyfin can match the same folder to different TMDB
    # entries (or Jellyfin to none): a row whose file (movies: folder + file
    # name, so a VOD .strm of the same film doesn't count) or series folder
    # Jellyfin still lists is not an orphan. Matching by id alone swept such
    # rows, with their download requests, and the next scan re-imported them,
    # night after night (#261). The paths differ per container (/data/... in
    # the *arr, /media/... here), hence the tail.
    jf_movie_tails = {_path_tail(i.get("Path"), 2) for i in movie_items} - {None}
    jf_series_tails = {_path_tail(i.get("Path"), 1) for i in series_items} - {None}

    def _still_listed(row_tmdb, ids, tail, tails, title):
        if row_tmdb in ids:
            return True
        if tail and tail in tails:
            logger.info(f"[Orphan sweep] Keeping '{title}': Jellyfin lists its files under another "
                        f"TMDB id than tmdb:{row_tmdb} (a different match in Jellyfin or the *arr)")
            return True
        return False

    orphan_movies = [m for m in radarr_movies
                     if not _still_listed(m.tmdb_id, jf_movie_ids, _path_tail(m.radarr_path, 2),
                                          jf_movie_tails, m.title)]
    orphan_series = [s for s in sonarr_series
                     if not _still_listed(s.tmdb_id, jf_series_ids, _path_tail(s.sonarr_path, 1),
                                          jf_series_tails, s.title)]

    allowance = max(SWEEP_MIN_ALLOWANCE, int(total_rows * SWEEP_MAX_FRACTION))
    candidates = len(orphan_movies) + len(orphan_series)
    if candidates > allowance:
        logger.error(
            f"[Orphan sweep] REFUSING to remove {candidates} download record(s): that exceeds the "
            f"safety limit of {allowance} ({int(SWEEP_MAX_FRACTION * 100)}% of {total_rows} rows). "
            "This looks like Jellyfin failing to list its library rather than that many titles "
            "being deleted — nothing was removed. If the removal is genuine, delete the titles "
            "from the Library page.")
        log_deletion(db, kind="orphan-sweep-blocked", name=f"{candidates} download record(s)",
                     reason="safety-limit",
                     detail=f"{candidates} downloaded titles were missing from Jellyfin's listing but "
                            f"exceed the {allowance}-row limit; nothing deleted")
        return 0

    for movie in orphan_movies:
        logger.info(f"[Orphan sweep] Removing orphaned radarr movie: {movie.title} (tmdb:{movie.tmdb_id})")
        swept_titles.append(movie.title)
        db.query(DownloadRequest).filter(
            DownloadRequest.tmdb_id == movie.tmdb_id,
            DownloadRequest.media_type == "movie",
        ).delete()
        db.delete(movie)
        orphans_removed += 1

    for series in orphan_series:
        logger.info(f"[Orphan sweep] Removing orphaned sonarr series: {series.title} (tmdb:{series.tmdb_id})")
        swept_titles.append(series.title)
        db.query(DownloadRequest).filter(
            DownloadRequest.tmdb_id == series.tmdb_id,
            DownloadRequest.media_type == "series",
        ).delete()
        db.delete(series)
        orphans_removed += 1

    if orphans_removed:
        db.commit()
        log_deletion(db, kind="orphan-sweep", name=f"{orphans_removed} download record(s)", reason="auto",
                     detail="DB records removed — no longer present in Jellyfin: " + ", ".join(swept_titles[:20])
                            + ("…" if len(swept_titles) > 20 else ""))
        logger.info(f"[Orphan sweep] Removed {orphans_removed} orphaned download(s)")

    return orphans_removed


# The tag push writes in batches with a pause between them, always: bulk
# imports age out of "Recently Added" together, so thousands of titles leave
# the window on the same night (#180).
TAG_PUSH_BATCH = 200
TAG_PUSH_PAUSE_SECONDS = 1.0


def sync_owned_tags(db, jf, log_prefix: str = "Pipeline") -> dict:
    """Bring every row's Tentacle tags on its Jellyfin item in line with the DB.

    Rules (#180):
    - every row counts, a row with no tags too (empty = none of Tentacle's);
    - Tentacle's own tags not on the row come off, and every other tag stays;
    - but only where the TMDB id is exactly ONE Jellyfin item of that type:
      one row can be two items (a provider .strm next to a downloaded file),
      and a removal there took "Downloaded Movies" off the download. Such an
      item, and one found by title rather than TMDB id, is only ever added to;
    - the listing only picks candidates; each write is computed from a fresh
      GET of the item, and nothing is written when that GET fails;
    - an item that needs nothing is not written, so a second run writes 0.

    Returns counts: written, unchanged, not_found, errors."""
    import time
    from models.database import Movie, Series
    from services.tagger import merge_owned_tags, tentacle_owned_tags
    owned = tentacle_owned_tags(db)
    counts = {"written": 0, "unchanged": 0, "not_found": 0, "errors": 0}
    for media_type, model in (("Movie", Movie), ("Series", Series)):
        lookup, title_lookup, per_id = jf.get_tmdb_lookup_with_fallback(media_type, with_counts=True)
        for row in db.query(model).all():
            try:
                jf_item = lookup.get(row.tmdb_id)
                by_title = False
                if not jf_item and row.title:
                    norm = JellyfinService._normalize_title(row.title)
                    jf_item = title_lookup.get((norm, str(row.year or ""))) or title_lookup.get((norm, ""))
                    by_title = True
                if not jf_item:
                    counts["not_found"] += 1
                    continue
                desired = list(row.tags or [])
                add_only = by_title or per_id.get(row.tmdb_id, 0) > 1
                listed = list(jf_item.get("Tags") or [])
                wanted = (listed + [t for t in desired if t not in listed]) if add_only \
                    else merge_owned_tags(listed, desired, owned)
                if sorted(wanted) == sorted(listed):
                    counts["unchanged"] += 1
                    continue
                result = jf.set_item_owned_tags(jf_item["Id"], desired, owned, add_only=add_only)
                if result == "written":
                    counts["written"] += 1
                    if counts["written"] % TAG_PUSH_BATCH == 0:
                        time.sleep(TAG_PUSH_PAUSE_SECONDS)
                elif result == "unchanged":
                    counts["unchanged"] += 1
                else:
                    counts["errors"] += 1
            except Exception as e:
                counts["errors"] += 1
                logger.debug(f"[{log_prefix}] Tag push failed for '{row.title}': {e}")
    return counts


def _retry_pending_rating_restores(jf, log_prefix: str = "Pipeline") -> None:
    """Finish any season/episode rating restore an earlier push left undone."""
    try:
        if _pending_restores_load():
            left = jf.retry_pending_rating_restores()
            if left:
                logger.warning(f"[{log_prefix}] {left} series still have season/episode ratings "
                               f"to restore after Jellyfin's rating cascade; retrying next time")
    except Exception as e:
        logger.warning(f"[{log_prefix}] Retrying pending rating restores failed: {e}")


def push_tags_to_jellyfin(db, log_prefix: str = "Pipeline") -> int:
    """Push tags from Tentacle DB to Jellyfin for all movies and series.

    Shared helper used by VOD sync and the nightly sync (Refresh Tags calls
    sync_owned_tags directly). Returns the number of items written.
    """
    from models.database import get_setting

    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    jf_uid = get_setting(db, "jellyfin_user_id", "")
    if not jf_url or not jf_key:
        logger.info(f"[{log_prefix}] Jellyfin not configured — skipping tag push")
        return 0

    jf = JellyfinService(jf_url, jf_key, jf_uid)
    _retry_pending_rating_restores(jf, log_prefix)
    counts = sync_owned_tags(db, jf, log_prefix)
    logger.info(f"[{log_prefix}] Pushed tags to Jellyfin for {counts['written']} items "
                f"({counts['unchanged']} already right, {counts['errors']} failed)")
    return counts["written"]


def run_full_jellyfin_pipeline(db, log_prefix: str = "Pipeline", refresh_playlists: bool = True) -> dict:
    """Run the complete Jellyfin integration pipeline after content changes.

    1. Trigger Jellyfin library scan (so new .strm files are indexed)
    2. Wait for scan to complete
    3. Push tags via API
    4. Refresh SmartList playlists (skipped with refresh_playlists=False, for
       callers that run the per-user playlist pass themselves — refreshing
       every playlist twice in one job doubles the Jellyfin write traffic and,
       on any playlist that rebuilds, clears and re-adds it a second time)
    5. Write home config

    Returns stats dict.
    """
    from models.database import get_setting, set_setting, log_activity
    from datetime import datetime

    stats = {"library_scan": False, "tags_pushed": 0, "playlists_refreshed": False}

    jf_url = get_setting(db, "jellyfin_url")
    jf_key = get_setting(db, "jellyfin_api_key")
    jf_uid = get_setting(db, "jellyfin_user_id", "")

    if not jf_url or not jf_key:
        logger.info(f"[{log_prefix}] Jellyfin not configured — skipping pipeline")
        return stats

    # Step 1+2: Library scan + wait
    jf = JellyfinService(jf_url, jf_key, jf_uid)
    try:
        stats["library_scan"] = jf.wait_for_library_scan(max_wait=120)
    except Exception as e:
        logger.warning(f"[{log_prefix}] Library scan failed: {e}")

    # Step 3: Push tags
    try:
        from services.tagger import refresh_recently_added_tags
        refresh_recently_added_tags(db)

        tagged = push_tags_to_jellyfin(db, log_prefix)
        stats["tags_pushed"] = tagged
        if tagged > 0:
            set_setting(db, "last_jellyfin_push", datetime.utcnow().isoformat())
            log_activity(db, "jellyfin_push", f"Pushed tags to Jellyfin — {tagged} items updated")
    except Exception as e:
        logger.error(f"[{log_prefix}] Tag push failed: {e}")

    # Step 4: Refresh playlist contents only (don't rebuild configs or home layout)
    if not refresh_playlists:
        logger.info(f"[{log_prefix}] Playlist refresh left to the caller")
        return stats
    try:
        from services.smartlists import refresh_smartlist_playlists
        refresh_smartlist_playlists(db)
        stats["playlists_refreshed"] = True
        logger.info(f"[{log_prefix}] Playlist contents refreshed")
    except Exception as e:
        logger.warning(f"[{log_prefix}] Playlist refresh failed: {e}")

    return stats
