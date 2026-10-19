"""The settings page's "Test" buttons, one per service.

Each check reports every step it took, in order, as ok / fail / warn, so the
user sees exactly what worked and what didn't ("reachable, key valid, but the
default profile no longer exists") instead of one pass/fail line. A warn is a
hint: it does not make the test fail.

Also the pickers' data (quality profiles, root folders, metadata profiles,
Jellyfin music libraries), read from the service itself.
"""
import logging
from typing import Optional

import requests

from models.database import get_setting

logger = logging.getLogger(__name__)

TIMEOUT = 15
DEEZER_API = "https://api.deezer.com"
# A stable, well-known MusicBrainz artist (Nirvana) for the reachability test.
MB_PROBE_ARTIST = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"


class Checks:
    def __init__(self, service: str):
        self.service = service
        self.items = []

    def ok(self, label: str, detail: str = ""):
        self.items.append({"label": label, "status": "ok", "detail": detail})

    def fail(self, label: str, detail: str = ""):
        self.items.append({"label": label, "status": "fail", "detail": detail})

    def warn(self, label: str, detail: str = ""):
        self.items.append({"label": label, "status": "warn", "detail": detail})

    @property
    def success(self) -> bool:
        return not any(i["status"] == "fail" for i in self.items)

    def result(self, summary: str = "") -> dict:
        failed = next((i for i in self.items if i["status"] == "fail"), None)
        if failed:
            message = f"{self.service}: {failed['label']}" + (f" — {failed['detail']}" if failed["detail"] else "")
        else:
            message = summary or f"{self.service} OK"
        return {"success": self.success, "message": message, "checks": self.items}


def _setting(db, picks: Optional[dict], key: str) -> str:
    """An on-screen pick if the page sent one, else the saved setting."""
    if picks and key in picks and picks[key] is not None:
        return str(picks[key]).strip()
    return (get_setting(db, key) or "").strip()


def _pick(value: Optional[str], saved: str) -> str:
    """A form value wins unless it is empty or still masked ("••••wxyz")."""
    from services.secret_mask import looks_masked
    value = (value or "").strip()
    return saved if (not value or looks_masked(value)) else value


def _explain_request_error(e: Exception, url: str) -> str:
    if isinstance(e, requests.exceptions.Timeout):
        return f"No answer from {url} within {TIMEOUT}s"
    if isinstance(e, requests.exceptions.ConnectionError):
        return f"Can't reach {url} (connection refused or wrong address)"
    return f"{e.__class__.__name__}: {e}"


# ── Radarr / Sonarr ───────────────────────────────────────────────────────

def arr_options(service: str, url: str, key: str) -> dict:
    """Quality profiles and root folders for the settings pickers."""
    h = {"X-Api-Key": key}
    base = url.rstrip("/")
    profiles = requests.get(f"{base}/api/v3/qualityprofile", headers=h, timeout=TIMEOUT)
    profiles.raise_for_status()
    folders = requests.get(f"{base}/api/v3/rootfolder", headers=h, timeout=TIMEOUT)
    folders.raise_for_status()
    return {
        "quality_profiles": [{"id": p["id"], "name": p["name"]} for p in profiles.json()],
        "root_folders": [{"path": f["path"], "free_space": f.get("freeSpace")} for f in folders.json()],
    }


def check_arr(db, service: str, url: Optional[str], key: Optional[str], picks: Optional[dict] = None) -> dict:
    """Radarr or Sonarr: reachable, key valid, version, and the request defaults."""
    name = {"radarr": "Radarr", "sonarr": "Sonarr"}[service]
    c = Checks(name)
    url = _pick(url, get_setting(db, f"{service}_url"))
    key = _pick(key, get_setting(db, f"{service}_api_key"))
    if not url or not key:
        c.fail("Not configured", "Enter the URL and API key")
        return c.result()
    base = url.rstrip("/")
    try:
        r = requests.get(f"{base}/api/v3/system/status", headers={"X-Api-Key": key}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        c.fail("Reachable", _explain_request_error(e, base))
        return c.result()
    c.ok("Reachable", base)
    if r.status_code == 401:
        c.fail("API key", f"{name} rejected it")
        return c.result()
    try:
        status = r.json()
    except ValueError:
        c.fail("API key", f"HTTP {r.status_code}, and the answer isn't {name}'s JSON (a proxy or login page?)")
        return c.result()
    if r.status_code >= 400 or not isinstance(status, dict):
        c.fail("API key", f"HTTP {r.status_code}")
        return c.result()
    c.ok("API key", "accepted")
    app = status.get("appName") or ""
    if app and app.lower() != service:
        c.fail("Version", f"That address is {app}, not {name}")
        return c.result()
    version = status.get("version") or "?"
    c.ok("Version", f"{name} {version}")

    try:
        opts = arr_options(service, base, key)
    except Exception as e:
        c.fail("Profiles and folders", f"Couldn't read them: {e}")
        return c.result(f"{name} {version} connected")
    profiles = {p["id"]: p["name"] for p in opts["quality_profiles"]}
    from services.media_requests import no_default_message
    raw = _setting(db, picks, f"{service}_quality_profile_id")
    default = int(raw) if raw.isdigit() and int(raw) > 0 else None
    if default is None:
        c.fail("Default quality profile", no_default_message(service))
    elif default not in profiles:
        c.fail("Default quality profile", f"The saved one (id {default}) no longer exists in {name}. Pick another.")
    else:
        c.ok("Default quality profile", profiles[default])
    root = _setting(db, picks, f"{service}_root_folder")
    paths = [f["path"] for f in opts["root_folders"]]
    if root:
        if root in paths:
            c.ok("Default root folder", root)
        else:
            c.fail("Default root folder", f"{root} isn't one of {name}'s root folders ({', '.join(paths) or 'none'})")
    elif paths:
        non_vod = [p for p in paths if "vod" not in p.lower()]
        c.ok("Default root folder", f"Automatic: {(non_vod or paths)[0]}")
    else:
        c.fail("Default root folder", f"{name} has no root folders. Add one in {name} → Settings → Media Management.")
    return c.result(f"{name} {version} connected")


# ── Lidarr ────────────────────────────────────────────────────────────────

LOSSLESS = {"flac", "flac 24bit", "alac", "alac 24bit", "wav", "ape", "wavpack"}


def allowed_qualities(profile: dict) -> list:
    """Names of the qualities a Lidarr quality profile accepts (groups flattened)."""
    names = []
    for item in profile.get("items") or []:
        if item.get("items"):
            if item.get("allowed"):
                names += [(sub.get("quality") or {}).get("name") or "" for sub in item["items"]]
        elif item.get("allowed"):
            names.append((item.get("quality") or {}).get("name") or "")
    return [n for n in names if n]


def lidarr_options(url: str, key: str, raw_profiles: bool = False) -> dict:
    from services.lidarr import LidarrClient
    client = LidarrClient(url, key)
    profiles = client.quality_profiles()
    out = {
        "root_folders": [{"path": f.get("path"), "free_space": f.get("freeSpace"),
                          "id": f.get("id")} for f in client.root_folders()],
        "quality_profiles": [{"id": p["id"], "name": p["name"]} for p in profiles],
        "metadata_profiles": [{"id": p["id"], "name": p["name"]} for p in client.metadata_profiles()],
    }
    if raw_profiles:
        out["_profiles"] = profiles
    return out


def check_lidarr(db, url: Optional[str], key: Optional[str], picks: Optional[dict] = None) -> dict:
    from services.lidarr import LidarrClient, LidarrError
    c = Checks("Lidarr")
    url = _pick(url, get_setting(db, "lidarr_url"))
    key = _pick(key, get_setting(db, "lidarr_api_key"))
    if not url or not key:
        c.fail("Not configured", "Enter the URL and API key")
        return c.result()
    client = LidarrClient(url, key)
    try:
        status = client.system_status(retries=0)
    except LidarrError as e:
        if e.status == 401:
            c.ok("Reachable", client.url)
            c.fail("API key", "Lidarr rejected it")
        elif e.status is None:
            c.fail("Reachable", e.message)
        else:
            c.ok("Reachable", client.url)
            c.fail("API key", e.message)
        return c.result()
    c.ok("Reachable", client.url)
    c.ok("API key", "accepted")
    app = (status or {}).get("appName") or ""
    if app and app.lower() != "lidarr":
        c.fail("Version", f"That address is {app}, not Lidarr")
        return c.result()
    version = (status or {}).get("version") or "?"
    c.ok("Version", f"Lidarr {version}")

    try:
        opts = lidarr_options(url, key, raw_profiles=True)
    except LidarrError as e:
        c.fail("Folders and profiles", e.message)
        return c.result(f"Lidarr {version} connected")
    root = _setting(db, picks, "lidarr_root_folder")
    paths = [f["path"] for f in opts["root_folders"]]
    if not paths:
        c.fail("Root folder", "Lidarr has no root folders. Add one in Lidarr → Settings → Media Management.")
    elif not root:
        c.warn("Root folder", "Not picked yet. Album requests need one.")
    elif root in paths:
        c.ok("Root folder", root)
    else:
        c.fail("Root folder", f"{root} isn't one of Lidarr's root folders ({', '.join(paths)})")
    for setting, label, key_name in (("lidarr_quality_profile_id", "Quality profile", "quality_profiles"),
                                     ("lidarr_metadata_profile_id", "Metadata profile", "metadata_profiles")):
        names = {p["id"]: p["name"] for p in opts[key_name]}
        raw = _setting(db, picks, setting)
        if not raw:
            c.warn(label, "Not picked yet. Album requests need one.")
        elif raw.isdigit() and int(raw) in names:
            c.ok(label, names[int(raw)])
        else:
            c.fail(label, f"The saved one (id {raw}) no longer exists in Lidarr. Pick another.")
    raw = _setting(db, picks, "lidarr_quality_profile_id")
    profile = next((p for p in opts["_profiles"] if raw.isdigit() and p.get("id") == int(raw)), None)
    if profile:
        allowed = allowed_qualities(profile)
        if allowed and all(q.lower() in LOSSLESS for q in allowed):
            c.warn("Lossy fallback", f"'{profile['name']}' accepts only lossless formats, so an album that "
                                     f"only exists as MP3 will never download. Allow lossy qualities below "
                                     f"FLAC, with FLAC as the cutoff, to take MP3 when that's all there is and "
                                     f"upgrade later.")
        elif allowed:
            cutoff = next((q.get("quality", {}).get("name") or q.get("name") for q in profile.get("items") or []
                           if (q.get("quality") or {}).get("id") == profile.get("cutoff") or q.get("id") == profile.get("cutoff")), None)
            c.ok("Lossy fallback", f"lossy formats allowed{'; upgrades until ' + cutoff if cutoff and profile.get('upgradeAllowed') else ''}")
    return c.result(f"Lidarr {version} connected")


# ── Navidrome ─────────────────────────────────────────────────────────────

def check_navidrome(db, url: Optional[str], username: Optional[str], password: Optional[str]) -> dict:
    from services.navidrome import NavidromeClient, NavidromeError, ignored_credit_roles
    c = Checks("Navidrome")
    url = _pick(url, get_setting(db, "navidrome_url"))
    username = (username or "").strip() or get_setting(db, "navidrome_username")
    password = _pick(password, get_setting(db, "navidrome_password"))
    if not url or not username or not password:
        c.fail("Not configured", "Enter the URL, username and password")
        return c.result()
    nd = NavidromeClient(url, username, password)
    try:
        app = nd.app_config()
    except NavidromeError as e:
        c.fail("Reachable", e.message)
        return c.result()
    c.ok("Reachable", nd.url)
    try:
        auth = nd.login()
    except NavidromeError as e:
        c.fail("Sign-in", e.message)
        return c.result()
    c.ok("Sign-in", f"as {auth.get('username') or username}{' (admin)' if nd.is_admin else ''}")
    version = (app or {}).get("version")
    if version:
        c.ok("Version", f"Navidrome {version}")
    else:
        c.warn("Version", "Couldn't read it from Navidrome's web page")

    try:
        config = nd.server_config()
    except NavidromeError as e:
        config = None
        logger.debug(f"Navidrome config read failed: {e.message}")
    enabled = config.get("EnableArtworkUpload") if config else (app or {}).get("enableArtworkUpload")
    if enabled:
        c.ok("Artwork upload", "enabled (EnableArtworkUpload)")
    elif nd.is_admin:
        c.ok("Artwork upload", "EnableArtworkUpload is off, but this user is an admin, and Navidrome "
                               "lets admins upload anyway")
    else:
        c.fail("Artwork upload", "EnableArtworkUpload is off and this user isn't an admin, so artist "
                                 "pictures can't be uploaded. Turn it on in navidrome.toml or use an admin account.")
    if config is None:
        c.warn("Credit roles", "Only an admin account can read Navidrome's config, so Tentacle can't "
                               "tell whether credit roles are ignored.")
    else:
        ignored = ignored_credit_roles(config)
        if ignored:
            c.ok("Credit roles", "ignored: " + ", ".join(ignored))
        else:
            c.warn("Credit roles", "Not ignored. Hint: Tags.Composer.Ignore = true (and the same for other "
                                   "roles) in navidrome.toml keeps session players and producers from "
                                   "showing up as artists. Optional.")
    return c.result(f"Navidrome {version} connected" if version else "Navidrome connected")


# ── Jellyfin music ────────────────────────────────────────────────────────

def _jellyfin(db) -> tuple:
    return (get_setting(db, "jellyfin_url") or "").rstrip("/"), get_setting(db, "jellyfin_api_key")


def jellyfin_libraries(db) -> list:
    url, key = _jellyfin(db)
    if not url or not key:
        raise RuntimeError("Jellyfin isn't connected (Settings → Connections).")
    r = requests.get(f"{url}/Library/VirtualFolders", headers={"X-Emby-Token": key}, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": f.get("ItemId"), "name": f.get("Name"), "collection_type": f.get("CollectionType"),
             "locations": f.get("Locations") or []} for f in r.json()]


def create_jellyfin_music_library(db, name: str, path: str) -> dict:
    url, key = _jellyfin(db)
    if not url or not key:
        raise RuntimeError("Jellyfin isn't connected (Settings → Connections).")
    # The path goes in the body: the query form splits on commas.
    r = requests.post(f"{url}/Library/VirtualFolders",
                      params={"name": name, "collectionType": "music", "refreshLibrary": "true"},
                      json={"LibraryOptions": {"PathInfos": [{"Path": path}]}},
                      headers={"X-Emby-Token": key}, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"Jellyfin refused it (HTTP {r.status_code}): {r.text[:200]}")
    created = next((lib for lib in jellyfin_libraries(db) if lib["name"] == name), None)
    if not created:
        raise RuntimeError("Jellyfin accepted the request but the library isn't listed yet. Refresh in a moment.")
    return created


def _norm(path: str) -> str:
    return (path or "").rstrip("/\\") or "/"


def check_jellyfin_music(db, library_id: Optional[str], picks: Optional[dict] = None) -> dict:
    c = Checks("Jellyfin music")
    url, key = _jellyfin(db)
    if not url or not key:
        c.fail("Jellyfin connection", "Connect Jellyfin first (Settings → Connections)")
        return c.result()
    try:
        libs = jellyfin_libraries(db)
    except requests.exceptions.RequestException as e:
        c.fail("Jellyfin connection", _explain_request_error(e, url))
        return c.result()
    c.ok("Jellyfin connection", url)
    library_id = (library_id or "").strip() or get_setting(db, "jellyfin_music_library_id")
    if not library_id:
        c.fail("Music library", "Pick the library that holds your music, or create one")
        return c.result()
    lib = next((l for l in libs if l["id"] == library_id), None)
    if not lib:
        c.fail("Music library", "The saved library no longer exists in Jellyfin. Pick another.")
        return c.result()
    if (lib["collection_type"] or "").lower() != "music":
        c.fail("Music library", f"'{lib['name']}' is a {lib['collection_type'] or 'mixed'} library, not a music library")
        return c.result()
    c.ok("Music library", lib["name"])
    root = _setting(db, picks, "lidarr_root_folder")
    locations = [_norm(p) for p in lib["locations"]]
    if not root:
        c.warn("Folder", "Pick Lidarr's root folder first, then test again to compare them")
    elif _norm(root) in locations:
        c.ok("Folder", f"matches Lidarr's root folder ({root})")
    else:
        c.warn("Folder", f"Jellyfin reads {', '.join(lib['locations']) or 'no folder'}, Lidarr writes to {root}. "
                         f"Fine if that's the same folder mounted at different paths; otherwise Jellyfin "
                         f"won't see Lidarr's albums.")
    return c.result(f"Jellyfin music library '{lib['name']}' found")


# ── MusicBrainz / Deezer ──────────────────────────────────────────────────

def check_musicbrainz(db, contact: Optional[str]) -> dict:
    from services import musicbrainz
    c = Checks("MusicBrainz")
    contact = (contact or "").strip() or get_setting(db, "musicbrainz_contact")
    if not musicbrainz.valid_contact(contact):
        c.fail("Contact email", "MusicBrainz requires a contact email in every request. Enter yours.")
        return c.result()
    c.ok("Contact email", f"User-Agent: {musicbrainz.user_agent(contact)}")
    try:
        data = musicbrainz.get(f"/artist/{MB_PROBE_ARTIST}", contact=contact, retries=0)
    except musicbrainz.MusicBrainzError as e:
        c.fail("Lookup", e.message)
        return c.result()
    c.ok("Lookup", f"found {data.get('name') or 'the test artist'}")
    return c.result("MusicBrainz answers")


def check_deezer() -> dict:
    c = Checks("Deezer")
    try:
        r = requests.get(f"{DEEZER_API}/search/artist", params={"q": "Big Star", "limit": 1}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        c.fail("Reachable", _explain_request_error(e, DEEZER_API))
        return c.result()
    c.ok("Reachable", DEEZER_API)
    try:
        data = r.json()
    except ValueError:
        c.fail("Search", f"HTTP {r.status_code}, not JSON")
        return c.result()
    if r.status_code >= 400 or data.get("error"):
        c.fail("Search", str((data.get("error") or {}).get("message") or f"HTTP {r.status_code}"))
        return c.result()
    c.ok("Search", "artist pictures available" if data.get("data") else "answers, no results for the test query")
    return c.result("Deezer answers")
