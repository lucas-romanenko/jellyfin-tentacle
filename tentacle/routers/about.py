"""About (Settings → About, admins): which versions run — this server, the
Jellyfin plugin, Jellyfin itself and each person's TV app — whether a newer
server release is out, and whether the plugin is older than this server
needs. The admin updates each of them; nothing here installs anything.

The version logic is in services/versions.py.
"""
import logging
import os

import requests
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from models.database import TentacleUser, get_db
from routers.auth import require_admin
from services import family as F, tv_app, versions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/about", tags=["about"], dependencies=[Depends(require_admin)])


def _plugin(plugins: list, repositories) -> dict:
    """The Tentacle entry of Jellyfin's /Plugins: the one running, and a
    newer one Jellyfin installed that waits for a restart."""
    mine = [p for p in plugins or []
            if str(p.get("Id") or "").replace("-", "").lower() == versions.PLUGIN_ID]
    running = [p for p in mine if p.get("Status") == "Active"] or mine
    run = max(running, key=lambda p: versions.plugin_key(p.get("Version")), default=None)
    version = run.get("Version") if run else None
    pending = [p.get("Version") for p in mine if p.get("Status") == "Restart"
               and versions.plugin_key(p.get("Version")) > versions.plugin_key(version)]
    catalog = None
    if repositories is not None:
        catalog = any(r.get("Enabled", True) and versions.REPO in str(r.get("Url") or "") for r in repositories)
    return {
        "version": version,
        "status": run.get("Status") if run else None,
        "minimum": versions.MIN_PLUGIN_VERSION,
        "too_old": versions.plugin_too_old(version),
        "restart_for": max(pending, key=versions.plugin_key) if pending else None,
        # Jellyfin updates a plugin itself only from a catalogue it lists
        "catalog": catalog,
        "catalog_url": versions.CATALOG_URL,
    }


@router.get("")
def about(db: Session = Depends(get_db)):
    running = versions.current()
    latest = versions.latest_release()
    out = {
        "server": {
            "version": running,
            "changes": versions.changes_since(running),
            "commit": (os.environ.get("TENTACLE_COMMIT") or "").strip() or None,
            "built": (os.environ.get("TENTACLE_BUILD_DATE") or "").strip() or None,
            "latest": latest,
            "update": versions.update_available(running, latest),
            "releases": versions.RELEASES_PAGE,
        },
        "jellyfin": None,
        "plugin": None,
        "tv_apps": [],
        "tv_latest": None,
        "problem": None,
    }
    try:
        jf = F.jellyfin(db)
    except F.FamilyError as e:
        out["problem"] = e.message
        return out
    try:
        info = jf.get("/System/Info") or {}
        out["jellyfin"] = {"version": info.get("Version"), "name": info.get("ServerName")}
        plugins = jf.get("/Plugins") or []
    except (requests.RequestException, ValueError) as e:
        logger.info(f"[About] Jellyfin not asked: {type(e).__name__}")
        out["problem"] = "Jellyfin can't be reached right now."
        return out
    try:
        repositories = jf.get("/Repositories") or []
    except (requests.RequestException, ValueError):
        repositories = None
    out["plugin"] = _plugin(plugins, repositories)
    try:
        devices = (jf.get("/Devices") or {}).get("Items") or []
    except (requests.RequestException, ValueError):
        devices = []
    newest = tv_app.latest()
    out["tv_latest"] = newest
    by_user = tv_app.versions_by_user(devices)
    server_release = versions.release_of(running) or None
    for row in db.query(TentacleUser).order_by(TentacleUser.display_name).all():
        tv = by_user.get((row.jellyfin_user_id or "").lower())
        if not tv:
            continue
        out["tv_apps"].append({
            "name": row.display_name,
            "version": tv,
            "up_to_date": bool(newest and newest.get("version")
                               and tv_app.version_key(tv) >= tv_app.version_key(newest["version"])),
            "waiting": [n["what"] for n in versions.tv_app_waiting(tv, server_release, out["plugin"]["version"])],
        })
    return out
