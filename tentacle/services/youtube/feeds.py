"""Cheap, official ways to ask a channel "anything new?".

The scheduled check used to load every tab of every channel with yt-dlp, every
hour, and fetch full details for each live or upcoming stream on top: scraped,
cookieless page loads at a fixed minute, which is the traffic shape Google flags.
Most checks find nothing new, so a check now starts here:

- without a key: the channel's (or playlist's) public RSS feed, one small request
  of the kind every feed reader makes (15 newest uploads, Shorts marked by their
  /shorts/ link);
- with a YouTube Data API key (optional, free from the Google Cloud console):
  the API instead, which also gives video details and live status, so the
  scheduled check never loads a YouTube page at all.

yt-dlp is still used for what only it can do: listing a whole tab when something
did change, and finding the stream a viewer plays.
"""
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from services.youtube import traffic
from services.youtube.errors import YouTubeBlocked, YouTubeError, YouTubeUnavailable

logger = logging.getLogger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml"
API_URL = "https://www.googleapis.com/youtube/v3"
TIMEOUT = 20

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# After the API refuses (quota spent, key invalid or restricted), use the free
# path until this time instead of asking again.
_api_off_until = 0.0
# After a 5xx, a 429 or no answer at all: the API is having a moment, not
# refusing this key, so it is left alone for a shorter while.
API_HICCUP_SECONDS = 15 * 60
_api_off_reason = ""


class FeedUnavailable(YouTubeError):
    """No feed for this source (or it could not be read); list the tab instead."""


# ── RSS ─────────────────────────────────────────────────────────────────────

def feed_url(channel) -> Optional[str]:
    if channel.kind == "playlist" and channel.playlist_id:
        return f"{FEED_URL}?playlist_id={channel.playlist_id}"
    if channel.channel_id:
        return f"{FEED_URL}?channel_id={channel.channel_id}"
    return None


def parse_feed(xml_text: str) -> list:
    """[{id, title, published, short}] newest first, from a YouTube Atom feed."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise FeedUnavailable(f"The feed is not XML: {e}")
    if not root.tag.endswith("feed"):
        raise FeedUnavailable("The answer is not a YouTube feed")
    out = []
    for entry in root.findall("atom:entry", _NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=_NS).strip()
        if not vid:
            continue
        link = entry.find("atom:link[@rel='alternate']", _NS)
        href = link.get("href", "") if link is not None else ""
        out.append({
            "id": vid,
            "title": (entry.findtext("atom:title", default="", namespaces=_NS) or "").strip(),
            "published": entry.findtext("atom:published", default="", namespaces=_NS),
            "short": "/shorts/" in href,
        })
    return out


def fetch_rss(channel) -> list:
    """The source's newest uploads from its RSS feed. Raises FeedUnavailable
    when there is none, YouTubeBlocked when YouTube refuses."""
    url = feed_url(channel)
    if not url:
        raise FeedUnavailable("No channel or playlist id to read a feed for")
    traffic.ensure_allowed()
    try:
        r = traffic.http_client().get(url, timeout=TIMEOUT)
    except Exception as e:
        raise FeedUnavailable(f"Could not read the feed: {e}")
    if r.status_code == 429 or "/sorry/" in str(r.url):
        # The client already started the pause.
        raise YouTubeBlocked(f"YouTube refused the feed (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise FeedUnavailable(f"The feed answered HTTP {r.status_code}")
    return parse_feed(r.text)


# ── YouTube Data API (optional key) ─────────────────────────────────────────

def api_available() -> bool:
    return bool(traffic.api_key()) and time.time() >= _api_off_until


def api_state() -> dict:
    left = max(0, int(_api_off_until - time.time()))
    return {"configured": bool(traffic.api_key()), "off_for_seconds": left,
            "reason": _api_off_reason if left else ""}


def _api_off(seconds: float, reason: str) -> None:
    global _api_off_until, _api_off_reason
    if time.time() >= _api_off_until:
        span = f"{int(seconds // 3600)} h" if seconds >= 3600 else f"{int(seconds // 60)} min"
        logger.warning(f"[YouTube] The YouTube Data API is not usable ({reason}); using RSS feeds "
                       f"and yt-dlp for the next {span} instead")
    _api_off_until = time.time() + seconds
    _api_off_reason = reason


def _api_get(path: str, params: dict) -> dict:
    if not api_available():
        raise FeedUnavailable("The YouTube Data API is not in use")
    try:
        r = traffic.http_client().get(f"{API_URL}/{path}", params={**params, "key": traffic.api_key()},
                                      timeout=TIMEOUT)
    except YouTubeError:
        raise                   # held: a saved proxy can't be used
    except Exception as e:
        _api_off(API_HICCUP_SECONDS, f"no answer: {type(e).__name__}")
        raise YouTubeUnavailable(f"The YouTube Data API did not answer: {e}")
    if r.status_code in (400, 403):
        try:
            reason = r.json()["error"]["errors"][0]["reason"]
        except Exception:
            reason = f"HTTP {r.status_code}"
        # A spent daily quota resets at midnight Pacific; anything else (a bad or
        # restricted key) is worth retrying hourly once someone fixes it.
        _api_off(6 * 3600 if "quota" in reason.lower() else 3600, reason)
        raise FeedUnavailable(f"The YouTube Data API refused: {reason}")
    if r.status_code >= 400:
        if r.status_code == 429 or r.status_code >= 500:
            _api_off(API_HICCUP_SECONDS, f"HTTP {r.status_code}")
        raise YouTubeUnavailable(f"The YouTube Data API answered HTTP {r.status_code}")
    return r.json()


def uploads_playlist(channel) -> Optional[str]:
    if channel.kind == "playlist" and channel.playlist_id:
        return channel.playlist_id
    if channel.channel_id and channel.channel_id.startswith("UC"):
        return "UU" + channel.channel_id[2:]
    return None


def fetch_api_uploads(channel) -> list:
    """The source's newest uploads from the Data API (1 quota unit)."""
    playlist_id = uploads_playlist(channel)
    if not playlist_id:
        raise FeedUnavailable("No channel or playlist id for the API")
    data = _api_get("playlistItems", {"part": "contentDetails", "playlistId": playlist_id,
                                      "maxResults": 25})
    return [{"id": i["contentDetails"]["videoId"], "title": "", "published": "", "short": False}
            for i in data.get("items", []) if i.get("contentDetails", {}).get("videoId")]


def api_playlist_size(playlist_id: str) -> Optional[int]:
    """How many entries a playlist has, from the Data API (1 quota unit); None
    when the API does not say (a private or removed playlist)."""
    data = _api_get("playlists", {"part": "contentDetails", "id": playlist_id})
    items = data.get("items") or []
    count = (items[0].get("contentDetails") or {}).get("itemCount") if items else None
    return count if isinstance(count, int) else None


_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(value: str) -> Optional[int]:
    """ISO 8601 duration (PT1H2M3S) in seconds."""
    m = _DURATION_RE.match(value or "")
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _epoch(value: str) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def api_to_details(item: dict) -> dict:
    """A Data API video resource in the shape yt-dlp's details have, as far as
    the indexer reads them."""
    snippet = item.get("snippet") or {}
    content = item.get("contentDetails") or {}
    status = item.get("status") or {}
    live = item.get("liveStreamingDetails") or {}
    broadcast = snippet.get("liveBroadcastContent")
    if broadcast == "live":
        live_status = "is_live"
    elif broadcast == "upcoming":
        live_status = "is_upcoming"
    elif live.get("actualEndTime"):
        live_status = "was_live"
    else:
        live_status = None
    thumbs = snippet.get("thumbnails") or {}
    thumb = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium", "default")
                  if k in thumbs and thumbs[k].get("url")), None)
    privacy = status.get("privacyStatus")
    availability = None if privacy in (None, "public") else privacy
    if (content.get("contentRating") or {}).get("ytRating") == "ytAgeRestricted":
        # The API calls an age-restricted upload public. yt-dlp can't play one
        # without cookies ("Sign in to confirm your age"), so it is skipped with
        # yt-dlp's own availability value for it, as the yt-dlp path skips it (#276).
        availability = "needs_auth"
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "duration": parse_duration(content.get("duration")) or None,
        "live_status": live_status,
        "availability": availability,
        "release_timestamp": _epoch(live.get("scheduledStartTime")) if live_status in ("is_live", "is_upcoming") else None,
        "timestamp": _epoch(snippet.get("publishedAt")),
        "thumbnail": thumb,
        "is_made_for_kids": status.get("madeForKids"),
    }


def api_details(video_ids: list) -> dict:
    """{video_id: details} for up to 50 ids per call (1 quota unit each call).
    A video the API does not return (private, removed) is absent."""
    out = {}
    ids = [v for v in dict.fromkeys(video_ids) if v]
    for i in range(0, len(ids), 50):
        data = _api_get("videos", {"part": "snippet,contentDetails,liveStreamingDetails,status",
                                   "id": ",".join(ids[i:i + 50]), "maxResults": 50})
        for item in data.get("items", []):
            out[item["id"]] = api_to_details(item)
    return out


# ── The check ───────────────────────────────────────────────────────────────

def newest_uploads(channel) -> list:
    """The source's newest uploads: the API when a key works, else RSS.

    Any API failure falls back to the feed. Only a refusal used to: a 5xx, a
    429 or no answer at all made the scheduled check list every channel's tabs
    with yt-dlp instead, on every run of the outage (#246).
    """
    if api_available():
        try:
            return fetch_api_uploads(channel)
        except YouTubeBlocked:
            raise
        except (FeedUnavailable, YouTubeError) as e:
            logger.debug(f"[YouTube] API uploads for '{getattr(channel, 'title', '?')}' failed; "
                         f"reading the feed: {e}")
    return fetch_rss(channel)


def reset_api_state() -> None:
    """Use the API again at once (a new key was saved)."""
    global _api_off_until, _api_off_reason
    _api_off_until, _api_off_reason = 0.0, ""


def check_api_key() -> tuple:
    """(ok, message) for the saved key: one videos.list call, 1 quota unit."""
    if not traffic.api_key():
        return False, "No key saved"
    try:
        _api_get("videos", {"part": "id", "id": "jNQXAC9IVRw"})
        return True, "The key works"
    except (FeedUnavailable, YouTubeError) as e:
        return False, str(e)


def reset_for_tests() -> None:
    global _api_off_until, _api_off_reason
    _api_off_until, _api_off_reason = 0.0, ""
