"""Discover → Music: what's trending, what's new, the all-time top albums per
genre, and the albums behind imported Spotify playlists.

Every source is free and needs no key:
- Apple Music's charts (per country, updated daily): the most-played songs and
  albums, and the per-genre album charts, for "Trending" and "New releases";
- ListenBrainz: the all-time most-listened albums (sitewide statistics) for
  "Top of all time", and new releases with MusicBrainz ids for "New from your
  artists";
- MusicBrainz: curated album lists (release group series: Rolling Stone 500,
  1001 Albums...), and types, years and genre tags, 50 albums per request;
- Lidarr's metadata server (through the user's Lidarr) and Deezer's search:
  matching chart names to albums without MusicBrainz's one-request-a-second
  limit (see Resolver); MusicBrainz decides what they can't;
- Deezer: pictures for trending artists (exact name), if it is on as a picture
  source.

Every card is a MusicBrainz release group, so album pages, status badges and
requests work as everywhere else. The sections are built by the music worker
(requests always go first) and kept in {data_dir}/music_discover.json: trending
and new releases about daily, the all-time list and the lists monthly. The page
reads that file and adds each album's status in the library.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date, timedelta
from typing import Optional

import requests

from models.database import MusicArtist, get_setting
from services.music import worker
from services.music.browse import (_cover, _credit, _is_album_for_song, _is_studio, _statuses, normalize,
                                   original_album_for_song, original_from_recordings, same_song)
from services.musicbrainz import MusicBrainz, MusicBrainzError

logger = logging.getLogger(__name__)

TIMEOUT = 15
USER_AGENT = "Tentacle/1.0 (+https://github.com/lucas-romanenko/jellyfin-tentacle)"
APPLE_CHART = "https://rss.marketingtools.apple.com/api/v2/{country}/music/most-played/{limit}/{kind}.json"
ITUNES_GENRE_CHART = "https://itunes.apple.com/{country}/rss/topalbums/limit={limit}/genre={genre}/json"
LISTENBRAINZ = "https://api.listenbrainz.org/1"

TRENDING_MAX_AGE = 20 * 3600       # rebuilt about once a day
ALL_TIME_MAX_AGE = 30 * 86400      # listening-history charts move slowly
RETRY_AFTER_FAILURE = 15 * 60      # a failed build isn't retried on every page view
NEW_RELEASE_DAYS = 56
TRENDING_SONGS = 50
TRENDING_ARTISTS = 30
NEW_RELEASES_MAX = 150
ALL_TIME_DEPTH = 2000              # how many of ListenBrainz's most-listened albums get sorted into genres
PER_GENRE = 48
MIN_GENRE_ALBUMS = 6              # a genre with fewer isn't worth a tab

# Apple's per-genre album charts (its genre ids); the names are Apple's, as its
# charts label their items.
APPLE_GENRES = [("Rock", 21), ("Alternative", 20), ("Pop", 14), ("Hip-Hop/Rap", 18), ("R&B/Soul", 15),
                ("Country", 6), ("Dance", 17), ("Electronic", 7), ("Singer/Songwriter", 10), ("Jazz", 11),
                ("Blues", 2), ("Metal", 1153), ("Latin", 12), ("Reggae", 24), ("Classical", 5)]

# "Top of all time": MusicBrainz genre tags → the genres Discover shows, in this
# order. A genre name's last word is what it is: "rap metal" is metal, "baroque
# pop" is pop, "blues rock" is rock. Names whose last word misleads are listed.
FAMILY_ORDER = ["Rock", "Pop", "Hip hop", "Electronic", "Metal", "Punk", "R&B and soul", "Folk", "Country",
                "Jazz", "Blues", "Reggae", "Latin", "Classical"]
_WHOLE_NAMES = {
    "hip hop": "Hip hop", "trip hop": "Electronic", "drum and bass": "Electronic", "uk garage": "Electronic",
    "new wave": "Rock", "post-punk": "Rock", "rock opera": "Rock", "punk rock": "Punk", "singer-songwriter": "Folk",
    "r&b": "R&B and soul", "rhythm and blues": "R&B and soul", "bossa nova": "Jazz", "jazz fusion": "Jazz",
    "big band": "Jazz", "synthpop": "Pop", "electropop": "Pop", "k-pop": "Pop", "j-pop": "Pop",
}
_LAST_WORD = {
    **dict.fromkeys(("rock", "grunge", "shoegaze", "britpop", "emo"), "Rock"),
    "pop": "Pop",
    **dict.fromkeys(("rap", "trap", "grime", "drill"), "Hip hop"),
    **dict.fromkeys(("electronic", "electronica", "house", "techno", "trance", "ambient", "dubstep", "idm",
                     "synthwave", "electro", "downtempo", "edm", "breakbeat", "jungle"), "Electronic"),
    **dict.fromkeys(("metal", "metalcore"), "Metal"),
    **dict.fromkeys(("punk", "hardcore"), "Punk"),
    **dict.fromkeys(("soul", "funk", "disco", "motown"), "R&B and soul"),
    "folk": "Folk",
    **dict.fromkeys(("country", "bluegrass", "americana"), "Country"),
    **dict.fromkeys(("jazz", "bebop", "swing"), "Jazz"),
    "blues": "Blues",
    **dict.fromkeys(("reggae", "ska", "dub", "dancehall", "rocksteady"), "Reggae"),
    **dict.fromkeys(("latin", "reggaeton", "salsa", "bachata", "cumbia", "mpb", "samba"), "Latin"),
    **dict.fromkeys(("classical", "baroque", "opera", "symphony", "minimalism"), "Classical"),
}


def family_of(genre: str) -> Optional[str]:
    """The Discover genre of one MusicBrainz genre tag, or None."""
    name = re.sub(r"\s+revival$", "", (genre or "").lower().strip().replace("hip-hop", "hip hop"))
    if name in _WHOLE_NAMES:
        return _WHOLE_NAMES[name]
    for whole in sorted(_WHOLE_NAMES, key=len, reverse=True):   # "west coast hip hop", "alternative r&b"
        if name.endswith((" " + whole, "-" + whole)):
            return _WHOLE_NAMES[whole]
    words = re.split(r"[\s\-]+", name)
    return _LAST_WORD.get(words[-1]) if words else None


_lock = threading.Lock()
jobs = {"trending": False, "all_time": False, "lists": False}   # queued or running
_failed = {"trending": 0.0, "all_time": 0.0, "lists": 0.0}


# ── Sources ──────────────────────────────────────────────────────────────

def _get_json(url: str, params: Optional[dict] = None, retry_after: float = 3):
    """GET as JSON; a timeout or dropped connection is tried once more."""
    try:
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except (requests.Timeout, requests.ConnectionError):
        time.sleep(retry_after)
        r = requests.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _artwork(url: str, size: int = 300) -> str:
    return re.sub(r"/\d+x\d+bb\.(?:jpg|png)$", f"/{size}x{size}bb.jpg", url or "")


def apple_chart(country: str, kind: str, limit: int = 100) -> list:
    """Apple Music's most-played songs or albums in a country."""
    data = _get_json(APPLE_CHART.format(country=country, limit=limit, kind=kind))
    return [{"rank": i + 1, "title": x.get("name") or "", "artist": x.get("artistName") or "",
             "date": (x.get("releaseDate") or "")[:10], "artwork": _artwork(x.get("artworkUrl100")),
             "genres": [g["name"] for g in x.get("genres") or [] if g.get("name") and g["name"] != "Music"]}
            for i, x in enumerate((data.get("feed") or {}).get("results") or [])]


def itunes_genre_albums(country: str, genre_id: int, limit: int = 100) -> list:
    """Apple's album chart for one genre in a country."""
    data = _get_json(ITUNES_GENRE_CHART.format(country=country, limit=limit, genre=genre_id))
    entries = (data.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):   # a chart of one comes as an object
        entries = [entries]
    out = []
    for i, e in enumerate(entries):
        images = e.get("im:image") or []
        genre = ((e.get("category") or {}).get("attributes") or {}).get("label")
        out.append({"rank": i + 1, "title": (e.get("im:name") or {}).get("label") or "",
                    "artist": (e.get("im:artist") or {}).get("label") or "",
                    "date": ((e.get("im:releaseDate") or {}).get("label") or "")[:10],
                    "artwork": _artwork(images[-1].get("label")) if images else "",
                    "genres": [genre] if genre else []})
    return out


def lb_fresh_releases(days: int = 45) -> list:
    """Albums released in the last `days` (and announced ones), with MusicBrainz ids."""
    data = _get_json(f"{LISTENBRAINZ}/explore/fresh-releases/",
                     {"days": days, "past": "true", "future": "true", "sort": "release_date"})
    return [{"rgid": r["release_group_mbid"], "title": r.get("release_name") or "",
             "artist": r.get("artist_credit_name") or "", "artist_mbids": r.get("artist_mbids") or [],
             "date": r.get("release_date") or "", "cover": _caa(r)}
            for r in (data.get("payload") or {}).get("releases") or []
            if (r.get("release_group_primary_type") or "") == "Album" and r.get("release_group_mbid")]


def lb_top_release_groups(offset: int, count: int = 100) -> list:
    data = _get_json(f"{LISTENBRAINZ}/stats/sitewide/release-groups",
                     {"range": "all_time", "count": count, "offset": offset})
    return (data.get("payload") or {}).get("release_groups") or []


# ── Matching chart entries to MusicBrainz ────────────────────────────────

_EXTRA = (r"feat|ft|featuring|with|from|remaster|remastered|version|edit|mix|remix|live|mono|stereo|demo|"
          r"bonus|single|radio|deluxe|expanded|edition|anniversary|explicit|clean|acoustic|instrumental|"
          r"re-recorded|rerecorded|soundtrack|ep")
_TRAIL_BRACKET = re.compile(rf"\s*[(\[][^()\[\]]*\b(?:{_EXTRA})\b[^()\[\]]*[)\]]\s*$", re.I)
_TRAIL_DASH = re.compile(rf"\s+[-–—]\s+(?:[^-–—]*\b(?:{_EXTRA})\b[^-–—]*|\d{{4}})$", re.I)
# Not "/" or "+": they are part of names (AC/DC, HUNTR/X).
_SPLIT = re.compile(r"\s*(?:,|;|&|(?<=\s)x(?=\s)|\band\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b)\s*", re.I)


def clean_title(title: str) -> str:
    """ "Dreams - 2004 Remaster" -> "Dreams"; "Song (feat. X)" -> "Song". A title's own
    parentheses stay: "(Don't Fear) The Reaper"."""
    original = (title or "").replace(" ", " ").strip()
    text, previous = original, None
    while text != previous:
        previous = text
        text = _TRAIL_DASH.sub("", _TRAIL_BRACKET.sub("", text).strip()).strip()
    return text or original


def artist_candidates(credit: str) -> list:
    """Names to look up for a credit: the whole credit first ("Crosby, Stills, Nash &
    Young" is one artist), then its first name ("Karan Aujla & MXRCI")."""
    credit = (credit or "").replace(" ", " ").strip()
    if not credit:
        return []
    first = _SPLIT.split(credit)[0].strip()
    return [credit] + ([first] if first and first != credit else [])


def _bare(name: str) -> str:
    """An artist name for comparing: "Derek & The Dominos" = "Derek and the Dominos"."""
    n = normalize(re.sub(r"\s*&\s*", " and ", name or ""))
    return n[4:] if n.startswith("the ") else n


def resolve_artist(mb: MusicBrainz, name: str) -> Optional[dict]:
    """The MusicBrainz artist with exactly this name (the best-scored one)."""
    for a in mb.search_artists(name, limit=5):
        if int(a.get("score") or 0) >= 90 and _bare(a.get("name")) == _bare(name) != "":
            return {"mbid": a["id"], "name": a.get("name") or name}
    return None


def find_album(mb: MusicBrainz, title: str, artist: str) -> Optional[dict]:
    """The studio album (release group) with this title by this artist, or None when
    MusicBrainz doesn't have it (yet) or it is a single, EP, live album or compilation."""
    wanted = normalize(clean_title(title))
    for name in artist_candidates(artist):
        groups = mb.find_release_groups(clean_title(title), name)
        exact = [g for g in groups if normalize(g.get("title")) == wanted and
                 any(_bare(c.get("name") or (c.get("artist") or {}).get("name") or "") == _bare(name)
                     for c in g.get("artist-credit") or [])]
        if exact:
            best = max(exact, key=lambda g: (_is_studio(g), int(g.get("score") or 0)))
            return best if _is_studio(best) else None
    return None


def resolve_song(mb: MusicBrainz, title: str, artist: str, album: str = "", album_artist: str = "") -> dict:
    """The original studio album of a song: {"album": {...}} or {"reason": why not}.
    The same rule as Discover's song search; `album` (a Spotify export's album
    name) is only a fallback for songs MusicBrainz can't place."""
    raw = (title or "").replace(" ", " ").strip()
    title = clean_title(raw)
    # A trailing part that looked like an extra may belong to the title
    # ("Against All Odds (Take a Look at Me Now)"): then the title as written.
    titles = [title] + ([raw] if raw != title else [])
    reason = f"MusicBrainz has no artist called “{artist}”."
    for name in artist_candidates(artist):
        found_artist = resolve_artist(mb, name)
        if not found_artist:
            continue
        singles = False
        for candidate in titles:
            found = original_album_for_song(mb, candidate, found_artist["mbid"])
            if found["album"]:
                # The album's own first release date and credit: the song's date is
                # its recording's (a 2004 remaster would make Rumours "2004").
                card = _card(mb.release_group(found["album"]["id"]))
                return {"album": {k: card[k] for k in ("mbid", "title", "year", "artist", "artist_mbid")}}
            singles = singles or bool(found["singles"])
        reason = (f"“{title}” came out only on singles or EPs." if singles else
                  f"MusicBrainz has no studio album by {found_artist['name']} with “{title}”.")
        break
    if album:
        rg = find_album(mb, album, album_artist or artist)
        if rg:
            card = _card(rg)
            return {"album": {k: card[k] for k in ("mbid", "title", "year", "artist", "artist_mbid")}}
    return {"reason": reason}


# ── Fast matching: Lidarr's metadata server and Deezer ───────────────────

DEEZER_API = "https://api.deezer.com"
# Albums a song is also on that plainly aren't studio albums: not worth a lookup.
_NOT_STUDIO_TITLE = re.compile(r"\blive (?:at|in|from|on)\b|\(live\b|\blive\)|unplugged|greatest hits|best of|"
                               r"anthology|in concert|\bcollection\b|\bsessions\b", re.I)
SONG_ALBUM_LOOKUPS = 5


def _lidarr_date(album: dict) -> str:
    value = (album.get("releaseDate") or "")[:10]
    return "" if value.startswith("0001") else value


def _lidarr_card(album: dict) -> dict:
    artist = album.get("artist") or {}
    cover = next((i.get("remoteUrl") for i in album.get("images") or []
                  if (i.get("coverType") or "").lower() == "cover" and i.get("remoteUrl")), None)
    return {"mbid": album["foreignAlbumId"], "title": album.get("title") or "",
            "artist": artist.get("artistName") or "", "artist_mbid": artist.get("foreignArtistId") or "",
            "year": _lidarr_date(album)[:4], "cover": cover or _cover(album["foreignAlbumId"])}


class Resolver:
    """Names → MusicBrainz albums and artists, for Discover and playlist imports.

    MusicBrainz allows one request a second, so names are matched in batches:
    one search request finds about eight "title by artist" albums. Lidarr's
    metadata server (MusicBrainz data through the user's Lidarr, no such limit,
    but its search misses albums from the last few weeks) covers what a batch
    missed, and Deezer's search tells which albums a song is on. The rules don't
    change: an album is a studio album with this exact title and artist (among
    same-titled ones, the nearest to a chart date, else the earliest), and a
    song's album is the earliest studio album it is on.

    Call prefetch() / prepare_songs() with everything first, then album() /
    song() per item: those answer from what the batches found.
    """

    BATCH = 8

    def __init__(self, db, mb: MusicBrainz):
        from services.music import library
        self.mb = mb
        try:
            self.lidarr = library.lidarr_client(db)
        except library.MusicUnavailable:
            self.lidarr = None
        self.deezer = (get_setting(db, "deezer_enabled", "true") or "").lower() == "true"
        self.reset()

    def reset(self) -> None:
        self._answers = {}     # Lidarr lookups
        self._albums = {}      # (title, artist, near) -> card, or None for "exists, but isn't a studio album"
        self._missing = set()  # albums a batch searched for and didn't find (not searched again)
        self._songs = {}       # (title, artist) -> album titles Deezer has the song on
        self._song_found = {}  # (title, artist) -> the album card its recordings point to
        self._failures = 0

    @staticmethod
    def _key(title: str, artist: str, near: str = "") -> tuple:
        return normalize(clean_title(title)), _bare(artist), near or ""

    # ── albums ──
    def prefetch(self, wanted: list) -> None:
        """Match many (title, artist, near) albums with MusicBrainz, BATCH per request."""
        todo, seen = [], set()
        for title, artist, near in wanted:
            key = self._key(title, artist, near)
            if title and artist and key not in self._albums and key not in seen:
                seen.add(key)
                todo.append((title, artist, near, key))
        for i in range(0, len(todo), self.BATCH):
            worker.run_urgent_jobs()
            batch = todo[i:i + self.BATCH]
            groups = self.mb.find_release_groups_batch(
                [(clean_title(t), artist_candidates(a)) for t, a, _, _ in batch])
            for title, artist, near, key in batch:
                found = _choose_album(groups, title, artist, near)
                if found is _MISSING:
                    self._missing.add(key)
                else:
                    self._albums[key] = _card(found) if found else None

    def album(self, title: str, artist: str, near: str = "", fallback: bool = True) -> Optional[dict]:
        """The studio album with this title by this artist, as a card; None when there is
        none (or it is a single, EP, live album or compilation)."""
        key = self._key(title, artist, near)
        if key in self._albums:
            return self._albums[key]
        clean = clean_title(title)
        wanted = normalize(clean)
        for name in artist_candidates(artist):
            results = self._lidarr("album", f"{name} {clean}")
            if results is None:
                break
            exact = [a for a in results if a.get("foreignAlbumId") and normalize(a.get("title")) == wanted
                     and _bare((a.get("artist") or {}).get("artistName") or "") == _bare(name)]
            if exact:
                studio = [a for a in exact if (a.get("albumType") or "") == "Album"
                          and set(t.lower() for t in a.get("secondaryTypes") or []) <= {"soundtrack"}]
                self._albums[key] = _lidarr_card(_pick(studio, _lidarr_date, near)) if studio else None
                return self._albums[key]
        if not fallback or key in self._missing:   # a batch already asked MusicBrainz
            return None
        rg = find_album(self.mb, title, artist)
        return _card(rg) if rg else None

    # ── artists ──
    def artist(self, name: str, fallback: bool = True) -> Optional[dict]:
        results = self._lidarr("artist", name)
        match = next((a for a in results or [] if a.get("foreignArtistId")
                      and _bare(a.get("artistName")) == _bare(name) != ""), None)
        if match:
            return {"mbid": match["foreignArtistId"], "name": match.get("artistName") or name}
        return resolve_artist(self.mb, name) if fallback else None

    # ── songs ──
    @staticmethod
    def _song_key(title: str, artist: str) -> tuple:
        return normalize(clean_title(title)), _bare(artist)

    def prepare_songs(self, songs: list) -> None:
        """For many (title, artist, album, album_artist) songs, in batches: the exports'
        album names; then the albums Deezer has each song on (right for most older
        songs); then, for songs still unplaced (a new hit, which Deezer lists as its
        single), their recordings on albums, a few songs per request."""
        self.prefetch([(album, album_artist or artist, "") for _, artist, album, album_artist in songs if album])
        pending = [(t, a) for t, a, album, album_artist in songs
                   if not (album and self._albums.get(self._key(album, album_artist or a)))]
        # Deezer's searches side by side: three at a time stays under its 50 per 5 seconds.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda song: self._song_albums(*song), pending))
        self.prefetch([(variant, a, "") for t, a in pending for album in self._song_albums(t, a)
                       for variant in _title_variants(album)])
        self._prefetch_recordings([(t, a) for t, a in pending if not self._deezer_answer(t, a)])

    def _deezer_answer(self, title: str, artist: str) -> Optional[dict]:
        """The earliest studio album among those Deezer has the song on (as matched so far)."""
        found = {}
        for album_title in self._song_albums(title, artist):
            for variant in _title_variants(album_title):
                card = self.album(variant, artist, fallback=False)
                if card:
                    found.setdefault(card["mbid"], card)
                    break
        return min(found.values(), key=lambda c: c.get("year") or "9999") if found else None

    def _prefetch_recordings(self, songs: list) -> None:
        items = []
        for title, artist in songs:
            key = self._song_key(title, artist)
            if key in self._song_found or key in {i[2] for i in items}:
                continue
            found = next(filter(None, (self.artist(n, fallback=False) for n in artist_candidates(artist))), None)
            if found:
                items.append((clean_title(title), found, key))
        batches = [items[i:i + 4] for i in range(0, len(items), 4)]
        while batches:
            worker.run_urgent_jobs()
            batch = batches.pop(0)
            recordings = self.mb.recordings_batch([(t, [a["mbid"]]) for t, a, _ in batch])
            if len(recordings) >= 100:
                # Capped: an old hit's many recordings may have pushed out the original.
                # Halves, down to one song per request; a song capped on its own is left
                # to the one-song lookup.
                if len(batch) > 1:
                    batches[:0] = [batch[:len(batch) // 2], batch[len(batch) // 2:]]
                continue
            for title, artist, key in batch:
                own = [r for r in recordings
                       if artist["mbid"] in {(c.get("artist") or {}).get("id") for c in r.get("artist-credit") or []}]
                album = original_from_recordings(own, title, [artist["mbid"]])["album"]
                if album:
                    self._song_found[key] = {"mbid": album["id"], "title": album.get("title") or "",
                                             "year": "" if album["date"] == "9999" else album["date"][:4],
                                             "artist": artist["name"], "artist_mbid": artist["mbid"],
                                             "cover": _cover(album["id"])}

    def _song_albums(self, title: str, artist: str) -> list:
        key = (normalize(clean_title(title)), _bare(artist))
        if key not in self._songs:
            self._songs[key] = [t for t in self._deezer_albums(clean_title(title), artist)
                                if not _NOT_STUDIO_TITLE.search(t)][:SONG_ALBUM_LOOKUPS]
        return self._songs[key]

    def _deezer_albums(self, title: str, artist: str) -> list:
        """The albums Deezer has this song on, most popular first."""
        if not self.deezer:
            return []
        try:
            data = _get_json(f"{DEEZER_API}/search", {"q": f"{artist} {title}", "limit": 25}).get("data") or []
        except (requests.RequestException, ValueError, AttributeError):
            return []
        wanted, names = normalize(title), {_bare(n) for n in artist_candidates(artist)}
        out, seen = [], set()
        for x in data:
            album = (x.get("album") or {}).get("title") or ""
            key = normalize(clean_title(album))
            if (album and key not in seen and _bare((x.get("artist") or {}).get("name") or "") in names
                    and same_song(clean_title(x.get("title_short") or x.get("title") or ""), wanted)):
                seen.add(key)
                out.append(album)
        return out

    def song(self, title: str, artist: str, album: str = "", album_artist: str = "") -> dict:
        """The original studio album of a song: {"album": card} or {"reason": why not}.

        An export's album name first (a studio album the song is on), then the
        albums Deezer has it on (earliest studio album wins), then its recordings on
        albums (batched), then MusicBrainz one song at a time, which also explains a miss.
        """
        if album:
            card = self.album(album, album_artist or artist)
            if card:
                return {"album": card}
        card = self._deezer_answer(title, artist) or self._song_found.get(self._song_key(title, artist))
        if card:
            return {"album": card}
        return resolve_song(self.mb, title, artist)

    # ── Lidarr ──
    def _lidarr(self, kind: str, term: str):
        """Lidarr's lookup results, or None when it can't answer (MusicBrainz decides that
        one; after three failures in a row, all the rest)."""
        if not self.lidarr:
            return None
        key = (kind, term)
        if key not in self._answers:
            from services.lidarr import LidarrError
            try:
                lookup = self.lidarr.lookup_albums if kind == "album" else self.lidarr.lookup_artists
                self._answers[key] = lookup(term)
                self._failures = 0
            except LidarrError as e:
                if e.status == 503:
                    # "Search for '…' failed": the metadata server can't answer this one
                    # search (seen for albums it doesn't have). Not an outage.
                    self._answers[key] = []
                    return []
                self._failures += 1
                if self._failures >= 3:
                    logger.warning(f"[Discover] Lidarr's search keeps failing ({e.message}); using MusicBrainz instead")
                    self.lidarr = None
                return None
        return self._answers[key]


_MISSING = object()


def _title_variants(title: str) -> list:
    """An album title as a store may extend it: "The Life of a Showgirl: The Encore",
    "Stick Season (We'll All Be Here Forever)". The title first, then without the subtitle."""
    out = [title]
    for shorter in (title.split(": ")[0], re.sub(r"\s*\([^()]*\)\s*$", "", title)):
        shorter = shorter.strip()
        if shorter and normalize(shorter) not in {normalize(o) for o in out}:
            out.append(shorter)
    return out


def _days(value: str) -> int:
    """A date ("2026-09-25", "1977") as a day number, for "nearest" comparisons."""
    parts = [int(p) for p in re.findall(r"\d+", value or "")[:3]]
    if not parts:
        return 10 ** 7
    y, m, d = (parts + [7, 1])[:3] if len(parts) == 1 else (parts + [1])[:3]
    try:
        return date(y, max(1, min(m, 12)), max(1, min(d, 28))).toordinal()
    except ValueError:
        return 10 ** 7


def _pick(albums: list, date_of, near: str = ""):
    """Among same-titled studio albums: the one released nearest the chart date (Weezer has
    six albums called "Weezer"), else the earliest."""
    if near:
        return min(albums, key=lambda a: abs(_days(date_of(a)) - _days(near)))
    return min(albums, key=lambda a: date_of(a) or "9999")


def _choose_album(groups: list, title: str, artist: str, near: str = ""):
    """From search results: the studio album matching exactly, None if the exact matches are
    all singles / live albums / compilations, _MISSING if there is no exact match."""
    wanted, names = normalize(clean_title(title)), {_bare(n) for n in artist_candidates(artist)}
    exact = [g for g in groups if normalize(g.get("title")) == wanted and
             any(_bare(c.get("name") or (c.get("artist") or {}).get("name") or "") in names
                 for c in g.get("artist-credit") or [])]
    if not exact:
        return _MISSING
    studio = [g for g in exact if _is_album_for_song(g)]
    return _pick(studio, lambda g: g.get("first-release-date") or "", near) if studio else None


# Chart entries that aren't artists' records: children's albums, soundtracks,
# workout mixes, and sleep / noise albums that only carry Apple's generic genre.
_NOT_MUSIC_GENRES = {"children's music", "soundtrack", "fitness & workout", "spoken word", "comedy", "karaoke"}
_NOT_MUSIC_TITLE = re.compile(r"\b(?:white noise|brown noise|pink noise|deep sleep|sleep music|sleep aid|sleep sounds|"
                              r"baby sleep|lullabies|for relaxation|meditation music|study music)\b", re.I)


def is_record(item: dict) -> bool:
    genres = {(g or "").lower().replace("\u2019", "'") for g in item.get("genres") or []}
    return not genres & _NOT_MUSIC_GENRES and not _NOT_MUSIC_TITLE.search(item.get("title") or "")


def _caa(entry: dict) -> Optional[str]:
    """The exact cover ListenBrainz names (skips the Cover Art Archive's release-group lookup)."""
    if entry.get("caa_id") and entry.get("caa_release_mbid"):
        return f"https://coverartarchive.org/release/{entry['caa_release_mbid']}/{entry['caa_id']}-250.jpg"
    return None


def _card(rg: dict, **extra) -> dict:
    name, aid = _credit(rg)
    card = {"mbid": rg["id"], "title": rg.get("title") or "", "artist": name, "artist_mbid": aid,
            "year": (rg.get("first-release-date") or "")[:4], "cover": _cover(rg["id"])}
    card.update({k: v for k, v in extra.items() if v not in (None, "")})
    return card


def families_of(genres: list) -> list:
    """The Discover genres of an album, from its MusicBrainz genre tags: those with at
    least half the votes of its top tag."""
    ranked = sorted((g for g in genres or [] if g.get("count")), key=lambda g: -g["count"])
    if not ranked:
        return []
    floor = max(1, ranked[0]["count"] / 2)
    out = []
    for g in ranked:
        if g["count"] < floor:
            break
        name = family_of(g.get("name"))
        if name and name not in out:
            out.append(name)
    return out


def _deezer_picture(db, name: str) -> str:
    if (get_setting(db, "deezer_enabled", "true") or "").lower() != "true":
        return ""
    from services.music.pictures import choose_deezer, deezer_candidates
    try:
        candidates = deezer_candidates(name)
    except requests.RequestException:
        return ""
    url, _ = choose_deezer(name, candidates)
    return next((c.get("thumb") or url for c in candidates if c.get("picture") == url), url or "")


# ── Building ─────────────────────────────────────────────────────────────

def chart_country(db) -> str:
    value = (get_setting(db, "music_chart_country", "us") or "us").strip().lower()
    return value if re.fullmatch(r"[a-z]{2}", value) else "us"


def _path(db) -> str:
    return os.path.join(get_setting(db, "data_dir", "/data"), "music_discover.json")


def load(db) -> dict:
    try:
        with open(_path(db)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(db, **sections) -> None:
    with _lock:
        data = load(db)
        data.update(sections)
        path = _path(db)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)


def build_trending(db, resolver: "Resolver", country: str) -> dict:
    """Trending artists and songs, new releases, and new albums from the library's artists."""
    mb = resolver.mb
    errors = []

    def source(label, fn, *args):
        try:
            return fn(*args)
        except (requests.RequestException, ValueError) as e:
            errors.append(label)
            logger.warning(f"[Discover] {label} unavailable: {e}")
            return []

    # The charts are independent pages: fetched side by side.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        songs_f = pool.submit(source, "Apple Music's song chart", apple_chart, country, "songs")
        albums_f = pool.submit(source, "Apple Music's album chart", apple_chart, country, "albums")
        genre_f = {name: pool.submit(source, f"Apple Music's {name} chart", itunes_genre_albums, country, gid)
                   for name, gid in APPLE_GENRES}
    songs = [x for x in songs_f.result() if is_record(x)]
    albums = [x for x in albums_f.result() if is_record(x)]
    by_genre = {name: [x for x in f.result() if is_record(x)] for name, f in genre_f.items()}

    # Artists: by how high and how often they chart, songs and albums together.
    scores = {}
    for chart in (songs, albums):
        for item in chart:
            names = artist_candidates(item["artist"])
            if not names:
                continue
            # Scored by the first-named artist, so "A & B" and "A" add up; looked up
            # whole credit first, as for songs.
            entry = scores.setdefault(normalize(names[-1]), {"names": names, "score": 0})
            entry["score"] += max(1, 101 - item["rank"])
    artists, seen = [], set()
    for entry in sorted(scores.values(), key=lambda e: -e["score"]):
        if len(artists) >= TRENDING_ARTISTS:
            break
        worker.run_urgent_jobs()
        names = entry["names"]
        found = next(filter(None, (resolver.artist(n, fallback=i == len(names) - 1)
                                   for i, n in enumerate(names))), None)
        if found and found["mbid"] not in seen:
            seen.add(found["mbid"])
            artists.append(dict(found, picture=_deezer_picture(db, found["name"])))

    song_cards, seen_songs = [], set()
    resolver.prepare_songs([(s["title"], s["artist"], "", "") for s in songs[:TRENDING_SONGS]])
    for item in songs[:TRENDING_SONGS]:
        key = (normalize(clean_title(item["title"])), normalize(item["artist"]))
        if key in seen_songs:
            continue
        seen_songs.add(key)
        worker.run_urgent_jobs()
        found = resolver.song(item["title"], item["artist"])
        if found.get("album"):
            album = found["album"]
            song_cards.append({"title": clean_title(item["title"]), "artist": item["artist"],
                               "artist_mbid": album["artist_mbid"], "artwork": item["artwork"],
                               "album": dict(album, cover=album.get("cover") or _cover(album["mbid"]))})

    # New releases: charting albums out in the last few weeks, genres merged across charts.
    today = date.today()
    cutoff = (today - timedelta(days=NEW_RELEASE_DAYS)).isoformat()
    candidates = {}
    for label, chart in [("", albums)] + [(name, by_genre[name]) for name, _ in APPLE_GENRES]:
        for item in chart:
            if not item["date"] or not (cutoff <= item["date"] <= today.isoformat()):
                continue
            key = (normalize(clean_title(item["title"])), normalize(item["artist"]))
            entry = candidates.setdefault(key, dict(item, genres=[]))
            entry["genres"] = sorted(set(entry["genres"]) | set(item["genres"]) | ({label} if label else set()))
    releases, seen_albums = [], set()
    newest = sorted(candidates.values(), key=lambda c: c["date"], reverse=True)[:NEW_RELEASES_MAX]
    resolver.prefetch([(c["title"], c["artist"], c["date"]) for c in newest])
    for item in newest:
        worker.run_urgent_jobs()
        card = resolver.album(item["title"], item["artist"], near=item["date"])
        if card and card["mbid"] not in seen_albums:
            seen_albums.add(card["mbid"])
            releases.append(dict(card, cover=item["artwork"] or card["cover"], date=item["date"],
                                 genres=item["genres"]))

    # New (and announced) albums by artists already in the library.
    mine = {mbid for (mbid,) in db.query(MusicArtist.mbid).all()}
    fresh = {r["rgid"]: r for r in source("ListenBrainz's new releases", lb_fresh_releases)
             if mine.intersection(r["artist_mbids"])}
    yours = []
    for rg in _release_groups(mb, list(fresh)):   # their types, 50 per request
        r = fresh[rg["id"]]
        if _is_studio(rg):
            yours.append(_card(rg, date=r["date"], upcoming=r["date"] > today.isoformat() or None,
                               cover=r.get("cover")))
    yours.sort(key=lambda a: a.get("date") or "", reverse=True)

    return {"country": country, "built": time.time(), "errors": errors, "artists": artists,
            "songs": song_cards, "releases": releases, "yours": yours}


def build_all_time(db, mb: MusicBrainz, save_progress=None) -> dict:
    """ListenBrainz's most-listened albums, studio albums only, sorted into genres."""
    entries, seen = [], set()
    for offset in range(0, ALL_TIME_DEPTH, 100):
        page = lb_top_release_groups(offset)
        for r in page:
            rgid = r.get("release_group_mbid")
            if rgid and rgid not in seen:
                seen.add(rgid)
                entries.append(r)
        if len(page) < 100:
            break
    genres, by_id = {}, {r["release_group_mbid"]: r for r in entries}
    for done, rg in _release_groups(mb, list(by_id), progress=True):
        if rg and _is_studio(rg):
            r = by_id[rg["id"]]
            # Search results carry tags (genres among them), not the genres list.
            for name in families_of(rg.get("tags") or rg.get("genres")):
                albums = genres.setdefault(name, [])
                if len(albums) < PER_GENRE:
                    albums.append(_card(rg, listens=r.get("listen_count"), cover=_caa(r)))
        if save_progress and done:
            save_progress(genres, done, len(entries))
    return {"built": time.time(), "genres": genres, "checked": len(entries)}


def _release_groups(mb: MusicBrainz, rgids: list, progress: bool = False):
    """Release groups by id, 50 per MusicBrainz request, in the order asked.
    Yields each release group; with progress=True yields (albums done at the end
    of a batch or 0, release group or None)."""
    for i in range(0, len(rgids), 50):
        worker.run_urgent_jobs()
        batch = rgids[i:i + 50]
        found = mb.release_groups_by_id(batch)
        for j, rgid in enumerate(batch):
            rg = found.get(rgid)
            if progress:
                yield (i + j + 1 if j == len(batch) - 1 else 0), rg
            elif rg:
                yield rg


# ── Lists: curated album lists (MusicBrainz release group series) ────────

LISTS_MAX_AGE = 30 * 86400
_SERIES_ID = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def list_ids(db) -> list:
    from services.music.settings import DEFAULTS
    value = get_setting(db, "music_lists", DEFAULTS["music_lists"]) or ""
    return [m.group(1) for m in (_SERIES_ID.search(p) for p in value.split(",")) if m]


def _rank(rel: dict, position: int) -> int:
    number = (rel.get("attribute-values") or {}).get("number") or rel.get("ordering-key")
    try:
        return int(str(number).split(".")[0])
    except (TypeError, ValueError):
        return position


def build_list(mb: MusicBrainz, series_id: str) -> dict:
    """One curated list, in its order, with each album's artist and year."""
    series = mb.series(series_id)
    rels = [r for r in series.get("relations") or [] if (r.get("release_group") or {}).get("id")]
    rels = sorted(enumerate(rels, 1), key=lambda pr: _rank(pr[1], pr[0]))
    details = {rg["id"]: rg for rg in _release_groups(mb, [r["release_group"]["id"] for _, r in rels])}
    albums = []
    for position, r in rels:
        rg = details.get(r["release_group"]["id"]) or r["release_group"]
        albums.append(_card(rg, rank=_rank(r, position), type=None if _is_studio(rg) else _type_of(rg)))
    return {"id": series_id, "name": series.get("name") or "", "built": time.time(), "albums": albums}


def _type_of(rg: dict) -> str:
    secondary = rg.get("secondary-types") or []
    return secondary[0] if secondary else (rg.get("primary-type") or "")


def build_lists(db, mb: MusicBrainz) -> dict:
    # Stamp the ids this build started from: a list added or removed meanwhile
    # then makes the result stale, so the next ensure_fresh builds again (#252).
    ids = list_ids(db)
    out = []
    for sid in ids:
        worker.run_urgent_jobs()
        try:
            out.append(build_list(mb, sid))
        except MusicBrainzError as e:
            if e.status != 404:
                raise
            logger.warning(f"[Discover] MusicBrainz has no list {sid}; skipped")
    return {"built": time.time(), "ids": ids, "lists": out}


def _trending_job(db):
    try:
        mb = MusicBrainz.from_settings(db)
        _save(db, trending=build_trending(db, Resolver(db, mb), chart_country(db)), trending_error=None)
        _failed["trending"] = 0.0
    except MusicBrainzError as e:
        _failed["trending"] = time.time()
        _save(db, trending_error=e.message)
        raise
    finally:
        jobs["trending"] = False


def _all_time_job(db):
    def progress(genres, done, total):
        _save(db, all_time_partial={"genres": genres, "done": done, "total": total})
    try:
        mb = MusicBrainz.from_settings(db)
        result = build_all_time(db, mb, progress)
        _save(db, all_time=result, all_time_partial=None, all_time_error=None)
        _failed["all_time"] = 0.0
    except (MusicBrainzError, requests.RequestException, ValueError) as e:
        _failed["all_time"] = time.time()
        _save(db, all_time_error=getattr(e, "message", None) or f"ListenBrainz unavailable ({e.__class__.__name__})")
        raise
    finally:
        jobs["all_time"] = False


def _lists_job(db):
    try:
        _save(db, lists=build_lists(db, MusicBrainz.from_settings(db)), lists_error=None)
        _failed["lists"] = 0.0
    except MusicBrainzError as e:
        _failed["lists"] = time.time()
        _save(db, lists_error=e.message)
        raise
    finally:
        jobs["lists"] = False


def ensure_fresh(db, force: bool = False) -> dict:
    """Queue whatever is missing or stale (once; failures back off). Returns the cache."""
    data = load(db)
    now = time.time()
    trending = data.get("trending") or {}
    stale = (not trending or trending.get("country") != chart_country(db)
             or now - trending.get("built", 0) > TRENDING_MAX_AGE)
    if (force or stale) and not jobs["trending"] and (force or now - _failed["trending"] > RETRY_AFTER_FAILURE):
        jobs["trending"] = True
        # Nothing to show yet: someone is looking at an empty page, so it goes ahead
        # of the background work. A daily refresh stays in the background.
        worker.submit(_trending_job, worker.NORMAL if not trending else worker.BACKGROUND,
                      "Discover: trending and new releases")
    all_time = data.get("all_time") or {}
    stale = not all_time or now - all_time.get("built", 0) > ALL_TIME_MAX_AGE
    if (force or stale) and not jobs["all_time"] and (force or now - _failed["all_time"] > RETRY_AFTER_FAILURE):
        jobs["all_time"] = True
        worker.submit(_all_time_job, worker.BACKGROUND, "Discover: top albums of all time")
    lists = data.get("lists") or {}
    stale = (not lists or lists.get("ids") != list_ids(db) or now - lists.get("built", 0) > LISTS_MAX_AGE)
    if (force or stale) and not jobs["lists"] and (force or now - _failed["lists"] > RETRY_AFTER_FAILURE):
        jobs["lists"] = True
        worker.submit(_lists_job, worker.NORMAL if not lists else worker.BACKGROUND, "Discover: album lists")
    return data


# ── The page ─────────────────────────────────────────────────────────────

def page(db, user) -> dict:
    """Everything Discover → Music shows, with each album's status in the library."""
    from services.music import spotify
    data = ensure_fresh(db)
    trending = data.get("trending") or {}
    complete = data.get("all_time") or {}
    partial = data.get("all_time_partial") or {}
    genres = complete.get("genres") or partial.get("genres") or {}
    imports = spotify.summaries(db, user)
    from_playlists = spotify.albums_for_discover(db, user)

    lists = data.get("lists") or {}
    with_status = _status_adder(db, (trending.get("releases") or []) + (trending.get("yours") or [])
                                + from_playlists + [s["album"] for s in trending.get("songs") or []]
                                + [a for albums in genres.values() for a in albums])

    known_artists = {mbid for (mbid,) in db.query(MusicArtist.mbid).all()}
    genre_counts = {}
    for a in trending.get("releases") or []:
        for g in a.get("genres") or []:
            genre_counts[g] = genre_counts.get(g, 0) + 1
    order = FAMILY_ORDER
    return {
        "country": trending.get("country") or chart_country(db),
        "building": dict(jobs),
        "errors": {"trending": data.get("trending_error"), "all_time": data.get("all_time_error"),
                   "lists": data.get("lists_error"), "sources": trending.get("errors") or []},
        "trending": {"built": trending.get("built"),
                     "artists": [dict(a, in_library=a["mbid"] in known_artists) for a in trending.get("artists") or []],
                     "songs": [dict(s, album=with_status(s["album"])) for s in trending.get("songs") or []]},
        "new": {"releases": [with_status(a) for a in trending.get("releases") or []],
                "yours": [with_status(a) for a in trending.get("yours") or []],
                "genres": sorted(((g, n) for g, n in genre_counts.items() if n >= 3),
                                 key=lambda kv: (-kv[1], kv[0]))[:12]},
        "all_time": {"built": complete.get("built"), "complete": bool(complete.get("genres")),
                     "progress": {"done": partial.get("done", 0), "total": partial.get("total", 0)},
                     "genres": [{"name": name, "albums": [with_status(a) for a in genres[name]]}
                                for name in order if len(genres.get(name) or []) >= MIN_GENRE_ALBUMS]},
        "spotify": {"imports": imports, "albums": [with_status(a) for a in from_playlists]},
        "lists": {"built": lists.get("built"),
                  "items": [{"id": l["id"], "name": l["name"], "count": len(l["albums"])}
                            for l in lists.get("lists") or []]},
    }


def list_page(db, series_id: str) -> Optional[dict]:
    """One list's albums, with their status in the library (loaded when its tab opens)."""
    for entry in (load(db).get("lists") or {}).get("lists") or []:
        if entry["id"] == series_id:
            with_status = _status_adder(db, entry["albums"])
            return {"id": entry["id"], "name": entry["name"], "albums": [with_status(a) for a in entry["albums"]]}
    return None


def _status_adder(db, cards: list):
    """A function adding each card's status in the library (one query per 400 albums,
    under SQLite's limit on query parameters)."""
    from services.music import library
    mbids = list({c["mbid"] for c in cards})
    statuses = {}
    for i in range(0, len(mbids), 400):
        statuses.update(_statuses(db, mbids[i:i + 400]))

    def with_status(card):
        row, status, progress = statuses.get(card["mbid"], (None, library.AVAILABLE, None))
        return dict(card, status=status, progress=progress)
    return with_status
