"""The one way Tentacle asks Radarr, Sonarr and Lidarr for something.

Every entry point says only WHAT it wants: Tentacle's Discover and Library
pages, a list's auto-add and "add missing", the Jellyfin plugin on the web and
on Android TV (which reach Tentacle through /api/lists/add-to-*), and anyone
calling that API directly. This module decides HOW. The quality profile and
root folder come from settings; for albums, also the metadata profile and the
original-release pin (services/music/original.py).

The one exception is an explicit choice the user made for this one request,
passed as `quality_profile_override`. It is logged. The older
`quality_profile_id` field is ignored, because every client that sent it
filled it in with the first profile in the *arr's list whether or not the user
touched the picker, and that profile is usually "Any".

There is no fallback profile. Profile 1 is how a movie once arrived as a disc
image, so with no default configured the request is refused with a message
saying where to pick one.
"""
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Optional

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.database import DownloadRequest, Series, get_setting
from services import arr_add
from services.arr_add import ADDED, EXISTS, FAILED, AddReport

logger = logging.getLogger(__name__)

_NAMES = {"radarr": "Radarr", "sonarr": "Sonarr"}


class RequestRefused(Exception):
    """Nothing was sent to the *arr. `message` is written for an admin (and the
    log); `user_message`, when set, for anyone else: the fix is in Settings,
    which only an admin can open."""

    def __init__(self, message: str, status: int = 400, user_message: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.user_message = user_message

    def for_user(self, user) -> str:
        """The text to show this user (a TentacleUser, or None)."""
        if self.user_message and user is not None and not user.is_admin:
            return self.user_message
        return self.message


@dataclass
class RequestOutcome:
    report: AddReport = field(default_factory=AddReport)
    added: list = field(default_factory=list)  # ids that were added
    release_date: Optional[str] = None

    def as_response(self) -> dict:
        resp = self.report.as_response()
        if self.release_date:
            resp["release_date"] = self.release_date
        return resp


def no_default_message(service: str) -> str:
    name = _NAMES[service]
    return (f"Pick a default {name} quality profile in Tentacle's settings (Settings → Connections). "
            f"Tentacle won't guess one: {name}'s first profile is usually \"Any\".")


def no_default_user_message(service: str) -> str:
    name = _NAMES[service]
    return (f"Requests aren't set up yet: an admin has to pick a default {name} quality profile "
            f"in Tentacle before anything can be added.")


def default_quality_profile(db: Session, service: str) -> Optional[int]:
    """The configured default profile id, or None when none is picked."""
    raw = (get_setting(db, f"{service}_quality_profile_id") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _connection(db: Session, service: str) -> tuple:
    url = get_setting(db, f"{service}_url")
    key = get_setting(db, f"{service}_api_key")
    if not url or not key:
        raise RequestRefused(f"{_NAMES[service]} not configured")
    return url.rstrip("/"), key


def read_quality_profiles(url: str, key: str) -> Optional[dict]:
    """{id: name} from the *arr, or None when it can't be read (or is empty).

    Only used to check the chosen profile still exists and to name it in the
    log, so a failed read is not fatal: the *arr validates the id itself.
    """
    try:
        r = requests.get(f"{url.rstrip('/')}/api/v3/qualityprofile",
                         headers={"X-Api-Key": key}, timeout=arr_add.READ_TIMEOUT)
        r.raise_for_status()
        profiles = {p["id"]: p.get("name") or f"id {p['id']}"
                    for p in r.json() if isinstance(p, dict) and isinstance(p.get("id"), int)}
        return profiles or None
    except Exception as e:
        logger.debug(f"Could not read quality profiles from {url}: {e}")
        return None


def _choose_profile(db: Session, service: str, url: str, key: str,
                    override: Optional[int], legacy: Optional[int], via: str) -> tuple:
    """(profile_id, label for the log). Raises RequestRefused."""
    name = _NAMES[service]
    if legacy is not None and override is None:
        logger.info(f"[Request] Ignored quality_profile_id={legacy} sent via {via}: older clients "
                    f"fill it in without the user choosing. Using the default instead.")
    default = default_quality_profile(db, service)
    if override is None and default is None:
        raise RequestRefused(no_default_message(service), user_message=no_default_user_message(service))
    chosen = override if override is not None else default
    names = read_quality_profiles(url, key)
    if names is not None and chosen not in names:
        if override is not None:
            raise RequestRefused(f"The quality profile you picked (id {chosen}) no longer exists in {name}. "
                                 f"Pick another.")
        raise RequestRefused(f"Your default {name} quality profile (id {chosen}) no longer exists in {name}. "
                             f"Pick another in Tentacle's settings (Settings → Connections).",
                             user_message=f"Requests aren't working right now: the default {name} quality "
                                          f"profile no longer exists in {name}, and an admin has to pick "
                                          f"another in Tentacle.")
    label = (names or {}).get(chosen, f"id {chosen}")
    return chosen, f"{label} ({'chosen for this request' if override is not None else 'default'})"


def record_download_request(db: Session, tmdb_id: int, media_type: str, user_id: int):
    """Record who requested a download so the scan can attribute it."""
    existing = db.query(DownloadRequest).filter(
        DownloadRequest.tmdb_id == tmdb_id,
        DownloadRequest.media_type == media_type,
    ).first()
    if not existing:
        db.add(DownloadRequest(tmdb_id=tmdb_id, media_type=media_type, user_id=user_id))
        db.commit()


def _bust_discover_cache():
    # New *arr entries should badge as "requested" in Discover immediately.
    from routers.discover import bust_arr_ids_cache
    bust_arr_ids_cache()


# ── Movies ────────────────────────────────────────────────────────────────

_NO_ROOT_USER = "{name} isn't ready for requests yet (it has no root folder): an admin has to add one in {name}."


def _radarr_root(db: Session, url: str, key: str) -> tuple:
    configured = (get_setting(db, "radarr_root_folder") or "").strip()
    if configured:
        return configured, "default"
    try:
        return arr_add.radarr_root_folder(url, key), "automatic"
    except RuntimeError as e:  # Radarr answered, but has no root folders
        raise RequestRefused(str(e), 503, user_message=_NO_ROOT_USER.format(name="Radarr"))
    except Exception as e:
        logger.warning(f"Failed to fetch Radarr root folders: {e}")
        raise RequestRefused("Could not read Radarr's root folders (Radarr may be busy or down). "
                             "Nothing was added — please retry in a moment.", 503)


def radarr_upcoming_release(url: str, key: str, tmdb_id: int) -> Optional[str]:
    """Earliest future release date Radarr knows for a movie, for UI feedback."""
    try:
        r = requests.get(f"{url}/api/v3/movie", headers={"X-Api-Key": key},
                         params={"tmdbId": tmdb_id}, timeout=arr_add.READ_TIMEOUT)
        r.raise_for_status()
        movies = r.json()
        movie = next((m for m in movies if m.get("tmdbId") == tmdb_id), None) if isinstance(movies, list) else None
        if not movie:
            return None
        now = datetime.utcnow()
        for fld in ("digitalRelease", "physicalRelease", "inCinemas"):
            val = movie.get(fld)
            if not val:
                continue
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, TypeError):
                continue
            if dt > now:
                return val[:10]
    except Exception as e:
        logger.debug(f"Could not read release date for tmdb:{tmdb_id}: {e}")
    return None


def request_movies(db: Session, tmdb_ids: Iterable[int], *, user_id: Optional[int], via: str,
                   quality_profile_override: Optional[int] = None,
                   legacy_profile_id: Optional[int] = None,
                   already_owned: Optional[Callable[[int], bool]] = None,
                   want_release_date: bool = False) -> RequestOutcome:
    """Ask Radarr for these movies with the configured profile and root folder.

    `already_owned(tmdb_id)` lets the entry point say which titles it counts as
    had (reported as already_exists, nothing sent). Raises RequestRefused
    before anything is sent when the request can't be made as configured.
    """
    url, key = _connection(db, "radarr")
    profile_id, profile_label = _choose_profile(
        db, "radarr", url, key, quality_profile_override, legacy_profile_id, via)
    root, root_how = _radarr_root(db, url, key)

    out = RequestOutcome()
    for tmdb_id in tmdb_ids:
        if already_owned and already_owned(tmdb_id):
            out.report.record(EXISTS)
            continue
        logger.info(f"[Request] movie tmdb:{tmdb_id} via {via}: Radarr profile {profile_label}, "
                    f"root folder {root} ({root_how})")
        outcome, reason = arr_add.add_movie_to_radarr(url, key, tmdb_id, profile_id, root)
        out.report.record(outcome, reason)
        if outcome != ADDED:
            continue
        out.added.append(tmdb_id)
        if user_id is not None:
            record_download_request(db, tmdb_id, "movie", user_id)
        # An extra round trip purely for UI feedback: only for a single-title add.
        if want_release_date and len(out.added) == 1:
            out.release_date = radarr_upcoming_release(url, key, tmdb_id)
    if out.added:
        _bust_discover_cache()
    return out


# ── Series ────────────────────────────────────────────────────────────────

def _sonarr_root_folders(sonarr) -> list:
    try:
        folders = sonarr.get_root_folders(required=True)
    except Exception as e:
        logger.warning(f"Failed to fetch Sonarr root folders: {e}")
        raise RequestRefused("Could not read Sonarr's root folders (Sonarr may be busy or down). "
                             "Nothing was added — please retry in a moment.", 503)
    if not folders:
        raise RequestRefused("Sonarr has no root folders configured. Add one in Sonarr → Settings → "
                             "Media Management, then retry.", 503,
                             user_message=_NO_ROOT_USER.format(name="Sonarr"))
    return folders


def _tmdb_service(db: Session):
    from services.tmdb import TMDBService, get_tmdb_token
    token = get_tmdb_token(db)
    if not token:
        return None
    return TMDBService(bearer_token=token, cache_dir=get_setting(db, "data_dir", "/data"))


_NO_VOD_ROOT = (
    "Sonarr has no VOD root folder. Add the folder containing your VOD "
    "series (the same host folder Tentacle writes .strm shows into) as a "
    "Root Folder in Sonarr → Settings → Media Management, then retry — or "
    "switch to the shared-library layout in Tentacle Settings → Integrations. "
    "Without one of these, downloads would create a duplicate series in Jellyfin.")


def _series_added(db: Session, out: RequestOutcome, sonarr, added_id: int, request_id: int,
                  user_id: Optional[int]):
    """Bookkeeping for a series that is now in Sonarr. When its picked
    episodes could not be applied (sonarr.last_error), the request still
    reads as failed, with that reason."""
    if sonarr.last_error:
        out.report.record(FAILED, sonarr.last_error)
    else:
        out.report.record(ADDED)
    out.added.append(added_id)
    if user_id is not None:
        record_download_request(db, request_id, "series", user_id)


def request_series(db: Session, *, tmdb_ids: Iterable[int] = (), tvdb_ids: Iterable[int] = (),
                   user_id: Optional[int], via: str,
                   quality_profile_override: Optional[int] = None,
                   legacy_profile_id: Optional[int] = None,
                   monitor: str = "all", season_folder: bool = True,
                   selected_episodes: Optional[list] = None, monitor_new: bool = False,
                   already_owned: Optional[Callable[[int], bool]] = None) -> RequestOutcome:
    """Ask Sonarr for these series with the configured profile and root folder.

    Also decides where a hybrid series goes: a VOD (.strm) series that gains
    downloaded episodes must end up as ONE Jellyfin series (see the
    hybrid_series_layout setting).
    """
    from services.sonarr import SonarrService

    url, key = _connection(db, "sonarr")
    sonarr = SonarrService(url, key)
    profile_id, profile_label = _choose_profile(
        db, "sonarr", url, key, quality_profile_override, legacy_profile_id, via)

    configured_root = (get_setting(db, "sonarr_root_folder") or "").strip()
    folders = None if configured_root else _sonarr_root_folders(sonarr)
    if configured_root:
        root, root_how = configured_root, "default"
    else:
        # Regular adds never default into the VOD root folder (hybrid series only).
        non_vod = [rf for rf in folders if "vod" not in rf["path"].lower()]
        root, root_how = (non_vod or folders)[0]["path"], "automatic"

    out = RequestOutcome()
    tmdb = None
    for tmdb_id in tmdb_ids:
        if already_owned and already_owned(tmdb_id):
            out.report.record(EXISTS)
            continue
        existing = db.query(Series).filter(Series.tmdb_id == tmdb_id).first()

        # Hybrid VOD series: unify VOD (.strm) + downloaded episodes as ONE
        # Jellyfin series. Two layouts (hybrid_series_layout setting):
        #   vod_root       — Sonarr downloads INTO the VOD folder (needs the VOD
        #                    root folder registered in Sonarr)
        #   shared_library — Sonarr downloads to its own root but with the SAME
        #                    folder name as the VOD show; Jellyfin merges the
        #                    same-named folders (both in one Jellyfin library)
        series_path = None
        is_hybrid = bool(existing and existing.strm_path and (existing.source or "").startswith("provider_"))
        if is_hybrid:
            folder_name = os.path.basename(existing.strm_path.rstrip("/"))
            layout = get_setting(db, "hybrid_series_layout", "vod_root")
            if layout == "shared_library":
                # Same folder name under Sonarr's regular root — Jellyfin's
                # cross-folder merge keys on matching series folder names
                series_path = f"{root.rstrip('/')}/{folder_name}"
            else:
                if folders is None:
                    try:
                        folders = _sonarr_root_folders(sonarr)
                    except RequestRefused as e:
                        out.report.record(FAILED, e.message)
                        continue
                vod_root = next((rf["path"].rstrip("/") for rf in folders if "vod" in rf["path"].lower()), None)
                if vod_root:
                    series_path = f"{vod_root}/{folder_name}"
                elif not existing.sonarr_path:
                    # vod_root layout but no VOD root folder in Sonarr: downloading
                    # to the regular root would show the series TWICE in Jellyfin.
                    out.report.record(FAILED, _NO_VOD_ROOT)
                    logger.warning(f"Blocked hybrid add for tmdb:{tmdb_id} — no VOD root folder in Sonarr")
                    continue

        # Resolve the exact TVDB id (Sonarr's native key) so add_series doesn't
        # have to rely on Skyhook's unreliable tmdb: term lookup
        if tmdb is None:
            tmdb = _tmdb_service(db) or False
        tvdb_id = tmdb.get_tvdb_id(tmdb_id) if tmdb else None
        logger.info(f"[Request] series tmdb:{tmdb_id} via {via}: Sonarr profile {profile_label}, "
                    f"{'folder ' + series_path + ' (hybrid)' if series_path else f'root folder {root} ({root_how})'}")
        result = sonarr.add_series(tmdb_id, profile_id, root, monitor=monitor, season_folder=season_folder,
                                   selected_episodes=selected_episodes, series_path=series_path,
                                   monitor_new=monitor_new, tvdb_id=tvdb_id)
        if result and result.get("alreadyExists"):
            # Sonarr already has it — the outcome the user wanted, not a failure.
            out.report.record(EXISTS)
        elif result:
            _series_added(db, out, sonarr, tmdb_id, tmdb_id, user_id)
            # Mark VOD series with sonarr_path so scan skips duplicate detection
            if existing and (existing.source or "").startswith("provider_") and result.get("path"):
                existing.sonarr_path = result["path"]
                db.commit()
        else:
            out.report.record(FAILED, sonarr.last_error)

    # TheTVDB-only content (no TMDB entry)
    for tvdb_id in tvdb_ids:
        logger.info(f"[Request] series tvdb:{tvdb_id} via {via}: Sonarr profile {profile_label}, "
                    f"root folder {root} ({root_how})")
        result = sonarr.add_series(tvdb_id=tvdb_id, quality_profile_id=profile_id, root_folder=root,
                                   monitor=monitor, season_folder=season_folder,
                                   selected_episodes=selected_episodes, monitor_new=monitor_new)
        if result and result.get("alreadyExists"):
            out.report.record(EXISTS)
        elif result:
            # Sonarr's tmdbId when it has one, else the negative tvdb id
            _series_added(db, out, sonarr, -tvdb_id, result.get("tmdbId") or -tvdb_id, user_id)
        else:
            out.report.record(FAILED, sonarr.last_error)

    if out.added:
        _bust_discover_cache()
    return out


# ── Albums ────────────────────────────────────────────────────────────────

def _lidarr_defaults(db: Session) -> dict:
    """Root folder and profiles for new artists, from settings. Never guessed."""
    root = (get_setting(db, "lidarr_root_folder") or "").strip()
    quality = (get_setting(db, "lidarr_quality_profile_id") or "").strip()
    metadata = (get_setting(db, "lidarr_metadata_profile_id") or "").strip()
    missing = [name for name, value in (("root folder", root), ("quality profile", quality),
                                        ("metadata profile", metadata)) if not value]
    if missing:
        raise RequestRefused(f"Pick a Lidarr {', '.join(missing)} in Tentacle's settings (Settings → Connections).",
                             user_message=f"Album requests aren't set up yet: an admin has to pick a Lidarr "
                                          f"{', '.join(missing)} in Tentacle.")
    return {"root": root, "quality": int(quality), "metadata": int(metadata)}


_LANDS_LATER = "If Lidarr adds the album after all, Tentacle pins the original and searches for it."


def _added_after_all(client, rgid: str) -> Optional[dict]:
    """The album, if an add whose answer failed landed in Lidarr anyway."""
    from services.lidarr import LidarrError
    try:
        return client.album_by_mbid(rgid)
    except LidarrError:
        return None


def _mark_requested(db: Session, library, album: dict, user_id: Optional[int], choice: Optional[dict]):
    row = library.upsert_album(db, album)
    row.monitored = True
    row.requested_by = user_id
    row.requested_at = datetime.utcnow()
    row.category = ""
    row.verdict = {"state": "Requested: pinning the original release…"}
    # Owed until finish_request has pinned and searched; picked up again after a
    # restart and by the daily check (jobs.finish_pending_requests).
    row.request_pending, row.request_choice = True, choice
    db.commit()
    return row


def request_album(db: Session, rgid: str, *, user_id: Optional[int], via: str,
                  choice: Optional[dict] = None) -> dict:
    """Ask Lidarr for one album (a MusicBrainz release group), pinned to its original.

    Adds the artist if Lidarr doesn't have it, with none of its other albums
    monitored (monitor none, new items none, root folder and profiles from
    settings); monitors only this album; then, in the background, pins the
    original release and starts the search. `choice` is the user's explicit
    pick of a tracklist for an album whose original is ambiguous.
    """
    from services.lidarr import LidarrError
    from services.music import jobs, library, settings as music_settings, worker
    from services.musicbrainz import is_mbid

    if not music_settings.is_enabled(db):
        raise RequestRefused("The music module is off (Settings → Music).")
    if not is_mbid(rgid):
        raise RequestRefused("That isn't a MusicBrainz album id.")
    try:
        client = library.lidarr_client(db)
    except library.MusicUnavailable as e:
        raise RequestRefused(e.message)
    defaults = _lidarr_defaults(db)
    try:
        album = client.album_by_mbid(rgid)
        added = album is None
        if added:
            found = client.lookup_album(rgid)
            if not found:
                raise RequestRefused("Lidarr's metadata server doesn't know this album yet. Try again later.")
            artist = dict(found.get("artist") or {})
            artist.update({
                "qualityProfileId": defaults["quality"], "metadataProfileId": defaults["metadata"],
                "rootFolderPath": defaults["root"], "monitored": True, "monitorNewItems": "none",
                # "None" alone would unmonitor this album too on Lidarr's first scan
                # of a new artist; albumsToMonitor keeps exactly this one.
                "addOptions": {"monitor": "none", "albumsToMonitor": [rgid], "searchForMissingAlbums": False},
            })
            resource = dict(found, artist=artist, monitored=True, anyReleaseOk=False,
                            addOptions={"searchForNewAlbum": False})
            logger.info(f"[Request] album {rgid} via {via}: adding to Lidarr (quality profile "
                        f"{defaults['quality']}, metadata profile {defaults['metadata']}, root {defaults['root']})")
            try:
                album = client.add_album(resource) or client.album_by_mbid(rgid)
            except LidarrError as e:
                # The add can land although its answer doesn't: a new artist's metadata
                # takes Lidarr longer than the timeout, or Lidarr restarts mid-add.
                album = _added_after_all(client, rgid)
                if not album:
                    if e.status and 400 <= e.status < 500:
                        raise   # Lidarr refused it: nothing was added
                    # Not listed yet, but the add may still commit (#431): owed for a while.
                    jobs.owe_add(db, rgid, found, user_id, choice)
                    logger.info(f"[Request] album {rgid} via {via}: Lidarr's answer failed ({e.message}) "
                                "and it doesn't list the album yet; will look for it again")
                    why = e.message if e.message.endswith((".", "?")) else e.message + "."
                    raise RequestRefused(f"{why} {_LANDS_LATER}", 502)
                logger.info(f"[Request] album {rgid} via {via}: Lidarr's answer failed ({e.message}), "
                            "but the album is in Lidarr: carrying on")
            if not album or not album.get("id"):
                jobs.owe_add(db, rgid, found, user_id, choice)
                raise RequestRefused(f"Lidarr accepted the album but doesn't list it yet. {_LANDS_LATER}")
        elif not album.get("monitored"):
            client.set_monitored([album["id"]], True)
            logger.info(f"[Request] album {rgid} via {via}: already in Lidarr, now monitored")
    except LidarrError as e:
        raise RequestRefused(e.message, 502)

    try:
        row = _mark_requested(db, library, album, user_id, choice)
    except IntegrityError:
        # The same album requested twice at once (a double click, the dashboard and
        # Jellyfin, two users): the other request wrote its row first. Use that row.
        db.rollback()
        row = _mark_requested(db, library, album, user_id, choice)
    worker.submit(jobs.finish_request(album["id"], rgid, choice), worker.URGENT, f"request {album.get('title')}")
    return {"status": "requested", "added_to_lidarr": added, "title": album.get("title"),
            "artist": (album.get("artist") or {}).get("artistName") or row.artist_name}
