"""Spotify playlists → the original studio albums behind their songs.

Two ways in; neither needs a Spotify account or key:
- a public playlist link: Tentacle reads the playlist's embed page, which lists
  its first 100 songs (title and artist);
- an Exportify CSV (exportify.net), for playlists of any size, private ones too.

Each song is resolved to the studio album it first came out on (discover.Resolver):
the export's album name when it is a studio album, else the albums Deezer has
the song on, checked through Lidarr's metadata server, else MusicBrainz's
recordings. Artist credits are tried whole first, then by their first name, so
"Simon & Garfunkel" stays one artist while "A & B" finds A; compilations, live
albums, soundtracks and singles never count. Nothing is requested until the user ticks albums in the
preview, and each one then goes through the single request path.
"""
import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

import requests
from sqlalchemy.orm.attributes import flag_modified

from models.database import MusicImport
from services.music import worker
from services.music.browse import _cover, normalize

logger = logging.getLogger(__name__)

EMBED_URL = "https://open.spotify.com/embed/playlist/{id}"
TIMEOUT = 15
MAX_TRACKS = 2000
CHUNK = 25   # songs per worker turn: other waiting jobs (Discover, requests) run in between
_PLAYLIST_ID = re.compile(r"playlist[/:]([A-Za-z0-9]{22})")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.S)

_active = set()   # import ids with a resolve job queued or running


class SpotifyImportError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ── Reading a playlist ───────────────────────────────────────────────────

def playlist_id(url: str) -> str:
    m = _PLAYLIST_ID.search(url or "")
    if not m:
        raise SpotifyImportError("That isn't a Spotify playlist link (open.spotify.com/playlist/…).")
    return m.group(1)


def fetch_playlist(url: str) -> tuple:
    """(name, songs) of a public playlist, from its embed page: the first 100 songs."""
    pid = playlist_id(url)
    try:
        r = requests.get(EMBED_URL.format(id=pid), timeout=TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; Tentacle)"})
    except requests.RequestException as e:
        raise SpotifyImportError(f"Couldn't reach Spotify ({e.__class__.__name__}). Try again later.", 502)
    if r.status_code in (400, 404):
        raise SpotifyImportError("Spotify says that playlist doesn't exist or isn't public.")
    if r.status_code >= 400:
        raise SpotifyImportError(f"Spotify answered HTTP {r.status_code}. Try again later.", 502)
    m = _NEXT_DATA.search(r.text)
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]
        if props.get("status") in (400, 403, 404) and not props.get("state"):
            # The page answers 200 with {"status": 404} for a missing or private playlist.
            raise SpotifyImportError("Spotify says that playlist doesn't exist or isn't public.")
        entity = props["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SpotifyImportError("Spotify's playlist page has changed and Tentacle can't read it. Export the "
                                 "playlist with Exportify (exportify.net) and upload the CSV instead.", 502)
    songs = [{"title": t["title"], "artist": (t.get("subtitle") or "").replace(" ", " ")}
             for t in entity.get("trackList") or []
             if t.get("title") and t.get("subtitle") and (t.get("entityType") or "track") == "track"]
    if not songs:
        raise SpotifyImportError("That playlist has no songs Tentacle can read.")
    return entity.get("name") or "Spotify playlist", songs


def parse_exportify(data: bytes, filename: str = "") -> tuple:
    """(name, songs) from an Exportify CSV. Column names have changed over the years,
    so they are matched loosely."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    columns = {normalize(c): c for c in reader.fieldnames or []}

    def column(*names):
        return next((columns[normalize(n)] for n in names if normalize(n) in columns), None)

    title_col = column("Track Name", "Name", "Title")
    artist_col = column("Artist Name(s)", "Artist Names", "Artist Name", "Artists", "Artist")
    album_col = column("Album Name", "Album")
    album_artist_col = column("Album Artist Name(s)", "Album Artist Names", "Album Artist Name", "Album Artist")
    if not title_col or not artist_col:
        raise SpotifyImportError("That file has no track name and artist columns. Export the playlist with "
                                 "Exportify (exportify.net) and upload the CSV it gives you.")
    songs = []
    for row in reader:
        title, artist = (row.get(title_col) or "").strip(), (row.get(artist_col) or "").strip()
        if title and artist:
            songs.append({"title": title, "artist": artist,
                          "album": (row.get(album_col) or "").strip() if album_col else "",
                          "album_artist": (row.get(album_artist_col) or "").strip() if album_artist_col else ""})
    if not songs:
        raise SpotifyImportError("That file has no songs in it.")
    name = os.path.splitext(os.path.basename(filename or ""))[0].replace("_", " ").strip()
    return name or "Spotify playlist", songs


def _dedupe(songs: list) -> list:
    out, seen = [], set()
    for s in songs:
        key = (normalize(s["title"]), normalize(s["artist"]))
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out[:MAX_TRACKS]


# ── Importing ────────────────────────────────────────────────────────────

def start_import(db, user_id: Optional[int], name: str, source: str, url: str, songs: list) -> MusicImport:
    songs = _dedupe(songs)
    imp = MusicImport(user_id=user_id, name=name[:200], source=source, url=url or "", status="resolving",
                      done=0, total=len(songs), tracks=[dict(s, result=None) for s in songs], outcomes={})
    db.add(imp)
    db.commit()
    _submit(imp.id)
    return imp


def refresh_import(db, imp: MusicImport) -> MusicImport:
    """Read a linked playlist again: new songs are resolved, known ones keep their result."""
    if imp.source != "spotify_url" or not imp.url:
        raise SpotifyImportError("Only playlists imported from a link can be refreshed. Upload a new export instead.")
    name, songs = fetch_playlist(imp.url)
    known = {(normalize(t["title"]), normalize(t["artist"])): t for t in imp.tracks or []}
    tracks = [known.get((normalize(s["title"]), normalize(s["artist"]))) or dict(s, result=None)
              for s in _dedupe(songs)]
    imp.name, imp.tracks, imp.total = name[:200], tracks, len(tracks)
    imp.done = sum(1 for t in tracks if t.get("result") is not None)
    imp.status, imp.error, imp.updated_at = "resolving", None, datetime.utcnow()
    flag_modified(imp, "tracks")
    db.commit()
    _submit(imp.id)
    return imp


def _submit(import_id: int) -> None:
    if import_id in _active:
        return
    _active.add(import_id)
    worker.submit(resolve_job(import_id), worker.NORMAL, f"Spotify import #{import_id}")


def resolve_job(import_id: int):
    """Worker job: resolve every song not resolved yet (so it resumes after a restart)."""
    def job(db):
        from services.music.discover import Resolver
        from services.musicbrainz import MusicBrainz, MusicBrainzError
        requeued = False
        try:
            imp = db.get(MusicImport, import_id)
            if not imp:
                return
            resolver = Resolver(db, MusicBrainz.from_settings(db))
            tracks = [dict(t) for t in imp.tracks or []]
            resolved_now = 0
            for i, t in enumerate(tracks):
                if t.get("result") is not None:
                    continue
                if resolved_now == 0:
                    # This turn's songs: which albums they're on, matched in batches.
                    pending = [x for x in tracks[i:] if x.get("result") is None][:CHUNK]
                    resolver.prepare_songs([(x["title"], x["artist"], x.get("album") or "",
                                             x.get("album_artist") or "") for x in pending])
                if resolved_now >= CHUNK:
                    # Back in line, so a long playlist doesn't hold up everything else.
                    _store(db, imp, tracks)
                    worker.submit(resolve_job(import_id), worker.NORMAL, f"Spotify import #{import_id}")
                    requeued = True
                    return
                resolved_now += 1
                worker.run_urgent_jobs()
                try:
                    t["result"] = resolver.song(t["title"], t["artist"], t.get("album") or "",
                                                t.get("album_artist") or "")
                except MusicBrainzError as e:
                    if e.status != 404:
                        imp.status, imp.error = "error", f"MusicBrainz: {e.message}"
                        _store(db, imp, tracks)
                        return
                    t["result"] = {"reason": "MusicBrainz doesn't know this song."}
                if i % 5 == 4:
                    _store(db, imp, tracks)
            imp.status, imp.error = "ready", None
            _store(db, imp, tracks)
            logger.info(f"[Music] Spotify import '{imp.name}': {imp.done} songs resolved")
        finally:
            if not requeued:
                _active.discard(import_id)
    return job


def _store(db, imp: MusicImport, tracks: list) -> None:
    imp.tracks = [dict(t) for t in tracks]
    imp.done = sum(1 for t in tracks if t.get("result") is not None)
    imp.updated_at = datetime.utcnow()
    flag_modified(imp, "tracks")
    db.commit()


def request_job(import_id: int, rgids: list, user_id: Optional[int]):
    """Worker job: request the ticked albums, one at a time, through the single request path."""
    def job(db):
        from services.media_requests import RequestRefused, request_album
        imp = db.get(MusicImport, import_id)
        if not imp:
            return
        outcomes = dict(imp.outcomes or {})
        for rgid in rgids:
            worker.run_urgent_jobs()
            try:
                request_album(db, rgid, user_id=user_id, via=f"the Spotify import “{imp.name}”")
                outcomes[rgid] = "requested"
            except RequestRefused as e:
                outcomes[rgid] = e.message
            imp.outcomes = dict(outcomes)
            flag_modified(imp, "outcomes")
            db.commit()
    return job


# ── What the pages show ──────────────────────────────────────────────────

def albums_of(imp: MusicImport) -> tuple:
    """(albums, skipped songs) of an import: albums with the playlist's songs on them."""
    from services.music.discover import clean_title
    albums, skipped = {}, []
    for t in imp.tracks or []:
        result = t.get("result")
        if result is None:
            continue
        album = result.get("album")
        if album:
            entry = albums.setdefault(album["mbid"], dict(album, cover=album.get("cover") or _cover(album["mbid"]),
                                                          songs=[]))
            entry["songs"].append(clean_title(t["title"]))
        else:
            skipped.append({"title": t["title"], "artist": t["artist"], "reason": result.get("reason") or ""})
    ordered = sorted(albums.values(), key=lambda a: (-len(a["songs"]), (a["artist"] or "").lower(), a["title"]))
    return ordered, skipped


def _visible(db, user):
    q = db.query(MusicImport)
    return q if user is None else q.filter(MusicImport.user_id == user.id)


def get_import(db, user, import_id: int) -> MusicImport:
    imp = db.get(MusicImport, import_id)
    if not imp or (user is not None and imp.user_id != user.id and not getattr(user, "is_admin", False)):
        raise SpotifyImportError("No such import.", 404)
    return imp


def summaries(db, user) -> list:
    out = []
    for imp in _visible(db, user).order_by(MusicImport.created_at.desc()).all():
        if imp.status == "resolving" and imp.id not in _active:
            _submit(imp.id)   # a restart interrupted it
        albums, skipped = albums_of(imp)
        out.append({"id": imp.id, "name": imp.name, "source": imp.source, "status": imp.status,
                    "error": imp.error, "done": imp.done, "total": imp.total, "albums": len(albums),
                    "skipped": len(skipped), "refreshable": imp.source == "spotify_url",
                    "created_at": imp.created_at.isoformat() if imp.created_at else None})
    return out


def albums_for_discover(db, user) -> list:
    """Albums from all the user's imports, most-represented first (status is added by the page)."""
    merged = {}
    for imp in _visible(db, user).all():
        for album in albums_of(imp)[0]:
            entry = merged.setdefault(album["mbid"], dict(album, songs=[], playlists=[]))
            entry["songs"] += album["songs"]
            if imp.name not in entry["playlists"]:
                entry["playlists"].append(imp.name)
    return sorted(merged.values(), key=lambda a: (-len(a["songs"]), (a["artist"] or "").lower()))
