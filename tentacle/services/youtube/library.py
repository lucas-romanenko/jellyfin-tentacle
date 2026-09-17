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
    if channel.rating:
        parts.append(f"  <mpaa>{escape(channel.rating)}</mpaa>")
    parts += [f"  <tag>{escape(t)}</tag>" for t in tags]
    parts += [
        f'  <uniqueid type="youtube" default="true">{escape(video.video_id)}</uniqueid>',
        "  <lockdata>true</lockdata>",
        "</movie>",
    ]
    return "\n".join(parts)


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

    video.folder_path = str(folder)
    video.strm_path = str(strm)
    return {"folder": str(folder), "strm_written": wrote_strm}


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
