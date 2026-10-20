"""The Tentacle Android TV app: its newest release on GitHub, and which
version each person's TV runs (Jellyfin's device list).

The app isn't in the Play Store (Lucas, 20 Oct 2026): people install it once
with the Downloader app (Tentacle's address + "/tv", a redirect to the newest
release), and the app offers every later release itself.
"""
import logging
import re
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REPO = "lucas-romanenko/jellyfin-tentacle-androidtv"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
APP_NAME = "Tentacle Android TV"
CACHE_SECONDS = 6 * 3600
FAILED_CACHE_SECONDS = 15 * 60

_cache = {"at": 0.0, "value": None}
_lock = threading.Lock()


def pick_asset(assets: list) -> Optional[dict]:
    """The release APK for people's TVs (never a debug build)."""
    apks = [a for a in assets or [] if str(a.get("name", "")).endswith(".apk")
            and "debug" not in str(a.get("name", "")).lower()]
    for a in apks:
        if a["name"].endswith("-github-release.apk"):
            return a
    return apks[0] if apks else None


def version_key(v: str) -> tuple:
    """Semver order: "1.21.0" > "1.21.0-rc.2" > "1.20.2"."""
    v = (v or "").strip().lstrip("vV")
    core, _, pre = v.partition("-")
    nums = [int(n) if n.isdigit() else 0 for n in core.split(".")[:3]]
    nums += [0] * (3 - len(nums))
    if not pre:
        return (*nums, 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.]", pre))
    return (*nums, 0, parts)


def latest(fetch=None) -> Optional[dict]:
    """{"version", "url" (the APK), "page"} of the newest release, cached;
    None when GitHub can't be asked."""
    now = time.time()
    with _lock:
        if _cache["at"] and now - _cache["at"] < (CACHE_SECONDS if _cache["value"] else FAILED_CACHE_SECONDS):
            return _cache["value"]
    value = None
    try:
        get = fetch or (lambda: requests.get(LATEST_API, timeout=10,
                                             headers={"Accept": "application/vnd.github+json"}))
        r = get()
        if r.status_code == 200:
            data = r.json()
            asset = pick_asset(data.get("assets") or [])
            value = {"version": str(data.get("tag_name") or "").lstrip("v"),
                     "url": asset["browser_download_url"] if asset else "",
                     "page": data.get("html_url") or RELEASES_PAGE}
        else:
            logger.info(f"[TV app] GitHub answered HTTP {r.status_code} for the newest release")
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.info(f"[TV app] Newest release not read from GitHub: {type(e).__name__}")
    with _lock:
        _cache["at"], _cache["value"] = now, value
    return value


def versions_by_user(devices: list) -> dict:
    """Jellyfin user id (no dashes) -> the newest Tentacle TV app version seen
    on any of their devices (debug builds left out)."""
    out = {}
    for d in devices or []:
        if (d.get("AppName") or "") != APP_NAME:
            continue
        uid = (d.get("LastUserId") or "").replace("-", "").lower()
        v = d.get("AppVersion") or ""
        if uid and v and (uid not in out or version_key(v) > version_key(out[uid])):
            out[uid] = v
    return out
