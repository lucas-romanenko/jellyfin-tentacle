"""MusicBrainz web service client.

MusicBrainz asks every client for two things: a User-Agent that names the
application and a way to contact its operator, and at most one request per
second. Both are enforced here for every caller, process-wide. A 503 means
"slow down": it is retried twice with a pause, then given up with a logged
reason.
"""
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://musicbrainz.org/ws/2"
TIMEOUT = 15
MIN_INTERVAL = 1.0        # between request starts: MusicBrainz allows one a second on average
RETRY_DELAYS = (2, 5)
APP = "Tentacle/1.0"
# Someone waiting on a page (an album, a search) goes before the music worker's
# background lookups (reconcile, Discover, playlist imports); the worker also
# holds off this long after a page's last lookup, so a page's follow-up
# lookups aren't interleaved with the worker's.
INTERACTIVE_GRACE = 2.0

_gate = threading.Lock()
_last_request = [0.0]
_turns = threading.Condition()
_interactive = {"waiting": 0, "last": 0.0}


def _is_background() -> bool:
    return threading.current_thread().name == "music-worker"


def _take_turn(background: bool) -> None:
    """Acquire the one-request-at-a-time gate; background callers give way to pages."""
    with _turns:
        if background:
            while True:
                idle = time.monotonic() - _interactive["last"]
                if not _interactive["waiting"] and idle >= INTERACTIVE_GRACE:
                    break
                _turns.wait(timeout=1.0 if _interactive["waiting"] else INTERACTIVE_GRACE - idle)
        else:
            _interactive["waiting"] += 1
    _gate.acquire()
    if not background:
        with _turns:
            _interactive["waiting"] -= 1
            _interactive["last"] = time.monotonic()
            _turns.notify_all()

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MusicBrainzError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status


def user_agent(contact: str) -> str:
    return f"{APP} ( {contact} )"


def valid_contact(contact: str) -> bool:
    return bool(contact and _EMAIL.match(contact.strip()))


def get(path: str, params: Optional[dict] = None, *, contact: str,
        retries: int = len(RETRY_DELAYS)) -> dict:
    """GET {BASE_URL}{path} as JSON, rate-limited to one request per second."""
    if not valid_contact(contact):
        raise MusicBrainzError("Set a contact email for MusicBrainz in Tentacle's settings "
                               "(MusicBrainz requires one in every request).")
    query = dict(params or {})
    query["fmt"] = "json"
    attempt = 0
    while True:
        _take_turn(_is_background())
        try:
            wait = _last_request[0] + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.monotonic()
            try:
                r = requests.get(f"{BASE_URL}{path}", params=query, timeout=TIMEOUT,
                                 headers={"User-Agent": user_agent(contact.strip()),
                                          "Accept": "application/json"})
                reason = None
            except requests.exceptions.Timeout:
                r, reason = None, f"MusicBrainz did not answer within {TIMEOUT}s"
            except requests.exceptions.RequestException as e:
                r, reason = None, f"Can't reach MusicBrainz ({e.__class__.__name__})"
        finally:
            _gate.release()
        if r is not None:
            if r.status_code == 503:
                reason = "MusicBrainz is rate-limiting (HTTP 503)"
            elif r.status_code == 404:
                raise MusicBrainzError("MusicBrainz has no such entry.", 404)
            elif r.status_code >= 400:
                raise MusicBrainzError(f"MusicBrainz answered HTTP {r.status_code}.", r.status_code)
            else:
                try:
                    return r.json()
                except ValueError:
                    raise MusicBrainzError("MusicBrainz returned something that isn't JSON.", r.status_code)
        if attempt >= retries:
            logger.warning(f"[MusicBrainz] Giving up on {path} after {attempt + 1} attempt"
                           f"{'s' if attempt else ''}: {reason}")
            raise MusicBrainzError(reason, getattr(r, "status_code", None))
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        logger.info(f"[MusicBrainz] {path}: {reason}; retrying in {delay}s")
        time.sleep(delay)
        attempt += 1


# ── Cached lookups ────────────────────────────────────────────────────────
# Same pattern as services/tmdb.py: one JSON file per request under
# {data_dir}/musicbrainz_cache, kept for the "cache lifetime" setting.

SEARCH_TTL = 86400  # searches go stale sooner than lookups


class MusicBrainz:
    """MusicBrainz for one Tentacle install: its contact, cache folder and lifetime."""

    def __init__(self, contact: str, cache_dir: str, cache_days: int = 30):
        self.contact = (contact or "").strip()
        self.cache_dir = Path(cache_dir) / "musicbrainz_cache"
        self.ttl = max(1, int(cache_days or 30)) * 86400

    @classmethod
    def from_settings(cls, db) -> "MusicBrainz":
        from models.database import get_setting
        try:
            days = int(get_setting(db, "musicbrainz_cache_days", "30") or 30)
        except ValueError:
            days = 30
        return cls(get_setting(db, "musicbrainz_contact"), get_setting(db, "data_dir", "/data"), days)

    # cache
    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(key.encode()).hexdigest()}.json"

    def _cached(self, key: str, ttl: int):
        path = self._path(key)
        try:
            data = json.loads(path.read_text())
            if data.get("ts", 0) > time.time() - ttl:
                return True, data.get("v")
        except (OSError, ValueError):
            pass
        return False, None

    def _store(self, key: str, value):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._path(key).write_text(json.dumps({"ts": time.time(), "v": value}))
        except OSError as e:
            logger.debug(f"[MusicBrainz] cache write failed: {e}")

    def cleanup_cache(self) -> int:
        removed = 0
        if not self.cache_dir.exists():
            return 0
        cutoff = time.time() - self.ttl
        for path in self.cache_dir.glob("*.json"):
            try:
                if json.loads(path.read_text()).get("ts", 0) < cutoff:
                    path.unlink()
                    removed += 1
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def get(self, path: str, params: Optional[dict] = None, ttl: Optional[int] = None):
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        hit, value = self._cached(key, ttl or self.ttl)
        if hit:
            return value
        value = get(path, params, contact=self.contact)
        self._store(key, value)
        return value

    def _browse_all(self, path: str, params: dict, list_key: str, count_key: str, max_pages: int = 20) -> list:
        """Every page of a browse request (100 per page; MusicBrainz's maximum)."""
        items, offset = [], 0
        for _ in range(max_pages):
            page = self.get(path, dict(params, limit=100, offset=offset))
            batch = page.get(list_key) or []
            items += batch
            offset += 100
            if not batch or offset >= (page.get(count_key) or 0):
                break
        return items

    # lookups
    def release_group(self, rgid: str) -> dict:
        # Genres ride along (same request): Discover sorts albums by them.
        return self.get(f"/release-group/{rgid}", {"inc": "artist-credits+genres"})

    def release_group_releases(self, rgid: str) -> list:
        """Every official release of a release group, with its media (formats, track counts)."""
        return self._browse_all("/release", {"release-group": rgid, "status": "official", "inc": "media"},
                                "releases", "release-count")

    def release(self, release_id: str) -> dict:
        return self.get(f"/release/{release_id}", {"inc": "recordings+artist-credits"})

    def artist(self, mbid: str) -> dict:
        return self.get(f"/artist/{mbid}", {"inc": "artist-rels"})

    def artist_release_groups(self, mbid: str) -> list:
        # "website-default" leaves out release groups with only bootleg / unofficial
        # releases (the Eagles: 99 -> 45; a bootleg "Hotel California" among them).
        return self._browse_all("/release-group", {"artist": mbid, "release-group-status": "website-default"},
                                "release-groups", "release-group-count")

    # searches (MusicBrainz's Lucene syntax; user text is quoted and escaped)
    def search_artists(self, q: str, limit: int = 8) -> list:
        return self.get("/artist", {"query": lucene_phrase(q), "limit": limit}, ttl=SEARCH_TTL).get("artists") or []

    def search_release_groups(self, q: str, limit: int = 12) -> list:
        return self.get("/release-group", {"query": lucene_phrase(q), "limit": limit},
                        ttl=SEARCH_TTL).get("release-groups") or []

    def search_recordings(self, q: str, limit: int = 25) -> list:
        return self.get("/recording", {"query": lucene_phrase(q), "limit": limit},
                        ttl=SEARCH_TTL).get("recordings") or []

    def find_release_groups(self, title: str, artist: str, limit: int = 10) -> list:
        """Release groups with this title credited to this artist (a chart entry)."""
        query = f"releasegroup:{lucene_quote(title)} AND artist:{lucene_quote(artist)}"
        return self.get("/release-group", {"query": query, "limit": limit},
                        ttl=SEARCH_TTL).get("release-groups") or []

    def recordings_by(self, title: str, artist_ids: list, limit: int = 100) -> list:
        ids = " OR ".join(a for a in artist_ids if _MBID.match(a or ""))
        query = f"recording:{lucene_quote(title)} AND arid:({ids})"
        return self.get("/recording", {"query": query, "limit": limit}, ttl=SEARCH_TTL).get("recordings") or []


_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def is_mbid(value: str) -> bool:
    return bool(_MBID.match(value or ""))


def lucene_quote(text: str) -> str:
    return '"' + _LUCENE_SPECIAL.sub(r"\\\1", (text or "").strip()) + '"'


def lucene_phrase(text: str) -> str:
    """Free text for a search box: escaped words, so ':' or quotes can't change the query."""
    return _LUCENE_SPECIAL.sub(r"\\\1", (text or "").strip())
