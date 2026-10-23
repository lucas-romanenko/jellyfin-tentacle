"""Which versions run: this server's release version, the newest release on
GitHub, and what the Jellyfin plugin and the TV app need of each other.

The image is stamped at build time (Dockerfile ARG TENTACLE_VERSION): Docker
Publish passes the release tag ("v1.23.0"); a build of main may pass
`git describe --tags --match 'v[0-9]*'` ("v1.23.0-3-gee28fde"), shown as
"v1.23.0+3": the release it is built on and how many commits since. A build
without the stamp (one made straight from a GitHub URL at a commit, which has
no tags to describe) asks GitHub once which release its commit follows.
"""
import logging
import os
import re
import threading
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

REPO = "lucas-romanenko/jellyfin-tentacle"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
API = f"https://api.github.com/repos/{REPO}"
CATALOG_URL = f"https://raw.githubusercontent.com/{REPO}/main/tentacle-plugin/manifest.json"
PLUGIN_ID = "b7e3f1a92c4d48569a1b8def23456789"

# The oldest Jellyfin plugin this server works with: the plugin released with
# it (plugin-v2.286.0 with v1.23.0: YouTube channels carry their channel's tag
# from creation). Raise it when the server starts relying on a newer plugin.
MIN_PLUGIN_VERSION = "2.286.0"

# What a TV app version brings that needs a newer server or plugin than an
# install may run (from the TV app's release notes). The About panel names
# the ones a person's TV has but this install can't serve yet.
TV_APP_NEEDS = [
    {"tv_app": "1.21.0", "server": "1.10.0", "plugin": "2.272.0",
     "what": "layout changes reach the TV while it is open"},
    {"tv_app": "1.22.0", "server": "1.12.0", "plugin": "2.276.0",
     "what": "Request / Requested for people who ask first"},
    {"tv_app": "1.25.0", "server": "1.17.0", "plugin": "2.280.0",
     "what": "the On Now row on Home"},
]

CACHE_SECONDS = 6 * 3600
FAILED_CACHE_SECONDS = 15 * 60

_DESCRIBE = re.compile(r"^v?(\d+\.\d+\.\d+(?:-rc\.\d+)?)(?:(?:-(\d+)-g[0-9a-f]+)|(?:\+(\d+)))?(?:-dirty)?$")


def normalize(raw: str) -> Optional[str]:
    """A stamp as shown: "v1.23.0" from a tag, "v1.23.0+3" from git describe
    ("v1.23.0-3-gee28fde") or already in that form; None for anything else
    (a plugin tag, "unknown", empty)."""
    m = _DESCRIBE.match((raw or "").strip())
    if not m:
        return None
    ahead = int(m.group(2) or m.group(3) or 0)
    return f"v{m.group(1)}" + (f"+{ahead}" if ahead else "")


def release_of(version: Optional[str]) -> str:
    """"v1.23.0+3" -> "1.23.0": the release a version is built on."""
    return (version or "").lstrip("vV").split("+")[0]


def changes_since(version: Optional[str]) -> int:
    """"v1.23.0+3" -> 3 commits since the release."""
    _, _, n = (version or "").partition("+")
    return int(n) if n.isdigit() else 0


def version_key(v: str) -> tuple:
    """Semver order: "1.21.0" > "1.21.0-rc.2" > "1.20.2" (a "+N" is ignored)."""
    v = release_of(v)
    core, _, pre = v.partition("-")
    nums = [int(n) if n.isdigit() else 0 for n in core.split(".")[:3]]
    nums += [0] * (3 - len(nums))
    if not pre:
        return (*nums, 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (*nums, 0, parts)


def plugin_key(v: str) -> tuple:
    """Jellyfin plugin versions have four parts ("2.286.0.0"; private builds
    "2.286.0.1511"): compare them as numbers, missing parts as 0."""
    nums = [int(n) if n.isdigit() else 0 for n in str(v or "").strip().lstrip("vV").split(".")[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def plugin_too_old(installed: Optional[str], minimum: str = MIN_PLUGIN_VERSION) -> bool:
    return bool(installed) and plugin_key(installed) < plugin_key(minimum)


# ── This server's version ───────────────────────────────────────────────────

_derived = {"commit": None, "version": None}
_derive_lock = threading.Lock()


def stamped() -> Optional[str]:
    return normalize(os.environ.get("TENTACLE_VERSION", ""))


def current() -> Optional[str]:
    """The image's stamp, else what GitHub said about its commit (once
    asked), else None."""
    return stamped() or _derived["version"]


def derive(commit: str, fetch: Optional[Callable] = None) -> Optional[str]:
    """For a build without a stamp: the newest release and how many commits
    the build's commit is ahead of it, asked of GitHub once per commit.
    None when GitHub can't be asked or the commit isn't on top of it."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        return None
    with _derive_lock:
        if _derived["commit"] == commit:
            return _derived["version"]
    get = fetch or (lambda url: requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"}))
    value = None
    try:
        rel = get(f"{API}/releases/latest")
        tag = rel.json().get("tag_name") if rel.status_code == 200 else None
        if normalize(tag or "") and "+" not in normalize(tag):
            cmp = get(f"{API}/compare/{tag}...{commit}")
            if cmp.status_code == 200:
                data = cmp.json()
                if data.get("status") == "identical":
                    value = normalize(tag)
                elif data.get("status") == "ahead":
                    value = f"{normalize(tag)}+{int(data.get('ahead_by') or 0)}"
    except (requests.RequestException, ValueError, TypeError) as e:
        logger.info(f"[Version] Release of this build not read from GitHub: {type(e).__name__}")
        return None
    with _derive_lock:
        _derived["commit"], _derived["version"] = commit, value
    return value


def derive_in_background(commit: str) -> None:
    """At start-up, for an image without a stamp: ask GitHub without holding
    anything up (offline it simply stays unknown)."""
    if stamped() or not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        return
    threading.Thread(target=derive, args=(commit,), name="version-derive", daemon=True).start()


# ── The newest release on GitHub ────────────────────────────────────────────

_latest = {"at": 0.0, "value": None}
_latest_lock = threading.Lock()


def latest_release(fetch: Optional[Callable] = None) -> Optional[dict]:
    """{"version": "v1.23.0", "page": its release notes} of the newest server
    release, cached 6 h (15 min after a failure); None offline."""
    now = time.time()
    with _latest_lock:
        if _latest["at"] and now - _latest["at"] < (CACHE_SECONDS if _latest["value"] else FAILED_CACHE_SECONDS):
            return _latest["value"]
    value = None
    try:
        get = fetch or (lambda url: requests.get(url, timeout=10, headers={"Accept": "application/vnd.github+json"}))
        r = get(f"{API}/releases/latest")
        if r.status_code == 200:
            data = r.json()
            v = normalize(str(data.get("tag_name") or ""))
            if v:
                value = {"version": v, "page": data.get("html_url") or f"{RELEASES_PAGE}/tag/{v}"}
        else:
            logger.info(f"[Version] GitHub answered HTTP {r.status_code} for the newest release")
    except (requests.RequestException, ValueError, TypeError) as e:
        logger.info(f"[Version] Newest release not read from GitHub: {type(e).__name__}")
    with _latest_lock:
        _latest["at"], _latest["value"] = now, value
    return value


def update_available(running: Optional[str], latest: Optional[dict]) -> bool:
    """A newer release than the one this build is (or is built on)."""
    if not running or not latest or not latest.get("version"):
        return False
    return version_key(latest["version"]) > version_key(running)


# ── The TV app ──────────────────────────────────────────────────────────────

def tv_app_waiting(tv_version: Optional[str], server: Optional[str], plugin: Optional[str]) -> list:
    """What a TV app version brings that this install can't serve yet: the
    TV_APP_NEEDS entries it has whose server or plugin is newer than what
    runs. Unknown versions (a development build) are not held against it."""
    if not tv_version:
        return []
    out = []
    for need in TV_APP_NEEDS:
        if version_key(tv_version) < version_key(need["tv_app"]):
            continue
        if (server and version_key(server) < version_key(need["server"])) or plugin_too_old(plugin, need["plugin"]):
            out.append(need)
    return out
