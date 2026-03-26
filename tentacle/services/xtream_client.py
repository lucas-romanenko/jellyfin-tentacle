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
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "TiviMate/4.7.0 (Linux; Android 12)"
DEFAULT_TIMEOUT = 60


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

    # ── helpers ──────────────────────────────────────────────────────────

    def _api_url(self, action: Optional[str] = None, **extra) -> str:
        url = (
            f"{self.server}/player_api.php"
            f"?username={self.username}&password={self.password}"
        )
        if action:
            url += f"&action={action}"
        for k, v in extra.items():
            url += f"&{k}={v}"
        return url

    def _get_json(self, url: str, timeout: Optional[int] = None) -> list | dict:
        t = timeout or self.timeout
        # Use (connect_timeout, read_timeout) tuple for large responses
        resp = self.session.get(url, timeout=(10, t))
        resp.raise_for_status()
        return resp.json()

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
        return f"{self.server}/xmltv.php?username={self.username}&password={self.password}"

    # ── stream URLs ──────────────────────────────────────────────────────

    def live_stream_url(self, stream_id: int, extension: str = "m3u8") -> str:
        return f"{self.server}/live/{self.username}/{self.password}/{stream_id}.{extension}"

    def movie_stream_url(self, stream_id: int, extension: str = "mp4") -> str:
        return f"{self.server}/movie/{self.username}/{self.password}/{stream_id}.{extension}"

    def series_stream_url(self, stream_id: int, extension: str = "mp4") -> str:
        return f"{self.server}/series/{self.username}/{self.password}/{stream_id}.{extension}"

    # ── cleanup ─────────────────────────────────────────────────────────��

    def close(self):
        self.session.close()
