"""Write the Jellyfin-facing files for indexed YouTube videos.

Each video becomes a Movie item: a .strm pointing at Tentacle's own resolver
endpoint, plus an NFO. Nothing is downloaded.

Two rules worth keeping:
  * The .strm content never changes once written. It is a stable Tentacle URL,
    so there is nothing to rewrite — and rewriting would reset the file's mtime,
    which wipes any media segments Jellyfin has scanned for it.
  * Files live under their own media root, and removal only ever touches a
    single video's own folder. Nothing here recurses over a shared parent.
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

YOUTUBE_MEDIA_ROOT = Path("/media/youtube")

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, limit: int = 120) -> str:
    """Filename-safe version of a video or channel title."""
    cleaned = _UNSAFE.sub("", (text or "").strip()).rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:limit].rstrip() or "Untitled")


def video_folder(channel_title: str, video, root: Path = None) -> Path:
    """<root>/<Channel>/<YYYY-MM-DD Title [videoid]>/"""
    root = root or YOUTUBE_MEDIA_ROOT
    date = (video.published_at or video.first_seen or datetime.utcnow()).strftime("%Y-%m-%d")
    # The id in the folder name lets SponsorBlock tooling and the resolver find
    # the video without a DB lookup, and keeps same-titled uploads apart.
    return root / safe_name(channel_title) / f"{date} {safe_name(video.title, 100)} [{video.video_id}]"


def build_nfo(video, channel, base_url: str) -> str:
    """A movie NFO Jellyfin's BaseNfoParser understands."""
    published = video.published_at or video.first_seen or datetime.utcnow()
    tags = ["youtube", f"yt:{channel.slug}"] + list(channel.extra_tags or [])
    runtime = int((video.duration or 0) / 60) or 1

    parts = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        "<movie>",
        f"  <title>{escape(video.title or '')}</title>",
        f"  <plot>{escape(video.description or '')}</plot>",
        f"  <premiered>{published.strftime('%Y-%m-%d')}</premiered>",
        f"  <year>{published.strftime('%Y')}</year>",
        # dateadded from the upload date, so backfilling a channel doesn't
        # flood "Recently Added" with years-old uploads.
        f"  <dateadded>{published.strftime('%Y-%m-%d %H:%M:%S')}</dateadded>",
        f"  <runtime>{runtime}</runtime>",
        f"  <studio>{escape(channel.title or '')}</studio>",
        f"  <set><name>{escape(channel.title or '')}</name></set>",
        "  <genre>YouTube</genre>",
    ]
    thumb = thumbnail_url(video)
    if thumb:
        # Also named in the NFO so Jellyfin still has artwork if the local
        # fetch failed, or the folder was populated before it was added.
        parts.append(f'  <thumb aspect="poster">{escape(thumb)}</thumb>')
        parts.append(f"  <fanart><thumb>{escape(thumb)}</thumb></fanart>")
    if channel.rating:
        parts.append(f"  <mpaa>{escape(channel.rating)}</mpaa>")
    parts += [f"  <tag>{escape(t)}</tag>" for t in tags]
    parts += [
        f'  <uniqueid type="youtube" default="true">{escape(video.video_id)}</uniqueid>',
        "  <lockdata>true</lockdata>",
        "</movie>",
    ]
    return "\n".join(parts)


def thumbnail_url(video) -> str:
    """Best artwork URL for a video.

    yt-dlp's chosen thumbnail when we have one; otherwise the well-known path,
    which YouTube generates for every video that exists. maxresdefault is
    deliberately not used as the fallback — it is absent for a lot of uploads
    and 404s, which would leave no artwork at all.
    """
    if video.thumbnail_url:
        return video.thumbnail_url
    if video.video_id:
        return f"https://i.ytimg.com/vi/{video.video_id}/hqdefault.jpg"
    return ""


def _download(url: str) -> bytes:
    """Fetch one image. Returns empty on any failure — artwork is never fatal.

    Kept as its own function so callers (and tests) have a seam for the network
    without having to reach into the HTTP client.
    """
    try:
        import httpx
        with httpx.Client(timeout=15, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            return r.content
    except Exception as e:                      # network, DNS, HTTP, anything
        logger.debug(f"[YouTube] Could not fetch {url}: {e}")
        return b""


def fetch_artwork(video, folder: Path) -> int:
    """Save the video's thumbnail beside its .strm. Best effort.

    Written locally rather than left as a URL so artwork survives the video
    being taken down, and so Jellyfin never has to reach the internet during a
    scan. Failure is not an error: the NFO still names the remote URL.
    """
    url = thumbnail_url(video)
    if not url:
        return 0
    poster, fanart = folder / "poster.jpg", folder / "fanart.jpg"
    if poster.exists() and fanart.exists():
        return 0
    data = _download(url)
    if not data:
        return 0
    written = 0
    # YouTube artwork is 16:9. It stands in for both images: as the poster it
    # is what a row shows, and as the backdrop it fills the detail page.
    for path in (poster, fanart):
        if not path.exists():
            try:
                path.write_bytes(data)
                written += 1
            except OSError as e:
                logger.warning(f"[YouTube] Could not write {path.name}: {e}")
    return written


def strm_url(base_url: str, video_id: str) -> str:
    """What goes inside the .strm — always Tentacle, never a googlevideo URL.

    Google's URLs expire in about six hours and are signed to the IP that
    fetched them, so a client playing one directly gets a 403.
    """
    return f"{base_url.rstrip('/')}/api/youtube/v/{video_id}/master.m3u8"


def write_video(video, channel, base_url: str, root: Path = None) -> dict:
    """Create the folder, .strm and NFO for one video. Idempotent."""
    folder = video_folder(channel.title, video, root)
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder.name
    strm = folder / f"{stem}.strm"
    nfo = folder / "movie.nfo"

    wrote_strm = False
    if not strm.exists():
        strm.write_text(strm_url(base_url, video.video_id), encoding="utf-8")
        wrote_strm = True

    # The NFO is safe to refresh — only the .strm's mtime matters for segments.
    nfo.write_text(build_nfo(video, channel, base_url), encoding="utf-8")
    art = fetch_artwork(video, folder)

    video.folder_path = str(folder)
    video.strm_path = str(strm)
    return {"folder": str(folder), "strm_written": wrote_strm, "artwork": art}


def remove_video(video) -> int:
    """Delete one video's own folder. Never touches a shared parent."""
    deleted = 0
    folder = Path(video.folder_path) if video.folder_path else None
    if not folder or not folder.is_dir():
        return 0
    try:
        for f in sorted(folder.rglob("*"), reverse=True):
            if f.is_file():
                f.unlink()
                deleted += 1
        folder.rmdir()
    except OSError as e:
        logger.warning(f"[YouTube] Could not remove {folder}: {e}")
    return deleted
