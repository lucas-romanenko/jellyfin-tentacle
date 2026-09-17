"""Rewrite YouTube's HLS playlists so every URL points back at Tentacle.

Google signs media URLs to the IP that extracted them and expires them after
about six hours, so a client given one directly gets a 403. Rewriting keeps the
structure intact — including the byte ranges and MPEG-TS segments Jellyfin's
ffmpeg needs — while routing the bytes through us.

URLs are handed out as opaque tokens rather than encoded upstream URLs, so the
endpoint can never be used as a general-purpose proxy for an arbitrary host.
"""
import base64
import hashlib
import logging
import re
import threading
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# token -> upstream URL. Bounded so a long-lived process can't grow without end.
_targets: dict = {}
_order: list = []
_lock = threading.Lock()
MAX_TARGETS = 20000

_ATTR_URI = re.compile(r'(URI=")([^"]+)(")')


def _token(url: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(url.encode()).digest()[:18]).decode().rstrip("=")


def register(url: str) -> str:
    """Mint a stable token for an upstream URL."""
    token = _token(url)
    with _lock:
        if token not in _targets:
            _targets[token] = url
            _order.append(token)
            while len(_order) > MAX_TARGETS:
                _targets.pop(_order.pop(0), None)
    return token


def lookup(token: str):
    with _lock:
        return _targets.get(token)


def is_master(playlist_text: str) -> bool:
    """A master playlist lists variants; a media playlist lists segments."""
    return "#EXT-X-STREAM-INF" in playlist_text


def rewrite(playlist_text: str, base_url: str, proxy_prefix: str,
            max_height: int = None) -> str:
    """Rewrite one playlist's URLs (and #EXT-X-MEDIA URI attributes) through the proxy.

    `proxy_prefix` is where segment/playlist requests should come back to, e.g.
    "/api/youtube/v/<id>". Absolute and relative upstream URLs both work —
    everything is resolved against `base_url` first.

    Proxied URLs keep a meaningful extension. ffmpeg's HLS demuxer checks every
    segment URL against `allowed_segment_extensions` and refuses anything it
    doesn't recognise ("not in allowed_segment_extensions"), so an opaque token
    with no suffix makes the whole playlist unplayable — which is how Jellyfin
    would have seen it.
    """
    out = []
    lines = playlist_text.splitlines()
    skip_next_url = False
    # Variants point at more playlists; a media playlist points at TS segments.
    url_suffix = ".m3u8" if is_master(playlist_text) else ".ts"

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#"):
            # Variant filtering: drop ladder entries above the height cap.
            if max_height and stripped.startswith("#EXT-X-STREAM-INF"):
                res = re.search(r"RESOLUTION=\d+x(\d+)", stripped)
                if res and int(res.group(1)) > max_height:
                    skip_next_url = True
                    continue
            # Audio/subtitle renditions carry their URL in an attribute.
            if "URI=" in stripped:
                def _sub(m):
                    target = urljoin(base_url, m.group(2))
                    # Renditions referenced by URI= are always playlists.
                    return f"{m.group(1)}{proxy_prefix}/r/{register(target)}.m3u8{m.group(3)}"
                out.append(_ATTR_URI.sub(_sub, line))
                continue
            out.append(line)
            continue

        if not stripped:
            out.append(line)
            continue

        if skip_next_url:
            skip_next_url = False
            continue

        target = urljoin(base_url, stripped)
        out.append(f"{proxy_prefix}/r/{register(target)}{url_suffix}")

    return "\n".join(out) + "\n"
