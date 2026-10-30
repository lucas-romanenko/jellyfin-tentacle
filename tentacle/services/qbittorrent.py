"""
Tentacle - qBittorrent (read only)

Only the duplicates rule uses it (services/duplicates_auto.py, #872): a
download whose torrent is still in qBittorrent is still seeding, and the
rule never deletes it. Tentacle never changes anything in qBittorrent.
The address, user and password live in settings (qbittorrent_url,
qbittorrent_username, qbittorrent_password), set on the Duplicates card.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 15


def configured(db) -> bool:
    from models.database import get_setting
    return bool((get_setting(db, "qbittorrent_url", "") or "").strip())


def torrent_hashes(db, tag: str = "") -> Optional[set]:
    """The info hashes (lower case) of every torrent in qBittorrent, or only
    of those with `tag`. None when it isn't configured; raises when it can't
    be read (the caller then can't tell what is seeding)."""
    from models.database import get_setting
    url = (get_setting(db, "qbittorrent_url", "") or "").strip().rstrip("/")
    if not url:
        return None
    s = requests.Session()
    # qBittorrent refuses a login whose Referer isn't its own address.
    s.headers.update({"Referer": url})
    user = get_setting(db, "qbittorrent_username", "") or ""
    password = get_setting(db, "qbittorrent_password", "") or ""
    if user or password:
        r = s.post(f"{url}/api/v2/auth/login", data={"username": user, "password": password}, timeout=TIMEOUT)
        r.raise_for_status()
        if r.text.strip() != "Ok.":
            raise RuntimeError("qBittorrent refused the user name or password")
    params = {"tag": tag} if tag else {}
    r = s.get(f"{url}/api/v2/torrents/info", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return {(t.get("hash") or "").lower() for t in r.json() or [] if t.get("hash")}
