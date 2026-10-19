"""Navidrome integration: talks to Navidrome's own API (never its database).

Checked against Navidrome 0.61.2's source:

* POST /auth/login {username, password} -> {token, isAdmin, ...}; the token
  goes in the X-ND-Authorization: Bearer header of native API calls.
* GET /app/ is the web UI's page; it embeds window.__APP_CONFIG__, a JSON
  string holding "version" and "enableArtworkUpload". No sign-in needed.
* GET /api/config (admins only) is the server configuration, including
  EnableArtworkUpload and the Tags overrides (Tags.<tag>.Ignore).
* Uploading an image (POST /api/artist/{id}/image) is allowed when
  EnableArtworkUpload is on OR the signed-in user is an admin.
"""
import json
import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 15

# Tags Navidrome turns into artist credits besides the main artist and album
# artist (model/participants.go). Each one it reads becomes an artist entry.
CREDIT_ROLES = ("composer", "lyricist", "arranger", "conductor", "producer", "director",
                "engineer", "mixer", "remixer", "djmixer", "performer")

_APP_CONFIG = re.compile(r"window\.__APP_CONFIG__\s*=\s*(.+?)\s*;?\s*(?:</script>|$)", re.MULTILINE)


class NavidromeError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status


def parse_app_config(html: str) -> Optional[dict]:
    """The config the web page embeds. It is a JSON string inside a JS string literal."""
    m = _APP_CONFIG.search(html or "")
    if not m:
        return None
    try:
        value = json.loads(m.group(1))
        if isinstance(value, str):
            value = json.loads(value)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def ignored_credit_roles(server_config: dict) -> list:
    """Credit roles Navidrome is told to ignore (Tags.<role>.Ignore = true)."""
    tags = (server_config or {}).get("Tags") or {}
    lowered = {str(k).lower(): v for k, v in tags.items()} if isinstance(tags, dict) else {}
    out = []
    for role in CREDIT_ROLES:
        conf = lowered.get(role)
        if isinstance(conf, dict) and any(str(k).lower() == "ignore" and v is True for k, v in conf.items()):
            out.append(role)
    return out


class NavidromeClient:
    def __init__(self, url: str, username: str, password: str):
        self.url = (url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self._auth: Optional[dict] = None

    def _call(self, method: str, path: str, **kw):
        try:
            return requests.request(method, f"{self.url}{path}", timeout=TIMEOUT, **kw)
        except requests.exceptions.Timeout:
            raise NavidromeError(f"Navidrome did not answer within {TIMEOUT}s")
        except requests.exceptions.RequestException as e:
            raise NavidromeError(f"Can't reach Navidrome at {self.url} ({e.__class__.__name__})")

    def login(self) -> dict:
        r = self._call("POST", "/auth/login", json={"username": self.username, "password": self.password})
        if r.status_code == 401:
            raise NavidromeError("Navidrome rejected the username or password.", 401)
        if r.status_code >= 400:
            raise NavidromeError(f"Navidrome sign-in answered HTTP {r.status_code}.", r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise NavidromeError("Navidrome's sign-in returned something that isn't JSON. "
                                 "Is the URL right (include any base path)?")
        if not data.get("token"):
            raise NavidromeError("Navidrome's sign-in returned no token.")
        self._auth = data
        return data

    @property
    def is_admin(self) -> bool:
        return bool((self._auth or {}).get("isAdmin"))

    def _headers(self) -> dict:
        if self._auth is None:
            self.login()
        return {"X-ND-Authorization": f"Bearer {self._auth['token']}"}

    def app_config(self) -> Optional[dict]:
        r = self._call("GET", "/app/")
        if r.status_code >= 400:
            return None
        return parse_app_config(r.text)

    def server_config(self) -> Optional[dict]:
        """Navidrome's configuration (admins only), or None if not allowed."""
        r = self._call("GET", "/api/config", headers=self._headers())
        if r.status_code in (401, 403):
            return None
        if r.status_code >= 400:
            raise NavidromeError(f"Navidrome's config answered HTTP {r.status_code}.", r.status_code)
        try:
            return (r.json() or {}).get("config") or {}
        except ValueError:
            return None
