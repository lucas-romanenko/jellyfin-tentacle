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


def _variant_score(attrs: str) -> tuple:
    """Sort key for one #EXT-X-STREAM-INF line. Higher is better.

    H.264 first: every Jellyfin client and jellyfin-ffmpeg handle it natively,
    while VP9 forces a software transcode at best. Then the tallest picture,
    then the richest bitrate at that height.
    """
    codecs = re.search(r'CODECS="([^"]*)"', attrs)
    codecs = codecs.group(1) if codecs else ""
    res = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
    height = int(res.group(1)) if res else 0
    bw = re.search(r"BANDWIDTH=(\d+)", attrs)
    bandwidth = int(bw.group(1)) if bw else 0
    return (1 if "avc1" in codecs else 0, height, bandwidth)


def select_variant(lines: list, max_height: int = None) -> tuple:
    """Pick the single variant to serve. Returns (attrs_line, url_line) or None.

    YouTube's master offers the whole ladder — 15 variants here, in two codecs,
    each referencing an alternate audio group holding two dozen auto-dubbed
    languages. Passing that through means ffmpeg opens every one of them at
    once: 400 streams for a five-minute video, which it reports as corrupt
    packets and Jellyfin reports as a fatal playback error. One variant is all
    a player needs, and every YouTube variant is already muxed with its
    original-language audio.
    """
    variants = []
    for i, line in enumerate(lines):
        if not line.strip().startswith("#EXT-X-STREAM-INF"):
            continue
        url = next((lines[j].strip() for j in range(i + 1, len(lines))
                    if lines[j].strip() and not lines[j].strip().startswith("#")), None)
        if url:
            variants.append((line.strip(), url))
    if not variants:
        return None

    if not max_height:
        return max(variants, key=lambda v: _variant_score(v[0]))

    capped = [v for v in variants
              if (lambda m: not m or int(m.group(1)) <= max_height)(
                  re.search(r"RESOLUTION=\d+x(\d+)", v[0]))]
    if capped:
        return max(capped, key=lambda v: _variant_score(v[0]))
    # Nothing is within the cap. Returning nothing would be a playback error,
    # which is worse than exceeding a quality preference — so fall back to the
    # smallest variant on offer, the closest thing to what was asked for.
    return min(variants, key=lambda v: _variant_score(v[0])[1:])


def _is_original_audio(line: str) -> bool:
    """Whether an #EXT-X-MEDIA audio rendition is the video's own soundtrack.

    YouTube marks none of them DEFAULT=YES, so the only thing separating the
    real audio from two dozen machine dubs is the content type inside
    YT-EXT-XTAGS — a base64 protobuf holding acont=original or
    acont=dubbed-auto. The same pair appears url-encoded in the rendition's own
    URI, which is the fallback when the tag is absent.
    """
    tags = re.search(r'YT-EXT-XTAGS="([^"]*)"', line)
    if tags:
        raw = tags.group(1)
        try:
            blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (ValueError, TypeError):
            blob = b""
        if b"original" in blob:
            return True
        if b"dubbed" in blob:
            return False
    if "acont%3Doriginal" in line or "acont=original" in line:
        return True
    return "dubbed" not in line.lower()


def select_audio(lines: list, group_id: str) -> str:
    """The one audio rendition to keep from a variant's group, or None.

    Preference: the original soundtrack, then anything marked DEFAULT=YES, then
    whatever came first. Returning several is what made ffmpeg open one stream
    per dubbed language.
    """
    group = [l.strip() for l in lines
             if l.strip().startswith("#EXT-X-MEDIA")
             and 'TYPE=AUDIO' in l
             and f'GROUP-ID="{group_id}"' in l]
    if not group:
        return None
    for line in group:
        if _is_original_audio(line):
            return line
    for line in group:
        if "DEFAULT=YES" in line:
            return line
    return group[0]


def _single_variant_master(lines: list, base_url: str, proxy_prefix: str,
                           max_height: int = None) -> str:
    """Build a master offering exactly one variant and exactly one audio track.

    YouTube advertises each variant's CODECS as muxed video+audio, but serves
    the variant video-only and puts the sound in the rendition group — so the
    group cannot simply be dropped, or the video plays silent. Exactly one
    rendition is kept and marked default. Subtitle and closed-caption groups go,
    since nothing references them any more.
    """
    chosen = select_variant(lines, max_height)
    if not chosen:
        return None
    attrs, url = chosen

    out = ["#EXTM3U", "#EXT-X-INDEPENDENT-SEGMENTS"]
    group = re.search(r'AUDIO="([^"]*)"', attrs)
    audio = select_audio(lines, group.group(1)) if group else None
    if audio:
        # Marked default so a player that ignores the group still selects it.
        audio = re.sub(r"DEFAULT=NO", "DEFAULT=YES", audio)

        def _sub(m):
            return f"{m.group(1)}{proxy_prefix}/r/{register(urljoin(base_url, m.group(2)))}.m3u8{m.group(3)}"
        out.append(_ATTR_URI.sub(_sub, audio))
    else:
        attrs = re.sub(r',?AUDIO="[^"]*"', "", attrs)
    attrs = re.sub(r',?SUBTITLES="[^"]*"', "", attrs)
    attrs = re.sub(r',?CLOSED-CAPTIONS=(?:"[^"]*"|NONE)', "", attrs)

    out.append(attrs)
    out.append(f"{proxy_prefix}/r/{register(urljoin(base_url, url))}.m3u8")
    return "\n".join(out) + "\n"


def rewrite(playlist_text: str, base_url: str, proxy_prefix: str,
            max_height: int = None) -> str:
    """Rewrite one playlist's URLs (and #EXT-X-MEDIA URI attributes) through the proxy.

    `proxy_prefix` is where segment/playlist requests should come back to, e.g.
    "/api/youtube/v/<id>". Absolute and relative upstream URLs both work —
    everything is resolved against `base_url` first.

    A master is reduced to a single variant on the way through; see
    select_variant for why. A media playlist is rewritten line for line.

    Proxied URLs keep a meaningful extension. ffmpeg's HLS demuxer checks every
    segment URL against `allowed_segment_extensions` and refuses anything it
    doesn't recognise ("not in allowed_segment_extensions"), so an opaque token
    with no suffix makes the whole playlist unplayable — which is how Jellyfin
    would have seen it.
    """
    lines = playlist_text.splitlines()
    if is_master(playlist_text):
        single = _single_variant_master(lines, base_url, proxy_prefix, max_height)
        if single:
            return single

    out = []
    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#"):
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

        out.append(f"{proxy_prefix}/r/{register(urljoin(base_url, stripped))}.ts")

    return "\n".join(out) + "\n"
