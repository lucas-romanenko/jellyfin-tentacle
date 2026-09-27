"""Discover → Music: what's trending, what's new, the all-time top albums per
genre, and the albums behind imported Spotify playlists.

Every source is free and needs no key:
- Apple Music's charts (per country, updated daily): the most-played songs and
  albums, and the per-genre album charts, for "Trending" and "New releases";
- ListenBrainz: the all-time most-listened albums (sitewide statistics) for
  "Top of all time", and new releases with MusicBrainz ids for "New from your
  artists";
- MusicBrainz: every card is resolved to a release group, so album pages,
  status badges and requests work as everywhere else; its genre tags sort the
  all-time albums into genres;
- Deezer: pictures for trending artists (exact name), if it is on as a picture
  source.

The sections are built by the music worker at low priority (requests always go
first) and kept in {data_dir}/music_discover.json: trending and new releases
about daily, the all-time list monthly. The page reads that file and adds each
album's status in the library.
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
from services.music.browse import _cover, _credit, _is_studio, _statuses, normalize, original_album_for_song
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
jobs = {"trending": False, "all_time": False}   # queued or running
_failed = {"trending": 0.0, "all_time": 0.0}


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
    n = normalize(name)
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


def build_trending(db, mb: MusicBrainz, country: str) -> dict:
    """Trending artists and songs, new releases, and new albums from the library's artists."""
    errors = []

    def source(label, fn, *args):
        try:
            return fn(*args)
        except (requests.RequestException, ValueError) as e:
            errors.append(label)
            logger.warning(f"[Discover] {label} unavailable: {e}")
            return []

    songs = [x for x in source("Apple Music's song chart", apple_chart, country, "songs") if is_record(x)]
    albums = [x for x in source("Apple Music's album chart", apple_chart, country, "albums") if is_record(x)]
    by_genre = {name: [x for x in source(f"Apple Music's {name} chart", itunes_genre_albums, country, gid)
                       if is_record(x)]
                for name, gid in APPLE_GENRES}

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
        found = next(filter(None, (resolve_artist(mb, n) for n in entry["names"])), None)
        if found and found["mbid"] not in seen:
            seen.add(found["mbid"])
            artists.append(dict(found, picture=_deezer_picture(db, found["name"])))

    song_cards, seen_songs = [], set()
    for item in songs[:TRENDING_SONGS]:
        key = (normalize(clean_title(item["title"])), normalize(item["artist"]))
        if key in seen_songs:
            continue
        seen_songs.add(key)
        worker.run_urgent_jobs()
        found = resolve_song(mb, item["title"], item["artist"])
        if found.get("album"):
            album = found["album"]
            song_cards.append({"title": clean_title(item["title"]), "artist": item["artist"],
                               "artist_mbid": album["artist_mbid"], "artwork": item["artwork"],
                               "album": dict(album, cover=_cover(album["mbid"]))})

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
    for item in sorted(candidates.values(), key=lambda c: c["date"], reverse=True)[:NEW_RELEASES_MAX]:
        worker.run_urgent_jobs()
        rg = find_album(mb, item["title"], item["artist"])
        if rg and rg["id"] not in seen_albums:
            seen_albums.add(rg["id"])
            releases.append(_card(rg, cover=item["artwork"] or None, date=item["date"], genres=item["genres"]))

    # New (and announced) albums by artists already in the library.
    mine = {mbid for (mbid,) in db.query(MusicArtist.mbid).all()}
    yours = []
    for r in source("ListenBrainz's new releases", lb_fresh_releases):
        if not mine.intersection(r["artist_mbids"]) or r["rgid"] in {y["mbid"] for y in yours}:
            continue
        worker.run_urgent_jobs()
        try:
            rg = mb.release_group(r["rgid"])
        except MusicBrainzError as e:
            if e.status == 404:
                continue
            raise
        if _is_studio(rg):
            yours.append(_card(rg, date=r["date"], upcoming=r["date"] > today.isoformat() or None, cover=r.get("cover")))
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
    genres = {}
    for i, r in enumerate(entries):
        worker.run_urgent_jobs()
        try:
            rg = mb.release_group(r["release_group_mbid"])
        except MusicBrainzError as e:
            if e.status == 404:
                continue
            raise
        if _is_studio(rg):
            for name in families_of(rg.get("genres")):
                albums = genres.setdefault(name, [])
                if len(albums) < PER_GENRE:
                    albums.append(_card(rg, listens=r.get("listen_count"), cover=_caa(r)))
        if save_progress and (i + 1) % 50 == 0:
            save_progress(genres, i + 1, len(entries))
    return {"built": time.time(), "genres": genres, "checked": len(entries)}


def _trending_job(db):
    try:
        mb = MusicBrainz.from_settings(db)
        _save(db, trending=build_trending(db, mb, chart_country(db)), trending_error=None)
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


def ensure_fresh(db, force: bool = False) -> dict:
    """Queue whatever is missing or stale (once; failures back off). Returns the cache."""
    data = load(db)
    now = time.time()
    trending = data.get("trending") or {}
    stale = (not trending or trending.get("country") != chart_country(db)
             or now - trending.get("built", 0) > TRENDING_MAX_AGE)
    if (force or stale) and not jobs["trending"] and (force or now - _failed["trending"] > RETRY_AFTER_FAILURE):
        jobs["trending"] = True
        worker.submit(_trending_job, worker.BACKGROUND, "Discover: trending and new releases")
    all_time = data.get("all_time") or {}
    stale = not all_time or now - all_time.get("built", 0) > ALL_TIME_MAX_AGE
    if (force or stale) and not jobs["all_time"] and (force or now - _failed["all_time"] > RETRY_AFTER_FAILURE):
        jobs["all_time"] = True
        worker.submit(_all_time_job, worker.BACKGROUND, "Discover: top albums of all time")
    return data


# ── The page ─────────────────────────────────────────────────────────────

def page(db, user) -> dict:
    """Everything Discover → Music shows, with each album's status in the library."""
    from services.music import library, spotify
    data = ensure_fresh(db)
    trending = data.get("trending") or {}
    complete = data.get("all_time") or {}
    partial = data.get("all_time_partial") or {}
    genres = complete.get("genres") or partial.get("genres") or {}
    imports = spotify.summaries(db, user)
    from_playlists = spotify.albums_for_discover(db, user)

    cards = (trending.get("releases") or []) + (trending.get("yours") or []) + from_playlists + \
        [s["album"] for s in trending.get("songs") or []] + [a for albums in genres.values() for a in albums]
    mbids = list({c["mbid"] for c in cards})
    statuses = {}
    for i in range(0, len(mbids), 400):   # stays under SQLite's limit on query parameters
        statuses.update(_statuses(db, mbids[i:i + 400]))

    def with_status(card):
        row, status, progress = statuses.get(card["mbid"], (None, library.AVAILABLE, None))
        return dict(card, status=status, progress=progress)

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
                   "sources": trending.get("errors") or []},
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
    }
