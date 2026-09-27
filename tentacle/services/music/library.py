"""The music library as Tentacle sees it: a snapshot of Lidarr's artists and
albums (music_artists / music_albums), each monitored album with a verdict from
the original-release rule.

Lidarr stays the truth. The snapshot is refreshed one artist at a time (by the
daily reconcile, the webhook, and after a request), so the Library page never
has to read Lidarr's whole library to draw itself.
"""
import logging
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models.database import MusicAlbum, MusicArtist, get_setting
from services.lidarr import LidarrClient, LidarrError
from services.music import original as rule

logger = logging.getLogger(__name__)

# Album statuses shown in the Library and on album/artist pages.
IN_LIBRARY = "in_library"
DOWNLOADING = "downloading"
WANTED = "wanted"
NEEDS_REVIEW = "needs_review"
AVAILABLE = "available"      # not requested (not in Lidarr, or not monitored)


class MusicUnavailable(Exception):
    """The music module can't do this as configured. `message` is for the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def lidarr_client(db: Session) -> LidarrClient:
    url, key = get_setting(db, "lidarr_url"), get_setting(db, "lidarr_api_key")
    if not url or not key:
        raise MusicUnavailable("Connect Lidarr first (Settings → Connections).")
    return LidarrClient(url, key)


def _cover(album: dict) -> str:
    for img in album.get("images") or []:
        if (img.get("coverType") or "").lower() == "cover" and img.get("remoteUrl"):
            return img["remoteUrl"]
    return f"https://coverartarchive.org/release-group/{album.get('foreignAlbumId')}/front-250"


def upsert_artist(db: Session, a: dict) -> Optional[MusicArtist]:
    mbid = a.get("foreignArtistId") or (a.get("artistMetadata") or {}).get("foreignArtistId")
    if not mbid:
        return None
    row = db.query(MusicArtist).filter(MusicArtist.mbid == mbid).first() or MusicArtist(mbid=mbid)
    row.lidarr_artist_id = a.get("id")
    row.name = a.get("artistName") or row.name or ""
    row.sort_name = a.get("sortName") or row.sort_name or ""
    row.disambiguation = a.get("disambiguation") or ""
    row.path = a.get("path") or row.path or ""
    row.updated_at = datetime.utcnow()
    db.add(row)
    return row


def upsert_album(db: Session, album: dict, artist: Optional[MusicArtist] = None) -> Optional[MusicAlbum]:
    mbid = album.get("foreignAlbumId")
    if not mbid:
        return None
    row = db.query(MusicAlbum).filter(MusicAlbum.mbid == mbid).first() or MusicAlbum(mbid=mbid)
    stats = album.get("statistics") or {}
    embedded = album.get("artist") or {}
    row.lidarr_album_id = album.get("id")
    row.lidarr_artist_id = album.get("artistId") or embedded.get("id") or row.lidarr_artist_id
    if artist is not None:
        row.artist_mbid, row.artist_name = artist.mbid, artist.name
    elif embedded:
        row.artist_mbid = embedded.get("foreignArtistId") or row.artist_mbid
        row.artist_name = embedded.get("artistName") or row.artist_name
    row.title = album.get("title") or row.title or ""
    row.album_type = album.get("albumType") or ""
    row.secondary_types = ",".join(str(t) for t in album.get("secondaryTypes") or [])
    row.release_date = (album.get("releaseDate") or "")[:10]
    row.monitored = bool(album.get("monitored"))
    row.any_release_ok = bool(album.get("anyReleaseOk", True))
    row.track_count = int(stats.get("trackCount") or 0)
    row.track_file_count = int(stats.get("trackFileCount") or 0)
    row.size_on_disk = int(stats.get("sizeOnDisk") or 0)
    row.cover_url = _cover(album)
    row.updated_at = datetime.utcnow()
    db.add(row)
    return row


def sync_artist(db: Session, client: LidarrClient, lidarr_artist: dict) -> list:
    """Refresh one artist and all its albums. Returns [(row, lidarr_album)]."""
    artist = upsert_artist(db, lidarr_artist)
    albums = client.albums_by_artist(lidarr_artist["id"])
    out, seen = [], set()
    for a in albums:
        row = upsert_album(db, a, artist)
        if row is not None:
            out.append((row, a))
            seen.add(row.mbid)
    # Albums Lidarr no longer has for this artist.
    for gone in db.query(MusicAlbum).filter(MusicAlbum.lidarr_artist_id == lidarr_artist["id"]).all():
        if gone.mbid not in seen:
            db.delete(gone)
    db.commit()
    return out


def sync_album(db: Session, client: LidarrClient, album_id: int) -> tuple:
    """Refresh one album (after a request, a pin or an import). Returns (row, lidarr_album)."""
    album = client.album(album_id)
    artist = upsert_artist(db, album.get("artist") or {}) if album.get("artist") else None
    row = upsert_album(db, album, artist)
    db.commit()
    return row, album


def check_album(db: Session, row: MusicAlbum, album: dict, mb, prefs: rule.Prefs) -> rule.Verdict:
    """Run the original-release rule on one Lidarr album and store the verdict."""
    from services.musicbrainz import MusicBrainzError
    try:
        releases = mb.release_group_releases(row.mbid)
    except MusicBrainzError as e:
        row.check_error = e.message
        db.commit()
        raise
    verdict = rule.evaluate(album, releases, prefs)
    row.category = verdict.category
    row.verdict = verdict.to_dict()
    row.checked_at = datetime.utcnow()
    row.check_error = None
    db.commit()
    return verdict


# ── Statuses ─────────────────────────────────────────────────────────────

_queue_cache = {"at": 0.0, "ids": set(), "progress": {}}
QUEUE_TTL = 15


def queued(client: Optional[LidarrClient]) -> tuple:
    """(album ids in Lidarr's download queue, {album id: percent}), cached briefly."""
    if client is None:
        return set(), {}
    if time.monotonic() - _queue_cache["at"] < QUEUE_TTL:
        return _queue_cache["ids"], _queue_cache["progress"]
    try:
        records = client.queue()
    except LidarrError as e:
        logger.debug(f"[Music] queue read failed: {e.message}")
        records = []
    ids, progress = set(), {}
    for r in records:
        aid = r.get("albumId")
        if not aid:
            continue
        ids.add(aid)
        size, left = r.get("size") or 0, r.get("sizeleft") or 0
        if size:
            progress[aid] = max(progress.get(aid, 0), round(100 * (size - left) / size))
    _queue_cache.update(at=time.monotonic(), ids=ids, progress=progress)
    return ids, progress


def status_of(row: Optional[MusicAlbum], queue_ids: set = frozenset()) -> str:
    if row is None or not row.monitored:
        return AVAILABLE
    if row.category == rule.REVIEW:
        return NEEDS_REVIEW
    if row.lidarr_album_id in queue_ids:
        return DOWNLOADING
    if row.track_count and row.track_file_count >= row.track_count:
        return IN_LIBRARY
    return WANTED


def album_summary(row: MusicAlbum, queue_ids: set = frozenset(), progress: Optional[dict] = None) -> dict:
    verdict = row.verdict or {}
    return {
        "mbid": row.mbid, "title": row.title, "artist": row.artist_name, "artist_mbid": row.artist_mbid,
        "year": (row.release_date or "")[:4], "type": row.album_type,
        "secondary_types": [t for t in (row.secondary_types or "").split(",") if t],
        "cover": row.cover_url, "status": status_of(row, queue_ids),
        "progress": (progress or {}).get(row.lidarr_album_id),
        "tracks": row.track_count, "files": row.track_file_count,
        "category": row.category or None, "message": verdict.get("message") or verdict.get("state") or "",
        "reason": verdict.get("reason") or "", "options": verdict.get("options") or [],
        "original": verdict.get("original"), "requested_at": row.requested_at.isoformat() if row.requested_at else None,
    }
