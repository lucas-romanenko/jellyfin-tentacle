"""What the music pages show: search results, album, artist and song pages.

Results come from MusicBrainz (cached, one request per second) and are marked
with their status in the library from Tentacle's snapshot of Lidarr.
"""
import itertools
import re
import threading
import unicodedata
from typing import Optional

from sqlalchemy.orm import Session

from models.database import MusicAlbum, MusicArtist
from services.music import library, original as rule
from services.musicbrainz import MusicBrainz

STUDIO = "album"
# MusicBrainz scores search results relative to the best match (always 100): for
# "big star radio city" Big Star scores 100 and "Radio Star" 87; for "led
# zeppelin" the tribute bands score 75 and below.
MIN_ARTIST_SCORE, MAX_ARTISTS, MIN_ALBUM_SCORE = 95, 5, 60
_search_seq = itertools.count(1)
_latest_search = {}
_latest_lock = threading.Lock()


class StaleSearch(Exception):
    """A newer search from the same person replaced this one; stop asking MusicBrainz."""


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[‘’`´]", "'", text).lower()
    return " ".join(re.sub(r"[^\w\s']", " ", text).replace("'", "").split())


def _credit(entity: dict) -> tuple:
    credits = entity.get("artist-credit") or []
    name = "".join((c.get("name") or c.get("artist", {}).get("name", "")) + (c.get("joinphrase") or "")
                   for c in credits)
    first = (credits[0].get("artist") or {}) if credits else {}
    return name, first.get("id") or ""


def _has_official(rg: dict) -> bool:
    """A search result with at least one official release (bootleg-only albums share
    titles with the real ones: a 1992 bootleg "Hotel California" by the Eagles)."""
    releases = rg.get("releases")
    return not releases or any((r.get("status") or "").lower() == "official" for r in releases)


def _is_studio(rg: dict) -> bool:
    return (rg.get("primary-type") or "").lower() == STUDIO and not rg.get("secondary-types")


def _is_album_for_song(rg: dict) -> bool:
    """Where a song "came out": a studio album, or the artist's own soundtrack album
    (Purple Rain). Releases credited to others (Various Artists) are left out by the caller."""
    return (rg.get("primary-type") or "").lower() == STUDIO and \
        set(t.lower() for t in rg.get("secondary-types") or []) <= {"soundtrack"}


def _type_label(rg: dict) -> str:
    secondary = rg.get("secondary-types") or []
    return secondary[0] if secondary else (rg.get("primary-type") or "Other")


def _statuses(db: Session, mbids: list) -> dict:
    rows = db.query(MusicAlbum).filter(MusicAlbum.mbid.in_(mbids)).all() if mbids else []
    try:
        client = library.lidarr_client(db)
    except library.MusicUnavailable:
        client = None
    queue_ids, progress = library.queued(client) if rows else (set(), {})
    return {r.mbid: (r, library.status_of(r, queue_ids), progress.get(r.lidarr_album_id)) for r in rows}


def _cover(rgid: str, size: int = 250) -> str:
    return f"https://coverartarchive.org/release-group/{rgid}/front-{size}"


# ── Search ───────────────────────────────────────────────────────────────

def search(db: Session, q: str, who: str = "") -> dict:
    """Artists, albums and songs for a search box. Three MusicBrainz requests;
    a newer search from the same person stops an older one between them."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"artists": [], "albums": [], "songs": []}
    token = next(_search_seq)
    with _latest_lock:
        _latest_search[who] = token

    def still_current():
        if _latest_search.get(who) != token:
            raise StaleSearch()

    mb = MusicBrainz.from_settings(db)
    artists = mb.search_artists(q)
    still_current()
    groups = mb.search_release_groups(q)
    still_current()
    recordings = mb.search_recordings(q)

    # Weak matches would push the albums out of view.
    artists = [a for a in artists if int(a.get("score") or 0) >= MIN_ARTIST_SCORE][:MAX_ARTISTS] or artists[:1]
    groups = [g for g in groups if int(g.get("score") or 0) >= MIN_ALBUM_SCORE and _has_official(g)] or groups[:3]
    known_artists = {a.mbid for a in db.query(MusicArtist).filter(
        MusicArtist.mbid.in_([a["id"] for a in artists])).all()} if artists else set()
    statuses = _statuses(db, [g["id"] for g in groups])

    songs, seen = [], set()
    for rec in recordings:
        artist_name, artist_id = _credit(rec)
        key = (normalize(rec.get("title")), artist_id)
        if key in seen or not artist_id:
            continue
        seen.add(key)
        songs.append({"title": rec.get("title"), "artist": artist_name, "artist_mbid": artist_id,
                      "recording_mbid": rec.get("id"), "length": rec.get("length")})
    return {
        "artists": [{"mbid": a["id"], "name": a.get("name"), "disambiguation": a.get("disambiguation") or "",
                     "type": a.get("type") or "", "country": a.get("country") or "",
                     "in_library": a["id"] in known_artists} for a in artists],
        "albums": [_album_card(g, statuses) for g in groups],
        "songs": songs[:10],
    }


def _album_card(rg: dict, statuses: dict, artist_name: str = "", artist_mbid: str = "") -> dict:
    name, aid = _credit(rg)
    row, status, progress = statuses.get(rg["id"], (None, library.AVAILABLE, None))
    return {"mbid": rg["id"], "title": rg.get("title"), "artist": name or artist_name,
            "artist_mbid": aid or artist_mbid, "year": (rg.get("first-release-date") or "")[:4],
            "type": _type_label(rg), "studio": _is_studio(rg), "cover": _cover(rg["id"]),
            "status": status, "progress": progress}


# ── Album page ───────────────────────────────────────────────────────────

def _tracklist(mb: MusicBrainz, release_id: str) -> list:
    release = mb.release(release_id)
    discs = []
    for medium in release.get("media") or []:
        discs.append({"position": medium.get("position"), "format": medium.get("format") or "",
                      "tracks": [{"number": t.get("number") or t.get("position"), "title": t.get("title"),
                                  "length": t.get("length"),
                                  "recording_mbid": (t.get("recording") or {}).get("id")}
                                 for t in medium.get("tracks") or []]})
    return discs


def _release_card(r: dict) -> dict:
    formats = "+".join(dict.fromkeys(f for f in rule.media_formats(r) if f))
    return {"id": r.get("id"), "title": r.get("title"), "date": r.get("date") or "", "country": r.get("country") or "",
            "format": formats, "discs": rule.disc_count(r), "tracks": rule.track_count(r),
            "disambiguation": r.get("disambiguation") or ""}


def album_page(db: Session, rgid: str) -> dict:
    mb = MusicBrainz.from_settings(db)
    prefs = rule.Prefs.from_settings(db)
    rg = mb.release_group(rgid)
    releases = mb.release_group_releases(rgid)
    name, artist_id = _credit(rg)
    statuses = _statuses(db, [rgid])
    row, status, progress = statuses.get(rgid, (None, library.AVAILABLE, None))
    page = {"mbid": rgid, "title": rg.get("title"), "artist": name, "artist_mbid": artist_id,
            "year": (rg.get("first-release-date") or "")[:4], "type": _type_label(rg), "studio": _is_studio(rg),
            "cover": _cover(rgid, 500), "status": status, "progress": progress,
            "in_lidarr": bool(row and row.lidarr_album_id), "original": None, "release": None, "review": None,
            "verdict": (row.verdict if row else None) or None, "players": []}
    found = rule.find_original(releases)
    if isinstance(found, rule.Ambiguous):
        options = []
        for opt in found.options[:3]:
            candidates = [r for r in releases if rule.track_count(r) == opt["tracks"]]
            best = min(candidates, key=lambda r: (r.get("date") or "9999", r.get("id")))
            options.append(dict(opt, release=_release_card(best), tracklist=_tracklist(mb, best["id"])))
        page["review"] = {"reason": found.reason, "message": found.message, "options": options}
    else:
        rep = rule.representative_release(found, prefs, rg.get("title") or "")
        page["original"] = {"year": found.year, "tracks": found.tracks, "discs": found.discs}
        page["release"] = dict(_release_card(rep), tracklist=_tracklist(mb, rep["id"]))
    if row and row.lidarr_album_id:
        from services.music.players import enabled_players
        page["players"] = [{"id": p.id, "name": p.name,
                            "url": f"/api/music/open/{rgid}?player={p.id}"} for p in enabled_players(db)]
    return page


# ── Artist page ──────────────────────────────────────────────────────────

def artist_page(db: Session, mbid: str) -> dict:
    mb = MusicBrainz.from_settings(db)
    artist = mb.artist(mbid)
    groups = mb.artist_release_groups(mbid)
    statuses = _statuses(db, [g["id"] for g in groups])
    cards = [_album_card(g, statuses, artist.get("name") or "", mbid) for g in groups]
    cards.sort(key=lambda c: (c["year"] or "9999", c["title"] or ""))
    in_lidarr = db.query(MusicArtist).filter(MusicArtist.mbid == mbid).first() is not None
    life = artist.get("life-span") or {}
    return {"mbid": mbid, "name": artist.get("name"), "disambiguation": artist.get("disambiguation") or "",
            "type": artist.get("type") or "", "country": artist.get("country") or "",
            "years": "–".join(y for y in ((life.get("begin") or "")[:4], (life.get("end") or "")[:4]) if y),
            "in_lidarr": in_lidarr,
            "studio": [c for c in cards if c["studio"]],
            "other": [c for c in cards if not c["studio"]]}


# ── Song ─────────────────────────────────────────────────────────────────

def same_song(title: str, wanted: str) -> bool:
    """A recording's title is the song (already normalized); a medley counts for its first
    song: "Black Magic Woman / Gypsy Queen" is on Abraxas as one track."""
    return normalize(title) == wanted or normalize((title or "").split(" / ")[0]) == wanted


def related_artists(mb: MusicBrainz, mbid: str) -> list:
    """The artist plus the bands it is a member of, so a song credited to "Jimi Hendrix"
    also finds its album by "The Jimi Hendrix Experience". Not a band's members: their
    solo records aren't the band's ("Paint It, Black" isn't on a Charlie Watts album)."""
    ids = [mbid]
    for rel in mb.artist(mbid).get("relations") or []:
        other = (rel.get("artist") or {}).get("id")
        if (rel.get("type") == "member of band" and rel.get("direction", "forward") == "forward"
                and other and other not in ids):
            ids.append(other)
    return ids


def original_album_for_song(mb: MusicBrainz, title: str, artist_mbid: str) -> dict:
    """The studio album a song first appeared on: the earliest official release
    group of type Album with no secondary types (compilation, live, soundtrack...)."""
    artists = related_artists(mb, artist_mbid)
    found = original_from_recordings(mb.recordings_by(title, artists, albums_only=True), title, artists)
    if found["album"]:
        return found
    # No album: all its recordings, to tell "only on singles" from "unknown".
    return original_from_recordings(mb.recordings_by(title, artists), title, artists)


def original_from_recordings(recordings: list, title: str, artists: list) -> dict:
    """original_album_for_song's rule over recordings already fetched (Discover fetches
    several songs' recordings per request)."""
    wanted = normalize(title)
    recordings = [r for r in recordings if same_song(r.get("title"), wanted)]
    studio, singles = {}, {}
    for rec in recordings:
        for rel in rec.get("releases") or []:
            if (rel.get("status") or "").lower() != "official":
                continue
            # Only the artist's own records: a charity album credited to Various
            # Artists isn't the album "Tennessee Whiskey" came out on (Traveller is).
            credited = {(c.get("artist") or {}).get("id") for c in rel.get("artist-credit") or []} - {None}
            if credited and not credited.intersection(artists):
                continue
            rg = rel.get("release-group") or {}
            date = rel.get("date") or "9999"
            bucket = studio if _is_album_for_song(rg) else (
                singles if (rg.get("primary-type") or "") in ("Single", "EP") and not rg.get("secondary-types")
                else None)
            if bucket is None or not rg.get("id"):
                continue
            if rg["id"] not in bucket or date < bucket[rg["id"]]["date"]:
                bucket[rg["id"]] = {"id": rg["id"], "title": rg.get("title") or rel.get("title"),
                                    "type": rg.get("primary-type"), "date": date, "recording": rec.get("id")}
    first = min(studio.values(), key=lambda g: (g["date"], g["title"] or "")) if studio else None
    return {"album": first, "recordings": sorted({r.get("id") for r in recordings}),
            "singles": sorted(singles.values(), key=lambda g: g["date"])[:5]}


def song_page(db: Session, title: str, artist_mbid: str) -> dict:
    mb = MusicBrainz.from_settings(db)
    found = original_album_for_song(mb, title, artist_mbid)
    out = {"title": title, "artist_mbid": artist_mbid, "highlight": {"title": normalize(title),
                                                                      "recordings": found["recordings"]}}
    if found["album"]:
        out["album"] = album_page(db, found["album"]["id"])
        return out
    if found["singles"]:
        names = ", ".join(f"{s['title']} ({s['type']}, {s['date'][:4]})" for s in found["singles"])
        out["message"] = (f"“{title}” isn't on any studio album. It was released only on singles or EPs: "
                          f"{names}.")
        out["singles"] = found["singles"]
    else:
        out["message"] = f"MusicBrainz has no official studio album, single or EP with “{title}”."
    return out
