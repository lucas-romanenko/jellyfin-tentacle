"""
Tentacle - M3U/M3U+ Parser

Parses M3U playlists from strings, files, or URLs into structured channel dicts.
Handles both standard M3U and extended M3U+ (with tvg-* attributes).
"""

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Regex to extract key="value" attributes from #EXTINF lines
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


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

            # Display name is after the last comma
            comma_idx = line.rfind(",")
            display_name = line[comma_idx + 1:].strip() if comma_idx != -1 else ""

            # Find the URL on the next non-comment, non-empty line
            url = ""
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
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
                })
                i = j + 1
                continue

        i += 1

    return channels


def parse_m3u_from_url(
    url: str,
    user_agent: str = "TiviMate/4.7.0 (Linux; Android 12)",
    timeout: int = 120,
) -> list[dict]:
    """Download M3U from URL and parse it."""
    logger.info(f"Downloading M3U from URL ({url[:60]}...)")
    resp = requests.get(
        url,
        headers={"User-Agent": user_agent},
        timeout=timeout,
    )
    resp.raise_for_status()
    return parse_m3u(resp.text)


def parse_m3u_from_file(path: str) -> list[dict]:
    """Read M3U from local file and parse it."""
    logger.info(f"Reading M3U from file: {path}")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return parse_m3u(f.read())


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
        lines.append(f"{server}/{sid}.ts?username={username}&password={password}")
    return "\n".join(lines)
