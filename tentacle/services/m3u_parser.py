"""
Tentacle - M3U/M3U+ Parser

Parses M3U playlists from strings, files, or URLs into structured channel dicts.
Handles both standard M3U and extended M3U+ (with tvg-* attributes).
"""

import logging
import re
from typing import Optional

import requests

from services.xtream_client import quote_cred

logger = logging.getLogger(__name__)

# Regex to extract key="value" attributes from #EXTINF lines
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def _extinf_name(line: str) -> str:
    """The display name of an #EXTINF line: everything after the first comma
    outside the quoted attributes.

    Taking the text after the LAST comma cut a name with a comma to its tail
    ("UFC 300, Pereira vs Hill" read "Pereira vs Hill", #525). A line with no
    comma outside quotes (an unbalanced quote) keeps that old reading.
    """
    in_quotes = False
    for idx, c in enumerate(line):
        if c == '"':
            in_quotes = not in_quotes
        elif c == "," and not in_quotes:
            return line[idx + 1:].strip()
    comma_idx = line.rfind(",")
    return line[comma_idx + 1:].strip() if comma_idx != -1 else ""


def parse_m3u(content: str) -> list[dict]:
    """
    Parse M3U content string into a list of channel dicts.

    Returns list of:
        {
            "name": str,
            "stream_url": str,
            "epg_channel_id": str | None,  # tvg-id
            "logo_url": str | None,         # tvg-logo
            "group_title": str | None,      # group-title
            "tvg_name": str | None,         # tvg-name
            "tvg_chno": str | None,         # tvg-chno (channel number)
            "tvg_type": str | None,         # tvg-type (live / movie / series), lower case
        }
    """
    channels = []
    lines = content.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("#EXTINF:"):
            # Parse attributes from the #EXTINF line
            attrs = dict(ATTR_RE.findall(line))

            display_name = _extinf_name(line)

            # Find the URL on the next non-comment, non-empty line. Directive
            # lines (#EXTVLCOPT, #EXTGRP, ...) are skipped, but the scan stops
            # at the next #EXTINF: an entry with no URL of its own must not be
            # given the FOLLOWING channel's stream — that mispairs every name
            # with the wrong stream and drops the real entry from the playlist.
            url = ""
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if next_line.startswith("#EXTINF:"):
                    break
                if next_line and not next_line.startswith("#"):
                    url = next_line
                    break
                j += 1

            if url:
                channels.append({
                    "name": attrs.get("tvg-name", display_name) or display_name,
                    "stream_url": url,
                    "epg_channel_id": attrs.get("tvg-id") or None,
                    "logo_url": attrs.get("tvg-logo") or None,
                    "group_title": attrs.get("group-title") or None,
                    "tvg_name": attrs.get("tvg-name") or None,
                    "tvg_chno": attrs.get("tvg-chno") or None,
                    "tvg_type": (attrs.get("tvg-type") or "").strip().lower() or None,
                })
                i = j + 1
                continue

        i += 1

    return channels


def guide_urls(content: str) -> list[str]:
    """The guide (XMLTV) addresses a playlist names in its #EXTM3U header:
    url-tvg / x-tvg-url, which may each hold several, comma-separated.
    Xtream panels' get.php, iptv-org and most providers write one."""
    for line in content[:65536].splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("#EXTM3U"):
            return []
        attrs = dict(ATTR_RE.findall(line))
        out = []
        for key in ("url-tvg", "x-tvg-url", "tvg-url"):
            for u in (attrs.get(key) or "").split(","):
                u = u.strip()
                if u.lower().startswith(("http://", "https://")) and u not in out:
                    out.append(u)
        return out
    return []


def m3u_file_problem(path: str) -> str:
    """Why Tentacle cannot read an M3U File provider's path, in words an admin
    can act on, or "" when it can. The path is read inside Tentacle's
    container, so a file on the host is only there once it is mounted."""
    import os
    path = (path or "").strip()
    if not path:
        return "Enter the playlist file's path."
    if path.lower().startswith(("http://", "https://")):
        return "That is a link: choose \"Playlist link (M3U URL)\" for it."
    if not os.path.isabs(path):
        return (f"Use the file's full path inside Tentacle's container, for example "
                f"/data/{os.path.basename(path) or 'playlist.m3u'}.")
    if os.path.isdir(path):
        return f"{path} is a folder. Enter the playlist file inside it."
    if not os.path.exists(path):
        return (f"Tentacle can't find {path}. The path is read inside Tentacle's container: "
                f"put the file in Tentacle's data folder (mounted at /data) and enter /data/<file name>, "
                f"or mount its folder into the container.")
    if not os.access(path, os.R_OK):
        return f"Tentacle isn't allowed to read {path}. Give the container's user read access to it."
    return ""


def read_m3u_text(m3u_url: str, is_file: bool,
                  user_agent: str = "TiviMate/4.7.0 (Linux; Android 12)", timeout: int = 120) -> str:
    """The playlist's text, downloaded (M3U URL) or read (M3U File)."""
    if is_file:
        problem = m3u_file_problem(m3u_url)
        if problem:
            raise FileNotFoundError(problem)
        logger.info(f"Reading M3U from file: {m3u_url}")
        with open(m3u_url, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    from services.log_redaction import redact
    logger.info(f"Downloading M3U from URL ({redact(m3u_url)[:80]})")
    resp = requests.get(m3u_url, headers={"User-Agent": user_agent}, timeout=timeout)
    if resp.status_code >= 400:
        # Not resp.raise_for_status(): its text carries the URL, and with it
        # a get.php login.
        raise requests.HTTPError(f"The playlist address answered HTTP {resp.status_code}", response=resp)
    return resp.text


def parse_m3u_from_url(
    url: str,
    user_agent: str = "TiviMate/4.7.0 (Linux; Android 12)",
    timeout: int = 120,
) -> list[dict]:
    """Download M3U from URL and parse it."""
    return parse_m3u(read_m3u_text(url, False, user_agent, timeout))


def parse_m3u_from_file(path: str) -> list[dict]:
    """Read M3U from local file and parse it."""
    return parse_m3u(read_m3u_text(path, True))


# ── Live, film or episode ─────────────────────────────────────────────────
# A playlist mixes live channels with films and episodes (an Xtream panel's
# get.php lists all three). Live TV must take only the channels and the VOD
# import only the films and episodes. In order: the entry's own tvg-type;
# an Xtream panel's path (/live/, /movie/, /series/); the file extension (a
# film is a file: .mkv, .mp4 ...; a stream is .ts, .m3u8 or no extension).
_VOD_EXTS = {"mkv", "mp4", "m4v", "avi", "mov", "wmv", "mpg", "mpeg", "webm", "divx", "ogv", "3gp", "vob", "iso"}
_STREAM_EXTS = {"ts", "m3u8", "m3u", "mpd", "flv"}
_TVG_TYPES = {"live": "live", "tv": "live", "channel": "live", "movie": "movie", "movies": "movie",
              "vod": "movie", "film": "movie", "series": "series", "serie": "series", "episode": "series"}


def classify(entry: dict) -> str:
    """"live", "movie" or "series" for one parsed playlist entry."""
    t = _TVG_TYPES.get((entry.get("tvg_type") or "").strip().lower())
    if t:
        return t
    url = (entry.get("stream_url") or "").strip()
    link = panel_link(url)
    if link is not None:
        return link.kind
    low = url.lower().split("?", 1)[0]
    tail = low.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1] if "." in tail else ""
    if ext in _VOD_EXTS:
        return "series" if ("/series/" in low or episode_from_title(entry.get("name") or "")) else "movie"
    if ext not in _STREAM_EXTS:
        if "/series/" in low:
            return "series"
        if "/movie/" in low or "/movies/" in low:
            return "movie"
    return "live"


# ── Xtream panel links ────────────────────────────────────────────────────
# Most provider playlists are an Xtream panel's get.php: every link is
# {server}/{user}/{pass}/{id} (live, output=ts), {server}/live/{user}/{pass}/
# {id}.m3u8|.ts, {server}/movie/{user}/{pass}/{id}.mkv or {server}/series/
# {user}/{pass}/{id}.mkv. Read that way, an M3U provider gets what an Xtream
# one does: live opened as one continuous .ts with .m3u8 as the fallback,
# films through Tentacle's VOD relay, the account's limits.
class PanelLink:
    __slots__ = ("base", "kind", "user", "pw", "stream_id", "ext", "live_prefix")

    def __init__(self, base, kind, user, pw, stream_id, ext, live_prefix):
        # user and pw as they stand in the link (percent-encoded if at all)
        self.base, self.kind, self.user, self.pw = base, kind, user, pw
        self.stream_id, self.ext, self.live_prefix = stream_id, ext, live_prefix

    @property
    def login(self) -> tuple:
        return (self.base, self.user, self.pw)

    def live_url(self, ext: str) -> str:
        """The channel in the given format, in the panel's own path."""
        return f"{self.base}/live/{self.user}/{self.pw}/{self.stream_id}.{ext}"


_PANEL_KINDED = re.compile(r"^/(live|movie|series)/([^/?#]+)/([^/?#]+)/(\d+)(?:\.([A-Za-z0-9]{1,5}))?$")
_PANEL_BARE = re.compile(r"^/([^/?#]+)/([^/?#]+)/(\d+)(?:\.(ts|m3u8))?$")


def panel_link(url: str):
    """The Xtream panel link `url` is, or None. Only the exact shapes above,
    with no query string: anything else is a URL of somebody else's."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
        return None
    base = f"{parts.scheme}://{parts.netloc}"
    m = _PANEL_KINDED.match(parts.path)
    if m:
        kind, user, pw, sid, ext = m.groups()
        if kind == "live" and (ext or "ts").lower() not in ("ts", "m3u8"):
            return None
        if kind != "live" and not ext:
            return None
        return PanelLink(base, kind, user, pw, sid, (ext or "ts").lower(), True)
    m = _PANEL_BARE.match(parts.path)
    if m:
        user, pw, sid, ext = m.groups()
        if user.lower() in ("live", "movie", "series", "hls", "play", "stream", "streams", "channel", "channels"):
            return None
        return PanelLink(base, "live", user, pw, sid, (ext or "ts").lower(), False)
    return None


def panel_login(entries: list[dict]):
    """(server, username, password) of the Xtream panel a playlist's links
    come from, or None. The login most of its panel links share, when they
    are at least half of the playlist: a playlist that merely contains a
    few such links is somebody else's. Username and password are returned
    decoded, as an admin would type them into an Xtream provider."""
    from collections import Counter
    from urllib.parse import unquote
    counts, total = Counter(), 0
    for e in entries:
        total += 1
        link = panel_link(e.get("stream_url") or "")
        if link is not None:
            counts[link.login] += 1
    if not counts:
        return None
    (base, user, pw), n = counts.most_common(1)[0]
    if n * 2 < total:
        return None
    return base, unquote(user), unquote(pw)


# ── VOD classification helpers (used by the M3U VOD sync client) ──────────
# SxxExx (S01E05 / S01 E05 / S01.E05 / S01xE05) then the NxNN fallback (1x05).
_SXXEXX = re.compile(r"\bS(\d{1,3})\s*[._x -]?\s*E(\d{1,4})\b", re.I)
_NXNN = re.compile(r"\b(\d{1,2})[xX](\d{1,3})\b")


def episode_from_title(title: str):
    """Extract (show_name, season, episode) from an episode title, else None."""
    m = _SXXEXX.search(title) or _NXNN.search(title)
    if not m:
        return None
    show = title[:m.start()].strip(" -._|")
    try:
        return show, int(m.group(1)), int(m.group(2))
    except (TypeError, ValueError):
        return None


def container_from_url(url: str) -> str:
    """Best-effort file container extension from a stream URL (default 'mp4')."""
    tail = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
        if 1 <= len(ext) <= 4 and ext.isalnum():
            return ext
    return "mp4"


def xtream_streams_to_m3u(
    streams: list[dict],
    categories: dict[str, str],
    server: str,
    username: str,
    password: str,
) -> str:
    """
    Convert Xtream API get_live_streams response to M3U string.

    Args:
        streams: List of stream dicts from Xtream API
        categories: Dict of {category_id: category_name}
        server: Provider server URL
        username: Provider username
        password: Provider password
    """
    lines = ["#EXTM3U"]
    for ch in streams:
        name = ch.get("name", "")
        epg_id = ch.get("epg_channel_id", "")
        logo = ch.get("stream_icon", "")
        cat_name = categories.get(str(ch.get("category_id", "")), "")
        sid = ch.get("stream_id", "")
        lines.append(
            f'#EXTINF:-1 tvg-id="{epg_id}" tvg-name="{name}" '
            f'tvg-logo="{logo}" group-title="{cat_name}",{name}'
        )
        lines.append(f"{server}/{sid}.ts?username={quote_cred(username)}&password={quote_cred(password)}")
    return "\n".join(lines)
