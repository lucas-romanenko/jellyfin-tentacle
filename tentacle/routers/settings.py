"""
Tentacle - Settings Router
Handles all settings API endpoints
"""

import logging
import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Dict
import requests

from models.database import get_db, Setting, get_setting, set_setting
from routers.auth import require_admin, get_user_from_request
from services.music.settings import SECRET_KEYS as MUSIC_SECRET_KEYS
from services.secret_mask import looks_masked

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_admin)])

# Separate router for plugin-facing endpoints. Gated by a valid Jellyfin user token
# (the plugin forwards the caller's token automatically — zero config) rather than a
# manually-configured shared secret.
plugin_router = APIRouter(prefix="/api/settings", tags=["settings"])


@plugin_router.get("/plugin-keys", dependencies=[Depends(get_user_from_request)])
def get_plugin_keys(db: Session = Depends(get_db)):
    """Return API keys needed by the Jellyfin plugin. Requires a valid Jellyfin user
    token (forwarded by the plugin as ?api_key=) — no manual secret setup.
    Only exposes specific keys, not all settings."""
    result = {}
    for key in ["mdblist_api_key", "tmdb_bearer_token", "tmdb_api_key"]:
        val = get_setting(db, key)
        if val:
            result[key] = val
    # Inject built-in TMDB token if not explicitly set
    if not result.get("tmdb_bearer_token") and not result.get("tmdb_api_key"):
        from services.tmdb import TMDB_DEFAULT_TOKEN
        result["tmdb_bearer_token"] = TMDB_DEFAULT_TOKEN
    return result


# Masked by GET /api/settings (and by /raw while no user exists yet); a Save
# that sends back the masked form keeps the stored value.
SENSITIVE_KEYS = {"tmdb_bearer_token", "tmdb_api_key", "radarr_api_key", "sonarr_api_key",
                  "jellyfin_api_key", "trakt_client_id", "mdblist_api_key", "vod_token_secret",
                  "youtube_api_key", "internal_secret", "webhook_secret",
                  "logodev_api_key"} | MUSIC_SECRET_KEYS

# Signing keys: session_secret signs the dashboard's session cookies,
# vod_token_secret the .strm playback tokens. Nothing in the dashboard shows or
# edits them, so no settings route serves them and a Save cannot set them.
NEVER_SERVED = {"session_secret", "vod_token_secret"}


# Passwords and shared secrets show no characters at all; API keys keep
# their last four so the admin can tell which key is saved.
PASSWORD_KEYS = {"navidrome_password", "music_webhook_secret", "internal_secret", "webhook_secret"}


def _shown(result: dict) -> dict:
    """Mask the secrets in a settings listing (the proxy keeps its address)."""
    from services.secret_mask import mask, mask_url_login
    for key in SENSITIVE_KEYS:
        if result.get(key):
            result[key] = mask(result[key], whole=key in PASSWORD_KEYS)
    if result.get("youtube_proxy"):
        result["youtube_proxy"] = mask_url_login(result["youtube_proxy"])
    return result


def _bootstrap(db: Session) -> bool:
    """require_admin lets anyone in while no user exists (first-run setup)."""
    from models.database import TentacleUser
    return db.query(TentacleUser).count() == 0


class SettingsUpdate(BaseModel):
    settings: Dict[str, str]


class ConnectionTest(BaseModel):
    type: str  # tmdb | radarr | jellyfin
    url: Optional[str] = None
    api_key: Optional[str] = None
    bearer_token: Optional[str] = None


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    settings = db.query(Setting).all()
    result = {s.key: s.value for s in settings if s.key not in NEVER_SERVED}
    return _shown(result)


@router.get("/raw")
def get_settings_raw(db: Session = Depends(get_db)):
    """Settings for the dashboard (the Settings page shows the admin's own keys).

    Never the signing keys. While no user exists yet, require_admin lets any
    caller in (first-run setup), so every secret is masked then: setup only
    needs the addresses."""
    settings = db.query(Setting).all()
    result = {s.key: s.value for s in settings if s.key not in NEVER_SERVED}
    # No built-in TMDB token: the page posts every field back on Save, so
    # serving it stored it as the user's own (#383). A copy stored that way
    # shows as unset, so the field shows "Using built-in key" again.
    from services.tmdb import TMDB_DEFAULT_TOKEN
    if result.get("tmdb_bearer_token") == TMDB_DEFAULT_TOKEN:
        result["tmdb_bearer_token"] = ""
    if _bootstrap(db):
        _shown(result)
    return result


@router.post("")
def update_settings(body: SettingsUpdate, db: Session = Depends(get_db)):
    from models.database import Setting
    sensitive_keys = SENSITIVE_KEYS
    from models.database import NON_EMPTY_DEFAULTS
    from services.secret_mask import is_shown_form, restore_url_login
    for key in NEVER_SERVED & set(body.settings):
        _log.warning(f"Settings save: {key} is not settable here; ignored")
        body.settings.pop(key)
    if "youtube_proxy" in body.settings:
        # Checked as the YouTube page checks it: a proxy that can't be used
        # holds every YouTube request (services/youtube/traffic.py, #244).
        # A masked password sent back means the stored one.
        from services.youtube import traffic
        body.settings["youtube_proxy"] = restore_url_login(
            body.settings["youtube_proxy"] or "", get_setting(db, "youtube_proxy", "") or "")
        try:
            body.settings["youtube_proxy"] = traffic.normalize_proxy(body.settings["youtube_proxy"] or "")
        except ValueError as e:
            raise HTTPException(400, str(e))
    if (body.settings.get("sync_schedule") or "").strip():
        # Refused before anything is stored: a value the scheduler can't use
        # answered success and left the old job running only until the next
        # restart (#458). A blank one stores the default, as before.
        from services.sync_schedule import sync_trigger
        try:
            sync_trigger(body.settings["sync_schedule"])
        except ValueError as e:
            raise HTTPException(400, f"Sync schedule '{body.settings['sync_schedule']}' is not a valid cron: {e}")
    if (body.settings.get("recently_added_days") or "").strip():
        # The number field posts "14.5", "7.0" or "1e2" as typed, and every
        # reader's int() raised on it (#536): store the whole days the window
        # uses, refuse what no number can be read from. Blank stores the default.
        from models.database import parse_recently_added_days
        days = parse_recently_added_days(body.settings["recently_added_days"])
        if days is None:
            raise HTTPException(400, f"Recently added days '{body.settings['recently_added_days']}' is not a number")
        body.settings["recently_added_days"] = str(days)
    from services.tmdb import TMDB_DEFAULT_TOKEN
    if (body.settings.get("tmdb_bearer_token") or "").strip() == TMDB_DEFAULT_TOKEN:
        # The built-in token stays a default: storing it would pin this install
        # to it after a release changes it (#383). A stored copy is cleared.
        body.settings["tmdb_bearer_token"] = ""
    for key, value in body.settings.items():
        # A secret sent back exactly as the masked listing showed it is unchanged
        if key in sensitive_keys and is_shown_form(value, get_setting(db, key, "") or ""):
            continue
        if value in ("", None):
            if key in NON_EMPTY_DEFAULTS:
                # A cleared "Recently added days" or match threshold (the
                # field then shows its placeholder, so it looks like the
                # default) was stored as "", and every reader's int()/float()
                # raised: the provider sync, the tag refresh and the playlist
                # build all failed until it was typed back in.
                value = NON_EMPTY_DEFAULTS[key]
            elif db.query(Setting).filter(Setting.key == key).first() is None:
                # Nothing to clear: don't create an empty row for a key that
                # was never set (a Save with nothing edited changed the table).
                continue
        set_setting(db, key, value)

    if "youtube_proxy" in body.settings:
        from services.youtube import traffic
        traffic.configure(proxy=get_setting(db, "youtube_proxy", "") or "")
    # setup_complete is the wizard's own to set (Get Started, Skip everything).
    # Setting it on any save with a Jellyfin address and key ended the wizard
    # at its first step: steps 2-6 were never shown again.

    if "music_reconcile_time" in body.settings:
        try:
            from main import reschedule_music_reconcile
            reschedule_music_reconcile(get_setting(db, "music_reconcile_time"))
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Could not reschedule the music check", exc_info=True)
    # Turning the music module on: fill the library snapshot now, not at the daily check.
    if body.settings.get("music_enabled") == "true" and not get_setting(db, "music_last_reconcile") \
            and get_setting(db, "lidarr_url") and get_setting(db, "lidarr_api_key"):
        try:
            from services.music.jobs import start_reconcile
            start_reconcile("first run")
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Could not start the first music check", exc_info=True)

    # If the sync schedule changed, reschedule the job live (no restart needed).
    if "sync_schedule" in body.settings:
        try:
            from main import reschedule_main_sync
            reschedule_main_sync(get_setting(db, "sync_schedule"))
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Could not reschedule sync after settings save", exc_info=True)

    return {"success": True}


@router.get("/schedule-info")
def schedule_info():
    """Sync schedule as a friendly time + the effective timezone + next run time.
    Used by the Settings UI to show the schedule without exposing raw cron."""
    try:
        from main import get_schedule_info
        return get_schedule_info()
    except Exception:
        return {"cron": "0 3 * * *", "time": "03:00", "timezone": "", "timezone_abbr": "", "next_run_human": None}


def _notify_plugin_discover_changed(db: Session):
    """Notify the Jellyfin plugin to clear its discover config cache."""
    import logging
    jellyfin_url = get_setting(db, "jellyfin_url", "")
    jellyfin_key = get_setting(db, "jellyfin_api_key", "")
    if not jellyfin_url or not jellyfin_key:
        return
    try:
        r = requests.post(
            f"{jellyfin_url.rstrip('/')}/Tentacle/Refresh",
            headers={"X-Emby-Token": jellyfin_key},
            timeout=5,
        )
        if r.ok:
            logging.getLogger(__name__).info("Notified Jellyfin plugin to clear discover cache")
    except Exception:
        pass


@router.post("/initial-scan")
def trigger_initial_scan():
    """Trigger background Radarr/Sonarr scan during setup wizard."""
    _trigger_post_setup_scan()
    return {"success": True}


def _trigger_post_setup_scan():
    """Scan Radarr/Sonarr in the background so the user's existing library is immediately available."""
    import threading
    import logging

    logger = logging.getLogger(__name__)

    def _post_setup_background():
        from models.database import SessionLocal, get_setting, set_setting, log_activity
        from datetime import datetime
        db = SessionLocal()
        try:
            radarr_url = get_setting(db, "radarr_url")
            radarr_key = get_setting(db, "radarr_api_key")
            if radarr_url and radarr_key:
                logger.info("[Post-setup] Auto-scanning Radarr library...")
                from services.radarr import scan_radarr_library
                result = scan_radarr_library(db)
                set_setting(db, "last_radarr_scan", datetime.utcnow().isoformat())
                n = result.get("new", 0) if isinstance(result, dict) else 0
                if n:
                    log_activity(db, "radarr_scan", f"Post-setup Radarr scan — {n} movie{'s' if n != 1 else ''} imported")
                logger.info(f"[Post-setup] Radarr scan complete: {n} new movies")

            sonarr_url = get_setting(db, "sonarr_url")
            sonarr_key = get_setting(db, "sonarr_api_key")
            if sonarr_url and sonarr_key:
                logger.info("[Post-setup] Auto-scanning Sonarr library...")
                from services.sonarr import scan_sonarr_library
                result = scan_sonarr_library(db)
                set_setting(db, "last_sonarr_scan", datetime.utcnow().isoformat())
                n = result.get("new", 0) if isinstance(result, dict) else 0
                if n:
                    log_activity(db, "sonarr_scan", f"Post-setup Sonarr scan — {n} series imported")
                logger.info(f"[Post-setup] Sonarr scan complete: {n} new series")

            # Run full Jellyfin pipeline if anything was scanned
            if (radarr_url and radarr_key) or (sonarr_url and sonarr_key):
                from services.jellyfin import run_full_jellyfin_pipeline
                run_full_jellyfin_pipeline(db, log_prefix="Post-setup")
                logger.info("[Post-setup] Jellyfin pipeline complete")

        except Exception as e:
            logger.error(f"[Post-setup] Auto-scan failed: {e}", exc_info=True)
        finally:
            db.close()

    thread = threading.Thread(target=_post_setup_background, daemon=True)
    thread.start()
    logging.getLogger(__name__).info("[Post-setup] Triggered background Radarr/Sonarr scan after setup wizard")


@router.post("/test")
def test_connection(body: ConnectionTest, db: Session = Depends(get_db)):
    if body.type == "tmdb":
        from services.tmdb import get_tmdb_token
        token = body.bearer_token if (body.bearer_token and not looks_masked(body.bearer_token)) else get_tmdb_token(db)
        if not token:
            raise HTTPException(400, "No TMDB token configured")
        try:
            r = requests.get(
                "https://api.themoviedb.org/3/configuration",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            r.raise_for_status()
            return {"success": True, "message": "TMDB connection successful"}
        except Exception as e:
            raise HTTPException(400, f"TMDB connection failed: {str(e)}")

    elif body.type == "mdblist":
        key = body.api_key if (body.api_key and not looks_masked(body.api_key)) else get_setting(db, "mdblist_api_key")
        if not key:
            raise HTTPException(400, "MDBList API key required")
        try:
            r = requests.get(
                f"https://api.mdblist.com/user?apikey={key}",
                timeout=10
            )
            if r.status_code == 401:
                raise HTTPException(400, "Invalid MDBList API key")
            r.raise_for_status()
            data = r.json()
            name = data.get("name", "Unknown")
            return {"success": True, "message": f"MDBList connected — {name}"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"MDBList connection failed: {str(e)}")

    elif body.type == "radarr":
        url = body.url or get_setting(db, "radarr_url")
        key = body.api_key or get_setting(db, "radarr_api_key")
        if not url or not key:
            raise HTTPException(400, "Radarr URL and API key required")
        try:
            r = requests.get(
                f"{url.rstrip('/')}/api/v3/system/status",
                headers={"X-Api-Key": key},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            return {"success": True, "message": f"Radarr {data.get('version', '')} connected"}
        except Exception as e:
            raise HTTPException(400, f"Radarr connection failed: {str(e)}")

    elif body.type == "sonarr":
        url = body.url or get_setting(db, "sonarr_url")
        key = body.api_key or get_setting(db, "sonarr_api_key")
        if not url or not key:
            raise HTTPException(400, "Sonarr URL and API key required")
        try:
            r = requests.get(
                f"{url.rstrip('/')}/api/v3/system/status",
                headers={"X-Api-Key": key},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            return {"success": True, "message": f"Sonarr {data.get('version', '')} connected"}
        except Exception as e:
            raise HTTPException(400, f"Sonarr connection failed: {str(e)}")

    elif body.type == "jellyfin":
        url = body.url or get_setting(db, "jellyfin_url")
        key = body.api_key or get_setting(db, "jellyfin_api_key")
        if not url or not key:
            raise HTTPException(400, "Jellyfin URL and API key required")
        try:
            r = requests.get(
                f"{url.rstrip('/')}/System/Info",
                headers={"X-Emby-Token": key},
                timeout=10
            )
            if r.status_code == 401:
                raise HTTPException(401, "Invalid API key — generate a new one in Jellyfin Dashboard → API Keys")
            r.raise_for_status()
            data = r.json()
            return {"success": True, "message": f"Jellyfin {data.get('Version', '')} connected"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Jellyfin connection failed: {str(e)}")

    raise HTTPException(400, "Unknown connection type")


class ServiceCheck(BaseModel):
    type: str  # radarr | sonarr | lidarr | navidrome | jellyfin_music | musicbrainz | deezer
    url: Optional[str] = None
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    contact: Optional[str] = None
    library_id: Optional[str] = None
    # Picker values on screen, not saved yet (setting key -> value): the test
    # checks what the user sees.
    picks: Optional[Dict[str, str]] = None


@router.post("/check")
def check_service(body: ServiceCheck, db: Session = Depends(get_db)):
    """The Test button: every step, ok / fail / warn, so the user sees exactly what
    worked. Always 200; `success` says whether any step failed. Unsaved form
    values are tested as typed (a masked secret falls back to the saved one)."""
    from services import service_checks as sc
    picks = body.picks or {}
    if body.type in ("radarr", "sonarr"):
        return sc.check_arr(db, body.type, body.url, body.api_key, picks)
    if body.type == "lidarr":
        return sc.check_lidarr(db, body.url, body.api_key, picks)
    if body.type == "navidrome":
        return sc.check_navidrome(db, body.url, body.username, body.password)
    if body.type == "jellyfin_music":
        return sc.check_jellyfin_music(db, body.library_id, picks)
    if body.type == "musicbrainz":
        return sc.check_musicbrainz(db, body.contact)
    if body.type == "deezer":
        return sc.check_deezer()
    raise HTTPException(400, "Unknown service")


class ServiceOptionsRequest(BaseModel):
    type: str  # radarr | sonarr | lidarr
    url: Optional[str] = None
    api_key: Optional[str] = None


@router.post("/service-options")
def service_options(body: ServiceOptionsRequest, db: Session = Depends(get_db)):
    """What the settings pickers offer, read from the service itself: quality
    profiles and root folders (plus metadata profiles for Lidarr)."""
    from services import service_checks as sc
    if body.type not in ("radarr", "sonarr", "lidarr"):
        raise HTTPException(400, "Unknown service")
    url = sc._pick(body.url, get_setting(db, f"{body.type}_url"))
    key = sc._pick(body.api_key, get_setting(db, f"{body.type}_api_key"))
    if not url or not key:
        raise HTTPException(400, "Enter the URL and API key first")
    try:
        if body.type == "lidarr":
            return sc.lidarr_options(url, key)
        return sc.arr_options(body.type, url, key)
    except Exception as e:
        message = getattr(e, "message", None) or str(e)
        raise HTTPException(502, f"Couldn't read them from {body.type.capitalize()}: {message}")


@router.get("/jellyfin-music/libraries")
def jellyfin_music_libraries(db: Session = Depends(get_db)):
    """Jellyfin's libraries, for the music library picker (music ones first)."""
    from services import service_checks as sc
    try:
        libs = sc.jellyfin_libraries(db)
    except Exception as e:
        raise HTTPException(502, f"Couldn't list Jellyfin's libraries: {e}")
    return sorted(libs, key=lambda l: ((l["collection_type"] or "").lower() != "music", (l["name"] or "").lower()))


class CreateMusicLibrary(BaseModel):
    name: str = "Music"
    path: str


@router.post("/jellyfin-music/create-library")
def create_jellyfin_music_library(body: CreateMusicLibrary, db: Session = Depends(get_db)):
    """Create a Jellyfin music library on a folder (by default, Lidarr's root folder
    as Jellyfin sees it) and select it."""
    from services import service_checks as sc
    name = (body.name or "").strip() or "Music"
    path = (body.path or "").strip()
    if not path.startswith("/") and not (len(path) > 2 and path[1] == ":"):
        raise HTTPException(400, "Enter the folder as Jellyfin sees it, e.g. /data/music")
    try:
        lib = sc.create_jellyfin_music_library(db, name, path)
    except Exception as e:
        raise HTTPException(502, str(e))
    set_setting(db, "jellyfin_music_library_id", lib["id"] or "")
    return lib


@router.get("/paths")
def check_paths():
    """Check which media paths are mounted and accessible."""
    paths = [
        {"key": "data", "path": "/data", "label": "Database & Config", "required": True,
         "mount_example": "./tentacle-data:/data"},
        {"key": "vod_movies", "path": "/media/vod/movies", "label": "VOD Movies",
         "mount_example": "/your/vod-movies:/media/vod/movies"},
        {"key": "vod_shows", "path": "/media/vod/shows", "label": "VOD Shows",
         "mount_example": "/your/vod-shows:/media/vod/shows"},
        {"key": "movies", "path": "/media/movies", "label": "Radarr Movies",
         "mount_example": "/your/movies:/media/movies"},
        {"key": "shows", "path": "/media/shows", "label": "Sonarr TV Shows",
         "mount_example": "/your/shows:/media/shows"},
    ]
    result = {}
    for info in paths:
        p = Path(info["path"])
        mounted = p.exists() and p.is_dir()
        writable = mounted and os.access(str(p), os.W_OK)
        result[info["key"]] = {
            "path": info["path"],
            "label": info["label"],
            "mounted": mounted,
            "writable": writable,
            "mount_example": info["mount_example"],
        }
        if info.get("required"):
            result[info["key"]]["required"] = True
    return result


@router.get("/connection-status")
def connection_status(db: Session = Depends(get_db)):
    """Test all configured service connections in parallel."""
    import concurrent.futures

    # Read all settings upfront (SQLite safety — don't share session across threads)
    jf_url = get_setting(db, "jellyfin_url", "")
    jf_key = get_setting(db, "jellyfin_api_key", "")
    radarr_url = get_setting(db, "radarr_url", "")
    radarr_key = get_setting(db, "radarr_api_key", "")
    sonarr_url = get_setting(db, "sonarr_url", "")
    sonarr_key = get_setting(db, "sonarr_api_key", "")

    def _test(url, key, health_path, auth_header, elevation_path=None):
        if not url or not key:
            return {"ok": False, "error": "Not configured", "configured": False}
        try:
            r = requests.get(
                f"{url.rstrip('/')}/{health_path}",
                headers={auth_header: key},
                timeout=5,
            )
            if r.status_code == 401:
                return {"ok": False, "error": "API key is invalid", "configured": True}
            r.raise_for_status()
            if elevation_path:
                # The health path only proves the key is valid. The Tentacle plugin's
                # refresh (the only thing that live-updates Android TV) requires an
                # administrator, so a user token or non-admin key would show green
                # here while every plugin notify fails 403 in silence.
                e = requests.get(
                    f"{url.rstrip('/')}/{elevation_path}",
                    headers={auth_header: key},
                    timeout=5,
                )
                if e.status_code == 403:
                    return {"ok": False, "configured": True,
                            "error": "Key is valid but not an administrator — plugin refresh "
                                     "(Android TV live updates) will fail. Use an API key from "
                                     "Jellyfin Dashboard → API Keys"}
            return {"ok": True, "configured": True}
        except requests.ConnectionError:
            return {"ok": False, "error": f"Cannot reach {url}", "configured": True}
        except requests.Timeout:
            return {"ok": False, "error": "Connection timed out", "configured": True}
        except Exception as e:
            return {"ok": False, "error": str(e), "configured": True}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futs = {
            # /Plugins requires elevation. /System/Configuration does not: any
            # signed-in user may read it, so probing it let a non-admin key pass (#129).
            "jellyfin": executor.submit(_test, jf_url, jf_key, "System/Info", "X-Emby-Token", "Plugins"),
            "radarr": executor.submit(_test, radarr_url, radarr_key, "api/v3/system/status", "X-Api-Key"),
            "sonarr": executor.submit(_test, sonarr_url, sonarr_key, "api/v3/system/status", "X-Api-Key"),
        }
        results = {}
        for name, fut in futs.items():
            try:
                results[name] = fut.result(timeout=10)
            except Exception:
                results[name] = {"ok": False, "error": "Test timed out", "configured": True}

    # Hybrid-series prerequisite: Sonarr needs a root folder pointing at the VOD
    # shows directory, or "Download More Episodes" on a VOD series would create
    # a duplicate series in Jellyfin. Only relevant when the user has VOD series.
    if results.get("sonarr", {}).get("ok") and get_setting(db, "hybrid_series_layout", "vod_root") == "vod_root":
        try:
            from models.database import Series
            has_vod_series = db.query(Series).filter(Series.source.like("provider_%")).count() > 0
            if has_vod_series:
                r = requests.get(
                    f"{sonarr_url.rstrip('/')}/api/v3/rootfolder",
                    headers={"X-Api-Key": sonarr_key},
                    timeout=5,
                )
                r.raise_for_status()
                has_vod_root = any("vod" in (rf.get("path") or "").lower() for rf in r.json())
                results["sonarr"]["vod_root_missing"] = not has_vod_root
        except Exception:
            pass

    return results


@router.get("/stale-files")
def check_stale_files(db: Session = Depends(get_db)):
    """Check for .strm files in VOD folders that Tentacle didn't create.
    Only relevant on first startup when DB is empty but VOD folders have content."""
    # Don't show if user already dismissed or has synced content
    if get_setting(db, "stale_files_dismissed") == "true":
        return {"show": False}

    from models.database import Movie, Series, SyncRun
    has_content = db.query(Movie).filter(Movie.source.like("provider_%")).count() > 0
    has_series = db.query(Series).filter(Series.source.like("provider_%")).count() > 0
    has_synced = db.query(SyncRun).filter(SyncRun.status == "completed").count() > 0
    if has_content or has_series or has_synced:
        return {"show": False}

    # Scan VOD folders for existing .strm files and the NFOs that go with them.
    movies_path = Path("/media/vod/movies")
    shows_path = Path("/media/vod/shows")
    strm_count = 0
    nfo_count = 0
    for vod_dir in [movies_path, shows_path]:
        if vod_dir.exists():
            strm_count += len(list(vod_dir.rglob("*.strm")))
            nfo_count += _stale_nfo_count(vod_dir)

    if strm_count == 0:
        return {"show": False}

    return {"show": True, "strm_count": strm_count, "nfo_count": nfo_count}


def _stale_nfo_count(vod_dir) -> int:
    """How many NFOs "Delete All & Start Fresh" would actually remove under vod_dir.

    The banner used to count every .nfo in the tree, but since #28 the cleanup
    removes only a .strm's own NFO, plus a show's tvshow.nfo / season.nfo when
    no real media is left in it — so in a merged Radarr/Sonarr library the
    banner promised far more than the action deleted (#107).
    """
    from services.media_files import _has_media_files
    count = 0
    seen = set()
    for strm in vod_dir.rglob("*.strm"):
        nfo = strm.with_suffix(".nfo")
        if nfo.exists() and nfo not in seen:
            seen.add(nfo)
            count += 1
    # Show-level metadata goes only with a show that holds .strm episodes and no media.
    for show in vod_dir.iterdir() if vod_dir.is_dir() else []:
        if not show.is_dir() or not any(show.rglob("*.strm")):
            continue
        if _has_media_files(show):
            continue
        for shared in (show / "tvshow.nfo", show / "season.nfo"):
            if shared.exists() and shared not in seen:
                seen.add(shared)
                count += 1
    return count


class StaleFilesDelete(BaseModel):
    confirm: bool = False


def _is_within(child: Path, parent: Path) -> bool:
    """True if `child` (resolved) is inside `parent` (resolved) — guards against
    symlinks pointing outside the VOD roots."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


@router.post("/stale-files/delete")
def delete_stale_files(
    body: StaleFilesDelete | None = None,
    confirm: bool = False,
    db: Session = Depends(get_db),
):
    """Delete all .strm and .nfo files in the (fixed) VOD folders, then remove empty
    directories. Destructive, so it requires an explicit confirm flag (body
    {"confirm": true} or ?confirm=true). Every deletion is constrained to the two
    known VOD roots (no user-supplied paths; resolved-path containment check guards
    against symlink escapes)."""
    if not ((body and body.confirm) or confirm):
        raise HTTPException(400, "Confirmation required: pass {\"confirm\": true} (or ?confirm=true) to delete stale files")

    # Same precondition as GET /stale-files. Once Tentacle has synced, every .strm
    # in these folders is its own, and this endpoint would wipe the library.
    from models.database import Movie, Series, SyncRun
    if (db.query(Movie).filter(Movie.source.like("provider_%")).count()
            or db.query(Series).filter(Series.source.like("provider_%")).count()
            or db.query(SyncRun).filter(SyncRun.status == "completed").count()):
        raise HTTPException(409, "Tentacle already manages content in the VOD folders — "
                                 "the stale-file cleanup is only available before the first sync")
    from services.media_files import delete_movie_files, delete_series_files

    # Hardcoded roots — never derived from request input, so there is no traversal
    # vector. The containment check below is defence-in-depth against symlinks.
    vod_roots = [Path("/media/vod/movies"), Path("/media/vod/shows")]
    deleted_strm = 0
    deleted_nfo = 0

    # Only the .strm files and the NFOs that belong to them go, through the same
    # helpers the prune uses. In a merged setup these roots are also the
    # Radarr/Sonarr library, whose own NFOs (episode .nfo, tvshow.nfo of
    # downloaded shows) must survive.
    for vod_dir in vod_roots:
        if not vod_dir.exists():
            continue
        strm_before = sum(1 for _ in vod_dir.rglob("*.strm"))
        nfo_before = sum(1 for _ in vod_dir.rglob("*.nfo"))
        if vod_dir == vod_roots[0]:
            for f in list(vod_dir.rglob("*.strm")):
                if _is_within(f, vod_dir):
                    delete_movie_files(f)
        else:
            for show in list(vod_dir.iterdir()):
                if not _is_within(show, vod_dir):
                    continue
                if show.is_dir() and any(show.rglob("*.strm")):
                    delete_series_files(show)
                elif show.suffix.lower() == ".strm":
                    delete_movie_files(show)
        deleted_strm += strm_before - sum(1 for _ in vod_dir.rglob("*.strm"))
        deleted_nfo += nfo_before - sum(1 for _ in vod_dir.rglob("*.nfo"))

    set_setting(db, "stale_files_dismissed", "true")
    if deleted_strm or deleted_nfo:
        from models.database import log_deletion
        log_deletion(db, kind="stale-cleanup", name=f"{deleted_strm} .strm + {deleted_nfo} .nfo file(s)",
                     reason="manual", detail="Stale VOD files from a previous tool deleted at user request")
    return {"success": True, "deleted_strm": deleted_strm, "deleted_nfo": deleted_nfo}


@router.post("/stale-files/dismiss")
def dismiss_stale_files(db: Session = Depends(get_db)):
    """Permanently dismiss the stale files warning."""
    set_setting(db, "stale_files_dismissed", "true")
    return {"success": True}


class WebhookTest(BaseModel):
    url: str


def _validate_webhook_target(url: str) -> None:
    """Constrain the admin-triggered webhook-test proxy.

    Radarr/Sonarr legitimately run on the LAN, so (unlike the public stream/image
    proxies) we can't require a fully public host here. But we still block the most
    dangerous SSRF targets — loopback (the Tentacle process itself / other local
    daemons) and link-local (cloud metadata, 169.254.169.254) — and reject
    non-http(s) schemes so this can't be pointed at file:// or internal admin APIs
    on the host itself.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Webhook URL must be http or https")
    host = parsed.hostname
    if not host:
        raise HTTPException(400, "Webhook URL has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        raise HTTPException(400, "Webhook host could not be resolved")
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise HTTPException(400, "Webhook host resolved to an invalid address")
        if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            raise HTTPException(400, "Webhook URL points to a disallowed (loopback/metadata) host")


@router.post("/test-webhook")
def test_webhook(body: WebhookTest):
    """Proxy a webhook test through the backend to avoid mixed-content browser issues."""
    _validate_webhook_target(body.url)
    try:
        r = requests.post(
            body.url,
            json={"eventType": "Test"},
            timeout=10,
            allow_redirects=False,
        )
        r.raise_for_status()
        return {"success": True, "message": "Webhook test successful"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Webhook test failed: {str(e)}")


class JellyfinLogin(BaseModel):
    username: str
    password: str


@router.post("/jellyfin-login")
def jellyfin_login(body: JellyfinLogin, db: Session = Depends(get_db)):
    """Authenticate with Jellyfin and save user ID/name."""
    url = get_setting(db, "jellyfin_url")
    if not url:
        raise HTTPException(400, "Jellyfin URL not configured")
    try:
        r = requests.post(
            f"{url.rstrip('/')}/Users/AuthenticateByName",
            headers={
                "Authorization": 'MediaBrowser Client="Tentacle", Device="Server", DeviceId="tentacle", Version="1.0"',
                "Content-Type": "application/json",
            },
            json={"Username": body.username, "Pw": body.password},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        user_id = data["User"]["Id"]
        user_name = data["User"]["Name"]
        set_setting(db, "jellyfin_user_id", user_id)
        set_setting(db, "jellyfin_user_name", user_name)
        return {"success": True, "user_id": user_id, "username": user_name}
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            raise HTTPException(401, "Invalid username or password")
        raise HTTPException(400, f"Jellyfin login failed: {e}")
    except Exception as e:
        raise HTTPException(400, f"Jellyfin login failed: {e}")
