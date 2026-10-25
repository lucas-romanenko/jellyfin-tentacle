"""
Tentacle - Xtream Codes API Client

Async client for Xtream Codes API v2. Used for both VOD and Live TV sync.

Key endpoints (all GET via player_api.php):
  ?action=get_live_categories         → Live TV categories
  ?action=get_live_streams            → All live channels (may be huge, prefer per-category)
  ?action=get_live_streams&category_id=X → Channels in one category
  ?action=get_vod_categories          → VOD movie categories
  ?action=get_vod_streams             → All VOD movies
  ?action=get_series_categories       → Series categories
  ?action=get_series                  → All series
  ?action=get_series_info&series_id=X → Series details + episodes

Stream URL formats:
  Live:   http://server/{stream_id}.ts?username=X&password=X
  Movie:  http://server/movie/username/password/{stream_id}.mp4
  Series: http://server/series/username/password/{stream_id}.mp4

EPG/XMLTV: http://server/xmltv.php?username=X&password=X

IMPORTANT: Many providers block requests without a known User-Agent.
IMPORTANT: get_live_streams without category_id can return 40,000+ items
and frequently times out. Always prefer per-category fetching.
"""

import logging
import os
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import requests

from services.exceptions import ProviderRefusedError

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "TiviMate/4.7.0 (Linux; Android 12)"
DEFAULT_TIMEOUT = 60


# Letters, digits, -._~ and these stay as they are, so a login of plain
# characters keeps byte-identical URLs (no .strm or channel rewrite on upgrade).
_CRED_SAFE = "!$()*,;=:@"


def quote_cred(value) -> str:
    """An Xtream username or password for a URL query value or path segment (#529).

    '#&+?/%' and spaces are percent-encoded: raw, they cut or split the URL.
    Every URL with a provider login builds it with this."""
    return urllib.parse.quote(str(value or ""), safe=_CRED_SAFE)


PROVIDER_REFUSAL_LIMIT = 3       # refused API requests in a row before a client stops asking
XTREAM_MIN_REQUEST_GAP = 0.25    # seconds between two API requests of one client (at most 4/s)


# When Tentacle last asked a provider's API anything (any provider, any
# client): the nightly guide download waits for a quiet spell after the
# sync's burst (main._run_scheduled_sync, GUIDE_QUIET_SECONDS).
_last_api_request = {"at": None}
_last_api_lock = threading.Lock()


def note_api_request() -> None:
    """Call when a provider API request goes out (RequestGuard does)."""
    with _last_api_lock:
        _last_api_request["at"] = time.monotonic()


def seconds_since_api_request() -> Optional[float]:
    """Seconds since the last provider API request, None when there was none."""
    with _last_api_lock:
        at = _last_api_request["at"]
    return None if at is None else max(0.0, time.monotonic() - at)


def pacing_is_virtual() -> bool:
    """Under the test suite (tests/hermetic.py sets TENTACLE_PACING=virtual)
    the fake providers answer at once: the pacing waits pass on a virtual
    clock instead of sleeping."""
    return os.environ.get("TENTACLE_PACING") == "virtual"


def pause(seconds: float) -> None:
    """time.sleep for spacing out provider requests (nothing under the test suite)."""
    if seconds > 0 and not pacing_is_virtual():
        time.sleep(seconds)


def retry_after_seconds(e) -> Optional[int]:
    """The provider's Retry-After (seconds or an HTTP date) on a refused
    request (an exception with a `response`), or None."""
    value = (getattr(getattr(e, "response", None), "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    value = str(value).strip()
    if value.isdigit():
        return int(value)
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(value)
        return max(0, int((when - datetime.now(when.tzinfo)).total_seconds()))
    except (TypeError, ValueError, IndexError):
        return None


class RequestGuard:
    """One client's API requests to an Xtream panel: paced, and stopped once
    the panel refuses this server (#149).

    A panel that blocks an IP or an account answers 401/403 to everything, and
    its flood protection counts those requests too: a sync that kept asking
    (every category left, every series) made the block last longer. After
    PROVIDER_REFUSAL_LIMIT refusals in a row every further call raises
    ProviderRefusedError without a request. A success starts the count again;
    server errors (5xx, 429/509) and timeouts are not refusals. One guard per
    client, so the next run starts fresh."""

    def __init__(self, gap: float = XTREAM_MIN_REQUEST_GAP):
        self.gap = gap
        self.clock = time.monotonic
        self.sleep = time.sleep
        self.refusals = 0
        self.refused_status = None   # set once the client stopped asking
        self.last_refusal = None     # the latest 401/403 since the last success (provider health, #652)
        self._last = None

    def before(self) -> None:
        """Call before each request: raises once stopped, else waits out the gap."""
        if self.refused_status is not None:
            raise ProviderRefusedError(self.refused_status)
        if self._last is not None and self.gap > 0:
            wait = self._last + self.gap - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()
        note_api_request()

    def after(self, status) -> None:
        """Call with each answer's status code."""
        if status in (401, 403):
            self.refusals += 1
            self.last_refusal = status
            if self.refusals >= PROVIDER_REFUSAL_LIMIT:
                self.refused_status = status
                logger.warning(f"The provider refused {self.refusals} requests in a row (HTTP {status}): "
                               f"no more requests this run")
                raise ProviderRefusedError(status)
        elif isinstance(status, int) and status < 400:
            self.refusals = 0
            self.last_refusal = None

    def refused(self, everything_failed: bool):
        """The 401/403 this run ended on, for provider health: the client
        stopped asking, or nothing at all was read and the last refusal came
        after the last success. None otherwise (one category a panel
        restricts is not the provider refusing this server)."""
        if self.refused_status is not None:
            return self.refused_status
        return self.last_refusal if everything_failed else None


def live_stream_url(server: str, username: str, password: str, stream_id, extension: str = "m3u8") -> str:
    """A live channel's URL. Built from the provider's settings alone, so a
    saved provider edit can rewrite its channels without asking the provider."""
    return f"{server.rstrip('/')}/live/{quote_cred(username)}/{quote_cred(password)}/{stream_id}.{extension}"


class XtreamClient:
    """Synchronous Xtream Codes API client using requests.Session."""

    def __init__(
        self,
        server: str,
        username: str,
        password: str,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.server = server.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        })
        self.guard = RequestGuard()

    # ── helpers ──────────────────────────────────────────────────────────

    def _api_url(self, action: Optional[str] = None, **extra) -> str:
        url = (
            f"{self.server}/player_api.php"
            f"?username={quote_cred(self.username)}&password={quote_cred(self.password)}"
        )
        if action:
            url += f"&action={action}"
        for k, v in extra.items():
            url += f"&{k}={v}"
        return url

    def _get_json(self, url: str, timeout: Optional[int] = None) -> list | dict:
        t = timeout or self.timeout
        self.guard.before()
        # Use (connect_timeout, read_timeout) tuple for large responses
        resp = self.session.get(url, timeout=(10, t))
        self.guard.after(getattr(resp, "status_code", None))
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as e:
            # Provider returned non-JSON (HTML error page, empty body, etc.).
            # Surface a clear error instead of an opaque JSONDecodeError.
            snippet = (resp.text or "")[:200]
            raise ValueError(
                f"Xtream provider returned malformed/non-JSON response: {e} — body starts: {snippet!r}"
            )

    # ── auth ─────────────────────────────────────────────────────────────

    def authenticate(self) -> dict:
        """Authenticate and return account + server info."""
        return self._get_json(self._api_url())

    # ── live TV ──────────────────────────────────────────────────────────

    def get_live_categories(self) -> list[dict]:
        return self._get_json(self._api_url("get_live_categories"))

    def get_live_streams(self, category_id: Optional[str] = None) -> list[dict]:
        """
        Fetch live streams. If category_id is None, fetches ALL streams
        (can be 40K+ items — prefer get_live_streams_by_category).
        """
        extra = {"category_id": category_id} if category_id else {}
        # Bulk fetch (no category_id) can return 40K+ items — needs long timeout
        timeout = 600 if not category_id else 120
        return self._get_json(
            self._api_url("get_live_streams", **extra),
            timeout=timeout,
        )

    def get_all_live_streams_by_category(
        self,
        category_ids: Optional[list[str]] = None,
        progress_callback=None,
        delay: float = 0.05,
    ) -> list[dict]:
        """
        Fetch live streams category-by-category to avoid timeouts.
        Much more reliable than a single get_live_streams call.
        """
        if category_ids is None:
            cats = self.get_live_categories()
            category_ids = [str(c["category_id"]) for c in cats]

        all_streams = []
        total = len(category_ids)
        for i, cat_id in enumerate(category_ids):
            try:
                streams = self.get_live_streams(category_id=cat_id)
                all_streams.extend(streams)
                if progress_callback:
                    progress_callback(i + 1, total, len(all_streams))
            except ProviderRefusedError:
                raise   # the provider refuses this server: no lineup to report
            except Exception as e:
                logger.warning(f"Failed to fetch live streams for category {cat_id}: {e}")
            if delay > 0:
                time.sleep(delay)

        return all_streams

    # ── VOD ──────────────────────────────────────────────────────────────

    def get_vod_categories(self) -> list[dict]:
        return self._get_json(self._api_url("get_vod_categories"))

    def get_vod_streams(self, category_id: Optional[str] = None) -> list[dict]:
        extra = {"category_id": category_id} if category_id else {}
        return self._get_json(self._api_url("get_vod_streams", **extra))

    def get_vod_info(self, vod_id: int) -> dict:
        """Details for one VOD movie — authoritative catalog-membership check:
        a response with an `info` block means the provider still has it."""
        return self._get_json(self._api_url("get_vod_info", vod_id=vod_id))

    # ── series ───────────────────────────────────────────────────────────

    def get_series_categories(self) -> list[dict]:
        return self._get_json(self._api_url("get_series_categories"))

    def get_series(self, category_id: Optional[str] = None) -> list[dict]:
        extra = {"category_id": category_id} if category_id else {}
        return self._get_json(self._api_url("get_series", **extra))

    def get_series_info(self, series_id: int) -> dict:
        return self._get_json(self._api_url("get_series_info", series_id=series_id))

    # ── EPG ──────────────────────────────────────────────────────────────

    def get_short_epg(self, stream_id: int, limit: int = 4) -> dict:
        return self._get_json(
            self._api_url("get_short_epg", stream_id=stream_id, limit=limit)
        )

    def get_xmltv_url(self) -> str:
        return f"{self.server}/xmltv.php?username={quote_cred(self.username)}&password={quote_cred(self.password)}"

    # ── stream URLs ──────────────────────────────────────────────────────

    def live_stream_url(self, stream_id: int, extension: str = "m3u8") -> str:
        return live_stream_url(self.server, self.username, self.password, stream_id, extension)

    def movie_stream_url(self, stream_id: int, extension: str = "mp4") -> str:
        return f"{self.server}/movie/{quote_cred(self.username)}/{quote_cred(self.password)}/{stream_id}.{extension}"

    def series_stream_url(self, stream_id: int, extension: str = "mp4") -> str:
        return f"{self.server}/series/{quote_cred(self.username)}/{quote_cred(self.password)}/{stream_id}.{extension}"

    # ── cleanup ─────────────────────────────────────────────────────────��

    def close(self):
        self.session.close()
